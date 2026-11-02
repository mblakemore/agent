"""make_plan — scaffold or validate an agent-executable project plan.

See docs/plan-playbook.md for the full methodology behind these checks.
"""

import os
import re
from datetime import datetime
from pathlib import Path

# ── Required sections every PLAN.md must have ───────────────────────────────
# Each entry: (key, regex to match heading, display name)
_REQUIRED_SECTIONS = [
    ("acceptance_criteria", r"##[^#].*acceptance\s+criteria",       "Acceptance Criteria"),
    ("tech_stack",          r"##[^#].*tech(nology)?\s*(stack)?",    "Tech Stack"),
    ("constraints",         r"##[^#].*technical\s*constraints?",    "Technical Constraints"),
    ("file_list",           r"##[^#].*file\s*(list|manifest|map)?", "File List"),
    ("impl_order",          r"##[^#].*impl(ementation)?\s*order",   "Implementation Order"),
    ("verification",        r"##[^#].*verif(ication|y|ied)",        "Verification Protocol / Checklists"),
    ("test_protocol",       r"##[^#].*test(ing)?\s*(protocol|plan|rounds?)?", "Test Protocol"),
]

# ── Weak checklist patterns ──────────────────────────────────────────────────
# Items that describe an action rather than an outcome — agent can tick ✅ on
# wrong code.
_WEAK_CHECKLIST_RE = [
    (
        r"calls?\s+`[^`]+\(\)`\s*$",
        "describes a function call with no result check — specify what the return value is assigned to",
    ),
    (
        r"^imports?\s+\w+",
        "describes an import statement — specify the exact names used and where",
    ),
    (
        r"\buses?\s+(?:correct|proper|right|appropriate|valid)\s+\w+",
        "vague qualifier — replace with the exact class, function, or value name",
    ),
    (
        r"^\s*[-*]\s+(?:has|have|contains?|includes?)\s+[\w\s]+$",
        "too vague — describe the exact value, pattern, or code snippet",
    ),
    (
        r"\bhandles?\s+\w+\s+correctly\b",
        '"correctly" is not verifiable — describe what correct looks like',
    ),
]

