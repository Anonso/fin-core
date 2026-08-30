---
name: fin-release-launcher-chain
description: Rebuild and deploy a FIN release when launcher/runtime code or the production consultation chain changed, then verify the candidate through the real product boundary. Use for runtime_unavailable, launcher identity/auth/schema failures, or “重建 release 并上线”; use manage-fin-codex-routes for config-only route changes, and do not use for personal Codex setup.
---

# FIN Release Launcher Chain

Get one candidate/current release working through the real FIN product path with
the shortest safe sequence. A release is an implementation detail; the outcome
is a useful answer delivered through the actual entry.

## Keep claims separate

1. Release readiness proves immutable local bytes and configuration.
2. Launcher execution proves the route can run a model.
3. A product-shaped call proves the FIN runtime seam.
4. A real Feishu-originated question and correlated displayed reply prove the
   user path.

Never promote an earlier claim into a later one. HTTP 200, tests, WebSocket
`connected`, plugin discovery or bot dispatch are not real inbound/displayed
evidence.

## Fast default path

1. Read `AGENTS.md`, `docs/pm/NOW.md`, Git status, current release identity and
   `fin-codex-routes validate/status`. Preserve unrelated WIP. If the requested
   change is YAML-only, use `manage-fin-codex-routes` and do not rebuild.
2. Reproduce the smallest failing production boundary. Start with the public
   product call when it is safely read-only; descend into launcher diagnostics
   only when that boundary fails. Read
   [references/execution-diagnostics.md](references/execution-diagnostics.md)
   for launcher, auth, schema or output ambiguity.
3. Apply one root-cause fix and run focused plus direct blast-radius checks.
   Full-suite testing requires a demonstrated unbounded impact surface.
4. Commit and push only the task-owned change, build one immutable candidate,
   and deploy it using
   [references/release-and-deploy.md](references/release-and-deploy.md).
5. Verify the new current, fresh gateway PID, MCP discovery and one natural
   product question. When real desktop evidence is required, use
   `automate-feishu-desktop-e2e` and keep only one message in flight.
6. Update `docs/pm/NOW.md` with the exact current SHA, highest proven product
   level and owner-only evidence path.

Prioritize the candidate/current release. Inspect the prior only as far as the
safe pointer change requires. Do not repair, enhance or rehearse the old release
unless an actual failed cutover needs narrow service recovery.

## Diagnose only the failed layer

- Launcher exit 78 is deterministic local unavailability. Timeout, ordinary
  nonzero exit or incomplete JSONL is inconclusive until the bounded product
  child decides.
- A launcher chat must produce a thread, non-empty Agent message and terminal
  turn. It still does not prove the product or Feishu.
- A product failure must preserve the narrow stage and sanitized stderr instead
  of collapsing into a generic runtime error.
- Provider arguments must follow the `exec` subcommand, installed fd-bound
  launcher/runtime bytes must match current, and shipped files must not be
  group/other writable.

Do not revive the rejected OAuth `send_as_user` experiment as an ingress
simulator: a platform user message without the matching Hermes receive event
does not prove admission. Do not add HTTP test ingress, private handler
injection, listeners, relays, portproxy rules or a second route registry to make
the gate green.

## Cutover boundaries

- Before stop, validate the candidate and prove installed/current identity.
- Keep the stop/activate/install/start window short and preserve the first
  failure.
- Reinstall the gateway unit/drop-in and all fd-bound route launchers from the
  candidate; then verify the running process is bound to the new current.
- A failed pointer-changing attempt requires fresh online preflight before a
  retry. Recovery must not hide the original failure.
- Secrets never enter argv, logs, evidence, diffs or the response.

Keep `.agents/skills/fin-release-launcher-chain/` and
`.claude/skills/fin-release-launcher-chain/` byte-identical. Detailed references
are conditional procedures, not steps to execute on every release.
