#!/usr/bin/env python3
"""Claude (Anthropic) analyst backend for Astra, the drop-in alternative to the OpenAI saved agent.

Switch with one setting in config/gold_v5_config.json:

    "analyst_provider": "anthropic"          (default "openai" = unchanged behaviour)

or, without touching the config, with the GOLD_ANALYST environment variable (it wins):

    .\\scripts\\run.ps1 -Analyst claude          this run only (astra = the OpenAI agent)
    setx GOLD_ANALYST claude                    every new window from now on

Only WHO writes the market map / setup review changes. Everything after the model answer is
the same Python code as before: strict schema validation, validate_map(), evidence playbooks,
ordered confirmations, 2R, calendar/news locks, broker spread, Telegram gate. Nothing here can
place an order; this module has no broker access at all.

Settings (all optional, defaults shown):
    "anthropic_model": "claude-opus-5-5"                 (env GOLD_CLAUDE_MODEL overrides)
    "anthropic_map_max_tokens": 32000                    (thinking + JSON for the hourly map)
    "anthropic_review_max_tokens": 8000                  (thinking + JSON for a setup review)
    "anthropic_instructions_file": "config/astra_instructions.md"   (Astra's role text)
    "anthropic_structured_output": true                  (false = schema in the prompt instead)
The existing "astra_reasoning" (low/medium/high/xhigh) becomes Claude's effort level.

Environment: ANTHROPIC_API_KEY (plus ANTHROPIC_WORKSPACE_ID only if the key is not scoped to a workspace). News stays on the configured news provider; with the default
"openai_web" news provider OPENAI_API_KEY is still needed for the news check.

Copy your saved OpenAI agent's instructions into the instructions file once (optional):
    python src/astra_anthropic.py --export-openai-instructions
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROVIDERS = ("openai", "anthropic")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULTS = {
    "analyst_provider": "openai",
    "anthropic_model": DEFAULT_MODEL,
    "anthropic_map_max_tokens": 32000,
    "anthropic_review_max_tokens": 8000,
    "anthropic_instructions_file": "config/astra_instructions.md",
    "anthropic_structured_output": True,
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ENV_SWITCH = "GOLD_ANALYST"          # claude | astra: overrides analyst_provider in the config
ALIASES = {"anthropic": "anthropic", "claude": "anthropic", "opus": "anthropic",
           "openai": "openai", "astra": "openai", "gpt": "openai", "chatgpt": "openai"}
LABELS = {"anthropic": "Claude", "openai": "Astra (OpenAI)"}
LAST_USAGE = None

# JSON-schema keywords the Messages API structured output accepts (see the structured outputs docs).
ALLOWED_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
                    "anyOf", "allOf", "$ref", "$defs", "definitions", "default", "description", "title",
                    "format", "minItems"}
MAX_UNION_PARAMETERS = 16


class AnalystError(ValueError):
    """The model answer cannot be used (refused, truncated, not JSON, wrong schema)."""


# ----------------------------------------------------------------------------- configuration

def normalize(name):
    """'claude' / 'opus' / 'anthropic' -> anthropic; 'astra' / 'gpt' / 'openai' -> openai; else None."""
    return ALIASES.get(str(name).strip().lower()) if name is not None else None


def env_choice():
    """The GOLD_ANALYST environment switch, normalised (None when unset). A typo stops the bot."""
    raw = os.environ.get(ENV_SWITCH, "").strip()
    if not raw:
        return None
    name = normalize(raw)
    if name is None:
        raise ValueError(f"{ENV_SWITCH}={raw!r}: use claude or astra")
    return name


def label(c):
    return LABELS.get(provider(c), provider(c))


def validate_config(c):
    """Fill defaults and check the analyst settings. Called from the monitor's load_config.
    Precedence: GOLD_ANALYST environment variable > analyst_provider in the config > "openai"."""
    for k, v in DEFAULTS.items():
        c.setdefault(k, v)
    chosen = normalize(c["analyst_provider"])
    if chosen is None:
        raise ValueError("analyst_provider must be claude/anthropic or astra/openai")
    c["analyst_provider"] = env_choice() or chosen
    if not isinstance(c["anthropic_model"], str) or not c["anthropic_model"].strip():
        raise ValueError("anthropic_model must be a model id string")
    for k in ("anthropic_map_max_tokens", "anthropic_review_max_tokens"):
        v = c[k]
        if isinstance(v, bool) or not isinstance(v, int) or not 1024 <= v <= 128000:
            raise ValueError(k + " must be an integer 1024..128000")
    if not isinstance(c["anthropic_instructions_file"], str):
        raise ValueError("anthropic_instructions_file must be a path string")
    if not isinstance(c["anthropic_structured_output"], bool):
        raise ValueError("anthropic_structured_output must be true or false")
    return c


def provider(c):
    return (c or {}).get("analyst_provider", "openai")


def required_keys(c):
    """Environment keys the configured analyst + news provider need."""
    keys = []
    if provider(c) == "anthropic":
        keys.append("ANTHROPIC_API_KEY")
    if provider(c) == "openai" or (c.get("providers") or {}).get("news") == "openai_web":
        keys.append("OPENAI_API_KEY")
    return keys


def required_modules(c):
    """{import name: pip package} for the configured analyst + news provider."""
    mods = {}
    if provider(c) == "anthropic":
        mods["anthropic"] = "anthropic"
    if "OPENAI_API_KEY" in required_keys(c):
        mods["openai"] = "openai"
    return mods


def analyst_key_name(config_path=None, override=None):
    """API key the tools need for the analyst call, read from the config without validating it."""
    name = normalize(override) if override else None
    if name is None:
        try:
            name = env_choice()
        except ValueError:
            name = None
    if name is None and config_path and Path(config_path).is_file():
        try:
            name = normalize(json.loads(Path(config_path).read_text(encoding="utf-8-sig")).get("analyst_provider"))
        except Exception:
            name = None
    return "ANTHROPIC_API_KEY" if name == "anthropic" else "OPENAI_API_KEY"


def model_name(c):
    return os.environ.get("GOLD_CLAUDE_MODEL") or c.get("anthropic_model") or DEFAULT_MODEL


def load_analyst(c, root):
    """Claude counterpart of load_saved_agent(): {"model", "instructions", "provider"}."""
    name = c.get("anthropic_instructions_file") or ""
    text = ""
    if name:
        path = Path(name) if Path(name).is_absolute() else Path(root) / name
        if path.is_file():
            if path.stat().st_size > 200_000:
                raise ValueError(f"{path.name} is larger than 200 KB")
            text = path.read_text(encoding="utf-8-sig").strip()
    return {"model": model_name(c), "instructions": text, "provider": "anthropic",
            "structured": c.get("anthropic_structured_output", True)}


WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"   # needed by keys not scoped to one workspace (e.g. personal sk-ant-usr- keys)


def make_client(c, retries=0):
    import anthropic
    headers = {}
    workspace = os.environ.get(WORKSPACE_ENV, "").strip()
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    kwargs = {"timeout": min(c.get("ai_deadline_seconds", 480), 600), "max_retries": retries,
              "default_headers": headers or None}
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"\'')
    if key:
        # Authorization: Bearer is the documented primary method and works for personal (sk-ant-usr-)
        # and service-account keys as well as classic keys; the legacy x-api-key header is not sent.
        kwargs["auth_token"] = key
    return anthropic.Anthropic(**kwargs)


# ----------------------------------------------------------------------------- schema helpers

def schema_issues(schema):
    """Keywords the structured-output grammar would reject (offline check, used by tests)."""
    issues, unions = [], 0

    def walk(node, path):
        nonlocal unions
        if isinstance(node, dict):
            for k, v in node.items():
                if k not in ALLOWED_KEYWORDS:
                    issues.append(f"{path}: unsupported keyword {k!r}")
                if k == "additionalProperties" and v is not False:
                    issues.append(f"{path}: additionalProperties must be false")
                if k == "minItems" and v not in (0, 1):
                    issues.append(f"{path}: minItems must be 0 or 1")
                if k == "type" and isinstance(v, list):
                    unions += 1
                if k == "anyOf":
                    unions += 1
            for k, v in (node.get("properties") or {}).items():
                walk(v, f"{path}.{k}")
            for k in ("items",):
                if isinstance(node.get(k), dict):
                    walk(node[k], path + "[]")
            for k in ("anyOf", "allOf"):
                for i, sub in enumerate(node.get(k) or []):
                    walk(sub, f"{path}.{k}[{i}]")
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                issues.append(f"{path}: object without additionalProperties false")
    walk(schema, "$")
    if unions > MAX_UNION_PARAMETERS:
        issues.append(f"{unions} union-type parameters (limit {MAX_UNION_PARAMETERS})")
    return issues


def extract_json(text):
    """The JSON object in a prompt-mode answer (tolerates a markdown fence or a short preamble)."""
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    if not t.startswith("{"):
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise AnalystError("No JSON object in model answer")
        t = t[a:b + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as exc:
        raise AnalystError(f"Model answer is not valid JSON: {exc.msg}") from None


def _schema_rejected(exc):
    msg = str(getattr(exc, "message", "") or exc).lower()
    return any(w in msg for w in ("schema", "output_config", "too complex", "json_schema", "grammar"))


def _usage(msg):
    u = getattr(msg, "usage", None)
    if u is None:
        return None
    try:
        d = u.model_dump()
    except Exception:
        d = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens")}
    d["total_tokens"] = sum(d.get(k) or 0 for k in ("input_tokens", "output_tokens",
                                                    "cache_creation_input_tokens", "cache_read_input_tokens"))
    return d


# ----------------------------------------------------------------------------- the model call

def _request(client, model, system, content, effort, max_tokens, schema, structured, timeout):
    system_text = system
    output_config = {"effort": effort}
    if structured:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    else:
        system_text += ("\n\nOUTPUT FORMAT: return ONLY one JSON object that validates against this JSON "
                        "Schema. No markdown, no text before or after it.\n"
                        + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    return client.messages.create(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        extra_body={"output_config": output_config}, timeout=timeout)


def call_json(client, model, schema, system, content, effort="medium", max_tokens=8000, structured=True,
              timeout=600):
    """One Messages API call returning a dict that strictly validates against `schema`.

    `content` is a string or a list of content blocks (text/image). Structured output is used when
    possible; if the API refuses the schema itself, the call is repeated once with the schema in
    the prompt. Refusals, truncation and non-conforming JSON raise AnalystError (fail closed)."""
    global LAST_USAGE
    from jsonschema import validate
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    try:
        msg = _request(client, model, system, content, effort, max_tokens, schema, structured, timeout)
    except Exception as exc:
        if not (structured and type(exc).__name__ == "BadRequestError" and _schema_rejected(exc)):
            raise
        structured = False
        msg = _request(client, model, system, content, effort, max_tokens, schema, False, timeout)
    LAST_USAGE = _usage(msg)
    if msg.stop_reason == "refusal":
        raise AnalystError("Model declined the request (stop_reason=refusal)")
    if msg.stop_reason == "max_tokens":
        raise AnalystError(f"Answer cut off at max_tokens={max_tokens}; raise anthropic_*_max_tokens")
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise AnalystError("Empty model answer")
    result = json.loads(text) if structured else extract_json(text)
    validate(result, schema)
    json.dumps(result, allow_nan=False)
    return result


def payload_text(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


# ----------------------------------------------------------------------------- live bot worker

def ai_worker(M, kind, payload, c, saved, pipe):
    """Same contract as the monitor's ai_worker, with Claude as the analyst.
    Runs in the isolated spawned process; the parent can terminate it at any time."""
    try:
        client = make_client(c)
        if saved is None or saved.get("provider") != "anthropic":
            saved = load_analyst(c, M.ROOT)
        pipe.send(("progress", f"Astra ({saved['model']}) technical analysis started"
                   + (" with news research" if kind == "review" else "")))
        instructions = M.astra_instructions(saved, kind)
        schema = M.MAP_SCHEMA if kind == "map" else M.REVIEW_SCHEMA
        news_provider = M.build_provider("news", c)

        def news_client():
            if c["providers"]["news"] != "openai_web":
                return None
            from openai import OpenAI
            return OpenAI(timeout=min(c["ai_deadline_seconds"], 360), max_retries=0)

        def safe_news():
            try:
                return news_provider.check(payload, client=news_client())
            except Exception as exc:
                return {"risk": "UNKNOWN", "summary": "News check unavailable", "facts": [], "opinions": [],
                        "checked_at": M.iso(M.utcnow()), "error_type": type(exc).__name__}

        def technical(tokens):
            out = call_json(client, saved["model"], schema, instructions, payload_text(payload),
                            c["astra_reasoning"], tokens, c["anthropic_structured_output"])
            M.LAST_USAGE = LAST_USAGE
            return out

        if kind == "map":
            result, news = technical(c["anthropic_map_max_tokens"]), None
        else:
            import concurrent.futures as futures
            with futures.ThreadPoolExecutor(max_workers=2) as pool:
                tech = pool.submit(technical, c["anthropic_review_max_tokens"])
                web = pool.submit(safe_news)
                result, news = tech.result(), web.result()
        pipe.send(("result", {"technical": result, "news": news, "saved": saved,
                              "usage": dict(LAST_USAGE or {}, model=saved["model"])}))
    except Exception as exc:
        detail = getattr(M, "error_detail", None)
        pipe.send(("error", detail(exc) if detail else f"{type(exc).__name__}: {str(exc)[:300]}"))
    finally:
        pipe.close()


# ----------------------------------------------------------------------------- tools

MONITOR_SIGNATURE = "def ai_worker(kind, payload, c, saved, pipe):"


def monitor_module(src_dir):
    """Import the bot's main module from src/ whatever it is called (gold_monitor_v51, gold_monitor...)."""
    import importlib
    src_dir = Path(src_dir)
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    names = []
    for p in sorted(src_dir.glob("*.py")):
        if p.name == Path(__file__).name:
            continue
        try:
            if MONITOR_SIGNATURE in p.read_text(encoding="utf-8", errors="replace"):
                names.append(p.stem)
        except OSError:
            continue
    if not names:
        raise ImportError(f"no bot module with {MONITOR_SIGNATURE!r} in {src_dir}")
    v6 = [n for n in names if "astra_anthropic" in (src_dir / (n + ".py")).read_text(encoding="utf-8", errors="replace")]
    names = v6 or names                     # the patched bot, not an old gold_monitor_v5.py beside it
    preferred = [n for n in ("gold_monitor_v51", "gold_monitor") if n in names]
    return importlib.import_module((preferred or names)[0])