# ── Scaffold template ────────────────────────────────────────────────────────
_TEMPLATE = """\
# {title}

> Scaffolded {date} by make_plan. Fill in every `<PLACEHOLDER>`.
> Run `make_plan(action="validate")` when done to check readiness.
> See `docs/plan-playbook.md` for guidance on each section.

---

## Overview

<PLACEHOLDER: 2-3 sentences. What are you building and why?>

---

## Acceptance Criteria

Observable, testable outcomes — what "done" looks like from the outside.
Not implementation details.

- [ ] <PLACEHOLDER: e.g. "Player renders with toon shading in a dungeon room">
- [ ] <PLACEHOLDER: e.g. "WASD moves player; camera follows">
- [ ] <PLACEHOLDER: e.g. "Collecting all crystals shows a win screen">

---

## Tech Stack

Pin exact versions. No ranges.

| Technology | Version | Notes |
|---|---|---|
{tech_rows}

---

## Technical Constraints

Non-negotiable rules. Each constraint MUST include:
- Imperative language: MUST / MUST NOT
- The exact WRONG pattern (what the agent might write)
- The exact RIGHT pattern (what it must write)
- Why the wrong pattern fails

### Constraint 1: <PLACEHOLDER name>

**MUST:** <PLACEHOLDER exact requirement>

```
// WRONG — <PLACEHOLDER: why this fails / what breaks>
<PLACEHOLDER wrong code>

// RIGHT
<PLACEHOLDER correct code>
```

**Why:** <PLACEHOLDER: consequence of violation>

### Constraint 2: <PLACEHOLDER name>

<PLACEHOLDER: same format as above>

---

## File List

Every file the agent will create. For each:
- One sentence on what it does
- What it exports (exact names)
- What it imports from (exact module paths)

Mark integration files (files that import from 3+ project modules) with ★.

| File | Purpose | Exports | Imports From |
|---|---|---|---|
| `<PLACEHOLDER>` | <PLACEHOLDER> | `<PLACEHOLDER>` | `<PLACEHOLDER>` |
| `<PLACEHOLDER>` ★ | Integration — wires everything | — | all of the above |

---

## Implementation Order

Derived from the import graph. A file must not be written before its dependencies.
Integration files (★) are always last.

1. `<PLACEHOLDER>` — no dependencies
2. `<PLACEHOLDER>` — depends on (1)
3. `<PLACEHOLDER>` ★ — integration file, depends on all above

---

## Per-File Verification Protocol

After writing each file, read it back and verify every item on its checklist
BEFORE moving to the next file.

Checklist items must be outcome-focused: they describe a verifiable result,
not just an action. Use exact code snippets.

### Checklist: `<PLACEHOLDER filename>`

- [ ] <PLACEHOLDER: e.g. "Exports exactly: `createFoo`, `FOO_IDX`">
- [ ] <PLACEHOLDER: e.g. "`const flat = geo.toNonIndexed()` — return value ASSIGNED, not discarded">
- [ ] <PLACEHOLDER: e.g. "No `setIndex()` call after non-indexed merge">

### Checklist: `<PLACEHOLDER integration filename>` ★

> Integration files must always cover:
> - All imports at module top level (NOT inside functions)
> - Resources created once at init, NOT per-frame
> - All event listeners explicitly registered
> - Input/event clearing called LAST in the update loop

- [ ] All `import` statements are at module top level — none inside functions or callbacks
- [ ] `<PLACEHOLDER resource>` created once at init — NOT inside the update/render loop
- [ ] `<PLACEHOLDER event system>` explicitly attached: `<PLACEHOLDER exact call>`
- [ ] clearJustPressed / equivalent called LAST — WRONG: `input.clearJustPressed()` before `player.update(dt, input)`; RIGHT: `input.clearJustPressed()` after all `.wasJustPressed()` / `.isHeld()` reads, last line before `renderer.render()`
- [ ] No object added to scene more than once
- [ ] Run `check_file(path="<this file>")` — all checks must PASS before proceeding

---

## Smoke Test Scripts

Minimal Node.js scripts that test geometry/logic modules without a browser.
Run with `node tests/_test_<name>.mjs` or via `check_file()` (auto-discovered).

### `tests/_test_<PLACEHOLDER module>.mjs`

```js
// Smoke test for <PLACEHOLDER module> — no browser, no Three.js CDN needed
// Tests pure data logic: vertex counts, array shapes, attribute integrity

// <PLACEHOLDER: import or inline the function under test>

// Example: test geometry merge output
const vertexCount = <PLACEHOLDER>;
console.assert(posArr.length === vertexCount * 3,
  `position: expected ${vertexCount * 3}, got ${posArr.length}`);
console.assert(siArr.length === vertexCount * 2,
  `skinIndex: expected ${vertexCount * 2}, got ${siArr.length}`);
console.assert(swArr.length === vertexCount * 4,
  `skinWeight: expected ${vertexCount * 4}, got ${swArr.length}`);

console.log('✅ <PLACEHOLDER module> smoke test passed');
```

---

## Test Protocol

How to verify the build works end-to-end once execution completes.

### Verify Server (do this BEFORE browser_check)

```bash
curl -s http://localhost:<PORT>/ | grep '<PLACEHOLDER: entry script name, e.g. src/game.js>'
```

If the entry script name doesn't appear, you're hitting the wrong server (stale process,
wrong port, or wrong directory). Fix the server before running browser_check.

### Initial Render Check
- Start the dev server: `python3 -m http.server <PORT> --bind 0.0.0.0`
- Run the server verify curl above
- Open browser console — expect zero errors
- <PLACEHOLDER: describe what should be visible on first load>

### Functional Tests
- [ ] <PLACEHOLDER: e.g. "WASD moves player">
- [ ] <PLACEHOLDER: e.g. "Golems chase and attack">
- [ ] <PLACEHOLDER: e.g. "Crystal collection increments score">

### Regression Checks
- <PLACEHOLDER: what must NOT break>

---

## Known Risks

Things likely to go wrong and how to catch them early.

| Risk | Signal | Mitigation |
|---|---|---|
| <PLACEHOLDER> | <PLACEHOLDER: error or symptom> | <PLACEHOLDER: intercept or fix> |

"""


