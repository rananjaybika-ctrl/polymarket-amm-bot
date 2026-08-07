# Polymarket AMM Bot

**A systematic trading research programme for Polymarket's BTC binary ("Up / Down") markets.**

> **LEGACY** — this project is no longer actively maintained. Research concluded February 2026. Retained as a public record of the methodology, the results, and — equally — the hypotheses that failed.

*Naming note: the repository name predates the design. Polymarket operates a central limit order book (CLOB), not an automated market maker; the system quotes and takes against that book.*

---

## ⚠️ Performance disclosure — read first

**All performance figures on this page are results derived from historical simulation.** They are not actual trading results and no representation is made that any account will achieve comparable returns. Simulated results benefit from hindsight, do not reflect the full cost of financing, infrastructure or operational failure, and cannot fully model queue position or adverse selection.

Where live results exist, they are reported alongside the backtest — including **one strategy whose live paper performance inverted the sign of its backtested return** ([see below](#5-live-versus-simulated-the-fade-divergence)). That divergence is the single most informative result in this repository.

Nothing here is investment advice.

---

## 1. The problem

Polymarket lists recurring binary options on BTC — "will BTC be above $X at the close of this 15-minute window?" — that settle at $1.00 or $0.00. Each market is a two-legged binary: the YES and NO contracts are mechanically linked, so buying one leg at $p is economically identical to selling the other at $(1 − p), and a filled pair at a combined cost below $1.00 is a locked, risk-free spread.

That structure creates three distinct research questions, which this repository addresses in turn:

1. **Directional** — can short-horizon BTC spot dynamics predict binary resolution better than the market's own implied probability?
2. **Microstructural** — can passive quoting capture spread and rebate faster than adverse selection erodes it?
3. **Arbitrage** — can both legs be filled at a combined cost below par often enough to matter?

---

## 2. Methodology

Credibility in a backtest rests entirely on its cost and fill assumptions. Those used here are stated in full so the results can be audited or reproduced.

### Execution model

| Assumption | Value | Source |
|---|---|---|
| Taker latency | 500 ms exchange + 42 ms network = **542 ms** | `src/services/paper_trading.py` |
| Taker fill price | The ask **prevailing after** the 542 ms delay — *not* the ask observed at signal time | `src/services/paper_trading.py` |
| Taker fee | `0.0156 × (1 − \|2p − 1\|)` — peaks at **1.56% at $0.50**, decays to zero at the extremes | `src/core/trading_utils.py:65` |
| Maker fill | Strict price-touch: fills only when `ask ≤ our_bid`, 0 ms delay | `src/services/paper_trading.py` |
| Maker fee | **0%** | Polymarket fee schedule |
| Position limit | 50% of *current* balance per market (compounding, not a fixed notional) | `research/reference/TRADING_CONFIGS.py` |

Modelling the taker fill at the *post-latency* ask rather than the observed ask is the assumption that most distinguishes these results from a naive backtest. An earlier iteration of this work omitted it, reported ~$7.42/hr, and was voided when the error was found — the entry is preserved in `CLAUDE_MISTAKES.md` rather than deleted.

### Data

Market data was captured live rather than sourced from a vendor: Polymarket CLOB order-book snapshots and incremental updates over WebSocket, time-aligned against Binance spot at ~60 Hz.

| Window | Hours | Period |
|---|---|---|
| IS + OOS2 | 62.7 | Jan 16–19, 2026 |
| OOS3 + OOS4 | 42.4 | Jan 22–24, 2026 |
| OOS7 | 19.0 | Jan 2026 |
| OOS8 | 18.1 | Jan 2026 |
| OOS9 | 24.9 | Jan–Feb 2026 |
| OOS10 | ~3 | Feb 2026 |

Parameters were fitted on the in-sample window and grid-searched, then evaluated on windows held out entirely. Reported headline figures are the pooled result across all windows, not the best window.

---

## 3. Results

### Primary strategy — EWMA spike detection with time stop

Config `EWMA_1000 + TS30`. Enters on a statistically significant short-horizon move in BTC spot, hedges the opposing leg, and exits on a 30-second time stop rather than a price stop.

| Window | Hours | Trades | Net P&L | $/hr | Sharpe |
|---|---:|---:|---:|---:|---:|
| IS + OOS2 | 62.7 | 309 | +$163 | +$2.60 | 0.28 |
| OOS3 + OOS4 | 42.4 | 704 | +$759 | +$17.91 | 1.15 |
| OOS7 | 19.0 | 798 | +$512 | +$27.00 | 1.11 |
| OOS8 | 18.1 | 912 | +$412 | +$22.75 | 0.77 |
| OOS9 | 24.9 | 1,095 | +$692 | +$27.78 | 1.09 |
| **Pooled** | **167.0** | **3,818** | **+$2,538** | **+$15.20** | **~0.90** |

Sizing: 50 shares. Win rate ~50%; the edge is in the payoff asymmetry (average win $4.18 against average loss $2.45), not in hit rate.

**Sharpe is stated on an hourly basis**, computed as mean per-trade P&L ÷ standard deviation of per-trade P&L, scaled by √(trades per hour). It is gross of the risk-free rate, financing and infrastructure. It is **not** annualised, and should not be annualised — doing so implies a figure that no realistic capacity assumption supports.

The material caveat is dispersion: per-window returns range from $2.60/hr to $27.78/hr. A pooled mean that wide is not a stable expectation, and the weakest window is the earliest and longest one.

### Other strategies tested

| Strategy | Design | Sample | Result | Verdict |
|---|---|---|---:|---|
| **PHOENIX** (hedged maker-prediction) | Buy the expensive leg passively at 0% fee, then hedge the cheap leg to lock a sub-par pair cost | 715 trades / ~166h | +$433, $5.02/hr, 97.2% WR | Best risk profile; hedge fill rate was the binding constraint |
| **FADE** (aggressive maker) | Treat an unreacted spike as noise; buy the expensive leg above $0.80 and hold to resolution | 858 trades / 152h | +$410, $2.70/hr, 94.7% accuracy | **Backtest only — see §5** |
| **Contrarian** (mean reversion) | Buy the cheap leg on confirmed intra-window retracement | 544 trades / ~200h | +$307, $1.29/hr, 39.2% WR | Thin edge: 39.2% hit rate against a 36% breakeven |

The Contrarian result illustrates the correct way to read a low win rate. At an average entry of $0.36 the breakeven hit rate is 36%; the realised 39.2% is a 3.2-point edge — real, but far too thin to survive a modest deterioration in fill quality.

---

## 4. Hypotheses that failed

Research value lies as much in the rejected hypotheses as the accepted ones. Each of these was tested, documented and abandoned; the findings remain in `research/findings/`.

| Hypothesis | Test result | Disposition |
|---|---|---|
| **Whale order-flow replication** — reverse-engineer a large participant's positioning from CLOB prints | 70.2% directional accuracy but **−$2.93/hr**. 86,542 whale trades analysed | Rejected. High accuracy, negative expectancy — the counterparty's edge was in execution and order-flow visibility, neither of which is replicable from public data |
| **Order-book imbalance as a contrarian signal** | Significant in 0 of 3 windows, p = 0.50 | Rejected as indistinguishable from noise |
| **Machine-learned resolution predictor** | 83.0% accuracy versus an 83.7% baseline of "the expensive leg wins" | Rejected — underperformed a one-line heuristic |
| **Latency arbitrage on BTC velocity** | r = 0.055 (0.3% of variance explained) | Rejected. 60 Hz spot data carried no exploitable lead |
| **Avellaneda–Stoikov quoting with time stop** | $18.04/hr in-sample → **−$7/hr out-of-sample** | Rejected as overfit — a textbook in-sample/out-of-sample inversion |
| **Pair-building for guaranteed spread** | 0 of 108 configurations achieved a mean pair cost below $1.00 | Rejected. The arbitrage is visible but not fillable at retail queue position |

The whale-replication and Avellaneda–Stoikov results are the two worth reading in full. Both were promising on the metric first examined (accuracy; in-sample Sharpe) and both failed on the metric that determines survival (expectancy; out-of-sample stability).

---

## 5. Live versus simulated — the FADE divergence

FADE backtested at **+$2.70/hr across 858 trades and 152 hours**, with 94.7% directional accuracy. Deployed to paper trading on live market data, it produced:

- **Feb 9:** 86 trades, +$62 on a $170 base
- **Feb 10:** 32 trades, **−$29.25 in seven hours**

Two positions accounted for the entire loss: 30 shares of DOWN at $0.81 and 30 at $0.79, both resolving UP. The strategy's accuracy was not the problem — at 94.7% accuracy and an entry above $0.80, the payoff profile is roughly 4:1 against, so two adverse resolutions erase a long run of wins.

This is the central lesson of the project, and it is a risk-management lesson rather than a signal one: **a high hit rate paired with a short-gamma payoff is not a positive-expectancy strategy, and average P&L per hour conceals the tail that determines whether the strategy survives.** The hedged PHOENIX design was the direct response — accept a lower headline return in exchange for a bounded loss on each position.

---

## 6. Repository map

~99,000 lines of Python across 415 tracked files.

```
src/
  api/          Binance, Polymarket CLOB and Chainlink clients; WebSocket layer
  strategies/   Strategy implementations (phoenix, enhanced_spike, contrarian, …)
  services/     Execution engine — paper_trading, live_trading, order_executor,
                position_tracker, balance_manager, auto_redeemer
  models/       Market, order book, position and trade-log domain objects
  trading/      Position management, fill processing, terminal display
  core/         Fee and pricing utilities (canonical fee function lives here)

research/
  backtests/    Per-strategy backtest and grid-search harnesses
  findings/     ~60 dated research memos — results, negative results, post-mortems
  strategies/   Strategy specifications
  reference/    TRADING_CONFIGS.py — single source of truth for all parameters
  observer/     Captured market data

scripts/        Data collection, live runners, analysis
web/            Local monitoring dashboard
deploy/         systemd unit and VPS provisioning
tests/          Strategy unit tests
```

Two files carry more weight than the rest:

- **`research/reference/TRADING_CONFIGS.py`** — every trading parameter, imported directly by both the live runner and the backtest harnesses so that simulation and production cannot silently diverge.
- **`CLAUDE_MISTAKES.md`** — a running log of every methodological error made during the project, including the ones that invalidated published results. Kept deliberately, on the view that a research record which shows only its successes is not a research record.

---

## 7. Known methodological limitations

Stated plainly, because a reader evaluating this work should not have to find them:

1. **Sharpe is not comparable to published fund figures.** It is hourly, gross of the risk-free rate, and excludes infrastructure and financing costs.
2. **Sharpe computation is inconsistent across the codebase.** Later grid-search harnesses apply an annualisation factor of √(252 × 24) to *per-trade* P&L, which mixes bases. Figures quoted in this README come from the per-trade-scaled-by-trade-frequency convention only.
3. **No capacity analysis.** Results are reported at 50-share and comparable sizes on a $170 base. Market impact at institutional size was never modelled, and the underlying markets are thin.
4. **Survivorship in window selection.** The out-of-sample windows are those for which data capture succeeded; capture failures were not random with respect to market conditions.
5. **Queue position is modelled optimistically.** Maker fills assume a strict price touch, with no allowance for being behind other resting orders at the same price.
6. **Single asset, single regime.** All windows fall within January–February 2026. No bear-regime or high-volatility-shock data was tested.

---

## 8. Technology

Python · WebSockets · Polymarket CLOB API · Polygon L2 · Binance spot feeds (~60 Hz) · pandas / NumPy · systemd deployment on AWS EC2

## 9. Development approach

Built through AI-assisted development. All strategy design, signal specification, hypothesis formation, execution-model assumptions and research direction are the author's; implementation was delegated to an AI pair-programming layer (Claude). The author's contribution is the domain judgement — what to test, what a valid fill model looks like.

## 10. Why it was retired

The research answered its own question. The strategies with a durable edge (PHOENIX) produced returns that did not justify the operational burden at the available capital base, and the strategies with attractive headline returns (FADE) carried tail risk that live testing confirmed. The author redirected to discretionary trading, where the same market analysis applies without the cost of maintaining execution infrastructure against a venue whose microstructure changes without notice.

---

*Research conducted December 2025 – February 2026. Archived, unmaintained, and preserved as-is.*
