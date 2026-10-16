import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api import WEB, create_app, demo_db_ok
from app.ops import SCENARIOS, SimMailbox, Workbench, build_demo_emails
from app.pipeline import Pipeline
from app.registry import Registry
from app.store import Store
from samples.generate import generate_all
from samples.sample_data import PASSWORDS

CONFIGS = Path(__file__).parent.parent / "configs"
TOKEN = "test-token-0123456789abcdef"
AUTH = {"X-Review-Token": TOKEN}


class FakeLLM:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)       # an unexpected extra call fails the test


def cfg(name):
    return (CONFIGS / f"{name}.json").read_text()


def units_cfg():
    data = json.loads(cfg("abc_pms"))
    for col in data["holdings"]["columns"]:
        if col["header"] == "Qty":
            col["header"] = "Units"
    return json.dumps(data)


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    root = tmp_path_factory.mktemp("wb")
    generate_all(root)
    return root / "pdfs"


def make(tmp_path, replies=(), demo=False, env_passwords=(), mailbox_factory=None):
    store, registry, llm = Store(), Registry(), FakeLLM(replies)
    bench = Workbench(store, registry, data_dir=tmp_path / "data", llm=llm,
                      env_passwords=list(env_passwords), mailbox_factory=mailbox_factory)
    client = TestClient(create_app(store, registry, TOKEN, bench=bench, demo=demo), headers=AUTH)
    return client, bench, store, registry, llm


def upload(client, data, name="abc_pms.pdf", password=""):
    return client.post("/api/process", content=data, headers={
        **AUTH, "Content-Type": "application/pdf",
        "X-Filename": quote(name), "X-Pdf-Password": quote(password)})


# ---------------------------------------------------------------- upload
def test_upload_processes_a_statement(pdfs, tmp_path):
    client, _, store, _, llm = make(tmp_path, [cfg("abc_pms")])
    r = upload(client, (pdfs / "abc_pms.pdf").read_bytes())
    body = r.json()
    assert r.status_code == 200 and body["status"] == "parsed_ok" and body["used_llm"] is True
    assert body["closing_value"] == "1330000.00" and body["document_id"]
    assert len(list((tmp_path / "data" / "uploads").iterdir())) == 1
    assert client.get(f"/api/documents/{body['document_id']}").json()["status"] == "parsed_ok"


def test_uploading_the_same_file_twice_is_a_duplicate(pdfs, tmp_path):
    client, _, _, _, llm = make(tmp_path, [cfg("abc_pms")])
    data = (pdfs / "abc_pms.pdf").read_bytes()
    upload(client, data)
    again = upload(client, data).json()
    assert again["duplicate"] is True and len(llm.prompts) == 1


def test_a_non_pdf_is_rejected(tmp_path):
    client, *_ = make(tmp_path)
    r = upload(client, b"hello, this is not a pdf", "evil.pdf")
    assert r.status_code == 400 and "not a PDF" in r.json()["detail"]


def test_an_oversized_file_is_rejected(pdfs, tmp_path):
    client, bench, *_ = make(tmp_path)
    bench.max_upload = 100
    assert upload(client, (pdfs / "abc_pms.pdf").read_bytes()).status_code == 413


