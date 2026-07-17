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
An agent reply delivered as one or more consecutive Discord messages, each within Discord's message length limit; the first message is anchored to the triggering message. Chunking is the only delivery mechanism for agent replies — there is no retry that asks the agent to shorten its reply.
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
