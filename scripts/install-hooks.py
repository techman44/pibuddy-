#!/usr/bin/env python3
"""Install PiBuddy hooks into Claude Code settings.

Run this on each machine where you use Claude Code (not on the Pi):

    python3 scripts/install-hooks.py --url http://pibuddy.local:8765

It copies the hook scripts to ~/.claude/pibuddy/ and merges hook entries
into ~/.claude/settings.json (a timestamped backup is written first).
Existing PiBuddy entries are replaced; everything else is left alone.

By default only the fire-and-forget event hooks are installed. Add
--approvals to also route PreToolUse permission decisions to the Pi's
touchscreen for matching tools:

    python3 scripts/install-hooks.py --url http://pi:8765 --approvals Bash

Use --uninstall to remove everything PiBuddy added.
"""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
import time
from pathlib import Path

EVENT_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
)
MARKER = "pibuddy"  # our commands are recognized by this substring


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def build_command(script: Path, url: str, token: str, extra_env: str = "") -> str:
    env = f"PIBUDDY_URL={sh_quote(url)}"
    if token:
        env += f" PIBUDDY_TOKEN={sh_quote(token)}"
    if extra_env:
        env += f" {extra_env}"
    return f"{env} bash {sh_quote(str(script))}"


def is_pibuddy_entry(entry: dict) -> bool:
    for hook in entry.get("hooks", []):
        if MARKER in hook.get("command", ""):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://pibuddy.local:8765", help="PiBuddy server URL")
    parser.add_argument("--token", default="", help="shared secret configured on the Pi")
    parser.add_argument(
        "--approvals",
        metavar="MATCHER",
        help="also install the blocking touchscreen approval hook for tools "
        "matching this pattern (e.g. 'Bash' or 'Bash|Write|Edit')",
    )
    parser.add_argument(
        "--approval-wait", type=int, default=45, help="seconds to wait for a tap (default 45)"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("~/.claude/settings.json").expanduser(),
        help="Claude Code settings file to modify",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove PiBuddy hooks")
    args = parser.parse_args()

    settings_path = args.settings
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            print(f"error: {settings_path} is not valid JSON; fix it first", file=sys.stderr)
            return 1
        backup = settings_path.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(settings_path, backup)
        print(f"backed up settings to {backup}")

    hooks_cfg = settings.setdefault("hooks", {})

    # Drop any existing PiBuddy entries (this is also how --uninstall works).
    for event, entries in list(hooks_cfg.items()):
        kept = [e for e in entries if not is_pibuddy_entry(e)]
        if kept:
            hooks_cfg[event] = kept
        else:
            del hooks_cfg[event]

    if not args.uninstall:
        src_dir = Path(__file__).resolve().parent.parent / "hooks"
        dest_dir = Path("~/.claude/pibuddy").expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in ("pibuddy-event.sh", "pibuddy-approve.sh"):
            dest = dest_dir / name
            shutil.copy2(src_dir / name, dest)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR)
        print(f"installed hook scripts to {dest_dir}")

        event_cmd = build_command(dest_dir / "pibuddy-event.sh", args.url, args.token)
        for event in EVENT_HOOKS:
            hooks_cfg.setdefault(event, []).append({"hooks": [{"type": "command", "command": event_cmd}]})

        if args.approvals:
            approve_cmd = build_command(
                dest_dir / "pibuddy-approve.sh",
                args.url,
                args.token,
                extra_env=f"PIBUDDY_APPROVAL_WAIT={args.approval_wait}",
            )
            hooks_cfg.setdefault("PreToolUse", []).append(
                {
                    "matcher": args.approvals,
                    "hooks": [
                        {
                            "type": "command",
                            "command": approve_cmd,
                            "timeout": args.approval_wait + 15,
                        }
                    ],
                }
            )

    if not hooks_cfg:
        settings.pop("hooks", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    verb = "removed from" if args.uninstall else "written to"
    print(f"PiBuddy hooks {verb} {settings_path}")
    if not args.uninstall:
        print(f"events will be sent to {args.url}")
        if args.approvals:
            print(f"touchscreen approvals enabled for tools matching: {args.approvals}")
        print("restart running Claude Code sessions (or /hooks) to pick up changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
