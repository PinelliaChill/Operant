---
name: skill-creator
description: Create a local Skill package with valid Operant frontmatter and instructions.
---

Prepare a JSON spec in the trusted workspace with lowercase hyphenated name,
single-line description, and useful instructions. Example:
{"name":"review-notes","description":"Review a note for unclear claims.","instructions":"Check each claim against the supplied source."}
Run `<runtime Python> -m operant.default_skill_tools create-skill spec.json review-notes`
through the formal run_command tool. The helper creates `SKILL.md` only inside the
workspace and verifies discovery. Read back the file. It is a local candidate;
installation and project enabling remain explicit separate actions.
