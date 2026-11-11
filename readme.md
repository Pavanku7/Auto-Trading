# Auto-Trading — NSE Intraday Bot

A personal intraday trading system for the Indian market (NSE). It scans a
watchlist, applies a rules-based strategy with indicator confirmation, sizes
positions off a fixed risk-per-trade, and manages exits with a trailing stop.

> ⚠️ **Status: paper-trading / research.** Across 24 simulated days the strategy
> is net **negative** (see "Honest performance" below). Do **not** put real money
> on it until the win-rate and expectancy are fixed. Treat this as a backtest/
> paper harness, not a money printer.

## Layout

```
Auto-Trading/
├── paper_trade_v4_indicators.py   ← current paper simulator (Open=Low/High + VWAP+EMA+RSI)
├── backtest_v4.py                 ← replays the v4 strategy over ~60d of history
├── Groww/
│   └── intraday_claudeai.py       ← current LIVE bot (Supertrend + VWAP + Volume, Groww MIS)
├── config.env                     ← API keys / secrets  (DO NOT COMMIT)
├── paper_trade_report.json        ← per-day results history
├── paper_trade_log.csv            ← trade-by-trade log
└── legacy/                        ← archived earlier versions (see legacy/README.md)
```

There are **two independent strategies** here — keep them straight:

1. **`paper_trade_v4_indicators.py`** — your "sheet" strategy. First 5-min candle
   Open==Low → BUY, Open==High → SELL, confirmed by VWAP + EMA(9/21) + RSI(14).
   Paper only, no broker calls. Uses `yfinance` for data.
2. **`Groww/intraday_claudeai.py`** — a different, momentum strategy (Supertrend +
   VWAP + volume surge). This one places **real Groww MIS orders**.

## Honest performance (paper_trade_report.json, 24 days)

| Metric | Value |
|--------|-------|
| Total trades | 60 |
| Win rate | **38.3%** |
| Net PnL | **−₹717** on ₹10,000 (−7.2%) |

At a 2:1 reward:risk, 38% win rate *should* be break-even; the loss comes from
brokerage/slippage (₹40 flat + 0.05% per side) eating the edge, and from most
trades exiting flat at the 3:10 PM time-stop rather than hitting target. See
suggestions below.

## Backtest results (`backtest_v4.py`, 136 trades over ~56 days)

`backtest_v4.py` replays the v4 strategy over ~60 days of 5-min history (Yahoo's
cap) using the *same* strategy code as the live sim. It found the same problem,
much more clearly. Two findings, in order of importance:

**1. The ₹40 flat fee is the #1 killer.** Risk per trade is 1% of ₹10k = ₹100, so
a ₹40 round-trip fee is **40% of the risk budget**. Many trades are gross-positive
but net-negative (e.g. +₹3.43 of price move → −₹26 after fee). Fee sensitivity:

| Round-trip fee | Win rate | Total PnL | Expectancy/trade |
|---------------|----------|-----------|------------------|
| ₹40 (current assumption) | 3.7% | −₹6,240 | −₹45.89 |
| ₹20 | 14.7% | −₹3,520 | −₹25.89 |
| ₹8 (realistic Groww intraday) | 27.2% | −₹1,888 | −₹13.89 |

The ₹40 figure is almost certainly too pessimistic — Groww intraday on a ₹100-risk
position is closer to ₹5–10 round-trip. **Fix the fee constant to a realistic value.**

**2. But even with a realistic fee, the entry has no edge — and tuning the target
does not help.** Sweeping the reward:risk target at the realistic ₹12 fee
(`--fee 12 --no-trailing --rr N`):

| Target R:R | Win rate | Realized R:R | Expectancy/trade | Total PnL |
|-----------|----------|--------------|------------------|-----------|
| 1.0 : 1 | 21.3% | 0.44 : 1 | −₹17.94 | −₹2,440 |
| 1.5 : 1 | 25.0% | 0.65 : 1 | −₹17.03 | −₹2,316 |
| 2.0 : 1 | 26.5% | 0.71 : 1 | −₹17.35 | −₹2,360 |

