"""
levels_audit.py
================
Аудит истории уровней симулятора (levels_timeline_YYYY_MM.json).
ТОЛЬКО ЧИТАЕТ: ничего не пишет ни в database/, ни в jsonbank/, бот не трогает.

Запуск из корня master_bot/:
    python levels_audit.py 2026_08
    python levels_audit.py 2026_08 2026_09      (несколько месяцев подряд)

Что делает по каждой зоне на каждом снимке:
  - ширина в % цены и в дневных ATR;
  - якорь (из level_id / первый уровень в type), сколько уровней склеено, класс;
  - сторона: поддержка ниже цены, сопротивление выше (по реальной цене на момент снимка);
  - "на пике": поддержка, чей верх доходит до локального хая своего периода (и зеркально);
  - для толстых зон (первое появление каждого level_id) — пересобирает сырые уровни
    levels_builder.py на ту же дату и показывает, каким был якорь ДО сжатия и какой у
    него ATR (для MACRO — ATR месячной/недельной свечи).

Результат:
  levels_audit_YYYY_MM.csv  — одна строка на зону и снимок (разделитель ';', Excel открывает сразу)
  сводка в консоли.
"""
import os
import sys
import csv
import json
import datetime
from collections import Counter, defaultdict

import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, KNOWN_TICKER_ALIASES
from backend.modules.cryptano.utils.paths import DATABASE_DIR
from backend.modules.cryptano.levels import levels_builder as lb
from backend.modules.cryptano.levels.swing_hunter import _safe_fetch_ohlcv

# ---------------- ПОРОГИ ----------------
FAT_ATR = 1.5        # толстая зона: шире стольких дневных ATR ...
FAT_PCT = 8.0        # ... или шире стольких % от цены
PEAK_TOL_ATR = 0.25  # "на пике": край зоны ближе стольких ATR к локальному экстремуму
TOP_N = 20           # сколько самых широких зон печатать в сводке

# Те же хвосты, что precalc_for_bot.py::build_snapshot_for_date — чтобы видеть ровно то, что видел builder
TAILS = {"1M": 60, "1W": 150, "1d": 365, "4h": 200}
# Сколько свечей тянем из базы (с запасом под старые месяцы)
FETCH = {"1M": 60, "1W": 150, "1d": 1000, "4h": 3000}
COLS = ["timestamp", "open", "high", "low", "close", "volume"]
H4_MS = 4 * 3600 * 1000


