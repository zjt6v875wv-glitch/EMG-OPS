# Money and rounding

- All money is `Decimal`, never float. It is stored as `numeric`, and the API sends it as strings.
- Earning lines (kWh × rate, or units × rate) are kept at full precision (`numeric(16,6)`).
- For each payee, each category total (agent commission, overrides, chargebacks) is rounded **half-up to cents** once, after summing lines. This matches the historical workbooks: W43 replays to the cent for all 78 people.
- Deductions, balances, checks and ledger entries are cents.
- Percent collections are `round_half_up(available × percent / 100)`, then capped at what is owed and at what is available.
- A check is never negative. Whatever cannot be recovered carries forward in that debt's own ledger.
- Every run asserts the identity: gross + cash credits = recovered + held + reserve + deductions + checks.
