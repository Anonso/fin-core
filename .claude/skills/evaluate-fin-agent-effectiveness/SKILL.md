---
name: evaluate-fin-agent-effectiveness
description: Compare FIN with the same-version direct Agent for answer quality and product usefulness. Use for FIN/direct A/B, R1 effectiveness gates, suspected Agent regression, or proving that account, G, continuity, or other proprietary context adds value; not for transport-only smoke tests.
---

# Evaluate FIN Against the Direct Agent

Build a red-capable comparison that can prove FIN made the strong Agent worse.
Transport success, valid JSON and completed turns are prerequisites, not the
effectiveness verdict.

## Freeze comparable pairs

- Use the same model, reasoning effort, exact questions, turn order, language
  and time horizon.
- Keep session boundaries equivalent. Record fresh, continued and degraded-fresh
  turns rather than treating them as interchangeable.
- Freeze account snapshots and other time-sensitive inputs when possible. If an
  account snapshot changes between arms, mark that chain sequential-effect only,
  not strictly comparable.
- Keep the direct arm free of FIN prompts, schemas, context, renderers and
  evaluators. Keep the FIN arm on the real public product path.
- Hash the question set and record release/runtime identity without credentials
  or personal data.

Use `automate-feishu-desktop-e2e` when the FIN arm must prove real desktop
ingress and display. One message at a time; correlate delayed replies by nonce
and platform timestamp.

## Judge user value

Compare the displayed answers on:

- correctness and unsupported claims;
- directness and clarity of conclusion;
- reasoning depth, counterarguments and falsifiers;
- appropriate use of account or owned knowledge;
- preservation of uncertainty without refusing a safe answer;
- repetition, template noise and user effort.

Blind or independent reviewers improve a formal gate, but do not hide an
obvious minimal counterexample behind missing reviewer ceremony. Preserve both
the practical observation and the stricter acceptance status.

## Interpret results

- A non-account chain is the direct-Agent floor: FIN should not lose when it has
  no relevant proprietary context.
- An account chain should normally favor FIN because the direct arm lacks the
  user's trusted state. It is not strict evidence if the snapshots differ.
- A G/context win counts only when the answer visibly uses relevant sourced
  knowledge; merely reporting that the knowledge system ran is not a gain.
- A presentation that discards Agent reasoning is a FIN regression even if the
  hidden structured product is rich.
- A generic gap footer, internal ID, profile, receipt or confirmation request is
  friction unless it changes a real user decision or protects a side effect.

Report per-chain winners, strict comparability, material regressions and the
smallest failing sample. A product passes only when FIN has zero strictly
comparable losses and demonstrates gains in the classes where its proprietary
context is relevant. Keep delivery/display evidence separate from this verdict.
