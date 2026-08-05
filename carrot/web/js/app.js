// ===== Carrot AI — workspace frontend =====
let currentTab = 'dashboard';
let currentConversationId = null;
let currentModel = null;
// The provider that serves `currentModel`. Sent with every turn so the server
// never has to guess: a name like "mistral-medium" is a hosted model to one
// provider and a pulled tag to Ollama, and guessing wrong routed chat to a
// model that was not there.
let currentProvider = null;
// A chat that is answered but not remembered. Per-conversation rather than a
// global setting, because the reason to want one is usually a single question
// rather than a change of policy.
let temporaryChat = false;
let speakReplies = false;
let mediaRecorder = null;
let recordedChunks = [];
let isRecording = false;
let isPulling = false;
let activeSkill = null;      // {slug, name} when a skill is armed for the next message
let skillCatalog = [];       // cached list of skills for the picker
let recapCfg = { enabled: false, time: '04:00', last_run: '' };  // overnight recap settings

// ===== Utilities =====
function escHtml(str) {
    const d = document.createElement('div');
    d.textContent = str == null ? '' : String(str);
    return d.innerHTML;
}

// The backend injects the session token into this page's <head>. It is the
// only way to obtain it, and the same-origin policy is what keeps another
// origin from reading it — so every API call has to carry it.
const CARROT_TOKEN = (document.querySelector('meta[name="carrot-token"]') || {}).content || '';

function authHeaders(extra = {}) {
    const headers = { 'Content-Type': 'application/json', ...extra };
    if (CARROT_TOKEN) headers['X-Carrot-Token'] = CARROT_TOKEN;
    return headers;
}

// EventSource cannot set headers, so SSE URLs carry the token as a query param.
function tokenUrl(path) {
    if (!CARROT_TOKEN) return path;
    return path + (path.includes('?') ? '&' : '?') + 'carrot_token=' + encodeURIComponent(CARROT_TOKEN);
}

async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: authHeaders(options.headers || {}),
    });
    if (!resp.ok) {
        let detail = resp.statusText;
        let raw = null;
        try {
            const d = (await resp.json()).detail;
            raw = d;
            if (typeof d === 'string') detail = d;
            else if (d && d.message) detail = d.message;
            else if (d) detail = Array.isArray(d) ? (d[0] && d[0].msg ? d.map(x => x.msg).join('; ') : JSON.stringify(d)) : String(d);
        } catch (_) {}
        // Carry the status and the structured detail: a 428 is the backend
        // asking for confirmation, not a failure, and the caller needs the
        // reasons to show. Losing them turned an object detail into
        // "[object Object]".
        const err = new Error(detail);
        err.status = resp.status;
        err.detail = raw;
        throw err;
    }
    return resp.json();
}

function fmtBytes(n) {
    if (!n) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + units[i];
}

function dueLabel(dueAt) {
    if (!dueAt) return { text: '', urgent: false };
    const due = new Date(dueAt);
    if (isNaN(due)) return { text: '', urgent: false };
    const days = Math.ceil((due - Date.now()) / 86400000);
    if (days < 0) return { text: 'OVERDUE', urgent: true };
    if (days === 0) return { text: 'TODAY', urgent: true };
    if (days === 1) return { text: 'TOMORROW', urgent: false };
    return { text: days + ' DAYS', urgent: false };
}

// ===== Tabs =====
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const el = document.getElementById(`view-${tab}`);
    if (el) el.classList.add('active');
    document.querySelectorAll('.app-nav .nav-item').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    // Auto-expand "More" when one of its sub-sections is active.
    const moreList = document.getElementById('nav-more-list');
    if (moreList && moreList.querySelector(`.nav-item[data-tab="${tab}"]`)) {
        moreList.classList.remove('hidden');
        const moreBtn = document.querySelector('.nav-more');
        if (moreBtn) moreBtn.classList.add('open');
    }
    // The chat command bar only belongs to the Conversations view.
    const cmdbar = document.getElementById('cmdbar');
    if (cmdbar) cmdbar.classList.toggle('hidden', tab !== 'workspace');
    const loaders = {
        dashboard: loadDashboard,
        workspace: loadWorkspace,
        settings: loadSettings,
        chats: loadConversations,
        notes: loadNotes,
        code: loadCodeTab,
        planner: loadPlanner,
        goals: loadGoals,
        reminders: loadReminders,
        assignments: loadAssignments,
        extensions: loadExtensions,
        hub: loadHub,
        research: () => loadResearch(),
        agent: () => loadAgent(),
        workspaces: () => loadWorkspaces(),
        help: () => loadHelp(),
        leaderboard: loadLeaderboard,
        memory: () => loadMemory(),
        files: () => loadIndex(),
        inbox: () => refreshNotifications(),
    };
    if (loaders[tab]) loaders[tab]();
}

function focusCmd() {
    document.getElementById('cmd-input').focus();
}

// ===== Skill picker (command bar) =====
async function loadSkillCatalog() {
    try {
        skillCatalog = await api('/api/skills');
    } catch (_) {
        skillCatalog = [];
    }
}

function cmdKeydown(event) {
    const pop = document.getElementById('skill-pop');
    const popOpen = !pop.classList.contains('hidden');
    if (event.key === 'Escape' && popOpen) { hideSkillPop(); return; }
    if (event.key === 'Enter') {
        if (popOpen) {
            const first = pop.querySelector('.skill-opt');
            if (first) { first.click(); return; }
        }
        sendChat();
    }
}

function cmdInputChanged() {
    const input = document.getElementById('cmd-input');
    const val = input.value;
    if (val.startsWith('/')) {
        showSkillPop(val.slice(1).trim().toLowerCase());
    } else {
        hideSkillPop();
    }
}

function showSkillPop(filter) {
    const pop = document.getElementById('skill-pop');
    const list = document.getElementById('skill-pop-list');
    const matches = skillCatalog.filter(s =>
        !filter || s.name.toLowerCase().includes(filter) || (s.description || '').toLowerCase().includes(filter));
    list.innerHTML = '';
    if (!skillCatalog.length) {
        list.innerHTML = '<div class="empty" style="padding:6px 10px">No skills yet. Create one in Extensions.</div>';
    } else if (!matches.length) {
        list.innerHTML = '<div class="empty" style="padding:6px 10px">No matching skills.</div>';
    } else {
        for (const s of matches) {
            const row = document.createElement('div');
            row.className = 'skill-opt';
            row.innerHTML = `<span class="m-name">${escHtml(s.name)}</span><span class="m-meta">${escHtml((s.description || '').slice(0, 40))}</span>`;
            row.onclick = () => pickSkill(s);
            list.appendChild(row);
        }
    }
    pop.classList.remove('hidden');
}

function hideSkillPop() {
    document.getElementById('skill-pop').classList.add('hidden');
}

function pickSkill(skill) {
    activeSkill = { slug: skill.slug, name: skill.name };
    const badge = document.getElementById('active-skill');
    document.getElementById('active-skill-name').textContent = skill.name;
    badge.classList.remove('hidden');
    const input = document.getElementById('cmd-input');
    input.value = '';
    hideSkillPop();
    input.focus();
}

function clearActiveSkill() {
    activeSkill = null;
    document.getElementById('active-skill').classList.add('hidden');
}

// ===== Status / engine =====
async function showBuildVersion() {
    try {
        const h = await api('/api/health');
        const el = document.getElementById('brand-sub');
        if (el && h.version) {
            el.textContent = `v${h.version}`;
            el.title = `Carrot ${h.version} · assets ${h.assets || '?'}`;
        }
    } catch (_) { /* leave the placeholder */ }
}

async function refreshStatus() {
    const dot = document.getElementById('engine-dot');
    const label = document.getElementById('engine-label');
    try {
        const s = await api('/api/status');
        const ok = s.ollama_available && s.model_loaded;
        dot.className = 'dot ' + (ok ? 'ok' : (s.ollama_available ? 'warn' : 'err'));
        label.textContent = ok ? 'Local Engine Active'
            : (s.ollama_available ? 'Model missing' : 'Engine offline');
        renderEngineCard(s);
        return s;
    } catch (e) {
        dot.className = 'dot err';
        label.textContent = 'Server unreachable';
        return null;
    }
}

function renderEngineCard(s) {
    const el = document.getElementById('card-engine');
    if (!el || !s) return;
    const on = recapCfg.enabled;
    el.innerHTML = `
        <div class="engine-row"><span class="dot ${s.ollama_available ? 'ok' : 'err'}"></span><span class="name">Ollama</span><span class="val">${s.ollama_available ? 'running' : 'offline'}</span></div>
        <div class="engine-row"><span class="dot ${s.model_loaded ? 'ok' : 'warn'}"></span><span class="name">Model</span><span class="val">${escHtml(currentModel || s.default_model)}</span></div>
        <div class="engine-row"><span class="dot ok"></span><span class="name">Conversations</span><span class="val">${s.conversations}</span></div>
        <div class="engine-row"><span class="dot ok"></span><span class="name">Messages</span><span class="val">${s.messages}</span></div>
        <div class="engine-auto">
            <label class="switch-row">
                <input type="checkbox" ${on ? 'checked' : ''} onchange="setRecapAuto(this.checked)">
                <span>Overnight briefing</span>
                <input type="time" class="auto-time" value="${escHtml(recapCfg.time || '04:00')}" onchange="setRecapTime(this.value)">
            </label>
            <div class="auto-hint">Auto-runs the deep-research recap daily at this time while Carrot is open${recapCfg.last_run ? ' · last: ' + escHtml(recapCfg.last_run) : ''}.</div>
        </div>`;
}

