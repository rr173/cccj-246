"""Local review-draft merge primitives.

A translator saves a correction batch as an immutable *draft*.  The draft is
snapshotted against one committed store state (the *baseline*) and records

* the complete value of every record the batch actually changes, together
  with that record's baseline value (``None`` means "no such record");
* the facility policy only when the batch actually changes it;
* for every target whose wording can be affected, the supply result under
  the baseline and under the draft applied to that baseline.

An approver later merges the draft against the *current* live state.  The
draft's correction commands are never replayed on live state.  Instead each
complete record and the whole policy are merged three ways:

    live == baseline          -> take the draft value
    live == draft             -> keep the already-live value
    otherwise                 -> record conflict

This module contains the pure comparison rules; persistence and the atomic
swap live in :mod:`termdb.store`.
"""

from __future__ import annotations

from .model import CLEAR, TEXT, WITHDRAWN

#: Draft saved, pending approval.
DRAFT_SAVED = "saved"
#: Draft approved and merged; approving it again must not write again.
DRAFT_MERGED = "merged"

_BODY_FIELDS = ("body_state", "text", "is_public", "review")


# ------------------------------------------------------------------- errors


class DraftError(ValueError):
    """Base class for review-draft failures; the live store is untouched."""


class DraftNotFound(DraftError):
    """No draft carries the requested id."""


class DraftAlreadyMerged(DraftError):
    """This draft was already merged; approving again writes nothing."""


class MergeConflict(DraftError):
    """The draft cannot be merged without overwriting reviewed wording.

    ``record_conflicts`` names every complete record or policy whose three
    values disagree; ``supply_conflicts`` names every target whose four
    supply results cannot be reconciled.  Record conflicts are reported
    first and checked first: when any exists, the supply check is not run
    and ``supply_conflicts`` stays empty.
    """

    def __init__(self, draft_id, record_conflicts=(), supply_conflicts=()):
        self.draft_id = draft_id
        self.record_conflicts = list(record_conflicts)
        self.supply_conflicts = list(supply_conflicts)
        parts = []
        if self.record_conflicts:
            parts.append(
                f"{len(self.record_conflicts)} record/policy conflict(s)"
            )
        if self.supply_conflicts:
            parts.append(
                f"{len(self.supply_conflicts)} supply conflict(s)"
            )
        detail = "; ".join(parts) or "merge conflict"
        super().__init__(f"draft {draft_id} cannot be merged: {detail}")


# ------------------------------------------------------- value serialisation


def entry_value(entry) -> "dict | None":
    """Complete value of one record, or ``None`` when it does not exist.

    ``withdrawn`` and ``clear`` are kept distinct from each other and from a
    missing record; an absent record is the Python ``None`` sentinel.
    """

    if entry is None:
        return None
    if entry.body_state not in {TEXT, CLEAR, WITHDRAWN}:
        raise DraftError(f"invalid body state: {entry.body_state}")
    return {
        "body_state": entry.body_state,
        "text": entry.text,
        "is_public": bool(entry.is_public),
        "review": entry.review,
    }


def policy_value(policy) -> dict:
    """Whole-facility policy as an order-independent comparable value."""

    return {
        "allow_generic": bool(policy.allow_generic),
        "require_public_reviewed": bool(policy.require_public_reviewed),
        "blocked": sorted(coord.key() for coord in policy.blocked),
    }


def supply_signature(result: dict) -> dict:
    """The wording a target supplies, ignoring provenance evidence.

    Outcome kind, display text and (for a same-level conflict) the retained
    conflict text set are compared.  Source ids, review flags and evidence
    are deliberately excluded: a provenance-only change is not a supply
    conflict.
    """

    return {
        "outcome": result["outcome"],
        "text": result.get("text"),
        "conflict_texts": sorted(result.get("conflict_texts") or []),
    }


# ------------------------------------------------------------ merge rules


def merge_value(base, live, draft):
    """Three-way merge of one complete value.

    Returns ``(merged_value, conflict)``.  A side that did not change its
    value never overrides the other side, so a draft that merely restates the
    baseline lets a live correction win.  ``None`` means "both sides adopted
    the same non-absent value" only when base/live/draft all agree; otherwise
    three pairwise-different values are a record conflict.

    A record that exists in neither baseline nor live is ``None``; withdrawn
    and explicit-clear values are ordinary non-``None`` values and never
    collapse into absence.
    """

    if live == base:
        return draft, False
    if draft == base:
        return live, False
    if live == draft:
        return draft, False
    return None, True


def expected_supply(base: dict, draft: dict, live: dict):
    """The supply result the merge must preserve, or ``None`` on divergence.

    * Only the draft changed the result -> the merge must keep the draft's.
    * Only live changed the result -> the merge must keep live's.
    * Both sides changed to the same result -> the merge must keep it.
    * Both sides changed differently -> no mergeable expectation.

    The caller also rejects whenever the merged result differs from the
    returned expectation, so "neither side changed" does not imply "any
    result is fine".
    """

    draft_moved = draft != base
    live_moved = live != base
    if not draft_moved:
        return live if live_moved else base
    if not live_moved:
        return draft
    if draft == live:
        return draft
    return None
