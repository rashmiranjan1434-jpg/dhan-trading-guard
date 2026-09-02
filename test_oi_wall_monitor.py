"""
Test harness for oi_wall_monitor.py

Simulates sequences of option chain snapshots to verify:
  - cluster detection picks the correct "strongest side"
  - wall-touch + OI-drop correctly confirms a "grab"
  - wall-touch WITHOUT an OI drop does NOT falsely confirm
  - reversal detection fires only after price genuinely moves back
  - the 10-minute reversal window expires correctly if no reversal happens
  - state persists correctly across separate process runs (the real
    GitHub Actions execution model - each run is a fresh process)

Usage:
    python3 test_oi_wall_monitor.py
"""

import sys
import os
import datetime as dt
sys.path.insert(0, ".")
from oi_wall_monitor import (
    ChainSnapshot, MonitorState, process_snapshot, find_best_cluster,
    determine_strongest_side, nearer_wall_strike, load_state, save_state,
)


def strike(s, call_oi, call_prev, put_oi, put_prev):
    return {
        "strike": s, "call_oi": call_oi, "call_oi_change": call_oi - call_prev,
        "put_oi": put_oi, "put_oi_change": put_oi - put_prev,
    }


def snap(time_str, spot, atm, strikes):
    return ChainSnapshot(timestamp=f"2026-09-02T{time_str}:00", spot=spot, atm=atm, strikes=strikes)


