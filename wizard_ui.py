"""Radio lists and checkboxes for the setup wizard.

Two implementations of one small interface:

- ``TtyUI``: prompt_toolkit widgets drawn inline (no full-screen dialog, so the
  terminal keeps its scrollback). Arrow keys move, Space ticks a checkbox, Enter
  accepts.
- ``PlainUI``: numbered prompts over ``input_fn`` / ``print_fn``. Used without a
  TTY, without prompt_toolkit, and by the tests, which script the answers.

Both take options as ``[(label, hint), ...]`` and return indexes, so the wizard
never knows which one it is talking to.
"""

from __future__ import annotations

import sys

import theme


class PlainUI:
    def __init__(self, input_fn, print_fn):
        self._input = input_fn
        self.say = print_fn

    @property
    def input_fn(self):
        """For screens still written as plain questions (advisor, agent scaffold)."""
        return self._input

    def radio(self, title, options, default=0):
        self.say(title)
        for i, (label, hint) in enumerate(options, 1):
            mark = "*" if i - 1 == default else " "
            self.say(f"  {mark}{i}) {label}" + (theme.dim(f"   {hint}") if hint else ""))
        for _ in range(3):
            ans = self._input(f"choose 1-{len(options)} [{default + 1}]: ").strip()
            if not ans:
                return default
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return int(ans) - 1
            self.say(f"  enter a number from 1 to {len(options)}")
        return default

    def checkboxes(self, title, options, checked=()):
        checked = set(checked)
        self.say(title)
        for i, (label, hint) in enumerate(options, 1):
            box = "[x]" if i - 1 in checked else "[ ]"
            self.say(f"  {i}) {box} {label}" + (theme.dim(f"   {hint}") if hint else ""))
        ans = self._input("numbers to switch on or off, comma-separated "
                          "(ENTER = as shown): ").strip()
        for part in ans.replace(" ", ",").split(","):
            if part.isdigit() and 1 <= int(part) <= len(options):
                checked ^= {int(part) - 1}
        return checked

    def text(self, prompt, default="", secret=False):
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        ans = self._input(shown).strip()
        return ans or default


class TtyUI(PlainUI):
    """prompt_toolkit front end. Ctrl-C raises KeyboardInterrupt, as input() does."""

    def __init__(self, print_fn=print):
        from prompt_toolkit import prompt as _pt_prompt
        self._pt_prompt = _pt_prompt
        super().__init__(lambda p: _pt_prompt(p), print_fn)

    @staticmethod
    def _fmt(label, hint):
        from prompt_toolkit.formatted_text import FormattedText
        parts = [("", label)]
        if hint:
            parts.append(("fg:ansibrightblack", f"   {hint}"))
        return FormattedText(parts)

    def radio(self, title, options, default=0):
        from prompt_toolkit.shortcuts import choice
        return choice(message=title,
                      options=[(i, self._fmt(label, hint))
                               for i, (label, hint) in enumerate(options)],
                      default=default)

    def checkboxes(self, title, options, checked=()):
        return run_checkbox_app(title, [(i, self._fmt(label, hint))
                                        for i, (label, hint) in enumerate(options)],
                                checked)

    def text(self, prompt, default="", secret=False):
        shown = f"{prompt} [{default}]: " if (default and not secret) else f"{prompt}: "
        ans = self._pt_prompt(shown, is_password=secret).strip()
        return ans or default


def build_checkbox_app(title, values, checked=(), input=None, output=None):
    """Inline checkbox list: arrows move, Space ticks, Enter accepts.

    prompt_toolkit's CheckboxList toggles on Enter as well as Space; here Enter
    must accept, so the list's own Enter binding is dropped. Built separately
    from running it so tests can drive it through a pipe input."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.widgets import CheckboxList, Label

    box = CheckboxList(values=values, default_values=list(checked))
    box.show_scrollbar = False      # the list always fits; the bar read as clutter
    old = box.control.key_bindings
    kb = KeyBindings()
    for b in old.bindings:
        if tuple(b.keys) != (Keys.ControlM,):
            kb.add(*b.keys, filter=b.filter)(b.handler)
    box.control.key_bindings = kb

    app_kb = KeyBindings()

    @app_kb.add("enter")
    def _accept(event):
        event.app.exit(result=set(box.current_values))

    @app_kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    hint = Label(text="  ↑/↓ move · Space ticks · Enter continues",
                 style="fg:ansibrightblack")
    return Application(layout=Layout(HSplit([Label(text=title), box, hint]), focused_element=box),
                       key_bindings=app_kb, full_screen=False, erase_when_done=False,
                       input=input, output=output)


def run_checkbox_app(title, values, checked=()):
    return build_checkbox_app(title, values, checked).run()


def make_ui(input_fn=None, print_fn=None):
    """TtyUI for a real interactive terminal, PlainUI otherwise. Passing an
    ``input_fn`` (tests, scripted callers) always gets PlainUI."""
    print_fn = print_fn or print
    if input_fn is None and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return TtyUI(print_fn)
        except ImportError:
            pass
    return PlainUI(input_fn or input, print_fn)
