# modules/cryptano/levels/level_rating.py
"""
Оценка уровней — ОТДЕЛЬНЫЙ слой поверх готовых зон. levels_builder.py не трогает.

Для зоны [min, max] смотрит историю свечей ДО момента as_of и считает, как цена
вела себя на этой зоне раньше:
  bounces      — сколько раз цена зашла в зону и ушла от неё обратно (отбой)
  breaks       — сколько раз цена прошла зону насквозь (пробой)
  hold_pct     — отбои / (отбои + пробои) * 100, None если вердиктов ещё не было
  days_since_bounce — сколько дней прошло с последнего отбоя, None если отбоев не было

Вердикт по каждому заходу (ATR — дневной, значение на тот момент, без заглядывания вперёд):
  SHORT (сопротивление, цена приходит снизу):
      отбой  — закрытие ниже низа зоны на BOUNCE_ATR * ATR
      пробой — закрытие выше верха зоны на BREAK_ATR  * ATR
  LONG (поддержка, цена приходит сверху) — зеркально.
  Нет вердикта за LOOKAHEAD_DAYS — заход не считается.
Заход считается, только если вердикт получен ДО as_of (будущее не подсматриваем).

Два способа оценки (--method):
  ranked  (по умолчанию) — балл готового индикатора Ranked S/R Zones (level_methods.ranked_zones),
          посчитанный на 4h-свечах ровно на момент рождения ватчера (без заглядывания вперёд);
          берётся зона индикатора той же стороны, перекрывающая нашу зону (--match, доля перекрытия).
  rebound — отбои/пробои по истории (см. выше), результаты теста не показали зависимости.
  context — фаза рынка и подход к уровню на 4h, на момент рождения ватчера:
          структура (BOS/CHoCH по пивотам 5/5: тренд по сделке или против, давность слома)
          и рывок к уровню (насколько цена прошла в сторону уровня за 24ч и 72ч).

Запуск как тест по готовому отчёту симулятора (из D:\\bot\\master_bot, cmd, venv включён):
    python -m backend.modules.cryptano.levels.level_rating <путь\\last_all_run.json>
Необязательно: --method context / rebound, --match 0.5, --tf 4h, --bounce 1.0 --break 1.0 --look 7.
Файлов не пишет, только печатает таблицы.
"""
import os
import sys
import math
import argparse
from collections import defaultdict

import numpy as np
import pandas as pd

BOUNCE_ATR = 1.0       # на сколько ATR цена должна уйти от зоны, чтобы это был отбой
BREAK_ATR = 1.0        # на сколько ATR цена должна пройти зону насквозь, чтобы это был пробой
LOOKAHEAD_DAYS = 7     # сколько дней после захода ждём вердикта
MAX_LOOKBACK_DAYS = 365  # глубже года в историю не лезем
DEFAULT_LOOKBACK_DAYS = 180  # если даты уровня нет


