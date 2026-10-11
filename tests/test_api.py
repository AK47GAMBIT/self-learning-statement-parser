import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import WEB, create_app
from app.pipeline import Pipeline
from app.registry import Registry
from app.store import Store
from samples.generate import generate_all, render_pdf
from samples.next_month import abc_may

CONFIGS = Path(__file__).parent.parent / "configs"
HOSTILE_SUBJECT = "Your April PMS statement <script>alert(1)</script>"
TOKEN = "test-token-0123456789abcdef"
AUTH = {"X-Review-Token": TOKEN}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, prompt):
        return self.replies.pop(0)       # a second call would fail the test


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp("api")
    generate_all(root)
    extra = root / "extra"
    extra.mkdir()
    render_pdf("abc_pms", abc_may(), extra / "may.pdf")
    render_pdf("abc_pms", abc_may(tamper=True), extra / "bad.pdf")
    (extra / "junk.pdf").write_text("not a pdf")
    return root


def build(files):
    store, registry = Store(), Registry()
    pipe = Pipeline(store, registry, llm=FakeLLM([(CONFIGS / "abc_pms.json").read_text()]),
                    passwords=[])
    pipe.process_file(files / "pdfs" / "abc_pms.pdf", source="email:<s@test>")   # learned
    pipe.process_file(files / "extra" / "may.pdf")                                # parsed_ok
    pipe.process_file(files / "extra" / "bad.pdf")                                # needs_review
    pipe.process_file(files / "pdfs" / "xyz_wealth.pdf")                          # locked
    pipe.process_file(files / "extra" / "junk.pdf")                               # failed
    store.record_email("<s@test>", HOSTILE_SUBJECT, "ops@abc.example",
                       "2026-05-11T09:00:00+05:30", "processed",
                       json.dumps([{"filename": "abc_pms.pdf", "score": 5, "accepted": True,
                                    "reasons": ["subject mentions 'pms' (+2)"],
                                    "status": "parsed_ok", "used_llm": True}]))
    store.record_email("<p@test>", "Lunch?", "friend@example.com", None, "no_pdf", "[]")
    return store, registry


@pytest.fixture
def client(files):
    store, registry = build(files)
    return TestClient(create_app(store, registry, TOKEN), headers=AUTH)


@pytest.fixture
def anon(files):
    store, registry = build(files)
    return TestClient(create_app(store, registry, TOKEN))


def listing(client, **params):
    return client.get("/api/documents", params={"limit": 100, **params}).json()


def docs(client, **params):
    return {d["filename"]: d for d in listing(client, **params)["items"]}


def detail(client, filename):
    return client.get(f"/api/documents/{docs(client)[filename]['id']}").json()


def review(client, filename, decision, note=""):
    doc_id = docs(client)[filename]["id"]
    return client.post(f"/api/documents/{doc_id}/review", json={"decision": decision, "note": note})


# ---------------------------------------------------------------- access token
@pytest.mark.parametrize("path", ["/api/summary", "/api/documents", "/api/documents/1",
                                  "/api/emails", "/api/formats", "/api/formats/1"])
def test_api_needs_the_token(anon, path):
    assert anon.get(path).status_code == 401
    assert anon.get(path, headers={"X-Review-Token": "wrong-token-0123456789"}).status_code == 401


def test_review_needs_the_token(anon):
    assert anon.post("/api/documents/1/review", json={"decision": "approved"}).status_code == 401


def test_page_and_static_files_are_public(anon):
    assert anon.get("/").status_code == 200
    assert anon.get("/static/app.js").status_code == 200


def test_short_token_is_refused():
    with pytest.raises(ValueError):
        create_app(Store(), Registry(), "short")


# ---------------------------------------------------------------- reading
def test_summary(client):
    s = client.get("/api/summary").json()
    assert s["total"] == 5 and s["needs_attention"] == 3
    assert s["documents"] == {"parsed_ok": 2, "needs_review": 1,
                              "password_required": 1, "failed": 1}
    assert s["reviewed"] == {"approved": 0, "rejected": 0}
    assert s["llm_documents"] == 1 and s["formats"] == 1
    assert s["emails"] == {"processed": 1, "no_pdf": 1}


