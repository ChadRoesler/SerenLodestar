'use strict';
/* global getToken, escapeHtml, showTab — provided by the SerenMeninges shell.
 *
 * Carved from the pre-baseplate RuntimeHost (now Lodestar) monolith. The shell now owns the
 * page chrome, the header, and the 🔑 bearer-token modal. Two deliberate
 * changes from the monolith:
 *   • its own api() became hostApi() — same-origin fetch (the viewer is served
 *     by Lodestar itself) with the bearer pulled from the shell's getToken().
 *     Kept the null-return + banner() + EXPECTED_STATUS(404/503) contract so
 *     every caller is unchanged except the name.
 *   • its own escapeHtml() is gone — we use the shell's global.
 * The localStorage URL/token config is retired: same-origin kills the URL, and
 * the token lives in the shell modal now.
 */

// ----------------------------------------------------------------------
// CONFIG
// ----------------------------------------------------------------------
const POLL_INTERVAL = 5000;   // ms
const TEMP_HOT_C = 75;        // pulse temperature reading at this threshold
const TEMP_DANGER_C = 85;     // pulse whole node header at this threshold

// Which nodes THIS browser keeps off its grid (settings, "On this screen").
// A per-screen choice, so it lives in localStorage and nowhere else: the
// rack's glance screen hides the desk beside it, the desk sees everything.
// A hidden node still counts for the LED and the gaps strip, and comes back
// on its own while it needs someone (offline, or a service down).
const HIDDEN_NODES_KEY = 'lodestar.hiddenNodes';
const GLANCE_MAX = 5;   // cards across the glance row at 1424px before it pages

function loadHiddenNodes() {
    try {
        const raw = localStorage.getItem(HIDDEN_NODES_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(arr) ? arr.filter(n => typeof n === 'string') : []);
    } catch (_) { return new Set(); }
}

function saveHiddenNodes(set) {
    try { localStorage.setItem(HIDDEN_NODES_KEY, JSON.stringify([...set])); } catch (_) { /* private window, blocked storage */ }
}

// The rest of "On this screen" (20 Oct 2026: "offer more control: auto
// expand instances, auto expand child processes, limit shown before scroll").
// Same home as the hidden nodes: this browser, nowhere else.
const SCREEN_KEY = 'lodestar.screen';
const SCREEN_DEFAULTS = { expandInstances: false, expandChildren: false, rowsBeforeScroll: 0 };  // 0 = the card grows

function loadScreen() {
    try {
        const raw = localStorage.getItem(SCREEN_KEY);
        const got = raw ? JSON.parse(raw) : {};
        return { ...SCREEN_DEFAULTS, ...(got && typeof got === 'object' ? got : {}) };
    } catch (_) { return { ...SCREEN_DEFAULTS }; }
}

function saveScreen(prefs) {
    try { localStorage.setItem(SCREEN_KEY, JSON.stringify(prefs)); } catch (_) { /* private window, blocked storage */ }
}

function nodeNeedsYou(node) {
    if (!node.online) return true;
    return Object.values(node.services || {}).some(s => symbolFor(s?.status)[1] === 'down');
}

// Hidden means hidden. Until 20 Oct 2026 a hidden node came back on its
// own while it was offline or carried something down, so the glance never
// lied; then the desktop went down for a day and it could not be taken off
// the grid ("unable to deactivate an offline node from the view"). The LED
// and the gaps strip still count every node, so the truth is one glance up.
function nodeShownHere(node, hidden) {
    return !hidden.has(node.name);
}

// ----------------------------------------------------------------------
// API CLIENT — same-origin fetch wrapper, bearer from the shell's getToken().
// Returns parsed JSON, or null on failure (and banner-displays the reason).
// EXPECTED_STATUS codes are normal operation (a node down, a service not
// installed) and are handled inline by the caller, not the global banner.
// ----------------------------------------------------------------------
const EXPECTED_STATUS = new Set([404, 503]);

async function hostApi(path, opts = {}) {
    const headers = { 'Accept': 'application/json', ...(opts.headers || {}) };
    const token = (typeof getToken === 'function' ? getToken() : '') || '';
    if (token) headers['Authorization'] = 'Bearer ' + token;

    const fetchOpts = { ...opts, headers };
    if (opts.json !== undefined) {
        headers['Content-Type'] = 'application/json';
        fetchOpts.body = JSON.stringify(opts.json);
        delete fetchOpts.json;
    }

    try {
        const resp = await fetch(path, fetchOpts);
        if (!resp.ok) {
            if (!EXPECTED_STATUS.has(resp.status)) {
                banner(`API ${path} → HTTP ${resp.status}`);
            }
            return null;
        }
        banner(null);
        return await resp.json();
    } catch (e) {
        banner(`Cannot reach Lodestar — ${e.message}`);
        return null;
    }
}

function banner(msg) {
    const el = document.getElementById('banner');
    if (!msg) {
        el.classList.remove('visible');
        el.textContent = '';
        return;
    }
    el.textContent = msg;
    el.classList.add('visible');
}

// ----------------------------------------------------------------------
// MODAL-LOCAL FLASH — per-node action results display inside the modal.
//   setModalFlash(msg, type)  - show; type ∈ {ok, err, info}
//   setModalFlash(null)       - dismiss
// Success/info auto-dismiss after 4s; errors persist until dismissed.
// ----------------------------------------------------------------------
let _modalFlashTimer = null;
function setModalFlash(msg, type) {
    const el = document.getElementById('modal-flash');
    const msgEl = document.getElementById('modal-flash-msg');
    if (!el || !msgEl) return;

    if (_modalFlashTimer !== null) {
        clearTimeout(_modalFlashTimer);
        _modalFlashTimer = null;
    }

    if (!msg) {
        el.classList.remove('visible', 'flash-ok', 'flash-err', 'flash-info');
        msgEl.textContent = '';
        return;
    }

    msgEl.textContent = msg;
    el.classList.remove('flash-ok', 'flash-err', 'flash-info');
    el.classList.add('visible', `flash-${type || 'info'}`);

    if (type !== 'err') {
        _modalFlashTimer = setTimeout(() => setModalFlash(null), 4000);
    }
}

// ----------------------------------------------------------------------
// STATUS SYMBOLS
//   ●  running + healthy      ◐  running, port not responding
//   ◯  not running            ◌  idle: off on purpose, Lodestar starts it when asked
// An orchestrated service that is off is resting, not down (the Observatory
// says `idle`). 20 Oct 2026: "services that are orchestrated shouldn't
// show as degraded on the top bar ... 12/28 services idle (it's on when we
// need it on)". ◌ is Geometric Shapes too, so the kiosk font has it.
// ----------------------------------------------------------------------
function symbolFor(status) {
    if (!status) return ['◯', 'down'];
    if (!status.running && status.library_mode) return ['●', 'ok'];
    if (!status.running && (status.idle || status.orchestrated)) return ['◌', 'idle'];
    if (!status.running) return ['◯', 'down'];

    if (status.library_mode) return ['●', 'ok'];
    const ph = status.port_health;
    if (ph === null || ph === undefined) return ['●', 'ok']; // systemd-managed
    if (ph.ok) return ['●', 'ok'];
    return ['◐', 'warn'];
}

// ----------------------------------------------------------------------
// POLLING — managed handle so the modal can swap rates when open
// ----------------------------------------------------------------------
const MODAL_POLL_INTERVAL = 2000;
let pollHandle = null;

function startPolling(intervalMs) {
    if (pollHandle !== null) clearInterval(pollHandle);
    pollHandle = setInterval(refresh, intervalMs);
}

// ----------------------------------------------------------------------
// MODAL — node drill-down + service drill-down state machine
// ----------------------------------------------------------------------
const modal = {
    open: false,
    nodeName: null,
    scrollIndex: 0,
    rowsVisible: 0,
    state: 'idle',   // idle | confirming | armed | countdown | update-confirming | svc-idle | svc-confirm-stop
    countdownSecs: 60,
    countdownHandle: null,
    svcName: null,
    logsText: '',
};

function initModal() {
    document.getElementById('modal-close').addEventListener('click', closeNodeModal);
    const flashClose = document.getElementById('modal-flash-close');
    if (flashClose) {
        flashClose.addEventListener('click', () => setModalFlash(null));
    }
    document.getElementById('modal-backdrop').addEventListener('click', (e) => {
        if (e.target.id !== 'modal-backdrop') return;
        if (modal.state !== 'idle' && modal.state !== 'svc-idle') return;
        closeNodeModal();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (!modal.open) return;
        if (modal.state !== 'idle' && modal.state !== 'svc-idle') return;
        closeNodeModal();
    });
}

function openNodeModal(nodeName) {
    modal.open = true;
    modal.nodeName = nodeName;
    modal.scrollIndex = 0;
    modal.state = 'idle';
    document.getElementById('modal-backdrop').classList.add('visible');
    renderModalContent();
    startPolling(MODAL_POLL_INTERVAL);
    refresh();
}

function closeNodeModal() {
    modal.open = false;
    modal.nodeName = null;
    modal.state = 'idle';
    if (modal.countdownHandle !== null) {
        clearInterval(modal.countdownHandle);
        modal.countdownHandle = null;
    }
    setModalFlash(null);
    document.getElementById('modal-backdrop').classList.remove('visible');
    startPolling(POLL_INTERVAL);
}

function refreshModal() {
    if (!modal.open) return;
    if (modal.state === 'idle' || modal.state === 'svc-idle') {
        if (modal.state === 'svc-idle') {
            refreshServiceLogs();
        }
        renderModalContent();
    }
}

async function refreshServiceLogs() {
    if (!modal.svcName || !modal.nodeName) return;
    const url = `/api/v1/node/${encodeURIComponent(modal.nodeName)}/service/${encodeURIComponent(modal.svcName)}/logs?lines=50`;
    const resp = await hostApi(url);
    if (resp?.logs?.lines) {
        modal.logsText = resp.logs.lines.join('\n');
    } else if (resp?.logs?.note) {
        modal.logsText = '(' + resp.logs.note + ')';
    } else if (!resp) {
        modal.logsText = '(could not fetch logs)';
    }
}

function findNode(nodeName) {
    return (window._lastStatus?.nodes || []).find(n => n.name === nodeName) || null;
}

