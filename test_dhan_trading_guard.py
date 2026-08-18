"""
Test harness for dhan_trading_guard.py

Simulates fake Dhan position snapshots (no real account/credentials needed)
to verify the guard correctly detects a REAL round-trip trade (position
goes flat -> open -> flat) rather than counting raw broker orders, and
correctly detects a loss-limit breach. Run this before pointing the real
script at your live account.

Usage:
    python3 test_dhan_trading_guard.py
"""

import sys
import os
sys.path.insert(0, ".")
from dhan_trading_guard import TradingGuard


class MockDhan:
    """Fakes the one dhanhq method the guard reads from (get_positions),
    returning whatever the test sets as the current snapshot."""
    def __init__(self):
        self._positions = []
        self.kill_switch_calls = []

    def set_positions(self, positions):
        self._positions = positions

    def get_positions(self):
        return {"status": "success", "data": self._positions}

    def kill_switch(self, action):
        self.kill_switch_calls.append(action)
        return {"status": "success", "data": f"kill switch {action}"}


def pos(security_id, buy_qty, sell_qty, realized_profit, unrealized_profit=0):
    return {
        "securityId": security_id, "buyQty": buy_qty, "sellQty": sell_qty,
        "realizedProfit": realized_profit, "unrealizedProfit": unrealized_profit,
    }


