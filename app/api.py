"""Review API, workbench actions and the web page.

Every /api call needs the access token. Writes are limited to: a human review decision, running
the pipeline on an uploaded file or the mailbox (the same code path as the command line), and
the demo simulator (only when started with --demo). Parsed data and the machine's status are
never edited.

There are no user accounts, no HTTPS and no rate limiting: keep this on localhost.
"""
import hmac
import json
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.ops import OpsError, Workbench
from app.pipeline import DocStatus
from app.registry import Registry
from app.store import Store

WEB = Path(__file__).parent / "web"
ATTENTION = {s.value for s in DocStatus} - {DocStatus.PARSED_OK.value}
RESOLVED = {"approved", "rejected"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def demo_db_ok(db_path: str) -> bool:
    """Demo mode can wipe the database, so it only runs on a file named like a demo database."""
    return "demo" in Path(db_path).name.lower()


def _loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _summary(row) -> dict:
    issues = _loads(row["issues_json"], [])
    errors = [i for i in issues if i["severity"] == "error"]
    statement = _loads(row["statement_json"], None) or {}
    decision = row["review_decision"] if "review_decision" in row.keys() else None
    return {
        "id": row["id"],
        "filename": row["filename"],
        "status": row["status"],
        "needs_attention": row["status"] in ATTENTION and decision not in RESOLVED,
        "review": ({"decision": decision, "note": row["review_note"],
                    "reviewed_at": row["reviewed_at"]} if decision else None),
        "provider": row["provider"],
        "strategy_name": row["strategy_name"],
        "client_id": row["client_id"],
        "statement_type": row["statement_type"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "closing_value": (statement.get("summary") or {}).get("closing_value"),
        "currency": statement.get("currency"),
        "format_name": row["format_name"],
        "match_kind": row["match_kind"],
        "match_score": row["match_score"],
        "used_llm": bool(row["used_llm"]),
        "errors": len(errors),
        "warnings": len(issues) - len(errors),
        "headline": row["error"] or (errors[0]["message"] if errors else None),
        "updated_at": row["updated_at"],
    }


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewIn(_Strict):
    decision: Literal["approved", "rejected", "reopened"]
    note: str = Field(default="", max_length=1000)


class RetryIn(_Strict):
    password: str = Field(default="", max_length=200)


class FetchIn(_Strict):
    days: int = Field(default=2, ge=1, le=60)
    dry_run: bool = False


class SendIn(_Strict):
    may: bool = False


class ResetIn(_Strict):
    confirm: str = Field(max_length=20)


def create_app(store: Store, registry: Registry, token: str,
               bench: Optional[Workbench] = None, demo: bool = False) -> FastAPI:
    if not token or len(token) < 16:
        raise ValueError("the access token must be at least 16 characters")
    token_bytes = token.encode()
    bench = bench or Workbench(store, registry)

    def require_token(x_review_token: str = Header(default="")):
        # compare_digest: constant-time comparison, so timing does not leak the token
        if not hmac.compare_digest(x_review_token.encode("utf-8", "replace"), token_bytes):
            raise HTTPException(401, "missing or wrong access token")

    def require_demo():
        if not demo:
            raise HTTPException(404, "not found")

    # No interactive docs page: it needs external scripts, which our CSP forbids.
    app = FastAPI(title="Statement review", docs_url=None, redoc_url=None, openapi_url=None)
    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    demo_api = APIRouter(prefix="/api/demo",
                         dependencies=[Depends(require_token), Depends(require_demo)])

    @app.exception_handler(OpsError)
    async def ops_error(request, exc: OpsError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # ------------------------------------------------------------ reading
    @api.get("/config")
    def config():
        return {"demo": demo}

    @api.get("/summary")
    def summary():
        counts = store.counts()
        emails: dict = {}
        for r in store.list_emails():
            emails[r["decision"]] = emails.get(r["decision"], 0) + 1
        _, attention = store.query(attention=True, limit=1)
        return {
            "total": sum(counts.values()),
            "documents": counts,
            "needs_attention": attention,
            "reviewed": store.review_counts(),
            "llm_documents": sum(1 for r in store.list() if r["used_llm"]),
            "emails": emails,
            "formats": len(registry.all_formats()),
        }

    @api.get("/documents")
    def documents(status: Optional[str] = Query(None), attention: bool = Query(False),
                  q: Optional[str] = Query(None, max_length=100),
                  limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0)):
        if status is not None and status not in {s.value for s in DocStatus}:
            raise HTTPException(422, f"unknown status {status!r}")
        rows, total = store.query(status=status, attention=attention, q=q,
                                  limit=limit, offset=offset)
        return {"items": [_summary(r) for r in rows], "total": total,
                "limit": limit, "offset": offset}

    @api.get("/documents/{doc_id}")
    def document(doc_id: int):
        row = store.get_with_review(doc_id)
        if row is None:
            raise HTTPException(404, "no such document")
        detail = _summary(row)
        issues = _loads(row["issues_json"], [])
        detail.update(
            source=row["source"],
            error=row["error"],
            issues=sorted(issues, key=lambda i: i["severity"] != "error"),   # errors first
            validation=_loads(row["validation_json"], None),
            statement=_loads(row["statement_json"], None),
            review_history=[{"decision": h["decision"], "note": h["note"],
                             "reviewed_at": h["reviewed_at"]}
                            for h in store.review_history(doc_id)],
            email=None,
        )
        source = row["source"] or ""
        if source.startswith("email:"):
            mail = store.get_email(source[len("email:"):])
            if mail is not None:
                detail["email"] = {"subject": mail["subject"], "sender": mail["sender"],
                                   "received": mail["received"]}
        return detail

    @api.get("/emails")
    def emails(include_no_pdf: bool = Query(False), limit: int = Query(100, ge=1, le=500)):
        out = []
        for r in store.list_emails():
            if r["decision"] == "no_pdf" and not include_no_pdf:
                continue
            out.append({"id": r["id"], "subject": r["subject"], "sender": r["sender"],
                        "received": r["received"], "decision": r["decision"],
                        "attachments": _loads(r["attachments_json"], [])})
        return out[:limit]

    @api.get("/formats")
    def list_formats():
        return [{"id": f["id"], "name": f["name"], "version": f["version"],
                 "created_at": f["created_at"],
                 "label_count": len(_loads(f["labels_json"], [])),
                 "has_config": f["config_json"] is not None} for f in registry.all_formats()]

    @api.get("/formats/{format_id}")
    def format_detail(format_id: int):
        for f in registry.all_formats():
            if f["id"] == format_id:
                return {"id": f["id"], "name": f["name"], "version": f["version"],
                        "created_at": f["created_at"],
                        "config": _loads(f["config_json"], None),
                        "labels": _loads(f["labels_json"], [])}
        raise HTTPException(404, "no such format")

    # ------------------------------------------------------------ human review
    @api.post("/documents/{doc_id}/review")
    def review(doc_id: int, body: ReviewIn):
        row = store.get_with_review(doc_id)
        if row is None:
            raise HTTPException(404, "no such document")
        status, current = row["status"], row["review_decision"]
        resolved = current in RESOLVED
        if status not in ATTENTION:
            raise HTTPException(409, "only flagged documents can be reviewed")
        if body.decision == "approved" and status != "needs_review":
            raise HTTPException(409, "there is no parsed data to approve; dismiss it instead")
        if body.decision == "reopened" and not resolved:
            raise HTTPException(409, "nothing to reopen")
        if body.decision in RESOLVED and resolved:
            raise HTTPException(409, "already reviewed; reopen it first")
        store.add_review(doc_id, body.decision, body.note.strip())
        return document(doc_id)

    # ------------------------------------------------------------ workbench actions
    @api.post("/process")
    async def process(request: Request, x_filename: str = Header(default="statement.pdf"),
                      x_pdf_password: str = Header(default="")):
        # The PDF is the raw request body. The file name and password travel in headers
        # (URL-encoded) and never in the URL, because the server logs URLs.
        limit = bench.max_upload
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise OpsError("The file is larger than the upload limit.", 413)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise OpsError("The file is larger than the upload limit.", 413)
        name = unquote(x_filename)[:200] or "statement.pdf"
        return await run_in_threadpool(bench.process_upload, name, bytes(data),
                                       unquote(x_pdf_password)[:200])

    @api.post("/documents/{doc_id}/retry")
    def retry(doc_id: int, body: RetryIn):
        return bench.retry(doc_id, body.password)

    @api.post("/email/fetch")
    def email_fetch(body: FetchIn):
        return bench.fetch_email(body.days, body.dry_run)

    # ------------------------------------------------------------ demo simulator
    @demo_api.post("/prepare")
    def demo_prepare():
        return bench.prepare_demo()

    @demo_api.get("/scenarios")
    def demo_scenarios():
        return bench.scenario_list()

    @demo_api.post("/scenarios/{key}/run")
    def demo_run(key: str):
        return bench.run_scenario(key)

    @demo_api.post("/inbox/{which}")
    def demo_inbox(which: str):
        return bench.deliver_inbox(which)

    @demo_api.post("/send-test-email")
    def demo_send(body: SendIn):
        return bench.send_test_emails(body.may)

    @demo_api.post("/reset")
    def demo_reset(body: ResetIn):
        return bench.reset(body.confirm)

    app.include_router(api)
    app.include_router(demo_api)
    return app


def main(argv=None):
    # python -m app.api --db demo.db --demo
    import argparse
    import os
    import secrets

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="Statement review page and workbench")
    ap.add_argument("--db", default="statements.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--demo", action="store_true",
                    help="enable the demo simulator and reset (needs a database named *demo*)")
    args = ap.parse_args(argv)

    if args.demo and not demo_db_ok(args.db):
        raise SystemExit("--demo can wipe the database, so it needs a database whose file name "
                         "contains 'demo', for example: --db demo.db")
    token = os.environ.get("REVIEW_TOKEN", "").strip()
    generated = not token
    if generated:
        token = secrets.token_urlsafe(24)
    elif len(token) < 16:
        raise SystemExit("REVIEW_TOKEN in .env must be at least 16 characters")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: this page uses plain HTTP and a single shared token and can run the "
              "pipeline. Do not expose it beyond your own machine.")
    store, registry = Store(args.db), Registry(args.db)
    bench = Workbench(store, registry, data_dir=args.data_dir)
    print(f"Review page: http://{args.host}:{args.port}")
    print(f"Access token: {token}" if generated else "Access token: REVIEW_TOKEN from .env")
    if args.demo:
        print(f"Demo mode ON (database: {args.db}). Reset will wipe it.")
    uvicorn.run(create_app(store, registry, token, bench=bench, demo=args.demo),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()