function deriveState(svcStatus) {
    if (!svcStatus) return ['-', 'down'];
    if (svcStatus.library_mode) return ['Library', 'library'];
    if (!svcStatus.running && (svcStatus.idle || svcStatus.orchestrated)) return ['Idle', 'idle'];
    if (!svcStatus.running) return ['Down', 'down'];
    const ph = svcStatus.port_health;
    if (ph === null || ph === undefined) return ['Running', 'running'];
    if (ph.ok) return ['Running', 'running'];
    return ['Loading', 'loading'];
}

// Service-type badges — one-glance "what kind of thing is this?"
function serviceBadge(svcStatus) {
    if (!svcStatus) return { emoji: '', title: '', cls: '' };
    const t = svcStatus.service_type;
    switch (t) {
        case 'pid_file':       return { emoji: '📄', title: 'PID-file daemon', cls: 'badge-pid' };
        case 'library':        return { emoji: '📚', title: 'Library (no daemon)', cls: 'badge-lib' };
        case 'systemd':        return { emoji: '🎛️', title: 'systemd unit', cls: 'badge-systemd' };
        case 'docker_compose': return { emoji: '📦', title: 'Docker Compose service', cls: 'badge-docker' };
        case 'container':      return { emoji: '📦', title: 'a container it started', cls: 'badge-docker' };
        default:               return { emoji: '', title: '', cls: '' };
    }
}

function renderModalContent() {
    const node = findNode(modal.nodeName);
    if (!node) {
        closeNodeModal();
        return;
    }
    renderModalHeader(node);
    renderModalBody(node);
}

function renderModalHeader(node) {
    const fields = document.getElementById('modal-header-fields');
    const ip = node.agent_node?.manifest?.ip_addresses?.[0] || '-';
    const hostname = node.agent_node?.manifest?.hostname || node.name;
    const displayName = node.nickname && node.nickname.trim().length > 0
        ? `${node.nickname} (${node.name})`
        : node.name;
    const maxTemp = node.thermal?.max_temp_c;
    const tempStr = maxTemp != null ? `${maxTemp.toFixed(1)} °C` : '-';
    const uptimeSec = node.agent_node?.runtime?.uptime_seconds;
    const uptimeStr = fmtUptime(uptimeSec);

    const inSvcDrill = modal.state === 'svc-idle' || modal.state === 'svc-confirm-stop';

    if (inSvcDrill && modal.svcName) {
        fields.innerHTML = `
            <div class="modal-header-field">
                <div class="modal-header-label">Node</div>
                <div class="modal-header-value">${escapeHtml(displayName)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">Service</div>
                <div class="modal-header-value">${escapeHtml(modal.svcName)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">IP</div>
                <div class="modal-header-value">${escapeHtml(ip)}</div>
            </div>
        `;
    } else {
        fields.innerHTML = `
            <div class="modal-header-field">
                <div class="modal-header-label">Node</div>
                <div class="modal-header-value">${escapeHtml(displayName)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">Host</div>
                <div class="modal-header-value">${escapeHtml(hostname)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">IP</div>
                <div class="modal-header-value">${escapeHtml(ip)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">Max temp</div>
                <div class="modal-header-value">${escapeHtml(tempStr)}</div>
            </div>
            <div class="modal-header-field">
                <div class="modal-header-label">Uptime</div>
                <div class="modal-header-value">${escapeHtml(uptimeStr)}</div>
            </div>
        `;
    }

    const closeBtn = document.getElementById('modal-close');
    if (inSvcDrill) {
        closeBtn.textContent = '←';
        closeBtn.setAttribute('aria-label', 'Back to node view');
        closeBtn.onclick = () => {
            modal.state = 'idle';
            modal.svcName = null;
            modal.logsText = '';
            renderModalContent();
        };
    } else {
        closeBtn.textContent = '✕';
        closeBtn.setAttribute('aria-label', 'Close');
        closeBtn.onclick = closeNodeModal;
    }
}

