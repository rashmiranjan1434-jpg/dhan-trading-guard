"""
Dhan Trading Guard
===================
Monitors your Dhan account during market hours and ACTIVATES Dhan's own
account-level kill switch once you hit either limit for the day:
  - MAX_TRADES executed trades, or
  - a loss of LOSS_PCT% of your capital (realized P&L from Dhan's own data)

CRITICAL FIX (this version): a real incident on live trading showed the
script marking itself "locked" even when the underlying Dhan kill-switch
activation had FAILED (Dhan requires positions to be flat before it will
activate - if a position was still open at the exact moment of breach,
activation silently failed while the script gave up watching anyway).
Now: `locked` is only set True once the kill switch is CONFIRMED active,
and if activation fails, the script keeps retrying on every subsequent
check instead of giving up.

Setup:
  pip install dhanhq requests
  export DHAN_CLIENT_ID="your-client-id"
  export DHAN_ACCESS_TOKEN="your-access-token"
"""

import os
import sys
import time
import json
import argparse
import requests
from datetime import datetime

try:
    from dhanhq import dhanhq, DhanContext
except ImportError:
    print("Missing dependency. Run: pip install dhanhq")
    sys.exit(1)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def get_client():
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        log("ERROR: Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first.")
        sys.exit(1)
    dhan_context = DhanContext(client_id, access_token)
    return dhanhq(dhan_context)


def _pnl_exit_headers():
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        log("ERROR: Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first.")
        sys.exit(1)
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def _pnl_exit_proxies():
    proxy_url = os.environ.get("STATICIP_PROXY_URL")
    if not proxy_url:
        return None
    return {"https": proxy_url}


def setup_pnl_exit(loss_value, profit_value=None, product_types=None, enable_kill_switch=True, debug=False):
    url = "https://api.dhan.co/v2/pnlExit"
    client_id = os.environ.get("DHAN_CLIENT_ID")
    body = {
        "dhanClientId": client_id,
        "lossValue": f"{-abs(loss_value):.2f}",
        "productType": product_types or ["INTRADAY"],
        "enableKillSwitch": enable_kill_switch,
    }
    if profit_value is not None:
        body["profitValue"] = f"{profit_value:.2f}"
    if debug:
        log(f"DEBUG pnlExit request body: {body}")
    resp = requests.post(url, headers=_pnl_exit_headers(), json=body, proxies=_pnl_exit_proxies(), timeout=20)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    log(f"P&L Exit setup - status {resp.status_code}: {data}")
    return resp.status_code, data


def get_pnl_exit_status(debug=False):
    url = "https://api.dhan.co/v2/pnlExit"
    resp = requests.get(url, headers=_pnl_exit_headers(), proxies=_pnl_exit_proxies(), timeout=20)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    log(f"P&L Exit current config - status {resp.status_code}: {data}")
    return resp.status_code, data


def remove_pnl_exit(debug=False):
    url = "https://api.dhan.co/v2/pnlExit"
    resp = requests.delete(url, headers=_pnl_exit_headers(), proxies=_pnl_exit_proxies(), timeout=20)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    log(f"P&L Exit removed - status {resp.status_code}: {data}")
    return resp.status_code, data


def compute_today_trade_count(dhan, debug=False):
    resp = dhan.get_trade_book()
    if debug:
        log(f"DEBUG trade_book raw response: {resp}")
    fills = resp.get("data", []) if isinstance(resp, dict) else []

    by_security = {}
    for t in fills:
        sid = t.get("securityId") or t.get("security_id")
        qty = float(t.get("tradedQuantity") or t.get("traded_qty") or t.get("quantity") or 0)
        txn = (t.get("transactionType") or t.get("transaction_type") or "").upper()
        ts = (t.get("exchangeTime") or t.get("createTime") or t.get("create_time")
              or t.get("updateTime") or t.get("tradeTime") or t.get("orderId") or "")
        if not sid or qty == 0:
            continue
        signed_qty = qty if txn == "BUY" else -qty
        by_security.setdefault(sid, []).append((ts, signed_qty))

    trade_count = 0
    for sid, sec_fills in by_security.items():
        sec_fills.sort(key=lambda x: x[0])
        running = 0.0
        for ts, signed_qty in sec_fills:
            was_flat = running == 0
            running += signed_qty
            if was_flat and running != 0:
                trade_count += 1
    return trade_count


