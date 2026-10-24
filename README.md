# EMG Operations

Internal app for Energy Marketing Group: commission payroll, agent statements, advances and other debts, reporting, and (later phases) sales mapping and leaderboards.

| Part | Where | What |
|---|---|---|
| Engine and API | `engine/emg` | Python. Importers, versioned rules, commission and debt waterfall, services, FastAPI |
| Database | `db/migrations` | PostgreSQL schema, applied in order by `python -m emg.cli migrate` |
| Web app | `apps/web` | Next.js and TypeScript. Calls the API through `/api` on the same origin |
| Sample config | `config/sample` | **Fictional** rules for development and tests |

Private payroll data (control workbooks, vendor files, final packages, the real rule overlay) is never committed. `.gitignore` blocks spreadsheets, PDFs, ZIPs, `storage/` and `private/`.

## Local setup

Requirements: Python 3.11+, Node 22, PostgreSQL 16 (or Docker).

```bash
cp .env.example .env                     # set passwords
docker compose up -d db                  # or use a local Postgres
python -m venv .venv && . .venv/bin/activate
pip install -e "engine[dev]"
cd engine
python -m emg.cli migrate
python -m emg.cli create-user you@example.com "Your Name" owner
# Load the real rules, roster emails, opening balances and processed chargebacks:
python -m emg.cli seed-control /secure/EMG_Payroll_Control_Week42.xlsx --week 2026-W42 --overlay /secure/rules_overlay.json
uvicorn emg.api.app:app --reload         # API on :8000
python -m emg.services.jobs              # background worker (separate shell)
cd ../apps/web && npm install && npm run dev   # web on :3000
```

Or run everything with `docker compose up --build`.

## Weekly flow

1. **Imports.** Upload each vendor file for the payroll week. Import the Nexamp sales report before Nexamp commissions. Review the preview (mapping, counts, errors, prior-year exclusions, already-imported lines), then commit.
2. **Week adjustments.** Enter one-week facts: new advances (with the transfer reference), room and flight charges, holds, protected checks, refunds, and this-week collection instructions. These never become standing rules.
3. **Calculate.** This is safe to repeat. Each calculation is a numbered revision showing every person, line, chargeback and review item.
4. **Review queue.** Blocking items (missing rate, unknown product, duplicate, ambiguous override) stop finalization until someone records a decision.
5. **Finalize.** This is atomic and safe to retry. It posts ledger entries, payout evidence and processed chargebacks, and creates *calculated* payments. It refuses if inputs changed since the calculation.
6. **Payments.** Mark payments submitted, then confirmed with the provider reference. Calculated payroll is never a payment.
7. **Corrections.** Reopen a finalized week as a new revision with a reason. Finalizing it reverses the old ledger entries and posts the new ones. Already-confirmed payments are netted, and overpayments go to review.

## Tests

```bash
cd engine && pytest
```

Database tests use `TEST_DATABASE_URL` (default `postgresql://emg:emg@localhost:5432/emg_test`) and recreate its schema. The Week 43 acceptance replay runs only when the private fixtures are present (`EMG_FIXTURES`, `EMG_RULES_OVERLAY`). It must match the final for every person: 28 checks, $17,591.81, AEG $5,760.

See `docs/` for rounding, rules, roles and operations.

## Reports and statement email (Phase 2)

From **Reports**, build the week's package for the finalized (or preliminary) run:

- Master workbook (`EMG_Week43_Final_Payroll_2026-10-23.xlsx`): summary, payroll calculation, payable checks, earnings detail, chargebacks, company adjustments, non-payable, pending, week adjustments, review, vendor reconciliation, sources with hashes.
- Agent statements (`Jane_Doe_Week43_2026-10-23.pdf`), the combined PDF, and a ZIP of all statements. Statements go to people with activity this week: pay, new charges, deductions, holds, credits, flight debt, or non-payable sales.
- Payable checks CSV, payable-only CSV, and the Paychex import CSV (`Contractor,1099-NEC Amount`, one row per positive check).
- For each company payee (for example AEG): the original Spark workbook with every column and its formatting kept, filtered to that company's agents, with payable rows repriced; plus an invoice PDF and CSV.
- Full package ZIP (`EMG_Week43_Final_Package_2026-10-23.zip`) with everything above, the original source files, a rules snapshot and a manifest of SHA-256 hashes.

Corrections produce `..._Final_Rev3_...` files, and earlier final revisions stay downloadable.

**Email statements** lists every statement with its verified roster address, or the reason it can't be sent (no email, unverified, excluded). Choose people, type `SEND 2026-W43`, and each person receives one message with only their own PDF attached. Retries never resend. A send that was interrupted is marked *unknown* for a human to check. Delivery and bounce events arrive by webhook (`POST /api/email/events` with `X-Webhook-Secret`) or can be marked by hand. Real delivery needs `EMAIL_SEND_ENABLED=1` and SMTP settings. Otherwise messages go to an outbox in storage.

To refresh emails from a newer control workbook, run `python -m emg.cli sync-roster CONTROL.xlsx`.
