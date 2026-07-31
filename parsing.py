"""Разбор команды /task @user [приоритет] [датаКлиента] [!коммент] текст"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

PRIORITY_ALIASES = {
    "срочно": "срочно", "срочная": "срочно", "срочный": "срочно", "urgent": "срочно",
    "важно": "важно", "важная": "важно", "важный": "важно", "important": "важно",
    "обычно": "обычно", "обычная": "обычно", "обычный": "обычно", "normal": "обычно",
}
PRIORITY_EMOJI = {"срочно": "🔴", "важно": "🟡", "обычно": "🟢"}
DEFAULT_PRIORITY = "обычно"

COMMENT_FLAGS = {"!коммент", "!комм", "!комментарий", "!comment", "!с", "!c"}

DATE_WORDS = {
    "сегодня", "завтра", "послезавтра", "утром", "днем", "днём", "вечером", "ночью",
    "пн", "вт", "ср", "чт", "пт", "сб", "вс",
    "понедельник", "вторник", "среда", "среду", "четверг", "пятница", "пятницу",
    "суббота", "субботу", "воскресенье",
}

# время — только с двоеточием (12:00), чтобы «01.08» однозначно читалось как дата
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"^\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?$")
ALL_TOKENS = {"all", "все", "всем", "@all", "@все"}

MAX_DATE_TOKENS = 3


class ParseError(ValueError):
    pass


@dataclass
class ParsedTask:
    assignee_token: str | None      # '@user' / 'all' / None (если был reply)
    is_all: bool
    priority: str
    client_date: str | None
    needs_comment: bool
    description: str


def _is_date_token(token: str) -> bool:
    low = token.lower().strip(",")
    return bool(low in DATE_WORDS or TIME_RE.match(low) or DATE_RE.match(low))


def parse_task(text: str, assignee_from_reply: bool = False) -> ParsedTask:
    """text — всё, что идёт после самой команды /task."""
    tokens = text.split()
    if not tokens:
        raise ParseError("Пустая команда.")

    assignee_token: str | None = None
    is_all = False

    first = tokens[0].lower()
    if first in ALL_TOKENS:
        assignee_token, is_all = "all", True
        tokens = tokens[1:]
    elif tokens[0].startswith("@"):
        assignee_token = tokens[0]
        tokens = tokens[1:]
    elif not assignee_from_reply:
        raise ParseError(
            "Не указан исполнитель. Начни с @username или all, "
            "либо ответь этой командой на сообщение нужного человека."
        )

    # приоритет
    priority = DEFAULT_PRIORITY
    if tokens and tokens[0].lower().strip(",") in PRIORITY_ALIASES:
        priority = PRIORITY_ALIASES[tokens[0].lower().strip(",")]
        tokens = tokens[1:]

    # дата клиента: либо в кавычках, либо подряд идущие «датоподобные» слова
    client_parts: list[str] = []
    if tokens and tokens[0].startswith('"'):
        rest = " ".join(tokens)
        end = rest.find('"', 1)
        if end == -1:
            raise ParseError("Не закрыта кавычка у даты клиента.")
        client_parts = [rest[1:end].strip()]
        tokens = rest[end + 1:].split()
    else:
        while tokens and len(client_parts) < MAX_DATE_TOKENS:
            token = tokens[0]
            low = token.lower()
            # предлог «в» имеет смысл только перед временем
            if low == "в" and len(tokens) > 1 and TIME_RE.match(tokens[1]):
                client_parts.append(token)
                tokens = tokens[1:]
                continue
            if _is_date_token(token):
                client_parts.append(token)
                tokens = tokens[1:]
                continue
            break

    client_date = " ".join(client_parts).strip() or None
    if client_date == "в":
        client_date = None

    # флаг комментария — по спецификации идёт здесь, но ловим и в конце текста
    needs_comment = False
    if tokens and tokens[0].lower() in COMMENT_FLAGS:
        needs_comment = True
        tokens = tokens[1:]
    else:
        kept = []
        for token in tokens:
            if token.lower() in COMMENT_FLAGS:
                needs_comment = True
            else:
                kept.append(token)
        tokens = kept

    description = " ".join(tokens).strip()
    if not description:
        raise ParseError("Не указан текст задачи.")

    return ParsedTask(
        assignee_token=assignee_token,
        is_all=is_all,
        priority=priority,
        client_date=client_date,
        needs_comment=needs_comment,
        description=description,
    )


TIME_SETTING_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


# ------------------------------------------------------------- сроки задачи
# «в течение 15 минут», «до 18:50», «завтра к 14:00», «через 2 часа»

# во сколько считать «утро», «обед», «вечер», «конец дня»
PART_OF_DAY = {
    "утро": (10, 0), "утром": (10, 0),
    "обед": (13, 0), "обеду": (13, 0), "полдень": (12, 0),
    "день": (15, 0), "днем": (15, 0), "днём": (15, 0),
    "вечер": (18, 0), "вечеру": (18, 0), "вечером": (18, 0),
    "ночь": (23, 0), "ночи": (23, 0),
}
DAY_END = (18, 0)          # «сегодня» / «завтра» без уточнения времени

_UNIT_MINUTES = [
    (("недел",), 7 * 24 * 60),
    (("сутк", "сутки", "день", "дня", "дней", "дн"), 24 * 60),
    (("час", "часа", "часов", "ч"), 60),
    (("минут", "мин", "м"), 1),
]

# «в течение 15 минут», «через 2 часа», «за 30 мин», «на 1 день»
_DURATION_RE = re.compile(
    r"(?:в\s+течени[еи]|через|за|на)?\s*(\d+(?:[.,]\d+)?)\s*"
    r"(недел\w*|сутк\w*|дн\w*|день|час\w*|ч|минут\w*|мин|м)\b",
    re.IGNORECASE)
_HALF_RE = re.compile(r"\b(?:через\s+)?пол\s*(часа|час|дня|суток)\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"\b(?:через\s+)?час\b", re.IGNORECASE)

# «до 18:50», «к 18-50», «в 9:00», просто «18:50»
_CLOCK_RE = re.compile(r"\b(?:до|к|в|ко)?\s*([01]?\d|2[0-3])[:.\-]([0-5]\d)\b",
                       re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")

_DAY_SHIFT = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_WEEKDAYS = {
    "пн": 0, "понедельник": 0, "вт": 1, "вторник": 1, "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3, "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "вс": 6, "воскресенье": 6,
}


def _unit_minutes(word: str) -> int | None:
    low = word.lower()
    for prefixes, minutes in _UNIT_MINUTES:
        if any(low.startswith(p) for p in prefixes):
            return minutes
    return None


def parse_deadline(text: str, now: datetime) -> datetime | None:
    """Превращает «в течение 15 минут» или «до 18:50» в конкретный момент.

    Возвращает None, если срока в тексте нет — тогда бот берёт срок
    по приоритету. Отсчёт длительностей идёт от now.
    """
    if not text:
        return None
    low = " " + text.lower().replace("ё", "е") + " "

    # 1. длительность: «в течение 15 минут», «через 2 часа», «полчаса»
    if _HALF_RE.search(low):
        word = _HALF_RE.search(low).group(1)
        minutes = 30 if word.startswith("час") else 12 * 60
        return now + timedelta(minutes=minutes)

    match = _DURATION_RE.search(low)
    if match:
        unit = _unit_minutes(match.group(2))
        if unit:
            amount = float(match.group(1).replace(",", "."))
            if 0 < amount <= 366 * 24 * 60:
                return now + timedelta(minutes=amount * unit)

    # «через час» без числа
    if re.search(r"\bчерез\s+час\b", low):
        return now + timedelta(hours=1)

    # 2. конкретный момент: день + время
    day = None
    clean = low          # из строки уберём дату, чтобы «01.08» не читалось как 01:08
    for word, shift in _DAY_SHIFT.items():
        if re.search(rf"\b{word}\b", low):
            day = (now + timedelta(days=shift)).date()
            break
    if day is None:
        for word, index in _WEEKDAYS.items():
            if re.search(rf"\b{word}\b", low):
                ahead = (index - now.weekday()) % 7 or 7
                day = (now + timedelta(days=ahead)).date()
                break
    if day is None:
        date_match = _DATE_RE.search(low)
        if date_match:
            try:
                d, m = int(date_match.group(1)), int(date_match.group(2))
                year = int(date_match.group(3) or now.year)
                if year < 100:
                    year += 2000
                day = date(year, m, d)
                clean = low[:date_match.start()] + " " + low[date_match.end():]
            except ValueError:
                day = None

    clock = _CLOCK_RE.search(clean)
    hour = minute = None
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
    elif re.search(r"\bконц[ауе]?\s+дня\b|\bконец\s+дня\b", clean):
        hour, minute = DAY_END
    else:
        for word, (h, m) in PART_OF_DAY.items():
            if re.search(rf"\b{word}\b", clean):
                hour, minute = h, m
                break

    if hour is None and day is None:
        return None
    if hour is None:
        hour, minute = DAY_END
    if day is None:
        day = now.date()

    moment = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)

    # «до 9:00», сказанное вечером, почти наверняка про завтра;
    # «до 18:50», сказанное в 19:00, — про сегодня, человек уже опаздывает
    if moment < now and not any(re.search(rf"\b{w}\b", low) for w in _DAY_SHIFT):
        if now - moment > timedelta(hours=3):
            moment += timedelta(days=1)
    return moment


def parse_time(value: str) -> str:
    """'9:45' / '09.45' -> '09:45'. Бросает ParseError при мусоре."""
    m = TIME_SETTING_RE.match(value.strip())
    if not m:
        raise ParseError("Время нужно в формате ЧЧ:ММ, например 19:30.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"
