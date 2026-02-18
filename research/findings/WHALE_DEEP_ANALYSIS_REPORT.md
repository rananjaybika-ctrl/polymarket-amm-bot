# Whale Deep Analysis Report: Gabagool & Baguette

*Generated: 2026-02-07 16:09:30*

---

## Executive Summary

- **Gabagool**: 76,839 trades across 101 markets
- **Baguette**: 14,446 trades across 76 markets

---

## A. Basic Trade Profiles

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Total Trades | 76,839 | 14,446 |
| BUY Trades | 74,209 | 7,285 |
| SELL Trades | 0 | 5,710 |
| Unique Markets | 101 | 76 |
| Trades/Market | 760.78 | 190.08 |
| Avg Size | 13.82 | 6.32 |
| Median Size | 13.90 | 5.00 |
| Avg Time Remaining | 488.01 | 431.83 |

---

## B. Entry Conditions

### Gabagool

- Total BUY trades: 74,209
- UP buys: 37,720, DOWN buys: 36,489
- Avg entry price: $0.4761
- Pair cost < $1.00: 0.0%
- **Contrarian rate (overall): 47.3%**
  - UP contrarian: 43.9%
  - DOWN contrarian: 50.8%
- Buy expensive side (UP): 38.3%
- Buy expensive side (DOWN): 53.7%

### Baguette

- Total BUY trades: 7,285
- UP buys: 3,346, DOWN buys: 3,939
- Avg entry price: $0.5836
- Pair cost < $1.00: 0.0%
- **Contrarian rate (overall): 56.8%**
  - UP contrarian: 52.7%
  - DOWN contrarian: 60.2%
- Buy expensive side (UP): 54.6%
- Buy expensive side (DOWN): 71.7%

---

## C. Win Rates

### Gabagool

- Resolved trades: 74,209
- **Overall win rate: 48.0%**
- Win rate (UP side): 41.2%
- Win rate (DOWN side): 55.0%

**By Time Remaining:**
- t0-120: 48.2% (n=3250)
- t120-300: 47.9% (n=16371)
- t300-600: 47.6% (n=28122)
- t600-900: 48.4% (n=26466)

**By Entry Price:**
- $0-40: 21.6% (n=30374)
- $40-60: 49.9% (n=17998)
- $60-80: 68.6% (n=15194)
- $80-100: 90.6% (n=10643)

### Baguette

- Resolved trades: 7,285
- **Overall win rate: 59.3%**
- Win rate (UP side): 52.6%
- Win rate (DOWN side): 65.0%

**By Time Remaining:**
- t0-120: 58.2% (n=852)
- t120-300: 64.2% (n=1510)
- t300-600: 59.9% (n=2456)
- t600-900: 56.1% (n=2467)

**By Entry Price:**
- $0-40: 28.5% (n=1664)
- $40-60: 47.4% (n=1931)
- $60-80: 69.3% (n=1915)
- $80-100: 90.3% (n=1775)

---

## D. Sequential Pair Building

### Gabagool

- Total markets traded: 101
- **Markets with BOTH UP and DOWN buys: 101 (100.0%)**
- Markets UP only: 0
- Markets DOWN only: 0
- Time between sides (mean): 7.6s
- Time between sides (median): 4.0s
- Net position abs mean: 70.35
- Achieved pair cost (mean): $0.9985

### Baguette

- Total markets traded: 76
- **Markets with BOTH UP and DOWN buys: 76 (100.0%)**
- Markets UP only: 0
- Markets DOWN only: 0
- Time between sides (mean): 34.8s
- Time between sides (median): 16.0s
- Net position abs mean: 229.47
- Achieved pair cost (mean): $2.2348

---

## E. BTC Correlation Analysis

### Gabagool

- Trades analyzed: 74,209

**Direction correlations with BTC indicators:**

| Indicator | Correlation | P-value | Significant? |
|-----------|-------------|---------|--------------|
| btc_price | -0.0078 | 0.0348 | Yes |
| btc_momentum_5 | -0.0004 | 0.9084 | No |
| btc_momentum_20 | -0.0035 | 0.3471 | No |
| btc_momentum_60 | -0.0042 | 0.2583 | No |
| btc_rsi_14 | 0.0031 | 0.3920 | No |
| btc_rsi_7 | -0.0014 | 0.7092 | No |
| btc_ema_trend | -0.0057 | 0.1229 | No |
| btc_volatility_20 | -0.0008 | 0.8170 | No |
| btc_vs_ema_10 | -0.0016 | 0.6607 | No |
| btc_vs_ema_30 | -0.0037 | 0.3136 | No |
| btc_macd_hist | 0.0012 | 0.7473 | No |
| btc_bollinger_pos | 0.0045 | 0.2162 | No |

