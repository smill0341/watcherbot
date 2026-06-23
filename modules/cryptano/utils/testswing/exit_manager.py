"""
exit_manager.py
===============
Логика закрытия позиции по TP/SL.
Вызывается на каждой свече для проверки условий выхода.
"""


class ExitManager:
    """Управляет выходом из позиции по TP и SL."""

    def __init__(self, disable_sl=False):
        self.position = None  # {'type', 'entry', 'tp', 'sl', 'deadline', 'opened_at', 'mae_pct'}
        self.disable_sl = disable_sl  # режим диагностики: SL игнорируется, выход по TP или по deadline
        self.last_closed_mae = 0.0  # MAE последней закрытой позиции (читается ПОСЛЕ check_exit)

    def open_position(self, pos_type, entry_price, tp, sl, opened_at=None, deadline=None):
        """
        Открывает позицию для отслеживания.
        opened_at - timestamp открытия (для расчёта времени удержания)
        deadline  - timestamp принудительного закрытия (используется в diagnostic-режиме)
        """
        self.position = {
            'type': pos_type,
            'entry': entry_price,
            'tp': tp,
            'sl': sl,
            'opened_at': opened_at,
            'deadline': deadline,
            'mae_pct': 0.0,  # максимальная просадка от входа (всегда >= 0, в %)
        }

    def check_exit(self, high, low, close, current_time=None):
        """
        Проверяет нужно ли закрыть позицию.

        Returns:
            (exit_triggered, exit_reason, exit_price)
            - exit_reason: 'TP' | 'SL' | 'DEADLINE' | None
        """
        if self.position is None:
            return False, None, None

        pos_type = self.position['type']
        tp = self.position['tp']
        sl = self.position['sl']
        entry = self.position['entry']

        # --- Обновляем MAE (максимальная просадка от входа) ---
        if pos_type == 'LONG':
            adverse_pct = ((entry - low) / entry) * 100  # насколько low ушёл вниз от входа
        else:
            adverse_pct = ((high - entry) / entry) * 100  # насколько high ушёл вверх от входа
        if adverse_pct > self.position['mae_pct']:
            self.position['mae_pct'] = adverse_pct

        exit_reason = None
        exit_price = None

        if pos_type == 'LONG':
            if high >= tp:
                exit_reason, exit_price = 'TP', tp
            elif not self.disable_sl and low <= sl:
                exit_reason, exit_price = 'SL', sl
        else:  # SHORT
            if low <= tp:
                exit_reason, exit_price = 'TP', tp
            elif not self.disable_sl and high >= sl:
                exit_reason, exit_price = 'SL', sl

        # --- Deadline: принудительный выход по времени (только в diagnostic-режиме) ---
        if exit_reason is None:
            deadline = self.position.get('deadline')
            if deadline is not None and current_time is not None and current_time >= deadline:
                exit_reason, exit_price = 'DEADLINE', close

        if exit_reason is not None:
            self.last_closed_mae = self.position['mae_pct']
            self.position = None
            return True, exit_reason, exit_price

        return False, None, None

    def get_mae_pct(self):
        """MAE текущей открытой позиции (до закрытия)."""
        return self.position['mae_pct'] if self.position else 0.0

    def close_position(self):
        """Закрывает позицию (для случаев принудительного выхода)."""
        self.position = None

    def is_open(self):
        """Проверяет есть ли открытая позиция."""
        return self.position is not None