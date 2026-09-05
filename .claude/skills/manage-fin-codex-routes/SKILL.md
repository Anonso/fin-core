---
name: manage-fin-codex-routes
description: Manage the FIN Codex route chain when a user asks to inspect, enable, disable, reorder, add, remove, rename, or change a route's API, model, auth locator, workload, timeout, probe cache, or cooldown, including safe reload, verification, and rollback. Use only for fin-analyse routing, not personal ~/.codex/config.toml or standalone Codex setup.
---

# Manage FIN Codex Routes

Change the production route chain through its single YAML owner, then prove the loaded chain matches the request. Do not create a second route registry or provider-specific Python branch.

## Fixed boundaries

- Production config: `/home/ypk/fin-data/codex_routes.yaml`, parent mode `0700`, file mode `0600`.
- Repository schema example: `/home/ypk/fin-core/config/codex_routes.yaml.example`.
- Deployed operator: `/home/ypk/.local/share/fin-analyse/current/.venv/bin/fin-codex-routes`.
- Declaration order is consultation/review priority. A route ID is execution and continuation identity, not a display label.
- Secrets stay in referenced owner-only credential files. Never put a secret in YAML, argv, evidence, logs, diffs, or the response.
- This skill never changes `/home/ypk/.codex/config.toml`.
- `codex-provider` is only for Responses-compatible native providers and only supports `consultation`. A new wire protocol, direct runtime identity, auth mechanism, or review-provider path is code/design work outside this config-only skill.

## Operation loop

1. Read the repository `AGENTS.md`, `docs/pm/NOW.md`, the current production YAML, its mode/owner, and the deployed operator's `validate` and `status` output. Also inspect Git status so unrelated WIP remains untouched. If the user requested only an explanation or proposal, stop before mutation. This step is complete when the current config digest, ordered enabled routes, local readiness, active release, and gateway state are known without exposing credentials.

2. Translate the request into the smallest YAML block change. Read [references/operations.md](references/operations.md) for the requested operation and adapter shape. Refuse a config-only shortcut when it would leave an affected workload with no enabled route, use `codex-provider` for review, or require an unsupported protocol/identity. This step is complete when every changed field and its continuation/failover effect are explicit.

3. Before mutation, create an owner-only evidence directory under `${XDG_STATE_HOME:-$HOME/.local/state}/fin-analyse/codex-route-operations/`; save the prior YAML, its SHA-256, and secret-free `validate`/`status` reports with directory mode `0700` and files mode `0600`. Do not copy credential files. Edit the production YAML with `apply_patch` and preserve mode `0600`. Provision or change a credential file only when the user supplied or explicitly authorized the exact credential operation; never print its contents. This step is complete when the diff contains only the requested config fields and the recovery copy is readable only by the owner.

4. Run both gates with `PYTHONDONTWRITEBYTECODE=1`:

   ```text
   /home/ypk/.local/share/fin-analyse/current/.venv/bin/fin-codex-routes --config /home/ypk/fin-data/codex_routes.yaml validate
   /home/ypk/.local/share/fin-analyse/current/.venv/bin/fin-codex-routes --config /home/ypk/fin-data/codex_routes.yaml status
   ```

   Require `ok=true`, the exact expected order/workloads/model, and `local_status=ready` for every enabled non-direct route. `probe` is an optional live connectivity check: exit `2`, HTTP 403, or `inconclusive` is not a schema failure and must not be relabeled as one. For a new or changed native provider, run an exact minimal Codex CLI canary through the attested `codex-provider` execution shape with an isolated temporary Codex home; keep the credential only in the child environment and record only route/model/config hash, exit code, thread-started, and expected-message booleans. This step is complete only when local gates pass and any required live result is classified honestly.

5. If the user authorized the route mutation, restart `hermes-gateway-fin.service`; a YAML edit alone does not activate the immutable in-process route snapshot. Verify `ActiveState=active`, `SubState=running`, rerun `validate`/`status`, and use the exact Hermes environment bound by the systemd unit to run `hermes --profile fin mcp test fin-analyse`. Check Lark connection only as a boolean; never print raw WebSocket URLs, tickets, access keys, or query strings. Save secret-free after evidence beside the before evidence. This step is complete when the running gateway, MCP discovery, config digest, and route status all describe the same requested chain.

6. If validation, composition, restart, MCP discovery, or the required canary fails, restore the exact prior YAML from the owner-only recovery copy, validate it, restart the gateway, and verify the restored chain. Do not delete a newly referenced credential or route-home directory during rollback unless that exact deletion is separately authorized and ownership is proven. Report the original failure and rollback result separately.

7. Report the final ordered chain, changed node fields, config digest, gateway/MCP status, live-probe or canary classification, and evidence location. Update `docs/pm/NOW.md` only when the change creates a durable current routing fact. A configuration operation does not require a new code release; code or tracked documentation changes still follow the repository's normal test, commit, push, and release rules.
