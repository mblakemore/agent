"""Execute shell commands via subprocess.

Each command runs in a fresh bash shell rooted at the agent's working
directory (os.getcwd()). Compound commands like 'cd ../e1 && git log'
work within a single call but do NOT affect future calls — every
invocation starts from the agent's home directory.

Sessions only matter for background processes — they track the Popen
handle and accumulated output so the agent can poll later.
"""

import atexit
import hashlib
import os
import re
import secrets
import subprocess
import threading
from pathlib import Path

from .file import _accessed_files


def _extract_write_target(command):
    """Extract target file path from a shell write command, or None if not a write."""
    # Heredoc: cat > file.ext <<'EOF'  or  cat > file.ext << EOF
    m = re.search(r'>\s*(\S+\.(?:py|json|md|txt|sh|yaml|yml|toml|cfg|jsonl))\b.*<<', command)
    if m:
        return m.group(1)
    # Redirect: echo/printf/cat ... > file.ext  (but not 2> or >>)
    m = re.match(r'^\s*(?:cat|echo|printf)\s+.*?[^2]>\s*(\S+)', command)
    if m:
        target = m.group(1)
        # Skip things that look like /dev/null or pipes
        if not target.startswith('/dev/'):
            return target
    return None


# Max temporary sessions per agent
_MAX_TEMP_SESSIONS = 4

# Background sessions: {session_id: {"bg_proc": Popen|None, "bg_output": str}}
_sessions = {}
_main_session_id = None
_temp_session_ids = []


def _derive_main_session():
    """Derive a stable main session name from the agent's working directory."""
    cwd = os.getcwd()
    agent_name = os.path.basename(cwd)
    path_hash = hashlib.md5(cwd.encode()).hexdigest()[:6]
    return f"agent-{agent_name}-{path_hash}"


def _get_or_create_session(session_id=None, new_session=False):
    """Get an existing session or create a new one. Returns (session_id, error)."""
    global _main_session_id

    if session_id:
        if session_id not in _sessions:
            return None, f"Error: session '{session_id}' does not exist"
        return session_id, None

    if new_session:
        # Clean up finished temp sessions
        _temp_session_ids[:] = [s for s in _temp_session_ids if s in _sessions]
        if len(_temp_session_ids) >= _MAX_TEMP_SESSIONS:
            return None, (
                f"Error: temporary session limit reached ({_MAX_TEMP_SESSIONS}). "
                f"Active temp sessions: {', '.join(_temp_session_ids)}. "
                f"Use an existing session_id or wait for one to be cleaned up."
            )
        sid = f"agent-tmp-{secrets.token_hex(4)}"
        _sessions[sid] = {"bg_proc": None, "bg_output": ""}
        _temp_session_ids.append(sid)
        return sid, None

    # Main session
    if _main_session_id and _main_session_id in _sessions:
        return _main_session_id, None

    sid = _derive_main_session()
    _sessions[sid] = {"bg_proc": None, "bg_output": ""}
    _main_session_id = sid
    return sid, None


def _read_bg_output(proc, session):
    """Background thread: read process output incrementally."""
    parts = []
    try:
        for line in proc.stdout:
            parts.append(line)
            session["bg_output"] = "".join(parts)
        proc.wait()
    except Exception as e:
        parts.append(f"\nError reading output: {e}\n")
    session["bg_output"] = "".join(parts)


def cleanup_temp_sessions():
    """Kill all temporary sessions and their background processes."""
    for sid in _temp_session_ids[:]:
        session = _sessions.pop(sid, None)
        if session and session.get("bg_proc"):
            try:
                session["bg_proc"].kill()
            except Exception:
                pass
    _temp_session_ids.clear()


# Clean up if the process exits unexpectedly
atexit.register(cleanup_temp_sessions)


