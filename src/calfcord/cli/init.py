"""``disco init`` — one continuous, resumable guided setup that ends LIVE.

This is the onboarding alternative to hand-editing ``.env`` *and* hand-writing an
``agents/<name>.md``. It walks the operator through one agent end to end, then —
on a native install — *opens the workspace, brings the agent online, and waits
until the agent registers on the live mesh*. The flow is the §4.6 / §11
"ends-live" experience: time-to-online over everything.

Composition, not reinvention
----------------------------
``init`` is a **composer**. Each cohesive unit lives in its own module and ``init``
only sequences them, so the wizard stays unit-testable and the pieces stay
reusable:

* **Agent + provider + model** — delegated wholesale to
  :func:`calfcord.cli.agent_create.create_agent` (the ONE shared create flow,
  which ``agent create`` also uses, so the two can't drift). ``init`` opts into
  pruning the pristine starter and persists the chosen provider as the install
  default.
* **Discord** — :func:`_run_discord` composes :mod:`calfcord.cli.discord_discovery`
  (verify-token-on-paste, the invite link + intents reminder, block-and-poll
  until the bot joins, then a guild pick-list plus a report-only postability
  preflight) in place of the old "paste a numeric ID" prompts (§4.5).
* **Live finish** — :func:`_run_finish` composes
  :func:`calfcord.supervisor.lifecycle.start` (substrate, health-gated) →
  :func:`calfcord.supervisor.roster.agent_start` (the agent clocks in) → an
  in-flow ``@<agent> hello`` prompt → online-presence detection on the mesh
  (:func:`_wait_for_agent_online`, §4.6 / §12.6). On a dev run (no install) or a
  missing supervisor binary it DEGRADES to honest manual next-steps rather than
  orchestrating something it cannot.
* **Resumability** — :mod:`calfcord.cli.setup_state` records *which steps are
  done* so a crash / Ctrl-C / the unavoidable browser detour resumes ("Welcome
  back …") instead of restarting. The checkpoint is **advisory** (§12.7): every
  resumed step RE-VERIFIES the real artifact (agent ``.md`` parses? token still
  valid?) before skipping — the world is ground truth, the checkpoint only
  chooses *where* to resume.

Injected seams
--------------
All prompting goes through an injected :class:`Prompter`; every world-touching
dependency — the Discord HTTP calls, the substrate/roster coroutines, the
online-presence watcher, the process-compose binary probe, and the clock — is a
keyword-only injectable defaulting to the real thing. So the whole wizard runs in
a unit test with no TTY, no Discord, no broker, and no supervisor.

Two invariants the design pins:

* **Idempotent and non-destructive to secrets.** Re-running treats an empty
  answer as "keep what's there" for every ``.env`` secret, and defaults a re-run
  to the saved (working) guild binding rather than clobbering it (§12.7).
* **No green light that lies.** The finish only celebrates once the agent is
  *seen online* on the mesh AND the postability preflight didn't already find the
  bot can't post in any channel; a clean timeout downgrades to an honest "try it
  yourself / run doctor" hint, and a substrate that never reaches ready stops the
  flow instead of clocking an agent into a workspace that isn't up (§12.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from calfcord.cli import _envfile, _supervisor, discord_discovery, setup_state
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.agent_create import create_agent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from calfcord.cli.discord_discovery import BotIdentity, ChannelListing, Guild, PostableChannel

_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"
_BROKER_VAR = "CALF_HOST_URL"
_LOCAL_BROKER_URL = "localhost:9092"

# How long the live finish waits for the agent to come online before downgrading
# to the honest "try it yourself" hint. Bounded so init never hangs on an agent
# that never registers — the §12.6 fallback is the safety net, not a failure.
_FIRST_REPLY_TIMEOUT_S = 60.0

# How often the online-presence watcher re-reads the mesh, and the per-read bound
# on the mesh view's open-time catch-up. Small so a brand-new org — whose
# ``calf.agents`` topic is not created until the first agent registers — fails a
# read fast and retries within the window rather than blocking on a missing topic.
_ONLINE_POLL_INTERVAL_S = 0.5
_ONLINE_CATCHUP_TIMEOUT_S = 5.0


async def _wait_for_agent_online(
    server_urls: str,
    *,
    agent_id: str,
    timeout_s: float,
) -> bool:
    """Poll calfkit's mesh until ``agent_id`` is online, or ``timeout_s`` elapses.

    The live-finish confirmation, replacing the deleted control-plane first-reply
    watcher. It proves the agent's PRESENCE — it registered on the ``calf.agents``
    mesh at startup — not an end-to-end message reply; the org is already live once
    the agent is online, so presence is the honest "it worked" signal.

    Opens a short-lived observer :class:`~calfkit.client.Client`, then reads
    ``client.mesh.get_agents()`` on a small interval until the name appears
    (-> ``True``) or the window elapses (-> ``False``). No broker pre-flight — the
    read raises at call time if the mesh can't be reached; a
    :class:`~calfkit.exceptions.MeshUnavailableError` (most often the ``calf.agents``
    topic not existing until the first agent registers, or the broker being down) is
    treated as "not online yet" and retried until ``timeout_s`` elapses, at which
    point the caller downgrades a ``False`` to the honest fallback. The mesh view is
    a compacted-topic (ktable) reader — a *level-triggered* read of who is currently
    online. ``agent_start`` only confirms the agent's process spawned; registration on
    the mesh completes asynchronously afterward (it may land after this opens), so the
    safety here is two-part: the read catches a registration that already landed, and
    the bounded poll (up to ``timeout_s``) catches one still in flight. Either way
    there is no lost-registration race — which is why the caller can run this AFTER the
    human prompt rather than concurrently with it. (Don't drop the poll for a single
    snapshot: a slow-booting agent would then be missed.)
    """
    from calfkit import MeshViewConfig
    from calfkit.client import Client
    from calfkit.exceptions import MeshUnavailableError

    client = Client.connect(server_urls, mesh_config=MeshViewConfig(catchup_timeout=_ONLINE_CATCHUP_TIMEOUT_S))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                agents = await client.mesh.get_agents()
                if agent_id in agents:
                    return True
            except MeshUnavailableError:
                pass  # topic absent / still establishing — the agent is still coming up
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_ONLINE_POLL_INTERVAL_S)
    finally:
        # A close that raises must not turn a confirmed ``True`` into the exception the
        # caller degrades to ``False`` — a cleanup hiccup is not a presence failure.
        with contextlib.suppress(Exception):
            await client.aclose()


# The reboot-non-survival fact, stated honestly (§12.6: the daemon is
# session-scoped, not init-managed). Kept as one constant so the live-finish and
# manual-degrade paths can't drift on the core claim while wording their own
# follow-up.
_REBOOT_NOTE = "The workspace runs for this session only — it does not survive a reboot."


def resolve_paths(home: Path | None) -> tuple[Path, Path]:
    """Resolve ``(env_path, agents_dir)`` for the current run.

    Native installs pass ``home`` (``$CALFCORD_HOME``): config lives at
    ``home/config/.env`` and agents at ``home/agents`` — unless the operator
    pinned a different agents dir via ``CALFKIT_AGENTS_DIR``, which the shim and
    runners already honour, so ``init``'s detection must agree with them.

    Dev / ``uv run calfcord-cli init`` passes ``home=None``: config is the
    project-local ``./.env`` and agents the project-local ``./agents`` (again
    overridable by ``CALFKIT_AGENTS_DIR``), matching the non-shim defaults.
    """
    agents_override = os.environ.get("CALFKIT_AGENTS_DIR")
    if home is not None:
        env_path = home / "config" / ".env"
        agents_dir = Path(agents_override) if agents_override else home / "agents"
    else:
        env_path = Path(".env")
        agents_dir = Path(agents_override) if agents_override else Path("agents")
    return env_path, agents_dir


def _set_label(value: str) -> str:
    """Render a secret's presence without leaking it: '(currently set)' / '(not set)'."""
    return "(currently set)" if value else "(not set)"


