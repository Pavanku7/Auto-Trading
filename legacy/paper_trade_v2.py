"""
================================================================
  PAPER TRADING — Open=Low / Open=High Strategy (v3)
  Improved with proper trading filters and risk management
================================================================

  STRATEGY IMPROVEMENTS OVER v2
  ─────────────────────────────────────────────
  1. CONFIRMATION CANDLE — Don't enter on the 1st candle alone.
     Wait for the 2nd candle to CONFIRM direction before entry.
     This filters out false signals where price reverses immediately.

  2. VOLUME FILTER — Only take signals where the 1st candle volume
     is above the stock's recent average. High volume = conviction.

  3. MARKET TREND FILTER — Check Nifty 50 index direction.
     Avoid LONG signals in a falling market (and vice versa).
     This eliminates fighting the trend.

  4. MINIMUM CANDLE RANGE — Skip signals where the 1st candle
     range (High-Low) is too small (< 0.3% of price). Flat
     candles produce unreliable Open=Low/High patterns.

  5. TIGHTER RISK — Risk per trade capped strictly. If candle
     range makes SL too wide, reduce qty or skip.

  6. TRAILING STOP — Once trade moves 1R in favor, move SL to
     breakeven. Protects capital on reversals.

  7. PARTIAL EXIT — Book 50% at 1.5R, trail remaining to 2R.
     Locks in profits while giving room for bigger moves.

  ENTRY LOGIC
  ─────────────────────────────────────────────
  BUY  signal  →  1st candle: Open == Low (bullish)
                   2nd candle: Close > 1st candle High (confirmation)
  SELL signal  →  1st candle: Open == High (bearish)
                   2nd candle: Close < 1st candle Low (confirmation)

  ENTRY  = 2nd candle Close (confirmed entry)
  SL     = 1st candle Low (buy) / 1st candle High (sell)
  Risk   = |Entry - SL|
  Target1= Entry + 1.5×Risk (partial exit 50%)
  Target2= Entry + 2.5×Risk (full exit)

  EXIT RULES
  ─────────────────────────────────────────────
  1. Stop-loss hit → full exit
  2. Target 1 hit  → exit 50%, move SL to breakeven
  3. Target 2 hit  → exit remaining
  4. Trailing stop → if price moves 1R in favor, SL = entry
  5. Force exit at 3:10 PM (10 min before close for MIS)
================================================================
"""

import yfinance as yf
import pandas as pd
import os
import json
import csv
import time
from dotenv import load_dotenv
from datetime import datetime, time as dtime, timedelta
import urllib3
from curl_cffi import requests as cffi_requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "config.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

# ================================================================
# CONFIG
# ================================================================
PAPER_CAPITAL       = 10000    # Simulated capital
RISK_PCT            = 0.01     # 1% of capital at risk per trade → Rs.100
MAX_RISK_PER_TRADE  = PAPER_CAPITAL * RISK_PCT  # Rs.100 hard cap

# Reward targets (multi-target approach)
TARGET_1_RR         = 1.5      # First partial exit at 1.5R
TARGET_2_RR         = 2.5      # Final exit at 2.5R
PARTIAL_EXIT_PCT    = 0.50     # Exit 50% at Target 1

MAX_TRADES_PER_DAY  = 3
MAX_DAILY_LOSS_PCT  = 0.02     # Kill switch at 2% daily loss (tighter)
PRICE_CAP           = 2000     # Max stock price
PRICE_FLOOR         = 50       # Min stock price (avoid penny stocks)

# How close Open must be to Low/High to qualify (0.15% tolerance)
OPEN_HL_TOLERANCE   = 0.0015

# If candle body is > 85% of its total range → marubozu
MARUBOZU_THRESHOLD  = 0.85

# MINIMUM candle range as % of price — filter out doji/flat candles
MIN_CANDLE_RANGE_PCT = 0.003   # 0.3% minimum range

# Volume filter — 1st candle volume must be at least this multiple
# of the average volume (based on prior day's candles)
MIN_VOLUME_RATIO    = 1.2      # 1.2x average volume minimum

# Timing
ENTRY_WINDOW_START  = dtime(9, 20)   # Earliest entry (after 2nd candle)
ENTRY_WINDOW_END    = dtime(10, 30)  # Stop taking new entries after this
FORCE_EXIT_TIME     = dtime(15, 10)  # Force-close (3:10 PM, 5 min buffer)
MARKET_CLOSE        = dtime(15, 30)

# Defense loop
DEFENSE_INTERVAL_SEC = 30      # Check every 30 seconds (faster monitoring)

# Costs
SLIPPAGE_PCT = 0.0005  # 0.05% slippage
FLAT_FEE_RS  = 40.0    # Brokerage + taxes per round trip

