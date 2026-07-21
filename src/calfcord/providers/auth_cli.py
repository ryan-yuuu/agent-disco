"""Unified ``calfkit-auth`` entry point composing every LLM auth provider.

Each provider owns its own subparser and dispatch (``register``/``dispatch``);
this module only assembles them under one ``calfkit-auth`` command:

    calfkit-auth codex login    # OpenAI ChatGPT subscription (Codex models)
    calfkit-auth grok  login    # xAI Grok subscription (SuperGrok / Premium+)

All provider subparsers are registered so ``calfkit-auth --help`` lists every
provider; the actual runtime machinery (model clients, OAuth flows) stays out of
the import graph until a command is dispatched. The ``calfkit-auth`` console
script points here (see ``pyproject.toml``); each provider module also keeps a
standalone ``main`` for direct use.
"""

from __future__ import annotations

import argparse
import logging
import sys
from types import ModuleType


def _provider_clis() -> dict[str, ModuleType]:
    """Map each provider to its CLI module (each exposing ``register``/``dispatch``)."""
    from calfcord.providers.codex import cli as codex_cli
    from calfcord.providers.grok import cli as grok_cli

    return {"codex": codex_cli, "grok": grok_cli}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calfkit-auth", description="Authentication for Agent Disco LLM providers.")
    sub = parser.add_subparsers(dest="provider", required=True)
    for provider_cli in _provider_clis().values():
        provider_cli.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    # ``provider`` is a required, constrained subparser choice, so it is always a
    # known key here.
    return _provider_clis()[args.provider].dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
