import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from agent import _expand_file_refs

def test_expand_valid_small_file(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("Hello World", encoding="utf-8")
    
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        expanded, files, err = _expand_file_refs(f"@{f.absolute()}")
        assert err is None
        assert "Hello World" in expanded
        # The header uses the ref string as provided in the input
        assert f"[{f.absolute()}: 1 lines]" in expanded

def test_expand_valid_large_file(tmp_path):
    f = tmp_path / "large.txt"
    content = "\n".join([f"Line {i}" for i in range(1000)])
    f.write_text(content, encoding="utf-8")
    
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        expanded, files, err = _expand_file_refs(f"@{f.absolute()}")
        assert err is None
        assert "first" in expanded
        assert "of 1000 lines" in expanded
        assert "Line 0" in expanded

def test_expand_missing_file(tmp_path):
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        expanded, files, err = _expand_file_refs(f"@missing.txt")
        assert err is not None
        assert "does not exist" in err

def test_expand_directory(tmp_path):
    d = tmp_path / "my_dir"
    d.mkdir()
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        expanded, files, err = _expand_file_refs(f"@{d.absolute()}")
        assert err is not None
        assert "is a directory, not a file" in err

def test_expand_outside_cwd(tmp_path):
    outside = Path("/tmp/agent_secret_test.txt")
    outside.write_text("secret", encoding="utf-8")
    try:
        with patch("os.getcwd", return_value=str(tmp_path)), \
             patch("pathlib.Path.cwd", return_value=tmp_path):
            expanded, files, err = _expand_file_refs(f"@{outside.absolute()}")
            assert err is not None
            assert "outside the working directory" in err
    finally:
        if outside.exists():
            outside.unlink()

def test_expand_agent_md(tmp_path):
    f = tmp_path / "agent.md"
    f.write_text("I am an agent", encoding="utf-8")
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        expanded, files, err = _expand_file_refs(f"@{f.absolute()}")
        assert err is None
        assert "AGENT IDENTITY FILE" in expanded
        assert "This is YOUR agent.md" in expanded

def test_expand_os_error(tmp_path):
    f = tmp_path / "error.txt"
    f.write_text("text", encoding="utf-8")
    # We only want to patch the resolve() call on the file path, not the CWD resolve.
    # Since p = Path(ref), we patch Path.resolve.
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path), \
         patch("pathlib.Path.resolve", side_effect=OSError(2, "No such file or directory")):
        # Since Path.cwd().resolve() is called first, and we patched Path.resolve,
        # it will still fail there. We need to be more specific or allow the first one.
        # Instead, let's use a mock that only fails on the second call.
        pass

def test_expand_os_error_v2(tmp_path):
    f = tmp_path / "error.txt"
    f.write_text("text", encoding="utf-8")
    
    with patch("os.getcwd", return_value=str(tmp_path)), \
         patch("pathlib.Path.cwd", return_value=tmp_path):
        # We patch the instance of Path created for the ref
        with patch("pathlib.Path.resolve") as mock_resolve:
            # First call is Path.cwd().resolve(), second is p.resolve()
            mock_resolve.side_effect = [tmp_path, OSError(2, "No such file or directory")]
            expanded, files, err = _expand_file_refs(f"@{f.absolute()}")
            assert err is not None
            assert "No such file or directory" in err
