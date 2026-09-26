"""The WhatsApp setup command talks to Meta's Graph API correctly and is safe to repeat."""
import json
from types import SimpleNamespace

import httpx

from vertelschat.copy_nl import TEMPLATES
from vertelschat.whatsapp_setup import Setup


def make_setup(handler):
    lines = []
    st = Setup(client=httpx.Client(transport=httpx.MockTransport(handler)), out=lines.append)
    st.s = SimpleNamespace(whatsapp_token="tok", whatsapp_phone_number_id="111", whatsapp_waba_id="222",
                           whatsapp_app_secret="sec", whatsapp_verify_token="ver", whatsapp_backend="cloud",
                           base_url="https://app.vertelschat.nl")
    st.base = "https://graph.facebook.com/v25.0"
    return st, lines


def test_setup_registers_subscribes_and_submits_templates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body, request.headers.get("authorization")))
        path = request.url.path
        if request.method == "GET" and path.endswith("/111"):
            return httpx.Response(200, json={"display_phone_number": "+31 6 1234 5678", "verified_name": "Vertelschat"})
        if path.endswith("/111/register") or path.endswith("/222/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and path.endswith("/222/message_templates"):
            if body["name"] == "vt_vraag_klaar_v1":  # submitted before: Meta refuses a duplicate
                return httpx.Response(400, json={"error": {"message": "Invalid parameter",
                                                           "error_user_msg": "Content in this language already exists"}})
            return httpx.Response(200, json={"id": "9", "status": "PENDING", "category": "UTILITY"})
        if request.method == "GET" and path.endswith("/222/message_templates"):
            if request.url.params.get("fields") == "name":  # before submitting: nothing exists yet
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": [{"name": n, "status": "APPROVED", "category": "UTILITY",
                                                       "language": "nl"} for n in TEMPLATES]})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    st, lines = make_setup(handler)
    assert st.check_config() and st.number_status()
    assert st.register("123456") and st.subscribe() and st.submit_templates()
    st.template_status()
    posted = [c for c in calls if c[1].endswith("/message_templates") and c[0] == "POST"]
    assert len(posted) == len(TEMPLATES)
    assert all(c[2]["category"] == "UTILITY" and c[2]["language"] == "nl" for c in posted)
    assert {c[2]["name"]: c[2]["components"][0]["text"] for c in posted} == TEMPLATES
    klaar = next(c[2] for c in posted if c[2]["name"] == "vt_vraag_klaar_v1")
    assert klaar["components"][1] == {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Laat de vraag zien"}]}
    # Meta refuses templates that start or end with a variable (formatting marks don't count)
    for text in TEMPLATES.values():
        stripped = text.strip().strip("*_~ ")
        assert not stripped.endswith("}}") and not stripped.startswith("{{")
    reg = next(c for c in calls if c[1].endswith("/register"))
    assert reg[2] == {"messaging_product": "whatsapp", "pin": "123456"} and reg[3] == "Bearer tok"
    text = "\n".join(lines)
    assert "Vertelschat" in text and "bestond al" in text and "APPROVED" in text


def test_setup_stops_when_values_are_still_placeholders():
    st, lines = make_setup(lambda r: httpx.Response(500))
    st.s.whatsapp_token = "later"
    assert st.check_config() is False and "WHATSAPP_ACCESS_TOKEN" in lines[0]


def test_no_template_asks_the_question_itself():
    # Meta classified every template that asked a question as marketing; questions are announced instead.
    assert set(TEMPLATES) == {"vt_vraag_klaar_v1", "vt_afsluiting_v1"}
    import inspect
    from vertelschat import handlers
    src = inspect.getsource(handlers)
    for old in ("vt_vraag_familie_v1", '"vt_vraag_v1"', "vt_vraag_kort_v1"):
        assert old not in src
