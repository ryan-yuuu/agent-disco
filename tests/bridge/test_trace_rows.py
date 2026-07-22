"""Unit tests for the PURE row model in :mod:`calfcord.bridge.trace_rows`.

A step trace is a list of :data:`TraceRow` values; this module turns one row
into one line. Everything here takes a value and returns a value — no Discord,
no Kafka, no clock — so the row state machine is testable without any of it.

Covers:

* ``_plain`` — the row's text hygiene (``TestPlain``). A row is ONE line, and
  tool output is not; a newline that survives into a row breaks out of the
  per-line ``-# `` prefix and renders the remainder bright, so flattening is a
  correctness requirement, not a cosmetic one.
* ``_summarise_args`` — one arg promoted to prose, scalar remainder bracketed,
  non-scalars dropped (``TestSummariseArgs``).
* ``render_row`` — one test per variant per state (``TestRenderRow``).
* the growth-reserve invariant the reservation rule rests on
  (``TestGrowthReserve``).
"""

from __future__ import annotations

import calfcord.bridge.trace_rows as trace_rows
from calfcord.bridge.trace_rows import (
    ConsultRow,
    HandoffRow,
    Plain,
    ProseRow,
    SealRow,
    ToolRow,
    _duration,
    _plain,
    _summarise_args,
    render_row,
)

_URL = "https://discord.com/channels/1/2"


class TestPlain:
    """``_plain`` flattens, escapes, and bounds arbitrary tool text so it can
    never break the single-line row it lands in."""

    def test_passes_plain_text_through_unchanged(self) -> None:
        assert _plain("no such file") == "no such file"

    def test_flattens_newlines_to_spaces(self) -> None:
        # THE load-bearing case: `-# ` is a per-LINE prefix, so a surviving
        # newline would render the remainder at full brightness.
        assert _plain("Traceback:\n  File 'x.py'\n  boom") == "Traceback: File 'x.py' boom"

    def test_collapses_whitespace_runs_and_strips(self) -> None:
        assert _plain("  a \t\t b \n\n  c  ") == "a b c"

    def test_escapes_markdown_that_would_break_the_row(self) -> None:
        assert _plain("**bold** _em_ ~s~ `code` |spoil|") == r"\*\*bold\*\* \_em\_ \~s\~ \`code\` \|spoil\|"

    def test_escapes_backslash_first_so_escapes_are_not_doubled(self) -> None:
        # A literal backslash must not turn the NEXT escape into a no-op.
        assert _plain(r"a\*b") == r"a\\\*b"

    def test_truncates_to_the_detail_cap_with_an_ellipsis(self) -> None:
        out = _plain("x" * 500)
        assert len(out) <= trace_rows._DETAIL_MAX
        assert out.endswith("…")

    def test_truncation_never_leaves_a_dangling_escape(self) -> None:
        # Cutting mid-escape would leave a lone backslash that escapes the
        # ellipsis and renders as a stray glyph.
        out = _plain("*" * 500)
        assert not out.removesuffix("…").endswith("\\")

    def test_empty_text_is_empty(self) -> None:
        assert _plain("") == ""
        assert _plain("   \n  ") == ""


class TestSummariseArgs:
    """opencode's rule, ported: promote one arg to prose, bracket the scalar
    remainder, drop every non-scalar so a nested object can't bloat a row."""

    def test_promotes_a_conventional_subject_key(self) -> None:
        assert _summarise_args({"path": "invoices/4417.json"}) == ("invoices/4417.json", "")

    def test_brackets_remaining_scalars_beside_the_subject(self) -> None:
        subject, detail = _summarise_args({"path": "a.py", "offset": 1, "limit": 130})
        assert subject == "a.py"
        assert detail == "[offset=1, limit=130]"

    def test_does_not_repeat_the_subject_in_the_detail(self) -> None:
        _, detail = _summarise_args({"path": "a.py", "limit": 5})
        assert "a.py" not in detail

    def test_drops_non_scalar_values(self) -> None:
        # A nested object or list can never bloat the line.
        subject, detail = _summarise_args({"path": "a.py", "opts": {"deep": [1, 2]}, "n": 3})
        assert subject == "a.py"
        assert detail == "[n=3]"

    def test_no_conventional_key_yields_no_subject_and_brackets_everything(self) -> None:
        assert _summarise_args({"alpha": 1, "beta": "two"}) == ("", "[alpha=1, beta=two]")

    def test_empty_args_are_empty(self) -> None:
        assert _summarise_args({}) == ("", "")

    def test_subject_key_precedence_follows_the_declared_order(self) -> None:
        # Both present: the earlier key in _SUBJECT_KEYS wins, deterministically.
        subject, _ = _summarise_args({"name": "n", "path": "p"})
        assert subject == "p"

    def test_a_non_scalar_subject_key_is_not_promoted(self) -> None:
        # `path` present but an object — fall through rather than str() a dict.
        subject, detail = _summarise_args({"path": {"nested": 1}, "n": 2})
        assert subject == ""
        assert detail == "[n=2]"

    def test_subject_and_detail_are_hygienised(self) -> None:
        subject, detail = _summarise_args({"path": "a\nb", "q": "*x*"})
        assert subject == "a b"
        assert detail == r"[q=\*x\*]"


