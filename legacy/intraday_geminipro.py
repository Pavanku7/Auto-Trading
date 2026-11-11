import yfinance as yf
import pandas as pd
import numpy as np
import pyotp
import time
import ta
from growwapi import GrowwAPI
import os
from dotenv import load_dotenv
import nsepython
from datetime import datetime
import ssl
import urllib3,requests
from curl_cffi import requests as cffi_requests

# ================= ENV LOAD =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CURRENT_DIR)
print('BASE_DIR:', BASE_DIR)
ENV_PATH = os.path.join(BASE_DIR, f"config.env")
print("ENV_PATH:", ENV_PATH)

if not os.path.exists(ENV_PATH):
    raise RuntimeError(f"config.dev.env not found at {ENV_PATH}")

load_dotenv(ENV_PATH)

# ==========================================
# 1. Automated Login (Groww Trading App)
# ==========================================
def authenticate_broker():
    access_token = os.getenv('GROWW_API_KEY')
    # totp_secret = os.getenv('TOTP_SECRET')
    if not access_token:
        raise ValueError("Access token not found in .env file.")

    # if not totp_secret or len(totp_secret) < 16:
    #     raise ValueError("Invalid TOTP secret. Must be Base32 string.")
    #
    # # Generate the live TOTP code using your actual secret
    # totp = pyotp.TOTP(totp_secret).now()

    print("Authenticating with Groww API...")
    # access_token = GrowwAPI.get_access_token(api_key=access_token, totp=totp)
    print(f"DEBUG: Access Token: {access_token}")
    return GrowwAPI(access_token)

# ==========================================
# 2. Dynamic function
# ==========================================
def get_dynamic_watchlist():
    """Dynamically fetches today's Top Gainers from the NSE."""
    print("Fetching live Top Gainers from the NSE...")
    try:
        # nsepython fetches the live top gainers list directly from the NSE website
        gainers_data = nsepython.nse_get_top_gainers()

        # Extract the symbols from the returned dictionary/list
        symbols = [stock['symbol'] for stock in gainers_data]

        print(f"🔥 Successfully pulled {len(symbols)} Top Gainers: {symbols}")
        return symbols

    except Exception as e:
        print(f"⚠️ Could not fetch NSE Top Gainers (NSE might be rate-limiting us): {e}")
        print("Falling back to the curated high-liquidity watchlist...")

        # Safe fallback so your bot keeps running no matter what
        return [
            "SUZLON", "YESBANK", "PNB", "IOC",
            "ONGC", "IDFCFIRSTB", "NHPC", "BHEL", "SAIL",
            "IRFC", "RVNL", "JIOFIN", "RECLTD", "PFC",
            "TATASTEEL", "BANKINDIA", "NATIONALUM", "BEL"
        ]

# ==========================================
# 3. Small Capital Screener (Stocks < ₹500)
# ==========================================

# Suppress the ugly red warnings that pop up when we bypass SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_affordable_intraday_stock(symbols):
    """Scans for the highest volatility stock priced under Rs 500 for small accounts."""
    print("Scanning for high-momentum stocks under ₹500...")
    best_stock = None
    highest_volatility = 0
    current_price_of_best = 0

    # 1. Create a curl_cffi session to mimic Chrome and forcefully disable SSL verification
    custom_session = cffi_requests.Session(impersonate="chrome", verify=False)

    for symbol in symbols:
        try:
            time.sleep(0.1)

            # 2. Inject the custom session into yfinance
            data = yf.download(
                f"{symbol}.NS",
                period="5d",
                interval="15m",
                progress=False,
                session=custom_session
            )

            if data is None or data.empty:
                continue

            # THE FIX: Flatten the MultiIndex columns so the 'ta' library can read them
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)

            if 'Close' not in data.columns:
                continue

            # Clean up any missing data points
            data = data.dropna()

            current_price = float(data['Close'].iloc[-1])

            # Filter out expensive stocks
            if current_price > 500:
                continue

            # Calculate Volatility
            atr_data = ta.volatility.AverageTrueRange(
                high=data['High'],
                low=data['Low'],
                close=data['Close'],
                window=14
            ).average_true_range()

            current_atr = float(atr_data.iloc[-1])
            volatility_pct = (current_atr / current_price) * 100

            # THE SCOREBOARD: Add this one line right here!
            print(f"  -> {symbol}: Price = ₹{current_price:.2f} | Volatility = {volatility_pct:.2f}%")

            if volatility_pct > highest_volatility:
                highest_volatility = volatility_pct
                best_stock = symbol
                current_price_of_best = current_price

        except Exception as e:
            # THIS IS CRUCIAL: We will finally see if the data is breaking!
            print(f"Skipped {symbol} due to error: {e}")

    if best_stock:
        print(f"Selected Affordable Stock: {best_stock} at ₹{current_price_of_best:.2f}")
    else:
        print("No suitable stocks found matching criteria.")

    return best_stock


