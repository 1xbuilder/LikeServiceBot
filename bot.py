"""Точка входа. Запуск: python bot.py"""
from __future__ import annotations

import logging
import sys

from telegram import BotCommand, Update
from telegram.error import NetworkError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          ChatMemberHandler, CommandHandler, MessageHandler,
                          filters)

import config
import db
import handlers
import jobs
import menu

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("taskbot")

COMMANDS = [
    BotCommand("menu", "Показать меню"),
    BotCommand("task", "Поставить задачу текстом"),
    BotCommand("list", "Активные задачи"),
    BotCommand("my", "Мои задачи"),
    BotCommand("history", "Закрытые задачи"),
    BotCommand("dashboard", "Пересобрать закреп"),
    BotCommand("settings", "Настройки напоминаний"),
    BotCommand("help", "Справка"),
]


async def on_error(update: object, context) -> None:
    """Ни одна ошибка в обработчике не должна ронять бота на сервере."""
    log.error("Ошибка при обработке апдейта", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)
    jobs.schedule_all(application)
    me = await application.bot.get_me()
    log.info("Бот @%s запущен. Часовой пояс: %s", me.username, config.TIMEZONE_NAME)


def main() -> None:
    if not config.BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN — положи его в .env рядом с bot.py")

    db.init()
    log.info("База: %s", config.DB_PATH)
    if config.DEBUG:
        log.warning("DEBUG=1 — отладочные команды доступны всем участникам группы. "
                    "На проде поставь DEBUG=0 в .env")

    builder = (ApplicationBuilder()
               .token(config.BOT_TOKEN)
               .post_init(post_init)
               .connect_timeout(config.CONNECT_TIMEOUT)
               .read_timeout(config.READ_TIMEOUT))

    if config.PROXY_URL:
        log.info("Использую прокси: %s", config.PROXY_URL)
        builder = builder.proxy(config.PROXY_URL).get_updates_proxy(config.PROXY_URL)

    app = builder.build()

    if app.job_queue is None:
        sys.exit(
            "\nПланировщик недоступен — утренние, вечерние напоминания и просрочка\n"
            "работать не будут. Установи зависимости целиком:\n\n"
            '    pip install "python-telegram-bot[job-queue]"\n'
        )

    # group=-1: учёт участников идёт раньше всего и не мешает остальным хендлерам
    app.add_handler(MessageHandler(filters.ALL, handlers.track_user), group=-1)

    # разные group: иначе первый подходящий обработчик перехватит апдейт целиком
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, menu.on_bot_added), group=-2)
    app.add_handler(ChatMemberHandler(
        menu.on_chat_member, ChatMemberHandler.CHAT_MEMBER), group=-2)

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("menu", menu.cmd_menu))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("task", handlers.cmd_task))
    app.add_handler(CommandHandler("list", handlers.cmd_list))
    app.add_handler(CommandHandler("my", handlers.cmd_my))
    app.add_handler(CommandHandler("history", handlers.cmd_history))
    app.add_handler(CommandHandler("dashboard", handlers.cmd_dashboard))
    app.add_handler(CommandHandler("settings", handlers.cmd_settings))
    app.add_handler(CommandHandler("setevening", handlers.cmd_setevening))
    app.add_handler(CommandHandler("setmorning", handlers.cmd_setmorning))
    app.add_handler(CommandHandler("settoggle", handlers.cmd_settoggle))
    app.add_handler(CommandHandler("skip", handlers.cmd_skip))

    if config.DEBUG:
        app.add_handler(CommandHandler("testevening", handlers.cmd_testevening))
        app.add_handler(CommandHandler("testmorning", handlers.cmd_testmorning))
        app.add_handler(CommandHandler("backdate", handlers.cmd_backdate))
        app.add_handler(CommandHandler("testnag", handlers.cmd_testnag))
        app.add_handler(CommandHandler("known", handlers.cmd_known))
        log.info("DEBUG=1 — включены отладочные команды")

    # порядок важен: узкие шаблоны раньше общего обработчика
    app.add_handler(CallbackQueryHandler(menu.on_draft_callback, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(menu.on_settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(menu.on_history_callback, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(
        menu.on_task_callback, pattern=r"^(task|editc|reopen):"))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))

    app.add_error_handler(on_error)

    try:
        app.run_polling(drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES)
    except NetworkError as exc:
        sys.exit(
            f"\nНе получилось связаться с Telegram: {type(exc).__name__}: {exc}\n\n"
            "Соединение с api.telegram.org не устанавливается. Скорее всего его\n"
            "блокирует провайдер или фаервол. Запусти диагностику:\n\n"
            "    python check.py\n"
        )


if __name__ == "__main__":
    main()
