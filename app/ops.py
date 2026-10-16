"""What the Workbench page can do: upload, retry, mailbox fetch, and the demo simulator.

Everything goes through the same Pipeline as the command line. One job runs at a time.
"""
import hashlib
import os
import smtplib
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

from app.email_ingest import (
    MAX_ATTACHMENT_BYTES, EmailIngestor, MailboxError, mailbox_from_env, safe_filename,
    senders_from_env,
)
from app.llm import LLMClient
from app.pipeline import Pipeline, ProcessResult, passwords_from_env
from app.reader import file_sha256
from app.registry import Registry
from app.store import Store
from app.validation import Severity
from samples.sample_data import PASSWORDS

RETRYABLE = {"password_required", "failed", "learning_failed"}
CORE_PDFS = ["abc_pms", "xyz_wealth", "meridian_capital", "sterling_aif", "helix_aif"]
VARIANT_PDFS = ["abc_pms_may.pdf", "abc_pms_bad_numbers.pdf", "abc_pms_units.pdf"]
STRESS_PDFS = ["stress_long_abc.pdf", "stress_two_pages.pdf", "stress_wrapped_above.pdf"]
DEMO_PASSWORD = PASSWORDS["xyz_wealth"]            # the fake demo password


class OpsError(Exception):
    """A problem the page should show to the person, with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class Busy(OpsError):
    def __init__(self):
        super().__init__("Another job is still running. Wait for it to finish.", 409)


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    say: str                       # the talking point for the video
    expect: str                    # what the page should show
    files: tuple                   # relative to data/demo
    passwords: tuple = ()          # EXACT password list for this step (the environment is ignored)
    expect_status: str = "parsed_ok"
    expect_llm: Optional[bool] = None


SCENARIOS = (
    Scenario("new_provider", "A provider the system has never seen",
             "ABC's layout is new, so the LLM writes a config once. Four checks must pass "
             "before it is stored.",
             "parsed OK; LLM used the first time", ("pdfs/abc_pms.pdf",)),
    Scenario("next_month", "Next month, same provider",
             "Different numbers, same layout. The stored config is used and the LLM is not "
             "called: look at 'LLM no'.",
             "parsed OK, LLM no", ("pdfs/abc_pms_may.pdf",), expect_llm=False),
    Scenario("bad_numbers", "Wrong numbers are caught",
             "One market value is 10,000 too high. No LLM is involved: validation notices "
             "that the figures do not add up.",
             "needs review, LLM no", ("pdfs/abc_pms_bad_numbers.pdf",),
             expect_status="needs_review", expect_llm=False),
    Scenario("renamed_column", "The provider renames a column",
             "'Qty' became 'Units'. The stored config cannot read it, so the system learns a "
             "new version and asks a person to confirm.",
             "needs review, LLM used (config v2)", ("pdfs/abc_pms_units.pdf",),
             expect_status="needs_review", expect_llm=True),
    Scenario("locked_no_password", "A password-protected PDF, no password",
             "The system does not guess. It records the document as 'password needed' and "
             "never sends it to the LLM.",
             "password needed, LLM no", ("pdfs/xyz_wealth.pdf",),
             expect_status="password_required", expect_llm=False),
    Scenario("unlock", "Supply the password",
             "Same file, now with the password. This is also the new Retry box in the "
             "document drawer.",
             "parsed OK; LLM used (new provider)", ("pdfs/xyz_wealth.pdf",),
             passwords=(DEMO_PASSWORD,)),
    Scenario("messy_pages", "A messy 3-page statement",
             "Repeated headers, page footers and long names wrapped over two lines. Still "
             "parsed by the stored config.",
             "parsed OK, LLM no", ("stress/stress_long_abc.pdf",), expect_llm=False),
    Scenario("more_providers", "Three more providers at once",
             "Meridian (Dr/Cr signs), Sterling and Helix (AIFs with no holdings, units and "
             "NAV). Each layout is learned once.",
             "all parsed OK",
             ("pdfs/meridian_capital.pdf", "pdfs/sterling_aif.pdf", "pdfs/helix_aif.pdf")),
)
SCENARIO_BY_KEY = {s.key: s for s in SCENARIOS}


# ------------------------------------------------------------------ simulated inbox
class SimMailbox:
    """An in-memory inbox with the same methods as ImapMailbox (no Gmail needed)."""

    def __init__(self, raws):
        self.raws = {str(i + 1): raw for i, raw in enumerate(raws)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recent_uids(self, since):
        return list(self.raws)

    def message_id(self, uid):
        header = message_from_bytes(self.raws[uid]).get("Message-ID") or ""
        return header.strip() or None

    def fetch(self, uid):
        return self.raws[uid]


def build_demo_emails(pdf_dir, may: bool = False, user: str = "investor@example.com") -> list:
    """Realistic emails with FIXED Message-IDs, so delivering them twice is idempotent."""
    from samples.send_test_email import decoy_pdf
    from email.message import EmailMessage
    from pathlib import Path

    pdf_dir = Path(pdf_dir)

    def mail(msg_id, date, subject, body, sender, attachments=()):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, user, subject
        msg["Message-ID"], msg["Date"] = msg_id, date
        msg.set_content(body)
        for name, data in attachments:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        return msg.as_bytes()

    if may:
        return [mail("<demo-may-statement@simulated.local>", "Thu, 04 Jun 2026 09:00:00 +0530",
                     "Your May 2026 PMS statement",
                     "Dear investor, your May statement is attached.", "reports@abc.example",
                     [("abc_pms_may.pdf", (pdf_dir / "abc_pms_may.pdf").read_bytes())])]
    return [
        mail("<demo-april-statements@simulated.local>", "Mon, 04 May 2026 09:00:00 +0530",
             "Your April 2026 PMS statements",
             "Dear investor, please find your statements attached.", "reports@abc.example",
             [(n, (pdf_dir / n).read_bytes()) for n in ("abc_pms.pdf", "xyz_wealth.pdf")]),
        mail("<demo-newsletter@simulated.local>", "Mon, 04 May 2026 10:00:00 +0530",
             "Weekly newsletter", "Market ideas for your portfolio this week.",
             "news@markets.example", [("brochure.pdf", decoy_pdf())]),
        mail("<demo-lunch@simulated.local>", "Mon, 04 May 2026 11:00:00 +0530",
             "Lunch on Friday?", "Are you free at 1pm?", "friend@example.com"),
    ]


def _smtp(user: str, password: str):
    try:
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        smtp.login(user, password)
        return smtp
    except (smtplib.SMTPServerDisconnected, OSError):
        pass                                           # some networks block 465: try 587
    smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    smtp.starttls()
    smtp.login(user, password)
    return smtp


# ------------------------------------------------------------------ the workbench
class Workbench:
    def __init__(self, store: Store, registry: Registry, data_dir="data",
                 llm: Optional[LLMClient] = None, env_passwords: Optional[list] = None,
                 mailbox_factory: Optional[Callable] = None):
        self.store, self.registry, self.llm = store, registry, llm
        self.data_dir = Path(data_dir)
        self.env_passwords = env_passwords           # None = read PDF_PASSWORDS when needed
        self.mailbox_factory = mailbox_factory or mailbox_from_env
        self.max_upload = MAX_ATTACHMENT_BYTES
        self._lock = threading.Lock()

    # ---- plumbing
    @contextmanager
    def exclusive(self):
        if not self._lock.acquire(blocking=False):
            raise Busy()
        try:
            yield
        finally:
            self._lock.release()

    def _base_passwords(self) -> list:
        return list(self.env_passwords) if self.env_passwords is not None else passwords_from_env()

    def _pipeline(self, passwords) -> Pipeline:
        return Pipeline(self.store, self.registry, llm=self.llm, passwords=list(passwords))

    def result_dict(self, r: ProcessResult) -> dict:
        row = self.store.get_by_hash(r.file_hash) if r.file_hash else None
        errors = [i for i in r.issues if i.severity == Severity.ERROR]
        closing = r.statement.summary.closing_value if r.statement else None
        return {
            "document_id": row["id"] if row else None,
            "filename": r.filename,
            "status": r.status.value,
            "duplicate": r.duplicate,
            "used_llm": r.used_llm,
            "format_name": r.format_name,
            "match_kind": r.match_kind,
            "match_score": r.match_score,
            "error": r.error,
            "errors": [{"code": i.code, "message": i.message} for i in errors[:5]],
            "closing_value": str(closing) if closing is not None else None,   # exact decimal text
            "currency": r.statement.currency if r.statement else None,
        }

    # ---- real operations
    def process_upload(self, filename: str, data: bytes, password: str = "") -> dict:
        if len(data) > self.max_upload:
            raise OpsError("The file is larger than the upload limit.", 413)
        if b"%PDF-" not in data[:1024]:
            raise OpsError("That file is not a PDF.", 400)
        digest = hashlib.sha256(data).hexdigest()
        with self.exclusive():
            folder = self.data_dir / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{digest[:12]}_{safe_filename(filename)}"
            if not path.exists():
                path.write_bytes(data)
            passwords = self._base_passwords() + ([password] if password else [])
            return self.result_dict(self._pipeline(passwords).process_file(path, source="upload"))

    def find_file(self, file_hash: str) -> Optional[Path]:
        for sub in ("uploads", "attachments"):
            folder = self.data_dir / sub
            if folder.is_dir():
                for p in folder.glob(f"{file_hash[:12]}_*"):
                    if p.is_file() and file_sha256(p) == file_hash:
                        return p
        return None

    def retry(self, doc_id: int, password: str = "") -> dict:
        row = self.store.get(doc_id)
        if row is None:
            raise OpsError("No such document.", 404)
        if row["status"] not in RETRYABLE:
            raise OpsError("Only documents that did not finish can be retried.", 409)
        path = self.find_file(row["file_hash"])
        if path is None:
            raise OpsError("The original file is not stored by this page, so it cannot be "
                           "retried here. Upload it again.", 409)
        with self.exclusive():
            passwords = self._base_passwords() + ([password] if password else [])
            result = self._pipeline(passwords).process_file(path, source=row["source"] or "retry")
            return self.result_dict(result)

    def fetch_email(self, days: int = 1, dry_run: bool = False) -> dict:
        with self.exclusive():
            pipeline = self._pipeline(self._base_passwords())
            ingestor = EmailIngestor(self.store, pipeline, self.data_dir / "attachments",
                                     senders_from_env())
            try:
                # Restrict the IMAP search window to just the last 1 day
                with self.mailbox_factory() as box:
                    summary = ingestor.run(box, days=1, dry_run=dry_run)
            except MailboxError as e:
                raise OpsError(str(e), 400) from e
        return asdict(summary)

    # ---- demo simulator
    def ensure_demo_files(self) -> Path:
        from samples.generate import generate_all
        from samples.next_month import write_demo_files
        from samples.stress import write_stress_files

        base = self.data_dir / "demo"
        pdfs, stress = base / "pdfs", base / "stress"
        if not all((pdfs / f"{n}.pdf").exists() for n in CORE_PDFS):
            generate_all(base)
        if not all((pdfs / n).exists() for n in VARIANT_PDFS):
            write_demo_files(pdfs)
        if not all((stress / n).exists() for n in STRESS_PDFS):
            write_stress_files(stress)
        return base

    def prepare_demo(self) -> dict:
        with self.exclusive():
            base = self.ensure_demo_files()
        return {"dir": str(base), "files": len(CORE_PDFS) + len(VARIANT_PDFS) + len(STRESS_PDFS)}

    def scenario_list(self) -> list:
        return [{"key": s.key, "title": s.title, "say": s.say, "expect": s.expect,
                 "files": [Path(f).name for f in s.files]} for s in SCENARIOS]

    @staticmethod
    def _check(sc: Scenario, results: list) -> tuple:
        notes = []
        for r in results:
            if r["status"] != sc.expect_status:
                if r["duplicate"]:
                    return False, (f"Already processed earlier as {r['status']}. "
                                   f"Reset the demo data to replay this step.")
                extra = f" ({r['error']})" if r["error"] else ""
                return False, (f"{r['filename']}: expected {sc.expect_status}, "
                               f"got {r['status']}{extra}")
            if sc.expect_llm is not None and not r["duplicate"] and r["used_llm"] != sc.expect_llm:
                return False, (f"{r['filename']}: expected LLM {'yes' if sc.expect_llm else 'no'}"
                               f" (have the earlier steps been run?)")
            if r["duplicate"]:
                notes.append("already processed earlier")
        return True, "; ".join(dict.fromkeys(notes))

    def run_scenario(self, key: str) -> dict:
        sc = SCENARIO_BY_KEY.get(key)
        if sc is None:
            raise OpsError("No such step.", 404)
        with self.exclusive():
            base = self.ensure_demo_files()
            pipeline = self._pipeline(sc.passwords)
            results = [self.result_dict(pipeline.process_file(base / rel, source="demo"))
                       for rel in sc.files]
        ok, note = self._check(sc, results)
        return {"key": sc.key, "ok": ok, "note": note, "results": results}

    def deliver_inbox(self, which: str) -> dict:
        if which not in ("april", "may"):
            raise OpsError("No such mailbox.", 404)
        with self.exclusive():
            base = self.ensure_demo_files()
            mailbox = SimMailbox(build_demo_emails(base / "pdfs", may=(which == "may")))
            pipeline = self._pipeline([DEMO_PASSWORD])
            ingestor = EmailIngestor(self.store, pipeline, self.data_dir / "attachments", [])
            return asdict(ingestor.run(mailbox, days=1))

    def send_test_emails(self, may: bool = False) -> dict:
        user = os.environ.get("GMAIL_ADDRESS")
        password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
        if not user or not password:
            raise OpsError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env", 400)
        with self.exclusive():
            base = self.ensure_demo_files()
            # 1. Get the list of raw email message objects (or update builder to return objects)
            messages = build_demo_emails(base / "pdfs", may=may, user=user)
            try:
                with _smtp(user, password) as smtp:
                    for m in messages:
                        # If messages are returned as bytes, convert them back to a message object
                        if isinstance(m, bytes):
                            msg_obj = message_from_bytes(m)
                            smtp.send_message(msg_obj)
                        else:
                            smtp.send_message(m)
            except smtplib.SMTPAuthenticationError as e:
                raise OpsError("Gmail rejected the login. Check GMAIL_APP_PASSWORD.", 400) from e
            except (smtplib.SMTPException, OSError) as e:
                raise OpsError(f"Could not send: {type(e).__name__}", 400) from e
        return {"sent": len(messages)}

    def reset(self, confirm: str) -> dict:
        if confirm != "RESET":
            raise OpsError("Send the word RESET to confirm.", 400)
        with self.exclusive():
            self.store.clear_all()
            self.registry.clear()
        return {"cleared": True}