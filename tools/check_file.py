"""check_file — syntax-check, anti-pattern scan, and smoke test a source file.

Call this after writing every critical source file, before moving to the next.
Fix all failures before proceeding.

See docs/plan-playbook.md §Per-File Verification Protocol for the full workflow.
"""

import re
import subprocess
from pathlib import Path

# ── Anti-pattern checks ──────────────────────────────────────────────────────

def _check_syntax(path: str) -> tuple[bool, str]:
    """node --check for JS/MJS files. Returns (passed, message)."""
    if Path(path).suffix.lower() not in ('.js', '.mjs', '.cjs'):
        return True, f"syntax check skipped (not a JS file)"
    try:
        r = subprocess.run(
            ['node', '--check', path],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return True, "node --check: OK"
        err = (r.stderr or r.stdout).strip()
        return False, f"node --check FAILED:\n{err}"
    except FileNotFoundError:
        return True, "node not in PATH — syntax check skipped"
    except subprocess.TimeoutExpired:
        return False, "node --check timed out"


def _check_toNonIndexed(lines: list[str]) -> list[str]:
    """Warn when toNonIndexed() return value is discarded."""
    warnings = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        if '.toNonIndexed()' not in line:
            continue
        before = line.split('.toNonIndexed()')[0]
        if '=' not in before:
            warnings.append(
                f"line {i}: toNonIndexed() return value discarded — "
                "WRONG: `geo.toNonIndexed();`  RIGHT: `const flat = geo.toNonIndexed();`"
            )
    return warnings


def _check_import_in_block(lines: list[str]) -> list[str]:
    """Warn on import statements with leading whitespace (likely inside a function)."""
    warnings = []
    for i, line in enumerate(lines, 1):
        if re.match(r'^[ \t]+import\s+', line) and not re.match(r'^\s*//', line):
            warnings.append(
                f"line {i}: import statement has leading indent — likely inside a function "
                "or block. Move all static imports to module top level."
            )
    return warnings


def _check_standard_material_on_skinned(src: str) -> list[str]:
    """Warn if SkinnedMesh and MeshStandardMaterial appear in the same file."""
    if 'SkinnedMesh' in src and 'MeshStandardMaterial' in src:
        return [
            "whole file: SkinnedMesh + MeshStandardMaterial detected — "
            "MUST use MeshToonMaterial for skinned meshes in Three.js r160; "
            "MeshStandardMaterial silently breaks skinning (vertex explosion)"
        ]
    return []


def _check_mixer_in_loop(lines: list[str]) -> list[str]:
    """Warn if AnimationMixer is constructed inside what looks like a loop or per-frame callback."""
    warnings = []
    loop_depth = 0
    in_animate = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Heuristic: track animate/update/loop function entry
        if re.search(r'function\s+(animate|update|render|loop|tick)\s*\(', line):
            in_animate = True
        if in_animate and 'new THREE.AnimationMixer' in line:
            warnings.append(
                f"line {i}: AnimationMixer constructed inside what appears to be a "
                "per-frame function — create ONE mixer per character at init, not each frame"
            )
    return warnings


def _check_set_index_on_merged(lines: list[str]) -> list[str]:
    """Warn if setIndex() is called on a geometry that was built by merging non-indexed parts."""
    # Look for setIndex() after any toNonIndexed call in the same file
    src = '\n'.join(lines)
    if 'toNonIndexed' in src and re.search(r'\.setIndex\s*\(', src):
        # Find the line
        for i, line in enumerate(lines, 1):
            if re.search(r'\.setIndex\s*\(', line) and not line.strip().startswith('//'):
                return [
                    f"line {i}: setIndex() called in a file that also uses toNonIndexed() — "
                    "non-indexed merged geometry must NOT call setIndex()"
                ]
    return []


def _check_clear_just_pressed_order(lines: list[str]) -> list[str]:
    """Warn if clearJustPressed() fires before the last input-read in the same file."""
    clear_lines, read_lines = [], []
    for i, line in enumerate(lines, 1):
        if line.strip().startswith('//'):
            continue
        if re.search(r'clearJustPressed\s*\(', line):
            clear_lines.append(i)
        if re.search(r'wasJustPressed\s*\(|justPressed\s*\(|isHeld\s*\(|isDown\s*\(', line):
            read_lines.append(i)
    if clear_lines and read_lines:
        first_clear = min(clear_lines)
        last_read  = max(read_lines)
        if first_clear < last_read:
            return [
                f"line {first_clear}: clearJustPressed() fires before the last input read "
                f"(wasJustPressed/isHeld at line {last_read}) — "
                "MUST call clearJustPressed() AFTER all input reads, at the very end of the update loop"
            ]
    return []


def _check_skeleton_bind_order(src: str) -> list[str]:
    """Warn if the four-step skeleton binding sequence is out of order or incomplete.
    Only runs on files that actually construct a THREE.Skeleton."""
    if 'new THREE.Skeleton(' not in src and 'new Skeleton(' not in src:
        return []
    steps = [
        ('mesh.add(bones',          'mesh.add(bones[0])'),
        ('updateWorldMatrix',        'mesh.updateWorldMatrix(true, true)'),
        ('calculateInverses',        'skeleton.calculateInverses()'),
        ('mesh.bind(',               'mesh.bind(skeleton)'),
    ]
    positions = []
    for token, label in steps:
        idx = src.find(token)
        positions.append((idx, label))

    missing = [label for idx, label in positions if idx == -1]
    if missing:
        return [f"whole file: skeleton binding incomplete — missing: {', '.join(missing)}"]

    ordered = sorted(positions, key=lambda x: x[0])
    expected = [label for _, label in steps]
    actual   = [label for _, label in ordered]
    if actual != expected:
        return [
            f"whole file: skeleton binding steps are out of order — "
            f"required order: {' → '.join(expected)}"
        ]
    return []


# ── Smoke test runner ────────────────────────────────────────────────────────

def _run_smoke_test(smoke_path: str) -> tuple[bool, str]:
    p = Path(smoke_path)
    if not p.exists():
        return False, f"smoke test not found: {smoke_path}"
    try:
        r = subprocess.run(
            ['node', smoke_path],
            capture_output=True, text=True, timeout=20,
            cwd=str(p.parent)
        )
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0:
            return True, f"PASSED\n{out}" if out else "PASSED"
        return False, f"FAILED (exit {r.returncode})\n{out}"
    except FileNotFoundError:
        return False, "node not in PATH — cannot run smoke test"
    except subprocess.TimeoutExpired:
        return False, "timed out after 20s"


# ── Auto-discover smoke test ─────────────────────────────────────────────────

def _find_smoke_test(file_path: str) -> str | None:
    """Look for tests/_test_<stem>.mjs next to the file."""
    p = Path(file_path)
    candidates = [
        p.parent / 'tests' / f'_test_{p.stem}.mjs',
        p.parent / f'_test_{p.stem}.mjs',
        p.parent.parent / 'tests' / f'_test_{p.stem}.mjs',
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# ── Main fn ──────────────────────────────────────────────────────────────────

def fn(path: str, smoke_test: str = "") -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: {path} not found."

    src   = p.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    out   = [f"## check_file: {path}\n"]
    fail  = False

    # ── 1. Syntax ─────────────────────────────────────────────────────────────
    ok, msg = _check_syntax(path)
    out.append(f"### Syntax\n  {'✅' if ok else '❌'} {msg}\n")
    if not ok:
        fail = True

    # ── 2. Anti-patterns ──────────────────────────────────────────────────────
    warnings = (
        _check_toNonIndexed(lines)
        + _check_import_in_block(lines)
        + _check_standard_material_on_skinned(src)
        + _check_mixer_in_loop(lines)
        + _check_set_index_on_merged(lines)
        + _check_clear_just_pressed_order(lines)
        + _check_skeleton_bind_order(src)
    )
    out.append("### Anti-patterns")
    if warnings:
        fail = True
        for w in warnings:
            out.append(f"  ⚠️  {w}")
    else:
        out.append("  ✅ No anti-patterns detected.")
    out.append("")

    # ── 3. Smoke test ─────────────────────────────────────────────────────────
    smoke_path = smoke_test or _find_smoke_test(path)
    if smoke_path:
        ok, msg = _run_smoke_test(smoke_path)
        out.append(f"### Smoke test ({Path(smoke_path).name})")
        out.append(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fail = True
    else:
        out.append("### Smoke test\n  — none found (optional)\n")

    # ── 4. Summary ────────────────────────────────────────────────────────────
    out.append("### Summary")
    if fail:
        out.append("  ❌ FAIL — fix the issues above before writing the next file.")
    else:
        out.append("  ✅ PASS — file looks correct. Proceed to the next file.")

    return "\n".join(out)


definition = {
    "type": "function",
    "function": {
        "name": "check_file",
        "description": (
            "Verify a source file immediately after writing it. Runs: "
            "(1) node --check syntax validation for JS/MJS files, "
            "(2) anti-pattern scan — toNonIndexed return discarded, import inside "
            "function/block, MeshStandardMaterial on SkinnedMesh, AnimationMixer "
            "per-frame, setIndex on non-indexed geometry, clearJustPressed before "
            "input reads, skeleton binding out of order, "
            "(3) auto-discovers and runs a smoke test if tests/_test_<name>.mjs exists. "
            "Call this after writing every critical file. Fix all failures before "
            "writing the next file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the source file to check.",
                },
                "smoke_test": {
                    "type": "string",
                    "description": (
                        "Optional explicit path to a Node.js smoke test script. "
                        "If omitted, check_file auto-searches for tests/_test_<stem>.mjs."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}

# Registry hints (tools/__init__.py): catalog phrase, other harnesses' names, search words.
_short = 'syntax-check a file you wrote'
_keywords = ['syntax', 'lint', 'compile', 'verify']
