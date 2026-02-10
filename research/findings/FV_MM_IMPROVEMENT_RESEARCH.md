# Fair Value Market Maker Improvement Research

**Date:** Feb 9, 2026
**Context:** Binary option P(up) = N(ln(S/K)/(sigma*sqrt(T/900))). Current FV MM makes $805 at 63% accuracy. FADE makes $1,442 at 96% accuracy. Goal: improve FV MM.
**Bankroll:** $170

---

## 1. Gamma Trading / Gamma Scalping for Binary Options

### Key Findings

Binary options have **extreme gamma near the strike at expiry**. The delta of a cash-or-nothing binary call is:

```
Delta = phi * e^(-r*T) / (S * sigma * sqrt(T)) * n(d2)
```

where `n(d2)` is the standard normal PDF. The gamma is:

```
Gamma = -phi * e^(-r*T) / (S^2 * sigma^2 * T) * n(d2) * d1
```

As T -> 0, delta becomes a spike (approaches a Dirac delta function) and gamma explodes. This means:

1. **Near the strike with little time left**, the binary option price swings violently between 0 and 1
2. **Traditional gamma scalping** (buy gamma, delta-hedge, profit from realized > implied vol) does NOT apply directly because there's no continuous underlying hedge
3. **The equivalent for Polymarket**: exploit the non-linearity by **widening spreads** near the strike when time is short. The fair value changes rapidly but the market updates slowly.

### Actionable for Our Bot

**Curvature exploitation near strike:**
- When BTC is within ~0.1% of strike AND time_remaining < 300s, the FV oscillates between 0.30-0.70 rapidly
- Our current model already captures this via the N(d2) formula
- **Improvement**: Instead of a fixed edge_threshold (0.05), use a **gamma-aware threshold**:

```python
# Current: fixed threshold
should_enter = abs(fair_value - market_price) > 0.05

# Improved: scale threshold inversely with gamma
gamma_factor = min(1.0, sigma * sqrt(T_frac))  # Small when near strike at expiry
adaptive_threshold = base_threshold * gamma_factor
# Near expiry + near strike: gamma_factor is tiny -> lower threshold -> more trades
# But each trade has huge risk of flipping
```

**Verdict: MODERATE value.** The gamma insight explains why late-market trades near the strike are so volatile. Rather than trading more aggressively there, we should **avoid** that regime (or widen spreads). The current `min_time_remaining=60s` cutoff partially addresses this. Consider raising it to 120s for FV MM since the model becomes unreliable when gamma is extreme.

---

## 2. Jump-Diffusion Models (Merton / Kou)

### Key Findings

**Bitcoin has significant jumps at 15-minute scale:**
- Average positive jump size: 4.7%, negative: 4.1%
- Kurtosis of 15-minute returns: 15-100 (vs 3 for Gaussian)
- Standard deviation of 15-min returns: 0.27% to 1.25% depending on regime

**Merton's Jump-Diffusion Model:**
```
dS/S = mu*dt + sigma*dW + J*dN(lambda)
```
Where J is jump size (lognormally distributed), N(lambda) is Poisson process with intensity lambda.

**For binary options, the Merton pricing formula is:**
```
P_binary_JD = sum_{n=0}^{inf} (e^(-lambda'*T) * (lambda'*T)^n / n!) * N(d2_n)
```
Where:
- lambda' = lambda * (1 + E[J])
- d2_n = [ln(S/K) + (r - sigma_n^2/2)*T] / (sigma_n * sqrt(T))
- sigma_n^2 = sigma^2 + n*delta_J^2/T  (effective vol with n jumps)
- delta_J = volatility of jump size

**For 15-minute BTC binary options:**
- lambda (jump intensity) ~ 2-5 jumps per hour for 1%+ moves
- Per 15-min period: expected ~0.5-1.25 jumps
- Jump vol adds ~0.1-0.3% to effective sigma

### Concrete Implementation

