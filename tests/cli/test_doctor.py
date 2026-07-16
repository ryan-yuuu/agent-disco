"""Tests for ``disco doctor`` (src/calfcord/cli/doctor.py)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from calfcord.cli import doctor
from calfcord.health.heartbeat import Heartbeat

# A recognizable bot token that must NEVER appear in doctor's output.
TOKEN = "SENTINEL_TOKEN_do_not_leak_42"


# --------------------------------------------------------------- _check_interpreter


def _install_fake_uv(home: Path, python_dir: str) -> None:
    """Put a ``uv`` in the home that answers ``uv python dir`` with ``python_dir``."""
    (home / "bin").mkdir(parents=True, exist_ok=True)
    uv = home / "bin" / "uv"
    uv.write_text(f'#!/usr/bin/env bash\nif [ "$1 $2" = "python dir" ]; then printf "%s\\n" "{python_dir}"; fi\n')
    uv.chmod(0o755)


def test_check_interpreter_reports_an_owned_interpreter(tmp_path: Path) -> None:
    """An install on uv's managed CPython is what ADR 0023 promises."""
    home = tmp_path / "home"
    # The running interpreter's base_prefix sits under this root, so it reads as managed.
    _install_fake_uv(home, str(Path(sys.base_prefix).parent))
    result = doctor._check_interpreter(home)
    assert result.status == "ok"


def test_check_interpreter_flags_a_borrowed_interpreter(tmp_path: Path) -> None:
    """A venv bound to a conda/system Python must be visible, not silent (ADR 0023).

    The install kept working while its interpreter lived in ~/miniconda3 — and
    would have broken the day conda was removed. It went unnoticed for over a day
    because nothing ever looked. ``warn``, not ``fail``: nothing is broken yet, so
    it must not flip doctor's scriptable exit code.
    """
    home = tmp_path / "home"
    _install_fake_uv(home, str(tmp_path / "somewhere-else"))
    result = doctor._check_interpreter(home)
    assert result.status == "warn"
    assert sys.base_prefix in result.detail


def test_check_interpreter_warns_when_uv_cannot_be_asked(tmp_path: Path) -> None:
    """No uv in the home is a warning, never a crash."""
    result = doctor._check_interpreter(tmp_path / "home")
    assert result.status == "warn"


def test_check_interpreter_warns_when_uv_errors(tmp_path: Path) -> None:
    """A uv that runs but cannot answer degrades to a warning, not a traceback.

    Reachable with a malformed ~/.config/uv/uv.toml, which makes uv error on
    nearly every subcommand. doctor's job is to report, so a broken uv must not
    take the whole preflight down.

    It prints a plausible path *and* exits non-zero, so the exit status is the
    only thing that can reject it: a stub that merely printed nothing would be
    caught by the empty-stdout guard and leave the returncode check unproven.
    """
    home = tmp_path / "home"
    _install_fake_uv(home, str(Path(sys.base_prefix).parent))
    uv = home / "bin" / "uv"
    uv.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "{Path(sys.base_prefix).parent}"\nexit 2\n')
    uv.chmod(0o755)
    result = doctor._check_interpreter(home)
    assert result.status == "warn"
    # Not the "ok" it would report if the failed call's stdout were trusted.
    assert "uv-managed" not in result.detail


def test_check_interpreter_warns_when_uv_is_not_executable_at_all(tmp_path: Path) -> None:
    """A uv that cannot even be spawned is still just a warning (OSError path)."""
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    uv = home / "bin" / "uv"
    uv.write_text("#!/nonexistent/interpreter\n")
    uv.chmod(0o755)
    result = doctor._check_interpreter(home)
    assert result.status == "warn"


# --------------------------------------------------------------------- _parse_broker


@pytest.mark.parametrize(
    "url,expected",
    [
        ("localhost:9094", ("localhost", 9094)),  # explicit non-default port is honored
        ("localhost", ("localhost", 9092)),  # bare host defaults to 9092
        ("b1:9092,b2:9093", ("b1", 9092)),  # first endpoint of a comma list
        ("kafka://h:9092", ("h", 9092)),  # scheme stripped
        ("[::1]:9092", ("::1", 9092)),  # ipv6 with port
        ("[::1]", ("::1", 9092)),  # ipv6 without port
        ("  host:9092  ", ("host", 9092)),  # surrounding whitespace
    ],
)
def test_parse_broker_valid(url, expected):
    assert doctor._parse_broker(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "host:abc", ":9092", "host:99999", "host:0", "host:-1"])
def test_parse_broker_invalid(url):
    assert doctor._parse_broker(url) is None


# --------------------------------------------------------------------- helpers