def _num(x, nd=2):
    """Число для CSV с запятой (русский Excel)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return f"{x:.{nd}f}".replace(".", ",")


def _snapshot_ms(time_str):
    # РОВНО как precalc_for_bot.py: strptime(...).timestamp() — локальное время машины
    return int(datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


def _slice(df, ts_ms, tail_n):
    if df is None:
        return None
    s = df[df['timestamp'] <= ts_ms]
    if s.empty:
        return None
    return s.tail(tail_n).reset_index(drop=True)


def _load_candles(coin, markets):
    symbol = resolve_symbol(coin, markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), markets)
    if not symbol:
        return None
    out = {}
    for tf, n in FETCH.items():
        rows = _safe_fetch_ohlcv(symbol, tf, n)
        out[tf] = pd.DataFrame(rows, columns=COLS) if rows else None
    return out


def _anchor_and_date(z):
    lid = z.get('level_id') or ''
    parts = lid.split('|')
    if len(parts) == 3:
        return parts[1], parts[2]
    return lb._anchor_type(z), z.get('date')


def _type_parts(z):
    return [p.strip() for p in str(z.get('type') or '').split('+') if p.strip()]


def _anchor_tf(anchor):
    for p in ("1M", "1W", "1d", "4h"):
        if anchor.startswith(p):
            return p
    return "?"


def _local_extreme(df_1d, date_str, anchor, is_support):
    """Экстремум дневок в окне формирования зоны: месяц для 1M, неделя для 1W, иначе ±3 дня."""
    if df_1d is None or not date_str:
        return None
    try:
        d0 = pd.Timestamp(str(date_str)[:10])
    except Exception:
        return None
    tf = _anchor_tf(anchor)
    if tf == "1M" and "MACRO" in anchor:
        a, b = d0, d0 + pd.Timedelta(days=31)
    elif tf == "1W" and "MACRO" in anchor:
        a, b = d0, d0 + pd.Timedelta(days=7)
    else:
        a, b = d0 - pd.Timedelta(days=3), d0 + pd.Timedelta(days=3)
    dt = pd.to_datetime(df_1d['timestamp'], unit='ms')
    w = df_1d[(dt >= a) & (dt <= b)]
    if w.empty:
        return None
    # для поддержки опасен ХАЙ внутри зоны, для сопротивления — ЛОЙ
    return float(w['high'].max()) if is_support else float(w['low'].min())


def _macro_own_atr(df_tf, prefix, date_str):
    """ATR MACRO-свечи ровно так, как levels_builder._extract_macro_swings: TR rolling(14, min_periods=1) / sqrt(дней)."""
    if df_tf is None or not date_str:
        return None
    scale = lb.MACRO_ATR_DAYS.get(prefix, 1.0) ** 0.5
    h, l, c = df_tf['high'], df_tf['low'], df_tf['close']
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    own = (tr.rolling(14, min_periods=1).mean() / scale)
    ds = pd.to_datetime(df_tf['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    idx = list(ds[ds == str(date_str)[:10]].index)
    if not idx:
        return None
    return float(own.iloc[idx[0]])


def _raw_anchor(sl, anchor, date_str, is_support, final_z):
    """Пересобираем сырые уровни на эту дату и ищем исходную зону якоря (до слияния/сжатия)."""
    df_1d = sl["1d"]
    idx = len(df_1d) - 1
    price = float(df_1d['close'].iloc[idx])
    atr_1d = lb.calculate_atr(df_1d, 14).iloc[idx]
    if pd.isna(atr_1d) or atr_1d == 0:
        atr_1d = price * 0.05
    max_dist = lb._calc_weekly_atr(df_1d, idx) * lb.ATR_DISTANCE_MULTIPLIER
    f_mid = (final_z['min'] + final_z['max']) / 2.0
    for deep, dist in ((False, max_dist), (True, float('inf'))):
        raw = lb._collect_zones(sl["1M"], sl["1W"], df_1d, sl["4h"], price, atr_1d, dist, idx,
                                deep=deep, count_stats=False)
        cands = [z for z in raw
                 if not z.get('mitigated') and z.get('_is_support') is is_support
                 and lb._anchor_type(z) == anchor and str(z.get('date')) == str(date_str)]
        if cands:
            best = min(cands, key=lambda z: abs((z['min'] + z['max']) / 2.0 - f_mid))
            return best, deep
    return None, None


def audit_month(month_label, rows_out, cache, markets):
    path = os.path.join(DATABASE_DIR, f"levels_timeline_{month_label}.json")
    if not os.path.exists(path):
        print(f"⚠️ Нет файла {path}")
        return
    with open(path, 'r') as f:
        timeline = json.load(f)
    print(f"📂 {path}: снимков {len(timeline)}")

    explained = {}  # (coin, side, level_id) -> dict с причиной (считаем один раз)

    for time_str in sorted(timeline.keys()):
        snap = timeline[time_str] or {}
        ts_ms = _snapshot_ms(time_str)
        for coin, entry in snap.items():
            if coin == "_meta" or not isinstance(entry, dict):
                continue
            if coin not in cache:
                cache[coin] = _load_candles(coin, markets)
            c = cache[coin]
            if not c or c.get("1d") is None:
                continue
            sl = {tf: _slice(c[tf], ts_ms, TAILS[tf]) for tf in TAILS}
            if sl["1d"] is None or len(sl["1d"]) < 50:
                continue
            idx = len(sl["1d"]) - 1
            price_builder = float(sl["1d"]['close'].iloc[idx])
            atr_1d = lb.calculate_atr(sl["1d"], 14).iloc[idx]
            if pd.isna(atr_1d) or atr_1d == 0:
                atr_1d = price_builder * 0.05
            atr_1d = float(atr_1d)
            # Реальная цена: закрытие последней ЗАКРЫТОЙ 4h-свечи к моменту снимка
            price_real = price_builder
            if c.get("4h") is not None:
                closed = c["4h"][c["4h"]['timestamp'] + H4_MS <= ts_ms]
                if not closed.empty:
                    price_real = float(closed['close'].iloc[-1])

            for side_key, is_sup in (("supports", True), ("resistances", False)):
                for z in entry.get(side_key) or []:
                    zmin, zmax = float(z['min']), float(z['max'])
                    mid = (zmin + zmax) / 2.0
                    width = zmax - zmin
                    w_pct = width / mid * 100.0 if mid else 0.0
                    w_atr = width / atr_1d if atr_1d else 0.0
                    anchor, adate = _anchor_and_date(z)
                    parts = _type_parts(z)
                    is_macro = z.get('class') == 'MACRO' or 'MACRO' in anchor

                    flags = []
                    fat = w_atr > FAT_ATR or w_pct > FAT_PCT
                    if fat:
                        flags.append("FAT")
                    if (is_sup and zmin > price_real) or ((not is_sup) and zmax < price_real):
                        flags.append("WRONG_SIDE")
                    elif zmin <= price_real <= zmax:
                        flags.append("PRICE_INSIDE")
                    ext = _local_extreme(c["1d"], adate, anchor, is_sup)
                    if ext is not None:
                        if is_sup and zmax >= ext - PEAK_TOL_ATR * atr_1d and ext > zmin:
                            flags.append("AT_PEAK")
                        if (not is_sup) and zmin <= ext + PEAK_TOL_ATR * atr_1d and ext < zmax:
                            flags.append("AT_BOTTOM")

                    # Причина — только для толстых/подозрительных, один раз на level_id
                    key = (coin, side_key, z.get('level_id') or f"{anchor}|{adate}|{zmin:.8g}")
                    reason = explained.get(key)
                    if reason is None and (fat or "AT_PEAK" in flags or "AT_BOTTOM" in flags or "WRONG_SIDE" in flags):
                        reason = {"raw_w_pct": None, "raw_w_atr": None, "own_atr_pct": None,
                                  "src": "", "text": ""}
                        try:
                            raw, deep = _raw_anchor(sl, anchor, adate, is_sup, z)
                        except Exception as e:
                            raw, deep = None, None
                            reason["text"] = f"ошибка пересборки: {e}"
                        if raw is not None:
                            rw = float(raw['max'] - raw['min'])
                            rmid = (raw['max'] + raw['min']) / 2.0
                            reason["raw_w_pct"] = rw / rmid * 100.0 if rmid else None
                            reason["raw_w_atr"] = rw / atr_1d if atr_1d else None
                            reason["src"] = "фолбэк (вся история)" if deep else "обычный"
                            txt = []
                            if "MACRO" in anchor:
                                own = _macro_own_atr(sl.get(anchor[:2]), anchor[:2], adate)
                                if own:
                                    reason["own_atr_pct"] = own / rmid * 100.0
                                    txt.append(f"ATR {anchor[:2]}-свечи={reason['own_atr_pct']:.1f}% цены "
                                               f"(={own / atr_1d:.1f} дн.ATR)")
                            if rw > width * 1.02:
                                txt.append(f"сжата {reason['raw_w_pct']:.1f}%→{w_pct:.1f}%")
                            elif abs(rw - width) <= width * 0.02:
                                txt.append("границы = сырой якорь")
                            else:
                                txt.append(f"сырой якорь {reason['raw_w_pct']:.1f}%, итог {w_pct:.1f}%")
                            if len(parts) > 1:
                                txt.append(f"склеено уровней: {len(parts)}")
                            reason["text"] = "; ".join(txt)
                        elif not reason["text"]:
                            reason["text"] = "якорь не найден при пересборке"
                        explained[key] = reason

                    rows_out.append({
                        "month": month_label, "snapshot": time_str, "coin": coin,
                        "side": "S" if is_sup else "R", "level_id": z.get('level_id', ''),
                        "anchor": anchor, "anchor_tf": _anchor_tf(anchor), "date": adate,
                        "class": z.get('class', ''), "parts": len(parts), "type": z.get('type', ''),
                        "min": zmin, "max": zmax, "width_pct": w_pct, "width_atr": w_atr,
                        "atr_1d_pct": atr_1d / price_real * 100.0 if price_real else None,
                        "price_builder": price_builder, "price_real": price_real,
                        "score": z.get('score'), "touches": z.get('reaction_count'),
                        "flags": ",".join(flags), "is_macro": is_macro,
                        "raw_w_pct": (reason or {}).get("raw_w_pct"),
                        "raw_w_atr": (reason or {}).get("raw_w_atr"),
                        "own_atr_pct": (reason or {}).get("own_atr_pct"),
                        "src": (reason or {}).get("src", ""),
                        "reason": (reason or {}).get("text", ""),
                    })


def write_csv(rows, path):
    fields = ["month", "snapshot", "coin", "side", "anchor", "anchor_tf", "date", "class", "parts",
              "min", "max", "width_pct", "width_atr", "atr_1d_pct", "price_builder", "price_real",
              "score", "touches", "flags", "raw_w_pct", "raw_w_atr", "own_atr_pct", "src", "reason",
              "level_id", "type"]
    nd = {"min": 8, "max": 8, "price_builder": 8, "price_real": 8}
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(fields)
        for r in rows:
            out = []
            for k in fields:
                v = r.get(k)
                if isinstance(v, bool):
                    out.append("1" if v else "0")
                elif isinstance(v, float):
                    out.append(_num(v, nd.get(k, 2)))
                else:
                    out.append("" if v is None else v)
            w.writerow(out)


def summary(rows):
    if not rows:
        print("Пусто — нечего анализировать.")
        return
    # Уникальные зоны: (coin, side, level_id) — по первому появлению
    uniq = {}
    for r in rows:
        k = (r["coin"], r["side"], r["level_id"] or f"{r['anchor']}|{r['date']}|{r['min']}")
        uniq.setdefault(k, r)
    u = list(uniq.values())
    n = len(u)

    def cnt(flag):
        return sum(1 for r in u if flag in r["flags"].split(","))

    print("\n================ СВОДКА ================")
    print(f"Строк (зона×снимок): {len(rows)} | уникальных зон: {n} | монет: {len({r['coin'] for r in u})}")
    for fl, title in (("FAT", f"толстые (>{FAT_ATR} ATR или >{FAT_PCT}%)"),
                      ("WRONG_SIDE", "не та сторона (поддержка выше цены / сопротивление ниже)"),
                      ("AT_PEAK", "поддержка на пике"),
                      ("AT_BOTTOM", "сопротивление на дне"),
                      ("PRICE_INSIDE", "цена внутри зоны на снимке (норма, для справки)")):
        k = cnt(fl)
        print(f"  {title}: {k} ({k / n * 100:.0f}%)")

    widths = sorted(r["width_pct"] for r in u)
    med = widths[len(widths) // 2]
    print(f"Ширина зон, % цены: медиана {med:.1f} | макс {widths[-1]:.1f}")

    print("\nТолстые зоны по типу якоря (уникальные):")
    by_anchor = Counter(r["anchor"] for r in u if "FAT" in r["flags"])
    tot_anchor = Counter(r["anchor"] for r in u)
    for a, k in by_anchor.most_common():
        print(f"  {a:<28} толстых {k:>4} из {tot_anchor[a]:>4}")

    comp = sum(1 for r in u if "FAT" in r["flags"] and "сжата" in (r["reason"] or ""))
    raw_eq = sum(1 for r in u if "FAT" in r["flags"] and "границы = сырой якорь" in (r["reason"] or ""))
    print(f"\nТолстые: обрезаны compress_fat_zones — {comp}; толстые уже на уровне сырого якоря — {raw_eq}")

    macro_fat = [r for r in u if "FAT" in r["flags"] and r.get("own_atr_pct")]
    if macro_fat:
        avg = sum(r["own_atr_pct"] for r in macro_fat) / len(macro_fat)
        d_atr = sorted(r['atr_1d_pct'] for r in u if r['atr_1d_pct'])
        d_med = d_atr[len(d_atr) // 2] if d_atr else 0.0
        print(f"Толстые MACRO: средний ATR своей свечи = {avg:.1f}% цены (дневной ATR, медиана {d_med:.1f}%)")

    print(f"\nТоп-{TOP_N} самых широких зон:")
    for r in sorted(u, key=lambda q: q["width_atr"], reverse=True)[:TOP_N]:
        print(f"  {r['coin']:<8} {r['snapshot'][:16]} {r['side']} {r['anchor']:<22} "
              f"{r['width_pct']:5.1f}% {r['width_atr']:4.1f}ATR [{r['flags']}] {r['reason']}")


def main():
    months = sys.argv[1:] or [datetime.datetime.now().strftime("%Y_%m")]
    if not exchange.markets:
        exchange.load_markets(reload=False)
    markets = exchange.markets or {}
    rows, cache = [], {}
    for m in months:
        audit_month(m, rows, cache, markets)
    out = os.path.join(CURRENT_DIR, f"levels_audit_{'_'.join(months)}.csv")
    write_csv(rows, out)
    summary(rows)
    print(f"\n✅ CSV: {out}")


if __name__ == "__main__":
    main()