```python
def fair_value_jump_diffusion(S, K, sigma, T_frac, lambda_jump=3.0,
                               mu_jump=0.0, sigma_jump=0.005):
    """
    Merton jump-diffusion binary option fair value.

    lambda_jump: jumps per hour (typical BTC: 2-5 for 1%+ moves)
    mu_jump: mean jump size (0 = symmetric)
    sigma_jump: jump size std (0.5% typical for BTC)
    """
    T = T_frac * 900 / 3600  # Convert to hours
    lambda_T = lambda_jump * T  # Expected jumps in remaining time

    P = 0.0
    for n in range(20):  # Truncate at 20 jumps (Poisson negligible beyond)
        # Poisson weight
        poisson_w = math.exp(-lambda_T) * (lambda_T ** n) / math.factorial(n)

        # Effective sigma with n jumps
        sigma_n_sq = sigma**2 + n * sigma_jump**2 / T_frac if T_frac > 0 else sigma**2
        sigma_n = math.sqrt(sigma_n_sq)

        # Binary option price given n jumps
        if sigma_n * math.sqrt(T_frac) > 1e-8:
            d = math.log(S / K) / (sigma_n * math.sqrt(T_frac))
            P += poisson_w * norm.cdf(d)
        else:
            P += poisson_w * (1.0 if S > K else 0.0)

    return max(0.02, min(0.98, P))
```

### Impact Assessment

**Effect on fair value**: Jump component WIDENS the distribution, pushing extreme fair values toward 0.50. When our standard model says FV=0.80, the jump model might say FV=0.73. This makes us MORE conservative, taking FEWER trades but with HIGHER accuracy.

**Key insight**: The market likely does NOT account for jumps (it uses low implied sigma ~0.28%). If we incorporate jumps, we get WIDER confidence intervals, meaning we only trade when the edge is genuinely large.

**Verdict: HIGH value for accuracy improvement, LOW value for volume.** Estimated +3-5% accuracy, -20% trade count. Net effect on PnL depends on accuracy/volume tradeoff. **Easy to implement** — it's the same N(d2) formula but with a weighted sum.

---

## 3. Stochastic / Regime-Switching Volatility

### Key Findings

**BTC volatility varies 5.9x hour-to-hour** (our own data confirms this):
- Asian session: ~0.06% per 15min
- US open: ~0.36% per 15min

**Three approaches ranked by complexity:**

#### A. EWMA (Already Implemented)
Your `compute_ewma_sigma()` with halflife=30 bars is already state-of-the-art for this timescale. Research shows EWMA with halflife 15-60 minutes correlates r=0.35 with future vol.

#### B. Markov Regime Switching (Moderate Complexity)
```python
from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

# Fit 2-regime model on 1-min log returns
model = MarkovRegression(returns, k_regimes=2, switching_variance=True)
result = model.fit()

# sigma_low = result.params['sigma2.0'] ~ 0.0015
# sigma_high = result.params['sigma2.1'] ~ 0.0045
# transition_probs = [[0.98, 0.02], [0.05, 0.95]]
```

**For binary option pricing**, the regime-switching approach says:
```
FV = P(low_regime) * N(d2|sigma_low) + P(high_regime) * N(d2|sigma_high)
```

This is similar to jump-diffusion: it widens the effective distribution.

#### C. Heston Stochastic Volatility (High Complexity)
```
dS/S = mu*dt + sqrt(V)*dW1
dV = kappa*(theta - V)*dt + xi*sqrt(V)*dW2
corr(dW1, dW2) = rho
```

For 15-minute binary options, Heston is **overkill**. The option lifetime is too short for vol-of-vol dynamics to matter much. The main benefit of Heston (capturing vol smile/skew) is irrelevant since we have a single strike.

### Actionable Implementation

**Best approach: EWMA + Time-of-Day adjustment (you already have this).**

Your `TOD_SCALE` dictionary and `tod_ewma` sigma model are the right approach. Additional improvement:

```python
# Add weekend/weekday adjustment
WEEKDAY_SCALE = {
    0: 0.90,  # Monday
    1: 1.00,  # Tuesday
    2: 1.00,  # Wednesday
    3: 1.05,  # Thursday
    4: 1.10,  # Friday (pre-weekend)
    5: 0.70,  # Saturday
    6: 0.75,  # Sunday
}

# Add recent-volatility momentum
def sigma_with_momentum(ewma_sigma, recent_5min_sigma):
    """If recent 5-min vol >> EWMA, expect elevated vol to persist."""
    ratio = recent_5min_sigma / max(ewma_sigma, 1e-6)
    if ratio > 2.0:
        # Recent vol surge: blend in recent estimate
        return ewma_sigma * (1 + 0.3 * (ratio - 1))
    return ewma_sigma
```

**Verdict: MODERATE value. Your EWMA + ToD is already 80% of the benefit. Adding regime detection or vol momentum gives marginal ~5% improvement. Not worth the complexity for $170 bankroll.**

---

## 4. Pair Trading Binary Options (Hedged Portfolios)

### Key Findings

