---
name: pdf
description: Create a PDF document from user content and verify its page structure.
---

Write a UTF-8 JSON spec in the trusted workspace with title and 1-30 sections.
Each section needs heading and paragraphs and/or bullets as arrays of strings.
Run `<runtime Python> -m operant.default_skill_tools make pdf spec.json output.pdf`
with the formal run_command tool. The runtime Python path is supplied with the
installed Skill context; the `artifacts` extra provides reportlab and pypdf.
The helper reopens the PDF, checks at least one page, and reports its page count.
Inspect the actual file before reporting completion. Use descriptive titles,
support claims with supplied facts, and keep sections readable.
