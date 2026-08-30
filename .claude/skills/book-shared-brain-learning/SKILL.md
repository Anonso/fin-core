---
name: book-shared-brain-learning
description: Turn books, courses, public essays, user notes, and long-term methodology sources into FIN-owned SharedKnowledgeBrain learning designs and safe seed handoffs. Use for book/course learning, reusable knowledge cards, cross-source synthesis, or reference-only SharedKnowledgeBrain seeds; not for G synthesis or direct trade signals.
---

# Book Shared Brain Learning

Convert long-form material into reusable analytical knowledge, not a book
summary. Keep the result reference-only: it may improve how the Agent asks and
reasons, but it is not current market evidence, teacher cognition or a trade
signal.

Follow the active writer and delivery rules in the repository `AGENTS.md`.
Do not hard-code Claude Code or Codex as the implementation owner.

## Establish the learning purpose

Identify what the source should help FIN do better: for example, understand
policy transmission, analyze an industry, inspect accounting quality, value a
business, or challenge a thesis. Ask one path-changing question only when that
purpose cannot be inferred.

Translate investment material through this lens:

```text
source idea
→ analytical framework
→ investment question
→ observable validation
→ failure condition or blind spot
```

## Preserve source boundaries

Classify material as user notes, user-provided short excerpts, official/public
discovery, or secondary commentary.

- Do not ingest or store copyrighted full text.
- Do not treat reviews or public notes as the original source.
- Public discovery may support draft cards; production use requires the source
  boundary to remain visible and user review when confidence is limited.
- Browse and cite official or publisher sources when public discovery is needed.

## Design reusable cards

Each proposed `SharedKnowledgeItem` should have one job: an entry question,
concept map, causal chain, mechanism, validation checklist, failure condition,
blind-spot reminder or scenario frame.

Preserve these invariants:

```yaml
is_g_source: false
source_mode: reference_only
forbidden_usages:
  - teacher_direct
  - formal_g_synthesis
  - direct_trade_signal
metadata:
  applicable_tasks: []
  validation_questions: []
  confidence_boundary: draft_reference_only | user_reviewed_reference_only
  source_refs: []
```

Map a new card to existing knowledge as `reinforces`, `extends`, `refines`,
`contrasts_with`, `applies_to`, `depends_on`, or `risk_check_for`. Keep source
cards intact. A synthesis card must cite every source it combines and remain
reference-only.

## Keep one durable design source

Add the learning decision to the current task's single active design homepage.
Do not create parallel brief/design/plan/spec documents. If the current task has
no durable design and the user only asked for advice, provide the design in the
answer without creating repository files.

The design should state purpose, non-goals, source boundary, proposed cards,
relationships, synthesis candidates and observable acceptance. Link existing
architecture or product principles instead of copying them.

## Seed only when requested

Do not write runtime knowledge merely because the design is ready. When the user
requests implementation, use the current writer workflow and make preview the
default:

- build deterministic `SharedKnowledgeItem` objects;
- make apply explicit and idempotent by `item_id`;
- show the complete proposed item IDs and source boundaries before production
  apply;
- require exact authorization for the production write;
- refuse a non-interactive production confirmation;
- compare resolved paths so aliases or symlinks cannot bypass the production
  root check;
- verify uniqueness, provider visibility, source boundaries and a second
  idempotent apply.

Use OpenSpec or another compatibility workflow only when the user explicitly
requests it. Do not create a second control plane for an ordinary seed task.

Finish by identifying the design source, what was actually seeded, the public
verification result and what remains unperformed. Never persist full text,
credentials, private notes beyond the requested cards, or unsupported claims of
teacher cognition.
