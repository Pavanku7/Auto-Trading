"""
================================================================
  PAPER TRADING v4 — Open=Low / Open=High + 3 Indicator Filters
  Based on manual trading strategy from analysis sheet
================================================================

  YOUR CORE STRATEGY (from sheet)
  ─────────────────────────────────────────────
  At 9:15 AM, check the FIRST 5-min candle:
    BUY  signal  →  Open == Low
    SELL signal  →  Open == High

  ENTRY  = 1st candle Close (or 2nd if marubozu)
  SL     = 1st candle Low (buy) / High (sell)
  Risk   = |Entry - SL|
  Qty    = (Capital × 1%) / Stock Risk
  Target = Entry + 2×Risk (buy) / Entry - 2×Risk (sell)

  Sheet examples:
    Buy:  Entry 3698.9, SL 3634.9, Risk 64, Qty 1.56, Target 3826.9
    Sell: Entry 94.1,   SL 99,     Risk 4.9, Qty 20,  Exit 77.65

  3 CONFIRMATION INDICATORS (new filters)
  ─────────────────────────────────────────────
  A trade is ONLY taken when the Open=Low/High pattern passes
  ALL 3 indicator checks. This dramatically reduces false signals.

  1. VWAP (Volume Weighted Average Price)
     ────────────────────────────────────
     - BUY only if current price > VWAP (buyers dominating)
     - SELL only if current price < VWAP (sellers dominating)
     - Why: VWAP is where institutions trade. Being on the right
       side means you're aligned with smart money flow.

  2. EMA 9 / EMA 21 Crossover (5-min chart)
     ────────────────────────────────────
     - BUY only if EMA9 > EMA21 (short-term momentum is UP)
     - SELL only if EMA9 < EMA21 (short-term momentum is DOWN)
     - Why: Catches the intraday trend. If momentum is against
       your signal, the Open=Low/High pattern is likely to fail.

  3. RSI (14-period) — Momentum Confirmation
     ────────────────────────────────────
     - BUY only if 50 < RSI < 80 (bullish, not overbought)
     - SELL only if 20 < RSI < 50 (bearish, not oversold)
     - Why: Confirms strength behind the move. Avoids entries
       at exhaustion (RSI > 80 = reversal likely).

  TRAILING STOPLOSS (from your sheet)
  ─────────────────────────────────────────────
  Your sheet shows a trailing table where SL tightens as CMP rises.
  Implementation: After +1R profit, SL trails at 50% of risk behind price.
    Example: Risk=20, CMP moves +20 (1R) → trail activates
    CMP 120 → SL = 120 - 10 = 110
    CMP 125 → SL = 125 - 10 = 115 (keeps following)

  EXIT RULES
  ─────────────────────────────────────────────
  1. Stop-loss hit (full exit)
  2. Target hit (2:1 R:R from your sheet)
  3. Trailing stop hit (after 1R move)
  4. Force exit at 3:10 PM
================================================================
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os
import sys
import json
import csv
import time
from dotenv import load_dotenv
from datetime import datetime, time as dtime
import urllib3
from curl_cffi import requests as cffi_requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows consoles default to cp1252 and crash on the ┌─✓ box characters
# this script prints. Force UTF-8 so the pretty logs render everywhere.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "config.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

# ================================================================
# CONFIG — matching your sheet exactly
# ================================================================
PAPER_CAPITAL       = 10000     # Capital (from sheet)
RISK_PCT            = 0.01      # 1% risk per trade = Rs.100 (from sheet)
RR_RATIO            = 2.0       # Target = 2× Risk (from sheet: "stock risk * 2")
MAX_TRADES_PER_DAY  = 3
MAX_DAILY_LOSS_PCT  = 0.03      # Kill switch at 3% (Rs.300)
PRICE_CAP           = 5000      # Max stock price
PRICE_FLOOR         = 50        # Min stock price

# Open=Low/High tolerance (0.1%)
OPEN_HL_TOLERANCE   = 0.001

# Marubozu: body > 85% of range → use 2nd candle (from your sheet note)
MARUBOZU_THRESHOLD  = 0.85

# ─── INDICATOR SETTINGS ───
EMA_FAST            = 9         # Fast EMA period
EMA_SLOW            = 21        # Slow EMA period
RSI_PERIOD          = 14        # RSI lookback
RSI_BUY_MIN         = 50        # RSI floor for BUY entries
RSI_BUY_MAX         = 80        # RSI ceiling for BUY (avoid overbought)
RSI_SELL_MAX        = 50        # RSI ceiling for SELL entries
RSI_SELL_MIN        = 20        # RSI floor for SELL (avoid oversold)

# ─── TRAILING STOPLOSS (from your sheet's trailing table) ───
TRAIL_ACTIVATION_R  = 1.0       # Activate trailing after +1R profit
TRAIL_GAP_FACTOR    = 0.5       # Trail at 50% of risk behind CMP

# ─── TIMING ───
ENTRY_WINDOW_START  = dtime(9, 20)    # After 1st candle closes
ENTRY_WINDOW_END    = dtime(10, 30)   # Stop new entries after this
FORCE_EXIT_TIME     = dtime(15, 10)   # Force close at 3:10 PM
MARKET_CLOSE        = dtime(15, 30)

# ─── EXECUTION ───
DEFENSE_INTERVAL_SEC = 30       # Check positions every 30 seconds
SLIPPAGE_PCT        = 0.0005    # 0.05% slippage
# Realistic Groww intraday round-trip cost for a ~Rs.3-5k order:
# brokerage min(Rs.20, 0.1%) per leg + STT/GST/exchange/SEBI/stamp.
# ~Rs.12 round-trip. (Was Rs.40 — far too high, it single-handedly
# turned a break-even strategy into a guaranteed loser. See backtest_v4.py.)
FLAT_FEE_RS         = 12.0      # Brokerage + taxes, round trip

# ─── FILES ───
LOG_CSV     = os.path.join(BASE_DIR, "paper_trade_v4_log.csv")
REPORT_JSON = os.path.join(BASE_DIR, "paper_trade_v4_report.json")

# ─── MARKET INDEX ───
NIFTY_INDEX = "^NSEI"


# ================================================================
# WATCHLIST — Nifty 50 + Nifty Next 50 (same as your sheet)
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
    print(f"  Watchlist: {index} — {len(symbols)} stocks")
    return symbols


# ================================================================
# TECHNICAL INDICATORS — VWAP, EMA, RSI
# ================================================================
def compute_vwap(df):
    """
    VWAP = Cumulative(Typical Price × Volume) / Cumulative(Volume)
    Typical Price = (High + Low + Close) / 3
    Resets each day (intraday indicator).
    """
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cum_tp_vol = (tp * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum()
    # Avoid division by zero
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap.fillna(method="ffill")


def compute_ema(series, period):
    """Exponential Moving Average — reacts faster than SMA."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series, period=14):
    """
    RSI = 100 - (100 / (1 + RS))
    RS  = Avg Gain / Avg Loss over `period` bars
    Uses Wilder's smoothing (EMA-based).
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def get_indicators(all_candles, today_candles):
    """
    Compute VWAP (today only), EMA9/21 and RSI (multi-day for smoothness).
    Returns latest values or None if insufficient data.
    """
    if all_candles is None or len(all_candles) < EMA_SLOW + RSI_PERIOD:
        return None
    if today_candles is None or today_candles.empty:
        return None

    # VWAP — computed on today's data only (resets daily)
    vwap_series = compute_vwap(today_candles)
    vwap = float(vwap_series.iloc[-1]) if not vwap_series.empty else None

    # EMA and RSI — use multi-day data for accuracy
    close = all_candles["Close"]
    ema9 = compute_ema(close, EMA_FAST)
    ema21 = compute_ema(close, EMA_SLOW)
    rsi = compute_rsi(close, RSI_PERIOD)

    latest_idx = all_candles.index[-1]

    return {
        "vwap": vwap if vwap and not np.isnan(vwap) else float(close.iloc[-1]),
        "ema9": float(ema9.iloc[-1]),
        "ema21": float(ema21.iloc[-1]),
        "rsi": float(rsi.iloc[-1]),
        "close": float(close.iloc[-1]),
    }


def check_indicators(indicators, signal_type):
    """
    ALL 3 indicators must confirm the signal direction.

    BUY requires:
      ✓ Price > VWAP  (institutional buyers present)
      ✓ EMA9 > EMA21  (upward momentum)
      ✓ 50 < RSI < 80 (bullish strength, not exhausted)

    SELL requires:
      ✓ Price < VWAP  (institutional sellers present)
      ✓ EMA9 < EMA21  (downward momentum)
      ✓ 20 < RSI < 50 (bearish strength, not exhausted)

    Returns: (passed, reason_string)
    """
    if indicators is None:
        return False, "no indicator data"

    close = indicators["close"]
    vwap = indicators["vwap"]
    ema9 = indicators["ema9"]
    ema21 = indicators["ema21"]
    rsi = indicators["rsi"]

    if signal_type == "LONG":
        if close <= vwap:
            return False, f"VWAP✗ (price {close:.1f} < VWAP {vwap:.1f})"
        if ema9 <= ema21:
            return False, f"EMA✗ (EMA9 {ema9:.1f} < EMA21 {ema21:.1f})"
        if rsi <= RSI_BUY_MIN:
            return False, f"RSI✗ ({rsi:.0f} < {RSI_BUY_MIN}, weak momentum)"
        if rsi >= RSI_BUY_MAX:
            return False, f"RSI✗ ({rsi:.0f} > {RSI_BUY_MAX}, overbought)"
        return True, f"VWAP✓ EMA✓ RSI({rsi:.0f})✓"

    else:  # SHORT
        if close >= vwap:
            return False, f"VWAP✗ (price {close:.1f} > VWAP {vwap:.1f})"
        if ema9 >= ema21:
            return False, f"EMA✗ (EMA9 {ema9:.1f} > EMA21 {ema21:.1f})"
        if rsi >= RSI_SELL_MAX:
            return False, f"RSI✗ ({rsi:.0f} > {RSI_SELL_MAX}, still bullish)"
        if rsi <= RSI_SELL_MIN:
            return False, f"RSI✗ ({rsi:.0f} < {RSI_SELL_MIN}, oversold)"
        return True, f"VWAP✓ EMA✓ RSI({rsi:.0f})✓"


# ================================================================
# HTTP SESSION
# ================================================================
def make_session():
    return cffi_requests.Session(impersonate="chrome", verify=False)


# ================================================================
# DATA FETCHER
# ================================================================
def fetch_candles(symbol, session, period="5d", interval="5m"):
    """Fetch candle data with UTC→IST conversion."""
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


def get_today_candles_from(all_data):
    """Extract today's candles from multi-day data."""
    if all_data is None or all_data.empty:
        return None
    today_str = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    today = all_data[all_data.index.strftime("%Y-%m-%d") == today_str]
    return today if not today.empty else None


