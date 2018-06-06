# Copyright 2011 Tencent Inc.
#
# Authors: Huan Yu <huanyu@tencent.com>
#          Feng Chen <phongchen@tencent.com>
#          Yi Wang <yiwang@tencent.com>
#          Chong Peng <michaelpeng@tencent.com>

"""
 Blade is a software building system built upon SCons, but restricts
 the generality and flexibility of SCons to prevent unnecessary
 error-prone complexity.  With Blade, users wrote a BUILD file and
 put it in each of the source directory.  In each BUILD file, there
 could be one or more build rules, each has a TARGET NAME, source
 files and dependent targets.  Blade suports the following types of
 build rules:


    cc_binary         -- build an executable binary from C++ source
    cc_library        -- build a library from C++ source
    cc_plugin         -- build a plugin from C++ source
    cc_test           -- build a unittest binary from C++ source
    cc_benchmark      -- build a benchmark binary from C++ source
    gen_rule          -- used to specify a general building rule
    java_jar          -- build java jar from java source files
    lex_yacc_library  -- build a library from lex/yacc source
    proto_library     -- build a library from Protobuf source
    thrift_library    -- build a library from Thrift source
    fbthrift_library  -- build a library from Thrift source for Facebook's Thrift Cpp2
    resource_library  -- build resource library and gen header files
    swig_library      -- build swig library for python and java

 A target may depend on other target(s), where the dependency is
 transitive.  A dependent target is referred by a TARGET ID, which
 has either of the following forms:

   //<source_dir>:<target_name> -- target defined in <source_dir>/BUILD
   :<target_name>               -- target defined in the current BUILD file
   #<target_name>               -- target is a system library, e.g., pthread

 where <source_dir> is an absolute path rooted at the source tree and
 specifying where the BUILD file locates, <target_name> specifies a
 target in the BUILD file, and '//' denotes the root of the source tree.

 Users invoke Blade from the command line to build (or clean, or
 test) one or more rule/targets.  In the command line, a target id
 is specified in either of the following forms:

   <path>:<target_name> -- to build target defined in <path>/BUILD
   <path>               -- to build all targets defined in <path>/BUILD
   <path>/...           -- to build all targets in all BUILD files in
                           <path> and its desendant directories.

 Note that <path> in command line targets is an operating system
 path, which might be a relative path, but <source_dir> in a BUILD
 referring to a dependent target must be an absolute path, rooted at
 '//'.

 For example, the following command line

    blade build base mapreduce_lite/... parallel_svm:perf_test

 builds all targets in base/BUILD, all targets in all BUILDs under
 directory mapreduce_lite, and the target perf_test defined in
 parallel_svm/BUILD
"""


import cProfile
import datetime
import errno
import fcntl
import os
import pstats
import signal
import subprocess
import sys
import time
import traceback
from string import Template

import blade
import build_attributes
import console
import configparse

from blade import Blade
from blade_util import get_cwd
from blade_util import lock_file
from blade_util import unlock_file
from command_args import CmdArguments
from configparse import BladeConfig
from load_build_files import find_blade_root_dir


# Run target
_TARGETS = None


_BLADE_ROOT_DIR = None
_WORKING_DIR = None


def is_svn_client(blade_root_dir):
    # We suppose that BLADE_ROOT is under svn root dir now.
    return os.path.exists(os.path.join(blade_root_dir, '.svn'))


# For our opensource projects (toft, thirdparty, foxy etc.), we mkdir a project
# dir , add subdirs are github repos, here we need to fix out the git ROOT for
# each build target
def is_git_client(blade_root_dir, target, working_dir):
    if target.endswith('...'):
        target = target[:-3]
    if os.path.exists(os.path.join(blade_root_dir, '.git')):
        return (True, blade_root_dir, target)
    blade_root_dir = os.path.normpath(blade_root_dir)
    root_dirs = blade_root_dir.split('/')
    full_target = os.path.normpath(os.path.join(working_dir, target))
    dirs = full_target.split('/')
    index = len(root_dirs)
    while index <= len(dirs):
        # Find git repo root dir
        top_dir = '/'.join(dirs[0:index])
        # Get subdir under git repo root
        sub_dir = '/'.join(dirs[index:])
        index += 1
        if (os.path.exists(os.path.join(top_dir, '.git'))):
            return (True, top_dir, sub_dir)
    return (False, None, None)


def _normalize_target_path(target):
    if target.endswith('...'):
        target = target[:-3]
    index = target.find(':')
    if index != -1:
        target = target[index + 1:]
    if target and not target.endswith('/'):
        target = target + '/'
    return target


