"""Generates the JSON templates, config and locales of the Shopify theme (content lives here, in one place)."""
import json
from pathlib import Path

T = Path(__file__).resolve().parents[2] / "storefront"  # FINAL-DELIVERY/storefront

def sec(type_, settings=None, blocks=None):
    s = {"type": type_}
    if settings: s["settings"] = settings
    if blocks:
        s["blocks"] = {f"b{i}": b for i, b in enumerate(blocks, 1)}
        s["block_order"] = [f"b{i}" for i in range(1, len(blocks) + 1)]
    return s

def page(sections: dict):
    return {"sections": sections, "order": list(sections.keys())}

def blk(t, **settings):
    return {"type": t, "settings": settings}

def faq(*pairs):
    return [blk("question", question=q, answer=a) for q, a in pairs]

def rt(*blocks):
    return [blk("block", heading=h, body=b) for h, b in blocks]

FAQ_CORE = [
    ("Heeft de verteller een app of account nodig?", "<p>Nee. De verteller gebruikt alleen WhatsApp, zoals altijd. Geen app, geen account, geen wachtwoord en geen link die verloopt. Beantwoorden gaat met een gewoon spraakbericht.</p>"),
    ("Wat als mijn moeder liever typt?", "<p>Dat mag. Langere tekstberichten worden gewoon een verhaal. We raden spraakberichten wel aan: dan bewaar je ook de stem.</p>"),
    ("Wat gebeurt er na het jaar?", "<p>Er komen geen nieuwe vragen meer, maar alles blijft bewaard en gratis te downloaden. De QR-codes in het boek blijven werken. Er is geen abonnement en geen heractivering. <a href=\"/pages/na-het-verteljaar\">Lees wat er precies gebeurt</a>.</p>"),
    ("Schrijft AI de verhalen?", "<p>AI helpt alleen met uitschrijven en netjes maken: stopwoordjes eruit, alinea's erin. Er wordt niets verzonnen of toegevoegd. De originele opname, de letterlijke tekst en het bewerkte verhaal worden apart bewaard, en jullie kunnen alles aanpassen.</p>"),
    ("Worden onze opnames gebruikt om AI te trainen?", "<p>Nee. We werken met leveranciers die jullie gegevens niet gebruiken voor training, en slaan alles op in de EU.</p>"),
]

FAQ_ALL = FAQ_CORE + [
    ("Hoe begint de verteller?", "<p>Jij stuurt een berichtje met een link, of geeft de cadeaukaart met een QR-code. Eén tik opent WhatsApp met een klaar bericht; op verzenden drukken is genoeg. Daarna vraagt Vertelschat eerst om toestemming, pas dan komt de eerste vraag.</p>"),
    ("Hoe vaak komt er een vraag?", "<p>Jullie kiezen: elke week, om de week of elke vier weken, op een vaste dag en tijd. Er komt pas een nieuwe vraag als de vorige beantwoord is, dus niets stapelt zich op.</p>"),
    ("Wie bedenkt de vragen?", "<p>Ruim 160 vragen zijn door mensen geschreven, over jeugd, werk, liefde, feesten en de tijdgeest, met Vlaamse varianten. Familieleden kunnen zelf vragen toevoegen. Vragen over verlies of ziekte komen alleen als je dat zelf aanzet.</p>"),
    ("Kan de verteller een verhaal in delen inspreken?", "<p>Ja. Meerdere spraakberichten achter elkaar worden één verhaal. Later nog iets bedacht? Gewoon sturen, of als antwoord op de vraag: het komt bij het juiste verhaal.</p>"),
    ("Hoe lang mag een spraakbericht zijn?", "<p>Zo lang als ze wil. Een minuut of een half uur, allebei goed.</p>"),
    ("Kan de verteller stoppen of pauzeren?", "<p>Altijd. PAUZE geeft vier weken rust, STOP stopt de vragen, START gaat weer verder. Wat al verteld is, blijft bewaard.</p>"),
    ("Hoeveel familieleden kunnen meeluisteren?", "<p>Zoveel als jullie willen. Iedereen krijgt een eigen login en een melding bij een nieuw verhaal. Rollen bepalen wie mag bewerken.</p>"),
    ("Hoe ziet het boek eruit?", "<p>Een hardcover van 17 bij 24 centimeter, tot 240 pagina's, met bij elk verhaal de vraag, de tekst, foto's en een QR-code met audiocode. Achterin staat een luisterregister. Extra exemplaren kosten €45, elk volgend exemplaar in hetzelfde pakket €39.</p>"),
    ("Werkt het in België?", "<p>Ja. Er zijn Vlaamse varianten van de vragen, betalen kan met Bancontact en verzending in België is gratis.</p>"),
    ("Kunnen we alles verwijderen?", "<p>Ja. Je kunt verwijdering aanvragen in de app; na veertien dagen bedenktijd wissen we alles definitief. Download eerst het archief als je iets wilt bewaren.</p>"),
    ("Wat als de verteller van telefoonnummer wisselt?", "<p>Dan koppel je in de app het oude nummer los en stuur je een nieuwe uitnodiging. Er gaat niets verloren.</p>"),
]

PROMISE = [blk("point", title="Altijd downloaden", text="Alle opnames, teksten, verhalen, foto's en het boek, als gewone bestanden die op elke computer werken."),
           blk("point", title="QR-codes blijven werken", text="Ze wijzen naar een eigen adres dat alleen daarvoor bestaat, los van onze winkel."),
           blk("point", title="Geen heractivering", text="Terugkomen om te luisteren of te downloaden kost niets, ook na jaren."),
           blk("point", title="Werkt ook zonder ons", text="Elke opname heeft een audiocode (A01, A02...) die ook in jullie eigen archief staat.")]

def promise(tint=None):
    return sec("promise", {"heading": "Alles blijft van jullie",
                           "intro": "<p>Een verteljaar duurt twaalf maanden. Wat er in dat jaar verteld is, blijft daarna gewoon van jullie: zonder abonnement, zonder heractivering, zonder kleine lettertjes.</p>",
                           "link_label": "Wat er gebeurt na het verteljaar", "link": "/pages/na-het-verteljaar"}, PROMISE)

