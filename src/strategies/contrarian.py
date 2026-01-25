"""
Contrarian Strategy - Path 2: Bet Against BTC Direction at 15-min Scale

This strategy bets against the current BTC direction when a reversal signal is detected.
It enters the contrarian side when price has moved significantly and shows signs of reverting.

Key Logic:
    1. Track peak move from window open
    2. Detect reversal: pullback >= 0.01% from peak
    3. Filter: retracement_frac >= 0.30, entry_price >= $0.20
    4. Check z-score >= 0.5 (moderate volatility)
    5. Enter contrarian direction at ~$0.30
    6. Hold to resolution (no stops)

Expected Performance:
    - Win rate: ~43.4% (based on historical analysis)
    - Filters out ~35% of low-vol windows
    - Best in moderate volatility regimes

Author: Claude Code
Date: January 25, 2026
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.volatility_tracker import LiveZScoreTracker

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Reversal detection parameters
DEFAULT_PULLBACK_THRESHOLD = 0.0001  # 0.01% pullback from peak to trigger
DEFAULT_RETRACEMENT_MIN = 0.30       # Must retrace 30% of move
DEFAULT_ENTRY_PRICE_MIN = 0.20       # Entry price floor
DEFAULT_ENTRY_PRICE_TARGET = 0.30    # Target entry price

# Volatility gate parameters
DEFAULT_VOL_GATE_K = 0.5             # Ratio threshold (pre_vol / ema_vol >= k)
DEFAULT_VOL_GATE_HALFLIFE = 50       # Windows for EMA halflife

# Timing parameters
DEFAULT_MIN_DELAY_SECONDS = 60       # Minimum delay from window start before entry
DEFAULT_WINDOW_DURATION = 900        # 15 minutes in seconds

# Z-score filter
DEFAULT_Z_THRESHOLD = 0.5            # Minimum z-score for entry

# Position sizing
DEFAULT_SHARES_PER_TRADE = 2500      # Large size for contrarian bets


# =============================================================================
# ENUMS
# =============================================================================

class ContrarianPhase(Enum):
    """Strategy phases for ContrarianStrategy."""
    WAITING = "waiting"           # Waiting for window to start
    MONITORING = "monitoring"     # Tracking price for reversal signal
    POSITIONED = "positioned"     # Entry filled, holding to resolution
    GATED_OUT = "gated_out"       # Skipped due to low volatility
    COMPLETE = "complete"         # Window resolved


class WindowDirection(Enum):
    """Direction of BTC move in the window."""
    UP = "up"
    DOWN = "down"
    NONE = "none"


# =============================================================================
# ADAPTIVE EWMA VOLATILITY GATE
# =============================================================================

class AdaptiveEWMAGate:
    """
    Adaptive volatility gate - filters ~35% of low-vol windows.

    Maintains an exponentially-weighted moving average of window volatility
    and only allows entry when current window volatility is at least k times
    the average.

    This prevents entering in dead/choppy markets where contrarian signals
    are less reliable.
    """

    def __init__(
        self,
        k: float = DEFAULT_VOL_GATE_K,
        halflife_windows: int = DEFAULT_VOL_GATE_HALFLIFE,
    ):
        """
        Initialize the volatility gate.

        Args:
            k: Ratio threshold - only allow if pre_vol >= k * vol_ema
            halflife_windows: Number of windows for EMA half-life
        """
        self.k = k
        # Calculate alpha from halflife: alpha = 1 - 0.5^(1/halflife)
        self.alpha = 1 - 0.5 ** (1 / halflife_windows)
        self.vol_ema: Optional[float] = None
        self._windows_seen = 0

    def update_and_check(self, pre_vol: float) -> bool:
        """
        Update EMA and check if current volatility passes the gate.

        Args:
            pre_vol: Pre-window volatility (e.g., from first 60s of window)

        Returns:
            True if volatility is sufficient for trading, False to skip window
        """
        self._windows_seen += 1

        # First window - initialize EMA and allow trading
        if self.vol_ema is None:
            self.vol_ema = pre_vol
            return True

        # Check ratio against threshold
        ratio = pre_vol / self.vol_ema if self.vol_ema > 0 else 1.0
        allowed = ratio >= self.k

        # Update EMA
        self.vol_ema = self.alpha * pre_vol + (1 - self.alpha) * self.vol_ema

        if not allowed:
            logger.debug(
                f"[CONTRARIAN] Vol gate: REJECTED (pre_vol={pre_vol:.6f}, "
                f"ema={self.vol_ema:.6f}, ratio={ratio:.2f} < {self.k})"
            )
        else:
            logger.debug(
                f"[CONTRARIAN] Vol gate: PASSED (pre_vol={pre_vol:.6f}, "
                f"ema={self.vol_ema:.6f}, ratio={ratio:.2f} >= {self.k})"
            )

        return allowed

    def get_state(self) -> Dict[str, Any]:
        """Get current gate state for debugging."""
        return {
            "k": self.k,
            "alpha": round(self.alpha, 4),
            "vol_ema": round(self.vol_ema, 6) if self.vol_ema else None,
            "windows_seen": self._windows_seen,
        }

    def reset(self) -> None:
        """Reset the gate (use for testing or fresh start)."""
        self.vol_ema = None
        self._windows_seen = 0


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class ContrarianState:
    """State tracking for contrarian strategy."""
    phase: ContrarianPhase = ContrarianPhase.WAITING

    # Window tracking
    window_start_time: float = 0.0
    window_start_price: float = 0.0
    window_peak_price: float = 0.0
    window_trough_price: float = 0.0
    window_direction: WindowDirection = WindowDirection.NONE

    # Entry tracking
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    entry_size: int = 0
    entry_time: float = 0.0

    # Statistics
    total_entries: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_profit: float = 0.0
    windows_gated: int = 0
    windows_traded: int = 0

    # Pre-window volatility for gate
    pre_window_vol: float = 0.0

    def reset_for_window(self) -> None:
        """Reset state for a new 15-min window."""
        self.phase = ContrarianPhase.WAITING
        self.window_start_time = 0.0
        self.window_start_price = 0.0
        self.window_peak_price = 0.0
        self.window_trough_price = 0.0
        self.window_direction = WindowDirection.NONE
        self.entry_side = None
        self.entry_price = 0.0
        self.entry_size = 0
        self.entry_time = 0.0
        self.pre_window_vol = 0.0


# =============================================================================
# CONTRARIAN STRATEGY
# =============================================================================

class ContrarianStrategy:
    """
    Path 2: Bet Against BTC Direction at 15-min Scale.

    This strategy monitors BTC price movement within each 15-minute window
    and enters a contrarian position when it detects a reversal signal.

    The key insight is that strong moves often overextend and then retrace,
    allowing us to profit by betting against the initial direction.

    Entry Conditions:
        1. Price has moved significantly from window open (establishes direction)
        2. Price has pulled back at least 0.01% from the peak/trough
        3. Retracement is at least 30% of the initial move
        4. Cheap side price is >= $0.20 (don't bet when too expensive)
        5. Z-score is >= 0.5 (moderate volatility - not dead market)
        6. At least 60s elapsed from window start (let direction establish)

    Exit:
        Hold to resolution - no stops (analysis shows stops hurt performance)

    Constructor Args:
        pullback_threshold: Minimum pullback from peak to trigger (default 0.01%)
        retracement_min: Minimum retracement fraction (default 0.30)
        entry_price_min: Minimum entry price floor (default $0.20)
        min_delay_seconds: Minimum wait from window start (default 60s)
        z_threshold: Minimum z-score for entry (default 0.5)
        shares_per_trade: Position size (default 2500)
    """

    def __init__(
        self,
        pullback_threshold: float = DEFAULT_PULLBACK_THRESHOLD,
        retracement_min: float = DEFAULT_RETRACEMENT_MIN,
        entry_price_min: float = DEFAULT_ENTRY_PRICE_MIN,
        entry_price_target: float = DEFAULT_ENTRY_PRICE_TARGET,
        min_delay_seconds: int = DEFAULT_MIN_DELAY_SECONDS,
        z_threshold: float = DEFAULT_Z_THRESHOLD,
        shares_per_trade: int = DEFAULT_SHARES_PER_TRADE,
        vol_gate_k: float = DEFAULT_VOL_GATE_K,
        vol_gate_halflife: int = DEFAULT_VOL_GATE_HALFLIFE,
        zscore_tracker: Optional["LiveZScoreTracker"] = None,
    ):
        # Entry parameters
        self.pullback_threshold = pullback_threshold
        self.retracement_min = retracement_min
        self.entry_price_min = entry_price_min
        self.entry_price_target = entry_price_target
        self.min_delay_seconds = min_delay_seconds
        self.z_threshold = z_threshold
        self.shares_per_trade = shares_per_trade

        # Volatility gate
        self.vol_gate = AdaptiveEWMAGate(k=vol_gate_k, halflife_windows=vol_gate_halflife)

        # Z-score tracker (optional - can be set later)
        self.zscore_tracker = zscore_tracker

        # State
        self.state = ContrarianState()

        # Price history for pre-window volatility calculation
        self._price_history: List[Tuple[float, float]] = []  # (timestamp, price)
        self._pre_window_prices: List[float] = []

        logger.info(
            f"[CONTRARIAN] Initialized: pullback={pullback_threshold:.4%}, "
            f"retracement={retracement_min:.0%}, entry_min=${entry_price_min:.2f}, "
            f"delay={min_delay_seconds}s, z_thresh={z_threshold}, "
            f"size={shares_per_trade}"
        )

    def on_window_start(
        self,
        btc_price: float,
        pre_vol: float,
        timestamp: Optional[float] = None,
    ) -> bool:
        """
        Initialize for new 15-min window.

        Should be called when a new market window starts. Initializes tracking
        and checks volatility gate.

        Args:
            btc_price: Current BTC price at window start
            pre_vol: Pre-window volatility (from previous window or first minute)
            timestamp: Window start timestamp (default: time.time())

        Returns:
            False if window is gated (skip trading), True if allowed
        """
        if timestamp is None:
            timestamp = time.time()

        # Reset state for new window
        self.state.reset_for_window()
        self.state.window_start_time = timestamp
        self.state.window_start_price = btc_price
        self.state.window_peak_price = btc_price
        self.state.window_trough_price = btc_price
        self.state.pre_window_vol = pre_vol

        # Clear pre-window price history
        self._pre_window_prices = []

        # Check volatility gate
        if not self.vol_gate.update_and_check(pre_vol):
            self.state.phase = ContrarianPhase.GATED_OUT
            self.state.windows_gated += 1
            logger.info(
                f"[CONTRARIAN] Window GATED OUT: low volatility "
                f"(pre_vol={pre_vol:.6f})"
            )
            return False

        self.state.phase = ContrarianPhase.MONITORING
        logger.info(
            f"[CONTRARIAN] Window started: btc=${btc_price:,.2f}, "
            f"pre_vol={pre_vol:.6f}, monitoring for reversal"
        )
        return True

    def update(
        self,
        btc_price: float,
        timestamp: Optional[float] = None,
        cheap_price: Optional[float] = None,
        up_ask: Optional[float] = None,
        down_ask: Optional[float] = None,
    ) -> Optional[Tuple[str, float, int]]:
        """
        Update with new price data and check for entry signal.

        Should be called on each price update during the window.

        Args:
            btc_price: Current BTC price
            timestamp: Current timestamp (default: time.time())
            cheap_price: Current cheap side price (derived from up_ask/down_ask)
            up_ask: Current UP ask price (optional, for deriving cheap_price)
            down_ask: Current DOWN ask price (optional, for deriving cheap_price)

        Returns:
            (side, price, size) if entry triggered, None otherwise
        """
        if timestamp is None:
            timestamp = time.time()

        s = self.state

        # Skip if not in monitoring phase
        if s.phase != ContrarianPhase.MONITORING:
            return None

        # Check minimum delay
        elapsed = timestamp - s.window_start_time
        if elapsed < self.min_delay_seconds:
            # Still collecting data, update peak/trough
            s.window_peak_price = max(s.window_peak_price, btc_price)
            s.window_trough_price = min(s.window_trough_price, btc_price)
            return None

        # Update peak and trough
        s.window_peak_price = max(s.window_peak_price, btc_price)
        s.window_trough_price = min(s.window_trough_price, btc_price)

        # Determine window direction based on total move
        up_move = s.window_peak_price - s.window_start_price
        down_move = s.window_start_price - s.window_trough_price

        if up_move > down_move and up_move > 0:
            s.window_direction = WindowDirection.UP
            peak = s.window_peak_price
            # Pullback from peak (price went up, now pulling back down)
            pullback = (peak - btc_price) / peak if peak > 0 else 0
            # Retracement fraction: how much of the up move has retraced
            retracement = (peak - btc_price) / up_move if up_move > 0 else 0
            contrarian_side = "DOWN"  # Bet against the up move
        elif down_move > 0:
            s.window_direction = WindowDirection.DOWN
            trough = s.window_trough_price
            # Pullback from trough (price went down, now pulling back up)
            pullback = (btc_price - trough) / trough if trough > 0 else 0
            # Retracement fraction: how much of the down move has retraced
            retracement = (btc_price - trough) / down_move if down_move > 0 else 0
            contrarian_side = "UP"  # Bet against the down move
        else:
            # No significant move yet
            return None

        # Check pullback threshold
        if pullback < self.pullback_threshold:
            return None

        # Check retracement minimum
        if retracement < self.retracement_min:
            logger.debug(
                f"[CONTRARIAN] Retracement too small: {retracement:.1%} < {self.retracement_min:.0%}"
            )
            return None

        # Determine cheap side price
        if cheap_price is None and up_ask is not None and down_ask is not None:
            cheap_price = min(up_ask, down_ask)

        if cheap_price is None:
            logger.debug("[CONTRARIAN] No cheap_price available, skipping")
            return None

        # Check entry price minimum
        if cheap_price < self.entry_price_min:
            logger.debug(
                f"[CONTRARIAN] Entry price too low: ${cheap_price:.3f} < ${self.entry_price_min:.2f}"
            )
            return None

        # Check z-score if tracker available
        if self.zscore_tracker is not None:
            current_z = self.zscore_tracker.get_zscore()
            if current_z < self.z_threshold:
                logger.debug(
                    f"[CONTRARIAN] Z-score too low: {current_z:.2f} < {self.z_threshold}"
                )
                return None

        # All conditions met - generate entry signal
        entry_price = min(cheap_price, self.entry_price_target)
        entry_price = round(entry_price, 2)

        s.entry_side = contrarian_side
        s.entry_price = entry_price
        s.entry_size = self.shares_per_trade
        s.entry_time = timestamp
        s.phase = ContrarianPhase.POSITIONED
        s.total_entries += 1
        s.windows_traded += 1

        logger.info(
            f"[CONTRARIAN] ENTRY SIGNAL: {contrarian_side} @ ${entry_price:.3f}, "
            f"size={self.shares_per_trade}, "
            f"pullback={pullback:.4%}, retracement={retracement:.0%}, "
            f"direction={s.window_direction.value}"
        )

        return (contrarian_side, entry_price, self.shares_per_trade)

    def get_quotes(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        time_remaining: float,
        current_time: Optional[float] = None,
        binance_price: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate quotes based on current market state.

        This method provides a unified interface similar to EnhancedSpikeStrategy.

        Args:
            up_bid, up_ask: UP side orderbook
            down_bid, down_ask: DOWN side orderbook
            time_remaining: Seconds until market resolution
            current_time: Current timestamp (default: time.time())
            binance_price: Current Binance BTC price

        Returns:
            List of quote dicts: [{'side': str, 'price': float, 'size': int}, ...]
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Update z-score tracker if available
        if binance_price is not None and self.zscore_tracker is not None:
            self.zscore_tracker.update(binance_price)

        # If gated out, return empty
        if s.phase == ContrarianPhase.GATED_OUT:
            return []

        # If already positioned, no new quotes (hold to resolution)
        if s.phase == ContrarianPhase.POSITIONED:
            return []

        # If window complete, return empty
        if s.phase == ContrarianPhase.COMPLETE:
            return []

        # Check for entry signal
        if s.phase == ContrarianPhase.MONITORING and binance_price is not None:
            result = self.update(
                btc_price=binance_price,
                timestamp=current_time,
                up_ask=up_ask,
                down_ask=down_ask,
            )

            if result is not None:
                side, price, size = result
                return [{
                    'side': side,
                    'price': price,
                    'size': size,
                    'is_contrarian_entry': True,
                    'retracement': s.window_direction.value,
                }]

        return []

    def on_fill(self, side: str, price: float, size: int) -> None:
        """Handle a fill notification."""
        s = self.state

        if s.phase == ContrarianPhase.MONITORING:
            # This is our entry fill
            s.phase = ContrarianPhase.POSITIONED
            s.entry_side = side.upper()
            s.entry_price = price
            s.entry_size = size
            s.entry_time = time.time()

            logger.info(
                f"[CONTRARIAN] Entry filled: {side} {size}@${price:.3f}, "
                f"holding to resolution"
            )

    def on_window_end(self, resolution: Optional[str] = None, profit: float = 0.0) -> None:
        """
        Handle window end/resolution.

        Args:
            resolution: "UP" or "DOWN" if market resolved, None if unknown
            profit: Realized profit from this window
        """
        s = self.state

        if s.phase == ContrarianPhase.POSITIONED and resolution is not None:
            # Track win/loss
            won = (s.entry_side == resolution)
            if won:
                s.total_wins += 1
            else:
                s.total_losses += 1

            s.total_profit += profit

            logger.info(
                f"[CONTRARIAN] Window resolved: {resolution}, "
                f"entry={s.entry_side}, {'WIN' if won else 'LOSS'}, "
                f"profit=${profit:.2f}, total=${s.total_profit:.2f}, "
                f"W/L={s.total_wins}/{s.total_losses}"
            )

        s.phase = ContrarianPhase.COMPLETE

    def set_zscore_tracker(self, tracker: "LiveZScoreTracker") -> None:
        """Set or update the z-score tracker."""
        self.zscore_tracker = tracker
        logger.info(f"[CONTRARIAN] Z-score tracker set: threshold={self.z_threshold}")

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
            "phase": s.phase.value,
            "window": {
                "start_price": s.window_start_price,
                "peak_price": s.window_peak_price,
                "trough_price": s.window_trough_price,
                "direction": s.window_direction.value,
                "elapsed": time.time() - s.window_start_time if s.window_start_time > 0 else 0,
            },
            "entry": {
                "side": s.entry_side,
                "price": s.entry_price,
                "size": s.entry_size,
            },
            "statistics": {
                "total_entries": s.total_entries,
                "total_wins": s.total_wins,
                "total_losses": s.total_losses,
                "win_rate": s.total_wins / max(1, s.total_entries),
                "total_profit": s.total_profit,
                "windows_gated": s.windows_gated,
                "windows_traded": s.windows_traded,
            },
            "vol_gate": self.vol_gate.get_state(),
        }

    def reset(self) -> None:
        """Reset strategy for new session."""
        self.state = ContrarianState()
        self._price_history = []
        self._pre_window_prices = []
        logger.info("[CONTRARIAN] Strategy reset")

    def __repr__(self) -> str:
        return (
            f"ContrarianStrategy("
            f"pullback={self.pullback_threshold:.4%}, "
            f"retracement={self.retracement_min:.0%}, "
            f"size={self.shares_per_trade})"
        )
