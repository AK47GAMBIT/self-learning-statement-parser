# Design note

## The problem in one paragraph
Investors receive PMS and AIF statements from many providers, each with its own layout. The
system must read them from email into one structure, calling an LLM only when it meets a layout
it has never seen. Later statements in a known layout must be parsed by deterministic code.

## The four questions from the brief

**1. How does the system recognise a known format?**
A *fingerprint*: the set of label-like lines (provider name, headings, row labels, table header
rows) with everything that changes monthly removed. Lines containing digits are dropped (dates,
amounts, holdings, client IDs), so next month's statement has the same fingerprint. Similarity is
Jaccard over the label sets: 0.85+ is a match, 0.50-0.85 is drift, below 0.50 is unknown.

**2. What is stored when it learns, and why that representation?**
A validated JSON config: regexes for header fields, row labels for summary values, column headers
for tables, and a named transform per value. Alternatives I rejected:
- *LLM-generated Python code.* Maximum flexibility, but executing model output is a security risk
  (a PDF can inject instructions), it is hard to audit, and nothing guarantees determinism.
- *An LLM call per statement.* Slow, costly, and different answers for the same input. This is
  what the brief rules out.
- *Hashing the whole text as the fingerprint.* It changes every month.

The cost of the choice: a layout my engine cannot express (values above labels, two tables side
by side) cannot be learned. That fails loudly and goes to review.

**3. Without an AI, how does the system know a parse is correct?**
Two layers. The engine refuses to guess: a missing column, a missing label, two cells in one
column, or an unreadable number raises an error naming the problem. Then validation checks the
numbers against each other: holdings + cash = closing value, quantity x price = market value,
weights, units x NAV, opening + flows - fees + gain = closing, ISIN checksums, dates within the
period. Errors send the statement to `needs_review`. Checks that cannot run are reported as
*skipped*, never as passed.

**4. What happens when a known provider quietly changes its layout?**
Three defences. A renamed column makes the stored config fail in the engine; the pipeline
re-learns a new config version (the old one is kept) and flags the document for review. A
moderate layout change gives a DRIFT fingerprint: if the stored config still parses, the result
is flagged `layout_drift`. A change that parses but misreads is caught by validation. The
fingerprint alone cannot see a single renamed column (the score barely moves), which is why the
engine and validation are the real safety nets.

## Architecture
reader -> fingerprint -> registry -> (stored config | learner) -> engine -> validation ->
pipeline/store -> API and page. The email module feeds the pipeline. See the README diagram.
Each module has one job and a narrow interface; the reader is the only file that touches the PDF
library, and `llm.py` is the only file that touches the provider.

## Where the LLM is, and how it is kept in check
It writes a config for an unseen layout, and nothing else. Four gates before storage: valid JSON,
schema-valid, parses the very document it was learned from, and the parsed numbers validate.
Problems go back to the LLM (3 attempts). The system, not the LLM, chooses the stored name. The
statement text is delimited and marked untrusted. Failure handling is in the README.

## Assumptions (the brief was silent)
- A *format* is one provider's layout, identified by its fingerprint.
- Statements are text PDFs; scanned PDFs are out of scope.
- Passwords come from a configured list, tried in order (real ones are often a PAN or date of birth).
- Fees and capital outflows are stored as positive amounts; other signs are stored as printed.
- Currency defaults to INR unless the statement states one.
- A statement email is recognised by weighted keyword signals plus an optional sender allow-list.
  Every skip is recorded with its reasons.
- Newly learned formats are accepted automatically (flagged with a warning) unless `--review-new`.
- A re-learned config for a known provider always goes to human review.
- Gmail via IMAP and an app password, opened read-only, on a dedicated test account.
- All data is synthetic: five fictional providers (3 PMS, 2 AIF), one password-protected.

## Trade-offs worth knowing
| Decision | Gain | Cost |
|---|---|---|
| Config instead of code | Safe, auditable, deterministic | Limited to layouts the engine can express |
| Strict config schema | An LLM's invented keys fail loudly | A few false rejections |
| Fail loudly on ambiguity | No silent wrong numbers | More statements go to review |
| Review decisions in their own table | The machine's verdict and its history are never lost | An extra join |
| Read-only mailbox and API | Bugs cannot damage mail or data | No write-back features |
| Token auth, localhost | Simple and honest | Not production security |
| Polling, not websockets | Much less machinery | Up to 20 seconds of lag |

## Testing approach
Golden files (output equals a generated answer key), failure paths (wrong password, scanned PDF,
renamed column, LLM down), corruption tests (validation must catch them), security tests (hostile
filenames and subjects, forged prompt delimiters, token required), and a test that counts LLM
calls to prove a known format makes none. The LLM and mailbox are replaced by fakes. The weakness:
all data is synthetic and written by me, so the suite shows consistency, not real-world coverage.

## How the design would absorb changes
- *Excel input:* a second reader that produces the same `Document` (rows as lines, columns as
  cells with synthetic positions); fingerprint, engine and validation are reused.
- *Another LLM:* implement `complete(system, prompt)` for it; nothing else changes.
- *Gmail OAuth:* replace `ImapMailbox` with a class that has the same three methods.
- *A new field:* add it to the schema. Allowed summary fields, column fields and the prompt are
  derived from the schema, so the config rules and the prompt update themselves.
- *Consolidated view:* add an investor table linking provider client IDs (e.g. by PAN) and
  aggregate over it. I did not build it because I would have had to invent the identity data.
- *Corrections:* an edited-values table with an audit trail, after real authentication.
- *Privacy:* redact identifiers before text goes to the LLM.