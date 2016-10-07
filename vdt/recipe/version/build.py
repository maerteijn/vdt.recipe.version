import argparse
import ConfigParser
import logging
import logging.handlers
import os
import platform
import subprocess
import multiprocessing
import sys

from glob import glob
from vdt.version.main import parse_args, run


def check_platform(target_extension):
    if target_extension == "*.deb":
        current_platform = platform.dist()[0].lower()
        return current_platform in ["ubuntu", "debian"]
    return True


def create_target_directory(directory):
    if not os.path.isdir(directory):
        os.mkdir(directory)


def get_config():
    path = os.getcwd()
    config = ConfigParser.ConfigParser(defaults={'build-directory': None,
                                                 'post-command': None,
                                                 'version-extra-args': None})
    config.readfp(open('%s/.vdt.recipe.version.cfg' % path))
    return config


def get_build_sources(sources_directory, sources_to_build):
    for src in os.listdir(sources_directory):
        if src in sources_to_build or '*' in sources_to_build:
            yield src


class BufferingHandler(logging.Handler):
    def __init__(self, target=None):
        logging.Handler.__init__(self)
        self.buffers = multiprocessing.Manager().dict()
        self.target = target
        self.lock = multiprocessing.RLock()
        self.pid = multiprocessing.current_process().pid

    def set_target(self, target):
        self.target = target

    def __append_to_buffer(self, pid, record):
        current_buffer = self.buffers[pid]
        current_buffer['buffer'].append(record)
        self.buffers[pid] = current_buffer

    def __clear_buffer(self, pid):
        current_buffer = self.buffers[pid]
        current_buffer['buffer'] = []
        self.buffers[pid] = current_buffer

    def emit(self, record):
        self.acquire()
        pid = multiprocessing.current_process().pid

        # create new buffer if process has not logged yet
        if pid not in self.buffers:
            self.buffers[pid] = {'parent_pid': os.getppid(), 'buffer': []}
        self.__append_to_buffer(pid, record)

        # all logs from main process should be printed directly
        if pid == self.pid:
            self.flush_by_pid(pid)

        self.release()

    def flush(self):
        self.acquire()
        try:
            if self.target:
                for pid, current_buffer in dict(self.buffers).iteritems():
                    for record in current_buffer['buffer']:
                        self.target.handle(record)
                    self.__clear_buffer(pid)
        finally:
            self.release()

    def flush_by_pid(self, pid):
        self.acquire()
        try:
            if self.target:
                for record in self.buffers[pid]['buffer']:
                    self.target.handle(record)
                self.__clear_buffer(pid)
        finally:
            self.release()

    def close(self):
        self.flush()
        self.acquire()
        try:
            self.target = None
            logging.Handler.close(self)
        finally:
            self.release()


class LogPrinter:
    '''LogPrinter class which serves to emulates a file object and logs
      whatever it gets sent to a Logger object at the INFO level.'''
    def __init__(self, logger):
        '''Grabs the specific logger to use for log printing.'''
        self.logger = logger

    def write(self, buf):
        '''Logs written output to a specific logger'''
        for line in buf.rstrip().splitlines():
            self.logger.info(line.rstrip())

    def fileno(self):
        pass

    def flush(self):
        for handler in self.logger.handlers:
            if isinstance(handler, BufferingHandler):
                handler.flush_by_pid(multiprocessing.current_process().pid)


