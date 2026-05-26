---
name: Bug report
about: Report a defect in calfcord so it can be reproduced and fixed.
title: '[Bug] '
labels: bug
assignees: ''
---

<!--
Fill in every section. "I don't know" is a valid answer, but a blank
field tends to stall the bug for a round-trip while a maintainer asks
the same question.

For security vulnerabilities, do NOT open a public issue. See
SECURITY.md for the private disclosure path.
-->

## Repro steps

1.
2.
3.

## Expected behavior

What you thought would happen.

## Actual behavior

What actually happened. Include error messages verbatim.

## Calfcord version

Commit SHA (`git rev-parse HEAD`) or release tag:

## Deployment mode

- [ ] Native (`uv run` each process)
- [ ] Hybrid (some native, some compose)
- [ ] All-in-Docker (`docker compose up`)

## Python version

Output of `python --version`:

## Logs

Relevant lines from `docker compose logs <service>` (or the native
process's stderr). Truncate to the surrounding 20-50 lines around the
failure — don't paste the whole log. Wrap in a fenced code block.

```
<paste here>
```

## Anything else?

Configuration deltas from `.env.example`, custom agent definitions,
unusual network setup, anything else that might matter.
