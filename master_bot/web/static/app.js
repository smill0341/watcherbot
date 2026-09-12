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
let emaMacroSeries = null; 
let rsiSeries = null; 
let levelLines = []; 
let selectedCoin = null;
let focusedLevel = null;
let isSignalView = false;

const STRATEGY_COLORS = {
  V_BOTTOM: "#4caf7d",
  V_GREEN_BOTTOM: "#26a69a",
  V_RED_TOP: "#ff9800",
  BOUNCE: "#29b6f6",
};

function friendlyStrategyWithMode(strategy, mode) {
  const base = friendlyStrategy(strategy);
  if (strategy !== "BOUNCE" || !mode) return base;
  if (mode === "CLIMAX") return "Bounce C";
  if (mode === "MIRROR") return "Bounce M";
  return base;
}
let currentPrecision = 4;
let globalCandles = []; 

const ema20ValueEl = document.getElementById("ema20-value");
const ema50ValueEl = document.getElementById("ema50-value");
const ema200ValueEl = document.getElementById("ema200-value");

const TF_LIMITS = {
  "15m": 5760, // ~60 дней
  "1h": 4320,  // ~180 дней
  "4h": 1080,  // ~180 дней
  "1d": 365,   // ~1 год (как в базе)
  "1w": 105,   // ~2 года
  "1M": 36,    // ~3 года
};
let currentTimeframe = "15m";

// Фильтр-выравниватель: жестко сажает время любой свечи на сетку, чтобы хвост графика не отрывался
function alignTime(t, tf) {
  const d = new Date(t * 1000);
  if (tf === "15m") {
    d.setUTCMinutes(Math.floor(d.getUTCMinutes() / 15) * 15, 0, 0);
  } else if (tf === "1h") {
    d.setUTCMinutes(0, 0, 0);
  } else if (tf === "4h") {
    d.setUTCHours(Math.floor(d.getUTCHours() / 4) * 4, 0, 0, 0);
  } else if (tf === "1d") {
    d.setUTCHours(0, 0, 0, 0);
  } else if (tf === "1w") {
    const day = d.getUTCDay();
    const diff = d.getUTCDate() - day + (day === 0 ? -6 : 1);
    d.setUTCDate(diff);
    d.setUTCHours(0, 0, 0, 0);
  } else if (tf === "1M") {
    d.setUTCDate(1);
    d.setUTCHours(0, 0, 0, 0);
  }
  return Math.floor(d.getTime() / 1000);
}

function formatVolume(v) {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (abs >= 1_000) return (v / 1_000).toFixed(1) + "k";
  return String(Math.round(v));
}

function formatDuration(fromUnixSec, toDate) {
  if (fromUnixSec === null || fromUnixSec === undefined) return "—";
  const fromMs = fromUnixSec * 1000;
  const toMs = toDate.getTime();
  const totalMin = Math.max(0, Math.floor((toMs - fromMs) / 60000));
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return `${days}д ${hours}ч`;
  if (hours > 0) return `${hours}ч ${mins}м`;
  return `${mins}м`;
}

let ohlcvLegendTextEl = null;
let bottomMarksLegendCreated = false;

