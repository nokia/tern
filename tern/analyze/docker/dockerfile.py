# -*- coding: utf-8 -*-
#
# Copyright (c) 2017-2020 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

"""
Dockerfile information retrieval and modification
"""

from dockerfile_parse import DockerfileParser
import re
import logging

from tern.utils import general
from tern.utils import constants
from tern.analyze.docker import container
from tern.analyze import common

# global logger
logger = logging.getLogger(constants.logger_name)

directives = ['FROM',
              'ARG',
              'ADD',
              'RUN',
              'ENV',
              'COPY',
              'ENTRYPOINT',
              'WORKDIR',
              'VOLUME',
              'EXPOSE',
              'CMD']

# regex for matching lines in a dockerfile
comments = re.compile('^#')
line_indent = re.compile('.*\\\\$')
tabs = re.compile('\t')

# regex strings
bash_var = R'[\$\{\}]'

# strings
tag_separator = ':'


class Dockerfile():
    ''' This class is used as a wrapper to store dockerfile information
    retrieved from the parser.'''

    def __init__(self):
        self.structure = None
        self.envs = None
        self.prev_env = None
        self.filepath = ""
        self.parent_images = []

    def is_none(self):
        """Check if the object is empty."""
        is_none = True
        if (self.structure or
                self.envs or
                self.prev_env or
                self.filepath):
            is_none = False
        return is_none


def get_dockerfile_obj(dockerfile_name, prev_env=None):
    '''Given a Dockerfile, create a Dockerfile parser object to be used later.
    dockerfile_name: This is the path to the Dockerfile including the
                            file name
    prev_env: These are environment variables that may have been used in
    previous stages in a multistage docker build. Should be a python dictionary
    of the form {'ENV': 'value',...}'''
    dfobj = Dockerfile()
    with open(dockerfile_name) as f:
        parser = DockerfileParser(parent_env=prev_env, fileobj=f)
        dfobj.filepath = dockerfile_name
        dfobj.structure = parser.structure
        dfobj.envs = parser.envs
        dfobj.prev_env = prev_env
        dfobj.parent_images = parser.parent_images
    return dfobj


def replace_env(key_value_dict, df_structure_dict):
    '''Replace the environment variables in the key_value_dict dictionary
    with its corresponding value in the df_line_dict dictionary
    key_value_dict: a dictionary of key-value pairs like envs in the dockerfile
                    object
    df_structure_dict: a dictionary from the dockerfile object's structure'''
    for key, val in key_value_dict.items():
        envvar1 = '$' + key
        envvar2 = '${' + key + '}'
        df_structure_dict['content'] = df_structure_dict['content'].replace(
            envvar1, val)
        df_structure_dict['content'] = df_structure_dict['content'].replace(
            envvar2, val)
        df_structure_dict['value'] = df_structure_dict['value'].replace(
            envvar1, val)
        df_structure_dict['value'] = df_structure_dict['value'].replace(
            envvar2, val)


def expand_vars(dfobj):
    '''Replace the environment variables with their values if known
    dfobj: the Dockerfile object created using get_dockerfile_obj'''
    if dfobj.envs:
        for obj in dfobj.structure:
            replace_env(dfobj.envs, obj)
    if dfobj.prev_env:
        for obj in dfobj.structure:
            replace_env(dfobj.prev_env, obj)


def expand_arg(dfobj):
    '''Replace the ARG variables with their values if known
    dfobj: the Dockerfile object created using get_dockerfile_obj'''
    # get arg dictionary
    arg_dict = {}
    for instruction_desc in dfobj.structure:
        if instruction_desc['instruction'] == 'ARG':
            instruction_value_split = instruction_desc['value'].split('=')
            # contains '='
            if len(instruction_value_split) == 2:
                key = instruction_value_split[0].strip(' ')
                value = instruction_value_split[1].strip(' ')
                arg_dict[key] = value
    # expand arg variables
    if arg_dict:
        for obj in dfobj.structure:
            replace_env(arg_dict, obj)
    # Update dfobj parent image just in case ARG value was used in FROM line
    update_parent_images(dfobj)


def update_parent_images(dfobj):
    '''Given a Dockerfile object, update the parent_images list. This function
    will be useful after ARG values have been replaced in expand_arg() that
    can sometimes affect the FROM line(s) of a Dockerfile.'''
    new_parent_list = []
    for cmd_dict in dfobj.structure:
        if cmd_dict['instruction'] == 'FROM':
            new_parent_list.append(re.split(" as", cmd_dict['value'],
                                            flags=re.IGNORECASE)[0])
    dfobj.parent_images = new_parent_list


def parse_from_image(dfobj):
    '''Get a list of dictionaries from the FROM instruction. The dictionary
    should be of the form:
        [{'name': <image name used (either from dockerhub or full name)>,
          'tag': <image tag>,
          'digest_type': <the hashing algorithm used>
          'digest': <image digest>}..]'''
    image_list = []
    for image_string in dfobj.parent_images:
        image_list.append(general.parse_image_string(image_string))
    return image_list


