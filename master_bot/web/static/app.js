const activeListEl = document.getElementById("active-list");
const watchlistListEl = document.getElementById("watchlist-list");
const activeCountEl = document.getElementById("active-count");
const watchlistCountEl = document.getElementById("watchlist-count");
const currentCoinEl = document.getElementById("current-coin");
const currentSymbolEl = document.getElementById("current-symbol");
const signalsBody = document.getElementById("signals-body");

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema20Series = null;
let ema50Series = null;
let emaMacroSeries = null; // EMA200 на 4H (span 3200) — макро-тренд поверх текущего таймфрейма
let rsiSeries = null; 
let levelLines = []; 
let selectedCoin = null;
// Если задан — loadLevels() рисует ТОЛЬКО этот один уровень (одна стратегия,
// один график, без общей свалки всех активных уровней монеты). Ставится при
// клике по строке в списке "активных" (loadActiveWatchers), сбрасывается при
// обычном выборе монеты из watchlist/поиска.
let focusedLevel = null;

// True, когда график открыт по клику на сигнал ("Последние сигналы") —
// см. loadChart(coin, focus, signal). Нужен отдельно от focusedLevel: если
// зону сигнала не удалось найти (buildFocusFromLevelId вернул null — старый
// сигнал без level_id, или вотчер уже нигде не числится), loadLevels всё
// равно не должен скатываться в "показать все текущие уровни монеты" — это
// и была причина "дна" (кучи левых линий поверх Entry/Target/Stop).
let isSignalView = false;

// Единый цвет на каждую стратегию — чтобы куча уровней читалась с одного
// взгляда, а не сливалась в одинаковые зелёные/красные линии.
const STRATEGY_COLORS = {
  V_BOTTOM: "#4caf7d",
  V_GREEN_BOTTOM: "#26a69a",
  V_RED_TOP: "#ff9800",
  BOUNCE: "#29b6f6",
};

// "Bounce M"/"Bounce C" — короткая подпись с режимом для BOUNCE SHORT;
// остальные стратегии — как раньше, без изменений.
function friendlyStrategyWithMode(strategy, mode) {
  const base = friendlyStrategy(strategy);
  if (strategy !== "BOUNCE" || !mode) return base;
  if (mode === "CLIMAX") return "Bounce C";
  if (mode === "MIRROR") return "Bounce M";
  return base;
}
let currentPrecision = 4;
let globalCandles = []; // Храним историю свечей для привязки линий к датам


const ema20ValueEl = document.getElementById("ema20-value");
const ema50ValueEl = document.getElementById("ema50-value");
const ema200ValueEl = document.getElementById("ema200-value");

const TF_LIMITS = {
  "15m": 5760, // ~60 дней
  "1h": 4320,  // ~180 дней
  "4h": 1080,  // ~180 дней
  "1d": 365,   // ~1 год
  "1w": 104,   // ~2 года (104 недели)
  "1M": 36,    // ~3 года (36 месяцев)
};
let currentTimeframe = "15m";

function formatVolume(v) {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (abs >= 1_000) return (v / 1_000).toFixed(1) + "k";
  return String(Math.round(v));
}

// === ЛЕГЕНДА: OHLCV СВЕРХУ, ЦВЕТА СНИЗУ ===
let ohlcvLegendTextEl = null;
let bottomMarksLegendCreated = false;

function ensureOhlcvLegend() {
  // 1. Создаем текстовую шпаргалку по цветам снизу (один раз)
  if (!bottomMarksLegendCreated) {
    const chartBox = document.getElementById("chart");
    const marksLegend = document.createElement("div");
    marksLegend.style.cssText = "padding: 10px 15px; font-size:12px; color:#555b66; background: #ffffff; border-top: 1px solid #e6e8eb;";
    marksLegend.innerHTML = `
      <b>Маркеры:</b> 
      <span style="color:#8a8f98">⚪️ Старт</span> | 
      <span style="color:#f2c14e">🟡 Кандидат</span> | 
      <span style="color:#5aa9e6">🔵 Структура</span> | 
      <span style="color:#9c27b0">🟣 Старт поиска</span> | 
      <span style="color:#4caf7d">🟢 Вход</span> | 
      <span style="color:#e5654f">🔴 Сброс/Стоп</span>
    `;
    chartBox.parentElement.insertBefore(marksLegend, chartBox.nextSibling);
    bottomMarksLegendCreated = true;
  }

  // 2. Создаем контейнер для OHLCV сверху графика (внутри .chart-legend)
  if (ohlcvLegendTextEl) return ohlcvLegendTextEl;
  const container = document.querySelector(".chart-legend");
  if (!container) return null;
  
  // Включаем отображение контейнера (на случай, если прошлая версия его скрыла)
  container.style.display = "block";
  
  const item = document.createElement("div");
  item.className = "legend-item ohlcv-legend-item";
  item.innerHTML = `<span id="ohlcv-legend-text" style="color: #333a45;">—</span>`;
  container.appendChild(item);
  ohlcvLegendTextEl = document.getElementById("ohlcv-legend-text");
  
  return ohlcvLegendTextEl;
}

function setOhlcvLegend(candle, vol, rsiVal) {
  const el = ensureOhlcvLegend();
  if (!el) return;
  if (!candle) {
    el.innerHTML = "—";
    return;
  }
  const p = currentPrecision;
  const o = candle.open.toFixed(p);
  const h = candle.high.toFixed(p);
  const l = candle.low.toFixed(p);
  const c = candle.close.toFixed(p);
  const volTxt = vol ? formatVolume(vol.value) : "—";
  const rsiTxt = rsiVal !== null ? rsiVal : "—";
  
  el.innerHTML = `O ${o} &nbsp; H ${h} &nbsp; L ${l} &nbsp; C ${c} &nbsp; Vol ${volTxt} &nbsp; <b style="color:#555b66;">RSI: ${rsiTxt}</b>`;
}
// === РАСЧЕТ ИНДИКАТОРОВ ===
function computeEMA(candles, period) {
  const k = 2 / (period + 1);
  let emaPrev = null;
  const out = [];
  candles.forEach((c) => {
    const price = c.close;
    emaPrev = emaPrev === null ? price : price * k + emaPrev * (1 - k);
    out.push({ time: c.time, value: emaPrev });
  });
  return out;
}

function computeRSI(candles, period = 14) {
  if (candles.length <= period) return [];
  const rsi = [];
  let gains = 0, losses = 0;
  
  for (let i = 1; i <= period; i++) {
    const diff = candles[i].close - candles[i-1].close;
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  rsi.push({ time: candles[period].time, value: avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain/avgLoss)) });

  for (let i = period + 1; i < candles.length; i++) {
    const diff = candles[i].close - candles[i-1].close;
    const gain = diff >= 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    rsi.push({ time: candles[i].time, value: avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain/avgLoss)) });
  }
  return rsi;
}

let historyPanelBodyEl = null;
function ensureHistoryPanel() {
  if (historyPanelBodyEl) return historyPanelBodyEl;
  historyPanelBodyEl = document.getElementById("history-panel-body");
  return historyPanelBodyEl;
}

