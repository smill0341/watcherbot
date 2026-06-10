import threading
import time


CACHE_TTL_SECONDS = 86400

_cache_lock = threading.RLock()
_markets_cache = {
    "expires_at": 0.0,
    "value": None,
}
_top_coins_cache = {}


def load_markets_cached(exchange, ttl_seconds=CACHE_TTL_SECONDS):
    now = time.monotonic()
    with _cache_lock:
        if _markets_cache["value"] is not None and _markets_cache["expires_at"] > now:
            return _markets_cache["value"]

        markets = exchange.load_markets()
        _markets_cache["value"] = markets
        _markets_cache["expires_at"] = now + ttl_seconds
        return markets


def get_top_usdt_coins_cached(exchange, limit=150, min_quote_volume=8_000_000.0, ttl_seconds=3600):
    cache_key = (limit, float(min_quote_volume))
    now = time.monotonic()

    with _cache_lock:
        cached = _top_coins_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return list(cached["value"])

        load_markets_cached(exchange, ttl_seconds=86400)
        tickers = exchange.fetch_tickers()

        # Черный список стейблкоинов и мусора
        stablecoins = ["USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDE"]

        usdt_pairs = []
        for symbol, ticker in tickers.items():
            # 1. Берем USDT пары (и спот, и фьючерсы)
            if symbol.endswith('/USDT') or symbol.endswith(':USDT'):
                
                # 2. Проверяем, не стейблкоин ли это
                is_stablecoin = any(stable in symbol for stable in stablecoins)
                if not is_stablecoin:
                    
                    # 3. Фильтруем по объему
                    try:
                        quote_volume = float(ticker.get("quoteVolume", 0) or 0)
                    except (ValueError, TypeError):
                        quote_volume = 0.0

                    if quote_volume > min_quote_volume:
                        usdt_pairs.append((symbol, quote_volume))

        usdt_pairs.sort(key=lambda x: x[1], reverse=True)
        coins = [pair[0] for pair in usdt_pairs[:limit]]

        _top_coins_cache[cache_key] = {
            "value": coins,
            "expires_at": now + ttl_seconds,
        }
        return list(coins)