def _agent_md_parses(agents_dir: Path, name: str) -> bool:
    """True iff ``agents_dir/<name>.md`` exists *and* parses (the re-verify gate).

    The §12.7 advisory contract: a checkpoint saying "agent done" is only trusted
    when the real artifact is actually there and loadable — a deleted or corrupted
    ``.md`` means the wizard re-walks the create step rather than skipping it on a
    stale flag. ``parse_agent_md`` is imported lazily so a dev run that bails
    before this never pays the agents-definition import cost.
    """
    md = agents_dir / f"{name}.md"
    if not md.is_file():
        return False
    from calfcord.agents.definition import parse_agent_md

    try:
        parse_agent_md(md)
    except (ValueError, OSError):
        return False
    return True


def run(
    prompter: Prompter,
    *,
    env_path: Path,
    agents_dir: Path,
    home: Path | None = None,
    server_urls: str = "localhost",
    # --- injected world-touching seams (default to the real thing) ----------
    verify_identity_fn: Callable[..., BotIdentity] | None = None,
    poll_joined_fn: Callable[..., list[Guild]] | None = None,
    list_guilds_fn: Callable[..., list[Guild]] | None = None,
    list_channels_fn: Callable[..., ChannelListing] | None = None,
    start_fn: Callable[..., Awaitable[int]] | None = None,
    agent_start_fn: Callable[..., Awaitable[int]] | None = None,
    first_reply_fn: Callable[..., Awaitable[bool]] | None = None,
    pc_binary_fn: Callable[[], str] | None = None,
    now: Callable[[], datetime] | None = None,
    open_url_fn: Callable[[str], None] | None = None,
) -> int:
    """Run the guided, resumable, ends-live setup flow and return an exit code.

    Phases, in order: **(1)** agent identity + provider + model + tools + write
    (the shared :func:`create_agent`), **(2)** Discord (:func:`_run_discord`),
    **(3)** broker, **(4)** the live finish (:func:`_run_finish`). A checkpoint is
    saved after each completed phase so a Ctrl-C resumes; each resumed phase
    re-verifies its real artifact before skipping (advisory, §12.7).

    Every ``.env`` write goes through :func:`_envfile.upsert`; an empty secret
    answer keeps the existing value (re-run safe). ``server_urls`` is the broker
    URL ``main.py`` sampled from ``CALF_HOST_URL`` BEFORE the wizard ran; it is a
    pre-wizard hint only — the broker phase (§3) may write a different
    ``CALF_HOST_URL``, so the live finish re-reads the EFFECTIVE value from the
    just-written ``.env`` (same ``value or "localhost"`` default the runners use)
    rather than trusting this. The injected seams are the test surface —
    production defaults wire the real :mod:`discord_discovery`, :mod:`supervisor`,
    and first-reply modules.
    """
    verify_identity_fn = verify_identity_fn or discord_discovery.verify_bot_identity
    poll_joined_fn = poll_joined_fn or discord_discovery.poll_until_joined
    list_guilds_fn = list_guilds_fn or discord_discovery.list_guilds
    list_channels_fn = list_channels_fn or discord_discovery.list_postable_channels

    checkpoint_file = setup_state.checkpoint_path(home)
    checkpoint = setup_state.load(checkpoint_file) or setup_state.SetupCheckpoint()

    print("disco init — configuring", env_path)
    # Advisory resume greeting: only when the checkpoint claims the agent step is
    # done AND the real .md still parses (re-verify, never trust the flag alone).
    resuming = (
        checkpoint.provider_done
        and checkpoint.agent_name is not None
        and _agent_md_parses(agents_dir, checkpoint.agent_name)
    )
    if resuming:
        # Honest wording (nit #18a): the resume RE-WALKS the create flow rather
        # than skipping it, so don't claim the agent step is settled. We pre-fill
        # the create defaults from the saved agent (a blank answer keeps it) — the
        # re-walk confirms/edits in place, it doesn't restart from scratch.
        print(
            f"Welcome back — picking up where you left off (agent {checkpoint.agent_name}). "
            "Press enter to keep each saved answer."
        )
    else:
        # First-run orientation: the very next thing is the agent-create flow, so
        # say plainly what is being built and how the pre-filled prompts behave. A
        # bare "? Agent name:" gives no hint that the shown value is an editable
        # suggestion or why it is being asked — spell out the Enter-to-accept /
        # type-to-change mechanic once, up front, for the whole wizard.
        print("Now we'll create your agent — its name, description, model, and tools.")
        print(
            "Each prompt shows a suggested value: press Enter to accept it, or type a "
            "new one and press Enter."
        )
    print()

    # --- Phase 1: agent identity + provider + model + tools + write --------
    # Delegated wholesale to the shared create flow so ``agent create`` and
    # ``init`` can't drift on how an agent is made. A write failure means no
    # usable agent landed, so abort before Discord / broker / the live finish.
    try:
        created = create_agent(
            prompter,
            agents_dir=agents_dir,
            env_path=env_path,
            # On resume, default the name prompt to the agent the operator was
            # mid-creating (nit #18b): in a 2+-agent install ``create_agent``'s own
            # default falls back to the starter (its lone-existing rule needs
            # exactly one agent), so without this the re-walk would re-create under
            # the wrong name. A fresh run passes None and lets that default logic own it.
            name_default=checkpoint.agent_name if resuming else None,
            prune_seed=True,
            offer_prompt=False,
            # Suppress the live-capability probe during setup: the broker phase
            # (§3) hasn't run yet, so a probe would dial the default/stale broker
            # — one the operator hasn't chosen — and could hang on a leftover
            # local broker. The tools editor still offers server-level
            # ``mcp/<server>`` rows from mcp.json (no broker); the live per-tool
            # rows come later via ``disco agent tools``, against the broker the
            # install actually configured.
            live_tools_fn=lambda: {},
        )
    except (ValueError, OSError) as e:
        print(f"error: could not create agent: {e}")
        return 1
    name = created.name
    _envfile.upsert(env_path, {_DEFAULT_PROVIDER_VAR: created.provider})
    checkpoint = checkpoint.model_copy(update={"provider_done": True, "agent_name": name})
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # --- Phase 2: Discord (verify → invite → poll → pick guild) ------------
    checkpoint, postable = _run_discord(
        prompter,
        env_path=env_path,
        checkpoint=checkpoint,
        verify_identity_fn=verify_identity_fn,
        poll_joined_fn=poll_joined_fn,
        list_guilds_fn=list_guilds_fn,
        list_channels_fn=list_channels_fn,
        open_url_fn=open_url_fn or _try_open_browser,
    )
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # --- Phase 3: broker ---------------------------------------------------
    _run_broker(prompter, env_path=env_path)
    checkpoint = checkpoint.model_copy(update={"broker_done": True})
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # Re-read the EFFECTIVE broker URL the broker phase just wrote, rather than
    # the pre-wizard ``server_urls`` (sampled by main.py BEFORE the wizard ran).
    # The operator can configure a different broker inside the wizard, so the
    # live finish's lifecycle.start broker probe AND the first-reply watcher must
    # connect to what is now on disk — using the SAME ``value or "localhost"``
    # default the runners (main.py ``_run_lifecycle``) resolve from the env, so
    # all three agree on the broker the install actually talks to.
    effective_server_urls = _envfile.read_env(env_path).get(_BROKER_VAR) or "localhost"

    # Re-sync THIS process's environment to the .env we just wrote before the
    # finish spawns the workspace. The broker/bridge/agents launch as detached
    # children that inherit this env, but disco init was started (via the shim's
    # `uv run --env-file config/.env`) when that file still held the seed's empty
    # DISCORD_* placeholders — so our os.environ carries those empties. `uv
    # run --env-file` can't override an already-set var, and pydantic-settings
    # ranks env vars above the .env file, so without this the bridge would read
    # the stale empties and die on DiscordSettings even though the real values are
    # now on disk. The values the operator just entered are authoritative for the
    # workspace we're about to launch, so push them into the environment children
    # inherit. (Empty entries like a never-set DISCORD_OWNER_USER_ID are harmless:
    # DiscordSettings treats an empty value as unset — see its env_ignore_empty.)
    os.environ.update(_envfile.read_env(env_path))

    # --- Phase 4: live finish (or honest degrade) --------------------------
    return _run_finish(
        prompter,
        name=name,
        home=home,
        env_path=env_path,
        server_urls=effective_server_urls,
        postable=postable,
        start_fn=start_fn,
        agent_start_fn=agent_start_fn,
        first_reply_fn=first_reply_fn,
        pc_binary_fn=pc_binary_fn,
    )


