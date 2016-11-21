#!/usr/bin/env python
#
# Copyright (C) 2016 Olzhas Rakhimov
# Copyright (C) 2010, 2014 Zhichang Yu
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""C/C++ dependency analyzer.

Physical dependency analyzer
for components/packages/package groups of a large C/C++ project.
"""

from __future__ import print_function, division, absolute_import

import argparse as ap
import itertools
import os.path
import re
import sys
from xml.etree import ElementTree

import graph


VERSION = '0.0.7'  # The latest release version.

# Allowed common abbreviations in the code:
# ccd   - Cumulative Component Dependency (CCD)
# nccd  - Normalized CCD
# accd  - Average CCD
# cd    - component dependency (discouraged abbreviation!)
# pkg   - package (discouraged abbreviation!)
# hfile - header file
# cfile - implementation file
# dep   - dependency (discouraged abbreviation!)


class XmlError(Exception):
    """Parsing errors in XML configuration file."""

    pass


class InvalidArgumentError(Exception):
    """General errors with invalid arguments."""

    pass


_RE_HFILE = re.compile(r'(?i).*\.h(xx|\+\+|h|pp|)$')
_RE_CFILE = re.compile(r'(?i).*\.c(xx|\+\+|c|pp|)$')


def warn(*args, **kwargs):
    """Prints a warning message into the standard error."""
    print(*args, file=sys.stderr, **kwargs)


def strip_ext(filename):
    """Strips the extension from a filename."""
    return os.path.splitext(filename)[0]


def common_path(paths):
    """Returns common prefix path for the argument absolute normalized paths."""
    path = os.path.commonprefix(paths)
    assert os.path.isabs(path)
    if path[-1] == os.path.sep:
        return path[:-1]
    sep_pos = len(path)
    if all(len(x) == sep_pos or x[sep_pos] == os.path.sep for x in paths):
        return path
    return os.path.dirname(path)


def find(path, fnmatcher):
    """Finds files with their names matching a regex pattern.

    Args:
        path: The root path to start the search.
        fnmatcher: regex for filename.

    Yields:
        The basename and full path to the matching files.
    """
    if os.path.isfile(path):
        filename = os.path.basename(path)
        if fnmatcher.match(filename):
            yield filename, path
    else:
        for root, _, files in os.walk(path):
            for entry in files:
                if fnmatcher.match(entry):
                    full_path = os.path.join(root, entry)
                    yield entry, full_path


def find_hfiles(path, hbases, hfiles):
    """Finds package header files.

    Args:
        path: The root path to start the search.
        hbases: The destination container for header file basenames.
        hfiles: The destination container for header file paths.
    """
    for hfile, hpath in find(path, _RE_HFILE):
        if hfile not in hfiles:
            hfiles[hfile] = hpath
        hbase = strip_ext(hfile)
        hbases[hbase] = hpath


def find_cfiles(path, cbases):
    """Finds package implement files.

    Args:
        path: The root path to start the search.
        cbases: The destination container for implementation file basenames.
    """
    for cfile, cpath in find(path, _RE_CFILE):
        cbase = strip_ext(cfile)
        assert cbase not in cbases
        cbases[cbase] = cpath


class Include(object):
    """Representation of an include directive.

    Attributes:
        with_quotes: True if the include is within quotes ("")
            instead of angle brackets (<>).
        hfile: The header file in the directive.
        hpath: The absolute path to the header file.
    """

    _RE_INCLUDE = re.compile(r'^\s*#include\s*'
                             '(<(?P<brackets>.+)>|"(?P<quotes>.+)")')

    __slots__ = ['hfile', 'with_quotes', 'hpath']

    def __init__(self, hfile, with_quotes):
        """Initializes with attributes."""
        self.hfile = hfile
        self.with_quotes = with_quotes
        self.hpath = None

    def __str__(self):
        """Produces the original include with quotes or brackets."""
        if self.with_quotes:
            return '"%s"' % self.hfile
        return '<%s>' % self.hfile

    @staticmethod
    def grep(file_path):
        """Processes include directives in a source file.

        Args:
            file_path: The full path to the source file.

        Yields:
            Include objects constructed with the directives.
        """
        with open(file_path) as src_file:
            for line in src_file:
                include = Include._RE_INCLUDE.search(line)
                if not include:
                    continue
                if include.group("brackets"):
                    yield Include(include.group("brackets"), False)
                else:
                    yield Include(include.group("quotes"), True)

    def locate(self, cwd, include_dirs):
        """Locates the included header file path.

        All input directory paths must be absolute.
        If the file is not located,
        only a warning message is produced.

        Args:
            cwd: The working directory for source file processing.
            include_dirs: The directories to search for the file.
        """
        assert self.hpath is None

        def _find_in(include_dir):
            """Returns True if the path is found."""
            file_hpath = os.path.join(include_dir, self.hfile)
            if os.path.isfile(file_hpath):
                self.hpath = file_hpath
                return True
            return False

        if self.with_quotes and _find_in(cwd):
            return
        for include_dir in include_dirs:
            if _find_in(include_dir):
                return
        warn('warning: include issues: header not found: %s' % self.hfile)


class Component(object):
    """Representation of a component in a package.

    Attributes:
        name: A unique name within the package.
        hpath: The absolute path to the header file.
        hfile: The basename of the header file.
        cpath: The absolute path to the implementation file.
        package: The package this components belongs to.
        dep_internal_components: Internal dependency components.
        dep_external_components: External dependency components.
        includes_in_h: Include directives in the header file.
        includes_in_c: Include directives in the implementation file.
    """

    def __init__(self, hpath, cpath, package):
        """Initialization of a free-standing component.

        Registers the component in the package upon initialization.
        Warns about incomplete components.

        Args:
            hpath: The path to the header file of the component.
            cpath: The path to the implementation file of the component.
            package: The package this components belongs to.
        """
        assert hpath or cpath
        self.name = strip_ext((hpath or cpath)[(len(package.root) + 1):])
        if not hpath:
            warn('warning: incomplete component: missing header: %s in %s.%s' %
                 (self.name, package.name, package.group.name))
        self.hpath = hpath
        self.hfile = None if not hpath else os.path.basename(hpath)
        self.cpath = cpath
        self.package = package
        self.dep_internal_components = set()
        self.dep_external_components = set()
        self.includes_in_h = [] if not hpath else list(Include.grep(hpath))
        self.includes_in_c = [] if not cpath else list(Include.grep(cpath))
        package.components.append(self)

    def __str__(self):
        """For printing graph nodes."""
        return self.name

    def dependencies(self):
        """Yeilds dependency components within the same package."""
        for dep_component in self.dep_internal_components:
            if dep_component.package == self.package:
                yield dep_component

    def check_include_issues(self):
        """Checks for issues with header inclusion in implementation files."""
        if not self.hpath or not self.cpath:
            return  # Header-only or incomplete components.
        if self.hfile not in (os.path.basename(x.hfile)
                              for x in self.includes_in_c):
            warn('warning: include issues: missing include: '
                 '%s does not include %s.' % (self.cpath, self.hfile))
        elif self.hfile != os.path.basename(self.includes_in_c[0].hfile):
            warn('warning: include issues: include order: '
                 '%s should be the first include in %s.' %
                 (self.hfile, self.cpath))

    @property
    def dep_external_packages(self):
        """Yields external dependency group.packages."""
        for group, package in set((x.package.group.name, x.package.name)
                                  for x in self.dep_external_components):
            yield '.'.join((group, package))


class ExternalComponent(object):
    """Representation of an external component.

    Note that external components are degenerate.
    There's no need to acquire full information about their dependencies.

    Attributes:
        hpath: Full path to the component header as an identifier.
        package: The package.
    """

    __slots__ = ['hpath', 'package']

    def __init__(self, hpath, package):
        """Constructs an external component with its attributes."""
        self.hpath = hpath
        self.package = package


class Package(object):
    """A collection of components.

    Attributes:
        name: The unique identifier name of the package within its group.
        paths: The absolute directory paths in the package.
        group: The package group this package belongs to.
        root: The common root path for all the paths in the package.
        components: The list of unique components in this package.
    """

    def __init__(self, paths, group, name=None):
        """Constructs an empty package.

        Registers the package in the package group.

        Args:
            paths: The directory paths relative to the package group directory.
            group: The package group.
            name: A unique identifier within the package group.
                If not provided, only one path is accepted
                for the identifier deduction.

        Raises:
            InvalidArgumentError: Issues with the argument directory paths.
        """
        self.paths = set()
        path = None  # Relative and normalized path to the group root path.
        for path in paths:
            path = os.path.normpath(path)
            abs_path = os.path.join(group.path, path)
            if not os.path.isdir(abs_path):
                raise InvalidArgumentError(
                    '%s is not a directory in %s (group %s).' %
                    (path, group.path, group.name))
            if abs_path in self.paths:
                assert name
                raise InvalidArgumentError('%s is duplicated in %s.%s' %
                                           (abs_path, name, group.name))
            self.paths.add(abs_path)
        assert self.paths, "No package directory paths are provided."
        assert name or len(self.paths) == 1, "The package name is undefined."
        self.name = name or '_'.join(x for x in path.split(os.path.sep) if x)
        self.group = group
        self.root = common_path(self.paths)
        self.components = []
        group.add_package(self)

    def __str__(self):
        """For printing graph nodes."""
        return self.name

    def dependencies(self):
        """Yields dependency packages within the same package group."""
        for component in self.components:
            for dep_component in component.dep_internal_components:
                if (dep_component.package.group == self.group and
                        dep_component.package != self):
                    yield dep_component.package


class PackageGroup(object):
    """A collection of packages.

    Attributes:
        name: The unique name of the package group.
        path: The absolute path to the group directory.
        packages: {package_name: package} belonging to this group.
    """

    def __init__(self, name, path):
        """Constructs an empty group.

        Args:
            name: A unique global identifier.
            path: The directory path to the group.

        Raises:
            InvalidArgumentError: The path is not a directory.
        """
        if not os.path.isdir(path):
            raise InvalidArgumentError('%s is not a directory.' % path)
        self.name = name
        self.path = os.path.abspath(os.path.normpath(path))
        self.packages = {}

    def __str__(self):
        """For printing graph nodes."""
        return self.name

    def dependencies(self):
        """Yields dependency package groups."""
        for package in self.packages.values():
            for component in package.components:
                for dep_component in component.dep_internal_components:
                    if dep_component.package.group != self:
                        yield dep_component.package.group

    def add_package(self, package):
        """Adds a package into the group.

        This function is automatically called in the package constructor.

        Args:
            package: The constructed package.

        Raises:
            InvalidArgumentError: Duplicate package.
        """
        if package.name in self.packages:
            raise InvalidArgumentError(
                '%s is a duplicate package in %s group.' %
                (package.name, self.name))
        self.packages[package.name] = package


class DependencyAnalysis(object):
    """Analysis of dependencies with package groups/packages/components.

    Attributes:
        config_file: The path to the configuration file.
        components: {base_name: Component}
        external_components: {hpath: ExternalComponent}
        internal_components: {hpath: Component}
        internal_hfiles: {hfile: hpath}
        external_groups: External dependency packages and package groups.
              {group_name: PackageGroup}
        internal_groups: The package groups of the project under analysis.
              {group_name: PackageGroup}
    """

    def __init__(self, config_file):
        """Initializes analysis containers.

        Args:
            config_file: The path to the configuration file.

        Raises:
            XmlError: The XML is malformed or invalid.
            InvalidArgumentError: The configuration has is invalid values.
        """
        self.config_file = config_file
        self.components = {}
        self.external_components = {}
        self.internal_components = {}
        self.internal_hfiles = {}
        self.__internal_hfile_deps = {}
        self.external_groups = {}
        self.internal_groups = {}
        self.__parse_xml_config(config_file)

    def __parse_xml_config(self, config_file):
        """Parses the XML configuration file.

        Args:
            config_file: The path to the configuration file.

        Raises:
            XmlError: The XML is malformed or invalid.
            InvalidArgumentError: The configuration has is invalid values.
        """
        root = ElementTree.parse(config_file).getroot()
        for pkg_group_element in root.findall('package-group'):
            pkg_role = pkg_group_element.get('role')
            assert pkg_role is None or pkg_role in ('external', 'internal')
            pkg_groups = self.external_groups if pkg_role == 'external' \
                else self.internal_groups
            DependencyAnalysis.__add_package_group(pkg_group_element,
                                                   pkg_groups)

    @staticmethod
    def __add_package_group(pkg_group_element, pkg_groups):
        """Parses the body of <package-group/> in XML config file.

        Args:
            pkg_group_element: The <package-group> XML element.
            pkg_groups: The destination dictionary for member packages.

        Raises:
            InvalidArgumentError: Invalid configuration.
        """
        group_name = pkg_group_element.get('name')
        group_path = pkg_group_element.get('path')
        if group_name in pkg_groups:
            raise InvalidArgumentError('Redefinition of %s group' % group_name)

        package_group = PackageGroup(group_name, group_path)

        for pkg_element in pkg_group_element.findall('package'):
            Package((x.text.strip() for x in pkg_element.findall('path')),
                    package_group,
                    pkg_element.get('name'))

        for pkg_element in pkg_group_element.findall('path'):
            Package([pkg_element.text.strip()], package_group)

        pkg_groups[group_name] = package_group

    def locate(self, include, component):
        """Locates the dependency component.

        The current approach is naive.
        It searches for a file by its basename
        as if every subdirectory were an include path.

        Args:
            include: The include object representing the directive.
            component: The dependent component.

        Returns:
            True if the include is found.
        """
        hfile = os.path.basename(include.hfile)
        if hfile == component.hfile:
            include.hpath = component.hpath
            return True

        if hfile in self.internal_components:
            dep_component = self.internal_components[hfile]
            include.hpath = dep_component.hpath
            component.dep_internal_components.add(dep_component)
            return True

        if hfile in self.external_components:
            dep_component = self.external_components[hfile]
            include.hpath = dep_component.hpath
            component.dep_external_components.add(dep_component)
            return True

        def _find_in(dir_path):
            for root, _, files in os.walk(dir_path):
                if hfile in files:
                    include.hpath = os.path.join(root, hfile)
                    return True
            return False

        def _find_external_component():
            for group in self.external_groups.values():
                for package in group.packages.values():
                    for dir_path in package.paths:
                        if _find_in(dir_path):
                            return ExternalComponent(hfile, package)
            return None

        dep_component = _find_external_component()
        if dep_component:
            self.external_components[hfile] = dep_component
            component.dep_external_components.add(dep_component)
            return True
        return False

    def make_components(self):
        """Pairs hfiles and cfiles."""
        for group in self.internal_groups.values():
            for package in group.packages.values():
                hbases = {}
                cbases = {}
                hfiles = {}
                for src_path in package.paths:
                    find_hfiles(src_path, hbases, hfiles)
                    find_cfiles(src_path, cbases)

                for hfile, hpath in hfiles.items():
                    if hfile not in self.internal_hfiles:
                        self.internal_hfiles[hfile] = hpath

                self.__construct_components(package, hbases, cbases)

    def __construct_components(self, package, hbases, cbases):
        """Pairs header and implementation files into components.

        Even though John Lakos defined a component as a pair of h and c files,
        C++ can have template only components
        residing only in header files (e.g., STL/Boost/etc.).
        Moreover, some header-only components
        may contain only inline functions or macros
        without any need for an implmentation file
        (e.g., inline math, Boost PPL).
        For these reasons, unpaired header files
        are counted as components by default.

        Args:
            package: The host package.
            hbases: Base names of header files.
            cbases: Base names of implementation files.
                    The paired base names will be removed from this container.
        """
        for key, hpath in hbases.items():
            cpath = None
            if key in cbases:
                cpath = cbases[key]
                del cbases[key]
            assert key not in self.components
            component = Component(hpath, cpath, package)
            self.components[key] = component
            self.internal_components[component.hfile] = component

        for key, cpath in cbases.items():
            assert key not in self.components
            self.components[key] = Component(None, cpath, package)

    def analyze(self):
        """Runs the analysis."""
        for component in self.components.values():
            for include in itertools.chain(component.includes_in_h,
                                           component.includes_in_c):
                if not self.locate(include, component):
                    warn('warning: include issues: header not found: %s' %
                         str(include))
            component.check_include_issues()

    def print_ldep(self):
        """Prints link time dependencies of components."""
        def _print_deps(deps):
            for name in sorted(deps):
                print('\t%s' % name)

        for group_name in sorted(self.internal_groups.keys()):
            packages = self.internal_groups[group_name].packages
            for pkg_name in sorted(packages.keys()):
                print('=' * 80)
                print('package %s.%s dependency:' % (group_name, pkg_name))
                for component in packages[pkg_name].components:
                    print('%s:' % component.name)
                    _print_deps(x.name for x
                                in component.dep_internal_components)
                    print('  (external)')
                    _print_deps(component.dep_external_packages)

    def make_graph(self):
        """Reports analysis results and graphs."""
        def _analyze(suffix, arg_components):
            digraph = graph.Graph(arg_components)
            digraph.analyze()
            digraph.print_cycles()
            digraph.print_levels()
            digraph.print_summary()
            digraph.write_dot(suffix)

        if len(self.internal_groups) > 1:
            print('\n' + '#' * 80)
            print('analyzing dependencies among all package groups ...')
            _analyze('system', self.internal_groups.values())

        for group_name, package_group in self.internal_groups.items():
            if len(package_group.packages) > 1:
                print('\n' + '#' * 80)
                print('analyzing dependencies among packages in ' +
                      'the specified package group %s ...' % group_name)
                _analyze(group_name, package_group.packages.values())

        for group_name, package_group in self.internal_groups.items():
            for pkg_name, package in package_group.packages.items():
                print('\n' + '#' * 80)
                print('analyzing dependencies among components in ' +
                      'the specified package %s.%s ...' %
                      (group_name, pkg_name))
                _analyze('_'.join((group_name, pkg_name)), package.components)


def main():
    """Runs the dependency analysis and prints results and graphs.

    Raises:
        IOError: filesystem operations failed.
        XmlError: XML configuration validity issues.
        InvalidArgumentError: The configuration has is invalid values.
    """
    parser = ap.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='store_true', default=False,
                        help='show the version information and exit')

    parser.add_argument('-c', '--config', default='cppdep.xml',
                        help="""an XML file which describes
                        the source code structure of a C/C++ project""")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return
    analysis = DependencyAnalysis(args.config)
    analysis.make_components()
    analysis.analyze()
    analysis.print_ldep()
    analysis.make_graph()


if __name__ == '__main__':
    try:
        main()
    except IOError as err:
        warn('IO Error:\n' + str(err))
        sys.exit(1)
    except XmlError as err:
        warn('Configuration XML Error:\n' + str(err))
        sys.exit(1)
    except InvalidArgumentError as err:
        warn('Invalid Argument Error:\n' + str(err))
        sys.exit(1)
