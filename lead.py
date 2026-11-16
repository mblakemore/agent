"""File references and the lead: `@path` expansion, identity files, and the one place
the lead files live in a trimmed window (agent.py's `initial_files`).

`expand_file_refs` turns `@path` references in a message into attached file contents;
an identity file (AGENT.md / CLAUDE.md) loads whole and carries the working-directory
preamble. The `LEAD` record names the files that lead a trimmed window so they can be
re-read from disk (`update`, `reload`) and placed back where they first appeared
(`place`, `absorb`) instead of being sent twice. `configure` is called once by agent.py
with the `context` config values and the UI callback hook.
"""
import os
import re
from pathlib import Path

FILE_REF = re.compile(r"(?<!\w)@(\.{0,2}/[^\s,;)]+|(?![^\s@]*[@:])[A-Za-z_][^\s,;:)}\]]*)")
RUNTIME_NOTE_HEAD = "[Runtime note — from the framework, not the user or the tool]"

_CFG = {"max_full_lines": 800, "preview_lines": 200, "model_guards": True,
        "emit": lambda *a, **k: None}


def configure(max_full_lines=None, preview_lines=None, model_guards=None, emit=None):
    """Set the `context` values this module reads and the UI hook for file banners.
    `model_guards` False drops the prompt text that steers small models (the identity
    sentence and the working-directory preamble); the structural headers stay."""
    if max_full_lines is not None:
        _CFG["max_full_lines"] = int(max_full_lines)
    if preview_lines is not None:
        _CFG["preview_lines"] = int(preview_lines)
    if model_guards is not None:
        _CFG["model_guards"] = bool(model_guards)
    if emit is not None:
        _CFG["emit"] = emit


def expand_file_refs(text):
    """Expand @filepath references in user input to inline file contents.

    Returns (expanded_text, files_content, error).
    """
    expanded, files_content, _refs, err = expand_file_refs_ex(text)
    return expanded, files_content, err


def expand_file_refs_ex(text):
    """`expand_file_refs` that also returns the refs it attached, as typed, so the
    lead of a trimmed window can be re-read from disk later (`update`,
    `reload`). Returns (expanded_text, files_content, attached_refs, error)."""
    refs = FILE_REF.findall(text)
    if not refs:
        return text, None, [], None
    files_content, attached, err = attach_file_refs(refs)
    if err:
        return None, None, [], err
    return text + "\n\n" + files_content, files_content, attached, None


