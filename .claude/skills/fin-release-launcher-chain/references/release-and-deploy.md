# Rebuild release and deploy

Read this when a launcher/runtime fix must reach the production gateway. The
authoritative procedure is
`docs/runbooks/hermes-official-runtime-upgrade.md`; this reference summarizes
the FIN-specific traps and never overrides that runbook.

## Build boundary

Run tests only from the development checkout. Default to focused + direct
blast-radius tests; do not run a full suite without first proving why those
bounded suites are insufficient. Never run pytest inside an immutable release
and never run release diagnostics without `-B` and
`PYTHONDONTWRITEBYTECODE=1`: `.pytest_cache`/`__pycache__` makes readiness fail.

Commit and push first, then build only from the full 40-character SHA:

```text
PYTHONDONTWRITEBYTECODE=1 uv run python -B scripts/build_fin_release.py \
  --commit <full-sha> --home /home/ypk
```

The builder performs detached worktree creation, frozen sync, permission
convergence, record-sync, prepare, and check. Require `status=ready` and then
repeat the candidate-owned clean-env check:

```text
env -i HOME=/home/ypk LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  <candidate>/.venv/bin/python -I -B \
  <candidate>/scripts/prepare_fin_release.py check \
  --home /home/ypk --commit <full-sha>
```

Require `ready=true`, `frozen_sync_receipt=true`, and both
`unexpected_untracked=[]` and `unexpected_ignored=[]`.

## Current-release priority

Spend deployment effort on proving the candidate/current release and its real
product path. Treat the prior release only as an input to the minimum safe
cutover checks. Do not proactively repair, enhance, rehearse, or maintain its
rollback behavior. Handle the prior narrowly only when the user explicitly
asks or an actual failed cutover needs service recovery.

## Prior readiness before stop

Check the current release while the gateway remains online. Normal activation
requires the prior to be fully ready.

- If the only prior pollution is bounded canonical source `__pycache__`, use
  the candidate-owned `preflight-runtime-bytecode-quarantine` and runbook §6.1.
  Do not stop until the preflight returns ready.
- `.pytest_cache` is not accepted by that operator. Do not hide it, rewrite the
  frozen receipt, or stop first. With explicit authorization, verify the exact
  directory is owner-controlled, contains only reconstructible pytest cache,
  and has no symlink/special entries; move that exact directory to an
  owner-only recoverable quarantine, retain its inventory, re-run current
  check, then re-run the candidate bytecode preflight. Without that authority,
  stop and ask.
- Any tracked drift, unexpected path outside those exact caches, receipt/venv
  mismatch, or failed preflight blocks deployment. Do not improvise a degraded
  cutover unless the canonical runbook explicitly allowlists that exact prior.

## Deploy sequence

Use candidate/current-owned scripts with `env -i`, `-I -B`, and the exact full
SHAs throughout.

1. Pass the candidate prestop check and, when needed, the online prior recovery
   preflight. While the gateway is still online, require the installed gateway
   base, sole drop-in, `codex-route-runtime`, `codex-proxy-a`, and
   `codex-proxy-b` to byte-match the active current release and have their
   canonical modes. This active-current check is separate from validating the
   candidate sources, which may intentionally differ. If it fails, keep the
   service online and diagnose or narrowly converge the exact file before
   cutover.
2. Verify the existing gateway base unit and sole drop-in are exact owner
   regular non-symlink single-link files. The canonical boundary is base
   `0600`, drop-in directory `0700`, drop-in `0600`. If only these modes have
   drifted and the exact identities/content are already proven, converge these
   three explicit paths before re-running the complete pre-install guard; never
   chmod a broader systemd directory.
3. Stop `hermes-gateway-fin.service`; assert `MainPID=0` and state `inactive`.
4. If the online plan required bytecode recovery, run the candidate-owned
   `quarantine-runtime-bytecode`; then activate with
   `--expected-current-commit <prior-full-sha>` and verify `current` resolves to
   the candidate.
5. Run current-owned `apply_fin_hermes_external_integration.py apply`, then
   `check`, for the new full SHA.
6. Install the candidate gateway base/drop-in with exact modes:

   ```text
   install -m 0600 <candidate>/hermes-migration/systemd/hermes-gateway-fin.service \
     /home/ypk/.config/systemd/user/hermes-gateway-fin.service
   install -d -m 0700 \
     /home/ypk/.config/systemd/user/hermes-gateway-fin.service.d
   install -m 0600 <candidate>/hermes-migration/systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf \
     /home/ypk/.config/systemd/user/hermes-gateway-fin.service.d/20-fin-python-safety.conf
   ```

7. Reinstall all three fd-bound launcher/runtime files even if their paths did
   not change:

   ```text
   install -m 0700 <candidate>/scripts/codex_route_runtime.sh /home/ypk/.local/bin/codex-route-runtime
   install -m 0700 <candidate>/scripts/codex_proxy_a.sh /home/ypk/.local/bin/codex-proxy-a
   install -m 0700 <candidate>/scripts/codex_proxy_b.sh /home/ypk/.local/bin/codex-proxy-b
   ```

   Skipping this can yield `shared runtime bytes drifted` after an otherwise
   successful restart.
