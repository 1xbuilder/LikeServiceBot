"""Тексты сообщений и inline-клавиатуры."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from html import escape

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)

import config
from parsing import PRIORITY_EMOJI

Task = sqlite3.Row


def emoji(task: Task) -> str:
    return PRIORITY_EMOJI.get(task["priority"], "🟢")


def assignee(task: Task) -> str:
    return "Все" if task["is_all"] else task["assignee_name"]


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _limit(text: str, tail: str = "") -> str:
    """Telegram отвергает сообщения длиннее 4096 символов — режем по строкам."""
    if len(text) <= config.MAX_MESSAGE_CHARS:
        return text
    lines, total = [], 0
    for line in text.split("\n"):
        if total + len(line) + 1 > config.MAX_MESSAGE_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    lines.append(tail or "\n<i>…список обрезан, слишком длинный</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ карточка

def task_card(task: Task) -> str:
    lines = [
        f"{emoji(task)} <b>Задача #{task['id']}</b>",
        escape(task["description"]),
        "",
        f"Ответственный: <b>{escape(assignee(task))}</b>",
        f"Приоритет: {escape(task['priority'])}",
    ]
    if task["client_date"]:
        lines.append(f"Клиент: <b>{escape(task['client_date'])}</b>")
    if task["needs_comment"]:
        lines.append("💬 При закрытии нужен комментарий")
    if task["author_name"]:
        lines.append(f"<i>Поставил: {escape(task['author_name'])}</i>")
    return "\n".join(lines)


def closed_card(task: Task) -> str:
    mark = "✅ Выполнено" if task["status"] == "done" else "✖️ Отменена"
    lines = [
        f"{mark} — <b>задача #{task['id']}</b>",
        f"<s>{escape(task['description'])}</s>",
        "",
        f"Ответственный: {escape(assignee(task))}",
    ]
    if task["completed_by"]:
        lines.append(f"Закрыл: {escape(task['completed_by'])}")
    if task["comment_text"]:
        lines.append(f"💬 {escape(task['comment_text'])}")
    return "\n".join(lines)


def overdue_days(task) -> int:
    """Сколько полных суток задача висит сверх отведённого срока. 0 — не просрочена."""
    if task["status"] != "active":
        return 0
    created = datetime.fromisoformat(task["created_at"]).date()
    today = datetime.now(config.TZ).date()
    return max(0, (today - created).days - (config.OVERDUE_DAYS - 1))


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def task_line(task) -> str:
    """Строка для дашборда / списков."""
    late = overdue_days(task)
    parts = []
    if late:
        parts.append("🔥 ")
    parts.append(f"{emoji(task)} <b>{escape(assignee(task))}</b> — {escape(task['description'])}")
    if late:
        parts.append(f" <b>[просрочено, {_plural_days(late)}]</b>")
    if task["client_date"]:
        parts.append(f" <i>(клиент: {escape(task['client_date'])})</i>")
    if task["needs_comment"]:
        parts.append(" 💬")
    return "".join(parts)


def nag_card(task) -> str:
    late = overdue_days(task)
    interval = config.NAG_INTERVALS.get(task["priority"], 720)
    lines = [
        f"🔥 <b>Просрочено: {_plural_days(late)}</b>",
        "",
        f"{emoji(task)} <b>Задача #{task['id']}</b> ({task['priority']})",
        escape(task["description"]),
    ]
    if task["client_date"]:
        lines.append(f"Клиент: {escape(task['client_date'])}")
    if task["needs_comment"]:
        lines.append("💬 При закрытии нужен комментарий")
    lines += ["", f"<i>Буду напоминать каждые {_human_interval(interval)}, "
                  f"пока задача не закрыта.</i>"]
    return "\n".join(lines)


def _human_interval(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes / 60
    if hours == int(hours):
        hours = int(hours)
        if hours == 1:
            return "час"
        if hours in (2, 3, 4):
            return f"{hours} часа"
        return f"{hours} часов"
    return f"{hours:.1f} ч"


def nag_keyboard(task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_done_button(task)],
        [InlineKeyboardButton(f"⏭ Отложить на {_human_interval(config.SNOOZE_MINUTES)}",
                              callback_data=f"snooze:{task['id']}"),
         InlineKeyboardButton("✖️ Отменить", callback_data=f"cancel:{task['id']}")],
    ])


def dashboard_text(tasks: list[Task]) -> str:
    if not tasks:
        return "📋 <b>Задачи менеджеров</b>\n\nАктивных задач нет 🎉"
    shown = tasks[: config.MAX_DASHBOARD_TASKS]
    lines = ["📋 <b>Задачи менеджеров</b>", ""]
    lines += [f"#{t['id']} {task_line(t)}" for t in shown]
    if len(tasks) > len(shown):
        lines.append(f"\n<i>…и ещё {len(tasks) - len(shown)}. Полный список: /list</i>")
    return _limit("\n".join(lines))


def history_text(tasks: list[Task], days: int) -> str:
    if not tasks:
        return f"За последние {days} дн. закрытых задач нет."
    lines = [f"🗂 <b>История за {days} дн.</b>", ""]
    for t in tasks:
        mark = "✅" if t["status"] == "done" else "✖️"
        when = (t["completed_at"] or "")[:16].replace("T", " ")
        lines.append(f"{mark} #{t['id']} {escape(assignee(t))} — {escape(t['description'])}")
        lines.append(f"    <i>{when}, закрыл {escape(t['completed_by'] or '—')}</i>")
        if t["comment_text"]:
            lines.append(f"    💬 {escape(t['comment_text'])}")
    return _limit("\n".join(lines),
                  "\n<i>…показаны не все. Уменьши период кнопками ниже.</i>")


# --------------------------------------------------------------- клавиатуры

def _done_button(task: Task) -> InlineKeyboardButton:
    label = "✅ Выполнено (нужен комм.)" if task["needs_comment"] else "✅ Выполнено"
    return InlineKeyboardButton(label, callback_data=f"done:{task['id']}")


def task_keyboard(task: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_done_button(task)],
        [InlineKeyboardButton("✖️ Отменить", callback_data=f"cancel:{task['id']}")],
    ])


def dashboard_keyboard(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    rows = []
    for t in tasks[: config.MAX_DASHBOARD_TASKS]:
        mark = "💬" if t["needs_comment"] else "✅"
        label = _cut(f"{mark} #{t['id']} {assignee(t)} — {t['description']}",
                     config.MAX_BUTTON_TEXT)
        rows.append([InlineKeyboardButton(label, callback_data=f"done:{t['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


def evening_keyboard(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    """✅ выполнить / ✖️ отменить по каждой задаче."""
    rows = []
    for t in tasks[: config.MAX_DASHBOARD_TASKS]:
        title = _cut(f"#{t['id']} {t['description']}", 28)
        rows.append([
            InlineKeyboardButton(f"✅ {title}", callback_data=f"done:{t['id']}"),
            InlineKeyboardButton("✖️", callback_data=f"cancel:{t['id']}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def morning_keyboard(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    """Перенести / отменить по каждой вчерашней задаче."""
    rows = []
    for t in tasks[: config.MAX_DASHBOARD_TASKS]:
        title = _cut(f"#{t['id']} {t['description']}", 26)
        rows.append([
            InlineKeyboardButton(f"⏭ {title}", callback_data=f"defer:{t['id']}"),
            InlineKeyboardButton("✖️", callback_data=f"cancel:{t['id']}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def task_list_text(title: str, tasks: list[Task]) -> str:
    if not tasks:
        return f"<b>{title}</b>\n\nПусто 🎉"
    late = [t for t in tasks if overdue_days(t)]
    fresh = [t for t in tasks if not overdue_days(t)]
    lines = [f"<b>{title}</b>"]
    if late:
        lines += ["", "🔥 <b>Просрочено</b>"]
        lines += [f"#{t['id']} {task_line(t)}" for t in late]
    if fresh:
        lines += ["", "<b>В работе</b>"] if late else [""]
        lines += [f"#{t['id']} {task_line(t)}" for t in fresh]
    return _limit("\n".join(lines))


HELP_TEXT = """<b>Бот-задачник</b>

