"""Слой хранения данных (SQLite).

Запросы синхронные — для группы из 5 человек этого более чем достаточно,
каждая операция занимает микросекунды и не блокирует event loop заметным образом.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import config

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    author_id      INTEGER,
    author_name    TEXT,
    assignee_id    INTEGER,
    assignee_name  TEXT    NOT NULL,
    is_all         INTEGER NOT NULL DEFAULT 0,
    description    TEXT    NOT NULL,
    priority       TEXT    NOT NULL DEFAULT 'обычно',
    client_date    TEXT,
    needs_comment  INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'active',   -- active / done / cancelled
    comment_text   TEXT,
    completed_at   TEXT,
    completed_by   TEXT,
    last_nag_at    TEXT,
    snooze_until   TEXT
);

-- все копии сообщения о задаче (группа + лички), чтобы обновлять их разом
CREATE TABLE IF NOT EXISTS task_messages (
    task_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    full_name       TEXT,
    private_chat_id INTEGER,
    last_seen       TEXT
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id              INTEGER PRIMARY KEY,
    dashboard_message_id INTEGER,
    evening_time         TEXT    NOT NULL,
    morning_time         TEXT    NOT NULL,
    evening_on           INTEGER NOT NULL DEFAULT 1,
    morning_on           INTEGER NOT NULL DEFAULT 1,
    nag_on               INTEGER NOT NULL DEFAULT 1,
    nag_until            TEXT    NOT NULL DEFAULT '22:00'
);

-- кто сейчас должен прислать комментарий к какой задаче
CREATE TABLE IF NOT EXISTS pending_comments (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    task_id    INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    mode       TEXT    NOT NULL DEFAULT 'close',
    PRIMARY KEY (chat_id, user_id)
);

-- кто состоит в каком чате (для списка исполнителей и рассылок)
CREATE TABLE IF NOT EXISTS chat_members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

-- незавершённый конструктор задачи: по одному на человека в чате
CREATE TABLE IF NOT EXISTS drafts (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    assignee_id   INTEGER,
    assignee_name TEXT,
    is_all        INTEGER NOT NULL DEFAULT 0,
    priority      TEXT    NOT NULL DEFAULT 'обычно',
    client_date   TEXT,
    needs_comment INTEGER NOT NULL DEFAULT 0,
    description   TEXT,
    message_id    INTEGER,
    awaiting      TEXT,
    updated_at    TEXT,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_chat_status ON tasks(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee    ON tasks(assignee_id, status);
"""


def now() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


# колонки, добавленные после первого релиза: база у людей уже работает,
# CREATE TABLE IF NOT EXISTS их не создаст — доливаем через ALTER TABLE
MIGRATIONS = [
    ("tasks", "last_nag_at", "last_nag_at TEXT"),
    ("tasks", "snooze_until", "snooze_until TEXT"),
    ("chat_settings", "nag_on", "nag_on INTEGER NOT NULL DEFAULT 1"),
    ("chat_settings", "nag_until", "nag_until TEXT NOT NULL DEFAULT '22:00'"),
    ("drafts", "updated_at", "updated_at TEXT"),
    ("pending_comments", "mode", "mode TEXT NOT NULL DEFAULT 'close'"),
]


def _migrate() -> None:
    for table, column, ddl in MIGRATIONS:
        existing = {row["name"] for row in _all(f"PRAGMA table_info({table})")}
        if column not in existing:
            _run(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init(path: str | None = None) -> None:
    global _conn
    _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    _conn.commit()
    _migrate()


def _run(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, tuple(params))
        _conn.commit()
        return cur


def _one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, tuple(params)).fetchone()


def _all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, tuple(params)).fetchall()


# ---------------------------------------------------------------- пользователи

def upsert_user(user_id: int, username: str | None, full_name: str,
                private_chat_id: int | None = None) -> None:
    existing = _one("SELECT private_chat_id FROM users WHERE user_id = ?", (user_id,))
    if existing and private_chat_id is None:
        private_chat_id = existing["private_chat_id"]
    _run(
        """INSERT INTO users (user_id, username, full_name, private_chat_id, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               username = excluded.username,
               full_name = excluded.full_name,
               private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id),
               last_seen = excluded.last_seen""",
        (user_id, (username or "").lstrip("@") or None, full_name, private_chat_id, now()),
    )


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    return _one("SELECT * FROM users WHERE user_id = ?", (user_id,))


def find_user_by_username(username: str) -> Optional[sqlite3.Row]:
    return _one(
        "SELECT * FROM users WHERE lower(username) = lower(?)",
        (username.lstrip("@"),),
    )


def find_user_by_name(name: str) -> Optional[sqlite3.Row]:
    rows = _all(
        "SELECT * FROM users WHERE lower(full_name) LIKE lower(?)",
        (f"%{name}%",),
    )
    return rows[0] if len(rows) == 1 else None


def known_users() -> list[sqlite3.Row]:
    return _all("SELECT * FROM users ORDER BY full_name")


# ------------------------------------------------------- участники чата

def add_member(chat_id: int, user_id: int) -> None:
    _run("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?,?)",
         (chat_id, user_id))