For Polymarket UP/DOWN pairs, P(UP) + P(DOWN) should = $1.00 (minus spread). Opportunities arise when:

1. **pair_cost < $1.00**: Buying BOTH sides guarantees profit (your CHEAP strategy exploits extreme cases of this)
2. **Asymmetric sizing**: Buy MORE of the side with higher edge

### Asymmetric Sizing Formula

```python
# Kelly criterion for binary options
def kelly_fraction(p_win, odds):
    """
    p_win: our estimated probability of winning
    odds: payout ratio (e.g., for 50c option, odds = 1.0)
    """
    b = (1.0 / odds) - 1  # Net odds
    return (p_win * (b + 1) - 1) / b

# For FV MM: if FV(up) = 0.65 and ask(up) = 0.55
# p_win = 0.65 (our model probability)
# odds = (1/0.55) - 1 = 0.818 (pay 55c to win 45c)
# kelly = (0.65 * 1.818 - 1) / 0.818 = 0.22 (bet 22% of bankroll)
```

### Hedged Portfolio Approach

```python
def asymmetric_hedge_sizing(fv_up, up_ask, down_ask, total_shares=15):
    """
    Buy both sides but weight toward the undervalued side.

    Key constraint: total_cost = up_shares * up_ask + down_shares * down_ask
    Goal: maximize E[PnL] = up_shares*(fv_up - up_ask) + down_shares*(fv_down - down_ask)
    """
    fv_down = 1.0 - fv_up
    edge_up = fv_up - up_ask
    edge_down = fv_down - down_ask

    if edge_up <= 0 and edge_down <= 0:
        return 0, 0  # No edge on either side

    # Allocate proportional to edge
    total_edge = max(edge_up, 0) + max(edge_down, 0)
    if total_edge == 0:
        return 0, 0

    up_frac = max(edge_up, 0) / total_edge
    down_frac = max(edge_down, 0) / total_edge

    up_shares = max(5, 5 * round(total_shares * up_frac / 5))
    down_shares = max(5, 5 * round(total_shares * down_frac / 5))

    return up_shares, down_shares
```

### Why This Matters for FV MM

Your current approach: buy ONE side when FV disagrees with market (63% accuracy).
FADE approach: always buy the expensive side at a discount (96% accuracy).

**The hybrid**: Buy the undervalued side with MORE shares, hedge with smaller position on the other side.

```
Example: FV(up)=0.65, up_ask=0.55, down_ask=0.46
- Edge on UP: 0.65 - 0.55 = 0.10
- Edge on DOWN: 0.35 - 0.46 = -0.11 (negative = overpriced)
- Pure FV: buy 15 UP shares at 0.55 (risking $8.25)
- Hedged FV: buy 15 UP + 5 DOWN (risking less if wrong)

If UP wins: +15*(1-0.55) - 5*0.46 = +$6.75 - $2.30 = +$4.45
If DOWN wins: -15*0.55 + 5*(1-0.46) = -$8.25 + $2.70 = -$5.55
```

**Verdict: LOW value for $170 bankroll.** Hedging reduces variance but also reduces expected PnL. With 63% accuracy, the hedge costs more than it saves. Hedging makes sense at larger scale ($5k+) where variance reduction matters. For $170, maximize expected value instead.

---

## 5. Market Making with Adverse Selection (Gueant-Lehalle, Cartea-Jaimungal, Avellaneda-Stoikov)

### Key Formulas

#### Avellaneda-Stoikov Reservation Price
```
r(s,q,t) = s - q * gamma * sigma^2 * (T - t)
```
Where:
- s = current mid-price
- q = inventory (positive = long, negative = short)
- gamma = risk aversion parameter
- sigma = volatility
- T-t = time remaining

#### Optimal Spread
```
delta_bid + delta_ask = gamma * sigma^2 * (T-t) + (2/gamma) * ln(1 + gamma/kappa)
```
Where kappa = order arrival intensity.

#### Guéant-Lehalle-Fernandez-Tapia Closed Form
Extends A-S with inventory constraints and exponential utility:
```
delta_ask = (1/gamma) * ln(1 + gamma/kappa) + (q+1)/2 * phi(q+1)
delta_bid = (1/gamma) * ln(1 + gamma/kappa) - (q-1)/2 * phi(q-1)
```
Where phi is a function of inventory risk.

### Application to Binary Option FV MM

**Critical insight**: These models are designed for continuous markets with symmetric information. Binary options on Polymarket have a KEY difference: **the outcome is deterministic** (BTC will go up or down). Every trade has a winner and loser. The adverse selection is not from "informed traders" but from **price movement**.

