"""End-to-end tests for the offline word-pack sync layer.

Covered here:
  * release snapshots (one committed state, current supply only, no source or
    review or evidence data), set semantics, unknown coordinates, immutable
    published versions, rejected releases, three-version retention;
  * download negotiation: none / full / delta, evicted and unknown bases,
    fixed page identity across publisher reopen and newer releases;
  * delta composition rules (add/change write, removed/dropped delete,
    same-text-different-source omitted, empty delta still switches version);
  * the 128-item page limit;
  * device resume after reopen, duplicate pages, missing/corrupt/foreign/
    contradictory pages, pack identity and integrity verification;
  * atomic installation, crash windows, delta base pinning, no downgrade,
    idempotent retries, and offline reads after reopen;
  * the reproducible local demo.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    Entry,
    EntryCorrection,
    Policy,
    REVIEWED,
    TEXT,
    TermStore,
)
from termdb.sync import (  # noqa: E402
    KEEP_VERSIONS,
    PAGE_LIMIT,
    OfflinePublisher,
    RenegotiateError,
    ScreenDevice,
    SyncError,
    TermStoreBusy,
    run_sync_demo,
)

EN, ZH, DE = "en", "zh", "de"
TECH, DOC = "tech", "doc"


def small_world(tmp, entries=None, facilities=("plant-a", "plant-b")):
    """Two facilities, four coordinates, known seed wording."""

    coords = [
        (EN, TECH), (EN, DOC), (ZH, TECH), (ZH, DOC), (DE, TECH),
    ]
    candidates = {
        (EN, TECH): [(EN, TECH), (EN, DOC)],
        (EN, DOC): [(EN, DOC), (EN, TECH)],
        (ZH, TECH): [(ZH, TECH), (EN, TECH)],
        (ZH, DOC): [(ZH, DOC), (EN, DOC)],
        (DE, TECH): [(DE, TECH), (ZH, DOC)],
    }
    from termdb import Coord

    cand = {
        Coord(*target): [Coord(*c) for c in chain]
        for target, chain in candidates.items()
    }
    seed = entries if entries is not None else seed_entries()
    store = TermStore.bootstrap(
        os.path.join(tmp, "terms.db"),
        cand,
        {
            facility: Policy(facility, False, False, frozenset())
            for facility in facilities
        },
        seed,
    )
    pub = OfflinePublisher(os.path.join(tmp, "pub.db"), store)
    return store, pub, cand


def seed_entries():
    from termdb import Coord

    return [
        Entry("plant-a", Coord(EN, TECH), "a-en-tech", TEXT, "pump",
              False, REVIEWED),
        Entry("plant-a", Coord(EN, DOC), "a-en-doc", TEXT, "pump doc",
              False, REVIEWED),
        Entry("plant-a", Coord(ZH, TECH), "a-zh-tech", TEXT, "泵",
              False, REVIEWED),
        Entry("plant-b", Coord(EN, TECH), "b-en-tech", TEXT, "valve",
              False, REVIEWED),
        Entry("plant-b", Coord(ZH, TECH), "b-zh-tech", TEXT, "阀",
              False, REVIEWED),
    ]


def release_all(facilities=("plant-a", "plant-b"), coords=(
    (EN, TECH), (EN, DOC), (ZH, TECH), (ZH, DOC))):
    return [
        (facility, lang, ctx)
        for facility in facilities
        for lang, ctx in coords
    ]


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.store, self.pub, self.candidates = small_world(self.dir)
        self.dev = ScreenDevice.open(
            os.path.join(self.dir, "screen"), "north", self.pub
        )

    def tearDown(self):
        self.dev.close()
        self.pub.close()
        self.store.close()
        self.tmp.cleanup()

    def install_full_latest(self, device=None):
        device = device or self.dev
        manifest = device.sync()
        self.assertNotEqual(manifest["mode"], "none")
        self.receive_all(device, manifest)
        return device.install(manifest["download"]), manifest

    def install(self, device, manifest):
        self.receive_all(device, manifest)
        return device.install(manifest["download"])

    def receive_all(self, device, manifest):
        for number in range(manifest["page_count"]):
            device.accept_page(self.pub.fetch_page(manifest["download"],
                                                   number))


class ReleaseSnapshotTest(SyncTestCase):
    def test_successful_publish_creates_next_version_per_channel(self):
        first = self.pub.publish("north", release_all())
        second = self.pub.publish("north", release_all())
        other = self.pub.publish("south", release_all())
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(other["version"], 1)
        self.assertEqual(self.pub.latest("north"), 2)
        self.assertEqual(self.pub.latest("south"), 1)
        self.assertEqual(
            [row["status"] for row in self.pub.release_attempts()],
            ["accepted", "accepted", "accepted"],
        )

    def test_release_carries_only_supplied_display_text(self):
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        pages = [
            self.pub.fetch_page(manifest["download"], n)
            for n in range(manifest["page_count"])
        ]
        items = [item for page in pages for item in page["items"]]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(set(item), {"facility", "coord", "text"})
            self.assertTrue(item["text"])
        serialized = json.dumps(pages, ensure_ascii=False)
        self.assertNotIn("source_id", serialized)
        self.assertNotIn("review", serialized)
        self.assertNotIn("evidence", serialized)
        self.assertNotIn("a-en-tech", serialized)

    def test_only_currently_supplied_coordinates_are_included(self):
        from termdb import Coord

        # Make one coordinate conflict, one explicit clear, and leave the
        # third release triple (de/tech) out of the request entirely.
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH),
                            "a-clash", text="different", is_public=False,
                            review=REVIEWED),
        ]))
        self.assertEqual(
            self.store.query("plant-a", Coord(EN, TECH))["outcome"],
            "conflict",
        )
        self.store.apply_batch(Batch([
            EntryCorrection("clear", "plant-a", Coord(ZH, TECH), "a-zh-tech",
                            is_public=False, review=REVIEWED),
        ]))
        meta = self.pub.publish("north", release_all())
        entries = self.pub.version_meta("north", meta["version"])["entries"]
        self.assertNotIn("plant-a\x00en/tech", entries)
        self.assertNotIn("plant-a\x00zh/tech", entries)
        # plant-a at en/doc supplies its own text; plant-b en/doc has no
        # record of its own and falls back to its en/tech "valve".
        self.assertIn("plant-a\x00en/doc", entries)
        self.assertEqual(entries["plant-b\x00en/doc"], "valve")
        # de/tech was not in the release list at all.
        self.assertNotIn("plant-a\x00de/tech", entries)

    def test_release_triples_are_a_set(self):
        triples = release_all() + release_all()
        meta = self.pub.publish("north", triples)
        entries = self.pub.version_meta("north", meta["version"])["entries"]
        self.assertEqual(len(entries), len(set(entries)))
        self.assertEqual(meta["entries"], len(entries))

    def test_unknown_coordinate_rejects_release_without_changing_versions(self):
        self.pub.publish("north", release_all())
        before = self.pub.version_meta("north", 1)
        with self.assertRaises(SyncError):
            self.pub.publish(
                "north", release_all() + [("plant-a", "fr", "tech")]
            )
        self.assertEqual(self.pub.latest("north"), 1)
        self.assertEqual(self.pub.version_meta("north", 1), before)
        self.assertEqual(self.pub.retained_versions("north"), [1])
        self.assertEqual(self.pub.release_attempts()[-1]["status"], "rejected")

    def test_unknown_facility_rejects_release(self):
        with self.assertRaises(SyncError):
            self.pub.publish("north", [("plant-z", EN, TECH)])
        self.assertEqual(self.pub.latest("north"), 0)
        self.assertEqual(self.pub.retained_versions("north"), [])

    def test_malformed_triple_rejects_release(self):
        with self.assertRaises(SyncError):
            self.pub.publish("north", [("plant-a", EN)])
        self.assertEqual(self.pub.latest("north"), 0)

    def test_corrections_do_not_rewrite_published_versions(self):
        first = self.pub.publish("north", release_all())
        before = self.pub.version_meta("north", first["version"])
        download = self.pub.negotiate("north", None)
        page_before = self.pub.fetch_page(download["download"], 0)
        from termdb import Coord

        self.store.apply_batch(Batch([
            EntryCorrection("withdraw", "plant-a", Coord(EN, TECH),
                            "a-en-tech"),
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-new",
                            text="pump-v2", is_public=False,
                            review=REVIEWED),
        ]))
        self.assertEqual(
            self.pub.version_meta("north", first["version"]), before
        )
        self.assertEqual(
            self.pub.fetch_page(download["download"], 0), page_before
        )
        # A later release snapshots the corrected state as a new version.
        second = self.pub.publish("north", release_all())
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            self.pub.version_meta("north", 2)["entries"]["plant-a\x00en/tech"],
            "pump-v2",
        )
        self.assertEqual(
            self.pub.version_meta("north", 1)["entries"]["plant-a\x00en/tech"],
            "pump",
        )

    def test_rejected_release_keeps_retention_window(self):
        for _ in range(5):
            self.pub.publish("north", release_all())
        retained = self.pub.retained_versions("north")
        before = {
            version: self.pub.version_meta("north", version)
            for version in retained
        }
        with self.assertRaises(SyncError):
            self.pub.publish("north", [("plant-a", "xx", "yy")])
        self.assertEqual(self.pub.retained_versions("north"), retained)
        for version, meta in before.items():
            self.assertEqual(self.pub.version_meta("north", version), meta)

    def test_only_three_latest_versions_remain(self):
        self.assertEqual(KEEP_VERSIONS, 3)
        for _ in range(5):
            self.pub.publish("north", release_all())
        self.assertEqual(self.pub.retained_versions("north"), [3, 4, 5])
        for evicted in (1, 2):
            with self.assertRaises(RenegotiateError):
                self.pub.version_meta("north", evicted)

    def test_channel_isolation_for_versions_and_downloads(self):
        self.pub.publish("north", release_all())
        self.pub.publish("south", [("plant-b", EN, TECH)])
        self.assertEqual(self.pub.latest("north"), 1)
        self.assertEqual(self.pub.latest("south"), 1)
        self.assertNotEqual(
            self.pub.version_meta("north", 1)["pack_id"],
            self.pub.version_meta("south", 1)["pack_id"],
        )
        south = self.pub.negotiate("south", None)
        with self.assertRaises(SyncError):
            self.dev.accept_page(self.pub.fetch_page(south["download"], 0))

    def test_duplicate_triples_and_ordering_do_not_change_content(self):
        triples = release_all()
        self.pub.publish("north", triples)
        self.pub.publish("north", list(reversed(triples)) + triples)
        # Duplicates collapse and the requested order is irrelevant: the
        # released content, and thus every page, is identical.  The pack id
        # also folds in the version number, so the two versions keep
        # distinct identities.
        self.assertEqual(
            self.pub.version_meta("north", 1)["entries"],
            self.pub.version_meta("north", 2)["entries"],
        )
        full = self.pub.negotiate("north", None)
        self.assertEqual(
            sum(
                len(self.pub.fetch_page(full["download"], n)["items"])
                for n in range(full["page_count"])
            ),
            len(self.pub.version_meta("north", 2)["entries"]),
        )
        # Between the two identical-content versions the net delta is empty,
        # yet the download still switches the device to the newer version.
        delta = self.pub.negotiate("north", 1)
        self.assertEqual(delta["mode"], "delta")
        self.assertEqual(delta["page_count"], 0)

    def test_release_with_no_supplied_coordinate_is_an_empty_version(self):
        from termdb import Coord

        empty = "plant-empty"
        store = TermStore.bootstrap(
            os.path.join(self.dir, "empty.db"),
            dict(self.candidates),
            {empty: Policy(empty, False, False, frozenset())},
            [],
        )
        pub = OfflinePublisher(os.path.join(self.dir, "empty-pub.db"), store)
        try:
            meta = pub.publish(
                "ghost", [(empty, EN, TECH), (empty, ZH, TECH)]
            )
            self.assertEqual(meta["entries"], 0)
            manifest = pub.negotiate("ghost", None)
            self.assertEqual(manifest["page_count"], 1)
            self.assertEqual(
                pub.fetch_page(manifest["download"], 0)["items"], []
            )
            # Installing it on a device yields a valid, empty pack.
            dev = ScreenDevice.open(
                os.path.join(self.dir, "ghost-screen"), "ghost", pub
            )
            try:
                manifest = dev.sync()
                for number in range(manifest["page_count"]):
                    dev.accept_page(
                        pub.fetch_page(manifest["download"], number)
                    )
                applied = dev.install(manifest["download"])
                self.assertEqual(applied["version"], 1)
                self.assertEqual(dev.content(), {})
            finally:
                dev.close()
        finally:
            pub.close()
            store.close()

    def test_release_reads_one_committed_state(self):
        original_query = self.store.query
        calls = {"n": 0}

        def interleaved(facility, target):
            calls["n"] += 1
            if calls["n"] == 2:
                self.store.apply_batch(Batch([
                    EntryCorrection(
                        "set_body", "plant-a", target, "late",
                        text="late-write", is_public=False,
                        review=REVIEWED,
                    )
                ]))
            return original_query(facility, target)

        self.store.query = interleaved
        with self.assertRaises(TermStoreBusy):
            self.pub.publish("north", release_all())
        self.assertEqual(self.pub.latest("north"), 0)
        self.assertEqual(self.pub.retained_versions("north"), [])
        self.assertEqual(self.pub.release_attempts()[-1]["status"], "rejected")
        self.assertEqual(self.store.ledger()[-1]["status"], "accepted")


class NegotiationTest(SyncTestCase):
    def test_already_latest_needs_no_download(self):
        self.pub.publish("north", release_all())
        self.install_full_latest()
        manifest = self.dev.sync()
        self.assertEqual(manifest["mode"], "none")
        self.assertIsNone(manifest["download"])
        self.assertEqual(manifest["page_count"], 0)
        self.assertEqual(self.dev.pending_downloads(), [])

    def test_no_base_gets_full_pack(self):
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        self.assertEqual(manifest["mode"], "full")
        self.assertIsNone(manifest["base"])
        self.assertEqual(manifest["target"], 1)

    def test_retained_base_gets_only_net_delta(self):
        self.pub.publish("north", release_all())
        self.install_full_latest()
        from termdb import Coord

        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-en-tech",
                            text="pump-2", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        self.assertEqual(manifest["mode"], "delta")
        self.assertEqual(manifest["base"], 1)
        self.assertEqual(manifest["target"], 2)
        items = [
            item
            for n in range(manifest["page_count"])
            for item in self.pub.fetch_page(manifest["download"], n)["items"]
        ]
        self.assertEqual(
            [(i["facility"], i["coord"], i["text"]) for i in items],
            [("plant-a", "en/tech", "pump-2")],
        )

    def test_evicted_base_is_told_to_renegotiate(self):
        for _ in range(5):
            self.pub.publish("north", release_all())
        with self.assertRaises(RenegotiateError):
            self.pub.negotiate("north", 1)
        # The device falls back to a full pack rather than stitching pages.
        device = ScreenDevice.open(
            os.path.join(self.dir, "stale"), "north", self.pub
        )
        try:
            manifest = device.sync()
            self.assertEqual(manifest["mode"], "full")
            self.assertIsNone(manifest["base"])
            self.assertEqual(manifest["target"], 5)
        finally:
            device.close()

    def test_unknown_base_and_mismatched_base_identity(self):
        self.pub.publish("north", release_all())
        with self.assertRaises(RenegotiateError):
            self.pub.negotiate("north", 1, unknown_base=True)
        correct = self.pub.version_meta("north", 1)["pack_id"]
        with self.assertRaises(RenegotiateError):
            self.pub.negotiate("north", 1, base_pack_id=correct + "bad")
        # A base newer than the channel is also not identifiable.
        with self.assertRaises(RenegotiateError):
            self.pub.negotiate("north", 9)

    def test_pages_stay_fixed_across_reopen_and_newer_release(self):
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        original = [
            self.pub.fetch_page(manifest["download"], n)
            for n in range(manifest["page_count"])
        ]

        # Reopen the publisher and terminology store at the same paths.
        self.pub.close()
        self.pub = OfflinePublisher(
            os.path.join(self.dir, "pub.db"), self.store
        )
        self.dev.publisher = self.pub
        self.assertEqual(
            [self.pub.fetch_page(manifest["download"], n)
             for n in range(manifest["page_count"])],
            original,
        )

        # A newer release must not mutate the existing download's pages.
        self.pub.publish("north", release_all())
        self.assertEqual(
            [self.pub.fetch_page(manifest["download"], n)
             for n in range(manifest["page_count"])],
            original,
        )
        self.assertEqual(manifest["download"],
                         self.dev.pending_downloads()[0]["download"])

    def test_evicted_target_refuses_pages_explicitly(self):
        self.pub.publish("north", release_all())
        stale = self.pub.negotiate("north", None)
        self.install_full_latest()
        for _ in range(KEEP_VERSIONS + 1):
            self.pub.publish("north", release_all())
        with self.assertRaises(RenegotiateError):
            self.pub.fetch_page(stale["download"], 0)
        fresh = self.pub.negotiate("north", None)
        self.assertNotEqual(fresh["download"], stale["download"])
        self.assertEqual(fresh["target"], self.pub.latest("north"))

    def test_manifest_exposes_verifiable_identity_and_integrity(self):
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        for field in ("download", "digest", "pages", "target_pack_id",
                      "page_count", "channel", "mode", "base", "target"):
            self.assertIn(field, manifest)
        self.assertEqual(len(manifest["pages"]), manifest["page_count"])
        page = self.pub.fetch_page(manifest["download"], 0)
        self.assertEqual(page["manifest"], {
            key: value for key, value in manifest.items()
            if key != "download"
        })
        from termdb.sync import PackManifest

        self.assertEqual(
            PackManifest.download_id(manifest), manifest["download"]
        )


class DeltaRuleTest(SyncTestCase):
    def _publish_and_install(self):
        self.pub.publish("north", release_all())
        return self.install_full_latest()

    def test_same_text_different_source_is_not_sent(self):
        self._publish_and_install()

        from termdb import Coord

        # Replace the source behind the identical final text: output text is
        # unchanged, only evidence differs, so no delta line is needed.
        self.store.apply_batch(Batch([
            EntryCorrection("withdraw", "plant-a", Coord(EN, TECH),
                            "a-en-tech"),
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-alt",
                            text="pump", is_public=True, review=REVIEWED),
        ]))
        result = self.store.query("plant-a", Coord(EN, TECH))
        self.assertEqual(result["text"], "pump")
        self.assertEqual(
            {e["source_id"] for e in result["evidence"]}, {"a-alt"}
        )

        meta = self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        self.assertEqual(manifest["mode"], "delta")
        self.assertEqual(manifest["page_count"], 0)
        applied = self.dev.install(manifest["download"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(self.dev.current_version(), meta["version"])
        self.assertEqual(self.dev.lookup("plant-a", Coord(EN, TECH)), "pump")

    def test_removed_or_unsupplied_coordinate_is_deleted(self):
        self._publish_and_install()
        from termdb import Coord

        # en/doc stops supplying for plant-a, and the whole plant-b/zh/tech
        # triple leaves the release list.
        self.store.apply_batch(Batch([
            EntryCorrection("clear", "plant-a", Coord(EN, DOC), "a-en-doc",
                            is_public=False, review=REVIEWED),
        ]))
        triples = [
            triple for triple in release_all()
            if triple != ("plant-b", ZH, TECH)
        ]
        meta = self.pub.publish("north", triples)
        manifest = self.dev.sync()
        items = [
            item
            for n in range(manifest["page_count"])
            for item in self.pub.fetch_page(manifest["download"], n)["items"]
        ]
        wire = {(i["facility"], i["coord"]): i["text"] for i in items}
        self.assertEqual(wire[("plant-a", "en/doc")], "")
        self.assertEqual(wire[("plant-b", "zh/tech")], "")
        self.receive_all(self.dev, manifest)
        self.dev.install(manifest["download"])
        with self.assertRaises(SyncError):
            self.dev.lookup("plant-a", Coord(EN, DOC))
        with self.assertRaises(SyncError):
            self.dev.lookup("plant-b", Coord(ZH, TECH))
        # Unrelated entries are preserved by the delta.
        self.assertEqual(self.dev.lookup("plant-a", Coord(EN, TECH)), "pump")

    def test_added_coordinate_and_changed_text_are_written(self):
        self._publish_and_install()
        from termdb import Coord

        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-en-tech",
                            text="pump-new", is_public=False,
                            review=REVIEWED),
            EntryCorrection("set_body", "plant-a", Coord(DE, TECH), "a-de",
                            text="pumpe", is_public=False, review=REVIEWED),
        ]))
        triples = release_all() + [("plant-a", DE, TECH)]
        self.pub.publish("north", triples)
        manifest = self.dev.sync()
        self.receive_all(self.dev, manifest)
        self.dev.install(manifest["download"])
        self.assertEqual(
            self.dev.lookup("plant-a", Coord(EN, TECH)), "pump-new"
        )
        self.assertEqual(self.dev.lookup("plant-a", Coord(DE, TECH)), "pumpe")

    def test_full_install_keeps_only_target_coordinates(self):
        from termdb import Coord

        self._publish_and_install()
        self.assertEqual(
            self.dev.lookup("plant-a", Coord(EN, TECH)), "pump"
        )
        # A narrower release replaces the device pack wholesale.
        self.pub.publish("north", [("plant-a", EN, TECH)])
        manifest = self.dev.sync(force_full=True)
        self.assertEqual(manifest["mode"], "full")
        self.receive_all(self.dev, manifest)
        self.dev.install(manifest["download"])
        self.assertEqual(self.dev.content().keys(),
                         {"plant-a\x00en/tech"})

class PageLimitAndResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        from termdb import Coord

        self.coords = [Coord("en", f"c{i}") for i in range(300)]
        candidates = {coord: [coord] for coord in self.coords}
        entries = [
            Entry("big", coord, f"src-{i}", TEXT, f"word-{i}", False,
                  REVIEWED)
            for i, coord in enumerate(self.coords)
        ]
        self.store = TermStore.bootstrap(
            os.path.join(self.dir, "terms.db"),
            candidates,
            {"big": Policy("big", False, False, frozenset())},
            entries,
        )
        self.pub = OfflinePublisher(os.path.join(self.dir, "pub.db"),
                                    self.store)
        self.triples = [("big", c.lang, c.ctx) for c in self.coords]

    def tearDown(self):
        self.pub.close()
        self.store.close()
        self.tmp.cleanup()

    def test_pages_hold_at_most_128_items(self):
        self.pub.publish("north", self.triples)
        device = ScreenDevice.open(
            os.path.join(self.dir, "screen"), "north", self.pub
        )
        try:
            manifest = device.sync()
            self.assertEqual(manifest["page_count"], 3)
            sizes = [
                len(self.pub.fetch_page(manifest["download"], n)["items"])
                for n in range(manifest["page_count"])
            ]
            self.assertEqual(sizes, [128, 128, 44])
            self.assertTrue(all(size <= PAGE_LIMIT for size in sizes))
        finally:
            device.close()

    def test_device_reopen_resumes_without_refetching_received_pages(self):
        self.pub.publish("north", self.triples)
        device_dir = os.path.join(self.dir, "screen")
        device = ScreenDevice.open(device_dir, "north", self.pub)
        manifest = device.sync()
        device.accept_page(self.pub.fetch_page(manifest["download"], 0))
        device.accept_page(self.pub.fetch_page(manifest["download"], 1))
        device.close()

        reopened = ScreenDevice.open(device_dir, "north", self.pub)
        try:
            pending = reopened.pending_downloads()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["download"], manifest["download"])
            self.assertEqual(pending[0]["received"], [0, 1])
            self.assertEqual(pending[0]["missing"], [2])
            # The same negotiation identity comes back, so the device need
            # not re-fetch pages 0 and 1.
            again = reopened.sync()
            self.assertEqual(again["download"], manifest["download"])
            self.assertEqual(
                reopened.missing_pages(manifest["download"]), [2]
            )
            reopened.accept_page(
                self.pub.fetch_page(manifest["download"], 2)
            )
            applied = reopened.install(manifest["download"])
            self.assertEqual(applied["version"], 1)
            self.assertEqual(len(reopened.content()), 300)
        finally:
            reopened.close()

    def test_duplicate_page_is_accepted_idempotently(self):
        self.pub.publish("north", self.triples)
        device = ScreenDevice.open(
            os.path.join(self.dir, "screen"), "north", self.pub
        )
        try:
            manifest = device.sync()
            page = self.pub.fetch_page(manifest["download"], 0)
            self.assertEqual(
                device.accept_page(page)["status"], "accepted"
            )
            self.assertEqual(
                device.accept_page(page)["status"], "duplicate"
            )
            self.assertEqual(len(device.missing_pages(
                manifest["download"])), 2)
        finally:
            device.close()


class PageValidationTest(SyncTestCase):
    def _manifest(self):
        self.pub.publish("north", release_all())
        return self.dev.sync()

    def test_missing_page_blocks_install(self):
        # Force a multi-page pack by shrinking the page limit for this test.
        with mock.patch("termdb.sync.PAGE_LIMIT", 2):
            manifest = self._manifest()
            self.assertEqual(manifest["page_count"], 4)
            self.dev.accept_page(
                self.pub.fetch_page(manifest["download"], 0)
            )
            with self.assertRaises(SyncError):
                self.dev.install(manifest["download"])
            self.assertIsNone(self.dev.installed_version())
            # Completing the remaining pages makes the same journal usable.
            for number in range(1, manifest["page_count"]):
                self.dev.accept_page(
                    self.pub.fetch_page(manifest["download"], number)
                )
            self.assertEqual(
                self.dev.install(manifest["download"])["version"], 1
            )

    def test_corrupted_page_is_rejected(self):
        manifest = self._manifest()
        page = self.pub.fetch_page(manifest["download"], 0)
        page["items"][0] = {
            "facility": "plant-a", "coord": "en/tech", "text": "tampered",
        }
        with self.assertRaises(SyncError):
            self.dev.accept_page(page)
        self.assertEqual(
            self.dev.missing_pages(manifest["download"]),
            list(range(manifest["page_count"])),
        )

    def test_page_from_another_download_is_rejected(self):
        from termdb import Coord

        manifest = self._manifest()
        # A different download (another target/base pair) is a different
        # fixed identity; its pages can never join this device's journal.
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH),
                            "a-en-tech", text="pump-other", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())
        other = self.pub.negotiate("north", None)
        self.assertNotEqual(other["download"], manifest["download"])
        foreign = self.pub.fetch_page(other["download"], 0)
        with self.assertRaises(SyncError):
            self.dev.accept_page(foreign)
        self.assertEqual(
            self.dev.missing_pages(manifest["download"]),
            list(range(manifest["page_count"])),
        )

    def test_page_from_another_channel_is_rejected(self):
        self.pub.publish("north", release_all())
        self.pub.publish("south", release_all())
        manifest = self.dev.sync()
        south = self.pub.negotiate("south", None)
        page = self.pub.fetch_page(south["download"], 0)
        with self.assertRaises(SyncError):
            self.dev.accept_page(page)

    def test_contradictory_same_page_number_is_refused(self):
        manifest = self._manifest()
        page = self.pub.fetch_page(manifest["download"], 0)
        self.dev.accept_page(page)
        contradictory = dict(page)
        contradictory["items"] = list(reversed(page["items"]))
        with self.assertRaises(SyncError):
            self.dev.accept_page(contradictory)
        # The accepted page is untouched and still installable once complete.
        self.assertEqual(self.dev.pending_downloads()[0]["received"], [0])

    def test_oversized_page_is_rejected(self):
        manifest = self._manifest()
        page = self.pub.fetch_page(manifest["download"], 0)
        page["items"] = page["items"] * (PAGE_LIMIT + 1)
        with self.assertRaises(SyncError):
            self.dev.accept_page(page)


class InstallAtomicityTest(SyncTestCase):
    def test_interrupted_install_leaves_previous_pack_readable(self):
        self.pub.publish("north", release_all())
        self.install_full_latest()
        old_version = self.dev.current_version()

        from termdb import Coord

        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-en-tech",
                            text="pump-atomic", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())
        manifest = self.dev.sync()
        self.receive_all(self.dev, manifest)
        pack_path = os.path.join(self.dev.directory, "pack.json")
        before = open(pack_path, "rb").read()

        with mock.patch("termdb.sync.os.replace",
                        side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.dev.install(manifest["download"])

        # The old pack is still exactly what it was, and offline reads work.
        self.assertEqual(open(pack_path, "rb").read(), before)
        self.assertEqual(self.dev.current_version(), old_version)
        self.assertEqual(self.dev.lookup("plant-a", Coord(EN, TECH)), "pump")
        # The journal survived, so a retry completes without re-downloading.
        self.assertTrue(self.dev.complete(manifest["download"]))
        applied = self.dev.install(manifest["download"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            self.dev.lookup("plant-a", Coord(EN, TECH)), "pump-atomic"
        )

    def test_complete_install_survives_reopen_offline(self):
        from termdb import Coord

        self.pub.publish("north", release_all())
        applied, _manifest = self.install_full_latest()
        self.assertEqual(applied["version"], 1)
        self.dev.close()

        # Reopen with no publisher at all: reads are purely local.
        offline = ScreenDevice.open(
            os.path.join(self.dir, "screen"), "north", publisher=None
        )
        try:
            self.assertEqual(offline.current_version(), 1)
            self.assertEqual(
                offline.lookup("plant-a", Coord(EN, TECH)), "pump"
            )
            self.assertEqual(
                offline.lookup("plant-b", Coord(ZH, TECH)), "阀"
            )
        finally:
            offline.close()
        # Restore the tearDown expectation of an open device.
        self.dev = ScreenDevice.open(
            os.path.join(self.dir, "screen"), "north", self.pub
        )

    def test_delta_requires_exact_base(self):
        from termdb import Coord

        self.pub.publish("north", release_all())
        self.install_full_latest()
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-en-tech",
                            text="pump-v2", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH), "a-en-tech",
                            text="pump-v3", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())

        delta = self.dev.sync()          # v1 -> v3
        self.receive_all(self.dev, delta)
        # Advance the device independently to v3 with a full pack.
        full = self.pub.negotiate("north", None)
        self.dev.adopt_download(full)
        self.receive_all(self.dev, full)
        self.dev.install(full["download"])
        self.assertEqual(self.dev.current_version(), 3)
        # The pending delta's base is no longer installed: it is a late
        # download and must not roll anything back.
        result = self.dev.install(delta["download"])
        self.assertEqual(result["status"], "noop")
        self.assertEqual(self.dev.current_version(), 3)

    def test_retrying_an_installed_pack_changes_nothing(self):
        self.pub.publish("north", release_all())
        applied, manifest = self.install_full_latest()
        pack_path = os.path.join(self.dev.directory, "pack.json")
        bytes_before = open(pack_path, "rb").read()
        mtime_before = os.stat(pack_path).st_mtime_ns
        again = self.dev.install(manifest["download"])
        self.assertEqual(again["status"], "noop")
        self.assertEqual(open(pack_path, "rb").read(), bytes_before)
        self.assertEqual(os.stat(pack_path).st_mtime_ns, mtime_before)
        self.assertEqual(self.dev.current_version(), applied["version"])

    def test_late_lower_version_download_is_noop(self):
        from termdb import Coord

        self.pub.publish("north", release_all())        # v1
        self.install_full_latest()                      # device on v1
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH),
                            "a-en-tech", text="pump-v2", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())        # v2
        stale = self.dev.sync()                         # delta v1 -> v2
        self.assertEqual((stale["mode"], stale["base"], stale["target"]),
                         ("delta", 1, 2))
        self.receive_all(self.dev, stale)

        # Meanwhile a newer version lands and the device jumps to it.
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", "plant-a", Coord(EN, TECH),
                            "a-en-tech", text="pump-v3", is_public=False,
                            review=REVIEWED),
        ]))
        self.pub.publish("north", release_all())        # v3
        newer = self.pub.negotiate("north", None)
        self.dev.adopt_download(newer)
        self.receive_all(self.dev, newer)
        self.assertEqual(
            self.dev.install(newer["download"])["version"], 3
        )
        pack_path = os.path.join(self.dev.directory, "pack.json")
        with open(pack_path, "rb") as fh:
            bytes_before = fh.read()

        # The late v1->v2 delta must not roll the device back.
        self.assertEqual(
            self.dev.install(stale["download"])["status"], "noop"
        )
        self.assertEqual(self.dev.current_version(), 3)
        with open(pack_path, "rb") as fh:
            self.assertEqual(fh.read(), bytes_before)
        self.assertEqual(
            self.dev.lookup("plant-a", Coord(EN, TECH)), "pump-v3"
        )

    def test_delta_on_wrong_base_is_refused_not_mixed(self):
        from termdb import Coord

        def correct(text):
            self.store.apply_batch(Batch([
                EntryCorrection("set_body", "plant-a", Coord(EN, TECH),
                                "a-en-tech", text=text, is_public=False,
                                review=REVIEWED),
            ]))
            self.pub.publish("north", release_all())

        self.pub.publish("north", release_all())        # v1
        self.install_full_latest()                      # device on v1
        correct("pump-v2")                              # v2
        to_v2 = self.dev.sync()                         # delta v1 -> v2
        self.assertEqual((to_v2["mode"], to_v2["base"], to_v2["target"]),
                         ("delta", 1, 2))
        self.receive_all(self.dev, to_v2)
        self.dev.install(to_v2["download"])
        self.assertEqual(self.dev.current_version(), 2)
        correct("pump-v3")                              # v3
        # A device still naming v1 as its base gets a v1 -> v3 delta.  This
        # one is adopted against a device that is now on v2: applying it
        # would mix states, so install must refuse rather than merge.
        stale = self.pub.negotiate("north", 1)
        self.assertEqual((stale["mode"], stale["base"], stale["target"]),
                         ("delta", 1, 3))
        self.dev.adopt_download(stale)
        self.receive_all(self.dev, stale)
        before = self.dev.installed_version()
        with self.assertRaises(SyncError):
            self.dev.install(stale["download"])
        self.assertEqual(self.dev.installed_version(), before)
        self.assertEqual(
            self.dev.lookup("plant-a", Coord(EN, TECH)), "pump-v2"
        )


class DemoTest(unittest.TestCase):
    def test_demo_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "artifacts")
            os.makedirs(target)
            first = run_sync_demo(target)
            self.assertEqual(first, target)
            self.assertTrue(
                os.path.exists(os.path.join(target, "screen", "pack.json"))
            )
            # Running again against a fresh directory produces the same
            # visible milestones and a readable pack.
            other = os.path.join(tmp, "second")
            os.makedirs(other)
            run_sync_demo(other)
            pack = json.load(open(
                os.path.join(other, "screen", "pack.json"), "rb"
            ))
            self.assertEqual(pack["channel"], "north-wing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
