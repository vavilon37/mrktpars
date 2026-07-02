"""
Клиент к неофициальному API @mrkt (https://api.tgmrkt.io/api/v1).

Использованы ТОЛЬКО задокументированные эндпоинты:
  - POST /auth           -> получить токен из Telegram init_data
  - POST /gifts/saling   -> листинги на продаже (фильтры + сортировка)

Эндпоинтов покупки/выставления тут НЕТ намеренно: их нет в публичной доке,
их надо снять из DevTools (F12 -> Network) и добавить отдельно на Этапе 2.
Мы не выдумываем то, чего не проверили.

MRKT стоит за Cloudflare, поэтому по возможности используем curl_cffi
с impersonate="chrome" — обычный requests часто ловит 403.
"""

from __future__ import annotations
import time

try:
    from curl_cffi import requests as _rq  # type: ignore
    _IMPERSONATE = True
except ImportError:  # запасной путь, если curl_cffi не поставлен
    import requests as _rq  # type: ignore
    _IMPERSONATE = False

BASE = "https://api.tgmrkt.io/api/v1"


class MrktError(RuntimeError):
    pass


class MrktClient:
    def __init__(self, token: str, proxy: str | None = None, timeout: int = 20):
        if not token:
            raise MrktError("Пустой токен. Возьми его из DevTools (см. README).")
        self.token = token
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def _post(self, path: str, json: dict) -> dict:
        kwargs = dict(
            json=json,
            headers={"Authorization": self.token,
                     "Content-Type": "application/json"},
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if _IMPERSONATE:
            kwargs["impersonate"] = "chrome"
        r = _rq.post(f"{BASE}{path}", **kwargs)
        if r.status_code == 401:
            raise MrktError("401 — токен протух. Возьми свежий из DevTools.")
        if r.status_code != 200:
            raise MrktError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # --- проверенные эндпоинты -------------------------------------------

    @staticmethod
    def auth(init_data: str, proxy: str | None = None) -> str:
        """Обменять Telegram init_data на токен. Для Этапа 2 (Pyrogram)."""
        kwargs = dict(json={"data": init_data}, timeout=20,
                      proxies={"http": proxy, "https": proxy} if proxy else None)
        if _IMPERSONATE:
            kwargs["impersonate"] = "chrome"
        r = _rq.post(f"{BASE}/auth", **kwargs)
        if r.status_code != 200:
            raise MrktError(f"/auth -> HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise MrktError(f"/auth не вернул token, а вот что вернул: {data}")
        return token

    def saling(self, *, collection: str | None = None, low_to_high: bool = True,
               count: int = 20, cursor: str | None = None,
               min_price: float | None = None, max_price: float | None = None,
               extra: dict | None = None) -> dict:
        """Список лотов на продаже. count максимум 20 (ограничение API)."""
        body: dict = {
            "collectionNames": [collection] if collection else [],
            "modelNames": [], "backdropNames": [], "symbolNames": [],
            "ordering": "Price",
            "lowToHigh": low_to_high,
            "count": min(count, 20),
            "promotedFirst": False,
        }
        if cursor:
            body["cursor"] = cursor
        if min_price is not None:
            body["minPrice"] = min_price
        if max_price is not None:
            body["maxPrice"] = max_price
        if extra:
            body.update(extra)
        return self._post("/gifts/saling", body)

    def cheapest(self, collection: str, count: int = 20, retries: int = 3) -> list[dict]:
        """Самые дешёвые лоты коллекции, отсортированы по цене по возрастанию."""
        for attempt in range(retries):
            try:
                data = self.saling(collection=collection, low_to_high=True, count=count)
                return _extract_items(data)
            except MrktError as e:
                if "401" in str(e) or attempt == retries - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return []


# --- защитные парсеры: точные имена полей неизвестны, пока не увидим probe ---

_ITEM_KEYS = ("items", "gifts", "results", "data", "list")
_PRICE_KEYS = ("price", "salePrice", "sellPrice", "amount", "priceTon", "tonPrice")
_ID_KEYS = ("id", "giftId", "nftId", "_id")
_NUM_KEYS = ("number", "num", "index", "tgId")


def _extract_items(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in _ITEM_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                return v
        # иногда список лежит на один уровень глубже
        for v in data.values():
            if isinstance(v, dict):
                for k in _ITEM_KEYS:
                    if isinstance(v.get(k), list):
                        return v[k]
    return []


def price_of(item: dict) -> float | None:
    for k in _PRICE_KEYS:
        v = item.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def id_of(item: dict) -> str:
    for k in _ID_KEYS:
        if item.get(k) is not None:
            return str(item[k])
    return "?"


def number_of(item: dict) -> str:
    for k in _NUM_KEYS:
        if item.get(k) is not None:
            return str(item[k])
    return "?"