function fmtUptime(secs) {
    if (secs == null) return '-';
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

// Inline SVG power symbol (avoids the U+23FB tofu-box on some Jetson fonts).
function powerSvg() {
    return `<svg viewBox="0 0 24 24" width="1em" height="1em"
                xmlns="http://www.w3.org/2000/svg"
                fill="none" stroke="currentColor"
                stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
                aria-hidden="true">
                <path d="M7.5 5.5 A 7.5 7.5 0 1 0 16.5 5.5" />
                <line x1="12" y1="2.5" x2="12" y2="12" />
            </svg>`;
}

function renderModalBody(node) {
    const body = document.getElementById('modal-body');

    if (modal.state === 'confirming' || modal.state === 'armed') {
        renderConfirmBody(body, node);
        return;
    }
    if (modal.state === 'update-confirming') {
        renderUpdateConfirmBody(body, node);
        return;
    }
    if (modal.state === 'countdown') {
        renderCountdownBody(body, node);
        return;
    }
    if (modal.state === 'svc-idle' || modal.state === 'svc-confirm-stop') {
        renderServiceDrillBody(body, node);
        return;
    }

    // Every service, a child under its owner (serviceTree): in the modal the
    // family is always open, this is the detail view.
    const rows = flattenTree(serviceTree(node, { hideObservatory: false }), () => true);
    const rowHeight = window.matchMedia('(max-height: 400px)').matches ? 56 : 64;
    const bodyHeight = body.getBoundingClientRect().height || 500;
    const headerHeight = 33;
    modal.rowsVisible = Math.max(1, Math.floor((bodyHeight - headerHeight) / rowHeight));
    const maxScroll = Math.max(0, rows.length - modal.rowsVisible);
    modal.scrollIndex = Math.min(modal.scrollIndex, maxScroll);

    const visible = rows.slice(modal.scrollIndex, modal.scrollIndex + modal.rowsVisible);

    let rowsHtml = '';
    for (const r of visible) {
        const svcStatus = r.status;
        const mem = svcStatus?.memory_mb;
        const cpu = svcStatus?.cpu_percent;
        const pid = svcStatus?.pid;
        const [stateLabel, stateCls] = deriveState(svcStatus);
        const badge = serviceBadge(r.container ? { service_type: 'container' } : svcStatus);
        const family = r.children.length ? ` <span class="svc-count" title="${r.children.length} of its own">${r.children.length}</span>` : '';

        rowsHtml += `
            <div class="modal-svc-row depth-${r.depth}${r.container ? ' container' : ''}" data-svc="${r.container ? '' : escapeHtml(r.name)}">
                <div class="name">
                    ${badge.emoji ? `<span class="svc-badge ${badge.cls}" title="${escapeHtml(badge.title)}">${badge.emoji}</span>` : ''}
                    ${escapeHtml(r.name)}${family}
                </div>
                <div class="num">${fmtBytes(mem)}</div>
                <div class="num">${cpu != null ? cpu.toFixed(1) + ' %' : '-'}</div>
                <div class="num">${pid != null ? pid : '-'}</div>
                <div class="state ${stateCls}">${stateLabel}</div>
            </div>
        `;
    }
    const svcNames = rows;

    const canUp = modal.scrollIndex > 0;
    const canDown = modal.scrollIndex < maxScroll;

    let thumbHtml = '';
    if (svcNames.length > 0) {
        const visibleFrac = Math.min(1, modal.rowsVisible / svcNames.length);
        const topFrac = svcNames.length > modal.rowsVisible
            ? modal.scrollIndex / svcNames.length
            : 0;
        thumbHtml = `<div class="modal-scroll-thumb-bar"
            style="top: ${(topFrac * 100).toFixed(1)}%; height: ${(visibleFrac * 100).toFixed(1)}%;"></div>`;
    }

    body.innerHTML = `
        <div class="modal-svc-list">
            <div class="modal-svc-header">
                <div>Service</div>
                <div>Memory</div>
                <div>CPU</div>
                <div>PID</div>
                <div>State</div>
            </div>
            <div class="modal-svc-rows" id="modal-svc-rows">${rowsHtml}</div>
        </div>
        <div class="modal-scroll">
            <button class="modal-scroll-btn" id="modal-scroll-up" ${canUp ? '' : 'disabled'}>▲</button>
            <div class="modal-scroll-thumb">${thumbHtml}</div>
            <button class="modal-scroll-btn" id="modal-scroll-down" ${canDown ? '' : 'disabled'}>▼</button>
        </div>
        <div class="modal-actions-col">
            <div class="modal-update-node" id="modal-update-node" role="button" tabindex="0">
                <div class="modal-update-node-icon">↑</div>
                <div class="modal-update-node-label">Update Node</div>
            </div>
            <div class="modal-reboot" id="modal-reboot" role="button" tabindex="0">
                <div class="modal-reboot-icon">${powerSvg()}</div>
                <div class="modal-reboot-label">Reboot Node</div>
            </div>
        </div>
    `;

    document.getElementById('modal-scroll-up').addEventListener('click', () => {
        modal.scrollIndex = Math.max(0, modal.scrollIndex - 1);
        renderModalContent();
    });
    document.getElementById('modal-scroll-down').addEventListener('click', () => {
        modal.scrollIndex = Math.min(maxScroll, modal.scrollIndex + 1);
        renderModalContent();
    });

    document.getElementById('modal-update-node').addEventListener('click', () => {
        modal.state = 'update-confirming';
        renderModalContent();
    });

    document.getElementById('modal-reboot').addEventListener('click', () => {
        modal.state = 'confirming';
        renderModalContent();
    });

    for (const row of document.querySelectorAll('.modal-svc-row')) {
        row.addEventListener('click', () => {
            const svc = row.dataset.svc;
            if (svc) openServiceDrill(svc);   // a container it started has no manifest to drill into
        });
    }
}

function renderConfirmBody(body, node) {
    const armed = modal.state === 'armed';
    body.innerHTML = `
        <div class="modal-confirm">
            <div class="modal-confirm-title">
                Reboot <strong>${escapeHtml(node.name)}</strong>?
            </div>
            <div class="modal-confirm-buttons">
                <button class="modal-confirm-btn yes" id="modal-confirm-yes" ${armed ? 'disabled' : ''}>Yes</button>
                <button class="modal-confirm-btn" id="modal-confirm-no" ${armed ? 'disabled' : ''}>No</button>
            </div>
        </div>
        <div class="modal-reboot ${armed ? 'armed-ready' : 'armed-pending'}" id="modal-reboot-armed" role="button" tabindex="0">
            <div class="modal-reboot-icon">${powerSvg()}</div>
            <div class="modal-reboot-label">
                ${armed ? 'Reboot Now' : 'Confirm First'}
            </div>
        </div>
    `;

    if (!armed) {
        document.getElementById('modal-confirm-no').addEventListener('click', () => {
            modal.state = 'idle';
            renderModalContent();
        });
        document.getElementById('modal-confirm-yes').addEventListener('click', () => {
            modal.state = 'armed';
            renderModalContent();
        });
    } else {
        document.getElementById('modal-reboot-armed').addEventListener('click', () => {
            fireReboot(node.name);
        });
    }
}

function renderUpdateConfirmBody(body, node) {
    body.innerHTML = `
        <div class="modal-confirm">
            <div class="modal-confirm-title">
                Upgrade <strong>${escapeHtml(node.name)}</strong>?
            </div>
            <div class="modal-confirm-subtitle">
                Every seren service on it that is behind the index: a backup to the stash,
                then pip, an import check, a restart and health - its Observatory last.
                A failure rolls that one back. Watch it on the Upgrades tab.
            </div>
            <div class="modal-confirm-buttons">
                <button class="modal-confirm-btn yes" id="modal-update-confirm-yes">Yes</button>
                <button class="modal-confirm-btn" id="modal-update-confirm-no">No</button>
            </div>
        </div>
        <div class="modal-update-node armed-ready" id="modal-update-armed" role="button" tabindex="0">
            <div class="modal-update-node-icon">↑</div>
            <div class="modal-update-node-label">Upgrade Node</div>
        </div>
    `;

    document.getElementById('modal-update-confirm-no').addEventListener('click', () => {
        modal.state = 'idle';
        renderModalContent();
    });
    for (const id of ['modal-update-confirm-yes', 'modal-update-armed']) {
        document.getElementById(id).addEventListener('click', () => {
            modal.state = 'idle';
            renderModalContent();
            upgradeNode(node.name);
        });
    }
}

function renderCountdownBody(body, node) {
    body.innerHTML = `
        <div class="modal-countdown">
            <div class="modal-countdown-title">Reboot scheduled — <strong>${escapeHtml(node.name)}</strong></div>
            <div class="modal-countdown-secs" id="modal-countdown-secs">${modal.countdownSecs}</div>
            <button class="modal-countdown-cancel" id="modal-countdown-cancel">Cancel Reboot</button>
        </div>
    `;
    document.getElementById('modal-countdown-cancel').addEventListener('click', () => {
        cancelReboot(node.name);
    });
}

async function fireReboot(nodeName) {
    const resp = await hostApi(`/api/v1/system/reboot/${encodeURIComponent(nodeName)}`, {
        method: 'POST',
        json: { delay_minutes: 1 },
    });
    if (!resp || resp.scheduled === false) {
        banner(`Reboot failed: ${resp?.error || 'no response'}${resp?.hint ? ' (' + resp.hint + ')' : ''}`);
        modal.state = 'idle';
        renderModalContent();
        return;
    }
    modal.state = 'countdown';
    modal.countdownSecs = 60;
    renderModalContent();
    if (modal.countdownHandle !== null) clearInterval(modal.countdownHandle);
    modal.countdownHandle = setInterval(() => {
        modal.countdownSecs--;
        const el = document.getElementById('modal-countdown-secs');
        if (el) el.textContent = Math.max(0, modal.countdownSecs);
        if (modal.countdownSecs <= 0) {
            clearInterval(modal.countdownHandle);
            modal.countdownHandle = null;
            closeNodeModal();
        }
    }, 1000);
}

async function cancelReboot(nodeName) {
    const resp = await hostApi(`/api/v1/system/reboot/${encodeURIComponent(nodeName)}/cancel`, {
        method: 'POST',
    });
    if (!resp || resp.cancelled === false) {
        banner(`Cancel failed: ${resp?.error || 'no response'}`);
        return;
    }
    if (modal.countdownHandle !== null) {
        clearInterval(modal.countdownHandle);
        modal.countdownHandle = null;
    }
    modal.state = 'idle';
    renderModalContent();
}

// ----------------------------------------------------------------------
// Service drill-down
// ----------------------------------------------------------------------
async function openServiceDrill(svcName) {
    modal.state = 'svc-idle';
    modal.svcName = svcName;
    modal.logsText = '(loading…)';
    renderModalContent();
    await refreshServiceLogs();
    renderModalContent();
}

function renderServiceDrillBody(body, node) {
    const svcStatus = node.services_detail?.[modal.svcName]?.status;
    const svcManifest = node.services_detail?.[modal.svcName]?.manifest;

    const running = svcStatus?.running === true;
    const libMode = svcStatus?.library_mode === true;
    const [stateLabel, stateCls] = deriveState(svcStatus);

    const memMb = svcStatus?.memory_mb;
    const cpuPct = svcStatus?.cpu_percent;
    const pid = svcStatus?.pid;
    const uptimeSec = svcStatus?.uptime_seconds;
    const port = svcManifest?.port;

    const canStart = !libMode && !running;
    const canStop = !libMode && running;
    const canRestart = !libMode && running;

    const confirmingStop = modal.state === 'svc-confirm-stop';

    body.innerHTML = `
        <div class="svc-drill-main">
            <div class="svc-drill-stats">
                <div class="svc-stat-cell"><div class="svc-stat-label">State</div><div class="svc-stat-value state ${stateCls}">${stateLabel}</div></div>
                <div class="svc-stat-cell"><div class="svc-stat-label">Memory</div><div class="svc-stat-value">${fmtBytes(memMb)}</div></div>
                <div class="svc-stat-cell"><div class="svc-stat-label">CPU</div><div class="svc-stat-value">${cpuPct != null ? cpuPct.toFixed(1) + ' %' : '-'}</div></div>
                <div class="svc-stat-cell"><div class="svc-stat-label">PID</div><div class="svc-stat-value">${pid != null ? pid : '-'}</div></div>
                <div class="svc-stat-cell"><div class="svc-stat-label">Uptime</div><div class="svc-stat-value">${fmtUptime(uptimeSec)}</div></div>
                <div class="svc-stat-cell"><div class="svc-stat-label">Port</div><div class="svc-stat-value">${port ?? '-'}</div></div>
            </div>
            <div class="svc-drill-logs-wrap">
                <div class="svc-drill-logs-header">
                    Logs · last 50 lines
                    ${svcManifest?.log_path ? `<span class="svc-drill-logs-path">${escapeHtml(svcManifest.log_path)}</span>` : ''}
                </div>
                <pre class="svc-drill-logs" id="svc-drill-logs">${escapeHtml(modal.logsText || '(no logs)')}</pre>
            </div>
        </div>
        <div class="svc-drill-actions">
            ${confirmingStop ? renderStopConfirmInline() : renderActionButtons(canStart, canStop, canRestart, libMode)}
        </div>
    `;

    const logsEl = document.getElementById('svc-drill-logs');
    if (logsEl) logsEl.scrollTop = logsEl.scrollHeight;

    if (!confirmingStop) {
        if (canStart) document.getElementById('svc-action-start')?.addEventListener('click', () => fireServiceAction('start'));
        if (canStop) document.getElementById('svc-action-stop')?.addEventListener('click', () => { modal.state = 'svc-confirm-stop'; renderModalContent(); });
        if (canRestart) document.getElementById('svc-action-restart')?.addEventListener('click', () => fireServiceAction('restart'));
    } else {
        document.getElementById('svc-stop-yes')?.addEventListener('click', () => fireServiceAction('stop'));
        document.getElementById('svc-stop-no')?.addEventListener('click', () => { modal.state = 'svc-idle'; renderModalContent(); });
    }
}

function renderActionButtons(canStart, canStop, canRestart, libMode) {
    if (libMode) {
        return `<div class="svc-drill-libnote">Library-mode service. No daemon to control.</div>`;
    }
    return `
        <button class="svc-action-btn ${canStart ? '' : 'disabled'}" id="svc-action-start" ${canStart ? '' : 'disabled'}>▶ Start</button>
        <button class="svc-action-btn danger ${canStop ? '' : 'disabled'}" id="svc-action-stop" ${canStop ? '' : 'disabled'}>■ Stop</button>
        <button class="svc-action-btn ${canRestart ? '' : 'disabled'}" id="svc-action-restart" ${canRestart ? '' : 'disabled'}>↻ Restart</button>
    `;
}

function renderStopConfirmInline() {
    return `
        <div class="svc-stop-confirm">
            <div class="svc-stop-confirm-title">Stop <strong>${escapeHtml(modal.svcName)}</strong>?</div>
            <button class="svc-action-btn danger" id="svc-stop-yes">Yes, stop</button>
            <button class="svc-action-btn" id="svc-stop-no">No</button>
        </div>
    `;
}

async function fireServiceAction(action) {
    const url = `/api/v1/node/${encodeURIComponent(modal.nodeName)}/service/${encodeURIComponent(modal.svcName)}/${action}`;
    const resp = await hostApi(url, { method: 'POST' });
    if (!resp || resp.result?.ok === false) {
        banner(`Action '${action}' failed: ${resp?.result?.error || 'no response'}`);
    }
    modal.state = 'svc-idle';
    await refresh();
    await refreshServiceLogs();
    renderModalContent();
}

function fmtBytes(mb) {
    if (mb == null) return '-';
    if (mb >= 1024) return (mb / 1024).toFixed(1) + ' Gb';
    return mb + ' Mb';
}

function fmtTime(iso) {
    if (!iso) return '-';
    const t = new Date(iso);
    if (isNaN(t)) return '-';
    const secs = Math.floor((Date.now() - t) / 1000);
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    return Math.floor(secs / 3600) + 'h ago';
}

// ----------------------------------------------------------------------
// Instance groups on a node card (design note, 16 Oct 2026): "seren-loci-wren",
// "seren-memory-wren", "SerenObservatory-wren" ... is one install's worth
// of services wearing the instance name ten times. Group them under the
// instance, collapsed by default, and show each as the app it is: loci,
// memory, observatory. Anything without an instance suffix (llama, kokoro,
// searxng, a bare "seren-theatre") stays where it was.
//
// The family (20 Oct 2026): a service can belong to another (`parent` on its
// status, from its manifest) or own things that have no manifest of their own
// (`children` on its status: the containers Probe starts). The ask: "probe
// should own its sub processes ... chevroned so that you can click probe and
// see its owned processes and their memory usage ... this way we don't lie
// about what's there and what it has". The owner's row folds the family and
// carries its memory; the fold is remembered across the poll's re-render.
// ----------------------------------------------------------------------
const INSTANCE_RE = /^(?:seren-|Seren)(.+?)-([A-Za-z0-9]+)$/;
// hideObservatory folds the node's own Observatory out of a listing. It was
// the card's default for an hour on 20 Oct 2026 ("Observatory, we know it's a
// node") until it was seen gone and the point turned out to be the Observatory
// viewer's hostname pill, not this. Kept as a switch, off everywhere.
const OBSERVATORY_RE = /observatory/i;
// Folds whose state differs from this screen's default ("node/instance" for
// an instance group, "node/svc:name" for an owner), so the poll's re-render
// keeps what was opened or closed, and the settings change the rest at once.
const toggledFolds = new Set();

function instanceOf(svcName) {
    const m = INSTANCE_RE.exec(svcName);
    if (!m) return null;
    return { app: m[1].toLowerCase(), instance: m[2] };
}

// → [{instance, label, services: [{name, app}]}] in first-seen order, and the
//   names that stay flat, in their original order.
function groupServices(names) {
    const groups = new Map();
    const flat = [];
    for (const name of names) {
        const hit = instanceOf(name);
        if (!hit) { flat.push(name); continue; }
        if (!groups.has(hit.instance)) {
            groups.set(hit.instance, { instance: hit.instance,
                                       label: hit.instance.charAt(0).toUpperCase() + hit.instance.slice(1),
                                       services: [] });
        }
        groups.get(hit.instance).services.push({ name, app: hit.app });
    }
    return { groups: [...groups.values()], flat };
}

// Who a service belongs to, when the owner is on the node: `parent` from the
// status (the manifest's), else the one shape we always rolled up by hand,
// searxng-redis under searxng.
function parentOf(node, name) {
    const names = node.installed_services || [];
    const st = node.services?.[name]?.status;
    const declared = st?.parent || node.services_detail?.[name]?.manifest?.parent;
    if (declared && names.includes(declared) && declared !== name) return declared;
    const lower = name.toLowerCase();
    if (lower !== 'searxng' && lower.includes('searxng')) {
        const owner = names.find(n => n.toLowerCase() === 'searxng');
        if (owner) return owner;
    }
    return null;
}

// The node's services as a tree: {groups: [{instance, label, rows}], flat: rows}
// where a row is {name, label, status, children: [row], container}. A child
// with a manifest is a row like any other; a container the owner started is
// a row made from what the Observatory found this poll.
function serviceTree(node, { hideObservatory } = {}) {
    const names = node.installed_services || [];
    const kids = {};
    for (const n of names) {
        const p = parentOf(node, n);
        if (p) (kids[p] = kids[p] || []).push(n);
    }
    const row = (name, label) => {
        const status = node.services?.[name]?.status;
        const own = (kids[name] || []).map(k => row(k, instanceOf(k)?.app || k));
        // A container that is there but not running was stopped on purpose
        // (a pod torn down leaves its exited containers behind): resting,
        // not down. It must not paint its owner, or the node, red.
        const found = (status?.children || []).map(c => ({
            name: c.name, label: c.name, container: true, children: [],
            status: { running: !!c.running, idle: !c.running, memory_mb: c.memory_mb, cpu_percent: c.cpu_percent,
                      service_type: 'container' },
        }));
        return { name, label, status, container: false, children: [...own, ...found] };
    };
    const top = names.filter(n => !parentOf(node, n) && !(hideObservatory && OBSERVATORY_RE.test(n)));
    const { groups, flat } = groupServices(top);
    return { groups: groups.map(g => ({ ...g, rows: g.services.map(sv => row(sv.name, sv.app)) })),
             flat: flat.map(n => row(n, n)) };
}

// The rows in reading order with their depth, opening a family when
// `open(row)` says so (the modal: always; the card: the fold).
function flattenTree(tree, open) {
    const out = [];
    const walk = (r, depth) => {
        out.push({ ...r, depth });
        if (r.children.length && open(r)) for (const c of r.children) walk(c, depth + 1);
    };
    for (const g of tree.groups) for (const r of g.rows) walk(r, 0);
    for (const r of tree.flat) walk(r, 0);
    return out;
}

// A row's light and memory, the family included: a closed fold still tells
// the truth about what is under it. idle ranks below ok: an owner with one
// running child and one resting is running.
const STATUS_RANK = { down: 3, warn: 2, ok: 1, idle: 0 };
function rollup(rows) {
    let worst = null, total = 0, hasMem = false;
    const take = (r) => {
        const [sym, cls] = symbolFor(r.status);
        if (worst === null || (STATUS_RANK[cls] ?? 0) > (STATUS_RANK[worst[1]] ?? 0)) worst = [sym, cls];
        if (r.status?.memory_mb != null) { total += r.status.memory_mb; hasMem = true; }
        for (const c of r.children) take(c);
    };
    for (const r of rows) take(r);
    return { sym: worst ? worst[0] : '◯', cls: worst ? worst[1] : 'down', mem: hasMem ? total : null };
}

function foldOpen(key, dflt) {
    return toggledFolds.has(key) ? !dflt : dflt;
}

function toggleFold(key) {
    if (toggledFolds.has(key)) toggledFolds.delete(key); else toggledFolds.add(key);
    const el = document.getElementById('grid');
    if (el && window._lastStatus) renderGrid(window._lastStatus);
}

// ----------------------------------------------------------------------
// RENDER — /api/v1/system/status payload → DOM
// ----------------------------------------------------------------------
function renderNode(node) {
    const card = document.createElement('div');
    card.className = 'node' + (node.online ? '' : ' offline');
    const screen = loadScreen();

    const ip = node.agent_node?.manifest?.ip_addresses?.[0] || '-';
    const hostname = node.agent_node?.manifest?.hostname || node.name;
    const hostBadge = node.is_host ? '🏠 -' : '';
    const displayName = node.nickname && node.nickname.trim().length > 0
        ? node.nickname
        : node.name;
    const thermal = node.thermal;
    let headerDangerClass = '';
    if (thermal && thermal.available && thermal.max_temp_c != null && thermal.max_temp_c >= TEMP_DANGER_C) {
        headerDangerClass = ' node-header-danger';
    }

    const header = document.createElement('div');
    header.className = headerDangerClass.trim();
    header.innerHTML = `
        <div class="node-name">${hostBadge} ${escapeHtml(displayName)}</div>
        <div class="node-ip">${escapeHtml(node.name)} · ${escapeHtml(hostname)} · ${escapeHtml(ip)}</div>
      `;
    card.appendChild(header);

    const list = document.createElement('div');
    list.className = 'svc-list';
    // "limit shown before scroll": past this many rows the list scrolls
    // instead of the card growing. 0 is the old behaviour.
    const cap = parseInt(screen.rowsBeforeScroll, 10) || 0;
    if (cap > 0) { list.classList.add('capped'); list.style.setProperty('--svc-rows', String(cap)); }

    if (!node.online) {
        list.innerHTML = `<div class="svc-empty">unreachable</div>`;
    } else if (!node.installed_services || node.installed_services.length === 0) {
        list.innerHTML = `<div class="svc-empty">no services installed</div>`;
    } else {
        const tree = serviceTree(node, { hideObservatory: false });
        const html = [];
        // ▸/▾ (Geometric Shapes) and not ⏵/⏷ (Misc Technical): a Linux kiosk with
        // DejaVu and Noto Emoji has the first pair and draws the second as a hex box.
        const caret = (open) => `<span class="svc-caret">${open ? '▾' : '▸'}</span>`;
        const svcRow = (r, depth) => {
            const own = rollup([r]);
            const badge = serviceBadge(r.container ? { service_type: 'container' } : r.status);
            const key = `${node.name}/svc:${r.name}`;
            const open = r.children.length ? foldOpen(key, !!screen.expandChildren) : false;
            const cls2 = depth > 0 ? ` svc-in-group depth-${depth}` : '';
            html.push(`
                    <div class="svc-name${r.children.length ? ' svc-owner' : ''}${cls2}" title="${escapeHtml(r.name)}${r.children.length ? ` · ${r.children.length} of its own` : ''}"
                         ${r.children.length ? `data-fold="${escapeHtml(key)}"` : ''}>${r.children.length ? caret(open) : ''}${badge.emoji ? `<span class="svc-badge ${badge.cls}" title="${escapeHtml(badge.title)}">${badge.emoji}</span>` : ''}${escapeHtml(r.label)}${r.children.length ? `<span class="svc-count">${r.children.length}</span>` : ''}</div>
                    <div class="svc-mem">${fmtBytes(own.mem)}</div>
                    <div class="svc-stat ${own.cls}">${own.sym}</div>
                  `);
            if (open) for (const c of r.children) svcRow(c, depth + 1);
        };
        for (const g of tree.groups) {
            // The group row carries the worst status and the summed memory of
            // what is inside it, so a collapsed instance still tells the truth.
            const all = rollup(g.rows);
            const key = `${node.name}/${g.instance}`;
            const open = foldOpen(key, !!screen.expandInstances);
            html.push(`
                    <div class="svc-name svc-group" data-fold="${escapeHtml(key)}"
                         title="${g.rows.length} services of the ${escapeHtml(g.instance)} instance">
                      ${caret(open)}${escapeHtml(g.label)}
                      <span class="svc-count">${g.rows.length}</span></div>
                    <div class="svc-mem">${fmtBytes(all.mem)}</div>
                    <div class="svc-stat ${all.cls}">${all.sym}</div>
                  `);
            if (open) for (const r of g.rows) svcRow(r, 1);
        }
        for (const r of tree.flat) svcRow(r, 0);
        list.innerHTML = html.join('');
        list.querySelectorAll('[data-fold]').forEach(el =>
            el.addEventListener('click', ev => { ev.stopPropagation(); toggleFold(el.dataset.fold); }));
    }
    card.appendChild(list);

    const footer = document.createElement('div');
    footer.className = 'node-footer';

    const rt = node.agent_node?.runtime;
    const memTotal = rt?.memory_mb_total;
    const memAvail = rt?.memory_mb_available;
    const memUsedGb = (memTotal != null && memAvail != null)
        ? ((memTotal - memAvail) / 1024).toFixed(1) : '-';
    const memTotalGb = memTotal != null ? (memTotal / 1024).toFixed(0) : '-';

    let tempDisplay = '-';
    let tempClass = '';
    if (thermal && thermal.available && thermal.max_temp_c != null) {
        const tc = thermal.max_temp_c;
        tempDisplay = tc.toFixed(0) + ' °C';
        if (tc >= TEMP_DANGER_C) tempClass = 'temp-danger';
        else if (tc >= TEMP_HOT_C) tempClass = 'temp-hot';
    } else if (thermal && !thermal.available) {
        tempDisplay = 'n/a';
    }

    footer.innerHTML = `
        <div class="row"><span>temp</span><span class="${tempClass}">${tempDisplay}</span></div>
        <div class="row"><span>mem</span><span>${memUsedGb} / ${memTotalGb} Gb</span></div>
      `;
    card.appendChild(footer);

    if (node.online) {
        card.addEventListener('click', () => openNodeModal(node.name));
    }

    return card;
}

// ----------------------------------------------------------------------
// REFRESH CYCLE
// ----------------------------------------------------------------------
async function refresh() {
    const status = await hostApi('/api/v1/system/status');
    if (!status) {
        setLed('down', 'unreachable');
        return;
    }

    const caps = await hostApi('/api/v1/cluster/capabilities');

    for (const node of (status.nodes || [])) {
        const detail = node.services_detail || {};
        node.services = {};
        for (const [svcName, svcData] of Object.entries(detail)) {
            node.services[svcName] = { status: svcData.status };
        }
    }

    renderGrid(status);
    renderGaps(status, caps);
    setOverallLed(status, caps);
    setLastRefresh();

    window._lastStatus = status;
    refreshModal();
    if (upgradesVisible()) refreshUpgrades();
}

function renderGrid(status) {
    const grid = document.getElementById('grid');
    const hidden = loadHiddenNodes();
    grid.innerHTML = '';
    let shown = 0;
    for (const node of (status.nodes || [])) {
        if (!nodeShownHere(node, hidden)) continue;
        grid.appendChild(renderNode(node));
        shown += 1;
    }
    grid.appendChild(renderActionsCard());
    // The glance row is as wide as it has nodes, up to GLANCE_MAX; past that
    // it pages and the arrows show. Decided from the count, not measured, so
    // the arrows never flicker in and out as the row re-lays itself.
    grid.style.setProperty('--glance-cols', String(Math.min(Math.max(shown, 1), GLANCE_MAX)));
    document.getElementById('grid-row').classList.toggle('paged', shown > GLANCE_MAX);
    updateGlanceArrows();
}

// Paging the glance row: one card per tap, arrows dim at the ends.
function glanceStep() {
    const first = document.querySelector('#grid > .node');
    return first ? first.getBoundingClientRect().width + 10 : 300;
}

function updateGlanceArrows() {
    const grid = document.getElementById('grid');
    const prev = document.getElementById('glance-prev');
    const next = document.getElementById('glance-next');
    if (!prev || !next) return;
    prev.disabled = grid.scrollLeft <= 1;
    next.disabled = grid.scrollLeft + grid.clientWidth >= grid.scrollWidth - 1;
}

function initGlance() {
    const grid = document.getElementById('grid');
    const prev = document.getElementById('glance-prev');
    const next = document.getElementById('glance-next');
    if (!prev || !next) return;
    prev.addEventListener('click', () => grid.scrollBy({ left: -glanceStep(), behavior: 'smooth' }));
    next.addEventListener('click', () => grid.scrollBy({ left: glanceStep(), behavior: 'smooth' }));
    grid.addEventListener('scroll', updateGlanceArrows, { passive: true });
    window.addEventListener('resize', updateGlanceArrows);
}

// Force eager rediscovery, then re-read. Shared by the actions card and the
// header-strip button the glance screen uses instead of it.
async function eagerRefresh(btn) {
    btn.disabled = true;
    try {
        await hostApi('/api/v1/cluster/refresh', { method: 'POST' });
        await refresh();
    } finally {
        btn.disabled = false;
    }
}

function renderActionsCard() {
    const actions = document.createElement('div');
    actions.className = 'actions-card';

    const settingsBtn = document.createElement('button');
    settingsBtn.className = 'actions-btn';
    settingsBtn.id = 'actions-settings';
    settingsBtn.innerHTML = '<span class="actions-icon">⚙</span><span class="actions-label">settings</span>';
    settingsBtn.addEventListener('click', openSettingsModal);

    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'actions-btn';
    refreshBtn.id = 'actions-refresh';
    refreshBtn.title = 'Force eager rediscovery';
    refreshBtn.innerHTML = '<span class="actions-icon">↻</span><span class="actions-label">refresh</span>';
    refreshBtn.addEventListener('click', () => eagerRefresh(refreshBtn));

    actions.appendChild(settingsBtn);
    actions.appendChild(refreshBtn);
    return actions;
}

function renderGaps(status, caps) {
    const offlineNodes = (status.nodes || []).filter(n => !n.online).map(n => n.name);
    const gapsEl = document.getElementById('gaps');
    const listEl = document.getElementById('gap-list');

    if (offlineNodes.length === 0) {
        gapsEl.style.display = 'none';
        return;
    }
    gapsEl.style.display = 'block';
    listEl.textContent = `Offline nodes: ${offlineNodes.join(', ')}`;
}

function setOverallLed(status, caps) {
    const nodes = status.nodes || [];
    const offlineCount = nodes.filter(n => !n.online).length;

    if (offlineCount > 0) {
        setLed('down', `${offlineCount} node(s) offline`);
        return;
    }

    // Degraded is what should be up and is not. Idle is the other half: off
    // because nobody needs it right now, and said so, not counted against
    // the cluster (20 Oct 2026).
    let degraded = 0, idle = 0, total = 0;
    for (const node of nodes) {
        for (const svcName of (node.installed_services || [])) {
            total++;
            const ss = node.services?.[svcName]?.status;
            const [, cls] = symbolFor(ss);
            if (cls === 'idle') idle++;
            else if (cls !== 'ok') degraded++;
        }
    }

    const idleNote = idle > 0 ? ` · ${idle} idle` : '';
    if (degraded > 0) setLed('warn', `${degraded}/${total} services degraded${idleNote}`);
    else setLed('ok', `${total - idle}/${total} services healthy${idleNote}`);
}

function setLed(cls, text) {
    const led = document.getElementById('led');
    led.className = 'led ' + cls;
    document.getElementById('led-text').textContent = text;
    // Lit in the accent while all is well, red when a node is offline or a
    // service that should be up is not (20 Oct 2026). Idle rides in the
    // text; it is not a fault.
    const pill = document.getElementById('led-overall');
    pill.classList.toggle('bad', cls === 'down' || cls === 'warn');
    pill.classList.toggle('hot', cls === 'ok');
}

function setLastRefresh() {
    document.getElementById('last-refresh').textContent =
        'refreshed ' + new Date().toLocaleTimeString();
}

// ----------------------------------------------------------------------
// UPGRADE ONE NODE — from its card: a Lodestar upgrade run narrowed to that
// node (every seren service on it that is behind). This replaced the
// agent-update push, which needed a tarball path in the yaml that no pip
// install has and so only ever answered "not configured".
// ----------------------------------------------------------------------
async function upgradeNode(nodeName) {
    setModalFlash(`starting an upgrade run for ${nodeName}…`, 'info');
    const { status, body } = await postUpgrade({ nodes: [nodeName], only_behind: true, backup: true,
                                                 reason: `node card: ${nodeName}` });
    if (status === 202 && body.ok) {
        setModalFlash(`run ${body.run.id} started for ${nodeName}: ${body.run.steps.length} step(s) - see Upgrades`, 'ok');
        showTab('upgrades');
        closeNodeModal();
        refreshUpgrades();
        openRun(body.run.id);
        return;
    }
    setModalFlash(body.error || `Lodestar answered HTTP ${status}`, 'err');
}

// POST /api/v1/upgrade and keep the answer whatever the status: a 409 here is
// a reason ("a run is already going", "nothing to upgrade"), not a failure
// to bury behind the banner.
async function postUpgrade(json) {
    const headers = { 'Accept': 'application/json', 'Content-Type': 'application/json' };
    const token = (typeof getToken === 'function' ? getToken() : '') || '';
    if (token) headers['Authorization'] = 'Bearer ' + token;
    try {
        const resp = await fetch('/api/v1/upgrade', { method: 'POST', headers, body: JSON.stringify(json) });
        let body = {};
        try { body = await resp.json(); } catch (e) { body = {}; }
        if (resp.status === 401 || resp.status === 403) banner(`API /api/v1/upgrade → HTTP ${resp.status} (set the 🔑 token)`);
        return { status: resp.status, body };
    } catch (e) {
        banner(`Cannot reach Lodestar — ${e.message}`);
        return { status: 0, body: { error: `cannot reach Lodestar: ${e.message}` } };
    }
}

// ----------------------------------------------------------------------
// SETTINGS MODAL — a door to the Upgrades tab and a word about the token,
// which lives in the shell's 🔑 modal; same-origin means no URL field.
// ----------------------------------------------------------------------
function initSettings() {
    document.getElementById('settings-close').addEventListener('click', closeSettingsModal);

    document.getElementById('settings-backdrop').addEventListener('click', (e) => {
        if (e.target.id === 'settings-backdrop') closeSettingsModal();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isSettingsOpen()) closeSettingsModal();
    });

    // The screen's folds and its row cap: saved as they change, applied to
    // the grid at once. The folds someone opened by hand (toggledFolds) are
    // relative to the default, so flipping the default would flip them too;
    // "auto expand" means all of them, so the hand-made ones are cleared.
    for (const id of ['cfg-expand-instances', 'cfg-expand-children']) {
        document.getElementById(id).addEventListener('change', () => {
            const prefs = loadScreen();
            prefs.expandInstances = document.getElementById('cfg-expand-instances').checked;
            prefs.expandChildren = document.getElementById('cfg-expand-children').checked;
            saveScreen(prefs);
            toggledFolds.clear();
            if (window._lastStatus) renderGrid(window._lastStatus);
        });
    }
    document.getElementById('cfg-rows').addEventListener('change', (e) => {
        const prefs = loadScreen();
        prefs.rowsBeforeScroll = Math.max(0, parseInt(e.target.value, 10) || 0);
        e.target.value = prefs.rowsBeforeScroll;
        saveScreen(prefs);
        if (window._lastStatus) renderGrid(window._lastStatus);
    });

    // The header-strip twins of the actions card (glance screen only).
    const asideSettings = document.getElementById('aside-settings');
    if (asideSettings) asideSettings.addEventListener('click', openSettingsModal);
    const asideRefresh = document.getElementById('aside-refresh');
    if (asideRefresh) asideRefresh.addEventListener('click', () => eagerRefresh(asideRefresh));
}