def test_attention_filter(client):
    got = docs(client, attention="true")
    assert set(got) == {"bad.pdf", "xyz_wealth.pdf", "junk.pdf"}
    assert all(d["needs_attention"] for d in got.values())


def test_status_filter(client):
    assert set(docs(client, status="parsed_ok")) == {"abc_pms.pdf", "may.pdf"}


def test_unknown_status_is_rejected(client):
    assert client.get("/api/documents", params={"status": "bogus"}).status_code == 422


def test_list_rows_are_compact(client):
    d = docs(client)["abc_pms.pdf"]
    assert "statement" not in d and "statement_json" not in d
    assert d["closing_value"] == "1330000.00" and d["currency"] == "INR"
    assert d["review"] is None
    assert d["used_llm"] is True and docs(client)["may.pdf"]["used_llm"] is False


def test_pagination(client):
    first = client.get("/api/documents", params={"limit": 2, "offset": 0}).json()
    last = client.get("/api/documents", params={"limit": 2, "offset": 4}).json()
    assert first["total"] == 5 and len(first["items"]) == 2 and len(last["items"]) == 1
    ids = [d["id"] for d in first["items"]]
    assert ids == sorted(ids, reverse=True)                     # newest first


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1000}, {"offset": -1}])
def test_bad_paging_is_rejected(client, params):
    assert client.get("/api/documents", params=params).status_code == 422


def test_search(client):
    assert set(docs(client, q="xyz")) == {"xyz_wealth.pdf"}
    assert set(docs(client, q="ABC")) == {"abc_pms.pdf", "may.pdf", "bad.pdf"}
    assert set(docs(client, q="junk")) == {"junk.pdf"}
    assert listing(client, q="no-such-thing")["total"] == 0


def test_search_treats_wildcards_literally(client):
    assert listing(client, q="%")["total"] == 0           # no stored text contains a percent sign
    # '_' matches only a real underscore. If it were a wildcard, junk.pdf would match too.
    assert "junk.pdf" not in docs(client, q="_")
    assert len(docs(client, q="_")) == 4
    # 'a_c' would match "abc" if '_' were a wildcard; literally, nothing contains "a_c".
    assert listing(client, q="a_c")["total"] == 0


def test_search_and_attention_combine(client):
    assert set(docs(client, q="abc", attention="true")) == {"bad.pdf"}


def test_detail_of_a_parsed_statement(client):
    d = detail(client, "abc_pms.pdf")
    assert len(d["statement"]["holdings"]) == 5
    assert "holdings_total" in d["validation"]["passed"]
    assert d["email"]["sender"] == "ops@abc.example" and d["review_history"] == []


def test_detail_of_a_flagged_statement_lists_errors_first(client):
    d = detail(client, "bad.pdf")
    assert d["status"] == "needs_review" and d["needs_attention"]
    assert d["issues"][0]["severity"] == "error"
    assert "holding_arithmetic" in {i["code"] for i in d["issues"]}


def test_detail_of_unreadable_documents(client):
    locked = detail(client, "xyz_wealth.pdf")
    assert locked["status"] == "password_required" and locked["statement"] is None
    assert "password" in locked["error"]
    assert detail(client, "junk.pdf")["status"] == "failed"


def test_missing_document_is_404(client):
    assert client.get("/api/documents/9999").status_code == 404


def test_emails_hide_no_pdf_by_default(client):
    shown = client.get("/api/emails").json()
    assert [m["decision"] for m in shown] == ["processed"]
    assert shown[0]["attachments"][0]["filename"] == "abc_pms.pdf"
    assert len(client.get("/api/emails", params={"include_no_pdf": "true"}).json()) == 2


