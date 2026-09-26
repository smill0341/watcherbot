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
let simSnapshotLines = []; // отдельно: уровни "на момент клика по пустому месту"
                            // (drawSnapshotLevels) — свой массив, чтобы не стирать
                            // simLevelLines (активная сделка + фон других сделок)
let simPlaybackTimer = null;
let simHistory = [];  // data.history последнего прогона — для поиска эпизодов по клику на сделку
let simTrades = [];   // data.trades последнего прогона

let isViewAllTrades = false; // 🔥 НОВОЕ
let activeTrade = null;      // 🔥 НОВОЕ
let activeTradeSource = "sim"; // Запоминает, из какой таблицы был клик

// === bulk-прогон (все монеты) — своё состояние, не пересекается с simHistory/
// simTrades одиночного симулятора выше. bulkHistoryByCoin используется точно
// так же, как simHistory: клик по сделке ищет в нём свой эпизод, просто ключ —
// монета, потому что сделок тут много и по разным монетам разом.
let bulkTrades = [];          // плоский список сделок по всем монетам bulk-прогона
let bulkHistoryByCoin = {};   // {coin: [...эпизоды TRIGGERED/DEAD...]}

const TF_LIMITS = { "15m": null, "1h": null, "4h": null, "1d": null, "1w": null, "1M": null };
let currentTimeframe = "15m";

// === ДУБЛИРОВАНО ИЗ app.js::formatPrice — синхронизировать вручную ===
function formatPrice(v) {
  if (v === null || v === undefined || v === "") return "";
  const num = Number(v);
  if (Number.isNaN(num)) return String(v);
  const abs = Math.abs(num);
  
  // Умная градация знаков после запятой
  let decimals;
  if (abs >= 100) decimals = 2;
  else if (abs >= 10) decimals = 3;
  else if (abs >= 0.1) decimals = 4;
  else if (abs >= 0.01) decimals = 5;
  else if (abs >= 0.001) decimals = 6;
  else decimals = 8;

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

  // Если уровень составной (содержит плюсы), разбиваем, переводим каждую часть и склеиваем обратно
  if (type.includes("+")) {
    return type.split("+").map(part => _translateToken(part.trim())).join(" + ");
  }
  
  // Если склеен через пробелы с плюсом
  if (type.includes(" + ")) {
    return type.split(" + ").map(part => _translateToken(part.trim())).join(" + ");
  }

  return _translateToken(type);
}

function _translateToken(token) {
  const t = token.toLowerCase();

  if (t.includes("pmc")) return "1M close";
  if (t.includes("pmh")) return "Хай месяца";
  if (t.includes("pml")) return "Лоу месяца";
  if (t.includes("pwh")) return "Хай недели";
  if (t.includes("pwl")) return "Лоу недели";
  if (t.includes("pdh")) return "Хай дня";
  if (t.includes("pdl")) return "Лоу дня";
  if (t.includes("extreme_peak")) return "Пик";
  if (t.includes("poc")) return "POC";
  if (t.includes("macro_support")) return "Macro Sup";
  if (t.includes("macro_resistance")) return "Macro Res";
  if (t.includes("confluence")) return "Конфлюэнция";

  // Если попался редкий или кастомный тип — просто убираем технические подчеркивания
  return token.replace(/_/g, " ");
}