// "On this screen": one tick box per node the cluster knows, from the last
// payload. Unticking hides the node from this browser's grid at once; the
// LED, the gaps strip and every other browser are untouched.
function renderScreenNodes() {
    const box = document.getElementById('cfg-nodes');
    const nodes = window._lastStatus?.nodes || [];
    if (!nodes.length) { box.innerHTML = '<span class="cfg-empty">no nodes yet</span>'; return; }
    const hidden = loadHiddenNodes();
    box.innerHTML = '';
    for (const node of nodes) {
        const label = document.createElement('label');
        const off = hidden.has(node.name);
        const needs = off && nodeNeedsYou(node);
        label.className = (off ? 'off' : '') + (needs ? ' needs' : '');
        label.title = needs ? 'hidden here; it is offline or has something down' : '';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !off;
        cb.addEventListener('change', () => {
            const set = loadHiddenNodes();
            if (cb.checked) set.delete(node.name); else set.add(node.name);
            saveHiddenNodes(set);
            renderScreenNodes();
            if (window._lastStatus) renderGrid(window._lastStatus);
        });
        label.appendChild(cb);
        label.appendChild(document.createTextNode(node.nickname?.trim() || node.name));
        box.appendChild(label);
    }
}

function renderScreenPrefs() {
    const prefs = loadScreen();
    document.getElementById('cfg-expand-instances').checked = !!prefs.expandInstances;
    document.getElementById('cfg-expand-children').checked = !!prefs.expandChildren;
    document.getElementById('cfg-rows').value = parseInt(prefs.rowsBeforeScroll, 10) || 0;
}