# ── Validation helpers ───────────────────────────────────────────────────────

def _check_sections(content: str) -> list[dict]:
    results = []
    for key, pattern, label in _REQUIRED_SECTIONS:
        found = bool(re.search(pattern, content, re.IGNORECASE | re.MULTILINE))
        results.append({"key": key, "label": label, "present": found})
    return results


def _section_body(content: str, heading_pattern: str) -> str | None:
    """Extract the body of a ## section, stopping at the next ## (not ###) heading."""
    m = re.search(
        r"^##[^#][^\n]*" + heading_pattern + r"[^\n]*(?P<body>.*?)(?=\n+##[^#]|\Z)",
        content, re.IGNORECASE | re.DOTALL | re.MULTILINE
    )
    return m.group("body") if m else None


def _check_constraints(content: str) -> list[str]:
    """Warn if the Technical Constraints section lacks wrong/right examples."""
    warnings = []
    body = _section_body(content, r"technical\s*constraints?")
    if body is None:
        return []
    if not re.search(r"\bWRONG\b", body):
        warnings.append(
            "Technical Constraints has no WRONG examples — add bad-pattern "
            "code blocks so the agent knows what to avoid"
        )
    if not re.search(r"\bMUST\b|\bMUST NOT\b", body):
        warnings.append(
            "Technical Constraints uses no MUST/MUST NOT language — use "
            "imperative language for non-negotiable rules"
        )
    return warnings


def _check_checklists(content: str) -> list[str]:
    """Flag checklist items with weak (action-focused) wording."""
    warnings = []
    items = re.findall(r"^\s*[-*]\s*\[[ x]\]\s*(.+)$", content, re.MULTILINE)
    for item in items:
        for pattern, advice in _WEAK_CHECKLIST_RE:
            if re.search(pattern, item, re.IGNORECASE):
                short = item[:70] + ("…" if len(item) > 70 else "")
                warnings.append(f'Weak checklist item: "{short}" — {advice}')
                break
    return warnings


def _check_integration(content: str) -> list[str]:
    """Warn if integration files are not identified or their checklist is thin."""
    warnings = []
    lower = content.lower()
    if "integration" not in lower:
        warnings.append(
            "No integration file identified — mark the wiring/entry-point file "
            "and give it the most detailed checklist (see playbook §Integration Files)"
        )
        return warnings
    # Check for the four key integration checklist items
    checks = [
        ("top level",       "integration checklist should verify all imports are at module top level, not inside functions"),
        ("init",            "integration checklist should verify resources are created once at init, not per-frame"),
        ("attach",          "integration checklist should verify event listeners are explicitly registered"),
        ("clearjustpressed","integration checklist should verify input clearing happens LAST in the update loop"),
    ]
    for phrase, advice in checks:
        if phrase not in lower:
            warnings.append(f"Missing integration check: {advice}")
    return warnings


def _check_impl_order(content: str) -> list[str]:
    """Warn if implementation order doesn't put integration files last."""
    warnings = []
    body = _section_body(content, r"impl(ementation)?\s*order")
    if body is None:
        return []
    if "integration" not in body.lower() and "last" not in body.lower():
        warnings.append(
            "Implementation order doesn't explicitly place integration files last — "
            "they must be written after all their dependencies"
        )
    return warnings