function renderHistoryPanel(historyArr) {
  const el = ensureHistoryPanel();
  if (!el) return;
  if (!historyArr || historyArr.length === 0) {
    el.innerHTML = `<div class="history-empty">История пуста.</div>`;
    return;
  }
  el.innerHTML = historyArr.map((w) => {
    const state = w.final_state || "?";
    const stateClass = state === "TRIGGERED" ? "history-state-TRIGGERED" : "history-state-DEAD";
    const diedAt = w.died_at ? new Date(w.died_at).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : "—";
    const dir = w.direction ? `<span class="dir-${w.direction}">${w.direction}</span>` : "";
    
    // Добавили w.coin и onclick для перехода на график
    // Клик по карточке истории — фокус на СВОИ собственные данные (min/max,
    // events, направление, режим), а не поиск по level_id среди текущих
    // активных вотчеров: этого вотчера там уже нет, он мёртв и вычищен
    // (clear_dead_watchers). fromHistory:true говорит loadLevels рисовать
    // напрямую по объекту ниже, не пытаясь ничего искать заново.
    const focusPayload = {
      fromHistory: true,
      min: w.level_min,
      max: w.level_max,
      level_min: w.level_min,
      level_max: w.level_max,
      level_date: w.level_date,
      level_type: w.level_type,
      level_score: w.level_score,
      direction: w.direction,
      strategy: w.strategy,
      mode: w.mode,
      events: w.events || [],
    };
    return `
      <div class="history-card" onclick='loadChart(${JSON.stringify(w.coin)}, ${JSON.stringify(focusPayload)})' style="cursor: pointer;" title="Кликни, чтобы открыть график на этом уровне">
        <div class="history-card-head">
          <span><b>${w.coin || "?"}</b> | ${w.strategy || "?"} ${dir}</span>
          <span class="history-state ${stateClass}">${state}</span>
        </div>
        <div class="history-log-text">${diedAt} · ${w.history_log || "нет лога"}</div>
      </div>
    `;
  }).join("");
}

function initChart() {
  const box = document.getElementById("chart");
  chart = LightweightCharts.createChart(box, {
    layout: { background: { color: "#ffffff" }, textColor: "#1b1d24" },
    grid: { vertLines: { color: "#e6e8eb" }, horzLines: { color: "#e6e8eb" } },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      uniformDistribution: true, // <-- Добавь вот эту строчку
      tickMarkFormatter: (time) => new Date(time * 1000).toLocaleTimeString("ru-RU", {
        timeZone: "Europe/Kyiv", hour: "2-digit", minute: "2-digit",
      }),
    },
    localization: {
      // Дата/время под курсором — плюс объём той самой свечи (ищем по
      // времени в уже загруженных данных). Верхняя легенда (Open/High/Low/
      // Close/Vol) не трогается — это отдельная подсказка, снизу у оси.
      timeFormatter: (time) => {
        const dateStr = new Date(time * 1000).toLocaleString("ru-RU", {
          timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit",
          hour: "2-digit", minute: "2-digit",
        });
        const c = globalCandles.find((c) => c.time === time);
        if (c && c.volume !== undefined && c.volume !== null) {
          return `${dateStr} · Vol: ${formatVolume(c.volume)}`;
        }
        return dateStr;
      },
    },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#4caf7d", downColor: "#e5654f",
    borderVisible: false,
    wickUpColor: "#4caf7d", wickDownColor: "#e5654f",
  });

  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
  });
  chart.priceScale("volume").applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 }, 
  });

  ema20Series = chart.addLineSeries({ color: "#f2c14e", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });
  ema50Series = chart.addLineSeries({ color: "#5aa9e6", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });
  // EMA200 на 4H (span 3200 свечей) — считается на отдельно загруженных
  // 4H-свечах (см. loadMacroEma200), не на текущем таймфрейме графика,
  // иначе на 15m это была бы совсем другая (намного более дёрганая) линия.
  // Точки редкие (раз в 4 часа) — chart сам соединяет их плавной линией
  // поверх более мелких свечей, интерполяция не нужна.
  emaMacroSeries = chart.addLineSeries({ color: "#7e57c2", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });
  rsiSeries = chart.addLineSeries({ visible: false, crosshairMarkerVisible: false }); 

  chart.subscribeCrosshairMove((param) => {
    if (!param || !param.time) {
      ema20ValueEl.textContent = "—";
      ema50ValueEl.textContent = "—";
      if (ema200ValueEl) ema200ValueEl.textContent = "—";
      setOhlcvLegend(null, null, null);
      return;
    }
    const e20 = param.seriesData.get(ema20Series);
    const e50 = param.seriesData.get(ema50Series);
    ema20ValueEl.textContent = e20 ? e20.value.toFixed(currentPrecision) : "—";
    ema50ValueEl.textContent = e50 ? e50.value.toFixed(currentPrecision) : "—";
    if (ema200ValueEl) {
      // seriesData.get отдаёт значение, только если у линии есть точка
      // РОВНО на этом времени — а у 4H-линии точки раз в 4 часа, так что
      // между ними тут будет "—", даже когда линия на графике видна (она
      // отрисована плавной интерполяцией между соседними точками).
      const eMacro = param.seriesData.get(emaMacroSeries);
      ema200ValueEl.textContent = eMacro ? eMacro.value.toFixed(currentPrecision) : "—";
    }

    const candle = param.seriesData.get(candleSeries);
    const vol = param.seriesData.get(volumeSeries);
    const rsiData = param.seriesData.get(rsiSeries);
    const rsiVal = rsiData ? rsiData.value.toFixed(1) : null;
    
    setOhlcvLegend(candle, vol, rsiVal);
  });

  new ResizeObserver(() => {
    chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
  }).observe(box);

  document.querySelectorAll(".tf-btn").forEach((btn) => {
    if (btn.dataset.tf === currentTimeframe) btn.classList.add("active");
    btn.onclick = () => {
      currentTimeframe = btn.dataset.tf;
      document.querySelectorAll(".tf-btn").forEach((b) => b.classList.toggle("active", b === btn));
      if (selectedCoin) loadChart(selectedCoin, focusedLevel);
    };
  });
}

function clearLevelLines() {
  levelLines.forEach((series) => {
    try { chart.removeSeries(series); } catch(e) {}
  });
  levelLines = [];
}

let signalLines = []; // Entry/Target/Stop линии текущего открытого сигнала
function clearSignalLines() {
  signalLines.forEach((series) => {
    try { chart.removeSeries(series); } catch(e) {}
  });
  signalLines = [];
}

// Рисует Entry/Target/Stop сплошными горизонтальными линиями через весь
// загруженный диапазон свечей — данные берутся прямо из записи сигнала
// (signals.json), поэтому работает всегда, даже если зону/точки найти
// не удалось (см. buildFocusFromSignal).
function drawSignalTradeLines(signal) {
  clearSignalLines();
  if (!globalCandles.length) return;
  const lineTimes = globalCandles.map((c) => c.time);
  const addLine = (price, color, label) => {
    if (price === null || price === undefined || Number.isNaN(Number(price))) return;
    const series = chart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: label,
      // Stop у этой стратегии может лежать далеко от Entry (десятки
      // процентов) — без этого такая линия одна тянула автомасштаб на
      // себя и сжимала все свечи в узкую полоску.
      autoscaleInfoProvider: () => null,
    });
    series.setData(lineTimes.map((t) => ({ time: t, value: Number(price) })));
    signalLines.push(series);
  };
  addLine(signal.entry, "#5aa9e6", "🔵 Entry");
  addLine(signal.target, "#4caf7d", "🟢 Target");
  addLine(signal.stop, "#e5654f", "🔴 Stop");
}

