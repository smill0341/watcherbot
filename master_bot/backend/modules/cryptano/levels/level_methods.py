"""Тестовые методы построения уровней (только отображение на графике).

Это ЧИСТЫЙ ТЕСТ: ничего не пишется в macro_levels.json, бот и levels_builder
эти зоны не читают, сделки по ним не открываются.

Метод "ranked" — порт индикатора «Ranked Support & Resistance Zones»
(Zeiierman, TradingView, CC BY-NC-SA 4.0, лицензия некоммерческая) с теми же
значениями по умолчанию. Порт идёт свеча за свечой, как Pine-скрипт:
  пивот (5/5) -> зона пивот ± 0.2 ATR -> слияние близких -> балл 0..100 ->
  пробой по закрытию за зоной на 0.12 ATR -> удаление; максимум 60 зон хранится.

Отличие от TradingView: считаем не от начала всей истории, а только по
последним LOOKBACK_BARS свечам. Зона живёт максимум 450 свечей, поэтому более
старая история на результат почти не влияет, зато считается быстро.
"""
import math

import numpy as np

LOOKBACK_BARS = 2000

# Значения по умолчанию из Pine-скрипта
RANKED = dict(
    pivot_span=5,        # Pivot Length
    min_swing_atr=0.15,  # Min Pivot ATR
    absorb_atr=0.55,     # Absorb Similar Zones
    zone_width_atr=0.40,  # Zone Width ATR
    vol_len=20,          # Volume MA Length
    trend_len=50,        # Trend EMA Length
    break_atr=0.12,      # Break Close Buffer ATR
    keep_broken=4,       # Keep Broken Zones
    stored_limit=60,     # Max Stored Zones
    max_age=450,         # зона удаляется после стольких свечей
    atr_len=14,
)


class _Zone:
    """Одна зона. Явные типы полей — чтобы индексы (born_i) не путались с float."""
    __slots__ = ("top", "bottom", "mid", "side_dir", "born_i", "broke_i", "width",
                 "mitig", "touches", "vol_s", "trend_s", "swing_s", "score")

    def __init__(self, top: float, bottom: float, mid: float, side_dir: int, born_i: int,
                 width: float, vol_s: float, trend_s: float, swing_s: float, score: float):
        self.top: float = top
        self.bottom: float = bottom
        self.mid: float = mid
        self.side_dir: int = side_dir      # 1 = сопротивление, -1 = поддержка
        self.born_i: int = born_i
        self.broke_i: int = -1
        self.width: float = width
        self.mitig: float = 0.0
        self.touches: float = 0.0
        self.vol_s: float = vol_s
        self.trend_s: float = trend_s
        self.swing_s: float = swing_s
        self.score: float = score


def _secs(t):
    return t / 1000.0 if t > 9999999999 else float(t)


