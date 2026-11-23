"""SQLite-backed store: batches, scoped recomputation and reopen support.

Design points
-------------
* Candidate coordinate lists are fixed at bootstrap and never edited.
* A correction batch is all-or-nothing.  A rejected batch is recorded in the
  ledger but touches neither current state nor any cached result: the prior
  version keeps serving.
* Only affected (facility, target) queries are recomputed:
    - entry correction  -> targets whose fixed candidate list visits the
      touched coordinate (never targets whose candidates never reach it);
    - policy correction -> every target governed by that facility policy.
* The full entry history is deliberately *not* kept: only the finite batch
  ledger plus the current state of each record.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .engine import resolve
from .review import (
    DRAFT_MERGED,
    DRAFT_SAVED,
    DraftAlreadyMerged,
    DraftError,
    DraftNotFound,
    MergeConflict,
    entry_value,
    expected_supply,
    merge_value,
    policy_value,
    supply_signature,
)
from .model import (
    BODY_STATES,
    Batch,
    BatchError,
    CLEAR,
    Coord,
    Entry,
    EntryCorrection,
    GENERIC,
    Policy,
    PolicyCorrection,
    REVIEW_STATES,
    TEXT,
    WITHDRAWN,
)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE candidate_coord (
    target_key TEXT NOT NULL,
    priority   INTEGER NOT NULL,
    coord_key  TEXT NOT NULL,
    PRIMARY KEY (target_key, priority)
);
CREATE TABLE facility (
    facility                  TEXT PRIMARY KEY,
    allow_generic             INTEGER NOT NULL,
    require_public_reviewed   INTEGER NOT NULL
);
CREATE TABLE blocked_coord (
    facility  TEXT NOT NULL,
    coord_key TEXT NOT NULL,
    PRIMARY KEY (facility, coord_key)
);
CREATE TABLE entry_record (
    facility   TEXT NOT NULL,
    lang       TEXT NOT NULL,
    ctx        TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    body_state TEXT NOT NULL,
    text       TEXT NOT NULL,
    is_public  INTEGER NOT NULL,
    review     TEXT NOT NULL,
    PRIMARY KEY (facility, lang, ctx, source_id)
);
CREATE TABLE batch_ledger (
    seq        INTEGER PRIMARY KEY,
    status     TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    detail     TEXT NOT NULL,
    error      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE result_cache (
    facility   TEXT NOT NULL,
    target_key TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (facility, target_key)
);
CREATE TABLE recompute_stat (
    facility   TEXT NOT NULL,
    target_key TEXT NOT NULL,
    recomputes INTEGER NOT NULL,
    PRIMARY KEY (facility, target_key)
);
"""