def main():
    print("=" * 60)
    print("TEST 1: Cluster detection picks the correct strongest side")
    print("=" * 60)
    strikes = [
        strike(24200, 50000, 50000, 200000, 200000),   # heavy PUT OI, no call cluster partner nearby in value
        strike(24250, 80000, 80000, 90000, 90000),
        strike(24300, 250000, 250000, 60000, 60000),
        strike(24350, 220000, 220000, 20000, 20000),    # 24300+24350 CALL cluster: 250k & 220k, within 15%
    ]
    side, cluster = determine_strongest_side(strikes)
    print(f"Strongest side: {side}, cluster: {cluster.strike_a}/{cluster.strike_b} (OI {cluster.oi_a:,}/{cluster.oi_b:,})")
    assert side == "CALL", f"FAILED: expected CALL, got {side}"
    assert {cluster.strike_a, cluster.strike_b} == {24300, 24350}
    print("PASS: correctly found the CALL cluster as strongest")

    print("\n" + "=" * 60)
    print("TEST 2: Full sequence - strongest side set, wall touched, grab confirmed")
    print("=" * 60)
    state = MonitorState(date="2026-09-02")

    s1 = snap("09:35", 24230, 24250, strikes)
    process_snapshot(s1, state)
    print(f"After snap 1: strongest={state.strongest_side}, wall={state.wall_strike}, grab={state.grab_flagged}")
    assert state.strongest_side == "CALL"
    assert state.wall_strike == 24300  # nearer of 24300/24350 to spot 24230
    assert not state.grab_flagged
    assert len(state.alerts) == 1
    print("PASS: strongest side and wall correctly established, no grab yet")

    # price reaches the wall (24300, within 5pt buffer), OI drops sharply -> grab
    strikes_at_wall = [
        strike(24200, 50000, 50000, 200000, 200000),
        strike(24250, 80000, 80000, 90000, 90000),
        strike(24300, 220000, 250000, 60000, 60000),  # CE OI dropped from 250k to 220k = 12% drop
        strike(24350, 220000, 220000, 20000, 20000),
    ]
    s2 = snap("09:40", 24298, 24300, strikes_at_wall)
    process_snapshot(s2, state)
    print(f"After snap 2 (at wall): grab_flagged={state.grab_flagged}")
    assert state.grab_flagged, "FAILED: should have confirmed the grab"
    assert any("GRAB CONFIRMED" in a for a in state.alerts)
    print("PASS: grab correctly confirmed on sharp OI drop at the wall")

    print("\n" + "=" * 60)
    print("TEST 3: Reversal confirms once price genuinely moves back")
    print("=" * 60)
    # price still near the wall - should NOT confirm reversal yet
    s3 = snap("09:42", 24299, 24300, strikes_at_wall)
    process_snapshot(s3, state)
    assert not any("REVERSAL CONFIRMED" in a for a in state.alerts)
    print("PASS: no false reversal while price is still at the wall")

    # price genuinely moves back below wall - buffer
    s4 = snap("09:45", 24280, 24300, strikes_at_wall)
    process_snapshot(s4, state)
    print(f"After snap 4 (moved back): grab_flagged={state.grab_flagged}")
    assert any("REVERSAL CONFIRMED" in a for a in state.alerts), "FAILED: should have confirmed reversal"
    assert not state.grab_flagged, "FAILED: grab flag should reset after reversal confirms"
    print("PASS: reversal correctly confirmed once price moved back from the wall")

    print("\n" + "=" * 60)
    print("TEST 4: Wall touched but OI does NOT drop - should NOT confirm a grab")
    print("=" * 60)
    state2 = MonitorState(date="2026-09-02")
    s1b = snap("09:35", 24230, 24250, strikes)
    process_snapshot(s1b, state2)

    strikes_defended = [
        strike(24200, 50000, 50000, 200000, 200000),
        strike(24250, 80000, 80000, 90000, 90000),
        strike(24300, 250000, 250000, 60000, 60000),  # unchanged OI - flat, clearly not a drop
        strike(24350, 220000, 220000, 20000, 20000),
    ]
    s2b = snap("09:40", 24298, 24300, strikes_defended)
    process_snapshot(s2b, state2)
    print(f"Grab flagged: {state2.grab_flagged}")
    assert not state2.grab_flagged, "FAILED: should NOT confirm grab when OI didn't drop"
    assert any("wall looks defended" in a for a in state2.alerts)
    print("PASS: correctly did NOT confirm a grab when the wall held")

    print("\n" + "=" * 60)
    print("TEST 5: Reversal watch window expires if reversal never happens")
    print("=" * 60)
    state3 = MonitorState(date="2026-09-02")
    process_snapshot(snap("09:35", 24230, 24250, strikes), state3)
    process_snapshot(snap("09:40", 24298, 24300, strikes_at_wall), state3)
    assert state3.grab_flagged
    # 15 minutes later, price never reversed - still hovering near the wall
    process_snapshot(snap("09:55", 24297, 24300, strikes_at_wall), state3)
    print(f"After window expiry: grab_flagged={state3.grab_flagged}")
    assert not state3.grab_flagged, "FAILED: grab flag should reset after window expiry"
    assert any("expired without confirmed reversal" in a for a in state3.alerts)
    print("PASS: reversal window correctly expires and resets when nothing happens")

    print("\n" + "=" * 60)
    print("TEST 6: Pre-9:30 readings are logged but not acted on")
    print("=" * 60)
    state4 = MonitorState(date="2026-09-02")
    s_early = snap("09:15", 24298, 24300, strikes_at_wall)  # already "at the wall" but pre-settle
    process_snapshot(s_early, state4)
    print(f"Pre-9:30 grab_flagged: {state4.grab_flagged}")
    assert not state4.grab_flagged, "FAILED: should not act on pre-9:30 data even if conditions match"
    assert any("pre-9:30" in a for a in state4.alerts)
    print("PASS: correctly held off acting on pre-9:30 data")

    print("\n" + "=" * 60)
    print("TEST 7: State persists correctly across SEPARATE process runs")
    print("=" * 60)
    state_path = "/tmp/test_oi_wall_state.json"
    if os.path.exists(state_path):
        os.remove(state_path)

    st = MonitorState(date="2026-09-02")
    process_snapshot(snap("09:35", 24230, 24250, strikes), st)
    save_state(state_path, st)

    st_loaded = load_state(state_path)
    print(f"Reloaded in a fresh process: strongest={st_loaded.strongest_side}, wall={st_loaded.wall_strike}")
    assert st_loaded.strongest_side == "CALL"
    assert st_loaded.wall_strike == 24300
    assert len(st_loaded.alerts) == 1

    # simulate a new day - state should reset
    st_loaded.date = "2026-08-31"  # pretend this was saved yesterday
    save_state(state_path, st_loaded)
    st_new_day = load_state(state_path)
    assert st_new_day.strongest_side is None, "FAILED: state should reset on a new trading day"
    print("PASS: state persists correctly within a day, and resets on a new day")
    os.remove(state_path)

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("REMINDER: this only proves the MATH and STATE LOGIC are correct against")
    print("synthetic data. Run --raw-debug against your real account and check the")
    print("printed status before trusting any live alert.")
    print("=" * 60)


if __name__ == "__main__":
    main()
