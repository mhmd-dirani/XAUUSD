#!/usr/bin/env python3
"""Check the Anthropic key and workspace setup. Free: it only lists models, no market map is sent.

    .\\.venv\\Scripts\\python.exe tools\\check_claude_key.py

Prints only the start of the key, never the whole secret.
"""
import os
import sys


def describe(name):
    raw = os.environ.get(name)
    if raw is None:
        return f"{name}: NOT SET"
    clean = raw.strip().strip('"\'')
    notes = []
    if raw != clean:
        notes.append("has surrounding spaces/quotes (the bot strips them)")
    if any(ord(ch) > 126 or ch.isspace() for ch in clean):
        notes.append("contains spaces, line breaks or non-ASCII characters: copy it again")
    return f"{name}: starts {clean[:12]!r}, length {len(clean)}" + (" | " + "; ".join(notes) if notes else "")


def main():
    print(describe("ANTHROPIC_API_KEY"))
    print(describe("ANTHROPIC_WORKSPACE_ID"))
    for other in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        if os.environ.get(other):
            print(f"WARNING: {other} is set; remove it unless you know you need it")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"\'')
    if not key:
        print("No key: setx ANTHROPIC_API_KEY \"...\" and open a new window.")
        return 1
    try:
        import anthropic
    except ImportError:
        print("anthropic package missing: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1
    print("anthropic SDK", anthropic.__version__)
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    ok = False
    for label, kwargs in (("Authorization: Bearer (what the bot uses)", {"auth_token": key}),
                          ("x-api-key (legacy header)", {"api_key": key})):
        client = anthropic.Anthropic(timeout=30, max_retries=0, default_headers=headers, **kwargs)
        try:
            models = client.models.list(limit=20)
            ids = [m.id for m in models.data]
            print(f"OK   {label}: key accepted; models include {', '.join(ids[:6])}")
            ok = True
        except Exception as exc:
            status = getattr(exc, "status_code", "")
            body = getattr(exc, "body", None) or str(exc)
            print(f"FAIL {label}: {type(exc).__name__} {status} {str(body)[:300]}")
    if not ok:
        print("\nThe key is not accepted at all. In the Console (Settings -> API keys): check the key is still "
              "listed and active, create a NEW key, copy it with the copy button, run\n"
              '  setx ANTHROPIC_API_KEY "<new key>"\n'
              "then close VS Code completely, reopen it and run this check again.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