function openSettingsModal() {
    renderScreenNodes();
    renderScreenPrefs();
    document.getElementById('settings-backdrop').classList.add('visible');
}

function closeSettingsModal() {
    document.getElementById('settings-backdrop').classList.remove('visible');
}

function isSettingsOpen() {
    return document.getElementById('settings-backdrop').classList.contains('visible');
}

// ----------------------------------------------------------------------
// UPGRADES TAB — /api/v1/upgrade (seren_lodestar.upgrade): what is behind
// the index, a dry run, a run, and the runs so far. The routes and the
// Workbench tools were there first; this is the page they never had.
// Nothing is decided here: `behind`, the plan and the order are Lodestar's.
// ----------------------------------------------------------------------
const UPG_STATE_CLASS = { done: 'ok', current: 'ok', running: 'run', pending: 'wait', handed_off: 'run',
                          partial: 'warn', held: 'warn', skipped: 'warn', failed: 'bad', not_started: 'wait' };

function upgradesVisible() {
    const v = document.getElementById('upgrades');
    return !!(v && v.classList.contains('active'));
}

function upgFlash(msg, type) {
    const el = document.getElementById('upg-flash');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'upg-flash' + (msg ? ` visible ${type || 'info'}` : '');
}

function upgBadge(state) {
    const s = state || 'unknown';
    return `<span class="upg-state ${UPG_STATE_CLASS[s] || 'wait'}">${escapeHtml(s.replace('_', ' '))}</span>`;
}

