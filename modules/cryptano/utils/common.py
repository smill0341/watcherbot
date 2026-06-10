import math

import ccxt

from modules.cryptano.utils.market_cache import get_top_usdt_coins_cached
from modules.cryptano.utils.crypto_utils import exchange

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))


def get_top_100_coins():
    return get_top_usdt_coins_cached(exchange, limit=100, min_quote_volume=5_000_000.0)


def price_precision_from_market(market_info, default=4):
    precision = market_info.get("precision", {}).get("price", default)
    if isinstance(precision, float) and precision < 1:
        return int(round(-math.log10(precision)))
    if isinstance(precision, int):
        return precision
    return default


def format_price(value):
    """
    Универсальная функция форматирования цены с новыми правилами:
    - >= 1000: 1 знак после запятой
    - >= 1 и < 1000: 2 знака после запятой
    - < 1: 3 знака после последнего ведущего нуля
    """
    if value is None:
        return "Нет"
    try:
        val = float(value)
    except (ValueError, TypeError):
        return str(value)

    if val == 0:
        return "0"

    abs_val = abs(val)
    
    # 1. Для крупных чисел (>= 1000) -> 1 знак
    if abs_val >= 1000:
        res = f"{val:.1f}"
    # 2. Для средних чисел (от 1 до 1000) -> 2 знака
    elif abs_val >= 1:
        res = f"{val:.2f}"
    # 3. Для микро-чисел (< 1) -> 3 знака после последнего ведущего нуля
    else:
        # Вычисляем позицию первой значащей цифры через логарифм
        first_significant_digit_pos = int(math.floor(math.log10(abs_val)))
        # Нам нужно 3 знака начиная с этой позиции
        decimals = 3 - first_significant_digit_pos - 1
        res = f"{val:.{decimals}f}"

    return res


# Алиас для обратной совместимости
fmt_p = format_price