def _try_open_browser(url: str) -> None:
    """Best-effort browser pop for the invite link; never raises.

    The URL is ALWAYS printed before this runs, so a wrong guess in either
    direction only costs the convenience, never the link (§12.6). Skipped when
    stdout isn't a terminal (piped/captured runs — including pytest — must
    never pop a tab), over SSH, and on display-less Linux, where
    ``webbrowser`` may fall back to a terminal browser and hijack the wizard.
    """
    if not sys.stdout.isatty():
        return
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return
    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _run_discord(
    prompter: Prompter,
    *,
    env_path: Path,
    checkpoint: setup_state.SetupCheckpoint,
    verify_identity_fn: Callable[..., BotIdentity],
    poll_joined_fn: Callable[..., list[Guild]],
    list_guilds_fn: Callable[..., list[Guild]],
    list_channels_fn: Callable[..., ChannelListing],
    open_url_fn: Callable[[str], None] = _try_open_browser,
) -> tuple[setup_state.SetupCheckpoint, bool | None]:
    """The Discord sub-flow: validate-on-paste → invite → poll → pick (§4.5/§12.6).

    Composes :mod:`discord_discovery` to replace the old numeric-ID prompts. The
    bot token is captured (keep-existing-on-empty), verified the instant it is
    pasted (echoing the bot's identity), then the operator invites the bot, the
    wizard block-and-polls until it joins, and a guild pick-list persists
    ``DISCORD_GUILD_ID``. There is no channel to pick — the bridge listens to
    every channel it can see — so the wizard only *reports* postability as a guard.

    Returns ``(checkpoint, postable)``: the checkpoint advanced with ``discord_done``
    and the chosen non-secret guild ID, plus a tri-state postability signal for the
    live finish — ``True`` (bot can post somewhere), ``False`` (bot can post nowhere),
    or ``None`` (not determined: no guild bound, or the listing failed). The whole
    sub-flow is non-fatal: a join timeout surfaces the §12.6 actionable hint and
    returns what progress it could make rather than aborting the wizard.
    """
    current = _envfile.read_env(env_path)
    print("Discord setup (the wizard discovers your server — no IDs to paste).")

    token, app_id = _capture_token(prompter, env_path, current, verify_identity_fn)
    if not token:
        # No token at all (fresh run, skipped): nothing to discover. The bridge
        # will fail-fast later; we surface it but keep going so the rest of the
        # config still lands and a re-run can finish Discord.
        print("  no Discord token set — skipping discovery; re-run init to finish Discord.")
        return checkpoint, None
    if not app_id:
        # The application id is DERIVED from the token's /applications/@me call, so a transient verify
        # failure (unreachable / rate-limited / unreadable) — or a freshly pasted token whose
        # application couldn't be confirmed — leaves no id to build the invite from. Degrade honestly
        # rather than emit a broken link; a re-run once Discord is reachable finishes it.
        print(
            "  couldn't determine the application id from the token — "
            "re-run `disco init` once Discord is reachable."
        )
        return checkpoint, None

    # Invite step: print the ready-made link + the privileged-intents reminder +
    # the resumability banner BEFORE the wait (§12.6 — Ctrl-C is safe here).
    invite = discord_discovery.invite_url(app_id)
    print()
    print("Invite the bot to your server — opening this link in your browser:")
    print(f"    {invite}")
    print("  (No browser tab? Copy the link above and open it yourself.)")
    print(f"  {discord_discovery.INTENTS_REMINDER}")
    print("  (Ctrl-C is safe & resumable — re-run `disco init` to pick up where you left off.)")
    # Pop the link in a browser AFTER printing it — best-effort only, and a
    # broken opener must never derail the wizard.
    with contextlib.suppress(Exception):
        open_url_fn(invite)
    print()
    print("Waiting for the bot to join a server…")

    try:
        guilds = poll_joined_fn(token)
    except discord_discovery.DiscordJoinTimeoutError:
        # The user never authorized within the budget. Surface the common causes
        # + the "I don't have a server" branch (§12.6) and degrade — the binding
        # is unset, but the rest of init still completes and a re-run finishes it.
        print("  the bot did not join a server in time. Common causes:")
        print("    - did you click Authorize on the invite link?")
        print("    - is the Message Content intent enabled (required; Server Members is recommended)?")
        print("    - do you have Manage Server on the server you tried to add it to?")
        print("  No server yet? Create one in Discord (the + button), then re-run `disco init`.")
        return checkpoint, None
    except discord_discovery.DiscordDiscoveryError as e:
        # Rate-limited / unreachable on the one-shot poll: surface and degrade.
        print(f"  could not confirm the bot joined ({e}); re-run init to finish Discord.")
        return checkpoint, None

    guild_id = _pick_guild(prompter, guilds, default=checkpoint.guild_id)
    if guild_id is None:
        return checkpoint, None
    _envfile.upsert(env_path, {"DISCORD_GUILD_ID": guild_id})

    # Advisory postability preflight (§12.6) — report-only, never blocks completion;
    # its result only lets the live finish avoid celebrating a bot that can't reply.
    postable = _report_postability(list_channels_fn, token, guild_id)

    return checkpoint.model_copy(update={"discord_done": True, "guild_id": guild_id}), postable


