# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude Code** (Anthropic), used in my terminal, including its research subagents. It was the only AI tool I used.

## What I used them for
I worked with Claude Code step by step, following a plan we wrote together at the start (normalisers → schema and PII → lead ingestion → event ingestion → derivation → reports and CLI → hardening → write-up).

For each step, Claude proposed a plan, I reviewed and approved it, and then it wrote the code and tests and ran them. Specifically:
- **Understanding the task:** summarised the spec, listed the rules most likely to break, and flagged the ambiguities I'd need to decide.
- **Code:** wrote most of the code in `solution/ledger/` and the tests in `solution/tests/`.
- **Smoke runs:** ran the pipeline on generated data and cross-checked the counts against the generator's own probabilities.
- **Performance work in Step 7:** profiled the large dataset with cProfile and applied the fixes step by step. Each change was timed, and the reports were compared byte for byte against the slower version.
- **Research:** used subagents to collect documentation, videos and a SQLite-vs-Postgres comparison, which I used to choose SQLite.
- **Documents:** drafted `SUBMISSION.md` (decision table, failure modes, results) and this file, which I reviewed and edited.

## What I wrote or decided myself
To be clear: Claude proposed most of the designs and wrote most of the code. My part was choosing between the options, directing the work, and approving each step before it went ahead.
- **Choices I made:**
  - Assignment A, out of the two assignments.
  - SQLite rather than Postgres. I chose it after reading a comparison Claude researched, mainly because the reviewer needs no setup.
- **How the work was run:**
  - I asked for a plan first, then had it done one step at a time, with every spec decision written into `SUBMISSION.md` as it was made.
  - I asked whether to work on a copy of the raw data. We settled on treating the raw files as read-only and keeping all derived state in the database.
- **What I approved:**
  - Each step's plan before any code was written. That includes Claude's proposed database design (the four tables, HMAC plus Fernet for PII, keys in `.env`) and the decisions in the `SUBMISSION.md` table, such as computing orphans and buckets at report time.
  - When Claude raised a choice that was mine to make, I gave the answer: my details, which tools I used, and how to set up git.

## Where the AI was wrong or unhelpful
- **The first version was far too slow at scale.** The design Claude first wrote passed every test but took **418 s** on the large dataset. It used single-row inserts and two secondary indexes that no query used. Profiling showed the time was inside SQLite, and after the fixes it ran in 115 s with identical output.
- **A wrong test.** In Step 7, Claude wrote a test meant to prove the database rejects a duplicate the in-memory check missed. It failed, because the two test files had identical bytes, so the second was skipped by its content hash before the insert ran. The failing test exposed the mistake, and the test was fixed rather than the code.
- **Advice that contradicted the spec.** A research subagent suggested numbering CSV rows with `enumerate(reader, start=2)`, spreadsheet style, and parsing dates with `strptime`. Both would break the spec: row numbers must count only non-blank rows starting at 1, and `strptime` accepts single-digit days like `1/2/2026`. Claude flagged both when it passed the research on to me, and neither was used.
- **Smaller slips:**
  - a smoke-test script printed `sqlite3.Row` objects instead of values and had to be rerun
  - a convoluted PII assertion in the end-to-end test was replaced with a plain regex check

## How I verified AI-generated code
- **Automated tests:** 197 pytest tests (`cd solution && pytest`), covering:
  - each spec rule with table-driven cases
  - the database constraints
  - end to end through the real CLI on a seed the code was never tuned on (seed 7)
- **Idempotency and order-independence:**
  - drop by drop, re-ingesting a drop and "all at once" produce **byte-identical** reports
  - checked on seeds 1, 7, 42, 99, 123 and 2026
- **Cross-checks against the data:** the line and row counts match the generator's own output exactly. Two of the spec's own example values appear in my reports: `0.5625` and `2026-10-14T17:55:24+05:30`.
- **Reading the code myself (in progress):** I haven't yet read every module line by line. Before the interview I'm reading each one and running the CLI myself, so I can explain and change any of it live.
