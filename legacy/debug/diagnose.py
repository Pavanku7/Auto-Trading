"""
Quick diagnostic + fix for nsepython.nse_get_top_gainers() structure.
Run this file once to see what your NSE data actually looks like,
then the fixed get_dynamic_watchlist() handles all known formats.
"""

import nsepython
import json
import pandas as pd


# ============================================================
# ============================================================
# FIXED WATCHLIST — handles DataFrame, list, and dict formats
# PAPER ORDER ENGINE  ← This replaces groww.place_order()
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


if __name__ == "__main__":
    get_dynamic_watchlist()