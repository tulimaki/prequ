import logging
import operator
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote  # noqa

from functools import partial

from pip.backwardcompat import ConfigParser
from pip.download import _download_url, _get_response_from_url
from pip.index import PackageFinder
from pip.locations import default_config_file
from pip.req import InstallRequirement
from pip.util import splitext

from .datastructures import Spec
from .version import NormalizedVersion  # PEP386 compatible version numbers


class NoPackageMatch(Exception):
    pass


class BasePackageManager(object):
    def find_best_match(self, spec):
        raise NotImplementedError('Implement this in a subclass.')

    def get_dependencies(self, name, version):
        raise NotImplementedError('Implement this in a subclass.')


class FakePackageManager(BasePackageManager):
    def __init__(self, fake_contents):
        """Creates a fake package manager index, for easy testing.  The
        fake_contents argument is a dictionary containing 'name-version' keys
        and lists-of-specs values.

        Example:

            {
                'foo-0.1': ['bar', 'qux'],
                'bar-0.2': ['qux>0.1'],
                'qux-0.1': [],
                'qux-0.2': [],
            }
        """
        # Sanity check (parsing will return errors if content is wrongly
        # formatted)
        for pkg_key, list_of_specs in fake_contents.items():
            try:
                _, _ = self.parse_package_key(pkg_key)
            except ValueError:
                raise ValueError('Invalid index entry: %s' % (pkg_key,))
            assert isinstance(list_of_specs, list)

        self._contents = fake_contents

    def parse_package_key(self, pkg_key):
        try:
            return pkg_key.rsplit('-', 1)
        except ValueError:
            raise ValueError('Invalid package key: %s (required format: "name-version")' % (pkg_key,))

    def iter_package_versions(self):
        """Iters over all package versions, returning key-value pairs."""
        for key in self._contents:
            yield self.parse_package_key(key)

    def iter_versions(self, given_name):
        """Will return all versions available for the current package name."""
        for name, version in self.iter_package_versions():
            if name == given_name:
                yield version

    def matches_qual(self, version, qual, value):
        """Returns whether version matches the qualifier and the given value."""
        ops = {
            '==': operator.eq,
            '<': operator.lt,
            '>': operator.gt,
            '<=': operator.le,
            '>=': operator.ge,
        }
        return ops[qual](NormalizedVersion(version), NormalizedVersion(value))

    def pick_highest(self, list_of_versions):
        """Picks the highest version from a list, according to PEP386 logic."""
        return str(max(map(NormalizedVersion, list_of_versions)))

    def find_best_match(self, spec):
        """This requires a bit of reverse engineering of PyPI's logic that
        finds a pacakge for a given spec, but it's not too hard.
        """
        versions = list(self.iter_versions(spec.name))
        for qual, value in spec.preds:
            pred = partial(self.matches_qual, qual=qual, value=value)
            versions = filter(pred, versions)
        if len(versions) == 0:
            raise NoPackageMatch('No package found for %s' % (spec,))
        return self.pick_highest(versions)

    def get_dependencies(self, name, version):
        pkg_key = '%s-%s' % (name, version)
        specs = []
        for specline in self._contents[pkg_key]:
            specs.append(Spec.from_line(specline))
        return specs


