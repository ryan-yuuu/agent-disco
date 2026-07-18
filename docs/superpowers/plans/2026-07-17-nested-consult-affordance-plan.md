# Implementation plan — nested-consult affordance

Spec: [`2026-07-17-nested-consult-audit-affordance-design.md`](../specs/2026-07-17-nested-consult-audit-affordance-design.md)

TDD throughout: write the failing test first, make it pass, refactor. Build
bottom-up (row → renderer → projector → handler) so each layer's tests run
against a finished layer below. `uvx ruff check` on touched files; no
`ruff format`.

## Phase 1 — `ConsultRow` carries a request preview (`bridge/trace_rows.py`)

- **Test first** (`tests/bridge/test_trace_rows.py`): a `ConsultRow` with
  `request_preview="review the auth changes…"` renders `◐ consulting terra —
  "review the auth changes…"` (pending) and `-# ● consulted terra — "…"` (ok),
  with **no** trailing `· [view exchange]` link and **no** `⚠️ couldn't write
  the audit log` audit-gap marker. A `ConsultRow` with no preview renders exactly
  as today (link / audit-gap unchanged). Width is identical across states
  (growth-reservation stability).
- **Implement**: add `request_preview: str = ""` to `ConsultRow`; include it in
  the `_hygienise` call. In `_render_consult`, when `request_preview` is set,
  render `— "<preview>"` in the tail position for every state and omit the link;
  otherwise keep today's `· <link>`/audit-gap behavior.
- **Acceptance**: reject/denied/interrupted states also render the preview tail
  without a link; `render_row` dispatch unchanged.

## Phase 2 — `on_consult` accepts a preview (`bridge/trace.py`)

- **Test first** (`tests/bridge/test_trace.py`): `on_consult(..., request_preview="x")`
  appends a `ConsultRow` whose `request_preview == "x"`; the existing no-preview
  call is unchanged.
- **Implement**: add keyword-only `request_preview: str = ""` to
  `StepTraceRenderer.on_consult`, thread it into the `ConsultRow`. Update the
  `StepTraceRendererLike` protocol signature in `mention_handler.py` to match.
- **Acceptance**: the human-thread caller (`_render_consult`) still compiles and
  passes nothing → unchanged rows.

## Phase 3 — projector methods (`bridge/a2a_project.py`)

- **Test first** (`tests/bridge/test_a2a_project.py`):
  - `project_consult(A2ARequest)` with a mapped thread calls the renderer's
    `on_consult` once with `key=tool_call_id`, `peer`, `persona_name=caller`,
    `dest=(channel_id, thread_id)`, `thread_url=None`, and a **truncated**
    `request_preview`; **no** persona message is sent.
  - No thread mapped ⇒ drops (renderer not called), no raise.
  - `project_consult_result` maps `A2AReply→state="ok"`, `A2AReject→"denied"`,
    `A2AFailed→"failed"`, passing `note=text` for reject/fail (empty for reply),
    and `key`/`correlation_id` through.
- **Implement**: add a preview-length constant (≈60); `project_consult` and
  `project_consult_result` as described in the spec's change surface. Reuse the
  `_threads`/`_channel_id` lookup shape from `project_step`.
- **Acceptance**: methods are best-effort (a renderer error is swallowed like the
  other projector renders) and never create a thread.

## Phase 4 — wire nested consults in the drain (`bridge/mention_handler.py`)

- **Test first** (`tests/bridge/test_mention_handler.py`):
  - A nested consult **request** (emitter ≠ acting agent) calls
    `a2a.project_consult` and does **not** call `a2a.project` (suppression) and
    does **not** touch the human trace.
  - A nested consult **reply/reject/fault** calls `a2a.project` (peer answer /
    note) **and** `a2a.project_consult_result`.
  - The acting agent's consult path is unchanged (`project` + `_render_consult`).
- **Implement**: in the `projection is not None` branch, split `is_acting`
  (unchanged) from the nested case: `A2ARequest → project_consult`; else
  `project` + `project_consult_result`. Add `project_consult` /
  `project_consult_result` to the `A2AProjectorLike` protocol.
- **Acceptance**: ordering is inherited from the shared renderer (no new
  sequencing code); the outer `finish` path is untouched.

## Phase 5 — ADR + doc

- New ADR (next number **0027**) extending ADR-0026: a nested consult is
  announced by a resolving row in the caller's trace, request text folded onto
  the row, standalone nested-request message suppressed, no cross-link. Follow
  `.agents/skills/grill-with-docs/ADR-FORMAT.md`.
- `docs/a2a-threads.md`: update the "What humans see" mock-up and the nested-
  consult paragraph (preview on the row; no cross-link; ordering now guaranteed
  because the row shares the peer box's writer).

## Phase 6 — verify

- `uv run pytest` (full suite) green; `uvx ruff check` clean on touched files.
- `/pytest-coverage` on the three touched modules to confirm the new lines are
  covered (mutation-test each new guard as #124 did: preview-vs-link branch,
  no-thread drop, result-state mapping, request-suppression).

## Phase 7 — review (after a commit)

- Commit the implementation, then fan out **read-only** review subagents
  (`/pr-review-toolkit:review-pr`, `/simplify`) per project convention. Converge
  to no must-fix findings before declaring done.
