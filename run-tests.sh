#!/usr/bin/env sh
# Runs the tests of the plugin with the Python standard library only (no pip install).
# PYTHON=/path/to/python ./run-tests.sh   overrides the interpreter (default: python3).
set -eu
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" -m unittest discover -s tests -v
