"""Output helpers for the interactive flows.

Every helper funnels through :func:`line`, which pins the two rules that make it
safe to route existing operator prose through Rich:

* ``markup=False`` — the prose contains bracketed text and ``$``-sigils that Rich
  would otherwise parse as style tags and delete.
* ``soft_wrap=True`` — disables wrapping and cropping, so a line reaches the
  terminal byte-identical to what ``print`` would have emitted. Rich's default
  is to wrap at the console width (80 off-TTY), which would bisect the phrases
  the CLI's existing tests match on.

``highlight=False`` is part of the same contract: Rich's automatic highlighter
would otherwise colour numbers, paths, and URLs inside plain sentences.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from calfcord.cli.tui import theme

_console: Console | None = None


def make_console(*, width: int | None = None, record: bool = False) -> Console:
    """Build a console. ``record`` + ``width`` are for tests; production uses neither."""
    return Console(width=width, record=record, highlight=False, soft_wrap=True)


def console() -> Console:
    """The shared stdout console.

    Module-level rather than injected per call site: the flows print from ~40
    places, and threading a console through every signature would churn the
    command modules this migration is meant to leave alone. Tests capture stdout
    as they already do.
    """
    global _console
    if _console is None:
        _console = make_console()
    return _console


def _target(explicit: Console | None) -> Console:
    """Resolve the console to print on — the injected one, else the shared one."""
    return explicit if explicit is not None else console()


def line(text: str = "", *, style: str = "", console: Console | None = None) -> None:
    """Print one line verbatim — no markup parsing, no wrapping, no highlighting."""
    _target(console).print(Text(text, style=style), markup=False, highlight=False, soft_wrap=True)


def note(text: str, *, console: Console | None = None) -> None:
    """Secondary detail — present, but visibly subordinate to the prose around it."""
    line(text, style=theme.MUTED, console=console)


def success(text: str, *, console: Console | None = None) -> None:
    line(f"{theme.TICK} {text}", style=theme.ACCENT, console=console)


def error(text: str, *, console: Console | None = None) -> None:
    line(text, style=theme.ERROR, console=console)


def answer_text(label: str, value: str) -> Text:
    """Build the collapsed record: a dim tick and label, then the value, bright.

    Built with **no base style** on purpose. A ``Text``'s base style is inherited
    by every appended span, so a dim base drags the value down with it — the
    value renders bold *and* dimmed however it was appended, and the one thing
    the record exists to show becomes the quietest thing on the line. Each span
    therefore carries its own style and nothing else.
    """
    text = Text()
    text.append(f"{theme.TICK} ", style=theme.MUTED)
    text.append(f"{label}  ", style=theme.MUTED)
    text.append(value, style=theme.ACCENT)
    return text


def answer(label: str, value: str, *, console: Console | None = None) -> None:
    """Print the one-line record a widget collapses to once answered.

    This is what makes the inline model read as a transcript: the live widget is
    torn down and replaced by this, so scrollback shows decisions, not dead UI.
    """
    _target(console).print(answer_text(label, value), soft_wrap=True)


def header(
    title: str,
    *,
    subtitle: str = "",
    step: tuple[int, int] | None = None,
    label: str = "",
    console: Console | None = None,
) -> None:
    """The panel that opens an interactive command.

    ``step`` renders as ``1/4`` in the top border, with ``label`` naming the
    phase — so a long wizard always says where the operator is. Single-shot
    commands pass neither and get a plain titled panel.
    """
    right = ""
    if step is not None:
        current, total = step
        right = f"{current}/{total}"
        if label:
            right = f"{right} {theme.BULLET} {label}"

    body = Text(subtitle, style=theme.MUTED) if subtitle else Text("")
    _target(console).print(
        Panel(
            body,
            title=Text(title, style=theme.TITLE),
            title_align="left",
            subtitle=Text(right, style=theme.MUTED) if right else None,
            subtitle_align="right",
            border_style=theme.BORDER,
            box=theme.BOX,
            padding=(0, 1),
        )
    )
