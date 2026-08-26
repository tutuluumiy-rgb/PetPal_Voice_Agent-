---
name: grill-me
description: Stress-test a draft artifact (PRD, plan, design doc, OKR) by acting as a skeptical reviewer. Use right before review meetings, or when the user says "challenge this", "找漏洞", "压力测试".
---

# Grill Me

Adopt a skeptical reviewer persona and attack a draft to surface blind spots before a real review meeting does.

## When to use

- The user wants to harden a PRD, spec, OKR, or plan before sharing it.
- The user explicitly invokes "grill me", "挑刺", "challenge this".
- Run after `prd-architect` and before `requirements-review`.

## Workflow

1. **Read** the draft end-to-end.
2. **Map** it onto this checklist (skip N/A items):
   - Problem: Is this actually a problem? Evidence? Severity vs. frequency?
   - Users: Who is excluded? Edge personas?
   - Scope: What is non-goal discipline — anything creep in?
   - Metrics: Will the chosen metric actually move if we win?
   - Risks: Largest unmitigated risk? Single point of failure?
   - Dependencies: Are blockers named, owned, dated?
   - Trade-offs: What did we give up and why is that OK?
3. **Fire 5–10 probing questions** in priority order. Cite the section / line being challenged.
4. **Suggest concrete edits**, not just opinions.
5. **Stop** when the user has 3 unanswered questions in a row — that's the natural review boundary.

## Tone

Direct, specific, never hostile. Always tie the criticism to a user/customer/business impact.

## Source
Reconstructed from [PANGKAIFENG/ai-product-manager-skills/skills/grill-me/SKILL.md](https://github.com/PANGKAIFENG/ai-product-manager-skills/blob/main/skills/grill-me/SKILL.md).
