"""Read-only SQL query tool over data/dataset.sqlite.

Safety comes from SQLite itself, not from inspecting SQL text:
  1. the file is opened with a read-only URI (mode=ro);
  2. PRAGMA query_only=ON;
  3. an authorizer that allows only SELECT, reads of the three business
     tables, recursive CTEs and an allow list of built-in functions.

Limits (attempts per question, time, rows) come from config/limits.json.
Every attempt is appended as one JSON line to a separate log file; the log is
also the source of truth for the attempt counter, so a new QueryTool instance
cannot reset a question's budget.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_TABLES = frozenset({"customers", "orders", "refunds"})

# Built-in functions that only compute values. Anything not listed is denied,
# including load_extension, randomblob/zeroblob and the sqlite_* functions.
ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "total", "avg", "min", "max", "group_concat", "string_agg",
    # scalar text / number / null handling
    "abs", "round", "coalesce", "ifnull", "nullif", "iif", "length", "lower",
    "upper", "substr", "substring", "trim", "ltrim", "rtrim", "replace",
    "instr", "printf", "format", "typeof",
    # the LIKE and GLOB operators are reported to the authorizer as functions
    "like", "glob",
    # dates
    "date", "time", "datetime", "julianday", "strftime", "unixepoch",
    # window functions
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value", "nth_value",
})

# SQLite authorizer action codes (https://sqlite.org/c3ref/c_alter_table.html).
# Written out by number because Python's sqlite3 module also exports error
# codes with the same values (SQLITE_INSERT and SQLITE_TOOBIG are both 18), so
# looking names up in the module would give wrong labels.
ACTION_NAMES = {
    1: "CREATE INDEX", 2: "CREATE TABLE", 3: "CREATE TEMP INDEX",
    4: "CREATE TEMP TABLE", 5: "CREATE TEMP TRIGGER", 6: "CREATE TEMP VIEW",
    7: "CREATE TRIGGER", 8: "CREATE VIEW", 9: "DELETE", 10: "DROP INDEX",
    11: "DROP TABLE", 12: "DROP TEMP INDEX", 13: "DROP TEMP TABLE",
    14: "DROP TEMP TRIGGER", 15: "DROP TEMP VIEW", 16: "DROP TRIGGER",
    17: "DROP VIEW", 18: "INSERT", 19: "PRAGMA", 20: "READ", 21: "SELECT",
    22: "TRANSACTION", 23: "UPDATE", 24: "ATTACH", 25: "DETACH",
    26: "ALTER TABLE", 27: "REINDEX", 28: "ANALYZE", 29: "CREATE VIRTUAL TABLE",
    30: "DROP VIRTUAL TABLE", 31: "FUNCTION", 32: "SAVEPOINT", 33: "RECURSIVE",
}
ACTION_READ = 20
ACTION_SELECT = 21
ACTION_FUNCTION = 31
ACTION_RECURSIVE = 33

# How many SQLite VM instructions run between two deadline checks.
PROGRESS_STEPS = 1000

LIMIT_KEYS = ("max_attempts", "timeout_ms", "max_rows")


def load_limits(path):
    """Read and validate limits from a JSON file such as config/limits.json."""
    with open(path, encoding="utf-8") as f:
        limits = json.load(f)
    for key in LIMIT_KEYS:
        value = limits.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"limits: '{key}' must be a positive integer, got {value!r}")
    return {key: limits[key] for key in LIMIT_KEYS}


def _json_default(value):
    # Query results may contain BLOBs (e.g. CAST(x AS BLOB)); log them as hex.
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class QueryTool:
    def __init__(self, db_path, log_path, limits):
        self.db_path = Path(db_path).resolve()
        self.log_path = Path(log_path).resolve()
        if self.log_path == self.db_path:
            raise ValueError("log_path must not be the database file")
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        self.max_attempts = limits["max_attempts"]
        self.timeout_ms = limits["timeout_ms"]
        self.max_rows = limits["max_rows"]

        self._deadline = None      # monotonic time; None outside a query
        self._timed_out = False
        self._denial_reason = None  # set by the authorizer when it denies

        # mode=ro: SQLite opens the file read-only and never creates it.
        # isolation_level=None: Python sends no implicit BEGIN.
        # cached_statements=0: every statement is prepared (and authorized) anew.
        self._conn = sqlite3.connect(
            self.db_path.as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
            cached_statements=0,
        )
        # Must run before the authorizer is installed, since it denies PRAGMA.
        self._conn.execute("PRAGMA query_only=ON")
        self._conn.set_authorizer(self._authorize)
        self._conn.set_progress_handler(self._check_deadline, PROGRESS_STEPS)

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # Authorizer. SQLite calls this while preparing each statement, once per
    # action. Returning SQLITE_DENY makes the whole statement fail to prepare,
    # so nothing in it runs. Default is DENY: only the four cases below pass.
    # ------------------------------------------------------------------ #
    def _authorize(self, action, arg1, arg2, db_name, trigger_or_view):
        # SELECT: issued for every SELECT, sub-select and CTE body. Allowing it
        # alone reads nothing; reads are checked separately as READ below.
        if action == ACTION_SELECT:
            return sqlite3.SQLITE_OK

        # READ: arg1 = table, arg2 = column ('' for count(*)).
        # Allowed only for the three business tables in the main database.
        # db_name is None for count(*) (observed with SQLite 3.49.1), so None is
        # accepted alongside 'main'. This denies sqlite_master / sqlite_schema,
        # pragma_* table-valued functions (they appear as tables here) and any
        # table in temp or an attached database.
        #
        # Known limitation (kept on purpose): count(*) over a CTE that SQLite
        # does not flatten (recursive or MATERIALIZED) arrives here as a READ of
        # the CTE name with column '' and db None. That is indistinguishable
        # from count(*) on a real table such as sqlite_master, so it is denied.
        # count(column) and other aggregates over such CTEs are not affected.
        if action == ACTION_READ:
            if arg1 in ALLOWED_TABLES and db_name in ("main", None):
                return sqlite3.SQLITE_OK
            reason = (f"reading table '{arg1}' is not allowed; "
                      f"only {', '.join(sorted(ALLOWED_TABLES))}")
            if arg2 == "":
                reason += " (if this is a CTE, use count(column) instead of count(*))"
            return self._deny(reason)

        # FUNCTION: arg2 = function name. Allowed only if on the allow list.
        # This is where load_extension() is stopped.
        if action == ACTION_FUNCTION:
            if arg2 is not None and arg2.lower() in ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_OK
            return self._deny(f"function '{arg2}' is not allowed")

        # RECURSIVE: WITH RECURSIVE. Read-only; runaway recursion is stopped
        # by the time limit and the row cap.
        if action == ACTION_RECURSIVE:
            return sqlite3.SQLITE_OK

        # Everything else: INSERT, UPDATE, DELETE, all CREATE / DROP / ALTER,
        # PRAGMA (statement form; the table-valued form is also caught as
        # READ above), ATTACH, DETACH, TRANSACTION, SAVEPOINT, ANALYZE,
        # REINDEX, virtual tables, and any code added in a future SQLite.
        name = ACTION_NAMES.get(action, f"action {action}")
        return self._deny(f"{name} is not allowed (read-only tool)")

    def _deny(self, reason):
        # Keep the first reason: it names the action that stopped the statement.
        if self._denial_reason is None:
            self._denial_reason = reason
        return sqlite3.SQLITE_DENY

    # Progress handler: a non-zero return makes SQLite abort the running
    # statement with "interrupted". Runs during execute and during fetching.
    def _check_deadline(self):
        if self._deadline is not None and time.monotonic() > self._deadline:
            self._timed_out = True
            return 1
        return 0

    # ------------------------------------------------------------------ #
    def _attempts_used(self, question_id):
        """Count logged attempts for question_id (the log is the source of truth).

        An unreadable log line raises an error instead of being skipped:
        skipping could silently give a question more attempts.
        """
        if not self.log_path.exists():
            return 0
        used = 0
        with open(self.log_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"attempt log {self.log_path} line {line_no} is unreadable; "
                        "refusing to guess the attempt count") from exc
                if entry.get("question_id") == question_id:
                    used += 1
        return used

    def _write_log(self, entry):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=_json_default)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def run(self, question_id, sql):
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("question_id must be a non-empty string")
        if self._conn is None:
            raise RuntimeError("QueryTool is closed")

        attempt_number = self._attempts_used(question_id) + 1
        result = {
            "columns": [],
            "rows": [],
            "error": None,
            "truncated": False,
            "attempt_number": attempt_number,
            "status": "ok",
        }
        started = time.monotonic()

        if attempt_number > self.max_attempts:
            result["status"] = "refused_limit"
            result["error"] = (
                f"Attempt limit reached: {self.max_attempts} attempts already used "
                f"for question '{question_id}'; not executed")
        else:
            self._execute(sql, result, started)

        duration_ms = round((time.monotonic() - started) * 1000, 3)
        self._write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question_id": question_id,
            "attempt_number": attempt_number,
            "sql": sql,
            "status": result["status"],
            "columns": result["columns"],
            "rows": result["rows"],
            "truncated": result["truncated"],
            "error": result["error"],
            "duration_ms": duration_ms,
        })
        return result

    def _execute(self, sql, result, started):
        self._denial_reason = None
        self._timed_out = False
        self._deadline = started + self.timeout_ms / 1000
        cursor = None
        try:
            # Python's sqlite3 refuses a second statement in one execute()
            # call before stepping anything (ProgrammingError).
            cursor = self._conn.execute(sql)
            if cursor.description is None:
                # Only possible for empty or comment-only input: every other
                # statement without result columns is denied by the authorizer.
                result["status"] = "error"
                result["error"] = "No result columns: send exactly one SELECT statement"
                return
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(self.max_rows + 1)
            result["columns"] = columns
            result["truncated"] = len(rows) > self.max_rows
            result["rows"] = [list(r) for r in rows[: self.max_rows]]
        except (sqlite3.Error, sqlite3.Warning) as exc:
            result["status"], result["error"] = self._classify(exc)
        finally:
            self._deadline = None
            if cursor is not None:
                cursor.close()

    def _classify(self, exc):
        """Turn a sqlite3 exception into (status, message). Labelling only."""
        msg = str(exc)
        low = msg.lower()
        if self._timed_out:
            return "timeout", f"Timeout: query exceeded {self.timeout_ms} ms"
        if self._denial_reason is not None:
            return "denied", f"Denied: {self._denial_reason}"
        if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.Warning)) and "one statement" in low:
            return "denied", "Denied: only one statement per call"
        if "not authorized" in low or "prohibited" in low:
            return "denied", f"Denied: {msg}"
        if "readonly" in low or "read-only" in low:
            return "denied", "Denied: database is read-only"
        if "no such table" in low:
            return "unknown_table", (f"Unknown table: {msg.split(':', 1)[-1].strip()} "
                                     f"(available: {', '.join(sorted(ALLOWED_TABLES))})")
        if "syntax error" in low or "incomplete input" in low or "unrecognized token" in low:
            return "syntax_error", f"Syntax error: {msg}"
        return "error", f"Error: {msg}"
