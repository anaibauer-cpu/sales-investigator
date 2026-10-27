# Assumptions and open ambiguities

`tasks/data/domain.md` defines the rules. Nothing here adds a business policy: each entry is either a modelling choice made for the fixtures (marked **Decision**) or something the rules do not settle (marked **Open**). Open items stay visible and are repeated in the final README.

Verified facts (checked by recomputation, not assumed):
- `seed.sqlite` matches `seed.json`: 2 customers, 4 orders, 3 refunds.
- Seed totals: August gross 10000 / refunds 1000 / net 9000; September gross 10000 / refunds 3000 / net 7000 (net change -2000). Order O3 has two refunds (RF2, RF3).

## Decisions for the fixtures

| ID | Topic | Decision |
|----|-------|----------|
| A1 | Seed months | Rows dated August and September 2026 are not changed. New rows go into other months, so `expected-seed-results.json` stays valid for those two periods. |
| A2 | Segments | Only `small` and `large` are used. |
| A3 | Date format | `YYYY-MM-DD`, UTC, no timestamps. A validator checks the format. |
| A4 | Zero amounts | Not generated. The schema allows them, so the validator warns instead of rejecting. |
| A5 | Refund before its order | Not generated. The rules do not forbid it; the validator warns. |
| A6 | Refunds above order amount | Total refunds per order <= order amount (explicit in `domain.md`). The main database never violates it. Invalid examples, if any, live in a separate file labelled `deliberate_invalid`. |
| A7 | Segment of a refund | Taken from the customer's current segment via refund -> order -> customer. The schema stores one segment per customer. |
| A8 | Direction of net change | Later period minus earlier period (consistent with the supplied -2000). |
| A9 | Periods | Reference cases use calendar months with half-open boundaries. The application may accept other periods; reference cases do not cover them. |
| A10 | Segments with no activity | Shown with zeros in a breakdown instead of being omitted (as `large` refunds = 0 in the supplied expectation). |
| A11 | Negative net | A segment or period can have negative net when refunds exceed gross for that period. It is reported as computed, not corrected. Not exercised by the current fixtures (see `data/README.md`). |
| A12 | Refunds on seed orders | No new refunds are added to seed orders O1-O4, so the refund history of seed orders stays exactly as supplied. The validator checks this. |
| A13 | New orders for seed customers | New orders for seed customers C1 and C2 are allowed in other months (O9, O12, O20, O21). This adds rows and changes no seed row or relationship. |
| A14 | Simulated label | The `simulated` label and row provenance live in `data/additions.json`, `data/dataset.json` (`meta`) and `data/README.md`, not in the database, which keeps only the three schema tables. |

## Open ambiguities (not resolved by the rules)

| ID | Question | Why it matters |
|----|----------|----------------|
| Q1 | "Segment of the customer on the original order" could mean the segment at order time, but the schema has no segment history. A7 is a workaround, not a rule. | A customer changing segment would change segment totals. |
| Q2 | How timestamps and time zones should be treated if they ever appear. | A3 avoids the issue but does not solve it. |
| Q3 | Whether a zero-amount order or refund is valid business data or an error. | A4 only chooses to warn. |
| Q4 | Whether more than two periods or non-calendar periods should be compared. | Reference cases cover only calendar months. |
| Q5 | Whether an over-refunded order should ever be allowed and how it would be counted. | A6 keeps such rows out of the main data. |
| Q6 | The records contain no refund reasons. Any explanation of customer behaviour is unsupported. | The report must list this as missing evidence. |
| Q7 | October 2026 is a partial month: the latest row is dated 2026-10-27, while the calendar-month period runs to 2026-11-01. The rules do not say whether a partial period may be compared with full ones. | Comparing October with full months may mislead. Reports must state the date ranges they use. |

## Deliberately invalid or simulated data
| File | Label | Content |
|------|-------|---------|
| `data/additions.json` (merged into `data/dataset.json` / `data/dataset.sqlite`) | `simulated` | Handwritten fictional rows C3-C10, O5-O30, RF4-RF8, dated June, July and October 2026. No randomness. |
| `data/deliberate_invalid.json` | `deliberate_invalid` | DI1 over-refunded order, DI2 unknown segment `medium`, DI3 bad date `2026-02-30`. Used only to test `scripts/validate_dataset.py`; never loaded into the main dataset. |
