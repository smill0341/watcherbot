# -*- coding: utf-8 -*-
"""Сравнение двух прогонов симулятора + двух файлов уровней. Только читает файлы, ничего не меняет.
Запуск (из D:\\bot\\master_bot):
  python -X utf8 backend\\compare_runs.py СТАРЫЙ_run.json НОВЫЙ_run.json СТАРЫЙ_levels.json НОВЫЙ_levels.json
"""
import json, sys, bisect, collections, datetime

def load(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)

def st(g):
    c = [x for x in g if x['status'] in ('✅', '🕐')]
    w = [x for x in c if x['result_percent'] > 0]
    return f"n={len(g)} WR={round(len(w)/len(c)*100,1) if c else '-'}% сумма={round(sum(x['result_percent'] for x in c),1)}"

def ov(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0) > 0

class TL:
    def __init__(self, d):
        self.keys = sorted(d)
        self.ms = [int(datetime.datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()) for k in self.keys]
        self.d = d
    def snap_idx(self, t):
        i = bisect.bisect_right(self.ms, t) - 1
        return i if i >= 0 else None
    def zones(self, i, coin, side):
        s = self.d[self.keys[i]].get(coin)
        if not s:
            return None
        return s['supports' if side == 'LONG' else 'resistances']

def classify(tr, tl):
    """Что было в уровнях tl на месте сделки tr."""
    coin, side = tr['coin'], tr['type']
    i = tl.snap_idx(tr['time'])
    if i is None:
        return 'нет снимка'
    z = tl.zones(i, coin, side)
    if z is None:
        return 'монеты нет в снимке'
    hit = [q for q in z if ov(q['min'], q['max'], tr['level_min'], tr['level_max'])]
    if hit:
        same = any(abs(q['min'] - tr['level_min']) < 1e-12 * max(1, abs(q['min'])) and abs(q['max'] - tr['level_max']) < 1e-12 * max(1, abs(q['max'])) for q in hit)
        return 'зона та же (границы равны)' if same else 'зона есть, границы другие'
    # зоны нет — была ли она в предыдущих снимках (исчезла) или нет совсем
    for j in range(i - 1, max(i - 8, -1), -1):
        zz = tl.zones(j, coin, side)
        if zz and any(ov(q['min'], q['max'], tr['level_min'], tr['level_max']) for q in zz):
            return 'зона ИСЧЕЗЛА (была в одном из прошлых 7 снимков)'
    return 'зоны нет (и раньше не было)'

def main():
    if len(sys.argv) < 5:
        print(__doc__); return
    ro, rn = load(sys.argv[1]), load(sys.argv[2])
    to, tn = TL(load(sys.argv[3])), TL(load(sys.argv[4]))

    def key(a, b):
        return a['coin'] == b['coin'] and a['type'] == b['type'] and a['source'] == b['source'] \
            and ov(a['level_min'], a['level_max'], b['level_min'], b['level_max']) and abs(a['time'] - b['time']) < 86400 * 3
    only_old = [a for a in ro['trades'] if not any(key(a, b) for b in rn['trades'])]
    only_new = [b for b in rn['trades'] if not any(key(a, b) for a in ro['trades'])]
    print(f"Только в СТАРОМ: {st(only_old)}\nТолько в НОВОМ:  {st(only_new)}\n")

    for title, trs, tl_other in (("ТОЛЬКО В СТАРОМ -> что было в НОВЫХ уровнях", only_old, tn),
                                 ("ТОЛЬКО В НОВОМ -> что было в СТАРЫХ уровнях", only_new, to)):
        print("=" * 70); print(title)
        for side in ('LONG', 'SHORT'):
            g = [t for t in trs if t['type'] == side]
            by = collections.defaultdict(list)
            for t in g:
                by[classify(t, tl_other)].append(t)
            print(f"\n{side}: {len(g)}")
            for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
                print(f"  {k:48} {st(v)}")

    print("\n" + "=" * 70)
    co = {t['coin'] for t in ro['trades']}; cn = {t['coin'] for t in rn['trades']}
    print("Монеты со сделками только в СТАРОМ:", sorted(co - cn))
    print("Монеты со сделками только в НОВОМ: ", sorted(cn - co))
    for c in sorted(co - cn):
        i = tn.snap_idx(int(datetime.datetime(2026, 9, 15).timestamp()))
        n_old = [t for t in ro['trades'] if t['coin'] == c]
        sn = [len(tn.d[k].get(c, {}).get('supports', [])) + len(tn.d[k].get(c, {}).get('resistances', [])) for k in tn.keys]
        so = [len(to.d[k].get(c, {}).get('supports', [])) + len(to.d[k].get(c, {}).get('resistances', [])) for k in to.keys]
        print(f"  {c}: сделок в старом {len(n_old)}; зон в снимках новый min/max {min(sn)}/{max(sn)}, старый {min(so)}/{max(so)}")

    print("\nПримеры 'зона ИСЧЕЗЛА'/'зоны нет' (до 15) — монета, сторона, дата, зона старого, результат:")
    shown = 0
    for t in only_old:
        c = classify(t, tn)
        if c.startswith('зон') and 'есть' not in c and 'та же' not in c and shown < 15:
            shown += 1
            print(f"  {t['coin']:10} {t['type']:5} {t['date']} [{t['level_min']:.6g}..{t['level_max']:.6g}] {t['level_type']} -> {t['result_percent']}%   ({c})")

if __name__ == '__main__':
    main()