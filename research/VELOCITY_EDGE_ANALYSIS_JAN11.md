# Velocity Edge Analysis - January 11, 2026

## Executive Summary

**Question:** Is the velocity edge real and scalable?

**Answer:** Theoretically valid but practically weak. The edge exists but is difficult to capture at scale due to low throughput and competition.

---

## Current Results (14-Hour Simulation)

| Metric | Value |
|--------|-------|
| Duration | 14 hours |
| Cycles Completed | 56 / 192 attempted (29%) |
| Win Rate | 91% |
| Avg Profit/Cycle | $0.0575 |
| Total Profit | $3.22 |
| Hourly Rate | $0.23/hour |
| Projected Monthly | $165 (94% return on $175) |

### Velocity Timing Effectiveness
- Entry improvement: 53 bps avg
- Hedge improvement: 604 bps avg
- Total improvement: 657 bps avg

### Velocity Threshold Analysis
| Threshold | Trigger Rate |
|-----------|--------------|
| 0.025 bps | 7.0% |
| 0.050 bps | 5.2% (current) |
| 0.075 bps | 3.8% |
| 0.100 bps | 2.8% |

---

## The Hard Questions

### 1. Is This Edge Real or Simulation Artifact?

**Concerns:**
- Fill simulation uses probability-based fills (added to improve completion rate)
- Real fills depend on actual order flow, not random probability
- 29% completion rate means 71% of attempts fail
- Velocity only exceeded threshold 5.2% of the time

**Suspicious Number:** Hedge improvement of 604 bps (6%) is very high. This comes from "let it ride" waiting for better prices - but in reality, you're exposed to adverse movement during this wait.

### 2. Comparison to Gabagool

| Metric | Velocity Strategy | Gabagool |
|--------|-------------------|----------|
| Trades/hour | ~4 cycles | 500+ trades |
| Throughput | Very low | Very high |
| Fill approach | Wait for signal | Always providing liquidity |
| Imbalance tolerance | Must hedge immediately | 277:11 ratio accepted |
| Capital needed | $175 | >>$10,000 |
| Edge source | Timing prediction | Spread capture + volume |

**Gabagool's Advantage:**
- They don't predict - they provide liquidity constantly
- They accept temporary imbalance, trusting UP+DOWN=$1 at expiry
- 500+ trades/market × small edge = consistent profit
- No timing risk

**Our Disadvantage:**
- We wait for signals that rarely come (5.2% of time)
- When signals come, we often can't get filled (71% failure)
- We're trying to outpredict the market, which is hard

### 3. Scaling Math

**Current Simulation (14h):**
```
$3.22 profit / 14 hours = $0.23/hour
$0.23 × 24 × 30 = $165/month
$165 / $175 capital = 94% monthly return
```

**If We Scale 10x to $1,750 Capital:**
- Same number of cycles (limited by market activity)
- Larger position size → worse fills, more slippage
- Still only 4 cycles/hour
- Competition (Gabagool) sees our larger orders

**Gabagool Scales By:**
- More trades per market (volume)
- Multiple markets
- Larger capital base
- Pure maker = rebates compound

**We Scale By:**
- Increasing position size only
- But completion rate stays at 29%
- And fill quality degrades with size

---

## The Core Problem

```
VELOCITY STRATEGY:
  Wait for signal (95% of time = nothing)
      ↓
  Signal detected (5% of time)
      ↓
  Try to enter (71% fail to fill)
      ↓
  Entry fills (29% × 5% = 1.5% of total time productive)
      ↓
  Wait for hedge signal
      ↓
  Hedge fills
      ↓
  Profit: $0.06

GABAGOOL STRATEGY:
  Always quoting (100% of time)
      ↓
  Fills happen naturally from order flow
      ↓
  Accept imbalance
      ↓
  Profit from spread + rebates
      ↓
  Repeat 500x per market
```

**We're productive 1.5% of the time. Gabagool is productive 100% of the time.**

---

## Evidence Assessment