INCLUDES = ["Een jaar lang vragen via WhatsApp, in het ritme dat jullie kiezen",
            "Onbeperkt verhalen, spraakberichten en foto's",
            "Onbeperkt familieleden die meeluisteren en vragen voorstellen",
            "Een hardcover boek, 17 × 24 cm, tot 240 pagina's, met QR-codes naar de stem",
            "Gratis verzending in Nederland en België",
            "Alles altijd gratis te downloaden, ook na het jaar"]

LATER = [
    ("Extra exemplaar", "€45", "Elk volgend exemplaar in hetzelfde pakket €39: het scheelt een verzending. Altijd na te bestellen, ook na jaren."),
    ("Familie bestelt zelf", "€45", "Via een privélink betaalt ieder zijn eigen exemplaar, zonder toegang tot jullie verhalen."),
    ("Tweede verteller", "€99", "Bijvoorbeeld de andere ouder: een eigen jaar en een eigen boek, in dezelfde familie."),
    ("Nog een verteljaar", "€79", "Twaalf maanden nieuwe vragen plus een boektegoed. Nooit nodig om bij je verhalen te kunnen."),
    ("Dikker boek", "+€15", "Per exemplaar voor 241 tot 320 pagina's (+€29 tot 400). Je ziet het ruim voordat je bestelt."),
    ("Cadeaupakket per post", "€17,95", "Kaart met persoonlijke QR-code en een uitlegboekje, in een doosje door de brievenbus."),
]


def pricing(tint=False, h1=False, later_open=False):
    return sec("pricing", {"heading_tag": "h1" if h1 else "h2", "heading": "Eén prijs, alles erin", "price": "€129",
                           "price_note": "eenmalig, inclusief btw en boek",
                           "intro": "Alles om een jaar lang verhalen te verzamelen én het boek te maken. Er zit niets achter slot.",
                           "cta_label": "Geef een verteljaar", "cta_link": "/products/verteljaar",
                           "fine": "Geen abonnement en geen automatische verlenging. Betalen met iDEAL | Wero, Bancontact of creditcard.",
                           "later_heading": "Later uitbreiden kan altijd",
                           "later_intro": "Alleen als jullie dat willen, op het moment dat het past.",
                           "later_note": "Alle prijzen inclusief btw en verzending in Nederland en België. Niets wordt automatisch verlengd.",
                           "later_open": later_open, "tint": tint},
               [blk("include", text=t) for t in INCLUDES] + [blk("later", title=t, price=p, text=d) for t, p, d in LATER])


def cta():
    return sec("cta-band", {"heading": "De mooiste verhalen komen als je ernaar vraagt.",
                            "text": "Een verteljaar kost €129, boek inbegrepen.",
                            "cta_label": "Geef een verteljaar", "cta_link": "/products/verteljaar",
                            "secondary_label": "Zo werkt het", "secondary_link": "/pages/hoe-het-werkt"})

CHAT_HOME = [blk("message", kind="in", text="Hoi Marijke! Sanne heeft Vertelschat voor je geregeld. Af en toe krijg je een vraag over vroeger, en je antwoordt gewoon met een spraakbericht. Doe je mee?", buttons="Ja, ik doe mee|Dit ben ik niet"),
             blk("message", kind="in", text="Hoi Marijke, hier is je vraag van deze week:\n\nHoe zag het huis eruit waar je opgroeide? Neem me eens mee door de kamers."),
             blk("message", kind="voice", text="4:38"),
             blk("message", kind="voice", text="0:51"),
             blk("message", kind="in", text="Dankjewel! Je verhaal is goed aangekomen. Sanne kan het nu beluisteren.")]

STEPS_HOME = [blk("step", title="Jij zet het klaar", text="Je kiest het ritme en de eerste vragen. Dan stuur je een berichtje met een link, of geef je de cadeaukaart."),
              blk("step", title="Zij vertellen via WhatsApp", text="Er komt een vraag. Antwoorden gaat met een gewoon spraakbericht, wanneer het uitkomt. Een minuut of een half uur, alles is goed."),
              blk("step", title="Jullie krijgen verhalen en een boek", text="Jij krijgt de opname, de uitgeschreven tekst en het verhaal. Aan het eind maak je het boek, met bij elk verhaal de stem.")]

templates = {}
templates["index"] = page({
    "hero": sec("hero-chat-book", {"cta_link": "/products/verteljaar", "secondary_link": "/pages/hoe-het-werkt"}),
    "facts": sec("facts", {"heading": "Wat je kunt verwachten"}, [
        blk("fact", title="Alleen WhatsApp", text="Geen app, geen account, geen wachtwoord voor de verteller."),
        blk("fact", title="Vragen door mensen geschreven", text="Ruim 160 vragen, ook in het Vlaams. Geen chatbot."),
        blk("fact", title="De echte stem in het boek", text="Bij elk verhaal een QR-code naar de opname."),
        blk("fact", title="Alles blijft van jullie", text="Downloaden kan altijd, ook na het jaar.")]),
    "steps": sec("steps", {"heading": "Zo werkt het", "intro": "Drie stappen, en voor de verteller maar één: een spraakbericht sturen."}, STEPS_HOME),
    "book": sec("image-text", {"heading": "Een boek dat je kunt horen",
        "text": "<p>Elk verhaal krijgt een eigen pagina, met de vraag, de tekst en foto's. Daaronder staat een QR-code: scan hem met je telefoon en je hoort het verhaal in hun eigen woorden, met hun eigen lach en hun eigen stiltes.</p><p>Naast elke QR-code staat een audiocode, bijvoorbeeld A07. In jullie eigen archief heet het bestand ook zo. Zo werkt het boek over twintig jaar nog, ook zonder ons.</p>",
        "asset_image": "book-spread.png", "image_alt": "Opengeslagen boek met een verhaal, een foto en een QR-code", "caption": "Voorbeeldpagina's. De verhalen in onze voorbeelden zijn verzonnen.",
        "layout": "image_left", "background": "paper", "cta_label": "Bekijk voorbeelden", "cta_link": "/pages/voorbeelden"}),
    "teller": sec("chat-sample", {"heading": "Voor de verteller verandert er niets",
        "text": "<p>Geen app installeren, geen wachtwoord onthouden, geen link die verloopt. Er komt een berichtje in WhatsApp, en antwoorden gaat met het microfoonknopje dat ze al kennen.</p><p>Iets vergeten? Later nog een spraakbericht sturen, het komt vanzelf bij het juiste verhaal. Even geen zin? PAUZE sturen. En er is altijd een mens bereikbaar.</p>",
        "note": "Voorbeeld. De namen en het verhaal zijn verzonnen.", "tint": True}, CHAT_HOME),
    "promise": promise(),
    "pricing": pricing(),
    "faq": sec("faq", {"heading": "Veelgestelde vragen", "link_label": "Alle vragen en antwoorden", "link": "/pages/veelgestelde-vragen"}, faq(*FAQ_CORE)),
    "cta": cta(),
})

