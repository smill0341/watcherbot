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

// На каком таймфрейме показывать сколько свечей одним запросом (без пагинации,
// один вызов к бирже отдаёт максимум ~1000 свечей):
const TF_LIMITS = {
  "15m": 200,  // ~2 суток — как реально сканит бот
  "1h": 720,   // ~30 дней
  "4h": 500,   // ~83 дня
  "1d": 365,   // ~1 год
};
let currentTimeframe = "15m";

function initChart() {
  const box = document.getElementById("chart");
  chart = LightweightCharts.createChart(box, {
    layout: { background: { color: "#0e0f13" }, textColor: "#cfd3da" },
    grid: {
      vertLines: { color: "#1b1d24" },
      horzLines: { color: "#1b1d24" },
    },
    timeScale: { timeVisible: true, secondsVisible: false },
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

  ema20Series = chart.addLineSeries({ color: "#f2c14e", lineWidth: 1, priceLineVisible: false });
  ema50Series = chart.addLineSeries({ color: "#5aa9e6", lineWidth: 1, priceLineVisible: false });

  // Живые значения EMA под курсором вместо непонятных плавающих кружков без подписи
  chart.subscribeCrosshairMove((param) => {
    if (!param || !param.time) {
      ema20ValueEl.textContent = "—";
      ema50ValueEl.textContent = "—";
      return;
    }
    const e20 = param.seriesData.get(ema20Series);
    const e50 = param.seriesData.get(ema50Series);
    ema20ValueEl.textContent = e20 ? e20.value.toFixed(currentPrecision) : "—";
    ema50ValueEl.textContent = e50 ? e50.value.toFixed(currentPrecision) : "—";
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
    const res = await fetch(`/api/levels/${encodeURIComponent(coin)}`);
    if (!res.ok) return; // нет уровней для монеты — молча пропускаем
    const data = await res.json();

    (data.supports || []).forEach((lvl) => {
      levelLines.push(candleSeries.createPriceLine({
        price: (lvl.min + lvl.max) / 2,
        color: "#4caf7d",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: `sup ${lvl.score ?? ""}`,
      }));
    });
    (data.resistances || []).forEach((lvl) => {
      levelLines.push(candleSeries.createPriceLine({
        price: (lvl.min + lvl.max) / 2,
        color: "#e5654f",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: `res ${lvl.score ?? ""}`,
      }));
    });
  } catch (e) {
    console.error("levels load failed", e);
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

    candleSeries.setData(data.candles);
    volumeSeries.setData(data.candles.map((c) => ({
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    ema20Series.setData(computeEMA(data.candles, 20));
    ema50Series.setData(computeEMA(data.candles, 50));

    chart.timeScale().fitContent();
    loadLevels(coin);
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
  const entries = Object.entries(data);
  watchlistCountEl.textContent = entries.length;

  if (entries.length === 0) {
    watchlistListEl.innerHTML = "<div class='muted'>пусто</div>";
    return;
  }

  watchlistListEl.innerHTML = "";
  entries
    .sort((a, b) => a[0].localeCompare(b[0]))
    .forEach(([coin, info]) => {
      const div = document.createElement("div");
      div.className = "list-item";
      div.dataset.coin = coin;
      div.innerHTML = `<span>${coin}</span><span class="dir-${info.direction}">${info.direction}</span>`;
      div.onclick = () => loadChart(coin);
      watchlistListEl.appendChild(div);
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