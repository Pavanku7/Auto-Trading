"""
================================================================
  BACKTESTER for the v4 strategy
  (Open=Low/High + VWAP + EMA(9/21) + RSI(14))
================================================================

  WHY THIS EXISTS
  ─────────────────────────────────────────────
  Paper-trading one live day at a time is slow and noisy — 24 days
  only gave 60 trades. This replays the SAME strategy over every
  trading day Yahoo will give us (~60 days of 5-min data) in one
  run, so you get a statistically meaningful win-rate and
  expectancy in seconds instead of months.

  It imports the strategy functions straight from
  paper_trade_v4_indicators.py, so the backtest and the live paper
  sim can never silently drift apart. Change the strategy in one
  place, both update.

  HOW IT WORKS
  ─────────────────────────────────────────────
  For each historical trading day D (after a warm-up period):
    1. Take D's first 5-min candle (9:15). Check Open=Low/High.
       Marubozu → use the 2nd candle (same rule as live).
    2. Compute VWAP/EMA/RSI *as of the entry candle only* — no
       look-ahead. EMA/RSI use prior days for warm-up; VWAP resets
       on D.
    3. If all 3 indicators confirm, record the signal.
    4. Rank the day's signals by RSI strength, take the top N
       (MAX_TRADES_PER_DAY), open them.
    5. Walk forward candle-by-candle through the rest of D,
       updating the trailing SL and checking SL / target / trailing
       / 3:10 PM force-exit — identical logic to the live defense
       loop. Fills, slippage and the flat fee reuse the live config.

  FILL REALISM
  ─────────────────────────────────────────────
  CONSERVATIVE_FILLS = True: if a single candle straddles BOTH the
  stop and the target, assume the STOP filled first (worst case).
  Intraday you can't tell the intra-candle path, and assuming the
  good fill is how backtests lie to you. Set False to match the
  (optimistic) live paper sim exactly.

  USAGE
  ─────────────────────────────────────────────
    python backtest_v4.py                # full watchlist, 60d
    python backtest_v4.py --days 30      # last 30 calendar days
    python backtest_v4.py --index NIFTY50
    python backtest_v4.py --symbols RELIANCE INFY SBIN
================================================================
"""

import argparse
import json
import os
import sys
import time

# Windows consoles default to cp1252 and choke on ✓/→ etc. Force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

# Reuse the EXACT strategy logic from the live paper sim ---------
import paper_trade_v4_indicators as strat
from paper_trade_v4_indicators import (
    BASE_DIR,
    PAPER_CAPITAL,
    MAX_TRADES_PER_DAY,
    PRICE_FLOOR,
    PRICE_CAP,
    RR_RATIO,
    SLIPPAGE_PCT,
    FLAT_FEE_RS,
    FORCE_EXIT_TIME,
    check_indicators,
    calculate_position,
    compute_vwap,
    compute_ema,
    compute_rsi,
    is_open_equal_low,
    is_open_equal_high,
    is_marubozu,
    update_trailing_sl,
    make_session,
    get_watchlist,
    EMA_FAST,
    EMA_SLOW,
    RSI_PERIOD,
)

# ─── BACKTEST SETTINGS ───
CONSERVATIVE_FILLS = True    # stop wins ties inside a candle (see docstring)
WARMUP_DAYS        = 2       # skip first N days so EMA/RSI are seeded
USE_TRAILING       = True    # False = fixed SL + target only (no trailing ratchet)
FEE_OVERRIDE       = None    # set to a rupee value to override strat.FLAT_FEE_RS
REPORT_JSON        = os.path.join(BASE_DIR, "backtest_v4_report.json")


# ================================================================
# DATA
# ================================================================
def download_history(symbol, session, days):
    """Download `days` of 5-min candles, IST-localised, columns flattened."""
    import yfinance as yf
    ticker = f"{symbol}.NS" if not symbol.startswith("^") else symbol
    period = f"{min(days, 60)}d"          # Yahoo hard-caps 5m at 60d
    data = yf.download(ticker, period=period, interval="5m",
                       progress=False, session=session)
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    try:
        if data.index.tz is None:
            data.index = data.index.tz_localize("UTC")
        data.index = data.index.tz_convert("Asia/Kolkata")
    except Exception:
        pass
    return data.dropna()


