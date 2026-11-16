"""Tool arguments the model got wrong: the sanitize pass for a parsed dict (the file
tool's action and path, argument names, quoting), the repair of arguments whose JSON
did not parse (json_repair first, then the heuristic salvage for token-leakage shapes),
the control tokens that leak from a broken chat template, and the think-call context
laundering check. Pure functions over the call and the history; no state.
"""
import difflib
import json
import re

try:
    import json_repair as _json_repair
except ImportError:                      # optional: without it the heuristic salvage is the only fallback
    _json_repair = None


FILE_ACTIONS = {"read", "write", "insert", "append", "delete", "list", "edit"}


# Harmony/ChatML/Llama-style control tokens that leak from model output into
# tool args when sampling goes wrong (typically under context pressure with a
# fragile chat template). Observed in a field-observed run — both full `<|tool_call|>`
# form and partial variants like `<tool_call|>`, `<|channel>`, `<|tool_call>`.
# Letting these dispatch is dangerous: one agent's `file({path: "...<|tool_call|>..."})`
# wrote a file with the tokens in its NAME, git committed it, and every cycle
# since re-injects the tokens into context via `ls` (self-amplifying poison).
HARMONY_TOKEN_RE = re.compile(
    r'<\|?(?:tool_call|channel|im_start|im_end|im_sep|message|return'
    r'|end_of_turn|start_of_turn|reasoning|analysis|final|commentary|thought)\|?>',
    re.IGNORECASE,
)


# Minimum substring length to count as "context laundering" — empirically the
# that agent failures used full paragraphs (300+ chars), so 50 is comfortably
# conservative without false-positiving on short overlapping phrases.
THINK_LAUNDER_MIN_LEN = 50


