import time
import datetime
import os
import threading
import pandas as pd
import numpy as np
from scipy.signal import find_peaks
import schedule  
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.levels_builder import build_levels

# =========================================================
# ⚙️ НАСТРОЙКИ РАСПИСАНИЯ
# =========================================================
TIME_ASIAN_CLOSE = "03:05"
TIME_US_OPEN = "15:05"       

# Сколько дней монета может отсутствовать в топ-70 по обороту, прежде чем
# её запись в macro_levels.json реально удаляется (мердж вместо перезаписи,
# см. build_macro_levels). Слишком мало — вернёмся к старому багу (вотчер
# осиротеет за один цикл), слишком много — файл будет копить мусор.
MACRO_STALE_DAYS = 14

# 🎛 НАСТРОЙКИ V2 ФИЛЬТРОВ
IMPULSE_ATR_MULTIPLIER = 2.5  # Цена должна улететь минимум на 2.5 ATR от зоны
IMPULSE_LOOKAHEAD_DAYS = 10   # Даем цене 10 дней на то, чтобы показать этот импульс

# BASE_DIR указывает на modules/cryptano/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MACRO_LEVELS_FILE = os.path.join(BASE_DIR, "macro_levels.json")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

LIGHT_RADAR_INTERVAL_SEC = 60
MIN_VOLUME_USD = 10_000_000

_hunter_lock = threading.Lock()


# Вынесено на уровень модуля (раньше жило только внутри build_macro_levels)
# — build_levels_for_single_coin теперь тоже качает 1M/1W и должен так же
# переживать rate-limit биржи, а не падать с первой ошибки.
def _safe_fetch_ohlcv(sym, tf, lim):
    for attempt in range(5):  # 5 попыток пробить блок
        try:
            return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"⚠️  Bybit Rate Limit. Ждем 4 сек (Попытка {attempt+1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы после 5 попыток")


def build_macro_levels(bot=None, admin_chat_id=None):
    print(f"[SWING HUNTER] Запуск генерации институциональных зон...")
    try:
        # Загружаем рынки из сети только один раз
        if not exchange.markets:
            exchange.load_markets(reload=True)
        else:
            exchange.load_markets(reload=False)
        tickers = exchange.fetch_tickers()
        
        # Собираем пары с их объемами для последующей сортировки
        symbols_with_volume = []
        for sym, tick in tickers.items():
            if sym.endswith('/USDT') or sym.endswith(':USDT'):
                vol = float(tick.get('quoteVolume') or 0)
                if vol >= MIN_VOLUME_USD:
                    symbols_with_volume.append((sym, vol))
                    
        # Сортируем по убыванию объема торгов и берем ТОП-70
        symbols_with_volume.sort(key=lambda x: x[1], reverse=True)
        valid_symbols = [sym for sym, vol in symbols_with_volume[:70]]
        
        print(f"🔥 Найдено {len(symbols_with_volume)} монет. Фильтруем до ТОП-70 самых ликвидных.")

        # МЕРДЖ вместо полной перезаписи: если монета в этот раз не попала
        # в топ-70 (или биржа моргнула ошибкой на fetch_tickers) — её старая
        # запись остаётся как есть, а не пропадает из файла. Иначе вотчер,
        # который на неё завязан, замирает (check_v_* видит coin_macro=None
        # и выходит рано) и на дашборде превращается в "?", хотя формально
        # ещё жив в памяти — см. cleanup_ghost_watchers.py и обсуждение бага.
        macro_base = load_json(MACRO_LEVELS_FILE, default={})
        seen_coins = set()

        for symbol in valid_symbols:
            time.sleep(0.3)
            coin = symbol.split("/")[0].replace(":USDT", "")

            try:
                # === АНАЛИЗ через новый levels_builder ===
                # 1M/1W добавлены для нового макро-слоя (_extract_macro_swings)
                # в levels_builder.py — build_levels() теперь их требует первыми
                # двумя аргументами. limit=60/150 — тот же запас, что в тестовой
                # версии (60 месяцев / 150 недель истории).
                ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
                ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
                ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", 365)
                ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", 200)

                if len(ohlcv_1d) < 50:
                    continue

                seen_coins.add(coin)  # реально дошли до расчёта — точка отсчёта для "протухания"

                df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
                df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
                df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

                levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

                has_any = (levels["supports"] or levels["resistances"])
                if has_any:
                    macro_base[coin] = {
                        "supports": levels["supports"],
                        "resistances": levels["resistances"],
                        "updated_at": datetime.datetime.now().isoformat()
                    }
                else:
                    # Явно пересчитали и уровней не нашли — это не "выпал из
                    # топ-70", а честный пустой результат, тут можно смело убрать.
                    macro_base.pop(coin, None)

            except Exception as e:
                print(f"[HUNTER ERROR] Ошибка для {coin}: {e}")
                continue

        # Чистим только по-настоящему протухшие записи — монеты, которые
        # не сканировались (не в топ-70) уже дольше STALE_DAYS. Если монета
        # просто на пару циклов выпала и вернулась — её данные спокойно ждут.
        cutoff = datetime.datetime.now() - datetime.timedelta(days=MACRO_STALE_DAYS)
        stale_coins = []
        for coin, entry in macro_base.items():
            if coin == "_meta" or coin in seen_coins:
                continue
            updated_at = entry.get("updated_at") if isinstance(entry, dict) else None
            try:
                if not updated_at or datetime.datetime.fromisoformat(updated_at) < cutoff:
                    stale_coins.append(coin)
            except Exception:
                stale_coins.append(coin)  # битая/непонятная дата — тоже протухшая
        for coin in stale_coins:
            del macro_base[coin]
        if stale_coins:
            print(f"🧹 Убраны протухшие (>{MACRO_STALE_DAYS} дн. вне топ-70): {stale_coins}")

        # 🕒 Метаданные всего файла — чтобы одним взглядом видеть, когда последний раз обновлялось
        macro_base["_meta"] = {
            "last_build": datetime.datetime.now().isoformat(),
            "coins_count": len([k for k in macro_base if k != "_meta"]),
        }

        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        print(f"✅ [SWING HUNTER] Сбор завершен! Зоны сохранены: {MACRO_LEVELS_FILE}")
        return macro_base
            
    except Exception as e:
        print(f"❌ [SWING HUNTER] КРИТИЧЕСКАЯ ОШИБКА: {e}")
        return {}