def _capture_token(
    prompter: Prompter,
    env_path: Path,
    current: dict[str, str],
    verify_identity_fn: Callable[..., BotIdentity],
) -> tuple[str, str]:
    """Prompt for the bot token (keep-existing-on-empty), verify it, and DERIVE the application id.

    Returns ``(token, application_id)``. The token comes from the prompt (freshly pasted or kept); the
    application id comes from the same single ``verify_identity_fn`` call (``GET /applications/@me``)
    that validates the token — so the operator never pastes the id by hand (§4.5). On a successful
    verify the token (fresh paste only, keep-on-empty otherwise) and ``DISCORD_APPLICATION_ID``
    (always, so a re-run self-heals a stale cached id) are persisted.

    A rejected token is fatal for *that* value, so we re-prompt for a fresh one (re-prompting the same
    token would be pointless, §12.6); a transient Discord error is surfaced but the token is still
    accepted so the rest of init can proceed (the bridge re-validates at boot anyway). On that
    transient path the id can't be derived, so we fall back to the cached ``DISCORD_APPLICATION_ID``
    ONLY when the token is unchanged — a freshly pasted, *different* token may belong to a different
    application, so we return no id and let the caller degrade rather than invite the wrong bot.
    """
    existing = current.get("DISCORD_BOT_TOKEN", "")
    cached_app_id = current.get("DISCORD_APPLICATION_ID", "")
    while True:
        pasted = prompter.secret(f"DISCORD_BOT_TOKEN {_set_label(existing)} — paste to set, enter to keep:")
        token = pasted or existing
        if not token:
            return "", ""
        try:
            identity = verify_identity_fn(token)
        except discord_discovery.DiscordAuthError:
            # The token Discord rejected is unusable; clear the kept value so an
            # empty answer can't "keep" the bad one, and ask for a fresh paste.
            print("  token rejected by Discord — paste a fresh bot token.")
            existing = ""
            continue
        except discord_discovery.DiscordDiscoveryError as e:
            # Couldn't reach Discord / rate-limited: don't block setup on a blip. The app id can't be
            # derived here, so fall back to the cached DISCORD_APPLICATION_ID — but ONLY when the token
            # is unchanged. A freshly pasted, DIFFERENT token may belong to a different application, so
            # trusting the old app's cached id would add the WRONG bot; drop it and degrade instead.
            print(f"  could not verify token right now ({e}); continuing — the bridge will re-check.")
            app_id = cached_app_id if token == existing else ""
            updates: dict[str, str] = {}
            if pasted:
                updates["DISCORD_BOT_TOKEN"] = pasted
            if not app_id and cached_app_id:
                # Also clear the now-stale cached id so disk never holds a mismatched (new token, old
                # app id) pair that a later kept-token re-run would satisfy ``token == existing`` on
                # and wrongly trust — closing the cross-run variant of the wrong-application hole.
                updates["DISCORD_APPLICATION_ID"] = ""
            _envfile.upsert(env_path, updates)
            return token, app_id
        print(f"  Connected as {identity.username} (id {identity.id}).")
        # A successful verify always yields the app id (verify_bot_identity raises otherwise), so
        # persist it unconditionally — self-healing a stale cached value — and the token only when
        # freshly pasted (an empty answer keeps the existing one).
        updates = {"DISCORD_APPLICATION_ID": identity.application_id}
        if pasted:
            updates["DISCORD_BOT_TOKEN"] = pasted
        _envfile.upsert(env_path, updates)
        return token, identity.application_id


