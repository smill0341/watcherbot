"""
find_all_peaks.py
==================
НЕЗАВИСИМЫЙ диагностический скрипт. НЕ использует VBottomWatcher и его
состояния (Ориентир/Старт/Пик/сбросы) вообще — просто качает историю
одной монеты с Binance и отмечает на графике КАЖДУЮ красную свечу, чей
объём >= ELEVATED_VOL_MULT * фон, где фон считается ТЕМ ЖЕ СПОСОБОМ,
что и в реальном коде (watcher_manager.py): скользящее среднее по
последним 52 свечам, с отступом в 2 свечи назад (df['volume'].iloc[-52:-2]).

Цель: увидеть на графике ВСЕ свечи-кандидаты на пик за весь период
разом, без какой-либо логики эскалации/сбросов/окон — чтобы вручную
сверить с тем, что видно глазами на графике (твои "круги").

Запуск:
    pip install requests pandas plotly --break-system-packages
    python find_all_peaks.py
"""

import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone

# ================= НАСТРОЙКИ =================
SYMBOL = "XPLUSDT"
INTERVAL = "15m"

LEVEL_MIN = 1700.00
LEVEL_MAX = 2136.0878

START_TIME_STR = "04.02.2026 16:00"
END_TIME_STR = "06.02.2026 01:00"

ELEVATED_VOL_MULT = 2.0   # тот же порог, что ELEVATED_VOL_MULT в v_bottom_watcher.py
BASELINE_WINDOW = 52      # тот же размер окна, что df['volume'].iloc[-52:-2] в watcher_manager.py
BASELINE_LAG = 2          # тот же отступ (последние 2 свечи не входят в расчёт фона)
# =============================================


