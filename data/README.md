# Fixture data

All rows except the original seed are **simulated** (fictional, handwritten). The rules are in `tasks/data/domain.md`, and the decisions are in `docs/assumptions.md`.

## Files
| File | Content |
|------|---------|
| `additions.json` | Handwritten simulated rows (label `simulated`). Each row has a note giving the case it serves. |
| `dataset.json` | JSON snapshot: seed rows followed by simulated rows, plus a `meta` block (seed IDs, simulated IDs, method, SHA-256 of `tasks/data/*` at build time). |
| `dataset.sqlite` | The same rows in the three schema tables only (`tasks/data/schema.sql` unchanged). Provenance labels are kept out of the database on purpose. |
| `expected-results.json` | Expected values for R1-R5 and R3b, each with input rows and rules. |
| `deliberate_invalid.json` | 3 cases labelled `deliberate_invalid` (over-refunded order, unknown segment, bad date). Used only to test the validator. Never loaded into `dataset.json` or `dataset.sqlite`. |

## Generation method
- Handwritten fixed list. **No randomness, so no random seed.**
- `scripts/build_dataset.py` reads `tasks/data/seed.json`, `tasks/data/schema.sql` and `data/additions.json`, and writes `dataset.json` and `dataset.sqlite`. It does not write anything in `tasks/data/`.
- Size: 10 customers (2 seed + 8), 30 orders (4 + 26), 8 refunds (3 + 5). Segments: `small` (C1, C3, C5, C7, C9) and `large` (C2, C4, C6, C8, C10).
- New rows are dated only June, July and October 2026. August and September rows are exactly the seed (A1). Seed orders O1-O4 get no new refunds (A12).
- Monthly gross is kept near the seed scale (about 10,000-13,000 cents), so August and September are not outliers by volume. Amounts are deliberately uneven, so a double count changes totals visibly.

## Rebuild and check
```
python scripts/build_dataset.py      # writes dataset.json + dataset.sqlite
python scripts/validate_dataset.py   # integrity, seed preservation, sqlite == json, source hashes, deliberate_invalid detection
python scripts/compute_expected.py   # writes expected-results.json (pure Python over dataset.json, no SQL)
```
Each script exits with a non-zero code on failure.

## Reference cases
| Case | Rows | Expected (cents) |
|------|------|------------------|
| R1 Aug -> Sep | seed only | Aug 10000 / 1000 / 9000; Sep 10000 / 3000 / 7000; net change -2000 (gross 0, refunds +2000) |
| R2 multiple refunds | O3 (RF2, RF3); supplementary O8 (RF7 June, RF8 July) | Sep gross 10000 (O3 once). Naive join would give 15000 (wrong). |
| R3 Sep by segment | seed only | small 5000 / 3000 / 2000; large 5000 / 0 / 5000; sum = period totals |
| R3b Oct by segment (supplementary) | O20-O30, RF4, RF5, RF6 | small 4575 / 1125 / 3450; large 8225 / 630 / 7595; sum = 12800 / 1755 / 11045 |
| R4 boundary | O20, O21, RF5 dated 2026-10-01; O19 dated 2026-07-31 | In October, not September; O19 in July, not August |
| R5 later-month refund | RF4 (O16 Jul -> Oct, large), RF5 (O17 Jul -> Oct, small), RF8 (O8 Jun -> Jul, large) | Counted by refund_date; segment from the order's customer |

Monthly overview (gross / refunds / net): June 11645 / 605 / 11040; July 12275 / 450 / 11825; August 10000 / 1000 / 9000; September 10000 / 3000 / 7000; October 12800 / 1755 / 11045.

## Not exercised
- **A11 (negative net)** is not exercised. No period or segment has refunds above gross.
- Zero amounts (A4) and refunds before their order (A5) are not generated, so the validator's warnings for them are not triggered by the main data.
- Segment changes over time (Q1) cannot be shown, because the schema stores one segment per customer.
- October ends at 2026-10-27, so it is a partial month (Q7). Reports must state date ranges.
