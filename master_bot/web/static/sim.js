// web/static/sim.js
//
// Отдельный, полностью изолированный JS для страницы /simulator.
// Раньше эта страница подключала общий app.js — из-за этого на неё влиял
// боевой фон дашборда: общий refreshAll() и минутный setInterval с
// loadEvents(selectedCoin) (см. app.js) периодически перезаписывали
// маркеры на графике боевыми данными бота поверх результата симуляции.
// Отсюда ощущение "точки то появляются, то пропадают, надо обновлять
// страницу" — на самом деле фон просто рисовал боевое состояние поверх.
//
// Здесь НЕТ ни одного фонового таймера, который трогает график или
// маркеры. Единственный setInterval во всём файле — анимация плейбека
// после запуска симуляции, и она сама себя останавливает по завершении.
//
// Куски инициализации графика (initChart/loadChart/computeEMA) —
// СОЗНАТЕЛЬНО ПРОДУБЛИРОВАНЫ из app.js, а не вынесены в общий модуль,
// по решению из обсуждения с пользователем (проще дублировать, чем
// синхронизировать общий файл). Если меняешь отрисовку графика
// (свечи/EMA/объём) на боевом дашборде в app.js — продублируй то же
// изменение сюда вручную, иначе страницы разъедутся по виду графика.
//
// Логи симуляции пишутся бэкендом в свою собственную папку
// modules/cryptano/simulator/logs/ (см. bounce_simulate.py,
// log_dir_override=SIM_LOG_DIR) — НЕ в боевые bounce_logs/*.log. Это уже
// сделано правильно на бэкенде, здесь на фронте менять нечего.

const simListEl = document.getElementById("sim-list");
const simCountEl = document.getElementById("sim-count");
const currentCoinEl = document.getElementById("current-coin");
const currentSymbolEl = document.getElementById("current-symbol");
const ema20ValueEl = document.getElementById("ema20-value");
const ema50ValueEl = document.getElementById("ema50-value");
const emaMacroValueEl = document.getElementById("ema-macro-value");
const simStartInputEl = document.getElementById("sim-start-input");
const simEndInputEl = document.getElementById("sim-end-input");

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema20Series = null;
let ema50Series = null;
let emaMacroSeries = null;
let globalCandles = [];
let currentPrecision = 4;

let selectedCoin = null;
let simMode = true; // страница целиком и есть симулятор — переключать нечего
let simStartTime = null; // unix seconds — из поля "От" (или клика по свече)
let simEndTime = null;   // unix seconds — из поля "До", null = до конца графика
let simRunning = false;
let simLevelLines = [];  // линии зон уровней (2 линии на уровень: min+max)
let simPlaybackTimer = null;
let simHistory = [];  // data.history последнего прогона — для поиска эпизодов по клику на сделку
let simTrades = [];   // data.trades последнего прогона

const TF_LIMITS = { "15m": null, "1h": null, "4h": null, "1d": null, "1w": null, "1M": null };
let currentTimeframe = "15m";

// === ДУБЛИРОВАНО ИЗ app.js::formatPrice — синхронизировать вручную ===
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

// === ДУБЛИРОВАНО ИЗ app.js::formatVolume ===
function formatVolume(v) {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (abs >= 1_000) return (v / 1_000).toFixed(1) + "k";
  return String(Math.round(v));
}

// === ДУБЛИРОВАНО ИЗ app.js::formatDuration ===
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

// === ДУБЛИРОВАНО ИЗ app.js::friendlyStrategy/friendlyLevelType ===
const STRATEGY_LABELS = { V_BOTTOM: "V-Bottom", V_GREEN_BOTTOM: "V-Green", V_RED_TOP: "V-Red", BOUNCE: "Bounce" };
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

// === ДУБЛИРОВАНО ИЗ app.js::computeEMA ===
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

// === ДУБЛИРОВАНО ИЗ app.js::loadMacroEma200 ===
// Для 1d/1w/1M — честная EMA200 по родным свечам этого же таймфрейма.
// Для внутридневных (15m/1h/4h) — отдельно тянем 4h-историю и считаем
// EMA200 по ней (макро-тренд поверх мелкого графика).
async function loadMacroEma200(coin) {
  if (!emaMacroSeries) return;

  if (currentTimeframe === "1d" || currentTimeframe === "1w" || currentTimeframe === "1M") {
    if (coin !== selectedCoin) return;
    emaMacroSeries.setData(computeEMA(globalCandles, 200));
    return;
  }

  try {
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

    emaMacroSeries.setData(computeEMA(candles, 200));
  } catch (e) {
    console.error("macro EMA200(4H) load failed", e);
  }
}

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

