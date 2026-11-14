# Astra on Claude (Opus 5.5)

The bot can use Claude instead of the OpenAI saved agent as Astra, the analyst that writes the
hourly market map and reviews setups. It's one setting, and you can switch back at any time.

**What changes:** who writes the market map and the setup reviews (and, if you run it, who
studies your lessons).

**What does not change:** everything after the answer. The same Python code validates the map,
checks the SMC evidence and playbooks, waits for ordered confirmations, requires 2R, applies the
calendar and news locks, checks the broker spread and gates Telegram. A bad answer from Claude
is rejected exactly like a bad answer from OpenAI. There is still no order code anywhere.

**News:** the news check stays on your current news provider (`openai_web`), so keep
`OPENAI_API_KEY` set. Moving news to Claude is a separate, later step.

## Install (Windows, once)

1. Copy the new files into the project, keeping the folders:
   - `src/astra_anthropic.py`
   - `tests/test_astra_anthropic.py`
   - `tools/apply_claude_analyst.py`
   - `config/astra_instructions.md`
   - `docs/CLAUDE_ANALYST.md`
2. Preview the edits, then apply them:
   ```powershell
   .\.venv\Scripts\python.exe tools\apply_claude_analyst.py --check
   .\.venv\Scripts\python.exe tools\apply_claude_analyst.py
   ```
   The script makes small edits to the monitor (`src/gold_monitor.py`), `tools/astra_dry_run.py`,
   `tools/learn_lessons.py` and `requirements.txt`. If any file differs from V6.1.3 it changes
   nothing and lists what didn't match. Originals are kept as `*.pre_claude.bak`.
3. Install the SDK and run the tests:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -U -r requirements.txt
   .\scripts\selftest.ps1          # must end with "OK"
   ```
4. Set your Anthropic key (console.anthropic.com → API keys), then open a new PowerShell window:
   ```powershell
   setx ANTHROPIC_API_KEY "sk-ant-..."
   ```
   Also set a monthly spend limit in the Anthropic Console.
   Personal keys (they start with `sk-ant-usr-`) are not tied to one workspace, so the bot must also
   send your workspace ID. Copy it from the **ID** column in Console → Settings → Workspaces (it starts
   with `wrkspc_`), then run `setx ANTHROPIC_WORKSPACE_ID "wrkspc_..."`. Without it Claude answers
   "anthropic-workspace-id is required" or "not scoped to a workspace". The bot sends the key as
   `Authorization: Bearer`, the method Anthropic documents for all key types.
5. Optional, but recommended for a fair comparison: give Claude exactly the role text your saved
   OpenAI agent uses. This overwrites `config/astra_instructions.md` and keeps a `.bak` copy:
   ```powershell
   .\.venv\Scripts\python.exe src\astra_anthropic.py --export-openai-instructions
   ```

## Compare before switching

Run the same dry run with each analyst, into separate folders:

```powershell
.\.venv\Scripts\python.exe tools\astra_dry_run.py --analyst astra  --state runtime_dryrun_openai
.\.venv\Scripts\python.exe tools\astra_dry_run.py --analyst claude --state runtime_dryrun_claude
```

Add `--offset-hours 3` (or 2 in winter) when the market is closed.

Compare the two `report.txt` files:
- Was the map accepted or rejected by Python validation?
- Are the levels the ones you see on your chart?
- How many tokens and seconds did each take?
- How good are the TRADE IDEA texts?

Run it on a few different days, not just once.

If Claude takes longer than about 300 seconds, raise `ai_deadline_seconds` (default 480) so the
live bot doesn't cut it off.

## Switch

Three ways to choose, from strongest to weakest:

| How | Lasts | Command |
|---|---|---|
| `run.ps1 -Analyst` | this run only | `.\scripts\run.ps1 -Analyst claude` or `-Analyst astra` |
| `GOLD_ANALYST` environment variable | every new PowerShell window | `setx GOLD_ANALYST claude` (back: `setx GOLD_ANALYST astra`) |
| config file | permanent default | `"analyst_provider": "anthropic"` in `config/gold_v5_config.json` |

`claude` means Claude, and `astra` means your saved OpenAI agent. The bot prints the analyst at
startup, and the Telegram ONLINE message names it. A typo such as `GOLD_ANALYST=cluade` stops the
bot with a clear error instead of silently picking one. To clear the variable completely, run
`[Environment]::SetEnvironmentVariable("GOLD_ANALYST", $null, "User")` and open a new window.

The offline self-test ignores `GOLD_ANALYST`, so the tests give the same result whichever analyst you
have chosen.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `analyst_provider` | `openai` | `openai` (saved agent) or `anthropic` (Claude) |
| `anthropic_model` | `claude-opus-5-5` | Claude model id; env `GOLD_CLAUDE_MODEL` overrides |
| `astra_reasoning` | `medium` | becomes Claude's effort: `low`, `medium`, `high`, `xhigh` |
| `anthropic_map_max_tokens` | 32000 | room for thinking plus the map JSON; a cut-off answer is rejected |
| `anthropic_review_max_tokens` | 8000 | the same, for a setup review |
| `anthropic_instructions_file` | `config/astra_instructions.md` | Astra's role text (the V5/V6 output contract is always added by the bot) |
| `anthropic_structured_output` | `true` | the API enforces the JSON schema; `false` puts the schema in the prompt instead |

If the API ever refuses the map schema as too complex, the bot repeats that call once with the
schema in the prompt. Python's strict validation applies in both cases.

## Lessons

`tools/learn_lessons.py` follows the same setting. Lessons already studied are not repeated.
The playbook is rebuilt by Claude the next time a new or edited lesson is studied. Use
`--redo NAME ...` if you want Claude to re-study specific lessons blind.

## Keeping score

Your learning history keeps scoring every idea and scenario (played out, failed, not
triggered), and the daily report shows the track record. Note the date you switched, so you can
compare the record before and after. A few weeks of ideas is the minimum before drawing a
conclusion.
