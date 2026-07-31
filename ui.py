"""Тексты сообщений и inline-клавиатуры."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from html import escape

import db

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)

import config
from parsing import PRIORITY_EMOJI

Task = sqlite3.Row


def field(task: Task, name: str, default=None):
    """Безопасное чтение колонки: база могла быть создана до её появления."""
    try:
        return task[name]
    except (IndexError, KeyError):
        return default


def emoji(task: Task) -> str:
    return PRIORITY_EMOJI.get(task["priority"], "🟢")


def assignee(task: Task) -> str:
    """Короткая подпись для списков."""
    if not task["is_all"]:
        return task["assignee_name"]
    names = db.member_names(task["chat_id"])
    return f"Все ({len(names)})" if names else "Все"


def assignee_detailed(task: Task) -> str:
    """Подпись для карточки: у задачи «Всем» перечисляем состав.

    Нужно, чтобы было видно, кого задача касается, — включая автора
    и не включая тех, кто помечен «не включать в задачи Всем».
    """
    if not task["is_all"]:
        return task["assignee_name"]
    names = db.member_names(task["chat_id"])
    if not names:
        return "Все"
    return f"Все ({len(names)}): " + ", ".join(names)


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

def files_line(count: int) -> str:
    if not count:
        return ""
    word = "файл" if count % 10 == 1 and count % 100 != 11 else (
        "файла" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14)
        else "файлов")
    return f"📎 Вложения: {count} {word}"


def task_card(task: Task, files_count: int = 0) -> str:
    lines = [
        f"{emoji(task)} <b>Задача #{task['id']}</b>",
        escape(task["description"]),
        "",
        f"Ответственный: <b>{escape(assignee_detailed(task))}</b>",
        f"Приоритет: {escape(task['priority'])}",
    ]
    if task["client_date"]:
        lines.append(f"Клиент: <b>{escape(task['client_date'])}</b>")
    if has_deadline(task) and task["status"] == "active":
        lines.append(deadline_line(task))
    if task["needs_comment"]:
        lines.append("💬 При закрытии нужен комментарий")
    if files_count:
        lines.append(files_line(files_count))
    if task["author_name"]:
        lines.append(f"<i>Поставил: {escape(task['author_name'])}</i>")
    lines += _rework_lines(task)
    return "\n".join(lines)


def _rework_lines(task: Task) -> list[str]:
    """Замечание автора, из-за которого задача вернулась в работу."""
    comment = field(task, "rework_comment")
    if not comment or task["status"] != "active":
        return []
    who = field(task, "rework_by") or "автор"
    return ["", f"🔁 <b>Возвращена на доработку</b> — {escape(who)}",
            f"💬 {escape(comment)}"]


def closed_card(task: Task, files_count: int = 0) -> str:
    mark = "✅ Выполнено" if task["status"] == "done" else "✖️ Отменена"
    lines = [
        f"{mark} — <b>задача #{task['id']}</b>",
        f"<s>{escape(task['description'])}</s>",
        "",
        f"Ответственный: {escape(assignee_detailed(task))}",
    ]
    if task["completed_by"]:
        lines.append(f"Закрыл: {escape(task['completed_by'])}")
    if task["comment_text"]:
        lines.append(f"💬 {escape(task['comment_text'])}")
    if files_count:
        lines.append(files_line(files_count))
    count = field(task, "rework_count") or 0
    if count:
        lines.append(f"<i>🔁 Была на доработке: {count}</i>")
    return "\n".join(lines)


def overdue_delta(task) -> timedelta:
    """На сколько задача просрочена. Ноль — срок ещё не вышел."""
    if task["status"] != "active":
        return timedelta(0)
    try:
        late = db.overdue_by(task)
    except ValueError:
        return timedelta(0)
    return late if late > timedelta(0) else timedelta(0)


def is_overdue(task) -> bool:
    return overdue_delta(task) > timedelta(0)


def has_deadline(task) -> bool:
    """Срок распознан из текста, а не выведен из приоритета."""
    return bool(field(task, "deadline_at"))


def deadline_label(task) -> str:
    """«сегодня до 18:50», «завтра до 14:00», «03.08 до 10:00»."""
    moment = db.deadline(task)
    days = (moment.date() - datetime.now(config.TZ).date()).days
    when = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(days)
    if when is None:
        when = moment.strftime("%d.%m")
    return f"{when} до {moment.strftime('%H:%M')}"


def deadline_line(task) -> str:
    """Строка со сроком и остатком времени для карточки задачи."""
    late = overdue_delta(task)
    if late:
        return f"🔥 <b>Срок вышел</b> ({deadline_label(task)}), просрочено на {human_late(late)}"
    left = db.deadline(task) - datetime.now(config.TZ)
    return f"⏳ Срок: <b>{deadline_label(task)}</b> — осталось {human_late(left)}"


def human_late(delta: timedelta) -> str:
    """«2 ч 15 мин», «3 ч», «1 день 4 ч» — коротко и без хвостов."""
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(_plural_days(days))
    if hours:
        parts.append(f"{hours} ч")
    if mins and not days:
        parts.append(f"{mins} мин")
    return " ".join(parts) or "меньше минуты"


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def task_line(task) -> str:
    """Строка для дашборда / списков."""
    late = overdue_delta(task)
    parts = []
    if late:
        parts.append("🔥 ")
    parts.append(f"{emoji(task)} <b>{escape(assignee(task))}</b> — {escape(task['description'])}")
    if late:
        parts.append(f" <b>[просрочено на {human_late(late)}]</b>")
    if has_deadline(task) and not late:
        parts.append(f" <i>({deadline_label(task)})</i>")
    elif task["client_date"]:
        parts.append(f" <i>(клиент: {escape(task['client_date'])})</i>")
    if task["needs_comment"]:
        parts.append(" 💬")
    return "".join(parts)


def nag_card(task) -> str:
    late = overdue_delta(task)
    interval = db.nag_interval(task)
    first = not field(task, "last_nag_at")
    head = ("⏰ <b>Срок вышел — задачу нужно закрыть</b>" if first
            else f"🔥 <b>Просрочено на {human_late(late)}</b>")
    lines = [
        head,
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


def history_text(tasks: list[Task], days: int, only_mine: bool | None = None) -> str:
    scope = ""
    if only_mine is not None:
        scope = " · только мои" if only_mine else " · вся группа"
    if not tasks:
        return f"🗂 <b>История за {days} дн.{scope}</b>\n\nЗакрытых задач нет."
    lines = [f"🗂 <b>История за {days} дн.{scope}</b>", ""]
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


def _rework_button(task: Task) -> InlineKeyboardButton:
    return InlineKeyboardButton("🔁 На доработку",
                                callback_data=f"rework:{task['id']}")


def closed_keyboard(task: Task) -> InlineKeyboardMarkup | None:
    """У выполненной задачи автор может вернуть её в работу с замечанием."""
    if task["status"] != "done":
        return None
    return InlineKeyboardMarkup([[_rework_button(task)]])


def review_request(task: Task) -> str:
    """Автору задачи: исполнитель отчитался, проверь."""
    lines = [
        f"🔎 <b>Проверь выполнение — задача #{task['id']}</b>",
        escape(task["description"]),
        "",
        f"Исполнитель: <b>{escape(assignee_detailed(task))}</b>",
        f"Закрыл: {escape(task['completed_by'] or '—')}",
    ]
    if task["comment_text"]:
        lines.append(f"💬 {escape(task['comment_text'])}")
    lines += ["", "<i>Если сделано некорректно — верни на доработку "
                  "с замечанием, исполнитель получит его в личку.</i>"]
    return "\n".join(lines)


def review_keyboard(task: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Принять", callback_data=f"accept:{task['id']}")],
        [_rework_button(task)],
    ])


def review_accepted(task: Task) -> str:
    return (f"👍 <b>Принято — задача #{task['id']}</b>\n"
            f"<s>{escape(task['description'])}</s>")


def rework_notice(task: Task) -> str:
    """Исполнителю и в группу: задача вернулась в работу."""
    lines = [
        f"🔁 <b>Задача #{task['id']} — на доработку</b>",
        escape(task["description"]),
        "",
        f"Ответственный: <b>{escape(assignee_detailed(task))}</b>",
        f"Вернул: {escape(field(task, 'rework_by') or '—')}",
        f"💬 {escape(field(task, 'rework_comment') or '—')}",
    ]
    if task["needs_comment"]:
        lines.append("")
        lines.append("<i>При повторном закрытии снова нужен комментарий.</i>")
    return "\n".join(lines)


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
    late = [t for t in tasks if is_overdue(t)]
    fresh = [t for t in tasks if not is_overdue(t)]
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
приоритет, срок и нужен ли комментарий при закрытии.
К задаче можно приложить фото или файл: пока открыт конструктор, просто
пришли их сообщением. Подпись к фото станет текстом задачи, если он ещё не задан.

📋 <b>Задачи</b> — все активные, с кнопкой ✅ у каждой.
🙋 <b>Мои задачи</b> — только твои.
🗂 <b>История</b> — закрытые и отменённые, с комментариями.
⚙️ <b>Настройки</b> — время утреннего и вечернего напоминания.

<b>В личке со мной</b> — личный кабинет: свои задачи, история, правки.
Чтобы не грузить общий чат, комментарии при закрытии я спрашиваю там же.
В истории можно открыть любую закрытую задачу и переписать комментарий
или вернуть её в работу — общий чат об этих правках не узнает,
карточка просто обновится на месте.

Наверху чата закреплён список всех активных задач — он обновляется сам.
Закрыть задачу можно оттуда, из личных сообщений или из напоминания.

Срок понимаю словами: «до 18:50», «в течение 15 минут», «завтра к 14:00»,
«через 2 часа». Когда время выйдет, напишу исполнителю в личку один раз,
дальше буду повторять тем чаще, чем короче был срок. Если срок не указан,
беру запас по приоритету: срочная — час, важная — 4 часа, обычная — 8 часов.

Если задача помечена 💬, при закрытии бот попросит написать комментарий.
Автору такой задачи я пришлю отчёт исполнителя на проверку: <b>👍 Принять</b>
или <b>🔁 На доработку</b> — во втором случае спрошу замечание и верну задачу
в работу, а исполнитель получит замечание в личку. Кнопка «🔁 На доработку»
есть и на карточке любой выполненной задачи, нажать её может только автор.

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
    [BTN_LIST, BTN_HISTORY],
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


def draft_text(draft, files: int = 0) -> str:
    if draft["is_all"]:
        names = db.member_names(draft["chat_id"])
        who = ("👥 Все — " + ", ".join(escape(n) for n in names)) if names else "👥 Все"
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
    if files:
        lines.append(f"6. {files_line(files)}")
    else:
        lines.append("6. Вложения: — <i>пришли фото или файл сообщением</i>")
    if draft["awaiting"] == "text":
        lines += ["", "✍️ <b>Напиши текст задачи сообщением</b>"]
    elif draft["awaiting"] == "date":
        lines += ["", "✍️ <b>Напиши срок</b> — «до 18:50», «в течение 15 минут», "
                      "«завтра к 14:00». Я пойму и напомню, когда время выйдет."]
    return "\n".join(lines)


def draft_keyboard(draft, members, files: int = 0) -> InlineKeyboardMarkup:
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
    count = len(db.member_names(draft["chat_id"]))
    rows.append([InlineKeyboardButton(
        ("✅ " if draft["is_all"] else "") + f"👥 Всем ({count})",
        callback_data="d:all")])
    rows.append([InlineKeyboardButton("🔄 Обновить список участников",
                                      callback_data="d:sync")])

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

    if files:
        rows.append([InlineKeyboardButton(
            f"🗑 Убрать вложения ({files})", callback_data="d:nofiles")])
    rows.append([
        InlineKeyboardButton("✅ Создать", callback_data="d:save"),
        InlineKeyboardButton("✖️ Отмена", callback_data="d:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


# ========================================================= экран настроек

def settings_text(settings, chat_id: int | None = None) -> str:
    extra = []
    if chat_id is not None:
        names = db.member_names(chat_id)
        extra = [
            "",
            f"👥 Задачи «Всем» касаются {len(names)} чел.: "
            f"{escape(', '.join(names)) if names else '—'}",
            "<i>кнопками ниже можно исключить человека: 🚫 — не попадает "
            "в задачи «Всем», только в личные</i>",
        ]
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
        f"<i>срок беру из задачи: «до 18:50», «в течение 15 минут», «завтра к 14:00». "
        f"Если срок не указан — по приоритету: срочная "
        f"{_human_interval(int(config.OVERDUE_HOURS['срочно'] * 60))}, важная "
        f"{_human_interval(int(config.OVERDUE_HOURS['важно'] * 60))}, обычная "
        f"{_human_interval(int(config.OVERDUE_HOURS['обычно'] * 60))}. "
        f"Когда срок вышел — пишу исполнителю в личку один раз, дальше повторяю "
        f"каждые {int(config.NAG_PERCENT)}% от отведённого времени "
        f"(не чаще {_human_interval(config.NAG_MIN_MINUTES)} и не реже "
        f"{_human_interval(config.NAG_MAX_MINUTES)}). "
        f"Тихие часы: пишу только с {settings['morning_time']} до {settings['nag_until']}</i>",
        *extra,
        "",
        f"Часовой пояс: {config.TIMEZONE_NAME}",
    ])


def settings_keyboard(settings, members=None) -> InlineKeyboardMarkup:
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
    if members:
        rows.append([InlineKeyboardButton(
            "👥 Кого не включать в задачи «Всем»", callback_data="s:noop")])
        for user in members[:10]:
            mark = "🚫" if user["exclude_from_all"] else "✅"
            rows.append([InlineKeyboardButton(
                _cut(f"{mark} {user['full_name']}", config.MAX_BUTTON_TEXT),
                callback_data=f"s:ex:{user['user_id']}")])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="s:close")])
    return InlineKeyboardMarkup(rows)


def history_keyboard(days: int = 7, only_mine: bool = True,
                     tasks: list[Task] | None = None,
                     private: bool = False) -> InlineKeyboardMarkup:
    scope = "my" if only_mine else "all"
    rows = [[
        InlineKeyboardButton(("✅ " if days == d else "") + f"{d} дн.",
                             callback_data=f"h:{d}:{scope}")
        for d in (7, 14, 30)
    ]]
    if private:
        rows.append([InlineKeyboardButton(
            "👥 Показать всю группу" if only_mine else "🙋 Показать только мои",
            callback_data=f"h:{days}:{'all' if only_mine else 'my'}")])
        for task in (tasks or [])[:8]:
            mark = "✅" if task["status"] == "done" else "✖️"
            rows.append([InlineKeyboardButton(
                _cut(f"{mark} #{task['id']} {task['description']}", config.MAX_BUTTON_TEXT),
                callback_data=f"task:{task['id']}:{days}:{scope}")])
    return InlineKeyboardMarkup(rows)


def task_detail(task: Task, files_count: int = 0) -> str:
    """Подробная карточка для личного кабинета."""
    if task["status"] == "done":
        head = "✅ <b>Выполнена</b>"
    elif task["status"] == "cancelled":
        head = "✖️ <b>Отменена</b>"
    else:
        head = "🔵 <b>В работе</b>"

    lines = [
        f"{head} — задача #{task['id']}",
        "",
        f"{emoji(task)} {escape(task['description'])}",
        "",
        f"Ответственный: {escape(assignee_detailed(task))}",
        f"Приоритет: {escape(task['priority'])}",
    ]
    if task["client_date"]:
        lines.append(f"Клиент: {escape(task['client_date'])}")
    if task["author_name"]:
        lines.append(f"Поставил: {escape(task['author_name'])}")
    if task["completed_at"]:
        when = task["completed_at"][:16].replace("T", " ")
        lines.append(f"Закрыл: {escape(task['completed_by'] or '—')}, {when}")
    if files_count:
        lines.append(files_line(files_count))
    if task["comment_text"]:
        lines.append("")
        lines.append(f"💬 {escape(task['comment_text'])}")
    elif task["needs_comment"]:
        lines.append("")
        lines.append("💬 <i>комментарий не заполнен</i>")
    count = field(task, "rework_count") or 0
    if count:
        lines.append("")
        lines.append(f"🔁 <i>Возвращалась на доработку: {count}</i>")
        if task["status"] == "active" and field(task, "rework_comment"):
            lines.append(f"💬 {escape(task['rework_comment'])} "
                         f"— {escape(field(task, 'rework_by') or '')}")
    return "\n".join(lines)


def task_detail_keyboard(task: Task, days: int = 7, only_mine: bool = True,
                         files_count: int = 0) -> InlineKeyboardMarkup:
    scope = "my" if only_mine else "all"
    rows = []
    if files_count:
        rows.append([InlineKeyboardButton(
            f"📎 Показать вложения ({files_count})",
            callback_data=f"files:{task['id']}:{days}:{scope}")])
    if task["status"] == "active":
        rows.append([_done_button(task)])
    else:
        rows.append([InlineKeyboardButton(
            "✏️ Переписать комментарий" if task["comment_text"]
            else "✏️ Добавить комментарий",
            callback_data=f"editc:{task['id']}:{days}:{scope}")])
        if task["status"] == "done":
            rows.append([InlineKeyboardButton(
                "🔁 На доработку с замечанием",
                callback_data=f"rework:{task['id']}:{days}:{scope}")])
        rows.append([InlineKeyboardButton(
            "↩️ Вернуть в работу", callback_data=f"reopen:{task['id']}:{days}:{scope}")])
    rows.append([InlineKeyboardButton(
        "⬅️ К истории", callback_data=f"h:{days}:{scope}")])
    return InlineKeyboardMarkup(rows)