# Files
LOG_CSV     = os.path.join(BASE_DIR, "paper_trade_log.csv")
REPORT_JSON = os.path.join(BASE_DIR, "paper_trade_report.json")

# Nifty index ticker for market trend filter
NIFTY_INDEX = "^NSEI"


# ================================================================
# WATCHLIST — Nifty 50 + Nifty Next 50
# ================================================================
NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFC",
    "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR",
    "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND",
    "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE",
    "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "ULTRACEMCO",
    "WIPRO",
]

NIFTY_NEXT_50 = [
    "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM",
    "BAJAJHLDNG", "BANKBARODA", "BERGEPAINT", "BOSCHLTD", "CANBK",
    "CHOLAFIN", "COLPAL", "DABUR", "DLF", "DMART",
    "GAIL", "GODREJCP", "GODREJPROP", "HAL", "HAVELLS",
    "ICICIPRULI", "INDHOTEL", "IOC", "IRCTC", "JSWENERGY",
    "JUBLFOOD", "LICI", "LODHA", "LTIM", "LTTS",
    "LUPIN", "MARICO", "MOTHERSON", "MPHASIS", "MUTHOOTFIN",
    "NAUKRI", "NHPC", "NMDC", "OBEROIRLTY", "OFSS",
    "OIL", "PAGEIND", "PAYTM", "PFC", "PIDILITIND",
    "PNB", "RECLTD", "SAIL", "SIEMENS", "SRF",
    "SUNTV", "TATAPOWER", "TRENT", "TVSMOTOR", "UBL",
    "UNIONBANK", "UNITDSPR", "VBL", "VEDL", "ZOMATO",
]


def get_watchlist(index="BOTH"):
    if index == "NIFTY50":
        symbols = NIFTY_50
    elif index == "NIFTYNEXT50":
        symbols = NIFTY_NEXT_50
    else:
        symbols = NIFTY_50 + NIFTY_NEXT_50
    print(f"  Watchlist: {index} — {len(symbols)} stocks loaded")
    print(f"  Price filter: Rs.{PRICE_FLOOR} – Rs.{PRICE_CAP}")
    return symbols


