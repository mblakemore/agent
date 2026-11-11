"""The lead files of a trimmed window: set by the first message that references files,
re-read from disk on `-c` and when a lead file is referenced again, kept when another
file is referenced.

Before this, `-c` carried the copy taken when the session began (an identity file
edited during the session was stale after every resume until the next fresh start),
and any later `@file` replaced the lead with that file."""

import json
import logging
from unittest.mock import MagicMock

import pytest

import agent

log = logging.getLogger("test_lead_files")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Iris\n\nRule one.\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("some notes\n", encoding="utf-8")
    return tmp_path


class TestExpander:
    def test_reports_the_refs_it_attached(self, repo):
        expanded, files, refs, err = agent._expand_file_refs_ex("@CLAUDE.md go")
        assert err is None and refs == ["CLAUDE.md"]
        assert "Rule one." in files and "[SYSTEM CONTEXT" in files, "the identity preamble leads"
        assert expanded.startswith("@CLAUDE.md go\n\n")

    def test_the_three_tuple_wrapper_is_unchanged(self, repo):
        assert agent._expand_file_refs("@notes.md") == (
            "@notes.md\n\n[notes.md: 1 lines]\nsome notes\n", "[notes.md: 1 lines]\nsome notes\n", None)
        assert agent._expand_file_refs("no refs here") == ("no refs here", None, None)
        _, _, err = agent._expand_file_refs("@missing.md")
        assert err and "does not exist" in err

    def test_a_decorator_name_attaches_nothing(self, repo):
        _, files, refs, err = agent._expand_file_refs_ex("use @property here")
        assert err is None and refs == [] and files == ""


class TestLeadUpdate:
    def test_the_first_files_set_the_lead(self, repo):
        _, files, refs, _ = agent._expand_file_refs_ex("@CLAUDE.md run")
        assert agent._lead_update(None, files, log) == files
        assert agent._LEAD["refs"] == ["CLAUDE.md"]

    def test_another_file_later_stays_in_its_message(self, repo):
        _, lead, refs, _ = agent._expand_file_refs_ex("@CLAUDE.md run")
        agent._lead_update(None, lead, log)
        _, files, refs2, _ = agent._expand_file_refs_ex("@notes.md look at this")
        lg = MagicMock()
        assert agent._lead_update(lead, files, lg) == lead
        assert agent._LEAD["refs"] == ["CLAUDE.md"]
        assert any("lead files kept" in str(c.args[0]) for c in lg.info.call_args_list)

    def test_naming_a_lead_file_again_re_reads_it(self, repo):
        _, lead, refs, _ = agent._expand_file_refs_ex("@CLAUDE.md run")
        agent._lead_update(None, lead, log)
        (repo / "CLAUDE.md").write_text("# Iris\n\nRule one.\nRule two.\n", encoding="utf-8")
        _, files, refs2, _ = agent._expand_file_refs_ex("./CLAUDE.md changed, re-read @./CLAUDE.md")
        fresh = agent._lead_update(lead, files, log)
        assert "Rule two." in fresh and "[SYSTEM CONTEXT" in fresh
        assert agent._LEAD["refs"] == ["CLAUDE.md"], "the lead keeps its own spelling of the ref"

    def test_no_files_changes_nothing(self, repo):
        assert agent._lead_update("lead", None, log) == "lead"
        assert agent._lead_update("lead", "", log) == "lead"


class TestLeadReload:
    def test_resume_re_reads_the_checkpointed_refs(self, repo):
        _, lead, refs, _ = agent._expand_file_refs_ex("@CLAUDE.md run")
        agent._LEAD["refs"] = list(refs)
        (repo / "CLAUDE.md").write_text("# Iris\n\nRule one.\nRule two.\n", encoding="utf-8")
        lg = MagicMock()
        fresh = agent._lead_reload(lead, lg)
        assert "Rule two." in fresh
        msg = lg.info.call_args_list[-1].args
        assert "re-read from disk on resume" in msg[0] and "changed since the checkpoint" in (msg[0] % msg[1:])

    def test_an_old_checkpoint_without_refs_reads_them_off_the_headers(self, repo):
        _, lead, _, _ = agent._expand_file_refs_ex("@CLAUDE.md and @notes.md")
        assert agent._lead_refs_from_text(lead) == ["CLAUDE.md", "notes.md"]
        agent._LEAD["refs"] = []
        (repo / "notes.md").write_text("newer notes\n", encoding="utf-8")
        fresh = agent._lead_reload(lead, log)
        assert "newer notes" in fresh and agent._LEAD["refs"] == ["CLAUDE.md", "notes.md"]

    def test_a_missing_file_keeps_the_checkpoint_copy(self, repo):
        _, lead, refs, _ = agent._expand_file_refs_ex("@CLAUDE.md run")
        agent._LEAD["refs"] = list(refs)
        (repo / "CLAUDE.md").unlink()
        lg = MagicMock()
        assert agent._lead_reload(lead, lg) == lead
        assert lg.warning.called

    def test_nothing_to_reload(self, repo):
        assert agent._lead_reload(None, log) is None
        assert agent._lead_reload("free text without headers", log) == "free text without headers"


def _first_message(text="@CLAUDE.md run your next cycle!"):
    """A history as a session leaves it: the first message with its attachment and
    a runtime note appended, an assistant reply."""
    expanded, files, refs, _ = agent._expand_file_refs_ex(text)
    agent._LEAD["refs"] = []
    lead = agent._lead_update(None, files, log)
    first = {"role": "user", "content": expanded + "\n\n" + agent._RUNTIME_NOTE_HEAD +
             "\nCurrent time: t0", "_tokens": 99}
    return [first, {"role": "assistant", "content": "ok"}], lead