def indicators_as_of(hist_upto, today_upto):
    """
    Indicator snapshot with NO look-ahead: `hist_upto` and `today_upto`
    must already be sliced to end at the entry candle.
    Mirrors strat.get_indicators but with slices we control.
    """
    if hist_upto is None or len(hist_upto) < EMA_SLOW + RSI_PERIOD:
        return None
    if today_upto is None or today_upto.empty:
        return None

    vwap_series = compute_vwap(today_upto)
    vwap = float(vwap_series.iloc[-1]) if not vwap_series.empty else None

    close = hist_upto["Close"]
    ema9 = compute_ema(close, EMA_FAST)
    ema21 = compute_ema(close, EMA_SLOW)
    rsi = compute_rsi(close, RSI_PERIOD)

    return {
        "vwap": vwap if vwap and not np.isnan(vwap) else float(close.iloc[-1]),
        "ema9": float(ema9.iloc[-1]),
        "ema21": float(ema21.iloc[-1]),
        "rsi": float(rsi.iloc[-1]),
        "close": float(close.iloc[-1]),
    }


# ================================================================
# SIGNAL GENERATION (per symbol, per day) — no look-ahead
# ================================================================
def find_signal_for_day(symbol, all_data, day_candles):
    """
    Returns a signal dict for this symbol on this day, or None.
    Uses only data up to and including the entry candle.
    """
    if day_candles is None or len(day_candles) < 2:
        return None

    c1 = day_candles.iloc[0]
    close_price = float(c1["Close"])
    if close_price < PRICE_FLOOR or close_price > PRICE_CAP:
        return None

    # STEP 1 — Open=Low / Open=High
    if is_open_equal_low(c1):
        signal_type = "LONG"
    elif is_open_equal_high(c1):
        signal_type = "SHORT"
    else:
        return None

    # STEP 2 — Marubozu → 2nd candle
    entry_candle = c1
    entry_idx = 0
    candle_label = "1st candle (9:15)"
    if is_marubozu(c1):
        entry_candle = day_candles.iloc[1]
        entry_idx = 1
        candle_label = "2nd candle (9:20) — marubozu"

    entry_ts = day_candles.index[entry_idx]

    # STEP 3 — indicators as of the entry candle (no look-ahead)
    hist_upto = all_data[all_data.index <= entry_ts]
    today_upto = day_candles.iloc[: entry_idx + 1]
    indicators = indicators_as_of(hist_upto, today_upto)
    if indicators is None:
        return None

    # STEP 4 — all 3 indicators must confirm
    passed, reason = check_indicators(indicators, signal_type)
    if not passed:
        return None

    # STEP 5 — entry / SL / target / qty (sheet formula)
    entry = float(entry_candle["Close"])
    sl = float(entry_candle["Low"]) if signal_type == "LONG" else float(entry_candle["High"])
    if abs(entry - sl) < 0.01:
        return None
    if abs(entry - sl) / entry > 0.025:       # SL too wide (gap risk)
        return None

    qty, stock_risk, target = calculate_position(PAPER_CAPITAL, entry, sl, signal_type)
    if qty <= 0:
        return None

    return {
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_ts": entry_ts,
        "entry_idx": entry_idx,
        "entry": entry,
        "sl": sl,
        "target": target,
        "qty": qty,
        "stock_risk": stock_risk,
        "candle_label": candle_label,
        "indicators": indicators,
    }


# ================================================================
# TRADE SIMULATION — walk the rest of the day, candle by candle
# ================================================================
def simulate_trade(sig, day_candles):
    """Replay exits over the post-entry candles. Returns a closed-trade dict."""
    side = sig["signal_type"]
    qty = sig["qty"]

    # Entry slippage (same as live place_order)
    if side == "LONG":
        entry = sig["entry"] * (1 + SLIPPAGE_PCT)
    else:
        entry = sig["entry"] * (1 - SLIPPAGE_PCT)

    pos = {
        "symbol": sig["symbol"], "side": side, "qty": qty,
        "entry": entry, "sl": sig["sl"], "target": sig["target"],
        "risk_pts": sig["stock_risk"], "trailing_active": False,
        "highest_price": entry, "lowest_price": entry,
    }

    forward = day_candles.iloc[sig["entry_idx"] + 1:]
    exit_price, exit_reason = None, None

    for ts, bar in forward.iterrows():
        high = float(bar["High"]); low = float(bar["Low"]); close = float(bar["Close"])

        if USE_TRAILING:
            update_trailing_sl(pos, close)

        hit_sl = (low <= pos["sl"]) if side == "LONG" else (high >= pos["sl"])
        hit_tp = (high >= pos["target"]) if side == "LONG" else (low <= pos["target"])

        if hit_sl and hit_tp:
            # Candle straddles both — conservative = stop fills first
            if CONSERVATIVE_FILLS:
                exit_reason = "TRAILING SL HIT" if pos["trailing_active"] else "STOP LOSS HIT"
                exit_price = pos["sl"]
            else:
                exit_reason = "TARGET HIT"
                exit_price = pos["target"]
        elif hit_sl:
            exit_reason = "TRAILING SL HIT" if pos["trailing_active"] else "STOP LOSS HIT"
            exit_price = pos["sl"]
        elif hit_tp:
            exit_reason = "TARGET HIT"
            exit_price = pos["target"]
        elif ts.time() >= FORCE_EXIT_TIME:
            exit_reason = "TIME EXIT (3:10 PM)"
            exit_price = close

        if exit_reason:
            break

    # Fell off the end of the day without an explicit exit → close at last candle
    if exit_reason is None:
        last = forward.iloc[-1] if not forward.empty else day_candles.iloc[-1]
        exit_price = float(last["Close"])
        exit_reason = "EOD CLOSE"

    # Exit slippage + fee (same as live exit_position)
    if side == "LONG":
        actual_exit = exit_price * (1 - SLIPPAGE_PCT)
        pnl_pts = actual_exit - entry
    else:
        actual_exit = exit_price * (1 + SLIPPAGE_PCT)
        pnl_pts = entry - actual_exit

    fee = FEE_OVERRIDE if FEE_OVERRIDE is not None else FLAT_FEE_RS
    net_pnl = pnl_pts * qty - fee

    return {
        "symbol": sig["symbol"], "side": side,
        "entry": round(entry, 2), "exit": round(actual_exit, 2),
        "qty": round(qty, 4), "pnl_pts": round(pnl_pts, 2),
        "pnl_rs": round(net_pnl, 2), "reason": exit_reason,
        "rsi": round(sig["indicators"]["rsi"], 1),
        "candle": sig["candle_label"],
    }


