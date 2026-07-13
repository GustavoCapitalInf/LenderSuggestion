# Minimum-3 Bank Statement Accumulation — Design

## Problem

Today, each `POST /bank-statement` for a `client_id` **overwrites** the previous
bank statement (`bs.json` is written with `write_text`, last-write-wins) and
**immediately** launches a Gemini lender analysis whenever an application is
already present. This causes three problems:

1. **Statements are lost.** A client who sends bank statements across multiple
   uploads (e.g. one per day) only ever has their *latest* statement reflected —
   earlier months are discarded.
2. **Premature analysis.** The suggestion runs on a single statement. The
   business never funds a deal on fewer than 3 months of statements, so a
   1- or 2-statement run is meaningless and wastes a Gemini call + fires a
   webhook to ORBIT with an incomplete picture.
3. **Race / stale-result / duplicate-webhook bugs.** Two near-simultaneous
   uploads spawn two `run_analysis` threads on the same job; whichever finishes
   last silently wins. The `/bank-statement` path (unlike `/application`) never
   clears a stale `result.json`, so `GET /job/<id>` reports the old `complete`
   result during reprocessing.

## Goal

Make MatchLender **accumulate** every bank statement a client sends (keyed by
`client_id`, never overwriting), and only run the lender-match analysis once the
job has at least **3** statements. When more statements arrive later, fold them
in and re-run.

## Scope

- **MatchLender (`app.py`) only.** No change to the OCR repo.
- Built on the current file-based storage (`jobs/<client_id>/`). Independent of
  the pending Neon Postgres migration; that migration will later adapt to the
  new list shape.

## Decisions (from brainstorming)

- **Accumulate, never overwrite.** Each statement is kept.
- **One POST = one statement.** The team uploads one PDF at a time, so
  MatchLender counts the POSTs it receives per client. (Forward-compatible: an
  optional `statement_count` in the payload is honored, default 1.)
- **Minimum = 3.** Analysis does not run until a job has ≥ 3 statements. Named
  constant `MIN_BANK_STATEMENTS = 3`.
- **Per-month combination.** Revenue, deposits, avg daily balance, and pos_count
  are **averaged** across statements; nsf_count and loan_count are **summed**.
- **Re-run each time.** Once ≥ 3, every additional statement folds in, re-runs
  the analysis over the full set, overwrites the result, and re-fires the
  ORBIT / Power Automate webhook with the fresher picture.
- **Same client_id → same job.** Accumulation is keyed entirely by
  `client_id` / `clientCode`; a later upload with the same id appends to the same
  `jobs/<client_id>/` folder. A *different* id would create a separate job — the
  feature depends on the OCR/ORBIT flow reusing the same client identifier
  across a client's uploads (which it already does).

## Design

### 1. Storage shape

`bs.json` changes from a single metrics object to a **JSON array**, one entry per
statement POST (the posted body, minus `client_id`):

```json
[
  { "summary_metrics": { "nsf_count": 1, "total_revenue": 42000, "avg_daily_balance": 3100 } },
  { "summary_metrics": { "nsf_count": 0, "total_revenue": 39500, "avg_daily_balance": 2900 } },
  { "summary_metrics": { "nsf_count": 2, "total_revenue": 45100, "avg_daily_balance": 3400 } }
]
```

A legacy `bs.json` that is still a single object is read as a 1-element list, so
existing jobs don't break. `app.json`, `result.json`, `error.json` are unchanged.

### 2. `POST /bank-statement` handler

Under a **per-client lock**:
1. Pop `client_id`; `statement_count` from payload defaults to 1.
2. Load the existing statement list (`[]` if none), append the new statement(s),
   write `bs.json`.
3. `count = len(list)` (sum of `statement_count`s).
4. If `app.json` exists **and** `count >= MIN_BANK_STATEMENTS`: launch analysis
   (respecting the in-flight guard, below); respond `status: "processing"`.
5. Otherwise respond `status: "waiting_for_bank_statements"` with
   `received: count, required: MIN_BANK_STATEMENTS`.

### 3. `POST /application` handler

Unchanged except the "bank statement already present" trigger condition becomes
`count >= MIN_BANK_STATEMENTS` (instead of "bs.json exists").

### 4. Combine logic

New `combine_statements(list) -> dict` returns a single `summary_metrics`-shaped
dict the existing `extract_monthly_rev` / `extract_bs_metrics` / `build_prompt`
already consume:

- **Averaged:** `total_revenue` (or `total_credits`), `total_deposits`,
  `avg_daily_balance`, `pos_count`.
- **Summed:** `nsf_count`, `loan_count`.
- Adds `statement_count: N`.
- Missing keys handled gracefully (average only over statements that have the
  field; `None` if none do).

The combined dict is what flows into the existing analysis functions. `N` is also
surfaced in the Gemini prompt ("based on N months of statements") so the model
knows the basis.

### 5. `run_analysis`

Takes the **full current statement list**, combines it, then proceeds exactly as
today (Gemini call → `result.json` → webhook). Re-runs always reflect the latest
full set.

### 6. Concurrency / in-flight guard

Per-`client_id` lock serializes append + trigger. An "analysis in progress" flag
per client prevents overlapping Gemini calls: if a statement arrives while an
analysis is running, a `pending_rerun` flag is set instead of spawning a second
thread; when the running analysis finishes, it re-runs once if the flag is set.
This also fixes today's race / stale-result / duplicate-launch bugs.

### 7. Status + API responses

- New/renamed status `waiting_for_bank_statements` (replaces singular
  `waiting_for_bank_statement`).
- `GET /job/<id>` while waiting returns
  `{"clientCode": ..., "status": "waiting_for_bank_statements", "received": N, "required": 3}`.
- `processing`, `complete`, `error`, `waiting_for_application` unchanged.
- `/queue` counts updated to include the renamed waiting status.
- Completed `result.json` responses and the webhook payload are **byte-for-byte
  unchanged**.

### 8. Startup recovery

`_recover_orphaned_jobs` re-launches a job only if `app.json` present **and**
statement count ≥ `MIN_BANK_STATEMENTS` **and** no `result.json`/`error.json`
yet — mirroring the new trigger rule.

## Out of scope

- Deduplicating identical statement uploads (each POST is trusted as one
  statement).
- Pairing uploads by anything other than `client_id` (e.g. business name / EIN).
- The Neon Postgres migration (separate plan; will adapt to the list shape
  later).
- Any change to the OCR repo.

## What does not change

Endpoint URLs, the completed-result response shape, the webhook payload, and the
OCR side all stay the same. This is purely MatchLender's accumulation + gating
behavior.
