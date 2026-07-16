"""The visual language, in one place.

Deliberately **monochrome**: hierarchy comes from weight and dimming, never from
hue. Nothing here names a colour except :data:`ERROR`.

Why no accent colour — including no off-white one. An off-white accent
(``#e6e6e6``) is only "off-white" on a dark terminal; on a light-background
profile it is near-invisible, and Solarized-style themes remap the 16-colour
palette out from under it. Bold-on-default-foreground is the one accent that
renders correctly on every terminal theme, because it asks the terminal for
emphasis instead of dictating a colour. So :data:`ACCENT` carries no hue.

:data:`ERROR` is the single exception: red is a safety signal rather than an
accent, and it marks only genuine failures. It is one constant if we ever want
it gone too.
"""

from __future__ import annotations

from rich import box

# --- styles -----------------------------------------------------------------
ACCENT = "bold"  # the pointer, the row under the cursor, the focused field
MUTED = "dim"  # descriptions, hints, secondary detail
BORDER = "dim"  # panel edges — present, never loud
ERROR = "red"

# "not dim" is load-bearing, not redundant. Rich renders a Panel's title inside
# the border row, so the title inherits ``border_style`` — and BORDER is dim.
# Without the explicit override the question comes out bold-and-dimmed: the most
# important text in the frame, washed out by its own frame. In a monochrome
# design, where weight carries all of the hierarchy, that is a real defect
# rather than a nicety.
TITLE = "bold not dim"

# --- glyphs -----------------------------------------------------------------
# The pointer is a heavy right-pointing angle ORNAMENT, not a plain greater-than.
# Ruff flags it as confusable; the distinction is the point. A greater-than reads
# as a quote marker or a shell prompt, where the ornament reads as a cursor — and
# it is the one glyph the whole selection language rests on.
POINTER = "❯"  # noqa: RUF001
CHECK_ON = "◉"
CHECK_OFF = "○"
BULLET = "·"

# The step-record vocabulary. TICK alone can only describe a flow that worked, and
# the flows that print records (`doctor`, `init`'s live finish) exist precisely to
# report honest partial success — a tools host that didn't come up, an agent that
# didn't register. WARN is "we could not confirm it", CROSS is "it is not so".
TICK = "✓"
WARN = "⚠"
CROSS = "✗"

# Rounded edges read as modern and match the house style of the tools this CLI
# sits alongside.
BOX = box.ROUNDED

# The hint rendered in a widget's bottom border. Ctrl-C — not Esc — is the cancel
# key: readchar blocks after "\x1b" waiting to disambiguate an escape sequence, so
# a lone Esc press cannot be observed at all (design §4.1). Ctrl-C is also what the
# CLI already documents as safe and resumable, so this is honest rather than a
# compromise.
HINT_SELECT = f"↑↓ move {BULLET} enter select {BULLET} ctrl-c cancel"
HINT_CHECKBOX = f"↑↓ move {BULLET} space toggle {BULLET} enter confirm {BULLET} ctrl-c cancel"
HINT_TEXT = f"enter confirm {BULLET} ctrl-c cancel"
