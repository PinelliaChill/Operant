---
name: documents
description: Create a Word document from user content and verify its readable structure.
---

Use the current trusted workspace. Ask only for missing content that changes the
document. Write a UTF-8 JSON spec in the workspace with a descriptive title and
1-30 sections. Each section has heading plus paragraphs and/or bullets, both arrays
of strings. Example:
{"title":"Project brief","sections":[{"heading":"Purpose","paragraphs":["What this project does."],"bullets":["One goal"]}]}

Create the file through the formal workspace tools, then invoke
`<runtime Python> -m operant.default_skill_tools make docx spec.json output.docx`
through run_command. The runtime Python path is supplied with the installed Skill
context. This needs the installable Operant `artifacts` extra. The helper reads back
the DOCX; also inspect the generated file and report the exact workspace path.
Never claim a Word file exists based only on a model answer or command plan.
