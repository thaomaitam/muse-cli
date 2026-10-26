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
            "expires_at_ms": a.get("request_expires_at_ms"),
            # The choices Muse offers in the app, e.g. allow_once ("Allow
            # once"), allow_always ("Always allow this site" = forever), deny.
            "options": [o.get("kind") for o in a.get("decision_options") or []
                        if isinstance(o, dict) and o.get("kind")] or None}


FOREVER = "allow_always"  # Muse's "forever" / "Always allow" choice


def offers_forever(described):
    """True when the approval can be decided with Muse's forever choice.
    Approvals that list no options (older servers) are assumed to accept it."""
    opts = described.get("options")
    return not opts or FOREVER in opts


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
    approver: optional callable(list of describe_approval dicts) that decides
    new pending approvals (auto-approve) and returns the ids it handled.
    Handled approvals do not stop the wait; anything it could not handle
    still ends it with ("approval", ...). Each id is offered once.
    """
    def __init__(self, thread=None, require_running=False, approver=None):
        self.thread = thread
        self.require_running = require_running
        self.approver = approver
        self.handled = set()    # approval ids the approver already decided
        self.offered = set()    # approval ids already passed to the approver
        self.seen_running = False
        self.result = None      # ("done"|"failed"|"approval", detail)
        self.status = None
        # A terminal status ignored because no `running` was seen yet. It is
        # stale when the turn has not started, but it is also what a turn
        # that finished before we subscribed looks like (fast replies): the
        # caller settles that with chat.history (see settle()).
        self.ignored_terminal = None

    def feed(self, ev):
        if isinstance(ev, (bytes, str)):
            try:
                ev = json.loads(ev)
            except json.JSONDecodeError:
                return None
        pend = pending_approvals(ev)
        if pend:
            described = [describe_approval(a) for a in pend]
            if self.approver:
                new = [d for d in described if d["approval_id"] not in self.offered]
                if new:
                    self.offered.update(d["approval_id"] for d in new)
                    self.handled.update(self.approver(new) or ())
                left = [d for d in described if d["approval_id"] not in self.handled]
                if not left:
                    return None  # all auto-approved; keep waiting for the turn
                described = left
            self.result = ("approval", described)
            return self.result
        s = task_status(ev, self.thread)
        if s:
            self.status = s
            if s in DONE or s in FAILED:
                if self.require_running and not self.seen_running:
                    self.ignored_terminal = s
                    return None
                self.result = ("done" if s in DONE else "failed", s)
                return self.result
            self.seen_running = True
            self.ignored_terminal = None
        return None

    def settle(self, replies):
        """Accept the ignored terminal status when chat.history already holds
        a reply to our message: the turn ran and ended before the
        subscription started. Returns the result or None."""
        s = self.ignored_terminal
        self.ignored_terminal = None
        if self.result is None and s and replies:
            self.result = ("done" if s in DONE else "failed", s)
        return self.result if s and replies else None


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


_WAIT_PHRASES = [
    # Vietnamese
    "chờ boss", "chờ bạn", "tôi chờ", "mình chờ", "đợi boss", "đợi bạn", "boss muốn",
    "bạn muốn", "boss có muốn", "bạn có muốn", "trước khi tiếp tục", "trước khi làm tiếp",
    "boss xác nhận", "bạn xác nhận", "cần boss", "cần bạn", "boss quyết", "bạn quyết",
    "hay không?", "được không?", "nhé?",
    # English
    "should i", "do you want", "would you like", "waiting for your", "let me know if",
    "let me know whether", "please confirm", "can you confirm", "before i continue",
    "before i proceed", "shall i", "which do you prefer", "your call",
]
_SAFETY_RE = re.compile(r"safety check|kiểm tra an toàn|cơ chế an toàn|tripped .{0,30}safety|"
                        r"refus(e|ed) to (run|do|continue)|từ chối (chạy|làm)", re.I)
_GATE_RE = re.compile(r"(gate|policy)[ _-]?questions?\s*[:=]\s*(?!none\b)\S", re.I)


def needs_input_reason(text):
    """Why the final reply is waiting on a human, or None. Conservative:
    a question at the end, a known waiting phrase, a non-empty gate/policy
    question field, or a stop because of a safety check."""
    t = clean_text(text or "")
    if not t:
        return None
    low = t.lower()
    if _GATE_RE.search(t):
        return "gate/policy question"
    if _SAFETY_RE.search(t):
        return "stopped by a safety check"
    for ph in _WAIT_PHRASES:
        if ph in low:
            return f"waiting phrase: {ph!r}"
    tail = t.rstrip(" \n*_`)\"'").splitlines()[-1] if t.strip() else ""
    if tail.rstrip().endswith("?") or tail.rstrip().endswith("？"):
        return "ends with a question"
    return None


def question_of(text):
    """The sentence(s) that carry the question, for printing."""
    t = clean_text(text or "")
    qs = [s.strip() for s in re.split(r"(?<=[.!?。])\s+", t) if "?" in s]
    return " ".join(qs[-2:]) if qs else t.splitlines()[-1] if t else ""


def outcome_of(kind, replies=()):
    """done | needs_input | needs_approval | failed | timeout."""
    if kind == "approval":
        return "needs_approval"
    if kind == "failed":
        return "failed"
    if kind == "done":
        last = replies[-1] if replies else None
        if last and (last.get("widgets") or needs_input_reason(last.get("text"))):
            return "needs_input"
        return "done"
    return "timeout"


def exit_code(kind, replies=()):
    return {"done": EXIT_OK, "needs_input": EXIT_HUMAN, "needs_approval": EXIT_HUMAN,
            "failed": EXIT_FAILED}.get(outcome_of(kind, replies), EXIT_TIMEOUT)


def user_ack(events, baseline, text=None):
    """seq of our message.user event (> baseline), or None."""
    for e in sorted(events, key=lambda e: e.get("seq") or 0):
        if (e.get("seq") or 0) > baseline and e.get("event_name") == "message.user":
            p = _payload(e)
            t = p.get("display_text") or p.get("content") or e.get("display_text") or ""
            if text is None or text.strip() in t or t.strip() in text:
                return e.get("seq")
    return None
