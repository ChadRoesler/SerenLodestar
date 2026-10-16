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
//   ◯  not running            ⚠  degraded / unknown
// ----------------------------------------------------------------------
function symbolFor(status) {
    if (!status) return ['◯', 'down'];
    if (!status.running && status.library_mode) return ['●', 'ok'];
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

    const svcNames = node.installed_services || [];
    const rowHeight = window.matchMedia('(max-height: 400px)').matches ? 56 : 64;
    const bodyHeight = body.getBoundingClientRect().height || 500;
    const headerHeight = 33;
    modal.rowsVisible = Math.max(1, Math.floor((bodyHeight - headerHeight) / rowHeight));
    const maxScroll = Math.max(0, svcNames.length - modal.rowsVisible);
    modal.scrollIndex = Math.min(modal.scrollIndex, maxScroll);

    const visible = svcNames.slice(modal.scrollIndex, modal.scrollIndex + modal.rowsVisible);

    let rowsHtml = '';
    for (const svcName of visible) {
        const svcStatus = node.services_detail?.[svcName]?.status;
        const mem = svcStatus?.memory_mb;
        const cpu = svcStatus?.cpu_percent;
        const pid = svcStatus?.pid;
        const [stateLabel, stateCls] = deriveState(svcStatus);
        const badge = serviceBadge(svcStatus);

        rowsHtml += `
            <div class="modal-svc-row" data-svc="${escapeHtml(svcName)}">
                <div class="name">
                    ${badge.emoji ? `<span class="svc-badge ${badge.cls}" title="${escapeHtml(badge.title)}">${badge.emoji}</span>` : ''}
                    ${escapeHtml(svcName)}
                </div>
                <div class="num">${fmtBytes(mem)}</div>
                <div class="num">${cpu != null ? cpu.toFixed(1) + ' %' : '-'}</div>
                <div class="num">${pid != null ? pid : '-'}</div>
                <div class="state ${stateCls}">${stateLabel}</div>
            </div>
        `;
    }

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
            openServiceDrill(svc);
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
                Update agent on <strong>${escapeHtml(node.name)}</strong>?
            </div>
            <div class="modal-confirm-subtitle">
                The agent will restart after the new code is extracted.
            </div>
            <div class="modal-confirm-buttons">
                <button class="modal-confirm-btn yes" id="modal-update-confirm-yes">Yes</button>
                <button class="modal-confirm-btn" id="modal-update-confirm-no">No</button>
            </div>
        </div>
        <div class="modal-update-node armed-ready" id="modal-update-armed" role="button" tabindex="0">
            <div class="modal-update-node-icon">↑</div>
            <div class="modal-update-node-label">Update Node</div>
        </div>
    `;

    document.getElementById('modal-update-confirm-no').addEventListener('click', () => {
        modal.state = 'idle';
        renderModalContent();
    });
    document.getElementById('modal-update-confirm-yes').addEventListener('click', () => {
        modal.state = 'idle';
        renderModalContent();
        fireAgentUpdate(node.name);
    });
    document.getElementById('modal-update-armed').addEventListener('click', () => {
        modal.state = 'idle';
        renderModalContent();
        fireAgentUpdate(node.name);
    });
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
// RENDER — /api/v1/system/status payload → DOM
// ----------------------------------------------------------------------
function renderNode(node) {
    const card = document.createElement('div');
    card.className = 'node' + (node.online ? '' : ' offline');

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

    if (!node.online) {
        list.innerHTML = `<div class="svc-empty">unreachable</div>`;
    } else if (!node.installed_services || node.installed_services.length === 0) {
        list.innerHTML = `<div class="svc-empty">no services installed</div>`;
    } else {
        const STATUS_RANK = { down: 2, warn: 1, ok: 0 };
        let searxngRolled = false;
        let searxngWorstCls = 'ok';
        let searxngWorstSym = '●';
        let searxngTotalMem = 0;
        let searxngHasMem = false;

        for (const svcName of node.installed_services) {
            if (!svcName.toLowerCase().includes('searxng')) continue;
            const svcStatus = node.services?.[svcName]?.status;
            const [sym, cls] = symbolFor(svcStatus);
            if ((STATUS_RANK[cls] ?? 0) > (STATUS_RANK[searxngWorstCls] ?? 0)) {
                searxngWorstCls = cls;
                searxngWorstSym = sym;
            }
            const mem = svcStatus?.memory_mb;
            if (mem != null) { searxngTotalMem += mem; searxngHasMem = true; }
            searxngRolled = true;
        }

        for (const svcName of node.installed_services) {
            if (svcName.toLowerCase().includes('searxng')) {
                if (searxngRolled) {
                    const badge = serviceBadge({ service_type: 'docker_compose' });
                    list.innerHTML += `
                    <div class="svc-name">${badge.emoji ? `<span class="svc-badge ${badge.cls}" title="${escapeHtml(badge.title)}">${badge.emoji}</span>` : ''}searxng</div>
                    <div class="svc-mem">${searxngHasMem ? fmtBytes(searxngTotalMem) : '-'}</div>
                    <div class="svc-stat ${searxngWorstCls}">${searxngWorstSym}</div>
                  `;
                    searxngRolled = false;
                }
                continue;
            }
            const svcStatus = node.services?.[svcName]?.status;
            const [sym, cls] = symbolFor(svcStatus);
            const mem = svcStatus?.memory_mb;
            const badge = serviceBadge(svcStatus);
            list.innerHTML += `
                    <div class="svc-name">${badge.emoji ? `<span class="svc-badge ${badge.cls}" title="${escapeHtml(badge.title)}">${badge.emoji}</span>` : ''}${escapeHtml(svcName)}</div>
                    <div class="svc-mem">${fmtBytes(mem)}</div>
                    <div class="svc-stat ${cls}">${sym}</div>
                  `;
        }
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
    grid.innerHTML = '';
    for (const node of (status.nodes || [])) {
        grid.appendChild(renderNode(node));
    }
    grid.appendChild(renderActionsCard());
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
    refreshBtn.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        try {
            await hostApi('/api/v1/cluster/refresh', { method: 'POST' });
            await refresh();
        } finally {
            refreshBtn.disabled = false;
        }
    });

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

    let degraded = 0, total = 0;
    for (const node of nodes) {
        for (const svcName of (node.installed_services || [])) {
            total++;
            const ss = node.services?.[svcName]?.status;
            const [, cls] = symbolFor(ss);
            if (cls !== 'ok') degraded++;
        }
    }

    if (degraded > 0) setLed('warn', `${degraded}/${total} services degraded`);
    else setLed('ok', `${total} services healthy`);
}

function setLed(cls, text) {
    const led = document.getElementById('led');
    led.className = 'led ' + cls;
    document.getElementById('led-text').textContent = text;
}

function setLastRefresh() {
    document.getElementById('last-refresh').textContent =
        'refreshed ' + new Date().toLocaleTimeString();
}

// ----------------------------------------------------------------------
// AGENT UPDATE — nodeName targets one node, null broadcasts to all.
// ----------------------------------------------------------------------
async function fireAgentUpdate(nodeName) {
    const isBroadcast = nodeName == null;
    const url = isBroadcast
        ? '/api/v1/system/agent-update'
        : `/api/v1/node/${encodeURIComponent(nodeName)}/agent-update`;

    const updateBtn = document.getElementById('modal-update-node');
    if (updateBtn) {
        updateBtn.classList.add('updating');
        updateBtn.style.pointerEvents = 'none';
    }

    const broadcastBtn = document.getElementById('cfg-update-nodes');
    const resultEl = document.getElementById('cfg-update-result');
    if (isBroadcast && broadcastBtn) {
        broadcastBtn.disabled = true;
        broadcastBtn.textContent = 'updating…';
        if (resultEl) { resultEl.style.display = 'none'; resultEl.textContent = ''; }
    }

    const resp = await hostApi(url, { method: 'POST' });

    if (updateBtn) {
        updateBtn.classList.remove('updating');
        updateBtn.style.pointerEvents = '';
    }

    if (isBroadcast && broadcastBtn) {
        broadcastBtn.disabled = false;
        broadcastBtn.textContent = 'update all nodes';
    }

    if (!resp) {
        if (isBroadcast && resultEl) {
            resultEl.className = 'update-result err';
            resultEl.textContent = 'Update failed: no response from Lodestar.';
            resultEl.style.display = '';
        } else {
            setModalFlash('Observatory update failed: no response from Lodestar.', 'err');
        }
        return;
    }

    if (isBroadcast) {
        const results = resp.results || [];
        const lines = results.map(r =>
            r.ok ? `✓ ${r.node}: ${r.message || 'ok'}` : `✗ ${r.node}: ${r.error || r.message || 'failed'}`
        );
        const allOk = results.every(r => r.ok);
        if (resultEl) {
            resultEl.className = `update-result ${allOk ? 'ok' : 'err'}`;
            resultEl.textContent = lines.join('\n') || (resp.error ? `Error: ${resp.error}` : 'No nodes updated.');
            resultEl.style.display = '';
        }
    } else {
        if (resp.ok) {
            setModalFlash(`Update sent to ${nodeName} — agent will restart shortly.`, 'ok');
        } else {
            setModalFlash(`Update failed on ${nodeName}: ${resp.error || resp.message || 'unknown error'}`, 'err');
        }
    }
}

// ----------------------------------------------------------------------
// SETTINGS MODAL — now just the broadcast agent-update action. Token lives
// in the shell's 🔑 modal; same-origin means no URL field.
// ----------------------------------------------------------------------
function initSettings() {
    document.getElementById('settings-close').addEventListener('click', closeSettingsModal);

    document.getElementById('settings-backdrop').addEventListener('click', (e) => {
        if (e.target.id === 'settings-backdrop') closeSettingsModal();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isSettingsOpen()) closeSettingsModal();
    });

    document.getElementById('cfg-update-nodes').addEventListener('click', () => {
        fireAgentUpdate(null);
    });
}

function openSettingsModal() {
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
    if (_upgOpenRun && status && !status.busy) {
        const fresh = (status.runs || []).find(r => r.id === _upgOpenRun);
        if (fresh) renderRunDetail(fresh);
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
        + rows.map(r => {
            const state = r.behind === true ? 'behind' : r.behind === false ? 'current' : 'unknown';
            const why = r.latest_error ? ` title="${escapeHtml(r.latest_error)}"` : '';
            return `<tr class="${state}"><td>${upgCell(r.node)}</td><td>${upgCell(r.service)}</td>`
                 + `<td class="mono">${upgCell(r.version)}</td><td class="mono"${why}>${upgCell(r.latest)}</td>`
                 + `<td>${r.behind === true ? '<span class="upg-state warn">↑ behind</span>'
                        : r.behind === false ? '<span class="upg-state ok">current</span>'
                        : '<span class="upg-state wait">?</span>'}</td></tr>`;
        }).join('');
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
    const card = document.getElementById('upg-plan-card');
    card.hidden = false;
    const order = plan.order || [];
    const current = new Set(plan.current || []);
    const lines = order.map(key => {
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
        return `<tr class="${state}"><td>${upgCell(node)}</td><td>${upgCell(svc)}</td><td>${upgBadge(state)}</td><td>${what}</td></tr>`;
    });
    const moving = order.filter(k => !current.has(k)).length;
    document.getElementById('upg-plan-sum').textContent =
        `${moving} would move, ${current.size} current` + (plan.skipped_offline?.length ? ` · offline: ${plan.skipped_offline.join(', ')}` : '');
    document.getElementById('upg-plan-body').innerHTML =
        `<div class="upg-table-wrap"><table class="upg-table"><tbody><tr><th>node</th><th>service</th><th></th><th>in run order</th></tr>${lines.join('')}</tbody></table></div>`
        + (plan.held?.length ? `<div class="upg-note">held: ${escapeHtml(plan.held.join(' · '))}</div>` : '')
        + (plan.error ? `<div class="upg-note bad">${escapeHtml(plan.error)}</div>` : '');
}

async function startUpgrades() {
    const n = (_upgBehind?.behind || []).length;
    const list = (_upgBehind?.behind || []).slice(0, 12).join('\n') + (n > 12 ? `\n… and ${n - 12} more` : '');
    if (!confirm(`Upgrade ${n} service(s) that are behind?\n\n${list}\n\nEach is backed up to the stash first, then pip, an import check, a restart and health; a failure rolls that one back and the run goes on. Stores first, each node's Observatory last, Lodestar last of all.`)) return;
    const go = document.getElementById('upg-go');
    go.disabled = true;
    upgFlash('starting the run…', 'info');
    const out = await hostApi('/api/v1/upgrade', { method: 'POST', json: { only_behind: true, backup: true, reason: 'from the viewer' } });
    if (!out || !out.ok) {
        upgFlash(out?.error || 'Lodestar would not start it (is a run already going?)', 'err');
        go.disabled = false;
        return;
    }
    upgFlash(`run ${out.run.id} started: ${out.run.steps.length} step(s)`, 'ok');
    _upgOpenRun = out.run.id;
    await refreshUpgrades();
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
            + (now ? ` · now ${escapeHtml(now.node)}/${escapeHtml(now.service)}` : '') + ` · ${escapeHtml(r.reason || '')}</div>`;
        if (!_upgOpenRun) _upgOpenRun = r.id;
        if (_upgOpenRun === r.id) renderRunDetail(r);
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

async function openRun(id) {
    _upgOpenRun = id;
    const out = await hostApi(`/api/v1/upgrade/${encodeURIComponent(id)}`);
    if (out && out.run) renderRunDetail(out.run);
    if (_upgStatus) renderRuns(_upgStatus);
}

function renderRunDetail(run) {
    const el = document.getElementById('upg-run-detail');
    const rows = (run.steps || []).map(s => {
        const ver = s.before && s.after && s.service
            ? `${upgCell(Object.values(s.before)[0])} → ${upgCell(Object.values(s.after)[0])}` : upgCell(s.version);
        const note = s.error || s.held || s.skipped || (s.state === 'current' ? `${upgCell(s.current)} already the newest` : '')
                   || (s.job ? `job ${s.job}` : '');
        return `<tr class="${s.state}"><td>${upgCell(s.node)}</td><td>${upgCell(s.service)}</td><td>${upgBadge(s.state)}</td>`
             + `<td class="mono">${ver}</td><td>${escapeHtml(String(note || ''))}</td></tr>`;
    });
    el.innerHTML = `<div class="upg-detail-head"><b>${escapeHtml(run.id)}</b> ${upgBadge(run.state)}`
        + (run.error ? ` <span class="upg-note bad">${escapeHtml(run.error)}</span>` : '')
        + ` <button class="upg-link" id="upg-detail-close">close</button></div>`
        + `<div class="upg-table-wrap"><table class="upg-table"><tbody><tr><th>node</th><th>service</th><th></th><th>version</th><th></th></tr>${rows.join('')}</tbody></table></div>`;
    document.getElementById('upg-detail-close').addEventListener('click', () => {
        _upgOpenRun = null; el.innerHTML = ''; if (_upgStatus) renderRuns(_upgStatus);
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
    // The shell switches views; the first look at this one fetches.
    document.querySelectorAll('.tab[data-tab="upgrades"]').forEach(b => b.addEventListener('click', () => refreshUpgrades()));
}

// ----------------------------------------------------------------------
// MAIN
// ----------------------------------------------------------------------
initSettings();
initModal();
initUpgrades();
refresh();
startPolling(POLL_INTERVAL);
