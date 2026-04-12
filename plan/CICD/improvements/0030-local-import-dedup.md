# 0030 — local-import-dedup

**Issue**: #64 — bug: agent.py has 4 redundant local imports (re×2, Path×1, hashlib×1) that duplicate or should be module-level
**Branch**: cicd/0030-local-import-dedup (will be created in Phase 6)

## Goal

Remove 4 unnecessary local `import` statements inside function bodies in `agent.py`:
- Lines 1067 and 1696: `import re as _re` shadows the module-level `import re` (line 14)
- Line 1702: `from pathlib import Path as _P` shadows the module-level `from pathlib import Path` (line 22)
- Line 1650: `import hashlib as _hl` — not at module top, re-imported on every text-only turn in the hot loop

## Motivation

`agent.py` already has `import re` at line 14 and `from pathlib import Path` at line 22. Two
function-body sites re-import `re` as the private alias `_re`, and one inner loop imports
`Path` as `_P`. These aliases look intentional but are just noise — the module-level names are
directly available in both scopes.

The `hashlib` case is slightly different: it's not at module top at all, so every text-only
response in the agent loop triggers a local `import hashlib as _hl`. Python caches module
imports, so the cost is a dict lookup rather than a disk read, but the pattern is inconsistent
with the repo's style (all standard-library imports are at the top).

Cycles 0014 removed dead imports from callbacks.py, tools/file.py, agent.py, and
tool_recovery.py. This is the same class of cleanup for three new sites discovered by the
static scan in cycle 0030's PROBE phase.

## Success metric

- **Baseline**: `grep -c 'import re as _re\|import hashlib as _hl\|from pathlib import Path as _P' agent.py` → **4**
- **Target**: → **0**
- **Measurement method**:
  ```bash
  grep -c 'import re as _re\|import hashlib as _hl\|from pathlib import Path as _P' \
    /tmp/agent-cicd/0030-local-import-dedup/agent.py
  ```
- **Secondary**: test suite stays at 152 (no new tests strictly needed, but add a static assertion)

## Scope

- **In**:
  - `agent.py`: 4 targeted line-range edits (one removal at each site + rename of alias)
  - `agent.py` top-level imports: add `import hashlib` in the stdlib block (alphabetically after `json`)
  - `tests/test_no_dead_imports.py`: add a new test class guarding the 4-now-0 pattern
- **Out**:
  - Any change to logic, control flow, or output
  - Any other file

## Implementation steps

### Step 1 — Hoist hashlib to module top (`agent.py` line ~9)

Add `import hashlib` after `import json` in the stdlib imports block:

```python
import json
import hashlib    # ← add here
import logging
```

### Step 2 — Remove `import re as _re` at line 1067 (`_auto_increment_cycle`)

Current (lines 1067-1072):
```python
        import re as _re
        committed_cycles = set()
        for line in result.stdout.strip().split("\n"):
            m = _re.search(r'\bC(\d+):', line)
            if m:
                committed_cycles.add(int(m.group(1)))
```

Replace with:
```python
        committed_cycles = set()
        for line in result.stdout.strip().split("\n"):
            m = re.search(r'\bC(\d+):', line)
            if m:
                committed_cycles.add(int(m.group(1)))
```

(Drop the import line; rename `_re` → `re` in the two uses.)

### Step 3 — Remove `import re as _re` + `from pathlib import Path as _P` at lines 1696/1702 (`run_agent_single`)

Current (lines 1695-1703):
```python
                try:
                    from tools.file import _accessed_files
                    import re as _re
                    _read_claims = _re.findall(
                        r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))',
                        full_content, _re.IGNORECASE
                    )
                    for claimed_file in _read_claims:
                        from pathlib import Path as _P
                        _resolved = str((_P.cwd() / claimed_file).resolve())
                        if _resolved not in _accessed_files:
```

Replace with:
```python
                try:
                    from tools.file import _accessed_files
                    _read_claims = re.findall(
                        r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))',
                        full_content, re.IGNORECASE
                    )
                    for claimed_file in _read_claims:
                        _resolved = str((Path.cwd() / claimed_file).resolve())
                        if _resolved not in _accessed_files:
```

### Step 4 — Remove `import hashlib as _hl` at line 1650 (`run_agent_single` hot path)

Current (lines 1649-1651):
```python
        if full_content:
            import hashlib as _hl
            _text_hash = _hl.md5(full_content.encode()).hexdigest()
```

Replace with:
```python
        if full_content:
            _text_hash = hashlib.md5(full_content.encode()).hexdigest()
```

### Step 5 — Add regression test in `tests/test_no_dead_imports.py`

Add a new test method `test_no_shadowing_local_imports_in_agent_py` to the existing
`TestNoDeadImports` class:

```python
def test_no_shadowing_local_imports_in_agent_py(self):
    """agent.py must not re-import stdlib names already at module top."""
    src = Path(__file__).parent.parent / "agent.py"
    text = src.read_text()
    bad = [
        "import re as _re",
        "import hashlib as _hl",
        "from pathlib import Path as _P",
    ]
    for pattern in bad:
        self.assertNotIn(
            pattern, text,
            f"agent.py still contains redundant local import: {pattern!r}"
        )
```

## Test plan

- Existing 152 tests must stay green
- New test (1): `TestNoDeadImports.test_no_shadowing_local_imports_in_agent_py`
  - Asserts that `import re as _re`, `import hashlib as _hl`, `from pathlib import Path as _P`
    do not appear anywhere in agent.py source

## Risks & mitigations

- **Re-import as alias had purpose**: unlikely — `re` is identical to `_re` in all 5 usages;
  `Path` is identical to `_P`. No shadowing of outer names. Safe substitution.
- **hashlib at module top adds startup cost**: negligible — hashlib is a standard C extension,
  imports in microseconds, already transitively loaded by tools/exec_command.py.
- **try-block exception swallowing**: the `from tools.file import _accessed_files` and the
  hallucination detection are in a try/except block. Removing `import re as _re` inside the
  same try-block cannot affect exception flow — `re` is bound before the try executes.

## Rollback

4 lines deleted / 1 line added — trivial `git revert`.

## Closes

Closes #64
