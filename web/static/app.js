/**
 * Polymarket Trading Bot - Card-Based Dashboard Controller
 * Manages trading modes: Aggressive, Contrarian, Volume Weighted
 */

// ============================================
// STATE MANAGEMENT
// ============================================

const modes = {
    'aggressive': {
        status: 'stopped',  // stopped | paper | live
        running: false,
        config: {},
        liveData: {},
        timeRemaining: 0,
        countdownInterval: null
    },
    'contrarian': {
        status: 'stopped',
        running: false,
        config: {},
        liveData: {},
        timeRemaining: 0,
        countdownInterval: null
    },
    'volume-weighted': {
        status: 'stopped',
        running: false,
        config: {},
        liveData: {},
        timeRemaining: 0,
        countdownInterval: null
    }
};

// WebSocket
let ws = null;
let reconnectTimeout = null;

// ============================================
// INITIALIZATION
// ============================================

/**
 * Clear all position displays on page load to prevent stale data.
 * Called before WebSocket connects to ensure clean slate.
 */
function clearAllPositionDisplays() {
    ['aggressive', 'contrarian', 'volume-weighted'].forEach(mode => {
        // Position quantities and prices
        setElementText(`${mode}-up-qty`, '--');
        setElementText(`${mode}-down-qty`, '--');
        setElementText(`${mode}-up-avg`, '--');
        setElementText(`${mode}-down-avg`, '--');
        setElementText(`${mode}-up-cost`, '--');
        setElementText(`${mode}-down-cost`, '--');

        // Metrics
        setElementText(`${mode}-pairs`, '--');
        setElementText(`${mode}-avg-pair-cost`, '--');
        setElementText(`${mode}-balance`, '--');

        // PNL displays
        const lockedEl = document.getElementById(`${mode}-locked-pnl`);
        if (lockedEl) {
            lockedEl.textContent = '--';
            lockedEl.classList.remove('profit', 'loss');
        }

        const rangeEl = document.getElementById(`${mode}-pnl-range`);
        if (rangeEl) {
            rangeEl.textContent = '--';
            rangeEl.classList.remove('profit', 'loss');
        }

        const realizedEl = document.getElementById(`${mode}-realized-pnl`);
        if (realizedEl) {
            realizedEl.textContent = '--';
            realizedEl.classList.remove('profit', 'loss');
        }

        // Time display
        const timeEl = document.getElementById(`${mode}-time`);
        if (timeEl) timeEl.textContent = '--:--';
    });
}

function init() {
    // Clear stale position data first (prevents old data showing after refresh)
    clearAllPositionDisplays();

    // Set default datetimes
    setDefaultDatetimes();

    // Setup config toggles
    ['aggressive', 'contrarian', 'volume-weighted'].forEach(mode => {
        const toggleBtn = document.getElementById(`toggle-${mode}`);
        const configContent = document.getElementById(`config-${mode}`);

        toggleBtn.addEventListener('click', () => {
            toggleBtn.classList.toggle('expanded');
            configContent.classList.toggle('expanded');
        });
    });

    // Setup action buttons
    setupModeButtons('aggressive');
    setupModeButtons('contrarian');
    setupModeButtons('volume-weighted');

    // Connect WebSocket
    connectWebSocket();

    // Initial status fetch
    fetchStatus();

    // Periodic status polling as fallback (every 2 seconds)
    setInterval(fetchStatus, 2000);
}

function setDefaultDatetimes() {
    const now = new Date();
    const start = new Date(now.getTime() + 5 * 60 * 1000);
    const end = new Date(now.getTime() + 35 * 60 * 1000);
    const startStr = formatDatetimeLocal(start);
    const endStr = formatDatetimeLocal(end);

    ['aggressive', 'contrarian', 'volume-weighted'].forEach(mode => {
        const startEl = document.getElementById(`${mode}-start`);
        const endEl = document.getElementById(`${mode}-end`);
        if (startEl) startEl.value = startStr;
        if (endEl) endEl.value = endStr;
    });
}

