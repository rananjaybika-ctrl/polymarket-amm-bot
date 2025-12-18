"""
Position Tracker service for managing trading positions.

Tracks holdings across multiple markets, calculates P&L,
and provides inventory management for the trading bot.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

from src.api.polymarket_client import PolymarketClient
from src.models.market import BTCMarket
from src.models.position import Position, Fill


logger = logging.getLogger(__name__)


@dataclass
class PortfolioSummary:
    """Summary of all positions."""
    total_positions: int = 0
    total_pairs: float = 0.0
    total_cost: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_exposure_up: float = 0.0
    total_exposure_down: float = 0.0
    usdc_balance: float = 0.0

    @property
    def total_value(self) -> float:
        """Total portfolio value (pairs at $1 + USDC)."""
        return self.total_pairs + self.usdc_balance

    @property
    def total_pnl_percent(self) -> float:
        """Total P&L as percentage."""
        if self.total_cost <= 0:
            return 0.0
        return (self.total_unrealized_pnl / self.total_cost) * 100

    @property
    def is_balanced(self) -> bool:
        """Check if portfolio has no directional exposure."""
        return self.total_exposure_up < 0.001 and self.total_exposure_down < 0.001

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_positions": self.total_positions,
            "total_pairs": self.total_pairs,
            "total_cost": self.total_cost,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "total_exposure_up": self.total_exposure_up,
            "total_exposure_down": self.total_exposure_down,
            "usdc_balance": self.usdc_balance,
            "total_value": self.total_value,
            "is_balanced": self.is_balanced,
        }


class PositionTracker:
    """
    Tracks and manages trading positions across markets.

    Provides:
    - Position tracking per market
    - P&L calculation
    - Portfolio summary
    - Balance synchronization with chain

    Example:
        tracker = PositionTracker(client)

        # Sync position from chain
        position = await tracker.sync_position(market)

        # Record a fill
        tracker.add_fill(market, "UP", price=0.51, size=10)

        # Get portfolio summary
        summary = await tracker.get_portfolio_summary()
    """

    def __init__(self, client: PolymarketClient):
        """
        Initialize PositionTracker.

        Args:
            client: Connected PolymarketClient
        """
        self.client = client
        self._positions: Dict[str, Position] = {}  # market_slug -> Position

    def get_position(self, market: BTCMarket) -> Optional[Position]:
        """
        Get position for a market.

        Args:
            market: BTCMarket to get position for

        Returns:
            Position if exists, None otherwise
        """
        return self._positions.get(market.slug)

    def get_or_create_position(self, market: BTCMarket) -> Position:
        """
        Get existing position or create new one.

        Args:
            market: BTCMarket

        Returns:
            Position (existing or new)
        """
        if market.slug not in self._positions:
            self._positions[market.slug] = Position(market=market)
            logger.debug(f"Created new position for {market.slug}")

        return self._positions[market.slug]

    async def sync_position(self, market: BTCMarket) -> Position:
        """
        Sync position balances from chain.

        Fetches current token balances from the blockchain
        and updates the position.

        Args:
            market: BTCMarket to sync

        Returns:
            Updated Position
        """
        position = self.get_or_create_position(market)

        try:
            # Fetch balances from chain
            up_balance = await self.client.get_position_balance(market.up_token_id)
            down_balance = await self.client.get_position_balance(market.down_token_id)

            # Update position
            position.sync_balances(up_balance, down_balance)

            logger.debug(
                f"Synced {market.slug}: Up={up_balance:.4f}, Down={down_balance:.4f}"
            )

        except Exception as e:
            logger.error(f"Failed to sync position for {market.slug}: {e}")

        return position

    async def sync_all_positions(self) -> Dict[str, Position]:
        """
        Sync all tracked positions from chain.

        Returns:
            Dictionary of all positions
        """
        for slug in list(self._positions.keys()):
            position = self._positions[slug]
            await self.sync_position(position.market)

        return self._positions

    def add_fill(
        self,
        market: BTCMarket,
        side: str,
        price: float,
        size: float,
        order_id: Optional[str] = None,
    ) -> Fill:
        """
        Record a fill for a market.

        Args:
            market: BTCMarket the fill is for
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
            order_id: Optional order ID

        Returns:
            The Fill object created
        """
        position = self.get_or_create_position(market)
        fill = position.add_fill(side, price, size, order_id)

        logger.info(
            f"Fill recorded: {market.slug} {side} {size:.4f}@${price:.4f}"
        )

        return fill

    def add_pair_fill(
        self,
        market: BTCMarket,
        up_price: float,
        down_price: float,
        size: float,
        up_order_id: Optional[str] = None,
        down_order_id: Optional[str] = None,
    ) -> tuple[Fill, Fill]:
        """
        Record a pair fill (both Up and Down).

        Args:
            market: BTCMarket
            up_price: Up fill price
            down_price: Down fill price
            size: Number of pairs
            up_order_id: Optional Up order ID
            down_order_id: Optional Down order ID

        Returns:
            Tuple of (up_fill, down_fill)
        """
        up_fill = self.add_fill(market, "UP", up_price, size, up_order_id)
        down_fill = self.add_fill(market, "DOWN", down_price, size, down_order_id)

        logger.info(
            f"Pair fill recorded: {market.slug} {size:.4f} pairs "
            f"@ ${up_price + down_price:.4f}/pair"
        )

        return up_fill, down_fill

    def get_all_positions(self) -> List[Position]:
        """
        Get all tracked positions.

        Returns:
            List of all positions
        """
        return list(self._positions.values())

    def get_active_positions(self) -> List[Position]:
        """
        Get positions with non-zero balances.

        Returns:
            List of positions with holdings
        """
        return [
            p for p in self._positions.values()
            if p.up_balance > 0 or p.down_balance > 0
        ]

    def get_imbalanced_positions(self) -> List[Position]:
        """
        Get positions with unbalanced exposure.

        Returns:
            List of positions where Up != Down
        """
        return [
            p for p in self._positions.values()
            if not p.is_balanced and (p.up_balance > 0 or p.down_balance > 0)
        ]

    async def get_portfolio_summary(self) -> PortfolioSummary:
        """
        Calculate portfolio summary across all positions.

        Returns:
            PortfolioSummary with totals
        """
        summary = PortfolioSummary()

        # Get USDC balance
        try:
            summary.usdc_balance = await self.client.get_balance()
        except Exception as e:
            logger.warning(f"Failed to get USDC balance: {e}")

        # Aggregate position stats
        active_positions = self.get_active_positions()
        summary.total_positions = len(active_positions)

        for position in active_positions:
            summary.total_pairs += position.pair_count
            summary.total_cost += position.total_cost
            summary.total_unrealized_pnl += position.unrealized_pnl
            summary.total_exposure_up += position.unmatched_up
            summary.total_exposure_down += position.unmatched_down

        return summary

    def calculate_market_pnl(self, market: BTCMarket) -> Dict[str, float]:
        """
        Calculate P&L for a specific market.

        Args:
            market: BTCMarket to calculate P&L for

        Returns:
            Dictionary with P&L metrics
        """
        position = self.get_position(market)

        if position is None:
            return {
                "unrealized_pnl": 0.0,
                "unrealized_pnl_percent": 0.0,
                "pair_count": 0.0,
                "pair_cost": 0.0,
            }

        return {
            "unrealized_pnl": position.unrealized_pnl,
            "unrealized_pnl_percent": position.unrealized_pnl_percent,
            "pair_count": position.pair_count,
            "pair_cost": position.pair_cost,
        }

    def get_total_pnl(self) -> float:
        """
        Get total unrealized P&L across all positions.

        Returns:
            Total unrealized P&L in dollars
        """
        return sum(p.unrealized_pnl for p in self._positions.values())

    def get_total_exposure(self) -> Dict[str, float]:
        """
        Get total directional exposure.

        Returns:
            Dictionary with up and down exposure
        """
        return {
            "up": sum(p.unmatched_up for p in self._positions.values()),
            "down": sum(p.unmatched_down for p in self._positions.values()),
        }

    def needs_rebalance(self, threshold: float = 1.0) -> bool:
        """
        Check if any position needs rebalancing.

        Args:
            threshold: Imbalance threshold (default 1.0)

        Returns:
            True if any position has imbalance > threshold
        """
        for position in self._positions.values():
            if position.unmatched_up > threshold or position.unmatched_down > threshold:
                return True
        return False

    def clear_position(self, market: BTCMarket) -> None:
        """
        Remove a position from tracking.

        Args:
            market: BTCMarket to remove
        """
        if market.slug in self._positions:
            del self._positions[market.slug]
            logger.info(f"Cleared position for {market.slug}")

    def clear_all_positions(self) -> None:
        """Clear all tracked positions."""
        self._positions.clear()
        logger.info("Cleared all positions")

    def __repr__(self) -> str:
        """String representation."""
        active = len(self.get_active_positions())
        return f"PositionTracker(positions={len(self._positions)}, active={active})"
