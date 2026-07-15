# Rich TUI with readchar menus and prompt_toolkit line editing

**Status:** accepted

The interactive CLI is a Rich-rendered TUI (`src/calfcord/cli/tui/`) with two
deliberately separate input backends. Select, checkbox, and confirm widgets read
navigation keys through **readchar**. Text and secret fields use
**prompt_toolkit** for mature line editing. InquirerPy and pfzy leave the dependency
tree; Rich, readchar, prompt_toolkit, and its wcwidth dependency remain.

The `Prompter` Protocol in `_prompts.py` is **unchanged** — only `make_prompter()`'s
return value differs. All 7 interactive commands and the 8 test fakes that mirror the
Protocol needed no edit, and the 813-test CLI suite passed the swap untouched. That
seam is the reason this was a small change.

## Why

**Rich has no select or checkbox widget.** `rich.prompt.Prompt.ask(choices=[...])`
makes the operator *type* the answer; there is no arrow-key navigation and no
multi-select. So "render with Rich" settles rendering and says nothing about input —
the two decisions are independent, and only the input one was open.

**InquirerPy is abandoned.** Last PyPI release 2022-06-27, last commit 2022-11-19, an
unfixed memory leak (kazhala/InquirerPy#88), no viable fork, and a README that now
points users at questionary. It works today, but nobody will fix it when
prompt_toolkit next breaks it. This alone justified leaving, independently of the
visual work.

**readchar rather than a hand-rolled reader.** readchar is narrow (raw key reads
only), zero-dependency, 13.8M downloads/month, and its POSIX reader is the same
`termios` recipe we would have written — but maintained, and field-tested on the
escape-sequence edge cases. Owning that code would have bought nothing.

**readchar rather than a higher-level prompt library for menus.** Every candidate
(beaupy, questionary, questo, simple-term-menu, pick) formats rows internally: beaupy's
only row hook is typed `Callable[[T], str]`, so it *cannot* accept a Rich renderable.
None can express the chosen design — rounded panel, monochrome, hint rendered in the
bottom border, two-column dim descriptions. Since the renderer had to be ours
regardless, the remaining value of such a library collapses to a key loop plus `Live`
plumbing, measured at ~60 lines. Textual was rejected separately: it inverts the sync
model, and `run_async()` mutates the caller's loop task factory without restoring it —
a hazard next to a calfkit loop.

**prompt_toolkit rather than a hand-rolled line editor.** The first Rich migration
incorrectly treated text as an append-only key stream and displayed defaults as
placeholders. Rich `Live` hid the hardware cursor, the widget rendered no software
caret, and users could neither see nor edit a position inside the value. Backspace
also did nothing to a displayed default because the real buffer was empty.
prompt_toolkit owns the difficult primitives here: editable defaults, cursor motion,
Home/End/Delete, word editing, Unicode display width, bracketed paste, masking, and
resize handling. Questionary adds no value for this use case; its text prompt is a
thin PromptSession wrapper, so the TUI uses PromptSession directly.

## Consequences

**The asyncio constraint is lifted, and the rule it forced is now only a preference.**
InquirerPy's `.execute()` called `asyncio.run()` internally, so a prompt nested inside
our own `asyncio.run()` raised `RuntimeError: asyncio.run() cannot be called from a
running event loop` — a crash this project shipped at the end of `disco agent create`.
That is why `agent_create._finish_create` and `init._run_finish` ask everything on the
sync side first, and why `pause` uses a bare `input()`. readchar owns no event loop;
the line editor calls PromptSession with `in_thread=True`, which keeps the synchronous
Prompter contract safe even from an active asyncio loop. **The structure is kept as
style, not necessity** — the code comments now say so rather than citing a constraint
that no longer binds.

Do not restate this as "prompt_toolkit cannot run inside a loop": that is false, and
verified false by probe. `Application.run_async()` never calls `asyncio.run()`, and
`InquirerPy.execute_async()` / `questionary.ask_async()` both work inside a live loop.
The real reason those were rejected is **fit** — the async API would make `Prompter`
async, turning ~32 call sites into `await` and breaking the 8 Protocol guards.

**One readchar trap is pinned by tests**, because it is silent: a lone Esc **blocks** —
`readkey()` reads `\x1b` and then waits on the next byte to disambiguate an escape
sequence, so a lone Esc press cannot be observed at all. Ctrl-C is therefore the
advertised cancel, which `main()` already maps to exit 130 and `init` already teaches as
safe and resumable.

**A retracted claim, recorded rather than quietly deleted.** Earlier revisions of this
ADR, the design doc, the code comments, and the PR description all asserted that
`readchar.key.ENTER` is LF while a terminal in raw mode sends CR — so binding only to the
constant would leave the Enter key "dead". **That is false.** readchar is *not* in raw
mode: it clears only c_lflag bits (ICANON/ECHO) and never touches `ICRNL` in c_iflag, so
the tty driver still translates CR→LF and `readchar.key.ENTER` matches on its own. A pty
probe disproved it; the `"\r"` binding is kept as belt-and-braces for Windows / true raw
mode, not as a correction.

It is worth knowing *how* it survived: the claim was reached by reading a `termios` call
and inferring "raw mode" without checking **which flag word** it modified — and it was
labelled "found by inspection" in the design doc, next to a section that had already
learned to say "verified by probe". Library behaviour in this area must be probed, not
read.

**Non-TTY behaviour moved.** A broken stdin breaks in three ways, on three different lines, and
none of them reach `main()`'s `except OSError` unaided: `termios.error` (piped/CI) does not
subclass `OSError`; `AttributeError` (fd 0 closed at exec, so CPython sets `sys.stdin = None` and
readchar hits `sys.stdin.fileno()` *before* termios); and `ValueError` (closed file object).
`keys.read_key` funnels all three into `OSError(ENOTTY)`, and `main`'s own `isatty` check is
guarded too — the code that diagnoses a broken stdin must not break on one.

Twice now the *test* was the problem, not the fix: first a unit test monkeypatched the exception
and stayed green while the product broke; then the end-to-end test used `subprocess.DEVNULL`, which
opens a **real fd**, so it proved only the one path already handled while a genuinely closed fd 0
still tracebacked. This class of bug requires running the CLI against each real stdin state.

**The `$EDITOR` shell-out stays, but it was wrong in three ways** (`_editor.py`). It
ignored `$VISUAL`, which every implementation surveyed honours first (click, gh,
gemini-cli, Codex, aider — 5/5). It never passed `--wait`, so `EDITOR=code` returned
instantly and the operator's edit was **silently discarded**. And it fell back to bare
`vi`, unannounced. Now: `VISUAL` → `EDITOR` → probe(`sensible-editor`, `nano`, `vim`,
`vi`), wait-flag injected for known GUI editors, and the editor **named before it opens**.

The fallback is a **probe, not a constant**, because the field genuinely disagrees — gh
(`defaultEditor = "nano" // EXTENDED to switch from vim`) and jj ship nano, click prefers
vim, aider and gemini-cli use vi, Codex refuses to guess and errors. Any hardcoded name is
a guess that fails where it is absent; prompt_toolkit hardcodes `/usr/bin/*` and silently
breaks under Homebrew and Nix. Note the "nano is the beginner-friendly choice" story is
largely folklore: jj's pico default was an accident (avoiding editor backup files in a VCS
with no ignore support), and Debian's nano default arose because vim's own maintainers
rescaled vim *below* a nano priority nobody was looking at.

**Defaults are editable buffer content.** Enter accepts an unchanged default;
Backspace, Delete, cursor movement, and ordinary insertion edit it in place; clearing
the buffer returns `""`. This restores conventional line-editor semantics and removes
the ambiguity between displayed state and editable state.

**Secret transcripts reveal neither content nor length.** The active field is masked
by prompt_toolkit using conventional one-bullet-per-character feedback. After submission
the permanent Rich transcript says only `provided` or `kept current`; retaining that
bullet count in scrollback would leak credential length.

**Pathologically small terminals use compact fallbacks.** Below 16 columns, text fields
drop the decorative right border and use a truncated title plus plain input marker.
List prompts first reduce their viewport by measured rendered height; if even one normal
row cannot fit, they preserve a one-row rounded panel while omitting the optional
instruction, scroll counters, and footer hint and ellipsizing the active label. Normal
terminal dimensions retain the complete panel design.

**Palette is monochrome by decision** (`theme.py`): weight and dimming carry all
hierarchy, no accent hue. An off-white accent is only off-white on a dark terminal;
bold-on-default-foreground is the one accent that renders on every theme. Red is
reserved for genuine errors.

Design: `docs/design/cli-tui-migration.md`
