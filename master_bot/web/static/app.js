const activeListEl = document.getElementById("active-list");
const watchlistListEl = document.getElementById("watchlist-list");
const simListEl = document.getElementById("sim-list");
const activeCountEl = document.getElementById("active-count");
const watchlistCountEl = document.getElementById("watchlist-count");
const simCountEl = document.getElementById("sim-count");
const currentCoinEl = document.getElementById("current-coin");
const currentSymbolEl = document.getElementById("current-symbol");
const signalsBody = document.getElementById("signals-body");

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema20Series = null;
let ema50Series = null;let rsiSeries = null; 
let levelLines = []; 
let selectedCoin = null;
let currentPrecision = 4;
let globalCandles = []; // Храним историю свечей для привязки линий к датам

// === СИМУЛЯЦИЯ ===
let simMode = false;
let simStartTime = null; // unix seconds клика по свече
let simRunning = false;
let simLevelLines = []; // линии уровней симуляции (жёлтый/фиолетовый), отдельно от боевых levelLines
let simPlaybackTimer = null; // таймер анимации плейбека

function clearSimLevelLines() {
  simLevelLines.forEach((series) => { try { chart.removeSeries(series); } catch (e) {} });
  simLevelLines = [];
}

function stopSimPlayback() {
  if (simPlaybackTimer) {
    clearInterval(simPlaybackTimer);
    simPlaybackTimer = null;
  }
}

const ema20ValueEl = document.getElementById("ema20-value");
const ema50ValueEl = document.getElementById("ema50-value");

const TF_LIMITS = {
  "15m": 5760,
  "1h": 1440,
  "4h": 360,
  "1d": 60,
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
    marksLegend.style.cssText = "padding: 10px 15px; font-size:12px; color:#8a8f98; background: #0e0f13; border-top: 1px solid #1b1d24;";
    marksLegend.innerHTML = `
      <b>Маркеры:</b> 
      <span style="color:#8a8f98">⚪️ Старт</span> | 
      <span style="color:#f2c14e">🟡 Пик (Структура)</span> | 
      <span style="color:#4dd0e1">🔵 Поиск</span> | 
      <span style="color:#ff8a65">🟠 Кандидат</span> | 
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
  item.innerHTML = `<span id="ohlcv-legend-text" style="color: #cfd3da;">—</span>`;
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
  
  el.innerHTML = `O ${o} &nbsp; H ${h} &nbsp; L ${l} &nbsp; C ${c} &nbsp; Vol ${volTxt} &nbsp; <b style="color:#b2b5be;">RSI: ${rsiTxt}</b>`;
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
    return `
      <div class="history-card" onclick="loadChart('${w.coin}')" style="cursor: pointer;" title="Кликни, чтобы открыть график">
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
    layout: { background: { color: "#0e0f13" }, textColor: "#cfd3da" },
    grid: { vertLines: { color: "#1b1d24" }, horzLines: { color: "#1b1d24" } },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time) => new Date(time * 1000).toLocaleTimeString("ru-RU", {
        timeZone: "Europe/Kyiv", hour: "2-digit", minute: "2-digit",
      }),
    },
    localization: {
      timeFormatter: (time) => new Date(time * 1000).toLocaleString("ru-RU", {
        timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit",
        hour: "2-digit", minute: "2-digit",
      }),
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
  rsiSeries = chart.addLineSeries({ visible: false, crosshairMarkerVisible: false }); 

  chart.subscribeCrosshairMove((param) => {
    if (!param || !param.time) {
      ema20ValueEl.textContent = "—";
      ema50ValueEl.textContent = "—";
      setOhlcvLegend(null, null, null);
      return;
    }
    const e20 = param.seriesData.get(ema20Series);
    const e50 = param.seriesData.get(ema50Series);
    ema20ValueEl.textContent = e20 ? e20.value.toFixed(currentPrecision) : "—";
    ema50ValueEl.textContent = e50 ? e50.value.toFixed(currentPrecision) : "—";

    const candle = param.seriesData.get(candleSeries);
    const vol = param.seriesData.get(volumeSeries);
    const rsiData = param.seriesData.get(rsiSeries);
    const rsiVal = rsiData ? rsiData.value.toFixed(1) : null;
    
    setOhlcvLegend(candle, vol, rsiVal);
  });

  chart.subscribeClick((param) => {
    if (!simMode || !param || !param.time) return;
    simStartTime = param.time;
    updateSimUI();
  });

  new ResizeObserver(() => {
    chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
  }).observe(box);

  document.querySelectorAll(".tf-btn").forEach((btn) => {
    if (btn.dataset.tf === currentTimeframe) btn.classList.add("active");
    btn.onclick = () => {
      currentTimeframe = btn.dataset.tf;
      document.querySelectorAll(".tf-btn").forEach((b) => b.classList.toggle("active", b === btn));
      if (selectedCoin) loadChart(selectedCoin);
    };
  });

  document.getElementById("sim-mode-btn").onclick = () => {
    simMode = !simMode;
    if (!simMode) {
      simStartTime = null;
      stopSimPlayback();
      clearSimLevelLines();
      candleSeries.setMarkers([]);
      if (selectedCoin) loadEvents(selectedCoin); // возвращаем боевые маркеры
    }
    updateSimUI();
  };
  document.getElementById("sim-cancel-btn").onclick = () => {
    simStartTime = null;
    stopSimPlayback();
    clearSimLevelLines();
    candleSeries.setMarkers([]);
    if (selectedCoin) loadEvents(selectedCoin);
    updateSimUI();
  };
  document.getElementById("sim-run-btn").onclick = runSimulation;
}

