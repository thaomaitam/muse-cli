# Protocol notes

How `muse` talks to the muse.ai personal gateway. Derived from the web app's
own client bundle and verified live. Internal APIs are unversioned and can
change without notice; if calls start failing, re-derive from a fresh bundle
(HAR capture + the `derive-client` workflow).

## Layers

```
muse CLI
  │  HTTPS (browser TLS fingerprint via curl-impersonated requests)
  ▼
muse.ai            cookies → POST /api/auth/check → access_token
                   POST /api/hatch/token {vmAddress, vmName} → hatch token
                   GET /api/session → assigned VM id (auto-discovered)
  │  wss://hatch.metaaivm.com/v1/noise?vm_id=..&auth_token=..
  │  Noise_XX_25519_AESGCM_SHA256, empty payloads both ways
  ▼
personal VM gateway
  │  protobuf envelopes (descriptors in desc0.bin / desc1.bin)
  │  request/response + newline-JSON event subscriptions
  ▼
chat / feed / goals / ideas / sessions / ...
```

## Details

- **Gateway host:** the shared `hatch.metaaivm.com`, not the per-VM hostname.
  Plain TLS to the per-VM host is reset at the edge; a Chrome fingerprint
  (curl_cffi `impersonate="chrome"`) is required.
- **Handshake:** standard Noise XX. This account negotiates standard mode
  (empty msg3). An attested/RV mode with SNP attestation and HMAC proofs
  exists in the client for other configurations.
- **Framing:** `NoiseTransportFrame{chunk_id(i64), chunk_index, total_chunks,
  payload}` protobuf → Noise transport encrypt → one WS binary frame each.
- **Request:** `ServiceRequest{service, payload =
  ServiceFrame{stream_id, request =
  ApplicationRequest{verb, path, headers, body, end_body}}}`. Stream ids start
  at 1. Services: daemon 0, sentinel 1, vault 2, authd 3.
- **Response:** `ServiceResponse{payload = ServiceFrame{... response =
  ApplicationResponse{status, headers, body, end_body}}}` plus `body_chunk`
  frames until `end_body`.
- **Subscriptions** (`chat.subscribe`, `chat.stream`, ...): same envelope, the
  server streams `application/x-ndjson` records in body chunks.
- **Method table:** `routes.json` (258 methods extracted from the web bundle).

## Server quirks learned from live errors

- `chat.history`: GET query params only; `transcript_mode` is rejected, omit
  it; `session_id` scopes to a side chat; `limit` works. History events use
  `event_name` (`message.user` / `message.assistant`), carry the text in both
  top-level and `payload.display_text`, and are the source of truth.
- Genuine assistant replies have an **empty `reply_to_message_id`**; proactive
  pushes (Telegram drafts, background task updates) are self-referential
  there. That is how `send` tells a reply apart from background chatter.
- Live `chat.subscribe` events use **different shapes** (`delta.text_append`
  with `message_seq` + text chunks, `delta.message_done` with the full
  transcript) and only cover main-chat traffic: threaded replies never arrive
  as live events, just a `sessions.updated` snippet. `send` therefore polls
  history instead of watching the stream. The `chat.stream` response stream
  itself carries only the send echo, then ends.
- `/api/session` can return 200 `{"status":"unavailable",
  "vm_resolution_issue":{"kind":"retryable"}}` while the VM restarts. The
  client raises a clean error (not KeyError); retry, or wake a known VM id
  via `MUSE_VM_ID=<id> muse-cli wake`.
- Concurrent `_read_frame` calls from two threads split frames and corrupt
  the stateful Noise decrypt (fatal BAD_DECRYPT). `Gateway` serializes
  receives with a lock as a backstop, but callers must still keep exactly
  one frame consumer at a time (`send` makes sequential unary calls only).
- **Agent files** (`sandbox://workspace/<p>` in `payload.data.videos[]` /
  `images[]` / `fallback_text`): each entry carries `mime`, `byte_len` and
  `variants.original = https://<vm>.metaaivm.com/media/raw/workspace/<p>`.
  The bytes come from `GET /media/raw/workspace/<p>` on the VM. That route is
  not in `routes.json`; sent as a raw-path daemon request over the Noise
  tunnel it returns the file (body chunks). `Gateway.download_file()` wraps
  this. `artifacts.fetch_url` is unrelated (needs `project_dir`).
- **Plan usage** (Settings > Usage) is not a REST route or a gateway method.
  It is the Next.js server action `fetchSubscriptionAction`: `POST
  https://muse.ai/settings` with cookies, `Next-Action: <id>`, `Accept:
  text/x-component`, body `[{"includeAgreement":false}]`. The RSC reply is
  `0:{"a":"$@1",...}` then `1:{"success":true,"subscription":{tier, usage:
  {state, percentUsed, resetsAt (unix s), quotaStatus}, statusSubtitle,
  usageRowLabel, topupBalance, topupTotal, topupRowLabel,
  entryPointCtaLabel, ...}}`. The bonus token % is the web app's own
  `round(100*(total-clamp(balance))/total)`. `topupBalance`/`topupTotal` are NOT
  tokens (120M units showed as "3B tokens left"); the web app never converts
  them and prints `topupRowValueLabel` verbatim, so the CLI parses tokens
  left from that label. The action id is a build hash
  (`4029ac5c...` as of 2026-10-23); when it stops working the client looks it
  up again by scanning `/_next/static/chunks/*.js` for
  `createServerReference)("<id>",...,"fetchSubscriptionAction")`, then caches
  it as `subscription_action_id` in config.json.
