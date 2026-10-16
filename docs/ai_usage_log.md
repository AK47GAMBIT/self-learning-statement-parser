# AI usage log

Tool: Claude (claude.ai chat), used for design discussion, code and tests. The full
conversation is in `docs/ai_chat_history.*`. I ran every command and test myself and pasted the
results back; the AI never ran any of this code.

## Where it helped most
- Explaining PMS/AIF statements and the validation arithmetic I did not know.
- Laying out the build order so I could test the engine with hand-written configs before adding
  the LLM. That made it possible to tell an engine bug from a bad LLM reply.
- Writing boilerplate (Pydantic models, FastAPI, test fixtures) and generating messy test PDFs.
- Thinking of failure modes and security issues I had not considered (stored XSS from email
  subjects, forged prompt delimiters, CSRF, path traversal in attachment names).
- Drafting the documents from the history of what actually went wrong.

## How I steered it
- I chose the hybrid config approach after comparing the three options, and rejected generated code.
- I asked why there was one AIF and three PMS formats, which led to five formats (3 PMS, 2 AIF).
- I used Gemini because it is free.
- I asked to fix the limitations I did not like (currency in the fingerprint, the plain review
  page, auth, paging, search, review actions) and agreed to skip the consolidated view.

## Where it misled me (all found by running tests or reading output)
| # | What the AI got wrong | How it was found | Fix |
|---|---|---|---|
| 1 | Code comments in the sample data stated wrong totals (it added numbers in its head) | Recomputed from the generated PDFs | Corrected the comments |
| 2 | The XYZ answer key stored fees and outflows positive, while the PDF printed them in brackets | Compared the PDF text with `expected.json` | New `number_abs` transform |
| 3 | The reader caught pdfminer's exceptions, but pdfplumber wraps them in `PdfminerException` | 3 failing reader tests | Unwrap `e.args[0]` |
| 4 | The first fingerprint passed its tests while storing the strategy name and splitting titles into cells | Reading the printed label list | Keys-only labels, join short headings |
| 5 | Currency text (`Cost (INR)`) in fingerprint labels | I noticed it in the dumps | Strip currency tokens |
| 6 | A test asserted a message ("Units") that the code never produced | A failing test | `_find_header` now names missing columns |
| 7 | The config schema did not check table column field names, so an invented field would be silently ignored | Found while designing the learner | Validate against the schema |
| 8 | The default model name (`gemini-2.5-flash`) had been retired | Live 404 | Model in `.env`, new default |
| 9 | My test claimed searching for `_` returns nothing; the sample names contain underscores | A failing test | Rewrote the test (the code was right) |
| 10 | Wrapped-name detection looked one line ahead, so a three-line name ended the table | A failing edge-case test | `_leads_to_data` follows the chain |
| 11 | The review page failed with "Cannot read properties of undefined" | I was still running the old server | Restarted the server (a version mismatch, not a code bug) |

Lessons: tests only prove what they assert, so I read output and dumps as well; failure-path
tests found more bugs than happy-path ones; the AI states assumptions about libraries and model
names confidently, so each needs checking against reality.