async function loadRecapConfig() {
    try {
        const cfg = await api('/api/config');
        recapCfg.enabled = !!cfg.recap_auto_enabled;
        recapCfg.time = cfg.recap_auto_time || '04:00';
        recapCfg.last_run = cfg.recap_auto_last_run || '';
    } catch (_) {}
}

async function setRecapAuto(enabled) {
    recapCfg.enabled = enabled;
    try {
        await api('/api/config/recap_auto_enabled', { method: 'PUT', body: JSON.stringify(enabled) });
    } catch (e) { alert('Could not save setting: ' + e.message); }
    refreshStatus();
}

async function setRecapTime(value) {
    recapCfg.time = value;
    try {
        await api('/api/config/recap_auto_time', { method: 'PUT', body: JSON.stringify(value) });
    } catch (e) { alert('Could not save setting: ' + e.message); }
}

// ===== Model picker =====
async function loadModels() {
    try {
        const data = await api('/api/models');
        // The label has to show what chat *actually* runs on. `active_model` is
        // only the Ollama default, so reading it here made a pinned cloud model
        // silently revert to the local one in the picker on every refresh.
        if (data.chat_local === false && data.chat_model) {
            currentModel = data.chat_model;
            currentProvider = data.chat_provider || null;
        } else {
            currentModel = data.active_model;
            currentProvider = 'ollama';
        }
        document.getElementById('model-label').textContent = currentModel;
        renderModelPop(data);
        renderEmptyStateLine();
    } catch (_) {
        document.getElementById('model-label').textContent = 'no engine';
    }
}

function renderModelPop(data) {
    const installedEl = document.getElementById('model-installed');
    const suggestedEl = document.getElementById('model-suggested');
    const remoteEl = document.getElementById('model-remote');
    installedEl.innerHTML = '';
    suggestedEl.innerHTML = '';
    if (remoteEl) remoteEl.innerHTML = '';

    // A local model is "current" only when chat isn't pinned to a provider.
    const localActive = data.chat_local !== false ? data.active_model : null;

    if (!data.installed.length) {
        installedEl.innerHTML = '<div class="empty" style="padding:4px 9px">No models installed yet.</div>';
    }
    for (const m of data.installed) {
        const row = document.createElement('div');
        row.className = 'model-row' + (m.name === localActive ? ' active' : '');
        row.innerHTML = `
            <span class="m-name">${escHtml(m.name)}</span>
            <span class="m-meta">${escHtml(m.parameter_size || '')} ${fmtBytes(m.size)}</span>
            ${m.name === localActive ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
        row.onclick = () => selectModel(m.name);
        installedEl.appendChild(row);
    }

    // Models from providers you've configured — the key is already saved,
    // so they belong in the same picker as the local ones.
    if (remoteEl) {
        for (const group of (data.remote || [])) {
            const head = document.createElement('div');
            head.className = 'pop-section';
            head.textContent = group.label;
            remoteEl.appendChild(head);

            for (const name of group.models) {
                const isActive = data.chat_local === false
                    && data.chat_provider === group.provider && data.chat_model === name;
                const row = document.createElement('div');
                row.className = 'model-row' + (isActive ? ' active' : '');
                row.innerHTML = `
                    <span class="m-name">${escHtml(name)}</span>
                    <span class="m-meta">cloud</span>
                    ${isActive ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
                row.onclick = () => selectRemoteModel(group.provider, name);
                remoteEl.appendChild(row);
            }

            // Listing can fail while the provider still works fine. Say why,
            // and let the model be named by hand instead of dead-ending.
            if (group.error) {
                const why = document.createElement('div');
                why.className = 'model-note';
                why.textContent = /401|403|unauthor/i.test(group.error)
                    ? 'Key rejected — check it in Settings → Providers.'
                    : `Could not list models: ${group.error}`.slice(0, 120);
                remoteEl.appendChild(why);
            }
            if (group.error || !group.models.length) {
                const row = document.createElement('div');
                row.className = 'pop-custom';
                row.innerHTML = `
                    <input type="text" placeholder="type a ${escHtml(group.label)} model name"
                           id="remote-custom-${escHtml(group.provider)}">
                    <button class="btn btn-ghost">Use</button>`;
                const input = row.querySelector('input');
                const use = () => {
                    const name = input.value.trim();
                    if (name) selectRemoteModel(group.provider, name);
                };
                input.onkeydown = (e) => { if (e.key === 'Enter') use(); };
                row.querySelector('button').onclick = use;
                remoteEl.appendChild(row);
            }
        }
    }

    const notInstalled = data.suggested.filter(m => !m.installed);
    if (!notInstalled.length) {
        suggestedEl.innerHTML = '<div class="empty" style="padding:4px 9px">All suggestions installed.</div>';
    }
    for (const m of notInstalled) {
        const row = document.createElement('div');
        row.className = 'model-row';
        row.style.cursor = 'default';
        row.innerHTML = `
            <span class="m-name" title="${escHtml(m.blurb)}">${escHtml(m.name)}</span>
            <span class="m-meta">${escHtml(m.size_hint)}</span>`;
        const btn = document.createElement('button');
        btn.className = 'm-install';
        btn.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>Install';
        btn.onclick = (e) => { e.stopPropagation(); pullModel(m.name); };
        row.appendChild(btn);
        suggestedEl.appendChild(row);
    }
}

// Popovers above the command bar are clamped to the space that actually
// exists. A fixed max-height ran off the top of the screen on short
// windows, leaving options you could see but never scroll to.
function fitPopoverAbove(popId, anchorId, gap = 10, floor = 170) {
    const pop = document.getElementById(popId);
    const anchor = document.getElementById(anchorId);
    if (!pop || !anchor) return;
    const room = anchor.getBoundingClientRect().top - gap - 14;
    pop.style.maxHeight = Math.max(floor, Math.min(460, room)) + 'px';
}

function toggleModelPop() {
    const pop = document.getElementById('model-pop');
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) fitPopoverAbove('model-pop', 'model-btn');
}

// Re-clamp on resize so a popover left open stays reachable.
window.addEventListener('resize', () => {
    if (!document.getElementById('model-pop')?.classList.contains('hidden')) {
        fitPopoverAbove('model-pop', 'model-btn');
    }
    if (!document.getElementById('search-pop')?.classList.contains('hidden')) {
        fitPopoverAbove('search-pop', 'search-btn');
    }
});

// Picking a cloud model pins the 'chat' task to that provider — the same
// mechanism the Task Routing table uses, so the two never disagree.
async function selectRemoteModel(provider, model) {
    try {
        await api('/api/router/route', {
            method: 'PUT',
            body: JSON.stringify({ task: 'chat', provider, model }),
        });
        currentModel = model;
        currentProvider = provider;
        document.getElementById('model-label').textContent = model;
        renderEmptyStateLine();
        document.getElementById('model-pop').classList.add('hidden');
        loadModels();
        refreshStatus();
        if (typeof loadRouting === 'function') loadRouting();
    } catch (e) {
        alert('Could not switch to that model: ' + e.message);
    }
}

async function selectModel(name) {
    try {
        // Selecting a local model also releases any cloud pin on chat.
        await api('/api/router/route/chat', { method: 'DELETE' }).catch(() => {});
        await api('/api/models/select', { method: 'POST', body: JSON.stringify({ model: name }) });
        currentModel = name;
        currentProvider = 'ollama';
        document.getElementById('model-label').textContent = name;
        renderEmptyStateLine();
        document.getElementById('model-pop').classList.add('hidden');
        loadModels();
        refreshStatus();
    } catch (e) {
        alert('Could not select model: ' + e.message);
    }
}

function pullCustomModel() {
    const input = document.getElementById('model-custom');
    const name = input.value.trim();
    if (!name) return;
    input.value = '';
    pullModel(name);
}

async function pullModel(name) {
    if (isPulling) return;
    isPulling = true;
    const wrap = document.getElementById('pull-progress');
    const label = document.getElementById('pull-label');
    const bar = document.getElementById('pull-bar');
    wrap.classList.remove('hidden');
    label.textContent = `pulling ${name}…`;
    bar.style.width = '2%';
    try {
        const resp = await fetch('/api/models/pull', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ model: name }),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const p = JSON.parse(raw.slice(5).trim());
                if (p.error) throw new Error(p.error);
                if (p.total && p.completed != null) {
                    const pct = Math.round((p.completed / p.total) * 100);
                    bar.style.width = pct + '%';
                    label.textContent = `${name} — ${p.status} ${pct}% (${fmtBytes(p.completed)} / ${fmtBytes(p.total)})`;
                } else if (p.status) {
                    label.textContent = `${name} — ${p.status}`;
                }
                if (p.done) {
                    bar.style.width = '100%';
                    label.textContent = `${name} installed`;
                }
            }
        }
        await loadModels();
        setTimeout(() => wrap.classList.add('hidden'), 2500);
    } catch (e) {
        label.textContent = `failed: ${e.message}`;
        bar.style.width = '0';
        setTimeout(() => wrap.classList.add('hidden'), 5000);
    } finally {
        isPulling = false;
    }
}

// ===== Chat (streaming) =====
function clearChatEmpty() {
    const empty = document.getElementById('chat-empty');
    if (empty) empty.remove();
}

