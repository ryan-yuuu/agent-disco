"""Tests for ``packaging._build.run_build``.

The function's success path is already exercised via every CLI
``--dry-run`` test. These tests pin the FAILURE paths that operators
depend on for debugging:

* Docker binary not on PATH → returns 127 with a usable message.
* ``docker buildx build`` exits non-zero → returns that code AND
  retains the tempdir so the operator can read the generated
  Dockerfile.
* ``docker buildx build`` succeeds → returns 0 AND cleans up the
  tempdir.
* Ctrl-C mid-build → re-raises after printing the retained
  tempdir path.

All paths use ``monkeypatch.setattr`` on ``shutil.which`` and
``subprocess.run`` to avoid touching a real Docker daemon.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from calfkit_organization.packaging import _build


def _fake_completed(returncode: int = 0):
    """Build a stand-in for ``subprocess.CompletedProcess``.

    The real class accepts ``args, returncode, stdout, stderr``; for
    these tests only ``returncode`` is read by the SUT.
    """
    class _R:
        pass

    r = _R()
    r.returncode = returncode  # type: ignore[attr-defined]
    return r


class TestDockerMissing:
    def test_returns_127_when_docker_not_on_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # ``shutil.which`` returning None is the "binary not installed"
        # signal the SUT checks before invoking subprocess.
        monkeypatch.setattr(shutil, "which", lambda _: None)
        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )
        assert exit_code == 127
        err = capsys.readouterr().err
        assert "docker" in err.lower()


class TestBuildFailure:
    def test_propagates_exit_code_and_retains_tempdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Pretend docker is installed but ``buildx build`` failed.
        # The SUT must propagate the docker exit code AND leave the
        # tempdir on disk so the operator can read the Dockerfile.
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _fake_completed(returncode=42)
        )

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        assert exit_code == 42
        err = capsys.readouterr().err
        # The retained-path message is the operator's only handle to
        # the generated Dockerfile. The path string from the message
        # must point at a real file.
        assert "retained at" in err
        # Pull the path out of the message and verify the Dockerfile
        # is still there (cleanup was correctly skipped). Use a regex
        # to grab the trailing absolute path so the test is robust to
        # the surrounding wording.
        import re
        m = re.search(r"retained at:?\s+(\S+)", err)
        assert m is not None, f"no path in stderr: {err}"
        retained = Path(m.group(1))
        assert retained.is_file(), f"expected file at {retained}, stderr was: {err}"
        # Clean up so we don't pollute /tmp.
        shutil.rmtree(retained.parent, ignore_errors=True)


class TestBuildSuccess:
    def test_cleans_up_tempdir_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Capture which tempdir was used by inspecting the actual call
        # to subprocess.run; the Dockerfile path is in the --file arg.
        captured: dict[str, Path] = {}

        def fake_run(cmd, check):
            # The --file flag's value is the Dockerfile path.
            idx = cmd.index("--file")
            captured["dockerfile"] = Path(cmd[idx + 1])
            return _fake_completed(returncode=0)

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(subprocess, "run", fake_run)

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        assert exit_code == 0
        # Success path must clean up — verify the Dockerfile is gone.
        assert "dockerfile" in captured
        assert not captured["dockerfile"].exists()
        # The success message points at follow-up commands for the
        # operator; verify it shows up.
        err = capsys.readouterr().err
        assert "Built and tagged" in err
        assert "docker push" in err


class TestKeyboardInterrupt:
    def test_writes_retained_path_to_stderr_and_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A Ctrl-C during ``subprocess.run`` must print the retained
        Dockerfile path to stderr AND re-raise ``KeyboardInterrupt``
        unchanged. The retained-path message is the operator's only
        handle to inspect what was being built; the re-raise preserves
        the standard SIGINT semantics so calling shells exit with the
        expected 130. A regression that swallows the interrupt or
        omits the path would silently regress operator UX."""
        captured: dict[str, Path] = {}

        def fake_run(cmd, check):
            idx = cmd.index("--file")
            captured["dockerfile"] = Path(cmd[idx + 1])
            raise KeyboardInterrupt

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(KeyboardInterrupt):
            _build.run_build(
                dockerfile_content="FROM scratch\n",
                tag="x:1",
                context=tmp_path,
                dry_run=False,
                verbose=False,
            )

        err = capsys.readouterr().err
        assert "interrupted" in err
        # The retained Dockerfile must still be on disk AND named in
        # the message — both are necessary for the operator to recover.
        assert "dockerfile" in captured
        assert captured["dockerfile"].is_file()
        assert str(captured["dockerfile"]) in err
        shutil.rmtree(captured["dockerfile"].parent, ignore_errors=True)


class TestOSError:
    def test_returns_1_and_retains_tempdir_on_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """If ``subprocess.run`` itself fails to spawn (PermissionError,
        ENOENT for a buildx plugin, etc.) the SUT must return 1, write
        an actionable message, and retain the tempdir for forensics.
        The handler exists at ``_build.py:150-153`` but was previously
        unpinned by tests — a refactor that drops the ``except OSError``
        would let the exception escape as a raw traceback."""
        captured: dict[str, Path] = {}

        def fake_run(cmd, check):
            idx = cmd.index("--file")
            captured["dockerfile"] = Path(cmd[idx + 1])
            raise PermissionError("docker daemon socket not writable")

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(subprocess, "run", fake_run)

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "failed to invoke docker" in err
        assert "dockerfile" in captured
        assert captured["dockerfile"].is_file()
        assert str(captured["dockerfile"]) in err
        shutil.rmtree(captured["dockerfile"].parent, ignore_errors=True)


class TestBuildxCommandShape:
    def test_invokes_docker_buildx_build_with_tag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Pin the exact buildx-command prefix and that ``--tag <tag>``
        is wired through. A regression that swaps ``buildx build`` for
        plain ``docker build`` would silently lose the multi-arch path
        documented in ``docs/distributed-deployment.md`` — and no other
        test would catch it because both shapes return the same exit
        codes."""
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, check):
            captured["cmd"] = list(cmd)
            return _fake_completed(returncode=0)

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(subprocess, "run", fake_run)

        _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="my-image:7.0",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        cmd = captured["cmd"]
        # First three tokens must be exactly the buildx prefix.
        assert cmd[:3] == ["docker", "buildx", "build"]
        # --tag <tag> must be wired in adjacent positions.
        assert "--tag" in cmd
        tag_idx = cmd.index("--tag")
        assert cmd[tag_idx + 1] == "my-image:7.0"
        # --file <dockerfile> and the context path must also be present.
        assert "--file" in cmd
        assert str(tmp_path) in cmd


class TestDryRun:
    def test_emits_dockerfile_to_stdout_and_does_not_invoke_docker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # If subprocess.run is invoked despite --dry-run, the test
        # blows up — a real fail-loud guard against regressions that
        # change the dry-run semantics.
        def explode(*_a, **_k):
            raise AssertionError("subprocess.run must not be called in dry-run mode")

        monkeypatch.setattr(subprocess, "run", explode)

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n# marker-for-dry-run-test\n",
            tag="x:1",
            context=tmp_path,
            dry_run=True,
            verbose=False,
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "marker-for-dry-run-test" in out


class TestRepoRoot:
    def test_walks_up_to_find_pyproject(self) -> None:
        # The default ``repo_root()`` walks up from this module's
        # install location until pyproject.toml appears. In the
        # in-tree test environment, that resolves to the calfcord
        # checkout root.
        root = _build.repo_root()
        assert (root / "pyproject.toml").is_file()