def _seed_agent(agents_dir: Path, name: str, *, valid: bool = True) -> None:
    body = f"You are {name}." if valid else ""  # empty body -> parse fails
    (agents_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: a test agent\n---\n{body}\n",
        encoding="utf-8",
    )


def _factory(handler):
    """A client_factory yielding an httpx.Client backed by a MockTransport handler."""
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _resp_ok(request):
    return httpx.Response(200, json={"username": "TestBot"})


def _resp_401(request):
    return httpx.Response(401, json={"message": "401: Unauthorized"})


def _raise_net(request):
    raise httpx.ConnectError("network down")


def _resp_non_json(request):
    return httpx.Response(200, text="<html>edge proxy interstitial</html>")


def _resp_non_dict(request):
    return httpx.Response(200, json=["not", "a", "dict"])


def _resp_403(request):
    return httpx.Response(403, json={"message": "403: Forbidden"})


def _resp_429(request):
    return httpx.Response(429, json={"message": "rate limited"})


def _resp_500(request):
    return httpx.Response(500, text="server error")


def _boom_factory():
    raise AssertionError("the network must not be called")


def _setup(
    monkeypatch,
    tmp_path,
    *,
    token=TOKEN,
    appid="123456789",
    broker="localhost:9092",
    reachable=True,
    make_env=True,
):
    """Build a healthy install layout + effective env; return (env_path, agents_dir)."""
    env_path = tmp_path / "config" / ".env"
    if make_env:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("DISCORD_BOT_TOKEN=seeded\n", encoding="utf-8")  # presence only
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_agent(agents_dir, "scribe")

    for key, val in (("DISCORD_BOT_TOKEN", token), ("DISCORD_APPLICATION_ID", appid), ("CALF_HOST_URL", broker)):
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)

    monkeypatch.setattr(doctor, "_tcp_reachable", lambda host, port, timeout=2.0: reachable)
    # A healthy install resolves the calfkit-mesh broker binary; stub it so unit
    # tests never perform the real one-time extraction to ~/.calfkit/bin.
    monkeypatch.setattr(doctor, "resolve_broker_bin", lambda: "/stub/bin/tansu-v0.6.0")
    return env_path, agents_dir


# --------------------------------------------------------------------- run() behaviors


def test_all_pass_returns_0_and_shows_bot_name(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "✗" not in out and "⚠" not in out
    assert "TestBot" in out


def test_missing_token_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, token=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


def test_broker_unset_warns_not_fail(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, broker=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0  # a warning never fails the run
    assert "⚠" in out


def test_broker_unreachable_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, reachable=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


def test_broker_binary_ok_shows_resolved_path(monkeypatch, tmp_path, capsys):
    """The broker-binary check reports the resolved calfkit-mesh path as ok."""
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "resolve_broker_bin", lambda: "/opt/tansu")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "broker binary" in out
    assert "/opt/tansu" in out
    assert "⚠" not in out and "✗" not in out  # the check is ok, not warn/fail


def test_broker_binary_unresolvable_warns_not_fails(monkeypatch, tmp_path, capsys):
    """An unresolvable/unextractable binary is a diagnostic warning, not a hard
    fail — a host pointed at a remote broker never runs the local binary, so this
    must not break doctor's scriptable exit code for it."""
    from calfkit_mesh import TansuBinaryNotFound

    env_path, agents_dir = _setup(monkeypatch, tmp_path)

    def _raise():
        raise TansuBinaryNotFound("no wheel for this platform")

    monkeypatch.setattr(doctor, "resolve_broker_bin", _raise)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out
    assert "no wheel for this platform" in out


def test_broker_binary_empty_message_names_the_type(monkeypatch, tmp_path, capsys):
    """A dependency exception with no message still names a cause in the detail
    line, rather than leaving a bare ``unavailable:``."""
    from calfkit_mesh import TansuBinaryNotFound

    env_path, agents_dir = _setup(monkeypatch, tmp_path)

    def _raise():
        raise TansuBinaryNotFound("")

    monkeypatch.setattr(doctor, "resolve_broker_bin", _raise)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "TansuBinaryNotFound" in out


@pytest.mark.parametrize(
    "handler,rc,needle",
    [
        (_resp_401, 1, "rejected"),  # token not accepted -> hard fail
        (_resp_403, 1, "rejected"),  # forbidden is also "won't boot" -> hard fail
        (_resp_429, 0, "rate-limited"),  # rate limited -> warn, never fail
        (_resp_500, 0, "⚠"),  # unexpected 5xx -> warn
        (_resp_non_json, 0, "⚠"),  # 200 + non-JSON body must not crash -> warn
        (_resp_non_dict, 0, "⚠"),  # 200 + non-dict JSON -> warn
        (_raise_net, 0, "⚠"),  # transport error -> warn
    ],
)
def test_token_check_classifies_response(monkeypatch, tmp_path, capsys, handler, rc, needle):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    assert doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(handler)) == rc
    out = capsys.readouterr().out
    assert needle in out.lower()  # ⚠ has no lowercase form, so the same check works for symbols
    assert TOKEN not in out