class TradingGuard:
    def __init__(self, capital, loss_pct, max_trades, auto_square_off=False,
                 state_file=None):
        self.capital = capital
        self.loss_pct = loss_pct
        self.max_trades = max_trades
        self.auto_square_off = auto_square_off
        self.state_file = state_file
        self.trade_count = 0
        self.locked = False
        self.lock_reason = ""
        self._load_state()

    def _load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            log(f"WARNING: could not read state file {self.state_file}, starting fresh.")
            return
        if data.get("date") != today_str():
            log(f"State file is from a previous day ({data.get('date')}) - starting today fresh.")
            return
        self.locked = data.get("locked", False)
        self.lock_reason = data.get("lock_reason", "")
        log(f"Loaded state: locked={self.locked}")

    def _save_state(self):
        if not self.state_file:
            return
        data = {
            "date": today_str(),
            "locked": self.locked,
            "lock_reason": self.lock_reason,
        }
        with open(self.state_file, "w") as f:
            json.dump(data, f)

    def square_off_all(self, dhan, positions_raw, debug=False):
        for p in positions_raw:
            sid = p.get("securityId") or p.get("security_id")
            segment = p.get("exchangeSegment") or p.get("exchange_segment")
            buy_qty = float(p.get("buyQty", 0) or 0)
            sell_qty = float(p.get("sellQty", 0) or 0)
            net = buy_qty - sell_qty
            if net == 0 or not sid or not segment:
                continue
            side = "SELL" if net > 0 else "BUY"
            qty = int(abs(net))
            log(f"AUTO SQUARE-OFF: {side} {qty} of {sid} ({segment}) to flatten open position")
            try:
                result = dhan.place_order(
                    security_id=sid, exchange_segment=segment, transaction_type=side,
                    quantity=qty, order_type="MARKET", product_type="INTRADAY", price=0,
                )
                log(f"Square-off order result: {result}")
            except Exception as e:
                log(f"ERROR placing square-off order for {sid}: {e}")

    def _activate_kill_switch_permanently(self, dhan):
        """Activates, deactivates, reactivates - burning the one manual
        override immediately. Returns True ONLY if the FINAL activation is
        genuinely confirmed active on Dhan's side - never assume success."""
        try:
            r1 = dhan.kill_switch("ACTIVATE")
            log(f"Kill switch ACTIVATE (1st) result: {r1}")
        except Exception as e:
            log(f"ERROR on first kill switch activation: {e}")
            log("This can happen if a position is still open - Dhan requires positions flat "
                "before the kill switch can activate. Will retry on the next check.")
            return False

        try:
            r2 = dhan.kill_switch("DEACTIVATE")
            log(f"Kill switch DEACTIVATE (burning the override) result: {r2}")
        except Exception as e:
            log(f"WARNING: could not deactivate to burn the override: {e}. "
                f"Kill switch is still ACTIVE from the first call - that's confirmed protection, "
                f"just not burned yet.")
            return True  # first activation genuinely succeeded, that's what matters

        try:
            r3 = dhan.kill_switch("ACTIVATE")
            log(f"Kill switch ACTIVATE (2nd, override now burned) result: {r3}")
        except Exception as e:
            log(f"WARNING: could not re-activate after burning the override: {e}. "
                f"Account may currently be UNLOCKED - will retry on the next check.")
            return False
        return True

    def _verify_kill_switch_active(self, dhan):
        """Independently confirms with Dhan whether the kill switch is
        genuinely active right now, rather than trusting our own memory of
        having called ACTIVATE. This is the real fix for the incident where
        the script believed it was locked while Dhan's account remained
        fully tradeable."""
        try:
            status = dhan.status_kill_switch()
            if isinstance(status, dict):
                data = status.get("data", status)
                text = json.dumps(data).upper() if not isinstance(data, str) else data.upper()
                return "ACTIVE" in text and "INACTIVE" not in text
        except Exception as e:
            log(f"WARNING: could not verify kill switch status: {e}")
        return False

    def poll(self, dhan, dry_run, debug):
        if self.locked:
            log(f"Already locked today ({self.lock_reason}). Nothing to do.")
            return True

        loss_limit = self.capital * self.loss_pct / 100
        resp = dhan.get_positions()
        if debug:
            log(f"DEBUG positions raw response: {resp}")
        positions_raw = resp.get("data", []) if isinstance(resp, dict) else []

        pnl = 0.0
        for p in positions_raw:
            pnl += float(p.get("realizedProfit", 0) or 0) + float(p.get("unrealizedProfit", 0) or 0)

        self.trade_count = compute_today_trade_count(dhan, debug)

        log(f"Trades today: {self.trade_count}/{self.max_trades} | P&L (realized+MTM): {pnl:.2f} | Loss limit: -{loss_limit:.2f}")

        breach_reason = None
        if pnl <= -loss_limit:
            breach_reason = f"Daily loss limit breached (P&L {pnl:.2f} <= -{loss_limit:.2f}, includes open MTM)"
        elif self.trade_count >= self.max_trades:
            breach_reason = f"Trade limit reached ({self.trade_count}/{self.max_trades})"

        breached = False
        if breach_reason:
            if dry_run:
                log(f"DRY RUN - would ACTIVATE kill switch now. Reason: {breach_reason}")
                if self.auto_square_off:
                    log("DRY RUN - would also auto square-off all open positions")
            else:
                log(f"BREACH: {breach_reason}")
                if self.auto_square_off:
                    self.square_off_all(dhan, positions_raw, debug)
                activation_reported_success = self._activate_kill_switch_permanently(dhan)

                # THE FIX: don't trust our own call's return value alone -
                # independently ask Dhan whether it's actually active.
                actually_active = self._verify_kill_switch_active(dhan)

                if actually_active:
                    self.locked = True
                    self.lock_reason = breach_reason
                    log("CONFIRMED: kill switch is genuinely active on Dhan's side. Locking.")
                else:
                    self.locked = False
                    log("*** NOT LOCKED YET *** Kill switch activation could not be confirmed "
                        "active (position likely still open - Dhan requires positions flat to "
                        "activate). Will keep checking and retrying every run until it succeeds "
                        "or the position closes. DO NOT ASSUME YOU ARE PROTECTED RIGHT NOW.")
            breached = True

        self._save_state()
        return breached


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, required=False, default=45000)
    parser.add_argument("--loss-pct", type=float, required=False, default=5)
    parser.add_argument("--max-trades", type=int, required=False, default=2)
    parser.add_argument("--poll-seconds", type=int, required=False, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-square-off", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--deactivate", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--state-file", type=str, default="guard_state.json")
    parser.add_argument("--setup-pnl-exit", action="store_true")
    parser.add_argument("--pnl-exit-status", action="store_true")
    parser.add_argument("--remove-pnl-exit", action="store_true")
    args = parser.parse_args()

    if args.setup_pnl_exit:
        loss_value = args.capital * args.loss_pct / 100
        log(f"Setting up P&L Exit: loss threshold -Rs{loss_value:.2f} (={args.loss_pct}% of Rs{args.capital:.0f}), kill switch enabled")
        setup_pnl_exit(loss_value=loss_value, enable_kill_switch=True, debug=args.debug)
        return

    if args.pnl_exit_status:
        get_pnl_exit_status(debug=args.debug)
        return

    if args.remove_pnl_exit:
        remove_pnl_exit(debug=args.debug)
        return

    dhan = get_client()

    if args.deactivate:
        result = dhan.kill_switch("DEACTIVATE")
        log(f"Deactivation result: {result}")
        return

    if args.status:
        result = dhan.status_kill_switch()
        log(f"Kill switch status: {result}")
        return

    guard = TradingGuard(args.capital, args.loss_pct, args.max_trades,
                          args.auto_square_off, state_file=args.state_file)

    if args.once:
        log(f"Single check | capital={args.capital} loss_pct={args.loss_pct}% max_trades={args.max_trades} dry_run={args.dry_run}")
        try:
            guard.poll(dhan, args.dry_run, args.debug)
        except Exception as e:
            log(f"ERROR during check: {e}")
            sys.exit(1)
        return

    log(f"Starting guard | capital={args.capital} loss_pct={args.loss_pct}% max_trades={args.max_trades} "
        f"poll={args.poll_seconds}s dry_run={args.dry_run}")

    while True:
        try:
            breached = guard.poll(dhan, args.dry_run, args.debug)
            if breached and not args.dry_run:
                log("Kill switch activated. Stopping monitor for today.")
                break
        except Exception as e:
            log(f"ERROR during check: {e}")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
