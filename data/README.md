Put optional bootstrap/memory files here: `gold_bootstrap_context.json`, `gold_bootstrap_raw.json`,
`gold_memory.db`. They are historical context only (ignored when older than 7 days) and never
replace fresh closed candles. `tools/mt5_calendar_to_excel.py` also writes `gold_calendar.xlsx` here.
