"""Transcription and story editing.

Principles (enforced in code, not only in prompts):
- the transcript and the original audio are stored separately and never altered by the editor;
- the story editor may only remove filler words, repair grammar and punctuation and add paragraphs;
- every draft passes a fabrication check: numbers and proper names that do not occur in the
  transcript (or in names the family supplied) are rejected; the local editor is the safe fallback;
- AI output after a human edit is kept as a suggestion and never overwrites the family's text;
- follow-up questions are suggestions that the family approves before anything is sent.
Providers: Mistral Voxtral (EU) for speech-to-text, Anthropic Claude (Sonnet) for editing. Both are
configured without training on customer data; see architecture/privacy.md."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import DATA_DIR, get_settings

log = logging.getLogger("vertelschat.ai")


# =========================================================================== transcription
class TranscriptionError(RuntimeError):
    def __init__(self, message: str, *, retriable: bool = False, manual: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.manual = manual


@dataclass
class TranscriptionResult:
    text: str
    provider: str
    model: str = ""
    language: str = "nl"
    segments: list = field(default_factory=list)


class FixtureTranscriber:
    """Development/demo transcriber: returns the known transcript of generated demo voice notes."""
    name = "fixture"
    max_chunk_seconds = None

    def __init__(self) -> None:
        path = DATA_DIR / "demo_audio" / "fixtures.json"
        self.fixtures = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def transcribe(self, path: Path, context_terms: list[str]) -> TranscriptionResult:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        hit = self.fixtures.get(digest)
        if not hit:
            raise TranscriptionError("Geen transcriptieprovider ingesteld (demo-modus kent dit bestand niet).",
                                     manual=True)
        return TranscriptionResult(text=hit["text"], provider="fixture", model="demo")


class MistralTranscriber:
    name = "mistral"
    max_chunk_seconds = 600
    url = "https://api.mistral.ai/v1/audio/transcriptions"

    def __init__(self) -> None:
        s = get_settings()
        self.key = s.mistral_api_key
        self.model = s.transcription_model

    def transcribe(self, path: Path, context_terms: list[str]) -> TranscriptionResult:
        if not self.key:
            raise TranscriptionError("MISTRAL_API_KEY ontbreekt", retriable=True)
        data: dict = {"model": self.model, "language": "nl", "timestamp_granularities": "segment"}
        terms = [t for t in context_terms if t][:50]
        if terms:
            data["context_bias"] = terms  # names and places the family entered, improves spelling
        try:
            with open(path, "rb") as fh:
                r = httpx.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, data=data,
                               files={"file": (Path(path).name, fh, "audio/ogg")}, timeout=300)
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Mistral onbereikbaar: {exc}", retriable=True) from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise TranscriptionError(f"Mistral tijdelijk niet beschikbaar ({r.status_code})", retriable=True)
        if r.status_code >= 400:
            raise TranscriptionError(f"Mistral weigerde het bestand ({r.status_code}): {r.text[:200]}")
        body = r.json()
        return TranscriptionResult(text=(body.get("text") or "").strip(), provider="mistral", model=self.model,
                                   language=body.get("language") or "nl", segments=body.get("segments") or [])


class ManualTranscriber:
    name = "manual"
    max_chunk_seconds = None

    def transcribe(self, path: Path, context_terms: list[str]) -> TranscriptionResult:
        raise TranscriptionError("Automatische transcriptie staat uit; typ het verhaal zelf uit.", manual=True)


def get_transcriber():
    backend = get_settings().transcription_backend
    if backend == "mistral":
        return MistralTranscriber()
    if backend == "fixture":
        return FixtureTranscriber()
    return ManualTranscriber()


# =========================================================================== fabrication guard
_NUM = re.compile(r"\b\d{2,4}\b")
_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+")


def _norm(word: str) -> str:
    table = str.maketrans("àáâäèéêëìíîïòóôöùúûüÀÁÂÄÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜ’", "aaaaeeeeiiiioooouuuuaaaaeeeeiiiioooouuuu'")
    return word.translate(table).lower().strip("'-")


def proper_nouns(text: str) -> set[str]:
    """Capitalised words that are not the first word of a sentence."""
    found: set[str] = set()
    for sentence in re.split(r"(?<=[.!?:;\n\u201c\u201d\"])\s+", text):
        words = _WORD.findall(sentence)
        for w in words[1:]:
            if w[:1].isupper() and len(w) > 1:
                found.add(w)
    return found


def check_fabrication(output: str, source: str, allowed: list[str] | None = None) -> list[str]:
    allowed_text = " ".join(allowed or [])
    src_words = {_norm(w) for w in _WORD.findall(source + " " + allowed_text)}
    src_nums = set(_NUM.findall(source + " " + allowed_text))
    problems = {n for n in _NUM.findall(output) if n not in src_nums}
    for noun in proper_nouns(output):
        if _norm(noun) not in src_words:
            problems.add(noun)
    return sorted(problems)


# =========================================================================== story editing
@dataclass
class ComposeInput:
    question: str
    parts: list[str]
    storyteller_name: str
    locale: str = "nl-NL"
    title_hint: str = ""
    known_names: list[str] = field(default_factory=list)


@dataclass
class ComposeResult:
    title: str
    body: str
    followups: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    provider: str = "local"
    model: str = ""


class ComposeError(RuntimeError):
    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


FILLERS = re.compile(r",?\s*(?<![\w-])(?:e+h+m*|e+hm+|u+h+m*|u+m+|hm+)(?![\w-])\s*[,.]?", re.IGNORECASE)
LEADING_FLUFF = re.compile(r"^(?:(?:nou|ja|nee|goh|tja|oké|ok)[,.]?\s+)+", re.IGNORECASE)
DOUBLE_WORD = re.compile(r"\b(\w+)(\s*,?\s+\1\b)+", re.IGNORECASE)


def clean_text(text: str) -> str:
    t = FILLERS.sub(" ", text)
    t = DOUBLE_WORD.sub(r"\1", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r",\s*,+", ",", t)
    t = re.sub(r"([.!?])\s*,", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    sentences = re.split(r"(?<=[.!?])\s+", t)
    out = []
    for s in sentences:
        s = LEADING_FLUFF.sub("", s.strip()).strip(" ,")
        if not s:
            continue
        s = s[0].upper() + s[1:]
        if s[-1] not in ".!?":
            s += "."
        out.append(s)
    return " ".join(out)


def paragraphs(text: str, per: int = 4) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks = [" ".join(sentences[i:i + per]) for i in range(0, len(sentences), per)]
    return "\n\n".join(c for c in chunks if c)


_PRON = [(r"\bjouw\b", "mijn"), (r"\bjij\b", "ik"), (r"\bje\b", "ik")]


def title_from_question(question: str) -> str:
    """First-person title for custom questions; falls back to the question itself (a common memoir convention)."""
    q = question.strip()
    core = q.rstrip("?").strip()
    if not core:
        return "Een eigen verhaal"
    if re.search(r"\bjullie\b", core, re.IGNORECASE):
        return q
    m = re.match(r"^(?:Wat|Wie)\s+(?:was|waren|is|zijn)\s+((?:de|het|een|je|jouw)\s.+)$", core, re.IGNORECASE)
    if m:  # "Wat was je eerste fiets?" -> "Mijn eerste fiets"
        title = re.sub(r"^(?:je|jouw)\s", "mijn ", m.group(1), flags=re.IGNORECASE)
    else:
        m = re.match(r"^(Hoe|Waar|Wanneer|Waarom|Wat)\s+(\w+)\s+(?:je|jij)\s+(.+)$", core, re.IGNORECASE)
        if not m:
            return q
        qw, verb, rest = m.groups()
        words = rest.split()
        cluster = 0  # trailing infinitive cluster, e.g. "leren kennen": the finite verb goes in front of it
        while cluster < min(2, len(words) - 1) and words[len(words) - 1 - cluster].endswith("en"):
            cluster += 1
        if cluster:
            rest = " ".join(words[:len(words) - cluster] + [verb] + words[len(words) - cluster:])
        else:
            rest = f"{rest} {verb}"
        title = f"{qw.capitalize()} ik {rest}"
    for pat, rep in _PRON:
        title = re.sub(pat, rep, title, flags=re.IGNORECASE)
    title = title[0].upper() + title[1:]
    return title if len(title) <= 70 else title[:67].rsplit(" ", 1)[0] + "\u2026"


_FOLLOWUP_PATTERNS = [
    (re.compile(r"verhuis\w*\s+(?:(?:we|wij|ik|hij|zij|ze)\s+)?naar ([A-Z][a-zé]+)"),
     "Je vertelde over de verhuizing naar {0}. Hoe was het om daar opnieuw te beginnen?"),
    (re.compile(r"\b(?:mijn|onze) (broer|opa|vader|oom) ([A-Z][a-z]+)"),
     "Wil je nog eens vertellen over {1}? Wat voor iemand was hij?"),
    (re.compile(r"\b(?:mijn|onze) (zus|oma|moeder|tante) ([A-Z][a-z]+)"),
     "Wil je nog eens vertellen over {1}? Wat voor iemand was zij?"),
    (re.compile(r"werkte (?:bij|in|op) (?:de |het |een )?([A-Z][\w-]+)"),
     "Hoe zag een gewone werkdag bij {0} eruit?"),
]


def local_followups(text: str) -> list[str]:
    out: list[str] = []
    for pattern, template in _FOLLOWUP_PATTERNS:
        m = pattern.search(text)
        if m:
            q = template.format(*m.groups())
            if q not in out:
                out.append(q)
        if len(out) == 2:
            break
    return out


class LocalComposer:
    """Deterministic editor without an LLM: removes fillers and repetitions, fixes capitals and punctuation,
    adds paragraphs. It cannot invent anything, which makes it the safe fallback."""
    name = "local"

    def compose(self, data: ComposeInput, strict_words: list[str] | None = None) -> ComposeResult:
        cleaned_parts = [paragraphs(clean_text(p)) for p in data.parts if p.strip()]
        body = "\n\n".join(cleaned_parts)
        title = data.title_hint or (title_from_question(data.question) if data.question else "Een eigen verhaal")
        return ComposeResult(title=title, body=body, followups=local_followups(" ".join(data.parts)),
                             provider="local", model="regels-v1")


SYSTEM_PROMPT = """Je bent een zorgvuldige eindredacteur voor een familieboek. Je krijgt de letterlijke transcriptie van \
een of meer spraakberichten waarin iemand uit eigen leven vertelt. Maak daar een goed leesbaar verhaal van in de ik-vorm, \
in de woorden van de verteller.

