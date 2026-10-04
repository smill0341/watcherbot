import math
import os
import threading
import time

import ccxt

from backend.modules.cryptano.utils.storage import load_json


CONFIG_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")
)
_config = load_json(CONFIG_FILE, default={})
MARKET_TYPE = _config.get("crypto", {}).get("market_mode", "swap")

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": MARKET_TYPE},
})

CACHE_TTL_SECONDS = 86400

_cache_lock = threading.RLock()
_markets_cache = {
    "expires_at": 0.0,
    "value": None,
}


def load_markets_cached(exchange, ttl_seconds=CACHE_TTL_SECONDS):
    now = time.monotonic()
    with _cache_lock:
        if _markets_cache["value"] is not None and _markets_cache["expires_at"] > now:
            return _markets_cache["value"]

        markets = exchange.load_markets()
        _markets_cache["value"] = markets
        _markets_cache["expires_at"] = now + ttl_seconds
        return markets


# Известные расхождения тикеров: некоторые монеты на Bybit торгуются
# под другим базовым тикером, чем принято (например TON -> GRAM,
# т.к. Bybit сменил пару TON/USDT -> GRAM/USDT).
KNOWN_TICKER_ALIASES = {
    "TON": "GRAM",
}


def resolve_symbol(coin, markets):
    """
    Надёжно находит реальный символ монеты на Bybit — ТОЛЬКО своп/фьючерс
    (":USDT"), спот не рассматривается вообще. Если у монеты нет своп-рынка
    на Bybit — resolve_symbol возвращает None, и монета просто пропускается
    (осознанное решение: не торговать/не строить уровни там, где нет
    фьючерсного рынка, а не молча откатываться на другой рынок с другими
    ценами/объёмом/историей).
    Возвращает найденный symbol (str) или None, если своп-рынка нет.
    """
    coin = coin.upper().strip()

    candidates = [f"{coin}/USDT:USDT"]

    alias = KNOWN_TICKER_ALIASES.get(coin)
    if alias:
        candidates.append(f"{alias}/USDT:USDT")

    for symbol in candidates:
        if symbol in markets:
            return symbol

    for symbol, market in markets.items():
        if ":USDT" not in symbol:
            continue
        base = (market.get("base") or "").upper()
        if base == coin or base == alias:
            return symbol

    return None


def price_precision_from_market(market_info, default=4):
    precision = market_info.get("precision", {}).get("price", default)
    if isinstance(precision, float) and precision < 1:
        return int(round(-math.log10(precision)))
    if isinstance(precision, int):
        return precision
    return default
