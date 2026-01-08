"""
Unit tests for SpreadCaptureStrategy

Tests cover:
- Z-score tier classification
- Entry offset calculations
- Hedge offset calculations
- Profit ceiling (max hedge price) calculations
- Wait time calculations
- State machine phase transitions
- Fill handling
"""

import pytest
import time
from src.strategies.spread_capture import (
    SpreadCaptureStrategy,
    SpreadCaptureState,
    SpreadCapturePhase,
    Z_STRONG_THRESHOLD,
    Z_SLIGHT_THRESHOLD,
)


class TestTierClassification:
    """Test z-score tier classification."""

    def test_strong_tier(self):
        """z >= 2.0 should be strong tier."""
        strategy = SpreadCaptureStrategy()
        assert strategy.get_tier(2.0) == "strong"
        assert strategy.get_tier(2.5) == "strong"
        assert strategy.get_tier(5.0) == "strong"

    def test_slight_tier(self):
        """1.0 <= z < 2.0 should be slight tier."""
        strategy = SpreadCaptureStrategy()
        assert strategy.get_tier(1.0) == "slight"
        assert strategy.get_tier(1.5) == "slight"
        assert strategy.get_tier(1.99) == "slight"

    def test_neutral_tier(self):
        """z < 1.0 should be neutral tier."""
        strategy = SpreadCaptureStrategy()
        assert strategy.get_tier(0.0) == "neutral"
        assert strategy.get_tier(0.5) == "neutral"
        assert strategy.get_tier(0.99) == "neutral"


class TestZFavorability:
    """Test z-score favorability determination."""

    def test_favorable_up_trend(self):
        """UP entry in UP trend is favorable."""
        strategy = SpreadCaptureStrategy()
        assert strategy.is_z_favorable("UP", "UP", 2.0) is True
        assert strategy.is_z_favorable("UP", "UP", 1.5) is True

    def test_favorable_down_trend(self):
        """DOWN entry in DOWN trend is favorable."""
        strategy = SpreadCaptureStrategy()
        assert strategy.is_z_favorable("DOWN", "DOWN", 2.0) is True
        assert strategy.is_z_favorable("DOWN", "DOWN", 1.5) is True

    def test_unfavorable_opposite_trend(self):
        """Entry opposite to trend is unfavorable."""
        strategy = SpreadCaptureStrategy()
        assert strategy.is_z_favorable("UP", "DOWN", 2.0) is False
        assert strategy.is_z_favorable("DOWN", "UP", 2.0) is False

    def test_neutral_z_not_favorable(self):
        """z < 1.0 is never favorable (neutral zone)."""
        strategy = SpreadCaptureStrategy()
        assert strategy.is_z_favorable("UP", "UP", 0.5) is False
        assert strategy.is_z_favorable("DOWN", "DOWN", 0.9) is False


class TestEntryOffset:
    """Test entry offset calculations."""

    def test_strong_favorable_offset(self):
        """Strong z + favorable = 0.00 offset (at best bid)."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_entry_offset(2.5, is_z_favorable=True)
        assert offset == 0.0

    def test_strong_unfavorable_offset(self):
        """Strong z + unfavorable = 0.01 offset."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_entry_offset(2.5, is_z_favorable=False)
        assert offset == 0.01

    def test_slight_favorable_offset(self):
        """Slight z (1.5) + favorable = ~0.005 offset (interpolated)."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_entry_offset(1.5, is_z_favorable=True)
        assert 0.0 < offset < 0.01

    def test_slight_unfavorable_offset(self):
        """Slight z + unfavorable adds 0.01 extra patience."""
        strategy = SpreadCaptureStrategy()
        offset_fav = strategy.calculate_entry_offset(1.5, is_z_favorable=True)
        offset_unfav = strategy.calculate_entry_offset(1.5, is_z_favorable=False)
        assert offset_unfav == offset_fav + 0.01

    def test_neutral_offset(self):
        """Neutral z = 0.01 offset."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_entry_offset(0.5, is_z_favorable=False)
        assert offset == 0.01


class TestHedgeOffset:
    """Test hedge offset calculations."""

    def test_strong_hedge_offset(self):
        """Strong z = 0.03 hedge offset (targeting ~0.06 spread)."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset(2.5)
        assert offset == 0.03

    def test_slight_hedge_offset(self):
        """Slight z (1.5) = interpolated offset between 0.02 and 0.03."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset(1.5)
        assert 0.02 < offset < 0.03

    def test_neutral_hedge_offset(self):
        """Neutral z = 0.01 hedge offset."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset(0.5)
        assert offset == 0.01

    def test_edge_of_slight(self):
        """z = 1.0 should be exactly 0.02."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset(1.0)
        assert offset == 0.02

    def test_edge_of_strong(self):
        """z = 2.0 should be exactly 0.03."""
        strategy = SpreadCaptureStrategy()
        offset = strategy.calculate_hedge_offset(2.0)
        assert offset == 0.03


