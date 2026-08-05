// ===== Carrot Workspace — widget dashboard =====
// Loaded before app.js. Functions are global (called from inline handlers and
// switchTab). Relies on api() / escHtml() defined in app.js at call time.

const RING_COLORS = { move: '#ff5c1f', exercise: '#3fd68f', stand: '#4da3ff' };
const RING_LABELS = { move: 'Move', exercise: 'Exercise', stand: 'Stand' };
const RING_UNITS = { move: 'kcal', exercise: 'min', stand: 'hr' };

let ghPollTimer = null;

// ===== Dashboard loader =====

async function loadDashboard() {
    renderGreeting();
    try {
        const resp = await api('/api/widgets');
        const widgets = resp.widgets || [];
        renderCanvas(widgets.filter(w => w.slot === 'canvas'));
        renderRail(widgets.filter(w => w.slot === 'rail'));
    } catch (e) {
        renderCanvas([]);
        renderRail([]);
    }
}

function renderGreeting() {
    const g = document.getElementById('dash-greeting');
    const sub = document.getElementById('dash-sub');
    const dateEl = document.getElementById('dash-date');
    if (!g) return;
    const h = new Date().getHours();
    let part = 'Good day';
    if (h < 12) part = 'Good morning';
    else if (h < 18) part = 'Good afternoon';
    else part = 'Good evening';
    const name = (window.__userName || '').trim();
    g.textContent = name ? `${part}, ${name}.` : `${part}.`;
    if (sub) sub.textContent = "Here's your workspace overview.";
    if (dateEl) {
        dateEl.textContent = new Date().toLocaleDateString(undefined, {
            weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
        });
    }
}

// ===== Canvas & rail rendering =====

function renderCanvas(widgets) {
    const canvas = document.getElementById('dash-canvas');
    if (!canvas) return;
    canvas.innerHTML = '';
    if (!widgets.length) {
        canvas.innerHTML = `
            <div class="empty-widget" id="canvas-empty">
              <span class="logo-mask big"></span>
              <p>No widgets yet — add your first widget.</p>
              <button class="btn btn-primary" onclick="openCatalog()">Add widget</button>
            </div>`;
        return;
    }
    for (const w of widgets) canvas.appendChild(buildWidgetCard(w));
}

function renderRail(widgets) {
    const rail = document.getElementById('dash-rail');
    if (!rail) return;
    rail.innerHTML = '';
    rail.classList.toggle('collapsed', widgets.length === 0);
    if (!widgets.length) {
        rail.innerHTML = `
            <div class="empty-widget rail-empty">
              <p>Rail is empty.</p>
              <button class="btn btn-ghost" onclick="openCatalog()">Add widget</button>
            </div>`;
        return;
    }
    for (const w of widgets) rail.appendChild(buildWidgetCard(w));
}

function buildWidgetCard(w) {
    const card = document.createElement('div');
    card.className = 'widget-card';
    card.dataset.id = w.id;
    if (w.type === 'fitness') {
        card.innerHTML = widgetHead('i-activity', 'Fitness', w.id);
        const body = document.createElement('div');
        body.className = 'widget-body';
        body.innerHTML = '<div class="muted small">Loading…</div>';
        card.appendChild(body);
        renderFitnessWidget(w, body);
    } else if (w.type === 'github') {
        card.innerHTML = widgetHead('i-github', 'Code Contributions', w.id);
        const body = document.createElement('div');
        body.className = 'widget-body';
        body.innerHTML = '<div class="muted small">Loading…</div>';
        card.appendChild(body);
        renderGithubWidget(w, body);
    } else if (w.type === 'calendar') {
        card.innerHTML = widgetHead('i-calendar', 'Calendar', w.id);
        const body = document.createElement('div');
        body.className = 'widget-body';
        body.innerHTML = '<div class="muted small">Loading…</div>';
        card.appendChild(body);
        renderCalendarWidget(w, body);
    } else if (w.type === 'weather') {
        card.innerHTML = widgetHead('i-cloud', 'Weather', w.id);
        const body = document.createElement('div');
        body.className = 'widget-body';
        body.innerHTML = '<div class="muted small">Loading…</div>';
        card.appendChild(body);
        renderWeatherWidget(w, body);
    } else if (w.type === 'quicknotes') {
        card.innerHTML = widgetHead('i-note', 'Quick Notes', w.id);
        const body = document.createElement('div');
        body.className = 'widget-body';
        body.innerHTML = '<div class="muted small">Loading…</div>';
        card.appendChild(body);
        renderQuickNotesWidget(w, body);
    } else {
        card.innerHTML = widgetHead('i-grid', w.type, w.id);
    }
    return card;
}

