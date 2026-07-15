# Rich TUI over readchar input; drop InquirerPy

**Status:** accepted

The interactive CLI's prompts are now a Rich-rendered TUI (`src/calfcord/cli/tui/`)
reading keys through **readchar**, replacing InquirerPy. `inquirerpy`,
`prompt-toolkit`, `pfzy`, and `wcwidth` leave the dependency tree; `rich` (already
resolved transitively) and `readchar` (zero dependencies) enter it.

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

**readchar rather than a higher-level prompt library.** Every candidate
(beaupy, questionary, questo, simple-term-menu, pick) formats rows internally: beaupy's
only row hook is typed `Callable[[T], str]`, so it *cannot* accept a Rich renderable.
None can express the chosen design — rounded panel, monochrome, hint rendered in the
bottom border, two-column dim descriptions. Since the renderer had to be ours
regardless, the remaining value of such a library collapses to a key loop plus `Live`
plumbing, measured at ~60 lines. Textual was rejected separately: it inverts the sync
model, and `run_async()` mutates the caller's loop task factory without restoring it —
a hazard next to a calfkit loop.

## Consequences

**The asyncio constraint is lifted, and the rule it forced is now only a preference.**
InquirerPy's `.execute()` called `asyncio.run()` internally, so a prompt nested inside
our own `asyncio.run()` raised `RuntimeError: asyncio.run() cannot be called from a
running event loop` — a crash this project shipped at the end of `disco agent create`.
That is why `agent_create._finish_create` and `init._run_finish` ask everything on the
sync side first, and why `pause` uses a bare `input()`. readchar owns no event loop, so
none of that is load-bearing any more. **The structure is kept as style, not
necessity** — the code comments now say so rather than citing a constraint that no
longer binds.

Do not restate this as "prompt_toolkit cannot run inside a loop": that is false, and
verified false by probe. `Application.run_async()` never calls `asyncio.run()`, and
`InquirerPy.execute_async()` / `questionary.ask_async()` both work inside a live loop.
The real reason those were rejected is **fit** — the async API would make `Prompter`
async, turning ~32 call sites into `await` and breaking the 8 Protocol guards.

**Two readchar traps are pinned by tests**, because both are silent:
`readchar.key.ENTER` is LF while a POSIX terminal in raw mode sends CR (binding only to
the constant leaves Enter dead), and a lone Esc **blocks** because `readkey()` waits on
the next byte to disambiguate an escape sequence. Ctrl-C is therefore the advertised
cancel — which `main()` already maps to exit 130 and `init` already teaches as safe and
resumable.

**Non-TTY behaviour moved.** readchar surfaces a non-terminal as `termios.error`, which
does **not** subclass `OSError` and so escaped `main()`'s non-TTY handler as a raw
traceback. `keys.read_key` re-raises it as `OSError(ENOTTY)` to route it back into that
existing handler. The unit test monkeypatches the exception and stayed green while the
product broke, so this class of bug needs the CLI actually run with piped stdin.

**Palette is monochrome by decision** (`theme.py`): weight and dimming carry all
hierarchy, no accent hue. An off-white accent is only off-white on a dark terminal;
bold-on-default-foreground is the one accent that renders on every theme. Red is
reserved for genuine errors.

Design: `docs/design/cli-tui-migration.md`
