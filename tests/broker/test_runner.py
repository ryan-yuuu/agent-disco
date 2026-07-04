"""Unit tests for the ``calfcord-broker`` launcher.

The launcher resolves the Tansu broker binary via calfkit-mesh's
``resolve_broker_bin`` and process-replaces it as ``tansu broker <args>``. It
supplies calfcord's substrate defaults (memory storage on localhost:9092), lets
the operator's shell env / passthrough args override them, and refuses to boot
the bundled *memory-only* binary into a non-memory storage engine (which would
crash-loop under the supervisor's ``restart: always``). These tests patch
``resolve_broker_bin`` and the process-replace call so the launcher stays a unit
test and never actually replaces the process.
"""

from __future__ import annotations

import inspect
import os

import pytest

from calfcord.broker import runner


@pytest.fixture(autouse=True)
def _clean_broker_env(monkeypatch):
    """Every test starts from a clean broker env; a test that wants a value sets
    it explicitly. Autouse runs before the test body, so a test's own ``setenv``
    still lands after this clear (same monkeypatch instance, correct order)."""
    for key in ("STORAGE_ENGINE", "ADVERTISED_LISTENER_URL", "CALF_TANSU_BIN"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_exec(monkeypatch):
    """Capture the launcher's process-replace call instead of replacing the test
    process. Returns the recorder list ``[(path, argv), ...]``."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(runner.os, "execv", lambda path, argv: calls.append((path, argv)))
    return calls


@pytest.fixture
def stub_binary(monkeypatch):
    """Make ``resolve_broker_bin`` return a fixed absolute path."""
    monkeypatch.setattr(runner, "resolve_broker_bin", lambda: "/opt/tansu")
    return "/opt/tansu"


class TestExec:
    def test_execs_resolved_binary_as_broker(self, monkeypatch, stub_binary, fake_exec) -> None:
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert fake_exec == [("/opt/tansu", ["/opt/tansu", "broker"])]

    def test_forwards_passthrough_args(self, monkeypatch, stub_binary, fake_exec) -> None:
        monkeypatch.setattr("sys.argv", ["calfcord-broker", "--kafka-cluster-id", "demo"])
        runner.main()
        assert fake_exec[0][1] == ["/opt/tansu", "broker", "--kafka-cluster-id", "demo"]


class TestEnvDefaults:
    def test_sets_memory_and_listener_when_unset(self, monkeypatch, stub_binary, fake_exec, capsys) -> None:
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "memory://tansu/"
        assert os.environ["ADVERTISED_LISTENER_URL"] == "tcp://localhost:9092"
        # The clean default path must NOT emit the memory-only warning.
        assert "memory-only" not in capsys.readouterr().err

    def test_preserves_explicit_listener(self, monkeypatch, stub_binary, fake_exec) -> None:
        monkeypatch.setenv("ADVERTISED_LISTENER_URL", "tcp://0.0.0.0:9092")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["ADVERTISED_LISTENER_URL"] == "tcp://0.0.0.0:9092"

    def test_empty_storage_engine_counts_as_unset(self, monkeypatch, stub_binary, fake_exec) -> None:
        """Match the old ``${STORAGE_ENGINE:-default}`` shell semantics: an empty
        value defaults, it is not preserved (as ``setdefault`` would)."""
        monkeypatch.setenv("STORAGE_ENGINE", "")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "memory://tansu/"

    def test_preserves_explicit_memory_storage_engine(self, monkeypatch, stub_binary, fake_exec, capsys) -> None:
        """A non-default but still-memory engine survives the enforcement guard
        and boots without a warning."""
        monkeypatch.setenv("STORAGE_ENGINE", "memory://custom/")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "memory://custom/"
        assert "memory-only" not in capsys.readouterr().err
        assert fake_exec == [("/opt/tansu", ["/opt/tansu", "broker"])]


class TestMemoryEnforcement:
    def test_bundled_binary_forces_memory_warns_and_still_boots(
        self, monkeypatch, stub_binary, fake_exec, capsys
    ) -> None:
        """No ``$CALF_TANSU_BIN`` override means the bundled memory-only binary;
        a non-memory ``STORAGE_ENGINE`` would crash-loop it, so force memory and
        warn rather than boot into an opaque failure — and it must still boot."""
        monkeypatch.setenv("STORAGE_ENGINE", "libsql://data.db")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "memory://tansu/"
        assert fake_exec == [("/opt/tansu", ["/opt/tansu", "broker"])]  # forced memory AND booted
        err = capsys.readouterr().err
        assert "memory-only" in err
        assert "libsql://data.db" in err

    def test_custom_binary_honors_non_memory_engine(self, monkeypatch, stub_binary, fake_exec, capsys) -> None:
        """With ``$CALF_TANSU_BIN`` set the operator supplied their own (possibly
        persistent) binary, so a non-memory ``STORAGE_ENGINE`` is honored."""
        monkeypatch.setenv("CALF_TANSU_BIN", "/usr/local/bin/tansu")
        monkeypatch.setenv("STORAGE_ENGINE", "libsql://data.db")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "libsql://data.db"
        assert "memory-only" not in capsys.readouterr().err

    def test_custom_binary_defaults_to_memory_when_engine_unset(
        self, monkeypatch, stub_binary, fake_exec, capsys
    ) -> None:
        """``$CALF_TANSU_BIN`` set but no explicit engine → memory default is
        applied and honored (no enforcement rewrite), and no warning."""
        monkeypatch.setenv("CALF_TANSU_BIN", "/usr/local/bin/tansu")
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        runner.main()
        assert os.environ["STORAGE_ENGINE"] == "memory://tansu/"
        assert "memory-only" not in capsys.readouterr().err


class TestResolutionFailure:
    def test_binary_not_found_exits_1(self, monkeypatch, fake_exec, capsys) -> None:
        def _raise() -> str:
            raise runner.TansuBinaryNotFound("no tansu here")

        monkeypatch.setattr(runner, "resolve_broker_bin", _raise)
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 1
        assert "no tansu here" in capsys.readouterr().err
        assert fake_exec == []

    def test_extraction_oserror_exits_1(self, monkeypatch, fake_exec, capsys) -> None:
        """First-run extraction (mkdir/copy/chmod) can raise ``OSError`` — the
        launcher must catch it too, not only ``TansuBinaryNotFound``."""

        def _raise() -> str:
            raise OSError("read-only home")

        monkeypatch.setattr(runner, "resolve_broker_bin", _raise)
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 1
        assert "read-only home" in capsys.readouterr().err
        assert fake_exec == []

    def test_empty_exception_message_falls_back_to_type_name(self, monkeypatch, fake_exec, capsys) -> None:
        """A dependency exception with no message must still name a cause, not
        leave a bare ``disco broker:`` prefix."""

        def _raise() -> str:
            raise runner.TansuBinaryNotFound("")

        monkeypatch.setattr(runner, "resolve_broker_bin", _raise)
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 1
        assert "TansuBinaryNotFound" in capsys.readouterr().err

    def test_exec_failure_exits_1_with_branded_message(self, monkeypatch, stub_binary, capsys) -> None:
        """A resolved-but-unrunnable binary (wrong arch → ENOEXEC, noexec mount →
        EACCES) surfaces as the branded message + exit 1, not a raw traceback
        that repeats under Process Compose's ``restart: always``."""

        def _boom(path, argv):
            raise OSError("Exec format error")

        monkeypatch.setattr(runner.os, "execv", _boom)
        monkeypatch.setattr("sys.argv", ["calfcord-broker"])
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "cannot exec" in err
        assert "/opt/tansu" in err
        assert "Exec format error" in err


class TestPackageContract:
    """Guard against silent drift in the calfkit-mesh dependency: its seam is
    stubbed everywhere else, so pin the shape of the real contract in one place —
    a signature or hierarchy change here would otherwise sail through green while
    breaking `disco broker`/`disco doctor` in production."""

    def test_resolve_broker_bin_takes_no_required_args(self) -> None:
        from calfkit_mesh import resolve_broker_bin

        assert list(inspect.signature(resolve_broker_bin).parameters) == []

    def test_tansu_binary_not_found_is_runtime_error(self) -> None:
        from calfkit_mesh import TansuBinaryNotFound

        assert issubclass(TansuBinaryNotFound, RuntimeError)
