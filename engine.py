"""
Ядро сканера — общее для CLI (scanner.py) и бота (bot.py).

Config хранит настройки (коллекции, порог маржи, токен, прокси) и умеет
сохраняться в JSON, чтобы переживать перезапуск. ScanEngine делает один обход
рынка (scan_once) и возвращает НОВЫЕ найденные флипы.
"""

from __future__ import annotations
import json
import datetime as dt
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor

from mrkt_client import MrktClient, MrktError, price_of, id_of, number_of


@dataclass
class Config:
    token: str = ""                       # токен MRKT из DevTools (или /settoken в боте)
    proxy: str | None = None              # socks5://... — напр. через немецкий VPS
    collections: list[str] = field(
        # ЛИКВИДНЫЕ коллекции с высоким дневным объёмом — там чаще всего
        # проскакивают транзиентные недооценки. Floor 3-35 TON (по карману).
        # Плохой выбор: floor-спам типа Xmas Stocking (все лоты по 2.46, спред 0)
        # и премиум Plush Pepe (~5500 TON). Менять из бота: /watch, /unwatch.
        default_factory=lambda: ["Mood Pack", "Swiss Watch", "Bonded Ring"])
    margin: float = 0.04                  # мин. скидка к floor (при комиссии 0% и
                                          # 4% уже прибыль; подними, если fee вырастет)
    floor_rank: int = 3                   # какой по счёту лот считать floor
    fee: float = 0.0                      # комиссия продавца — ДИНАМИЧЕСКАЯ (промо).
                                          # Сейчас 0% (проверено в приложении). API
                                          # не отдаёт — если вырастет, обнови тут.
    poll: float = 5.0                     # пауза между обходами, сек (API держит
                                          # ~1 rps без 429; ниже 5с смысла мало —
                                          # см. вывод про ручную скорость в README)
    paused: bool = False

    @classmethod
    def load(cls, path: str) -> "Config":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__annotations__}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)


def is_buyable(item: dict) -> bool:
    """Отсеиваем то, что нельзя перепродать: свои и залоченные лоты."""
    if item.get("isMine"):
        return False
    if item.get("isLocked") or item.get("isLockedForSale"):
        return False
    return True


def find_deals(items: list[dict], cfg: Config) -> tuple[float | None, list[dict]]:
    """floor = цена лота ранга floor_rank; deals = лоты дешевле floor*(1-margin)."""
    priced = [(price_of(it), it) for it in items if is_buyable(it)]
    priced = [(p, it) for p, it in priced if p is not None]
    priced.sort(key=lambda x: x[0])
    if len(priced) < cfg.floor_rank:
        return (priced[0][0] if priced else None), []
    floor = priced[cfg.floor_rank - 1][0]
    threshold = floor * (1 - cfg.margin)
    deals = []
    for p, it in priced:
        if p <= threshold:
            it = dict(it)
            it["_buy"] = p
            it["_floor"] = floor
            it["_net_profit"] = round(floor * (1 - cfg.fee) - p, 3)
            it["_margin_pct"] = round((floor - p) / floor * 100, 1)
            deals.append(it)
    return floor, deals


class TokenExpired(MrktError):
    pass


class ScanEngine:
    def __init__(self, cfg: Config, log_path: str = "deals.jsonl"):
        self.cfg = cfg
        self.log_path = log_path
        self.seen: set[str] = set()
        self.scans = 0
        self.total_found = 0
        self.last_scan: str | None = None
        self.last_error: str | None = None

    def _client(self) -> MrktClient:
        return MrktClient(self.cfg.token, proxy=self.cfg.proxy)

    def scan_once(self) -> list[dict]:
        """Один обход коллекций. Запросы идут ПАРАЛЛЕЛЬНО, поэтому полный проход
        занимает ~время одного запроса, а не сумму по всем коллекциям."""
        if not self.cfg.token:
            raise MrktError("Токен не задан. Пришли его командой /settoken.")
        client = self._client()
        colls = list(self.cfg.collections)

        def fetch(coll):
            try:  # count=10 — для детекта floor (3-й лот) хватает, ответ вдвое легче
                return coll, client.cheapest(coll, count=10), None
            except Exception as e:  # noqa: BLE001 — одна коллекция не валит проход
                return coll, None, e

        # все коллекции опрашиваем разом
        workers = min(10, max(1, len(colls)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(fetch, colls))

        out: list[dict] = []
        token_expired = False
        for coll, items, err in results:
            if err is not None:
                if "протух" in str(err) or "401" in str(err):
                    token_expired = True
                else:
                    self.last_error = f"{coll}: {err}"
                continue
            _, deals = find_deals(items, self.cfg)
            for d in deals:
                key = f"{coll}:{id_of(d)}"
                if key in self.seen:
                    continue
                self.seen.add(key)
                rec = {
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "collection": coll, "id": id_of(d), "number": number_of(d),
                    "buy": d["_buy"], "floor": d["_floor"],
                    "margin_pct": d["_margin_pct"],
                    "net_profit_ton": d["_net_profit"],
                }
                out.append(rec)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.scans += 1
        self.total_found += len(out)
        self.last_scan = dt.datetime.now().isoformat(timespec="seconds")
        # защита памяти на долгом ране
        if len(self.seen) > 50000:
            self.seen.clear()
        if token_expired and not out:
            raise TokenExpired("токен протух")
        return out


def format_deal(rec: dict) -> str:
    return (f"💎 {rec['collection']} #{rec['number']}\n"
            f"цена {rec['buy']} → floor {rec['floor']} (-{rec['margin_pct']}%)\n"
            f"чистыми ≈ {rec['net_profit_ton']} TON после комиссии\n"
            f"id: {rec['id']}")
