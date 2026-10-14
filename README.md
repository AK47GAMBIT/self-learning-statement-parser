# Self-learning PMS/AIF statement parser

Reads PMS and AIF statements from email, extracts holdings, transactions and summary
figures into one standard structure, and **uses an LLM only the first time it sees a
statement layout**. Every later statement in a known layout is parsed by plain code,
with no LLM call.

Built by Aniket Khandelwal for the Activv Money internship assignment.
Notes: [design note](docs/design_note.md) · [known limitations](docs/limitations.md) ·
[AI usage log](docs/ai_usage_log.md)

## How it works

```
Gmail (IMAP, read-only) ──► classify: is this a statement email?
                                   │ only accepted PDFs continue
                                   ▼
PDF ─► hash (already done? skip) ─► read (try passwords) ─► fingerprint ─► registry
                                                                              │
                      MATCH / DRIFT: parse with the STORED config (no LLM) ◄─┤
                      UNKNOWN: ask the LLM once for a config ─► 4 gates ─► store
                                   │
                                   ▼
                     validate the numbers ─► parsed_ok / needs_review ─► SQLite ─► review page
```

**What is "learned"?** A JSON config (validated by Pydantic) describing one layout: regexes
for header fields, row labels for summary values, and column headers for tables, each paired
with a named transform from a fixed whitelist (`number`, `number_abs`, `number_drcr`,
`percent`, date formats, ...). A config is data, never code, so nothing the LLM returns can
execute.

**Four gates before a learned config is stored:**
1. the reply is a single valid JSON object
2. it satisfies the config schema (known keys, transforms and fields; valid regexes)
3. the engine can parse the very document it was learned from, without errors
4. validation finds no errors in the parsed numbers (holdings + cash = closing value,
   quantity x price = value, ISIN checksums, weights, reconciliation, ...)

If a gate fails, the problems are sent back to the LLM (3 attempts in total). Nothing is
stored unless all four gates pass.

**When the LLM is called:** (a) the format has never been seen, or (b) a known format's
stored config provably no longer fits the document (the layout changed). A normal parse of
a known format never calls it. `tests/test_pipeline.py` proves this with a fake LLM that
holds exactly one reply.

## Requirements coverage

| Brief | Status |
|---|---|
| Email ingestion | Done: read-only IMAP, scored classifier, every decision recorded |
| PDF, password-protected PDFs | Done (multi-page tables, repeated headers/footers, wrapped names) |
| Excel / CSV ("a plus") | **Not built** |
| Format recognition, learning, deterministic parsing | Done |
| Standard output, validation | Done |
| Review interface | Done: web page + API with token, search, paging, approve/reject/reopen |
| Stretch: layout-change detection | Done (DRIFT status; changed layouts are re-learned and flagged) |
| Stretch: several statements in one email | Done (each PDF handled separately) |
| Stretch: consolidated view per client | **Not built** (see design note) |

## Setup

Python 3.10+ (developed on 3.13).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # then edit .env
python -m app.preflight            # checks the setup (add --llm --mail for live checks)
```

### Configuration (`.env`, never committed)

| Variable | Needed for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | learning new formats | Free key from Google AI Studio |
| `LLM_MODEL` | learning | Default `gemini-3.8-flash`. Models get retired, so this is configurable |
| `PDF_PASSWORDS` | locked PDFs | Comma-separated, tried in order (e.g. PAN, date of birth) |
| `REVIEW_TOKEN` | review page | 16+ characters. If unset, a random token is printed at start |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | email ingestion | Dedicated test account, app password (2-Step Verification on) |
| `STATEMENT_SENDERS` | email (optional) | Comma-separated senders or domains treated as trusted sources |
| `IMAP_HOST`, `MAILBOX_FOLDER` | email (optional) | Defaults `imap.gmail.com`, `INBOX` |

The demo password of the sample locked PDF is `demo1234` (fake).

## Run it

```bash
python -m samples.generate          # 5 synthetic statements + answer keys
python -m samples.next_month        # a "next month" ABC statement + two broken variants
python -m samples.stress            # messy ABC variants: 3 pages, wrapped names, footers

