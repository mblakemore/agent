import pytest
from agent import _git_short_sha

def test_git_short_sha():
    """Test that _git_short_sha returns a string."""
    sha = _git_short_sha()
    assert isinstance(sha, str)