def _get_opened_files(targets, blade_root_dir, working_dir):
    check_dir = set()
    opened_files = set()
    blade_root_dir = os.path.normpath(blade_root_dir)

    for target in targets:
        target = _normalize_target_path(target)
        d = os.path.dirname(target)
        if d in check_dir:
            return
        check_dir.add(d)
        output = []
        if is_svn_client(blade_root_dir):
            full_target = os.path.normpath(os.path.join(working_dir, d))
            top_dir = full_target[len(blade_root_dir) + 1:]
            output = os.popen('svn st %s' % top_dir).read().split('\n')
        else:
            (is_git, git_root, git_subdir) = is_git_client(blade_root_dir, target, working_dir)
            if is_git:
                os.chdir(git_root)
                status_cmd = 'git status --porcelain %s' % (git_subdir)
                output = os.popen(status_cmd).read().split('\n')
                os.chdir(blade_root_dir)
            else:
                console.warning('unknown source client type, NOT svn OR git')
        for f in output:
            seg = f.strip().split(' ')
            if seg[0] != 'M' and seg[0] != 'A':
                continue
            f = seg[len(seg) - 1]
            if f.endswith('.h') or f.endswith('.hpp') or f.endswith('.cc') or f.endswith('.cpp'):
                fullpath = os.path.join(os.getcwd(), f)
                opened_files.add(fullpath)
    return opened_files


def _check_code_style(targets):
    cpplint = configparse.blade_config.configs['cc_config']['cpplint']
    if not cpplint:
        console.info('cpplint disabled')
        return 0
    opened_files = _get_opened_files(targets, _BLADE_ROOT_DIR, _WORKING_DIR)
    if not opened_files:
        return 0
    console.info('Begin to check code style for changed source code')
    p = subprocess.Popen(('%s %s' % (cpplint, ' '.join(opened_files))), shell=True)
    try:
        p.wait()
        if p.returncode:
            if p.returncode == 127:
                msg = ("Can't execute '{0}' to check style, you can config the "
                       "'cpplint' option to be a valid cpplint path in the "
                       "'cc_config' section of blade.conf or BLADE_ROOT, or "
                       "make sure '{0}' command is correct.").format(cpplint)
            else:
                msg = 'Please fixing style warnings before submitting the code!'
            console.warning(msg)
    except KeyboardInterrupt, e:
        console.error(str(e))
        return 1
    return 0


def _run_native_builder(cmd):
    p = subprocess.Popen(subprocess.list2cmdline(cmd), shell=True)
    try:
        p.wait()
        return p.returncode
    except:  # KeyboardInterrupt
        return 1


def native_builder_options(options):
    '''
    Setup some options which are same in different native builders happenly
    '''
    build_options = []
    if options.dry_run:
        build_options.append('-n')
    if options.native_builder_options:
        build_options.append(options.native_builder_options)
    return build_options


def _scons_build(options):
    cmd = ['scons']
    cmd += native_builder_options(options)
    cmd.append('-j%s' % (options.jobs or blade.blade.parallel_jobs_num()))
    cmd += ['--duplicate=soft-copy', '--cache-show']
    if options.keep_going:
        cmd.append('-k')
    return _run_native_builder(cmd)


def _ninja_build(options):
    cmd = ['ninja']
    cmd += native_builder_options(options)
    if options.jobs:
        # Unlike scons, ninja enable parallel building defaultly,
        # so only set it when user specified it explicitly.
        cmd.append('-j%s' % options.jobs)
    if options.keep_going:
        cmd.append('-k0')
    if options.verbose:
        cmd.append('-v')
    return _run_native_builder(cmd)


def build(options):
    _check_code_style(_TARGETS)
    if options.native_builder == 'ninja':
        returncode = _ninja_build(options)
    else:
        returncode = _scons_build(options)
    if returncode != 0:
        console.error('building failure')
    else:
        console.info('building done.')
    console.flush()
    return returncode


def run(options):
    ret = build(options)
    if ret != 0:
        return ret
    run_target = _TARGETS[0]
    return blade.blade.run(run_target)


def test(options):
    if not options.no_build:
        ret = build(options)
        if ret != 0:
            return ret
    return blade.blade.test()


def clean(options):
    console.info('cleaning...(hint: please specify --generate-dynamic to '
                 'clean your so)')
    cmd = [options.native_builder]
    # cmd += native_builder_options(options)
    if options.native_builder == 'ninja':
        cmd += ['-t', 'clean']
    else:
        cmd += ['--duplicate=soft-copy', '-c', '-s', '--cache-show']
    returncode = _run_native_builder(cmd)
    console.info('cleaning done.')
    return returncode


def query(options):
    query_targets = _TARGETS
    if not query_targets:
        query_targets = ['.']
    return blade.blade.query(query_targets)