def _pick_guild(prompter: Prompter, guilds: list[Guild], *, default: str | None) -> str | None:
    """Present a guild pick-list; return the chosen id, or ``None`` if none exist.

    Zero guilds is a legitimate surfaced outcome (the bot joined nowhere the API
    reports yet); we explain it rather than offering an empty menu. ``default``
    pre-selects a previously-saved binding so a re-run keeps the working guild
    (don't clobber it, §12.7).
    """
    if not guilds:
        print("  the bot isn't in any server the API reports yet — re-run init once it has joined one.")
        return None
    choices = [Choice(g.id, f"{g.name}{' (owner)' if g.owner else ''}") for g in guilds]
    return prompter.select(
        "Which server should the agent live in?",
        choices,
        default=default if any(g.id == default for g in guilds) else guilds[0].id,
    )


def _channel_names(channels: list[PostableChannel]) -> str:
    """Render channels as ``#general, #dev`` for the postability report lines."""
    return ", ".join(f"#{c.name}" for c in channels)


def _report_postability(
    list_channels_fn: Callable[..., ChannelListing],
    token: str,
    guild_id: str,
) -> bool | None:
    """Report whether the bot can post in the bound guild — the postability preflight.

    Not a picker and not persisted: the bridge listens to every channel it can see,
    so there is no default channel to choose. The effective-permission computation
    runs purely to SURFACE a green light that lies (§12.6) — a bot that is present
    but can never reply, for want of View Channel, Send Messages, or Manage Webhooks.
    Postable channels are confirmed, channels it can't post in are noted, and a
    can't-post-anywhere server is called out with a ``warning:``.

    Returns a tri-state the live finish uses to avoid celebrating a mute bot:
    ``True`` (a postable channel exists), ``False`` (none — the bot can post nowhere),
    or ``None`` (postability couldn't be determined because the listing failed). Every
    branch is advisory — nothing here blocks the wizard's completion.
    """
    try:
        listing = list_channels_fn(token, guild_id)
    except discord_discovery.DiscordAuthError:
        # The token was rejected (revoked/reset between the join-poll and now). Don't
        # falsely promise the bot will listen anywhere — the bridge won't even start.
        print("  warning: the bot token was rejected by Discord — the bridge won't start.")
        print("  Re-run `disco init` and paste a fresh bot token.")
        return None
    except discord_discovery.DiscordDiscoveryError as e:
        # Rate-limited / unreachable: we genuinely don't know postability, so say so
        # rather than claiming either outcome.
        print(f"  couldn't check postability ({e}) — if the bot never replies, verify it has")
        print("  View Channel + Send Messages + Manage Webhooks on a channel.")
        return None

    if listing.postable:
        print(f"  the bot can post in {len(listing.postable)} channel(s): {_channel_names(listing.postable)}")
        if listing.unpostable:
            print(f"  (note: the bot can't post in: {_channel_names(listing.unpostable)})")
        return True

    if listing.unpostable:
        print(
            f"  warning: the bot can't post in {_channel_names(listing.unpostable)} "
            "(needs View Channel + Send Messages + Manage Webhooks)."
        )
        print("  Grant those permissions on a channel (or the bot's role) so it can reply.")
    else:
        print("  warning: this server has no text channels the bot can post in — add one (or grant permissions).")
    return False


