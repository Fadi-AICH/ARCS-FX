# ARCS-CRYPTO

Separate weekend crypto bot built for fast `M1` action on XM crypto pairs.

## Run

From the repo root:

```powershell
py -3.11 crypto_main.py --dashboard
```

Or use:

```powershell
start_crypto_weekend.bat
```

Dashboard:

```text
http://127.0.0.1:5050
```

## Scope

- Crypto only
- `BTCUSD`, `ETHUSD`, `SOLUSD`, `XRPUSD`, `LTCUSD`, `DOGEUSD`
- `H1` context
- `M15` structure
- `M1` scalp trigger

## Storage

- Runtime status: `crypto_weekend/data/bot_status.json`
- Trades DB: `crypto_weekend/data/crypto_trades.db`
- Logs: `crypto_weekend/logs/`

## Notes

- This bot is intentionally separate from the main FX bot.
- The main `ARCS-FX` project is back to forex-only behavior.
