"""Portable artifact and Skill helpers used by the opt-in default Skill pack.

All writes are confined to the current trusted workspace. The helpers do not
install dependencies, run shell commands, or publish files.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from html import escape
from pathlib import Path
from typing import Any

from operant.package_resources import default_skill_root
from operant.skills.discovery import SkillDiscovery

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _workspace_path(value: str, *, output: bool = False) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path must stay inside the current workspace")
    if output and (path == root or path.is_symlink()):
        raise ValueError("output must be a new workspace file or directory")
    return path


def _text(value: Any, label: str, *, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text within {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label} contains unsupported control characters")
    return value.strip()


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError(f"{label} must be a list of at most 40 strings")
    return [_text(item, label, limit=4000) for item in value]


def _document_spec(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValueError("document spec must be an object")
    title = _text(value.get("title"), "title", limit=200)
    sections = value.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 30:
        raise ValueError("sections must contain 1-30 sections")
    normalized = []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("section must be an object")
        normalized.append(
            {
                "heading": _text(section.get("heading"), "heading", limit=200),
                "paragraphs": _strings(section.get("paragraphs", []), "paragraphs"),
                "bullets": _strings(section.get("bullets", []), "bullets"),
            }
        )
        if not normalized[-1]["paragraphs"] and not normalized[-1]["bullets"]:
            raise ValueError("each section needs content")
    return title, normalized


def _load_spec(path: str) -> Any:
    return json.loads(_workspace_path(path).read_text(encoding="utf-8"))


def _make_docx(spec: Any, output: Path) -> dict[str, Any]:
    Document = importlib.import_module("docx").Document
    shared = importlib.import_module("docx.shared")
    Inches, Pt = shared.Inches, shared.Pt

    title, sections = _document_spec(spec)
    doc = Document()
    doc.sections[0].top_margin = Inches(0.75)
    doc.sections[0].bottom_margin = Inches(0.75)
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(title, 0)
    for section in sections:
        doc.add_heading(section["heading"], 1)
        for paragraph in section["paragraphs"]:
            doc.add_paragraph(paragraph)
        for bullet in section["bullets"]:
            doc.add_paragraph(bullet, style="List Bullet")
    doc.save(str(output))
    reopened = Document(str(output))
    actual = [p.text for p in reopened.paragraphs]
    if title not in actual or not all(s["heading"] in actual for s in sections):
        raise RuntimeError("DOCX content readback failed")
    return {
        "format": "docx",
        "title": title,
        "sections": len(sections),
        "bytes": output.stat().st_size,
    }


def _make_pptx(spec: Any, output: Path) -> dict[str, Any]:
    Presentation = importlib.import_module("pptx").Presentation
    units = importlib.import_module("pptx.util")
    Inches, Pt = units.Inches, units.Pt

    if not isinstance(spec, dict):
        raise ValueError("presentation spec must be an object")
    title = _text(spec.get("title"), "title", limit=200)
    slides = spec.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= 20:
        raise ValueError("slides must contain 1-20 slides")
    deck = Presentation()
    deck.slide_width = Inches(13.333)
    deck.slide_height = Inches(7.5)
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = title
    cover.placeholders[1].text = _text(spec.get("subtitle", "Overview"), "subtitle")
    for item in slides:
        if not isinstance(item, dict):
            raise ValueError("slide must be an object")
        heading = _text(item.get("title"), "slide title", limit=200)
        bullets = _strings(item.get("bullets"), "slide bullets")
        if not 1 <= len(bullets) <= 8:
            raise ValueError("each slide needs 1-8 bullets for readable layout")
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = heading
        frame = slide.placeholders[1].text_frame
        frame.clear()
        for index, bullet in enumerate(bullets):
            if len(bullet) > 180:
                raise ValueError("slide bullet is too long for readable layout")
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = bullet
            paragraph.level = 0
            paragraph.font.size = Pt(24)
    deck.save(str(output))
    reopened = Presentation(str(output))
    if len(reopened.slides) != len(slides) + 1 or reopened.slides[0].shapes.title.text != title:
        raise RuntimeError("PPTX content readback failed")
    return {
        "format": "pptx",
        "title": title,
        "slides": len(reopened.slides),
        "bytes": output.stat().st_size,
    }


def _make_pdf(spec: Any, output: Path) -> dict[str, Any]:
    PdfReader = importlib.import_module("pypdf").PdfReader
    colors = importlib.import_module("reportlab.lib.colors")
    A4 = importlib.import_module("reportlab.lib.pagesizes").A4
    styles_module = importlib.import_module("reportlab.lib.styles")
    ParagraphStyle, getSampleStyleSheet = (
        styles_module.ParagraphStyle,
        styles_module.getSampleStyleSheet,
    )
    pdfmetrics = importlib.import_module("reportlab.pdfbase.pdfmetrics")
    UnicodeCIDFont = importlib.import_module("reportlab.pdfbase.cidfonts").UnicodeCIDFont
    platypus = importlib.import_module("reportlab.platypus")
    SimpleDocTemplate, Spacer, Paragraph = (
        platypus.SimpleDocTemplate,
        platypus.Spacer,
        platypus.Paragraph,
    )

    title, sections = _document_spec(spec)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "OperantBody",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=11,
        leading=17,
        spaceAfter=8,
        textColor=colors.black,
    )
    heading = ParagraphStyle(
        "OperantHeading",
        parent=base,
        fontSize=15,
        leading=21,
        spaceBefore=12,
    )
    title_style = ParagraphStyle(
        "OperantTitle",
        parent=base,
        fontSize=21,
        leading=29,
        spaceAfter=20,
    )
    story: list[Any] = [Paragraph(escape(title), title_style), Spacer(1, 8)]
    for section in sections:
        story.append(Paragraph(escape(section["heading"]), heading))
        for paragraph in section["paragraphs"]:
            story.append(Paragraph(escape(paragraph).replace("\n", "<br/>"), base))
        for bullet in section["bullets"]:
            story.append(Paragraph("• " + escape(bullet), base))
    SimpleDocTemplate(str(output), pagesize=A4, leftMargin=50, rightMargin=50).build(story)
    reader = PdfReader(output)
    if (
        len(reader.pages) < 1
        or reader.is_encrypted
        or title not in (reader.pages[0].extract_text() or "")
    ):
        raise RuntimeError("PDF structure readback failed")
    return {
        "format": "pdf",
        "title": title,
        "pages": len(reader.pages),
        "bytes": output.stat().st_size,
    }


def make_artifact(kind: str, spec_path: str, output_path: str) -> dict[str, Any]:
    extension = {"docx": ".docx", "pptx": ".pptx", "pdf": ".pdf"}[kind]
    output = _workspace_path(output_path, output=True)
    if output.suffix.lower() != extension:
        raise ValueError(f"output must end in {extension}")
    if output.exists():
        raise FileExistsError("output exists; choose a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    spec = _load_spec(spec_path)
    try:
        result = {"docx": _make_docx, "pptx": _make_pptx, "pdf": _make_pdf}[kind](spec, output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {**result, "path": str(output)}


def create_skill(spec_path: str, output_dir: str) -> dict[str, Any]:
    spec = _load_spec(spec_path)
    if not isinstance(spec, dict):
        raise ValueError("Skill spec must be an object")
    name = _text(spec.get("name"), "name", limit=64)
    if not _NAME.fullmatch(name):
        raise ValueError("Skill name must use lowercase letters, digits and hyphens")
    description = _text(spec.get("description"), "description", limit=500)
    body = _text(spec.get("instructions"), "instructions", limit=40_000)
    if "---" in (name, description) or "\n" in description:
        raise ValueError("Skill description must be a single line")
    folder = _workspace_path(output_dir, output=True)
    if folder.name != name or folder.exists():
        raise ValueError("output must be a new directory named after the Skill")
    folder.mkdir(parents=True)
    manifest = folder / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    found = SkillDiscovery((folder.parent,)).discover()
    if not any(candidate.relative_directory == name for candidate in found.candidates):
        manifest.unlink()
        folder.rmdir()
        raise ValueError("created Skill failed Operant discovery validation")
    return {"name": name, "path": str(manifest), "validated": True}


def find_skills(query: str, root: str | None = None) -> dict[str, Any]:
    source = default_skill_root() if root is None else _workspace_path(root)
    found = SkillDiscovery((source,)).discover()
    needle = query.casefold()
    matches = [
        {"name": item.name, "description": item.description}
        for item in found.candidates
        if needle in item.name.casefold() or needle in item.description.casefold()
    ]
    return {"root": str(source), "matches": matches, "issues": len(found.issues)}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("make")
    make.add_argument("kind", choices=("docx", "pptx", "pdf"))
    make.add_argument("spec")
    make.add_argument("output")
    create = sub.add_parser("create-skill")
    create.add_argument("spec")
    create.add_argument("output_dir")
    find = sub.add_parser("find-skills")
    find.add_argument("query")
    find.add_argument("--root")
    args = parser.parse_args()
    if args.action == "make":
        result = make_artifact(args.kind, args.spec, args.output)
    elif args.action == "create-skill":
        result = create_skill(args.spec, args.output_dir)
    else:
        result = find_skills(args.query, args.root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, FileExistsError, ImportError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
