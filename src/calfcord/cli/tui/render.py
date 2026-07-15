"""Output helpers for the interactive flows.

Two rules make it safe to route existing operator prose through Rich, and it is
worth being precise about which mechanism enforces which:

* **The prose is wrapped in ``Text()`` before printing.** *That* — not
  ``markup=False`` — is what keeps bracketed text and ``$``-sigils literal, because
  Rich parses markup only when converting a ``str`` into a Text. Delete the
  ``Text()`` wrapper and the brackets get eaten as style tags no matter what the
  flags say. ``markup=False``/``highlight=False`` are passed as belt-and-braces so
  the contract survives someone later handing :func:`line` a bare string.
* ``soft_wrap=True`` — disables wrapping and cropping, so a line reaches the
  terminal byte-identical to what ``print`` would have emitted. Rich's default
  is to wrap at the console width (80 off-TTY), which would bisect the phrases
  the CLI's existing tests match on. This one is genuinely load-bearing.

Both are passed **per call**, never set on the console. As constructor defaults
they would apply to every render — including the widgets', where ``soft_wrap``
propagates into a Panel's children and cuts long choice labels at the panel edge
instead of wrapping them. Prose wants no wrapping; a menu row wants wrapping. The
console must not decide for both.

``highlight=False`` is part of the same contract: Rich's automatic highlighter
would otherwise colour numbers, paths, and URLs inside plain sentences.

:func:`answer` and :func:`header` bypass :func:`line` and print directly. They are
safe for a *different* reason — they build ``Text`` objects, which are never
markup-parsed — not because they funnel. A future helper that prints a raw ``str``
must use :func:`line`, or it inherits the markup-eating bug these rules exist to
prevent.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from calfcord.cli.tui import theme

_console: Console | None = None


def make_console(*, width: int | None = None, record: bool = False) -> Console:
    """Build a console. ``record`` + ``width`` are for tests; production uses neither."""
    # soft_wrap is NOT set here. As a constructor default it sets no_wrap +
    # overflow="ignore" for EVERY render, and those options propagate into a
    # Panel's children — so a long choice label is cut at the panel edge rather
    # than wrapping onto a second line, losing the end of the sentence that says
    # what the row is. The prose helpers below pass soft_wrap per call instead,
    # which keeps their byte-identical-to-print contract without imposing it on
    # the widgets.
    return Console(width=width, record=record, highlight=False)


def console() -> Console:
    """The shared stdout console.

    Module-level rather than injected per call site: the flows print from many
    places, and threading a console through every signature would churn the
    command modules this migration is meant to leave alone. Tests capture stdout
    as they already do.
    """
    global _console
    if _console is None:
        _console = make_console()
    return _console


def target(explicit: Console | None) -> Console:
    """Resolve the console to print on — the injected one, else the shared one.

    Public because :mod:`calfcord.cli.tui.widgets` needs the same rule, and two
    modules re-deriving it invites drift: the ``or`` spelling widgets used is
    equivalent only because ``Console`` happens to define no ``__bool__``, which
    is an accident rather than a guarantee.
    """
    return explicit if explicit is not None else console()


def line(text: str = "", *, style: str = "", console: Console | None = None) -> None:
    target(console).print(Text(text, style=style), markup=False, highlight=False, soft_wrap=True)


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
    target(console).print(answer_text(label, value), soft_wrap=True)


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

    # A leading blank line so a header never butts against whatever preceded it
    # (a collapsed answer record, a note). Owned here rather than left to each
    # call site, which would drift.
    out = target(console)
    body = Text(subtitle, style=theme.MUTED) if subtitle else Text("")
    out.print()
    out.print(
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