Всё делается кнопками внизу экрана:

➕ <b>Новая задача</b> — конструктор: пишешь текст, тапами выбираешь исполнителя,
приоритет, когда ждать клиента и нужен ли комментарий при закрытии.
📋 <b>Задачи</b> — все активные, с кнопкой ✅ у каждой.
🙋 <b>Мои задачи</b> — только твои.
🗂 <b>История</b> — закрытые и отменённые, с комментариями.
⚙️ <b>Настройки</b> — время утреннего и вечернего напоминания.

Наверху чата закреплён список всех активных задач — он обновляется сам.
Закрыть задачу можно оттуда, из личных сообщений или из напоминания.

Если задача помечена 💬, при закрытии бот попросит написать комментарий.

<i>Команды тоже работают, если удобнее печатать: /task, /list, /my, /history,
/settings. Полный синтаксис: /task @user срочно завтра 12:00 !коммент текст</i>"""


# ============================================================ нижнее меню

BTN_NEW = "➕ Новая задача"
BTN_LIST = "📋 Задачи"
BTN_MY = "🙋 Мои задачи"
BTN_HISTORY = "🗂 История"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"

GROUP_MENU = [
    [BTN_NEW],
    [BTN_LIST, BTN_MY],
    [BTN_HISTORY, BTN_SETTINGS],
    [BTN_HELP],
]
PRIVATE_MENU = [
    [BTN_MY],
    [BTN_HELP],
]
ALL_BUTTONS = {BTN_NEW, BTN_LIST, BTN_MY, BTN_HISTORY, BTN_SETTINGS, BTN_HELP}


def menu_keyboard(private: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        PRIVATE_MENU if private else GROUP_MENU,
        resize_keyboard=True,
        is_persistent=True,
    )


# ==================================================== конструктор задачи

DATE_PRESETS = [("сегодня", "сегодня"), ("завтра", "завтра"),
                ("послезавтра", "послезавтра")]


def draft_text(draft) -> str:
    if draft["is_all"]:
        who = "👥 Все"
    elif draft["assignee_name"]:
        who = escape(draft["assignee_name"])
    else:
        who = "<i>не выбран</i>"

    text = escape(draft["description"]) if draft["description"] else "<i>не задан</i>"
    date = escape(draft["client_date"]) if draft["client_date"] else "—"

    lines = [
        "➕ <b>Новая задача</b>",
        "",
        f"1. Текст: {text}",
        f"2. Исполнитель: {who}",
        f"3. Приоритет: {PRIORITY_EMOJI[draft['priority']]} {draft['priority']}",
        f"4. Клиент: {date}",
        f"5. Комментарий при закрытии: "
        f"{'нужен 💬' if draft['needs_comment'] else 'не нужен'}",
    ]
    if draft["awaiting"] == "text":
        lines += ["", "✍️ <b>Напиши текст задачи сообщением</b>"]
    elif draft["awaiting"] == "date":
        lines += ["", "✍️ <b>Напиши, когда ждать клиента</b> (например: после обеда)"]
    return "\n".join(lines)


def draft_keyboard(draft, members) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # 1. текст — первым, как и в описании
    rows.append([InlineKeyboardButton(
        "✍️ 1. Изменить текст" if draft["description"] else "✍️ 1. Написать текст",
        callback_data="d:txt")])

    # 2. исполнители: по двое в ряд
    people = []
    for user in members[:10]:
        chosen = (not draft["is_all"]) and draft["assignee_id"] == user["user_id"]
        label = ("✅ " if chosen else "") + _cut(user["full_name"], 22)
        people.append(InlineKeyboardButton(label, callback_data=f"d:as:{user['user_id']}"))
    for i in range(0, len(people), 2):
        rows.append(people[i:i + 2])
    rows.append([
        InlineKeyboardButton(
            ("✅ " if draft["is_all"] else "") + "👥 Всем", callback_data="d:all"),
        InlineKeyboardButton("🔄 Обновить список", callback_data="d:sync"),
    ])

    # 3. приоритет
    rows.append([
        InlineKeyboardButton(
            ("✅ " if draft["priority"] == name else "") + f"{PRIORITY_EMOJI[name]} {title}",
            callback_data=f"d:pr:{name}")
        for name, title in (("срочно", "Срочно"), ("важно", "Важно"), ("обычно", "Обычно"))
    ])

    # 4. дата клиента
    rows.append([
        InlineKeyboardButton(
            ("✅ " if draft["client_date"] == value else "") + title.capitalize(),
            callback_data=f"d:cd:{value}")
        for value, title in DATE_PRESETS
    ])
    rows.append([
        InlineKeyboardButton("📅 Своя дата", callback_data="d:cd:manual"),
        InlineKeyboardButton("✖️ Без даты", callback_data="d:cd:none"),
    ])

    # 5. комментарий
    rows.append([InlineKeyboardButton(
        "💬 Комментарий нужен ✅" if draft["needs_comment"] else "💬 Комментарий не нужен",
        callback_data="d:nc")])

    rows.append([
        InlineKeyboardButton("✅ Создать", callback_data="d:save"),
        InlineKeyboardButton("✖️ Отмена", callback_data="d:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


# ========================================================= экран настроек

def settings_text(settings) -> str:
    return "\n".join([
        "⚙️ <b>Напоминания</b>",
        "",
        f"☀️ Утро: <b>{settings['morning_time']}</b> "
        f"{'включено' if settings['morning_on'] else '— выключено'}",
        "<i>список задач, оставшихся со вчера</i>",
        "",
        f"🌙 Вечер: <b>{settings['evening_time']}</b> "
        f"{'включено' if settings['evening_on'] else '— выключено'}",
        "<i>все незакрытые задачи за день</i>",
        "",
        f"🔥 Просрочка: <b>{'напоминаю' if settings['nag_on'] else 'напоминания выключены'}</b>",
        f"<i>задача просрочена, если не закрыта за "
        f"{_plural_days(config.OVERDUE_DAYS)} с постановки. "
        f"Напоминаю в личку с {settings['morning_time']} до {settings['nag_until']}: "
        f"срочные — каждые {_human_interval(config.NAG_INTERVALS['срочно'])}, "
        f"важные — каждые {_human_interval(config.NAG_INTERVALS['важно'])}, "
        f"обычные — каждые {_human_interval(config.NAG_INTERVALS['обычно'])}</i>",
        "",
        f"Часовой пояс: {config.TIMEZONE_NAME}",
    ])


def settings_keyboard(settings) -> InlineKeyboardMarkup:
    rows = []
    for key, icon in (("mo", "☀️"), ("ev", "🌙")):
        field = "morning" if key == "mo" else "evening"
        rows.append([InlineKeyboardButton(
            f"{icon} {settings[field + '_time']}", callback_data="s:noop")])
        rows.append([
            InlineKeyboardButton("−1 ч", callback_data=f"s:{key}:-60"),
            InlineKeyboardButton("−15 м", callback_data=f"s:{key}:-15"),
            InlineKeyboardButton("+15 м", callback_data=f"s:{key}:15"),
            InlineKeyboardButton("+1 ч", callback_data=f"s:{key}:60"),
        ])
        rows.append([InlineKeyboardButton(
            "Выключить" if settings[field + "_on"] else "Включить",
            callback_data=f"s:{key}:toggle")])
    rows.append([InlineKeyboardButton(
        f"🔥 Просрочка — напоминать до {settings['nag_until']}", callback_data="s:noop")])
    rows.append([
        InlineKeyboardButton("−1 ч", callback_data="s:ng:-60"),
        InlineKeyboardButton("+1 ч", callback_data="s:ng:60"),
        InlineKeyboardButton("Выключить" if settings["nag_on"] else "Включить",
                             callback_data="s:ng:toggle"),
    ])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="s:close")])
    return InlineKeyboardMarkup(rows)


def history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("7 дней", callback_data="h:7"),
        InlineKeyboardButton("14 дней", callback_data="h:14"),
        InlineKeyboardButton("30 дней", callback_data="h:30"),
    ]])