// Кусок подписи уровня "касаний: N" — то же самое, что app.js::levelTouchesLabel
// (reaction_count, см. levels_builder.py::count_zone_touches). Дублируем
// принцип, а не импортируем — симулятор и главный дашборд разные файлы,
// без общего модуля между ними.
function levelTouchesLabel(z) {
  if (!z) return "";
  const n = z.reaction_count;
  return (n === undefined || n === null) ? "" : ` · Касаний: ${n}`;
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

function clearSimSnapshotLines() {
  simSnapshotLines.forEach((series) => { try { chart.removeSeries(series); } catch (e) {} });
  simSnapshotLines = [];
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
      clearSimSnapshotLines();
      candleSeries.setMarkers([]);
      updateSimUI();
    };
  }
  const simRunBtnEl = document.getElementById("sim-run-btn");
  if (simRunBtnEl) simRunBtnEl.onclick = runSimulation;

  const simBulkBtnEl = document.getElementById("sim-bulk-btn");
  if (simBulkBtnEl) simBulkBtnEl.onclick = runBulkSimulation;

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
// Рисует ОДНУ зону как закрашенную полосу между min и max
function addZoneBand(rawMin, rawMax, color, candles, titleText, styleMode = "normal") {
  const zMin = Math.min(rawMin, rawMax);
  const zMax = Math.max(rawMin, rawMax);

  let topAlpha = "40";
  let bottomAlpha = "15";
  let weight = 1; // По умолчанию всё тонкое

  if (styleMode === "active") {
    weight = 2; // Только выделенная сделка чуть толще
  } else if (styleMode === "bg" || styleMode === "snapshot") {
    // 1A (10%) было слишком мало для красного цвета, он сливался с белым фоном.
    topAlpha = "33"; // Прозрачность 20%
    bottomAlpha = "1A"; // Прозрачность 10%
  }

  const bandSeries = chart.addBaselineSeries({
    baseValue: { type: "price", price: zMin },
    topFillColor1: "#" + hexWithAlpha(color, topAlpha),
    topFillColor2: "#" + hexWithAlpha(color, bottomAlpha),
    topLineColor: color,
    bottomFillColor1: "rgba(0,0,0,0)",
    bottomFillColor2: "rgba(0,0,0,0)",
    bottomLineColor: "rgba(0,0,0,0)",
    lineWidth: weight,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    title: titleText || "",
    autoscaleInfoProvider: () => null,
  });
  bandSeries.setData(candles.map((c) => ({ time: c.time, value: zMax })));

  const bottomLine = chart.addLineSeries({
    color,
    lineWidth: weight,
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
//
// Свой массив (simSnapshotLines) и своя очистка — НЕ трогаем simLevelLines
// (зона активной сделки + фон других сделок) и НЕ сбрасываем маркеры
// candleSeries. Раньше это была общая перерисовка "с нуля", поэтому клик
// по пустому месту графика стирал выбранную сделку — теперь снимок уровней
// просто накладывается поверх того, что уже нарисовано (пунктиром, чтобы
// не путать с сама сделкой).
async function drawSnapshotLevels(coin, whenSec) {
  clearSimSnapshotLines();
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
      // Используем activated_at если есть, иначе дату уровня
      const zoneStartSec = (z.activated_at || z.date) ? dateInputToUnixSec(z.activated_at || z.date) : null;
      let candlesForZone = globalCandles;
      if (zoneStartSec != null) {
        const sliced = globalCandles.filter((c) => c.time >= zoneStartSec);
        if (sliced.length) candlesForZone = sliced;
      }
      simSnapshotLines.push(...addZoneBand(z.min, z.max, z.color, candlesForZone, `${friendlyLevelType(z.type)} · ${z.date || "—"} · ${z.score ?? "—"}${levelTouchesLabel(z)}`, "snapshot"));
    });
  } catch (e) {
    console.error("drawSnapshotLevels failed", e);
  }
}