function formatDatetimeLocal(date) {
    const pad = (n) => n.toString().padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setupModeButtons(mode) {
    const startBtn = document.getElementById(`btn-start-${mode}`);
    const stopBtn = document.getElementById(`btn-stop-${mode}`);
    const nukeBtn = document.getElementById(`btn-nuke-${mode}`);

    startBtn.addEventListener('click', () => handleStart(mode));
    stopBtn.addEventListener('click', () => handleStop(mode));
    nukeBtn.addEventListener('click', () => handleNuke(mode));
}

// ============================================
// WEBSOCKET
// ============================================

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onopen = () => {
        updateWsStatus(true);
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        routeMessage(data);
    };

    ws.onclose = () => {
        updateWsStatus(false);
        if (!reconnectTimeout) {
            reconnectTimeout = setTimeout(() => {
                reconnectTimeout = null;
                connectWebSocket();
            }, 3000);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };

    // Ping every 30 seconds
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send('ping');
        }
    }, 30000);
}

function updateWsStatus(connected) {
    const statusEl = document.getElementById('ws-status');
    const textEl = statusEl.querySelector('.ws-text');

    if (connected) {
        statusEl.className = 'ws-status connected';
        textEl.textContent = 'Connected';
    } else {
        statusEl.className = 'ws-status disconnected';
        textEl.textContent = 'Disconnected';
    }
}

function routeMessage(data) {
    // DEBUG: Write to visible debug element
    const debugEl = document.getElementById('debug-ws');
    if (debugEl) {
        const now = new Date().toLocaleTimeString();
        const pos = data.position || {};
        debugEl.innerHTML = `[${now}] type=${data.type} strategy=${data.strategy || 'N/A'} up=${pos.up_qty || 0} down=${pos.down_qty || 0}`;
    }

    if (data.type === 'status') {
        // Status update for all modes
        if (data.aggressive) updateModeStatus('aggressive', data.aggressive);
        if (data.contrarian) updateModeStatus('contrarian', data.contrarian);
        if (data.volume_weighted) updateModeStatus('volume-weighted', data.volume_weighted);
    } else if (data.type === 'trading_update') {
        // Route trading update to specific mode's card
        // Convert underscore to hyphen for frontend mode names (volume_weighted -> volume-weighted)
        const strategy = (data.strategy || 'aggressive').replace('_', '-');
        console.log('[WS] Routing trading_update to:', strategy, 'Position:', data.position);
        updateLiveData(strategy, data);
    }
}

async function fetchStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();

        if (data.aggressive) updateModeStatus('aggressive', data.aggressive);
        if (data.contrarian) updateModeStatus('contrarian', data.contrarian);
        if (data.volume_weighted) updateModeStatus('volume-weighted', data.volume_weighted);
    } catch (error) {
        console.error('Failed to fetch status:', error);
    }
}

// ============================================
// MODE STATUS UPDATES
// ============================================

function updateModeStatus(modeName, status) {
    const mode = modes[modeName];
    mode.running = status.running;
    mode.config = status.config || {};

    // Determine status type
    let statusType = 'stopped';
    let statusText = 'Stopped';

    if (status.running) {
        const isPaper = status.config?.mode === 'paper';
        statusType = isPaper ? 'paper' : 'live';
        statusText = isPaper ? 'Paper' : 'LIVE';

        if (status.stopping) {
            statusText = 'Stopping...';
        } else if (status.waiting_until) {
            statusText += ' (Waiting)';
        } else if (status.trading_started) {
            statusText += ' Trading';
        }
    } else if (status.error) {
        statusText = 'Error';
    } else if (status.completed) {
        statusText = 'Completed';
    } else if (status.emergency_stopped) {
        statusText = 'Emergency';
    }

    mode.status = statusType;

    // Update badge (pass full status for tooltip when running - includes end_datetime)
    updateStatusBadge(modeName, statusType, statusText, status.running ? status : null);

    // Update buttons
    updateModeButtons(modeName, status.running);

    // Update live data if available
    if (status.latest_trading_update) {
        updateLiveData(modeName, status.latest_trading_update);
    }

    // Show error if any
    if (status.error) {
        showError(modeName, status.error);
    }
}

function updateStatusBadge(modeName, statusType, statusText, status = null) {
    const badge = document.getElementById(`badge-${modeName}`);
    const dot = badge.querySelector('.status-dot');
    const text = badge.querySelector('.status-text');

    badge.className = `status-badge ${statusType}`;
    text.textContent = statusText;

    // Set tooltip with config params and end time when running
    if (status && statusType !== 'stopped') {
        const tooltip = formatConfigTooltip(modeName, status);
        badge.setAttribute('title', tooltip);
    } else {
        badge.removeAttribute('title');
    }
}

