"""Tests for app/query_tool.py.

Every test works on its own temporary copy of data/dataset.sqlite; the real
file is only read (copied and hashed). Run with either:
    python -m unittest discover -s tests -v
    python -m pytest
"""

import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.query_tool import QueryTool, load_limits

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "dataset.sqlite"
SNAPSHOT = json.loads((ROOT / "data" / "dataset.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((ROOT / "data" / "expected-results.json").read_text(encoding="utf-8"))
CASES = {c["case"]: c for c in EXPECTED["cases"]}
LIMITS = load_limits(ROOT / "config" / "limits.json")

_real_db_hash_before = None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def setUpModule():
    global _real_db_hash_before
    _real_db_hash_before = sha256(REAL_DB)


def tearDownModule():
    after = sha256(REAL_DB)
    if after != _real_db_hash_before:
        raise AssertionError(f"data/dataset.sqlite changed: {_real_db_hash_before} -> {after}")


def period_sql(start, end):
    """Gross and refunds for one half-open period, aggregated separately."""
    return (
        "SELECT "
        f"(SELECT COALESCE(SUM(amount_cents), 0) FROM orders "
        f" WHERE order_date >= '{start}' AND order_date < '{end}') AS gross_cents, "
        f"(SELECT COALESCE(SUM(amount_cents), 0) FROM refunds "
        f" WHERE refund_date >= '{start}' AND refund_date < '{end}') AS refunds_cents"
    )


def segment_sql(start, end):
    """Per-segment gross and refunds; refund segment via refund -> order -> customer."""
    return f"""
        WITH g AS (
            SELECT c.segment, SUM(o.amount_cents) AS gross
            FROM orders o JOIN customers c ON c.customer_id = o.customer_id
            WHERE o.order_date >= '{start}' AND o.order_date < '{end}'
            GROUP BY c.segment),
        r AS (
            SELECT c.segment, SUM(rf.amount_cents) AS refunds
            FROM refunds rf
            JOIN orders o ON o.order_id = rf.order_id
            JOIN customers c ON c.customer_id = o.customer_id
            WHERE rf.refund_date >= '{start}' AND rf.refund_date < '{end}'
            GROUP BY c.segment),
        seg AS (SELECT DISTINCT segment FROM customers)
        SELECT seg.segment,
               COALESCE(g.gross, 0) AS gross_cents,
               COALESCE(r.refunds, 0) AS refunds_cents,
               COALESCE(g.gross, 0) - COALESCE(r.refunds, 0) AS net_cents
        FROM seg LEFT JOIN g USING (segment) LEFT JOIN r USING (segment)
        ORDER BY seg.segment
    """


class ToolTestCase(unittest.TestCase):
    """Gives each test a temp dir with a copy of the database and a log path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qt_test_"))
        # Registered first, so it runs last (cleanups are LIFO), after every
        # connection has been closed; Windows will not delete open files.
        self.addCleanup(shutil.rmtree, self.tmp)
        # The database copy has its own folder, so tests can check that no
        # stray file (journal, attached database) appears next to it.
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir()
        self.db = self.db_dir / "dataset.sqlite"
        shutil.copyfile(REAL_DB, self.db)
        self.log = self.tmp / "runs" / "attempts.jsonl"

    def make_tool(self, **overrides):
        limits = dict(LIMITS, **overrides)
        tool = QueryTool(self.db, self.log, limits)
        self.addCleanup(tool.close)
        return tool

    def log_entries(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def table_counts(self):
        uri = self.db.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("customers", "orders", "refunds")}

    def assertOk(self, res):
        self.assertEqual(res["status"], "ok", res["error"])
        self.assertIsNone(res["error"])


class BasicTests(ToolTestCase):
    def test_config_limits(self):
        self.assertEqual(LIMITS, {"max_attempts": 6, "timeout_ms": 2000, "max_rows": 200})

    def test_valid_select(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT customer_id, segment FROM customers ORDER BY customer_id")
        self.assertOk(res)
        self.assertEqual(res["columns"], ["customer_id", "segment"])
        self.assertEqual(len(res["rows"]), len(SNAPSHOT["customers"]))
        self.assertFalse(res["truncated"])
        self.assertEqual(res["attempt_number"], 1)

    def test_august_gross_and_september_refunds(self):
        tool = self.make_tool()
        aug = tool.run("q1", "SELECT SUM(amount_cents) FROM orders "
                             "WHERE order_date >= '2026-08-01' AND order_date < '2026-09-01'")
        sep = tool.run("q1", "SELECT SUM(amount_cents) FROM refunds "
                             "WHERE refund_date >= '2026-09-01' AND refund_date < '2026-10-01'")
        self.assertOk(aug)
        self.assertOk(sep)
        self.assertEqual(aug["rows"], [[10000]])
        self.assertEqual(sep["rows"], [[3000]])

    def test_empty_result(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT order_id, amount_cents FROM orders WHERE order_date >= '2030-01-01'")
        self.assertOk(res)
        self.assertEqual(res["columns"], ["order_id", "amount_cents"])
        self.assertEqual(res["rows"], [])
        self.assertFalse(res["truncated"])

    def test_syntax_error_counts_as_attempt(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELEC amount_cents FROM orders")
        self.assertEqual(res["status"], "syntax_error")
        self.assertIn("Syntax error", res["error"])
        self.assertEqual(res["attempt_number"], 1)
        self.assertEqual(tool.run("q1", "SELECT 1")["attempt_number"], 2)

    def test_unknown_table(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT * FROM payments")
        self.assertEqual(res["status"], "unknown_table")
        self.assertIn("payments", res["error"])
        self.assertIn("customers, orders, refunds", res["error"])

    def test_empty_statement_is_error(self):
        tool = self.make_tool()
        res = tool.run("q1", "  -- only a comment")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["attempt_number"], 1)

    def test_row_cap_sets_truncated(self):
        tool = self.make_tool(max_rows=5)
        res = tool.run("q1", "SELECT order_id FROM orders ORDER BY order_id")
        self.assertOk(res)
        self.assertEqual(len(res["rows"]), 5)
        self.assertTrue(res["truncated"])
        exact = tool.run("q1", "SELECT order_id FROM orders ORDER BY order_id LIMIT 5")
        self.assertEqual(len(exact["rows"]), 5)
        self.assertFalse(exact["truncated"])

    def test_timeout_on_runaway_recursive_query(self):
        tool = self.make_tool(timeout_ms=100)
        # An aggregate forces the whole (infinite) recursion; a bare SELECT x
        # would just stop at the row cap instead. max(x), not count(*): see
        # test_count_star_on_recursive_cte_is_denied.
        res = tool.run("q1", "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) "
                             "SELECT max(x) FROM r")
        self.assertEqual(res["status"], "timeout")
        self.assertIn("100 ms", res["error"])
        self.assertEqual(res["attempt_number"], 1)
        self.assertLess(self.log_entries()[-1]["duration_ms"], 5000)
        # The connection is still usable afterwards.
        self.assertOk(tool.run("q1", "SELECT 1"))

    def test_count_star_on_recursive_cte_is_denied(self):
        # Known limitation: SQLite reports count(*) over a non-flattened CTE as
        # a READ of the CTE name, which looks the same as count(*) on a real
        # table such as sqlite_master. count(column) works.
        tool = self.make_tool()
        cte = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r LIMIT 5) "
        star = tool.run("q1", cte + "SELECT count(*) FROM r")
        self.assertEqual(star["status"], "denied")
        self.assertIn("count(column)", star["error"])
        col = tool.run("q1", cte + "SELECT count(x), max(x) FROM r")
        self.assertOk(col)
        self.assertEqual(col["rows"], [[5, 5]])

    def test_log_path_must_not_be_database(self):
        with self.assertRaises(ValueError):
            QueryTool(self.db, self.db, LIMITS)


class PositivePathTests(ToolTestCase):
    def test_group_by(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT segment, COUNT(*) FROM customers GROUP BY segment ORDER BY segment")
        self.assertOk(res)
        expected = {}
        for c in SNAPSHOT["customers"]:
            expected[c["segment"]] = expected.get(c["segment"], 0) + 1
        self.assertEqual(res["rows"], [[k, v] for k, v in sorted(expected.items())])

    def test_join(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT o.order_id, c.segment FROM orders o "
                             "JOIN customers c ON c.customer_id = o.customer_id "
                             "WHERE o.order_id IN ('O1', 'O2')")
        self.assertOk(res)
        self.assertEqual(sorted(res["rows"]), [["O1", "small"], ["O2", "large"]])

    def test_case(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT SUM(CASE WHEN amount_cents >= 5000 THEN 1 ELSE 0 END) FROM orders")
        self.assertOk(res)
        self.assertEqual(res["rows"], [[sum(o["amount_cents"] >= 5000 for o in SNAPSHOT["orders"])]])

    def test_like_and_glob(self):
        tool = self.make_tool()
        like = tool.run("q1", "SELECT order_id FROM orders WHERE order_id LIKE 'O3%' ORDER BY order_id")
        glob = tool.run("q1", "SELECT refund_id FROM refunds WHERE refund_id GLOB 'RF[23]' ORDER BY refund_id")
        self.assertOk(like)
        self.assertOk(glob)
        self.assertEqual(like["rows"], [[o["order_id"]] for o in sorted(SNAPSHOT["orders"], key=lambda o: o["order_id"])
                                        if o["order_id"].startswith("O3")])
        self.assertEqual(glob["rows"], [["RF2"], ["RF3"]])

    def test_subquery(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT order_id FROM orders WHERE order_id IN "
                             "(SELECT order_id FROM refunds GROUP BY order_id HAVING COUNT(*) > 1) "
                             "ORDER BY order_id")
        self.assertOk(res)
        self.assertEqual(res["rows"], [["O3"], ["O8"]])

    def test_cte(self):
        tool = self.make_tool()
        res = tool.run("q1", "WITH big AS (SELECT * FROM orders WHERE amount_cents > 0) "
                             "SELECT COUNT(*) FROM big")
        self.assertOk(res)
        self.assertEqual(res["rows"], [[len(SNAPSHOT["orders"])]])

    def test_window_function(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT order_id, customer_id, "
                             "SUM(amount_cents) OVER (PARTITION BY customer_id) AS customer_total, "
                             "ROW_NUMBER() OVER (ORDER BY order_date, order_id) AS rn "
                             "FROM orders ORDER BY rn")
        self.assertOk(res)
        totals = {}
        for o in SNAPSHOT["orders"]:
            totals[o["customer_id"]] = totals.get(o["customer_id"], 0) + o["amount_cents"]
        for order_id, customer_id, customer_total, rn in res["rows"]:
            self.assertEqual(customer_total, totals[customer_id], order_id)
        self.assertEqual([r[3] for r in res["rows"]], list(range(1, len(SNAPSHOT["orders"]) + 1)))

    def test_strftime_month(self):
        tool = self.make_tool()
        res = tool.run("q1", "SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) "
                             "FROM orders GROUP BY month ORDER BY month")
        self.assertOk(res)
        expected = {}
        for o in SNAPSHOT["orders"]:
            expected[o["order_date"][:7]] = expected.get(o["order_date"][:7], 0) + 1
        self.assertEqual(res["rows"], [[k, v] for k, v in sorted(expected.items())])


class CountVariantTests(ToolTestCase):
    """COUNT forms against the READ rule (see docs/known-limitations.md).

    count(*) over a named CTE that SQLite does not flatten arrives at the
    authorizer as a READ of the CTE name and is denied; the other forms pass.
    """

    N_ORDERS = len(SNAPSHOT["orders"])
    N_CUSTOMERS_WITH_ORDERS = len({o["customer_id"] for o in SNAPSHOT["orders"]})
    PER_ORDER_CTE = ("WITH per_order AS (SELECT order_id, SUM(amount_cents) AS total "
                     "FROM orders GROUP BY order_id) ")

    def check(self, sql, expected_rows):
        tool = self.make_tool()
        res = tool.run("q1", sql)
        self.assertOk(res)
        self.assertEqual(res["rows"], expected_rows)

    def check_denied(self, sql, cte_name):
        tool = self.make_tool()
        res = tool.run("q1", sql)
        self.assertEqual(res["status"], "denied")
        self.assertIn(f"'{cte_name}'", res["error"])
        self.assertIn("count(column)", res["error"])

    def test_count_star_plain_table(self):
        self.check("SELECT COUNT(*) FROM orders", [[self.N_ORDERS]])

    def test_count_star_over_grouped_subquery(self):
        self.check("SELECT COUNT(*) FROM (SELECT customer_id FROM orders GROUP BY customer_id)",
                   [[self.N_CUSTOMERS_WITH_ORDERS]])

    def test_count_distinct(self):
        self.check("SELECT COUNT(DISTINCT customer_id) FROM orders",
                   [[self.N_CUSTOMERS_WITH_ORDERS]])

    def test_count_one(self):
        self.check("SELECT count(1) FROM orders", [[self.N_ORDERS]])

    def test_count_column_over_aggregate_cte(self):
        self.check(self.PER_ORDER_CTE + "SELECT count(order_id) FROM per_order", [[self.N_ORDERS]])

    def test_count_star_over_aggregate_cte_is_denied(self):
        self.check_denied(self.PER_ORDER_CTE + "SELECT COUNT(*) FROM per_order", "per_order")

    def test_count_star_over_materialized_cte_is_denied(self):
        self.check_denied("WITH m AS MATERIALIZED (SELECT order_id FROM orders) "
                          "SELECT COUNT(*) FROM m", "m")

    def test_count_column_over_materialized_cte(self):
        self.check("WITH m AS MATERIALIZED (SELECT order_id FROM orders) "
                   "SELECT COUNT(order_id) FROM m", [[self.N_ORDERS]])


class ReferenceCaseTests(ToolTestCase):
    """R1-R5 (and R3b) via SQL through the tool, compared with data/expected-results.json."""

    def period(self, tool, qid, start, end):
        res = tool.run(qid, period_sql(start, end))
        self.assertOk(res)
        gross, refunds = res["rows"][0]
        return {"gross_cents": gross, "refunds_cents": refunds, "net_cents": gross - refunds}

    def segments(self, tool, qid, start, end):
        res = tool.run(qid, segment_sql(start, end))
        self.assertOk(res)
        self.assertEqual(res["columns"], ["segment", "gross_cents", "refunds_cents", "net_cents"])
        return {r[0]: dict(zip(res["columns"][1:], r[1:])) for r in res["rows"]}

    def test_monthly_overview(self):
        tool = self.make_tool()
        res = tool.run("monthly", """
            WITH g AS (SELECT strftime('%Y-%m', order_date) AS month, SUM(amount_cents) AS gross
                       FROM orders GROUP BY month),
                 r AS (SELECT strftime('%Y-%m', refund_date) AS month, SUM(amount_cents) AS refunds
                       FROM refunds GROUP BY month),
                 m AS (SELECT month FROM g UNION SELECT month FROM r)
            SELECT m.month, COALESCE(g.gross, 0), COALESCE(r.refunds, 0),
                   COALESCE(g.gross, 0) - COALESCE(r.refunds, 0)
            FROM m LEFT JOIN g USING (month) LEFT JOIN r USING (month)
            ORDER BY m.month""")
        self.assertOk(res)
        expected = [[p["start"][:7], p["gross_cents"], p["refunds_cents"], p["net_cents"]]
                    for p in EXPECTED["monthly_overview"]]
        self.assertEqual(res["rows"], expected)
        # The same months via explicit half-open periods.
        for i, p in enumerate(EXPECTED["monthly_overview"]):
            got = self.period(tool, f"monthly-{i}", p["start"], p["end_exclusive"])
            self.assertEqual(got, {k: p[k] for k in ("gross_cents", "refunds_cents", "net_cents")})

    def test_r1_main_investigation(self):
        case = CASES["R1"]
        tool = self.make_tool()
        got = []
        for p in case["periods"]:
            totals = self.period(tool, "R1", p["start"], p["end_exclusive"])
            self.assertEqual(totals, {k: p[k] for k in ("gross_cents", "refunds_cents", "net_cents")})
            got.append(totals)
        self.assertEqual(got[1]["net_cents"] - got[0]["net_cents"], case["net_change_cents"])
        self.assertEqual(got[1]["gross_cents"] - got[0]["gross_cents"], case["gross_change_cents"])
        self.assertEqual(got[1]["refunds_cents"] - got[0]["refunds_cents"], case["refunds_change_cents"])

    def test_r2_multiple_refunds_counted_once(self):
        case = CASES["R2"]
        tool = self.make_tool()
        sep = self.period(tool, "R2", "2026-09-01", "2026-10-01")
        self.assertEqual(sep["gross_cents"], case["september_gross_cents"])
        june = self.period(tool, "R2", "2026-06-01", "2026-07-01")
        self.assertEqual(june["gross_cents"], case["supplementary"]["june_gross_cents"])
        res = tool.run("R2", """
            SELECT o.order_id, o.amount_cents,
                   (SELECT SUM(amount_cents) FROM refunds r WHERE r.order_id = o.order_id),
                   (SELECT COUNT(*) FROM orders o2 WHERE o2.order_id = o.order_id)
            FROM orders o WHERE o.order_id IN ('O3', 'O8') ORDER BY o.order_id""")
        self.assertOk(res)
        for order, row in zip([case["order"], case["supplementary"]["order"]], res["rows"]):
            self.assertEqual(row, [order["order_id"], order["amount_cents"],
                                   order["refund_total_cents"], order["times_counted_in_gross"]])
        # The anti-expectation: a raw join repeats O3 (comparison only).
        naive = tool.run("R2", "SELECT SUM(o.amount_cents) FROM orders o "
                               "LEFT JOIN refunds r ON r.order_id = o.order_id "
                               "WHERE o.order_date >= '2026-09-01' AND o.order_date < '2026-10-01'")
        self.assertOk(naive)
        self.assertEqual(naive["rows"], [[case["anti_expectation"]["naive_join_gross_cents"]]])
        self.assertNotEqual(naive["rows"][0][0], case["september_gross_cents"])

    def check_breakdown(self, case_id):
        case = CASES[case_id]
        tool = self.make_tool()
        start, end = case["period"]["start"], case["period"]["end_exclusive"]
        got = self.segments(tool, case_id, start, end)
        for seg, exp in case["segments"].items():
            self.assertEqual(got[seg], {k: exp[k] for k in ("gross_cents", "refunds_cents", "net_cents")})
        self.assertEqual(set(got), set(case["segments"]))
        totals = self.period(tool, case_id, start, end)
        self.assertEqual(totals, case["period_totals"])
        for k in ("gross_cents", "refunds_cents", "net_cents"):
            self.assertEqual(sum(s[k] for s in got.values()), totals[k], k)

    def test_r3_september_by_segment(self):
        self.check_breakdown("R3")

    def test_r3b_october_by_segment(self):
        self.check_breakdown("R3b")

    def test_r4_boundary(self):
        case = CASES["R4"]
        tool = self.make_tool()
        flags = ("CASE WHEN {d} >= '2026-09-01' AND {d} < '2026-10-01' THEN 1 ELSE 0 END, "
                 "CASE WHEN {d} >= '2026-10-01' AND {d} < '2026-11-01' THEN 1 ELSE 0 END")
        res = tool.run("R4", f"""
            SELECT 'order', order_id, order_date, amount_cents, {flags.format(d='order_date')}
            FROM orders WHERE order_date = '2026-10-01'
            UNION ALL
            SELECT 'refund', refund_id, refund_date, amount_cents, {flags.format(d='refund_date')}
            FROM refunds WHERE refund_date = '2026-10-01'
            ORDER BY 1, 2""")
        self.assertOk(res)
        expected = sorted([[b["kind"], b["id"], b["date"], b["amount_cents"],
                            int(b["in_september"]), int(b["in_october"])] for b in case["boundary_rows"]])
        self.assertEqual(res["rows"], expected)
        self.assertEqual(self.period(tool, "R4", "2026-09-01", "2026-10-01"), case["september"])
        self.assertEqual(self.period(tool, "R4", "2026-10-01", "2026-11-01"), case["october"])
        sup = case["supplementary"]
        o19 = tool.run("R4", f"""
            SELECT amount_cents,
                   order_date >= '2026-07-01' AND order_date < '2026-08-01',
                   order_date >= '2026-08-01' AND order_date < '2026-09-01'
            FROM orders WHERE order_id = '{sup["order_id"]}'""")
        self.assertOk(o19)
        self.assertEqual(o19["rows"], [[sup["amount_cents"], int(sup["in_july"]), int(sup["in_august"])]])

    def test_r5_refund_in_later_period(self):
        case = CASES["R5"]
        tool = self.make_tool()
        ids = ", ".join(f"'{r['refund_id']}'" for r in case["refunds"])
        res = tool.run("R5", f"""
            SELECT rf.refund_id, rf.refund_date, rf.amount_cents, o.order_id, o.order_date,
                   c.customer_id, c.segment,
                   strftime('%Y-%m', rf.refund_date) AS counted_in_month_of,
                   strftime('%Y-%m', o.order_date) AS order_month
            FROM refunds rf
            JOIN orders o ON o.order_id = rf.order_id
            JOIN customers c ON c.customer_id = o.customer_id
            WHERE rf.refund_id IN ({ids}) ORDER BY rf.refund_id""")
        self.assertOk(res)
        got = [dict(zip(res["columns"], row)) for row in res["rows"]]
        self.assertEqual(got, sorted(case["refunds"], key=lambda r: r["refund_id"]))
        for month, start, end in (("july", "2026-07-01", "2026-08-01"),
                                  ("october", "2026-10-01", "2026-11-01")):
            exp = case[month]
            self.assertEqual(self.period(tool, f"R5-{month}", start, end), exp["all"])
            segs = self.segments(tool, f"R5-{month}", start, end)
            self.assertEqual(segs, {"small": exp["small"], "large": exp["large"]})


class RejectionTests(ToolTestCase):
    """Writes and forbidden reads are denied; the file and its rows stay unchanged."""

    def assertRejectedUnchanged(self, sql, expected_status="denied"):
        tool = self.make_tool()
        hash_before, counts_before = sha256(self.db), self.table_counts()
        files_before = sorted(p.name for p in self.db_dir.iterdir())
        res = tool.run("q1", sql)
        tool.close()
        self.assertEqual(res["status"], expected_status, res["error"])
        self.assertTrue(res["error"])
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["attempt_number"], 1)
        self.assertEqual(sha256(self.db), hash_before)
        self.assertEqual(self.table_counts(), counts_before)
        self.assertEqual(sorted(p.name for p in self.db_dir.iterdir()), files_before)
        return res

    def test_insert(self):
        res = self.assertRejectedUnchanged(
            "INSERT INTO customers (customer_id, segment) VALUES ('C99', 'small')")
        self.assertIn("INSERT", res["error"])

    def test_update(self):
        self.assertRejectedUnchanged("UPDATE orders SET amount_cents = 0")

    def test_delete(self):
        self.assertRejectedUnchanged("DELETE FROM refunds")

    def test_drop(self):
        self.assertRejectedUnchanged("DROP TABLE orders")

    def test_create(self):
        self.assertRejectedUnchanged("CREATE TABLE notes (x TEXT)")

    def test_create_temp(self):
        self.assertRejectedUnchanged("CREATE TEMP TABLE notes (x TEXT)")

    def test_attach(self):
        # The file listing check also shows no other.sqlite was created.
        self.assertRejectedUnchanged(f"ATTACH DATABASE '{self.db_dir / 'other.sqlite'}' AS other")

    def test_pragma(self):
        self.assertRejectedUnchanged("PRAGMA query_only = OFF")

    def test_pragma_read(self):
        self.assertRejectedUnchanged("PRAGMA table_info(orders)")

    def test_multiple_statements(self):
        res = self.assertRejectedUnchanged("SELECT 1; DROP TABLE orders")
        self.assertIn("one statement", res["error"])

    def test_with_insert(self):
        self.assertRejectedUnchanged(
            "WITH t AS (SELECT 'C99' AS id) INSERT INTO customers SELECT id, 'small' FROM t")

    def test_sqlite_master(self):
        res = self.assertRejectedUnchanged("SELECT name, sql FROM sqlite_master")
        self.assertIn("sqlite_master", res["error"])

    def test_sqlite_schema(self):
        self.assertRejectedUnchanged("SELECT name FROM sqlite_schema")

    def test_pragma_table_info_function(self):
        self.assertRejectedUnchanged("SELECT * FROM pragma_table_info('orders')")

    def test_load_extension(self):
        res = self.assertRejectedUnchanged("SELECT load_extension('evil')")
        self.assertIn("load_extension", res["error"])

    def test_function_not_on_allow_list(self):
        self.assertRejectedUnchanged("SELECT randomblob(10)")

    def test_transaction(self):
        self.assertRejectedUnchanged("BEGIN")


class AttemptLimitTests(ToolTestCase):
    def test_seventh_attempt_refused(self):
        tool = self.make_tool()
        for n in range(1, 7):
            self.assertEqual(tool.run("q1", "SELECT 1")["attempt_number"], n)
        hash_before = sha256(self.db)
        res = tool.run("q1", "INSERT INTO customers VALUES ('C99', 'small')")
        self.assertEqual(res["status"], "refused_limit")
        self.assertEqual(res["attempt_number"], 7)
        self.assertIn("Attempt limit", res["error"])
        self.assertEqual(sha256(self.db), hash_before)

    def test_failed_and_rejected_attempts_count(self):
        tool = self.make_tool()
        tool.run("q1", "SELEC 1")                       # syntax error
        tool.run("q1", "DELETE FROM orders")            # denied
        tool.run("q1", "SELECT * FROM nope")            # unknown table
        tool.run("q1", "SELECT 1; SELECT 2")            # multiple statements
        tool.run("q1", "SELECT 1")
        tool.run("q1", "SELECT 1")
        self.assertEqual(tool.run("q1", "SELECT 1")["status"], "refused_limit")

    def test_counters_independent_per_question(self):
        tool = self.make_tool()
        for _ in range(6):
            tool.run("q1", "SELECT 1")
        self.assertEqual(tool.run("q1", "SELECT 1")["status"], "refused_limit")
        res = tool.run("q2", "SELECT 1")
        self.assertOk(res)
        self.assertEqual(res["attempt_number"], 1)

    def test_counter_rebuilt_from_log_across_instances(self):
        first = self.make_tool()
        for _ in range(4):
            first.run("q1", "SELECT 1")
        first.close()
        second = self.make_tool()
        self.assertEqual(second.run("q1", "SELECT 1")["attempt_number"], 5)
        self.assertEqual(second.run("q1", "SELECT 1")["attempt_number"], 6)
        second.close()
        third = self.make_tool()
        res = third.run("q1", "SELECT 1")
        self.assertEqual(res["status"], "refused_limit")
        self.assertEqual(res["attempt_number"], 7)
        self.assertEqual(third.run("q2", "SELECT 1")["attempt_number"], 1)

    def test_unreadable_log_fails_closed(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text('{"question_id": "q1"}\nnot json\n', encoding="utf-8")
        tool = self.make_tool()
        with self.assertRaises(RuntimeError):
            tool.run("q1", "SELECT 1")


class LoggingTests(ToolTestCase):
    def test_log_contains_every_attempt(self):
        tool = self.make_tool()
        calls = [
            ("q1", "SELECT order_id FROM orders WHERE order_id = 'O1'"),
            ("q1", "SELEC 1"),
            ("q1", "DROP TABLE orders"),
            ("q1", "SELECT * FROM sqlite_master"),
            ("q2", "SELECT 1"),
            ("q1", "SELECT * FROM nope"),
            ("q1", "SELECT 1"),
            ("q1", "SELECT 1"),
        ]
        results = [tool.run(qid, sql) for qid, sql in calls]
        entries = self.log_entries()
        self.assertEqual(len(entries), len(calls))
        for (qid, sql), res, entry in zip(calls, results, entries):
            self.assertEqual(entry["question_id"], qid)
            self.assertEqual(entry["sql"], sql)
            self.assertEqual(entry["status"], res["status"])
            self.assertEqual(entry["attempt_number"], res["attempt_number"])
            self.assertEqual(entry["error"], res["error"])
            self.assertEqual(entry["columns"], res["columns"])
            self.assertEqual(entry["rows"], res["rows"])
            self.assertIsInstance(entry["duration_ms"], (int, float))
            self.assertIn("timestamp", entry)
        self.assertEqual([e["status"] for e in entries],
                         ["ok", "syntax_error", "denied", "denied", "ok",
                          "unknown_table", "ok", "refused_limit"])
        self.assertEqual(entries[0]["rows"], [["O1"]])
        self.assertIsNone(entries[0]["error"])

    def test_log_not_written_into_database(self):
        tool = self.make_tool()
        hash_before = sha256(self.db)
        tool.run("q1", "SELECT 1")
        tool.run("q1", "DELETE FROM orders")
        self.assertEqual(sha256(self.db), hash_before)
        self.assertEqual(self.table_counts(), {"customers": len(SNAPSHOT["customers"]),
                                               "orders": len(SNAPSHOT["orders"]),
                                               "refunds": len(SNAPSHOT["refunds"])})


if __name__ == "__main__":
    unittest.main()