class TestPlace:
    """The lead files live in one history message; re-reads refresh it in place."""

    def test_re_referencing_the_lead_refreshes_its_place_and_leaves_a_note(self, repo):
        history, lead = _first_message()
        (repo / "CLAUDE.md").write_text("# Iris\n\nRule one.\nRule two.\n", encoding="utf-8")
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md made some updates")
        new_lead, body = agent._lead_absorb(history, lead, expanded, files, log)
        assert "Rule two." in new_lead
        place = history[0]["content"]
        assert place.startswith("@CLAUDE.md run your next cycle!\n\n[SYSTEM CONTEXT")
        assert "Rule two." in place and place.count("Rule one.") == 1
        assert place.endswith("\n\n" + agent._RUNTIME_NOTE_HEAD + "\nCurrent time: t0")
        assert "_tokens" not in history[0], "the cached token count is dropped with the old text"
        assert body == "@CLAUDE.md made some updates\n\n" + agent._lead_note()
        assert "Rule" not in body and "AGENT IDENTITY FILE" not in body
        assert agent._lead_refs_from_text(body) == [], "the note is not a header"

    def test_an_unchanged_file_still_goes_to_its_place_not_the_message(self, repo):
        history, lead = _first_message()
        before = history[0]["content"]
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md again")
        _, body = agent._lead_absorb(history, lead, expanded, files, log)
        assert history[0]["content"] == before and history[0]["_tokens"] == 99
        assert body.endswith(agent._lead_note()) and "Rule one." not in body

    def test_another_file_alongside_the_lead_rides_in_the_message(self, repo):
        history, lead = _first_message()
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md and @notes.md")
        _, body = agent._lead_absorb(history, lead, expanded, files, log)
        assert "some notes" in body and "[notes.md: 1 lines]" in body
        assert "Rule one." not in body and agent._lead_note() in body

    def test_other_files_alone_leave_the_place_and_the_message_as_they_were(self, repo):
        history, lead = _first_message()
        expanded, files, _, _ = agent._expand_file_refs_ex("@notes.md look")
        new_lead, body = agent._lead_absorb(history, lead, expanded, files, log)
        assert new_lead is lead and body == expanded and history[0]["_tokens"] == 99

    def test_no_place_in_the_history_keeps_the_file_in_the_message(self, repo):
        _, lead = _first_message()
        history = [{"role": "user", "content": "a session whose first message was summarized away"}]
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md again")
        _, body = agent._lead_absorb(history, lead, expanded, files, log)
        assert body == expanded

    def test_the_first_files_make_their_own_message_the_place(self, repo):
        agent._LEAD["refs"] = []
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md go")
        new_lead, body = agent._lead_absorb([], None, expanded, files, log)
        assert new_lead == files and body == expanded

    def test_resume_places_the_disk_copy_and_reduces_an_old_duplicate(self, repo):
        history, lead = _first_message()
        # a second whole copy, as every re-reference left before the place existed
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md Run your next cycle!")
        history.append({"role": "user", "content": expanded + "\n\n" + agent._RUNTIME_NOTE_HEAD + "\nt1"})
        (repo / "CLAUDE.md").write_text("# Iris\n\nRule one.\nRule two.\n", encoding="utf-8")
        fresh = agent._lead_reload(lead, log)
        assert agent._lead_place(history, fresh, log)
        assert "Rule two." in history[0]["content"]
        assert history[2]["content"] == ("@CLAUDE.md Run your next cycle!\n\n" + agent._lead_note() +
                                         "\n\n" + agent._RUNTIME_NOTE_HEAD + "\nt1")
        assert sum(m["content"].count("Rule one.") for m in history) == 1

    def test_a_later_copy_carrying_other_files_is_not_cut(self, repo):
        history, lead = _first_message()
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md with @notes.md")
        history.append({"role": "user", "content": expanded})
        assert agent._lead_place(history, lead, log)
        assert history[2]["content"] == expanded

    def test_the_banner_is_shown_once_for_a_re_reference(self, repo, monkeypatch):
        history, lead = _first_message()
        shown = []
        monkeypatch.setattr(agent, "_emit", lambda ev, *a: shown.append(ev) if ev == "on_file_attached" else None)
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md again")
        agent._lead_absorb(history, lead, expanded, files, log)
        assert shown == ["on_file_attached"]

    def test_an_untrimmed_window_carries_the_lead_once(self, repo):
        history, lead = _first_message()
        (repo / "CLAUDE.md").write_text("# Iris\n\nRule one.\nRule two.\n", encoding="utf-8")
        expanded, files, _, _ = agent._expand_file_refs_ex("@CLAUDE.md updated")
        lead, body = agent._lead_absorb(history, lead, expanded, files, log)
        history.append({"role": "user", "content": body})
        sent, _ = agent._build_context(history, {"text": "", "up_to": 0}, lead, 60000, 1000, log)
        text = "\n".join(m["content"] for m in sent if isinstance(m.get("content"), str))
        assert text.count("Rule two.") == 1 and "Rule one.\nRule two." in text


def test_the_checkpoint_carries_the_refs(repo, monkeypatch):
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(repo / "cp.json"))
    agent._LEAD["refs"] = ["CLAUDE.md"]
    agent._save_checkpoint([{"role": "user", "content": "x"}], {"text": "", "up_to": 0}, 3, "lead text")
    assert json.load(open(repo / "cp.json"))["initial_file_refs"] == ["CLAUDE.md"]
    agent._LEAD["refs"] = []
    hist, state, turn, files, clean = agent._load_checkpoint()
    assert (turn, files) == (3, "lead text") and agent._LEAD["refs"] == ["CLAUDE.md"]
