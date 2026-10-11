"""Email ingestion: find statement emails and feed their PDFs to the pipeline.

Design points:
  - the mailbox is opened READ-ONLY and fetched with BODY.PEEK: we never change it
  - emails are classified BEFORE anything reaches the pipeline, so unrelated PDFs
    are never sent to the LLM
  - idempotent: emails by Message-ID, PDFs by file hash (in the pipeline)
  - attachment filenames are sanitised before touching the disk
"""
import hashlib
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional, Protocol

from dotenv import load_dotenv

from app.pipeline import Pipeline
from app.store import Store

load_dotenv()
log = logging.getLogger(__name__)

THRESHOLD = 2
BODY_CHARS = 2000
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

POSITIVE = re.compile(
    r"\b(statements?|portfolios?|pms|aif|capital account|valuation|holdings?|nav|"
    r"investor report|account summary)\b")
NEGATIVE = re.compile(
    r"\b(invoice|receipt|ticket|boarding pass|newsletter|brochure|factsheet|fact sheet|"
    r"terms|disclaimer|otp|resume|cv|quotation|payslip)\b")


class MailboxError(Exception):
    """The mailbox could not be reached or read."""


# ------------------------------------------------------------------ classification
@dataclass(frozen=True)
class Classification:
    score: int
    reasons: tuple
    threshold: int = THRESHOLD

    @property
    def accepted(self) -> bool:
        return self.score >= self.threshold