function upgCell(v) {
    return escapeHtml(v == null ? '—' : String(v));
}

let _upgBehind = null;      // the last /upgrade/behind
let _upgStatus = null;      // the last /upgrade describe()
let _upgOpenRun = null;     // the run shown in detail

async function refreshUpgrades(refreshIndex = false) {
    const [behind, status] = await Promise.all([
        hostApi('/api/v1/upgrade/behind' + (refreshIndex ? '?refresh=true' : '')),
        hostApi('/api/v1/upgrade'),
    ]);
    if (behind) { _upgBehind = behind; renderBehind(behind); }
    if (status) { _upgStatus = status; renderRuns(status); }
    const go = document.getElementById('upg-go');
    const moves = !!(behind && behind.behind && behind.behind.length);
    go.disabled = !moves || !!(status && status.busy);
    go.title = status && status.busy ? 'a run is going' : moves ? `move ${behind.behind.length} service(s) to their newest`
             : 'everything is on its newest';
    // The run modal follows the run it shows while it is going: the steps,
    // and the log of the open step (20 Oct 2026: "log portion of the
    // modal ... is not refreshing as new data comes in"). The log is asked
    // again quietly, no placeholder, until the step it shows has ended.
    if (_upgOpenRun && isRunOpen()) {
        const fresh = status?.busy && status.current?.id === _upgOpenRun ? status.current
                    : (status?.runs || []).find(r => r.id === _upgOpenRun);
        if (fresh) {
            renderRunModal(fresh);
            const shown = runModal.step != null ? (fresh.steps || [])[runModal.step] : null;
            if (shown && !runModal.logSettled) showStepLog(fresh.id, runModal.step, { quiet: true });
        }
    }
}

function renderBehind(data) {
    const rows = data.services || [];
    const n = (data.behind || []).length;
    document.getElementById('upg-behind-count').textContent =
        rows.length ? `${n} of ${rows.length} behind` + (data.unknown_nodes?.length ? ` · ${data.unknown_nodes.join(', ')} did not say` : '') : '';
    const tb = document.querySelector('#upg-behind tbody');
    if (!rows.length) {
        tb.innerHTML = '<tr><td class="upg-empty">no online node reports a seren service an Observatory can upgrade</td></tr>';
        return;
    }
    tb.innerHTML = '<tr><th>node</th><th>service</th><th>running</th><th>newest</th><th></th></tr>'
        + rows.map((r, i) => {
            const state = r.behind === true ? 'behind' : r.behind === false ? 'current' : 'unknown';
            const why = r.latest_error ? ` title="${escapeHtml(r.latest_error)}"` : '';
            // A library behind is its own mark: the service can be on its
            // newest and still run a stale sinew or meninges.
            const libsBehind = Object.entries(r.libs || {}).filter(([, l]) => l.behind === true).map(([n]) => n.replace(/^seren-/, ''));
            const libMark = libsBehind.length ? `<span class="upg-state warn" title="${escapeHtml(libsBehind.join(', '))} behind">↑ ${escapeHtml(libsBehind.join(' + '))}</span>` : '';
            return `<tr class="${state} upg-svc" data-row="${i}" title="click for its packages"><td>${upgCell(r.node)}</td><td>${upgCell(r.service)}</td>`
                 + `<td class="mono">${upgCell(r.version)}</td><td class="mono"${why}>${upgCell(r.latest)}</td>`
                 + `<td>${r.behind === true ? '<span class="upg-state warn">↑ behind</span>'
                        : r.behind === false ? '<span class="upg-state ok">current</span>'
                        : '<span class="upg-state wait">?</span>'}${libMark}</td></tr>`;
        }).join('');
    if (pkgModal.open) renderPkgModal();
}

// ----------------------------------------------------------------------
// PACKAGE MODAL (20 Oct 2026): a service clicked in the Behind table. Its
// own package and the libraries its venv runs (sinew, meninges), each
// against the index's newest, each with an Update; Update all moves the
// three. Every button is a run of that one service through postUpgrade, so
// the stash, the import check, the restart and the rollback are the usual
// ones. A library alone pins the service to the version it has and names
// the library in `with`: pip moves the library, the service stays, and the
// node still restarts it so it runs the new code.
// ----------------------------------------------------------------------
const pkgModal = { open: false, node: null, service: null };

function pkgRow() {
    return (_upgBehind?.services || []).find(r => r.node === pkgModal.node && r.service === pkgModal.service) || null;
}

function openPkgModal(node, service) {
    pkgModal.open = true;
    pkgModal.node = node;
    pkgModal.service = service;
    document.getElementById('pkg-backdrop').classList.add('visible');
    pkgFlash('');
    renderPkgModal();
}

function closePkgModal() {
    pkgModal.open = false;
    document.getElementById('pkg-backdrop').classList.remove('visible');
}

function pkgFlash(msg, type) {
    pkgModal.flash = msg ? { msg, type: type || 'info' } : null;
    const el = document.getElementById('pkg-flash');
    if (el) { el.textContent = msg || ''; el.className = 'pkg-flash' + (msg ? ` ${type || 'info'}` : ''); }
}

function pkgState(behind) {
    return behind === true ? '<span class="upg-state warn">↑ behind</span>'
         : behind === false ? '<span class="upg-state ok">current</span>'
         : '<span class="upg-state wait">?</span>';
}

