import json
import threading
import time
import datetime
import os
from dataclasses import dataclass
from typing import Any

# Импорт базовых инструментов
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.bybit import KNOWN_TICKER_ALIASES
from backend.modules.cryptano.utils.notifier import notify
from backend.modules.cryptano.utils.paths import (
    MACRO_LEVELS_FILE, WATCHER_HISTORY_FILE, ACTIVE_WATCHERS_FILE, CUSTOM_LEVELS_FILE,
    WATCHLIST_FILE, WATCHER_STATE_FILE, BOUNCE_STATE_FILE, BUYBACK_STATE_FILE,
)
from backend.modules.cryptano.strategy.v_manager import VManager
from backend.modules.cryptano.strategy.bounce_manager import BounceManager
from backend.modules.cryptano.strategy.bounce_manager_buyback import BounceManager as BuybackManager
from backend.modules.cryptano.strategy.bounce_watcher_buyback import BounceWatcher as BuybackWatcher
from backend.modules.cryptano.levels.swing_hunter import start_swing_hunter

# Импорт спортивных модулей
from backend.modules.footballnogoal.football import run_football_monitor
from backend.modules.playerpropsbasket.player_props import run_nba_monitor

# ================= СТРАТЕГИИ =================
# Каждая стратегия — свой менеджер со своими вотчерами и своим файлом состояния.
# Читаются с диска при импорте (старт бота), сохраняются раз в скан-цикл.
vb_mgr = VManager("VB")
vgb_mgr = VManager("VGB")
vrt_mgr = VManager("VRT")
bounce_mgr = BounceManager()
# BUYBACK — тот же bounce с другим режимом входа (id "BB_..."): свои логи, чтобы
# строки двух стратегий по одной зоне не смешивались в одном файле монеты.
BUYBACK_LOG_DIR = os.path.join(os.path.dirname(BuybackWatcher._LOG_DIR), "bounce_logs_buyback")
buyback_mgr = BuybackManager(log_dir_override=BUYBACK_LOG_DIR, entry_mode='buyback')


@dataclass(frozen=True)
class Strategy:
    """Одна строка реестра: всё, чем стратегия отличается от остальных.
    Цикл скана, очистка, архив и экспорт ходят по STRATEGIES и ничего не знают
    про конкретную стратегию."""
    key: str         # ключ в config.json crypto.strategies (вкл/выкл) и в статистике
    name: str        # имя в сигналах, архиве и логе
    mgr: Any         # менеджер стратегии (VManager или BounceManager — у каждого свои методы)
    kind: str        # "bounce" — проверка check_bounce, "v" — check_v
    state_file: str  # файл состояния менеджера


_state_dir = os.path.dirname(WATCHER_STATE_FILE)
STRATEGIES = [
    Strategy("BOUNCE", "BOUNCE", bounce_mgr, "bounce", BOUNCE_STATE_FILE),
    Strategy("BUYBACK", "BUYBACK", buyback_mgr, "bounce", BUYBACK_STATE_FILE),
    Strategy("VB", "V_BOTTOM", vb_mgr, "v", os.path.join(_state_dir, "vb_state.json")),
    Strategy("VGB", "V_GREEN_BOTTOM", vgb_mgr, "v", os.path.join(_state_dir, "vgb_state.json")),
    Strategy("VRT", "V_RED_TOP", vrt_mgr, "v", os.path.join(_state_dir, "vrt_state.json")),
]
BOUNCE_STRATEGIES = [st for st in STRATEGIES if st.kind == "bounce"]
V_STRATEGIES = [st for st in STRATEGIES if st.kind == "v"]
V_MANAGERS = {m.tag: m for m in (vb_mgr, vgb_mgr, vrt_mgr)}

for _st in STRATEGIES:
    if _st.kind == "v":
        # WATCHER_STATE_FILE — старый общий файл V-стратегий, читается один раз
        # при переходе на отдельные файлы.
        _st.mgr.load_state(_st.state_file, legacy_path=WATCHER_STATE_FILE)
    else:
        _st.mgr.load_state(_st.state_file)