def attach_file_refs(refs, announce=True):
    """Read the files behind @refs. Returns (files_content, attached_refs, error);
    files_content carries the working-directory preamble when an identity file is
    among them, exactly as the lead message of a trimmed window needs it. `announce`
    shows each header in the UI; a re-read of files the UI already announced for
    this message passes False (two banners for one file read as a double load)."""
    # Resolve working directory once so every ref can be checked against it.
    # We use Path.cwd().resolve() (not os.getcwd()) to follow any symlinks in
    # the cwd itself, giving a canonical base for confinement checks.
    cwd_resolved = Path.cwd().resolve()
    cwd_prefix = str(cwd_resolved) + os.sep  # e.g. <repo>/

    seen = set()
    attachments = []
    attached = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)

        p = Path(ref)
        try:
            path_exists = p.exists()
            path_is_dir = p.is_dir() if path_exists else False
            resolved_ref = p.resolve()
        except OSError as exc:
            return None, [], f"Error: '{ref}': {exc.strerror}"
        if not path_exists:
            # Bare identifiers with no path separator or extension (e.g. @state,
            # @transition) are Python decorators or @-mentions — not file refs.
            # Silently skip rather than erroring so prompts mentioning decorator
            # names don't abort before the first LLM call.
            if '/' not in ref and '.' not in ref:
                continue
            return None, [], f"Error: file '{ref}' does not exist"
        if path_is_dir:
            return None, [], f"Error: '{ref}' is a directory, not a file"

        # Confinement check: reject any ref that resolves outside the working
        # directory.  This blocks both relative traversals (@../../secret) and
        # absolute paths (@/etc/passwd) that escape the project tree.
        if resolved_ref != cwd_resolved and not str(resolved_ref).startswith(cwd_prefix):
            return (
                None,
                [],
                f"Error: '{ref}' resolves to '{resolved_ref}' which is outside "
                f"the working directory '{cwd_resolved}'. "
                f"Only files inside the current working directory can be referenced with @.",
            )

        lines = p.read_text(encoding='utf-8', errors='replace').splitlines(True)
        total = len(lines)
        # Identity files load WHOLE regardless of length — a preview-
        # truncated identity is a lobotomy, not a summary. claude.md
        # counts too (the DC/agentx convention; _bootstrap_template_check
        # already treats both as the cognitive-instructions file — this
        # loader disagreed and served 10-line stubs of >50-line CLAUDE.mds,
        # caught in the 2026-08-15 scaffold review).
        _is_agent_md = p.name.lower() in ("agent.md", "claude.md")
        if total <= _CFG["max_full_lines"] or _is_agent_md:
            content = "".join(lines)
            header = f"[{ref}: {total} lines]"
        else:
            content = "".join(lines[:_CFG["preview_lines"]])
            header = f"[{ref}: first {_CFG['preview_lines']} of {total} lines]"

        resolved = str(p.resolve())
        if _is_agent_md:
            note = "This is YOUR identity file — do not search for it elsewhere. " if _CFG["model_guards"] else ""
            header = f"[AGENT IDENTITY FILE: {ref} (loaded from {resolved}). {note}{total} lines]"

        attachments.append(f"{header}\n{content}")
        attached.append(ref)
        if announce:
            # The console gets the file and where it came from; the sentence telling
            # the model it is its own identity file is for the model, not the operator.
            _CFG["emit"]("on_file_attached",
                  f"[{ref}: loaded from {resolved}, {total} lines]" if _is_agent_md else header)

    files_content = "\n\n".join(attachments)
    # Prepend working directory context when agent.md is loaded
    cwd = os.getcwd()
    if _CFG["model_guards"] and any(Path(ref).name.lower() in ("agent.md", "claude.md") for ref in seen):
        preamble = (
            f"[SYSTEM CONTEXT: Your working directory is {cwd}. "
            f"All relative paths resolve from here. "
            f"Do not cd to other repositories or search for files outside this tree. "
            f"Do not start with a directory listing for orientation. The task prompt specifies the repo path. "
            f"Begin with the first tool call that directly advances the task — read a specific file, "
            f"search for a pattern, or look up a symbol. "
            f"Use `file list` only when you specifically need to enumerate a directory's contents as part of the task. "
            f"**Search before reading large files.** When you need to find where something is defined or called, use `search_files` first to locate the right file and line — then read only from that line using `start_line=`. Never read a whole file >300 lines hoping to find something; search first.]\n\n"
        )
    else:
        preamble = ""
    return (preamble + files_content if preamble else files_content), attached, None


# The files that lead a trimmed window (`initial_files`), as the refs that named them.
# Kept so the lead can be re-read from disk instead of carried as the copy taken when
# the session began: an identity file edited during the session (an agent that edits
# its own several times a day) was stale after every `-c` until the next fresh start.
LEAD = {"refs": []}
LEAD_HEADER_RE = re.compile(r"^\[(\S+): (?:first \d+ of )?\d+ lines\]$|"
                             r"^\[AGENT IDENTITY FILE: (\S+) \(loaded from ", re.MULTILINE)


def refs_from_text(initial_files):
    """The refs behind a lead built before refs were checkpointed, read off its headers."""
    out = []
    if not isinstance(initial_files, str):   # tests and old code hand lists through here
        return out
    for m in LEAD_HEADER_RE.finditer(initial_files):
        ref = m.group(1) or m.group(2)
        if ref and ref not in out:
            out.append(ref)
    return out


