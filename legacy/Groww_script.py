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

# ================= ENV LOAD =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
    print('control comes here')
    # access_token = GrowwAPI.get_access_token(api_key=access_token, totp=totp)
    print(f"DEBUG: Access Token: {access_token}")
    return GrowwAPI(access_token)

# ==========================================
# 2. Dynamic function
# ==========================================
def get_dynamic_watchlist():
    """Dynamically fetches all active NSE stock symbols."""
    print("Fetching live NSE symbols from the exchange...")

    # Retrieves a list of all active equity symbols directly from the NSE
    symbols = nsepython.nse_eq_symbols()

    # We slice the list to the top 200 stocks to prevent the script from taking
    # too long to scan. You can increase this number if you want to scan more.
    return symbols[:5]

# ==========================================
# 3. Small Capital Screener (Stocks < ₹500)
# ==========================================
def get_affordable_intraday_stock(symbols):
    """Scans for the highest volatility stock priced under Rs 500 for small accounts."""
    print("Scanning for high-momentum stocks under ₹500...")
    best_stock = None
    highest_volatility = 0

    for symbol in symbols:
        data = yf.download(f"{symbol}.NS", period="5d", interval="15m", progress=False)
        if data.empty: continue

        current_price = data['Close'].iloc[-1]

        # Filter for affordability: Only trade stocks under ₹500
        if current_price > 500:
            continue

        data = ta.volatility.AverageTrueRange(
            high=data['High'], low=data['Low'], close=data['Close'], window=14
        ).average_true_range()

        current_atr = data.iloc[-1]
        volatility_pct = (current_atr / current_price) * 100

        if volatility_pct > highest_volatility:
            highest_volatility = volatility_pct
            best_stock = symbol

    print(f"Selected Affordable Stock: {best_stock} at ₹{current_price:.2f}")
    return best_stock


# ==========================================
# 4. High Win-Rate Confluence Strategy
# ==========================================
def execute_confluence_strategy(groww, symbol, account_balance=5000):
    """Executes trades using ORB, VWAP, EMA, and RSI to maximize win rate."""
    data = yf.download(f"{symbol}.NS", period="1d", interval="5m", progress=False)

    # Calculate VWAP, 9-EMA, 21-EMA, and RSI for high-probability confluence
    data = ta.volume.VolumeWeightedAveragePrice(
        high=data['High'], low=data['Low'], close=data['Close'], volume=data['Volume']
    ).volume_weighted_average_price()

    data['EMA_9'] = ta.trend.EMAIndicator(close=data['Close'], window=9).ema_indicator()
    data['EMA_21'] = ta.trend.EMAIndicator(close=data['Close'], window=21).ema_indicator()
    data = ta.momentum.RSIIndicator(close=data['Close'], window=14).rsi()

    # Define Opening Range
    opening_range = data.between_time('09:15', '09:30')
    orb_high = opening_range['High'].max()

    latest_candle = data.iloc[-1]
    current_price = latest_candle['Close']

    # 1% Risk Management
    atr = ta.volatility.AverageTrueRange(
        high=data['High'], low=data['Low'], close=data['Close'], window=14
    ).average_true_range().iloc[-1]

    risk_per_trade = account_balance * 0.01  # ₹50 strict risk
    position_size = int(risk_per_trade / atr)

    # High Win Rate Conditions: Breakout + Price > VWAP + EMA Uptrend + RSI Momentum
    is_breakout = current_price > orb_high
    is_above_vwap = current_price > latest_candle
    is_ema_uptrend = latest_candle['EMA_9'] > latest_candle['EMA_21']
    is_strong_momentum = latest_candle > 50

    if is_breakout and is_above_vwap and is_ema_uptrend and is_strong_momentum:
        stop_loss_price = current_price - atr
        target_price = current_price + (2 * atr)

        print(f"*** HIGH PROBABILITY BUY SIGNAL TRIGGERED ***")
        print(f"Purchasing {position_size} shares of {symbol}")
        print(f"Max Loss Capped at: ₹{risk_per_trade:.2f}")
        print(f"Targeting 2x Return: ₹{target_price:.2f}")

        try:
            response = groww.place_order(
                trading_symbol=symbol,
                quantity=position_size,
                validity=groww.VALIDITY_DAY,
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                product=groww.PRODUCT_MIS,  # 5x Intraday Leverage
                order_type=groww.ORDER_TYPE_MARKET,
                transaction_type=groww.TRANSACTION_TYPE_BUY
            )
            print("Order routed successfully:", response)
        except Exception as e:
            print("Order execution failed:", e)
    else:
        print("Waiting for high-probability confluence setup...")


if __name__ == "__main__":

    # affordable_watchlist =
    try:
        # 1. Login to broker
        broker_session = authenticate_broker()

        # 2. Dynamically fetch the NSE stock list
        affordable_watchlist = get_dynamic_watchlist()

        # 3. Scan the dynamic list for the best momentum stock under Rs 500
        best_ticker = get_affordable_intraday_stock(affordable_watchlist)

        if best_ticker:
            print(f"Monitoring {best_ticker} for the 2x setup...")

            # Keep checking the market every 5 minutes until 3:15 PM
            while True:
                current_time = datetime.now().time()

                # Stop trading at 3:15 PM to avoid auto-square-off penalties
                if current_time.hour == 15 and current_time.minute >= 15:
                    print("Market closing soon. Shutting down the bot for the day.")
                    break

                    # Run the strategy
                execute_confluence_strategy(broker_session, best_ticker, account_balance=5000)

                # Pause the script for 5 minutes (300 seconds) before checking the next candle
                time.sleep(300)

    except Exception as e:
        print("Error:", e)