function renderPkgModal() {
    const r = pkgRow();
    const busy = !!(_upgStatus && _upgStatus.busy);
    document.getElementById('pkg-header-fields').innerHTML = `
        <div class="modal-header-field"><div class="modal-header-label">Service</div><div class="modal-header-value">${escapeHtml(pkgModal.service || '')}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">Node</div><div class="modal-header-value">${escapeHtml(pkgModal.node || '')}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">State</div><div class="modal-header-value">${r ? pkgState(r.behind) : '<span class="upg-state wait">gone</span>'}</div></div>`;
    const bodyEl = document.getElementById('pkg-body');
    if (!r) {
        bodyEl.innerHTML = '<div class="pkg-flash" id="pkg-flash"></div><div class="upg-empty">this service is not in the last Behind answer</div>';
        return;
    }
    const pkgs = [{ name: r.package, version: r.version, latest: r.latest, behind: r.behind, latest_error: r.latest_error, self: true }]
        .concat(Object.entries(r.libs || {}).map(([name, l]) => ({ name, ...l, self: false })));
    const row = (p) => {
        const can = !busy && p.behind === true;
        const why = p.latest_error ? ` title="${escapeHtml(p.latest_error)}"` : '';
        return `<tr class="${p.self ? 'pkg-self' : ''}${p.behind === true ? ' behind' : ''}"><td class="pkg-name">${upgCell(p.name)}</td>`
             + `<td class="mono">${upgCell(p.version)}</td><td class="mono"${why}>${upgCell(p.latest)}</td><td>${pkgState(p.behind)}</td>`
             + `<td class="pkg-act"><button class="upg-btn" data-pkg="${escapeHtml(p.name)}" ${can ? '' : 'disabled'} `
             + `title="${busy ? 'a run is going' : p.behind === true ? `move ${escapeHtml(p.name)} to ${escapeHtml(p.latest || 'its newest')}` : 'nothing newer on the index'}">update</button></td></tr>`;
    };
    const anyBehind = pkgs.some(p => p.behind === true);
    bodyEl.innerHTML = `<div class="pkg-flash${pkgModal.flash ? ' ' + pkgModal.flash.type : ''}" id="pkg-flash">${escapeHtml(pkgModal.flash?.msg || '')}</div>`
        + `<div class="upg-table-wrap"><table class="upg-table"><tbody><tr><th>package</th><th>running</th><th>newest</th><th></th><th></th></tr>${pkgs.map(row).join('')}</tbody></table></div>`
        + `<div class="pkg-foot"><div class="upg-note">${busy ? 'a run is going; the buttons wake when it ends' : 'an update backs the service up, installs, checks the import, restarts it and waits for health; anything that fails is rolled back'}</div>`
        + `<button class="upg-btn upg-go" id="pkg-all" ${!busy && anyBehind ? '' : 'disabled'} title="the service and both libraries to their newest">update all</button></div>`;
    bodyEl.querySelectorAll('button[data-pkg]').forEach(b => b.addEventListener('click', () => updatePackage(b.dataset.pkg)));
    document.getElementById('pkg-all')?.addEventListener('click', () => updatePackage(null));
}

// pkg: the service's own package (its newest, the libraries left alone),
// a library (the service pinned where it is, the library named), or null
// for all three.
async function updatePackage(pkg) {
    const r = pkgRow();
    if (!r) return;
    const self = !pkg || pkg === r.package;
    const body = { nodes: [r.node], services: [r.service], only_behind: false, backup: true,
                   reason: `packages: ${r.service} ${pkg || 'all'}` };
    if (!pkg) {
        body.version = null;                         // the service to its newest, sinew + meninges with it
    } else if (self) {
        body.version = null; body.with = [];        // the service alone
    } else {
        body.version = r.version; body.with = [pkg]; // the library alone, the service pinned where it is
    }
    const what = !pkg ? `${r.service} and both libraries` : pkg;
    if (!confirm(`Update ${what} on ${r.node}?\n\nThe service is backed up to the stash first, then pip, an import check, a restart and health; a failure rolls it back.`)) return;
    pkgFlash(`starting a run for ${what}…`, 'info');
    const { status, body: out } = await postUpgrade(body);
    if (status !== 202 || !out.ok) {
        pkgFlash(out.error || `Lodestar answered HTTP ${status}`, 'err');
        return;
    }
    pkgFlash(`run ${out.run.id} started`, 'ok');
    closePkgModal();
    await refreshUpgrades();
    openRun(out.run.id);
}

function initPkgModal() {
    document.getElementById('pkg-close').addEventListener('click', closePkgModal);
    document.getElementById('pkg-refresh').addEventListener('click', async (e) => {
        e.currentTarget.disabled = true;
        pkgFlash('asking the node and the index…', 'info');
        try { await refreshUpgrades(true); } finally { document.getElementById('pkg-refresh').disabled = false; }
        pkgFlash('');
        renderPkgModal();
    });
    document.getElementById('pkg-backdrop').addEventListener('click', (e) => {
        if (e.target.id === 'pkg-backdrop') closePkgModal();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && pkgModal.open) closePkgModal();
    });
    document.getElementById('upg-behind').addEventListener('click', (e) => {
        const tr = e.target.closest('tr[data-row]');
        const r = tr && (_upgBehind?.services || [])[parseInt(tr.dataset.row, 10)];
        if (r) openPkgModal(r.node, r.service);
    });
}

async function planUpgrades() {
    const btn = document.getElementById('upg-plan');
    btn.disabled = true;
    upgFlash('asking every node for its dry run…', 'info');
    try {
        const plan = await hostApi('/api/v1/upgrade', { method: 'POST', json: { dry_run: true, only_behind: true } });
        if (!plan) { upgFlash('the plan did not come back', 'err'); return; }
        renderPlan(plan);
        upgFlash(plan.ok ? 'plan ready - nothing was changed' : 'plan ready, with problems - see each line', plan.ok ? 'ok' : 'err');
    } finally {
        btn.disabled = false;
    }
}

function renderPlan(plan) {
    // The plan opens in the same modal as a run (20 Oct 2026: "Plan
    // should show a modal similar to viewing an upgrade. currently it slaps
    // it between behind and runs and squishes and looks weird"). Left: every
    // service in run order; click a line and the right half says what it
    // would install, or why it would not move.
    _upgOpenRun = null;
    runModal.open = true;
    runModal.step = null;
    runModal.plan = plan;
    const order = plan.order || [];
    const current = new Set(plan.current || []);
    const moving = order.filter(k => !current.has(k)).length;
    document.getElementById('run-header-fields').innerHTML = `
        <div class="modal-header-field"><div class="modal-header-label">Plan</div><div class="modal-header-value">dry run · nothing changed</div></div>
        <div class="modal-header-field"><div class="modal-header-label">Would move</div><div class="modal-header-value">${moving}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">Current</div><div class="modal-header-value">${current.size}</div></div>
        ${plan.skipped_offline?.length ? `<div class="modal-header-field"><div class="modal-header-label">Offline</div><div class="modal-header-value">${escapeHtml(plan.skipped_offline.join(', '))}</div></div>` : ''}`;
    const lines = order.map((key, i) => {
        const [node, svc] = key.split('/');
        const d = ((plan.nodes || {})[node] || {})[svc] || {};
        let state, what;
        if (current.has(key)) { state = 'current'; what = `${upgCell(d.current)} is the newest`; }
        else if (d.skipped) { state = 'skipped'; what = upgCell(d.skipped); }
        else if (d.held) { state = 'held'; what = upgCell(((d.needs || {}).problems || []).join('; ') || 'a peer is too old'); }
        else if (d.error) { state = 'failed'; what = upgCell(d.error); }
        else {
            state = 'pending';
            const moves = (d.would_install || []).map(w => `${w.name} ${w.version}`);
            what = moves.length ? `would install ${upgCell(moves.join(', '))}` : 'nothing to install';
            if (d.before && d.package) what = `${upgCell(d.before[d.package])} → ` + what;
        }
        return `<tr class="${state} upg-step" data-plan-row="${i}"><td>${upgCell(node)}</td><td>${upgCell(svc)}</td><td>${upgBadge(state)}</td><td>${what}</td></tr>`;
    });
    document.getElementById('run-steps').innerHTML =
        (plan.error ? `<div class="upg-note bad">${escapeHtml(plan.error)}</div>` : '')
        + `<div class="upg-table-wrap"><table class="upg-table"><tbody><tr><th>node</th><th>service</th><th></th><th>in run order</th></tr>${lines.join('')}</tbody></table></div>`
        + (plan.held?.length ? `<div class="upg-note">held: ${escapeHtml(plan.held.join(' · '))}</div>` : '');
    document.getElementById('run-log').innerHTML = '<div class="run-log-empty">click a line to see what it would install, or why it would not move</div>';
    document.getElementById('run-backdrop').classList.add('visible');
    document.getElementById('run-steps').querySelectorAll('tr[data-plan-row]').forEach(tr =>
        tr.addEventListener('click', () => showPlanDetail(plan, order[parseInt(tr.dataset.planRow, 10)], tr)));
}

function showPlanDetail(plan, key, tr) {
    document.querySelectorAll('#run-steps tr[data-plan-row]').forEach(r => r.classList.toggle('open', r === tr));
    const [node, svc] = key.split('/');
    const d = ((plan.nodes || {})[node] || {})[svc] || {};
    const lines = [`<div class="run-log-head"><b>${upgCell(node)}/${upgCell(svc)}</b>${d.package ? ` <span class="run-log-dim">${escapeHtml(d.package)}</span>` : ''}</div>`];
    const kv = (k, v) => lines.push(`<div class="run-log-kv"><span class="run-log-key">${escapeHtml(k)}</span> ${escapeHtml(typeof v === 'string' ? v : JSON.stringify(v))}</div>`);
    const list = (k, arr) => { if (arr && arr.length) lines.push(`<div class="run-log-key">${escapeHtml(k)}</div><pre class="run-log-pre">${escapeHtml(arr.join('\n'))}</pre>`); };
    if (d.before) kv('before', d.before);
    list('would install', (d.would_install || []).map(w => `${w.name} ${w.version}`));
    if (d.current) kv('current', d.current);
    if (d.skipped) kv('skipped', d.skipped);
    if (d.held) kv('held', d.held);
    list('problems', (d.needs || {}).problems || []);
    list('unchecked', (d.needs || {}).unchecked || []);
    if (d.error) lines.push(`<div class="run-log-line bad">${escapeHtml(d.error)}</div>`);
    if (lines.length === 1) lines.push('<div class="run-log-note">nothing more to say about this one</div>');
    document.getElementById('run-log').innerHTML = lines.join('');
}

async function startUpgrades() {
    const n = (_upgBehind?.behind || []).length;
    const list = (_upgBehind?.behind || []).slice(0, 12).join('\n') + (n > 12 ? `\n… and ${n - 12} more` : '');
    if (!confirm(`Upgrade ${n} service(s) that are behind?\n\n${list}\n\nEach is backed up to the stash first, then pip, an import check, a restart and health; a failure rolls that one back and the run goes on. Stores first, each node's Observatory last, Lodestar last of all.`)) return;
    const go = document.getElementById('upg-go');
    go.disabled = true;
    upgFlash('starting the run…', 'info');
    const { status, body } = await postUpgrade({ only_behind: true, backup: true, reason: 'from the viewer' });
    if (status !== 202 || !body.ok) {
        upgFlash(body.error || `Lodestar answered HTTP ${status}`, 'err');
        go.disabled = false;
        return;
    }
    upgFlash(`run ${body.run.id} started: ${body.run.steps.length} step(s)`, 'ok');
    await refreshUpgrades();
    openRun(body.run.id);
}

