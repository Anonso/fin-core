---
name: design-fin-agent
description: Design or simplify FIN's dedicated advisory Agent, natural consultation path, personalized context, G knowledge use, continuity, or answer presentation. Use when FIN must become more useful than a direct Agent or when context/schema/gates are degrading the answer; not for route configuration or release operations.
---

# Design the FIN Agent

Design FIN as an enhancement around one strong advisory Agent. The direct Agent
is the quality floor. FIN may add trusted personal state, owned knowledge and
continuity, but must not own the investment prose or make an ordinary question
harder to answer.

## Start from the observable outcome

Before proposing code, freeze no more than five outcomes at the real Feishu
entry. Include these invariants when relevant:

- A natural question produces one useful natural answer without requiring a
  slash command, profile name, receipt or internal identifier.
- With no relevant FIN context, the answer is no worse than the same direct
  Agent on the same question and model.
- Relevant account or knowledge context produces an identifiable improvement.
- Missing optional context is silent and does not invalidate the answer.
- Only a real state write or execution-capable action introduces confirmation.

Do not add an Agent, evaluator, scheduler, state machine, compatibility path or
durable schema unless the frozen outcome proves it is necessary.

## Keep one deep consultation seam

Reuse the existing `AgentRuntimePort`. The ordinary read-only path should be
conceptually this small:

```text
natural question
→ strong Agent with a compact capability map
→ optional Agent-chosen reads
→ one natural answer
→ verbatim display
```

The runtime owns model invocation and continuation. FIN owns trusted tool
results and provenance. The Agent owns relevance, reasoning, synthesis and final
wording. The presenter must not select one claim, rewrite the answer, append a
generic gap list, or run a second model over it.

Prefer a plain answer string plus machine-owned runtime metadata. Do not require
the Agent to reproduce context IDs, source receipt structures, profiles,
dispositions, claim taxonomies, recognized decisions, bet expressions or action
readiness for an ordinary consultation. Record tool provenance from the actual
tool trace instead of asking the model to copy it.

## Pull context; do not push a dossier

Give the Agent a compact description of available data, not all data on every
turn. Let it read only what the question makes useful.

| Source | Authoritative for | Not authoritative for |
| --- | --- | --- |
| Portfolio | account quantities, cost and cash | whether a security is attractive |
| G knowledge | teacher cognition and investment method | current price, news or user intent |
| User decisions | explicit preferences and commitments | external market facts |
| Market evidence | time-sensitive price and events | personal constraints or teacher cognition |
| Conversation | current target and prior discussion | permanent preferences or external truth |

Retrieval may rank candidates by question relevance, authority within that
scope, freshness and explicit user priority. Ranking is a search aid, not a
gate. No match means the Agent answers normally. Persist only user-explicit
long-term preferences or decisions; do not turn Agent inference into permanent
personal data.

G is optional and relevance-driven. If used, preserve its source and distinguish
teacher material from the Agent's extrapolation. External evidence may test the
target thesis but does not validate, override or impersonate G. Missing or
irrelevant G creates no user-visible obligation.

## Put strictness at side effects

Hard gates are justified for principal/account ownership, credentials,
portfolio writes, explicit confirmation, CAS/concurrent overwrite prevention,
execution prohibition, resource limits and irreversible effects.

Ordinary reasoning is fail-soft: missing optional fields, G, live prices,
canonical tickers or continuation may reduce certainty or trigger one concise
degradation notice, but must not suppress a safe conditional answer.

Advisory language such as “能不能买” or “该不该减仓” is not an execution
request. Do not route it through trade readiness or account-write gates.

## Replace, do not relax one gate at a time

When the active path has accumulated classifiers, context options, structured
claims, finalization and templated presentation, replace the public path at one
seam. Do not add another compatibility layer or join hidden claim fragments in
the renderer. Once the replacement passes its public tests, delete the replaced
callers and implementation-coupled tests.

Acceptance must include a same-model direct-Agent comparison and a real Feishu
sample. Delivery success proves transport only; product completion requires no
strictly comparable loss and a visible gain where FIN has relevant proprietary
context.

## Capability bridge is the fragile link; verify it per consultation

FIN capabilities reach the runtime Agent through the `fin_capabilities` MCP
bridge (`local_capability_transport` + codex CLI config injection). Bridge
materialization has been intermittent in production (many real consultations
ran with zero callable tools; the Agent then honestly said it could not read
data, or worse, answered from priors). Rules learned 2026-08-26/27:

- Portfolio questions must not depend on the bridge: FIN injects the confirmed
  account facts directly into the prompt (server-bound ADVISORY_REAL), so the
  answer survives a zero-tool session. Market/teacher/evidence reads still
  depend on the bridge.
- A codex thread created while the bridge was broken stays zero-tool on later
  resumes (same thread, same rollout, no tools in any turn), silently producing
  degraded answers. Fresh threads in a healthy environment do get tools
  (verified: multiple fresh + healthy-resume sessions, 7–10 calls each).
- **Delete, do not repair (user decision 2026-08-27).** Stale zero-tool threads
  are disposable acceleration state: do not write per-thread detection,
  compatibility conversion, or resume-identity churn to "fix" them. Delete the
  stale continuation bindings (follow-ups then take the existing
  `DEGRADED_FRESH` degraded-fresh path) and archive the orphaned rollouts
  (owner-only, with a manifest) so they are recoverable but never resumed.
  Do **not** bind the resume identity to the release root: that forces every
  existing continuation fresh after any deploy and broke all continuations
  (`runtime_unavailable` with `codex_runtime_identity_invalid` on the
  fresh-after-mismatch path); that change was reverted. The FIN semantic chain
  is the authoritative continuity spine; codex threads are a disposable
  acceleration layer.
- Treat `capability_trace` (FIN side) and rollout `McpToolCall` items as the
  evidence of what the Agent really consumed; an empty trace on a capabilities
  question is a red flag, not normal behavior.