def fn(command: str = "", session_id: str = "", timeout: float = 120,
       background: bool = False, new_session: bool = False) -> str:
    """Execute a shell command in the agent's working directory.

    Args:
        command: Shell command to execute. If empty, checks on a background session.
        session_id: Existing session to reuse (only for background process polling).
        timeout: Max seconds to wait (default 120). LLM-calling scripts may need 300+.
        background: If true, start the command and return immediately.
        new_session: If true, create a new temporary session for parallel work.
    """
    if not command and not session_id:
        return "Error: at least one of 'command' or 'session_id' is required"

    sid, err = _get_or_create_session(session_id, new_session)
    if err:
        return err
    session = _sessions[sid]

    # ── Polling (no command) ──────────────────────────────────────────
    if not command:
        bg = session.get("bg_proc")
        if bg:
            output = session.get("bg_output", "")
            if bg.poll() is not None:
                rc = bg.returncode
                session["bg_proc"] = None
                return f"[session: {sid}] exit={rc} (background process finished)\n{output}"
            else:
                # Show tail of output so far
                tail = output[-4000:] if len(output) > 4000 else output
                return f"[session: {sid}] (still running)\n{tail}"
        return f"[session: {sid}] (idle)"

    # ── Guards ────────────────────────────────────────────────────────

    # Every command runs from the agent's home directory
    home_cwd = os.getcwd()

    # Block cd to paths outside the repo tree.
    # Relative cd (cd ../shared && ...) is fine — only block absolute paths and ~ expansion
    # that leave the repo.
    cd_match = re.match(r'^cd\s+(\S+)\s*&&\s*(.+)', command)
    if cd_match:
        target_dir = cd_match.group(1)
        # Expand ~ so we can check the resolved path
        expanded = os.path.expanduser(target_dir)
        # Only check absolute paths (relative ones are fine — they stay in the repo)
        if os.path.isabs(expanded):
            # Resolve both to absolute, real paths to handle symlinks and avoid prefix bugs
            real_target = os.path.realpath(expanded)
            repo_root = os.path.realpath(os.path.dirname(home_cwd))
            
            # Check if real_target is inside repo_root
            try:
                is_inside = os.path.commonpath([repo_root, real_target]) == repo_root
            except ValueError: 
                # This can happen if paths are on different drives (Windows)
                is_inside = False

            if not is_inside:
                return (
                    f"Error: You are trying to cd to '{target_dir}' which is outside "
                    f"your repo tree ('{repo_root}'). Your working directory is "
                    f"'{home_cwd}'. Use relative paths — the session is already in "
                    f"the correct directory."
                )

    # Worktree path guard: ensure worktrees are created in WORKTREE_ROOT
    # Anchored to shell separators so heredoc bodies/quoted strings don't false-positive.
    wt_match = re.search(r'(?:^|&&\s*|;\s*|\|\|?\s*)git\s+worktree\s+add\s+(\S+)', command)
    if wt_match:
        wt_root = os.environ.get("WORKTREE_ROOT")
        if wt_root:
            wt_path = wt_match.group(1)
            if not wt_path.startswith(wt_root):
                return (
                    f"ERROR: Worktree must be created under {wt_root}, not {wt_path}. "
                    f"Use: git worktree add {wt_root}/<branch-slug> -b <branch-name>"
                )

    # Pre-merge validation: ensure PR has a valid linked issue (CICD mode)
    if os.environ.get("CICD_MODE"):
        merge_match = re.search(r'gh\s+pr\s+merge\s+(\d+)', command)
        if merge_match:
            import subprocess as _sp
            pr_num = merge_match.group(1)
            check_cmd = f"gh pr view {pr_num} --json body --jq '.body'"
            check_result = _sp.run(
                check_cmd, shell=True, capture_output=True, text=True, cwd=home_cwd
            )
            body = check_result.stdout.strip()
            closes_match = re.search(r'Closes\s+#(\d+)', body)
            if not closes_match:
                return (
                    f"BLOCKED: PR #{pr_num} body does not contain 'Closes #N' "
                    f"with a valid issue number. "
                    f"Per decision matrix, CICD PRs without a linked issue "
                    f"must be CLOSE'd, not merged."
                )
            issue_num = closes_match.group(1)
            verify_cmd = f"gh issue view {issue_num} --json number,state"
            verify_result = _sp.run(
                verify_cmd, shell=True, capture_output=True, text=True, cwd=home_cwd
            )
            if verify_result.returncode != 0:
                return (
                    f"BLOCKED: PR #{pr_num} references issue #{issue_num} "
                    f"but that issue does not exist or cannot be read. "
                    f"Per decision matrix, CICD PRs without a valid linked "
                    f"issue must be CLOSE'd."
                )

    # Shell write guard: if writing to an existing file that hasn't been
    # read or written yet this session, require a read first.
    write_target = _extract_write_target(command)
    if write_target:
        target_path = Path(write_target)
        if not target_path.is_absolute():
            target_path = Path(home_cwd) / target_path
        try:
            resolved = str(target_path.resolve())
        except (OSError, ValueError):
            resolved = str(target_path)
        if target_path.exists() and resolved not in _accessed_files:
            return (
                f"'{write_target}' exists but you haven't read it this session. "
                f"Read the file first (file tool or cat) to verify your changes "
                f"won't overwrite important content, then retry."
            )
        # Track the write so subsequent commands know it's been touched
        _accessed_files.add(resolved)

    # ── Background execution ──────────────────────────────────────────
    if background:
        try:
            proc = subprocess.Popen(
                ['bash', '-c', f'{command} 2>&1'],
                cwd=home_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as e:
            return f"Error starting background command: {e}"
        session["bg_proc"] = proc
        session["bg_output"] = ""
        t = threading.Thread(target=_read_bg_output, args=(proc, session), daemon=True)
        t.start()
        return f"[session: {sid}]\nCommand started in background. Poll with session_id to check output."

    # ── Foreground execution ──────────────────────────────────────────
    try:
        result = subprocess.run(
            ['bash', '-c', f'{command} 2>&1'],
            cwd=home_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            f"[session: {sid}] (timed out after {timeout}s)\n"
            f"The command is no longer running. Try a shorter operation or "
            f"use background=true for long-running commands."
        )
    except Exception as e:
        return f"Error running command: {e}"

    output = result.stdout.rstrip('\n')

    # Post-write sanitizer: strip trailing EOF/heredoc junk from written files.
    # Qwen generates heredocs like: cat > file << 'EOF'\n...\nEOF 2>&1
    # The "EOF 2>&1" sometimes ends up as literal file content.
    if result.returncode == 0 and write_target:
        try:
            wt = Path(write_target)
            if not wt.is_absolute():
                wt = Path(home_cwd) / wt
            if wt.exists() and wt.is_file():
                text = wt.read_text(encoding='utf-8', errors='replace')
                # Strip trailing heredoc terminators: EOF, EOF 2>&1, JSONEOF 2>&1, etc.
                cleaned = re.sub(r'\n\s*(?:JSON|YAML|SH|PY)?EOF(?:\s+2>&1)?\s*$', '\n', text)
                if cleaned != text:
                    wt.write_text(cleaned, encoding='utf-8')
        except Exception:
            pass

    # Track shell reads (cat/head/tail <file>) so they count toward access
    if result.returncode == 0:
        read_match = re.match(r'^\s*(?:cat|head|tail|less|more)\s+(\S+)', command)
        if read_match:
            read_target = read_match.group(1)
            if not read_target.startswith('-'):  # skip flags like -n
                rp = Path(read_target)
                if not rp.is_absolute():
                    rp = Path(home_cwd) / rp
                try:
                    _accessed_files.add(str(rp.resolve()))
                except (OSError, ValueError):
                    pass

    return f"[session: {sid}] exit={result.returncode}\n{output}"


definition = {
    "type": "function",
    "function": {
        "name": "exec_command",
        "description": (
            "Execute a shell command. "
            "Every command starts from the agent's home directory — "
            "use 'cd ../e1 && cmd' for one-off commands in other directories. "
            "Set new_session=true to create a temporary session for background work "
            "(e.g., running a server). "
            "Temp sessions are cleaned up at end of cycle. "
            "Commands that call the LLM or do heavy computation may take minutes — "
            "the default timeout is 120s. For long-running commands, use "
            "background=true and poll with session_id to check output. "
            "You can write files via shell (cat >, heredocs). "
            "For existing files, you must read them first (cat or file tool) in this session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute. If empty, checks on a background session.",
                    "default": "",
                },
                "session_id": {
                    "type": "string",
                    "description": "Existing session ID for polling a background process.",
                    "default": "",
                },
                "timeout": {
                    "type": "number",
                    "description": "Max seconds to wait (default 120). LLM-calling scripts may need 300+.",
                    "default": 120,
                },
                "background": {
                    "type": "boolean",
                    "description": "If true, start the command and return immediately without waiting. Poll with session_id later.",
                    "default": False,
                },
                "new_session": {
                    "type": "boolean",
                    "description": "If true, create a new temporary session instead of using the main one. Use for parallel tasks like running a server.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}
