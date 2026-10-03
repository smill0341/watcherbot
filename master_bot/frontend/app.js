const activeListEl = document.getElementById("active-list");
const watchlistListEl = document.getElementById("watchlist-list");
const activeCountEl = document.getElementById("active-count");
const watchlistCountEl = document.getElementById("watchlist-count");
const currentCoinEl = document.getElementById("current-coin");
const currentSymbolEl = document.getElementById("current-symbol");
const signalsBody = document.getElementById("signals-body");
const toggleLevelsBtn = document.getElementById("toggle-levels-btn"); 
let backgroundLevelLines = []; 
let isBackgroundLevelsShowing = false; 

// ===== Элементы для поиска =====
const showAllLevelsBtn = document.getElementById("show-all-levels-btn");
// ===== Элементы для поиска =====
const watchlistSearchEl = document.getElementById("watchlist-search");
const activeSearchEl = document.getElementById("active-search");

// Кэши для фильтрации
let watchlistCache = [];
let activeWatchersCache = [];

// Единая панель списков (Watchlist/Серый/Чёрный) — состояние объявлено
// здесь, а не рядом с функциями рендера внизу файла, потому что
// loadWatchlist() (вызывается уже в refreshAll() почти сразу при загрузке
// страницы) читает currentListMode — будь оно объявлено через `let` ниже
// по файлу, до того момента, как интерпретатор дойдёт до той строки,
// обращение попало бы в temporal dead zone (ReferenceError).
let currentListMode = "whitelist"; // "whitelist" | "greylist" | "blacklist"
let greylistCache = [];
let blacklistCache = [];

// Чистый CSS для неоновой загрузки (блокирует клики и убивает конфликты транзишенов)
const neonStyle = document.createElement('style');
neonStyle.innerHTML = `
@keyframes neonPulse {
    0% { box-shadow: 0 0 8px #00c853, inset 0 0 2px #00c853; border-color: #00c853; color: #00c853; text-shadow: none; }
    50% { box-shadow: 0 0 22px #00ffaa, inset 0 0 10px #00ffaa; border-color: #00ffaa; color: #00ffaa; text-shadow: 0 0 5px #00ffaa; }
    100% { box-shadow: 0 0 8px #00c853, inset 0 0 2px #00c853; border-color: #00c853; color: #00c853; text-shadow: none; }
}
.neon-loading, button.neon-loading:disabled {
    animation: neonPulse 1.5s ease-in-out infinite !important;
    pointer-events: none !important; /* Блокирует клики */
    background-color: rgba(0, 200, 83, 0.05) !important;
    transition: none !important; /* 🔥 УБИВАЕТ КОНФЛИКТ С :HOVER */
    transform: none !important;
    opacity: 1 !important; /* 🔥 УБИВАЕТ СЕРОСТЬ ОТ DISABLED */
}
`;
document.head.appendChild(neonStyle);

// Функция просто вешает класс
function toggleNeon(btn, isActive) {
    if (!btn) return;
    if (isActive) {
        btn.classList.add('neon-loading');
    } else {
        btn.classList.remove('neon-loading');
    }
}

// ===== Поиск в Watchlist =====
// Поиск работает в любом из трёх режимов (Watchlist/Серый/Чёрный) — какой
// сейчас выбран (currentListMode), тот массив и фильтруется, но только
// СТРОКИ перерисовываются; счётчик в заголовке при этом не трогаем — он
// всегда должен показывать размер полного списка, не отфильтрованного
// (та же логика, что была для обычного Watchlist раньше).
function filterWatchlist(query) {
  if (!watchlistListEl) return;
  const q = query.toLowerCase().trim();

  if (currentListMode === "whitelist") {
    const filtered = !q ? watchlistCache : watchlistCache.filter(item => item.coin.toLowerCase().includes(q));
    renderWatchlistFiltered(filtered);
  } else if (currentListMode === "greylist") {
    const filtered = !q ? greylistCache : greylistCache.filter(c => (c.coin || "").toLowerCase().includes(q));
    renderGreylistRows(filtered);
  } else {
    const filtered = !q ? blacklistCache : blacklistCache.filter(coin => (coin || "").toLowerCase().includes(q));
    renderBlacklistRows(filtered);
  }
}

// ===== Поиск + закладки стратегий в Active Watchers =====
// Закладки (см. renderActiveStrategyTabs ниже) — чисто фронтовый фильтр,
// activeWatchersCache и так весь целиком лежит в памяти (см. loadActiveWatchers),
// backend трогать незачем: поле w.strategy уже есть в каждой записи.
const STRATEGY_TAB_DEFS = [
  { tag: "VB", strategy: "V_BOTTOM" },
  { tag: "VGB", strategy: "V_GREEN_BOTTOM" },
  { tag: "VRT", strategy: "V_RED_TOP" },
  { tag: "BO", strategy: "BOUNCE" },
  { tag: "H", strategy: "VOL_SPIKE", fullName: "Ручные (объёмные триггеры)" },
];
// null = ни одна закладка не выбрана -> показываем все стратегии разом
// (по решению Jack закладки "Все" нет — повторный клик по активной снимает
// фильтр сам).
let activeStrategyTab = null;

function filterActiveWatchers(query) {
  if (!activeListEl) return;
  const q = (query || "").toLowerCase().trim();

  let filtered = activeWatchersCache;
  if (activeStrategyTab) {
    filtered = filtered.filter((w) => w.strategy === activeStrategyTab);
  }
  if (q) {
    filtered = filtered.filter((w) => (w.coin || "").toLowerCase().includes(q));
  }

  renderActiveWatchersFiltered(filtered);
}

// Рисует ряд закладок под полем поиска "В работе" — по одной на стратегию
// (плюс "Ручные" для VOL_SPIKE), с цветной точкой (тот же STRATEGY_COLORS,
// что и у самих строк списка) и счётчиком текущих вотчеров этой стратегии.
// Зовётся при каждой загрузке activeWatchersCache (см. loadActiveWatchers),
// чтобы счётчики не отставали от реальности.
function renderActiveStrategyTabs() {
  const el = document.getElementById("active-strategy-tabs");
  if (!el) return;
  el.innerHTML = STRATEGY_TAB_DEFS.map(({ tag, strategy, fullName }) => {
    const count = activeWatchersCache.filter((w) => w.strategy === strategy).length;
    const color = STRATEGY_COLORS[strategy] || "#8a8f98";
    const isActive = activeStrategyTab === strategy;
    const titleText = (fullName || tag) + (count ? ` — ${count}` : "");
    return `<span class="strategy-tab${isActive ? " active" : ""}" data-strategy="${strategy}" title="${titleText}">
      <span class="st-dot" style="background:${color};"></span>${tag}${count ? ` <span class="st-count">${count}</span>` : ""}
    </span>`;
  }).join("");
  el.querySelectorAll(".strategy-tab").forEach((chip) => {
    chip.onclick = () => {
      const strategy = chip.dataset.strategy;
      activeStrategyTab = activeStrategyTab === strategy ? null : strategy;
      renderActiveStrategyTabs();
      filterActiveWatchers(activeSearchEl ? activeSearchEl.value : "");
    };
  });
}

// ===== Список монет (watchlist) — ЕДИНСТВЕННАЯ функция отрисовки =====
// Используется и при загрузке (loadWatchlist), и при поиске (filterWatchlist).
// Порядок монет решает бэкенд (/api/watchlist::sort_by_priority: избранные
// -> с ручными зонами -> ручные без зон -> остальные по алфавиту) — тут
// своей сортировки и подписей-заголовков секций больше нет, просто плоский
// список: звёздочка слева сама показывает избранное, факт присутствия в
// списке — что уровни есть (монеты без уровней сюда вообще не попадают).
// В строке: слева звёздочка (клик — в избранное/из избранного), справа
// маленькая рука ✋, если у монеты есть ручные зоны или она добавлена
// вручную (клик — окно ручных зон ЭТОЙ монеты: изменить/удалить).
function watchlistRowHtml(info) {
  const fav = !!info.favorite;
  const showHand = info.has_custom_levels || info.source === "MANUAL";
  const isManual = info.source === "MANUAL" ? "true" : "false";
  const frozen = !!info.ignore_auto;
  const hasOwnTpSl = !!(info.coin_tp_sl && (info.coin_tp_sl.tp != null || info.coin_tp_sl.sl != null));
  const ownTpSlTitle = hasOwnTpSl
    ? `Свой TP/SL: TP ${info.coin_tp_sl.tp != null ? info.coin_tp_sl.tp : "—"}% / SL ${info.coin_tp_sl.sl != null ? info.coin_tp_sl.sl : "—"}%`
    : "";
  return `
    <div class="list-item" data-coin="${info.coin}" style="display:flex;align-items:center;">
      <span class="fav-star" title="${fav ? "Убрать из избранного" : "В избранное"}"
            onclick="event.stopPropagation(); toggleFavorite('${info.coin}');"
            style="cursor:pointer;margin-right:6px;font-size:14px;line-height:1;color:${fav ? "#f2c14e" : "#9aa0a6"};">${fav ? "★" : "☆"}</span>
      <span style="flex-grow:1;">${info.coin}</span>
      ${hasOwnTpSl ? `<span class="own-tpsl-badge" title="${ownTpSlTitle}"
            onclick="event.stopPropagation(); openCoinTpSlModal('${info.coin}');"
            style="cursor:pointer;font-size:12px;line-height:1;margin-left:6px;">⚖️</span>` : ""}
      ${showHand ? `<span class="manual-hand" title="${frozen ? "❄️ Заморожена — торгует только по ручному, авто-уровни игнорируются" : "Ручные зоны — изменить / удалить"}"
            onclick="event.stopPropagation(); openManageZonesModal('${info.coin}', ${isManual});"
            style="cursor:pointer;font-size:13px;line-height:1;margin-left:6px;${frozen ? "filter:grayscale(1) sepia(1) hue-rotate(175deg) saturate(4) brightness(1.35);" : ""}">✋</span>` : ""}
      <span class="wl-menu-wrap" style="position:relative;margin-left:6px;">
        <span class="wl-menu-btn" title="Действия"
              onclick="event.stopPropagation(); toggleWlMenu(this);"
              style="cursor:pointer;font-size:14px;line-height:1;color:#9aa0a6;padding:0 2px;">⋮</span>
        <div class="wl-menu-dropdown" style="display:none;position:absolute;top:100%;right:0;z-index:60;background:#1e1e1e;border:1px solid #3f3f4e;border-radius:6px;margin-top:2px;min-width:170px;box-shadow:0 4px 16px rgba(0,0,0,0.5);">
          <div style="padding:7px 10px;cursor:pointer;font-size:12px;font-weight:normal;"
               onclick="event.stopPropagation(); openCoinTpSlModal('${info.coin}');">⚖️ Свой TP/SL${hasOwnTpSl ? " ✓" : ""}</div>
          <div style="padding:7px 10px;cursor:pointer;font-size:12px;font-weight:normal;"
               onclick="event.stopPropagation(); demoteWatchlistCoin('${info.coin}', 'greylist');">🔘 В серый список</div>
          <div style="padding:7px 10px;cursor:pointer;font-size:12px;font-weight:normal;color:#e5654f;"
               onclick="event.stopPropagation(); demoteWatchlistCoin('${info.coin}', 'blacklist');">⚫ В чёрный список</div>
        </div>
      </span>
    </div>`;
}

// Открыть/закрыть ⋮-меню строки Watchlist. Одновременно открыто не больше
// одного меню — закрываем остальные, а также по клику вне меню.
function toggleWlMenu(btn) {
  const dropdown = btn.nextElementSibling;
  const wasOpen = dropdown.style.display === "block";
  document.querySelectorAll(".wl-menu-dropdown").forEach(d => { d.style.display = "none"; });
  if (!wasOpen) {
    dropdown.style.display = "block";
    const closeOnOutsideClick = (e) => {
      if (!dropdown.contains(e.target)) {
        dropdown.style.display = "none";
        document.removeEventListener("click", closeOnOutsideClick);
      }
    };
    setTimeout(() => document.addEventListener("click", closeOnOutsideClick), 0);
  }
}

// ⋮ → "В серый список" / "В чёрный список" на строке боевого Watchlist.
// Монета уходит из whitelist.json, её уровни в macro_levels.json — либо
// переносятся в greylist_levels.json (серый), либо отбрасываются (чёрный).
// См. POST /api/watchlist/demote в app.py.
async function demoteWatchlistCoin(coin, action) {
  const label = action === "greylist" ? "серый список" : "чёрный список";
  if (!confirm(`Убрать ${coin} из боевого Watchlist в ${label}?`)) return;
  const ok = await _postJson("/api/watchlist/demote", { coin, action });
  if (!ok) return;
  await loadWatchlist();
  if (action === "greylist") greylistCache = [];
  else blacklistCache = [];
}

function renderWatchlistEntries(entries) {
  if (!watchlistListEl) return;

  if (entries.length === 0) {
    watchlistListEl.innerHTML = "<div class='muted'>ничего не найдено</div>";
    return;
  }

  watchlistListEl.innerHTML = entries.map(watchlistRowHtml).join("");

  watchlistListEl.querySelectorAll(".list-item").forEach((div) => {
    div.onclick = () => loadChart(div.dataset.coin);
  });
  highlightSelection();
}

// Для совместимости со старыми вызовами (filterWatchlist).
function renderWatchlistFiltered(entries) {
  renderWatchlistEntries(entries);
}

// Звёздочка: переключить избранное и перерисовать оба списка (звезда есть
// и в watchlist, и в активных ватчерах — оба завязаны на один favorites.json).
window.toggleFavorite = async function(coin) {
  try {
    const res = await fetch("/api/favorites/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ coin }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || "Не удалось изменить избранное");
      return;
    }
  } catch (e) {
    alert("Ошибка запроса: " + e.message);
    return;
  }
  // 🔥 loadGreylist() тоже — звёздочка есть и в сером списке, favorites.json
  // общий для всех списков (см. /api/favorites/toggle).
  await Promise.all([loadWatchlist(), loadActiveWatchers(), loadGreylist()]);
  // loadActiveWatchers() теперь сама вызывает filterActiveWatchers() в конце
  // (сохраняет и поиск, и закладку стратегии) — тут остаётся только watchlist.
  const q = watchlistSearchEl ? watchlistSearchEl.value : "";
  if (q) filterWatchlist(q);
};

