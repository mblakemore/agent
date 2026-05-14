"""pytest configuration for the test suite.

bedrock proactive quota check (#864): BedrockBackend.__init__ calls
_log_token_usage(), which hits the real gateway.  When quota is over 100%
(as it currently is), the new proactive check in complete()/stream_chat()
raises BedrockBudgetExceeded before the test's own mock can run.  The
``mock_bedrock_token_usage`` fixture below patches get_token_usage() to
return safe data for all bedrock backend/tool_loop/cost_cap test files,
keeping the real quota state out of tests that don't test quota behavior.

file_tool write/append path confinement (#847): the file tool now rejects writes to
paths outside the working directory.  All tests in test_file_tool.py that write to
tempfile.TemporaryDirectory() paths need to run with a cwd that is an ancestor of
those temp paths (or equal to the temp dir itself).

find_symbol path confinement (#856): find_symbol now refuses to search paths outside
cwd.  Most test_find_symbol.py tests use tempfile.mkdtemp() which lives under /tmp,
so we set cwd=/tmp for those tests.  TestFindSymbolPathConfinement tests the
confinement boundary itself and needs cwd=/droid/repos/agent so relative happy-path
lookups (path='.', path='tools/find_symbol.py') work correctly.  Classes that search
real repo files (AC1–AC4, etc.) chdir to the resolved repo root so their absolute
_REPO_ROOT / _AGENT_PY lookups satisfy path confinement when pytest is invoked from
a linked worktree (#1013, where CICD builders run pytest from
WORKTREE_ROOT/NNN-slug, a sibling of _REPO_ROOT rather than an ancestor).

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
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

_AGENT_REPO = "/droid/repos/agent"


def _resolve_repo_root() -> str:
    """Return the absolute path of the main repo root.

    Computed via ``git rev-parse --path-format=absolute --git-common-dir`` so
    it resolves to the **main** repo path even when pytest is invoked from a
    linked worktree (where ``--show-toplevel`` would return the worktree path
    instead). Matches the resolution used by ``tests/test_find_symbol.py``'s
    ``_REPO_ROOT``, so chdir'ing here makes the test file's absolute
    ``_REPO_ROOT`` / ``_AGENT_PY`` paths satisfy ``find_symbol``'s
    path-confinement check (#856).
    """
    common = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        text=True,
    ).strip()
    return str(Path(common).parent)


_REPO_ROOT = _resolve_repo_root()

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
        # cwd = main repo root, so the test's absolute _REPO_ROOT / _AGENT_PY
        # paths satisfy find_symbol's path-confinement check (#856). Without
        # this chdir, pytest invoked from a linked worktree (CICD's typical
        # invocation) has cwd that is a *sibling* of _REPO_ROOT, not an
        # ancestor — every AC2/AC3/AC4 lookup is rejected before _is_excluded
        # (#1013) is reached.
        orig = os.getcwd()
        os.chdir(_REPO_ROOT)
        try:
            yield
        finally:
            os.chdir(orig)
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


# Bedrock test files that need the safe-quota stub.  Tests within these files
# that specifically test quota behaviour set _cached_usage_pct directly on the
# BedrockBackend instance after construction.
_BEDROCK_TEST_FILES = {
    "test_bedrock_backend",
    "test_bedrock_backend_tool_loop",
    "test_bedrock_cost_cap",
    "test_bedrock_quota_check",
    "test_bedrock_spend_logging",
    "test_bedrock_dev_mode_roundtrip",
    "test_bedrock_security",
}

_SAFE_TOKEN_USAGE = {"total_tokens": 100_000, "token_limit": 1_000_000}


@pytest.fixture(autouse=True)
def mock_bedrock_token_usage(request):
    """Patch BedrockChatAPI.get_token_usage to return safe (10%) quota data.

    BedrockBackend.__init__ calls _log_token_usage() which hits the real gateway.
    When the account quota is over 100% the new proactive check (#864) raises
    BedrockBudgetExceeded before the test's own mock can run.  This fixture
    keeps the real quota state out of tests that don't exercise quota behaviour.

    Only applies to the test files listed in _BEDROCK_TEST_FILES.
    Tests that need a >100% scenario should set b._cached_usage_pct directly.
    """
    stem = request.fspath.basename.replace(".py", "")
    if stem not in _BEDROCK_TEST_FILES:
        yield
        return

    # Exclude tests that directly exercise _log_token_usage or get_token_usage
    # — they use their own mocks and need the real implementation accessible.
    test_name = request.node.name
    _USAGE_TEST_PREFIXES = (
        "test_token_usage_",     # _log_token_usage behaviour tests
        "test_get_token_usage_", # BedrockChatAPI.get_token_usage unit tests
    )
    if any(test_name.startswith(p) for p in _USAGE_TEST_PREFIXES):
        yield
        return

    # Patch BedrockBackend._log_token_usage as a no-op.  This prevents the
    # startup probe in __init__ from hitting the real gateway (which is
    # currently >100%) and leaving _cached_usage_pct ≥ 100, which would cause
    # the new proactive quota check to raise BedrockBudgetExceeded in every
    # test that calls complete() or stream_chat().
    # _cached_usage_pct stays at 0.0 (the default), so the proactive check
    # is satisfied and the test's own send_and_wait mock runs normally.
    with patch("llm_backend.BedrockBackend._log_token_usage"):
        yield
