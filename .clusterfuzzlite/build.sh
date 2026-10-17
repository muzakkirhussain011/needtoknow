#!/bin/bash -eu
# Install the package with no extras (the translations under test need no client library), then
# package each fuzz target with its interpreter so ClusterFuzzLite can run it.
pip3 install .
for fuzzer in fuzz/*_fuzzer.py; do
  compile_python_fuzzer "$fuzzer"
done
