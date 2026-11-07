// Мобильная раскладка: полоса вкладок под графиком. Логика приложения (app.js) не трогается:
// вкладки только показывают/прячут уже существующие блоки, а «Сигналы»/«На бирже» переключают
// штатные вкладки .sig-tab (их обработчики остаются в app.js). На широких экранах полоса скрыта CSS.
(function () {
  const layout = document.querySelector(".layout");
  if (!layout) return;

  const TABS = [
    { id: "active", label: "В работе", count: "active-count" },
    { id: "watch", label: "Watchlist", count: "watchlist-count" },
    { id: "signals", label: "Сигналы бота" },
    { id: "exchange", label: "На бирже" },
    { id: "scan", label: "Скан" },
  ];

  const bar = document.createElement("div");
  bar.className = "m-tabbar";
  bar.innerHTML = TABS.map((t) =>
    `<button type="button" class="m-tab" data-mtab="${t.id}">${t.label}` +
    (t.count ? ` <span class="badge" data-src="${t.count}"></span>` : "") + `</button>`
  ).join("");
  layout.appendChild(bar);

  function select(id) {
    if (!TABS.some((t) => t.id === id)) id = "active";
    layout.dataset.mtab = id;
    bar.querySelectorAll(".m-tab").forEach((b) => b.classList.toggle("active", b.dataset.mtab === id));
    if (id === "signals" || id === "exchange") {
      const stab = document.querySelector('.sig-tab[data-stab="' + id + '"]');
      if (stab) stab.click();
    }
    try { localStorage.setItem("m_tab", id); } catch (e) {}
  }

  bar.addEventListener("click", (e) => {
    const b = e.target.closest(".m-tab");
    if (b) select(b.dataset.mtab);
  });

  // счётчики на вкладках берём из уже существующих бейджей
  function syncCounts() {
    bar.querySelectorAll("[data-src]").forEach((el) => {
      const src = document.getElementById(el.dataset.src);
      const txt = src ? src.textContent.trim() : "";
      el.textContent = txt;
      el.style.display = txt ? "" : "none";
    });
  }
  syncCounts();
  setInterval(syncCounts, 2000);

  // выбор монеты (из списков или из таблиц) — показать график
  layout.addEventListener("click", (e) => {
    if (!window.matchMedia("(max-width: 1000px)").matches) return;
    if (e.target.closest("button, input, select, a, .m-tabbar")) return;
    if (e.target.closest(".list-item, #signals-body tr, #exchange-body tr, #active-list > div, #history-panel-body")) {
      const main = document.querySelector(".main");
      if (main) setTimeout(() => main.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }
  });

  // тёмный график на телефоне (на компьютере остаются исходные цвета из app.js)
  const mq = window.matchMedia("(max-width: 1000px)");
  const THEMES = {
    dark:  { bg: "#0e0f13", text: "#9aa0ac", grid: "#1b1d24", border: "#262832" },
    light: { bg: "#ffffff", text: "#1b1d24", grid: "#e6e8eb", border: "#e6e8eb" },
  };
  function themeOpts(t) {
    return {
      layout: { background: { color: t.bg }, textColor: t.text },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.border },
      timeScale: { borderColor: t.border },
    };
  }
  function applyChartTheme() {
    const t = mq.matches ? THEMES.dark : THEMES.light;
    if (typeof chart !== "undefined" && chart) chart.applyOptions(themeOpts(t));
    if (typeof adxChart !== "undefined" && adxChart) adxChart.applyOptions(themeOpts(t));
  }
  if (mq.matches) applyChartTheme();
  mq.addEventListener("change", applyChartTheme);
  // Шкала времени (только телефон): подписи по типу тика — начало суток/месяца/года показываем датой,
  // обычные тики — временем. Так видно, где граница дней. Свечи не мельче MIN_BAR px, чтобы подписи не слипались.
  const MIN_BAR = 3;
  const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  let origTick = null;
  function phoneTick(time, type) {
    const tf = typeof currentTimeframe !== "undefined" ? currentTimeframe : "15m";
    const d = new Date(time * 1000);
    const parts = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Kyiv", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(d).reduce((o, x) => (o[x.type] = x.value, o), {});
    const dd = parts.day.padStart(2, "0"), mm = parts.month.padStart(2, "0");
    if (type === 0) return parts.year;                                  // начало года
    if (type === 1) return MONTHS[parseInt(parts.month, 10) - 1];        // начало месяца
    if (type === 2 || tf === "1d" || tf === "1w" || tf === "1M") return dd + "." + mm;  // начало суток
    return parts.hour + ":" + parts.minute;                              // обычный тик
  }
  function syncAxis() {
    if (typeof chart === "undefined" || !chart) return;
    if (origTick === null) origTick = chart.options().timeScale.tickMarkFormatter || undefined;
    if (mq.matches) chart.applyOptions({ timeScale: { visible: true, tickMarkFormatter: phoneTick, minBarSpacing: MIN_BAR } });
    else chart.applyOptions({ timeScale: { visible: true, tickMarkFormatter: origTick, minBarSpacing: 0.5 } });
  }
  syncAxis();
  mq.addEventListener("change", syncAxis);

  // панель ADX создаётся при первом включении — красим её, как только она появится
  const mainEl = document.querySelector(".main");
  if (mainEl && !document.getElementById("adx-panel")) {
    const obs = new MutationObserver(() => {
      if (!document.getElementById("adx-panel")) return;
      if (mq.matches) applyChartTheme();
      obs.disconnect();
    });
    obs.observe(mainEl, { childList: true });
  } else if (mq.matches) applyChartTheme();

  // Стартовое окно графика (только телефон). app.js ставит окно в ±CHART_HALF_WINDOW свечей (~900) —
  // на узком экране это пусто и свечи уезжают к краю. Здесь то же окно сужаем до того, что реально
  // помещается при нормальной толщине свечи (WANT_BAR px), сохраняя замысел app.js:
  //   есть центр (вход сигнала / начало слежения) -> он ровно посередине;
  //   центра нет (вотчер без событий)              -> последняя свеча справа с небольшим запасом.
  const WANT_BAR = 5, RIGHT_PAD = 0.12;
  if (typeof chart !== "undefined" && chart) {
    const ts = chart.timeScale();
    const origSet = ts.setVisibleLogicalRange.bind(ts);
    ts.setVisibleLogicalRange = function (r) {
      try {
        const W = typeof CHART_HALF_WINDOW !== "undefined" ? CHART_HALF_WINDOW : null;
        if (mq.matches && W && r && r.to - r.from === 2 * W && typeof globalCandles !== "undefined" && globalCandles.length) {
          const n = Math.max(20, Math.floor(ts.width() / WANT_BAR));
          const last = globalCandles.length - 1;
          if (r.from === last - 1.5 * W && r.to === last + 0.5 * W) {
            const to = last + n * RIGHT_PAD;
            return origSet({ from: to - n, to });
          }
          const c = (r.from + r.to) / 2;
          return origSet({ from: c - n / 2, to: c + n / 2 });
        }
      } catch (e) {}
      return origSet(r);
    };
  }

  // Подписи зон уровня (только телефон). На графике у зоны — короткий ярлык («Хай нед 6.5») за её внешним
  // краем: под зоной, если она ниже цены или цена внутри, над зоной, если она выше цены (туда свечи почти не
  // заходят). Касание зоны/ярлыка раскрывает полный текст, повторное касание — сворачивает. Раскрыт один уровень.
  const LABEL_GAP_PX = 14;   // отступ ярлыка от края зоны
  const HIT_ZONE_PX = 12;    // допуск касания вокруг самой зоны
  const HIT_LABEL_PX = 26;   // допуск касания со стороны ярлыка
  let zones = [];

  function fullTitle(t) {
    const parts = String(t).split(" · ");
    const types = parts[0].split("+").map((x) => x.trim());
    parts[0] = types[0] + (types.length > 1 ? " +" + (types.length - 1) : "");
    return parts.join(" · ").replace("Касаний: ", "кас. ");
  }
  function tagTitle(t) {
    const parts = String(t).split(" · ");
    const w = parts[0].split("+")[0].trim().split(/\s+/);
    const head = w[0] + (w[1] ? " " + w[1].slice(0, 3) : "");
    const m = (parts[2] || "").match(/\d+(?:\.\d+)?/);
    return m ? head + " " + m[0] : head;
  }
  function setExpanded(z, on) {
    z.expanded = on;
    try { z.label.applyOptions({ title: on ? z.full : z.tag }); } catch (e) { z.dead = true; }
  }
  function collapseAll(except) {
    zones.forEach((z) => { if (z !== except && z.expanded) setExpanded(z, false); });
    zones = zones.filter((z) => !z.dead);
  }

  if (typeof addZoneBand === "function") {
    const origBand = addZoneBand;
    addZoneBand = function (zMin, zMax, color, candles, titleText) {
      if (!mq.matches || !titleText || typeof candleSeries === "undefined" || !candleSeries || !candles.length) {
        return origBand(zMin, zMax, color, candles, titleText);
      }
      const res = origBand(zMin, zMax, color, candles, "");
      try {
        const lastClose = candles[candles.length - 1].close;
        const above = typeof lastClose === "number" && zMin > lastClose;
        const edge = above ? zMax : zMin;
        const y = candleSeries.priceToCoordinate(edge);
        let price = edge;
        if (y !== null && y !== undefined) {
          const p2 = candleSeries.coordinateToPrice(y + (above ? -LABEL_GAP_PX : LABEL_GAP_PX));
          if (p2 !== null && p2 !== undefined) price = p2;
        }
        const z = { min: zMin, max: zMax, above, full: fullTitle(titleText), tag: tagTitle(titleText), expanded: false };
        z.label = chart.addLineSeries({
          color, lineVisible: false, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, title: z.tag, autoscaleInfoProvider: () => null,
        });
        z.label.setData([{ time: candles[candles.length - 1].time, value: price }]);
        res.push(z.label);
        zones.push(z);
      } catch (e) {}
      return res;
    };
  }

  // убранные с графика зоны выбрасываем из списка касаний
  ["clearLevelLines", "clearSignalLines"].forEach((fn) => {
    const arrName = fn === "clearLevelLines" ? "levelLines" : "signalLines";
    try {
      const orig = window[fn];
      if (typeof orig !== "function") return;
      window[fn] = function () {
        try { const gone = new Set(window[arrName] || (0, eval)(arrName)); zones = zones.filter((z) => !gone.has(z.label)); } catch (e) {}
        return orig.apply(this, arguments);
      };
    } catch (e) {}
  });

  // касание на графике: ближайшая зона (по пикселям) раскрывается / сворачивается
  if (typeof chart !== "undefined" && chart && typeof candleSeries !== "undefined" && candleSeries) {
    chart.subscribeClick((param) => {
      if (!mq.matches || !param || !param.point) return;
      const py = param.point.y;
      let best = null, bestD = Infinity;
      zones.forEach((z) => {
        const y1 = candleSeries.priceToCoordinate(z.max), y2 = candleSeries.priceToCoordinate(z.min);
        if (y1 === null || y2 === null) return;
        const top = Math.min(y1, y2), bot = Math.max(y1, y2);
        const d = py < top ? top - py : (py > bot ? py - bot : 0);
        const labelSide = z.above ? py < top : py > bot;
        if (d <= (labelSide ? HIT_LABEL_PX : HIT_ZONE_PX) && d < bestD) { best = z; bestD = d; }
      });
      if (!best) { collapseAll(null); return; }
      collapseAll(best);
      setExpanded(best, !best.expanded);
    });
  }

  let saved = "active";
  try { saved = localStorage.getItem("m_tab") || "active"; } catch (e) {}
  select(saved);
})();