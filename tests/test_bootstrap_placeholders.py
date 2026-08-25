"""Bootstrap placeholders are opt-in: declared in config, or nothing happens.

The capability is useful — an instructions file that references a scratch file which
does not exist produces a read error on every startup. Shipping one project's
filenames as the default is what makes it a problem: the tool would create another
project's layout in a stranger's repository, unasked.

So both directions matter, and both are tested here. Proving it stays quiet proves
nothing on its own if the feature is simply broken.
"""
import json
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_REPO, "agent.py")


def _run(cwd):
    env = dict(os.environ, AGENT_FAKE_BACKEND="1")
    return subprocess.run([sys.executable, _AGENT, "-a", "go"], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180, env=env)


def test_nothing_is_created_when_nothing_is_declared(tmp_path):
    """A stranger's repo mentioning these paths must come back untouched."""
    (tmp_path / "CLAUDE.md").write_text(
        "Read messages/from-creator.md every cycle. See state/ and logs/.\n", encoding="utf-8")
    _run(tmp_path)
    for created in ("messages", "state", "logs"):
        assert not (tmp_path / created).exists(), f"{created} was created without being declared"


def test_declared_placeholders_are_created(tmp_path):
    """The capability itself still works — with the caller's names, not ours."""
    (tmp_path / "CLAUDE.md").write_text(
        "Read notes/inbox.md every cycle. Also uses state/ for persistence.\n", encoding="utf-8")
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps(
        {"bootstrap": {"placeholder_files": ["notes/inbox.md"],
                       "placeholder_dirs": ["state"]}}), encoding="utf-8")
    _run(tmp_path)
    assert (tmp_path / "notes" / "inbox.md").is_file()
    assert (tmp_path / "state").is_dir()


def test_declared_but_unreferenced_paths_are_not_created(tmp_path):
    """Declaring a path is permission, not an instruction: the agent's own
    instructions file still has to ask for it."""
    (tmp_path / "CLAUDE.md").write_text("This file mentions nothing in particular.\n",
                                        encoding="utf-8")
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps(
        {"bootstrap": {"placeholder_files": ["notes/inbox.md"]}}), encoding="utf-8")
    _run(tmp_path)
    assert not (tmp_path / "notes").exists()
