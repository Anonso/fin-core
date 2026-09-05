# Execution-path diagnostics

Read this when a consultation/review fails or a probe result is ambiguous.
Goal: find which of the three launcher-chain layers breaks, using only
read-only commands until a fix is authorized.

## 1. Configuration and status

```text
PYTHONDONTWRITEBYTECODE=1 \
  /home/ypk/.local/share/fin-analyse/current/.venv/bin/fin-codex-routes \
  --config /home/ypk/fin-data/codex_routes.yaml validate
PYTHONDONTWRITEBYTECODE=1 \
  /home/ypk/.local/share/fin-analyse/current/.venv/bin/fin-codex-routes \
  --config /home/ypk/fin-data/codex_routes.yaml status
```

`validate` proves the YAML schema (including any new fields such as
`reasoning.effort`). `status` shows ordered routes, models and local
readiness. A current release built before a new config field was added will
reject the YAML — that is expected until the release is rebuilt.

## 2. Probe through the launcher, not HTTP

`fin-codex-routes probe` now runs a launcher-mediated minimal chat for the
A/B slots. Outcome meanings:

| outcome | meaning |
| --- | --- |
| `reachable` | valid thread id + non-empty agent message + terminal turn through the attested launcher |
| `confirmed_unavailable` | launcher exit 78: deterministic local identity/attestation failure |
| `inconclusive` | timeout, ordinary nonzero exit, or incomplete/malformed event stream; allow the bounded product child to decide |

Probe output is not always pure JSON (child logs may interleave); read raw
output before parsing. The probe uses the route-owned model with
`model_reasoning_effort="low"`; it must not override route model identity to a
different Terra/Luna route merely to get a fast green result.

## 3. Manual launcher minimal chat

Run the exact attested environment so each layer is exercised:

```text
FIN_CODEX_ROUTE_LAUNCHER_ID=codex-proxy-b \
FIN_CODEX_ROUTE_ID=codex-proxy-b \
FIN_CODEX_ROUTE_LAUNCHER_PATH=/home/ypk/.local/bin/codex-proxy-b \
FIN_CODEX_ROUTE_HOME=/home/ypk/fin-data/codex-routes/codex-proxy-b \
FIN_CODEX_ROUTE_BASE_URL=https://ai.codesonline.dev \
FIN_CODEX_ROUTE_MODEL=gpt-5.6-sol \
FIN_CODEX_ROUTE_CONFIG_SHA256=<config fingerprint> \
FIN_CODEX_ROUTE_AUTH_SHA256=<auth sha256> \
timeout 60 /home/ypk/.local/bin/codex-proxy-b exec \
  --json --ignore-user-config --ignore-rules --strict-config \
  --skip-git-repo-check -s read-only \
  -c 'model_reasoning_effort="low"' \
  -c 'approval_policy="never"' \
  -c 'web_search="disabled"' ping
```

The config/auth SHA values are emitted by the route binder, not the YAML
digest:

```text
PYTHONPATH=/home/ypk/fin-core \
  /home/ypk/fin-core/.venv/bin/python -B -m \
  fin_analyse.guo_teacher_research.codex_route_binding \
  codex-proxy-b /home/ypk/.local/bin/codex-proxy-b
```

Success looks like `thread.started`, an `agent_message` (e.g. `pong`), and
`turn.completed`. This proves launcher transport only, not the formal
consultation schema or public entry.

## 4. Layer-by-layer triage

Run the runtime directly (not through the fd) with `bash -x` to see the
final `exec` line:

```text
FIN_CODEX_ROUTE_LAUNCHER_ID=codex-proxy-b ... \
  timeout 20 bash -x /home/ypk/.local/bin/codex-route-runtime exec \
  --json --ignore-user-config --ignore-rules --strict-config \
  --skip-git-repo-check -s read-only \
  -c 'model_reasoning_effort="low"' \
  -c 'approval_policy="never"' \
  -c 'web_search="disabled"' ping 2>&1 | tail -30
```

Read the last `exec /.../codex ...` line:

- `shared runtime identity is invalid` before any exec → identity check
  failed. Check `-L` handling for `/proc/self/fd/*`, release file modes
  (`& 022 == 0`), and sha match between `~/.local/bin/codex-route-runtime`
  and `current/scripts/codex_route_runtime.sh`.
- `shared runtime bytes drifted` → installed runtime and current release
  differ; reinstall the runtime after deploying the matching release.
- `caller route binding conflicts with config` → environment SHA values do
  not match the binder output; use the binder values.
- Provider args **before** `exec` (e.g.
  `codex -c 'model_provider=...' ... exec ...`) → the route provider is
  ignored and codex hits `api.openai.com` with the route key (401). The
  runtime must place `ROUTE_CONFIG_ARGS` **after** `FINAL_CODEX_ARGS`
  (which opens with `exec`).
- `401 Incorrect API key ... api.openai.com` after a correct invocation
  shape → endpoint/auth misconfiguration, not a local-chain bug.

## 5. Product-shaped boundary

After the component chat, run one neutral isolated `AgentRunRequest` through
the same adapter shape as production: same binary/launcher, route-bound env,
cwd policy, output schema, production model and production reasoning effort.
Keep FIN capabilities empty only when diagnosing schema/transport; do not
write semantic ledgers or send Feishu messages from this diagnostic.

Useful distinctions:

- A manual `codex-open` chat can succeed while the formal ProductContract
  fails. Console Go requires explicit `type` on enum nodes even where ordinary
  JSON Schema permits enum-only nodes.
- An `item.completed` whose item type is `error` is not itself a tool call. If
  a valid agent message and terminal turn follow, normal ProductContract
  validation decides the result. Real web/command/MCP/unknown item types remain
  tool-policy violations in a zero-tool run.
- `urllib`/raw HTTP may receive 403 while the exact Codex provider invocation
  succeeds. Treat raw HTTP as a transport hint, never as the authoritative
  credential verdict.

Do not declare the route healthy until the product-shaped run returns a
schema-valid payload. It still does not prove Hermes/Feishu delivery.

## 6. Reasoning-effort override

`reasoning.effort` is request-scoped and lives in the route YAML
(`reasoning: {effort: xhigh}`), consumed by
`_runtime_reasoning_effort` in `use_case_runner.py`; the hook and workflow
docs pin the same value. The A/B runtime also accepts
`-c 'model_reasoning_effort="low"'` for the component probe; the real product
request continues to carry the production `xhigh` budget.
