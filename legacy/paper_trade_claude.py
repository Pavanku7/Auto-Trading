"""
============================================================
  PAPER TRADING SIMULATOR — Indian Market (NSE Intraday)
  v2.1 — Fixed nsepython watchlist parsing
============================================================
"""

import yfinance as yf
import pandas as pd
import numpy as np
import ta
import os
import json
import csv
import time
from dotenv import load_dotenv
import nsepython
from datetime import datetime, time as dtime
import urllib3
from curl_cffi import requests as cffi_requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "config.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

# ============================================================
# CONFIG
# ============================================================
PAPER_CAPITAL        = 5000
RISK_PER_TRADE_PCT   = 0.01
MAX_TRADES_PER_DAY   = 3
MAX_DAILY_LOSS_PCT   = 0.03
PRICE_CAP            = 500
RR_RATIO             = 2.0
RSI_MIN              = 52        # Relaxed from 55 — 55 was filtering too many valid setups
VOLUME_MULTIPLIER    = 1.2       # Relaxed from 1.5 — NSE mid-caps don't always spike volume
ADX_THRESHOLD        = 18        # Relaxed from 20 — 20 was blocking valid trending days
SUPERTREND_PERIOD    = 10
SUPERTREND_MULT      = 3.0
HUNT_INTERVAL_SEC    = 300
DEFENSE_INTERVAL_SEC = 90
MARKET_OPEN          = dtime(9, 15)
MARKET_CLOSE         = dtime(15, 0)
FORCE_EXIT_TIME      = dtime(14, 50)

# ── Condition toggles — set False to skip a filter while tuning ──
USE_ADX_FILTER    = True
USE_VOLUME_FILTER = False
USE_MACD_FILTER   = True   # set False if too few trades trigger
USE_NIFTY_FILTER  = False   # set False during strong individual stock moves

LOG_CSV     = os.path.join(BASE_DIR, "paper_trade_log.csv")
REPORT_JSON = os.path.join(BASE_DIR, "paper_trade_report.json")

# ============================================================
# FIXED WATCHLIST — handles DataFrame, list, and dict formats
# ============================================================
def get_dynamic_watchlist():
    """
    nsepython.nse_get_top_gainers() returns a pandas DataFrame
    with a 'symbol' column (confirmed via diagnose_nse.py).
    This function handles that plus all other known return formats
    as a safety net.
    """
    print("Fetching live Top Gainers from NSE...")
    try:
        raw = nsepython.nse_get_top_gainers()

        symbols = []

        # ── Case 1: pandas DataFrame (confirmed format) ────
        if isinstance(raw, pd.DataFrame):
            # Try known column name variants
            for col in ('symbol', 'Symbol', 'SYMBOL', 'ticker', 'scrip'):
                if col in raw.columns:
                    symbols = raw[col].dropna().str.strip().str.upper().tolist()
                    break
            if not symbols:
                raise KeyError(f"No symbol column found. Columns: {list(raw.columns)}")

        # ── Case 2: plain list ─────────────────────────────
        elif isinstance(raw, list) and len(raw) > 0:
            first = raw[0]
            if isinstance(first, dict):
                for key in ('symbol', 'Symbol', 'SYMBOL', 'ticker', 'scrip'):
                    if key in first:
                        symbols = [item[key] for item in raw if key in item]
                        break
            elif isinstance(first, str):
                symbols = raw  # already a list of symbol strings

        # ── Case 3: dict wrapper ───────────────────────────
        elif isinstance(raw, dict):
            inner = raw.get('data') or next(
                (v for v in raw.values() if isinstance(v, list)), []
            )
            for item in inner:
                if isinstance(item, dict):
                    for key in ('symbol', 'Symbol', 'SYMBOL'):
                        if key in item:
                            symbols.append(item[key])
                            break
                elif isinstance(item, str):
                    symbols.append(item)

        # ── Validate ───────────────────────────────────────
        symbols = [str(s).strip().upper() for s in symbols if s]
        if not symbols:
            raise ValueError("Parsed symbol list is empty after cleaning")

        print(f"  ✅ Got {len(symbols)} symbols: {symbols[:8]}{'...' if len(symbols) > 8 else ''}")
        return symbols

    except Exception as e:
        print(f"  ⚠️  NSE fetch failed: {e}")
        print("  Using curated fallback watchlist...")
        return [
            "SUZLON", "YESBANK", "PNB", "IOC", "ONGC",
            "IDFCFIRSTB", "NHPC", "BHEL", "SAIL", "IRFC",
            "RVNL", "JIOFIN", "RECLTD", "PFC", "TATASTEEL",
            "BANKINDIA", "NATIONALUM", "BEL",
        ]


