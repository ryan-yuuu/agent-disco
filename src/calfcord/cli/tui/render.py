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


def step(
    label: str,
    value: str,
    *,
    status: theme.Status = "ok",
    width: int = 0,
    console: Console | None = None,
) -> None:
    """Print one completed-step record: ``✓ label  value``.

    Shares :func:`answer`'s hierarchy — quiet glyph and label, bright value — because
    a step and an answer are the same shape of fact: the thing that happened matters,
    the name of the slot it happened in does not. It is a separate helper for two
    reasons. ``answer`` hard-codes a two-space gap, which is correct for a lone record
    collapsing out of a prompt and ragged for a block of them printed together, so
    ``width`` pads the label column and lets the values line up. And ``answer`` is only
    ever ``✓``: a step can fail, and a flow whose contract is "no green light that
    lies" cannot report failure with a tick.

    ``width`` is the label column, and the caller owns it because only the caller knows
    the whole block; ``0`` (the default) means "no padding", for a record printed alone.
    A ``fail`` is styled :data:`theme.ERROR` — the theme reserves its one colour for
    genuine failures, and a step that did not happen is one.
    """
    glyph = theme.STEP_GLYPHS[status]
    text = Text()
    text.append(f"{glyph} ", style=theme.ERROR if status == "fail" else theme.MUTED)
    text.append_text(pair_text(label, value, width=width))
    target(console).print(text, soft_wrap=True)


def pair_text(label: str, value: str, *, width: int = 0) -> Text:
    """Build a ``label  value`` row: quiet label, bright value. No glyph.

    Each span carries its own style and the ``Text`` carries none — a base style is
    inherited by every append and would drag the value dim, making the one thing the
    row exists to show the quietest thing on it. See :func:`answer_text`.
    """
    text = Text()
    text.append(f"{label.ljust(width)}  ", style=theme.MUTED)
    text.append(value, style=theme.ACCENT)
    return text


def pair(label: str, value: str, *, width: int = 0, console: Console | None = None) -> None:
    """Print a label/value row with no outcome glyph.

    :func:`step` minus the mark, for rows that report no outcome — the "what next"
    block a flow signs off with. Sharing the padded two-column shape is what lets that
    block read as the same object as the record board above it, rather than as unrelated
    prose that happens to follow.
    """
    target(console).print(pair_text(label, value, width=width), soft_wrap=True)


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
