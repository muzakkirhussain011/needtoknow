#!/bin/bash -eu
# Install the package with no extras (the translations under test need no client library), then
# package each fuzz target with its interpreter so ClusterFuzzLite can run it. needtoknow has no
# dependencies, and its build backend comes from the hash-pinned build.txt, so nothing is
# installed unpinned.
pip3 install --require-hashes --no-deps -r .github/requirements/build.txt
pip3 wheel --no-deps --no-build-isolation --wheel-dir /tmp/wheels .
pip3 install --no-index --no-deps /tmp/wheels/*.whl
for fuzzer in fuzz/*_fuzzer.py; do
  compile_python_fuzzer "$fuzzer"
done