def chat_members(chat_id: int, fallback: bool = True) -> list[sqlite3.Row]:
    """fallback=False — строго участники этого чата, без подмешивания остальных."""
    rows = _all(
        """SELECT u.* FROM users u
           JOIN chat_members m ON m.user_id = u.user_id
           WHERE m.chat_id = ? ORDER BY u.full_name""",
        (chat_id,),
    )
    if rows or not fallback:
        return rows
    return known_users()


# ------------------------------------------------------------ черновики

DRAFT_FIELDS = {"assignee_id", "assignee_name", "is_all", "priority",
                "client_date", "needs_comment", "description",
                "message_id", "awaiting", "updated_at"}


def is_stale(iso_value: str | None, minutes: int) -> bool:
    """Просрочен ли момент времени: используется для черновиков и ожидания комментария."""
    if not iso_value:
        return True
    try:
        moment = datetime.fromisoformat(iso_value)
    except ValueError:
        return True
    return datetime.now(config.TZ) - moment > timedelta(minutes=minutes)


def draft_start(chat_id: int, user_id: int, awaiting: str | None = "text") -> None:
    _run("DELETE FROM drafts WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    _run("INSERT INTO drafts (chat_id, user_id, awaiting, updated_at) VALUES (?,?,?,?)",
         (chat_id, user_id, awaiting, now()))