class PackageManager(BasePackageManager):
    """The default package manager that goes to PyPI and caches locally."""
    cache_path = os.path.join(os.path.expanduser('~'), '.pip-tools', 'cache')

    def __init__(self):
        # TODO: provide options for pip, such as index URL or use-mirrors
        if not os.path.exists(self.cache_path):
            os.makedirs(self.cache_path)
        self._link_cache = {}
        self._dependency_cache = {}

    def find_best_match(self, spec):
        # TODO: if the spec is pinned, we might be able to go straight to the
        # local cache without having to use the PackageFinder. Cached file
        # names look like this:
        # http%3A%2F%2Fpypi.python.org%2Fpackages%2Fsource%2Fs%2Fsix%2Fsix-1.2.0.tar.gz
        # This is easy to guess from a package==version spec but requires the
        # package to be actually hosted on pypi, which is not the case for
        # everything (e.g. redis).
        #
        # Option 1: make this work for packages hosted on PyPI and accept
        # external packages to be slower.
        #
        # Option 2: only use the last part of the URL as a file name
        # (six-1.2.0.tar.gz). This makes it easy to check the local cache for
        # any pinned spec but *might* lead to inconsistencies for people
        # maintaining their own PyPI servers and adding their modified
        # packages as the same names/versions as the originals on the
        # canonical PyPI. The shouldn't do it, and this is probably an edge
        # case but it's still worth making a decision.
        if str(spec) not in self._link_cache:
            requirement = InstallRequirement.from_line(str(spec))
            finder = PackageFinder(
                find_links=[],
                index_urls=['http://pypi.python.org/simple/'],
                use_mirrors=True,
                mirrors=[],
            )
            self._link_cache[str(spec)] = finder.find_requirement(requirement,
                                                                  False)
        link = self._link_cache[str(spec)]
        package, version = splitext(link.filename)[0].rsplit('-', 1)
        return version

    def get_local_package_path(self, url):
        """Returns the full local path name for a given URL.  This
        does not require the package to exist locally.  In fact, this
        can be used to calculate the destination path for a download.
        """
        cache_key = quote(url, '')
        fullpath = os.path.join(self.cache_path, cache_key)
        return fullpath

    def get_package_location(self, spec):
        self.find_best_match(spec)
        link = self._link_cache[str(spec)]
        fullpath = self.get_local_package_path(link.url_without_fragment)

        if os.path.exists(fullpath):
            logging.debug('Archive cache hit: {0}'.format(link.filename))
            return fullpath

        logging.debug('Archive cache miss, downloading {0}...'.format(
            link.filename
        ))
        return self.download_packge(link)

    def get_pip_cache_root():
        """Returns pip's cache root, or None if no such cache root is
        configured."""
        pip_config = ConfigParser.RawConfigParser()
        pip_config.read([default_config_file])
        download_cache = None
        try:
            for key, value in pip_config.items('global'):
                if key == 'download-cache':
                    download_cache = value
                    break
        except ConfigParser.NoSectionError:
            pass
        if download_cache is not None:
            download_cache = os.path.expanduser(download_cache)
        return download_cache

    def download_package(self, link):
        """Downloads the given package link contents to the local
        package cache. Overwrites anything that's in the cache already.
        """
        # TODO integrate pip's download-cache
        #pip_cache_root = self.get_pip_cache_root()
        #if pip_cache_root:
        #    cache_path = os.path.join(pip_cache_root, cache_key)
        #    if os.path.exists(cache_path):
        #        # pip has a cached version, copy it
        #        shutil.copyfile(cache_path, fullpath)
        #else:
        #    actually download the requirement
        fullpath = self.get_local_package_path(link.url_fragment)
        response = _get_response_from_url(link.url_fragment, link)
        _download_url(response, link, fullpath)
        return fullpath

    def get_dependencies(self, name, version):
        spec = Spec.from_line('{0}=={1}'.format(name, version))
        specs = []
        self.get_all_dependencies(spec, specs, source=name)
        return specs

    def get_all_dependencies(self, spec, specs, source):
        logging.info("Looking up dependencies for {0} (from {1})".format(
            spec, source
        ))

        path = self.get_package_location(str(spec))
        deps = self.extract_dependencies(path)
        if not deps:
            return

        for dep in deps:
            dependency = Spec.from_line(dep, source=source)
            specs.append(dependency)
            self.get_all_dependencies(dependency, specs, source=str(spec))

    def unpack_archive(self, path, target_directory):
        if (path.endswith('.tar.gz') or
            path.endswith('.tar') or
            path.endswith('.tar.bz2') or
            path.endswith('.tgz')):

            archive = tarfile.open(path)
        elif path.endswith('.zip'):
            archive = zipfile.ZipFile(path)
        else:
            assert False, "Unsupported archive file: {}".format(path)

        archive.extractall(target_directory)
        archive.close()

    def has_egg_info(self, dist_dir):
        try:
            subprocess.check_call([sys.executable, 'setup.py', 'egg_info'],
                                  cwd=dist_dir, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            logging.debug("egg_info failed for {0}".format(
                dist_dir.rsplit('/', 1)[-1]
            ))
            return False
        return True

    def extract_dependencies(self, path):
        """Returns a list of dependencies for a given distribution."""
        if not path in self._dependency_cache:
            build_dir = tempfile.mkdtemp()
            unpack_dir = os.path.join(build_dir, 'build')
            self.unpack_archive(path, unpack_dir)
            name = os.listdir(unpack_dir)[0]
            dist_dir = os.path.join(unpack_dir, name)
            name, version = name.rsplit('-', 1)
            if not self.has_egg_info(dist_dir):
                shutil.rmtree(build_dir)
                self._dependency_cache[path] = None
                return

            egg_info_dir = '{0}.egg-info'.format(name.replace('-', '_'))
            for dirpath, dirnames, filenames in os.walk(dist_dir):
                if egg_info_dir in dirnames:
                    requires = os.path.join(dirpath, egg_info_dir,
                                            'requires.txt')
                    if os.path.exists(requires):
                        break
            else:  # requires.txt not found
                shutil.rmtree(build_dir)
                self._dependency_cache[path] = None
                return

            deps = []
            with open(requires, 'r') as requirements:
                for requirement in requirements.readlines():
                    dep = requirement.strip()
                    if dep == '[test]' or not dep:
                        break
                    deps.append(dep)
            shutil.rmtree(build_dir)
            self._dependency_cache[path] = deps
        return self._dependency_cache[path]

if __name__ == '__main__':
    pass