function formatConfigTooltip(modeName, status) {
    const lines = [];
    const config = status.config || {};

    // Add scheduled start time if waiting
    if (status.waiting_until) {
        const startDt = new Date(status.waiting_until);
        lines.push(`Starts: ${startDt.toLocaleTimeString()}`);
    }

    // Add end time at top if available (check status first, then config)
    const endTime = status.end_datetime || (status.config && status.config.end_datetime);
    if (endTime) {
        const endDt = new Date(endTime);
        lines.push(`Ends: ${endDt.toLocaleTimeString()}`);
    }

    if (status.waiting_until || endTime) {
        lines.push('---');
    }

    if (modeName === 'aggressive') {
        lines.push(`Base Size: ${config.base_size || '--'}`);
        lines.push(`Skip High: $${config.high_entry_threshold || '--'}`);
        lines.push(`Lookback: ${config.lookback_ms || '--'}ms`);
        lines.push(`Time Stop: ${config.time_stop_seconds || '--'}s`);
        lines.push(`Cycling: ${config.use_cycling ? 'Yes' : 'No'}`);
        lines.push(`Max Entries/Mkt: ${config.max_entries_per_market || 'unlimited'}`);
    } else if (modeName === 'contrarian') {
        lines.push(`Shares: ${config.shares_per_trade || '--'}`);
        lines.push(`Pullback: ${config.pullback_threshold || '--'}`);
        lines.push(`Retracement: ${config.retracement_min || '--'}`);
        lines.push(`Entry Min: $${config.entry_price_min || '--'}`);
        lines.push(`Delay: ${config.min_delay_seconds || '--'}s`);
        lines.push(`Z Thresh: ${config.z_threshold || '--'}`);
    } else if (modeName === 'volume-weighted') {
        lines.push(`Target: ${config.target_shares || '--'}`);
        lines.push(`Cheap: $${config.vw_cheap_threshold || '--'}`);
        lines.push(`Imbal %: ${config.vw_imbalance_pct || '--'}`);
        lines.push(`Hedge Trig: ${config.vw_hedge_trigger_pct || '--'}`);
        lines.push(`Max Hedge: $${config.vw_max_hedge_price || '--'}`);
    }

    lines.push(`Balance: $${config.starting_balance || '--'}`);
    return lines.join('\n');
}

function updateModeButtons(modeName, running) {
    const startBtn = document.getElementById(`btn-start-${modeName}`);
    const stopBtn = document.getElementById(`btn-stop-${modeName}`);
    const nukeBtn = document.getElementById(`btn-nuke-${modeName}`);

    startBtn.disabled = running;
    stopBtn.disabled = !running;
    nukeBtn.disabled = !running;

    // Disable config inputs when running
    const configContent = document.getElementById(`config-${modeName}`);
    const inputs = configContent.querySelectorAll('input');
    inputs.forEach(input => {
        // Keep mode toggle enabled
        if (input.name.includes('_mode')) return;
        input.disabled = running;
    });
}

// ============================================
// LIVE DATA UPDATES
// ============================================

