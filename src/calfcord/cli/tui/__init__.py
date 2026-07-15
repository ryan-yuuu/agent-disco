"""The Rich-rendered TUI behind the interactive ``disco`` commands.

Rendering is Rich; raw key reading is :mod:`readchar` (see
``docs/design/cli-tui-migration.md``). Nothing here owns an event loop, so a
prompt is safe to call from anywhere — including code that later drives
``asyncio.run`` — which the InquirerPy surface this replaces was not.
"""
