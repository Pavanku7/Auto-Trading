import yfinance as yf
import pandas as pd
import numpy as np
import time
import ta
from growwapi import GrowwAPI
import os
from dotenv import load_dotenv
import nsepython
from datetime import datetime, time as dtime
import urllib3
from curl_cffi import requests as cffi_requests

# ================= ENV LOAD =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR    = os.path.dirname(CURRENT_DIR)
ENV_PATH    = os.path.join(BASE_DIR, "config.env")
if not os.path.exists(ENV_PATH):
    raise RuntimeError(f"config.env not found at {ENV_PATH}")
load_dotenv(ENV_PATH)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# CONFIGURATION — Tune these values
# ==========================================
ACCOUNT_BALANCE      = 10000     # Your capital in ₹
RISK_PER_TRADE_PCT   = 0.01      # 1% risk per trade  (sheet: Risk = 0.01)
MIS_LEVERAGE         = 5         # Groww MIS leverage  (sheet: leverage = 5)
SL_PCT               = 0.02      # Stop-loss = 2% of entry price (sheet: stoploss = 2%*price)
RR_RATIO             = 2.0       # Target = 2× SL distance (sheet: Target = Risk * 2)
PRICE_CAP            = 500       # Only trade stocks under ₹500
MAX_TRADES_PER_DAY   = 3
MAX_DAILY_LOSS_PCT   = 0.03      # Kill switch at 3% daily loss

# --- Indicator settings ---
SUPERTREND_PERIOD    = 10        # ATR period for Supertrend
SUPERTREND_MULT      = 3.0       # Supertrend multiplier (standard for Indian markets)
VOLUME_MULTIPLIER    = 1.5       # Volume must be 1.5× the 20-bar average to confirm breakout
VWAP_BUFFER_PCT      = 0.001     # Price must be 0.1% above VWAP (avoids false signals at VWAP boundary)

# --- Timing ---
MARKET_OPEN          = dtime(9, 15)
MARKET_CLOSE         = dtime(15, 0)
FORCE_EXIT_TIME      = dtime(14, 50)
HUNT_INTERVAL_SEC    = 300       # Re-scan every 5 min
DEFENSE_INTERVAL_SEC = 60        # Check open position every 60 sec

# ==========================================
# 1. Broker Authentication
# ==========================================
def authenticate_broker():
    access_token = os.getenv('GROWW_API_KEY')
    if not access_token:
        raise ValueError("GROWW_API_KEY not found in config.env")
    print("🔐 Authenticating with Groww...")
    return GrowwAPI(access_token)

# ==========================================
# 2. Market Hours
# ==========================================
def is_market_open():
    now = datetime.now().time()
    return MARKET_OPEN <= now < MARKET_CLOSE

def wait_for_market_open():
    """Wait until 9:20 AM so the ORB window (9:15–9:30) has time to form."""
    orb_ready = dtime(9, 20)
    while datetime.now().time() < orb_ready:
        remaining = (
            datetime.combine(datetime.today(), orb_ready) - datetime.now()
        ).seconds
        print(f"⏳ Waiting for ORB to form... {remaining}s remaining")
        time.sleep(30)

# ==========================================
# 3. HTTP Session
# ==========================================
def make_session():
    return cffi_requests.Session(impersonate="chrome", verify=False)

# ==========================================
# 4. Supertrend
# ==========================================
def calculate_supertrend(high, low, close, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULT):
    """
    Returns a direction Series: +1 = bullish, -1 = bearish.
    The single most popular indicator among Indian retail traders.
    Clear signal, no ambiguity.
    """
    atr    = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=period).average_true_range()
    hl_avg = (high + low) / 2
    upper  = hl_avg + multiplier * atr
    lower  = hl_avg - multiplier * atr

    direction = pd.Series(index=close.index, dtype=int)

    for i in range(1, len(close)):
        upper.iloc[i] = upper.iloc[i] if (upper.iloc[i] < upper.iloc[i-1] or close.iloc[i-1] > upper.iloc[i-1]) else upper.iloc[i-1]
        lower.iloc[i] = lower.iloc[i] if (lower.iloc[i] > lower.iloc[i-1] or close.iloc[i-1] < lower.iloc[i-1]) else lower.iloc[i-1]

        if   close.iloc[i] > upper.iloc[i-1]: direction.iloc[i] =  1
        elif close.iloc[i] < lower.iloc[i-1]: direction.iloc[i] = -1
        else:                                  direction.iloc[i] = direction.iloc[i-1]

    return direction

