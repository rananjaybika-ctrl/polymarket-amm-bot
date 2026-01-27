# Handover: January 26, 2026 — Binance Safety Gate & Live Trading Prep

## Session Summary

Implemented critical safety mechanism for live trading: **Binance connection health check + entry blocking when data is stale**. This prevents trading on stale price data if Binance WebSocket disconnects or stops receiving updates.

**Main Goal:** Take AGGRESSIVE mode live with 5 shares first, then scale to full size after fund transfer.

---

## Changes Implemented

### Binance Safety Gate (`scripts/run_paper_bot.py`)

#### 1. `_is_binance_healthy()` Method (line 1512)

```python
def _is_binance_healthy(self, max_stale_seconds: float = 5.0) -> bool:
    """Check if Binance connection is healthy and data is fresh."""
    if not self._binance_client:
        return False
    if not self._binance_client.is_connected:
        return False
    if not self._binance_client._price_history:
        return False

    last_update = self._binance_client._price_history[-1].timestamp
    age_seconds = (datetime.now(timezone.utc) - last_update).total_seconds()
    return age_seconds <= max_stale_seconds
```

**Checks 4 conditions:**
| Check | What it catches |
|-------|-----------------|
| `_binance_client` exists | Client never initialized |
| `is_connected == True` | WebSocket disconnected |
| `_price_history` not empty | Connected but no data yet |
| `age <= 5 seconds` | Connected but data stopped (stale) |

#### 2. Hard Fail on Startup for LIVE Mode (line 1491)

```python
if self._binance_client.current_price <= 0:
    if self.trading_mode == "live":
        raise RuntimeError("Binance price initialization failed - cannot proceed in LIVE mode")
    else:
        logger.warning("Could not get initial Binance price, continuing in paper mode")
```

#### 3. Per-Strategy Safety Gates

| Strategy | Location | Behavior |
|----------|----------|----------|
| **AGGRESSIVE** | line 4913 | Blocks NEW entries if unhealthy; **hedging always allowed** |
| **SPREAD_CAPTURE** | line 4747 | Sets `binance_price=None`; hedging continues |
| **CONTRARIAN** | line 5208 | Sets `binance_price=None`; strategy naturally skips new entries |

**Key Design Decision:** Safety gates only block **new entries**, never hedging. If we have a position that needs hedging (`first_fill_side is not None`), the cycle continues.

---

## Risk Mitigation Summary

| Before | After |
|--------|-------|
| Bot trades on stale Binance data | Bot blocks entries until fresh data |
| Silent failure on disconnect | Clear warning logs every 30s |
| LIVE mode starts without price ref | LIVE mode fails fast with clear error |
| Hedge blocked during disconnect | Hedge always allowed (uses locked target) |

---

## Live Trading Readiness Checklist

### AGGRESSIVE (Path 1) — Primary Focus

- [x] Direction accuracy validated (68-72% across 4 test periods)
- [x] Profitable in ALL periods (IS, OOS3, OOS4, combined)
- [x] Z-zone filter validated (0 < z < 1.5)
- [x] Time-stop logic validated (180s, +33% vs price-stop)
- [x] **Binance Safety Gate implemented** ← TODAY
- [x] WebSocket fills + REST backup working
- [x] Exponential backoff reconnection (1s → 60s)
- [ ] **Paper trade with live orderbook** (in progress)
- [ ] **Start LIVE with 5 shares**
- [ ] Scale to 50 shares after verification

### CONTRARIAN (Path 2) — Secondary

- [x] Win rate (43.4%) above breakeven (30%)
- [x] Cross-validated on IS + OOS3+4 (132h total)
- [x] Improved filters (retrace >= 0.30, price >= $0.20)
- [x] Binance Safety Gate implemented
- [ ] $0.30 fill rate verification
- [ ] Fund transfer for 2500sh trades ($750/trade)

---

## Go-Live Plan: AGGRESSIVE @ 5 Shares

### Phase 1: Micro-Live Test (5 shares)

**Purpose:** Verify execution path works with real money, minimal risk.

```bash
# SSH to server
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221

# Start bot in LIVE mode with 5 shares
python scripts/run_paper_bot.py \
    --mode live \
    --accum-mode aggressive \
    --base-size 5 \
    --max-position 20 \
    --strategy-name "AGGRESSIVE_LIVE_TEST"
```

**Expected behavior:**
- ~3-5 trades/hour
- ~$1.67/hr profit (@5sh, scaled from $16.72/hr @50sh)
- Entry: passive on winner side after spike
- Exit: 180s time-stop or passive fill

**Success criteria:**
- [ ] First fill executes correctly
- [ ] Hedge fills after entry
- [ ] Time-stop triggers if hedge doesn't fill
- [ ] Position syncs correctly with REST backup
- [ ] No errors in logs for 2+ hours

### Phase 2: Scale to Full Size (50 shares)

