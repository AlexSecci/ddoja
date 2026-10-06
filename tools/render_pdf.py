"""Convert the pipeline's restricted Markdown into readable, searchable PDFs."""
import argparse
from html import escape
import json
from pathlib import Path
import re
import subprocess

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem


def ascii_dashes(text):
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text)


def inline(text):
    # Escape raw HTML. Only our narrow Markdown formatting creates PDF markup.
    value = escape(ascii_dashes(text))
    value = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)", r'<link href="\2" color="#294761">\1</link>', value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return value


def visible(text):
    value = ascii_dashes(text)
    value = re.sub(r"\[([^\]]+)\]\((?:https?://[^\s)]+|mailto:[^\s)]+)\)", r"\1", value)
    value = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+|\d+\.\s+)", "", value)
    return value.replace("**", "").replace("`", "")


def render(source, target, pdftoppm, max_pages, regular=None, bold=None):
    text = source.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("The Markdown document is empty.")
    if any(line.strip().startswith(("|", "```", "![")) for line in text.splitlines()):
        raise ValueError("Use headings, paragraphs and bullets. Tables, code fences and images are unsupported in resumes.")
    font, bold_font = "Helvetica", "Helvetica-Bold"
    if regular and bold and Path(regular).is_file() and Path(bold).is_file():
        font, bold_font = "ApplicantSans", "ApplicantSans-Bold"
        pdfmetrics.registerFont(TTFont(font, regular))
        pdfmetrics.registerFont(TTFont(bold_font, bold))
        pdfmetrics.registerFontFamily(font, normal=font, bold=bold_font, italic=font, boldItalic=bold_font)
        unsupported = {char for char in visible(text) if not char.isspace() and ord(char) not in pdfmetrics.getFont(font).face.charToGlyph}
        if unsupported:
            raise ValueError("The configured font cannot display every character in this document.")
    body = ParagraphStyle("Body", fontName=font, fontSize=10, leading=13.4, spaceAfter=5, alignment=TA_LEFT)
    title = ParagraphStyle("Name", parent=body, fontName=bold_font, fontSize=20, leading=24, spaceAfter=9)
    heading = ParagraphStyle("Section", parent=body, fontName=bold_font, fontSize=11, leading=14,
                             spaceBefore=9, spaceAfter=5, keepWithNext=True)
    subheading = ParagraphStyle("Subsection", parent=heading, fontSize=10, leading=13, spaceBefore=6)
    story, paragraph, bullets = [], [], []
    def flush():
        if paragraph:
            story.append(Paragraph(inline(" ".join(paragraph)), body))
            paragraph.clear()
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(inline(item), body)) for item in bullets],
                                      bulletType="bullet", leftIndent=12, bulletFontName=font,
                                      bulletFontSize=6, spaceAfter=5))
            bullets.clear()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush()
        elif re.match(r"^#{1,6}\s", line):
            flush()
            level = len(line) - len(line.lstrip("#"))
            story.append(Paragraph(inline(line[level:].strip()), title if level == 1 else heading if level == 2 else subheading))
        elif re.match(r"^[-*]\s+", line):
            if paragraph:
                flush()
            bullets.append(re.sub(r"^[-*]\s+", "", line))
        else:
            if bullets:
                flush()
            paragraph.append(line)
    flush()
    target.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(target), pagesize=letter, rightMargin=42, leftMargin=42,
                      topMargin=36, bottomMargin=36, title=source.stem, author="").build(story)
    reader = PdfReader(target)
    if not 1 <= len(reader.pages) <= max_pages:
        raise ValueError(f"Rendered document has {len(reader.pages)} pages; maximum is {max_pages}. Shorten the draft before review.")
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    def normalized(value):
        return "".join(char.casefold() for char in ascii_dashes(value) if char.isalnum())
    if normalized(visible(text)) != normalized(extracted):
        raise ValueError("PDF text differs from the Markdown draft; keep the package out of submission.")
    prefix = target.with_suffix("")
    subprocess.run([pdftoppm, "-r", "110", "-png", str(target), str(prefix)], check=True, capture_output=True, timeout=60)
    images = [str(path) for path in sorted(target.parent.glob(target.stem + "-*.png"))]
    if len(images) != len(reader.pages):
        raise ValueError("Rendered preview count does not match PDF page count.")
    return {"pdf": str(target), "pages": len(reader.pages), "previews": images, "text_matches": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--pdftoppm", required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--font-regular")
    parser.add_argument("--font-bold")
    args = parser.parse_args()
    result = render(args.source, args.target, args.pdftoppm, args.max_pages, args.font_regular, args.font_bold)
    print(json.dumps(result))
