"""Разбор команды /task @user [приоритет] [датаКлиента] [!коммент] текст"""
from __future__ import annotations

import re
from dataclasses import dataclass

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


def parse_time(value: str) -> str:
    """'9:45' / '09.45' -> '09:45'. Бросает ParseError при мусоре."""
    m = TIME_SETTING_RE.match(value.strip())
    if not m:
        raise ParseError("Время нужно в формате ЧЧ:ММ, например 19:30.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"