### Baguette

- Trades analyzed: 7,285

**Direction correlations with BTC indicators:**

| Indicator | Correlation | P-value | Significant? |
|-----------|-------------|---------|--------------|
| btc_price | 0.0727 | 0.0000 | Yes |
| btc_momentum_5 | 0.0133 | 0.2571 | No |
| btc_momentum_20 | -0.0300 | 0.0106 | Yes |
| btc_momentum_60 | -0.0411 | 0.0005 | Yes |
| btc_rsi_14 | -0.0229 | 0.0504 | No |
| btc_rsi_7 | -0.0219 | 0.0616 | No |
| btc_ema_trend | -0.0929 | 0.0000 | Yes |
| btc_volatility_20 | -0.0046 | 0.6952 | No |
| btc_vs_ema_10 | 0.0025 | 0.8335 | No |
| btc_vs_ema_30 | -0.0283 | 0.0158 | Yes |
| btc_macd_hist | 0.0205 | 0.0811 | No |
| btc_bollinger_pos | -0.0414 | 0.0004 | Yes |

---

## F. Statistical Tests

| Test | Statistic | P-value | Interpretation |
|------|-----------|---------|----------------|
| chi2_side_selection | 63.5038 | 0.0000 | different strategies |
| ttest_trade_price | -33.9534 | 0.0000 | different |
| ttest_trade_size | 64.0018 | 0.0000 | different |
| ttest_time_remaining | 10.0110 | 0.0000 | different |
| ttest_net_obi | -1.7424 | 0.0815 | similar |
| ttest_velocity_bps | 2.0596 | 0.0394 | different |
| ttest_pair_cost | -4.6338 | 0.0000 | different |
| ks_trade_price | 0.1886 | 0.0000 | different distribution |
| ks_trade_size | 0.4457 | 0.0000 | different distribution |
| ks_time_remaining | 0.0865 | 0.0000 | different distribution |
| mannwhitneyu_trade_size | 376645687.0000 | 0.0000 | different |
| mannwhitneyu_time_remaining | 287846536.0000 | 0.0000 | different |

---

## G. Whale Comparison (Head-to-Head)

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Unique Markets | 102 | 77 |
| Total Trades | 76,839 | 14,446 |
| BUY Trades | 74,209 | 7,285 |
| Avg Size | 13.82 | 6.78 |
| Avg Time Remaining | 487.00 | 458.81 |
| Contrarian Rate | 47.29 | 56.77 |

- Overlapping markets: 77
- Gabagool trades first: 25 markets
- Baguette trades first: 46 markets
- Simultaneous entry: 6 markets

---

## H. Binance HF Latency Analysis

### Gabagool

- Trades matched to BTC HF: 74,209

**BTC price movement BEFORE each trade:**

| Window | Avg Move (%) | Std | Direction Corr | P-value |
|--------|--------------|-----|----------------|---------|
| 100ms | -0.000028 | 0.002815 | 0.0054 | 0.1392 |
| 500ms | -0.000075 | 0.006088 | 0.0060 | 0.1036 |
| 1000ms | 0.000024 | 0.008789 | 0.0032 | 0.3888 |
| 5000ms | 0.000022 | 0.025683 | 0.0259 | 0.0000 |

### Baguette

- Trades matched to BTC HF: 7,285

**BTC price movement BEFORE each trade:**

| Window | Avg Move (%) | Std | Direction Corr | P-value |
|--------|--------------|-----|----------------|---------|
| 100ms | -0.000002 | 0.002050 | -0.0141 | 0.2273 |
| 500ms | -0.000076 | 0.005432 | -0.0120 | 0.3062 |
| 1000ms | -0.000107 | 0.007913 | -0.0101 | 0.3894 |
| 5000ms | -0.000136 | 0.020953 | -0.0905 | 0.0000 |

---

## Key Findings & Conclusions

### Hypothesis Testing Results

- **Gabagool is sequential pair builder**: YES (100.0% of markets have both sides)
- **Baguette is sequential pair builder**: YES (100.0% of markets have both sides)

- **Gabagool uses BTC for direction**: YES
- **Baguette uses BTC for direction**: YES

- **Strategies are identical**: DIFFERENT (chi-sq p=0.0000)

---

*Report generated by whale_deep_analysis.py*