# ============================================================
# PAPER BROKER ENGINE
# ============================================================
class PaperBroker:
    def __init__(self, starting_balance: float):
        self.balance          = starting_balance
        self.starting_balance = starting_balance
        self.open_position    = None
        self.trade_history    = []
        self.order_count      = 0

        if not os.path.exists(LOG_CSV):
            with open(LOG_CSV, "w", newline="") as f:
                csv.writer(f).writerow([
                    "date", "time", "symbol", "side",
                    "qty", "price", "sl", "target",
                    "exit_price", "exit_reason",
                    "pnl", "balance_after", "indicators"
                ])

        print(f"\n{'='*55}")
        print(f"  PAPER TRADING ENGINE STARTED")
        print(f"  Simulated Capital : ₹{self.balance:,.2f}")
        print(f"  Risk per trade    : {RISK_PER_TRADE_PCT*100:.0f}% (₹{self.balance*RISK_PER_TRADE_PCT:.2f})")
        print(f"  Max daily loss    : ₹{self.balance*MAX_DAILY_LOSS_PCT:.2f}")
        print(f"{'='*55}\n")

    def place_buy(self, symbol, qty, entry_price, sl, target, indicators=None):
        self.order_count += 1
        now  = datetime.now()
        cost = qty * entry_price
        self.open_position = {
            "symbol": symbol, "qty": qty, "entry_price": entry_price,
            "sl": sl, "target": target, "entry_time": now,
            "indicators": indicators or {},
        }
        print(f"\n  🟢 [PAPER BUY #{self.order_count}]")
        print(f"     Symbol : {symbol}")
        print(f"     Qty    : {qty} shares @ ₹{entry_price:.2f}  (cost ₹{cost:.2f})")
        print(f"     SL     : ₹{sl:.2f}   Target: ₹{target:.2f}")
        print(f"     Time   : {now.strftime('%H:%M:%S')}")
        self._log(symbol, "BUY", qty, entry_price, sl, target, None, None, None, indicators)
        return True

    def place_sell(self, symbol, qty, exit_price, exit_reason):
        if not self.open_position:
            return 0.0
        pos  = self.open_position
        pnl  = (exit_price - pos["entry_price"]) * qty
        self.balance += pnl
        hold = int((datetime.now() - pos["entry_time"]).total_seconds() / 60)
        print(f"\n  🔴 [PAPER SELL]  {symbol}")
        print(f"     Exit   : ₹{exit_price:.2f}  ({exit_reason})")
        print(f"     PnL    : {'✅' if pnl>=0 else '❌'} ₹{pnl:+.2f}   Hold: {hold} min")
        print(f"     Balance: ₹{self.balance:,.2f}")
        self.trade_history.append({**pos, "exit_price": exit_price,
                                   "exit_reason": exit_reason, "pnl": pnl, "hold_mins": hold})
        self._log(symbol, "SELL", qty, pos["entry_price"], pos["sl"], pos["target"],
                  exit_price, exit_reason, pnl, pos.get("indicators"))
        self.open_position = None
        return pnl

    def _log(self, symbol, side, qty, price, sl, target,
             exit_price, exit_reason, pnl, indicators):
        now = datetime.now()
        with open(LOG_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                symbol, side, qty,
                round(price, 2),
                round(sl, 2) if sl else "",
                round(target, 2) if target else "",
                round(exit_price, 2) if exit_price is not None else "",
                exit_reason or "",
                round(pnl, 2) if pnl is not None else "",
                round(self.balance, 2),
                json.dumps(indicators) if indicators else "",
            ])

    def print_summary(self):
        trades = self.trade_history
        n = len(trades)
        if n == 0:
            print("\n  No trades completed today.")
            return
        wins     = [t for t in trades if t["pnl"] > 0]
        losses   = [t for t in trades if t["pnl"] <= 0]
        total    = sum(t["pnl"] for t in trades)
        win_rate = (len(wins) / n) * 100
        avg_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        rr       = abs(avg_win / avg_loss) if avg_loss else 0

        print(f"\n{'='*55}")
        print(f"  END OF DAY REPORT")
        print(f"  Trades   : {n}   Wins: {len(wins)}   Losses: {len(losses)}")
        print(f"  Win rate : {win_rate:.1f}%")
        print(f"  Avg win  : ₹{avg_win:.2f}   Avg loss: ₹{avg_loss:.2f}")
        print(f"  Actual RR: {rr:.2f}:1  (target {RR_RATIO}:1)")
        print(f"  Total PnL: ₹{total:+.2f}")
        print(f"  Capital  : ₹{self.starting_balance:.2f} → ₹{self.balance:.2f}")
        print(f"  Return   : {((self.balance-self.starting_balance)/self.starting_balance)*100:+.2f}%")
        print(f"  Log      : {LOG_CSV}")
        print(f"{'='*55}")

        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "starting_capital": self.starting_balance,
            "ending_capital": round(self.balance, 2),
            "total_pnl": round(total, 2),
            "return_pct": round(((self.balance-self.starting_balance)/self.starting_balance)*100, 2),
            "trades_taken": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "actual_rr": round(rr, 2),
            "trades": [
                {"symbol": t["symbol"], "entry": round(t["entry_price"],2),
                 "exit": round(t["exit_price"],2), "qty": t["qty"],
                 "pnl": round(t["pnl"],2), "reason": t["exit_reason"],
                 "hold_mins": t["hold_mins"]}
                for t in trades
            ]
        }
        all_rep = []
        if os.path.exists(REPORT_JSON):
            with open(REPORT_JSON) as f:
                try: all_rep = json.load(f)
                except: all_rep = []
        all_rep = [r for r in all_rep if r.get("date") != report["date"]]
        all_rep.append(report)
        with open(REPORT_JSON, "w") as f:
            json.dump(all_rep, f, indent=2)


