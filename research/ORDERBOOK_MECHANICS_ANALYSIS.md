# Polymarket Orderbook Mechanics Analysis

**The Core Question:** How does Gabagool's passive grid strategy accumulate the WINNING side when orderbook mechanics seem to suggest it should accumulate the LOSING (cheap) side?

---

## The Unified Orderbook Constraint

Polymarket binary markets have a fundamental constraint:

```
UP_PRICE + DOWN_PRICE ≈ $1.00
```

When UP price rises from $0.50 to $0.70:
- DOWN price must fall from $0.50 to $0.30
- This is an ARBITRAGE relationship, not a direct orderbook link

**Key Insight:** UP and DOWN are SEPARATE tokens with SEPARATE orderbooks. The price relationship is maintained by arbitrageurs, not by the exchange.

---

## Initial Intuition (Wrong)

My initial analysis suggested:

```
When BTC rises:
1. UP becomes expensive (ask rises from $0.50 to $0.70)
2. DOWN becomes cheap (ask falls from $0.50 to $0.30)
3. Grid has bids at various prices
4. DOWN bids fill easily (cheap tokens sold to us)
5. UP bids don't fill (no one selling expensive token cheap)
→ Result: We accumulate DOWN (the LOSER)
```

**This was the contradiction:** How can we accumulate the WINNER if the cheap side fills more?

---

## The Real Mechanism: Volume Follows Trend

After deeper analysis, the mechanism appears to be:

### 1. Activity/Volume Correlation

When BTC trends UP:
- **More total activity** on UP token (both buying AND selling)
- Speculators rush to buy UP → high volume
- Some profit-takers sell UP at various prices → hits our bids
- DOWN becomes "dead market" with less activity
- Fewer people trading DOWN → fewer fills on our DOWN bids

### 2. Who Fills Gabagool's Bids?

Gabagool posts BUY limit orders (bids) at multiple price levels. These get filled by SELLERS:

**Sellers of UP when UP is rising:**
- Early buyers taking profits
- People who bought at $0.40 selling at $0.50 (even as price goes to $0.60)
- Traders rotating positions
- Market makers rebalancing

**Sellers of DOWN when UP is rising:**
- Panic sellers (DOWN holders cutting losses)
- People expecting further UP movement
- But there are FEWER of these because DOWN is less active

### 3. The Net Effect

```
Trending UP Market:
- UP activity: HIGH (both buyers and sellers)
- DOWN activity: LOW (only panic sellers)
- Gabagool's UP bids: Fill frequently (high volume)
- Gabagool's DOWN bids: Fill less (low volume)
→ Net: UP-heavy position accumulates
```

---

## Alternative Hypothesis: Order Flow Asymmetry

Another possible mechanism:

### Market Maker Hedging

1. When speculators buy UP (taking from asks), market makers sell UP
2. Market makers then need to hedge by selling DOWN
3. This creates correlated order flow:
   - Demand for UP → Market makers sell UP
   - Market makers hedge → Market makers sell DOWN
   - But DOWN sells happen at HIGHER prices (closer to $0.50)
   - While UP buying happens at LOWER prices for Gabagool's bids

### Price Level Distribution

At market open (50/50):
- UP and DOWN both have bids/asks around $0.50
- Gabagool has bids from $0.05 to $0.95 on both sides

When price moves to 60/40:
- UP trades concentrate between $0.55-0.65
- DOWN trades concentrate between $0.35-0.45
- Gabagool's bids in those ranges get hit

**Key:** The ACTIVE trading range shifts with price. Gabagool's grid captures activity in the current active range, which follows the trend.

---

## Testing the Hypothesis

To verify which mechanism is correct, we need to analyze:

### 1. Fill Price Distribution

Compare fill prices to market prices at time of fill:
- If fills happen at current market price → we're capturing active volume
- If fills happen at stale prices → something else is happening

### 2. Trade Timing vs Price Level

For each trade, record:
- Fill price
- Market price at that moment
- Time since market open

If fills happen when price PASSES THROUGH a level (not at a stale level), that confirms the volume correlation hypothesis.

### 3. Volume Correlation

Calculate:
- Total UP volume when BTC is UP
- Total DOWN volume when BTC is UP
- Compare to Gabagool's fill rates on each side

---

## Possible Flaw in Analysis

The Gabagool analysis shows:
```
When BTC went UP (89 markets):
  Average imbalance: +79.4 shares (UP heavy)
  UP heavy: 62 markets (69.7%)
```

**Question:** Is this actually causation or correlation?

### Reverse Causation

What if:
1. Gabagool's grid fills naturally tend to favor one side
2. His buying creates price pressure
3. His UP-heavy position pushes UP price higher
4. This correlates with BTC direction (or IS the BTC direction)

This would mean the imbalance CAUSES the trend, not the other way around.

### Selection Bias

Or:
1. Some markets naturally trend UP strongly
2. In these markets, there's more UP activity
3. Gabagool fills more UP
4. BTC also goes UP
5. Correlation, not causation

---

## Practical Implications for Our Grid Maker

Regardless of the exact mechanism, the analysis suggests:

### 1. Grid Will Accumulate Both Sides

The grid will fill on both sides. The question is ratio, not direction.

### 2. Imbalance Tolerance is Key

Gabagool tolerates up to 100% imbalance (274 shares average). We should not aggressively rebalance.

### 3. Volume Matters

Higher volume markets = more fills = more profit potential. Focus on BTC over ETH.

### 4. Resolution is the Profit Source

Profit comes from resolution ($1.00 for winner), not from spread. The 71.2% win rate on imbalances suggests trend-following works.

---

## Data Collection Needed

To resolve this question definitively, we need:

1. **Live orderbook snapshots** at time of each trade
2. **Volume data** on both tokens over time
3. **Correlation analysis** between:
   - Binance price movement
   - Polymarket UP/DOWN volume
   - Gabagool's fill rates on each side

The new `gabagool_live_capture.py` script will help collect this data.

---

## Summary

**Most Likely Mechanism:**

The grid accumulates the winning side because:
1. **Volume follows trend** - more activity on trending side
2. **Activity includes sellers** - profit-takers and rotators sell to grid bids
3. **Quiet side has less volume** - fewer fills on losing side bids

**Still Uncertain:**
- Exact causal mechanism
- Whether it's volume-based or price-level based
- Whether Gabagool's buying itself affects prices

**Next Steps:**
- Run live capture for several days
- Analyze fill prices vs market prices
- Test volume correlation hypothesis
