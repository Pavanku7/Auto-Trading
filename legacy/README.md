# Legacy / archived code

These files are earlier iterations kept for reference only. **Do not run them.**
They are superseded by the current scripts in the project root and `Groww/`.

| File | What it was | Superseded by |
|------|-------------|---------------|
| `paper_trade_claude.py` | v2.1 paper simulator (ADX/RSI/volume screener) | `paper_trade_v4_indicators.py` |
| `paper_trade_v2.py` | v3 Open=Low/High + confirmation candle | `paper_trade_v4_indicators.py` |
| `Groww_script.py` | First Groww live attempt — has bugs (VWAP/RSI overwrite the `data` frame) | `Groww/intraday_claudeai.py` |
| `intraday_geminipro.py` | Gemini-authored Groww bot, same VWAP/RSI bugs | `Groww/intraday_claudeai.py` |
| `Zerodha_script.py` | Zerodha/Kite port — never finished, same confluence bugs | (none — Groww is the active broker) |
| `debug/` | One-off Groww auth/TOTP diagnostics | — |
| `expirement.ipynb`, `Untitled-1.ipynb` | Scratch notebooks | — |

## Why the old broker scripts were retired
`Groww_script.py`, `intraday_geminipro.py`, and `Zerodha_script.py` all reassign
the `data` DataFrame to the VWAP series, then try to read `data['EMA_9']` and
`data['Close']` off it — which no longer exist. They would crash on the first
`execute_confluence_strategy` call. The rewritten `Groww/intraday_claudeai.py`
keeps indicators as separate series and is the only broker script that runs.