def test_hostile_file_names_stay_inside_the_upload_folder(pdfs, tmp_path):
    client, *_ = make(tmp_path, [cfg("abc_pms")])
    upload(client, (pdfs / "abc_pms.pdf").read_bytes(), "../../evil.pdf")
    saved = list((tmp_path / "data" / "uploads").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_evil.pdf")
    assert not (tmp_path / "evil.pdf").exists() and not (tmp_path.parent / "evil.pdf").exists()


def test_only_one_job_runs_at_a_time(pdfs, tmp_path):
    client, bench, *_ = make(tmp_path, [cfg("abc_pms")])
    assert bench._lock.acquire(blocking=False)
    try:
        assert upload(client, (pdfs / "abc_pms.pdf").read_bytes()).status_code == 409
    finally:
        bench._lock.release()


@pytest.mark.parametrize("method, path, body", [
    ("post", "/api/process", b"%PDF-"),
    ("post", "/api/documents/1/retry", None),
    ("post", "/api/email/fetch", None),
    ("get", "/api/config", None),
    ("get", "/api/demo/scenarios", None),
    ("post", "/api/demo/reset", None),
])
def test_workbench_endpoints_need_the_token(tmp_path, method, path, body):
    store, registry = Store(), Registry()
    anon = TestClient(create_app(store, registry, TOKEN, demo=True))
    kwargs = {"content": body} if body is not None else {"json": {}}
    r = getattr(anon, method)(path, **kwargs) if method == "post" else anon.get(path)
    assert r.status_code == 401


# ---------------------------------------------------------------- passwords and retry
def test_locked_pdf_then_retry_with_the_password(pdfs, tmp_path):
    client, _, store, _, llm = make(tmp_path, [cfg("xyz_wealth")])
    data = (pdfs / "xyz_wealth.pdf").read_bytes()
    r = upload(client, data, "xyz_wealth.pdf", "wrong-password-123")
    body = r.json()
    assert body["status"] == "password_required" and llm.prompts == []
    assert "wrong-password-123" not in r.text                  # never echoed back
    doc_id = body["document_id"]
    assert client.post(f"/api/documents/{doc_id}/retry", json={}).json()["status"] == "password_required"
    ok = client.post(f"/api/documents/{doc_id}/retry", json={"password": PASSWORDS["xyz_wealth"]})
    assert ok.status_code == 200 and ok.json()["status"] == "parsed_ok"
    assert store.counts() == {"parsed_ok": 1}


def test_retry_rules(pdfs, tmp_path):
    client, _, store, registry, _ = make(tmp_path, [cfg("abc_pms")])
    done = upload(client, (pdfs / "abc_pms.pdf").read_bytes()).json()
    assert client.post(f"/api/documents/{done['document_id']}/retry", json={}).status_code == 409
    assert client.post("/api/documents/9999/retry", json={}).status_code == 404

    # processed from the command line: the page does not hold the file
    Pipeline(store, registry, llm=FakeLLM(), passwords=[]).process_file(pdfs / "xyz_wealth.pdf")
    locked = next(d for d in client.get("/api/documents").json()["items"]
                  if d["status"] == "password_required")
    r = client.post(f"/api/documents/{locked['id']}/retry", json={})
    assert r.status_code == 409 and "not stored" in r.json()["detail"]


# ---------------------------------------------------------------- mailbox
def test_mailbox_without_credentials_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    client, *_ = make(tmp_path)
    r = client.post("/api/email/fetch", json={"days": 2})
    assert r.status_code == 400 and "GMAIL_ADDRESS" in r.json()["detail"]


def test_mailbox_fetch_classifies_and_processes(pdfs, tmp_path):
    factory = lambda: SimMailbox(build_demo_emails(pdfs))
    client, _, store, _, llm = make(tmp_path, [cfg("abc_pms"), cfg("xyz_wealth")],
                                    env_passwords=[PASSWORDS["xyz_wealth"]], mailbox_factory=factory)
    s = client.post("/api/email/fetch", json={"days": 2}).json()
    assert (s["processed"], s["skipped"], s["no_pdf"]) == (1, 1, 1)
    assert store.counts() == {"parsed_ok": 2} and len(llm.prompts) == 2


def test_mailbox_dry_run_changes_nothing(pdfs, tmp_path):
    factory = lambda: SimMailbox(build_demo_emails(pdfs))
    client, _, store, _, llm = make(tmp_path, mailbox_factory=factory)
    s = client.post("/api/email/fetch", json={"days": 2, "dry_run": True}).json()
    assert s["processed"] == 1 and store.counts() == {} and llm.prompts == []


@pytest.mark.parametrize("body", [{"days": 0}, {"days": 61}, {"days": 2, "extra": 1}])
def test_bad_fetch_requests_are_rejected(tmp_path, body):
    client, *_ = make(tmp_path)
    assert client.post("/api/email/fetch", json=body).status_code == 422


# ---------------------------------------------------------------- demo simulator
def test_demo_endpoints_are_hidden_unless_demo_mode(tmp_path):
    client, *_ = make(tmp_path, demo=False)
    assert client.get("/api/config").json() == {"demo": False}
    assert client.get("/api/demo/scenarios").status_code == 404
    assert client.post("/api/demo/reset", json={"confirm": "RESET"}).status_code == 404


def test_demo_mode_lists_the_steps(tmp_path):
    client, *_ = make(tmp_path, demo=True)
    assert client.get("/api/config").json() == {"demo": True}
    listed = client.get("/api/demo/scenarios").json()
    assert [s["key"] for s in listed] == [s.key for s in SCENARIOS] and len(listed) == 8


def test_every_step_plays_in_order_and_meets_its_expectation(tmp_path):
    replies = [cfg("abc_pms"), units_cfg(), cfg("xyz_wealth"),
               cfg("meridian_capital"), cfg("sterling_aif"), cfg("helix_aif")]
    client, bench, store, registry, llm = make(tmp_path, replies, demo=True)
    assert client.post("/api/demo/prepare").status_code == 200
    assert (tmp_path / "data" / "demo" / "pdfs" / "abc_pms_may.pdf").exists()
    for step in client.get("/api/demo/scenarios").json():
        run = client.post(f"/api/demo/scenarios/{step['key']}/run").json()
        assert run["ok"], (step["key"], run["note"], run["results"])
    assert len(llm.prompts) == 6                               # and no step needed a seventh call
    assert store.counts() == {"parsed_ok": 7, "needs_review": 2}


def test_replaying_a_step_without_reset_is_reported_not_repeated(tmp_path):
    client, *_ , llm = make(tmp_path, [cfg("abc_pms")], demo=True)
    first = client.post("/api/demo/scenarios/new_provider/run").json()
    again = client.post("/api/demo/scenarios/new_provider/run").json()
    assert first["ok"] and again["ok"] and again["results"][0]["duplicate"]
    assert len(llm.prompts) == 1
    assert client.post("/api/demo/scenarios/nonsense/run").status_code == 404


def test_simulated_inbox_is_idempotent_and_proves_the_core_rule(tmp_path):
    client, _, store, _, llm = make(tmp_path, [cfg("abc_pms"), cfg("xyz_wealth")], demo=True)
    april = client.post("/api/demo/inbox/april").json()
    assert (april["processed"], april["skipped"], april["no_pdf"]) == (1, 1, 1)
    assert [a.get("status") for a in april["attachments"] if a["accepted"]] == ["parsed_ok"] * 2
    again = client.post("/api/demo/inbox/april").json()
    assert again["already_known"] == 3 and again["new_emails"] == 0
    may = client.post("/api/demo/inbox/may").json()
    accepted = [a for a in may["attachments"] if a["accepted"]]
    assert may["processed"] == 1 and accepted[0]["used_llm"] is False
    assert len(llm.prompts) == 2
    assert client.post("/api/demo/inbox/june").status_code == 404


def test_reset_needs_confirmation_and_keeps_the_demo_files(tmp_path):
    client, bench, store, registry, _ = make(tmp_path, [cfg("abc_pms")], demo=True)
    client.post("/api/demo/scenarios/new_provider/run")
    assert store.counts() and registry.all_formats()
    assert client.post("/api/demo/reset", json={"confirm": "nope"}).status_code == 400
    assert client.post("/api/demo/reset", json={}).status_code == 422
    assert store.counts() and registry.all_formats()           # nothing happened yet
    assert client.post("/api/demo/reset", json={"confirm": "RESET"}).status_code == 200
    assert store.counts() == {} and registry.all_formats() == []
    assert (tmp_path / "data" / "demo" / "pdfs" / "abc_pms.pdf").exists()


def test_sending_test_mail_without_credentials_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    client, *_ = make(tmp_path, demo=True)
    r = client.post("/api/demo/send-test-email", json={"may": False})
    assert r.status_code == 400 and "GMAIL_ADDRESS" in r.json()["detail"]


def test_demo_mode_refuses_a_database_that_is_not_named_demo():
    assert demo_db_ok("demo.db") and demo_db_ok("data/my_demo_run.sqlite")
    assert not demo_db_ok("statements.db") and not demo_db_ok("interview.db")


# ---------------------------------------------------------------- the page
def test_page_has_the_workbench_tab_and_no_inline_code():
    page = (WEB / "index.html").read_text()
    assert 'data-tab="workbench"' in page and "<script>" not in page and "style=" not in page


def test_front_end_keeps_passwords_out_of_urls():
    js = (WEB / "app.js").read_text()
    assert '"X-Pdf-Password"' in js and "password=" not in js and "?password" not in js