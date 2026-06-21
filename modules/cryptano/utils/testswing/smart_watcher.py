class LevelWatcher:
    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "FRESH"
        self.extreme_price = None

    def update(self, c_open, c_high, c_low, c_close):
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        # ==========================================
        # ЛОГИКА LONG
        # ==========================================
        if self.trade_type == 'LONG':
            if self.state == "FRESH":
                if c_low <= self.max:  # Касание зоны поддержки
                    if c_close < self.min:
                        # Ушли под воду (Вынос). Начинаем искать абсолютное дно
                        self.state = "BELOW"
                        self.extreme_price = c_low
                    else:
                        # Мгновенный отскок (Закрылись в зоне или выше)
                        self.state = "TRIGGERED"
                        return {"action": "BUY", "sl": None, "reason": "Первое касание (Отскок)"}

            elif self.state == "BELOW":
                # Мы под водой. Обновляем абсолютное дно с каждой свечой
                self.extreme_price = min(self.extreme_price, c_low) if self.extreme_price is not None else c_low
                
                # Жесткий триггер: пересечение линии обратно в зону
                if c_close > self.min:
                    self.state = "TRIGGERED"
                    return {"action": "BUY", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)"}

        # ==========================================
        # ЛОГИКА SHORT
        # ==========================================
        elif self.trade_type == 'SHORT':
            if self.state == "FRESH":
                if c_high >= self.min: # Касание зоны сопротивления
                    if c_close > self.max:
                        # Ушли выше сопротивления (Вынос). Начинаем искать абсолютный хай
                        self.state = "ABOVE"
                        self.extreme_price = c_high
                    else:
                        # Мгновенный отскок
                        self.state = "TRIGGERED"
                        return {"action": "SELL", "sl": None, "reason": "Первое касание (Отскок)"}

            elif self.state == "ABOVE":
                # Обновляем абсолютный хай
                self.extreme_price = max(self.extreme_price, c_high) if self.extreme_price is not None else c_high
                
                # Жесткий триггер: пересечение линии обратно вниз
                if c_close < self.max:
                    self.state = "TRIGGERED"
                    return {"action": "SELL", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)"}

        return None