// === ЧЕЛОВЕЧЕСКИЕ ПОДПИСИ ВМЕСТО "sup1"/"АКТИВНО" ===
const STRATEGY_LABELS = {
  V_BOTTOM: "V-Bottom",
  V_GREEN_BOTTOM: "V-Green",
  V_RED_TOP: "V-Red",
};
function friendlyStrategy(code) {
  return STRATEGY_LABELS[code] || code || "?";
}
function friendlyLevelType(type) {
  if (!type) return "уровень";
  if (type.includes("poc")) return "Объём (POC)";
  if (type.includes("PMH")) return "Хай месяца";
  if (type.includes("PML")) return "Лоу месяца";
  if (type.includes("PWH")) return "Хай недели";
  if (type.includes("PWL")) return "Лоу недели";
  if (type.includes("PDH")) return "Хай дня";
  if (type.includes("PDL")) return "Лоу дня";
  if (type.includes("extreme_peak")) return "Экстремум";
  return type;
}

// === РИСУЕМ УРОВНИ СТРОГО ОТ ДАТЫ ВОЗНИКНОВЕНИЯ ===
async function loadLevels(coin) {
  clearLevelLines();
  if (globalCandles.length === 0) return;

  try {
    // ВАЖНО: берём активных вотчеров из /api/events/{coin} (data.active),
    // НЕ из /api/watchlist/active — тот фильтрует строго (currently_pierced/
    // climax_stage для BOUNCE), и вотчер может на конкретный момент не
    // проходить этот фильтр, оставаясь при этом в active_watchers.json.
    // Точки (loadEvents) берутся из того же /api/events без этой строгой
    // фильтрации — если брать уровень из другого источника, линия и точки
    // расходятся: точки есть, линии нет (или наоборот).
    const [levelsRes, eventsRes] = await Promise.all([
      fetch(`/api/levels/${encodeURIComponent(coin)}`),
      fetch(`/api/events/${encodeURIComponent(coin)}`).catch(() => null),
    ]);
    if (!levelsRes.ok) return; 
    const data = await levelsRes.json();

    let activeLevels = [];
    if (eventsRes && eventsRes.ok) {
      const eventsData = await eventsRes.json();
      activeLevels = Array.isArray(eventsData.active) ? eventsData.active : [];
    }

    const firstTime = globalCandles[0].time;

    // Рисует активный уровень НАПРЯМУЮ по данным самого вотчера — либо
    // потому что мы в фокус-режиме (уровень вотчера всегда рисуется его
    // собственными данными, без сверки с текущим macro_levels.json), либо
    // потому что в обычном просмотре его исходный уровень не нашёлся среди
    // текущих supports/resistances (пересчитался/пропал), но вотчер всё
    // ещё жив. Объявлена ДО фокус-режима ниже — иначе ReferenceError
    // (temporal dead zone): const-функция недоступна до своего объявления
    // по коду, даже если вызов физически ниже в файле при чтении.
    const drawOrphanActiveLevel = (w) => {
      const wMin = w.level_min ?? w.min;
      const wMax = w.level_max ?? w.max;
      // focusedLevel (клик по строке) не несёт direction — если пришлось
      // упасть именно на него (редкий случай, см. вызов ниже), угадываем
      // по наличию mode (он бывает только у SHORT climax/mirror).
      const direction = w.direction ?? (w.mode ? "SHORT" : "LONG");
      const isSupport = direction === "LONG";
      // Цвет линии — всегда по направлению (зелёный = поддержка/LONG,
      // красный = сопротивление/SHORT), а не по стратегии. Раньше тут был
      // STRATEGY_COLORS[w.strategy] в приоритете — у BOUNCE это давало
      // ОДИН и тот же синий цвет что для LONG, что для SHORT, хотя эмодзи
      // в подписи (🟢/🔴) честно показывал направление — несовпадение.
      const activeColor = isSupport ? "#00c853" : "#ff3d3d";
      const label = friendlyStrategyWithMode(w.strategy, w.mode);
      // Тип уровня из базы (месячный хай/лоу, недельный, дневной, POC и т.д.) —
      // то же самое, что уже показывает addLevel() в обычном виде монеты,
      // просто раньше у вотчера не было доступа к level_type вообще (не
      // экспортировался в active_watchers.json/watcher_history.json).
      const scoreSuffix = w.level_score != null ? ` ${w.level_score}` : "";
      const title = `${isSupport ? '🟢' : '🔴'} ${label} · ${friendlyLevelType(w.level_type)}${scoreSuffix}`;
      // Линия начинается с даты ФОРМИРОВАНИЯ уровня (w.level_date — заморожена
      // на вотчере в момент его рождения, из macro_levels.json), а не с
      // момента начала слежки — уровень обычно появляется раньше, чем цена
      // его реально касается. Если level_date почему-то нет (старый вотчер,
      // созданный до этого поля) — откатываемся на самое раннее событие,
      // как было раньше, лучше это, чем совсем без линии.
      let startTime = globalCandles[0].time;
      if (w.level_date) {
        const parsed = new Date(w.level_date).getTime() / 1000;
        if (!Number.isNaN(parsed)) startTime = parsed;
      } else {
        const events = (w.events || []).filter((ev) => ev.time);
        if (events.length > 0) startTime = Math.min(...events.map((ev) => ev.time));
      }
      const lineTimes = globalCandles.filter((c) => c.time >= startTime).map(c => c.time);
      if (lineTimes.length === 0) return;
      [wMax, wMin].forEach((priceValue, idx) => {
        const series = chart.addLineSeries({
          color: activeColor,
          lineWidth: 2,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          crosshairMarkerVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          title: idx === 0 ? title : "",
          // Линии уровня/зоны не должны участвовать в автомасштабе цены —
          // иначе далёкая линия (старый уровень, давно оставленный ценой)
          // сжимает все свечи в узкую полоску. Свечи — единственный
          // ориентир масштаба, линии просто рисуются там, где положено.
          autoscaleInfoProvider: () => null,
        });
        series.setData(lineTimes.map(t => ({ time: t, value: priceValue })));
        levelLines.push(series);
      });
    };

    // === ФОКУС-РЕЖИМ (клик по конкретной строке в "активных") ===
    // Правило: уровень активного вотчера — ВСЕГДА его собственные
    // level_min/level_max, замороженные на момент пробития. Никогда не
    // сверяем заново с текущим macro_levels.json (он мог пересчитаться
    // с тех пор) — это не "тот же уровень, просто пересчитанный", это
    // просто "уровень вотчера, как есть". И только "в работе"
    // (activeLevels), никогда история (watcher_history.json) — история
    // тут вообще не участвует, это отдельная, ещё не сделанная фича.
    if (focusedLevel) {
      // Клик по карточке ИСТОРИИ (DEAD/TRIGGERED) — вотчер уже удалён из
      // активных (clear_dead_watchers), искать его там бессмысленно.
      // focusedLevel в этом случае УЖЕ содержит все нужные данные (min/max/
      // events/direction/mode) — рисуем прямо по ним.
      if (focusedLevel.fromHistory) {
        drawOrphanActiveLevel(focusedLevel);
        return;
      }
      const w = activeLevels.find((x) => x.level_id === focusedLevel.level_id);
      if (!w) return; // вотчер уже не активен (умер/сработал) — рисовать нечего
      drawOrphanActiveLevel(w);
      return;
    }

    // Открыт по клику на сигнал, а зону найти не удалось (старый сигнал без
    // level_id, или вотчер уже нигде не числится) — НЕ скатываемся в показ
    // всех текущих уровней монеты. Entry/Target/Stop (см. drawSignalTradeLines)
    // уже нарисованы отдельно, добавлять сюда ещё и все чужие уровни — та
    // самая "свалка", от которой график становится нечитаемым.
    if (isSignalView) return;

    // === ОБЫЧНЫЙ ПРОСМОТР МОНЕТЫ (без фокуса) ===
    // Тут по-прежнему рисуем все текущие supports/resistances монеты,
    // подсвечивая те, что сейчас активны (сверка по level_id, если он
    // есть у обеих сторон, иначе по min/max как раньше — у "сырых"
    // macro_levels.json записей своего level_id нет).
    const findActive = (lvl) => activeLevels.find(
      (w) => Math.abs((w.level_min ?? NaN) - lvl.min) < 1e-9 && Math.abs((w.level_max ?? NaN) - lvl.max) < 1e-9
    );
    const matchedActiveKeys = new Set();
    const activeKey = (w) => w.level_id ?? `${w.level_min}_${w.level_max}`;

    const addLevel = (lvl, isSupport) => {
      const active = findActive(lvl);
      if (active) matchedActiveKeys.add(activeKey(active));
      const levelLabel = friendlyLevelType(lvl.type);
      let startTime = lvl.date ? new Date(lvl.date).getTime() / 1000 : firstTime;
      const lineTimes = globalCandles.filter(c => c.time >= startTime).map(c => c.time);
      if (lineTimes.length === 0) return;

      if (active) {
        // Активный уровень — широкая зона: две линии по границам (min/max),
        // а не одна посередине. Цвет — по направлению (см. drawOrphanActiveLevel
        // выше): зелёный support/LONG, красный resistance/SHORT. Раньше тут
        // побеждал цвет СТРАТЕГИИ (STRATEGY_COLORS) — у BOUNCE это давало один
        // и тот же синий и для LONG, и для SHORT, хотя эмодзи в подписи (🟢/🔴)
        // честно показывал направление — несовпадение путало.
        const activeColor = isSupport ? "#00c853" : "#ff3d3d";
        const label = friendlyStrategyWithMode(active.strategy, active.mode);
        const title = `${isSupport ? '🟢' : '🔴'} ${label} · ${levelLabel}${lvl.score != null ? ' ' + lvl.score : ''}`;
        [lvl.max, lvl.min].forEach((priceValue, idx) => {
          const series = chart.addLineSeries({
            color: activeColor,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            crosshairMarkerVisible: false,
            priceLineVisible: false,
            lastValueVisible: false,
            title: idx === 0 ? title : "",
            autoscaleInfoProvider: () => null,
          });
          series.setData(lineTimes.map(t => ({ time: t, value: priceValue })));
          levelLines.push(series);
        });
        return;
      }

      const color = isSupport ? "#4caf7d" : "#e5654f";
      const midPrice = (lvl.min + lvl.max) / 2;
      const series = chart.addLineSeries({
        color: color,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Solid,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        title: `${levelLabel}${lvl.score != null ? ' ' + lvl.score : ''}`,
        autoscaleInfoProvider: () => null,
      });
      series.setData(lineTimes.map(t => ({ time: t, value: midPrice })));
      levelLines.push(series);
    };

    (data.supports || []).forEach((lvl) => addLevel(lvl, true));
    (data.resistances || []).forEach((lvl) => addLevel(lvl, false));

    // Осиротевшие активные вотчеры (общий просмотр монеты, без фокуса) —
    // их исходный уровень не нашёлся среди текущих supports/resistances
    // (пересчитался/пропал), но сам вотчер всё ещё жив и отслеживается.
    // Рисуем напрямую по его собственным level_min/level_max, чтобы он не
    // пропадал с графика молча.
    activeLevels
      .filter((w) => !matchedActiveKeys.has(activeKey(w)))
      .forEach((w) => drawOrphanActiveLevel(w));
  } catch (e) {
    console.error("levels load failed", e);
  }
}