python -m app.pipeline samples/pdfs --db statements.db       # a folder, or a single file
python -m app.pipeline <path> --db statements.db --review-new  # review every new format

python -m app.email_ingest --days 2 --dry-run      # classify only, change nothing
python -m app.email_ingest --days 2                # fetch, classify, process
python -m samples.send_test_email [--may]          # put test emails in your test inbox

python -m app.api --db statements.db               # review page, http://127.0.0.1:8000
python -m app.learner samples/pdfs/abc_pms.pdf     # learn one format and print the report
python -m samples.export_outputs --db statements.db   # write per-document JSON outputs
```

Failed documents (`password_required`, `failed`, `learning_failed`) are retried by running
the pipeline over them again. Saved email attachments are in `data/attachments`.

## Statuses

| Status | Meaning |
|---|---|
| `parsed_ok` | Parsed and every runnable check passed |
| `needs_review` | Parsed, but a check failed, the layout drifted, or a config was re-learned |
| `password_required` | Encrypted and none of the configured passwords opens it |
| `failed` | Unreadable, scanned (no text layer), or an unexpected error |
| `learning_failed` | New format, but the LLM was unavailable or produced no valid config |

Validation checks that cannot run (for example the PDF does not print inflows) are reported
as *skipped*, never hidden. A reviewer can approve, reject or reopen a flagged document on
the page. That never changes the stored status or data.

## LLM provider and failure behaviour

Provider: **Google Gemini** (`google-genai`), model set by `LLM_MODEL`.

| Failure | Behaviour |
|---|---|
| No API key | `learning_failed` immediately, clear message, no network call |
| Network error, 429, 5xx | Retried 4 times with exponential backoff, then `learning_failed` |
| Other API errors (401, 403, 404) | `learning_failed` immediately (retrying cannot help) |
| Reply invalid or fails a gate | Problems fed back to the LLM, up to 3 attempts, then `learning_failed` |

Nothing is stored for a failed learning attempt. The text of the first document in each new
layout is sent to the provider (see the limitations note).

## Project layout

```
app/
  reader.py        PDF -> lines -> cells with x-positions; passwords; page furniture
  fingerprint.py   stable "label" set describing a layout; Jaccard similarity
  registry.py      SQLite store of learned formats (config + fingerprint, versioned)
  config_spec.py   what a legal config may contain (strict; rejects anything else)
  transforms.py    whitelist of value parsers the config may name
  engine.py        generic, provider-agnostic parser: Document + config -> Statement
  validation.py    arithmetic consistency checks (errors, warnings, skipped)
  llm.py           Gemini client behind a tiny interface (retries, clear failures)
  learner.py       prompt + the four gates + retry loop
  pipeline.py      orchestration, statuses, dedup by file hash
  store.py         SQLite store: documents, emails, human reviews
  email_ingest.py  read-only IMAP, statement-email classifier, attachment handling
  api.py, web/     review API and page
  preflight.py     pre-demo setup check
samples/           synthetic generator, variants, stress files, outputs/
configs/           hand-written reference configs (tests); learned/ = written by the LLM
tests/             pytest suite (no network or API key needed)
docs/              design note, limitations, AI usage log
```

At runtime configs are read from the registry in SQLite, not from `configs/`.

## Tests

```bash
python -m pytest -q
```

No network or key needed: the LLM and the mailbox are replaced by fakes. All test data is
synthetic and written by the same author as the configs (see limitations).

## Data and safety

All sample data is synthetic: fictional providers, invented client IDs, valid-checksum ISINs,
a fake password. No real client data is in this repository. `.env`, databases and downloaded
attachments (`data/`) are gitignored.