def get_draft(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    return _one("SELECT * FROM drafts WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id))


def update_draft(chat_id: int, user_id: int, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if k in DRAFT_FIELDS}
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    _run(f"UPDATE drafts SET {sets} WHERE chat_id = ? AND user_id = ?",
         (*fields.values(), chat_id, user_id))


def clear_draft(chat_id: int, user_id: int) -> None:
    _run("DELETE FROM drafts WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))


# --------------------------------------------------------------------- задачи

def add_task(chat_id: int, author_id: int, author_name: str, assignee_id: int | None,
             assignee_name: str, is_all: bool, description: str, priority: str,
             client_date: str | None, needs_comment: bool) -> int:
    # гарантируем строку настроек: по ней сверка планировщика находит чат
    # и включает утренние, вечерние напоминания
    get_settings(chat_id)
    cur = _run(
        """INSERT INTO tasks (chat_id, created_at, author_id, author_name, assignee_id,
                              assignee_name, is_all, description, priority, client_date,
                              needs_comment, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active')""",
        (chat_id, now(), author_id, author_name, assignee_id, assignee_name,
         int(is_all), description, priority, client_date, int(needs_comment)),
    )
    return int(cur.lastrowid)


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    return _one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def close_task(task_id: int, status: str, by_name: str, comment: str | None = None) -> None:
    _run(
        """UPDATE tasks SET status = ?, completed_at = ?, completed_by = ?,
                            comment_text = COALESCE(?, comment_text)
           WHERE id = ?""",
        (status, now(), by_name, comment, task_id),
    )


def backdate_task(task_id: int, days: int = 1) -> bool:
    """Сдвигает дату создания задачи назад — нужно только для тестов."""
    task = get_task(task_id)
    if task is None:
        return False
    created = datetime.fromisoformat(task["created_at"]) - timedelta(days=days)
    _run("UPDATE tasks SET created_at = ? WHERE id = ?",
         (created.isoformat(timespec="seconds"), task_id))
    return True


def update_comment(task_id: int, comment: str) -> None:
    _run("UPDATE tasks SET comment_text = ? WHERE id = ?", (comment, task_id))


def reopen_task(task_id: int) -> None:
    """Возвращает закрытую задачу в работу. Комментарий сохраняем как след правки."""
    _run(
        """UPDATE tasks SET status = 'active', completed_at = NULL, completed_by = NULL,
                            last_nag_at = NULL, snooze_until = NULL
           WHERE id = ?""",
        (task_id,),
    )


def user_chats(user_id: int) -> list[int]:
    return [r["chat_id"] for r in
            _all("SELECT chat_id FROM chat_members WHERE user_id = ?", (user_id,))]


def history_for_user(user_id: int, days: int, only_mine: bool = True) -> list[sqlite3.Row]:
    """История по всем чатам, где состоит человек. Для личного кабинета."""
    chats = user_chats(user_id)
    if not chats:
        return []
    since = (datetime.now(config.TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    marks = ",".join("?" * len(chats))
    mine = " AND (assignee_id = ? OR is_all = 1)" if only_mine else ""
    params = [*chats, since] + ([user_id] if only_mine else [])
    return _all(
        f"""SELECT * FROM tasks
            WHERE chat_id IN ({marks}) AND status IN ('done','cancelled')
              AND completed_at >= ?{mine}
            ORDER BY completed_at DESC""",
        params,
    )


def active_tasks(chat_id: int, assignee_id: int | None = None) -> list[sqlite3.Row]:
    if assignee_id is None:
        return _all(
            """SELECT * FROM tasks WHERE chat_id = ? AND status = 'active'
               ORDER BY CASE priority WHEN 'срочно' THEN 0 WHEN 'важно' THEN 1 ELSE 2 END,
                        id""",
            (chat_id,),
        )
    return _all(
        """SELECT * FROM tasks WHERE chat_id = ? AND status = 'active'
             AND (assignee_id = ? OR is_all = 1)
           ORDER BY CASE priority WHEN 'срочно' THEN 0 WHEN 'важно' THEN 1 ELSE 2 END,
                    id""",
        (chat_id, assignee_id),
    )


def active_tasks_for_user_all_chats(user_id: int) -> list[sqlite3.Row]:
    return _all(
        """SELECT * FROM tasks WHERE status = 'active' AND (assignee_id = ? OR is_all = 1)
           ORDER BY CASE priority WHEN 'срочно' THEN 0 WHEN 'важно' THEN 1 ELSE 2 END, id""",
        (user_id,),
    )


def stale_tasks(chat_id: int) -> list[sqlite3.Row]:
    """Активные задачи, созданные раньше сегодняшнего дня."""
    today = datetime.now(config.TZ).date().isoformat()
    return _all(
        """SELECT * FROM tasks WHERE chat_id = ? AND status = 'active'
             AND substr(created_at, 1, 10) < ?
           ORDER BY CASE priority WHEN 'срочно' THEN 0 WHEN 'важно' THEN 1 ELSE 2 END, id""",
        (chat_id, today),
    )


def overdue_tasks(chat_id: int, days: int | None = None) -> list[sqlite3.Row]:
    """Активные задачи, не закрытые за отведённые сутки."""
    days = config.OVERDUE_DAYS if days is None else days
    cutoff = (datetime.now(config.TZ).date() - timedelta(days=days - 1)).isoformat()
    return _all(
        """SELECT * FROM tasks WHERE chat_id = ? AND status = 'active'
             AND substr(created_at, 1, 10) < ?
           ORDER BY CASE priority WHEN 'срочно' THEN 0 WHEN 'важно' THEN 1 ELSE 2 END,
                    id""",
        (chat_id, cutoff),
    )


def mark_nagged(task_id: int) -> None:
    _run("UPDATE tasks SET last_nag_at = ? WHERE id = ?", (now(), task_id))


def snooze_task(task_id: int, minutes: int) -> str:
    until = datetime.now(config.TZ) + timedelta(minutes=minutes)
    value = until.isoformat(timespec="seconds")
    _run("UPDATE tasks SET snooze_until = ? WHERE id = ?", (value, task_id))
    return until.strftime("%H:%M")


def history(chat_id: int, days: int) -> list[sqlite3.Row]:
    since = (datetime.now(config.TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    return _all(
        """SELECT * FROM tasks
           WHERE chat_id = ? AND status IN ('done', 'cancelled') AND completed_at >= ?
           ORDER BY completed_at DESC""",
        (chat_id, since),
    )


# ----------------------------------------------------- сообщения о задачах

def add_task_message(task_id: int, chat_id: int, message_id: int) -> None:
    _run(
        "INSERT OR IGNORE INTO task_messages (task_id, chat_id, message_id) VALUES (?,?,?)",
        (task_id, chat_id, message_id),
    )


def task_messages(task_id: int) -> list[sqlite3.Row]:
    return _all("SELECT * FROM task_messages WHERE task_id = ?", (task_id,))


# --------------------------------------------------------- настройки чата

def get_settings(chat_id: int) -> sqlite3.Row:
    row = _one("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
    if row is None:
        _run(
            """INSERT INTO chat_settings (chat_id, evening_time, morning_time, nag_until)
               VALUES (?, ?, ?, ?)""",
            (chat_id, config.DEFAULT_EVENING, config.DEFAULT_MORNING,
             config.DEFAULT_NAG_UNTIL),
        )
        row = _one("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
    return row


def update_settings(chat_id: int, **fields: Any) -> None:
    get_settings(chat_id)
    allowed = {"dashboard_message_id", "evening_time", "morning_time",
               "evening_on", "morning_on", "nag_on", "nag_until"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    _run(f"UPDATE chat_settings SET {sets} WHERE chat_id = ?",
         (*fields.values(), chat_id))


def all_chats() -> list[sqlite3.Row]:
    return _all("SELECT * FROM chat_settings")


# ------------------------------------------------- ожидание комментария

def set_pending(chat_id: int, user_id: int, task_id: int,
                mode: str = "close") -> None:
    """mode: close — закрыть задачу комментарием, edit — переписать комментарий."""
    _run(
        """INSERT INTO pending_comments (chat_id, user_id, task_id, created_at, mode)
           VALUES (?,?,?,?,?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
               task_id = excluded.task_id, created_at = excluded.created_at,
               mode = excluded.mode""",
        (chat_id, user_id, task_id, now(), mode),
    )


def get_pending(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    return _one(
        "SELECT * FROM pending_comments WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )


def clear_pending(chat_id: int, user_id: int) -> None:
    _run("DELETE FROM pending_comments WHERE chat_id = ? AND user_id = ?",
         (chat_id, user_id))


def clear_pending_for_task(task_id: int) -> None:
    _run("DELETE FROM pending_comments WHERE task_id = ?", (task_id,))
