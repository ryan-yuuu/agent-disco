# Agent Disco

Agent Disco is a Discord-native assistant team made of independently deployed agents, tools, and bridge processes that collaborate through the calfkit broker.

## Language

**Sticky conversation**:
A Discord channel or thread whose ambient human messages are routed to the agent that owns the last successful visible agent reply in that same channel or thread.
_Avoid_: Sticky session, sticky replies session, cached agent

**Sticky owner**:
The agent currently assigned to receive ambient human messages for a sticky conversation.
_Avoid_: Assigned agent, cached sticky name

**Chunked reply**:
An agent reply delivered as one or more consecutive Discord messages, each within Discord's message length limit; the final message is anchored to the triggering message (the "↩ Replying to" affordance), while the transcript row used for tool-call replay is keyed to the first successfully posted chunk so tools hydrate before the whole answer. Chunking is the only delivery mechanism for agent replies — there is no retry that asks the agent to shorten its reply.
_Avoid_: Chunk-split fallback, retry-with-feedback

**Acting agent**:
The agent in control of the run right now. Starts as the mentioned agent and transfers on handoff. Only the acting agent may render to the human's thread or transfer that control.
_Avoid_: Owning agent, owner, run owner

**Step trace**:
The visible record of one turn's intermediate events, posted under the acting agent's persona and sealed with the run's outcome. Distinct from the agent's reply, which is the turn's answer.
_Avoid_: Progress, aggregate, step message, live progress

**Segment**:
One Discord message of a step trace — a contiguous run of rows sharing one persona.
_Avoid_: Chunk, block, part

**Row**:
One line of a step trace: one thing the agent did, in one of a fixed set of states.
_Avoid_: Block, line, entry, step line

**Consult**:
One agent asking another a question and awaiting its answer, without surrendering the human conversation — the asking agent stays in control of the turn throughout. Distinct from a handoff, which transfers that control.
_Avoid_: Private chat, agent-to-agent message, delegation

**Consulted agent**:
The agent a consult is addressed to. It does its own work and answers, and never speaks in the human's conversation. Called a *peer* throughout the code and docs; prefer this term where the distinction from a handoff target matters, since both are "peers".
_Avoid_: Sub-agent, callee

**A2A thread**:
The Discord thread recording one human turn's agent-to-agent interaction in full — what was asked, what each consulted agent did and answered, and any system notes. One per human turn that produced a consult. The work of the agent talking to the human is not part of it; that belongs to the human's conversation.
_Avoid_: Exchange thread, private chat

**Nested consult**:
A consult made *by* a consulted agent — one peer consulting another (B→C inside A→B). It lands in the same A2A thread as the turn's other consults, announced by a resolving row in the consulting agent's own trace so the peer's work never appears unannounced.
_Avoid_: Sub-consult, transitive consult
