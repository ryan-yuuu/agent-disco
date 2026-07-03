"""calfcord — multi-agent organization on Discord."""

# Quiet third-party import-time noise before anything imports openhands/litellm.
# This package root is imported before any submodule, and every openhands/litellm
# import in the tree is lazy or lives under ``calfcord.providers.codex`` — so
# setting these here guarantees they are in the environment first. Each silences a
# DISTINCT mechanism (proven independently in tests/cli/test_quiet_startup.py):
# the OpenHands banner is a bare ``print`` to stderr at import; ``LOG_AUTO_CONFIG``
# stops OpenHands installing a RichHandler on the ROOT logger (which would surface
# every library's INFO line); ``LITELLM_LOG`` gates litellm's OWN handler (the
# botocore pre-load warnings). ``setdefault`` keeps operator overrides intact
# (export ``LOG_AUTO_CONFIG=true`` to debug).
import os

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LOG_AUTO_CONFIG", "false")
os.environ.setdefault("LITELLM_LOG", "ERROR")