// ===== Активные ватчеры — ЕДИНСТВЕННАЯ функция отрисовки =====
// Используется и при загрузке (loadActiveWatchers), и при поиске
// (filterActiveWatchers). Порядок решает бэкенд (/api/watchlist/active::
// sort_by_priority: избранные -> ручные -> остальные по алфавиту), как и
// в watchlist — своей сортировки тут больше нет.
function renderActiveWatchersFiltered(watchers) {
  if (!activeListEl) return;

  if (watchers.length === 0) {
    activeListEl.innerHTML = "<div class='muted'>ничего не найдено</div>";
    return;
  }

  activeListEl.innerHTML = "";

  watchers.forEach((w) => {
    const div = document.createElement("div");
    div.className = "list-item";
    div.dataset.coin = w.coin;
    const label = friendlyStrategyWithMode(w.strategy, w.mode);
    const dotColor = STRATEGY_COLORS[w.strategy] || "#8a8f98";
    const fav = !!w.favorite;
    const isVolSpike = w.strategy === "VOL_SPIKE";

    const manualBadge = w.source === 'MANUAL' ? '<span class="manual-badge" title="Добавлена вручную">✋</span>' : '';

    // 2 строки: 1-я — монета+направление, 2-я — стратегия+статус. Каждая
    // своя строка не переносится (nowrap+ellipsis), так что высота ряда
    // фиксирована (2 строки текста), а не "плавает" от длины лейбла — и
    // звезда/чекбокс/кнопки (align-self:center, все одинаково) центрируются
    // относительно ЭТОЙ фиксированной высоты одинаково, без разъезда.
    div.innerHTML = `
      <span class="fav-star" title="${fav ? "Убрать из избранного" : "В избранное"}"
            onclick="event.stopPropagation(); toggleFavorite('${w.coin}');"
            style="cursor:pointer;margin-right:5px;font-size:13px;line-height:1;color:${fav ? "#f2c14e" : "#9aa0a6"};align-self:center;flex-shrink:0;">${fav ? "★" : "☆"}</span>
      <input type="checkbox" class="watcher-checkbox" data-level-id="${w.level_id}" style="cursor: pointer; margin-right: 6px; align-self:center; flex-shrink:0;">
      <div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; align-self:center;">
        <div style="font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
          <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${dotColor};margin-right:5px;vertical-align: middle;"></span>
          ${w.coin ?? "?"} <span class="dir-${w.direction}">${w.direction ?? ""}</span>${manualBadge}
        </div>
        <div style="font-size: 10px; color: #8a8f98; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
          ${label} · <span class="state-tag" style="padding: 0 5px;">${w.state}</span>
        </div>
      </div>
      <button title="Открыть монету в симуляторе (история, прошлые сделки)" onclick="openInSimulator(event, '${w.coin}')" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 13px; margin-left: 4px; align-self: center; flex-shrink:0;">📈</button>
      ${isVolSpike ? '' : `<button title="Лог этого вотчера — что видит код (скан/пропуски/входы), отдельно от других уровней монеты" onclick="openWatcherLogModal(event, '${w.level_id}', '${w.coin}')" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 13px; margin-left: 2px; align-self: center; flex-shrink:0;">📜</button>`}
      ${isVolSpike ? '' : `<button title="Точечный рескан ТОЛЬКО этого уровня, от его activated_at — соседей не трогает" onclick="triggerSingleWatcherRescan(event, '${w.coin}', '${w.level_id}')" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 13px; margin-left: 2px; align-self: center; flex-shrink:0;">🎯</button>`}
    `;
    // Полная строка (монета/направление/режим/статус) в title — если текст
    // где-то обрежется многоточием, при наведении видно целиком; если есть
    // ещё и лог истории, добавляем его следующей строкой.
    const fullLine = `${w.coin ?? "?"} ${w.direction ?? ""} · ${label} · ${w.state}`;
    div.title = w.history_log ? `${fullLine}\n${w.history_log}` : fullLine;
    div.onclick = (e) => {
      // Не открывать график если кликнули на checkbox
      if (e.target.classList.contains("watcher-checkbox")) return;

      // 🎯 VOL_SPIKE — не зона, нечего фокусировать на графике, просто
      // открываем монету как есть (без level_id/min/max — их и нет).
      if (isVolSpike) {
        loadChart(w.coin);
        return;
      }

            loadChart(w.coin, {
        level_id: w.level_id,
        min: w.level_min,
        max: w.level_max,
        strategy: w.strategy,
        mode: w.mode,
        level_date: w.level_date,      // дата пика/экстремума в истории (для легенды)
        level_type: w.level_type,      // тип зоны (PDH/PDL/MACRO/...) для легенды
        level_score: w.level_score,    // сила уровня для легенды
        level_reaction_count: w.level_reaction_count, // касания зоны для легенды
        direction: w.direction,        // LONG/SHORT — влияет на цвет зоны и Target/Stop
        activated_at: w.activated_at,  // дата технической активации — от неё рисуется линия
      });
    };
    activeListEl.appendChild(div);
  });
}

// ===== Добавить после инициализации =====
if (watchlistSearchEl) {
  watchlistSearchEl.addEventListener("input", (e) => {
    filterWatchlist(e.target.value);
  });
}

if (activeSearchEl) {
  activeSearchEl.addEventListener("input", (e) => {
    filterActiveWatchers(e.target.value);
  });
}

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema20Series = null;
let ema50Series = null;
let emaMacroSeries = null; 
let rsiSeries = null; 
let levelLines = []; 

// hex "#rrggbb" -> "rrggbbAA" (альфа для CSS-цвета заливки)
function hexWithAlpha(hex, alphaHex) {
  return hex.replace("#", "") + alphaHex;
}

// Рисует ОДНУ зону как закрашенную полосу между min и max (перенесено из
// sim.js — тот же приём: Baseline-серия, база = низ зоны, данные = верх
// зоны, плюс отдельная сплошная линия по низу, т.к. Baseline даёт границу
// только сверху). Возвращает обе серии — обе кладём в levelLines.
function addZoneBand(zMin, zMax, color, candles, titleText) {
  // Для глобальных таймфреймов (1W, 1M) - рисуем одну линию по центру
  if (currentTimeframe === "1w" || currentTimeframe === "1M") {
    const centerPrice = (zMin + zMax) / 2;
    const singleLine = chart.addLineSeries({
      color: color, lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
      title: titleText || "", autoscaleInfoProvider: () => null,
    });
    singleLine.setData(candles.map((c) => ({ time: c.time, value: centerPrice })));
    return [singleLine];
  }

  // Стандартная заливка для всех остальных ТФ (твой оригинальный код)
  const bandSeries = chart.addBaselineSeries({
    baseValue: { type: "price", price: zMin },
    topFillColor1: "#" + hexWithAlpha(color, "40"),
    topFillColor2: "#" + hexWithAlpha(color, "15"),
    topLineColor: color,
    bottomFillColor1: "rgba(0,0,0,0)",
    bottomFillColor2: "rgba(0,0,0,0)",
    bottomLineColor: "rgba(0,0,0,0)",
    lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    title: titleText || "", autoscaleInfoProvider: () => null,
  });
  bandSeries.setData(candles.map((c) => ({ time: c.time, value: zMax })));

  const bottomLine = chart.addLineSeries({
    color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid,
    crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    autoscaleInfoProvider: () => null,
  });
  bottomLine.setData(candles.map((c) => ({ time: c.time, value: zMin })));

  return [bandSeries, bottomLine];
}

// Новая функция для разделения уровня на "тонкую линию истории" и "цветную боевую зону"
function addSplitLevel(zMin, zMax, color, candles, birthTime, zoneStartTime, titleText) {
  const result = [];
  const centerPrice = (zMin + zMax) / 2;

  // Защита от кривого времени: зона не может начаться раньше рождения
  if (zoneStartTime < birthTime) zoneStartTime = birthTime;

  // 1. Тонкая линия истории (от даты рождения до старта активности)
  const historyCandles = candles.filter(c => c.time >= birthTime && c.time <= zoneStartTime);
  if (historyCandles.length > 0) {
    const historyLine = chart.addLineSeries({
      color: color,
      lineWidth: 1, // Тонкая сплошная линия
      lineStyle: LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: titleText + " (исток)",
      autoscaleInfoProvider: () => null,
    });
    historyLine.setData(historyCandles.map(c => ({ time: c.time, value: centerPrice })));
    result.push(historyLine);
  }

  // 2. Активная зона (заливка)
  const activeCandles = candles.filter(c => c.time >= zoneStartTime);
  if (activeCandles.length > 0) {
    result.push(...addZoneBand(zMin, zMax, color, activeCandles, titleText));
  }

  return result;
}

let selectedCoin = null;
let focusedLevel = null;
let isSignalView = false;
let currentSignal = null; // сигнал, по которому сейчас открыт график (для точки закрытия сделки в loadEvents)

const STRATEGY_COLORS = {
  V_BOTTOM: "#4caf7d",
  V_GREEN_BOTTOM: "#26a69a",
  V_RED_TOP: "#ff9800",
  BOUNCE: "#29b6f6",
  VOL_SPIKE: "#f2c14e",
};

// Стиль маркеров событий вотчера на графике (loadEvents). Та же самая
// причина, что была у loadLevels — тело/константа потерялись при более
// ранней правке файла, вызов остался. Восстановлено ОТСЮДА в sim.js
// (там прямым текстом в комментарии: "ДУБЛИРОВАНО ИЗ app.js::
// EVENT_MARKER_STYLE") — значит это и есть оригинал, не гадаю.
const EVENT_MARKER_STYLE = {
  // ENTRY стиль задаётся отдельно (зависит от LONG/SHORT) — см. entryStyle
  // в loadEvents ниже, сюда не входит.
  CANCEL:             { color: "#5c6370", shape: "square" },
  DEAD:               { color: "#e5654f", shape: "square" },
  SWEEP_BOTTOM:       { color: "#9c27b0", shape: "circle" },  // фиолетовый - прокол/уход до 50%
  RUNAWAY:            { color: "#e5654f", shape: "circle" },
  CLIMAX_NEAR_BREACH: { color: "#5aa9e6", shape: "circle" },
  CLIMAX_FAR_BREACH:  { color: "#5aa9e6", shape: "circle" },
  ZONE_TOUCH:         { color: "#5aa9e6", shape: "circle" },  // синий - касание уровня
  SCAN:               { color: "#f2c14e", shape: "circle" },
  // V_BOTTOM (v_bottom_watcher.py::_record_event) — ORIENTIR/START это
  // самое первое движение под уровнем, визуально то же, что ZONE_TOUCH/
  // CLIMAX выше. PEAK (Пик 1, Пик 2, Пик 3...) — жёлтый, тот же, что SCAN
  // у BOUNCE (не пересекаются на одном графике). ДУБЛИРОВАНО ИЗ
  // sim.js::EVENT_MARKER_STYLE — держать в синхроне при правках.
  ORIENTIR:           { color: "#5aa9e6", shape: "circle" },
  START:              { color: "#5aa9e6", shape: "circle" },
  PEAK:               { color: "#f2c14e", shape: "circle" },
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


// Глубину истории теперь целиком задаёт candle_store.BACKFILL_DAYS_MAP на
// бэкенде (и держит её постоянной cleanup_old() — скользящее окно). Фронт
// запрашивает /api/ohlcv без limit и просто получает всё, что есть в базе.
const savedState = JSON.parse(sessionStorage.getItem("watcher_state") || "null");
let currentTimeframe = savedState ? (savedState.tf || "15m") : "15m";

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

// ── Индикаторы под графиком (только отображение, к торговле не привязаны) ──
const INDICATOR_DEFS = [
  { key: "supertrend", label: "Supertrend", color: "#26a69a", hint: "Supertrend (14, 3): зелёная линия — тренд вверх, красная — вниз" },
  { key: "adx",        label: "ADX",        color: "#8e24aa", hint: "ADX (14): сила тренда. <20 — флэт, >25 — тренд. Показывается отдельной панелью под графиком" },
  { key: "hma",        label: "HMA",        color: "#ff9800", hint: "Hull MA (21): быстрая сглаженная средняя" },
  { key: "vwap",       label: "VWAP",       color: "#2962ff", hint: "VWAP: средневзвешенная по объёму цена, сбрасывается каждый день (UTC)" },
  { key: "env",        label: "UB/LB",      color: "#ab47bc", hint: "Envelope: SMA(20) ± 1.4% — верхняя/нижняя граница" },
];
// EMA — тоже кнопки в этой панели (по умолчанию включены, как было раньше).
const EMA_DEFS = [
  { key: "ema20",  label: "EMA20",       color: "#f2c14e", hint: "EMA 20" },
  { key: "ema50",  label: "EMA50",       color: "#5aa9e6", hint: "EMA 50" },
  { key: "ema200", label: "EMA200 (4H)", color: "#7e57c2", hint: "EMA 200 по 4-часовым свечам" },
];
let indicatorState = {};
try { indicatorState = JSON.parse(localStorage.getItem("chart_indicators") || "{}") || {}; } catch (e) { indicatorState = {}; }
function isEmaOn(key) { return indicatorState[key] !== false; }  // не задано = включено
let indicatorBarCreated = false;
let indicatorCache = { key: null, data: null };
let indSeries = {};

// ── 🧪 Методы уровней (ТЕСТ): уровни отдельным методом поверх графика ──
// Только показ. В macro_levels.json ничего не пишется, бот и стратегии эти зоны не читают.
// Новый метод = ещё одна строка здесь + ветка в level_methods.py (кнопка остаётся одна).
const LEVEL_METHODS = [
  { key: "ranked", short: "Ranked S/R", label: "Ranked S/R (Zeiierman)", color: "#1ad8c2",
    hint: "Зоны от пивотов с рейтингом 0–100 (касания, объём, возраст, съеденность). S = поддержка, R = сопротивление, цифра — балл" },
  { key: "profile", short: "Buyers/Sellers", label: "Buyers & Sellers Profile (Zeiierman)", color: "#1fc774",
    hint: "Линии: пивот, сдвинутый к ближайшему узлу профиля объёма (600 свечей), балл 0–100 (узел, поглощение, объём, хвост). Живёт до закрытия за линией" },
  { key: "matrix", short: "Prem/Disc Matrix", label: "Premium/Discount Matrix (BOSWaves)", color: "#ff0066",
    hint: "Карта текущего диапазона: последний свинг-хай/лоу, середина EQ. Выше — premium (зона шортов), ниже — discount (зона лонгов). Полосы — строки диапазона, где копятся пивоты; цифра — оценка" },
];
let levelMethodState = { methods: {}, topN: 8 };
try {
  const _s = JSON.parse(localStorage.getItem("chart_level_methods") || "{}") || {};
  if (_s.methods && typeof _s.methods === "object") levelMethodState.methods = _s.methods;
  if (typeof _s.topN === "number") levelMethodState.topN = _s.topN;
} catch (e) {}
let methodSeries = [];
let methodCache = {};
let methodRefreshSeq = 0;
function saveLevelMethodState() {
  try { localStorage.setItem("chart_level_methods", JSON.stringify(levelMethodState)); } catch (e) {}
}

function ensureIndicatorBar() {
  if (indicatorBarCreated) return;
  const chartBox = document.getElementById("chart");
  if (!chartBox) return;
  const bar = document.createElement("div");
  bar.id = "indicator-bar";
  bar.className = "indicator-bar";
  const mkBtn = (d, on, isEma) =>
    `<button type="button" class="ind-btn${on ? " active" : ""}" data-ind="${d.key}" data-ema="${isEma ? 1 : 0}" title="${d.hint}" style="--ind-color:${d.color}"><i></i>${d.label}</button>`;
  bar.innerHTML = '<span class="indicator-bar-title">EMA:</span>' + EMA_DEFS.map((d) => mkBtn(d, isEmaOn(d.key), true)).join("")
    + '<span class="indicator-bar-title" style="margin-left:8px;">Индикаторы:</span>' + INDICATOR_DEFS.map((d) => mkBtn(d, !!indicatorState[d.key], false)).join("")
    + '<span class="indicator-bar-title" style="margin-left:8px;">🧪 Уровни:</span>'
    + LEVEL_METHODS.map((m) => `<button type="button" class="ind-btn${levelMethodState.methods[m.key] ? " active" : ""}" data-method="${m.key}" title="${m.label}. ${m.hint}" style="--ind-color:${m.color}"><i></i>${m.short}</button>`).join("")
    + '<select id="lm-topn" title="Сколько лучших зон показывать" style="margin-left:4px;background:#1b1d24;color:#9aa0ac;border:1px solid #262832;border-radius:5px;font-size:12px;padding:3px 4px;">'
    + [5, 8, 15, 30, 0].map((v) => `<option value="${v}"${levelMethodState.topN === v ? " selected" : ""}>${v === 0 ? "все" : "топ " + v}</option>`).join("")
    + '</select>';
  chartBox.parentElement.insertBefore(bar, chartBox.nextSibling);
  bar.querySelectorAll(".ind-btn[data-ind]").forEach((btn) => {
    btn.onclick = () => {
      const k = btn.dataset.ind;
      if (btn.dataset.ema === "1") {
        indicatorState[k] = !isEmaOn(k);
        btn.classList.toggle("active", isEmaOn(k));
        applyEmaVisibility();
      } else {
        indicatorState[k] = !indicatorState[k];
        btn.classList.toggle("active", !!indicatorState[k]);
      }
      try { localStorage.setItem("chart_indicators", JSON.stringify(indicatorState)); } catch (e) {}
      if (btn.dataset.ema !== "1") refreshIndicators();
    };
  });
  applyEmaVisibility();
  bar.querySelectorAll(".ind-btn[data-method]").forEach((btn) => {
    btn.onclick = () => {
      const k = btn.dataset.method;
      levelMethodState.methods[k] = !levelMethodState.methods[k];
      btn.classList.toggle("active", !!levelMethodState.methods[k]);
      saveLevelMethodState(); refreshLevelMethods();
    };
  });
  document.getElementById("lm-topn").onchange = (e) => {
    levelMethodState.topN = parseInt(e.target.value, 10) || 0;
    saveLevelMethodState(); refreshLevelMethods();
  };
  indicatorBarCreated = true;
}

function applyEmaVisibility() {
  if (ema20Series) ema20Series.applyOptions({ visible: isEmaOn("ema20") });
  if (ema50Series) ema50Series.applyOptions({ visible: isEmaOn("ema50") });
  if (emaMacroSeries) emaMacroSeries.applyOptions({ visible: isEmaOn("ema200") });
}

// ── Панель ADX под графиком (второй маленький график, синхронный с основным) ──
const ADX_PANEL_H = 33;
let adxChart = null;
let adxSeries = null;
let adxPanelEl = null;
let adxValueMap = new Map();
let _adxRangeSyncing = false;
let _adxXhSyncing = false;

function ensureAdxPanel() {
  if (adxChart || !chart) return;
  const chartBox = document.getElementById("chart");
  if (!chartBox) return;
  adxPanelEl = document.createElement("div");
  adxPanelEl.id = "adx-panel";
  adxPanelEl.className = "adx-panel";
  adxPanelEl.style.display = "none";
  adxPanelEl.innerHTML = '<div class="adx-panel-label">ADX (14)</div>';
  const bar = document.getElementById("indicator-bar");
  chartBox.parentElement.insertBefore(adxPanelEl, bar || chartBox.nextSibling);

  // одинаковая ширина правой шкалы — чтобы свечи стояли строго друг под другом
  chart.applyOptions({ rightPriceScale: { minimumWidth: 70 } });
  adxChart = LightweightCharts.createChart(adxPanelEl, {
    width: chartBox.clientWidth,
    height: ADX_PANEL_H,
    layout: { background: { color: "#ffffff" }, textColor: "#1b1d24" },
    grid: { vertLines: { color: "#e6e8eb" }, horzLines: { color: "#e6e8eb" } },
    rightPriceScale: { minimumWidth: 70 },
    timeScale: { visible: false },
  });
  // Заливка столбиками от нуля, цвет по зоне: <20 флэт (серый), 20–25 переход (жёлтый), >25 тренд (фиолетовый)
  adxSeries = adxChart.addHistogramSeries({
    priceLineVisible: false, lastValueVisible: true,
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    autoscaleInfoProvider: (orig) => {
      const r = orig();
      const hi = r && r.priceRange ? Math.max(r.priceRange.maxValue, 30) : 50;
      return { priceRange: { minValue: 0, maxValue: hi } };
    },
  });
  adxSeries.createPriceLine({ price: 20, color: "#6b7280", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
  adxSeries.createPriceLine({ price: 25, color: "#ff9800", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });

  // прокрутка/зум — синхронно в обе стороны (по индексам свечей; данные ADX
  // содержат все времена свечей, поэтому индексы совпадают)
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (_adxRangeSyncing || !r || !adxChart || adxPanelEl.style.display === "none") return;
    _adxRangeSyncing = true;
    try { adxChart.timeScale().setVisibleLogicalRange(r); } catch (e) {}
    _adxRangeSyncing = false;
  });
  adxChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (_adxRangeSyncing || !r) return;
    _adxRangeSyncing = true;
    try { chart.timeScale().setVisibleLogicalRange(r); } catch (e) {}
    _adxRangeSyncing = false;
  });

  // перекрестие — синхронно в обе стороны
  chart.subscribeCrosshairMove((param) => {
    if (_adxXhSyncing || !adxChart || adxPanelEl.style.display === "none") return;
    _adxXhSyncing = true;
    try {
      const v = param && param.time ? adxValueMap.get(param.time) : undefined;
      if (v !== undefined) adxChart.setCrosshairPosition(v, param.time, adxSeries);
      else adxChart.clearCrosshairPosition();
    } catch (e) {}
    _adxXhSyncing = false;
  });
  adxChart.subscribeCrosshairMove((param) => {
    if (_adxXhSyncing) return;
    _adxXhSyncing = true;
    try {
      if (param && param.time) {
        const c = globalCandles.find((x) => x.time === param.time);
        if (c) chart.setCrosshairPosition(c.close, param.time, candleSeries);
      } else {
        chart.clearCrosshairPosition();
      }
    } catch (e) {}
    _adxXhSyncing = false;
  });

  // ширина панели всегда = ширине основного графика; после любого ресайза
  // заново выравниваем видимый диапазон (иначе линия "съезжает" в сторону)
  new ResizeObserver(() => {
    if (!chartBox.clientWidth || adxPanelEl.style.display === "none") return;
    adxChart.applyOptions({ width: chartBox.clientWidth });
    const r = chart.timeScale().getVisibleLogicalRange();
    if (r) { _adxRangeSyncing = true; try { adxChart.timeScale().setVisibleLogicalRange(r); } catch (e) {} _adxRangeSyncing = false; }
  }).observe(chartBox);
}

function _showAdxPanel(adxPts) {
  ensureAdxPanel();
  if (!adxChart) return;
  adxPanelEl.style.display = "block";
  adxChart.applyOptions({ width: document.getElementById("chart").clientWidth });
  adxValueMap = new Map();
  (adxPts || []).forEach((p) => { if (p.value !== undefined) adxValueMap.set(_indTime(p.time), p.value); });
  // все времена свечей (прогрев индикатора — пустые точки), чтобы индексы совпадали с основным графиком
  adxSeries.setData(globalCandles.map((c) => {
    const v = adxValueMap.get(c.time);
    if (v === undefined) return { time: c.time };
    return { time: c.time, value: v, color: v < 20 ? "#b8bcc4" : (v <= 25 ? "#ffb74d" : "#8e24aa") };
  }));
  const _sync = () => {
    const r = chart.timeScale().getVisibleLogicalRange();
    if (r) { _adxRangeSyncing = true; try { adxChart.timeScale().setVisibleLogicalRange(r); } catch (e) {} _adxRangeSyncing = false; }
  };
  _sync();
  requestAnimationFrame(_sync);
}

function _hideAdxPanel() {
  if (adxPanelEl) adxPanelEl.style.display = "none";
}

// Точки вне окна загруженных свечей растягивают общую шкалу времени графика и
// сбивают синхронизацию с панелью ADX (например EMA200 4H тянется дальше свечей 15m).
function _clipToCandles(arr) {
  if (!globalCandles.length) return arr;
  const lo = globalCandles[0].time;
  const hi = globalCandles[globalCandles.length - 1].time;
  return arr.filter((p) => p.time >= lo && p.time <= hi);
}

function _indTime(t) { return t > 9999999999 ? Math.floor(t / 1000) : t; }
function _indPts(arr) {
  // Линии в lightweight-charts не рвутся на пустых точках, поэтому разрыв делаем
  // прозрачным цветом: отрезок рисуется цветом ПЕРВОЙ из двух точек — последнюю точку
  // перед разрывом красим в прозрачный. Заодно режем точки по окну загруженных свечей.
  const lo = globalCandles.length ? globalCandles[0].time : -Infinity;
  const hi = globalCandles.length ? globalCandles[globalCandles.length - 1].time : Infinity;
  const out = [];
  (arr || []).forEach((p) => {
    const t = _indTime(p.time);
    if (t < lo || t > hi) return;
    if (p.value === undefined) {
      if (out.length) out[out.length - 1].color = "rgba(0,0,0,0)";
    } else {
      out.push({ time: t, value: p.value });
    }
  });
  return out;
}

function _ensureIndSeries() {
  if (!chart || indSeries.created) return;
  const base = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  indSeries.stUp = chart.addLineSeries({ ...base, color: "#26a69a", lineWidth: 2, visible: false });
  indSeries.stDn = chart.addLineSeries({ ...base, color: "#ef5350", lineWidth: 2, visible: false });
  indSeries.hma = chart.addLineSeries({ ...base, color: "#ff9800", lineWidth: 2, visible: false });
  indSeries.vwap = chart.addLineSeries({ ...base, color: "#2962ff", lineWidth: 2, visible: false });
  indSeries.envUp = chart.addLineSeries({ ...base, color: "#ab47bc", lineWidth: 1, lineStyle: 2, visible: false });
  indSeries.envLo = chart.addLineSeries({ ...base, color: "#ab47bc", lineWidth: 1, lineStyle: 2, visible: false });
  indSeries.created = true;
}

function _applyIndicators(ind) {
  _ensureIndSeries();
  const on = indicatorState;
  const setS = (s, visible, arr) => {
    if (!s) return;
    if (visible && arr && arr.length) { s.setData(_indPts(arr)); s.applyOptions({ visible: true }); }
    else { s.applyOptions({ visible: false }); }
  };
  ind = ind || {};
  setS(indSeries.stUp, on.supertrend, ind.supertrend_up);
  setS(indSeries.stDn, on.supertrend, ind.supertrend_dn);
  if (on.adx && ind.adx && ind.adx.length) _showAdxPanel(ind.adx);
  else _hideAdxPanel();
  setS(indSeries.hma, on.hma, ind.hma);
  setS(indSeries.vwap, on.vwap, ind.vwap);
  setS(indSeries.envUp, on.env, ind.env_up);
  setS(indSeries.envLo, on.env, ind.env_lo);
}

function _hideAllIndicators() {
  _hideAdxPanel();
  if (!indSeries.created) return;
  ["stUp", "stDn", "hma", "vwap", "envUp", "envLo"].forEach((k) => indSeries[k].applyOptions({ visible: false }));
}

async function refreshIndicators(coin, token) {
  coin = coin || selectedCoin;
  if (!coin || !chart) return;
  if (!INDICATOR_DEFS.some((d) => indicatorState[d.key])) { _hideAllIndicators(); return; }
  const key = coin + "|" + currentTimeframe;
  if (indicatorCache.key !== key) {
    try {
      const res = await fetch(`/api/indicators/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}`);
      if (token !== undefined && token !== chartLoadToken) return;
      if (!res.ok) { _hideAllIndicators(); return; }
      const j = await res.json();
      indicatorCache = { key, data: j.indicators || {} };
    } catch (e) { _hideAllIndicators(); return; }
  }
  if (coin !== selectedCoin) return;
  _applyIndicators(indicatorCache.data);
}

// ── 🧪 Методы уровней: отрисовка зон ──
function clearMethodZones() {
  methodSeries.forEach((s) => { try { chart.removeSeries(s); } catch (e) {} });
  methodSeries = [];
}

function drawMethodZones(method, data) {
  if (!chart || !globalCandles.length) return;
  const all = (data && data.zones) || [];
  const zones = levelMethodState.topN > 0 ? all.slice(0, levelMethodState.topN) : all;
  const lo = globalCandles[0].time;
  const hi = globalCandles[globalCandles.length - 1].time;
  // линии диапазона (RANGE HIGH/LOW/EQ) — показываем всегда, независимо от "лучших N"
  ((data && data.lines) || []).forEach((ln) => {
    const a = Math.max(_indTime(ln.start_time), lo);
    const b = Math.min(_indTime(ln.end_time), hi);
    const lcs = globalCandles.filter((c) => c.time >= a && c.time <= b);
    if (!lcs.length) return;
    const isEq = ln.side === "eq";
    const col = isEq ? "#9aa0a6" : (ln.side === "resistance" ? "#ff0066" : "#00b85c");
    const sr = chart.addLineSeries({
      color: col, lineWidth: isEq ? 1 : 2,
      lineStyle: isEq ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: true,
      title: ln.name, autoscaleInfoProvider: () => null,
    });
    sr.setData(lcs.map((c) => ({ time: c.time, value: ln.price })));
    methodSeries.push(sr);
  });
  zones.forEach((z) => {
    const t0 = Math.max(_indTime(z.born_time), lo);
    const t1 = Math.min(_indTime(z.end_time), hi);
    const cs = globalCandles.filter((c) => c.time >= t0 && c.time <= t1);
    if (!cs.length) return;
    const sup = z.side === "support";
    if (z.kind === "line") {
      // метод рисует линию (не зону): толщина и прозрачность по баллу
      const lcol = sup ? "#1fc774" : "#ef4b4b";
      const lw = z.score >= 82 ? 4 : z.score >= 62 ? 3 : 2;
      const al = z.score >= 82 ? "ff" : z.score >= 62 ? "e0" : z.score >= 45 ? "b8" : "90";
      const ln = chart.addLineSeries({
        color: lcol + al, lineWidth: lw, lineStyle: LightweightCharts.LineStyle.Solid,
        crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: true,
        title: `P#${z.rank} ${sup ? "S" : "R"} ${Math.round(z.score)}`,
        autoscaleInfoProvider: () => null,
      });
      ln.setData(cs.map((c) => ({ time: c.time, value: z.mid })));
      methodSeries.push(ln);
      return;
    }
    if (z.kind === "row") {
      // строка матрицы: заливка по баллу (premium — красная, discount — зелёная, EQ — серая)
      const rcol = z.side === "resistance" ? "#ff0066" : (z.side === "support" ? "#00b85c" : "#9aa0a6");
      const a1 = Math.min(150, 24 + Math.round(z.score * 1.3)).toString(16).padStart(2, "0");
      const a2 = Math.min(90, 12 + Math.round(z.score * 0.7)).toString(16).padStart(2, "0");
      const rb = chart.addBaselineSeries({
        baseValue: { type: "price", price: z.bottom },
        topFillColor1: "#" + hexWithAlpha(rcol, a1), topFillColor2: "#" + hexWithAlpha(rcol, a2),
        topLineColor: "rgba(0,0,0,0)",
        bottomFillColor1: "rgba(0,0,0,0)", bottomFillColor2: "rgba(0,0,0,0)", bottomLineColor: "rgba(0,0,0,0)",
        lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => null,
      });
      rb.setData(cs.map((c) => ({ time: c.time, value: z.top })));
      methodSeries.push(rb);
      if (z.rank <= 5) {   // подпись только у 5 лучших строк, иначе ось превращается в кашу
        const rm = chart.addLineSeries({
          color: rcol, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
          crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: true,
          title: `M#${z.rank} ${z.side === "resistance" ? "P" : (z.side === "support" ? "D" : "EQ")} ${Math.round(z.score)}`,
          autoscaleInfoProvider: () => null,
        });
        rm.setData(cs.map((c) => ({ time: c.time, value: z.mid })));
        methodSeries.push(rm);
      }
      return;
    }
    const color = sup ? "#1ad8c2" : "#e8590c";
    const band = chart.addBaselineSeries({
      baseValue: { type: "price", price: z.bottom },
      topFillColor1: "#" + hexWithAlpha(color, "40"), topFillColor2: "#" + hexWithAlpha(color, "15"),
      topLineColor: color,
      bottomFillColor1: "rgba(0,0,0,0)", bottomFillColor2: "rgba(0,0,0,0)", bottomLineColor: "rgba(0,0,0,0)",
      lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => null,
    });
    band.setData(cs.map((c) => ({ time: c.time, value: z.top })));
    const bottomLine = chart.addLineSeries({
      color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
      autoscaleInfoProvider: () => null,
    });
    bottomLine.setData(cs.map((c) => ({ time: c.time, value: z.bottom })));
    // средняя линия с подписью на оси цены: "#ранг S/R балл"
    const mid = chart.addLineSeries({
      color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: true,
      title: `#${z.rank} ${sup ? "S" : "R"} ${Math.round(z.score)}`,
      autoscaleInfoProvider: () => null,
    });
    mid.setData(cs.map((c) => ({ time: c.time, value: (z.top + z.bottom) / 2 })));
    methodSeries.push(band, bottomLine, mid);
  });
}

