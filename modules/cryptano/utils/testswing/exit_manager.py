"""
exit_manager.py
===============
Логика закрытия позиции по TP/SL.
Вызывается на каждой свече для проверки условий выхода.
"""


class ExitManager:
    """Управляет выходом из позиции по TP и SL."""
    
    def __init__(self):
        self.position = None  # {'type': 'LONG'/'SHORT', 'entry': price, 'tp': price, 'sl': price}
    
    def open_position(self, pos_type, entry_price, tp, sl):
        """Открывает позицию для отслеживания."""
        self.position = {
            'type': pos_type,
            'entry': entry_price,
            'tp': tp,
            'sl': sl,
        }
    
    def check_exit(self, high, low, close):
        """
        Проверяет нужно ли закрыть позицию.
        
        Returns:
            (exit_triggered, exit_reason, exit_price)
            - exit_triggered: bool
            - exit_reason: 'TP' | 'SL' | None
            - exit_price: цена выхода или None
        """
        if self.position is None:
            return False, None, None
        
        pos_type = self.position['type']
        tp = self.position['tp']
        sl = self.position['sl']
        
        if pos_type == 'LONG':
            # Проверяем TP (high дошла до TP)
            if high >= tp:
                self.position = None
                return True, 'TP', tp
            
            # Проверяем SL (low упала ниже SL)
            if low <= sl:
                self.position = None
                return True, 'SL', sl
        
        else:  # SHORT
            # Проверяем TP (low дошла до TP)
            if low <= tp:
                self.position = None
                return True, 'TP', tp
            
            # Проверяем SL (high поднялась выше SL)
            if high >= sl:
                self.position = None
                return True, 'SL', sl
        
        return False, None, None
    
    def close_position(self):
        """Закрывает позицию (для случаев принудительного выхода)."""
        self.position = None
    
    def is_open(self):
        """Проверяет есть ли открытая позиция."""
        return self.position is not None