function appendMessage(role, content) {
    clearChatEmpty();
    const messagesEl = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = `message ${role}`;
    const body = role === 'assistant' && content
        ? `<div class="content md">${mdToHtml(content)}</div>`
        : `<div class="content">${escHtml(content)}</div>`;
    div.innerHTML = `<div class="role-label">${role === 'user' ? 'You' : 'Carrot'}</div>${body}`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

async function sendChat() {
    const input = document.getElementById('cmd-input');
    const msg = input.value.trim();
    // An attachment on its own is a valid turn ("what is this?").
    if (!msg && !pendingAttachments.length) return;
    const attachments = pendingAttachments.slice();
    input.value = '';
    hideSkillPop();
    switchTab('workspace');
    appendMessage('user', msg + (attachments.length
        ? `\n\n_${attachments.map(a => a.name).join(', ')}_` : ''));
    clearAttachments();
    if (!currentConversationId) {
        document.getElementById('chat-title').textContent = (msg || attachments[0].name).slice(0, 42);
    }

    await streamTurn('/api/chat/stream', {
        message: msg || 'What is in the attached file?',
        attachments: attachments.map(a => ({ name: a.name, mime: a.mime, data: a.data })),
        conversation_id: currentConversationId,
        model: currentModel,
        provider: currentProvider,
        temporary: temporaryChat,
        skill: activeSkill ? activeSkill.slug : null,
        search_mode: currentSearchMode,
    }, activeSkill);
}

// ===== Attachments =====
// Images go to the model as images (vision models only — the server says so
// plainly rather than dropping them); PDFs and text files are extracted
// server-side and folded into the prompt, so they work with any model.

let pendingAttachments = [];
const ATTACH_MAX_BYTES = 20 * 1024 * 1024;

function attachIcon(mime, name) {
    if ((mime || '').startsWith('image/')) return 'i-image';
    if ((mime || '') === 'application/pdf' || /\.pdf$/i.test(name || '')) return 'i-file-pdf';
    return 'i-doc';
}

function renderAttachTray() {
    const tray = document.getElementById('attach-tray');
    if (!tray) return;
    tray.classList.toggle('hidden', !pendingAttachments.length);
    tray.innerHTML = pendingAttachments.map((a, i) => `
        <span class="attach-chip">
          ${a.thumb
            ? `<img src="${a.thumb}" alt="">`
            : `<svg class="ico"><use href="#${attachIcon(a.mime, a.name)}"/></svg>`}
          <span class="attach-name" title="${escHtml(a.name)}">${escHtml(a.name)}</span>
          <span class="attach-size">${fmtBytes(a.bytes)}</span>
          <button class="attach-x" title="Remove" onclick="removeAttachment(${i})">
            <svg class="ico"><use href="#i-x"/></svg>
          </button>
        </span>`).join('');
}

function removeAttachment(index) {
    pendingAttachments.splice(index, 1);
    renderAttachTray();
}

function clearAttachments() {
    pendingAttachments = [];
    renderAttachTray();
}

async function addAttachments(files) {
    for (const file of Array.from(files || [])) {
        if (file.size > ATTACH_MAX_BYTES) {
            alert(`${file.name} is too large (limit ${fmtBytes(ATTACH_MAX_BYTES)}).`);
            continue;
        }
        const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
        const isImage = (file.type || '').startsWith('image/');
        pendingAttachments.push({
            name: file.name, mime: file.type, bytes: file.size, data,
            thumb: isImage ? `data:${file.type};base64,${data}` : null,
        });
    }
    renderAttachTray();
}

// Paste a screenshot straight into the composer, and drop files anywhere.
document.addEventListener('paste', (e) => {
    if (!e.clipboardData || currentTab !== 'workspace') return;
    const files = Array.from(e.clipboardData.files || []);
    if (files.length) { e.preventDefault(); addAttachments(files); }
});
document.addEventListener('dragover', (e) => {
    if (e.dataTransfer && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        document.body.classList.add('dropping');
    }
});
document.addEventListener('dragleave', (e) => {
    if (e.relatedTarget === null) document.body.classList.remove('dropping');
});
document.addEventListener('drop', (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    document.body.classList.remove('dropping');
    switchTab('workspace');
    addAttachments(e.dataTransfer.files);
});

// ===== Search mode =====
// Three postures for one question: never reach the web, reach it once, or keep
// going until the gaps are closed. The choice is sent with the turn and also
// saved, so it is both a per-message override and a default.

let currentSearchMode = null;
let searchModes = [];

async function loadSearchModes() {
    try {
        const body = await api('/api/chat/search-modes');
        searchModes = body.modes || [];
        currentSearchMode = currentSearchMode || body.current;
        renderSearchModes();
    } catch (e) {
        console.warn('search modes failed', e);
    }
}

function searchModeLabel(id) {
    const mode = searchModes.find(m => m.id === id);
    return mode ? mode.label : 'Search';
}

function renderSearchModes() {
    const label = document.getElementById('search-label');
    if (label) label.textContent = searchModeLabel(currentSearchMode);

    const button = document.getElementById('search-btn');
    if (button) button.classList.toggle('search-off', currentSearchMode === 'off');

    const list = document.getElementById('search-mode-list');
    if (!list) return;
    list.innerHTML = searchModes.map(mode => `
        <button class="pop-item${mode.id === currentSearchMode ? ' active' : ''}"
                onclick="setSearchMode('${escHtml(mode.id)}')">
            <span class="pop-item-name">${escHtml(mode.label)}</span>
            <span class="pop-item-sub">${escHtml(mode.help)}</span>
        </button>`).join('');
}

function toggleSearchPop() {
    const pop = document.getElementById('search-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) fitPopoverAbove('search-pop', 'search-btn');
}

async function setSearchMode(id) {
    currentSearchMode = id;
    renderSearchModes();
    document.getElementById('search-pop').classList.add('hidden');
    // Persist as the default too — a user who turns search off for a private
    // conversation means it, and should not have to turn it off again.
    try {
        await api('/api/config/chat_search_mode', {
            method: 'PUT', body: JSON.stringify(id),
        });
    } catch (e) {
        console.warn('could not save search mode', e);
    }
}

// Renders one streamed turn into the chat view. Shared by the chat box and by
// "send to agent" in Notes, so both get the same tool trace, reasoning panel
// and approval prompts without duplicating any of it.
async function streamTurn(url, payload, skill) {
    const assistantEl = appendMessage('assistant', '');
    const contentEl = assistantEl.querySelector('.content');
    contentEl.innerHTML = '<span class="caret">&nbsp;</span>';

    // Lazily created tool-call trace box (terminal-style, above the answer).
    let toolEl = null;
    function toolLine(text, cls) {
        if (!toolEl) {
            toolEl = document.createElement('div');
            toolEl.className = 'trace tool-trace';
            assistantEl.insertBefore(toolEl, contentEl);
        }
        const div = document.createElement('div');
        div.className = 'trace-line' + (cls ? ' ' + cls : '');
        div.textContent = text;
        toolEl.appendChild(div);
        toolEl.scrollTop = toolEl.scrollHeight;
    }
    if (skill) toolLine('skill: ' + skill.name, 'intent');

    // Lazily created reasoning trace box (for thinking models).
    let thinkEl = null;
    let thinkBody = null;
    function ensureThink() {
        if (thinkEl) return;
        thinkEl = document.createElement('details');
        thinkEl.className = 'think streaming';
        thinkEl.open = true;
        thinkEl.innerHTML = '<summary>Thinking</summary><div class="think-body"></div>';
        thinkBody = thinkEl.querySelector('.think-body');
        assistantEl.insertBefore(thinkEl, contentEl);
    }
    function finishThink() {
        if (!thinkEl) return;
        thinkEl.classList.remove('streaming');
        thinkEl.querySelector('summary').textContent = 'Thought process';
        thinkEl.open = false;
    }

    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let full = '';
        const pendingArtifacts = [];
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const payload = JSON.parse(raw.slice(5).trim());
                const box = document.getElementById('chat-messages');
                if (payload.skill) toolLine('skill active: ' + payload.skill.name, 'intent');
                if (payload.route) {
                    // Always say where the answer came from — local vs hosted is
                    // the single most important thing to be honest about here.
                    const where = payload.route.local ? 'on-device' : payload.route.provider;
                    toolLine(`${payload.route.model} (${where})`, 'intent');
                }
                if (payload.document) {
                    // A doc send reports what it actually attached, before any
                    // tokens arrive — a citation that silently failed is worse
                    // than useless, so failures are shown too.
                    for (const ref of payload.document.references || []) {
                        toolLine(`${ref.raw} ${ref.ok ? '✓' : '✗'} ${ref.detail}`,
                                 ref.ok ? 'search' : 'error');
                    }
                    for (const warning of payload.document.warnings || []) {
                        toolLine(warning, 'error');
                    }
                }
                if (payload.tool) {
                    toolLine(`tool → ${payload.tool.name}(${JSON.stringify(payload.tool.args)})`, 'search');
                }
                if (payload.tool_result) {
                    const raw = String(payload.tool_result.result);
                    // show_artifact answers with a marker the UI swaps for the
                    // rendered thing; the raw marker is noise in the trace.
                    for (const id of artifactIdsIn(raw)) pendingArtifacts.push(id);
                    toolLine(`  ← ${stripArtifactMarkers(raw).slice(0, 160)}`, 'stage');
                }
                if (payload.approval_request) {
                    showApprovalPrompt(payload.approval_request);
                }
                if (payload.approval_resolved) {
                    dismissApprovalPrompt(payload.approval_resolved.id);
                }
                if (payload.thinking) {
                    ensureThink();
                    thinkBody.textContent += payload.thinking;
                    thinkBody.scrollTop = thinkBody.scrollHeight;
                    box.scrollTop = box.scrollHeight;
                }
                if (payload.chunk) {
                    if (!full) finishThink();
                    full += payload.chunk;
                    contentEl.innerHTML = mdToHtml(full);
                    box.scrollTop = box.scrollHeight;
                }
                if (payload.done && payload.conversation_id) {
                    currentConversationId = payload.conversation_id;
                }
            }
        }
        finishThink();
        contentEl.classList.add('md');
        contentEl.innerHTML = full ? mdToHtml(full) : '(no response)';
        // Charts and diagrams land under the finished answer, in the order the
        // model produced them.
        if (pendingArtifacts.length && typeof mountArtifacts === 'function') {
            mountArtifacts(contentEl.parentElement,
                           pendingArtifacts.map(id => `[[carrot:artifact:${id}]]`).join(' '));
        }
        if (speakReplies && full) speakText(full);
    } catch (e) {
        contentEl.textContent = e.message;
        contentEl.classList.add('error');
    } finally {
        clearActiveSkill();
    }
}

