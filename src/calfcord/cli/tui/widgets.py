"""The six prompt shapes, rendered with Rich and driven by readchar.

Each widget is split in two: a ``*_panel`` function that is a pure function of
state (so what the operator sees is directly assertable), and the widget itself,
which owns the key loop. ``read`` is the single input seam — inject a scripted
callable and the whole surface tests without a TTY.

Nothing here starts an event loop. That is the point: InquirerPy's ``.execute()``
drives prompt_toolkit's ``Application``, which calls ``asyncio.run`` internally
and therefore explodes when a prompt is reached from inside an existing loop —
a crash this project actually shipped. A blocking readchar call has no such
constraint, so these are safe to call from anywhere.

Live rendering is transient (``transient=True``): the widget is erased once
answered and replaced by a one-line record, so scrollback reads as a transcript
of decisions rather than a graveyard of dead UI.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from calfcord.cli._prompts import Choice
from calfcord.cli.tui import render, theme
from calfcord.cli.tui.keys import Key, read_key, resolve
from calfcord.cli.tui.state import CheckboxState, ListState, SelectState

Reader = Callable[[], str]


def _panel(message: str, body: RenderableType, hint: str) -> Panel:
    """The shared frame: the question as the title, the hint in the bottom border."""
    return Panel(
        body,
        title=Text(message, style=theme.TITLE),
        title_align="left",
        subtitle=Text(hint, style=theme.MUTED),
        subtitle_align="left",
        border_style=theme.BORDER,
        box=theme.BOX,
        padding=(0, 1),
    )


# What a list panel costs beyond its rows: two borders, the two scroll markers,
# and a line of slack so the frame does not sit flush against the terminal edge.
_PANEL_OVERHEAD = 5


def viewport_for(console: Console | None) -> int:
    """How many rows this terminal can show without the panel being cropped.

    Measured rather than assumed. A fixed row count is wrong in both directions:
    it crops in a short tmux pane — the exact failure the viewport exists to
    prevent, since Live clips at ``vertical_overflow="ellipsis"`` — and wastes
    most of a tall terminal.

    Floors at 1: a list with no visible rows cannot be answered at all, so a
    pathologically short terminal gets a cramped prompt rather than an impossible
    one.
    """
    height = render.target(console).size.height
    return max(1, height - _PANEL_OVERHEAD)


def _more(count: int, arrow: str) -> Text:
    """A dim "<n> more" marker, or a blank line holding that row's height.

    Blank rather than omitted when the count is zero: dropping the line would
    change the panel's height as the cursor reaches either end, making the whole
    frame jump. A steady frame is half the point of the viewport.
    """
    return Text(f"  {arrow} {count} more" if count else "", style=theme.MUTED)


def _rows(state: ListState, marker: Callable[[Choice], str]) -> Group:
    """Render the visible window of the list, dimming all but the cursor row.

    Only ``state.window()`` is painted, because Live CROPS anything taller than
    the terminal: ``vertical_overflow`` defaults to ``"ellipsis"``, so a 60-row
    list on a 24-line terminal paints roughly the first 22 rows, a centred
    ``...``, and nothing else — no bottom border, no hint, and no cursor at all
    once it moves past the fold. The operator is left navigating a list they
    cannot see.

    Do NOT "verify" that default by reading ``live.vertical_overflow`` after the
    ``with`` block: ``Live.stop`` sets it to ``"visible"`` for the final frame, so
    a probe that reads it afterwards reports the wrong value. An earlier revision
    of this comment claimed "visible" for exactly that reason and was wrong.

    The viewport is what keeps a long ``agent tools`` list usable on a 24-line
    terminal — InquirerPy paged its lists, so losing this would be a regression.
    """
    start, stop = state.window()
    lines: list[Text] = []

    # An honest count of what is off-screen. Without it a scrolled list is
    # indistinguishable from a complete one, and an operator would never learn
    # that the rows they want exist at all.
    if state.scrolled:
        lines.append(_more(start, "↑"))

    for index in range(start, stop):
        choice = state.choices[index]
        active = index == state.cursor
        # The row's base style carries the emphasis; appended spans inherit it.
        # Do NOT restyle the label here — style="" does not RESET a dim base, it
        # inherits it, so it would read as an undim that silently is not one.
        text = Text(f"{theme.POINTER if active else ' '} ", style=theme.ACCENT if active else theme.MUTED)
        mark = marker(choice)
        if mark:
            text.append(f"{mark} ")
        text.append(choice.label)
        lines.append(text)

    if state.scrolled:
        lines.append(_more(len(state.choices) - stop, "↓"))
    return Group(*lines)


def select_panel(message: str, state: SelectState) -> Panel:
    return _panel(message, _rows(state, lambda _c: ""), theme.HINT_SELECT)


def checkbox_panel(message: str, state: CheckboxState, *, instruction: str = "") -> Panel:
    """The multi-select frame.

    ``instruction`` is caller-supplied guidance rendered above the rows. It is
    rendered rather than dropped because the Protocol declares it: a widget that
    accepts a parameter and silently ignores it lies to the next caller, who
    passes guidance and has no way to learn it went nowhere. The key mechanics
    are NOT its job — the hint in the bottom border states those for every list.
    """
    rows = _rows(state, lambda c: theme.CHECK_ON if state.is_checked(c.value) else theme.CHECK_OFF)
    body = Group(Text(instruction, style=theme.MUTED), rows) if instruction else rows
    return _panel(message, body, theme.HINT_CHECKBOX)


def _field_panel(message: str, shown: str, *, placeholder: str = "") -> Panel:
    """A single-line entry frame. Every field shares one hint, so it is not a param."""
    body = Text(shown, style=theme.ACCENT) if shown else Text(placeholder, style=theme.MUTED)
    return _panel(message, body, theme.HINT_TEXT)


def text_panel(message: str, typed: str, *, default: str = "") -> Panel:
    return _field_panel(message, typed, placeholder=default)


def secret_panel(message: str, typed: str) -> Panel:
    """Paint the *length* of the secret, never the secret."""
    return _field_panel(message, "•" * len(typed), placeholder="skip to keep current")


def confirm_panel(message: str, *, default: bool) -> Panel:
    body = Text("Y/n" if default else "y/N", style=theme.ACCENT)
    return _panel(message, body, theme.HINT_TEXT)


def _live(build: Callable[[], RenderableType], console: Console | None) -> Live:
    """A transient Live over the target console.

    Transient so the widget is erased once answered and replaced by a one-line
    record. A non-terminal console (tests, a piped run) renders nothing at all,
    which is why no test-only quiet switch is needed here.
    """
    return Live(build(), console=render.target(console), transient=True, auto_refresh=False)


def _loop(
    build: Callable[[], RenderableType],
    step: Callable[[Key | None, str], bool],
    *,
    read: Reader,
    console: Console | None,
) -> None:
    """Drive one widget: paint, read a key, apply it, repeat until ``step`` says stop.

    KeyboardInterrupt from ``read`` is deliberately not caught — readchar raises
    it on Ctrl-C and the CLI entry point maps it to a clean "aborted." exit 130,
    which is the resumable-abort contract ``init`` teaches.
    """
    with _live(build, console) as live:
        while True:
            raw = read()
            key = resolve(raw)
            if key is Key.EOF:
                # Ctrl-D: no answer is coming. main() turns this into the
                # "needs an interactive terminal" message rather than a traceback.
                raise EOFError
            if step(key, raw):
                return
            live.update(build(), refresh=True)


def _navigate(state: ListState, key: Key | None) -> bool:
    """Apply the navigation both list widgets share; True when Enter confirms."""
    if key is Key.UP:
        state.up()
    elif key is Key.DOWN:
        state.down()
    return key is Key.ENTER


def select(
    message: str,
    choices: list[Choice],
    *,
    default: str | None = None,
    read: Reader = read_key,
    console: Console | None = None,
) -> str:
    state = SelectState(choices, default=default, viewport=viewport_for(console))

    def step(key: Key | None, _raw: str) -> bool:
        return _navigate(state, key)

    _loop(lambda: select_panel(message, state), step, read=read, console=console)
    render.answer(message, state.choices[state.cursor].label, console=console)
    return state.value


def checkbox(
    message: str,
    choices: list[Choice],
    *,
    instruction: str = "",
    read: Reader = read_key,
    console: Console | None = None,
) -> list[str]:
    state = CheckboxState(choices, viewport=viewport_for(console))

    def step(key: Key | None, raw: str) -> bool:
        if raw == " ":
            # Matched raw rather than bound as a Key: space is printable text
            # everywhere else, and only this widget reads it as a command. Same
            # shape as ``confirm`` matching y/n. Space resolves to no Key, so
            # _navigate below is a no-op on this press.
            state.toggle()
        return _navigate(state, key)

    _loop(
        lambda: checkbox_panel(message, state, instruction=instruction),
        step,
        read=read,
        console=console,
    )
    render.answer(message, f"{len(state.selected)} selected", console=console)
    return state.selected


def _typed_field(
    build: Callable[[str], RenderableType],
    *,
    read: Reader,
    console: Console | None,
) -> str:
    """The shared editing loop behind :func:`text` and :func:`secret`."""
    buffer: list[str] = []

    def step(key: Key | None, raw: str) -> bool:
        if key is Key.ENTER:
            return True
        if key is Key.BACKSPACE:
            if buffer:
                buffer.pop()
        elif key is None and raw.isprintable():
            # ``resolve`` returning None means "not a control key"; anything
            # printable is literal input. Unprintable leftovers (stray escape
            # sequences, unbound control codes) are ignored rather than injected
            # into the value.
            buffer.append(raw)
        return False

    _loop(lambda: build("".join(buffer)), step, read=read, console=console)
    return "".join(buffer)


def text(
    message: str,
    *,
    default: str = "",
    read: Reader = read_key,
    console: Console | None = None,
) -> str:
    typed = _typed_field(
        lambda shown: text_panel(message, shown, default=default),
        read=read,
        console=console,
    )
    # Enter on an untouched field accepts the suggestion — the press-Enter-to-keep
    # contract every pre-filled prompt in the wizard depends on.
    value = typed or default
    render.answer(message, value, console=console)
    return value


def secret(
    message: str,
    *,
    read: Reader = read_key,
    console: Console | None = None,
) -> str:
    typed = _typed_field(
        lambda shown: secret_panel(message, shown),
        read=read,
        console=console,
    )
    # "" means the operator skipped: callers read that as keep-what-is-stored, so
    # it must never be coerced to a default here.
    render.answer(message, "•" * len(typed) if typed else "kept current", console=console)
    return typed


def confirm(
    message: str,
    *,
    default: bool = False,
    read: Reader = read_key,
    console: Console | None = None,
) -> bool:
    answer = default

    def step(key: Key | None, raw: str) -> bool:
        nonlocal answer
        if key is Key.ENTER:
            return True
        if raw.lower() in ("y", "n"):
            answer = raw.lower() == "y"
            return True
        # Anything else is ignored: a stray keypress must never read as consent.
        return False

    _loop(lambda: confirm_panel(message, default=default), step, read=read, console=console)
    render.answer(message, "yes" if answer else "no", console=console)
    return answer
