"""
Balance Manager service for fund management and risk controls.

Handles:
- Pre-trade validation (sufficient funds, position limits)
- Imbalance recovery strategies
- Risk limit enforcement
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

from src.api.polymarket_client import PolymarketClient
from src.models.market import BTCMarket
from src.models.position import Position
from src.services.pair_analyzer import PairOpportunity
from src.services.position_tracker import PositionTracker, PortfolioSummary


logger = logging.getLogger(__name__)


class RecoveryAction(Enum):
    """Recommended action for imbalanced position."""
    NONE = "none"  # No action needed
    BUY_UP = "buy_up"  # Buy Up to match Down
    BUY_DOWN = "buy_down"  # Buy Down to match Up
    SELL_UP = "sell_up"  # Sell excess Up
    SELL_DOWN = "sell_down"  # Sell excess Down
    HOLD = "hold"  # Hold and wait for better conditions


@dataclass
class RecoveryRecommendation:
    """Recommendation for recovering an imbalanced position."""
    action: RecoveryAction
    side: str  # "UP" or "DOWN"
    size: float  # Amount to trade
    reason: str  # Why this action
    estimated_cost: float = 0.0  # Cost/proceeds of action
    profitable: bool = False  # Whether recovery maintains profit


@dataclass
class TradeValidation:
    """Result of pre-trade validation."""
    valid: bool
    max_size: float
    reason: str
    available_funds: float = 0.0
    opportunity_valid: bool = True


class BalanceManager:
    """
    Manages trading funds, position limits, and risk controls.

    Provides pre-trade validation and imbalance recovery strategies.

    Example:
        manager = BalanceManager(client, tracker)

        # Validate before trading
        validation = await manager.validate_trade(opportunity, size=10)
        if validation.valid:
            # Execute trade
            ...

        # Check for imbalances
        recommendations = await manager.get_recovery_recommendations()
    """

    # Default risk limits
    DEFAULT_MAX_POSITION_SIZE = 100  # Max pairs per market
    DEFAULT_MAX_DAILY_LOSS = 50.0  # Max daily loss in USDC
    DEFAULT_MIN_BALANCE_RESERVE = 10.0  # Keep minimum USDC reserve
    DEFAULT_MAX_EXPOSURE_PERCENT = 0.8  # Max 80% of balance in positions

    def __init__(
        self,
        client: PolymarketClient,
        tracker: PositionTracker,
        max_position_size: int = DEFAULT_MAX_POSITION_SIZE,
        max_daily_loss: float = DEFAULT_MAX_DAILY_LOSS,
        min_balance_reserve: float = DEFAULT_MIN_BALANCE_RESERVE,
        max_exposure_percent: float = DEFAULT_MAX_EXPOSURE_PERCENT,
    ):
        """
        Initialize BalanceManager.

        Args:
            client: Connected PolymarketClient
            tracker: PositionTracker for position data
            max_position_size: Maximum pairs per market
            max_daily_loss: Maximum daily loss before stopping
            min_balance_reserve: Minimum USDC to keep
            max_exposure_percent: Max % of balance to use
        """
        self.client = client
        self.tracker = tracker
        self.max_position_size = max_position_size
        self.max_daily_loss = max_daily_loss
        self.min_balance_reserve = min_balance_reserve
        self.max_exposure_percent = max_exposure_percent

        # Track daily loss
        self._daily_realized_loss = 0.0

        # Session loss tracking (Feb 1, 2026)
        # Tracks cumulative PnL within a trading session
        self.session_realized_pnl = 0.0
        self.max_session_loss = 0  # Disabled (Feb 10, 2026) — was $50

    async def get_available_capital(self) -> float:
        """
        Get USDC available for new trades.

        Accounts for:
        - Current balance
        - Minimum reserve
        - Max exposure limit

        Returns:
            Available USDC for trading
        """
        try:
            balance = await self.client.get_balance()

            # Subtract minimum reserve
            available = balance - self.min_balance_reserve

            # Apply max exposure limit
            max_exposure = balance * self.max_exposure_percent
            available = min(available, max_exposure)

            return max(0, available)

        except Exception as e:
            logger.error(f"Failed to get available capital: {e}")
            return 0.0

    async def check_sufficient_funds(self, cost: float) -> Tuple[bool, float]:
        """
        Check if sufficient funds for a trade.

        Args:
            cost: Total cost of trade

        Returns:
            Tuple of (is_sufficient, available_capital)
        """
        available = await self.get_available_capital()
        return (available >= cost, available)

    async def validate_trade(
        self,
        opportunity: PairOpportunity,
        size: Optional[float] = None,
    ) -> TradeValidation:
        """
        Validate a trade before execution.

        Checks:
        - Sufficient funds
        - Position limits
        - Opportunity still profitable
        - Daily loss limits

        Args:
            opportunity: PairOpportunity to trade
            size: Desired size (uses max if None)

        Returns:
            TradeValidation with approval/rejection details
        """
        # Check opportunity validity
        if not opportunity.is_profitable:
            return TradeValidation(
                valid=False,
                max_size=0,
                reason=f"Opportunity not profitable: ${opportunity.pair_cost:.4f}/pair",
                opportunity_valid=False,
            )

        # Check daily loss limit
        if self._daily_realized_loss >= self.max_daily_loss:
            return TradeValidation(
                valid=False,
                max_size=0,
                reason=f"Daily loss limit reached: ${self._daily_realized_loss:.2f}",
            )

        # Get available funds
        available = await self.get_available_capital()

        if available <= 0:
            return TradeValidation(
                valid=False,
                max_size=0,
                reason="No available capital",
                available_funds=available,
            )

        # Calculate max size from funds
        pair_cost = opportunity.pair_cost
        max_from_funds = int(available / pair_cost)

        # Check position limits
        position = self.tracker.get_position(opportunity.market)
        current_pairs = position.pair_count if position else 0
        remaining_capacity = self.max_position_size - current_pairs
        max_from_limits = max(0, remaining_capacity)

        # Use minimum of all limits
        max_size = min(
            max_from_funds,
            max_from_limits,
            opportunity.executable_size,
        )

        # Check requested size
        requested_size = size if size else max_size

        if requested_size > max_size:
            return TradeValidation(
                valid=False,
                max_size=max_size,
                reason=f"Requested {requested_size} exceeds max {max_size}",
                available_funds=available,
            )

        if max_size <= 0:
            return TradeValidation(
                valid=False,
                max_size=0,
                reason="Cannot trade: position or fund limits reached",
                available_funds=available,
            )

        return TradeValidation(
            valid=True,
            max_size=max_size,
            reason="Trade validated",
            available_funds=available,
        )

    def get_recovery_recommendation(
        self,
        position: Position,
        up_bid: Optional[float] = None,
        down_bid: Optional[float] = None,
        up_ask: Optional[float] = None,
        down_ask: Optional[float] = None,
    ) -> RecoveryRecommendation:
        """
        Get recommendation for recovering an imbalanced position.

        Args:
            position: The imbalanced position
            up_bid: Current Up bid (for selling)
            down_bid: Current Down bid (for selling)
            up_ask: Current Up ask (for buying)
            down_ask: Current Down ask (for buying)

        Returns:
            RecoveryRecommendation with action to take
        """
        # Check if balanced
        if position.is_balanced:
            return RecoveryRecommendation(
                action=RecoveryAction.NONE,
                side="",
                size=0,
                reason="Position is balanced",
            )

        # Determine imbalance direction
        if position.unmatched_up > 0:
            # Have excess Up tokens
            imbalance = position.unmatched_up
            avg_up_price = position.up_avg_price

            # Option 1: Buy Down to complete pairs
            if down_ask is not None:
                potential_pair_cost = avg_up_price + down_ask
                if potential_pair_cost < 1.0:
                    # Buying Down would still be profitable
                    return RecoveryRecommendation(
                        action=RecoveryAction.BUY_DOWN,
                        side="DOWN",
                        size=imbalance,
                        reason=f"Complete pairs profitably at ${potential_pair_cost:.4f}/pair",
                        estimated_cost=imbalance * down_ask,
                        profitable=True,
                    )

            # Option 2: Sell Up if have bid price
            if up_bid is not None:
                proceeds = imbalance * up_bid
                cost_basis = imbalance * avg_up_price
                pnl = proceeds - cost_basis

                return RecoveryRecommendation(
                    action=RecoveryAction.SELL_UP,
                    side="UP",
                    size=imbalance,
                    reason=f"Exit excess Up at ${up_bid:.4f} (PnL: ${pnl:.4f})",
                    estimated_cost=-proceeds,  # Negative = proceeds
                    profitable=pnl > 0,
                )

            # No prices available - hold
            return RecoveryRecommendation(
                action=RecoveryAction.HOLD,
                side="UP",
                size=imbalance,
                reason="Waiting for better market prices",
            )

        else:
            # Have excess Down tokens
            imbalance = position.unmatched_down
            avg_down_price = position.down_avg_price

            # Option 1: Buy Up to complete pairs
            if up_ask is not None:
                potential_pair_cost = up_ask + avg_down_price
                if potential_pair_cost < 1.0:
                    return RecoveryRecommendation(
                        action=RecoveryAction.BUY_UP,
                        side="UP",
                        size=imbalance,
                        reason=f"Complete pairs profitably at ${potential_pair_cost:.4f}/pair",
                        estimated_cost=imbalance * up_ask,
                        profitable=True,
                    )

            # Option 2: Sell Down if have bid price
            if down_bid is not None:
                proceeds = imbalance * down_bid
                cost_basis = imbalance * avg_down_price
                pnl = proceeds - cost_basis

                return RecoveryRecommendation(
                    action=RecoveryAction.SELL_DOWN,
                    side="DOWN",
                    size=imbalance,
                    reason=f"Exit excess Down at ${down_bid:.4f} (PnL: ${pnl:.4f})",
                    estimated_cost=-proceeds,
                    profitable=pnl > 0,
                )

            return RecoveryRecommendation(
                action=RecoveryAction.HOLD,
                side="DOWN",
                size=imbalance,
                reason="Waiting for better market prices",
            )

    async def get_all_recovery_recommendations(
        self,
    ) -> list[Tuple[Position, RecoveryRecommendation]]:
        """
        Get recovery recommendations for all imbalanced positions.

        Returns:
            List of (position, recommendation) tuples
        """
        recommendations = []

        imbalanced = self.tracker.get_imbalanced_positions()

        for position in imbalanced:
            # Would need to fetch current prices for each market
            # For now, return basic recommendation without prices
            rec = self.get_recovery_recommendation(position)
            recommendations.append((position, rec))

        return recommendations

    def record_realized_loss(self, loss: float) -> None:
        """
        Record a realized loss.

        Args:
            loss: Loss amount (positive number)
        """
        if loss > 0:
            self._daily_realized_loss += loss
            logger.info(f"Recorded loss: ${loss:.4f}, daily total: ${self._daily_realized_loss:.4f}")

    def reset_daily_loss(self) -> None:
        """Reset daily loss counter (call at start of trading day)."""
        self._daily_realized_loss = 0.0
        logger.info("Daily loss counter reset")

    # =========================================================================
    # SESSION LOSS TRACKING (Feb 1, 2026)
    # =========================================================================

    def record_trade_pnl(self, pnl: float) -> bool:
        """
        Record trade PnL and check session limit.

        Args:
            pnl: Trade PnL (positive = profit, negative = loss)

        Returns:
            True if within limits and trading should continue,
            False if session loss limit reached and trading should stop
        """
        self.session_realized_pnl += pnl

        # Also update daily loss if it's a loss
        if pnl < 0:
            self.record_realized_loss(-pnl)  # record_realized_loss expects positive number

        # Check session limit (0 = disabled)
        if self.max_session_loss > 0 and self.session_realized_pnl <= -self.max_session_loss:
            logger.warning(
                f"SESSION LOSS LIMIT REACHED: ${-self.session_realized_pnl:.2f} "
                f"(limit: ${self.max_session_loss:.2f}) - STOPPING TRADING"
            )
            return False

        return True

    def reset_session(self) -> None:
        """Reset session PnL counter (call at start of new trading session)."""
        old_pnl = self.session_realized_pnl
        self.session_realized_pnl = 0.0
        logger.info(f"Session reset (previous session PnL: ${old_pnl:.2f})")

    def is_within_session_limit(self) -> bool:
        """Check if within session loss limit (0 = disabled, always True)."""
        if self.max_session_loss <= 0:
            return True
        return self.session_realized_pnl > -self.max_session_loss

    def get_session_pnl(self) -> float:
        """Get current session realized PnL."""
        return self.session_realized_pnl

    def is_within_daily_limit(self) -> bool:
        """Check if within daily loss limit."""
        return self._daily_realized_loss < self.max_daily_loss

    def get_remaining_daily_budget(self) -> float:
        """Get remaining daily loss budget."""
        return max(0, self.max_daily_loss - self._daily_realized_loss)

    async def get_portfolio_health(self) -> dict:
        """
        Get overall portfolio health metrics.

        Returns:
            Dictionary with health metrics
        """
        summary = await self.tracker.get_portfolio_summary()
        available = await self.get_available_capital()

        return {
            "usdc_balance": summary.usdc_balance,
            "available_capital": available,
            "total_positions": summary.total_positions,
            "total_pairs": summary.total_pairs,
            "total_pnl": summary.total_unrealized_pnl,
            "exposure_up": summary.total_exposure_up,
            "exposure_down": summary.total_exposure_down,
            "is_balanced": summary.is_balanced,
            "daily_loss": self._daily_realized_loss,
            "within_daily_limit": self.is_within_daily_limit(),
            "remaining_daily_budget": self.get_remaining_daily_budget(),
            # Session tracking (Feb 1, 2026)
            "session_pnl": self.session_realized_pnl,
            "within_session_limit": self.is_within_session_limit(),
            "max_session_loss": self.max_session_loss,
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"BalanceManager(max_pos={self.max_position_size}, "
            f"daily_loss=${self._daily_realized_loss:.2f})"
        )