def test_offline_skips_network(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    # _boom_factory raises if called; offline must not call it.
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, offline=True, client_factory=_boom_factory)
    out = capsys.readouterr().out
    assert rc == 0
    assert "✗" not in out  # token is present; presence-only check passes


def test_unparseable_agent_fails_and_names_it(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    _seed_agent(agents_dir, "broken", valid=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "broken" in out


def test_no_agents_warns(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    (agents_dir / "scribe.md").unlink()  # remove the seeded agent -> empty dir
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out


def test_missing_env_fails_with_init_hint(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, make_env=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "disco init" in out


def test_appid_non_numeric_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, appid="not-a-number")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


def test_unreadable_env_fails_cleanly(monkeypatch, tmp_path, capsys):
    # A non-UTF-8 .env must be reported, not crash doctor with a UnicodeDecodeError.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    env_path.write_bytes(b"\xff\xfe not utf-8")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unreadable" in out


def test_empty_config_warns(monkeypatch, tmp_path, capsys):
    # A present-but-empty .env (the fresh-install state) is a warning, not a "no config" failure.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    env_path.write_text("# only a comment\n", encoding="utf-8")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0  # config-empty is a warn; the real values come from os.environ
    assert "⚠" in out


def test_appid_missing_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, appid=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "not set" in out


def test_broker_unparseable_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, broker="host:abc")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unparseable" in out


def test_token_never_leaks_across_paths(monkeypatch, tmp_path, capsys):
    handlers = (_resp_ok, _resp_401, _resp_403, _resp_429, _resp_500, _raise_net, _resp_non_json, _resp_non_dict)
    for handler in handlers:
        env_path, agents_dir = _setup(monkeypatch, tmp_path / handler.__name__)
        doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(handler))
        captured = capsys.readouterr()
        assert TOKEN not in captured.out
        assert TOKEN not in captured.err


# =================================================================== runtime section
#
# When the daemon is up (detected via the bridge heartbeat) doctor adds a RUNTIME
# section on top of the 5 STATIC checks. The daemon-alive check (heartbeat
# freshness) always runs; a fresh beat then adds a single informational "roster"
# line pointing at the native mesh view. The calfkit 0.12 migration removed the
# bespoke control-plane deep-probe + local↔org drift checks (roster liveness rides
# the native mesh now — see ``disco status``). The heartbeat reader / clock are
# injected so no real heartbeat file is needed (§4.4 / §12.1 / §13.3).

_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)


def _beat(component: str = "bridge", *, age_s: float = 1.0, identity: str = "TestBot") -> Heartbeat:
    """A bridge heartbeat ``age_s`` seconds old relative to ``_NOW``."""
    last = _NOW - timedelta(seconds=age_s)
    return Heartbeat(
        component=component,
        pid=4242,
        started_at=last - timedelta(seconds=60),
        last_beat=last,
        status="healthy",
        identity=identity,
    )


def _reader(beats: dict[str, Heartbeat]):
    """A ``read_beat_fn(home, component)`` stub backed by an in-memory beat map."""
    return lambda home, component: beats.get(component)


def _runtime_setup(monkeypatch, tmp_path):
    """A healthy STATIC layout plus an install ``home`` for the runtime section."""
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    home = tmp_path  # the heartbeat reader is stubbed, so the beat file is never read
    # ...but a native install always owns a uv (ADR 0023), and doctor asks it where
    # the managed interpreters live. Report a root that contains this interpreter's
    # base_prefix, so the home reads as the healthy install these tests describe.
    _install_fake_uv(home, str(Path(sys.base_prefix).parent))
    return env_path, agents_dir, home


def _run_runtime(monkeypatch, tmp_path, *, beats):
    """Invoke doctor with the runtime seams wired; return (rc, stdout)."""
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        now=_NOW,
        read_beat_fn=_reader(beats),
    )
    return rc, monkeypatch  # caller reads capsys separately


def test_daemon_down_skips_runtime_section(monkeypatch, tmp_path, capsys):
    # No bridge heartbeat at all -> the daemon is not running. doctor reports the
    # STATIC checks and explicitly notes the runtime section was skipped; it must
    # NOT fail solely because the workspace is closed (read-only, closed is valid).
    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={},  # no beats -> daemon down
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "disco start" in out  # the next-step hint for a closed workspace
    # `disco start` reopens only the substrate — the closed-workspace remedy must
    # also name the roster re-start (matching the stale-daemon remedy) or the
    # operator's agents stay silently offline.
    assert "disco agent start --all" in out
    # The runtime roster line never renders when the daemon is down.
    assert "roster" not in out.lower()


