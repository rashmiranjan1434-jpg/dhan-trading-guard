"""
Test harness for oi_dashboard.py

Uses a synthetic (fake) HTTP response shaped like Dhan's documented API
format, so the analysis math (PCR, buildup classification, top movers,
support/resistance) can be verified without needing live credentials.

Mocks requests.post directly (not the dhanhq SDK) because the real fix
found via live testing bypasses the SDK entirely for these two endpoints -
the SDK was confirmed (via --raw-debug against the real account) to mangle
a perfectly valid {"data": [...], "status": "success"} response into a
generic failure shape. Raw HTTP calls work correctly; this test suite
exercises that same raw-HTTP code path.

IMPORTANT CAVEAT this test suite CANNOT cover: whether Dhan's REAL option
chain response actually uses the field names this script assumes (oc / ce
/ pe / oi / last_price / implied_volatility). The expiry_list raw shape
IS confirmed correct via --raw-debug; the option_chain raw shape is not
yet confirmed the same way - run --debug against a real account once
market data is flowing and check the printed raw response.

Usage:
    python3 test_oi_dashboard.py
"""

import sys
import os
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")
from oi_dashboard import analyze, _pct_change, _classify_buildup


def make_chain_json(spot, strikes):
    """strikes: dict of strike -> (ce_oi, ce_ltp, ce_iv, pe_oi, pe_ltp, pe_iv)"""
    oc = {}
    for strike, (ce_oi, ce_ltp, ce_iv, pe_oi, pe_ltp, pe_iv) in strikes.items():
        oc[str(strike)] = {
            "ce": {"oi": ce_oi, "last_price": ce_ltp, "implied_volatility": ce_iv},
            "pe": {"oi": pe_oi, "last_price": pe_ltp, "implied_volatility": pe_iv},
        }
    return {"status": "success", "data": {"last_price": spot, "oc": oc}}


def mock_requests_post(expiry, chain_json):
    """Returns a function to use as requests.post's side_effect - inspects
    the URL to decide whether to return an expiry-list or option-chain
    shaped response, same as the real Dhan API does for these two paths."""
    def _side_effect(url, headers=None, json=None, proxies=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if "expirylist" in url:
            resp.json.return_value = {"status": "success", "data": [expiry]}
        elif "optionchain" in url:
            resp.json.return_value = chain_json
        else:
            resp.json.return_value = {"status": "failure", "data": ""}
        return resp
    return _side_effect


def main():
    os.environ.setdefault("DHAN_CLIENT_ID", "test")
    os.environ.setdefault("DHAN_ACCESS_TOKEN", "test")

    print("=" * 60)
    print("TEST 1: Basic PCR and support/resistance calculation")
    print("=" * 60)
    strikes = {
        24200: (5000, 100, 12, 20000, 30, 11),   # heavy PE OI here -> support
        24250: (8000, 70, 11, 9000, 50, 10.5),
        24300: (25000, 40, 10.8, 6000, 80, 11),  # heavy CE OI here -> resistance
        24350: (3000, 20, 12, 2000, 120, 12),
    }
    chain = make_chain_json(spot=24278.25, strikes=strikes)
    state_path = "/tmp/test_oi_state.json"
    if os.path.exists(state_path):
        os.remove(state_path)

    with patch("oi_dashboard.requests.post", side_effect=mock_requests_post("2026-08-28", chain)):
        result = analyze(None, strikes_each_side=5, state_file=state_path, debug=False)

    print(f"Spot: {result['spot']}, ATM: {result['atm_strike']}")
    print(f"PCR: {result['pcr']} ({result['pcr_read']})")
    print(f"Support: {result['support_strike']}, Resistance: {result['resistance_strike']}")

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
    strikes_2 = dict(strikes)
    strikes_2[24300] = (25000 * 1.92, 45, 10.9, 6000, 80, 11)   # CE OI +92%, premium up -> long_buildup
    strikes_2[24200] = (5000, 100, 12, 20000 * 1.41, 25, 11.2)  # PE OI +41%, premium down -> short_buildup
    chain2 = make_chain_json(spot=24278.25, strikes=strikes_2)

    with patch("oi_dashboard.requests.post", side_effect=mock_requests_post("2026-08-28", chain2)):
        result2 = analyze(None, strikes_each_side=5, state_file=state_path, debug=False)

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
    print("TEST 5: PCR read thresholds / pct_change helper")
    print("=" * 60)
    assert _pct_change(100, 150) == 50.0
    assert _pct_change(100, 50) == -50.0
    assert _pct_change(0, 50) is None
    print("PASS: pct_change helper correct")

    print("\n" + "=" * 60)
    print("TEST 6: Narrative text generated correctly")
    print("=" * 60)
    assert "NIFTY 50 is at" in result2["narrative"]["headline"]
    assert len(result2["narrative"]["bullets"]) >= 3
    print(result2["narrative"]["headline"])
    for b in result2["narrative"]["bullets"]:
        print(" -", b)
    print("PASS: narrative headline and bullets generated")

    print("\n" + "=" * 60)
    print("TEST 7: History log accumulates across multiple runs, same day")
    print("=" * 60)
    hist_path = "/tmp/test_oi_history.json"
    if os.path.exists(hist_path):
        os.remove(hist_path)
    hist_state = "/tmp/test_oi_history_state.json"
    if os.path.exists(hist_state):
        os.remove(hist_state)

    with patch("oi_dashboard.requests.post", side_effect=mock_requests_post("2026-08-28", chain)):
        r1 = analyze(None, strikes_each_side=5, state_file=hist_state, debug=False, history_file=hist_path)
    with patch("oi_dashboard.requests.post", side_effect=mock_requests_post("2026-08-28", chain2)):
        r2 = analyze(None, strikes_each_side=5, state_file=hist_state, debug=False, history_file=hist_path)

    print(f"History length after 2 runs: {len(r2['history'])}")
    assert len(r2["history"]) == 2, f"FAILED: expected 2 history entries, got {len(r2['history'])}"
    assert r1["history"][0]["support_strike"] == 24200
    print("PASS: history accumulates one entry per run, same trading day")

    os.remove(hist_path)
    os.remove(hist_state)

    os.remove(state_path)
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("REMINDER: expiry_list raw shape is confirmed correct against the real")
    print("account. option_chain raw shape is still unverified - check --debug")
    print("output once real market data is flowing before trusting the numbers.")
    print("=" * 60)


if __name__ == "__main__":
    main()