# ==========================================
# 4. High Win-Rate Confluence Strategy
# ==========================================
def execute_confluence_strategy(groww, symbol, account_balance=5000):
    """Executes trades using ORB, VWAP, EMA, and RSI to maximize win rate."""
    print(f"\nAnalyzing 5-minute timeframe for {symbol}...")

    # 1. Use the Chrome-mimicking session
    custom_session = cffi_requests.Session(impersonate="chrome", verify=False)

    data = yf.download(
        f"{symbol}.NS",
        period="1d",
        interval="5m",
        progress=False,
        session=custom_session
    )
    print('data', data)

    if data is None or data.empty:
        print(f"No 5-minute data available for {symbol}.")
        return

    # 2. Flatten the MultiIndex (just like in the scanner)
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    # Drop any rows with missing data that could break the math
    data = data.dropna()

    # 3. FIX THE SHAPE ERROR: Use .squeeze() to force columns into 1D arrays
    high = data['High'].squeeze()
    low = data['Low'].squeeze()
    close = data['Close'].squeeze()
    volume = data['Volume'].squeeze()

    # 4. FIX VARIABLE BUGS: Save indicators as NEW columns, don't overwrite 'data'
    data['VWAP'] = ta.volume.VolumeWeightedAveragePrice(
        high=high, low=low, close=close, volume=volume
    ).volume_weighted_average_price()

    data['EMA_9'] = ta.trend.EMAIndicator(close=close, window=9).ema_indicator()
    data['EMA_21'] = ta.trend.EMAIndicator(close=close, window=21).ema_indicator()
    data['RSI'] = ta.momentum.RSIIndicator(close=close, window=14).rsi()

    # Define Opening Range (ORB)
    opening_range = data.between_time('09:15', '09:30')
    if opening_range.empty:
        print("Waiting for morning data to establish Opening Range...")
        return

    orb_high = float(opening_range['High'].max())

    # Get the most recent completed 5-minute candle
    latest_candle = data.iloc[-1]
    current_price = float(latest_candle['Close'])

    # Calculate ATR for 1% Risk Management
    atr_series = ta.volatility.AverageTrueRange(
        high=high, low=low, close=close, window=14
    ).average_true_range()

    atr = float(atr_series.iloc[-1])
    risk_per_trade = account_balance * 0.01  # ₹50 strict risk

    # Prevent divide-by-zero errors if ATR is 0
    position_size = int(risk_per_trade / atr) if atr > 0 else 0

    # 5. FIX LOGIC BUGS: Explicitly compare values to specific columns
    is_breakout = current_price > orb_high
    is_above_vwap = current_price > float(latest_candle['VWAP'])
    is_ema_uptrend = float(latest_candle['EMA_9']) > float(latest_candle['EMA_21'])
    is_strong_momentum = float(latest_candle['RSI']) > 50

    if is_breakout and is_above_vwap and is_ema_uptrend and is_strong_momentum:
        stop_loss_price = current_price - atr
        target_price = current_price + (2 * atr)

        print(f"\n*** HIGH PROBABILITY BUY SIGNAL TRIGGERED ***")
        print(f"Purchasing {position_size} shares of {symbol}")
        print(f"Max Loss Capped at: ₹{risk_per_trade:.2f} (SL: ₹{stop_loss_price:.2f})")
        print(f"Targeting 2x Return: ₹{target_price:.2f}")

        try:
            response = groww.place_order(
                trading_symbol=symbol,
                quantity=position_size,
                validity=groww.VALIDITY_DAY,
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                product=groww.PRODUCT_MIS,
                order_type=groww.ORDER_TYPE_MARKET,
                transaction_type=groww.TRANSACTION_TYPE_BUY
            )
            print("Order routed successfully:", response)
        except Exception as e:
            print("Order execution failed (Likely due to zero balance):", e)
        # 🚨 THE NEW PART: Return 'True' and the trade math so Defense Mode can use it
        return True, current_price, stop_loss_price, target_price, position_size
    else:
        # Print exactly which conditions are met so you can watch it hunt for the setup!
        print(
            f"Waiting for confluence -> Breakout: {is_breakout} | >VWAP: {is_above_vwap} | EMA Uptrend: {is_ema_uptrend} | RSI>50: {is_strong_momentum}")
        # 🚨 Return 'False' because we didn't buy anything yet
        return False, 0, 0, 0, 0
