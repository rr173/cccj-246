"""Reopenable display-count reconciliation ledger (purely local).

The terminology store answers *what wording* a screen should show.  This
module answers a different question: **how many term displays did each
actually installed word pack account for?**  It never talks to a network, a
publisher or a screen UI.  Callers hand it screen ledgers that may arrive
late or be corrected afterwards; the ledger merges revisions, derives
counting intervals and attributes their increments to word-pack identities.

Two record kinds share one UTC-millisecond timeline
---------------------------------------------------
Installation record
    Key ``(screen, time_ms)``.  Content is the word-pack identity in force
    at that instant -- ``(channel, version, pack_id)`` -- or an *explicit*
    identity-unknown marker.  It records an actual installation, never a
    download notification.  An installation remains in force from its own
    instant until the next effective installation record; before the first
    record the identity is unknown.  Revoking an installation record removes
    an installation fact -- it is deliberately *not* the same as keeping a
    record whose identity is unknown.

Reading
    Key ``(screen, round_id, facility, language, context, time_ms)``.
    Content is a non-negative *cumulative* display counter.  A counter
    restart starts a new round.  Changing a player or a channel does not by
    itself restart the counter; only an explicit round change does.  The
    first reading of a round is a baseline (nothing is estimated before it)
    and nothing is estimated after the last reading.

Revision rules
--------------
Every key is represented only by its highest revision.  Lower revisions are
ignored.  If two records at the highest revision disagree -- inside one
batch or against the stored record -- the *whole* batch is rejected and the
ledger plus every reconciled result stays byte-for-byte unchanged.  Resending
the same revision and content has no effect.  A higher revision may revoke a
record; the revision number is retained (as a tombstone) so a late lower
revision can never resurrect it.

Incremental derivation
----------------------
Readings and installations are stored as effective records; the reconciled
result is materialised as *segments* (one per adjacent-reading interval).
A reading correction only rebuilds the segments whose closed interval
contains the corrected instant -- the two neighbours, or the one interval a
new/removed reading splits or joins.  An installation correction rebuilds
attribution only for segments of that screen that overlap the period in
which the effective installation state can change, including segments whose
two endpoints lie outside that period but whose interior crosses it.
Reports read only the segment table, never the raw telemetry, and each
report observes one committed state.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- record kinds ---------------------------------------------------------

#: Installation record asserting a concrete word-pack identity.
INSTALLED = "identity"
#: Installation record explicitly saying "the identity is unknown here".
UNKNOWN_INSTALL = "unknown"
#: Tombstone: this revision revokes the record at the key.
REVOKED = "revoked"
#: Reading carrying a cumulative count.
CUMULATIVE = "cumulative"

# --- attribution ----------------------------------------------------------

#: The whole interval is confirmed to be one word pack.
ATTRIBUTED = "pack"
#: Some part of the interval had no determinable identity.
IDENTITY_UNKNOWN = "identity_unknown"
#: The interval was fully identified but saw more than one pack.
CROSS_VERSION = "cross_version"

REASONS = frozenset({IDENTITY_UNKNOWN, CROSS_VERSION})

#: Sentinel used for "no upper bound" in segment overlap queries.
_MAX_MS = 2 ** 63 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS install_record (
    screen   TEXT NOT NULL,
    time_ms  INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    channel  TEXT,
    version  INTEGER,
    pack_id  TEXT,
    PRIMARY KEY (screen, time_ms)
);
CREATE TABLE IF NOT EXISTS reading_record (
    screen    TEXT NOT NULL,
    round_id  TEXT NOT NULL,
    facility  TEXT NOT NULL,
    lang      TEXT NOT NULL,
    ctx       TEXT NOT NULL,
    time_ms   INTEGER NOT NULL,
    revision  INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    count     INTEGER,
    PRIMARY KEY (screen, round_id, facility, lang, ctx, time_ms)
);
CREATE TABLE IF NOT EXISTS segment (
    screen      TEXT NOT NULL,
    round_id    TEXT NOT NULL,
    facility    TEXT NOT NULL,
    lang        TEXT NOT NULL,
    ctx         TEXT NOT NULL,
    a_time      INTEGER NOT NULL,
    b_time      INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    attrib_kind TEXT NOT NULL,
    channel     TEXT,
    version     INTEGER,
    pack_id     TEXT,
    reason      TEXT,
    PRIMARY KEY (screen, round_id, facility, lang, ctx, a_time, b_time)
);
CREATE INDEX IF NOT EXISTS segment_by_start
    ON segment (screen, a_time, b_time);
CREATE INDEX IF NOT EXISTS segment_by_end
    ON segment (screen, b_time);
CREATE TABLE IF NOT EXISTS commit_log (
    seq              INTEGER PRIMARY KEY,
    applied_at       TEXT NOT NULL,
    installs_written INTEGER NOT NULL,
    readings_written INTEGER NOT NULL,
    segments_touched INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(value, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{what} must be a non-empty string")
    return value


def _require_int(value, what: str, *, minimum=None) -> int:
    if not _is_int(value):
        raise LedgerError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise LedgerError(f"{what} must be >= {minimum}")
    return value


class LedgerError(ValueError):
    """The batch is invalid, contradictory or would rewind a counter.

    Raising it leaves the record ledger, the derived segments and every
    reconciled result untouched.
    """


@dataclass(frozen=True)
class PackIdentity:
    """The full identity of an installed word pack."""

    channel: str
    version: int
    pack_id: str

    def __post_init__(self):
        _require_text(self.channel, "identity channel")
        _require_int(self.version, "identity version", minimum=0)
        _require_text(self.pack_id, "identity pack_id")

    @staticmethod
    def parse(value) -> "PackIdentity":
        if isinstance(value, PackIdentity):
            return value
        if isinstance(value, dict):
            try:
                return PackIdentity(
                    value["channel"], value["version"], value["pack_id"]
                )
            except KeyError as exc:
                raise LedgerError(
                    f"identity mapping is missing {exc.args[0]!r}"
                ) from None
        if isinstance(value, (tuple, list)) and len(value) == 3:
            return PackIdentity(value[0], value[1], value[2])
        raise LedgerError(
            "identity must be PackIdentity, a 3-tuple or a mapping with "
            "channel/version/pack_id"
        )

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "version": self.version,
            "pack_id": self.pack_id,
        }

    def key(self) -> tuple[str, int, str]:
        return (self.channel, self.version, self.pack_id)


def identity_from_device(device) -> PackIdentity:
    """Read the full identity of a :class:`termdb.ScreenDevice` pack.

    Only the device's local ``pack.json`` is consulted: no publisher, no
    network.  A version the publisher has since evicted is still identified
    normally.
    """

    pack = device.installed_version()
    if pack is None:
        raise LedgerError("the device has no installed word pack")
    return PackIdentity(pack["channel"], pack["version"], pack["pack_id"])


@dataclass(frozen=True)
class InstallRecord:
    """One installation assertion or revocation at ``(screen, time_ms)``."""

    screen: str
    time_ms: int
    revision: int
    kind: str
    identity: "PackIdentity | None" = None

    @classmethod
    def installed(
        cls,
        screen: str,
        time_ms: int,
        revision: int,
        identity,
    ) -> "InstallRecord":
        return cls(screen, time_ms, revision, INSTALLED,
                   PackIdentity.parse(identity))

    #: Alias that reads well at call sites.
    pack = installed

    @classmethod
    def unknown(
        cls, screen: str, time_ms: int, revision: int
    ) -> "InstallRecord":
        return cls(screen, time_ms, revision, UNKNOWN_INSTALL)

    @classmethod
    def revoke(
        cls, screen: str, time_ms: int, revision: int
    ) -> "InstallRecord":
        return cls(screen, time_ms, revision, REVOKED)

    @classmethod
    def from_device(
        cls,
        screen: str,
        time_ms: int,
        revision: int,
        device,
    ) -> "InstallRecord":
        return cls.installed(
            screen, time_ms, revision, identity_from_device(device)
        )


@dataclass(frozen=True)
class Reading:
    """One cumulative display reading, or a revocation of one reading."""

    screen: str
    round_id: str
    facility: str
    language: str
    context: str
    time_ms: int
    revision: int
    kind: str
    count: "int | None" = None

    @classmethod
    def cumulative(
        cls,
        screen: str,
        round_id: str,
        facility: str,
        language: str,
        context: str,
        time_ms: int,
        revision: int,
        count: int,
    ) -> "Reading":
        return cls(screen, round_id, facility, language, context, time_ms,
                   revision, CUMULATIVE, count)

    @classmethod
    def revoke(
        cls,
        screen: str,
        round_id: str,
        facility: str,
        language: str,
        context: str,
        time_ms: int,
        revision: int,
    ) -> "Reading":
        return cls(screen, round_id, facility, language, context, time_ms,
                   revision, REVOKED)


@dataclass
class LedgerBatch:
    """A batch may carry installation records and readings together."""

    installs: list = field(default_factory=list)
    readings: list = field(default_factory=list)


class CountLedger:
    """A reopenable, locally correctable display-count reconciliation ledger.

    The ledger owns one SQLite file.  Reopening it restores the effective
    records, the derived segments and the commit log; nothing is recomputed
    from raw telemetry and no network or screen page is involved.
    """

    def __init__(self, path: str, conn: sqlite3.Connection):
        self.path = path
        self._conn = conn
        self._seq = 0
        self._segments_touched = 0

    # ------------------------------------------------------------------ open

    @classmethod
    def open(cls, path: str) -> "CountLedger":
        """Open (or create) the ledger at ``path``.

        An existing file is reopened exactly: effective records, derived
        segments and commit history are all read back from disk.
        """

        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        ledger = cls(path, conn)
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM commit_log"
        ).fetchone()
        ledger._seq = row[0]
        return ledger

    @classmethod
    def open_near(cls, term_store_path: str, name: str = "counts.db"):
        """Open a ledger beside an existing terminology store file.

        This is only a convenience for local tools: the ledger shares no
        tables and no transaction with the term store.
        """

        directory = os.path.dirname(os.path.abspath(term_store_path))
        return cls.open(os.path.join(directory, name))

    def close(self) -> None:
        self._conn.close()

    @property
    def state_seq(self) -> int:
        """Commit token of the currently committed ledger state."""

        return self._seq

    # -------------------------------------------------------------- batching

    def apply(self, batch: LedgerBatch) -> int:
        """Validate and commit one batch of installation records/readings.

        A batch may carry both record kinds.  The whole batch is rejected if
        any input is malformed, any key's highest revision disagrees with a
        stored record at the same revision, or any effective count series
        would go backwards after the batch.  Rejection changes nothing.
        Returns the new commit sequence.
        """

        if not isinstance(batch, LedgerBatch):
            raise LedgerError("apply() requires a LedgerBatch")
        installs = self._normalise_installs(batch.installs)
        readings = self._normalise_readings(batch.readings)
        if not installs and not readings:
            raise LedgerError("empty batch: nothing to record")

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            install_plan = self._merge_installs(installs)
            reading_plan = self._merge_readings(readings)

            affected_series = {
                item["series"] for item in reading_plan if item["write"]
            }
            changed_times_for: dict[tuple, set[int]] = {}
            for item in reading_plan:
                if item["write"]:
                    changed_times_for.setdefault(item["series"], set()).add(
                        item["record"].time_ms
                    )

            if not any(item["write"] for item in install_plan) and not any(
                item["write"] for item in reading_plan
            ):
                # Every line was a lower revision or an identical resend:
                # a genuine no-op, not even a new commit token.
                conn.commit()
                return self._seq

            for item in install_plan:
                if item["write"]:
                    self._write_install(item["record"])
            for item in reading_plan:
                if item["write"]:
                    self._write_reading(item["record"])

            # Counts may only grow within one (screen, round, facility,
            # language, context) series.  Compare the *post-batch* effective
            # readings; never compare different rounds.
            for series in affected_series:
                self._assert_monotone(
                    series,
                    changed_times_for.get(series, ()),
                )

            self._segments_touched = 0
            if reading_plan:
                self._rebuild_reading_segments(reading_plan)
            if install_plan:
                self._rebuild_install_attribution(install_plan)

            self._seq += 1
            conn.execute(
                "INSERT INTO commit_log (seq, applied_at, installs_written, "
                "readings_written, segments_touched) VALUES (?,?,?,?,?)",
                (
                    self._seq,
                    _now(),
                    sum(1 for i in install_plan if i["write"]),
                    sum(1 for i in reading_plan if i["write"]),
                    self._segments_touched,
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self._seq

    # --------------------------------------------------- normalise / inspect

    @staticmethod
    def _normalise_installs(records) -> dict:
        pending: dict[tuple[str, int], InstallRecord] = {}
        for rec in records:
            if not isinstance(rec, InstallRecord):
                raise LedgerError("installation list carries a non-record")
            _require_text(rec.screen, "installation screen")
            _require_int(rec.time_ms, "installation time_ms")
            _require_int(rec.revision, "installation revision", minimum=0)
            if rec.kind not in (INSTALLED, UNKNOWN_INSTALL, REVOKED):
                raise LedgerError(
                    f"invalid installation kind: {rec.kind!r}"
                )
            identity = None
            if rec.kind == INSTALLED:
                if rec.identity is None:
                    raise LedgerError(
                        "identity installation requires a pack identity"
                    )
                identity = PackIdentity.parse(rec.identity)
            elif rec.identity is not None:
                raise LedgerError(
                    f"{rec.kind} installation must not carry an identity"
                )
            normalised = InstallRecord(
                rec.screen, rec.time_ms, rec.revision, rec.kind, identity
            )
            key = (normalised.screen, normalised.time_ms)
            _merge_pending(pending, key, normalised, "installation")
        return pending

    @staticmethod
    def _normalise_readings(records) -> dict:
        pending: dict[tuple, Reading] = {}
        for rec in records:
            if not isinstance(rec, Reading):
                raise LedgerError("reading list carries a non-record")
            for value, what in (
                (rec.screen, "reading screen"),
                (rec.round_id, "reading round_id"),
                (rec.facility, "reading facility"),
                (rec.language, "reading language"),
                (rec.context, "reading context"),
            ):
                _require_text(value, what)
            _require_int(rec.time_ms, "reading time_ms")
            _require_int(rec.revision, "reading revision", minimum=0)
            if rec.kind not in (CUMULATIVE, REVOKED):
                raise LedgerError(f"invalid reading kind: {rec.kind!r}")
            count = None
            if rec.kind == CUMULATIVE:
                count = _require_int(
                    rec.count, "reading count", minimum=0
                )
            elif rec.count is not None:
                raise LedgerError("a revoked reading must not carry a count")
            normalised = Reading(
                rec.screen,
                rec.round_id,
                rec.facility,
                rec.language,
                rec.context,
                rec.time_ms,
                rec.revision,
                rec.kind,
                count,
            )
            _merge_pending(
                pending, _reading_key(normalised), normalised, "reading"
            )
        return pending

    # --------------------------------------------------------- revision merge

    def _merge_installs(self, pending: dict) -> list[dict]:
        plan = []
        for key, rec in pending.items():
            row = self._conn.execute(
                "SELECT revision, kind, channel, version, pack_id "
                "FROM install_record WHERE screen=? AND time_ms=?",
                key,
            ).fetchone()
            if row is None:
                plan.append({
                    "record": rec,
                    "write": True,
                    "changed": rec.kind != REVOKED,
                    "screen": rec.screen,
                })
                continue
            stored = InstallRecord(
                key[0], key[1], row[0], row[1],
                None if row[1] != INSTALLED
                else PackIdentity(row[2], row[3], row[4]),
            )
            if rec.revision < stored.revision:
                continue                      # a late lower revision
            if rec.revision == stored.revision:
                if _install_content(rec) != _install_content(stored):
                    raise LedgerError(
                        f"contradictory installation revision "
                        f"{rec.revision} for {key}"
                    )
                continue                      # idempotent resend
            plan.append({
                "record": rec,
                "write": True,
                "changed": (
                    _install_content(rec) != _install_content(stored)
                ),
                "screen": rec.screen,
            })
        return plan

    def _merge_readings(self, pending: dict) -> list[dict]:
        plan = []
        for key, rec in pending.items():
            row = self._conn.execute(
                "SELECT revision, kind, count FROM reading_record "
                "WHERE screen=? AND round_id=? AND facility=? AND lang=? "
                "AND ctx=? AND time_ms=?",
                key,
            ).fetchone()
            if row is None:
                plan.append({
                    "record": rec,
                    "write": True,
                    "changed": rec.kind == CUMULATIVE,
                    "series": _series_of(rec),
                })
                continue
            stored = Reading(
                rec.screen, rec.round_id, rec.facility, rec.language,
                rec.context, rec.time_ms, row[0], row[1], row[2],
            )
            if rec.revision < stored.revision:
                continue
            if rec.revision == stored.revision:
                if _reading_content(rec) != _reading_content(stored):
                    raise LedgerError(
                        f"contradictory reading revision {rec.revision} "
                        f"for {key}"
                    )
                continue
            plan.append({
                "record": rec,
                "write": True,
                "changed": (
                    _reading_content(rec) != _reading_content(stored)
                ),
                "series": _series_of(rec),
            })
        return plan

    # ----------------------------------------------------------------- writes

    def _write_install(self, rec: InstallRecord) -> None:
        identity = rec.identity
        self._conn.execute(
            "INSERT INTO install_record (screen, time_ms, revision, kind, "
            "channel, version, pack_id) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (screen, time_ms) DO UPDATE SET "
            "revision=excluded.revision, kind=excluded.kind, "
            "channel=excluded.channel, version=excluded.version, "
            "pack_id=excluded.pack_id",
            (
                rec.screen,
                rec.time_ms,
                rec.revision,
                rec.kind,
                None if identity is None else identity.channel,
                None if identity is None else identity.version,
                None if identity is None else identity.pack_id,
            ),
        )

    def _write_reading(self, rec: Reading) -> None:
        self._conn.execute(
            "INSERT INTO reading_record (screen, round_id, facility, lang, "
            "ctx, time_ms, revision, kind, count) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (screen, round_id, facility, lang, ctx, time_ms) "
            "DO UPDATE SET revision=excluded.revision, kind=excluded.kind, "
            "count=excluded.count",
            (
                rec.screen,
                rec.round_id,
                rec.facility,
                rec.language,
                rec.context,
                rec.time_ms,
                rec.revision,
                rec.kind,
                rec.count,
            ),
        )

    # ------------------------------------------------------------- validation

    def _assert_monotone(self, series: tuple, changed_times) -> None:
        """Check that counts never decrease within ``series``.

        A correction can only break ordering against the corrected instant's
        immediate neighbours, so this reads a bounded neighbourhood rather
        than the whole series.
        """

        for time_ms in sorted(changed_times):
            neighbours = []
            previous = self._conn.execute(
                "SELECT time_ms, count FROM reading_record WHERE screen=? "
                "AND round_id=? AND facility=? AND lang=? AND ctx=? AND "
                "kind=? AND time_ms < ? ORDER BY time_ms DESC LIMIT 1",
                (*series, CUMULATIVE, time_ms),
            ).fetchone()
            current = self._conn.execute(
                "SELECT time_ms, count FROM reading_record WHERE screen=? "
                "AND round_id=? AND facility=? AND lang=? AND ctx=? AND "
                "kind=? AND time_ms = ?",
                (*series, CUMULATIVE, time_ms),
            ).fetchone()
            following = self._conn.execute(
                "SELECT time_ms, count FROM reading_record WHERE screen=? "
                "AND round_id=? AND facility=? AND lang=? AND ctx=? AND "
                "kind=? AND time_ms > ? ORDER BY time_ms LIMIT 1",
                (*series, CUMULATIVE, time_ms),
            ).fetchone()
            for row in (previous, current, following):
                if row is not None:
                    neighbours.append(row)
            for (a_time, a_count), (b_time, b_count) in zip(
                neighbours, neighbours[1:]
            ):
                if b_count < a_count:
                    raise LedgerError(
                        f"cumulative count went backwards in round "
                        f"{series[1]!r} at {series[0]}/{series[2]}/"
                        f"{series[3]}/{series[4]}: {a_count} -> {b_count} "
                        f"(at {a_time}..{b_time})"
                    )

    # --------------------------------------------------- segment bookkeeping

    def _rebuild_reading_segments(self, reading_plan: list[dict]) -> None:
        changed_by_series: dict[tuple, set[int]] = {}
        for item in reading_plan:
            if item["changed"]:
                changed_by_series.setdefault(item["series"], set()).add(
                    item["record"].time_ms
                )
        if not changed_by_series:
            return

        for series, changed_times in changed_by_series.items():
            screen = series[0]
            # Each changed instant affects only the local interval partition:
            # between its surviving neighbours (or the point itself when it
            # has none).  Merge overlapping neighbourhoods.
            spans: list[list[int]] = []
            for time_ms in sorted(changed_times):
                low_row = self._conn.execute(
                    "SELECT time_ms FROM reading_record WHERE screen=? AND "
                    "round_id=? AND facility=? AND lang=? AND ctx=? AND "
                    "kind=? AND time_ms < ? ORDER BY time_ms DESC LIMIT 1",
                    (*series, CUMULATIVE, time_ms),
                ).fetchone()
                high_row = self._conn.execute(
                    "SELECT time_ms FROM reading_record WHERE screen=? AND "
                    "round_id=? AND facility=? AND lang=? AND ctx=? AND "
                    "kind=? AND time_ms > ? ORDER BY time_ms LIMIT 1",
                    (*series, CUMULATIVE, time_ms),
                ).fetchone()
                lo = time_ms if low_row is None else low_row[0]
                hi = time_ms if high_row is None else high_row[0]
                if spans and lo <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], hi)
                else:
                    spans.append([lo, hi])

            for lo, hi in spans:
                # Read only the readings inside this neighbourhood; no scan
                # of the whole series.
                rows = self._conn.execute(
                    "SELECT time_ms, count FROM reading_record WHERE "
                    "screen=? AND round_id=? AND facility=? AND lang=? AND "
                    "ctx=? AND kind=? AND time_ms >= ? AND time_ms <= ? "
                    "ORDER BY time_ms",
                    (*series, CUMULATIVE, lo, hi),
                ).fetchall()
                deleted = self._conn.execute(
                    "DELETE FROM segment WHERE screen=? AND round_id=? AND "
                    "facility=? AND lang=? AND ctx=? AND a_time >= ? AND "
                    "b_time <= ?",
                    (*series, lo, hi),
                ).rowcount
                self._segments_touched += max(0, deleted)
                # The new partition within the span may have merged former
                # neighbours (a revoked reading) or split an old interval (a
                # new reading); rebuild every interval the local readings
                # define.
                for (a_time, ca), (b_time, cb) in zip(rows, rows[1:]):
                    kind, identity = self._attribute(screen, a_time, b_time)
                    self._insert_segment(
                        series, a_time, b_time, cb - ca, kind, identity
                    )
                self._segments_touched += max(0, len(rows) - 1)

    def _install_at_or_before(self, screen: str, time_ms: int):
        return self._conn.execute(
            "SELECT kind, channel, version, pack_id FROM install_record "
            "WHERE screen=? AND time_ms <= ? AND kind != ? "
            "ORDER BY time_ms DESC LIMIT 1",
            (screen, time_ms, REVOKED),
        ).fetchone()

    def _next_install_time(self, screen: str, time_ms: int) -> int:
        row = self._conn.execute(
            "SELECT MIN(time_ms) FROM install_record WHERE screen=? AND "
            "time_ms > ? AND kind != ?",
            (screen, time_ms, REVOKED),
        ).fetchone()
        return _MAX_MS if row[0] is None else row[0]

    def _installs_between(self, screen: str, a_time: int, b_time: int):
        return self._conn.execute(
            "SELECT kind, channel, version, pack_id FROM install_record "
            "WHERE screen=? AND time_ms > ? AND time_ms < ? AND kind != ? "
            "ORDER BY time_ms",
            (screen, a_time, b_time, REVOKED),
        ).fetchall()

    def _attribute(self, screen: str, a_time: int, b_time: int):
        """Attribute one segment straight from indexed neighbour lookups."""

        row = self._install_at_or_before(screen, a_time)
        if row is None:
            return IDENTITY_UNKNOWN, None
        kind, channel, version, pack_id = row
        if kind == UNKNOWN_INSTALL or kind == REVOKED:
            return IDENTITY_UNKNOWN, None
        identities = {PackIdentity(channel, version, pack_id)}
        for kind, channel, version, pack_id in self._installs_between(
            screen, a_time, b_time
        ):
            if kind == UNKNOWN_INSTALL:
                return IDENTITY_UNKNOWN, None
            identities.add(PackIdentity(channel, version, pack_id))
        if len(identities) == 1:
            return ATTRIBUTED, next(iter(identities))
        return CROSS_VERSION, None

    def _rebuild_install_attribution(self, install_plan: list[dict]) -> None:
        changed: dict[str, set[int]] = {}
        for item in install_plan:
            if item["changed"]:
                changed.setdefault(item["screen"], set()).add(
                    item["record"].time_ms
                )
        for screen, times in changed.items():
            for time_ms in sorted(times):
                # An installation record only changes the effective state
                # from its own instant until the next effective installation
                # record.  Only segments overlapping that half-open period
                # can change attribution -- including segments whose two
                # endpoints both sit outside it but whose interior crosses
                # it.
                limit = self._next_install_time(screen, time_ms)
                rows = self._conn.execute(
                    "SELECT screen, round_id, facility, lang, ctx, a_time, "
                    "b_time FROM segment WHERE screen=? AND b_time > ? "
                    "AND a_time < ?",
                    (screen, time_ms, limit),
                ).fetchall()
                self._segments_touched += len(rows)
                for row in rows:
                    a_time, b_time = row[5], row[6]
                    kind, identity = self._attribute(screen, a_time, b_time)
                    self._conn.execute(
                        "UPDATE segment SET attrib_kind=?, channel=?, "
                        "version=?, pack_id=?, reason=? WHERE screen=? AND "
                        "round_id=? AND facility=? AND lang=? AND ctx=? AND "
                        "a_time=? AND b_time=?",
                        (
                            kind,
                            None if identity is None else identity.channel,
                            None if identity is None else identity.version,
                            None if identity is None else identity.pack_id,
                            None if kind == ATTRIBUTED else kind,
                            row[0], row[1], row[2], row[3], row[4],
                            a_time, b_time,
                        ),
                    )

    def _insert_segment(self, series, a_time, b_time, count, kind,
                        identity) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO segment (screen, round_id, facility, "
            "lang, ctx, a_time, b_time, count, attrib_kind, channel, "
            "version, pack_id, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *series,
                a_time,
                b_time,
                count,
                kind,
                None if identity is None else identity.channel,
                None if identity is None else identity.version,
                None if identity is None else identity.pack_id,
                None if kind == ATTRIBUTED else kind,
            ),
        )

    # ---------------------------------------------------------------- report

    def report(
        self, start_ms: int = 0, end_ms: int = _MAX_MS
    ) -> dict:
        """Reconcile segments whose *right reading time* lies in
        ``[start_ms, end_ms)``.

        Increments are never split by duration.  Zero increments are omitted
        from the report (they still shaped the interval partition).  The
        whole report observes one committed state.
        """

        _require_int(start_ms, "range start_ms")
        _require_int(end_ms, "range end_ms")
        if end_ms < start_ms:
            raise LedgerError("range end is before its start")

        conn = self._conn
        conn.execute("BEGIN")
        try:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM commit_log"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT screen, round_id, facility, lang, ctx, a_time, "
                "b_time, count, attrib_kind, channel, version, pack_id, "
                "reason FROM segment WHERE b_time >= ? AND b_time < ? "
                "AND count != 0 ORDER BY screen, facility, lang, ctx, "
                "round_id, a_time",
                (start_ms, end_ms),
            ).fetchall()
        finally:
            conn.commit()

        attributed: dict[tuple, dict] = {}
        unattributed: dict[tuple, dict] = {}
        increments = []
        attributed_total = 0
        unattributed_total = 0

        for (screen, round_id, facility, lang, ctx, a_time, b_time, count,
             kind, channel, version, pack_id, reason) in rows:
            entry = {
                "screen": screen,
                "round_id": round_id,
                "facility": facility,
                "language": lang,
                "context": ctx,
                "a_time": a_time,
                "b_time": b_time,
                "count": count,
            }
            if kind == ATTRIBUTED:
                identity = PackIdentity(channel, version, pack_id)
                key = (screen, facility, lang, ctx) + identity.key()
                bucket = attributed.setdefault(key, {
                    "screen": screen,
                    "facility": facility,
                    "language": lang,
                    "context": ctx,
                    **identity.as_dict(),
                    "count": 0,
                })
                bucket["count"] += count
                attributed_total += count
                entry.update(identity.as_dict())
                entry["kind"] = ATTRIBUTED
            else:
                key = (screen, facility, lang, ctx, kind)
                bucket = unattributed.setdefault(key, {
                    "screen": screen,
                    "facility": facility,
                    "language": lang,
                    "context": ctx,
                    "reason": kind,
                    "count": 0,
                })
                bucket["count"] += count
                unattributed_total += count
                entry["kind"] = kind
                entry["reason"] = kind
            increments.append(entry)

        return {
            "state_seq": ordinal,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "attributed": sorted(
                attributed.values(),
                key=lambda r: (r["screen"], r["facility"],
                               r["language"], r["context"],
                               r["channel"], r["version"], r["pack_id"]),
            ),
            "unattributed": sorted(
                unattributed.values(),
                key=lambda r: (r["screen"], r["facility"],
                               r["language"], r["context"], r["reason"]),
            ),
            "increments": increments,
            "attributed_total": attributed_total,
            "unattributed_total": unattributed_total,
            "total": attributed_total + unattributed_total,
        }

    # ---------------------------------------------------------- introspection

    def effective_installs(self, screen: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT time_ms, kind, channel, version, pack_id, revision "
            "FROM install_record WHERE screen=? AND kind != ? "
            "ORDER BY time_ms",
            (screen, REVOKED),
        ).fetchall()
        result = []
        for time_ms, kind, channel, version, pack_id, revision in rows:
            item = {
                "screen": screen,
                "time_ms": time_ms,
                "revision": revision,
                "kind": kind,
            }
            if kind == INSTALLED:
                item.update(PackIdentity(channel, version, pack_id).as_dict())
            result.append(item)
        return result

    def effective_readings(
        self,
        screen: "str | None" = None,
        round_id: "str | None" = None,
    ) -> list[dict]:
        clauses = ["kind = ?"]
        params: list = [CUMULATIVE]
        if screen is not None:
            clauses.append("screen = ?")
            params.append(screen)
        if round_id is not None:
            clauses.append("round_id = ?")
            params.append(round_id)
        rows = self._conn.execute(
            "SELECT screen, round_id, facility, lang, ctx, time_ms, count, "
            "revision FROM reading_record WHERE " + " AND ".join(clauses)
            + " ORDER BY screen, round_id, facility, lang, ctx, time_ms",
            params,
        ).fetchall()
        return [
            {
                "screen": screen_,
                "round_id": round_,
                "facility": facility,
                "language": lang,
                "context": ctx,
                "time_ms": time_ms,
                "count": count,
                "revision": revision,
            }
            for (screen_, round_, facility, lang, ctx, time_ms, count,
                 revision) in rows
        ]

    def segments(self, screen: "str | None" = None) -> list[dict]:
        if screen is None:
            rows = self._conn.execute(
                "SELECT screen, round_id, facility, lang, ctx, a_time, "
                "b_time, count, attrib_kind, channel, version, pack_id, "
                "reason FROM segment ORDER BY screen, round_id, facility, "
                "lang, ctx, a_time"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT screen, round_id, facility, lang, ctx, a_time, "
                "b_time, count, attrib_kind, channel, version, pack_id, "
                "reason FROM segment WHERE screen=? ORDER BY round_id, "
                "facility, lang, ctx, a_time",
                (screen,),
            ).fetchall()
        return [
            {
                "screen": row[0],
                "round_id": row[1],
                "facility": row[2],
                "language": row[3],
                "context": row[4],
                "a_time": row[5],
                "b_time": row[6],
                "count": row[7],
                "kind": row[8],
                "channel": row[9],
                "version": row[10],
                "pack_id": row[11],
                "reason": row[12],
            }
            for row in rows
        ]

    def commits(self) -> list[dict]:
        return [
            {
                "seq": seq,
                "applied_at": applied_at,
                "installs_written": installs,
                "readings_written": readings,
                "segments_touched": segments,
            }
            for seq, applied_at, installs, readings, segments in
            self._conn.execute(
                "SELECT seq, applied_at, installs_written, "
                "readings_written, segments_touched FROM commit_log "
                "ORDER BY seq"
            )
        ]


# ------------------------------------------------------------------ helpers


def _reading_key(rec: Reading) -> tuple:
    return (
        rec.screen, rec.round_id, rec.facility, rec.language, rec.context,
        rec.time_ms,
    )


def _series_of(rec: Reading) -> tuple:
    return (
        rec.screen, rec.round_id, rec.facility, rec.language, rec.context,
    )



def _install_content(rec: InstallRecord):
    return (rec.kind, None if rec.identity is None else rec.identity.key())


def _reading_content(rec: Reading):
    return (rec.kind, rec.count if rec.kind == CUMULATIVE else None)


def _merge_pending(pending: dict, key, rec, what: str) -> None:
    """Keep only the highest revision per key; equal revisions must agree."""

    previous = pending.get(key)
    if previous is None:
        pending[key] = rec
        return
    if rec.revision < previous.revision:
        return
    if rec.revision == previous.revision:
        if what == "installation":
            same = _install_content(rec) == _install_content(previous)
        else:
            same = _reading_content(rec) == _reading_content(previous)
        if not same:
            raise LedgerError(
                f"contradictory {what} at revision {rec.revision} for {key}"
            )
        return
    pending[key] = rec
