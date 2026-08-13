"""
Aurora theme — colors, gradients, and text helpers for terminal UI.

Palette is a violet → sky → mint aurora with amber/rose accents for
warnings and errors. All color output honors NO_COLOR and falls back
to 256-color or plain text when truecolor isn't available.
"""

import math
import os
import re
import sys

# ── Palette (24-bit RGB) ────────────────────────────────────────────────
VIOLET = (123, 77, 255)
SKY    = (53, 194, 245)
MINT   = (95, 255, 176)
AMBER  = (255, 190, 61)
ROSE   = (255, 77, 109)

# Gradient stops used by pulse animations (violet → sky → mint and back)
_GRADIENT = [VIOLET, SKY, MINT]

def _no_color():
    """Return True when color output should be suppressed."""
    if os.environ.get("NO_COLOR"):
        return True
    if not sys.stdout.isatty():
        return True
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return True
    return False


# Style constants — empty when color is suppressed at import time.
# Runtime color checks still happen via _no_color() for per-call paths.
if _no_color():
    RESET = ""
    BOLD  = ""
    DIM   = ""
else:
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"

# Terminal control sequences. Suppressed when piping or NO_COLOR, same as
# color escapes — noisy escape output in a log file is never what you want.
CLEAR_LINE = "" if _no_color() else "\r\033[K"


# ── Width-safe line helpers ─────────────────────────────────────────────
# CSI sequences (SGR colors, cursor moves, clears) and OSC sequences
# (terminal title). Both render zero-width, so display-width math must
# ignore them.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"            # CSI … final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC … BEL or ST
)


def strip_ansi(text):
    """Return text with ANSI CSI/OSC escape sequences removed."""
    return _ANSI_RE.sub("", text)


def visible_len(text):
    """Display width of text: length with zero-width escapes stripped."""
    return len(strip_ansi(text))


def truncate_middle(text, width, marker="…"):
    """Middle-truncate text to at most `width` visible columns.

    Keeps the head and tail, drops the middle — a too-long tool line stays
    readable at both ends instead of hard-wrapping. ANSI escapes are
    preserved (they cost zero columns); a RESET is inserted before the
    marker so head styling can't bleed into it. Styling opened in the
    dropped middle is lost for the tail — acceptable for status lines,
    which are styled at the edges, not the middle.

    Returns text unchanged when it already fits.
    """
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    mlen = len(marker)
    if width <= mlen:
        return marker[:width]
    keep = width - mlen
    head_n = (keep + 1) // 2
    tail_n = keep - head_n

    # Tokenize into escape sequences (zero-width) and single characters.
    tokens = []
    pos = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            tokens.extend(text[pos:m.start()])
        tokens.append(m.group())
        pos = m.end()
    tokens.extend(text[pos:])

    def _is_visible(tok):
        return len(tok) == 1 and tok != "\x1b"

    head, count, i = [], 0, 0
    while i < len(tokens) and count < head_n:
        head.append(tokens[i])
        if _is_visible(tokens[i]):
            count += 1
        i += 1

    tail, count, j = [], 0, len(tokens) - 1
    while j >= i and count < tail_n:
        tail.append(tokens[j])
        if _is_visible(tokens[j]):
            count += 1
        j -= 1
    tail.reverse()

    return "".join(head) + RESET + marker + "".join(tail)


def cursor_up_clear(n):
    """ANSI escape: move cursor up n lines and clear from there to end of screen.

    Returns "" when piping or NO_COLOR is set, so log files don't end up with
    ANSI garbage. n must be > 0 to emit anything.
    """
    if n <= 0 or _no_color():
        return ""
    return f"\033[{n}A\033[0J"


def _truecolor():
    """Return True if the terminal advertises 24-bit color support."""
    ct = os.environ.get("COLORTERM", "").lower()
    return ct in ("truecolor", "24bit")


# Minimal 256-color fallback table for the palette above.
# Indices were picked to approximate each RGB within the xterm-256 cube.
_FALLBACK_256 = {
    VIOLET: 99,   # violet
    SKY:    81,   # sky blue
    MINT:   84,   # mint green
    AMBER:  214,  # amber
    ROSE:   203,  # rose/salmon
}


def _fallback_index(rgb):
    """Approximate an arbitrary RGB triple to a 256-color cube index."""
    if rgb in _FALLBACK_256:
        return _FALLBACK_256[rgb]
    r, g, b = rgb
    # 6x6x6 cube: 16 + 36*r + 6*g + b, each channel 0–5
    def _q(v):
        return max(0, min(5, round(v / 255 * 5)))
    return 16 + 36 * _q(r) + 6 * _q(g) + _q(b)


def escape(rgb, bold=False):
    """Return the SGR escape sequence that sets foreground to rgb."""
    if _no_color():
        return ""
    prefix = BOLD if bold else ""
    if _truecolor():
        r, g, b = rgb
        return f"{prefix}\033[38;2;{r};{g};{b}m"
    return f"{prefix}\033[38;5;{_fallback_index(rgb)}m"


def c(rgb, text, bold=False):
    """Wrap text in an SGR sequence for rgb (plain text if NO_COLOR)."""
    if _no_color():
        return text
    return f"{escape(rgb, bold=bold)}{text}{RESET}"


def dim(text):
    """Wrap text in the DIM SGR sequence (plain text if NO_COLOR)."""
    if _no_color():
        return text
    return f"{DIM}{text}{RESET}"


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_rgb(c1, c2, t):
    return (
        int(round(_lerp(c1[0], c2[0], t))),
        int(round(_lerp(c1[1], c2[1], t))),
        int(round(_lerp(c1[2], c2[2], t))),
    )


def pulse_rgb(t):
    """Return an RGB triple on the aurora gradient for cycle position t (seconds)."""
    # 3-second cycle sweeping violet → sky → mint → sky → violet
    phase = (math.sin(t * (2 * math.pi / 3.0)) + 1.0) / 2.0  # 0..1
    # Map phase onto the gradient stops
    if phase <= 0.5:
        return _lerp_rgb(_GRADIENT[0], _GRADIENT[1], phase * 2.0)
    return _lerp_rgb(_GRADIENT[1], _GRADIENT[2], (phase - 0.5) * 2.0)


def pulse_escape(t):
    """Return an SGR escape for the aurora gradient at cycle position t."""
    return escape(pulse_rgb(t))


def bar(pct, width=30):
    """Render a solid aurora gradient progress bar of the given width.

    pct is clamped to [0, 1]. Each filled cell picks a color along the
    gradient so the bar itself reads as an aurora sweep.
    """
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    if _no_color():
        return "[" + "#" * filled + "-" * (width - filled) + "]"
    cells = []
    for i in range(width):
        if i < filled:
            stop = i / max(1, width - 1)
            rgb = _lerp_rgb(_GRADIENT[0], _GRADIENT[2], stop)
            cells.append(f"{escape(rgb)}█{RESET}")
        else:
            cells.append(f"{DIM}·{RESET}")
    return "[" + "".join(cells) + "]"
