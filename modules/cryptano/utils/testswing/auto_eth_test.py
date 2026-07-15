import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
from v_bottom_watcher import VBottomWatcher

# ================= НАСТРОЙКИ =================
SYMBOL = "ETHUSDT"
INTERVAL = "15m"
LEVEL_MIN = 1700.00
LEVEL_MAX = 2136.0878

START_TIME_STR = "04.02.2026 16:00"
END_TIME_STR = "06.02.2026 01:00"
# =============================================

def dt_to_ms(dt_str):
    dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

def fetch_klines():
    start_ms = dt_to_ms(START_TIME_STR)
    end_ms = dt_to_ms(END_TIME_STR)
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&startTime={start_ms - (86400000)}&endTime={end_ms}&limit=1000"
    return requests.get(url).json()

print(f"📥 Выкачиваем {SYMBOL} {INTERVAL} с Binance...")
raw_data = fetch_klines()

watcher = VBottomWatcher(LEVEL_MIN, LEVEL_MAX, 'LONG')

volumes = []
last_state = "SEARCHING"
last_peak_vol = 0.0

# Списки для графика
df_data = []
markers = {
    'Ориентир': {'x': [], 'y': [], 'text': []},
    'Старт': {'x': [], 'y': [], 'text': []},
    'Пик': {'x': [], 'y': [], 'text': []},
    'Отмена': {'x': [], 'y': [], 'text': []},
    'Вход': {'x': [], 'y': [], 'text': []}
}

for kline in raw_data:
    t_ms = kline[0]
    dt_obj = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    
    c_open, c_high, c_low, c_close, c_vol = map(float, kline[1:6])
    df_data.append([dt_obj, c_open, c_high, c_low, c_close, c_vol])
    
    volumes.append(c_vol)
    if len(volumes) > 20: volumes.pop(0)
    baseline_vol = sum(volumes) / len(volumes) if len(volumes) == 20 else 0.0
    
    if t_ms < dt_to_ms(START_TIME_STR):
        continue

    # Запоминаем статус ДО обновления
    pre_state = watcher.state

    result = watcher.update(
        c_open, c_high, c_low, c_close, c_vol, baseline_vol, 
        10.0, [], 'UNKNOWN', None
    )

    # Проверяем, что изменилось
    if watcher.state != last_state or (watcher.state in ["WAIT_GREEN", "WAIT_NEW_PEAK"] and watcher.cand_vol != last_peak_vol):
        
        # Точка ставится чуть ниже Low свечи
        dot_y = c_low - 2.0 
        vol_text = f"Объем: {watcher._fmt(c_vol)}"

        if watcher.state == "WAIT_START" and pre_state == "SEARCHING":
            markers['Ориентир']['x'].append(dt_obj)
            markers['Ориентир']['y'].append(dot_y)
            markers['Ориентир']['text'].append(f"Ориентир<br>{vol_text}")
            
        elif watcher.state == "WAIT_PEAK" and pre_state == "WAIT_START":
            markers['Старт']['x'].append(dt_obj)
            markers['Старт']['y'].append(dot_y)
            markers['Старт']['text'].append(f"Старт<br>{vol_text}")
            
        elif watcher.state in ["WAIT_GREEN", "WAIT_NEW_PEAK"]:
            markers['Пик']['x'].append(dt_obj)
            markers['Пик']['y'].append(dot_y)
            markers['Пик']['text'].append(f"Пик: {watcher._fmt(watcher.cand_vol)}<br>{vol_text}")
            
        elif watcher.state == "SEARCHING" and pre_state != "SEARCHING":
            markers['Отмена']['x'].append(dt_obj)
            markers['Отмена']['y'].append(c_high + 2.0) # Отмену рисуем над свечой
            markers['Отмена']['text'].append(f"Сброс (выше уровня)")

        last_state = watcher.state
        last_peak_vol = watcher.cand_vol

    if result:
        markers['Вход']['x'].append(dt_obj)
        markers['Вход']['y'].append(c_low - 5.0)
        markers['Вход']['text'].append("🚀 ВХОД!")
        print(f"🔥 СДЕЛКА НАЙДЕНА: {result}")
        break

# --- ОТРИСОВКА ГРАФИКА ---
print("📊 Рисуем график...")
df = pd.DataFrame(df_data, columns=['Time', 'Open', 'High', 'Low', 'Close', 'Volume'])

fig = go.Figure(data=[go.Candlestick(x=df['Time'], open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'], name="ETHUSDT")])

# Добавляем уровень отмены
fig.add_hline(y=LEVEL_MAX, line_dash="dash", line_color="orange", annotation_text="Уровень Сброса")

# Добавляем точки
fig.add_trace(go.Scatter(x=markers['Ориентир']['x'], y=markers['Ориентир']['y'], mode='markers', marker=dict(color='blue', size=10), name='Ориентир', text=markers['Ориентир']['text']))
fig.add_trace(go.Scatter(x=markers['Старт']['x'], y=markers['Старт']['y'], mode='markers', marker=dict(color='yellow', size=10), name='Старт', text=markers['Старт']['text']))
fig.add_trace(go.Scatter(x=markers['Пик']['x'], y=markers['Пик']['y'], mode='markers', marker=dict(color='red', size=12, symbol='diamond'), name='Пик', text=markers['Пик']['text']))
fig.add_trace(go.Scatter(x=markers['Отмена']['x'], y=markers['Отмена']['y'], mode='markers', marker=dict(color='black', size=12, symbol='x'), name='Отмена', text=markers['Отмена']['text']))
fig.add_trace(go.Scatter(x=markers['Вход']['x'], y=markers['Вход']['y'], mode='markers', marker=dict(color='green', size=15, symbol='triangle-up'), name='ВХОД', text=markers['Вход']['text']))

fig.update_layout(title="Отладка V-Дна", xaxis_rangeslider_visible=False, template="plotly_dark")
fig.show()