async function refreshLevelMethods(coin, token) {
  coin = coin || selectedCoin;
  const seq = ++methodRefreshSeq;
  const active = LEVEL_METHODS.filter((m) => levelMethodState.methods[m.key]);
  if (!coin || !chart || !active.length) { clearMethodZones(); return; }
  const results = [];
  for (const m of active) {
    const key = coin + "|" + currentTimeframe + "|" + m.key;
    if (!methodCache[key]) {
      try {
        const res = await fetch(`/api/level_methods/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}&method=${m.key}`);
        if (!res.ok) continue;
        methodCache[key] = await res.json();
      } catch (e) { continue; }
    }
    results.push([m, methodCache[key]]);
  }
  if (seq !== methodRefreshSeq) return;
  if (token !== undefined && token !== chartLoadToken) return;
  if (coin !== selectedCoin) return;
  clearMethodZones();
  results.forEach(([m, d]) => drawMethodZones(m, d));
}

function ensureOhlcvLegend() {
  ensureIndicatorBar();
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

function setOhlcvLegend(candle, vol, rsiVal, emaTxt) {
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
  
  el.innerHTML = `O ${o} &nbsp; H ${h} &nbsp; L ${l} &nbsp; C ${c} &nbsp; Vol ${volTxt} &nbsp; <b style="color:#555b66;">RSI: ${rsiTxt}</b>${emaTxt || ""}`;
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
      level_reaction_count: w.level_reaction_count,
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
    borderVisible: true, 
    borderUpColor: "#4caf7d",   
    borderDownColor: "#e5654f", 
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
      setOhlcvLegend(null, null, null);
      return;
    }
    const emaTxt = [
      [ema20Series, "ema20", "EMA20", "#c99a1c"],
      [ema50Series, "ema50", "EMA50", "#2f80c9"],
      [emaMacroSeries, "ema200", "EMA200", "#7e57c2"],
    ].map(([ser, key, name, col]) => {
      const d = isEmaOn(key) ? param.seriesData.get(ser) : null;
      return d ? ` &nbsp; <span style="color:${col}">${name} ${d.value.toFixed(currentPrecision)}</span>` : "";
    }).join("");

    const candle = param.seriesData.get(candleSeries);
    const vol = param.seriesData.get(volumeSeries);
    const rsiData = param.seriesData.get(rsiSeries);
    const rsiVal = rsiData ? rsiData.value.toFixed(1) : null;

    setOhlcvLegend(candle, vol, rsiVal, emaTxt);
  });

  ensureIndicatorBar();

  // 14.09 (рабочая версия) это делал ручной ResizeObserver, не встроенный
  // autoSize: true у createChart — при более поздней правке ResizeObserver
  // убрали, оставили только autoSize, и график стал заметно "кривить"
  // ширину свечей (видимо, встроенный автосайз некорректно меряет момент,
  // когда в шапке появились новые элементы — TP/SL, выбор направления —
  // которых 14.09 ещё не было). Возвращаем ровно то, что было в рабочей
  // версии, тем же кодом.
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
  if (!globalCandles.length || signal.time == null) return;

  // Временные рамки сделки (для Target/Stop)
  const fromSec = signal.time;
  const toSec = signal.closed_at ? Math.floor(new Date(signal.closed_at).getTime() / 1000) : Math.floor(Date.now() / 1000);
  const lineCandles = globalCandles.filter((c) => c.time >= fromSec && c.time <= toSec);
  if (!lineCandles.length) return;
  const lineTimes = lineCandles.map((c) => c.time);

  // Функция рисования пунктирной линии
  const addLine = (price, color, title = "") => {
    if (price === null || price === undefined || Number.isNaN(Number(price))) return;
    const series = chart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: title,
      autoscaleInfoProvider: () => null,
    });
    series.setData(lineTimes.map((t) => ({ time: t, value: Number(price) })));
    signalLines.push(series);
  };

  // 🔥 Уровень (от activated_at, зелёный LONG / красный SHORT) - как в loadLevels
  if (focusedLevel && focusedLevel.min !== null && focusedLevel.max !== null) {
    const levelColor = focusedLevel.direction === "LONG" ? "#00c853" : "#ff3d3d";
    const startDate = focusedLevel.activated_at || focusedLevel.level_date;
    const levelStartSec = startDate
      ? Math.floor(new Date(startDate + "T00:00:00Z").getTime() / 1000)
      : fromSec;
    const levelCandles = globalCandles.filter((c) => c.time >= levelStartSec && c.time <= toSec);
    if (levelCandles.length) {
      const typeLabel = friendlyLevelType(focusedLevel.level_type);
      const title = `${typeLabel} · ${focusedLevel.level_date || "—"} · ${focusedLevel.level_score ?? "—"}${focusTouchesLabel(focusedLevel)}`;
      signalLines.push(...addZoneBand(focusedLevel.min, focusedLevel.max, levelColor, levelCandles, title));
    }
  }

  // Target (TP) всегда зеленый, Stop (SL) всегда красный
  addLine(signal.target, "#4caf7d", "Target");
  addLine(signal.stop, "#e5654f", "Stop");
}

