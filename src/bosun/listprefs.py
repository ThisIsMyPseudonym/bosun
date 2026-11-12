"""How you left the lists: grouping, sort order and which sections are folded.

Kept in the `settings` table, not in the browser: the Mac window and a browser tab are different profiles, and
both should open the page the way you left it. Nothing here changes what Bosun does; it is layout.
"""

from __future__ import annotations

import json

SETTING = "list_prefs"
PAGES = ("pc",)  # the machine page; project pages keep their lanes
GROUPS = ("route", "kind", "publisher", "none")
SORTS = ("risk", "name", "newest", "jump")
DEFAULT = {"group": "route", "sort": "risk"}
MAX_FOLDS = 300
MAX_KEY = 300


def read(store) -> dict:
    """{"pc": {"group", "sort"}, "folds": {section key: open?}}, whatever is stored: unknown values fall back."""
    try:
        raw = json.loads(store.setting(SETTING) or "{}")
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for page in PAGES:
        got = raw.get(page) if isinstance(raw.get(page), dict) else {}
        out[page] = {"group": got.get("group") if got.get("group") in GROUPS else DEFAULT["group"],
                     "sort": got.get("sort") if got.get("sort") in SORTS else DEFAULT["sort"]}
    folds = raw.get("folds") if isinstance(raw.get("folds"), dict) else {}
    out["folds"] = {str(k)[:MAX_KEY]: bool(v) for k, v in list(folds.items())[-MAX_FOLDS:]}
    return out


def update(store, page: str, group: str | None = None, sort: str | None = None,
           folds: dict[str, bool] | None = None) -> dict:
    current = read(store)
    if group is not None:
        current[page]["group"] = group
    if sort is not None:
        current[page]["sort"] = sort
    if folds:
        merged = {**current["folds"], **{str(k)[:MAX_KEY]: bool(v) for k, v in folds.items()}}
        current["folds"] = dict(list(merged.items())[-MAX_FOLDS:])  # the oldest fall off
    store.set_setting(SETTING, json.dumps(current))
    return current