class TestDuration:
    """Two significant units, never three — opencode's ``Locale.duration``."""

    def test_sub_second_is_milliseconds(self) -> None:
        assert _duration(40) == "40ms"

    def test_sub_minute_is_one_decimal_of_seconds(self) -> None:
        assert _duration(12_300) == "12.3s"

    def test_over_a_minute_is_minutes_and_seconds(self) -> None:
        assert _duration(125_000) == "2m 5s"

    def test_zero_is_milliseconds(self) -> None:
        assert _duration(0) == "0ms"


class TestRenderRow:
    """One line per row. Dim (``-# ``) is the resting state; escaping it is the
    attention mechanism, because Discord has no per-line colour. Emoji are
    rationed to the two states that need the reader."""

    def test_prose_is_bright_and_unprefixed(self) -> None:
        # The agent's own narration is not machine chatter — it never dims.
        assert render_row(ProseRow(text="Let me look into that.")) == "Let me look into that."

    def test_pending_tool_is_bright_with_the_in_flight_glyph(self) -> None:
        # Bright because it is the live edge; Discord has no spinner to offer.
        row = ToolRow(key="t1", name="read_file", subject="invoices/4417.json")
        assert render_row(row) == r"◐ read\_file invoices/4417.json"

    def test_ok_tool_dims_and_carries_its_duration(self) -> None:
        row = ToolRow(key="t1", name="read_file", subject="invoices/4417.json", state="ok", elapsed_ms=40)
        assert render_row(row) == r"-# ● read\_file invoices/4417.json · 40ms"

    def test_ok_tool_keeps_its_bracketed_args(self) -> None:
        row = ToolRow(key="t1", name="read_file", subject="a.py", detail="[limit=130]", state="ok", elapsed_ms=40)
        assert render_row(row) == r"-# ● read\_file a.py [limit=130] · 40ms"

    def test_failed_tool_escapes_the_dim_register_and_shows_the_error(self) -> None:
        # THE attention mechanism: no `-# `, an emoji, and the error itself.
        row = ToolRow(
            key="t1",
            name="search_docs",
            subject="rejection codes",
            state="failed",
            note="connection timed out after 5s",
        )
        assert render_row(row) == r"❌ search\_docs rejection codes — connection timed out after 5s"

    def test_denied_tool_is_dim_and_struck_not_red(self) -> None:
        # opencode separates these deliberately: a denial is routine (a winning
        # handoff stubs its siblings), a failure is not. Red is not spent here.
        row = ToolRow(key="t1", name="search_docs", state="denied", note="superseded by handoff")
        assert render_row(row) == r"-# ~~⊘ search\_docs~~ — superseded by handoff"

    def test_denied_tool_without_a_note_renders_no_dangling_dash(self) -> None:
        row = ToolRow(key="t1", name="search_docs", state="denied")
        assert render_row(row) == r"-# ~~⊘ search\_docs~~"

    def test_interrupted_tool_states_it_without_needing_a_note(self) -> None:
        # What the seal rewrites an unresolved row to when the run faults.
        row = ToolRow(key="t1", name="lookup_account", subject="acct_88213", state="interrupted")
        assert render_row(row) == r"-# ~~⊘ lookup\_account acct\_88213~~ — interrupted"

    def test_ok_tool_without_a_measured_duration_omits_it(self) -> None:
        # An orphan result (no call row was ever seen) has nothing to measure
        # from. Rendering "· 0ms" would state a timing that was never taken.
        row = ToolRow(key="t1", name="read_file", subject="a.py", state="ok", elapsed_ms=None)
        assert render_row(row) == r"-# ● read\_file a.py"

    def test_tool_with_no_subject_or_detail_renders_bare(self) -> None:
        assert render_row(ToolRow(key="t1", name="ping", state="ok", elapsed_ms=5)) == "-# ● ping · 5ms"

    def test_pending_consult_reads_in_the_present_tense_and_already_links(self) -> None:
        # The bug this fixes: today's marker says "consulted" the moment the
        # consult STARTS, and never updates. The link is present from the start
        # because the audit thread already exists by then — and because a link
        # appearing only on resolve is growth the reservation cannot afford.
        row = ConsultRow(key="c1", peer="conan", thread_url=_URL)
        assert render_row(row) == f"◐ consulting conan · [view exchange]({_URL})"

    def test_ok_consult_dims_and_links_the_exchange(self) -> None:
        row = ConsultRow(key="c1", peer="conan", thread_url=_URL, state="ok")
        assert render_row(row) == f"-# ● consulted conan · [view exchange]({_URL})"

    def test_failed_consult_escapes_the_dim_register(self) -> None:
        row = ConsultRow(key="c1", peer="conan", thread_url=_URL, state="failed")
        assert render_row(row) == f"❌ conan didn't answer · [view exchange]({_URL})"

    def test_denied_consult_is_dim_and_struck_with_its_reason(self) -> None:
        row = ConsultRow(key="c1", peer="conan", thread_url=_URL, state="denied", denial_reason="cycle detected")
        assert render_row(row) == f"-# ~~⊘ conan~~ — cycle detected · [view exchange]({_URL})"

    def test_interrupted_consult_says_the_peer_never_replied(self) -> None:
        # A consult still open when the run faulted — dispatcher.dangling().
        row = ConsultRow(key="c1", peer="conan", thread_url=_URL, state="interrupted")
        assert render_row(row) == f"-# ~~⊘ conan~~ — never replied · [view exchange]({_URL})"

    def test_consult_without_a_thread_url_states_the_audit_gap(self) -> None:
        # The projection is best-effort; a consult that renders nothing is
        # exactly the invisibility the marker exists to prevent.
        row = ConsultRow(key="c1", peer="conan", thread_url=None, state="ok")
        assert render_row(row) == "-# ● consulted conan · ⚠️ couldn't write the audit log"

    def test_pending_inline_consult_shows_the_ask_in_the_link_slot(self) -> None:
        # A NESTED consult (inline=True) renders in the caller's trace INSIDE the
        # same audit thread, so its exchange sits right below — there is no
        # separate thread to link. The preview shows a glimpse of the ask in the
        # link's slot; the absent thread_url is deliberate, NOT an audit gap.
        row = ConsultRow(
            key="c1", caller="sol", peer="terra", inline=True, request_preview="review the auth changes in src"
        )
        assert render_row(row) == '◐ sol → terra · "review the auth changes in src"'

    def test_resolved_inline_consult_keeps_the_request_preview(self) -> None:
        row = ConsultRow(key="c1", peer="terra", inline=True, request_preview="review the auth changes", state="ok")
        assert render_row(row) == '-# ● consulted terra · "review the auth changes"'

    def test_denied_inline_consult_appends_the_preview_after_the_reason(self) -> None:
        # The preview takes the link's slot (same `·` separator), so the denied
        # reason and the preview coexist without a doubled em-dash.
        row = ConsultRow(
            key="c1",
            peer="terra",
            inline=True,
            state="denied",
            denial_reason="cycle detected",
            request_preview="review the auth",
        )
        assert render_row(row) == '-# ~~⊘ terra~~ — cycle detected · "review the auth"'

    def test_interrupted_inline_consult_keeps_the_preview(self) -> None:
        # The one place the interrupted+preview render is asserted: a nested
        # consult left dangling by a fault seals to "never replied" but keeps its
        # ask, so a dropped `tail` here is caught.
        row = ConsultRow(key="c1", peer="terra", inline=True, request_preview="review the auth", state="interrupted")
        assert render_row(row) == '-# ~~⊘ terra~~ — never replied · "review the auth"'

    def test_inline_consult_with_an_empty_ask_shows_a_bare_marker(self) -> None:
        # A nested consult whose prompt was blank is STILL inline (its exchange is
        # right below), so it shows a bare marker — never the top-level
        # "couldn't write the audit log" tail, which would be a false alarm.
        assert render_row(ConsultRow(key="c1", peer="terra", inline=True)) == "◐ consulting terra"
        assert render_row(ConsultRow(key="c1", peer="terra", inline=True, state="ok")) == "-# ● consulted terra"

    def test_inline_takes_the_tail_even_if_a_thread_url_is_present(self) -> None:
        # `inline` is the discriminant, not the presence of a url: an inline row's
        # exchange is in this thread, so it shows the ask, never a link.
        row = ConsultRow(key="c1", peer="terra", inline=True, thread_url=_URL, request_preview="ask")
        assert render_row(row) == '◐ consulting terra · "ask"'

    def test_request_preview_is_hygienised(self) -> None:
        # The preview is the model's own message arg: it MUST be flattened and
        # markdown-escaped like every other model-controlled field, or a newline
        # breaks out of the per-line prefix.
        row = ConsultRow(key="c1", peer="terra", inline=True, request_preview="line1\n*bold*")
        assert "\n" not in render_row(row)
        assert render_row(row) == r'◐ consulting terra · "line1 \*bold\*"'

    def test_handoff_is_bright_and_carries_the_model_s_reason(self) -> None:
        # `reason` is always populated (calfkit rejects a blank one) and is
        # currently rendered in NO surface at all.
        row = HandoffRow(target="billing", reason="card on file expired, billing owns re-runs")
        assert render_row(row) == "➜ handed off to billing — card on file expired, billing owns re-runs"

    def test_handoff_without_a_reason_renders_bare(self) -> None:
        assert render_row(HandoffRow(target="billing")) == "➜ handed off to billing"

    def test_seal_dims_and_counts_the_turn(self) -> None:
        assert render_row(SealRow(outcome="ok", tool_count=4, elapsed_ms=12_300)) == "-# 4 tools · 12.3s"

    def test_seal_pluralises_a_single_tool(self) -> None:
        assert render_row(SealRow(outcome="ok", tool_count=1, elapsed_ms=900)) == "-# 1 tool · 900ms"

    def test_seal_omits_the_tool_count_when_no_tools_ran(self) -> None:
        # codex only labels a footer when the turn actually did work.
        assert render_row(SealRow(outcome="ok", tool_count=0, elapsed_ms=900)) == "-# 900ms"

    def test_faulted_seal_escapes_the_dim_register_and_points_at_the_notice(self) -> None:
        # The connective tissue: the notice lands below, on a different lineage.
        row = SealRow(outcome="faulted", tool_count=4, elapsed_ms=12_300)
        assert render_row(row) == "⚠️ run failed after 4 tools · 12.3s — details below"

    def test_faulted_seal_with_no_tools_still_points_at_the_notice(self) -> None:
        row = SealRow(outcome="faulted", tool_count=0, elapsed_ms=900)
        assert render_row(row) == "⚠️ run failed after 900ms — details below"

    def test_interrupted_seal_claims_neither_success_nor_failure(self) -> None:
        # When the stream ends without a terminal the bridge does NOT know the
        # outcome. Saying "run failed — details below" would assert a failure
        # that may not have happened AND point at a notice that may not exist;
        # the reply can still arrive perfectly well. Say only what is known: the
        # trace was cut short.
        row = SealRow(outcome="interrupted", tool_count=4, elapsed_ms=12_300)
        assert render_row(row) == "-# ⊘ interrupted after 4 tools · 12.3s"

    def test_rows_are_frozen(self) -> None:
        # Resolving a row REPLACES it; nothing mutates a row in place.
        import dataclasses

        import pytest

        row = ToolRow(key="t1", name="read_file")
        with pytest.raises(dataclasses.FrozenInstanceError):
            row.state = "ok"  # type: ignore[misc]


