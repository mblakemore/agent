"""pytest configuration for the test suite.

file_tool write/append path confinement (#847): the file tool now rejects writes to
paths outside the working directory.  All tests in test_file_tool.py that write to
tempfile.TemporaryDirectory() paths need to run with a cwd that is an ancestor of
those temp paths (or equal to the temp dir itself).

The ``tmp_cwd`` fixture below provides an isolated temporary directory as the cwd
for every test in test_file_tool.py, so that absolute /tmp/... paths are inside cwd.
Tests that explicitly test the confinement check (TestFileWritePathConfinement) opt out
by managing their own cwd in setUp/tearDown.
"""

import os
import tempfile
import pytest


@pytest.fixture(autouse=True)
def tmp_cwd(request, tmp_path):
    """For tests in test_file_tool.py, set cwd to tmp_path (a pytest-managed temp dir).

    This makes absolute /tmp/... paths used in existing tests resolve as *inside* cwd,
    because tmp_path itself is a subdirectory of /tmp.

    Tests in TestFileWritePathConfinement manage their own cwd and must NOT have
    tmp_path imposed on them — they test the confinement boundary explicitly.
    Only applies to test_file_tool.py.
    """
    if "test_file_tool" not in request.fspath.basename:
        yield
        return

    # TestFileWritePathConfinement manages its own cwd explicitly
    cls = request.node.cls
    if cls is not None and cls.__name__ == "TestFileWritePathConfinement":
        yield
        return

    orig = os.getcwd()
    # Use /tmp as cwd so that any /tmp/... path is inside cwd
    os.chdir("/tmp")
    try:
        yield
    finally:
        os.chdir(orig)
