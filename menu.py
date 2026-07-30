"""Кнопочный интерфейс: нижнее меню, конструктор задачи, настройки, история."""
from __future__ import annotations

import logging
from html import escape

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
import db
import handlers
import ui
from parsing import PRIORITY_EMOJI

log = logging.getLogger(__name__)


async def show_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                    private: bool = False, text: str | None = None) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=text or "Меню внизу экрана 👇",
        reply_markup=ui.menu_keyboard(private=private),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    private = update.effective_chat.type == "private"
    await update.effective_message.reply_text(
        "Меню внизу экрана 👇", reply_markup=ui.menu_keyboard(private=private))


async def sync_members(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """Подтягивает администраторов группы в список исполнителей.

    Telegram не даёт боту полный состав чата, но список админов — даёт.
    Возвращает число новых людей.
    """
    known = {u["user_id"] for u in db.chat_members(chat_id)}
    added = 0
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except TelegramError as exc:
        log.warning("Не получил админов чата %s: %s", chat_id, exc)
        return 0

    for member in admins:
        user = member.user
        if user.is_bot:
            continue
        db.upsert_user(user.id, user.username, handlers.full_name(user))
        db.add_member(chat_id, user.id)
        if user.id not in known:
            added += 1
    return added


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кто-то вошёл в группу или изменил статус — сразу берём в список."""
    change = update.chat_member
    if change is None:
        return
    member = change.new_chat_member
    user = member.user
    if user.is_bot:
        return
    if member.status in ("member", "administrator", "creator", "restricted"):
        db.upsert_user(user.id, user.username, handlers.full_name(user))
        db.add_member(update.effective_chat.id, user.id)


async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бота добавили в группу — сразу показываем меню и закреп."""
    me = await context.bot.get_me()
    members = update.effective_message.new_chat_members or []
    if not any(u.id == me.id for u in members):
        return
    chat_id = update.effective_chat.id
    from jobs import schedule_chat_jobs
    schedule_chat_jobs(context.application, chat_id)
    await sync_members(context, chat_id)
    await show_menu(
        context, chat_id,
        text=("Привет! Я веду задачи менеджеров.\n\n"
              "Жми <b>➕ Новая задача</b> в меню внизу.\n"
              "Каждому участнику нужно один раз написать мне в личку /start — "
              "иначе я не смогу присылать задачи в личные сообщения."),
    )
    await handlers.refresh_dashboard(context, chat_id)


# ==================================================== конструктор задачи

async def _render_draft(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                        user_id: int, new: bool = False) -> None:
    draft = db.get_draft(chat_id, user_id)
    if draft is None:
        return
    members = db.chat_members(chat_id)
    text = ui.draft_text(draft)
    markup = ui.draft_keyboard(draft, members)

    if new or not draft["message_id"]:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        db.update_draft(chat_id, user_id, message_id=msg.message_id)
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=draft["message_id"], text=text,
            parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            msg = await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                reply_markup=markup)
            db.update_draft(chat_id, user_id, message_id=msg.message_id)


async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.effective_message.reply_text(
            "Задачи ставятся в рабочей группе.")
        return
    db.add_member(chat.id, user.id)
    await sync_members(context, chat.id)
    db.draft_start(chat.id, user.id, awaiting="text")
    await _render_draft(context, chat.id, user.id, new=True)