class TestMaxHedgePrice:
    """Test profit ceiling (max hedge price) calculations."""

    def test_profit_ceiling_preserves_min_profit(self):
        """Max hedge price should leave min_profit margin."""
        strategy = SpreadCaptureStrategy(min_profit=0.005)
        # Entry at 0.55 means max hedge = 1.00 - 0.55 - 0.005 = 0.445
        max_hedge = strategy.calculate_max_hedge_price(0.55)
        assert max_hedge == 0.445

    def test_profit_ceiling_high_entry(self):
        """High entry price leaves less room for hedge."""
        strategy = SpreadCaptureStrategy(min_profit=0.005)
        # Entry at 0.80 means max hedge = 1.00 - 0.80 - 0.005 = 0.195
        max_hedge = strategy.calculate_max_hedge_price(0.80)
        assert max_hedge == 0.195

    def test_profit_ceiling_custom_min_profit(self):
        """Custom min_profit should be respected."""
        strategy = SpreadCaptureStrategy(min_profit=0.01)
        # Entry at 0.50 means max hedge = 1.00 - 0.50 - 0.01 = 0.49
        max_hedge = strategy.calculate_max_hedge_price(0.50)
        assert max_hedge == 0.49


class TestWaitTime:
    """Test wait time calculations."""

    def test_strong_entry_wait(self):
        """Strong z = shorter wait (5s base)."""
        strategy = SpreadCaptureStrategy()
        wait = strategy.calculate_wait_time(2.5, attempt=0, is_entry=True)
        assert wait == 5.0

    def test_slight_entry_wait(self):
        """Slight z = longer wait (10s base)."""
        strategy = SpreadCaptureStrategy()
        wait = strategy.calculate_wait_time(1.5, attempt=0, is_entry=True)
        assert wait == 10.0

    def test_hedge_gets_more_patience(self):
        """Hedge wait = 1.5x entry wait."""
        strategy = SpreadCaptureStrategy()
        entry_wait = strategy.calculate_wait_time(2.0, attempt=0, is_entry=True)
        hedge_wait = strategy.calculate_wait_time(2.0, attempt=0, is_entry=False)
        assert hedge_wait == entry_wait * 1.5

    def test_exponential_backoff(self):
        """Each retry multiplies wait time by escalation factor."""
        strategy = SpreadCaptureStrategy(retry_escalation=1.5)
        wait0 = strategy.calculate_wait_time(2.0, attempt=0, is_entry=True)
        wait1 = strategy.calculate_wait_time(2.0, attempt=1, is_entry=True)
        wait2 = strategy.calculate_wait_time(2.0, attempt=2, is_entry=True)
        assert wait1 == wait0 * 1.5
        assert wait2 == wait0 * (1.5 ** 2)

    def test_max_wait_cap(self):
        """Wait time should not exceed 60s."""
        strategy = SpreadCaptureStrategy()
        wait = strategy.calculate_wait_time(1.0, attempt=10, is_entry=True)
        assert wait <= 60.0


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
            z_score=2.0,
            trend_direction="UP",
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
            z_score=2.0,
            trend_direction="UP",
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
            z_score=2.0,
            trend_direction="UP",
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
            z_score=2.0,
            trend_direction="UP",
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
            z_score=2.0,
            trend_direction="UP",
            time_remaining=600,
            current_imbalance=5,  # Below threshold
            current_time=time.time()
        )
        assert action is not None
        assert strategy.state.phase != SpreadCapturePhase.EMERGENCY_DEFERRED


class TestEntrySideSelection:
    """Test entry side selection logic."""

    def test_favors_trend_direction_strong_z(self):
        """Strong z should follow trend direction for entry."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            z_score=2.5,
            trend_direction="UP",
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert strategy.state.entry_side == "UP"
        assert strategy.state.hedge_side == "DOWN"

    def test_follows_down_trend(self):
        """Strong DOWN trend should enter DOWN first."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            z_score=2.5,
            trend_direction="DOWN",
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert strategy.state.entry_side == "DOWN"
        assert strategy.state.hedge_side == "UP"

    def test_neutral_z_enters_cheaper_side(self):
        """Neutral z should enter cheaper side."""
        strategy = SpreadCaptureStrategy()
        strategy.decide(
            up_bid=0.40, up_ask=0.42,  # UP is cheaper
            down_bid=0.56, down_ask=0.58,
            z_score=0.5,  # Neutral
            trend_direction="FLAT",
            time_remaining=600,
            current_imbalance=0,
            current_time=time.time()
        )
        assert strategy.state.entry_side == "UP"  # Cheaper side


class TestCompleteCycle:
    """Test complete entry+hedge cycle."""

    def test_full_cycle_completion(self):
        """Test entry fill -> hedge fill -> cycle complete."""
        strategy = SpreadCaptureStrategy(entry_size=5, target_shares=5)

        # Place entry
        action1 = strategy.decide(
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            z_score=2.0,
            trend_direction="UP",
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
            z_score=2.0,
            trend_direction="UP",
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
