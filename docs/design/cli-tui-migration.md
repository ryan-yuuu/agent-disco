# Interactive CLI → Rich TUI migration

**Status:** Design proposal — no code written yet.
**Scope:** The **7 interactive `disco` commands** only. Non-interactive commands
(`doctor`, `agent list/show`, `status`, `logs`, `explain`, `deploy`, lifecycle verbs)
are explicitly out of scope and keep their current plain-text output.

---

## 1. Goal

Replace the InquirerPy-backed prompt surface with a Rich-rendered TUI that reads as
simple, clean, and modern — the inline, scrollback-preserving idiom of Claude Code
and Codex, not a full-screen dashboard.

## 2. The interactive surface (complete)

`make_prompter()` is called at exactly 7 sites in `cli/main.py`. That set *is* the
migration surface:

| Command | Entry | Prompts used |
|---|---|---|
| `disco init` | `init.run` | select, text, secret, pause |
| `disco agent create [name]` | `agent_create.run` | select, text, secret, confirm, checkbox |
| `disco agent edit [name]` | `agent_edit.run` | select, text, secret, confirm, checkbox |
| `disco agent delete <name>` | `agent_lifecycle.run_delete` | confirm |
| `disco agent tools [name]` | `agent_tools.run` | select, checkbox |
| `disco mcp add [server]` | `mcp_admin.run_add` | text, select, confirm |
| `disco mcp remove <server>` | `mcp_admin.run_remove` | confirm |

## 3. Constraints discovered (these drive the design)

### 3.1 Rich has no select or checkbox widget

Rich is a **rendering** library. `rich.prompt.Prompt.ask(choices=[...])` makes the
operator *type* the choice; there is no arrow-key navigation, and no multi-select at
all. Verified against current Rich docs, not memory.

**So arrow-key `select`/`checkbox` must be built from `rich.live.Live` plus a key
reader we own.** This is the central architectural consequence: "use Rich" settles
rendering and says nothing about input.

### 3.2 The asyncio landmine — real, but narrower than it first looks

InquirerPy's `.execute()` internally calls `asyncio.run()` (via prompt_toolkit's
`Application`). A prompt nested inside the CLI's own `asyncio.run()` therefore raises
`RuntimeError: asyncio.run() cannot be called from a running event loop`. **This was a
real shipped crash** at the end of `disco agent create`.

The codebase's fix is an unwritten architectural rule — *ask everything first, THEN
`asyncio.run` the work* — and it is load-bearing structure today, visible at
`agent_create.py`'s `_finish_create`, `init._run_finish`, and in `pause` using bare
`input()` precisely to avoid driving a second loop.

**Correction — verified by probe; do not repeat the stronger claim.** It is *not* true
that prompt_toolkit cannot run inside a live loop. `Application.run_async()` is an
ordinary coroutine that never calls `asyncio.run()`, and both wrappers expose it:
`InquirerPy.execute_async()` and `questionary.ask_async()` each run fine inside a
running loop — no `nest_asyncio`, no worker thread. The crash is specific to the
**synchronous** API, which is the one this codebase used.

So the landmine is real, but it does not by itself disqualify prompt_toolkit. What
disqualifies it is **fit**: taking the async escape hatch makes `Prompter` async, which
turns ~32 call sites into `await`, makes all 7 flows async, and breaks the 8
`isinstance(..., Prompter)` guards §3.3 says must not move — a large cost for a
capability the current structure does not need.

The honest benefit of a loop-free reader is therefore narrower, and still worth having:
it removes a latent footgun (any future prompt reached from async code) at **zero**
architectural cost, and lets the sync `Prompter` stay exactly as it is. A full-screen
TUI, by contrast, owns a persistent loop and would *invert* the rule outright — see §5.

### 3.3 The `Prompter` seam is clean, complete — and pinned by tests

Every flow depends only on the 6-method `Prompter` Protocol plus `Choice(value, label,
checked)`. A new implementation needs **zero changes** to any command module.

But 8 test files assert `isinstance(FakePrompter(), Prompter)`. **Adding a method to
the Protocol breaks all 8.** The Protocol must stay exactly as-is.

Behavioural contracts the fakes and callers pin:
- `select`'s `default` must be a value present in `choices` (`_providers.py:352-358`).
- Empty `choices` crashes InquirerPy today and `pick_model` defends against it
  (`_providers.py:343-350`) — the new widget should raise a clear error instead.
- `secret` must return `""` on skip, so keep-existing-on-empty works.
- `pause` is pinned by `tests/cli/test_prompts.py` to bare `input()` + swallowed
  `EOFError`. **It must keep using `input()`.**

### 3.4 Output is coupled to tests by substring matching

~235 assertions match printed output via `capsys`. Mitigating facts: **no matched
literal is ≥60 chars** (156 are <20), and Rich auto-disables color on a non-TTY.