def _run_broker(prompter: Prompter, *, env_path: Path) -> None:
    """The broker step: a local Tansu (``CALF_HOST_URL=localhost:9092``) or a URL.

    Native is the default — the live finish starts the substrate (broker + bridge)
    detached, so unlike the old flow there is no command to print here. The URL
    branch keeps-existing-on-empty and warns only when a fresh install ends with
    no broker (the processes can't start without one).
    """
    current = _envfile.read_env(env_path)
    choice = prompter.select(
        "Start the agent mesh",
        [
            Choice("native", "Start a local mesh (recommended)"),
            Choice("url", "I have a broker URL (advanced)"),
        ],
        default="native",
    )
    if choice == "native":
        _envfile.upsert(env_path, {_BROKER_VAR: _LOCAL_BROKER_URL})
        return
    url = prompter.text(f"{_BROKER_VAR} (e.g. broker.example.com:9092):", default=current.get(_BROKER_VAR, ""))
    if url:
        _envfile.upsert(env_path, {_BROKER_VAR: url})
    elif not current.get(_BROKER_VAR):
        print(
            f"  warning: no {_BROKER_VAR} is set — the processes won't start until one "
            f"is (re-run 'disco init' or run 'disco self set-broker <url>')."
        )


def _run_finish(
    prompter: Prompter,
    *,
    name: str,
    home: Path | None,
    env_path: Path,
    server_urls: str,
    postable: bool | None = None,
    start_fn: Callable[..., Awaitable[int]] | None,
    agent_start_fn: Callable[..., Awaitable[int]] | None,
    first_reply_fn: Callable[..., Awaitable[bool]] | None,
    pc_binary_fn: Callable[[], str] | None,
) -> int:
    """The ends-live finish (§4.6 / §12.6): start substrate → agent → prompt hello → confirm presence.

    Only possible on a **native install** (the supervisor is install-scoped — its
    lock, derived REST port, generated YAML, logs, and shim launcher all live
    under ``$CALFCORD_HOME``). On a dev run (``home is None``) or when the
    process-compose binary is missing, this DEGRADES to honest manual next-steps
    instead of orchestrating something it cannot (no green light that lies).

    On the native happy path it runs three correctly-layered phases:
    :func:`_bring_online` (async) → an in-flow ``@<name> hello`` prompt (synchronous,
    no event loop running) → :func:`_await_presence` (async), mapping each failure to
    its specific hint:

    * substrate not ready → tear-down already happened in ``start``; point at
      ``disco logs`` / ``disco doctor`` (don't misattribute the cause) and stop
      (don't clock the agent into a workspace that isn't up);
    * agent start failed → stop before the presence watch;
    * agent seen online → 🎉 (unless the ``postable`` preflight already proved the
      bot can post nowhere, which downgrades to an honest "online but can't post");
      timed out → the bounded "org is live — try it yourself / run ``disco doctor``".

    The prompt is a blocking, synchronous terminal interaction — prompt_toolkit
    drives its OWN event loop (``Application.run()`` → ``asyncio.run()``) — so it must
    never run inside a loop of ours. Keeping it BETWEEN two independent
    ``asyncio.run`` calls (rather than awaited inside one) makes the nested-loop crash
    impossible by construction. Running the presence watch AFTER the prompt (rather
    than concurrently) is safe for the reason :func:`_wait_for_agent_online` documents:
    it is a level-triggered mesh read backed by a bounded poll, so it detects the agent
    whenever registration lands — before or after the watch opens.
    """
    pc_binary_fn = pc_binary_fn or _supervisor.default_pc_binary

    # Two distinct degrades, not one path: a dev run (``home is None`` — no
    # install-scoped supervisor by design) is benign and degrades silently; a native
    # install whose supervisor binary won't resolve is a fixable DEFECT, so name the
    # actionable reason rather than send the operator to `disco start` — which hits the
    # same wall — without knowing why (§12.6, honest degrade).
    if home is None:
        _print_manual_finish(name)
        return 0
    reason = _supervisor.supervisor_unavailable_reason(pc_binary_fn)
    if reason is not None:
        print("Your agent is configured, but the workspace can't be started automatically. Fix this first:")
        print(f"  {reason}")
        _print_manual_finish(name)
        return 0

    # Resolve the real orchestration coroutines lazily (import-light): the agent
    # deployment path must not pull supervisor modules at import. The presence
    # watcher (:func:`_wait_for_agent_online`) is a local module function whose own
    # calfkit imports are deferred to its body, so referencing it here adds nothing
    # to init's import graph.
    if start_fn is None or agent_start_fn is None or first_reply_fn is None:
        from calfcord.supervisor import lifecycle, roster

        start_fn = start_fn or lifecycle.start
        agent_start_fn = agent_start_fn or roster.agent_start
        first_reply_fn = first_reply_fn or _wait_for_agent_online

    # Phase 1 — bring the org live (async). The substrate and agent are DETACHED
    # external processes, so this loop can close afterward without touching them.
    rc = asyncio.run(
        _bring_online(
            name=name,
            home=home,
            server_urls=server_urls,
            start_fn=start_fn,
            agent_start_fn=agent_start_fn,
        )
    )
    if rc != 0:
        return rc

    # Phase 2 — the human nudge (§12.6: prompt the @mention INSIDE init). Runs
    # synchronously with NO event loop running, so the real InquirerPy prompt (which
    # drives its own loop) works exactly as it does everywhere else in the wizard.
    prompter.confirm(f"In Discord, say:  @{name} hello   — press enter once you've sent it.", default=True)
    print(f"Waiting for {name} to come online…")

    # Phase 3 — confirm presence (async, level-triggered read). The org is already
    # live; detection is advisory, so a failure degrades to the honest fallback.
    detected = asyncio.run(_await_presence(first_reply_fn, name=name, server_urls=server_urls))
    _print_finish_epilogue(name, detected=detected, postable=postable)
    return 0