def _words(text: str) -> str:
    """'abc_pms.pdf' -> 'abc pms pdf', so word boundaries work on file names."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def sender_allowed(address: str, allowed: Iterable[str]) -> bool:
    address = (address or "").lower()
    domain = address.rpartition("@")[2]
    for entry in allowed:
        entry = entry.strip().lower().lstrip("@")
        if entry and (address == entry or domain == entry):
            return True
    return False


def classify(subject: str, sender: str, body: str, filename: str,
             allowed_senders: Iterable[str] = (), threshold: int = THRESHOLD) -> Classification:
    score, reasons = 0, []

    def add(points: int, why: str):
        nonlocal score
        score += points
        reasons.append(f"{why} ({points:+d})")

    if sender_allowed(sender, allowed_senders):
        add(3, "sender is on the allow-list")
    subj, fname, text = _words(subject), _words(filename), _words(body[:BODY_CHARS])
    if m := POSITIVE.search(subj):
        add(2, f"subject mentions '{m.group(1)}'")
    if m := NEGATIVE.search(subj):
        add(-1, f"subject mentions '{m.group(1)}'")
    if m := POSITIVE.search(text):
        add(1, f"body mentions '{m.group(1)}'")
    if m := POSITIVE.search(fname):
        add(2, f"file name mentions '{m.group(1)}'")
    if m := NEGATIVE.search(fname):
        add(-3, f"file name mentions '{m.group(1)}'")
    return Classification(score, tuple(reasons), threshold)


# ------------------------------------------------------------------ parsing
@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    sender: str
    received: Optional[str]
    body: str
    attachments: list[Attachment]


def safe_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "attachment"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[-80:]


def parse_email(raw: bytes) -> ParsedEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("message-id") or "").strip() or \
        "raw:" + hashlib.sha256(raw).hexdigest()
    try:
        received = parsedate_to_datetime(str(msg["date"])).isoformat()
    except (TypeError, ValueError):
        received = None

    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
            if part.get_content_type() == "text/html":
                body = re.sub(r"<[^>]+>", " ", body)
    except (LookupError, ValueError, KeyError):
        body = ""                      # undecodable body: classification just loses a signal

    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        if not (part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf")):
            continue
        data = part.get_payload(decode=True) or b""
        if b"%PDF-" not in data[:1024] or len(data) > MAX_ATTACHMENT_BYTES:
            continue                   # not a real PDF, or too big
        attachments.append(Attachment(name or "attachment.pdf", data))

    return ParsedEmail(message_id, str(msg.get("subject") or ""),
                       parseaddr(str(msg.get("from") or ""))[1].lower(),
                       received, body, attachments)


# ------------------------------------------------------------------ mailbox access
class Mailbox(Protocol):
    def recent_uids(self, since: date) -> list[str]: ...
    def message_id(self, uid: str) -> Optional[str]: ...
    def fetch(self, uid: str) -> bytes: ...


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"      # locale-independent


class ImapMailbox:
    def __init__(self, host: str, user: str, password: str, folder: str = "INBOX"):
        self.host, self.user, self.password, self.folder = host, user, password, folder
        self._m = None

    def open(self):
        try:
            self._m = imaplib.IMAP4_SSL(self.host)
            self._m.login(self.user, self.password)
            status, _ = self._m.select(self.folder, readonly=True)     # never modifies mail
        except imaplib.IMAP4.error as e:
            raise MailboxError(
                "IMAP login failed. Gmail needs an APP PASSWORD (not your normal password), "
                f"with 2-Step Verification on and IMAP enabled. Server said: {e}") from e
        except OSError as e:
            raise MailboxError(f"cannot reach {self.host}: {e}") from e
        if status != "OK":
            raise MailboxError(f"cannot open folder {self.folder!r}")
        return self

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        try:
            self._m.close()
            self._m.logout()
        except Exception:
            pass

    def recent_uids(self, since: date) -> list[str]:
        status, data = self._m.uid("search", None, "SINCE", imap_date(since))
        if status != "OK":
            raise MailboxError("mailbox search failed")
        return [u.decode() for u in (data[0] or b"").split()]

    def message_id(self, uid: str) -> Optional[str]:
        status, data = self._m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        header = message_from_bytes(data[0][1])
        return (header.get("Message-ID") or "").strip() or None

    def fetch(self, uid: str) -> bytes:
        status, data = self._m.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise MailboxError(f"could not fetch message {uid}")
        return data[0][1]


def mailbox_from_env() -> ImapMailbox:
    user = os.environ.get("GMAIL_ADDRESS")
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")  # Google shows spaces
    if not user or not password:
        raise MailboxError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")
    return ImapMailbox(os.environ.get("IMAP_HOST", "imap.gmail.com"), user, password,
                       os.environ.get("MAILBOX_FOLDER", "INBOX"))


def senders_from_env() -> list[str]:
    return [s.strip() for s in os.environ.get("STATEMENT_SENDERS", "").split(",") if s.strip()]


# ------------------------------------------------------------------ the ingestor
@dataclass
class IngestSummary:
    new_emails: int = 0
    already_known: int = 0
    processed: int = 0
    skipped: int = 0
    no_pdf: int = 0
    attachments: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class EmailIngestor:
    def __init__(self, store: Store, pipeline: Pipeline,
                 attachments_dir="data/attachments", allowed_senders: Iterable[str] = (),
                 threshold: int = THRESHOLD):
        self.store = store
        self.pipeline = pipeline
        self.dir = Path(attachments_dir)
        self.allowed = list(allowed_senders)
        self.threshold = threshold

    def run(self, mailbox: Mailbox, days: int = 30, today: Optional[date] = None,
            dry_run: bool = False) -> IngestSummary:
        since = (today or date.today()) - timedelta(days=days)
        summary = IngestSummary()
        for uid in mailbox.recent_uids(since):
            try:
                self._handle(mailbox, uid, summary, dry_run)
            except MailboxError:
                raise
            except Exception as e:  # one bad message must not stop the run
                log.exception("failed on message %s", uid)
                summary.errors.append(f"message {uid}: {type(e).__name__}: {e}")
        return summary

    def _save(self, att: Attachment) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{hashlib.sha256(att.data).hexdigest()[:12]}_{safe_filename(att.filename)}"
        if not path.exists():
            path.write_bytes(att.data)
        return path

    def _handle(self, mailbox: Mailbox, uid: str, summary: IngestSummary, dry_run: bool):
        mid = mailbox.message_id(uid)
        if mid and self.store.email_seen(mid):
            summary.already_known += 1
            return
        mail = parse_email(mailbox.fetch(uid))
        if self.store.email_seen(mail.message_id):       # covers emails with no Message-ID header
            summary.already_known += 1
            return

        summary.new_emails += 1
        details = []
        for att in mail.attachments:
            cls = classify(mail.subject, mail.sender, mail.body, att.filename,
                           self.allowed, self.threshold)
            entry = {"filename": att.filename, "score": cls.score,
                     "reasons": list(cls.reasons), "accepted": cls.accepted}
            if cls.accepted and not dry_run:
                result = self.pipeline.process_file(self._save(att),
                                                    source=f"email:{mail.message_id}")
                entry.update(status=result.status.value, used_llm=result.used_llm,
                             duplicate=result.duplicate)
            details.append(entry)
            summary.attachments.append({**entry, "subject": mail.subject})

        if not mail.attachments:
            decision = "no_pdf"
            summary.no_pdf += 1
        elif any(d["accepted"] for d in details):
            decision = "processed"
            summary.processed += 1
        else:
            decision = "skipped"
            summary.skipped += 1

        if not dry_run:
            self.store.record_email(mail.message_id, mail.subject, mail.sender, mail.received,
                                    decision, json.dumps(details))


def main(argv=None):
    # python -m app.email_ingest --days 2 [--dry-run]
    import argparse

    from app.registry import Registry

    ap = argparse.ArgumentParser(description="Ingest statement emails")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default="statements.db")
    ap.add_argument("--dir", default="data/attachments")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify only: no files saved, nothing processed or recorded")
    ap.add_argument("--review-new", action="store_true")
    args = ap.parse_args(argv)

    store, registry = Store(args.db), Registry(args.db)
    pipeline = Pipeline(store, registry, review_new_formats=args.review_new)
    ingestor = EmailIngestor(store, pipeline, args.dir, senders_from_env())
    try:
        with mailbox_from_env() as mailbox:
            summary = ingestor.run(mailbox, args.days, dry_run=args.dry_run)
    except MailboxError as e:
        print(f"mailbox error: {e}")
        raise SystemExit(1)

    for a in summary.attachments:
        verdict = "ACCEPT" if a["accepted"] else "skip  "
        outcome = (f"-> {a['status']}, llm={'yes' if a['used_llm'] else 'no'}"
                   f"{', duplicate' if a.get('duplicate') else ''}") if "status" in a else ""
        print(f"{verdict} score={a['score']:+d} {a['filename']:28} | {a['subject'][:40]} {outcome}")
        if not a["accepted"] or args.dry_run:
            for why in a["reasons"]:
                print(f"         {why}")
    print(f"\nnew emails: {summary.new_emails} (already known: {summary.already_known}) | "
          f"processed {summary.processed}, skipped {summary.skipped}, no PDF {summary.no_pdf}")
    for err in summary.errors:
        print("error:", err)
    if not args.dry_run:
        print("documents:", store.counts())


if __name__ == "__main__":
    main()