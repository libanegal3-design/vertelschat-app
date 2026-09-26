"""Book generation with ReportLab.

Output per version:
- interior.pdf  print interior, 170 x 240 mm trim + 3 mm bleed, TrimBox/BleedBox set, fonts embedded,
                page count padded to a multiple of 4 with note pages;
- cover.pdf     hardcover case-wrap spread (back, spine, front) with configurable spine and wrap; the
                exact dimensions must be replaced by the printer's template before production;
- screen.pdf    the same interior cropped to trim, with clickable QR links (digital edition);
- page previews (PNG) rendered with pdfium for the book builder.
Every story with audio gets a QR code plus a printed audio code (A01, A02 ...) that matches the file names
in the family's own archive, so the book keeps working even without Vertelschat."""
from __future__ import annotations

import io
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Flowable, Frame, Image as RLImage, Paragraph, Spacer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio import spoken_duration
from .config import PACKAGE_DIR
from .db import utcnow
from .jobs import job
from .models import Book, BookVersion, Chapter, MediaAsset, Photo, Project, Prompt, QRLink, Recording, Story
from .qr import qr_display_url, qr_matrix, qr_url
from .storage import get_storage, store_path

log = logging.getLogger("vertelschat.book")

TRIM_W, TRIM_H = 170 * mm, 240 * mm
BLEED = 3 * mm
PAGE_W, PAGE_H = TRIM_W + 2 * BLEED, TRIM_H + 2 * BLEED
M_INNER, M_OUTER, M_TOP, M_BOTTOM = 21 * mm, 17 * mm, 21 * mm, 24 * mm
MAX_PAGES = 240
NIGHT = HexColor("#17324D")
LAMP = HexColor("#F2B544")
INK = HexColor("#1A2230")
GREY = HexColor("#5B6574")
RULE = HexColor("#C9D2DA")
TWILIGHT = HexColor("#E9EEF2")
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
          "november", "december"]
_fonts_ready = False