def _check_clearjustpressed_example(content: str) -> list[str]:
    """Warn if clearJustPressed is mentioned in a checklist without a WRONG/RIGHT example."""
    warnings = []
    lower = content.lower()
    if "clearjustpressed" not in lower:
        return []
    # Check that a wrong/right example exists near the clearJustPressed mention
    # Search within ±300 chars of the first mention
    idx = lower.find("clearjustpressed")
    context = content[max(0, idx - 300):idx + 300].lower()
    if "wrong" not in context and "before" not in context and "last" not in context:
        warnings.append(
            "clearJustPressed appears in the plan but has no WRONG/RIGHT example or "
            "'LAST' ordering cue — add: WRONG: called before player.update(); "
            "RIGHT: called after all input reads, last line of update loop"
        )
    return warnings


def _check_server_verification(content: str) -> list[str]:
    """Warn if the Test Protocol has no step to verify the server is serving the right game."""
    warnings = []
    body = _section_body(content, r"test(ing)?\s*(protocol|plan|rounds?)?")
    if body is None:
        return []
    lower = body.lower()
    if "curl" not in lower and "verify" not in lower and "grep" not in lower:
        warnings.append(
            "Test Protocol has no server verification step — add a curl check before "
            "browser_check to confirm you're hitting the right game: "
            "`curl -s http://localhost:PORT/ | grep <entry-script>` "
            "(prevents testing a stale or wrong server on the same port)"
        )
    return warnings


def _check_smoke_tests(content: str) -> list[str]:
    """Recommend a Smoke Test Scripts section for projects with geometry/logic modules."""
    lower = content.lower()
    has_geometry = any(w in lower for w in ("skinnedmesh", "geometry", "buffergeometry", "skeleton"))
    has_smoke = "smoke test" in lower or "_test_" in lower or "tests/" in lower
    if has_geometry and not has_smoke:
        return [
            "Recommended: no Smoke Test Scripts section — for projects with geometry or "
            "skinning modules, add minimal Node.js tests (tests/_test_<name>.mjs) that "
            "verify vertex counts and attribute shapes without a browser. "
            "Run with check_file() which auto-discovers them."
        ]
    return []


# ── Scaffold ─────────────────────────────────────────────────────────────────

def _scaffold(idea: str, tech_stack: str, path: str, write: bool) -> str:
    title = (idea[:80].strip() if idea else "New Project") or "New Project"
    date = datetime.now().strftime("%Y-%m-%d")

    if tech_stack:
        rows = []
        for item in tech_stack.split(","):
            item = item.strip()
            # Try to split "Name vX.Y" or "Name X.Y"
            parts = re.split(r"\s+(?=v?\d)", item, maxsplit=1)
            if len(parts) == 2:
                rows.append(f"| {parts[0]} | {parts[1]} |  |")
            else:
                rows.append(f"| {item} | <version> |  |")
        tech_rows = "\n".join(rows)
    else:
        tech_rows = "| <PLACEHOLDER> | <version> |  |"

    content = _TEMPLATE.format(title=title, date=date, tech_rows=tech_rows)

    if write:
        out = Path(path)
        if out.exists():
            return (
                f"Error: {path} already exists. Delete it or choose a different path."
            )
        out.write_text(content, encoding="utf-8")
        return (
            f"Scaffolded {path} ({len(content)} chars). "
            "Fill in all <PLACEHOLDER> sections, then run "
            'make_plan(action="validate") before executing.'
        )
    return (
        f"PLAN.md template ({len(content)} chars). "
        "Pass write=True to write it to disk, or copy the content below.\n\n"
        + content
    )


# ── Validate ─────────────────────────────────────────────────────────────────

