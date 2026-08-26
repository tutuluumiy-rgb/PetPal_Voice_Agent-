# Install ai-product-manager-skills in DSH

DSH scans SKILL.md files under
`config/agent-presets/<preset>/skills/<skill-name>/SKILL.md`.

You have two recommended paths:

## Path A — Workspace-local (no global DSH edit)

1. Pick or create a preset you actually use (e.g., `cordis`).
2. Symlink each skill into the preset's `skills` folder:

```powershell
$dst = "C:\Users\Administrator\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\config\agent-presets\cordis\skills"
$src = "G:\hello\agent-ai语音\ai-product-manager-skills\skills"

Get-ChildItem $src | ForEach-Object {
  New-Item -ItemType Junction -Path (Join-Path $dst $_.Name) -Target $_.FullName
}
```

After this, restart DSH (or refresh the skill catalog if your build supports
hot reload). The six skills will appear under names
`brainstorming`, `prd-architect`, `grill-me`, `research-topic-compiler`,
`requirements-review`, `planning-handoff`.

## Path B — Copy (no symlinks)

Same as Path A but `Copy-Item -Recurse` instead of `New-Item -Junction`.
Choose this if DSH does not follow junctions.

## Verifying

After restart, ask DSH: "list your loaded skills" and confirm the six names
above appear. If only 2 appear (the DSH built-ins), the preset path was
wrong — try the other preset directories.

## Upgrading

Since this is a reconstructed copy, when upstream changes you can
re-fetch by editing each SKILL.md in place. The reconstruction notes
in each file link back to the upstream path.