const STRATEGY_LABELS = {
  V_BOTTOM: "V-Bottom",
  V_GREEN_BOTTOM: "V-Green",
  V_RED_TOP: "V-Red",
  BOUNCE: "Bounce",
  VOL_SPIKE: "Объём"
};

function friendlyStrategy(code) {
  return STRATEGY_LABELS[code] || code || "?";
}

function friendlyLevelType(type) {
  if (!type) return "уровень";
  if (type.includes("+")) {
    return type.split("+").map(part => _translateToken(part.trim())).join(" + ");
  }
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
  if (t.includes("4h_extreme_peak")) return "Пик 4h";
  if (t.includes("extreme_peak")) return "Пик 1d";
  if (t.includes("poc")) return "POC";
  if (t.includes("macro_support")) return "Macro Sup";
  if (t.includes("macro_resistance")) return "Macro Res";
  if (t.includes("confluence")) return "Конфлюэнция";

  return token.replace(/_/g, " ");
}

async function loadEvents(coin, token = chartLoadToken) {
  // Обычный график монеты (клик в Watchlist/Greylist) — только уровни, без точек вотчеров.
  // Точки пути и входа показываем, когда открыт конкретный вотчер («В работе») или сигнал.
  if (!focusedLevel && !isSignalView) { candleSeries.setMarkers([]); return; }
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

    // ЖЕСТКИЙ ФИЛЬТР: Строго 1 маркер на 1 свечу (требование движка)
    const markersMap = {};

    // Показываем маркеры событий только в обычном режиме (не сигнал)
    const sourceWatchers = isSignalView
      ? (focusedLevel && focusedLevel.events && focusedLevel.events.length ? [focusedLevel] : [])
      : (data.active || []);

    sourceWatchers.forEach((w) => {
      // При сигнале focusedLevel уже является нужным вотчером - не фильтруем
      if (!isSignalView && !matchesFocus(w)) return;
      const isShort = w.direction === "SHORT";
      const entryStyle = isShort ? { color: "#e5654f", shape: "arrowDown" } : { color: "#4caf7d", shape: "arrowUp" };
      
      let _prevEvType = null;
      (w.events || []).forEach((ev) => {
        // Подряд идущие CANCEL (старые вотчеры писали их на каждую свечу после сброса) — рисуем только первый
        const _dupCancel = ev.type === "CANCEL" && _prevEvType === "CANCEL";
        _prevEvType = ev.type;
        if (_dupCancel) return;
        if (!ev.time || !inRange(ev.time)) return;
        
        // Примагничиваем событие к точной свече
        const cIdx = findNearestCandleIndex(globalCandles, ev.time);
        const snappedTime = globalCandles[cIdx].time;

        const style = ev.type === "ENTRY" ? entryStyle : (EVENT_MARKER_STYLE[ev.type] || { color: "#cfd3da", shape: "circle" });
        
        // Если маркера на этой свече еще нет, или это важный маркер ENTRY - перезаписываем
        if (!markersMap[snappedTime] || ev.type === "ENTRY") {
          markersMap[snappedTime] = {
            time: snappedTime,
            position: ev.type === "ENTRY" && isShort ? "belowBar" : "aboveBar",
            color: style.color,
            shape: style.shape,
            text: "",
          };
        }
      });
    });

    // Точка закрытия сделки — ищем ЧЕСТНО, по факту касания TP/SL на уже
    // загруженных свечах, а не по closed_at/result_percent из signals.json.
    // Причина: 15-минутный фоновый чек (history.py::update_open_signals)
    // закрывает сделку либо по касанию TP/SL, ЛИБО по истечению
    // MAX_HOLD_DAYS — во втором случае closed_at/result_percent берутся
    // по ТЕКУЩЕЙ цене на момент истечения таймера, вообще не обязательно
    // связанной с линиями target/stop на графике (сделка могла закрыться
    // "в отвал" где-то между ними). Точка на closed_at в таком случае
    // висела в произвольном месте — не там, где линии реально пересекались
    // ценой, отсюда и жалоба "рисует, но не по target/stop".
    // Работает и для старых сигналов — нужны только entry/target/stop/
    // time/type, которые есть и в старых записях, ничего нового в
    // signals.json подкладывать не надо.
    if (isSignalView && currentSignal && currentSignal.time != null && globalCandles.length) {
      const target = Number(currentSignal.target);
      const stop = Number(currentSignal.stop);
      const hasTarget = Number.isFinite(target);
      const hasStop = Number.isFinite(stop);
      const isShortSignal = currentSignal.type === "SHORT" || (focusedLevel && focusedLevel.direction === "SHORT");

      let actualCloseTime = null;
      let actualWin = null;
      if (hasTarget || hasStop) {
        const entryIdx = findNearestCandleIndex(globalCandles, currentSignal.time);
        for (let i = entryIdx; i < globalCandles.length; i++) {
          const c = globalCandles[i];
          const hitTp = hasTarget && (isShortSignal ? c.low <= target : c.high >= target);
          const hitSl = hasStop && (isShortSignal ? c.high >= stop : c.low <= stop);
          if (hitTp || hitSl) {
            actualCloseTime = c.time;
            actualWin = hitTp;
            break;
          }
        }
      }

      if (actualCloseTime !== null && inRange(actualCloseTime)) {
        markersMap[actualCloseTime] = {
          time: actualCloseTime,
          position: "inBar",
          color: actualWin ? "#4caf7d" : "#e5654f",
          shape: "square",
          text: "",
        };
      } else if (currentSignal.closed_at) {
        // Сделка формально закрыта (например, по MAX_HOLD_DAYS), но по
        // свечам ни TP, ни SL реально не коснулась — честно показываем
        // нейтральной серой точкой на реальном closed_at, а не врём
        // зелёным/красным про несуществующее касание линии.
        const closeSec = Math.floor(new Date(currentSignal.closed_at).getTime() / 1000);
        if (inRange(closeSec)) {
          const cIdx = findNearestCandleIndex(globalCandles, closeSec);
          const snappedTime = globalCandles[cIdx].time;
          markersMap[snappedTime] = {
            time: snappedTime,
            position: "inBar",
            color: "#cfd3da",
            shape: "square",
            text: "",
          };
        }
      }
    }

    // TradingView требует массив, жестко отсортированный по времени
    const markers = Object.values(markersMap).sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  } catch (e) {
    console.error("events load failed", e);
  }
}

// Кусок подписи уровня "касаний: N" — сколько раз цена касалась ЗОНЫ
// (её реальных границ) и отбивалась, по дневкам (reaction_count, см.
// levels_builder.py::count_zone_touches). Считается для всех типов, MACRO тоже.
// То же для сработавшего/рабочего уровня: поле в вотчере/сигнале называется level_reaction_count.
function focusTouchesLabel(f) {
  if (!f) return "";
  const n = f.level_reaction_count;
  return (n === undefined || n === null) ? "" : ` · Касаний: ${n}`;
}

function levelTouchesLabel(z) {
  if (!z) return "";
  const n = z.reaction_count;
  return (n === undefined || n === null) ? "" : ` · Касаний: ${n}`;
}

// Кнопка "👁️ Уровни" в режиме обычного графика (watchlist/greylist, не фокус на вотчере
// и не сигнал): показать/скрыть авто-уровни монеты. По умолчанию показаны, выбор запоминается.
let levelsVisible = true;
try { levelsVisible = localStorage.getItem("chart_levels_visible") !== "0"; } catch (e) { levelsVisible = true; }
let lastLevelsSource = null;

function setLevelsBtnActive(on) {
  if (!toggleLevelsBtn) return;
  toggleLevelsBtn.style.background = on ? "#2a3550" : "";
  toggleLevelsBtn.style.color = on ? "#9fb4e8" : "";
  toggleLevelsBtn.style.borderColor = on ? "#3a4a70" : "";
}

async function loadLevels(coin, token = chartLoadToken, source = null) {
  if (!levelsVisible) { clearLevelLines(); return; }
  // Эта функция была ПОЛНОСТЬЮ утеряна при более раннем изменении файла —
  // вызов остался (loadChart/submitAddLevel), само тело исчезло, отсюда
  // "loadLevels is not defined". addZoneBand/addSplitLevel (см. верх
  // файла) остались нетронуты — они и есть готовые "кирпичики" для
  // рисования, просто их было НЕКОМУ вызывать. Написана заново, тем же
  // принципом, что уже проверен в симуляторе (sim.js::drawSnapshotLevels):
  // support — зелёным, resistance — красным, полоса от даты уровня, не
  // от начала графика.
  //
  // 🔭 source === "greylist" — клик по кандидату в сворачиваемой секции
  // "Серый список" (см. loadGreylist): уровни для него лежат ОТДЕЛЬНО, в
  // greylist_levels.json, а не в обычном macro_levels.json/custom_levels.json —
  // поэтому и эндпоинт другой (см. app.py::get_greylist_levels).
  try {
    const url = source === "greylist"
      ? `/api/levels/greylist/${encodeURIComponent(coin)}`
      : `/api/levels/${encodeURIComponent(coin)}`;
    const res = await fetch(url);
    if (token !== chartLoadToken) return;
    clearLevelLines();
    if (!res.ok) return; // 404 — у монеты просто нет уровней, это не ошибка

    const data = await res.json();
    if (token !== chartLoadToken) return;
    if (!globalCandles.length) return;

    const drawSide = (zones, color) => {
      (zones || []).forEach((z) => {
        if (z.min == null || z.max == null) return;
        const zoneStartSec = (z.activated_at || z.date) ? Math.floor(new Date((z.activated_at || z.date) + "T00:00:00Z").getTime() / 1000) : null;
        const lineCandles = zoneStartSec !== null
          ? globalCandles.filter((c) => c.time >= zoneStartSec)
          : globalCandles;
        if (!lineCandles.length) return;
        const typeLabel = friendlyLevelType(z.type);
        const tooltipInfo = `${typeLabel} · ${z.date || "—"} · Вес: ${z.score ?? "—"}${levelTouchesLabel(z)}`;
        levelLines.push(...addZoneBand(z.min, z.max, color, lineCandles, tooltipInfo));
      });
    };

    drawSide(data.supports, "#00c853");
    drawSide(data.resistances, "#ff3d3d");
  } catch (e) {
    console.error("levels load failed", e);
  }
}

async function loadMacroEma200(coin) {
  if (!emaMacroSeries) return;

  // Для глобальных графиков строим честную 200 EMA по родным свечам этого же таймфрейма
  if (currentTimeframe === "1d" || currentTimeframe === "1w" || currentTimeframe === "1M") {
    if (coin !== selectedCoin) return;
    emaMacroSeries.setData(computeEMA(globalCandles, 200));
    return;
  }

  // Для внутридневных графиков (15m, 1h) скачиваем 4-часовую историю как макро-тренд
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
    
    emaMacroSeries.setData(_clipToCandles(computeEMA(candles, 200)));
  } catch (e) {
    console.error("macro EMA200(4H) load failed", e);
  }
}

let chartLoadToken = 0;

// Бинарный поиск ближайшей по времени свечи (candles отсортированы по time).
function findNearestCandleIndex(candles, targetTime) {
  let lo = 0, hi = candles.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (candles[mid].time < targetTime) lo = mid + 1; else hi = mid;
  }
  return lo;
}