class TestGrowthReserve:
    """The invariant the trace renderer's reservation rule rests on.

    A segment that has already been posted CANNOT be re-split, so when a pending
    row resolves it must be guaranteed to still fit. The renderer buys that by
    reserving :data:`_ROW_GROWTH_RESERVE` per pending row — which is only sound
    if no row can ever grow by more than that. Break this and a resolve pushes a
    posted segment past 4000, Discord 400s it, the failure is swallowed to a
    WARNING, and the segment stays ``dirty`` forever: retried on every wake,
    never rendered.

    The bound assumes the row-building contract — that text reaching a row has
    been through :func:`_plain`, so no field exceeds ``_DETAIL_MAX``. The
    segment's own hard cap is the backstop if that is ever violated.
    """

    # Worst case on every axis: a max-length note, a long head, a long duration,
    # and a real Discord thread URL.
    _NOTE = "x" * trace_rows._DETAIL_MAX
    _LONG_URL = "https://discord.com/channels/123456789012345678/123456789012345678"

    def _growth(self, pending: object, resolved: object) -> int:
        return len(render_row(resolved)) - len(render_row(pending))  # type: ignore[arg-type]

    def test_no_resolved_state_grows_a_tool_row_beyond_the_reserve(self) -> None:
        import dataclasses

        for head in ({}, {"subject": "invoices/4417.json"}, {"subject": "a" * 60, "detail": "[limit=130]"}):
            pending = ToolRow(key="t1", name="read_file", **head)  # type: ignore[arg-type]
            for state in ("ok", "failed", "denied", "interrupted"):
                resolved = dataclasses.replace(
                    pending,
                    state=state,  # type: ignore[arg-type]
                    note=self._NOTE,
                    elapsed_ms=1440 * 60 * 1000,  # a 24h run — the longest duration string
                )
                growth = self._growth(pending, resolved)
                assert growth <= trace_rows._ROW_GROWTH_RESERVE, f"{state} on {head!r} grew by {growth}"

    def test_no_resolved_state_grows_a_consult_row_beyond_the_reserve(self) -> None:
        # The consult row is the tight one: it carries BOTH a note and a link.
        # It is why the pending render must already include the link — a link
        # that only appears on resolve is pure, unbudgeted growth.
        import dataclasses

        for url in (None, self._LONG_URL):
            pending = ConsultRow(key="c1", peer="conan", thread_url=url)
            for state in ("ok", "failed", "denied", "interrupted"):
                resolved = dataclasses.replace(pending, state=state, denial_reason=self._NOTE)  # type: ignore[arg-type]
                growth = self._growth(pending, resolved)
                assert growth <= trace_rows._ROW_GROWTH_RESERVE, f"{state} with url={url!r} grew by {growth}"

    def test_no_resolved_state_grows_a_consult_row_with_a_preview_beyond_the_reserve(self) -> None:
        # The preview sits in BOTH the pending and resolved renders (it is the
        # link's slot), so it must not add resolve-time growth over the reserve.
        import dataclasses

        pending = ConsultRow(key="c1", peer="conan", inline=True, request_preview="p" * 60)
        for state in ("ok", "failed", "denied", "interrupted"):
            resolved = dataclasses.replace(pending, state=state, denial_reason=self._NOTE)  # type: ignore[arg-type]
            growth = self._growth(pending, resolved)
            assert growth <= trace_rows._ROW_GROWTH_RESERVE, f"{state} with a preview grew by {growth}"

    def test_the_reserve_is_not_wastefully_larger_than_the_worst_case(self) -> None:
        # Headroom is cheap but not free: every pending row's reserve is
        # subtracted from a 4000-char segment. Keep the constant honest.
        assert trace_rows._ROW_GROWTH_RESERVE < trace_rows._DETAIL_MAX * 2


