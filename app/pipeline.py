"""One document in, one stored result out.

The LLM is called in exactly two situations:
  - the format is unknown (never seen), or
  - a known format's stored config is proven not to fit this document (layout changed).
A normal parse of a known format never calls it.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

from pydantic import ValidationError

from app.config_spec import FormatConfig
from app.engine import ParseError, parse
from app.fingerprint import compute_fingerprint
from app.learner import learn_and_register
from app.llm import GeminiClient, LLMClient
from app.reader import (
    Document, PasswordRequiredError, ReaderError, WrongPasswordError, file_sha256,
    read_document,
)
from app.registry import MatchKind, MatchResult, Registry
from app.schema import Statement
from app.store import Store
from app.validation import Issue, Severity, ValidationReport, validate

log = logging.getLogger(__name__)


class DocStatus(str, Enum):
    PARSED_OK = "parsed_ok"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    LEARNING_FAILED = "learning_failed"
    PASSWORD_REQUIRED = "password_required"


# Only these count as finished. The others can be retried (add a password, LLM recovers...).
DONE = {DocStatus.PARSED_OK.value, DocStatus.NEEDS_REVIEW.value}


@dataclass
class ProcessResult:
    status: DocStatus
    file_hash: str = ""
    filename: str = ""
    duplicate: bool = False
    used_llm: bool = False
    format_name: Optional[str] = None
    match_kind: Optional[str] = None
    match_score: Optional[float] = None
    statement: Optional[Statement] = None
    report: Optional[ValidationReport] = None
    issues: list[Issue] = field(default_factory=list)
    error: Optional[str] = None


def passwords_from_env() -> list[str]:
    raw = os.environ.get("PDF_PASSWORDS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _record(r: ProcessResult, source: str) -> dict:
    s = r.statement
    return dict(
        filename=r.filename, source=source, status=r.status.value,
        statement_type=s.statement_type.value if s else None,
        provider=s.provider if s else None,
        strategy_name=s.strategy_name if s else None,
        client_id=s.client_id if s else None,
        period_start=s.period_start.isoformat() if s else None,
        period_end=s.period_end.isoformat() if s else None,
        format_name=r.format_name, match_kind=r.match_kind, match_score=r.match_score,
        used_llm=int(r.used_llm),
        issues_json=json.dumps([{"code": i.code, "severity": i.severity.value,
                                 "message": i.message} for i in r.issues]),
        validation_json=json.dumps(r.report.to_dict()) if r.report else None,
        statement_json=s.model_dump_json() if s else None,
        error=r.error,
    )


class Pipeline:
    def __init__(self, store: Store, registry: Registry, llm: Optional[LLMClient] = None,
                 passwords: Optional[Iterable[str]] = None,
                 review_new_formats: bool = False, max_learn_attempts: int = 3):
        self.store = store
        self.registry = registry
        self.llm = llm or GeminiClient()
        self.passwords = list(passwords) if passwords is not None else passwords_from_env()
        self.review_new_formats = review_new_formats
        self.max_learn_attempts = max_learn_attempts

    # ------------------------------------------------------------------ public
    def process_file(self, path, source: str = "file") -> ProcessResult:
        path = Path(path)
        try:
            file_hash = file_sha256(path)
        except OSError as e:
            return ProcessResult(DocStatus.FAILED, filename=path.name,
                                 error=f"cannot read file: {e}")

        existing = self.store.get_by_hash(file_hash)
        if existing and existing["status"] in DONE:
            return ProcessResult(DocStatus(existing["status"]), file_hash=file_hash,
                                 filename=existing["filename"], duplicate=True,
                                 format_name=existing["format_name"])
        try:
            result = self._process(path, file_hash)
        except Exception as e:  # one bad file must never stop a batch
            log.exception("unexpected failure while processing %s", path.name)
            result = ProcessResult(DocStatus.FAILED,
                                   error=f"unexpected error: {type(e).__name__}: {e}")
        result.file_hash, result.filename = file_hash, path.name
        self.store.upsert(file_hash, **_record(result, source))
        return result

    # ------------------------------------------------------------------ internals
    def _read(self, path: Path) -> Document:
        try:
            return read_document(path)
        except PasswordRequiredError:
            if not self.passwords:
                raise
        for pw in self.passwords:
            try:
                return read_document(path, pw)
            except WrongPasswordError:
                continue
        raise WrongPasswordError(
            f"none of the {len(self.passwords)} configured password(s) opens {path.name}")

    def _stored_config(self, match: MatchResult, notes: list[Issue]) -> Optional[FormatConfig]:
        if match.kind == MatchKind.UNKNOWN:
            return None
        if not match.config:
            notes.append(Issue("stored_config_missing", Severity.WARNING,
                               f"format {match.format_name!r} has no stored config"))
            return None
        try:
            return FormatConfig.model_validate(match.config)
        except ValidationError as e:
            notes.append(Issue("stored_config_invalid", Severity.WARNING,
                               f"stored config for {match.format_name!r} is invalid: {e}"))
            return None

    def _process(self, path: Path, file_hash: str) -> ProcessResult:
        try:
            doc = self._read(path)
        except (PasswordRequiredError, WrongPasswordError) as e:
            return ProcessResult(DocStatus.PASSWORD_REQUIRED, error=str(e))
        except ReaderError as e:
            return ProcessResult(DocStatus.FAILED, error=str(e))

        match = self.registry.match(compute_fingerprint(doc))
        res = ProcessResult(DocStatus.FAILED, format_name=match.format_name,
                            match_kind=match.kind.value, match_score=round(match.score, 3))
        notes: list[Issue] = []
        statement = report = None

        # 1. Known format: deterministic parse, no LLM.
        stored = self._stored_config(match, notes)
        if stored is not None:
            try:
                statement = parse(doc, stored)
                report = validate(statement)
                if match.kind == MatchKind.DRIFT:
                    notes.append(Issue(
                        "layout_drift", Severity.ERROR,
                        f"layout differs from known format {match.format_name!r} "
                        f"(similarity {match.score:.2f}); parsed with the existing "
                        f"config, please confirm"))
            except ParseError as e:
                statement = report = None
                notes.append(Issue(
                    "stored_config_failed", Severity.WARNING,
                    f"stored config for {match.format_name!r} no longer fits this "
                    f"document: " + "; ".join(e.issues[:3])))

        # 2. Unknown format, or the stored config provably failed: learn once.
        if statement is None:
            learned = learn_and_register(doc, self.registry, self.llm,
                                         self.max_learn_attempts)
            res.used_llm = True
            if not learned.success:
                res.status = DocStatus.LEARNING_FAILED
                res.error = f"{learned.failure_kind}: {learned.failure}"
                res.issues = notes + [Issue("learning_problem", Severity.ERROR, h)
                                      for h in learned.history[:10]]
                return res
            statement, report = learned.statement, learned.report
            res.format_name = learned.config.format_name
            if match.kind == MatchKind.UNKNOWN:
                level = Severity.ERROR if self.review_new_formats else Severity.WARNING
                notes.append(Issue("new_format_learned", level,
                                   f"new format {res.format_name!r} learned from this document"))
            else:
                notes.append(Issue("config_relearned", Severity.ERROR,
                                   f"layout changed: a new version of {res.format_name!r} "
                                   f"was learned from this document, please confirm"))

        statement.source_file_hash = file_hash
        res.statement, res.report = statement, report
        res.issues = notes + report.issues
        needs_review = bool(report.errors) or any(n.severity == Severity.ERROR for n in notes)
        res.status = DocStatus.NEEDS_REVIEW if needs_review else DocStatus.PARSED_OK
        return res


def main(argv=None):
    # python -m app.pipeline samples/pdfs/abc_pms.pdf
    # python -m app.pipeline samples/pdfs            (every PDF in a folder)
    import argparse

    ap = argparse.ArgumentParser(description="Process statement PDFs")
    ap.add_argument("path")
    ap.add_argument("--db", default="statements.db")
    ap.add_argument("--review-new", action="store_true",
                    help="send every newly learned format to human review")
    args = ap.parse_args(argv)

    store, registry = Store(args.db), Registry(args.db)
    pipe = Pipeline(store, registry, review_new_formats=args.review_new)
    target = Path(args.path)
    files = sorted(target.glob("*.pdf")) if target.is_dir() else [target]

    for f in files:
        r = pipe.process_file(f)
        print(f"{f.name:26} {r.status.value:18} format={r.format_name or '-':24} "
              f"match={r.match_kind or '-':8} llm={'yes' if r.used_llm else 'no':3}"
              f"{'  (duplicate)' if r.duplicate else ''}")
        for i in r.issues:
            if i.severity == Severity.ERROR:
                print(f"      ! {i.code}: {i.message}")
        if r.error:
            print(f"      error: {r.error}")
    print("\nsummary:", store.counts())


if __name__ == "__main__":
    main()