def _wilder_atr(high, low, close, length):
    """ta.atr(): RMA от True Range, первое значение — SMA первых `length` TR."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n >= length:
        atr[length - 1] = tr[:length].mean()
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def _sma(x, length):
    n = len(x)
    out = np.full(n, np.nan)
    if n >= length:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[length - 1:] = (cs[length:] - cs[:-length]) / length
    return out


def _ema(x, length):
    """ta.ema(): стартует с первого значения, alpha = 2/(length+1)."""
    a = 2.0 / (length + 1)
    out = np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _is_pivot_high(h, p, span):
    c = h[p]
    for k in range(1, span + 1):
        if h[p - k] >= c or h[p + k] > c:
            return False
    return True


def _is_pivot_low(l, p, span):
    c = l[p]
    for k in range(1, span + 1):
        if l[p - k] <= c or l[p + k] < c:
            return False
    return True


def ranked_zones(candles, params=None, on_bar=None, use_all=False):
    """Возвращает {"zones": [...активные, по убыванию балла...], "broken": [...], "meta": {...}}.
    on_bar(i, zones, atr) — необязательный колбэк после каждой свечи (нужен тесту качества зон);
    use_all=True — считать по всем переданным свечам, без обрезки до LOOKBACK_BARS."""
    P = dict(RANKED)
    if params:
        P.update(params)
    span = int(P["pivot_span"])
    if not candles or len(candles) < max(int(P["trend_len"]), 60):
        return {"zones": [], "broken": [], "meta": {"bars": len(candles or [])}}

    cs = candles if use_all else candles[-LOOKBACK_BARS:]
    n = len(cs)
    times = [c["time"] for c in cs]
    o = np.array([float(c["open"]) for c in cs])
    h = np.array([float(c["high"]) for c in cs])
    l = np.array([float(c["low"]) for c in cs])
    cl = np.array([float(c["close"]) for c in cs])
    vol = np.array([float(c.get("volume") or 0.0) for c in cs])

    atr_arr = _wilder_atr(h, l, cl, int(P["atr_len"]))
    vol_base = _sma(vol, int(P["vol_len"]))
    trend = _ema(cl, int(P["trend_len"]))

    first_bar = int(max(P["atr_len"], P["vol_len"], span * 2))  # раньше индикаторы ещё не прогреты
    zones: list = []     # активные
    broken: list = []    # недавно пробитые (хранятся keep_broken штук)

    def swing_quality(i, price, d, atr):
        p = i - span
        if d == 1:
            return max((price - max(h[p + 1], h[p - 1])) / atr, 0.0)
        return max((min(l[p + 1], l[p - 1]) - price) / atr, 0.0)

    def score_fn(width, vol_s, trend_s, swing_s, mitig, touches, age, atr):
        size_n = min(width / atr, 1.0)
        vol_n = min(vol_s / 2.0, 1.0)
        swing_n = min(swing_s / 1.5, 1.0)
        touch_n = min(touches / 4.0, 1.0)
        age_n = min(age / max(P["max_age"], 1), 1.0)
        raw = size_n * 20.0 + vol_n * 18.0 + trend_s * 12.0 + swing_n * 28.0 + touch_n * 16.0 + 10.0
        pen = mitig * 22.0 + age_n * 10.0
        return max(min(raw - pen, 100.0), 0.0)

    def overlaps(z, top, bottom, d, atr):
        ov = max(min(z.top, top) - max(z.bottom, bottom), 0.0)
        tiny = 1e-12
        smaller = min(max(z.top - z.bottom, tiny), max(top - bottom, tiny))
        same = z.side_dir == d
        close_mid = abs(z.mid - (top + bottom) / 2.0) <= P["absorb_atr"] * atr
        return same and (close_mid or ov / smaller >= 0.35)

    def add_zone(i, price, d, atr):
        p = i - span
        half = P["zone_width_atr"] * atr * 0.5
        top, bottom = price + half, price - half
        vb = vol_base[i]
        vol_s = float(vol[p] / vb) if (vb and not math.isnan(vb) and vb != 0) else 1.0
        trend_s = 1.0 if ((d == -1 and price > trend[i]) or (d == 1 and price < trend[i])) else 0.0
        sw = swing_quality(i, price, d, atr)
        for z in zones:
            if overlaps(z, top, bottom, d, atr):
                z.top = max(z.top, top)
                z.bottom = min(z.bottom, bottom)
                z.mid = (z.top + z.bottom) / 2.0
                z.width = max(z.top - z.bottom, 1e-12)
                z.born_i = min(z.born_i, p)
                z.vol_s = max(z.vol_s, vol_s)
                z.trend_s = max(z.trend_s, trend_s)
                z.swing_s = max(z.swing_s, sw)
                z.mitig = 0.0
                z.touches += 1.0
                return
        width = max(top - bottom, 1e-12)
        zones.append(_Zone(
            top=top, bottom=bottom, mid=price, side_dir=d, born_i=p, width=width,
            vol_s=vol_s, trend_s=trend_s, swing_s=sw,
            score=score_fn(width, vol_s, trend_s, sw, 0.0, 0.0, span, atr),
        ))

    for i in range(first_bar, n):
        atr = atr_arr[i]
        if not atr or math.isnan(atr) or atr <= 0:
            continue
        p = i - span
        if p >= span:
            if _is_pivot_high(h, p, span) and swing_quality(i, h[p], 1, atr) >= P["min_swing_atr"]:
                add_zone(i, h[p], 1, atr)
            if _is_pivot_low(l, p, span) and swing_quality(i, l[p], -1, atr) >= P["min_swing_atr"]:
                add_zone(i, l[p], -1, atr)

        for k in range(len(zones) - 1, -1, -1):
            z = zones[k]
            age = i - z.born_i
            expired = age > P["max_age"]
            buf = P["break_atr"] * atr
            b_sup = z.side_dir == -1 and cl[i] < z.bottom - buf
            b_res = z.side_dir == 1 and cl[i] > z.top + buf
            touched = h[i] >= z.bottom and l[i] <= z.top
            if touched and not b_sup and not b_res:
                z.touches += 0.20
                fill = (z.top - l[i]) if z.side_dir == -1 else (h[i] - z.bottom)
                z.mitig = min(max(fill / max(z.width, 1e-12), 0.0), 1.0)
            z.score = score_fn(z.width, z.vol_s, z.trend_s, z.swing_s,
                                  z.mitig, z.touches, age, atr)
            if expired:
                zones.pop(k)
            elif b_sup or b_res:
                z.broke_i = i
                zones.pop(k)
                if int(P["keep_broken"]) > 0:
                    broken.append(z)
                    while len(broken) > int(P["keep_broken"]):
                        broken.pop(0)

        zones.sort(key=lambda zz: -zz.score)  # sort стабильный, как в Pine
        while len(zones) > int(P["stored_limit"]):
            zones.pop()
        if on_bar is not None:   # для тестов: состояние зон после каждой свечи (список мутируется — читать сразу)
            on_bar(i, zones, float(atr))

    last_atr = float(atr_arr[-1]) if not math.isnan(atr_arr[-1]) else None

    def out(z, active):
        d = {
            "top": float(z.top), "bottom": float(z.bottom), "mid": float(z.mid),
            "side": "resistance" if z.side_dir == 1 else "support",
            "score": round(float(z.score), 1),
            "touches": round(float(z.touches), 1),
            "mitigation": round(float(z.mitig), 2),
            "born_time": times[z.born_i],
            "end_time": times[-1] if active else times[z.broke_i],
            "age_bars": int(n - 1 - z.born_i),
        }
        return d

    res_active = []
    for rank, z in enumerate(zones, 1):
        d = out(z, True)
        d["rank"] = rank
        res_active.append(d)
    res_broken = [out(z, False) for z in broken]
    return {
        "zones": res_active,
        "broken": res_broken,
        "meta": {"bars": n, "atr": last_atr, "last_time": times[-1], "last_close": float(cl[-1])},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Метод "profile" — порт «Buyers & Sellers Profile + Dynamic S/R» (Zeiierman,
# TradingView, CC BY-NC-SA 4.0, лицензия некоммерческая), значения по умолчанию.
# Идея: пивот -> профиль объёма по 600 свечам на момент пивота -> уровень
# подтягивается к ближайшему объёмному узлу -> балл 0..100 -> живёт до пробоя
# закрытием за линией на 0.10 ATR; максимум 20 уровней, слабейший удаляется.
# Это ЛИНИИ (не зоны). Считаем по последним PROFILE_LOOKBACK_BARS свечам.
# ─────────────────────────────────────────────────────────────────────────────
PROFILE_LOOKBACK_BARS = 5000

PROFILE = dict(
    prof_lb=600,      # Lookback профиля, свечей
    prof_rows=72,     # число ценовых строк профиля
    swing_len=5,      # Swing Length
    srch_atr=1.20,    # Search ATR: радиус поиска узла вокруг пивота
    node_min=0.08,    # Min Node: сила узла относительно POC
    evid_min=0.22,    # Min Evidence
    pull_max=0.85,    # Profile Pull: на какую долю пути к узлу сдвигаем уровень
    score_min=30.0,   # Min Score
    max_lvl=20,       # Max Levels
    merge_atr=0.16,   # Merge ATR: дубли одной стороны ближе этого — заменяются сильнейшим
    break_atr=0.10,   # Break ATR: буфер для пробоя по закрытию
    vol_len=40,
    atr_len=14,
)
_ABS_BODY_MAX = 0.40
_ABS_VOL_X = 1.5
_PIV_VOL_X = 2.2


def _clip(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class _Lvl:
    __slots__ = ("p", "sc", "raw", "node", "evid", "n_str", "supp", "born_i")

    def __init__(self, p: float, sc: float, raw: float, node: float, evid: float,
                 n_str: float, supp: bool, born_i: int):
        self.p: float = p
        self.sc: float = sc
        self.raw: float = raw
        self.node: float = node
        self.evid: float = evid
        self.n_str: float = n_str
        self.supp: bool = supp
        self.born_i: int = born_i


def _volume_profile(h, l, cl, vol, a: int, b: int, rows: int):
    """Профиль объёма по свечам a..b (включительно). Объём свечи делится по строкам
    пропорционально пересечению диапазона свечи со строкой; покупки — по положению
    закрытия внутри свечи. Возвращает (bot, step, tot, buy)."""
    hh = h[a:b + 1]
    ll = l[a:b + 1]
    cc = cl[a:b + 1]
    vv = np.maximum(vol[a:b + 1], 0.0)
    lo = float(ll.min())
    hi = float(hh.max())
    tick = max(abs(hi), 1.0) * 1e-9
    span = max(hi - lo, tick * rows)
    bot = lo - (span - (hi - lo)) * 0.5
    step = span / rows
    rng = hh - ll
    pos = rng > 0
    bf = np.where(pos, np.clip((cc - ll) / np.where(pos, rng, 1.0), 0.0, 1.0), 0.5)
    row_lo = bot + np.arange(rows) * step                        # нижняя граница каждой строки
    ov = np.minimum(hh[:, None], row_lo[None, :] + step) - np.maximum(ll[:, None], row_lo[None, :])
    ov = np.maximum(ov, 0.0)
    frac = np.where(pos[:, None], ov / np.where(pos, rng, 1.0)[:, None], 0.0)
    if (~pos).any():                                              # свеча без диапазона — целиком в одну строку
        ridx = np.clip(np.floor((ll - bot) / step).astype(int), 0, rows - 1)
        for k in np.nonzero(~pos)[0]:
            frac[k, ridx[k]] = 1.0
    av = vv[:, None] * frac
    tot = av.sum(axis=0)
    buy = (av * bf[:, None]).sum(axis=0)
    return bot, step, tot, buy


def _profile_node(raw: float, supp: bool, atr_p: float, bot: float, step: float, tot, buy, P):
    rows = len(tot)
    mx = max(float(tot.max()), 1e-7)
    rad = max(atr_p * P["srch_atr"], step * 2.0)
    best_p, best_e, best_s = raw, -1.0, 0.0
    for r in range(rows):
        pr = bot + (r + 0.5) * step
        d = abs(pr - raw)
        if d > rad:
            continue
        rv = float(tot[r])
        strength = rv / mx
        lv = float(tot[r - 1]) if r > 0 else rv
        rv2 = float(tot[r + 1]) if r < rows - 1 else rv
        n_avg = max((lv + rv2) * 0.5, mx * 0.01)
        pk = _clip((rv / n_avg - 0.85) / 0.65)
        b_sh = _clip(float(buy[r]) / rv) if rv > 0 else 0.5
        direction = b_sh if supp else 1.0 - b_sh
        prox = 1.0 - d / rad
        e = 0.50 * strength + 0.20 * pk + 0.20 * direction + 0.10 * prox
        if e > best_e:
            best_e, best_p, best_s = e, pr, strength
    return best_p, max(best_e, 0.0), best_s


def profile_levels(candles, params=None):
    """Возвращает {"zones": [...линии по убыванию балла...], "broken": [], "meta": {...}}.
    Каждая "зона" здесь — линия: top == bottom == цена уровня, kind = "line"."""
    P = dict(PROFILE)
    if params:
        P.update(params)
    span_n = int(P["swing_len"])
    rows = int(P["prof_rows"])
    prof_lb = int(P["prof_lb"])
    if not candles or len(candles) < 80:
        return {"zones": [], "broken": [], "meta": {"bars": len(candles or [])}}

    cs = candles[-PROFILE_LOOKBACK_BARS:]
    n = len(cs)
    times = [c["time"] for c in cs]
    o = np.array([float(c["open"]) for c in cs])
    h = np.array([float(c["high"]) for c in cs])
    l = np.array([float(c["low"]) for c in cs])
    cl = np.array([float(c["close"]) for c in cs])
    vol = np.array([float(c.get("volume") or 0.0) for c in cs])

    atr_arr = _wilder_atr(h, l, cl, int(P["atr_len"]))
    avg_vol = _sma(vol, int(P["vol_len"]))
    abs_rad = min(3, span_n)
    ready_from = int(P["vol_len"]) + span_n * 2 + 5               # bar_index > ... как в Pine
    tick = max(float(np.max(h)), 1.0) * 1e-9

    lvls: list = []

    def pivot_features(p: int, supp: bool):
        vb = avg_vol[p] if not math.isnan(avg_vol[p]) else vol[p]
        vr = (max(vol[p], 0.0) / vb) if vb > 0 else 1.0
        v_sc = _clip((vr - 0.8) / (_PIV_VOL_X - 0.8))
        pr = max(h[p] - l[p], tick)
        wick = (min(o[p], cl[p]) - l[p]) if supp else (h[p] - max(o[p], cl[p]))
        w_sc = _clip((wick / pr) / 0.50)
        abs_sc = 0.0
        for q in range(p - abs_rad, p + abs_rad + 1):
            cr = max(h[q] - l[q], tick)
            v = max(vol[q], 0.0)
            base = avg_vol[q] if not math.isnan(avg_vol[q]) else v
            v_rat = (v / base) if base > 0 else 1.0
            b_rat = abs(cl[q] - o[q]) / cr
            small = _clip((_ABS_BODY_MAX - b_rat) / _ABS_BODY_MAX)
            av = _clip((v_rat - 1.0) / (_ABS_VOL_X - 1.0))
            abs_sc = max(abs_sc, math.sqrt(small * av))
        return v_sc, w_sc, abs_sc

    def store(price, raw, node, evid, n_str, supp, sc, born_i, atr_p):
        tol = max(atr_p * P["merge_atr"], tick)
        best_k, best_d = -1, 1e20
        for k, lv in enumerate(lvls):
            if lv.supp == supp:
                d = abs(lv.p - price)
                if d <= tol and d < best_d:
                    best_d, best_k = d, k
        if best_k >= 0:
            lv = lvls[best_k]
            if sc > lv.sc:
                lv.p, lv.sc, lv.raw, lv.node, lv.evid, lv.n_str, lv.born_i = price, sc, raw, node, evid, n_str, born_i
        else:
            lvls.append(_Lvl(price, sc, raw, node, evid, n_str, supp, born_i))

    def pivot_eval(i: int, raw: float, supp: bool):
        p = i - span_n
        bars = max(1, min(prof_lb, p + 1))
        a = p - bars + 1
        bot, step, tot, buy = _volume_profile(h, l, cl, vol, a, p, rows)
        atr_p = float(atr_arr[p]) if not math.isnan(atr_arr[p]) else tick
        atr_p = max(atr_p, tick)
        node, evid, n_str = _profile_node(raw, supp, atr_p, bot, step, tot, buy, P)
        v_sc, w_sc, a_sc = pivot_features(p, supp)
        ok = n_str >= P["node_min"] and evid >= P["evid_min"]
        pull = P["pull_max"] * _clip(evid / 0.65) if ok else 0.0
        price = raw + (node - raw) * pull
        sc = 100.0 * (0.50 * evid + 0.20 * a_sc + 0.15 * v_sc + 0.15 * w_sc)
        if ok and sc >= P["score_min"]:
            store(price, raw, node, evid, n_str, supp, sc, p, atr_p)

    for i in range(max(ready_from + 1, 2 * span_n + 1), n):
        p = i - span_n
        if p >= span_n:
            if _is_pivot_low(l, p, span_n):
                pivot_eval(i, float(l[p]), True)
            if _is_pivot_high(h, p, span_n):
                pivot_eval(i, float(h[p]), False)
        atr = atr_arr[i]
        buf = (float(atr) if not math.isnan(atr) else 0.0) * P["break_atr"]
        for k in range(len(lvls) - 1, -1, -1):
            lv = lvls[k]
            broken = (cl[i] < lv.p - buf) if lv.supp else (cl[i] > lv.p + buf)
            if broken:
                lvls.pop(k)
        while len(lvls) > int(P["max_lvl"]):
            weak = 0
            for k in range(1, len(lvls)):
                if lvls[k].sc < lvls[weak].sc:
                    weak = k
            lvls.pop(weak)

    out = []
    for rank, lv in enumerate(sorted(lvls, key=lambda x: -x.sc), 1):
        out.append({
            "kind": "line",
            "top": float(lv.p), "bottom": float(lv.p), "mid": float(lv.p),
            "side": "support" if lv.supp else "resistance",
            "score": round(float(lv.sc), 1),
            "raw": float(lv.raw), "node": float(lv.node),
            "evidence": round(float(lv.evid), 2),
            "born_time": times[lv.born_i], "end_time": times[-1],
            "age_bars": int(n - 1 - lv.born_i),
            "rank": rank,
        })
    last_atr = float(atr_arr[-1]) if not math.isnan(atr_arr[-1]) else None
    return {"zones": out, "broken": [],
            "meta": {"bars": n, "atr": last_atr, "last_time": times[-1], "last_close": float(cl[-1])}}


# ─────────────────────────────────────────────────────────────────────────────
# Метод "matrix" — порт «Premium Discount Matrix» (BOSWaves, TradingView,
# Mozilla Public License 2.0), значения по умолчанию.
# Это не поиск уровней, а карта ТЕКУЩЕГО диапазона: последний свинг-хай и
# свинг-лоу (8/8) задают диапазон; середина (EQ ±5%) делит его на premium (выше,
# зона шортов) и discount (ниже, зона лонгов); диапазон режется на 16 строк, каждая
# получает оценку по числу внутренних пивотов (3/3) и силе их отскока.
# Это снимок на последнюю свечу (как в Pine: рисуется только на последнем баре).
# ─────────────────────────────────────────────────────────────────────────────
MATRIX_LOOKBACK_BARS = 1500

MATRIX = dict(
    swing_len=8,         # Swing Length: свинги, задающие диапазон
    max_range_bars=250,  # Maximum Range History
    atr_len=14,
    rows=16,             # Matrix Rows
    int_pivot_len=3,     # Internal Pivot Length
    pivot_memory=80,     # Internal Pivot Memory
    density_w=0.60,      # Pivot Density Weight
    reject_w=0.40,       # Rejection Weight
    eq_width=0.10,       # Equilibrium Width (доля диапазона)
)


def matrix_levels(candles, params=None):
    """{"zones": [строки матрицы со score>0, по убыванию балла], "lines": [RANGE HIGH/LOW/EQ], "meta": {...}}.
    Строка: side = resistance (premium) / support (discount) / eq."""
    P = dict(MATRIX)
    if params:
        P.update(params)
    sw = int(P["swing_len"])
    ip = int(P["int_pivot_len"])
    rows = int(P["rows"])
    if not candles or len(candles) < 60:
        return {"zones": [], "lines": [], "broken": [], "meta": {"bars": len(candles or [])}}

    cs = candles[-MATRIX_LOOKBACK_BARS:]
    n = len(cs)
    times = [c["time"] for c in cs]
    o = np.array([float(c["open"]) for c in cs])
    h = np.array([float(c["high"]) for c in cs])
    l = np.array([float(c["low"]) for c in cs])
    cl = np.array([float(c["close"]) for c in cs])
    atr_arr = _wilder_atr(h, l, cl, int(P["atr_len"]))
    tick = max(float(np.max(h)), 1.0) * 1e-9

    range_high = range_low = None
    range_high_bar = range_low_bar = -1
    # внутренние пивоты: (цена, качество, бар) — как массивы Pine, старые вытесняются
    piv: list = []

    for i in range(n):
        p_sw = i - sw
        if p_sw >= sw:
            if _is_pivot_high(h, p_sw, sw):
                range_high, range_high_bar = float(h[p_sw]), p_sw
            if _is_pivot_low(l, p_sw, sw):
                range_low, range_low_bar = float(l[p_sw]), p_sw
        p = i - ip
        if p >= ip:
            atr = float(atr_arr[i]) if not math.isnan(atr_arr[i]) else 0.0
            crange = max(float(h[p] - l[p]), tick)
            if _is_pivot_high(h, p, ip):
                wick = float(h[p] - max(o[p], cl[p]))
                w_sc = min(1.0, wick / crange * 2.0)
                rej = abs(float(h[p]) - float(cl[i])) / atr if atr > 0 else 0.0
                r_sc = min(1.0, rej / 2.0)
                piv.append((float(h[p]), min(1.0, w_sc * 0.55 + r_sc * 0.45), p))
            if _is_pivot_low(l, p, ip):
                wick = float(min(o[p], cl[p]) - l[p])
                w_sc = min(1.0, wick / crange * 2.0)
                rej = abs(float(cl[i]) - float(l[p])) / atr if atr > 0 else 0.0
                r_sc = min(1.0, rej / 2.0)
                piv.append((float(l[p]), min(1.0, w_sc * 0.55 + r_sc * 0.45), p))
            while len(piv) > int(P["pivot_memory"]):
                piv.pop(0)

    if range_high is None or range_low is None or not range_high > range_low:
        return {"zones": [], "lines": [], "broken": [], "meta": {"bars": n, "note": "диапазон не сформирован"}}

    rng = range_high - range_low
    eq = (range_high + range_low) * 0.5
    eq_half = rng * float(P["eq_width"]) * 0.5
    range_start = max(min(range_high_bar, range_low_bar), (n - 1) - int(P["max_range_bars"]))
    row_size = rng / rows

    zones = []
    for r in range(rows):
        bottom = range_low + row_size * r
        top = bottom + row_size
        mid = (top + bottom) * 0.5
        cnt = 0
        total = 0.0
        for price, qual, pb in piv:
            if pb >= range_start and bottom <= price <= top:
                cnt += 1
                total += qual
        density = min(1.0, cnt / 4.0)
        reaction = min(1.0, total / cnt) if cnt > 0 else 0.0
        score = min(1.0, density * float(P["density_w"]) + reaction * float(P["reject_w"]))
        if score <= 0.05:
            continue
        if mid > eq + eq_half:
            side = "resistance"      # premium
        elif mid < eq - eq_half:
            side = "support"         # discount
        else:
            side = "eq"
        zones.append({
            "kind": "row",
            "top": float(top), "bottom": float(bottom), "mid": float(mid),
            "side": side, "score": round(score * 100.0, 1), "pivots": cnt,
            "born_time": times[range_start], "end_time": times[-1],
        })
    zones.sort(key=lambda z: -z["score"])
    for rank, z in enumerate(zones, 1):
        z["rank"] = rank

    t0 = times[range_start]
    t1 = times[-1]
    lines = [
        {"name": "RANGE HIGH", "price": float(range_high), "side": "resistance", "start_time": t0, "end_time": t1},
        {"name": "RANGE LOW", "price": float(range_low), "side": "support", "start_time": t0, "end_time": t1},
        {"name": "EQ", "price": float(eq), "side": "eq", "start_time": t0, "end_time": t1},
    ]
    last_close = float(cl[-1])
    state = "premium" if last_close > eq + eq_half else ("discount" if last_close < eq - eq_half else "equilibrium")
    return {"zones": zones, "lines": lines, "broken": [],
            "meta": {"bars": n, "range_high": float(range_high), "range_low": float(range_low),
                     "eq": float(eq), "price_state": state, "last_close": last_close,
                     "range_start_time": t0}}


METHODS = {
    "ranked": ranked_zones,
    "profile": profile_levels,
    "matrix": matrix_levels,
}


def compute_level_method(method, candles):
    fn = METHODS.get(method)
    if fn is None:
        return {"error": "unknown method: " + str(method), "zones": [], "broken": []}
    return fn(candles)
