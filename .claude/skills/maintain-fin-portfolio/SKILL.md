---
name: maintain-fin-portfolio
description: Design, diagnose, or fix FIN portfolio update and view behavior. Use for holding extraction, preview/confirmation, incomplete snapshot errors, stale display, or missing total-assets, market-value, position-ratio, cost, price, P&L, sellable, or margin fields; keep optional data nonblocking.
---

# Maintain FIN Portfolio State

Make the common path boring: extract the user's account facts, show one complete
preview, confirm once, replace atomically, and always render the best available
snapshot.

## Data policy

The user's basic snapshot consists of:

- total assets, total market value, available cash and position ratio;
- each instrument's name or ticker, held shares and cost basis.

Preserve supplied values. Reliably derive total market value or position ratio
when the source facts make that unambiguous, and label derived values internally.
Current price, P&L, P&L percentage, sellable shares and margin debt are optional:
show them when present and keep them unknown when absent. Never invent zero.

If a basic field is truly missing and cannot be derived, report all missing
basic fields together in the same preview or correction request. Do not reveal
one new blocker after each confirmation. An optional field must never make the
snapshot globally incomplete.

## Update path

```text
best-effort extraction → one preview → exact user confirmation → atomic replace
```

- The preview must contain every value that will be persisted and distinguish
  supplied, derived and unknown facts without exposing internal schemas.
- Require confirmation only for the write. A preview is read-only.
- Bind confirmation to the pending preview and account owner; use CAS or the
  existing single-owner mechanism to prevent stale or duplicate replacement.
- Do not add a second confirmation, completeness state machine or per-field
  repair workflow.

## Read path

Return the most recent confirmed snapshot whenever its stored core is usable.
Staleness changes the valuation note, not the existence of the holdings. Render
known fields, say “未知” only where useful, and do not turn missing optional data
into “当前持仓快照不完整”.

## Verification

Start with the smallest public-seam regression that fails on the reported user
experience, then cover preview → confirm → read. Assert rendered user fields and
persisted values, not private helper calls. Use a real Feishu desktop sample for
the final product check; stop after preview unless the user explicitly authorized
that exact confirmation and portfolio write.