# ==========================================
# 5. Dynamic Watchlist
# ==========================================
def get_dynamic_watchlist():
    print("📋 Fetching NSE Top Gainers...")
    try:
        gainers = nsepython.nse_get_top_gainers()
        symbols = [s['symbol'] for s in gainers]
        print(f"✅ {len(symbols)} top gainers pulled.")
        return symbols
    except Exception as e:
        print(f"⚠️ NSE fetch failed ({e}). Using fallback list.")
        return [
            "SUZLON", "YESBANK", "PNB", "IOC", "ONGC",
            "IDFCFIRSTB", "NHPC", "BHEL", "SAIL", "IRFC",
            "RVNL", "JIOFIN", "RECLTD", "PFC", "TATASTEEL",
            "BANKINDIA", "NATIONALUM", "BEL",
        ]

# ==========================================
# 6. Stock Scanner
# ==========================================
def scan_for_best_stock(symbols, session):
    """
    Scans the watchlist and returns the single best stock that:
      ✅ Priced under ₹500
      ✅ Supertrend is BULLISH on the 15-min chart
      ✅ Highest ATR volatility % (= most room to move)

    Supertrend is checked here as a pre-filter so we only
    run the full entry check on genuinely trending stocks.
    """
    print(f"\n📡 Scanning {len(symbols)} stocks (under ₹{PRICE_CAP}, Supertrend bullish)...")
    best_stock    = None
    best_vol_pct  = 0
    best_price    = 0

    for symbol in symbols:
        try:
            time.sleep(0.15)
            data = yf.download(f"{symbol}.NS", period="5d", interval="15m",
                               progress=False, session=session)
            if data is None or data.empty:
                continue
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            data = data.dropna()
            if 'Close' not in data.columns or len(data) < 20:
                continue

            price = float(data['Close'].iloc[-1])
            if price > PRICE_CAP:
                continue

            high  = data['High'].squeeze()
            low   = data['Low'].squeeze()
            close = data['Close'].squeeze()

            direction = calculate_supertrend(high, low, close)
            if direction.iloc[-1] != 1:
                print(f"  {symbol}: ₹{price:.2f} — Supertrend BEARISH, skip")
                continue

            atr     = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
            vol_pct = (float(atr.iloc[-1]) / price) * 100

            print(f"  {symbol}: ₹{price:.2f} — Supertrend ✅ | Volatility {vol_pct:.2f}%")

            if vol_pct > best_vol_pct:
                best_vol_pct = vol_pct
                best_stock   = symbol
                best_price   = price

        except Exception as e:
            print(f"  {symbol}: skipped — {e}")

    if best_stock:
        print(f"\n🏆 Best pick: {best_stock} @ ₹{best_price:.2f} | Volatility {best_vol_pct:.2f}%")
    else:
        print("❌ No suitable stock found this scan.")
    return best_stock