def test_no_home_skips_runtime_section(monkeypatch, tmp_path, capsys):
    # A dev invocation (no install home) cannot locate heartbeats; the runtime
    # section is simply absent and the static contract is unchanged.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=None,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon" not in out.lower()


def test_stale_heartbeat_fails_as_zombie(monkeypatch, tmp_path, capsys):
    # A bridge heartbeat exists but is older than the TTL -> a wedged/zombie
    # daemon. That is a hard fail framed as a fix (restart the workspace).
    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat(age_s=600)},  # well past the 10s TTL
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗" in out
    assert "calfcord" in out  # a fix is named (restart)


def test_fresh_daemon_reports_roster_hint(monkeypatch, tmp_path, capsys):
    # Daemon fresh -> the runtime section is all-green: the daemon-alive line names
    # the bridge identity and the roster line points at the native mesh view.
    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat(identity="MyBot")},
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "MyBot" in out  # the bridge identity is surfaced
    assert "disco status" in out  # the roster hint points at the mesh view
    assert "✗" not in out


def test_runtime_section_keeps_static_checks(monkeypatch, tmp_path, capsys):
    # The 5 STATIC checks must still run (and still fail the run) even when the
    # runtime section is active: a missing app id is still a hard ✗.
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)

    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        now=_NOW,
        read_beat_fn=_reader({"bridge": _beat()}),
    )
    out = capsys.readouterr().out
    assert rc == 1  # the static app-id failure still fails the whole run
    assert "discord app id" in out


def test_runtime_token_never_leaks(monkeypatch, tmp_path, capsys):
    # The token must not leak through the runtime paths either.
    _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
    )
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


# --------------------------------------------------------- production seam defaults
#
# The tests above inject the runtime seams; these two exercise the production
# *defaults* (the real heartbeat reader plus the ``now`` resolution) — so a wiring
# regression in the default-resolution path is caught, not silently un-covered. The
# deep probe was removed in the calfkit 0.12 migration, so the default runtime
# section stops at the daemon-alive + roster-hint lines.


def test_default_read_beat_resolves_from_disk_daemon_down(monkeypatch, tmp_path, capsys):
    # With `home` set but no seams injected, doctor must default to the real
    # `read_beat` and (finding no on-disk beat) report the workspace closed. This
    # covers the `now` / `read_beat` default-resolution branches.
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,  # only home; every runtime seam defaults
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "disco start" in out  # closed-workspace next-step hint


def test_default_seams_report_roster_hint_from_disk_beat(monkeypatch, tmp_path, capsys):
    # A real fresh on-disk beat + the default seams: the deep control-plane probe
    # was removed in the calfkit 0.12 migration (roster liveness rides the native
    # mesh now), so doctor defaults `now` / `read_beat_fn`, reads the real beat, and
    # emits the single informational "roster" line pointing at `disco status`.
    from calfcord.health.heartbeat import write_beat

    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    write_beat(home, "bridge", status="healthy", identity="DiskBot", now=_NOW)

    # Freeze the default freshness clock so the just-written beat reads as fresh.
    monkeypatch.setattr(doctor, "datetime", _FrozenDatetime)

    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        # now / read_beat_fn all default
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "DiskBot" in out  # the real on-disk beat was read by the default reader
    assert "disco status" in out  # the roster hint replaces the deleted deep probe


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now()`` is pinned to ``_NOW`` so the default
    freshness clock in :func:`doctor._runtime_section` is deterministic."""

    @classmethod
    def now(cls, tz=None):  # matches datetime.now signature
        return _NOW


# ------------------------------------------------------------ state-aware closer


def test_all_ok_closer_is_state_aware_when_workspace_open(monkeypatch, tmp_path, capsys):
    """When doctor's own runtime board just showed the workspace OPEN (fresh
    bridge beat), the closer must not say "you're ready to start" — the
    operator already started it."""
    rc, _ = _run_runtime(monkeypatch, tmp_path, beats={"bridge": _beat()})
    out = capsys.readouterr().out
    assert rc == 0
    assert "ready to start" not in out
    assert "all checks passed" in out
    assert "workspace is open" in out


def test_all_ok_closer_still_steers_to_start_when_workspace_closed(
    monkeypatch, tmp_path, capsys
):
    rc, _ = _run_runtime(monkeypatch, tmp_path, beats={})  # no beat -> closed
    out = capsys.readouterr().out
    assert rc == 0
    assert "ready to start" in out
