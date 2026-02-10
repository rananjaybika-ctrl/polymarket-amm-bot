# Session Handoff — Feb 10, 2026

## READ FIRST
1. `/cm` — read CLAUDE_MISTAKES.md (55 mistakes, #55 is the latest)
2. MEMORY.md — has ABSOLUTE RULES at top (never write from scratch, execution architecture)
3. This file

---

## CURRENT STATE

### FV MM V2 Backtest — VOID (Mistake #55)
- `research/backtests/fair_value_mm_v2_backtest.py` — **INVALID, DO NOT TRUST**
- Written from scratch instead of copying `aggressive_m_v2_grid_search.py`
- Used taker fills at observed ask with NO 500ms delay, NO capital constraint
- Results ($7.42/hr) are meaningless
- Output files exist but are garbage: `research/findings/data/fv_mm_v2_results.csv`, `fv_mm_v2_trades.csv`

### A-S Revival Plan — Exists but Not Started
- Plan file: `/Users/rananjaybika/.claude/plans/cosmic-enchanting-moore.md`
- 36 configs across 3 modes (FADE+pull/SL, A-S two-sided, CALC+FADE hybrid)
- Plan is reasonable BUT must use FADE execution engine, not write from scratch

---

## AGENT FINDINGS TO UTILIZE (all saved in `research/findings/FV_MM_IMPROVEMENT_RESEARCH.md`)

### Agent aec3f0d — Implied Vol Analysis (COMPLETED)
- Market IV consistently 2-3x realized vol (variance risk premium = structural edge)
- EWMA sigma needs 2.2-2.5x multiplier to match market
- Multiplier varies by hour: 4x at hours 1-2 UTC, 1.5-2x at hours 22-23 UTC
- Model accuracy >85% only when |ln(S/K)| > 10 bps from strike
- At 10-20 bps with <2min left: 99.8% accuracy
- Zero spread asymmetry between UP/DOWN (AMM treats both identically)
- Pair cost always ~$1.01, sub-$1.00 only 0.13% (last 60s)

### Agent a258f79 — Enhanced FV Pricing Models (COMPLETED)
- **MR-Vol (Model 5) RECOMMENDED**: sigma_eff with mean-reversion, kappa=0.00419/sec, half-life 165s
- At confidence >= 0.20: 87.3% accuracy vs 85.1% standard (+2.2pp)
- Regime sizing useful as TRADE FILTER (not pricing): reduce size in high-vol, increase in low-vol
- **DO NOT implement**: Drift/momentum (harmful), Jump-diffusion (useless), Asymmetric vol (too small)

### Agent a32305c — Pair Cost & Entry Timing (COMPLETED this session)
- Pair cost always ~$1.01, stable across market lifetime
- Best entry window: 360-480s remaining (6-8 min), 95% fillability
- Taker pairs always lose; maker pairs profit ~$0.0108/pair
- Spreads tightest near expiry, widest at open

---

## UNDERSTANDING THE FADE STRATEGY (Production Config)

### What FADE Does
- **Signal**: BTC spike detected via EWMA on 60Hz Binance data
- **Entry**: MAKER limit order at `expensive_ask - 0.03` (3 cents below ask)
- **Fill**: Price-touch — ask must DROP to our bid level (no delay, 0% fee)
- **Hold**: To market resolution (15 min expiry). No stop loss, no early exit
- **Win**: If we bought the correct side → $1.00 payout per share minus entry price
- **Lose**: If wrong → lose entire entry price per share

### Why FADE Works
- Maker entry = 0% fee (vs 1.56% taker fee at $0.50)
- 3-cent offset below ask = built-in edge (only fills when price moves toward us)
- Hold to resolution = no taker exit fees
- BTC spike signal + velocity/OBI filters = ~65% directional accuracy
- Adaptive session stop (ADAPT25_T5_DD20) limits drawdown in bad regimes

### Production Config: FADE80_3c_ADAPT25_T5_DD20
```
Entry: MAKER bid at expensive_ask - 0.03  (Line 485, aggressive_m_v2_grid_search.py)
Threshold: expensive_ask >= $0.80
Shares: 15 per trade
Hour filter: skip hours (14, 20, 8, 4, 3) UTC
Adaptive stop: after 25 trades, if PnL < -$5, enable 20% drawdown stop
Performance: $2.70/hr, 241.3% ROI, 858 trades across 152 hours (6 datasets)
```

---

## EXECUTION ARCHITECTURE (IMMUTABLE — from paper_trading.py)

| | Taker | Maker |
|---|---|---|
| Delay | 500ms exchange + 42ms network = 542ms | 0ms (price-touch) |
| Fill price | Current ask AFTER 542ms | Our bid when ask <= bid |
| Fee | `0.0156 * (1 - |2p-1|)` max 1.56% | **0%** |
| Rebate | None | ~1% estimated |
| Used for | Entry, time-stop, breakeven | Hedge (passive) |

**Unified orderbook**: Selling YES = Buying NO. One book per market. Our orders compete with ALL bots on both 15m and 1h timeframes.

---

## CONSTRAINTS

- **Starting capital**: $170
- **Max capital per market**: 50% of CURRENT BALANCE (not fixed $85!)
  - Start: 50% of $170 = $85
  - If balance grows to $300 → 50% = $150 per market
  - If balance drops to $100 → 50% = $50 per market
  - This means shares scale WITH equity — compounding on wins, derisking on losses
- Must track running balance throughout simulation (not just starting capital)
- 15-min markets overlap: ~4 markets active at any time
- Position sizing is DYNAMIC based on current equity

---

## TODO LIST FOR NEXT SESSION

### Priority 1: Rebuild FV MM Backtest Correctly
1. **COPY** `aggressive_m_v2_grid_search.py` → `fair_value_mm_v3_backtest.py`
2. Keep the ENTIRE execution engine (fill simulation, fee model, dataset loading, metrics)
3. Replace ONLY the signal/entry logic with FV model:
   - Compute fair value: `P(up) = N(ln(S/K) / (sigma_eff * sqrt(T/900)))`
   - Use MR-vol sigma (agent a258f79 findings)
   - Entry: MAKER bid at some offset below ask (like FADE's 3-cent offset)
   - Fill: price-touch (ask drops to our bid), 0% maker fee
4. Add capital constraint: $170 starting, max 50% of CURRENT BALANCE per market (dynamic sizing — grows with PnL)
5. Test configs: buy_winner with various edge thresholds + confidence filters
6. Run on all 6 datasets, compare fairly to FADE baseline

### Priority 2: A-S Revival (if FV results are promising)
- Use the plan in `cosmic-enchanting-moore.md`
- But COPY execution engine from `aggressive_m_v2_grid_search.py`, don't write from scratch
- Add order pulling, post-fill SL, CALC hybrid ON TOP of existing engine

### Priority 3: Investigate Scalability
- With $170 and $85 max per market, how many concurrent positions?
- Which config maximizes edge per dollar deployed?
- Can we run FV + FADE simultaneously on different market subsets?

### Priority 4: Save/Organize Research
- Agent findings already saved to `FV_MM_IMPROVEMENT_RESEARCH.md`
- Consider which MR-vol parameters to hardcode vs compute dynamically
- Document hour-specific sigma multipliers as lookup table

---

## KEY FILES

| File | Purpose |
|------|---------|
| `research/backtests/aggressive_m_v2_grid_search.py` | **COPY THIS** for any new backtest |
| `src/services/paper_trading.py:70-92` | Execution architecture (fill delays, fees) |
| `src/core/trading_utils.py:53-65` | Taker fee formula |
| `src/config.py:290-414` | FeeConfig class (maker rebate, net profit calc) |
| `research/findings/FV_MM_IMPROVEMENT_RESEARCH.md` | All agent research findings |
| `research/findings/data/fv_mm_v2_*.csv` | VOID results (wrong fill model) |
| `CLAUDE_MISTAKES.md` | 55 mistakes — READ FIRST EVERY SESSION |

---

## RULES FOR NEXT SESSION
1. **Read /cm first** — always
2. **COPY don't create** — `aggressive_m_v2_grid_search.py` is the base
3. **Execution engine is sacred** — market logic can change, fills/fees/delays cannot
4. **$170 capital, 50% of CURRENT BALANCE per market** — dynamic sizing, scales with equity
5. **Ask questions if unsure** — don't waste tokens guessing