function widgetHead(icon, title, id) {
    return `
        <div class="widget-head">
          <svg class="ico"><use href="#${icon}"/></svg>
          <span class="widget-title">${escHtml(title)}</span>
          <button class="widget-kebab" title="Remove widget" onclick="removeWidget('${id}')">
            <svg class="ico"><use href="#i-trash"/></svg>
          </button>
        </div>`;
}

// ===== Fitness widget (rings) =====

async function renderFitnessWidget(w, body) {
    const cfg = w.config || {};
    const goals = {
        move: cfg.move_goal || 600,
        exercise: cfg.exercise_goal || 40,
        stand: cfg.stand_goal || 12,
    };
    let metrics = {};
    try {
        const resp = await api('/api/health/today');
        metrics = resp.metrics || {};
    } catch (_) {}
    const values = {
        move: metrics.move ? metrics.move.value : 0,
        exercise: metrics.exercise ? metrics.exercise.value : 0,
        stand: metrics.stand ? metrics.stand.value : 0,
    };
    body.innerHTML = renderRings(values, goals);
}

function renderRings(values, goals) {
    const rings = ['move', 'exercise', 'stand'].map(key => {
        const val = values[key] || 0;
        const goal = goals[key] || 1;
        return ringSvg(key, val, goal);
    }).join('');
    const legend = ['move', 'exercise', 'stand'].map(key => {
        const val = values[key] || 0;
        const goal = goals[key] || 1;
        return `
            <div class="ring-legend-item">
              <span class="swatch" style="background:${RING_COLORS[key]}"></span>
              <span class="rname">${RING_LABELS[key]}</span>
              <span class="rval">${Math.round(val)}<small>/${goal} ${RING_UNITS[key]}</small></span>
            </div>`;
    }).join('');
    return `
        <div class="ring-row">${rings}</div>
        <div class="ring-legend" style="margin-top:14px">${legend}</div>`;
}

function ringSvg(key, val, goal) {
    const r = 38;
    const c = 2 * Math.PI * r;
    const pct = goal > 0 ? Math.min(1, val / goal) : 0;
    const offset = c * (1 - pct);
    const color = RING_COLORS[key];
    return `
        <div class="ring">
          <svg width="92" height="92" viewBox="0 0 92 92">
            <circle cx="46" cy="46" r="${r}" fill="none" stroke="#212127" stroke-width="8"/>
            <circle cx="46" cy="46" r="${r}" fill="none" stroke="${color}" stroke-width="8"
              stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}"
              stroke-dashoffset="${offset.toFixed(1)}"/>
          </svg>
          <div class="ring-center">
            <div class="rv">${Math.round(val)}</div>
            <div class="rl">${RING_LABELS[key]}</div>
          </div>
        </div>`;
}

// ===== GitHub widget (heatmap) =====

async function renderGithubWidget(w, body) {
    let status = { configured: false, state: 'none' };
    try { status = await api('/api/widgets/github/status'); } catch (_) {}

    if (!status.configured) {
        body.innerHTML = `
            <div class="gh-connect-prompt">
              <div class="muted">Connect GitHub to see your contribution heatmap.</div>
              <button class="btn btn-primary" onclick="switchTab('settings')">Configure in Settings</button>
            </div>`;
        return;
    }
    if (status.state === 'pending') {
        body.innerHTML = `<div class="muted small">Waiting for GitHub authorization… finish the step in your browser.</div>`;
        return;
    }
    if (status.state === 'error') {
        body.innerHTML = `
            <div class="gh-connect-prompt">
              <div class="muted">GitHub authorization failed.</div>
              <button class="btn btn-primary" onclick="connectGithub()">Retry</button>
            </div>`;
        return;
    }

    let grid = { available: false };
    try { grid = await api('/api/widgets/github/grid'); } catch (_) {}
    if (!grid.available) {
        body.innerHTML = `
            <div class="gh-connect-prompt">
              <div class="muted">Connect GitHub to see your contribution heatmap.</div>
              <button class="btn btn-primary" onclick="connectGithub()">Connect</button>
            </div>`;
        return;
    }
    body.innerHTML = renderHeatmap(grid.data);
}