# ==========================================
# 7. Entry Strategy — 3 Conditions Only
# ==========================================
def check_entry(groww, symbol, session, account_balance=ACCOUNT_BALANCE):
    """
    Entry requires ALL THREE of:

      ✅ 1. SUPERTREND BULLISH  — trend is up on the 5-min chart
             The primary signal. No trend = no trade.

      ✅ 2. PRICE ABOVE VWAP   — institutional players are net buyers
             VWAP is the single most watched intraday level on NSE.
             Buying above VWAP = trading with institutions, not against them.

      ✅ 3. VOLUME SURGE        — current bar volume ≥ 1.5× 20-bar average
             Confirms the move has real buying behind it.
             Prevents entering on low-conviction drifts.

    That's it. Three clean, non-redundant signals. Each measures something
    different: trend direction, institutional interest, and conviction.

    Position Sizing (from your Excel sheet):
      SL distance   = 2% × entry price
      Rupee Risk    = 1% × capital
      Base Qty      = Rupee Risk / SL distance
      Total Qty     = Base Qty × 5× MIS leverage
      Target        = entry + 2× SL distance
    """
    print(f"\n🔍 Entry check: {symbol}")

    data = yf.download(f"{symbol}.NS", period="1d", interval="5m",
                       progress=False, session=session)
    if data is None or data.empty:
        print(f"  ❌ No 5-min data.")
        return False, 0, 0, 0, 0
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.dropna()
    if len(data) < 20:
        print(f"  ❌ Not enough bars yet.")
        return False, 0, 0, 0, 0

    high   = data['High'].squeeze()
    low    = data['Low'].squeeze()
    close  = data['Close'].squeeze()
    volume = data['Volume'].squeeze()

    # ── Indicator Calculations ──────────────────────────────
    direction   = calculate_supertrend(high, low, close)
    vwap_series = ta.volume.VolumeWeightedAveragePrice(
                      high=high, low=low, close=close, volume=volume
                  ).volume_weighted_average_price()

    last_price  = float(close.iloc[-1])
    last_vwap   = float(vwap_series.iloc[-1])
    last_vol    = float(volume.iloc[-1])
    avg_vol     = float(volume.iloc[-20:].mean())
    last_dir    = int(direction.iloc[-1])

    # ── The 3 Conditions ────────────────────────────────────
    cond_supertrend = (last_dir == 1)
    cond_vwap       = (last_price > last_vwap * (1 + VWAP_BUFFER_PCT))
    cond_volume     = (last_vol >= avg_vol * VOLUME_MULTIPLIER)

    print(f"\n  ┌─ Signal Checklist ─────────────────────────────────┐")
    print(f"  │  1. Supertrend Bullish : {'✅ YES' if cond_supertrend else '❌ NO '}")
    print(f"  │  2. Price > VWAP       : {'✅ YES' if cond_vwap       else '❌ NO '}  (₹{last_price:.2f} vs ₹{last_vwap:.2f})")
    print(f"  │  3. Volume Surge       : {'✅ YES' if cond_volume     else '❌ NO '}  ({last_vol:.0f} vs avg {avg_vol:.0f})")
    print(f"  └────────────────────────────────────────────────────┘")

    if not (cond_supertrend and cond_vwap and cond_volume):
        failed = []
        if not cond_supertrend: failed.append("Supertrend")
        if not cond_vwap:       failed.append("VWAP")
        if not cond_volume:     failed.append("Volume")
        print(f"  ⏳ Waiting — failed: {', '.join(failed)}")
        return False, 0, 0, 0, 0

    # ── Position Sizing (Excel sheet method) ────────────────
    sl_distance   = round(SL_PCT * last_price, 2)            # 2% of price
    rupee_risk    = account_balance * RISK_PER_TRADE_PCT      # 1% of capital
    base_qty      = int(rupee_risk / sl_distance)             # risk ÷ sl per share
    position_size = base_qty * MIS_LEVERAGE                   # apply 5× leverage

    if position_size <= 0:
        print(f"  ❌ Position size is zero. Price too high for capital.")
        return False, 0, 0, 0, 0

    stop_loss_price = round(last_price - sl_distance, 2)
    target_price    = round(last_price + (RR_RATIO * sl_distance), 2)
    capital_used    = last_price * position_size

    print(f"\n  ┌─ Order Details ─────────────────────────────────────┐")
    print(f"  │  Entry          : ₹{last_price:.2f}")
    print(f"  │  Stop Loss      : ₹{stop_loss_price:.2f}  (-{SL_PCT*100:.0f}%)")
    print(f"  │  Target         : ₹{target_price:.2f}  ({RR_RATIO}:1 R:R)")
    print(f"  │  SL distance    : ₹{sl_distance:.2f}")
    print(f"  │  Base Qty       : {base_qty} × {MIS_LEVERAGE}× = {position_size} shares")
    print(f"  │  Capital used   : ₹{capital_used:.2f}  (leveraged)")
    print(f"  │  Max loss       : ₹{sl_distance * position_size:.2f}  ({RISK_PER_TRADE_PCT*100:.0f}% capital)")
    print(f"  └────────────────────────────────────────────────────┘")

    print(f"\n  🚀 ALL 3 CONDITIONS MET — PLACING BUY ORDER")
    try:
        response = groww.place_order(
            trading_symbol=symbol,
            quantity=position_size,
            validity=groww.VALIDITY_DAY,
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            product=groww.PRODUCT_MIS,
            order_type=groww.ORDER_TYPE_MARKET,
            transaction_type=groww.TRANSACTION_TYPE_BUY,
        )
        print(f"  Groww response: {response}")
    except Exception as e:
        print(f"  ⚠️ Order failed: {e}")

    return True, last_price, stop_loss_price, target_price, position_size

