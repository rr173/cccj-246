"""From-scratch resolution engine.

Given the *current* state (candidate coordinate lists, facility policies,
current entries) the engine decides, without consulting any cached result:

1. policy eligibility, then
2. the highest-priority eligible source on the fixed candidate coordinates.

Same-level text conflicts are retained and lower sources are never tried.
The engine performs no recursion: a candidate coordinate pointing at another
language/context is used as-is, never expanded.
"""

from __future__ import annotations

from .model import (
    CLEAR,
    CONFLICT,
    EXPLICIT_CLEAR,
    GENERIC,
    NO_AVAILABLE,
    REVIEWED,
    SUPPLIED,
    TEXT,
    WITHDRAWN,
    BLOCKED,
    Coord,
    Entry,
    Policy,
)


def eligibility(entry: Entry, policy: Policy) -> tuple[bool, str]:
    """Policy gate.  Returns (eligible, reason)."""

    if entry.body_state == WITHDRAWN:
        return False, "withdrawn"
    if entry.is_public and policy.require_public_reviewed:
        if entry.review != REVIEWED:
            return False, "public_not_reviewed"
    return True, "eligible"


def resolve(
    facility: str,
    target: Coord,
    candidate_lists: dict[Coord, list[Coord]],
    policies: dict[str, Policy],
    entries: dict[tuple[str, Coord, str], Entry],
) -> dict:
    """Resolve one (facility, target language/context) query from scratch.

    The returned dict is JSON-ready and fully auditable: it carries the
    winning text, the outcome kind, the whole fixed candidate range (with
    every coordinate and every source considered) and the reason each one
    did or did not qualify.
    """

    policy = policies[facility]
    candidates = candidate_lists[target]

    # Pre-materialize the full candidate range so callers always see every
    # fixed coordinate, including ones resolution never reached.
    trace: list[dict] = [
        {"coord": coord.key(), "priority": priority, "sources": []}
        for priority, coord in enumerate(candidates)
    ]
    blocked_any = False
    stopped_at = None

    # Entries are kept in a dict; group them once per coordinate to make the
    # visit order explicit and independent of insertion order.
    by_coord: dict[Coord, list[Entry]] = {}
    for entry in entries.values():
        by_coord.setdefault(entry.coord, []).append(entry)
    for bucket in by_coord.values():
        bucket.sort(key=lambda e: (e.facility, e.source_id))

    for priority, coord in enumerate(candidates):
        node = trace[priority]

        if coord in policy.blocked:
            node["status"] = "blocked_by_policy"
            blocked_any = True
            continue

        # Generic words: the '*' pseudo-facility is consulted only when the
        # facility policy allows that fallback.
        source_facilities = [facility]
        if policy.allow_generic:
            source_facilities.append(GENERIC)
        allowed_facilities = set(source_facilities)

        eligible_records: list[Entry] = []
        for entry in by_coord.get(coord, []):
            if entry.facility not in allowed_facilities:
                continue
            ok, reason = eligibility(entry, policy)
            node["sources"].append(
                {
                    "facility": entry.facility,
                    "source_id": entry.source_id,
                    "body_state": entry.body_state,
                    "text": entry.text,
                    "is_public": entry.is_public,
                    "review": entry.review,
                    "eligible": ok,
                    "reason": reason,
                }
            )
            if ok:
                eligible_records.append(entry)

        if not eligible_records:
            node["status"] = "no_eligible_record"
            continue

        clears = [e for e in eligible_records if e.body_state == CLEAR]
        texts = [e for e in eligible_records if e.body_state == TEXT]

        if clears and not texts:
            # A policy-conformant explicit clear wins this coordinate and
            # stops all fallback — including to lower-priority coordinates.
            node["status"] = "explicit_clear"
            stopped_at = priority
            return _result(
                EXPLICIT_CLEAR, "", coord, clears, trace, stopped_at,
                blocked_any, facility, target,
            )

        distinct_texts = sorted({e.text for e in texts})
        if len(distinct_texts) > 1:
            # Same-level conflict is retained. Lower sources never tried.
            node["status"] = "conflict"
            stopped_at = priority
            result = _result(
                CONFLICT, None, coord, texts, trace, stopped_at,
                blocked_any, facility, target,
            )
            result["conflict_texts"] = distinct_texts
            return result

        if distinct_texts:
            node["status"] = "supplied"
            stopped_at = priority
            winners = [e for e in texts if e.text == distinct_texts[0]]
            return _result(
                SUPPLIED, distinct_texts[0], coord, winners, trace,
                stopped_at, blocked_any, facility, target,
            )

        node["status"] = "no_eligible_record"

    outcome = BLOCKED if blocked_any else NO_AVAILABLE
    return _result(
        outcome, None, None, [], trace, stopped_at, blocked_any,
        facility, target,
    )


def _result(outcome, text, win_coord, winners, trace, stopped_at,
            blocked_any, facility, target):
    if stopped_at is not None:
        for node in trace[stopped_at + 1:]:
            # Coordinates after the stopping coordinate are never evaluated,
            # so they carry no source inspection at all.
            node["status"] = "not_reached"
            node["sources"] = []
    return {
        "facility": facility,
        "target": target.key(),
        "outcome": outcome,
        "text": text,
        "winning_coord": win_coord.key() if win_coord else None,
        "evidence": sorted(
            (
                {
                    "facility": e.facility,
                    "coord": e.coord.key(),
                    "source_id": e.source_id,
                    "text": e.text,
                    "is_public": e.is_public,
                    "review": e.review,
                }
                for e in winners
            ),
            key=lambda ev: (ev["facility"], ev["source_id"]),
        ),
        "conflict_texts": [],
        "saw_blocked_coordinate": blocked_any,
        "trace": trace,
    }
