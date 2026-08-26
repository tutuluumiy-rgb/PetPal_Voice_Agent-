---
name: planning-handoff
description: Convert an approved PRD into an engineering-ready delivery plan: milestones, workstreams, owners, dependencies, sequencing, and risks. Use after a PRD passes `requirements-review`.
---

# Planning Handoff

Turn an approved PRD into a delivery plan a tech lead or PM can run the next standup from.

## When to use

- The PRD has passed `requirements-review` (or the user is confident enough to skip).
- The user wants "sprint plan", "milestones", "工程排期", "交付计划".

## Output structure

1. **Scope summary** — one paragraph refresher + the PRD link.
2. **Workstreams** — each with: scope, deliverables, owner, depends-on, started-by date.
3. **Milestones** — M0 (kickoff), M1 (alpha), M2 (GA), each with exit criteria.
4. **Risks & mitigations** — top 5, each with owner and trigger.
5. **Telemetry / launch plan** — what we instrument, what thresholds we watch.
6. **Decision log placeholder** — for things we will re-decide after rollout.

## Workflow

1. **Read** the approved PRD.
2. **Decompose** by capability (not by team-org-chart). Team boundaries come later.
3. **Sequence** by critical path and unblocking, not by alphabetical order.
4. **Pull risks** from the PRD `Risks` section, and add delivery-specific risks (e.g., hiring, vendor onboarding, migration windows).
5. **Emit** the plan as Markdown, with stable anchors (`#ws-1`, `#m1`) for easy linking.

## Anti-patterns

- One-person workstreams with unclear accountability.
- Milestones tied to dates instead of exit criteria.
- "Risks" that are vague hopes ("we'll be careful").

## Note
Synthetic — distilled from the public description of the repo. Confirm against upstream before relying on it.