async def on_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None:
        await query.answer("Сообщение слишком старое, начни заново")
        return
    chat_id = query.message.chat_id
    user = query.from_user

    draft = db.get_draft(chat_id, user.id)
    if draft is not None and draft["message_id"] \
            and draft["message_id"] != query.message.message_id:
        await query.answer("Это старый черновик — работай с последним сообщением")
        return
    if draft is None:
        await query.answer("Этот черновик уже не активен — начни заново")
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "cancel":
        db.clear_draft(chat_id, user.id)
        await query.answer("Отменено")
        try:
            await query.edit_message_text("✖️ Создание задачи отменено.")
        except TelegramError:
            pass
        return

    if action == "as":
        target = int(parts[2])
        row = db.get_user(target)
        db.update_draft(chat_id, user.id, assignee_id=target, is_all=0,
                        assignee_name=row["full_name"] if row else str(target))
        await query.answer()

    elif action == "all":
        db.update_draft(chat_id, user.id, is_all=1, assignee_id=None,
                        assignee_name="Все")
        await query.answer()

    elif action == "pr":
        db.update_draft(chat_id, user.id, priority=parts[2])
        await query.answer()

    elif action == "cd":
        value = parts[2]
        if value == "none":
            db.update_draft(chat_id, user.id, client_date=None, awaiting=None)
            await query.answer()
        elif value == "manual":
            db.update_draft(chat_id, user.id, awaiting="date")
            await query.answer("Напиши дату сообщением")
        else:
            db.update_draft(chat_id, user.id, client_date=value, awaiting=None)
            await query.answer()

    elif action == "nc":
        db.update_draft(chat_id, user.id, needs_comment=0 if draft["needs_comment"] else 1)
        await query.answer()

    elif action == "sync":
        added = await sync_members(context, chat_id)
        if added:
            await query.answer(f"Добавлено: {added}")
        else:
            await query.answer(
                "Новых не нашёл. Бот видит только администраторов группы и тех, "
                "кто уже писал в чат — попроси остальных отправить любое сообщение.",
                show_alert=True)

    elif action == "txt":
        db.update_draft(chat_id, user.id, awaiting="text")
        await query.answer("Напиши текст задачи сообщением")

    elif action == "save":
        await _save_draft(context, query, chat_id, user)
        return

    else:
        await query.answer()

    await _render_draft(context, chat_id, user.id)


async def _save_draft(context: ContextTypes.DEFAULT_TYPE, query,
                      chat_id: int, user) -> None:
    draft = db.get_draft(chat_id, user.id)
    if not draft["description"]:
        await query.answer("Сначала напиши текст задачи", show_alert=True)
        return
    if not draft["is_all"] and not draft["assignee_id"]:
        await query.answer("Выбери исполнителя", show_alert=True)
        return

    task_id = db.add_task(
        chat_id=chat_id,
        author_id=user.id,
        author_name=handlers.full_name(user),
        assignee_id=None if draft["is_all"] else draft["assignee_id"],
        assignee_name="Все" if draft["is_all"] else draft["assignee_name"],
        is_all=bool(draft["is_all"]),
        description=draft["description"],
        priority=draft["priority"],
        client_date=draft["client_date"],
        needs_comment=bool(draft["needs_comment"]),
    )
    db.clear_draft(chat_id, user.id)
    task = db.get_task(task_id)

    await query.answer("Задача создана ✅")
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=query.message.message_id,
            text=ui.task_card(task), parse_mode=ParseMode.HTML,
            reply_markup=ui.task_keyboard(task))
        db.add_task_message(task_id, chat_id, query.message.message_id)
    except TelegramError:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=ui.task_card(task), parse_mode=ParseMode.HTML,
            reply_markup=ui.task_keyboard(task))
        db.add_task_message(task_id, msg.chat_id, msg.message_id)

    unreachable = await handlers._notify_privately(context, task)
    if unreachable:
        names = ", ".join(escape(n) for n in dict.fromkeys(unreachable))
        await context.bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text=(f"ℹ️ В личку не отправил: {names}. "
                  "Нужно, чтобы человек написал боту /start в личные сообщения."))

    await handlers.refresh_dashboard(context, chat_id)


async def fill_draft_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               draft) -> bool:
    """Принимает текст задачи или свою дату. True, если текст был использован."""
    msg = update.effective_message
    user = update.effective_user
    value = msg.text.strip()

    if draft["awaiting"] == "text":
        db.update_draft(msg.chat_id, user.id, description=value, awaiting=None)
    elif draft["awaiting"] == "date":
        db.update_draft(msg.chat_id, user.id, client_date=value, awaiting=None)
    else:
        return False

    try:
        await msg.delete()
    except TelegramError:
        pass
    await _render_draft(context, msg.chat_id, user.id)
    return True


# ============================================================== настройки

def _minutes(value: str) -> int:
    hour, minute = (int(x) for x in value.split(":"))
    return hour * 60 + minute


def _shift(value: str, minutes: int) -> str:
    hour, minute = (int(x) for x in value.split(":"))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        return
    settings = db.get_settings(chat.id)
    await update.effective_message.reply_html(
        ui.settings_text(settings), reply_markup=ui.settings_keyboard(settings))


