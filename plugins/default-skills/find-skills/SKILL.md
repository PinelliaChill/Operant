---
name: find-skills
description: Search locally available Skill candidates and report their verified names.
---

Use the local bundled Skill catalog, or an explicit trusted root inside the
current workspace. Run
`<runtime Python> -m operant.default_skill_tools find-skills QUERY`
through the formal run_command tool, optionally adding `--root RELATIVE_PATH`
for an explicit workspace catalog. The helper uses Operant's bounded discovery
validator and returns names and descriptions; it does not fetch remote packages.
Report only actual matches. Installation and enablement require explicit actions.
