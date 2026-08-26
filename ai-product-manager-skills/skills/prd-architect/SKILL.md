---
name: prd-architect
description: Compose a structured Product Requirements Document (PRD) from a confirmed problem statement. Use when the user asks for a PRD, requirements doc, spec, or wants to convert an idea/brainstorm/research output into a reviewable document.
---

# PRD Architect

Produce a complete, reviewable PRD that engineering, design, and leadership can all read in one sitting.

## When to use

- The problem and target user are already clear (use `brainstorming` first if not).
- The user explicitly asks for a PRD, spec, requirements doc, or "写 PRD".
- Output must hand off cleanly to `planning-handoff` or to engineering.

## Output structure

The PRD should contain, at minimum:

1. **Problem & opportunity** — what we observed, who is affected, why now.
2. **Goals & non-goals** — measurable outcomes and explicit out-of-scope.
3. **Users & use cases** — primary personas and 2–4 jobs-to-be-done.
4. **Success metrics & guardrails** — north-star + leading indicators + counter-metrics.
5. **Solution overview** — narrative + simple flow diagram; no premature UI detail.
6. **Detailed requirements** — functional (MoSCoW), non-functional (perf, privacy, a11y).
7. **Edge cases & failure modes** — empty states, rate limits, abuse, offline.
8. **Risks, dependencies, open questions** — with owners and decision dates.
9. **Rollout & learning plan** — phased launch, telemetry, kill criteria.
10. **Appendix** — glossary, links to research, prior art.

## Workflow

1. **Pull context** from the user-provided inputs (brainstorm notes, research, prior PRD).
2. **Confirm assumptions** for anything missing — ask in batches, not 1-by-1.
3. **Draft section by section**, in the order above. Inline-section reasoning, but final output is a single coherent document.
4. **Run `grill-me`** on the draft to harden weak sections.
5. **Emit the PRD** as Markdown with stable anchors so downstream skills can `references/` into it.

## Quality bar

- Every requirement is testable. "Fast", "intuitive" → "p95 < 400ms", "first-time success > 80%".
- Every metric has a baseline and a target.
- Every open question has an owner and a due date.

## Source
Reconstructed from public web search results for
[PANGKAIFENG/ai-product-manager-skills/skills/prd-architect/SKILL.md](https://github.com/PANGKAIFENG/ai-product-manager-skills/blob/main/skills/prd-architect/SKILL.md).
Verify against upstream before production use.
