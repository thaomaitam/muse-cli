"""Block until the agent's turn is really over.

Live `chat.subscribe` events that matter (observed):
  task.status        payload {status: running|completed|..., agent_id,
                     root_agent_id, task_id, session_id, is_thread,
                     active_subagent_count}
  approvals.snapshot payload {pending_approvals: [...], recent_approvals: [...]}
  message.assistant / delta.*  partial or full reply text (ignored here;
                     the final text is re-read from chat.history)
On subscribe the server first replays the current task.status and
approvals.snapshot, so a stale "completed" can arrive before the turn we
care about starts; `require_running` handles that.
"""
import json
import re

DONE = {"completed", "complete", "idle", "done", "finished", "succeeded", "success"}
FAILED = {"failed", "failure", "error", "errored", "cancelled", "canceled", "aborted", "timed_out"}

EXIT_OK, EXIT_FAILED, EXIT_HUMAN, EXIT_TIMEOUT = 0, 2, 4, 124

WIDGET_RE = re.compile(r"\[\[hatch_widget:[^\]]+\]\]")


def _payload(ev):
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def event_name(ev):
    return ev.get("event") or ev.get("event_name") or ev.get("type")


def in_scope(ev, thread=None):
    p = _payload(ev)
    if thread:
        return p.get("session_id") in (None, thread)
    return not p.get("is_thread", False)


def task_status(ev, thread=None):
    """Lower-cased status of the root agent's task, or None."""
    if event_name(ev) != "task.status" or not in_scope(ev, thread):
        return None
    p = _payload(ev)
    root = p.get("root_agent_id")
    if root and p.get("agent_id") and p["agent_id"] != root:
        return None  # a subagent finishing is not the turn finishing
    s = p.get("status")
    return str(s).lower() if s else None


def pending_approvals(ev):
    if not str(event_name(ev) or "").startswith("approvals"):
        return None
    p = _payload(ev)
    pend = p.get("pending_approvals")
    if pend is None:
        pend = p.get("pending")
    return pend if isinstance(pend, list) else None


def describe_approval(a):
    d = a.get("display") or {}
    q = d.get("permission_question") or {}
    rows = {r.get("label"): r.get("value") for r in d.get("detail_rows") or [] if isinstance(r, dict)}
    return {"approval_id": a.get("approval_id"),
            "title": d.get("summary_title") or d.get("display_name"),
            "question": (q.get("text") or "").replace("{assistant}", "Muse") or None,
            "kind": q.get("kind") or (a.get("payload") or {}).get("type"),
            "scope": q.get("scope") or rows.get("Host"),
            "why": d.get("short_explanation") or d.get("reason") or a.get("reason"),
            "expires_at_ms": a.get("request_expires_at_ms")}


def open_widgets(ev):
    """Unanswered hatch widgets (questions for the human) in a history event."""
    p = _payload(ev)
    out = []
    for w in p.get("widgets") or []:
        st = w.get("state") or {}
        answered = any(st.get(k) not in (None, "", [], {}) for k in
                       ("value", "selected", "selection", "answer", "submitted", "response"))
        if not answered:
            out.append({"widget_id": w.get("widget_id"), "kind": w.get("kind"),
                        "text": w.get("display_text") or w.get("fallback_text"),
                        "options": (w.get("data") or {}).get("options")})
    return out


def clean_text(text):
    return WIDGET_RE.sub("", text or "").strip()


class Waiter:
    """Feed live events; `decide()` says whether to stop and why.

    require_running: ignore terminal statuses until a `running` one is seen
    (used right after sending, when the replayed status is stale).
    """
    def __init__(self, thread=None, require_running=False):
        self.thread = thread
        self.require_running = require_running
        self.seen_running = False
        self.result = None      # ("done"|"failed"|"approval", detail)
        self.status = None

    def feed(self, ev):
        if isinstance(ev, (bytes, str)):
            try:
                ev = json.loads(ev)
            except json.JSONDecodeError:
                return None
        pend = pending_approvals(ev)
        if pend:
            self.result = ("approval", [describe_approval(a) for a in pend])
            return self.result
        s = task_status(ev, self.thread)
        if s:
            self.status = s
            if s in DONE or s in FAILED:
                if self.require_running and not self.seen_running:
                    return None
                self.result = ("done" if s in DONE else "failed", s)
                return self.result
            self.seen_running = True
        return None


def replies_after(events, after_seq):
    """Complete assistant messages with seq > after_seq, oldest first."""
    out = []
    for e in sorted(events, key=lambda e: e.get("seq") or 0):
        if (e.get("seq") or 0) <= after_seq or e.get("event_name") != "message.assistant":
            continue
        p = _payload(e)
        text = p.get("display_text") or p.get("content") or e.get("display_text") or ""
        if not text:
            continue
        r = {"seq": e.get("seq"), "message_id": p.get("message_id") or e.get("message_id"),
             "text": clean_text(text)}
        w = open_widgets(e)
        if w:
            r["widgets"] = w
        out.append(r)
    return out


def exit_code(kind, replies=()):
    if kind == "approval":
        return EXIT_HUMAN
    if kind == "failed":
        return EXIT_FAILED
    if kind == "done":
        if replies and replies[-1].get("widgets"):
            return EXIT_HUMAN
        return EXIT_OK
    return EXIT_TIMEOUT


def user_ack(events, baseline, text=None):
    """seq of our message.user event (> baseline), or None."""
    for e in sorted(events, key=lambda e: e.get("seq") or 0):
        if (e.get("seq") or 0) > baseline and e.get("event_name") == "message.user":
            p = _payload(e)
            t = p.get("display_text") or p.get("content") or e.get("display_text") or ""
            if text is None or text.strip() in t or t.strip() in text:
                return e.get("seq")
    return None