# ================================================================
# BACKTEST DRIVER
# ================================================================
def run_backtest(symbols, days):
    session = make_session()

    print(f"\n{'='*64}")
    print(f"  BACKTEST — v4 (Open=Low/High + VWAP + EMA + RSI)")
    print(f"{'='*64}")
    print(f"  Symbols        : {len(symbols)}")
    print(f"  History        : {min(days,60)} days (Yahoo 5m cap = 60)")
    print(f"  Capital        : Rs.{PAPER_CAPITAL:,}")
    print(f"  Max trades/day : {MAX_TRADES_PER_DAY}")
    print(f"  Fill model     : {'CONSERVATIVE (stop wins ties)' if CONSERVATIVE_FILLS else 'OPTIMISTIC (target wins ties)'}")
    print(f"  Fee/trade      : Rs.{FLAT_FEE_RS}  |  Slippage: {SLIPPAGE_PCT*100:.3f}%/side")
    print(f"{'='*64}\n")

    # 1) Download everything once, group by day
    print("  Downloading history...")
    hist = {}
    for i, sym in enumerate(symbols, 1):
        try:
            time.sleep(0.15)
            data = download_history(sym, session, days)
            if data is not None and len(data) >= EMA_SLOW + RSI_PERIOD:
                hist[sym] = data
            sys.stdout.write(f"\r  {i}/{len(symbols)} downloaded ({len(hist)} usable)")
            sys.stdout.flush()
        except Exception as e:
            print(f"\n  {sym}: download error — {e}")
    print(f"\n  {len(hist)} symbols with usable data.\n")

    if not hist:
        print("  No data. Market data source may be blocked or off-hours.")
        return

    # 2) Union of all trading days (sorted)
    all_days = sorted({d for data in hist.values()
                       for d in data.index.normalize().unique()})
    trading_days = all_days[WARMUP_DAYS:]     # skip warm-up
    print(f"  {len(all_days)} trading days found, "
          f"testing {len(trading_days)} (after {WARMUP_DAYS}-day warm-up).\n")

    # 3) Replay each day
    balance = float(PAPER_CAPITAL)
    all_trades = []
    daily = []

    for day in trading_days:
        day_str = pd.Timestamp(day).strftime("%Y-%m-%d")

        # Gather signals across all symbols for this day
        signals = []
        for sym, data in hist.items():
            day_candles = data[data.index.normalize() == day]
            if day_candles.empty:
                continue
            sig = find_signal_for_day(sym, data, day_candles)
            if sig:
                signals.append((sig, day_candles))

        if not signals:
            continue

        # Rank by RSI strength, take top N (same as live)
        signals.sort(key=lambda s: abs(s[0]["indicators"]["rsi"] - 50), reverse=True)
        signals = signals[:MAX_TRADES_PER_DAY]

        day_pnl = 0.0
        for sig, day_candles in signals:
            trade = simulate_trade(sig, day_candles)
            trade["date"] = day_str
            all_trades.append(trade)
            day_pnl += trade["pnl_rs"]

        balance += day_pnl
        daily.append({"date": day_str, "trades": len(signals),
                      "pnl": round(day_pnl, 2), "balance": round(balance, 2)})

    print_report(all_trades, daily, balance)