function renderHeatmap(data) {
    let weeks = [];
    let total = 0;
    let login = '';
    try {
        const cal = data.viewer.contributionsCollection.contributionCalendar;
        weeks = cal.weeks || [];
        total = cal.totalContributions || 0;
        login = data.viewer.login || '';
    } catch (_) {}

    const cols = weeks.map(week => {
        const days = (week.contributionDays || []).map(d => {
            const lvl = Math.max(0, Math.min(4, d.level || 0));
            const cls = lvl > 0 ? ` l${lvl}` : '';
            return `<div class="heat-cell${cls}" title="${escHtml(d.date)}: ${d.contributionCount}"></div>`;
        }).join('');
        return `<div class="heat-week">${days}</div>`;
    }).join('');

    return `
        <div class="heat-wrap"><div class="heatmap">${cols}</div></div>
        <div class="heat-foot">
          <span class="heat-total">${login ? escHtml(login) + ' · ' : ''}<b>${total}</b> contributions this year</span>
          <span class="heat-legend">Less
            <span class="heat-cell"></span>
            <span class="heat-cell l1"></span>
            <span class="heat-cell l2"></span>
            <span class="heat-cell l3"></span>
            <span class="heat-cell l4"></span>
          More</span>
        </div>`;
}

// ===== Calendar widget =====

let calCursor = null;   // { y, m }
let calReminders = [];
let calWidgetId = null;

let calGcal = { configured: false, events: [] };

async function renderCalendarWidget(w, body) {
    calWidgetId = w.id;
    if (!calCursor) {
        const n = new Date();
        calCursor = { y: n.getFullYear(), m: n.getMonth() };
    }
    try { calReminders = await api('/api/reminders'); } catch (_) { calReminders = []; }
    try { calGcal = await api('/api/calendar/events?days=62'); } catch (_) { calGcal = { configured: false, events: [] }; }
    drawCalendar();
}

function calBodyEl() {
    return document.querySelector(`.widget-card[data-id="${calWidgetId}"] .widget-body`);
}

function calShift(delta) {
    calCursor.m += delta;
    if (calCursor.m < 0) { calCursor.m = 11; calCursor.y--; }
    if (calCursor.m > 11) { calCursor.m = 0; calCursor.y++; }
    drawCalendar();
}

function drawCalendar() {
    const body = calBodyEl();
    if (!body) return;
    const { y, m } = calCursor;
    const monthName = new Date(y, m, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

    // Map date (YYYY-MM-DD) -> number of open reminders due that day,
    // plus Google Calendar events on their own layer.
    const events = {};
    for (const r of calReminders) {
        if (r.completed) continue;
        const d = (r.due_at || '').slice(0, 10);
        if (d) events[d] = (events[d] || 0) + 1;
    }
    const gcalDays = {};
    for (const e of (calGcal.events || [])) {
        const d = (e.start || '').slice(0, 10);
        if (d) gcalDays[d] = (gcalDays[d] || 0) + 1;
    }

    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

    const startDow = new Date(y, m, 1).getDay();
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    let cells = '';
    for (let i = 0; i < startDow; i++) cells += '<div class="cal-cell blank"></div>';
    for (let d = 1; d <= daysInMonth; d++) {
        const ds = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        const cls = 'cal-cell' + (ds === todayStr ? ' today' : '') + (events[ds] ? ' has-event' : '');
        const dots = (events[ds] ? '<span class="cal-dot"></span>' : '')
            + (gcalDays[ds] ? '<span class="cal-dot gcal"></span>' : '');
        cells += `<div class="${cls}">${d}${dots}</div>`;
    }

    // Merge reminders and calendar events into one upcoming list.
    const upcoming = calReminders
        .filter(r => !r.completed && r.due_at)
        .map(r => ({ when: r.due_at, title: r.title, kind: 'rem' }))
        .concat((calGcal.events || []).map(e => ({
            when: e.start,
            title: e.title + (e.all_day ? '' : ` · ${e.start.slice(11, 16)}`),
            kind: 'gcal',
        })))
        .sort((a, b) => (a.when > b.when ? 1 : -1))
        .slice(0, 6);
    const upHtml = upcoming.length
        ? upcoming.map(u => `
            <div class="cal-event${u.kind === 'gcal' ? ' gcal' : ''}">
              <span class="cal-event-date">${escHtml((u.when || '').slice(5, 10))}</span>
              <span class="cal-event-title">${escHtml(u.title)}</span>
            </div>`).join('')
        : '<div class="muted small">Nothing coming up.</div>';
    const connectHint = calGcal.configured ? '' :
        `<div class="muted small cal-connect">
           <a href="#" onclick="switchTab('settings');return false">Connect Google Calendar in Settings →</a>
         </div>`;

    body.innerHTML = `
        <div class="cal-nav">
          <button class="widget-kebab" title="Previous month" onclick="calShift(-1)"><svg class="ico chev cal-prev"><use href="#i-chevron"/></svg></button>
          <span class="cal-month">${escHtml(monthName)}</span>
          <button class="widget-kebab" title="Next month" onclick="calShift(1)"><svg class="ico chev cal-next"><use href="#i-chevron"/></svg></button>
        </div>
        <div class="cal-grid cal-weekdays">
          <div>S</div><div>M</div><div>T</div><div>W</div><div>T</div><div>F</div><div>S</div>
        </div>
        <div class="cal-grid">${cells}</div>
        <div class="cal-upcoming-head">Upcoming</div>
        <div class="cal-upcoming">${upHtml}</div>
        ${connectHint}`;
}

// ===== Weather widget (Open-Meteo, key-less) =====

const WEATHER_CODES = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Rime fog',
    51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
    61: 'Light rain', 63: 'Rain', 65: 'Heavy rain', 66: 'Freezing rain', 67: 'Freezing rain',
    71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
    80: 'Light showers', 81: 'Showers', 82: 'Heavy showers', 85: 'Snow showers', 86: 'Snow showers',
    95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm',
};
let weatherWidgetId = null;