def save_watcher_state():
    """Состояние всех менеджеров на диск (раз в скан-цикл и после каждой монеты)."""
    for st in STRATEGIES:
        st.mgr.save_state(st.state_file)


# _watcher_lock защищает данные менеджеров; берётся на время одной монеты в
# скан-каскаде, а не на весь проход — иначе дашборд (рескан/удаление/список
# "в работе") ждал бы окончания всего скана.
_watcher_lock = threading.Lock()
# _cascade_lock — только чтобы не запустить второй полный каскад поверх
# незавершённого первого.
_cascade_lock = threading.Lock()


def find_bounce_strategy(level_id):
    """Стратегия (BOUNCE или BUYBACK), в чьём менеджере живёт вотчер level_id, или None."""
    return next((st for st in BOUNCE_STRATEGIES if level_id in st.mgr._watchers), None)


def find_v_watcher(level_id):
    """Живой вотчер V-стратегии по level_id или None."""
    return next((m._watchers[level_id] for m in V_MANAGERS.values() if level_id in m._watchers), None)


def _load_watchlist():
    for attempt in range(2):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            if attempt == 0:
                time.sleep(0.5)  # файл мог быть в момент записи
    return {}


def _save_watchlist(data):
    save_json_atomic(WATCHLIST_FILE, data)



# level_id всегда имеет вид "{TAG}_{trade_type}_{min}_{max}" (см. VManager.level_id) — TAG однозначно говорит, какая
# это стратегия, независимо от того, есть ли метаданные в текущем

_STRATEGY_BY_TAG = {"VB": "V_BOTTOM", "VGB": "V_GREEN_BOTTOM", "VRT": "V_RED_TOP", "BC": "BOUNCE", "BB": "BUYBACK"}


def _resolve_watcher_meta(level_id, watcher, level_id_meta):
    """
    Надёжно достаёт coin/direction/strategy для вотчера — сначала из
    level_id_meta (свежий скан, самый точный источник), а если там пусто
    (уровень в этот скан не встретился в macro_levels.json — не значит,
    что вотчер "ничей"), достраивает из самого вотчера и из level_id.
    Раньше при пустой meta coin/strategy улетали в None и на дашборде
    превращались в "?", хотя вотчер прекрасно знает, кто он.
    """
    meta = level_id_meta.get(level_id, {})
    coin = meta.get("coin") or getattr(watcher, "coin", None)
    direction = meta.get("direction") or getattr(watcher, "trade_type", None)
    strategy = meta.get("strategy") or _STRATEGY_BY_TAG.get(level_id.split("_", 1)[0])
    return coin, direction, strategy


def _build_active_watchers_export(level_id_meta=None):
    """Строит export-словарь (level_id -> данные вотчера) из живых вотчеров
    обеих семей (V_BOTTOM/VGB/VRT + BOUNCE) — ЧИСТАЯ функция, БЕЗ побочных
    эффектов (ничего не пишет на диск). Вынесена отдельно от
    _write_active_watchers_snapshot (которая берёт этот же словарь и кладёт
    его на диск + сохраняет состояние вотчеров), чтобы этим же построением
    мог напрямую пользоваться и дашборд (app.py::get_active_watchers,
    GET /api/watchlist/active) — читая актуальное состояние прямо из памяти
    процесса под _watcher_lock, а не через промежуточный файл на диске,
    который иначе неизбежно немного отстаёт от реальности между
    сохранениями (раньше, во времена двух отдельных процессов, это ещё и
    был отдельный процесс с отдельной, несинхронной копией состояния —
    теперь после слияния в один процесс читать напрямую из памяти и
    корректно, и дёшево).

    level_id_meta — необязательный {level_id: {"coin":..., "direction":...}}
    из живого скана (свежее и точнее для V_BOTTOM/VGB/VRT); без него
    (например при вызове из дашборда без контекста текущего скана)
    _resolve_watcher_meta сам падает назад на атрибуты вотчера
    (watcher.coin/watcher.trade_type), которые всегда на месте и для
    V_BOTTOM, и для BOUNCE."""
    level_id_meta = level_id_meta or {}
    now_dt = datetime.datetime.now()

    export = {}
    for level_id, watcher in [kv for m in V_MANAGERS.values() for kv in m._watchers.items()]:
        coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
        export[level_id] = {
            "coin": coin,
            "direction": direction,
            "strategy": strategy,
            "state": getattr(watcher, "state", None),
            "breach_count": getattr(watcher, "breach_count", 0),
            "history_log": getattr(watcher, "history_log", ""),
            "level_min": getattr(watcher, "min", None),
            "level_max": getattr(watcher, "max", None),
            "level_date": getattr(watcher, "level_date", None),
            "level_type": getattr(watcher, "level_type", None),
            "level_score": getattr(watcher, "level_score", None),
            "events": getattr(watcher, "event_log", []),
            "updated_at": now_dt.isoformat(),
        }
    # BOUNCE — тот же самый export-словарь, тот же файл (дашборд читает один
    # active_watchers.json на все стратегии). coin/strategy тут достаются из
    # _resolve_watcher_meta через сам вотчер (watcher.coin, watcher.trade_type),
    # т.к. level_id_meta для BC обычно не заполняется — префикс "BC_" в
    # level_id и так однозначно резолвится в _STRATEGY_BY_TAG.
    # BUYBACK (buyback_mgr, id "BB_...") — те же поля, что у BOUNCE: это тот же
    # класс вотчера, только режим входа другой.
    bounce_watchers = [kv for st in BOUNCE_STRATEGIES for kv in st.mgr._watchers.items()]
    for level_id, watcher in bounce_watchers:
        coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
        export[level_id] = {
            "coin": coin,
            "direction": direction,
            "strategy": strategy,
            "mode": getattr(watcher, "mode", None),
            "state": getattr(watcher, "state", None),
            "currently_pierced": getattr(watcher, "currently_pierced", False),
            "climax_stage": getattr(watcher, "climax_stage", None),
            "breach_count": getattr(watcher, "pierce_count", 0),
            "history_log": getattr(watcher, "history_log", ""),
            "level_min": getattr(watcher, "min", None),
            "level_max": getattr(watcher, "max", None),
            "level_date": getattr(watcher, "level_date", None),
            "level_type": getattr(watcher, "level_type", None),
            "level_score": getattr(watcher, "level_score", None),
            "level_reaction_count": getattr(watcher, "level_reaction_count", None),
            "activated_at": getattr(watcher, "activated_at", None),
            "events": getattr(watcher, "event_log", []),
            "updated_at": now_dt.isoformat(),
        }
    return export


def _write_active_watchers_snapshot(level_id_meta=None):
    """Строит export-словарь через _build_active_watchers_export и кладёт
    его на диск (active_watchers.json) + сохраняет реальное состояние
    вотчеров (save_watcher_state).

    Единственное место, где эта пара (снимок для дашборда + сохранение
    состояния) вызывается вместе — раньше было два независимых куска кода
    (тут и внутри crypto_orchestrator), которые легко было поправить в
    одном и забыть про другой. ТОЛЬКО запись снимка — очистку/архивацию
    мёртвых вотчеров (она у V_BOTTOM и BOUNCE РАЗНАЯ, см.
    VManager.clear_dead_watchers vs BounceParent.clear_dead_watchers)
    этот хелпер не делает и не должен — это остаётся заботой вызывающего
    кода, до вызова этой функции."""
    export = _build_active_watchers_export(level_id_meta)
    save_json_atomic(ACTIVE_WATCHERS_FILE, export)

    # Персистентность: сохраняем реальное состояние вотчеров (не только
    # event_log для дашборда, а весь прогресс паттерна) + оба tracked-словаря
    # + BOUNCE (вотчеры/graveyard/pierced_count), чтобы рестарт бота не
    # обнулял прогресс ни у одной стратегии.
    save_watcher_state()


def export_dashboard_state():
    """
    Архивирует TRIGGERED/DEAD вотчеров BOUNCE и BUYBACK в watcher_history.json и
    перезаписывает active_watchers.json (обе стратегии — файл общий,
    поэтому пересобираем целиком, а не только BOUNCE-часть, иначе
    затёрли бы записи V_BOTTOM/VGB/VRT, которые сюда не передавали).

    Та же самая логика, что раньше жила только внутри 15-минутного цикла
    (crypto_orchestrator) — вынесена сюда отдельной функцией, чтобы её же
    мог вызвать и рескан (dashboard_actions.py::rescan_coin) сразу после
    себя. Без этого результат рескана (точки на графике, архивная запись
    сработавшей сделки, статус TRIGGERED) был бы не виден на дашборде до
    следующего планового 15-минутного цикла — реплей отрабатывал честно,
    а дашборд просто не успевал об этом узнать.
    """
    now_dt = datetime.datetime.now()

    for st in BOUNCE_STRATEGIES:
        _archive_dead_bounce(st.mgr, st.name, now_dt)

    try:
        _write_active_watchers_snapshot()
    except Exception as e:
        print(f"⚠️ [DASHBOARD EXPORT] Не удалось сохранить active_watchers.json: {e}")


def _archive_dead_bounce(mgr, strategy_name, now_dt):
    """Убирает TRIGGERED/DEAD вотчеры менеджера bounce (BOUNCE или BUYBACK) и
    кладёт их в watcher_history.json под ключом "{coin}_{strategy}_{level_id}"."""
    try:
        bc_removed = mgr.clear_dead_watchers(set())
        if bc_removed:
            history_db = load_json(WATCHER_HISTORY_FILE, default={})
            for level_id, watcher in bc_removed.items():
                w_coin = getattr(watcher, "coin", None)
                if not w_coin:
                    continue
                hist_key = f"{w_coin}_{strategy_name}_{level_id}"
                history_db[hist_key] = {
                    "coin": w_coin,
                    "direction": getattr(watcher, "trade_type", None),
                    "strategy": strategy_name,
                    "mode": getattr(watcher, "mode", None),
                    "final_state": getattr(watcher, "state", None),
                    "history_log": getattr(watcher, "history_log", ""),
                    "level_min": getattr(watcher, "min", None),
                    "level_max": getattr(watcher, "max", None),
                    "level_date": getattr(watcher, "level_date", None),
                    "level_type": getattr(watcher, "level_type", None),
                    "level_score": getattr(watcher, "level_score", None),
                    "level_reaction_count": getattr(watcher, "level_reaction_count", None),
                    "events": getattr(watcher, "event_log", []),
                    "died_at": now_dt.isoformat(),
                }
            save_json_atomic(WATCHER_HISTORY_FILE, history_db)
    except Exception as e:
        print(f"⚠️ [DASHBOARD EXPORT] Не удалось заархивировать {strategy_name} в watcher_history.json: {e}")


def _has_active(mgr, coin, side):
    """Есть ли у менеджера живой (не DEAD/TRIGGERED) вотчер этой монеты и стороны."""
    return any(
        getattr(w, "coin", None) == coin and getattr(w, "trade_type", None) == side
        and getattr(w, "state", None) not in ("DEAD", "TRIGGERED")
        for w in mgr._watchers.values()
    )


def _sync_watchlist(macro_db):
    """Приводит watchlist.json в соответствие с macro_levels.json и возвращает его.
    Любая монета из macro_levels, которой нет в watchlist, добавляется
    (source="SWING_HUNTER"); существующие записи не трогаем. В обратную сторону —
    удаляются только записи с source="SWING_HUNTER", у которых пропали уровни;
    ручные добавления не трогаем."""
    wl = _load_watchlist()
    wl_changed = False
    for coin in macro_db.keys():
        if coin == "_meta":
            continue  # служебный ключ swing_hunter, не монета
        if coin not in wl:
            wl[coin] = {"source": "SWING_HUNTER", "added_at": datetime.datetime.now().isoformat()}
            wl_changed = True
    ghosts = [c for c, meta in wl.items()
              if c != "_meta" and c not in macro_db and (meta or {}).get("source") == "SWING_HUNTER"]
    for c in ghosts:
        del wl[c]
        wl_changed = True
    if ghosts:
        print(f"🧹 [DISPATCHER] Убраны из watchlist (нет уровней в macro_levels.json): {ghosts}")
    if wl_changed:
        _save_watchlist(wl)
    return wl


def _run_strategy(st, coin, dirs, allow_long, allow_short):
    """Проверка одной стратегии по одной монете. Возвращает (отчёты, оценено уровней).
    V: работает, если у монеты есть уровни нужной стороны.
    Bounce: сторона включена переключателем LONG/SHORT и у монеты есть уровни этой
    стороны либо ещё живой вотчер (чтобы не потерять его, если уровень уже пропал)."""
    from backend.modules.cryptano.backstage.watcher_plan import check_v, check_bounce
    if st.kind == "v":
        if st.mgr.direction not in dirs:
            return [], 0
        return check_v(coin, st.mgr)
    _count, reports, levels = check_bounce(
        coin,
        allow_long and ("LONG" in dirs or _has_active(st.mgr, coin, "LONG")),
        allow_short and ("SHORT" in dirs or _has_active(st.mgr, coin, "SHORT")),
        st.mgr,
        strategy=st.name,
    )
    return reports, levels


def _scan_coin(coin, macro_db, enabled, allow_long, allow_short, stats, active_level_ids, level_id_meta):
    """Все включённые стратегии по одной монете. Вызывается под _watcher_lock.
    Возвращает число найденных сигналов, None — если монете проверять нечего."""
    coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
    dirs = []
    if coin_macro.get("supports"):
        dirs.append("LONG")
    if coin_macro.get("resistances"):
        dirs.append("SHORT")

    if not dirs and not any(_has_active(st.mgr, coin, side)
                            for st in BOUNCE_STRATEGIES for side in ("LONG", "SHORT")):
        return None

    # Актуальные level_id V-стратегий этой монеты — по ним в конце скана
    # clear_dead_watchers решает, чей вотчер ещё нужен.
    for st in V_STRATEGIES:
        for lvl in coin_macro.get("supports" if st.mgr.direction == "LONG" else "resistances", []):
            level_id = st.mgr.level_id(lvl)
            active_level_ids.add(level_id)
            level_id_meta[level_id] = {"coin": coin, "direction": st.mgr.direction, "strategy": st.name}

    found = 0
    for st in STRATEGIES:
        if not enabled[st.key]:
            continue
        reports, levels = _run_strategy(st, coin, dirs, allow_long, allow_short)
        stats[st.key]["levels"] += levels
        for report in reports:
            found += 1
            stats[st.key]["signals"] += 1
            notify(report, kind="signal", source="crypto")

    # Прогресс монеты сохраняем сразу, не дожидаясь конца скана: падение бота
    # посреди прохода не теряет остальные монеты.
    save_watcher_state()
    return found


def _scan_volume_triggers():
    """Ручные объёмные триггеры (custom_levels.json::volume_triggers) — отдельный
    проход: у монеты может быть только триггер, без уровней и вотчеров, и основной
    цикл её бы пропустил. check_volume_triggers про стратегии ничего не знает."""
    try:
        custom_db = load_json(CUSTOM_LEVELS_FILE, default={})
        coins = [c for c, data in custom_db.items()
                 if c != "_meta" and isinstance(data, dict) and data.get("volume_triggers")]
        from backend.modules.cryptano.backstage.watcher_plan import check_volume_triggers
        for coin in coins:
            with _watcher_lock:
                check_volume_triggers(coin)
            time.sleep(0.3)
    except Exception as e:
        print(f"[DISPATCHER ERROR] Ошибка проверки объёмных триггеров: {e}")


def _archive_dead_v(removed, level_id_meta, now_dt):
    """Последний слепок умершего/сработавшего V-вотчера по паре монета+стратегия
    (watcher_history.json, ключ "{coin}_{strategy}", перезаписывается при следующей смерти)."""
    if not removed:
        return
    try:
        history_db = load_json(WATCHER_HISTORY_FILE, default={})
        for level_id, watcher in removed.items():
            coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
            if not coin or not strategy:
                continue  # ни meta, ни сам вотчер не знают, кто это
            history_db[f"{coin}_{strategy}"] = {
                "coin": coin,
                "direction": direction,
                "strategy": strategy,
                "final_state": getattr(watcher, "state", None),
                "history_log": getattr(watcher, "history_log", ""),
                "level_min": getattr(watcher, "min", None),
                "level_max": getattr(watcher, "max", None),
                "level_date": getattr(watcher, "level_date", None),
                "level_type": getattr(watcher, "level_type", None),
                "level_score": getattr(watcher, "level_score", None),
                "events": getattr(watcher, "event_log", []),
                "died_at": now_dt.isoformat(),
            }
        save_json_atomic(WATCHER_HISTORY_FILE, history_db)
    except Exception as e:
        print(f"⚠️ [DASHBOARD EXPORT] Не удалось сохранить watcher_history.json: {e}")


def _cleanup_and_export(active_level_ids, level_id_meta, now_dt):
    """Конец скана, под _watcher_lock: убрать и заархивировать отработавшие вотчеры,
    выгрузить снимок для дашборда. Возвращает {что чистили: (удалено, осталось)}."""
    result = {}

    # V: вотчеры отработавших уровней, которых больше нет в macro_levels.json.
    # clear_dead_watchers возвращает удалённых — успеваем забрать их путь в архив.
    before = sum(st.mgr.watcher_count() for st in V_STRATEGIES)
    removed = {}
    for st in V_STRATEGIES:
        removed.update(st.mgr.clear_dead_watchers(active_level_ids))
    after = sum(st.mgr.watcher_count() for st in V_STRATEGIES)
    _archive_dead_v(removed, level_id_meta, now_dt)
    result["VB/VGB/VRT"] = (before - after, after)

    # Bounce и BUYBACK: убрать DEAD/TRIGGERED, каждого — в архив под своим ключом.
    for st in BOUNCE_STRATEGIES:
        before = st.mgr.watcher_count()
        _archive_dead_bounce(st.mgr, st.name, now_dt)
        after = st.mgr.watcher_count()
        result[st.name] = (before - after, after)

    try:
        _write_active_watchers_snapshot(level_id_meta)
    except Exception as e:
        print(f"⚠️ [DASHBOARD EXPORT] Не удалось сохранить active_watchers.json: {e}")
    return result


def _scan_pass(strategies_cfg, allow_long, allow_short):
    """Один полный проход по монетам: все включённые стратегии, объёмные триггеры,
    очистка, экспорт, итог в лог."""
    macro_db = load_json(MACRO_LEVELS_FILE, default={})
    wl = _sync_watchlist(macro_db)
    if not wl:
        print("✅ [DISPATCHER] macro_levels.json пуст. Скан отменен.")
        return

    # _cascade_lock — только чтобы не запустить второй проход поверх незавершённого.
    # Сами менеджеры защищает _watcher_lock, отдельно на каждую монету: дашборд
    # (рескан/удаление) не ждёт окончания всего скана.
    if not _cascade_lock.acquire(blocking=False):
        return
    try:
        now_dt = datetime.datetime.now()
        enabled = {st.key: strategies_cfg.get(st.key, True) for st in STRATEGIES}
        stats = {st.key: {"signals": 0, "levels": 0} for st in STRATEGIES}
        active_level_ids = set()  # для clear_dead_watchers в конце скана
        level_id_meta = {}        # level_id -> {coin, direction, strategy} для дашборда
        total_scanned = 0
        signals_found = 0

        # Скан = watchlist + монеты с живым bounce/buyback-вотчером: монета могла
        # выпасть из watchlist (упал объём), но её вотчер "в работе", пока не
        # случится сделка или цена не уйдёт от зоны.
        with _watcher_lock:
            orphan_coins = {
                getattr(w, "coin", None)
                for st in BOUNCE_STRATEGIES for w in list(st.mgr._watchers.values())
                if getattr(w, "state", None) not in ("DEAD", "TRIGGERED") and getattr(w, "coin", None)
            }
        scan_coins = list(wl.keys()) + [c for c in orphan_coins if c not in wl]

        for coin in scan_coins:
            if coin == "_meta":
                continue
            total_scanned += 1
            with _watcher_lock:
                found = _scan_coin(coin, macro_db, enabled, allow_long, allow_short,
                                   stats, active_level_ids, level_id_meta)
            if found is not None:
                signals_found += found
                time.sleep(0.5)  # пауза между монетами

        _scan_volume_triggers()

        with _watcher_lock:
            cleanup = _cleanup_and_export(active_level_ids, level_id_meta, now_dt)

            print(f"✅ [DISPATCHER] Анализ прошел. Монет просканировано: {total_scanned} | Сигналов найдено: {signals_found}")
            for st in STRATEGIES:
                print(f"   -> {st.name}: Уровней оценено: {stats[st.key]['levels']} | Сделок найдено: {stats[st.key]['signals']}")
            for label, (removed, left) in cleanup.items():
                print(f"   -> Очистка {label}: удалено {removed}, осталось в памяти {left}")
    finally:
        _cascade_lock.release()


def crypto_orchestrator():
    """
    Единый каскадный Диспетчер автоматики Крипты.
    Раз в 15 минут (граница 15м свечи по UTC + 60 сек на закрытие свечи биржей)
    запускает _scan_pass и строго следит за кнопками ON/OFF.
    """
    print("🪙  Watcher Crypto инициализирован!")

    last_watcher = 0
    cascade_initialized = False
    session_start_time = 0
    config_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))

    while True:
        time.sleep(1)  # пульс раз в секунду

        try:
            # Конфиг перечитывается каждую секунду: переключения в меню применяются
            # на ближайшем же скане, без рестарта. Нет ключа = включено.
            crypto_cfg = load_json(config_path, default={}).get("crypto", {})
            status = crypto_cfg.get("status", "STOPPED")
            strategies_cfg = crypto_cfg.get("strategies", {})
            # LONG/SHORT переключатель bounce-стратегий — единая точка правды с
            # дашбордом (app.py::get_bounce_direction) и симулятором.
            allow_long = crypto_cfg.get("allow_long", True)
            allow_short = crypto_cfg.get("allow_short", True)

            # 📴 STOPPED: автобот выключен — вся автоматика молчит
            if status != "RUNNING":
                if cascade_initialized:
                    print("[DISPATCHER] 💤 Автобот переведен в STOPPED. Диспетчер уходит в режим ожидания.")
                    cascade_initialized = False
                continue

            # 🟢 Старт: статус RUNNING, а сессия ещё не зафиксирована
            if not cascade_initialized:
                print("[DISPATCHER] 🚀 Обнаружен запуск Автобота! Включаю каскадный отсчет времени...")
                session_start_time = time.time()
                cascade_initialized = True
                last_watcher = 0  # принудительно прогнать стартовый скан

            elapsed = time.time() - session_start_time

            # Скан: через 5 минут после старта, дальше — на каждой границе 15м
            # свечи + 60 сек. Раньше скан мог попасть внутрь ещё не закрытой свечи,
            # и вотчер видел один бар по нескольку раз с разными high/close.
            now_ts = time.time()
            next_boundary = (now_ts // 900) * 900 + 60
            if elapsed >= 300 and now_ts >= next_boundary and (last_watcher < next_boundary or last_watcher == 0):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Начало анализа Watcher списка...")
                last_watcher = time.time()

                # Живой результат открытых сигналов — на той же границе; сбой тут
                # не должен ронять остальной скан.
                try:
                    from backend.modules.cryptano.levels.history import update_open_signals
                    update_open_signals()
                except Exception as e:
                    print(f"[DISPATCHER ERROR] Не удалось обновить открытые сигналы: {e}")

                try:
                    _scan_pass(strategies_cfg, allow_long, allow_short)
                except Exception as e:
                    print(f"[DISPATCHER ERROR] Ошибка внутри Watcher цикла: {e}")

        except Exception as e:
            print(f"[CRITICAL DISPATCHER ERROR] Сбой главного диспетчера задач: {e}")


def start_all_background_tasks():
    """
    Запускает спортивные мониторы и наш единый Каскадный Диспетчер крипты.
    """
    # 🪙 Запуск Единого Диспетчера Крипты в один поток
    threading.Thread(target=crypto_orchestrator, daemon=True).start()
    
    # 🌪 Запуск Свинг Хантера (Генератор уровней + Минутный дозор)
    start_swing_hunter()
    
    # ⚽️🏀 Спортивные мониторы (остались без изменений)
    threading.Thread(target=run_football_monitor, daemon=True).start()
    threading.Thread(target=run_nba_monitor, daemon=True).start()