function updateSimUI() {
  const status = document.getElementById("sim-status");
  const runBtn = document.getElementById("sim-run-btn");
  const bulkBtn = document.getElementById("sim-bulk-btn");
  if (!status || !runBtn) return;

  if (simStartTime) {
    status.textContent = ""; 
    runBtn.style.display = "";
    if (bulkBtn) bulkBtn.style.display = "";
  } else {
    status.textContent = "Укажи дату (или кликни свечу)";
    runBtn.style.display = "none";
    if (bulkBtn) bulkBtn.style.display = "none";
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
    // Монета могла быть выбрана раньше, чем пришёл список (открытие по
    // ссылке /simulator?coin=XXX) — подсвечиваем её и в списке.
    highlightSelection();
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
  clearSimSnapshotLines();
  if (candleSeries) candleSeries.setMarkers([]);
  if (isCoinChange) {
    // Смена монеты — старая точка старта и старый результат теряют смысл.
    simStartTime = null;
    simEndTime = null;
    if (simStartInputEl) simStartInputEl.value = "";
    if (simEndInputEl) simEndInputEl.value = "";
  }
  updateSimUI();

  try {
    const ok = await loadFullCandlesForCoin(coin);
    if (!ok) return;

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

// Собственно загрузка + отрисовка свечей монеты на график — БЕЗ побочных
// эффектов на состояние периода/UI (это отдельно решает loadChartForCoin
// выше). Всегда вся доступная локальная история (TF_LIMITS для страницы
// симулятора — null на всех таймфреймах, см. объявление TF_LIMITS), без
// узких окон — переиспользуется и обычным кликом по монете слева, и
// кликом по bulk-сделке (см. selectBulkTrade): график должен грузиться
// одинаково в обоих местах, не двумя разными путями с разным поведением.
async function loadFullCandlesForCoin(coin) {
  const limit = TF_LIMITS[currentTimeframe]; // null -> вся история из базы, без ограничения
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
    return false;
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
  return true;
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
  clearSimSnapshotLines(); // новая сделка выбрана — старый снимок уровней (от прошлого клика по графику) больше не в тему
  if (candleSeries) candleSeries.setMarkers([]);
  if (!trade || !globalCandles.length) return;

  const group = findGroupEpisodes(trade);
  
  // БРОНЯ: Если истории нет (зеркальные сделки), рисуем просто зону по данным из таблицы
  if (!group.length) {
    if (trade.level_min && trade.level_max) {
      const color = trade.type === "LONG" ? "#4caf7d" : "#e5654f";
      const endSec = trade.closed_at ? Math.floor(new Date(trade.closed_at).getTime() / 1000) : Math.floor(Date.now() / 1000);
      const lineCandles = globalCandles.filter(c => c.time >= (trade.time - 2*24*3600) && c.time <= endSec);
      
      const lvlTypeStr = trade.level_type ? friendlyLevelType(trade.level_type) : "—";
      simLevelLines.push(...addZoneBand(trade.level_min, trade.level_max, color, lineCandles, `🧪 ${lvlTypeStr}`, "active"));

      const entryStyle = trade.type === "SHORT" ? { color: "#e5654f", shape: "arrowDown" } : { color: "#4caf7d", shape: "arrowUp" };
      const markers = [{ time: trade.time, position: trade.type === "SHORT" ? "belowBar" : "aboveBar", color: entryStyle.color, shape: entryStyle.shape, text: "" }];
      
      const closeM = buildCloseMarker(trade);
      if (closeM) markers.push(closeM);
      
      // 🔥 Подмешиваем бледные маркеры и зоны других сделок (если глазок включен)
      let bgMarkers = typeof drawBackgroundTradesAndGetMarkers === "function" ? drawBackgroundTradesAndGetMarkers() : [];
      candleSeries.setMarkers([...bgMarkers, ...markers].sort((a,b) => a.time - b.time));
      
      centerChartOnEvents([{ time: trade.time }], endSec);
    }
    const statusEl = document.getElementById("sim-status");
    if (statusEl) statusEl.textContent = "";
    return;
  }

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

  // Полоса идёт от настоящей даты уровня (level_date) и до момента, когда
  // СДЕЛКА разрешилась (closed_at) — не до конца всей истории. Если
  // уровень уже отработал, дальше он не торгуется, значит и рисовать его
  // как "ещё актуальный" не нужно. Для ⏳ (ещё не закрыта) — тянем до
  // "сейчас", это единственный случай, где зона реально ещё жива.
  // Рисуем от первого события в event_log - это реальный старт слежки
  const zoneStartSec = events[0].time;
  const zoneEndSec = trade.closed_at
    ? Math.floor(new Date(trade.closed_at).getTime() / 1000)
    : Math.floor(Date.now() / 1000);
  let lineCandles = globalCandles.filter((c) => c.time <= zoneEndSec);
  if (zoneStartSec != null) {
    const sliced = lineCandles.filter((c) => c.time >= zoneStartSec);
    if (sliced.length) lineCandles = sliced;
  }

  simLevelLines.push(...addZoneBand(zoneMin, zoneMax, color, lineCandles, `🧪 ${friendlyLevelType(group[0].level_type)} · ${group[0].level_date || "—"} · ${group[0].level_score ?? "—"}`, "active"));

  const statusEl = document.getElementById("sim-status");
  if (statusEl) statusEl.textContent = ""; // Убрали текст с названием уровня

  // Данные всегда полные (вся локальная история монеты, без урезания —
  // см. loadFullCandlesForCoin), поэтому сама сделка может оказаться
  // крошечным отрезком на фоне месяцев истории. Явно просим график
  // "камерой" приблизиться к месту сделки — это не подмена данных,
  // просто какой диапазон сейчас показан, весь остальной график
  // никуда не делся, можно спокойно проскроллить/отдалить обратно.
  // Правая граница — zoneEndSec (закрытие сделки), а не последнее
  // событие вотчера: вотчер пишет события только ДО входа (ENTRY —
  // последнее, что он вообще знает), а сделка может держаться ещё
  // много дней после входа до реального закрытия.
  centerChartOnEvents(events, zoneEndSec);

  // Крупная точка на месте реального закрытия сделки — куда бы оно ни
  // пришлось (TP ✅, дедлайн 14 дней 🕐). Не требует ничего нового от
  // бэкенда: closed_at/status/result_percent уже есть в каждой сделке.
  // ⏳ (ещё не закрыта) — closed_at нет, значит и точку ставить не на что,
  // это честно, не выдумываем момент, которого не было.
  playTradeAnimation(events, group[0].direction, buildCloseMarker(trade));
}

// Точка конца сделки — зелёная (в плюс) или красная (в минус), крупнее
// обычных маркеров пути, с подписью причины закрытия. Насколько именно
// цена была "у стопа" — этот симулятор в принципе не считает: сделка
// закрывается только по TP или по дедлайну (см. докстринг
// _compute_trade_outcome в bounce_simulate.py) — closed_at это и есть
// честный момент, который система реально засчитала как конец.
function buildCloseMarker(trade) {
  if (!trade.closed_at) return null;
  const win = (trade.result_percent ?? 0) > 0;
  return {
    time: Math.floor(new Date(trade.closed_at).getTime() / 1000),
    position: "inBar",
    color: win ? "#4caf7d" : "#e5654f",
    shape: "circle",
    size: 2,
    text: trade.status === "🕐" ? "14д" : "TP",
  };
}

// Приближает видимую область графика к диапазону сделки — от первого
// события вотчера (касание/скан ДО входа) до момента закрытия сделки
// (tradeEndSec), с отступом по паре дней с каждой стороны, чтобы был
// виден контекст (что было до и после), а не только точки впритык.
function centerChartOnEvents(events, tradeEndSec, paddingSec = 2 * 24 * 3600) {
  if (!chart || !events.length) return;
  const rightEdge = Math.max(events[events.length - 1].time, tradeEndSec ?? 0);
  try {
    chart.timeScale().setVisibleRange({
      from: events[0].time - paddingSec,
      to: rightEdge + paddingSec,
    });
  } catch (e) {
    // Диапазон сделки может вылезать за пределы того, что реально
    // загружено (край доступной истории) — не критично, график просто
    // останется в состоянии fitContent(), как после обычной загрузки.
  }
}

// Плейбек — теперь только вдоль пути ВЫБРАННОЙ сделки (от первого события
// до входа), не всей симуляции разом. Показывает ВСЕ события (касание,
// шаги climax, проколы, скан...), не только вход — это и есть "путь".
// direction нужен только для ENTRY-маркера: LONG — зелёная стрелка вверх,
// SHORT — красная стрелка вниз (раньше ENTRY был всегда зелёной "вверх",
// даже у шортов — вводило в заблуждение).
function playTradeAnimation(events, direction, closeMarker) {
  stopSimPlayback();

  const entryStyle = direction === "SHORT"
    ? { color: "#e5654f", shape: "arrowDown" }
    : { color: "#4caf7d", shape: "arrowUp" };

  const markers = events.map((ev) => {
    const style = ev.type === "ENTRY" ? entryStyle : (EVENT_MARKER_STYLE[ev.type] || { color: "#8a8f98", shape: "circle" });
    return { time: ev.time, position: ev.type === "ENTRY" ? (direction === "SHORT" ? "belowBar" : "aboveBar") : "inBar", color: style.color, shape: style.shape, text: "" };
  });
  if (closeMarker) markers.push(closeMarker);

  // 🔥 Получаем фоновые маркеры и рисуем их зоны
  let bgMarkers = typeof drawBackgroundTradesAndGetMarkers === "function" ? drawBackgroundTradesAndGetMarkers() : [];
  const combinedFinal = [...bgMarkers, ...markers].sort((a,b) => a.time - b.time);

  const playCandles = globalCandles.filter((c) => c.time >= events[0].time && c.time <= events[events.length - 1].time);
  if (playCandles.length < 2) {
    candleSeries.setMarkers(combinedFinal);
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
    
    // 🔥 Подмешиваем фон, чтобы он не пропадал при анимации
    candleSeries.setMarkers([...bgMarkers, ...revealed, cursorMarker].sort((a,b) => a.time - b.time));

    if (done) {
      stopSimPlayback();
      candleSeries.setMarkers(combinedFinal);
    }
  }, STEP_MS);
}
function selectTrade(trade, rowEl) {
  document.querySelectorAll("#sim-trades-body tr").forEach((tr) => tr.classList.remove("selected"));
  if (rowEl) rowEl.classList.add("selected");
  activeTrade = trade;
  activeTradeSource = "sim"; // <--- Жестко фиксируем источник
  const toggleBtn = document.getElementById("sim-view-toggle");
  if (toggleBtn) toggleBtn.style.display = "inline-block";
  drawTradeDetail(activeTrade);
}

// Разметка ОДНОЙ строки таблицы сделок — общая для одиночного симулятора
// (renderTradesTable/#sim-trades-body) и bulk-вкладки "Все сделки"
// (renderBulkTradesTable/#sim-bulk-trades-body). Колонки одинаковые в обеих
// таблицах (см. simulator.html), поэтому строим HTML один раз, а не дважды
// одним и тем же кодом с риском разъехаться при следующей правке.
function _tradeRowHtml(t, i) {
  const pct = t.result_percent;
  const pctText = pct === null || pct === undefined ? "" : `${pct > 0 ? "+" : ""}${pct}%`;
  const durationText = t.status === "⏳"
    ? formatDuration(t.time, new Date())
    : (t.closed_at ? formatDuration(t.time, new Date(t.closed_at)) : "—");
  const methodText = t.method === "volume" && t.method_value != null
    ? formatVolume(t.method_value) + (t.method_mult != null ? " / x" + t.method_mult.toFixed(1) : "")
    : "—";
  const levelTypeStr = t.level_type ? friendlyLevelType(t.level_type) : "—"; // <--- Добавили форматирование
  return `
    <tr data-idx="${i}">
      <td>${t.date ?? ""}</td>
      <td>${t.coin ?? ""}</td>
      <td class="dir-${t.type}">${t.type ?? ""}</td>
      <td>${t.source ?? ""}</td>
      <td>${levelTypeStr}</td> <!-- Вывели колонку -->
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
  tradesBody.innerHTML = trades.map((t, i) => _tradeRowHtml(t, i)).join("");
  tradesBody.querySelectorAll("tr[data-idx]").forEach((tr) => {
    tr.onclick = () => selectTrade(simTrades[Number(tr.dataset.idx)], tr);
  });
}

// То же самое для bulk-вкладки — единственная разница с renderTradesTable:
// пишет в bulkTrades (не в simTrades) и клик открывает selectBulkTrade
// (переключает монету на графике перед отрисовкой — сделки тут вперемешку
// с разных монет, не одна, как в одиночном симуляторе).
function renderBulkTradesTable(trades) {
  const tradesBody = document.getElementById("sim-bulk-trades-body");
  if (!tradesBody) return;
  bulkTrades = trades || [];
  if (!bulkTrades.length) {
    tradesBody.innerHTML = "<tr><td colspan='12'>сделок не найдено</td></tr>";
    return;
  }
  tradesBody.innerHTML = bulkTrades.map((t, i) => _tradeRowHtml(t, i)).join("");
  tradesBody.querySelectorAll("tr[data-idx]").forEach((tr) => {
    tr.onclick = () => selectBulkTrade(bulkTrades[Number(tr.dataset.idx)], tr);
  });
}

// Клик по сделке из bulk-списка — тот же принцип, что и в selectTrade()
// одиночного симулятора (drawTradeDetail рисует по simHistory/globalCandles),
// просто ПЕРЕД отрисовкой переключаем эти общие "текущий контекст"
// переменные на монету этой конкретной сделки. История уже в памяти
// (bulkHistoryByCoin — пришла одним общим bulk-ответом, см.
// runBulkSimulation/restoreLastBulkSimIfAny), новый поход на сервер нужен
// только за свечами — та же полная загрузка, что и при обычном клике по
// монете слева (loadFullCandlesForCoin), без урезания до какого-то окна:
// раз вся история уже на диске, зачем её резать. Приближение к самой
// сделке на графике делает drawTradeDetail() через centerChartOnEvents().
async function selectBulkTrade(trade, rowEl) {
  if (!trade) return;
  document.querySelectorAll("#sim-bulk-trades-body tr").forEach((tr) => tr.classList.remove("selected"));
  if (rowEl) rowEl.classList.add("selected");

  simHistory = bulkHistoryByCoin[trade.coin] || [];
  selectedCoin = trade.coin;
  if (currentCoinEl) currentCoinEl.textContent = trade.coin;
  highlightSelection();

  await loadFullCandlesForCoin(trade.coin);

  activeTrade = trade;
  activeTradeSource = "bulk"; // <--- Жестко фиксируем источник
  const toggleBtn = document.getElementById("sim-view-toggle");
  if (toggleBtn) toggleBtn.style.display = "inline-block";
  drawTradeDetail(activeTrade);
}

// Читает #sim-direction-select и превращает его в query-параметры
// allow_long/allow_short — общая точка для одиночного прогона (runSimulation)
// и bulk (runBulkSimulation), чтобы оба всегда понимали значение одинаково.
function directionQueryParams() {
  const sel = document.getElementById("sim-direction-select");
  const value = sel ? sel.value : "both";
  if (value === "long") return "&allow_long=true&allow_short=false";
  if (value === "short") return "&allow_long=false&allow_short=true";
  return "&allow_long=true&allow_short=true";
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
  // LONG/SHORT — тоже только у BOUNCE (V-семейство свой отдельный набор
  // вотчеров, туда это не относится вообще).
  if (isBounce) {
    endpoint += directionQueryParams();
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

    // Загружаем ВСЮ историю монеты без обрезки
    await loadFullCandlesForCoin(selectedCoin);

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
// === BULK-ПРОГОН ПО ВСЕМ МОНЕТАМ (отчёт в отдельном окне) ===============
// Полностью аддитивный блок: ничего из существующих функций выше не
// меняет. Список монет слева (loadSimCoins/#sim-list) и одиночный прогон
// (runSimulation/#sim-run-btn) продолжают работать ровно как раньше —
// bulk использует тот же ужё выбранный период (simStartTime/simEndTime),
// просто зовёт отдельный эндпоинт /api/simulate_bounce_bulk, который сам
// внутри себя решает, какие монеты имели уровни в этом периоде (см.
// bounce_simulate.list_coins_with_levels на бэкенде) — независимо от
// того, что сейчас показано в левом списке (тот всегда "сегодняшний").
//
// Кнопка (#sim-bulk-btn), диалог (#sim-bulk-dialog/#sim-bulk-body) и его
// закрытие (#sim-bulk-close) — разметка и стили в simulator.html/style.css
// (.bulk-dialog/.bulk-table), здесь их только находим и наполняем.
// Обработчики клика вешаются в initChart(), рядом с sim-run-btn/
// sim-cancel-btn — тем же способом, каким там уже всё сделано.

function renderBulkReport(data) {
  const body = document.getElementById("sim-bulk-body");
  if (!body) return;
  if (!data || !data.results || Object.keys(data.results).length === 0) {
    body.innerHTML = "<div class='muted'>Монет с уровнями за этот период не найдено</div>";
    return;
  }

  const rows = Object.entries(data.results)
    // Монеты со сделками — сверху, ошибки — в самый низ, остальное по алфавиту.
    .sort((a, b) => {
      const aErr = !!a[1].error, bErr = !!b[1].error;
      if (aErr !== bErr) return aErr ? 1 : -1;
      return (b[1].total_trades || 0) - (a[1].total_trades || 0);
    })
    .map(([coin, r]) => {
      if (r.error) {
        return `<tr><td>${coin}</td><td colspan="6" class="muted">ошибка: ${r.error}</td></tr>`;
      }
      const cov = r.levels_coverage || {};
      const covStr = cov.snapshots_total ? `${cov.snapshots_with_levels}/${cov.snapshots_total}` : "-";
      const longWr = r.long.win_rate_pct != null ? `${r.long.win_rate_pct}%` : "-";
      const shortWr = r.short.win_rate_pct != null ? `${r.short.win_rate_pct}%` : "-";
      return `<tr>
        <td>${coin}</td>
        <td>${r.total_trades}</td>
        <td class="dir-LONG">${r.long.count} (${longWr})</td>
        <td class="dir-SHORT">${r.short.count} (${shortWr})</td>
        <td>${r.long.avg_result_pct ?? "-"}</td>
        <td>${r.short.avg_result_pct ?? "-"}</td>
        <td class="muted">${covStr}</td>
      </tr>`;
    })
    .join("");

  body.innerHTML = `
    <div class="bulk-summary">
      Период: ${data.start_time} → ${data.end_time || "сейчас"} ·
      монет просканировано: ${data.coins_scanned} · со сделками: ${data.coins_with_trades}
    </div>
    <table class="bulk-table">
      <thead>
        <tr>
          <th>Монета</th>
          <th>Сделок</th>
          <th>LONG (WR)</th>
          <th>SHORT (WR)</th>
          <th>Avg LONG %</th>
          <th>Avg SHORT %</th>
          <th>Покрытие уровней</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function runBulkSimulation() {
  if (!simStartTime) return;
  const body = document.getElementById("sim-bulk-body");
  const progressEl = document.getElementById("sim-bulk-progress");
  if (!body) return;
  body.innerHTML = "<div class='muted'>Запускаю...</div>";
  if (progressEl) progressEl.textContent = "Запускаю...";

  const startIso = new Date(simStartTime * 1000).toISOString();
  const startParam = startIso.slice(0, 16).replace("T", " ");
  let endpoint = `/api/simulate_bounce_bulk?start=${encodeURIComponent(startParam)}`;
  if (simEndTime) {
    const endIso = new Date(simEndTime * 1000).toISOString();
    const endParam = endIso.slice(0, 16).replace("T", " ");
    endpoint += `&end=${encodeURIComponent(endParam)}`;
  }
  endpoint += directionQueryParams();

  // Сам POST долгий и отвечает только когда прогон целиком закончен —
  // пока ждём его, отдельно опрашиваем /progress раз в секунду и
  // обновляем счётчик "X/Y — MONETA" в шапке над вкладками (виден на
  // любой из них, не только на "Отчёт по монетам"). Опрос останавливаем
  // в finally, что бы ни случилось с основным запросом (успех/ошибка).
  const pollProgress = async () => {
    try {
      const res = await fetch("/api/simulate_bounce_bulk/progress");
      const p = await res.json();
      if (progressEl && p && p.total) {
        const coinLabel = p.current_coin ? ` — ${p.current_coin}` : "";
        progressEl.textContent = `Считаю... ${p.done}/${p.total}${coinLabel}`;
      }
    } catch (e) {
      // молча — это фоновый опрос, ошибка тут не критична, основной fetch важнее
    }
  };
  const progressTimer = setInterval(pollProgress, 1000);
  pollProgress(); // не ждать первую секунду, спросить сразу

  try {
    const res = await fetch(endpoint, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      body.innerHTML = `<div class="muted">${data.detail || "ошибка bulk-симуляции"}</div>`;
      return;
    }
    applyBulkResult(data);
  } catch (e) {
    console.error("bulk simulate failed", e);
    body.innerHTML = "<div class='muted'>ошибка запроса</div>";
  } finally {
    clearInterval(progressTimer);
    // Результат уже разложен по вкладкам — счётчик в шапке своё отработал,
    // дальше висел бы неактуальным остатком прошлого прогона.
    if (progressEl) progressEl.textContent = "";
  }
}

// Раскладывает один bulk-ответ (свежий прогон ИЛИ восстановленный после F5
// из /api/simulate_bounce_bulk/last — формат тот же) по обеим вкладкам и
// сохраняет bulkHistoryByCoin для клика по сделке (см. selectBulkTrade).
// Общая точка, чтобы "свежий прогон" и "восстановление после F5" не могли
// разъехаться в том, как они раскладывают один и тот же формат ответа.
function applyBulkResult(data) {
  renderBulkReport(data);
  renderBulkTradesTable(data.trades || []);
  bulkHistoryByCoin = data.history_by_coin || {};
}

// Восстановление последнего bulk-прогона после F5 — тот же принцип, что и
// restoreLastBounceSimIfAny() для одиночного симулятора (см. ниже), свой
// файл на сервере (last_all_run.json), без пересчёта.
async function restoreLastBulkSimIfAny() {
  try {
    const res = await fetch("/api/simulate_bounce_bulk/last");
    const data = await res.json();
    if (!data || !data.results || Object.keys(data.results).length === 0) return;
    applyBulkResult(data);
  } catch (e) {
    console.error("restoreLastBulkSimIfAny failed", e);
  }
}

// Переключатель вкладок под графиком — простой show/hide, тот же паттерн,
// что уже есть у .tf-btn (переключение таймфреймов) чуть выше в файле.
function initSimTabs() {
  const tabBtns = document.querySelectorAll(".sim-tab-btn");
  if (!tabBtns.length) return;
  tabBtns.forEach((btn) => {
    btn.onclick = () => {
      tabBtns.forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".sim-tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `tab-${btn.dataset.tab}`);
      });
    };
  });
}

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

    const status = document.getElementById("sim-status");
    if (!startSec) {
      // Старого прогона без start_time мы честно НЕ можем разместить точки/
      // уровни (не на чем — не знаем, откуда начинать окно свечей) — молча
      // ничего не рисовать было бы вводящим в заблуждение, поэтому явно
      // говорим об этом в статусе и показываем хотя бы таблицу сделок.
      renderTradesTable(data.trades || []);
      if (status) status.textContent = ""; // Убрали текст
      return;
    }

    simStartTime = startSec;
    // Свечи уже полностью загружены выше (loadChartForCoin) — раньше тут
    // был повторный поход за узким окном вокруг периода, который просто
    // затирал уже загруженную полную историю худшей, урезанной версией.

    simHistory = data.history || [];
    renderTradesTable(data.trades || []);
    const firstRow = document.querySelector('#sim-trades-body tr[data-idx="0"]');
    if (simTrades.length) selectTrade(simTrades[0], firstRow);

    if (status) status.textContent = ""; // Убрали текст
  } catch (e) {
    console.error("restoreLastBounceSimIfAny failed", e);
  }
}