async function renderWeatherWidget(w, body) {
    weatherWidgetId = w.id;
    const cfg = w.config || {};
    if (cfg.lat != null && cfg.lon != null) {
        body.innerHTML = '<div class="muted small">Loading weather…</div>';
        fetchWeather(body, cfg.lat, cfg.lon, cfg.label || '', cfg.units || 'c');
    } else {
        renderWeatherSetup(body);
    }
}

function renderWeatherSetup(body) {
    body.innerHTML = `
        <div class="wx-setup">
          <div class="muted small">Set a location to see the forecast.</div>
          <button class="btn btn-primary" onclick="wxUseMyLocation()">Use my location</button>
          <div class="wx-city-row">
            <input type="text" id="wx-city-input" placeholder="Search city…" onkeydown="if(event.key==='Enter')wxSearchCity()">
            <button class="btn btn-ghost" onclick="wxSearchCity()">Search</button>
          </div>
          <div id="wx-city-results"></div>
        </div>`;
}

function wxUseMyLocation() {
    if (!navigator.geolocation) { alert('Geolocation is not supported by this browser.'); return; }
    navigator.geolocation.getCurrentPosition(
        pos => wxSaveLocation(pos.coords.latitude, pos.coords.longitude, 'Current location'),
        err => alert('Could not get your location: ' + err.message),
        { timeout: 10000 }
    );
}

