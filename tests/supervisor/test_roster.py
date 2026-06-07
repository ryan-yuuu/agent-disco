"""Unit tests for the agent-roster operations (design §3.4, §3.5, §13.1).

These exercise ``agent_start`` / ``agent_stop`` / ``agent_restart`` / ``agent_ps``
with **no real process-compose binary, no broker, and no network**: the control-
plane probe and the REST client are both injected. A stub probe scripts the
broker-wide live roster; a stub client scripts the supervisor REST surface
(``project_state`` for the workspace check, ``start_process`` /
``stop_process`` / ``restart_process`` for lifecycle, ``list_processes`` for the
physical half of the ps union).

The contracts pinned here are the distributed-correct duplicate guard (§3.5 —
refuse to start a name already live anywhere, CLI-side, without a bridge change),
the not-declared-agent reload path (§13.1 — a brand-new agent authored after
``calfcord start`` needs a workspace reload, never an ``update_project`` that
would bounce the substrate), and the three-way ps union (§3.4 — physical∩logical,
physical-only "not yet registered", logical-only "running on another host").
"""

from __future__ import annotations

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.supervisor import roster
from calfcord.supervisor.client import ProcessComposeError

# --- fakes ------------------------------------------------------------------


def _defn(name: str) -> AgentDefinition:
    """A minimal valid AgentDefinition with ``agent_id == name``.

    The probe returns AgentDefinitions; the roster ops key off ``.agent_id``, so
    a stub roster entry only needs a valid id (the rest is required by the model).
    """
    return AgentDefinition(
        agent_id=name,
        display_name=name.title(),
        description=f"{name} agent",
        system_prompt="hi",
    )