# ============================================================
# INDICATORS
# ============================================================
def make_session():
    return cffi_requests.Session(impersonate="chrome", verify=False)

def calculate_supertrend(high, low, close, period=SUPERTREND_PERIOD, mult=SUPERTREND_MULT):
    atr       = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=period).average_true_range()
    hl_avg    = (high + low) / 2
    upper     = hl_avg + mult * atr
    lower     = hl_avg - mult * atr
    direction = pd.Series(0, index=close.index, dtype=int)
    for i in range(1, len(close)):
        upper.iloc[i] = upper.iloc[i] if (upper.iloc[i] < upper.iloc[i-1] or close.iloc[i-1] > upper.iloc[i-1]) else upper.iloc[i-1]
        lower.iloc[i] = lower.iloc[i] if (lower.iloc[i] > lower.iloc[i-1] or close.iloc[i-1] < lower.iloc[i-1]) else lower.iloc[i-1]
        if   close.iloc[i] > upper.iloc[i-1]: direction.iloc[i] =  1
        elif close.iloc[i] < lower.iloc[i-1]: direction.iloc[i] = -1
        else:                                  direction.iloc[i] =  direction.iloc[i-1]
    return direction

def calculate_adx(high, low, close, window=14):
    return ta.trend.ADXIndicator(high=high, low=low, close=close, window=window).adx()

def volume_is_confirmed(volume, current_volume, lookback=20):
    return current_volume >= volume.iloc[-lookback:].mean() * VOLUME_MULTIPLIER

def nifty_is_bullish(session):
    try:
        nifty = yf.download("^NSEI", period="1d", interval="5m", progress=False, session=session)
        if nifty is None or nifty.empty: return True
        if isinstance(nifty.columns, pd.MultiIndex):
            nifty.columns = nifty.columns.get_level_values(0)
        c = nifty['Close'].squeeze().dropna()
        return float(ta.trend.EMAIndicator(close=c, window=9).ema_indicator().iloc[-1]) > \
               float(ta.trend.EMAIndicator(close=c, window=21).ema_indicator().iloc[-1])
    except:
        return True


