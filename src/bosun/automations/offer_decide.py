"""Owner-only offer decision flow: re-check, atomically claim, spend one-call Approval via Actions.permit."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from pydantic import ValidationError

from ..approvals import Approvals
from ..registry import ActionContext, ActionError, Actions, Conflict, Forbidden, Invalid, NotFound
from ..store import Store
from . import api as aapi, digest, match as amatch, offers, store as astore
from .schema import AutomationDef, grant_digest, grant_digest_matches, grant_expired, has_unset_project


def _skip_summary(skipped: Any) -> str:
    if isinstance(skipped, list) and skipped:
        first = skipped[0]
        if isinstance(first, dict) and first.get("reason"):
            return str(first["reason"])[:300]
    return "skipped by safety checks"


def _claim_decline(store: Store, automation_id: int, firing_id: int, when: float) -> bool:
    marker = f"{offers.MERGED_REASON_PREFIX}{firing_id}"
    with store._tx(write=True) as db:
        cur = db.execute(
            "UPDATE automation_firings SET status='declined', decided_by='user', decided=?, finished=? "
            "WHERE id=? AND status='offered' AND (deadline IS NULL OR deadline > ?)",
            (when, when, firing_id, when),
        )
        if cur.rowcount != 1:
            return False
        db.execute(
            "UPDATE automation_firings SET status='declined', reason=?, decided_by='user', decided=?, finished=? "
            "WHERE automation_id=? AND status='skipped' AND reason=?",
            (f"offer #{firing_id} declined", when, when, automation_id, marker),
        )
        return True


def _claim_approve(store: Store, firing_id: int, when: float) -> bool:
    with store._tx(write=True) as db:
        cur = db.execute(
            "UPDATE automation_firings SET status='running', decided_by='user', decided=?, started_at=? "
            "WHERE id=? AND status='offered' AND (deadline IS NULL OR deadline > ?)",
            (when, when, firing_id, when),
        )
        return cur.rowcount == 1


def _mark_expired(store: Store, automation_id: int, firing_id: int, reason: str, when: float) -> None:
    offers.expire_offer_tx(store, automation_id, firing_id, reason, when)


def _check_open_offer(store: Store, firing_id: int, when: float) -> dict[str, Any]:
    firing = astore.get_firing(store, firing_id)
    if firing is None:
        raise NotFound("No offer with that id")
    status = str(firing.get("status") or "")
    if status == "expired":
        raise Conflict("This offer has expired")
    if status == "declined":
        raise Conflict("This offer was already declined")
    if status != "offered":
        raise Conflict(f"This offer is no longer open (status: {status})")
    deadline = firing.get("deadline")
    if deadline is not None and when >= float(deadline):
        _mark_expired(store, int(firing["automation_id"]), firing_id, "expired after 24 hours", when)
        raise Conflict("This offer expired after 24 hours")
    return firing


def _verify_rule_for_approve(
    store: Store, firing: dict[str, Any], when: float,
) -> tuple[int, dict[str, Any], AutomationDef]:
    if astore.is_paused(store):
        raise Forbidden("All automations are paused")
    aid, fid = int(firing["automation_id"]), int(firing["id"])
    row = astore.get(store, aid)
    if row is None:
        _mark_expired(store, aid, fid, "no longer applies: automation deleted", when)
        raise Conflict("This automation no longer exists")
    if not row["enabled"] or row["paused_reason"]:
        raise Forbidden("This automation is disabled or paused")
    try:
        defn = astore.parse_def(row)
    except (ValueError, ValidationError):
        raise Forbidden("This automation has an invalid definition") from None
    if has_unset_project(defn):
        raise Forbidden("This automation has no project selected")
    if not grant_digest_matches(row.get("grant_digest"), grant_digest(defn)):
        raise Forbidden("This automation's grant does not match its current definition")
    if grant_expired(defn.action, defn.mode, row.get("granted_at"), now=when):
        raise Forbidden("This automation's 90-day INSTALL grant expired; renew it in the dashboard")
    return aid, row, defn


def _finalize_siblings(
    store: Store, siblings: tuple[int, ...], status: str, trace_id: str, when: float, reason: str | None = None,
) -> None:
    for sfid in siblings:
        astore.set_firing(
            store, sfid, status=status, trace_id=trace_id, decided_by="user",
            decided=when, finished=when, reason=reason, now=when,
        )


def _execute_approved(
    store: Store, actions: Actions, approvals: Approvals, aid: int, defn: AutomationDef,
    firing_id: int, siblings: tuple[int, ...], body: Any, when: float,
) -> dict[str, Any]:
    asked = approvals.request(defn.action, body, conversation_id=f"automation:{aid}:{firing_id}")
    approvals.decide(asked.id, True, by="user")
    act_ctx = ActionContext(
        actor=f"automation:{aid}", surface="automation", approval=asked.id,
        firing_id=firing_id, firing_ids=(firing_id, *siblings), now=when,
    )
    astore.set_firing(store, firing_id, trace_id=act_ctx.trace_id, now=when)
    try:
        result = actions.run(defn.action, body, act_ctx)
    except Forbidden as exc:
        astore.set_firing(store, firing_id, status="refused", reason=exc.message, finished=when, now=when)
        raise
    except (ActionError, Exception) as exc:
        astore.set_firing(store, firing_id, status="failed", reason=str(exc), finished=when, now=when)
        raise
    finally:
        if asked.state == "approved":
            asked.state = "void"
    if isinstance(result, dict) and defn.action in ("upgrade", "pin_mcp") and not result.get("queued") and result.get("skipped"):
        why = _skip_summary(result["skipped"])
        astore.set_firing(store, firing_id, status="skipped", reason=why, finished=when, now=when)
        _finalize_siblings(store, siblings, "skipped", act_ctx.trace_id, when, reason=why)
        return {"firing_id": firing_id, "status": "skipped", "decided_by": "user",
                "trace_id": act_ctx.trace_id, "approval_id": asked.id, "result": result}
    astore.set_firing(store, firing_id, status="done", finished=when, now=when)
    _finalize_siblings(store, siblings, "done", act_ctx.trace_id, when)
    return {"firing_id": firing_id, "status": "done", "decided_by": "user",
            "trace_id": act_ctx.trace_id, "approval_id": asked.id, "result": result}


def decide_offer(
    store: Store, actions: Actions, approvals: Approvals, firing_id: int, approve: bool,
    *, actor: str = "user", now: float | None = None,
) -> dict[str, Any]:
    """Owner-only decision on one open offer: Decline records it; Approve spends one Approval through permit."""
    if actor != "user":
        raise Forbidden("decide_offer can only be done by you, in the Bosun dashboard")
    with aapi._writable(store):
        when = time.time() if now is None else now
        firing = _check_open_offer(store, firing_id, when)
        if not approve:
            if not _claim_decline(store, int(firing["automation_id"]), firing_id, when):
                raise Conflict("This offer was already decided or expired")
            return {"firing_id": firing_id, "status": "declined", "decided_by": "user"}
        aid, row, defn = _verify_rule_for_approve(store, firing, when)
        why = offers.recheck_offer(store, row, defn, firing, when)
        if why is not None:
            reason = why if why.startswith("expired") else f"no longer applies: {why}"
            _mark_expired(store, aid, firing_id, reason, when)
            raise Conflict(f"This offer no longer applies: {why}")
        raw_input = offers._parse_json(firing.get("input"))
        try:
            body = actions.validate(defn.action, raw_input)
        except ValidationError as exc:
            astore.set_firing(store, firing_id, status="refused", reason="invalid offer input", finished=when, now=when)
            raise Invalid(f"Offer input is invalid: {exc}") from None
        siblings = offers.merged_siblings(store, aid, firing_id)
        if not amatch.input_in_scope(
            store, defn, body, firing_id=firing_id, firing_ids=(firing_id, *siblings), automation_id=aid,
        ):
            astore.set_firing(store, firing_id, status="refused", reason="input outside scope", finished=when, now=when)
            raise Forbidden("Offer input is outside this automation's granted scope")
        if not _claim_approve(store, firing_id, when):
            raise Conflict("This offer was already decided or expired")
        return _execute_approved(store, actions, approvals, aid, defn, firing_id, siblings, body, when)


def _safe_offer_input(input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Return only known offer input fields, cleaned and length-capped for UI serialization."""
    out: dict[str, Any] = {}
    if isinstance(input_data.get("app"), str):
        out["app"] = digest._clean_name(input_data["app"], limit=80)
    if isinstance(input_data.get("key"), str):
        out["key"] = offers._clean_key(input_data["key"])
    if isinstance(input_data.get("keys"), list):
        out["keys"] = [offers._clean_key(k) for k in input_data["keys"][: offers.MAX_OFFER_ITEMS] if isinstance(k, str) and k]
    if isinstance(input_data.get("project"), str):
        out["project"] = offers._clean_key(input_data["project"])
    if "only" in input_data:
        only = input_data["only"]
        out["only"] = [digest._clean_name(x, limit=40) for x in only[:10] if isinstance(x, str) and x] if isinstance(only, list) else None
    if isinstance(input_data.get("auto_upgrade"), bool):
        out["auto_upgrade"] = input_data["auto_upgrade"]
    return out


