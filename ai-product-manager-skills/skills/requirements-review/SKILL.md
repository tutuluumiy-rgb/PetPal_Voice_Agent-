---
name: requirements-review
description: Final pass review of a PRD or spec before engineering handoff. Checks completeness, testability, consistency, and risks. Use immediately before `planning-handoff`.
---

# Requirements Review

Run a structured, checklist-driven review on a near-final PRD. This is different from `grill-me` (which probes weak thinking); this checks **delivery readiness**.

## When to use

- The PRD is otherwise stable; you want a final defect sweep.
- You are about to hand off to engineering / planning.

## Checklist

- [ ] Every requirement is testable (or has an explicit non-testable rationale).
- [ ] Functional / non-functional requirements are separate.
- [ ] Out-of-scope is named.
- [ ] Success metrics have baseline + target + owner.
- [ ] Counter-metrics identified (so we don't win the wrong thing).
- [ ] Edge cases enumerated (empty, error, offline, abuse, concurrency).
- [ ] Dependencies and external contracts listed with owners.
- [ ] Risks have mitigation + trigger conditions.
- [ ] Open questions have owners + due dates.
- [ ] Rollout / telemetry / kill-criteria defined.
- [ ] Glossary / persona references consistent across the doc.

## Output

A short "review verdict" doc: ✅ ready / 🟡 ready with comments / 🔴 rework needed, plus a numbered list of must-fix items.

## Note
Synthetic — distilled from the public description of the repo. Confirm against upstream before relying on it.
