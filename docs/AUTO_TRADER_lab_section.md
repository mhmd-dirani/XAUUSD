## Lab mode (1.10) — testing the rules on the demo account

The live track keeps every rule. The lab track is the experiment: it takes every idea the live track did
not trade, at the minimum lot, and records which rules the trade broke. After enough trades the report
shows which rules actually cost money and which were theory.

Turn it on in `config\auto_trader_config.json`:

```json
"lab_mode": true,
"lab_max_open": 6,
"lab_max_per_day": 20,
"lab_drawdown_kill_pct": 15.0,
"lab_unconfirmed_entry": true
```

then restart `scripts\run_trader.ps1`. `scripts\trader_check.ps1` shows `Lab mode ON`.

What you will see:
- Log channel: `🧪 LAB SELL LAB-SELL-4013 SELL_LIMIT @ 4009.0 | SL 4024.5 | TP1 3977.0 | R:R 2.07 | 0.01 lots`
  followed by `violations: session_off, mtf_opposed, counter_no_zone, class_off, no_confirmation`.
- The usual FILLED / CLOSED / REVIEW messages for lab trades (setup ids start with `LAB-`).
- Nothing on the signals channel.
- `scripts\trader_report.ps1`: a LAB section per rule, e.g.
  `session_off  broke  14 avg -0.42R | kept  22 avg +0.31R | diff +0.73R (< 20 samples)`.

How to read it: `diff` is what keeping the rule was worth per trade. A rule earns its place when `diff` is
clearly positive over 20-30 samples. Rules overlap, so read the "violation sets" lines before judging one rule.
Money results of lab trades mean nothing (0.01 lot); only R and hit rates count.

What the lab never does: trade a real account, trade on stale data, trade through a spread above
`broker_max_spread`, place two lab orders on the same setup in one day, or keep going after
`lab_drawdown_kill_pct` of the balance is lost in a week. The STOP file stops it like everything else.
