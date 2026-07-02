#!/usr/bin/env python3
"""
Твой Telegram-бот для перепродажи на @mrkt (Этап 1: сканер эджа с управлением).

Что делает:
  - в фоне обходит рынок и присылает тебе выгодные лоты (флипы) в личку;
  - управляется командами прямо в Telegram — токен, коллекции, порог маржи;
  - настройки хранит в config.json (переживают перезапуск).

Запуск локально:  BOT_TOKEN=... ADMIN_ID=... MRKT_TOKEN=... python bot.py
На bothost.ru:    те же значения задать как переменные окружения (Environment).

Токен MRKT протухает ~раз в сутки — просто пришли боту новый: /settoken <токен>.
"""

from __future__ import annotations
import os
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from engine import Config, ScanEngine, TokenExpired, format_deal
from mrkt_client import MrktError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mrkt-bot")

CONFIG_PATH = os.getenv("MRKT_CONFIG", "config.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))   # твой Telegram user_id (у @userinfobot)

cfg = Config.load(CONFIG_PATH)
if not cfg.token:                              # первичный токен можно дать через env
    cfg.token = os.getenv("MRKT_TOKEN", "")
if not cfg.proxy:
    cfg.proxy = os.getenv("MRKT_PROXY") or None
cfg.save(CONFIG_PATH)

engine = ScanEngine(cfg, log_path=os.getenv("MRKT_LOG", "deals.jsonl"))
dp = Dispatcher()


def allowed(msg: Message) -> bool:
    return ADMIN_ID == 0 or (msg.from_user and msg.from_user.id == ADMIN_ID)


async def guard(msg: Message) -> bool:
    if not allowed(msg):
        await msg.answer("Этот бот приватный.")
        return False
    return True


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not await guard(msg):
        return
    warn = "" if ADMIN_ID else ("\n⚠️ ADMIN_ID не задан — бот слушает всех. "
                                "Поставь свой user_id в переменную ADMIN_ID.")
    await msg.answer(
        "Бот-сканер @mrkt. Ищу лоты ниже floor и присылаю их сюда.\n\n"
        "Команды:\n"
        "/status — состояние и статистика\n"
        "/deals — последние найденные флипы\n"
        "/settoken <токен> — обновить токен MRKT (из DevTools)\n"
        "/watch <коллекция> — добавить коллекцию\n"
        "/unwatch <коллекция> — убрать коллекцию\n"
        "/margin <проценты> — порог скидки к floor (напр. 12)\n"
        "/pause и /resume — остановить/запустить сканирование" + warn)


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not await guard(msg):
        return
    c = engine.cfg
    await msg.answer(
        f"{'⏸ на паузе' if c.paused else '▶️ работает'}\n"
        f"токен: {'есть' if c.token else 'НЕТ — пришли /settoken'}\n"
        f"прокси: {c.proxy or 'нет'}\n"
        f"коллекции: {', '.join(c.collections) or '—'}\n"
        f"порог: {c.margin:.0%}, floor = {c.floor_rank}-й лот, пауза {c.poll:.0f}с\n"
        f"обходов: {engine.scans}, найдено флипов: {engine.total_found}\n"
        f"последний скан: {engine.last_scan or '—'}\n"
        f"последняя ошибка: {engine.last_error or '—'}")


@dp.message(Command("deals"))
async def cmd_deals(msg: Message):
    if not await guard(msg):
        return
    try:
        with open(engine.log_path, encoding="utf-8") as f:
            lines = f.readlines()[-10:]
    except FileNotFoundError:
        lines = []
    if not lines:
        await msg.answer("Пока флипов не найдено.")
        return
    import json
    out = []
    for ln in lines:
        r = json.loads(ln)
        out.append(f"{r['ts'][11:16]} {r['collection']} #{r['number']}: "
                   f"{r['buy']}→{r['floor']} (-{r['margin_pct']}%, +{r['net_profit_ton']} TON)")
    await msg.answer("Последние флипы:\n" + "\n".join(out))


@dp.message(Command("settoken"))
async def cmd_settoken(msg: Message, command: CommandObject):
    if not await guard(msg):
        return
    if not command.args:
        await msg.answer("Формат: /settoken <токен из DevTools>")
        return
    engine.cfg.token = command.args.strip()
    engine.cfg.save(CONFIG_PATH)
    engine.last_error = None
    await msg.answer("Токен обновлён ✅")


@dp.message(Command("watch"))
async def cmd_watch(msg: Message, command: CommandObject):
    if not await guard(msg):
        return
    name = (command.args or "").strip()
    if not name:
        await msg.answer("Формат: /watch <название коллекции> (регистр важен)")
        return
    if name not in engine.cfg.collections:
        engine.cfg.collections.append(name)
        engine.cfg.save(CONFIG_PATH)
    await msg.answer(f"Слежу: {', '.join(engine.cfg.collections)}")


@dp.message(Command("unwatch"))
async def cmd_unwatch(msg: Message, command: CommandObject):
    if not await guard(msg):
        return
    name = (command.args or "").strip()
    if name in engine.cfg.collections:
        engine.cfg.collections.remove(name)
        engine.cfg.save(CONFIG_PATH)
    await msg.answer(f"Слежу: {', '.join(engine.cfg.collections) or '—'}")


@dp.message(Command("margin"))
async def cmd_margin(msg: Message, command: CommandObject):
    if not await guard(msg):
        return
    try:
        pct = float((command.args or "").replace("%", "").replace(",", "."))
        engine.cfg.margin = pct / 100
        engine.cfg.save(CONFIG_PATH)
        await msg.answer(f"Порог скидки к floor: {engine.cfg.margin:.0%}")
    except ValueError:
        await msg.answer("Формат: /margin 12  (это 12%)")


@dp.message(Command("pause"))
async def cmd_pause(msg: Message):
    if not await guard(msg):
        return
    engine.cfg.paused = True
    engine.cfg.save(CONFIG_PATH)
    await msg.answer("⏸ Сканирование на паузе.")


@dp.message(Command("resume"))
async def cmd_resume(msg: Message):
    if not await guard(msg):
        return
    engine.cfg.paused = False
    engine.cfg.save(CONFIG_PATH)
    await msg.answer("▶️ Сканирование возобновлено.")


async def scan_loop(bot: Bot):
    """Фоновый цикл: не блокирует бота (сеть — в отдельном потоке)."""
    warned_token = False
    while True:
        if engine.cfg.paused or not engine.cfg.token:
            await asyncio.sleep(3)
            continue
        try:
            deals = await asyncio.to_thread(engine.scan_once)
            warned_token = False
            if ADMIN_ID:
                for d in deals:
                    await bot.send_message(ADMIN_ID, format_deal(d))
        except TokenExpired:
            if ADMIN_ID and not warned_token:
                await bot.send_message(
                    ADMIN_ID, "🔑 Токен MRKT протух. Пришли свежий: /settoken <токен>")
                warned_token = True
        except MrktError as e:
            engine.last_error = str(e)
            log.warning("scan error: %s", e)
        except Exception as e:  # noqa: BLE001 — цикл не должен падать
            engine.last_error = f"{type(e).__name__}: {e}"
            log.exception("unexpected scan error")
        await asyncio.sleep(engine.cfg.poll)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN. Создай бота у @BotFather и задай BOT_TOKEN.")
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(scan_loop(bot))
    log.info("Бот запущен. ADMIN_ID=%s, коллекций=%d", ADMIN_ID, len(cfg.collections))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
