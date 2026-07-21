"""``calfkit-auth grok`` — device-code login for the xAI Grok OAuth provider.

Commands:

  calfkit-auth grok login [--no-browser] [--force]
      Run the device-code flow and cache credentials under ``$CALFCORD_HOME/auth/``.
  calfkit-auth grok logout
      Delete cached credentials.
  calfkit-auth grok status
      Show whether credentials are present and when the access token expires.
  calfkit-auth grok refresh
      Force a token refresh now (debugging convenience).
  calfkit-auth grok models
      List the Grok models available to your account (live catalog, or the
      pinned fallback when the API can't be reached).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import UTC, datetime

from calfcord.providers.grok import credentials, oauth, token_store
from calfcord.providers.grok.credentials import GrokNotLoggedInError
from calfcord.providers.grok.models import get_default_resolver
from calfcord.providers.grok.oauth import GrokAuthError, decode_jwt_exp
from calfcord.providers.grok.token_store import GrokCredentials


def register(sub: argparse._SubParsersAction) -> None:
    """Add the ``grok`` provider subparser to a shared ``calfkit-auth`` parser."""
    grok = sub.add_parser("grok", help="xAI Grok subscription auth (SuperGrok / X Premium+).")
    grok_sub = grok.add_subparsers(dest="command", required=True)

    login = grok_sub.add_parser("login", help="Authenticate via device-code OAuth.")
    login.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser.")
    login.add_argument("--force", action="store_true", help="Force a fresh login even if cached credentials exist.")

    grok_sub.add_parser("logout", help="Delete cached credentials.")
    grok_sub.add_parser("status", help="Show whether credentials are present and when they expire.")
    grok_sub.add_parser("refresh", help="Force a token refresh now.")
    grok_sub.add_parser("models", help="List the Grok models available to your account.")


def dispatch(args: argparse.Namespace) -> int:
    """Route a parsed ``grok`` subcommand to its handler. Returns an exit code."""
    if args.command == "login":
        return asyncio.run(_cmd_login(args))
    if args.command == "logout":
        return _cmd_logout(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "refresh":
        return asyncio.run(_cmd_refresh(args))
    if args.command == "models":
        return asyncio.run(_cmd_models(args))
    # Unreachable: ``command`` is a required, constrained subparser choice.
    raise ValueError(f"unknown grok command: {args.command}")


def _credential_dir() -> str:
    return str(token_store.credentials_path().parent)


async def _cmd_login(args: argparse.Namespace) -> int:
    if not args.force:
        try:
            await credentials.resolve_credentials()
            print(f"Already logged in; credentials cached at {_credential_dir()}", file=sys.stderr)
            return 0
        except GrokNotLoggedInError:
            pass
        except (GrokAuthError, OSError) as exc:
            # OSError = a lock timeout / unreadable cred dir; fall through to a
            # fresh login rather than crashing.
            print(f"Cached credentials unusable ({exc}); performing fresh login.", file=sys.stderr)

    try:
        payload = await oauth.device_code_login(open_browser=not args.no_browser)
        token_store.save_credentials(GrokCredentials.from_login(payload))
    except (GrokAuthError, OSError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Login successful; credentials cached at {_credential_dir()}", file=sys.stderr)
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    if token_store.delete_credentials():
        print("Logged out; cached credentials removed.", file=sys.stderr)
    else:
        print("Not logged in; nothing to remove.", file=sys.stderr)
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    creds = token_store.load_credentials()
    if creds is None:
        error = token_store.load_auth_error()
        if error:
            print(f"Not logged in (last error {error.get('code')}: {error.get('message')}).")
        else:
            print(f"Not logged in. Credential dir: {_credential_dir()}")
        print("Run: uv run calfkit-auth grok login")
        return 1

    print(f"Logged in. Credential dir: {_credential_dir()}")
    print(f"  Base URL: {creds.base_url}")
    exp = decode_jwt_exp(creds.access_token)
    if exp is not None:
        remaining = int(exp - time.time())
        print(f"  Access token expires: {datetime.fromtimestamp(exp, tz=UTC).isoformat()}")
        if remaining > 0:
            print(f"  Time remaining: {remaining}s ({remaining // 60}m)")
        else:
            print(f"  Time remaining: expired {-remaining}s ago (refresh on next use)")
    return 0


async def _cmd_refresh(args: argparse.Namespace) -> int:
    if token_store.load_credentials() is None:
        print("Not logged in. Run: uv run calfkit-auth grok login", file=sys.stderr)
        return 1
    try:
        await credentials.resolve_credentials(force_refresh=True)
    except (GrokAuthError, OSError) as exc:
        # OSError = a lock-acquisition timeout under contention / unreadable dir.
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 1
    return _cmd_status(args)


async def _cmd_models(_args: argparse.Namespace) -> int:
    creds = token_store.load_credentials()
    bearer = creds.access_token if creds else os.getenv("XAI_API_KEY", "").strip()
    resolver = get_default_resolver()
    resolver.reset()
    await resolver.ensure_loaded(bearer)
    default = resolver.default_slug()
    print(f"Grok models ({resolver.source}):")
    for model in resolver.selectable_models():
        print(f"  {model}{' (default)' if model == default else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone ``calfkit-auth grok ...`` entry (also composed by auth_cli)."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="calfkit-auth", description="Authentication for Agent Disco LLM providers.")
    sub = parser.add_subparsers(dest="provider", required=True)
    register(sub)
    args = parser.parse_args(argv)
    if args.provider != "grok":
        parser.error(f"unknown provider: {args.provider}")
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
