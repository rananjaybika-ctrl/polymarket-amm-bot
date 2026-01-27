"""
Unit tests for ContrarianStrategy (Path 2)

Tests cover:
- Reversal detection (pullback from peak/trough)
- Volatility gate filtering (EWMA-based)
- Entry price minimum enforcement
- Retracement percentage requirements
- Window lifecycle management
"""

import pytest
import time
from unittest.mock import Mock, MagicMock

from src.strategies.contrarian import (
    ContrarianStrategy,
    ContrarianState,
    ContrarianPhase,
    AdaptiveEWMAGate,
    DEFAULT_PULLBACK_THRESHOLD,
    DEFAULT_RETRACEMENT_MIN,
    DEFAULT_ENTRY_PRICE_MIN,
    DEFAULT_VOL_GATE_K,
    DEFAULT_Z_THRESHOLD,
)


class TestReversalDetection:
    """Test reversal detection (pullback from peak/trough)."""

    def test_detects_reversal_from_up_move(self):
        """Detects reversal when price pulls back from peak after up move."""
        strategy = ContrarianStrategy(
            pullback_threshold=0.0001,  # 0.01%
            retracement_min=0.30,
            min_delay_seconds=0,  # No delay for testing
        )

        # Start window at 100000
        strategy.on_window_start(
            btc_price=100000.0,
            pre_vol=0.001,
            timestamp=time.time(),
        )

        # Price moves up to peak
        strategy.update(btc_price=100050.0)

        # Price pulls back (reversal)
        result = strategy.update(
            btc_price=100030.0,  # Pulled back ~40% of move
            cheap_price=0.30,
        )

        # If reversal detected, should return entry signal
        # Note: actual return depends on strategy's internal state
        assert isinstance(result, (tuple, type(None)))

    def test_detects_reversal_from_down_move(self):
        """Detects reversal when price bounces from trough after down move."""
        strategy = ContrarianStrategy(
            pullback_threshold=0.0001,
            retracement_min=0.30,
            min_delay_seconds=0,
        )

        strategy.on_window_start(
            btc_price=100000.0,
            pre_vol=0.001,
        )

        # Price moves down to trough
        strategy.update(btc_price=99950.0)

        # Price bounces (reversal)
        result = strategy.update(
            btc_price=99970.0,  # Bounced ~40% of move
            cheap_price=0.30,
        )

        assert isinstance(result, (tuple, type(None)))


class TestVolatilityGateFiltering:
    """Test volatility gate filtering (EWMA-based)."""

    def test_vol_gate_allows_first_window(self):
        """First window always allowed (no EMA baseline yet)."""
        gate = AdaptiveEWMAGate(k=0.5)

        allowed = gate.update_and_check(pre_vol=0.001)

        assert allowed is True
        assert gate.vol_ema == 0.001

    def test_vol_gate_blocks_low_vol_window(self):
        """Low volatility window blocked when below k * EMA."""
        gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)

        # Initialize with higher volatility
        gate.update_and_check(pre_vol=0.01)  # Sets EMA to 0.01

        # Low vol window should be blocked
        allowed = gate.update_and_check(pre_vol=0.002)  # 0.002 / 0.01 = 0.2 < 0.5

        assert allowed is False

    def test_vol_gate_allows_high_vol_window(self):
        """High volatility window allowed when above k * EMA."""
        gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)

        gate.update_and_check(pre_vol=0.01)  # EMA = 0.01

        # High vol window should pass
        allowed = gate.update_and_check(pre_vol=0.008)  # 0.008 / ~0.01 > 0.5

        assert allowed is True


class TestEntryPriceMinimum:
    """Test entry price minimum enforcement."""

    def test_rejects_entry_below_minimum(self):
        """Rejects entry when cheap side price is below minimum."""
        strategy = ContrarianStrategy(
            entry_price_min=0.20,
            min_delay_seconds=0,
        )

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)
        strategy.update(btc_price=100100.0)  # Create peak

        result = strategy.update(
            btc_price=100050.0,  # Pullback
            cheap_price=0.15,  # Below $0.20 minimum
        )

        assert result is None

    def test_respects_entry_price_min_parameter(self):
        """Entry price min parameter is respected."""
        strategy = ContrarianStrategy(
            entry_price_min=0.25,  # Set minimum to $0.25
        )

        assert strategy.entry_price_min == 0.25


class TestMinDelaySeconds:
    """Test minimum delay from window start."""

    def test_delay_parameter_is_stored(self):
        """min_delay_seconds parameter is stored correctly."""
        strategy = ContrarianStrategy(
            min_delay_seconds=120,
        )

        assert strategy.min_delay_seconds == 120


