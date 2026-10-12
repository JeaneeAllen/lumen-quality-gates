# AI Session Notes

Log of the AI-assisted build of Lumen Quality Gates (Claude Code).

## Constraints given to the assistant

- Python, CSV, pytest, PyYAML only. No Docker, dbt, database server, or JS framework.
- Build in small stages; explain files, command, success, and failure after each.
- No commits, pushes, PRs, or merges unless asked.
- Do not remove failed records, silently deduplicate, weaken tests, or use `continue-on-error`.
- Synthetic data only.

## Stage 0 — Scaffolding (2026-10-11)

- Created `requirements.txt` (pytest 8.3.5, PyYAML 6.0.2), `.gitignore`,
  `release_label.txt` (`staging-v1`), `README.md` skeleton, this file, and `evidence/.gitkeep`.
- Decision: `evidence/` is committed (it is class proof); `data/`, `demo_data/`, `site/`,
  and `.venv/` are ignored.

## Stage 1 — Data generator (2026-10-11)

- Created `scripts/generate_data.py`, `tests/conftest.py`, `tests/test_generate_data.py`.
- Reproducibility: `random.Random(seed)` plus fixed calendar dates (no `today()`), and
  `lineterminator="\n"` so the same seed gives byte-identical CSVs on any OS.
- Scenarios: `clean`, `defects` (7 known defects, rows modified or appended, never removed),
  `drift` (`account_type` renamed to `acct_type`).
- "Row N" in defect notes means the Nth data row, not counting the header.
- Result: 7 tests passed.

## Stage 1 revision + Stage 2 — Quality layers (2026-10-11)

- Instructor corrections: clean statuses are only `active` and `closed`; `frozen` is reserved for the
  `invalid_status` defect. The contract is not to be loosened. No rule for deposits to closed accounts.
- Data model changed to support later stages (branch-key joins, account/date grain, balance totals,
  customer balances): `branches.csv`, `accounts.csv` (adds `customer_id`, `branch_key`),
  `deposits.csv` as daily closing balances per account.
- Generator now has one scenario per defect: schema_drift, bad_type, missing_required,
  duplicate_account, orphan_account, invalid_status, balance_spike.
- Created `src/quality.py` (layers 1-4) and `tests/test_quality.py`; rewrote `tests/test_generate_data.py`.
- Decisions: layers gate in order (later layers SKIPPED, not passed); money uses `Decimal`;
  300% movement is a failure, exactly 300% passes; a move away from a zero balance is flagged;
  `invalid_status` intentionally passes layers 1-4 and is caught by the contract in Stage 3.
- Result: 56 tests passed.

## Stage 3 — Accounts contract and validator (2026-10-11)

- Created `contracts/accounts.yaml`, `scripts/validate_contract.py`, `tests/test_contract.py`.
- Contract covers types, required, patterns, uniqueness, allowed values (status: active, closed),
  relationships (accounts→branches, deposits→accounts), freshness, field meanings, and a
  breaking-change policy.
- Added `snapshot_date` to accounts (needed to measure freshness) and a `stale_snapshot` scenario.
  Freshness compares to the business date (latest deposits balance_date), not the wall clock,
  so the fixed 2025 synthetic data stays reproducible.
- Change policy is enforced, not just documented: `--baseline` compares contracts and fails a
  breaking change without a MAJOR bump. Demo: adding `frozen` to status at 1.0.0 is blocked.
- Added `read_table` and `format_layers` to `src/quality.py` for reuse.
- A test ties contract field names to the quality-layer column list so they cannot drift apart.
- Result: 94 tests passed.

## Reconciliation of Stages 1–3 with the assignment (2026-10-11)

Instructor review found gaps; targeted corrections, working code preserved.

1. **Customers table.** Added `customers.csv` (unique `customer_id`). Layer 3 and the contract
   now validate `accounts.customer_id → customers.customer_id`. New `orphan_customer` scenario.
   Customers are drawn from the RNG last so existing tables keep their seeded values.
2. **Balance semantics.** `deposits.csv` held closing balances, not deposit transactions.
   Renamed to `daily_balances.csv` everywhere; grain declared as one row per `account_id` and
   `balance_date`. (Earlier entries in this log say "deposits"; they describe the old name.)
3. **Original incident.** New `duplicate_branch` scenario appends `BR-03` again. Layer 3's
   reference-key uniqueness check rejects it (`BR-03 appears on lines 4, 7`); the row is kept.
   It passes the accounts contract, because branch uniqueness is not an accounts rule;
   Stage 4's gate runs both, and must also verify mart grain, join row-count preservation,
   and balance-total reconciliation.