Expectancy is pinned at ≈ **−₹17/trade no matter where the target is**. The
realized R:R never gets above 0.71 because the stop (candle low/high) is hit far
more often than any target — i.e. **the Open=Low/High + VWAP/EMA/RSI entry has no
directional edge in this sample.** You can't fix a no-edge entry by moving the
target or cutting fees; the fee fix just changes how fast you lose.

The ₹40 → ₹12 fee correction has been applied to `paper_trade_v4_indicators.py`.
That was a real bug (a break-even edge would have survived it), but it is not
enough on its own.

**Bottom line: do not trade this live.** The next real work is the *entry signal*,
not fees or targets. Options: test a genuinely different entry (ORB breakout,
gap-and-go, VWAP reclaim), add a market-regime filter so it only trades trending
days, or accept that this pattern doesn't work on NSE large-caps and move on.
Re-run `backtest_v4.py` after every change — the bar is **profit factor > 1** and
**positive expectancy** before any real money.

## Running the paper simulator

```bash
pip install yfinance pandas numpy python-dotenv curl_cffi ta nsepython
python paper_trade_v4_indicators.py
```

It waits for 9:20 AM IST, scans, and monitors until 3:30 PM. Off-hours it will
run but find no "today" candles.

## Running the backtester

```bash
python backtest_v4.py                      # full watchlist, 60d, ₹40 fee
python backtest_v4.py --fee 8              # realistic Groww intraday fee
python backtest_v4.py --no-trailing       # test with fixed SL + target only
python backtest_v4.py --symbols RELIANCE INFY SBIN   # quick subset
python backtest_v4.py --optimistic        # target wins intra-candle ties (matches live sim)
```

By default it uses **conservative fills** (if a candle straddles both stop and
target, the stop fills first) so results don't overstate the edge. Writes a full
per-trade breakdown to `backtest_v4_report.json`. Runs against live Yahoo data,
so it needs a network connection and takes ~1–2 min for the full 110-symbol list.

## Live Groww bot (real orders)

```bash
python Groww/intraday_claudeai.py
```

Requires a valid `GROWW_API_KEY` in `config.env`. **Start with paper mode.**

---

## Suggested improvements (ranked)

**1. Rotate your API keys NOW.** `config.env` contains a live Groww API key,
API secret, and TOTP secret in plaintext. If this folder was ever shared,
zipped, or synced, treat those as compromised — regenerate them in Groww.

**2. Add a paper/live safety switch to the Groww bot.** `intraday_claudeai.py`
places real orders with no dry-run flag and no confirmation. One accidental run
outside market hours or with bad data can fire live orders. Add a `PAPER_MODE`
env flag that logs instead of calling `place_order`.

**3. The real problem is fees vs. edge, not the signals.** ₹40 flat fee on a
₹100-risk trade means you need the trade to make >₹40 just to break even. Either
(a) risk more per trade so fees are proportionally smaller, or (b) target a
higher R multiple, or (c) trade fewer, higher-conviction setups. The current
2:1 target + tight time-stop bleeds the account through costs.

**4. Backtest before paper-trading.** You're "paper trading" live one day at a
time — that's slow and noisy (24 days = 60 trades). Download 6–12 months of
5-min history once and replay the strategy over it to get a statistically
meaningful win-rate/expectancy in minutes. Both strategies share the same
indicator code, so this is straightforward to build.

**5. Add a market-regime filter.** Both strategies trade every day regardless of
whether the Nifty is trending or chopping. Open=Low/High and Supertrend both
lose in sideways markets. Gate entries on Nifty trend / VIX / ADX.

**6. Consolidate to one strategy.** You have two live strategies with different
logic. Pick the one with better backtested expectancy and archive the other, so
you're improving one thing instead of maintaining two.

**7. Realistic fills.** The paper sim fills SL/target at the exact level. Real
intraday fills gap through stops. Model worst-case fills (candle close, or
next-candle open) so paper results don't overstate the edge.
