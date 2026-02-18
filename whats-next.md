# What's Next - Polymarket Strategy Development

**Updated**: 2026-02-05 (Strategy Pivot: Taker → Maker)

---

## ⚠️ MAJOR PIVOT: AGGRESSIVE Strategy Deprecated

**Previous approach (taker-based latency arbitrage) is NOT viable.**

| Finding | Evidence |
|---------|----------|
| BTC velocity useless | r = 0.055 (explains 0.3% variance) |
| 60Hz Binance data | NO latency advantage |
| Pair building fails | 0/108 configs profitable |
| Taker fees hurt | 2% on every entry |

**New focus: MAKER-PREDICTION (Path B) + Frank-Wolfe (Path C)**

---

## Current Priorities

### 1. MAKER-PREDICTION Strategy (Path B) 🔧 IN PROGRESS

**Core idea:** Use prediction signal + maker orders (0% fee)

| Component | Status | Details |
|-----------|--------|---------|
| Prediction signal | Research | "expensive side = winner" (57% baseline) |
| Maker order logic | TODO | Limit orders, 2s fill delay simulation |
| OBI filter | Available | Contrarian (-0.64 correlation like Baguette) |
| Backtest | TODO | `research/backtests/maker_prediction_backtest.py` |

**Next step:** Create maker_prediction_backtest.py

### 2. Frank-Wolfe Position Sizing (Path C) 🔬 RESEARCH

**Core idea:** Optimize position size based on price coherence

| Component | Status | Details |
|-----------|--------|---------|
| Coherence checker | Available | `PolyClaude/polyclaude/strategies/arbitrage/coherence.py` |
| FW optimizer | Available | `PolyClaude/polyclaude/strategies/arbitrage/optimizer.py` |
| Integration | TODO | Import to polymarket-amm-bot |

**Next step:** Import FW code, test on historical data

### 3. CONTRARIAN Strategy (Path 2) ✅ READY

Still viable but requires large bankroll ($750/trade).

---

## Files to Create

| File | Purpose | Priority |
|------|---------|----------|
| `research/backtests/maker_prediction_backtest.py` | Backtest new strategy | HIGH |
| `research/strategies/MAKER_PREDICTION.md` | Full strategy spec | HIGH |
| `src/strategies/maker_prediction.py` | Live implementation | MEDIUM |
| `src/core/frank_wolfe.py` | FW optimizer (from PolyClaude) | MEDIUM |

## Files Updated (Feb 5, 2026)

| File | Change |
|------|--------|
| `research/MASTER_PLAN.md` | Added pivot section, marked AGGRESSIVE deprecated |
| `research/strategies/AGGRESSIVE.md` | Marked deprecated |
| `research/strategies/STRATEGY_PIVOT_FEB2026.md` | NEW - Full pivot documentation |
| `CLAUDE.md` | Added pivot notice, new key files |

---

## Key Research Findings

### Whale Analysis (from WHALE_OBI_ANALYSIS.md)

| Whale | Accuracy | Strategy |
|-------|----------|----------|
| Gabagool | 67-70% | Buys expensive side, ~50/50 OBI |
| Baguette | **82.5%** | Strong OBI contrarian (-0.64), momentum filter |

### Prediction Signal Value

| Signal | Accuracy |
|--------|----------|
| Expensive side (baseline) | 57% |
| + OBI confirmation | ~65% |
| + Momentum (60s) | ~70%+ |
| Baguette (unknown edge) | 82.5% |

---

## Commands

```bash
# SSH to AWS
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221

# Stop bot
sudo systemctl stop polymarket-bot

# Check logs
journalctl -u polymarket-bot -f
```

---

## Reference Documents

| Document | Purpose |
|----------|---------|
| `research/strategies/STRATEGY_PIVOT_FEB2026.md` | **NEW** Pivot plan |
| `research/MASTER_PLAN.md` | Overall strategy overview |
| `research/findings/WHALE_OBI_ANALYSIS.md` | Whale trading patterns |
| `research/findings/gabagool_strategy_decoded.md` | Gabagool analysis |
| `PolyClaude/research/findings/gabagool_btc_correlation_findings.md` | BTC velocity analysis |

---

*Strategy pivot started: February 5, 2026*
