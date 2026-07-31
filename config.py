"""Конфигурация бота. Всё читается из переменных окружения / файла .env"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Минималистичный загрузчик .env, чтобы не тянуть лишнюю зависимость."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


def _default_db_path() -> str:
    """Каталог, переживающий перезапуск контейнера, если хостинг его даёт.

    На хостингах ботов рядом с кодом писать нельзя: при деплое папка
    пересобирается из репозитория и база пропадает. Постоянный том обычно
    отдают через DATA_DIR или просто монтируют /app/data.
    """
    candidates = []
    if os.getenv("DATA_DIR"):
        candidates.append(Path(os.getenv("DATA_DIR")))
    # хостинг-контейнер: код лежит в /app, постоянный том смонтирован в /app/data
    if BASE_DIR == Path("/app") or Path("/app") in BASE_DIR.parents:
        candidates.append(Path("/app/data"))

    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        return str(folder / "tasks.db")

    # обычный компьютер или сервер: база рядом с кодом
    return str(BASE_DIR / "tasks.db")


DB_PATH = os.getenv("DB_PATH") or _default_db_path()
DB_IS_PERSISTENT = Path(DB_PATH).parent != BASE_DIR
# базу из папки с кодом забираем только когда путь выбрали сами:
# если DB_PATH задан руками, человек знает, куда положил базу
DB_ADOPT_LEGACY = not os.getenv("DB_PATH") and DB_IS_PERSISTENT

# Часовой пояс, в котором считается время утреннего/вечернего напоминания.
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")

COMMON_ZONES = (
    "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
    "Asia/Omsk", "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk",
    "Asia/Yakutsk", "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka",
    "Europe/Minsk", "Europe/Kyiv", "Asia/Almaty", "Asia/Tashkent",
)


def _resolve_timezone(name: str) -> ZoneInfo:
    """Принимает 'Asia/Omsk', а также просто 'Omsk' — и внятно ругается на мусор."""
    name = name.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass

    # человек написал только город — попробуем найти полное имя
    wanted = name.lower().replace(" ", "_")
    try:
        candidates = sorted(z for z in available_timezones()
                            if z.rsplit("/", 1)[-1].lower() == wanted)
    except Exception:
        candidates = []

    if len(candidates) == 1:
        print(f"[config] TIMEZONE={name} — использую {candidates[0]}. "
              f"Лучше указать полное имя в .env")
        return ZoneInfo(candidates[0])

    hint = "\n  ".join(COMMON_ZONES)
    extra = ""
    if candidates:
        extra = ("\n\nПодходит несколько вариантов, выбери нужный:\n  "
                 + "\n  ".join(candidates))
    raise SystemExit(
        f"\nНеизвестный часовой пояс: TIMEZONE={name!r}\n\n"
        f"Нужно полное имя из базы IANA, например Asia/Omsk (а не просто Omsk).\n"
        f"Частые варианты:\n  {hint}{extra}\n\n"
        f"Поправь TIMEZONE в файле .env рядом с bot.py.\n"
    )


TZ = _resolve_timezone(TIMEZONE_NAME)
TIMEZONE_NAME = str(TZ)

DEFAULT_EVENING = os.getenv("DEFAULT_EVENING", "19:30")
DEFAULT_MORNING = os.getenv("DEFAULT_MORNING", "09:45")

# --- просрочка ---
# Через сколько суток незакрытая задача считается просроченной.
# 1 = не закрыл в день постановки → со следующего утра просрочена.
OVERDUE_DAYS = int(os.getenv("OVERDUE_DAYS", "1"))

# Как часто напоминать в личку о просроченной задаче, минуты.
NAG_INTERVALS = {
    "срочно": int(os.getenv("NAG_URGENT", "30")),
    "важно": int(os.getenv("NAG_IMPORTANT", "120")),
    "обычно": int(os.getenv("NAG_NORMAL", "720")),
}

# До какого часа можно напоминать (начало окна = время утреннего напоминания).
DEFAULT_NAG_UNTIL = os.getenv("DEFAULT_NAG_UNTIL", "22:00")

# Как часто планировщик проверяет, кому пора напомнить, секунды.
NAG_CHECK_SECONDS = int(os.getenv("NAG_CHECK_SECONDS", "300"))

# На сколько откладывает кнопка «Отложить», минуты.
SNOOZE_MINUTES = int(os.getenv("SNOOZE_MINUTES", "60"))

# Сколько дней показывает /history без аргумента
DEFAULT_HISTORY_DAYS = 7

# Прокси для доступа к Telegram API, если он заблокирован провайдером.
# Примеры: socks5://user:pass@host:1080  |  http://user:pass@host:8080
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Сетевые таймауты, сек. На нестабильном канале имеет смысл поднять.
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "20"))

# Сколько живёт незавершённое действие, минуты. По истечении бот перестаёт
# считать следующее сообщение человека текстом задачи или комментарием.
DRAFT_TTL_MINUTES = int(os.getenv("DRAFT_TTL_MINUTES", "20"))
COMMENT_TTL_MINUTES = int(os.getenv("COMMENT_TTL_MINUTES", "120"))

# Отладочные команды (/testevening, /testmorning, /backdate, /known).
# На проде поставь DEBUG=0.
DEBUG = os.getenv("DEBUG", "1") == "1"

# Ограничения Telegram
MAX_DASHBOARD_TASKS = 40      # больше кнопок в одном сообщении лучше не вешать
MAX_BUTTON_TEXT = 60
MAX_MESSAGE_CHARS = 3800   # запас к лимиту Telegram в 4096
