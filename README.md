<h1 align="center">🕺💃 Agent Disco</h1>

<h3 align="center">
Your agent team for anything. 

Message them from anywhere using Discord.
</h3>

<br>

<p align="center">
  <a href="https://github.com/calf-ai/calfkit-sdk"><img src="https://img.shields.io/badge/built%20with-🐮%20agents-6f42c1" alt="Built with calfkit"></a>
  <a href="https://github.com/ryan-yuuu/agent-disco/actions/workflows/ci.yml"><img src="https://github.com/ryan-yuuu/agent-disco/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/ryan-yuuu/agent-disco/tree/python-coverage-comment-action-data"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/endpoint.json" alt="Coverage"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/calf-ai/calfkit-sdk" alt="License"></a>
</p>

Message your agent team and watch them work in real time. When an agent needs a hand, it opens a thread and delegates to other agents or hands the task off. The agent swarm divides and conquers complex work, with a full messaging trail, right in Discord.

**Fully open source and self-hostable on your machine.**

<!-- Demo: record agents messaging each other in Discord threads → docs/assets/demo.gif, then uncomment. -->
<!-- ![Agent Disco demo](docs/assets/demo.gif) -->
> _📸 Demo coming soon_

### 🐮 Built on [calfkit](https://github.com/calf-ai/calfkit-sdk)

- **Agent Disco is built on [🐮 calfkit](https://github.com/calf-ai/calfkit-sdk)**, the SDK for highly-connected, event-driven, and scalable agents.

- **Want to build your own multi-agent system?** Start with the calfkit [quickstart](https://github.com/calf-ai/calfkit-sdk#quickstart) and [examples](https://github.com/calf-ai/calfkit-sdk/blob/main/examples/README.md).

## A connected *team*, not just a chatbot

- 🤝 **Agents choreograph work with no central orchestrator.** Agents dynamically consult peers and transfer work when someone else is a better fit. Using calfkit's native agent-to-agent `Messaging` + `Handoff` with runtime **mesh discovery**, any agent can reach any other. Every A2A exchange is streamed in a dedicated Discord thread.
- 🌎 **Self host and split the team across machines.** Every agent and tool is an independent service that talks over the mesh — run the whole team on one laptop or spread it across twenty hosts.
- ✏️ **Onboard new agents in <1 min.** A new agent can be configured in a Markdown file and added to the team instantly.
- 🧠 **Any model — including your ChatGPT subscription.** Each agent can run on its own provider: Anthropic, OpenAI, any OpenAI-compatible APIs, or **use your ChatGPT Plus/Pro plan**.
- 🛠️ **Built-in tools.** Agents get task-tracking, coding, and web search tools right out of the box.
- 🔌 **Proxy any MCP servers.** Agent disco is compatible with [Model Context Protocol](https://modelcontextprotocol.io). MCP servers are proxied through the mesh so agents discover it like any other tool.

## Install

**macOS, Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/agent-disco/main/scripts/install.sh | bash
```

When it finishes, **restart your terminal**.

## Quickstart

**1. Set up your own Discord app integration.** Follow [`docs/discord-setup.md`](./docs/discord-setup.md).

**2. Run the guided setup via the disco CLI.**

```bash
disco init
```

It walks you through picking a provider + model, creating your first agent, and connecting Discord.

**3. Say hello.** After the setup is complete, message your first agent in Discord:

```
!<agent_name> hello
```

Your first agent is live 🎉

## add MORE agents

Just run:

```bash
disco agent create
```

## What you just built

Your Agent Disco workspace: a local agent mesh, a Discord bridge, and your first agent. From here you can add more agents, tools, and even split the team across machines.

## CLI overview

A handful of CLI commands:

```bash
disco status                 # system status: agents, tools, discord bridge, etc.
disco agent create <name>    # create a new agent
disco agent start  <name>    # bring an agent online
disco logs -f                # read the workspace logs as it runs
disco <stop/start>           # stop / start the workspace
```

## Documentation

- See [`docs/`](docs/README.md).
- How-to guides: see [`How-to guides`](docs/README.md##How-to)

## Contributing

Issues and pull requests are welcome. 

See [`CONTRIBUTING.md`](./CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md), and [`SECURITY.md`](./SECURITY.md).

## License

[Apache-2.0](./LICENSE).

<br>

---

<p align="center">
  <strong>Built on <a href="https://github.com/calf-ai/calfkit-sdk">🐮 calfkit</a></strong> — the SDK to for highly-connected, event-driven, and scalable agents.<br>
  Building your own agent team? <a href="https://github.com/calf-ai/calfkit-sdk#quickstart">Start with the calfkit quickstart</a> · ⭐ <a href="https://github.com/calf-ai/calfkit-sdk">star the repo</a>
</p>
