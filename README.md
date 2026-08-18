# Dhan Trading Guard

Watches your Dhan account and activates Dhan's account-level kill switch once
you hit either limit for the day: a set number of trades, or a loss (realized
+ open MTM) of a set % of your capital. Runs on a schedule via GitHub Actions
so it's live even when your laptop is off and you're trading from your phone.

## Setup (do this once)

1. **Create a private GitHub repository** and push this whole folder to it.
   (Private, not public - it will hold your account-related config.)

2. **Add your Dhan API credentials as repo secrets** (never commit these
   directly): GitHub repo -> Settings -> Secrets and variables -> Actions ->
   New repository secret.
   - `DHAN_CLIENT_ID`
   - `DHAN_ACCESS_TOKEN`

   Get these from Dhan web -> DhanHQ Trading APIs section.

3. **Set your trading limits as repo variables** (same Settings page, the
   "Variables" tab next to Secrets):
   - `GUARD_CAPITAL` - e.g. `45000`
   - `GUARD_LOSS_PCT` - e.g. `5`
   - `GUARD_MAX_TRADES` - e.g. `2`
   - `GUARD_DRY_RUN` - set to `true` for testing (default), `false` once you
     trust it to go live and actually trip the kill switch.

4. **Turn on GitHub's failure email notifications** (usually on by default):
   your GitHub profile -> Settings -> Notifications -> make sure "Actions"
   failure emails are enabled. This is your alert if a scheduled run breaks.

## Test before trusting it (do this for at least 2 full trading days)

- Leave `GUARD_DRY_RUN` set to `true`.
- Each run prints what it *would* do without touching your account.
- Check the run logs: GitHub repo -> Actions tab -> click a run -> read the
  output. Compare the P&L number it prints against what the Dhan app shows
  you at that moment.
- You can also trigger a manual run any time from Actions -> Dhan Trading
  Guard -> "Run workflow", instead of waiting for the schedule.
- If the numbers don't match what Dhan shows you, something's wrong with
  the field-name assumptions in the script - stop and fix it before going
  live, don't just switch off dry-run and hope.

## Going live

Once you've verified 2+ days of matching numbers: set `GUARD_DRY_RUN` to
`false`. From then on, a real breach will call Dhan's kill switch for real.

## Manually checking or resetting

- Check kill switch status: `python3 dhan_trading_guard.py --status`
- Deactivate it (next trading day, or if you need to override):
  `python3 dhan_trading_guard.py --deactivate`
- These need `DHAN_CLIENT_ID`/`DHAN_ACCESS_TOKEN` set as local environment
  variables if you run them from your own machine instead of through Actions.

## Re-running the logic tests any time

No credentials needed - this uses fake data, not your real account:

```
pip install dhanhq
python3 test_dhan_trading_guard.py
```

## Known limitations - read before trusting this with real money

- Checks every 5 minutes (schedule), not instantly. A very fast move against
  you between checks can still happen before this reacts.
- The kill switch blocks *new* orders. It does NOT automatically close a
  position that's already open and losing - use `--auto-square-off` for that,
  but it is OFF by default and is the riskiest part of this script (it places
  a real market order on your behalf). Test extensively with `--debug` before
  ever turning it on.
- Depends on Dhan's `get_positions()` returning the field names this script
  expects (`securityId`, `buyQty`, `sellQty`, `realizedProfit`,
  `unrealizedProfit`). Verify with `--debug` against your real account before
  trusting the numbers.
- This is a personal safety tool, not a certified/audited financial product.
  Test it thoroughly. You are responsible for your own trading and capital.