function renderRuns(status) {
    const runs = status.runs || [];
    document.getElementById('upg-runs-count').textContent = runs.length ? `${runs.length} kept` : '';
    const cur = document.getElementById('upg-current');
    if (status.busy && status.current) {
        const r = status.current;
        const done = r.steps.filter(s => !['pending', 'running'].includes(s.state)).length;
        const now = r.steps.find(s => s.state === 'running');
        cur.innerHTML = `<div class="upg-live">${upgBadge('running')} <b>${escapeHtml(r.id)}</b> · ${done}/${r.steps.length} steps`
            + (now ? ` · now ${escapeHtml(now.node)}/${escapeHtml(now.service)}` : '') + ` · ${escapeHtml(r.reason || '')}`
            + ` <button class="upg-link" data-run="${escapeHtml(r.id)}">open</button></div>`;
    } else {
        cur.innerHTML = '';
    }
    const tb = document.querySelector('#upg-runs tbody');
    if (!runs.length) { tb.innerHTML = '<tr><td class="upg-empty">none yet</td></tr>'; return; }
    tb.innerHTML = '<tr><th>run</th><th>when</th><th></th><th>steps</th><th>why</th></tr>' + runs.map(r => {
        const steps = r.steps || [];
        const moved = steps.filter(s => s.state === 'done').length;
        const bad = steps.filter(s => s.state === 'failed').length;
        return `<tr class="upg-run${r.id === _upgOpenRun ? ' open' : ''}" data-run="${escapeHtml(r.id)}">`
             + `<td class="mono">${escapeHtml(r.id)}</td><td>${fmtTime(r.created_at ? new Date(r.created_at * 1000).toISOString() : null)}</td>`
             + `<td>${upgBadge(r.state)}</td><td>${moved} moved${bad ? `, ${bad} failed` : ''}${(r.held || []).length ? `, ${r.held.length} held` : ''} of ${steps.length}</td>`
             + `<td>${upgCell(r.reason)}</td></tr>`;
    }).join('');
}

// The run in a modal, not at the foot of the page (20 Oct 2026: "when
// clicking on a run, the data should show in a modal ... we should be able to
// click the line and view the logs of what happened"). The left half is the
// steps; a step clicked fetches its job from the node and the right half is
// every note that job took - the pip tail, the health that never came, the
// peer that was too old. What to read when an upgrade failed.
const runModal = { open: false, step: null, plan: null, logSettled: false };

function isRunOpen() { return runModal.open; }

async function openRun(id) {
    _upgOpenRun = id;
    runModal.step = null;
    const out = await hostApi(`/api/v1/upgrade/${encodeURIComponent(id)}`);
    if (!out || !out.run) return;
    runModal.open = true;
    document.getElementById('run-backdrop').classList.add('visible');
    document.getElementById('run-log').innerHTML = '<div class="run-log-empty">click a step to see what happened on the node</div>';
    renderRunModal(out.run);
    if (_upgStatus) renderRuns(_upgStatus);
}

function closeRun() {
    runModal.open = false;
    runModal.step = null;
    runModal.plan = null;
    _upgOpenRun = null;
    document.getElementById('run-backdrop').classList.remove('visible');
    if (_upgStatus) renderRuns(_upgStatus);
}

function renderRunModal(run) {
    const when = run.created_at ? new Date(run.created_at * 1000).toLocaleString() : '-';
    document.getElementById('run-header-fields').innerHTML = `
        <div class="modal-header-field"><div class="modal-header-label">Run</div><div class="modal-header-value">${escapeHtml(run.id)}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">State</div><div class="modal-header-value">${upgBadge(run.state)}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">When</div><div class="modal-header-value">${escapeHtml(when)}</div></div>
        <div class="modal-header-field"><div class="modal-header-label">Why</div><div class="modal-header-value">${upgCell(run.reason)}</div></div>`;
    const rows = (run.steps || []).map((s, i) => {
        const ver = s.before && s.after && s.service
            ? `${upgCell(Object.values(s.before)[0])} → ${upgCell(Object.values(s.after)[0])}` : upgCell(s.version);
        const note = s.error || s.held || s.skipped || (s.state === 'current' ? `${upgCell(s.current)} already the newest` : '')
                   || (s.job ? `job ${s.job}` : '');
        return `<tr class="${s.state} upg-step${runModal.step === i ? ' open' : ''}" data-step="${i}"><td>${upgCell(s.node)}</td><td>${upgCell(s.service)}</td><td>${upgBadge(s.state)}</td>`
             + `<td class="mono">${ver}</td><td>${escapeHtml(String(note || ''))}</td></tr>`;
    });
    document.getElementById('run-steps').innerHTML =
        (run.error ? `<div class="upg-note bad">${escapeHtml(run.error)}</div>` : '')
        + `<div class="upg-table-wrap"><table class="upg-table"><tbody><tr><th>node</th><th>service</th><th></th><th>version</th><th></th></tr>${rows.join('')}</tbody></table></div>`;
    document.getElementById('run-steps').querySelectorAll('tr[data-step]').forEach(tr =>
        tr.addEventListener('click', () => showStepLog(run.id, parseInt(tr.dataset.step, 10))));
}

function fmtClock(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return isNaN(d) ? '' : d.toLocaleTimeString();
}

// Terminal on both sides: the step has ended and the job on the node has
// too (a job that is still "restarting" keeps taking notes). After that
// the poll leaves the log alone.
const STEP_LIVE = ['pending', 'running', 'handed_off'];

async function showStepLog(runId, index, { quiet = false } = {}) {
    if (runModal.step !== index) runModal.logSettled = false;
    runModal.step = index;
    document.querySelectorAll('#run-steps tr[data-step]').forEach(tr =>
        tr.classList.toggle('open', parseInt(tr.dataset.step, 10) === index));
    const log = document.getElementById('run-log');
    if (!quiet) log.innerHTML = '<div class="run-log-empty">asking the node…</div>';
    const out = await hostApi(`/api/v1/upgrade/${encodeURIComponent(runId)}/step/${index}`);
    if (runModal.step !== index || !isRunOpen()) return;          // another step was opened meanwhile
    if (!out || !out.ok) { if (!quiet) log.innerHTML = '<div class="run-log-empty">the step did not come back</div>'; return; }
    const step = out.step || {};
    runModal.logSettled = !STEP_LIVE.includes(step.state) && !(out.job && out.job.terminal === false);
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    const head = `<div class="run-log-head"><b>${upgCell(step.node)}/${upgCell(step.service)}</b> ${upgBadge(step.state)}`
               + (step.backup?.stashed?.length ? ` <span class="run-log-dim">backup ${escapeHtml(step.backup.stashed.join(', '))}</span>` : '') + `</div>`;
    if (!out.job) {
        log.innerHTML = head + `<div class="run-log-note">${escapeHtml(out.note || 'nothing more to show')}</div>`;
        return;
    }
    const job = out.job;
    const lines = [];
    lines.push(`<div class="run-log-line"><span class="run-log-dim">${escapeHtml(fmtClock(job.created_at))}</span> job ${escapeHtml(job.id)} · ${escapeHtml(job.package || '')} ${escapeHtml(job.version || 'newest')} · ${upgBadge(job.state)}</div>`);
    for (const n of (job.steps || [])) {
        // Everything the note carried beyond its name and verdict; an empty
        // list or string says nothing, so it is not drawn.
        const extra = Object.entries(n).filter(([k, v]) => !['step', 'ok', 'at'].includes(k)
                                                       && !(Array.isArray(v) && !v.length) && v !== '' && v != null);
        lines.push(`<div class="run-log-line ${n.ok ? 'ok' : 'bad'}"><span class="run-log-dim">${escapeHtml(fmtClock(n.at))}</span> ${escapeHtml(n.step)} ${n.ok ? '✓' : '✗'}</div>`);
        for (const [k, v] of extra) {
            if (Array.isArray(v) && v.every(x => typeof x === 'string')) {
                lines.push(`<div class="run-log-key">${escapeHtml(k)}</div><pre class="run-log-pre">${escapeHtml(v.join('\n'))}</pre>`);
            } else {
                lines.push(`<div class="run-log-kv"><span class="run-log-key">${escapeHtml(k)}</span> ${escapeHtml(typeof v === 'string' ? v : JSON.stringify(v))}</div>`);
            }
        }
    }
    if (job.error) lines.push(`<div class="run-log-line bad">${escapeHtml(job.error)}</div>`);
    log.innerHTML = head + lines.join('');
    if (atBottom) log.scrollTop = log.scrollHeight;              // following along: keep the newest note in view
}

function initRunModal() {
    document.getElementById('run-close').addEventListener('click', closeRun);
    document.getElementById('run-backdrop').addEventListener('click', (e) => {
        if (e.target.id === 'run-backdrop') closeRun();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && runModal.open) closeRun();
    });
}

function initUpgrades() {
    document.getElementById('upg-refresh').addEventListener('click', async (e) => {
        e.target.disabled = true;
        try { await refreshUpgrades(true); } finally { e.target.disabled = false; }
    });
    document.getElementById('upg-plan').addEventListener('click', planUpgrades);
    document.getElementById('upg-go').addEventListener('click', startUpgrades);
    document.getElementById('upg-runs').addEventListener('click', (e) => {
        const row = e.target.closest('tr[data-run]');
        if (row) openRun(row.dataset.run);
    });
    document.getElementById('upg-current').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-run]');
        if (btn) openRun(btn.dataset.run);
    });
    // The tab's own onclick (tabs.html) switches the view and fetches: the
    // shell styles tabs and activates the first, it does not wire clicks.
}

// ----------------------------------------------------------------------
// MAIN
// ----------------------------------------------------------------------
initSettings();
initModal();
initUpgrades();
initRunModal();
initPkgModal();
initGlance();
refresh();
startPolling(POLL_INTERVAL);