async function loadChart(coin, focus = null, signal = null, opts = {}) {
  const skipLiveOverlay = !!opts.skipLiveOverlay;
  if (!coin || coin === "null" || coin === "undefined") return;

  const myToken = ++chartLoadToken;
  focusedLevel = focus;
  isSignalView = !!signal;
  currentSignal = signal;

  selectedCoin = coin;
  currentCoinEl.textContent = coin;
  currentSymbolEl.textContent = "загрузка графика...";

  // 🔥 НОВОЕ: Показываем кнопку 👁️ только если передан фокус на уровень
  if (showAllLevelsBtn) {
    showAllLevelsBtn.style.display = focus ? "inline-block" : "none";
  }

  sessionStorage.setItem("watcher_state", JSON.stringify({
    coin, tf: currentTimeframe, focus, signal, opts
  }));
  highlightSelection();

  lastLevelsSource = opts.source || null;
  clearSignalLines();
  clearLevelLines();
  
  // 🔥 НОВОЕ: Сброс фоновых уровней при смене графика
  if (typeof clearBackgroundLevels === 'function') clearBackgroundLevels();
  isBackgroundLevelsShowing = false;
  if (toggleLevelsBtn) {
    // Фокус на вотчере: кнопка показывает фоновые уровни монеты (выкл. по умолчанию).
    // Обычный график монеты (не сигнал): кнопка скрывает/показывает авто-уровни (вкл. по умолчанию).
    // Просмотр сигнала: кнопки нет.
    if (focus) {
      toggleLevelsBtn.style.display = "inline-block";
      setLevelsBtnActive(false);
    } else if (!signal) {
      toggleLevelsBtn.style.display = "inline-block";
      setLevelsBtnActive(levelsVisible);
    } else {
      toggleLevelsBtn.style.display = "none";
    }
  }

  try {
    const res = await fetch(`/api/ohlcv/${encodeURIComponent(coin)}?timeframe=${currentTimeframe}`);
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
      _hideAllIndicators();
      clearMethodZones();
      clearLevelLines();
      return;
    }
    const data = await res.json();

    // Гонка при быстром переключении монеты/таймфрейма: если пока этот
    // запрос летел, пользователь успел кликнуть что-то ещё — myToken уже
    // не совпадает с chartLoadToken. Раньше это нигде не проверялось для
    // самих свечей (только для уровней/событий ниже) — устаревший, более
    // медленный ответ мог прилететь ПОСЛЕ свежего и молча перезаписать
    // правильный график чужим таймфреймом/монетой без единой ошибки.
    if (myToken !== chartLoadToken) return;

    currentSymbolEl.textContent = `${data.symbol} · ${data.timeframe}`;
    currentPrecision = data.price_precision ?? 4;

    const priceFormat = { type: "price", precision: currentPrecision, minMove: 1 / Math.pow(10, currentPrecision) };
    candleSeries.applyOptions({ priceFormat });
    ema20Series.applyOptions({ priceFormat });
    ema50Series.applyOptions({ priceFormat });
    emaMacroSeries.applyOptions({ priceFormat });

    // Бэкенд (candle_store.py) уже гарантирует уникальность и порядок —
    // PRIMARY KEY (symbol, timeframe, timestamp) физически не даёт дублей
    // попасть в базу, ORDER BY timestamp ASC отдаёт их отсортированными.
    // Досюда доходит уже чистый набор — насильно подгонять каждую свечу
    // под сетку (alignTime) и схлопывать дубли самим не нужно: если у
    // двух РАЗНЫХ свечей после такой подгонки совпадало время, одна из
    // них молча терялась (Map просто перезаписывался), а вторая сдвигалась
    // на чужое время — источник "кривого" графика, не решение проблемы.
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
    rsiSeries.setData(computeRSI(formattedCandles, 14)); 
    indicatorCache = { key: null, data: null };
    methodCache = {};
    refreshIndicators(coin, myToken);
    refreshLevelMethods(coin, myToken);

    // Клик по сигналу — не обрезаем данные, грузим ВСЮ историю как обычно,
    // просто ставим видимое окно (viewport) вокруг момента сигнала. Данные
    // за пределами окна никуда не делись — можно спокойно отскроллить/
    // отзумить и посмотреть весь график целиком.
    if (signal && signal.time && formattedCandles.length) {
      const idx = findNearestCandleIndex(formattedCandles, signal.time);
      const half = 150; // свечей до и после сигнала
      const fromIdx = Math.max(0, idx - half);
      const toIdx = Math.min(formattedCandles.length - 1, idx + half);
      chart.timeScale().setVisibleRange({
        from: formattedCandles[fromIdx].time,
        to: formattedCandles[toIdx].time,
      });
    } else {
      chart.timeScale().fitContent();
    }
    candleSeries.priceScale().applyOptions({ autoScale: true });
    if (!skipLiveOverlay) {
      if (!signal && !focusedLevel) loadLevels(coin, myToken, opts.source);
      loadEvents(coin, myToken);
      loadMacroEma200(coin);
      // Если кликнули на вотчера - рисуем только его уровень
      if (!signal && focusedLevel && focusedLevel.min != null && focusedLevel.max != null) {
        const levelColor = focusedLevel.direction === "LONG" ? "#00c853" : "#ff3d3d";
        // activated_at - дата активации уровня (когда он был впервые обнаружен)
        // level_date - дата пика/лоу (может быть очень старой)
        const startDate = focusedLevel.activated_at || focusedLevel.level_date;
        const levelStartSec = startDate
          ? Math.floor(new Date(startDate + "T00:00:00Z").getTime() / 1000)
          : null;
        const levelCandles = levelStartSec
          ? globalCandles.filter((c) => c.time >= levelStartSec)
          : globalCandles;
        if (levelCandles.length) {
          const typeLabel = friendlyLevelType(focusedLevel.level_type);
          const title = `${typeLabel} · ${focusedLevel.level_date || "—"} · ${focusedLevel.level_score ?? "—"}${focusTouchesLabel(focusedLevel)}`;
          levelLines.push(...addZoneBand(focusedLevel.min, focusedLevel.max, levelColor, levelCandles, title));
        }
      }
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

  // "without_levels" (монеты без уровней) в список не выводим — по идее
  // всё, что в рабочем списке, уже должно быть рабочим; бэкенд их всё
  // ещё присылает отдельным полем (на будущее/для диагностики), но
  // дашборд их больше не показывает.
  const withLevels = data.with_levels || [];

  // Кэш обновляем всегда, а перерисовываем список/счётчик только если
  // сейчас реально выбран режим "Watchlist" — иначе фоновое обновление
  // (см. refreshAll, раз в минуту) перетирало бы серый/чёрный список,
  // который пользователь смотрит в данный момент, данными белого.
  watchlistCache = withLevels;
  if (currentListMode === "whitelist") {
    renderCurrentList();
  }
}

let rescanStatusCache = {};
async function loadRescanStatus() {
  try {
    const res = await fetch("/api/rescan_status");
    const data = await res.json();
    
    const rebuildBtn = document.getElementById("rebuild-levels-btn");
    if (rebuildBtn) {
      const isRebuilding = data._global_rebuild && data._global_rebuild.status === "running";
      if (rescanStatusCache._global_rebuild && !isRebuilding) {
        loadWatchlist();
        alert("Пересчет уровней завершен!");
      }
      rebuildBtn.disabled = !!isRebuilding;
      toggleNeon(rebuildBtn, !!isRebuilding);
    }

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
  activeWatchersCache = data;
  renderActiveStrategyTabs(); // счётчики на закладках — всегда актуальные

  if (data.length === 0) {
    activeListEl.innerHTML = "<div class='muted'>сейчас никого нет</div>";
    return;
  }

  // Порядок уже решён на бэкенде (sort_by_priority в app.py: избранные ->
  // ручные -> остальные по алфавиту) — своей сортировки тут больше нет,
  // как и в watchlist. filterActiveWatchers (не прямой рендер) — чтобы
  // после каждой перезагрузки (периодический опрос, рескан, избранное...)
  // сохранялись и текущий текст поиска, и выбранная закладка стратегии.
  filterActiveWatchers(activeSearchEl ? activeSearchEl.value : "");
}

async function buildFocusFromLevelId(coin, levelId) {
  if (!levelId) return null;
  try {
    const res = await fetch(`/api/events/${encodeURIComponent(coin)}`);
    if (!res.ok) return null;
    const data = await res.json();
    
    // Ищем точное совпадение в history по ключу
    const histKey = `${coin}_BOUNCE_${levelId}`;
    let w = (data.history || []).find((x) => x.key === histKey);
    
    // Если не нашли в history - ищем в active строго по level_id
    if (!w) {
      w = (data.active || []).find((x) => x.level_id === levelId);
    }
    
    if (!w) return null;
    return {
      fromHistory: true,
      min: w.level_min, max: w.level_max,
      level_min: w.level_min, level_max: w.level_max,
      level_date: w.level_date, level_type: w.level_type, level_score: w.level_score, level_reaction_count: w.level_reaction_count, direction: w.direction,
      strategy: w.strategy, mode: w.mode,
      born_at: w.born_at || null,
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

// Пагинация таблицы "Последние сигналы" — страницами по SIGNALS_PAGE_SIZE,
// а не всё сразу (сигналов может быть сотни). signalsPage — 0-based.
const SIGNALS_PAGE_SIZE = 50;
let signalsPage = 0;
let signalsTotal = 0;

async function loadSignals(page = signalsPage) {
  if (!signalsBody) return;
  signalsPage = Math.max(0, page);
  const offset = signalsPage * SIGNALS_PAGE_SIZE;
  const res = await fetch(`/api/signals?limit=${SIGNALS_PAGE_SIZE}&offset=${offset}`);
  const resp = await res.json();
  const data = resp.items || [];
  signalsTotal = resp.total || 0;

  const selectAllEl = document.getElementById("signals-select-all");
  if (selectAllEl) selectAllEl.checked = false;
  renderSignalsPagination();

  if (data.length === 0) {
    signalsBody.innerHTML = "<tr><td colspan='14'>нет сигналов</td></tr>";
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
    
    // 🔥 НОВОЕ: Checkbox в начале
    tr.innerHTML = `
      <td><input type="checkbox" class="signal-checkbox" data-time="${s.time}" data-coin="${s.coin ?? ""}" data-level-id="${s.level_id ?? ""}" style="cursor: pointer;"></td>
      <td>${s.date ?? ""}</td>
      <td>${s.coin ?? ""}</td>
      <td class="dir-${s.type}">${s.type ?? ""}</td>
      <td>${s.source ?? ""}</td>
      <td>${friendlyLevelType(s.level_type)}</td>
      <td>${formatPrice(s.entry)}</td>
      <td>${formatPrice(s.target)}</td>
      <td>${formatPrice(s.stop)}</td>
      <td>${s.status ?? ""}</td>
      <td style="color:${pct > 0 ? '#4caf7d' : pct < 0 ? '#e5654f' : 'inherit'}">${pctText}</td>
      <td>${durationText}</td>
      <td>${methodText}</td>
      <td>${s.level_id ? `<button title="Лог вотчера этого сигнала — что видел код (скан/пропуски/вход), из архива, если вотчер уже умер" onclick="event.stopPropagation(); openWatcherLogModal(event, '${s.level_id}', '${s.coin ?? ""}');" style="background:none; border:none; cursor:pointer; color:#8a8f98; font-size: 13px;">📜</button>` : ""}</td>
    `;
    if (s.coin && s.time) {
      tr.style.cursor = "pointer";
      tr.title = "Открыть график в момент сигнала";
      tr.onclick = async (e) => {
        // Не открывать график если кликнули на checkbox
        if (e.target.classList.contains("signal-checkbox")) return;
        
        // Уровень и путь вотчера теперь хранятся ПРЯМО В СИГНАЛЕ (снимок на
        // момент сделки, см. history.py::save_signal). Раньше искали по
        // level_id в watcher_history.json/active_watchers.json — там одна
        // ячейка на level_id, её перезаписывает каждая следующая жизнь
        // вотчера на этой зоне и стирает очистка "Последний скан", отсюда
        // чужие кружки или вообще пустой график. Старый поиск остаётся
        // только для старых записей, где снимка ещё нет.
        const focus = (s.level_min != null && s.level_max != null)
          ? {
              fromHistory: true,
              level_id: s.level_id,
              min: s.level_min, max: s.level_max,
              level_min: s.level_min, level_max: s.level_max,
              level_date: s.level_date, level_type: s.level_type, level_score: s.level_score, level_reaction_count: s.level_reaction_count,
              // strategy раньше был жёстко "BOUNCE" — не мешало, пока только
              // у BOUNCE в сигнале были level_min/max. Теперь VB/VGB/VRT
              // (watcher_plan.py::check_v_bottom/check_v_green_bottom/
              // check_v_red_top) тоже их присылают — берём реальный
              // источник (s.source: "V_BOTTOM"/"V_GREEN_BOTTOM"/"V_RED_TOP"/
              // "BOUNCE", см. save_signal(...) в watcher_plan.py), иначе
              // зона V-семьи красилась/подписывалась бы как BOUNCE.
              direction: s.type, strategy: s.source || "BOUNCE", mode: s.mode,
              events: s.events || [],
            }
          : (s.level_id ? await buildFocusFromLevelId(s.coin, s.level_id) : null);
        loadChart(s.coin, focus, { time: s.time, entry: s.entry, target: s.target, stop: s.stop, closed_at: s.closed_at, status: s.status, result_percent: s.result_percent, type: s.type });
      };
    }
    signalsBody.appendChild(tr);
  });
}

function renderSignalsPagination() {
  const label = document.getElementById("signals-page-label");
  const prevBtn = document.getElementById("signals-prev-btn");
  const nextBtn = document.getElementById("signals-next-btn");
  const totalPages = Math.max(1, Math.ceil(signalsTotal / SIGNALS_PAGE_SIZE));
  const currentPage = signalsPage + 1;
  if (label) label.textContent = `${currentPage}/${totalPages} (${signalsTotal})`;
  if (prevBtn) prevBtn.disabled = signalsPage <= 0;
  if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
}

document.getElementById("signals-prev-btn")?.addEventListener("click", () => loadSignals(signalsPage - 1));
document.getElementById("signals-next-btn")?.addEventListener("click", () => loadSignals(signalsPage + 1));

// "Выбрать все" — только среди строк ТЕКУЩЕЙ страницы (условные 50), не
// вообще всех сигналов — удалять тоже можно только то, что видно на экране.
document.getElementById("signals-select-all")?.addEventListener("change", (e) => {
  document.querySelectorAll(".signal-checkbox").forEach((cb) => { cb.checked = e.target.checked; });
});

// 🔥 НОВОЕ: Удаление выбранных вотчеров
async function deleteSelectedWatchers() {
  const checkboxes = document.querySelectorAll(".watcher-checkbox:checked");
  if (checkboxes.length === 0) {
    alert("Выбери вотчеры для удаления");
    return;
  }
  
  if (!confirm(`Удалить ${checkboxes.length} вотчер(ов)? Это необратимо!`)) {
    return;
  }
  
  const levelIds = Array.from(checkboxes).map(cb => cb.dataset.levelId);
  try {
    const res = await fetch("/api/watchers/delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(levelIds)
    });
    
    const result = await res.json();
    let msg = `Удалено ${result.deleted} вотчер(ов)`;
    // Если среди удалённых был ручной уровень — бэк заодно стёр саму зону
    // из custom_levels.json (см. app.py::delete_watchers), иначе ближайший
    // скан-цикл пересоздал бы вотчер с тем же level_id заново.
    if (result.removed_custom_levels) {
      msg += `\nЗаодно удалено ${result.removed_custom_levels} ручных уровней (иначе вотчер бы возродился на ближайшем скане)`;
    }
    alert(msg);
    loadActiveWatchers();
  } catch (e) {
    console.error("Ошибка удаления вотчеров", e);
    alert("Ошибка при удалении");
  }
}

// Добавляем обработчик для кнопки вотчеров
document.getElementById("delete-selected-watchers-btn")?.addEventListener("click", deleteSelectedWatchers);

// 🔥 НОВОЕ: Удаление выбранных сигналов
async function deleteSelectedSignals() {
  const checkboxes = document.querySelectorAll(".signal-checkbox:checked");
  if (checkboxes.length === 0) {
    alert("Выбери сигналы для удаления");
    return;
  }
  
  if (!confirm(`Удалить ${checkboxes.length} сигнал(ов)? Это необратимо!`)) {
    return;
  }
  
  // Раньше отправляли голый time — не уникален (свечи разных монет часто
  // закрываются в одну и ту же секунду), из-за чего удаление одного
  // выделенного сигнала сносило все сигналы с тем же time, включая чужие
  // монеты. Теперь шлём составной ключ (time+coin+level_id), см.
  // web/backend/app.py::delete_signals.
  const keys = Array.from(checkboxes).map(cb => ({
    time: parseInt(cb.dataset.time),
    coin: cb.dataset.coin || null,
    level_id: cb.dataset.levelId || null,
  }));
  try {
    const res = await fetch("/api/signals/delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(keys)
    });
    
    const result = await res.json();
    alert(`Удалено ${result.deleted} сигнал(ов)`);
    // Если удалили всё, что было на последней странице — шагаем на
    // страницу назад, а не показываем пустую таблицу с рабочей кнопкой
    // "Пред".
    const remainingPages = Math.max(1, Math.ceil((signalsTotal - result.deleted) / SIGNALS_PAGE_SIZE));
    await loadSignals(Math.min(signalsPage, remainingPages - 1));
  } catch (e) {
    console.error("Ошибка удаления сигналов", e);
    alert("Ошибка при удалении");
  }
}

// Добавляем обработчик для кнопки
document.getElementById("delete-selected-signals-btn")?.addEventListener("click", deleteSelectedSignals);

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
  await Promise.all([loadWatchlist(), loadActiveWatchers(), loadSignals(), loadGlobalHistory(), loadGreylist(), loadBlacklist()]);
}

initChart();
refreshAll();
loadRescanStatus();

if (savedState && savedState.coin) {
  loadChart(savedState.coin, savedState.focus, savedState.signal, savedState.opts);
}

// === АВТООБНОВЛЕНИЕ РАЗ В МИНУТУ ===
setInterval(async () => {
  refreshAll(); 
  
  if (selectedCoin) {
    try {
      const res = await fetch(`/api/ohlcv/${encodeURIComponent(selectedCoin)}?timeframe=${currentTimeframe}&limit=10`);
      if (!res.ok) return;
      const data = await res.json();

      // Тот же принцип, что и в loadChart() — бэкенд уже отдаёт чистые,
      // уникальные, отсортированные свечи, подгонять/схлопывать самим не
      // нужно. Здесь это даже важнее: candleSeries.update() требует строго
      // неубывающее время — если бы подгонка сдвинула свечу не туда, это
      // могло бы испортить уже нарисованную серию прямо во время просмотра.
      const formattedCandles = data.candles
        .map((c) => ({ ...c, time: c.time > 9999999999 ? Math.floor(c.time / 1000) : c.time }))
        .sort((a, b) => a.time - b.time);

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
    if (!confirm("Точно пересчитать ВСЕ уровни с нуля? Это дольше, может занять пару минут. Отменить нельзя.")) return;
    
    rebuildLevelsBtnEl.disabled = true;
    toggleNeon(rebuildLevelsBtnEl, true); 
    
    try {
      await fetch("/api/rebuild_levels", { method: "POST" });
    } catch (e) {
      alert("Ошибка: " + e);
      rebuildLevelsBtnEl.disabled = false;
      toggleNeon(rebuildLevelsBtnEl, false);
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

// Кнопка 📈 у монеты в списке "в работе" — открывает её в СИМУЛЯТОРЕ
// (новая вкладка, /simulator?coin=XXX). Заменила старый рескан монеты (🔄),
// который внутри боевого бота откатывал монету на неделю назад и гнал всё
// заново. Смотреть прошлое — задача симулятора: у него своя история
// уровней, свой изолированный менеджер, боевое состояние он не трогает.
// Точечный рескан вотчера (🎯) остаётся как был.
window.openInSimulator = function(event, coin) {
  event.stopPropagation();
  window.open(`/simulator?coin=${encodeURIComponent(coin)}`, "_blank");
};

// Точечный рескан ОДНОГО вотчера (см. watcher_plan.py::rescan_single_bounce_watcher) —
// ничего не спрашивает (дата берётся из
// activated_at самого вотчера) и не трогает соседние вотчеры той же монеты.
// Синхронный запрос — результат приходит сразу в ответе, без флага/поллинга.
window.triggerSingleWatcherRescan = async function(event, coin, levelId) {
  event.stopPropagation();
  const btn = event.currentTarget;
  const originalText = btn.textContent;
  btn.textContent = "⏳";
  
  toggleNeon(btn, true); 
  
  try {
    const res = await fetch(`/api/rescan_watcher/${encodeURIComponent(coin)}?level_id=${encodeURIComponent(levelId)}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Ошибка точечного рескана");
      return;
    }
    const orders = data.orders || [];
    let msg = `Точечный рескан ${coin} / ${levelId}\n`;
    if (data.final_state === "NOT_TOUCHED") {
      msg += `Уровень активировался ${data.activated_at}, но цена ни разу не коснулась зоны с тех пор — вотчеру физически неоткуда было родиться.\n`;
    } else {
      msg += `Уровень активировался (activated_at): ${data.activated_at}\n`;
      msg += `Цена реально коснулась зоны: ${data.since}${data.clamped_to_available_history ? " (обрезано до доступной глубины истории!)" : ""}\n`;
      msg += `Итоговое состояние: ${data.final_state}\n`;
      msg += `Найдено сделок: ${orders.length}\n`;
      if (orders.length > 0) {
        msg += "\n" + orders.map(o =>
          `  ${o.time}  entry=${o.entry}  sl=${o.sl}  tp=${o.tp}\n  ${o.reason || ""}`
        ).join("\n\n");
      }
    }
    console.log("[SINGLE WATCHER RESCAN]", data);
    alert(msg);
    loadActiveWatchers();
    if (selectedCoin === coin) loadChart(coin, focusedLevel);
  } catch (e) {
    console.error("Ошибка точечного рескана", e);
    alert("Ошибка: " + e);
  } finally {
    btn.textContent = originalText;
    toggleNeon(btn, false); 
  }
};

// Массовый рескан ВСЕХ вотчеров из списка "в работе" — тот же
// /api/rescan_watcher по каждому, но одним запросом на бэке (последовательно,
// см. докстринг /api/rescan_all_watchers в app.py) и с ОДНИМ итоговым алертом
// вместо диалога на каждую монету. Кнопка #rescan-all-btn — в index.html,
// рядом с #reset-watchers-btn/#delete-selected-watchers-btn.
const rescanAllBtnEl = document.getElementById("rescan-all-btn");
if (rescanAllBtnEl) {
  rescanAllBtnEl.onclick = async () => {
    toggleNeon(rescanAllBtnEl, true); 
    try {
      const res = await fetch("/api/rescan_all_watchers", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data.detail || "Ошибка массового рескана");
        return;
      }
      console.log("[RESCAN ALL WATCHERS]", data);
      alert(data.summary || `Проверено: ${data.total}, мёртвых: ${(data.newly_dead || []).length}, активных: ${data.still_active}`);
      loadActiveWatchers();
      if (selectedCoin) loadChart(selectedCoin, focusedLevel);
    } catch (e) {
      console.error("Ошибка массового рескана", e);
      alert("Ошибка: " + e);
    } finally {
      toggleNeon(rescanAllBtnEl, false); 
    }
  };
}

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

// // === LONG/SHORT переключатель BOUNCE (#direction-select в index.html) ===
async function refreshDirectionButtons() {
  const selectEl = document.getElementById("direction-select");
  if (!selectEl) return;
  try {
    const res = await fetch("/api/bounce_direction");
    const state = await res.json();
    
    if (state.allow_long && state.allow_short) selectEl.value = "both";
    else if (state.allow_long) selectEl.value = "long";
    else if (state.allow_short) selectEl.value = "short";
    
  } catch (e) {
    console.error("Не удалось загрузить состояние LONG/SHORT", e);
  }
}

function wireDirectionButtons() {
  const selectEl = document.getElementById("direction-select");
  if (!selectEl) return;

  selectEl.onchange = async () => {
    selectEl.disabled = true;
    const val = selectEl.value;
    
    let allowLong = false;
    let allowShort = false;
    
    if (val === "both") { allowLong = true; allowShort = true; }
    else if (val === "long") { allowLong = true; }
    else if (val === "short") { allowShort = true; }

    try {
      await fetch('/api/bounce_direction/set', {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allow_long: allowLong, allow_short: allowShort })
      });
      await refreshDirectionButtons();
    } catch (e) {
      console.error("Ошибка переключения направления", e);
    } finally {
      selectEl.disabled = false;
    }
  };

  refreshDirectionButtons();
}

if (isMainDashboardPage()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireDirectionButtons);
  } else {
    wireDirectionButtons();
  }
}

// === Ручное добавление уровня (#add-level-btn / #add-level-overlay в index.html) ===
function openAddLevelModal() {
  const overlay = document.getElementById("add-level-overlay");
  const errEl = document.getElementById("add-level-error");
  if (!overlay) return;
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  const coinInput = document.getElementById("add-level-coin");
  // В Blacklist поле "Монета" не подставляем из selectedCoin — обычно
  // блэклистят монету ИЗ другого списка (грей/белый), а не открытую сейчас.
  if (coinInput) coinInput.value = (currentListMode !== "blacklist" && selectedCoin) ? selectedCoin : "";

  const ignoreAutoCb = document.getElementById("add-level-ignore-auto");
  if (ignoreAutoCb) ignoreAutoCb.checked = false;

  const sideSelect = document.getElementById("add-level-side");
  const sideField = document.getElementById("add-level-side-field");
  if (sideSelect) {
    if (currentListMode === "blacklist") {
      // В чёрном списке уровням/объёму взяться неоткуда — только тикер.
      sideSelect.value = "add_only";
      toggleAddLevelFields("add_only");
      if (sideField) sideField.style.display = "none";
    } else {
      if (sideField) sideField.style.display = "";
      sideSelect.value = "support";
      toggleAddLevelFields("support");
    }
  }
  overlay.style.display = "flex";
}

function closeAddLevelModal() {
  const overlay = document.getElementById("add-level-overlay");
  if (overlay) overlay.style.display = "none";
}

function toggleAddLevelFields(side) {
  const priceGroup = document.getElementById("price-fields-group");
  const volGroup = document.getElementById("volume-fields-group");
  const ignoreAutoField = document.getElementById("add-level-ignore-auto-field");
  if (!priceGroup || !volGroup) return;
  if (side === "volume") {
    priceGroup.style.display = "none";
    volGroup.style.display = "block";
  } else if (side === "add_only") {
    // "Просто добавить монету" — ни зона, ни объёмный триггер не нужны, и
    // нечего игнорировать — галочка заморозки тут смысла не имеет.
    priceGroup.style.display = "none";
    volGroup.style.display = "none";
  } else {
    priceGroup.style.display = "block";
    volGroup.style.display = "none";
  }
  // ❄️ Галочка "игнорировать авто" — только там, где реально добавляется
  // ручной уровень/объём (support/resistance/volume), не для add_only.
  if (ignoreAutoField) ignoreAutoField.style.display = side === "add_only" ? "none" : "";
}

async function submitAddLevel() {
  const errEl = document.getElementById("add-level-error");
  const showError = (msg) => {
    if (errEl) { errEl.textContent = msg; errEl.style.display = "block"; }
  };

  const coin = (document.getElementById("add-level-coin").value || "").trim().toUpperCase();
  const side = document.getElementById("add-level-side").value;

  if (!coin) return showError("Укажи монету");

  const submitBtn = document.getElementById("add-level-submit-btn");

  // "Просто добавить монету" — ни зона, ни триггер, просто тикер в текущий
  // список (Watchlist/Greylist/Blacklist). Свой, отдельный от /api/levels/
  // custom эндпоинт на каждый список (см. app.py: add_to_whitelist/
  // add_to_greylist/add_to_blacklist).
  if (side === "add_only") {
    const url = currentListMode === "blacklist" ? "/api/blacklist/add"
      : currentListMode === "greylist" ? "/api/greylist/add"
      : "/api/whitelist/add";
    submitBtn.disabled = true;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coin }),
      });
      const data = await res.json();
      if (!res.ok) { showError(data.detail || "Не удалось сохранить"); return; }
      closeAddLevelModal();
      if (currentListMode === "blacklist") await loadBlacklist();
      else if (currentListMode === "greylist") await loadGreylist();
      else await loadWatchlist();
    } catch (e) {
      showError("Ошибка запроса: " + e.message);
    } finally {
      submitBtn.disabled = false;
    }
    return;
  }

  const ignoreAutoCb = document.getElementById("add-level-ignore-auto");
  let body = { coin, side, ignore_auto: !!(ignoreAutoCb && ignoreAutoCb.checked) };

  if (side === "volume") {
    const targetVol = parseFloat(document.getElementById("add-level-vol-target").value);
    if (!Number.isFinite(targetVol) || targetVol <= 0) return showError("Укажи корректный целевой объем (> 0)");
    body.target_volume = targetVol;
  } else {
    const minVal = parseFloat(document.getElementById("add-level-min").value);
    const maxVal = parseFloat(document.getElementById("add-level-max").value);
    const scoreRaw = document.getElementById("add-level-score").value;
    const score = scoreRaw ? parseFloat(scoreRaw) : undefined;

    if (!Number.isFinite(minVal) || !Number.isFinite(maxVal)) return showError("min/max должны быть числами");
    if (minVal >= maxVal) return showError("min должен быть меньше max");
    body.min = minVal;
    body.max = maxVal;
    if (score !== undefined) body.score = score;
  }

  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/levels/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || "Не удалось сохранить");
      return;
    }
    closeAddLevelModal();
    await loadWatchlist();
    // Ручная зона могла быть добавлена и монете из серого списка (см.
    // build_macro_levels — переводит её на боевой рельс) — обновляем и
    // грей-кэш, иначе ✋ на строке не появится без полной перезагрузки.
    if (currentListMode === "greylist") await loadGreylist();
    if (selectedCoin === coin && side !== "volume") {
      await loadLevels(coin);
    }
  } catch (e) {
    showError("Ошибка запроса: " + e.message);
  } finally {
    submitBtn.disabled = false;
  }
}

function wireAddLevelModal() {
  const btn = document.getElementById("add-level-btn");
  const overlay = document.getElementById("add-level-overlay");
  const cancelBtn = document.getElementById("add-level-cancel-btn");
  const submitBtn = document.getElementById("add-level-submit-btn");
  const sideSelect = document.getElementById("add-level-side");
  if (!btn || !overlay || !cancelBtn || !submitBtn) return;

  btn.onclick = openAddLevelModal;
  cancelBtn.onclick = closeAddLevelModal;
  submitBtn.onclick = submitAddLevel;
  // БАГ (был и до правок с объёмными триггерами): toggleAddLevelFields()
  // вызывалась только один раз при ОТКРЫТИИ модалки (openAddLevelModal,
  // всегда со значением "support") — переключение селекта на "Объем"
  // никак не слушалось, поля min/max так и оставались показанными.
  if (sideSelect) {
    sideSelect.onchange = () => toggleAddLevelFields(sideSelect.value);
  }
  // Клик по затемнённому фону (не по самой плашке) — тоже закрывает.
  overlay.onclick = (e) => {
    if (e.target === overlay) closeAddLevelModal();
  };
}

if (isMainDashboardPage()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAddLevelModal);
  } else {
    wireAddLevelModal();
  }
}
// === Управление ручными зонами (клик по ✋ в списке монет) ===
// index.html недоступен в этой сессии (не в git-репозитории), поэтому вся
// модалка строится прямо тут через JS, без готовой разметки в HTML —
// в отличие от #add-level-overlay выше, которая уже есть в index.html.
function ensureManageZonesOverlay() {
  let overlay = document.getElementById("manage-zones-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "manage-zones-overlay";
  overlay.style.cssText = "display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;align-items:center;justify-content:center;";
  overlay.innerHTML = `
    <div style="background:#1e1e1e;color:#eee;border-radius:8px;padding:16px 20px;min-width:320px;max-width:420px;max-height:70vh;overflow-y:auto;box-shadow:0 4px 24px rgba(0,0,0,0.5);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <strong id="manage-zones-title">Ручные зоны</strong>
        <span id="manage-zones-close" style="cursor:pointer;font-size:18px;line-height:1;padding:0 4px;">✕</span>
      </div>
      <div id="manage-zones-body">Загрузка...</div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.onclick = (e) => {
    if (e.target === overlay) closeManageZonesModal();
  };
  overlay.querySelector("#manage-zones-close").onclick = closeManageZonesModal;
  return overlay;
}

function closeManageZonesModal() {
  const overlay = document.getElementById("manage-zones-overlay");
  if (overlay) overlay.style.display = "none";
}

// Окно ручных зон ОДНОЙ монеты (клик по ✋ в списке монет). У каждой зоны
// ровно два действия: ✏️ изменить границы и 🗑 удалить. Внизу — добавить
// зону этой же монете. Если зон не осталось, а монета попала в список только
// из-за них (source="MANUAL") — кнопка "убрать монету из списка".
async function openManageZonesModal(coin, isManualSource = false) {
  const overlay = ensureManageZonesOverlay();
  const titleEl = overlay.querySelector("#manage-zones-title");
  const bodyEl = overlay.querySelector("#manage-zones-body");
  titleEl.textContent = `✋ Ручные зоны — ${coin}`;
  bodyEl.textContent = "Загрузка...";
  overlay.style.display = "flex";
  await renderManageZonesList(coin, bodyEl, isManualSource);
}

// Обновить всё, что зависит от ручных зон монеты: окно, список, график.
async function _afterManualZonesChanged(coin, isManualSource) {
  const bodyEl = document.getElementById("manage-zones-body");
  if (bodyEl) await renderManageZonesList(coin, bodyEl, isManualSource);
  await loadWatchlist();
  if (typeof selectedCoin !== "undefined" && selectedCoin === coin) {
    await loadLevels(coin);
  }
}

// Кнопка "с подстраховкой": первый клик взводит на 3 сек, второй — делает.
function _armedClick(btn, idleText, armedText, action) {
  btn.onclick = async () => {
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = armedText;
      setTimeout(() => {
        if (btn.dataset.armed === "1") { btn.dataset.armed = "0"; btn.textContent = idleText; }
      }, 3000);
      return;
    }
    btn.dataset.armed = "0";
    btn.textContent = "...";
    const ok = await action();
    if (!ok) btn.textContent = idleText;
  };
}

async function _postJson(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { alert(data.detail || "Ошибка"); return false; }
    return true;
  } catch (e) {
    alert("Ошибка запроса: " + e.message);
    return false;
  }
}

async function renderManageZonesList(coin, bodyEl, isManualSource = false) {
  let data;
  try {
    const res = await fetch(`/api/levels/${encodeURIComponent(coin)}`);
    data = res.ok ? await res.json() : { supports: [], resistances: [], volume_triggers: [] };
  } catch (e) {
    bodyEl.textContent = "Ошибка запроса: " + e.message;
    return;
  }
  // Ручные зоны помечены сервером как type: "MANUAL" (см. add_custom_level).
  const zones = [
    ...(data.supports || []).filter(z => z.type === "MANUAL").map(z => ({ ...z, side: "support", kind: "zone" })),
    ...(data.resistances || []).filter(z => z.type === "MANUAL").map(z => ({ ...z, side: "resistance", kind: "zone" })),
  ];
  // 🎯 Объёмные триггеры — своя, отдельная от зон сущность (нет min/max,
  // зато есть id и target), но в этом же окне должны точно так же
  // редактироваться/удаляться, а не только "добавляться и всё".
  const volTriggers = (data.volume_triggers || []).map(v => ({ ...v, kind: "volume" }));
  const items = [...zones, ...volTriggers];

  const btnStyle = "cursor:pointer;background:none;border:1px solid #444;color:#ddd;border-radius:4px;padding:2px 8px;margin-left:6px;font-size:12px;";
  let html = "";
  if (items.length === 0) {
    html += `<div style="opacity:0.8;margin:6px 0;">Ручных зон нет.</div>`;
  } else {
    html += items.map((z, i) => `
      <div class="mz-row" data-i="${i}" style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #333;">
        <span class="mz-text">${z.kind === "volume"
          ? `🔊 Объём ≥ ${formatVolume(z.target)}`
          : `${z.side === "support" ? "🟢 Поддержка" : "🔴 Сопротивление"}: ${formatPrice(z.min)} — ${formatPrice(z.max)}`}</span>
        <span style="white-space:nowrap;">
          <button class="mz-edit" style="${btnStyle}" title="Изменить">✏️</button>
          <button class="mz-del" style="${btnStyle}color:#e55;" title="Удалить">🗑</button>
        </span>
      </div>`).join("");
  }
  html += `<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
      <button id="mz-add" style="${btnStyle}margin-left:0;">+ добавить зону</button>
      ${items.length === 0 && isManualSource ? `<button id="mz-remove-coin" style="${btnStyle}margin-left:0;color:#e55;">убрать монету из списка</button>` : ""}
    </div>`;
  bodyEl.innerHTML = html;

  bodyEl.querySelectorAll(".mz-row").forEach((row) => {
    const z = items[Number(row.dataset.i)];

    // 🎯 Объёмный триггер — свои тело запроса и рендер редактирования
    // (одно поле target, а не пара min/max).
    if (z.kind === "volume") {
      _armedClick(row.querySelector(".mz-del"), "🗑", "точно?", async () => {
        const body = z.id ? { coin, side: "volume", id: z.id } : { coin, side: "volume", target_volume: z.target };
        const ok = await _postJson("/api/levels/custom/delete", body);
        if (ok) await _afterManualZonesChanged(coin, isManualSource);
        return ok;
      });

      row.querySelector(".mz-edit").onclick = () => {
        row.innerHTML = `
          <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            🔊 <input class="mz-target" type="number" step="any" value="${z.target}" style="width:140px;">
          </span>
          <span style="white-space:nowrap;">
            <button class="mz-save" style="${btnStyle}" title="Сохранить">✔</button>
            <button class="mz-cancel" style="${btnStyle}" title="Отмена">✕</button>
          </span>`;
        row.querySelector(".mz-cancel").onclick = () => renderManageZonesList(coin, bodyEl, isManualSource);
        row.querySelector(".mz-save").onclick = async () => {
          const newTarget = parseFloat(row.querySelector(".mz-target").value);
          if (!Number.isFinite(newTarget) || newTarget <= 0) { alert("Целевой объём должен быть числом больше нуля"); return; }
          const body = z.id
            ? { coin, side: "volume", id: z.id, target_volume: newTarget }
            : { coin, side: "volume", old_target: z.target, target_volume: newTarget };
          const ok = await _postJson("/api/levels/custom/update", body);
          if (ok) await _afterManualZonesChanged(coin, isManualSource);
        };
      };
      return;
    }

    _armedClick(row.querySelector(".mz-del"), "🗑", "точно?", async () => {
      const ok = await _postJson("/api/levels/custom/delete", { coin, side: z.side, min: z.min, max: z.max });
      if (ok) await _afterManualZonesChanged(coin, isManualSource);
      return ok;
    });

    row.querySelector(".mz-edit").onclick = () => {
      row.innerHTML = `
        <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          ${z.side === "support" ? "🟢" : "🔴"}
          <input class="mz-min" type="number" step="any" value="${z.min}" style="width:110px;">
          —
          <input class="mz-max" type="number" step="any" value="${z.max}" style="width:110px;">
        </span>
        <span style="white-space:nowrap;">
          <button class="mz-save" style="${btnStyle}" title="Сохранить">✔</button>
          <button class="mz-cancel" style="${btnStyle}" title="Отмена">✕</button>
        </span>`;
      row.querySelector(".mz-cancel").onclick = () => renderManageZonesList(coin, bodyEl, isManualSource);
      row.querySelector(".mz-save").onclick = async () => {
        const newMin = parseFloat(row.querySelector(".mz-min").value);
        const newMax = parseFloat(row.querySelector(".mz-max").value);
        if (!Number.isFinite(newMin) || !Number.isFinite(newMax)) { alert("min/max должны быть числами"); return; }
        if (newMin >= newMax) { alert("min должен быть меньше max"); return; }
        const ok = await _postJson("/api/levels/custom/update",
          { coin, side: z.side, old_min: z.min, old_max: z.max, min: newMin, max: newMax });
        if (ok) await _afterManualZonesChanged(coin, isManualSource);
      };
    };
  });

  const addBtn = bodyEl.querySelector("#mz-add");
  if (addBtn) addBtn.onclick = () => {
    closeManageZonesModal();
    openAddLevelModal();
    const coinInput = document.getElementById("add-level-coin");
    if (coinInput) coinInput.value = coin;
  };

  const rmBtn = bodyEl.querySelector("#mz-remove-coin");
  if (rmBtn) _armedClick(rmBtn, "убрать монету из списка", "точно убрать?", async () => {
    const ok = await _postJson("/api/watchlist/remove", { coin });
    if (ok) { closeManageZonesModal(); await loadWatchlist(); }
    return ok;
  });
}

// === Единая панель списков (одна строка "Watchlist" вместо трёх кривых
// панелей разом). Клик по слову открывает выпадающее меню (⚪ Watchlist /
// 🔘 Серый список / ⚫ Чёрный список), выбор переключает содержимое ТОГО
// ЖЕ #watchlist-list на выбранный список — один список монет на экране,
// та же вёрстка (h2+input+list как прямые дети .panel), что и была
// изначально, поэтому скролл/отступы снова работают как у остальных
// панелей ("В работе" и т.д.), а не через отдельную обёртку, которая их
// ломала. currentListMode/greylistCache/blacklistCache объявлены наверху
// файла — см. комментарий там.

const LIST_MODE_LABELS = {
  whitelist: "Watchlist",
  greylist: "Greylist",
  blacklist: "Blacklist",
};

function applyListModeUI() {
  const textEl = document.getElementById("list-mode-text");
  if (textEl) textEl.textContent = LIST_MODE_LABELS[currentListMode];

  const addLevelBtn = document.getElementById("add-level-btn");
  const rebuildBtn = document.getElementById("rebuild-levels-btn");
  // ➕ теперь одна на все три режима — модалка сама подстраивается
  // (см. openAddLevelModal/submitAddLevel): в Watchlist/Greylist доступен
  // весь выбор (support/resistance/volume/просто тикер), в Blacklist —
  // только "просто добавить тикер" (уровням там взяться неоткуда).
  if (addLevelBtn) {
    addLevelBtn.style.display = "";
    addLevelBtn.title = currentListMode === "blacklist"
      ? "Добавить монету в чёрный список"
      : "Добавить уровень вручную / добавить монету";
  }
  if (rebuildBtn) rebuildBtn.style.display = currentListMode === "whitelist" ? "" : "none";

  if (watchlistSearchEl) watchlistSearchEl.value = "";
}

// 🔘 Серый список — кандидаты Потока Б (радар, см. swing_hunter.py::
// build_greylist_candidates). Чистый рендер строк в переданный контейнер —
// без установки счётчика (счётчик всегда = размер ПОЛНОГО кэша, см.
// renderCurrentList, а не отфильтрованного при поиске подсписка).
function renderGreylistRows(data) {
  if (!watchlistListEl) return;
  if (data.length === 0) {
    watchlistListEl.innerHTML = `<div class="muted">Кандидатов пока нет — радар ещё не нашёл ничего сверх боевого списка.</div>`;
    return;
  }
  watchlistListEl.innerHTML = data.map((c, i) => `
    <div class="list-item gl-row" data-i="${i}" style="display:flex;align-items:center;justify-content:space-between;">
      <span class="fav-star" title="${c.favorite ? "Убрать из избранного" : "В избранное"}"
            onclick="event.stopPropagation(); toggleFavorite('${c.coin}');"
            style="cursor:pointer;margin-right:6px;font-size:14px;line-height:1;color:${c.favorite ? "#f2c14e" : "#9aa0a6"};">${c.favorite ? "★" : "☆"}</span>
      <span style="flex-grow:1;min-width:0;">
        <span>${c.coin}</span>
        <span style="opacity:0.7;font-size:11px;display:block;">
          ${formatVolume(c.volume)}
        </span>
      </span>
      ${c.has_custom_levels ? `<span class="manual-hand" title="${c.ignore_auto ? "❄️ Заморожена — торгует только по ручному, авто-уровни игнорируются" : "Ручные зоны — изменить / удалить"}"
            onclick="event.stopPropagation(); openManageZonesModal('${c.coin}', false);"
            style="cursor:pointer;font-size:13px;line-height:1;margin-left:6px;${c.ignore_auto ? "filter:grayscale(1) sepia(1) hue-rotate(175deg) saturate(4) brightness(1.35);" : ""}">✋</span>` : ""}
      <span class="wl-menu-wrap" style="position:relative;margin-left:6px;">
        <span class="wl-menu-btn" title="Действия"
              onclick="event.stopPropagation(); toggleWlMenu(this);"
              style="cursor:pointer;font-size:14px;line-height:1;color:#9aa0a6;padding:0 2px;">⋮</span>
        <div class="wl-menu-dropdown" style="display:none;position:absolute;top:100%;right:0;z-index:60;background:#1e1e1e;border:1px solid #3f3f4e;border-radius:6px;margin-top:2px;min-width:170px;box-shadow:0 4px 16px rgba(0,0,0,0.5);">
          <div style="padding:7px 10px;cursor:pointer;font-size:12px;font-weight:normal;"
               onclick="event.stopPropagation(); promoteGreylistCoin('${c.coin}', 'whitelist');">✅ В белый список</div>
          <div style="padding:7px 10px;cursor:pointer;font-size:12px;font-weight:normal;color:#e5654f;"
               onclick="event.stopPropagation(); promoteGreylistCoin('${c.coin}', 'blacklist');">🗑 Убрать из списка</div>
        </div>
      </span>
    </div>`).join("");

  watchlistListEl.querySelectorAll(".gl-row").forEach((row) => {
    const c = data[Number(row.dataset.i)];

    // Клик по строке (не по ⋮/меню) — график кандидата, уровни из
    // greylist_levels.json (см. loadLevels: source="greylist").
    row.onclick = (e) => {
      if (e.target.closest(".wl-menu-wrap")) return;
      loadChart(c.coin, null, null, { source: "greylist" });
    };
  });
}

// ⋮ на строке серого списка — "В белый список" / "Убрать из списка"
// (снимает с радара насовсем, через blacklist — см. /api/greylist/promote).
async function promoteGreylistCoin(coin, action) {
  const ok = await _postJson("/api/greylist/promote", { coin, action });
  if (ok) { await loadGreylist(); await loadBlacklist(); }
}

// ⚫ Чёрный список — плоский список тикеров (blacklist.json), которые
// сканер игнорирует на всех этапах (Поток А и Поток Б оба его читают,
// см. swing_hunter.py::get_battle_symbols/build_greylist_candidates).
// Монета попадает сюда либо кнопкой 🚫 из серого списка, либо вручную
// (➕ в заголовке при выборе этого режима) — снять её отсюда тоже можно
// вручную (🗑).
function renderBlacklistRows(data) {
  if (!watchlistListEl) return;
  if (data.length === 0) {
    watchlistListEl.innerHTML = `<div class="muted">Чёрный список пуст.</div>`;
    return;
  }
  const btnStyle = "cursor:pointer;background:none;border:1px solid #444;color:#ddd;border-radius:4px;padding:2px 8px;margin-left:6px;font-size:12px;";
  watchlistListEl.innerHTML = data.map((coin, i) => `
    <div class="list-item bl-row" data-i="${i}" style="display:flex;align-items:center;justify-content:space-between;">
      <span>${coin}</span>
      <button class="bl-remove" style="${btnStyle}" title="Убрать из чёрного списка">🗑</button>
    </div>`).join("");

  watchlistListEl.querySelectorAll(".bl-row").forEach((row) => {
    const coin = data[Number(row.dataset.i)];
    row.querySelector(".bl-remove").onclick = async () => {
      const ok = await _postJson("/api/blacklist/remove", { coin });
      if (ok) await loadBlacklist();
    };
  });
}

// Полная (неотфильтрованная) перерисовка текущего режима — вызывается при
// переключении режима и после фоновой подгрузки данных. Счётчик в
// заголовке всегда берётся тут, из ПОЛНОГО кэша — поиск (filterWatchlist)
// рисует поверх строки списка напрямую, самого счётчика не трогая (так
// же вело себя старое поведение для обычного Watchlist).
function renderCurrentList() {
  if (!watchlistListEl) return;
  if (currentListMode === "whitelist") {
    watchlistCountEl.textContent = watchlistCache.length;
    if (watchlistCache.length === 0) {
      watchlistListEl.innerHTML = "<div class='muted'>список пуст</div>";
    } else {
      renderWatchlistEntries(watchlistCache);
    }
  } else if (currentListMode === "greylist") {
    watchlistCountEl.textContent = greylistCache.length;
    renderGreylistRows(greylistCache);
  } else {
    watchlistCountEl.textContent = blacklistCache.length;
    renderBlacklistRows(blacklistCache);
  }
}

async function loadGreylist() {
  let data;
  try {
    const res = await fetch("/api/greylist");
    data = res.ok ? await res.json() : [];
  } catch (e) {
    console.error("greylist load failed", e);
    return;
  }
  greylistCache = Array.isArray(data) ? data : [];
  if (currentListMode === "greylist") renderCurrentList();
}

async function loadBlacklist() {
  let data;
  try {
    const res = await fetch("/api/blacklist");
    data = res.ok ? await res.json() : [];
  } catch (e) {
    console.error("blacklist load failed", e);
    return;
  }
  blacklistCache = Array.isArray(data) ? data : [];
  if (currentListMode === "blacklist") renderCurrentList();
}

// Выпадающее меню выбора списка (клик по "Watchlist" в заголовке панели).
function wireListModeDropdown() {
  const label = document.getElementById("list-mode-label");
  const dropdown = document.getElementById("list-mode-dropdown");
  if (!label || !dropdown) return;

  label.onclick = (e) => {
    e.stopPropagation();
    dropdown.style.display = dropdown.style.display === "none" ? "block" : "none";
  };
  dropdown.onclick = (e) => e.stopPropagation(); // клик внутри меню не должен закрывать его раньше выбора
  document.addEventListener("click", () => { dropdown.style.display = "none"; });

  dropdown.querySelectorAll(".list-mode-option").forEach((opt) => {
    opt.onclick = () => {
      currentListMode = opt.dataset.mode;
      dropdown.style.display = "none";
      applyListModeUI();
      renderCurrentList();
    };
  });
}

applyListModeUI();
wireListModeDropdown();


// === Настройки TP/SL для BOUNCE (#tpsl-btn / #tpsl-overlay) ===
// Одна и та же модалка работает в двух режимах:
//  - общие (глобальные) TP/SL — tpSlModalCoin === null, кнопка ⚙️ TP/SL в
//    шапке графика, читает/пишет /api/config/bounce_tpsl;
//  - свой TP/SL монеты — tpSlModalCoin = "BTCUSDT" и т.п., открывается из
//    ⋮-меню строки watchlist (openCoinTpSlModal), читает/пишет
//    /api/config/coin_tpsl. Пустые поля при сохранении = сброс на общий.
let tpSlModalCoin = null;

function openTpSlModal() {
  tpSlModalCoin = null;
  const overlay = document.getElementById("tpsl-overlay");
  const errEl = document.getElementById("tpsl-error");
  const titleEl = document.getElementById("tpsl-title");
  const hintEl = document.getElementById("tpsl-hint");
  const resetBtn = document.getElementById("tpsl-reset-btn");
  if (!overlay) return;
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  if (titleEl) titleEl.textContent = "Настройки BOUNCE TP/SL";
  if (hintEl) hintEl.style.display = "none";
  if (resetBtn) resetBtn.style.display = "none";

  // Подтягиваем текущие значения с бэкенда
  fetch("/api/config/bounce_tpsl")
    .then(r => r.json())
    .then(data => {
      document.getElementById("tpsl-tp").value = data.bounce_tp_pct;
      document.getElementById("tpsl-sl").value = data.bounce_sl_pct;
      overlay.style.display = "flex";
    })
    .catch(e => console.error("Ошибка загрузки TP/SL:", e));
}

// ⋮ → "⚖️ Свой TP/SL" на строке watchlist. Поля пустые = своего override нет
// (используется общий) — специально НЕ подставляем сюда общие значения,
// чтобы не путать "нет своего" с "свой = как общий".
function openCoinTpSlModal(coin) {
  tpSlModalCoin = coin;
  const overlay = document.getElementById("tpsl-overlay");
  const errEl = document.getElementById("tpsl-error");
  const titleEl = document.getElementById("tpsl-title");
  const hintEl = document.getElementById("tpsl-hint");
  const resetBtn = document.getElementById("tpsl-reset-btn");
  if (!overlay) return;
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  if (titleEl) titleEl.textContent = `TP/SL — ${coin}`;
  if (hintEl) {
    hintEl.textContent = "Пусто — используется общий TP/SL. Свой — общий для этой монеты игнорируется.";
    hintEl.style.display = "block";
  }

  fetch(`/api/config/coin_tpsl/${encodeURIComponent(coin)}`)
    .then(r => r.json())
    .then(data => {
      document.getElementById("tpsl-tp").value = data.tp != null ? data.tp : "";
      document.getElementById("tpsl-sl").value = data.sl != null ? data.sl : "";
      if (resetBtn) resetBtn.style.display = (data.tp != null || data.sl != null) ? "" : "none";
      overlay.style.display = "flex";
    })
    .catch(e => console.error("Ошибка загрузки TP/SL монеты:", e));
}

function closeTpSlModal() {
  const overlay = document.getElementById("tpsl-overlay");
  if (overlay) overlay.style.display = "none";
  tpSlModalCoin = null;
}

async function submitTpSl() {
  const errEl = document.getElementById("tpsl-error");
  const showError = (msg) => {
    if (errEl) { errEl.textContent = msg; errEl.style.display = "block"; }
  };

  const tpRaw = document.getElementById("tpsl-tp").value.trim();
  const slRaw = document.getElementById("tpsl-sl").value.trim();

  const submitBtn = document.getElementById("tpsl-submit-btn");

  if (tpSlModalCoin) {
    // Свой TP/SL монеты: оба поля пустые -> сброс на общий, разрешено.
    // Иначе оба должны быть заданы (валидные числа > 0) — не даём
    // половинчатый override.
    if (tpRaw === "" && slRaw === "") {
      return await _saveCoinTpSl(tpSlModalCoin, null, null, submitBtn, showError);
    }
    const tpVal = parseFloat(tpRaw);
    const slVal = parseFloat(slRaw);
    if (!Number.isFinite(tpVal) || !Number.isFinite(slVal)) return showError("Заполните оба поля или оставьте оба пустыми");
    if (tpVal <= 0 || slVal <= 0) return showError("Значения должны быть больше 0");
    return await _saveCoinTpSl(tpSlModalCoin, tpVal, slVal, submitBtn, showError);
  }

  const tpVal = parseFloat(tpRaw);
  const slVal = parseFloat(slRaw);
  if (!Number.isFinite(tpVal) || !Number.isFinite(slVal)) return showError("Значения должны быть числами");
  if (tpVal <= 0 || slVal <= 0) return showError("Значения должны быть больше 0");

  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/config/bounce_tpsl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bounce_tp_pct: tpVal, bounce_sl_pct: slVal }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || "Не удалось сохранить настройки");
      return;
    }
    closeTpSlModal();
  } catch (e) {
    showError("Ошибка запроса: " + e.message);
  } finally {
    submitBtn.disabled = false;
  }
}

async function _saveCoinTpSl(coin, tp, sl, submitBtn, showError) {
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/config/coin_tpsl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ coin, tp, sl }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || "Не удалось сохранить настройки");
      return;
    }
    closeTpSlModal();
    if (typeof loadWatchlist === "function") loadWatchlist();
  } catch (e) {
    showError("Ошибка запроса: " + e.message);
  } finally {
    submitBtn.disabled = false;
  }
}

