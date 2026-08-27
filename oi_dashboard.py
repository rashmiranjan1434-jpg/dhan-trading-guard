"""
Nifty OI Dashboard
===================
Fetches Nifty's live option chain from Dhan every few minutes, computes
standard options-market-structure metrics (PCR, OI buildup, top OI movers,
support/resistance from OI, IV read), and writes the result to a JSON file
this repo also serves as a simple public dashboard page.

This tool DESCRIBES market structure. It does not generate buy/sell signals
and never will - the trade decision stays entirely with the person reading
the dashboard. See DASHBOARD.md / index.html for the display side.

Definitions used (standard options terminology, not invented here):
  PCR = Total Put OI / Total Call OI across the chain
    < ~0.7  -> call-heavy positioning -> bearish/neutral lean
    ~0.7-1.3 -> roughly neutral
    > ~1.3  -> put-heavy positioning -> bullish lean

  OI buildup per strike (uses that OPTION CONTRACT's own premium + OI,
  not the underlying index):
    premium up,   OI up   -> Long Buildup    (fresh bullish positions)
    premium down, OI up   -> Short Buildup   (fresh bearish positions)
    premium up,   OI down -> Short Covering  (bears exiting)
    premium down, OI down -> Long Unwinding  (bulls exiting)
  Buildup requires a PREVIOUS snapshot to compare against - the very first
  run of the day has no buildup data yet, only current OI levels.

  Top movers ("OI1"/"OI2"): the two strikes (call side and put side,
  independently) with the largest % OI change since the last snapshot.

  Support = highest ABSOLUTE OI on the put side.
  Resistance = highest ABSOLUTE OI on the call side.

Setup:
  pip install dhanhq
  export DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (same as the trading guard)

Run:
  python3 oi_dashboard.py --strikes-each-side 5 --state-file oi_state.json --debug
"""

import os
import sys
import json
import argparse
import requests
from datetime import datetime

try:
    from dhanhq import dhanhq, DhanContext
except ImportError:
    print("Missing dependency. Run: pip install dhanhq")
    sys.exit(1)

NIFTY_SECURITY_ID = "13"
NIFTY_SEGMENT = "IDX_I"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def raw_debug_call(debug=False):
    """Bypasses the dhanhq SDK entirely and calls the expiry_list endpoint
    directly with requests, so we can see the REAL HTTP status code and raw
    response body - the SDK's wrapper can mask what actually came back."""
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    proxy_url = os.environ.get("STATICIP_PROXY_URL")
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    url = "https://api.dhan.co/v2/optionchain/expirylist"
    headers = {
        "access-token": access_token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"UnderlyingScrip": int(NIFTY_SECURITY_ID), "UnderlyingSeg": NIFTY_SEGMENT}
    resp = requests.post(url, headers=headers, json=body, proxies=proxies, timeout=20)
    log(f"RAW HTTP status: {resp.status_code}")
    log(f"RAW response headers: {dict(resp.headers)}")
    log(f"RAW response body (first 2000 chars): {resp.text[:2000]}")


def get_client():
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        log("ERROR: Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first.")
        sys.exit(1)
    dhan = dhanhq(DhanContext(client_id, access_token))
    proxy_url = os.environ.get("STATICIP_PROXY_URL")
    if proxy_url:
        dhan.dhan_http.session.proxies = {"https": proxy_url, "http": proxy_url}
        log("Routing requests through the static-IP proxy.")
    return dhan


def get_nearest_expiry(dhan, debug=False):
    resp = dhan.expiry_list(NIFTY_SECURITY_ID, NIFTY_SEGMENT)
    if debug:
        log(f"DEBUG expiry_list raw response: {resp}")
    expiries = resp.get("data", []) if isinstance(resp, dict) else []
    if not expiries:
        raise RuntimeError(f"No expiries returned: {resp}")
    return sorted(expiries)[0]


def fetch_chain(dhan, expiry, debug=False):
    resp = dhan.option_chain(NIFTY_SECURITY_ID, NIFTY_SEGMENT, expiry)
    if debug:
        log(f"DEBUG option_chain raw response (truncated): {str(resp)[:1500]}")
    return resp


