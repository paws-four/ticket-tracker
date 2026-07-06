"""SQLite data layer for the website tracker.

Plain sqlite3 (no ORM) to keep this small personal tool simple. All access
goes through short-lived connections with row_factory set to sqlite3.Row so
callers get dict-like rows.
"""
import json
import sqlite3
from datetime import datetime, timezone

import config

# Valid states for a tracked item / a check result.
STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"
STATE_AVAILABLE = "available"
STATES = (STATE_UNKNOWN, STATE_UNAVAILABLE, STATE_AVAILABLE)

MODE_KEYWORD = "keyword"
MODE_DIFF = "diff"
MODES = (MODE_KEYWORD, MODE_DIFF)


def utcnow_iso():
    """Timezone-aware UTC timestamp as ISO-8601 string (stored everywhere)."""
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they do not exist. Safe to call on every startup."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_items (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT    NOT NULL,
                url                 TEXT    NOT NULL,
                detection_mode      TEXT    NOT NULL DEFAULT 'keyword',
                available_phrases   TEXT    NOT NULL DEFAULT '[]',
                unavailable_phrases TEXT    NOT NULL DEFAULT '[]',
                check_interval_minutes INTEGER NOT NULL DEFAULT 15,
                is_active           INTEGER NOT NULL DEFAULT 1,
                current_state       TEXT    NOT NULL DEFAULT 'unknown',
                last_snapshot_hash  TEXT,
                consecutive_errors  INTEGER NOT NULL DEFAULT 0,
                last_fetch_method   TEXT,
                last_error          TEXT,
                last_checked_at     TEXT,
                created_at          TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS check_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                checked_at      TEXT    NOT NULL,
                detected_state  TEXT    NOT NULL,
                snippet         TEXT,
                fetch_method    TEXT,
                error           TEXT,
                FOREIGN KEY (tracked_item_id) REFERENCES tracked_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notification_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER,
                sent_at         TEXT    NOT NULL,
                channel         TEXT    NOT NULL,
                target          TEXT,
                success         INTEGER NOT NULL,
                error           TEXT,
                FOREIGN KEY (tracked_item_id) REFERENCES tracked_items(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_history_item
                ON check_history(tracked_item_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notif_item
                ON notification_log(tracked_item_id, sent_at DESC);
            """
        )


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------
def _item_from_row(row):
    """Convert a sqlite Row into a plain dict, decoding JSON phrase lists."""
    if row is None:
        return None
    d = dict(row)
    d["available_phrases"] = json.loads(d.get("available_phrases") or "[]")
    d["unavailable_phrases"] = json.loads(d.get("unavailable_phrases") or "[]")
    d["is_active"] = bool(d.get("is_active"))
    return d


# ---------------------------------------------------------------------------
# tracked_items CRUD
# ---------------------------------------------------------------------------
def list_items():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tracked_items ORDER BY created_at DESC"
        ).fetchall()
    return [_item_from_row(r) for r in rows]


def get_item(item_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tracked_items WHERE id = ?", (item_id,)
        ).fetchone()
    return _item_from_row(row)


def create_item(
    name,
    url,
    detection_mode=MODE_KEYWORD,
    available_phrases=None,
    unavailable_phrases=None,
    check_interval_minutes=None,
    is_active=True,
):
    if detection_mode not in MODES:
        detection_mode = MODE_KEYWORD
    if check_interval_minutes is None:
        check_interval_minutes = config.DEFAULT_CHECK_INTERVAL_MINUTES
    available_phrases = available_phrases or []
    unavailable_phrases = unavailable_phrases or []
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tracked_items
                (name, url, detection_mode, available_phrases, unavailable_phrases,
                 check_interval_minutes, is_active, current_state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                url,
                detection_mode,
                json.dumps(available_phrases),
                json.dumps(unavailable_phrases),
                int(check_interval_minutes),
                1 if is_active else 0,
                STATE_UNKNOWN,
                utcnow_iso(),
            ),
        )
        return cur.lastrowid


def update_item(
    item_id,
    name,
    url,
    detection_mode,
    available_phrases,
    unavailable_phrases,
    check_interval_minutes,
    is_active,
):
    if detection_mode not in MODES:
        detection_mode = MODE_KEYWORD
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tracked_items
               SET name = ?, url = ?, detection_mode = ?,
                   available_phrases = ?, unavailable_phrases = ?,
                   check_interval_minutes = ?, is_active = ?
             WHERE id = ?
            """,
            (
                name,
                url,
                detection_mode,
                json.dumps(available_phrases or []),
                json.dumps(unavailable_phrases or []),
                int(check_interval_minutes),
                1 if is_active else 0,
                item_id,
            ),
        )


def set_active(item_id, is_active):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_items SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, item_id),
        )


def set_all_active(is_active):
    """Pause or resume every item at once."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_items SET is_active = ?", (1 if is_active else 0,)
        )


def delete_item(item_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM tracked_items WHERE id = ?", (item_id,))


def record_check_result(
    item_id,
    detected_state,
    snippet,
    fetch_method,
    error,
    snapshot_hash=None,
):
    """Persist a check to history and update the item's live status columns.

    Returns the previous current_state (before this check) so the caller can
    decide whether an UNAVAILABLE -> AVAILABLE transition happened.
    """
    now = utcnow_iso()
    with get_conn() as conn:
        prev = conn.execute(
            "SELECT current_state FROM tracked_items WHERE id = ?", (item_id,)
        ).fetchone()
        previous_state = prev["current_state"] if prev else STATE_UNKNOWN

        conn.execute(
            """
            INSERT INTO check_history
                (tracked_item_id, checked_at, detected_state, snippet, fetch_method, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, now, detected_state, snippet, fetch_method, error),
        )

        if error:
            conn.execute(
                """
                UPDATE tracked_items
                   SET last_checked_at = ?, last_error = ?,
                       last_fetch_method = ?,
                       consecutive_errors = consecutive_errors + 1
                 WHERE id = ?
                """,
                (now, error, fetch_method, item_id),
            )
        else:
            conn.execute(
                """
                UPDATE tracked_items
                   SET current_state = ?, last_checked_at = ?, last_error = NULL,
                       last_fetch_method = ?, last_snapshot_hash = ?,
                       consecutive_errors = 0
                 WHERE id = ?
                """,
                (detected_state, now, fetch_method, snapshot_hash, item_id),
            )
    return previous_state


def get_consecutive_errors(item_id):
    item = get_item(item_id)
    return item["consecutive_errors"] if item else 0


# ---------------------------------------------------------------------------
# check_history
# ---------------------------------------------------------------------------
def list_history(item_id, limit=50):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM check_history
             WHERE tracked_item_id = ?
             ORDER BY checked_at DESC
             LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# notification_log
# ---------------------------------------------------------------------------
def log_notification(item_id, channel, target, success, error=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notification_log
                (tracked_item_id, sent_at, channel, target, success, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, utcnow_iso(), channel, target, 1 if success else 0, error),
        )


def list_notifications(item_id=None, limit=50):
    with get_conn() as conn:
        if item_id is None:
            rows = conn.execute(
                "SELECT * FROM notification_log ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM notification_log
                 WHERE tracked_item_id = ?
                 ORDER BY sent_at DESC LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def recent_notification_failures(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT n.*, t.name AS item_name
              FROM notification_log n
              LEFT JOIN tracked_items t ON t.id = n.tracked_item_id
             WHERE n.success = 0
             ORDER BY n.sent_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