# ============================================================
# SCANNER
# ============================================================
def get_affordable_intraday_stock(symbols, session):
    print(f"\n  Scanning {len(symbols)} stocks under ₹{PRICE_CAP}...")
    best, best_vol, best_price = None, 0, 0
    for symbol in symbols:
        try:
            time.sleep(0.15)
            data = yf.download(f"{symbol}.NS", period="5d", interval="15m", progress=False, session=session)
            if data is None or data.empty: continue
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            data = data.dropna()
            if 'Close' not in data.columns or len(data) < 20: continue
            price = float(data['Close'].iloc[-1])
            if price > PRICE_CAP: continue
            h, l, c = data['High'].squeeze(), data['Low'].squeeze(), data['Close'].squeeze()
            atr_val  = float(ta.volatility.AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range().iloc[-1])
            vol_pct  = (atr_val / price) * 100
            direction = calculate_supertrend(h, l, c)
            if direction.iloc[-1] != 1:
                print(f"    {symbol}: Supertrend BEARISH — skip"); continue
            print(f"    {symbol}: ₹{price:.2f} | Volatility {vol_pct:.2f}% ✅")
            if vol_pct > best_vol:
                best_vol, best, best_price = vol_pct, symbol, price
        except Exception as e:
            print(f"    Skipped {symbol}: {e}")
    if best:
        print(f"\n  Selected: {best} @ ₹{best_price:.2f}")
    return best


