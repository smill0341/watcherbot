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
let levelLines = []; // храним price line объекты, чтобы удалять при смене монеты
let selectedCoin = null;
let currentPrecision = 4;

const ema20ValueEl = document.getElementById("ema20-value");
const ema50ValueEl = document.getElementById("ema50-value");

// На каком таймфрейме сколько свечей просить у /api/ohlcv (теперь это чтение
// из локальной SQLite-истории на backend, а не поход на биржу за раз — можно
// смело просить глубину вплоть до того, что реально накоплено, ~2 месяца):
const TF_LIMITS = {
  "15m": 5760,  // ~60 дней
  "1h": 1440,   // ~60 дней
  "4h": 360,    // ~60 дней
  "1d": 60,     // ~60 дней
};
let currentTimeframe = "15m";

// index.html пока без готовой разметки под OHLCV-легенду и блок истории —
// создаём и вставляем их из JS в существующие контейнеры (.chart-legend, .sidebar),
// вместо того чтобы требовать правки HTML.
function formatVolume(v) {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (abs >= 1_000) return (v / 1_000).toFixed(1) + "k";
  return String(Math.round(v));
}

let ohlcvLegendTextEl = null;
function ensureOhlcvLegend() {
  if (ohlcvLegendTextEl) return ohlcvLegendTextEl;
  const container = document.querySelector(".chart-legend");
  if (!container) return null;
  const item = document.createElement("div");
  item.className = "legend-item ohlcv-legend-item";
  item.innerHTML = `<span id="ohlcv-legend-text" class="muted">—</span>`;
  container.appendChild(item);
  ohlcvLegendTextEl = document.getElementById("ohlcv-legend-text");
  return ohlcvLegendTextEl;
}

function setOhlcvLegend(candle, vol) {
  const el = ensureOhlcvLegend();
  if (!el) return;
  if (!candle) {
    el.textContent = "—";
    return;
  }
  const p = currentPrecision;
  const o = candle.open.toFixed(p);
  const h = candle.high.toFixed(p);
  const l = candle.low.toFixed(p);
  const c = candle.close.toFixed(p);
  const volTxt = vol ? formatVolume(vol.value) : "—";
  el.textContent = `O ${o}  H ${h}  L ${l}  C ${c}  Vol ${volTxt}`;
}

let historyPanelBodyEl = null;
function ensureHistoryPanel() {
  if (historyPanelBodyEl) return historyPanelBodyEl;
  const sidebar = document.querySelector(".sidebar");
  if (!sidebar) return null;
  const panel = document.createElement("div");
  panel.className = "panel";
  panel.innerHTML = `
    <h2>Последний скан</h2>
    <div id="history-panel-body" class="history-panel-body">
      <div class="history-empty">Выбери монету слева</div>
    </div>
  `;
  sidebar.appendChild(panel);
  historyPanelBodyEl = document.getElementById("history-panel-body");
  return historyPanelBodyEl;
}

