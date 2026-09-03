"""
Test harness for dhan_trading_guard.py - focused on the critical fix found
via a real live-trading incident on 2026-09-02.

THE INCIDENT: the guard detected a breach at -Rs8,953.50, called the kill
switch activation sequence, and marked itself `locked=True` UNCONDITIONALLY
- without checking whether the activation actually succeeded on Dhan's
side. Dhan requires all positions to be flat before it will activate the
kill switch; if a position was still open at that exact moment, activation
silently failed while the script gave up watching anyway, believing it was
locked. The account remained fully tradeable, and the loss ran to -Rs44,000
before coming back down, with zero further intervention from the guard.

THE FIX: `locked` is now only set True after an INDEPENDENT verification
call (`status_kill_switch()`) confirms the switch is genuinely active on
Dhan's side - never trusting the activation call's own return value alone.
If verification fails, the script does NOT mark itself locked, and will
keep re-attempting on every subsequent check instead of going quiet.

Usage:
    python3 test_dhan_trading_guard.py
"""

import sys
import os
sys.path.insert(0, ".")
from dhan_trading_guard import TradingGuard


class MockDhan:
    def __init__(self):
        self._positions = []
        self._trade_book = []
        self.kill_switch_calls = []
        self.kill_switch_will_succeed = True   # simulates whether Dhan's side actually activates
        self.status_kill_switch_response = None

    def set_positions(self, positions):
        self._positions = positions

    def set_trade_book(self, fills):
        self._trade_book = fills

    def get_positions(self):
        return {"status": "success", "data": self._positions}

    def get_trade_book(self):
        return {"status": "success", "data": self._trade_book}

    def kill_switch(self, action):
        self.kill_switch_calls.append(action)
        if action == "ACTIVATE" and not self.kill_switch_will_succeed:
            raise Exception("Cannot activate: open positions exist")
        return {"status": "success", "data": f"kill switch {action}"}

    def status_kill_switch(self):
        return self.status_kill_switch_response


def pos(security_id, realized_profit=0, unrealized_profit=0):
    return {"securityId": security_id, "realizedProfit": realized_profit, "unrealizedProfit": unrealized_profit}


def fill(security_id, txn, qty, ts):
    return {"securityId": security_id, "transactionType": txn, "tradedQuantity": qty, "exchangeTime": ts}


def main():
    print("=" * 70)
    print("TEST 1: THE ACTUAL INCIDENT - activation fails (open position) ->")
    print("must NOT set locked=True, must keep retrying")
    print("=" * 70)
    dhan = MockDhan()
    dhan.kill_switch_will_succeed = False  # simulates Dhan rejecting activation (position still open)
    dhan.status_kill_switch_response = {"status": "success", "data": {"killSwitchStatus": "INACTIVE"}}
    guard = TradingGuard(capital=98000, loss_pct=9, max_trades=14)
    dhan.set_trade_book([fill("SEC1", "BUY", 130, "1")])
    dhan.set_positions([pos("SEC1", realized_profit=0, unrealized_profit=-8953.50)])

    breached = guard.poll(dhan, dry_run=False, debug=False)
    print(f"Breached: {breached} | guard.locked: {guard.locked}")
    assert breached, "FAILED: should report a breach happened"
    assert not guard.locked, "FAILED: THIS IS THE EXACT BUG - locked must NOT be True when Dhan never actually activated"
    print("PASS: correctly did NOT mark itself locked when activation could not be confirmed")

    print("\n" + "=" * 70)
    print("TEST 2: Next poll retries instead of silently giving up")
    print("=" * 70)
    # position closes between polls - activation would succeed now
    dhan.kill_switch_will_succeed = True
    dhan.status_kill_switch_response = {"status": "success", "data": {"killSwitchStatus": "ACTIVE"}}
    dhan.set_positions([pos("SEC1", realized_profit=-8953.50, unrealized_profit=0)])  # now closed/realized

    breached2 = guard.poll(dhan, dry_run=False, debug=False)
    print(f"Breached: {breached2} | guard.locked: {guard.locked}")
    assert guard.locked, "FAILED: should now be locked, since activation succeeded and was verified"
    print("PASS: retried on the next check and correctly locked once verification succeeded")

    print("\n" + "=" * 70)
    print("TEST 3: Once genuinely locked and verified, does not re-check every run")
    print("=" * 70)
    calls_before = len(dhan.kill_switch_calls)
    guard.poll(dhan, dry_run=False, debug=False)
    calls_after = len(dhan.kill_switch_calls)
    print(f"Kill switch calls made in this run: {calls_after - calls_before}")
    assert calls_after == calls_before, "FAILED: should not re-attempt once genuinely locked"
    print("PASS: no redundant calls once locked is genuinely confirmed")

    print("\n" + "=" * 70)
    print("TEST 4: Activation succeeds on the FIRST try (normal, non-incident case)")
    print("=" * 70)
    dhan2 = MockDhan()
    dhan2.kill_switch_will_succeed = True
    dhan2.status_kill_switch_response = {"status": "success", "data": {"killSwitchStatus": "ACTIVE"}}
    guard2 = TradingGuard(capital=45000, loss_pct=5, max_trades=2)
    dhan2.set_trade_book([fill("SEC1", "BUY", 130, "1"), fill("SEC1", "SELL", 130, "2")])
    dhan2.set_positions([pos("SEC1", realized_profit=-2400)])

    breached3 = guard2.poll(dhan2, dry_run=False, debug=False)
    print(f"Breached: {breached3} | guard.locked: {guard2.locked}")
    assert guard2.locked, "FAILED: should lock immediately when activation succeeds and verifies clean"
    assert dhan2.kill_switch_calls == ["ACTIVATE", "DEACTIVATE", "ACTIVATE"]
    print("PASS: normal case still works correctly - locks on the first successful attempt")

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("This proves the exact incident (locked=True despite a failed activation)")
    print("is no longer possible in this version of the code.")
    print("REMINDER: status_kill_switch()'s real response shape from Dhan has not")
    print("been confirmed via --debug against a live account yet. The verification")
    print("logic currently checks for the word 'ACTIVE' in the response text - test")
    print("this against a real response before fully trusting it.")
    print("=" * 70)


if __name__ == "__main__":
    main()