def update(initial_files, files, log, refs=None):
    """Decide what leads the trimmed window after a message that referenced files
    (`files` is what `expand_file_refs` attached; `refs` are the refs it attached, as
    typed, and without them they are read off the headers in `files`, which a file
    quoting a header can fool).

    The first message with files sets the lead. A later message that names one of
    the lead files again re-reads them all from disk (so `@CLAUDE.md` refreshes an
    edited identity file); one that names other files keeps the lead as it is, and
    those files stay in their own message. Replacing the lead with whichever file
    was referenced last made `@notes.md look at this` drop the identity file from
    every later window."""
    if not files:
        return initial_files
    refs = list(refs) if refs else refs_from_text(files)
    if not initial_files or not LEAD["refs"]:
        LEAD["refs"] = list(refs)
        return files
    if not any(same_path(r, lead) for r in refs for lead in LEAD["refs"]):
        log.info("lead files kept (%s); %s stays in its message",
                 ", ".join(LEAD["refs"]), ", ".join(refs))
        return initial_files
    # The UI announced these files when the message was expanded; a second banner
    # for the same read looked like the file being loaded twice.
    fresh, attached, err = attach_file_refs(LEAD["refs"], announce=False)
    if err or not fresh:
        log.warning("lead files not re-read (%s); the earlier copy stays", err or "nothing attached")
        return initial_files
    LEAD["refs"] = list(attached)
    log.info("lead files re-read from disk: %s%s", ", ".join(attached),
             "" if fresh == initial_files else " (changed)")
    return fresh


