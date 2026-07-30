"""Команды, нажатия кнопок и приём комментариев."""
from __future__ import annotations

import logging
import sqlite3
from html import escape

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                      MessageEntity, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

import config
import db
import ui
from parsing import ParseError, parse_task, parse_time

log = logging.getLogger(__name__)


def full_name(user) -> str:
    return user.full_name or (user.username and f"@{user.username}") or str(user.id)


# ============================================================ вспомогательное

async def refresh_dashboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Обновляет закреплённое сообщение со списком активных задач."""
    tasks = db.active_tasks(chat_id)
    text = ui.dashboard_text(tasks)
    markup = ui.dashboard_keyboard(tasks)
    settings = db.get_settings(chat_id)
    message_id = settings["dashboard_message_id"]

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.info("Дашборд пересоздаётся: %s", exc)
        except TelegramError as exc:
            log.warning("Не удалось обновить дашборд: %s", exc)

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
        db.update_settings(chat_id, dashboard_message_id=msg.message_id)
        try:
            await context.bot.pin_chat_message(chat_id, msg.message_id,
                                               disable_notification=True)
        except TelegramError as exc:
            log.info("Не удалось закрепить дашборд (нужны права админа): %s", exc)
    except TelegramError as exc:
        log.warning("Не удалось создать дашборд: %s", exc)


def _strip_task_buttons(markup: InlineKeyboardMarkup | None,
                        task_id: int) -> InlineKeyboardMarkup | None:
    """Убирает из клавиатуры все кнопки, относящиеся к закрытой задаче."""
    if markup is None:
        return None
    suffix = f":{task_id}"
    rows = []
    for row in markup.inline_keyboard:
        kept = [b for b in row
                if not (b.callback_data or "").endswith(suffix)]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(rows) if rows else None


async def _update_task_cards(context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    """Перерисовывает все карточки задачи: в группе и в личках."""
    task = db.get_task(task_id)
    if task is None:
        return
    if task["status"] == "active":
        text, markup = ui.task_card(task), ui.task_keyboard(task)
    else:
        text, markup = ui.closed_card(task), None
    for row in db.task_messages(task_id):
        try:
            await context.bot.edit_message_text(
                chat_id=row["chat_id"], message_id=row["message_id"],
                text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
            )
        except TelegramError:
            pass


async def _notify_privately(context: ContextTypes.DEFAULT_TYPE,
                            task: sqlite3.Row) -> list[str]:
    """Дублирует задачу в личку. Возвращает список тех, до кого не достучались."""
    if task["is_all"]:
        members = db.all_task_members(task["chat_id"])
        recipients = [u for u in members if u["private_chat_id"]]
        unreachable = [u["full_name"] for u in members if not u["private_chat_id"]]
    else:
        user = db.get_user(task["assignee_id"]) if task["assignee_id"] else None
        recipients = [user] if user and user["private_chat_id"] else []
        unreachable = [] if recipients else [task["assignee_name"]]

    for user in recipients:
        try:
            msg = await context.bot.send_message(
                chat_id=user["private_chat_id"], text=ui.task_card(task),
                parse_mode=ParseMode.HTML, reply_markup=ui.task_keyboard(task),
            )
            db.add_task_message(task["id"], msg.chat_id, msg.message_id)
        except (Forbidden, TelegramError):
            unreachable.append(user["full_name"])
    return unreachable


# ==================================================================== команды

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запоминает всех, кто пишет в группу, — иначе @username не с чем сопоставить."""
    msg = update.effective_message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return
    user = msg.from_user
    private_chat = msg.chat_id if msg.chat.type == "private" else None
    db.upsert_user(user.id, user.username, full_name(user), private_chat)
    if msg.chat.type in ("group", "supergroup"):
        db.add_member(msg.chat_id, user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.id, user.username, full_name(user),
                   update.effective_chat.id if update.effective_chat.type == "private" else None)
    private = update.effective_chat.type == "private"
    if private:
        db.clear_pending(update.effective_chat.id, user.id)
        await update.message.reply_text(
            "Готово — теперь я смогу присылать тебе задачи в личку.\n\n"
            "Кнопка «🙋 Мои задачи» внизу покажет твои активные задачи.\n"
            "Новые задачи ставятся в рабочей группе.",
            reply_markup=ui.menu_keyboard(private=True),
        )
    else:
        await update.message.reply_html(
            ui.HELP_TEXT, reply_markup=ui.menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(ui.HELP_TEXT)


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    author = update.effective_user

    if chat.type == "private":
        await msg.reply_text("Задачи ставятся в рабочей группе, а не в личке.")
        return

    raw = (msg.text or msg.caption or "")
    body = raw.partition(" ")[2].strip()
    reply_user = msg.reply_to_message.from_user if msg.reply_to_message else None
    if reply_user and reply_user.is_bot:
        reply_user = None

    if not body and not reply_user:
        await msg.reply_html(ui.HELP_TEXT)
        return

    try:
        parsed = parse_task(body, assignee_from_reply=reply_user is not None)
    except ParseError as exc:
        await msg.reply_html(f"⚠️ {escape(str(exc))}\n\n{ui.HELP_TEXT}")
        return

    # --- определяем исполнителя
    assignee_id, assignee_name, is_all = None, "", parsed.is_all

    if is_all:
        assignee_name = "Все"
    elif parsed.assignee_token is None and reply_user:
        assignee_id, assignee_name = reply_user.id, full_name(reply_user)
        db.upsert_user(reply_user.id, reply_user.username, assignee_name)
    else:
        # текстовое упоминание (у человека нет @username)
        mention = None
        for ent in msg.entities:
            if ent.type == MessageEntity.TEXT_MENTION:
                mention = ent.user
                break
        if mention:
            assignee_id, assignee_name = mention.id, full_name(mention)
            db.upsert_user(mention.id, mention.username, assignee_name)
        else:
            token = parsed.assignee_token or ""
            row = db.find_user_by_username(token) or db.find_user_by_name(token.lstrip("@"))
            if row is None:
                await msg.reply_html(
                    f"⚠️ Не знаю, кто такой {escape(token)}.\n"
                    "Я запоминаю участников по их сообщениям — пусть он напишет "
                    "что-нибудь в группу (или отправит мне /start в личку), и повтори команду. "
                    "Ещё вариант: ответь командой <code>/task ...</code> на его сообщение."
                )
                return
            assignee_id, assignee_name = row["user_id"], row["full_name"]

    task_id = db.add_task(
        chat_id=chat.id, author_id=author.id, author_name=full_name(author),
        assignee_id=assignee_id, assignee_name=assignee_name, is_all=is_all,
        description=parsed.description, priority=parsed.priority,
        client_date=parsed.client_date, needs_comment=parsed.needs_comment,
    )
    task = db.get_task(task_id)

    sent = await msg.reply_html(ui.task_card(task), reply_markup=ui.task_keyboard(task))
    db.add_task_message(task_id, sent.chat_id, sent.message_id)

    unreachable = await _notify_privately(context, task)
    if unreachable:
        names = ", ".join(escape(n) for n in dict.fromkeys(unreachable))
        await msg.reply_html(
            f"ℹ️ В личку не отправил: {names}. "
            "Нужно, чтобы человек хотя бы раз написал боту /start в личные сообщения."
        )

    await refresh_dashboard(context, chat.id)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        await cmd_my(update, context)
        return
    tasks = db.active_tasks(chat.id)
    await update.effective_message.reply_html(
        ui.task_list_text("📋 Активные задачи", tasks),
        reply_markup=ui.dashboard_keyboard(tasks),
    )


async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    db.upsert_user(user.id, user.username, full_name(user),
                   chat.id if chat.type == "private" else None)
    tasks = (db.active_tasks_for_user_all_chats(user.id) if chat.type == "private"
             else db.active_tasks(chat.id, assignee_id=user.id))
    await update.effective_message.reply_html(
        ui.task_list_text("🙋 Мои задачи", tasks),
        reply_markup=ui.dashboard_keyboard(tasks),
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = config.DEFAULT_HISTORY_DAYS
    if context.args:
        try:
            days = max(1, min(365, int(context.args[0])))
        except ValueError:
            await update.effective_message.reply_text("Формат: /history 7")
            return
    import menu
    chat = update.effective_chat
    body, markup = menu._history_view(chat, update.effective_user.id, days,
                                      only_mine=True)
    await update.effective_message.reply_html(body, reply_markup=markup)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        return
    db.update_settings(chat.id, dashboard_message_id=None)
    await refresh_dashboard(context, chat.id)


# --------------------------------------------------------------- настройки

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        await update.effective_message.reply_text(
            "Время напоминаний настраивается в рабочей группе — "
            "оно общее для всех.")
        return
    s = db.get_settings(chat.id)
    await update.effective_message.reply_html(
        "⚙️ <b>Настройки</b>\n"
        f"Утреннее напоминание: <b>{s['morning_time']}</b> "
        f"({'вкл' if s['morning_on'] else 'выкл'})\n"
        f"Вечерний контроль: <b>{s['evening_time']}</b> "
        f"({'вкл' if s['evening_on'] else 'выкл'})\n"
        f"Часовой пояс: {config.TIMEZONE_NAME}"
    )


async def _set_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    field: str, label: str) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        return
    if not context.args:
        await update.effective_message.reply_text(f"Формат: /set{label} 19:30")
        return
    try:
        value = parse_time(context.args[0])
    except ParseError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    db.update_settings(chat.id, **{field: value})
    from jobs import schedule_chat_jobs
    schedule_chat_jobs(context.application, chat.id)
    await update.effective_message.reply_text(f"Ок, новое время: {value} ({config.TIMEZONE_NAME}).")


async def cmd_setevening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_time(update, context, "evening_time", "evening")


async def cmd_setmorning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_time(update, context, "morning_time", "morning")


async def cmd_settoggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Формат: /settoggle evening off  (или morning on)")
        return
    which, state = context.args[0].lower(), context.args[1].lower()
    if which not in {"evening", "morning"} or state not in {"on", "off"}:
        await update.effective_message.reply_text(
            "Формат: /settoggle evening off  (или morning on)")
        return
    db.update_settings(chat.id, **{f"{which}_on": int(state == "on")})
    from jobs import schedule_chat_jobs
    schedule_chat_jobs(context.application, chat.id)
    ru = "вечерний контроль" if which == "evening" else "утреннее напоминание"
    await update.effective_message.reply_text(
        f"{ru.capitalize()}: {'включено' if state == 'on' else 'выключено'}.")


# ======================================================= отладка (DEBUG=1)

async def cmd_testevening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает вечерний контроль немедленно, не дожидаясь расписания."""
    if not config.DEBUG or update.effective_chat.type == "private":
        return
    from jobs import run_evening
    count = await run_evening(context, update.effective_chat.id)
    if count == 0:
        await update.effective_message.reply_text(
            "Активных задач нет — вечерний контроль ничего не рассылает.")


async def cmd_testmorning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает утреннее напоминание немедленно."""
    if not config.DEBUG or update.effective_chat.type == "private":
        return
    from jobs import run_morning
    count = await run_morning(context, update.effective_chat.id)
    if count == 0:
        await update.effective_message.reply_text(
            "Вчерашних задач нет. Сделай задачу «вчерашней»: /backdate <id>")


async def cmd_backdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переносит дату создания задачи на сутки назад — для проверки утреннего напоминания."""
    if not config.DEBUG or update.effective_chat.type == "private":
        return
    if not context.args:
        await update.effective_message.reply_text("Формат: /backdate 3")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Формат: /backdate 3")
        return
    ok = db.backdate_task(task_id)
    await update.effective_message.reply_text(
        f"Задача #{task_id} теперь считается вчерашней. Проверяй: /testmorning"
        if ok else f"Задача #{task_id} не найдена.")


async def cmd_testnag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Рассылает напоминания о просрочке немедленно, игнорируя тихие часы."""
    if not config.DEBUG or update.effective_chat.type == "private":
        return
    from jobs import run_nag
    count = await run_nag(context, force=True)
    if count == 0:
        await update.effective_message.reply_text(
            "Просроченных задач нет (или некому писать в личку). "
            "Сделай задачу вчерашней: /backdate <id>")
    else:
        await update.effective_message.reply_text(f"Отправлено напоминаний: {count}")


async def cmd_known(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает, кого бот уже запомнил и кому может писать в личку."""
    if not config.DEBUG:
        return
    rows = db.known_users()
    if not rows:
        await update.effective_message.reply_text(
            "Пока никого не знаю. Напиши что-нибудь в группу.")
        return
    lines = ["<b>Известные боту участники</b>", ""]
    for u in rows:
        handle = f"@{u['username']}" if u["username"] else "без юзернейма"
        lichka = "личка ✅" if u["private_chat_id"] else "личка ❌ (нужен /start)"
        lines.append(f"• {escape(u['full_name'])} — {escape(handle)}, {lichka}")
    await update.effective_message.reply_html("\n".join(lines))


# ====================================================================== кнопки

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None:
        await query.answer("Сообщение слишком старое, открой список заново")
        return
    user = query.from_user
    db.upsert_user(user.id, user.username, full_name(user),
                   query.message.chat_id if query.message.chat.type == "private" else None)

    try:
        action, raw_id = query.data.split(":", 1)
        task_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.answer("Непонятная кнопка")
        return

    task = db.get_task(task_id)
    if task is None:
        await query.answer("Задача не найдена")
        return

    if task["status"] != "active":
        await query.answer("Задача уже закрыта")
        await _redraw_after_close(context, query, task)
        return

    if action == "defer":
        until = db.snooze_task(task_id, config.SNOOZE_MINUTES)
        await query.answer(f"Перенесено. Напомню после {until}")
        await _remove_button(query, task_id)
        return

    if action == "snooze":
        until = db.snooze_task(task_id, config.SNOOZE_MINUTES)
        await query.answer(f"Отложено, напомню после {until}")
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return

    if action == "cancel":
        db.close_task(task_id, "cancelled", full_name(user))
        db.clear_pending_for_task(task_id)
        await query.answer("Задача отменена")
        await _finish(context, query, task_id)
        return

    if action == "done":
        if task["needs_comment"]:
            await ask_for_comment(context, query, task, user)
            return

        db.close_task(task_id, "done", full_name(user))
        await query.answer("Готово ✅")
        await _finish(context, query, task_id)


async def ask_for_comment(context: ContextTypes.DEFAULT_TYPE, query, task, user,
                          mode: str = "close") -> None:
    """Просит комментарий в личке; если личка недоступна — в текущем чате."""
    row = db.get_user(user.id)
    private_chat = row["private_chat_id"] if row else None

    if mode == "edit":
        body = (f"Задача #{task['id']} — <b>{escape(task['description'])}</b>\n"
                f"Текущий комментарий: {escape(task['comment_text'] or '—')}\n\n"
                "Напиши новый комментарий одним сообщением.\n"
                "<i>Отменить: /skip</i>")
    else:
        body = (f"Задача #{task['id']} — <b>{escape(task['description'])}</b>\n\n"
                "Напиши комментарий одним сообщением, и я её закрою.\n"
                "<i>Отменить: /skip</i>")

    if private_chat:
        try:
            # ForceReply здесь намеренно не ставим: в личке следующее сообщение
            # и так очевидно относится к вопросу, а принудительный ответ
            # перекрывает нижнее меню и залипает в клиентах
            await context.bot.send_message(
                chat_id=private_chat, text=body, parse_mode=ParseMode.HTML)
            db.set_pending(private_chat, user.id, task["id"], mode=mode)
            await query.answer("Написал тебе в личку — комментарий там",
                               show_alert=query.message.chat.type != "private")
            return
        except TelegramError as exc:
            log.info("Личка недоступна (%s), спрашиваю в чате", exc)

    # запасной путь: человек не писал боту в личку
    mention = f'<a href="tg://user?id={user.id}">{escape(full_name(user))}</a>'
    db.set_pending(query.message.chat_id, user.id, task["id"], mode=mode)
    await query.answer("Напиши комментарий сообщением")
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=f"{mention}, {body}",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True,
                                    input_field_placeholder="Комментарий"))
    except TelegramError as exc:
        log.warning("Не смог запросить комментарий: %s", exc)


async def _remove_button(query, task_id: int) -> None:
    try:
        await query.edit_message_reply_markup(
            _strip_task_buttons(query.message.reply_markup, task_id))
    except TelegramError:
        pass


async def _redraw_after_close(context, query, task) -> None:
    settings = db.get_settings(task["chat_id"])
    if query.message.message_id == settings["dashboard_message_id"]:
        await refresh_dashboard(context, task["chat_id"])
    else:
        await _remove_button(query, task["id"])


async def _finish(context: ContextTypes.DEFAULT_TYPE, query, task_id: int) -> None:
    """После закрытия: карточки, дашборд, кнопка, из которой нажали."""
    task = db.get_task(task_id)
    await _update_task_cards(context, task_id)
    await refresh_dashboard(context, task["chat_id"])

    known = {(r["chat_id"], r["message_id"]) for r in db.task_messages(task_id)}
    settings = db.get_settings(task["chat_id"])
    here = (query.message.chat_id, query.message.message_id)
    if here not in known and query.message.message_id != settings["dashboard_message_id"]:
        await _remove_button(query, task_id)


# =========================================================== ввод комментария

async def _restore_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Возвращает нижнее меню: заодно снимает залипший «ответ боту» в клиенте."""
    try:
        await context.bot.send_message(
            chat_id=chat_id, text="Готово 👇",
            reply_markup=ui.menu_keyboard(private=True))
    except TelegramError:
        pass


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменяет начатый ввод. В личке ещё и снимает залипший «ответ боту»."""
    chat = update.effective_chat
    user_id = update.effective_user.id
    had = db.get_pending(chat.id, user_id) is not None
    db.clear_pending(chat.id, user_id)
    db.clear_draft(chat.id, user_id)
    await update.effective_message.reply_text(
        "Ок, отменил ввод." if had else "Нечего отменять — всё чисто.",
        reply_markup=ui.menu_keyboard(private=True)
        if chat.type == "private" else None)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит кнопки нижнего меню, комментарии к задачам и ввод в конструктор."""
    import menu  # локальный импорт: menu зависит от handlers

    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.text:
        return

    # 1) кнопка нижнего меню
    if await menu.on_menu_button(update, context):
        return

    # 2) ожидается комментарий к закрываемой задаче
    pending = db.get_pending(msg.chat_id, user.id)
    if pending is not None:
        if db.is_stale(pending["created_at"], config.COMMENT_TTL_MINUTES):
            # запрос комментария давно забыт — не превращаем случайную реплику в него
            db.clear_pending(msg.chat_id, user.id)
        else:
            await _accept_comment(update, context, pending)
            return

    # 3) ожидается ввод в конструктор задачи
    draft = db.get_draft(msg.chat_id, user.id)
    if draft is not None and draft["awaiting"]:
        if db.is_stale(draft["updated_at"], config.DRAFT_TTL_MINUTES):
            db.clear_draft(msg.chat_id, user.id)
            return
        await menu.fill_draft_from_text(update, context, draft)


async def _accept_comment(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          pending) -> None:
    msg = update.effective_message
    user = update.effective_user

    task = db.get_task(pending["task_id"])
    db.clear_pending(msg.chat_id, user.id)
    if task is None:
        await msg.reply_text("Задача не найдена.")
        return
    if pending["mode"] != "edit" and task["status"] != "active":
        await msg.reply_text("Задача уже закрыта.")
        return

    comment = msg.text.strip()

    private = msg.chat.type == "private"

    if pending["mode"] == "edit":
        db.update_comment(task["id"], comment)
        await _update_task_cards(context, task["id"])
        task = db.get_task(task["id"])
        await msg.reply_html(
            "✏️ Комментарий обновлён.\n\n" + ui.task_detail(task),
            reply_markup=ui.task_detail_keyboard(task))
        if private:
            await _restore_menu(context, msg.chat_id)
        return

    db.close_task(task["id"], "done", full_name(user), comment=comment)
    await _update_task_cards(context, task["id"])
    await refresh_dashboard(context, task["chat_id"])
    await msg.reply_html(
        f"✅ Задача #{task['id']} закрыта.\n💬 {escape(comment)}")
    if private:
        await _restore_menu(context, msg.chat_id)
