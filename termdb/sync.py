"""Offline word-pack sync: publisher side, device side and local interfaces.

The screens this models are sometimes only briefly networked.  A device keeps
a *word pack* on local disk, keeps serving the last fully installed pack while
offline, and after reconnecting resumes the very same download rather than
starting over.  A new pack replaces the old one only after every page has been
verified complete.

Publisher (release end, simulated locally)
------------------------------------------
* :meth:`OfflinePublisher.publish` takes the channel and the requested
  ``(facility, language, context)`` triples and reads *one* committed state of
  the terminology store.  Unknown facilities or coordinates reject the whole
  release.  A known coordinate that cannot currently supply text (``conflict``,
  ``clear``, ``blocked``, ``no_available``) is simply not part of the release
  -- the publisher never invents wording.
* Every successful release creates the channel's next monotonically numbered
  content version.  A version carries supplied display text only: no source
  id, no review flags, no evidence trace.
* A release never rewrites an already published version.  A rejected release
  leaves the channel, its latest pointer and every existing version's
  snapshotted data untouched.
* Only the three most recent successful versions stay syncable per channel.
  Later terminology corrections never alter those snapshots.
* A device naming a base version that is not retained (or one the publisher is
  explicitly told it cannot identify) is told to *renegotiate*; it gets a full
  pack, never a page from one download stitched to a page from another.

Device (screen end, simulated locally)
--------------------------------------
* :class:`ScreenDevice` binds one channel, persists its installed pack and its
  in-progress download journal under one directory, and resumes that journal
  after reopen without re-requesting pages it already holds.
* A download's identity is derived from its manifest: channel, mode, base,
  target, per-page hashes and whole-pack digest.  Repeated ``negotiate`` calls
  for the same download produce the same identity and byte-identical pages.
* Pages are a pure function of ``(channel, mode, base, target)`` and an
  immutable version snapshot: the same page number always means the same
  coordinates, even if the publisher reopens or newer versions are published.
* Installation is atomic: the complete new pack (identity + content) is
  written to one file and renamed into place, so no interrupt can expose a
  half-written or half-deleted set.  An incomplete, corrupted, contradictory
  or foreign download installs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from .model import SUPPLIED, Coord

#: Maximum number of coordinate entries per page.
PAGE_LIMIT = 128
#: How many successful content versions stay syncable per channel.
KEEP_VERSIONS = 3
#: Pack format identifier.
DEFAULT_FORMAT = "termdb-wordpack"
DEVICE_DB = "device.db"
PACK_FILE = "pack.json"


# ---------------------------------------------------------------- utilities


def canonical(value) -> bytes:
    """Deterministic JSON bytes used for hashing and for stored manifests."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncError(ValueError):
    """A sync protocol violation; nothing is installed or published."""


class RenegotiateError(SyncError):
    """The requested base/download is unavailable; start a new negotiation.

    This is deliberately distinct from a corrupt page: the device must not
    stitch pages from different downloads together.
    """


class TermStoreBusy(SyncError):
    """A release attempted while the terminology store was mid-transaction."""


# ------------------------------------------------------- coordinate helpers


def _pack_key(facility: str, coord: Coord) -> str:
    return f"{facility}\x00{coord.key()}"


def _split_key(key: str) -> tuple[str, str]:
    facility, coord_key = key.split("\x00", 1)
    return facility, coord_key


def _coord_sort_key(coord_key: str) -> tuple[str, str]:
    lang, ctx = coord_key.split("/", 1)
    return lang, ctx


def _sorted_items(entries: dict[str, str]) -> list[dict]:
    ordered = sorted(
        entries.items(),
        key=lambda kv: (
            _split_key(kv[0])[0],
            _coord_sort_key(_split_key(kv[0])[1]),
        ),
    )
    return [
        {
            "facility": _split_key(key)[0],
            "coord": _split_key(key)[1],
            "text": text,
        }
        for key, text in ordered
    ]


def _unpack_items(items: list[dict]) -> dict[str, str]:
    content: dict[str, str] = {}
    for item in items:
        key = _pack_key(item["facility"], Coord.parse(item["coord"]))
        content[key] = item["text"]
    return content


def _chunk(items: list[dict]) -> list[list[dict]]:
    return [
        items[start:start + PAGE_LIMIT]
        for start in range(0, len(items), PAGE_LIMIT)
    ]


def pack_identity(channel: str, version: int, entries: dict[str, str]) -> str:
    """Content identity of one published version."""

    return digest(
        {
            "format": DEFAULT_FORMAT,
            "channel": channel,
            "version": version,
            "entries": entries,
        }
    )


class PackManifest:
    """A fixed download manifest; its hash is the download identity."""

    @staticmethod
    def build(
        *,
        channel: str,
        mode: str,
        base: "int | None",
        target: int,
        base_pack_id: "str | None",
        target_pack_id: str,
        pages: list[list[dict]],
    ) -> dict:
        return {
            "format": DEFAULT_FORMAT,
            "channel": channel,
            "mode": mode,
            "base": base,
            "target": target,
            "base_pack_id": base_pack_id,
            "target_pack_id": target_pack_id,
            "pages": [digest(page) for page in pages],
            "digest": digest(pages),
            "page_count": len(pages),
        }

    @staticmethod
    def download_id(manifest: dict) -> str:
        # The identity hashes the manifest proper; the transport-level
        # ``download`` field is not part of it.
        return digest({k: v for k, v in manifest.items() if k != "download"})


