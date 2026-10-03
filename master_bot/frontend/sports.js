// Спорт-страница — полностью отдельно от крипто-графика (app.js). Просто
// две таблицы, каждая читает свой /api/sports/* эндпоинт и обновляется
// раз в минуту. Никакого Telegram тут нет — данные пишут сами
// football.py/player_props.py (см. _save_football_signal/_save_nba_signal)
// рядом с отправкой в Telegram, независимо от неё.

function renderStatus(elId, data) {
  const el = document.getElementById(elId);
  if (!el) return;
  const running = data.running ? "🟢 включено" : "🔴 выключено";
  const scan = data.last_scan || {};
  if (!scan.checked_at) {
    el.textContent = `${running} · сканов ещё не было`;
    return;
  }
  if (elId === "football-status") {
    el.textContent = `${running} · проверено ${scan.checked_at} · в лайве: ${scan.total_live ?? "?"} · топ-лиг: ${scan.top_leagues_live ?? "?"} · подходит: ${scan.matches_found ?? "?"}`;
  } else {
    el.textContent = `${running} · проверено ${scan.checked_at} · составов: ${scan.matches_scanned ?? "?"} · травм: ${scan.injuries_found ?? "?"} · сигналов: ${scan.signals_found ?? "?"}`;
  }
}

async function loadFootballStatus() {
  try {
    const res = await fetch("/api/sports/football/status");
    const data = await res.json();
    renderStatus("football-status", data);
    const btn = document.getElementById("football-toggle-btn");
    if (btn) btn.textContent = data.running ? "⏸ Выключить" : "▶️ Включить";
  } catch (e) {
    console.error("football status load failed", e);
  }
}

async function loadNbaStatus() {
  try {
    const res = await fetch("/api/sports/nba/status");
    const data = await res.json();
    renderStatus("nba-status", data);
    const btn = document.getElementById("nba-toggle-btn");
    if (btn) btn.textContent = data.running ? "⏸ Выключить" : "▶️ Включить";
  } catch (e) {
    console.error("nba status load failed", e);
  }
}

async function loadFootball() {
  const body = document.getElementById("football-body");
  if (!body) return;
  try {
    const res = await fetch("/api/sports/football?limit=50");
    const data = await res.json();
    if (!data.length) {
      body.innerHTML = "<tr><td colspan='10'>сигналов пока нет</td></tr>";
      return;
    }
    body.innerHTML = "";
    data.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.date ?? ""}</td>
        <td>${s.league ?? ""}</td>
        <td>${s.minute ?? ""}'</td>
        <td>${s.home_team ?? ""} — ${s.away_team ?? ""}</td>
        <td>${s.score ?? ""}</td>
        <td>${s.halftime_score ?? ""}</td>
        <td>${s.total_xg ?? ""}</td>
        <td>${s.total_chances ?? ""}</td>
        <td>${s.home_shots ?? ""} / ${s.away_shots ?? ""}</td>
        <td>${s.prediction ?? ""}</td>
      `;
      body.appendChild(tr);
    });
  } catch (e) {
    body.innerHTML = "<tr><td colspan='10'>ошибка загрузки</td></tr>";
    console.error("football load failed", e);
  }
}

async function loadNba() {
  const body = document.getElementById("nba-body");
  if (!body) return;
  try {
    const res = await fetch("/api/sports/nba?limit=50");
    const data = await res.json();
    if (!data.length) {
      body.innerHTML = "<tr><td colspan='9'>сигналов пока нет</td></tr>";
      return;
    }
    body.innerHTML = "";
    data.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.date ?? ""}</td>
        <td>${s.matchup ?? ""}</td>
        <td>${s.injured_player ?? ""}</td>
        <td>${s.status ?? ""}</td>
        <td>${s.beneficiary ?? ""}</td>
        <td>${s.pts_out ?? ""}</td>
        <td>${s.pts_in ?? ""}</td>
        <td>+${s.diff ?? ""}</td>
        <td>${s.games_out ?? ""}</td>
      `;
      body.appendChild(tr);
    });
  } catch (e) {
    body.innerHTML = "<tr><td colspan='9'>ошибка загрузки</td></tr>";
    console.error("nba load failed", e);
  }
}

function refreshAll() {
  loadFootball();
  loadNba();
  loadFootballStatus();
  loadNbaStatus();
}

const footballToggleBtn = document.getElementById("football-toggle-btn");
if (footballToggleBtn) {
  footballToggleBtn.onclick = async () => {
    footballToggleBtn.disabled = true;
    try {
      await fetch("/api/sports/football/toggle", { method: "POST" });
      await loadFootballStatus();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      footballToggleBtn.disabled = false;
    }
  };
}

const nbaToggleBtn = document.getElementById("nba-toggle-btn");
if (nbaToggleBtn) {
  nbaToggleBtn.onclick = async () => {
    nbaToggleBtn.disabled = true;
    try {
      await fetch("/api/sports/nba/toggle", { method: "POST" });
      await loadNbaStatus();
    } catch (e) {
      alert("Ошибка: " + e);
    } finally {
      nbaToggleBtn.disabled = false;
    }
  };
}

refreshAll();
setInterval(refreshAll, 60000);