Regels, zonder uitzondering:
1. Gebruik alleen wat in de transcriptie staat. Voeg geen feiten, namen, plaatsen, jaartallen, gevoelens, dialogen, \
beschrijvingen of conclusies toe.
2. Behoud de stem van de verteller: eigen woorden, uitdrukkingen, dialect en zinsbouw blijven zoveel mogelijk staan.
3. Je mag: stopwoorden en herhalingen weghalen, valse starts verwijderen, interpunctie en hoofdletters herstellen, \
zinnen licht glad strijken en alinea's maken.
4. Laat geen concrete details weg. Vat niet samen.
5. Als iets onverstaanbaar of onduidelijk is, gok niet: laat het weg en noem het onder "unclear".
6. Titel: kort (maximaal 8 woorden), bij voorkeur met woorden van de verteller zelf, geen clickbait.
7. Vervolgvragen: maximaal 2, warm en open geformuleerd met "je", over dingen die de verteller zelf noemde. Geen \
vragen over pijnlijke onderwerpen tenzij de verteller die zelf aansnijdt.
8. Schrijf in het Nederlands{flemish}.

Antwoord uitsluitend met JSON: {{"title": "...", "body": "...", "unclear": ["..."], "followups": ["..."]}}. \
Gebruik in "body" lege regels tussen alinea's."""


