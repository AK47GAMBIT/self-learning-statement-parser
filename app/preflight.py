"""Pre-demo checklist.   python -m app.preflight [--llm] [--mail]

--llm   makes one tiny real call to the LLM provider
--mail  logs in to the mailbox (read-only) and searches today's mail
"""
import argparse
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SAMPLES = ["abc_pms", "xyz_wealth", "meridian_capital", "sterling_aif", "helix_aif"]


def run_checks(llm: bool = False, mail: bool = False) -> int:
    """Prints one line per check and returns the number of failures."""
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> bool:
        nonlocal failures
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures += 1
        return ok

    def note(text: str) -> None:
        print(f"[note] {text}")

    check("Python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0])
    key = os.environ.get("GEMINI_API_KEY", "")
    check("GEMINI_API_KEY is set", bool(key), "" if key else "new formats cannot be learned")

    from app.llm import DEFAULT_MODEL
    note(f"model: {os.environ.get('LLM_MODEL') or DEFAULT_MODEL}")

    token = os.environ.get("REVIEW_TOKEN", "")
    note("REVIEW_TOKEN is set" if len(token) >= 16
         else "REVIEW_TOKEN not set: the review page will print a random token at start")
    passwords = [p for p in os.environ.get("PDF_PASSWORDS", "").split(",") if p.strip()]
    note(f"PDF_PASSWORDS: {len(passwords)} configured")

    missing = [n for n in SAMPLES if not (Path("samples/pdfs") / f"{n}.pdf").exists()]
    check("sample PDFs present", not missing,
          f"run: python -m samples.generate (missing {missing})" if missing else "")

    try:
        from app.store import Store
        with tempfile.TemporaryDirectory() as folder:
            Store(str(Path(folder) / "t.db")).counts()
        check("database can be created", True)
    except Exception as e:  # any failure here matters
        check("database can be created", False, str(e)[:120])

    if llm:
        from app.llm import GeminiClient, LLMError
        try:
            reply = GeminiClient(retries=1).complete(
                'Reply with exactly the JSON object {"ok": true} and nothing else.', "ping")
            check("LLM call works", "ok" in reply, reply.strip()[:60])
        except LLMError as e:
            check("LLM call works", False, str(e)[:160])

    if mail:
        from app.email_ingest import MailboxError, mailbox_from_env
        try:
            with mailbox_from_env() as box:
                count = len(box.recent_uids(date.today()))
            check("mailbox login (read-only)", True, f"{count} message(s) today")
        except MailboxError as e:
            check("mailbox login (read-only)", False, str(e)[:160])

    print("\nall good" if not failures else f"\n{failures} problem(s) found")
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Check the setup before a demo")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--mail", action="store_true")
    args = ap.parse_args()
    raise SystemExit(1 if run_checks(args.llm, args.mail) else 0)