const EVENT_MARKER_STYLE = {
  ORIENTIR:    { color: "#8a8f98", shape: "circle" },   
  START:       { color: "#5aa9e6", shape: "circle" },   
  PEAK:        { color: "#f2c14e", shape: "circle" },   
  PIT:         { color: "#e5654f", shape: "circle" },   
  SCAN:        { color: "#f2c14e", shape: "circle" },   
  GOOD_GREEN:  { color: "#f2c14e", shape: "circle" },   
  TRACK_START: { color: "#8a8f98", shape: "circle" },   
  NEW_PEAK:    { color: "#f2c14e", shape: "circle" },   
  GOOD_RED:    { color: "#f2c14e", shape: "circle" },   
  ENTRY:       { color: "#4caf7d", shape: "arrowUp" },  
  CANCEL:      { color: "#5c6370", shape: "square" },   
  DEAD:        { color: "#e5654f", shape: "square" },   
  // BOUNCE — упрощённая палитра: синий = всё "до старта скана" (касание +
  // climax шаг1/шаг2), фиолетовый = момент старта скана (пробой LONG/MIRROR
  // ИЛИ уход на ATR climax — в bounce_watcher.py оба уже пишутся как
  // SWEEP_BOTTOM, один и тот же смысл), жёлтый = сама свеча скана (кандидат
  // на вход, неважно подошла или нет), красный = отмена (RUNAWAY).
  SWEEP_BOTTOM:       { color: "#9c27b0", shape: "circle" },
  RUNAWAY:            { color: "#e5654f", shape: "circle" },
  CLIMAX_NEAR_BREACH: { color: "#5aa9e6", shape: "circle" },
  CLIMAX_FAR_BREACH:  { color: "#5aa9e6", shape: "circle" },
  // LONG/MIRROR/SHORT-MIRROR: коснулись края полосы (не середины) — тот же
  // синий, что и climax-шаги, тот же смысл ("первый подтверждённый шаг").
  ZONE_TOUCH:         { color: "#5aa9e6", shape: "circle" },
};

