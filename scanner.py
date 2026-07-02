#!/usr/bin/env python3
"""
CLI-сканер (без Telegram-бота) — для быстрой проверки с ноутбука.
Логика общая с ботом (engine.py), тут только консольный вывод.

  python scanner.py --probe   # разово: сырой JSON лота, чтобы сверить поля
  python scanner.py           # цикл сканирования, пишет в deals.jsonl

Настройки — через переменные окружения (см. .env.example).
"""

from __future__ import annotations
import os
import sys
import json
import time

from engine import Config, ScanEngine, format_deal
from mrkt_client import MrktClient, MrktError, _extract_items


def build_config() -> Config:
    return Config(
        token=os.getenv("MRKT_TOKEN", ""),
        proxy=os.getenv("MRKT_PROXY") or None,
        collections=[c.strip() for c in os.getenv(
            "MRKT_COLLECTIONS", "Mood Pack,Swiss Watch,Bonded Ring").split(",") if c.strip()],
        margin=float(os.getenv("MRKT_MARGIN", "0.08")),
        floor_rank=int(os.getenv("MRKT_FLOOR_RANK", "3")),
        fee=float(os.getenv("MRKT_FEE", "0.05")),
        poll=float(os.getenv("MRKT_POLL", "12")),
    )


def probe(cfg: Config) -> None:
    client = MrktClient(cfg.token, proxy=cfg.proxy)
    coll = cfg.collections[0]
    print(f"Пробую коллекцию: {coll!r}\n")
    data = client.saling(collection=coll, low_to_high=True, count=3)
    items = _extract_items(data)
    print("Ключи верхнего уровня:", list(data.keys()) if isinstance(data, dict) else type(data))
    print(f"Нашёл лотов: {len(items)}\n")
    if items:
        print(json.dumps(items[0], ensure_ascii=False, indent=2))
        print("\nЕсли имена полей цены/id отличаются — допиши их в mrkt_client.py "
              "(_PRICE_KEYS / _ID_KEYS / _NUM_KEYS).")
    else:
        print("Лотов нет. Проверь имя коллекции (регистр!) и живость токена.")


def main() -> None:
    cfg = build_config()
    if not cfg.token:
        sys.exit("Нет MRKT_TOKEN. Возьми токен из DevTools (см. README) и "
                 "экспортируй: export MRKT_TOKEN='...'")
    if "--probe" in sys.argv:
        probe(cfg)
        return

    engine = ScanEngine(cfg, log_path=os.getenv("MRKT_LOG", "deals.jsonl"))
    print(f"Сканер запущен. Коллекций: {len(cfg.collections)}, порог: {cfg.margin:.0%}, "
          f"floor = {cfg.floor_rank}-й лот, пауза: {cfg.poll:.0f}с")
    try:
        while True:
            try:
                for rec in engine.scan_once():
                    print(format_deal(rec).replace("\n", " | "))
            except MrktError as e:
                print(f"[ошибка] {e}")
                if "протух" in str(e) or "401" in str(e):
                    print(">>> Обнови MRKT_TOKEN и перезапусти.")
                    return
            if engine.scans and engine.scans % max(1, int(300 / cfg.poll)) == 0:
                print(f"[stat] обходов {engine.scans}, найдено {engine.total_found}")
            time.sleep(cfg.poll)
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