4. **Freshness.** Replaced the data-derived business date (my Stage 3 design) with a required
   `--as-of`. Every row (not just the newest) must satisfy `as-of − 1 ≤ snapshot_date ≤ as-of`.
   The Stage 3 max-based rule would have let one fresh row hide stale ones, and would have
   passed when accounts and balances were both old. Both cases now have tests.
   Historical demo uses `--as-of 2025-09-30`; documented that this does not prove current delivery.
5. **Change policy.** A MAJOR bump no longer approves a breaking change; breaking changes are
   rejected by default and go through a documented consumer review process (new contract file).
   Stage 6 must use the PR base commit (PRs) or the pre-push commit (main) as `--baseline`.
6. **Business rule review.** The movement check compared with the previous *available* row,
   so a gap would have been bridged. Now it compares only consecutive calendar days, and a
   separate Layer 4 check reports missing days per account. Strict >300% and zero-baseline
   behavior unchanged (zero → zero passes).

Result: 119 tests passed. Not committed.

## Balance coverage + Stage 4 — Gate and deposits mart (2026-10-11)

- **missing_day scenario** deletes `ACC-00012` 2025-09-10. The modify-or-append rule was my
  project choice, not an assignment requirement; deleting is right when missing data is the defect.
- **Coverage against a declared batch range.** Replaced the per-account min/max gap check (which
  could not see a missing first or last day, or an account with no rows) with
  `check_balance_coverage`. The batch is the 30 days ending on `--as-of` (`BATCH_DAYS`), never
  derived from the data. Every account is expected to have one balance per batch date. Tests cover
  interior, first, last, and no-rows cases, plus rows outside the range.
- **Coverage kept separate from reconciliation.** A test shows `missing_day` passes layer 5
  reconciliation but fails layer 4 coverage.
- **Created `scripts/run_gate.py`** (`--data`, `--as-of` required) and `tests/test_run_gate.py`.
  It runs the accounts contract and layers 1–4; layer 5 runs only when both pass.
- **Layer 5:** join keys unique before joining (the join is skipped if not); after joining,
  mart grain, row count, and Decimal balance totals (grand total and per date). `build_mart`
  never deduplicates: a test forces a join on duplicate branches and shows the post-join checks
  catch the multiplied rows.
- **No stale mart:** outputs in `mart/` are deleted at the start of every run, including runs with
  a bad `--as-of` or an invalid contract. The mart is written atomically, and only after every
  check passes; `gate_result.json` is written last with the mart's SHA-256. `mart/` added to `.gitignore`.
- Output prints the first 10 failures per check with a total count; `gate_result.json` keeps all.
- Result: 151 tests passed. Not committed.

## Stage 5 — Staging deposits report (2026-10-11)

- Created `scripts/build_report.py` and `tests/test_build_report.py` (18 tests).
- Evidence gate: deletes old `site/index.html` and `site/report.json` first, then requires the gate
  result (valid JSON, status PASS, required fields), a mart whose SHA-256 matches, matching
  columns and row count, a latest balance date equal to the gate's batch end, and a one-line
  release label. Any failure: `REPORT REJECTED`, exit 1, nothing written.