**Adapted A-S for binary options:**
```python
def reservation_price_binary(fair_value, inventory, gamma, sigma_fv, time_frac):
    """
    Reservation price adjusted for inventory.

    fair_value: our model FV (0-1)
    inventory: net shares held (positive = long this side)
    gamma: risk aversion (higher = more aggressive inventory reduction)
    sigma_fv: volatility of fair value itself
    time_frac: T_remaining / 900
    """
    # Inventory penalty: want to offload if holding too much
    inv_penalty = inventory * gamma * sigma_fv**2 * time_frac * 900
    return fair_value - inv_penalty

# Example: FV=0.65, holding 15 shares, gamma=0.001, sigma_fv=0.10
# penalty = 15 * 0.001 * 0.01 * 0.5 * 900 = 0.0675
# reservation = 0.65 - 0.068 = 0.582
# -> Lower reservation price -> more willing to sell, less willing to buy more
```

### VPIN / Toxic Flow Detection

For our 15-minute binary options, "toxic flow" = someone who knows the BTC direction before the market reflects it. Detection:

```python
def estimate_vpin(recent_trades, window=20):
    """
    Volume-synchronized probability of informed trading.
    High VPIN = market is being traded by informed participants.
    """
    buys = sum(1 for t in recent_trades if t['side'] == 'buy')
    sells = len(recent_trades) - buys
    volume_imbalance = abs(buys - sells) / max(len(recent_trades), 1)
    return volume_imbalance  # 0-1, higher = more toxic

# When VPIN > 0.7: widen spreads or stop quoting
# When VPIN < 0.3: tight spreads, aggressive quoting
```

**Verdict: HIGH value.** The inventory-adjusted reservation price is the single most impactful improvement. Your current FV MM has NO inventory management — it keeps buying even when already holding positions. Adding inventory-aware quoting would:
1. Reduce overexposure in one direction
2. Scale back when losing (natural drawdown control)
3. Integrate cleanly with existing fair value model

---

## 6. Polymarket-Specific Strategies

### Fee Structure (Confirmed from Docs)

**15-minute crypto markets taker fee formula:**
```python
fee_equivalent = shares * price * 0.25 * (price * (1 - price))**2
```

**Effective fee rates by probability:**
| Price (prob) | Fee per 100 shares | Effective rate |
|---|---|---|
| $0.10 | $0.02 | 0.02% |
| $0.25 | $0.22 | 0.88% |
| $0.50 | $0.78 | 1.56% |
| $0.75 | $0.66 | 0.88% |
| $0.90 | $0.16 | 0.18% |

**Key insight**: Fees are HIGHEST at 50% (near strike) and LOWEST at extremes (0/100). This strongly favors trading at the extremes — exactly where FADE operates (buying at 80-95c where fees are 0.18-0.88%).

### Maker Rebates

- **100% of taker fees redistributed to makers** (initially; now 20%)
- Rebate formula: `your_rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool`
- Distributed daily in USDC
- **Post-only orders** available: `postOnly=true` in API ensures maker status

### Maker Order Strategy

```python
# Post-only order ensures maker status (zero fees + rebate)
order = {
    "type": "LIMIT",
    "side": "BUY",
    "price": str(bid_price),
    "size": str(shares),
    "postOnly": True  # CRITICAL: ensures maker, rejected if would cross spread
}
```

### Spread Characteristics

Bid-ask spreads narrowed from 4.5% (2023) to 1.2% (2025). For 15-min crypto markets, typical spread is 1-3 cents. Meaningful market making requires $5k-25k capital according to practitioner sources (though our $170 bankroll can work with fewer markets).

**Verdict: CRITICAL.** The fee structure alone justifies two things:
1. **Always use post-only orders** (maker: 0 fee + rebate vs taker: up to 1.56% fee)
2. **Trade at probability extremes** where fees are lowest (FADE territory, 80-95c)

---

## 7. Time-Weighted Entry Strategies

### Key Findings

**Research on 0DTE options (closest analogue to 15-min binary):**

1. **Theta decay is non-linear**: Gradual early, accelerates sharply near expiry
2. **For binary options specifically**: theta follows an S-curve. At T=900s, a near-strike binary decays slowly. At T=60s, decay is explosive.
3. **Our data confirms**: 88.7% of the time, BTC direction at entry = final direction

