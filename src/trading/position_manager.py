"""
Position Manager - Position tracking, sync, and imbalance calculations

Extracted from run_paper_bot.py to provide a clean interface for:
- Position synchronization with REST API (live mode)
- Position tracking for paper mode
- Imbalance calculations
- Emergency threshold management
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.api.polymarket_client import PolymarketClient
    from src.services.paper_trading import PaperTradingEngine
    from src.services.live_trading import LiveTradingEngine

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    """
    Current position state for a market.

    Tracks shares held, average prices, and calculated metrics.
    """
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    last_sync_time: float = 0.0

    @property
    def hedged_pairs(self) -> float:
        """Number of fully hedged pairs (min of UP and DOWN)."""
        return min(self.up_shares, self.down_shares)

    @property
    def imbalance(self) -> float:
        """Absolute imbalance in shares."""
        return abs(self.up_shares - self.down_shares)

    @property
    def imbalance_pct(self) -> float:
        """Imbalance as percentage of max position."""
        max_pos = max(self.up_shares, self.down_shares)
        return self.imbalance / max_pos if max_pos > 0 else 0.0

    @property
    def deficit_side(self) -> Optional[str]:
        """Which side has fewer shares (needs hedging)."""
        if self.up_shares == self.down_shares:
            return None
        return "UP" if self.up_shares < self.down_shares else "DOWN"

    @property
    def surplus_side(self) -> Optional[str]:
        """Which side has more shares."""
        if self.up_shares == self.down_shares:
            return None
        return "DOWN" if self.up_shares < self.down_shares else "UP"

    @property
    def pair_cost(self) -> float:
        """Average cost per pair (if balanced)."""
        if self.hedged_pairs <= 0:
            return 0.0
        return (self.up_cost + self.down_cost) / self.hedged_pairs

    @property
    def locked_profit(self) -> float:
        """Guaranteed profit from hedged pairs (resolves at $1)."""
        if self.hedged_pairs <= 0:
            return 0.0
        cost = self.hedged_pairs * self.pair_cost
        return self.hedged_pairs - cost

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "up_shares": self.up_shares,
            "down_shares": self.down_shares,
            "up_avg_price": self.up_avg_price,
            "down_avg_price": self.down_avg_price,
            "up_cost": self.up_cost,
            "down_cost": self.down_cost,
            "hedged_pairs": self.hedged_pairs,
            "imbalance": self.imbalance,
            "imbalance_pct": self.imbalance_pct,
            "deficit_side": self.deficit_side,
            "pair_cost": self.pair_cost,
            "locked_profit": self.locked_profit,
        }


@dataclass
class ImbalanceInfo:
    """
    Detailed imbalance analysis for trading decisions.
    """
    imbalance_shares: float
    imbalance_pct: float
    deficit_side: Optional[str]
    is_hard_stop: bool
    is_emergency: bool
    should_hedge: bool
    max_allowed_imbalance: float
    time_remaining_secs: float

    @property
    def status(self) -> str:
        """Human-readable status."""
        if self.is_hard_stop:
            return "HARD_STOP"
        elif self.is_emergency:
            return "EMERGENCY"
        elif self.should_hedge:
            return "HEDGE_NEEDED"
        else:
            return "OK"


class PositionManager:
    """
    Manages position tracking and synchronization.

    Provides a unified interface for both paper and live trading modes.
    Handles REST API sync for live mode and internal tracking for paper mode.
    """

    def __init__(
        self,
        trading_mode: str = "paper",
        hard_max_imbalance: int = 10,
        max_imbalance_pct: float = 0.20,
        hedge_trigger_pct: float = 0.15,
        sync_interval_secs: float = 5.0,
    ):
        """
        Initialize position manager.

        Args:
            trading_mode: "paper" or "live"
            hard_max_imbalance: HARD STOP - no trading if imbalance >= this
            max_imbalance_pct: Maximum imbalance as percentage of target
            hedge_trigger_pct: Start hedging at this imbalance percentage
            sync_interval_secs: How often to sync position from REST API (live mode)
        """
        self.trading_mode = trading_mode
        self.hard_max_imbalance = hard_max_imbalance
        self.max_imbalance_pct = max_imbalance_pct
        self.hedge_trigger_pct = hedge_trigger_pct
        self.sync_interval_secs = sync_interval_secs

        # Position cache per market
        self._positions: Dict[str, PositionState] = {}

        # Emergency tracking
        self._emergency_triggered_markets: set = set()
        self._last_emergency_time: Dict[str, float] = {}

        # Logging throttle
        self._last_hard_stop_log: float = 0.0

    def get_position(self, market_slug: str) -> PositionState:
        """Get cached position for a market."""
        if market_slug not in self._positions:
            self._positions[market_slug] = PositionState()
        return self._positions[market_slug]

    def update_position(
        self,
        market_slug: str,
        up_shares: float,
        down_shares: float,
        up_avg_price: float = 0.0,
        down_avg_price: float = 0.0,
    ) -> PositionState:
        """
        Update cached position state.

        Args:
            market_slug: Market identifier
            up_shares: Total UP shares held
            down_shares: Total DOWN shares held
            up_avg_price: Average price paid for UP shares
            down_avg_price: Average price paid for DOWN shares

        Returns:
            Updated position state
        """
        position = self.get_position(market_slug)
        position.up_shares = up_shares
        position.down_shares = down_shares
        position.up_avg_price = up_avg_price
        position.down_avg_price = down_avg_price
        position.up_cost = up_shares * up_avg_price
        position.down_cost = down_shares * down_avg_price
        position.last_sync_time = time.time()
        return position

    def record_fill(
        self,
        market_slug: str,
        side: str,
        price: float,
        size: float,
    ) -> PositionState:
        """
        Record a fill and update position.

        Args:
            market_slug: Market identifier
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size in shares

        Returns:
            Updated position state
        """
        position = self.get_position(market_slug)

        if side == "UP":
            new_cost = position.up_cost + (price * size)
            new_shares = position.up_shares + size
            position.up_shares = new_shares
            position.up_cost = new_cost
            position.up_avg_price = new_cost / new_shares if new_shares > 0 else 0.0
        else:  # DOWN
            new_cost = position.down_cost + (price * size)
            new_shares = position.down_shares + size
            position.down_shares = new_shares
            position.down_cost = new_cost
            position.down_avg_price = new_cost / new_shares if new_shares > 0 else 0.0

        position.last_sync_time = time.time()
        return position

    def analyze_imbalance(
        self,
        market_slug: str,
        time_remaining_secs: float,
        target_shares: int = 50,
    ) -> ImbalanceInfo:
        """
        Analyze position imbalance and determine trading action.

        Args:
            market_slug: Market identifier
            time_remaining_secs: Seconds until market resolution
            target_shares: Target shares per side

        Returns:
            ImbalanceInfo with analysis results
        """
        position = self.get_position(market_slug)

        imbalance = position.imbalance
        imbalance_pct = position.imbalance_pct
        deficit_side = position.deficit_side

        # Calculate max allowed based on target
        max_allowed = int(self.max_imbalance_pct * target_shares)

        # HARD STOP check
        is_hard_stop = imbalance >= self.hard_max_imbalance

        # Emergency threshold varies with time
        emergency_threshold = self._get_emergency_threshold(time_remaining_secs)
        is_emergency = imbalance >= emergency_threshold and not is_hard_stop

        # Should we actively hedge?
        should_hedge = imbalance_pct >= self.hedge_trigger_pct

        return ImbalanceInfo(
            imbalance_shares=imbalance,
            imbalance_pct=imbalance_pct,
            deficit_side=deficit_side,
            is_hard_stop=is_hard_stop,
            is_emergency=is_emergency,
            should_hedge=should_hedge,
            max_allowed_imbalance=max_allowed,
            time_remaining_secs=time_remaining_secs,
        )

    def _get_emergency_threshold(self, time_remaining_secs: float) -> int:
        """
        Get time-based emergency imbalance threshold.

        Early in market: Higher threshold (10) - let patient orders work
        Late in market: Lower threshold (5) - must hedge before resolution
        """
        if time_remaining_secs > 420:  # > 7 minutes
            return 10  # Patient - let chased orders fill
        else:
            return 5   # Urgent - must hedge before resolution

    def mark_emergency_triggered(self, market_slug: str) -> None:
        """Mark a market as having triggered emergency - stop further trading."""
        self._emergency_triggered_markets.add(market_slug)
        self._last_emergency_time[market_slug] = time.time()
        logger.warning(f"[EMERGENCY] Market {market_slug} marked - stopping trading")

    def is_emergency_triggered(self, market_slug: str) -> bool:
        """Check if emergency was triggered for a market."""
        return market_slug in self._emergency_triggered_markets

    def get_emergency_cooldown(self, market_slug: str) -> float:
        """Get seconds since last emergency for a market."""
        last_time = self._last_emergency_time.get(market_slug, 0)
        return time.time() - last_time

    def reset_market(self, market_slug: str) -> None:
        """Reset position tracking for a market (e.g., after resolution)."""
        if market_slug in self._positions:
            del self._positions[market_slug]
        self._emergency_triggered_markets.discard(market_slug)
        if market_slug in self._last_emergency_time:
            del self._last_emergency_time[market_slug]

    async def sync_from_api(
        self,
        market_slug: str,
        client: "PolymarketClient",
        market: Any,
    ) -> Optional[PositionState]:
        """
        Sync position from Polymarket REST API (live mode).

        Args:
            market_slug: Market identifier
            client: Polymarket API client
            market: Market object with token_ids

        Returns:
            Updated position state or None on error
        """
        if self.trading_mode != "live":
            return self.get_position(market_slug)

        position = self.get_position(market_slug)

        # Check if we need to sync
        if time.time() - position.last_sync_time < self.sync_interval_secs:
            return position

        try:
            # Get positions from API
            import aiohttp

            wallet = client.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return position
                    positions = await response.json()

            # Find positions for this market
            up_shares = 0.0
            down_shares = 0.0

            for pos in positions:
                token_id = pos.get("token_id") or pos.get("asset")
                size = float(pos.get("size", 0))

                if size <= 0:
                    continue

                # Match token to market
                if hasattr(market, 'up_token') and token_id == market.up_token:
                    up_shares = size
                elif hasattr(market, 'down_token') and token_id == market.down_token:
                    down_shares = size

            # Update position
            position.up_shares = up_shares
            position.down_shares = down_shares
            position.last_sync_time = time.time()

            logger.debug(
                f"[SYNC] {market_slug}: UP={up_shares:.0f}, DOWN={down_shares:.0f}, "
                f"imbalance={position.imbalance:.0f}"
            )

            return position

        except Exception as e:
            logger.warning(f"[SYNC] Failed for {market_slug}: {e}")
            return position

    async def check_existing_positions(
        self,
        client: "PolymarketClient",
    ) -> Dict[str, Any]:
        """
        Check for existing positions on startup (live mode only).

        Warns user if positions exist from previous sessions.

        Returns:
            Dict with total UP/DOWN shares and position count
        """
        import aiohttp

        result = {"up": 0.0, "down": 0.0, "total": 0, "positions": []}

        if self.trading_mode != "live":
            return result

        try:
            wallet = client.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return result
                    positions = await response.json()

            for pos in positions:
                size = float(pos.get("size", 0))
                if size <= 0:
                    continue

                outcome = pos.get("outcome", "").upper()
                market = pos.get("title", pos.get("slug", "Unknown"))

                result["total"] += 1
                result["positions"].append({
                    "market": market[:50],
                    "outcome": outcome,
                    "size": size,
                })

                if outcome in ["YES", "UP"]:
                    result["up"] += size
                elif outcome in ["NO", "DOWN"]:
                    result["down"] += size

        except Exception as e:
            logger.debug(f"Could not check existing positions: {e}")

        return result

    def should_buy(
        self,
        side: str,
        price: float,
        market_slug: str,
        cheap_threshold: float = 0.45,
        max_hedge_price: float = 0.70,
    ) -> bool:
        """
        Determine if we should buy a side (Gabagool-style logic).

        Args:
            side: "UP" or "DOWN"
            price: Current price
            market_slug: Market identifier
            cheap_threshold: Buy aggressively below this price
            max_hedge_price: Max price for hedge buys

        Returns:
            True if should buy
        """
        # Always buy if cheap
        if price < cheap_threshold:
            return True

        position = self.get_position(market_slug)

        # Check if we need to hedge this side
        if position.deficit_side == side:
            if position.imbalance_pct > self.hedge_trigger_pct:
                return price < max_hedge_price

        return False

    def get_metrics(self) -> Dict[str, Any]:
        """Get position manager metrics for monitoring."""
        total_positions = len(self._positions)
        total_up = sum(p.up_shares for p in self._positions.values())
        total_down = sum(p.down_shares for p in self._positions.values())
        total_hedged = sum(p.hedged_pairs for p in self._positions.values())

        return {
            "total_positions": total_positions,
            "total_up_shares": total_up,
            "total_down_shares": total_down,
            "total_hedged_pairs": total_hedged,
            "emergency_markets": len(self._emergency_triggered_markets),
            "trading_mode": self.trading_mode,
        }
