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
        store._rebuild_indexes()
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
        seq = self._seq + 1
        detail = self._batch_detail(batch)
        try:
            plan, scope = self._validate(batch)
        except BatchError as exc:
            self._record_batch(seq, "rejected", detail, str(exc))
            self._seq = seq
            raise

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for entry in plan:
                self._write_entry(entry)
                self.entries[self._ekey(entry)] = entry
            if batch.policy is not None:
                pc = batch.policy
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

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._record_batch(seq, "accepted", detail, "")
        self._seq = seq
        return seq

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
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO batch_ledger (seq, status, applied_at, detail, error) "
            "VALUES (?,?,?,?,?)",
            (
                seq,
                status,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(detail, ensure_ascii=False),
                error,
            ),
        )
        conn.commit()

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