async function loadEvents(coin) {
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) { candleSeries.setMarkers([]); return; }
    const data = await res.json();

    // "В работе" = только активные вотчеры (data.active), НИКОГДА история
    // (data.history) — история это отдельная, ещё не сделанная фича
    // (рескан). Раньше подмешивали историю сюда же, и на графике всплывали
    // точки от уже мёртвых, закрытых эпизодов того же уровня.
    const matchesFocus = (w) => !focusedLevel
      || (focusedLevel.level_id ? w.level_id === focusedLevel.level_id
          : (Math.abs((w.level_min ?? NaN) - focusedLevel.min) < 1e-9 && Math.abs((w.level_max ?? NaN) - focusedLevel.max) < 1e-9));

    // lightweight-charts требует точное совпадение времени маркера с одной
    // из свечей загруженной серии — событие со временем вне загруженного
    // диапазона библиотека не может корректно разместить (визуально
    // "слипается" в кучу у края). Отсекаем такие здесь явно, а не отдаём
    // библиотеке на откуп.
    const minCandleTime = globalCandles.length ? globalCandles[0].time : null;
    const maxCandleTime = globalCandles.length ? globalCandles[globalCandles.length - 1].time : null;
    const inRange = (t) => minCandleTime !== null && t >= minCandleTime && t <= maxCandleTime;

    const seen = new Set(); // дедуп на случай повторной записи того же (time,type)
    const markers = [];

    // Клик по карточке ИСТОРИИ — вотчер уже мёртв, в data.active (текущие
    // живые) его нет и не будет. Все его события уже приехали вместе с
    // focusedLevel (см. renderHistoryPanel) — рисуем прямо по ним, а не по
    // /api/events, который для мёртвого вотчера ничего не вернёт.
    // Открыт по сигналу, а зону найти не удалось — не подмешиваем точки
    // ВСЕХ активных вотчеров монеты (matchesFocus без focusedLevel пропускает
    // всех); честно ничего не рисуем, чем создавать ту же "свалку".
    const sourceWatchers = isSignalView && !focusedLevel
      ? []
      : (focusedLevel && focusedLevel.fromHistory)
        ? [focusedLevel]
        : (data.active || []);

    sourceWatchers.forEach((w) => {
      if (!matchesFocus(w)) return;
      (w.events || []).forEach((ev) => {
        if (!ev.time || !inRange(ev.time)) return;
        const key = `${ev.time}_${ev.type}`;
        if (seen.has(key)) return;
        seen.add(key);
        const style = EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" };
        markers.push({
          time: ev.time,
          position: "aboveBar",
          color: style.color,
          shape: style.shape,
          text: "",
        });
      });
    });

    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  } catch (e) {
    console.error("events load failed", e);   
  }
}