function resetTpSl() {
  document.getElementById("tpsl-tp").value = "";
  document.getElementById("tpsl-sl").value = "";
  submitTpSl();
}

function wireTpSlModal() {
  const btn = document.getElementById("tpsl-btn");
  const overlay = document.getElementById("tpsl-overlay");
  const cancelBtn = document.getElementById("tpsl-cancel-btn");
  const submitBtn = document.getElementById("tpsl-submit-btn");
  const resetBtn = document.getElementById("tpsl-reset-btn");

  if (!btn || !overlay || !cancelBtn || !submitBtn) return;

  btn.onclick = openTpSlModal;
  cancelBtn.onclick = closeTpSlModal;
  submitBtn.onclick = submitTpSl;
  if (resetBtn) resetBtn.onclick = resetTpSl;

  overlay.onclick = (e) => {
    if (e.target === overlay) closeTpSlModal();
  };
}

// === Лог одного вотчера (#watcher-log-overlay), кнопка 📜 в "В работе" ===
// Показывает watcher.debug_ring с бэкенда (см. /api/watcher_debug_log/{level_id})
// — то, что реально видит код этого КОНКРЕТНОГО вотчера, а не общий файл на
// монету, где вперемешку пишут все уровни/режимы сразу. Пока окно открыто —
// раз в 2с само подтягивает свежие строки (если включено "Автообновление"),
// чтобы можно было следить за живым сканом, не переоткрывая руками.
// coin передаём вторым параметром query — бэкенду он нужен, чтобы найти
// АРХИВНУЮ запись умершего вотчера в watcher_history.json (ключ там
// {coin}_{strategy}_{level_id}, level_id сам по себе координаты уровня без
// монеты не гарантирует уникальность). Для ЖИВОГО вотчера coin не нужен —
// он и так один в _watchers по одному level_id — но передаём всегда, вреда
// нет, а для сигналов (архив) без него работать не будет.
let watcherLogLevelId = null;
let watcherLogCoin = null;
let watcherLogTimer = null;

