import math
import ccxt
from modules.cryptano.market_cache import get_top_usdt_coins_cached

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
    """Получает топ ликвидных USDT-пар с биржи."""
    try:
        tickers = exchange.fetch_tickers()
        liquid_coins = []
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and ':' not in symbol:
                # Безопасное извлечение: если None, превращаем в 0.0
                raw_vol = ticker.get('quoteVolume')
                quote_volume = float(raw_vol) if raw_vol is not None else 0.0

                if quote_volume >= min_volume:
                    liquid_coins.append((symbol, quote_volume))
        liquid_coins.sort(key=lambda x: x[1], reverse=True)
        return [coin[0] for coin in liquid_coins[:limit]]
    except Exception as e:
        print(f"Ошибка получения топ монет: {e}")
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