function newChat() {
    currentConversationId = null;
    document.getElementById('chat-title').textContent = 'New session';
    const messagesEl = document.getElementById('chat-messages');
    messagesEl.innerHTML = `
        <div class="chat-empty" id="chat-empty">
            <span class="logo-mask big"></span>
            <p id="chat-empty-line">Ask anything below.</p>
        </div>`;
    renderEmptyStateLine();
    switchTab('workspace');
    focusCmd();
}

// ===== Conversations (Chats tab) =====
let chatCollapsed = {};
let chatFoldersCache = [];
let chatNewFolderOpen = false;
let chatRenamingFolder = null;

async function loadConversations() {
    const listEl = document.getElementById('conversations-list');
    try {
        const [convs, folders] = await Promise.all([
            api('/api/conversations?limit=200'),
            api('/api/chat-folders'),
        ]);
        chatFoldersCache = folders;
        listEl.innerHTML = '';
        if (chatNewFolderOpen) {
            listEl.appendChild(folderEditorRow('', createFolderSubmit, cancelNewFolder));
        }
        if (!convs.length && !folders.length) {
            if (!chatNewFolderOpen) {
                listEl.innerHTML = '<div class="empty">No conversations yet. Start one from the command bar below.</div>';
            }
            return;
        }
        const starred = convs.filter(c => (c.metadata || {}).starred);
        const unfiled = convs.filter(c => !(c.metadata || {}).folder_id);

        if (starred.length) {
            listEl.appendChild(chatSection({ id: '__starred', name: 'Starred', icon: 'i-star' }, starred, folders, false));
        }
        for (const f of folders) {
            const inFolder = convs.filter(c => (c.metadata || {}).folder_id === f.id);
            listEl.appendChild(chatSection(f, inFolder, folders, true));
        }
        listEl.appendChild(chatSection({ id: '', name: 'All chats', icon: 'i-chat' }, unfiled, folders, false));
    } catch (e) {
        listEl.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// Inline folder name editor (works in Electron, unlike prompt()).
function folderEditorRow(initial, onSave, onCancel) {
    const row = document.createElement('div');
    row.className = 'chat-folder-editor';
    row.innerHTML = `
        <svg class="ico chat-section-ico"><use href="#i-folder"/></svg>
        <input type="text" class="chat-folder-input" placeholder="Folder name" value="${escHtml(initial)}">
        <button class="btn btn-primary">Save</button>
        <button class="btn btn-ghost">Cancel</button>`;
    const input = row.querySelector('input');
    const btns = row.querySelectorAll('button');
    const save = () => { const v = input.value.trim(); if (v) onSave(v); };
    btns[0].onclick = save;
    btns[1].onclick = () => onCancel();
    input.onkeydown = (e) => {
        if (e.key === 'Enter') save();
        else if (e.key === 'Escape') onCancel();
    };
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return row;
}

function chatSection(section, convs, folders, isFolder) {
    if (isFolder && section.id === chatRenamingFolder) {
        return folderEditorRow(
            section.name,
            (name) => submitRenameFolder(section.id, name),
            () => { chatRenamingFolder = null; loadConversations(); }
        );
    }
    const key = section.id || '__all';
    const collapsed = !!chatCollapsed[key];
    const wrap = document.createElement('div');
    wrap.className = 'chat-section';
    const head = document.createElement('div');
    head.className = 'chat-section-head';
    head.innerHTML = `
        <svg class="ico chev chat-chev${collapsed ? '' : ' open'}"><use href="#i-chevron"/></svg>
        <svg class="ico chat-section-ico"><use href="#${section.icon || 'i-folder'}"/></svg>
        <span class="chat-section-name">${escHtml(section.name)}</span>
        <span class="chat-section-count">${convs.length}</span>
        ${isFolder ? `
          <button class="icon-btn" title="Rename folder" onclick="event.stopPropagation();renameFolder('${section.id}')"><svg class="ico"><use href="#i-edit"/></svg></button>
          <button class="icon-btn" title="Delete folder" onclick="event.stopPropagation();deleteFolder('${section.id}')"><svg class="ico"><use href="#i-trash"/></svg></button>
        ` : ''}`;
    head.onclick = () => { chatCollapsed[key] = !chatCollapsed[key]; loadConversations(); };
    wrap.appendChild(head);

    const body = document.createElement('div');
    body.className = 'chat-section-body' + (collapsed ? ' hidden' : '');
    if (!convs.length) {
        body.innerHTML = '<div class="empty small">No chats here.</div>';
    } else {
        for (const c of convs) body.appendChild(chatRow(c, folders));
    }
    wrap.appendChild(body);
    return wrap;
}

function chatRow(c, folders) {
    const meta = c.metadata || {};
    const div = document.createElement('div');
    div.className = 'chat-row';
    const dateStr = (c.updated_at || c.created_at || '').slice(0, 10);
    const folderOpts = ['<option value="">No folder</option>'].concat(
        folders.map(f => `<option value="${f.id}"${meta.folder_id === f.id ? ' selected' : ''}>${escHtml(f.name)}</option>`)
    ).join('');
    div.innerHTML = `
        <button class="chat-star${meta.starred ? ' on' : ''}" title="${meta.starred ? 'Unstar' : 'Star'}" onclick="toggleStar('${c.id}', ${meta.starred ? 'false' : 'true'})">
          <svg class="ico"><use href="#i-star"/></svg>
        </button>
        <div class="chat-row-main" onclick="openConversation('${c.id}')">
          <div class="chat-row-title">${escHtml(c.title || 'Untitled')}</div>
          <div class="chat-row-sub">${escHtml(dateStr)}</div>
        </div>
        <select class="chat-folder-select" title="Move to folder" onchange="moveToFolder('${c.id}', this.value)" onclick="event.stopPropagation()">
          ${folderOpts}
        </select>
        <button class="icon-btn" title="Delete chat" onclick="deleteChat('${c.id}')"><svg class="ico"><use href="#i-trash"/></svg></button>`;
    return div;
}

async function toggleStar(convId, starred) {
    try { await api(`/api/conversations/${convId}`, { method: 'PATCH', body: JSON.stringify({ starred }) }); loadConversations(); }
    catch (e) { alert('Could not update chat: ' + e.message); }
}

async function moveToFolder(convId, folderId) {
    try { await api(`/api/conversations/${convId}`, { method: 'PATCH', body: JSON.stringify({ folder_id: folderId }) }); loadConversations(); }
    catch (e) { alert('Could not move chat: ' + e.message); }
}

async function deleteChat(convId) {
    if (!confirm('Delete this chat and its messages?')) return;
    try { await api(`/api/conversations/${convId}`, { method: 'DELETE' }); loadConversations(); }
    catch (e) { alert('Could not delete chat: ' + e.message); }
}

async function newFolder() {
    chatNewFolderOpen = true;
    loadConversations();
}

async function createFolderSubmit(name) {
    chatNewFolderOpen = false;
    try { await api('/api/chat-folders', { method: 'POST', body: JSON.stringify({ name }) }); }
    catch (e) { alert('Could not create folder: ' + e.message); }
    loadConversations();
}

function cancelNewFolder() {
    chatNewFolderOpen = false;
    loadConversations();
}

function renameFolder(folderId) {
    chatRenamingFolder = folderId;
    loadConversations();
}

async function submitRenameFolder(folderId, name) {
    chatRenamingFolder = null;
    try { await api(`/api/chat-folders/${folderId}`, { method: 'PUT', body: JSON.stringify({ name }) }); }
    catch (e) { alert('Could not rename folder: ' + e.message); }
    loadConversations();
}

async function deleteFolder(folderId) {
    if (!confirm('Delete this folder? Chats inside will move back to All chats.')) return;
    try { await api(`/api/chat-folders/${folderId}`, { method: 'DELETE' }); loadConversations(); }
    catch (e) { alert('Could not delete folder: ' + e.message); }
}

async function openConversation(convId) {
    currentConversationId = convId;
    const conv = await api(`/api/conversations/${convId}`);
    const messagesEl = document.getElementById('chat-messages');
    messagesEl.innerHTML = '';
    document.getElementById('chat-title').textContent = conv.title || 'Untitled';
    const rendered = conv.messages.map(m => appendMessage(m.role, m.content));
    // Charts made earlier in this conversation are part of it — reopening a
    // chat and finding the figures gone would make them feel disposable.
    if (typeof mountArtifacts === 'function') {
        try {
            const { artifacts } = await api(`/api/conversations/${convId}/artifacts`);
            const last = rendered[rendered.length - 1];
            const host = last && last.querySelector('.content');
            if (host && artifacts && artifacts.length) {
                mountArtifacts(host.parentElement,
                    artifacts.map(a => `[[carrot:artifact:${a.id}]]`).join(' '));
            }
        } catch (_) { /* older conversation, or none stored */ }
    }
    switchTab('workspace');
}

// ===== Workspace cards =====
async function loadWorkspace() {
    loadRecapCard();
    loadDeadlinesCard();
    loadMilestonesCard();
    refreshStatus();
}

async function loadRecapCard() {
    const el = document.getElementById('card-recap');
    try {
        const briefing = await api('/api/recap/briefing/today');
        if (briefing.available && briefing.markdown) {
            el.innerHTML = `<div class="recap-briefing md">${mdToHtml(briefing.markdown)}</div>`;
            return;
        }
        const recaps = await api('/api/recap');
        if (!recaps.length) {
            el.innerHTML = '<div class="empty">No briefing yet today. Run one to research your morning digest.</div>';
            return;
        }
        el.innerHTML = '';
        for (const r of recaps.slice(0, 3)) {
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(r.title)}</span><span class="k-sub">${escHtml((r.created_at || '').slice(5, 10))}</span>`;
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Recap unavailable.</div>';
    }
}

async function loadDeadlinesCard() {
    const el = document.getElementById('card-deadlines');
    const badge = document.getElementById('deadline-badge');
    try {
        const reminders = await api('/api/reminders');
        const open = reminders.filter(r => !r.completed);
        open.sort((a, b) => (a.due_at || '9999') < (b.due_at || '9999') ? -1 : 1);
        if (!open.length) {
            el.innerHTML = '<div class="empty">Nothing due. Add reminders to see them here.</div>';
            badge.classList.add('hidden');
            return;
        }
        const urgent = open.filter(r => dueLabel(r.due_at).urgent).length;
        if (urgent > 0) {
            badge.textContent = urgent + ' urgent';
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
        el.innerHTML = '';
        for (const r of open.slice(0, 4)) {
            const d = dueLabel(r.due_at);
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(r.title)}</span>` +
                (d.text ? `<span class="${d.urgent ? 'k-urgent' : 'k-sub'}">${d.text}</span>` : '');
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Reminders unavailable.</div>';
    }
}

async function loadMilestonesCard() {
    const el = document.getElementById('card-milestones');
    try {
        const goals = await api('/api/goals');
        if (!goals.length) {
            el.innerHTML = '<div class="empty">No goals yet.</div>';
            return;
        }
        el.innerHTML = '';
        for (const g of goals.slice(0, 4)) {
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(g.title)}</span><span class="k-sub">${escHtml(g.category || '')}</span>`;
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Goals unavailable.</div>';
    }
}

// ===== Speech: voice input (STT) =====
async function toggleVoiceInput() {
    if (isRecording) { stopRecording(); return; }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recordedChunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = e => { if (e.data.size) recordedChunks.push(e.data); };
        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            setRecordingUI(false);
            const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType });
            await transcribeBlob(blob);
        };
        mediaRecorder.start();
        isRecording = true;
        setRecordingUI(true);
    } catch (e) {
        alert('Microphone unavailable: ' + e.message);
    }
}

function stopRecording() {
    if (mediaRecorder && isRecording) mediaRecorder.stop();
    isRecording = false;
}

function setRecordingUI(on) {
    const el = document.getElementById('mic-btn');
    if (el) el.classList.toggle('recording', on);
}

async function transcribeBlob(blob) {
    try {
        const arrayBuf = await blob.arrayBuffer();
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuf = await audioCtx.decodeAudioData(arrayBuf);
        const wavB64 = await audioBufferToWavBase64(audioBuf, 16000);
        const result = await api('/api/speech/transcribe', {
            method: 'POST',
            body: JSON.stringify({ audio_base64: wavB64 }),
        });
        if (result.success && result.text) {
            document.getElementById('cmd-input').value = result.text;
            focusCmd();
        } else {
            alert('Transcription failed: ' + (result.error || 'no speech detected'));
        }
    } catch (e) {
        alert('Transcription error: ' + e.message);
    }
}

// Encode an AudioBuffer to 16-bit PCM WAV (resampled) and return base64.
function audioBufferToWavBase64(buffer, targetRate) {
    const offline = new OfflineAudioContext(1,
        Math.ceil(buffer.duration * targetRate), targetRate);
    const src = offline.createBufferSource();
    src.buffer = buffer;
    src.connect(offline.destination);
    src.start(0);
    return offline.startRendering().then(rendered => {
        const data = rendered.getChannelData(0);
        const pcm = new Int16Array(data.length);
        for (let i = 0; i < data.length; i++) {
            const s = Math.max(-1, Math.min(1, data[i]));
            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        const wav = new ArrayBuffer(44 + pcm.length * 2);
        const view = new DataView(wav);
        const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
        writeStr(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); writeStr(8, 'WAVE');
        writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, 1, true); view.setUint32(24, targetRate, true);
        view.setUint32(28, targetRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
        writeStr(36, 'data'); view.setUint32(40, pcm.length * 2, true);
        new Int16Array(wav, 44).set(pcm);
        let bin = '';
        const bytes = new Uint8Array(wav);
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        return btoa(bin);
    });
}

// ===== Speech: read replies aloud (TTS) =====
function toggleSpeak() {
    speakReplies = !speakReplies;
    const btn = document.getElementById('speak-toggle');
    btn.querySelector('use').setAttribute('href', speakReplies ? '#i-speaker' : '#i-speaker-off');
    btn.title = speakReplies ? 'Reading replies aloud' : 'Read replies aloud';
    btn.style.color = speakReplies ? 'var(--accent)' : '';
}

async function speakText(text) {
    try {
        const result = await api('/api/speech/speak', {
            method: 'POST',
            body: JSON.stringify({ text: text.slice(0, 1200) }),
        });
        if (result.success && result.audio_base64) {
            const audio = new Audio('data:audio/wav;base64,' + result.audio_base64);
            audio.play();
        }
    } catch (_) { /* TTS optional — fail silently */ }
}

// ===== Search =====
async function doSearch() {
    const q = document.getElementById('search-input').value.trim();
    if (!q) return;
    const container = document.getElementById('search-results');
    container.innerHTML = '<div class="empty">Searching…</div>';
    try {
        const results = await api(`/api/search?q=${encodeURIComponent(q)}&limit=20`);
        container.innerHTML = `<div class="empty">${results.count} results for "${escHtml(q)}"</div>`;
        for (const r of results.results) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `
                <div class="sub">${escHtml((r.timestamp || '').slice(0, 16).replace('T', ' '))} · ${escHtml(r.role)} · ${escHtml(r.conversation_title || r.conversation_id)}</div>
                <div class="body">${escHtml((r.content || '').slice(0, 400))}</div>`;
            container.appendChild(div);
        }
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// ===== Terminal =====
function toggleTerminal() {
    document.getElementById('terminal-panel').classList.toggle('collapsed');
}

function termAppend(text, cls) {
    const outputEl = document.getElementById('terminal-output');
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text;
    outputEl.appendChild(span);
    outputEl.scrollTop = outputEl.scrollHeight;
}

async function runTerminal() {
    const input = document.getElementById('terminal-input');
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';
    termAppend(`$ ${cmd}\n`, 't-cmd');
    await executeTerminal(cmd, false);
}

// The server answers 428 for commands it judges destructive. That is a
// question, not a failure — ask, then re-send with confirm set.
async function executeTerminal(cmd, confirm) {
    try {
        const resp = await fetch('/api/terminal/execute', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ command: cmd, confirm: !!confirm }),
        });

        if (resp.status === 428) {
            const detail = (await resp.json()).detail || {};
            const reasons = (detail.reasons || []).join(', ');
            termAppend(`⚠ ${reasons || 'this command looks destructive'}\n`, 't-warn');
            if (window.confirm(`This command ${reasons || 'looks destructive'}.\n\n${cmd}\n\nRun it anyway?`)) {
                return executeTerminal(cmd, true);
            }
            termAppend('cancelled\n', 't-err');
            return;
        }
        if (!resp.ok) {
            const detail = (await resp.json().catch(() => ({}))).detail;
            throw new Error(typeof detail === 'string' ? detail : resp.statusText);
        }
        const data = await resp.json();
        termAppend((data.output || '') + '\n');
    } catch (e) {
        termAppend('error: ' + e.message + '\n', 't-err');
    }
}

async function loadTerminalHistory() {
    const outputEl = document.getElementById('terminal-output');
    outputEl.innerHTML = '';
    try {
        const history = await api('/api/terminal/history');
        for (const h of history.slice(0, 20).reverse()) {
            termAppend(`$ ${h.command}\n`, 't-cmd');
            termAppend((h.output || '') + '\n');
        }
    } catch (_) {}
}

// ===== Notes ===== (implemented in features.js)

// ===== Goals =====
async function loadGoals() {
    const container = document.getElementById('goals-list');
    container.innerHTML = '';
    try {
        const goals = await api('/api/goals');
        if (!goals.length) { container.innerHTML = '<div class="empty">No goals yet.</div>'; return; }
        for (const g of goals) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `<div class="goal-head"><strong>${escHtml(g.title)}</strong>${g.category ? `<span class="tag">${escHtml(g.category)}</span>` : ''}</div>` +
                (g.description ? `<div class="body">${escHtml(g.description)}</div>` : '');
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function addGoal() {
    const title = document.getElementById('new-goal-title').value.trim();
    const category = document.getElementById('new-goal-category').value.trim();
    if (!title) return;
    await api('/api/goals', { method: 'POST', body: JSON.stringify({ title, category }) });
    document.getElementById('new-goal-title').value = '';
    document.getElementById('new-goal-category').value = '';
    loadGoals();
}

// ===== Reminders =====
async function loadReminders() {
    const container = document.getElementById('reminders-list');
    container.innerHTML = '';
    try {
        const reminders = await api('/api/reminders');
        if (!reminders.length) { container.innerHTML = '<div class="empty">No reminders yet.</div>'; return; }
        for (const r of reminders) {
            const d = dueLabel(r.due_at);
            const div = document.createElement('div');
            div.className = `list-item rem-row ${r.completed ? 'completed' : ''}`;
            div.innerHTML = `
                <input type="checkbox" ${r.completed ? 'checked' : ''} onchange="toggleReminder('${r.id}', this.checked)">
                <span class="rem-title" style="flex:1">${escHtml(r.title)}</span>` +
                (d.text ? `<span class="tag ${d.urgent ? 'hot' : ''}">${d.text}</span>` : '');
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function addReminder() {
    const title = document.getElementById('new-reminder-title').value.trim();
    const dueAt = document.getElementById('new-reminder-due').value;
    if (!title) return;
    await api('/api/reminders', { method: 'POST', body: JSON.stringify({ title, due_at: dueAt || null }) });
    document.getElementById('new-reminder-title').value = '';
    document.getElementById('new-reminder-due').value = '';
    loadReminders();
    loadDeadlinesCard();
}

async function toggleReminder(id, completed) {
    await api(`/api/reminders/${id}/complete`, {
        method: 'POST',
        body: JSON.stringify({ completed }),
    });
    loadReminders();
    loadDeadlinesCard();
}

// ===== Recap =====
async function runRecap() {
    const el = document.getElementById('card-recap');
    el.innerHTML = '<div class="trace" id="recap-trace"></div><div class="recap-out" id="recap-out"></div>';
    const traceEl = document.getElementById('recap-trace');
    const outEl = document.getElementById('recap-out');

    function traceLine(text, cls) {
        const div = document.createElement('div');
        div.className = 'trace-line' + (cls ? ' ' + cls : '');
        div.textContent = text;
        traceEl.appendChild(div);
        traceEl.scrollTop = traceEl.scrollHeight;
    }

    let thinkLine = null;
    function traceThink(text) {
        if (!thinkLine) {
            thinkLine = document.createElement('div');
            thinkLine.className = 'trace-think';
            traceEl.appendChild(thinkLine);
        }
        thinkLine.textContent = ('thinking: ' + (thinkLine.dataset.raw = (thinkLine.dataset.raw || '') + text)).slice(0, 4000);
        traceEl.scrollTop = traceEl.scrollHeight;
    }

    try {
        const resp = await fetch('/api/recap/run/stream', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({}),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let summary = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const p = JSON.parse(raw.slice(5).trim());
                if (p.stage) traceLine(`${p.stage}: ${p.detail || ''}`, 'stage');
                if (p.intents) {
                    for (const it of p.intents) traceLine('intent → ' + it, 'intent');
                }
                if (p.search) {
                    traceLine(`search [${p.search.topic}] ${p.search.title || ''} — ${p.search.url || ''}`.trim(), 'search');
                }
                if (p.thinking) traceThink(p.thinking);
                if (p.token) {
                    summary += p.token;
                    outEl.innerHTML = mdToHtml(summary);
                    outEl.scrollTop = outEl.scrollHeight;
                }
                if (p.error) traceLine(p.error, 'err');
                if (p.done) traceLine('done — briefing saved', 'ok');
            }
        }
        if (summary) {
            outEl.classList.add('md');
            outEl.innerHTML = mdToHtml(summary);
        } else {
            outEl.innerHTML = '<div class="empty">No summary produced.</div>';
        }
    } catch (e) {
        traceLine(e.message, 'err');
    }
}

// ===== Assignments =====
async function loadAssignments() {
    const container = document.getElementById('assignments-list');
    container.innerHTML = '';
    try {
        const result = await api('/api/assignments');
        if (!result.assignments.length) { container.innerHTML = '<div class="empty">No assignments found. Scan your files to index them.</div>'; return; }
        for (const a of result.assignments) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `<strong>${escHtml(a.name)}</strong><span class="tag">${escHtml(a.extension)}</span><div class="sub">${escHtml(a.directory)}</div>`;
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function scanAssignments() {
    const container = document.getElementById('assignments-list');
    container.innerHTML = '<div class="empty">Scanning…</div>';
    try {
        const result = await api('/api/computer_use/scan', { method: 'POST' });
        loadAssignments();
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// ===== Leaderboard =====
async function loadLeaderboard() {
    const list = document.getElementById('leaderboard-list');
    try {
        const data = await api('/api/leaderboard?limit=50');
        const stats = await api('/api/leaderboard/stats');
        document.getElementById('lb-total').innerHTML = `<div class="value">${stats.total_submissions}</div><div class="label">Total submissions</div>`;
        const modelEl = document.getElementById('lb-top-model');
        const osEl = document.getElementById('lb-top-os');
        const gpuEl = document.getElementById('lb-top-gpu');
        modelEl.innerHTML = stats.by_model.length
            ? `<div class="value">${escHtml(stats.by_model[0].model)}</div><div class="label">Top model (${stats.by_model[0].count})</div>`
            : '<div class="value">—</div><div class="label">Top model</div>';
        osEl.innerHTML = stats.by_os.length
            ? `<div class="value">${escHtml(stats.by_os[0].os)}</div><div class="label">Top OS (${stats.by_os[0].count})</div>`
            : '<div class="value">—</div><div class="label">Top OS</div>';
        gpuEl.innerHTML = stats.top_gpus.length
            ? `<div class="value">${escHtml(stats.top_gpus[0].gpu.slice(0, 22))}</div><div class="label">Top GPU</div>`
            : '<div class="value">—</div><div class="label">Top GPU</div>';
        renderLeaderboardList(data, list);
        const modelSelect = document.getElementById('lb-filter-model');
        modelSelect.innerHTML = '<option value="">All Models</option>';
        for (const m of (stats.by_model || [])) {
            const opt = document.createElement('option');
            opt.value = m.model; opt.textContent = m.model;
            modelSelect.appendChild(opt);
        }
    } catch (e) { list.innerHTML = '<div class="empty">Failed to load leaderboard.</div>'; }
}

function renderLeaderboardList(data, list) {
    list.innerHTML = '';
    if (!data.length) {
        list.innerHTML = '<div class="empty">No submissions yet. Be the first to share your setup.</div>';
        return;
    }
    for (const entry of data) {
        const div = document.createElement('div');
        div.className = 'list-item';
        div.innerHTML = `
            <strong>${escHtml(entry.os)} · ${entry.ram_gb}GB</strong>
            <span class="tag">${escHtml(entry.gpu ? entry.gpu.slice(0, 36) : 'N/A')}</span>
            <span class="tag">${escHtml(entry.active_model || 'No model')}</span>
            <div class="sub">${entry.submitted_at ? entry.submitted_at.slice(0, 10) : ''}</div>`;
        list.appendChild(div);
    }
}

async function filterLeaderboard() {
    const os = document.getElementById('lb-filter-os').value;
    const ram = document.getElementById('lb-filter-ram').value;
    const model = document.getElementById('lb-filter-model').value;
    let url = '/api/leaderboard?limit=50';
    const params = [];
    if (os) params.push(`os_name=${encodeURIComponent(os)}`);
    if (ram) params.push(`ram_gb_min=${encodeURIComponent(ram)}`);
    if (model) params.push(`model=${encodeURIComponent(model)}`);
    if (params.length) url += '&' + params.join('&');
    const data = await api(url);
    renderLeaderboardList(data, document.getElementById('leaderboard-list'));
}

async function submitToLeaderboard() {
    const result = await api('/api/leaderboard/submit', { method: 'POST' });
    if (result.anonymous_id) alert('Thanks for submitting your setup. Anonymous ID: ' + result.anonymous_id);
}

// ===== Bootstrap splash =====
async function checkBootstrap() {
    try {
        const s = await api('/api/bootstrap/status');
        if (s.bootstrap_complete) { hideSplash(); return; }
        showSplash(s);
    } catch (_) { hideSplash(); }
}

let splashModel = null; // model picked on the splash; null = stock default
let splashHub = null;   // /api/hub payload, reused by the in-splash catalog

async function showSplash(s) {
    document.getElementById('splash').classList.remove('hidden');
    const status = document.getElementById('splash-status');
    if (!s.ollama_installed) status.textContent = 'Ollama is not installed. Carrot can set it up for you.';
    else if (!s.model_pulled) status.textContent = 'Ollama is ready — pick a model that fits your machine.';
    document.getElementById('splash-btn').classList.remove('hidden');
    document.getElementById('splash-skip').classList.remove('hidden');
    // Hardware-based picks from the Hub. New users shouldn't have to know
    // which model or quantization suits their specs — show what fits, let
    // experienced users skip, and link the full daily catalog.
    try {
        const hub = await api('/api/hub');
        renderSplashPicks(hub);
    } catch (_) { /* no picks — the default-model path still works */ }
}

function renderSplashPicks(hub) {
    splashHub = hub;
    const specsEl = document.getElementById('splash-specs');
    const picksEl = document.getElementById('splash-picks');
    const link = document.getElementById('splash-hub-link');
    const s = hub.specs || {};
    specsEl.textContent = `Detected: ${hubSpecLine(s)} — ${s.model_budget_gb} GB usable for models`;
    specsEl.classList.remove('hidden');
    link.classList.remove('hidden');

    const recs = hub.recommendations || {};
    if (!recs.best) return;
    const picks = [{ role: 'Recommended', m: recs.best }];
    if (recs.light && recs.light.id !== recs.best.id) picks.push({ role: 'Light & fast', m: recs.light });
    const coding = (recs.by_use_case || {}).coding;
    if (coding && !picks.some(p => p.m.id === coding.id)) picks.push({ role: 'For coding', m: coding });

    picksEl.innerHTML = '';
    picksEl.classList.remove('hidden');
    for (const p of picks) {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'splash-pick';
        card.dataset.model = p.m.id;
        card.innerHTML = `
            <span class="splash-pick-role">${escHtml(p.role)}</span>
            <strong>${escHtml(p.m.label || p.m.id)}</strong>
            <span class="muted small">${p.m.download_gb} GB · ${escHtml(p.m.quant || '')}${p.m.est_tps ? ` · ~${p.m.est_tps} tok/s` : ''} · ${escHtml(p.m.blurb || '')}</span>`;
        card.onclick = () => {
            splashModel = p.m.id;
            picksEl.querySelectorAll('.splash-pick').forEach(el =>
                el.classList.toggle('selected', el.dataset.model === splashModel));
        };
        picksEl.appendChild(card);
    }
    // Preselect the recommendation so plain "Set up now" does the right thing.
    splashModel = recs.best.id;
    picksEl.querySelector('.splash-pick').classList.add('selected');
}

function hideSplash() { document.getElementById('splash').classList.add('hidden'); }

// The full catalog, right on the setup screen — including the models that
// do NOT fit, each saying why. Seeing "needs 12 GB, you have 3.9" is more
// reassuring than a short list with no explanation.
function toggleSplashCatalog() {
    const el = document.getElementById('splash-catalog');
    const link = document.getElementById('splash-hub-link');
    if (!el.classList.contains('hidden')) {
        el.classList.add('hidden');
        link.textContent = 'See every model and why some won\'t run here →';
        return;
    }
    if (!splashHub) return;
    const budget = (splashHub.specs || {}).model_budget_gb || 0;
    const fitOrder = { great: 0, good: 1, tight: 2, too_big: 3 };
    const models = [...(splashHub.models || [])].sort((a, b) =>
        (fitOrder[a.fit] - fitOrder[b.fit]) || (a.min_mem_gb - b.min_mem_gb));
    // Compact badge text — the full wording would squeeze out model names.
    const SHORT_FIT = { great: 'Great', good: 'Good', tight: 'Tight', too_big: 'Too big' };
    el.innerHTML = models.map(m => {
        const why = m.fit === 'too_big'
            ? `needs ${m.min_mem_gb} GB, you have ${budget}`
            : (m.fit === 'tight'
                ? `needs ${m.min_mem_gb} GB — slow`
                : `${m.download_gb} GB${m.est_tps ? ` · ~${m.est_tps} tok/s` : ''}`);
        return `
          <button type="button" class="splash-cat-row fit-${m.fit}"
                  ${m.fit === 'too_big' ? 'disabled' : `onclick="pickSplashModel('${escHtml(m.id)}')"`}>
            <span class="splash-cat-name">${escHtml(m.label || m.id)}</span>
            <span class="fit-badge fit-${m.fit}">${SHORT_FIT[m.fit] || m.fit}</span>
            <span class="splash-cat-why">${escHtml(why)}</span>
          </button>`;
    }).join('');
    el.classList.remove('hidden');
    link.textContent = 'Hide the full catalog ←';
}

function pickSplashModel(id) {
    splashModel = id;
    // Reflect the choice in both the picks row and the catalog list.
    document.querySelectorAll('#splash-picks .splash-pick').forEach(el =>
        el.classList.toggle('selected', el.dataset.model === id));
    document.querySelectorAll('#splash-catalog .splash-cat-row').forEach(el =>
        el.classList.toggle('selected', el.textContent.trim().startsWith(
            (splashHub.models.find(m => m.id === id) || {}).label || id)));
    const status = document.getElementById('splash-status');
    if (status) status.textContent = `${id} selected — press Set up now.`;
}

function skipModelChoice() {
    // Experienced users: no picker, stock default, straight to setup.
    splashModel = null;
    runBootstrap();
}

function splashFailed(message) {
    const btn = document.getElementById('splash-btn');
    document.getElementById('splash-status').textContent = message;
    document.getElementById('splash-detail').textContent = '';
    btn.textContent = 'Retry';
    btn.classList.remove('hidden');
    document.getElementById('splash-skip').classList.remove('hidden');
    document.getElementById('splash-picks').classList.remove('hidden');
    document.getElementById('splash-hub-link').classList.remove('hidden');
}

// Setup streams over SSE so the bar tracks the actual download. A model
// is gigabytes; a bar that jumps 30% -> 100% just looks frozen.
function runBootstrap() {
    const btn = document.getElementById('splash-btn');
    const status = document.getElementById('splash-status');
    const detail = document.getElementById('splash-detail');
    const bar = document.getElementById('splash-bar');
    btn.classList.add('hidden');
    document.getElementById('splash-skip').classList.add('hidden');
    document.getElementById('splash-picks').classList.add('hidden');
    document.getElementById('splash-catalog').classList.add('hidden');
    document.getElementById('splash-hub-link').classList.add('hidden');
    status.textContent = 'Setting up…';
    detail.textContent = '';
    bar.style.width = '2%';

    const url = tokenUrl('/api/bootstrap/stream'
        + (splashModel ? `?model=${encodeURIComponent(splashModel)}` : ''));
    const src = new EventSource(url);
    let started = Date.now();

    src.onmessage = (ev) => {
        let p;
        try { p = JSON.parse(ev.data); } catch (_) { return; }

        if (p.type === 'status' || p.type === 'install') {
            status.textContent = p.message || '';
        } else if (p.type === 'download') {
            // Downloading the Ollama installer itself.
            const pct = p.total ? Math.round(p.downloaded / p.total * 100) : 0;
            status.textContent = 'Downloading Ollama…';
            bar.style.width = Math.max(pct * 0.2, 2) + '%';   // installer = first 20%
            detail.textContent = `${fmtBytes(p.downloaded)} of ${fmtBytes(p.total)}`;
        } else if (p.type === 'pull') {
            status.textContent = `Downloading ${p.model || 'model'}…`;
            if (p.total && p.completed != null) {
                const pct = Math.round(p.completed / p.total * 100);
                bar.style.width = (20 + pct * 0.8) + '%';      // model = remaining 80%
                const secs = (Date.now() - started) / 1000;
                const rate = secs > 2 ? ` · ${fmtBytes(p.completed / secs)}/s` : '';
                detail.textContent = `${fmtBytes(p.completed)} of ${fmtBytes(p.total)} (${pct}%)${rate}`;
            } else if (p.status) {
                detail.textContent = p.status;
            }
        } else if (p.type === 'error') {
            detail.textContent = p.message || '';
        } else if (p.type === 'done') {
            src.close();
            if (p.error) { splashFailed(p.error); return; }
            bar.style.width = '100%';
            status.textContent = 'Setup complete. Launching Carrot…';
            detail.textContent = '';
            setTimeout(() => { hideSplash(); refreshStatus(); loadModels(); }, 900);
        }
    };

    src.onerror = () => {
        src.close();
        splashFailed('Lost contact with Carrot during setup. Press Retry.');
    };
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', async () => {
    await loadRecapConfig();
    await refreshStatus();
    showBuildVersion();
    loadModels();
    loadSkillCatalog();
    loadSearchModes();
    loadWorkspaces();
    // Onboarding decides whether the bootstrap splash runs at all.
    maybeShowOnboarding();
    switchTab('dashboard');
    loadTerminalHistory();
    setInterval(refreshStatus, 15000);
    // The council chip lives in the composer, so its state has to be known
    // from the first paint rather than only after a visit to Settings.
    if (typeof loadConsensusPanel === 'function') loadConsensusPanel();

    // Ctrl+K focuses the command bar
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            focusCmd();
        }
    });

    // Click outside closes model popover
    document.addEventListener('click', e => {
        const picker = document.getElementById('model-picker');
        if (!picker.contains(e.target)) {
            document.getElementById('model-pop').classList.add('hidden');
        }
        const cmdbar = document.getElementById('cmdbar');
        if (!cmdbar.contains(e.target)) hideSkillPop();
    });
});

// ===== First-run onboarding =====
// Runs in front of the bootstrap splash. "Which kind of setup do you want"
// and "which model should I download" are different questions, and asking
// them together is what made first run confusing: a new user was shown a
// list of quantized model names before anyone had explained what a model is.

const ONBOARD_KEY_PAGES = {
    anthropic: 'https://console.anthropic.com/settings/keys',
    openai: 'https://platform.openai.com/api-keys',
    openrouter: 'https://openrouter.ai/keys',
    groq: 'https://console.groq.com/keys',
    together: 'https://api.together.xyz/settings/api-keys',
    deepseek: 'https://platform.deepseek.com/api_keys',
    mistral: 'https://console.mistral.ai/api-keys',
};

function onboardStep(step) {
    document.querySelectorAll('#onboard .onboard-step').forEach(el => {
        el.classList.toggle('hidden', el.dataset.step !== step);
    });
    // Choosing to run locally used to close the whole flow immediately, which
    // meant most people never saw where anything was. It goes to the tour now,
    // and the model download starts behind it.
    if (step === 'local') { startLocalSetup(); return; }
    if (step === 'key') onboardLoadProviders();
    if (step === 'subscription') onboardCheckSubscription();
}

let onboardingBootstrapStarted = false;

function startLocalSetup() {
    onboardingBootstrapStarted = true;
    onboardStep('tour');
}

// ---------- "I want to use my own AI subscription" ----------
//
// Most people who reach this screen are already paying one of these companies
// every month. Being told to go create a second, separately-billed developer
// account is the worst five minutes in the app, and it is where people stop.

async function onboardCheckSubscription() {
    const status = document.getElementById('onboard-sub-status');
    const select = document.getElementById('onboard-sub-provider');
    if (!status || !select) return;
    status.textContent = '';
    try {
        const state = await api(`/api/auth/status/${encodeURIComponent(select.value)}`);
        if (state.signed_in) {
            status.textContent = `Already signed in to ${select.value}.`;
        } else if (!state.oauth_configured) {
            // Saying so beats a button that fails for reasons nobody can see.
            status.textContent = 'This copy of Carrot does not have sign-in details for '
                + 'that provider yet, so an API key is the reliable path for now.';
        }
    } catch (_) { /* the screen still works without this */ }
}

async function startOnboardingSignIn() {
    const select = document.getElementById('onboard-sub-provider');
    const status = document.getElementById('onboard-sub-status');
    const provider = select.value;
    status.textContent = 'Opening the sign-in page…';
    try {
        await api(`/api/auth/mode/${encodeURIComponent(provider)}`,
            { method: 'PUT', body: JSON.stringify({ mode: 'subscription' }) });
        const started = await api(`/api/auth/login/${encodeURIComponent(provider)}`,
            { method: 'POST' });
        if (window.carrot?.openExternal) window.carrot.openExternal(started.url);
        else window.open(started.url, '_blank', 'noopener');
        status.textContent = 'Finish signing in in your browser, then come back here.';
        pollOnboardingSignIn(provider, status);
    } catch (e) {
        status.textContent = e.detail || e.message;
    }
}

function pollOnboardingSignIn(provider, status) {
    let tries = 0;
    const timer = setInterval(async () => {
        tries += 1;
        try {
            const state = await api(`/api/auth/status/${encodeURIComponent(provider)}`);
            if (state.signed_in) {
                clearInterval(timer);
                status.textContent = 'Signed in.';
                onboardStep('tour');
            }
        } catch (_) { clearInterval(timer); }
        if (tries > 90) clearInterval(timer);
    }, 2000);
}

async function onboardLoadProviders() {
    const select = document.getElementById('onboard-provider');
    if (select.dataset.loaded) return;
    select.dataset.loaded = '1';
    // The hosted ones only. Offering "LM Studio (local)" on the screen for
    // people who chose the cloud path is just noise.
    let options = [
        { id: 'anthropic', label: 'Anthropic (Claude)' },
        { id: 'openai', label: 'OpenAI (GPT)' },
    ];
    try {
        const body = await api('/api/router/providers');
        for (const preset of (body.presets || [])) {
            if (/local/i.test(preset.label || '')) continue;
            if (!options.some(o => o.id === preset.id)) {
                options.push({ id: preset.id, label: preset.label });
            }
        }
    } catch (_) { /* the two built-ins are enough to get started */ }
    select.innerHTML = options
        .map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('');
    onboardProviderChanged();
}

function onboardProviderChanged() {
    const id = document.getElementById('onboard-provider').value;
    const link = document.getElementById('onboard-key-link');
    const url = ONBOARD_KEY_PAGES[id];
    link.href = url || '#';
    link.classList.toggle('hidden', !url);
}

async function saveOnboardingKey() {
    const provider = document.getElementById('onboard-provider').value;
    const key = document.getElementById('onboard-key').value.trim();
    const status = document.getElementById('onboard-key-status');
    const button = document.getElementById('onboard-key-btn');
    if (!key) { status.textContent = 'Paste a key first.'; status.className = 'onboard-status bad'; return; }

    button.disabled = true;
    status.className = 'onboard-status';
    status.textContent = 'Checking the key…';
    try {
        await api(`/api/router/providers/${encodeURIComponent(provider)}/key`, {
            method: 'PUT', body: JSON.stringify({ api_key: key }),
        });
        // Saving a key that does not work is worse than not saving one: the
        // failure surfaces later, in the middle of an answer. /test exists for
        // exactly this and reports the provider's own error — listing models
        // is not a check, because it falls back to a cached list and returns
        // an `error` field rather than failing, so a garbage key looked fine.
        const probe = await api(`/api/router/providers/${encodeURIComponent(provider)}/test`,
                                { method: 'POST' });
        if (!probe.ok) {
            status.className = 'onboard-status bad';
            status.textContent = 'That key did not work: ' + (probe.error || 'the provider rejected it');
            return;
        }
        status.className = 'onboard-status good';
        status.textContent = probe.models
            ? `Working — ${probe.models} models available.`
            : 'Working.';
        await api(`/api/router/providers/${encodeURIComponent(provider)}/enabled`, {
            method: 'PUT', body: JSON.stringify({ enabled: true }),
        }).catch(() => {});
        // Everyone ends on the tour, whichever path they took.
        setTimeout(() => onboardStep('tour'), 1200);
    } catch (e) {
        status.className = 'onboard-status bad';
        status.textContent = 'That key did not work: ' + e.message;
    } finally {
        button.disabled = false;
    }
}

async function finishOnboarding(skipped, goTo) {
    document.getElementById('onboard').classList.add('hidden');
    try {
        await api('/api/config/onboarding_done', { method: 'PUT', body: JSON.stringify(true) });
    } catch (_) { /* it is only a "do not show again" flag */ }
    if (goTo && typeof switchTab === 'function') {
        // Landing on Help rather than being told where it is: the difference
        // between knowing a page exists and having seen it.
        switchTab(goTo);
        return;
    }
    // Hand over to the model-download splash unless they skipped outright, or
    // already chose a cloud provider and need no local model.
    if (!skipped && onboardingBootstrapStarted && typeof checkBootstrap === 'function') {
        checkBootstrap();
    }
}

async function maybeShowOnboarding() {
    let done = false;
    try {
        done = !!(await api('/api/config')).onboarding_done;
    } catch (_) { done = true; }        // cannot ask: do not block the app
    if (done) { checkBootstrap(); return; }
    document.getElementById('onboard').classList.remove('hidden');
    onboardStep('welcome');
}

// ===== Temporary chats =====
//
// No memory extraction, no rolling summary, no workspace filing, and deleted
// on the next start. The banner is not decoration: a mode that silently
// changes whether you are being remembered is a mode people forget they are
// in, and the whole value here is knowing.

function toggleTemporaryChat() {
    // Switching mode mid-conversation would be a lie either way — the earlier
    // turns are already remembered, or already not — so it starts a new one.
    if (currentConversationId) newChat();
    temporaryChat = !temporaryChat;
    renderTemporaryState();
}

function renderTemporaryState() {
    document.getElementById('temp-btn')?.classList.toggle('on', temporaryChat);
    let banner = document.getElementById('temp-banner');
    if (!temporaryChat) {
        banner?.remove();
        return;
    }
    if (banner) return;
    const log = document.getElementById('chat-log') || document.getElementById('messages');
    if (!log) return;
    banner = document.createElement('div');
    banner.id = 'temp-banner';
    banner.className = 'temp-banner';
    banner.innerHTML = `
      <strong>Temporary chat.</strong> Nothing here is saved to memory, summarised,
      or filed in a workspace, and the whole conversation is deleted when Carrot
      next starts. Attachments you send are still processed normally.`;
    log.prepend(banner);
}

// A new chat inherits the mode you are in, so the banner has to follow it.
document.addEventListener('DOMContentLoaded', renderTemporaryState);

// ===== "Everything runs on your machine" — only when it does =====
//
// The empty state used to say that unconditionally. With a hosted model
// selected it was simply false, and a privacy claim that is false in the one
// place people read it is worse than no claim at all.

function renderEmptyStateLine() {
    const line = document.getElementById('chat-empty-line');
    if (!line) return;
    const local = currentProvider === 'ollama' || currentProvider === null;
    line.textContent = local
        ? 'Everything runs on your machine. Ask anything below.'
        : `Answers come from ${currentModel || 'a hosted model'} over the internet. `
          + 'Ask anything below.';
    line.classList.toggle('cloud', !local);
}
