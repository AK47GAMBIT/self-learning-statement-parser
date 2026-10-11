import imaplib
from datetime import date
from email import message_from_bytes
from email.message import EmailMessage

import pytest
from reportlab.pdfgen import canvas
import io

from app.email_ingest import (
    EmailIngestor, ImapMailbox, MailboxError, classify, imap_date, mailbox_from_env,
    parse_email, safe_filename,
)
from app.pipeline import Pipeline
from app.registry import Registry
from app.store import Store
from samples.generate import generate_all
from pathlib import Path
import json

CONFIGS = Path(__file__).parent.parent / "configs"


class FakeLLM:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class FakeMailbox:
    def __init__(self, raws):
        self.raws = {str(i + 1): r for i, r in enumerate(raws)}
        self.fetched = []

    def recent_uids(self, since):
        return list(self.raws)

    def message_id(self, uid):
        return message_from_bytes(self.raws[uid]).get("Message-ID")

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.raws[uid]


def build_email(subject, sender="reports@abc.example", body="Hello", attachments=(),
                message_id="<m1@test>"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "me@example.com", subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 11 May 2026 09:00:00 +0530"
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def decoy_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Brochure")
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return root


def pdf(samples, name):
    return (samples / "pdfs" / f"{name}.pdf").read_bytes()


def cfg(name):
    return (CONFIGS / f"{name}.json").read_text()


def setup(tmp_path, replies=(), allowed=()):
    store, registry, llm = Store(), Registry(), FakeLLM(replies)
    pipe = Pipeline(store, registry, llm=llm, passwords=["demo1234"])
    return EmailIngestor(store, pipe, tmp_path / "att", allowed_senders=allowed), store, llm


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("subject, sender, body, filename, allowed, accepted", [
    ("Your April PMS statement", "ops@abc.example", "", "stmt_apr.pdf", (), True),
    ("Docs", "ops@abc.example", "", "statement_april.pdf", (), True),
    ("Invoice 1042", "billing@shop.example", "Please pay", "invoice.pdf", (), False),
    ("Weekly newsletter", "news@x.example", "ideas for your portfolio", "brochure.pdf", (), False),
    ("FW: docs", "boss@corp.example", "", "scan001.pdf", ("corp.example",), True),
    ("FW: docs", "boss@corp.example", "", "scan001.pdf", (), False),
    ("Navigation tips", "a@b.example", "", "map.pdf", (), False),     # 'nav' inside a longer word
])
def test_classifier(subject, sender, body, filename, allowed, accepted):
    assert classify(subject, sender, body, filename, allowed).accepted is accepted


def test_filename_words_are_split_on_underscores():
    assert classify("x", "a@b.example", "", "abc_pms.pdf").score == 2


def test_classifier_explains_itself():
    c = classify("Your PMS statement", "a@b.example", "", "invoice.pdf")
    assert any("subject mentions" in r for r in c.reasons)
    assert any("file name mentions 'invoice'" in r for r in c.reasons)


# ---------------------------------------------------------------- parsing and safety
@pytest.mark.parametrize("raw, expected", [
    ("../../evil.pdf", "evil.pdf"),
    ("C:\\Users\\x\\stmt.pdf", "stmt.pdf"),
    ("report", "report.pdf"),
    ("résumé final.PDF", "r_sum_final.PDF"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_fake_pdf_is_not_an_attachment():
    raw = build_email("Your statement", attachments=[("statement.pdf", b"MZ not a pdf")])
    assert parse_email(raw).attachments == []


def test_missing_message_id_gets_a_stable_fallback():
    raw = build_email("hi").replace(b"Message-ID: <m1@test>\n", b"")
    assert parse_email(raw).message_id.startswith("raw:")


def test_hostile_filename_cannot_escape_the_folder(samples, tmp_path):
    raw = build_email("Your PMS statement", attachments=[("../../evil.pdf", pdf(samples, "abc_pms"))])
    ingestor, _, _ = setup(tmp_path, [cfg("abc_pms")])
    ingestor.run(FakeMailbox([raw]), today=date(2026, 5, 12))
    saved = list((tmp_path / "att").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_evil.pdf")
    assert not (tmp_path.parent / "evil.pdf").exists()


# ---------------------------------------------------------------- end to end
def inbox(samples):
    return FakeMailbox([
        build_email("Your April 2026 PMS statements", attachments=[
            ("abc_pms.pdf", pdf(samples, "abc_pms")),
            ("xyz_wealth.pdf", pdf(samples, "xyz_wealth"))], message_id="<s@test>"),
        build_email("Weekly newsletter", sender="news@x.example",
                    body="Market ideas for your portfolio",
                    attachments=[("brochure.pdf", decoy_pdf())], message_id="<n@test>"),
        build_email("Lunch on Friday?", message_id="<p@test>"),
    ])


def test_inbox_end_to_end(samples, tmp_path):
    ingestor, store, llm = setup(tmp_path, [cfg("abc_pms"), cfg("xyz_wealth")])
    summary = ingestor.run(inbox(samples), today=date(2026, 5, 12))

    assert (summary.processed, summary.skipped, summary.no_pdf) == (1, 1, 1)
    assert store.counts() == {"parsed_ok": 2}              # locked XYZ PDF opened via password list
    assert len(llm.prompts) == 2                           # the decoy never reached the LLM
    decisions = {r["message_id"]: r["decision"] for r in store.list_emails()}
    assert decisions == {"<s@test>": "processed", "<n@test>": "skipped", "<p@test>": "no_pdf"}
    assert all(d["source"] == "email:<s@test>" for d in store.list())


def test_second_run_does_not_refetch_or_reprocess(samples, tmp_path):
    ingestor, store, llm = setup(tmp_path, [cfg("abc_pms"), cfg("xyz_wealth")])
    mailbox = inbox(samples)
    ingestor.run(mailbox, today=date(2026, 5, 12))
    mailbox.fetched.clear()
    again = ingestor.run(mailbox, today=date(2026, 5, 12))
    assert again.already_known == 3 and again.new_emails == 0
    assert mailbox.fetched == [] and len(llm.prompts) == 2


def test_forwarded_copy_of_a_statement_is_a_duplicate(samples, tmp_path):
    ingestor, store, llm = setup(tmp_path, [cfg("abc_pms")])
    mailbox = FakeMailbox([
        build_email("Your PMS statement", attachments=[("a.pdf", pdf(samples, "abc_pms"))],
                    message_id="<1@test>"),
        build_email("Fwd: Your PMS statement", attachments=[("b.pdf", pdf(samples, "abc_pms"))],
                    message_id="<2@test>"),
    ])
    summary = ingestor.run(mailbox, today=date(2026, 5, 12))
    assert [a.get("duplicate") for a in summary.attachments] == [False, True]
    assert len(llm.prompts) == 1 and store.counts() == {"parsed_ok": 1}


def test_dry_run_changes_nothing(samples, tmp_path):
    ingestor, store, llm = setup(tmp_path)
    summary = ingestor.run(inbox(samples), today=date(2026, 5, 12), dry_run=True)
    assert (summary.processed, summary.skipped, summary.no_pdf) == (1, 1, 1)
    assert store.counts() == {} and store.list_emails() == []
    assert llm.prompts == [] and not (tmp_path / "att").exists()


# ---------------------------------------------------------------- IMAP layer
def test_imap_date_ignores_locale():
    assert imap_date(date(2026, 10, 3)) == "03-Oct-2026"


class FakeImap:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "search":
            return "OK", [b"4 7"]
        if "HEADER" in args[1]:
            return "OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {24}",
                           b"Message-ID: <x@y>\r\n\r\n"), b")"]
        return "OK", [(b"7 (UID 7 BODY[] {99}", self.raw), b")"]


def test_imap_reads_without_marking_mail_as_seen():
    mailbox = ImapMailbox("h", "u", "p")
    mailbox._m = FakeImap(b"RAW")
    assert mailbox.recent_uids(date(2026, 5, 1)) == ["4", "7"]
    assert mailbox.message_id("7") == "<x@y>"
    assert mailbox.fetch("7") == b"RAW"
    fetches = [c for c in mailbox._m.calls if c[0] == "fetch"]
    assert fetches and all("PEEK" in c[2] for c in fetches)


def test_login_failure_explains_app_passwords(monkeypatch):
    class BadSSL:
        def __init__(self, host):
            pass

        def login(self, user, password):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", BadSSL)
    with pytest.raises(MailboxError, match="(?i)app password"):
        ImapMailbox("h", "u", "p").open()


def test_missing_credentials_fail_clearly(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(MailboxError, match="GMAIL_ADDRESS"):
        mailbox_from_env()