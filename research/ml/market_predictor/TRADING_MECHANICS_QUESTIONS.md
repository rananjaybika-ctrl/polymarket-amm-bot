# Trading Mechanics Questions for ML Strategy

*Created: February 8, 2026*
*Context: Pre-implementation questions for hedged/hybrid ML strategy*

---

## 1. Position Entry & Maker Fills

**Current Understanding:**
- Maker orders = 0% fees, but not guaranteed to fill
- Taker orders = instant fill, but fees eat profits

**USER ANSWERS:**

1. **Fill Rate Reality:**
   - `best_ask - offset` is correct formula (stay below ask = maker)
   - **Assume maker fill if ask touches our bid**
   - Backtest with this assumption

2. **Partial Fill Handling:**
   - Strategy dependent
   - High conviction: put half size, cancel and replace if needed
   - **Use websocket for fill notifications**

3. **Laddering:**
   - Claude decides per strategy
   - **Avoid adverse selection**
   - Can try **asymmetric laddering favoring expensive winner after volatile move**
   - Example: more size closer to current price on the side that moved

---

## 2. Hedged Position Math

**Pair Arbitrage (Gabagool-style):**
```python
pair_cost = up_ask + down_ask
if pair_cost < 1.00:
    guaranteed_profit = (1.00 - pair_cost) * min(up_shares, down_shares)
```

**USER ANSWERS:**

4. **Imbalanced Pairs:** If UP fills but DOWN doesn't:
   - **YES - sell UP if DOWN never fills** (accept taker fee)
   - Max wait time: strategy dependent

5. **Sequential vs Parallel:**
   - Strategy dependent
   - Gabagool's 4-second gap likely intentional for price discovery

---

## 3. Exit Mechanics (Stop Loss)

**On Polymarket, to exit a position you can:**
- A) Sell back to orderbook (maker or taker)
- B) Buy the opposite side to hedge (creates locked pair)

**USER ANSWERS:**

6. **Exit method preference:**
   - **If really required: LIMIT SELL ORDER** (maker exit)
   - Avoid taker exits when possible

7-9. **Time Stop / % Stop / Trailing Stop:**
   - All strategy dependent
   - User didn't specify fixed values = Claude decides per strategy
   - Use ADAPT25 pattern from AGGRESSIVE_M_V2: check after N trades, apply stops if losing

---

## 4. The Merge Function

**ANSWERED FROM CODEBASE:**

### Merge Mechanics (scripts/merge_positions.py)
```python
# Burns matching UP + DOWN pairs → $1 USDC per pair
mergePositions(USDC, 0, conditionId, [1, 2], amount)

# Example: 50 UP + 50 DOWN → $50 USDC instantly
# Can be done BEFORE resolution (exits position early)
# Gasless via Builder Relayer (Safe wallet) or ~$0.01 gas (EOA)
```

### Merge Window (from market_rotator.py)
- **Window:** -20s to +10s relative to market end
- Rotation waits until merge window closes
- Pre-fetches next market for instant rotation (<100ms)

### Gabagool Behavior (from GABAGOOL_MERGE_ANALYSIS.md)
| Pattern | Value |
|---------|-------|
| Merge during market | NO (batch at session end) |
| Hold to resolution | 86% of markets |
| Merge timing | 6 AM ET batch cleanup |
| Net exposure | 147-1,540 shares (NOT perfectly hedged) |

**USER ANSWERS:**

10. **When to merge:**
    - **Strategy dependent:**
      - High volume, high share count → merge more (capture profits, rotate capital)
      - High conviction → merge less (hold to resolution)
    - **NOT using merge as stop-loss mechanism**

11. **Capital Rotation:**
    - Merge gives instant USDC
    - Can immediately deploy to next market
    - Use market_rotator.py for rotation timing

---

## 5. Unified Orderbook Considerations

**ANSWERED FROM CODEBASE (src/services/pair_analyzer.py):**

### PairOpportunity (Symmetric Arbitrage)
```python
pair_cost = up_ask + down_ask
profit_per_pair = 1.00 - pair_cost
is_profitable = pair_cost < 1.00
executable_size = min(up_ask_size, down_ask_size)
```

### AsymmetricOpportunity (Gabagool-style)
```python
# Checks if buying keeps AVERAGE pair cost < threshold
def calculate_prospective_pair_cost(side, buy_price, buy_qty):
    # Accounts for existing position size/cost
    # Returns what pair cost WOULD BE after this buy

def should_buy_up(buy_qty) -> bool:
    prospective = calculate_prospective_pair_cost("UP", up_ask, buy_qty)
    return prospective < pair_cost_threshold  # default 0.99
```

### Fee Model (src/core/trading_utils.py)
```python
def polymarket_taker_fee(price: float) -> float:
    return 0.0156 * (1 - abs(2 * price - 1))
    # Max 1.56% at price=0.50, zero at extremes
```

**REMAINING QUESTIONS:**

12. **Deduplication strategy:**
    - Per-market cooldown? (e.g., 10s between entries)
    - Max positions per market?
    - Signal-level dedup vs position-level dedup?

---

## 6. Mathematical Formalization

**Potential Equations:**

**Entry Signal (Hedged):**
```python
pair_cost = up_ask + down_ask
expected_profit = (1.00 - pair_cost) * shares - fees
enter = expected_profit > min_profit_threshold
```

**Entry Signal (Hybrid with ML):**
```python
p_up = model.predict_proba(features)  # P(UP wins)
ev_up = p_up * (1.00 - up_ask) - (1 - p_up) * up_ask
ev_down = (1 - p_up) * (1.00 - down_ask) - p_up * down_ask
if ev_up > 0 and ev_down > 0:
    buy_both()  # hedged
elif ev_up > threshold:
    buy_up()    # directional lean
```

**Stop Loss (% based):**
```python
unrealized_pnl = (current_bid - entry_price) * shares
pnl_pct = unrealized_pnl / entry_cost
if pnl_pct < -stop_pct:
    exit_position()
```

**USER ANSWERS:**

14. **Formalization:**
    - Not specified = Claude decides per strategy
    - Both HEDGED (pair arbitrage) and HYBRID (hedge + directional tilt) in scope
    - ML model provides probability estimates to guide

15. **Risk per market:** $50 max risk
    - **Interpretation:** Max position cost = $50 per side
    - Total exposure per market = up to $100 (both sides)
    - Could lose entire amount if strategy wrong

---

## User Answers Summary

| Q# | Topic | Answer |
|----|-------|--------|
| 1 | Fill rate | Assume maker fill if ask touches bid |
| 2 | Partial fills | Strategy dependent, use websocket for notifs |
| 3 | Laddering | Avoid adverse selection, asymmetric OK |
| 4 | Imbalanced pairs | Sell if opposite never fills (accept taker fee) |
| 5 | Sequential vs parallel | Strategy dependent |
| 6 | Exit method | Limit sell order if required |
| 7-9 | Stop types | Strategy dependent (use ADAPT25 pattern) |
| 10 | When to merge | High volume=more, high conviction=less |
| 11 | Capital rotation | Instant via merge, use market_rotator |
| 12 | Unified orderbook | Already in codebase (pair_analyzer.py) |
| 13 | Deduplication | Strategy dependent |
| 14 | Formalization | Claude decides per strategy |
| 15 | $50 risk | Max position cost per side |

**Key Principle:** Most answers are "strategy dependent" = Claude designs per strategy type.
