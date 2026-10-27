"""Compute expected results for reference cases R1-R5 (plus R3b).

Independent answer key: pure Python over the JSON snapshot data/dataset.json.
It does not import sqlite3, does not read data/dataset.sqlite, and shares no
code or SQL with any application. Orders and refunds are aggregated separately
and only then combined (domain.md rule 3).

Writes data/expected-results.json and prints a summary.

Usage: python scripts/compute_expected.py
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEED_EXPECTED = ROOT / "tasks" / "data" / "expected-seed-results.json"

RULES = {
    "gross": "domain.md rule 2: gross sales for a period sum orders by order_date.",
    "refunds": "domain.md rule 2: refunds for that period sum refunds by refund_date.",
    "net": "domain.md rule 2: net sales equal gross sales minus refunds.",
    "period": "domain.md rule 2: UTC dates and half-open periods, start included, end excluded.",
    "separate": "domain.md rule 3: integer USD cents; group order and refund totals separately before combining them; "
    "joining raw refund rows to orders can repeat order amounts.",
    "segment": "domain.md rule 4: assign refunds to the segment of the customer on the original order "
    "(refund -> order -> customer; A7).",
    "change": "docs/assumptions.md A8: net change = later period minus earlier period.",
    "zeros": "docs/assumptions.md A10: segments with no activity are shown with zeros.",
}

MONTHS = [
    ("2026-06-01", "2026-07-01"),
    ("2026-07-01", "2026-08-01"),
    ("2026-08-01", "2026-09-01"),
    ("2026-09-01", "2026-10-01"),
    ("2026-10-01", "2026-11-01"),
]
AUG, SEP, OCT = MONTHS[2], MONTHS[3], MONTHS[4]
JUL = MONTHS[1]
SEGMENTS = ("small", "large")


def d(value):
    return date.fromisoformat(value)


def in_period(value, period):
    start, end = period
    return d(start) <= d(value) < d(end)


class Data:
    def __init__(self, snapshot):
        self.customers = {c["customer_id"]: c for c in snapshot["customers"]}
        self.orders = {o["order_id"]: o for o in snapshot["orders"]}
        self.refunds = {r["refund_id"]: r for r in snapshot["refunds"]}

    def order_segment(self, order_id):
        return self.customers[self.orders[order_id]["customer_id"]]["segment"]

    def refund_segment(self, refund_id):
        return self.order_segment(self.refunds[refund_id]["order_id"])

    def period_orders(self, period, segment=None):
        return [o for o in self.orders.values()
                if in_period(o["order_date"], period)
                and (segment is None or self.order_segment(o["order_id"]) == segment)]

    def period_refunds(self, period, segment=None):
        return [r for r in self.refunds.values()
                if in_period(r["refund_date"], period)
                and (segment is None or self.refund_segment(r["refund_id"]) == segment)]

    def totals(self, period, segment=None):
        orders = self.period_orders(period, segment)
        refunds = self.period_refunds(period, segment)
        gross = sum(o["amount_cents"] for o in orders)
        refunded = sum(r["amount_cents"] for r in refunds)
        return {
            "start": period[0],
            "end_exclusive": period[1],
            **({"segment": segment} if segment else {}),
            "gross_cents": gross,
            "refunds_cents": refunded,
            "net_cents": gross - refunded,
            "input_rows": {
                "orders": [{"order_id": o["order_id"], "order_date": o["order_date"], "amount_cents": o["amount_cents"]}
                           for o in orders],
                "refunds": [{"refund_id": r["refund_id"], "order_id": r["order_id"], "refund_date": r["refund_date"],
                             "amount_cents": r["amount_cents"]} for r in refunds],
            },
        }


def values(t):
    return {k: t[k] for k in ("gross_cents", "refunds_cents", "net_cents")}


def segment_breakdown(data, period):
    total = data.totals(period)
    segments = {s: data.totals(period, s) for s in SEGMENTS}
    summed = {k: sum(segments[s][k] for s in SEGMENTS) for k in ("gross_cents", "refunds_cents", "net_cents")}
    return {
        "period": {"start": period[0], "end_exclusive": period[1]},
        "rule": [RULES["gross"], RULES["refunds"], RULES["net"], RULES["segment"], RULES["zeros"]],
        "segments": segments,
        "period_totals": values(total),
        "segments_sum": summed,
        "segments_add_up": summed == values(total),
    }


def r1(data, supplied):
    aug, sep = data.totals(AUG), data.totals(SEP)
    change = sep["net_cents"] - aug["net_cents"]
    supplied_periods = [{k: p[k] for k in ("start", "end_exclusive", "gross_cents", "refunds_cents", "net_cents")}
                        for p in supplied["periods"]]
    ours = [{k: t[k] for k in ("start", "end_exclusive", "gross_cents", "refunds_cents", "net_cents")} for t in (aug, sep)]
    return {
        "case": "R1",
        "title": "Main investigation, August -> September 2026",
        "rule": [RULES["gross"], RULES["refunds"], RULES["net"], RULES["period"], RULES["separate"], RULES["change"]],
        "periods": [aug, sep],
        "net_change_cents": change,
        "gross_change_cents": sep["gross_cents"] - aug["gross_cents"],
        "refunds_change_cents": sep["refunds_cents"] - aug["refunds_cents"],
        "matches_supplied_seed_expectation": ours == supplied_periods and change == supplied["net_change_cents"],
    }


def r2(data):
    def order_case(order_id):
        order = data.orders[order_id]
        refunds = [r for r in data.refunds.values() if r["order_id"] == order_id]
        return {
            "order_id": order_id,
            "order_date": order["order_date"],
            "amount_cents": order["amount_cents"],
            "refunds": [{"refund_id": r["refund_id"], "refund_date": r["refund_date"], "amount_cents": r["amount_cents"]}
                        for r in refunds],
            "refund_total_cents": sum(r["amount_cents"] for r in refunds),
            "times_counted_in_gross": 1,
        }

    sep = data.totals(SEP)
    # Anti-expectation: the wrong figure a raw orders LEFT JOIN refunds produces,
    # summing the order amount once per joined row. Computed here only to show the error.
    naive = 0
    for o in data.period_orders(SEP):
        joined_rows = max(1, sum(1 for r in data.refunds.values() if r["order_id"] == o["order_id"]))
        naive += o["amount_cents"] * joined_rows
    return {
        "case": "R2",
        "title": "Order O3 with two refunds is counted once in gross",
        "rule": [RULES["gross"], RULES["separate"]],
        "order": order_case("O3"),
        "september_gross_cents": sep["gross_cents"],
        "september_input_orders": sep["input_rows"]["orders"],
        "anti_expectation": {
            "label": "WRONG - for comparison only",
            "description": "September gross if orders are joined to raw refund rows before summing (O3 repeated).",
            "naive_join_gross_cents": naive,
        },
        "supplementary": {
            "description": "O8 also has two refunds, in two different months (June and July).",
            "order": order_case("O8"),
            "june_gross_cents": data.totals(MONTHS[0])["gross_cents"],
        },
    }


def r3(data):
    return {"case": "R3", "title": "September 2026 breakdown by segment", **segment_breakdown(data, SEP)}


def r3b(data):
    return {"case": "R3b", "title": "Supplementary: October 2026 breakdown by segment (partial month, Q7)",
            **segment_breakdown(data, OCT)}


def r4(data):
    boundary = "2026-10-01"
    rows = [("order", o["order_id"], o["order_date"], o["amount_cents"]) for o in data.orders.values()
            if o["order_date"] == boundary]
    rows += [("refund", r["refund_id"], r["refund_date"], r["amount_cents"]) for r in data.refunds.values()
             if r["refund_date"] == boundary]
    sep, oct_ = data.totals(SEP), data.totals(OCT)
    boundary_rows = []
    for kind, row_id, day, amount in rows:
        boundary_rows.append({
            "kind": kind, "id": row_id, "date": day, "amount_cents": amount,
            "in_september": in_period(day, SEP), "in_october": in_period(day, OCT),
        })
    o19 = data.orders["O19"]
    return {
        "case": "R4",
        "title": "Period boundary: rows dated exactly 2026-10-01",
        "rule": [RULES["period"], RULES["gross"], RULES["refunds"]],
        "boundary_rows": boundary_rows,
        "september": values(sep),
        "october": values(oct_),
        "september_excludes_all_boundary_rows": not any(r["in_september"] for r in boundary_rows),
        "october_includes_all_boundary_rows": all(r["in_october"] for r in boundary_rows),
        "supplementary": {
            "description": "O19 dated 2026-07-31 is in July and not in August.",
            "order_id": "O19", "order_date": o19["order_date"], "amount_cents": o19["amount_cents"],
            "in_july": in_period(o19["order_date"], JUL), "in_august": in_period(o19["order_date"], AUG),
        },
    }


def r5(data):
    cases = []
    for refund_id in ("RF4", "RF5", "RF8"):
        r = data.refunds[refund_id]
        o = data.orders[r["order_id"]]
        cases.append({
            "refund_id": refund_id, "refund_date": r["refund_date"], "amount_cents": r["amount_cents"],
            "order_id": o["order_id"], "order_date": o["order_date"], "customer_id": o["customer_id"],
            "segment": data.refund_segment(refund_id),
            "counted_in_month_of": r["refund_date"][:7],
            "order_month": o["order_date"][:7],
        })
    return {
        "case": "R5",
        "title": "Refund in a later period than its order",
        "rule": [RULES["refunds"], RULES["segment"], RULES["period"]],
        "refunds": cases,
        "july": {"all": values(data.totals(JUL)), **{s: values(data.totals(JUL, s)) for s in SEGMENTS}},
        "october": {"all": values(data.totals(OCT)), **{s: values(data.totals(OCT, s)) for s in SEGMENTS}},
    }


def main():
    snapshot = json.loads((DATA / "dataset.json").read_text(encoding="utf-8"))
    supplied = json.loads(SEED_EXPECTED.read_text(encoding="utf-8"))
    data = Data(snapshot)

    results = {
        "scope": "expanded fixture dataset (seed + simulated rows), data/dataset.json",
        "computed_by": "scripts/compute_expected.py (pure Python over the JSON snapshot; no SQL)",
        "units": "integer USD cents",
        "dates": "UTC YYYY-MM-DD; half-open periods [start, end_exclusive)",
        "note": "October 2026 data ends 2026-10-27; October is a partial month (docs/assumptions.md Q7).",
        "monthly_overview": [{"start": p[0], "end_exclusive": p[1], **values(data.totals(p))} for p in MONTHS],
        "cases": [r1(data, supplied), r2(data), r3(data), r4(data), r5(data), r3b(data)],
    }
    (DATA / "expected-results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    print("monthly overview (cents):")
    print(f"  {'start':<11}{'end_excl':<12}{'gross':>7}{'refunds':>9}{'net':>7}")
    for m in results["monthly_overview"]:
        print(f"  {m['start']:<11}{m['end_exclusive']:<12}{m['gross_cents']:>7}{m['refunds_cents']:>9}{m['net_cents']:>7}")

    c = {case["case"]: case for case in results["cases"]}
    checks = [
        ("R1 matches tasks/data/expected-seed-results.json", c["R1"]["matches_supplied_seed_expectation"]),
        ("R2 O3 refunds <= amount", c["R2"]["order"]["refund_total_cents"] <= c["R2"]["order"]["amount_cents"]),
        ("R2 September gross counts O3 once (10000, naive join differs)",
         c["R2"]["september_gross_cents"] == 10000 and c["R2"]["anti_expectation"]["naive_join_gross_cents"] != 10000),
        ("R3 September segments add up", c["R3"]["segments_add_up"]),
        ("R3 September matches supplied segment expectation",
         {s: values(c["R3"]["segments"][s]) for s in SEGMENTS} == supplied["september_segments"]),
        ("R3b October segments add up", c["R3b"]["segments_add_up"]),
        ("R4 boundary rows excluded from September", c["R4"]["september_excludes_all_boundary_rows"]),
        ("R4 boundary rows included in October", c["R4"]["october_includes_all_boundary_rows"]),
        ("R4 O19 (2026-07-31) in July, not August",
         c["R4"]["supplementary"]["in_july"] and not c["R4"]["supplementary"]["in_august"]),
        ("R5 each refund counted in a later month than its order",
         all(r["counted_in_month_of"] > r["order_month"] for r in c["R5"]["refunds"])),
    ]
    print(f"R1 net change Aug->Sep: {c['R1']['net_change_cents']} "
          f"(gross change {c['R1']['gross_change_cents']}, refunds change {c['R1']['refunds_change_cents']})")
    print(f"R2 naive-join September gross (wrong): {c['R2']['anti_expectation']['naive_join_gross_cents']}")
    for s in SEGMENTS:
        print(f"R3 September {s}: {values(c['R3']['segments'][s])}")
        print(f"R3b October {s}: {values(c['R3b']['segments'][s])}")
    for r in c["R5"]["refunds"]:
        print(f"R5 {r['refund_id']}: order {r['order_id']} {r['order_date']} -> refund {r['refund_date']} "
              f"{r['amount_cents']} cents, segment {r['segment']}")
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
        failed += not ok
    print(f"wrote {(DATA / 'expected-results.json').relative_to(ROOT)}")
    print(f"RESULT: {'FAIL' if failed else 'PASS'} ({failed} failed of {len(checks)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