# ================================================================
# PATTERN CHECKS (from your sheet)
# ================================================================
def is_open_equal_low(candle):
    """Open == Low → BUY (from sheet: 'Open = Low buying strategy')"""
    o = float(candle["Open"])
    l = float(candle["Low"])
    if l == 0:
        return False
    return abs(o - l) / l <= OPEN_HL_TOLERANCE


def is_open_equal_high(candle):
    """Open == High → SELL (from sheet: 'Open = High selling strategy')"""
    o = float(candle["Open"])
    h = float(candle["High"])
    if h == 0:
        return False
    return abs(o - h) / h <= OPEN_HL_TOLERANCE


def is_marubozu(candle):
    """
    Very long candle with minimal wicks (body > 85% of range).
    From your sheet note: 'If 1st candle is very long, open=high
    and marubozu then pick the 2nd candle for safer side.'
    """
    body = abs(float(candle["Close"]) - float(candle["Open"]))
    rng = float(candle["High"]) - float(candle["Low"])
    if rng == 0:
        return False
    return (body / rng) >= MARUBOZU_THRESHOLD


# ================================================================
# POSITION SIZING (exactly from your sheet)
# ================================================================
def calculate_position(capital, entry, sl, side):
    """
    Your sheet formula:
      Capital = 10000
      RISK = 100 (1% of capital)
      Stock Risk = Entry - SL  (or SL - Entry for sell)
      QTY = RISK / Stock Risk
      Actual Target = Entry + (Stock Risk × 2)

    Example from sheet:
      Entry 285.2, SL 281.8 → Risk 3.4 → Qty 29.4 → Target 292
    """
    stock_risk = abs(entry - sl)
    if stock_risk <= 0:
        return 0.0, 0.0, 0.0

    rupee_risk = capital * RISK_PCT  # Rs.100
    qty = rupee_risk / stock_risk

    # Safety: cap position value at 50% of capital
    if qty * entry > capital * 0.5:
        qty = (capital * 0.5) / entry

    if side == "LONG":
        target = entry + (stock_risk * RR_RATIO)
    else:
        target = entry - (stock_risk * RR_RATIO)

    return round(qty, 4), round(stock_risk, 2), round(target, 2)