async function _fetchWatcherLog() {
  if (!watcherLogLevelId) return;
  const textEl = document.getElementById("watcher-log-text");
  const errEl = document.getElementById("watcher-log-error");
  const hintEl = document.getElementById("watcher-log-hint");
  const autoWrapEl = document.getElementById("watcher-log-autorefresh")?.closest("label");
  try {
    const url = `/api/watcher_debug_log/${encodeURIComponent(watcherLogLevelId)}` +
      (watcherLogCoin ? `?coin=${encodeURIComponent(watcherLogCoin)}` : "");
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) {
      if (errEl) { errEl.textContent = data.detail || "Лог не найден — вотчер умер и в архиве этого уровня ещё нет"; errEl.style.display = "block"; }
      return;
    }
    if (errEl) errEl.style.display = "none";
    if (textEl) {
      const wasAtBottom = textEl.scrollTop + textEl.clientHeight >= textEl.scrollHeight - 4;
      textEl.textContent = (data.lines || []).join("\n") || "(пока пусто)";
      if (wasAtBottom) textEl.scrollTop = textEl.scrollHeight;
    }
    // Архивная запись (вотчер уже умер) — статична, дальше опрашивать
    // нечего, останавливаем автообновление и прячем переключатель, чтобы
    // не путать: "Автообновление" имеет смысл только для живого вотчера.
    if (data.source === "archive") {
      if (watcherLogTimer) { clearInterval(watcherLogTimer); watcherLogTimer = null; }
      if (autoWrapEl) autoWrapEl.style.display = "none";
      if (hintEl) hintEl.textContent = "Архив — вотчер уже умер, это его полная история до смерти.";
    } else {
      if (autoWrapEl) autoWrapEl.style.display = "";
      if (hintEl) hintEl.textContent = "Последние строки — то, что реально видит код этого вотчера, отдельно от других уровней/режимов той же монеты.";
    }
  } catch (e) {
    if (errEl) { errEl.textContent = "Ошибка запроса: " + e.message; errEl.style.display = "block"; }
  }
}

