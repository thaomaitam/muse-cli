"""Multi-profile support: --profile / MUSE_CLI_PROFILE and `auth list`."""
import json
import os
import stat

import pytest

from muse_cli import chat, cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HOME, no profile env var, and the active profile restored afterwards."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(cli.PROFILE_ENV, raising=False)
    monkeypatch.delenv("MUSE_AUTO_APPROVE", raising=False)
    for attr in ("PROFILE", "CONFIG_DIR", "CONFIG_FILE", "COOKIES_FILE"):
        monkeypatch.setattr(cli, attr, getattr(cli, attr))
    cli.activate_profile(cli.DEFAULT_PROFILE)
    return tmp_path


def base(home):
    return os.path.join(str(home), ".config", "muse-cli")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def no_network(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("network used")
    monkeypatch.setattr(cli, "Gateway", boom)
    monkeypatch.setattr(cli, "connect", boom)


# -- default profile is the original layout ------------------------------------
def test_default_paths_unchanged(home):
    b = base(home)
    assert cli.profile_paths("default") == {
        "dir": b, "config": os.path.join(b, "config.json"),
        "cookies": os.path.join(b, "cookies.txt"), "chat": os.path.join(b, "chat.json")}
    cli.activate_profile(cli.resolve_profile())
    assert cli.PROFILE == "default"
    assert cli.CONFIG_DIR == b
    assert cli.CONFIG_FILE == os.path.join(b, "config.json")
    assert cli.COOKIES_FILE == os.path.join(b, "cookies.txt")
    assert chat.state_file() == os.path.join(b, "chat.json")
    assert "profiles" not in cli.COOKIES_FILE


def test_import_time_default_is_home_config(tmp_path):
    # A fresh interpreter, before any profile is chosen, sees the old paths.
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != cli.PROFILE_ENV}
    env["HOME"] = str(tmp_path)
    code = ("from muse_cli import cli; "
            "print(cli.PROFILE, cli.CONFIG_DIR, cli.CONFIG_FILE, cli.COOKIES_FILE)")
    got = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, check=True).stdout.split()
    b = os.path.join(str(tmp_path), ".config", "muse-cli")
    assert got == ["default", b, os.path.join(b, "config.json"), os.path.join(b, "cookies.txt")]
    assert not os.path.exists(b)


def test_explicit_default_profile_is_base_dir(home, monkeypatch):
    monkeypatch.setenv(cli.PROFILE_ENV, "default")
    cli.activate_profile(cli.resolve_profile())
    assert cli.CONFIG_DIR == base(home)
    cli.activate_profile(cli.resolve_profile("default"))
    assert cli.COOKIES_FILE == os.path.join(base(home), "cookies.txt")


def test_default_profile_files_are_not_moved(home, capsys):
    b = base(home)
    os.makedirs(b)
    with open(os.path.join(b, "cookies.txt"), "w") as fh:
        fh.write("hatch_sess=x\n")
    with open(os.path.join(b, "config.json"), "w") as fh:
        json.dump({"auto_approve": "always", "node_id": "n1"}, fh)
    before = sorted(os.listdir(b))
    cli.main(["auth", "list"])
    cli.main(["--profile", "work", "auth", "list"])
    assert sorted(os.listdir(b)) == before
    assert json.load(open(os.path.join(b, "config.json")))["auto_approve"] == "always"
    capsys.readouterr()


# -- resolution order ------------------------------------------------------------
def test_resolve_none_is_default(home):
    assert cli.resolve_profile() == "default"
    assert cli.resolve_profile(None, {}) == "default"
    assert cli.resolve_profile(None, {cli.PROFILE_ENV: "  "}) == "default"


def test_resolve_env(home, monkeypatch):
    monkeypatch.setenv(cli.PROFILE_ENV, "work")
    assert cli.resolve_profile() == "work"
    assert cli.profile_paths("work")["cookies"] == os.path.join(
        base(home), "profiles", "work", "cookies.txt")


def test_resolve_flag(home):
    assert cli.resolve_profile("work") == "work"


def test_flag_beats_env(home, monkeypatch, capsys):
    monkeypatch.setenv(cli.PROFILE_ENV, "fromenv")
    assert cli.resolve_profile("fromflag") == "fromflag"
    cli.main(["--profile", "fromflag", "auth", "list"])
    assert cli.PROFILE == "fromflag"
    assert cli.CONFIG_DIR == os.path.join(base(home), "profiles", "fromflag")
    assert "* fromflag" in capsys.readouterr().out