templates["product"] = page({
    "main": sec("main-product", {"show_gift": True, "gift_product": "cadeaupakket", "variant_legend": "Voor wie is het?",
                                 "variant_note_1": "Een jaar vragen, verhalen en één boek.",
                                 "variant_note_2": "Bijvoorbeeld beide ouders: ieder een eigen jaar en een eigen boek. De tweede verteller kost €99."},
                [blk("include", text=t) for t in INCLUDES]),
    "steps": sec("steps", {"heading": "Na je bestelling", "tint": True}, [
        blk("step", title="Meteen een mail", text="Met een link om het verteljaar klaar te zetten: voor wie, welk ritme, welke eerste vragen."),
        blk("step", title="Uitnodigen", text="Stuur het berichtje met de link, of print de cadeaukaart. Koos je het cadeaupakket, dan sturen wij het op."),
        blk("step", title="De eerste vraag", text="Zodra de verteller ja zegt in WhatsApp, komt de eerste vraag. Jij hoort het als er een verhaal binnen is.")]),
    "promise": promise(),
    "faq": sec("faq", {"heading": "Nog vragen?", "link_label": "Alle vragen en antwoorden", "link": "/pages/veelgestelde-vragen"}, faq(*FAQ_CORE[:4])),
})

templates["page.hoe-het-werkt"] = page({
    "intro": sec("rich-text", {"heading": "Zo werkt Vertelschat", "heading_level": "1", "narrow": True, "compact_bottom": True,
        "intro": "<p>Een jaar lang, in een rustig ritme, een vraag via WhatsApp. De verteller antwoordt met spraakberichten; de familie krijgt de opname, de tekst en het verhaal, en maakt aan het eind samen het boek.</p>"}),
    "steps": sec("steps", {"heading": "Van bestelling tot boek"}, [
        blk("step", title="Bestellen", text="Je bestelt een verteljaar, als cadeau of voor jezelf. Meteen daarna krijg je een mail om alles klaar te zetten."),
        blk("step", title="Klaarzetten", text="Je vult in voor wie het is, kiest het ritme (bijvoorbeeld elke zondag om tien uur) en de eerste vragen, en nodigt familie uit."),
        blk("step", title="Verbinden", text="De verteller krijgt jouw berichtje of de cadeaukaart, tikt op de link en drukt op verzenden. Vertelschat vraagt eerst of het goed is."),
        blk("step", title="Vertellen", text="Er komt een vraag, de verteller spreekt een antwoord in. Wij bewaren de opname, schrijven hem uit en maken er een verhaal van, zonder iets te verzinnen."),
        blk("step", title="Het boek", text="Jullie kiezen de verhalen en foto's, bekijken een voorbeeld en keuren het goed. Het boek wordt gedrukt en gratis bezorgd.")]),
    "chat": sec("chat-sample", {"heading": "In WhatsApp", "text": "<p>Zo ziet het eruit voor de verteller: een vraag, een spraakbericht en een bedankje. Meer niet.</p>", "tint": True}, CHAT_HOME),
    "app": sec("image-text", {"heading": "Voor de familie: alles op één plek",
        "text": "<p>In de familie-app luister en lees je elk verhaal, stel je zelf vragen voor en kijk je wat er nog nagekeken moet worden. Iedereen krijgt een eigen login, zonder wachtwoord.</p><ul><li>Bij elk verhaal de opname, de letterlijke tekst en de bewerkte versie</li><li>Vragen in de wachtrij zetten, verschuiven of pauzeren</li><li>Het boek samenstellen en een voorbeeld bekijken</li></ul>",
        "asset_image": "app-dashboard.png", "image_alt": "Het overzicht in de familie-app met het nieuwste verhaal", "layout": "image_right", "background": "paper"}),
    "book": sec("image-text", {"heading": "Het boek, met de stem erbij",
        "text": "<p>Elk verhaal begint op een nieuwe pagina, met de vraag, de tekst, foto's en een QR-code. Achterin staat een luisterregister met alle audiocodes. Het boek is een hardcover van 17 bij 24 centimeter.</p>",
        "asset_image": "book-spread.png", "image_alt": "Opengeslagen boek", "layout": "image_left", "background": "twilight"}),
    "faq": sec("faq", {"heading": "Vragen over hoe het werkt"}, faq(*FAQ_ALL[5:10])),
    "cta": cta(),
})