# ================================================================
# PAPER BROKER
# ================================================================
class PaperBroker:
    def __init__(self, starting_balance: float):
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.open_positions = {}
        self.trade_history = []
        self.order_count = 0

        # Create CSV header if new file
        if not os.path.exists(LOG_CSV):
            with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "date", "time", "symbol", "side", "action",
                    "qty", "entry", "sl", "target",
                    "exit_price", "exit_reason",
                    "risk_pts", "pnl_pts", "pnl_rs",
                    "balance_after", "vwap", "ema9", "ema21", "rsi",
                ])

        print(f"\n{'='*64}")
        print(f"  PAPER TRADING v4 — Open=Low/High + VWAP + EMA(9/21) + RSI(14)")
        print(f"{'='*64}")
        print(f"  Capital      : Rs.{self.balance:,.2f}")
        print(f"  Risk/trade   : {RISK_PCT*100:.0f}% = Rs.{self.balance * RISK_PCT:.0f}")
        print(f"  R:R target   : {RR_RATIO}:1 (from your sheet)")
        print(f"  Indicators   : VWAP + EMA(9/21) + RSI(14)")
        print(f"  Trailing SL  : After +{TRAIL_ACTIVATION_R}R, trail at {TRAIL_GAP_FACTOR*100:.0f}% risk gap")
        print(f"  Entry window : 9:20 – 10:30 AM")
        print(f"  Force exit   : 3:10 PM")
        print(f"  Max trades   : {MAX_TRADES_PER_DAY}/day")
        print(f"  Log file     : {LOG_CSV}")
        print(f"  NO REAL ORDERS — paper mode only")
        print(f"{'='*64}\n")

    def place_order(self, symbol, side, qty, entry, sl, target,
                    risk_pts, candle_label, indicators):
        """Open a paper position (BUY or SHORT)."""
        self.order_count += 1
        now = datetime.now()

        # Entry slippage
        if side == "LONG":
            actual_entry = entry * (1 + SLIPPAGE_PCT)
        else:
            actual_entry = entry * (1 - SLIPPAGE_PCT)

        self.open_positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry": actual_entry,
            "original_entry": entry,
            "sl": sl,
            "target": target,
            "risk_pts": risk_pts,
            "entry_time": now,
            "candle_used": candle_label,
            "indicators": indicators,
            "trailing_active": False,
            "highest_price": actual_entry,  # For LONG trailing
            "lowest_price": actual_entry,   # For SHORT trailing
        }

        side_str = "BUY" if side == "LONG" else "SHORT"
        print(f"\n  ┌─ PAPER {side_str} #{self.order_count} ──────────────────────────")
        print(f"  │ {symbol}  ({candle_label})")
        print(f"  │ Entry  : Rs.{actual_entry:.2f}  (slippage applied)")
        print(f"  │ SL     : Rs.{sl:.2f}  |  Risk: Rs.{risk_pts:.2f}/share")
        print(f"  │ Target : Rs.{target:.2f}  ({RR_RATIO:.0f}:1 R:R)")
        print(f"  │ Qty    : {qty:.4f}  |  Max Loss: Rs.{risk_pts * qty:.2f}")
        if indicators:
            print(f"  │ VWAP={indicators['vwap']:.2f}  "
                  f"EMA9={indicators['ema9']:.2f}  "
                  f"EMA21={indicators['ema21']:.2f}  "
                  f"RSI={indicators['rsi']:.1f}")
        print(f"  │ Time   : {now.strftime('%H:%M:%S')}")
        print(f"  └────────────────────────────────────────────────")

        self._log(symbol, side, "ENTRY", qty, actual_entry, sl, target,
                  None, None, risk_pts, None, None, indicators)

    def exit_position(self, symbol, exit_price, exit_reason):
        """Close position and calculate net PnL."""
        pos = self.open_positions.get(symbol)
        if not pos:
            return 0.0

        side = pos["side"]
        qty = pos["qty"]

        # Exit slippage
        if side == "LONG":
            actual_exit = exit_price * (1 - SLIPPAGE_PCT)
            pnl_pts = actual_exit - pos["entry"]
        else:
            actual_exit = exit_price * (1 + SLIPPAGE_PCT)
            pnl_pts = pos["entry"] - actual_exit

        gross_pnl = pnl_pts * qty
        net_pnl = gross_pnl - FLAT_FEE_RS
        self.balance += net_pnl
        hold_min = int((datetime.now() - pos["entry_time"]).total_seconds() / 60)

        result = "✓ PROFIT" if net_pnl > 0 else "✗ LOSS"
        print(f"\n  ┌─ EXIT — {result} ────────────────────────────")
        print(f"  │ {symbol} [{side}]")
        print(f"  │ Entry  : Rs.{pos['entry']:.2f}")
        print(f"  │ Exit   : Rs.{actual_exit:.2f}  ({exit_reason})")
        print(f"  │ PnL    : {pnl_pts:+.2f} pts × {qty:.4f} = Rs.{net_pnl:+.2f}")
        print(f"  │ Hold   : {hold_min} min")
        print(f"  │ Balance: Rs.{self.balance:,.2f}")
        if pos["trailing_active"]:
            print(f"  │ (Trailing SL was active)")
        print(f"  └────────────────────────────────────────────────")

        self.trade_history.append({
            **pos,
            "exit_price": actual_exit,
            "exit_reason": exit_reason,
            "pnl_pts": pnl_pts,
            "pnl_rs": net_pnl,
            "hold_mins": hold_min,
        })

        self._log(symbol, side, "EXIT", qty, pos["entry"], pos["sl"],
                  pos["target"], actual_exit, exit_reason,
                  pos["risk_pts"], pnl_pts, net_pnl, pos["indicators"])
        del self.open_positions[symbol]
        return net_pnl

    def _log(self, symbol, side, action, qty, entry, sl, target,
             exit_price, exit_reason, risk_pts, pnl_pts, pnl_rs, indicators):
        now = datetime.now()
        vwap_val = indicators.get("vwap", "") if indicators else ""
        ema9_val = indicators.get("ema9", "") if indicators else ""
        ema21_val = indicators.get("ema21", "") if indicators else ""
        rsi_val = indicators.get("rsi", "") if indicators else ""

        with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                symbol, side, action,
                round(qty, 4),
                round(entry, 2),
                round(sl, 2) if sl is not None else "",
                round(target, 2) if target is not None else "",
                round(exit_price, 2) if exit_price is not None else "",
                exit_reason or "",
                round(risk_pts, 2) if risk_pts is not None else "",
                round(pnl_pts, 2) if pnl_pts is not None else "",
                round(pnl_rs, 2) if pnl_rs is not None else "",
                round(self.balance, 2),
                round(vwap_val, 2) if isinstance(vwap_val, (int, float)) else "",
                round(ema9_val, 2) if isinstance(ema9_val, (int, float)) else "",
                round(ema21_val, 2) if isinstance(ema21_val, (int, float)) else "",
                round(rsi_val, 1) if isinstance(rsi_val, (int, float)) else "",
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

        print(f"\n{'='*64}")
        print(f"  DAY REPORT — Open=Low/High + VWAP + EMA + RSI")
        print(f"{'='*64}")
        print(f"  {'Symbol':<12} {'Side':<6} {'Entry':>8} {'Exit':>8} {'PnL':>10}  Reason")
        print(f"  {'-'*62}")
        for t in trades:
            icon = "✓" if t["pnl_rs"] > 0 else "✗"
            print(
                f"  {t['symbol']:<12} {t['side']:<6} "
                f"{t['entry']:>8.2f} {t['exit_price']:>8.2f} "
                f"{icon} Rs.{t['pnl_rs']:>+7.2f}  {t['exit_reason']}"
            )
        print(f"  {'-'*62}")
        print(f"  Trades     : {n}  |  Wins: {len(wins)}  |  Losses: {len(losses)}")
        print(f"  Win Rate   : {win_rate:.1f}%")
        print(f"  Avg Win    : Rs.{avg_win:+.2f}")
        print(f"  Avg Loss   : Rs.{avg_loss:+.2f}")
        print(f"  Actual R:R : {rr_act:.2f}:1  (target {RR_RATIO}:1)")
        print(f"  Total PnL  : Rs.{total:+.2f}")
        print(f"  Capital    : Rs.{self.starting_balance:,.2f} → Rs.{self.balance:,.2f}")
        print(f"  Return     : {((self.balance - self.starting_balance) / self.starting_balance) * 100:+.2f}%")
        print(f"{'='*64}")

        # Save JSON report
        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": "Open=Low/High + VWAP + EMA(9/21) + RSI(14)",
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
                    "qty": round(t["qty"], 4),
                    "pnl_pts": round(t["pnl_pts"], 2),
                    "pnl_rs": round(t["pnl_rs"], 2),
                    "reason": t["exit_reason"],
                    "hold_mins": t["hold_mins"],
                    "candle": t.get("candle_used", ""),
                    "indicators": {
                        "vwap": round(t["indicators"]["vwap"], 2),
                        "ema9": round(t["indicators"]["ema9"], 2),
                        "ema21": round(t["indicators"]["ema21"], 2),
                        "rsi": round(t["indicators"]["rsi"], 1),
                    } if t.get("indicators") else {},
                }
                for t in trades
            ],
        }

        all_reports = []
        if os.path.exists(REPORT_JSON):
            with open(REPORT_JSON, encoding="utf-8") as f:
                try:
                    all_reports = json.load(f)
                except Exception:
                    all_reports = []
        all_reports = [r for r in all_reports if r.get("date") != report["date"]]
        all_reports.append(report)
        with open(REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(all_reports, f, indent=2)
        print(f"  Report: {REPORT_JSON}")


# ================================================================
# SCAN — Open=Low/High + 3 Indicator Confirmation
# ================================================================
def scan_for_signals(watchlist, session):
    """
    For each stock:
      1. Check 1st candle (9:15) for Open=Low/High pattern
      2. If marubozu → use 2nd candle
      3. Compute VWAP, EMA9/21, RSI on multi-day 5m data
      4. All 3 indicators must agree with signal direction
      5. Calculate Entry, SL, Target, Qty (from your sheet formula)

    Returns list of confirmed signals sorted by RSI momentum strength.
    """
    signals = []
    stats = {"total": 0, "no_pattern": 0, "indicator_reject": 0, "confirmed": 0}

    print(f"\n  ┌─ SCANNING {len(watchlist)} stocks ─────────────────────────")
    print(f"  │ Pattern: Open=Low/High")
    print(f"  │ Filter : VWAP > price | EMA9 vs EMA21 | RSI zone")
    print(f"  │")

    for symbol in watchlist:
        try:
            time.sleep(0.15)  # Rate limit
            stats["total"] += 1

            # Fetch 5-day data (for EMA/RSI history)
            all_data = fetch_candles(symbol, session, period="5d", interval="5m")
            if all_data is None or len(all_data) < 30:
                continue

            # Extract today's candles
            today = get_today_candles_from(all_data)
            if today is None or today.empty:
                continue

            # Get 1st candle (9:15 AM)
            c1_df = today.between_time("09:15", "09:19")
            if c1_df.empty:
                c1_df = today.head(1)
            if c1_df.empty:
                continue
            c1 = c1_df.iloc[0]

            # Price filter
            close_price = float(c1["Close"])
            if close_price < PRICE_FLOOR or close_price > PRICE_CAP:
                continue

            # ─── STEP 1: Open=Low / Open=High ───
            signal_type = None
            if is_open_equal_low(c1):
                signal_type = "LONG"
            elif is_open_equal_high(c1):
                signal_type = "SHORT"

            if signal_type is None:
                stats["no_pattern"] += 1
                continue

            pattern_str = "Open=Low→BUY" if signal_type == "LONG" else "Open=High→SELL"

            # ─── STEP 2: Marubozu check ───
            entry_candle = c1
            candle_label = "1st candle (9:15)"

            if is_marubozu(c1):
                # Use 2nd candle for safer entry (from your sheet note)
                c2_df = today.between_time("09:20", "09:24")
                if c2_df.empty and len(today) >= 2:
                    c2_df = today.iloc[[1]]
                if c2_df.empty:
                    continue
                entry_candle = c2_df.iloc[0]
                candle_label = "2nd candle (9:20) — marubozu"

            # ─── STEP 3: Compute indicators ───
            indicators = get_indicators(all_data, today)
            if indicators is None:
                continue

            # ─── STEP 4: Check ALL 3 indicators ───
            passed, reason = check_indicators(indicators, signal_type)

            if not passed:
                stats["indicator_reject"] += 1
                print(f"  │ {symbol:<12} {pattern_str:<16} ✗ {reason}")
                continue

            # ─── STEP 5: Entry, SL, Target (your sheet formula) ───
            entry = float(entry_candle["Close"])
            if signal_type == "LONG":
                sl = float(entry_candle["Low"])
            else:
                sl = float(entry_candle["High"])

            # Skip flat candles
            if abs(entry - sl) < 0.01:
                continue

            # Skip if SL too wide (> 2.5% — avoids gap risk)
            sl_pct = abs(entry - sl) / entry
            if sl_pct > 0.025:
                print(f"  │ {symbol:<12} {pattern_str:<16} ✗ SL too wide ({sl_pct*100:.1f}%)")
                continue

            qty, stock_risk, target = calculate_position(
                PAPER_CAPITAL, entry, sl, signal_type
            )
            if qty <= 0:
                continue

            # ─── CONFIRMED SIGNAL ───
            stats["confirmed"] += 1
            print(f"  │ {symbol:<12} {pattern_str:<16} ✓ {reason}")
            print(f"  │ {'':14} Entry={entry:.2f}  SL={sl:.2f}  "
                  f"Target={target:.2f}  Qty={qty:.2f}  Risk={stock_risk:.2f}")

            signals.append({
                "symbol": symbol,
                "signal_type": signal_type,
                "entry": entry,
                "sl": sl,
                "target": target,
                "qty": qty,
                "stock_risk": stock_risk,
                "candle_label": candle_label,
                "indicators": indicators,
            })

        except Exception as e:
            print(f"  │ {symbol:<12} ERROR: {e}")

    print(f"  │")
    print(f"  │ Summary: {stats['confirmed']} confirmed | "
          f"{stats['no_pattern']} no pattern | "
          f"{stats['indicator_reject']} rejected by indicators")
    print(f"  └────────────────────────────────────────────────────────")

    # Sort by RSI strength (strongest momentum first)
    signals.sort(key=lambda x: abs(x["indicators"]["rsi"] - 50), reverse=True)
    return signals


# ================================================================
# TRAILING STOPLOSS (from your sheet's trailing table)
# ================================================================
def update_trailing_sl(pos, current_price):
    """
    Your sheet trailing logic:
      CMP  SL   MaxLoss
      100  80   20
      101  81   19
      102  82   18
      ...

    Implementation: After price moves +1R in favor, trail SL
    at (highest_price - trail_gap) where trail_gap = risk × 0.5

    SL ONLY moves in favorable direction (never backwards).
    """
    side = pos["side"]
    risk = pos["risk_pts"]
    entry = pos["entry"]

    if side == "LONG":
        # Update highest price seen
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price

        # Check if trailing should activate
        profit = pos["highest_price"] - entry
        if profit >= risk * TRAIL_ACTIVATION_R:
            pos["trailing_active"] = True
            trail_gap = risk * TRAIL_GAP_FACTOR
            new_sl = pos["highest_price"] - trail_gap
            # SL only moves UP
            if new_sl > pos["sl"]:
                old_sl = pos["sl"]
                pos["sl"] = round(new_sl, 2)
                return True, old_sl
    else:  # SHORT
        if current_price < pos["lowest_price"]:
            pos["lowest_price"] = current_price

        profit = entry - pos["lowest_price"]
        if profit >= risk * TRAIL_ACTIVATION_R:
            pos["trailing_active"] = True
            trail_gap = risk * TRAIL_GAP_FACTOR
            new_sl = pos["lowest_price"] + trail_gap
            # SL only moves DOWN
            if new_sl < pos["sl"]:
                old_sl = pos["sl"]
                pos["sl"] = round(new_sl, 2)
                return True, old_sl

    return False, pos["sl"]


# ================================================================
# DEFENSE — Monitor positions + trailing SL
# ================================================================
def defense_loop(broker: PaperBroker, session):
    """
    Every 30 seconds:
      - Fetch 1-min data for each open position
      - Update trailing stoploss
      - Check SL hit / Target hit / Time exit
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

        # Fetch 1-min candles for tight monitoring
        data = fetch_candles(symbol, session, period="1d", interval="1m")
        if data is None or data.empty:
            print(f"     {symbol}: no data")
            continue

        today_str = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
        data = data[data.index.strftime("%Y-%m-%d") == today_str]
        if data.empty:
            continue

        last = data.iloc[-1]
        price = float(last["Close"])
        candle_high = float(last["High"])
        candle_low = float(last["Low"])

        # Update trailing SL
        sl_moved, old_sl = update_trailing_sl(pos, price)

        # Unrealised PnL
        if side == "LONG":
            unrlsd = (price - pos["entry"]) * pos["qty"]
        else:
            unrlsd = (pos["entry"] - price) * pos["qty"]

        trail_info = ""
        if pos["trailing_active"]:
            trail_info = f" [TRAIL SL={pos['sl']:.2f}]"
        if sl_moved:
            trail_info += f" ↑SL moved from {old_sl:.2f}"

        print(f"     {symbol:<10} [{side}] LTP={price:.2f} "
              f"SL={pos['sl']:.2f} Target={pos['target']:.2f} "
              f"PnL=Rs.{unrlsd:+.2f}{trail_info}")

        # ─── Exit checks ───
        exit_reason = ""
        exit_price = price

        if side == "LONG":
            if candle_low <= pos["sl"]:
                exit_reason = "TRAILING SL HIT" if pos["trailing_active"] else "STOP LOSS HIT"
                exit_price = pos["sl"]  # Fill at SL level
            elif candle_high >= pos["target"]:
                exit_reason = "TARGET HIT ✓"
                exit_price = pos["target"]  # Fill at target level
        else:  # SHORT
            if candle_high >= pos["sl"]:
                exit_reason = "TRAILING SL HIT" if pos["trailing_active"] else "STOP LOSS HIT"
                exit_price = pos["sl"]
            elif candle_low <= pos["target"]:
                exit_reason = "TARGET HIT ✓"
                exit_price = pos["target"]

        # Force exit at 3:10 PM
        if not exit_reason and datetime.now().time() >= FORCE_EXIT_TIME:
            exit_reason = "TIME EXIT (3:10 PM)"
            exit_price = price

        if exit_reason:
            pnl = broker.exit_position(symbol, exit_price, exit_reason)
            cycle_pnl += pnl
            closed_count += 1

    return closed_count, cycle_pnl


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":

    broker = PaperBroker(starting_balance=PAPER_CAPITAL)
    session = make_session()
    watchlist = get_watchlist(index="BOTH")

    trades_done = 0
    daily_pnl = 0.0
    MAX_DAILY_LOSS = PAPER_CAPITAL * MAX_DAILY_LOSS_PCT

    # ── Wait for 9:20 AM ──
    print(f"\n  Waiting for 9:20 AM (1st candle must close)...")
    while datetime.now().time() < ENTRY_WINDOW_START:
        remaining = (
            datetime.combine(datetime.today(), ENTRY_WINDOW_START) - datetime.now()
        ).seconds
        print(f"  {datetime.now().strftime('%H:%M:%S')} — {remaining}s until 9:20...")
        time.sleep(15)

    print(f"\n  9:20 AM — 1st candle closed. Scanning now.\n")

    # ── Scan for signals ──
    print(f"[{datetime.now().strftime('%H:%M:%S')}] SCANNING WITH INDICATORS...")
    signals = scan_for_signals(watchlist, session)

    # ── Enter trades ──
    entered = 0
    for sig in signals:
        if entered >= MAX_TRADES_PER_DAY:
            print(f"\n  Max {MAX_TRADES_PER_DAY} trades reached.")
            break
        if sig["symbol"] in broker.open_positions:
            continue

        broker.place_order(
            symbol=sig["symbol"],
            side=sig["signal_type"],
            qty=sig["qty"],
            entry=sig["entry"],
            sl=sig["sl"],
            target=sig["target"],
            risk_pts=sig["stock_risk"],
            candle_label=sig["candle_label"],
            indicators=sig["indicators"],
        )
        entered += 1

    if entered == 0:
        print(f"\n  No signals passed all 3 indicator checks.")
        print(f"  This is SELECTIVE — better 0 trades than losing trades.")

    # ── Defense loop ──
    try:
        while True:
            now = datetime.now().time()

            if now >= MARKET_CLOSE:
                print("\n  3:30 PM — Market closed.")
                break

            if trades_done >= MAX_TRADES_PER_DAY and not broker.open_positions:
                print(f"\n  All {MAX_TRADES_PER_DAY} trades completed for today.")
                break

            if daily_pnl <= -MAX_DAILY_LOSS:
                print(f"\n  ⚠ DAILY LOSS LIMIT hit (Rs.{daily_pnl:.2f})")
                for sym in list(broker.open_positions.keys()):
                    broker.exit_position(sym, broker.open_positions[sym]["entry"],
                                         "DAILY LOSS LIMIT")
                break

            if broker.open_positions:
                n = len(broker.open_positions)
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                      f"MONITORING {n} position(s):")
                closed, pnl = defense_loop(broker, session)
                if closed > 0:
                    trades_done += closed
                    daily_pnl += pnl
                    print(f"\n  → Closed: {closed} | Day PnL: Rs.{daily_pnl:+.2f} | "
                          f"Trades: {trades_done}/{MAX_TRADES_PER_DAY}")
            else:
                # Re-scan if within entry window and haven't filled all slots
                if now < ENTRY_WINDOW_END and entered < MAX_TRADES_PER_DAY:
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Re-scanning for late signals...")
                    signals = scan_for_signals(watchlist, session)
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
                            target=sig["target"],
                            risk_pts=sig["stock_risk"],
                            candle_label=sig["candle_label"],
                            indicators=sig["indicators"],
                        )
                        entered += 1
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"No positions. Day PnL: Rs.{daily_pnl:+.2f}")

            time.sleep(DEFENSE_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\n  Manually stopped (Ctrl+C).")
        if broker.open_positions:
            print("  Closing remaining positions...")
            for sym in list(broker.open_positions.keys()):
                broker.exit_position(sym, broker.open_positions[sym]["entry"],
                                     "MANUAL STOP")

    finally:
        broker.print_summary()