function openWatcherLogModal(e, levelId, coin) {
  if (e) e.stopPropagation();
  const overlay = document.getElementById("watcher-log-overlay");
  const titleEl = document.getElementById("watcher-log-title");
  const textEl = document.getElementById("watcher-log-text");
  const errEl = document.getElementById("watcher-log-error");
  const autoWrapEl = document.getElementById("watcher-log-autorefresh")?.closest("label");
  if (!overlay) return;
  watcherLogLevelId = levelId;
  watcherLogCoin = coin || null;
  if (titleEl) titleEl.textContent = `Лог вотчера — ${coin || ""}`;
  if (textEl) textEl.textContent = "Загрузка…";
  if (errEl) errEl.style.display = "none";
  if (autoWrapEl) autoWrapEl.style.display = "";
  overlay.style.display = "flex";
  _fetchWatcherLog();
  if (watcherLogTimer) clearInterval(watcherLogTimer);
  watcherLogTimer = setInterval(() => {
    const autoEl = document.getElementById("watcher-log-autorefresh");
    if (autoEl && !autoEl.checked) return;
    _fetchWatcherLog();
  }, 2000);
}

function closeWatcherLogModal() {
  const overlay = document.getElementById("watcher-log-overlay");
  if (overlay) overlay.style.display = "none";
  if (watcherLogTimer) { clearInterval(watcherLogTimer); watcherLogTimer = null; }
  watcherLogLevelId = null;
  watcherLogCoin = null;
}

function wireWatcherLogModal() {
  const overlay = document.getElementById("watcher-log-overlay");
  const closeBtn = document.getElementById("watcher-log-close-btn");
  if (!overlay || !closeBtn) return;
  closeBtn.onclick = closeWatcherLogModal;
  overlay.onclick = (e) => {
    if (e.target === overlay) closeWatcherLogModal();
  };
}

if (isMainDashboardPage()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireWatcherLogModal);
  } else {
    wireWatcherLogModal();
  }
}

if (isMainDashboardPage()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireTpSlModal);
  } else {
    wireTpSlModal();
  }
}

// === Кнопка "Все уровни" (👁️) ===
if (showAllLevelsBtn) {
  showAllLevelsBtn.onclick = () => {
    if (selectedCoin) {
      // Загружаем текущую монету, но передаем focus = null (все уровни)
      loadChart(selectedCoin, null);
    }
  };
}


// ===== 🔥 НОВОЕ: Логика быстрого переключения всех уровней =====
function clearBackgroundLevels() {
  backgroundLevelLines.forEach((series) => {
    try { chart.removeSeries(series); } catch(e) {}
  });
  backgroundLevelLines = [];
}

async function showBackgroundLevels(coin) {
  if (!globalCandles.length) return;
  try {
    const res = await fetch(`/api/levels/${encodeURIComponent(coin)}`);
    if (!res.ok) return;
    const data = await res.json();
    
    const drawSide = (zones, color) => {
      (zones || []).forEach((z) => {
        if (z.min == null || z.max == null) return;
        
        // Пропускаем рабочий уровень, чтобы не было двойной заливки и грязи
        if (focusedLevel && Math.abs(z.min - focusedLevel.min) < 1e-9 && Math.abs(z.max - focusedLevel.max) < 1e-9) return;
        
        const zoneStartSec = (z.activated_at || z.date) ? Math.floor(new Date((z.activated_at || z.date) + "T00:00:00Z").getTime() / 1000) : null;
        const lineCandles = zoneStartSec !== null ? globalCandles.filter((c) => c.time >= zoneStartSec) : globalCandles;
        if (!lineCandles.length) return;
        
        const typeLabel = friendlyLevelType(z.type);
        const tooltipInfo = `${typeLabel} · ${z.date || "—"} · Вес: ${z.score ?? "—"}${levelTouchesLabel(z)} (ФОН)`;
        
        // Сохраняем линии в отдельный массив, чтобы не трогать рабочий уровень вотчера
        backgroundLevelLines.push(...addZoneBand(z.min, z.max, color, lineCandles, tooltipInfo));
      });
    };

    drawSide(data.supports, "#00c853");
    drawSide(data.resistances, "#ff3d3d");
  } catch (e) {
    console.error("Фоновые уровни не загрузились", e);
  }
}

if (toggleLevelsBtn) {
  toggleLevelsBtn.onclick = async () => {
    if (!selectedCoin) return;
    if (!focusedLevel && !isSignalView) {
      // Обычный график: скрыть/показать авто-уровни
      levelsVisible = !levelsVisible;
      try { localStorage.setItem("chart_levels_visible", levelsVisible ? "1" : "0"); } catch (e) {}
      setLevelsBtnActive(levelsVisible);
      if (levelsVisible) await loadLevels(selectedCoin, chartLoadToken, lastLevelsSource);
      else clearLevelLines();
      return;
    }
    toggleLevelsBtn.disabled = true;
    
    if (isBackgroundLevelsShowing) {
      clearBackgroundLevels();
      isBackgroundLevelsShowing = false;
      toggleLevelsBtn.style.background = "";
      toggleLevelsBtn.style.color = "";
      toggleLevelsBtn.style.borderColor = "";
    } else {
      await showBackgroundLevels(selectedCoin);
      isBackgroundLevelsShowing = true;
      toggleLevelsBtn.style.background = "#2a3550"; // Подсвечиваем активную кнопку
      toggleLevelsBtn.style.color = "#9fb4e8";
      toggleLevelsBtn.style.borderColor = "#3a4a70";
    }
    toggleLevelsBtn.disabled = false;
  };
}