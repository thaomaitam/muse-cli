---
name: muse-cli
description: Manage a personal muse.ai agent from the terminal (send messages, read chats, watch live events, feed/goals/ideas/sessions). Use when asked to message, check, or automate someone's Muse personal AI, work with muse.ai chats/threads/feed/goals outside the browser, or when the muse-cli tool itself needs installing or auth setup.
---

# muse-cli: drive a muse.ai personal agent from the terminal

Assume a bare machine: only this skill is present. No repo clone, no
dependencies, no auth. Work top to bottom; stop at the first step that
fails and report it.

The `muse-cli` package (from the fork github.com/thoitiettxl-cyber/muse-cli,
not PyPI) talks to the muse.ai personal gateway
directly. No browser needed after the one-time cookie export. Every command
prints JSON.

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/thoitiettxl-cyber/muse-cli/main/install.sh | bash
```

This installs the CLI from the fork with
`uv tool install git+https://github.com/thoitiettxl-cyber/muse-cli.git` (its
own isolated environment) and this skill (to `~/.agents/skills/muse-cli`; on a
Grok Bot box to `/home/box/skill-assets/muse-cli`; `MUSE_CLI_SKILLS`
overrides). CLI only? Use that same `uv tool install git+...` line, or
`pipx install git+https://github.com/thoitiettxl-cyber/muse-cli.git`. Never
install `muse-cli` from PyPI: it lacks the fork's fixes. The command is `muse-cli` (`muse` clashes
with Muse Code, don't use it). Verify before continuing:

```bash
command -v muse-cli
muse-cli --help >/dev/null && echo cli-ok   # proves the install + deps resolve
```

## 2. Auth

The CLI stores the user's muse.ai browser login at
`~/.config/muse-cli/cookies.txt` (mode 0600). Chrome hides those cookies
until remote debugging is on. Walk the user through the steps below and wait
for them. Do not run `muse-cli auth export` before they say the Chrome
switch is on and a logged-in muse.ai tab is open.

Access and gateway tokens are fetched fresh on every run; only cookies
persist. When a later command fails with `auth error`, the cookies expired:
repeat 2b (or 2c).

### 2a. Cookie reader

```bash
command -v node >/dev/null && node --version
command -v npm >/dev/null && npm --version
command -v agent-browser >/dev/null && agent-browser --version
```

If `agent-browser` is missing and `npm` works:

```bash
npm i -g agent-browser
agent-browser --version   # must print a version
command -v agent-browser  # must print a path
```

`EACCES` from `npm i -g` means npm cannot write its global bin directory.
Stop and tell the user. They fix it with an npm prefix they own
(https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally)
or by installing with the same rights they use for other global npm tools.
Do not sudo unless they tell you to.

If `node` or `npm` is missing, stop. Tell them to install Node.js LTS from
https://nodejs.org, open a new terminal, and come back. Offer 2c if they
would rather not install Node.

### 2b. Chrome, then export

Tell the user to do all of this in the Google Chrome window they already
use for muse.ai. Needs Chrome 144 or newer. Wait until they confirm:

1. Address bar: `chrome://inspect/#remote-debugging`
2. Turn on remote debugging ("Allow remote debugging for this browser instance").
3. Open https://muse.ai/ and log in. Leave that tab in front. The export
   reads the active tab.

Then:

```bash
muse-cli auth export
test -s ~/.config/muse-cli/cookies.txt && echo auth-ok
```

If Chrome shows an Allow prompt, they click Allow. If the command failed
before that click, run it once more.

Success is a line like `saved N muse.ai cookies to ~/.config/muse-cli/cookies.txt`.

If it fails, the CLI prints the next step. Map it like this, and do not retry
in a loop:

- "No running Chrome" or "remote-debugging-port": the switch in step 2 is
  off, or they are in a different Chrome. Send them back to the steps above.
  Do not tell them to launch Chrome with `--remote-debugging-port`. That
  opens another profile, and it does not have their muse.ai login.
- "no muse.ai tab": Chrome connected. They still need https://muse.ai/ open
  and in front in that same window.
- "no hatch_sess" / "do not include hatch_sess": the tab is open and they
  are logged out. The cookies file on disk was kept.
- "daemon already running": `agent-browser close`, then `muse-cli auth export` once.
- `agent-browser` not found: back to 2a.

### 2c. Copy cookies by hand

Use this when Node is unavailable, or the user does not want remote debugging on.

