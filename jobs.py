"""Ежедневные напоминания: вечерний контроль и утренний разбор вчерашних задач."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from datetime import time as dt_time

from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

import config
import db
import ui

log = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = value.split(":")
    return dt_time(hour=int(hour), minute=int(minute), tzinfo=config.TZ)


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                     header: str, tasks: list, keyboard_fn) -> None:
    """Шлёт список в группу и персональные выжимки в лички."""
    if not tasks:
        return

    text = ui.task_list_text(header, tasks)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text,
                                       parse_mode=ParseMode.HTML,
                                       reply_markup=keyboard_fn(tasks))
    except TelegramError as exc:
        log.warning("Не отправил напоминание в группу %s: %s", chat_id, exc)

    for user in db.chat_members(chat_id, fallback=False):
        if not user["private_chat_id"]:
            continue
        mine = [t for t in tasks
                if t["is_all"] or t["assignee_id"] == user["user_id"]]
        if not mine:
            continue
        try:
            await context.bot.send_message(
                chat_id=user["private_chat_id"],
                text=ui.task_list_text(header, mine),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_fn(mine),
            )
        except TelegramError:
            pass


async def run_evening(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """Вечерний контроль. Возвращает число разосланных задач."""
    tasks = db.active_tasks(chat_id)
    await _broadcast(
        context, chat_id,
        "🌙 Вечерний контроль — что осталось незакрытым",
        tasks, ui.evening_keyboard,
    )
    return len(tasks)


async def run_morning(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """Утреннее напоминание о вчерашних задачах."""
    tasks = db.stale_tasks(chat_id)
    await _broadcast(
        context, chat_id,
        "☀️ Задачи, оставшиеся со вчера — перенести или отменить?",
        tasks, ui.morning_keyboard,
    )
    return len(tasks)


async def evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_evening(context, context.job.chat_id)


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_morning(context, context.job.chat_id)


async def _nag_recipients(chat_id: int, task) -> list:
    if task["is_all"]:
        return [u for u in db.chat_members(chat_id, fallback=False)
                if u["private_chat_id"]]
    user = db.get_user(task["assignee_id"]) if task["assignee_id"] else None
    return [user] if user and user["private_chat_id"] else []


async def _send_nag(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task) -> bool:
    """Отправляет напоминание о просрочке в лички. True, если хоть кому-то дошло."""
    sent = False
    for user in await _nag_recipients(chat_id, task):
        try:
            await context.bot.send_message(
                chat_id=user["private_chat_id"], text=ui.nag_card(task),
                parse_mode=ParseMode.HTML, reply_markup=ui.nag_keyboard(task),
            )
            sent = True
        except TelegramError:
            pass
    return sent


def _in_window(now: datetime, start: str, end: str) -> bool:
    def minutes(value: str) -> int:
        hour, minute = (int(x) for x in value.split(":"))
        return hour * 60 + minute

    current = now.hour * 60 + now.minute
    begin, finish = minutes(start), minutes(end)
    if finish <= begin:
        # границу увели ниже утреннего времени — считаем «до конца суток»,
        # иначе напоминания молча перестали бы приходить
        return current >= begin
    return begin <= current <= finish


async def run_nag(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> int:
    """Проверяет все чаты и напоминает о просроченных задачах. Возвращает число писем."""
    now = datetime.now(config.TZ)
    total = 0

    for row in db.all_chats():
        chat_id = row["chat_id"]
        if not force and not row["nag_on"]:
            continue
        # тихие часы: напоминаем только внутри рабочего окна
        if not force and not _in_window(now, row["morning_time"], row["nag_until"]):
            continue

        for task in db.overdue_tasks(chat_id):
            if task["snooze_until"] and not force:
                if datetime.fromisoformat(task["snooze_until"]) > now:
                    continue
            interval = config.NAG_INTERVALS.get(task["priority"], 720)
            if task["last_nag_at"] and not force:
                due = datetime.fromisoformat(task["last_nag_at"]) + timedelta(minutes=interval)
                if due > now:
                    continue
            if await _send_nag(context, chat_id, task):
                db.mark_nagged(task["id"])
                total += 1

    if total:
        log.info("Напоминаний о просрочке отправлено: %s", total)
    return total


async def nag_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    reconcile_jobs(context.application)
    await run_nag(context)


# чаты, для которых расписание уже создано в этом процессе
_scheduled: set[int] = set()


def reconcile_jobs(application: Application) -> None:
    """Досоздаёт расписание для чатов, появившихся уже после запуска бота.

    На свежем сервере база пустая: чат попадает в неё только когда им начнут
    пользоваться, а schedule_all к тому моменту уже отработал. Без этой сверки
    утренние и вечерние напоминания в таком чате не включились бы до перезапуска.
    """
    for row in db.all_chats():
        chat_id = row["chat_id"]
        if chat_id not in _scheduled:
            schedule_chat_jobs(application, chat_id)


def schedule_chat_jobs(application: Application, chat_id: int) -> None:
    """Пересоздаёт задания планировщика для одного чата."""
    queue = application.job_queue
    settings = db.get_settings(chat_id)
    _scheduled.add(chat_id)

    for kind, callback, enabled, value in (
        ("evening", evening_job, settings["evening_on"], settings["evening_time"]),
        ("morning", morning_job, settings["morning_on"], settings["morning_time"]),
    ):
        name = f"{kind}:{chat_id}"
        for job in queue.get_jobs_by_name(name):
            job.schedule_removal()
        if not enabled:
            continue
        queue.run_daily(callback, time=_parse_hhmm(value), name=name, chat_id=chat_id)
        log.info("Запланировано %s для чата %s на %s (%s)",
                 kind, chat_id, value, config.TIMEZONE_NAME)


def schedule_all(application: Application) -> None:
    for row in db.all_chats():
        schedule_chat_jobs(application, row["chat_id"])

    # единый цикл проверки просрочки на все чаты
    queue = application.job_queue
    for job in queue.get_jobs_by_name("nag"):
        job.schedule_removal()
    queue.run_repeating(nag_job, interval=config.NAG_CHECK_SECONDS,
                        first=60, name="nag")
    log.info("Проверка просрочки — каждые %s сек", config.NAG_CHECK_SECONDS)
