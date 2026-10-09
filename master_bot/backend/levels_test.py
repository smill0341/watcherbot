"""
levels_test.py
==============
Сравнение двух способов получать уровни за неделю (снимки раз в 12 часов):

  СТАРЫЙ — как сейчас: каждый снимок = свежий build_levels(), без связи со
           снимком до него.
  НОВЫЙ  — те же отдельные точки, что build_levels собирает ДО склейки (у каждой своя
           геометрия), но с другим правилом (G = GAP_ATR * дневной ATR):
           - точка пересекается с уже стоящей зоной или ближе G -> поглощается ею
             (в название зоны добавляется её тип, отдельной зоны нет);
           - точка дальше G от всех зон -> отдельная зона СО СВОЕЙ геометрией, без обрезки;
           - зоны прошлого снимка стоят как есть, пока их не пробили закрытием за
             дальним краем (считая с первого появления зоны);
           - на сторону остаются MAX_PER_SIDE ближайших к цене, как в builder.
           Остатков нет, порога ширины нет. Фолбэк «минимум 2 на сторону» и
           compress_fat_zones в тесте не применяются.

Ничего не пишет (ни в базу, ни в json), боевые и симуляторные файлы не трогает.
Только читает свечи (как precalc_for_bot.py) и печатает отчёт.

Что мерим: если бы КАЖДАЯ зона, когда-либо появившаяся за неделю и ещё не
пробитая, была живым ватчером — сколько пар зон одной стороны пересекаются или
лежат ближе G. Это ровно то, что даёт дубли сделок.

Запуск (cmd, из той же папки и тем же способом, что precalc_for_bot.py — файл
кладётся РЯДОМ с ним):
    python levels_test.py
    python levels_test.py --days 7 --coins BTC,ETH,SOL
    python levels_test.py --start 2026-09-24 --days 7
"""
import os
import sys
import argparse
import datetime
import copy