def register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    d = PACKAGE_DIR / "book_fonts"
    pdfmetrics.registerFont(TTFont("Castoro", str(d / "Castoro-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Castoro-SemiBold", str(d / "Castoro-SemiBold.ttf")))
    pdfmetrics.registerFont(TTFont("Castoro-Italic", str(d / "Castoro-Italic.ttf")))
    pdfmetrics.registerFont(TTFont("Atkinson", str(d / "AtkinsonHyperlegible-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Atkinson-Bold", str(d / "AtkinsonHyperlegible-Bold.ttf")))
    pdfmetrics.registerFontFamily("Castoro", normal="Castoro", bold="Castoro-SemiBold", italic="Castoro-Italic",
                                  boldItalic="Castoro-SemiBold")
    _fonts_ready = True


def _styles() -> dict[str, ParagraphStyle]:
    register_fonts()
    body = ParagraphStyle("body", fontName="Castoro", fontSize=10.8, leading=15.6, textColor=INK, alignment=TA_LEFT)
    return {
        "body_first": body,
        "body": ParagraphStyle("body_indent", parent=body, firstLineIndent=5 * mm),
        "title": ParagraphStyle("title", fontName="Castoro-SemiBold", fontSize=19, leading=23, textColor=NIGHT,
                                spaceAfter=3 * mm),
        "question": ParagraphStyle("question", fontName="Castoro-Italic", fontSize=10.5, leading=14.5, textColor=GREY,
                                   spaceAfter=6 * mm),
        "caption": ParagraphStyle("caption", fontName="Atkinson", fontSize=7.8, leading=10.5, textColor=GREY,
                                  spaceBefore=1.5 * mm, spaceAfter=5 * mm),
        "small": ParagraphStyle("small", fontName="Atkinson", fontSize=8.4, leading=12, textColor=GREY),
        "toc_ch": ParagraphStyle("toc_ch", fontName="Castoro-SemiBold", fontSize=11, leading=15, textColor=NIGHT),
        "note": ParagraphStyle("note", fontName="Castoro", fontSize=10.5, leading=15.5, textColor=INK, spaceAfter=3 * mm),
        "note_head": ParagraphStyle("note_head", fontName="Castoro-SemiBold", fontSize=12.5, leading=17,
                                    textColor=NIGHT, spaceBefore=2 * mm, spaceAfter=2 * mm),
    }


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- content model
@dataclass
class BookPhoto:
    path: Path
    caption: str
    width: int
    height: int


@dataclass
class BookStory:
    id: str
    title: str
    question: str
    body: str
    audio_code: str = ""
    qr_token: str = ""
    duration: float = 0.0
    photos: list[BookPhoto] = field(default_factory=list)
    date_label: str = ""


@dataclass
class BookChapter:
    title: str
    stories: list[BookStory]


@dataclass
class BookSpec:
    title: str
    subtitle: str
    storyteller: str
    dedication: str
    chapters: list[BookChapter]
    cover_style: str = "nacht"
    cover_photo: Path | None = None
    year: int = field(default_factory=lambda: utcnow().year)
    warnings: list[str] = field(default_factory=list)

    @property
    def stories(self) -> list[BookStory]:
        return [s for ch in self.chapters for s in ch.stories]


# --------------------------------------------------------------------------- flowables
class QRBlock(Flowable):
    def __init__(self, story: BookStory, name: str, clickable: bool) -> None:
        super().__init__()
        self.story, self.name, self.clickable = story, name, clickable
        self.size = 21 * mm

    def wrap(self, aw, ah):
        self.aw = aw
        return aw, 29 * mm

    def draw(self):
        c = self.canv
        url = qr_url(self.story.qr_token)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.4)
        c.line(0, 27.5 * mm, 40 * mm, 27.5 * mm)
        matrix = qr_matrix(url)
        n = len(matrix)
        cell = self.size / n
        c.setFillColor(NIGHT)
        base_y = 1.5 * mm
        for y, row in enumerate(matrix):
            x = 0
            while x < n:
                if row[x]:
                    start = x
                    while x < n and row[x]:
                        x += 1
                    c.rect(start * cell, base_y + self.size - (y + 1) * cell, (x - start) * cell, cell, stroke=0, fill=1)
                else:
                    x += 1
        tx = self.size + 5 * mm
        c.setFont("Castoro-Italic", 11.5)
        c.drawString(tx, 16.5 * mm, f"Luister naar {self.name}")
        c.setFont("Atkinson-Bold", 8.5)
        c.setFillColor(GREY)
        c.drawString(tx, 11.8 * mm, f"Audiocode {self.story.audio_code}")
        c.setFont("Atkinson", 8.5)
        c.drawString(tx + c.stringWidth(f"Audiocode {self.story.audio_code}", "Atkinson-Bold", 8.5) + 3 * mm,
                     11.8 * mm, spoken_duration(self.story.duration))
        c.drawString(tx, 7.6 * mm, qr_display_url(self.story.qr_token))
        if self.clickable:
            c.linkURL(url, (0, base_y, self.size, base_y + self.size), relative=1)


class Keep:
    """Marker: keep these flowables together on one page (moved to the next page if they do not fit)."""

    def __init__(self, items: list) -> None:
        self.items = list(items)


class Rule(Flowable):
    def __init__(self, width: float = 24 * mm, space: float = 5 * mm) -> None:
        super().__init__()
        self.w, self.space = width, space

    def wrap(self, aw, ah):
        return aw, self.space

    def draw(self):
        self.canv.setStrokeColor(LAMP)
        self.canv.setLineWidth(1.2)
        self.canv.line(0, self.space / 2, self.w, self.space / 2)


# --------------------------------------------------------------------------- layout engine
class Layout:
    """Manual page control: facing pages (inner/outer margins), chapter openers on recto, blank versos without folio."""

    def __init__(self, c, spec: BookSpec, clickable: bool) -> None:
        self.c, self.spec, self.clickable = c, spec, clickable
        self.page = 0
        self.frame: Frame | None = None
        self.folio = False
        self.running = ""
        self.story_pages: dict[str, int] = {}
        self.chapter_pages: dict[str, int] = {}

    @staticmethod
    def is_recto(page: int) -> bool:
        return page % 2 == 1

    def _boxes(self) -> None:
        c = self.c
        trim = (BLEED, BLEED, BLEED + TRIM_W, BLEED + TRIM_H)
        if hasattr(c, "setTrimBox"):
            c.setTrimBox(trim)
        if hasattr(c, "setBleedBox"):
            c.setBleedBox((0, 0, PAGE_W, PAGE_H))
        if self.clickable and hasattr(c, "setCropBox"):
            c.setCropBox(trim)

    def new_page(self, *, folio: bool = True, running: str = "") -> None:
        if self.page > 0:
            self._finish()
            self.c.showPage()
        self.page += 1
        self._boxes()
        self.folio, self.running = folio, running
        left = BLEED + (M_INNER if self.is_recto(self.page) else M_OUTER)
        self.frame = Frame(left, BLEED + M_BOTTOM, TRIM_W - M_INNER - M_OUTER, TRIM_H - M_TOP - M_BOTTOM,
                           leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    def to_recto(self, **kw) -> None:
        self.new_page(**kw)
        if not self.is_recto(self.page):
            self.new_page(folio=False)  # this verso stays blank
            self.folio, self.running = kw.get("folio", True), kw.get("running", "")

    def _finish(self) -> None:
        c = self.c
        if self.folio:
            c.setFont("Atkinson", 8)
            c.setFillColor(GREY)
            y = BLEED + 12 * mm
            if self.is_recto(self.page):
                c.drawRightString(BLEED + TRIM_W - M_OUTER, y, str(self.page))
            else:
                c.drawString(BLEED + M_OUTER, y, str(self.page))
        if self.running:
            c.setFont("Castoro-Italic", 8.5)
            c.setFillColor(GREY)
            y = BLEED + TRIM_H - 12 * mm
            if self.is_recto(self.page):
                c.drawRightString(BLEED + TRIM_W - M_OUTER, y, self.running)
            else:
                c.drawString(BLEED + M_OUTER, y, self.running)

    def _avail(self) -> float:
        return self.frame._y - self.frame._y1p

    def flow(self, items: list) -> None:
        """Place flowables in the current frame, splitting long paragraphs and starting new pages as needed.
        Keep(...) groups (title with first paragraph, photo with caption, QR block) are never split."""
        items = list(items)
        while items:
            f = items.pop(0)
            if isinstance(f, Keep):
                height = 0.0
                for x in f.items:
                    height += x.wrap(self.frame._aW, PAGE_H)[1] + x.getSpaceBefore() + x.getSpaceAfter()
                if height > self._avail() and not self.frame._atTop:
                    self.new_page(running=self.running)
                items[0:0] = f.items
                continue
            if self.frame.add(f, self.c, trySplit=0):
                continue
            parts = f.split(self.frame._aW, self._avail()) if hasattr(f, "split") else []
            if len(parts) > 1 and self.frame.add(parts[0], self.c, trySplit=0):
                items[0:0] = parts[1:]
                continue
            if self.frame._atTop:
                log.warning("element past niet op een lege pagina en is overgeslagen: %s", type(f).__name__)
                continue
            self.new_page(running=self.running)
            items.insert(0, f)

    def text_at(self, text: str, font: str, size: float, y_from_top: float, color=NIGHT, center: bool = False) -> None:
        c = self.c
        c.setFont(font, size)
        c.setFillColor(color)
        y = BLEED + TRIM_H - y_from_top
        if center:
            c.drawCentredString(BLEED + TRIM_W / 2, y, text)
        else:
            x = BLEED + (M_INNER if self.is_recto(self.page) else M_OUTER)
            c.drawString(x, y, text)

    def finish(self) -> None:
        self._finish()
        self.c.showPage()
        self.c.save()


def _toc_rows(spec: BookSpec) -> int:
    return len(spec.chapters) + len(spec.stories)


TOC_ROWS_PER_PAGE = 30


def _render(spec: BookSpec, out, *, clickable: bool, pages: dict[str, int] | None) -> Layout:
    st = _styles()
    c = rl_canvas.Canvas(out, pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    c.setTitle(spec.title)
    c.setAuthor(spec.storyteller)
    c.setSubject("Gemaakt met Vertelschat")
    c.setCreator("Vertelschat book engine")
    L = Layout(c, spec, clickable)
    first = spec.storyteller.split(" ")[0]

    # half title, blank verso, title page, imprint
    L.new_page(folio=False)
    L.text_at(spec.title, "Castoro", 20, 78 * mm, center=True)
    L.new_page(folio=False)
    L.new_page(folio=False)
    L.text_at(spec.title, "Castoro-SemiBold", 28, 70 * mm)
    if spec.subtitle:
        L.text_at(spec.subtitle, "Castoro-Italic", 14, 82 * mm, color=GREY)
    L.text_at(f"Verteld door {spec.storyteller}", "Castoro", 12, 100 * mm, color=INK)
    L.text_at(str(spec.year), "Atkinson", 9, TRIM_H - 34 * mm, color=GREY)
    L.new_page(folio=False)
    imprint = [Spacer(1, 120 * mm),
               Paragraph(f"\u00a9 {spec.year} de familie van {_esc(spec.storyteller)}. Alle rechten voorbehouden.", st["small"]),
               Spacer(1, 2 * mm),
               Paragraph("De verhalen in dit boek zijn verteld in spraakberichten en daarna uitgeschreven en licht "
                         "geredigeerd. Er is niets aan toegevoegd. De originele opnames horen bij dit boek: scan de "
                         "QR-code bij een verhaal, of zoek de audiocode op in het eigen archief van de familie.",
                         st["small"]),
               Spacer(1, 2 * mm), Paragraph("Gemaakt met Vertelschat.", st["small"])]
    L.flow(imprint)
    if spec.dedication.strip():
        L.to_recto(folio=False)
        L.flow([Spacer(1, 60 * mm), Paragraph(_esc(spec.dedication).replace("\n", "<br/>"),
                                              ParagraphStyle("ded", parent=st["question"], alignment=TA_CENTER,
                                                             fontSize=12, leading=18))])

    # table of contents (fixed number of pages so page numbers are stable between passes)
    toc_pages = max(1, -(-_toc_rows(spec) // TOC_ROWS_PER_PAGE))
    L.to_recto(folio=False)
    L.text_at("Inhoud", "Castoro-SemiBold", 22, 32 * mm)
    rows_done = 0
    y0 = 48 * mm
    for ch in spec.chapters:
        for kind, label, target in [("ch", ch.title, "ch:" + ch.title)] + [("st", s.title, s.id) for s in ch.stories]:
            if rows_done and rows_done % TOC_ROWS_PER_PAGE == 0:
                L.new_page(folio=False)
                y0 = 32 * mm
            y = y0 + (rows_done % TOC_ROWS_PER_PAGE) * 5.6 * mm
            page_no = (pages or {}).get(target, 0)
            x_left = BLEED + (M_INNER if L.is_recto(L.page) else M_OUTER)
            x_right = BLEED + TRIM_W - (M_OUTER if L.is_recto(L.page) else M_INNER)
            c.setFillColor(NIGHT if kind == "ch" else INK)
            font = "Castoro-SemiBold" if kind == "ch" else "Castoro"
            c.setFont(font, 10.5 if kind == "ch" else 10)
            text = label if kind == "ch" else "    " + label
            max_w = x_right - x_left - 14 * mm
            while c.stringWidth(text, font, 10) > max_w and len(text) > 8:
                text = text[:-2]
            if text != (label if kind == "ch" else "    " + label):
                text = text.rstrip() + "\u2026"
            c.drawString(x_left, BLEED + TRIM_H - y, text)
            if page_no:
                c.setFont("Atkinson", 9)
                c.setFillColor(GREY)
                c.drawRightString(x_right, BLEED + TRIM_H - y, str(page_no))
            rows_done += 1
    while L.page < _first_toc_page(spec) + toc_pages - 1:
        L.new_page(folio=False)

    # how to listen
    L.to_recto(folio=False)
    listen = [Spacer(1, 22 * mm), Paragraph("Luisteren naar dit boek", st["title"]), Rule(),
              Paragraph(f"Bij bijna elk verhaal staat een QR-code. Scan die met de camera van je telefoon en je hoort "
                        f"{_esc(first)} het verhaal zelf vertellen, precies zoals het is ingesproken.", st["note"]),
              Paragraph("Naast elke QR-code staat een audiocode, bijvoorbeeld A07. Het archief van de familie bevat "
                        "alle opnames als gewone geluidsbestanden, met die code aan het begin van de bestandsnaam. Zo "
                        "vind je elk verhaal terug, ook zonder internet en ook zonder Vertelschat.", st["note"]),
              Paragraph("Achter in het boek staat een luisterregister met alle audiocodes op een rij.", st["note"])]
    L.flow(listen)

    # chapters and stories
    for ci, ch in enumerate(spec.chapters, start=1):
        L.to_recto(folio=False)
        L.chapter_pages["ch:" + ch.title] = L.page
        L.text_at(f"Hoofdstuk {ci}", "Castoro-Italic", 11, 62 * mm, color=GREY)
        L.text_at(ch.title, "Castoro", 30, 78 * mm)
        c.setStrokeColor(LAMP)
        c.setLineWidth(1.4)
        x = BLEED + M_INNER
        c.line(x, BLEED + TRIM_H - 88 * mm, x + 24 * mm, BLEED + TRIM_H - 88 * mm)
        for si, story in enumerate(ch.stories):
            L.new_page(running=ch.title)
            L.story_pages[story.id] = L.page
            head = [Paragraph(_esc(story.title), st["title"])]
            if story.question:
                head.append(Paragraph(_esc(story.question), st["question"]))
            paras = [p.strip() for p in story.body.split("\n\n") if p.strip()]
            body = []
            for pi, p in enumerate(paras):
                body.append(Paragraph(_esc(p).replace("\n", "<br/>"), st["body_first"] if pi == 0 else st["body"]))
            items: list = [Keep(head + body[:1])] + body[1:]
            if story.qr_token:  # the voice belongs right under the words; photos follow
                items.append(Keep([Spacer(1, 6 * mm), QRBlock(story, first, clickable), Spacer(1, 2 * mm)]))
            for ph in story.photos:
                max_w = TRIM_W - M_INNER - M_OUTER
                max_h = 92 * mm
                ratio = min(max_w / ph.width, max_h / ph.height)
                img = RLImage(str(ph.path), width=ph.width * ratio, height=ph.height * ratio)
                img.hAlign = "LEFT"
                block = [Spacer(1, 4 * mm), img]
                if ph.caption:
                    block.append(Paragraph(_esc(ph.caption), st["caption"]))
                items.append(Keep(block))
            L.flow(items)

    # back matter: listening index and about
    L.to_recto(folio=True, running="Luisterregister")
    index = [Paragraph("Luisterregister", st["title"]), Rule(),
             Paragraph("Alle verhalen met een opname, op audiocode. In het archief begint elke bestandsnaam met deze "
                       "code.", st["note"]), Spacer(1, 3 * mm)]
    for s in spec.stories:
        if s.qr_token:
            page_no = (pages or {}).get(s.id, 0)
            index.append(Paragraph(f"<font name='Atkinson-Bold'>{s.audio_code}</font>&nbsp;&nbsp;{_esc(s.title)}"
                                   f"<font color='#5B6574'>&nbsp;&nbsp;{('p. ' + str(page_no)) if page_no else ''}"
                                   f"&nbsp;&nbsp;{spoken_duration(s.duration)}</font>", st["note"]))
    L.flow(index)
    total_minutes = int(sum(s.duration for s in spec.stories) // 60)
    L.new_page(folio=True, running="Over dit boek")
    about = [Paragraph("Over dit boek", st["title"]), Rule(),
             Paragraph(f"Dit boek bevat {len(spec.stories)} verhalen van {_esc(spec.storyteller)}, samen ongeveer "
                       f"{total_minutes} minuten in eigen stem. De verhalen zijn verteld via WhatsApp-spraakberichten, "
                       "uitgeschreven en voorzichtig geredigeerd. De familie heeft ze nagelezen.", st["note"]),
             Paragraph("De QR-codes verwijzen naar v.vertelschat.nl, een adres dat speciaal voor deze codes bestaat en "
                       "los staat van onze winkel en onze opslag. Het blijft werken, ook na afloop van het verteljaar. "
                       "Mocht het ooit toch niet meer bereikbaar zijn, dan vind je elke opname terug in het archief "
                       "van de familie via de audiocode.", st["note"]),
             Paragraph(f"Gezet in Castoro en Atkinson Hyperlegible. {spec.year}.", st["small"])]
    L.flow(about)
    # pad to a multiple of 4 with note pages
    while L.page % 4 != 0:
        L.new_page(folio=False)
        if L.page % 4 != 0:
            _note_lines(L)
    L.finish()
    return L


def _first_toc_page(spec: BookSpec) -> int:
    page = 4
    if spec.dedication.strip():
        page = 5
    page += 1
    return page if page % 2 == 1 else page + 1


def _note_lines(L: Layout) -> None:
    c = L.c
    c.setFont("Castoro-Italic", 10)
    c.setFillColor(GREY)
    x0 = BLEED + (M_INNER if L.is_recto(L.page) else M_OUTER)
    x1 = BLEED + TRIM_W - (M_OUTER if L.is_recto(L.page) else M_INNER)
    c.drawString(x0, BLEED + TRIM_H - M_TOP - 4 * mm, "Aantekeningen")
    c.setStrokeColor(RULE)
    c.setLineWidth(0.35)
    y = BLEED + TRIM_H - M_TOP - 16 * mm
    while y > BLEED + M_BOTTOM:
        c.line(x0, y, x1, y)
        y -= 9 * mm


def render_interior(spec: BookSpec, path: Path, clickable: bool = False) -> int:
    first = _render(spec, io.BytesIO(), clickable=clickable, pages=None)
    pages = {**first.story_pages, **first.chapter_pages}
    final = _render(spec, str(path), clickable=clickable, pages=pages)
    return final.page


# --------------------------------------------------------------------------- cover
@dataclass
class CoverSpec:
    spine_mm: float
    board_overhang_mm: float = 3.0
    hinge_mm: float = 8.0
    wrap_mm: float = 15.0


def spine_width_mm(pages: int, caliper_mm: float = 0.1) -> float:
    return round(max(6.0, pages * caliper_mm + 2.0), 1)


def render_cover(spec: BookSpec, pages: int, path: Path) -> dict:
    register_fonts()
    cs = CoverSpec(spine_mm=spine_width_mm(pages))
    board_w = TRIM_W + cs.board_overhang_mm * mm
    board_h = TRIM_H + 2 * cs.board_overhang_mm * mm
    spine = cs.spine_mm * mm
    hinge = cs.hinge_mm * mm
    wrap = cs.wrap_mm * mm
    W = 2 * wrap + 2 * board_w + 2 * hinge + spine
    H = 2 * wrap + board_h
    c = rl_canvas.Canvas(str(path), pagesize=(W, H))
    dark = spec.cover_style != "schemer"
    bg, fg, accent = (NIGHT, white, LAMP) if dark else (TWILIGHT, NIGHT, HexColor("#B7791F"))
    c.setFillColor(bg)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    front_x = wrap + board_w + 2 * hinge + spine
    back_x = wrap
    spine_x = wrap + board_w + hinge
    # lamplight glow on the front
    if dark and hasattr(c, "radialGradient"):
        c.saveState()
        p = c.beginPath()
        p.rect(front_x - hinge / 2, 0, W - front_x + hinge / 2, H)  # front panel including its wrap: no hard edges
        c.clipPath(p, stroke=0, fill=0)
        c.radialGradient(front_x + board_w * 0.74, wrap + board_h * 0.78, 78 * mm,
                         (HexColor("#F2B544"), HexColor("#6E6A4F"), HexColor("#17324D")), (0, 0.32, 1), extend=True)
        c.restoreState()
    if spec.cover_style == "foto" and spec.cover_photo:
        img = ImageReader(str(spec.cover_photo))
        iw, ih = img.getSize()
        box_w = board_w - 36 * mm
        box_h = box_w * ih / iw
        c.drawImage(img, front_x + 18 * mm, wrap + board_h - 26 * mm - box_h, box_w, box_h)
    c.setFillColor(fg)
    title_y = wrap + board_h * 0.36
    c.setFont("Castoro-SemiBold", 30)
    c.drawString(front_x + 18 * mm, title_y, spec.title)
    c.setFont("Castoro-Italic", 14)
    c.setFillColor(accent)
    c.drawString(front_x + 18 * mm, title_y - 11 * mm, spec.subtitle or f"Verteld door {spec.storyteller}")
    c.setFillColor(fg)
    c.setFont("Castoro", 11)
    c.drawString(front_x + 18 * mm, wrap + 20 * mm, "vertelschat")
    # spine
    c.saveState()
    c.translate(spine_x + spine / 2 + 1.5 * mm, wrap + board_h / 2)
    c.rotate(-90)
    c.setFont("Castoro-SemiBold", 11 if spine > 12 * mm else 9)
    c.setFillColor(fg)
    c.drawCentredString(0, 0, f"{spec.title}")
    c.restoreState()
    # back
    c.setFont("Castoro-Italic", 12)
    c.setFillColor(fg)
    minutes = int(sum(s.duration for s in spec.stories) // 60)
    lines = [f"{len(spec.stories)} verhalen van {spec.storyteller},",
             f"verteld in {minutes} minuten eigen stem.",
             "Scan de QR-codes in dit boek om te luisteren."]
    y = wrap + board_h * 0.62
    for line in lines:
        c.drawString(back_x + 20 * mm, y, line)
        y -= 7 * mm
    # guides for the printer (outside the trim, in the wrap area)
    c.setStrokeColor(accent)
    c.setLineWidth(0.25)
    for gx in (wrap, spine_x - hinge, spine_x, spine_x + spine, spine_x + spine + hinge, W - wrap):
        c.line(gx, 0, gx, 6 * mm)
        c.line(gx, H - 6 * mm, gx, H)
    c.setFont("Atkinson", 6)
    c.drawString(wrap, 2 * mm, f"Voorlopige coverspecificatie: rug {cs.spine_mm} mm, omslag {cs.wrap_mm} mm, scharnier "
                               f"{cs.hinge_mm} mm. Vervang door het sjabloon van de drukker.")
    c.showPage()
    c.save()
    return {"spine_mm": cs.spine_mm, "width_mm": round(W / mm, 1), "height_mm": round(H / mm, 1)}


# --------------------------------------------------------------------------- data collection
def month_label(dt: datetime | None) -> str:
    return f"{MONTHS[dt.month - 1]} {dt.year}" if dt else ""


def collect(session: Session, project: Project, workdir: Path) -> BookSpec:
    book = session.scalar(select(Book).where(Book.project_id == project.id))
    st = project.storyteller
    storage = get_storage()
    warnings: list[str] = []
    chapters = session.scalars(select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)).all()
    stories = session.scalars(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None))
                              .order_by(Story.position)).all()
    processing = [s for s in stories if s.status == "processing" or not s.body.strip()]
    if processing:
        n = len(processing)
        warnings.append(f"{n} {'verhaal wordt' if n == 1 else 'verhalen worden'} nog verwerkt of "
                        f"{'heeft' if n == 1 else 'hebben'} nog geen tekst, en {'staat' if n == 1 else 'staan'} "
                        "daarom niet in dit voorbeeld.")
    review = [s for s in stories if s.status == "needs_review" and s.include_in_book and s.body.strip()]
    if review:
        n = len(review)
        warnings.append(f"{n} {'verhaal heeft' if n == 1 else 'verhalen hebben'} nog een controlepunt. Kijk "
                        f"{'het' if n == 1 else 'ze'} even na voordat je het boek goedkeurt.")
    usable = [s for s in stories if s.include_in_book and s.body.strip() and s.status != "processing"]
    by_chapter: dict[str | None, list[Story]] = {}
    for s in usable:
        by_chapter.setdefault(s.chapter_id, []).append(s)
    ordered: list[tuple[str, list[Story]]] = [(ch.title, by_chapter.get(ch.id, [])) for ch in chapters]
    if by_chapter.get(None):
        ordered.append(("Meer verhalen", by_chapter[None]))
    out_chapters = []
    for title, items in ordered:
        if not items:
            continue
        bstories = []
        for s in items:
            qr = session.scalar(select(QRLink).where(QRLink.story_id == s.id, QRLink.revoked_at.is_(None)))
            recs = session.scalars(select(Recording).where(Recording.story_id == s.id, Recording.retracted_at.is_(None),
                                                           Recording.kind == "audio")).all()
            prompt = session.get(Prompt, s.prompt_id) if s.prompt_id else None
            photos = []
            for ph in session.scalars(select(Photo).where(Photo.story_id == s.id, Photo.include_in_book.is_(True))
                                      .order_by(Photo.position, Photo.created_at)).all():
                asset = session.get(MediaAsset, ph.media_id)
                if asset is None or asset.status != "stored" or not asset.width:
                    continue
                dest = workdir / f"{asset.id}{Path(asset.storage_key).suffix}"
                with storage.open(asset.storage_key) as fh:
                    dest.write_bytes(fh.read())
                dpi = asset.width / (136 / 25.4)
                if dpi < 200:
                    warnings.append(f"De foto bij \u201c{s.title}\u201d heeft een lage resolutie (ongeveer {int(dpi)} dpi); "
                                    "in druk kan die wat zacht worden.")
                photos.append(BookPhoto(dest, ph.caption, asset.width, asset.height))
            bstories.append(BookStory(id=s.id, title=s.title or "Een verhaal", question=prompt.text if prompt else "",
                                      body=s.body, audio_code=qr.audio_code if qr else "", qr_token=qr.token if qr else "",
                                      duration=sum(r.duration_seconds or 0 for r in recs), photos=photos,
                                      date_label=month_label(s.first_part_at)))
        out_chapters.append(BookChapter(title=title, stories=bstories))
    if not out_chapters:
        warnings.append("Er zijn nog geen verhalen klaar voor het boek.")
    cover_photo = None
    if book and book.cover_style == "foto" and book.cover_photo_id:
        ph = session.get(Photo, book.cover_photo_id)
        asset = session.get(MediaAsset, ph.media_id) if ph else None
        if asset and asset.status == "stored":
            cover_photo = workdir / f"cover{Path(asset.storage_key).suffix}"
            with storage.open(asset.storage_key) as fh:
                cover_photo.write_bytes(fh.read())
    return BookSpec(title=(book.title if book else project.title), subtitle=book.subtitle if book else "",
                    storyteller=st.name if st else "", dedication=book.dedication if book else "",
                    chapters=out_chapters, cover_style=book.cover_style if book else "nacht", cover_photo=cover_photo,
                    warnings=warnings)


def render_previews(pdf_path: Path, workdir: Path, pages: list[int], scale: float = 1.1) -> list[Path]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    out = []
    for i in pages:
        if i >= len(pdf):
            break
        img = pdf[i].render(scale=scale).to_pil()
        p = workdir / f"pagina-{i + 1:03d}.png"
        img.save(p, optimize=True)
        out.append(p)
    pdf.close()
    return out


@job("build_book", max_attempts=3)
def build_book(session: Session, payload: dict) -> None:
    from .analytics import track
    from .notify import notify_organizers

    version = session.get(BookVersion, payload["version_id"])
    if version is None or version.status == "ready":
        return
    book = session.get(Book, version.book_id)
    project = session.get(Project, book.project_id)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        spec = collect(session, project, work)
        interior = work / "binnenwerk.pdf"
        screen = work / "digitale-editie.pdf"
        cover = work / "omslag.pdf"
        pages = render_interior(spec, interior, clickable=False)
        render_interior(spec, screen, clickable=True)
        cover_info = render_cover(spec, pages, cover)
        warnings = list(spec.warnings)
        from .pricing import MAX_PAGES as ONE_VOLUME, TIER_SURCHARGE, page_tier

        tier = page_tier(pages)
        if tier is None:
            warnings.append(f"Het boek telt {pages} pagina's. Eén band kan maximaal {ONE_VOLUME} pagina's hebben: laat "
                            "verhalen weg of verdeel ze over twee delen.")
        elif tier:
            extra = TIER_SURCHARGE[tier] // 100
            warnings.append(f"Het boek telt {pages} pagina's. Tot {MAX_PAGES} pagina's zit in de prijs; dit boek kost "
                            f"€{extra} extra per exemplaar. Liever niet? Laat een paar verhalen weg.")
        a_int = store_path(session, interior, kind="pdf", mime="application/pdf", project_id=project.id,
                           source="generated", original_filename="binnenwerk.pdf")
        a_scr = store_path(session, screen, kind="pdf", mime="application/pdf", project_id=project.id,
                           source="generated", original_filename="digitale-editie.pdf")
        a_cov = store_path(session, cover, kind="pdf", mime="application/pdf", project_id=project.id,
                           source="generated", original_filename="omslag.pdf")
        preview_ids = []
        (work / "c").mkdir()
        cover_png = render_previews(cover, work / "c", [0], scale=0.45)
        for p in cover_png + render_previews(screen, work, list(range(min(pages, 16))), scale=1.0):
            asset = store_path(session, p, kind="preview", mime="image/png", project_id=project.id, source="generated",
                               original_filename=p.name)
            preview_ids.append(asset.id)
    version.interior_media_id, version.screen_media_id, version.cover_media_id = a_int.id, a_scr.id, a_cov.id
    version.preview_media_ids = preview_ids
    version.page_count = pages
    version.warnings = warnings + [f"Rugdikte (indicatief): {cover_info['spine_mm']} mm."]
    version.story_ids = [s.id for s in spec.stories]
    version.status = "ready"
    track(session, "book_preview_generated", project.id, pages=pages)
    notify_organizers(session, project, "book_ready", "Het voorbeeld van het boek staat klaar",
                      f"{pages} pagina's, {len(spec.stories)} verhalen. Blader erdoor en keur het goed als alles klopt.",
                      url=f"/p/{project.id}/boek/versie/{version.id}")
