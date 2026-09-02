"""
OI Wall Liquidity-Grab & Reversal Monitor - NIFTY
===================================================

Strategy detected (as defined by the user):
  1. Take ATM strike, look at 5 strikes above (calls) and 5 strikes below (puts).
  2. On each side, find the 2 heaviest OI strikes whose OI values are close to
     each other (a genuine "cluster") - that side is the STRONGEST side.
  3. Expectation: price moves toward the strongest side FIRST (liquidity grab),
     then reverses toward the opposite side.
  4. When price actually reaches the wall strike, check Change-in-OI at that
     moment as confirmation:
        - Sharp OI DROP on that strike  -> writers unwinding/stopped out
                                            -> grab confirmed, reversal likely
        - OI flat/rising on that strike -> wall being defended
  5. Pre-9:30 readings are logged but not acted on.
  6. DECISION SUPPORT ONLY. Never places trades.

REWRITTEN from the original design to fit the proven, tested infrastructure
already running for the trading guard and OI dashboard:
  - Fetches via raw HTTP + the static-IP proxy (same as oi_dashboard.py) -
    NOT the dhanhq SDK, which was confirmed this week to mangle this
    endpoint's response into a fake failure.
  - Runs as a SINGLE check-and-exit (not an infinite loop) - GitHub Actions
    containers are ephemeral, and cron-job.org already reliably triggers a
    fresh run every minute. All state that needs to persist between polls
    (strongest side, wall, grab flag, 10-minute reversal window) is saved
    to a JSON file and reloaded on the next run, same pattern as the
    trading guard's lock state.
  - Practical interval: 1 minute, not 30 seconds - GitHub Actions' own
    startup overhead (~10-15s per run) plus cron-job.org's free-tier
    granularity make 30s unrealistic to promise honestly.

Requires: pip install requests
"""

import os
import sys
import json
import argparse
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

NIFTY_SECURITY_ID = "13"
NIFTY_SEGMENT = "IDX_I"

STRIKES_EACH_SIDE = 5
CLUSTER_CLOSENESS_PCT = 15.0
WALL_TOUCH_BUFFER_POINTS = 5
OI_DROP_CONFIRM_PCT = 8.0
SETTLE_TIME = dt.time(9, 30)
REVERSAL_WATCH_MINUTES = 10


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def today_str():
    return dt.datetime.now().strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# FETCH - raw HTTP + proxy, same proven pattern as oi_dashboard.py
# ----------------------------------------------------------------------------

