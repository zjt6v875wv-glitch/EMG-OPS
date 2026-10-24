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
