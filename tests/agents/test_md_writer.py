"""Unit tests for the .md frontmatter atomic writer."""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from calfkit_organization.agents.md_writer import update_thinking_effort


def _seed_md(
    path: Path,
    *,
    agent_id: str = "scribe",
    provider: str = "openai",
    thinking_effort: str | None = None,
    body: str = "Hello body.",
) -> Path:
    meta: dict[str, str] = {
        "name": agent_id,
        "display_name": agent_id.capitalize(),
        "description": f"Test {agent_id}.",
        "provider": provider,
    }
    if thinking_effort is not None:
        meta["thinking_effort"] = thinking_effort
    post = frontmatter.Post(body, **meta)
    md_path = path / f"{agent_id}.md"
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return md_path


def test_inserts_thinking_effort_when_absent(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_thinking_effort(md_path, "high")
    assert updated.thinking_effort == "high"

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["thinking_effort"] == "high"


def test_overwrites_existing_thinking_effort(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    updated = update_thinking_effort(md_path, "max")
    assert updated.thinking_effort == "max"

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["thinking_effort"] == "max"


def test_preserves_other_frontmatter_fields(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, agent_id="scheduler", provider="anthropic")
    update_thinking_effort(md_path, "medium")

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["name"] == "scheduler"
    assert reloaded.metadata["provider"] == "anthropic"


def test_preserves_body_content(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, body="You are a helpful agent.\n\nBe concise.")
    update_thinking_effort(md_path, "high")

    reloaded = frontmatter.load(md_path)
    assert reloaded.content.strip() == "You are a helpful agent.\n\nBe concise."


def test_returns_parsed_definition_with_source_path(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_thinking_effort(md_path, "xhigh")
    assert updated.agent_id == "scribe"
    assert updated.thinking_effort == "xhigh"
    assert updated.source_path == md_path


def test_atomic_no_tmp_files_left_behind(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    update_thinking_effort(md_path, "high")
    tmp_files = list(tmp_path.glob(".*.tmp"))
    assert tmp_files == []


def test_missing_md_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_thinking_effort(tmp_path / "ghost.md", "high")


def test_round_trip_through_parse(tmp_path: Path) -> None:
    """A round-trip must produce a re-parsable .md (no corruption)."""
    from calfkit_organization.agents.definition import parse_agent_md

    md_path = _seed_md(tmp_path)
    update_thinking_effort(md_path, "high")
    re_parsed = parse_agent_md(md_path)
    assert re_parsed.thinking_effort == "high"
    assert re_parsed.agent_id == "scribe"


def test_atomic_write_failure_leaves_original_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash during the atomic rename must leave the original file unchanged
    and clean up the .tmp sibling — exercises the ``except: tmp_path.unlink``
    branch in ``_atomic_write_text``.
    """
    import os

    md_path = _seed_md(tmp_path, thinking_effort="low")
    original_payload = md_path.read_text(encoding="utf-8")

    def _raise_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _raise_replace)

    with pytest.raises(OSError):
        update_thinking_effort(md_path, "high")

    # Original file content unchanged.
    assert md_path.read_text(encoding="utf-8") == original_payload
    # No leftover .tmp files.
    assert list(tmp_path.glob(".*.tmp")) == []


def test_malformed_existing_frontmatter_raises_valueerror(
    tmp_path: Path,
) -> None:
    """An unparseable existing .md surfaces as ValueError with the path —
    not a raw yaml.YAMLError."""
    md_path = tmp_path / "scribe.md"
    md_path.write_text(
        "---\nname: scribe\n  invalid: : : yaml\n---\nbody\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="malformed YAML"):
        update_thinking_effort(md_path, "high")


def test_validation_failure_does_not_touch_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the in-memory validation fails, the on-disk file must be unchanged.

    Forces a failure by monkeypatching ``AgentDefinition`` construction in the
    md_writer module to raise.
    """
    from calfkit_organization.agents import md_writer

    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")

    class _Boom:
        def __init__(self, **_kwargs: object) -> None:
            raise ValueError("simulated validation failure")

    monkeypatch.setattr(md_writer, "AgentDefinition", _Boom)

    with pytest.raises(ValueError, match="simulated validation"):
        update_thinking_effort(md_path, "high")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []
