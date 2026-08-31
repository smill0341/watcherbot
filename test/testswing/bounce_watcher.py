# -*- coding: utf-8 -*-

class BounceWatcher:
    """
    v1 — ПРИМИТИВ. Задача не "точная стратегия", а быстро увидеть на графике,
    где вообще срабатывает идея "объёмный вход у уровня", прежде чем
    ужесточать условия (тень/тело, RR-фильтр, ATR-стоп и т.д. — см. TODO ниже).

    Условие входа:
        - цена коснулась зоны уровня (не обязательно глубоко)
        - свеча правильного цвета (зелёная для LONG, красная для SHORT)
        - объём >= VOL_SPIKE_MULT (по умолчанию x3) от фонового (baseline_vol)

    Выход: фиксированный % (FIXED_TP_PCT) — работает по-настоящему в этой
    версии, в отличие от старой (там 'fixed_pct' был мёртвой веткой и TP
    реально брался от противоположных уровней через _calc_tp_and_rr).
    """

    _log_cleared = False

    CONFIG = {
        'MIN_SCORE': 1.0,          
        'VOL_SPIKE_MULT': 3.0,     
        'MIN_VOL_MULT_TO_LOG': 1.5,   # Фильтр мусора: не рисовать SCAN и не писать лог, если объем ниже х1.5
        'MAX_RUNAWAY_PCT': 5.0,       # После пробоя: если цена ушла дальше этого % от уровня — отбой, вотчер умирает сам
        'MIN_BODY_PCT': 20.0,         # Плотность свечи: тело должно занимать минимум 40% от всего размаха
        'MAX_WICKS_PCT': 60.0,        # Защита от отвержения: верхняя тень (для лонга) не больше 30%
        'FIXED_TP_PCT': 7.0,       
        'SL_PCT': 50.0,            
        'MAX_TRADES_PER_PIERCE': 1,   # сколько сделок разрешено на ОДИН пробой, пока не случится новый
        'MAX_TRADES_PER_LEVEL': 1,    # сколько сделок разрешено за всю жизнь уровня (0 = без лимита). Пробоев может
                                        # быть сколько угодно — сами по себе они бесплатны, считаются только сделки.
        'KILL_ON_TELEPORT_OPEN': False,  # хоронить вотчер, если он появился УЖЕ пробитым (цена открытия по ту
                                          # сторону уровня на первой же свече жизни вотчера). False — не хоронить
                                          # и не считать эту свечу пробоем вообще: просто пропустить её и следить
                                          # дальше как обычно, честный пробой засчитается позже своим чередом.
        'DEBUG': True,

        # --- TODO для следующих итераций (сейчас не используется) ---
        # 'PINBAR_SHADOW_RATIO': 1.5,   # требовать тень/тело — вернуть, когда примитив обкатан
        # 'MAX_BODY_PCT': 40.0,         # ограничить жирность тела свечи входа
        # 'USE_RR_FILTER': True,        # включить проверку риск/прибыль перед входом
        # 'MIN_RR': 1.0,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SCANNING"
        self._last_time = None
        self.last_event_time = None
        self.last_event_type = None
        self.trades_count = 0          # всего сделок за всю жизнь уровня (для лога/статистики)
        self.trades_since_pierce = 0   # сделок с момента ПОСЛЕДНЕГО пробоя — сбрасывается на новом пробое
        self.pierce_count = 0          # сколько раз этот уровень вообще был пробит
        self.last_pierce_time = None   # когда был последний пробой — для выбора "кто сейчас в фокусе"
        self._awaiting_recovery = False  # True = уровень появился уже пробитым (телепорт), ждём честного
                                          # реклейма (закрытие обратно за уровень), прежде чем считать пробои
    

        if self.CONFIG.get('DEBUG') and not BounceWatcher._log_cleared:
            with open("bounce_debug.log", "w", encoding="utf-8") as f:
                f.write("=== НОВЫЙ ТЕСТ BOUNCE (v1, примитив) ЗАПУЩЕН ===\n")
            BounceWatcher._log_cleared = True

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "SCANNING"

    def _dbg(self, msg):
        if self.CONFIG.get('DEBUG'):
            time_str = f"{self._last_time} " if self._last_time else ""
            tag = " OD" if getattr(self, 'reborn', False) else ""
            with open("bounce_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{time_str}[{self.trade_type} {self.min:.4f}-{self.max:.4f}{tag}] {msg}\n")

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, level_score=0, candle_time=None, vol_90=0.0, is_focus=True):
        is_first_candle = (self._last_time is None)
        
        self._last_time = candle_time
        self.last_event_time = candle_time
        self.last_event_type = None

        if self.state in ("TRIGGERED", "DEAD"):
            return None

        if level_score < self.CONFIG['MIN_SCORE']:
            return None

        c_open, c_high, c_low, c_close, c_vol = map(float, (c_open, c_high, c_low, c_close, c_vol))

        # --- ЗАЩИТА ОТ ФАЛЬШИВОГО ПРОБОЯ (ТЕЛЕПОРТАЦИИ БАЗЫ) ---
        # Если уровень только появился в памяти бота, но свеча УЖЕ открылась
        # с пробитой стороны — по умолчанию (KILL_ON_TELEPORT_OPEN=True) хороним
        # его сразу, чтобы не словить сделку-мираж. Если выключено — НЕ хороним,
        # а ставим флаг ожидания честного реклейма (см. ниже): пока цена не
        # закроется обратно ЗА уровень хотя бы раз, вообще ничего не считаем
        # пробоем — просто наблюдаем молча.
        if is_first_candle:
            teleported = (self.trade_type == 'LONG' and c_open < self.min) or \
                         (self.trade_type == 'SHORT' and c_open > self.max)
            if teleported:
                if self.CONFIG.get('KILL_ON_TELEPORT_OPEN', True):
                    self.state = "DEAD"
                    self.last_event_type = "RUNAWAY"
                    if self.trade_type == 'LONG':
                        self._dbg(f"🔴 ОТМЕНА | Уровень появился УЖЕ пробитым (Open {c_open:.4f} < Min {self.min:.4f}).")
                    else:
                        self._dbg(f"🔴 ОТМЕНА | Уровень появился УЖЕ пробитым (Open {c_open:.4f} > Max {self.max:.4f}).")
                    return None
                else:
                    self._awaiting_recovery = True
                    self._dbg(f"⚪ ТЕЛЕПОРТ | Уровень появился уже пробитым (Open {c_open:.4f}) — ждём честного реклейма, пока не считаем пробоем")
                    return None

        # --- ОЖИДАНИЕ РЕКЛЕЙМА ПОСЛЕ ТЕЛЕПОРТА ---
        # Пока цена ни разу не закрылась обратно ЗА уровень — вообще ничего не
        # считаем (ни пробой, ни отбой), просто ждём. Как только реклейм
        # случился — снимаем флаг, и со СЛЕДУЮЩЕЙ свечи всё работает как обычно
        # (сам реклейм-close ещё не считается пробоем).
        if self._awaiting_recovery:
            if self.trade_type == 'LONG':
                if c_close > self.min:
                    self._awaiting_recovery = False
                    self._dbg(f"🟢 РЕКЛЕЙМ | Закрытие вернулось выше уровня ({c_close:.4f} > {self.min:.4f}) — с этого момента честные пробои считаются")
            else:
                if c_close < self.max:
                    self._awaiting_recovery = False
                    self._dbg(f"🔴 РЕКЛЕЙМ | Закрытие вернулось ниже уровня ({c_close:.4f} < {self.max:.4f}) — с этого момента честные пробои считаются")
            return None

        # --- СБОР СТАТИСТИКИ: Прокол дна (Sweep) ---
        if not hasattr(self, 'pierced_bottom'):
            self.pierced_bottom = False
            self.currently_pierced = False
            self.currently_runaway = False

        new_pierce_this_candle = False  # только что случился НОВЫЙ пробой именно на этой свече

        if self.trade_type == 'LONG':
            if c_low <= self.min:
                self.pierced_bottom = True  # Глобальный флаг для отчета (навсегда)
                if not self.currently_pierced:
                    self.currently_pierced = True
                    new_pierce_this_candle = True
            else:
                self.currently_pierced = False  # Цена полностью поднялась над линией (c_low > min)

        elif self.trade_type == 'SHORT':
            if c_high >= self.max:
                self.pierced_bottom = True
                if not self.currently_pierced:
                    self.currently_pierced = True
                    new_pierce_this_candle = True
            else:
                self.currently_pierced = False
        # -------------------------------------------

        # --- ОТБОЙ: цена ушла слишком далеко в другую сторону — эту конкретную
        # попытку не считаем сделкой, но и не хороним уровень навсегда: пробой,
        # не давший сделки, бесплатен, просто ждём следующий. Событие/лог — один
        # раз на начало ухода, не на каждой свече, пока он длится. ---
        runaway_this_candle = False
        if self.pierced_bottom:
            if self.trade_type == 'LONG':
                runaway_limit = self.max * (1 + self.CONFIG['MAX_RUNAWAY_PCT'] / 100.0)
                runaway_this_candle = c_close > runaway_limit
            elif self.trade_type == 'SHORT':
                runaway_limit = self.min * (1 - self.CONFIG['MAX_RUNAWAY_PCT'] / 100.0)
                runaway_this_candle = c_close < runaway_limit

        if new_pierce_this_candle and runaway_this_candle:
            # Пробой и отбой случились на ОДНОЙ и той же свече (глубокий фитиль,
            # моментальный отскок за пределы лимита) — реального окна для входа
            # никогда не существовало. Не считаем это пробоем для статистики/
            # фокуса (pierce_count, last_pierce_time, событие SWEEP_BOTTOM), но
            # pierced_bottom остаётся True — честный будущий реклейм всё ещё разрешён.
            self.currently_pierced = False  # эта "попытка" пробоя отменена, ждём следующую как новую
            self._dbg(f"⚪ ШУМ | Пробой и отбой в одной свече (Лой:{c_low:.4f} / Хай:{c_high:.4f} vs Закрытие:{c_close:.4f}) — не считаем")
        elif new_pierce_this_candle:
            self.pierce_count += 1  # только для лога/нумерации, ничего не ограничивает
            self.trades_since_pierce = 0  # новый пробой — бюджет сделок на него свежий
            self.last_pierce_time = candle_time
            self.last_event_type = "SWEEP_BOTTOM"  # Рисуем точку только 1 раз на прокол
            if self.trade_type == 'LONG':
                self._dbg(f"🔵 ПРОКОЛ ДНА (#{self.pierce_count}) | Лой свечи: {c_low:.4f} <= Уровень: {self.min:.4f}")
            else:
                self._dbg(f"🔵 ПРОКОЛ ХАЯ (#{self.pierce_count}) | Хай свечи: {c_high:.4f} >= Уровень: {self.max:.4f}")

        if runaway_this_candle:
            if not self.currently_runaway:
                self.currently_runaway = True
                self.last_event_type = "RUNAWAY"
                # Этот пробой отменён — цена улетела слишком далеко. Раньше
                # pierced_bottom оставался True навсегда, и любая ПОЗЖЕ зелёная
                # свеча (хоть через несколько дней, хоть на совсем другом
                # падении) засчитывалась как вход по этому старому пробою.
                # Теперь честно требуем НОВЫЙ пробой (c_low <= self.min) прежде
                # чем снова разрешать вход.
                self.pierced_bottom = False
                self.currently_pierced = False
                if self.trade_type == 'LONG':
                    self._dbg(f"🟠 ОТБОЙ (пробой #{self.pierce_count} не реализован) | Цена ушла слишком высоко: {c_close:.4f} > лимит {runaway_limit:.4f} | Пробой аннулирован, жду новый")
                else:
                    self._dbg(f"🟠 ОТБОЙ (пробой #{self.pierce_count} не реализован) | Цена ушла слишком низко: {c_close:.4f} < лимит {runaway_limit:.4f} | Пробой аннулирован, жду новый")
            return None
        else:
            self.currently_runaway = False
        # -------------------------------------------

        
        # Для математики входа используем 90-й перцентиль (vol_90)
        logic_vol = vol_90 if vol_90 > 0 else baseline_vol
        vol_mult = (c_vol / logic_vol) if logic_vol > 0 else 0.0
        
        is_green = c_close > c_open
        is_red = c_close < c_open

        v_str = self._fmt(c_vol)

        # Высчитываем анатомию свечи
        hl = float(c_high - c_low)
        body = abs(float(c_close - c_open))
        top_shadow = float(c_high - max(c_open, c_close))
        bottom_shadow = min(c_open, c_close) - float(c_low)
        
        body_pct = (body / hl * 100.0) if hl > 0 else 0.0
        top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
        bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0

        if self.trade_type == 'LONG':
            max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
            is_pierce_budget_ok = (max_per_pierce <= 0) or (self.trades_since_pierce < max_per_pierce)

            if self.pierced_bottom and is_green:
                # Фильтр мусора: игнорим всё, что ниже MIN_VOL_MULT_TO_LOG
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = top_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok and is_pierce_budget_ok and is_focus:
                        self.last_event_type = "GOOD_GREEN"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        # Собираем причины отказа в строку
                        fail_reasons = []
                        if not is_focus: fail_reasons.append("не в фокусе (пробит более свежий уровень)")
                        if not is_pierce_budget_ok: fail_reasons.append(f"Бюджет пробоя #{self.pierce_count} исчерпан, жду новый")
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"В.Тень:{top_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | ЗЕЛЕНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        elif self.trade_type == 'SHORT':
            max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
            is_pierce_budget_ok = (max_per_pierce <= 0) or (self.trades_since_pierce < max_per_pierce)

            if self.pierced_bottom and is_red:
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = bottom_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok and is_pierce_budget_ok and is_focus:
                        self.last_event_type = "GOOD_RED"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        fail_reasons = []
                        if not is_focus: fail_reasons.append("не в фокусе (пробит более свежий уровень)")
                        if not is_pierce_budget_ok: fail_reasons.append(f"Бюджет пробоя #{self.pierce_count} исчерпан, жду новый")
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"Н.Тень:{bottom_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | КРАСНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        return None

    def _enter(self, actual_entry, vol_mult, c_vol, logic_vol, baseline_vol):
        tp_pct = self.CONFIG['FIXED_TP_PCT'] / 100.0
        sl_pct = self.CONFIG['SL_PCT'] / 100.0

        if self.trade_type == 'LONG':
            tp = actual_entry * (1 + tp_pct)
            sl = actual_entry * (1 - sl_pct)
        else:
            tp = actual_entry * (1 - tp_pct)
            sl = actual_entry * (1 + sl_pct)

        self.trades_count += 1
        self.trades_since_pierce += 1

        max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
        max_trades_total = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 1)

        # Насовсем умираем только когда исчерпан общий лимит сделок за всю жизнь
        # уровня. Пробоев может быть сколько угодно — они сами по себе бесплатны.
        if max_trades_total > 0 and self.trades_count >= max_trades_total:
            self.state = "TRIGGERED"
        else:
            self.state = "SCANNING"

        v_str = self._fmt(c_vol)
        
        # Короткий и чёткий лог: сделка X из общего лимита, в рамках пробоя #N
        reason_str = f"BOUNCE (пробой #{self.pierce_count}, сделка {self.trades_count}/{max_trades_total if max_trades_total > 0 else '∞'}) | V:{v_str} (x{vol_mult:.1f})"
        self._dbg(f"✅ ВХОД: {actual_entry:.4f} | {reason_str}")

        if self.state == "TRIGGERED":
            self._dbg(f"🛑 СТОП-СКАН | Общий лимит сделок исчерпан ({self.trades_count}/{max_trades_total}), уровень закрыт насовсем")

        return {
            "allow": True,
            "level_id": f"{self.min}_{self.max}",
            "action": "BUY" if self.trade_type == 'LONG' else "SELL",
            "entry_price": actual_entry,
            "sl": sl,
            "tp": tp,
            "reason": reason_str,
            "is_real_sweep": False,
            "candles_in_sweep": 0,
            "pierced_bottom": getattr(self, 'pierced_bottom', False),
            "reborn": getattr(self, 'reborn', False),
        }