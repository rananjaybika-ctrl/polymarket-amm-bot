# Lessons from Gabagool - January 11, 2026

## 15-Minute Live Capture Results

| Metric | Value |
|--------|-------|
| Duration | 15 minutes |
| Markets | 3 |
| Total Fills | 117 |
| Total Pairs | 492 shares |
| Total Profit | $14.73 |
| Hourly Rate | $58.93/hour |

---

## The Core Insight: Edge Comes from EXTREMES

### Market 1 (BTC above strike most of time):
```
UP avg price:  $0.6918 (expensive - BTC above strike)
DOWN avg price: $0.2714 (CHEAP - this is the edge!)
Pair cost:     $0.9632 (3.68% profit)
```

### Market 2 (BTC crossed strike multiple times):
```
UP avg price:  $0.4479
DOWN avg price: $0.5250
Pair cost:     $0.9728 (2.72% profit)
```

**Key Discovery**: The edge comes from filling the CHEAP side at extreme prices (0.13-0.30), not from perfect complementary pairs.

---

## Price Bucket Analysis

| Bucket | UP Fills | DOWN Fills | Avg Price |
|--------|----------|------------|-----------|
| CHEAP (0.00-0.35) | 60 shares | 227 shares | UP: $0.15, DOWN: $0.27 |
| MID (0.35-0.65) | 310 shares | 309 shares | UP: $0.51, DOWN: $0.51 |
| EXPENSIVE (0.65-1.00) | 168 shares | 70 shares | UP: $0.70, DOWN: $0.91 |

**Insight**: They get 4x more DOWN fills in the CHEAP bucket. This is where profit is made.

---

## Gabagool's Strategy Decoded

### 1. POST GRID AT ALL PRICE LEVELS
- Not just near current price
- Cover 0.10 to 0.90 range
- ~10 shares per level
- Pre-post orders, don't react

### 2. PROFIT FROM EXTREMES
- When BTC far from strike, one side gets very cheap
- DOWN at $0.21-0.30 when BTC above strike = 70-80% edge on that side
- UP at $0.13-0.18 when BTC below strike = 80-87% edge on that side
- These cheap fills create the overall edge

### 3. ACCEPT IMBALANCE
- Max imbalance observed: 114 shares (1.3:1 ratio)
- No panic hedging
- Trust expiry: UP + DOWN = $1.00 guaranteed

### 4. VOLUME OVER MARGIN
- 3% edge × 500 shares = $15
- Better than 6% edge × 50 shares = $3
- They make 256x our hourly rate

### 5. NO PREDICTION NEEDED
- Buy both sides always
- 33% aligned with BTC, 28% contrarian, 39% neutral
- Order flow determines fills
- Market neutralizes at expiry

### 6. TIMING: BURST FILLS
- 97-100% of fills happen within 100ms of each other
- Orders are PRE-POSTED
- They're not reacting - they're already there waiting

---

## What Our Grid Strategy Should Do

### Current Velocity Approach (ABANDON):
```
Wait for velocity signal (5% of time)
    ↓
Enter expensive side
    ↓
Wait for hedge signal
    ↓
Hedge cheap side
    ↓
Result: 4 cycles/hour, $0.23/hour
```

### New Grid Approach (ADOPT):
```
Market opens
    ↓
Immediately post grid on BOTH sides:
    UP bids at: 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75
    DOWN bids at: 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75
    ↓
Let fills come naturally from order flow
    ↓
Accept imbalance up to 100+ shares
    ↓
At expiry: UP + DOWN = $1.00
    ↓
Result: 100+ fills/hour, $15+/hour
```

---

## Specific Implementation Recommendations

### 1. Grid Order Placement
```python
GRID_LEVELS = [0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85]
SIZE_PER_LEVEL = 10  # shares

for level in GRID_LEVELS:
    # Post on BOTH sides at each level
    post_bid(UP, price=level, size=SIZE_PER_LEVEL)
    post_bid(DOWN, price=level, size=SIZE_PER_LEVEL)
```

### 2. Position Limits
```python
MAX_IMBALANCE = 150  # shares - Gabagool tolerates 114+
MAX_TOTAL_POSITION = 500  # shares per side
```

### 3. Grid Refresh
```python
# Refresh grid every 30 seconds
# Cancel unfilled orders
# Re-post at current levels
# Adjust levels if price moved significantly
```

### 4. Pair Cost Monitoring
```python
# Track running pair cost
# Alert if pair cost > $0.99 (losing money)
# Target pair cost < $0.97 (3%+ edge)
```

### 5. NO Velocity Timing for Entry
```python
# DON'T wait for velocity signals
# DON'T try to predict direction
# DO post orders immediately at market open
# DO let order flow fill you naturally
```

---

## Expected Results (Grid vs Velocity)

| Metric | Velocity Strategy | Grid Strategy |
|--------|-------------------|---------------|
| Fills/hour | 4 cycles | 100+ fills |
| Edge/trade | 6% | 3% |
| Profit/hour | $0.23 | $15+ |
| Capital efficiency | 1.5% productive | 100% productive |
| Prediction needed | Yes (velocity) | No |
| Risk | Directional | Hedged by design |

---

## Key Takeaways

1. **Stop trying to predict** - Gabagool doesn't, and they make 256x more

2. **Profit from extremes** - The edge is in cheap fills at 0.15-0.30, not perfect timing

3. **Volume is everything** - 3% × many > 6% × few

4. **Accept imbalance** - Trust the $1.00 expiry payout

5. **Be there first** - Pre-post orders, don't react to price moves

6. **Both sides always** - ~50/50 position split, no directional bias

---

## Files Reference

- Live capture data: `research/live_capture/gabagool_btc_fills_20260111_111552.csv`
- BTC samples: `research/live_capture/btc_samples_20260111_111552.csv`
- Analysis script: `scripts/gabagool_btc_correlation.py`

---

*Analysis completed: January 11, 2026*
