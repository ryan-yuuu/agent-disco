# Supervisor narration is the caller's; causes are never

The supervisor's start paths print operator-facing prose written for the standalone CLIs
(`disco start`, `disco tools start`, `disco agent start`). Composite flows — `disco init`'s
live finish, `disco agent create`'s start-now — narrate the same events in their own voice,
so that prose arrived as a second narrator: `lifecycle.start`'s closing signpost told the
operator to run `disco agent start <name>` on the line before init ran it for them, and
`start_slot`'s "agent scribe started" duplicated init's own record. We added two boolean
kwargs — `banner` on `lifecycle.start`, `announce` on `start_slot` / `component_start` /
`roster.agent_start` / `cli/_supervisor.start_tools_host` — that suppress **only** the
next-step signpost and the success/outcome narration. Both default to `True`, so every
existing caller is unchanged.

The line these flags draw, and the reason this ADR exists:

- **Signposts and outcomes are the caller's to own.** A flow that goes on to take the next
  step, or that reports the outcome itself, is entitled to silence the standalone version.
- **Causes are never the caller's.** Every error, refusal, and warning prints regardless.
  `start_tools_host`'s advisory guard degrades a raise to a return code, so the caller
  *structurally cannot* see the exception — its repr is the only channel the cause has.

That second rule is not decoration. We widened the flag to cover the exception path and a
`PermissionError` spawning the tools host produced **zero output**, sending the operator to
`disco logs tools` — a log the host never lived to write. A related mistake followed:
`announce=False` was granted on the promise that init names the remedy itself, but init's
epilogue *ranked* its remedies, so a coinciding "bot can't post" silently outranked and
dropped the tools-host remedy. Both are the same error — a flag that quietly grew from
"the caller says this too" into "the caller says nothing".

So: if you are tempted to widen these into a general `quiet`, don't. The contract is
narration-only, and the reason it is worded so narrowly is that we already broke it twice.

## Considered Options

- **An injected reporter callback** (`narrate: Callable[[str], None] = print`) matches
  `init.py`'s heavy dependency-injection house style. Rejected: init's DI exists to swap
  *world-touching dependencies* — clocks, spawners, probes — so tests run hermetically. A
  banner is not a dependency; it is a policy about who narrates. It also doesn't escape the
  real question ("which prints go through it?" — only the signpost), it just encodes that
  answer less legibly, with more machinery.
- **Return structured state and let every caller narrate.** The most principled option, and
  what we'd choose if the errors were caller-narrated too. They can't be: the ten error
  strings in `lifecycle.start` are composed where their context lives (the bridge log path,
  the failure diagnosis, the teardown outcome), and flattening them into data would gut the
  honest-error design. It would change `int` → tuple across three production callers and
  ~29 test call sites, and relocate the already-open/agents-defined knowledge away from the
  function that computes it — a large diff buying only the banner.

## Consequences

`banner=False` also skips `lifecycle.start`'s `_agents_defined(home)` read, which exists
solely to choose between the signpost's two variants and lazily imports the heavy agents
package. Both of its reads sit inside `if banner:` blocks, so this is unobservable.

A caller that silences narration takes on reporting the outcome — the return code is not
operator-facing feedback. `disco init` discharges that with its record board (`✓/⚠/✗`) and
its finish banner. The duty is **per exit, not per flow**: init's agent-start failure returns
before the epilogue, and for a while that path printed `✗ tools host  not running` and then
nothing — a failure shown with no remedy, on a path `main` had covered. It now names the
remedy and a next step itself. The general rule for anyone adding a caller: a flow that
passes `announce=False` owes the report on **every** exit, and any early return is a place
that debt silently goes unpaid.