class AnthropicComposer:
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(self) -> None:
        s = get_settings()
        self.key = s.anthropic_api_key
        self.model = s.story_model

    def compose(self, data: ComposeInput, strict_words: list[str] | None = None) -> ComposeResult:
        if not self.key:
            raise ComposeError("ANTHROPIC_API_KEY ontbreekt", retriable=True)
        flemish = " zoals in Vlaanderen gesproken; behoud Vlaamse woorden" if data.locale == "nl-BE" else ""
        parts = "\n\n".join(f"<deel nummer=\"{i + 1}\">\n{p}\n</deel>" for i, p in enumerate(data.parts))
        user = (f"<vraag>{data.question or '(geen vraag: de verteller begon zelf)'}</vraag>\n"
                f"<verteller>{data.storyteller_name}</verteller>\n<transcriptie>\n{parts}\n</transcriptie>")
        if strict_words:
            user += ("\n\nLet op: een eerdere versie bevatte woorden die niet in de transcriptie staan: "
                     + ", ".join(strict_words) + ". Gebruik die niet.")
        body = {"model": self.model, "max_tokens": 4000, "system": SYSTEM_PROMPT.format(flemish=flemish),
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            r = httpx.post(self.url, json=body, headers=headers, timeout=120)
        except httpx.HTTPError as exc:
            raise ComposeError(f"Anthropic onbereikbaar: {exc}", retriable=True) from exc
        if r.status_code in (429, 500, 502, 503, 504, 529):
            raise ComposeError(f"Anthropic tijdelijk niet beschikbaar ({r.status_code})", retriable=True)
        if r.status_code >= 400:
            raise ComposeError(f"Anthropic-fout {r.status_code}: {r.text[:200]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ComposeError("Antwoord was geen geldige JSON") from exc
        return ComposeResult(title=str(parsed.get("title", "")).strip()[:120], body=str(parsed.get("body", "")).strip(),
                             followups=[str(q) for q in parsed.get("followups", [])][:2],
                             unclear=[str(u) for u in parsed.get("unclear", [])][:10],
                             provider="anthropic", model=self.model)


def get_composer():
    return AnthropicComposer() if get_settings().story_backend == "anthropic" else LocalComposer()


def compose_checked(data: ComposeInput) -> tuple[ComposeResult, list[str]]:
    """Compose with the configured editor, verify, retry strictly once, then fall back to the local editor.
    Returns (result, review_notes)."""
    composer = get_composer()
    source = " ".join(data.parts) + " " + data.question
    allowed = data.known_names + [data.storyteller_name]
    notes: list[str] = []
    result = composer.compose(data)
    problems = check_fabrication(result.title + "\n" + result.body, source, allowed)
    if problems and composer.name != "local":
        result = composer.compose(data, strict_words=problems)
        problems = check_fabrication(result.title + "\n" + result.body, source, allowed)
    if problems:
        log.info("fabrication guard rejected draft (%s); using local editor", ", ".join(problems))
        result = LocalComposer().compose(data)
        notes.append("De automatische redactie bevatte woorden die niet in de opname voorkomen ("
                     + ", ".join(problems[:5]) + "). We tonen daarom een eenvoudiger bewerking. Controleer de tekst.")
    result.followups = [q for q in result.followups
                        if not check_fabrication(q, source + " " + " ".join(allowed), allowed)][:2]
    if result.unclear:
        notes.append("Onduidelijk in de opname: " + "; ".join(result.unclear[:3]))
    return result, notes
