"""
Market Type Detection for adaptive strategy parameters.

Detects market behavior patterns after observing prices for ~2 minutes,
then recommends strategy parameters based on market type.

Market Types:
    SIDEWAYS: Both sides had cheap prices (<$0.45) - balanced market
    TRENDING_UP: UP never cheap, DOWN was cheap - UP is favorite
    TRENDING_DOWN: DOWN never cheap, UP was cheap - DOWN is favorite
    NO_OPPORTUNITY: Neither side ever cheap - both expensive
    UNKNOWN: Not enough data yet

Usage:
    detector = MarketTypeDetector()

    # Feed price data as it comes in
    for price_update in stream:
        detector.add_price(price_update.side, price_update.price)

    # After ~2 minutes, detect market type
    if detector.has_enough_data():
        market_type = detector.detect()
        params = detector.get_recommended_params()
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Threshold for "cheap" price - below this means opportunity to buy
CHEAP_THRESHOLD = 0.45

# Minimum observations needed for detection (~2 minutes at 2s intervals)
MIN_OBSERVATIONS = 30

# Maximum price history to keep
MAX_HISTORY = 120


@dataclass
class DetectionResult:
    """Result of market type detection."""
    market_type: str
    confidence: float
    up_ever_cheap: bool
    down_ever_cheap: bool
    up_min_price: float
    down_min_price: float
    recommended_params: Dict[str, Any]


class MarketTypeDetector:
    """
    Detect market type after observing prices for ~2 minutes.

    Classifies markets as:
    - SIDEWAYS: Both sides had cheap opportunities
    - TRENDING_UP: UP is favorite (never cheap), DOWN was cheap
    - TRENDING_DOWN: DOWN is favorite (never cheap), UP was cheap
    - NO_OPPORTUNITY: Neither side ever went cheap
    - UNKNOWN: Not enough data

    The logic:
    - If a side's price is cheap (< $0.45), it means low implied probability
    - If a side is NEVER cheap, it's the market favorite
    - Trending markets need tighter parameters to avoid chasing
    """

    def __init__(self, cheap_threshold: float = CHEAP_THRESHOLD):
        """
        Initialize detector.

        Args:
            cheap_threshold: Price below which is considered "cheap" (default $0.45)
        """
        self.cheap_threshold = cheap_threshold
        self.price_history: Dict[str, List[float]] = {"UP": [], "DOWN": []}
        self.market_type: str = "UNKNOWN"
        self.detected: bool = False
        self._detection_result: Optional[DetectionResult] = None

    def add_price(self, side: str, price: float) -> None:
        """
        Add a price observation.

        Args:
            side: "UP" or "DOWN"
            price: Current price
        """
        side_upper = side.upper()
        if side_upper not in self.price_history:
            return

        self.price_history[side_upper].append(price)

        # Keep only recent history
        if len(self.price_history[side_upper]) > MAX_HISTORY:
            self.price_history[side_upper] = self.price_history[side_upper][-MAX_HISTORY:]

    def has_enough_data(self) -> bool:
        """Check if we have enough data for detection."""
        up_count = len(self.price_history["UP"])
        down_count = len(self.price_history["DOWN"])
        return up_count >= MIN_OBSERVATIONS and down_count >= MIN_OBSERVATIONS

    def detect(self) -> str:
        """
        Detect market type based on collected price data.

        Call this after ~2 minutes of data collection.

        Returns:
            Market type: "SIDEWAYS", "TRENDING_UP", "TRENDING_DOWN", "NO_OPPORTUNITY", or "UNKNOWN"
        """
        if not self.has_enough_data():
            return "UNKNOWN"

        # Check if either side was EVER cheap (had opportunity to buy)
        up_ever_cheap = any(p < self.cheap_threshold for p in self.price_history["UP"])
        down_ever_cheap = any(p < self.cheap_threshold for p in self.price_history["DOWN"])

        # Get min prices for logging/debugging
        up_min = min(self.price_history["UP"]) if self.price_history["UP"] else 1.0
        down_min = min(self.price_history["DOWN"]) if self.price_history["DOWN"] else 1.0

        # Classify market type
        # Key insight: cheap = low probability = underdog
        #              never cheap = high probability = favorite
        if up_ever_cheap and down_ever_cheap:
            # Both sides had cheap prices - balanced/sideways market
            self.market_type = "SIDEWAYS"
        elif down_ever_cheap and not up_ever_cheap:
            # DOWN was cheap (underdog), UP never cheap (favorite)
            # UP is the favorite - market trending UP
            self.market_type = "TRENDING_UP"
        elif up_ever_cheap and not down_ever_cheap:
            # UP was cheap (underdog), DOWN never cheap (favorite)
            # DOWN is the favorite - market trending DOWN
            self.market_type = "TRENDING_DOWN"
        else:
            # Neither ever cheap - both expensive, no good opportunity
            self.market_type = "NO_OPPORTUNITY"

        self.detected = True

        # Build detection result
        self._detection_result = DetectionResult(
            market_type=self.market_type,
            confidence=self._calculate_confidence(),
            up_ever_cheap=up_ever_cheap,
            down_ever_cheap=down_ever_cheap,
            up_min_price=up_min,
            down_min_price=down_min,
            recommended_params=self._get_params_for_type(self.market_type),
        )

        logger.info(
            f"[MARKET_DETECTOR] Detected: {self.market_type} "
            f"(UP min=${up_min:.2f}, DOWN min=${down_min:.2f}, "
            f"UP_cheap={up_ever_cheap}, DOWN_cheap={down_ever_cheap})"
        )

        return self.market_type

    def _calculate_confidence(self) -> float:
        """Calculate confidence in detection (0-1)."""
        # More data = higher confidence
        total_obs = len(self.price_history["UP"]) + len(self.price_history["DOWN"])
        data_confidence = min(1.0, total_obs / (MIN_OBSERVATIONS * 4))

        # Clear price separation = higher confidence
        up_min = min(self.price_history["UP"]) if self.price_history["UP"] else 0.5
        down_min = min(self.price_history["DOWN"]) if self.price_history["DOWN"] else 0.5
        price_spread = abs(up_min - down_min)
        spread_confidence = min(1.0, price_spread / 0.3)  # Max confidence at 30c spread

        return (data_confidence + spread_confidence) / 2

    def _get_params_for_type(self, market_type: str) -> Dict[str, Any]:
        """Get recommended strategy parameters for market type."""
        if market_type == "SIDEWAYS":
            return {
                "max_chase_price": 0.55,
                "emergency_threshold": 10,
                "max_buys_per_side": 3,
                "description": "Balanced market - normal parameters",
            }
        elif market_type in ["TRENDING_UP", "TRENDING_DOWN"]:
            return {
                "max_chase_price": 0.50,  # Tighter - don't chase expensive side
                "emergency_threshold": 7,   # More sensitive for trending
                "max_buys_per_side": 2,     # Reduce exposure on expensive side
                "description": "Trending market - tighter parameters to avoid chasing favorite",
            }
        elif market_type == "NO_OPPORTUNITY":
            return {
                "max_chase_price": 0.45,   # Very tight - probably skip this market
                "emergency_threshold": 5,
                "max_buys_per_side": 1,
                "description": "No cheap opportunities - minimal exposure recommended",
            }
        else:  # UNKNOWN - conservative defaults
            return {
                "max_chase_price": 0.50,
                "emergency_threshold": 10,
                "max_buys_per_side": 2,
                "description": "Unknown market type - using conservative defaults",
            }

    def get_recommended_params(self) -> Dict[str, Any]:
        """
        Get recommended strategy parameters based on detected market type.

        Returns:
            Dict with recommended parameters for max_chase_price, emergency_threshold, etc.
        """
        if self._detection_result:
            return self._detection_result.recommended_params
        return self._get_params_for_type(self.market_type)

    def get_detection_result(self) -> Optional[DetectionResult]:
        """Get full detection result including confidence and analysis."""
        return self._detection_result

    def reset(self) -> None:
        """Reset detector for a new market."""
        self.price_history = {"UP": [], "DOWN": []}
        self.market_type = "UNKNOWN"
        self.detected = False
        self._detection_result = None

    def get_summary(self) -> str:
        """Get human-readable summary of detection."""
        if not self.detected:
            up_count = len(self.price_history["UP"])
            down_count = len(self.price_history["DOWN"])
            return f"Collecting data: UP={up_count}/{MIN_OBSERVATIONS}, DOWN={down_count}/{MIN_OBSERVATIONS}"

        result = self._detection_result
        if not result:
            return f"Type: {self.market_type}"

        return (
            f"Type: {result.market_type} (confidence: {result.confidence:.0%})\n"
            f"UP: min=${result.up_min_price:.2f}, cheap={result.up_ever_cheap}\n"
            f"DOWN: min=${result.down_min_price:.2f}, cheap={result.down_ever_cheap}\n"
            f"Params: chase<=${result.recommended_params['max_chase_price']:.2f}, "
            f"emergency>{result.recommended_params['emergency_threshold']}"
        )