async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from jobs import schedule_chat_jobs

    query = update.callback_query
    if query.message is None:
        await query.answer()
        return
    chat_id = query.message.chat_id
    parts = query.data.split(":")

    if parts[1] == "close":
        await query.answer()
        try:
            await query.delete_message()
        except TelegramError:
            pass
        return

    if parts[1] == "noop":
        await query.answer()
        return

    settings = db.get_settings(chat_id)

    if parts[1] == "ng":
        if parts[2] == "toggle":
            db.update_settings(chat_id, nag_on=0 if settings["nag_on"] else 1)
            await query.answer("Напоминания о просрочке выключены" if settings["nag_on"]
                               else "Напоминания о просрочке включены")
        else:
            new_time = _shift(settings["nag_until"], int(parts[2]))
            if _minutes(new_time) <= _minutes(settings["morning_time"]):
                await query.answer(
                    "Ниже времени утреннего напоминания опускать нельзя — "
                    "напоминать стало бы некогда", show_alert=True)
                return
            db.update_settings(chat_id, nag_until=new_time)
            await query.answer(f"Напоминаю до {new_time}")
        settings = db.get_settings(chat_id)
        try:
            await query.edit_message_text(
                ui.settings_text(settings), parse_mode=ParseMode.HTML,
                reply_markup=ui.settings_keyboard(settings))
        except TelegramError:
            pass
        return

    field = "morning" if parts[1] == "mo" else "evening"

    if parts[2] == "toggle":
        db.update_settings(chat_id, **{f"{field}_on": 0 if settings[f"{field}_on"] else 1})
        await query.answer("Выключено" if settings[f"{field}_on"] else "Включено")
    else:
        new_time = _shift(settings[f"{field}_time"], int(parts[2]))
        db.update_settings(chat_id, **{f"{field}_time": new_time})
        await query.answer(new_time)

    schedule_chat_jobs(context.application, chat_id)
    settings = db.get_settings(chat_id)
    try:
        await query.edit_message_text(
            ui.settings_text(settings), parse_mode=ParseMode.HTML,
            reply_markup=ui.settings_keyboard(settings))
    except TelegramError:
        pass


# ================================================================ история

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        return
    days = config.DEFAULT_HISTORY_DAYS
    await update.effective_message.reply_html(
        ui.history_text(db.history(chat.id, days), days),
        reply_markup=ui.history_keyboard())


async def on_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None:
        await query.answer()
        return
    days = int(query.data.split(":")[1])
    await query.answer(f"{days} дн.")
    try:
        await query.edit_message_text(
            ui.history_text(db.history(query.message.chat_id, days), days),
            parse_mode=ParseMode.HTML, reply_markup=ui.history_keyboard())
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            log.warning("История: %s", exc)


# ========================================================= роутер меню

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True, если текст был кнопкой меню и его обработали."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    text = (msg.text or "").strip()
    if text not in ui.ALL_BUTTONS:
        return False

    in_group = chat.type in ("group", "supergroup")
    # нажатие кнопки уходит в чат обычным сообщением — убираем, чтобы не мусорить
    if in_group:
        try:
            await msg.delete()
        except TelegramError:
            pass

    async def send(body: str, markup=None) -> None:
        await context.bot.send_message(
            chat_id=chat.id, text=body, parse_mode=ParseMode.HTML,
            reply_markup=markup)

    if text == ui.BTN_NEW:
        if not in_group:
            await send("Задачи ставятся в рабочей группе.")
            return True
        db.add_member(chat.id, user.id)
        await sync_members(context, chat.id)
        db.draft_start(chat.id, user.id, awaiting="text")
        await _render_draft(context, chat.id, user.id, new=True)

    elif text == ui.BTN_LIST:
        tasks = db.active_tasks(chat.id) if in_group else \
            db.active_tasks_for_user_all_chats(user.id)
        await send(ui.task_list_text("📋 Активные задачи", tasks),
                   ui.dashboard_keyboard(tasks))

    elif text == ui.BTN_MY:
        tasks = (db.active_tasks_for_user_all_chats(user.id) if not in_group
                 else db.active_tasks(chat.id, assignee_id=user.id))
        await send(ui.task_list_text("🙋 Мои задачи", tasks),
                   ui.dashboard_keyboard(tasks))

    elif text == ui.BTN_HISTORY:
        if not in_group:
            await send("История доступна в рабочей группе.")
            return True
        days = config.DEFAULT_HISTORY_DAYS
        await send(ui.history_text(db.history(chat.id, days), days),
                   ui.history_keyboard())

    elif text == ui.BTN_SETTINGS:
        if not in_group:
            await send("Настройки меняются в рабочей группе.")
            return True
        settings = db.get_settings(chat.id)
        await send(ui.settings_text(settings), ui.settings_keyboard(settings))

    elif text == ui.BTN_HELP:
        await send(ui.HELP_TEXT)

    return True
