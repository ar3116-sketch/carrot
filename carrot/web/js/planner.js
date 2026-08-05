// ===== Semester planner =====
//
// A photo of your schedule in, a week you can actually live in out. The hard
// parts are not the grid: they are the questions nobody volunteers the answer
// to (where you live, when you sleep), and the minutes between two buildings.
// This file is the interface to both; the arithmetic is in carrot/planner.py.

let plannerState = null;

async function loadPlanner() {
    try {
        plannerState = await api('/api/planner/state');
    } catch (e) {
        document.getElementById('planner-intake').innerHTML =
            `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    renderIntake();
    renderCourses(plannerState.courses, []);
    renderBuildings();
    if (plannerState.plan && plannerState.plan.days) renderWeek(plannerState.plan);
    const status = document.getElementById('planner-status');
    status.textContent = plannerState.ready
        ? 'ready to plan'
        : `${plannerState.missing.length} question(s) left`;
}

// ---------- Telling Carrot in a sentence ----------
//
// Fourteen labelled boxes is a form, and nobody fills in a form to try
// something out. One sentence answers four of them at once; the ones it could
// not answer are the only ones worth asking about, and they get asked here
// rather than sitting on the page as empty required fields.

async function tellPlanner() {
    const input = document.getElementById('planner-freeform');
    const host = document.getElementById('planner-conversation');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    say('you', text);
    say('carrot', 'reading that…', 'pending');

    let body;
    try {
        body = await api('/api/planner/understand',
            { method: 'POST', body: JSON.stringify({ text }) });
    } catch (e) {
        replacePending(e.detail || e.message);
        return;
    }

    const got = Object.entries(body.understood || {});
    const heard = got.length
        ? 'Got it — ' + got.map(([k, v]) => `${labelFor(k)}: ${v}`).join(', ') + '.'
        : 'I could not pull anything definite out of that.';
    const next = body.next_question
        ? ` ${body.next_question.question} (${body.next_question.why})`
        : ' That is everything I need — add your classes and I can build the week.';
    replacePending(heard + next);
    plannerState = { ...(plannerState || {}), answers: body.answers, missing: body.missing };
    loadPlanner();
    if (body.next_question) input.focus();
}

function labelFor(id) {
    const question = (plannerState?.questions || []).find(q => q.id === id);
    return question ? question.question.replace(/\?.*$/, '').toLowerCase() : id;
}

function say(who, text, cls) {
    const host = document.getElementById('planner-conversation');
    if (!host) return;
    const row = document.createElement('div');
    row.className = 'planner-said ' + who + (cls ? ' ' + cls : '');
    row.textContent = text;
    host.appendChild(row);
    host.scrollTop = host.scrollHeight;
}

function replacePending(text) {
    const pending = document.querySelector('#planner-conversation .pending');
    if (pending) {
        pending.textContent = text;
        pending.classList.remove('pending');
    } else {
        say('carrot', text);
    }
}

// ---------- Intake ----------

function renderIntake() {
    const host = document.getElementById('planner-intake');
    host.innerHTML = '';
    for (const question of plannerState.questions) {
        const answered = plannerState.answers[question.id] || '';
        const row = document.createElement('div');
        row.className = 'intake-row' + (question.required && !answered ? ' needed' : '');

        const label = document.createElement('label');
        label.className = 'intake-q';
        label.textContent = question.question;
        if (question.required) {
            const star = document.createElement('span');
            star.className = 'intake-required';
            star.textContent = ' required';
            label.appendChild(star);
        }
        row.appendChild(label);

        // A stranger asking which dorm you live in owes you a reason.
        const why = document.createElement('div');
        why.className = 'intake-why';
        why.textContent = question.why;
        row.appendChild(why);

        let field;
        if (question.kind === 'choice') {
            field = document.createElement('select');
            field.innerHTML = (question.options || [])
                .map(o => `<option value="${escHtml(o)}">${escHtml(o)}</option>`).join('');
            field.value = answered || question.default || '';
        } else {
            field = document.createElement('input');
            field.type = 'text';
            field.spellcheck = false;
            field.value = answered;
            field.placeholder = question.default || '';
        }
        field.className = 'intake-input';
        field.onchange = () => saveAnswer(question.id, field.value.trim());
        row.appendChild(field);
        host.appendChild(row);
    }
}

async function saveAnswer(id, value) {
    try {
        const body = await api('/api/planner/answers', {
            method: 'PUT', body: JSON.stringify({ answers: { [id]: value } }),
        });
        plannerState.answers = body.answers;
        plannerState.missing = body.missing;
        document.getElementById('planner-status').textContent = body.ready
            ? 'ready to plan' : `${body.missing.length} question(s) left`;
        document.querySelectorAll('.intake-row').forEach((row, i) => {
            const question = plannerState.questions[i];
            row.classList.toggle('needed',
                question.required && !String(plannerState.answers[question.id] || '').trim());
        });
    } catch (e) {
        document.getElementById('planner-status').textContent = 'could not save: ' + e.message;
    }
}

// ---------- Reading the schedule ----------

async function readSyllabusFile(file) {
    if (!file) return;
    const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
        reader.onerror = reject;
        reader.readAsDataURL(file);
    });
    await readSyllabus({ image: data, name: file.name });
}

async function readSyllabusText() {
    const text = document.getElementById('planner-text').value.trim();
    if (!text) return;
    await readSyllabus({ text });
}

async function readSyllabus(payload) {
    const host = document.getElementById('planner-courses');
    host.innerHTML = '<div class="muted small">Reading the schedule…</div>';
    let body;
    try {
        body = await api('/api/planner/syllabus', {
            method: 'POST', body: JSON.stringify(payload),
        });
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.detail || e.message)}</div>`;
        return;
    }
    // Nothing is saved yet. Extraction from a photo is the step most likely to
    // be subtly wrong, so it is shown for confirmation first.
    renderCourses(body.courses, body.problems, true);
}

