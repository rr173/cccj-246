"""Independent reference oracle.

This is a deliberately separate, from-scratch reimplementation of the
resolution rules.  It imports nothing from the package engine and works only
on the plain-dict state produced by ``TermStore.export_state()``.  Tests use
it to check every cached query after every batch.
"""

WITHDRAWN = "withdrawn"
REVIEWED = "reviewed"
CLEAR = "clear"
TEXT = "text"

OUT_SUPPLIED = "supplied"
OUT_CLEAR = "explicit_clear"
OUT_CONFLICT = "conflict"
OUT_BLOCKED = "blocked"
OUT_NONE = "no_available"

GENERIC_FACILITY = "*"


def _records_at(state, facility_names, coord_key):
    found = []
    for rec in state["entries"]:
        if rec["coord"] == coord_key and rec["facility"] in facility_names:
            found.append(rec)
    return found


def _passes_policy(rec, policy):
    if rec["body_state"] == WITHDRAWN:
        return False, "withdrawn"
    if rec["is_public"] and policy["require_public_reviewed"]:
        if rec["review"] != REVIEWED:
            return False, "public_not_reviewed"
    return True, "eligible"


def oracle_resolve(state, facility, target_key):
    policy = state["policies"][facility]
    blocked = set(policy["blocked"])
    chain = state["candidates"][target_key]

    # Pre-build the whole fixed candidate range, including coordinates the
    # resolution never reaches.
    trace = [
        {"coord": coord_key, "priority": level, "sources": []}
        for level, coord_key in enumerate(chain)
    ]
    ever_blocked = False
    cut = None

    for level, coord_key in enumerate(chain):
        node = trace[level]

        if coord_key in blocked:
            node["status"] = "blocked_by_policy"
            ever_blocked = True
            continue

        facilities = [facility]
        if policy["allow_generic"]:
            facilities.append(GENERIC_FACILITY)
        facilities = set(facilities)

        usable = []
        for rec in sorted(_records_at(state, facilities, coord_key),
                          key=lambda r: (r["facility"], r["source_id"])):
            allowed, why = _passes_policy(rec, policy)
            node["sources"].append(
                {
                    "facility": rec["facility"],
                    "source_id": rec["source_id"],
                    "body_state": rec["body_state"],
                    "text": rec["text"],
                    "is_public": rec["is_public"],
                    "review": rec["review"],
                    "eligible": allowed,
                    "reason": why,
                }
            )
            if allowed:
                usable.append(rec)

        if not usable:
            node["status"] = "no_eligible_record"
            continue

        good = [r for r in usable if r["body_state"] == TEXT]
        blank = [r for r in usable if r["body_state"] == CLEAR]

        if blank and not good:
            node["status"] = "explicit_clear"
            cut = level
            return _finish(
                OUT_CLEAR, "", coord_key, blank, trace, cut, ever_blocked,
                facility, target_key,
            )

        word_set = sorted({r["text"] for r in good})
        if len(word_set) > 1:
            node["status"] = "conflict"
            cut = level
            payload = _finish(
                OUT_CONFLICT, None, coord_key, good, trace, cut,
                ever_blocked, facility, target_key,
            )
            payload["conflict_texts"] = word_set
            return payload

        if word_set:
            node["status"] = "supplied"
            cut = level
            winners = [r for r in good if r["text"] == word_set[0]]
            return _finish(
                OUT_SUPPLIED, word_set[0], coord_key, winners, trace, cut,
                ever_blocked, facility, target_key,
            )

        node["status"] = "no_eligible_record"

    outcome = OUT_BLOCKED if ever_blocked else OUT_NONE
    return _finish(
        outcome, None, None, [], trace, cut, ever_blocked,
        facility, target_key,
    )


def _finish(outcome, text, win_coord, winners, trace, cut, ever_blocked,
            facility, target_key):
    if cut is not None:
        for later in trace[cut + 1:]:
            later["status"] = "not_reached"
            later["sources"] = []
    evidence = sorted(
        (
            {
                "facility": r["facility"],
                "coord": r["coord"],
                "source_id": r["source_id"],
                "text": r["text"],
                "is_public": r["is_public"],
                "review": r["review"],
            }
            for r in winners
        ),
        key=lambda e: (e["facility"], e["source_id"]),
    )
    return {
        "facility": facility,
        "target": target_key,
        "outcome": outcome,
        "text": text,
        "winning_coord": win_coord,
        "evidence": evidence,
        "conflict_texts": [],
        "saw_blocked_coordinate": ever_blocked,
        "trace": trace,
    }