def prepare_candles(df15):
    """df15 — 15m свечи (индекс UTC, колонки open/high/low/close/volume).
    Возвращает (df1h, atr_1h): часовые свечи и дневной ATR14 на каждый час.
    ATR берётся по ЗАВЕРШЁННЫМ дням (сдвиг на день) — без заглядывания вперёд."""
    h1 = df15.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()
    d1 = df15.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last"}).dropna()
    prev_close = d1["close"].shift(1)
    tr = pd.concat([d1["high"] - d1["low"],
                    (d1["high"] - prev_close).abs(),
                    (d1["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr_d = tr.rolling(14).mean().shift(1)          # последний завершённый день
    atr_1h = atr_d.reindex(h1.index, method="ffill")
    return h1, atr_1h


def rate_zone(h1, atr_1h, zmin, zmax, side, as_of, zone_date=None,
              bounce_atr=BOUNCE_ATR, break_atr=BREAK_ATR, look_days=LOOKAHEAD_DAYS):
    """side: 'SHORT' (сопротивление) или 'LONG' (поддержка). as_of — tz-aware Timestamp (UTC)."""
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    start = as_of - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if zone_date is not None:
        try:
            zd = pd.Timestamp(zone_date)
            if zd.tzinfo is None:
                zd = zd.tz_localize("UTC")
            start = zd
        except Exception:
            pass
    start = max(start, as_of - pd.Timedelta(days=MAX_LOOKBACK_DAYS))

    # только полностью закрытые к as_of часовые свечи
    mask = (h1.index >= start) & (h1.index + pd.Timedelta(hours=1) <= as_of)
    hh = h1[mask]
    if len(hh) < 3:
        return {"bounces": 0, "breaks": 0, "hold_pct": None, "days_since_bounce": None}
    hi = hh["high"].values
    lo = hh["low"].values
    cl = hh["close"].values
    atr = atr_1h[mask].values
    ts = hh.index
    n = len(hh)
    look = int(look_days * 24)

    bounces = breaks = 0
    last_bounce_i = None
    i = 1
    while i < n:
        if side == "SHORT":
            touch = hi[i] >= zmin and cl[i - 1] < zmin
        else:
            touch = lo[i] <= zmax and cl[i - 1] > zmax
        if not touch:
            i += 1
            continue
        verdict = None
        j = i
        end = min(n, i + look + 1)
        while j < end:
            a = atr[j]
            if a == a:  # не NaN
                if side == "SHORT":
                    if cl[j] >= zmax + break_atr * a:
                        verdict = "break"
                    elif cl[j] <= zmin - bounce_atr * a:
                        verdict = "bounce"
                else:
                    if cl[j] <= zmin - break_atr * a:
                        verdict = "break"
                    elif cl[j] >= zmax + bounce_atr * a:
                        verdict = "bounce"
            if verdict:
                break
            j += 1
        if verdict == "bounce":
            bounces += 1
            last_bounce_i = j
            i = j + 1
        elif verdict == "break":
            breaks += 1
            i = j + 1
        else:
            i += 1

    total = bounces + breaks
    days_since = None
    if last_bounce_i is not None:
        days_since = round((as_of - ts[last_bounce_i]).total_seconds() / 86400.0, 1)
    return {
        "bounces": bounces,
        "breaks": breaks,
        "hold_pct": round(bounces / total * 100.0, 1) if total else None,
        "days_since_bounce": days_since,
    }


# ----------------------------------------------------------------------------
# Метод ranked: балл индикатора Ranked S/R Zones на момент рождения ватчера
# ----------------------------------------------------------------------------
TF_SEC = {"1h": 3600, "4h": 14400, "1d": 86400}


def ranked_snapshots(candles, req, ranked_zones, tf_sec=14400):
    """Один проход ranked_zones по свечам монеты; на каждый момент из req (unix-сек, по возрастанию)
    запоминает зоны индикатора ПОСЛЕ последней завершённой свечи (close <= момента).
    Индикатор считает свеча за свечой, будущего не видит.
    Возвращает {момент: [(top, bottom, side_dir, score, touches, rank), ...] или None}."""
    out: dict = {r: None for r in req}
    n = len(candles)
    if n < 60 or not req:
        return out
    times = [int(c["time"]) for c in candles]
    state = {"k": 0}

    def cb(i, zones, atr):
        close_i = times[i] + tf_sec
        close_next = times[i + 1] + tf_sec if i + 1 < n else float("inf")
        k = state["k"]
        while k < len(req) and req[k] < close_next:
            if req[k] >= close_i:
                out[req[k]] = [(z.top, z.bottom, z.side_dir, z.score, z.touches, rank)
                               for rank, z in enumerate(zones, 1)]
            k += 1
        state["k"] = k

    ranked_zones(candles, on_bar=cb, use_all=True)
    return out


def match_ranked(snap, zmin, zmax, side, min_frac):
    """Зона индикатора той же стороны, перекрывающая нашу зону не меньше чем на min_frac
    от меньшей из двух ширин. Если таких несколько — с лучшим баллом.
    Возвращает (score, rank, touches) или None."""
    if not snap:
        return None
    want = 1 if side == "SHORT" else -1
    ow = max(zmax - zmin, 1e-12)
    best = None
    for top, bottom, sd, score, touches, rank in snap:
        if sd != want:
            continue
        ov = max(min(top, zmax) - max(bottom, zmin), 0.0)
        if ov <= 0:
            continue
        frac = ov / min(ow, max(top - bottom, 1e-12))
        if frac >= min_frac and (best is None or score > best[0]):
            best = (score, rank, touches)
    return best


# ----------------------------------------------------------------------------
# Метод context: структура рынка (BOS/CHoCH) и рывок к уровню, на момент рождения ватчера
# ----------------------------------------------------------------------------
STRUCT_SPAN = 5   # пивот: 5 свечей слева и справа (как в Ranked), подтверждается через 5 свечей


def context_snapshots(candles, req, is_pivot_high, is_pivot_low, tf_sec=14400, span=STRUCT_SPAN):
    """Один проход по свечам монеты. Структура:
      - пивот-хай/лоу (span/span) запоминается, когда подтверждён (через span свечей);
      - закрытие выше последнего непробитого пивот-хая = слом вверх, ниже пивот-лоу = слом вниз;
      - слом в сторону текущего тренда = BOS, против (или первый) = CHoCH.
    На каждый момент из req (unix-сек, по возрастанию) — состояние после последней
    завершённой свечи: trend (+1 вверх / -1 вниз / 0 нет), event ('BOS'/'CHoCH'/None),
    ev_age (свечей с последнего слома), chg24/chg72 (% изменения закрытия за 6/18 свечей)."""
    out: dict = {r: None for r in req}
    n = len(candles)
    if n < 2 * span + 20 or not req:
        return out
    t = [int(x["time"]) for x in candles]
    h = np.array([float(x["high"]) for x in candles])
    l = np.array([float(x["low"]) for x in candles])
    c = np.array([float(x["close"]) for x in candles])

    sh = sl = None
    sh_broken = sl_broken = True
    trend = 0
    last_ev = None
    last_ev_i = None
    k = 0
    for i in range(n):
        pp = i - span
        if pp >= span:
            if is_pivot_high(h, pp, span):
                sh, sh_broken = float(h[pp]), False
            if is_pivot_low(l, pp, span):
                sl, sl_broken = float(l[pp]), False
        if sh is not None and not sh_broken and c[i] > sh:
            last_ev = "BOS" if trend == 1 else "CHoCH"
            trend, sh_broken, last_ev_i = 1, True, i
        if sl is not None and not sl_broken and c[i] < sl:
            last_ev = "BOS" if trend == -1 else "CHoCH"
            trend, sl_broken, last_ev_i = -1, True, i

        close_i = t[i] + tf_sec
        close_next = t[i + 1] + tf_sec if i + 1 < n else float("inf")
        while k < len(req) and req[k] < close_next:
            if req[k] >= close_i:
                chg24 = (c[i] / c[i - 6] - 1.0) * 100.0 if i >= 6 else None
                chg72 = (c[i] / c[i - 18] - 1.0) * 100.0 if i >= 18 else None
                out[req[k]] = {
                    "trend": trend, "event": last_ev,
                    "ev_age": (i - last_ev_i) if last_ev_i is not None else None,
                    "chg24": chg24, "chg72": chg72,
                }
            k += 1
    return out


# ----------------------------------------------------------------------------
# Тест по готовому отчёту симулятора
# ----------------------------------------------------------------------------

def _load_df15(coin, markets, resolve_symbol):
    """15m-свечи монеты из АРХИВА симулятора (sim_candles.db) — всё, что
    симулятор уже докачал по этой монете. Сам ничего не качает."""
    import backend.modules.cryptano.levels.candle_archive as candle_archive
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        return None
    candles = candle_archive.get_candles(symbol, "15m")
    if not candles:
        return None
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop(columns=["time"]).set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _load_candles_tf(coin, tf, markets, candle_store, resolve_symbol):
    """Свечи в формате list[dict(time,open,high,low,close,volume)] для ranked_zones.
    Сначала из общей базы (candle_store); если там такого ТФ нет — собираем
    из 15m архива симулятора."""
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        return []
    try:
        cs = candle_store.get_candles(symbol, tf)
    except Exception:
        cs = None
    if cs:
        return cs
    df15 = _load_df15(coin, markets, resolve_symbol)
    if df15 is None:
        return []
    rule = {"1h": "1h", "4h": "4h", "1d": "1D"}.get(tf, "4h")
    d = df15.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                 "close": "last", "volume": "sum"}).dropna()
    return [{"time": int(ts.timestamp()), "open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume} for ts, r in d.iterrows()]


def _find_episode(history_by_coin, coin, level_id, trade_date):
    """Эпизод ватчера (из history_by_coin) для сделки — даёт born_at и level_date."""
    best = None
    for ep in history_by_coin.get(coin, []):
        if ep.get("level_id") != level_id:
            continue
        b = ep.get("born_at")
        if not b:
            continue
        if best is None or (str(b) <= trade_date.replace(" ", "T") and str(b) > str(best.get("born_at"))):
            best = ep
    return best


def _stats(rows):
    closed = [r for r in rows if r["status"] in ("✅", "🕐")]
    if not closed:
        return None
    tp = sum(1 for r in closed if r["status"] == "✅") / len(closed) * 100.0
    maes = sorted(r["mae_pct"] for r in closed if r.get("mae_pct") is not None)
    mae = maes[len(maes) // 2] if maes else float("nan")
    return len(closed), tp, mae


def _print_group(title, rows, groups):
    print(f"\n  {title}")
    print(f"    {'группа':<14}{'сделок':>8}{'TP%':>7}{'просадка(мед)':>16}")
    for label, fn in groups:
        g = [r for r in rows if fn(r)]
        s = _stats(g)
        if s:
            print(f"    {label:<14}{s[0]:>8}{s[1]:>7.0f}{s[2]:>15.1f}%")


def _born_ts(ep, trade):
    """Момент рождения ватчера как tz-aware UTC Timestamp (если нет — время сделки)."""
    b = (ep or {}).get("born_at") or trade["date"].replace(" ", "T")
    ts = pd.Timestamp(b)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _report_rebound(rated):
    for side in ("LONG", "SHORT"):
        rows = [r for r in rated if r["type"] == side]
        s = _stats(rows)
        if not s:
            continue
        print(f"\n===== {side} =====")
        print(f"  всего закрытых: {s[0]}, TP {s[1]:.0f}%, просадка(мед) {s[2]:.1f}%")
        _print_group("по числу отбоев", rows, [
            ("0", lambda r: r["bounces"] == 0), ("1", lambda r: r["bounces"] == 1),
            ("2", lambda r: r["bounces"] == 2), ("3+", lambda r: r["bounces"] >= 3)])
        _print_group("по пробоям", rows, [
            ("0", lambda r: r["breaks"] == 0), ("1", lambda r: r["breaks"] == 1),
            ("2+", lambda r: r["breaks"] >= 2)])
        _print_group("по удержанию", rows, [
            ("нет истории", lambda r: r["hold_pct"] is None),
            ("<50%", lambda r: r["hold_pct"] is not None and r["hold_pct"] < 50),
            ("50-74%", lambda r: r["hold_pct"] is not None and 50 <= r["hold_pct"] < 75),
            ("75%+", lambda r: r["hold_pct"] is not None and r["hold_pct"] >= 75)])
        _print_group("давность отбоя", rows, [
            ("нет отбоев", lambda r: r["days_since_bounce"] is None),
            ("до 7 дн", lambda r: r["days_since_bounce"] is not None and r["days_since_bounce"] <= 7),
            ("7-30 дн", lambda r: r["days_since_bounce"] is not None and 7 < r["days_since_bounce"] <= 30),
            ("30+ дн", lambda r: r["days_since_bounce"] is not None and r["days_since_bounce"] > 30)])


def _quantiles(vals):
    v = sorted(vals)
    if not v:
        return None
    q = lambda p: v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))]
    return q(0), q(0.25), q(0.5), q(0.75), q(1)


def _report_ranked(rated):
    for side in ("LONG", "SHORT"):
        rows = [r for r in rated if r["type"] == side]
        s = _stats(rows)
        if not s:
            continue
        base_n = s[0]
        print(f"\n===== {side} =====")
        print(f"  всего закрытых: {s[0]}, TP {s[1]:.0f}%, просадка(мед) {s[2]:.1f}%")
        matched = [r for r in rows if r["rk_score"] is not None]
        print(f"  совпало с зоной Ranked: {len(matched)} из {len(rows)} ({len(matched) / len(rows) * 100:.0f}%)")
        q = _quantiles([r["rk_score"] for r in matched])
        if q:
            print(f"  балл совпавших: мин {q[0]:.0f} / 25% {q[1]:.0f} / медиана {q[2]:.0f} / 75% {q[3]:.0f} / макс {q[4]:.0f}")
        sc = lambda r: r["rk_score"]
        _print_group("по баллу Ranked", rows, [
            ("нет совпадения", lambda r: sc(r) is None),
            ("до 35", lambda r: sc(r) is not None and sc(r) < 35),
            ("35-50", lambda r: sc(r) is not None and 35 <= sc(r) < 50),
            ("50-65", lambda r: sc(r) is not None and 50 <= sc(r) < 65),
            ("65+", lambda r: sc(r) is not None and sc(r) >= 65)])
        _print_group("по рангу зоны (1 = лучшая)", rows, [
            ("ранг 1-3", lambda r: r["rk_rank"] is not None and r["rk_rank"] <= 3),
            ("ранг 4-10", lambda r: r["rk_rank"] is not None and 4 <= r["rk_rank"] <= 10),
            ("ранг 11+", lambda r: r["rk_rank"] is not None and r["rk_rank"] > 10)])
        print("\n  Что останется, если брать только зоны с баллом Ranked >= S")
        print(f"    {'S':>4}{'осталось':>10}{'доля':>7}{'TP%':>7}{'просадка(мед)':>16}")
        for S in (0, 35, 45, 55, 65):
            g = [r for r in rows if r["rk_score"] is not None and r["rk_score"] >= S]
            st = _stats(g)
            if st:
                print(f"    {S:>4}{st[0]:>10}{st[0] / base_n * 100:>6.0f}%{st[1]:>7.0f}{st[2]:>15.1f}%")


def _report_context(rated, tf_label):
    for side in ("LONG", "SHORT"):
        rows = [r for r in rated if r["type"] == side]
        s = _stats(rows)
        if not s:
            continue
        print(f"\n===== {side} =====")
        print(f"  всего закрытых: {s[0]}, TP {s[1]:.0f}%, просадка(мед) {s[2]:.1f}%")
        want = -1 if side == "SHORT" else 1   # тренд "по сделке": для шорта вниз, для лонга вверх
        tr = lambda r: r["st_trend"]
        ev = lambda r: r["st_event"]
        _print_group(f"структура {tf_label} на момент рождения", rows, [
            ("против, BOS", lambda r: tr(r) == -want and ev(r) == "BOS"),
            ("против, CHoCH", lambda r: tr(r) == -want and ev(r) == "CHoCH"),
            ("по сделке,BOS", lambda r: tr(r) == want and ev(r) == "BOS"),
            ("по сделке,CHoCH", lambda r: tr(r) == want and ev(r) == "CHoCH"),
            ("нет данных", lambda r: tr(r) in (None, 0))])
        print("    (против = тренд против сделки: для шорта рынок ломает пики вверх; по сделке = наоборот)")
        ag = lambda r: r["st_age"]
        _print_group("давность последнего слома", rows, [
            ("до 1 дня", lambda r: ag(r) is not None and ag(r) <= 6),
            ("1-5 дней", lambda r: ag(r) is not None and 6 < ag(r) <= 30),
            ("5+ дней", lambda r: ag(r) is not None and ag(r) > 30)])
        i24 = lambda r: r["imp24"]
        _print_group("рывок к уровню за 24ч", rows, [
            ("от уровня", lambda r: i24(r) is not None and i24(r) < 0),
            ("0-3%", lambda r: i24(r) is not None and 0 <= i24(r) < 3),
            ("3-7%", lambda r: i24(r) is not None and 3 <= i24(r) < 7),
            ("7-15%", lambda r: i24(r) is not None and 7 <= i24(r) < 15),
            ("15%+", lambda r: i24(r) is not None and i24(r) >= 15)])
        i72 = lambda r: r["imp72"]
        _print_group("рывок к уровню за 72ч", rows, [
            ("от уровня", lambda r: i72(r) is not None and i72(r) < 0),
            ("0-5%", lambda r: i72(r) is not None and 0 <= i72(r) < 5),
            ("5-10%", lambda r: i72(r) is not None and 5 <= i72(r) < 10),
            ("10-20%", lambda r: i72(r) is not None and 10 <= i72(r) < 20),
            ("20%+", lambda r: i72(r) is not None and i72(r) >= 20)])
        print("    (рывок к уровню: для шорта — рост цены, для лонга — падение, в %)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="путь к last_all_run.json")
    ap.add_argument("--method", choices=("ranked", "rebound", "context"), default="ranked")
    ap.add_argument("--tf", default="4h", help="таймфрейм свечей для ranked (1h/4h/1d)")
    ap.add_argument("--match", type=float, default=0.5, help="мин. доля перекрытия зон для совпадения")
    ap.add_argument("--bounce", type=float, default=BOUNCE_ATR)
    ap.add_argument("--break", dest="brk", type=float, default=BREAK_ATR)
    ap.add_argument("--look", type=float, default=LOOKAHEAD_DAYS)
    args = ap.parse_args()

    if not os.path.isfile(args.report):
        print(f"ОШИБКА: файл не найден: {args.report}")
        sys.exit(1)

    from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, load_markets_cached
    from backend.modules.cryptano.utils.storage import load_json
    import backend.modules.cryptano.levels.candle_store as candle_store

    data = load_json(args.report, default={})
    trades = data.get("trades", [])
    hist = data.get("history_by_coin", {})
    if not trades:
        print("ОШИБКА: в файле нет сделок (проверь, что это last_all_run.json от bulk-прогона).")
        sys.exit(1)
    markets = load_markets_cached(exchange)

    ranked_zones = None
    piv_hi = piv_lo = None
    if args.method == "context":
        try:
            from backend.modules.cryptano.levels.level_methods import _is_pivot_high as piv_hi, _is_pivot_low as piv_lo
        except ImportError as e:
            print(f"ОШИБКА: не нашёл level_methods ({e}). Положи level_rating.py в ту же папку, где level_methods.py.")
            sys.exit(1)
        print(f"Сделок в отчёте: {len(trades)}. Метод: context (структура BOS/CHoCH и рывок), свечи {args.tf}.")
    elif args.method == "ranked":
        try:
            from backend.modules.cryptano.levels.level_methods import ranked_zones
        except ImportError as e:
            print(f"ОШИБКА: не нашёл level_methods.ranked_zones ({e}). Положи level_rating.py в ту же папку, где level_methods.py.")
            sys.exit(1)
        print(f"Сделок в отчёте: {len(trades)}. Метод: ranked, свечи {args.tf}, совпадение зон от {args.match:g} перекрытия.")
    else:
        print(f"Сделок в отчёте: {len(trades)}. Метод: rebound, отбой {args.bounce} ATR, пробой {args.brk} ATR, ждём {args.look:g} дн.")

    by_coin = defaultdict(list)
    for idx, t in enumerate(trades):
        by_coin[t["coin"]].append(idx)

    rated = []
    skipped = 0
    for coin, idxs in by_coin.items():
        if args.method == "context":
            cs = _load_candles_tf(coin, args.tf, markets, candle_store, resolve_symbol)
            if not cs:
                skipped += len(idxs)
                continue
            infos = []
            for idx in idxs:
                t = trades[idx]
                ep = _find_episode(hist, coin, t.get("level_id"), t["date"])
                infos.append((idx, int(_born_ts(ep, t).timestamp())))
            req = sorted(set(a for _, a in infos))
            snaps = context_snapshots(cs, req, piv_hi, piv_lo, tf_sec=TF_SEC.get(args.tf, 14400))
            for idx, a in infos:
                t = trades[idx]
                st = snaps.get(a) or {}
                sign = 1.0 if t["type"] == "SHORT" else -1.0   # "к уровню": шорт — рост, лонг — падение
                rr = dict(t)
                rr["st_trend"] = st.get("trend")
                rr["st_event"] = st.get("event")
                rr["st_age"] = st.get("ev_age")
                rr["imp24"] = sign * st["chg24"] if st.get("chg24") is not None else None
                rr["imp72"] = sign * st["chg72"] if st.get("chg72") is not None else None
                rated.append(rr)
        elif args.method == "ranked":
            cs = _load_candles_tf(coin, args.tf, markets, candle_store, resolve_symbol)
            if not cs:
                skipped += len(idxs)
                continue
            tf_sec = TF_SEC.get(args.tf, 14400)
            infos = []
            for idx in idxs:
                t = trades[idx]
                ep = _find_episode(hist, coin, t.get("level_id"), t["date"])
                infos.append((idx, int(_born_ts(ep, t).timestamp())))
            req = sorted(set(a for _, a in infos))
            snaps = ranked_snapshots(cs, req, ranked_zones, tf_sec=tf_sec)
            for idx, a in infos:
                t = trades[idx]
                m = match_ranked(snaps.get(a), t["level_min"], t["level_max"], t["type"], args.match)
                rr = dict(t)
                rr["rk_score"], rr["rk_rank"], rr["rk_touches"] = (m if m else (None, None, None))
                rated.append(rr)
        else:
            df15 = _load_df15(coin, markets, resolve_symbol)
            if df15 is None:
                skipped += len(idxs)
                continue
            prep = prepare_candles(df15)
            for idx in idxs:
                t = trades[idx]
                ep = _find_episode(hist, coin, t.get("level_id"), t["date"])
                r = rate_zone(prep[0], prep[1], t["level_min"], t["level_max"], t["type"],
                              _born_ts(ep, t), zone_date=(ep or {}).get("level_date"),
                              bounce_atr=args.bounce, break_atr=args.brk, look_days=args.look)
                rr = dict(t)
                rr.update(r)
                rated.append(rr)
    print(f"Оценено: {len(rated)}, пропущено (нет свечей): {skipped}")

    if args.method == "context":
        _report_context(rated, args.tf)
    elif args.method == "ranked":
        _report_ranked(rated)
    else:
        _report_rebound(rated)


if __name__ == "__main__":
    main()