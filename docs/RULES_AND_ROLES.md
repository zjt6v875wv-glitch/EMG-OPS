# Rules, week facts and roles

## Versioned rules (data, not code)
Rule sets hold agent rates, utility groups, vendor products, aliases (optionally scoped to a vendor, for example Spark agent IDs), manager teams, override rules (fixed, target remainder, flat per enrollment, with optional explicit seller lists), personal targets (which suppress overrides on a manager's own sales), company routing (for example AEG at $90 per enrollment, excluded from individual payroll), badge ownership by sale date, temporary ownership assignments, and standing collection rules.

Every rule can have `first_week`, `last_week` and `status`. To choose an agent rate, the most specific rule wins: agent, then utility group, then product. Equally specific rules with different rates go to review.

The workflow is: create a draft from the active rules, edit it, preview the affected checks for a week, then activate it with a note. Each payroll run records its rule version.

## Week facts (never rules)
`week_adjustments` kinds: `new_advance`, `room_charge`, `flight_charge`, `other_charge`, `collection` (all, fixed or percent, this week only), `defer`, `protected_check`, `manual_hold` (an amount, or the whole check), `cash_credit` (refund, reimbursement, bonus or hold release), `reserve`. A reference such as a transfer screenshot ID blocks duplicate advances.

Default collection: rooms and other charges are collected in full. Advances and flights are collected only by a standing rule or a week instruction. A week instruction beats a standing rule.

## Roles
| Role | Can |
|---|---|
| owner | everything, including creating owners |
| admin | everything except creating owners; activate rules; start and finalize corrections |
| payroll | imports, review, adjustments, calculate, finalize, payments, reports, email |
| manager | their own and their team's statements, the map for their team, leaderboards |
| agent | their own statements and ledger, leaderboards |
