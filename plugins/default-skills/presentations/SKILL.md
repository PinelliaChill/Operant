---
name: presentations
description: Create a PowerPoint deck from user content and verify its slide structure.
---

Write a UTF-8 JSON spec in the trusted workspace with title, optional subtitle,
and 1-20 slides. Each slide needs title and 1-8 concise bullet strings. Example:
{"title":"Project review","slides":[{"title":"Results","bullets":["One measured result"]}]}

Run `<runtime Python> -m operant.default_skill_tools make pptx spec.json output.pptx`
with the formal run_command tool. The runtime Python path is supplied with the
installed Skill context; the `artifacts` extra provides python-pptx. The helper
reopens the deck and checks slide count and title. Inspect the output path and
report actual slides. Keep claims tied to source content; avoid overloaded slides.
