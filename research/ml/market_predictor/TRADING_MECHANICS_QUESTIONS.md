# Trading Mechanics Questions for ML Strategy

*Created: February 8, 2026*
*Context: Pre-implementation questions for hedged/hybrid ML strategy*

---

## 1. Position Entry & Maker Fills

**Current Understanding:**
- Maker orders = 0% fees, but not guaranteed to fill
- Taker orders = instant fill, but fees eat profits

**Questions:**

1. **Fill Rate Reality:** What's the typical fill rate for maker orders at various offsets?
   - At `expensive_ask - 0.01` (1c below)?
   - At `expensive_ask - 0.03` (3c below)?
   - Does this vary by market liquidity or time remaining?

2. **Partial Fill Handling:** If we place a maker order for 50 shares and only 20 fill:
   - Do we leave the remaining 30 open?
   - Cancel and re-place at a new price?
   - Accept partial and move on?

3. **Laddering:** Should we ladder entry orders?
   ```
   10 shares @ expensive_ask - 0.02
   20 shares @ expensive_ask - 0.03
   20 shares @ expensive_ask - 0.04
   ```
   Or single price point?

---

## 2. Hedged Position Math

**Pair Arbitrage (Gabagool-style):**
```python
pair_cost = up_ask + down_ask
if pair_cost < 1.00:
    guaranteed_profit = (1.00 - pair_cost) * min(up_shares, down_shares)
```

**Questions:**

4. **Imbalanced Pairs:** If UP fills but DOWN doesn't:
   - We have directional risk until DOWN fills
   - Max wait time before canceling DOWN order?
   - Do we sell UP if DOWN never fills? (taker fee cost?)

5. **Sequential vs Parallel:** When buying both sides:
   - Place both orders simultaneously?
   - Or sequence them (wait for one fill, then place other)?
   - Gabagool had 4-second gap between sides - intentional?

---

## 3. Exit Mechanics (Stop Loss)

**On Polymarket, to exit a position you can:**
- A) Sell back to orderbook (maker or taker)
- B) Buy the opposite side to hedge (creates locked pair)

**Questions:**

6. **Which exit method do you prefer?**
   - Sell directly: Immediate capital recovery, but taker fee?
   - Buy opposite: Locks capital until resolution, but maker possible?

7. **Time Stop:** Exit after N seconds regardless of price
   - What's a reasonable N? (180s? 300s? 600s?)
   - Should time stop trigger only if PnL is negative?

8. **% Stop:** Exit when unrealized loss exceeds X%
   - Based on entry price vs current bid?
   - What % threshold? (10%? 20%? 30%?)

9. **Trailing Stop:** Lock in profits as position moves favorably
   - Trail by X% from peak unrealized profit?
   - Or fixed amount (e.g., $5 buffer)?

---

## 4. The Merge Function

10. **What is the merge function?**
    - Is this a Polymarket API feature?
    - Does it combine opposing positions into cash?
    - Example: 50 UP shares + 50 DOWN shares = $50 cash?

11. **Capital Rotation:**
    - When a market resolves, capital is freed
    - Can we instantly redeploy to new markets?
    - Or is there settlement delay?

---

## 5. Unified Orderbook Considerations

12. **Cross-side Liquidity:** When UP is expensive (ask = $0.85):
    - DOWN bid should be ~$0.15 (complement)
    - Can we exploit mispricings between UP ask and DOWN bid?
    - `if up_ask + down_ask < 1.00: buy_both()`?

13. **Deduplication:** How do we avoid:
    - Same signal triggering multiple entries?
    - Buying same market multiple times?
    - Conflicting signals (buy UP, then buy DOWN)?

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

**Questions:**

14. **Which formalization captures your intent best?**
    - Pure arbitrage (pair_cost < 1.00)?
    - EV-based with ML prediction?
    - Hybrid (arbitrage base + directional tilt)?

15. **Risk per market:** $50 max risk means:
    - Max loss = $50? (position size adjusted by stop %)
    - Or max position cost = $50? (could lose entire $50)

---

## User Answers

*(To be filled in)*

| Q# | Answer | Notes |
|----|--------|-------|
| 1  |        |       |
| 2  |        |       |
| 3  |        |       |
| 4  |        |       |
| 5  |        |       |
| 6  |        |       |
| 7  |        |       |
| 8  |        |       |
| 9  |        |       |
| 10 |        |       |
| 11 |        |       |
| 12 |        |       |
| 13 |        |       |
| 14 |        |       |
| 15 |        |       |