The real hazards are **wrapping** (Rich defaults to width 80 off-TTY, and a wrap inside
a matched fragment fails the assert silently) and **markup collision** (`#general`,
`$VAR`, and any `[...]` would be eaten as Rich markup tags).

Both are solved by `console.print(msg, markup=False, highlight=False, soft_wrap=True)`
— `soft_wrap` disables wrapping and cropping, making Rich byte-identical to `print()`
for plain lines while still allowing styling where we want it. This is the key that
makes migrating the interleaved prints safe.

### 3.4a InquirerPy is abandoned — an independent reason to move

Last PyPI release **0.3.4, 2022-06-27**; last commit 2022-11-19. It carries an unfixed
memory leak (kazhala/InquirerPy#88), has no viable fork (the leading one has 1 star),
and its own README now points users at questionary. It works today, but nobody will fix
it when prompt_toolkit next breaks it.

This matters because it holds independently of the design argument: even a "do nothing
visually" plan would eventually need to leave InquirerPy. Worth stating plainly in the
ADR rather than resting the case on aesthetics.

For calibration: **the whole category is low-velocity.** prompt_toolkit has no release
in ~11 months, questionary likewise, and readchar itself had a 17-month gap before
4.2.2. "Actively maintained" is not a useful tiebreaker here; readchar's claim is that
it is *small, feature-complete and zero-dependency*, not that it is bustling.

### 3.5 Smaller facts

- `rich` is **not** a direct dependency (only transitive via typer/cyclopts). Needs `uv add rich`.
- No Windows support (macOS + Linux only) → the key reader is POSIX `termios` only.
- ~40 `print()` calls sit *outside* the prompt seam, interleaved with prompts inside
  the interactive flows. These are the "chrome" (§7).
- The 5 InquirerPy-backed methods have **zero direct test coverage** today, so swapping
  the backend breaks no existing test.

## 3.6 Decisions taken

| Decision | Choice |
|---|---|
| Input layer | **A** — Rich `Live` + in-house POSIX key reader (§4) |
| Presentation | **Panelled inline** — rounded panels, hint in the bottom border; scrollback preserved (§5) |
| Accent | **Monochrome / off-white.** No chromatic accent. Hierarchy comes from weight and dim, not hue: off-white bold for the pointer / selected row / focus, dim grey for borders and hints. Red is reserved for genuine errors only (a safety signal, not an accent) and is one constant in `theme.py` if we later want it gone too. |
| Scope | **Widgets + chrome** for the 7 interactive commands (§7) |

## 4. Decision 1 — the input layer

Rendering is Rich either way (§3.1), so this decision is only about **input**.

| Option | Verdict |
|---|---|
| **A. Rich `Live` + `readchar` for key reads** | **CHOSEN.** `readchar` is narrow (raw key reads only), 13.8M downloads/month, **zero dependencies**, last release 2026-04-06, `requires_python >=3.8`. Its POSIX reader is synchronous `termios` — no asyncio anywhere — so §3.2 dies. It owns the escape-sequence state machine and restores `termios` in a `finally`, and raises `KeyboardInterrupt` on Ctrl-C, which `main.py` already maps to exit 130. We keep full control of rendering, which the panelled monochrome design requires. |
| B. Hand-rolled `termios` reader | Rejected. It would be a near-copy of readchar's reader with none of the maintenance or field-testing. No reason to own this. |
| C. Rich + prompt_toolkit key bindings | Reintroduces the §3.2 landmine the codebase already paid for. Very heavy dep for key reads. |
| D. `beaupy` (Rich + readchar, pre-assembled) | Rejected on **rendering**, not input: its look is fixed, so the panelled/monochrome/hint-in-border design is unreachable. 87k downloads/month vs readchar's 13.8M — we'd take a less-proven dep to get *less* control. |
| E. `questionary` / keep `InquirerPy` | Both prompt_toolkit-based (§3.2). Styling cannot reach the target design. |
| F. `Textual` | Full-screen + async app model — rejected with §5. |

### 4.1 readchar facts (verify by probe — inspection got one of these wrong)

1. ~~**`readchar.key.ENTER` is LF but raw mode delivers CR, so binding only to the constant leaves
   Enter dead.**~~ **RETRACTED — this was false, and it shipped in the code, the ADR, and the PR
   description before a pty probe caught it.** readchar is **not** in raw mode: it clears only
   c_lflag bits (ICANON/ECHO) and never touches `ICRNL` in c_iflag, so the tty driver still
   translates CR->LF and `readchar.key.ENTER` (`"\n"`) matches on its own. The `"\r"` binding is
   kept as belt-and-braces for Windows / true-raw-mode, not as a correction.

   **Note how it failed**, because §3.2 already learned this lesson and this section did not: the
   claim was labelled "found by inspection" — I saw a `termios` call and inferred "raw mode" without
   checking *which flag word* it modified. Every library claim in this document needs a probe, not a
   reading.
2. **A bare Esc blocks.** `readkey()` reads `\x1b` and then *blocks* on the next byte to
   disambiguate an escape sequence, so a lone Esc press hangs until another key arrives.
   **"esc cancel" is therefore unimplementable** and must not be advertised in the hint line.
   **Ctrl-C is the honest cancel** — readchar raises `KeyboardInterrupt` for it, `main.py` already
   maps that to `"aborted."` + 130, and `init` already teaches Ctrl-C as safe and resumable. The
   hint line reads `↑↓ move · enter select · ctrl-c cancel`.

### 4.2 What `keys.py` is (and is not)

It is **not** terminal handling — readchar owns that. It is a ~40-line semantic layer that:
- maps readchar's raw strings onto a `Key` enum, aliasing CR/LF onto `ENTER` (trap 1) and the
  DECCKM cursor variants (`\x1bOA`/`\x1bOB`) onto `UP`/`DOWN`, which readchar does not define;
- exposes `read_key()` as the one injectable input seam, so every widget is testable by feeding a
  scripted key list with no TTY.

## 5. Decision 2 — presentation model

**Recommended: inline, scrollback-preserving.** Widgets render transiently via `Live`;
on answer, `Live` is torn down and a compact one-line record is printed, so history
reads as a clean transcript.

This matches Claude Code and Codex, and it is the only option compatible with `disco
init`'s actual requirements: the invite link must persist in scrollback to be copied,
the browser detour and `pause` gate need the normal terminal, and Ctrl-C must stay safe
and resumable. A full-screen alt-screen app fights all three and triggers §3.2's
inversion.

## 6. Module layout

`_prompts.py` **keeps** `Choice`, the `Prompter` Protocol, and `make_prompter()` — so
all 8 consumer modules' imports are untouched and the 8 protocol guards keep passing.
Only `make_prompter()`'s return value changes.

```
src/calfcord/cli/tui/
  __init__.py    public surface
  theme.py       colors + glyphs — one source of truth
  keys.py        Key enum, read_key(), raw_mode() context manager
  state.py       pure SelectState / CheckboxState reducers (move, toggle, filter)
  widgets.py     select, checkbox, text, secret, confirm, pause on Live + keys
  render.py      console(), header(), step(), note(), success(), warn(), error()
  prompter.py    RichPrompter — implements Prompter
```

The chrome (§7) uses `render.console()` at module level, so no command signature
changes and capsys keeps working.

## 7. Scope tiers

- **Tier 1 — widgets.** Swap the 6 prompt shapes to Rich. Zero test breakage (§3.3).
- **Tier 2 — chrome.** The ~40 interleaved prints in the 7 interactive flows become
  headers, step indicators, and styled result lines. This is what makes the result a
  coherent TUI rather than pretty widgets in a plain stream. Guarded by §3.4's
  `soft_wrap` rule; touches ~110 assertions but should not break them.

Non-interactive commands are untouched, so `doctor` / `agent_inspect`'s 56
formatting assertions stay safe.

## 8. Test strategy (this gets *better*, not worse)

The widgets become far more testable than InquirerPy's were:

- `keys.py` — decode byte sequences (`\x1b[A` → `Key.UP`) as pure table-driven tests.
- `state.py` — pure reducers; wrap-around, toggle, select-all are unit tests with no
  terminal at all.
- `widgets.py` — inject a scripted key iterator + `Console(record=True)`, then assert
  on `export_text()`. No TTY, no subprocess.
- `RichPrompter` — driven end-to-end through the same injected seam.

Per CLAUDE.md this is TDD: tests first, via `/test-driven-development`.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Raw mode left on after an exception/signal | `raw_mode()` restores in `finally`; `Live` teardown is also in `finally`. |
| Non-TTY (CI, piped stdin) | Widgets check `isatty` and raise a clean error; `main.py`'s OSError/EOFError branches keep their contract (`test_main.py:294-331` pins the message). |
| Rich markup eats `#general` / `$VAR` / `[...]` | `markup=False` by default in `render.py`; opt in explicitly. |
| Wrap breaks a substring assertion | `soft_wrap=True` for plain lines (§3.4). |
| Terminal resize mid-widget | `Live` re-renders on refresh; state is width-independent. |

## 10. Deliverables

1. `uv add rich`; drop `inquirerpy` (and prompt_toolkit with it).
2. The `cli/tui/` package + tests.
3. `make_prompter()` returns `RichPrompter`.
4. Chrome migration for the 7 interactive flows.
5. **ADR-0018** — "Rich TUI with an in-house key reader; drop InquirerPy/prompt_toolkit",
   recording the §3.2 rationale (this qualifies under `.agents/skills/grill-with-docs/ADR-FORMAT.md`).
6. Docs refresh where InquirerPy is named (`docs/authoring-agents.md:216`,
   `docs/design/mcp-reintroduction.md:176`).