def _validate(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return (
            f"Error: {path} not found. "
            'Use make_plan(action="scaffold") to create a new plan.'
        )

    content = p.read_text(encoding="utf-8", errors="replace")
    lines = [f"## Validating {path}\n"]

    # ── Section presence ────────────────────────────────────────────────────
    sections = _check_sections(content)
    missing = [s for s in sections if not s["present"]]
    lines.append("### Required Sections")
    for s in sections:
        mark = "✅" if s["present"] else "❌"
        lines.append(f"  {mark} {s['label']}")
    if missing:
        lines.append(
            f"\n  ⚠️  {len(missing)} section(s) missing: "
            + ", ".join(s["label"] for s in missing)
        )
    else:
        lines.append("\n  ✅ All required sections present.")

    # ── Quality warnings ────────────────────────────────────────────────────
    warnings = (
        _check_constraints(content)
        + _check_checklists(content)
        + _check_integration(content)
        + _check_impl_order(content)
        + _check_clearjustpressed_example(content)
        + _check_server_verification(content)
        + _check_smoke_tests(content)
    )

    lines.append("\n### Quality Checks")
    if warnings:
        for w in warnings:
            lines.append(f"  ⚠️  {w}")
    else:
        lines.append("  ✅ No quality warnings.")

    # ── Readiness verdict ───────────────────────────────────────────────────
    lines.append("\n### Readiness")
    if not missing and not warnings:
        lines.append(
            "  ✅ READY — plan meets all playbook criteria. "
            "Proceed to execute."
        )
    elif not missing and len(warnings) <= 2:
        lines.append(
            f"  ⚠️  MOSTLY READY — {len(warnings)} minor issue(s) above. "
            "Address for best results before executing."
        )
    elif missing:
        lines.append(
            f"  ❌ NOT READY — {len(missing)} required section(s) missing. "
            "Add them before executing."
        )
    else:
        lines.append(
            f"  ⚠️  NEEDS WORK — {len(warnings)} quality issue(s). "
            "A plan with these issues will produce more agent errors."
        )

    return "\n".join(lines)


# ── Tool entry point ─────────────────────────────────────────────────────────

definition = {
    "type": "function",
    "function": {
        "name": "make_plan",
        "description": (
            "Scaffold a new agent-executable PLAN.md from an idea, or validate "
            "an existing PLAN.md against the planning playbook before executing it. "
            "See docs/plan-playbook.md for the full methodology. "
            "Use action='scaffold' when starting a new project to get a structured "
            "template with all required sections. "
            "Use action='validate' to check an existing plan for missing sections "
            "and weak checklist items before beginning execution."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["scaffold", "validate"],
                    "description": (
                        "'scaffold': generate a PLAN.md template from an idea. "
                        "'validate': check an existing plan against playbook criteria."
                    ),
                },
                "idea": {
                    "type": "string",
                    "description": (
                        "scaffold only — natural-language description of what to build. "
                        "Used as the plan title and overview placeholder."
                    ),
                },
                "tech_stack": {
                    "type": "string",
                    "description": (
                        "scaffold only — comma-separated technologies with versions, "
                        "e.g. 'Three.js r160, Vite 5.2, ES modules'. "
                        "Pre-populates the Tech Stack table."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path to read (validate) or write (scaffold) the plan. "
                        "Defaults to 'PLAN.md' in the current directory."
                    ),
                },
                "write": {
                    "type": "boolean",
                    "description": (
                        "scaffold only — if true, write the template to disk at 'path'. "
                        "If false (default), return the template as text so you can review "
                        "or modify it before writing."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


def fn(
    action: str,
    idea: str = "",
    tech_stack: str = "",
    path: str = "PLAN.md",
    write: bool = False,
) -> str:
    if action == "scaffold":
        return _scaffold(idea, tech_stack, path, write)
    if action == "validate":
        return _validate(path)
    return f"Error: unknown action '{action}'. Use 'scaffold' or 'validate'."

# Registry hints (tools/__init__.py): catalog phrase, other harnesses' names, search words.
_short = 'scaffold or validate a PLAN.md'
_keywords = ['plan', 'roadmap']