// Открытие по ссылке с дашборда (кнопка 📈 у монеты, /simulator?coin=XXX)
// — сразу эта монета, текущий месяц, уровни на дату старта. Последний
// одиночный прогон в этом случае НЕ восстанавливаем: он про другую монету
// и перебил бы ту, ради которой сюда пришли. Без ?coin= — всё как раньше.
const urlCoin = (new URLSearchParams(window.location.search).get("coin") || "").toUpperCase().trim();

initChart();
loadSimCoins();
if (urlCoin) {
  loadChartForCoin(urlCoin);
} else {
  restoreLastBounceSimIfAny();
}
restoreLastBulkSimIfAny();
initSimTabs();

// === ГЛАЗОК: ФОНОВЫЕ СДЕЛКИ ===
let showBackgroundZones = false;
const viewToggleBtn = document.getElementById("sim-view-toggle");
if (viewToggleBtn) {
  viewToggleBtn.onclick = () => {
    showBackgroundZones = !showBackgroundZones;
    viewToggleBtn.textContent = showBackgroundZones ? "👁️ Фон вкл" : "👁️ Фон выкл";
    if (activeTrade) drawTradeDetail(activeTrade);
  };
}

// Рисует бледные зоны сделок и собирает массив их маркеров (вход, старт, выход)
function drawBackgroundTradesAndGetMarkers() {
  if (!showBackgroundZones || !activeTrade) return [];
  
  // 🔥 БРОНЯ: Строго маршрутизируем по флагу источника, без "угадывания"
  const currentTrades = (activeTradeSource === "bulk") 
    ? bulkTrades.filter(t => t.coin === selectedCoin) 
    : simTrades;
    
  let bgMarkers = [];

  currentTrades.forEach(bgTrade => {
    if (bgTrade.time === activeTrade.time) return; // Активную не трогаем, она рисуется ярко

    // 1. Рисуем бледную зону сделки
    const zMin = bgTrade.level_min;
    const zMax = bgTrade.level_max;
    if (zMin && zMax) {
      const dimmedColor = bgTrade.type === "LONG" ? "#a5d7be" : "#f2b2a7";
      const startSec = bgTrade.time - (2 * 24 * 3600);
      const endSec = bgTrade.closed_at ? Math.floor(new Date(bgTrade.closed_at).getTime() / 1000) : Math.floor(Date.now() / 1000);
      const lineCandles = globalCandles.filter(c => c.time >= startSec && c.time <= endSec);
      simLevelLines.push(...addZoneBand(zMin, zMax, dimmedColor, lineCandles, "", "bg"));
    }

    // 2. Собираем маркеры этой сделки
    const entryStyle = bgTrade.type === "SHORT" ? { color: "#f2b2a7", shape: "arrowDown" } : { color: "#a5d7be", shape: "arrowUp" };
    bgMarkers.push({ time: bgTrade.time, position: bgTrade.type === "SHORT" ? "belowBar" : "aboveBar", color: entryStyle.color, shape: entryStyle.shape, text: "" });

    const group = typeof findGroupEpisodes === "function" ? findGroupEpisodes(bgTrade) : [];
    let events = [];
    group.forEach(ep => (ep.events || []).forEach(ev => { if (ev.time) events.push(ev); }));
    events.sort((a, b) => a.time - b.time);
    if (events.length > 0) bgMarkers.push({ time: events[0].time, position: "inBar", color: "#a5d7be", shape: "circle", text: "" }); // бледный старт

    const closeM = typeof buildCloseMarker === "function" ? buildCloseMarker(bgTrade) : null;
    if (closeM) {
      closeM.color = (bgTrade.result_percent ?? 0) > 0 ? "#a5d7be" : "#f2b2a7";
      bgMarkers.push(closeM);
    }
  });
  
  return bgMarkers;
}