- **Invite friends** is a plain REST route on muse.ai (cookies only):
  `GET /api/hatch/invite` returns `{invite_state, code, invite_link,
  max_uses, use_count, uses_remaining, share_text, reward{title, body},
  subtitle, has_redeemed_invite_code, redeem_invite_code_screen, ...}`. The
  earned stats come only as display text in `reward`. `POST` (no body) to the same URL
  creates the code and returns `{code, max_uses (30), use_count, invite_link
  (https://muse.ai/join), share_text}`; `invite --create` sends it only when
  GET shows `code: null`; codes match
  `^[A-Z0-9]{6,16}$`. Found via `useHatchInvitePageQuery` in the bundle.
- **Redeem a code:** `POST /api/hatch/invite-code/redeem` `{code,
  supportsRedemptionStatus: true}`. Success: `{success: true,
  redemptionStatus: "redeemed"}`. Failure (400): `{success: false, reason,
  detail}`, e.g. `window_expired` ("Your 48-hour window to redeem a code has
  passed."), `already_redeemed`, `invalid`.
- `session.start`: `{method: "/api/session/start", params: {origin: "fresh",
  lifecycle: "persistent", title?}}`.
- `session.rename`: flat `{session_id, title}`. pin/unpin/archive/unarchive/
  delete: `{method: "/api/session/<op>", session_id}`.
- `api.idea-cards.execute`: `{ideaCardId, mode: "full"}` plus path param.
- POSTs to muse.ai need browser `Sec-Fetch-*` headers or they return 403.
- `auth export` reads cookies from the user's own Google Chrome through
  agent-browser `--auto-connect`. Chrome 144+ shares that profile only after
  remote debugging is enabled at `chrome://inspect/#remote-debugging`. The
  export follows the active tab, so a muse.ai tab has to be focused first,
  and it must never overwrite a working jar without a `hatch_sess` in the
  new one. A freshly launched browser has no login; do not fall back to one.

## System Files (fs.*)

All POST with JSON bodies; paths relative to the agent home (System Files
root); absolute paths -> 400 "absolute paths are not allowed", `..` -> 400
"path traversal is not allowed". Every field listed is required (serde 422
otherwise). Responses are `{"ok":true,"result":{...}}` except `fs.list`.

| method | body | result |
|---|---|---|
| `fs.list` | `{path}` (`""` = root) | `{path, entries:[{name,type:file|directory,size?,modifiedAt}]}` (not wrapped) |
| `fs.stat` | `{path}` | `{path, kind:file|dir, size, modified_ms}` |
| `fs.read` | `{path, offset, len}` | `{path, offset, len, eof, data_base64}` |
| `fs.reads` | `{entries:[...]}` | batch read (unused) |
| `fs.search` | `{name:{contains}}` or an extensions filter | `{entries:[{name,path,size,modified_ms}], total, has_more}`; index lags new files |
| `fs.write` | `{path, overwrite, append, create_parent, data_base64 \| text}` | `{path, bytes_written, append}` |
| `fs.mkdir` | `{path, recursive}` | `{path, created}` |
| `fs.rename` | `{source, destination, overwrite, create_parent}` | `{source, destination}` |
| `fs.delete` | `{path, recursive}` | `{path}` |

`fs.read` of some home files (IDENTITY.md ...) returns a longer body than
`fs.list`'s size: the server prepends an "About this file" note. muse-cli
reads in 4 MiB windows until `eof` and writes in 4 MiB append chunks.

## Turn completion and approvals

Live `chat.subscribe` replays current state on connect, then streams:

- `task.status` payload `{status, agent_id, root_agent_id, task_id, session_id,
  is_thread, active_subagent_count}`; seen values `running`, `completed`.
  Only the root agent's status (agent_id == root_agent_id) ends a turn.
- `approvals.snapshot` payload `{emitted_at_ms, pending_approvals[], recent_approvals[]}`;
  each approval has `approval_id`, `display.{summary_title, short_explanation,
  permission_question{kind,scope,text}, detail_rows}`, `payload.network_payload`,
  `request_expires_at_ms`, and once decided `decision`.
- Widgets: assistant messages embed `[[hatch_widget:<id>]]` in text and carry
  `payload.widgets[{widget_id, kind:"option", data.options, display_text, state}]`.

Routes: `egress.approvals` GET /approvals (`pending`, `pending_approvals`,
`recent`); `egress.approval.decide` POST /approvals/{id}/decide `{decision}` with
decision in `allow_once|allow_session|allow_today|allow_always|deny|deny_always`
(server also lists `timed_out`, `auto_allow`).
