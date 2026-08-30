---
name: fix-bug
description: Optional named bug-fix checklist for fin-analyse. Use only when the user explicitly invokes fix-bug or asks to apply this named workflow. Ordinary bug reports are diagnosed and fixed autonomously by the executor selected in the current prompt and do not auto-trigger this skill.
---

# Optional Fix-Bug Checklist

This is a task-local checklist, not a mandatory bug router. Outside an explicit invocation, follow the autonomous loop in `AGENTS.md`.

## Loop

1. Translate the reported symptom into one observable failure and identify the real public entry path.
2. Reproduce it with the smallest reliable test or runtime probe. If reproduction is unsafe or unavailable, state the evidence limit.
3. Trace the data and control flow to the root cause. Distinguish the cause from downstream error text and unrelated baseline failures.
4. Fix the smallest complete boundary. Remove a wrong abstraction or public path when that is the cause; do not preserve it only for compatibility without a real consumer.
5. Add or update behavior-focused regression coverage through the same interface callers use.
6. Run focused validation, adjacent regression, and any live smoke test justified and authorized by the risk.
7. Review the stable snapshot once, batch all in-scope blockers, and repair them together.

## FIN Boundaries

- Preserve FIN Domain Kernel + Replaceable Strong Agent; do not add a second generic Agent loop around Codex.
- Keep Hermes as bridge/display, not research orchestration owner.
- Preserve G/Z and evidence/cognition isolation.
- Fail closed for source trust, auth/security, RiskGuard, real trading, production writes, and provider contract violations.
- Do not turn a missing input or external dependency into a fake success.

Use at most 3 concentrated semantic repair rounds for the same slice. If a hard failure remains, re-slice or revisit the design rather than opening an unbounded repair loop.

OpenSpec, Superpowers, memory writes, dev-orchestrator, CC, and live production access are not implied by this skill. Use them only when independently and explicitly selected or authorized.

Report the root cause, changed behavior, validation evidence, baseline failures, Git/provenance, residual risk, and anything not run.