// EMA200 на 4H, span 3200 свечей (~530 дней) — независимая от текущего
// таймфрейма графика макро-линия тренда. Тянется отдельным запросом,
// не await'ится в loadChart (медленно двигается, не нужно ждать её ради
// остального графика). Точки редкие (раз в 4 часа) — chart сам рисует
// плавную линию между ними поверх более мелких свечей.
async function loadMacroEma200(coin) {
  if (!emaMacroSeries) return;
  try {
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=4h&limit=3200`);
    if (!res.ok) { emaMacroSeries.setData([]); return; }
    const data = await res.json();
    const candles = (data.candles || [])
      .map((c) => {
        const unixSeconds = c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time;
        return { ...c, time: unixSeconds };
      })
      .sort((a, b) => a.time - b.time);
    // Монета сменилась, пока запрос летал — не затираем уже актуальные данные чужой EMA
    if (coin !== selectedCoin) return;
    emaMacroSeries.setData(computeEMA(candles, 200));
  } catch (e) {
    console.error("macro EMA200(4H) load failed", e);
  }
}

async function loadChart(coin, focus = null, signal = null, opts = {}) {
  const skipLiveOverlay = !!opts.skipLiveOverlay;
  // --- ЗАЩИТА ОТ 404 ОШИБКИ И ПУСТЫХ КЛИКОВ ---
  if (!coin || coin === "null" || coin === "undefined") return;
  // ---------------------------------------------

  // focus задаётся только при клике по конкретному активному вотчеру
  // (см. loadActiveWatchers) — обычный выбор монеты сбрасывает фокус,
  // тогда рисуются все уровни монеты, как раньше.
  focusedLevel = focus;
  isSignalView = !!signal;

  selectedCoin = coin;
  currentCoinEl.textContent = coin;
  currentSymbolEl.textContent = "загрузка графика...";
  highlightSelection();

  clearSignalLines();
  clearLevelLines();

  // Раньше сигнал открывался в узком окне (96 свечей вокруг входа) — из-за
  // этого график выглядел обрубком. Теперь то же самое окно, что и везде
  // (как открывает история) — просто широкий обычный график с зоной и
  // Entry/Target/Stop поверх.
  const limit = TF_LIMITS[currentTimeframe] ?? 200;

  try {
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}&limit=${limit}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      currentSymbolEl.textContent = err.detail || "график недоступен";
      candleSeries.setData([]);
      candleSeries.setMarkers([]);
      volumeSeries.setData([]);
      ema20Series.setData([]);
      ema50Series.setData([]);
      emaMacroSeries.setData([]);
      rsiSeries.setData([]);
      clearLevelLines();
      return;
    }
    const data = await res.json();
    currentSymbolEl.textContent = `${data.symbol} · ${data.timeframe}`;
    currentPrecision = data.price_precision ?? 4;

    const priceFormat = { type: "price", precision: currentPrecision, minMove: 1 / Math.pow(10, currentPrecision) };
    candleSeries.applyOptions({ priceFormat });
    ema20Series.applyOptions({ priceFormat });
    ema50Series.applyOptions({ priceFormat });
    emaMacroSeries.applyOptions({ priceFormat });

    const formattedCandles = data.candles.map(c => {
      const unixSeconds = c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time;
      return { ...c, time: unixSeconds };
    });

    formattedCandles.sort((a, b) => a.time - b.time);
    globalCandles = formattedCandles; // Сохраняем историю для привязки линий

    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedCandles.map((c) => ({
      time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    ema20Series.setData(computeEMA(formattedCandles, 20));
    ema50Series.setData(computeEMA(formattedCandles, 50));
    rsiSeries.setData(computeRSI(formattedCandles, 14)); 

    chart.timeScale().fitContent();
    // Автомасштаб по цене — если на предыдущей монете потаскать ценовую ось
    // мышкой, lightweight-charts запоминает это как "ручной масштаб" и не
    // отпускает сам, даже когда данные меняются на совершенно другую монету
    // с другим диапазоном цен. Форсим обратно при каждой загрузке.
    candleSeries.priceScale().applyOptions({ autoScale: true });
    if (!skipLiveOverlay) {
      // Живые уровни/точки/EMA200 — только для обычного просмотра монеты.
      // Для симулятора это боевое состояние монеты, не имеющее отношения
      // к результату симуляции, — раньше оно рисовалось поверх/вперемешку
      // с результатом (та самая "куча всего" и "точки на секунду мелькают,
      // потом исчезают"), потому что loadChart вызывался одинаково и с
      // главной страницы, и с симулятора.
      loadLevels(coin);
      loadEvents(coin);
      loadMacroEma200(coin); // не await — медленный индикатор, не должен тормозить остальной график
    }
    if (signal) drawSignalTradeLines(signal);
  } catch (e) {
    currentSymbolEl.textContent = "ошибка загрузки графика";
    console.error(e);
  }
}

function highlightSelection() {
  document.querySelectorAll(".list-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.coin === selectedCoin);
  });
}

async function loadWatchlist() {
  // Страница может не иметь этой панели (например, отдельная страница
  // симулятора) — тихо ничего не делаем, а не падаем на null.innerHTML.
  if (!watchlistListEl) return;
  const res = await fetch("/api/watchlist");
  const data = await res.json();

  // Раньше тут ещё вычитались монеты, у которых есть активный вотчер
  // ("в работе") — список монет и список "в работе" считались двумя
  // взаимоисключающими вещами. По факту это не так: монета остаётся
  // в watchlist, пока проходит порог объёма, НЕЗАВИСИМО от того, есть
  // ли у неё сейчас активный вотчер — это не повод прятать её из общего
  // списка.
  const withLevels = data.with_levels || [];
  const withoutLevels = data.without_levels || [];

  const total = withLevels.length + withoutLevels.length;
  watchlistCountEl.textContent = total;

  if (total === 0) {
    watchlistListEl.innerHTML = "<div class='muted'>список пуст</div>";
    return;
  }

  const renderGroup = (entries) =>
    entries
      .slice()
      .sort((a, b) => a.coin.localeCompare(b.coin))
      .map(
        (info) => `
        <div class="list-item" data-coin="${info.coin}">
          <span>${info.coin}</span>
        </div>`
      )
      .join("");
      
  let html = "";
  if (withLevels.length > 0) {
    html += `<div class="list-section-header">📊 С уровнями (${withLevels.length})</div>`;
    html += renderGroup(withLevels);
  }
  if (withoutLevels.length > 0) {
    html += `<div class="list-section-header">⏳ Без уровней (${withoutLevels.length})</div>`;
    html += renderGroup(withoutLevels);
  }
  watchlistListEl.innerHTML = html;

  watchlistListEl.querySelectorAll(".list-item").forEach((div) => {
    div.onclick = () => loadChart(div.dataset.coin);
  });
}

// Статус рескана по монетам ({status: running|done, ...}) — опрашивается
// отдельно и чаще, чем общий refreshAll (раз в минуту), не через
// telegram-заглушку/notifications.json, а напрямую (см. /api/rescan_status).
// Файл маленький, частый опрос ничего не нагружает. Перерисовываем список
// "в работе" только когда статус реально поменялся, а не на каждый тик.
let rescanStatusCache = {};
async function loadRescanStatus() {
  try {
    const res = await fetch("/api/rescan_status");
    const data = await res.json();
    const changed = JSON.stringify(data) !== JSON.stringify(rescanStatusCache);
    rescanStatusCache = data;
    if (changed) loadActiveWatchers();
  } catch (e) {
    console.error("rescan status load failed", e);
  }
}
setInterval(loadRescanStatus, 4000);

async function loadActiveWatchers() {
  if (!activeListEl) return;
  const res = await fetch("/api/watchlist/active");
  const data = await res.json();
  activeCountEl.textContent = data.length;

  if (data.length === 0) {
    activeListEl.innerHTML = "<div class='muted'>сейчас никого нет</div>";
    return;
  }

  activeListEl.innerHTML = "";
  data.forEach((w) => {
    const div = document.createElement("div");
    div.className = "list-item";
    div.dataset.coin = w.coin;
    const label = friendlyStrategyWithMode(w.strategy, w.mode);
    const dotColor = STRATEGY_COLORS[w.strategy] || "#8a8f98";
    // Статус рескана этой монеты — виснет постоянно (не пропадает само),
    // чтобы было видно, что рескан вообще нажимали, и что с ним стало.
    const rescanInfo = rescanStatusCache[w.coin];
    let rescanBadge = "";
    if (rescanInfo && rescanInfo.status === "running") {
      rescanBadge = `<span title="Рескан идёт — с ${rescanInfo.since ?? ""}" style="margin-left:4px;">⏳</span>`;
    } else if (rescanInfo && rescanInfo.status === "done") {
      rescanBadge = `<span title="Рескан завершён — с ${rescanInfo.since ?? ""}, найдено сигналов: ${rescanInfo.found ?? 0}" style="margin-left:4px;">✅</span>`;
    }
    div.innerHTML = `
      <span style="flex-grow: 1;">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};margin-right:6px;"></span>
        ${w.coin ?? "?"} <span class="dir-${w.direction}">${w.direction ?? ""}</span> <span class="muted">${label}</span>
      </span>
      <span class="state-tag">${w.state}</span>
      <button title="Пересчитать структуру" onclick="triggerRescan(event, '${w.coin}')" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 14px; margin-left: 8px;">🔄</button>${rescanBadge}
    `;
    div.title = w.history_log || "";
    // Клик по конкретному активному вотчеру — фокус ТОЛЬКО на его уровень
    // (одна строка -> один график -> одна стратегия), без остальных
    // активных уровней монеты, даже если их несколько. level_id — реальный
    // ключ вотчера (min/max могут случайно совпасть у двух разных
    // вотчеров/стратегий, level_id — никогда).
    div.onclick = () => loadChart(w.coin, {
      level_id: w.level_id,
      min: w.level_min,
      max: w.level_max,
      strategy: w.strategy,
      mode: w.mode,
    });
    activeListEl.appendChild(div);
  });
}

// Ищет зону вотчера по level_id — сначала среди живых (active_watchers.json),
// потом среди истории (watcher_history.json) — для клика по сигналу: сам
// вотчер к этому моменту мог уже сработать/умереть, это нормально. Отдаёт
// объект в формате fromHistory (см. renderHistoryPanel) — та же отрисовка,
// что уже работает для "ПОСЛЕДНИЙ СКАН", просто источник данных другой.
async function buildFocusFromLevelId(coin, levelId) {
  if (!levelId) return null;
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) return null;
    const data = await res.json();
    let w = (data.active || []).find((x) => x.level_id === levelId);
    if (!w) {
      // История хранит ключ "{coin}_BOUNCE_{level_id}" (см. bc_removed_watchers
      // в background_tasks.py), самого level_id отдельным полем там нет.
      const histKey = `${coin}_BOUNCE_${levelId}`;
      w = (data.history || []).find((x) => x.key === histKey);
    }
    if (!w) return null;
    return {
      fromHistory: true,
      min: w.level_min, max: w.level_max,
      level_min: w.level_min, level_max: w.level_max,
      level_date: w.level_date, level_type: w.level_type, level_score: w.level_score, direction: w.direction,
      strategy: w.strategy, mode: w.mode,
      events: w.events || [],
    };
  } catch (e) {
    console.error("signal level lookup failed", e);
    return null;
  }
}

// Округление цены под масштаб числа — 799.7180000000001 -> 799.72,
// а для мелких монет (0.0000123...) оставляет больше знаков, чтобы не
// обнулить цену. Тот же принцип, что price_precision на самом графике,
// только без похода на биржу — тут просто по порядку величины числа.
function formatPrice(v) {
  if (v === null || v === undefined || v === "") return "";
  const num = Number(v);
  if (Number.isNaN(num)) return String(v);
  const abs = Math.abs(num);
  const decimals = abs >= 100 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  let str = num.toFixed(decimals);
  str = str.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  return str;
}

async function loadSignals() {
  if (!signalsBody) return;
  const res = await fetch("/api/signals?limit=50");
  const data = await res.json();

  if (data.length === 0) {
    signalsBody.innerHTML = "<tr><td colspan='9'>нет сигналов</td></tr>";
    return;
  }

  signalsBody.innerHTML = "";
  data.forEach((s) => {
    const tr = document.createElement("tr");
    const pct = s.result_percent;
    const pctText = pct === null || pct === undefined ? "" : `${pct > 0 ? "+" : ""}${pct}%`;
    tr.innerHTML = `
      <td>${s.date ?? ""}</td>
      <td>${s.coin ?? ""}</td>
      <td class="dir-${s.type}">${s.type ?? ""}</td>
      <td>${s.source ?? ""}</td>
      <td>${formatPrice(s.entry)}</td>
      <td>${formatPrice(s.target)}</td>
      <td>${formatPrice(s.stop)}</td>
      <td>${s.status ?? ""}</td>
      <td style="color:${pct > 0 ? '#4caf7d' : pct < 0 ? '#e5654f' : 'inherit'}">${pctText}</td>
    `;
    // Кликабельны только сигналы с unix-временем входа (сохранены после
    // этого фикса, пока только BOUNCE — см. watcher_plan.py::check_bounce).
    // У старых записей time нет и взять его неоткуда — оставляем как есть,
    // без курсора-указателя, чтобы не обещать то, что не сработает.
    if (s.coin && s.time) {
      tr.style.cursor = "pointer";
      tr.title = "Открыть график в момент сигнала";
      tr.onclick = async () => {
        const focus = s.level_id ? await buildFocusFromLevelId(s.coin, s.level_id) : null;
        loadChart(s.coin, focus, { time: s.time, entry: s.entry, target: s.target, stop: s.stop });
      };
    }
    signalsBody.appendChild(tr);
  });
}

async function loadGlobalHistory() {
  try {
    const res = await fetch("/api/history/all");
    if (!res.ok) return;
    const data = await res.json();
    renderHistoryPanel(data);
  } catch (e) {
    console.error("Ошибка загрузки глобальной истории", e);
  }
}

async function refreshAll() {
  await Promise.all([loadWatchlist(), loadActiveWatchers(), loadSignals(), loadGlobalHistory()]);
}

initChart();
refreshAll();
loadRescanStatus();

// === АВТООБНОВЛЕНИЕ РАЗ В МИНУТУ ===
setInterval(async () => {
  refreshAll(); 
  
  if (selectedCoin) {
    try {
      const res = await fetch(`/api/ohlcv/${encodeURIComponent(selectedCoin)}?timeframe=${currentTimeframe}&limit=10`);
      if (!res.ok) return;
      const data = await res.json();

      const formattedCandles = data.candles.map(c => ({
        ...c,
        time: c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time,
      })).sort((a, b) => a.time - b.time);

      formattedCandles.forEach(c => {
        try {
          candleSeries.update(c);
          volumeSeries.update({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
          });
        } catch (e) {
           // Библиотека игнорирует старые свечи
        }
      });
      
      loadEvents(selectedCoin);
    } catch (e) {
      console.error("Ошибка автообновления свечей:", e);
    }
  }
}, 60000);

// === КНОПКИ "СБРОСИТЬ ВОТЧЕРОВ" / "REBUILD УРОВНЕЙ" (только на главной) ===
const resetWatchersBtnEl = document.getElementById("reset-watchers-btn");
if (resetWatchersBtnEl) {
  resetWatchersBtnEl.onclick = async () => {
    if (!confirm("Точно сбросить ВСЕХ живых вотчеров? Уровни (macro_levels.json/watchlist.json) не тронутся — только состояние отслеживания. Отменить нельзя.")) return;
    resetWatchersBtnEl.disabled = true;
    try {
      const res = await fetch("/api/reset_watchers", { method: "POST" });
      const data = await res.json();
      alert(data.message || "Готово");
      // Флаг-слушатель в run_web.py проверяет раз в секунду — даём секунду
      // с запасом, потом обновляем список сами, а не ждём следующего
      // автообновления или ручной перезагрузки страницы.
      setTimeout(() => { loadActiveWatchers(); loadWatchlist(); }, 1500);
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      resetWatchersBtnEl.disabled = false;
    }
  };
}

const rebuildLevelsBtnEl = document.getElementById("rebuild-levels-btn");
if (rebuildLevelsBtnEl) {
  rebuildLevelsBtnEl.onclick = async () => {
    if (!confirm("Точно пересчитать ВСЕ уровни с нуля? Это дольше (фетч истории по ~70 монетам), может занять пару минут. Отменить нельзя.")) return;
    rebuildLevelsBtnEl.disabled = true;
    try {
      const res = await fetch("/api/rebuild_levels", { method: "POST" });
      const data = await res.json();
      alert(data.message || "Готово");
      // Rebuild реально идёт пару минут — сразу освежаем список (обычно
      // без изменений), а не молчим до случайного следующего обновления.
      // Полную свежую картину увидишь, когда rebuild реально закончится
      // (или просто обнови страницу вручную через пару минут).
      loadWatchlist();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      rebuildLevelsBtnEl.disabled = false;
    }
  };
}

// === КНОПКИ "ОЧИСТИТЬ СИГНАЛЫ" / "ОЧИСТИТЬ ПОСЛЕДНИЙ СКАН" ===
// В отличие от reset-watchers/rebuild — без флага и без ожидания:
// signals.json/watcher_history.json не живут в памяти сканера, backend
// дашборда пишет файл сам и сразу же, поэтому просто перезагружаем
// соответствующую панель сразу после ответа, без setTimeout.
const clearSignalsBtnEl = document.getElementById("clear-signals-btn");
if (clearSignalsBtnEl) {
  clearSignalsBtnEl.onclick = async () => {
    if (!confirm("Точно очистить список сигналов? Отменить нельзя.")) return;
    clearSignalsBtnEl.disabled = true;
    try {
      const res = await fetch("/api/signals/clear", { method: "POST" });
      const data = await res.json();
      alert(data.message || "Готово");
      loadSignals();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      clearSignalsBtnEl.disabled = false;
    }
  };
}

const clearHistoryBtnEl = document.getElementById("clear-history-btn");
if (clearHistoryBtnEl) {
  clearHistoryBtnEl.onclick = async () => {
    if (!confirm("Точно очистить \"Последний скан\" (историю смертей/срабатываний)? Отменить нельзя.")) return;
    clearHistoryBtnEl.disabled = true;
    try {
      const res = await fetch("/api/history/clear", { method: "POST" });
      const data = await res.json();
      alert(data.message || "Готово");
      loadGlobalHistory();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      clearHistoryBtnEl.disabled = false;
    }
  };
}
// Маленькое модальное окошко с датой вместо голого confirm() — предзаполнено
// "неделя назад" (просто жми "Рескан"), можно поменять на свою дату. Собрано
// прямо в JS без правки index.html (тот же приём, что и панель стратегий ниже).
function formatDatetimeLocal(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function showRescanDialog(coin) {
  return new Promise((resolve) => {
    const defaultDate = new Date(Date.now() - 7 * 24 * 3600 * 1000);
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed; inset:0; background:rgba(0,0,0,0.4); display:flex; align-items:center; justify-content:center; z-index:9999;";
    overlay.innerHTML = `
      <div style="background:#fff; border-radius:8px; padding:20px; min-width:280px; font-size:14px; color:#1b1d24; box-shadow:0 4px 20px rgba(0,0,0,0.2);">
        <div style="font-weight:600; margin-bottom:10px;">🔄 Рескан ${coin}</div>
        <div style="margin-bottom:10px; color:#555b66;">
          Честно повторить путь монеты (BOUNCE) от даты до сейчас — текущие
          вотчеры будут заархивированы, реплей пересоберёт всё заново.
        </div>
        <label style="display:block; margin-bottom:14px;">
          Дата начала:
          <input type="datetime-local" id="rescan-since-input" value="${formatDatetimeLocal(defaultDate)}"
                 style="display:block; margin-top:4px; width:100%; box-sizing:border-box; padding:4px;">
        </label>
        <div style="text-align:right;">
          <button id="rescan-cancel-btn" class="sim-btn" style="margin-right:8px;">Отмена</button>
          <button id="rescan-confirm-btn" class="sim-btn">Рескан</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (result) => { document.body.removeChild(overlay); resolve(result); };
    overlay.querySelector("#rescan-cancel-btn").onclick = () => cleanup(null);
    overlay.querySelector("#rescan-confirm-btn").onclick = () => {
      const val = overlay.querySelector("#rescan-since-input").value;
      const since = val ? Math.floor(new Date(val).getTime() / 1000) : null;
      cleanup(since);
    };
  });
}

