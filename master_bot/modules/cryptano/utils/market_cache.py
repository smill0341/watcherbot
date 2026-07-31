import threading
import time
from datetime import datetime


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
    
    # ==========================================
# ЕДИНЫЙ ЦЕНТР ДАННЫХ (Data Hub)
# Синхронизация по астрономическому времени
# ==========================================
_ohlcv_cache = {}
_cache_lock = threading.Lock()

def _get_cache_expiry(timeframe):
    """
    Рассчитывает точное время смерти кэша (Timestamp) по часам биржи (UTC)
    с задержкой +30 секунд для гарантии закрытия свечи.
    """
    now = datetime.utcnow()
    now_ts = time.time()
    
    if timeframe == '15m':
        # Снайпер (Ватчер): защитный кэш от спама на 60 секунд. Качаем живую свечу.
        return now_ts + 60
        
    elif timeframe == '1h':
        # Сброс каждые 15 минут (00, 15, 30, 45) + 30 секунд
        mins_past = now.minute
        next_mark = ((mins_past // 15) + 1) * 15
        
        if mins_past % 15 == 0 and now.second < 30:
            # Если мы в окне 30 сек после отсечки - ждем эти секунды
            seconds_to_next = 30 - now.second
        else:
            seconds_to_next = (next_mark - mins_past) * 60 - now.second + 30
        return now_ts + seconds_to_next
        
    elif timeframe == '4h':
        # Сброс каждый час в XX:00:30
        if now.minute == 0 and now.second < 30:
            seconds_to_next = 30 - now.second
        else:
            seconds_to_next = (60 - now.minute) * 60 - now.second + 30
        return now_ts + seconds_to_next
        
    elif timeframe == '1d':
        # Сброс каждые 4 часа (00, 04, 08, 12, 16, 20) + 30 секунд
        hour_block = now.hour % 4
        hours_to_next = 4 - hour_block
        
        if hour_block == 0 and now.minute == 0 and now.second < 30:
            seconds_to_next = 30 - now.second
        else:
            seconds_to_next = (hours_to_next * 3600) - (now.minute * 60) - now.second + 30
        return now_ts + seconds_to_next
        
    # По умолчанию (защита на 1 минуту)
    return now_ts + 60

def get_ohlcv_cached(exchange, symbol, timeframe, limit=250):
    """
    Главная функция. Отдает график из памяти, если он свежий.
    Если протух по астрономическому времени — качает новый.
    """
    cache_key = f"{symbol}_{timeframe}_{limit}"
    now_ts = time.time()
    
    with _cache_lock:
        cached = _ohlcv_cache.get(cache_key)
        # Проверяем, жив ли кэш
        if cached and now_ts < cached["expires_at"]:
            return cached["data"]
            
        # Если кэш пуст или протух - идем на биржу
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            
            if not data:
                return []
                
            # Рассчитываем время следующего обновления
            expires_at = _get_cache_expiry(timeframe)
            
            # Сохраняем в память
            _ohlcv_cache[cache_key] = {
                "data": data,
                "expires_at": expires_at
            }
            return data
            
        except Exception as e:
            # Если биржа лагнула (таймаут/502), спасаем бота — отдаем старый график
            if cached:
                # print(f"⚠️ [Data Hub] Bybit лагает на {symbol} {timeframe}. Отдаю старый кэш.")
                return cached["data"]
            # print(f"❌ [Data Hub] Ошибка скачивания {symbol} {timeframe}: {e}")
            return []
