"""Overview sheets for the screenshot folders (brand: Vertelschat)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
F = "/root/work/vt/application/vertelschat/book_fonts/"
S = Path("/root/work/vt/screens")
def font(size, bold=False): return ImageFont.truetype(F + ("AtkinsonHyperlegible-Bold.ttf" if bold else "AtkinsonHyperlegible-Regular.ttf"), size)
NIGHT, PAPER, LAMP = (23, 50, 77), (244, 247, 249), (242, 181, 68)
def sheet(out, title, items, cols, thumb_w, max_h):
    gap, label_h, head_h = 28, 44, 110
    thumbs = []
    for fn, label in items:
        im = Image.open(fn).convert("RGB")
        im = im.resize((thumb_w, int(im.height * thumb_w / im.width)), Image.LANCZOS)
        if im.height > max_h: im = im.crop((0, 0, thumb_w, max_h))
        thumbs.append((im, label))
    rows = [thumbs[i:i + cols] for i in range(0, len(thumbs), cols)]
    rh = [max(t[0].height for t in r) + label_h for r in rows]
    W = cols * thumb_w + (cols + 1) * gap; H = head_h + sum(rh) + (len(rows) + 1) * gap
    c = Image.new("RGB", (W, H), PAPER); d = ImageDraw.Draw(c)
    d.rectangle([0, 0, W, head_h - 20], fill=NIGHT); d.ellipse([gap, 38, gap + 18, 56], fill=LAMP)
    d.text((gap + 32, 26), title, font=font(34, True), fill="white")
    y = head_h + gap - 20
    for r, h in zip(rows, rh):
        x = gap
        for im, label in r:
            d.text((x, y + 8), label, font=font(22, True), fill=NIGHT)
            c.paste(im, (x, y + label_h)); d.rectangle([x - 1, y + label_h - 1, x + im.width, y + label_h + im.height], outline=(216, 223, 230))
            x += thumb_w + gap
        y += h + gap
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    c.save(out, optimize=True); print(out, c.size)
st, ap, v2 = S / "storefront", S / "app", S / "pricing-v2"
sheet(S / "overview/1-winkel-desktop.png", "Vertelschat: winkel op desktop", [
    (st / "S01-home-1440.png", "Home"), (st / "S02-product-1440.png", "Product"), (st / "S03-hoe-het-werkt-1440.png", "Hoe het werkt"),
    (st / "S04-cadeau-1440.png", "Cadeau geven"), (st / "S05-voorbeelden-1440.png", "Voorbeelden"), (st / "S06-prijzen-1440.png", "Prijzen"),
    (st / "S07-veelgestelde-vragen-1440.png", "Veelgestelde vragen"), (st / "S08-over-ons-1280.png", "Over ons")], 4, 420, 1400)
sheet(S / "overview/2-winkel-mobiel.png", "Vertelschat: winkel op mobiel", [
    (st / "S01-home-390.png", "Home"), (st / "S02-product-390.png", "Product"), (st / "S03-hoe-het-werkt-390.png", "Hoe het werkt"),
    (st / "S06-prijzen-390.png", "Prijzen")], 4, 360, 1500)
sheet(S / "overview/3-app-desktop.png", "Vertelschat: familie-app op desktop", [
    (ap / "06-dashboard-1440.png", "Overzicht"), (ap / "07-verhalen-1440.png", "Verhalen"), (ap / "08-verhaal-1440.png", "Verhaal"),
    (ap / "10-vragen-1440.png", "Vragen"), (ap / "12-boek-1440.png", "Boek"), (ap / "14-downloads-1440.png", "Downloads")], 3, 560, 900)
sheet(S / "overview/4-app-mobiel.png", "Vertelschat: familie-app op mobiel", [
    (ap / "06-dashboard-390.png", "Overzicht"), (ap / "08-verhaal-390.png", "Verhaal"), (ap / "16-qr-luisteren-390.png", "QR: luisteren"),
    (ap / "16-qr-luisteren-375.png", "QR: luisteren (375)")], 4, 360, 1200)
B = v2 / "before"
sheet(v2 / "overview/1-voor-en-na-prijzen.png", "Prijspagina: voor (links, oude naam) en na (rechts)",
      [(B / "S06-prijzen-1440.png", "Voor: alle extra's naast €129"), (v2 / "V2-01-prijzen-1440.png", "Na: één pakket, later-prijzen stil eronder")], 2, 820, 1500)
sheet(v2 / "overview/2-voor-en-na-product.png", "Productpagina: voor (links, oude naam) en na (rechts)",
      [(B / "S02-product-1440.png", "Voor"), (v2 / "V2-03-product-1440.png", "Na: één of twee vertellers, cadeaupakket optioneel")], 2, 820, 1500)
sheet(v2 / "overview/3-stromen-mobiel.png", "Pricing V2: alle stromen op mobiel (390 px)", [
    (v2 / "V2-01-prijzen-390.png", "Prijzen"), (v2 / "V2-03-product-390.png", "Product en cadeau"),
    (v2 / "V2-20-boek-bestellen-390.png", "Boek klaar: hoeveel exemplaren?"), (v2 / "V2-06-afrekenen-boek-390.png", "Afrekenen (winkel)"),
    (v2 / "V2-22-familiepagina-390.png", "Familielink"), (v2 / "V2-07-afrekenen-familie-390.png", "Familie rekent af"),
    (v2 / "V2-25-overzicht-tweede-verteller-390.png", "Tweede verteller (na het boek)"), (v2 / "V2-10-extra-verteller-390.png", "Extra verteller (winkel)"),
    (v2 / "V2-21-overzicht-verlengen-390.png", "30 dagen voor het einde"), (v2 / "V2-08-verlengen-390.png", "Nog een verteljaar (winkel)"),
    (v2 / "V2-26-verteljaren-390.png", "Overzicht verteljaren"), (v2 / "V2-11-cadeaupakket-390.png", "Cadeaupakket")], 4, 360, 1150)
sheet(v2 / "overview/4-stromen-desktop.png", "Pricing V2: app-stromen op desktop", [
    (v2 / "V2-20-boek-bestellen-1440.png", "Boek klaar: hoeveel exemplaren?"), (v2 / "V2-22-familiepagina-1440.png", "Familiepagina (publiek, minimaal)"),
    (v2 / "V2-21-overzicht-verlengen-1440.png", "Overzicht: verlengen (30 dagen)"), (v2 / "V2-25-overzicht-tweede-verteller-1440.png", "Overzicht: tweede verteller"),
    (v2 / "V2-26-verteljaren-1280.png", "Verteljaren + nog een verteller"), (v2 / "V2-27-tweede-verteller-instellen-1280.png", "Tweede verteller in dezelfde familie"),
    (v2 / "V2-23-instellingen-verlengen-1280.png", "Instellingen: verlengen"), (v2 / "V2-06-afrekenen-boek-1440.png", "Afrekenpagina in de winkel")], 2, 820, 900)
