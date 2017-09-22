# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The piptool module imports pip requirements into Bazel rules."""

import argparse
import json
import os
import pkgutil
import pkg_resources
import re
import sys
import tempfile
import zipfile

# Make sure we're using a suitable version of pip as a library.
# Fallback on using it as a CLI.
from pip._vendor import requests

from pip import main as _pip_main
def pip_main(argv):
  # Extract the certificates from the PAR following the example of get-pip.py
  # https://github.com/pypa/get-pip/blob/430ba37776ae2ad89/template.py#L164-L168
  cert_path = os.path.join(tempfile.mkdtemp(), "cacert.pem")
  with open(cert_path, "wb") as cert:
    cert.write(pkgutil.get_data("pip._vendor.requests", "cacert.pem"))
  # PIP erroneously emits an error when bundled as a PAR file.  We
  # disable the version check to silence it.
  argv = ["--disable-pip-version-check", "--cert", cert_path] + argv
  return _pip_main(argv)

from rules_python.whl import Wheel

parser = argparse.ArgumentParser(
    description='Import Python dependencies into Bazel.')

parser.add_argument('--name', action='store',
                    help=('The namespace of the import.'))

parser.add_argument('--input', action='store',
                    help=('The requirements.txt file to import.'))

parser.add_argument('--output', action='store',
                    help=('The requirements.bzl file to export.'))

parser.add_argument('--directory', action='store',
                    help=('The directory into which to put .whl files.'))

def determine_possible_extras(whls):
  """Determines the list of possible "extras" for each .whl

  The possibility of an extra is determined by looking at its
  additional requirements, and determinine whether they are
  satisfied by the complete list of available wheels.

  Args:
    whls: a list of Wheel objects

  Returns:
    a dict that is keyed by the Wheel objects in whls, and whose
    values are lists of possible extras.
  """
  whl_map = {
    whl.distribution(): whl
    for whl in whls
  }

  # TODO(mattmoor): Consider memoizing if this recursion ever becomes
  # expensive enough to warrant it.
  def is_possible(distro, extra):
    distro = distro.replace("-", "_")
    # If we don't have the .whl at all, then this isn't possible.
    if distro not in whl_map:
      return False
    whl = whl_map[distro]
    # If we have the .whl, and we don't need anything extra then
    # we can satisfy this dependency.
    if not extra:
      return True
    # If we do need something extra, then check the extra's
    # dependencies to make sure they are fully satisfied.
    for extra_dep in whl.dependencies(extra=extra):
      req = pkg_resources.Requirement.parse(extra_dep)
      # Check that the dep and any extras are all possible.
      if not is_possible(req.project_name, None):
        return False
      for e in req.extras:
        if not is_possible(req.project_name, e):
          return False
    # If all of the dependencies of the extra are satisfiable then
    # it is possible to construct this dependency.
    return True

  return {
    whl: [
      extra
      for extra in whl.extras()
      if is_possible(whl.distribution(), extra)
    ]
    for whl in whls
  }

def main():
  args = parser.parse_args()

  # https://github.com/pypa/pip/blob/9.0.1/pip/__init__.py#L209
  if pip_main(["wheel", "-w", args.directory, "-r", args.input]):
    sys.exit(1)

  # Enumerate the .whl files we downloaded.
  def list_whls():
    dir = args.directory + '/'
    for root, unused_dirnames, filenames in os.walk(dir):
      for fname in filenames:
        if fname.endswith('.whl'):
          yield os.path.join(root, fname)

  whls = [Wheel(path) for path in list_whls()]
  possible_extras = determine_possible_extras(whls)

  def whl_library(wheel):
    # Indentation here matters.  whl_library must be within the scope
    # of the function below.  We also avoid reimporting an existing WHL.
    return """
  if "{repo_name}" not in native.existing_rules():
    whl_library(
        name = "{repo_name}",
        whl = "@{name}//:{path}",
        requirements = "@{name}//:requirements.bzl",
        extras = [{extras}]
    )""".format(name=args.name, repo_name=wheel.repository_name(),
                path=wheel.basename(),
                extras=','.join([
                  '"%s"' % extra
                  for extra in possible_extras.get(wheel, [])
                ]))

  whl_targets = ','.join([
    ','.join([
      '"%s": "@%s//:pkg"' % (whl.distribution().lower(), whl.repository_name())
    ] + [
      # For every extra that is possible from this requirements.txt
      '"%s[%s]": "@%s//:%s"' % (whl.distribution().lower(), extra.lower(),
                                whl.repository_name(), extra)
      for extra in possible_extras.get(whl, [])
    ])
    for whl in whls
  ])

  with open(args.output, 'w') as f:
    f.write("""\
# Install pip requirements.
#
# Generated from {input}

load("@io_bazel_rules_python//python:whl.bzl", "whl_library")

def pip_install():
  {whl_libraries}

_requirements = {{
  {mappings}
}}

all_requirements = _requirements.values()

def requirement(name):
  name = name.replace("-", "_").lower()
  return _requirements[name]
""".format(input=args.input,
           whl_libraries='\n'.join(map(whl_library, whls)),
           mappings=whl_targets))

if __name__ == '__main__':
  main()
