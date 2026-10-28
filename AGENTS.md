# muse-cli agent notes

## Layout

- `src/muse_cli/` is the package: `cli.py` (commands), `gateway.py` (auth,
  Noise transport), plus `routes.json` and `desc0.bin`/`desc1.bin` data files.
- `tgbot.py` is `muse-cli telegram` (bot loop, commands, systemd unit) and
  `tgapi.py` its Telegram Bot API client and formatting (curl_cffi, no bot
  framework). Bot logic calls the data helpers in `cli.py` (`deliver`,
  `run_wait`, `session_op`, `status_data`, ...), never the CLI's print paths.
  Tests drive it with fakes in `tests/test_telegram.py`.
- `pyproject.toml` holds all packaging metadata (hatchling). The version is
  read from `__version__` in `src/muse_cli/__init__.py`; that is the only
  place it lives.
- Fork-only: this repo is github.com/thoitiettxl-cyber/muse-cli. Never install
  from, publish to, or point anything at PyPI or upstream nikships/muse-cli
  (they lack the fork's fixes). Installs come from `git+` the fork's `main`.
- Profiles: `--profile NAME` / `MUSE_CLI_PROFILE`. `default` is
  `~/.config/muse-cli/` and must never move; named profiles are
  `~/.config/muse-cli/profiles/NAME/`. Code reads `cli.CONFIG_DIR`,
  `CONFIG_FILE`, `COOKIES_FILE` at call time (set by `activate_profile`);
  never capture them at import. `update.json` (update check) stays global.
- No CI: there are no GitHub Actions workflows (the account has no Actions
  minutes). Run tests locally before pushing.

## Rule for agents driving Muse from an agent: never poll

- Never poll with `sleep` + `history` loops.
- Send with `muse-cli send "..." --until-done` (blocks until the turn ends), or
  `muse-cli send "..." --wait 0` (returns once delivered, prints `user_seq`) and
  then run `muse-cli wait --after-seq <user_seq> --timeout 1800` as a
  background command and act when it exits.
- `send` always waits for the delivery ack (our message appears in history
  with a seq) before exiting; it exits 5 if no ack arrives. Never close the
  connection before that (no raw fire-and-forget).
- Exit codes of `wait` and `send --wait/--until-done`:
  `0` turn done (final replies printed) · `2` task failed/cancelled ·
  `4` needs a human: a question in the final reply (widget or plain text,
  printed), or a pending approval when auto-approve is off or could not
  decide it (`muse-cli approvals`, then `muse-cli approve ID --always` or
  `muse-cli deny ID`; ask the user first) · `124` timeout (run `wait` again).
- After every `wait`/`send` read the final reply text (`final_text` in JSON,
  or the printed reply), not just the exit code. Outcomes: `done`,
  `needs_input` (Muse asked you something in plain text, a widget, a
  non-empty gate/policy question, or stopped on a safety check; `question`
  holds it), `needs_approval`, `failed`, `timeout`. `send --text` prints the
  reply as plain text instead of JSON.


## Approvals and auto-approve

Muse's approval prompt (e.g. `network_access` to `uploads.github.com`) has
three choices: Allow once (`allow_once`), Always allow this site
(`allow_always`, the "forever" choice) and Deny (`deny`). `approve --always`
sends `allow_always`.

Auto-approve makes `wait` and `send --wait/--until-done` decide every pending
approval with `allow_always` and keep waiting for the turn instead of
exiting 4. When the separate `auto_allow_once` fallback toggle is on (and
auto-approve is on), an approval that offers no forever option is decided
with `allow_once` instead of being left for a human; the fallback applies
to every kind, never picks `options[0]`, and never uses session/today.
Each decision is logged on stderr with its real decision
(`auto-approved ID (allow_always|allow_once): ... [kind scope]`) and listed
in the JSON result as `auto_approved` (`approval_id`, `title`, `kind`,
`scope`, `decision`). The shared helper is `choose_auto_decision` in
`wait.py`, used by both `make_auto_approver` (`cli.py`) and
`Bot.approve_forever` (`tgbot.py`). Questions (`needs_input`) still exit
4: they can't be auto-answered. An approval with neither option, or a
failed decide, is reported in `auto_approve_errors` and still exits 4 with
`needs_approval`. An approval that offers no forever option is warned
about at most once per id (a `warned` set that gates only the warning);
the warning names its title, kind, scope and offered options. If the
monitor and a turn present the same approval at once, the second caller
waits up to 15 seconds for the in-flight claim to resolve instead of
ending the turn as `needs_approval`; if the claim fails or times out,
the second caller decides it itself.

```bash
muse-cli auto-approve status   # {"auto_approve": "always"|"off", "auto_allow_once": true|false, ...}
muse-cli auto-approve on       # persistent, in the active profile's config.json
muse-cli auto-approve off
muse-cli auto-approve once on|off   # the allow_once fallback toggle (default off)
MUSE_AUTO_APPROVE=always|off   # env override for one shell/process
MUSE_AUTO_ALLOW_ONCE=on|off    # env override for the fallback, one shell/process
muse-cli wait ... --auto-approve | --no-auto-approve   # per call (send too)
muse-cli wait ... --auto-allow-once | --no-auto-allow-once   # per call (send too)
muse-cli approve --all --always   # approve everything pending now, forever
muse-cli approvals --approve-all  # same
```

Precedence (each toggle separately): flag, then its env var, then the
config. Auto-approve defaults to off; the fallback defaults to off. The
Telegram bot defaults auto-approve to on and the fallback to off;
`/autoapprove once on|off|status` toggles the fallback, stored in
`telegram-state.json` and mirrored to the profile's `config.json`, and
takes effect in the running bot. By contrast `muse-cli auto-approve once
on|off` writes only `config.json`: the bot reads `telegram-state.json`
once at startup and never reloads it, so a running bot needs a restart
to pick the CLI change up. `muse-cli telegram status` reports the
bot's stored `auto_allow_once` value.

## Cutting a release

There is no PyPI and no CI. A release is a version bump on the fork's `main`;
`install.sh` and `muse-cli update` install `git+https://github.com/thoitiettxl-cyber/muse-cli.git`
(main), and the daily update notice compares the installed version with
`__version__` in `src/muse_cli/__init__.py` on main. When asked to "cut a
release", "publish a new version", or similar, follow these steps in order.
Stop and report if any step fails.

1. **Pick the version.** Use the version the user gave. If they didn't give
   one, propose one using semantic versioning (patch for fixes, minor for
   new features or behavior changes, major for breaking CLI changes) and
   confirm it with them. Check it is unused:

   ```bash
   git fetch --tags origin
   git tag -l 'v*'
   ```

2. **Check the working tree.** Be on `main`, up to date with `origin/main`
   (`thoitiettxl-cyber/muse-cli`), with no uncommitted changes
   (`git status -sb`). Git author identity must already be configured; never
   set it yourself.

3. **Bump the version** in `src/muse_cli/__init__.py`, nothing else.
   `pyproject.toml` reads it from there (`dynamic = ["version"]`); don't add a
   version to `pyproject.toml`.

4. **Test locally** (no CI will do it for you):

   ```bash
   uv run --no-project --with-editable . --with pytest pytest -q tests
   rm -rf /tmp/muse-release && uv venv -q /tmp/muse-release
   uv pip install -q --python /tmp/muse-release/bin/python .
   /tmp/muse-release/bin/muse-cli --version   # must print the new version
   rm -rf /tmp/muse-release
   ```

5. **Commit and push the bump to the fork's main.** This is the release:
   installs and `muse-cli update` pick it up from main.

   ```bash
   git commit -am "chore(release): bump version to X.Y.Z"
   git push origin main
   ```

6. **Optional: tag and create a GitHub release** on the fork:

   ```bash
   git tag -a vX.Y.Z -m "muse-cli X.Y.Z"
   git push origin vX.Y.Z
   gh release create vX.Y.Z -R thoitiettxl-cyber/muse-cli --title "muse-cli X.Y.Z" --generate-notes
   ```

7. **Verify it is live:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/thoitiettxl-cyber/muse-cli/main/src/muse_cli/__init__.py | grep __version__
   uv tool run --from git+https://github.com/thoitiettxl-cyber/muse-cli.git muse-cli --version
   ```

   raw.githubusercontent.com can cache for a few minutes; retry before
   assuming failure. Report the commit (and release, if made) links.

### If something goes wrong

- **Bad version pushed to main:** fix forward with a new patch version; don't
  force-push `main`.
- **Wrong tag:** ask the user before deleting and re-pushing it
  (`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`, and
  `gh release delete vX.Y.Z -R thoitiettxl-cyber/muse-cli` if a release exists).

### Don't

- Don't build for or upload to PyPI (`twine`, `uv publish`, publish
  workflows), and don't add a PyPI install path back to `install.sh`,
  `update.py`, the skill, or the README.
- Don't add GitHub Actions workflows unless the user asks; there are no
  minutes for them.