**The theta curve for a binary option:**
```
theta_binary = -e^(-r*T) * n(d2) * d1 / (2 * sigma * T * sqrt(T))
```

This means:
- **Early entry (T=900s-600s)**: Fair value changes slowly. Your model is least confident (wide uncertainty). Market is also uncertain. Entry here is a COIN FLIP enhanced by your vol model.
- **Mid entry (T=600s-300s)**: Fair value starts to crystallize. BTC direction is becoming clearer. This is where your 63% accuracy lives — the model has some edge but lots of uncertainty.
- **Late entry (T=300s-60s)**: Fair value is nearly locked in. 88.7% directional persistence. But: gamma explosion means FV swings wildly near the strike.

### Optimal Entry Timing for FV MM

```python
# Time-weighted edge threshold
def time_weighted_threshold(time_remaining, base_threshold=0.05):
    """
    Entry threshold that reflects the quality of our FV estimate.

    Early: high threshold (FV is uncertain, need large edge)
    Mid: moderate threshold (FV is stabilizing)
    Late: medium threshold (FV is confident but gamma risk is high)
    """
    T_frac = time_remaining / 900.0

    if T_frac > 0.67:  # First 5 min
        # FV is unreliable, need big edge to justify entry
        return base_threshold * 1.5  # 7.5% edge required
    elif T_frac > 0.33:  # Middle 5 min
        # Sweet spot: FV is stabilizing, market hasn't converged
        return base_threshold * 0.8  # 4% edge required
    elif T_frac > 0.10:  # 1.5-5 min remaining
        # FV is clear, but market is catching up
        return base_threshold * 1.0  # 5% edge required
    else:
        # Last 90 seconds: gamma explosion zone, avoid
        return base_threshold * 2.0  # 10% edge required (effectively no trade)
```

### Entry Pattern from Existing Data

Your research showed EWMA halflife=30min is best sigma predictor. The sigma estimate improves as more data arrives. This means:

**EARLY markets have stale sigma -> bad FV -> random entries.**
**LATER markets have fresh sigma -> good FV -> informed entries.**

This is why your current approach gets only 63% — many early trades are noise.

**Verdict: HIGH value.** The most impactful change is to **raise the threshold for early entries** (first 5 minutes) and focus on the 5-10 minute window where FV is becoming reliable but the market hasn't fully converged. This could boost accuracy from 63% to 70%+ while sacrificing ~20% of trades.

---

## SYNTHESIS: Ranked Improvements for $170 Bankroll

### Tier 1: Implement Now (High Impact, Low Effort)

| # | Improvement | Expected Impact | Implementation |
|---|---|---|---|
| 1 | **Post-only maker orders** | +2-3% per trade (fee savings + rebate) | Set `postOnly=True` on all limit orders |
| 2 | **Time-weighted entry threshold** | +5-7% accuracy (fewer bad early trades) | Raise threshold for T>600s, lower for 300-600s |
| 3 | **Inventory-aware quoting** | Reduce max drawdown 30-40% | A-S reservation price adjustment |

### Tier 2: Implement Next (Moderate Impact, Moderate Effort)

| # | Improvement | Expected Impact | Implementation |
|---|---|---|---|
| 4 | **Jump-diffusion FV model** | +3-5% accuracy (better FV at extremes) | Merton weighted sum of N(d2) |
| 5 | **VPIN-based spread adjustment** | Avoid toxic trades (-10-15% loss reduction) | Monitor volume imbalance, widen when high |
| 6 | **Vol momentum blending** | +2-3% FV accuracy in volatile periods | Blend recent 5-min vol when >> EWMA |

### Tier 3: Research More (Lower Priority)

| # | Improvement | Expected Impact | Implementation |
|---|---|---|---|
| 7 | Regime-switching vol | +1-2% marginal over EWMA+ToD | statsmodels MarkovRegression |
| 8 | Asymmetric pair sizing | Variance reduction only | Kelly-weighted dual sides |
| 9 | Gamma-aware late-market filter | Avoid blowups in last 60s | Raise threshold when gamma extreme |

### Tier 4: Not Worth It for $170

| # | Why Not |
|---|---|
| Heston stochastic vol | Option lifetime too short for vol-of-vol to matter |
| SABR model | Single strike means no smile to capture |
| Full pair hedging | Reduces expected PnL too much at this bankroll |

---

## CONCRETE NEXT STEPS

### Step 1: Add post-only maker orders (if not already)
```python
# In order execution: ensure all orders are post-only
order_params["postOnly"] = True
```

