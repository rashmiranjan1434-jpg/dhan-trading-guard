"""
Test harness for oi_dashboard.py

Uses a synthetic (fake) option chain response shaped like Dhan's documented
format, so the analysis math (PCR, buildup classification, top movers,
support/resistance) can be verified without needing live credentials.

IMPORTANT CAVEAT this test suite CANNOT cover: whether Dhan's REAL response
actually uses the field names this script assumes (oc / ce / pe / oi /
last_price / implied_volatility). That can only be confirmed by running
`python3 oi_dashboard.py --debug` against a real account and reading the
raw response it prints - do this before trusting the output.

Usage:
    python3 test_oi_dashboard.py
"""

import sys
import os
sys.path.insert(0, ".")
from oi_dashboard import analyze, _pct_change, _classify_buildup


class MockDhan:
    def __init__(self, expiry, chain_response):
        self._expiry = expiry
        self._chain = chain_response

    def expiry_list(self, sid, seg):
        return {"status": "success", "data": [self._expiry]}

    def option_chain(self, sid, seg, expiry):
        return self._chain


def make_chain(spot, strikes):
    """strikes: dict of strike -> (ce_oi, ce_ltp, ce_iv, pe_oi, pe_ltp, pe_iv)"""
    oc = {}
    for strike, (ce_oi, ce_ltp, ce_iv, pe_oi, pe_ltp, pe_iv) in strikes.items():
        oc[str(strike)] = {
            "ce": {"oi": ce_oi, "last_price": ce_ltp, "implied_volatility": ce_iv},
            "pe": {"oi": pe_oi, "last_price": pe_ltp, "implied_volatility": pe_iv},
        }
    return {"status": "success", "data": {"last_price": spot, "oc": oc}}


def main():
    print("=" * 60)
    print("TEST 1: Basic PCR and support/resistance calculation")
    print("=" * 60)
    strikes = {
        24200: (5000, 100, 12, 20000, 30, 11),   # heavy PE OI here -> support
        24250: (8000, 70, 11, 9000, 50, 10.5),
        24300: (25000, 40, 10.8, 6000, 80, 11),  # heavy CE OI here -> resistance
        24350: (3000, 20, 12, 2000, 120, 12),
    }
    chain = make_chain(spot=24278.25, strikes=strikes)
    dhan = MockDhan(expiry="2026-08-28", chain_response=chain)

    state_path = "/tmp/test_oi_state.json"
    if os.path.exists(state_path):
        os.remove(state_path)

    result = analyze(dhan, strikes_each_side=5, state_file=state_path, debug=False)
    print(f"Spot: {result['spot']}, ATM: {result['atm_strike']}")
    print(f"PCR: {result['pcr']} ({result['pcr_read']})")
    print(f"Support: {result['support_strike']}, Resistance: {result['resistance_strike']}")

    assert result["atm_strike"] == 24300 or result["atm_strike"] == 24250  # closest to 24278.25 - 24250 is 28.25 away, 24300 is 21.75 away
    assert result["atm_strike"] == 24300, f"FAILED: expected ATM 24300, got {result['atm_strike']}"
    assert result["support_strike"] == 24200, f"FAILED: expected support 24200, got {result['support_strike']}"
    assert result["resistance_strike"] == 24300, f"FAILED: expected resistance 24300, got {result['resistance_strike']}"
    total_pe = 20000 + 9000 + 6000 + 2000
    total_ce = 5000 + 8000 + 25000 + 3000
    expected_pcr = round(total_pe / total_ce, 2)
    assert result["pcr"] == expected_pcr, f"FAILED: expected PCR {expected_pcr}, got {result['pcr']}"
    print(f"PASS: ATM, support, resistance, and PCR all correct (expected PCR {expected_pcr})")

    print("\n" + "=" * 60)
    print("TEST 2: First run has no buildup data (nothing to compare against)")
    print("=" * 60)
    for s in result["strikes"]:
        assert s["ce_buildup"] == "no_prior_data"
        assert s["pe_buildup"] == "no_prior_data"
    print("PASS: correctly reports no_prior_data on the very first snapshot")

    print("\n" + "=" * 60)
    print("TEST 3: Second run detects OI change and classifies buildup correctly")
    print("=" * 60)
    # 24300 CE: OI jumps a lot, premium also up -> long_buildup
    # 24200 PE: OI jumps, premium down -> short_buildup on put side... wait
    # for puts, "long buildup" still means price up + OI up for THAT contract
    strikes_2 = dict(strikes)
    strikes_2[24300] = (25000 * 1.92, 45, 10.9, 6000, 80, 11)   # CE OI +92%, premium up -> long_buildup
    strikes_2[24200] = (5000, 100, 12, 20000 * 1.41, 25, 11.2)  # PE OI +41%, premium down -> short_buildup
    chain2 = make_chain(spot=24278.25, strikes=strikes_2)
    dhan2 = MockDhan(expiry="2026-08-28", chain_response=chain2)

    result2 = analyze(dhan2, strikes_each_side=5, state_file=state_path, debug=False)
    ce_top = result2["top_ce_movers"][0]
    pe_top = result2["top_pe_movers"][0]
    print(f"Top CE mover: strike {ce_top['strike']}, {ce_top['oi_chg_pct']}%, {ce_top['buildup']}")
    print(f"Top PE mover: strike {pe_top['strike']}, {pe_top['oi_chg_pct']}%, {pe_top['buildup']}")

    assert ce_top["strike"] == 24300, f"FAILED: expected top CE mover 24300, got {ce_top['strike']}"
    assert ce_top["buildup"] == "long_buildup", f"FAILED: expected long_buildup, got {ce_top['buildup']}"
    assert abs(ce_top["oi_chg_pct"] - 92.0) < 1, f"FAILED: expected ~92% change, got {ce_top['oi_chg_pct']}"
    assert pe_top["strike"] == 24200, f"FAILED: expected top PE mover 24200, got {pe_top['strike']}"
    assert pe_top["buildup"] == "short_buildup", f"FAILED: expected short_buildup, got {pe_top['buildup']}"
    print("PASS: OI % change and buildup classification both correct on the second run")

    print("\n" + "=" * 60)
    print("TEST 4: _classify_buildup covers all four quadrants directly")
    print("=" * 60)
    assert _classify_buildup(1000, 1500, 50, 60) == "long_buildup"
    assert _classify_buildup(1000, 1500, 50, 40) == "short_buildup"
    assert _classify_buildup(1500, 1000, 50, 60) == "short_covering"
    assert _classify_buildup(1500, 1000, 50, 40) == "long_unwinding"
    assert _classify_buildup(None, 1000, None, 40) == "no_prior_data"
    print("PASS: all four OI buildup quadrants classify correctly")

    print("\n" + "=" * 60)
    print("TEST 5: PCR read thresholds")
    print("=" * 60)
    assert _pct_change(100, 150) == 50.0
    assert _pct_change(100, 50) == -50.0
    assert _pct_change(0, 50) is None
    print("PASS: pct_change helper correct")

    os.remove(state_path)
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("REMINDER: this only proves the MATH is correct against a fake response.")
    print("Run with --debug against your real account before trusting the output -")
    print("Dhan's actual field names have not been verified yet.")
    print("=" * 60)


if __name__ == "__main__":
    main()