def test_env_used_by_main(home, monkeypatch, capsys):
    monkeypatch.setenv(cli.PROFILE_ENV, "work")
    cli.main(["auth", "list"])
    assert cli.PROFILE == "work"
    assert "* work" in capsys.readouterr().out


def test_named_profile_paths(home):
    p = cli.activate_profile("work")
    d = os.path.join(base(home), "profiles", "work")
    assert p == {"dir": d, "config": os.path.join(d, "config.json"),
                 "cookies": os.path.join(d, "cookies.txt"), "chat": os.path.join(d, "chat.json")}
    assert (cli.CONFIG_DIR, cli.CONFIG_FILE, cli.COOKIES_FILE) == (d, p["config"], p["cookies"])
    assert chat.state_file() == p["chat"]
    assert not os.path.exists(d)  # activating creates nothing


def test_prescan_only_reads_flag_before_subcommand():
    assert cli._prescan_profile(["--profile", "a", "status"]) == "a"
    assert cli._prescan_profile(["--profile=b", "status"]) == "b"
    assert cli._prescan_profile(["send", "--profile", "x"]) is None
    assert cli._prescan_profile(["--", "--profile", "x"]) is None
    assert cli._prescan_profile([]) is None


# -- slug validation ---------------------------------------------------------------
@pytest.mark.parametrize("name", ["work", "a", "A-b_9", "x" * 64, "default"])
def test_valid_names(name):
    assert cli.validate_profile_name(name) == name


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "/abs", "a b", "work.",
                                  "x" * 65, "é", "a\\b", "~", "a\nb", None])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        cli.validate_profile_name(name)


@pytest.mark.parametrize("name", ["../evil", "a/b", ""])
def test_invalid_flag_exits_2(home, name, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--profile", name, "auth", "list"])
    assert e.value.code == 2
    assert "invalid profile name" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(str(home), ".config"))


def test_invalid_env_exits_2(home, monkeypatch, capsys):
    monkeypatch.setenv(cli.PROFILE_ENV, "../../etc")
    with pytest.raises(SystemExit) as e:
        cli.main(["auth", "list"])
    assert e.value.code == 2
    assert "invalid profile name" in capsys.readouterr().err


# -- auth list -------------------------------------------------------------------------
def make_profile(home, name, cookies):
    d = cli.profile_dir(name)
    os.makedirs(d, exist_ok=True)
    if cookies:
        with open(os.path.join(d, "cookies.txt"), "w") as fh:
            fh.write("hatch_sess=x\n")
    return d


def test_auth_list_text(home, monkeypatch, capsys):
    no_network(monkeypatch)
    make_profile(home, "default", cookies=True)
    make_profile(home, "work", cookies=False)
    make_profile(home, "alt", cookies=True)
    pdir = os.path.join(base(home), "profiles")
    os.makedirs(os.path.join(pdir, "bad name"))           # not a valid slug: skipped
    open(os.path.join(pdir, "stray-file"), "w").close()   # not a dir: skipped
    cli.main(["--profile", "work", "auth", "list"])
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split()[0] if not ln.startswith("*") else "*" + ln.split()[1] for ln in lines] == \
        ["default", "alt", "*work"]
    assert "cookies" in lines[0] and "no cookies" not in lines[0]
    assert lines[1].split()[1] == "cookies"
    assert lines[2].startswith("* work") and "no cookies" in lines[2]
    assert lines[2].rstrip().endswith(os.path.join(pdir, "work"))