#: Review tables are also created by ``open`` so databases made before this
#: feature existed can still save and approve drafts.
REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_draft (
    draft_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    merged_at    TEXT,
    merged_seq   INTEGER,
    batch_detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_draft_entry (
    draft_id    INTEGER NOT NULL,
    facility    TEXT NOT NULL,
    lang        TEXT NOT NULL,
    ctx         TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    base_value  TEXT,
    draft_value TEXT NOT NULL,
    PRIMARY KEY (draft_id, facility, lang, ctx, source_id)
);
CREATE TABLE IF NOT EXISTS review_draft_policy (
    draft_id    INTEGER PRIMARY KEY,
    facility    TEXT NOT NULL,
    base_value  TEXT NOT NULL,
    draft_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_draft_target (
    draft_id     INTEGER NOT NULL,
    facility     TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    base_result  TEXT NOT NULL,
    draft_result TEXT NOT NULL,
    PRIMARY KEY (draft_id, facility, target_key)
);
"""


def _draft_signature(snapshot, facility, target, base_sig):
    """Baseline and draft supply signatures recorded for one target.

    Targets the draft's scope never touched have no recorded draft result;
    both sides then trivially equal the live baseline signature.
    """

    for item in snapshot["targets"]:
        if item["facility"] == facility and item["target"] == target.key():
            return item["base_result"], item["draft_result"]
    return base_sig, base_sig


class TermStore:
    def __init__(self, path: str, conn: sqlite3.Connection):
        self.path = path
        self._conn = conn
        self.candidates: dict[Coord, list[Coord]] = {}
        self.policies: dict[str, Policy] = {}
        self.entries: dict[tuple[str, Coord, str], Entry] = {}
        self.cache: dict[tuple[str, str], dict] = {}
        self.recomputes: dict[tuple[str, str], int] = {}
        self._reverse: dict[Coord, set[Coord]] = {}
        self._universe: set[Coord] = set()
        self._seq = 0
        # Token of the live state this instance last loaded/wrote; used to
        # notice a correction committed by another connection on the same
        # file before we write our own.
        self._token = None

    # ------------------------------------------------------------------ open

    @classmethod
    def bootstrap(
        cls,
        path: str,
        candidate_lists: dict,
        policies: dict[str, Policy],
        entries: list[Entry],
    ) -> "TermStore":
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        try:
            conn.executescript(SCHEMA)
            conn.executescript(REVIEW_SCHEMA)
            store = cls(path, conn)
            store._load_candidates(candidate_lists)
            for policy in policies.values():
                store._write_policy(policy)
            store.policies = dict(policies)
            for entry in entries:
                store._write_entry(entry)
                store.entries[store._ekey(entry)] = entry
            store._rebuild_indexes()
            conn.execute(
                "INSERT INTO meta VALUES ('created_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute(
                "INSERT INTO meta VALUES ('state_counter', '0')"
            )
            store._build_initial_cache()
            conn.execute(
                "INSERT INTO batch_ledger (seq, status, applied_at, detail) "
                "VALUES (0, 'bootstrap', ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps({"entry_count": len(entries)}),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        store._seq = 0
        return store

    @classmethod
    def open(cls, path: str) -> "TermStore":
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(REVIEW_SCHEMA)
        store = cls(path, conn)

        rows = conn.execute(
            "SELECT target_key, priority, coord_key FROM candidate_coord "
            "ORDER BY target_key, priority"
        ).fetchall()
        by_target: dict[str, list[tuple[int, Coord]]] = {}
        for target_key, priority, coord_key in rows:
            by_target.setdefault(target_key, []).append(
                (priority, Coord.parse(coord_key))
            )
        store.candidates = {
            Coord.parse(tk): [c for _, c in sorted(items)]
            for tk, items in by_target.items()
        }

        for facility, allow, req in conn.execute(
            "SELECT facility, allow_generic, require_public_reviewed FROM facility"
        ):
            blocked = frozenset(
                Coord.parse(k)
                for (k,) in conn.execute(
                    "SELECT coord_key FROM blocked_coord WHERE facility=?",
                    (facility,),
                )
            )
            store.policies[facility] = Policy(
                facility=facility,
                allow_generic=bool(allow),
                require_public_reviewed=bool(req),
                blocked=blocked,
            )

        for fac, lang, ctx, sid, state, text, pub, rev in conn.execute(
            "SELECT facility, lang, ctx, source_id, body_state, text, "
            "is_public, review FROM entry_record"
        ):
            entry = Entry(
                facility=fac,
                coord=Coord(lang, ctx),
                source_id=sid,
                body_state=state,
                text=text,
                is_public=bool(pub),
                review=rev,
            )
            store.entries[store._ekey(entry)] = entry

        for fac, tk, payload in conn.execute(
            "SELECT facility, target_key, payload FROM result_cache"
        ):
            store.cache[(fac, tk)] = json.loads(payload)
        for fac, tk, n in conn.execute(
            "SELECT facility, target_key, recomputes FROM recompute_stat"
        ):
            store.recomputes[(fac, tk)] = n
        store._seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM batch_ledger"
        ).fetchone()[0]
        if conn.execute(
            "SELECT 1 FROM meta WHERE key = 'state_counter'"
        ).fetchone() is None:
            conn.execute("INSERT INTO meta VALUES ('state_counter', '0')")
        store._rebuild_indexes()
        store._token = store._state_counter()
        return store

    def close(self) -> None:
        self._conn.close()

    @property
    def state_seq(self) -> int:
        """Monotonic commit token for the current committed state.

        Accepted and rejected batches both advance the ledger, so this is a
        *state observation* token rather than a content hash.  A publisher
        reads every manifest coordinate while holding one store instance and
        records the token alongside the version it produces.
        """
        return self._seq

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    # ------------------------------------------------------------- indexing

    def _load_candidates(self, candidate_lists: dict) -> None:
        normalized: dict[Coord, list[Coord]] = {}
        for target, chain in candidate_lists.items():
            target_coord = Coord.parse(target)
            coords = [Coord.parse(c) for c in chain]
            if len(coords) != len(set(coords)):
                raise BatchError(
                    f"candidate list for {target_coord} contains duplicates"
                )
            normalized[target_coord] = coords
            for priority, coord in enumerate(coords):
                self._conn.execute(
                    "INSERT INTO candidate_coord "
                    "(target_key, priority, coord_key) VALUES (?,?,?)",
                    (target_coord.key(), priority, coord.key()),
                )
        self.candidates = normalized

    def _rebuild_indexes(self) -> None:
        self._universe = set()
        self._reverse = {}
        for target, coords in self.candidates.items():
            self._universe.add(target)
            # A correction on a coordinate always affects the query whose
            # target is that coordinate itself.
            self._reverse.setdefault(target, set()).add(target)
            for coord in coords:
                self._universe.add(coord)
                self._reverse.setdefault(coord, set()).add(target)

    def _build_initial_cache(self) -> None:
        for facility in self.policies:
            for target in self.candidates:
                self._recompute_one(facility, target, count=False)

    # ----------------------------------------------------------------- query

    def query(self, facility: str, target: "str | Coord") -> dict:
        if facility not in self.policies:
            raise KeyError(f"unknown facility: {facility}")
        coord = Coord.parse(target)
        if coord not in self.candidates:
            raise KeyError(f"unknown target language/context: {coord}")
        return dict(self.cache[(facility, coord.key())])

    def recompute_stats(self) -> dict[tuple[str, str], int]:
        return dict(self.recomputes)

    def ledger(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, status, applied_at, detail, error "
            "FROM batch_ledger ORDER BY seq"
        ).fetchall()
        return [
            {
                "seq": seq,
                "status": status,
                "applied_at": applied_at,
                "detail": json.loads(detail),
                "error": error,
            }
            for seq, status, applied_at, detail, error in rows
        ]

    def export_state(self) -> dict:
        """Full current state, used by the independent reference oracle."""
        return {
            "candidates": {
                t.key(): [c.key() for c in coords]
                for t, coords in self.candidates.items()
            },
            "policies": {
                f: {
                    "allow_generic": p.allow_generic,
                    "require_public_reviewed": p.require_public_reviewed,
                    "blocked": sorted(c.key() for c in p.blocked),
                }
                for f, p in self.policies.items()
            },
            "entries": [
                {
                    "facility": e.facility,
                    "coord": e.coord.key(),
                    "source_id": e.source_id,
                    "body_state": e.body_state,
                    "text": e.text,
                    "is_public": e.is_public,
                    "review": e.review,
                }
                for e in self.entries.values()
            ],
        }

    # ---------------------------------------------------------------- batches

    def apply_batch(self, batch: Batch) -> int:
        detail = self._batch_detail(batch)
        while True:
            self._ensure_fresh()
            seq = self._seq + 1
            try:
                plan, scope = self._validate(batch)
            except BatchError as exc:
                self._record_batch(seq, "rejected", detail, str(exc))
                self._seq = seq
                raise

            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # A correction committed by another connection after this
                # batch was validated would be overwritten by the stale
                # plan; reload and revalidate instead.
                if self._state_counter() != self._token:
                    conn.rollback()
                    self._reload_live_state()
                    continue
                self._apply_in_transaction(plan, batch.policy, scope)
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            self._token = self._state_counter()
            break
        self._record_batch(seq, "accepted", detail, "")
        self._seq = seq
        return seq

    def _apply_in_transaction(self, plan, policy_correction, scope) -> None:
        """Write an already-validated plan; the caller owns the transaction.

        Also advances the persistent state counter in the same transaction,
        which is how other connections notice that live wording moved.
        """

        for entry in plan:
            self._write_entry(entry)
            self.entries[self._ekey(entry)] = entry
        if policy_correction is not None:
            pc = policy_correction
            policy = Policy(
                facility=pc.facility,
                allow_generic=pc.allow_generic,
                require_public_reviewed=pc.require_public_reviewed,
                blocked=pc.blocked,
            )
            self._write_policy(policy)
            self.policies[pc.facility] = policy

        for facility, target in sorted(
            scope, key=lambda ft: (ft[0], ft[1].key())
        ):
            self._recompute_one(facility, target, count=True)
        self._conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
            "WHERE key = 'state_counter'"
        )

    def _validate(self, batch: Batch):
        if not batch.entry_corrections and batch.policy is None:
            raise BatchError("empty batch: nothing to correct")

        concrete_facilities = set()
        for c in batch.entry_corrections:
            if c.facility != GENERIC:
                concrete_facilities.add(c.facility)
        if batch.policy is not None:
            concrete_facilities.add(batch.policy.facility)
        if len(concrete_facilities) > 1:
            raise BatchError(
                "a batch carries corrections for one concrete facility at most"
            )
        if concrete_facilities:
            facility = next(iter(concrete_facilities))
            if facility not in self.policies:
                raise BatchError(f"unknown facility: {facility}")
        else:
            facility = None

        seen_keys: set[tuple[str, str, str]] = set()
        # A single record may carry several corrections in one batch; they
        # are applied in order against the evolving working copy so the batch
        # becomes visible atomically as one final record state.
        working: dict[tuple[str, Coord, str], Entry] = {}
        touched_coords: set[Coord] = set()
        generic_touched = False

        def _current(corr: EntryCorrection):
            key = (corr.facility, corr.coord, corr.source_id)
            if key in working:
                return working[key]
            return self.entries.get(key)

        for c in batch.entry_corrections:
            if not isinstance(c.coord, Coord):
                raise BatchError("correction coordinate must be a Coord")
            if c.coord not in self._universe:
                raise BatchError(
                    f"coordinate {c.coord} is outside the fixed candidate "
                    "universe"
                )
            k = (c.facility, c.coord.key(), c.source_id)
            seen_keys.add(k)

            existing = _current(c)
            touched_coords.add(c.coord)
            if c.facility == GENERIC:
                generic_touched = True

            if c.kind == "set_body":
                if not isinstance(c.text, str) or not c.text:
                    raise BatchError("set_body requires non-empty text")
                updated = Entry(
                    facility=c.facility,
                    coord=c.coord,
                    source_id=c.source_id,
                    body_state=TEXT,
                    text=c.text,
                    is_public=c.is_public,
                    review=c.review,
                )
            elif c.kind == "clear":
                updated = Entry(
                    facility=c.facility,
                    coord=c.coord,
                    source_id=c.source_id,
                    body_state=CLEAR,
                    text="",
                    is_public=c.is_public
                    if existing is None
                    else existing.is_public,
                    review=c.review
                    if existing is None
                    else existing.review,
                )
            elif c.kind == "withdraw":
                if existing is None:
                    raise BatchError(
                        f"cannot withdraw missing record {k}"
                    )
                updated = Entry(
                    facility=c.facility,
                    coord=c.coord,
                    source_id=c.source_id,
                    body_state=WITHDRAWN,
                    text="",
                    is_public=existing.is_public,
                    review=existing.review,
                )
            elif c.kind == "set_review":
                if existing is None:
                    raise BatchError(
                        f"cannot change review of missing record {k}"
                    )
                if c.review not in REVIEW_STATES:
                    raise BatchError(f"invalid review state: {c.review}")
                updated = Entry(
                    facility=c.facility,
                    coord=c.coord,
                    source_id=c.source_id,
                    body_state=existing.body_state,
                    text=existing.text,
                    is_public=existing.is_public,
                    review=c.review,
                )
            else:
                raise BatchError(f"unknown correction kind: {c.kind}")

            working[(c.facility, c.coord, c.source_id)] = updated

        plan = list(working.values())

        if batch.policy is not None:
            pc = batch.policy
            if not isinstance(pc.allow_generic, bool) or not isinstance(
                pc.require_public_reviewed, bool
            ):
                raise BatchError("policy flags must be booleans")
            unknown = set(pc.blocked) - self._universe
            if unknown:
                raise BatchError(
                    "blocked coordinate outside universe: "
                    + ", ".join(c.key() for c in sorted(unknown, key=Coord.key))
                )

        scope = self._recompute_scope(
            facility,
            concrete_facilities,
            touched_coords,
            generic_touched,
            policy_changed=batch.policy is not None,
        )
        return plan, scope

    def _recompute_scope(
        self,
        facility: "str | None",
        concrete_facilities: set[str],
        touched_coords: set[Coord],
        generic_touched: bool,
        policy_changed: bool,
    ) -> set[tuple[str, Coord]]:
        scope: set[tuple[str, Coord]] = set()

        affected_targets: set[Coord] = set()
        for coord in touched_coords:
            affected_targets |= self._reverse.get(coord, set())

        if policy_changed:
            # A policy governs every query of that facility.
            for target in self.candidates:
                scope.add((facility, target))

        if generic_touched:
            # A generic-word correction changes every facility whose policy
            # actually reads the generic pseudo-facility — including batches
            # that touch only generic records (where ``facility`` is None)
            # and batches that also carry a policy correction.
            for fac, pol in self.policies.items():
                if not pol.allow_generic:
                    continue
                if policy_changed and fac == facility:
                    continue  # already fully covered above
                for target in affected_targets:
                    scope.add((fac, target))

        if not policy_changed and facility is not None:
            for target in affected_targets:
                scope.add((facility, target))
        return scope

    # ------------------------------------------------------- review drafts

    def _actual_changes(self, plan):
        """Keep only records whose complete value really moved from live."""

        return [
            entry for entry in plan
            if entry_value(entry) != entry_value(
                self.entries.get(self._ekey(entry))
            )
        ]

    def _prepare_draft(self, batch: Batch):
        """Validate a batch and snapshot its net effect against live state.

        Returns ``(plan, policy_change, scope)``.  ``save_draft`` uses this
        while observing one committed baseline; approval later reuses the
        stored snapshot, never these commands.
        """

        try:
            plan, _ = self._validate(batch)
        except BatchError as exc:
            raise DraftError(f"invalid correction batch: {exc}") from None

        # Only *actual* changes form the draft: a correction line whose net
        # effect equals the baseline changes nothing and must not later be
        # reported as either a draft edit or a supply hazard.
        plan = self._actual_changes(plan)

        policy_change = None
        if batch.policy is not None:
            pc = batch.policy
            live_policy = self.policies[pc.facility]
            before = policy_value(live_policy)
            after = policy_value(Policy(
                facility=pc.facility,
                allow_generic=pc.allow_generic,
                require_public_reviewed=pc.require_public_reviewed,
                blocked=pc.blocked,
            ))
            if before != after:
                policy_change = (pc.facility, before, after)

        if policy_change is None and not plan:
            raise DraftError("batch changes nothing relative to the baseline")

        # The review scope is wider than the direct-recompute scope: a
        # generic change can affect facilities the baseline policy currently
        # forbids from using generic words, because approval happens later.
        scope = self._review_targets(
            [(entry.facility, entry.coord) for entry in plan],
            policy_change[0] if policy_change is not None else None,
        )
        return plan, policy_change, scope

    def save_draft(self, batch: Batch) -> int:
        """Validate ``batch`` like a direct correction and snapshot a draft.

        Nothing is written to the live word supply: the draft only records
        the batch's net effect on the *current committed baseline*, the
        complete value of every record it changes, an optional changed
        policy, and every affected target's baseline/draft supply result.
        The returned draft id is immutable from now on and survives reopen.
        """

        while True:
            self._ensure_fresh()
            observed = self._token
            plan, policy_change, scope = self._prepare_draft(batch)

            base_signatures = {
                (fac, target.key()): supply_signature(self.query(fac, target))
                for fac, target in scope
            }

            # Apply the plan as a small override map over live state, then
            # resolve affected targets against that hypothetical state.  The
            # unchanged entry table is never copied.
            overrides = {self._ekey(entry): entry for entry in plan}
            patched_policies = None
            if policy_change is not None:
                fac, _, after = policy_change
                patched_policies = dict(self.policies)
                patched_policies[fac] = Policy(
                    facility=fac,
                    allow_generic=after["allow_generic"],
                    require_public_reviewed=after[
                        "require_public_reviewed"
                    ],
                    blocked=frozenset(
                        Coord.parse(c) for c in after["blocked"]
                    ),
                )

            draft_results = {}
            for fac, target in scope:
                payload = resolve(
                    fac,
                    target,
                    self.candidates,
                    patched_policies or self.policies,
                    self.entries,
                    entry_overrides=overrides,
                )
                draft_results[(fac, target.key())] = supply_signature(payload)

            changed = {self._ekey(entry): entry for entry in plan}
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # If live state moved since the snapshot was computed, the
                # baseline would be wrong; roll back and retake it.
                if self._state_counter() != observed:
                    conn.rollback()
                    self._reload_live_state()
                    continue
                cur = conn.execute(
                    "INSERT INTO review_draft (created_at, status, "
                    "batch_detail) VALUES (?,?,?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        DRAFT_SAVED,
                        json.dumps(
                            self._batch_detail(batch), ensure_ascii=False
                        ),
                    ),
                )
                draft_id = cur.lastrowid
                for key, entry in changed.items():
                    conn.execute(
                        "INSERT INTO review_draft_entry (draft_id, facility, "
                        "lang, ctx, source_id, base_value, draft_value) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            draft_id,
                            entry.facility,
                            entry.coord.lang,
                            entry.coord.ctx,
                            entry.source_id,
                            None
                            if key not in self.entries
                            else json.dumps(
                                entry_value(self.entries[key]), sort_keys=True
                            ),
                            json.dumps(entry_value(entry), sort_keys=True),
                        ),
                    )
                if policy_change is not None:
                    fac, before, after = policy_change
                    conn.execute(
                        "INSERT INTO review_draft_policy (draft_id, facility, "
                        "base_value, draft_value) VALUES (?,?,?,?)",
                        (
                            draft_id,
                            fac,
                            json.dumps(before, sort_keys=True),
                            json.dumps(after, sort_keys=True),
                        ),
                    )
                for (fac, target_key), base_sig in base_signatures.items():
                    conn.execute(
                        "INSERT INTO review_draft_target (draft_id, facility, "
                        "target_key, base_result, draft_result) "
                        "VALUES (?,?,?,?,?)",
                        (
                            draft_id,
                            fac,
                            target_key,
                            json.dumps(base_sig, sort_keys=True),
                            json.dumps(
                                draft_results[(fac, target_key)],
                                sort_keys=True,
                            ),
                        ),
                    )
                # Saving a draft does not change live wording, so it must not
                # advance the shared state token other connections watch.
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            self._token = observed
            return draft_id

    def draft(self, draft_id: int) -> dict:
        """Read one saved draft's immutable snapshot."""

        row = self._conn.execute(
            "SELECT draft_id, created_at, status, merged_at, merged_seq, "
            "batch_detail FROM review_draft WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise DraftNotFound(f"unknown draft: {draft_id}")
        entries = [
            {
                "facility": fac,
                "coord": Coord(lang, ctx).key(),
                "source_id": sid,
                "base_value": None if base is None else json.loads(base),
                "draft_value": json.loads(draft),
            }
            for fac, lang, ctx, sid, base, draft in self._conn.execute(
                "SELECT facility, lang, ctx, source_id, base_value, "
                "draft_value FROM review_draft_entry WHERE draft_id=? "
                "ORDER BY facility, lang, ctx, source_id",
                (draft_id,),
            )
        ]
        policy_rows = [
            {
                "facility": fac,
                "base_value": json.loads(base),
                "draft_value": json.loads(draft),
            }
            for fac, base, draft in self._conn.execute(
                "SELECT facility, base_value, draft_value "
                "FROM review_draft_policy WHERE draft_id=?",
                (draft_id,),
            )
        ]
        targets = [
            {
                "facility": fac,
                "target": target_key,
                "base_result": json.loads(base),
                "draft_result": json.loads(draft),
            }
            for fac, target_key, base, draft in self._conn.execute(
                "SELECT facility, target_key, base_result, draft_result "
                "FROM review_draft_target WHERE draft_id=? "
                "ORDER BY facility, target_key",
                (draft_id,),
            )
        ]
        return {
            "draft_id": row[0],
            "created_at": row[1],
            "status": row[2],
            "merged_at": row[3],
            "merged_seq": row[4],
            "batch": json.loads(row[5]),
            "entries": entries,
            "policy": policy_rows[0] if policy_rows else None,
            "targets": targets,
        }

    def drafts(self) -> list[dict]:
        """Ids and statuses of every saved draft, oldest first."""

        return [
            {
                "draft_id": draft_id,
                "created_at": created_at,
                "status": status,
                "merged_at": merged_at,
                "merged_seq": seq,
            }
            for draft_id, created_at, status, merged_at, seq in self._conn
            .execute(
                "SELECT draft_id, created_at, status, merged_at, merged_seq "
                "FROM review_draft ORDER BY draft_id"
            )
        ]

    def merge_draft(self, draft_id: int) -> dict:
        """Merge a saved draft into the live store, or reject without writes.

        The approval is evaluated against the *current* live state, never by
        replaying the draft's correction commands.  Records, the one policy
        and every supply result the draft could affect are checked three
        ways; a single disagreement raises :class:`MergeConflict` and the
        live word supply, ledger and caches stay exactly as they were.
        """

        snapshot = self.draft(draft_id)
        if snapshot["status"] == DRAFT_MERGED:
            raise DraftAlreadyMerged(
                f"draft {draft_id} was already merged at "
                f"seq {snapshot['merged_seq']}; approve it again is a no-op"
            )

        # ``_merge_once`` decides against the current live state and then
        # re-reads it inside the write transaction.  The approval decision
        # and the applied result therefore belong to the same committed
        # state: a correction committed between check and write forces the
        # whole decision to be retaken rather than being overwritten.
        return self._merge_once(draft_id, snapshot)

    # -- internals ----------------------------------------------------------

    def _merge_once(self, draft_id: int, snapshot: dict) -> dict:
        if self._state_counter() != self._token:
            # Another connection committed since our in-memory state was
            # loaded; re-read before deciding anything.
            self._reload_live_state()
        while True:
            observed = self._token
            conflicts, _, plan, policy_change, scope, supply_report = (
                self._evaluate_draft(snapshot)
            )
            if conflicts:
                raise MergeConflict(
                    draft_id, record_conflicts=conflicts
                )
            if supply_report:
                raise MergeConflict(
                    draft_id, supply_conflicts=supply_report
                )

            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read inside the write lock.  In WAL mode this connection
                # now observes the newest committed data; if another
                # connection committed after the decision was taken, abandon
                # this attempt, reload and take the whole decision again.
                # The approved outcome and the decision therefore always
                # belong to one committed state, and a correction committed
                # after the check can never be overwritten by this merge.
                if self._state_counter() != observed:
                    conn.rollback()
                    self._reload_live_state()
                    continue
                self._apply_in_transaction(plan, policy_change, scope)
                # Choose the ledger slot from the ledger itself, inside the
                # write lock, so a concurrent rejected batch cannot collide.
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM batch_ledger"
                ).fetchone()[0]
                cur = conn.execute(
                    "UPDATE review_draft SET status=?, merged_at=?, "
                    "merged_seq=? WHERE draft_id=? AND status=?",
                    (
                        DRAFT_MERGED,
                        datetime.now(timezone.utc).isoformat(),
                        seq,
                        draft_id,
                        DRAFT_SAVED,
                    ),
                )
                if cur.rowcount != 1:
                    # Another approver merged this draft first; this approval
                    # must not write a second set of corrections.
                    conn.rollback()
                    self._reload_live_state()
                    raise DraftAlreadyMerged(
                        f"draft {draft_id} was merged concurrently"
                    )
                # Record the merge in the same finite batch ledger as direct
                # corrections, so the commit token survives reopen and the
                # approval is auditable alongside them.
                conn.execute(
                    "INSERT INTO batch_ledger (seq, status, applied_at, "
                    "detail, error) VALUES (?,?,?,?,?)",
                    (
                        seq,
                        "merged_draft",
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(
                            {
                                "draft_id": draft_id,
                                "records": len(plan),
                                "policy": policy_change.facility
                                if policy_change is not None else None,
                                "targets": sorted(
                                    t.key() for _, t in scope
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ),
                )
                conn.commit()
            except DraftAlreadyMerged:
                raise
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            self._seq = max(self._seq, seq)
            self._token = self._state_counter()
            return {
                "draft_id": draft_id,
                "status": DRAFT_MERGED,
                "seq": seq,
                "records": len(plan),
                "policy": policy_change.facility
                if policy_change is not None else None,
                "targets": len(scope),
                "supply": [],
            }

    def _evaluate_draft(self, snapshot):
        """Three-way merge one draft against the live state.

        Returns ``(record_conflicts, resolved_entries, plan, policy_change,
        scope, supply_conflicts)``.  ``record_conflicts`` is a non-empty list
        when any complete record or the policy disagrees; in that case no
        supply check is attempted.  Otherwise ``supply_conflicts`` lists every
        target whose expected result does not survive the merge.
        """

        # -- complete records ------------------------------------------------
        record_conflicts = []
        resolved_entries: dict[tuple[str, Coord, str], "Entry | None"] = {}
        for item in snapshot["entries"]:
            key = (
                item["facility"],
                Coord.parse(item["coord"]),
                item["source_id"],
            )
            base = item["base_value"]
            draft = item["draft_value"]
            live = entry_value(self.entries.get(key))
            merged, clash = merge_value(base, live, draft)
            if clash:
                record_conflicts.append(
                    {
                        "kind": "entry",
                        "facility": item["facility"],
                        "coord": item["coord"],
                        "source_id": item["source_id"],
                        "base": base,
                        "draft": draft,
                        "live": live,
                    }
                )
            elif merged is not None:
                resolved_entries[key] = self._entry_from_value(
                    item["facility"], Coord.parse(item["coord"]),
                    item["source_id"], merged,
                )

        # -- the single optional policy -------------------------------------
        policy_change = None
        policy_item = snapshot["policy"]
        if policy_item is not None:
            fac = policy_item["facility"]
            base = policy_item["base_value"]
            draft = policy_item["draft_value"]
            live_policy = self.policies.get(fac)
            live = None if live_policy is None else policy_value(live_policy)
            merged, clash = merge_value(base, live, draft)
            if clash:
                record_conflicts.append(
                    {
                        "kind": "policy",
                        "facility": fac,
                        "base": base,
                        "draft": draft,
                        "live": live,
                    }
                )
            elif merged is not None:
                policy_change = PolicyCorrection(
                    facility=fac,
                    allow_generic=merged["allow_generic"],
                    require_public_reviewed=merged[
                        "require_public_reviewed"
                    ],
                    blocked=frozenset(
                        Coord.parse(c) for c in merged["blocked"]
                    ),
                )

        if record_conflicts:
            return (
                record_conflicts,
                resolved_entries,
                list(resolved_entries.values()),
                policy_change,
                set(),
                [],
            )

        # -- supply check ---------------------------------------------------
        plan = list(resolved_entries.values())
        # Overlay just the merged records on live state; the unchanged entry
        # table is shared rather than copied.
        overrides = dict(resolved_entries)
        policies_after = dict(self.policies)
        if policy_change is not None:
            pc = policy_change
            policies_after[pc.facility] = Policy(
                facility=pc.facility,
                allow_generic=pc.allow_generic,
                require_public_reviewed=pc.require_public_reviewed,
                blocked=pc.blocked,
            )

        checked = self._supply_check_targets(snapshot)
        # The merge writes exactly these targets, so the recompute scope is
        # the checked set itself: no unrelated facility or chain is touched.
        scope = set(checked)
        supply_conflicts = []
        for facility, target in sorted(
            checked, key=lambda ft: (ft[0], ft[1].key())
        ):
            base_sig = supply_signature(self.query(facility, target))
            live_sig = base_sig
            merged_sig = supply_signature(
                resolve(
                    facility,
                    target,
                    self.candidates,
                    policies_after,
                    self.entries,
                    entry_overrides=overrides,
                )
            )
            recorded_base, draft_sig = _draft_signature(
                snapshot, facility, target, base_sig
            )
            expected = expected_supply(recorded_base, draft_sig, live_sig)
            if expected is None or merged_sig != expected:
                supply_conflicts.append(
                    {
                        "facility": facility,
                        "target": target.key(),
                        "base": recorded_base,
                        "draft": draft_sig,
                        "live": live_sig,
                        "merged": merged_sig,
                        "expected": expected,
                    }
                )

        return (
            [],
            resolved_entries,
            plan,
            policy_change,
            scope,
            supply_conflicts,
        )

    def _review_targets(self, changes, policy_facility):
        """Every target whose wording a set of review changes can affect.

        ``changes`` is an iterable of ``(facility, coord)`` pairs that the
        draft really changes; ``policy_facility`` is the facility whose whole
        policy changed, if any.  The scope follows the approval rules:

        * an ordinary record -> only that facility's targets whose fixed
          chain references the coordinate;
        * a generic record -> every facility's targets that reference the
          coordinate, even facilities the baseline policy forbids from using
          generic words (they may be allowed by the time of approval);
        * a policy -> every target that policy governs.

        The index makes this proportional to the changed coordinates and the
        targets that cite them, never to the whole library.
        """

        record_coords: set[Coord] = set()
        generic_coords: set[Coord] = set()
        for facility, coord in changes:
            if facility == GENERIC:
                generic_coords.add(coord)
            else:
                record_coords.add(coord)

        record_facilities = {
            facility for facility, _ in changes if facility != GENERIC
        }
        targets: set[tuple[str, Coord]] = set()
        for fac in record_facilities:
            for coord in record_coords:
                for target in self._reverse.get(coord, ()):
                    targets.add((fac, target))

        if generic_coords:
            for fac in self.policies:
                for coord in generic_coords:
                    for target in self._reverse.get(coord, ()):
                        targets.add((fac, target))

        if policy_facility is not None:
            for target in self.candidates:
                targets.add((policy_facility, target))

        return targets

    def _supply_check_targets(self, snapshot):
        """Targets the merge must re-check, derived from the draft snapshot."""

        changes = [
            (item["facility"], Coord.parse(item["coord"]))
            for item in snapshot["entries"]
        ]
        policy_facility = (
            snapshot["policy"]["facility"]
            if snapshot["policy"] is not None else None
        )
        return self._review_targets(changes, policy_facility)

    @staticmethod
    def _entry_from_value(facility, coord, source_id, value):
        return Entry(
            facility=facility,
            coord=coord,
            source_id=source_id,
            body_state=value["body_state"],
            text=value["text"],
            is_public=bool(value["is_public"]),
            review=value["review"],
        )

    def _reload_live_state(self):
        """Drop in-memory caches after a failed write; re-read from SQLite."""

        self.entries = {}
        for fac, lang, ctx, sid, state, text, pub, rev in self._conn.execute(
            "SELECT facility, lang, ctx, source_id, body_state, text, "
            "is_public, review FROM entry_record"
        ):
            entry = Entry(
                facility=fac,
                coord=Coord(lang, ctx),
                source_id=sid,
                body_state=state,
                text=text,
                is_public=bool(pub),
                review=rev,
            )
            self.entries[self._ekey(entry)] = entry
        self.policies = {}
        for facility, allow, req in self._conn.execute(
            "SELECT facility, allow_generic, require_public_reviewed "
            "FROM facility"
        ):
            blocked = frozenset(
                Coord.parse(k)
                for (k,) in self._conn.execute(
                    "SELECT coord_key FROM blocked_coord WHERE facility=?",
                    (facility,),
                )
            )
            self.policies[facility] = Policy(
                facility=facility,
                allow_generic=bool(allow),
                require_public_reviewed=bool(req),
                blocked=blocked,
            )
        # Cached query results belong to the state we just discarded; re-read
        # them too so readers on this connection never see a stale answer.
        self.cache = {}
        for fac, tk, payload in self._conn.execute(
            "SELECT facility, target_key, payload FROM result_cache"
        ):
            self.cache[(fac, tk)] = json.loads(payload)
        self.recomputes = {
            (fac, tk): n
            for fac, tk, n in self._conn.execute(
                "SELECT facility, target_key, recomputes FROM recompute_stat"
            )
        }
        self._token = self._state_counter()

    def _ensure_fresh(self) -> None:
        """Reload in-memory state if another connection committed since load."""

        if self._state_counter() != self._token:
            self._reload_live_state()

    def _state_counter(self) -> int:
        """Persistent live-state token shared by every connection.

        It is advanced inside the same transaction as every accepted batch
        and every draft merge, so two connections on the same file can tell
        whether the wording they validated is still the wording on disk.
        """

        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'state_counter'"
        ).fetchone()
        return 0 if row is None else int(row[0])

    # ----------------------------------------------------------------- writes

    @staticmethod
    def _ekey(entry: Entry) -> tuple[str, Coord, str]:
        return entry.facility, entry.coord, entry.source_id

    def _write_entry(self, entry: Entry) -> None:
        if entry.body_state not in BODY_STATES:
            raise BatchError(f"invalid body state: {entry.body_state}")
        if entry.review not in REVIEW_STATES:
            raise BatchError(f"invalid review state: {entry.review}")
        self._conn.execute(
            "INSERT INTO entry_record (facility, lang, ctx, source_id, "
            "body_state, text, is_public, review) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (facility, lang, ctx, source_id) DO UPDATE SET "
            "body_state=excluded.body_state, text=excluded.text, "
            "is_public=excluded.is_public, review=excluded.review",
            (
                entry.facility,
                entry.coord.lang,
                entry.coord.ctx,
                entry.source_id,
                entry.body_state,
                entry.text,
                int(entry.is_public),
                entry.review,
            ),
        )

    def _write_policy(self, policy: Policy) -> None:
        self._conn.execute(
            "INSERT INTO facility (facility, allow_generic, "
            "require_public_reviewed) VALUES (?,?,?) ON CONFLICT(facility) "
            "DO UPDATE SET allow_generic=excluded.allow_generic, "
            "require_public_reviewed=excluded.require_public_reviewed",
            (
                policy.facility,
                int(policy.allow_generic),
                int(policy.require_public_reviewed),
            ),
        )
        self._conn.execute(
            "DELETE FROM blocked_coord WHERE facility=?", (policy.facility,)
        )
        self._conn.executemany(
            "INSERT INTO blocked_coord (facility, coord_key) VALUES (?,?)",
            [(policy.facility, c.key()) for c in policy.blocked],
        )

    def _recompute_one(
        self, facility: str, target: Coord, count: bool
    ) -> None:
        payload = resolve(
            facility,
            target,
            self.candidates,
            self.policies,
            self.entries,
        )
        self.cache[(facility, target.key())] = payload
        self._conn.execute(
            "INSERT INTO result_cache (facility, target_key, payload) "
            "VALUES (?,?,?) ON CONFLICT (facility, target_key) DO UPDATE SET "
            "payload=excluded.payload",
            (facility, target.key(), json.dumps(payload, ensure_ascii=False)),
        )
        if count:
            key = (facility, target.key())
            self.recomputes[key] = self.recomputes.get(key, 0) + 1
            self._conn.execute(
                "INSERT INTO recompute_stat (facility, target_key, recomputes) "
                "VALUES (?,?,1) ON CONFLICT (facility, target_key) DO UPDATE "
                "SET recomputes=recompute_stat.recomputes+1",
                key,
            )

    def _record_batch(
        self, seq: int, status: str, detail: dict, error: str
    ) -> None:
        conn = self._conn
        if conn.in_transaction:
            # Should not happen: rejected batches are logged in their own
            # transaction so a later failed BEGIN IMMEDIATE cannot roll them.
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another connection may have logged batches since ``seq`` was
            # chosen; take the next free ledger slot rather than colliding.
            actual = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM batch_ledger"
            ).fetchone()[0]
            if actual != seq:
                seq = actual
            conn.execute(
                "INSERT INTO batch_ledger "
                "(seq, status, applied_at, detail, error) VALUES (?,?,?,?,?)",
                (
                    seq,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(detail, ensure_ascii=False),
                    error,
                ),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        self._seq = max(self._seq, seq)

    @staticmethod
    def _batch_detail(batch: Batch) -> dict:
        return {
            "entries": [
                {
                    "kind": c.kind,
                    "facility": c.facility,
                    "coord": c.coord.key(),
                    "source_id": c.source_id,
                    "text": c.text,
                    "is_public": c.is_public,
                    "review": c.review,
                }
                for c in batch.entry_corrections
            ],
            "policy": None
            if batch.policy is None
            else {
                "facility": batch.policy.facility,
                "allow_generic": batch.policy.allow_generic,
                "require_public_reviewed": batch.policy.require_public_reviewed,
                "blocked": sorted(
                    c.key() for c in batch.policy.blocked
                ),
            },
        }