templates["page.cadeau"] = page({
    "intro": sec("rich-text", {"heading": "Een cadeau dat je samen uitpakt", "heading_level": "1", "compact_bottom": True,
        "intro": "<p>Voor een verjaardag, Moederdag, Vaderdag of zomaar. Het verteljaar begint pas als de verteller ja zegt, dus je kunt het rustig van tevoren kopen.</p>"}),
    "options": sec("gift-options", {"heading": "Hoe geef je het?", "intro": "Na de bestelling krijg je alles wat je nodig hebt. Kies wat bij het moment past."}, [
        blk("option", title="Digitaal", price="inbegrepen", text="Je krijgt een berichtje om door te sturen via WhatsApp en een cadeaukaart om zelf te printen. Meteen klaar."),
        blk("option", title="Cadeaupakket per post", price="€17,95", text="De kaart op stevig papier met jouw boodschap en de persoonlijke QR-code, een uitlegboekje in grote letters voor de verteller, in een doosje dat door de brievenbus past."),
        blk("option", title="Samen aan tafel", price="inbegrepen", text="Laat de verteller de QR-code op je scherm scannen. Twee minuten, en de eerste vraag is onderweg.")]),
    "steps": sec("steps", {"heading": "Zo verloopt het", "tint": True}, [
        blk("step", title="Bestellen en klaarzetten", text="Je kiest het ritme en de eerste vragen. Een persoonlijke boodschap zet je op de kaart."),
        blk("step", title="Geven", text="De kaart of het berichtje. Niets begint zonder dat de verteller ja zegt; de verrassing blijft een verrassing."),
        blk("step", title="Samen genieten", text="Iedereen die je uitnodigt, hoort het als er een nieuw verhaal is.")]),
    "faq": sec("faq", {"heading": "Vragen over cadeau geven"}, faq(
        ("Wanneer begint het jaar?", "<p>Pas als de verteller ja zegt in WhatsApp. Tot die tijd loopt er niets af.</p>"),
        ("Ziet de verteller wat het kost?", "<p>Nee. Op de kaart en in de berichten staan geen prijzen.</p>"),
        ("Kan ik het voor twee opa's tegelijk geven?", "<p>Ja, bestel dan twee verteljaren. Elke verteller heeft een eigen project.</p>"),
        ("Wat als de verteller geen WhatsApp heeft?", "<p>Dan werkt Vertelschat helaas niet goed. Neem contact op, dan zoeken we samen een oplossing of krijg je je geld terug zolang het jaar niet is begonnen.</p>"))),
    "cta": cta(),
})

templates["page.voorbeelden"] = page({
    "intro": sec("rich-text", {"heading": "Voorbeelden", "heading_level": "1", "compact_bottom": True,
        "intro": "<p>Hoe het boek, de app en het gesprek in WhatsApp eruitzien. Alle namen en verhalen hier zijn verzonnen; echte verhalen van families laten we alleen zien als zij dat zelf willen.</p>"}),
    "book": sec("image-text", {"heading": "Een verhaal in het boek",
        "text": "<p>De vraag staat schuin onder de titel, daarna volgt het verhaal zoals het verteld is: licht geredigeerd, niets toegevoegd. De QR-code staat direct onder de tekst, met de audiocode ernaast.</p>",
        "asset_image": "book-page.png", "image_alt": "Boekpagina met het verhaal Het huis waar ik opgroeide", "layout": "image_left", "background": "paper"}),
    "cover": sec("image-text", {"heading": "Titel en omslag kiezen jullie zelf",
        "text": "<p>Drie omslagen: Nacht, Schemer of met een eigen foto. Rug en titel worden automatisch gezet.</p>",
        "asset_image": "book-cover.png", "image_alt": "Omslag van het boek De verhalen van Marijke", "layout": "image_right", "background": "twilight"}),
    "questions": sec("rich-text", {"heading": "Een greep uit de vragen", "narrow": True}, rt(
        ("", "<ul><li>Hoe zag het huis eruit waar je opgroeide? Neem me eens mee door de kamers.</li><li>Hoe zag een gewone zondag eruit toen jij klein was?</li><li>Wat was je allereerste baantje, en wat verdiende je ermee?</li><li>Welk gerecht maakte jouw moeder dat je nooit vergeet?</li><li>Wat deed je op een regenachtige woensdagnamiddag? <em>(Vlaamse variant)</em></li><li>Hoe leerde je je grote liefde kennen?</li><li>Wat wil je dat je kleinkinderen over het leven weten?</li></ul><p>De vragen zijn door mensen geschreven en passen zich aan: aan het geboortejaar, aan Nederland of Vlaanderen, en aan wat de verteller al vertelde.</p>"))),
    "chat": sec("chat-sample", {"heading": "Het gesprek", "text": "<p>Eerst toestemming, dan de vraag, dan het antwoord.</p>", "tint": False}, CHAT_HOME),
    "cta": cta(),
})

templates["page.prijzen"] = page({
    "pricing": pricing(h1=True, later_open=True),
    "compare": sec("comparison", {"heading": "Hoe verhoudt Vertelschat zich tot de alternatieven?",
        "intro": "Eerlijk vergeleken met de soorten oplossingen die er zijn. Details verschillen per aanbieder.",
        "note": "Vergelijking op hoofdlijnen, stand september 2026. Kijk bij andere aanbieders altijd naar hun actuele voorwaarden."}, [
        blk("row", label="Wat de verteller moet doen", us="Een spraakbericht sturen in WhatsApp", app="Een app of link openen, soms inloggen", book="Zelf schrijven"),
        blk("row", label="De echte stem bewaard", us="Ja, bij elk verhaal", app="Soms", book="Nee"),
        blk("row", label="Na afloop", us="Alles gratis downloaden, QR-codes blijven werken", app="Verschilt, soms alleen bekijken", book="Het boek"),
        blk("row", label="Kosten", us="€129 eenmalig, boek inbegrepen", app="Vaak een jaarbedrag", book="Vanaf ongeveer €25")]),
    "promise": promise(),
    "faq": sec("faq", {"heading": "Vragen over de prijs"}, faq(
        ("Wordt het jaar automatisch verlengd?", "<p>Nee. Er is geen abonnement. Wil je nog een jaar vragen, dan bestel je dat zelf: nog een verteljaar kost €79, inclusief een boektegoed voor deel 2 of een extra exemplaar.</p>"),
        ("Wat kost een extra boek?", "<p>€45, gratis verzonden. Gaan er meer exemplaren in hetzelfde pakket, dan kost elk volgend exemplaar €39: het scheelt ons een verzending, dat geven we door. Bestellen kan altijd, ook na jaren.</p>"),
        ("Kan de familie zelf een boek kopen?", "<p>Ja. Als het boek klaar is, maak je in de app een privélink. Broers, zussen of kleinkinderen bestellen en betalen daarmee hun eigen exemplaar (€45). Ze zien alleen de omslag en de titel, niet de verhalen.</p>"),
        ("Wat als het boek dikker wordt dan 240 pagina's?", "<p>Dat mag. Tot 320 pagina's kost een exemplaar €15 meer, tot 400 pagina's €29 meer. Je ziet de omvang in de app terwijl het boek groeit, dus nooit pas bij het afrekenen. Liever niet? Laat een paar verhalen weg.</p>"),
        ("Kan ik later ook de verhalen van mijn vader toevoegen?", "<p>Ja. Een tweede verteller in dezelfde familie kost €99: een eigen jaar vragen en een eigen boek. Wie al meeleest, hoort er meteen bij. Voor beide ouders tegelijk kies je bij het bestellen 'Twee vertellers' (€228).</p>"),
        ("Kost downloaden iets?", "<p>Nee, nooit. Ook niet na het verteljaar.</p>"),
        ("Hoe kan ik betalen?", "<p>Met iDEAL | Wero, Bancontact, creditcard en andere methodes die de kassa toont.</p>"))),
    "cta": cta(),
})

