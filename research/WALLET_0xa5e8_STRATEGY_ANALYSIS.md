# Strategy Analysis: Wallet 0xa5e8...95f5

**Generated:** 2026-01-23
**Wallet:** `0xa5e83423126dbc6cdb34f10f37f5d27668ab95f5`
**Analysis Period:** 2026-01-21 00:21 to 2026-01-23 02:54 UTC

---

## Strategy Classification: **CONTRARIAN / MEAN-REVERSION**

This bot is a directional contrarian trader on BTC 15-minute Up/Down markets. It waits for BTC to move in one direction within the 15-min window, then bets the price will reverse.

---

## How It Works

1. **Wait** ~5-6 minutes after a BTC 15-min market opens
2. **Observe** which direction BTC has moved since open
3. **Buy the opposite side** (the cheap one, now priced $0.05-$0.40)
4. **Hold to resolution** - no stop-losses, no exits (90% of markets)
5. **Profit** from asymmetric payoff when BTC reverses

---

## BTC Price Correlation (Key Finding)

| Metric | Value |
|--------|-------|
| **Contrarian entries** | **76%** (bets AGAINST BTC direction) |
| BTC move at entry (from market open) | avg 0.06% (~$54) |
| Entry delay after market open | avg 329s (5.5 min) |
| Avg entry price | $0.30 |
| Breakeven win rate needed | 30% |
| **Actual win rate** | **54.2%** |

### The Pattern
- When BTC drops 0.05-0.20% from market open → bot buys **UP** (cheap)
- When BTC rises 0.05-0.20% from market open → bot buys **DOWN** (cheap)
- 76% of trades follow this contrarian pattern

### Entry Threshold
- 76% of entries happen after BTC moves >= 0.01%
- 60% of entries happen after BTC moves >= 0.03%
- 53% of entries happen after BTC moves >= 0.05%
- Only 20% wait for moves >= 0.10%
- Never waits for moves >= 0.30%

---

## Performance

| Metric | Value |
|--------|-------|
| Resolved Markets | 48 |
| Wins | 26 |
| Losses | 22 |
| **Win Rate** | **54.2%** |
| **Total PnL** | **$61,817** |
| Avg PnL/Market | $1,288 |
| Avg Win | $2,887 |
| Avg Loss | -$602 |
| Max Win | $14,697 |
| Max Loss | -$4,042 |
| **Win/Loss Ratio** | **4.8:1** |
| Total Capital Deployed | $68,403 |

### Why It's So Profitable
- Buys at avg $0.30 → risk $0.30/share, reward $0.70/share
- 54% accuracy far exceeds the 30% breakeven threshold
- Kelly edge: huge expected value per trade

---

## Key Metrics

| Metric | Value |
|--------|-------|
| Total Markets | 50 |
| BTC 15-min Markets | 48 |
| Weekly BTC Markets | 2 |
| Two-sided Markets | 1 (2%) |
| One-sided (directional) | 49 (98%) |
| Total Trades | 92 |
| Total Buys | 87 |
| Total Sells | 5 |
| Avg Order Size | 2,576 shares |
| Time Span | 50.6 hours |
| Trades/Hour | 1.8 |

---

## Position Structure

- **98% directional** - buys only ONE side per market
- 27 markets: buys UP only
- 20 markets: buys DOWN only
- 1 market: two-sided (outlier)
- Avg buy price: $0.27-$0.30 (buying the cheap/underdog side)

---

## Entry Timing

| Metric | Value |
|--------|-------|
| Min delay | 8s |
| Max delay | 828s (13.8 min) |
| Mean delay | 404s (6.7 min) |
| Median delay | 394s (6.6 min) |
| Entries after 5 min | 60% |

Distribution:
- 0-30s: 1 market (2%)
- 30-60s: 5 markets (12%)
- 60-120s: 3 markets (7%)
- 120-300s: 8 markets (19%)
- 300s+: 26 markets (60%)

---

## Exit Strategy

- **90% hold to resolution** - no pre-resolution exits
- 5 sells across 5 markets (possible profit-taking on winners that moved in-the-money)
- Sell/Buy ratio: 0.06

---

## Active Hours (UTC)

Bot is active 23:00-08:00 UTC (6PM-3AM ET):
- Peak: 06:00 UTC (1AM ET) - 16 trades
- Quiet: 23:00 UTC (6PM ET) - 3 trades

---

## Comparison to Gabagool (Grid Market Maker)

| Attribute | This Wallet (0xa5e8) | Gabagool22 |
|-----------|---------------------|------------|
| Strategy | Contrarian/directional | Grid market maker |
| Sides | One-sided (98%) | Two-sided (100%) |
| Entry timing | Waits 5+ min | Immediate grid posting |
| Avg price | $0.27 (cheap side) | $0.50 (balanced) |
| Pair cost | N/A (one-sided) | ~$0.97 |
| Trade bursts | None | Heavy (pre-posted grid) |
| Trades/market | 1.8 avg | 20+ avg |
| Hold strategy | To resolution | To resolution |
| Edge source | Directional accuracy | Spread capture |

---

## Strategy Replication Notes

To replicate this strategy:
1. Monitor BTC price from each 15-min market open
2. After 5-7 minutes, check BTC direction (up/down from open)
3. If BTC moved >= 0.03-0.05%, buy the OPPOSITE side
4. Target entry price: $0.20-$0.40 (cheap side)
5. Size: variable, $300-$5,000 per market
6. Hold to resolution (no stops)
7. Expected: ~54% win rate, 2.3:1 reward/risk

---

## Raw Data

- Trade CSV: `research/wallet_0xa5e8_trades.csv`
- BTC prices: `research/btc_jan21_23_1m.json`
- Total trade records: 92
- Analysis script: `scripts/reverse_engineer_wallet.py`
