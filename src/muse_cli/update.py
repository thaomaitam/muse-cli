"""Tell an interactive user when a newer muse-cli is on the fork's main.

This build tracks only the fork at github.com/thoitiettxl-cyber/muse-cli,
never PyPI or upstream. The check reads `__version__` from the fork's main
branch over an unauthenticated raw fetch.

Same shape as `gh` and `uv`: look at most once a day, never on a pipe or in
CI, never fail the command, and print the upgrade command instead of
replacing the install. uv tools do not upgrade themselves.

A source checkout (editable or plain git clone) is never replaced: `update`
prints the `git pull` to run instead.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request

from . import __version__

CHECK_INTERVAL = 24 * 60 * 60
FAIL_BACKOFF = 60 * 60
STATE_FILE = os.path.expanduser("~/.config/muse-cli/update.json")
FORK = "thoitiettxl-cyber/muse-cli"
REPO_URL = f"https://github.com/{FORK}.git"
INSTALL_SPEC = f"git+{REPO_URL}"
VERSION_URL = f"https://raw.githubusercontent.com/{FORK}/main/src/muse_cli/__init__.py"
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def version_key(value):
    """Stable x.y.z as a tuple. Pre-releases and junk compare as absent."""
    text = str(value).strip().lstrip("v").split("+", 1)[0]
    if not text or any(c.isalpha() for c in text):
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def is_newer(latest, current):
    new, old = version_key(latest), version_key(current)
    return bool(new and old and new > old)


def source_checkout(pkg_file=None):
    """Repo root when running from a git checkout (editable install), else None."""
    pkg_dir = os.path.dirname(os.path.realpath(pkg_file or __file__))
    root = os.path.dirname(os.path.dirname(pkg_dir))  # src/muse_cli -> repo
    if os.path.exists(os.path.join(root, ".git")) and os.path.isfile(
            os.path.join(root, "pyproject.toml")):
        return root
    return None


def upgrade_argv():
    """The upgrade command for this install: git pull, uv tool, pipx, or pip."""
    root = source_checkout()
    if root:
        return ["git", "-C", root, "pull", "--ff-only"]
    prefix = os.path.realpath(sys.prefix)
    sep = os.sep
    # uv and pipx leave a receipt in the tool env; the path check covers
    # default locations when the receipt is missing.
    if (f"{sep}uv{sep}tools{sep}" in prefix
            or os.path.isfile(os.path.join(prefix, "uv-receipt.toml"))):
        return ["uv", "tool", "install", "--upgrade", INSTALL_SPEC]
    if (f"{sep}pipx{sep}" in prefix
            or os.path.isfile(os.path.join(prefix, "pipx_metadata.json"))):
        return ["pipx", "install", "--force", INSTALL_SPEC]
    return [sys.executable, "-m", "pip", "install", "-U", INSTALL_SPEC]


def upgrade_command():
    return " ".join(upgrade_argv())


def _should_check():
    if os.environ.get("MUSE_NO_UPDATE_CHECK"):
        return False
    if os.environ.get("CI"):
        return False
    try:
        return sys.stdout.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def parse_version(text):
    match = _VERSION_RE.search(text or "")
    if not match:
        raise ValueError("no __version__ found")
    return match.group(1)


def _fetch_latest():
    req = urllib.request.Request(
        VERSION_URL, headers={"User-Agent": f"muse-cli/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return parse_version(resp.read().decode())


def start_update_check():
    """Begin a check, or return a cached result. None when the notice is off."""
    if not _should_check():
        return None
    state = _load_state()
    latest = state.get("latest") or ""
    checked = float(state.get("checked_at") or 0)
    if latest and time.time() - checked < CHECK_INTERVAL:
        return {"latest": latest}
    holder = {"latest": None, "thread": None}

    def work():
        try:
            found = _fetch_latest()
            holder["latest"] = found
            _save_state({"checked_at": time.time(), "latest": found})
        except Exception:
            # Offline or GitHub blip: try again in an hour, keep any known version.
            holder["latest"] = latest or None
            _save_state({
                "checked_at": time.time() - CHECK_INTERVAL + FAIL_BACKOFF,
                "latest": latest,
            })

    thread = threading.Thread(target=work, daemon=True)
    holder["thread"] = thread
    thread.start()
    return holder


def finish_update_check(holder):
    """Print the notice on stderr once the check has had a moment to finish."""
    if not holder:
        return
    thread = holder.get("thread")
    if thread is not None:
        thread.join(timeout=1.5)
    latest = holder.get("latest")
    if not is_newer(latest, __version__):
        return
    print(
        f"\nA new release of muse-cli is available: {__version__} → {latest}\n"
        f"To upgrade, run: {upgrade_command()}\n",
        file=sys.stderr,
    )


def cmd_update(_args):
    root = source_checkout()
    if root:
        # Never swap a checkout for a remote install; the user owns this tree.
        print(f"muse-cli runs from a source checkout at {root}.", file=sys.stderr)
        print(f"To update it, run: git -C {root} pull --ff-only", file=sys.stderr)
        return
    argv = upgrade_argv()
    if argv[0] != sys.executable and shutil.which(argv[0]) is None:
        print(f"{argv[0]} is not on PATH.", file=sys.stderr)
        print(f"To upgrade, run: {upgrade_command()}", file=sys.stderr)
        sys.exit(1)
    print(f"running: {upgrade_command()}", file=sys.stderr)
    import subprocess
    raise SystemExit(subprocess.call(argv))
