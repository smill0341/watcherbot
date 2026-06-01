import math
import ccxt
from master_bot.modules.cryptano.utils.market_cache import get_top_usdt_coins_cached

exchange = ccxt.bybit({"enableRateLimit": True})

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))


def get_top_coins(limit=150, min_volume=5000000):
    """Исправленная версия получения топ ликвидных пар Bybit."""
    try:
        print("[DIAGNOSTIC] Запуск get_top_coins...")
        tickers = exchange.fetch_tickers()
        if not tickers:
            print("[WARNING] Биржа вернула пустой список тикеров!")
            return []
            
        print(f"[DIAGNOSTIC] Успешно получено {len(tickers)} тикеров с биржи.")
        liquid_coins = []
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') or symbol.endswith('/USDT:USDT'):
                # CCXT Standard: quoteVolume — это объем в USDT
                quote_volume = ticker.get('quoteVolume')
                base_volume = ticker.get('baseVolume') or ticker.get('volume') or 0.0
                last_price = ticker.get('last') or 0.0
                
                # Защита: если quoteVolume пустой или подозрительно маленький (меньше базового), 
                # рассчитываем долларовый объем вручную: объем_в_монетах * цена
                if quote_volume is None or float(quote_volume) <= float(base_volume):
                    calc_volume = float(base_volume) * float(last_price)
                else:
                    calc_volume = float(quote_volume)
                
                if calc_volume >= min_volume:
                    liquid_coins.append((symbol, calc_volume))
                    
        print(f"[DIAGNOSTIC] Прошли фильтр по объему (>{min_volume}$): {len(liquid_coins)} монет.")
        liquid_coins.sort(key=lambda x: x[1], reverse=True)
        
        result = [coin[0] for coin in liquid_coins[:limit]]
        print(f"[DIAGNOSTIC] Итоговый список для сканера содержит: {len(result)} монет.")
        return result
    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА BYBIT] Не удалось получить тикеры: {e}")
        return []


def price_precision_for_value(value, one_to_ten_decimals=3, small_extra_decimals=3):
    value = float(value)
    if value >= 1000:
        return 0
    if value >= 100:
        return 1
    if value >= 10:
        return 2
    if value >= 1:
        return one_to_ten_decimals

    str_value = f"{value:.10f}"
    if "." not in str_value:
        return one_to_ten_decimals

    decimals = str_value.split(".")[1]
    zeros = 0
    for char in decimals:
        if char == "0":
            zeros += 1
        else:
            break
    return zeros + small_extra_decimals


def price_precision_from_market(market_info, default=4):
    precision = market_info.get("precision", {}).get("price", default)
    if isinstance(precision, float) and precision < 1:
        return int(round(-math.log10(precision)))
    if isinstance(precision, int):
        return precision
    return default


def format_price(value):
    if value is None:
        return "Нет"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "Нет"
    if math.isnan(value):
        return "Нет"

    precision = price_precision_for_value(value)
    if precision == 0:
        return f"{int(value)}"
    return f"{value:.{precision}f}"
