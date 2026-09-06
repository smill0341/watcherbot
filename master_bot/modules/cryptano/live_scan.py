import time
from telebot import types
import datetime
import os
import threading
import re

from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.strategy.vbottom_manager import VBottomManager
from modules.cryptano.strategy.bounce_manager import BounceManager
from modules.cryptano.strategy.bounce_parent import BounceParent
# json-файлы теперь в jsonbank/, а не рядом с этим модулем — см. utils/paths.py
from modules.cryptano.utils.paths import (
    WATCHLIST_FILE, WATCHER_STATE_FILE, BOUNCE_STATE_FILE,
    TRACKED_LONG_FILE, TRACKED_VRT_FILE, MACRO_LEVELS_FILE,
)

bounce_mgr = BounceManager(BounceParent())
import json

# ================= НАСТРОЙКИ WATCHER =================
COOLDOWN_HOURS = 4     # Заморозка повторных сигналов по монете

# 🆕 НОВЫЕ РУБИЛЬНИКИ АВТОМАТИЗАЦИИ
AUTO_ADD_FROM_CRITICAL = True   # Разрешить Critical фильтру самому добавлять монеты
AUTO_REMOVE_AFTER_SIGNAL = True # Удалять монету из Watchlist после успешного сигнала

# ========== ПЕРСИСТЕНТНОСТЬ СОСТОЯНИЯ ВОТЧЕРОВ МЕЖДУ РЕСТАРТАМИ ==========
# Раньше весь прогресс паттерна (пики, C1/C2, счётчик сделок) жил только
# в памяти процесса и терялся при каждом рестарте бота. Теперь раз в скан
# (см. background_tasks.py::save_watcher_state) состояние сбрасывается на
# диск, а тут, при импорте модуля (то есть при старте бота), читается
# обратно — если файлов ещё нет (первый запуск), просто начинаем с чистого
# листа, ничего не падает.

watcher_cooldown_cache = {}
v_bottom_mgr = VBottomManager()  # Менеджер V-BOTTOM/V-GREEN-BOTTOM/V-RED-TOP стратегий
# Персистентное состояние origin-tracking (какой уровень сейчас "пробит"
# и отслеживается на монету) — та же модель, что в test_simulator.py.
# Разные ключи для V_BOTTOM/V_GREEN_BOTTOM зашиты в самих track_key
# внутри watcher_plan.py (coin_LONG / coin_VGB_LONG), поэтому это один
# общий словарь на обе стратегии, без коллизий.
tracked_origin_levels = {}
# Отдельный персистентный словарь для V_RED_TOP (SHORT от сопротивлений).
# Не общий с tracked_origin_levels: track_key там всё равно не пересекается
# (coin_LONG / coin_VGB_LONG против coin_VRT_SHORT), но разводим по смыслу —
# разные направления, разный источник уровней (supports vs resistances).
tracked_origin_levels_vrt = {}

# Восстанавливаем всё сохранённое в прошлую сессию (если было)
v_bottom_mgr.load_state(WATCHER_STATE_FILE)
bounce_mgr.load_state(BOUNCE_STATE_FILE)  # BOUNCE: вотчеры + graveyard + pierced_count
_restored_long = load_json(TRACKED_LONG_FILE, default={})
if isinstance(_restored_long, dict):
    tracked_origin_levels.update(_restored_long)
_restored_vrt = load_json(TRACKED_VRT_FILE, default={})
if isinstance(_restored_vrt, dict):
    tracked_origin_levels_vrt.update(_restored_vrt)


def save_watcher_state():
    """Сохраняет вотчеров + оба tracked-словаря на диск. Вызывается из
    background_tasks.py раз в скан-цикл — небольшая, дешёвая операция,
    не нужно дёргать чаще."""
    v_bottom_mgr.save_state(WATCHER_STATE_FILE)
    bounce_mgr.save_state(BOUNCE_STATE_FILE)
    save_json_atomic(TRACKED_LONG_FILE, tracked_origin_levels)
    save_json_atomic(TRACKED_VRT_FILE, tracked_origin_levels_vrt)


_watcher_lock = threading.Lock()

def is_in_watchlist(coin):
    """Проверяет, есть ли монета уже в списке слежения (чтобы не дублировать сообщения)."""
    wl = _load_watchlist()
    return coin in wl

def auto_add_to_watchlist(coin, direction, source="Critical"):
    """Универсальное автодобавление монет из фильтров"""
    # Блокируем ТОЛЬКО если пришло от Critical и его рубильник выключен
    if source == "Critical" and not AUTO_ADD_FROM_CRITICAL:
        return False
        
    wl = _load_watchlist()
    if coin not in wl:
        macro_levels = load_json(MACRO_LEVELS_FILE, default={})
        if coin not in macro_levels:
            from modules.cryptano.swing_hunter import build_levels_for_single_coin
            build_levels_for_single_coin(coin)

        wl[coin] = {
            "direction": direction, 
            "added_at": datetime.datetime.now().isoformat(),
            "source": source
        }
        _save_watchlist(wl)
        return True
    return False