function renderHistoryPanel(historyArr) {
  const el = ensureHistoryPanel();
  if (!el) return;
  if (!historyArr || historyArr.length === 0) {
    el.innerHTML = `<div class="history-empty">Пока нет завершённых сканов по этой монете.</div>`;
    return;
  }
  el.innerHTML = historyArr.map((w) => {
    const state = w.final_state || "?";
    const stateClass = state === "TRIGGERED" ? "history-state-TRIGGERED" : "history-state-DEAD";
    const diedAt = w.died_at ? new Date(w.died_at).toLocaleString() : "—";
    const dir = w.direction ? `<span class="dir-${w.direction}">${w.direction}</span>` : "";
    return `
      <div class="history-card">
        <div class="history-card-head">
          <span>${w.strategy || "?"} ${dir}</span>
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
    grid: {
      vertLines: { color: "#1b1d24" },
      horzLines: { color: "#1b1d24" },
    },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      // Данные приходят в UTC (unix-секунды), lightweight-charts по
      // умолчанию рисует шкалу тоже в UTC. Переводим подписи на Киев
      // (UTC+3) явным форматтером, сами данные не трогаем.
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

  // Объём — отдельная шкала цен, прижатая к низу того же графика (не отдельный chart)
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
  });
  chart.priceScale("volume").applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 }, // занимает нижние ~18% высоты
  });

  // crosshairMarkerVisible: false — убирает кружок-маркер на самой линии EMA
  // в точке под курсором. Без этого визуально кажется, что курсор "прилип"
  // к EMA, а не к свече — крестик при этом всё равно обычный, просто без
  // лишней точки поверх линии. Цифры EMA в легенде под курсором не зависят
  // от этой настройки и продолжают обновляться как обычно.
  ema20Series = chart.addLineSeries({ color: "#f2c14e", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });
  ema50Series = chart.addLineSeries({ color: "#5aa9e6", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });

  // Живые значения EMA под курсором вместо непонятных плавающих кружков без подписи
  chart.subscribeCrosshairMove((param) => {
    if (!param || !param.time) {
      ema20ValueEl.textContent = "—";
      ema50ValueEl.textContent = "—";
      setOhlcvLegend(null, null);
      return;
    }
    const e20 = param.seriesData.get(ema20Series);
    const e50 = param.seriesData.get(ema50Series);
    ema20ValueEl.textContent = e20 ? e20.value.toFixed(currentPrecision) : "—";
    ema50ValueEl.textContent = e50 ? e50.value.toFixed(currentPrecision) : "—";

    const candle = param.seriesData.get(candleSeries);
    const vol = param.seriesData.get(volumeSeries);
    setOhlcvLegend(candle, vol);
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
}

// Простая EMA по массиву свечей lightweight-charts формата {time, close, ...}
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

function clearLevelLines() {
  levelLines.forEach((line) => candleSeries.removePriceLine(line));
  levelLines = [];
}

async function loadLevels(coin) {
  clearLevelLines();
  try {
    const [levelsRes, activeRes] = await Promise.all([
      fetch(`/api/levels/${encodeURIComponent(coin)}`),
      fetch(`/api/watchlist/active`).catch(() => null),
    ]);
    if (!levelsRes.ok) return; // нет уровней для монеты — молча пропускаем
    const data = await levelsRes.json();

    // Находим уровни, которые ПРЯМО СЕЙЧАС отслеживает живой вотчер этой
    // монеты — чтобы выделить их жирной линией и подписью со стратегией,
    // а не просто рисовать все supports/resistances монеты одинаково.
    let activeLevels = [];
    if (activeRes && activeRes.ok) {
      const activeData = await activeRes.json();
      activeLevels = (Array.isArray(activeData) ? activeData : [])
        .filter((w) => (w.coin || "").toUpperCase() === coin.toUpperCase());
    }
    const findActive = (lvl) => activeLevels.find(
      (w) => Math.abs((w.level_min ?? NaN) - lvl.min) < 1e-9 && Math.abs((w.level_max ?? NaN) - lvl.max) < 1e-9
    );

    (data.supports || []).forEach((lvl) => {
      const active = findActive(lvl);
      levelLines.push(candleSeries.createPriceLine({
        price: (lvl.min + lvl.max) / 2,
        color: "#4caf7d",
        lineWidth: active ? 3 : 1,
        lineStyle: active ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: active ? `🟢 АКТИВНО (${active.strategy}) sup ${lvl.score ?? ""}` : `sup ${lvl.score ?? ""}`,
      }));
    });
    (data.resistances || []).forEach((lvl) => {
      const active = findActive(lvl);
      levelLines.push(candleSeries.createPriceLine({
        price: (lvl.min + lvl.max) / 2,
        color: "#e5654f",
        lineWidth: active ? 3 : 1,
        lineStyle: active ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: active ? `🔴 АКТИВНО (${active.strategy}) res ${lvl.score ?? ""}` : `res ${lvl.score ?? ""}`,
      }));
    });
  } catch (e) {
    console.error("levels load failed", e);
  }
}

// Цвет/форма маркера по типу события вотчера (см. _record_event в
// v_bottom_watcher.py / v_green_bottom_watcher.py / v_red_top_watcher.py
// на бэкенде бота).
const EVENT_MARKER_STYLE = {
  ORIENTIR:    { color: "#8a8f98", shape: "circle" },   // V_BOTTOM: нашли ориентир
  START:       { color: "#5aa9e6", shape: "circle" },   // V_BOTTOM: старт
  PEAK:        { color: "#f2c14e", shape: "circle" },   // V_BOTTOM: пик/пик+
  PIT:         { color: "#e5654f", shape: "circle" },   // V_GREEN_BOTTOM: новая яма
  SCAN:        { color: "#f2c14e", shape: "circle" },   // V_GREEN_BOTTOM/V_RED_TOP: активный поиск входа
  GOOD_GREEN:  { color: "#4dd0e1", shape: "circle" },   // V_GREEN_BOTTOM: кандидат на вход
  TRACK_START: { color: "#8a8f98", shape: "circle" },   // V_RED_TOP: первый пик, старт слежения
  NEW_PEAK:    { color: "#f2c14e", shape: "circle" },   // V_RED_TOP: новый структурный пик ("три индейца")
  GOOD_RED:    { color: "#ff8a65", shape: "circle" },   // V_RED_TOP: С3 подтвердила, кандидат на вход
  ENTRY:       { color: "#4caf7d", shape: "arrowUp" },  // вход состоялся
  CANCEL:      { color: "#5c6370", shape: "square" },   // цепочка сорвана (буфер/срыв)
  DEAD:        { color: "#e5654f", shape: "square" },   // калькулятор отклонил сделку
};

async function loadEvents(coin) {
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) { candleSeries.setMarkers([]); return; }
    const data = await res.json();

    const markers = [];
    const pushWatcherEvents = (w, isHistory) => {
      (w.events || []).forEach((ev) => {
        if (!ev.time) return; // событие записано до конвертации времени на бэкенде — пропускаем
        const style = EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" };
        markers.push({
          time: ev.time,
          position: "aboveBar",
          color: style.color,
          shape: style.shape,
          // Подписи над точками убраны — тип события и так виден по
          // цвету/форме (см. EVENT_MARKER_STYLE), текст только шумел.
          text: "",
        });
      });
    };

    (data.active || []).forEach((w) => pushWatcherEvents(w, false));
    (data.history || []).forEach((w) => pushWatcherEvents(w, true));

    // lightweight-charts требует маркеры отсортированными по времени
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);

    renderHistoryPanel(data.history);
  } catch (e) {
    console.error("events load failed", e);
    renderHistoryPanel([]);
  }
}

async function loadChart(coin) {
  selectedCoin = coin;
  currentCoinEl.textContent = coin;
  currentSymbolEl.textContent = "загрузка графика...";
  highlightSelection();

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
      clearLevelLines();
      return;
    }
    const data = await res.json();
    currentSymbolEl.textContent = `${data.symbol} · ${data.timeframe}`;
    currentPrecision = data.price_precision ?? 4;

    const priceFormat = {
      type: "price",
      precision: currentPrecision,
      minMove: 1 / Math.pow(10, currentPrecision),
    };
    candleSeries.applyOptions({ priceFormat });
    ema20Series.applyOptions({ priceFormat });
    ema50Series.applyOptions({ priceFormat });

    // 1. Форматируем свечи: переводим время из миллисекунд в секунды
    const formattedCandles = data.candles.map(c => {
      const unixSeconds = c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time;
      return {
        ...c,
        time: unixSeconds,
      };
    });

    // 2. Строго сортируем по времени (исправляет баг "отображает криво частями")
    formattedCandles.sort((a, b) => a.time - b.time);

    // 3. Передаем уже правильные данные в график
    candleSeries.setData(formattedCandles);
    
    volumeSeries.setData(formattedCandles.map((c) => ({
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    
    ema20Series.setData(computeEMA(formattedCandles, 20));
    ema50Series.setData(computeEMA(formattedCandles, 50));

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
  const res = await fetch("/api/watchlist");
  const data = await res.json();
  const withLevels = data.with_levels || [];
  const withoutLevels = data.without_levels || [];
  const total = withLevels.length + withoutLevels.length;
  watchlistCountEl.textContent = total;

  if (total === 0) {
    watchlistListEl.innerHTML = "<div class='muted'>пусто</div>";
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

  // Две группы: сверху монеты, по которым уже есть построенные уровни
  // (macro_levels.json), снизу — которые ещё в очереди. Список пересчитывается
  // при каждой загрузке, так что деление обновляется само по себе.
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
      <span>${w.coin ?? "?"} <span class="dir-${w.direction}">${w.direction ?? ""}</span></span>
      <span class="state-tag">${w.state}</span>
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

async function refreshAll() {
  await Promise.all([loadWatchlist(), loadActiveWatchers(), loadSignals()]);
}

initChart();
refreshAll();
// Обновляем списки каждые 60 секунд, график руками (по клику) — без агрессивного polling'а
setInterval(refreshAll, 60000);