- Reads only the mart; no source re-reads, joins, or repairs.
- Inspected the generator before designing branch summaries: `customer_id` and `branch_key` are
  drawn independently per account; 49 of 99 customers span several branches. Chose per-branch
  customer totals (a customer counts in each branch they use, with only that branch's balances),
  so branch totals add up to the overall total.
- Latest-day metrics: latest date only, customer totals summed before averaging, Decimal, averages
  rounded to cents ROUND_HALF_UP. Tests show the wrong answers too: a per-account average gives
  200.00 instead of 300.00, and summing the whole batch gives 3600.00 instead of 600.00.
- Source commit: `GITHUB_SHA`, else local HEAD (flags uncommitted changes), else "unavailable".
  The repo has no commits yet, so the local report shows "unavailable".
- Output has no timestamps, so the same mart and commit give byte-identical pages (useful for the
  Stage 7 rollback comparison).
- Result: 169 tests passed. Clean report: latest-day total 2389107.02, average customer
  balance 24132.39 (99 customers). Not committed.

## Stage 6 — GitHub Actions and Pages (2026-10-11)

- Inspected the CLI arguments of all four scripts first. The generator had no `--as-of` (its end
  date was hard-coded), so I added `--as-of` (default 2025-09-30). Output with the default is
  byte-identical to before (checked with `diff -r`). One `AS_OF` value now drives generation,
  contract validation, and the gate.
- Created `.github/workflows/quality-gates.yml`: job `quality-gate` (PRs to main and pushes to
  main) and job `deploy` (push to main only, needs quality-gate, `github-pages` environment,
  concurrency group with cancel-in-progress false).
- Python 3.12 to match the local venv (3.12.9). Action majors checked with `git ls-remote` on
  2026-10-11: checkout v7, setup-python v7, configure-pages v6, upload-pages-artifact v5, deploy-pages v5.
- Contract baseline: PR base SHA for pull requests, `github.event.before` for pushes, fetched
  explicitly. All-zero `before` on push is the documented bootstrap. Any other missing baseline,
  fetch failure, or self-comparison fails the job.
- Simulated the baseline step locally against a throwaway Git repo (seven cases: missing contract
  at base, bootstrap, breaking PR, non-breaking push, self comparison, empty SHA, unfetchable SHA).
  All behaved as intended. The real workflow has not run on GitHub yet.
- README: Pages source, branch protection, the required check name `quality-gate`, PR evidence
  capture, the historical evaluation date, and the five-branch sample disclosure.

## Stage 6 correction — deployment ordering (2026-10-11)

- My Stage 6 README claimed serialization alone stops an older build overwriting a newer one.
  That was overstated: concurrency controls when runs deploy, not which commit is newest.
- Added a superseded-build guard step to `deploy`. Just before `deploy-pages`, it reads the current
  `main` SHA via `gh api` and compares it with `GITHUB_SHA`. Same SHA: deploy. Different: warning
  and skip. Unreadable or malformed: the job fails. `deploy` gained `contents: read` for the API call.
- Simulated the guard with a stubbed `gh` (same SHA, moved SHA, API failure, garbage response).
- README documents it as a guard, not an absolute guarantee (main can move after the check).
  For the class demo, wait for each deployment to finish before merging the next release or rollback.
- README now separates validation/build failures (no new report published) from upload and deploy
  failures (operational: check the deployment log and the live site).

## Stage 6 demonstration — broken PR, blocked, fixed in place (2026-10-11)

- `main` was already pushed by the instructor (a7ee096); bootstrap run 38180930842 succeeded.
  Branch protection confirmed via the public API: `quality-gate` required, enforcement everyone.
- `gh` is not installed and no API token was available. I pushed branches with the user's Git
  credentials, the user opened the PR in the browser, and I read run, job, and PR data from the
  public unauthenticated API. Job logs need authentication (HTTP 403), so the failing test list
  is a labelled local reproduction of the same commit.
- PR #1 commit 70f9da9 (contract 2.0.0 + `frozen`) → run 38188763882 failed at Run test suite
  (13 tests); every later step and deploy skipped. The README had predicted the Contract
  compatibility step; corrected, because the tests catch this break first.
- The blocked-merge screenshot showed GitHub citing "1 approving review required". The failing
  required check was a second block. A solo author can't approve their own PR, so the user set
  required approvals to 0 before capturing the passing state. The README now documents this.
- Fixed the same PR (c43f197, contract restored to main's version) → run 38189467486 succeeded;
  merge enabled, files changed 0. PR closed without merging; demo branches deleted.
- Built the demo branch in a separate git worktree so the user's uncommitted Stage 6 changes
  stayed out of the demo PR.

## Stage 7 — Rollback demonstration (2026-10-12)

- Snapshotted the live known-good report (532368b, staging-v1, mart fa47c065493f).
- Bad release PR #3 (746938c → merge e38c3a9): release label staging-v2 and CI seed 42 → 7.
  Predicted locally first. Every gate passed, because the data is valid, but every business metric
  changed (customers 99 → 93, latest-day total 2389107.02 → 2578190.61). Live report matched the
  prediction exactly.
- Rollback PR #4: `git revert -m 1 e38c3a9` (2088c3c → merge 3f94a2f). Before pushing, I checked
  that the reverted tree is identical to 532368b. No force push or history rewrite; main stayed protected.
- Each merge waited for the previous deployment to finish (polled until the live source_commit
  equalled the merge commit).
- Verification: the live report.json after the rollback differs from the known-good snapshot only in
  `source_commit`; index.html differs only in its source-commit line; mart SHA-256 restored.
- The runbook in the README covers: identify, revert on a branch, PR, wait, verify by field diff.
- Evidence: evidence/rollback_summary.txt and evidence/rollback_{1,2,3}_*.json/.html.
