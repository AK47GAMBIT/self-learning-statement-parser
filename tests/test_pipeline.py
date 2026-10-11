import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.llm import LLMNotConfiguredError
from app.pipeline import DocStatus, Pipeline
from app.registry import MatchKind, Registry
from app.store import Store
from app.validation import validate
from samples.generate import generate_all, render_pdf
from samples.next_month import abc_may

CONFIGS = Path(__file__).parent.parent / "configs"


class FakeLLM:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)       # an unexpected extra call fails the test
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return root


def good(name):
    return (CONFIGS / f"{name}.json").read_text()


def codes(result):
    return {i.code for i in result.issues}


def make(replies=(), passwords=(), registry=None, store=None, **kw):
    llm = FakeLLM(replies)
    pipe = Pipeline(store or Store(), registry or Registry(), llm=llm,
                    passwords=list(passwords), **kw)
    return pipe, llm


def may_pdf(tmp_path, name="may.pdf", tamper=False, qty_header="Qty"):
    path = tmp_path / name
    render_pdf("abc_pms", abc_may(tamper), path, qty_header=qty_header)
    return path


def test_may_sample_is_internally_consistent():
    assert validate(abc_may()).errors == []


# ---------------------------------------------------------------- the core constraint
def test_unknown_format_is_learned_and_stored(samples):
    pipe, llm = make([good("abc_pms")])
    r = pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    assert r.status == DocStatus.PARSED_OK and r.used_llm
    assert r.match_kind == "unknown" and r.format_name == "abc_portfolio_managers"
    assert len(llm.prompts) == 1 and len(pipe.registry.all_formats()) == 1
    assert pipe.store.get_by_hash(r.file_hash)["status"] == "parsed_ok"


def test_second_statement_of_known_format_makes_zero_llm_calls(samples, tmp_path):
    pipe, llm = make([good("abc_pms")])              # exactly ONE reply available
    first = pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    second = pipe.process_file(may_pdf(tmp_path))
    assert first.used_llm and len(llm.prompts) == 1
    assert second.status == DocStatus.PARSED_OK and not second.used_llm
    assert second.match_kind == "match" and len(llm.prompts) == 1
    assert second.statement.period_start == date(2026, 5, 1)
    assert second.statement.source_file_hash == second.file_hash
    assert sum(pipe.store.counts().values()) == 2


def test_duplicate_file_is_skipped(samples):
    pipe, llm = make([good("abc_pms")])
    path = samples / "pdfs" / "abc_pms.pdf"
    pipe.process_file(path)
    again = pipe.process_file(path)
    assert again.duplicate and again.status == DocStatus.PARSED_OK
    assert len(llm.prompts) == 1 and pipe.store.counts() == {"parsed_ok": 1}


def test_known_format_with_bad_numbers_goes_to_review_without_llm(samples, tmp_path):
    pipe, llm = make([good("abc_pms")])
    pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    r = pipe.process_file(may_pdf(tmp_path, "bad.pdf", tamper=True))
    assert r.status == DocStatus.NEEDS_REVIEW and not r.used_llm
    assert "holding_arithmetic" in codes(r) and len(llm.prompts) == 1


# ---------------------------------------------------------------- layout changes
def test_renamed_column_triggers_relearn_and_review(samples, tmp_path):
    data = json.loads(good("abc_pms"))
    for col in data["holdings"]["columns"]:
        if col["header"] == "Qty":
            col["header"] = "Units"
    pipe, llm = make([good("abc_pms"), json.dumps(data)])
    pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    r = pipe.process_file(may_pdf(tmp_path, "units.pdf", qty_header="Units"))
    assert r.status == DocStatus.NEEDS_REVIEW and r.used_llm
    assert {"stored_config_failed", "config_relearned"} <= codes(r)
    assert [f["version"] for f in pipe.registry.all_formats()] == [1, 2]