window.triggerRescan = async function(event, coin) {
  event.stopPropagation();
  const since = await showRescanDialog(coin);
  if (since === null) return; // отмена
  // Запоминаем, что именно было открыто ДО рескана (конкретный вотчер,
  // если смотрели через фокус) — иначе перезагрузка после рескана падает
  // на loadChart(coin) без фокуса, то есть на общий вид "все текущие
  // уровни + обе стратегии разом", а не на то, что реально смотрели.
  const focusBeforeRescan = focusedLevel;
  try {
      const res = await fetch(`/api/rescan/${encodeURIComponent(coin)}?since=${since}`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
          console.log(`[RESCAN] Заявка отправлена для ${coin}. Ждем тик сканера...`);
          alert(data.message || "Рескан запрошен");
          setTimeout(() => { if (selectedCoin === coin) loadChart(coin, focusBeforeRescan); }, 2000);
      } else {
          alert(data.detail || "Ошибка рескана");
      }
  } catch (e) {
      console.error("Ошибка рескана", e);
      alert("Ошибка: " + e);
  }
};
// === ПАНЕЛЬ ВКЛ/ВЫКЛ СТРАТЕГИЙ ===
// Собирается прямо в JS (без правки index.html — файл вне репозитория,
// см. .gitignore *.html) и вставляется фиксированной плашкой в правый
// верхний угол. Пишет/читает config.json через /api/strategies —
// тот же файл, что читает background_tasks.py::crypto_orchestrator
// (перечитывается раз в секунду, поэтому переключение применяется на
// ближайшем скане без рестарта бота).
const STRATEGY_TOGGLE_LABELS = {
  VB: "V_BOTTOM",
  VGB: "V_GREEN_BOTTOM",
  VRT: "V_RED_TOP",
  BOUNCE: "BOUNCE",
};