// === ГРАФИК — упрощённая версия app.js::initChart: без EMA200(4H), без
// RSI, без боевой легенды снизу — на этой странице их никто не смотрит,
// это чисто симуляция цены/EMA20/EMA50. ===
function initChart() {
  const box = document.getElementById("chart");
  chart = LightweightCharts.createChart(box, {
    layout: { background: { color: "#ffffff" }, textColor: "#1b1d24" },
    grid: { vertLines: { color: "#e6e8eb" }, horzLines: { color: "#e6e8eb" } },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time) => {
        const d = new Date(time * 1000);
        if (["1d", "1w", "1M"].includes(currentTimeframe)) {
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
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  ema20Series = chart.addLineSeries({ color: "#f2c14e", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });
  ema50Series = chart.addLineSeries({ color: "#5aa9e6", lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false });
  emaMacroSeries = chart.addLineSeries({ color: "#7e57c2", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });

  chart.subscribeCrosshairMove((param) => {
    if (!param || !param.time) {
      if (ema20ValueEl) ema20ValueEl.textContent = "—";
      if (ema50ValueEl) ema50ValueEl.textContent = "—";
      if (emaMacroValueEl) emaMacroValueEl.textContent = "—";
      return;
    }
    const e20 = param.seriesData.get(ema20Series);
    const e50 = param.seriesData.get(ema50Series);
    const eMacro = param.seriesData.get(emaMacroSeries);
    if (ema20ValueEl) ema20ValueEl.textContent = e20 ? e20.value.toFixed(currentPrecision) : "—";
    if (ema50ValueEl) ema50ValueEl.textContent = e50 ? e50.value.toFixed(currentPrecision) : "—";
    if (emaMacroValueEl) emaMacroValueEl.textContent = eMacro ? eMacro.value.toFixed(currentPrecision) : "—";
  });

  // Точка старта симуляции — клик по свече (быстрый способ, синхронизирует
  // поле календаря "От"), либо просто вписать дату в само поле — работает
  // и так, и так.
  chart.subscribeClick((param) => {
    if (!param || !param.time) return;
    setSimStartTime(param.time);
  });

  new ResizeObserver(() => {
    chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
  }).observe(box);

  document.querySelectorAll(".tf-btn").forEach((btn) => {
    if (btn.dataset.tf === currentTimeframe) btn.classList.add("active");
    btn.onclick = () => {
      currentTimeframe = btn.dataset.tf;
      document.querySelectorAll(".tf-btn").forEach((b) => b.classList.toggle("active", b === btn));
      if (selectedCoin) loadChartForCoin(selectedCoin);
    };
  });

  const simCancelBtnEl = document.getElementById("sim-cancel-btn");
  if (simCancelBtnEl) {
    simCancelBtnEl.onclick = () => {
      simStartTime = null;
      simEndTime = null;
      if (simStartInputEl) simStartInputEl.value = "";
      if (simEndInputEl) simEndInputEl.value = "";
      stopSimPlayback();
      clearSimLevelLines();
      candleSeries.setMarkers([]);
      updateSimUI();
    };
  }
  const simRunBtnEl = document.getElementById("sim-run-btn");
  if (simRunBtnEl) simRunBtnEl.onclick = runSimulation;

  // Поле "От" — источник правды для simStartTime, когда его меняют руками
  // (не кликом по свече). Сразу тянет и рисует снимок уровней на эту дату.
  if (simStartInputEl) {
    simStartInputEl.onchange = () => {
      const sec = dateInputToUnixSec(simStartInputEl.value);
      if (sec != null) setSimStartTime(sec, { syncInput: false });
    };
  }
  // Поле "До" — просто конец периода теста. Пусто = до конца графика,
  // как и раньше (backend уже поддерживает end=None).
  if (simEndInputEl) {
    simEndInputEl.onchange = () => {
      simEndTime = dateInputToUnixSec(simEndInputEl.value);
    };
  }
}

// Поля календаря — только дата, без времени (type="date", не datetime-
// local). Строим Date из компонентов явно (не из "YYYY-MM-DD" строки
// напрямую) — JS парсит голую ISO-дату как UTC-полночь, а нам нужно
// местное календарное 00:00, как и весь остальной график (Europe/Kyiv).
function dateInputToUnixSec(value) {
  if (!value) return null;
  const [y, m, d] = value.split("-").map(Number);
  if (!y || !m || !d) return null;
  return Math.floor(new Date(y, m - 1, d, 0, 0, 0).getTime() / 1000);
}
function unixSecToDateInputValue(sec) {
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Диапазон текущего месяца (1-е число -> последний день) — дефолт для
// полей "От"/"До", чтобы не вводить даты руками каждый раз.
function currentMonthRangeSec() {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0);
  const end = new Date(now.getFullYear(), now.getMonth() + 1, 0, 0, 0, 0); // 0-й день след. месяца = последний день текущего
  return { startSec: Math.floor(start.getTime() / 1000), endSec: Math.floor(end.getTime() / 1000) };
}

// Единая точка входа для "поставили дату старта" — что кликом по свече,
// что автозаполнением месяца, что руками в календаре: округляет до
// полуночи того же дня, синхронизирует источники и сразу тянет снимок
// уровней на эту дату (см. drawSnapshotLevels).
//
// syncInput=false — не перезаписывать поле "От" значением, которое там и
// так уже стоит (вызывается из собственного onchange поля). Раньше тут
// стояла безусловная перезапись — в некоторых браузерах (особенно Chrome)
// input[type=date] сбрасывает/дёргает отображение, если его .value
// переприсвоить прямо ВНУТРИ обработчика его же события change, даже
// тем же самым значением. Для клика по свече/автозаполнения месяца
// (дата приходит СНАРУЖИ поля) синхронизация по-прежнему нужна.
function setSimStartTime(sec, { syncInput = true } = {}) {
  const d = new Date(sec * 1000);
  sec = Math.floor(new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0).getTime() / 1000);
  simStartTime = sec;
  if (simStartInputEl && syncInput) simStartInputEl.value = unixSecToDateInputValue(sec);
  updateSimUI();
  if (selectedCoin) drawSnapshotLevels(selectedCoin, sec);
}

// hex "#rrggbb" -> "rrggbbAA" (добавляет альфу для CSS-цвета заливки)
function hexWithAlpha(hex, alphaHex) {
  return hex.replace("#", "") + alphaHex;
}

// Рисует ОДНУ зону как закрашенную полосу между min и max (Baseline-серия
// даёт границу только СВЕРХУ, у base-значения снизу заливка просто
// растворяется в фоне) + отдельную сплошную линию по нижней границе, чтобы
// низ зоны тоже было видно чётко. Возвращает массив обеих серий — обе
// нужно почистить при следующей перерисовке (см. clearSimLevelLines).
function addZoneBand(zMin, zMax, color, candles, titleText) {
  const bandSeries = chart.addBaselineSeries({
    baseValue: { type: "price", price: zMin },
    topFillColor1: "#" + hexWithAlpha(color, "40"),
    topFillColor2: "#" + hexWithAlpha(color, "15"),
    topLineColor: color,
    bottomFillColor1: "rgba(0,0,0,0)",
    bottomFillColor2: "rgba(0,0,0,0)",
    bottomLineColor: "rgba(0,0,0,0)",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    title: titleText || "",
    autoscaleInfoProvider: () => null,
  });
  bandSeries.setData(candles.map((c) => ({ time: c.time, value: zMax })));

  const bottomLine = chart.addLineSeries({
    color,
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Solid,
    crosshairMarkerVisible: false,
    priceLineVisible: false,
    lastValueVisible: false,
    autoscaleInfoProvider: () => null,
  });
  bottomLine.setData(candles.map((c) => ({ time: c.time, value: zMin })));

  return [bandSeries, bottomLine];
}

// Рисует ВСЕ зоны (supports зелёным, resistances красным) из честного
// исторического снимка уровней на дату "От" (см. /api/levels_at/{coin} —
// levels_history.get_levels_snapshot(), те же снимки раз в ~12ч, что
// читает и рескан, и precalc_for_bot.py). Не текущий macro_levels.json —
// то, что бот реально видел в этот момент.
async function drawSnapshotLevels(coin, whenSec) {
  clearSimLevelLines();
  if (candleSeries) candleSeries.setMarkers([]);
  if (!globalCandles.length) return;
  try {
    const res = await fetch(`/api/levels_at/${encodeURIComponent(coin)}?when=${whenSec}`);
    if (!res.ok) return; // нет снимка на эту дату — молча ничего не рисуем
    const data = await res.json();
    const zones = [
      ...(data.supports || []).map((z) => ({ ...z, color: "#4caf7d" })),
      ...(data.resistances || []).map((z) => ({ ...z, color: "#e5654f" })),
    ];
    zones.forEach((z) => {
      if (z.min == null || z.max == null) return;
      // Не тянуть полосу на весь загруженный график — только с той даты,
      // когда уровень реально появился (z.date). Без этого даже POC (у
      // которого date вообще "сегодняшний скан", см. levels_builder.py)
      // рисовался бы задним числом на недели назад.
      const zoneStartSec = z.date ? dateInputToUnixSec(z.date) : null;
      let candlesForZone = globalCandles;
      if (zoneStartSec != null) {
        const sliced = globalCandles.filter((c) => c.time >= zoneStartSec);
        if (sliced.length) candlesForZone = sliced;
      }
      simLevelLines.push(...addZoneBand(z.min, z.max, z.color, candlesForZone, friendlyLevelType(z.type)));
    });
  } catch (e) {
    console.error("drawSnapshotLevels failed", e);
  }
}

function updateSimUI() {
  const status = document.getElementById("sim-status");
  const runBtn = document.getElementById("sim-run-btn");
  const cancelBtn = document.getElementById("sim-cancel-btn");
  if (!status || !runBtn || !cancelBtn) return;

  if (simStartTime) {
    const d = new Date(simStartTime * 1000);
    const startLabel = d.toLocaleDateString("ru-RU", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", year: "numeric" });
    const endLabel = simEndTime
      ? new Date(simEndTime * 1000).toLocaleDateString("ru-RU", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", year: "numeric" })
      : "конец графика";
    status.textContent = `Период: ${startLabel} → ${endLabel}`;
    runBtn.style.display = "";
    cancelBtn.style.display = "";
  } else {
    status.textContent = "Укажи дату «От» (или кликни свечу)";
    runBtn.style.display = "none";
    cancelBtn.style.display = "none";
  }
}

function highlightSelection() {
  document.querySelectorAll(".list-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.coin === selectedCoin);
  });
}

// === СПИСОК МОНЕТ СЛЕВА — грузится ОДИН РАЗ при открытии страницы, без
// таймера. Список меняется, когда бот пересчитывает уровни (раз в ~12ч),
// не поминутно — периодический опрос тут не даёт практической пользы,
// только лишний риск задеть что-то ещё. Если появилась новая монета —
// обновить страницу вручную. ===
async function loadSimCoins() {
  if (!simListEl) return;
  try {
    const res = await fetch("/api/macro/coins");
    const data = await res.json();
    const coins = data.coins || [];
    if (simCountEl) simCountEl.textContent = coins.length;

    if (coins.length === 0) {
      simListEl.innerHTML = "<div class='muted'>нет монет с уровнями</div>";
      return;
    }

    simListEl.innerHTML = coins
      .map((coin) => `<div class="list-item" data-coin="${coin}"><span>${coin}</span></div>`)
      .join("");

    simListEl.querySelectorAll(".list-item").forEach((div) => {
      div.onclick = () => loadChartForCoin(div.dataset.coin);
    });
  } catch (e) {
    console.error("sim coins load failed", e);
    simListEl.innerHTML = "<div class='muted'>ошибка загрузки</div>";
  }
}

// === Загрузка графика монеты — НИКОГДА не тянет боевые уровни/события/
// EMA200 (в отличие от app.js::loadChart) — на этой странице боевого
// оверлея не существует в принципе, тут нечего "не путать с результатом
// симуляции", потому что живых данных тут просто нет. ===
async function loadChartForCoin(coin) {
  if (!coin || coin === "null" || coin === "undefined") return;

  // Переключение таймфрейма (тот же tf-btn.onclick, что и смена монеты)
  // зовёт эту же функцию с ТЕМ ЖЕ coin — раньше это неотличимо трактовалось
  // как "новая монета", и уже выбранный период "От"/"До" сбрасывался на
  // дефолтный текущий месяц при каждом переключении 15m/1h/4h/1d/1w/1M.
  // Теперь сброс — только при реальной смене монеты.
  const isCoinChange = coin !== selectedCoin;
  selectedCoin = coin;
  if (currentCoinEl) currentCoinEl.textContent = coin;
  if (currentSymbolEl) currentSymbolEl.textContent = "загрузка графика...";
  highlightSelection();

  stopSimPlayback();
  clearSimLevelLines();
  if (candleSeries) candleSeries.setMarkers([]);
  if (isCoinChange) {
    // Смена монеты — старая точка старта и старый результат теряют смысл.
    simStartTime = null;
    simEndTime = null;
    if (simStartInputEl) simStartInputEl.value = "";
    if (simEndInputEl) simEndInputEl.value = "";
  }
  updateSimUI();

  const limit = TF_LIMITS[currentTimeframe]; // null (1w/1M) -> вся история из базы, без ограничения

  try {
    const limitParam = limit != null ? `&limit=${limit}` : "";
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}${limitParam}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (currentSymbolEl) currentSymbolEl.textContent = err.detail || "график недоступен";
      candleSeries.setData([]);
      candleSeries.setMarkers([]);
      volumeSeries.setData([]);
      ema20Series.setData([]);
      ema50Series.setData([]);
      if (emaMacroSeries) emaMacroSeries.setData([]);
      return;
    }
    const data = await res.json();
    if (currentSymbolEl) currentSymbolEl.textContent = `${data.symbol} · ${data.timeframe}`;
    currentPrecision = data.price_precision ?? 4;

    const priceFormat = { type: "price", precision: currentPrecision, minMove: 1 / Math.pow(10, currentPrecision) };
    candleSeries.applyOptions({ priceFormat });
    ema20Series.applyOptions({ priceFormat });
    ema50Series.applyOptions({ priceFormat });
    if (emaMacroSeries) emaMacroSeries.applyOptions({ priceFormat });

    const formattedCandles = data.candles
      .map((c) => ({ ...c, time: c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time }))
      .sort((a, b) => a.time - b.time);
    globalCandles = formattedCandles;

    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedCandles.map((c) => ({
      time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    ema20Series.setData(computeEMA(formattedCandles, 20));
    ema50Series.setData(computeEMA(formattedCandles, 50));
    loadMacroEma200(coin);

    chart.timeScale().fitContent();
    candleSeries.priceScale().applyOptions({ autoScale: true });

    if (isCoinChange) {
      // Монету сменили, пока режим симуляции уже был включён — сразу
      // заполняем текущий месяц заново (как при первом включении режима).
      if (simMode) {
        const { startSec, endSec } = currentMonthRangeSec();
        simEndTime = endSec;
        if (simEndInputEl) simEndInputEl.value = unixSecToDateInputValue(endSec);
        setSimStartTime(startSec);
      }
    } else if (simMode && simStartTime != null) {
      // Та же монета, просто другой таймфрейм — период уже выбран,
      // просто перерисовываем полосы на новом наборе свечей.
      drawSnapshotLevels(coin, simStartTime);
    }
  } catch (e) {
    if (currentSymbolEl) currentSymbolEl.textContent = "ошибка загрузки графика";
    console.error(e);
  }
}

// Подтягивает окно свечей, реально покрывающее диапазон [fromSec, toSec] —
// нужно перед показом результата: маркеры/линии ставятся поверх УЖЕ
// загруженных на график свечей, а обычная загрузка держит только
// последние пару дней. /api/ohlcv поддерживает "around" (окно вокруг
// точки), не произвольный диапазон — берём середину периода, не больше
// 6000 свечей (потолок бэкенда, см. app.py::get_ohlcv).
async function loadCandlesAroundRange(coin, fromSec, toSec) {
  const spanCandles = Math.ceil((toSec - fromSec) / 900) + 20;
  const limit = Math.min(6000, Math.max(200, spanCandles));
  const aroundSec = Math.floor((fromSec + toSec) / 2);
  try {
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}&limit=${limit}&around=${aroundSec}`);
    if (!res.ok) return;
    const data = await res.json();
    const formattedCandles = (data.candles || [])
      .map((c) => ({ ...c, time: c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time }))
      .sort((a, b) => a.time - b.time);
    if (!formattedCandles.length) return;
    globalCandles = formattedCandles;
    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedCandles.map((c) => ({
      time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(76,175,125,0.5)" : "rgba(229,101,79,0.5)",
    })));
    ema20Series.setData(computeEMA(formattedCandles, 20));
    ema50Series.setData(computeEMA(formattedCandles, 50));
    loadMacroEma200(coin);
    chart.timeScale().fitContent();
  } catch (e) {
    console.error("loadCandlesAroundRange failed", e);
  }
}

// === СТИЛЬ ТОЧЕК ПУТИ — ДУБЛИРОВАНО ИЗ app.js::EVENT_MARKER_STYLE ===
// (там используется для боевых маркеров дашборда, тут — для полного пути
// одной выбранной сделки: от первого касания зоны до входа).
const EVENT_MARKER_STYLE = {
  // ENTRY стиль задаётся отдельно в playTradeAnimation (зависит от
  // LONG/SHORT) — тут не нужен, см. entryStyle ниже.
  CANCEL:             { color: "#5c6370", shape: "square" },
  DEAD:               { color: "#e5654f", shape: "square" },
  SWEEP_BOTTOM:       { color: "#9c27b0", shape: "circle" },
  RUNAWAY:            { color: "#e5654f", shape: "circle" },
  CLIMAX_NEAR_BREACH: { color: "#5aa9e6", shape: "circle" },
  CLIMAX_FAR_BREACH:  { color: "#5aa9e6", shape: "circle" },
  ZONE_TOUCH:         { color: "#5aa9e6", shape: "circle" },
  SCAN:               { color: "#f2c14e", shape: "circle" },
};

// level_id вотчера — "BC_SHORT_0.1072_0.1127__CLIMAX" — у CLIMAX и MIRROR
// на ОДНОЙ физической зоне отличается только суффиксом. Без этого суффикса
// получаем "базовый" id зоны — им и группируем.
function stripMode(levelId) {
  return levelId ? levelId.replace(/__(CLIMAX|MIRROR)$/, "") : levelId;
}

// Сделка -> все эпизоды (вотчеры) на её физической зоне, включая напарника
// (CLIMAX+MIRROR), если он тоже дошёл до сделки на той же зоне — просили
// показывать их вместе как одно целое, не два отдельных клика.
function findGroupEpisodes(trade) {
  const base = stripMode(trade.level_id);
  return simHistory.filter((ep) => ep.level_id && stripMode(ep.level_id) === base);
}

// === ОДНА СДЕЛКА = ОДНА ЗОНА (2 линии min/max, цвет по направлению:
// support/LONG — зелёный, resistance/SHORT — красный) + ВЕСЬ ПУТЬ
// вотчера(ов) этой зоны — от первого касания до входа, не только точка
// входа. Ничего лишнего с других зон на графике не остаётся. ===
function drawTradeDetail(trade) {
  stopSimPlayback();
  clearSimLevelLines();
  if (candleSeries) candleSeries.setMarkers([]);
  if (!trade || !globalCandles.length) return;

  const group = findGroupEpisodes(trade);
  if (!group.length) return;

  const mins = group.map((g) => g.level_min).filter((v) => v != null);
  const maxs = group.map((g) => g.level_max).filter((v) => v != null);
  if (!mins.length || !maxs.length) return;
  const zoneMin = Math.min(...mins);
  const zoneMax = Math.max(...maxs);
  const color = group[0].direction === "LONG" ? "#4caf7d" : "#e5654f"; // зелёный support / красный resistance

  let events = [];
  group.forEach((ep) => (ep.events || []).forEach((ev) => { if (ev.time) events.push(ev); }));
  events.sort((a, b) => a.time - b.time);
  if (!events.length) return;

  // Та же логика, что и в drawSnapshotLevels — полоса идёт от настоящей
  // даты уровня (level_date), а не от диапазона событий вотчера. Раньше
  // тут был диапазон "от первого до последнего события" (spanCandles) —
  // выглядело иначе, чем при обычном клике по монете, где полоса уже
  // обрезается по level_date. Теперь оба пути согласованы.
  const zoneStartSec = group[0].level_date ? dateInputToUnixSec(group[0].level_date) : null;
  let lineCandles = globalCandles;
  if (zoneStartSec != null) {
    const sliced = globalCandles.filter((c) => c.time >= zoneStartSec);
    if (sliced.length) lineCandles = sliced;
  }

  simLevelLines.push(...addZoneBand(zoneMin, zoneMax, color, lineCandles, `🧪 ${friendlyLevelType(group[0].level_type)}`));

  // title у Baseline-серии без priceLineVisible нигде реально не
  // отображается — подписываем явным текстом, какой именно уровень сработал.
  const statusEl = document.getElementById("sim-status");
  if (statusEl) statusEl.textContent = `Уровень: ${friendlyLevelType(group[0].level_type)}`;

  playTradeAnimation(events, group[0].direction);
}

// Плейбек — теперь только вдоль пути ВЫБРАННОЙ сделки (от первого события
// до входа), не всей симуляции разом. Показывает ВСЕ события (касание,
// шаги climax, проколы, скан...), не только вход — это и есть "путь".
// direction нужен только для ENTRY-маркера: LONG — зелёная стрелка вверх,
// SHORT — красная стрелка вниз (раньше ENTRY был всегда зелёной "вверх",
// даже у шортов — вводило в заблуждение).
function playTradeAnimation(events, direction) {
  stopSimPlayback();

  const entryStyle = direction === "SHORT"
    ? { color: "#e5654f", shape: "arrowDown" }
    : { color: "#4caf7d", shape: "arrowUp" };

  const markers = events.map((ev) => {
    const style = ev.type === "ENTRY" ? entryStyle : (EVENT_MARKER_STYLE[ev.type] || { color: "#8a8f98", shape: "circle" });
    return { time: ev.time, position: ev.type === "ENTRY" ? (direction === "SHORT" ? "belowBar" : "aboveBar") : "inBar", color: style.color, shape: style.shape, text: "" };
  });

  const playCandles = globalCandles.filter((c) => c.time >= events[0].time && c.time <= events[events.length - 1].time);
  if (playCandles.length < 2) {
    candleSeries.setMarkers(markers);
    return;
  }

  const TOTAL_MS = 1500;
  const STEP_MS = 20;
  const steps = Math.max(1, Math.floor(TOTAL_MS / STEP_MS));
  const candlesPerStep = Math.max(1, Math.ceil(playCandles.length / steps));

  let idx = 0;
  simPlaybackTimer = setInterval(() => {
    idx += candlesPerStep;
    const done = idx >= playCandles.length - 1;
    if (done) idx = playCandles.length - 1;

    const cursorTime = playCandles[idx].time;
    const revealed = markers.filter((m) => m.time <= cursorTime);
    const cursorMarker = { time: cursorTime, position: "inBar", color: "#5aa9e6", shape: "circle", text: "" };
    candleSeries.setMarkers([...revealed, cursorMarker]);

    if (done) {
      stopSimPlayback();
      candleSeries.setMarkers(markers);
    }
  }, STEP_MS);
}

function selectTrade(trade, rowEl) {
  document.querySelectorAll("#sim-trades-body tr").forEach((tr) => tr.classList.remove("selected"));
  if (rowEl) rowEl.classList.add("selected");
  drawTradeDetail(trade);
}

function renderTradesTable(trades, noDataNote) {
  const tradesBody = document.getElementById("sim-trades-body");
  if (!tradesBody) return;
  simTrades = trades || [];
  if (noDataNote) {
    tradesBody.innerHTML = `<tr><td colspan="12">${noDataNote}</td></tr>`;
    return;
  }
  if (!trades || !trades.length) {
    tradesBody.innerHTML = "<tr><td colspan='12'>сделок не найдено</td></tr>";
    return;
  }
  tradesBody.innerHTML = trades.map((t, i) => {
    const pct = t.result_percent;
    const pctText = pct === null || pct === undefined ? "" : `${pct > 0 ? "+" : ""}${pct}%`;
    const durationText = t.status === "⏳"
      ? formatDuration(t.time, new Date())
      : (t.closed_at ? formatDuration(t.time, new Date(t.closed_at)) : "—");
    const methodText = t.method === "volume" && t.method_value != null
      ? formatVolume(t.method_value) + (t.method_mult != null ? " / x" + t.method_mult.toFixed(1) : "")
      : "—";
    return `
    <tr data-idx="${i}">
      <td>${t.date ?? ""}</td>
      <td>${t.coin ?? ""}</td>
      <td class="dir-${t.type}">${t.type ?? ""}</td>
      <td>${t.source ?? ""}</td>
      <td>${formatPrice(t.entry)}</td>
      <td>${formatPrice(t.target)}</td>
      <td>${formatPrice(t.stop)}</td>
      <td>${t.status ?? ""}</td>
      <td style="color:${pct > 0 ? '#4caf7d' : pct < 0 ? '#e5654f' : 'inherit'}">${pctText}</td>
      <td>${durationText}</td>
      <td>${methodText}</td>
      <td>${t.rr ?? ""}</td>
    </tr>
  `;
  }).join("");
  tradesBody.querySelectorAll("tr[data-idx]").forEach((tr) => {
    tr.onclick = () => selectTrade(simTrades[Number(tr.dataset.idx)], tr);
  });
}

async function runSimulation() {
  if (!selectedCoin || !simStartTime || simRunning) return;
  simRunning = true;
  stopSimPlayback();
  // Маркеры прошлого прогона чистим сразу, а вот линии снимка уровней "От"
  // (см. drawSnapshotLevels) НЕ трогаем — их уже нарисовали при выборе
  // даты, и они актуальны всё то же время, пока не сменили дату/монету.
  if (candleSeries) candleSeries.setMarkers([]);

  const status = document.getElementById("sim-status");
  const runBtn = document.getElementById("sim-run-btn");
  const strategySelect = document.getElementById("sim-strategy-select");
  const isBounce = !strategySelect || strategySelect.value === "bounce";
  runBtn.disabled = true;
  status.textContent = "Считаю...";
  renderTradesTable(null, "Считаю...");

  const startIso = new Date(simStartTime * 1000).toISOString();
  const startParam = startIso.slice(0, 16).replace("T", " ");
  let endpoint = isBounce
    ? `/api/simulate_bounce/${encodeURIComponent(selectedCoin)}?start=${encodeURIComponent(startParam)}`
    : `/api/simulate/${encodeURIComponent(selectedCoin)}?start=${encodeURIComponent(startParam)}`;
  // "До" указано — период ограничен, иначе (как раньше) до конца графика.
  // end= понимает только /api/simulate_bounce — у V-семейства (/api/simulate)
  // такого параметра нет вообще, добавлять туда нечего.
  if (simEndTime && isBounce) {
    const endIso = new Date(simEndTime * 1000).toISOString();
    const endParam = endIso.slice(0, 16).replace("T", " ");
    endpoint += `&end=${encodeURIComponent(endParam)}`;
  }

  try {
    const res = await fetch(endpoint, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      status.textContent = data.detail || "ошибка симуляции";
      renderTradesTable(null, data.detail || "ошибка");
      return;
    }

    simHistory = data.history || [];

    const endTimeSec = data.end_time ? Math.floor(new Date(data.end_time + "Z").getTime() / 1000) : Math.floor(Date.now() / 1000);
    await loadCandlesAroundRange(selectedCoin, simStartTime, endTimeSec);

    // V-семейство (/api/simulate/{coin}) пока не отдаёт структурированный
    // список сделок (data.trades) — только эпизоды вотчеров. Не выдумываем
    // данные, которых нет: честно показываем это в таблице, не оставляем
    // пустое "сделок не найдено", которое выглядело бы как "их не было".
    if (isBounce) {
      renderTradesTable(data.trades || []);
      // Сразу открываем первую сделку из списка — не сваливаем всё в кучу.
      const firstRow = document.querySelector('#sim-trades-body tr[data-idx="0"]');
      if (simTrades.length) selectTrade(simTrades[0], firstRow);
    } else {
      renderTradesTable(null, "Для V-семейства таблица сделок пока не поддерживается бэкендом (нет data.trades в /api/simulate)");
    }

    // Результат уже виден в таблице сделок ниже — статус наверху просто
    // возвращаем к выбранному периоду (не затираем его текстом "Найдено...").
    updateSimUI();
  } catch (e) {
    console.error("simulate failed", e);
    status.textContent = "ошибка запроса";
    renderTradesTable(null, "ошибка запроса");
  } finally {
    runBtn.disabled = false;
    simRunning = false;
  }
}

// Восстановление последнего результата после F5 — источник тот же
// last_run.json на сервере (см. bounce_simulate.py::LAST_RUN_FILE).
// Больше не может быть затёрто фоновым обновлением — на этой странице
// фонового обновления графика нет вообще.
async function restoreLastBounceSimIfAny() {
  const tradesBody = document.getElementById("sim-trades-body");
  if (!tradesBody) return;
  try {
    const res = await fetch("/api/simulate_bounce/last");
    const data = await res.json();
    if (!data || !data.coin) return; // ещё ни разу не запускали

    await new Promise((resolve) => {
      const check = () => (chart && candleSeries ? resolve() : setTimeout(check, 50));
      check();
    });

    await loadChartForCoin(data.coin);
    simMode = true;
    updateSimUI();

    const startSec = data.start_time
      ? Math.floor(new Date(data.start_time.replace(" ", "T") + "Z").getTime() / 1000)
      : null;
    const endSec = data.end_time
      ? Math.floor(new Date(data.end_time.replace(" ", "T") + "Z").getTime() / 1000)
      : Math.floor(Date.now() / 1000);

    const status = document.getElementById("sim-status");
    if (!startSec) {
      // Старого прогона без start_time мы честно НЕ можем разместить точки/
      // уровни (не на чем — не знаем, откуда начинать окно свечей) — молча
      // ничего не рисовать было бы вводящим в заблуждение, поэтому явно
      // говорим об этом в статусе и показываем хотя бы таблицу сделок.
      renderTradesTable(data.trades || []);
      if (status) status.textContent = "Восстановлено частично: нет даты старта, точки/уровни не показаны";
      return;
    }

    simStartTime = startSec;
    await loadCandlesAroundRange(data.coin, startSec, endSec);

    simHistory = data.history || [];
    renderTradesTable(data.trades || []);
    const firstRow = document.querySelector('#sim-trades-body tr[data-idx="0"]');
    if (simTrades.length) selectTrade(simTrades[0], firstRow);

    if (status) status.textContent = `Восстановлено: старт ${data.start_time ?? ""}`;
  } catch (e) {
    console.error("restoreLastBounceSimIfAny failed", e);
  }
}

initChart();
loadSimCoins();
restoreLastBounceSimIfAny();