### Step 2: Time-weighted threshold in fair_value_mm_backtest.py
```python
# Replace fixed edge_threshold with time-weighted version
def get_edge_threshold(time_remaining, base=0.05):
    T_frac = time_remaining / 900.0
    if T_frac > 0.67:
        return base * 1.5   # First 5 min: require 7.5% edge
    elif T_frac > 0.33:
        return base * 0.8   # Middle: sweet spot, 4% edge
    elif T_frac > 0.10:
        return base * 1.0   # Late: standard 5% edge
    else:
        return base * 3.0   # Last 90s: effectively skip
```

### Step 3: Inventory penalty in FV model
```python
def adjusted_fair_value(fv, inventory, time_remaining, gamma=0.001, sigma_fv=0.10):
    T_frac = time_remaining / 900.0
    penalty = inventory * gamma * sigma_fv**2 * T_frac * 900
    return fv - penalty
```

### Step 4: Jump-diffusion FV (replace current N(d2))
```python
def fair_value_with_jumps(S, K, sigma, T_frac, lambda_jump=3.0, sigma_jump=0.005):
    """Weighted sum over possible jump counts."""
    T_hours = T_frac * 900 / 3600
    lambda_T = lambda_jump * T_hours
    P = 0.0
    for n in range(15):
        w = math.exp(-lambda_T) * (lambda_T**n) / math.factorial(n)
        sigma_eff = math.sqrt(sigma**2 + n * sigma_jump**2 / max(T_frac, 0.001))
        denom = sigma_eff * math.sqrt(max(T_frac, 0.001))
        d = math.log(S / K) / denom if denom > 1e-8 else 0
        P += w * norm.cdf(d)
    return max(0.02, min(0.98, P))
```

---

## Sources