function buildStrategiesPanel() {
  // Вставляем СРАЗУ ПОСЛЕ последней кнопки таймфрейма (.tf-switch) — тот
  // же тулбар. Раньше тут ещё был fallback на "sim-mode-btn" (кнопка
  // режима симуляции на главной странице) — с тех пор как симулятор стал
  // отдельной страницей на своём sim.js, кнопки sim-mode-btn на дашборде
  // больше нет вообще, оставлен только актуальный якорь.
  const anchor = document.querySelector(".tf-switch .tf-btn:last-child")
    || document.querySelector(".tf-switch");
  if (!anchor) {
    console.warn("[strategies-panel] не нашлось .tf-switch — панель стратегий не вставлена");
    return;
  }

  const toggle = document.createElement("button");
  toggle.id = "strategies-toggle";
  toggle.className = "tf-btn";
  toggle.textContent = "🧩 Стратегии";
  toggle.type = "button";
  anchor.insertAdjacentElement("afterend", toggle);

  const panel = document.createElement("div");
  panel.id = "strategies-panel";
  panel.style.cssText = `
    position: absolute; z-index: 1000; margin-top: 4px;
    background: #1b1d24; border: 1px solid #262832; border-radius: 8px;
    padding: 10px 12px; font-size: 13px; color: #e6e6e6;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    display: none;
  `;
  panel.innerHTML = `<div style="margin-bottom:6px; color:#9aa0ab; font-weight:600;">🧩 Стратегии</div>
    <div id="strategies-buttons" style="display:flex; flex-direction:column; gap:4px;"></div>`;
  document.body.appendChild(panel);

  toggle.onclick = () => {
    const isOpen = panel.style.display !== "none";
    if (isOpen) {
      panel.style.display = "none";
      return;
    }
    // Позиционируем панель под самой кнопкой — считаем координаты в момент
    // открытия (на случай ресайза/скролла), а не один раз при старте.
    const rect = toggle.getBoundingClientRect();
    panel.style.left = `${rect.left + window.scrollX}px`;
    panel.style.top = `${rect.bottom + window.scrollY}px`;
    panel.style.display = "block";
    refreshStrategiesPanel();
  };

  // Клик мимо панели — закрыть, чтобы не оставалась висеть поверх графика
  document.addEventListener("click", (e) => {
    if (panel.style.display === "none") return;
    if (e.target === toggle || panel.contains(e.target)) return;
    panel.style.display = "none";
  });
}

async function refreshStrategiesPanel() {
  const container = document.getElementById("strategies-buttons");
  if (!container) return;
  try {
    const res = await fetch("/api/strategies");
    const states = await res.json();
    container.innerHTML = "";
    for (const tag of Object.keys(STRATEGY_TOGGLE_LABELS)) {
      const enabled = !!states[tag];
      const btn = document.createElement("button");
      btn.textContent = `${enabled ? "✅" : "❌"} ${STRATEGY_TOGGLE_LABELS[tag]}`;
      btn.style.cssText = `
        background: ${enabled ? "#1f2e22" : "#2a1f1f"};
        border: 1px solid ${enabled ? "#3a5c3f" : "#5c3a3a"};
        color: #e6e6e6; border-radius: 5px; padding: 5px 8px;
        cursor: pointer; text-align: left; font-size: 12px;
      `;
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await fetch(`/api/strategies/${tag}/toggle`, { method: "POST" });
          await refreshStrategiesPanel();
        } catch (e) {
          console.error("Ошибка переключения стратегии", tag, e);
          btn.disabled = false;
        }
      };
      container.appendChild(btn);
    }
  } catch (e) {
    console.error("Не удалось загрузить состояние стратегий", e);
  }
}

// Панель "🧩 Стратегии" управляет config.json, который читает БОЕВОЙ
// сканер (background_tasks.py) — показываем её только на главной странице
// (есть watchlist-list). На странице симулятора её не должно быть вообще:
// это два разных, независимых понятия "какие стратегии сейчас работают" —
// выбор стратегии для конкретного прогона симуляции ещё не сделан, но
// когда будет, это точно не тот же переключатель и не тот же config.json.
function isMainDashboardPage() {
  return !!document.getElementById("watchlist-list");
}

if (isMainDashboardPage()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildStrategiesPanel);
  } else {
    buildStrategiesPanel();
  }
}