class _StubProbe:
    """A scriptable stand-in for ``probe_live_roster``.

    Records the ``server_urls`` it was called with so a test can assert the probe
    fired (duplicate guard / ps) or did NOT (workspace-down short-circuit), and
    returns a fixed list of live AgentDefinitions.
    """

    def __init__(self, roster_result: list[AgentDefinition] | None = None) -> None:
        self._roster = list(roster_result or [])
        self.calls: list[str] = []

    async def __call__(self, server_urls: str):
        self.calls.append(server_urls)
        return list(self._roster)


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient.

    ``workspace_up`` drives the ``project_state`` workspace check (False → the
    real client's RuntimeError-on-transport-failure, i.e. supervisor unreachable).
    ``start_raises`` models the not-declared-process error the PC server returns
    for an agent authored after ``calfcord start``. Every lifecycle call records
    its name so tests can assert it was (or was NOT) issued. ``update_project`` is
    present only so a test can assert §13.1: the not-declared path NEVER calls it.
    """

    def __init__(
        self,
        *,
        workspace_up: bool = True,
        start_raises: Exception | None = None,
        list_processes_result: object = None,
        fail_start: dict[str, Exception] | None = None,
        fail_stop: dict[str, Exception] | None = None,
        fail_restart: dict[str, Exception] | None = None,
    ) -> None:
        self._workspace_up = workspace_up
        self._start_raises = start_raises
        self._list_processes_result = list_processes_result or []
        # Per-name failure scripts let the bulk best-effort tests fail ONE item and
        # assert the sweep continues; absent (the default), no per-name op fails, so
        # the single-op tests above are unaffected.
        self._fail_start = fail_start or {}
        self._fail_stop = fail_stop or {}
        self._fail_restart = fail_restart or {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []
        self.update_project_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            # Mirrors ProcessComposeClient: a transport failure (supervisor not
            # up) surfaces as RuntimeError, which the workspace check reads as
            # "not running".
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def start_process(self, name: str):
        self.start_calls.append(name)
        if name in self._fail_start:
            raise self._fail_start[name]
        if self._start_raises is not None:
            raise self._start_raises
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        if name in self._fail_stop:
            raise self._fail_stop[name]
        return {}

    async def restart_process(self, name: str):
        self.restart_calls.append(name)
        if name in self._fail_restart:
            raise self._fail_restart[name]
        return {}

    async def list_processes(self):
        return self._list_processes_result

    async def update_project(self, yaml_text: str):
        self.update_project_calls.append(yaml_text)
        return {}


_SERVERS = "localhost:9092"


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- agent_start: duplicate guard (§3.5) ------------------------------------


async def test_agent_start_refuses_duplicate_when_probe_shows_live(tmp_path, capsys):
    """Name already live anywhere → no duplicate, clear message, exit 0 (§3.5).

    The guard is broker-wide (the probe), so a name running on ANY host is caught
    CLI-side without a bridge change. It is a benign no-op, not a failure: return
    0, and crucially do NOT call ``start_process`` (no second instance).
    """
    client = _StubClient()
    probe = _StubProbe([_defn("assistant")])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == []  # the whole point: no duplicate started
    assert probe.calls == [_SERVERS]  # the guard actually queried the org
    out = capsys.readouterr().out
    assert "already running in the organization" in out
    assert "assistant" in out


# --- agent_start: already-running-here is a restart (behavior #2) ------------


async def test_agent_start_local_running_restarts_not_duplicate_refusal(tmp_path, capsys):
    """`start` on a name that is Running on THIS host → restart, not the refusal.

    Behavior #2: a re-`start` of a locally-running instance is the useful
    idempotency — reload it in place (the same effect as `restart`). This must take
    precedence over the org-wide duplicate guard: the guard refuses (and tells you
    it is running elsewhere), but a *local* running instance is ours to restart, so
    we never reach the probe and never print the refusal.
    """
    client = _StubClient(
        list_processes_result=[{"name": "assistant", "status": "Running"}]
    )
    # The probe would also report it live; the local-running branch must win BEFORE
    # the guard, so the probe is never consulted.
    probe = _StubProbe([_defn("assistant")])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == ["assistant"]  # restarted in place
    assert client.start_calls == []  # NOT a fresh start
    assert probe.calls == []  # local-running short-circuits before the org probe
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "restarted" in out
    assert "already running in the organization" not in out


async def test_agent_start_remote_running_keeps_duplicate_refusal(tmp_path, capsys):
    """`start` on a name live on ANOTHER host (not local) → keep the §3.5 refusal.

    Behavior #2 only restarts a *local* running instance; a name answering on a
    different host is the duplicate-guard case (starting a second would
    double-reply / split-brain), so refuse with the org-wide message and return 0.
    """
    client = _StubClient(
        list_processes_result=[]  # not running on THIS host
    )
    probe = _StubProbe([_defn("assistant")])  # answering on another host

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == []  # not ours to restart
    assert client.start_calls == []  # no duplicate started
    assert probe.calls == [_SERVERS]  # the guard did query the org
    out = capsys.readouterr().out
    assert "already running in the organization" in out


# --- agent_start: happy path ------------------------------------------------


async def test_agent_start_happy_path_starts_when_roster_empty(tmp_path, capsys):
    """Not live anywhere → start the local disabled slot, report online, exit 0."""
    client = _StubClient()
    probe = _StubProbe([])  # nobody live → free to start

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "online" in out


async def test_agent_start_ignores_other_live_agents(tmp_path):
    """A different agent being live must not block starting this one."""
    client = _StubClient()
    probe = _StubProbe([_defn("scheduler")])  # someone else is live

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]


# --- agent_start: workspace down --------------------------------------------


async def test_agent_start_workspace_down_short_circuits(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, probe/start NOT called.

    The workspace check is first: with no supervisor there is nothing to start,
    so we must not waste a broker probe or attempt a start that would only raise.
    """
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert probe.calls == []  # cheap-out: no broker probe when nothing can start
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "calfcord start" in out


class _RaisingProbe:
    """A probe that raises (the broker being unreachable) to test degradation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, server_urls: str):
        self.calls.append(server_urls)
        raise RuntimeError("broker unreachable")


async def test_agent_ps_physical_excludes_all_non_agent_processes(tmp_path, capsys):
    """The physical half of `ps` lists only AGENTS — not the substrate
    (broker/bridge) and not the other non-agent processes (tools/router/mcp)."""
    client = _StubClient(
        list_processes_result=[
            {"name": "broker", "status": "Running"},
            {"name": "bridge", "status": "Running"},
            {"name": "tools", "status": "Running"},
            {"name": "router", "status": "Running"},
            {"name": "mcp", "status": "Running"},
            {"name": "assistant", "status": "Running"},
        ]
    )
    probe = _StubProbe([])  # nothing logical → the board shows only physical agents

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    for non_agent in ("broker", "bridge", "tools", "router", "mcp"):
        assert non_agent not in out


async def test_agent_ps_tolerates_probe_failure(tmp_path, capsys):
    """A broker hiccup must not crash read-only `ps`: degrade to physical-only."""
    client = _StubClient(
        list_processes_result=[{"name": "assistant", "status": "Running"}]
    )
    probe = _RaisingProbe()

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    assert probe.calls == [_SERVERS]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "broker unreachable" in out.lower()


async def test_agent_start_tolerates_probe_failure_and_proceeds(tmp_path, capsys):
    """If the org-wide duplicate probe fails (broker unreachable), warn and proceed
    with the local start rather than crashing."""
    client = _StubClient()
    probe = _RaisingProbe()

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]
    out = capsys.readouterr().out.lower()
    assert "could not verify" in out or "broker unreachable" in out


# --- agent_start: not declared in the running project (§13.1) ----------------


async def test_agent_start_not_declared_asks_for_reload_no_update_project(
    tmp_path, capsys
):
    """start_process raises (brand-new agent) → reload message, exit 1, no update.

    A new agent authored after ``calfcord start`` is not a declared slot, so the
    PC server errors on start with a 4xx. §13.1: bringing it online needs a
    workspace reload (stop && start) because an in-place ``update_project`` bounces
    the substrate — so we must NOT call ``update_project``. The reload hint is
    reserved for this STRUCTURAL 4xx not-declared case (Fix #9).
    """
    client = _StubClient(
        start_raises=ProcessComposeError(
            "start_process: ... HTTP 404: process newbie is not defined",
            status_code=404,
        )
    )
    probe = _StubProbe([])  # not live → guard passes → we reach start_process

    rc = await roster.agent_start(
        _home(tmp_path),
        name="newbie",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert client.start_calls == ["newbie"]  # we did attempt the start
    assert client.update_project_calls == []  # §13.1: never bounce the substrate
    out = capsys.readouterr().out
    assert "newbie" in out
    # The message must steer to a reload, not leave the operator stranded.
    assert "calfcord stop" in out
    assert "calfcord start" in out


async def test_agent_start_server_error_raises_loudly_not_reload_hint(tmp_path):
    """A 5xx on start is a genuine infra fault → raise loudly (Fix #9).

    §13.1's reload hint is reserved for the STRUCTURAL 4xx not-declared case. A 5xx
    (a wedged supervisor) is not "a brand-new agent"; mistranslating it into the
    benign reload hint would mask a real fault. Per the error convention it must
    raise with caller/target/correlation context, carrying the PC body.
    """
    pc_error = ProcessComposeError(
        "start_process: process-compose POST /process/start/assistant "
        "failed with HTTP 500: internal supervisor error",
        status_code=500,
    )
    client = _StubClient(start_raises=pc_error)
    probe = _StubProbe([])  # not live → guard passes → we reach start_process

    with pytest.raises(RuntimeError) as excinfo:
        await roster.agent_start(
            _home(tmp_path),
            name="assistant",
            server_urls=_SERVERS,
            client=client,
            probe=probe,
        )

    message = str(excinfo.value)
    # The target agent is named (correlation), and PC's body survives.
    assert "assistant" in message
    assert "500" in message
    assert client.start_calls == ["assistant"]  # we did attempt the start
    assert client.update_project_calls == []  # still never bounce the substrate


async def test_agent_start_unknown_error_raises_loudly(tmp_path):
    """A raise with no structural status is a genuine fault → raise loudly (Fix #9).

    A bare ``RuntimeError`` (status_code absent → ``None``) is NOT the 4xx
    not-declared case, so it must not be mistranslated into the benign reload hint;
    it propagates as the infra fault it is.
    """
    client = _StubClient(start_raises=RuntimeError("unexpected transport blowup"))
    probe = _StubProbe([])

    with pytest.raises(RuntimeError):
        await roster.agent_start(
            _home(tmp_path),
            name="assistant",
            server_urls=_SERVERS,
            client=client,
            probe=probe,
        )


# --- agent_stop -------------------------------------------------------------


async def test_agent_stop_happy_path(tmp_path, capsys):
    """Workspace up → PATCH stop, report stopped, exit 0."""
    client = _StubClient()

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=client)

    assert rc == 0
    assert client.stop_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "stopped" in out


async def test_agent_stop_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no stop issued."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "calfcord start" in out


# --- agent_restart ----------------------------------------------------------


async def test_agent_restart_happy_path(tmp_path, capsys):
    """Workspace up → POST restart (reload after an edited .md), exit 0."""
    client = _StubClient()

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=client)

    assert rc == 0
    assert client.restart_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "restarted" in out


async def test_agent_restart_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no restart issued."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=client)

    assert rc == 1
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_start_all: sweep every DEFINED agent (behavior #1) ---------------


async def test_agent_start_all_mixes_running_stopped_and_remote(tmp_path, capsys):
    """`start --all` runs the single-start logic per DEFINED id (decision B).

    The caller passes the defined ids; each runs through `agent_start`'s body, so a
    locally-running one restarts (behavior #2), a stopped one starts, and one only
    answering on another host hits the duplicate-refusal. All three are honored in
    one sweep; every id gets a per-item line; an all-success sweep returns 0.
    """
    client = _StubClient(
        list_processes_result=[{"name": "local_up", "status": "Running"}]
    )
    probe = _StubProbe([_defn("remote")])  # answering elsewhere, not here

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["local_up", "stopped", "remote"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == ["local_up"]  # running here → restarted
    assert client.start_calls == ["stopped"]  # not anywhere → started
    out = capsys.readouterr().out
    assert "restarted" in out  # local_up
    assert "online" in out  # stopped
    assert "already running in the organization" in out  # remote


async def test_agent_start_all_empty_defined_set(tmp_path, capsys):
    """No defined agents → an explicit "none" line and a clean 0."""
    client = _StubClient()
    probe = _StubProbe([])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=[],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "no agents defined" in out


async def test_agent_start_all_continues_past_a_hard_failure_returns_1(tmp_path, capsys):
    """A hard failure on one id must not abort the sweep; return 1 if any failed.

    Best-effort: a 5xx (genuine infra fault) on one id raises inside the single
    start, but the sweep catches it, keeps going for the rest, and reports a
    non-zero summary so the operator knows something needs attention.
    """
    boom = ProcessComposeError("HTTP 500: wedged supervisor", status_code=500)
    client = _StubClient(fail_start={"bad": boom})
    probe = _StubProbe([])  # nobody live → each id reaches start_process

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["good", "bad", "later"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1  # at least one hard-failed
    # The sweep continued past the failure: every id was attempted, including the
    # one AFTER the failing one.
    assert client.start_calls == ["good", "bad", "later"]


async def test_agent_start_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing started."""
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["assistant"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert client.start_calls == []
    assert probe.calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_stop_all: every LOCAL Running agent (behavior #1) -----------------


async def test_agent_stop_all_targets_only_running_local_agents(tmp_path, capsys):
    """`stop --all` stops every Running local AGENT — never the substrate/singletons.

    The target set is the same physical filter `ps` uses: Running processes whose
    name is not a reserved (substrate/tools/router/mcp) process. A Stopped agent is
    not a target. An all-success sweep returns 0.
    """
    client = _StubClient(
        list_processes_result=[
            {"name": "broker", "status": "Running"},  # substrate — never
            {"name": "tools", "status": "Running"},  # singleton — never
            {"name": "router", "status": "Running"},  # singleton — never
            {"name": "mcp", "status": "Running"},  # singleton — never
            {"name": "assistant", "status": "Running"},  # agent → stop
            {"name": "scheduler", "status": "Running"},  # agent → stop
            {"name": "dormant", "status": "Stopped"},  # not Running → skip
        ]
    )

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 0
    assert sorted(client.stop_calls) == ["assistant", "scheduler"]
    for never in ("broker", "tools", "router", "mcp", "dormant"):
        assert never not in client.stop_calls


async def test_agent_stop_all_empty_running_set(tmp_path, capsys):
    """No agents running locally → an explicit "none" line and a clean 0."""
    client = _StubClient(list_processes_result=[{"name": "broker", "status": "Running"}])

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "no agents running locally" in out


async def test_agent_stop_all_continues_past_a_failure_returns_1(tmp_path, capsys):
    """One failing stop must not abort the sweep; return 1 if any failed."""
    client = _StubClient(
        list_processes_result=[
            {"name": "a", "status": "Running"},
            {"name": "b", "status": "Running"},
            {"name": "c", "status": "Running"},
        ],
        fail_stop={"b": RuntimeError("stop blew up")},
    )

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 1
    assert sorted(client.stop_calls) == ["a", "b", "c"]  # every one attempted


async def test_agent_stop_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing stopped."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_restart_all: every LOCAL Running agent (behavior #1) --------------


async def test_agent_restart_all_targets_only_running_local_agents(tmp_path, capsys):
    """`restart --all` restarts every Running local AGENT (same target as stop)."""
    client = _StubClient(
        list_processes_result=[
            {"name": "bridge", "status": "Running"},  # substrate — never
            {"name": "assistant", "status": "Running"},  # agent → restart
            {"name": "dormant", "status": "Stopped"},  # not Running → skip
        ]
    )

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.restart_calls == ["assistant"]
    assert "bridge" not in client.restart_calls
    assert "dormant" not in client.restart_calls


async def test_agent_restart_all_empty_running_set(tmp_path, capsys):
    """No agents running locally → an explicit "none" line and a clean 0."""
    client = _StubClient(list_processes_result=[])

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "no agents running locally" in out


async def test_agent_restart_all_continues_past_a_failure_returns_1(tmp_path, capsys):
    """One failing restart must not abort the sweep; return 1 if any failed."""
    client = _StubClient(
        list_processes_result=[
            {"name": "a", "status": "Running"},
            {"name": "b", "status": "Running"},
        ],
        fail_restart={"a": RuntimeError("restart blew up")},
    )

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 1
    assert sorted(client.restart_calls) == ["a", "b"]  # every one attempted


async def test_agent_restart_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing restarted."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 1
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_ps: the three-way union (§3.4) -----------------------------------


def _pc_proc(name: str, status: str) -> dict:
    """A process row as ``list_processes`` returns it (name + status)."""
    return {"name": name, "status": status}


async def test_agent_ps_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 0, probe NOT called.

    ps with no workspace is an expected state (you haven't opened the office),
    not an error — so exit 0, and don't spend a broker probe.
    """
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    assert probe.calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


async def test_agent_ps_union_three_cases(tmp_path, capsys):
    """The union renders all three §3.4 states distinctly.

    * ``assistant`` — physical (Running here) AND logical (answering) → registered.
    * ``ghost`` — physical (Running here) but NOT logical → started, not yet
      registered (the wedged/just-starting case).
    * ``remote`` — logical (answering) but NOT physical here → running on another
      host (expected multi-host, NOT an error).

    Substrate processes (broker/bridge) are excluded from the roster board, and a
    non-Running physical process is not counted as physically up.
    """
    client = _StubClient(
        list_processes_result=[
            _pc_proc("broker", "Running"),  # substrate — excluded
            _pc_proc("bridge", "Running"),  # substrate — excluded
            _pc_proc("assistant", "Running"),  # physical + logical
            _pc_proc("ghost", "Running"),  # physical only
            _pc_proc("stopped", "Stopped"),  # not Running → not physically up
        ]
    )
    probe = _StubProbe([_defn("assistant"), _defn("remote")])

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    out = capsys.readouterr().out

    # assistant: both halves → registered/running.
    assert "assistant" in out
    # ghost: physical up, not answering → "not yet registered".
    assert "ghost" in out
    assert "not yet registered" in out
    # remote: answering elsewhere, not local → "running on another host".
    assert "remote" in out
    assert "another host" in out

    # Substrate must not appear on the roster board.
    for line in out.splitlines():
        # broker/bridge can legitimately appear inside a sentence; assert they
        # are not rendered as roster rows by checking no row leads with them.
        stripped = line.strip()
        assert not stripped.startswith("broker")
        assert not stripped.startswith("bridge")
    # A non-Running declared process is not "physically up", so it is not shown
    # as running here (it is neither logical nor physically-up).
    assert "stopped" not in out


async def test_agent_ps_empty(tmp_path, capsys):
    """Workspace up but nothing running anywhere → an explicit empty board, exit 0."""
    client = _StubClient(list_processes_result=[_pc_proc("broker", "Running")])
    probe = _StubProbe([])

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() != ""  # must render *something*, not a blank board


async def test_agent_ps_tolerates_data_wrapped_and_nondict_rows(tmp_path, capsys):
    """The physical view survives the ``{"data": [...]}`` shape and junk rows.

    Process Compose's process-list wire shape wobbles across versions (bare list
    vs. ``{"data": [...]}``); a non-dict entry must be skipped, not crash the
    board. Both robustness paths are pinned so a shape wobble never blanks ps.
    """
    client = _StubClient(
        list_processes_result={
            "data": [
                _pc_proc("assistant", "Running"),
                "not-a-dict",  # defensive skip, must not crash
            ]
        }
    )
    probe = _StubProbe([_defn("assistant")])

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=client, probe=probe
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "running" in out


# --- default wiring (production seams, no real broker) -----------------------


def test_agent_start_defaults_to_per_home_process_compose_client(tmp_path):
    """With no ``client`` injected, the per-home ``ProcessComposeClient`` is built.

    The default REST client must target the port :func:`lifecycle.pc_port_for`
    derives from ``$CALFCORD_HOME`` — the same port ``up -p`` pins — so a second
    install on one host does not collide. Asserting the resolver wiring (not a
    live call) keeps this unit-pure.
    """
    from calfcord.supervisor.client import ProcessComposeClient
    from calfcord.supervisor.lifecycle import pc_port_for

    home = _home(tmp_path)
    client = roster._resolve_client(None, home)

    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    # The base URL bakes in the derived port; equal base URLs prove equal ports.
    assert client._base_url == expected._base_url


async def test_default_probe_delegates_to_probe_live_roster(monkeypatch):
    """The default probe adapts ``probe_live_roster`` to the injectable shape.

    With no ``probe`` injected, the resolver returns a closure that calls
    :func:`control_plane.probe.probe_live_roster` with the given ``server_urls``.
    Monkeypatching that function lets the closure body run with no real broker,
    pinning the production delegation (the seam tests otherwise bypass).
    """
    seen: dict[str, str] = {}

    async def _fake_probe_live_roster(server_urls: str):
        seen["server_urls"] = server_urls
        return [_defn("assistant")]

    monkeypatch.setattr(roster, "probe_live_roster", _fake_probe_live_roster)

    default_probe = roster._resolve_probe(None)
    result = await default_probe(_SERVERS)

    assert seen["server_urls"] == _SERVERS
    assert [d.agent_id for d in result] == ["assistant"]


# --- import-lightness (decoupling invariant) --------------------------------

# Must run in a *subprocess* (the ``test_import_isolation.py`` pattern): other
# tests in the full suite import ``calfcord.mcp.config`` in-process, which would
# pollute ``sys.modules`` and make an in-process assertion vacuously false. A
# fresh interpreter gives a clean ``sys.modules`` to assert against.
_ROSTER_ISOLATION_SCRIPT = """
import sys

import calfcord.supervisor.roster  # noqa: F401

leaked = [m for m in sys.modules if m == "calfcord.mcp.config"]
assert not leaked, (
    "roster import pulled in the bridge-only MCP secrets loader: "
    + repr(leaked)
)
print("ROSTER_ISOLATION_OK")
"""


def test_roster_module_does_not_import_mcp_config():
    """roster.py must stay off the bridge-only MCP-secrets path (CLAUDE.md, §12.3).

    Importing ``calfcord.supervisor.roster`` must not drag in
    ``calfcord.mcp.config`` (transport + ``$VAR`` secrets), preserving the
    CLI/agent-side import isolation so the roster ops stay importable on a host
    that holds no MCP credentials.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _ROSTER_ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ROSTER_ISOLATION_OK" in result.stdout, (
        "isolation subprocess exited 0 but did not run to completion "
        f"(no sentinel)\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