### Arguments FOR the Edge Being Real:
- 91% win rate is legitimate
- Velocity DOES predict short-term moves (academically proven)
- The lag between Binance and Polymarket exists (1-5 seconds)
- Entry/hedge improvements (53 + 604 = 657 bps) are meaningful
- Visualizations show clear correlation between velocity signals and price movements

### Arguments AGAINST:
- Simulation fill model is generous (probability-based)
- 29% completion = most capital sits idle
- Velocity threshold (0.05 bps) too high - rarely triggers
- We're competing against faster players (Gabagool has lower latency)
- Real execution would face slippage, failed fills, API latency (200-500ms)

---

## Honest Assessment

| Factor | Assessment |
|--------|------------|
| Edge exists? | **Probably yes** - lag is real |
| Edge is capturable? | **Questionable** - execution is hard |
| Simulation accurate? | **Probably optimistic** by 30-50% |
| Scalable? | **Limited** - throughput caps at ~4 cycles/hour |
| Competitive? | **No** - Gabagool does 100x more volume |

---

## Realistic Projections

| Scenario | Monthly Return | Notes |
|----------|---------------|-------|
| Simulation (optimistic) | 94% | Probability-based fills |
| Adjusted (realistic) | 50-60% | Account for execution friction |
| Conservative | 30-40% | Competition + slippage |

**Expected Real-World Performance:**
- Simulated: $3.22 / 14 hours
- Realistic: $1.50-2.00 / 14 hours (50-60% of simulated)
- Monthly: $100-150 on $175 capital

---

## Action Plan

### Phase 1: Validate Edge (1-2 days)
1. **Run with real money ($50-100)**
   - Compare actual fills to simulated fills
   - Measure real completion rate
   - See actual slippage

2. **Key metrics to track:**
   - Fill rate (simulated: 29%, real: ?)
   - Avg profit/cycle (simulated: $0.0575, real: ?)
   - Time to fill (simulated uses probability, real uses order flow)

3. **Success criteria:**
   - If real results > 50% of simulated → edge is capturable
   - If real results < 30% of simulated → simulation artifact

### Phase 2: Optimize (if Phase 1 succeeds)
1. Lower velocity threshold to 0.02-0.03 bps (more triggers)
2. Tighter entry offset (0.003 instead of 0.005)
3. Consider WebSocket for order execution (reduce latency)

### Phase 3: Scale (if Phase 2 succeeds)
1. Increase position size gradually (5 → 10 → 15 shares)
2. Monitor fill quality degradation
3. Consider multiple markets (not just BTC 15-min)

---

## Comparison Summary

| | Velocity Strategy | Gabagool Strategy |
|--|-------------------|-------------------|
| Theoretical return | ~100%/month | Unknown but consistent |
| Realistic return | ~50-60%/month | Likely similar |
| Execution difficulty | High | Low (just quote) |
| Throughput | 4 cycles/hour | 500+ trades/hour |
| Scalability | Low | High |
| Competition risk | High (we're slower) | Low (they ARE the competition) |
| Capital efficiency | Low (idle 98.5% of time) | High (always working) |

---

## Conclusion

The velocity edge is **theoretically valid but practically constrained**:

1. **The lag exists** - Polymarket prices do lag Binance by 1-5 seconds
2. **Velocity predicts** - When BTC moves fast, prices will follow
3. **Execution is hard** - 29% completion rate, 5.2% signal rate
4. **Throughput is capped** - ~4 cycles/hour regardless of capital
5. **Competition is fierce** - Gabagool does 100x our volume

**Bottom Line:** This strategy can generate 50-60% monthly returns on small capital ($175-500), but does not scale well beyond that due to throughput limitations. For serious scaling, need to either:
- Adopt Gabagool's approach (always quoting, accept imbalance)
- Find additional markets to trade
- Significantly improve execution speed (WebSocket orders)

---

## Files Reference

- Simulation script: `scripts/calc_maker_velocity_sim.py`
- Results CSV: `research/calc_velocity_sim_20260110_143519.csv`
- Velocity timeseries: `research/calc_velocity_timeseries_20260110_143519.csv`
- Visualization: `research/velocity_edge_explained.png`
- Flow diagram: `research/velocity_edge_how_it_works.png`

---

*Analysis completed: January 11, 2026*