def _load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.5)
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

def _save_watchlist(data):
    save_json_atomic(WATCHLIST_FILE, data)

def extract_watcher_data(report):
    """
    Парсит длинный отчет из watcher_plan и достает только самое важное
    для короткого watcher алерта. Безопасен для новой SFP логики.
    """
    if "ВХОД! УСЛОВИЯ ВЫПОЛНЕНЫ" not in report:
        return None
    
    # Регулярки для вытаскивания данных из нового формата текста
    price_match = re.search(r"Текущая цена:\s*([\d.]+)", report)
    price = price_match.group(1) if price_match else "N/A"
    
    dir_match = re.search(r"WATCHER\s*(SHORT|LONG)", report)
    direction = dir_match.group(1) if dir_match else "N/A"
    
    trigger_match = re.search(r"Триггер:\s*([^\n]+)", report)
    trigger_str = trigger_match.group(1).strip() if trigger_match else "SFP"
    
    sl_match = re.search(r"Стоп-лосс:\s*([\d.]+)", report)
    sl = sl_match.group(1) if sl_match else "N/A"
    
    return {
        "price": price,
        "direction": direction,
        "score": trigger_str,  # Вместо баллов передаем тип триггера (SFP/Пинбар)
        "sl": sl
    }

def manage_watchlist(command, bot, chat_id):
    """
    Обработчик команд из Телеграма: +BTC, +BTC LONG, -BTC
    Возвращает True, если команда была распознана и обработана.
    """
    cmd = command.strip().upper()
    if not (cmd.startswith('+') or cmd.startswith('-')):
        return False

    parts = cmd.split()
    action_coin = parts[0]
    action = action_coin[0]  # '+' или '-'
    coin = action_coin[1:].replace("USDT", "").replace("/", "").strip()
    
    if not coin:
        return False

    direction = "ANY"
    if len(parts) > 1:
        if parts[1] in ["LONG", "SHORT"]:
            direction = parts[1]

    wl = _load_watchlist()

    if action == '+':
        wl[coin] = {
            "direction": direction, 
            "added_at": datetime.datetime.now().isoformat(),
            "source": "Manual"  # 🆕 Отметка ручного добавления
        }
        _save_watchlist(wl)
        
        mode_text = "Любое" if direction == "ANY" else direction
        bot.send_message(
            chat_id, 
            f"✅ *{coin}* добавлена в watchlist.\n"
            f"Режим: *{mode_text}*", 
            parse_mode="Markdown"
        )
        return True
        
    elif action == '-':
        if coin in wl:
            del wl[coin]
            _save_watchlist(wl)
            bot.send_message(chat_id, f"❌ Монета *{coin}* удалена из watchlist.", parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"⚠️ Монеты *{coin}* нет в списке.", parse_mode="Markdown")
        return True
        
    return False

def show_watchlist(bot, chat_id):
    """Показывает текущий список слежения с группировкой SWING монет"""
    from telebot import types
    wl = _load_watchlist()
    if not wl:
        bot.send_message(chat_id, "📋 Мой watchlist пуст.\nДобавь монеты командой `+BTC`", parse_mode="Markdown")
        return
    
    msg = "📋 Мой watchlist:\n\n"
    swing_hunter_count = 0
    
    # 1. Сначала выводим построчно все монеты, кроме SWING
    for coin, data in wl.items():
        source = data.get('source', 'Manual')
        
        # Если монета от Swing Hunter — просто считаем её и пропускаем поштучный вывод
        if source == "Swing Hunter":
            swing_hunter_count += 1
            continue
            
        # 1. Защита от пустых значений (None)
        safe_source = source if source else "Manual"
        
        # 2. Полный словарь со всеми метками
        source_map = {
            "Manual": "MAN", 
            "Critical": "CRIT", 
            "Light": "LIGHT",
            "Swing Hunter": "SWING"
        }
        
        # 3. Безопасное форматирование
        src_label = source_map.get(safe_source, str(safe_source)).upper()
        
        direction_text = data['direction'].capitalize() if data['direction'] != "ANY" else "ANY"
        msg += f"• {coin} | {direction_text} | {src_label}\n"
    
    # 2. В самом конце выводим ОДНУ строку со счетом для Swing Hunter
    if swing_hunter_count > 0:
        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M , %d.%m")
        msg += f"\n`{now_str}` — добавлено {swing_hunter_count} монет от swinghunter\n"
    
    msg += "\nДля удаления напиши: `-ТИКЕР` (например, `-BTC`)"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Полная очистка списка", callback_data="clear_entire_watchlist"))
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)