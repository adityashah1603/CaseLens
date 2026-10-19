"""Police-report and statement layout for SYNTHETIC PDFs (fpdf2 core fonts, latin-1 only).

A document is a ``DocSpec``: a title, a field grid, and a list of blocks. ``render_pdf`` turns it
into deterministic PDF bytes; ``locate_phrase`` finds where a key phrase landed (page + bbox) by
matching word tokens in the rendered PDF with PyMuPDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pymupdf
from fpdf import FPDF
from fpdf.enums import MethodReturnValue, XPos, YPos
from fpdf.fonts import FontFace

from caselens_data_gen.common import SYNTHETIC_NOTICE
from caselens_data_gen.documents.pdfio import page_words

HEADER_LINE = "SYNTHETIC DATA - fictional case"
FIXED_CREATION_DATE = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)

_ASCII_MAP = str.maketrans(
    {
        chr(0x2014): "-",
        chr(0x2013): "-",
        chr(0x2018): "'",
        chr(0x2019): "'",
        chr(0x201C): '"',
        chr(0x201D): '"',
        chr(0x2026): "...",
        chr(0x00A0): " ",
    }
)


def ascii_safe(text: str) -> str:
    """Map typographic characters to ASCII and fail loudly on anything else outside latin-1."""
    cleaned = text.translate(_ASCII_MAP)
    cleaned.encode("latin-1")
    return cleaned


@dataclass(frozen=True)
class Heading:
    text: str


@dataclass(frozen=True)
class Para:
    text: str
    style: str = "body"  # body | quote | small | hand


@dataclass(frozen=True)
class Table:
    headers: list[str]
    rows: list[list[str]]
    widths: list[float]


@dataclass(frozen=True)
class Fields:
    pairs: list[tuple[str, str]]


@dataclass(frozen=True)
class PageBreak:
    pass


@dataclass(frozen=True)
class Signature:
    lines: list[tuple[str, str]]  # (label, signed value)


Block = Heading | Para | Table | Fields | PageBreak | Signature


@dataclass
class DocSpec:
    plan_id: str
    form_title: str
    doc_title: str
    case_number: str
    agency: str
    blocks: list[Block]
    facts: list[tuple[str, str]] = field(default_factory=list)  # (fact_id, key phrase)
    handwritten: bool = False


class _ReportPDF(FPDF):
    def __init__(self, spec: DocSpec) -> None:
        super().__init__(orientation="P", unit="pt", format="Letter")
        self.spec = spec
        self.ruled = False
        self.rule_h = 22.0
        self.set_margins(54, 54, 54)
        self.set_auto_page_break(auto=True, margin=60)
        self.set_creation_date(FIXED_CREATION_DATE)
        self.set_title(ascii_safe(f"{spec.doc_title} (SYNTHETIC)"))
        self.set_author(ascii_safe(spec.agency))
        self.set_subject(SYNTHETIC_NOTICE)
        self.set_creator("CaseLens data_gen (SYNTHETIC)")
        self.set_producer("CaseLens data_gen")
        self.alias_nb_pages()

    def header(self) -> None:
        self.set_fill_color(40, 40, 40)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 9)
        self.set_xy(self.l_margin, 24)
        self.cell(self.epw, 14, HEADER_LINE, fill=True, align="C")
        self.set_text_color(0, 0, 0)
        self.set_xy(self.l_margin, 42)
        self.set_font("Helvetica", "B", 10)
        self.cell(self.epw / 2, 12, ascii_safe(self.spec.agency.upper()))
        self.set_font("Helvetica", "", 9)
        self.cell(
            self.epw / 2,
            12,
            ascii_safe(f"Case {self.spec.case_number}  |  Page {self.page_no()} of {{nb}}"),
            align="R",
        )
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.8)
        self.line(self.l_margin, 57, self.w - self.r_margin, 57)
        self.set_y(66)
        if self.ruled:
            self._draw_rules()

    def footer(self) -> None:
        self.set_y(-44)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(90, 90, 90)
        self.multi_cell(self.epw, 8, SYNTHETIC_NOTICE, align="C")
        self.set_text_color(0, 0, 0)

    def _draw_rules(self) -> None:
        self.set_draw_color(150, 170, 200)
        self.set_line_width(0.4)
        y = self.get_y() + self.rule_h
        while y < self.h - 64:
            self.line(self.l_margin, y, self.w - self.r_margin, y)
            y += self.rule_h
        self.set_draw_color(0, 0, 0)


def _title_block(pdf: _ReportPDF, spec: DocSpec) -> None:
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(pdf.epw, 18, ascii_safe(spec.form_title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(pdf.epw, 13, ascii_safe(spec.doc_title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)


def _line_count(pdf: FPDF, width: float, line_h: float, text: str) -> int:
    lines = pdf.multi_cell(
        width, line_h, ascii_safe(text), dry_run=True, output=MethodReturnValue.LINES
    )
    if not isinstance(lines, list):
        raise TypeError("fpdf2 dry run did not return wrapped lines")
    return len(lines)


def _fields(pdf: _ReportPDF, block: Fields) -> None:
    label_w = 130.0
    for label, value in block.pairs:
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_fill_color(235, 235, 235)
        y = pdf.get_y()
        pdf.set_font("Helvetica", "", 9)
        height = 12 * max(1, _line_count(pdf, pdf.epw - label_w, 12, value)) + 2
        if y + height > pdf.page_break_trigger:
            pdf.add_page()
            y = pdf.get_y()
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_xy(pdf.l_margin, y)
        pdf.cell(label_w, height, ascii_safe(label.upper()), border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_xy(pdf.l_margin + label_w, y)
        pdf.multi_cell(pdf.epw - label_w, 12, ascii_safe(value), border=0)
        pdf.rect(pdf.l_margin + label_w, y, pdf.epw - label_w, height)
        pdf.set_xy(pdf.l_margin, y + height)
    pdf.ln(6)


def _heading(pdf: _ReportPDF, block: Heading) -> None:
    if pdf.get_y() + 110 > pdf.page_break_trigger:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_fill_color(210, 210, 210)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(pdf.epw, 15, ascii_safe(block.text.upper()), fill=True, new_x=XPos.LMARGIN)
    pdf.ln(19)


_PARA_STYLES: dict[str, tuple[str, str, float, float, float]] = {
    # style: (font family, emphasis, size, line height, left indent)
    "body": ("Helvetica", "", 10, 13.5, 0),
    "quote": ("Helvetica", "I", 10, 13.5, 18),
    "small": ("Helvetica", "", 8.5, 11, 0),
    "hand": ("Times", "I", 13, 22, 0),
}


def _para(pdf: _ReportPDF, block: Para) -> None:
    """Write one paragraph, kept on a single page so every key phrase has exactly one page."""
    text = ascii_safe(block.text)
    family, emphasis, size, line_h, indent = _PARA_STYLES[block.style]
    pdf.set_font(family, emphasis, size)
    width = pdf.epw - indent
    if pdf.get_y() + line_h * _line_count(pdf, width, line_h, text) > pdf.page_break_trigger:
        pdf.add_page()
        pdf.set_font(family, emphasis, size)
    pdf.set_x(pdf.l_margin + indent)
    pdf.multi_cell(width, line_h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(line_h if block.style == "hand" else 5)


def _table(pdf: _ReportPDF, block: Table) -> None:
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_fill_color(255, 255, 255)
    total = sum(block.widths)
    widths = tuple(w * pdf.epw / total for w in block.widths)
    with pdf.table(
        col_widths=widths,
        line_height=11,
        text_align="LEFT",
        first_row_as_headings=True,
        headings_style=FontFace(emphasis="BOLD", fill_color=(225, 225, 225)),
        padding=2,
    ) as table:
        head = table.row()
        for title in block.headers:
            head.cell(ascii_safe(title))
        for values in block.rows:
            row = table.row()
            for value in values:
                row.cell(ascii_safe(value))
    pdf.ln(8)


def _signature(pdf: _ReportPDF, block: Signature) -> None:
    pdf.ln(10)
    for label, value in block.lines:
        if pdf.get_y() + 40 > pdf.page_break_trigger:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_font("Times", "I", 14)
        pdf.set_xy(pdf.l_margin + 6, y)
        pdf.cell(260, 18, ascii_safe(value))
        pdf.line(pdf.l_margin, y + 20, pdf.l_margin + 280, y + 20)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(pdf.l_margin, y + 22)
        pdf.cell(280, 10, ascii_safe(label), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(10)


def render_pdf(spec: DocSpec) -> bytes:
    """Render a document spec to deterministic PDF bytes."""
    pdf = _ReportPDF(spec)
    pdf.add_page()
    _title_block(pdf, spec)
    for block in spec.blocks:
        if isinstance(block, Heading):
            _heading(pdf, block)
        elif isinstance(block, Para):
            if block.style == "hand" and not pdf.ruled:
                pdf.ruled = True
                pdf._draw_rules()
            _para(pdf, block)
        elif isinstance(block, Table):
            _table(pdf, block)
        elif isinstance(block, Fields):
            _fields(pdf, block)
        elif isinstance(block, Signature):
            pdf.ruled = False
            _signature(pdf, block)
        else:
            pdf.add_page()
    return bytes(pdf.output())


_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens; punctuation and line breaks are ignored."""
    return _TOKEN.findall(text.lower())


def locate_phrase(
    doc: pymupdf.Document, phrase: str
) -> tuple[int, tuple[float, float, float, float]]:
    """Return (1-based page, bbox in PDF points) of the first occurrence of ``phrase``."""
    wanted = tokens(phrase)
    if not wanted:
        raise ValueError("empty key phrase")
    for page_index in range(doc.page_count):
        page = doc[page_index]
        flat: list[tuple[str, int]] = []
        words = page_words(page)
        for word_index, word in enumerate(words):
            for tok in tokens(str(word[4])):
                flat.append((tok, word_index))
        seq = [tok for tok, _ in flat]
        n = len(wanted)
        for start in range(len(seq) - n + 1):
            if seq[start : start + n] == wanted:
                hit = {flat[i][1] for i in range(start, start + n)}
                rects = [words[i] for i in sorted(hit)]
                x0 = min(float(r[0]) for r in rects) - 1
                y0 = min(float(r[1]) for r in rects) - 1
                x1 = max(float(r[2]) for r in rects) + 1
                y1 = max(float(r[3]) for r in rects) + 1
                return page_index + 1, (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
    raise LookupError(f"key phrase not found in rendered PDF: {phrase!r}")