def detect_context_laundering(text, history, lookback=3):
    """Walk the last N assistant messages and check whether `text` re-includes
    a substring of theirs verbatim at >= THINK_LAUNDER_MIN_LEN chars.

    Used to reject `think` calls whose prompt or context paraphrases the
    conversation context (one agent's "I am <agent>, current state: cycle 10..."
    pattern — pays the LLM cost to reason about content already in context).

    Returns (offending_substring, source_msg_index) on hit, None if clean.
    Substring matching is done in normalized form (collapsed whitespace) to
    catch reformatted-but-still-verbatim copies.
    """
    if not text or not history:
        return None

    def _norm(s):
        return re.sub(r'\s+', ' ', s).strip()

    norm_text = _norm(text)
    if len(norm_text) < THINK_LAUNDER_MIN_LEN:
        return None

    # Walk newest → oldest, only consider assistant messages
    seen = 0
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") != "assistant":
            continue
        seen += 1
        if seen > lookback:
            break
        prior = msg.get("content") or ""
        if isinstance(prior, list):
            # Some message formats wrap content in [{type:text,text:...}, ...]
            prior = " ".join(
                c.get("text", "") for c in prior if isinstance(c, dict)
            )
        if not isinstance(prior, str) or not prior.strip():
            continue
        norm_prior = _norm(prior)
        # Slide a window of size THINK_LAUNDER_MIN_LEN across norm_text and
        # check for membership in norm_prior. O(len(text) * len(prior)) worst
        # case but bounded by typical sizes; cheap in practice.
        step = max(1, THINK_LAUNDER_MIN_LEN // 4)
        for start in range(0, len(norm_text) - THINK_LAUNDER_MIN_LEN + 1, step):
            window = norm_text[start:start + THINK_LAUNDER_MIN_LEN]
            if window in norm_prior:
                return (window, i)
    return None


def detect_harmony_token(args):
    """Walk tool args recursively. Return (key_path, matched_token) on the first
    Harmony control-token hit, or None if clean. Strings, lists, and nested
    dicts are all scanned."""
    if isinstance(args, str):
        m = HARMONY_TOKEN_RE.search(args)
        if m:
            return ("", m.group(0))
        return None
    if isinstance(args, dict):
        for k, v in args.items():
            r = detect_harmony_token(v)
            if r:
                sub = r[0]
                return (f"{k}.{sub}" if sub else k, r[1])
        return None
    if isinstance(args, list):
        for i, v in enumerate(args):
            r = detect_harmony_token(v)
            if r:
                sub = r[0]
                return (f"[{i}].{sub}" if sub else f"[{i}]", r[1])
        return None
    return None


def harmony_retry_hint(func_name: str) -> str:
    """Return a one-line correct-call example for the given tool name.
    Used in harmony-token rejection messages to guide the model's retry."""
    hints = {
        "task_tracker": "task_tracker(action='done', description='PERCEIVE')",
        "exec_command": "exec_command(command='ls -la')",
        "read_file":    "read_file(path='state/current-state.json')",
        "write_file":   "write_file(path='state/context.json', content='{}')",
        "edit_file":    "edit_file(path='file.py', old_string='x', new_string='y')",
        "append_file":  "append_file(path='logs/activity.log', content='entry\\n')",
        "list_files":   "list_files(path='.')",
        "search_files": "search_files(pattern='TODO', path='.', glob='*.py')",
    }
    return hints.get(func_name, f"{func_name}(...) — use only plain quoted string args.")


def sanitize_tool_args(func_name, args, log):
    """Fix garbled args that parsed as valid JSON but have bogus values.

    Gemma 4 concatenates **,key:value into field values, e.g.:
      {"action": "write**,content:some text"}
      {"action": "write", "path": "foo.json**,start_line:1", "end_line": 14}
    Also catches Gemma 4 escape-token leakage: <|"|> or its decoded form »
    appearing inside string values when the native tool-call JSON encoding
    breaks on complex arguments (e.g. old_string/new_string with inner quotes).
    This extracts embedded params from ALL string fields.
    """
    if func_name != "file" or not isinstance(args, dict):
        return args

    # Check if any string value contains the **,key: or `,key: pattern OR escape-token leakage.
    # Gemma 4 uses ** or a backtick as the delimiter before embedded key:value pairs.
    _GARBLE_PAT = re.compile(r'(?:\*\*|`),\s*(\w+):')
    _ESCAPE_TOKEN = re.compile(r'<\|"\|>|»')  # <|"|> or »
    needs_fix = False
    for v in args.values():
        if isinstance(v, str) and (_GARBLE_PAT.search(v) or _ESCAPE_TOKEN.search(v)):
            needs_fix = True
            break

    action = args.get("action", "")
    if not needs_fix and action in FILE_ACTIONS:
        return args

    log.warning("Sanitizing garbled file args: %s",
                {k: repr(v)[:60] for k, v in args.items()})

    # Collect all key:value pairs from garbled strings across all fields
    extracted = {}
    clean_vals = {}
    for key, val in args.items():
        if not isinstance(val, str):
            extracted[key] = val
            continue
        # Split on **,key: or `,key: boundaries to extract embedded params
        parts = _GARBLE_PAT.split(val)
        # parts[0] is the clean prefix of this field's value
        clean_val = parts[0].rstrip('*`').strip()
        # Strip Gemma 4 escape-token artifacts: <|"|>, », surrounding quotes/backticks
        clean_val = _ESCAPE_TOKEN.sub('', clean_val).strip("'\"`")
        if clean_val:
            clean_vals[key] = clean_val
        # Remaining parts alternate: key_name, value_before_next_split
        for i in range(1, len(parts) - 1, 2):
            embed_key = parts[i]
            embed_val = parts[i + 1].rstrip('*').strip() if i + 1 < len(parts) else ""
            # Split off trailing ", key:..." garbage (e.g. ", content:..." after a path value)
            embed_val = re.split(r',\s*\w+:', embed_val)[0]
            embed_val = _ESCAPE_TOKEN.sub('', embed_val).strip("'\"`")
            if embed_val:
                # Try to parse integers for line numbers
                if embed_key in ("start_line", "end_line"):
                    try:
                        extracted[embed_key] = int(embed_val)
                    except ValueError:
                        extracted[embed_key] = embed_val
                else:
                    extracted[embed_key] = embed_val

    # Build fixed args: extracted embedded params + clean field values
    fixed = {}
    for key, val in clean_vals.items():
        fixed[key] = val
    for key, val in extracted.items():
        if key not in fixed:
            fixed[key] = val

    # Fix action if it was garbled
    if "action" in fixed and fixed["action"] not in FILE_ACTIONS:
        import difflib
        action_lower = str(fixed["action"]).lower()
        # 1. Try substring match (e.g., "read_this_file" -> "read")
        substring_match = next((t for t in FILE_ACTIONS if t in action_lower), None)
        # 2. Try fuzzy match for typos (e.g., "raed" -> "read")
        fuzzy_matches = difflib.get_close_matches(action_lower, FILE_ACTIONS, n=1, cutoff=0.6)
        
        if substring_match:
            fixed["action"] = substring_match
        elif fuzzy_matches:
            fixed["action"] = fuzzy_matches[0]
    log.info("Sanitized args: %s",
             {k: repr(v)[:60] if isinstance(v, str) else v for k, v in fixed.items()})
    return fixed


def repair_tool_args(func_name, raw_args, log):
    """Arguments for a tool call whose JSON did not parse.

    `json_repair` first, for the common malformations (a truncated string, an unbalanced
    brace or quote, a trailing comma, single quotes, text after the object); then the
    heuristic salvage below for the token-leakage shapes a generic repairer does not know.
    A repaired dict goes through the same `sanitize_tool_args` as a parsed one. None when
    neither finds a dict. Never raises.
    """
    repaired = None
    if _json_repair is not None and isinstance(raw_args, str) and raw_args.strip():
        try:
            repaired = _json_repair.repair_json(raw_args, return_objects=True)
        except Exception as e:  # noqa: BLE001 — a repairer bug must not cost the turn
            log.debug("json_repair failed on %s args: %s", func_name, e)
            repaired = None
    if isinstance(repaired, dict) and repaired:
        log.warning("Repaired malformed %s args: %s -> %s", func_name, raw_args[:100], repaired)
        try:
            return sanitize_tool_args(func_name, repaired, log)
        except Exception as e:  # noqa: BLE001
            log.debug("sanitize after repair failed on %s args: %s", func_name, e)
            return repaired
    return salvage_tool_args(func_name, raw_args, log)


def salvage_tool_args(func_name, raw_args, log):
    """Try to extract valid arguments from garbled Gemma 4 tool call output.

    Gemma 4 sometimes concatenates keys into the action field
    (e.g. "write**,content:..." or "read,path:..."). This attempts to
    recover the intended arguments.

    Returns a dict on success, None if unsalvageable.
    """
    try:
        # Strip Gemma 4 thinking blocks and special tokens that leak into args
        cleaned = re.sub(r'<\|channel>.*?<channel\|>', '', raw_args, flags=re.DOTALL)
        cleaned = cleaned.replace('»', '"').replace('<|"|>', '"').replace('<|', '').replace('|>', '')
        # Try parsing again after cleanup
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Pattern: "action**,key:value,key:value" or "action,key:value"
        # Try to extract action and rebuild as JSON
        if func_name == "file":
            for action in ("read", "write", "insert", "append", "delete", "list"):
                if action in raw_args.lower():
                    result = {"action": action}
                    # Try to find path
                    path_match = re.search(r'path["\s:`]+([^\s,}"` ]+)', raw_args)
                    if path_match:
                        result["path"] = path_match.group(1).strip('"\' `')
                    # Try to find content
                    content_match = re.search(r'content["\s:`]+(.+?)(?:,\s*(?:path|start_line|end_line)|$)', raw_args, re.DOTALL)
                    if content_match:
                        result["content"] = content_match.group(1).strip('"\' `')
                    if "path" in result:
                        log.warning("Salvaged garbled tool args: %s → %s", raw_args[:100], result)
                        return result

        # For exec_command, try to find the command string
        if func_name == "exec_command":
            cmd_match = re.search(r'command["\s:]+(.+)', raw_args, re.DOTALL)
            if cmd_match:
                cmd = cmd_match.group(1).strip('"\'').rstrip('}')
                # Issue #1007 Bug 1: the salvage path bypasses json.loads, so JSON
                # string escape sequences (\n, \t, \r, \\, \", \') survive as literal
                # two-char pairs. Bash then sees ``cat > f << 'EOF'\n<!DOCTYPE...``
                # as a single line and never finds the EOF delimiter (exit=127).
                # Unescape via a sentinel for \\ so we don't double-decode \\n.
                _SENT = "\x00"
                cmd = (cmd
                       .replace("\\\\", _SENT)
                       .replace("\\n", "\n")
                       .replace("\\t", "\t")
                       .replace("\\r", "\r")
                       .replace('\\"', '"')
                       .replace("\\'", "'")
                       .replace(_SENT, "\\"))
                log.warning("Salvaged garbled exec_command: %s", cmd[:100])
                return {"command": cmd}

    except Exception as e:
        log.debug("Salvage attempt failed: %s", e)

    return None
