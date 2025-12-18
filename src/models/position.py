"""
Position model for tracking holdings in Polymarket markets.

A position represents the tokens held for a specific BTC Up/Down market,
including both Up and Down tokens, average prices, and P&L calculations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from src.models.market import BTCMarket


@dataclass
class Fill:
    """Record of a single trade fill."""
    token_id: str
    side: str  # "UP" or "DOWN"
    price: float
    size: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: Optional[str] = None

    @property
    def cost(self) -> float:
        """Total cost of this fill."""
        return self.price * self.size

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "cost": self.cost,
            "timestamp": self.timestamp.isoformat(),
            "order_id": self.order_id,
        }


@dataclass
class Position:
    """
    Position in a single BTC Up/Down market.

    Tracks both Up and Down token holdings, entry prices,
    and calculates P&L.
    """
    market: BTCMarket

    # Current balances (from chain)
    up_balance: float = 0.0
    down_balance: float = 0.0

    # Average entry prices (calculated from fills)
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0

    # Total costs (for P&L calculation)
    up_total_cost: float = 0.0
    down_total_cost: float = 0.0

    # Fill history
    fills: List[Fill] = field(default_factory=list)

    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def pair_count(self) -> float:
        """Number of complete pairs (min of Up and Down)."""
        return min(self.up_balance, self.down_balance)

    @property
    def unmatched_up(self) -> float:
        """Unmatched Up tokens (exposure to Up)."""
        return max(0, self.up_balance - self.down_balance)

    @property
    def unmatched_down(self) -> float:
        """Unmatched Down tokens (exposure to Down)."""
        return max(0, self.down_balance - self.up_balance)

    @property
    def is_balanced(self) -> bool:
        """Check if position is balanced (equal Up and Down)."""
        return abs(self.up_balance - self.down_balance) < 0.001

    @property
    def total_cost(self) -> float:
        """Total cost of position."""
        return self.up_total_cost + self.down_total_cost

    @property
    def pair_cost(self) -> float:
        """Average cost per pair."""
        if self.pair_count <= 0:
            return 0.0
        # For balanced positions, cost = up_avg + down_avg
        return self.up_avg_price + self.down_avg_price

    @property
    def pair_value(self) -> float:
        """Value of pairs at resolution ($1.00 per pair)."""
        return self.pair_count * 1.0

    @property
    def unrealized_pnl(self) -> float:
        """
        Unrealized P&L for balanced pairs.

        For pairs: guaranteed $1.00 at resolution, so PnL = $1.00 - pair_cost
        """
        if self.pair_count <= 0:
            return 0.0
        return self.pair_count * (1.0 - self.pair_cost)

    @property
    def unrealized_pnl_percent(self) -> float:
        """Unrealized P&L as percentage of cost."""
        if self.total_cost <= 0:
            return 0.0
        return (self.unrealized_pnl / self.total_cost) * 100

    @property
    def has_exposure(self) -> bool:
        """Check if position has unbalanced exposure."""
        return self.unmatched_up > 0 or self.unmatched_down > 0

    def add_fill(self, side: str, price: float, size: float, order_id: Optional[str] = None) -> Fill:
        """
        Record a new fill and update position.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
            order_id: Optional order ID

        Returns:
            The Fill object created
        """
        token_id = self.market.up_token_id if side.upper() == "UP" else self.market.down_token_id

        fill = Fill(
            token_id=token_id,
            side=side.upper(),
            price=price,
            size=size,
            order_id=order_id,
        )
        self.fills.append(fill)

        # Update balances and averages
        if side.upper() == "UP":
            new_total_cost = self.up_total_cost + fill.cost
            new_balance = self.up_balance + size
            self.up_avg_price = new_total_cost / new_balance if new_balance > 0 else 0
            self.up_balance = new_balance
            self.up_total_cost = new_total_cost
        else:
            new_total_cost = self.down_total_cost + fill.cost
            new_balance = self.down_balance + size
            self.down_avg_price = new_total_cost / new_balance if new_balance > 0 else 0
            self.down_balance = new_balance
            self.down_total_cost = new_total_cost

        self.updated_at = datetime.now(timezone.utc)
        return fill

    def sync_balances(self, up_balance: float, down_balance: float) -> None:
        """
        Sync balances from chain state.

        Note: This updates balances but not costs/averages.
        Use when reconciling with on-chain state.
        """
        self.up_balance = up_balance
        self.down_balance = down_balance
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "market": self.market.slug,
            "up_balance": self.up_balance,
            "down_balance": self.down_balance,
            "up_avg_price": self.up_avg_price,
            "down_avg_price": self.down_avg_price,
            "pair_count": self.pair_count,
            "pair_cost": self.pair_cost,
            "total_cost": self.total_cost,
            "unrealized_pnl": self.unrealized_pnl,
            "is_balanced": self.is_balanced,
            "fills_count": len(self.fills),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"Position({self.market.slug}: "
            f"Up={self.up_balance:.1f}@${self.up_avg_price:.4f}, "
            f"Down={self.down_balance:.1f}@${self.down_avg_price:.4f}, "
            f"PnL=${self.unrealized_pnl:.4f})"
        )