import numpy as np
import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
for _p in (CURRENT_DIR, ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.modules.cryptano.levels.levels_builder import (
    build_levels, _tf_rank, _merge_type_labels, _collect_zones, _split_alive,
    _calc_weekly_atr, ATR_DISTANCE_MULTIPLIER, _pick_nearest, MAX_PER_SIDE, compress_fat_zones,
    resolve_cross_overlaps, count_zone_touches, _get_poc, _apply_poc_confluence, _anchor_type, _dist_to_price,
    REJECTION_SCORE_STEP, REJECTION_SCORE_CAP,
)
from backend.modules.cryptano.utils.indicators import calculate_atr
import backend.modules.cryptano.levels.levels_builder as NEW_BUILDER

DUMP_AT = set()      # --dump: 'ГГГГ-ММ-ДД ЧЧ:ММ' — вывести полные списки зон трёх вариантов (нужна --coins)
OLD_BUILDER = None   # модуль старого билдера (до правки ATR) — только для сравнения ширины зон


def _load_old_builder(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("levels_builder_old", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cls_cross(zs_, zr_, lv_old_like):
    """Пересечение S/R в НОВОМ: общие метки (одна и та же точка в обеих зонах) и было ли такое же пересечение
    в СТАРОМ в тот же момент (зона S старого пересекается с zs_, зона R старого с zr_, и они сами пересекаются)."""
    shared = sorted(_parts(zs_) & _parts(zr_))
    existed = any(_gap(zs_, a) < 0 and _gap(zr_, b) < 0 and _gap(a, b) < 0
                  for a in lv_old_like["supports"] for b in lv_old_like["resistances"])
    return shared, existed


def _pre_pick(mod, df_1M, df_1W, df_1d, df_4h, coin):
    """Все зоны одной стороны ДО отбора ближайших (после склейки точек), без реестра — как внутри build_levels.
    Возвращает ({'S': [...], 'R': [...]}, число сырых точек с MACRO по сторонам)."""
    idx = len(df_1d) - 1
    price = float(df_1d['close'].iloc[idx])
    d1 = df_1d.copy()
    d1['atr'] = calculate_atr(d1, 14)
    a1 = d1['atr'].iloc[idx]
    if pd.isna(a1) or a1 == 0:
        a1 = price * 0.05
    max_dist = mod._calc_weekly_atr(d1, idx) * mod.ATR_DISTANCE_MULTIPLIER
    raw = mod._collect_zones(df_1M, df_1W, d1, df_4h, price, a1, max_dist, idx, count_stats=False)
    raw_s, raw_r = mod._split_alive(raw)
    out, macro = {}, {}
    for side, pts in (("S", raw_s), ("R", raw_r)):
        macro[side] = [dict(z) for z in pts if "MACRO" in str(z.get("type"))]
        mod.compress_fat_zones(pts, coin)
        zones, _ = mod._place(pts, [])
        out[side] = sorted(zones, key=lambda q: mod._dist_to_price(q, price))
    return out, macro, price


def _width_check(lv_new, lv_old, coin, when):
    """Одни и те же свечи, два билдера. Для каждой зоны нового — зона старого той же стороны с наибольшим
    пересечением. Считаем ширину в % от середины зоны."""
    out = {"w_new": [], "w_old": [], "same": 0, "changed": 0, "only_new": 0, "only_old": 0, "examples": []}
    for key, side in (("supports", "S"), ("resistances", "R")):
        a_list, b_list = lv_new[key], lv_old[key]
        used = set()
        for z in a_list:
            mid = (z["min"] + z["max"]) / 2
            out["w_new"].append((z["max"] - z["min"]) / mid * 100)
            hit = [(i, b) for i, b in enumerate(b_list) if _gap(z, b) < 0]
            if not hit:
                out["only_new"] += 1
                out["examples"].append((999.0, "только в новом", coin, when, side, "-", _fz(z), 0.0, (z["max"] - z["min"]) / mid * 100))
                continue
            i, b = max(hit, key=lambda ib: -_gap(z, ib[1]))
            used.add(i)
            same = (round(float(z["min"]), 10), round(float(z["max"]), 10)) == (round(float(b["min"]), 10), round(float(b["max"]), 10))
            if same:
                out["same"] += 1
            else:
                out["changed"] += 1
                wn = (z["max"] - z["min"]) / mid * 100
                wo = (b["max"] - b["min"]) / ((b["min"] + b["max"]) / 2) * 100
                out["examples"].append((wn - wo, "границы", coin, when, side, _fz(b), _fz(z), wo, wn))
        for i, b in enumerate(b_list):
            out["w_old"].append((b["max"] - b["min"]) / ((b["min"] + b["max"]) / 2) * 100)
            if i not in used:
                out["only_old"] += 1
    return out


GAP_ATR = 0.0     # зазор G в дневных ATR; 0 = зоны только не должны пересекаться (без коэффициентов)


# ----------------------------------------------------------------------------
# Геометрия и состав
# ----------------------------------------------------------------------------

def _gap(a, b):
    """Расстояние между зонами; <= 0 — пересекаются."""
    return max(a["min"], b["min"]) - min(a["max"], b["max"])


def _parts(z):
    return frozenset(p.strip() for p in str(z.get("type") or "").split("+") if p.strip())


def _key(side, z):
    return (side, round(float(z["min"]), 10), round(float(z["max"]), 10))


# ----------------------------------------------------------------------------
# НОВЫЙ метод
# ----------------------------------------------------------------------------

def _fz(z):
    return f"[{z['min']:.6g}..{z['max']:.6g}] {z.get('type')}"


def place(points, existing, G, log=None):
    """НОВОЕ правило на ОТДЕЛЬНЫХ ТОЧКАХ (до склейки builder-а), у каждой своя геометрия.
    Идём от самой сильной точки (1M > 1W > 1d > 4h, при равенстве — старше).
      - точка пересекается с уже стоящей зоной или ближе G -> поглощается ею: в названии
        зоны добавляется её тип, отдельной зоны нет;
      - точка дальше G от всех зон -> становится отдельной зоной СО СВОЕЙ геометрией (без обрезки).
    existing — зоны, которые уже стоят (прошлый снимок, замороженные); они не двигаются.
    Возвращает (все зоны, список новых отдельных зон)."""
    zones = list(existing)
    added = []
    for p in sorted(points, key=lambda q: (-_tf_rank(q), str(q.get("date") or "9999"), q["min"])):
        host = next((z for z in zones if _gap(p, z) < G), None)
        if host is None:
            q = dict(p)
            q["anchor"] = _anchor_type(q)
            zones.append(q)
            added.append(q)
            continue
        if log is not None and (p["min"], p["max"]) != (host["min"], host["max"]):
            log.append(("поглощена (пересекается или ближе G) зоной", p, host))
        host["type"] = _merge_type_labels(host.get("type"), p.get("type"))
        host["score"] = round(max(float(host.get("score", 0)), float(p.get("score", 0))), 2)
    return zones, added


def explain_snapshot(coin, t, df_1M, df_1W, df_1d, df_4h, prev, snap_day, G, old_lv):
    """Диагностика одного снимка: что видит сборщик, что в реестре, кого выбрали и куда делись остальные."""
    d1 = df_1d.copy()
    d1["atr"] = calculate_atr(d1, 14)
    idx = len(d1) - 1
    price = float(d1["close"].iloc[idx])
    a1 = d1["atr"].iloc[idx]
    if pd.isna(a1) or a1 == 0:
        a1 = price * 0.05
    max_dist = _calc_weekly_atr(d1, idx) * ATR_DISTANCE_MULTIPLIER
    print(f"\n===== {coin} {t}  цена {price}  max_dist {max_dist:.6g} ({max_dist / price * 100:.1f}%) =====")
    print("СТАРЫЙ метод выдал:")
    for sd, key in (("S", "supports"), ("R", "resistances")):
        for z in old_lv[key]:
            print(f"   {sd} {_fz(z)} {z.get('type')} дата {z.get('date')}")
    raw = _collect_zones(df_1M, df_1W, d1, df_4h, price, a1, max_dist, idx, count_stats=False)
    raw_s, raw_r = _split_alive(raw)
    for side, is_sup, pts in (("S", True, raw_s), ("R", False, raw_r)):
        print(f"\n--- сторона {side} ---")
        print(f"сырых живых точек: {len(pts)}")
        for p in sorted(pts, key=lambda q: _dist_to_price(q, price)):
            print(f"   точка {_fz(p)} {p.get('type')} дата {p.get('date')} расстояние {_dist_to_price(p, price) / price * 100:.1f}%")
        reg_all = prev[side]
        print(f"реестр с прошлого снимка: {len(reg_all)}")
        for z in reg_all:
            alive = _still_alive(z, is_sup, d1)
            print(f"   {'жива ' if alive else 'ПРОБИТА'} {_fz(z)} {z.get('type')} с {z.get('_since') or z.get('date')} расстояние {_dist_to_price(z, price) / price * 100:.1f}%")
        existing = [copy.deepcopy(z) for z in reg_all if _still_alive(z, is_sup, d1)]
        pts2 = copy.deepcopy(pts)
        compress_fat_zones(pts2, coin)
        zones, added = place(pts2, existing, G, [])
        print(f"после расстановки (реестр + новые): {len(zones)}")
        chosen = _pick_nearest(zones, price, MAX_PER_SIDE)
        ids = {id(z) for z in chosen}
        for z in sorted(zones, key=lambda q: _dist_to_price(q, price)):
            print(f"   {'ВЫБРАНА ' if id(z) in ids else 'не выбр. '} {_fz(z)} {z.get('type')} расстояние {_dist_to_price(z, price) / price * 100:.1f}%")
        if len(chosen) < 2:
            print(f"   выбрано {len(chosen)} < 2 -> фолбэк (вся история)")


def build_new(df_1M, df_1W, df_1d, df_4h, coin, prev, snap_day, G, log=None):
    """ВСЯ цепочка build_levels с новым правилом (то, что вернул бы новый builder):
    сырые точки -> непробитые -> сжатие широких -> расстановка относительно зон прошлого снимка
    (стоят до пробоя) -> POC-бонус -> ближайшие MAX_PER_SIDE -> фолбэк (минимум 2 на сторону) ->
    сжатие -> resolve_cross_overlaps -> отбои/score -> level_id.
    prev = {"S": [...], "R": [...]} — РЕЕСТР: все зоны, которые хоть раз были выданы и ещё не пробиты
    (не только 2 ближайших). Возвращает {"S","R"} — выдача, и {"reg"} — новый реестр."""
    d1 = df_1d.copy()
    d1["atr"] = calculate_atr(d1, 14)
    idx = len(d1) - 1
    price = float(d1["close"].iloc[idx])
    a1 = d1["atr"].iloc[idx]
    if pd.isna(a1) or a1 == 0:
        a1 = price * 0.05
    max_dist = _calc_weekly_atr(d1, idx) * ATR_DISTANCE_MULTIPLIER

    raw = _collect_zones(df_1M, df_1W, d1, df_4h, price, a1, max_dist, idx, count_stats=False)
    raw_s, raw_r = _split_alive(raw)
    out = {}
    reg_alive = {}
    poc_price = atr_4h = None
    if df_4h is not None and len(df_4h) >= 50:
        poc_price, atr_4h = _get_poc(df_4h, price)

    for side, is_sup, pts in (("S", True, raw_s), ("R", False, raw_r)):
        existing = [copy.deepcopy(z) for z in prev[side] if _still_alive(z, is_sup, df_1d)]
        reg_alive[side] = existing
        compress_fat_zones(pts, coin)
        zones, added = place(pts, existing, G, [] if log is None else log_side(log, side))
        for z in added:
            z["_since"] = snap_day
        if poc_price is not None:
            zones = _apply_poc_confluence(zones, poc_price, atr_4h)
        chosen = _pick_nearest(zones, price, MAX_PER_SIDE)

        if len(chosen) < 2:   # фолбэк: вся история, бесконечный радар (как в builder)
            fb_all = _collect_zones(df_1M, df_1W, d1, df_4h, price, a1, float("inf"), idx, deep=True, count_stats=False)
            fb_s, fb_r = _split_alive(fb_all)
            cand = fb_s if is_sup else fb_r
            cand = [z for z in cand if (z["max"] < price if is_sup else z["min"] > price)]
            compress_fat_zones(cand, coin)
            fb_zones, fb_added = place(cand, [dict(z) for z in chosen], G, [])
            fb_added.sort(key=lambda x: _dist_to_price(x, price))
            for z in fb_added[: 2 - len(chosen)]:
                z["_since"] = snap_day
                chosen.append(z)
        out[side] = chosen

    for side in ("S", "R"):
        compress_fat_zones(out[side], coin)
    sup, res = resolve_cross_overlaps(out["S"], out["R"])
    for sd, zs, is_sup in (("S", sup, True), ("R", res, False)):
        for z in zs:
            if "reaction_count" not in z or z.get("_since") == snap_day:
                z["reaction_count"] = count_zone_touches(d1, z, is_sup, idx)
            z["level_id"] = f"{sd}|{z.get('anchor') or _anchor_type(z)}|{z.get('date')}"
    reg = {}
    for sd, zs in (("S", sup), ("R", res)):
        seen_keys = set()
        reg[sd] = []
        for z in reg_alive[sd] + zs:      # реестр = живые прежние + выданные сейчас
            k = _key(sd, z)
            if k not in seen_keys:
                seen_keys.add(k)
                reg[sd].append(z)
    return {"S": sorted(sup, key=lambda q: q["min"]), "R": sorted(res, key=lambda q: q["min"]), "reg": reg}


def log_side(log, side):
    """Обёртка: события place() складываются в общий список с пометкой стороны."""
    class _L(list):
        def append(self, item):
            log.append((side,) + tuple(item))
    return _L()


def _still_alive(zone, is_support, df_1d_slice):
    """Зона жива, пока ни одно дневное закрытие после её даты не ушло за дальний край."""
    d = str(zone.get("_since") or zone.get("date") or "")[:10]
    dates = pd.to_datetime(df_1d_slice["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")
    closes = df_1d_slice["close"][dates > d]
    if closes.empty:
        return True
    return not ((closes < zone["min"]).any() if is_support else (closes > zone["max"]).any())


# ----------------------------------------------------------------------------
# Прогон
# ----------------------------------------------------------------------------

_TF_MS = {"4h": 4 * 3600 * 1000, "1d": 24 * 3600 * 1000, "1W": 7 * 24 * 3600 * 1000}


def _slice_by_time(df, target_ts_ms, tail_n, tf):
    """Только свечи, ЗАКРЫТЫЕ к моменту снимка: время открытия + длина свечи <= target.
    (timestamp свечи — время её ОТКРЫТИЯ; раньше бралось timestamp <= target, и в снимок
    попадала целиком ещё не закрытая свеча — день/неделя/месяц — с закрытием из будущего.)"""
    if df is None:
        return None
    if tf == "1M":
        opened = pd.to_datetime(df['timestamp'], unit='ms')
        close_ms = (opened + pd.offsets.MonthBegin(1)).astype('datetime64[ms]').astype('int64')
    else:
        close_ms = df['timestamp'] + _TF_MS[tf]
    sliced = df[close_ms <= target_ts_ms]
    if sliced.empty:
        return None
    return sliced.tail(tail_n).reset_index(drop=True)


def _zid(side, z):
    """Идентичность зоны без границ: сторона | якорь | дата (как level_id у builder)."""
    return z.get("level_id") or f"{side}|{_anchor_type(z)}|{z.get('date')}"


def _drift(prev, cur, atr_d, coin, t, flips=None):
    """Что произошло с зонами между двумя соседними снимками одной монеты (то, что видит менеджер).
    Зона текущего снимка сопоставляется с зоной прошлого, у которой тот же id И которая пересекается с ней.
    same_id — такая пара найдена; bounds_changed — у неё поменялись границы;
    id_flip — то же место (пересекается), но id другой; new/gone — зона появилась/ушла совсем;
    id_dup — в одном снимке две зоны одной стороны с одинаковым id (по id их не различить)."""
    out = {"same_id": 0, "bounds_changed": 0, "id_flip": 0, "new": 0, "gone": 0, "id_dup": 0, "examples": []}
    when = t.strftime("%m-%d %H:%M")
    for side in ("S", "R"):
        p, c = prev.get(side, []), cur.get(side, [])
        ids = [_zid(side, z) for z in c]
        out["id_dup"] += len(ids) - len(set(ids))
        matched_prev = set()
        for z in c:
            zid = _zid(side, z)
            cands = [(i, a) for i, a in enumerate(p) if _zid(side, a) == zid and _gap(z, a) < 0]
            if cands:
                i, a = min(cands, key=lambda ia: _gap(z, ia[1]))
                matched_prev.add(i)
                out["same_id"] += 1
                if (round(float(a["min"]), 10), round(float(a["max"]), 10)) != (round(float(z["min"]), 10), round(float(z["max"]), 10)):
                    out["bounds_changed"] += 1
                    shift = max(abs(z["min"] - a["min"]), abs(z["max"] - a["max"])) / atr_d
                    out["examples"].append((shift, "границы", coin, when, side, _fz(a), _fz(z)))
                continue
            hit = [(i, a) for i, a in enumerate(p) if _gap(z, a) < 0]
            if hit:
                i, a = min(hit, key=lambda ia: _gap(z, ia[1]))
                matched_prev.add(i)
                out["id_flip"] += 1
                out["examples"].append((0.0, "id", coin, when, side, _fz(a), _fz(z)))
                if flips is not None:
                    flips.append((side, a, z))
            else:
                out["new"] += 1
        for i, a in enumerate(p):
            if i not in matched_prev:
                out["gone"] += 1
    return out


def _window_check(lv, lv_alt, coin, when):
    """Одни и те же сутки считаем с обычным окном свечей и с окном чуть длиннее. Зона, которая есть в обоих
    (тот же id и то же место), должна иметь ОДИНАКОВЫЕ границы: уровень не зависит от того, где началась
    выгрузка свечей. Зона только в одном из результатов — не ошибка (длинное окно просто видит дальше)."""
    out = {"pairs": 0, "differ": 0, "only_one": 0, "examples": []}
    for key, side in (("supports", "S"), ("resistances", "R")):
        a_list, b_list = lv[key], lv_alt[key]
        used = set()
        for a in a_list:
            cands = [(i, b) for i, b in enumerate(b_list)
                     if i not in used and _zid(side, b) == _zid(side, a) and _gap(a, b) < 0]
            if not cands:
                out["only_one"] += 1
                continue
            i, b = min(cands, key=lambda ib: _gap(a, ib[1]))
            used.add(i)
            out["pairs"] += 1
            if (round(float(a["min"]), 10), round(float(a["max"]), 10)) != (round(float(b["min"]), 10), round(float(b["max"]), 10)):
                out["differ"] += 1
                out["examples"].append((coin, when, side, _fz(a), _fz(b)))
        out["only_one"] += len(b_list) - len(used)
    return out


def run_coin(coin, tf_data, times, explain_at=None):
    """Возвращает словарь с метриками по одной монете для старого и нового метода."""
    res = {}
    seen = {"old": {}, "new": {}}      # (side,min,max) -> (zone, first_ts_ms)
    per_snap = {"old": [], "new": []}
    prev_new = {"S": [], "R": []}   # реестр новых зон
    last_df_1d = None
    last_G = None
    events = {}   # дедуп по (событие, зона, с чем) -> первая дата
    nocover = {"old": {"S": 0, "R": 0}, "new": {"S": 0, "R": 0}}
    cross = {"old": 0, "new": 0}
    nocover_list = {"old": [], "new": []}
    cross_list = {"old": [], "new": []}
    cross_cls = {"shared_existed": 0, "shared_new": 0, "noshared_existed": 0, "noshared_new": 0, "examples": {}}
    last_snap = None
    prev_sel = None
    wincheck = {"pairs": 0, "differ": 0, "only_one": 0, "examples": []}
    widthck = {"w_new": [], "w_old": [], "same": 0, "changed": 0, "only_new": 0, "only_old": 0, "examples": []}
    snap_no = 0
    drift = {m_: {"same_id": 0, "bounds_changed": 0, "id_flip": 0, "new": 0, "gone": 0, "id_dup": 0, "examples": []} for m_ in ("old", "new")}

    for t in times:
        ts_ms = int(t.timestamp() * 1000)
        df_1M = _slice_by_time(tf_data["1M"], ts_ms, 60, "1M")
        df_1W = _slice_by_time(tf_data["1W"], ts_ms, 150, "1W")
        df_1d = _slice_by_time(tf_data["1d"], ts_ms, 365, "1d")
        df_4h = _slice_by_time(tf_data["4h"], ts_ms, 200, "4h")
        if df_1d is None or len(df_1d) < 50:
            continue
        atr = calculate_atr(df_1d, 14).iloc[-1]
        if pd.isna(atr) or atr == 0:
            atr = float(df_1d["close"].iloc[-1]) * 0.05
        G = GAP_ATR * float(atr)
        last_df_1d, last_G = df_1d, G

        lv = build_levels(df_1M, df_1W, df_1d, df_4h, coin)
        if snap_no % 4 == 0:   # окно чуть длиннее: 1M 60->66, 1W 150->160, 1d 365->380, 4h 200->215
            lv_alt = build_levels(_slice_by_time(tf_data["1M"], ts_ms, 66, "1M"), _slice_by_time(tf_data["1W"], ts_ms, 160, "1W"),
                                  _slice_by_time(tf_data["1d"], ts_ms, 380, "1d"), _slice_by_time(tf_data["4h"], ts_ms, 215, "4h"), coin)
            w_ = _window_check(lv, lv_alt, coin, t.strftime("%m-%d %H:%M"))
            for k_ in ("pairs", "differ", "only_one"):
                wincheck[k_] += w_[k_]
            wincheck["examples"].extend(w_["examples"])
            if OLD_BUILDER is not None:
                lv_o = OLD_BUILDER.build_levels(df_1M, df_1W, df_1d, df_4h, coin)
                wd_ = _width_check(lv, lv_o, coin, t.strftime("%m-%d %H:%M"))
                for k_ in ("w_new", "w_old", "examples"):
                    widthck[k_].extend(wd_[k_])
                for k_ in ("same", "changed", "only_new", "only_old"):
                    widthck[k_] += wd_[k_]
        snap_no += 1

        # --- СТАРЫЙ ---
        n_old = 0
        for side, zs in (("S", lv["supports"]), ("R", lv["resistances"])):
            n_old += len(zs)
            for z in zs:
                seen["old"].setdefault(_key(side, z), (dict(z), ts_ms))
        per_snap["old"].append(n_old)

        if explain_at is not None and t.strftime("%Y-%m-%d %H:%M") in explain_at:
            explain_snapshot(coin, t, df_1M, df_1W, df_1d, df_4h, prev_new, t.strftime("%Y-%m-%d"), G, lv)

        # --- НОВЫЙ: вся цепочка ---
        snap_day = t.strftime("%Y-%m-%d")
        elog = []
        cur_new = build_new(df_1M, df_1W, df_1d, df_4h, coin, prev_new, snap_day, G, elog)
        for side_, what, p0, host in elog:
            events.setdefault((side_, what, _fz(p0), _fz(host)), (t.strftime("%m-%d %H:%M"), None))
        n_new = 0
        for side in ("S", "R"):
            n_new += len(cur_new[side])
            for z in cur_new[side]:
                seen["new"].setdefault(_key(side, z), (dict(z), ts_ms))
        prev_new = cur_new["reg"]
        if t.strftime("%Y-%m-%d %H:%M") in DUMP_AT:
            print(f"\n=== ДАМП {coin} {t} ===")
            variants = [("ДО правки ATR (без реестра)", OLD_BUILDER.build_levels(df_1M, df_1W, df_1d, df_4h, coin) if OLD_BUILDER else None),
                        ("ПОСЛЕ правки (без реестра)", lv),
                        ("ПОСЛЕ правки (с реестром, НОВЫЙ)", {"supports": cur_new["S"], "resistances": cur_new["R"]})]
            for tag_, mod_ in (("ДО правки ATR", OLD_BUILDER), ("ПОСЛЕ правки", NEW_BUILDER)):
                if mod_ is None:
                    continue
                try:
                    pp_, mc_, pr_ = _pre_pick(mod_, df_1M, df_1W, df_1d, df_4h, coin)
                except Exception as e_:
                    print(f"-- {tag_}: до отбора — ошибка {e_}")
                    continue
                print(f"-- {tag_}: ВСЕ зоны до отбора ближайших (цена {pr_:.6g}; ближайшие первыми; в выдачу идут 2 на сторону)")
                for sd_ in ("R", "S"):
                    for z_ in pp_[sd_]:
                        print(f"   {sd_} {(z_['max'] - z_['min']) / ((z_['max'] + z_['min']) / 2) * 100:5.1f}%  {_fz(z_)}")
                    for z_ in mc_[sd_]:
                        print(f"      сырая MACRO-точка {sd_}: [{z_['min']:.6g}..{z_['max']:.6g}] {z_.get('type')}")
            for name_, v_ in variants:
                if v_ is None:
                    continue
                print(f"-- {name_}")
                for z_ in sorted(v_["resistances"], key=lambda q: -q["min"]):
                    print(f"   R {(z_['max'] - z_['min']) / ((z_['max'] + z_['min']) / 2) * 100:5.1f}%  {_fz(z_)}")
                for z_ in sorted(v_["supports"], key=lambda q: -q["min"]):
                    print(f"   S {(z_['max'] - z_['min']) / ((z_['max'] + z_['min']) / 2) * 100:5.1f}%  {_fz(z_)}")
        # покрытие: есть ли хоть одна зона снизу (S) и сверху (R)
        for m_, lvs in (("old", {"S": lv["supports"], "R": lv["resistances"]}), ("new", cur_new)):
            for side in ("S", "R"):
                if not lvs[side]:
                    nocover[m_][side] += 1
                    nocover_list[m_].append((t.strftime("%m-%d %H:%M"), side))
            for zs_ in lvs["S"]:
                for zr_ in lvs["R"]:
                    if _gap(zs_, zr_) < 0:
                        cross[m_] += 1
                        cross_list[m_].append((t.strftime("%m-%d %H:%M"), _fz(zs_), _fz(zr_)))
                        if m_ == "new":
                            sh_, ex_ = _cls_cross(zs_, zr_, lv)
                            k_ = ("shared_" if sh_ else "noshared_") + ("existed" if ex_ else "new")
                            cross_cls[k_] += 1
                            cross_cls["examples"].setdefault(k_, [])
                            if len(cross_cls["examples"][k_]) < 4:
                                cross_cls["examples"][k_].append((coin, t.strftime("%m-%d %H:%M"), _fz(zs_), _fz(zr_), sh_))
        last_snap = {"old": {"S": lv["supports"], "R": lv["resistances"]}, "new": cur_new}
        cur_sel = {"old": {"S": lv["supports"], "R": lv["resistances"]}, "new": {"S": cur_new["S"], "R": cur_new["R"]}}
        if prev_sel is not None:
            for m_ in ("old", "new"):
                d_ = _drift(prev_sel[m_], cur_sel[m_], float(atr), coin, t)
                for k_ in ("same_id", "bounds_changed", "id_flip", "new", "gone", "id_dup"):
                    drift[m_][k_] += d_[k_]
                drift[m_]["examples"].extend(d_["examples"])
        prev_sel = cur_sel
        per_snap["new"].append(n_new)

    if last_df_1d is None:
        return None

    for m in ("old", "new"):
        alive = {"S": [], "R": []}
        for (side, _, _), (z, first_ms) in seen[m].items():
            first_date = pd.to_datetime(first_ms, unit="ms").strftime("%Y-%m-%d")
            zz = dict(z)
            zz["date"] = first_date  # проверяем пробой только ПОСЛЕ появления зоны в снимках
            if _still_alive(zz, side == "S", last_df_1d):
                alive[side].append(z)
        pairs, examples = 0, []
        for side, zs in alive.items():
            zs = sorted(zs, key=lambda q: q["min"])
            for i in range(len(zs)):
                for j in range(i + 1, len(zs)):
                    if _gap(zs[i], zs[j]) < last_G:
                        pairs += 1
                        if len(examples) < 6:
                            examples.append((side, zs[i], zs[j]))
        res[m] = {
            "alive_zones": {k: sorted(v, key=lambda q: q["min"]) for k, v in alive.items()},
            "avg_per_snap": float(np.mean(per_snap[m])) if per_snap[m] else 0.0,
            "alive": len(alive["S"]) + len(alive["R"]),
            "pairs": pairs,
            "examples": examples,
        }
    res["events"] = events
    res["nocover"] = nocover
    res["cross"] = cross
    res["nocover_list"] = nocover_list
    res["cross_list"] = cross_list
    res["last_snap"] = last_snap
    res["drift"] = drift
    res["wincheck"] = wincheck
    res["widthck"] = widthck
    res["cross_cls"] = cross_cls
    res["snaps"] = len(per_snap["new"])
    res["G"] = last_G
    return res


def run_test(cache, start, days, coins=None, explain_at=None):
    times = list(pd.date_range(start=start, periods=days * 2, freq="12h"))
    rows = []
    n = len(cache)
    for i, (coin, tf_data) in enumerate(cache.items(), 1):
        if coins and coin not in coins:
            continue
        try:
            r = run_coin(coin, tf_data, times, explain_at)
        except Exception as e:
            print(f"[ERROR] {coin}: {e}")
            continue
        if r:
            rows.append((coin, r))
        if i % 10 == 0:
            print(f"   …{i}/{n}")
    return times, rows


def report(times, rows):
    print(f"\nПериод: {times[0]} .. {times[-1]}  (снимков: {len(times)}), монет: {len(rows)}")
    if not rows:
        print("Нет данных.")
        return
    tot = {m: {"pairs": 0, "alive": 0, "avg": 0.0, "coins_with_pairs": 0} for m in ("old", "new")}
    for _, r in rows:
        for m in ("old", "new"):
            tot[m]["pairs"] += r[m]["pairs"]
            tot[m]["alive"] += r[m]["alive"]
            tot[m]["avg"] += r[m]["avg_per_snap"]
            tot[m]["coins_with_pairs"] += 1 if r[m]["pairs"] else 0
    print(f"\n{'':28}{'СТАРЫЙ':>10}{'НОВЫЙ':>10}")
    print(f"{'живых зон за неделю (всего)':28}{tot['old']['alive']:>10}{tot['new']['alive']:>10}")
    print(f"{'пар пересекаются/<G':28}{tot['old']['pairs']:>10}{tot['new']['pairs']:>10}")
    print(f"{'монет с такими парами':28}{tot['old']['coins_with_pairs']:>10}{tot['new']['coins_with_pairs']:>10}")
    print(f"{'зон в снимке на монету (ср.)':28}{tot['old']['avg'] / len(rows):>10.1f}{tot['new']['avg'] / len(rows):>10.1f}")

    tot_snaps = sum(r["snaps"] for _, r in rows)
    for label, key in (("снимков без зон снизу", "S"), ("снимков без зон сверху", "R")):
        print(f"{label:28}{sum(r['nocover']['old'][key] for _, r in rows):>10}{sum(r['nocover']['new'][key] for _, r in rows):>10}   (из {tot_snaps} снимков монет)")
    print(f"{'пересечений поддержка/сопр.':28}{sum(r['cross']['old'] for _, r in rows):>10}{sum(r['cross']['new'] for _, r in rows):>10}   (по снимкам)")

    cc = {k_: sum(r["cross_cls"][k_] for _, r in rows) for k_ in ("shared_existed", "shared_new", "noshared_existed", "noshared_new")}
    print("\nРАЗБОР пересечений поддержка/сопр. в НОВОМ (реестр):")
    print(f"  одна и та же точка в обеих зонах, такое же пересечение есть и в СТАРОМ: {cc['shared_existed']}")
    print(f"  одна и та же точка в обеих зонах, в СТАРОМ такого пересечения нет:      {cc['shared_new']}")
    print(f"  разные точки, такое же пересечение есть и в СТАРОМ:                      {cc['noshared_existed']}")
    print(f"  разные точки, в СТАРОМ такого пересечения нет:                           {cc['noshared_new']}")
    for k_ in ("shared_new", "shared_existed", "noshared_new", "noshared_existed"):
        exs_ = [e for _, r in rows for e in r["cross_cls"]["examples"].get(k_, [])][:3]
        for c_, d_, a_, b_, sh_ in exs_:
            print(f"    [{k_}] {c_} {d_}  общие: {sh_}\n        S {a_}\n        R {b_}")

    for m_, name_ in (("old", "СТАРЫЙ"), ("new", "НОВЫЙ")):
        lines = [(c_, d_, sd_) for c_, r_ in rows for d_, sd_ in r_["nocover_list"][m_]]
        if lines:
            print(f"\nСнимки без зон ({name_}): монета, время, сторона (S снизу / R сверху)")
            for c_, d_, sd_ in lines[:20]:
                print(f"  {c_:<10}{d_}  {sd_}")
        cl = [(c_,) + x_ for c_, r_ in rows for x_ in r_["cross_list"][m_]]
        if cl:
            print(f"\nПересечения поддержка/сопротивление ({name_}):")
            for c_, d_, zs_, zr_ in cl[:20]:
                print(f"  {c_:<10}{d_}  S {zs_}   <->   R {zr_}")

    print("\nДРЕЙФ МЕЖДУ СОСЕДНИМИ СНИМКАМИ (зоны в выдаче; id = сторона | якорь | дата):")
    print(f"{'':44}{'СТАРЫЙ':>10}{'НОВЫЙ':>10}")
    for label, key in (("зон с тем же id в двух снимках подряд", "same_id"),
                       ("  из них границы поменялись", "bounds_changed"),
                       ("то же место, но id другой", "id_flip"),
                       ("зона появилась (нового места)", "new"),
                       ("зона ушла совсем", "gone"),
                       ("две зоны одной стороны с одним id", "id_dup")):
        print(f"{label:44}{sum(r['drift']['old'][key] for _, r in rows):>10}{sum(r['drift']['new'][key] for _, r in rows):>10}")
    wc = {k_: sum(r["wincheck"][k_] for _, r in rows) for k_ in ("pairs", "differ", "only_one")}
    print("\nПРОВЕРКА «УРОВЕНЬ НЕ ЗАВИСИТ ОТ ОКНА СВЕЧЕЙ» (каждый 4-й снимок: обычное окно против чуть длиннее):")
    print(f"  зон, найденных в обоих окнах: {wc['pairs']}   из них с разными границами: {wc['differ']}   (должно быть 0)")
    print(f"  зон только в одном окне (длинное видит дальше): {wc['only_one']}")
    for c_, when_, side_, a_, b_ in [e for _, r in rows for e in r["wincheck"]["examples"]][:8]:
        print(f"    {c_:<9}{when_} {side_}\n        обычное окно {a_}\n        длинное окно {b_}")
    if OLD_BUILDER is not None:
        W = {k_: [x for _, r in rows for x in r["widthck"][k_]] for k_ in ("w_new", "w_old", "examples")}
        C = {k_: sum(r["widthck"][k_] for _, r in rows) for k_ in ("same", "changed", "only_new", "only_old")}
        print("\nШИРИНА ЗОН: билдер ДО правки ATR против ПОСЛЕ (те же свечи, каждый 4-й снимок; ширина в % от цены):")
        print(f"{'':30}{'ДО':>10}{'ПОСЛЕ':>10}")
        for label, fn in (("зон", len), ("медиана, %", lambda a: float(np.median(a)) if a else 0.0),
                          ("90-й процентиль, %", lambda a: float(np.percentile(a, 90)) if a else 0.0),
                          ("максимум, %", lambda a: max(a) if a else 0.0),
                          ("зон шире 10%", lambda a: sum(1 for x in a if x > 10)),
                          ("зон шире 15%", lambda a: sum(1 for x in a if x > 15))):
            o_, n_ = fn(W["w_old"]), fn(W["w_new"])
            print(f"{label:30}{o_:>10.1f}{n_:>10.1f}" if isinstance(o_, float) or isinstance(n_, float) else f"{label:30}{o_:>10}{n_:>10}")
        print(f"сопоставлено по месту: границы те же {C['same']}, границы другие {C['changed']}, "
              f"только в НОВОМ {C['only_new']}, только в СТАРОМ {C['only_old']}")
        ch = sorted([e for e in W["examples"] if e[1] == "границы"], key=lambda e: -e[0])
        if ch:
            print("  Зоны, которые стали ШИРЕ всего (разница в п.п. ширины):")
            for d_, what, c_, when, side, a_, b_, wo, wn in ch[:10]:
                print(f"    {c_:<9}{when} {side}  {wo:.1f}% -> {wn:.1f}%\n        было  {a_}\n        стало {b_}")
            print("  Зоны, которые стали УЖЕ всего:")
            for d_, what, c_, when, side, a_, b_, wo, wn in ch[::-1][:5]:
                print(f"    {c_:<9}{when} {side}  {wo:.1f}% -> {wn:.1f}%\n        было  {a_}\n        стало {b_}")
        on = [e for e in W["examples"] if e[1] == "только в новом"]
        for d_, what, c_, when, side, a_, b_, wo, wn in on[:6]:
            print(f"  только в НОВОМ: {c_} {when} {side} {b_}  ширина {wn:.1f}%")
    ex = sorted([e for _, r in rows for e in r["drift"]["old"]["examples"]], key=lambda e: -e[0])
    if ex:
        print("\nСТАРЫЙ (без реестра): примеры смены границ/id, самые большие сдвиги первыми (сдвиг в дневных ATR):")
        for shift, what, c_, when, side, a_, b_ in ex[:12]:
            print(f"  {c_:<9}{when} {side} {what}  сдвиг={shift:.2f} ATR\n      было {a_}\n      стало {b_}")

    worst = sorted(rows, key=lambda x: -x[1]["old"]["pairs"])[:10]
    for x in rows:
        if x[1]["new"]["pairs"] and x not in worst:
            worst.append(x)
    print(f"\n{'монета':<10}{'пар стар.':>10}{'пар нов.':>10}{'живых стар.':>13}{'живых нов.':>12}")
    for coin, r in worst:
        print(f"{coin:<10}{r['old']['pairs']:>10}{r['new']['pairs']:>10}{r['old']['alive']:>13}{r['new']['alive']:>12}")

    for coin, r in (worst[:5] if len(rows) <= 10 else []):
        print(f"\n===== {coin} =====")
        print("Пары в СТАРОМ:")
        for side, a, b in r["old"]["examples"]:
            print(f"  {'SUPPORT' if side == 'S' else 'RESIST '}  "
                  f"{_fz(a)}   <->   {_fz(b)}")
        print("Что сделал НОВЫЙ метод (первое появление за неделю):")
        for (side, what, zs, es), (when, res_z) in list(r["events"].items())[:15]:
            print(f"  {when}  {'S' if side == 'S' else 'R'}  зона {zs}")
            print(f"       {what} {es}")
            if res_z:
                print(f"       -> осталось: {res_z}")
        for m, name in (("old", "СТАРЫЙ"), ("new", "НОВЫЙ")):
            print(f"Последний снимок, {name}:")
            for side in ("R", "S"):
                for z in sorted(r["last_snap"][m][side], key=lambda q: -q["min"]):
                    print(f"  {side}  {_fz(z)}")
    bad = sorted([x for x in rows if x[1]["new"]["pairs"]], key=lambda x: -x[1]["new"]["pairs"])[:4]
    for coin, r in bad:
        print(f"\n##### ПАРЫ В НОВОМ: {coin}  (G={r['G']:.6g}) #####")
        for side, a, b in r["new"]["examples"]:
            print(f"  {'S' if side == 'S' else 'R'}  gap={_gap(a, b):.6g}  {_fz(a)}   <->   {_fz(b)}")
        print("Живые зоны НОВОГО:")
        for side, zs in r["new"]["alive_zones"].items():
            for z in zs:
                print(f"  {side}  {_fz(z)}  дата={z.get('date')}")
    print("\nПары в НОВОМ должны быть 0. Если не 0 — это ошибка метода, покажи вывод.")


# ----------------------------------------------------------------------------
# --warmup: проверка подхода «прогрев» (тестовая копия билдера levels_builder_warmup.py)
# ----------------------------------------------------------------------------
WARM_BUILDER = None
WIN = {"1M": 60, "1W": 150, "1d": 365, "4h": 200}


def _raw_points(mod, dfs, warm, alive=None, known=None):
    """Сырые точки всех слоёв ДО склейки, без фильтра по расстоянию до цены. warm=True — свечи с запасом
    проходят mod.prepare (ATR по всем, окна, все дневные свечи для макро)."""
    d1M, d1W, d1d, d4h = dfs
    full = None
    if warm:
        d1M, d1W, d1d, d4h, full = mod.prepare(d1M, d1W, d1d, d4h)
    idx = len(d1d) - 1
    price = float(d1d['close'].iloc[idx])
    d1 = d1d.copy()
    d1['atr'] = calculate_atr(d1, 14)
    a1 = d1['atr'].iloc[idx]
    if pd.isna(a1) or a1 == 0:
        a1 = price * 0.05
    if warm:
        raw = mod._collect_zones(d1M, d1W, d1, d4h, price, a1, float('inf'), idx, count_stats=False, df_1d_macro=full)
    else:
        raw = mod._collect_zones(d1M, d1W, d1, d4h, price, a1, float('inf'), idx, count_stats=False)
    out, dup = {}, set()
    for z in raw:
        k = (str(z.get('type')), str(z.get('date')), bool(z.get('_is_support')))
        if alive is not None and not z.get('mitigated'):
            alive.add(k)
        if known is not None:
            known[k] = z.get('_known')
        if k in out:
            dup.add(k)
        out[k] = (round(float(z['min']), 10), round(float(z['max']), 10))
    for k in dup:
        out.pop(k, None)
    return out


def _flip_class(side, a, z, raw_cur, alive_cur, dfs=None):
    """Почему id зоны сменился: смотрим на основу СТАРОЙ зоны (якорь|дата из её level_id) в сырых точках нового снимка."""
    parts = str(a.get('level_id') or '').split('|')
    if len(parts) < 3:
        return ("0. нет level_id", "")
    anchor, date = parts[1], parts[2]
    kb = (anchor, date, side == 'S')
    if kb not in raw_cur:
        if (anchor, date, side != 'S') in raw_cur:
            return ("1. основа исчезла из данных", "та же точка стала другой стороной (цена прошла уровень)")
        if 'MACRO' in anchor and dfs is not None:
            df_ = dfs[0] if anchor.startswith('1M') else dfs[1]
            if df_ is not None:
                dd_ = pd.to_datetime(df_['timestamp'], unit='ms').dt.strftime('%Y-%m-%d').to_numpy()
                pos_ = np.flatnonzero(dd_ == date)
                if len(pos_):
                    cl_ = df_['close'].to_numpy(dtype=float)[int(pos_[0]) + 3:]
                    tip_ = a['max'] if side == 'R' else a['min']
                    pierced_ = bool(len(cl_) and ((cl_ > tip_ * 0.998).any() if side == 'R' else (cl_ < tip_ * 1.002).any()))
                    return ("1. основа исчезла из данных", "MACRO пробит закрытием" if pierced_ else "MACRO исчез, закрытием НЕ пробит (проверить)")
        if anchor.startswith('4h'):
            sub = '4h-точка вышла из окна'
        elif anchor.startswith(('1M_high', '1M_low', '1M_close', '1W_high', '1W_low')):
            sub = 'смена месяца/недели (PMH/PML/PMC/PWH/PWL)'
        elif anchor.startswith('1d'):
            sub = '1d-точка вышла из окна'
        elif 'MACRO' in anchor:
            sub = 'MACRO'
        else:
            sub = 'другое'
        return ("1. основа исчезла из данных", sub)
    if kb not in alive_cur:
        return ("2. основа пробита закрытием", "")
    lo, hi = raw_cur[kb]
    if min(hi, z['max']) - max(lo, z['min']) <= 0:
        return ("3. основа жива, но новая зона её не содержит", "")
    nd = str(z.get('level_id') or '').split('|')
    nd_date = nd[2] if len(nd) > 2 else ''
    return ("4. основа жива в зоне, но основой стала другая точка", "новая основа старше" if nd_date < date else "новая основа не старше")


def _flicker(coin, trk, known, times, done, st8):
    """Раздел 8: точки W, которые между соседними снимками то есть, то нет.
    mерцание — жива в снимке a, НЕ жива в b, снова жива в c (a<b<c, все три посчитаны);
    поздно — первый раз жива в снимке s, хотя к предыдущему снимку (p) уже была известна (_known < время p)."""
    idx = sorted(done)
    for k, tr_ in trk.items():
        al, rw = tr_['alive'], tr_['inraw']
        seq = ''.join(('1' if al[i] else '0') for i in idx)
        lbl_ = ("MACRO " + k[0][:2]) if "MACRO" in k[0] else k[0]
        first = seq.find('1')
        if first < 0:
            continue
        last = seq.rfind('1')
        inner = seq[first:last + 1]
        if '0' in inner:
            st8['flick_n'] += 1
            st8['flick_by'][lbl_] = st8['flick_by'].get(lbl_, 0) + 1
            gap_i = [idx[j] for j in range(first, last + 1) if seq[j] == '0']
            in_raw = any(rw[i] for i in gap_i)
            kind = "в провале точка была в сырых (пробитая)" if in_raw else "в провале точки не было вообще"
            st8['flick_kind'][kind] = st8['flick_kind'].get(kind, 0) + 1
            if len(st8['flick_ex']) < 12:
                st8['flick_ex'].append((coin, k, seq, kind, [times[i].strftime('%m-%d %H:%M') for i in idx]))
        if first > 0:
            prev_t = int(times[idx[first - 1]].timestamp() * 1000)
            kn = known.get(k)
            if kn is not None and kn < prev_t:
                st8['late_n'] += 1
                st8['late_by'][lbl_] = st8['late_by'].get(lbl_, 0) + 1
                if len(st8['late_ex']) < 12:
                    st8['late_ex'].append((coin, k, seq, times[idx[first]].strftime('%m-%d %H:%M'),
                                           pd.to_datetime(kn, unit='ms').strftime('%m-%d %H:%M')))


def _wpct(z):
    return (z['max'] - z['min']) / ((z['max'] + z['min']) / 2) * 100


def run_warmup(cache, times, coins=None):
    W = WARM_BUILDER
    if W is None:
        raise RuntimeError("levels_builder_warmup.py не загружен")
    st = {"drift": {"A": 0, "W": 0}, "pairs": {"A": 0, "W": 0}, "ex_drift": {"A": [], "W": []},
          "win_same": 0, "win_diff": 0, "win_only": 0, "out_diff_win": 0, "ex_win": [],
          "cnt": {"A": {}, "W": {}}, "only_W": [], "only_A": [], "n_onlyW": 0, "n_onlyA": 0,
          "w": {"A": [], "W": []}, "fb": {}, "flip": {m: {"same_id": 0, "bounds_changed": 0, "id_flip": 0, "new": 0, "gone": 0} for m in ("A", "W")},
          "ex_flip": [], "f7": {"n": 0, "cls": {}, "ex": {}}, "f8": {"flick_n": 0, "flick_by": {}, "flick_kind": {}, "flick_ex": [], "late_n": 0, "late_by": {}, "late_ex": [], "pts": 0}, "raw_macro_n": 0, "wide": [], "out_same": 0, "out_diff": 0, "ex_out": []}
    lbl = lambda t: ("MACRO " + t[:2]) if "MACRO" in t else t
    for ci, (coin, tf) in enumerate(cache.items(), 1):
        if coins and coin not in coins:
            continue
        prev: dict = {"A": None, "W": None}
        prev_out: dict = {"A": None, "W": None}
        trk: dict = {}
        known_all: dict = {}
        done_sn: set = set()
        for sn, t in enumerate(times):
            ms = int(t.timestamp() * 1000)
            std = [_slice_by_time(tf[k], ms, WIN[k], k) for k in ("1M", "1W", "1d", "4h")]
            wrm = [_slice_by_time(tf[k], ms, WIN[k] + W.LOOKBACK[k], k) for k in ("1M", "1W", "1d", "4h")]
            if std[2] is None or len(std[2]) < 50:
                continue
            try:
                rA = _raw_points(NEW_BUILDER, std, False)
                W.MACRO_FB.clear()
                aliveW: set = set()
                knownW: dict = {}
                rW = _raw_points(W, wrm, True, aliveW, knownW)
                for k_, v_ in W.MACRO_FB.items():
                    st["fb"][k_] = st["fb"].get(k_, 0) + v_
                st["raw_macro_n"] += sum(1 for k_ in rW if "MACRO" in k_[0])
            except Exception as e:
                print(f"[WARMUP ERROR] {coin} {t}: {e}")
                continue
            when = t.strftime("%m-%d %H:%M")
            done_sn.add(sn)
            for k_ in rW:
                tk_ = trk.setdefault(k_, {"alive": [False] * len(times), "inraw": [False] * len(times)})
                tk_["inraw"][sn] = True
                if k_ in aliveW:
                    tk_["alive"][sn] = True
                known_all[k_] = knownW.get(k_)
            for m, r in (("A", rA), ("W", rW)):
                if prev[m] is not None:
                    for k, b in r.items():
                        if k in prev[m]:
                            st["pairs"][m] += 1
                            if prev[m][k] != b:
                                st["drift"][m] += 1
                                if len(st["ex_drift"][m]) < 6:
                                    st["ex_drift"][m].append((coin, when, k, prev[m][k], b))
                prev[m] = r
            if sn == len(times) - 1:   # потерянные точки и количества — по последнему снимку
                for m, r in (("A", rA), ("W", rW)):
                    for k in r:
                        st["cnt"][m][lbl(k[0])] = st["cnt"][m].get(lbl(k[0]), 0) + 1
                for k in rW:
                    if k not in rA:
                        st["n_onlyW"] += 1
                        if "MACRO" in k[0] or len(st["only_W"]) < 10:
                            st["only_W"].append((coin, k, rW[k]))
                for k in rA:
                    if k not in rW:
                        st["n_onlyA"] += 1
                        if len(st["only_A"]) < 10:
                            st["only_A"].append((coin, k, rA[k]))
            # выдача A и W + ширина
            try:
                oA = NEW_BUILDER.build_levels(std[0], std[1], std[2], std[3], coin)
                oW = W.build_levels(wrm[0], wrm[1], wrm[2], wrm[3], coin)
            except Exception as e:
                print(f"[WARMUP ERROR] {coin} {t}: {e}")
                continue
            for m, o in (("A", oA), ("W", oW)):
                st["w"][m].extend(_wpct(z) for z in o["supports"] + o["resistances"])
            for m, o in (("A", oA), ("W", oW)):
                cur_ = {"S": o["supports"], "R": o["resistances"]}
                if prev_out[m] is not None:
                    fl_: list = []
                    d_ = _drift(prev_out[m], cur_, 1.0, coin, t, fl_ if m == "W" else None)
                    for k_ in st["flip"][m]:
                        st["flip"][m][k_] += d_[k_]
                    if m == "W":
                        st["ex_flip"].extend(e_ for e_ in d_["examples"] if e_[1] == "id")
                        for sd_, a_, z_ in fl_:
                            c7 = _flip_class(sd_, a_, z_, rW, aliveW, wrm)
                            st["f7"]["n"] += 1
                            st["f7"]["cls"][c7] = st["f7"]["cls"].get(c7, 0) + 1
                            ex7 = st["f7"]["ex"].setdefault(c7, [])
                            if len(ex7) < 4:
                                ex7.append((coin, when, sd_, _fz(a_), _fz(z_), a_.get('level_id'), z_.get('level_id')))
                prev_out[m] = cur_
            for z_ in oW["supports"] + oW["resistances"]:
                if _wpct(z_) > 10:
                    st["wide"].append((_wpct(z_), coin, when, _fz(z_)))
            fa = sorted((sd, round(z['min'], 10), round(z['max'], 10)) for sd, key in (("S", "supports"), ("R", "resistances")) for z in oA[key])
            fw = sorted((sd, round(z['min'], 10), round(z['max'], 10)) for sd, key in (("S", "supports"), ("R", "resistances")) for z in oW[key])
            if fa == fw:
                st["out_same"] += 1
            else:
                st["out_diff"] += 1
                if len(st["ex_out"]) < 8:
                    st["ex_out"].append((coin, when, oA, oW))
            # независимость от длины запаса: тот же билдер, запас на 20 свечей больше
            if sn % 4 == 0:
                wrm2 = [_slice_by_time(tf[k], ms, WIN[k] + W.LOOKBACK[k] + 20, k) for k in ("1M", "1W", "1d", "4h")]
                rW2 = _raw_points(W, wrm2, True)
                for k in set(rW) | set(rW2):
                    if k in rW and k in rW2:
                        if rW[k] == rW2[k]:
                            st["win_same"] += 1
                        else:
                            st["win_diff"] += 1
                            if len(st["ex_win"]) < 6:
                                st["ex_win"].append((coin, when, k, rW[k], rW2[k]))
                    else:
                        st["win_only"] += 1
                oW2 = W.build_levels(wrm2[0], wrm2[1], wrm2[2], wrm2[3], coin)
                if oW2 != oW:
                    st["out_diff_win"] += 1
        st["f8"]["pts"] += len(trk)
        _flicker(coin, trk, known_all, times, done_sn, st["f8"])
        if ci % 10 == 0:
            print(f"   …{ci}/{len(cache)}")
    return st


def report_warmup(times, st):
    print(f"\nПРОГРЕВ: период {times[0]} .. {times[-1]} (снимков: {len(times)})")
    print("A = боевой билдер сейчас (с моей правкой ATR),  W = тестовая копия с прогревом")
    print("\n1) Сырые точки (до склейки) между соседними снимками: та же точка (тип+дата+сторона) — те же границы?")
    for m in ("A", "W"):
        print(f"   {m}: сравнено {st['pairs'][m]}, границы поменялись {st['drift'][m]}   (должно быть 0)")
        for c, w, k, a, b in st["ex_drift"][m]:
            print(f"      {c} {w} {k[0]} {k[1]} {'S' if k[2] else 'R'}  было {a}  стало {b}")
    print("\n2) W: больше старых свечей в запасе (+20) — результат должен быть тот же самый:")
    print(f"   сырых точек совпало {st['win_same']}, границы разные {st['win_diff']}, только в одном {st['win_only']}   (должно быть 0 и 0)")
    print(f"   снимков, где выдача отличается: {st['out_diff_win']}   (должно быть 0)")
    for c, w, k, a, b in st["ex_win"]:
        print(f"      {c} {w} {k[0]} {k[1]}  обычный запас {a}  больше запас {b}")
    print("\n3) Потерянные точки (последний снимок, все монеты):")
    print(f"   есть в W, нет в A (A выкинул): {st['n_onlyW']}      есть в A, нет в W: {st['n_onlyA']}   (должно быть 0)")
    labels = sorted(set(st["cnt"]["A"]) | set(st["cnt"]["W"]))
    print(f"   {'слой':<22}{'A':>7}{'W':>7}")
    for l_ in labels:
        print(f"   {l_:<22}{st['cnt']['A'].get(l_, 0):>7}{st['cnt']['W'].get(l_, 0):>7}")
    for c, k, b in st["only_W"][:15]:
        print(f"      только в W: {c:<9}{k[0]} {k[1]} {'S' if k[2] else 'R'} {b}")
    for c, k, b in st["only_A"][:10]:
        print(f"      только в A: {c:<9}{k[0]} {k[1]} {'S' if k[2] else 'R'} {b}")
    print("\n4) Выдача (2 зоны на сторону) A против W:")
    print(f"   снимков с одинаковой выдачей {st['out_same']}, с разной {st['out_diff']}")
    print(f"   {'ширина зон':<22}{'A':>8}{'W':>8}")
    for name_, fn in (("медиана, %", lambda a: float(np.median(a)) if a else 0.0),
                      ("90-й проц., %", lambda a: float(np.percentile(a, 90)) if a else 0.0),
                      ("максимум, %", lambda a: max(a) if a else 0.0),
                      ("шире 10%, шт", lambda a: float(sum(1 for x in a if x > 10))),
                      ("шире 15%, шт", lambda a: float(sum(1 for x in a if x > 15)))):
        print(f"   {name_:<22}{fn(st['w']['A']):>8.1f}{fn(st['w']['W']):>8.1f}")
    print("\n6) Выдача между соседними снимками (то, что видит менеджер): A = основа зоны — самая сильная точка, "
          "W = основа — первая точка на месте")
    print(f"   {'':40}{'A':>8}{'W':>8}")
    for lbl_, k_ in (("та же зона (тот же id) в двух снимках", "same_id"), ("  из них границы поменялись", "bounds_changed"),
                     ("то же место, но id другой (новая зона)", "id_flip"), ("зона появилась на новом месте", "new"),
                     ("зона ушла совсем", "gone")):
        print(f"   {lbl_:40}{st['flip']['A'][k_]:>8}{st['flip']['W'][k_]:>8}")
    for _, _, c_, wh_, sd_, a_, b_ in st["ex_flip"][:10]:
        print(f"      W {c_:<9}{wh_} {sd_}\n         было  {a_}\n         стало {b_}")
    f7 = st["f7"]
    print(f"\n7) Почему сменился id у W (то же место, другой id): всего {f7['n']}")
    for (c_, sub_), n_ in sorted(f7["cls"].items()):
        print(f"   {c_}{(' — ' + sub_) if sub_ else ''}: {n_}")
    for key_, exs_ in sorted(f7["ex"].items()):
        print(f"   --- {key_[0]}{(' — ' + key_[1]) if key_[1] else ''}")
        for c_, wh_, sd_, a_, z_, ia_, iz_ in exs_:
            print(f"      {c_} {wh_} {sd_}\n         было  {a_}   id {ia_}\n         стало {z_}   id {iz_}")
    f8 = st["f8"]
    print(f"\n8) Мерцание сырых точек W (всего разных точек: {f8['pts']}):")
    print(f"   жива -> нет -> жива снова: {f8['flick_n']}   (должно быть 0)")
    for k_, n_ in sorted(f8["flick_by"].items()):
        print(f"      {k_}: {n_}")
    for k_, n_ in f8["flick_kind"].items():
        print(f"      {k_}: {n_}")
    print(f"   появилась позже, чем стала известна: {f8['late_n']}   (должно быть 0)")
    for k_, n_ in sorted(f8["late_by"].items()):
        print(f"      {k_}: {n_}")
    for c_, k_, seq_, kind_, wh_ in f8["flick_ex"]:
        print(f"   --- мерцает {c_} {k_[0]} {k_[1]} {'S' if k_[2] else 'R'}  по снимкам: {seq_}   ({kind_})")
    for c_, k_, seq_, first_, kn_ in f8["late_ex"]:
        print(f"   --- поздно {c_} {k_[0]} {k_[1]} {'S' if k_[2] else 'R'}  по снимкам: {seq_}  впервые {first_}, известна с {kn_}")
    fb = st["fb"]
    print("\n5) Макро-зоны, построенные НЕ по дневной свече (запасной путь), в W — за все снимки:")
    print(f"   всего макро-точек (до склейки, сумма по снимкам): {st['raw_macro_n']}")
    print(f"   запасной путь: нет дневной свечи {fb.get('nodaily', 0)}, экстремум дня не совпал с ценой свинга {fb.get('mismatch', 0)}"
          f"   (1M: {fb.get('1M', 0)}, 1W: {fb.get('1W', 0)})")
    wd = sorted(st["wide"], reverse=True)
    print(f"   зон шире 10% в выдаче W: {len(wd)} (по снимкам); самые широкие:")
    seen_ = set()
    for w_, c_, wh_, z_ in wd:
        if (c_, z_) in seen_:
            continue
        seen_.add((c_, z_))
        print(f"      {w_:5.1f}%  {c_:<9}{wh_}  {z_}")
        if len(seen_) >= 12:
            break
    for c, w, oA, oW in st["ex_out"]:
        print(f"   --- {c} {w}")
        for m, o in (("A", oA), ("W", oW)):
            for z in sorted(o["resistances"], key=lambda q: -q["min"]):
                print(f"      {m} R {_wpct(z):5.1f}%  {_fz(z)}")
            for z in sorted(o["supports"], key=lambda q: -q["min"]):
                print(f"      {m} S {_wpct(z):5.1f}%  {_fz(z)}")


def build_timeline_w(cache, month):
    """Месяц уровней для симулятора тестовой копией W, БЕЗ реестра: каждый снимок считается с нуля.
    Формат и даты — как в precalc_for_bot.py (снимки каждые 12ч с 1-го числа 00:00 по последнее число 00:00)."""
    import json
    import shutil
    import datetime as _dt
    from backend.modules.cryptano.utils.paths import DATABASE_DIR
    W = WARM_BUILDER
    if W is None:
        raise RuntimeError("levels_builder_warmup.py не загружен")
    start = pd.Timestamp(month + "-01")
    end = (start + pd.offsets.MonthEnd(1)).normalize()
    dates = pd.date_range(start=start, end=end, freq="12h")
    out_path = os.path.join(DATABASE_DIR, f"levels_timeline_{start.strftime('%Y_%m')}.json")
    backup = out_path.replace(".json", "_before_w.json")
    if os.path.exists(out_path) and not os.path.exists(backup):
        shutil.copyfile(out_path, backup)
        print(f"💾 Старый файл сохранён как {backup}")
    elif os.path.exists(backup):
        print(f"💾 Копия старого файла уже есть: {backup} (не перезаписываю)")
    timeline = {}
    for dt in dates:
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        ms = int(dt.tz_localize("UTC").timestamp() * 1000)
        snap = {}
        for coin, tf in cache.items():
            try:
                dfs = [_slice_by_time(tf[k], ms, WIN[k] + W.LOOKBACK[k], k) for k in ("1M", "1W", "1d", "4h")]
                if dfs[2] is None or len(dfs[2]) < 50:
                    continue
                lv = W.build_levels(dfs[0], dfs[1], dfs[2], dfs[3], coin)
                if lv["supports"] or lv["resistances"]:
                    snap[coin] = {"supports": lv["supports"], "resistances": lv["resistances"],
                                  "updated_at": _dt.datetime.now().isoformat()}
            except Exception as e:
                print(f"[TIMELINE ERROR] {coin} {time_str}: {e}")
        timeline[time_str] = snap
        print(f"⏳ {time_str}: монет с уровнями {len(snap)}")
    with open(out_path, "w") as f:
        json.dump(timeline, f, indent=2)
    print(f"\n✅ Записано: {out_path}  (снимков {len(timeline)}, считала тестовая копия W без реестра)")


def main():
    global GAP_ATR
    ap = argparse.ArgumentParser(description="Старый и новый способ получения уровней за неделю")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", default="", help="ГГГГ-ММ-ДД; по умолчанию — неделя до сегодняшней полуночи (UTC)")
    ap.add_argument("--gap", type=float, default=GAP_ATR, help="зазор G в дневных ATR (по умолчанию 0.5)")
    ap.add_argument("--explain", default="", help="ДИАГНОСТИКА: даты через запятую 'ГГГГ-ММ-ДД ЧЧ:ММ' (нужна --coins)")
    ap.add_argument("--coins", default="", help="тикеры через запятую; по умолчанию вся whitelist бота")
    ap.add_argument("--old-builder", default=os.path.join(CURRENT_DIR, "levels_builder_before_atrfix.py"),
                    help="путь к старому билдеру (до правки ATR) для сравнения ширины зон; нет файла — секция пропускается")
    ap.add_argument("--timeline", default="", help="ГГГГ-ММ: пересчитать месяц тестовой копией W БЕЗ реестра и записать "
                    "levels_timeline_ГГГГ_ММ.json для симулятора (старый файл сначала копируется в *_before_w.json)")
    ap.add_argument("--warmup", action="store_true", help="проверка подхода «прогрев» (levels_builder_warmup.py рядом); основной отчёт не считается")
    ap.add_argument("--dump", default="", help="ДИАГНОСТИКА: моменты 'ГГГГ-ММ-ДД ЧЧ:ММ' через запятую (нужна --coins): полные списки зон до/после")
    args = ap.parse_args()
    GAP_ATR = args.gap
    global DUMP_AT
    DUMP_AT = {x.strip() for x in args.dump.split(",") if x.strip()}
    global OLD_BUILDER
    if os.path.exists(args.old_builder):
        OLD_BUILDER = _load_old_builder(args.old_builder)
        print(f"Старый билдер для сравнения ширины: {args.old_builder}")
    else:
        print(f"Старый билдер не найден ({args.old_builder}) — секция «ширина зон» пропущена")
    print(f"Параметры: зазор G = {GAP_ATR} ATR")

    import precalc_for_bot as pre  # лежит рядом; тот же отбор монет и тот же кэш свечей

    if args.timeline:
        start = pd.Timestamp(args.timeline + "-01")
    elif args.start:
        start = pd.Timestamp(args.start)
    else:
        start = pd.Timestamp.utcnow().tz_localize(None).normalize() - pd.Timedelta(days=args.days)
    coins = {c.strip().upper() for c in args.coins.split(",") if c.strip()} or None

    symbols = pre.get_valid_symbols()
    if coins:
        symbols = [s for s in symbols if s.split("/")[0].replace(":USDT", "").upper() in coins]
    days_span = max((pd.Timestamp.now() - start).days, 0)
    cache = pre.build_full_history_cache(symbols, extra_candles=max(1500, (days_span + 40) * 6))

    if args.warmup or args.timeline:
        global WARM_BUILDER
        wp = os.path.join(CURRENT_DIR, "levels_builder_warmup.py")
        WARM_BUILDER = _load_old_builder(wp)
        # в общем кэше 1M/1W ровно 60/150 свечей — для прогрева читаем из той же базы с запасом (сеть не трогается)
        lb = WARM_BUILDER.LOOKBACK
        big = {"1M": 60 + lb["1M"] + days_span // 28 + 4, "1W": 150 + lb["1W"] + days_span // 7 + 4,
               "1d": 365 + lb["1d"] + days_span + 10, "4h": 200 + lb["4h"] + days_span * 6 + 40}
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        for sym in symbols:
            coin_ = sym.split("/")[0].replace(":USDT", "")
            if coin_ not in cache:
                continue
            for tf_, n_ in big.items():
                try:
                    rows_ = pre._safe_fetch_ohlcv(sym, tf_, n_)
                    if rows_:
                        cache[coin_][tf_] = pd.DataFrame(rows_, columns=cols)
                except Exception as e:
                    print(f"[WARMUP LOAD] {coin_} {tf_}: {e}")
        if args.timeline:
            build_timeline_w(cache, args.timeline)
            return
        times = list(pd.date_range(start=start, periods=args.days * 2, freq="12h"))
        st = run_warmup(cache, times, coins)
        report_warmup(times, st)
        return

    explain_at = {x.strip() for x in args.explain.split(',') if x.strip()} or None
    times, rows = run_test(cache, start, args.days, coins, explain_at)
    report(times, rows)


if __name__ == "__main__":
    main()