- [Gamma Scalping Primer - Charles Schwab](https://www.schwab.com/learn/story/gamma-scalping-primer)
- [Detecting Jump Risk and Jump-Diffusion Model for Bitcoin Options Pricing](https://www.mdpi.com/2227-7390/9/20/2567)
- [Kou Double-Exponential Jump-Diffusion Model](http://www.columbia.edu/~sk75/MagSci02.pdf)
- [Merton Jump Diffusion Model with Python](https://www.codearmo.com/python-tutorial/merton-jump-diffusion-model-python)
- [Pricing Bitcoin Derivatives under Jump-Diffusion Models](https://ideas.repec.org/p/arx/papers/2002.07117.html)
- [Pricing of a Binary Option Under a Mixed Exponential Jump Diffusion Model](https://www.mdpi.com/2227-7390/12/20/3233)
- [Guide to the Avellaneda & Stoikov Strategy - Hummingbot](https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy/)
- [Guéant-Lehalle-Fernandez-Tapia: Dealing with the Inventory Risk](https://arxiv.org/abs/1105.3115)
- [Guéant: Optimal Market Making](https://arxiv.org/pdf/1605.01862)
- [GLFT Model and Grid Trading - hftbacktest](https://hftbacktest.readthedocs.io/en/py-v2.0.0/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html)
- [Cartea-Jaimungal: Algorithmic and High-Frequency Trading](https://www.amazon.com/Algorithmic-High-Frequency-Trading-Mathematics-Finance/dp/1107091144)
- [Detecting Toxic Flow](https://arxiv.org/html/2312.05827v1)
- [From PIN to VPIN: Order Flow Toxicity](https://www.quantresearch.org/From%20PIN%20to%20VPIN.pdf)
- [Binary Options: Pricing, Replication and Skew Sensitivity - Quant Next](https://quant-next.com/binary-options-pricing-replication-and-skew-sensitivity/)
- [Cash-or-Nothing Binary Options - QuantPie](https://quantpie.co.uk/bsm_bin_c_formula/bs_bin_c_summary.php)
- [Polymarket Maker Rebates Program](https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program)
- [Polymarket Trading Fees](https://docs.polymarket.com/polymarket-learn/trading/fees)
- [Polymarket Dynamic Fees - Finance Magnates](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [0DTE Options Time Decay Research - Option Alpha](https://optionalpha.com/blog/0dte-options-time-decay)
- [High-Frequency Jump Analysis of Bitcoin](https://scaillet.ch/pdfs/bitcoin.pdf)
- [Regime Switching Forecasting for Cryptocurrencies](https://link.springer.com/article/10.1007/s42521-024-00123-2)
- [Regime-Specific Trading Using HMM - QuantInsti](https://blog.quantinsti.com/regime-adaptive-trading-python/)
- [Prediction Market Making Guide 2026](https://newyorkcityservers.com/blog/prediction-market-making-guide)
- [Adaptive Multi-Strategy Market-Making Agent](https://arxiv.org/pdf/2204.13265)

---

# Feb 9, 2026 — Agent Research Results (Implied Vol, Enhanced FV Models, Pair Cost)

> **IMPORTANT (Mistake #55):** FV MM v2 backtest results from prior sessions are **VOID** due to wrong fill model. Any PnL numbers from those backtests should NOT be used for decision-making. Only the qualitative findings below are reliable.

Three parallel research agents were launched on Feb 9, 2026. Two completed; one hit a rate limit and failed.

---

## Agent aec3f0d — Polymarket Implied Vol Analysis (COMPLETED)

### Variance Risk Premium (Structural Edge)

Market implied volatility (IV) is **consistently 2-3x realized volatility**. This is the variance risk premium — sellers of volatility (i.e., sellers of options priced at market IV) earn a structural premium. For our FV MM, this means the market systematically **overprices** uncertainty, creating persistent edge for strategies that price closer to realized vol.

### IV by Hour of Day (UTC)

| Hours (UTC) | IV Level | Multiplier vs Realized |
|---|---|---|
| 1-2 | Very High | 3-4x |
| 9-10 | Moderate | ~2x |
| 22-23 | Low | 1.5-2x |

**Implication:** The EWMA sigma needs a **2.2-2.5x multiplier** to match market-implied levels on average, but the best multiplier varies dramatically by hour:
- Hours 1-2 UTC: use **4x** multiplier
- Hours 9-10 UTC: use **1.5-2x** multiplier
- Hours 22-23 UTC: use **1.5-2x** multiplier

This suggests the current flat `TOD_SCALE` dictionary could be improved with hour-specific sigma multipliers calibrated to implied vol.

### Model Accuracy Thresholds

| Condition | Accuracy |
|---|---|
| General (all conditions) | <85% |
| \|ln(S/K)\| > 10 bps | >85% |
| 10-20 bps from strike, <2 min left | **99.8%** |

**Key insight:** The model is only reliably accurate when the underlying price is at least 10 basis points away from the strike. Near the strike, the model is essentially a coin flip unless time remaining is very short (under 2 minutes), at which point the outcome is nearly determined.

### Model vs Market Accuracy

At **T=600s** (10 minutes remaining):
- Model accuracy: **79.7%**
- Market accuracy: **74.3%**

The model **outperforms** the market at the 10-minute mark. This is the window where FV MM should be most aggressive — the model has an edge over what the market is pricing.

### Pair Cost Analysis

| Metric | Value |
|---|---|
| Mean pair cost | $1.0109 |
| Fraction below $1.00 | **0.13%** |
| When below $1.00 | Last 60 seconds only |

**Implication:** Free arbitrage from pair cost < $1.00 is extremely rare (0.13% of the time) and only appears in the final minute. Not a viable standalone strategy. The mean pair cost of $1.0109 represents a 1.09% round-trip cost (spread + fees).

### Spread Asymmetry

**Zero asymmetry** between UP and DOWN sides. The AMM treats both identically. This confirms there is no systematic bias to exploit by favoring one side over the other purely based on spread dynamics.

---

## Agent a258f79 — Enhanced FV Pricing Models Research (COMPLETED)

Six alternative pricing models were evaluated against the standard N(d2) binary option formula.

### Model Results Summary

| # | Model | Result | Recommendation |
|---|---|---|---|
| 1 | Jump-Diffusion (Merton) | Negligible improvement | **SKIP** — not worth the complexity |
| 2 | Asymmetric Volatility | 8.7% downside leverage effect | **SKIP** — effect too small to matter |
| 3 | Regime-Switching | Active/calm vol ratio: 1.89x | **Use as TRADE FILTER**, not pricing input |
| 4 | Drift/Momentum | 1% *worse* accuracy | **DO NOT IMPLEMENT** — harmful |
| 5 | Mean-Reverting Vol (MR-Vol) | Brier 0.1549 vs 0.1572 standard | **RECOMMENDED** |
| 6 | Implied Volatility | Cannot backtest without live data | **INCONCLUSIVE** — oracle test shows even perfect vol barely helps |

### Model 1: Jump-Diffusion (Merton)

Despite the theoretical analysis in the prior section suggesting jump-diffusion could add +3-5% accuracy, empirical testing shows **negligible improvement**. The jump component at the 15-minute timescale is too small relative to the diffusion component. The complexity cost (Poisson-weighted sum over N(d2)) is not justified.

### Model 2: Asymmetric Volatility

Tested whether downside moves have higher volatility than upside moves (leverage effect). Found an **8.7% asymmetry** — downside vol is slightly higher. However, this effect is too small to materially change fair value estimates at the 15-minute timescale.

### Model 3: Regime-Switching

The active-to-calm volatility ratio is **1.89x** (active regime vol is 1.89 times calm regime vol). However, incorporating this into the pricing formula does not improve accuracy. The recommended use is as a **trade filter**: when the regime detector indicates "active," adjust position sizing or entry thresholds rather than changing the FV formula.

### Model 4: Drift/Momentum — HARMFUL

Adding a drift (momentum) term to the pricing model **decreases accuracy by 1%**. This confirms the finding that at the 15-minute timescale, BTC price movements do not exhibit exploitable momentum. The random walk assumption in N(d2) is correct.

### Model 5: Mean-Reverting Volatility — RECOMMENDED

The best-performing model. Uses the insight that volatility mean-reverts over the option lifetime.

**Formula:**
```
sigma_eff = sqrt(a^2 + 2ab(1 - e^{-kT})/(kT) + b^2(1 - e^{-2kT})/(2kT))
```

**Calibrated parameters:**
- `a = sigma_long = 0.000128/sec` (long-run vol level)
- `kappa = 0.00419/sec` (mean-reversion speed; half-life = 165 seconds)

**Performance:**
| Metric | Standard N(d2) | MR-Vol Model |
|---|---|---|
| Brier Score (all) | 0.1572 | **0.1549** |
| Accuracy at conf >= 0.20 | 85.1% | **87.3%** |

The improvement is modest but consistent. At higher confidence thresholds (where trades are actually taken), the MR-Vol model shows a **2.2 percentage point accuracy gain** (87.3% vs 85.1%). For a strategy that trades hundreds of times, this compounds meaningfully.

**Implementation:** Replace the constant sigma in N(d2) with `sigma_eff` computed from the formula above using the calibrated `sigma_long` and `kappa`.

### Model 6: Implied Volatility

Cannot be backtested without live order book data (need real-time bid/ask to extract IV). An "oracle test" was run using the actual realized vol as the IV input — even with **perfect knowledge** of future volatility, accuracy improvement is minimal. This suggests the vol input is not the primary bottleneck for model accuracy; rather, the binary nature of the payoff and the discreteness of price movements are the limiting factors.

---

## Agent a2cb695 — Pair Cost and Entry Timing (FAILED)

**Status:** Hit rate limit. Did not complete. No results available.

This agent was intended to analyze:
- Optimal entry timing relative to pair cost dynamics
- Whether pair cost trajectory predicts final outcome
- Entry timing windows that maximize fill probability and edge

These questions remain open for future research.

---

## Consolidated Action Items

### Implement (High Confidence)

1. **MR-Vol sigma_eff** — Replace constant sigma with mean-reverting vol formula (Model 5). Expected +2.2pp accuracy at tradeable confidence levels.

2. **Hour-specific sigma multipliers** — Replace flat `TOD_SCALE` with implied-vol-calibrated multipliers per hour. Hours 1-2 UTC need 4x, hours 22-23 UTC need 1.5-2x.

3. **Focus trading on T=600s window** — Model outperforms market at 10 minutes remaining (79.7% vs 74.3%). This is the optimal entry window.

4. **Require |ln(S/K)| > 10 bps** — Model accuracy drops below 85% when price is too close to strike. Add a moneyness filter.

### Do NOT Implement

1. **Drift/Momentum (Model 4)** — Harmful. Confirmed: BTC 15-min returns have no exploitable drift.

2. **Jump-Diffusion (Model 1)** — Theoretical benefit does not materialize empirically at this timescale.

3. **Pair cost arbitrage** — Only available 0.13% of the time in the last 60 seconds. Not viable.

### Investigate Further

1. **Regime-switching as trade filter** — Use active/calm regime detector to modulate position sizing, not pricing. Need to design the filter logic and backtest.

2. **Pair cost and entry timing** — Agent failed. Re-run when rate limits allow.

3. **Implied vol live extraction** — Cannot backtest, but could improve live trading. Requires capturing order book snapshots.

---

*Section added: Feb 10, 2026. Source: Agents aec3f0d, a258f79, a2cb695 (Feb 9, 2026 runs).*