templates["page.veelgestelde-vragen"] = page({
    "faq": sec("faq", {"heading_tag": "h1", "heading": "Veelgestelde vragen", "intro": "Staat je vraag er niet bij? Mail ons, we antwoorden binnen één werkdag.", "link_label": "Contact", "link": "/pages/contact"}, faq(*FAQ_ALL)),
    "cta": cta(),
})

templates["page.over-ons"] = page({
    "about": sec("rich-text", {"heading": "Over Vertelschat", "heading_level": "1",
        "intro": "<p>Bijna iedereen heeft een ouder of grootouder met verhalen die nog niet opgeschreven zijn. En bijna iedereen van die generatie gebruikt WhatsApp. Daar begint Vertelschat.</p>"}, rt(
        ("Waarom WhatsApp", "<p>Veel mooie diensten vragen de verteller om een app, een account of een link. Voor wie daar niet dagelijks mee werkt, is dat net een drempel te veel. Een spraakbericht sturen kan bijna iedereen. Dus laten we de techniek bij ons, en het vertellen bij hen.</p>"),
        ("Waar we voor staan", "<ul><li><strong>Niets verzinnen.</strong> AI helpt met uitschrijven en netjes maken, nooit met toevoegen. De opname blijft altijd de bron.</li><li><strong>Alles blijft van jullie.</strong> Downloaden kan altijd, zonder abonnement. QR-codes blijven werken.</li><li><strong>Geen trucs.</strong> Geen aftelklokken, geen nep-recensies, geen automatische verlenging.</li><li><strong>Zorgvuldig met privacy.</strong> Opslag in de EU, geen training van AI-modellen met jullie verhalen.</li><li><strong>Vragen van mensen.</strong> Door mensen geschreven, met aandacht voor Nederland en Vlaanderen.</li></ul>"),
        ("Wie we zijn", "<p>[Hier komt een kort, echt verhaal over de oprichters, met foto. Nog in te vullen voor de lancering.]</p>"))),
    "promise": promise(),
})

templates["page.contact"] = page({
    "form": sec("contact-form", {"heading": "Contact"}),
    "faq": sec("faq", {"heading": "Misschien staat je antwoord hier"}, faq(*FAQ_ALL[10:14])),
})