def build_levels_for_single_coin(coin):
    """Быстрая генерация уровней для одной монеты (для авто-добавлений)."""
    try:
        # Резолвим реальный символ на бирже (спот/футы + известные алиасы
        # тикеров типа TON->TONCOIN) — не все монеты торгуются как SPOT
        # COIN/USDT, часть только как COIN/USDT:USDT (перпы), либо не
        # торгуются на Bybit вообще (например токенизированные акции).
        if not exchange.markets:
            exchange.load_markets(reload=False)

        markets = exchange.markets or {}
        symbol = resolve_symbol(coin, markets)

        if not symbol:
            print(f"[HUNTER SKIP] {coin}: нет рынка ни SPOT, ни FUTURES на Bybit — пропускаю.")
            return None

        ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
        ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
        ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", 365)
        ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", 200)

        if len(ohlcv_1d) < 50:
            return None

        df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
        df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
        df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

        levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)
        
        macro_base = load_json(MACRO_LEVELS_FILE, default={})
        macro_base[coin] = {
            "supports": levels["supports"],
            "resistances": levels["resistances"],
            "updated_at": datetime.datetime.now().isoformat()
        }
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        
        return levels
    except Exception as e:
        print(f"[HUNTER ERROR] Ошибка при построении уровней для {coin}: {e}")
        return None

def minute_radar(bot, admin_chat_id):
    """
    Радар, который сканирует рынок каждую минуту и ждет, 
    пока цена зайдет ВНУТРЬ созданной зоны ликвидности.
    """
    while True:
        time.sleep(LIGHT_RADAR_INTERVAL_SEC)
        try:
            macro_base = load_json(MACRO_LEVELS_FILE, default={})
            if not macro_base: continue 
                
            watchlist = load_json(WATCHLIST_FILE, default={})
            tickers = exchange.fetch_tickers()
            added_coins = []
            
            for symbol, tick in tickers.items():
                if not (symbol.endswith('/USDT') or symbol.endswith(':USDT')): continue
                coin = symbol.split("/")[0].replace(":USDT", "")
                if coin not in macro_base: continue
                    
                current_price = float(tick.get('last') or 0)
                if current_price == 0: continue
                if coin in watchlist: continue
                    
                levels = macro_base[coin]
                is_triggered = False
                trigger_direction = ""
                
                # Проверяем касание именно ЗОНЫ, а не точной линии
                for sup in levels.get("supports", []):
                    # Если цена внутри зоны или на 1% выше нее (на подходе)
                    if sup['min'] <= current_price <= (sup['max'] * 1.01): 
                        is_triggered = True
                        trigger_direction = "LONG"
                        break
                        
                if not is_triggered:
                    for res in levels.get("resistances", []):
                        # Если цена внутри зоны или на 1% ниже нее
                        if (res['min'] * 0.99) <= current_price <= res['max']: 
                            is_triggered = True
                            trigger_direction = "SHORT"
                            break
                            
                if is_triggered:
                    watchlist[coin] = {
                        "direction": trigger_direction,
                        "added_at": datetime.datetime.now().isoformat(),
                        "source": "Swing Hunter"
                    }
                    added_coins.append(coin)
            
            if added_coins:
                save_json_atomic(WATCHLIST_FILE, watchlist)
                print(f"🎯 [SWING HUNTER] Радар: В Watchlist залетело {len(added_coins)} монет.")

        except Exception as e:
            print(f"[RADAR ERROR] {e}")

def run_heavy_generator(bot, admin_chat_id):
    """Фоновый поток для генерации уровней по расписанию."""
    schedule.every().day.at(TIME_ASIAN_CLOSE).do(build_macro_levels, bot, admin_chat_id)
    schedule.every().day.at(TIME_US_OPEN).do(build_macro_levels, bot, admin_chat_id)
    while True:
        schedule.run_pending()
        time.sleep(1)

def start_swing_hunter(bot, admin_chat_id):
    """Инициализация Swing Hunter: запускает фоновые потоки, не блокируя старт бота."""
    threading.Thread(target=run_heavy_generator, args=(bot, admin_chat_id), daemon=True).start()
    threading.Thread(target=minute_radar, args=(bot, admin_chat_id), daemon=True).start()
    print("[SWING HUNTER] Инициализирован (heavy_generator + minute_radar запущены в фоне)")

# ТОЧКА ВХОДА ДЛЯ ПРЯМОГО ЗАПУСКА
if __name__ == "__main__":
    print("🚀 [SWING HUNTER] Начинаем принудительный сбор уровней...")
    build_macro_levels()
    print("🏁 [SWING HUNTER] Сбор завершен.")