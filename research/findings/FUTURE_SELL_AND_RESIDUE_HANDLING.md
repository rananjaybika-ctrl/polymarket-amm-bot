# Future Task: SELL Orders & Residue Share Handling

**Created:** Feb 4, 2026
**Status:** NOT NEEDED (fees deducted from cash, not shares)
**Priority:** Low - implement only if residue shares become a problem

---

## Executive Summary

This document captures research on implementing SELL orders and residue share cleanup. **Current conclusion: NOT NEEDED** because Polymarket deducts taker fees from USDC cash, not from shares received. Taker orders should always receive full share fills.

The partial fill issue observed on Feb 4, 2026 (4/10 shares) was caused by a **paper trading config bug** (`partial_fill_rate=0.10`), not by Polymarket fee structure. This has been fixed.

---

## Table of Contents

1. [Polymarket Fee Structure](#1-polymarket-fee-structure)
2. [Minimum Order Constraints](#2-minimum-order-constraints)
3. [SELL Order Infrastructure](#3-sell-order-infrastructure)
4. [Residue Share Handling Design](#4-residue-share-handling-design)
5. [Implementation Guide](#5-implementation-guide)
6. [File References](#6-file-references)

---

## 1. Polymarket Fee Structure

### Key Finding: Fees Deducted from CASH, Not Shares

When placing a taker (market) order:
- You specify the **number of shares** you want to buy
- Polymarket calculates cost: `cash_paid = shares × price`
- Taker fee is **additional USDC cost**: `fee = cash_paid × fee_rate`
- **You receive the full number of shares requested**

### Fee Formula

```python
def polymarket_taker_fee(price: float) -> float:
    """Polymarket taker fee: 1.56% * (1 - |2*price - 1|)"""
    return 0.0156 * (1 - abs(2 * price - 1))
```

Equivalently: `fee = 1000 bps × 4 × price × (1 - price)`

### Fee Schedule by Price

| Price | Fee Rate | Example (100 shares) |
|-------|----------|---------------------|
| $0.50 | 1.56% (max) | $50.00 + $0.78 fee = $50.78 |
| $0.25 / $0.75 | 1.17% | $25.00 + $0.29 fee = $25.29 |
| $0.10 / $0.90 | 0.70% | $10.00 + $0.07 fee = $10.07 |
| $0.01 / $0.99 | ~0.00% | Near zero fee |

### Config Constants

```python
# From src/config.py
TAKER_FEE_BPS = 1000  # Base 10% (1000 basis points)
MAX_TAKER_FEE_RATE = 0.0156  # 1.56% cap at 50% probability
```

### Source Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/config.py` | 283-426 | FeeConfig class with fee calculations |
| `src/config.py` | 315 | Fee formula documentation |
| `src/config.py` | 329-341 | `get_taker_fee_amount()` implementation |
| `src/core/trading_utils.py` | 53-65 | `polymarket_taker_fee()` function |
| `src/core/trading_utils.py` | 68-99 | `calculate_pnl_with_fees()` function |
| `vendor/polymarket_apis/clients/clob_client.py` | 236-246 | `get_fee_rate_bps()` API call |

---

## 2. Minimum Order Constraints

### Constants

```python
MIN_ORDER_SHARES = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0     # Minimum $1 order value
```

### Validation Logic

**Both constraints must be satisfied (AND logic):**

```python
def validate_order(shares: int, price: float) -> bool:
    if shares < MIN_ORDER_SHARES:
        return False  # Fails share minimum
    if shares * price < MIN_ORDER_VALUE:
        return False  # Fails value minimum
    return True
```

### Examples

| Shares | Price | Value | Valid? | Reason |
|--------|-------|-------|--------|--------|
| 5 | $0.20 | $1.00 | Yes | Minimum valid order |
| 5 | $0.21 | $1.05 | Yes | Above both minimums |
| 4 | $0.30 | $1.20 | No | Fails share minimum |
| 10 | $0.05 | $0.50 | No | Fails value minimum |
| 6 | $0.15 | $0.90 | No | Fails value minimum |

### Source Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/services/paper_trading.py` | 34-36 | Constants definition |
| `src/services/paper_trading.py` | 439-468 | Pair trade validation |
| `src/services/paper_trading.py` | 644-669 | Single-side validation |
| `src/services/live_trading.py` | 28-30 | Constants definition |
| `src/services/live_trading.py` | 543-564 | Order validation |
| `research/backtests/comprehensive_strategy_backtest.py` | 35-50 | Backtest validation |

---

## 3. SELL Order Infrastructure

### Current State: BUY-Only Implementation

All order placement currently hardcodes `side="BUY"`:

```python
# src/services/live_trading.py line 579
result = await self.client.place_order(
    token_id=token_id,
    side="BUY",  # <-- HARDCODED
    price=price,
    size=size,
)
```

### Infrastructure Status

| Component | SELL Support | Notes |
|-----------|--------------|-------|
| `vendor/polymarket_apis/` | **FULL** | API fully supports SELL |
| `clob_client.py` | **FULL** | `calculate_sell_market_price()` exists |
| `order_builder.py` | **FULL** | Handles BUY/SELL amount calculation |
| `polymarket_client.py` | **READY** | Accepts `side` parameter |
| `live_trading.py` | **HARDCODED BUY** | Needs modification |
| `paper_trading.py` | **HARDCODED BUY** | Needs modification |

### Key Differences: BUY vs SELL

```python
# ORDER BUILDER (vendor/polymarket_apis/utilities/order_builder/builder.py)

# For BUY orders:
#   taker_amount = shares (what you're buying)
#   maker_amount = USDC (what you're paying)
#   Use ASKS for price calculation

# For SELL orders:
#   maker_amount = shares (what you're selling)
#   taker_amount = USDC (what you're receiving)
#   Use BIDS for price calculation
```

### Existing SELL Example

```python
# scripts/sell_all_positions.py line 152
result = await client.place_order(
    token_id=token_id,
    side="SELL",  # <-- SELL works!
    price=sell_price,
    size=size,
)
```

### Source Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/services/live_trading.py` | 579, 935 | Hardcoded BUY (needs change) |
| `src/services/paper_trading.py` | 446-494 | Hardcoded BUY (needs change) |
| `src/services/order_executor.py` | 24-27 | Unused OrderSide enum |
| `src/api/polymarket_client.py` | 493-536 | `create_order()` accepts side |
| `src/api/polymarket_client.py` | 538-593 | `place_order()` accepts side |
| `vendor/polymarket_apis/utilities/order_builder/builder.py` | 56-92 | `get_order_amounts()` BUY/SELL |
| `vendor/polymarket_apis/utilities/order_builder/builder.py` | 236-258 | `calculate_sell_market_price()` |
| `vendor/polymarket_apis/clients/clob_client.py` | 643-670 | `calculate_market_price()` BUY/SELL |
| `vendor/polymarket_apis/utilities/constants.py` | 26-27 | BUY/SELL constants |
| `scripts/sell_all_positions.py` | 152 | Working SELL example |

---

## 4. Residue Share Handling Design

### When Would Residue Occur?

Residue shares (orphaned, unhedged shares) could occur from:
1. ~~Partial fills due to taker fees~~ (NOT TRUE - fees from cash)
2. Paper trading `partial_fill_rate > 0` (FIXED - now 0.0)
3. Failed hedge orders leaving entry unmatched
4. Network/API errors mid-cycle

### Detection Logic

```python
def detect_residue(state):
    """Check for unbalanced positions after cycle completion."""
    if state.first_fill_side is not None:
        return None  # Still in active cycle

    residue_up = state.up_shares - state.down_shares
    residue_down = state.down_shares - state.up_shares

    if residue_up > 0:
        return {"side": "UP", "shares": residue_up}
    elif residue_down > 0:
        return {"side": "DOWN", "shares": residue_down}
    return None
```

### Cleanup Strategies

#### Option A: Ride to Resolution (Current Approach)
- Let residue resolve with market outcome
- If UP residue and market resolves UP → profit ($1/share)
- If UP residue and market resolves DOWN → loss ($0/share)
- **Best for:** Small test sizes (10 shares), low residue frequency

#### Option B: Sell Residue (Future Implementation)
```python
async def sell_residue(self, market, side: str, shares: int) -> bool:
    """Sell residue shares to clean up position."""
    # Validate minimum order constraints
    if shares < MIN_ORDER_SHARES:
        logger.info(f"Residue {shares} < min {MIN_ORDER_SHARES}, letting ride")
        return False

    # Get current bid price
    bid_price = await self.get_best_bid(market, side)

    if shares * bid_price < MIN_ORDER_VALUE:
        logger.info(f"Residue value ${shares * bid_price:.2f} < min ${MIN_ORDER_VALUE}")
        return False

    # Place SELL order
    result = await self.client.place_order(
        token_id=market.up_token_id if side == "UP" else market.down_token_id,
        side="SELL",
        price=bid_price,
        size=shares,
    )

    if result.get("success"):
        # Update position (subtract shares)
        self.state.subtract_shares(side, shares)
        logger.info(f"Sold {shares} {side} residue @ ${bid_price}")
        return True
    return False
```

#### Option C: Hedge Residue (Buy Other Side)
```python
async def hedge_residue(self, market, residue_side: str, shares: int) -> bool:
    """Hedge residue by buying the other side."""
    hedge_side = "DOWN" if residue_side == "UP" else "UP"

    # Get current ask for hedge side
    ask_price = await self.get_best_ask(market, hedge_side)

    # Place BUY order for other side
    result = await self.execute_single_side_trade(
        market=market,
        side=hedge_side,
        price=ask_price,
        size=shares,
    )

    if result.get("success"):
        # Now we have balanced pairs that can merge
        logger.info(f"Hedged {shares} {residue_side} residue by buying {hedge_side}")
        return True
    return False
```

### Recommended Approach by Size

| Trade Size | Strategy | Rationale |
|------------|----------|-----------|
| 10 shares | Ride to resolution | Low capital at risk, simple |
| 25 shares | Hybrid | Hedge if residue > 10, else ride |
| 50+ shares | Active cleanup | Sell or hedge residue immediately |

---

## 5. Implementation Guide

### If SELL Implementation Needed

#### Step 1: Add `sell_shares()` to Trading Engines

```python
# src/services/live_trading.py

async def sell_shares(
    self,
    market: BTCMarket,
    side: str,  # "UP" or "DOWN"
    size: int,
    min_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Sell shares from existing position.

    FIRST SELL IMPLEMENTATION IN CODEBASE.

    Args:
        market: Market to sell in
        side: "UP" or "DOWN" - which position to sell
        size: Number of shares to sell
        min_price: Minimum acceptable price (optional)

    Returns:
        Dict with success, filled_size, filled_price, proceeds
    """
    # Validate we have shares to sell
    current_shares = self.get_position_shares(market, side)
    if size > current_shares:
        return {"success": False, "error": f"Insufficient shares: {current_shares} < {size}"}

    # Validate minimum order constraints
    if size < MIN_ORDER_SHARES:
        return {"success": False, "error": f"Size {size} < min {MIN_ORDER_SHARES}"}

    # Get current bid (NOT ask - we're selling)
    token_id = market.up_token_id if side == "UP" else market.down_token_id
    bid_price = await self.get_best_bid(token_id)

    if size * bid_price < MIN_ORDER_VALUE:
        return {"success": False, "error": f"Value ${size * bid_price:.2f} < min ${MIN_ORDER_VALUE}"}

    if min_price and bid_price < min_price:
        return {"success": False, "error": f"Bid ${bid_price} < min ${min_price}"}

    # Place SELL order
    result = await self.client.place_order(
        token_id=token_id,
        side="SELL",  # <-- KEY DIFFERENCE
        price=bid_price,
        size=size,
    )

    if result.get("success"):
        filled_size = result.get("filled_size", size)
        filled_price = result.get("filled_price", bid_price)
        proceeds = filled_size * filled_price

        # Update position (SUBTRACT, not add)
        self._subtract_position(market, side, filled_size)

        logger.info(f"SELL {side} {filled_size} @ ${filled_price:.4f} = ${proceeds:.2f}")

        return {
            "success": True,
            "filled_size": filled_size,
            "filled_price": filled_price,
            "proceeds": proceeds,
        }

    return {"success": False, "error": result.get("error", "Unknown error")}
```

#### Step 2: Add Position Subtraction

```python
def _subtract_position(self, market: BTCMarket, side: str, shares: int):
    """Subtract shares from position (for SELL orders)."""
    if side == "UP":
        self._positions[market.slug]["up_shares"] -= shares
    else:
        self._positions[market.slug]["down_shares"] -= shares
```

#### Step 3: Add Residue Cleanup Trigger

```python
async def cleanup_residue_on_rotation(self, old_market: BTCMarket):
    """Clean up any residue shares when rotating away from a market."""
    residue = self.detect_residue(old_market)
    if residue is None:
        return

    side, shares = residue["side"], residue["shares"]

    if shares >= MIN_ORDER_SHARES:
        # Try to sell
        result = await self.sell_shares(old_market, side, shares)
        if result["success"]:
            logger.info(f"Cleaned up {shares} {side} residue from {old_market.slug}")
        else:
            logger.warning(f"Failed to clean residue: {result['error']}, letting ride")
```

---

## 6. File References

### Primary Implementation Files

| File | Purpose | Key Lines |
|------|---------|-----------|
| `src/services/live_trading.py` | Live order execution | 579, 935 (hardcoded BUY) |
| `src/services/paper_trading.py` | Paper order simulation | 446-494 (hardcoded BUY) |
| `src/api/polymarket_client.py` | API wrapper | 493-593 (supports side param) |
| `src/config.py` | Fee configuration | 283-426 |
| `src/core/trading_utils.py` | Fee calculations | 53-99 |

### Vendor API Files

| File | Purpose | Key Lines |
|------|---------|-----------|
| `vendor/polymarket_apis/clients/clob_client.py` | CLOB API | 643-670 (BUY/SELL price calc) |
| `vendor/polymarket_apis/utilities/order_builder/builder.py` | Order building | 56-130 (amount calc), 236-258 (sell price) |
| `vendor/polymarket_apis/utilities/constants.py` | Constants | 26-27 (BUY/SELL strings) |
| `vendor/polymarket_apis/types/clob_types.py` | Type definitions | 58, 266, 414-455 |

### Example/Reference Files

| File | Purpose |
|------|---------|
| `scripts/sell_all_positions.py` | Working SELL order example |
| `research/backtests/comprehensive_strategy_backtest.py` | Order validation example |

---

## Conclusion

**Current Status:** No action needed. Taker fees are deducted from USDC cash, not shares. With `partial_fill_rate=0.0` and `fill_probability=1.0` in paper trading config, residue shares should not occur.

**Future Action:** If residue shares become a problem at larger sizes (50+ shares), implement the SELL functionality following this guide. The infrastructure is ready - only the trading engine hardcoded BUY needs to change.

---

*Document created: Feb 4, 2026*
*Last updated: Feb 4, 2026*
*Author: Claude Code Assistant*