LEGAL = {
 "privacy": ("Privacyverklaring", [
   ("Wie zijn wij", "<p>Vertelschat is een dienst van [handelsnaam], [adres], KvK [nummer] (hierna: wij). Wij zijn verwerkingsverantwoordelijke voor de gegevens in deze verklaring. Contact: hallo@vertelschat.nl.</p>"),
   ("Welke gegevens", "<ul><li>Van de koper en familieleden: naam, e-mailadres, bestel- en verzendgegevens.</li><li>Van de verteller: naam of aanspreeknaam, geboortejaar (optioneel), WhatsApp-nummer of WhatsApp-gebruikers-ID (versleuteld opgeslagen), spraakberichten, berichten en foto's die de verteller stuurt, en de toestemming die de verteller geeft.</li><li>Afgeleid: uitgeschreven tekst, bewerkte verhalen, het boek.</li><li>Technisch: inloggegevens, beveiligingslogboeken. Geen advertentiecookies.</li></ul>"),
   ("Waarvoor en op welke grondslag", "<ul><li>Het leveren van het verteljaar en het boek (uitvoering van de overeenkomst met de koper).</li><li>Het verwerken van de opnames van de verteller: op basis van toestemming van de verteller, die in WhatsApp wordt gevraagd en altijd kan worden ingetrokken. [JURIDISCH TOETSEN: grondslag en rol van de verteller als betrokkene die geen partij is bij de overeenkomst.]</li><li>Beveiliging en het voorkomen van misbruik (gerechtvaardigd belang).</li><li>Wettelijke bewaarplichten, zoals de administratie.</li></ul>"),
   ("Automatisch uitschrijven en bewerken", "<p>Spraakberichten worden automatisch uitgeschreven en licht bewerkt. We controleren automatisch dat er geen namen of getallen worden toegevoegd die niet in de opname voorkomen. De originele opname en de letterlijke tekst blijven apart bewaard. Er vindt geen geautomatiseerde besluitvorming plaats over personen.</p>"),
   ("Wie verwerkt gegevens voor ons", "<ul><li>Meta Platforms Ireland (WhatsApp Business Platform): berichten tussen verteller en Vertelschat.</li><li>[Hostingpartij, EU]: servers en opslag.</li><li>Mistral AI (Frankrijk): uitschrijven van spraak. Training met onze gegevens staat uit.</li><li>Anthropic: licht redigeren van de tekst. Geen training met API-gegevens. [JURIDISCH TOETSEN: doorgifte buiten de EER, SCC's/DPF, bewaartermijn bij de leverancier.]</li><li>Shopify: webwinkel en betalingen.</li><li>[Mailprovider] en [drukkerij, bijvoorbeeld Peecho in Amsterdam].</li></ul><p>Met al deze partijen sluiten wij verwerkersovereenkomsten.</p>"),
   ("Hoe lang bewaren we", "<p>Zolang het project bestaat, ook na het verteljaar: dat is precies de belofte dat alles van jullie blijft. Verwijderen kan altijd; na een aanvraag wissen we alles na 14 dagen bedenktijd. Berichten van onbekende nummers die niet aan een project gekoppeld worden, wissen we na 30 dagen. Administratieve gegevens bewaren we 7 jaar (wettelijke plicht).</p>"),
   ("Jouw rechten", "<p>Je hebt recht op inzage, correctie, verwijdering, beperking, bezwaar en overdraagbaarheid. Via \"Download alles\" krijg je al je gegevens in open formaten. De verteller kan altijd STOP sturen en om verwijdering vragen. Klachten kun je indienen bij de Autoriteit Persoonsgegevens (Nederland) of de Gegevensbeschermingsautoriteit (België).</p>"),
   ("Beveiliging", "<p>Telefoonnummers worden versleuteld opgeslagen, alle verbindingen zijn versleuteld, bestanden zijn alleen bereikbaar via tijdelijke, ondertekende links. QR-codes zijn niet te raden en kunnen met een pincode worden beveiligd.</p>")]),
 "voorwaarden": ("Algemene voorwaarden", [
   ("De dienst", "<p>Een verteljaar bestaat uit twaalf maanden vragen via WhatsApp vanaf het moment dat de verteller toestemming geeft, het verwerken van de antwoorden tot verhalen, een familie-app en één gedrukt boek. Er is geen abonnement en geen automatische verlenging.</p>"),
   ("Prijs en betaling", "<p>De prijs staat bij het product en is inclusief btw. Betaling vindt plaats via de kassa van onze webwinkel.</p>"),
   ("Toestemming van de verteller", "<p>Je mag een verteljaar alleen starten voor iemand die daar zelf mee instemt. Vertelschat vraagt de verteller in WhatsApp om toestemming voordat er vragen komen. De verteller kan altijd stoppen.</p>"),
   ("Na het verteljaar", "<p>Na afloop komen er geen nieuwe vragen meer. Opnames, teksten, verhalen, foto's, boekbestanden en exports blijven gratis te bekijken en te downloaden, en de QR-codes blijven werken, zonder heractiveringskosten. Bewerken hoort bij een actief verteljaar (plus 30 dagen). Extra exemplaren van een goedgekeurd boek blijven altijd te bestellen.</p>"),
   ("Als Vertelschat stopt", "<p>Mochten wij ooit stoppen, dan krijgen alle families minstens zes maanden van tevoren bericht, met de mogelijkheid om alles te downloaden. [JURIDISCH TOETSEN: continuïteitsafspraak / escrow voor het QR-domein.]</p>"),
   ("Aansprakelijkheid", "<p>[In te vullen na juridisch advies.]</p>"),
   ("Toepasselijk recht", "<p>Op deze voorwaarden is Nederlands recht van toepassing. Consumenten in België behouden de bescherming van dwingend Belgisch recht.</p>")]),
 "retourneren": ("Retourneren en herroepen", [
   ("Bedenktijd", "<p>Je hebt 14 dagen bedenktijd na aankoop. Is het verteljaar nog niet begonnen (de verteller heeft nog geen toestemming gegeven), dan krijg je het volledige bedrag terug. Mail hallo@vertelschat.nl.</p>"),
   ("Als het jaar al is begonnen", "<p>Door te starten binnen de bedenktijd vraag je ons uitdrukkelijk om te beginnen. Herroep je daarna alsnog binnen 14 dagen, dan betaal je een evenredig deel. [JURIDISCH TOETSEN: formulering conform art. 6:230p BW / Belgisch WER boek VI.]</p>"),
   ("Het boek", "<p>Het boek wordt speciaal voor jullie gemaakt en valt daarom buiten het herroepingsrecht. Is er een drukfout of beschadiging? Dan drukken we het opnieuw, kosteloos.</p>")]),
 "verzending": ("Verzending", [
   ("Kosten", "<p>Verzending van het boek is gratis in Nederland en België. Andere landen: [op aanvraag].</p>"),
   ("Levertijd", "<p>Een boek wordt gedrukt na jullie goedkeuring. Reken op ongeveer twee tot drie weken tot de bezorging. Je krijgt een track-and-tracecode.</p>"),
   ("Cadeaupakket", "<p>We sturen het binnen twee werkdagen nadat je het verteljaar hebt klaargezet (de kaart krijgt de persoonlijke QR-code), met PostNL of bpost door de brievenbus.</p>"),
   ("Familie-exemplaren", "<p>Een exemplaar dat familie via de privélink bestelt, gaat naar het adres dat zij bij het afrekenen invullen.</p>")]),
}
for handle, (title, blocks) in LEGAL.items():
    templates[f"page.{handle}"] = page({"main": sec("rich-text", {"heading": title, "heading_level": "1", "legal_notice": True, "narrow": True}, rt(*blocks))})

templates["page.na-het-verteljaar"] = page({
    "main": sec("rich-text", {"heading": "Na het verteljaar", "heading_level": "1", "narrow": True,
        "intro": "<p>Een verteljaar duurt twaalf maanden. Dit is wat er daarna gebeurt, zonder kleine lettertjes.</p>"}, rt(
        ("Wat blijft", "<ul><li>Alle opnames, de letterlijke teksten, de verhalen en de foto's: bekijken, beluisteren en downloaden, gratis.</li><li>Het boek als pdf, en extra exemplaren bestellen van een goedgekeurd boek.</li><li>De QR-codes in het boek.</li><li>Alle familieleden houden toegang.</li></ul>"),
        ("Wat stopt", "<ul><li>Er komen geen nieuwe vragen meer via WhatsApp.</li><li>Na 30 dagen extra kun je verhalen niet meer bewerken. Bekijken en downloaden blijft altijd kunnen.</li></ul>"),
        ("Laat binnengekomen verhalen", "<p>Stuurt de verteller na het jaar nog een spraakbericht? Dan bewaren we het gewoon, en je kunt het beluisteren en downloaden. Antwoorden op vragen die nog binnen het jaar gesteld zijn, verwerken we tot 30 dagen na afloop nog volledig.</p>"),
        ("Nog een jaar?", "<p>Dat kan met Nog een verteljaar: €79, inclusief een boektegoed voor deel 2 of een extra exemplaar. Kocht je Vertelschat vóór oktober 2026? Dan blijft verlengen zonder boektegoed voor jou €69. Het is nooit nodig om bij je verhalen te kunnen.</p>"),
        ("Extra boeken", "<p>Een extra exemplaar bestellen blijft altijd kunnen, ook als het verteljaar al lang voorbij is: €45, elk volgend exemplaar in hetzelfde pakket €39. Familie kan via een privélink zelf bestellen.</p>"),
        ("Download alles", "<p>In de app staat de knop Download alles: één zip-bestand met alle opnames (origineel en mp3), teksten, verhalen, foto's, het boek, en een overzicht dat je zonder internet in je browser opent. Bewaar een kopie op twee plekken.</p>"),
        ("Als Vertelschat ooit stopt", "<p>Dan krijgen alle families minstens zes maanden van tevoren bericht om alles op te halen. De audiocodes in het boek komen overeen met de bestandsnamen in jullie archief, zodat het boek ook dan te beluisteren blijft.</p>"))),
    "promise": promise(),
})