def serialize_offer(
    store: Store, firing: dict[str, Any], *, automations: Mapping[int, dict[str, Any]] | None = None,
    snapshot_items: Mapping[str, dict[str, Any]] | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Serialize one offer row with code-built plain text and capped known input fields."""
    when = time.time() if now is None else now
    aid = int(firing["automation_id"])
    rule = automations.get(aid) if automations is not None else astore.get(store, aid)
    action = str(rule["action"]) if rule is not None else ""
    input_data = offers._parse_json(firing.get("input"))
    items = offers.offer_items(store, action, input_data, snapshot_items=snapshot_items)
    deadline = firing.get("deadline")
    is_expired = firing.get("status") == "expired" or (deadline is not None and when >= float(deadline))
    label = (offers.action_summary(action, input_data, items) if action
             else digest._clean_name(str(firing.get("reason") or "Run automation"), limit=120))
    return {
        "id": int(firing["id"]), "automation_id": aid,
        "automation_name": digest._clean_name(str(firing.get("automation_name") or f"Automation #{aid}"), limit=80),
        "action": action, "action_label": label, "items": list(items), "input": _safe_offer_input(input_data),
        "why": offers.offer_why(firing), "status": "expired" if is_expired else str(firing.get("status") or "offered"),
        "expired": is_expired, "created": firing.get("created"), "deadline": deadline,
    }


def waiting_offers(store: Store, *, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return serialized open offers (including any just-expired rows not yet swept by a tick)."""
    when = time.time() if now is None else now
    rows = astore.list_firings(store, status="offered", limit=limit)
    if not rows:
        return []
    autos = {int(a["id"]): a for a in astore.list_all(store)}
    snap_items = amatch._snap_items_by_key(store)
    return [serialize_offer(store, r, automations=autos, snapshot_items=snap_items, now=when) for r in rows]