function ensureOhlcvLegend() {
  if (!bottomMarksLegendCreated) {
    const chartBox = document.getElementById("chart");
    const marksLegend = document.createElement("div");
    marksLegend.style.cssText = "padding: 10px 15px; font-size:12px; color:#555b66; background: #ffffff; border-top: 1px solid #e6e8eb; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;";
    const bar = (color) => `<span style="display:inline-block;width:14px;height:3px;background:${color};margin-right:4px;vertical-align:middle;border-radius:1px;"></span>`;
    marksLegend.innerHTML = `
      <span>
        <b>Маркеры:</b>
        <span style="color:#8a8f98">⚪️ Старт</span> |
        <span style="color:#f2c14e">🟡 Кандидат</span> |
        <span style="color:#5aa9e6">🔵 Структура</span> |
        <span style="color:#9c27b0">🟣 Старт поиска</span> |
        <span style="color:#4caf7d">🟢 Вход</span> |
        <span style="color:#e5654f">🔴 Сброс/Стоп</span>
      </span>
      <span>
        ${bar("#5aa9e6")}Entry &nbsp;
        ${bar("#4caf7d")}Target &nbsp;
        ${bar("#e5654f")}Stop &nbsp;
        ${bar("#00c853")}Уровень (LONG) &nbsp;
        ${bar("#ff3d3d")}Уровень (SHORT)
      </span>
    `;
    chartBox.parentElement.insertBefore(marksLegend, chartBox.nextSibling);
    bottomMarksLegendCreated = true;
  }

  if (ohlcvLegendTextEl) return ohlcvLegendTextEl;
  const container = document.querySelector(".chart-legend");
  if (!container) return null;
  
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
      uniformDistribution: true,
      tickMarkFormatter: (time) => {
        const d = new Date(time * 1000);
        // Выводит нормальную дату на старших графиках, убирает "02:00" на шкале месяцев/недель
        if (currentTimeframe === "1d" || currentTimeframe === "1w" || currentTimeframe === "1M") {
          return d.toLocaleDateString("ru-RU", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", year: "2-digit" });
        }
        return d.toLocaleTimeString("ru-RU", { timeZone: "Europe/Kyiv", hour: "2-digit", minute: "2-digit" });
      },
    },
    localization: {
      timeFormatter: (time) => {
        const dateStr = new Date(time * 1000).toLocaleString("ru-RU", {
          timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", year: "2-digit",
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

let signalLines = []; 
function clearSignalLines() {
  signalLines.forEach((series) => {
    try { chart.removeSeries(series); } catch(e) {}
  });
  signalLines = [];
}

function drawSignalTradeLines(signal) {
  clearSignalLines();
  if (!globalCandles.length) return;
  const lineTimes = globalCandles.map((c) => c.time);
  const addLine = (price, color) => {
    if (price === null || price === undefined || Number.isNaN(Number(price))) return;
    const series = chart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: "",
      autoscaleInfoProvider: () => null,
    });
    series.setData(lineTimes.map((t) => ({ time: t, value: Number(price) })));
    signalLines.push(series);
  };
  addLine(signal.entry, "#5aa9e6");
  addLine(signal.target, "#4caf7d");
  addLine(signal.stop, "#e5654f");
}

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

async function loadLevels(coin, token = chartLoadToken) {
  clearLevelLines();
  if (globalCandles.length === 0) return;

  try {
    const [levelsRes, eventsRes] = await Promise.all([
      fetch(`/api/levels/${encodeURIComponent(coin)}`),
      fetch(`/api/events/${encodeURIComponent(coin)}`).catch(() => null),
    ]);
    if (token !== chartLoadToken) return;
    if (!levelsRes.ok) return; 
    const data = await levelsRes.json();
    if (token !== chartLoadToken) return;

    let activeLevels = [];
    if (eventsRes && eventsRes.ok) {
      const eventsData = await eventsRes.json();
      if (token !== chartLoadToken) return;
      activeLevels = Array.isArray(eventsData.active) ? eventsData.active : [];
    }

    const firstTime = globalCandles[0].time;

    const drawOrphanActiveLevel = (w) => {
      const wMin = w.level_min ?? w.min;
      const wMax = w.level_max ?? w.max;
      const direction = w.direction ?? (w.mode ? "SHORT" : "LONG");
      const isSupport = direction === "LONG";
      const activeColor = isSupport ? "#00c853" : "#ff3d3d";
      const label = friendlyStrategyWithMode(w.strategy, w.mode);
      const scoreSuffix = w.level_score != null ? ` ${w.level_score}` : "";
      const levelTitle = `${isSupport ? '🟢' : '🔴'} ${label} · ${friendlyLevelType(w.level_type)}${scoreSuffix}`;
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
          title: idx === 0 ? levelTitle : "",
          autoscaleInfoProvider: () => null,
        });
        series.setData(lineTimes.map(t => ({ time: t, value: priceValue })));
        levelLines.push(series);
      });
    };

    if (focusedLevel) {
      if (focusedLevel.fromHistory) {
        drawOrphanActiveLevel(focusedLevel);
        return;
      }
      const w = activeLevels.find((x) => x.level_id === focusedLevel.level_id);
      if (!w) return;
      drawOrphanActiveLevel(w);
      return;
    }

    if (isSignalView) return;

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
  CANCEL:      { color: "#5c6370", shape: "square" },   
  DEAD:        { color: "#e5654f", shape: "square" },   
  SWEEP_BOTTOM:       { color: "#9c27b0", shape: "circle" },
  RUNAWAY:            { color: "#e5654f", shape: "circle" },
  CLIMAX_NEAR_BREACH: { color: "#5aa9e6", shape: "circle" },
  CLIMAX_FAR_BREACH:  { color: "#5aa9e6", shape: "circle" },
  ZONE_TOUCH:         { color: "#5aa9e6", shape: "circle" },
};