templates["page.boek-afrekenen"] = page({"main": sec("order-bridge")})

APP = "https://app.vertelschat.nl/app"
def addon(context, points, **settings):
    return page({"main": sec("main-addon", {"context": context, "app_link": APP, **settings}, [blk("point", text=t) for t in points])})

templates["product.verlenging"] = addon("project", [
    "Twaalf maanden nieuwe vragen via WhatsApp, in hetzelfde ritme",
    "Nieuwe verhalen, uitgeschreven en netjes gemaakt zoals nu",
    "Een boektegoed: voor deel 2 met de nieuwe verhalen, of als extra exemplaar",
    "Geen abonnement: na twaalf maanden stopt het vanzelf"],
    variant_legend="Uitvoering", price_note="eenmalig, inclusief btw",
    missing_text="Verlengen doe je vanuit je verteljaar in de app, zodat we weten welk verteljaar verder gaat.",
    app_label="Naar mijn verteljaar", note="Alles wat al verteld is, blijft ook zonder verlengen van jullie.")
templates["product.extra-verteller"] = addon("family_optional", [
    "Een eigen jaar vragen via WhatsApp voor de nieuwe verteller",
    "Een eigen boek, 17 × 24 cm, met QR-codes naar de stem",
    "Komt in dezelfde familie: wie al meeleest, hoort er meteen bij",
    "Geen abonnement en geen automatische verlenging"],
    price_note="eenmalig, inclusief btw en boek",
    note="Bestel je dit vanuit de app, dan komt de verteller vanzelf bij jullie familie. Zonder familie begin je een nieuwe.",
    fallback_image="book-cover.png")
templates["product.cadeaupakket"] = addon("project", [
    "Kaart op stevig papier met jouw boodschap en de persoonlijke QR-code",
    "Uitlegboekje in grote letters: zo werkt het, handige woorden, beginnen",
    "In een doosje dat door de brievenbus past, verzonden binnen twee werkdagen"],
    price_note="inclusief btw en verzending",
    missing_text="Het cadeaupakket bestel je samen met het verteljaar, of later vanuit de app, zodat de kaart de juiste QR-code krijgt.",
    app_label="Naar Vertelschat", fallback_image="gift-card.png")
templates["product.boekexemplaar"] = addon("app_only", [
    "Eerste exemplaar in een pakket €45, elk volgend exemplaar in hetzelfde pakket €39",
    "Gratis verzonden in Nederland en België",
    "Ook na jaren nog na te bestellen, zonder abonnement"],
    howto="<p>Extra exemplaren bestel je vanuit het boek in de app: je kiest daar het aantal en het adres, en betaalt daarna hier.</p>",
    app_label="Naar mijn boek")
templates["product.familie-exemplaar"] = addon("app_only", [
    "Een eigen exemplaar van het boek, 17 × 24 cm, met QR-codes naar de stem",
    "Je betaalt zelf en vult je eigen adres in",
    "Je ziet alleen de omslag en de titel; de verhalen staan in het boek"],
    howto="<p>Je hebt een privélink nodig van degene die het boek maakte. Vraag die link aan je broer, zus of ouder.</p>",
    app_label="", app_link="")
templates["product.dikker-boek"] = addon("app_only", [
    "Voor boeken van 241 tot 320 pagina's: €15 per exemplaar",
    "Voor boeken van 321 tot 400 pagina's: €29 per exemplaar",
    "Je ziet de omvang in de app terwijl het boek groeit"],
    howto="<p>Deze toeslag komt vanzelf in je winkelmand als je een dikker boek bestelt vanuit de app.</p>",
    app_label="Naar mijn boek")

templates["page"] = page({"main": sec("main-page")})
templates["404"] = page({"main": sec("main-404")})
templates["cart"] = page({"main": sec("main-cart")})
templates["collection"] = page({"main": sec("main-collection")})
templates["search"] = page({"main": sec("main-search")})

