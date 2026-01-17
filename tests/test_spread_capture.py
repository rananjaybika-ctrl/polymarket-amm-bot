"""
Unit tests for SpreadCaptureStrategy

Tests cover:
- Fixed entry/hedge offset calculations
- Profit ceiling (max hedge price) calculations
- Wait time calculations
- State machine phase transitions
- Fill handling
- Velocity-based quote pulling
"""

import pytest
import time
from src.strategies.spread_capture import (
    SpreadCaptureStrategy,
    SpreadCaptureState,
    SpreadCapturePhase,
    DEFAULT_ENTRY_OFFSET,
    DEFAULT_HEDGE_OFFSET,
    VELOCITY_PULL_THRESHOLD,
)


class TestEntryOffset:
    """Test entry offset calculations (now fixed)."""

    def test_entry_offset_is_fixed(self):
        """Entry offset is fixed at DEFAULT_ENTRY_OFFSET."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_entry_offset()
        assert offset == DEFAULT_ENTRY_OFFSET
        assert offset == 0.01

    def test_entry_offset_custom(self):
        """Custom entry offset can be set via constructor."""
        strategy = SpreadCaptureStrategy(entry_offset=0.02)
        offset = strategy.calculate_entry_offset()
        assert offset == 0.02


class TestHedgeOffset:
    """Test hedge offset calculations (now fixed)."""

    def test_hedge_offset_is_fixed(self):
        """Hedge offset is fixed at DEFAULT_HEDGE_OFFSET."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset()
        assert offset == DEFAULT_HEDGE_OFFSET
        assert offset == 0.02

    def test_hedge_offset_custom(self):
        """Custom hedge offset can be set via constructor."""
        strategy = SpreadCaptureStrategy(hedge_offset=0.03)
        offset = strategy.calculate_hedge_offset()
        assert offset == 0.03


class TestMaxHedgePrice:
    """Test profit ceiling (max hedge price) calculations.

    Note: Calculations account for ~1% maker rebates on both sides.
    Formula: max_pair_cost = (1.00 - min_profit) / 0.99
    max_hedge = max_pair_cost - entry_price
    """

    def test_profit_ceiling_preserves_min_profit(self):
        """Max hedge price should leave min_profit margin after rebates."""
        strategy = SpreadCaptureStrategy(min_profit=0.005)
        max_hedge = strategy.calculate_max_hedge_price(0.55)
        # With rebates: max_pair_cost = 0.995/0.99 ≈ 1.00505
        # max_hedge = 1.00505 - 0.55 ≈ 0.4551
        assert 0.45 < max_hedge < 0.46

    def test_profit_ceiling_high_entry(self):
        """High entry price leaves less room for hedge."""
        strategy = SpreadCaptureStrategy(min_profit=0.005)
        max_hedge = strategy.calculate_max_hedge_price(0.80)
        # max_hedge = 1.00505 - 0.80 ≈ 0.2051
        assert 0.20 < max_hedge < 0.21

    def test_profit_ceiling_custom_min_profit(self):
        """Custom min_profit should be respected."""
        strategy = SpreadCaptureStrategy(min_profit=0.01)
        max_hedge = strategy.calculate_max_hedge_price(0.50)
        # With rebates: max_pair_cost = 0.99/0.99 = 1.00
        # max_hedge = 1.00 - 0.50 = 0.50
        assert max_hedge == 0.50


class TestWaitTime:
    """Test wait time calculations."""

    def test_base_entry_wait(self):
        """Entry wait time starts at configured base."""
        strategy = SpreadCaptureStrategy()
        wait = strategy.calculate_wait_time(attempt=0, is_entry=True)
        assert wait == 8.0  # DEFAULT_ENTRY_WAIT

    def test_base_hedge_wait(self):
        """Hedge wait time depends on price room."""
        strategy = SpreadCaptureStrategy()
        # Hedge wait with $0.10 price room
        hedge_wait = strategy.calculate_wait_time(
            attempt=0, is_entry=False, price_room=0.10
        )
        assert 25.0 <= hedge_wait <= 35.0  # ~30s for $0.10 room

    def test_exponential_backoff(self):
        """Wait time increases with retry attempts."""
        strategy = SpreadCaptureStrategy()
        wait0 = strategy.calculate_wait_time(attempt=0, is_entry=True)
        wait1 = strategy.calculate_wait_time(attempt=1, is_entry=True)
        wait2 = strategy.calculate_wait_time(attempt=2, is_entry=True)
        # Uses 1.3x backoff
        assert wait1 > wait0
        assert wait2 > wait1

    def test_max_wait_cap(self):
        """Wait time should not exceed MAX_WAIT_TIME."""
        strategy = SpreadCaptureStrategy()
        wait = strategy.calculate_wait_time(attempt=10, is_entry=True)
        assert wait <= 60.0  # MAX_WAIT_TIME


