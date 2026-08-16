"""
ui — the shared component layer between theme.py (colors) and the
front-ends (callbacks, slash commands, the /setup and /agent wizards).

Every visual convention in plan/tui-design-system.md § 2 exists here as a
function, so the standard is enforced by the call, not by review: semantic
tokens only (never pigments), the four-state status model with glyph +
word + color, the glyph set, the 2-space indent scale, and the prompt
contract. Pure presentation: renderers return strings, interactors take
injectable ``input_fn``/``print_fn`` (the same testability contract the
wizards already use — scripted answers drive them). Leaf module: imports
theme and the standard library only, so every front-end can import it
without cycles.

Two inherited rules:
  * No raw ANSI literals — every escape routes through theme, so NO_COLOR
    and piped output degrade to plain text everywhere for free.
  * ui never calls on_notice. Components return strings or drive their own
    injected I/O; notice levels stay literal at their call sites (the
    callbacks dispatch-arm AST guard rejects dynamic level arguments).

``ask``/``menu`` are borrow-mode compatible by construction: they hand the
full question to ``input_fn`` and never pre-print it, so both plain
``input`` and commands._borrow_input (which prints the prompt as its own
line before reading) render correctly.
"""

from __future__ import annotations

import shutil
import sys

import theme

# Width of the banner/section rule, matching the engine's session banner.
RULE_WIDTH = 60

# ── Status model (§ 2.2) ────────────────────────────────────────────────
# Four states, each carrying glyph + color; the WORD (OK / FAILED /
# UNMEASURED / free text) is the caller's, colored via status_word().
# Color is never the only carrier: the glyphs differ, so meaning survives
# NO_COLOR and colorblindness.

_STATES = {
    "ok":      ("●", theme.GOOD),
    "warn":    ("⚠", theme.WARN),
    "err":     ("✗", theme.ERR),
    "unknown": ("○", None),      # None = dim, not a pigment
}


def mark(state):
    """The bare status glyph, colored: ``mark("ok")`` → mint ``●``."""
    glyph, color = _STATES[state]
    return theme.c(color, glyph) if color else theme.dim(glyph)


def status_word(state, word):
    """A status word colored by state: ``status_word("err", "FAILED")``."""
    color = _STATES[state][1]
    return theme.c(color, word) if color else theme.dim(word)


def status_row(state, label, detail=""):
    """One health row, banner-style: ``● main   [llamacpp]  model-id``.

    Glyph and label take the state's color together (matching the session
    banner's established ``● main`` rendering); detail stays plain.
    """
    glyph, color = _STATES[state]
    head = f"{glyph} {label}"
    head = theme.c(color, head) if color else theme.dim(head)
    return head + (f" {detail}" if detail else "")


# ── Structure (§ 2.4) ───────────────────────────────────────────────────


def banner(mode=None, version="", sha=""):
    """The program banner: chrome rule / heading / chrome rule.

    ``mode`` is the dim ``·  setup`` / ``·  scaffold`` suffix wizards use;
    version/sha are passed in (ui never imports the engine to find them).
    """
    bar = theme.c(theme.CHROME, "─" * RULE_WIDTH)
    title = theme.c(theme.HEADING, "agent", bold=True)
    if version:
        title += f" v{version}"
    if sha:
        title += theme.dim(f" ({sha})")
    if mode:
        title += theme.dim(f"  ·  {mode}")
    return f"{bar}\n{title}\n{bar}"


def section(label, rest=""):
    """A section header: blank line, then ``── LABEL ──`` (HEADING bold)."""
    return "\n── " + theme.c(theme.HEADING, label, bold=True) + rest + " ──"


def bullet(text, level=1):
    """An unordered bullet on the 2-space scale: ``  • text``."""
    return "  " * level + "• " + text


def fit(line):
    """Middle-truncate a line to the terminal width on a TTY.

    Piped / NO_COLOR output is returned untouched — log files want the
    full line. (Same contract as TerminalCallbacks._fit_line.)
    """
    if theme._no_color():
        return line
    cols = shutil.get_terminal_size((80, 24)).columns
    return theme.truncate_middle(line, cols - 1)


# ── Prompts (§ 2.5) ─────────────────────────────────────────────────────


def ask(prompt, default=None, input_fn=input):
    """One question: ``prompt [default]: `` — ENTER takes the default.

    A None/"" default renders without brackets and returns "" on ENTER.
    """
    shown = (f"{prompt} [{default}]: " if default not in (None, "")
             else f"{prompt}: ")
    ans = input_fn(shown).strip()
    return ans or (default if default is not None else "")


def ask_yn(prompt, default=True, input_fn=input):
    """A yes/no question: ``prompt (y/n) [y]: `` → bool.

    The one place ``startswith("y")`` parsing lives.
    """
    ans = ask(f"{prompt} (y/n)", "y" if default else "n", input_fn)
    return ans.lower().startswith("y")


def numbered(options, current=None, level=1):
    """Render menu rows: ``N) option``, the current value marked ``● ``.

    ``options`` are display strings; ``current`` matches by equality.
    Returns a list of lines (callers print, so menus compose with any
    print_fn).
    """
    pad = "  " * level
    lines = []
    for i, opt in enumerate(options, 1):
        marker = (theme.c(theme.GOOD, "● ")
                  if current is not None and opt == current else "  ")
        lines.append(f"{pad}{marker}{i}) {opt}")
    return lines


def menu(prompt, options, default=None, current=None,
         input_fn=input, print_fn=print, level=1):
    """Options first (indented rows), question last, on its own line.

    A number picks that option; any other text passes through verbatim
    (the typed-URL behavior of the wizards' endpoint pickers,
    generalized); ENTER takes ``default``. Returns the chosen string.
    """
    for line in numbered(options, current=current, level=level):
        print_fn(line)
    ans = ask(prompt, default, input_fn)
    if ans.isdigit() and 1 <= int(ans) <= len(options):
        return options[int(ans) - 1]
    return ans


# ── stderr warnings (§ 2.6) ─────────────────────────────────────────────


def warn_stderr(msg):
    """``⚠ msg`` to stderr, WARN-colored when color is on AND stderr is
    a TTY.

    The one replacement for the raw ``print(f"⚠ …", file=sys.stderr)``
    pattern. Deliberately conservative: theme.c's internal gate keys off
    stdout, so a piped stdout also mutes stderr color (a redirected
    stderr never sees escapes; a stderr-TTY-with-piped-stdout run loses
    color it could technically have — plain text in a log beats escapes
    in one).
    """
    text = f"⚠ {msg}"
    try:
        tty = sys.stderr.isatty()
    except Exception:
        tty = False
    if tty and not theme._no_color():
        text = theme.c(theme.WARN, text)
    print(text, file=sys.stderr, flush=True)
