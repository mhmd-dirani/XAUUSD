#!/usr/bin/env python3
"""Install the Claude analyst switch into an existing Gold Monitor V6.1.3 folder.

Run once from the project root, after copying src/astra_anthropic.py, tests/test_astra_anthropic.py
and config/astra_instructions.md into place:

    .\\.venv\\Scripts\\python.exe tools\\apply_claude_analyst.py            # apply
    .\\.venv\\Scripts\\python.exe tools\\apply_claude_analyst.py --check    # only report, change nothing

It makes small, exact edits to the monitor (src/gold_monitor_v51.py or whatever you renamed it
to: it is found by its content), tools/astra_dry_run.py, tools/learn_lessons.py and
requirements.txt. If several files look like the monitor, name it: --monitor src\\gold_monitor.py Every edit must find its anchor text exactly once;
if ANY anchor is missing nothing is written (all or nothing). Originals are kept as
*.pre_claude.bak. Running it again is safe: edits already present are skipped.

The OpenAI path is unchanged. Claude is used only when the config says
"analyst_provider": "anthropic".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MONITOR = [
    ("import",
     'import astra_analysis as AA  # noqa: E402\n',
     'import astra_analysis as AA  # noqa: E402\nimport astra_anthropic as AN  # noqa: E402\n'),
    ("config defaults",
     '    "playbook_file": "lessons/astra_playbook.json",   # from tools/learn_lessons.py; "" disables\n}\n',
     '    "playbook_file": "lessons/astra_playbook.json",   # from tools/learn_lessons.py; "" disables\n'
     '    # Analyst backend: "openai" (saved agent) or "anthropic" (Claude). See src/astra_anthropic.py.\n'
     '    "analyst_provider": "openai",\n'
     '    "anthropic_model": "claude-opus-5-5",\n'
     '    "anthropic_map_max_tokens": 32000,          # thinking + JSON for the hourly map\n'
     '    "anthropic_review_max_tokens": 8000,        # thinking + JSON for a setup review\n'
     '    "anthropic_instructions_file": "config/astra_instructions.md",\n'
     '    "anthropic_structured_output": True,\n'
     '}\n'),
    ("config validation",
     '        raise ValueError("provider_options.broker_http_url is required for http_json broker quotes")\n'
     '    return c\n',
     '        raise ValueError("provider_options.broker_http_url is required for http_json broker quotes")\n'
     '    AN.validate_config(c)\n'
     '    return c\n'),
    ("secret redaction",
     '    for key in ("OPENAI_API_KEY", "TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):\n',
     '    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN",\n'
     '                "TELEGRAM_CHAT_ID"):\n'),
    ("analyst dispatch",
     'def ai_worker(kind, payload, c, saved, pipe):\n'
     '    """Isolated spawned process; parent can terminate it without blocking price monitoring."""\n',
     'def ai_worker(kind, payload, c, saved, pipe):\n'
     '    """Isolated spawned process; parent can terminate it without blocking price monitoring."""\n'
     '    if c.get("analyst_provider") == "anthropic":\n'
     '        return AN.ai_worker(sys_module(), kind, payload, c, saved, pipe)\n'),
    ("self-test list",                     # (V6.1.3 file names, renamed test files)
     ('"test_calendar_holiday_hotfix", "test_gold_v613", "test_astra_v6")\n',
      '"test_calendar_holidays", "test_reliability", "test_astra")\n'),
     ('"test_calendar_holiday_hotfix", "test_gold_v613", "test_astra_v6",\n'
      '             "test_astra_anthropic")\n',
      '"test_calendar_holidays", "test_reliability", "test_astra",\n'
      '             "test_astra_anthropic")\n')),
    ("required keys",
     '    need = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]\n',
     '    need = AN.required_keys(c) + ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]\n'),
    ("required packages",
     '    modules = {"requests": "requests", "openai": "openai", "jsonschema": "jsonschema"}\n',
     '    modules = dict({"requests": "requests", "jsonschema": "jsonschema"}, **AN.required_modules(c))\n'),
]

DRY_RUN = [
    ("import",
     'import astra_analysis as AA  # noqa: E402\n',
     'import astra_analysis as AA  # noqa: E402\nimport astra_anthropic as AN  # noqa: E402\n'),
    ("analyst dispatch",
     'def call_astra(c, payload):\n    from openai import OpenAI\n',
     'def call_astra(c, payload):\n'
     '    if c.get("analyst_provider") == "anthropic":\n'
     '        return AN.dry_run_map(M, c, payload)\n'
     '    from openai import OpenAI\n'),
    ("--analyst override",                 # (fresh V6.1.3 file, first version of this edit)
     ('    if args.language:\n        c["telegram_language"] = args.language\n',
      '    if args.language:\n        c["telegram_language"] = args.language\n'
      '    if args.analyst:\n        c["analyst_provider"] = args.analyst\n'),
     ('    if args.language:\n        c["telegram_language"] = args.language\n'
      '    if args.analyst:\n        c["analyst_provider"] = AN.normalize(args.analyst)\n',) * 2),
    ("--analyst option",
     ('    p.add_argument("--language", help="override telegram_language (Arabic/English)")\n',
      '    p.add_argument("--language", help="override telegram_language (Arabic/English)")\n'
      '    p.add_argument("--analyst", choices=("openai", "anthropic"),\n'
      '                   help="override analyst_provider (compare Astra on OpenAI vs Claude)")\n'),
     ('    p.add_argument("--language", help="override telegram_language (Arabic/English)")\n'
      '    p.add_argument("--analyst", choices=("astra", "claude", "openai", "anthropic"),\n'
      '                   help="analyst for this run: astra (OpenAI) or claude")\n',) * 2),
    ("key check",
     '    if not args.no_ai and not args.raw and not os.getenv("OPENAI_API_KEY"):\n'
     '        p.error("OPENAI_API_KEY is not set (or use --no-ai / --raw)")\n',
     '    key = AN.analyst_key_name(args.config, args.analyst)\n'
     '    if not args.no_ai and not args.raw and not os.getenv(key):\n'
     '        p.error(f"{key} is not set (or use --no-ai / --raw)")\n'),
]

LESSONS = [
    ("import",
     'import astra_analysis as AA  # noqa: E402\n',
     'import astra_analysis as AA  # noqa: E402\nimport astra_anthropic as AN  # noqa: E402\n'),
    ("lesson call",
     '    instructions = saved["instructions"] + TEACHING.format(language=language) + "\\n" + step\n',
     '    instructions = saved["instructions"] + TEACHING.format(language=language) + "\\n" + step\n'
     '    if saved.get("provider") == "anthropic":\n'
     '        return AN.lesson_ask(client, saved, schema, instructions, parts, effort, tokens)\n'),
    ("client",
     'def make_client(c):\n    from openai import OpenAI\n',
     'def make_client(c):\n'
     '    if c.get("analyst_provider") == "anthropic":\n'
     '        return AN.make_client(c, retries=1)\n'
     '    from openai import OpenAI\n'),
    ("analyst",
     '        saved = M.load_saved_agent(client)\n',
     '        saved = (AN.load_analyst(c, M.ROOT) if c.get("analyst_provider") == "anthropic"\n'
     '                 else M.load_saved_agent(client))\n'),
    ("key check",
     '    if client is None and not args.report_only and not os.getenv("OPENAI_API_KEY"):\n'
     '        p.error("OPENAI_API_KEY is not set")\n',
     '    key = AN.analyst_key_name(args.config)\n'
     '    if client is None and not args.report_only and not os.getenv(key):\n'
     '        p.error(f"{key} is not set")\n'),
]

# (label, usual path, text that identifies the file before AND after patching, edits, required)
FILES = [
    ("monitor", "src/gold_monitor_v51.py", "def ai_worker(kind, payload, c, saved, pipe):", MONITOR, True),
    ("dry run", "tools/astra_dry_run.py", "def call_astra(c, payload):", DRY_RUN, False),
    ("lessons", "tools/learn_lessons.py",
     'instructions = saved["instructions"] + TEACHING.format(language=language)', LESSONS, False),
]
OWN = {"astra_anthropic.py", "apply_claude_analyst.py", "test_astra_anthropic.py"}
NEEDED = ["src/astra_anthropic.py", "tests/test_astra_anthropic.py"]
REQUIREMENT = "anthropic>=1.13"


def plan_file(path, edits):
    """(new_text, applied, skipped) or raise ValueError listing every anchor problem."""
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    applied, skipped, problems = [], [], []
    for name, olds, news in edits:
        olds = olds if isinstance(olds, tuple) else (olds,)
        news = news if isinstance(news, tuple) else (news,)
        if any(new in text for new in news):
            skipped.append(name)
            continue
        hits = [(old, new) for old, new in zip(olds, news) if text.count(old) == 1]
        if hits:                    # an earlier version of the edit contains the original anchor: longest wins
            hits = [max(hits, key=lambda h: len(h[0]))]
        if len(hits) != 1:
            counts = "/".join(str(text.count(old)) for old in olds)
            problems.append(f"{path.name}: '{name}' anchor found {counts} times (expected 1)")
            continue
        text = text.replace(*hits[0])
        applied.append(name)
    if problems:
        raise ValueError("\n".join(problems))
    return (text.replace("\n", "\r\n") if crlf else text), applied, skipped


def locate(root, usual, signature, override=None, edits=None):
    """(path, None) or (None, problem). The usual path first, then any .py in src/, tools/ or the
    project root whose content identifies it (renamed files are found too)."""
    if override:
        path = Path(override) if Path(override).is_absolute() else root / override
        if not path.is_file():
            return None, f"{override} not found"
        if signature not in path.read_text(encoding="utf-8", errors="replace"):
            return None, f"{override} does not contain {signature!r}"
        return path, None
    first = root / usual
    if first.is_file() and signature in first.read_text(encoding="utf-8", errors="replace"):
        return first, None
    found = []
    for folder in (root / "src", root / "tools", root):
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.py")):
            if p.name in OWN or p in found:
                continue
            try:
                if signature in p.read_text(encoding="utf-8", errors="replace"):
                    found.append(p)
            except OSError:
                continue
    if len(found) > 1 and edits:
        # e.g. an old gold_monitor_v5.py next to the current bot: keep the file the edits fit
        fitting = []
        for p in found:
            try:
                plan_file(p, edits)
                fitting.append(p)
            except ValueError:
                pass
        found = fitting or found
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, f"no file containing {signature!r} (looked for {usual} and in src/, tools/, the root)"
    return None, "several candidates: " + ", ".join(str(p.relative_to(root)) for p in found)


def run(root, check=False, out=print, overrides=None):
    root = Path(root)
    overrides = overrides or {}
    missing = [n for n in NEEDED if not (root / n).is_file()]
    if missing:
        out("Copy these files into the project first: " + ", ".join(missing))
        return 1
    plans, problems = [], []
    for label, usual, signature, edits, required in FILES:
        path, why = locate(root, usual, signature, overrides.get(label), edits)
        if path is None:
            if required or overrides.get(label):
                problems.append(f"{label}: {why}")
            else:
                out(f"{label}: skipped ({why})")
            continue
        try:
            plans.append((path, *plan_file(path, edits)))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        out("NOTHING CHANGED.\n  " + "\n  ".join(problems))
        out("Send this output so the edit can be adjusted to your version.")
        return 2
    req = root / "requirements.txt"
    req_text = req.read_text(encoding="utf-8") if req.is_file() else ""
    need_req = not any(l.strip().lower().startswith("anthropic") for l in req_text.splitlines())
    for path, text, applied, skipped in plans:
        out(f"{path.relative_to(root)}: " + (f"apply {', '.join(applied)}" if applied else "already done")
            + (f" (already present: {', '.join(skipped)})" if applied and skipped else ""))
    out("requirements.txt: " + (f"add {REQUIREMENT}" if need_req else "already lists anthropic"))
    if check:
        out("--check: nothing written.")
        return 0
    for path, text, applied, _ in plans:
        if not applied:
            continue
        backup = path.with_name(path.name + ".pre_claude.bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        path.write_bytes(text.encode("utf-8"))
    if need_req:
        req.write_text(req_text.rstrip("\n") + ("\n" if req_text else "") + REQUIREMENT + "\n", encoding="utf-8")
    out("Done. Next: pip install -r requirements.txt, then scripts\\selftest.ps1 (must end with OK).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report what would change, write nothing")
    ap.add_argument("--root", default=str(ROOT), help="project root (default: this script's project)")
    ap.add_argument("--monitor", help="path of the main bot file if it cannot be found automatically")
    ap.add_argument("--dry-run-file", help="path of astra_dry_run.py if renamed")
    ap.add_argument("--lessons-file", help="path of learn_lessons.py if renamed")
    args = ap.parse_args(argv)
    overrides = {"monitor": args.monitor, "dry run": args.dry_run_file, "lessons": args.lessons_file}
    return run(args.root, args.check, overrides={k: v for k, v in overrides.items() if v})


if __name__ == "__main__":
    sys.exit(main())
