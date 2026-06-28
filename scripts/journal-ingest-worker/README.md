# Journal Ingest Worker
Receives TradingView alerts directly from the 3 strategy indicators and writes
closed trades to `docs/trading/journal-data.json`, tagged by strategy.

- URL: https://journal-ingest.laurenlev10.workers.dev/<strategy>  (base|partial|momentum)
- Each strategy's TradingView alert ("Any alert() function call") posts to its path.
- Pairs entry+exit per strategy, computes ticks/$ from prices (MNQ tick 0.25, $0.5/tick).
- Holds GH_TOKEN server-side (secret). Deploy: `wrangler deploy` (CF token "Bookkeeping Deploy 2").
- ⚠️ TIME/generic exits with no price fall back to entry price (0 result) — refine after live data.