function renderCourses(courses, problems, unconfirmed) {
    const host = document.getElementById('planner-courses');
    host.innerHTML = '';
    if (!courses || !courses.length) {
        if (!unconfirmed) host.innerHTML = '<div class="empty">No classes yet.</div>';
        return;
    }
    for (const problem of problems || []) {
        const warn = document.createElement('div');
        warn.className = 'planner-problem';
        warn.textContent = problem;
        host.appendChild(warn);
    }

    const table = document.createElement('div');
    table.className = 'course-table';
    table.innerHTML = `
      <div class="course-row head">
        <span>Code</span><span>Title</span><span>Days</span>
        <span>Start</span><span>End</span><span>Where</span><span></span>
      </div>`;
    courses.forEach((course, index) => {
        const row = document.createElement('div');
        row.className = 'course-row';
        for (const field of ['code', 'title', 'days', 'start', 'end', 'location']) {
            const input = document.createElement('input');
            input.type = 'text';
            input.value = course[field] || '';
            input.spellcheck = false;
            // Editable on purpose: a misread room number is easier to fix here
            // than to discover on the walk there.
            input.onchange = () => { courses[index][field] = input.value.trim(); };
            row.appendChild(input);
        }
        const remove = document.createElement('button');
        remove.className = 'icon-btn';
        remove.textContent = '×';
        remove.onclick = () => { courses.splice(index, 1); renderCourses(courses, [], unconfirmed); };
        row.appendChild(remove);
        table.appendChild(row);
    });
    host.appendChild(table);

    const actions = document.createElement('div');
    actions.className = 'settings-row';
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = unconfirmed ? 'These look right — save them' : 'Save changes';
    save.onclick = () => saveCourses(courses);
    actions.appendChild(save);
    host.appendChild(actions);
}

async function saveCourses(courses) {
    try {
        const body = await api('/api/planner/courses', {
            method: 'PUT', body: JSON.stringify({ courses }),
        });
        plannerState.courses = body.courses;
        document.getElementById('planner-status').textContent =
            `${body.meetings} class meetings a week saved`;
        renderCourses(body.courses, body.problems);
        loadPlanner();
    } catch (e) {
        document.getElementById('planner-status').textContent = 'could not save: ' + e.message;
    }
}

// ---------- Campus ----------