def lock_workspace():
    lock_file_fd = open('.Building.lock', 'w')
    old_fd_flags = fcntl.fcntl(lock_file_fd.fileno(), fcntl.F_GETFD)
    fcntl.fcntl(lock_file_fd.fileno(), fcntl.F_SETFD, old_fd_flags | fcntl.FD_CLOEXEC)
    locked, ret_code = lock_file(lock_file_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not locked:
        if ret_code == errno.EAGAIN:
            console.error_exit(
                    'There is already an active building in current source '
                    'dir tree. Blade will exit...')
        else:
            console.error_exit('Lock exception, please try it later.')
    return lock_file_fd


def unlock_workspace(lock_file_fd):
    try:
        unlock_file(lock_file_fd.fileno())
        lock_file_fd.close()
    except OSError:
        pass


def parse_command_line():
    parsed_command_line = CmdArguments()
    command = parsed_command_line.get_command()
    options = parsed_command_line.get_options()
    targets = parsed_command_line.get_targets()
    return command, options, targets


def load_config(options, blade_root_dir):
    # Init global build attributes
    build_attributes.attributes = build_attributes.TargetAttributes(options)

    # Init global configuration manager
    configparse.blade_config = BladeConfig(blade_root_dir)
    configparse.blade_config.parse()
    return configparse.blade_config


def setup_build_dir(options, config):
    build_path_format = config.configs['global_config']['build_path_template']
    s = Template(build_path_format)
    build_dir = s.substitute(m=options.m, profile=options.profile)
    if not os.path.exists(build_dir):
        os.mkdir(build_dir)
    return build_dir


def setup_dirs(options):
    # Set blade_root_dir to the directory which contains the
    # file BLADE_ROOT, is upper than and is closest to the current
    # directory.  Set working_dir to current directory.
    working_dir = get_cwd()
    blade_root_dir = find_blade_root_dir(working_dir)
    if blade_root_dir != working_dir:
        # This message is required by vim quickfix mode if pwd is changed during
        # the building, DO NOT change the pattern of this message.
        print >>sys.stderr, "Blade: Entering directory `%s'" % blade_root_dir
        os.chdir(blade_root_dir)
    config = load_config(options, blade_root_dir)
    build_dir = setup_build_dir(options, config)
    return working_dir, blade_root_dir, build_dir


def setup_log(build_dir):
    log_file = os.path.join(build_dir, 'blade.log')
    console.set_log_file(log_file)


def clear_build_script():
    for script in ('SConstruct', 'build.ninja'):
        script = os.path.join(_BLADE_ROOT_DIR, script)
        try:
            os.remove(script)
        except OSError:
            pass


def run_subcommand(command, options, targets, blade_path, build_dir):
    if command == 'query' and getattr(options, 'depended', None):
        targets = ['...']
    blade.blade = Blade(targets,
                        blade_path,
                        _WORKING_DIR,
                        build_dir,
                        _BLADE_ROOT_DIR,
                        options,
                        command)

    # Build the targets
    blade.blade.load_targets()
    if options.stop_after == 'load':
        return 0
    blade.blade.analyze_targets()
    if options.stop_after == 'analyze':
        return 0
    blade.blade.generate()
    if options.stop_after == 'generate':
        return 0

    # Switch case due to different sub command
    action = {
             'build': build,
             'run': run,
             'test': test,
             'clean': clean,
             'query': query
             }[command]
    try:
        returncode = action(options)
    finally:
        clear_build_script()

    return returncode


def _main(blade_path):
    """The main entry of blade. """

    command, options, targets = parse_command_line()
    print targets
    global _TARGETS
    _TARGETS = list(targets)

    global _BLADE_ROOT_DIR
    global _WORKING_DIR
    _WORKING_DIR, _BLADE_ROOT_DIR, build_dir = setup_dirs(options)

    setup_log(build_dir)

    lock_file_fd = lock_workspace()
    try:
        if options.profiling:
            restats_file = os.path.join(build_dir, 'restats')
            pstats_file = os.path.join(build_dir, 'blade.pstats')
            cProfile.runctx("run_subcommand(command, options, targets, blade_path, build_dir)",
                            globals(), locals(), restats_file)
            p = pstats.Stats(restats_file)
            p.dump_stats(pstats_file)
            p.sort_stats('cumulative').print_stats(20)
            p.sort_stats('time').print_stats(20)
            console.info('Binary result file %s is also generated, '
                         'you can use gprof2dot or vprof to convert it to graph' % pstats_file)
            console.info('gprof2dot.py -f pstats --color-nodes-by-selftime %s'
                         ' | dot -T pdf -o blade.pdf' % pstats_file)
        else:
            run_subcommand(command, options, targets, blade_path, build_dir)
    finally:
        unlock_workspace(lock_file_fd)


def main(blade_path):
    exit_code = 0
    try:
        start_time = time.time()
        exit_code = _main(blade_path)
        cost_time = int(time.time() - start_time)
        if exit_code == 0:
            console.info('success')
        console.info('cost time is %ss' % datetime.timedelta(seconds=cost_time))
    except SystemExit, e:
        exit_code = e.code
    except KeyboardInterrupt:
        console.error_exit('keyboard interrupted', -signal.SIGINT)
    except:
        console.error_exit(traceback.format_exc())
    sys.exit(exit_code)