# ================================================================
# REPORTING
# ================================================================
def print_report(trades, daily, ending_balance):
    n = len(trades)
    if n == 0:
        print("  No trades generated over the backtest window.")
        print("  (The 3-indicator filter is very selective — that may be expected.)")
        return

    wins = [t for t in trades if t["pnl_rs"] > 0]
    losses = [t for t in trades if t["pnl_rs"] <= 0]
    total = sum(t["pnl_rs"] for t in trades)
    win_rate = len(wins) / n * 100
    avg_win = sum(t["pnl_rs"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["pnl_rs"] for t in losses) / len(losses) if losses else 0.0
    rr = abs(avg_win / avg_loss) if avg_loss else 0.0
    expectancy = total / n

    # Profit factor & max drawdown (on daily equity)
    gross_win = sum(t["pnl_rs"] for t in wins)
    gross_loss = abs(sum(t["pnl_rs"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else float("inf")

    peak = PAPER_CAPITAL; max_dd = 0.0
    for d in daily:
        peak = max(peak, d["balance"])
        max_dd = max(max_dd, peak - d["balance"])

    # Reason breakdown
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    print(f"\n{'='*64}")
    print(f"  BACKTEST REPORT")
    print(f"{'='*64}")
    print(f"  Trading days w/ trades : {len(daily)}")
    print(f"  Total trades           : {n}")
    print(f"  Wins / Losses          : {len(wins)} / {len(losses)}")
    print(f"  Win rate               : {win_rate:.1f}%")
    print(f"  Avg win                : Rs.{avg_win:+.2f}")
    print(f"  Avg loss               : Rs.{avg_loss:+.2f}")
    print(f"  Actual R:R             : {rr:.2f}:1  (target {RR_RATIO}:1)")
    print(f"  Expectancy / trade     : Rs.{expectancy:+.2f}")
    print(f"  Profit factor          : {profit_factor:.2f}   (>1 = profitable)")
    print(f"  Max drawdown           : Rs.{max_dd:.2f}")
    print(f"  {'-'*60}")
    print(f"  Capital                : Rs.{PAPER_CAPITAL:,} → Rs.{ending_balance:,.2f}")
    print(f"  Total PnL              : Rs.{total:+.2f}  ({total/PAPER_CAPITAL*100:+.2f}%)")
    print(f"  {'-'*60}")
    print(f"  Exit reasons:")
    for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:<22} {c:>3}  ({c/n*100:.0f}%)")
    print(f"{'='*64}")

    # Verdict
    print(f"\n  VERDICT:")
    if expectancy > 0 and profit_factor > 1.1:
        print(f"  ✓ Positive expectancy (Rs.{expectancy:+.2f}/trade). Worth forward-testing on paper.")
    elif expectancy > 0:
        print(f"  ~ Barely positive — thin edge, fees could flip it. Needs tuning.")
    else:
        print(f"  ✗ Negative expectancy. Do NOT trade live. Fix the strategy first.")
        tp = reasons.get("TARGET HIT", 0)
        te = reasons.get("TIME EXIT (3:10 PM)", 0) + reasons.get("EOD CLOSE", 0)
        if te > tp:
            print(f"    Most trades ({te}) exit flat at the time-stop, not at target.")
            print(f"    → target may be too far, or entries lack follow-through.")
    print()

    # Persist
    report = {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "actual_rr": round(rr, 2), "expectancy": round(expectancy, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "max_drawdown": round(max_dd, 2),
        "total_pnl": round(total, 2), "ending_balance": round(ending_balance, 2),
        "conservative_fills": CONSERVATIVE_FILLS,
        "exit_reasons": reasons,
        "daily": daily,
        "all_trades": trades,
    }
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Full report saved: {REPORT_JSON}\n")


# ================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backtest the v4 strategy.")
    ap.add_argument("--days", type=int, default=60, help="Calendar days of history (max 60 for 5m).")
    ap.add_argument("--index", default="BOTH", choices=["NIFTY50", "NIFTYNEXT50", "BOTH"])
    ap.add_argument("--symbols", nargs="+", help="Explicit symbols (overrides --index).")
    ap.add_argument("--fee", type=float, help="Override round-trip fee in rupees (e.g. --fee 8).")
    ap.add_argument("--rr", type=float, help="Override reward:risk target (e.g. --rr 1.0).")
    ap.add_argument("--no-trailing", action="store_true", help="Disable trailing stop (fixed SL + target only).")
    ap.add_argument("--optimistic", action="store_true", help="Target wins intra-candle ties (matches live paper sim).")
    args = ap.parse_args()

    if args.fee is not None:
        FEE_OVERRIDE = args.fee
    if args.rr is not None:
        strat.RR_RATIO = args.rr    # calculate_position reads this at call time
    if args.no_trailing:
        USE_TRAILING = False
    if args.optimistic:
        CONSERVATIVE_FILLS = False

    syms = args.symbols if args.symbols else get_watchlist(index=args.index)
    run_backtest(syms, args.days)
