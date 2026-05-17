---
name: echo
slash: /echo
display_name: Echo
description: Echoes back whatever you send. Useful as a smoke-test agent.
avatar_url: https://api.dicebear.com/9.x/glass/png?seed=echo
---

Echo test agent — replies `echo: <content>` to every message that passes its gates. Used to verify the bridge end-to-end.

The hand-coded runtime in `agents/echo.py` ignores this body; the system prompt is here only so the file satisfies the schema and documents the agent.