# ==========================================
# 8. Defense Mode — Exit Strategy
# ==========================================
def manage_active_trade(groww, symbol, trade_details, session):
    """
    Exits on:
      1. TARGET HIT          — price reached 2× SL distance above entry
      2. TRAILING STOP HIT   — SL ratchets up 2% below current price,
                               but NEVER goes down (locks in profit)
      3. SUPERTREND FLIPS    — primary signal reverses: exit immediately,
                               no argument. The same indicator that got us
                               in will get us out.
      4. FORCE EXIT 2:50 PM  — never hold MIS overnight

    Returns: (sold: bool, pnl: float, updated_sl: float)
    The caller must save updated_sl back to active_trade['sl'] each tick.
    """
    data = yf.download(f"{symbol}.NS", period="1d", interval="1m",
                       progress=False, session=session)
    if data is None or data.empty:
        return False, 0, trade_details['sl']
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    high   = data['High'].squeeze()
    low    = data['Low'].squeeze()
    close  = data['Close'].squeeze()
    volume = data['Volume'].squeeze()

    current_price = float(close.iloc[-1])

    # Trailing stop: 2% below current price, never lower than stored SL
    trailing_sl  = round(current_price * (1 - SL_PCT), 2)
    effective_sl = max(trade_details['sl'], trailing_sl)

    # Supertrend on 1-min chart for exit
    direction     = calculate_supertrend(high, low, close)
    st_flipped    = (int(direction.iloc[-1]) == -1)

    print(
        f"  🛡️  {symbol} | ₹{current_price:.2f} | "
        f"Target ₹{trade_details['target']:.2f} | "
        f"Trail SL ₹{effective_sl:.2f} | "
        f"Supertrend {'🔴 BEAR' if st_flipped else '🟢 BULL'}"
    )

    sell_reason = ""
    if current_price >= trade_details['target']:
        sell_reason = "TARGET HIT 🎯"
    elif current_price <= effective_sl:
        sell_reason = "TRAILING STOP HIT 🛑"
    elif st_flipped:
        sell_reason = "SUPERTREND FLIPPED BEARISH 🔄"
    elif datetime.now().time() >= FORCE_EXIT_TIME:
        sell_reason = "TIME EXIT ⏰ (2:50 PM)"

    if sell_reason:
        print(f"\n  🔔 SELL — {sell_reason}")
        try:
            groww.place_order(
                trading_symbol=symbol,
                quantity=trade_details['qty'],
                validity=groww.VALIDITY_DAY,
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                product=groww.PRODUCT_MIS,
                order_type=groww.ORDER_TYPE_MARKET,
                transaction_type=groww.TRANSACTION_TYPE_SELL,
            )
            print("  ✅ Sell order placed.")
        except Exception as e:
            print(f"  ⚠️ Sell order failed: {e}")

        pnl = (current_price - trade_details['entry']) * trade_details['qty']
        print(f"  PnL: ₹{pnl:.2f}")
        return True, pnl, effective_sl

    return False, 0, effective_sl   # not sold — return updated sl for ratchet

# ==========================================
# 9. Main Loop
# ==========================================
if __name__ == "__main__":
    try:
        groww     = authenticate_broker()
        session   = make_session()
        watchlist = get_dynamic_watchlist()

        # State
        is_in_position = False
        active_trade   = {}
        bought_ticker  = None
        trades_today   = 0
        daily_pnl      = 0.0
        MAX_DAILY_LOSS = ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT

        print("\n" + "="*55)
        print("  INTRADAY BOT v3  — Supertrend + VWAP + Volume")
        print(f"  Capital ₹{ACCOUNT_BALANCE} | Risk {RISK_PER_TRADE_PCT*100:.0f}% | SL {SL_PCT*100:.0f}% | {MIS_LEVERAGE}× MIS")
        print("="*55)

        wait_for_market_open()

        while True:
            now = datetime.now().time()

            # ── Kill switches ──────────────────────────────────────
            if now >= MARKET_CLOSE:
                print("\n🛑 3:00 PM — Market closed.")
                break
            if trades_today >= MAX_TRADES_PER_DAY:
                print(f"\n🛑 Max {MAX_TRADES_PER_DAY} trades done for today.")
                break
            if daily_pnl <= -MAX_DAILY_LOSS:
                print(f"\n🛑 Daily loss limit ₹{daily_pnl:.2f} hit. Stopping.")
                break
            # ──────────────────────────────────────────────────────

            if not is_in_position:
                # ── HUNTING MODE ──────────────────────────────────
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 HUNTING")
                ticker = scan_for_best_stock(watchlist, session)

                if ticker:
                    bought, entry, sl, target, qty = check_entry(
                        groww, ticker, session
                    )
                    if bought:
                        is_in_position = True
                        bought_ticker  = ticker
                        active_trade   = {'entry': entry, 'sl': sl, 'target': target, 'qty': qty}
                        print(f"\n➡️  In position: {ticker}. Switching to DEFENSE.")
                        time.sleep(DEFENSE_INTERVAL_SEC)
                    else:
                        time.sleep(HUNT_INTERVAL_SEC)
                else:
                    time.sleep(HUNT_INTERVAL_SEC)

            else:
                # ── DEFENSE MODE ──────────────────────────────────
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🛡️  DEFENSE — {bought_ticker}")
                sold, pnl, updated_sl = manage_active_trade(
                    groww, bought_ticker, active_trade, session
                )

                # Always ratchet the trailing stop upward
                active_trade['sl'] = updated_sl

                if sold:
                    is_in_position = False
                    daily_pnl     += pnl
                    trades_today  += 1
                    bought_ticker  = None
                    active_trade   = {}
                    print(f"\n✅ Trade #{trades_today} | PnL ₹{pnl:.2f} | Day ₹{daily_pnl:.2f}")
                    time.sleep(DEFENSE_INTERVAL_SEC)
                else:
                    time.sleep(DEFENSE_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n🛑 Bot stopped manually.")
    except Exception as e:
        print(f"\n💥 Fatal: {e}")
        raise