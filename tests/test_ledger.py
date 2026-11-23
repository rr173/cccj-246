"""End-to-end tests for the display-count reconciliation ledger.

Covered here:
  * installation records vs explicit identity-unknown records vs revocations;
  * revisions: lower ignored, equal-and-same idempotent, equal-and-different
    rejects the whole batch, higher revision revises, revoke tombstones block
    late resurrection;
  * readings as cumulative counters, rounds, no cross-round comparison,
    backward counts rejected without changing anything;
  * interval attribution: single pack, first baseline, no tail estimate,
    exact-at-b install excluded, unknown precedence over cross-version,
    switch-and-return still cross-version;
  * report ranges keyed by the right reading instant, no splitting, zero
    increments omitted, grouping by full identity and by reason;
  * report/reopen parity and "one committed state" visibility;
  * late records changing historical attribution, with bounded incremental
    work verified against a full-recompute oracle;
  * device identity lookup for an evicted pack version.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    ATTRIBUTED,
    CROSS_VERSION,
    CountLedger,
    Entry,
    IDENTITY_UNKNOWN,
    InstallRecord,
    LedgerBatch,
    LedgerError,
    PackIdentity,
    Policy,
    Reading,
    REVIEWED,
    TEXT,
    TermStore,
    identity_from_device,
)
from termdb.sync import OfflinePublisher, ScreenDevice  # noqa: E402

SCREEN = "lobby"
FAC = "pump-hall"
LANG, CTX = "en", "tech"


def identity(channel="north", version=1, pack_id="pack-1"):
    return PackIdentity(channel, version, pack_id)


def reading(t, count, *, screen=SCREEN, round_id="r1", facility=FAC,
            lang=LANG, ctx=CTX, rev=1):
    return Reading.cumulative(
        screen, round_id, facility, lang, ctx, t, rev, count
    )


def install(t, pack=None, *, screen=SCREEN, rev=1):
    if pack is None:
        return InstallRecord.unknown(screen, t, rev)
    return InstallRecord.installed(screen, t, rev, pack)


def revoke_reading(t, *, screen=SCREEN, round_id="r1", facility=FAC,
                   lang=LANG, ctx=CTX, rev=2):
    return Reading.revoke(
        screen, round_id, facility, lang, ctx, t, rev
    )


def revoke_install(t, *, screen=SCREEN, rev=2):
    return InstallRecord.revoke(screen, t, rev)


def batch(installs=(), readings=()):
    return LedgerBatch(installs=list(installs), readings=list(readings))


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "counts.db")
        self.ledger = CountLedger.open(self.path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def apply(self, installs=(), readings=()):
        return self.ledger.apply(batch(installs, readings))

    def report(self, start=0, end=2 ** 63 - 1):
        return self.ledger.report(start, end)


class BasicAttributionTest(LedgerTestCase):
    def test_single_pack_interval_and_zero_increment_omitted(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[
            reading(10, 5),
            reading(20, 20),
            reading(30, 20),      # zero increment
            reading(40, 50),
        ])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 45)
        self.assertEqual(rep["unattributed_total"], 0)
        self.assertEqual(len(rep["increments"]), 2)   # (10,20),(30,40)
        self.assertTrue(all(i["count"] > 0 for i in rep["increments"]))
        self.assertEqual(
            [(i["a_time"], i["b_time"], i["count"])
             for i in rep["increments"]],
            [(10, 20, 15), (30, 40, 30)],
        )
        self.assertEqual(rep["attributed"][0]["count"], 45)
        self.assertEqual(rep["attributed"][0]["pack_id"], "pack-1")
        # The zero reading still split the interval partition: a pack change
        # at 30 must not change the attribution of [20,30).
        self.assertEqual(len(self.ledger.segments()), 3)

    def test_first_reading_is_baseline_and_tail_not_estimated(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[reading(10, 100), reading(20, 130)])
        rep = self.report()
        self.assertEqual(rep["total"], 30)
        self.assertEqual(rep["increments"][0]["a_time"], 10)
        # No interval after 20 even though the report range extends beyond.
        self.assertEqual(len(rep["increments"]), 1)

    def test_before_first_install_is_identity_unknown(self):
        self.apply(installs=[install(15, identity())])
        self.apply(readings=[reading(10, 0), reading(20, 10)])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 0)
        self.assertEqual(rep["unattributed_total"], 10)
        self.assertEqual(rep["unattributed"][0]["reason"], IDENTITY_UNKNOWN)

    def test_install_exactly_at_b_does_not_affect_segment(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(20, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(10, 0), reading(20, 10)])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 10)
        self.assertEqual(rep["attributed"][0]["version"], 1)

    def test_switch_and_return_is_cross_version(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(10, identity(version=2, pack_id="p2")),
            install(20, identity(version=1, pack_id="p1")),
        ])
        self.apply(readings=[reading(0, 0), reading(30, 90)])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 0)
        self.assertEqual(rep["unattributed_total"], 90)
        self.assertEqual(rep["unattributed"][0]["reason"], CROSS_VERSION)

    def test_unknown_anywhere_wins_over_cross_version(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(10),                                  # unknown
            install(20, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(0, 0), reading(30, 30)])
        rep = self.report()
        self.assertEqual(rep["unattributed"][0]["reason"], IDENTITY_UNKNOWN)

    def test_reinstall_same_identity_is_not_cross_version(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(10, identity(version=1, pack_id="p1")),
        ])
        self.apply(readings=[reading(0, 0), reading(20, 20)])
        self.assertEqual(self.report()["attributed_total"], 20)

    def test_revoked_install_no_longer_changes_state(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(10, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(0, 0), reading(20, 20)])
        before = self.report()
        self.assertEqual(before["unattributed_total"], 20)
        self.assertEqual(before["unattributed"][0]["reason"], CROSS_VERSION)
        self.apply(installs=[revoke_install(10)])
        after = self.report()
        # The revoked record is gone; identity 1 is in force the whole time.
        self.assertEqual(after["attributed_total"], 20)
        self.assertEqual(after["attributed"][0]["version"], 1)
        self.assertEqual(after["increments"][0]["a_time"], 0)

    def test_explicit_unknown_record_differs_from_revocation(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(10, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(0, 0), reading(20, 20)])
        # Replace the second installation with an explicit unknown; this
        # still changes state (unlike a revocation).
        self.apply(installs=[install(10, None, rev=2)])
        rep = self.report()
        self.assertEqual(rep["unattributed_total"], 20)
        self.assertEqual(rep["unattributed"][0]["reason"], IDENTITY_UNKNOWN)
        # Revoking that unknown record makes the earlier pack effective.
        self.apply(installs=[revoke_install(10, rev=3)])
        rep2 = self.report()
        self.assertEqual(rep2["attributed_total"], 20)
        self.assertEqual(rep2["increments"][0]["kind"], ATTRIBUTED)


class RevisionTest(LedgerTestCase):
    def test_lower_revision_is_ignored(self):
        self.apply(installs=[install(0, identity(version=2, pack_id="p2"),
                                     rev=5)])
        self.apply(installs=[install(0, identity(version=1, pack_id="p1"),
                                     rev=4)])
        self.assertEqual(
            self.ledger.effective_installs(SCREEN)[0]["version"], 2
        )

    def test_equal_revision_same_content_is_noop(self):
        self.apply(installs=[install(0, identity())])
        seq = self.ledger.state_seq
        self.apply(installs=[install(0, identity())])
        self.assertEqual(self.ledger.state_seq, seq)
        self.assertEqual(len(self.ledger.commits()), 1)

    def test_equal_revision_conflict_rejects_whole_batch(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1"), rev=2),
            install(10, identity(version=1, pack_id="p1"), rev=2),
        ])
        self.apply(readings=[reading(0, 0), reading(20, 30)])
        before = self.report()
        with self.assertRaises(LedgerError):
            self.apply(
                installs=[install(0, identity(version=9, pack_id="p9"),
                                  rev=2)],
                readings=[reading(20, 99, rev=2)],
            )
        self.assertEqual(self.report(), before)
        self.assertEqual(self.ledger.state_seq, before["state_seq"])

    def test_conflict_inside_one_batch_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.apply(readings=[
                reading(10, 5, rev=2),
                reading(10, 6, rev=2),
            ])
        self.assertEqual(self.ledger.effective_readings(), [])

    def test_higher_revision_revises_and_tombstone_blocks_old_value(self):
        self.apply(readings=[reading(10, 50, rev=1)])
        self.apply(readings=[revoke_reading(10, rev=3)])
        self.assertEqual(self.ledger.effective_readings(), [])
        # A late revision 2 must not resurrect the revoked reading.
        self.apply(readings=[reading(10, 50, rev=2)])
        self.assertEqual(self.ledger.effective_readings(), [])
        # A still higher revision may restore it.
        self.apply(readings=[reading(10, 60, rev=4)])
        self.assertEqual(self.ledger.effective_readings()[0]["count"], 60)

    def test_resend_of_whole_batch_has_no_effect(self):
        recs = [
            install(0, identity()),
            Reading.cumulative(SCREEN, "r1", FAC, LANG, CTX, 10, 1, 1),
            Reading.cumulative(SCREEN, "r1", FAC, LANG, CTX, 20, 1, 5),
        ]
        self.ledger.apply(batch([recs[0]], recs[1:]))
        seq = self.ledger.state_seq
        rep = self.report()
        self.ledger.apply(batch([recs[0]], recs[1:]))
        self.assertEqual(self.ledger.state_seq, seq)
        self.assertEqual(self.report(), rep)

    def test_rejected_batch_changes_neither_records_nor_segments(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[reading(10, 5), reading(20, 10)])
        before_segments = self.ledger.segments()
        before = self.report()
        with self.assertRaises(LedgerError):
            self.apply(
                installs=[install(30, identity(version=2, pack_id="p2"))],
                readings=[reading(20, 4, rev=2)],   # backwards
            )
        self.assertEqual(self.ledger.segments(), before_segments)
        self.assertEqual(self.report(), before)


class CounterRulesTest(LedgerTestCase):
    def test_backwards_count_rejected_and_ledger_unchanged(self):
        self.apply(readings=[reading(10, 100)])
        with self.assertRaises(LedgerError):
            self.apply(readings=[reading(20, 99)])
        self.assertEqual(len(self.ledger.effective_readings()), 1)

    def test_rounds_are_never_compared(self):
        self.apply(readings=[
            reading(10, 500, round_id="r1"),
            reading(20, 600, round_id="r1"),
            reading(5, 0, round_id="r2"),
            reading(15, 40, round_id="r2"),
        ])
        rep = self.report()
        self.assertEqual(rep["total"], 140)
        rounds = {i["round_id"] for i in rep["increments"]}
        self.assertEqual(rounds, {"r1", "r2"})

    def test_player_or_channel_change_need_not_restart_round(self):
        # One round, one counter series; a pack switch is seen as a pack
        # change, not implicitly as a counter restart.
        self.apply(installs=[
            install(0, identity(channel="north", version=1, pack_id="p1")),
            install(15, identity(channel="south", version=1, pack_id="p9")),
        ])
        self.apply(readings=[reading(0, 0), reading(30, 30)])
        rep = self.report()
        self.assertEqual(rep["unattributed_total"], 30)
        self.assertEqual(rep["unattributed"][0]["reason"], CROSS_VERSION)
        self.assertEqual(len(self.ledger.segments()), 1)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(LedgerError):
            self.apply()
        with self.assertRaises(LedgerError):
            self.apply(readings=[reading(10, -1)])
        with self.assertRaises(LedgerError):
            self.apply(readings=[reading(10, 1.5)])
        with self.assertRaises(LedgerError):
            self.apply(readings=[reading(True, 1)])
        with self.assertRaises(LedgerError):
            self.apply(installs=[install(0, ("north", 1))])
        self.assertEqual(self.ledger.effective_readings(), [])

    def test_batch_may_mix_installations_and_readings(self):
        # One installation record and readings, applied atomically.
        self.apply(
            installs=[install(0, identity())],
            readings=[reading(10, 5), reading(20, 50)],
        )
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 45)
        self.assertEqual(len(self.ledger.commits()), 1)

    def test_two_reading_corrections_in_one_batch_merge_neighbourhoods(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[reading(t, t) for t in (0, 10, 40, 70, 100)])
        # Correct two non-adjacent readings in the same batch; each split
        # must be handled and the union of neighbourhoods rebuilt.
        self.apply(readings=[
            reading(20, 15, rev=2),
            reading(55, 55, rev=2),
        ])
        segs = self.ledger.segments()
        spans = [(s["a_time"], s["b_time"], s["count"]) for s in segs]
        self.assertEqual(
            spans,
            [
                (0, 10, 10), (10, 20, 5), (20, 40, 25),
                (40, 55, 15), (55, 70, 15), (70, 100, 30),
            ],
        )
        self.assertEqual(self.report()["total"], 100)

    def test_revoke_and_add_reading_in_one_batch(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[reading(t, t) for t in (0, 10, 30)])
        # Revoke the middle reading and add a new one in the same batch.
        self.apply(readings=[
            revoke_reading(10, rev=2),
            reading(20, 15, rev=2),
        ])
        spans = [
            (s["a_time"], s["b_time"], s["count"])
            for s in self.ledger.segments()
        ]
        self.assertEqual(spans, [(0, 20, 15), (20, 30, 15)])

    def test_non_negative_integer_only(self):
        self.apply(readings=[reading(0, 0)])
        self.assertEqual(
            self.ledger.effective_readings()[0]["count"], 0
        )


class ReportRangeTest(LedgerTestCase):
    def test_range_uses_right_reading_time_and_does_not_split(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[
            reading(10, 0),
            reading(40, 30),     # interval [10,40): 30
            reading(70, 90),     # interval [40,70): 60
            reading(100, 100),   # interval [70,100): 10
        ])
        # The range selects by the *right reading instant*.  [40, 70) takes
        # only the increment whose right endpoint is 40; the one at 70 is
        # excluded.
        rep = self.report(40, 70)
        self.assertEqual(rep["total"], 30)
        self.assertEqual([i["a_time"] for i in rep["increments"]], [10])
        # [35, 75) takes right endpoints 40 and 70 -- whole intervals, never
        # split by duration.
        rep = self.report(35, 75)
        self.assertEqual(rep["total"], 90)
        self.assertEqual(
            [(i["a_time"], i["b_time"], i["count"])
             for i in rep["increments"]],
            [(10, 40, 30), (40, 70, 60)],
        )
        # The empty range selects nothing.
        self.assertEqual(self.report(40, 40)["total"], 0)

    def test_grouping_by_full_identity_and_reason(self):
        self.apply(installs=[
            install(0, identity(channel="north", version=1, pack_id="p1"),
                    screen="A"),
            install(0, identity(channel="north", version=1, pack_id="p1"),
                    screen="B"),
            install(0, identity(channel="south", version=1, pack_id="p1"),
                    screen="C"),
            install(0, identity(channel="north", version=2, pack_id="p1"),
                    screen="D"),
        ])
        for screen in "ABCD":
            self.apply(readings=[
                reading(0, 0, screen=screen),
                reading(100, 10, screen=screen),
            ])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 40)
        keys = {
            (r["screen"], r["channel"], r["version"], r["pack_id"])
            for r in rep["attributed"]
        }
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(rep["attributed"]), 4)


class ReopenTest(LedgerTestCase):
    def test_reopen_is_exact_and_accepts_more(self):
        self.apply(installs=[install(0, identity())])
        self.apply(readings=[reading(10, 5), reading(30, 25)])
        rep = self.report()
        segments = self.ledger.segments()
        seq = self.ledger.state_seq
        commits = self.ledger.commits()
        self.ledger.close()
        self.ledger = CountLedger.open(self.path)
        self.assertEqual(self.ledger.state_seq, seq)
        self.assertEqual(self.ledger.segments(), segments)
        self.assertEqual(self.ledger.commits(), commits)
        self.assertEqual(self.report(), rep)
        # Still accepts revisions.
        self.apply(readings=[reading(40, 45)])
        self.assertEqual(self.report()["total"], 40)


class LateCorrectionTest(LedgerTestCase):
    def test_late_install_record_changes_history_including_crossing(self):
        self.apply(installs=[install(0, identity(version=1, pack_id="p1"))])
        self.apply(readings=[
            reading(10, 0), reading(50, 40), reading(90, 100),
        ])
        self.assertEqual(self.report()["attributed_total"], 100)
        # A late record says v2 was installed at 20, in the *interior* of
        # the first segment [10, 50).  That segment now crosses v1->v2 and
        # becomes cross-version, while the later segment [50, 90) now sees
        # v2 throughout and stays attributable.
        self.apply(installs=[
            install(20, identity(version=2, pack_id="p2")),
        ])
        rep = self.report()
        self.assertEqual(rep["unattributed_total"], 40)    # [10, 50)
        self.assertEqual(
            rep["unattributed"][0]["reason"], CROSS_VERSION
        )
        self.assertEqual(rep["attributed_total"], 60)      # [50, 90)
        self.assertEqual(rep["attributed"][0]["version"], 2)

    def test_late_install_makes_crossing_segment_attributed(self):
        # [10, 90) currently crosses a pack change at 50 -> cross-version.
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(50, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(10, 0), reading(90, 80)])
        self.assertEqual(self.report()["unattributed_total"], 80)
        # A higher revision revokes the mid-interval installation; the
        # segment's two endpoints now see one identity, so the *whole*
        # previously-crossing segment becomes attributable.
        self.apply(installs=[revoke_install(50)])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 80)
        self.assertEqual(rep["attributed"][0]["version"], 1)

    def test_late_reading_correction_changes_adjacent_attribution(self):
        self.apply(installs=[
            install(0, identity(version=1, pack_id="p1")),
            install(30, identity(version=2, pack_id="p2")),
        ])
        self.apply(readings=[reading(10, 0), reading(60, 50)])
        # [10,60) crosses the pack change at 30 -> cross-version.
        self.assertEqual(self.report()["unattributed_total"], 50)
        # A late reading at 40 arrives.  It splits the interval into
        # [10,40) (cross-version) and [40,60) (v2 only, attributed).
        self.apply(readings=[reading(40, 30, rev=2)])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 20)
        self.assertEqual(rep["attributed"][0]["version"], 2)
        self.assertEqual(rep["unattributed_total"], 30)

    def test_install_correction_updates_segments_whose_ends_straddle(self):
        # Segments: [0,10), [10,20), [20,60).  A late install at 20 starts
        # exactly at a segment boundary: [0,10) and [10,20) stay v1, while
        # [20,60) now sees v2..v2 and stays attributed.
        self.apply(installs=[install(0, identity(version=1, pack_id="p1"))])
        self.apply(readings=[
            reading(0, 0), reading(10, 10), reading(20, 20),
            reading(60, 70),
        ])
        writes_before = len(self.ledger.segments())
        self.apply(installs=[
            install(20, identity(version=2, pack_id="p2")),
        ])
        rep = self.report()
        self.assertEqual(rep["attributed_total"], 70)
        self.assertEqual(rep["unattributed_total"], 0)
        by_version = {
            r["version"]: r["count"] for r in rep["attributed"]
        }
        self.assertEqual(by_version, {1: 20, 2: 50})
        self.assertEqual(len(rep["increments"]), 3)
        self.assertEqual(len(self.ledger.segments()), writes_before)

    def test_late_install_in_the_middle_of_a_long_segment(self):
        # One segment [0, 100).  A late install at 50 does not sit on either
        # endpoint but does cross the segment's interior: it must become
        # cross-version without any reading arriving.
        self.apply(installs=[install(0, identity(version=1, pack_id="p1"))])
        self.apply(readings=[reading(0, 0), reading(100, 100)])
        self.assertEqual(self.report()["attributed_total"], 100)
        self.apply(installs=[
            install(50, identity(version=2, pack_id="p2")),
        ])
        rep = self.report()
        self.assertEqual(rep["unattributed_total"], 100)
        self.assertEqual(rep["unattributed"][0]["reason"], CROSS_VERSION)


class DeviceIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_read_from_device_and_survives_eviction(self):
        from termdb import Coord

        coord = Coord(LANG, CTX)
        store = TermStore.bootstrap(
            os.path.join(self.dir, "terms.db"),
            {coord: [coord]},
            {"pump-hall": Policy("pump-hall", False, False, frozenset())},
            [Entry("pump-hall", coord, "s1", TEXT, "pump", False,
                   REVIEWED)],
        )
        pub = OfflinePublisher(os.path.join(self.dir, "pub.db"), store)
        dev = ScreenDevice.open(os.path.join(self.dir, "screen"), "north",
                                pub)
        try:
            for version in range(1, 6):
                store.apply_batch(__import__("termdb").Batch([
                    __import__("termdb").EntryCorrection(
                        "set_body", "pump-hall", coord, "s1",
                        text=f"pump-{version}", is_public=False,
                        review=REVIEWED,
                    )
                ]))
                pub.publish("north", [("pump-hall", LANG, CTX)])
                manifest = dev.sync()
                for n in range(manifest["page_count"]):
                    dev.accept_page(pub.fetch_page(manifest["download"], n))
                dev.install(manifest["download"])
            identity_now = identity_from_device(dev)
            self.assertEqual(identity_now.channel, "north")
            self.assertEqual(identity_now.version, 5)
            # Publish past the retention window so v5 is evicted from the
            # publisher, then drop the publisher and the term store entirely:
            # the identity is read from the device's own pack file, so an
            # evicted version is still identified normally.
            for version in (6, 7, 8):
                pub.publish("north", [("pump-hall", LANG, CTX)])
            self.assertFalse(pub.is_retained("north", 5))
            dev.close()
            dev = None
            pub.close()
            pub = None
            store.close()
            store = None

            dev = ScreenDevice.open(
                os.path.join(self.dir, "screen"), "north", publisher=None
            )
            identity_now = identity_from_device(dev)
            self.assertEqual(identity_now.version, 5)
            ledger = CountLedger.open(os.path.join(self.dir, "counts.db"))
            try:
                ledger.apply(batch(
                    installs=[InstallRecord.from_device(
                        SCREEN, 1000, 1, dev
                    )],
                    readings=[reading(1000, 0), reading(2000, 7)],
                ))
                rep = ledger.report()
                self.assertEqual(rep["attributed_total"], 7)
                self.assertEqual(rep["attributed"][0]["version"], 5)
                self.assertEqual(
                    rep["attributed"][0]["pack_id"],
                    identity_now.pack_id,
                )
            finally:
                ledger.close()
        finally:
            if dev is not None:
                dev.close()
            if pub is not None:
                pub.close()
            if store is not None:
                store.close()


if __name__ == "__main__":
    unittest.main()