class TestPlainIsIdempotent:
    """``_plain`` must be safe to apply again.

    The contract used to be "call ``_plain`` EXACTLY once" — the hardest kind to
    keep: zero lets a newline break out of the per-line ``-# `` prefix, twice
    double-escapes visibly. It spanned two modules, and three fields (`name`,
    `peer`, `target`) missed it. Idempotency makes the contract "at least once",
    which is trivially satisfiable — so a row can hygienise its own fields
    without caring whether the caller already did.
    """

    def test_applying_plain_twice_changes_nothing(self) -> None:
        once = _plain(r"a\*b **bold** ~s~")
        assert _plain(once) == once

    def test_the_result_is_marked_as_already_plain(self) -> None:
        assert isinstance(_plain("hi"), Plain)

    def test_a_plain_value_passes_through_untouched(self) -> None:
        # _summarise_args already returns hygienised text; a row coercing its
        # fields must not escape it a second time.
        assert _plain(Plain(r"already \*escaped\*")) == r"already \*escaped\*"

    def test_rows_hygienise_their_own_fields(self) -> None:
        # The fields a model or peer controls, coerced by the row itself — so a
        # caller that forgets cannot reintroduce the break-out.
        assert "\n" not in render_row(ToolRow(key="t", name="evil\nrm -rf /"))
        assert "\n" not in render_row(ConsultRow(key="c", peer="bob\n# HUGE"))
        assert "\n" not in render_row(HandoffRow(target="peer\n-# fake", reason="x\ny"))

    def test_replace_does_not_double_escape(self) -> None:
        # THE reason a transforming __post_init__ was rejected before: replace()
        # re-runs it. Idempotency is what makes it safe.
        import dataclasses

        row = ToolRow(key="t", name="a_b", note=r"x\*y")
        for _ in range(3):
            row = dataclasses.replace(row, state="failed")
        assert render_row(row) == render_row(ToolRow(key="t", name="a_b", note=r"x\*y", state="failed"))

    def test_prose_is_never_hygienised(self) -> None:
        # Prose is the agent's own markdown, rendered bright with no `-# `
        # prefix — escaping it would corrupt the answer.
        assert render_row(ProseRow(text="**bold**\nsecond line")) == "**bold**\nsecond line"
