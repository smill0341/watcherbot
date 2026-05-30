import threading

# Import the background tasks
from modules.cryptano.cryptano import auto_scheduler as run_crypto
from modules.cryptano.scanner import run_light_scanner
from modules.footballnogoal.football import run_football_monitor
from modules.playerpropsbasket.player_props import run_nba_monitor

def start_all_background_tasks(bot, admin_chat_id):
    """
    Starts all background monitoring tasks.
    Currently uses threading.Thread as originally implemented in main.py.
    """
    import asyncio
    
    # Крипто-бот с расширенными фильтрами и сигналами
    threading.Thread(target=run_crypto, args=(bot, admin_chat_id), daemon=True).start()
    
    # УПРОЩЕННЫЙ автобот для легкого сканирования рынка
    threading.Thread(target=lambda b, a: asyncio.run(run_light_scanner(b, a)), args=(bot, admin_chat_id), daemon=True).start()
    
    # Спортивные мониторы 
    threading.Thread(target=run_football_monitor, args=(bot, admin_chat_id), daemon=True).start()
    threading.Thread(target=run_nba_monitor, args=(bot, admin_chat_id), daemon=True).start()