# ============================================================
# PAPER ENTRY
# ============================================================
def paper_entry(broker: PaperBroker, symbol: str, session):
    print(f"\n  Analysing 5-min chart: {symbol}...")

    # Use "5d" period so we always have enough rows for ADX (needs 2×14=28 minimum)
    # "1d" can return as few as 9 candles early in the session — crashes ADX
    data = yf.download(f"{symbol}.NS", period="5d", interval="5m", progress=False, session=session)
    if data is None or data.empty:
        print("  ❌ No data returned"); return False, 0, 0, 0, 0
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    # Step 1: Convert to IST FIRST before anything else
    # yfinance returns UTC — between_time on UTC looks for 9:15 UTC = 2:45 PM IST (always empty)
    try:
        if data.index.tz is None:
            data.index = data.index.tz_localize('UTC')
        data.index = data.index.tz_convert('Asia/Kolkata')
    except Exception as tz_err:
        print(f"  ⚠️  TZ conversion warning: {tz_err}")

    data = data.dropna()

    # Step 2: Enforce minimum candle count AFTER tz conversion
    # ADX needs 2×window=28, EMA21 needs 21, MACD needs 26 — require 35 to be safe
    MIN_CANDLES = 35
    if len(data) < MIN_CANDLES:
        print(f"  ❌ Only {len(data)} candles available — need {MIN_CANDLES} for indicators. Waiting...")
        return False, 0, 0, 0, 0

    # Step 3: Slice to today's candles only for ORB/signal logic
    # But use full history for indicator calculation so ADX has enough data
    today_str  = pd.Timestamp.now(tz='Asia/Kolkata').strftime('%Y-%m-%d')
    today_data = data[data.index.strftime('%Y-%m-%d') == today_str]
    if today_data.empty:
        print(f"  ❌ No candles for today ({today_str}) yet"); return False, 0, 0, 0, 0

    print(f"  Total candles: {len(data)} | Today: {len(today_data)}")

    # Step 4: Calculate indicators on FULL multi-day data (enough rows for ADX)
    h, l, c, v = data['High'].squeeze(), data['Low'].squeeze(), data['Close'].squeeze(), data['Volume'].squeeze()
    data['VWAP']     = ta.volume.VolumeWeightedAveragePrice(high=h, low=l, close=c, volume=v).volume_weighted_average_price()
    data['EMA_9']    = ta.trend.EMAIndicator(close=c, window=9).ema_indicator()
    data['EMA_21']   = ta.trend.EMAIndicator(close=c, window=21).ema_indicator()
    data['RSI']      = ta.momentum.RSIIndicator(close=c, window=14).rsi()
    data['ADX']      = calculate_adx(h, l, c)
    data['ST_DIR']   = calculate_supertrend(h, l, c)
    macd             = ta.trend.MACD(close=c)
    data['MACD']     = macd.macd()
    data['MACD_SIG'] = macd.macd_signal()

    # Step 5: ORB from today's candles only
    today_data = data[data.index.strftime('%Y-%m-%d') == today_str]

    orb = today_data.between_time('09:15', '09:30')

    # Fallback: if between_time still empty, use first 3 candles of today
    if orb.empty:
        orb = today_data.head(3)
        print(f"  ⚠️  ORB between_time empty — using first 3 candles of today as fallback")

    if orb.empty:
        print("  ❌ No today candle data at all — skipping"); return False, 0, 0, 0, 0

    orb_high = float(orb['High'].max())
    orb_low  = float(orb['Low'].min())
    print(f"  ORB established: High ₹{orb_high:.2f} | Low ₹{orb_low:.2f} ({len(orb)} candles)")

    last       = data.iloc[-1]    # latest candle (most recent)
    price      = float(last['Close'])
    cur_volume = float(last['Volume'])
    atr        = float(ta.volatility.AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range().iloc[-1])
    if atr <= 0: return False, 0, 0, 0, 0

    risk_amount = broker.balance * RISK_PER_TRADE_PCT
    qty = min(int(risk_amount / atr), int((broker.balance * 0.8) / price))
    if qty <= 0:
        print(f"  ❌ Position size zero at ₹{price:.2f} (ATR={atr:.2f}). Skipping."); return False, 0, 0, 0, 0

    # FIX 2: Capture live values for display — so you see WHY each condition fails
    rsi_val    = float(last['RSI'])
    adx_val    = float(last['ADX'])
    ema9_val   = float(last['EMA_9'])
    ema21_val  = float(last['EMA_21'])
    vwap_val   = float(last['VWAP'])
    macd_val   = float(last['MACD'])
    macd_sig   = float(last['MACD_SIG'])
    st_dir     = int(last['ST_DIR'])
    avg_vol    = float(v.iloc[-20:].mean())
    nifty_bull = nifty_is_bullish(session)

    # FIX 3: Toggle flags let you disable individual conditions while tuning
    cond = {
        f"ORB breakout  [{price:.2f} > {orb_high:.2f}]"              : price > orb_high,
        f"Above VWAP    [{price:.2f} > {vwap_val:.2f}]"              : price > vwap_val,
        f"EMA uptrend   [EMA9={ema9_val:.2f} EMA21={ema21_val:.2f}]" : ema9_val > ema21_val,
        f"RSI > {RSI_MIN}         [RSI={rsi_val:.1f}]"                : rsi_val > RSI_MIN,
        f"Volume surge  [{cur_volume:.0f} vs avg {avg_vol:.0f}]"      : (not USE_VOLUME_FILTER) or volume_is_confirmed(v, cur_volume),
        f"ADX > {ADX_THRESHOLD}         [ADX={adx_val:.1f}]"          : (not USE_ADX_FILTER)    or adx_val > ADX_THRESHOLD,
        f"Supertrend    [dir={'+Bull' if st_dir==1 else '-Bear'}]"    : st_dir == 1,
        f"MACD cross    [{macd_val:.3f} > {macd_sig:.3f}]"           : (not USE_MACD_FILTER)   or macd_val > macd_sig,
        f"Nifty bullish [{nifty_bull}]"                               : (not USE_NIFTY_FILTER)  or nifty_bull,
    }

    # FIX 4: ALWAYS print checklist — even when nothing passes
    passed_count = sum(cond.values())
    print(f"\n  {'─'*56}")
    print(f"  Checklist: {symbol} @ ₹{price:.2f}  |  ORB high ₹{orb_high:.2f}")
    print(f"  {passed_count}/{len(cond)} conditions passed")
    print(f"  {'─'*56}")
    for name, passed in cond.items():
        print(f"    {'✅' if passed else '❌'} {name}")
    print(f"  {'─'*56}")

    if all(cond.values()):
        sl     = round(price - atr, 2)
        target = round(price + RR_RATIO * atr, 2)
        broker.place_buy(symbol=symbol, qty=qty, entry_price=price, sl=sl, target=target,
                         indicators={"rsi": round(rsi_val,1), "adx": round(adx_val,1),
                                     "atr": round(atr,2), "vwap": round(vwap_val,2),
                                     "orb_high": round(orb_high,2)})
        return True, price, sl, target, qty
    else:
        blocking = [n for n, p in cond.items() if not p]
        print(f"  Blocking condition: {blocking[0] if blocking else 'unknown'}")
        return False, 0, 0, 0, 0


