# What's Next - Polymarket AGGRESSIVE Strategy

**Updated**: 2026-01-27 (Post Testing Config Deploy)

---

## Current Status

### DEPLOYED: Testing Configuration (10 shares)

| Parameter | Production | Testing (LIVE) |
|-----------|------------|----------------|
| `base_size` | 50 | **10** |
| `high_entry_threshold` | 0.90 | **0.80** |
| `time_stop_seconds` | 120 | 120 |
| `min_time_remaining` | 180 | 180 |

**Dashboard:** http://54.170.244.221:8000

**SSH:** `ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221`

### Expected Performance (Testing @ 10sh)

| Metric | Backtest |
|--------|----------|
| $/hr | ~$2.32 |
| Unhedgeable trades | 0 |
| Min hedge price | $0.10 |

---

## Immediate Next Steps

### 1. Start Live Testing
```bash
# On AWS - restart bot with new config
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 'sudo systemctl restart polymarket-bot'
```

### 2. Monitor First Few Trades
- Check fills are executing correctly
- Verify hedge orders are placing
- Watch for any skip messages at >= $0.80

### 3. After Validation (1-2 hours of clean trades)
- [ ] Revert to production config (50sh, skip >= $0.90)
- [ ] Scale up gradually

---

## Production Config (After Testing Validation)

```python
# scripts/run_paper_bot.py - revert these lines:
spread_base_size=config.get("base_size", 50),  # Change 10 -> 50
high_entry_threshold=0.90,                      # Change 0.80 -> 0.90
```

---

## Reference Documents

| Document | Purpose |
|----------|---------|
| `research/STRATEGY_OPTIMIZATION_PLAN.md` | Full optimization analysis (TIME120s_SKIP) |
| `research/strategies/AGGRESSIVE.md` | Strategy specification |
| `research/TRADING_CONFIGS.py` | Python config definitions |
| `research/MASTER_PLAN.md` | Overview of both strategies |

---

## Strategy Summary

**AGGRESSIVE (Path 1)** - Spike detection + full hedge
- OU threshold, EWMA z-score, 1200ms lookback
- 120s time-stop, min_time=180s
- Skip entries >= threshold (testing: $0.80, production: $0.90)
- ~$9.00/hr @50sh cross-validated (157.4h, 456 markets)

**CONTRARIAN (Path 2)** - Mean reversion (NOT YET DEPLOYED)
- $0.30 entry, hold to resolution
- $618/hr @2500sh, 42% WR
- Requires larger bankroll ($750/trade)

---

## Outstanding Items

- [ ] Validate testing config live (10sh, skip >= $0.80)
- [ ] Scale to production config (50sh, skip >= $0.90)
- [ ] Implement hybrid maker/taker entry (saves ~$1/trade)
- [ ] Deploy CONTRARIAN strategy

---

*Testing config deployed: Jan 27, 2026*
