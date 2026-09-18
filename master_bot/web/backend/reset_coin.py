import sqlite3
import os
from datetime import datetime, timezone

# Дата, с которой начинаются "рваные" данные (16 сентября 2026)
target_date = datetime(2026, 9, 16, tzinfo=timezone.utc)
target_timestamp = int(target_date.timestamp())

db_path = os.path.normpath(os.path.join("modules", "cryptano", "database", "candles.db"))

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Считаем, сколько кривых свечей накопилось
    cursor.execute("SELECT COUNT(*) FROM candles WHERE timestamp >= ?", (target_timestamp,))
    bad_candles_count = cursor.fetchone()[0]
    
    if bad_candles_count > 0:
        # Удаляем хвост у всех монет
        cursor.execute("DELETE FROM candles WHERE timestamp >= ?", (target_timestamp,))
        conn.commit()
        print(f"✅ Успешно удалено {bad_candles_count} свечей, начиная с {target_date.strftime('%Y-%m-%d')}.")
        print("Бот автоматически докачает этот период заново при следующем тике.")
    else:
        print("Нет свечей за этот период для удаления.")

finally:
    conn.close()