for name, data in templates.items():
    (T / "templates" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

settings_schema = [
    {"name": "theme_info", "theme_name": "Vertelschat", "theme_version": "1.0.0", "theme_author": "Vertelschat",
     "theme_documentation_url": "https://vertelschat.nl", "theme_support_url": "https://vertelschat.nl/pages/contact"},
    {"name": "Algemeen", "settings": [
        {"type": "textarea", "id": "default_description", "label": "Standaard omschrijving (delen op sociale media)",
         "default": "Een jaar lang vragen via WhatsApp. Je ouders of grootouders antwoorden met een spraakbericht; jij krijgt hun verhalen, hun stem en een boek met QR-codes."},
        {"type": "text", "id": "plausible_domain", "label": "Plausible-domein (cookieloze statistieken, optioneel)"}]}]
(T / "config" / "settings_schema.json").write_text(json.dumps(settings_schema, ensure_ascii=False, indent=2), encoding="utf-8")
(T / "config" / "settings_data.json").write_text(json.dumps({"current": {"default_description": settings_schema[1]["settings"][0]["default"], "plausible_domain": ""}, "presets": {"Default": {}}}, ensure_ascii=False, indent=2), encoding="utf-8")

nl = {
 "general": {"skip_to_content": "Naar de inhoud", "nav_label": "Hoofdmenu"},
 "nav": {"how": "Hoe het werkt", "gift": "Cadeau geven", "examples": "Voorbeelden", "pricing": "Prijzen", "faq": "Vragen", "login": "Inloggen", "menu": "Menu"},
 "hero": {"chat_status": "vragen en verhalen", "audio_code": "Audiocode"},
 "product": {"gift": {"legend": "Is het een cadeau?", "yes": "Ja, ik geef het cadeau", "no": "Nee, het is voor mij of voor ons", "for": "Voor wie?", "for_hint": "(voornaam, voor op de kaart)", "message": "Persoonlijke boodschap", "message_hint": "(niet verplicht)"},
             "quantity": "Aantal", "add_to_cart": "In winkelmand", "sold_out": "Tijdelijk niet beschikbaar"},
 "cart": {"title": "Winkelmand", "empty": "Je winkelmand is leeg.", "continue": "Bekijk het verteljaar", "total": "Totaal", "shipping_note": "Verzending van het boek is gratis in Nederland en België. Geen abonnement.", "update": "Bijwerken", "checkout": "Afrekenen"},
 "footer": {"no_dark_patterns": "Geen abonnement, geen aftelklokken, geen nep-recensies.", "payments": "iDEAL | Wero · Bancontact · creditcard"},
 "legal": {"draft_notice": "Concept, nog niet juridisch gecontroleerd. Laat deze tekst voor publicatie nakijken."},
 "notfound": {"title": "Deze pagina bestaat niet", "text": "Misschien is de link niet helemaal goed overgenomen.", "home": "Naar de voorpagina", "qr_hint": "Kwam je hier via een QR-code uit een boek? Die codes werken via v.vertelschat.nl. Staat er een audiocode bij (bijvoorbeeld A07)? Dan vind je de opname ook in het archief van de familie."},
 "collection": {"empty": "Nog geen producten."},
 "search": {"title": "Zoeken", "label": "Zoekterm", "submit": "Zoeken", "none": "Niets gevonden."},
 "contact": {"thanks": "Dankjewel! We antwoorden binnen één werkdag.", "error": "Controleer de velden en probeer het opnieuw.", "name": "Naam", "email": "E-mailadres", "message": "Bericht", "privacy": "We gebruiken je gegevens alleen om je vraag te beantwoorden.", "send": "Versturen"},
}
en = {
 "general": {"skip_to_content": "Skip to content", "nav_label": "Main menu"},
 "nav": {"how": "How it works", "gift": "Give as a gift", "examples": "Examples", "pricing": "Pricing", "faq": "Questions", "login": "Log in", "menu": "Menu"},
 "hero": {"chat_status": "questions and stories", "audio_code": "Audio code"},
 "product": {"gift": {"legend": "Is it a gift?", "yes": "Yes, it's a gift", "no": "No, it's for me or for us", "for": "For whom?", "for_hint": "(first name, for the card)", "message": "Personal message", "message_hint": "(optional)"},
             "quantity": "Quantity", "add_to_cart": "Add to cart", "sold_out": "Temporarily unavailable"},
 "cart": {"title": "Cart", "empty": "Your cart is empty.", "continue": "See the storytelling year", "total": "Total", "shipping_note": "Free book shipping in the Netherlands and Belgium. No subscription.", "update": "Update", "checkout": "Check out"},
 "footer": {"no_dark_patterns": "No subscription, no countdown timers, no fake reviews.", "payments": "iDEAL | Wero · Bancontact · credit card"},
 "legal": {"draft_notice": "Draft, not yet reviewed by a lawyer. Have this text checked before publishing."},
 "notfound": {"title": "This page does not exist", "text": "The link may not have been copied completely.", "home": "Go to the home page", "qr_hint": "Did you come here from a QR code in a book? Those codes work via v.vertelschat.nl. Is there an audio code next to it (for example A07)? The recording is also in the family's own archive."},
 "collection": {"empty": "No products yet."},
 "search": {"title": "Search", "label": "Search term", "submit": "Search", "none": "Nothing found."},
 "contact": {"thanks": "Thank you! We reply within one working day.", "error": "Please check the fields and try again.", "name": "Name", "email": "Email address", "message": "Message", "privacy": "We only use your details to answer your question.", "send": "Send"},
}
fr = {
 "general": {"skip_to_content": "Aller au contenu", "nav_label": "Menu principal"},
 "nav": {"how": "Comment ça marche", "gift": "Offrir", "examples": "Exemples", "pricing": "Prix", "faq": "Questions", "login": "Connexion", "menu": "Menu"},
 "hero": {"chat_status": "questions et récits", "audio_code": "Code audio"},
 "product": {"gift": {"legend": "C'est un cadeau ?", "yes": "Oui, c'est un cadeau", "no": "Non, c'est pour moi ou pour nous", "for": "Pour qui ?", "for_hint": "(prénom, pour la carte)", "message": "Message personnel", "message_hint": "(facultatif)"},
             "quantity": "Quantité", "add_to_cart": "Ajouter au panier", "sold_out": "Temporairement indisponible"},
 "cart": {"title": "Panier", "empty": "Votre panier est vide.", "continue": "Découvrir l'année des récits", "total": "Total", "shipping_note": "Livraison du livre gratuite aux Pays-Bas et en Belgique. Sans abonnement.", "update": "Mettre à jour", "checkout": "Commander"},
 "footer": {"no_dark_patterns": "Sans abonnement, sans compte à rebours, sans faux avis.", "payments": "iDEAL | Wero · Bancontact · carte de crédit"},
 "legal": {"draft_notice": "Projet, pas encore relu par un juriste. Faites vérifier ce texte avant publication."},
 "notfound": {"title": "Cette page n'existe pas", "text": "Le lien n'a peut-être pas été copié entièrement.", "home": "Retour à l'accueil", "qr_hint": "Vous venez d'un code QR dans un livre ? Ces codes fonctionnent via v.vertelschat.nl. Un code audio (par exemple A07) est indiqué à côté ? L'enregistrement se trouve aussi dans l'archive de la famille."},
 "collection": {"empty": "Pas encore de produits."},
 "search": {"title": "Rechercher", "label": "Terme de recherche", "submit": "Rechercher", "none": "Aucun résultat."},
 "contact": {"thanks": "Merci ! Nous répondons sous un jour ouvrable.", "error": "Vérifiez les champs et réessayez.", "name": "Nom", "email": "Adresse e-mail", "message": "Message", "privacy": "Nous utilisons vos données uniquement pour répondre à votre question.", "send": "Envoyer"},
}
for fn, data in (("nl.default.json", nl), ("en.json", en), ("fr.json", fr)):
    (T / "locales" / fn).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print("templates:", len(templates))