def _raw_headers():
    return {
        "access-token": os.environ.get("DHAN_ACCESS_TOKEN"),
        "client-id": os.environ.get("DHAN_CLIENT_ID"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _raw_proxies():
    proxy_url = os.environ.get("STATICIP_PROXY_URL")
    return {"https": proxy_url, "http": proxy_url} if proxy_url else None


def get_nearest_expiry(debug=False):
    url = "https://api.dhan.co/v2/optionchain/expirylist"
    body = {"UnderlyingScrip": int(NIFTY_SECURITY_ID), "UnderlyingSeg": NIFTY_SEGMENT}
    resp = requests.post(url, headers=_raw_headers(), json=body, proxies=_raw_proxies(), timeout=20)
    data = resp.json()
    if debug:
        log(f"DEBUG expiry_list raw response: {data}")
    expiries = data.get("data", []) if isinstance(data, dict) else []
    if not expiries:
        raise RuntimeError(f"No expiries returned: {data}")
    return sorted(expiries)[0]


def fetch_option_chain(expiry, debug=False):
    url = "https://api.dhan.co/v2/optionchain"
    body = {"UnderlyingScrip": int(NIFTY_SECURITY_ID), "UnderlyingSeg": NIFTY_SEGMENT, "Expiry": expiry}
    resp = requests.post(url, headers=_raw_headers(), json=body, proxies=_raw_proxies(), timeout=20)
    data = resp.json()
    if debug:
        log(f"DEBUG option_chain raw response (truncated): {str(data)[:1500]}")
    return data


def raw_debug_call(debug=False):
    """Bypasses everything else, shows the real HTTP status/body directly -
    use this once against a real account before trusting any alert."""
    expiry = get_nearest_expiry(debug=True)
    log(f"Nearest expiry: {expiry}")
    chain = fetch_option_chain(expiry, debug=True)
    log(f"Chain fetch status field: {chain.get('status') if isinstance(chain, dict) else 'N/A'}")


# ----------------------------------------------------------------------------
# DATA STRUCTURES
# ----------------------------------------------------------------------------

@dataclass
class StrikeData:
    strike: float
    call_oi: int
    call_oi_change: int
    put_oi: int
    put_oi_change: int


@dataclass
class ChainSnapshot:
    timestamp: str  # isoformat string, not datetime, for clean JSON round-trip
    spot: float
    atm: float
    strikes: list  # list[dict] shaped like StrikeData


@dataclass
class Cluster:
    side: str
    strike_a: float
    strike_b: float
    oi_a: int
    oi_b: int


@dataclass
class MonitorState:
    date: str = field(default_factory=today_str)
    strongest_side: Optional[str] = None
    wall_strike: Optional[float] = None
    grab_flagged: bool = False
    reversal_watch_until: Optional[str] = None  # isoformat string
    alerts: list = field(default_factory=list)  # rolling log for today, shown on the dashboard


def load_state(path) -> MonitorState:
    if not path or not os.path.exists(path):
        return MonitorState()
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return MonitorState()
    if data.get("date") != today_str():
        return MonitorState()  # new trading day - start fresh
    return MonitorState(**data)


def save_state(path, state: MonitorState):
    if not path:
        return
    with open(path, "w") as f:
        json.dump(asdict(state), f, indent=2)


def add_alert(state: MonitorState, message: str):
    stamped = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}"
    log(message)
    state.alerts.append(stamped)
    state.alerts = state.alerts[-100:]  # keep the log from growing unbounded across a long day


# ----------------------------------------------------------------------------
# PARSE
# ----------------------------------------------------------------------------

def parse_snapshot(raw: dict) -> ChainSnapshot:
    data = raw.get("data", {}) if isinstance(raw, dict) else {}
    spot = float(data.get("last_price", 0))
    oc = data.get("oc", {})
    if not oc or not spot:
        raise RuntimeError(f"Could not parse option chain response: {raw}")

    all_strikes = sorted(float(k) for k in oc.keys())
    strike_gap = min(b - a for a, b in zip(all_strikes, all_strikes[1:])) if len(all_strikes) > 1 else 50
    atm = round(spot / strike_gap) * strike_gap

    window = [s for s in all_strikes if abs(s - atm) <= strike_gap * STRIKES_EACH_SIDE]

    strikes = []
    for s in window:
        entry = oc.get(f"{s:.6f}", oc.get(str(s), {}))
        ce = entry.get("ce", {})
        pe = entry.get("pe", {})
        strikes.append(asdict(StrikeData(
            strike=s,
            call_oi=int(ce.get("oi", 0)),
            call_oi_change=int(ce.get("oi", 0)) - int(ce.get("previous_oi", ce.get("oi", 0))),
            put_oi=int(pe.get("oi", 0)),
            put_oi_change=int(pe.get("oi", 0)) - int(pe.get("previous_oi", pe.get("oi", 0))),
        )))

    return ChainSnapshot(timestamp=dt.datetime.now().isoformat(timespec="seconds"), spot=spot, atm=atm, strikes=strikes)


# ----------------------------------------------------------------------------
# CLUSTER LOGIC
# ----------------------------------------------------------------------------

def find_best_cluster(strikes: list, side: str) -> Optional[Cluster]:
    field_name = "call_oi" if side == "CALL" else "put_oi"
    ranked = sorted(strikes, key=lambda s: s[field_name], reverse=True)
    ranked = [s for s in ranked if s[field_name] > 0]

    best = None
    for i in range(len(ranked)):
        for j in range(i + 1, len(ranked)):
            oi_a, oi_b = ranked[i][field_name], ranked[j][field_name]
            bigger, smaller = max(oi_a, oi_b), min(oi_a, oi_b)
            if bigger == 0:
                continue
            closeness_pct = (1 - smaller / bigger) * 100
            if closeness_pct <= CLUSTER_CLOSENESS_PCT:
                combined = oi_a + oi_b
                if best is None or combined > (best.oi_a + best.oi_b):
                    best = Cluster(side=side, strike_a=ranked[i]["strike"], strike_b=ranked[j]["strike"],
                                   oi_a=oi_a, oi_b=oi_b)
        if i >= 4:
            break
    return best


def determine_strongest_side(strikes: list):
    call_cluster = find_best_cluster(strikes, "CALL")
    put_cluster = find_best_cluster(strikes, "PUT")
    if call_cluster is None and put_cluster is None:
        return None, None
    if call_cluster is None:
        return "PUT", put_cluster
    if put_cluster is None:
        return "CALL", call_cluster
    call_strength = call_cluster.oi_a + call_cluster.oi_b
    put_strength = put_cluster.oi_a + put_cluster.oi_b
    return ("CALL", call_cluster) if call_strength >= put_strength else ("PUT", put_cluster)


def nearer_wall_strike(cluster: Cluster, spot: float) -> float:
    return min([cluster.strike_a, cluster.strike_b], key=lambda s: abs(s - spot))


def get_strike_data(strikes: list, strike: float) -> Optional[dict]:
    for s in strikes:
        if s["strike"] == strike:
            return s
    return None


# ----------------------------------------------------------------------------
# MAIN PROCESSING - one snapshot in, state updated, alerts recorded
# ----------------------------------------------------------------------------

def process_snapshot(snapshot: ChainSnapshot, state: MonitorState):
    snap_time = dt.datetime.fromisoformat(snapshot.timestamp)
    is_settled = snap_time.time() >= SETTLE_TIME

    side, cluster = determine_strongest_side(snapshot.strikes)
    if cluster is None:
        return

    wall = nearer_wall_strike(cluster, snapshot.spot)

    if state.strongest_side != side or state.wall_strike != wall:
        state.strongest_side = side
        state.wall_strike = wall
        state.grab_flagged = False
        state.reversal_watch_until = None
        tag = "" if is_settled else " (pre-9:30, not yet reliable)"
        add_alert(state,
            f"STRONGEST SIDE: {side} - cluster at {cluster.strike_a} (OI {cluster.oi_a:,}) "
            f"& {cluster.strike_b} (OI {cluster.oi_b:,}). Watching wall at {wall}.{tag}")

    if not is_settled:
        return

    wall_data = get_strike_data(snapshot.strikes, wall)
    if wall_data is None:
        return

    distance = abs(snapshot.spot - wall)

    if distance <= WALL_TOUCH_BUFFER_POINTS and not state.grab_flagged:
        oi_change = wall_data["call_oi_change"] if side == "CALL" else wall_data["put_oi_change"]
        current_oi = wall_data["call_oi"] if side == "CALL" else wall_data["put_oi"]
        prior_oi = current_oi - oi_change
        drop_pct = (-oi_change / prior_oi * 100) if prior_oi else 0

        if drop_pct >= OI_DROP_CONFIRM_PCT:
            state.grab_flagged = True
            state.reversal_watch_until = (snap_time + dt.timedelta(minutes=REVERSAL_WATCH_MINUTES)).isoformat(timespec="seconds")
            add_alert(state,
                f"*** GRAB CONFIRMED *** Price {snapshot.spot} reached {side} wall {wall}. "
                f"OI dropped {drop_pct:.1f}% ({prior_oi:,} -> {current_oi:,}) - writers unwinding. "
                f"Watching for reversal over next {REVERSAL_WATCH_MINUTES} min.")
        else:
            add_alert(state,
                f"Price {snapshot.spot} reached {side} wall {wall}, but OI change is only "
                f"{drop_pct:.1f}% - wall looks defended, no signal yet.")

    if state.grab_flagged and state.reversal_watch_until:
        watch_until = dt.datetime.fromisoformat(state.reversal_watch_until)
        if snap_time > watch_until:
            add_alert(state, "Reversal watch window expired without confirmed reversal. Resetting.")
            state.grab_flagged = False
            state.reversal_watch_until = None
            return

        opposite_side = "PUT" if side == "CALL" else "CALL"
        opp_cluster = find_best_cluster(snapshot.strikes, opposite_side)
        moved_back = (
            (side == "CALL" and snapshot.spot < wall - WALL_TOUCH_BUFFER_POINTS)
            or (side == "PUT" and snapshot.spot > wall + WALL_TOUCH_BUFFER_POINTS)
        )
        if moved_back:
            if opp_cluster:
                opp_wall = nearer_wall_strike(opp_cluster, snapshot.spot)
                target_note = f"heading toward {opposite_side} wall {opp_wall}"
            else:
                target_note = f"no clean {opposite_side} cluster forming yet - watch price action directly"
            add_alert(state,
                f"*** REVERSAL CONFIRMED *** Price moved back from {side} wall {wall} to "
                f"{snapshot.spot}, {target_note}. Setup complete - your call on entry.")
            state.grab_flagged = False
            state.reversal_watch_until = None


def run_once(state_file, output_file, debug=False):
    expiry = get_nearest_expiry(debug)
    raw = fetch_option_chain(expiry, debug)
    snapshot = parse_snapshot(raw)

    state = load_state(state_file)
    process_snapshot(snapshot, state)
    save_state(state_file, state)

    if output_file:
        with open(output_file, "w") as f:
            json.dump({
                "snapshot": asdict(snapshot),
                "state": asdict(state),
            }, f, indent=2)

    log(f"Spot {snapshot.spot} | Strongest {state.strongest_side} | Wall {state.wall_strike} | "
        f"Grab flagged: {state.grab_flagged}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=str, default="oi_wall_state.json")
    parser.add_argument("--output-file", type=str, default="oi_wall_data.json",
                         help="Where the full result is written - for the dashboard page.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--raw-debug", action="store_true")
    args = parser.parse_args()

    if args.raw_debug:
        raw_debug_call(args.debug)
        return

    try:
        run_once(args.state_file, args.output_file, args.debug)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