function updateSimUI() {
  const modeBtn = document.getElementById("sim-mode-btn");
  const status = document.getElementById("sim-status");
  const runBtn = document.getElementById("sim-run-btn");
  const cancelBtn = document.getElementById("sim-cancel-btn");

  modeBtn.classList.toggle("active", simMode);

  if (!simMode) {
    status.textContent = "";
    runBtn.style.display = "none";
    cancelBtn.style.display = "none";
    return;
  }

  if (simStartTime) {
    const d = new Date(simStartTime * 1000);
    status.textContent = "Старт: " + d.toLocaleString("ru-RU", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    runBtn.style.display = "";
    cancelBtn.style.display = "";
  } else {
    status.textContent = "Кликни свечу на графике — точка старта";
    runBtn.style.display = "none";
    cancelBtn.style.display = "none";
  }
}

async function runSimulation() {
  if (!selectedCoin || !simStartTime || simRunning) return;
  simRunning = true;
  stopSimPlayback();

  const status = document.getElementById("sim-status");
  const runBtn = document.getElementById("sim-run-btn");
  runBtn.disabled = true;
  status.textContent = "Считаю...";

  // Бэкенд ждёт 'YYYY-MM-DD HH:MM' в UTC (см. simulate_engine.run_simulation)
  const iso = new Date(simStartTime * 1000).toISOString(); // "2026-07-01T12:00:00.000Z"
  const startParam = iso.slice(0, 16).replace("T", " ");

  try {
    const res = await fetch(`/api/simulate/${encodeURIComponent(selectedCoin)}?start=${encodeURIComponent(startParam)}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      status.textContent = data.detail || "ошибка симуляции";
      return;
    }

    const episodes = [...(data.active || []), ...(data.history || [])];

    drawSimLevels(data.levels || []);
    playSimulation(episodes);

    if (episodes.length === 0) {
      status.textContent = "Старт: " + startParam + " · пробоев не найдено (уровней проверено: " + (data.levels || []).length + ")";
    } else {
      const summary = episodes.map((ep) => `${friendlyStrategy(ep.strategy)}:${ep.state}`).join(", ");
      status.textContent = `Найдено: ${summary}`;
    }
  } catch (e) {
    console.error("simulate failed", e);
    status.textContent = "ошибка запроса";
  } finally {
    runBtn.disabled = false;
    simRunning = false;
  }
}

// === УРОВНИ СИМУЛЯЦИИ — жёлтый (support) / фиолетовый (resistance),
// отдельным цветом от боевых (зелёный/красный из loadLevels), чтобы не путать
// "что сейчас реально висит в боте" с "что проверялось в этом прогоне" ===
function drawSimLevels(levels) {
  clearSimLevelLines();
  if (!levels.length || !simStartTime || !globalCandles.length) return;

  const relevant = globalCandles.filter((c) => c.time >= simStartTime);
  if (relevant.length === 0) return;

  levels.forEach((lvl) => {
    const midPrice = (lvl.min + lvl.max) / 2;
    const color = lvl.is_support ? "#f2c14e" : "#b46ee0"; // жёлтый / фиолетовый
    const series = chart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: `🧪 ${friendlyLevelType(lvl.type)} ${lvl.score ?? ""}`,
    });
    series.setData(relevant.map((c) => ({ time: c.time, value: midPrice })));
    simLevelLines.push(series);
  });
}

// === ПЛЕЙБЕК: "проигрываем" сканирование свечей от точки старта до сейчас,
// с бегущим курсором и постепенным появлением маркеров — наглядно видно,
// что реально идёт скан, а не просто мгновенно нарисовался готовый результат ===
function playSimulation(episodes) {
  stopSimPlayback();

  const allMarkers = [];
  episodes.forEach((ep) => {
    (ep.events || []).forEach((ev) => {
      if (!ev.time) return;
      const style = EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" };
      allMarkers.push({ time: ev.time, position: "aboveBar", color: style.color, shape: style.shape, text: "" });
    });
  });
  allMarkers.sort((a, b) => a.time - b.time);

  const playCandles = globalCandles.filter((c) => c.time >= simStartTime).sort((a, b) => a.time - b.time);
  if (playCandles.length === 0) {
    candleSeries.setMarkers(allMarkers);
    return;
  }

  const TOTAL_MS = 3000;
  const STEP_MS = 20;
  const steps = Math.max(1, Math.floor(TOTAL_MS / STEP_MS));
  const candlesPerStep = Math.max(1, Math.ceil(playCandles.length / steps));

  let idx = 0;
  simPlaybackTimer = setInterval(() => {
    idx += candlesPerStep;
    const done = idx >= playCandles.length - 1;
    if (done) idx = playCandles.length - 1;

    const cursorTime = playCandles[idx].time;
    const revealed = allMarkers.filter((m) => m.time <= cursorTime);
    const cursorMarker = { time: cursorTime, position: "inBar", color: "#5aa9e6", shape: "circle", text: "" };
    candleSeries.setMarkers([...revealed, cursorMarker]);

    if (done) {
      stopSimPlayback();
      candleSeries.setMarkers(allMarkers); // финально — без курсора, все маркеры на местах
    }
  }, STEP_MS);
}

function clearLevelLines() {
  levelLines.forEach((series) => {
    try { chart.removeSeries(series); } catch(e) {}
  });
  levelLines = [];
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
    const [levelsRes, activeRes] = await Promise.all([
      fetch(`/api/levels/${encodeURIComponent(coin)}`),
      fetch(`/api/watchlist/active`).catch(() => null),
    ]);
    if (!levelsRes.ok) return; 
    const data = await levelsRes.json();

    let activeLevels = [];
    if (activeRes && activeRes.ok) {
      const activeData = await activeRes.json();
      activeLevels = (Array.isArray(activeData) ? activeData : [])
        .filter((w) => (w.coin || "").toUpperCase() === coin.toUpperCase());
    }
    const findActive = (lvl) => activeLevels.find(
      (w) => Math.abs((w.level_min ?? NaN) - lvl.min) < 1e-9 && Math.abs((w.level_max ?? NaN) - lvl.max) < 1e-9
    );

    const firstTime = globalCandles[0].time;

    const addLevel = (lvl, color, isSupport) => {
      const active = findActive(lvl);
      const midPrice = (lvl.min + lvl.max) / 2;
      const levelLabel = friendlyLevelType(lvl.type);
      
      const series = chart.addLineSeries({
        color: color,
        lineWidth: active ? 3 : 1,
        lineStyle: LightweightCharts.LineStyle.Solid,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        title: active ? `${isSupport ? '🟢' : '🔴'} ${friendlyStrategy(active.strategy)} · ${levelLabel}` : `${levelLabel} ${lvl.score ?? ""}`
      });
      
      // Парсим дату. Если ее нет или она кривая — берем начало графика
      let startTime = lvl.date ? new Date(lvl.date).getTime() / 1000 : firstTime;
      
      // Строим линию ИСКЛЮЧИТЕЛЬНО по реальным меткам времени свечей, иначе библиотека ломается
      const lineData = globalCandles
        .filter(c => c.time >= startTime)
        .map(c => ({ time: c.time, value: midPrice }));
        
      if (lineData.length > 0) {
        series.setData(lineData);
        levelLines.push(series);
      }
    };

    (data.supports || []).forEach((lvl) => addLevel(lvl, "#4caf7d", true));
    (data.resistances || []).forEach((lvl) => addLevel(lvl, "#e5654f", false));
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
  GOOD_GREEN:  { color: "#4dd0e1", shape: "circle" },   
  TRACK_START: { color: "#8a8f98", shape: "circle" },   
  NEW_PEAK:    { color: "#f2c14e", shape: "circle" },   
  GOOD_RED:    { color: "#ff8a65", shape: "circle" },   
  ENTRY:       { color: "#4caf7d", shape: "arrowUp" },  
  CANCEL:      { color: "#5c6370", shape: "square" },   
  DEAD:        { color: "#e5654f", shape: "square" },   
};

async function loadEvents(coin) {
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) { candleSeries.setMarkers([]); return; }
    const data = await res.json();

    const markers = [];
    const pushWatcherEvents = (w, isHistory) => {
      (w.events || []).forEach((ev) => {
        if (!ev.time) return; 
        const style = EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" };
        markers.push({
          time: ev.time,
          position: "aboveBar",
          color: style.color,
          shape: style.shape,
          text: "",
        });
      });
    };

    (data.active || []).forEach((w) => pushWatcherEvents(w, false));
    (data.history || []).forEach((w) => pushWatcherEvents(w, true));

    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  } catch (e) {
    console.error("events load failed", e);   
  }
}

async function loadChart(coin) {
  // --- ЗАЩИТА ОТ 404 ОШИБКИ И ПУСТЫХ КЛИКОВ ---
  if (!coin || coin === "null" || coin === "undefined") return;
  // ---------------------------------------------
  
  selectedCoin = coin;
  currentCoinEl.textContent = coin;
  currentSymbolEl.textContent = "загрузка графика...";
  highlightSelection();

  // При смене монеты старая точка старта симуляции теряет смысл
  simStartTime = null;
  stopSimPlayback();
  clearSimLevelLines();
  updateSimUI();
  
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
    loadLevels(coin);
    loadEvents(coin);
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
  const [res, activeRes] = await Promise.all([
    fetch("/api/watchlist"),
    fetch("/api/watchlist/active")
  ]);
  const data = await res.json();
  const activeData = await activeRes.json();
  
  const activeCoins = activeData.map(w => (w.coin || "").toUpperCase());
  const withLevels = (data.with_levels || []).filter(c => !activeCoins.includes((c.coin || "").toUpperCase()));
  const withoutLevels = (data.without_levels || []).filter(c => !activeCoins.includes((c.coin || "").toUpperCase()));
  
  const total = withLevels.length + withoutLevels.length;
  watchlistCountEl.textContent = total;

  if (total === 0) {
    watchlistListEl.innerHTML = "<div class='muted'>все монеты в работе (или пусто)</div>";
    return;
  }

  const renderGroup = (entries) =>
    entries
      .slice()
      .sort((a, b) => a.coin.localeCompare(b.coin))
      .map(
        (info) => `
        <div class="list-item" data-coin="${info.coin}">
          <span>${info.coin}</span><span class="dir-${info.direction}">${info.direction || ""}</span>
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

// === ПАНЕЛЬ "СИМУЛЯЦИЯ" ===
// Пока просто список монет (те же, что прошли фильтр по обороту в
// macro_levels.json) + клик открывает график. Без расчётов — это делаем
// следующим шагом.
async function loadSimCoins() {
  try {
    const res = await fetch("/api/macro/coins");
    const data = await res.json();
    const coins = data.coins || [];
    simCountEl.textContent = coins.length;

    if (coins.length === 0) {
      simListEl.innerHTML = "<div class='muted'>нет монет с уровнями</div>";
      return;
    }

    simListEl.innerHTML = coins
      .map((coin) => `<div class="list-item" data-coin="${coin}"><span>${coin}</span></div>`)
      .join("");

    simListEl.querySelectorAll(".list-item").forEach((div) => {
      div.onclick = () => loadChart(div.dataset.coin);
    });
  } catch (e) {
    console.error("sim coins load failed", e);
    simListEl.innerHTML = "<div class='muted'>ошибка загрузки</div>";
  }
}

async function loadActiveWatchers() {
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
    div.innerHTML = `
      <span style="flex-grow: 1;">${w.coin ?? "?"} <span class="dir-${w.direction}">${w.direction ?? ""}</span> <span class="muted">${friendlyStrategy(w.strategy)}</span></span>
      <span class="state-tag">${w.state}</span>
      <button title="Пересчитать структуру" onclick="triggerRescan(event, '${w.coin}')" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 14px; margin-left: 8px;">🔄</button>
    `;
    div.title = w.history_log || "";
    div.onclick = () => loadChart(w.coin);
    activeListEl.appendChild(div);
  });
}

async function loadSignals() {
  const res = await fetch("/api/signals?limit=50");
  const data = await res.json();

  if (data.length === 0) {
    signalsBody.innerHTML = "<tr><td colspan='9'>нет сигналов</td></tr>";
    return;
  }

  signalsBody.innerHTML = "";
  data.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.date ?? ""}</td>
      <td>${s.coin ?? ""}</td>
      <td class="dir-${s.type}">${s.type ?? ""}</td>
      <td>${s.source ?? ""}</td>
      <td>${s.entry ?? ""}</td>
      <td>${s.target ?? ""}</td>
      <td>${s.stop ?? ""}</td>
      <td>${s.status ?? ""}</td>
      <td>${s.result_percent ?? ""}</td>
    `;
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
  await Promise.all([loadWatchlist(), loadActiveWatchers(), loadSignals(), loadGlobalHistory(), loadSimCoins()]);
}

initChart();
refreshAll();

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

// === ФУНКЦИЯ РУЧНОГО РЕСКАНА ===
window.triggerRescan = async function(event, coin) {
  event.stopPropagation(); 
  try {
      const res = await fetch(`/api/rescan/${encodeURIComponent(coin)}`, { method: "POST" });
      if (res.ok) {
          console.log(`[RESCAN] Заявка отправлена для ${coin}. Ждем тик сканера...`);
          setTimeout(() => { if (selectedCoin === coin) loadChart(coin); }, 2000);
      }
  } catch (e) {
      console.error("Ошибка рескана", e);
  }
};