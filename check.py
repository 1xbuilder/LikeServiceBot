"""Диагностика подключения к Telegram. Запуск: python check.py

Отвечает на вопрос «почему бот не стартует»: не работает сеть, мешает прокси
или неверный токен.
"""
from __future__ import annotations

import socket
import sys

HOST = "api.telegram.org"
PORT = 443


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def main() -> int:
    try:
        import config
    except SystemExit as exc:
        print(exc)
        return 1

    print("=" * 60)
    print("Диагностика подключения к Telegram")
    print("=" * 60)
    print(f"Python:       {sys.version.split()[0]}")
    print(f"Часовой пояс: {config.TIMEZONE_NAME}")
    print(f"Прокси:       {config.PROXY_URL or 'не задан'}")

    token = config.BOT_TOKEN
    if not token:
        print("\n❌ BOT_TOKEN не задан. Впиши токен в .env рядом с bot.py")
        return 1
    if ":" not in token:
        print("\n❌ BOT_TOKEN выглядит неполным — нужен вид 123456789:AA...")
        return 1
    print(f"Токен:        {token.split(':')[0]}:... (вид корректный)")

    # ---------------------------------------------------------------- DNS
    step(1, f"DNS: {HOST}")
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(HOST, PORT)})
        print(f"    ✅ Адреса: {', '.join(addrs)}")
    except OSError as exc:
        print(f"    ❌ Имя не разрешается: {exc}")
        print("    Похоже на блокировку на уровне DNS или на отсутствие интернета.")
        return 1

    # ---------------------------------------------------------------- TCP
    step(2, f"TCP-соединение: {HOST}:{PORT}")
    tcp_ok = False
    try:
        with socket.create_connection((HOST, PORT), timeout=10):
            print("    ✅ Порт открыт, соединение установилось")
            tcp_ok = True
    except OSError as exc:
        print(f"    ❌ Не подключиться: {exc}")
        if not config.PROXY_URL:
            print("    Именно на этом шаге падает бот. Telegram API недоступен")
            print("    напрямую — нужен прокси или VPN (см. ниже).")

    # ------------------------------------------------------------ getMe
    step(3, "Запрос getMe к Bot API")
    try:
        import httpx
    except ImportError:
        print("    ⚠️ httpx не установлен — пропускаю")
        return 0 if tcp_ok else 1

    kwargs = {"timeout": 15.0}
    if config.PROXY_URL:
        kwargs["proxy"] = config.PROXY_URL
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as exc:
        print(f"    ❌ {type(exc).__name__}: {exc}")
        _advice(config.PROXY_URL)
        return 1

    if resp.status_code == 200:
        me = resp.json().get("result", {})
        print(f"    ✅ Всё в порядке. Бот: @{me.get('username')} ({me.get('first_name')})")
        print("\nМожно запускать: python bot.py")
        return 0

    if resp.status_code == 401:
        print("    ❌ 401 Unauthorized — токен неверный или отозван.")
        print("    Возьми свежий у @BotFather: /mybots → твой бот → API Token")
        return 1

    print(f"    ❌ HTTP {resp.status_code}: {resp.text[:200]}")
    return 1


def _advice(proxy: str) -> None:
    if proxy:
        print("\n    Прокси задан, но соединение не прошло. Проверь, что прокси")
        print("    живой и что для socks5 установлен пакет: pip install httpx[socks]")
        return
    print("""
    Что делать:

    1) Проверь в браузере: https://api.telegram.org
       Если браузер тоже не открывает — Telegram API блокирует провайдер.

    2) Быстрое решение для теста — включить VPN на всём компьютере
       и запустить бота снова.

    3) Аккуратное решение — прокси только для бота. В .env добавь:
          PROXY_URL=socks5://user:pass@host:1080
       или
          PROXY_URL=http://user:pass@host:8080
       Для socks5 нужен пакет: pip install "httpx[socks]"

    4) Правильное решение для работы — арендовать VPS за 3-5 $ в месяц
       и запускать бота там. Ему всё равно нужно работать круглосуточно,
       иначе напоминания в 09:45 и 19:30 не сработают.
    """)


if __name__ == "__main__":
    sys.exit(main())