async function wxSearchCity() {
    const input = document.getElementById('wx-city-input');
    const res = document.getElementById('wx-city-results');
    const q = input ? input.value.trim() : '';
    if (!q || !res) return;
    res.innerHTML = '<div class="muted small">Searching…</div>';
    try {
        const r = await fetch(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(q)}&count=5&language=en&format=json`);
        const data = await r.json();
        const results = data.results || [];
        if (!results.length) { res.innerHTML = '<div class="muted small">No matches found.</div>'; return; }
        res.innerHTML = results.map((c, i) => `
            <div class="wx-city-item" onclick="wxPickCity(${i})">
              ${escHtml(c.name)}${c.admin1 ? ', ' + escHtml(c.admin1) : ''}${c.country ? ' · ' + escHtml(c.country) : ''}
            </div>`).join('');
        res._results = results;
    } catch (_) {
        res.innerHTML = '<div class="muted small">City search failed. Check your connection.</div>';
    }
}

function wxPickCity(i) {
    const res = document.getElementById('wx-city-results');
    const c = res && res._results ? res._results[i] : null;
    if (c) wxSaveLocation(c.latitude, c.longitude, c.name || '');
}

async function wxSaveLocation(lat, lon, label) {
    try {
        await api(`/api/widgets/${weatherWidgetId}`, { method: 'PUT', body: JSON.stringify({ config: { lat, lon, label } }) });
    } catch (e) { alert('Could not save location: ' + e.message); return; }
    loadDashboard();
}

async function fetchWeather(body, lat, lon, label, units) {
    const unit = units === 'f' ? 'fahrenheit' : 'celsius';
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}` +
        `&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m` +
        `&daily=temperature_2m_max,temperature_2m_min,weather_code&timezone=auto&forecast_days=5&temperature_unit=${unit}`;
    try {
        const r = await fetch(url);
        const data = await r.json();
        body.innerHTML = renderWeatherData(data, label, units);
    } catch (_) {
        body.innerHTML = `
            <div class="wx-setup">
              <div class="muted small">Could not load weather.</div>
              <button class="btn btn-ghost" onclick="wxChangeLocation()">Change location</button>
            </div>`;
    }
}

function renderWeatherData(data, label, units) {
    const cur = data.current || {};
    const daily = data.daily || {};
    const deg = units === 'f' ? '°F' : '°C';
    const cond = WEATHER_CODES[cur.weather_code] || '—';
    const days = (daily.time || []).map((d, i) => `
        <div class="wx-day">
          <span class="wx-day-name">${escHtml(new Date(d + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' }))}</span>
          <span class="wx-day-hi">${Math.round(daily.temperature_2m_max[i])}°</span>
          <span class="wx-day-lo">${Math.round(daily.temperature_2m_min[i])}°</span>
        </div>`).join('');
    return `
        <div class="wx-top">
          <div class="wx-temp">${Math.round(cur.temperature_2m)}<span class="wx-deg">${deg}</span></div>
          <div class="wx-meta">
            <div class="wx-cond">${escHtml(cond)}</div>
            <div class="wx-loc">${escHtml(label || 'Your location')}</div>
          </div>
        </div>
        <div class="wx-details">
          <span>Feels ${Math.round(cur.apparent_temperature)}°</span>
          <span>Humidity ${cur.relative_humidity_2m ?? '—'}%</span>
          <span>Wind ${Math.round(cur.wind_speed_10m || 0)} km/h</span>
        </div>
        <div class="wx-forecast">${days}</div>
        <div class="wx-actions">
          <button class="wx-toggle" onclick="wxToggleUnits()">${units === 'f' ? 'Switch to °C' : 'Switch to °F'}</button>
          <button class="wx-toggle" onclick="wxChangeLocation()">Change location</button>
        </div>`;
}

async function wxToggleUnits() {
    let units = 'c';
    try {
        const widgets = (await api('/api/widgets')).widgets || [];
        const w = widgets.find(x => x.id === weatherWidgetId);
        units = (w && w.config && w.config.units === 'f') ? 'c' : 'f';
        await api(`/api/widgets/${weatherWidgetId}`, { method: 'PUT', body: JSON.stringify({ config: { units } }) });
    } catch (_) { return; }
    loadDashboard();
}

function wxChangeLocation() {
    const body = document.querySelector(`.widget-card[data-id="${weatherWidgetId}"] .widget-body`);
    if (body) renderWeatherSetup(body);
}

// ===== Quick Notes widget (autosaves to Notes) =====

let qnSaveTimer = null;

async function renderQuickNotesWidget(w, body) {
    const cfg = w.config || {};
    let noteId = cfg.note_id || '';
    let content = '';
    if (noteId) {
        try { content = (await api(`/api/notes/${noteId}`)).body || ''; }
        catch (_) { noteId = ''; }
    }
    if (!noteId) {
        try {
            const n = await api('/api/notes', { method: 'POST', body: JSON.stringify({ title: 'Quick note', content: '' }) });
            noteId = n.id;
            await api(`/api/widgets/${w.id}`, { method: 'PUT', body: JSON.stringify({ config: { note_id: noteId } }) });
        } catch (_) {
            body.innerHTML = '<div class="muted small">Could not create a note.</div>';
            return;
        }
    }
    body.innerHTML = `
        <textarea class="qn-area" id="qn-textarea" placeholder="Jot something down…">${escHtml(content)}</textarea>
        <div class="qn-foot"><span class="qn-status muted small" id="qn-status">Saved</span></div>`;
    const ta = document.getElementById('qn-textarea');
    if (ta) ta.addEventListener('input', () => qnScheduleSave(noteId));
}

function qnScheduleSave(noteId) {
    const status = document.getElementById('qn-status');
    if (status) status.textContent = 'Editing…';
    if (qnSaveTimer) clearTimeout(qnSaveTimer);
    qnSaveTimer = setTimeout(() => qnSave(noteId), 800);
}

async function qnSave(noteId) {
    const ta = document.getElementById('qn-textarea');
    const status = document.getElementById('qn-status');
    if (!ta) return;
    try {
        await api(`/api/notes/${noteId}`, { method: 'PUT', body: JSON.stringify({ content: ta.value }) });
        if (status) status.textContent = 'Saved';
    } catch (_) {
        if (status) status.textContent = 'Save failed';
    }
}

// ===== Widget add / remove =====

async function removeWidget(id) {
    if (!confirm('Remove this widget?')) return;
    try {
        await api(`/api/widgets/${id}`, { method: 'DELETE' });
        loadDashboard();
    } catch (e) { alert('Could not remove widget: ' + e.message); }
}

// ===== Catalog modal =====

async function openCatalog() {
    const modal = document.getElementById('widget-catalog');
    modal.classList.remove('hidden');
    const setup = document.getElementById('catalog-setup');
    setup.classList.add('hidden');
    setup.innerHTML = '';
    await renderCatalog();
}

function closeCatalog() {
    document.getElementById('widget-catalog').classList.add('hidden');
}

async function renderCatalog() {
    const list = document.getElementById('catalog-list');
    let catalog = [];
    try { catalog = (await api('/api/widgets/catalog')).catalog || []; } catch (_) {}
    const icons = { fitness: 'i-activity', github: 'i-github', calendar: 'i-calendar', weather: 'i-cloud', quicknotes: 'i-note' };
    list.innerHTML = catalog.map(item => {
        const btn = item.installed
            ? '<button class="btn btn-ghost" disabled>Added</button>'
            : `<button class="btn btn-primary" onclick="addWidget('${item.type}')">Add</button>`;
        return `
            <div class="catalog-item">
              <div class="catalog-ico"><svg class="ico"><use href="#${icons[item.type] || 'i-grid'}"/></svg></div>
              <div class="catalog-info">
                <div class="cname">${escHtml(item.name)}</div>
                <div class="cdesc">${escHtml(item.desc || '')}</div>
              </div>
              ${btn}
            </div>`;
    }).join('') || '<div class="muted small">No widgets available.</div>';
}

async function addWidget(type) {
    try {
        await api('/api/widgets', { method: 'POST', body: JSON.stringify({ type }) });
    } catch (e) { alert('Could not add widget: ' + e.message); return; }
    await renderCatalog();
    loadDashboard();

    const setup = document.getElementById('catalog-setup');
    if (type === 'fitness') {
        const url = `${location.origin}/api/health/sync`;
        setup.innerHTML = `
            <div><b>Fitness connected.</b> Point your iPhone Shortcut at:</div>
            <pre>POST ${escHtml(url)}</pre>
            <pre>{ "move": 420, "exercise_minutes": 35, "stand_hours": 10 }</pre>
            <div class="muted small">Rings appear as soon as the first sync lands.</div>
            <button class="btn btn-ghost" onclick="simulateSync()">Send a test sync</button>`;
        setup.classList.remove('hidden');
    } else if (type === 'github') {
        setup.innerHTML = '<div class="muted small">Starting GitHub authorization…</div>';
        setup.classList.remove('hidden');
        startGithubAuth(true);
    }
}

async function simulateSync() {
    try {
        await api('/api/health/sync', {
            method: 'POST',
            body: JSON.stringify({ move: 420, exercise_minutes: 35, stand_hours: 10 }),
        });
        loadDashboard();
        alert('Test sync sent — check your rings.');
    } catch (e) { alert('Sync failed: ' + e.message); }
}

// ===== GitHub device flow (shared by widget + settings) =====

async function startGithubAuth(fromCatalog) {
    let result;
    try {
        result = await api('/api/widgets/github/auth', { method: 'POST' });
    } catch (e) {
        const setup = document.getElementById('catalog-setup');
        if (fromCatalog && setup) {
            setup.innerHTML = `<div class="muted">${escHtml(e.message)}</div>
                <button class="btn btn-ghost" onclick="switchTab('settings');closeCatalog()">Open Settings</button>`;
        } else {
            setGhStatus(e.message, 'err');
        }
        return;
    }
    // Show the user code in both the catalog and settings UI.
    const setup = document.getElementById('catalog-setup');
    if (fromCatalog && setup) {
        setup.innerHTML = `
            <div>A browser tab opened. Enter this code to authorize:</div>
            <div class="gh-code">${escHtml(result.user_code || '')}</div>
            <div class="muted small" style="margin-top:8px">Waiting for authorization…</div>`;
    }
    const codeBox = document.getElementById('gh-code-box');
    const codeEl = document.getElementById('gh-user-code');
    if (codeBox && codeEl) {
        codeEl.textContent = result.user_code || '';
        codeBox.classList.remove('hidden');
    }
    setGhStatus('Waiting for authorization…', 'warn');
    pollGithubStatus(fromCatalog);
}

function pollGithubStatus(fromCatalog) {
    if (ghPollTimer) clearInterval(ghPollTimer);
    let tries = 0;
    ghPollTimer = setInterval(async () => {
        tries += 1;
        let status;
        try { status = await api('/api/widgets/github/status'); } catch (_) { return; }
        if (status.state === 'authorized') {
            clearInterval(ghPollTimer); ghPollTimer = null;
            setGhStatus(`Connected as ${status.login || 'you'}`, 'ok');
            const codeBox = document.getElementById('gh-code-box');
            if (codeBox) codeBox.classList.add('hidden');
            if (fromCatalog) { closeCatalog(); }
            loadDashboard();
        } else if (status.state === 'error' || tries > 120) {
            clearInterval(ghPollTimer); ghPollTimer = null;
            setGhStatus('Authorization failed or timed out.', 'err');
        }
    }, 3000);
}

// ===== Settings =====

// ===== Your apps: Obsidian vault + editor detection =====

async function loadInteropSettings() {
    let s;
    try { s = await api('/api/interop/status'); } catch (_) { return; }
    const input = document.getElementById('obsidian-vault');
    if (input && !input.value) input.value = s.vault_path || '';
    const bits = [];
    bits.push(s.vault_ok ? 'Obsidian vault connected'
        : (s.vault_path ? 'Vault folder not found' : 'No vault set'));
    if ((s.editors || []).length) {
        bits.push(`Editor: ${s.editors[0] === 'cursor' ? 'Cursor' : 'VS Code'} detected`);
    } else {
        bits.push('No editor CLI found (install VS Code or Cursor)');
    }
    const el = document.getElementById('interop-status');
    if (el) { el.textContent = bits.join(' · '); el.className = 'settings-status' + (s.vault_ok ? ' ok' : ''); }
}

async function saveVaultPath() {
    const path = document.getElementById('obsidian-vault').value.trim();
    try {
        await api('/api/interop/vault', { method: 'PUT', body: JSON.stringify({ vault_path: path }) });
        loadInteropSettings();
    } catch (e) {
        const el = document.getElementById('interop-status');
        if (el) { el.textContent = e.message; el.className = 'settings-status err'; }
    }
}

async function importObsidian() {
    const el = document.getElementById('interop-status');
    if (el) el.textContent = 'Importing your vault…';
    try {
        const r = await api('/api/interop/obsidian/import', { method: 'POST' });
        if (el) {
            el.textContent = `Imported ${r.imported} new, updated ${r.updated}, unchanged ${r.skipped}.`;
            el.className = 'settings-status ok';
        }
    } catch (e) {
        if (el) { el.textContent = e.message; el.className = 'settings-status err'; }
    }
}

// ===== Google Calendar settings (secret iCal address — no OAuth) =====

function setCalStatus(text, cls) {
    const el = document.getElementById('cal-status');
    if (el) { el.textContent = text; el.className = 'settings-status' + (cls ? ' ' + cls : ''); }
}

async function loadCalendarSettings() {
    let s;
    try { s = await api('/api/calendar/status'); } catch (_) { return; }
    const en = document.getElementById('cal-enabled');
    const aw = document.getElementById('cal-agent-aware');
    if (en) en.checked = !!s.enabled;
    if (aw) aw.checked = !!s.agent_aware;
    if (s.url_set) {
        setCalStatus(`Connected (${s.url_hint})` + (s.agent_aware ? ' · assistant can see it' : ''), 'ok');
    } else {
        setCalStatus('Not connected');
    }
}

async function saveCalendarUrl() {
    const input = document.getElementById('cal-ics-url');
    const url = input.value.trim();
    if (!url) { setCalStatus('Paste the secret address first.', 'err'); return; }
    setCalStatus('Checking your calendar…');
    try {
        await api('/api/calendar/config', { method: 'PUT', body: JSON.stringify({ ics_url: url }) });
        const test = await api('/api/calendar/refresh', { method: 'POST' });
        if (!test.ok) { setCalStatus(test.detail || 'Could not read that address.', 'err'); return; }
        input.value = '';
        setCalStatus(`Connected — ${test.count} event${test.count === 1 ? '' : 's'} in the next 2 weeks.`, 'ok');
        loadCalendarSettings();
        loadDashboard();
    } catch (e) {
        setCalStatus(e.message, 'err');
    }
}

async function disconnectCalendar() {
    try {
        await api('/api/calendar/config', { method: 'PUT', body: JSON.stringify({ ics_url: '', enabled: false }) });
        setCalStatus('Not connected');
        loadDashboard();
    } catch (e) { setCalStatus(e.message, 'err'); }
}

async function saveCalendarToggles() {
    const enabled = document.getElementById('cal-enabled').checked;
    const agentAware = document.getElementById('cal-agent-aware').checked;
    try {
        await api('/api/calendar/config', {
            method: 'PUT',
            body: JSON.stringify({ enabled, agent_aware: agentAware }),
        });
        loadCalendarSettings();
    } catch (e) { setCalStatus(e.message, 'err'); }
}

async function loadSettings() {
    let cfg = {};
    try { cfg = await api('/api/config'); } catch (_) {}
    const cid = document.getElementById('gh-client-id');
    if (cid) cid.value = cfg.github_client_id || '';
    const uname = document.getElementById('user-name');
    if (uname) uname.value = cfg.user_name || '';
    window.__userName = cfg.user_name || '';

    const endpoint = document.getElementById('health-endpoint');
    if (endpoint) endpoint.textContent = `POST ${location.origin}/api/health/sync`;

    const cloudToggle = document.getElementById('cloud-enabled');
    if (cloudToggle) cloudToggle.checked = !!cfg.cloud_enabled;
    loadCalendarSettings();
    loadInteropSettings();
    if (typeof loadRouting === 'function') loadRouting();
    if (typeof loadBackups === 'function') loadBackups();
    if (typeof loadAuthPanel === 'function') loadAuthPanel();
    if (typeof loadMediaPanel === 'function') loadMediaPanel();
    if (typeof loadHooksPanel === 'function') loadHooksPanel();
    if (typeof loadConsensusPanel === 'function') loadConsensusPanel();

    const toggle = document.getElementById('recap-auto-toggle');
    if (toggle) toggle.checked = !!cfg.recap_auto_enabled;
    const rtime = document.getElementById('recap-auto-time');
    if (rtime) rtime.value = cfg.recap_auto_time || '04:00';
    const last = document.getElementById('recap-last-run');
    if (last) last.textContent = cfg.recap_auto_last_run ? `Last run: ${cfg.recap_auto_last_run}` : '';

    refreshGhStatus();
}

async function refreshGhStatus() {
    let status = { configured: false, state: 'none' };
    try { status = await api('/api/widgets/github/status'); } catch (_) {}
    if (!status.configured) setGhStatus('Not configured', '');
    else if (status.state === 'authorized') setGhStatus(`Connected as ${status.login || 'you'}`, 'ok');
    else if (status.state === 'pending') setGhStatus('Waiting for authorization…', 'warn');
    else if (status.state === 'error') setGhStatus('Authorization error', 'err');
    else setGhStatus('Configured — not connected', '');
}

function setGhStatus(text, cls) {
    const el = document.getElementById('gh-status');
    if (!el) return;
    el.textContent = text;
    el.className = 'settings-status' + (cls ? ' ' + cls : '');
}

async function saveGithubClientId() {
    const val = document.getElementById('gh-client-id').value.trim();
    try {
        await api('/api/config/github_client_id', { method: 'PUT', body: JSON.stringify(val) });
        refreshGhStatus();
    } catch (e) { alert('Could not save: ' + e.message); }
}

async function saveUserName() {
    const val = document.getElementById('user-name').value.trim();
    try {
        await api('/api/config/user_name', { method: 'PUT', body: JSON.stringify(val) });
        window.__userName = val;
        renderGreeting();
    } catch (e) { alert('Could not save: ' + e.message); }
}

function connectGithub() { startGithubAuth(false); }

async function disconnectGithub() {
    try {
        await api('/api/widgets/github', { method: 'DELETE' });
        const codeBox = document.getElementById('gh-code-box');
        if (codeBox) codeBox.classList.add('hidden');
        refreshGhStatus();
        loadDashboard();
    } catch (e) { alert('Could not disconnect: ' + e.message); }
}

// ===== Nav helpers =====

function toggleNavMore() {
    const list = document.getElementById('nav-more-list');
    const btn = document.querySelector('.nav-more');
    list.classList.toggle('hidden');
    btn.classList.toggle('open');
}

function topSearch() {
    const val = document.getElementById('top-search-input').value.trim();
    if (!val) return;
    switchTab('search');
    const input = document.getElementById('search-input');
    if (input) { input.value = val; }
    if (typeof doSearch === 'function') doSearch();
}
