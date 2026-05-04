"""pytest configuration for the test suite.

file_tool write/append path confinement (#847): the file tool now rejects writes to
paths outside the working directory.  All tests in test_file_tool.py that write to
tempfile.TemporaryDirectory() paths need to run with a cwd that is an ancestor of
those temp paths (or equal to the temp dir itself).

find_symbol path confinement (#856): find_symbol now refuses to search paths outside
cwd.  Most test_find_symbol.py tests use tempfile.mkdtemp() which lives under /tmp,
so we set cwd=/tmp for those tests.  TestFindSymbolPathConfinement tests the
confinement boundary itself and needs cwd=/droid/repos/agent so relative happy-path
lookups (path='.', path='tools/find_symbol.py') work correctly.  Classes that search
real repo files (AC1–AC4, etc.) use _REPO_ROOT paths and run with the default cwd.

search_files path confinement (#863): search_files now refuses to search paths outside
cwd.  Most test_search_files.py tests use tempfile.TemporaryDirectory() which lives
under /tmp, so we set cwd=/tmp for those tests.  TestSearchFilesPathConfinement tests
the confinement boundary itself and needs cwd=/droid/repos/agent so that relative
happy-path lookups (path='.', path='tools/') resolve inside cwd, while absolute
outside paths (/etc, /home, ../other) are correctly rejected.

The ``tmp_cwd`` fixture sets cwd=/tmp for test_file_tool.py (except
TestFileWritePathConfinement).  The ``find_symbol_cwd`` fixture handles the
test_find_symbol.py cwd routing.  The ``search_files_cwd`` fixture handles the
test_search_files.py cwd routing.
"""

import os
import tempfile
import pytest

_AGENT_REPO = "/droid/repos/agent"

# test_find_symbol classes that search real repo paths (agent.py, etc.) — these
# must run with cwd = repo root so their _REPO_ROOT / _AGENT_PY paths are inside cwd.
_FIND_SYMBOL_REPO_PATH_CLASSES = {
    "TestFindSymbolAC1",
    "TestFindSymbolAC2",
    "TestFindSymbolAC3",
    "TestFindSymbolAC4",
    "TestFindSymbolInvalidMode",
    "TestFindSymbolInvalidKind",
    "TestFindSymbolEmptyName",
    "TestFindSymbolRegistered",
    "TestFindSymbolNonStringGuards",
}


@pytest.fixture(autouse=True)
def tmp_cwd(request):
    """Set cwd=/tmp for test_file_tool.py tests (except TestFileWritePathConfinement).

    This makes absolute /tmp/... paths used in existing tests resolve as *inside* cwd,
    because /tmp is an ancestor of all tempfile.mkdtemp() paths.

    TestFileWritePathConfinement manages its own cwd explicitly and is excluded.
    Only applies to test_file_tool.py.
    """
    if "test_file_tool" not in request.fspath.basename:
        yield
        return

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


@pytest.fixture(autouse=True)
def find_symbol_cwd(request):
    """Set an appropriate cwd for each test class in test_find_symbol.py.

    find_symbol now enforces path confinement (#856), so each test needs a cwd
    that contains all paths it passes to find_symbol.

    - TestFindSymbolPathConfinement: cwd = /droid/repos/agent, so that relative
      happy-path lookups (path='.', path='tools/find_symbol.py') resolve inside
      cwd, while absolute outside paths (/etc, /tmp) are correctly rejected.

    - Classes that search real repo files (_REPO_ROOT / _AGENT_PY): no cwd change —
      the default pytest cwd (/mnt/droid/repos/agent) already contains those paths.

    - All other classes (which use tempfile.mkdtemp() -> /tmp/...): cwd = /tmp so
      that absolute temp-dir paths are inside cwd and the confinement check passes.

    Only applies to test_find_symbol.py.
    """
    if "test_find_symbol" not in request.fspath.basename:
        yield
        return

    cls = request.node.cls
    cls_name = cls.__name__ if cls is not None else None

    if cls_name == "TestFindSymbolPathConfinement":
        orig = os.getcwd()
        os.chdir(_AGENT_REPO)
        try:
            yield
        finally:
            os.chdir(orig)
    elif cls_name in _FIND_SYMBOL_REPO_PATH_CLASSES or cls_name is None:
        # No cwd change — default cwd contains the repo paths used in these tests.
        yield
    else:
        # tempdir-using tests: set cwd=/tmp so /tmp/... paths are inside cwd.
        orig = os.getcwd()
        os.chdir("/tmp")
        try:
            yield
        finally:
            os.chdir(orig)


# test_search_files classes that need the default (repo-root) cwd rather than /tmp.
# Currently empty: TestSearchFilesPathIsFile manages its own cwd in the one method
# that uses a real repo path (test_issue_567_reproduction); all other methods use
# tempfile.TemporaryDirectory() which lives under /tmp.
_SEARCH_FILES_REPO_PATH_CLASSES: set[str] = set()


@pytest.fixture(autouse=True)
def search_files_cwd(request):
    """Set an appropriate cwd for each test class in test_search_files.py.

    search_files now enforces path confinement (#863), so each test needs a cwd
    that contains all paths it passes to search_files.

    - TestSearchFilesPathConfinement: cwd = /droid/repos/agent, so that relative
      happy-path lookups (path='.', path='tools/') resolve inside cwd, while
      absolute outside paths (/etc, /home, ../other) are correctly rejected.

    - Classes in _SEARCH_FILES_REPO_PATH_CLASSES: no cwd change — the default
      pytest cwd already contains their paths.

    - All other classes (which use tempfile.TemporaryDirectory() -> /tmp/...):
      cwd = /tmp so that absolute temp-dir paths are inside cwd and the
      confinement check passes.

    Only applies to test_search_files.py.
    """
    if "test_search_files" not in request.fspath.basename:
        yield
        return

    cls = request.node.cls
    cls_name = cls.__name__ if cls is not None else None

    if cls_name == "TestSearchFilesPathConfinement":
        orig = os.getcwd()
        os.chdir(_AGENT_REPO)
        try:
            yield
        finally:
            os.chdir(orig)
    elif cls_name in _SEARCH_FILES_REPO_PATH_CLASSES or cls_name is None:
        # No cwd change — default cwd contains the repo paths used in these tests.
        yield
    else:
        # tempdir-using tests: set cwd=/tmp so /tmp/... paths are inside cwd.
        orig = os.getcwd()
        os.chdir("/tmp")
        try:
            yield
        finally:
            os.chdir(orig)
