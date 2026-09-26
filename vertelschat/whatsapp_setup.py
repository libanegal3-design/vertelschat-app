"""One command to finish the WhatsApp Cloud API setup from the server (for example the Render Shell).

    python -m vertelschat.whatsapp_setup                      # check the configuration and show the status
    python -m vertelschat.whatsapp_setup --pin 123456         # register the phone number for the Cloud API
    python -m vertelschat.whatsapp_setup --sjablonen          # submit the message templates

It also subscribes the WhatsApp Business Account to the app, so incoming messages reach the webhook. Every step is
safe to repeat: Meta answers "already done" and the command carries on.
"""
from __future__ import annotations

import argparse
import sys

import httpx

from .config import get_settings
from .copy_nl import TEMPLATES
from .whatsapp import GRAPH_BASE

# Example values Meta needs to review each template (the real values are filled in per message).
TEMPLATE_EXAMPLES = {
    "vt_vraag_kort_v1": ["Marijke", "Wat was je allereerste baantje?"],
    "vt_afsluiting_v1": ["Marijke"],
}


def template_payload(name: str) -> dict:
    return {
        "name": name, "language": get_settings().whatsapp_template_lang, "category": "UTILITY",
        "parameter_format": "POSITIONAL",
        "components": [{"type": "BODY", "text": TEMPLATES[name], "example": {"body_text": [TEMPLATE_EXAMPLES[name]]}}],
    }


class Setup:
    def __init__(self, client: httpx.Client | None = None, out=print) -> None:
        s = get_settings()
        self.s = s
        self.base = f"{GRAPH_BASE}/{s.whatsapp_api_version}"
        self.client = client or httpx.Client(timeout=30.0)
        self.out = out

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.whatsapp_token}"}

    def _call(self, method: str, path: str, **kw) -> tuple[bool, dict]:
        r = self.client.request(method, f"{self.base}/{path}", headers=self.headers, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        return r.status_code < 400, data

    def check_config(self) -> bool:
        missing = [k for k, v in (("WHATSAPP_ACCESS_TOKEN", self.s.whatsapp_token),
                                  ("WHATSAPP_PHONE_NUMBER_ID", self.s.whatsapp_phone_number_id),
                                  ("WHATSAPP_WABA_ID", self.s.whatsapp_waba_id),
                                  ("WHATSAPP_APP_SECRET", self.s.whatsapp_app_secret),
                                  ("WHATSAPP_VERIFY_TOKEN", self.s.whatsapp_verify_token)) if not v or v == "later"]
        if missing:
            self.out("Nog niet ingevuld in Render: " + ", ".join(missing))
            return False
        if self.s.whatsapp_backend != "cloud":
            self.out("Let op: WHATSAPP_BACKEND staat nog niet op 'cloud' (nu: %s). Berichten gaan dan nergens heen."
                     % self.s.whatsapp_backend)
        return True

    def number_status(self) -> bool:
        ok, d = self._call("GET", self.s.whatsapp_phone_number_id, params={
            "fields": "display_phone_number,verified_name,name_status,code_verification_status,quality_rating"})
        if not ok:
            self.out("Telefoonnummer niet gevonden of sleutel ongeldig: " + d.get("error", {}).get("message", "?"))
            return False
        self.out(f"Nummer: {d.get('display_phone_number')}  naam: {d.get('verified_name')} "
                 f"(naam-status: {d.get('name_status', '?')}, kwaliteit: {d.get('quality_rating', '?')})")
        return True

    def register(self, pin: str) -> bool:
        ok, d = self._call("POST", f"{self.s.whatsapp_phone_number_id}/register",
                           json={"messaging_product": "whatsapp", "pin": pin})
        self.out("Nummer geregistreerd voor de Cloud API." if ok else
                 "Registreren mislukt: " + d.get("error", {}).get("message", "?"))
        return ok

    def subscribe(self) -> bool:
        ok, d = self._call("POST", f"{self.s.whatsapp_waba_id}/subscribed_apps")
        self.out("WhatsApp-account gekoppeld aan de app (inkomende berichten komen binnen)." if ok else
                 "Koppelen mislukt: " + d.get("error", {}).get("message", "?"))
        return ok

    def existing_templates(self) -> set[str]:
        ok, d = self._call("GET", f"{self.s.whatsapp_waba_id}/message_templates", params={"fields": "name", "limit": 200})
        return {t.get("name") for t in d.get("data", [])} if ok else set()

    def submit_templates(self) -> bool:
        all_ok = True
        existing = self.existing_templates()
        for name in TEMPLATES:
            if name in existing:
                self.out(f"Sjabloon {name} is al ingediend.")
                continue
            ok, d = self._call("POST", f"{self.s.whatsapp_waba_id}/message_templates", json=template_payload(name))
            msg = d.get("error", {}).get("error_user_msg") or d.get("error", {}).get("message", "")
            if ok:
                self.out(f"Sjabloon {name} ingediend (status: {d.get('status', 'PENDING')}).")
            elif "already exists" in msg.lower() or "bestaat al" in msg.lower():
                self.out(f"Sjabloon {name} bestond al.")
            else:
                all_ok = False
                self.out(f"Sjabloon {name} niet ingediend: {msg or '?'}")
        return all_ok

    def template_status(self) -> None:
        ok, d = self._call("GET", f"{self.s.whatsapp_waba_id}/message_templates",
                           params={"fields": "name,status,category,language", "limit": 50})
        if not ok:
            self.out("Sjablonen opvragen mislukt: " + d.get("error", {}).get("message", "?"))
            return
        ours = {t["name"]: t for t in d.get("data", []) if t.get("name") in TEMPLATES}
        for name in TEMPLATES:
            t = ours.get(name)
            self.out(f"  {name}: " + (f"{t.get('status')} ({t.get('category')}, {t.get('language')})" if t else
                                      "nog niet ingediend"))
            if t and t.get("category") == "MARKETING":
                self.out("    Meta ziet dit als marketing. Vraag in WhatsApp Manager een nieuwe beoordeling aan (zie "
                         "WHATSAPP-KOPPELEN.md), of laat het mij weten.")
        unused = sorted(t["name"] for t in d.get("data", []) if t.get("name", "").startswith("vt_")
                        and t["name"] not in TEMPLATES)
        if unused:
            self.out("Niet meer gebruikt (mag je verwijderen in WhatsApp Manager): " + ", ".join(unused))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WhatsApp Cloud API afronden voor Vertelschat")
    p.add_argument("--pin", help="De 6-cijferige pincode (tweestapsverificatie) om het nummer te registreren")
    p.add_argument("--sjablonen", action="store_true", help="Dien de berichtsjablonen in bij Meta")
    args = p.parse_args(argv)
    st = Setup()
    if not st.check_config() or not st.number_status():
        return 1
    ok = True
    if args.pin:
        ok &= st.register(args.pin.strip())
    ok &= st.subscribe()
    if args.sjablonen:
        ok &= st.submit_templates()
    st.out("Sjablonen bij Meta:")
    st.template_status()
    st.out(f"Webhook-URL voor Meta: {st.s.base_url}/webhooks/whatsapp")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
