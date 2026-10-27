# Business Data Investigator: project instructions

## Current phase: DATA PREPARATION ONLY
- Do not create application code (no `app/`, query tool, model integration, UI) until the owner explicitly starts the next phase.
- Before creating or changing any file, show a plan and wait for approval.
- Do not run live model/API calls in this phase.

## Source of truth (read in this order)
1. `tasks/data/domain.md` (fixed fictional rules; wins on any conflict)
2. `TASK.md` (working summary of the assignment: sizes, checks, requirements)
3. `docs/assumptions.md` (recorded decisions and open ambiguities)
4. `.claude/skills/generate-assignment-data/SKILL.md` (fixture-generation rules)

## Fixed metric rules (summary of domain.md)
- Gross sales for a period = sum of `orders.amount_cents` selected by `order_date`.
- Refunds for a period = sum of `refunds.amount_cents` selected by `refund_date`.
- Net sales = gross - refunds.
- UTC dates (`YYYY-MM-DD`), half-open periods: start included, end excluded.
- Integer USD cents only.
- Aggregate orders and refunds separately, then combine. Never join raw refund rows to orders before summing (it repeats order amounts).
- A refund belongs to the segment of the customer on the original order (refund -> order -> customer).
- The data shows amounts and dates only. It never shows why a refund was requested; do not state or imply reasons.

## Data rules
- Never modify anything in `tasks/data/` (originals). Write generated files to `data/`.
- Preserve all seed IDs and relationships. Keep the seed cases and the order with multiple refunds (O3: RF2, RF3).
- Per decision A1 in `docs/assumptions.md`, rows dated August and September 2026 stay unchanged; new rows go into other months.
- Total refunds per order must be <= the order amount.
- Segments are only `small` and `large`.
- Do not add real-world accounting, legal, pricing or compliance rules. If a rule is unclear, state the ambiguity in `docs/assumptions.md` instead of inventing a policy.
- Deliberately invalid examples live in a separate file and are labelled `deliberate_invalid`. Never mix them into the main database.
- If generation uses randomness, use a fixed seed and record it.

## Verification rules
- Expected results are computed by a separate pure-Python script from a JSON snapshot of the data, never from the application and never by reusing application SQL.
- Every expected result records its input rows and the rule that determines it.
- Run checks with code and show the real command output. Do not claim a check passed without running it.
- The supplied `expected-seed-results.json` applies only to the original seed rows.

## Secrets
- Never read or print `.env`. Only `.env.example` (variable names) belongs in the repository.

## Reporting format after each task
List: files created or changed, cases covered, checks run with results, unresolved questions.
