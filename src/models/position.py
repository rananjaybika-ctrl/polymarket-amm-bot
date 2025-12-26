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
    Unified position model for tracking holdings in Polymarket markets.

    Used by both PaperTradingEngine and LiveTradingEngine. Tracks both
    Up and Down token holdings, entry prices, and calculates P&L.

    This unified class eliminates duplication between PaperPosition and
    LivePosition, ensuring consistent behavior in paper and live modes.
    """
    # Market identifier (required)
    market_slug: str

    # Market object (optional - for token ID access)
    market: Optional[BTCMarket] = None

    # Current balances (share counts)
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

    # Token IDs for live trading (emergency sell capability)
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None

    # Realized P&L (from resolved markets)
    realized_pnl: float = 0.0

    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ==========================================================================
    # PROPERTY ALIASES for compatibility with PaperPosition/LivePosition
    # The bot code uses these names, so we provide aliases to internal fields
    # ==========================================================================

    @property
    def up_size(self) -> float:
        """Alias for up_balance (PaperPosition compatibility)."""
        return self.up_balance

    @property
    def down_size(self) -> float:
        """Alias for down_balance (PaperPosition compatibility)."""
        return self.down_balance

    @property
    def up_shares(self) -> float:
        """Alias for up_balance (LivePosition compatibility)."""
        return self.up_balance

    @property
    def down_shares(self) -> float:
        """Alias for down_balance (LivePosition compatibility)."""
        return self.down_balance

    @property
    def up_cost(self) -> float:
        """Alias for up_total_cost."""
        return self.up_total_cost

    @property
    def down_cost(self) -> float:
        """Alias for down_total_cost."""
        return self.down_total_cost

    # ==========================================================================
    # CORE PROPERTIES - Position metrics and calculations
    # ==========================================================================

    @property
    def pair_count(self) -> float:
        """Number of complete pairs (min of Up and Down)."""
        return min(self.up_balance, self.down_balance)

    @property
    def total_cost(self) -> float:
        """Total cost of position."""
        return self.up_total_cost + self.down_total_cost

    @property
    def pair_cost(self) -> float:
        """Average cost per pair (up_avg + down_avg)."""
        if self.pair_count <= 0:
            return 0.0
        return self.up_avg_price + self.down_avg_price

    @property
    def pair_value(self) -> float:
        """Value of pairs at resolution ($1.00 per pair)."""
        return self.pair_count * 1.0

    @property
    def avg_pair_cost(self) -> float:
        """Alias for pair_cost (LivePosition compatibility)."""
        return self.pair_cost

    # ==========================================================================
    # HEDGING METRICS - Gabagool's strategy calculations
    # ==========================================================================

    @property
    def locked_profit(self) -> float:
        """
        Gabagool's locked profit calculation.

        locked_profit = hedged_pairs × (1.00 - pair_cost)

        This is the GUARANTEED profit if both sides are held to resolution,
        regardless of which side wins.
        """
        hedged_pairs = min(self.up_balance, self.down_balance)
        if hedged_pairs <= 0:
            return 0.0
        avg_pair_cost = self.up_avg_price + self.down_avg_price
        return hedged_pairs * (1.0 - avg_pair_cost)

    @property
    def unrealized_pnl(self) -> float:
        """Alias for locked_profit (for backwards compatibility)."""
        return self.locked_profit

    @property
    def expected_profit(self) -> float:
        """Alias for locked_profit (PaperPosition compatibility)."""
        return self.locked_profit

    @property
    def unrealized_pnl_percent(self) -> float:
        """Unrealized P&L as percentage of cost."""
        if self.total_cost <= 0:
            return 0.0
        return (self.unrealized_pnl / self.total_cost) * 100

    @property
    def imbalance(self) -> float:
        """
        Position imbalance using gabagool's formula.

        imbalance = |UP - DOWN| / max(UP, DOWN)
        Returns value between 0 (balanced) and 1 (fully one-sided).
        """
        max_side = max(self.up_balance, self.down_balance)
        if max_side == 0:
            return 0.0
        return abs(self.up_balance - self.down_balance) / max_side

    # ==========================================================================
    # UNHEDGED EXPOSURE - Track directional risk
    # ==========================================================================

    @property
    def unhedged_up_size(self) -> float:
        """Number of unhedged UP shares."""
        return max(0, self.up_balance - self.down_balance)

    @property
    def unhedged_down_size(self) -> float:
        """Number of unhedged DOWN shares."""
        return max(0, self.down_balance - self.up_balance)

    @property
    def unmatched_up(self) -> float:
        """Alias for unhedged_up_size."""
        return self.unhedged_up_size

    @property
    def unmatched_down(self) -> float:
        """Alias for unhedged_down_size."""
        return self.unhedged_down_size

    @property
    def unhedged_exposure_side(self) -> Optional[str]:
        """Which side has unhedged exposure, or None if balanced."""
        if self.up_balance > self.down_balance:
            return "UP"
        elif self.down_balance > self.up_balance:
            return "DOWN"
        return None

    @property
    def deficit_side(self) -> Optional[str]:
        """Which side has fewer shares, or None if balanced."""
        if self.up_balance < self.down_balance:
            return "UP"
        elif self.down_balance < self.up_balance:
            return "DOWN"
        return None

    @property
    def is_balanced(self) -> bool:
        """Check if position is balanced (equal Up and Down)."""
        return abs(self.up_balance - self.down_balance) < 0.01

    @property
    def has_exposure(self) -> bool:
        """Check if position has unbalanced exposure."""
        return self.unhedged_up_size > 0 or self.unhedged_down_size > 0

    # ==========================================================================
    # P&L CALCULATION METHODS
    # ==========================================================================

    def calculate_expected_pnl_range(self) -> tuple:
        """
        Calculate expected P&L range at resolution.

        Returns (min_pnl, max_pnl, locked_pnl):
        - min_pnl: P&L if the unhedged side LOSES
        - max_pnl: P&L if the unhedged side WINS
        - locked_pnl: Guaranteed profit from hedged pairs
        """
        locked = self.locked_profit

        if self.unhedged_up_size > 0:
            # We have extra UP shares
            unhedged_cost = self.unhedged_up_size * self.up_avg_price
            # If UP loses, we lose the cost of unhedged UP
            min_pnl = locked - unhedged_cost
            # If UP wins, unhedged UP pays $1 each
            max_pnl = locked + (self.unhedged_up_size * 1.0 - unhedged_cost)
        elif self.unhedged_down_size > 0:
            # We have extra DOWN shares
            unhedged_cost = self.unhedged_down_size * self.down_avg_price
            # If DOWN loses, we lose the cost of unhedged DOWN
            min_pnl = locked - unhedged_cost
            # If DOWN wins, unhedged DOWN pays $1 each
            max_pnl = locked + (self.unhedged_down_size * 1.0 - unhedged_cost)
        else:
            # Perfectly hedged
            min_pnl = locked
            max_pnl = locked

        return (min_pnl, max_pnl, locked)

    def calculate_position_value(self, current_up_price: float, current_down_price: float) -> float:
        """
        Calculate current market value of the entire position.

        Args:
            current_up_price: Current UP ask price
            current_down_price: Current DOWN ask price

        Returns:
            Total market value of position
        """
        return self.up_balance * current_up_price + self.down_balance * current_down_price

    def calculate_true_pnl(self, current_up_price: float, current_down_price: float) -> float:
        """
        Calculate true P&L including position value at current prices.

        Args:
            current_up_price: Current UP price (use bid for conservative)
            current_down_price: Current DOWN price (use bid for conservative)

        Returns:
            True P&L at current market prices
        """
        position_value = self.calculate_position_value(current_up_price, current_down_price)
        return position_value - self.total_cost

    # ==========================================================================
    # PROSPECTIVE PAIR COST - Core of gabagool's strategy
    # ==========================================================================

    def calculate_prospective_pair_cost(
        self,
        side: str,
        buy_price: float,
        buy_qty: float,
    ) -> float:
        """
        Calculate what the average pair cost would be AFTER a prospective buy.

        This is the core of gabagool's strategy: before buying, check if the
        new average pair cost would still be below the profit threshold.

        Args:
            side: "UP" or "DOWN" - which side we're considering buying
            buy_price: The ask price we'd pay
            buy_qty: How many shares we'd buy

        Returns:
            The prospective average pair cost after this buy.
        """
        # Project new state after buy
        if side.upper() == "UP":
            new_up_cost = self.up_total_cost + (buy_price * buy_qty)
            new_up_size = self.up_balance + buy_qty
            new_down_cost = self.down_total_cost
            new_down_size = self.down_balance
        else:  # DOWN
            new_up_cost = self.up_total_cost
            new_up_size = self.up_balance
            new_down_cost = self.down_total_cost + (buy_price * buy_qty)
            new_down_size = self.down_balance + buy_qty

        # Calculate hedged quantity (pairs we could form)
        hedged_qty = min(new_up_size, new_down_size)

        if hedged_qty <= 0:
            # First buy - return buy price as prospective cost
            return buy_price

        # Calculate average price for each side
        avg_up = new_up_cost / new_up_size if new_up_size > 0 else 0
        avg_down = new_down_cost / new_down_size if new_down_size > 0 else 0

        return avg_up + avg_down

    def would_improve_pair_cost(
        self,
        side: str,
        buy_price: float,
        buy_qty: float,
        threshold: float = 0.99,
    ) -> bool:
        """
        Check if a prospective buy would keep pair cost below threshold.

        Args:
            side: "UP" or "DOWN"
            buy_price: The ask price
            buy_qty: How many to buy
            threshold: Maximum acceptable pair cost (default 0.99)

        Returns:
            True if buying would result in pair_cost < threshold
        """
        prospective = self.calculate_prospective_pair_cost(side, buy_price, buy_qty)
        return prospective < threshold

    def calculate_max_recovery_price(self, side: str, safety_margin: float = 0.01) -> float:
        """
        Calculate maximum price we can pay to recover the deficit side
        while maintaining pair_cost < 1.00.

        Args:
            side: "UP" or "DOWN" - the side we need more of
            safety_margin: Buffer below $1.00 (default 0.01)

        Returns:
            Maximum price we should pay for recovery shares.
        """
        side_upper = side.upper()

        if side_upper == "UP":
            if self.down_balance <= 0:
                return 0.0
            return max(0.0, 1.00 - self.down_avg_price - safety_margin)
        else:  # DOWN
            if self.up_balance <= 0:
                return 0.0
            return max(0.0, 1.00 - self.up_avg_price - safety_margin)

    # ==========================================================================
    # FILL RECORDING AND STATE UPDATES
    # ==========================================================================

    def add_fill(
        self,
        side: str,
        price: float,
        size: float,
        cost: Optional[float] = None,
        order_id: Optional[str] = None,
    ) -> Optional[Fill]:
        """
        Record a new fill and update position.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
            cost: Optional explicit cost (if None, calculated as price * size)
            order_id: Optional order ID

        Returns:
            The Fill object created, or None if no market is set
        """
        # Calculate cost if not provided
        fill_cost = cost if cost is not None else price * size

        # Get token ID from market if available
        token_id = ""
        if self.market:
            token_id = self.market.up_token_id if side.upper() == "UP" else self.market.down_token_id
        elif self.up_token_id and side.upper() == "UP":
            token_id = self.up_token_id
        elif self.down_token_id and side.upper() == "DOWN":
            token_id = self.down_token_id

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
            new_total_cost = self.up_total_cost + fill_cost
            new_balance = self.up_balance + size
            self.up_avg_price = new_total_cost / new_balance if new_balance > 0 else price
            self.up_balance = new_balance
            self.up_total_cost = new_total_cost
        else:
            new_total_cost = self.down_total_cost + fill_cost
            new_balance = self.down_balance + size
            self.down_avg_price = new_total_cost / new_balance if new_balance > 0 else price
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
            "market_slug": self.market_slug,
            "up_size": self.up_balance,
            "down_size": self.down_balance,
            "up_avg_price": self.up_avg_price,
            "down_avg_price": self.down_avg_price,
            "pair_count": self.pair_count,
            "pair_cost": self.pair_cost,
            "total_cost": self.total_cost,
            "locked_profit": self.locked_profit,
            "expected_profit": self.expected_profit,
            "imbalance": self.imbalance,
            "is_balanced": self.is_balanced,
            "fills_count": len(self.fills),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"Position({self.market_slug}: "
            f"Up={self.up_balance:.1f}@${self.up_avg_price:.4f}, "
            f"Down={self.down_balance:.1f}@${self.down_avg_price:.4f}, "
            f"PnL=${self.locked_profit:.4f})"
        )