def _extract_strike_rows(chain_resp):
    """Dhan's option_chain response shape can vary - this reads defensively
    and logs what it finds so field-name mismatches are visible via --debug,
    same lesson learned from the trading guard's positions/trade_book fields."""
    data = chain_resp.get("data", {}) if isinstance(chain_resp, dict) else {}
    last_price = data.get("last_price") or data.get("lastPrice")
    oc = data.get("oc", {}) or data.get("optionChain", {}) or {}
    rows = []
    for strike_str, entry in oc.items():
        try:
            strike = float(strike_str)
        except (TypeError, ValueError):
            continue
        ce = entry.get("ce", {}) or entry.get("CE", {}) or {}
        pe = entry.get("pe", {}) or entry.get("PE", {}) or {}
        rows.append({
            "strike": strike,
            "ce_oi": float(ce.get("oi", 0) or 0),
            "ce_ltp": float(ce.get("last_price", ce.get("lastPrice", 0)) or 0),
            "ce_iv": float(ce.get("implied_volatility", ce.get("iv", 0)) or 0),
            "pe_oi": float(pe.get("oi", 0) or 0),
            "pe_ltp": float(pe.get("last_price", pe.get("lastPrice", 0)) or 0),
            "pe_iv": float(pe.get("implied_volatility", pe.get("iv", 0)) or 0),
        })
    rows.sort(key=lambda r: r["strike"])
    return last_price, rows


def _classify_buildup(prev_oi, curr_oi, prev_ltp, curr_ltp):
    if prev_oi is None or prev_ltp is None:
        return "no_prior_data"
    oi_up = curr_oi > prev_oi
    price_up = curr_ltp > prev_ltp
    if price_up and oi_up:
        return "long_buildup"
    if not price_up and oi_up:
        return "short_buildup"
    if price_up and not oi_up:
        return "short_covering"
    if not price_up and not oi_up:
        return "long_unwinding"
    return "flat"


def _pct_change(prev, curr):
    if prev is None or prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def _build_narrative(spot, atm_strike, atm_row, pcr, pcr_read, support_strike,
                      resistance_strike, ce_movers, pe_movers):
    """Builds a plain-English summary from the computed numbers - templated,
    not a live AI call, since this runs unattended on a schedule. Mirrors
    the read-out style requested: a spot line, then a few bullet reads."""
    bullets = []

    bullets.append(
        f"ATM zone ({int(atm_strike)}): IV is {atm_row['ce_iv']:.1f}%-{atm_row['pe_iv']:.1f}% "
        f"on the call/put side."
    )

    bullets.append(f"PCR is {pcr} - {pcr_read}." if pcr is not None else "PCR unavailable this snapshot.")

    if support_strike is not None and resistance_strike is not None:
        bullets.append(
            f"OI structure: heaviest put OI sits at {int(support_strike)} (likely support), "
            f"heaviest call OI sits at {int(resistance_strike)} (likely resistance)."
        )

    for m in ce_movers:
        if m["ce_oi_chg_pct"] is not None:
            bullets.append(
                f"{int(m['strike'])} CE shows the biggest fresh OI move on the call side "
                f"({m['ce_oi_chg_pct']:+.0f}% change) - {m['ce_buildup'].replace('_', ' ')}."
            )
    for m in pe_movers:
        if m["pe_oi_chg_pct"] is not None:
            bullets.append(
                f"{int(m['strike'])} PE shows the biggest fresh OI move on the put side "
                f"({m['pe_oi_chg_pct']:+.0f}% change) - {m['pe_buildup'].replace('_', ' ')}."
            )

    return {
        "headline": f"NIFTY 50 is at {spot:,.2f}, ATM strike {int(atm_strike)}.",
        "bullets": bullets,
    }


