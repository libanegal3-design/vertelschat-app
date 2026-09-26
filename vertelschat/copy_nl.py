"""Every WhatsApp text the storyteller can receive, in one place for review by a native editor.

Tone: warm, short, adult. No exclamation-mark overload, no emoji walls, no instructions every week.
nl-BE uses 'Dag' as greeting; templates use the neutral 'Hallo' because one approved text serves both."""
from __future__ import annotations


def greet(locale: str) -> str:
    return "Dag" if locale == "nl-BE" else "Hoi"


def cadence_phrase(cadence_days: int) -> str:
    return {7: "elke week", 14: "om de week", 3: "twee keer per week", 30: "elke maand"}.get(
        cadence_days, f"elke {cadence_days} dagen")


def welcome(locale: str, name: str, organizer: str, cadence_days: int, privacy_url: str, is_gift: bool) -> str:
    who = f"{organizer} heeft Vertelschat voor je geregeld. " if (organizer and is_gift) else "Welkom bij Vertelschat. "
    return (f"{greet(locale)} {name}, wat fijn dat je er bent.\n\n"
            f"{who}Ik stuur je hier {cadence_phrase(cadence_days)} een vraag over vroeger. Je antwoordt gewoon "
            "met een spraakbericht, wanneer het jou uitkomt. Lang of kort, alles is goed.\n\n"
            "Je verhalen en je stem worden bewaard voor je familie. Stoppen kan altijd: stuur dan STOP. "
            f"Hoe we met je gegevens omgaan: {privacy_url}\n\nDoe je mee?")


BTN_YES = "Ja, ik doe mee"
BTN_NOT_ME = "Dit ben ik niet"


def consent_thanks(name: str) -> str:
    return (f"Dankjewel, {name}. Hier komt je eerste vraag. Neem gerust de tijd: houd het microfoontje "
            "ingedrukt en vertel maar. Meerdere berichtjes achter elkaar mag ook.")


NOT_ME = ("Geen probleem, dan heb ik dit nummer weer losgekoppeld. De uitnodiging is bedoeld voor degene die de "
          "verhalen gaat vertellen. Stuur het bericht gerust aan die persoon door.")


def question_session(locale: str, name: str, question: str, asker: str = "", first_weeks: bool = False,
                     extra: bool = False) -> str:
    if extra:
        lead = "Hier is nog een vraag voor je:"
    elif asker:
        lead = f"{greet(locale)} {name}, {asker} vroeg zich af:"
    else:
        lead = f"{greet(locale)} {name}, hier is je vraag van deze week:"
    text = f"{lead}\n\n*{question}*"
    if first_weeks:
        text += "\n\nVertel het gerust in een spraakbericht, wanneer het jou uitkomt."
    return text


# Templates as submitted to Meta (category: utility). Keep in sync with whatsapp/templates.json.
TEMPLATES = {
    "vt_vraag_kort_v1": "Hallo {{1}}, een nieuwe vraag voor je:\n\n*{{2}}*\n\nAntwoorden kan met een spraakbericht.",
    "vt_afsluiting_v1": "Hallo {{1}}, dit was de laatste vraag van dit verteljaar. Dankjewel voor al je verhalen. Ze blijven bewaard voor je familie, ook na vandaag.",
}


def thanks(name: str, n: int) -> str:
    if n == 0:
        return f"Dankjewel, {name}. Je verhaal is goed aangekomen en veilig bewaard."
    return f"Mooi verteld, {name}. Ook dit verhaal is bewaard."


def after_period(name: str) -> str:
    return f"Dankjewel, {name}. Je bericht is goed aangekomen en bewaard voor je familie."


def unknown_sender(support: str) -> str:
    return ("Hallo! Dit is het WhatsApp-nummer van Vertelschat. Ik weet nog niet bij welk familieverhaal dit "
            "nummer hoort.\n\nHeb je een uitnodiging gekregen? Tik dan op de link in dat bericht of scan de "
            "QR-code op de kaart, dan koppel ik je meteen. Je bericht bewaar ik ondertussen 30 dagen.\n\n"
            f"Vragen? Mail gerust naar {support}.")


UNKNOWN_CODE = ("Die code ken ik helaas niet. Kijk nog even of hij goed is overgenomen (hij begint met VT-), of "
                "vraag degene die je heeft uitgenodigd om het bericht opnieuw te sturen.")


def code_taken(organizer: str) -> str:
    return ("Deze uitnodiging is al gekoppeld aan een ander telefoonnummer. Is dat niet de bedoeling? "
            f"Vraag {organizer or 'de familie'} dan om het nummer te wijzigen in Vertelschat.")


def consent_reminder(name: str) -> str:
    return (f"Dankjewel voor je bericht, {name}. Ik heb het veilig bewaard. Voordat ik je verhalen verder "
            "verwerk, wil ik graag even zeker weten dat je meedoet.")


TEXT_HINT = ("Fijn dat je schrijft. Je mag ook gewoon inspreken: houd het microfoontje ingedrukt en vertel maar. "
             "Een getypt antwoord is natuurlijk ook goed.")


def stop_confirm() -> str:
    return ("Je bent afgemeld. Je krijgt geen vragen meer van Vertelschat. Alles wat je al vertelde, blijft bewaard "
            "voor je familie.\n\nWil je later toch weer meedoen? Stuur dan START.")


def start_confirm(name: str) -> str:
    return f"Welkom terug, {name}. De vragen komen weer, in het gewone ritme."


def pause_confirm(date_text: str) -> str:
    return f"Prima, ik pauzeer de vragen tot {date_text}. Wil je eerder weer? Stuur dan VRAAG."


MORE_LIMIT = "Voor vandaag heb je al een paar extra vragen gehad. Morgen kan het weer."
MORE_NONE = "Er staat op dit moment geen nieuwe vraag klaar. De familie kan nieuwe vragen toevoegen."


def help_text(support: str) -> str:
    return ("Zo werkt Vertelschat:\n\n"
            "\u2022 Je krijgt regelmatig een vraag.\n"
            "\u2022 Antwoorden doe je met een spraakbericht: houd het microfoontje ingedrukt. Typen mag ook.\n"
            "\u2022 Iets anders vertellen dan de vraag? Dat mag altijd.\n\n"
            "Handige woorden:\nVRAAG: stuur me een nieuwe vraag\nPAUZE: vier weken geen vragen\nSTOP: afmelden\n\n"
            f"Vragen? Mail {support}.")


UNSUPPORTED = "Dat bericht kon ik helaas niet openen. Wil je het nog een keer als gewoon spraakbericht sturen?"
CORRUPT_RESEND = ("Je laatste spraakbericht kwam helaas niet goed door. Wil je het nog een keer inspreken? "
                  "Sorry voor de moeite.")
ASSIGN_QUESTION = "Dankjewel! Voor wie is dit verhaal bedoeld?"


def assign_done(title: str) -> str:
    return f"Dankjewel, het verhaal staat nu bij \u201c{title}\u201d."


def farewell_session(locale: str, name: str) -> str:
    return (f"{greet(locale)} {name}, dit was de laatste vraag van dit verteljaar. Dankjewel voor al je "
            "verhalen. Ze blijven bewaard voor je familie, ook na vandaag.")