# ==========================================
# 5. Defense Mode Selling strategy
# ==========================================
def manage_active_trade(groww, symbol, trade_details):
    """Defense Mode: Monitors an open position and sells when criteria are met."""
    custom_session = cffi_requests.Session(impersonate="chrome", verify=False)

    # Pull the 1-minute data so we can react fast to stop-losses!
    data = yf.download(
        f"{symbol}.NS",
        period="1d",
        interval="1m",
        progress=False,
        session=custom_session
    )

    if data is None or data.empty:
        return False, 0

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    current_price = float(data['Close'].iloc[-1])

    print(
        f"🛡️ DEFENSE MODE | {symbol} Price: ₹{current_price:.2f} | Target: ₹{trade_details['target']:.2f} | SL: ₹{trade_details['sl']:.2f}")

    sell_reason = ""

    # 1. Target Hit (Secure Profit)
    if current_price >= trade_details['target']:
        sell_reason = "TARGET HIT 🎯"

    # 2. Stop Loss Hit (Cut Losses)
    elif current_price <= trade_details['sl']:
        sell_reason = "STOP LOSS HIT 🛑"

    # 3. Time Exit (Sideways market, closing soon)
    else:
        current_time = datetime.now().time()
        # If it's 2:50 PM, get out no matter what to avoid broker penalties
        if current_time.hour == 14 and current_time.minute >= 50:
            sell_reason = "TIME EXIT ⏰ (Market closing soon)"

    # If any of the 3 conditions are met, FIRE THE SELL ORDER!
    if sell_reason:
        print(f"\n*** EXECUTING SELL ORDER: {sell_reason} ***")

        try:
            # Send the SELL payload to Groww
            groww.place_order(
                trading_symbol=symbol,
                quantity=trade_details['qty'],
                validity=groww.VALIDITY_DAY,
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                product=groww.PRODUCT_MIS,
                order_type=groww.ORDER_TYPE_MARKET,
                transaction_type=groww.TRANSACTION_TYPE_SELL  # <--- SELLING!
            )
            print("Sell order routed successfully!")
        except Exception as e:
            print("Sell execution failed (Likely due to zero balance testing):", e)

        # Calculate exactly how much money we made or lost
        pnl = (current_price - trade_details['entry']) * trade_details['qty']

        alert_msg = f"📉 **TRADE CLOSED** 📉\n**Reason:** {sell_reason}\n**Final Price:** ₹{current_price:.2f}\n**Estimated PnL:** ₹{pnl:.2f}"
        print(alert_msg)
        # send_discord_alert(alert_msg)

        return True, pnl  # True means the trade is officially over

    return False, 0  # False means we are still holding the stock

if __name__ == "__main__":

    # affordable_watchlist =
    try:
        # 1. Login to broker
        broker_session = authenticate_broker()

        # 2. Dynamically fetch the NSE stock list
        affordable_watchlist = get_dynamic_watchlist()

        # STATE VARIABLES
        is_in_position = False
        active_trade_details = {}
        bought_ticker = None
        trades_completed = 0
        MAX_TRADES_PER_DAY = 3  # Protects your account from over-trading

        while True:
            current_time = datetime.now().time()

            # 🛑 THE KILL SWITCH (3:00 PM)
            if current_time.hour >= 15:
                print("\n🛑 MARKET CLOSING SOON (3:00 PM) 🛑")
                print("Shutting down the bot for the day.")
                break

            # 🛑 MAX TRADES LIMIT
            if trades_completed >= MAX_TRADES_PER_DAY:
                print(
                    f"\n🛑 Reached maximum of {MAX_TRADES_PER_DAY} trades for the day. Shutting down to protect capital.")
                break

            if not is_in_position:
                # MODE 1: HUNTING
                print("\n📡 Sweeping the market for the current highest momentum...")
                best_ticker = get_affordable_intraday_stock(affordable_watchlist)

                if best_ticker:
                    bought, entry, sl, target, qty = execute_confluence_strategy(broker_session, best_ticker,
                                                                                 account_balance=5000)

                    if bought:
                        is_in_position = True
                        bought_ticker = best_ticker
                        active_trade_details = {'entry': entry, 'sl': sl, 'target': target, 'qty': qty}
                        print("\n➡️ Switching to DEFENSE MODE...")
                        time.sleep(60)  # Wait 60 seconds before checking the first exit
                    else:
                        print("Waiting 5 minutes before the next market sweep...")
                        time.sleep(300)
                else:
                    print("No valid stocks found. Waiting 5 minutes...")
                    time.sleep(300)

            else:
                # MODE 2: DEFENDING
                sold, pnl = manage_active_trade(broker_session, bought_ticker, active_trade_details)

                if sold:
                    is_in_position = False
                    bought_ticker = None
                    trades_completed += 1

                    print(
                        f"\n✅ Trade Complete! PnL: ₹{pnl:.2f}. Total trades today: {trades_completed}/{MAX_TRADES_PER_DAY}")
                    print("🔄 Switching back to HUNTING MODE...")
                    time.sleep(60)  # Take a short breather before scanning the market again
                else:
                    time.sleep(60)  # Still holding the stock, check again in 60 seconds

    except Exception as e:
        print("Fatal Error in main loop:", e)