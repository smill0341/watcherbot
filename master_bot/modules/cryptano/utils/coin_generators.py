# modules/cryptano/utils/coin_generators.py
import time
import datetime
import os
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json, save_json_atomic

# Путь к watchlist.json (поднимаемся на уровень вверх из папки utils)
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "watchlist.json")

def get_momentum_coins(limit_per_side=10, min_volume_usd=10000000):
    """Скачивает тикеры и возвращает Топ-N растущих и падающих."""
    try:
        tickers = exchange.fetch_tickers()
        valid_coins = []
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') or symbol.endswith(':USDT'):
                pct_change = ticker.get('percentage')
                volume = ticker.get('quoteVolume')
                
                if pct_change is not None and volume is not None and float(volume) >= min_volume_usd:
                    coin_name = symbol.split("/")[0].replace(":USDT", "").strip()
                    valid_coins.append({"coin": coin_name, "pct": float(pct_change)})
                        
        valid_coins.sort(key=lambda x: x["pct"], reverse=True)
        
        results = []
        added_names = set()
        
        # 1. ПАМПЫ (ищем шорт)
        for item in valid_coins:
            if len(results) >= limit_per_side: break
            if item["coin"] not in added_names and item["pct"] > 5.0:
                results.append({"coin": item["coin"], "direction": "SHORT", "source": "MOMENTUM_PUMP", "info": f"+{item['pct']:.1f}%"})
                added_names.add(item["coin"])
                
        # 2. ДАМПЫ (ищем лонг)
        losers_count = 0
        for item in reversed(valid_coins):
            if losers_count >= limit_per_side: break
            if item["coin"] not in added_names and item["pct"] < -5.0:
                results.append({"coin": item["coin"], "direction": "LONG", "source": "MOMENTUM_DUMP", "info": f"{item['pct']:.1f}%"})
                added_names.add(item["coin"])
                losers_count += 1
                
        return results
    except Exception as e:
        print(f"[GENERATORS ERROR] Ошибка: {e}")
        return []

def update_momentum_watchlist(bot=None, admin_chat_id=None):
    """
    Функция-Дворник. Сама берет монеты, сама чистит старые, сама пишет новые в JSON.
    Никак не трогает и не ломает live_scan.py.
    """
    print("🔄 [MOMENTUM] Запуск сканирования Лидеров дня...")
    new_coins = get_momentum_coins(limit_per_side=10)
    
    if not new_coins:
        return
        
    wl = load_json(WATCHLIST_FILE, default={})
    
    # 1. ДВОРНИК: Удаляем все старые монеты, которые были добавлены этим же скриптом
    keys_to_delete = [k for k, v in wl.items() if v.get("source") in ["MOMENTUM_PUMP", "MOMENTUM_DUMP"]]
    for k in keys_to_delete:
        del wl[k]
        
    # 2. ДОБАВЛЕНИЕ НОВЫХ
    added_count = 0
    pump_names = []
    dump_names = []
    
    for item in new_coins:
        coin = item["coin"]
        # Не перезаписываем монету, если ты добавил её руками или её дал Critical
        if coin not in wl:
            wl[coin] = {
                "direction": item["direction"],
                "added_at": datetime.datetime.now().isoformat(),
                "source": item["source"],
                "info": item["info"]
            }
            added_count += 1
            if item["direction"] == "SHORT":
                pump_names.append(coin)
            else:
                dump_names.append(coin)
            
    # Сохраняем обновленный файл
    save_json_atomic(WATCHLIST_FILE, wl)
    
    print(f"✅ [MOMENTUM] Очищено: {len(keys_to_delete)} | Добавлено: {added_count}")
    
    # Отправляем отчет в Телеграм
    if bot and admin_chat_id:
        msg = f"🔥 *Обновление ТОП Лидеров*\n\n"
        msg += f"🧹 Старые отработанные монеты удалены.\n\n"
        if pump_names:
            msg += f"🔴 *Ищем ШОРТ (Пампы):*\n`{', '.join(pump_names)}`\n\n"
        if dump_names:
            msg += f"🟢 *Ищем ЛОНГ (Дампы):*\n`{', '.join(dump_names)}`\n\n"
        msg += f"👁 Watcher начал охоту за новыми жертвами."
        
        try:
            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
        except Exception:
            pass