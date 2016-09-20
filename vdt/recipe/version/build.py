import argparse
import ConfigParser
import logging
import logging.handlers
import os
import platform
import subprocess

from glob import glob
from multiprocessing import Pool
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


def build(build_job):
    supported_plaform = check_platform(build_job['target_extension'])

    if not supported_plaform:
        logging.error("Cannot run version, your platform is not supported.")
        return False

    # create target directory for the built packages
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
    try:
        exit_code = run(args, extra_args)
    except Exception as exc:
        logging.exception(exc.message)
        return False

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


def create_build_jobs(sections, config, extra_args):
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


def main():
    p = argparse.ArgumentParser(
        description="Buildout recipe command to run vdt.version")
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
    logging.basicConfig(level=log_level)

    config = get_config()
    sections = config.sections()
    logging.info("Spawning worker pool with %s processes" % args.parallel)
    pool = Pool(args.parallel)

    if args.section:
        sections = [args.section]

    build_jobs = create_build_jobs(sections, config, extra_args)

    results = pool.map(build, build_jobs)
    pool.close()
    pool.join()

    if False in results:
        return 1