class DriftRegistry(Registry):
    """Reports every close match as DRIFT, to test the pipeline's drift policy."""

    def match(self, fp):
        r = super().match(fp)
        return replace(r, kind=MatchKind.DRIFT, score=0.7) if r.kind == MatchKind.MATCH else r


def test_drift_with_working_config_goes_to_review(samples, tmp_path):
    pipe, llm = make([good("abc_pms")], registry=DriftRegistry())
    pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    r = pipe.process_file(may_pdf(tmp_path))
    assert r.status == DocStatus.NEEDS_REVIEW and "layout_drift" in codes(r)
    assert not r.used_llm and len(llm.prompts) == 1


def test_review_new_formats_flag(samples):
    pipe, _ = make([good("abc_pms")], review_new_formats=True)
    r = pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    assert r.status == DocStatus.NEEDS_REVIEW and "new_format_learned" in codes(r)


# ---------------------------------------------------------------- passwords
def test_password_is_found_in_the_list(samples):
    pipe, _ = make([good("xyz_wealth")], passwords=["wrong", "demo1234"])
    assert pipe.process_file(samples / "pdfs" / "xyz_wealth.pdf").status == DocStatus.PARSED_OK


def test_locked_pdf_without_password_never_calls_llm(samples):
    pipe, llm = make()
    r = pipe.process_file(samples / "pdfs" / "xyz_wealth.pdf")
    assert r.status == DocStatus.PASSWORD_REQUIRED and "password" in r.error
    assert llm.prompts == []


def test_wrong_passwords_are_reported(samples):
    pipe, _ = make(passwords=["nope", "also-nope"])
    r = pipe.process_file(samples / "pdfs" / "xyz_wealth.pdf")
    assert r.status == DocStatus.PASSWORD_REQUIRED and "none of the 2" in r.error


def test_password_failure_can_be_retried_later(samples):
    store, registry = Store(), Registry()
    path = samples / "pdfs" / "xyz_wealth.pdf"
    Pipeline(store, registry, llm=FakeLLM(), passwords=[]).process_file(path)
    assert store.counts() == {"password_required": 1}
    retry = Pipeline(store, registry, llm=FakeLLM([good("xyz_wealth")]), passwords=["demo1234"])
    assert retry.process_file(path).status == DocStatus.PARSED_OK
    assert store.counts() == {"parsed_ok": 1}                 # same row, updated


# ---------------------------------------------------------------- bad input, bad LLM
def test_corrupt_and_scanned_files_fail_without_llm(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank))
    c.rect(50, 50, 100, 100)
    c.save()
    pipe, llm = make()
    assert pipe.process_file(junk).status == DocStatus.FAILED
    scanned = pipe.process_file(blank)
    assert scanned.status == DocStatus.FAILED and "no extractable text" in scanned.error
    assert llm.prompts == []


def test_learning_failure_is_recorded_and_retryable(samples):
    store, registry = Store(), Registry()
    path = samples / "pdfs" / "abc_pms.pdf"
    down = Pipeline(store, registry,
                    llm=FakeLLM([LLMNotConfiguredError("GEMINI_API_KEY is not set")]),
                    passwords=[])
    r = down.process_file(path)
    assert r.status == DocStatus.LEARNING_FAILED and "llm_not_configured" in r.error
    assert registry.all_formats() == []                       # nothing half-stored

    up = Pipeline(store, registry, llm=FakeLLM([good("abc_pms")]), passwords=[])
    assert up.process_file(path).status == DocStatus.PARSED_OK
    assert store.counts() == {"parsed_ok": 1}


class BrokenRegistry(Registry):
    def match(self, fp):
        raise RuntimeError("boom")


def test_unexpected_error_is_contained(samples):
    pipe, _ = make(registry=BrokenRegistry())
    r = pipe.process_file(samples / "pdfs" / "abc_pms.pdf")
    assert r.status == DocStatus.FAILED and "boom" in r.error
    assert pipe.store.counts() == {"failed": 1}