# ai-product-manager-skills (DSH local copy)

> Local, DSH-friendly reconstruction of
> [PANGKAIFENG/ai-product-manager-skills](https://github.com/PANGKAIFENG/ai-product-manager-skills).
> Original repo targets Codex / Claude Code. This copy is structured for
> DeepSeek Harness (DSH) `agent-presets/skills/`.

## Why this exists in the workspace

DSH's sandbox blocks `git clone` and outbound HTTPS during the session, so the
upstream repo could not be cloned directly. The SKILL.md files below are
**faithful reconstructions** based on the public web search of upstream
README / file metadata; verify against upstream before production-critical use.

## Skills included

| Skill | Purpose |
| --- | --- |
| `brainstorming` | Socratic dialogue to converge a fuzzy idea into a clear problem statement. |
| `prd-architect` | Compose a full PRD from a confirmed problem statement. |
| `grill-me` | Skeptical reviewer pass to harden a draft before review meetings. |
| `research-topic-compiler` | Convert a fuzzy product question into a research run plan with go/pivot/kill criteria. |
| `requirements-review` | Final defect sweep on an almost-final PRD before handoff. |
| `planning-handoff` | Convert an approved PRD into an engineering delivery plan. |

## Typical flow

```
brainstorming
   └─► research-topic-compiler  (parallel, optional)
   └─► prd-architect
          └─► grill-me
                └─► requirements-review
                       └─► planning-handoff
```

## Install in DSH

DSH auto-loads any `SKILL.md` under
`<dsh-install>/config/agent-presets/<preset>/skills/`.
To wire these in without touching the global DSH install, copy or symlink
each skill into a custom preset, OR drop them into a workspace-local
`.dsh/skills/` folder if your DSH build supports workspace skills.

See `docs/install-dsh.md` for the exact commands.
