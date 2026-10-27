# Known limitations

Accepted limitations of the current application. Each entry gives the effect and what to do instead.

## L1. Attempt counting is not safe for concurrent writers

`QueryTool` rebuilds the per-question attempt counter from the attempt log before every call (the log is the source of truth). Two `QueryTool` instances writing to the same log at the same time could both read the same count and use the same attempt number, so a question could get more than `max_attempts` attempts.

- Current design: single process, one caller at a time. Accepted.
- **Serialize access to the attempt log when a server is added** (for example one lock per log file, or a single writer).

## L2. `COUNT(*)` over a non-flattened named CTE is denied

SQLite reports `COUNT(*)` over a named CTE that it does not flatten into the outer query as a READ of the CTE name, with column `''` and no database name. A `COUNT(*)` on a real table such as `sqlite_master` is reported in exactly the same way, so the authorizer cannot tell them apart. The authorizer allows reads of `customers`, `orders` and `refunds` only, so these queries are denied. This is kept on purpose: loosening the rule would let `COUNT(*)` reach tables outside the three allowed ones.

The error message says: `... (if this is a CTE, use count(column) instead of count(*))`.

Observed with SQLite 3.49.1 (Python 3.12.10); covered by `CountVariantTests` and `test_count_star_on_recursive_cte_is_denied` in `tests/test_query_tool.py`.

| Query | Result |
|---|---|
| `SELECT COUNT(*) FROM orders` | allowed, 30 |
| `SELECT COUNT(*) FROM (SELECT customer_id FROM orders GROUP BY customer_id)` | allowed, 10 |
| `SELECT COUNT(DISTINCT customer_id) FROM orders` | allowed, 10 |
| `SELECT count(1) FROM orders` | allowed, 30 |
| `WITH per_order AS (SELECT order_id, SUM(amount_cents) AS total FROM orders GROUP BY order_id) SELECT count(order_id) FROM per_order` | allowed, 30 |
| `WITH per_order AS (... GROUP BY order_id) SELECT COUNT(*) FROM per_order` | **denied** |
| `WITH m AS MATERIALIZED (SELECT order_id FROM orders) SELECT COUNT(*) FROM m` | **denied** |
| `WITH RECURSIVE r(x) AS (...) SELECT count(*) FROM r` | **denied** |

Denied forms and the recommended alternative:

| Denied | Use instead |
|---|---|
| `COUNT(*)` over a CTE containing an aggregate / `GROUP BY` | `COUNT(<column>)` on a non-NULL column of the CTE (e.g. the grouping key), or move the CTE into an unnamed subquery: `SELECT COUNT(*) FROM (SELECT ... GROUP BY ...)` |
| `COUNT(*)` over a `MATERIALIZED` CTE | `COUNT(<column>)` on a non-NULL column, or drop `MATERIALIZED` so SQLite can flatten it |
| `COUNT(*)` over a `WITH RECURSIVE` CTE | `COUNT(<column>)`, e.g. `count(x)` |

`COUNT(column)` skips NULLs, so pick a column that is never NULL (a key or grouping column) when the full row count is wanted.
