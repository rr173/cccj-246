"""Domain model for the facility terminology supply database.

Identity of a term record is exactly::

    (facility, language, context, source_id)

There is no history chain per record.  Every *correction* is recorded as a
whole in a finite batch ledger instead.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- entry.body states ----------------------------------------------------

#: An explicitly supplied, non-empty text.
TEXT = "text"
#: An explicit clear: a reviewer deliberately empties this coordinate.
CLEAR = "clear"
#: The record at this source was withdrawn; it no longer supplies anything.
WITHDRAWN = "withdrawn"

# --- review states --------------------------------------------------------
REVIEWED = "reviewed"
REVOKED = "revoked"

# --- resolution outcomes --------------------------------------------------
SUPPLIED = "supplied"          # non-empty text available
EXPLICIT_CLEAR = "explicit_clear"  # policy-conformant clear stopped fallback
CONFLICT = "conflict"          # same-level text conflict, lower levels untried
BLOCKED = "blocked"            # every candidate was disqualified by policy
NO_AVAILABLE = "no_available"  # no usable record existed anywhere

BODY_STATES = frozenset({TEXT, CLEAR, WITHDRAWN})
REVIEW_STATES = frozenset({REVIEWED, REVOKED})

#: Pseudo-facility whose records every concrete facility may use as generic
#: words (unless its policy forbids that fallback).
GENERIC = "*"


@dataclass(frozen=True)
class Coord:
    """A candidate coordinate: a (language, context) pair.

    Candidate coordinates are fixed and explicit per target (language,
    context).  A coordinate may point at another language/context; the
    resolution engine never follows such a pointer recursively.
    """

    lang: str
    ctx: str

    @staticmethod
    def parse(value: "str | Coord") -> "Coord":
        if isinstance(value, Coord):
            return value
        lang, ctx = value.split("/", 1)
        return Coord(lang=lang, ctx=ctx)

    def key(self) -> str:
        return f"{self.lang}/{self.ctx}"

    def __str__(self) -> str:
        return self.key()


@dataclass(frozen=True)
class EntryKey:
    facility: str
    coord: Coord
    source_id: str

    @property
    def lang(self) -> str:
        return self.coord.lang

    @property
    def ctx(self) -> str:
        return self.coord.ctx


@dataclass
class Entry:
    facility: str
    coord: Coord
    source_id: str
    body_state: str          # TEXT / CLEAR / WITHDRAWN
    text: str                # non-empty when body_state == TEXT, else ""
    is_public: bool
    review: str              # REVIEWED / REVOKED

    def key(self) -> EntryKey:
        return EntryKey(self.facility, self.coord, self.source_id)


@dataclass
class Policy:
    """Per-facility policy.  Policy decides eligibility only; it never
    changes which language/context an entry lives in."""

    facility: str
    allow_generic: bool
    require_public_reviewed: bool
    blocked: frozenset[Coord]


@dataclass
class EntryCorrection:
    """One correction line inside a batch."""

    kind: str                # set_body / clear / withdraw / set_review
    facility: str
    coord: Coord
    source_id: str
    text: str = ""
    is_public: bool = False
    review: str = REVIEWED


@dataclass
class PolicyCorrection:
    """The single per-facility policy that may accompany an entry batch."""

    facility: str
    allow_generic: bool
    require_public_reviewed: bool
    blocked: frozenset[Coord]


@dataclass
class Batch:
    entry_corrections: list[EntryCorrection]
    policy: "PolicyCorrection | None" = None


class BatchError(ValueError):
    """The batch is invalid.  The whole batch is rejected atomically; the
    previously committed version keeps serving untouched."""
