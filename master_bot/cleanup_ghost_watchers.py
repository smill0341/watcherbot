#!/usr/bin/env python3
"""
cleanup_ghost_watchers.py
==========================
Разовая чистка "вотчеров-призраков" — тех, что застряли в watcher_state.json
(и tracked_origin_levels*.json) с уровнем, которого больше нет в текущем
macro_levels.json. На дашборде такие показываются как "?" вместо монеты и
стратегии, график по ним не открывается.

Причина: build_macro_levels() раньше полностью перезаписывал macro_levels.json
каждые ~12ч. Если монета/уровень не переоткрылись — вотчер остаётся жить в
памяти (специально, чтобы не терять сделку в процессе), но метаданных для
дашборда для него больше нет. См. фикс в swing_hunter.py (мердж вместо
перезаписи) — после него новые призраки появляться не должны, но старые,
уже застрявшие, сами не пропадут (см. clear_dead_watchers в vbottom_manager.py:
вотчер не в финальном состоянии — не трогаем).

ВАЖНО: запускать ТОЛЬКО пока бот остановлен. Если бот работает, он держит
это состояние в памяти и на следующем сохранении перезапишет файлы обратно
как было — правка мимо живого процесса не пройдёт.

Восстановить призраков нельзя — уровень, на котором они висели, уже не
в macro_levels.json, продолжать его отслеживать не от чего.

Использование:
    cd master_bot
    python3 cleanup_ghost_watchers.py            # только показать, что будет удалено
    python3 cleanup_ghost_watchers.py --apply     # реально удалить
"""
import os
import json
import argparse

CRYPTANO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules", "cryptano")
JSONBANK_DIR = os.path.join(CRYPTANO_DIR, "jsonbank")

WATCHER_STATE_FILE = os.path.join(JSONBANK_DIR, "watcher_state.json")
TRACKED_LONG_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels.json")
TRACKED_VRT_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels_vrt.json")
MACRO_LEVELS_FILE = os.path.join(JSONBANK_DIR, "macro_levels.json")


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_valid_level_ids(macro_db):
    """Те же ключи, что background_tasks.py считает "живыми" на скане —
    зеркалит level_id_meta/active_level_ids оттуда."""
    valid = set()
    for coin, coin_macro in macro_db.items():
        if coin == "_meta":
            continue
        for lvl in coin_macro.get("supports", []):
            valid.add(f"VB_LONG_{lvl['min']}_{lvl['max']}")
            valid.add(f"VGB_LONG_{lvl['min']}_{lvl['max']}")
        for lvl in coin_macro.get("resistances", []):
            valid.add(f"VRT_SHORT_{lvl['min']}_{lvl['max']}")
    return valid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="реально удалить (без флага — только показать список)")
    args = parser.parse_args()

    macro_db = _load(MACRO_LEVELS_FILE)
    if not macro_db:
        print(f"⚠️  {MACRO_LEVELS_FILE} пуст или не найден — прерываю, слишком опасно чистить вслепую.")
        return

    valid_ids = build_valid_level_ids(macro_db)

    watcher_state = _load(WATCHER_STATE_FILE)
    if not watcher_state:
        print("watcher_state.json пуст или не найден — чистить нечего.")
        return

    tracked_long = _load(TRACKED_LONG_FILE)
    tracked_vrt = _load(TRACKED_VRT_FILE)

    ghosts = {lid: w for lid, w in watcher_state.items() if lid not in valid_ids}

    if not ghosts:
        print("✅ Призраков не найдено — watcher_state.json чист.")
        return

    print(f"Найдено призраков: {len(ghosts)}\n")
    for lid, w in ghosts.items():
        inner_state = (w.get("state") or {}).get("state")
        print(f"  {lid}")
        print(f"    coin={w.get('coin')}  trade_type={w.get('trade_type')}  "
              f"min={w.get('min')}  max={w.get('max')}  state={inner_state}")

    if not args.apply:
        print("\n(это предпросмотр — ничего не изменено. Запусти с --apply, чтобы реально удалить)")
        return

    # 1. Убираем из watcher_state.json
    for lid in ghosts:
        del watcher_state[lid]
    _save_atomic(WATCHER_STATE_FILE, watcher_state)

    # 2. Убираем совпадающие записи из tracked_origin_levels*.json — там
    #    другие ключи (coin_LONG / coin_VGB_LONG / coin_VRT_SHORT),
    #    сопоставляем по coin+min+max каждого призрака.
    removed_tracked = 0
    for lid, w in ghosts.items():
        coin = w.get("coin")
        min_v, max_v = w.get("min"), w.get("max")
        if not coin:
            continue
        for key in (f"{coin}_LONG", f"{coin}_VGB_LONG"):
            t = tracked_long.get(key)
            if t and t.get("min") == min_v and t.get("max") == max_v:
                del tracked_long[key]
                removed_tracked += 1
        vrt_key = f"{coin}_VRT_SHORT"
        t = tracked_vrt.get(vrt_key)
        if t and t.get("min") == min_v and t.get("max") == max_v:
            del tracked_vrt[vrt_key]
            removed_tracked += 1

    _save_atomic(TRACKED_LONG_FILE, tracked_long)
    _save_atomic(TRACKED_VRT_FILE, tracked_vrt)

    print(f"\n✅ Удалено из watcher_state.json: {len(ghosts)}")
    print(f"✅ Удалено сопутствующих записей из tracked_origin_levels*.json: {removed_tracked}")
    print("Перезапусти бота, чтобы он загрузил чистое состояние.")


if __name__ == "__main__":
    main()