# ------------------------------------------------------------------ publisher


PUBLISHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS pack_channel (
    channel TEXT PRIMARY KEY,
    latest  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pack_version (
    channel      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    pack_id      TEXT NOT NULL,
    facilities   TEXT NOT NULL,
    coordinates  TEXT NOT NULL,
    entries      TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (channel, version)
);
CREATE TABLE IF NOT EXISTS pack_release_attempt (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    request      TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    error        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pack_download (
    download_id    TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    mode           TEXT NOT NULL,
    base_version   INTEGER,
    base_pack_id   TEXT,
    target_version INTEGER NOT NULL,
    target_pack_id TEXT NOT NULL,
    digest         TEXT NOT NULL,
    page_hashes    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pack_download_page (
    download_id TEXT NOT NULL,
    page        INTEGER NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (download_id, page)
);
"""


class OfflinePublisher:
    """Release end of the offline word-pack protocol.

    Reads current wording from a :class:`termdb.store.TermStore` and never
    writes to it.  Its own channel snapshots live in a separate SQLite file.
    """

    def __init__(self, path: str, store):
        self.path = path
        self.store = store
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(PUBLISHER_SCHEMA)
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    # -- release ------------------------------------------------------------

    def publish(
        self,
        channel: str,
        coordinates,
        *,
        format_id: str = DEFAULT_FORMAT,
    ) -> dict:
        """Publish one content version for ``channel`` from the live store.

        ``coordinates`` is a set of ``(facility, language, context)`` triples
        -- the facilities and targets this release should push.  Returns the
        new version's metadata.  On any validation failure a rejected attempt
        is recorded and :class:`SyncError` is raised; neither the channel
        pointer nor any existing version data changes.
        """

        pairs = self._normalize_pairs(coordinates)
        facilities = sorted({facility for facility, _ in pairs})
        coords = sorted({coord for _, coord in pairs}, key=Coord.key)
        request = {
            "pairs": [[facility, coord.key()] for facility, coord in pairs],
            "format": format_id,
        }

        try:
            self._validate_request(channel, pairs, format_id)
            entries = self._snapshot_state(pairs)
            version, pack_id = self._commit_release(
                channel, facilities, coords, entries, request
            )
        except SyncError as exc:
            self._record_attempt(channel, "rejected", request, str(exc))
            raise

        return {
            "channel": channel,
            "version": version,
            "pack_id": pack_id,
            "facilities": facilities,
            "coordinates": [c.key() for c in coords],
            "entries": len(entries),
            "latest": version,
        }

    @staticmethod
    def _normalize_pairs(coordinates) -> list[tuple[str, Coord]]:
        """Normalize the release triple list into sorted unique pairs."""

        pairs: set[tuple[str, Coord]] = set()
        for item in coordinates:
            if not isinstance(item, (tuple, list)) or len(item) != 3:
                raise SyncError(
                    "release coordinates must be "
                    "(facility, language, context) triples"
                )
            facility, lang, ctx = item
            if not all(isinstance(part, str) and part
                       for part in (facility, lang, ctx)):
                raise SyncError(
                    "release triple parts must be non-empty strings"
                )
            pairs.add((facility, Coord(lang, ctx)))
        return sorted(pairs, key=lambda pair: (pair[0], pair[1].key()))

    @staticmethod
    def _validate_request(channel, pairs, format_id):
        if not isinstance(channel, str) or not channel:
            raise SyncError("channel must be a non-empty string")
        if format_id != DEFAULT_FORMAT:
            raise SyncError(f"unsupported pack format: {format_id!r}")
        if not pairs:
            raise SyncError("release must name at least one coordinate")

    def _snapshot_state(self, pairs) -> dict[str, str]:
        """Read every requested pair from one committed store state."""

        if self.store.in_transaction:
            raise TermStoreBusy(
                "a terminology batch is being applied; release rejected"
            )
        token_before = self.store.state_seq
        # Unknown facilities/coordinates reject the release outright, before
        # any version is created.  Only the coordinate's *target* identity is
        # validated here; the facility's own candidates are never expanded.
        for facility, coord in pairs:
            if facility not in self.store.policies:
                raise SyncError(f"unknown facility: {facility}")
            if coord not in self.store.candidates:
                raise SyncError(f"unknown coordinate: {coord.key()}")
        entries: dict[str, str] = {}
        for facility, coord in pairs:
            result = self.store.query(facility, coord)
            if result["outcome"] == SUPPLIED:
                entries[_pack_key(facility, coord)] = result["text"]
            # explicit_clear / conflict / blocked / no_available: the target
            # cannot currently supply a display text, so it is not part of
            # this release.  A conflict never gets an arbitrary winner.
        token_after = self.store.state_seq
        if token_before != token_after:
            raise TermStoreBusy(
                "terminology store changed while the release was being read; "
                "release rejected"
            )
        # An empty entry set is a legitimate release: every requested
        # coordinate stopped supplying, so the new version drops them.
        return entries

    def _commit_release(self, channel, facilities, coords, entries, request):
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT latest FROM pack_channel WHERE channel=?", (channel,)
            ).fetchone()
            version = 1 if row is None else row[0] + 1
            pack_id = pack_identity(channel, version, entries)
            conn.execute(
                "INSERT INTO pack_version (channel, version, pack_id, "
                "facilities, coordinates, entries, published_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    channel,
                    version,
                    pack_id,
                    json.dumps(facilities, ensure_ascii=False),
                    json.dumps([c.key() for c in coords], ensure_ascii=False),
                    json.dumps(entries, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            conn.execute(
                "INSERT INTO pack_channel (channel, latest) VALUES (?,?) "
                "ON CONFLICT(channel) DO UPDATE SET latest=excluded.latest",
                (channel, version),
            )
            self._prune(channel, version)
            self._record_attempt(channel, "accepted", request, "")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return version, pack_id

    def _prune(self, channel: str, latest: int) -> None:
        cutoff = latest - KEEP_VERSIONS
        self._conn.execute(
            "DELETE FROM pack_version WHERE channel=? AND version <= ?",
            (channel, cutoff),
        )
        # A download is only syncable while every version it needs -- its
        # target and, for a delta, its base -- is retained.  Anything else
        # can only be resumed by renegotiating, so its pages are dropped.
        self._conn.execute(
            "DELETE FROM pack_download_page WHERE download_id IN ("
            "SELECT download_id FROM pack_download WHERE channel=? AND ("
            "target_version <= ? OR "
            "(base_version IS NOT NULL AND base_version <= ?)))",
            (channel, cutoff, cutoff),
        )
        self._conn.execute(
            "DELETE FROM pack_download WHERE channel=? AND ("
            "target_version <= ? OR "
            "(base_version IS NOT NULL AND base_version <= ?))",
            (channel, cutoff, cutoff),
        )

    def _record_attempt(self, channel, status, request, error) -> None:
        self._conn.execute(
            "INSERT INTO pack_release_attempt "
            "(channel, status, request, attempted_at, error) "
            "VALUES (?,?,?,?,?)",
            (
                channel,
                status,
                json.dumps(request, ensure_ascii=False, sort_keys=True),
                _now(),
                error,
            ),
        )

    # -- introspection ------------------------------------------------------

    def latest(self, channel: str) -> int:
        row = self._conn.execute(
            "SELECT latest FROM pack_channel WHERE channel=?", (channel,)
        ).fetchone()
        return 0 if row is None else row[0]

    def retained_versions(self, channel: str) -> list[int]:
        return [
            v for (v,) in self._conn.execute(
                "SELECT version FROM pack_version WHERE channel=? "
                "ORDER BY version",
                (channel,),
            )
        ]

    def is_retained(self, channel: str, version: "int | None") -> bool:
        if version is None:
            return False
        return self._conn.execute(
            "SELECT 1 FROM pack_version WHERE channel=? AND version=?",
            (channel, version),
        ).fetchone() is not None

    def version_meta(self, channel: str, version: int) -> dict:
        row = self._conn.execute(
            "SELECT pack_id, facilities, coordinates, entries, published_at "
            "FROM pack_version WHERE channel=? AND version=?",
            (channel, version),
        ).fetchone()
        if row is None:
            raise RenegotiateError(
                f"version {version} of {channel!r} is not retained"
            )
        return {
            "channel": channel,
            "version": version,
            "pack_id": row[0],
            "facilities": json.loads(row[1]),
            "coordinates": json.loads(row[2]),
            "entries": json.loads(row[3]),
            "published_at": row[4],
        }

    def release_attempts(self) -> list[dict]:
        return [
            {
                "seq": seq,
                "channel": channel,
                "status": status,
                "request": json.loads(request),
                "attempted_at": at,
                "error": error,
            }
            for seq, channel, status, request, at, error in self._conn.execute(
                "SELECT seq, channel, status, request, attempted_at, error "
                "FROM pack_release_attempt ORDER BY seq"
            )
        ]

    # -- download negotiation ----------------------------------------------

    def negotiate(
        self,
        channel: str,
        base_version: "int | None" = None,
        *,
        base_pack_id: "str | None" = None,
        format_id: str = DEFAULT_FORMAT,
        unknown_base: bool = False,
    ) -> dict:
        """Start or resume a download of the channel's latest version.

        ``base_version`` is the pack the device currently has installed, or
        ``None`` for a full pack.  ``base_pack_id`` is the identity the
        device believes that pack has; if it does not match the publisher's
        snapshot the base is not identifiable.  ``unknown_base=True`` models
        a base the publisher cannot identify at all; it is handled exactly
        like an evicted base.  Returns the fixed manifest plus its
        ``download`` identity (or ``mode == 'none'`` when already latest).
        """

        if format_id != DEFAULT_FORMAT:
            raise SyncError(f"unsupported pack format: {format_id!r}")
        latest = self.latest(channel)
        if latest == 0:
            raise SyncError(f"channel {channel!r} has no published version")

        if base_version is not None and (
            unknown_base
            or not self.is_retained(channel, base_version)
            or base_version > latest
        ):
            raise RenegotiateError(
                f"base version {base_version!r} is not identifiable for "
                f"channel {channel!r}; renegotiate a full download"
            )
        if base_version is not None and base_pack_id is not None:
            stored = self.version_meta(channel, base_version)["pack_id"]
            if stored != base_pack_id:
                raise RenegotiateError(
                    f"base version {base_version} has identity {stored[:12]}… "
                    f"on the publisher, not {base_pack_id[:12]}…; renegotiate"
                )

        latest_pack = self.version_meta(channel, latest)["pack_id"]
        if base_version == latest:
            return {
                "format": DEFAULT_FORMAT,
                "channel": channel,
                "mode": "none",
                "base": latest,
                "target": latest,
                "target_pack_id": latest_pack,
                "download": None,
                "page_count": 0,
                "pages": [],
                "digest": None,
            }

        target = latest
        target_meta = self.version_meta(channel, target)
        if base_version is None:
            mode = "full"
            base_pack = None
            entries = target_meta["entries"]
        else:
            mode = "delta"
            base_pack = self.version_meta(channel, base_version)["pack_id"]
            entries = self._delta_entries(channel, base_version, target)

        items = _sorted_items(entries)
        pages = _chunk(items)
        # A full pack always has at least one (possibly empty) page so a
        # download identity always names content.  An empty delta has zero
        # pages and switches the version by manifest alone.
        if mode == "full" and not pages:
            pages = [[]]

        manifest = PackManifest.build(
            channel=channel,
            mode=mode,
            base=base_version,
            target=target,
            base_pack_id=base_pack,
            target_pack_id=target_meta["pack_id"],
            pages=pages,
        )
        download_id = PackManifest.download_id(manifest)
        self._store_download(download_id, manifest, pages)
        return {"download": download_id, **manifest}

    def _delta_entries(self, channel, base, target) -> dict[str, str]:
        old = self.version_meta(channel, base)["entries"]
        new = self.version_meta(channel, target)["entries"]
        delta: dict[str, str] = {}
        for key, text in new.items():
            if old.get(key) != text:
                delta[key] = text
        for key in old:
            if key not in new:
                # An empty text is the delta wire form of "delete this
                # coordinate"; the device removes it on install.
                delta[key] = ""
        return delta

    def _store_download(self, download_id, manifest, pages) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            exists = conn.execute(
                "SELECT 1 FROM pack_download WHERE download_id=?",
                (download_id,),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO pack_download (download_id, channel, mode, "
                    "base_version, base_pack_id, target_version, "
                    "target_pack_id, digest, page_hashes, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        download_id,
                        manifest["channel"],
                        manifest["mode"],
                        manifest["base"],
                        manifest["base_pack_id"],
                        manifest["target"],
                        manifest["target_pack_id"],
                        manifest["digest"],
                        json.dumps(manifest["pages"]),
                        _now(),
                    ),
                )
                for number, items in enumerate(pages):
                    body = {
                        "download": download_id,
                        "manifest": manifest,
                        "page": number,
                        "items": items,
                    }
                    conn.execute(
                        "INSERT INTO pack_download_page "
                        "(download_id, page, body) VALUES (?,?,?)",
                        (download_id, number,
                         json.dumps(body, ensure_ascii=False, sort_keys=True)),
                    )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def download_meta(self, download_id: str) -> dict:
        """Identity of a stored download, without any page content."""

        row = self._conn.execute(
            "SELECT channel, mode, base_version, base_pack_id, "
            "target_version, target_pack_id, digest, page_hashes "
            "FROM pack_download WHERE download_id=?",
            (download_id,),
        ).fetchone()
        if row is None:
            raise RenegotiateError(f"unknown download {download_id!r}")
        if not self.is_retained(row[0], row[4]) or (
            row[2] is not None and not self.is_retained(row[0], row[2])
        ):
            raise RenegotiateError(
                f"download {download_id!r} depends on an evicted version; "
                "renegotiate"
            )
        return {
            "download": download_id,
            "channel": row[0],
            "mode": row[1],
            "base": row[2],
            "base_pack_id": row[3],
            "target": row[4],
            "target_pack_id": row[5],
            "digest": row[6],
            "page_hashes": json.loads(row[7]),
        }

    def fetch_page(self, download_id: str, page: int) -> dict:
        """Serve one immutable page of a fixed download.

        Reopening the publisher or publishing newer versions never changes
        what a page of an existing download contains.  If the download's
        target version has been evicted the publisher says so explicitly
        rather than serving a mix of old and new content.
        """

        meta = self.download_meta(download_id)
        row = self._conn.execute(
            "SELECT body FROM pack_download_page "
            "WHERE download_id=? AND page=?",
            (download_id, page),
        ).fetchone()
        if row is None:
            raise SyncError(
                f"page {page} is outside download {download_id!r} "
                f"(0..{len(meta['page_hashes']) - 1})"
            )
        return json.loads(row[0])


# --------------------------------------------------------------------- device


DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_download (
    download_id    TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    mode           TEXT NOT NULL,
    base_version   INTEGER,
    base_pack_id   TEXT,
    target_version INTEGER NOT NULL,
    target_pack_id TEXT NOT NULL,
    digest         TEXT NOT NULL,
    page_hashes    TEXT NOT NULL,
    page_count     INTEGER NOT NULL,
    started_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dev_page (
    download_id TEXT NOT NULL,
    page        INTEGER NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (download_id, page)
);
CREATE TABLE IF NOT EXISTS dev_completed (
    download_id TEXT PRIMARY KEY,
    target_version INTEGER NOT NULL,
    target_pack_id TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
"""


class ScreenDevice:
    """One screen, bound to one channel, with durable offline state.

    ``directory`` holds ``pack.json`` (the currently installed pack, replaced
    by a single atomic rename) and ``device.db`` (the in-progress download
    journal).  Reopening the device resumes the journal; the pack file is the
    sole source of truth for what is installed and can be read with no
    network at all.
    """

    def __init__(self, directory: str, channel: str, publisher):
        self.directory = directory
        self.channel = channel
        self.publisher = publisher
        self._conn = sqlite3.connect(
            os.path.join(directory, DEVICE_DB), isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(DEVICE_SCHEMA)
        self._drop_stale_journals()

    @classmethod
    def open(cls, directory: str, channel: str, publisher) -> "ScreenDevice":
        os.makedirs(directory, exist_ok=True)
        return cls(directory, channel, publisher)

    def close(self) -> None:
        self._conn.close()

    # -- installed pack -----------------------------------------------------

    @property
    def _pack_path(self) -> str:
        return os.path.join(self.directory, PACK_FILE)

    def installed_version(self) -> "dict | None":
        try:
            with open(self._pack_path, "rb") as fh:
                return json.loads(fh.read())
        except FileNotFoundError:
            return None

    def current_version(self) -> "int | None":
        pack = self.installed_version()
        return None if pack is None else pack["version"]

    def content(self) -> dict:
        pack = self.installed_version()
        return {} if pack is None else dict(pack["content"])

    def lookup(self, facility: str, coord) -> str:
        """Offline wording lookup; no publisher or network involved."""

        pack = self.installed_version()
        if pack is None:
            raise SyncError(
                f"no pack installed for channel {self.channel!r}; run sync() "
                "once while online"
            )
        coord = Coord.parse(coord)
        key = _pack_key(facility, coord)
        try:
            return pack["content"][key]
        except KeyError:
            raise SyncError(
                f"no wording for {facility} / {coord} in installed pack "
                f"v{pack['version']}"
            ) from None

    # -- negotiation --------------------------------------------------------

    def sync(self, *, force_full: bool = False) -> dict:
        """Ask the publisher what to download from the current base.

        The installed version is the base.  If the publisher cannot identify
        that base any more (or ``force_full`` is set) the device renegotiates
        a full pack instead of risking a stitched download.
        """

        base = None if force_full else self.installed_version()
        if base is None:
            manifest = self.publisher.negotiate(self.channel, None)
        else:
            try:
                manifest = self.publisher.negotiate(
                    self.channel, base["version"],
                    base_pack_id=base["pack_id"],
                )
            except RenegotiateError:
                manifest = self.publisher.negotiate(self.channel, None)
        if manifest["mode"] == "none":
            return manifest
        self._ensure_download(manifest)
        return manifest

    def adopt_download(self, manifest: dict) -> str:
        """Bind this device to an existing fixed download manifest.

        Used when a sync session is handed a manifest out of band (for
        example a resumed session on another transport).  The manifest must
        verify against its own identity and the device's channel; pages are
        still checked individually before they are journaled.
        """

        if not isinstance(manifest, dict):
            raise SyncError("manifest is not a mapping")
        download_id = manifest.get("download")
        if not isinstance(download_id, str):
            raise SyncError("manifest has no download identity")
        if PackManifest.download_id(manifest) != download_id:
            raise SyncError("manifest does not hash to its download identity")
        if manifest.get("channel") != self.channel:
            raise SyncError(
                f"manifest belongs to channel {manifest.get('channel')!r}; "
                f"device is bound to {self.channel!r}"
            )
        self._ensure_download(manifest)
        return download_id

    def pending_downloads(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT download_id, channel, mode, base_version, base_pack_id, "
            "target_version, target_pack_id, digest, page_hashes, page_count "
            "FROM dev_download WHERE channel=? ORDER BY rowid",
            (self.channel,),
        ).fetchall()
        return [
            {
                "download": row[0],
                "channel": row[1],
                "mode": row[2],
                "base": row[3],
                "base_pack_id": row[4],
                "target": row[5],
                "target_pack_id": row[6],
                "digest": row[7],
                "page_hashes": json.loads(row[8]),
                "page_count": row[9],
                "received": self._received(row[0]),
                "missing": self.missing_pages(row[0]),
            }
            for row in rows
        ]

    # -- page reception -----------------------------------------------------

    def accept_page(self, page: dict) -> dict:
        """Validate and durably journal one received page.

        The page must belong to a download this device negotiated, carry its
        manifest and page number, and hash to the per-page identity recorded
        at negotiation.  Repeats are idempotent; contradictory, corrupted,
        oversized or foreign pages raise :class:`SyncError` and journal
        nothing.
        """

        if not isinstance(page, dict):
            raise SyncError("received page is not a mapping")
        manifest = page.get("manifest")
        download_id = page.get("download")
        items = page.get("items")
        number = page.get("page")
        if not isinstance(manifest, dict) or not isinstance(download_id, str):
            raise SyncError("received page has no download identity")
        if PackManifest.download_id(manifest) != download_id:
            raise SyncError("page manifest does not match its download identity")
        if manifest.get("channel") != self.channel:
            raise SyncError(
                f"download belongs to channel {manifest.get('channel')!r}; "
                f"device is bound to {self.channel!r}"
            )
        state = self._download(download_id)
        if state is None:
            raise SyncError(
                "page belongs to a download this device never negotiated"
            )
        for field in ("mode", "base", "base_pack_id", "target",
                      "target_pack_id", "digest", "pages", "page_count"):
            if manifest.get(field) != state[field]:
                raise SyncError(
                    f"page manifest field {field!r} contradicts the download "
                    "recorded on this device"
                )
        if not isinstance(number, int) or not (
            0 <= number < state["page_count"]
        ):
            raise SyncError(
                f"page number {number!r} is outside this download "
                f"(0..{state['page_count'] - 1})"
            )
        if not isinstance(items, list) or len(items) > PAGE_LIMIT:
            raise SyncError(
                f"page {number} has an invalid item list "
                f"(limit {PAGE_LIMIT})"
            )
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("facility"), str)
                or not isinstance(item.get("coord"), str)
                or not isinstance(item.get("text"), str)
            ):
                raise SyncError(f"page {number} has an invalid item")
            # A delta expresses "delete this coordinate" as empty text; a
            # full pack only ever carries real display text.
            if not item["text"] and state["mode"] != "delta":
                raise SyncError(f"page {number} has an invalid item")
            Coord.parse(item["coord"])
            if not item["facility"]:
                raise SyncError(f"page {number} has an invalid item")
        if digest(items) != state["page_hashes"][number]:
            raise SyncError(f"page {number} content does not match its hash")

        previous = self._conn.execute(
            "SELECT body FROM dev_page WHERE download_id=? AND page=?",
            (download_id, number),
        ).fetchone()
        if previous is not None:
            if json.loads(previous[0]) != items:
                raise SyncError(
                    f"page {number} contradicts the page already received; "
                    "refusing to mix content"
                )
            return {
                "status": "duplicate",
                "page": number,
                "complete": self._is_complete(state),
            }

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO dev_page (download_id, page, body) "
                "VALUES (?,?,?)",
                (
                    download_id,
                    number,
                    json.dumps(items, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.execute(
                "DELETE FROM dev_completed WHERE download_id=?", (download_id,)
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return {
            "status": "accepted",
            "page": number,
            "complete": self._is_complete(self._download(download_id)),
        }

    def missing_pages(self, download_id: str) -> list[int]:
        state = self._download(download_id)
        if state is None:
            raise SyncError(f"unknown download {download_id!r}")
        received = set(self._received(download_id))
        return [n for n in range(state["page_count"]) if n not in received]

    def complete(self, download_id: str) -> bool:
        state = self._download(download_id)
        if state is None:
            raise SyncError(f"unknown download {download_id!r}")
        return self._is_complete(state)

    # -- installation -------------------------------------------------------

    def install(self, download_id: str) -> dict:
        """Atomically install a fully verified download (or report a no-op).

        Nothing on disk changes unless every page is present, matches its
        recorded hash, and the assembled pack matches the target version's
        content identity.  A delta is applied only when the installed version
        is exactly its base; a late download never rolls the device back.  A
        retry of an already-installed pack is a no-op.
        """

        state = self._download(download_id)
        if state is None:
            if self._was_installed(download_id):
                # A clean retry of an already installed pack: no second
                # change of any kind.
                installed = self.installed_version()
                return {
                    "status": "noop",
                    "download": download_id,
                    "version": None if installed is None
                    else installed["version"],
                    "pack_id": None if installed is None
                    else installed["pack_id"],
                    "previous_version": None,
                    "entries": 0,
                }
            raise SyncError(f"unknown download {download_id!r}")
        missing = self.missing_pages(download_id)
        if missing:
            raise SyncError(
                f"download is incomplete; missing pages {missing!r}"
            )
        pages = [
            json.loads(body) for (_, body) in self._conn.execute(
                "SELECT page, body FROM dev_page WHERE download_id=? "
                "ORDER BY page",
                (download_id,),
            )
        ]
        if len(pages) != state["page_count"]:
            raise SyncError("journal page count does not match manifest")
        for number, items in enumerate(pages):
            if digest(items) != state["page_hashes"][number]:
                raise SyncError(f"journaled page {number} is corrupted")
        if digest(pages) != state["digest"]:
            raise SyncError("assembled download digest does not match")

        installed = self.installed_version()
        current = None if installed is None else installed["version"]

        if current is not None and state["target"] <= current:
            # A late download (or an already-installed one) must never roll
            # the device back or change it a second time.
            self._finish(download_id, state)
            return self._install_result("noop", state, installed, current)

        if state["mode"] == "full":
            merged: dict[str, str] = {}
            for items in pages:
                for key, text in _unpack_items(items).items():
                    if key in merged and merged[key] != text:
                        raise SyncError(
                            "pack pages disagree about the same coordinate"
                        )
                    merged[key] = text
            content = merged
            result = "replaced"
        else:
            if current != state["base"]:
                raise SyncError(
                    f"delta base v{state['base']} does not match installed "
                    f"v{current}; renegotiate"
                )
            if installed["pack_id"] != state["base_pack_id"]:
                raise SyncError(
                    "installed pack identity does not match the delta base; "
                    "renegotiate"
                )
            content = dict(installed["content"])
            for items in pages:
                for key, text in _unpack_items(items).items():
                    if text == "":
                        content.pop(key, None)
                    else:
                        content[key] = text
            result = "applied"

        if pack_identity(self.channel, state["target"], content) != state[
            "target_pack_id"
        ]:
            raise SyncError(
                "assembled pack does not match the target version identity"
            )

        self._write_pack(
            version=state["target"],
            pack_id=state["target_pack_id"],
            content=content,
        )
        self._finish(download_id, state)
        return self._install_result(result, state, installed, current)

    def _write_pack(self, *, version, pack_id, content) -> None:
        """Write the whole pack to one file, then atomically rename it.

        Because identity, content and version live in a single file, an
        interruption leaves either the complete previous pack or the
        complete new pack -- never a partial mix of writes and deletions.
        """

        body = canonical(
            {
                "channel": self.channel,
                "version": version,
                "pack_id": pack_id,
                "installed_at": _now(),
                "content": {key: content[key] for key in sorted(content)},
            }
        )
        target = self._pack_path
        temporary = target + ".new"
        with open(temporary, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
        _fsync_dir(self.directory)

    def _finish(self, download_id: str, state: dict) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO dev_completed "
                "(download_id, target_version, target_pack_id, finished_at) "
                "VALUES (?,?,?,?)",
                (download_id, state["target"], state["target_pack_id"],
                 _now()),
            )
            self._conn.execute(
                "DELETE FROM dev_page WHERE download_id=?", (download_id,)
            )
            self._conn.execute(
                "DELETE FROM dev_download WHERE download_id=?", (download_id,)
            )
            # Older "already installed" markers can never be retried usefully
            # (their target is below the installed pack), so keep only the
            # newest few to bound local growth.
            self._conn.execute(
                "DELETE FROM dev_completed WHERE target_version <= ? "
                "AND download_id != ?",
                (state["target"] - KEEP_VERSIONS, download_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _was_installed(self, download_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM dev_completed WHERE download_id=?", (download_id,)
        ).fetchone() is not None

    def _install_result(self, status, state, installed, current):
        return {
            "status": status,
            "version": state["target"],
            "pack_id": state["target_pack_id"],
            "previous_version": current,
            "entries": 0 if installed is None else len(installed["content"]),
        }

    # -- internals ----------------------------------------------------------

    def _download(self, download_id: str) -> "dict | None":
        row = self._conn.execute(
            "SELECT channel, mode, base_version, base_pack_id, "
            "target_version, target_pack_id, digest, page_hashes, page_count "
            "FROM dev_download WHERE download_id=?",
            (download_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "download": download_id,
            "channel": row[0],
            "mode": row[1],
            "base": row[2],
            "base_pack_id": row[3],
            "target": row[4],
            "target_pack_id": row[5],
            "digest": row[6],
            "page_hashes": json.loads(row[7]),
            "pages": json.loads(row[7]),
            "page_count": row[8],
        }

    def _ensure_download(self, manifest: dict) -> None:
        download_id = manifest["download"]
        existing = self._download(download_id)
        if existing is not None:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO dev_download (download_id, channel, mode, "
                "base_version, base_pack_id, target_version, target_pack_id, "
                "digest, page_hashes, page_count, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    download_id,
                    manifest["channel"],
                    manifest["mode"],
                    manifest["base"],
                    manifest["base_pack_id"],
                    manifest["target"],
                    manifest["target_pack_id"],
                    manifest["digest"],
                    json.dumps(manifest["pages"]),
                    manifest["page_count"],
                    _now(),
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _received(self, download_id: str) -> list[int]:
        return [
            n for (n,) in self._conn.execute(
                "SELECT page FROM dev_page WHERE download_id=? ORDER BY page",
                (download_id,),
            )
        ]

    def _is_complete(self, state: dict) -> bool:
        return set(self._received(state["download"])) == set(
            range(state["page_count"])
        )

    def _drop_stale_journals(self) -> None:
        """After reopen, forget journals superseded by the installed pack."""

        installed = self.current_version()
        if installed is None:
            return
        self._conn.execute(
            "DELETE FROM dev_page WHERE download_id IN ("
            "SELECT download_id FROM dev_download "
            "WHERE channel=? AND target_version <= ?)",
            (self.channel, installed),
        )
        self._conn.execute(
            "DELETE FROM dev_download WHERE channel=? AND target_version <= ?",
            (self.channel, installed),
        )


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------- demo


def run_sync_demo(directory: str | None = None) -> str:
    """Reproducible offline-sync walkthrough using local interfaces only.

    Prints each step and returns the artifact directory (a temporary one when
    the caller does not supply a directory).
    """

    import tempfile

    from .model import Batch, Entry, EntryCorrection, Policy, REVIEWED, TEXT
    from .store import TermStore

    coords = [Coord("en", "tech"), Coord("en", "doc"),
              Coord("zh", "tech"), Coord("zh", "doc")]
    candidates = {
        coords[0]: [coords[0], coords[1]],
        coords[1]: [coords[1], coords[0]],
        coords[2]: [coords[2], coords[0]],
        coords[3]: [coords[3], coords[1]],
    }
    facilities = ["pump-hall", "gate"]
    # The release names every facility/target triple it wants to push.
    release = [
        (facility, coord.lang, coord.ctx)
        for facility in facilities
        for coord in coords
    ]
    own_tmp = None
    if directory is None:
        own_tmp = tempfile.TemporaryDirectory()
        directory = own_tmp.name
    screen_dir = os.path.join(directory, "screen")
    screen2_dir = os.path.join(directory, "screen-2")

    def show(label, result):
        print(f"  {label}: {result}")

    store = TermStore.bootstrap(
        os.path.join(directory, "terms.db"),
        candidates,
        {f: Policy(f, False, False, frozenset()) for f in facilities},
        [
            Entry("pump-hall", coords[0], "s1", TEXT, "pump", False,
                  REVIEWED),
            Entry("pump-hall", coords[2], "s2", TEXT, "泵", False, REVIEWED),
            Entry("gate", coords[0], "s3", TEXT, "gate", False, REVIEWED),
            Entry("gate", coords[2], "s4", TEXT, "闸门", False, REVIEWED),
        ],
    )
    pub = OfflinePublisher(os.path.join(directory, "pub.db"), store)
    dev = ScreenDevice.open(screen_dir, "north-wing", pub)
    screen2 = None
    try:
        print("1. publish v1, then the screen drops offline mid-download")
        pub.publish("north-wing", release)
        start = dev.sync()
        show("download", f"{start['mode']}, {start['page_count']} page(s), "
                         f"identity {start['download'][:12]}...")
        print("   network drops before the first page arrives")

        print("2. reconnect: the very same download is resumed")
        same = dev.sync()
        assert same["download"] == start["download"]
        _receive_all(dev, same)
        dev.install(same["download"])
        show("installed", f"v{dev.current_version()}")
        show("offline en/tech", dev.lookup("pump-hall", coords[0]))

        print("3. correct wording; published v1 pages stay byte-identical")
        v1_page = pub.fetch_page(start["download"], 0)
        for owner, corrections in [
            ("pump-hall", [
                EntryCorrection("withdraw", "pump-hall", coords[0], "s1"),
                EntryCorrection("withdraw", "pump-hall", coords[2], "s2"),
                EntryCorrection("set_body", "pump-hall", coords[0], "s5",
                                text="pump-v2", is_public=False,
                                review=REVIEWED),
                EntryCorrection("set_body", "pump-hall", coords[2], "s6",
                                text="泵-v2", is_public=False,
                                review=REVIEWED),
            ]),
            ("gate", [
                EntryCorrection("withdraw", "gate", coords[0], "s3"),
                EntryCorrection("withdraw", "gate", coords[2], "s4"),
                EntryCorrection("set_body", "gate", coords[0], "s7",
                                text="gate-v2", is_public=False,
                                review=REVIEWED),
                EntryCorrection("set_body", "gate", coords[2], "s8",
                                text="闸门-v2", is_public=False,
                                review=REVIEWED),
            ]),
        ]:
            store.apply_batch(Batch(corrections))
        pub.publish("north-wing", release)
        delta = dev.sync()
        show("v2 download", f"{delta['mode']}, "
                            f"{delta['page_count']} page(s)")
        _receive_all(dev, delta)
        applied = dev.install(delta["download"])
        show("install", f"{applied['status']} v{applied['version']}")
        show("offline en/tech", dev.lookup("pump-hall", coords[0]))
        show("v1 page 0 unchanged",
             pub.fetch_page(start["download"], 0) == v1_page)

        print("4. publish v3..v5; only the latest three stay syncable")
        for _ in range(3):
            next_version = pub.latest("north-wing") + 1
            store.apply_batch(Batch([
                EntryCorrection("set_body", "gate", coords[2], "s9",
                                text=f"闸门-v{next_version}",
                                is_public=False, review=REVIEWED)
            ]))
            pub.publish("north-wing", release)
        show("retained versions", pub.retained_versions("north-wing"))
        try:
            pub.negotiate("north-wing", 1)
        except RenegotiateError as exc:
            fresh = pub.negotiate("north-wing", None)
            show("base v1", f"explicit renegotiate ({exc}); "
                            f"got a {fresh['mode']} pack")

        print("5. missing/corrupt pages never install; retries resume")
        screen2 = ScreenDevice.open(screen2_dir, "north-wing", pub)
        manifest = screen2.sync()
        show("screen-2 download",
             f"{manifest['mode']}, {manifest['page_count']} page(s)")
        if manifest["page_count"]:
            page = pub.fetch_page(manifest["download"], 0)
            page["items"] = [
                {"facility": "tampered", "coord": "en/tech", "text": "x"}
            ]
            try:
                screen2.accept_page(page)
            except SyncError as exc:
                show("corrupt page", f"rejected ({exc})")
        try:
            screen2.install(manifest["download"])
        except SyncError as exc:
            show("incomplete install", f"refused ({exc})")
        show("screen-2 still empty", screen2.installed_version() is None)
        _receive_all(screen2, manifest)
        show("after retry", screen2.install(manifest["download"])["status"])

        print("6. reopen offline: the installed pack is readable with no "
              "publisher")
        dev.close()
        dev = ScreenDevice.open(screen_dir, "north-wing", publisher=None)
        show("offline v", dev.current_version())
        show("offline zh/tech", dev.lookup("pump-hall", coords[2]))
        print("demo artifacts under:", directory)
        return directory
    finally:
        dev.close()
        if screen2 is not None:
            screen2.close()
        pub.close()
        store.close()
        if own_tmp is not None:
            own_tmp.cleanup()


def _receive_all(device: ScreenDevice, manifest: dict) -> None:
    for number in range(manifest["page_count"]):
        device.accept_page(
            device.publisher.fetch_page(manifest["download"], number)
        )