def same_path(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


# ── The lead's one place ──────────────────────────────────────────────────────
# The lead files live in exactly one history message: the one that first attached
# them. A later message that names a lead file again re-reads it from disk, but the
# fresh text goes into that place and the new message keeps a one-line note; before
# this, every re-reference (`@CLAUDE.md run your next cycle`, the usual way an edited
# identity file was pushed to a running agent) added a second whole copy, and an
# untrimmed window sent both, one of them stale, for the rest of the session
# (measured 2026-11-11: 8k of a 33k-token request). `-c` places the disk copy too,
# so an edit takes effect at the next start without a re-reference.
LEAD_PREAMBLE_RE = re.compile(r"\[SYSTEM CONTEXT: Your working directory is [^\n]*\]\n\n\Z")


def block(content, refs):
    """(start, end) of the attachment block of a message that carries one of the lead
    `refs` (any spelling of the same path): from the working-directory preamble (or
    the header) to the runtime note or the end. Attachments always close a message
    (`text + "\\n\\n" + files`) and a runtime note is only ever appended after them, so
    the end is found first and the header is the first one before it naming a lead
    file. A header-like line typed by the person can still fool this; `span`
    is tried first where the earlier text is known."""
    if not isinstance(content, str):
        return None
    end = content.find("\n\n" + RUNTIME_NOTE_HEAD)
    if end == -1:
        end = len(content)
    start = None
    for m in LEAD_HEADER_RE.finditer(content, 0, end):
        ref = m.group(1) or m.group(2)
        if ref and any(same_path(ref, lead) for lead in refs):
            start = m.start()
            break
    if start is None:
        return None
    pm = LEAD_PREAMBLE_RE.search(content[:start])
    if pm:
        start = pm.start()
    return start, end


def span(content, old):
    """The exact span of `old` (the lead text as it was last attached) in a message, if
    it is there verbatim: the block without any header parsing. A trailing newline
    the pinned-block extractor stripped does not matter."""
    if not isinstance(content, str) or not isinstance(old, str) or not old:
        return None
    o = old.rstrip("\n")
    if not o:
        return None
    i = content.find(o)
    if i == -1:
        return None
    e = i + len(o)
    while e < len(content) and content[e] == "\n" \
            and not content.startswith("\n\n" + RUNTIME_NOTE_HEAD, e):
        e += 1
    return i, e


def note():
    return "[" + ", ".join(LEAD["refs"]) + ": re-read from disk; the current text leads this conversation]"


def place(conversation_history, fresh, log, old=None):
    """Put `fresh` (the current lead text) in its place: the first user message carrying
    a lead file has its attachment block replaced; any later user message whose
    attachments are exactly the lead files is reduced to the note. `old` is the lead
    text as it was last attached (the previous `initial_files`): where a message holds
    it verbatim, that span is the block, so a header-like line in the typed text
    cannot widen the cut. A message already holding the text is left untouched (no
    cache cost); `LEAD["replaced"]` is set when one was rewritten, for the cache-miss
    log. Returns True when a place was found."""
    refs = LEAD["refs"]
    if not refs or not isinstance(fresh, str) or not fresh or not conversation_history:
        return False
    placed = False
    for msg in conversation_history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        blk = (span(content, old) if not placed else None) or block(content, refs)
        if not blk:
            continue
        s, e = blk
        if not placed:
            replacement = fresh
            placed = True
        else:
            block_refs = refs_from_text(content[s:e])
            if not block_refs or not all(any(same_path(r, lead) for lead in refs) for r in block_refs):
                continue                  # carries other files too; not ours to cut
            replacement = note()
        if content[s:e].rstrip("\n") != replacement.rstrip("\n"):
            msg["content"] = content[:s] + replacement + content[e:]
            msg.pop("_tokens", None)     # token_utils caches counts per message
            LEAD["replaced"] = True
    return placed


def refs_of(expanded, files):
    """The refs a message attached: the headers in `files`, kept only when the typed
    text names them, so a header-like line inside a file's content (a file quoting
    `[notes.md: 1 lines]`) is not taken for an attachment. Callers go through the
    three-tuple `expand_file_refs`, which tests stand in for, so the refs are
    recovered here rather than carried from the expansion."""
    parsed = refs_from_text(files)
    if not isinstance(expanded, str) or not isinstance(files, str):
        return parsed
    tail = "\n\n" + files.rstrip("\n")
    body = expanded.rstrip("\n")
    if not body.endswith(tail):
        return parsed
    typed = set(FILE_REF.findall(body[:-len(tail)]))
    kept = [r for r in parsed if r in typed]
    return kept or parsed


def absorb(conversation_history, initial_files, expanded, files, log, refs=None):
    """A message's files and the lead, together. `update` decides the lead; when
    the message re-read a lead file, the fresh text goes to the lead's place in the
    history and the message keeps the note (plus any other files it named), so the
    lead is never in the window twice. `refs` are the refs the expansion attached.
    Returns (initial_files, message_content)."""
    if not files:
        return initial_files, expanded
    refs = list(refs) if refs else refs_of(expanded, files)
    before = initial_files
    after = update(initial_files, files, log, refs=refs)
    if before is None or after is before:
        return after, expanded            # first files (this message is the place) or lead kept
    others = [r for r in refs if not any(same_path(r, lead) for lead in LEAD["refs"])]
    if not refs or not place(conversation_history, after, log, old=before):
        log.info("lead place not found in the history; %s stays in its message", ", ".join(refs))
        return after, expanded
    typed = expanded[:-len("\n\n" + files)] if expanded.endswith("\n\n" + files) else expanded
    body = typed + "\n\n" + note()
    if others:
        extra, _, err = attach_file_refs(others, announce=False)
        if err or not extra:
            return after, expanded        # keep everything rather than lose a file
        body += "\n\n" + extra
    log.info("lead files re-read into their place: %s", ", ".join(LEAD["refs"]))
    return after, body


def reload(initial_files, log):
    """On `-c`, re-read the lead files from disk; the checkpoint's copy is the fallback."""
    if not initial_files or not isinstance(initial_files, str):
        return initial_files
    refs = LEAD["refs"] or refs_from_text(initial_files)
    if not refs:
        return initial_files
    try:
        fresh, attached, err = attach_file_refs(refs)
    except Exception as e:                      # a reload never breaks a resume
        log.warning("lead files not re-read on resume (%s); the checkpoint's copy is used", e)
        return initial_files
    if err or not fresh:
        log.warning("lead files not re-read on resume (%s); the checkpoint's copy is used",
                    err or "nothing attached")
        return initial_files
    LEAD["refs"] = list(attached)
    log.info("lead files re-read from disk on resume: %s%s", ", ".join(attached),
             "" if fresh == initial_files else " (changed since the checkpoint)")
    return fresh
