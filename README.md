# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                          |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|---------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| src/calfcord/\_atomic.py                      |       19 |        0 |        2 |        0 |    100% |           |
| src/calfcord/\_provisioning.py                |       19 |        0 |        6 |        0 |    100% |           |
| src/calfcord/\_worker\_runtime.py             |       41 |        0 |       14 |        0 |    100% |           |
| src/calfcord/agents/definition.py             |       98 |        0 |       24 |        0 |    100% |           |
| src/calfcord/agents/factory.py                |      113 |        2 |       42 |        3 |     97% |156, 172, 249-\>244 |
| src/calfcord/agents/identifier.py             |       26 |        0 |       10 |        0 |    100% |           |
| src/calfcord/agents/loader.py                 |       38 |        1 |       16 |        1 |     96% |       115 |
| src/calfcord/agents/md\_writer.py             |      113 |        0 |       28 |        3 |     98% |89-\>92, 340-\>346, 346-\>exit |
| src/calfcord/agents/memory.py                 |       53 |        0 |       10 |        0 |    100% |           |
| src/calfcord/agents/runner.py                 |      113 |       21 |       24 |        1 |     84% |276-307, 311-320, 324 |
| src/calfcord/agents/thinking.py               |       41 |        4 |       22 |        2 |     90% |103-107, 118-122 |
| src/calfcord/agents/tool\_selectors.py        |       11 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/a2a\_dispatch.py          |       65 |        0 |        6 |        0 |    100% |           |
| src/calfcord/bridge/a2a\_project.py           |      193 |        6 |       50 |        7 |     95% |381-\>389, 425-426, 438-442, 486, 489, 526-\>515 |
| src/calfcord/bridge/egress.py                 |       90 |        4 |       18 |        0 |     96% |97, 219-225 |
| src/calfcord/bridge/gateway.py                |      399 |       57 |      108 |       19 |     84% |141-142, 147-148, 170-171, 328, 332, 359-\>exit, 364-367, 371-372, 424, 441, 462-464, 468-\>472, 495, 550, 639, 658, 699, 700-\>744, 715-\>744, 721, 723, 740, 749, 750-\>765, 763, 803, 806, 818-820, 826-931, 935 |
| src/calfcord/bridge/history.py                |      276 |        7 |       86 |        5 |     97% |387-\>389, 399-\>401, 421-\>423, 665-672, 748, 898-\>900, 1081-1092 |
| src/calfcord/bridge/mention\_handler.py       |      194 |        3 |       58 |        2 |     98% |97, 467-468 |
| src/calfcord/bridge/normalizer.py             |       46 |        0 |        8 |        0 |    100% |           |
| src/calfcord/bridge/overrides.py              |       23 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/persona\_resolve.py       |       12 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/reply\_poster.py          |       89 |        0 |       12 |        0 |    100% |           |
| src/calfcord/bridge/roster.py                 |       29 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/settings.py               |       48 |        1 |        6 |        0 |     98% |        86 |
| src/calfcord/bridge/slash.py                  |       98 |        6 |       16 |        0 |     95% |74, 97, 185-186, 224-225 |
| src/calfcord/bridge/step\_events.py           |       56 |        3 |       16 |        1 |     94% | 73-74, 76 |
| src/calfcord/bridge/trace.py                  |      275 |       10 |       68 |        5 |     96% |449-454, 474-475, 576, 580, 664-669, 701-\>exit |
| src/calfcord/bridge/trace\_rows.py            |      170 |        4 |       52 |        4 |     96% |364, 400, 419, 444 |
| src/calfcord/bridge/transcript\_tree.py       |       77 |        4 |       40 |        3 |     94% |92-93, 110-113, 202-\>197, 215-\>210, 217-\>208 |
| src/calfcord/bridge/transcripts.py            |      154 |        3 |       18 |        0 |     98% |   181-185 |
| src/calfcord/bridge/wire.py                   |       40 |        0 |        6 |        0 |    100% |           |
| src/calfcord/broker/runner.py                 |       30 |        1 |        6 |        1 |     94% |        80 |
| src/calfcord/cli/\_agents.py                  |      127 |        5 |       26 |        1 |     96% |246-247, 289-291, 422-\>424 |
| src/calfcord/cli/\_editor.py                  |       28 |        1 |       12 |        1 |     95% |        92 |
| src/calfcord/cli/\_envfile.py                 |       55 |        2 |       32 |        2 |     95% |   75, 141 |
| src/calfcord/cli/\_fields.py                  |       78 |        0 |       30 |        0 |    100% |           |
| src/calfcord/cli/\_mcp.py                     |        8 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_prompts.py                 |       17 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_providers.py               |      179 |       14 |       50 |        4 |     91% |260-261, 266-267, 269, 346-\>350, 408, 447-448, 554-555, 557, 566-571 |
| src/calfcord/cli/\_supervisor.py              |       37 |        0 |       10 |        0 |    100% |           |
| src/calfcord/cli/agent\_create.py             |      142 |       11 |       42 |        2 |     93% |345-346, 537-543, 661-664 |
| src/calfcord/cli/agent\_edit.py               |      142 |       15 |       38 |        2 |     91% |98-102, 143-147, 171-173, 266-267, 296, 325-329 |
| src/calfcord/cli/agent\_inspect.py            |       74 |        2 |       20 |        2 |     96% |   89, 152 |
| src/calfcord/cli/agent\_lifecycle.py          |      110 |        2 |       34 |        1 |     98% |250-251, 265-\>267 |
| src/calfcord/cli/agent\_tools.py              |      100 |        0 |       30 |        0 |    100% |           |
| src/calfcord/cli/deploy.py                    |       86 |        1 |       30 |        2 |     97% |488-\>493, 497 |
| src/calfcord/cli/discord\_discovery.py        |      194 |       16 |       50 |        7 |     91% |223-224, 248-250, 392, 396, 503-\>501, 528-529, 548, 555, 563-564, 572-574 |
| src/calfcord/cli/doctor.py                    |      198 |        5 |       76 |        0 |     98% |   106-110 |
| src/calfcord/cli/explain.py                   |       17 |        0 |        2 |        0 |    100% |           |
| src/calfcord/cli/init.py                      |      323 |        0 |       84 |        1 |     99% |705-\>exit |
| src/calfcord/cli/logs.py                      |       77 |        0 |       28 |        0 |    100% |           |
| src/calfcord/cli/main.py                      |      376 |       14 |      148 |        5 |     96% |549, 569, 595, 932-936, 985-986, 1001-1002, 1019-1020, 1049 |
| src/calfcord/cli/mcp\_admin.py                |      184 |       16 |       74 |        4 |     91% |93, 99-\>101, 199, 278-280, 305-323 |
| src/calfcord/cli/setup\_state.py              |       44 |        0 |        4 |        0 |    100% |           |
| src/calfcord/cli/tool\_aliases.py             |       60 |        0 |        8 |        0 |    100% |           |
| src/calfcord/cli/tui/keys.py                  |       24 |        2 |        2 |        1 |     88% |  129, 135 |
| src/calfcord/cli/tui/line\_input.py           |       71 |        4 |       16 |        3 |     92% |55, 58-\>64, 121-122, 123-\>126, 147 |
| src/calfcord/cli/tui/progress.py              |       61 |        0 |       20 |        1 |     99% | 100-\>102 |
| src/calfcord/cli/tui/prompter.py              |       27 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/tui/render.py                |       60 |        0 |        6 |        0 |    100% |           |
| src/calfcord/cli/tui/state.py                 |       54 |        0 |       10 |        0 |    100% |           |
| src/calfcord/cli/tui/theme.py                 |       23 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/tui/widgets.py               |      125 |        0 |       28 |        0 |    100% |           |
| src/calfcord/discord/avatar.py                |        3 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/chunking.py              |      114 |        0 |       40 |        0 |    100% |           |
| src/calfcord/discord/messages.py              |       18 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/persona.py               |      156 |       45 |       34 |        4 |     66% |274-276, 279-280, 288, 292-298, 302-307, 350, 365-368, 370, 526-553, 558-565 |
| src/calfcord/discord/read\_service.py         |      102 |        0 |       28 |        0 |    100% |           |
| src/calfcord/discord/receiver.py              |       47 |       27 |        4 |        0 |     39% |43-45, 55-57, 64-65, 68-87, 95-97, 101-103, 110-111, 114-115, 118 |
| src/calfcord/discord/sender.py                |       45 |       28 |       10 |        0 |     31% |36-37, 40-41, 49, 53-60, 64-68, 81-85, 112-131 |
| src/calfcord/discord/settings.py              |       12 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/typing.py                |       39 |        3 |        6 |        0 |     93% |     82-87 |
| src/calfcord/health/check.py                  |       38 |        0 |        6 |        0 |    100% |           |
| src/calfcord/health/heartbeat.py              |       37 |        0 |        2 |        0 |    100% |           |
| src/calfcord/health/refresher.py              |       28 |        0 |        2 |        0 |    100% |           |
| src/calfcord/mcp/agent\_select.py             |       21 |        0 |        8 |        0 |    100% |           |
| src/calfcord/mcp/capability\_read.py          |       22 |        0 |        0 |        0 |    100% |           |
| src/calfcord/mcp/config.py                    |      133 |        2 |       60 |        1 |     98% |  158, 220 |
| src/calfcord/mcp/config\_write.py             |       44 |        1 |       14 |        0 |     98% |        82 |
| src/calfcord/mcp/runner.py                    |       37 |        8 |        2 |        1 |     77% | 80-89, 93 |
| src/calfcord/mcp/selector.py                  |       35 |        0 |       12 |        0 |    100% |           |
| src/calfcord/providers/\_paths.py             |       10 |        0 |        0 |        0 |    100% |           |
| src/calfcord/providers/auth\_cli.py           |       21 |        1 |        4 |        1 |     92% |        49 |
| src/calfcord/providers/codex/\_paths.py       |        3 |        0 |        0 |        0 |    100% |           |
| src/calfcord/providers/codex/cli.py           |      154 |       69 |       40 |        5 |     52% |89, 91, 94-103, 107-128, 132-139, 150-162, 172-182, 216-217, 251-259, 263 |
| src/calfcord/providers/codex/factory\_hook.py |       10 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/jwt.py           |       25 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/model\_client.py |       98 |        1 |       26 |        3 |     97% |189, 361-\>357, 374-\>377 |
| src/calfcord/providers/codex/prompt\_cache.py |      122 |       14 |       24 |        6 |     86% |111-113, 129-\>135, 181-182, 191, 195, 201, 203-\>193, 220-\>exit, 223-224, 244-248 |
| src/calfcord/providers/codex/prompts.py       |      223 |       14 |       60 |        7 |     93% |207, 211-\>exit, 213, 216-217, 263-264, 314, 342-343, 348, 354, 377-378, 555 |
| src/calfcord/providers/codex/token\_store.py  |       41 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/grok/cli.py            |      111 |        2 |       30 |        2 |     97% |  160, 165 |
| src/calfcord/providers/grok/credentials.py    |       66 |        2 |       14 |        2 |     95% |   52, 102 |
| src/calfcord/providers/grok/factory\_hook.py  |       22 |        0 |        4 |        0 |    100% |           |
| src/calfcord/providers/grok/model\_client.py  |       45 |        0 |        6 |        0 |    100% |           |
| src/calfcord/providers/grok/models.py         |      157 |        2 |       36 |        1 |     98% |  211, 225 |
| src/calfcord/providers/grok/oauth.py          |      200 |        4 |       56 |        2 |     98% |277, 435-437 |
| src/calfcord/providers/grok/token\_store.py   |      101 |        0 |        6 |        0 |    100% |           |
| src/calfcord/supervisor/\_progress.py         |       14 |        0 |        0 |        0 |    100% |           |
| src/calfcord/supervisor/\_slot\_ops.py        |       88 |        5 |       24 |        2 |     94% |157-\>159, 202-203, 236-238 |
| src/calfcord/supervisor/\_workspace.py        |      189 |        4 |       42 |        1 |     98% |189, 296-298 |
| src/calfcord/supervisor/client.py             |       59 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/component.py          |       47 |        0 |       14 |        0 |    100% |           |
| src/calfcord/supervisor/compose.py            |       66 |        0 |       14 |        2 |     98% |148-\>150, 232-\>234 |
| src/calfcord/supervisor/lifecycle.py          |      395 |        1 |      108 |        3 |     99% |239-\>244, 715-\>723, 1192 |
| src/calfcord/supervisor/mcp\_roster.py        |      140 |        3 |       52 |        2 |     97% |330-331, 335 |
| src/calfcord/supervisor/procspawn.py          |      239 |       15 |       60 |        5 |     93% |302, 442-443, 524, 533-534, 540, 543, 562-563, 565, 661-662, 709-710 |
| src/calfcord/supervisor/roster.py             |      290 |        5 |       92 |        2 |     98% |606-607, 673-675 |
| src/calfcord/tools/deploy\_filters.py         |      114 |        0 |       62 |        0 |    100% |           |
| src/calfcord/tools/discord.py                 |       11 |        0 |        0 |        0 |    100% |           |
| src/calfcord/tools/runner.py                  |       56 |        1 |        6 |        1 |     97% |       169 |
| **TOTAL**                                     | **10198** |  **504** | **2698** |  **149** | **94%** |           |


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