# ============================================================
# PAPER DEFENSE
# ============================================================
def paper_defense(broker: PaperBroker, symbol: str, trade: dict, session):
    # Reduced from 3d to 2d to prevent Yahoo rate-limiting
    data = yf.download(f"{symbol}.NS", period="2d", interval="1m", progress=False, session=session)

    # Session Refresh Logic: If Yahoo blocks us, try creating a fresh session
    if data is None or data.empty:
        print("  ⚠️ Yahoo returned empty data. Refreshing session...")
        session = make_session()  # Generate a new spoofed session
        return False, 0

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    # Convert to IST
    try:
        if data.index.tz is None:
            data.index = data.index.tz_localize('UTC')
        data.index = data.index.tz_convert('Asia/Kolkata')
    except Exception:
        pass

    data = data.dropna()

    # Defense requires at least 15 candles for a 14-period ATR
    if len(data) < 15:
        return False, 0

    h, l, c, v = data['High'].squeeze(), data['Low'].squeeze(), data['Close'].squeeze(), data['Volume'].squeeze()
    price = float(c.iloc[-1])
    atr_now = float(ta.volatility.AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range().iloc[-1])

    # Calculate effective stop loss
    eff_sl = max(trade['sl'], round(price - atr_now, 2))
    vwap = float(
        ta.volume.VolumeWeightedAveragePrice(high=h, low=l, close=c, volume=v).volume_weighted_average_price().iloc[-1])

    print(f"  🛡️ {symbol} | ₹{price:.2f} | Target ₹{trade['target']:.2f} | SL ₹{eff_sl:.2f}")

    reason = ""
    if price >= trade['target']:
        reason = "TARGET HIT 🎯"
    elif price <= eff_sl:
        reason = "TRAILING STOP 🛑"
    elif price < vwap and price < trade['entry']:
        reason = "VWAP REVERSAL ⚠️"
    elif datetime.now().time() >= FORCE_EXIT_TIME:
        reason = "TIME EXIT ⏰"

    if reason:
        pnl = broker.place_sell(symbol, trade['qty'], price, reason)
        return True, pnl

    return False, 0


# ============================================================
# MAIN LOOP
# ============================================================
if __name__ == "__main__":
    broker    = PaperBroker(starting_balance=PAPER_CAPITAL)
    session   = make_session()
    watchlist = get_dynamic_watchlist()

    is_in_position = False
    active_trade   = {}
    bought_ticker  = None
    trades_done    = 0
    daily_pnl      = 0.0
    MAX_DAILY_LOSS = PAPER_CAPITAL * MAX_DAILY_LOSS_PCT

    # Wait for ORB window to form
    orb_ready = dtime(9, 20)
    while datetime.now().time() < orb_ready:
        secs = (datetime.combine(datetime.today(), orb_ready) - datetime.now()).seconds
        print(f"  Waiting for market to settle... {secs}s")
        time.sleep(30)

    print("\n  Bot is live in PAPER MODE. Watching markets...\n")

    try:
        while True:
            now = datetime.now().time()
            if now >= MARKET_CLOSE:
                print("\n  Market closed (3:00 PM). Ending session."); break
            if trades_done >= MAX_TRADES_PER_DAY:
                print(f"\n  Max trades ({MAX_TRADES_PER_DAY}) reached. Done."); break
            if daily_pnl <= -MAX_DAILY_LOSS:
                print(f"\n  Daily loss limit (₹{daily_pnl:.2f}) hit. Stopping."); break

            if not is_in_position:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] HUNTING MODE")
                ticker = get_affordable_intraday_stock(watchlist, session)
                if ticker:
                    bought, entry, sl, target, qty = paper_entry(broker, ticker, session)
                    if bought:
                        is_in_position = True
                        bought_ticker  = ticker
                        active_trade   = {'entry': entry, 'sl': sl, 'target': target, 'qty': qty}
                        time.sleep(DEFENSE_INTERVAL_SEC)
                    else:
                        time.sleep(HUNT_INTERVAL_SEC)
                else:
                    time.sleep(HUNT_INTERVAL_SEC)
            else:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] DEFENSE MODE — {bought_ticker}")
                sold, pnl = paper_defense(broker, bought_ticker, active_trade, session)
                if sold:
                    is_in_position = False
                    daily_pnl     += pnl
                    trades_done   += 1
                    bought_ticker  = None
                    active_trade   = {}
                    print(f"\n  Trade #{trades_done} closed. Day PnL: ₹{daily_pnl:+.2f}")
                    time.sleep(DEFENSE_INTERVAL_SEC)
                else:
                    time.sleep(DEFENSE_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\n  Stopped manually.")
    finally:
        broker.print_summary()