function renderBuildings() {
    const host = document.getElementById('planner-buildings');
    const known = plannerState.buildings_known || [];
    const needed = plannerState.places_needed || [];
    const missing = needed.filter(place =>
        !known.some(k => k.toLowerCase().includes(place.toLowerCase().split(' ')[0])));
    host.innerHTML = '';
    if (!needed.length) {
        host.innerHTML = '<div class="empty">Add your classes first.</div>';
        return;
    }
    const list = document.createElement('div');
    list.className = 'building-list';
    for (const place of needed) {
        const chip = document.createElement('span');
        const unknown = missing.includes(place);
        chip.className = 'tag' + (unknown ? '' : ' tag-accent');
        chip.textContent = place + (unknown ? ' — unknown' : '');
        list.appendChild(chip);
    }
    host.appendChild(list);
    if (missing.length) {
        const note = document.createElement('div');
        note.className = 'muted small';
        // Being explicit matters: these are the walks the planner has to guess.
        note.textContent = `${missing.length} place(s) have no coordinates yet, so `
            + `travel time to them is a generous guess rather than a real number.`;
        host.appendChild(note);
    }
}

async function lookupCampus() {
    const status = document.getElementById('planner-campus-status');
    status.textContent = 'looking them up…';
    try {
        const body = await api('/api/planner/campus/lookup', { method: 'POST' });
        status.textContent = `found ${body.found.length}`
            + (body.still_unknown.length ? `, still unsure of ${body.still_unknown.length}` : '');
        await loadPlanner();
    } catch (e) {
        status.textContent = e.detail || e.message;
    }
}

// ---------- The week ----------

async function buildPlan() {
    const status = document.getElementById('planner-build-status');
    status.textContent = 'building…';
    try {
        const plan = await api('/api/planner/plan', { method: 'POST', body: JSON.stringify({}) });
        status.textContent = '';
        renderWeek(plan);
    } catch (e) {
        status.textContent = e.detail || e.message;
    }
}

const BLOCK_LABELS = {
    class: 'Class', meal: 'Meal', gym: 'Gym', study: 'Study',
    commute: 'Travel', work: 'Work', fixed: 'Fixed',
};

function renderWeek(plan) {
    const notes = document.getElementById('planner-notes');
    notes.innerHTML = '';
    // Conflicts and warnings go above the grid: a plan that quietly contains an
    // unwalkable gap is worse than one that says so.
    for (const clash of plan.conflicts || []) {
        const row = document.createElement('div');
        row.className = 'planner-problem';
        row.textContent = `Clash on ${clash.day}: ${clash.a} and ${clash.b} both run ${clash.when}.`;
        notes.appendChild(row);
    }
    for (const note of plan.notes || []) {
        const row = document.createElement('div');
        row.className = 'planner-note';
        row.textContent = note;
        notes.appendChild(row);
    }
    if (plan.totals) {
        const totals = document.createElement('div');
        totals.className = 'planner-totals';
        totals.textContent = Object.entries(plan.totals)
            .map(([kind, hours]) => `${BLOCK_LABELS[kind] || kind}: ${hours}h`).join(' · ');
        notes.appendChild(totals);
    }

    const host = document.getElementById('planner-week');
    host.innerHTML = '';
    for (const day of plan.days || []) {
        const column = document.createElement('div');
        column.className = 'day-column';
        column.innerHTML = `<div class="day-head">${escHtml(day.label)}</div>`;
        if (!day.blocks.length) {
            column.innerHTML += '<div class="day-empty">nothing scheduled</div>';
        }
        for (const block of day.blocks) {
            const el = document.createElement('div');
            el.className = 'day-block kind-' + block.kind;
            el.innerHTML = `
              <div class="block-time">${escHtml(block.start_label)}</div>
              <div class="block-title">${escHtml(block.title)}</div>
              ${block.place ? `<div class="block-place">${escHtml(block.place)}</div>` : ''}`;
            column.appendChild(el);
        }
        host.appendChild(column);
    }
}

// Drop or paste a screenshot of the schedule straight onto the tab.
document.addEventListener('DOMContentLoaded', () => {
    const drop = document.getElementById('planner-drop');
    if (!drop) return;
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('dropping'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dropping'));
    drop.addEventListener('drop', (e) => {
        e.preventDefault();
        drop.classList.remove('dropping');
        readSyllabusFile(e.dataTransfer?.files?.[0]);
    });
    document.addEventListener('paste', (e) => {
        if (document.getElementById('view-planner')?.classList.contains('active')) {
            const file = Array.from(e.clipboardData?.files || [])[0];
            if (file) { e.preventDefault(); readSyllabusFile(file); }
        }
    });
});