class TestStateTransitions:
    """Test state machine phase transitions."""

    def test_initial_state_is_idle(self):
        """Strategy starts in IDLE phase."""
        strategy = SpreadCaptureStrategy()
        assert strategy.state.phase == SpreadCapturePhase.IDLE

    def test_decide_transitions_to_entry_pending(self):
        """First decide() should place entry and transition to ENTRY_PENDING."""
        strategy = SpreadCaptureStrategy()
        action = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert action is not None
        assert strategy.state.phase == SpreadCapturePhase.ENTRY_PENDING

    def test_entry_fill_transitions_to_entry_filled(self):
        """Entry fill should transition to ENTRY_FILLED."""
        strategy = SpreadCaptureStrategy()
        # Place entry
        strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        # Simulate fill
        entry_side = strategy.state.entry_side
        strategy.on_fill(side=entry_side, price=0.55, size=5)
        assert strategy.state.phase == SpreadCapturePhase.ENTRY_FILLED

    def test_reset_returns_to_idle(self):
        """Reset should return to IDLE phase."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        strategy.reset()
        assert strategy.state.phase == SpreadCapturePhase.IDLE


class TestEmergencyDeferral:
    """Test emergency imbalance deferral."""

    def test_high_imbalance_defers_to_emergency(self):
        """Imbalance >= threshold should defer to emergency."""
        strategy = SpreadCaptureStrategy(emergency_imbalance_threshold=10)
        action = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=15,  # Exceeds threshold
            current_time=time.time()
        )
        assert action is None
        assert strategy.state.phase == SpreadCapturePhase.EMERGENCY_DEFERRED

    def test_normal_imbalance_does_not_defer(self):
        """Imbalance < threshold should not defer."""
        strategy = SpreadCaptureStrategy(emergency_imbalance_threshold=10)
        action = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=5,  # Below threshold
            current_time=time.time()
        )
        assert action is not None
        assert strategy.state.phase != SpreadCapturePhase.EMERGENCY_DEFERRED


class TestEntrySideSelection:
    """Test entry side selection logic - always enters EXPENSIVE side first."""

    def test_enters_expensive_side_up(self):
        """When UP is more expensive, enter UP first."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.55, up_ask=0.56,  # UP is more expensive (0.56 > 0.45)
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert strategy.state.entry_side == "UP"
        assert strategy.state.hedge_side == "DOWN"

    def test_enters_expensive_side_down(self):
        """When DOWN is more expensive, enter DOWN first."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.40, up_ask=0.42,
            down_bid=0.56, down_ask=0.58,  # DOWN is more expensive (0.58 > 0.42)
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert strategy.state.entry_side == "DOWN"
        assert strategy.state.hedge_side == "UP"


class TestVelocityPulling:
    """Test velocity-based quote pulling logic."""

    def test_velocity_pull_threshold_constant(self):
        """Velocity pull threshold should be 0.05 bps/sec."""
        assert VELOCITY_PULL_THRESHOLD == 0.05

    def test_should_pull_up_entry_adverse(self):
        """UP entry should be pulled when velocity is strongly negative."""
        strategy = SpreadCaptureStrategy()
        # UP entry: adverse if velocity < -0.05 (BTC falling, UP getting expensive)
        assert strategy.should_pull_entry(velocity_bps=-0.10, entry_side="UP") is True
        assert strategy.should_pull_entry(velocity_bps=-0.05, entry_side="UP") is False
        assert strategy.should_pull_entry(velocity_bps=0.05, entry_side="UP") is False

    def test_should_pull_down_entry_adverse(self):
        """DOWN entry should be pulled when velocity is strongly positive."""
        strategy = SpreadCaptureStrategy()
        # DOWN entry: adverse if velocity > 0.05 (BTC rising, DOWN getting expensive)
        assert strategy.should_pull_entry(velocity_bps=0.10, entry_side="DOWN") is True
        assert strategy.should_pull_entry(velocity_bps=0.05, entry_side="DOWN") is False
        assert strategy.should_pull_entry(velocity_bps=-0.05, entry_side="DOWN") is False


class TestCompleteCycle:
    """Test complete entry+hedge cycle."""

    def test_full_cycle_completion(self):
        """Test entry fill -> hedge fill -> cycle complete."""
        # Disable cycling to test COMPLETE phase (cycling resets to IDLE)
        strategy = SpreadCaptureStrategy(entry_size=5, target_shares=5, enable_cycling=False)

        # Place entry
        action1 = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert action1 is not None
        entry_side = strategy.state.entry_side

        # Entry fills
        strategy.on_fill(side=entry_side, price=0.55, size=5)
        assert strategy.state.phase == SpreadCapturePhase.ENTRY_FILLED

        # Place hedge
        action2 = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert action2 is not None
        hedge_side = strategy.state.hedge_side

        # Hedge fills
        strategy.on_fill(side=hedge_side, price=0.44, size=5)
        assert strategy.state.phase == SpreadCapturePhase.COMPLETE
        assert strategy.state.cycles_completed == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
