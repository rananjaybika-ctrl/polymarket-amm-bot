"""
Trade log models for recording and analyzing trades.

Provides structured logging for all trading activity with
support for CSV export and statistics calculation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import uuid


@dataclass
class TradeEntry:
    """
    Record of a single trade execution.

    Captures all details needed for analysis and auditing.
    """
    # Trade identification
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    # Timing
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Market info
    market_slug: str = ""
    market_question: str = ""
    condition_id: str = ""

    # Trade details
    side: str = ""  # "UP" or "DOWN"
    action: str = ""  # "BUY" or "SELL"
    price: float = 0.0
    size: float = 0.0
    order_id: str = ""

    # Status
    fill_status: str = "filled"  # "filled", "partial", "cancelled"

    # Grouping
    pair_id: Optional[str] = None  # Links Up/Down in same pair
    session_id: str = ""

    # Optional
    notes: str = ""

    @property
    def cost(self) -> float:
        """Total cost/proceeds of trade."""
        return self.price * self.size

    @property
    def is_buy(self) -> bool:
        """Check if this is a buy order."""
        return self.action.upper() == "BUY"

    @property
    def is_up(self) -> bool:
        """Check if this is an Up token trade."""
        return self.side.upper() == "UP"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "trade_id": self.trade_id,
            "timestamp": self.timestamp.isoformat(),
            "market_slug": self.market_slug,
            "market_question": self.market_question,
            "condition_id": self.condition_id,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "size": self.size,
            "cost": self.cost,
            "order_id": self.order_id,
            "fill_status": self.fill_status,
            "pair_id": self.pair_id,
            "session_id": self.session_id,
            "notes": self.notes,
        }

    def to_csv_row(self) -> list:
        """Convert to CSV row."""
        return [
            self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            self.market_slug,
            self.side,
            self.action,
            f"{self.price:.4f}",
            f"{self.size:.4f}",
            f"{self.cost:.4f}",
            self.order_id,
            self.fill_status,
            self.pair_id or "",
            self.session_id,
            self.notes,
        ]

    @staticmethod
    def csv_headers() -> list:
        """Get CSV column headers."""
        return [
            "timestamp",
            "market",
            "side",
            "action",
            "price",
            "size",
            "cost",
            "order_id",
            "status",
            "pair_id",
            "session_id",
            "notes",
        ]

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"TradeEntry({self.timestamp.strftime('%H:%M:%S')} "
            f"{self.action} {self.size:.1f} {self.side} @ ${self.price:.4f})"
        )


@dataclass
class PairTradeEntry:
    """
    Record of a paired Up/Down trade.

    Groups the two sides of a pair trade for analysis.
    """
    pair_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Market info
    market_slug: str = ""
    market_question: str = ""

    # Up trade details
    up_price: float = 0.0
    up_size: float = 0.0
    up_order_id: str = ""

    # Down trade details
    down_price: float = 0.0
    down_size: float = 0.0
    down_order_id: str = ""

    # Status
    session_id: str = ""
    notes: str = ""

    @property
    def pair_cost(self) -> float:
        """Cost per pair (up_price + down_price)."""
        return self.up_price + self.down_price

    @property
    def pair_count(self) -> float:
        """Number of complete pairs."""
        return min(self.up_size, self.down_size)

    @property
    def total_cost(self) -> float:
        """Total cost of pair trade."""
        return (self.up_price * self.up_size) + (self.down_price * self.down_size)

    @property
    def profit_per_pair(self) -> float:
        """Profit per pair at resolution."""
        return 1.0 - self.pair_cost

    @property
    def total_profit(self) -> float:
        """Total profit at resolution."""
        return self.pair_count * self.profit_per_pair

    @property
    def is_profitable(self) -> bool:
        """Check if trade is profitable."""
        return self.pair_cost < 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "pair_id": self.pair_id,
            "timestamp": self.timestamp.isoformat(),
            "market_slug": self.market_slug,
            "market_question": self.market_question,
            "up_price": self.up_price,
            "up_size": self.up_size,
            "up_order_id": self.up_order_id,
            "down_price": self.down_price,
            "down_size": self.down_size,
            "down_order_id": self.down_order_id,
            "pair_cost": self.pair_cost,
            "pair_count": self.pair_count,
            "total_cost": self.total_cost,
            "profit_per_pair": self.profit_per_pair,
            "total_profit": self.total_profit,
            "is_profitable": self.is_profitable,
            "session_id": self.session_id,
        }

    def to_csv_row(self) -> list:
        """Convert to CSV row."""
        return [
            self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            self.market_slug,
            f"{self.up_price:.4f}",
            f"{self.up_size:.4f}",
            f"{self.down_price:.4f}",
            f"{self.down_size:.4f}",
            f"{self.pair_cost:.4f}",
            f"{self.pair_count:.4f}",
            f"{self.total_cost:.4f}",
            f"{self.profit_per_pair:.4f}",
            f"{self.total_profit:.4f}",
            "Yes" if self.is_profitable else "No",
            self.session_id,
        ]

    @staticmethod
    def csv_headers() -> list:
        """Get CSV column headers."""
        return [
            "timestamp",
            "market",
            "up_price",
            "up_size",
            "down_price",
            "down_size",
            "pair_cost",
            "pair_count",
            "total_cost",
            "profit/pair",
            "total_profit",
            "profitable",
            "session_id",
        ]

    def __repr__(self) -> str:
        """String representation."""
        status = "+" if self.is_profitable else "-"
        return (
            f"PairTrade({self.timestamp.strftime('%H:%M:%S')} "
            f"{self.pair_count:.1f} pairs @ ${self.pair_cost:.4f} "
            f"[{status}${abs(self.total_profit):.4f}])"
        )


@dataclass
class TradeStats:
    """Statistics for a set of trades."""
    total_trades: int = 0
    total_pairs: float = 0.0
    total_cost: float = 0.0
    total_profit: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0

    @property
    def win_rate(self) -> float:
        """Win rate as percentage."""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100

    @property
    def avg_profit_per_trade(self) -> float:
        """Average profit per trade."""
        if self.total_trades == 0:
            return 0.0
        return self.total_profit / self.total_trades

    @property
    def roi(self) -> float:
        """Return on investment as percentage."""
        if self.total_cost == 0:
            return 0.0
        return (self.total_profit / self.total_cost) * 100

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_trades": self.total_trades,
            "total_pairs": self.total_pairs,
            "total_cost": self.total_cost,
            "total_profit": self.total_profit,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "avg_profit_per_trade": self.avg_profit_per_trade,
            "roi": self.roi,
        }