async function loadEvents(coin, token = chartLoadToken) {
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (token !== chartLoadToken) return; 
    if (!res.ok) { candleSeries.setMarkers([]); return; }
    const data = await res.json();
    if (token !== chartLoadToken) return;

    const matchesFocus = (w) => !focusedLevel
      || (focusedLevel.level_id ? w.level_id === focusedLevel.level_id
          : (Math.abs((w.level_min ?? NaN) - focusedLevel.min) < 1e-9 && Math.abs((w.level_max ?? NaN) - focusedLevel.max) < 1e-9));

    const minCandleTime = globalCandles.length ? globalCandles[0].time : null;
    const maxCandleTime = globalCandles.length ? globalCandles[globalCandles.length - 1].time : null;
    const inRange = (t) => minCandleTime !== null && t >= minCandleTime && t <= maxCandleTime;

    const seen = new Set(); 
    const markers = [];

    const sourceWatchers = isSignalView && !focusedLevel
      ? []
      : (focusedLevel && focusedLevel.fromHistory)
        ? [focusedLevel]
        : (data.active || []);

    sourceWatchers.forEach((w) => {
      if (!matchesFocus(w)) return;
      const isShort = w.direction === "SHORT";
      const entryStyle = isShort ? { color: "#e5654f", shape: "arrowDown" } : { color: "#4caf7d", shape: "arrowUp" };
      (w.events || []).forEach((ev) => {
        if (!ev.time || !inRange(ev.time)) return;
        const key = `${ev.time}_${ev.type}`;
        if (seen.has(key)) return;
        seen.add(key);
        const style = ev.type === "ENTRY" ? entryStyle : (EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" });
        markers.push({
          time: ev.time,
          position: ev.type === "ENTRY" && isShort ? "belowBar" : "aboveBar",
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

async function loadMacroEma200(coin) {
  if (!emaMacroSeries) return;
  try {
    // Лимит строго 999, как и заложено в бэкенде
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=4h&limit=999`);
    if (!res.ok) { emaMacroSeries.setData([]); return; }
    const data = await res.json();
    const candles = (data.candles || [])
      .map((c) => {
        const unixSeconds = c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time;
        return { ...c, time: unixSeconds };
      })
      .sort((a, b) => a.time - b.time);
      
    if (coin !== selectedCoin) return;
    
    let emaData = computeEMA(candles, 200);
    
    // ФИЛЬТР ДЫР: на старших таймфреймах оставляем только те точки EMA, 
    // которые идеально ложатся на существующие бары, чтобы не создавать пустых слотов
    if (currentTimeframe === "1d" || currentTimeframe === "1w" || currentTimeframe === "1M") {
      const mainTimes = new Set(globalCandles.map(c => c.time));
      emaData = emaData.filter(d => mainTimes.has(d.time));
    }

    emaMacroSeries.setData(emaData);
  } catch (e) {
    console.error("macro EMA200(4H) load failed", e);
  }
}

let chartLoadToken = 0;

async function loadChart(coin, focus = null, signal = null, opts = {}) {
  const skipLiveOverlay = !!opts.skipLiveOverlay;
  if (!coin || coin === "null" || coin === "undefined") return;

  const myToken = ++chartLoadToken;
  focusedLevel = focus;
  isSignalView = !!signal;

  selectedCoin = coin;
  currentCoinEl.textContent = coin;
  currentSymbolEl.textContent = "загрузка графика...";
  highlightSelection();

  clearSignalLines();
  clearLevelLines();

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
    globalCandles = formattedCandles;

    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedCandles.map((c) => ({
      time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    ema20Series.setData(computeEMA(formattedCandles, 20));
    ema50Series.setData(computeEMA(formattedCandles, 50));
    rsiSeries.setData(computeRSI(formattedCandles, 14)); 

    chart.timeScale().fitContent();
    candleSeries.priceScale().applyOptions({ autoScale: true });
    if (!skipLiveOverlay) {
      loadLevels(coin, myToken);
      loadEvents(coin, myToken);
      loadMacroEma200(coin); 
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
  if (!watchlistListEl) return;
  const res = await fetch("/api/watchlist");
  const data = await res.json();

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

async function buildFocusFromLevelId(coin, levelId) {
  if (!levelId) return null;
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) return null;
    const data = await res.json();
    const histKey = `${coin}_BOUNCE_${levelId}`;
    let w = (data.history || []).find((x) => x.key === histKey);
    if (!w) {
      w = (data.active || []).find((x) => x.level_id === levelId);
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
    signalsBody.innerHTML = "<tr><td colspan='11'>нет сигналов</td></tr>";
    return;
  }

  signalsBody.innerHTML = "";
  data.forEach((s) => {
    const tr = document.createElement("tr");
    const pct = s.result_percent;
    const pctText = pct === null || pct === undefined ? "" : `${pct > 0 ? "+" : ""}${pct}%`;
    const durationText = s.status === "⏳"
      ? formatDuration(s.time, new Date())
      : (s.closed_at ? formatDuration(s.time, new Date(s.closed_at)) : "—");
    const methodText = s.method === "volume" && s.method_value != null
      ? formatVolume(s.method_value) + (s.method_mult != null ? " / x" + s.method_mult.toFixed(1) : "")
      : "—";
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
      <td>${durationText}</td>
      <td>${methodText}</td>
    `;
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
        }
      });
      
      loadEvents(selectedCoin);
    } catch (e) {
      console.error("Ошибка автообновления свечей:", e);
    }
  }
}, 60000);

const resetWatchersBtnEl = document.getElementById("reset-watchers-btn");
if (resetWatchersBtnEl) {
  resetWatchersBtnEl.onclick = async () => {
    if (!confirm("Точно сбросить ВСЕХ живых вотчеров? Уровни (macro_levels.json/watchlist.json) не тронутся — только состояние отслеживания. Отменить нельзя.")) return;
    resetWatchersBtnEl.disabled = true;
    try {
      const res = await fetch("/api/reset_watchers", { method: "POST" });
      const data = await res.json();
      alert(data.message || "Готово");
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
      loadWatchlist();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      rebuildLevelsBtnEl.disabled = false;
    }
  };
}

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
  if (since === null) return; 
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

const STRATEGY_TOGGLE_LABELS = {
  VB: "V_BOTTOM",
  VGB: "V_GREEN_BOTTOM",
  VRT: "V_RED_TOP",
  BOUNCE: "BOUNCE",
};

function buildStrategiesPanel() {
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
    const rect = toggle.getBoundingClientRect();
    panel.style.left = `${rect.left + window.scrollX}px`;
    panel.style.top = `${rect.bottom + window.scrollY}px`;
    panel.style.display = "block";
    refreshStrategiesPanel();
  };

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