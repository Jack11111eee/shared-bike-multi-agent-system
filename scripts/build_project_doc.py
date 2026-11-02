"""Render ``docs/project-detail.md`` into a formatted Word document.

The Markdown file is the single source of truth; this script only handles
layout (cover, TOC field, headings, tables, code blocks, figures, hyperlinks).
Run from the repository root:

    python3 scripts/build_project_doc.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "project-detail.md"
TARGET = ROOT / "docs" / "project-detail.docx"

BODY_FONT = "宋体"
HEADING_FONT = "黑体"
LATIN_FONT = "Times New Roman"
MONO_FONT = "Consolas"
ACCENT = RGBColor(0x1C, 0x5D, 0x54)

REPO_URL = "https://github.com/Jack11111eee/shared-bike-multi-agent-system"
DEMO_URL = "http://127.0.0.1:8000/console/"


# -- low level helpers ------------------------------------------------------
def set_run_font(run, *, name: str, size: float | None = None, bold=None,
                 color: RGBColor | None = None, latin: str = LATIN_FONT) -> None:
    run.font.name = latin
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def style_font(style, *, name: str, size: float, bold: bool = False,
               color: RGBColor | None = None) -> None:
    style.font.name = LATIN_FONT
    style.font.size = Pt(size)
    style.font.bold = bold
    if color is not None:
        style.font.color.rgb = color
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)


def shade(paragraph_or_cell, fill: str) -> None:
    element = paragraph_or_cell._element
    pr = element.get_or_add_pPr() if hasattr(element, "get_or_add_pPr") else element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def add_hyperlink(paragraph, text: str, url: str) -> None:
    part = paragraph.part
    rel_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:eastAsia"), BODY_FONT)
    fonts.set(qn("w:ascii"), LATIN_FONT)
    fonts.set(qn("w:hAnsi"), LATIN_FONT)
    rpr.append(fonts)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1C5D54")
    rpr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    run.append(rpr)
    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(text_el)
    link.append(run)
    paragraph._element.append(link)


def add_field(paragraph, instruction: str) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(separate)
    run._r.append(end)


INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|<https?://[^>]+>)")


def join_wrapped(parts: list[str]) -> str:
    """Join source lines that were hard-wrapped; CJK joins without a space."""
    out = ""
    for part in parts:
        piece = part.strip()
        if not piece:
            continue
        if out and not _is_cjk(out[-1]) and not piece.startswith((",", ".", "，", "。", "；", "：", "）")):
            out += " "
        out += piece
    return out


def _is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿" or char in "，。；：、）】」"


def write_inline(paragraph, text: str, *, size: float | None = None) -> None:
    for token in INLINE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            set_run_font(run, name=BODY_FONT, size=size, bold=True)
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, name=MONO_FONT, size=(size or 10.5) - 1.0, latin=MONO_FONT)
            run.font.color.rgb = RGBColor(0x8A, 0x2B, 0x1E)
        elif token.startswith("<http") and token.endswith(">"):
            url = token[1:-1]
            add_hyperlink(paragraph, url, url)
        elif token.startswith("[") and "](" in token:
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token).groups()
            add_hyperlink(paragraph, label, url)
        else:
            run = paragraph.add_run(token)
            set_run_font(run, name=BODY_FONT, size=size)


# -- document scaffolding ---------------------------------------------------
def configure_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    style_font(normal, name=BODY_FONT, size=12)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(6)

    specs = {
        "Heading 1": (16, True, 18, 10),
        "Heading 2": (14, True, 12, 6),
        "Heading 3": (12, True, 8, 4),
    }
    for name, (size, bold, before, after) in specs.items():
        style = doc.styles[name]
        style_font(style, name=HEADING_FONT, size=size, bold=bold, color=ACCENT)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    for name in ("List Bullet", "List Number"):
        style = doc.styles[name]
        style_font(style, name=BODY_FONT, size=12)
        style.paragraph_format.line_spacing = 1.4


def page_setup(doc: Document) -> None:
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.8)
    section.right_margin = Cm(2.6)

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("城市共享单车供需失衡预测与多智能体调度工作台")
    set_run_font(run, name=BODY_FONT, size=9, color=RGBColor(0x77, 0x77, 0x77))

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run()
    set_run_font(run, name=BODY_FONT, size=9, color=RGBColor(0x77, 0x77, 0x77))
    add_field(footer, "PAGE")


def cover(doc: Document) -> None:
    for _ in range(5):
        doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("城市共享单车供需失衡预测\n与多智能体调度工作台")
    set_run_font(run, name=HEADING_FONT, size=26, bold=True, color=ACCENT)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("项目详细方案")
    set_run_font(run, name=HEADING_FONT, size=16, color=RGBColor(0x33, 0x33, 0x33))

    doc.add_paragraph()
    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = line.add_run("双源订单接入 · 需求预测 · 失衡风险排序 · 调度仿真 · 多智能体决策 · 离线回放")
    set_run_font(run, name=BODY_FONT, size=11, color=RGBColor(0x55, 0x55, 0x55))

    for _ in range(6):
        doc.add_paragraph()

    info = doc.add_table(rows=4, cols=2)
    info.alignment = WD_TABLE_ALIGNMENT.CENTER
    info.autofit = False
    rows = [
        ("代码仓库", REPO_URL),
        ("在线演示", "本机启动后访问 " + DEMO_URL),
        ("启动方式", "./start-demo.command 或 bike-agent serve --port 8000"),
        ("版本 / 日期", "Phase 1–7（F1–F29）· 2026 年 11 月"),
    ]
    for row, (label, value) in zip(info.rows, rows):
        for cell, width in zip(row.cells, (Cm(3.4), Cm(11.4))):
            cell.width = width
        p = row.cells[0].paragraphs[0]
        p.paragraph_format.space_after = Pt(2)
        run = p.add_run(label)
        set_run_font(run, name=HEADING_FONT, size=10.5, bold=True)
        p2 = row.cells[1].paragraphs[0]
        p2.paragraph_format.space_after = Pt(2)
        if value.startswith("http"):
            add_hyperlink(p2, value, value)
        else:
            run = p2.add_run(value)
            set_run_font(run, name=BODY_FONT, size=10.5)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def outline(text: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,4})\s+(.*)$", line)
        if not match:
            continue
        level = len(match.group(1))
        if level in (2, 3):
            entries.append((level - 1, match.group(2).strip()))
    return entries


def add_toc_field(doc: Document, entries: list[tuple[int, str]]) -> None:
    """Insert a real TOC field with a cached outline, so it reads before update."""
    paragraphs: list = []
    for level, text in entries:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.left_indent = Cm(0.6 * (level - 1))
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.2
        run = paragraph.add_run(text)
        set_run_font(run, name=HEADING_FONT if level == 1 else BODY_FONT,
                     size=11.5 if level == 1 else 10.5,
                     bold=level == 1)
        paragraphs.append(paragraph)
        if level > 2:
            paragraphs[-1].paragraph_format.left_indent = Cm(1.2)

    first, last = paragraphs[0], paragraphs[-1]
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    run = OxmlElement("w:r")
    run.append(begin)
    run.append(instr)
    run.append(separate)
    first._p.insert(0, run)

    end_run = OxmlElement("w:r")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run.append(end)
    last._p.append(end_run)


def toc(doc: Document, entries: list[tuple[int, str]]) -> None:
    heading = doc.add_paragraph()
    run = heading.add_run("目  录")
    set_run_font(run, name=HEADING_FONT, size=18, bold=True, color=ACCENT)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    note = doc.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    note.paragraph_format.space_after = Pt(14)
    run = note.add_run("（在 Word 中全选后按 F9，或右键「更新域」，可生成页码）")
    set_run_font(run, name=BODY_FONT, size=9, color=RGBColor(0x88, 0x88, 0x88))

    add_toc_field(doc, entries)


# -- markdown rendering -----------------------------------------------------
def add_table(doc: Document, rows: list[list[str]]) -> None:
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, values in enumerate(rows):
        row = table.rows[index]
        if index == 0:
            tr_pr = row._tr.get_or_add_trPr()
            header = OxmlElement("w:tblHeader")
            header.set(qn("w:val"), "true")
            tr_pr.append(header)
        for cell, value in zip(row.cells, values):
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.line_spacing = 1.15
            write_inline(paragraph, value, size=9.5)
            if index == 0:
                for run in paragraph.runs:
                    run.bold = True
                shade(cell, "EAF1EF")
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_code(doc: Document, lines: list[str]) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.6)
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.0
    shade(paragraph, "F3F5F4")
    for index, line in enumerate(lines):
        run = paragraph.add_run(line)
        set_run_font(run, name=MONO_FONT, size=9, latin=MONO_FONT)
        if index != len(lines) - 1:
            run.add_break()


def add_image(doc: Document, caption: str, target: Path) -> None:
    path = (SOURCE.parent / target).resolve()
    if not path.exists():
        print(f"warning: missing image {path}", file=sys.stderr)
        return
    with Image.open(path) as image:
        width, height = image.size
    max_width = 15.0
    max_height = 10.5
    scale = min(max_width / (width / 96 * 2.54), max_height / (height / 96 * 2.54), 1.0)
    width_cm = width / 96 * 2.54 * scale

    picture = doc.add_paragraph()
    picture.alignment = WD_ALIGN_PARAGRAPH.CENTER
    picture.paragraph_format.space_before = Pt(6)
    picture.add_run().add_picture(str(path), width=Cm(width_cm))

    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    line.paragraph_format.space_after = Pt(10)
    run = line.add_run(caption)
    set_run_font(run, name=BODY_FONT, size=9.5, color=RGBColor(0x55, 0x55, 0x55))


def render_markdown(doc: Document, text: str) -> None:
    lines = text.splitlines()
    index = 0
    first_heading_skipped = False
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.first_line_indent = Cm(0.74)
        write_inline(paragraph, join_wrapped(buffer))
        buffer.clear()

    def absorb() -> str:
        """Collect indented continuation lines that belong to the current item."""
        nonlocal index
        parts = []
        while index < len(lines):
            nxt = lines[index]
            if not nxt.strip():
                break
            if not re.match(r"^\s{2,}\S", nxt):
                break
            if re.match(r"^\s*([-*]|\d+\.)\s+", nxt):
                break
            parts.append(nxt)
            index += 1
        return join_wrapped(parts)

    while index < len(lines):
        line = lines[index]

        if line.startswith("```"):
            flush()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            add_code(doc, block)
            continue

        if line.startswith("|") and index + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[index + 1].strip()):
            flush()
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", "|".join(cells)):
                    rows.append(cells)
                index += 1
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            add_table(doc, rows)
            continue

        image = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if image:
            flush()
            add_image(doc, image.group(1), Path(image.group(2)))
            index += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1 and not first_heading_skipped:
                first_heading_skipped = True
                index += 1
                continue
            doc_heading = doc.add_heading(title, level=min(level - 1, 3) if level > 1 else 1)
            if level == 2:
                doc_heading.paragraph_format.page_break_before = True
            index += 1
            continue

        if line.startswith(">"):
            flush()
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.6)
            write_inline(paragraph, line.lstrip("> ").strip(), size=10.5)
            for run in paragraph.runs:
                run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            index += 1
            continue

        bullet = re.match(r"^(\s*)-\s+(.*)$", line)
        if bullet:
            flush()
            indent = len(bullet.group(1))
            style = "List Bullet" if indent < 2 else "List Bullet 2"
            index += 1
            text_value = join_wrapped([bullet.group(2), absorb()])
            paragraph = doc.add_paragraph(style=style)
            paragraph.paragraph_format.space_after = Pt(2)
            write_inline(paragraph, text_value)
            continue

        numbered = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if numbered:
            flush()
            index += 1
            text_value = join_wrapped([numbered.group(3), absorb()])
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.9)
            paragraph.paragraph_format.first_line_indent = Cm(-0.55)
            paragraph.paragraph_format.space_after = Pt(2)
            write_inline(paragraph, f"{numbered.group(2)}. {text_value}")
            continue

        if line.strip() in ("---", "***"):
            flush()
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(2)
            paragraph.paragraph_format.space_after = Pt(2)
            run = paragraph.add_run("—" * 30)
            set_run_font(run, name=BODY_FONT, size=9, color=RGBColor(0xBB, 0xBB, 0xBB))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            index += 1
            continue

        if not line.strip():
            flush()
            index += 1
            continue

        buffer.append(line)
        index += 1

    flush()


def main() -> int:
    text = SOURCE.read_text(encoding="utf-8")
    body = text[text.index("\n## "):]
    doc = Document()
    configure_styles(doc)
    page_setup(doc)
    cover(doc)
    toc(doc, outline(body))
    render_markdown(doc, body)
    doc.save(TARGET)
    print(f"wrote {TARGET.relative_to(ROOT)} ({TARGET.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