1. They are logged in at https://muse.ai/ in Chrome.
2. DevTools (F12) → Application → Cookies → `https://muse.ai`.
3. Write one line to `~/.config/muse-cli/cookies.txt`. `hatch_sess` is required.
   Separate cookies with `; `:

```
hatch_sess=VALUE; other_name=other_value
```

A Netscape jar or `{"hatch_sess": "..."}` JSON also works.

4. `chmod 600 ~/.config/muse-cli/cookies.txt`
5. `test -s ~/.config/muse-cli/cookies.txt && echo auth-ok`, then `muse-cli status`.
   Stop if status fails and report the error text.

## 3. Verify end to end

```bash
muse-cli status    # VM id, chat count, unread, identity: install + auth proven
```

## Everyday commands

```bash
muse-cli threads                                        # main chat + side chats (session_ids)
muse-cli history --limit 5                              # recent main-chat messages
muse-cli history --thread <session-id> --limit 5        # one side chat
muse-cli send "message" --wait 120                      # send + wait for the reply
muse-cli send "message" --thread <session-id> --wait 0  # fire and forget to a side chat
muse-cli watch --timeout 60                             # tail live agent events
muse-cli watch --thread <session-id>                    # tail one side chat
muse-cli usage                                          # weekly limit % used, reset time, bonus tokens
muse-cli usage --json
muse-cli invite [--json]                                # invite code, uses left, rewards (read-only)
muse-cli files --limit 20                               # images/videos the agent produced
muse-cli download <seq|message-id|sandbox://...> -o DIR # save them locally
muse-cli history --limit 3 --download DIR               # save files from these messages
muse-cli send "make an image of X" --wait 300 --download DIR
muse-cli feed --limit 5
muse-cli goals
muse-cli ideas
muse-cli unread
muse-cli seen <thread-id>
```

Management (visible side effects, confirm with the user first when destructive):

```bash
muse-cli feed-react <unit-id> love
muse-cli idea-exec <idea-id>          # the agent acts on the idea (real work)
muse-cli session-start --title "x"    # new side chat
muse-cli session-rename <id> "title"  # also: pin, unpin, archive, unarchive, delete
muse-cli wake
muse-cli redeem <CODE>                # redeem a friend's invite code: one-time, ask first
```

## Driving Muse from an agent: never poll

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
`scope`, `decision`). Questions (`needs_input`) still exit 4: they can't
be auto-answered. An approval with neither option, or a failed decide, is
reported in `auto_approve_errors` and still exits 4 with `needs_approval`.
An approval that offers no forever option is warned about at most once
per id; the warning names its title, kind, scope and offered options.

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
config. Auto-approve defaults to off; the fallback defaults to off.

On the user's Grok Bot box auto-approve is ON for the default profile (the
user enabled it permanently); check with `muse-cli auto-approve status` and do not turn it off
unless the user asks.

## Profiles (several Muse logins)

Every command reads one profile: its cookies, config (auto-approve etc.) and
chat state. With no flag and no env var it is `default`, the original
`~/.config/muse-cli/` (the user's main Muse; never move or edit its files by
hand). A named profile is `~/.config/muse-cli/profiles/NAME/` (folder 700,
cookies 600). Names: letters, digits, `.`, `-`, `_` (no leading/trailing `.`).

```bash
muse-cli auth list                       # offline: profiles, active one (*), cookies or not
muse-cli --profile work auth export      # log a second account in (same Chrome steps as 2b)
muse-cli --profile work status
MUSE_CLI_PROFILE=work muse-cli send "hello" --until-done
muse-cli --profile work auto-approve on  # per-profile; new profiles start with it off
```

`--profile` goes before the subcommand and wins over `MUSE_CLI_PROFILE`.
Errors and help print the active profile's real cookie path and the exact
`muse-cli --profile NAME ...` command to run. For a named profile, use its
path wherever 2b/2c say `~/.config/muse-cli/cookies.txt`.

## System Files (fs)

Paths are relative to the System Files root (the agent home: AGENTS.md,
MEMORY.md, agents/, config/, memory/, workspace/ ...). Absolute paths and
`..` are rejected by the server; a leading `/` is stripped for you.