8. `systemctl --user daemon-reload`, start the gateway, and keep the runbook's
   fail-closed stop/rollback trap active until post-start acceptance completes.
9. After post-start acceptance passes, close the deploy with one candidate-owned
   `record-sync` for the new current:

   ```text
   env -i HOME=/home/ypk LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
     <candidate>/.venv/bin/python -I -B <candidate>/scripts/prepare_fin_release.py record-sync \
     --home /home/ypk --commit <candidate-full-sha>
   ```

   Step 7's launcher reinstalls churn the handoff binding captured in every
   release receipt; without this re-record the NEXT cutover's prior-ready gate
   fails (`frozen_sync_receipt=false`) even though nothing drifted. Re-recording
   after acceptance keeps each deploy cycle self-consistent. Proven twice on
   2026-08-27 (commits d340e3b0/a045e414 cutovers). It is not a tamper gate:
   content drift of installed launchers stays covered by the pre-stop byte-match
   check and the runtime child's fd-bound assertion.

Run these as visible phases rather than one opaque command. Emit the phase
name before each mutation/check and preserve the first failing command's
stderr. Cleanup and rollback output may be compact, but must not replace the
original failure evidence. A successful rollback changes the pointer binding:
rerun online preflight and obtain fresh authorization for the newly returned
exact cutover/pointer digest before retrying.

## Post-start acceptance

Reduce user involvement before the final real-client sample:

- Do not use `POST /v1/chat/completions` as a Feishu probe. Its transport
  identity is `api_server`, it has no Feishu sender/chat/message envelope, and
  its response stays on HTTP.
- After the product-shaped run succeeds, send that exact saved answer through
  official `hermes --profile <profile> send --to feishu:<chat-id> --file
  <answer-path> --json`. Require exit 0, `success=true`, `platform=feishu`, and
  a non-empty `message_id`.
- If the installed Feishu SDK and app permissions allow it, call
  `im.v1.message.get` for that ID and require one matching message. Query
  `read_users` only as an optional displayed signal; unsupported/read-user
  errors leave displayed unknown rather than failing dispatch acceptance.
- Store this as split-path evidence. It proves the production answer can be
  produced and that Feishu accepted and retained the outbound message, but it
  does not prove a Feishu client-originated inbound event or same-turn
  correlation. Never forge a delivery ledger correlation between the two.
- Ask the user for at most one natural Feishu message and visual confirmation
  after all of the above is green. Automate the resulting public-entry,
  injection, dispatch, and ledger correlation checks.

Require all of the following before updating status:

- `current` resolves to the new full SHA; gateway is `active/running` with a
  fresh positive PID.
- `DropInPaths` contains only `20-fin-python-safety.conf`; the live process has
  `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`, and
  `PYTHONDONTWRITEBYTECODE=1` without dumping its full environment.
- Candidate-owned release check is still ready with both unexpected arrays
  empty.
- Installed runtime/A/B launchers byte-match current and have mode `0700`.
- Official Hermes `plugins list` and `mcp test fin-analyse` pass; the Feishu
  websocket is connected.
- Same-route launcher chat reaches a complete terminal event; a production
  product-shaped AgentRun returns a schema-valid payload.
- One real Feishu consultation reaches public-entry, dispatch, and user-visible
  display. Only this final evidence supports `live/product complete`.

Record the new current SHA and evidence path in `docs/pm/NOW.md`. Never treat a
raw HTTP/urllib result, component chat, process liveness, or dispatch acceptance
as displayed delivery.

## Known deploy snags (2026-08-26/27, verified)

- **The running gateway regenerates plugin bytecode.** While the gateway (or
  `hermes chat`) runs, `hermes-migration/plugins/fin-consultation-first-tool/`
  gains `__pycache__/__init__.cpython-311.pyc` even though the unit drop-in sets
  `PYTHONDONTWRITEBYTECODE=1`. The next cutover's prior check then reports
  `unexpected_ignored` and `frozen_sync_receipt=false`, and the candidate-owned
  `preflight-runtime-bytecode-quarantine` can refuse with "prior frozen-sync
  receipt cannot be restored by source quarantine".
- **Recovery that works:** if the only pollution is that single plugin pyc, move
  the exact file to an owner-only recoverable quarantine dir (record its
  sha256), then run the candidate-owned `record-sync --commit <prior-full-sha>`
  to refresh the prior receipt, then re-run `check` (must be `ready=true`),
  then `activate`.
- **Why the receipt drifts:** the deploy sequence reinstalls the fd-bound
  launchers (`codex-route-runtime`, `codex-proxy-a/b`), which changes their
  inodes. The current release's `.fin-frozen-sync.json` handoff binding records
  the launcher identities, so a reinstall invalidates the *new* current's own
  receipt for the next cutover. Expect the quarantine+`record-sync` recovery on
  every subsequent cutover until launcher installs stop churning inodes.
- **Activate is the authoritative prior gate.** Even if the quarantine preflight
  still refuses on a clean prior (an internal receipt-compat inconsistency),
  `activate --expected-current-commit <prior>` re-validates the prior itself; a
  green `check` plus a successful activate is the deploy authority.