def build(build_job):
    log_printer = LogPrinter(logging.getLogger())
    sys.stdout = log_printer
    sys.stderr = log_printer
    try:
        supported_platform = check_platform(build_job['target_extension'])

        if not supported_platform:
            logging.error("Cannot run version, your platform is not supported.")
            return False

        # create target directory for the builded packages
        create_target_directory(build_job['target_directory'])

        # add the buildout bin directory to the path
        os.environ['PATH'] = "%s:" % build_job['bin_directory'] + os.environ['PATH']

        # now build package
        cwd = os.path.join(build_job['sources_directory'], build_job['src'])

        logging.info("Running 'vdt.version' for %s" % build_job['src'])

        # collect all the arguments
        vdt_args = ["--plugin=%s" % build_job['version_plugin']]

        if build_job['version_extra_args']:
            for row in build_job['version_extra_args'].split("\n")[1:]:
                # subprocess.checkoutput wants each argument to be
                # separate, like ["ls", "-l" "-a"]
                vdt_args += row.split(" ")

        if build_job['cmd_extra_args']:
            # add optional command line arguments to version
            vdt_args += build_job['cmd_extra_args']

        args, extra_args = parse_args(vdt_args)
        logging.info("calling run with arguments %s from %s" % (" ".join(vdt_args), cwd))

        os.chdir(cwd)
        # TODO: "HEAD is now at..." message from Git is still printed directly to std.out
        exit_code = run(args, extra_args)

        if exit_code != 1:
            # sometimes packages are build in a separate directory
            # (fe wheels)
            if build_job['build_directory']:
                cwd = os.path.join(cwd, build_job['build_directory'])
            # move created files to target directory
            package_files = glob(os.path.join(cwd, build_job['target_extension']))
            if package_files:
                move_cmd = ["mv"] + package_files + [build_job['target_directory']]

                logging.info("Executing command %s" % move_cmd)
                logging.info(subprocess.check_output(move_cmd, cwd=cwd))

            if build_job['post_command']:
                logging.info("Executing command %s" % build_job['post_command'])
                logging.info(subprocess.check_output(build_job['post_command'], cwd=cwd, shell=True))
        else:
            return False
    except Exception as exc:
        # TODO: can't use logging.exception() because stacktrace can't be pickled
        logging.error(exc.message)
        return False
    finally:
        log_printer.flush()


def generate_build_jobs(sections, config, extra_args):
    build_jobs = []
    for section in sections:
        build_sources = get_build_sources(config.get(section, 'sources-directory'),
                                          config.get(section, 'sources-to-build').split('\n'))
        for src in build_sources:
            build_job = {'src': src,
                         'build_directory': config.get(section, 'build-directory'),
                         'version_extra_args': config.get(section, 'version-extra-args'),
                         'post_command': config.get(section, 'post-command'),
                         'version_plugin': config.get(section, 'version-plugin'),
                         'bin_directory': config.get(section, 'bin-directory'),
                         'sources_directory': config.get(section, 'sources-directory'),
                         'target_extension': config.get(section, 'target-extension'),
                         'target_directory': config.get(section, 'target-directory'),
                         'cmd_extra_args': extra_args}
            build_jobs.append(build_job)
    return build_jobs


def get_main_logger(formatter, log_level):
    main_logger = logging.getLogger('main')
    main_logger.propagate = False
    main_logger.setLevel(log_level)
    main_handler = logging.StreamHandler(sys.stdout)
    main_handler.setFormatter(formatter)
    main_logger.addHandler(main_handler)
    return main_logger


def get_buffered_logger(formatter, log_level, target):
    buffered_logger = logging.getLogger()
    buffered_logger.setLevel(log_level)
    buffering_handler = BufferingHandler(target=target)
    buffering_handler.setFormatter(formatter)
    buffered_logger.addHandler(buffering_handler)
    return buffered_logger


def main():
    p = argparse.ArgumentParser(description="Buildout recipe command to run vdt.version")
    p.add_argument(
        "-v", "--verbose", default=False,
        dest="verbose", action="store_true", help="more output")
    p.add_argument(
        "--section", dest="section", action="store",
        help="Only build a specific section")
    p.add_argument(
        "--parallel", dest="parallel", type=int, default=1,
        help="Number of packages you want to build in parallel")

    args, extra_args = p.parse_known_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    formatter = logging.Formatter('%(levelname)s:%(processName)s:%(name)s: %(message)s')
    main_logger = get_main_logger(formatter, log_level)
    buffered_logger = get_buffered_logger(formatter, log_level, main_logger)

    config = get_config()
    sections = config.sections()
    buffered_logger.info("Spawning worker pool with %s processes" % args.parallel)
    pool = multiprocessing.Pool(args.parallel)

    if args.section:
        sections = [args.section]

    build_jobs = generate_build_jobs(sections, config, extra_args)
    return_values = pool.map(build, build_jobs)
    pool.close()
    pool.join()

    if False in return_values:
        return 1