**After Phase 1 success:**
1. Transfer funds to Polymarket wallet
2. Increase `--base-size 50 --max-position 200`
3. Monitor for 1 market cycle (~15 min)
4. Expected: ~$16.72/hr

---

## Fund Transfer Requirements

### Current Wallet Balance
```
Wallet: 0xeCf99c5f646dEe86B4Bca1C33F013a8ACe6c0dbB
```

### AGGRESSIVE (50 shares)
- Entry cost: ~$25-30 per trade (50sh @ $0.50-0.60)
- Hedge cost: ~$20-25 per trade (50sh @ $0.40-0.50)
- **Minimum:** $100 (handles 2 concurrent cycles)
- **Recommended:** $200-300 (buffer for imbalance)

### CONTRARIAN (2500 shares) — Future
- Entry cost: ~$750 per trade (2500sh @ $0.30)
- **Minimum:** $1,500 (2 trades buffer)
- **Recommended:** $3,000+ (for multiple windows)

---

## Server Status

- **IP:** 54.170.244.221 (Ireland AWS)
- **Service:** `polymarket-bot.service`
- **Status:** Running (web dashboard only, no active trading)
- **Logs:** `~/polymarket-amm-bot/logs/server.log`

### Useful Commands

```bash
# Check service status
sudo systemctl status polymarket-bot

# View recent logs
tail -100 ~/polymarket-amm-bot/logs/server.log

# Check Binance connection
grep -i binance ~/polymarket-amm-bot/logs/server.log | tail -20

# Restart service
sudo systemctl restart polymarket-bot
```

---

## Files Modified This Session

| Action | File | Details |
|--------|------|---------|
| MODIFIED | `scripts/run_paper_bot.py` | Added `_is_binance_healthy()`, safety gates for all modes |
| CREATED | `research/archive/handovers/HANDOVER_JAN26.md` | This file |

---

## Key Metrics Reference

### AGGRESSIVE Performance (Combined OOS, 50.6h)

| Metric | Value |
|--------|-------|
| Direction Accuracy | 69.0% |
| $/hr @50sh | $13.52-16.72 |
| Trades/hr | 3-5 |
| Win Rate | ~49% |
| Avg Entry | $0.52 |
| Avg Hedge | $0.45 |

### Execution Timing

| Step | Latency |
|------|---------|
| Binance spike detect | <1ms |
| Order signing | 2-5ms |
| HTTP to Polymarket | 80-100ms |
| CLOB matching | 10-20ms |
| **Total** | **~130ms** (vs 800ms window) |

---

## Next Session Priorities

1. **Start LIVE test with 5 shares** — verify execution path
2. **Monitor for 2+ hours** — confirm no errors
3. **Fund transfer** — add capital for full 50sh trades
4. **Scale to 50 shares** — run at target size
5. **Consider CONTRARIAN** — after AGGRESSIVE stable

---

## Quick Commands

```bash
# Deploy code changes to server
scp -i ~/Downloads/polymarket-key.pem scripts/run_paper_bot.py ubuntu@54.170.244.221:~/polymarket-amm-bot/scripts/

# Restart service
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 "sudo systemctl restart polymarket-bot"

# Check logs for Binance health warnings
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 "grep 'BINANCE UNHEALTHY' ~/polymarket-amm-bot/logs/server.log"
```

---

---

## Future: Clawdbot (Telegram AI Assistant + Expense Tracking)

**What:** [Clawdbot](https://github.com/clawdbot/clawdbot) - open-source AI assistant that works via Telegram (8.2k GitHub stars, by Peter Steinberger)

**Why:**
1. Use Claude from Telegram for convenience
2. **Automate expense tracking** (see below)

**Deployment Options:**
| Option | Cost | Notes |
|--------|------|-------|
| New t3.micro instance | ~$8/mo | Safest, keeps trading isolated |
| Upgrade current to t3.small | ~$15/mo | 2GB RAM, run both |
| Run on local Mac | Free | Designed for this, no server cost |

**Current instance constraint:** Only ~387MB RAM free - risky to run both

---

### Expense Tracking Automation

**Current workflow:** Note expenses in Notes app with acronym + amount + description → Manually update Excel

**Finance file:** `/Users/rananjaybika/Downloads/Basic Finances jan26.xlsx`

**Category acronyms:**
| Code | Category | Monthly Budget |
|------|----------|----------------|
| F | Food | ₹11,000 |
| L | Living | ₹31,290 |
| G | Gym | ₹6,000 |
| Le | Leisure | ₹10,000 |
| Fu | Fuel | ₹1,000 |
| T | Travel | ₹5,000 |
| Gr | Groceries | ₹2,000 |
| I | Invest | ₹21,000 |

**Proposed automation with Clawdbot:**
1. Move Excel to Google Sheets
2. Send Telegram: `expense F 500 lunch`
3. Clawdbot parses and appends to Google Sheets via API
4. Monthly totals auto-calculated

**Decision:** Deferred - focus on live trading first, implement with Clawdbot setup

---

*Last Updated: January 26, 2026*