function updateLiveData(modeName, data) {
    const mode = modes[modeName];
    mode.liveData = data;

    // Debug: log received data
    console.log(`[${modeName}] Live data:`, data);

    // Market
    const marketEl = document.getElementById(`${modeName}-market`);
    if (marketEl) marketEl.textContent = data.market_slug || '--';

    // Time remaining
    if (data.time_remaining_secs !== undefined && data.time_remaining_secs > 0) {
        mode.timeRemaining = data.time_remaining_secs;
        updateTimeDisplay(modeName);
        startCountdown(modeName);
    } else {
        const timeEl = document.getElementById(`${modeName}-time`);
        if (timeEl) timeEl.textContent = data.time_remaining || '--:--';
    }

    // Position
    const pos = data.position || {};
    const upQty = pos.up_qty || 0;
    const downQty = pos.down_qty || 0;
    const upAvg = pos.up_avg_price || 0;
    const downAvg = pos.down_avg_price || 0;

    setElementText(`${modeName}-up-qty`, Math.round(upQty));
    setElementText(`${modeName}-up-avg`, upAvg > 0 ? `$${upAvg.toFixed(3)}` : '--');
    setElementText(`${modeName}-down-qty`, Math.round(downQty));
    setElementText(`${modeName}-down-avg`, downAvg > 0 ? `$${downAvg.toFixed(3)}` : '--');

    // Position costs (qty x avg price)
    const upCost = pos.up_cost || (upQty * upAvg);
    const downCost = pos.down_cost || (downQty * downAvg);
    setElementText(`${modeName}-up-cost`, upCost > 0 ? `$${upCost.toFixed(2)}` : '--');
    setElementText(`${modeName}-down-cost`, downCost > 0 ? `$${downCost.toFixed(2)}` : '--');

    // Current market prices (UP/DOWN mid-prices)
    const upPrice = pos.up_current || 0;
    const downPrice = pos.down_current || 0;
    setElementText(`${modeName}-up-price`, upPrice > 0 ? `UP $${upPrice.toFixed(3)}` : 'UP $--');
    setElementText(`${modeName}-down-price`, downPrice > 0 ? `DOWN $${downPrice.toFixed(3)}` : 'DOWN $--');

    // Metrics
    const metrics = data.metrics || {};
    setElementText(`${modeName}-pairs`, metrics.pairs || '--');

    // Average pair cost (up_avg + down_avg)
    const avgPairCost = upAvg + downAvg;
    setElementText(`${modeName}-avg-pair-cost`, avgPairCost > 0 ? `$${avgPairCost.toFixed(3)}` : '--');

    setElementText(`${modeName}-balance`, metrics.balance ? `$${metrics.balance.toFixed(2)}` : '--');

    // Locked PNL (guaranteed from hedged pairs)
    const lockedPnl = metrics.locked_profit || 0;
    const lockedEl = document.getElementById(`${modeName}-locked-pnl`);
    if (lockedEl) {
        lockedEl.textContent = `$${lockedPnl.toFixed(2)}`;
        lockedEl.classList.remove('profit', 'loss');
        lockedEl.classList.add(lockedPnl >= 0 ? 'profit' : 'loss');
    }

    // PNL Range (for unhedged positions)
    const rangeEl = document.getElementById(`${modeName}-pnl-range`);
    if (rangeEl) {
        const pnlMin = metrics.pnl_min || 0;
        const pnlMax = metrics.pnl_max || 0;
        if (pnlMin !== pnlMax && pnlMax !== 0) {
            rangeEl.textContent = `$${pnlMin.toFixed(2)} to $${pnlMax.toFixed(2)}`;
            rangeEl.classList.remove('profit', 'loss');
            rangeEl.classList.add(pnlMin >= 0 ? 'profit' : 'loss');
        } else {
            rangeEl.textContent = '--';
            rangeEl.classList.remove('profit', 'loss');
        }
    }

    // Session P&L (from auto-merged cycles, or resolved markets as fallback)
    const realizedEl = document.getElementById(`${modeName}-realized-pnl`);
    if (realizedEl) {
        // Prefer session_pnl (actual merged cycle profit) over realized_pnl (resolved markets)
        const sessionPnl = metrics.session_pnl || 0;
        const realizedPnl = metrics.realized_pnl || 0;
        const displayPnl = sessionPnl !== 0 ? sessionPnl : realizedPnl;
        const mergeCount = metrics.session_merge_count || 0;

        const sign = displayPnl >= 0 ? '+' : '';
        if (mergeCount > 0) {
            realizedEl.textContent = `${sign}$${displayPnl.toFixed(2)} (${mergeCount} cycles)`;
        } else {
            realizedEl.textContent = `${sign}$${displayPnl.toFixed(2)}`;
        }
        realizedEl.classList.remove('profit', 'loss');
        realizedEl.classList.add(displayPnl >= 0 ? 'profit' : 'loss');
    }
}

function setElementText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function updateTimeDisplay(modeName) {
    const mode = modes[modeName];
    const timeEl = document.getElementById(`${modeName}-time`);
    if (timeEl) {
        timeEl.textContent = formatTimeRemaining(mode.timeRemaining);
    }
}