def test_api_returns_hostile_text_verbatim_and_the_page_is_responsible_for_escaping(client):
    assert client.get("/api/emails").json()[0]["subject"] == HOSTILE_SUBJECT


def test_formats(client):
    formats = client.get("/api/formats").json()
    assert len(formats) == 1 and formats[0]["name"] == "abc_portfolio_managers"
    one = client.get(f"/api/formats/{formats[0]['id']}").json()
    assert one["config"]["provider"] == "ABC Portfolio Managers"
    assert "opening value" in one["labels"]
    assert client.get("/api/formats/999").status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_collection_endpoints_cannot_be_written(client, method):
    assert getattr(client, method)("/api/documents").status_code == 405


# ---------------------------------------------------------------- human review
def test_approving_resolves_it_without_changing_the_machine_result(client):
    before = detail(client, "bad.pdf")
    r = review(client, "bad.pdf", "approved", "checked against the PDF")
    assert r.status_code == 200
    after = r.json()
    assert after["status"] == "needs_review"                          # machine status untouched
    assert after["statement"] == before["statement"]                  # data untouched
    assert after["review"]["decision"] == "approved" and not after["needs_attention"]
    assert after["review_history"][0]["note"] == "checked against the PDF"
    assert set(docs(client, attention="true")) == {"xyz_wealth.pdf", "junk.pdf"}
    s = client.get("/api/summary").json()
    assert s["needs_attention"] == 2 and s["reviewed"] == {"approved": 1, "rejected": 0}


def test_unreadable_documents_can_be_dismissed(client):
    assert review(client, "xyz_wealth.pdf", "rejected", "wrong file").status_code == 200
    assert client.get("/api/summary").json()["reviewed"] == {"approved": 0, "rejected": 1}


def test_reopen_puts_it_back_in_the_queue_and_keeps_history(client):
    review(client, "bad.pdf", "rejected", "looks wrong")
    r = review(client, "bad.pdf", "reopened", "second opinion")
    assert r.status_code == 200 and r.json()["needs_attention"]
    assert [h["decision"] for h in r.json()["review_history"]] == ["reopened", "rejected"]
    assert "bad.pdf" in docs(client, attention="true")
    assert client.get("/api/summary").json()["reviewed"] == {"approved": 0, "rejected": 0}


def test_rules_about_what_can_be_reviewed(client):
    assert review(client, "may.pdf", "approved").status_code == 409        # not flagged
    assert review(client, "xyz_wealth.pdf", "approved").status_code == 409  # no data to approve
    assert review(client, "bad.pdf", "reopened").status_code == 409        # nothing to reopen
    assert review(client, "bad.pdf", "approved").status_code == 200
    assert review(client, "bad.pdf", "rejected").status_code == 409        # reopen first
    assert client.post("/api/documents/9999/review", json={"decision": "approved"}).status_code == 404


@pytest.mark.parametrize("body", [
    {"decision": "maybe"},
    {"decision": "approved", "extra": 1},
    {"decision": "approved", "note": "x" * 1001},
    {},
])
def test_malformed_review_is_rejected(client, body):
    doc_id = docs(client)["bad.pdf"]["id"]
    assert client.post(f"/api/documents/{doc_id}/review", json=body).status_code == 422


# ---------------------------------------------------------------- the page
def test_page_is_served_without_inline_script(client):
    page = client.get("/")
    assert page.status_code == 200 and 'src="/static/app.js"' in page.text
    assert "<script>" not in page.text and "style=" not in page.text
    assert client.get("/docs").status_code == 404


def test_security_headers(client):
    headers = client.get("/").headers
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "form-action 'none'" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/summary").headers["cache-control"] == "no-store"


def test_front_end_never_turns_untrusted_text_into_html():
    js = (WEB / "app.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in js


def test_front_end_never_converts_money_to_a_float():
    js = (WEB / "app.js").read_text()
    for banned in ("Number(", "parseFloat", "toLocaleString", "toFixed"):
        assert banned not in js