def analyze(dhan, strikes_each_side, state_file, debug=False):
    expiry = get_nearest_expiry(dhan, debug)
    chain_resp = fetch_chain(dhan, expiry, debug)
    spot, rows = _extract_strike_rows(chain_resp)
    if spot is None or not rows:
        raise RuntimeError(f"Could not parse option chain response: {chain_resp}")

    # find ATM = strike closest to spot
    atm_row = min(rows, key=lambda r: abs(r["strike"] - spot))
    atm_strike = atm_row["strike"]
    atm_index = rows.index(atm_row)

    lo = max(0, atm_index - strikes_each_side)
    hi = min(len(rows), atm_index + strikes_each_side + 1)
    window = rows[lo:hi]

    total_ce_oi = sum(r["ce_oi"] for r in window)
    total_pe_oi = sum(r["pe_oi"] for r in window)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else None

    # load previous snapshot for change/buildup calc
    prev = {}
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                prev = json.load(f)
        except (json.JSONDecodeError, OSError):
            prev = {}
    prev_rows_by_strike = {r["strike"]: r for r in prev.get("rows", [])}

    strikes_out = []
    for r in window:
        p = prev_rows_by_strike.get(r["strike"])
        ce_oi_chg_pct = _pct_change(p["ce_oi"], r["ce_oi"]) if p else None
        pe_oi_chg_pct = _pct_change(p["pe_oi"], r["pe_oi"]) if p else None
        strikes_out.append({
            **r,
            "ce_oi_chg_pct": ce_oi_chg_pct,
            "pe_oi_chg_pct": pe_oi_chg_pct,
            "ce_buildup": _classify_buildup(p["ce_oi"], r["ce_oi"], p["ce_ltp"], r["ce_ltp"]) if p else "no_prior_data",
            "pe_buildup": _classify_buildup(p["pe_oi"], r["pe_oi"], p["pe_ltp"], r["pe_ltp"]) if p else "no_prior_data",
        })

    # top movers - rank by abs % OI change, top 2 each side
    ce_movers = sorted([s for s in strikes_out if s["ce_oi_chg_pct"] is not None],
                        key=lambda s: abs(s["ce_oi_chg_pct"]), reverse=True)[:2]
    pe_movers = sorted([s for s in strikes_out if s["pe_oi_chg_pct"] is not None],
                        key=lambda s: abs(s["pe_oi_chg_pct"]), reverse=True)[:2]

    # support/resistance = highest absolute OI on each side, across full window
    resistance_strike = max(window, key=lambda r: r["ce_oi"])["strike"] if window else None
    support_strike = max(window, key=lambda r: r["pe_oi"])["strike"] if window else None

    if pcr is None:
        pcr_read = "unknown"
    elif pcr < 0.7:
        pcr_read = "call-heavy - bearish/neutral lean"
    elif pcr > 1.3:
        pcr_read = "put-heavy - bullish lean"
    else:
        pcr_read = "roughly neutral"

    narrative = _build_narrative(spot, atm_strike, atm_row, pcr, pcr_read,
                                  support_strike, resistance_strike, ce_movers, pe_movers)

    result = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "expiry": expiry,
        "spot": spot,
        "atm_strike": atm_strike,
        "atm_ce_iv": atm_row["ce_iv"],
        "atm_pe_iv": atm_row["pe_iv"],
        "pcr": pcr,
        "pcr_read": pcr_read,
        "support_strike": support_strike,
        "resistance_strike": resistance_strike,
        "top_ce_movers": [{"strike": s["strike"], "oi_chg_pct": s["ce_oi_chg_pct"], "buildup": s["ce_buildup"]} for s in ce_movers],
        "top_pe_movers": [{"strike": s["strike"], "oi_chg_pct": s["pe_oi_chg_pct"], "buildup": s["pe_buildup"]} for s in pe_movers],
        "strikes": strikes_out,
        "narrative": narrative,
    }

    # persist this snapshot (bare rows only) for next run's comparison
    if state_file:
        with open(state_file, "w") as f:
            json.dump({"timestamp": result["timestamp"], "rows": window}, f)

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strikes-each-side", type=int, default=5)
    parser.add_argument("--state-file", type=str, default="oi_state.json",
                         help="Stores the previous snapshot for change/buildup calc - separate from the output file.")
    parser.add_argument("--output-file", type=str, default="oi_data.json",
                         help="Where the full result is written - this is what the dashboard page reads.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--raw-debug", action="store_true",
                         help="Bypass the SDK, call the endpoint directly, show real HTTP status and body.")
    args = parser.parse_args()

    if args.raw_debug:
        raw_debug_call(args.debug)
        return

    dhan = get_client()
    try:
        result = analyze(dhan, args.strikes_each_side, args.state_file, args.debug)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    with open(args.output_file, "w") as f:
        json.dump(result, f, indent=2)

    log(f"Spot {result['spot']} | ATM {result['atm_strike']} | PCR {result['pcr']} ({result['pcr_read']}) "
        f"| Support {result['support_strike']} | Resistance {result['resistance_strike']}")
    for m in result["top_ce_movers"]:
        log(f"  CE mover: {m['strike']} {m['oi_chg_pct']}% ({m['buildup']})")
    for m in result["top_pe_movers"]:
        log(f"  PE mover: {m['strike']} {m['oi_chg_pct']}% ({m['buildup']})")


if __name__ == "__main__":
    main()