# ================================================================
# PAPER BROKER — with partial exits and trailing stop
# ================================================================
class PaperBroker:
    def __init__(self, starting_balance: float):
        self.balance          = starting_balance
        self.starting_balance = starting_balance
        self.open_positions   = {}
        self.trade_history    = []
        self.order_count      = 0

        if not os.path.exists(LOG_CSV):
            with open(LOG_CSV, "w", newline="") as f:
                csv.writer(f).writerow([
                    "date", "time", "symbol", "side", "action",
                    "qty", "entry", "sl", "target1", "target2",
                    "exit_price", "exit_reason",
                    "risk_pts", "pnl_pts", "pnl_rs",
                    "balance_after",
                ])

        print(f"\n{'='*60}")
        print(f"  PAPER TRADING ENGINE v3 — Open=Low / Open=High")
        print(f"  Capital     : Rs.{self.balance:,.2f}")
        print(f"  Risk/trade  : {RISK_PCT*100:.0f}% -> Rs.{MAX_RISK_PER_TRADE:.0f}")
        print(f"  Targets     : {TARGET_1_RR}R (50%) + {TARGET_2_RR}R (remaining)")
        print(f"  Max loss/day: {MAX_DAILY_LOSS_PCT*100:.0f}% -> Rs.{self.balance * MAX_DAILY_LOSS_PCT:.0f}")
        print(f"  Entry window: 9:20 AM – 10:30 AM")
        print(f"  Force exit  : 3:10 PM")
        print(f"  Trailing SL : Moves to breakeven at +1R")
        print(f"  NO REAL ORDERS — simulated fills only")
        print(f"{'='*60}\n")

    def place_order(self, symbol, side, qty, entry, sl, target1, target2, risk_pts, candle_label):
        """Place a LONG or SHORT paper order."""
        self.order_count += 1
        now = datetime.now()

        # Apply entry slippage
        if side == "LONG":
            actual_entry = entry * (1 + SLIPPAGE_PCT)
        else:
            actual_entry = entry * (1 - SLIPPAGE_PCT)

        self.open_positions[symbol] = dict(
            symbol=symbol, side=side, qty=qty,
            entry=actual_entry, original_entry=entry,
            sl=sl, target1=target1, target2=target2,
            risk_pts=risk_pts, entry_time=now,
            candle_used=candle_label,
            partial_exited=False,       # Track if T1 was hit
            original_qty=qty,
            trailing_active=False,      # Track if trailing SL is active
        )

        side_str = "BUY" if side == "LONG" else "SHORT"
        print(f"\n  [PAPER {side_str} #{self.order_count}]  {candle_label}")
        print(f"     Symbol  : {symbol}")
        print(f"     Entry   : Rs.{actual_entry:.2f} (filled with slippage)")
        print(f"     SL      : Rs.{sl:.2f}  |  Risk: Rs.{risk_pts:.2f}/share")
        print(f"     Target1 : Rs.{target1:.2f} ({TARGET_1_RR}R — exit 50%)")
        print(f"     Target2 : Rs.{target2:.2f} ({TARGET_2_RR}R — exit rest)")
        print(f"     Qty     : {qty:.4f} shares")
        print(f"     Max loss: Rs.{risk_pts * qty:.2f}")
        print(f"     Time    : {now.strftime('%H:%M:%S')}")

        self._log(symbol, side, "ENTRY", qty, actual_entry, sl, target1, target2,
                  None, None, risk_pts, None, None)

    def exit_position(self, symbol, exit_price, exit_reason, partial=False):
        """Exit full or partial position."""
        pos = self.open_positions.get(symbol)
        if not pos:
            return 0.0

        side = pos["side"]
        qty_to_exit = pos["qty"]

        if partial:
            qty_to_exit = pos["original_qty"] * PARTIAL_EXIT_PCT
            pos["qty"] -= qty_to_exit
            pos["partial_exited"] = True
            # Move SL to breakeven after partial exit
            pos["sl"] = pos["entry"]
            pos["trailing_active"] = True

        # Apply exit slippage
        if side == "LONG":
            actual_exit = exit_price * (1 - SLIPPAGE_PCT)
            pnl_pts = actual_exit - pos["entry"]
        else:
            actual_exit = exit_price * (1 + SLIPPAGE_PCT)
            pnl_pts = pos["entry"] - actual_exit

        gross_pnl = pnl_pts * qty_to_exit

        # Deduct fees only on full exit or final partial
        if not partial:
            net_pnl = gross_pnl - FLAT_FEE_RS
        else:
            net_pnl = gross_pnl - (FLAT_FEE_RS * 0.5)  # Half fees for partial

        self.balance += net_pnl
        hold_min = int((datetime.now() - pos["entry_time"]).total_seconds() / 60)

        result = "PROFIT" if net_pnl >= 0 else "LOSS"
        action = "PARTIAL EXIT" if partial else "FULL EXIT"
        print(f"\n  [{action} — {result}]  {symbol} [{side}]")
        print(f"     Exit    : Rs.{actual_exit:.2f}  ({exit_reason})")
        print(f"     Qty     : {qty_to_exit:.4f} of {pos['original_qty']:.4f}")
        print(f"     PnL     : {pnl_pts:+.2f} pts x {qty_to_exit:.4f} = Rs.{net_pnl:+.2f}")
        print(f"     Balance : Rs.{self.balance:,.2f}")

        if partial:
            print(f"     SL moved to breakeven: Rs.{pos['sl']:.2f}")
            print(f"     Remaining qty: {pos['qty']:.4f}")
        else:
            # Full exit — record trade and remove position
            self.trade_history.append({
                **pos,
                "exit_price": actual_exit,
                "exit_reason": exit_reason,
                "pnl_pts": pnl_pts,
                "pnl_rs": net_pnl,
                "hold_mins": hold_min,
            })
            del self.open_positions[symbol]

        self._log(pos["symbol"], side, action, qty_to_exit, pos["entry"],
                  pos["sl"], pos["target1"], pos["target2"],
                  actual_exit, exit_reason, pos["risk_pts"], pnl_pts, net_pnl)

        return net_pnl

    def _log(self, symbol, side, action, qty, entry, sl, target1, target2,
             exit_price, exit_reason, risk_pts, pnl_pts, pnl_rs):
        now = datetime.now()
        with open(LOG_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                symbol, side, action,
                round(qty, 4),
                round(entry, 2),
                round(sl, 2) if sl is not None else "",
                round(target1, 2) if target1 is not None else "",
                round(target2, 2) if target2 is not None else "",
                round(exit_price, 2) if exit_price is not None else "",
                exit_reason or "",
                round(risk_pts, 2) if risk_pts is not None else "",
                round(pnl_pts, 2) if pnl_pts is not None else "",
                round(pnl_rs, 2) if pnl_rs is not None else "",
                round(self.balance, 2),
            ])

    def print_summary(self):
        trades = self.trade_history
        n = len(trades)
        if n == 0:
            print("\n  No completed trades today.")
            return

        wins = [t for t in trades if t["pnl_rs"] > 0]
        losses = [t for t in trades if t["pnl_rs"] <= 0]
        total = sum(t["pnl_rs"] for t in trades)
        win_rate = (len(wins) / n) * 100
        avg_win = sum(t["pnl_rs"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_rs"] for t in losses) / len(losses) if losses else 0
        rr_act = abs(avg_win / avg_loss) if avg_loss else 0

        print(f"\n{'='*60}")
        print(f"  END OF DAY REPORT")
        print(f"  {'Symbol':<12} {'Side':<6} {'Entry':>8} {'Exit':>8} {'PnL Rs':>9}  Reason")
        print(f"  {'-'*60}")
        for t in trades:
            icon = "✓" if t["pnl_rs"] > 0 else "✗"
            print(
                f"  {t['symbol']:<12} {t['side']:<6} "
                f"Rs.{t['entry']:>7.2f} Rs.{t['exit_price']:>7.2f} "
                f"{icon} Rs.{t['pnl_rs']:>+8.2f}  {t['exit_reason']}"
            )
        print(f"  {'-'*60}")
        print(f"  Trades   : {n}  |  Wins: {len(wins)}  |  Losses: {len(losses)}")
        print(f"  Win rate : {win_rate:.1f}%")
        print(f"  Avg win  : Rs.{avg_win:+.2f}  |  Avg loss: Rs.{avg_loss:+.2f}")
        print(f"  Actual RR: {rr_act:.2f}:1")
        print(f"  Total PnL: Rs.{total:+.2f}")
        print(f"  Capital  : Rs.{self.starting_balance:.2f} → Rs.{self.balance:.2f}")
        print(f"  Return   : {((self.balance - self.starting_balance) / self.starting_balance) * 100:+.2f}%")
        print(f"{'='*60}")

        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "starting_capital": self.starting_balance,
            "ending_capital": round(self.balance, 2),
            "total_pnl": round(total, 2),
            "return_pct": round(((self.balance - self.starting_balance) / self.starting_balance) * 100, 2),
            "trades_taken": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "actual_rr": round(rr_act, 2),
            "trades": [
                {
                    "symbol": t["symbol"],
                    "side": t["side"],
                    "entry": round(t["entry"], 2),
                    "exit": round(t["exit_price"], 2),
                    "qty": round(t["original_qty"], 4),
                    "pnl_pts": round(t["pnl_pts"], 2),
                    "pnl_rs": round(t["pnl_rs"], 2),
                    "reason": t["exit_reason"],
                    "hold_mins": t["hold_mins"],
                    "candle": t.get("candle_used", ""),
                }
                for t in trades
            ],
        }

        all_reports = []
        if os.path.exists(REPORT_JSON):
            with open(REPORT_JSON) as f:
                try:
                    all_reports = json.load(f)
                except Exception:
                    all_reports = []
        all_reports = [r for r in all_reports if r.get("date") != report["date"]]
        all_reports.append(report)
        with open(REPORT_JSON, "w") as f:
            json.dump(all_reports, f, indent=2)
        print(f"  Report saved: {REPORT_JSON}")


# ================================================================
# HTTP SESSION
# ================================================================
def make_session():
    return cffi_requests.Session(impersonate="chrome", verify=False)


# ================================================================
# CANDLE FETCHER
# ================================================================
def fetch_candles(symbol, session, period="5d", interval="5m"):
    """Fetch intraday candles with UTC→IST conversion."""
    ticker = f"{symbol}.NS" if not symbol.startswith("^") else symbol
    data = yf.download(
        ticker, period=period, interval=interval,
        progress=False, session=session
    )
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


def get_today_candles(symbol, session):
    """Get today's candles only."""
    data = fetch_candles(symbol, session, period="2d", interval="5m")
    if data is None or data.empty:
        return None
    today_str = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    today_data = data[data.index.strftime("%Y-%m-%d") == today_str]
    return today_data if not today_data.empty else None


def get_avg_volume(symbol, session):
    """Get average 5-min candle volume from prior days (for volume filter)."""
    data = fetch_candles(symbol, session, period="5d", interval="5m")
    if data is None or data.empty:
        return 0
    today_str = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    prior = data[data.index.strftime("%Y-%m-%d") != today_str]
    if prior.empty:
        return 0
    # Average volume of morning candles (9:15-10:30) on prior days
    morning = prior.between_time("09:15", "10:30")
    if morning.empty:
        return float(prior["Volume"].mean())
    return float(morning["Volume"].mean())


# ================================================================
# MARKET TREND FILTER — Check Nifty direction
# ================================================================
def get_market_bias(session):
    """
    Determines overall market direction using Nifty 50 index.
    Returns: "BULLISH", "BEARISH", or "NEUTRAL"

    Logic:
    - If Nifty's first candle is Open=Low → BULLISH day
    - If Nifty's first candle is Open=High → BEARISH day
    - If Nifty is green and above VWAP → BULLISH
    - If Nifty is red and below VWAP → BEARISH
    - Otherwise → NEUTRAL (take both directions)
    """
    candles = get_today_candles(NIFTY_INDEX, session)
    if candles is None or len(candles) < 2:
        return "NEUTRAL"

    c1 = candles.iloc[0]
    open_p = float(c1["Open"])
    high_p = float(c1["High"])
    low_p = float(c1["Low"])
    close_p = float(c1["Close"])

    # Check Nifty's own Open=Low/Open=High pattern
    if low_p > 0 and abs(open_p - low_p) / low_p <= 0.001:
        return "BULLISH"
    if high_p > 0 and abs(open_p - high_p) / high_p <= 0.001:
        return "BEARISH"

    # Fallback: is the market candle green or red?
    if close_p > open_p and (close_p - open_p) / open_p > 0.001:
        return "BULLISH"
    elif open_p > close_p and (open_p - close_p) / open_p > 0.001:
        return "BEARISH"

    return "NEUTRAL"


# ================================================================
# PATTERN CHECKS
# ================================================================
def is_open_equal_low(candle):
    """Open == Low → bullish signal (sellers absent at open)."""
    o = float(candle["Open"])
    l = float(candle["Low"])
    if l == 0:
        return False
    return abs(o - l) / l <= OPEN_HL_TOLERANCE


def is_open_equal_high(candle):
    """Open == High → bearish signal (buyers absent at open)."""
    o = float(candle["Open"])
    h = float(candle["High"])
    if h == 0:
        return False
    return abs(o - h) / h <= OPEN_HL_TOLERANCE


def is_marubozu(candle):
    """Body fills >85% of range — extreme momentum candle."""
    body = abs(float(candle["Close"]) - float(candle["Open"]))
    rng = float(candle["High"]) - float(candle["Low"])
    if rng == 0:
        return False
    return (body / rng) >= MARUBOZU_THRESHOLD


def has_minimum_range(candle):
    """
    Filter out tiny/flat candles. The candle must have a range
    of at least MIN_CANDLE_RANGE_PCT of the open price.
    """
    rng = float(candle["High"]) - float(candle["Low"])
    open_p = float(candle["Open"])
    if open_p == 0:
        return False
    return (rng / open_p) >= MIN_CANDLE_RANGE_PCT


def is_confirmation_candle_valid(c1, c2, signal_type):
    """
    CONFIRMATION: The 2nd candle must confirm the direction.
    - For LONG: 2nd candle should close above the 1st candle's high
      (buyers are in control, price broke above resistance)
    - For SHORT: 2nd candle should close below the 1st candle's low
      (sellers are in control, price broke below support)

    This single filter eliminates the majority of false signals
    where the opening pattern didn't hold.
    """
    c2_close = float(c2["Close"])
    c1_high = float(c1["High"])
    c1_low = float(c1["Low"])

    if signal_type == "LONG":
        return c2_close > c1_high
    else:  # SHORT
        return c2_close < c1_low


# ================================================================
# POSITION SIZING — with hard Rs.100 risk cap
# ================================================================
def calculate_position(capital, entry, sl, side):
    """
    Calculates position size ensuring max loss = Rs.100 (1% of 10K).
    If the stop distance is too large, qty is reduced (never increased).

    Returns: (qty, stock_risk, target1, target2)
    """
    stock_risk = abs(entry - sl)
    if stock_risk <= 0:
        return 0.0, 0.0, 0.0, 0.0

    rupee_risk = min(capital * RISK_PCT, MAX_RISK_PER_TRADE)
    qty = rupee_risk / stock_risk

    # Sanity check: don't take more than 50% of capital in a single trade
    position_value = qty * entry
    if position_value > capital * 0.5:
        qty = (capital * 0.5) / entry

    if side == "LONG":
        target1 = entry + TARGET_1_RR * stock_risk
        target2 = entry + TARGET_2_RR * stock_risk
    else:
        target1 = entry - TARGET_1_RR * stock_risk
        target2 = entry - TARGET_2_RR * stock_risk

    return round(qty, 4), round(stock_risk, 2), round(target1, 2), round(target2, 2)


# ================================================================
# SCAN — find confirmed signals
# ================================================================
def scan_for_signals(watchlist, session, market_bias):
    """
    Scans for Open=Low/Open=High signals with CONFIRMATION.
    Requires both the 1st and 2nd 5-min candles to be available.

    Filters applied:
    1. Open=Low or Open=High on 1st candle
    2. Minimum candle range (no flat/doji candles)
    3. 2nd candle confirms direction (closes above c1 high / below c1 low)
    4. Volume filter (1st candle volume > average)
    5. Market trend alignment (skip counter-trend signals)
    6. Price range filter
    7. NOT a marubozu (or if marubozu, use 2nd as signal, 3rd as confirm)
    """
    signals = []
    print(f"\n  Scanning {len(watchlist)} stocks | Market bias: {market_bias}")
    print(f"  {'Symbol':<14} {'Pattern':<14} {'Status':<30}")
    print(f"  {'-'*60}")

    for symbol in watchlist:
        try:
            time.sleep(0.15)  # Rate limiting
            candles = get_today_candles(symbol, session)
            if candles is None or len(candles) < 2:
                continue

            # Get first candle (9:15)
            c1_df = candles.between_time("09:15", "09:19")
            if c1_df.empty:
                c1_df = candles.head(1)
            if c1_df.empty:
                continue
            c1 = c1_df.iloc[0]

            # Price range check
            close_price = float(c1["Close"])
            if close_price < PRICE_FLOOR or close_price > PRICE_CAP:
                continue

            # ── Filter 1: Detect Open=Low or Open=High pattern ──
            signal_type = None
            if is_open_equal_low(c1):
                signal_type = "LONG"
            elif is_open_equal_high(c1):
                signal_type = "SHORT"

            if signal_type is None:
                continue  # No pattern — skip silently

            pattern_str = "Open=Low" if signal_type == "LONG" else "Open=High"

            # ── Filter 2: Minimum candle range ──
            if not has_minimum_range(c1):
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: candle range too small")
                continue

            # ── Filter 3: Market trend alignment ──
            if market_bias == "BULLISH" and signal_type == "SHORT":
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: counter-trend (market bullish)")
                continue
            if market_bias == "BEARISH" and signal_type == "LONG":
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: counter-trend (market bearish)")
                continue

            # ── Filter 4: Volume check ──
            c1_volume = float(c1["Volume"]) if "Volume" in c1.index else 0
            avg_vol = get_avg_volume(symbol, session)
            if avg_vol > 0 and c1_volume < avg_vol * MIN_VOLUME_RATIO:
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: low volume ({c1_volume:.0f} < {avg_vol*MIN_VOLUME_RATIO:.0f})")
                continue

            # ── Filter 5: Marubozu handling ──
            # If 1st candle is marubozu, the move is already extended.
            # Use 2nd candle for signal, 3rd for confirmation.
            if is_marubozu(c1):
                if len(candles) < 3:
                    print(f"  {symbol:<14} {pattern_str:<14} SKIP: marubozu, awaiting 3rd candle")
                    continue
                # Shift: use c2 as "signal candle", c3 as "confirmation"
                c2_df = candles.between_time("09:20", "09:24")
                if c2_df.empty and len(candles) >= 2:
                    c2_df = candles.iloc[[1]]
                c3_df = candles.between_time("09:25", "09:29")
                if c3_df.empty and len(candles) >= 3:
                    c3_df = candles.iloc[[2]]
                if c2_df.empty or c3_df.empty:
                    continue
                signal_candle = c2_df.iloc[0]
                confirm_candle = c3_df.iloc[0]
                candle_label = "marubozu: 2nd signal, 3rd confirm"
            else:
                # Normal case: 1st is signal, 2nd is confirmation
                c2_df = candles.between_time("09:20", "09:24")
                if c2_df.empty and len(candles) >= 2:
                    c2_df = candles.iloc[[1]]
                if c2_df.empty:
                    print(f"  {symbol:<14} {pattern_str:<14} SKIP: no 2nd candle yet")
                    continue
                signal_candle = c1
                confirm_candle = c2_df.iloc[0]
                candle_label = "1st signal, 2nd confirm"

            # ── Filter 6: Confirmation candle check ──
            if not is_confirmation_candle_valid(signal_candle, confirm_candle, signal_type):
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: 2nd candle did NOT confirm")
                continue

            # ── Calculate entry, SL, targets ──
            entry = float(confirm_candle["Close"])
            # SL is placed at the low of signal candle (LONG) or high (SHORT)
            if signal_type == "LONG":
                sl = float(signal_candle["Low"])
            else:
                sl = float(signal_candle["High"])

            # Skip if entry and SL are too close (avoid whipsaw)
            if abs(entry - sl) < 0.01:
                continue

            # Skip if SL distance is more than 3% of entry (too risky)
            sl_pct = abs(entry - sl) / entry
            if sl_pct > 0.03:
                print(f"  {symbol:<14} {pattern_str:<14} SKIP: SL too wide ({sl_pct*100:.1f}%)")
                continue

            qty, stock_risk, target1, target2 = calculate_position(
                PAPER_CAPITAL, entry, sl, signal_type
            )
            if qty <= 0:
                continue

            print(
                f"  {symbol:<14} {pattern_str:<14} "
                f"✓ CONFIRMED | Entry Rs.{entry:.2f} SL Rs.{sl:.2f} "
                f"T1 Rs.{target1:.2f} T2 Rs.{target2:.2f}"
            )

            signals.append({
                "symbol": symbol,
                "signal_type": signal_type,
                "entry": entry,
                "sl": sl,
                "target1": target1,
                "target2": target2,
                "qty": qty,
                "stock_risk": stock_risk,
                "candle_label": candle_label,
                "volume_ratio": c1_volume / avg_vol if avg_vol > 0 else 1.0,
            })

        except Exception as e:
            print(f"  {symbol:<14} {'ERROR':<14} {e}")

    print(f"\n  Found {len(signals)} confirmed signal(s)")

    # Sort by: higher volume ratio first (strongest conviction)
    signals.sort(key=lambda x: x["volume_ratio"], reverse=True)
    return signals


# ================================================================
# DEFENSE — monitor positions with trailing stop logic
# ================================================================
def defense_loop(broker: PaperBroker, session):
    """
    Check all open positions for exit conditions.
    Implements:
    - Target 1 hit → partial exit (50%), SL to breakeven
    - Target 2 hit → full exit
    - Stop loss hit → full exit
    - Trailing stop (SL moves to breakeven after +1R)
    - Force exit at 3:10 PM
    """
    if not broker.open_positions:
        return 0, 0.0

    closed_count = 0
    cycle_pnl = 0.0
    symbols = list(broker.open_positions.keys())

    for symbol in symbols:
        pos = broker.open_positions.get(symbol)
        if not pos:
            continue

        side = pos["side"]

        # Fetch latest 1-min data for tighter monitoring
        data = fetch_candles(symbol, session, period="1d", interval="1m")
        if data is None or data.empty:
            print(f"  {symbol}: no data — skip")
            continue
        today_str = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
        data = data[data.index.strftime("%Y-%m-%d") == today_str]
        if data.empty:
            continue

        # Use the LAST candle's high and low to check intracandle triggers
        last_candle = data.iloc[-1]
        price = float(last_candle["Close"])
        high = float(last_candle["High"])
        low = float(last_candle["Low"])

        # Calculate unrealised PnL
        if side == "LONG":
            unrlsd = (price - pos["entry"]) * pos["qty"]
        else:
            unrlsd = (pos["entry"] - price) * pos["qty"]

        print(
            f"  {symbol:<12} [{side:<5}] LTP Rs.{price:.2f} | "
            f"Entry Rs.{pos['entry']:.2f} | SL Rs.{pos['sl']:.2f} | "
            f"T1 Rs.{pos['target1']:.2f} | T2 Rs.{pos['target2']:.2f} | "
            f"Unrlsd Rs.{unrlsd:+.2f}"
        )

        exit_reason = ""
        do_partial = False

        if side == "LONG":
            # Check if SL was breached (use candle low for accuracy)
            if low <= pos["sl"]:
                exit_reason = "STOP LOSS HIT"
            # Check Target 2 first (full exit)
            elif high >= pos["target2"]:
                exit_reason = "TARGET 2 HIT"
            # Check Target 1 (partial exit)
            elif not pos["partial_exited"] and high >= pos["target1"]:
                do_partial = True
                exit_reason = "TARGET 1 HIT"
            # Trailing stop: move SL to breakeven at +1R
            elif not pos["trailing_active"]:
                move_in_favor = price - pos["entry"]
                if move_in_favor >= pos["risk_pts"]:
                    pos["sl"] = pos["entry"]
                    pos["trailing_active"] = True
                    print(f"     → Trailing SL activated: SL moved to Rs.{pos['sl']:.2f} (breakeven)")
        else:  # SHORT
            if high >= pos["sl"]:
                exit_reason = "STOP LOSS HIT"
            elif low <= pos["target2"]:
                exit_reason = "TARGET 2 HIT"
            elif not pos["partial_exited"] and low <= pos["target1"]:
                do_partial = True
                exit_reason = "TARGET 1 HIT"
            elif not pos["trailing_active"]:
                move_in_favor = pos["entry"] - price
                if move_in_favor >= pos["risk_pts"]:
                    pos["sl"] = pos["entry"]
                    pos["trailing_active"] = True
                    print(f"     → Trailing SL activated: SL moved to Rs.{pos['sl']:.2f} (breakeven)")

        # Force exit at time limit
        if not exit_reason and datetime.now().time() >= FORCE_EXIT_TIME:
            exit_reason = "TIME EXIT (3:10 PM)"

        if exit_reason:
            if do_partial:
                # Partial exit at target 1
                pnl = broker.exit_position(symbol, price, exit_reason, partial=True)
            else:
                # Full exit
                pnl = broker.exit_position(symbol, price, exit_reason, partial=False)
                closed_count += 1
            cycle_pnl += pnl

    return closed_count, cycle_pnl


# ================================================================
# MAIN LOOP
# ================================================================
if __name__ == "__main__":

    broker = PaperBroker(starting_balance=PAPER_CAPITAL)
    session = make_session()
    watchlist = get_watchlist(index="BOTH")

    trades_done = 0
    daily_pnl = 0.0
    MAX_DAILY_LOSS = PAPER_CAPITAL * MAX_DAILY_LOSS_PCT

    # ── Wait for 9:25 AM (need at least 2 candles: 9:15 + 9:20) ──
    wait_time = dtime(9, 25)
    print(f"\n  Waiting for 9:25 AM (need 1st + 2nd candle for confirmation)...")
    while datetime.now().time() < wait_time:
        remaining = (
            datetime.combine(datetime.today(), wait_time) - datetime.now()
        ).seconds
        print(f"  {datetime.now().strftime('%H:%M:%S')} — {remaining}s remaining...")
        time.sleep(15)

    print(f"\n  Two candles available. Bot is LIVE in PAPER MODE.\n")

    # ── Check market bias first ──
    print(f"[{datetime.now().strftime('%H:%M:%S')}] CHECKING MARKET BIAS...")
    market_bias = get_market_bias(session)
    print(f"  Market bias: {market_bias}")
    if market_bias == "BULLISH":
        print("  → Will prefer LONG signals, skip SHORT signals")
    elif market_bias == "BEARISH":
        print("  → Will prefer SHORT signals, skip LONG signals")
    else:
        print("  → NEUTRAL — will take both directions")

    # ── Morning scan ──
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] SCANNING FOR CONFIRMED SIGNALS...")
    signals = scan_for_signals(watchlist, session, market_bias)

    # ── Enter top signals (up to MAX_TRADES_PER_DAY) ──
    entered = 0
    for sig in signals:
        if entered >= MAX_TRADES_PER_DAY:
            print(f"\n  Max {MAX_TRADES_PER_DAY} trades reached — skipping rest")
            break
        if sig["symbol"] in broker.open_positions:
            continue

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ENTERING: {sig['symbol']} [{sig['signal_type']}]")
        broker.place_order(
            symbol=sig["symbol"],
            side=sig["signal_type"],
            qty=sig["qty"],
            entry=sig["entry"],
            sl=sig["sl"],
            target1=sig["target1"],
            target2=sig["target2"],
            risk_pts=sig["stock_risk"],
            candle_label=sig["candle_label"],
        )
        entered += 1

    if entered == 0:
        print(f"\n  No confirmed signals found. Will monitor until market close.")

    # ── Defense loop — monitor all positions ──
    try:
        while True:
            now = datetime.now().time()

            # Kill switches
            if now >= MARKET_CLOSE:
                print("\n  3:30 PM — Market closed.")
                break

            if trades_done >= MAX_TRADES_PER_DAY and not broker.open_positions:
                print(f"\n  All {MAX_TRADES_PER_DAY} trades completed.")
                break

            if daily_pnl <= -MAX_DAILY_LOSS:
                print(f"\n  ⚠ Daily loss limit hit (Rs.{daily_pnl:.2f}) — closing all positions")
                for sym in list(broker.open_positions.keys()):
                    broker.exit_position(sym, broker.open_positions[sym]["entry"], "DAILY LOSS LIMIT")
                break

            if broker.open_positions:
                n = len(broker.open_positions)
                syms = ", ".join(broker.open_positions.keys())
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] MONITORING — {n} position(s): {syms}")
                closed, pnl = defense_loop(broker, session)
                if closed > 0:
                    trades_done += closed
                    daily_pnl += pnl
                    print(f"\n  Closed: {closed} | Cycle PnL: Rs.{pnl:+.2f} | "
                          f"Day PnL: Rs.{daily_pnl:+.2f} | "
                          f"Trades: {trades_done}/{MAX_TRADES_PER_DAY}")
                elif pnl != 0:
                    # Partial exits contribute to daily PnL
                    daily_pnl += pnl
            else:
                if trades_done > 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] All positions closed. "
                          f"Day PnL: Rs.{daily_pnl:+.2f}")
                else:
                    # No trades and no positions — check for late entries
                    if now < ENTRY_WINDOW_END:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] No positions. "
                              f"Re-scanning for late entries...")
                        signals = scan_for_signals(watchlist, session, market_bias)
                        for sig in signals:
                            if entered >= MAX_TRADES_PER_DAY:
                                break
                            if sig["symbol"] in broker.open_positions:
                                continue
                            broker.place_order(
                                symbol=sig["symbol"],
                                side=sig["signal_type"],
                                qty=sig["qty"],
                                entry=sig["entry"],
                                sl=sig["sl"],
                                target1=sig["target1"],
                                target2=sig["target2"],
                                risk_pts=sig["stock_risk"],
                                candle_label=sig["candle_label"],
                            )
                            entered += 1
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"Past entry window. Waiting for close...")

            time.sleep(DEFENSE_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\n  Stopped manually (Ctrl+C).")
        if broker.open_positions:
            print("  Force-closing remaining positions...")
            for sym in list(broker.open_positions.keys()):
                pos = broker.open_positions[sym]
                broker.exit_position(sym, pos["entry"], "MANUAL STOP")

    finally:
        broker.print_summary()
