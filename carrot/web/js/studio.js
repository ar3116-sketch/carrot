// ===== Sign-in modes and media generation =====
//
// Two settings panels that answer the same question from different ends: how
// does Carrot reach a provider, and what does it get back. Both are built the
// same way — read state, draw it, and never claim something is configured
// when it is not, because the failure that follows is unreadable.

// ---------- How you sign in ----------

async function loadAuthPanel() {
    const host = document.getElementById('auth-panel');
    if (!host) return;
    let providers = [];
    try {
        providers = (await api('/api/auth/status')).providers || [];
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    // Only providers that actually have a consumer plan get the switch. Showing
    // a disabled "Subscription" button next to Groq would just raise a question
    // with no answer.
    const relevant = providers.filter(p => p.subscription_supported);
    if (!relevant.length) {
        host.innerHTML = '<div class="empty">No provider here offers a consumer subscription.</div>';
        return;
    }
    host.innerHTML = '';
    for (const provider of relevant) host.appendChild(authRow(provider));
}

function authRow(provider) {
    const row = document.createElement('div');
    row.className = 'auth-row';
    const usable = provider.usable;
    const detail = provider.mode === 'subscription'
        ? (provider.signed_in
            ? `signed in with ${provider.plan_label}`
            : provider.oauth_configured
                ? 'not signed in yet'
                : 'needs this install’s OAuth client details')
        : (provider.key_set ? 'API key configured' : 'no API key yet');

    row.innerHTML = `
      <div class="auth-main">
        <span class="dot ${usable ? 'ok' : 'warn'}"></span>
        <span class="provider-name">${escHtml(provider.provider)}</span>
        <span class="auth-detail">${escHtml(detail)}</span>
      </div>
      <div class="mode-switch">
        <button class="mode-opt ${provider.mode === 'api_key' ? 'on' : ''}"
                data-mode="api_key">API key</button>
        <button class="mode-opt ${provider.mode === 'subscription' ? 'on' : ''}"
                data-mode="subscription">Subscription</button>
      </div>`;

    row.querySelectorAll('.mode-opt').forEach(button => {
        button.onclick = () => setAuthMode(provider.provider, button.dataset.mode);
    });

    if (provider.mode === 'subscription') {
        const action = document.createElement('button');
        action.className = 'btn btn-ghost';
        action.textContent = provider.signed_in ? 'Sign out' : 'Sign in';
        action.onclick = () => provider.signed_in
            ? signOutProvider(provider.provider)
            : startSignIn(provider.provider);
        row.appendChild(action);
        if (!provider.oauth_configured) row.appendChild(oauthDetails(provider.provider));
    }
    return row;
}

// Carrot ships the shape of the OAuth flow, not someone else's client
// credentials — so an installation supplies its own, here.
function oauthDetails(providerId) {
    const wrap = document.createElement('details');
    wrap.className = 'oauth-details';
    wrap.innerHTML = `
      <summary>OAuth client details</summary>
      <div class="settings-row">
        <input type="text" placeholder="client id" spellcheck="false" data-field="client_id">
        <input type="text" placeholder="https://…/authorize" spellcheck="false" data-field="authorize_url">
      </div>
      <div class="settings-row">
        <input type="text" placeholder="https://…/token" spellcheck="false" data-field="token_url">
        <button class="btn btn-ghost">Save</button>
      </div>`;
    wrap.querySelector('button').onclick = async () => {
        const body = {};
        wrap.querySelectorAll('input').forEach(input => {
            if (input.value.trim()) body[input.dataset.field] = input.value.trim();
        });
        try {
            await api(`/api/auth/oauth/${encodeURIComponent(providerId)}`,
                { method: 'PUT', body: JSON.stringify(body) });
            loadAuthPanel();
        } catch (e) {
            alert('Could not save: ' + (e.detail || e.message));
        }
    };
    return wrap;
}

async function setAuthMode(providerId, mode) {
    try {
        await api(`/api/auth/mode/${encodeURIComponent(providerId)}`,
            { method: 'PUT', body: JSON.stringify({ mode }) });
    } catch (e) {
        alert('Could not switch: ' + (e.detail || e.message));
        return;
    }
    loadAuthPanel();
    if (typeof loadRouting === 'function') loadRouting();
}

async function startSignIn(providerId) {
    let started;
    try {
        started = await api(`/api/auth/login/${encodeURIComponent(providerId)}`, { method: 'POST' });
    } catch (e) {
        alert(e.detail || e.message);
        return;
    }
    // The provider's sign-in page has to open in the real browser: it is where
    // the user is already logged in, and an embedded view is exactly what a
    // phishing page would look like.
    if (window.carrot?.openExternal) window.carrot.openExternal(started.url);
    else window.open(started.url, '_blank', 'noopener');
    // The callback lands on the backend; poll briefly so the panel updates
    // itself rather than making the user hunt for a refresh button.
    let tries = 0;
    const timer = setInterval(async () => {
        tries += 1;
        try {
            const state = await api(`/api/auth/status/${encodeURIComponent(providerId)}`);
            if (state.signed_in || tries > 60) {
                clearInterval(timer);
                loadAuthPanel();
            }
        } catch (_) { clearInterval(timer); }
    }, 2000);
}

async function signOutProvider(providerId) {
    if (!confirm(`Sign out of ${providerId}? Carrot will stop using that subscription.`)) return;
    try {
        await api(`/api/auth/logout/${encodeURIComponent(providerId)}`, { method: 'POST' });
    } catch (_) { /* signing out locally cannot really fail */ }
    loadAuthPanel();
}

// ---------- Image and video generation ----------

let mediaState = null;

async function loadMediaPanel() {
    const host = document.getElementById('media-panel');
    if (!host) return;
    try {
        mediaState = await api('/api/media');
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    host.innerHTML = '';
    for (const backend of mediaState.backends) host.appendChild(mediaRow(backend));
}

function mediaRow(backend) {
    const row = document.createElement('div');
    row.className = 'media-row';
    const isDefault = mediaState.default_image === backend.id
        || mediaState.default_video === backend.id;
    row.innerHTML = `
      <div class="media-main">
        <span class="dot ${backend.configured ? 'ok' : 'warn'}"></span>
        <span class="provider-name">${escHtml(backend.label)}</span>
        ${backend.local ? '<span class="tag">on-device</span>' : ''}
        ${backend.kinds.map(k => `<span class="tag">${escHtml(k)}</span>`).join('')}
        ${isDefault ? '<span class="tag tag-accent">default</span>' : ''}
      </div>
      ${backend.note ? `<div class="muted small">${escHtml(backend.note)}</div>` : ''}`;

    const controls = document.createElement('div');
    controls.className = 'settings-row';
    if (backend.local) {
        // A local server moves — second GPU box, non-default port — so the URL
        // has to be editable without touching a config file.
        const url = document.createElement('input');
        url.type = 'text';
        url.value = backend.base_url;
        url.spellcheck = false;
        url.onchange = () => saveMediaField(backend.id, 'endpoint', { base_url: url.value.trim() });
        controls.appendChild(url);
    } else {
        const key = document.createElement('input');
        key.type = 'password';
        key.placeholder = backend.configured ? 'key configured — paste to replace' : 'API key';
        key.spellcheck = false;
        key.onchange = () => saveMediaField(backend.id, 'key', { api_key: key.value.trim() });
        controls.appendChild(key);
    }
    for (const kind of backend.kinds) {
        const use = document.createElement('button');
        use.className = 'btn btn-ghost';
        use.textContent = `Use for ${kind}`;
        use.onclick = () => setMediaDefault(backend.id, kind);
        controls.appendChild(use);
    }
    row.appendChild(controls);
    return row;
}

async function saveMediaField(backendId, field, body) {
    try {
        await api(`/api/media/backends/${encodeURIComponent(backendId)}/${field}`,
            { method: 'PUT', body: JSON.stringify(body) });
        loadMediaPanel();
    } catch (e) {
        alert('Could not save: ' + (e.detail || e.message));
    }
}

async function setMediaDefault(backendId, kind) {
    try {
        await api('/api/media/default',
            { method: 'PUT', body: JSON.stringify({ backend: backendId, kind }) });
        loadMediaPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

async function tryGenerate() {
    const input = document.getElementById('media-prompt');
    const preview = document.getElementById('media-preview');
    const prompt = input.value.trim();
    if (!prompt) return;
    preview.innerHTML = '<div class="muted small">Generating…</div>';
    try {
        const result = await api('/api/media/generate',
            { method: 'POST', body: JSON.stringify({ prompt }) });
        const where = result.local ? 'on this machine' : result.backend_label;
        preview.innerHTML = `<div class="muted small">${escHtml(where)} · ${result.seconds}s</div>`;
        if (result.artifact) {
            const img = document.createElement('img');
            img.src = result.artifact.content;
            img.alt = prompt;
            preview.appendChild(img);
        }
    } catch (e) {
        preview.innerHTML = `<div class="empty error">${escHtml(e.detail || e.message)}</div>`;
    }
}

// ---------- Local webhooks ----------
//
// The one door into Carrot with no session behind it, so the panel is built to
// make that obvious: an explicit switch, one named action per hook, and the
// token shown exactly once at creation rather than rendered in a list forever.

let hooksState = null;

async function loadHooksPanel() {
    const host = document.getElementById('hooks-panel');
    if (!host) return;
    try {
        hooksState = await api('/api/webhooks');
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    const toggle = document.getElementById('hooks-enabled');
    if (toggle) toggle.checked = !!hooksState.enabled;

    const actions = document.getElementById('hook-action');
    if (actions && !actions.dataset.loaded) {
        actions.dataset.loaded = '1';
        actions.innerHTML = hooksState.actions
            .map(a => `<option value="${escHtml(a.id)}">${escHtml(a.description)}</option>`).join('');
    }

    host.innerHTML = '';
    if (!hooksState.hooks.length) {
        host.innerHTML = '<div class="empty">No hooks yet.</div>';
    }
    for (const hook of hooksState.hooks) host.appendChild(hookRow(hook));
    renderHookTargets();
}

function hookRow(hook) {
    const row = document.createElement('div');
    row.className = 'hook-row';
    row.innerHTML = `
      <div class="hook-main">
        <span class="provider-name">${escHtml(hook.label || hook.id)}</span>
        <span class="tag">${escHtml(hook.action)}</span>
        ${hook.fires ? `<span class="muted small">${hook.fires} call(s)</span>` : ''}
      </div>
      <code class="offer-cmd">${escHtml(hook.url)}</code>`;

    const controls = document.createElement('div');
    controls.className = 'settings-row';
    const rotate = document.createElement('button');
    rotate.className = 'btn btn-ghost';
    rotate.textContent = 'New token';
    rotate.onclick = () => rotateHook(hook.id);
    const remove = document.createElement('button');
    remove.className = 'btn btn-ghost';
    remove.textContent = 'Delete';
    remove.onclick = () => deleteHook(hook.id, hook.label || hook.id);
    controls.append(rotate, remove);
    row.appendChild(controls);
    return row;
}

async function setHooksEnabled(enabled) {
    try {
        await api('/api/webhooks/enabled',
            { method: 'PUT', body: JSON.stringify({ enabled }) });
        loadHooksPanel();
    } catch (e) {
        alert('Could not change that: ' + (e.detail || e.message));
    }
}

async function createHook() {
    const id = document.getElementById('hook-id').value.trim();
    const action = document.getElementById('hook-action').value;
    if (!id) return;
    let made;
    try {
        made = await api('/api/webhooks/hooks',
            { method: 'POST', body: JSON.stringify({ id, action }) });
    } catch (e) {
        alert(e.detail || e.message);
        return;
    }
    document.getElementById('hook-id').value = '';
    showHookToken(made);
    loadHooksPanel();
}

// The token is shown once, here, with a copyable example — because the next
// thing anyone does is paste it into Home Assistant, and hunting for the right
// curl incantation is where people give up.
function showHookToken(hook) {
    const host = document.getElementById('hook-created');
    host.classList.remove('hidden');
    const example = `curl -X POST ${hook.url} \\\n`
        + `  -H "Authorization: Bearer ${hook.token}" \\\n`
        + `  -H "Content-Type: application/json" \\\n`
        + `  -d '{"title": "Hello from my house"}'`;
    host.innerHTML = `
      <div class="hook-token-warn">This token is shown once. Copy it now.</div>
      <pre class="offer-cmd hook-example">${escHtml(example)}</pre>`;
    const copy = document.createElement('button');
    copy.className = 'btn btn-primary';
    copy.textContent = 'Copy the command';
    copy.onclick = () => {
        navigator.clipboard?.writeText(example);
        copy.textContent = 'Copied';
    };
    host.appendChild(copy);
}

async function rotateHook(id) {
    if (!confirm(`Give "${id}" a new token? Anything using the old one stops working.`)) return;
    try {
        showHookToken(await api(`/api/webhooks/hooks/${encodeURIComponent(id)}/rotate`,
            { method: 'POST' }));
    } catch (e) {
        alert(e.detail || e.message);
    }
}

async function deleteHook(id, label) {
    if (!confirm(`Delete "${label}"? Anything calling it stops working.`)) return;
    try {
        await api(`/api/webhooks/hooks/${encodeURIComponent(id)}`, { method: 'DELETE' });
        loadHooksPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

function renderHookTargets() {
    const host = document.getElementById('hook-targets');
    if (!host) return;
    const targets = hooksState?.targets || [];
    host.innerHTML = targets.length ? '' : '<div class="empty">No targets yet.</div>';
    for (const target of targets) {
        const row = document.createElement('div');
        row.className = 'hook-row';
        row.innerHTML = `<div class="hook-main">
            <span class="provider-name">${escHtml(target.label)}</span>
            <code class="offer-cmd">${escHtml(target.url)}</code></div>`;
        const remove = document.createElement('button');
        remove.className = 'btn btn-ghost';
        remove.textContent = 'Remove';
        remove.onclick = async () => {
            await api(`/api/webhooks/targets/${encodeURIComponent(target.id)}`,
                { method: 'DELETE' }).catch(() => {});
            loadHooksPanel();
        };
        row.appendChild(remove);
        host.appendChild(row);
    }
}

async function addHookTarget() {
    const input = document.getElementById('target-url');
    const url = input.value.trim();
    if (!url) return;
    try {
        await api('/api/webhooks/targets',
            { method: 'POST', body: JSON.stringify({ url, events: ['notification'] }) });
        input.value = '';
        loadHooksPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

// ---------- Consensus & debate ----------
//
// The valuable output of a debate is not the answer, it is the disagreement.
// So the panel renders the split first and the prose second: a run where two
// models contradicted each other is telling you to go look, and burying that
// under a confident paragraph would waste the whole exercise.

let consensusState = null;
// The models this machine can actually reach, from the same endpoint the
// composer's picker uses. Typing a model name by hand was the wrong answer to
// a question the app already knows: a typo produced a panel member that failed
// at debate time rather than at pick time.
let availableModels = [];

// The composer's Debate chip is unhidden by this, and the settings host does
// not exist outside the Settings tab — so bailing on a missing host meant the
// chip stayed hidden forever for anyone who never opened Settings. Which is
// everyone: the feature was invisible in the only place it is used.
async function loadConsensusPanel() {
    const host = document.getElementById('consensus-panel');
    try {
        consensusState = await api('/api/consensus');
    } catch (e) {
        if (host) host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    // The chip first, and unconditionally: it is the part that lives outside
    // this panel and it must reflect the state wherever you are.
    renderCouncilChip();
    if (!host) return;

    await loadAvailableModels();
    renderPanelMembers(host);
    renderPanelAdder();
    renderJudgePicker();
    renderPanelSuggestion();
}

// Always shown, never hidden. A feature you cannot see is a feature you do not
// have: hiding the chip until a panel existed meant nobody ever discovered
// that a panel was a thing you could make. Without one it explains itself and
// takes you to the setup instead of doing nothing.
function renderCouncilChip() {
    const chip = document.getElementById('debate-btn');
    if (!chip) return;
    chip.classList.remove('hidden');
    const ready = !!consensusState?.ready;
    chip.classList.toggle('needs-setup', !ready);
    chip.title = ready
        ? `Ask your council — ${consensusState.panel.map(m => m.model).join(', ')} — `
          + 'and make them argue before answering'
        : 'Set up a model council: two or more models answer, critique each '
          + 'other, then one writes the final answer';
}

async function loadAvailableModels() {
    try {
        const data = await api('/api/models');
        availableModels = [
            ...(data.installed || []).map(m => ({
                model: m.name, provider: 'ollama', group: 'On this machine',
            })),
            ...(data.remote || []).flatMap(group => (group.models || []).map(name => ({
                model: name, provider: group.provider, group: group.label,
            }))),
        ];
    } catch (_) {
        availableModels = [];
    }
}

function memberKey(member) {
    return `${member.provider || 'ollama'}::${member.model}`;
}

function renderPanelMembers(host) {
    host.innerHTML = '';
    if (!consensusState.panel.length) {
        host.innerHTML = `<div class="empty">No panel yet — pick at least `
            + `${consensusState.min_members} models below.</div>`;
        return;
    }
    consensusState.panel.forEach((member, index) => {
        const row = document.createElement('div');
        row.className = 'hook-row';
        row.innerHTML = `<div class="hook-main">
            <span class="provider-name">${escHtml(member.model)}</span>
            <span class="tag">${escHtml(member.provider || 'on-device')}</span>
          </div>`;
        const remove = document.createElement('button');
        remove.className = 'btn btn-ghost';
        remove.textContent = 'Remove';
        remove.onclick = () => savePanel(consensusState.panel.filter((_, i) => i !== index));
        row.appendChild(remove);
        host.appendChild(row);
    });
}

function renderPanelAdder() {
    const select = document.getElementById('consensus-add');
    if (!select) return;
    const chosen = new Set(consensusState.panel.map(memberKey));
    const groups = {};
    for (const entry of availableModels) {
        if (chosen.has(memberKey(entry))) continue;   // already on the panel
        (groups[entry.group] ||= []).push(entry);
    }
    const full = consensusState.panel.length >= consensusState.max_members;
    select.disabled = full;
    select.innerHTML = `<option value="">${
        full ? `Panel is full (${consensusState.max_members} models)` : 'Add a model to the panel…'
    }</option>`;
    for (const [group, entries] of Object.entries(groups)) {
        const optgroup = document.createElement('optgroup');
        optgroup.label = group;
        for (const entry of entries) {
            const option = document.createElement('option');
            option.value = memberKey(entry);
            option.textContent = entry.model;
            optgroup.appendChild(option);
        }
        select.appendChild(optgroup);
    }
    if (!availableModels.length) {
        select.innerHTML = '<option value="">No models found — pull one, or add a provider key</option>';
        select.disabled = true;
    }
}

function renderJudgePicker() {
    const select = document.getElementById('consensus-judge');
    if (!select) return;
    const current = consensusState.synthesiser?.model || '';
    // Only panel members: a judge that never saw the debate is not a judge.
    select.innerHTML = '<option value="">The first model on the panel</option>';
    for (const member of consensusState.panel) {
        const option = document.createElement('option');
        option.value = memberKey(member);
        option.textContent = `${member.model} (${member.provider || 'on-device'})`;
        option.selected = member.model === current;
        select.appendChild(option);
    }
}

// A panel of two models from the same family agrees for the same reasons, so
// the suggestion deliberately pairs different ones when it can see them.
function renderPanelSuggestion() {
    const host = document.getElementById('consensus-suggest');
    if (!host) return;
    host.innerHTML = '';
    if (consensusState.panel.length >= consensusState.min_members) return;
    const local = availableModels.filter(m => m.provider === 'ollama');
    if (local.length < 2) return;

    const family = name => (name || '').split(/[:\-.]/)[0].toLowerCase();
    let pair = null;
    for (let i = 0; i < local.length && !pair; i++) {
        for (let j = i + 1; j < local.length; j++) {
            if (family(local[i].model) !== family(local[j].model)) {
                pair = [local[i], local[j]];
                break;
            }
        }
    }
    if (!pair) return;

    const button = document.createElement('button');
    button.className = 'btn btn-ghost';
    button.textContent = `Use ${pair[0].model} and ${pair[1].model}`;
    button.onclick = () => savePanel(pair.map(p => ({ model: p.model, provider: p.provider })));
    const note = document.createElement('div');
    note.className = 'muted small';
    note.textContent = 'Suggested because they are different families, so they tend to '
        + 'be wrong about different things:';
    host.append(note, button);
}

async function savePanel(members) {
    try {
        await api('/api/consensus/panel',
            { method: 'PUT', body: JSON.stringify({ members }) });
        loadConsensusPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

function addPanelMember(select) {
    const value = select?.value;
    if (!value) return;
    select.value = '';
    const [provider, model] = value.split('::');
    savePanel([...(consensusState?.panel || []), { model, provider }]);
}

async function saveSynthesiser(value) {
    const [provider, model] = (value || '').split('::');
    try {
        await api('/api/consensus/synthesiser',
            { method: 'PUT', body: JSON.stringify({ model: model || '', provider: provider || '' }) });
        loadConsensusPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

// ---------- Running one from the composer ----------

async function debateCurrentQuestion() {
    if (!consensusState?.ready) {
        // Take them to the setup rather than failing quietly. A chip that does
        // nothing teaches people the feature is broken.
        if (typeof switchTab === 'function') switchTab('settings');
        setTimeout(() => document.getElementById('consensus-panel')
            ?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 300);
        return;
    }
    const input = document.getElementById('cmd-input') || document.getElementById('chat-input');
    const question = (input?.value || '').trim();
    if (!question) return;
    input.value = '';

    if (typeof appendMessage === 'function') appendMessage('user', question);
    const el = typeof appendMessage === 'function' ? appendMessage('assistant', '') : null;
    const content = el?.querySelector('.content');
    const trace = document.createElement('div');
    trace.className = 'debate-trace';
    if (el && content) el.insertBefore(trace, content);

    function step(text) {
        const line = document.createElement('div');
        line.className = 'debate-step';
        line.textContent = text;
        trace.appendChild(line);
    }
    step('asking the panel…');

    try {
        const response = await fetch('/api/consensus/debate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ question, stream: true }),
        });
        if (!response.ok) throw new Error((await response.json()).detail || 'failed');

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split('\n\n');
            buffer = frames.pop();
            for (const frame of frames) {
                const line = frame.split('\n').find(l => l.startsWith('data: '));
                if (!line) continue;
                let payload;
                try { payload = JSON.parse(line.slice(6)); } catch (_) { continue; }
                if (payload.round === 'propose') step(`${payload.members} models answering independently`);
                if (payload.round === 'critique') step('models critiquing each other, anonymously');
                if (payload.round === 'synthesis') step('writing the final answer');
                if (payload.search_mode && payload.search_mode !== 'off') {
                    step(`each model may search for its own evidence (${payload.search_mode})`);
                }
                if (payload.proposals) {
                    for (const p of payload.proposals) {
                        const searched = p.tool_calls ? `, ${p.tool_calls} lookup(s)` : '';
                        step(`  ${p.model}: ${p.ok ? `${p.seconds}s${searched}` : `failed — ${p.error}`}`);
                    }
                }
                if (payload.error) step('error: ' + payload.error);
                if (payload.done) renderDebate(payload.done, content);
            }
        }
    } catch (e) {
        if (content) content.textContent = 'Debate failed: ' + e.message;
    }
}

function renderDebate(run, content) {
    if (!content) return;
    content.innerHTML = '';

    // Disagreement first. A split panel is the finding; the prose is the
    // summary of it, and showing them the other way round buries the point.
    if (run.disagreements?.length) {
        const box = document.createElement('div');
        box.className = 'debate-split';
        box.innerHTML = `<div class="split-head">The panel did not agree — worth reading</div>`;
        for (const item of run.disagreements.slice(0, 6)) {
            const row = document.createElement('div');
            row.className = 'split-point';
            row.textContent = item.point;
            box.appendChild(row);
        }
        content.appendChild(box);
    }
    if (run.degraded) {
        const warn = document.createElement('div');
        warn.className = 'debate-split';
        warn.textContent = run.degraded;
        content.appendChild(warn);
    }

    const answer = document.createElement('div');
    answer.innerHTML = typeof mdToHtml === 'function' ? mdToHtml(run.answer) : escHtml(run.answer);
    content.appendChild(answer);

    const footer = document.createElement('div');
    footer.className = 'debate-footer';
    const models = (run.proposals || []).map(p => p.model).join(', ');
    footer.textContent = `${models} · ${run.seconds}s · `
        + (run.agreement >= 0.5 ? 'the answers were close' : 'the answers diverged');
    content.appendChild(footer);
}

// ---------- What Carrot is allowed to see ----------
//
// The rules for ambient capture, shipped before ambient capture. The panel is
// deliberately a list of switches that are already on rather than a list of
// things to opt into: an exclusion you have to know to add is an exclusion
// that only protects the people who already knew.

let ambientState = null;

// The four built-in protections, in the order they matter. `hint` is the part
// worth reading — a switch labelled "skip private windows" is a setting, and a
// sentence explaining why it is already on is a promise.
const AMBIENT_GUARDS = [
    ['skip_private_windows', 'Never capture private browsing',
     'Incognito, Private Browsing, InPrivate. You already said what you wanted by opening one.'],
    ['skip_password_fields', 'Never capture while typing a password',
     'When the OS says a secure input field has focus, capture stops until it does not.'],
    ['skip_known_secret_apps', 'Never capture password managers',
     '1Password, Bitwarden, KeePass, Keychain and the rest — no configuration needed.'],
    ['skip_sensitive_titles', 'Skip banking, medical and tax windows',
     'Matched on the window title, so it works for a site Carrot has never heard of.'],
];

const AMBIENT_RESOURCE_GUARDS = [
    ['yield_to_models', 'Stand aside while a model is working',
     'You are waiting on that answer. You are not waiting on the screen index.'],
    ['skip_when_idle', 'Stop when you are away from the machine',
     'Capturing a screensaver a thousand times is pure cost.'],
    ['pause_on_battery', 'Stop entirely on battery',
     'Off by default — it already slows down and stops at a low charge.'],
];

async function loadAmbientPanel() {
    const host = document.getElementById('ambient-panel');
    if (!host) return;
    try {
        ambientState = await api('/api/ambient');
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    const p = ambientState.policy;
    host.innerHTML = '';

    const master = document.createElement('div');
    master.className = 'settings-row';
    master.innerHTML = `
      <label class="switch">
        <input type="checkbox" id="ambient-enabled" ${p.enabled ? 'checked' : ''}>
        <span>Let Carrot read the screen in the background</span>
      </label>`;
    master.querySelector('input').onchange = (e) =>
        saveAmbientPolicy({ enabled: e.target.checked });
    host.appendChild(master);

    // What the gate says right now — including "it is off", which is the most
    // common answer and the most confusing one to leave unsaid.
    const state = document.createElement('div');
    state.className = 'muted small';
    const decision = ambientState.decision || {};
    state.textContent = decision.allowed
        ? `Right now: capturing, next look in ${Math.round(decision.retry_after)}s.`
        : `Right now: not capturing — ${decision.reason}.`;
    host.appendChild(state);

    host.appendChild(ambientGuardGroup('Privacy', AMBIENT_GUARDS, p));
    host.appendChild(ambientGuardGroup('Your machine', AMBIENT_RESOURCE_GUARDS, p));

    const cadence = document.createElement('div');
    cadence.className = 'settings-row';
    cadence.innerHTML = `
      <label class="muted small" for="ambient-interval">Look every</label>
      <input type="number" id="ambient-interval" min="2" max="120" step="1"
             value="${p.interval_seconds}" style="width:5em">
      <span class="muted small">seconds — and every</span>
      <input type="number" id="ambient-battery-interval" min="2" max="120" step="1"
             value="${p.battery_interval_seconds}" style="width:5em">
      <span class="muted small">seconds on battery, stopping below</span>
      <input type="number" id="ambient-battery-floor" min="0" max="100" step="5"
             value="${p.battery_floor_percent}" style="width:5em">
      <span class="muted small">% charge.</span>`;
    for (const [id, key] of [['ambient-interval', 'interval_seconds'],
                             ['ambient-battery-interval', 'battery_interval_seconds'],
                             ['ambient-battery-floor', 'battery_floor_percent']]) {
        cadence.querySelector('#' + id).onchange = (e) =>
            saveAmbientPolicy({ [key]: Number(e.target.value) });
    }
    host.appendChild(cadence);

    const pause = document.createElement('div');
    pause.className = 'settings-row';
    const paused = p.paused_until > Date.now() / 1000;
    const btn = document.createElement('button');
    btn.className = 'btn btn-ghost';
    btn.textContent = paused ? 'Resume capture' : 'Pause for an hour';
    btn.onclick = () => pauseAmbient(paused);
    pause.appendChild(btn);
    host.appendChild(pause);

    renderAmbientExclusions();
}

function ambientGuardGroup(heading, guards, policy) {
    const group = document.createElement('div');
    group.style.marginTop = '10px';
    const title = document.createElement('div');
    title.className = 'muted small';
    title.textContent = heading;
    group.appendChild(title);
    for (const [key, label, hint] of guards) {
        const row = document.createElement('div');
        row.className = 'settings-row';
        row.innerHTML = `
          <label class="switch">
            <input type="checkbox" ${policy[key] ? 'checked' : ''}>
            <span>${escHtml(label)}<br><span class="muted small">${escHtml(hint)}</span></span>
          </label>`;
        row.querySelector('input').onchange = (e) =>
            saveAmbientPolicy({ [key]: e.target.checked });
        group.appendChild(row);
    }
    return group;
}

function renderAmbientExclusions() {
    const host = document.getElementById('ambient-exclusions');
    if (!host || !ambientState) return;
    const p = ambientState.policy;
    host.innerHTML = '';
    const entries = [
        ...p.excluded_apps.map(v => ['app', v]),
        ...p.excluded_titles.map(v => ['title', v]),
        ...p.excluded_urls.map(v => ['url', v]),
    ];
    if (!entries.length) {
        host.innerHTML = '<div class="muted small">'
            + 'Nothing of your own yet — the built-in rules above already cover private '
            + 'windows, password managers and password fields.</div>';
        return;
    }
    for (const [kind, value] of entries) {
        const row = document.createElement('div');
        row.className = 'settings-row';
        row.innerHTML = `<span class="tag">${escHtml(kind)}</span>
                         <span class="provider-name">${escHtml(value)}</span>`;
        const remove = document.createElement('button');
        remove.className = 'btn btn-ghost';
        remove.textContent = 'Remove';
        remove.onclick = () => removeAmbientExclusion(kind, value);
        row.appendChild(remove);
        host.appendChild(row);
    }
}

async function saveAmbientPolicy(changes) {
    try {
        await api('/api/ambient/policy',
            { method: 'PUT', body: JSON.stringify({ policy: changes }) });
    } catch (e) {
        alert('Could not save that: ' + (e.detail || e.message));
    }
    loadAmbientPanel();
}

async function addAmbientExclusion() {
    const kind = document.getElementById('ambient-kind').value;
    const input = document.getElementById('ambient-value');
    const value = input.value.trim();
    if (!value) return;
    try {
        await api('/api/ambient/exclusions',
            { method: 'POST', body: JSON.stringify({ kind, value }) });
    } catch (e) {
        alert(e.detail || e.message);
        return;
    }
    input.value = '';
    loadAmbientPanel();
}

async function removeAmbientExclusion(kind, value) {
    try {
        await api('/api/ambient/exclusions/remove',
            { method: 'POST', body: JSON.stringify({ kind, value }) });
    } catch (e) {
        alert(e.detail || e.message);
        return;
    }
    loadAmbientPanel();
}

async function pauseAmbient(resuming) {
    try {
        await api(resuming ? '/api/ambient/resume' : '/api/ambient/pause',
            { method: 'POST', body: JSON.stringify({ minutes: 60 }) });
    } catch (e) {
        alert(e.detail || e.message);
    }
    loadAmbientPanel();
}

// The honesty test. A promise that private windows are skipped is worth less
// than watching one be refused, so you can type a window that does not exist
// and see exactly which rule catches it.
async function tryAmbientWindow() {
    const out = document.getElementById('ambient-try-result');
    if (!out) return;
    const app = document.getElementById('ambient-try-app').value.trim();
    const title = document.getElementById('ambient-try-title').value.trim();
    if (!app && !title) {
        out.textContent = 'Type an app or a window title to try.';
        return;
    }
    let result;
    try {
        result = await api('/api/ambient/check',
            { method: 'POST', body: JSON.stringify({ app, title }) });
    } catch (e) {
        out.textContent = e.detail || e.message;
        return;
    }
    // The privacy gate is the interesting one: "it is off" is true but useless
    // when what you are asking is whether this window would ever be read.
    const privacy = result.privacy || {};
    const overall = result.decision || {};
    out.textContent = privacy.allowed
        ? (overall.allowed
            ? 'That window would be captured.'
            : `Nothing about that window is private, but capture is not running: ${overall.reason}.`)
        : `That window would never be captured — ${privacy.reason}.`;
}