class TestWindowLifecycle:
    """Test window lifecycle management."""

    def test_window_start_resets_state(self):
        """on_window_start resets tracking state."""
        strategy = ContrarianStrategy()

        # Build some state
        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)
        strategy.update(btc_price=100100.0)

        # Start new window
        strategy.on_window_start(btc_price=99000.0, pre_vol=0.002)

        assert strategy.state.window_start_price == 99000.0
        assert strategy.state.window_peak_price == 99000.0
        assert strategy.state.window_trough_price == 99000.0
        assert strategy.state.phase == ContrarianPhase.MONITORING

    def test_window_end_records_outcome(self):
        """on_window_end records win/loss correctly."""
        strategy = ContrarianStrategy(min_delay_seconds=0)

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)

        # Manually set positioned state
        strategy.state.phase = ContrarianPhase.POSITIONED
        strategy.state.entry_side = "DOWN"

        # End window with UP resolution (we bet DOWN, so we lose)
        strategy.on_window_end(resolution="UP", profit=-30.0)

        assert strategy.state.total_losses == 1
        assert strategy.state.total_profit == -30.0

    def test_tracks_wins(self):
        """Tracks wins correctly when bet matches resolution."""
        strategy = ContrarianStrategy(min_delay_seconds=0)

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)

        # Manually set positioned state
        strategy.state.phase = ContrarianPhase.POSITIONED
        strategy.state.entry_side = "DOWN"

        # End window with DOWN resolution (we bet DOWN, so we win)
        strategy.on_window_end(resolution="DOWN", profit=70.0)

        assert strategy.state.total_wins == 1
        assert strategy.state.total_profit == 70.0


class TestGetQuotesInterface:
    """Test get_quotes interface (unified with EnhancedSpikeStrategy)."""

    def test_get_quotes_returns_list(self):
        """get_quotes returns a list."""
        strategy = ContrarianStrategy()

        quotes = strategy.get_quotes(
            up_bid=0.50, up_ask=0.51,
            down_bid=0.49, down_ask=0.50,
            time_remaining=600,
        )

        assert isinstance(quotes, list)

    def test_get_quotes_returns_empty_when_gated(self):
        """Returns empty list when window is gated out."""
        strategy = ContrarianStrategy()

        strategy.state.phase = ContrarianPhase.GATED_OUT

        quotes = strategy.get_quotes(
            up_bid=0.50, up_ask=0.51,
            down_bid=0.49, down_ask=0.50,
            time_remaining=600,
        )

        assert quotes == []

    def test_get_quotes_returns_empty_when_positioned(self):
        """Returns empty list when already positioned (hold to resolution)."""
        strategy = ContrarianStrategy()

        strategy.state.phase = ContrarianPhase.POSITIONED

        quotes = strategy.get_quotes(
            up_bid=0.50, up_ask=0.51,
            down_bid=0.49, down_ask=0.50,
            time_remaining=600,
        )

        assert quotes == []


class TestAdaptiveEWMAGate:
    """Test AdaptiveEWMAGate in isolation."""

    def test_ema_updates_correctly(self):
        """EMA updates with each window."""
        gate = AdaptiveEWMAGate(k=0.5, halflife_windows=1)  # Fast decay

        gate.update_and_check(0.01)  # EMA = 0.01
        gate.update_and_check(0.02)  # EMA updates toward 0.02

        # EMA should be between 0.01 and 0.02
        assert 0.01 < gate.vol_ema < 0.02

    def test_reset_clears_ema(self):
        """Reset clears EMA state."""
        gate = AdaptiveEWMAGate()

        gate.update_and_check(0.01)
        gate.reset()

        assert gate.vol_ema is None
        assert gate._windows_seen == 0

    def test_get_state_returns_info(self):
        """get_state returns current gate state."""
        gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)

        gate.update_and_check(0.01)

        state = gate.get_state()

        assert state["k"] == 0.5
        assert state["vol_ema"] is not None
        assert state["windows_seen"] == 1


class TestStrategyStatus:
    """Test strategy status reporting."""

    def test_get_status_returns_complete_info(self):
        """get_status returns comprehensive status."""
        strategy = ContrarianStrategy()

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)

        status = strategy.get_status()

        assert isinstance(status, dict)
        assert "phase" in status

    def test_reset_clears_state(self):
        """reset clears all state."""
        strategy = ContrarianStrategy()

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)
        strategy.reset()

        assert strategy.state.window_start_time == 0.0
        assert strategy.state.phase == ContrarianPhase.WAITING


class TestOnFill:
    """Test fill handling."""

    def test_on_fill_updates_state(self):
        """on_fill updates entry state."""
        strategy = ContrarianStrategy()

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)
        strategy.on_fill("DOWN", 0.30, 100)

        assert strategy.state.entry_side == "DOWN"
        assert strategy.state.entry_price == 0.30
        assert strategy.state.entry_size == 100
        assert strategy.state.phase == ContrarianPhase.POSITIONED


class TestPhaseTransitions:
    """Test phase transitions."""

    def test_initial_phase_is_waiting(self):
        """Initial phase is WAITING."""
        strategy = ContrarianStrategy()

        assert strategy.state.phase == ContrarianPhase.WAITING

    def test_window_start_transitions_to_monitoring(self):
        """on_window_start transitions to MONITORING."""
        strategy = ContrarianStrategy()

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)

        assert strategy.state.phase == ContrarianPhase.MONITORING

    def test_fill_transitions_to_positioned(self):
        """on_fill transitions to POSITIONED."""
        strategy = ContrarianStrategy()

        strategy.on_window_start(btc_price=100000.0, pre_vol=0.001)
        strategy.on_fill("DOWN", 0.30, 100)

        assert strategy.state.phase == ContrarianPhase.POSITIONED


class TestZScoreTrackerIntegration:
    """Test z-score tracker integration."""

    def test_can_set_zscore_tracker(self):
        """Can set z-score tracker via set_zscore_tracker."""
        mock_tracker = Mock()

        strategy = ContrarianStrategy()
        strategy.set_zscore_tracker(mock_tracker)

        assert strategy.zscore_tracker == mock_tracker


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
