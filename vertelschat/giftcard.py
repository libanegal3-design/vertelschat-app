"""Printable gift card (A5 landscape, fold to A6) with the storyteller's personal WhatsApp join QR code."""
from __future__ import annotations

import io

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A5, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Frame, Paragraph

from .book import LAMP, NIGHT, register_fonts
from .models import Project
from .qr import qr_matrix, wa_join_link

GREY = HexColor("#5B6574")


def _esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_gift_card(project: Project, from_name: str = "") -> bytes:
    register_fonts()
    st = project.storyteller
    W, H = landscape(A5)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Cadeaukaart voor {st.name}")
    # left half (outside back when folded): night with wordmark; right half: the message
    c.setFillColor(NIGHT)
    c.rect(0, 0, W / 2, H, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Castoro", 22)
    c.drawString(14 * mm, H - 24 * mm, "vertelschat")
    c.setFillColor(LAMP)
    c.setFont("Castoro-Italic", 13)
    c.drawString(14 * mm, H - 33 * mm, "Jouw verhalen, in je eigen stem.")
    c.setFillColor(white)
    style = ParagraphStyle("w", fontName="Atkinson", fontSize=10.5, leading=15, textColor=white)
    how = ("<b>Zo werkt het</b><br/>Je krijgt af en toe een vraag via WhatsApp. Je antwoordt met een spraakbericht, "
           "wanneer het jou uitkomt. Geen app, geen account, geen wachtwoord. Uit al die verhalen maken we samen een "
           "boek, met bij elk verhaal een code om je stem te horen.")
    Frame(14 * mm, 18 * mm, W / 2 - 28 * mm, 60 * mm, showBoundary=0).addFromList([Paragraph(how, style)], c)
    # right half
    x0 = W / 2 + 14 * mm
    c.setFillColor(NIGHT)
    c.setFont("Castoro-SemiBold", 20)
    c.drawString(x0, H - 24 * mm, f"Voor {st.greeting_name}")
    msg = project.gift_message or "Ik ben zo benieuwd naar je verhalen. Vertel je ze aan mij?"
    if from_name:
        msg += f"<br/><br/>Liefs, {_esc(from_name)}"
    mstyle = ParagraphStyle("m", fontName="Castoro-Italic", fontSize=11.5, leading=16, textColor=NIGHT)
    Frame(x0, H - 78 * mm, W / 2 - 28 * mm, 48 * mm, showBoundary=0).addFromList([Paragraph(_esc(msg).replace(
        "&lt;br/&gt;", "<br/>"), mstyle)], c)
    # QR to the prefilled WhatsApp join message
    matrix = qr_matrix(wa_join_link(st.join_code))
    size = 34 * mm
    cell = size / len(matrix)
    qx, qy = x0, 16 * mm
    c.setFillColor(NIGHT)
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                c.rect(qx + x * cell, qy + size - (y + 1) * cell, cell, cell, stroke=0, fill=1)
    tstyle = ParagraphStyle("t", fontName="Atkinson", fontSize=9.5, leading=13.5, textColor=GREY)
    steps = ("<b>Beginnen:</b> richt de camera van je telefoon op de code en tik op de melding. WhatsApp opent met een "
             "berichtje. Druk op verzenden, klaar."
             f"<br/><br/>Lukt dat niet? Stuur dan de code <b>{st.join_code}</b> via WhatsApp naar het nummer dat je "
             "hebt gekregen.")
    Frame(qx + size + 6 * mm, 12 * mm, W / 2 - 28 * mm - size - 6 * mm, 44 * mm, showBoundary=0).addFromList(
        [Paragraph(steps, tstyle)], c)
    c.setStrokeColor(HexColor("#D5DDE4"))
    c.setDash(2, 3)
    c.line(W / 2, 4 * mm, W / 2, H - 4 * mm)
    c.showPage()
    c.save()
    return buf.getvalue()


def render_storyteller_booklet(project: Project) -> bytes:
    """A6 booklet for the storyteller in the gift package: four pages, large type, no jargon."""
    from reportlab.lib.pagesizes import A6

    register_fonts()
    st = project.storyteller
    W, H = A6
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Zo werkt Vertelschat, voor {st.name}")
    body = ParagraphStyle("b", fontName="Atkinson", fontSize=13, leading=19, textColor=NIGHT)
    head = ParagraphStyle("h", fontName="Castoro-SemiBold", fontSize=18, leading=22, textColor=NIGHT, spaceAfter=8)

    def page(title: str, text: str) -> None:
        Frame(11 * mm, 12 * mm, W - 22 * mm, H - 24 * mm, showBoundary=0).addFromList(
            [Paragraph(_esc(title), head), Paragraph(text, body)], c)
        c.showPage()

    # 1. cover
    c.setFillColor(NIGHT)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(LAMP)
    c.circle(W / 2, H * 0.62, 5 * mm, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Castoro", 24)
    c.drawCentredString(W / 2, H * 0.45, "vertelschat")
    c.setFont("Castoro-Italic", 13)
    c.drawCentredString(W / 2, H * 0.38, f"Voor {st.greeting_name}")
    c.showPage()
    page("Zo werkt het",
         "Af en toe krijg je in WhatsApp een vraag over vroeger.<br/><br/>Je antwoordt met een <b>spraakbericht</b>: "
         "houd het microfoontje ingedrukt en vertel maar. Lang of kort, alles is goed.<br/><br/>Geen app, geen "
         "wachtwoord. Wanneer het jou uitkomt.")
    page("Handige woorden",
         "Stuur een van deze woorden in WhatsApp:<br/><br/><b>VRAAG</b> &nbsp;een nieuwe vraag<br/><b>PAUZE</b> "
         "&nbsp;vier weken rust<br/><b>STOP</b> &nbsp;geen vragen meer<br/><b>HULP</b> &nbsp;uitleg<br/><br/>"
         "Wat je al vertelde, blijft altijd bewaard.")
    # 4. start page with QR
    matrix = qr_matrix(wa_join_link(st.join_code))
    n = len(matrix)
    size = 40 * mm
    cell = size / n
    x0, y0 = (W - size) / 2, 30 * mm
    c.setFillColor(NIGHT)
    for r, row in enumerate(matrix):
        for col, on in enumerate(row):
            if on:
                c.rect(x0 + col * cell, y0 + (n - 1 - r) * cell, cell, cell, stroke=0, fill=1)
    c.setFont("Castoro-SemiBold", 17)
    c.drawCentredString(W / 2, H - 22 * mm, "Beginnen")
    c.setFont("Atkinson", 11.5)
    c.drawCentredString(W / 2, H - 31 * mm, "Scan de code met je telefoon")
    c.drawCentredString(W / 2, H - 37 * mm, "en druk op verzenden.")
    c.setFont("Atkinson-Bold", 11)
    c.drawCentredString(W / 2, 20 * mm, f"Of stuur {st.join_code} naar")
    from .config import get_settings
    c.drawCentredString(W / 2, 14 * mm, get_settings().whatsapp_display_number)
    c.showPage()
    c.save()
    return buf.getvalue()
