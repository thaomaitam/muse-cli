"""muse-cli: CLI for your personal muse.ai agent. No browser needed after cookie export.

One-time setup (also printed by `muse-cli auth export` when a step is missing):
  1. npm i -g agent-browser
  2. In Chrome, open chrome://inspect/#remote-debugging and turn remote debugging on.
  3. Open https://muse.ai/ in that same window and log in. Leave the tab open.
  4. muse-cli auth export   # saves session cookies locally (chmod 600)

Then: muse-cli status | muse-cli threads | muse-cli history | muse-cli send "hello" | ...
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

from . import __version__
from .gateway import Gateway, AuthError, GatewayError, create_invite, fetch_invite, fetch_subscription, redeem_invite_code, load_cookies
from .update import cmd_update, finish_update_check, start_update_check

# -- Profiles -------------------------------------------------------------------
# The `default` profile is the original layout, ~/.config/muse-cli/{config.json,
# cookies.txt,chat.json}, and never moves. A named profile is a sibling login
# at ~/.config/muse-cli/profiles/NAME/ with its own copies of those files.
DEFAULT_PROFILE = "default"
PROFILE_ENV = "MUSE_CLI_PROFILE"
_PROFILE_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,64}(?<!\.)$")


def base_config_dir():
    """~/.config/muse-cli, resolved now (so a changed HOME is honored)."""
    return os.path.expanduser("~/.config/muse-cli")


def validate_profile_name(name):
    """Return NAME if it is a safe slug ([A-Za-z0-9._-], 1-64 chars, no leading/trailing '.'), else raise ValueError."""
    if not isinstance(name, str) or not _PROFILE_RE.match(name):
        raise ValueError(
            f"invalid profile name {name!r}: use 1-64 letters, digits, '.', '-' or '_' (not starting or ending with '.')")
    return name


def resolve_profile(flag=None, environ=None):
    """Active profile name: --profile flag, then $MUSE_CLI_PROFILE, then `default`."""
    if flag is not None:
        return validate_profile_name(flag)
    env = (os.environ if environ is None else environ).get(PROFILE_ENV)
    if env is not None and env.strip():
        return validate_profile_name(env.strip())
    return DEFAULT_PROFILE


def profile_dir(name):
    name = validate_profile_name(name)
    if name == DEFAULT_PROFILE:
        return base_config_dir()
    return os.path.join(base_config_dir(), "profiles", name)


def profile_paths(name):
    d = profile_dir(name)
    return {"dir": d, "config": os.path.join(d, "config.json"),
            "cookies": os.path.join(d, "cookies.txt"), "chat": os.path.join(d, "chat.json")}


PROFILE = DEFAULT_PROFILE
CONFIG_DIR = base_config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COOKIES_FILE = os.path.join(CONFIG_DIR, "cookies.txt")


def activate_profile(name):
    """Point CONFIG_DIR/CONFIG_FILE/COOKIES_FILE at profile NAME. Creates nothing."""
    global PROFILE, CONFIG_DIR, CONFIG_FILE, COOKIES_FILE
    paths = profile_paths(name)
    PROFILE = name
    CONFIG_DIR, CONFIG_FILE, COOKIES_FILE = paths["dir"], paths["config"], paths["cookies"]
    return paths


def ensure_config_dir():
    """Create the active profile's folder. Named profiles get mode 700; the
    default folder is created exactly as before (it predates profiles)."""
    if PROFILE == DEFAULT_PROFILE:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        return CONFIG_DIR
    os.makedirs(os.path.dirname(CONFIG_DIR), mode=0o700, exist_ok=True)
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    return CONFIG_DIR


def cli_cmd(rest):
    """`muse-cli REST` as the user should type it for the active profile."""
    if PROFILE == DEFAULT_PROFILE:
        return f"muse-cli {rest}"
    return f"muse-cli --profile {PROFILE} {rest}"


def _read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_profiles():
    """Every profile (default first, then profiles/*), read-only: no network, no writes."""
    names = [DEFAULT_PROFILE]
    pdir = os.path.join(base_config_dir(), "profiles")
    try:
        found = sorted(n for n in os.listdir(pdir)
                       if os.path.isdir(os.path.join(pdir, n)) and _PROFILE_RE.match(n)
                       and n != DEFAULT_PROFILE)
    except OSError:
        found = []
    names += found
    if PROFILE not in names:
        names.append(PROFILE)
    rows = []
    for n in names:
        paths = profile_paths(n)
        try:
            has_cookies = os.path.getsize(paths["cookies"]) > 0
        except OSError:
            has_cookies = False
        cfg = _read_json(paths["config"])
        rows.append({"name": n, "active": n == PROFILE, "dir": paths["dir"],
                     "exists": os.path.isdir(paths["dir"]), "cookies": has_cookies,
                     "cookies_file": paths["cookies"],
                     "auto_approve": str(cfg.get("auto_approve") or "off").lower()})
    return rows


def cmd_auth_list(args):
    rows = list_profiles()
    if getattr(args, "json", False):
        out({"active": PROFILE, "profiles": rows})
        return
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        mark = "*" if r["active"] else " "
        state = "cookies" if r["cookies"] else ("no cookies" if r["exists"] else "not created")
        print(f"{mark} {r['name']:<{width}}  {state:<11}  {r['dir']}")


def load_config():
    import secrets
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        cfg = json.load(open(CONFIG_FILE))
    changed = False
    # A named profile never borrows another profile's login: a config copied
    # from elsewhere still reads this profile's cookies.
    cf = cfg.get("cookies_file")
    if (PROFILE != DEFAULT_PROFILE and cf and cf != COOKIES_FILE
            and os.path.abspath(cf).startswith(base_config_dir() + os.sep)):
        cfg["cookies_file"] = COOKIES_FILE
    cfg.setdefault("cookies_file", COOKIES_FILE)
    if "vm_id" not in cfg and os.environ.get("MUSE_VM_ID"):
        cfg["vm_id"] = os.environ["MUSE_VM_ID"]
    if "node_id" not in cfg:
        if os.environ.get("MUSE_NODE_ID"):
            cfg["node_id"] = os.environ["MUSE_NODE_ID"]
        else:
            cfg["node_id"] = secrets.token_hex(8)
            changed = True
    if changed:
        ensure_config_dir()
        json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)
    return cfg


def save_config(cfg):
    ensure_config_dir()
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_FILE)


AUTO_APPROVE_ON = {"always", "on", "1", "true", "yes"}
AUTO_APPROVE_OFF = {"off", "0", "false", "no", "never", ""}


def auto_approve_setting(cfg, flag=None):
    """(enabled, source). Precedence: per-call flag, MUSE_AUTO_APPROVE, config.

    Enabled means: `wait` and `send --wait/--until-done` approve every pending
    approval with allow_always and keep waiting. Default is off.
    """
    if flag is not None:
        return bool(flag), "flag"
    env = os.environ.get("MUSE_AUTO_APPROVE")
    if env is not None:
        v = env.strip().lower()
        if v in AUTO_APPROVE_ON:
            return True, "env MUSE_AUTO_APPROVE"
        if v in AUTO_APPROVE_OFF:
            return False, "env MUSE_AUTO_APPROVE"
        raise ValueError(f"MUSE_AUTO_APPROVE must be always or off, not {env!r}")
    return str(cfg.get("auto_approve") or "off").lower() == "always", f"config {CONFIG_FILE}"


def cmd_auto_approve(args):
    cfg = load_config()
    if args.state in ("on", "always"):
        cfg["auto_approve"] = "always"
        save_config(cfg)
    elif args.state == "off":
        cfg["auto_approve"] = "off"
        save_config(cfg)
    enabled, source = auto_approve_setting(cfg)
    out({"auto_approve": "always" if enabled else "off", "source": source,
         "config": cfg.get("auto_approve") or "off",
         "env": os.environ.get("MUSE_AUTO_APPROVE")})


def connect(cfg):
    if not os.path.exists(cfg["cookies_file"]):
        raise AuthError(
            f"no cookies at {cfg['cookies_file']}. Run `{cli_cmd('auth export')}` "
            "and follow the steps it prints: turn on Chrome remote debugging at "
            "chrome://inspect/#remote-debugging, open https://muse.ai/ in that "
            "window, then export again.")
    cookies = load_cookies(cfg["cookies_file"])
    if not cookies.strip():
        raise AuthError(f"cookies file {cfg['cookies_file']} is empty; run `{cli_cmd('auth export')}`")
    import hashlib
    key = hashlib.sha256(cookies.encode()).hexdigest()
    cached = _TOKENS.get(key)
    if cached:
        # Reconnect with the VM id and tokens of the last good connection:
        # skips 3 HTTPS round trips (api/session, auth/check, hatch/token).
        try:
            return Gateway(cookies, vm_id=cached["vm_id"], access_token=cached["access_token"],
                           hatch_token=cached["hatch_token"])
        except AuthError:
            raise
        except Exception:  # noqa: BLE001 - stale token or VM moved: fetch fresh ones
            _TOKENS.pop(key, None)
    gw = Gateway(cookies, vm_id=cfg.get("vm_id"))
    _TOKENS[key] = {"vm_id": gw.vm_id, "access_token": getattr(gw, "access_token", None),
                    "hatch_token": getattr(gw, "hatch_token", None)}
    if not _TOKENS[key]["hatch_token"]:
        _TOKENS.pop(key, None)
    return gw


_TOKENS = {}   # sha256(cookies) -> vm_id + tokens of the last good connection (memory only)


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _parse_browser_doc(stdout):
    """agent-browser prints a JSON envelope, sometimes after a warning line."""
    text = (stdout or "").strip()
    if not text:
        return None
    blobs = [text]
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line not in blobs:
            blobs.append(line)
    for blob in blobs:
        try:
            doc = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def _interpret_browser(returncode, stdout, stderr, require_json):
    """Turn one agent-browser invocation into data, or raise RuntimeError.

    `tab <id>` confirms success with a human line (`✓ Title` plus the URL)
    and exit 0. That is not a failure. A real failure is `success: false`
    or a non-zero exit.
    """
    doc = _parse_browser_doc(stdout) or _parse_browser_doc(stderr)
    if isinstance(doc, dict) and doc.get("success") is False:
        raise RuntimeError(str(doc.get("error") or "unknown error")[:400])
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
        if "cookies" in doc or "tabs" in doc:
            return doc
        return data if isinstance(data, dict) else (doc if doc.get("success") else {})
    text = (stderr or stdout or "").strip()
    if returncode == 0 and not require_json:
        return {}
    raise RuntimeError(text[:400] or f"exit {returncode}")


def _browser_run(argv, require_json=True):
    """Run agent-browser and return the JSON envelope's data.

    On failure the tool prints {"success": false, "error": ...}. Some versions
    exit 0 anyway, some exit non-zero; trust the envelope when it is there.
    """
    import subprocess
    r = subprocess.run(argv, capture_output=True, text=True)
    return _interpret_browser(r.returncode, r.stdout, r.stderr, require_json)


def _export_error_kind(err):
    e = (err or "").lower()
    if "daemon already running" in e:
        return "daemon"
    if ("no running chrome" in e or "remote-debugging" in e
            or "auto-launch failed" in e or "auto-connect" in e):
        return "debug"
    if err == "no muse.ai tab open in Chrome":
        return "tab"
    # A page title from agent-browser means Chrome already answered.
    if "https://muse.ai" in e or e.startswith("✓") or e.startswith("connected to chrome"):
        return "connected"
    return "other"


def _print_hand_copy():
    print("Copy the login by hand instead:", file=sys.stderr)
    print("  1. In Chrome, open https://muse.ai/ and log in.", file=sys.stderr)
    print("  2. DevTools (F12) → Application → Cookies → https://muse.ai", file=sys.stderr)
    print(f"  3. Write one line to {COOKIES_FILE}", file=sys.stderr)
    print("       hatch_sess=VALUE; other_name=other_value", file=sys.stderr)
    print("     hatch_sess is required. Separate cookies with '; '.", file=sys.stderr)
    if PROFILE != DEFAULT_PROFILE and not os.path.isdir(CONFIG_DIR):
        print(f"     Create its folder first: mkdir -p -m 700 {CONFIG_DIR}", file=sys.stderr)
    print(f"  4. chmod 600 {COOKIES_FILE}", file=sys.stderr)
    print(f"  5. {cli_cmd('status')}", file=sys.stderr)


def _print_chrome_debug_steps():
    print("Turn on remote debugging so muse-cli can read the muse.ai cookies.", file=sys.stderr)
    print("Do this in the Chrome window where you already use muse.ai:", file=sys.stderr)
    print(file=sys.stderr)
    print("  1. Open chrome://inspect/#remote-debugging", file=sys.stderr)
    print("  2. Turn on remote debugging", file=sys.stderr)
    print('     ("Allow remote debugging for this browser instance").', file=sys.stderr)
    print("  3. Open https://muse.ai/ and log in. Leave that tab open.", file=sys.stderr)
    print(f"  4. Run `{cli_cmd('auth export')}` again.", file=sys.stderr)
    print("     If Chrome asks to allow the connection, click Allow.", file=sys.stderr)
    print("     If this command already failed, run it again after Allow.", file=sys.stderr)
    print(file=sys.stderr)
    print("Stay in that same window. A second Chrome started with", file=sys.stderr)
    print("--remote-debugging-port is another profile and has no muse.ai login.", file=sys.stderr)
    print(file=sys.stderr)
    print("Success looks like:", file=sys.stderr)
    print(f"  saved N muse.ai cookies to {COOKIES_FILE}", file=sys.stderr)
    print(file=sys.stderr)
    _print_hand_copy()


def _print_need_agent_browser():
    print("agent-browser is not installed, so muse-cli cannot read Chrome's cookies.", file=sys.stderr)
    print(file=sys.stderr)
    print("  1. Install Node.js LTS if `node --version` fails: https://nodejs.org", file=sys.stderr)
    print("  2. Open a new terminal, then:", file=sys.stderr)
    print("       npm i -g agent-browser", file=sys.stderr)
    print("       agent-browser --version", file=sys.stderr)
    print(f"  3. Run `{cli_cmd('auth export')}` again and follow the Chrome steps it prints.",
          file=sys.stderr)
    print(file=sys.stderr)
    _print_hand_copy()


def _print_export_failure(err):
    kind = _export_error_kind(err)
    print("cookie export failed.", file=sys.stderr)
    if kind == "daemon":
        print("agent-browser is still running from an earlier attempt. Close it, then retry:", file=sys.stderr)
        print("  agent-browser close", file=sys.stderr)
        print(f"  {cli_cmd('auth export')}", file=sys.stderr)
        print(file=sys.stderr)
        _print_chrome_debug_steps()
        return
    if kind == "debug":
        _print_chrome_debug_steps()
        return
    if kind == "tab":
        print("Chrome is connected, but no muse.ai tab is open.", file=sys.stderr)
        print("Open https://muse.ai/, log in, leave that tab open, and run", file=sys.stderr)
        print(f"`{cli_cmd('auth export')}` again.", file=sys.stderr)
        return
    if kind == "connected":
        print("Chrome is connected and the muse.ai tab is open.", file=sys.stderr)
        print("Remote debugging is already on. The cookie read did not come back.", file=sys.stderr)
        print(f"Run `{cli_cmd('auth export')}` again. If Chrome asks, click Allow.", file=sys.stderr)
        print(file=sys.stderr)
        _print_hand_copy()
        return
    print(err, file=sys.stderr)
    print(file=sys.stderr)
    print(f"Check both of these, then run `{cli_cmd('auth export')}` again:", file=sys.stderr)
    print("  - chrome://inspect/#remote-debugging has remote debugging on", file=sys.stderr)
    print("  - https://muse.ai/ is open in that same Chrome and you are logged in", file=sys.stderr)
    print(file=sys.stderr)
    _print_hand_copy()


def _browser_cookies():
    """Read muse.ai cookies from the user's Chrome via agent-browser.

    Auto-connect attaches to the real profile. A fresh browser has no login,
    so this never falls back to launching one.
    """
    base = ["agent-browser", "--headed", "--auto-connect"]
    last_err = "unknown error"
    for attempt in range(3):
        try:
            # Cookies follow the active tab: focus a muse.ai tab first,
            # else the export comes back empty even when logged in.
            data = _browser_run(base + ["tab", "list", "--json"])
            tabs = data.get("tabs", []) if isinstance(data, dict) else []
            muse_tabs = [t for t in tabs
                         if isinstance(t, dict) and "muse.ai" in (t.get("url") or "")
                         and (t.get("id") or t.get("tabId"))]
            if not muse_tabs:
                return None, "no muse.ai tab open in Chrome"
            tab_id = (muse_tabs[0].get("id") or muse_tabs[0].get("tabId")
                      or muse_tabs[0].get("targetId"))
            # Switching tabs prints a human confirmation unless --json is set.
            # Exit 0 is success either way; the cookies read is the next call.
            _browser_run(base + ["tab", tab_id, "--json"], require_json=False)
            data = _browser_run(base + ["cookies", "get", "--json"])
            raw = []
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict):
                for key in ("cookies", "items"):
                    if isinstance(data.get(key), list):
                        raw = data[key]
                        break
            jar = [c for c in raw if isinstance(c, dict) and "muse.ai" in (c.get("domain") or c.get("url") or "")]
            return jar, None
        except RuntimeError as e:
            last_err = str(e)
            # Missing debug port and a stuck daemon will not change on retry.
            if _export_error_kind(last_err) in ("debug", "daemon") or attempt == 2:
                return None, last_err
            time.sleep(2)
    return None, last_err


def cmd_auth_export(_args):
    ensure_config_dir()
    if shutil.which("agent-browser") is None:
        _print_need_agent_browser()
        sys.exit(1)
    print("Reading muse.ai cookies from Chrome...", file=sys.stderr)
    jar, err = _browser_cookies()
    if jar is None:
        _print_export_failure(err)
        sys.exit(1)
    # Never clobber a working login with an empty or logged-out jar.
    if not any(c["name"] == "hatch_sess" for c in jar):
        print("Chrome answered, but the cookies do not include hatch_sess.", file=sys.stderr)
        print("That cookie is set only after you log in at https://muse.ai/.", file=sys.stderr)
        print(f"Log in, leave the muse.ai tab open, and run `{cli_cmd('auth export')}` again.",
              file=sys.stderr)
        print(f"Left {COOKIES_FILE} unchanged.", file=sys.stderr)
        sys.exit(1)
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        dom = c["domain"]
        lines.append("\t".join([
            dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expires", 0) or 0)), c["name"], c["value"],
        ]))
    if os.path.exists(COOKIES_FILE):
        os.replace(COOKIES_FILE, COOKIES_FILE + ".bak")
    with open(COOKIES_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(COOKIES_FILE, 0o600)
    print(f"saved {len(jar)} muse.ai cookies to {COOKIES_FILE}")


def status_data(gw):
    """VM id, session count, unread counts and identity (what `status` prints)."""
    sess = gw.call_json("sessions.list")
    unread = gw.call_json("chat.unread_count")
    ident = gw.call_json("identity")
    return {"vm_id": gw.vm_id, "sessions": len(sess.get("sessions", [])),
            "unread": unread, "identity": ident}


def cmd_status(_args):
    gw = connect(load_config())
    try:
        out(status_data(gw))
    finally:
        gw.close()


_SCALE = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12,
          "nghìn": 10**3, "triệu": 10**6, "tỷ": 10**9}
_TOKENS_LEFT_RE = re.compile(
    r"(?:còn\s*)?([\d][\d.,]*)\s*(K|M|B|T|nghìn|triệu|tỷ)?\s*tokens?(?:\s*left)?", re.I)


def parse_tokens_left(label):
    """'0% used (3B tokens left)' / 'Đã dùng 4% (còn 12 tỷ token)' -> 3e9 / 12e9."""
    if not label:
        return None
    m = _TOKENS_LEFT_RE.search(label)
    if not m:
        return None
    num = m.group(1)
    num = num.replace(",", "") if "." in num or num.count(",") != 1 else num.replace(",", ".")
    unit = m.group(2) or ""
    scale = _SCALE.get(unit.upper() if len(unit) == 1 else unit.lower(), 1)
    try:
        return int(round(float(num) * scale))
    except ValueError:
        return None


def summarize_usage(sub):
    """Flatten fetchSubscriptionAction's result into the numbers that matter.

    topupBalance/topupTotal are NOT tokens (a server-side unit; 120M of them
    showed as "3B tokens left"). The web app uses them only for the bar
    percent and prints the server's topupRowValueLabel verbatim, so do the
    same: percent from the ratio, token count parsed from the label.
    """
    tier = sub.get("tier") or {}
    usage = sub.get("usage") or {}
    resets = usage.get("resetsAt")
    total, balance = sub.get("topupTotal") or 0, sub.get("topupBalance") or 0
    value_label = sub.get("topupRowValueLabel")
    bonus = None
    if total > 0 and value_label is not None and value_label.strip():
        used = total - min(max(balance, 0), total)
        bonus = {"label": sub.get("topupRowLabel") or "Additional tokens",
                 "value_label": value_label,
                 "used_percent": round(100 * used / total),
                 "remaining_tokens": parse_tokens_left(value_label),
                 "balance_units": balance, "total_units": total}
    return {
        "plan": tier.get("name"), "tier": tier.get("tierCode"), "paid": tier.get("isPaid"),
        "label": sub.get("usageRowLabel"),
        "value_label": sub.get("usageRowValueLabel"),
        "state": usage.get("state"),
        "used_percent": usage.get("percentUsed"),
        "quota_status": usage.get("quotaStatus"),
        "resets_at": (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(resets))
                      if resets else None),
        "status": sub.get("statusSubtitle"),
        "bonus_tokens": bonus,
        "upgrade": sub.get("entryPointCtaLabel"),
    }


def _human_tokens(n):
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def fetch_usage(cfg):
    """The raw subscription object; caches the server action id in config."""
    if not os.path.exists(cfg["cookies_file"]):
        connect(cfg)  # raises the usual "run auth export" error
    sub, aid = fetch_subscription(load_cookies(cfg["cookies_file"]),
                                  action_id=cfg.get("subscription_action_id"))
    if cfg.get("subscription_action_id") != aid:
        cfg["subscription_action_id"] = aid
        ensure_config_dir()
        json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)
    return sub


def usage_lines(u):
    """`usage` as human lines, from summarize_usage()."""
    lines = []
    head = u["label"] or u["plan"] or "Plan"
    if u["value_label"]:
        line = f"{head}: {u['value_label']}"
    elif u["used_percent"] is not None:
        line = f"{head}: {u['used_percent']}% used"
    else:
        line = f"{head}: {u['state'] or 'unknown'}"
    if u["resets_at"]:
        line += f" (resets {u['resets_at'][:16].replace('T', ' ')} {time.strftime('%Z')})"
    lines.append(line)
    if u["quota_status"] and u["quota_status"] != "SUFFICIENT":
        lines.append(f"  quota: {u['quota_status']}")
    b = u["bonus_tokens"]
    if b:
        lines.append(f"{b['label']}: {b['value_label']} (never expire)")
    else:
        lines.append("Additional tokens: none")
    if u["upgrade"] and not u["paid"]:
        lines.append("Upgrade: https://muse.ai/settings")
    return lines


def cmd_usage(args):
    sub = fetch_usage(load_config())
    if args.raw:
        out(sub)
        return
    u = summarize_usage(sub)
    if args.json:
        out(u)
        return
    for line in usage_lines(u):
        print(line)


def summarize_invite(d):
    """The invite fields that matter, from GET /api/hatch/invite."""
    reward = d.get("reward") if isinstance(d.get("reward"), dict) else None
    remaining = d.get("uses_remaining")
    if remaining is None and d.get("max_uses") is not None and d.get("use_count") is not None:
        remaining = max(d["max_uses"] - d["use_count"], 0)
    return {
        "state": d.get("invite_state"),
        "code": d.get("code"),
        "link": d.get("invite_link"),
        "max_uses": d.get("max_uses"),
        "used": d.get("use_count"),
        "uses_remaining": remaining,
        "reward": ({"title": reward.get("title"), "body": reward.get("body")} if reward else None),
        "offer": d.get("subtitle"),
        "share_text": d.get("share_text"),
        "redeemed_someone_elses_code": d.get("has_redeemed_invite_code"),
    }


def cmd_invite(args):
    cfg = load_config()
    if not os.path.exists(cfg["cookies_file"]):
        connect(cfg)  # raises the usual "run auth export" error
    cookies = load_cookies(cfg["cookies_file"])
    d = fetch_invite(cookies)
    if args.create and not d.get("code"):
        create_invite(cookies)
        d = fetch_invite(cookies)
    if args.raw:
        out(d)
        return
    s = summarize_invite(d)
    if args.json:
        out(s)
        return
    if s["code"]:
        print(f"Invite code: {s['code']}")
        if s["link"]:
            print(f"Link: {s['link']}")
        if s["uses_remaining"] is not None:
            extra = f" of {s['max_uses']}" if s["max_uses"] is not None else ""
            print(f"Uses left: {s['uses_remaining']}{extra}")
    else:
        print(f"Invite code: none yet (state {s['state']}). Open Invite friends in the "
              "muse.ai app once, or run `muse-cli invite --create`.")
    if s["reward"]:
        print(" ".join(x for x in (s["reward"]["title"], s["reward"]["body"]) if x))
    if s["offer"]:
        print(s["offer"])


def cmd_redeem(args):
    cfg = load_config()
    if not os.path.exists(cfg["cookies_file"]):
        connect(cfg)
    try:
        status, body = redeem_invite_code(load_cookies(cfg["cookies_file"]), args.code)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    if args.json:
        out({"status": status, **body})
    elif body.get("success"):
        print(f"Redeemed: {body.get('redemptionStatus') or 'ok'}")
    else:
        print(f"Not redeemed ({body.get('reason')}): {body.get('detail') or ''}".rstrip(": "))
    if not body.get("success"):
        sys.exit(1)


def session_rows(d, include_archived=False):
    """sessions.list -> the rows `threads` prints."""
    rows = [{
        "session_id": s.get("session_id"),
        "title": s.get("title"),
        "thread": s.get("is_thread"),
        "pinned": s.get("pinned"),
        "archived": s.get("archived"),
        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("updated_at_ms", 0) / 1000)),
        } for s in d.get("sessions", [])]
    if not include_archived:
        rows = [r for r in rows if not r["archived"]]
    return rows


def cmd_threads(args):
    gw = connect(load_config())
    try:
        out(session_rows(gw.call_json("sessions.list"), include_archived=bool(args.archived)))
    finally:
        gw.close()


def fmt_event(e):
    p = e.get("payload", {}) if isinstance(e.get("payload"), dict) else {}
    role = p.get("role") or e.get("event_name")
    text = p.get("display_text") or p.get("content") or ""
    return {"seq": e.get("seq"), "role": role,
            "message_id": p.get("message_id") or e.get("message_id"),
            "text": text}


def cmd_history(args):
    gw = connect(load_config())
    try:
        params = {"limit": args.limit}
        if args.thread:
            params["session_id"] = args.thread
        d = gw.call_json("chat.history", body=params)
        events = d.get("chat_events", [])
        if args.download:
            res = []
            for e in events:
                for r in event_resources(e):
                    if r["path"] not in [x["path"] for x in res]:
                        res.append(r)
            d["downloads"] = save_resources(gw, res, args.download)
            if not args.raw:
                rows = [fmt_event(e) for e in events if (e.get("payload", {}) or {}).get("display_text")
                        or (e.get("payload", {}) or {}).get("content")]
                out({"messages": rows, "downloads": d["downloads"]})
                return
        if args.raw:
            out(d)
            return
        out([fmt_event(e) for e in events if (e.get("payload", {}) or {}).get("display_text")
             or (e.get("payload", {}) or {}).get("content")])
    finally:
        gw.close()


# -- files the agent produced -------------------------------------------------
# Assistant messages carry media as payload.data.{videos,images,...}[]
# entries ({path: "sandbox://workspace/...", mime, byte_len, variants:
# {original: "https://<vm>.metaaivm.com/media/raw/..."}}), and as
# sandbox:// links in fallback_text.
_SANDBOX_RE = re.compile(r"sandbox://[^\s)\]\"'>]+")


def event_resources(ev):
    """List the downloadable files referenced by one history event."""
    p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
    found = {}

    def add(item, kind):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            return
        path = item.get("path") or (item.get("variants") or {}).get("original")
        if not path or not (path.startswith("sandbox://") or "/media/raw/" in path):
            return
        if item.get("missing"):
            return
        r = found.setdefault(path, {"path": path})
        r.setdefault("kind", kind)
        for k in ("mime", "byte_len", "label", "variants"):
            if item.get(k) is not None:
                r[k] = item[k]

    data = p.get("data") if isinstance(p.get("data"), dict) else {}
    for key, val in data.items():
        if isinstance(val, list):
            for item in val:
                add(item, key.rstrip("s"))
    for src in (ev.get("resources"), p.get("resources")):
        for item in src or []:
            add(item, "resource")
    for text in (p.get("fallback_text"), data.get("fallback_text"),
                 p.get("display_text"), p.get("content")):
        if isinstance(text, str):
            for m in _SANDBOX_RE.findall(text):
                add(m, "link")
    return list(found.values())


def _local_name(path, directory):
    name = os.path.basename(path.split("?", 1)[0].rstrip("/")) or "download"
    return os.path.join(directory, name)


def save_resources(gw, resources, directory, overwrite=False):
    """Download each resource into directory; return one result row each."""
    os.makedirs(directory, exist_ok=True)
    rows = []
    for r in resources:
        dest = _local_name(r["path"], directory)
        row = {"path": r["path"], "file": os.path.abspath(dest)}
        if os.path.exists(dest) and not overwrite:
            row.update(skipped="exists", bytes=os.path.getsize(dest))
            rows.append(row)
            continue
        try:
            data = gw.download_file(r)
        except (GatewayError, TimeoutError, ValueError) as e:
            row["error"] = str(e)
            rows.append(row)
            continue
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        row["bytes"] = len(data)
        if r.get("byte_len") and r["byte_len"] != len(data):
            row["warning"] = f"expected {r['byte_len']} bytes"
        rows.append(row)
    return rows


def _history_events(gw, thread, limit):
    params = {"limit": limit}
    if thread:
        params["session_id"] = thread
    return gw.call_json("chat.history", body=params).get("chat_events", [])


def file_rows(events):
    """One row per file referenced by these history events (what `files` prints)."""
    rows = []
    for ev in events:
        p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
        for r in event_resources(ev):
            rows.append({"seq": ev.get("seq"),
                         "message_id": p.get("message_id") or ev.get("message_id"), **r})
    return rows


def cmd_files(args):
    gw = connect(load_config())
    try:
        out(file_rows(_history_events(gw, args.thread, args.limit)))
    finally:
        gw.close()


def cmd_download(args):
    gw = connect(load_config())
    try:
        ref = args.ref
        if ref.startswith("sandbox://") or "/media/raw/" in ref:
            resources = [{"path": ref}]
        else:
            # A message id or history seq: collect that message's files.
            resources = []
            for ev in _history_events(gw, args.thread, args.limit):
                p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
                ids = {str(ev.get("seq")), str(p.get("message_id") or ""),
                       str(ev.get("message_id") or ""), str(p.get("id") or "")}
                if ref in ids:
                    for r in event_resources(ev):
                        if r["path"] not in [x["path"] for x in resources]:
                            resources.append(r)
            if not resources:
                print(f"no files found for '{ref}' in the last {args.limit} messages"
                      + (f" of thread {args.thread}" if args.thread else " of the main chat")
                      + " (try --thread / --limit)", file=sys.stderr)
                sys.exit(1)
        rows = save_resources(gw, resources, args.output, overwrite=args.force)
        out(rows)
        if any("error" in r for r in rows):
            sys.exit(3)
    finally:
        gw.close()


def is_reply(ev, baseline):
    """True for a genuine assistant reply (not a proactive push).

    In history, genuine replies are message.assistant events with an empty
    reply_to_message_id; proactive pushes (Telegram drafts, background task
    updates) are self-referential there.
    """
    if ev.get("event_name") != "message.assistant":
        return False
    if (ev.get("seq") or 0) <= baseline:
        return False
    p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
    if (ev.get("reply_to_message_id") or p.get("reply_to_message_id")):
        return False
    if not (p.get("display_text") or p.get("content")):
        return False
    # A reply still being written has no status yet (and display_text_ready
    # is already true), so its text is cut short; wait for "completed".
    if not p.get("status"):
        return False
    return True


# How often a waiting send re-reads history. A history read is one small
# unary call; at 4 s a finished reply sat unshown for up to 4 s.
POLL = 1
ACK_POLL = 0.1   # delivery-ack history checks: one cheap round trip each on a warm connection


def _accepts_track(gw):
    import inspect
    try:
        return "track" in inspect.signature(gw._open).parameters
    except (TypeError, ValueError):
        return False


def deliver(gw, cfg, text, thread=None, ack_timeout=30, mark=None):
    """Send TEXT on gw and wait for the delivery ack; returns (user_seq, stream_id).

    user_seq is None when no ack arrived within ack_timeout. Keep gw open
    until the turn is over: closing it early can drop the chat.stream request.
    """
    params = {"items": [{"type": "text", "text": text}],
              "node_id": cfg["node_id"], "capabilities": {}}
    if thread:
        params["session_id"] = thread
    # chat.history without session_id reads the main chat; with it, the
    # thread. Either way the scope matches where the reply will land.
    scope = {"session_id": thread} if thread else {}
    baseline = 0
    try:
        h = gw.call_json("chat.history", body={"limit": 1, **scope})
        evs = h.get("chat_events", [])
        if evs:
            baseline = max(e.get("seq", 0) for e in evs)
    except (GatewayError, TimeoutError):
        pass
    mark = mark or (lambda name: None)
    mark("baseline")
    # Nobody reads chat.stream's own frames (the turn is followed on
    # chat.subscribe); on a shared connection they are simply dropped.
    stream_sid = gw._open("chat.stream", body=params, track=False) if _accepts_track(gw) \
        else gw._open("chat.stream", body=params)
    mark("posted")
    # Wait for delivery: our message.user must show up in history with a
    # seq. Closing the connection before that can drop the message.
    from .wait import user_ack
    user_seq, ack_deadline = None, time.time() + ack_timeout
    while user_seq is None and time.time() < ack_deadline:
        try:
            h = gw.call_json("chat.history", body={"limit": 10, **scope})
            user_seq = user_ack(h.get("chat_events", []), baseline, text)
        except (GatewayError, TimeoutError):
            pass
        if user_seq is None:
            time.sleep(min(POLL, ACK_POLL))
    return user_seq, stream_sid


def cmd_send(args):
    cfg = load_config()
    gw = connect(cfg)
    scope = {"session_id": args.thread} if args.thread else {}
    try:
        user_seq, stream_sid = deliver(gw, cfg, args.text, args.thread, args.ack_timeout)
    except BaseException:
        gw.close()
        raise
    # The sending connection stays open until we are done waiting, so the
    # chat.stream request is never torn down mid-turn.
    try:
        _send_finish(args, cfg, scope, user_seq, stream_sid, gw)
    finally:
        gw.close()


def _send_finish(args, cfg, scope, user_seq, stream_sid, gw=None):
    result = {"sent": True, "delivered": user_seq is not None, "user_seq": user_seq,
              "stream": stream_sid}
    if user_seq is None:
        result["note"] = f"no delivery ack within {args.ack_timeout}s; check `muse-cli history`"
        out(result)
        sys.exit(5)
    wait_s = 24 * 3600 if args.until_done else args.wait
    if not wait_s:
        out(result)
        return
    # Follow the turn on the connection that sent it (multiplexed): no new
    # connect + Noise handshake per subscription or history read.
    conn = (lambda _cfg: gw.lease()) if gw is not None and hasattr(gw, "lease") else None
    code, res = run_wait(cfg, args.thread, user_seq, wait_s, require_running=True,
                         auto_approve=args.auto_approve, connector=conn)
    result.update(outcome=res["outcome"], status=res["status"], final_text=res["final_text"],
                  replies=res["replies"])
    for k in ("approvals", "auto_approved", "auto_approve_errors", "widgets", "question",
              "needs_input_reason"):
        if k in res:
            result[k] = res[k]
    if res["replies"]:
        r = res["replies"][-1]
        result["reply"] = {"seq": r["seq"], "role": "message.assistant",
                           "message_id": r["message_id"], "text": r["text"]}
    if args.download and res["replies"]:
        gw = connect(cfg)
        try:
            h = gw.call_json("chat.history", body={"limit": 30, **scope})
            found = []
            for ev in h.get("chat_events", []):
                if (ev.get("seq") or 0) > user_seq:
                    for r in event_resources(ev):
                        if r["path"] not in [x["path"] for x in found]:
                            found.append(r)
            result["downloads"] = save_resources(gw, found, args.download)
        except (GatewayError, TimeoutError) as e:
            result["download_error"] = str(e)
        finally:
            gw.close()
    if res["outcome"] == "timeout":
        result["note"] = f"turn not finished within {wait_s}s; run `muse-cli wait --after-seq {user_seq}`"
    if args.text:
        print(f"user_seq={user_seq}")
        _print_wait(res, False)
    else:
        out(result)
    sys.exit(code)


def decide_approval(gw, approval_id, decision):
    """The one call behind `approve`, `deny`, `approve --all` and auto-approve."""
    return gw.call_json("egress.approval.decide", path_params={"approval_id": approval_id},
                        body={"decision": decision})


def make_auto_approver(cfg, approved, errors, connector=None):
    """Approver for Waiter: allow_always each new approval on its own connection
    (the subscription's reader owns the other one). Logs to stderr, records
    successes in `approved` and failures in `errors`; returns handled ids."""
    from .wait import FOREVER, offers_forever

    def approve(items):
        done = []
        try:
            gw = (connector or connect)(cfg)
        except Exception as e:  # noqa: BLE001 - surface, then stop with exit 4
            for a in items:
                errors.append({"approval_id": a["approval_id"], "error": str(e)})
            return done
        try:
            for a in items:
                aid = a["approval_id"]
                row = {"approval_id": aid, "title": a.get("title"), "kind": a.get("kind"),
                       "scope": a.get("scope"), "decision": FOREVER}
                if not offers_forever(a):
                    # Never fall back to once/session/today: leave it for a human.
                    errors.append({**row, "error": f"no forever option (offers {a.get('options')})"})
                    print(f"auto-approve SKIPPED {aid}: Muse offers no forever option "
                          f"{a.get('options')}", file=sys.stderr, flush=True)
                    continue
                try:
                    decide_approval(gw, aid, FOREVER)
                except (GatewayError, TimeoutError) as e:
                    errors.append({**row, "error": str(e)})
                    print(f"auto-approve FAILED {aid}: {e}", file=sys.stderr, flush=True)
                    continue
                approved.append(row)
                done.append(aid)
                print(f"auto-approved {aid} (allow_always): {row['title']} "
                      f"[{row['kind']} {row['scope']}]", file=sys.stderr, flush=True)
        finally:
            gw.close()
        return done
    return approve


def task_state(gw, thread=None, timeout=6):
    """Muse's current task status for the main chat or THREAD, as the server
    replays it on chat.subscribe (e.g. "running", "completed"), or None."""
    from .wait import task_status
    found = []

    def on_record(line):
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return False
        s = task_status(ev, thread)
        if s:
            found.append(s)
            return True
        return False
    body = {"capabilities": ["chat_cancel", "delta_stream"]}
    if thread:
        body["session_id"] = thread
    gw.subscribe_raw("chat.subscribe", body=body, max_records=50, idle_timeout=timeout,
                     overall_timeout=timeout, on_record=on_record)
    return found[0] if found else None


RECHECK_AFTER = 30      # seconds without any live record before re-checking the server
ONLINE_GRACE = 3        # Muse said "online" (idle) but no turn end followed within this
RESUB_PAUSE = 1.0       # pause before re-subscribing after a stream ended


def run_wait(cfg, thread=None, after_seq=None, timeout=600, require_running=False,
             on_event=None, auto_approve=None, connector=None, approver=None,
             should_stop=None, recheck=None, recheck_after=None, log=None):
    """Subscribe until the turn ends, a human is needed, or timeout.

    auto_approve: True/False per call, None to use MUSE_AUTO_APPROVE or the
    config (`muse-cli auto-approve`). When on, pending approvals are approved
    with allow_always and the wait goes on; questions still end it.
    connector: callable(cfg) -> gateway (default `connect`). approver: replaces
    the built-in auto-approver when auto-approve is on. should_stop: callable
    polled every couple of seconds; True ends the wait early (outcome timeout).

    Turn-end detection never trusts one subscription alone:
    - a terminal status replayed before our turn was seen running is checked
      against chat.history (a reply after after_seq means the turn already
      ran and ended, e.g. a fast answer that finished before we subscribed);
    - when no record arrives for recheck_after seconds (watchdog), or the
      stream ends, or recheck() returns True, the subscription is reopened
      on a fresh connection: the server replays the current task.status, so
      the state is re-read from the server instead of waiting forever.

    Returns (exit_code, result). The final replies are re-read from
    chat.history on a fresh connection (the subscription's reader thread
    still owns the old one).
    """
    from .wait import (Waiter, replies_after, exit_code, outcome_of, needs_input_reason,
                       question_of, in_scope)
    import logging
    log = log or logging.getLogger("muse_cli.wait")
    scope = {"session_id": thread} if thread else {}
    enabled, _src = auto_approve_setting(cfg, auto_approve)
    approved, approve_errors = [], []
    conn = connector or connect
    recheck_after = RECHECK_AFTER if recheck_after is None else recheck_after
    if enabled:
        approver = approver or make_auto_approver(cfg, approved, approve_errors, connector=conn)
    else:
        approver = None

    def history_replies(limit=30):
        g = conn(cfg)
        try:
            return replies_after(g.call_json("chat.history", body={"limit": limit, **scope})
                                 .get("chat_events", []), after_seq)
        finally:
            g.close()

    if after_seq is None:
        gw = conn(cfg)
        try:
            h = gw.call_json("chat.history", body={"limit": 1, **scope})
            evs = h.get("chat_events", [])
            after_seq = max((e.get("seq") or 0 for e in evs), default=0)
            # A plain `wait` after the last message: anything newer counts.
        finally:
            gw.close()
    w = Waiter(thread=thread, require_running=require_running, approver=approver)
    body = {"capabilities": ["chat_cancel", "delta_stream"], **scope}
    deadline = time.time() + timeout
    user_stop = should_stop or (lambda: False)
    forced = [False]
    last_record = [time.time()]
    online_at = [0.0]          # when Muse said "online" (idle) mid-turn

    def stop_now():
        if user_stop():
            return True
        if (recheck and recheck()) or (recheck_after and time.time() - last_record[0] > recheck_after):
            forced[0] = True
            return True
        if online_at[0] and time.time() - online_at[0] > ONLINE_GRACE:
            # Muse went idle but no terminal task.status followed: re-read the
            # state from the server instead of trusting this stream.
            log.info("Muse is online (idle) with no turn end on the stream; re-checking")
            online_at[0] = 0.0
            forced[0] = True
            return True
        return False

    def on_record(line):
        if on_event:
            on_event(line)
        if w.feed(line) is not None:
            return True
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            ev = {}
        name = str(ev.get("event") or ev.get("event_name") or "") if isinstance(ev, dict) else ""
        if name.startswith(("delta.", "task.", "agent.", "message.")):
            last_record[0] = time.time()   # progress; keep-alives do not count
        p = ev.get("payload") if isinstance(ev, dict) and isinstance(ev.get("payload"), dict) else {}
        if (name == "agent.status" and p.get("activity_code") == "online" and w.seen_running
                and in_scope(ev, thread)):
            # Muse went idle but no terminal task.status came with it (yet):
            # re-read the state from the server instead of trusting the stream.
            if not online_at[0]:
                online_at[0] = time.time()
        elif name in ("delta.text_append", "delta.message_start") or (
                name == "agent.status" and p.get("activity_code") not in (None, "online")):
            online_at[0] = 0.0
        if w.ignored_terminal:
            try:
                replies = history_replies()
            except (GatewayError, TimeoutError, ConnectionError, OSError) as e:
                log.debug("history check failed: %s", e)
                return False
            if w.settle(replies) is not None:
                log.info("turn already over (status %s, %d reply(s) after seq %s)",
                         w.status, len(replies), after_seq)
                return True
        return False

    attempt, failures = 0, 0
    while w.result is None and time.time() < deadline and not user_stop():
        if attempt:
            time.sleep(min(RESUB_PAUSE * (2 ** min(failures, 5)), 30) if failures else RESUB_PAUSE)
            if w.result is not None or user_stop() or time.time() >= deadline:
                break
        attempt += 1
        forced[0] = False
        online_at[0] = 0.0
        last_record[0] = time.time()
        left = max(1, deadline - time.time())
        idle = min(left, recheck_after) if recheck_after else left
        try:
            gw = conn(cfg)
        except (GatewayError, TimeoutError, ConnectionError, OSError) as e:
            failures += 1
            log.warning("wait: connect failed (%s); retrying", e)
            continue
        try:
            gw.subscribe_raw("chat.subscribe", body=body, max_records=10**9,
                             idle_timeout=idle, overall_timeout=left, on_record=on_record,
                             should_stop=stop_now)
            failures = 0
        except (GatewayError, TimeoutError, ConnectionError, OSError) as e:
            failures += 1
            log.warning("wait: subscription failed (%s); retrying", e)
        finally:
            gw.close()
        if w.result is None and not user_stop() and time.time() < deadline:
            log.debug("wait: re-checking the server (%s; status %s)",
                      "requested" if forced[0] else "no events / stream ended", w.status)
    kind, detail = w.result or ("timeout", w.status)
    replies = []
    gw = conn(cfg)
    try:
        h = gw.call_json("chat.history", body={"limit": 30, **scope})
        replies = replies_after(h.get("chat_events", []), after_seq)
    except (GatewayError, TimeoutError):
        pass
    finally:
        gw.close()
    res = {"outcome": outcome_of(kind, replies), "status": w.status, "after_seq": after_seq,
           "final_text": replies[-1]["text"] if replies else None, "replies": replies}
    if kind == "approval":
        res["approvals"] = detail
    if enabled:
        res["auto_approved"] = approved
        if approve_errors:
            res["auto_approve_errors"] = approve_errors
    if replies and replies[-1].get("widgets"):
        res["widgets"] = replies[-1]["widgets"]
    if res["outcome"] == "needs_input":
        last = replies[-1]
        res["needs_input_reason"] = "widget" if last.get("widgets") else needs_input_reason(last["text"])
        res["question"] = question_of(last["text"])
    return exit_code(kind, replies), res


def _print_wait(res, as_json):
    if as_json:
        return out(res)
    for r in res["replies"]:
        print(f"[{r['seq']}] {r['text']}")
        for wd in r.get("widgets") or []:
            opts = " / ".join(map(str, wd.get("options") or []))
            print(f"  ? widget {wd['widget_id']}: {wd.get('text') or ''}" + (f" [{opts}]" if opts else ""))
    if res.get("question"):
        print(f"NEEDS INPUT ({res.get('needs_input_reason')}): {res['question']}")
    for a in res.get("approvals") or []:
        print(f"NEEDS APPROVAL {a['approval_id']}: {a.get('title')} - {a.get('question')}")
        if a.get("why"):
            print(f"  why: {a['why']}")
    print(f"-- {res['outcome']} (status={res['status']})", file=sys.stderr)


def cmd_wait(args):
    code, res = run_wait(load_config(), args.thread, args.after_seq, args.timeout,
                         require_running=args.require_running, auto_approve=args.auto_approve)
    _print_wait(res, args.json)
    sys.exit(code)


def _pending(gw):
    r = gw.call_json("egress.approvals")
    return r, r.get("pending_approvals") or r.get("pending") or []


def approve_all(gw, decision):
    """Decide every currently pending approval; returns the rows decided."""
    from .wait import FOREVER, describe_approval, offers_forever
    _r, pend = _pending(gw)
    done = []
    for a in pend:
        d = describe_approval(a)
        if decision == FOREVER and not offers_forever(d):
            print(f"skipped {d['approval_id']}: no forever option {d['options']}", file=sys.stderr)
            continue
        decide_approval(gw, d["approval_id"], decision)
        done.append({"approval_id": d["approval_id"], "title": d["title"], "kind": d["kind"],
                     "scope": d["scope"], "decision": decision})
        print(f"approved {d['approval_id']} ({decision}): {d['title']} [{d['kind']} {d['scope']}]",
              file=sys.stderr)
    return done


def cmd_approvals(args):
    from .wait import describe_approval
    gw = connect(load_config())
    try:
        if args.approve_all:
            from .wait import FOREVER
            return out({"approved": approve_all(gw, FOREVER)})
        r, pend = _pending(gw)
        if args.raw:
            return out(r)
        o = {"pending": [describe_approval(a) for a in pend]}
        if args.recent:
            o["recent"] = [{**describe_approval(a), "decision": a.get("decision")}
                           for a in (r.get("recent_approvals") or r.get("recent") or [])]
        out(o)
    finally:
        gw.close()


def cmd_decide(decision_of):
    def run(args):
        decision = decision_of(args)
        if getattr(args, "all", False) == bool(args.id):
            raise ValueError("give an approval ID or --all (not both)")
        gw = connect(load_config())
        try:
            if getattr(args, "all", False):
                out({"approved": approve_all(gw, decision)})
            else:
                out(decide_approval(gw, args.id, decision))
        finally:
            gw.close()
    return run


def _approve_decision(args):
    for k in ("once", "session", "today", "always"):
        if getattr(args, k):
            return "allow_" + k
    return "allow_once"


def _auto_approve_flags(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--auto-approve", dest="auto_approve", action="store_true", default=None,
                   help="approve pending approvals with allow_always and keep waiting")
    g.add_argument("--no-auto-approve", dest="auto_approve", action="store_false",
                   help="stop with exit 4 on approvals even if auto-approve is on")


def _print_watch_row(line, show_all):
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return
    et = ev.get("event", ev.get("type"))
    if et in ("agent.status",) and not show_all:
        return
    p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
    row = {"event": et, "seq": ev.get("seq")}
    for k in ("display_text", "content", "text", "activity_text", "status"):
        if p.get(k):
            row[k] = str(p[k])[:300]
    print(json.dumps(row, ensure_ascii=False), flush=True)


def cmd_watch(args):
    gw = connect(load_config())
    try:
        # Subscribe the way the web client does: capabilities as a list, and
        # a thread's own session id when watching a side chat.
        body = {"capabilities": ["chat_cancel", "delta_stream"]}
        if args.thread:
            body["session_id"] = args.thread
        # Rows print as each event arrives, not after the stream ends.
        gw.subscribe_raw("chat.subscribe", body=body, max_records=args.n,
                         idle_timeout=args.timeout, overall_timeout=args.timeout,
                         on_record=lambda line: _print_watch_row(line, args.all))
    finally:
        gw.close()


def feed_rows(d, limit=None):
    rows = []
    for day in d.get("days", []):
        for ed in day.get("editions", []):
            for u in ed.get("units", []):
                rows.append({"id": u.get("unit_id"),
                             "title": u.get("title"),
                             "date": day.get("local_date"),
                             "edition": ed.get("kind")})
    return rows[:limit] if limit else rows


def goal_rows(d):
    return [{"id": g.get("id") or g.get("goal_id"), "title": g.get("title"),
             "status": g.get("status"), "subtitle": g.get("subtitle")}
            for g in d.get("goals", [])]


def idea_rows(d):
    rows = []
    for s in d.get("sections", []):
        for c in s.get("cards", s.get("ideas", [])):
            rows.append({"id": c.get("id") or c.get("ideaCardId"),
                         "title": c.get("title"), "section": s.get("title")})
    return rows


def idea_execute(gw, idea_id, thread=None):
    body = {"ideaCardId": idea_id, "mode": "full"}
    if thread:
        body["session_id"] = thread
    return gw.call_json("api.idea-cards.execute", path_params={"ideaCardId": idea_id}, body=body)


def cmd_feed(args):
    gw = connect(load_config())
    try:
        d = gw.call_json("feed.list", query={"limit": args.limit} if args.limit else None)
        if args.raw:
            out(d); return
        out(feed_rows(d, args.limit))
    finally:
        gw.close()


def cmd_feed_status(_args):
    gw = connect(load_config())
    try:
        out(gw.call_json("feed.status"))
    finally:
        gw.close()


def cmd_feed_react(args):
    gw = connect(load_config())
    try:
        out(gw.call_json("feed.unit.reaction", path_params={"unit_id": args.unit},
                         body={"reaction": args.reaction}))
    finally:
        gw.close()


def cmd_feed_prompt(_args):
    gw = connect(load_config())
    try:
        out(gw.call_json("feed.prompt.get"))
    finally:
        gw.close()


def cmd_goals(_args):
    gw = connect(load_config())
    try:
        out(goal_rows(gw.call_json("goals.list")))
    finally:
        gw.close()


def cmd_goal(args):
    gw = connect(load_config())
    try:
        out(gw.call_json("goals.get", path_params={"id": args.id}))
    finally:
        gw.close()


def cmd_ideas(_args):
    gw = connect(load_config())
    try:
        out(idea_rows(gw.call_json("api.idea-cards.list")))
    finally:
        gw.close()


def cmd_idea(args):
    gw = connect(load_config())
    try:
        out(gw.call_json("api.idea-cards.detail", path_params={"ideaCardId": args.id}))
    finally:
        gw.close()


def cmd_idea_exec(args):
    gw = connect(load_config())
    try:
        out(idea_execute(gw, args.id, args.thread))
    finally:
        gw.close()


def cmd_unread(_args):
    gw = connect(load_config())
    try:
        out(gw.call_json("chat.unread_count"))
    finally:
        gw.close()


def cmd_seen(args):
    gw = connect(load_config())
    try:
        out(gw.call_json("chat.mark_seen", path_params={"thread_id": args.thread}))
    finally:
        gw.close()


def session_start(gw, title=None):
    """Start a new side chat; returns the server reply (has session_id)."""
    params = {"origin": "fresh", "lifecycle": "persistent"}
    if title:
        params["title"] = title
    return gw.call_json("session.start", body={"method": "/api/session/start", "params": params})


def session_op(gw, kind, session_id, title=None):
    """rename | pin | unpin | archive | unarchive | delete one session."""
    if kind == "rename":
        body = {"session_id": session_id, "title": title}
    else:
        body = {"method": f"/api/session/{kind}", "session_id": session_id}
    return gw.call_json(f"session.{kind}", body=body)


def cancel_turn(gw, thread=None):
    """Stop Muse's current turn (the web app's Stop button): POST /chat/cancel.

    Body is {} for the main chat or {"session_id": ID} for a side chat, as the
    web client sends it. Returns the reply, e.g. {"cancelled": true}.
    """
    return gw.call_json("chat.cancel", body={"session_id": thread} if thread else {})


def cmd_stop(args):
    gw = connect(load_config())
    try:
        out(cancel_turn(gw, args.thread))
    finally:
        gw.close()


def cmd_session_start(args):
    gw = connect(load_config())
    try:
        out(session_start(gw, args.title))
    finally:
        gw.close()


def cmd_session_op(kind):
    def run(args):
        gw = connect(load_config())
        try:
            out(session_op(gw, kind, args.id, getattr(args, "title", None)))
        finally:
            gw.close()
    return run


def wake_vm(cfg):
    """Ask muse.ai to wake the VM; returns {"status": http_status, "body": ...}."""
    from .gateway import _hatch_headers, fetch_access_token, fetch_session_info
    from curl_cffi import requests as rq
    cookies = load_cookies(cfg["cookies_file"])
    at = fetch_access_token(cookies)
    vm_id = cfg.get("vm_id") or fetch_session_info(cookies)["vm_id"]
    resp = rq.post("https://muse.ai/api/hatch/vm/wake", headers=_hatch_headers(cookies, at),
                   json={"vm_id": vm_id, "retry_count": 0},
                   impersonate="chrome", timeout=15)
    try:
        body = resp.json() if resp.text else None
    except ValueError:
        body = resp.text[:300]
    return {"status": resp.status_code, "body": body}


def cmd_wake(_args):
    out(wake_vm(load_config()))


def cmd_raw(args):
    from .gateway import ROUTES
    if args.method not in ROUTES:
        print(f"unknown method '{args.method}' (see routes.json for the {len(ROUTES)} known methods)",
              file=sys.stderr)
        sys.exit(2)
    try:
        body = json.loads(args.body) if args.body else None
    except json.JSONDecodeError as e:
        print(f"invalid --body JSON: {e}", file=sys.stderr)
        sys.exit(2)
    pp = {}
    for kv in (args.param or []):
        if "=" not in kv:
            print(f"bad --param '{kv}': expected k=v", file=sys.stderr)
            sys.exit(2)
        k, v = kv.split("=", 1)
        pp[k] = v
    gw = connect(load_config())
    try:
        res = raw_call(gw, args.method, body, pp or None, args.timeout)
        if isinstance(res, str):
            print(res)
        else:
            out(res)
    finally:
        gw.close()


def raw_call(gw, method, body=None, path_params=None, timeout=30):
    """Any gateway method: parsed JSON, or the text when the reply is not JSON."""
    data = gw.request(method, path_params=path_params, body=body, query=None, timeout=timeout)
    try:
        return json.loads(data) if data else {}
    except json.JSONDecodeError:
        return data.decode("utf-8", "replace")


# -- System Files (fs) ---------------------------------------------------------
def _fs(args, fn):
    from .fs import FS
    gw = connect(load_config())
    try:
        return fn(FS(gw))
    finally:
        gw.close()


def cmd_fs_ls(args):
    from .fs import fmt_size
    def run(fs):
        ents = fs.list_dir(args.path)
        if not args.all:
            ents = [e for e in ents if not e["name"].startswith(".")]
        if args.json:
            return out(ents)
        for e in ents:
            name = e["name"] + ("/" if e.get("type") == "directory" else "")
            if args.long:
                print(f"{e.get('type','?')[0]} {fmt_size(e.get('size')):>7} {e.get('modifiedAt','')[:19]:19} {name}")
            else:
                print(name)
    _fs(args, run)


def cmd_fs_tree(args):
    def run(fs):
        rows = [(r, e, lvl) for r, e, lvl in fs.walk(args.path, args.depth)
                if args.all or not any(x.startswith(".") for x in r.split("/"))]
        if args.json:
            return out([{"path": r, **e} for r, e, _ in rows])
        print(args.path or "/")
        for r, e, lvl in rows:
            print("  " * (lvl + 1) + e["name"] + ("/" if e.get("type") == "directory" else ""))
    _fs(args, run)


def cmd_fs_stat(args):
    _fs(args, lambda fs: out(fs.stat(args.path)))


def cmd_fs_cat(args):
    def run(fs):
        data = fs.read_file(args.path, max_bytes=args.max_bytes)
        if b"\0" in data[:8192] and not args.binary:
            print(f"{args.path}: binary file ({len(data)} bytes); use `fs get` or --binary", file=sys.stderr)
            sys.exit(1)
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    _fs(args, run)


def cmd_fs_get(args):
    def run(fs):
        os.makedirs(args.output, exist_ok=True)
        out({"saved": fs.get(args.path, args.output, recursive=args.recursive)})
    _fs(args, run)


def cmd_fs_find(args):
    _fs(args, lambda fs: out(fs.search(args.name, args.limit)))


def cmd_fs_put(args):
    _fs(args, lambda fs: out(fs.put(args.local, args.remote, recursive=args.recursive,
                                    overwrite=not args.no_clobber)))


def cmd_fs_write(args):
    data = sys.stdin.buffer.read()
    def run(fs):
        if args.append:
            data2 = fs.read_file(args.remote) + data
            return out(fs.write_file(args.remote, data2))
        out(fs.write_file(args.remote, data, overwrite=not args.no_clobber))
    _fs(args, run)


def cmd_fs_edit(args):
    from .fs import apply_replace, edit_in_editor
    def run(fs):
        before = fs.read_file(args.remote)
        if args.replace:
            text = before.decode("utf-8")
            for old, new in args.replace:
                text = apply_replace(text, old, new)
            after = text.encode("utf-8")
        else:
            after = edit_in_editor(before, os.path.splitext(args.remote)[1])
        if after == before:
            return out({"path": args.remote, "changed": False})
        r = fs.write_file(args.remote, after, overwrite=True)
        out({**r, "changed": True})
    _fs(args, run)


def cmd_fs_mkdir(args):
    _fs(args, lambda fs: out(fs.mkdir(args.path, parents=True)))


def cmd_fs_mv(args):
    _fs(args, lambda fs: out(fs.move(args.src, args.dst, overwrite=args.force)))


def cmd_fs_rm(args):
    if not args.yes:
        print("refusing to delete without --yes", file=sys.stderr)
        sys.exit(2)
    _fs(args, lambda fs: out(fs.delete(args.path, recursive=args.recursive)))


def _chat(args):
    from .chat import cmd_chat
    cmd_chat(args)


def _prescan_profile(argv):
    """The --profile value given before the subcommand, or None.

    Help text is built before argparse runs, so read the flag early; argparse
    still parses (and owns) it afterwards. Stops at the first positional so a
    message text like `send -- --profile` is never mistaken for the flag.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--profile":
            return argv[i + 1] if i + 1 < len(argv) else None
        if tok.startswith("--profile="):
            return tok.split("=", 1)[1]
        if tok == "--" or not tok.startswith("-"):
            return None
        i += 1
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    profile_error = None
    try:
        activate_profile(resolve_profile(_prescan_profile(argv)))
    except ValueError as e:
        profile_error = str(e)
        activate_profile(DEFAULT_PROFILE)

    ap = argparse.ArgumentParser(
        prog="muse-cli", description="CLI for your personal muse.ai agent",
        epilog=f"Active profile: {PROFILE} ({CONFIG_DIR})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--profile", metavar="NAME", default=None,
        help=f"use login/config profile NAME (default: ${PROFILE_ENV}, else 'default'). "
             "'default' is ~/.config/muse-cli; others live in ~/.config/muse-cli/profiles/NAME")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("auth", help="auth helpers"); a = p.add_subparsers(dest="op", required=True)
    export_help = (
        "Copy your muse.ai login out of Google Chrome into\n"
        f"{COOKIES_FILE} (mode 600).\n"
        "\n"
        "Chrome shares those cookies only after remote debugging is on.\n"
        "In the Chrome window you already use for muse.ai:\n"
        "\n"
        "  1. Open chrome://inspect/#remote-debugging\n"
        "  2. Turn on remote debugging\n"
        '     ("Allow remote debugging for this browser instance")\n'
        "  3. Open https://muse.ai/ and log in. Leave that tab open.\n"
        "  4. Run this command. If Chrome asks to allow the connection,\n"
        "     click Allow, then run it again if it failed before the click.\n"
        "\n"
        "Requires agent-browser: npm i -g agent-browser\n"
        "(install Node.js LTS from https://nodejs.org first if npm is missing).\n"
        "\n"
        "Stay in that same Chrome. Starting another Chrome with\n"
        "--remote-debugging-port uses a different profile and has no muse.ai login."
    )
    a.add_parser(
        "export",
        help="copy the muse.ai login out of Chrome (remote debugging must be on)",
        description=export_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).set_defaults(fn=cmd_auth_export)
    q = a.add_parser("list", help="list profiles, the active one (*), and which have cookies (offline)",
                     description="List the default profile and every ~/.config/muse-cli/profiles/NAME. "
                                 "Read-only: no network, nothing is created.")
    q.add_argument("--json", action="store_true", help="print as JSON")
    q.set_defaults(fn=cmd_auth_list)

    sub.add_parser("chat", help="interactive chat (the default with no command)").set_defaults(fn=_chat)
    sub.add_parser("status", help="VM, session count, unread, identity").set_defaults(fn=cmd_status)
    p = sub.add_parser("usage", help="plan usage: weekly limit %% used, reset date, bonus tokens")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.add_argument("--raw", action="store_true", help="print the server's full subscription object")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("invite", help="your invite code, uses left, rewards earned (read-only)")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.add_argument("--raw", action="store_true", help="print the server's full invite object")
    p.add_argument("--create", action="store_true",
                   help="create your code first if you have none (a write; no-op if one exists)")
    p.set_defaults(fn=cmd_invite)
    p = sub.add_parser("redeem", help="redeem a friend's invite code (within 48h of joining)")
    p.add_argument("code"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_redeem)
    p = sub.add_parser("threads", help="list chats and side chats")
    p.add_argument("--all", dest="archived", action="store_true", help="include archived")
    p.set_defaults(fn=cmd_threads)
    p = sub.add_parser("history", help="read chat messages")
    p.add_argument("--thread", default=None); p.add_argument("--limit", type=int, default=10)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--download", metavar="DIR", default=None,
                   help="also save files (images/videos) from these messages into DIR")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("send", help="send the agent a message")
    p.add_argument("text"); p.add_argument("--thread", default=None)
    p.add_argument("--wait", type=int, default=90,
                   help="max seconds to wait for the turn to finish (0 = only wait for delivery)")
    p.add_argument("--until-done", action="store_true", help="wait with no practical time limit")
    p.add_argument("--text", action="store_true", help="print the final reply as plain text instead of JSON")
    p.add_argument("--ack-timeout", type=int, default=30, help="seconds to wait for the delivery ack")
    p.add_argument("--download", metavar="DIR", default=None,
                   help="save files the reply produced into DIR (needs --wait)")
    _auto_approve_flags(p)
    p.set_defaults(fn=cmd_send)
    p = sub.add_parser("wait", help="block until the agent's turn ends (0 ok, 2 failed, 4 needs human, 124 timeout)")
    p.add_argument("--thread", default=None); p.add_argument("--after-seq", type=int, default=None)
    p.add_argument("--timeout", type=int, default=600); p.add_argument("--json", action="store_true")
    p.add_argument("--require-running", action="store_true",
                   help="ignore an already-finished status until a new run starts")
    _auto_approve_flags(p)
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("approvals", help="list pending approvals (network access etc.)")
    p.add_argument("--recent", action="store_true"); p.add_argument("--raw", action="store_true")
    p.add_argument("--approve-all", action="store_true",
                   help="approve every pending approval with allow_always")
    p.set_defaults(fn=cmd_approvals)
    p = sub.add_parser("approve", help="approve a pending request (or --all)")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--all", action="store_true", help="approve every pending approval")
    g = p.add_mutually_exclusive_group()
    for k in ("once", "session", "today", "always"):
        g.add_argument(f"--{k}", action="store_true", help=f"allow_{k}")
    p.set_defaults(fn=cmd_decide(_approve_decision))
    p = sub.add_parser("auto-approve",
                       help="let wait/send approve every approval with allow_always (on|off|status)")
    p.add_argument("state", nargs="?", default="status", choices=["on", "always", "off", "status"])
    p.set_defaults(fn=cmd_auto_approve)
    p = sub.add_parser("deny", help="deny a pending request"); p.add_argument("id")
    p.add_argument("--always", action="store_true", help="deny_always")
    p.set_defaults(fn=cmd_decide(lambda a: "deny_always" if a.always else "deny"))
    p = sub.add_parser("files", help="list files (images/videos) in recent messages")
    p.add_argument("--thread", default=None); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_files)
    p = sub.add_parser("download", help="download a file the agent produced")
    p.add_argument("ref", help="sandbox:// path, media URL, message id, or history seq")
    p.add_argument("-o", "--output", default=".", help="directory to save into (default .)")
    p.add_argument("--thread", default=None, help="side chat to search for a message id/seq")
    p.add_argument("--limit", type=int, default=50, help="messages to search for a message id/seq")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    p.set_defaults(fn=cmd_download)
    p = sub.add_parser("watch", help="tail live agent events")
    p.add_argument("--timeout", type=int, default=60); p.add_argument("--n", type=int, default=50)
    p.add_argument("--all", action="store_true"); p.add_argument("--thread", default=None)
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("feed", help="list feed units")
    p.add_argument("--limit", type=int, default=None); p.add_argument("--raw", action="store_true")
    p.set_defaults(fn=cmd_feed)
    sub.add_parser("feed-status", help="feed refresh status").set_defaults(fn=cmd_feed_status)
    p = sub.add_parser("feed-react", help="react to a feed unit")
    p.add_argument("unit"); p.add_argument("reaction", nargs="?", default="love")
    p.set_defaults(fn=cmd_feed_react)
    sub.add_parser("feed-prompt", help="show feed prompt").set_defaults(fn=cmd_feed_prompt)
    sub.add_parser("goals", help="list goals").set_defaults(fn=cmd_goals)
    p = sub.add_parser("goal", help="show one goal"); p.add_argument("id"); p.set_defaults(fn=cmd_goal)
    sub.add_parser("ideas", help="list idea cards").set_defaults(fn=cmd_ideas)
    p = sub.add_parser("idea", help="show one idea"); p.add_argument("id"); p.set_defaults(fn=cmd_idea)
    p = sub.add_parser("idea-exec", help="execute an idea (agent acts on it)")
    p.add_argument("id"); p.add_argument("--thread", default=None); p.set_defaults(fn=cmd_idea_exec)
    sub.add_parser("unread", help="unread counts").set_defaults(fn=cmd_unread)
    p = sub.add_parser("seen", help="mark a thread seen"); p.add_argument("thread")
    p.set_defaults(fn=cmd_seen)
    p = sub.add_parser("session-start", help="start a new side chat")
    p.add_argument("--title", default=None); p.set_defaults(fn=cmd_session_start)
    for kind, help_text in [("rename", "rename a session"), ("pin", "pin a session"),
                            ("unpin", "unpin a session"), ("archive", "archive a session"),
                            ("unarchive", "unarchive a session"), ("delete", "delete a session")]:
        p = sub.add_parser(f"session-{kind}", help=help_text)
        p.add_argument("id")
        if kind == "rename":
            p.add_argument("title")
        p.set_defaults(fn=cmd_session_op(kind))
    sub.add_parser("wake", help="request a VM wake").set_defaults(fn=cmd_wake)
    p = sub.add_parser("stop", help="stop Muse's current turn (like the app's Stop button)")
    p.add_argument("--thread", default=None, help="side chat session id (default: main chat)")
    p.set_defaults(fn=cmd_stop)
    from .tgbot import add_telegram_parser
    add_telegram_parser(sub)
    sub.add_parser("update", help="upgrade this install to the latest release").set_defaults(fn=cmd_update)
    p = sub.add_parser("fs", help="browse/read/write Muse's System Files (agent home)")
    f = p.add_subparsers(dest="op", required=True)
    q = f.add_parser("ls", help="list a folder (default: System Files root)")
    q.add_argument("path", nargs="?", default=""); q.add_argument("-l", "--long", action="store_true")
    q.add_argument("-a", "--all", action="store_true", help="include dotfiles")
    q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_fs_ls)
    q = f.add_parser("tree", help="recursive listing")
    q.add_argument("path", nargs="?", default=""); q.add_argument("--depth", type=int, default=2)
    q.add_argument("-a", "--all", action="store_true"); q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_fs_tree)
    q = f.add_parser("stat", help="file/folder metadata"); q.add_argument("path"); q.set_defaults(fn=cmd_fs_stat)
    q = f.add_parser("cat", help="print a file"); q.add_argument("path")
    q.add_argument("--max-bytes", type=int, default=None); q.add_argument("--binary", action="store_true")
    q.set_defaults(fn=cmd_fs_cat)
    q = f.add_parser("get", help="download a file (or folder with -r)"); q.add_argument("path")
    q.add_argument("-o", "--output", default="."); q.add_argument("-r", "--recursive", action="store_true")
    q.set_defaults(fn=cmd_fs_get)
    q = f.add_parser("find", help="search by name substring"); q.add_argument("name")
    q.add_argument("--limit", type=int, default=None); q.set_defaults(fn=cmd_fs_find)
    q = f.add_parser("put", help="upload a local file (or folder with -r)")
    q.add_argument("local"); q.add_argument("remote"); q.add_argument("-r", "--recursive", action="store_true")
    q.add_argument("-n", "--no-clobber", action="store_true"); q.set_defaults(fn=cmd_fs_put)
    q = f.add_parser("write", help="write stdin to a remote file"); q.add_argument("remote")
    q.add_argument("--append", action="store_true"); q.add_argument("-n", "--no-clobber", action="store_true")
    q.set_defaults(fn=cmd_fs_write)
    q = f.add_parser("edit", help="edit a remote file in $EDITOR or with --replace OLD NEW")
    q.add_argument("remote"); q.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"))
    q.set_defaults(fn=cmd_fs_edit)
    q = f.add_parser("mkdir", help="create a folder (with parents)"); q.add_argument("path"); q.set_defaults(fn=cmd_fs_mkdir)
    q = f.add_parser("mv", help="move/rename"); q.add_argument("src"); q.add_argument("dst")
    q.add_argument("-f", "--force", action="store_true", help="overwrite destination"); q.set_defaults(fn=cmd_fs_mv)
    q = f.add_parser("rm", help="delete (requires --yes)"); q.add_argument("path")
    q.add_argument("-r", "--recursive", action="store_true"); q.add_argument("--yes", action="store_true")
    q.set_defaults(fn=cmd_fs_rm)
    p = sub.add_parser("raw", help="call any gateway method (escape hatch)")
    p.add_argument("method"); p.add_argument("--body", default=None)
    p.add_argument("--param", action="append", default=[], help="path param k=v (repeatable)")
    p.add_argument("--timeout", type=int, default=30); p.set_defaults(fn=cmd_raw)

    args = ap.parse_args(argv)
    if profile_error:
        ap.error(profile_error)
    try:
        activate_profile(resolve_profile(args.profile))
    except ValueError as e:
        ap.error(str(e))
    if args.cmd is None:
        args.cmd, args.fn = "chat", _chat
    # A piped command prints JSON. The notice stays off unless both streams
    # are a terminal, and `update` is already doing the upgrade.
    notice = None if args.cmd == "update" else start_update_check()
    try:
        try:
            args.fn(args)
        except AuthError as e:
            print(f"auth error: {e}", file=sys.stderr)
            sys.exit(2)
        except GatewayError as e:
            print(f"gateway error: {e}", file=sys.stderr)
            sys.exit(3)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        except TimeoutError as e:
            print(f"timeout: {e}", file=sys.stderr)
            sys.exit(4)
    finally:
        finish_update_check(notice)


if __name__ == "__main__":
    main()