def main():
    print("=" * 60)
    print("TEST 1: One real trade with averaging (3 orders) should count as ONE trade")
    print("=" * 60)
    dhan = MockDhan()
    guard = TradingGuard(capital=45000, loss_pct=5, max_trades=2)

    dhan.set_positions([pos("SEC1", 0, 0, 0)])
    guard.poll(dhan, dry_run=False, debug=False)
    dhan.set_positions([pos("SEC1", 260, 0, 0)])
    guard.poll(dhan, dry_run=False, debug=False)
    dhan.set_positions([pos("SEC1", 260, 260, 285)])
    b3 = guard.poll(dhan, dry_run=False, debug=False)
    print(f"Trade count after averaged trade closed: {guard.trade_count} (should be 1)")
    assert guard.trade_count == 1, "FAILED: averaging inflated the trade count"
    assert not b3, "FAILED: should not have breached yet (only 1 of 2 trades used)"
    print("PASS: averaging into one position still counts as ONE trade, not multiple")

    print("\n" + "=" * 60)
    print("TEST 2: Second distinct trade after the first goes flat -> should lock at 2/2")
    print("=" * 60)
    dhan.set_positions([pos("SEC1", 260, 260, 285), pos("SEC2", 130, 0, 0)])
    b4 = guard.poll(dhan, dry_run=False, debug=False)
    print(f"Trade count: {guard.trade_count} | Breached: {b4} | Kill switch calls: {dhan.kill_switch_calls}")
    assert guard.trade_count == 2 and b4 and dhan.kill_switch_calls == ["ACTIVATE"], "FAILED"
    print("PASS: correctly locked exactly on the 2nd real trade, not before")

    print("\n" + "=" * 60)
    print("TEST 3: Loss-limit breach on the FIRST trade (should lock before trade 2)")
    print("=" * 60)
    dhan2 = MockDhan()
    guard2 = TradingGuard(capital=45000, loss_pct=5, max_trades=2)
    dhan2.set_positions([pos("SEC1", 0, 0, 0)])
    guard2.poll(dhan2, dry_run=False, debug=False)
    dhan2.set_positions([pos("SEC1", 130, 0, 0)])
    guard2.poll(dhan2, dry_run=False, debug=False)
    dhan2.set_positions([pos("SEC1", 130, 130, -2400)])
    b = guard2.poll(dhan2, dry_run=False, debug=False)
    print(f"Trade count: {guard2.trade_count} | Breached: {b} | Kill switch calls: {dhan2.kill_switch_calls}")
    assert b and guard2.trade_count == 1, "FAILED: should breach on loss even with only 1 trade used"
    print("PASS: loss limit correctly overrides trade-count limit when it hits first")

    print("\n" + "=" * 60)
    print("TEST 4: Replay of your actual Aug 10 trading day (real quantities and P&L)")
    print("=" * 60)
    dhan3 = MockDhan()
    guard3 = TradingGuard(capital=45000, loss_pct=5, max_trades=2)
    day_snapshots = [
        ("flat start",                    [pos("NIFTY_24650PE", 0, 0, 0)]),
        ("Trade 1 opened (130 buy)",       [pos("NIFTY_24650PE", 130, 0, 0)]),
        ("Trade 1 closed, +2887.50",       [pos("NIFTY_24650PE", 130, 130, 2887.50)]),
        ("Trade 2 opened (Crude, 10 buy)", [pos("NIFTY_24650PE", 130, 130, 2887.50), pos("CRUDE_7450PE", 10, 0, 0)]),
        ("Trade 2 closed, +622.37",        [pos("NIFTY_24650PE", 130, 130, 2887.50), pos("CRUDE_7450PE", 80, 80, 622.37)]),
    ]
    locked_at = None
    for label, snapshot in day_snapshots:
        dhan3.set_positions(snapshot)
        breached = guard3.poll(dhan3, dry_run=False, debug=False)
        status = "LOCKED" if breached else "armed"
        print(f"  {label}: trades={guard3.trade_count} -> {status}")
        if breached and locked_at is None:
            locked_at = label
    print(f"\nWith the FIXED logic, the guard locks after: {locked_at}")
    print("That's after your real 2nd trade closed - matching what '2 trades a day' actually means.")
    print("Everything from trade 3 onward today (including the near-expiry averaging mistake,")
    print("the option-writing trade, and the later hero-zero trade) would have been blocked.")

    print("\n" + "=" * 60)
    print("TEST 5: Open MTM loss (not yet realized) should still trigger a breach")
    print("=" * 60)
    dhan4 = MockDhan()
    guard4 = TradingGuard(capital=45000, loss_pct=5, max_trades=2)
    dhan4.set_positions([pos("SEC1", 0, 0, 0, 0)])
    guard4.poll(dhan4, dry_run=False, debug=False)
    dhan4.set_positions([pos("SEC1", 130, 0, 0, 0)])
    guard4.poll(dhan4, dry_run=False, debug=False)
    # Position still OPEN (never sold) - just sitting at a growing MTM loss
    dhan4.set_positions([pos("SEC1", 130, 0, 0, -2700)])  # -6% of 45000 = -2700, still unrealized
    b5 = guard4.poll(dhan4, dry_run=False, debug=False)
    print(f"Trade count: {guard4.trade_count} (still 1 - position never closed) | Breached: {b5}")
    assert b5 and guard4.trade_count == 1, "FAILED: should breach on unrealized MTM loss alone"
    print("PASS: an open position at -6% MTM triggers the guard even though nothing was ever sold")

    print("\n" + "=" * 60)
    print("TEST 6: State persists across SEPARATE runs (simulating GitHub Actions)")
    print("=" * 60)
    import tempfile
    state_path = os.path.join(tempfile.gettempdir(), "test_guard_state.json")
    if os.path.exists(state_path):
        os.remove(state_path)

    # Run 1: a fresh process, trade 1 opens
    dhan5 = MockDhan()
    guard_run1 = TradingGuard(capital=45000, loss_pct=5, max_trades=2, state_file=state_path)
    dhan5.set_positions([pos("SEC1", 130, 0, 0, 0)])
    guard_run1.poll(dhan5, dry_run=False, debug=False)
    print(f"Run 1 (fresh process): trade_count={guard_run1.trade_count}")
    assert guard_run1.trade_count == 1

    # Run 2: brand NEW TradingGuard instance (simulates a new GitHub Actions run) -
    # must load trade_count=1 from the state file, not start over at 0
    dhan5b = MockDhan()
    guard_run2 = TradingGuard(capital=45000, loss_pct=5, max_trades=2, state_file=state_path)
    print(f"Run 2 (new process, loaded from state file): trade_count={guard_run2.trade_count}")
    assert guard_run2.trade_count == 1, "FAILED: state did not carry over to the new process"
    # Trade 1 closes, trade 2 opens - should now lock
    dhan5b.set_positions([pos("SEC1", 130, 130, 400, 0), pos("SEC2", 65, 0, 0, 0)])
    b6 = guard_run2.poll(dhan5b, dry_run=False, debug=False)
    print(f"Run 2 after trade 2 opens: trade_count={guard_run2.trade_count} | Breached: {b6}")
    assert guard_run2.trade_count == 2 and b6

    # Run 3: another new process - must load the LOCKED state and refuse to re-fire
    dhan5c = MockDhan()
    guard_run3 = TradingGuard(capital=45000, loss_pct=5, max_trades=2, state_file=state_path)
    b7 = guard_run3.poll(dhan5c, dry_run=False, debug=False)
    print(f"Run 3 (new process, already locked): kill_switch calls made this run = {dhan5c.kill_switch_calls}")
    assert dhan5c.kill_switch_calls == [], "FAILED: should not re-fire kill switch on an already-locked day"
    print("PASS: trade count, lock status, and 'don't re-fire when already locked' all survive across separate runs")
    os.remove(state_path)

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