def dry_run_map(M, c, payload):
    """tools/astra_dry_run.py: (result, model, usage, seconds, instruction_chars)."""
    import time
    client = make_client(c)
    saved = load_analyst(c, M.ROOT)
    instructions = M.astra_instructions(saved, "map")
    started = time.time()
    try:
        result = call_json(client, saved["model"], M.MAP_SCHEMA, instructions, payload_text(payload),
                           c["astra_reasoning"], c["anthropic_map_max_tokens"], c["anthropic_structured_output"])
    finally:
        M.LAST_USAGE = LAST_USAGE
    M.strict_validate(result, M.MAP_SCHEMA)
    return result, saved["model"], dict(LAST_USAGE or {}), time.time() - started, len(instructions)


def to_blocks(parts):
    """OpenAI Responses input parts (input_text / input_image data URL) -> Anthropic content blocks."""
    blocks = []
    for p in parts:
        if p.get("type") == "input_text":
            blocks.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "input_image":
            m = re.fullmatch(r"data:(image/[\w.+-]+);base64,(.+)", p["image_url"], re.S)
            if not m:
                raise ValueError("Only base64 data-URL images are supported")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1),
                                                       "data": m.group(2)}})
        else:
            raise ValueError(f"Unsupported lesson part {p.get('type')!r}")
    return blocks


