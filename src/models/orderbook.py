"""
Orderbook data models.

Represents orderbook data from Polymarket CLOB API for trading analysis.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from decimal import Decimal


@dataclass
class Order:
    """
    Represents a single order in the orderbook.

    Attributes:
        price: Order price (0.0 to 1.0 for binary markets)
        size: Number of shares at this price level
    """

    price: float
    size: float

    @classmethod
    def from_order_summary(cls, summary) -> "Order":
        """
        Create Order from py_clob_client OrderSummary.

        Args:
            summary: OrderSummary object with price and size strings

        Returns:
            Order instance
        """
        return cls(
            price=float(summary.price),
            size=float(summary.size),
        )

    @property
    def value(self) -> float:
        """Total value of this order level (price * size)."""
        return self.price * self.size

    def __repr__(self) -> str:
        return f"Order(${self.price:.2f} x {self.size:.1f})"


@dataclass
class Orderbook:
    """
    Represents an orderbook for a single token.

    Contains all bid and ask orders sorted by price.
    Bids are sorted descending (best bid first).
    Asks are sorted ascending (best ask first).

    Attributes:
        token_id: The token ID this orderbook represents
        bids: List of bid orders (buy orders), best first
        asks: List of ask orders (sell orders), best first
        timestamp: When the orderbook was fetched (unix ms)
    """

    token_id: str
    bids: List[Order] = field(default_factory=list)
    asks: List[Order] = field(default_factory=list)
    timestamp: int = 0

    @classmethod
    def from_clob_response(cls, response) -> "Orderbook":
        """
        Create Orderbook from py_clob_client OrderBookSummary.

        Args:
            response: OrderBookSummary from get_order_book()

        Returns:
            Orderbook instance
        """
        bids = [Order.from_order_summary(o) for o in response.bids]
        asks = [Order.from_order_summary(o) for o in response.asks]

        # Ensure proper sorting
        bids.sort(key=lambda o: o.price, reverse=True)  # Highest first
        asks.sort(key=lambda o: o.price)  # Lowest first

        return cls(
            token_id=response.asset_id,
            bids=bids,
            asks=asks,
            timestamp=int(response.timestamp) if response.timestamp else 0,
        )

    @property
    def best_bid(self) -> Optional[float]:
        """Highest bid price, or None if no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Lowest ask price, or None if no asks."""
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> float:
        """Size available at best bid."""
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        """Size available at best ask."""
        return self.asks[0].size if self.asks else 0.0

    @property
    def spread(self) -> Optional[float]:
        """
        Bid-ask spread.

        Returns:
            Spread as decimal (e.g., 0.02 for 2 cent spread),
            or None if either side is empty
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid_price(self) -> Optional[float]:
        """
        Mid-market price.

        Returns:
            Average of best bid and ask, or None if either side is empty
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def compute_imbalance(self, levels: int = 5) -> float:
        """
        Compute orderbook imbalance from top N levels.

        Imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        Range: -1 (all asks) to +1 (all bids)

        Positive imbalance = more buying pressure = price likely to rise
        Negative imbalance = more selling pressure = price likely to fall

        Args:
            levels: Number of price levels to include (default 5)

        Returns:
            Imbalance value between -1 and 1, or 0 if no depth
        """
        bid_depth = sum(o.size for o in self.bids[:levels])
        ask_depth = sum(o.size for o in self.asks[:levels])
        total = bid_depth + ask_depth

        if total == 0:
            return 0.0

        return (bid_depth - ask_depth) / total

    def depth_at_price(self, price: float, side: str = "ask") -> float:
        """
        Get total size available at or better than a price.

        Args:
            price: Price threshold
            side: "bid" or "ask"

        Returns:
            Total shares available at or better than price
        """
        if side == "bid":
            return sum(o.size for o in self.bids if o.price >= price)
        else:
            return sum(o.size for o in self.asks if o.price <= price)

    def size_for_cost(self, max_cost: float, side: str = "ask") -> float:
        """
        Calculate how many shares can be bought/sold for a given cost.

        Args:
            max_cost: Maximum total cost in dollars
            side: "bid" (selling) or "ask" (buying)

        Returns:
            Number of shares that can be traded
        """
        orders = self.asks if side == "ask" else self.bids
        total_size = 0.0
        remaining_cost = max_cost

        for order in orders:
            order_cost = order.price * order.size
            if order_cost <= remaining_cost:
                total_size += order.size
                remaining_cost -= order_cost
            else:
                # Partial fill at this level
                shares_at_level = remaining_cost / order.price
                total_size += shares_at_level
                break

        return total_size

    def cost_for_size(self, size: float, side: str = "ask") -> float:
        """
        Calculate total cost to buy/sell a given number of shares.

        Args:
            size: Number of shares
            side: "bid" (selling) or "ask" (buying)

        Returns:
            Total cost in dollars
        """
        orders = self.asks if side == "ask" else self.bids
        remaining_size = size
        total_cost = 0.0

        for order in orders:
            if remaining_size <= 0:
                break

            fill_size = min(order.size, remaining_size)
            total_cost += order.price * fill_size
            remaining_size -= fill_size

        return total_cost

    def has_liquidity(self) -> bool:
        """Check if orderbook has orders on both sides."""
        return bool(self.bids and self.asks)

    def is_garbage(self) -> bool:
        """
        Detect invalid/stale orderbook data from CLOB API.

        Only flags truly invalid data:
        - No bids or asks (empty orderbook)
        - None values preventing trading

        NOTE: Extreme prices ($0.01/$0.99) are NOT garbage - they're
        legitimate near market resolution or after big BTC moves.

        Returns:
            True if orderbook data is invalid (empty/None)
        """
        # No orders = can't trade
        if not self.bids or not self.asks:
            return True

        # Check for None values in best prices
        if self.best_bid is None or self.best_ask is None:
            return True

        return False

    def __repr__(self) -> str:
        bid_str = f"${self.best_bid:.2f}" if self.best_bid else "None"
        ask_str = f"${self.best_ask:.2f}" if self.best_ask else "None"
        return f"Orderbook({bid_str}/{ask_str}, {len(self.bids)} bids, {len(self.asks)} asks)"

    def __str__(self) -> str:
        lines = [f"Orderbook for {self.token_id[:20]}..."]

        if self.spread is not None:
            lines.append(f"  Spread: {self.spread:.2%}")
            lines.append(f"  Mid: ${self.mid_price:.4f}")

        lines.append(f"  Best Bid: ${self.best_bid:.4f} x {self.best_bid_size:.1f}" if self.best_bid else "  Best Bid: None")
        lines.append(f"  Best Ask: ${self.best_ask:.4f} x {self.best_ask_size:.1f}" if self.best_ask else "  Best Ask: None")

        return "\n".join(lines)