def expand_from_images(dfobj):
    '''Replace all parent_image values with their full image@digest_type:digest
    value. Update the structure dictionary with the same information.
    dfobj: the Dockerfile object created using get_dockerfile_obj'''
    # update parent_images
    images = parse_from_image(dfobj)
    for i, img in enumerate(images):
        # don't re-pull digest if already available
        if not img['digest_type'] and not img['digest']:
            if not img['tag']:
                img['tag'] = 'latest'
            image = container.get_image(img['name'] + tag_separator +
                                        img['tag'])
            if image is not None:
                dfobj.parent_images[i] = container.get_image_digest(image)
            else:
                logger.error("Error pinning digest to '%s'. Image not found.",
                             dfobj.parent_images[i])
    # update structure
    counter = 0
    for i, command_dict in enumerate(dfobj.structure):
        if command_dict['instruction'] == 'FROM':
            # Pull digest in order of parent_images
            dfobj.structure[i]['content'] = command_dict['instruction'] + \
                ' ' + dfobj.parent_images[counter] + '\n'
            dfobj.structure[i]['value'] = dfobj.parent_images[counter]
            counter = counter + 1


def expand_package(command_dict, package, version, pinning_separator):
    '''Update the given dockerfile object with the pinned package
    and version information. '''
    command_dict['value'] = command_dict['value'].replace(package, package +
                                                          pinning_separator +
                                                          version, 1)
    # Update 'content' to match 'value' in dfobj
    command_dict['content'] = command_dict['instruction'] + ' ' + \
        command_dict['value'] + '\n'


def get_run_layers(dfobj):
    '''Given a dockerfile object, collect a list of RUN command dictionaries'''
    run_list = []
    for command_dict in dfobj.structure:
        if command_dict['instruction'] == 'RUN':
            run_list.append(command_dict)
    return run_list


def package_in_dockerfile(command_dict, pkg_name):
    '''Return True if pkg_name is a package specified in the command_dict
    RUN line provided, otherwise return False.'''
    command_words, _ = common.filter_install_commands(command_dict['value'])
    for command in command_words:
        if pkg_name in command.words:
            return True
    return False


def get_command_list(dfobj_structure):
    '''Given a dockerfile object structure, return the list of commands
    from the list of dictionaries. Useful when you don't want to loop
    through the dictionary for commands'''
    cmd_list = []
    for cmd_dict in dfobj_structure:
        if cmd_dict['instruction'] != 'COMMENT':
            cmd_list.append(cmd_dict['content'].rstrip())
    return cmd_list


def find_git_info(line, dockerfile_path):
    '''Given a line of ADD command and the path of dockerfile,
    return the information(string format) on the git project name and sha
    if the dockerfile is included in a git repository.
    ADD command has a general format:
    ADD [--chown=<user>:<group>] <src> <dst>
    Currently we parse the <src>, but not use it.
    '''
    args = line.split(' ')
    src_path = ''
    # check if --chown exists
    if args[1].startswith('--chown'):
        # check if the line is valid
        if len(args) < 4:
            logger.error('Invalid ADD command line')
        src_path = args[2]
    else:
        # the line has no --chown option
        if len(args) < 3:
            logger.error('Invalid ADD command line')
        src_path = args[1]
    # log the parsed src_path
    logger.debug('Parsed src_path is %s', src_path)
    # get the git project info
    comment_line = common.check_git_src(dockerfile_path)
    return comment_line


def expand_add_command(dfobj):
    dockerfile_path = dfobj.filepath
    for i, command_dict in enumerate(dfobj.structure):
        if command_dict['instruction'] == 'ADD':
            comment_line = find_git_info(command_dict['content'],
                                         dockerfile_path)
            dfobj.structure[i]['content'] = \
                command_dict['content'].strip('\n') + \
                ' # ' + comment_line + '\n'
            dfobj.structure[i]['value'] = command_dict['value']\
                + ' # ' + comment_line


def create_locked_dockerfile(dfobj):
    '''Given a dockerfile object, the information in a new Dockerfile object
    Copy the dfobj info to the destination output Dockerfile location'''
    # packages in RUN lines, ENV, and ARG values are already expanded
    expand_from_images(dfobj)
    expand_add_command(dfobj)
    # create the output file
    dfile = ''
    for command_dict in dfobj.structure:
        dfile = dfile + command_dict['content']
    return dfile


def write_locked_dockerfile(dfile, destination=None):
    '''Write the pinned Dockerfile to a file'''
    if destination is not None:
        file_name = destination
    else:
        file_name = constants.locked_dockerfile
    with open(file_name, 'w') as f:
        f.write(dfile)