async def _bring_online(
    *,
    name: str,
    home: Path,
    server_urls: str,
    start_fn: Callable[..., Awaitable[int]],
    agent_start_fn: Callable[..., Awaitable[int]],
) -> int:
    """Phase 1: start the substrate, then the agent.

    Returns 0 once both are up, or the first non-zero code (substrate or agent
    start). On a cold start ``start`` tears the substrate back down on its own
    failure; on an ALREADY-OPEN workspace it can instead fail the in-place bridge
    restart with the substrate (broker + agents) still up. Either way ``start``
    prints the specific cause first.
    """
    print("Opening your workspace (broker + bridge)…")
    # Mirror main.py's _run_lifecycle wiring (DRY): the shim launcher every
    # supervised process execs under, and the broker URL. The project is
    # substrate-only — the roster (this agent included) spawns off Process
    # Compose right after, and mcp.json is not consulted here at all (unlike
    # `disco start`'s deliberate fail-fast validation): onboarding's job is
    # reaching a live org, and a broken mcp.json surfaces later through the
    # strict readers (`disco start`, `disco mcp start`).
    launcher = str(home / "shims" / "disco")
    rc = await start_fn(home, server_urls=server_urls, launcher=launcher)
    if rc != 0:
        # start() has already printed the specific cause (§12.6 — never
        # misattribute). Several failures land here: a cold-start broker failure
        # that tore the substrate down, a Discord-intents gap, a bad config, OR — on
        # a re-run over an already-open workspace — a bridge that didn't come back
        # from its in-place restart while the broker/agents stay up. So don't claim
        # the workspace is down; point at the error above + the diagnostics.
        print(
            "  couldn't finish bringing the workspace online — see the error above "
            "(or run `disco logs` / `disco doctor`), then re-run `disco init`."
        )
        return rc

    print(f"Bringing {name} online…")
    return await agent_start_fn(home, name=name, server_urls=server_urls)


