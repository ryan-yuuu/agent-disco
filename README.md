# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                          |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|---------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| src/calfcord/\_atomic.py                      |       19 |        0 |        2 |        0 |    100% |           |
| src/calfcord/\_provisioning.py                |       19 |        0 |        6 |        0 |    100% |           |
| src/calfcord/\_worker\_runtime.py             |       41 |        0 |       14 |        0 |    100% |           |
| src/calfcord/agents/definition.py             |       96 |        0 |       22 |        0 |    100% |           |
| src/calfcord/agents/factory.py                |      100 |        2 |       30 |        2 |     97% |  139, 153 |
| src/calfcord/agents/identifier.py             |       26 |        0 |       10 |        0 |    100% |           |
| src/calfcord/agents/loader.py                 |       38 |        1 |       16 |        1 |     96% |       115 |
| src/calfcord/agents/md\_writer.py             |      103 |        6 |       24 |        4 |     92% |89-\>92, 160, 210-211, 283, 310-\>316, 316-\>exit, 323-327 |
| src/calfcord/agents/memory.py                 |       53 |        0 |       10 |        0 |    100% |           |
| src/calfcord/agents/runner.py                 |       93 |       20 |       18 |        1 |     81% |226-256, 260-269, 273 |
| src/calfcord/agents/thinking.py               |       41 |        4 |       22 |        2 |     90% |103-107, 116-120 |
| src/calfcord/bridge/a2a\_dispatch.py          |       56 |        0 |        4 |        0 |    100% |           |
| src/calfcord/bridge/a2a\_project.py           |       67 |        1 |       16 |        1 |     98% |       150 |
| src/calfcord/bridge/egress.py                 |       87 |        3 |       18 |        0 |     97% |   213-219 |
| src/calfcord/bridge/gateway.py                |      298 |       54 |       64 |        5 |     79% |129-130, 135-136, 308-311, 315-316, 402-404, 408-\>412, 435, 490, 557, 576, 590-701, 705 |
| src/calfcord/bridge/history.py                |      276 |        7 |       86 |        5 |     97% |387-\>389, 399-\>401, 421-\>423, 665-672, 748, 893-\>895, 1076-1087 |
| src/calfcord/bridge/mention\_handler.py       |      165 |        1 |       48 |        1 |     99% |        86 |
| src/calfcord/bridge/normalizer.py             |       46 |        0 |        8 |        0 |    100% |           |
| src/calfcord/bridge/overrides.py              |       23 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/persona\_resolve.py       |        5 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/progress.py               |      116 |        0 |       24 |        1 |     99% |273-\>exit |
| src/calfcord/bridge/reply\_poster.py          |       85 |        0 |       12 |        0 |    100% |           |
| src/calfcord/bridge/roster.py                 |       29 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/settings.py               |       48 |        1 |        6 |        0 |     98% |        86 |
| src/calfcord/bridge/slash.py                  |       98 |        6 |       16 |        0 |     95% |74, 97, 185-186, 224-225 |
| src/calfcord/bridge/step\_events.py           |       50 |        3 |       12 |        1 |     94% | 72-73, 75 |
| src/calfcord/bridge/steps\_render.py          |      115 |        4 |       60 |        4 |     95% |97-98, 115-118, 207-\>202, 220-\>215, 222-\>213, 274-\>281 |
| src/calfcord/bridge/transcripts.py            |      154 |        3 |       18 |        0 |     98% |   181-185 |
| src/calfcord/bridge/wire.py                   |       40 |        0 |        6 |        0 |    100% |           |
| src/calfcord/broker/runner.py                 |       30 |        1 |        6 |        1 |     94% |        80 |
| src/calfcord/cli/\_agents.py                  |      108 |        6 |       20 |        2 |     94% |163-164, 203-205, 286, 293-\>295 |
| src/calfcord/cli/\_editor.py                  |       28 |        1 |       12 |        1 |     95% |        92 |
| src/calfcord/cli/\_envfile.py                 |       55 |        2 |       32 |        2 |     95% |   75, 141 |
| src/calfcord/cli/\_fields.py                  |       76 |        0 |       28 |        0 |    100% |           |
| src/calfcord/cli/\_mcp.py                     |        8 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_prompts.py                 |       17 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_providers.py               |      137 |        6 |       42 |        4 |     94% |215-216, 221-222, 224, 295-\>299, 357, 393-\>395 |
| src/calfcord/cli/\_supervisor.py              |       34 |        0 |        8 |        0 |    100% |           |
| src/calfcord/cli/agent\_create.py             |      127 |       11 |       36 |        2 |     92% |338-339, 448-454, 568-571 |
| src/calfcord/cli/agent\_edit.py               |      142 |       15 |       38 |        2 |     91% |98-102, 143-147, 171-173, 266-267, 296, 325-329 |
| src/calfcord/cli/agent\_inspect.py            |       70 |        2 |       18 |        2 |     95% |   74, 137 |
| src/calfcord/cli/agent\_lifecycle.py          |      110 |        2 |       34 |        1 |     98% |250-251, 265-\>267 |
| src/calfcord/cli/agent\_tools.py              |       93 |        9 |       24 |        1 |     91% |76, 159-164, 242-247 |
| src/calfcord/cli/deploy.py                    |       86 |        1 |       30 |        2 |     97% |488-\>493, 497 |
| src/calfcord/cli/discord\_discovery.py        |      193 |       16 |       50 |        7 |     91% |214-215, 239-241, 383, 387, 494-\>492, 519-520, 539, 546, 554-555, 563-565 |
| src/calfcord/cli/doctor.py                    |      171 |        5 |       66 |        0 |     98% |   101-105 |
| src/calfcord/cli/explain.py                   |       17 |        0 |        2 |        0 |    100% |           |
| src/calfcord/cli/init.py                      |      290 |        0 |       68 |        1 |     99% |675-\>exit |
| src/calfcord/cli/logs.py                      |       77 |        0 |       28 |        0 |    100% |           |
| src/calfcord/cli/main.py                      |      361 |       13 |      148 |        5 |     96% |506, 526, 552, 871-873, 918-919, 934-935, 952-953, 982 |
| src/calfcord/cli/mcp\_admin.py                |      184 |       16 |       74 |        4 |     91% |93, 99-\>101, 199, 278-280, 305-323 |
| src/calfcord/cli/setup\_state.py              |       44 |        0 |        4 |        0 |    100% |           |
| src/calfcord/cli/tool\_aliases.py             |       60 |        0 |        8 |        0 |    100% |           |
| src/calfcord/cli/tui/keys.py                  |       24 |        2 |        2 |        1 |     88% |  129, 135 |
| src/calfcord/cli/tui/line\_input.py           |       71 |        4 |       16 |        3 |     92% |55, 58-\>64, 121-122, 123-\>126, 147 |
| src/calfcord/cli/tui/prompter.py              |       27 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/tui/render.py                |       41 |        0 |        6 |        0 |    100% |           |
| src/calfcord/cli/tui/state.py                 |       54 |        0 |       10 |        0 |    100% |           |
| src/calfcord/cli/tui/theme.py                 |       16 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/tui/widgets.py               |      125 |        0 |       28 |        0 |    100% |           |
| src/calfcord/discord/avatar.py                |        3 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/chunking.py              |       32 |        0 |       18 |        1 |     98% |   60-\>85 |
| src/calfcord/discord/messages.py              |       18 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/persona.py               |      154 |       58 |       34 |        1 |     57% |260-262, 265-266, 274, 278-284, 288-293, 332-373, 507-534, 539-546 |
| src/calfcord/discord/receiver.py              |       47 |       27 |        4 |        0 |     39% |43-45, 55-57, 64-65, 68-87, 95-97, 101-103, 110-111, 114-115, 118 |
| src/calfcord/discord/sender.py                |       45 |       28 |       10 |        0 |     31% |36-37, 40-41, 49, 53-60, 64-68, 81-85, 112-131 |
| src/calfcord/discord/settings.py              |       12 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/typing.py                |       39 |        3 |        6 |        0 |     93% |     82-87 |
| src/calfcord/health/check.py                  |       38 |        0 |        6 |        0 |    100% |           |
| src/calfcord/health/heartbeat.py              |       37 |        0 |        2 |        0 |    100% |           |
| src/calfcord/health/refresher.py              |       28 |        0 |        2 |        0 |    100% |           |
| src/calfcord/mcp/agent\_select.py             |       13 |        0 |        4 |        0 |    100% |           |
| src/calfcord/mcp/capability\_read.py          |       22 |        0 |        0 |        0 |    100% |           |
| src/calfcord/mcp/config.py                    |      132 |        2 |       60 |        1 |     98% |  149, 207 |
| src/calfcord/mcp/config\_write.py             |       44 |        1 |       14 |        0 |     98% |        82 |
| src/calfcord/mcp/runner.py                    |       39 |       13 |        2 |        1 |     66% |68-79, 83-92, 96 |
| src/calfcord/mcp/selector.py                  |       35 |        0 |       12 |        0 |    100% |           |
| src/calfcord/providers/codex/\_paths.py       |        6 |        0 |        0 |        0 |    100% |           |
| src/calfcord/providers/codex/cli.py           |      151 |       79 |       40 |        1 |     43% |79-100, 104-111, 115-134, 144-154, 188-189, 223-247, 251 |
| src/calfcord/providers/codex/factory\_hook.py |       10 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/jwt.py           |       25 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/model\_client.py |       98 |        1 |       26 |        3 |     97% |189, 361-\>357, 374-\>377 |
| src/calfcord/providers/codex/prompt\_cache.py |      122 |       14 |       24 |        6 |     86% |111-113, 129-\>135, 181-182, 191, 195, 201, 203-\>193, 220-\>exit, 223-224, 244-248 |
| src/calfcord/providers/codex/prompts.py       |      223 |       14 |       60 |        7 |     93% |207, 211-\>exit, 213, 216-217, 263-264, 314, 342-343, 348, 354, 377-378, 555 |
| src/calfcord/providers/codex/token\_store.py  |       44 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/\_slot\_ops.py        |       87 |        5 |       22 |        1 |     94% |194-195, 228-230 |
| src/calfcord/supervisor/\_workspace.py        |      189 |        4 |       42 |        1 |     98% |189, 296-298 |
| src/calfcord/supervisor/client.py             |       59 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/component.py          |       47 |        0 |       14 |        0 |    100% |           |
| src/calfcord/supervisor/compose.py            |       66 |        0 |       14 |        2 |     98% |148-\>150, 232-\>234 |
| src/calfcord/supervisor/lifecycle.py          |      388 |        1 |      104 |        2 |     99% |231-\>236, 1141 |
| src/calfcord/supervisor/mcp\_roster.py        |      140 |        3 |       52 |        2 |     97% |330-331, 335 |
| src/calfcord/supervisor/procspawn.py          |      239 |       15 |       60 |        5 |     93% |302, 442-443, 524, 533-534, 540, 543, 562-563, 565, 661-662, 709-710 |
| src/calfcord/supervisor/roster.py             |      290 |        5 |       92 |        2 |     98% |603-604, 670-672 |
| src/calfcord/tools/deploy\_filters.py         |      114 |        0 |       62 |        0 |    100% |           |
| src/calfcord/tools/runner.py                  |       56 |        1 |        6 |        1 |     97% |       169 |
| **TOTAL**                                     | **8355** |  **502** | **2210** |  **106** | **93%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fryan-yuuu%2Fagent-disco%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.