function formatTimeRemaining(seconds) {
    if (seconds <= 0) return 'EXPIRED';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

// ============================================
// COUNTDOWN TIMERS
// ============================================

function startCountdown(modeName) {
    const mode = modes[modeName];

    // Store the timestamp when we received this time
    mode.timeReceivedAt = Date.now();
    mode.serverTimeRemaining = mode.timeRemaining;

    if (mode.countdownInterval) {
        clearInterval(mode.countdownInterval);
    }

    // Calculate time based on server value minus elapsed time
    // This prevents drift between strategies on the same market
    mode.countdownInterval = setInterval(() => {
        const elapsed = (Date.now() - mode.timeReceivedAt) / 1000;
        mode.timeRemaining = Math.max(0, mode.serverTimeRemaining - elapsed);
        updateTimeDisplay(modeName);

        if (mode.timeRemaining <= 0) {
            stopCountdown(modeName);
        }
    }, 1000);
}

function stopCountdown(modeName) {
    const mode = modes[modeName];
    if (mode.countdownInterval) {
        clearInterval(mode.countdownInterval);
        mode.countdownInterval = null;
    }
}

// ============================================
// CONFIG COLLECTION
// ============================================

function getAggressiveConfig() {
    return {
        mode: document.querySelector('input[name="aggressive_mode"]:checked').value,
        market: 'btc-15m',
        start_datetime: document.getElementById('aggressive-start').value,
        end_datetime: document.getElementById('aggressive-end').value,
        starting_balance: parseFloat(document.getElementById('aggressive-balance-input').value),
        // AGGRESSIVE specific parameters - EWMA_1000 + TS30 winner (Feb 3, 2026)
        lookback_ms: parseInt(document.getElementById('aggressive-lookback').value),
        time_stop_seconds: parseFloat(document.getElementById('aggressive-time-stop').value),
        // z_lo/z_hi removed - EWMA config doesn't use z-score filter (Feb 3, 2026)
        base_size: parseInt(document.getElementById('aggressive-base-size').value),
        high_entry_threshold: parseFloat(document.getElementById('aggressive-high-entry').value),
        use_cycling: document.getElementById('aggressive-cycling').checked,
        max_daily_loss: parseFloat(document.getElementById('aggressive-max-loss').value) || 0,
        max_entries_per_market: parseInt(document.getElementById('aggressive-max-entries').value) || 0
    };
}

function getContrarianConfig() {
    return {
        mode: document.querySelector('input[name="contrarian_mode"]:checked').value,
        market: 'btc-15m',
        start_datetime: document.getElementById('contrarian-start').value,
        end_datetime: document.getElementById('contrarian-end').value,
        starting_balance: parseFloat(document.getElementById('contrarian-balance-input').value),
        // CONTRARIAN specific parameters
        pullback_threshold: parseFloat(document.getElementById('contrarian-pullback').value),
        retracement_min: parseFloat(document.getElementById('contrarian-retracement').value),
        entry_price_min: parseFloat(document.getElementById('contrarian-entry-min').value),
        min_delay_seconds: parseInt(document.getElementById('contrarian-delay').value),
        z_threshold: parseFloat(document.getElementById('contrarian-z-thresh').value),
        shares_per_trade: parseInt(document.getElementById('contrarian-shares').value)
    };
}

function getVolumeWeightedConfig() {
    return {
        mode: document.querySelector('input[name="volume_weighted_mode"]:checked').value,
        market: 'btc-15m',
        start_datetime: document.getElementById('volume-weighted-start').value,
        end_datetime: document.getElementById('volume-weighted-end').value,
        starting_balance: parseFloat(document.getElementById('volume-weighted-balance-input').value),
        // VW specific parameters
        vw_imbalance_pct: parseFloat(document.getElementById('volume-weighted-imbalance').value),
        vw_cheap_threshold: parseFloat(document.getElementById('volume-weighted-cheap').value),
        vw_hedge_trigger_pct: parseFloat(document.getElementById('volume-weighted-hedge-trigger').value),
        vw_max_hedge_price: parseFloat(document.getElementById('volume-weighted-max-hedge').value),
        target_shares: parseInt(document.getElementById('volume-weighted-target').value),
        max_daily_loss: parseFloat(document.getElementById('volume-weighted-max-loss').value) || 0
    };
}

// ============================================
// ACTION HANDLERS
// ============================================

async function handleStart(modeName) {
    hideError(modeName);

    // Get config based on mode
    let config;
    let endpoint;

    if (modeName === 'aggressive') {
        config = getAggressiveConfig();
        endpoint = '/api/start/aggressive';
    } else if (modeName === 'contrarian') {
        config = getContrarianConfig();
        endpoint = '/api/start/contrarian';
    } else if (modeName === 'volume-weighted') {
        config = getVolumeWeightedConfig();
        endpoint = '/api/start/volume_weighted';
    }

    // Validation
    const startDt = new Date(config.start_datetime);
    const endDt = new Date(config.end_datetime);

    if (endDt <= startDt) {
        showError(modeName, 'End time must be after start time');
        return;
    }

    // Confirm live trading
    if (config.mode === 'live') {
        if (!confirm(`LIVE TRADING - Real money at risk!\n\nProceed with ${modeName.toUpperCase()}?`)) {
            return;
        }
    }

    const startBtn = document.getElementById(`btn-start-${modeName}`);

    try {
        startBtn.disabled = true;
        startBtn.textContent = 'STARTING...';

        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });

        const data = await res.json();

        if (data.error) {
            showError(modeName, data.error);
            startBtn.disabled = false;
            startBtn.textContent = 'START';
        }
        // Status will be updated via WebSocket
    } catch (error) {
        showError(modeName, 'Failed to start: ' + error.message);
        startBtn.disabled = false;
        startBtn.textContent = 'START';
    }
}

