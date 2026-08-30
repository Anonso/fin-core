# FIN Codex route operations

Read this reference only after inspecting the current production YAML. Preserve complete route blocks and declaration order; YAML aliases and duplicate keys are rejected.

## Enable or disable

Change only `enabled: true|false`. Keep the block in place so re-enabling preserves its priority and identity. Before disabling, prove at least one other enabled route remains for every workload the node serves. Enabling requires local readiness and, for a previously unverified provider, the matching live check before activation.

## Reorder

Move the entire route block. The first enabled route for a workload is attempted first, so order is product behavior. Do not copy only selected fields or change the route ID while moving it. Validate the exact resulting consultation and review lists before restart.

## Change API, model, auth locator, workload, or budgets

- `api.base_url` must be HTTPS, contain no credentials/query/fragment, and use a normalized host/path.
- `model.id` is the upstream model identifier; `model.quality` is `pinned` or `degraded` and controls the visible degradation label.
- For `codex-provider`, `auth.path`, `auth.key_path`, and `model_catalog` are execution identity. The selected credential must be a string and the configured model slug must exist in the owner-only model catalog.
- `workloads` contains `consultation`, `review`, or both, except `codex-provider`, which is consultation-only.
- Per-route probe timeout is 1–60 seconds; attempt timeout is 30–1800 seconds.
- Global `probe.reachable_ttl_seconds` is 0–3600 seconds. Cooldown step is 1–3600 seconds, max is at least step and at most 86400 seconds, and half-open lease is 1–3600 seconds.

Any API/model/auth locator/auth bytes/model-catalog change intentionally changes execution identity. Existing continuation must not silently cross that boundary; a later fresh continuation may be reported as degraded according to the product contract.

## Add a standard Responses proxy

Use this shape for an OpenAI Responses-compatible proxy managed through the standard route home:

```yaml
- id: proxy-example
  enabled: true
  adapter: codex-responses
  workloads: [consultation, review]
  probe_timeout_seconds: 60
  attempt_timeout_seconds: 900
  api:
    base_url: https://api.example.com
  model:
    id: model-id
    quality: degraded
```

The route ID must start with `proxy-`. Its credential lives at `/home/ypk/fin-data/codex-routes/proxy-example/auth.json` as `OPENAI_API_KEY`, with the route directory mode `0700` and file mode `0600`. Do not create a route-local `config.toml`; the shared YAML is the only config owner.

## Add a native Codex provider

Use this shape when Codex itself must receive a custom Responses-compatible provider definition:

```yaml
- id: codex-example
  enabled: true
  adapter: codex-provider
  workloads: [consultation]
  probe_timeout_seconds: 60
  attempt_timeout_seconds: 900
  api:
    base_url: https://api.example.com/v1
  auth:
    path: /absolute/owner-only/auth.json
    key_path: [provider-name, key]
  model_catalog: /absolute/owner-only/models.json
  model:
    id: model-id
    quality: degraded
```

The route ID must start with `codex-`. `key_path` is a one-to-four-component lookup path into the JSON credential document. The auth and model catalog files must be regular, owner-only `0600` files. Clone this block for another compatible provider; no Python registration is needed.

## Add a direct route

`direct-codex` has no `api`, `auth`, or `model_catalog` fields and its ID starts with `direct-`. All such blocks use the single pinned direct runtime identity owned by production composition, so adding another block does not create another independent upstream. A request for another direct identity is not config-only; stop and rebaseline it as code/design work.

## Remove

Remove the complete YAML block, validate both workload lists, restart, and verify. Removing a route intentionally makes its old continuation identity unavailable. Do not delete its credential, model catalog, route-home directory, cooldown/session evidence, or historical Git data as part of the block removal; those may be shared or needed for audit. Clean them only under a separate exact deletion authorization after proving no caller or shared owner remains.

## Rename

A rename changes route identity. Treat it as add-and-remove, not an in-place label edit:

1. Add the new ID as a separate block and provision/attest it.
2. Activate and verify the new route.
3. Remove the old block in a second bounded mutation.

Expect old route-bound continuation to become unavailable rather than silently resume through the new ID.

## Roll back

Restore the prior YAML bytes, mode, and owner from the operation's recovery copy; run `validate`, restart the gateway, rerun `status` and MCP discovery, then record the restored config digest. Credential deletion is never part of automatic rollback.
