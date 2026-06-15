import math
import ccxt
import os
from modules.cryptano.utils.market_cache import get_top_usdt_coins_cached
from modules.cryptano.utils.storage import load_json

# Читаем конфиг при старте (по умолчанию ставим swap/фьючерсы)
CONFIG_FILE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json"))
_config = load_json(CONFIG_FILE, default={})
MARKET_TYPE = _config.get("crypto", {}).get("market_mode", "swap")
MAX_COINS_LIMIT = 150

# Динамический порог объема (Фьючи = 10 млн, Спот = 1 млн)
MIN_DAILY_VOLUME = 10000000 if MARKET_TYPE == "swap" else 1000000

# === ЕДИНАЯ ТОЧКА ВХОДА К BYBIT ===
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": MARKET_TYPE}
})

def switch_market_mode(new_mode):
    """Мгновенно переключает рынок для ВСЕХ сканеров бота с защитой от None"""
    global MARKET_TYPE, MIN_DAILY_VOLUME
    MARKET_TYPE = new_mode
    
    # ЗАЩИТА: Если options по какой-то причине None, создаем пустой словарь
    if exchange.options is None:
        exchange.options = {}
        
    # Безопасно меняем тип рынка
    exchange.options['defaultType'] = new_mode
    
    # Меняем порог объема
    MIN_DAILY_VOLUME = 10000000 if new_mode == "swap" else 1000000
    
    # Принудительно очищаем кэш рынков, чтобы Bybit отдал правильные тикеры
    exchange.load_markets(reload=True) 
    print(f"[CRYPTO_UTILS] 🔄 Рынок успешно переключен на: {new_mode.upper()}")


def get_top_coins(limit=MAX_COINS_LIMIT, min_volume=MIN_DAILY_VOLUME, return_stats=False):
    try:
        tickers = exchange.fetch_tickers()
        if not tickers:
            return ([], 0, 0) if return_stats else []
            
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
                    
        liquid_coins.sort(key=lambda x: x[1], reverse=True)
        
        result = [coin[0] for coin in liquid_coins[:limit]]
        if return_stats:
            return result, len(tickers), len(liquid_coins)
        return result
    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА BYBIT] Не удалось получить тикеры: {e}")
        return ([], 0, 0) if return_stats else []


