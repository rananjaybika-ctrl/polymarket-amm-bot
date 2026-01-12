"""
State Persistence service for crash recovery and position tracking.

Provides JSON-based state persistence for the trading bot, enabling:
- Recovery from crashes with full position context
- Position history preservation across market rotations
- Periodic auto-save to prevent data loss

Based on best practices from poly-maker and industry algorithmic trading systems.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any
import fcntl

logger = logging.getLogger(__name__)


@dataclass
class PersistedPosition:
    """Position data for a single market."""
    market_slug: str
    up_shares: float
    up_avg_price: float
    down_shares: float
    down_avg_price: float
    hedged_pairs: int
    pair_cost: float
    locked_profit: float
    last_updated: str  # ISO format


@dataclass
class PersistedState:
    """
    Complete bot state for persistence.

    Captures everything needed to resume trading after a crash:
    - Current balance and realized PnL
    - Positions across all markets
    - Session metadata
    """
    # Identity
    strategy_name: str
    trading_mode: str  # "paper" or "live"

    # Balances
    balance: float
    initial_balance: float
    realized_pnl: float

    # Session info
    session_start: str  # ISO format
    last_save: str  # ISO format
    trade_count: int

    # Current market
    current_market_slug: Optional[str] = None

    # Positions by market slug
    positions: Dict[str, PersistedPosition] = field(default_factory=dict)

    # Trade history (last N trades for context)
    recent_trades: List[Dict[str, Any]] = field(default_factory=list)

    # Version for forward compatibility
    version: int = 1


class StatePersistence:
    """
    Handles saving and loading bot state to/from JSON files.

    Features:
    - File locking for safe concurrent access
    - Auto-save on configurable interval
    - Archiving of old state files
    - Graceful handling of corrupted files

    Example:
        persistence = StatePersistence(
            state_dir=Path("./state"),
            strategy_name="spread_capture",
        )

        # Load previous state if exists
        state = await persistence.load()

        # Save periodically
        await persistence.save(current_state)

        # Archive on session end
        await persistence.archive()
    """

    MAX_RECENT_TRADES = 50  # Keep last 50 trades in state

    def __init__(
        self,
        state_dir: Path,
        strategy_name: str,
        trading_mode: str = "paper",
    ):
        """
        Initialize state persistence.

        Args:
            state_dir: Directory for state files
            strategy_name: Name of strategy (used in filename)
            trading_mode: "paper" or "live"
        """
        self.state_dir = Path(state_dir)
        self.strategy_name = strategy_name
        self.trading_mode = trading_mode

        # Ensure directory exists
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # State file path
        self.state_file = self.state_dir / f"state_{strategy_name}_{trading_mode}.json"
        self.archive_dir = self.state_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        # Track last save time
        self._last_save: Optional[datetime] = None

    def _serialize_state(self, state: PersistedState) -> dict:
        """Convert state to JSON-serializable dict."""
        data = asdict(state)
        # Convert Position objects to dicts
        data["positions"] = {
            slug: asdict(pos) if isinstance(pos, PersistedPosition) else pos
            for slug, pos in (state.positions or {}).items()
        }
        return data

    def _deserialize_state(self, data: dict) -> PersistedState:
        """Convert dict back to PersistedState."""
        # Convert position dicts to PersistedPosition objects
        positions = {}
        for slug, pos_data in data.get("positions", {}).items():
            if isinstance(pos_data, dict):
                positions[slug] = PersistedPosition(**pos_data)
            else:
                positions[slug] = pos_data

        return PersistedState(
            strategy_name=data.get("strategy_name", self.strategy_name),
            trading_mode=data.get("trading_mode", self.trading_mode),
            balance=data.get("balance", 0.0),
            initial_balance=data.get("initial_balance", 100.0),
            realized_pnl=data.get("realized_pnl", 0.0),
            session_start=data.get("session_start", datetime.now(timezone.utc).isoformat()),
            last_save=data.get("last_save", datetime.now(timezone.utc).isoformat()),
            trade_count=data.get("trade_count", 0),
            current_market_slug=data.get("current_market_slug"),
            positions=positions,
            recent_trades=data.get("recent_trades", []),
            version=data.get("version", 1),
        )

    def save(self, state: PersistedState) -> bool:
        """
        Save state to JSON file with file locking.

        Args:
            state: State to save

        Returns:
            True if save successful, False otherwise
        """
        try:
            # Update last_save timestamp
            state.last_save = datetime.now(timezone.utc).isoformat()

            # Serialize state
            data = self._serialize_state(state)

            # Write with file locking
            with open(self.state_file, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            self._last_save = datetime.now(timezone.utc)
            logger.debug(f"State saved to {self.state_file}")
            return True

        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False

    def load(self) -> Optional[PersistedState]:
        """
        Load state from JSON file.

        Returns:
            PersistedState if file exists and is valid, None otherwise
        """
        if not self.state_file.exists():
            logger.info(f"No state file found at {self.state_file}")
            return None

        try:
            with open(self.state_file, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            state = self._deserialize_state(data)
            logger.info(
                f"Loaded state: balance=${state.balance:.2f}, "
                f"positions={len(state.positions)}, "
                f"trades={state.trade_count}"
            )
            return state

        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted state file, archiving and starting fresh: {e}")
            self._archive_corrupted()
            return None
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return None

    def _archive_corrupted(self) -> None:
        """Move corrupted state file to archive."""
        if self.state_file.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_name = f"corrupted_{self.strategy_name}_{timestamp}.json"
            archive_path = self.archive_dir / archive_name
            self.state_file.rename(archive_path)
            logger.info(f"Archived corrupted state to {archive_path}")

    def archive(self) -> bool:
        """
        Archive current state file on session end.

        Returns:
            True if archive successful
        """
        if not self.state_file.exists():
            return True

        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_name = f"session_{self.strategy_name}_{timestamp}.json"
            archive_path = self.archive_dir / archive_name

            # Copy to archive (don't move - keep current state too)
            import shutil
            shutil.copy2(self.state_file, archive_path)

            logger.info(f"Archived state to {archive_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to archive state: {e}")
            return False

    def should_save(self, interval_seconds: float = 60.0) -> bool:
        """
        Check if enough time has passed since last save.

        Args:
            interval_seconds: Minimum time between saves

        Returns:
            True if should save now
        """
        if self._last_save is None:
            return True

        elapsed = (datetime.now(timezone.utc) - self._last_save).total_seconds()
        return elapsed >= interval_seconds

    def add_trade_to_history(
        self,
        state: PersistedState,
        trade: Dict[str, Any],
    ) -> None:
        """
        Add a trade to the recent trades history.

        Maintains a rolling window of MAX_RECENT_TRADES.

        Args:
            state: State to update
            trade: Trade data to add
        """
        state.recent_trades.append(trade)
        # Keep only last N trades
        if len(state.recent_trades) > self.MAX_RECENT_TRADES:
            state.recent_trades = state.recent_trades[-self.MAX_RECENT_TRADES:]

    def update_position(
        self,
        state: PersistedState,
        market_slug: str,
        up_shares: float,
        up_avg_price: float,
        down_shares: float,
        down_avg_price: float,
        hedged_pairs: int,
        pair_cost: float,
        locked_profit: float,
    ) -> None:
        """
        Update position for a market.

        Args:
            state: State to update
            market_slug: Market identifier
            *: Position data
        """
        state.positions[market_slug] = PersistedPosition(
            market_slug=market_slug,
            up_shares=up_shares,
            up_avg_price=up_avg_price,
            down_shares=down_shares,
            down_avg_price=down_avg_price,
            hedged_pairs=hedged_pairs,
            pair_cost=pair_cost,
            locked_profit=locked_profit,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    def clear_position(self, state: PersistedState, market_slug: str) -> None:
        """Remove a position from state (after resolution)."""
        if market_slug in state.positions:
            del state.positions[market_slug]
            logger.debug(f"Cleared position for {market_slug}")

    def cleanup_old_archives(self, max_age_days: int = 7) -> int:
        """
        Remove archive files older than max_age_days.

        Args:
            max_age_days: Maximum age for archive files

        Returns:
            Number of files removed
        """
        import time

        cutoff = time.time() - (max_age_days * 24 * 3600)
        removed = 0

        for archive_file in self.archive_dir.glob("*.json"):
            if archive_file.stat().st_mtime < cutoff:
                archive_file.unlink()
                removed += 1

        if removed > 0:
            logger.info(f"Cleaned up {removed} old archive files")

        return removed