async function handleStop(modeName) {
    if (!confirm(`Stop ${modeName.toUpperCase()} mode?`)) {
        return;
    }

    const stopBtn = document.getElementById(`btn-stop-${modeName}`);

    try {
        stopBtn.disabled = true;
        stopBtn.textContent = 'STOPPING...';

        // Map mode name to strategy for API (hyphen to underscore)
        let strategy = modeName;
        if (modeName === 'volume-weighted') strategy = 'volume_weighted';
        await fetch(`/api/stop/${strategy}`, { method: 'POST' });
    } catch (error) {
        showError(modeName, 'Failed to stop: ' + error.message);
    } finally {
        stopBtn.textContent = 'STOP';
    }
}

async function handleNuke(modeName) {
    if (!confirm(`EMERGENCY SELL - ${modeName.toUpperCase()}\n\nSell ALL positions immediately?`)) {
        return;
    }

    if (!confirm('FINAL CONFIRMATION: Sell everything and stop?')) {
        return;
    }

    const nukeBtn = document.getElementById(`btn-nuke-${modeName}`);
    const stopBtn = document.getElementById(`btn-stop-${modeName}`);

    try {
        nukeBtn.disabled = true;
        stopBtn.disabled = true;
        nukeBtn.textContent = 'NUKING...';

        // Map mode name to strategy for API (hyphen to underscore)
        let strategy = modeName;
        if (modeName === 'volume-weighted') strategy = 'volume_weighted';
        const res = await fetch(`/api/emergency-stop/${strategy}`, { method: 'POST' });
        const data = await res.json();

        if (data.positions_closed > 0) {
            const pnlSign = data.realized_pnl >= 0 ? '+' : '';
            alert(
                `Emergency Sell Complete!\n\n` +
                `Positions: ${data.positions_closed}\n` +
                `P&L: ${pnlSign}$${data.realized_pnl.toFixed(2)}`
            );
        } else {
            alert('Emergency stop complete. No positions to sell.');
        }
    } catch (error) {
        showError(modeName, 'Emergency stop failed: ' + error.message);
    } finally {
        nukeBtn.textContent = 'NUKE';
    }
}

// ============================================
// ERROR HANDLING
// ============================================

function showError(modeName, message) {
    const errorEl = document.getElementById(`error-${modeName}`);
    if (errorEl) {
        errorEl.textContent = message;
        errorEl.classList.remove('hidden');
    }
}

function hideError(modeName) {
    const errorEl = document.getElementById(`error-${modeName}`);
    if (errorEl) {
        errorEl.classList.add('hidden');
    }
}

// ============================================
// INITIALIZE ON LOAD
// ============================================

document.addEventListener('DOMContentLoaded', init);