```bash
muse-cli fs ls [PATH] [-l] [-a] [--json]           # System Files (agent home); "" or / is the root
muse-cli fs tree memory --depth 3                 # recursive listing
muse-cli fs stat MEMORY.md
muse-cli fs cat config/home.yaml                  # text to stdout (binary refused unless --binary)
muse-cli fs get workspace/imagine_media/x.mp4 -o ./out   # download (-r for folders)
muse-cli fs find hello                            # search by name substring
muse-cli fs put ./notes.md docs/notes.md          # upload (-r for folders, -n no-clobber)
echo hi | muse-cli fs write data/hi.txt           # stdin -> file (--append)
muse-cli fs edit docs/notes.md --replace OLD NEW  # or no flags: opens $EDITOR
muse-cli fs mkdir data/new ; muse-cli fs mv a b [-f] ; muse-cli fs rm PATH [-r] --yes
```

Writes (`put`, `write`, `edit`, `mkdir`, `mv`, `rm`) change files Muse reads
(MEMORY.md, SOUL.md, config/*). Confirm with the user before touching existing
files; `rm` requires `--yes`.

## Telegram bot

Operate Muse from Telegram (chat with streamed replies, buttons for Muse's
questions, photos/files into System Files `telegram-inbox/`, chats,
approvals, fs, feed, goals, ideas, usage, profiles, update). One-time setup
on the machine that runs it, after creating a bot with @BotFather:

```bash
muse-cli telegram setup             # token at a hidden prompt (or piped on stdin, never an argument);
                                    # then send `/start CODE` to the bot to pair the owner
muse-cli telegram install-service   # systemd unit, Restart=always (root; --user for a user unit)
muse-cli telegram status            # config, allowed users, token check, service state
muse-cli telegram run               # foreground instead of a service
```

Config is `telegram.json` (mode 600) in the active profile's folder; use
`--profile NAME` before `telegram` for another login. Bot commands: /help
/status /stop /chats /main /new /history /unread /rename /pin /unpin /archive
/unarchive /delete /approvals /autoapprove /watch /files /fs /feed /goals
/goal /ideas /usage /wake /profile /login /update /raw. Auto-approve is on by
default in the bot (allow_always). The allow_once fallback is off by
default; enable it with `/autoapprove once on` (stored in
`telegram-state.json`, mirrored to the profile's `config.json`, effective
in the running bot); check both
toggles with `/autoapprove once status`. `muse-cli auto-approve once on|off`
writes only `config.json`, so a running bot needs a restart to pick it up;
`muse-cli telegram status` reports the bot's stored value. `muse-cli stop [--thread ID]` stops
Muse's current turn from the terminal (`chat.cancel`).

## Escape hatch

`muse-cli raw <method> --body '{...}' [--param k=v]` calls any of the 258
gateway methods in `routes.json`. Prefer the named commands above; use raw
only for methods with no wrapper.

## Gotchas

- `send` returns `{"sent": true, "reply": {...}}`, polling history until the
  reply lands (up to `--wait`). If `reply` is missing, the agent was slower
  than the wait: the message still landed, confirm with `history`. The reply
  is matched as a genuine answer, not background chatter.
- `send --wait 0` is fire-and-forget (no polling).
- `history` without `--thread` reads the main chat only.
- `invite` prints "none yet" until the code exists. The code is created
  when the Invite friends sheet is opened in the app, or by `invite
  --create` (POST /api/hatch/invite, only sent when there is no code yet).
- `usage` goes to muse.ai with cookies only (no VM connection), so it still works
  while the VM sleeps. The first run after a web app deploy may take a few
  seconds while it re-finds the action id in the JS bundle.
- Agent media shows up as `sandbox://workspace/...` paths, often in a
  separate `delta.presentation` event after the text reply. `download` looks
  up a message id or seq in the last `--limit` messages (main chat unless
  `--thread`); existing files are skipped unless `-f`.
- The gateway API is unversioned. Whole classes of calls failing at once means
  the protocol drifted: see `docs/PROTOCOL.md` for the re-derivation notes,
  don't guess at crypto or framing.
- Respect rate limits. Writes (send, react, execute, session ops) act as the
  user in their agent: announce them before running, never loop them.
- A stderr line `A new release of muse-cli is available` is an upgrade
  notice: the fork's main has a newer version. Upgrade with `muse-cli update`
  (reinstalls from the fork; a source checkout prints the `git pull` to run
  instead). The check runs at most once a day, only in a terminal, and never changes the install
  by itself. `MUSE_NO_UPDATE_CHECK=1` silences it.
