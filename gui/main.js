const { app, BrowserWindow, dialog, globalShortcut, ipcMain, Notification, screen, session, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const http = require('http');

const BACKEND_URL = 'http://127.0.0.1:8181';
const IS_DEV = process.argv.includes('--dev');

// The quick-ask panel. It was 620x92, which fits a single line of text and
// nothing else — no room for an attachment chip, a workspace name, or a
// reply worth reading.
const OVERLAY_WIDTH = 760;
const OVERLAY_HEIGHT = 148;
const OVERLAY_MAX_HEIGHT = 620;

let mainWindow = null;
let overlayWindow = null;
let fastapiProcess = null;

// ===== Backend lifecycle =====
// Packaged app: launch the frozen backend bundled in resources/ — end
// users never need Python. Dev checkout: fall back to the system Python.
function backendCommand() {
  if (app.isPackaged) {
    const exeName = process.platform === 'win32' ? 'carrot-backend.exe' : 'carrot-backend';
    const exe = path.join(process.resourcesPath, 'backend', 'carrot-backend', exeName);
    if (fs.existsSync(exe)) {
      return { cmd: exe, args: [], cwd: path.dirname(exe) };
    }
    console.error(`Bundled backend not found at ${exe}; falling back to system Python.`);
  }
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  return { cmd: pythonCmd, args: ['-m', 'carrot.app'], cwd: path.join(__dirname, '..') };
}

function startFastAPI() {
  const { cmd, args, cwd } = backendCommand();
  fastapiProcess = spawn(cmd, args, {
    cwd,
    stdio: IS_DEV ? 'inherit' : 'ignore',
    windowsHide: true,
    env: { ...process.env, CARROT_RESOURCES: process.resourcesPath || '' },
  });

  fastapiProcess.on('error', (err) => {
    console.error('Failed to start FastAPI backend:', err);
  });

  fastapiProcess.on('exit', (code) => {
    if (code !== 0 && code !== null) {
      console.error(`FastAPI backend exited with code ${code}`);
    }
  });
}

function checkHealth() {
  return backendHealth().then((health) => health !== null);
}

// The health body carries the build id, which is what makes "is this the
// version I just installed?" answerable.
function backendHealth() {
  return new Promise((resolve) => {
    const req = http.get(`${BACKEND_URL}/api/health`, { timeout: 1500 }, (res) => {
      if (res.statusCode !== 200) { res.resume(); resolve(null); return; }
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { resolve({}); }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

// A backend answering before we have started one is not ours. It is an older
// Carrot still running — and because it already holds port 8181, the copy we
// spawn cannot bind, exits, and the new UI ends up talking to the old
// backend. Every feature added since that build then 404s: the buttons are
// there, and pressing them reports "Not Found". Detecting it is the whole
// point; without this it fails silently and looks like broken features.
async function warnAboutForeignBackend() {
  const health = await backendHealth();
  if (!health) return false;
  const running = health.version || 'an unknown version';
  const choice = dialog.showMessageBoxSync({
    type: 'warning',
    title: 'Another Carrot is already running',
    message: 'Another copy of Carrot is already running on this computer.',
    detail:
      `It is serving version ${running}, and it owns the port this one needs.\n\n` +
      `This window would show the new interface but talk to the old backend, ` +
      `so anything added since ${running} would fail with "Not Found".\n\n` +
      `Quit the other Carrot — check the system tray and Task Manager for ` +
      `carrot-backend — then press Retry.`,
    buttons: ['Retry', 'Use the running version anyway', 'Quit'],
    defaultId: 0,
    cancelId: 2,
  });
  if (choice === 2) { app.quit(); return true; }
  if (choice === 0) {
    // Give the other process a moment to actually go away.
    await new Promise((r) => setTimeout(r, 1500));
    return warnAboutForeignBackend();
  }
  return false;
}

async function waitForBackend(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await checkHealth()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// ===== Windows =====
// The window background is painted by Chromium before the page loads, so it
// cannot come from the stylesheet — a light-theme user would get a dark
// flash on every launch. The renderer reports its resolved theme colour
// after each change and we replay the last one at startup.
const DEFAULT_APPEARANCE = { background: '#131419', theme: 'dark', accent: 'carrot' };
let appearance = { ...DEFAULT_APPEARANCE };

function appearancePath() {
  return path.join(app.getPath('userData'), 'appearance.json');
}

function loadAppearance() {
  try {
    const saved = JSON.parse(fs.readFileSync(appearancePath(), 'utf8'));
    if (/^#[0-9a-fA-F]{6}$/.test(String(saved.background || ''))) {
      appearance = {
        background: saved.background,
        theme: saved.theme === 'light' ? 'light' : 'dark',
        accent: /^[a-z]+$/.test(String(saved.accent || '')) ? saved.accent : 'carrot',
      };
    }
  } catch (e) { /* first run, or never set */ }
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    // Version in the title bar: it comes from the Electron shell, not the
    // web assets, so it identifies the build even if the UI fails to load.
    title: `Carrot AI ${app.getVersion()}`,
    backgroundColor: appearance.background,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  // The FastAPI backend serves the glassmorphism web UI at the root.
  mainWindow.loadURL(`${BACKEND_URL}/`);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  if (IS_DEV) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  // Open external links in the system browser, not inside the app shell.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http')) shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

function createOverlayWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { width, height } = primaryDisplay.size;

  overlayWindow = new BrowserWindow({
    width: OVERLAY_WIDTH,
    height: OVERLAY_HEIGHT,
    // Sit it in the upper third — a centred panel covers what you're reading.
    x: Math.floor(width / 2) - Math.floor(OVERLAY_WIDTH / 2),
    y: Math.floor(height * 0.22),
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    // Without this the window paints Electron's default opaque white behind
    // the panel, which is the grey slab that showed around it on Windows —
    // `transparent: true` alone does not clear the base colour.
    backgroundColor: '#00000000',
    hasShadow: false,          // the panel draws its own soft shadow
    resizable: false,
    focusable: true,
    skipTaskbar: true,
    show: false,               // never flash before the page has painted
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  overlayWindow.loadFile(path.join(__dirname, 'public', 'overlay.html'));
  overlayWindow.setAlwaysOnTop(true, 'pop-up-menu');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  overlayWindow.on('blur', () => {
    // Ignore the blur that fires while the window is still coming up,
    // otherwise the first Alt+Space appears to do nothing.
    if (overlayWindow && overlayWindow.isVisible() && !overlayWindow.webContents.isDevToolsFocused()) {
      overlayWindow.hide();
    }
  });

  overlayWindow.on('closed', () => {
    overlayWindow = null;
  });

  return overlayWindow;
}

function showOverlay() {
  if (!overlayWindow) createOverlayWindow();
  const reveal = () => {
    overlayWindow.setSize(OVERLAY_WIDTH, OVERLAY_HEIGHT);
    overlayWindow.show();
    overlayWindow.focus();
    // Push the theme before revealing: the overlay is a file:// page and
    // cannot read the app's stored preference itself.
    overlayWindow.webContents.send('appearance', appearance);
    overlayWindow.webContents.send('overlay-shown');
  };
  if (overlayWindow.webContents.isLoading()) {
    overlayWindow.webContents.once('did-finish-load', reveal);
  } else {
    reveal();
  }
}

function toggleOverlay() {
  if (overlayWindow && overlayWindow.isVisible()) overlayWindow.hide();
  else showOverlay();
}

// ===== App lifecycle =====
// Clear the renderer's HTTP cache when the installed version changes.
// Cache headers cannot retroactively fix entries a previous build already
// stored with a long expiry, so an update would otherwise keep running the
// old JavaScript until those entries aged out.
async function clearCacheOnUpgrade() {
  try {
    const stampPath = path.join(app.getPath('userData'), 'asset-cache-version');
    const current = app.getVersion();
    let previous = null;
    try { previous = fs.readFileSync(stampPath, 'utf8').trim(); } catch (e) { /* first run */ }
    if (previous !== current) {
      await session.defaultSession.clearCache();
      await session.defaultSession.clearStorageData({ storages: ['cachestorage'] });
      fs.writeFileSync(stampPath, current);
      console.log(`Cleared renderer cache for ${previous || 'first run'} -> ${current}`);
    }
  } catch (e) {
    console.error('Could not clear renderer cache:', e);
  }
}

app.whenReady().then(async () => {
  loadAppearance();
  await clearCacheOnUpgrade();
  // Check before spawning: anything already answering is not ours.
  await warnAboutForeignBackend();
  startFastAPI();
  const ready = await waitForBackend();
  if (!ready) {
    console.error('Backend did not become ready; opening window anyway.');
  }
  createMainWindow();

  // Build the overlay up front so the first press is instant.
  createOverlayWindow();

  // Alt+Space is the Windows system-menu shortcut, so the OS sometimes wins
  // the race. Fall back through alternatives and report what actually bound.
  const accelerators = ['Alt+Space', 'Super+Space', 'CommandOrControl+Shift+Space'];
  const bound = accelerators.find(a => {
    try { return globalShortcut.register(a, toggleOverlay); } catch (e) { return false; }
  });
  if (bound) {
    console.log(`Quick-ask overlay bound to ${bound}`);
  } else {
    console.error('Could not bind any quick-ask shortcut; another app holds them.');
  }

  globalShortcut.register('Alt+Q', () => {
    if (mainWindow) mainWindow.close();
  });
});

app.on('before-quit', () => {
  globalShortcut.unregisterAll();
  if (fastapiProcess) {
    fastapiProcess.kill();
    fastapiProcess = null;
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// ===== IPC =====

// The backend gates /api behind a session token it writes to disk. The renderer
// gets it injected into its HTML; the main process has to read the same file.
function sessionToken() {
  // Installed builds keep data in the per-user directory, dev checkouts keep
  // it beside the code. Try both — reading the wrong one means every API
  // call from the shell 401s.
  const candidates = [];
  if (process.platform === 'win32' && process.env.APPDATA) {
    candidates.push(path.join(process.env.APPDATA, 'Carrot', 'config', 'session.json'));
  } else if (process.platform === 'darwin') {
    candidates.push(path.join(app.getPath('home'), 'Library', 'Application Support',
                              'Carrot', 'config', 'session.json'));
  } else {
    const xdg = process.env.XDG_DATA_HOME
      || path.join(app.getPath('home'), '.local', 'share');
    candidates.push(path.join(xdg, 'carrot', 'config', 'session.json'));
  }
  candidates.push(path.join(__dirname, '..', 'carrot', 'data', 'config', 'session.json'));
  for (const tokenPath of candidates) {
    try {
      const token = JSON.parse(fs.readFileSync(tokenPath, 'utf8')).token;
      if (token) return token;
    } catch (e) { /* try the next location */ }
  }
  return '';
}

function apiHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const token = sessionToken();
  if (token) headers['X-Carrot-Token'] = token;
  return headers;
}

// The overlay can attach files and target a workspace, so this takes an
// options object. A bare string is still accepted — the quick-ask panel is
// not the only caller and a signature change should not break the others.
ipcMain.handle('send-command', async (event, command) => {
  const opts = typeof command === 'string' ? { message: command } : (command || {});
  try {
    const body = { message: opts.message || '' };
    if (Array.isArray(opts.attachments) && opts.attachments.length) {
      body.attachments = opts.attachments;
    }
    if (opts.conversation_id) body.conversation_id = opts.conversation_id;
    if (opts.workspace_id) body.workspace_id = opts.workspace_id;
    // The overlay is a full chat turn, not a lesser one: without this it fell
    // back to the saved default and had no way to ask for more.
    if (opts.search_mode) body.search_mode = opts.search_mode;
    const response = await fetch(`${BACKEND_URL}/api/chat`, {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify(body),
    });
    return await response.json();
  } catch (e) {
    return { error: e.message };
  }
});

// Files for the quick-ask panel. Read in the main process because the
// renderer has no Node access, and returned as base64 because that is the
// shape /api/chat wants.
const OVERLAY_MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024;

function readAsAttachment(filePath) {
  const stat = fs.statSync(filePath);
  if (stat.size > OVERLAY_MAX_ATTACHMENT_BYTES) {
    return { name: path.basename(filePath), error: 'larger than 12 MB' };
  }
  const ext = path.extname(filePath).toLowerCase();
  const mimes = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/markdown',
    '.csv': 'text/csv', '.json': 'application/json', '.py': 'text/x-python',
    '.js': 'text/javascript', '.ts': 'text/typescript', '.html': 'text/html',
  };
  return {
    name: path.basename(filePath),
    mime: mimes[ext] || 'application/octet-stream',
    data: fs.readFileSync(filePath).toString('base64'),
    size: stat.size,
  };
}

ipcMain.handle('pick-attachments', async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const result = await dialog.showOpenDialog(parent, {
    title: 'Attach to your question',
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: 'Everything Carrot reads', extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'pdf', 'txt', 'md', 'csv', 'json', 'py', 'js', 'ts', 'html'] },
      { name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'] },
      { name: 'Documents', extensions: ['pdf', 'txt', 'md', 'csv', 'json'] },
      { name: 'All files', extensions: ['*'] },
    ],
  });
  if (result.canceled) return { files: [] };
  const files = [];
  for (const filePath of result.filePaths) {
    try {
      files.push(readAsAttachment(filePath));
    } catch (e) {
      files.push({ name: path.basename(filePath), error: e.message });
    }
  }
  return { files };
});

// Dropping a file onto the panel gives the renderer a path, not contents.
ipcMain.handle('read-attachment', async (event, filePath) => {
  try {
    return readAsAttachment(String(filePath));
  } catch (e) {
    return { name: path.basename(String(filePath)), error: e.message };
  }
});

ipcMain.handle('list-workspaces', async () => {
  try {
    const response = await fetch(`${BACKEND_URL}/api/workspaces`, { headers: apiHeaders() });
    return await response.json();
  } catch (e) {
    return { error: e.message, workspaces: [] };
  }
});

ipcMain.handle('get-status', async () => {
  try {
    const response = await fetch(`${BACKEND_URL}/api/status`, { headers: apiHeaders() });
    return await response.json();
  } catch (e) {
    return { error: e.message };
  }
});

// The quick-ask overlay is a bare floating panel: it grows to fit its reply
// and dismisses itself on Escape.
ipcMain.handle('resize-overlay', async (event, height) => {
  if (!overlayWindow) return { ok: false };
  const [width] = overlayWindow.getSize();
  overlayWindow.setSize(width, Math.max(OVERLAY_HEIGHT,
    Math.min(Number(height) || OVERLAY_HEIGHT, OVERLAY_MAX_HEIGHT)));
  return { ok: true };
});

ipcMain.handle('hide-overlay', async () => {
  if (overlayWindow) overlayWindow.hide();
  return { ok: true };
});

// Electron disables window.prompt(), so anything that used it to ask for a
// path silently did nothing. A native folder picker is the right control
// for choosing a directory anyway.
ipcMain.handle('pick-directory', async (event, { title, defaultPath } = {}) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const result = await dialog.showOpenDialog(parent, {
    title: title || 'Choose a folder',
    defaultPath: defaultPath || undefined,
    properties: ['openDirectory', 'createDirectory'],
  });
  if (result.canceled || !result.filePaths.length) return { path: '' };
  return { path: result.filePaths[0] };
});

// The renderer reports its resolved appearance after every theme change.
// Two consumers: the window background (so the next launch opens on the
// right colour) and the quick-ask overlay, which is a file:// page and so
// cannot read the app's own localStorage to find out.
ipcMain.handle('set-appearance', async (event, { background, theme, accent } = {}) => {
  if (!/^#[0-9a-fA-F]{6}$/.test(String(background || ''))) return { ok: false };
  appearance = {
    background,
    theme: theme === 'light' ? 'light' : 'dark',
    accent: /^[a-z]+$/.test(String(accent || '')) ? accent : 'carrot',
  };
  try {
    if (mainWindow) mainWindow.setBackgroundColor(appearance.background);
    if (overlayWindow) overlayWindow.webContents.send('appearance', appearance);
    fs.writeFileSync(appearancePath(), JSON.stringify(appearance));
    return { ok: true };
  } catch (e) {
    return { ok: false };
  }
});

// Only http(s) gets through. A renderer that asked to "open" a file:// or a
// custom scheme would be asking the OS to launch something, which is not what
// this channel is for.
ipcMain.handle('open-external', async (event, url) => {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return { opened: false };
  await shell.openExternal(url);
  return { opened: true };
});

ipcMain.handle('notify', async (event, { title, body }) => {
  if (!Notification.isSupported()) return { shown: false };
  const notification = new Notification({ title: title || 'Carrot', body: body || '' });
  // Clicking a toast should bring the user to the thing it is about.
  notification.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  notification.show();
  return { shown: true };
});
