# Company Brain: Retrieval Design for Mixed-Trust, Mixed-Structure Data

*A technical writeup on the retrieval and safety design behind Company Brain, an AI agent that turns a company's Google Drive documents and Slack conversations into a queryable knowledge base.*

## Problem

Internal knowledge exists in two fundamentally different forms: reviewed, structured documents (playbooks, docs) and raw conversational history (Slack threads). Naively embedding both into one vector store treats them as equivalent, but they aren't — one is authoritative and curated, the other is noisy, off-topic as often as not, and carries no guarantee of correctness. Retrieval that doesn't distinguish between them risks surfacing a stray Slack message with equal or greater weight than a reviewed playbook on the same topic.

## Design decision: dual-collection retrieval

The system splits retrieval into two separate collections rather than one combined index, queried independently and merged before generation.

**Document collection**: documents are split into 500-token chunks with ~50-token overlap, using `tiktoken` for counting and splitting at paragraph/sentence boundaries where possible. Chunks are embedded with `all-MiniLM-L6-v2` (384-dimensional vectors) via `sentence-transformers`.

**Conversation collection**: Slack threads (parent message + all replies) are grouped into a single chunk; standalone messages are grouped into 15-minute time windows. Each chunk is formatted as labeled dialogue with username attribution, and metadata includes `source_type: "slack"`, channel name, permalink, and date. Retrieval is plain cosine similarity — no recency weighting is applied yet, which means a six-month-old message currently ranks the same as one from this morning if the embeddings are equally similar.

**Merge**: results aren't re-ranked by a model — it's a fixed allocation (top-5 from the document collection, top 1-2 from the conversation collection), concatenated into the prompt with distinct labels (`[Source: ...]` vs. `[Playbook: ...]`). The system prompt instructs the model to prefer playbook content when both sources are relevant to the same question, effectively delegating the final re-ranking judgment to the LLM rather than a scoring function.

## Honest limitation: this was reasoned, not measured

The dual-collection split was an upfront architectural decision based on reasoning about the data (authoritative vs. noisy), not something discovered by running a single-collection version and watching it fail on a real query. There's no before/after example to point to, and no benchmark comparing answer quality or latency between the single- and dual-collection approaches. That's a real gap, not a detail I'm omitting for cleanliness — the natural next step is building a small eval set of question/answer pairs against the actual ingested data, to check whether the reasoning behind the split holds up empirically.

## Safety: retrieved content as untrusted input

Retrieved Slack messages and documents are user-generated, not system-authored — meaning they're a plausible vector for prompt injection (a document or message containing text that reads as an instruction to the agent rather than the user). Two things constrain this risk:

1. **Structural separation, not classification.** The system prompt explicitly instructs the model that retrieved content is informational only, and that only the actual user in the live conversation can request actions — instructions embedded in retrieved content are never to be treated as commands. There's no dedicated injection classifier or pattern-matching layer; the defense is instructional, at the prompt level.
2. **Iteration cap.** The agent is capped at 5 reasoning/tool-use loops per user message (`AGENT_MAX_ITERATIONS`). This is a conservative default meant to bound the damage of a runaway loop, not a threshold derived from measurement.
3. **Confirmation gating.** Any tool call with a real-world side effect (sending a Slack message, writing to Sheets, scheduling a calendar event) is structurally required to pass through `confirm_action(approved=True)` before `execute_tool` can run — the code path makes autonomous execution of side-effecting actions impossible without that gate, regardless of what the model decides to do.

The acceptance criteria for the build included a test case planting instruction-like text inside a document to confirm the model doesn't act on it — a reasonable test to run before treating this as validated.

## What's actually unresolved

- **No eval set.** There's no measured retrieval quality — the design is unvalidated against real queries on real data.
- **No persistent conversation storage.** State is currently in-memory and lost on restart.
- **No recency weighting** in Slack retrieval, despite recent messages almost always being more relevant.
- **No deduplication** in skill/playbook synthesis beyond title-matching, so near-duplicate playbooks can accumulate for similar procedures discussed multiple times.
- **Single-user only** — no multi-user support yet.

## Why this matters for document/data-intelligence work

The core problem — deciding what to trust and retrieve from unstructured, heterogeneous data before acting on it, under constraints that assume some of that data may be adversarial or simply wrong — is close in spirit to the layout-aware, deterministic-extraction problem in document intelligence: both require treating "what does this data actually mean, and how much should I trust it" as a first-class design question rather than an afterthought.
