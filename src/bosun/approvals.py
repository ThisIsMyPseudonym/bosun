"""One-call approvals: how you say yes to something the assistant proposed.

The assistant can read on its own. For anything else it asks, and the question is an Approval: an id the server
made up, the action's name, and a digest of the exact input. Three things can happen to it, each in one place:

- it is *requested* by the assistant's tool handler (assistant.py), which then waits;
- it is *decided* by the `assistant_decide` action, which only the signed-in browser can call (cookie and page
  token; `user_only` in the registry). Nothing the model writes reaches `decide`;
- it is *spent* by `registry.Actions.permit` when the approved call runs. Spending checks the action and the
  digest again and answers yes once. A second call, a different input, an unanswered or declined approval, or an
  id that was never issued all get nothing.

Approvals live in memory. A restart forgets them, which is the safe direction: a pending card can no longer be
approved, and the turn that was waiting for it is gone too.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

TTL_S = 300.0  # an approval nobody answers is a no
USE_WITHIN_S = 60.0  # an approved call starts at once; a yes left lying around is not kept
KEEP_S = 3600.0  # how long a finished approval stays, so a late click gets a clear answer


def digest(action: str, body: BaseModel) -> str:
    """What exactly was approved: the action and its validated input, in one canonical form."""
    canonical = json.dumps({"action": action, "input": body.model_dump(mode="json")}, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Approval:
    id: str
    action: str
    digest: str
    input: dict[str, Any]
    conversation_id: str
    created: float
    expires: float
    state: str = "pending"  # pending -> approved | declined | expired | stopped; approved -> used | void | expired
    reason: str = ""
    decided: float | None = None
    decided_by: str = ""
    _answered: threading.Event = field(default_factory=threading.Event, repr=False)


class Refused(Exception):
    """This decision can't be taken: the message says why, in words for the panel."""


class Approvals:
    def __init__(self, ttl_s: float = TTL_S, clock: Callable[[], float] = time.time):
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._all: dict[str, Approval] = {}

    def request(self, action: str, body: BaseModel, conversation_id: str = "") -> Approval:
        now = self._clock()
        a = Approval(secrets.token_urlsafe(18), action, digest(action, body), body.model_dump(mode="json", exclude_defaults=True),
                     conversation_id, now, now + self.ttl_s)
        with self._lock:
            for old in [k for k, v in self._all.items() if v.state != "pending" and now - v.created > KEEP_S]:
                del self._all[old]
            self._all[a.id] = a
        return a

    def decide(self, approval_id: str, approve: bool, *, by: str, reason: str = "") -> Approval:
        """The user's answer. `by` is the actor of the request that carried it: only "user" is accepted."""
        if by != "user":
            raise Refused("Only you can answer an approval, in the Ask Bosun panel")
        with self._lock:
            a = self._all.get(approval_id)
            if a is None:
                raise Refused("That approval is no longer open (Bosun restarted, or it was answered long ago)")
            self._expire(a)
            if a.state != "pending":
                raise Refused(f"That approval was already {a.state}")
            a.state, a.reason = ("approved" if approve else "declined"), reason.strip()
            a.decided, a.decided_by = self._clock(), by
        a._answered.set()
        return a

    def wait(self, approval_id: str) -> Approval:
        """Block until it is answered or its time is up. Called off the event loop."""
        a = self._all[approval_id]
        a._answered.wait(max(0.0, a.expires - self._clock()))
        with self._lock:
            self._expire(a)
        return a

    def consume(self, approval_id: str | None, action: str, body: BaseModel) -> Approval | None:
        """Spend an approval on one call. Yes only if the user approved exactly this action and input, just now,
        and it has not been spent. Anything presented for another call is voided, not kept for a second try."""
        with self._lock:
            a = self._all.get(approval_id or "")
            if a is None or a.state != "approved":
                return None
            if a.action != action or not secrets.compare_digest(a.digest, digest(action, body)):
                a.state = "void"
                return None
            if self._clock() - (a.decided or 0.0) > USE_WITHIN_S:
                a.state = "expired"
                return None
            a.state = "used"
            return a

    def stop(self, conversation_id: str) -> list[Approval]:
        """The user pressed Stop: whatever that conversation was asking is answered no."""
        with self._lock:
            stopped = [a for a in self._all.values() if a.conversation_id == conversation_id and a.state == "pending"]
            for a in stopped:
                a.state, a.reason, a.decided = "stopped", "the user stopped the assistant", self._clock()
        for a in stopped:
            a._answered.set()
        return stopped

    def get(self, approval_id: str) -> Approval | None:
        return self._all.get(approval_id)

    def _expire(self, a: Approval) -> None:
        if a.state == "pending" and self._clock() >= a.expires:
            a.state, a.reason, a.decided = "expired", f"nobody answered within {self.ttl_s / 60:g} minutes", self._clock()
            a._answered.set()
