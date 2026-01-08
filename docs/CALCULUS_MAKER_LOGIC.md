# CALCULUS MAKER Trading Logic

> Visual Reference | Updated: January 8, 2026

```
CALCULUS MAKER: OVERVIEW
═══════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │  GOAL: Buy BOTH Up and Down shares when total cost < $1.00     │
  │  This creates "paired" positions that guarantee profit at      │
  │  market resolution (one side always pays $1.00)                │
  └─────────────────────────────────────────────────────────────────┘

  Strategy Type:    Orderbook-driven arbitrage (NOT directional)
  Market:           BTC 15-minute Up/Down on Polymarket
  Key Insight:      If Up + Down < $1.00, you profit regardless of outcome


ENTRY DECISION: Exponential Decay Mispricing Threshold
═══════════════════════════════════════════════════════════════════════

  Formula: threshold(t) = m_min + (m_max - m_min) × e^(-lambda × (900-t))

  Default Parameters:
  ─────────────────────────
  m_min:     0.005  (late: 0.5% edge)
  m_max:     0.025  (early: 2.5% edge)
  lambda:    0.004  (decay constant)

  ┌────────────────────┬──────────────────┬──────────────────┐
  │ Time Remaining     │ Threshold        │ Max Pair Cost    │
  ├────────────────────┼──────────────────┼──────────────────┤
  │ 15 min (900s)      │ 2.5%             │ $0.975           │
  │ 10 min (600s)      │ 1.1%             │ $0.989           │
  │ 5 min (300s)       │ 0.7%             │ $0.993           │
  │ 1 min (60s)        │ 0.6%             │ $0.994           │
  └────────────────────┴──────────────────┴──────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  WHY DECAY?                                                     │
  │  - Early: Require HIGH edge (2.5%) - more uncertainty          │
  │  - Late: Accept LOW edge (0.5%) - need to complete position    │
  └─────────────────────────────────────────────────────────────────┘


EXAMPLE: ENTRY DECISION AT T=10min
═══════════════════════════════════════════════════════════════════════

  Current Orderbook:

  UP SHARES                          DOWN SHARES
  ─────────────────────────          ─────────────────────────
  Asks:                              Asks:
    $0.55 - 100 shares                 $0.48 - 100 shares
    $0.53 - 200 shares  ← best ask     $0.46 - 200 shares  ← best ask
  ─────────────────────────          ─────────────────────────
    $0.51 - 150 shares  ← best bid     $0.44 - 150 shares  ← best bid
    $0.49 - 50 shares                  $0.42 - 50 shares
  Bids:                              Bids:

  Calculation:
  ─────────────────────────
  Pair cost:      $0.53 + $0.46 = $0.99
  Mispricing:     $1.00 - $0.99 = $0.01 (1.0%)
  Threshold:      1.1% at T=10min

  ┌─────────────────────────────────────────────────────────────────┐
  │  DECISION: NO ENTRY                                             │
  │  Mispricing (1.0%) < Threshold (1.1%)                          │
  │  Wait for better opportunity or more time to pass              │
  └─────────────────────────────────────────────────────────────────┘


EXAMPLE: ENTRY DECISION AT T=5min (Same Prices)
═══════════════════════════════════════════════════════════════════════

  Same orderbook, but now:

  Pair cost:      $0.99
  Mispricing:     1.0%
  Threshold:      0.7% at T=5min

  ┌─────────────────────────────────────────────────────────────────┐
  │  DECISION: ENTER TRADE                                          │
  │  Mispricing (1.0%) > Threshold (0.7%)                          │
  │  Proceed with sequential pairing                                │
  └─────────────────────────────────────────────────────────────────┘


SIZE CALCULATION: Quadratic Ramp (Small Early, Large Late)
═══════════════════════════════════════════════════════════════════════

  Formula: size(t) = min_shares + (max_shares - min_shares) × (1 - t/900)²
  All sizes rounded to multiples of 5 (Polymarket constraint)

  ┌────────────────────┬──────────────────┐
  │ Time Remaining     │ Size (shares)    │
  ├────────────────────┼──────────────────┤
  │ 15 min             │ 5                │
  │ 10 min             │ 6                │
  │ 5 min              │ 9                │
  │ 2 min              │ 13               │
  │ 0 min              │ 50               │
  └────────────────────┴──────────────────┘

  Size over time (visual):

  15min  [▓▓]                                          5 shares
  10min  [▓▓▓]                                         6 shares
   5min  [▓▓▓▓▓]                                       9 shares
   2min  [▓▓▓▓▓▓▓]                                     13 shares
   0min  [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓]              50 shares

  ┌─────────────────────────────────────────────────────────────────┐
  │  WHY RAMP UP LATE?                                              │
  │  - Early: Small size to test fills, avoid showing hand         │
  │  - Late: Large size to complete position under time pressure   │
  └─────────────────────────────────────────────────────────────────┘


PRICING LOGIC: Patient Bid Offset
═══════════════════════════════════════════════════════════════════════

  Formula: price = best_bid - mispricing_threshold

  Example at T=10min with best_bid = $0.50:
  ─────────────────────────
  Threshold:    1.1% ($0.011)
  Price:        $0.50 - $0.011 = $0.489

  ┌────────────────────┬──────────────┬──────────────────┐
  │ Time Remaining     │ Offset       │ Example Price    │
  ├────────────────────┼──────────────┼──────────────────┤
  │ 15 min             │ -$0.025      │ $0.475           │
  │ 10 min             │ -$0.011      │ $0.489           │
  │ 5 min              │ -$0.007      │ $0.493           │
  │ 1 min              │ -$0.006      │ $0.494           │
  │ Emergency          │ 0 (taker)    │ best_ask         │
  └────────────────────┴──────────────┴──────────────────┘


SEQUENTIAL PAIRING: Expensive-First Order
═══════════════════════════════════════════════════════════════════════

  UP SHARES                          DOWN SHARES
  ─────────────────────────          ─────────────────────────
    $0.53 ← EXPENSIVE                  $0.46 ← CHEAP

  Order of Operations:
  ─────────────────────────
  T=0s:    Identify expensive side → UP @ $0.53
  T=0s:    Place UP order FIRST
  T=1s:    Wait for UP fill confirmation
  T=2s:    Place DOWN hedge order

  ┌─────────────────────────────────────────────────────────────────┐
  │  WHY EXPENSIVE FIRST?                                           │
  │  Expensive side = harder to fill = more risk of imbalance      │
  │  If cheap fills first, may get stuck with unpaired position    │
  └─────────────────────────────────────────────────────────────────┘

  Tracking Structure:
  ─────────────────────────
  _pending_expensive_orders[market.slug] = {
      side:                 "UP" or "DOWN"
      placed_at:            timestamp
      position_when_placed: current shares
      expected_size:        order size
      cheap_side:           opposite side
      cheap_price:          hedge price
      expensive_price:      for profit ceiling
  }


INSTANT HEDGE: WebSocket Sub-Second Response
═══════════════════════════════════════════════════════════════════════

  Event Flow:

  Fill on UP @ $0.53
      │
      ▼
  ┌─────────────────────────────────────┐
  │ WebSocket on_fill callback          │
  │ (~50-100ms latency)                 │
  └─────────────────────────────────────┘
      │
      ▼
  ┌─────────────────────────────────────┐
  │ Calculate profit-preserving ceiling │
  │ max_hedge = $1.00 - $0.53 - $0.005  │
  │           = $0.465                  │
  └─────────────────────────────────────┘
      │
      ▼
  Place DOWN hedge order @ min($0.465, best_ask)

  Total latency: ~100-200ms


PROFIT CEILING: Preserving Edge
═══════════════════════════════════════════════════════════════════════

  Formula:
  ─────────────────────────
  MIN_PROFIT = $0.005 (half cent per pair)
  max_hedge_price = $1.00 - expensive_price - MIN_PROFIT

  Example calculation:
  ─────────────────────────
  Expensive side filled:   UP @ $0.73
  Minimum profit:          $0.005 (half cent)
  Max hedge price:         $1.00 - $0.73 - $0.005 = $0.265

  ┌─────────────────────────────────────────────────────────────────┐
  │  RULE: Never pay more than max_hedge for the second side       │
  │  This ensures every completed pair has at least $0.005 profit  │
  └─────────────────────────────────────────────────────────────────┘

  WITHOUT PROFIT CEILING              WITH PROFIT CEILING
  ─────────────────────────          ─────────────────────────
  UP filled @ $0.73                   UP filled @ $0.73
  DOWN available @ $0.28              DOWN available @ $0.28
  Buy DOWN @ $0.28                    Max hedge = $0.265
  Pair cost: $1.01                    Skip: $0.28 > $0.265
  Result: LOSS $0.01/pair             Result: Wait for better price


GRADUAL CHASE: Patient Price Improvement
═══════════════════════════════════════════════════════════════════════

  If order doesn't fill, improve price gradually:

  ┌────────────────────┬──────────┬───────────┬─────────────────┐
  │ Time Remaining     │ Wait     │ Step Size │ Max Iterations  │
  ├────────────────────┼──────────┼───────────┼─────────────────┤
  │ >10 min            │ 60s      │ $0.02     │ 5               │
  │ 5-10 min           │ 30s      │ $0.02     │ 5               │
  │ 2-5 min            │ 15s      │ $0.02     │ 5               │
  │ <2 min             │ 10s      │ $0.03     │ 5               │
  └────────────────────┴──────────┴───────────┴─────────────────┘

  Example at T=7min, best bid = $0.50:

  T=0s:    Place order @ $0.47 (bid - $0.03)
  T=30s:   No fill → improve to $0.49
  T=60s:   No fill → improve to $0.51
  T=90s:   No fill → improve to $0.53
  T=120s:  No fill → improve to $0.55 (capped at ceiling)
  T=150s:  Max iterations reached → stop chasing

  ┌─────────────────────────────────────────────────────────────────┐
  │  KEY RULES:                                                     │
  │  - MAX_CHASE_ITERATIONS = 5                                     │
  │  - Hedge side gets +$0.15 ceiling bonus                        │
  │  - Profit ceiling caps ALL chase prices                        │
  └─────────────────────────────────────────────────────────────────┘


EMERGENCY HEDGE: Imbalance Protection
═══════════════════════════════════════════════════════════════════════

  Trigger conditions:

  ┌────────────────────┬─────────────────────┐
  │ Time Remaining     │ Imbalance Threshold │
  ├────────────────────┼─────────────────────┤
  │ >7 min             │ 10 shares           │
  │ ≤7 min             │ 5 shares            │
  └────────────────────┴─────────────────────┘

  Example:

  Current position:    UP: 30 shares, DOWN: 10 shares
  Imbalance:           20 shares (UP heavy)
  Time remaining:      5 min
  Threshold:           5 shares

  ┌─────────────────────────────────────────────────────────────────┐
  │  EMERGENCY TRIGGERED                                            │
  │  Action: Buy DOWN at TAKER price (best ask) immediately        │
  │  Cooldown: 30 seconds before next emergency order              │
  └─────────────────────────────────────────────────────────────────┘


PAIR COST SAFETY GATES
═══════════════════════════════════════════════════════════════════════

  Three rules that determine when to allow/block buys:

  RULE 1: First Buy of Each Side
  ─────────────────────────
  Always allow first buy of UP or DOWN
  Rationale: Need both sides to create pairs - never block

  RULE 2: Deficit Side
  ─────────────────────────
  Always allow buying the side with FEWER shares
  Rationale: Reduces imbalance, always good

  RULE 3: Surplus Side
  ─────────────────────────
  Only gate buys on the side with MORE shares
  Block if: prospective_pair_cost > max_pair_cost ($0.995)


TREND INTEGRATION: Z-Score Based
═══════════════════════════════════════════════════════════════════════

  TrendDetector thresholds:
  ─────────────────────────
  z_score_mild:     1.0  → MILD state
  z_score_strong:   2.0  → STRONG state
  z_score_extreme:  3.0  → EXTREME state

  Impact on Trading:

  1. DYNAMIC TARGET REDUCTION
     Strong/Extreme trends → reduce target shares by 33-50%

  2. PRIORITY SIDE SELECTION
     Trending UP   → buy UP first (it's getting expensive)
     Trending DOWN → buy DOWN first

  3. TREND-GATED PAIR COST
     In strong trends, block if hedge at market would exceed max_pair_cost

  4. QUOTE PULLING
     Cancel stale quotes when z-score moves sharply
     Velocity threshold: 15 basis points/sec


QUOTE PULLING: Trend-Aware Cancellation
═══════════════════════════════════════════════════════════════════════

  Parameters:
  ─────────────────────────
  max_age_secs:           20s (paper) / 10s (live)
  velocity_threshold_bps: 15 basis points/sec

  Event-driven pull fires when z-score crosses STRONG threshold (2.0)
  Reaction time: ~100-200ms vs 1-2s polling


COMPLETE TRADE FLOW: 10-Minute Example
═══════════════════════════════════════════════════════════════════════

  T=0s (10min left):
  ─────────────────────────
  Threshold:        1.1%
  Size:             6 shares
  Pair cost:        $0.99 (1.0% edge)
  Decision:         NO ENTRY (below threshold)

  T=300s (5min left):
  ─────────────────────────
  Threshold:        0.7%
  Size:             9 shares
  Pair cost:        $0.99 (1.0% edge)
  Decision:         ENTER

  UP @ $0.53 (expensive) → place first
  DOWN @ $0.46 (cheap) → wait for UP fill

  T=301s:
  ─────────────────────────
  UP filled @ $0.53
  Calculate hedge ceiling: $1.00 - $0.53 - $0.005 = $0.465
  Place DOWN @ $0.46 (within ceiling)

  T=302s:
  ─────────────────────────
  DOWN filled @ $0.46

  ┌─────────────────────────────────────────────────────────────────┐
  │  RESULT: 9 paired shares @ $0.99/pair                          │
  │  Guaranteed profit: 9 × $0.01 = $0.09                          │
  └─────────────────────────────────────────────────────────────────┘


SUMMARY: KEY PARAMETERS
═══════════════════════════════════════════════════════════════════════

  ┌──────────────────────────┬─────────────────────────────────────┐
  │ Parameter                │ Default Value                       │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ m_min (late threshold)   │ 0.5%                                │
  │ m_max (early threshold)  │ 2.5%                                │
  │ lambda (decay rate)      │ 0.004                               │
  │ min_shares               │ 5                                   │
  │ max_shares               │ 50                                  │
  │ max_pair_cost            │ $0.995                              │
  │ min_profit               │ $0.005                              │
  │ max_chase_iterations     │ 5                                   │
  │ emergency_cooldown       │ 30 seconds                          │
  │ hard_max_imbalance       │ 10 shares                           │
  └──────────────────────────┴─────────────────────────────────────┘


COMPARISON: CALC vs FAIR VALUE MM
═══════════════════════════════════════════════════════════════════════

  ┌─────────────────────┬────────────────────┬────────────────────┐
  │ Feature             │ CALC               │ Fair Value MM      │
  ├─────────────────────┼────────────────────┼────────────────────┤
  │ Entry Logic         │ Pair cost < thresh │ FV > ask (select)  │
  │ Pricing             │ best_bid - thresh  │ fair_value - edge  │
  │ Size Ramp           │ Quadratic (5→50)   │ Quadratic (5→50)   │
  │ Sequential Pairing  │ Yes                │ Yes                │
  │ Instant Hedge       │ Yes (WebSocket)    │ Yes (WebSocket)    │
  │ Profit Ceiling      │ All paths          │ All paths          │
  │ Imbalance Check     │ Enforced           │ Skipped (direct.)  │
  │ Gradual Chase       │ Yes                │ Yes                │
  │ Quote Pulling       │ Yes                │ Yes                │
  └─────────────────────┴────────────────────┴────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  KEY DIFFERENCE:                                                │
  │  CALC: Orderbook-driven mispricing arbitrage (buys BOTH sides) │
  │  FV:   Information-driven (buys ONLY undervalued via Binance)  │
  └─────────────────────────────────────────────────────────────────┘


FILE LOCATIONS
═══════════════════════════════════════════════════════════════════════

  ┌──────────────────────┬─────────────────────────────┬───────────┐
  │ Component            │ File                        │ Lines     │
  ├──────────────────────┼─────────────────────────────┼───────────┤
  │ Entry Decision       │ calculus_maker.py           │ 202-227   │
  │ Threshold Calc       │ calculus_maker.py           │ 58-87     │
  │ Size Ramp            │ calculus_maker.py           │ 90-154    │
  │ Sequential Pairing   │ run_paper_bot.py            │ 3631-3840 │
  │ Instant Hedge        │ run_paper_bot.py            │ 4741-4846 │
  │ Profit Ceiling       │ run_paper_bot.py            │ 4769-4783 │
  │ Gradual Chase        │ run_paper_bot.py            │ 270-366   │
  │ Quote Pulling        │ run_paper_bot.py            │ 3852-3876 │
  │ Emergency            │ run_paper_bot.py            │ 2823-2878 │
  │ Trend Integration    │ run_paper_bot.py            │ 3245-3350 │
  └──────────────────────┴─────────────────────────────┴───────────┘
```
