# -*- coding: utf-8 -*-
import os
import json
import glob
import pandas as pd
import numpy as np

class StandalonePumpAnalyzer:
    CONFIG = {
        # ==========================================
        # 1. ПУТИ К ФАЙЛАМ И ПАПКАМ
        # ==========================================
        'LEVELS_JSON_PATH': r'D:\bot\test\levels_timeline_2026_01.json',
        'DATA_CACHE_FOLDER': r'D:\bot\test\data_cache',
        'CACHE_FILE_EXT': '.csv',           # Расширение файлов в кеше ('.csv', '.pkl', etc.)
        
        # ==========================================
        # 2. НАСТРОЙКИ ПАРСЕРА ДЛЯ ТВОЕГО JSON
        # ==========================================
        'JSON_SECTION_NAME': 'resistances', # Что ищем внутри монеты: 'resistances' или 'supports'
        'JSON_PRICE_FIELD': 'max',          # Какую цену брать из зоны: 'max', 'min' или 'mid' (среднее)

        # ==========================================
        # 3. НАСТРОЙКИ АНАЛИЗА ПАМПОВ И ПИКОВ
        # ==========================================
        'MIN_PUMP_FILTER_PCT': 10.0,        # Минимальный памп от уровня (%): всё что ниже — отсеиваем
        'PULLBACK_RESET_PCT': 11.0,         # Откат вниз от локального хая (%), чтобы засчитать конец пика
        'MAX_DAYS_AFTER_BREACH': 30,        # Сколько дней максимум смотреть график после момента пробоя
        'TIMEFRAME_MINUTES': 15,            # Таймфрейм свечей (15м = 96 свечей в сутки)
        'HEIGHT_BINS': [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 70.0, 1000.0],
        # ==========================================
        # 4. НАСТРОЙКИ КОЛОНОК В ФАЙЛАХ DATA_CACHE
        # ==========================================
        'COL_TIME': 'open_time',            # Или 'timestamp', 'time', 'date'
        'COL_HIGH': 'high',
        'COL_LOW': 'low',
        'COL_CLOSE': 'close',
    }

    def __init__(self):
        self.results = []

    def _load_json_events(self) -> list:
        json_path = self.CONFIG['LEVELS_JSON_PATH']
        if not os.path.exists(json_path):
            print(f"❌ Файл с уровнями не найден: {json_path}")
            return []

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"❌ Ошибка чтения JSON {json_path}: {e}")
            return []

        events = []
        section_name = self.CONFIG['JSON_SECTION_NAME']
        price_field = self.CONFIG['JSON_PRICE_FIELD']

        # Проходим по 3 уровням вложенности: "2026-01-01 00:00:00" -> "BTC" -> "resistances"
        if isinstance(data, dict):
            for time_str, coins_dict in data.items():
                if not isinstance(coins_dict, dict):
                    continue
                
                for coin_name, coin_data in coins_dict.items():
                    if not isinstance(coin_data, dict):
                        continue
                    
                    level_items = coin_data.get(section_name, [])
                    if not isinstance(level_items, list):
                        continue

                    for item in level_items:
                        if not isinstance(item, dict):
                            continue

                        # Вычисляем цену уровня
                        if price_field == 'mid':
                            lvl_min = float(item.get('min', 0.0))
                            lvl_max = float(item.get('max', 0.0))
                            level_price = (lvl_min + lvl_max) / 2.0 if (lvl_min and lvl_max) else float(item.get('max', 0.0))
                        else:
                            level_price = float(item.get(price_field, item.get('max', 0.0)))

                        if level_price > 0:
                            events.append({
                                'coin': str(coin_name).strip(),
                                'time': str(time_str).strip(),
                                'level': level_price
                            })
        
        return events

    def _load_coin_candles(self, coin_name: str) -> pd.DataFrame:
        folder = self.CONFIG['DATA_CACHE_FOLDER']
        ext = self.CONFIG['CACHE_FILE_EXT']
        
        file_path = os.path.join(folder, f"{coin_name}{ext}")
        if not os.path.exists(file_path):
            matched = glob.glob(os.path.join(folder, f"*{coin_name}*{ext}"))
            if not matched:
                return pd.DataFrame()
            file_path = matched[0]

        try:
            if ext.lower() == '.csv':
                df = pd.read_csv(file_path)
            elif ext.lower() in ('.pkl', '.pickle'):
                df = pd.read_pickle(file_path)
            else:
                df = pd.read_csv(file_path)

            df.columns = [str(c).lower().strip() for c in df.columns]
            return df
        except Exception as e:
            print(f"⚠️ Ошибка чтения кеша для {coin_name}: {e}")
            return pd.DataFrame()

    def _slice_from_breach(self, df: pd.DataFrame, breach_time: str) -> pd.DataFrame:
        col_time = self.CONFIG['COL_TIME'].lower()
        if col_time not in df.columns or df.empty:
            return pd.DataFrame()

        df[col_time] = df[col_time].astype(str)
        sliced_df = df[df[col_time] >= str(breach_time)].reset_index(drop=True)
        
        candles_per_day = int(1440 / self.CONFIG['TIMEFRAME_MINUTES'])
        max_candles = self.CONFIG['MAX_DAYS_AFTER_BREACH'] * candles_per_day
        return sliced_df.iloc[:max_candles]

    def analyze_event(self, event_data: dict):
        coin_name = event_data['coin']
        level_price = event_data['level']
        breach_time = event_data['time']

        df = self._load_coin_candles(coin_name)
        df_sliced = self._slice_from_breach(df, breach_time)
        
        if df_sliced.empty:
            return

        col_high = self.CONFIG['COL_HIGH'].lower()
        col_low = self.CONFIG['COL_LOW'].lower()
        if col_high not in df_sliced.columns or col_low not in df_sliced.columns:
            return

        global_max_high = level_price
        local_max_high = level_price
        peaks = []
        in_pullback = False

        for _, row in df_sliced.iterrows():
            high_val = float(row[col_high])
            low_val = float(row[col_low])

            if high_val > global_max_high:
                global_max_high = high_val

            if high_val > local_max_high:
                local_max_high = high_val
                in_pullback = False

            pullback_pct = (local_max_high - low_val) / local_max_high * 100.0

            if pullback_pct >= self.CONFIG['PULLBACK_RESET_PCT'] and not in_pullback:
                peak_height_pct = (local_max_high - level_price) / level_price * 100.0
                if peak_height_pct >= self.CONFIG['MIN_PUMP_FILTER_PCT']:
                    peaks.append(peak_height_pct)
                in_pullback = True
                local_max_high = high_val

        final_peak_pct = (global_max_high - level_price) / level_price * 100.0
        if final_peak_pct >= self.CONFIG['MIN_PUMP_FILTER_PCT'] and (not peaks or max(peaks) < final_peak_pct):
            peaks.append(final_peak_pct)

        if not peaks and final_peak_pct < self.CONFIG['MIN_PUMP_FILTER_PCT']:
            return

        total_peaks_count = len(peaks) if peaks else 1
        max_height_pct = (global_max_high - level_price) / level_price * 100.0

        self.results.append({
            'coin': coin_name,
            'breach_time': breach_time,
            'level_price': level_price,
            'max_height_pct': max_height_pct,
            'peaks_count': total_peaks_count,
            'peaks_history': [round(p, 2) for p in peaks]
        })

    def run_analysis(self):
        events = self._load_json_events()
        if not events:
            print("❌ Список событий пуст. Проверь путь к файлу JSON и структуру данных.")
            return

        print(f"✅ Найдено {len(events)} уровней в разделе '{self.CONFIG['JSON_SECTION_NAME']}'.")
        print("⏳ Подтягиваем свечи из кеша и рассчитываем статистику пампов...")
        
        for ev in events:
            self.analyze_event(ev)

        self.print_report()

    def print_report(self):
        if not self.results:
            print(f"\n❌ Ни один памп не прошел фильтр (рост был ниже {self.CONFIG['MIN_PUMP_FILTER_PCT']}% или не найдены CSV в кеше).")
            return

        df_res = pd.DataFrame(self.results)
        total_coins = len(df_res)

        print("\n" + "="*70)
        print("📊 СТАТИСТИКА ПАМПОВ И ПИКОВ (РАЗВЕРНУТЫЙ ОТЧЕТ ПО ШТУКАМ)")
        print("="*70)
        print(f"Успешно обработано пробоев:     {total_coins}")
        print(f"Фильтр минимального пампа:      >={self.CONFIG['MIN_PUMP_FILTER_PCT']}%")
        print(f"Порог отката для нового пика:   >={self.CONFIG['PULLBACK_RESET_PCT']}%\n")

        # --- 1. РАСПРЕДЕЛЕНИЕ ВЫСОТЫ ПО ШТУКАМ ---
        print("--- 1. РАСПРЕДЕЛЕНИЕ ВЫСОТЫ РОСТА (ПО ШТУКАМ МОНЕТ) ---")
        bins = self.CONFIG.get('HEIGHT_BINS', [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 70.0, 1000.0])
        
        cum_count = 0
        for i in range(len(bins) - 1):
            low_b = bins[i]
            high_b = bins[i+1]
            
            # Считаем, сколько монет попало в текущий диапазон
            if i == len(bins) - 2:
                # Последний диапазон включает верхнюю границу
                mask = (df_res['max_height_pct'] >= low_b) & (df_res['max_height_pct'] <= high_b)
                label = f"от {low_b:5.1f}% и выше   "
            else:
                mask = (df_res['max_height_pct'] >= low_b) & (df_res['max_height_pct'] < high_b)
                label = f"от {low_b:5.1f}% до {high_b:5.1f}%"
            
            count = int(mask.sum())
            cum_count += count
            pct_step = (count / total_coins) * 100.0
            pct_cum = (cum_count / total_coins) * 100.0
            
            print(f"Рост {label} : {count:4d} шт. ({pct_step:5.1f}%) | Умерло к этой отметке: {pct_cum:5.1f}%")

        print("\n--- Экстремальные отметки ---")
        print(f"Минимальный памп:              {df_res['max_height_pct'].min():6.2f}%")
        print(f"Абсолютный рекорд:             {df_res['max_height_pct'].max():6.2f}%\n")

        # --- 2. ПИКИ ---
        print("--- 2. РАСПРЕДЕЛЕНИЕ ПО КОЛИЧЕСТВУ ПИКОВ ---")
        peak_counts = df_res['peaks_count'].value_counts().sort_index()
        for count, num_coins in peak_counts.items():
            pct_total = (num_coins / len(df_res)) * 100.0
            print(f"Сделали ровно {count:2d} пик(а/ов):  {num_coins:4d} шт.  ({pct_total:5.1f}%)")
        print("="*70)

        out_csv = "pump_stats_report_timeline.csv"
        df_res[['coin', 'breach_time', 'level_price', 'max_height_pct', 'peaks_count', 'peaks_history']].to_csv(out_csv, index=False)
        print(f"📁 Подробный отчет сохранен в: {out_csv}\n")


if __name__ == "__main__":
    analyzer = StandalonePumpAnalyzer()
    analyzer.run_analysis()