def dt_to_ms(dt_str):
    dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_klines():
    """Bybit v5 public kline API (category=linear, USDT-перпетуалы).
    Отдаёт свечи от новых к старым — переворачиваем."""
    start_ms = dt_to_ms(START_TIME_STR)
    end_ms = dt_to_ms(END_TIME_STR)
    pad_ms = BASELINE_WINDOW * 15 * 60 * 1000 * 2

    interval_map = {"15m": "15", "1h": "60", "5m": "5", "1m": "1", "4h": "240", "1d": "D"}
    bybit_interval = interval_map.get(INTERVAL, "15")

    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "interval": bybit_interval,
        "start": start_ms - pad_ms,
        "end": end_ms,
        "limit": 1000,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error для {SYMBOL}: {data.get('retMsg')}")

    rows = list(reversed(data["result"]["list"]))  # от старых к новым
    # Формат строки Bybit: [start, open, high, low, close, volume, turnover]
    return [[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in rows]


print(f"📥 Выкачиваем {SYMBOL} {INTERVAL} с Bybit...")
raw_data = fetch_klines()

rows = []
for kline in raw_data:
    t_ms = kline[0]
    dt_obj = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    c_open, c_high, c_low, c_close, c_vol = map(float, kline[1:6])
    rows.append([t_ms, dt_obj, c_open, c_high, c_low, c_close, c_vol])

df = pd.DataFrame(rows, columns=['t_ms', 'Time', 'Open', 'High', 'Low', 'Close', 'Volume'])

# Фон ТЕМ ЖЕ способом, что в реальном коде: скользящее среднее за 52 свечи,
# с отступом в 2 свечи (не включает текущую и предыдущую)
df['baseline'] = df['Volume'].shift(BASELINE_LAG).rolling(BASELINE_WINDOW).mean()

start_ms = dt_to_ms(START_TIME_STR)
end_ms = dt_to_ms(END_TIME_STR)
window_df = df[(df['t_ms'] >= start_ms) & (df['t_ms'] <= end_ms)].copy()

# Кандидат на пик: красная свеча, объём >= фон * ELEVATED_VOL_MULT.
# НИКАКОЙ логики "первая/вторая/эскалация/сброс" — просто отмечаем ВСЕ такие свечи.
window_df['is_red'] = window_df['Close'] < window_df['Open']
window_df['is_candidate'] = window_df['is_red'] & (window_df['Volume'] >= window_df['baseline'] * ELEVATED_VOL_MULT)

candidates = window_df[window_df['is_candidate']]

print(f"\n📊 Найдено кандидатов на пик за период: {len(candidates)}\n")
for _, r in candidates.iterrows():
    ratio = r['Volume'] / r['baseline'] if r['baseline'] > 0 else float('nan')
    print(f"  {r['Time']:%Y-%m-%d %H:%M} | vol={r['Volume']:,.0f} | фон={r['baseline']:,.0f} | x{ratio:.2f}")

# --- ОТРИСОВКА ---
print("\n📊 Рисуем график...")
fig = go.Figure(data=[go.Candlestick(
    x=window_df['Time'], open=window_df['Open'], high=window_df['High'],
    low=window_df['Low'], close=window_df['Close'], name=SYMBOL
)])

fig.add_hline(y=LEVEL_MAX, line_dash="dash", line_color="orange", annotation_text="Уровень")

fig.add_trace(go.Scatter(
    x=candidates['Time'], y=candidates['Low'] * 0.995,
    mode='markers', marker=dict(color='red', size=12, symbol='diamond'),
    name='Кандидат на пик',
    text=[f"vol={v:,.0f}<br>фон={b:,.0f}<br>x{v/b:.2f}" for v, b in zip(candidates['Volume'], candidates['baseline'])]
))

fig.update_layout(title=f"Все кандидаты на пик — {SYMBOL} (без логики V_BOTTOM)",
                   xaxis_rangeslider_visible=False, template="plotly_dark")
fig.show()


# =====================================================================
# КОПИЯ ЛОГИКИ ЭСКАЛАЦИИ ИЗ v_bottom_watcher.py (те же CONFIG-значения),
# применённая к ТЕМ ЖЕ, уже подтверждённым Bybit-данным. Отдельно от
# симулятора (без уровней/origin/буфера) — проверяем, находит ли сама
# логика вход на этом участке.
# =====================================================================
ELEVATED_VOL_MULT_SIM = 2.0
PEAK_TOLERANCE_PCT_SIM = 90.0
VOL_MATCH_PCT_SIM = 110.0

print("\n" + "=" * 70)
print("СИМУЛЯЦИЯ ЛОГИКИ V_BOTTOM НА ЭТИХ ЖЕ ДАННЫХ")
print("=" * 70)

state = "SEARCHING"
tracker_vol = start_vol = cand_vol = 0.0

for _, r in window_df.iterrows():
    if pd.isna(r['baseline']):
        continue
    is_red = r['Close'] < r['Open']
    t = r['Time']
    vol = r['Volume']
    baseline = r['baseline']

    if state == "SEARCHING":
        if is_red and vol >= baseline * ELEVATED_VOL_MULT_SIM:
            tracker_vol = vol
            state = "WAIT_START"
            print(f"  {t:%Y-%m-%d %H:%M} Ориентир: vol={vol:,.0f} (фон={baseline:,.0f}, x{vol/baseline:.2f})")

    elif state == "WAIT_START":
        if is_red and vol > tracker_vol:
            start_vol = vol
            state = "WAIT_PEAK"
            print(f"  {t:%Y-%m-%d %H:%M} Старт: vol={vol:,.0f}")

    elif state == "WAIT_PEAK":
        if is_red and vol > start_vol:
            cand_vol = vol
            state = "WAIT_NEW_PEAK"
            print(f"  {t:%Y-%m-%d %H:%M} Пик(1, без проверки выкупа): vol={vol:,.0f}")

    elif state == "WAIT_GREEN":
        if not is_red:
            need = cand_vol * (VOL_MATCH_PCT_SIM / 100.0)
            if vol >= need:
                print(f"  {t:%Y-%m-%d %H:%M} ЗЕЛЁНАЯ vol={vol:,.0f} >= need={need:,.0f} -> ВХОД!")
                state = "TRIGGERED"
            else:
                print(f"  {t:%Y-%m-%d %H:%M} зелёная vol={vol:,.0f} < need={need:,.0f} -> мимо")
                state = "WAIT_NEW_PEAK"
        elif vol >= cand_vol * (PEAK_TOLERANCE_PCT_SIM / 100.0):
            if vol > cand_vol:
                cand_vol = vol
            print(f"  {t:%Y-%m-%d %H:%M} Пик+ (в WAIT_GREEN): vol={vol:,.0f}")
        else:
            print(f"  {t:%Y-%m-%d %H:%M} красная vol={vol:,.0f} не сильнее пика -> мимо")
            state = "WAIT_NEW_PEAK"

    elif state == "WAIT_NEW_PEAK":
        if is_red and vol >= cand_vol * (PEAK_TOLERANCE_PCT_SIM / 100.0):
            if vol > cand_vol:
                cand_vol = vol
            state = "WAIT_GREEN"
            print(f"  {t:%Y-%m-%d %H:%M} Пик+ (новый): vol={vol:,.0f}")

    if state == "TRIGGERED":
        break

if state != "TRIGGERED":
    print(f"\n❌ Вход НЕ найден. Финальное состояние: {state}, последний пик={cand_vol:,.0f}")
else:
    print("\n✅ Вход найден (см. выше).")