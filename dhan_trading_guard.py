"""
Dhan Trading Guard
===================
Monitors your Dhan account during market hours and ACTIVATES Dhan's own
account-level kill switch once you hit either limit for the day:
  - MAX_TRADES executed trades, or
  - a loss of LOSS_PCT% of your capital (realized P&L from Dhan's own data)

IMPORTANT: Dhan's kill switch is a broker/account-level flag - once activated,
it should block new orders for the rest of the trading day account-wide,
including orders placed manually in the Dhan app itself, not just through
this script. That's what makes this different from a local tracker: the
enforcement happens on Dhan's side, not just in a spreadsheet or app you
could ignore.

BEFORE YOU TRUST THIS WITH REAL MONEY:
  1. Run with --dry-run for at least one full trading day first. It will
     print exactly what it WOULD do, without touching your account.
  2. Verify the field names this script reads from get_positions() and
     get_trade_book() actually match what your account returns - Dhan's
     API response shape can vary slightly. Run `python3 dhan_trading_guard.py --debug`
     once to dump raw responses and check the P&L numbers make sense.
  3. Know how to deactivate: python3 dhan_trading_guard.py --deactivate
     (or through the Dhan app itself, if they expose it there).
  4. This polls every POLL_SECONDS - it is NOT instantaneous. A fast-moving
     loss between polls can still happen before this reacts.

Setup:
  pip install dhanhq
  export DHAN_CLIENT_ID="your-client-id"
  export DHAN_ACCESS_TOKEN="your-access-token"
  (Get these from Dhan web -> DhanHQ Trading APIs section)

Run during market hours:
  python3 dhan_trading_guard.py --capital 45000 --loss-pct 5 --max-trades 2 --dry-run
"""

import os
import sys
import time
import json
import argparse
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


def get_today_pnl(dhan, debug=False):
    """Sums REALIZED + UNREALIZED (open, mark-to-market) P&L across today's
    positions. Realized alone is not enough - an open position sitting at
    a big MTM loss must count too, or the guard stays blind to exactly the
    situation where it matters most (a losing trade still open)."""
    resp = dhan.get_positions()
    if debug:
        log(f"DEBUG positions raw response: {resp}")
    positions = resp.get("data", []) if isinstance(resp, dict) else []
    total = 0.0
    for p in positions:
        realized = float(p.get("realizedProfit", 0) or 0)
        unrealized = float(p.get("unrealizedProfit", 0) or 0)
        total += realized + unrealized
    return total


def compute_today_trade_count(dhan, debug=False):
    """Counts real round-trip trades from the day's individual fills
    (trade book), not from the position snapshot. This is recomputed fresh
    from the FULL day's fills every single check - not built up
    incrementally - which fixes two real bugs found via live testing:
      1. A trade that opens AND closes within one polling gap was
         invisible to a position-snapshot-based check.
      2. The SAME instrument traded twice in one day (two separate
         round trips) only showed as one entry in the position snapshot,
         since Dhan aggregates position data per security per day.
    A "trade" here = a position going from flat (0) to non-flat, walked
    chronologically through that security's fills for the day."""
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
    """Holds state (only lock status now - trade count is recomputed fresh
    from the trade book on every check, see compute_today_trade_count) so a
    fresh run - like a new GitHub Actions invocation every few minutes -
    knows whether today is already locked, instead of forgetting and
    potentially re-evaluating after a lock should already hold."""

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
        """Places a MARKET order in the opposite direction to flatten every
        open position. OFF by default - only runs if auto_square_off=True.
        This is the riskiest part of this script: verify the field names
        (securityId, exchangeSegment, netQty) match your account's real
        response via --debug before ever enabling this live."""
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
                    security_id=sid,
                    exchange_segment=segment,
                    transaction_type=side,
                    quantity=qty,
                    order_type="MARKET",
                    product_type="INTRADAY",
                    price=0,
                )
                log(f"Square-off order result: {result}")
            except Exception as e:
                log(f"ERROR placing square-off order for {sid}: {e}")

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
                result = dhan.kill_switch("ACTIVATE")
                log(f"Kill switch activation result: {result}")
                if self.auto_square_off:
                    self.square_off_all(dhan, positions_raw, debug)
                self.locked = True
                self.lock_reason = breach_reason
            breached = True

        self._save_state()
        return breached


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, required=False, default=45000)
    parser.add_argument("--loss-pct", type=float, required=False, default=5)
    parser.add_argument("--max-trades", type=int, required=False, default=2)
    parser.add_argument("--poll-seconds", type=int, required=False, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, never actually activate the kill switch.")
    parser.add_argument("--auto-square-off", action="store_true", help="DANGEROUS if untested: automatically close open positions with a market order when a breach fires. Off by default.")
    parser.add_argument("--debug", action="store_true", help="Print raw API responses to check field names.")
    parser.add_argument("--deactivate", action="store_true", help="Deactivate the kill switch and exit.")
    parser.add_argument("--status", action="store_true", help="Check current kill switch status and exit.")
    parser.add_argument("--once", action="store_true", help="Do a single check and exit, instead of looping. Use this for GitHub Actions / any scheduler.")
    parser.add_argument("--state-file", type=str, default="guard_state.json", help="Where to persist trade count / lock status between runs. Required for --once to work across scheduled runs.")
    args = parser.parse_args()

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