async def _await_presence(
    first_reply_fn: Callable[..., Awaitable[bool]],
    *,
    name: str,
    server_urls: str,
) -> bool:
    """Phase 3: confirm the agent registered on the mesh; ``True`` if seen online.

    The org is ALREADY live (substrate + agent both started), so presence detection
    is advisory: the watcher opens its OWN ``Client.connect`` and a broker drop or a
    transient connect blip mid-watch could raise out of it. Degrade any such failure
    to ``False`` — the same bounded "org is live — try it yourself / run ``doctor``"
    fallback a clean timeout takes — rather than crash the wizard after the org came
    up. But don't do it silently: a clean timeout and a permanently broken detector
    (a calfkit import/signature drift, a malformed ``server_urls``) both degrade here,
    so name the cause rather than let a hard failure masquerade as a slow agent.
    ``except Exception`` (not bare) deliberately lets ``asyncio.CancelledError``
    propagate: the failure is in detection, not in the (already-live) org.
    """
    try:
        return await first_reply_fn(server_urls, agent_id=name, timeout_s=_FIRST_REPLY_TIMEOUT_S)
    except Exception as exc:
        print(f"  (couldn't confirm {name} came online: {exc!r})")
        return False


def _print_finish_epilogue(name: str, *, detected: bool, postable: bool | None) -> None:
    """Celebrate or honestly degrade, then signpost the next step.

    Both the celebrate and the presence-timeout degrade converge here, so the
    "add a teammate" signpost (and where to learn more) shows on either. ``postable``
    is the Discord-step postability preflight verdict. A ``False`` (the bot can post in
    no channel) is a proven, fixable problem handled INDEPENDENTLY of detection: it
    always surfaces the permission remedy — never the "try `@name hello`" errand that
    can't get a reply — whether or not the agent was also seen online (§12.6, no green
    light that lies). ``None`` (unknown) is not asserted as a failure, so a detected
    agent still celebrates.
    """
    if postable is False:
        # The preflight PROVED the bot can post in no channel — a known, fixable problem
        # independent of detection. Lead with the remedy rather than the generic "try
        # `@name hello`" (it can't get a reply) or a bare `disco doctor` (it won't
        # diagnose Discord permissions) (§12.6). Whether or not it also came online.
        if detected:
            print(f"{name} is online, but it can't post in any Discord channel yet.")
        else:
            print(f"{name} can't post in any Discord channel yet — and it isn't online in Discord either.")
        print(f"  Grant it View Channel + Send Messages + Manage Webhooks, then say `@{name} hello`.")
        if not detected:
            print("  If it stays quiet after that, run `disco doctor`.")
    elif detected:
        print(f"🎉 {name} is online — your organization is live!")
    else:
        # Bounded fallback (§12.6): never promise more than we detected.
        print(
            f"  your organization is live — try `@{name} hello` in Discord. If nothing replies, run `disco doctor`."
        )
    # The next step nobody teaches: a one-agent org is step one, not the finish line.
    print()
    print("Add a teammate any time:")
    print("    disco agent create <name>")
    print("Learn more:  disco explain topology  ·  docs/using-disco.md")
    print()
    # `disco start` reopens only the substrate; the detached roster does not
    # auto-start, so the reboot note must name the agent re-start too.
    print(
        f"({_REBOOT_NOTE} `disco start` reopens it, then `disco agent start --all` "
        "brings the agents back; `disco status` shows who's online.)"
    )


def _print_manual_finish(name: str) -> None:
    """Honest degrade (§12.6): everything is configured; name the manual next steps.

    Used on a dev run or a missing supervisor binary, where init cannot
    orchestrate the install-scoped supervisor. The next step is always named so
    the operator is never stranded at "configured, now what?".
    """
    print(f"Set up agent '{name}'. To bring it online:")
    print("    disco start")
    print(f"    disco agent start {name}")
    print(f"Then in Discord, say: @{name} hello")
    print("Add more teammates any time: disco agent create <name>")
    # Agents do not auto-start with the substrate, so the reboot steer names both.
    print(f"({_REBOOT_NOTE} Re-run `disco start`, then `disco agent start --all`, after a reboot.)")