def lesson_ask(client, saved, schema, instructions, parts, effort, tokens):
    """tools/learn_lessons.py ask(): (result, tokens_used). Extra room for adaptive thinking."""
    result = call_json(client, saved["model"], schema, instructions, to_blocks(parts), effort,
                       tokens + 8000, saved.get("structured", True))
    return result, (LAST_USAGE or {}).get("total_tokens")


def export_openai_instructions(root, target=None):
    """Write the saved OpenAI Astra agent's instructions into the Claude instructions file."""
    from openai import OpenAI
    M = monitor_module(Path(root) / "src")
    saved = M.load_saved_agent(OpenAI(timeout=30, max_retries=1))
    if not saved["instructions"].strip():
        raise SystemExit("The saved agent has no instructions (or GOLD_USE_SAVED_AGENT=0).")
    path = Path(target) if target else Path(root) / DEFAULTS["anthropic_instructions_file"]
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())
        print("Previous file kept as", backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(saved["instructions"].strip() + "\n", encoding="utf-8")
    print(f"Wrote {len(saved['instructions']):,} characters from {saved['model']} to {path}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-openai-instructions", action="store_true",
                    help="copy the saved OpenAI agent's instructions into config/astra_instructions.md")
    ap.add_argument("--to", help="target file (default config/astra_instructions.md)")
    args = ap.parse_args()
    if args.export_openai_instructions:
        export_openai_instructions(Path(__file__).resolve().parents[1], args.to)
    else:
        ap.print_help()