def test_auth_list_json_marks_default_active(home, monkeypatch, capsys):
    no_network(monkeypatch)
    make_profile(home, "work", cookies=True)
    cli.main(["auth", "list", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["active"] == "default"
    rows = {r["name"]: r for r in doc["profiles"]}
    assert rows["default"]["active"] and not rows["work"]["active"]
    assert rows["work"]["cookies"] is True
    assert rows["default"]["cookies"] is False
    assert rows["default"]["dir"] == base(home)


def test_auth_list_is_read_only(home, monkeypatch, capsys):
    no_network(monkeypatch)
    cli.main(["--profile", "scratch", "auth", "list"])
    out = capsys.readouterr().out
    assert "* scratch" in out and "not created" in out
    assert not os.path.exists(os.path.join(str(home), ".config"))


def test_empty_cookies_file_counts_as_no_cookies(home, capsys):
    d = make_profile(home, "work", cookies=False)
    open(os.path.join(d, "cookies.txt"), "w").close()
    cli.main(["--profile", "work", "auth", "list", "--json"])
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["profiles"]}
    assert rows["work"]["cookies"] is False


# -- per-profile files, settings, and messages ---------------------------------------
def test_new_profile_gets_fresh_defaults(home):
    b = base(home)
    os.makedirs(b)
    with open(os.path.join(b, "config.json"), "w") as fh:
        json.dump({"auto_approve": "always", "node_id": "main"}, fh)
    cli.activate_profile("work")
    cfg = cli.load_config()
    assert "auto_approve" not in cfg
    assert cfg["node_id"] != "main"
    assert cfg["cookies_file"] == cli.COOKIES_FILE
    enabled, source = cli.auto_approve_setting(cfg)
    assert enabled is False and cli.CONFIG_FILE in source
    d = os.path.join(b, "profiles", "work")
    assert mode(d) == 0o700
    assert json.load(open(os.path.join(b, "config.json")))["auto_approve"] == "always"


def test_auto_approve_is_per_profile(home, capsys):
    cli.main(["--profile", "work", "auto-approve", "on"])
    cli.main(["auto-approve", "off"])
    cli.activate_profile("work")
    assert cli.auto_approve_setting(cli.load_config())[0] is True
    cli.activate_profile("default")
    assert cli.auto_approve_setting(cli.load_config())[0] is False
    capsys.readouterr()


def test_copied_config_does_not_borrow_default_cookies(home):
    d = make_profile(home, "work", cookies=False)
    with open(os.path.join(d, "config.json"), "w") as fh:
        json.dump({"cookies_file": os.path.join(base(home), "cookies.txt"), "node_id": "n"}, fh)
    cli.activate_profile("work")
    assert cli.load_config()["cookies_file"] == os.path.join(d, "cookies.txt")


def test_auth_export_writes_profile_cookies(home, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/agent-browser")
    jar = [{"domain": ".muse.ai", "name": "hatch_sess", "value": "v", "path": "/"}]
    monkeypatch.setattr(cli, "_browser_cookies", lambda: (jar, None))
    cli.main(["--profile", "work", "auth", "export"])
    d = os.path.join(base(home), "profiles", "work")
    f = os.path.join(d, "cookies.txt")
    assert "hatch_sess\tv" in open(f).read()
    assert mode(f) == 0o600 and mode(d) == 0o700
    assert mode(os.path.join(base(home), "profiles")) == 0o700
    assert not os.path.exists(os.path.join(base(home), "cookies.txt"))
    assert f in capsys.readouterr().out


def test_messages_show_active_profile_path(home, capsys):
    cli.activate_profile("work")
    with pytest.raises(cli.AuthError) as e:
        cli.connect(cli.load_config())
    msg = str(e.value)
    assert cli.COOKIES_FILE in msg and "muse-cli --profile work auth export" in msg
    cli._print_hand_copy()
    err = capsys.readouterr().err
    assert f"Write one line to {cli.COOKIES_FILE}" in err
    assert f"chmod 600 {cli.COOKIES_FILE}" in err
    assert "muse-cli --profile work status" in err
    assert "~/.config/muse-cli/cookies.txt" not in err


def test_default_messages_keep_plain_commands(home, capsys):
    cli._print_chrome_debug_steps()
    err = capsys.readouterr().err
    assert "Run `muse-cli auth export` again." in err
    assert "--profile" not in err
    assert os.path.join(base(home), "cookies.txt") in err


def test_export_help_shows_profile_path(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--profile", "work", "auth", "export", "--help"])
    out = capsys.readouterr().out
    assert os.path.join(base(home), "profiles", "work", "cookies.txt") in out


def test_chat_state_is_per_profile(home):
    cli.activate_profile("work")
    chat.save_state({"session_id": "s1", "title": "t"})
    assert json.load(open(os.path.join(base(home), "profiles", "work", "chat.json")))["session_id"] == "s1"
    cli.activate_profile("default")
    assert chat.load_state()["session_id"] is None
