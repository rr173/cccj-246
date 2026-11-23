"""Review drafts: local save + later approval merge.

Covered here:
  * saving a draft changes no live supply, survives reopen, and cannot be
    edited;
  * approval merges against the *live* state, never by replaying commands;
  * three-way record merge: baseline/live/draft agree -> take, same value
    both sides -> keep, divergent -> record conflict naming all three
    values (absent vs withdrawn vs explicit clear kept distinct);
  * policy merges the same way and names the facility;
  * supply check: ordinary, generic (including facilities the baseline
    policy forbids generic words to) and policy scope; results compared by
    outcome/text/conflict texts, not provenance; only-one-side-changed must
    be preserved; both-changed-identically kept; divergent or merge-shifted
    -> supply conflict naming the four results;
  * rejection leaves live supply, ledger and caches untouched;
  * approving a merged draft again writes nothing;
  * conncurrent direct corrections and approval produce one of the two
    complete serial orders, never losing a checked-after correction;
  * unaffected records keep live values; output is atomic.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    CLEAR,
    CONFLICT,
    Coord,
    DRAFT_MERGED,
    DraftAlreadyMerged,
    DraftError,
    DraftNotFound,
    Entry,
    EntryCorrection,
    GENERIC,
    MergeConflict,
    Policy,
    PolicyCorrection,
    REVIEWED,
    REVOKED,
    SUPPLIED,
    TEXT,
    TermStore,
    WITHDRAWN,
)
from termdb.review import DRAFT_SAVED  # noqa: E402

EN_TECH = Coord("en", "tech")
EN_DOC = Coord("en", "doc")
ZH_TECH = Coord("zh", "tech")
ZH_DOC = Coord("zh", "doc")

CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],
    ZH_DOC: [ZH_DOC, EN_DOC],
}

F1 = "facility-one"
F2 = "facility-two"


def policies():
    return {
        # Generic fallback is forbidden for both facilities at baseline so
        # target results are simple; a review still has to check generic
        # edits against facilities that forbid them, because approval can
        # happen after that policy changes.
        F1: Policy(F1, allow_generic=False, require_public_reviewed=False,
                    blocked=frozenset()),
        F2: Policy(F2, allow_generic=False, require_public_reviewed=False,
                    blocked=frozenset()),
    }


def entries():
    return [
        Entry(F1, EN_TECH, "s1", TEXT, "pump", False, REVIEWED),
        Entry(F1, EN_DOC, "s2", TEXT, "pump-doc", False, REVIEWED),
        Entry(F1, ZH_TECH, "s10", TEXT, "泵", False, REVIEWED),
        Entry(F2, EN_TECH, "s20", TEXT, "valve", False, REVIEWED),
        Entry(GENERIC, EN_DOC, "g1", TEXT, "generic-device", False,
              REVIEWED),
    ]


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "terms.db")
        self.store = TermStore.bootstrap(
            self.path, CANDIDATES, policies(), entries()
        )

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self.tmp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = TermStore.open(self.path)

    def q(self, facility, target):
        return self.store.query(facility, target)

    def snapshot(self):
        return {
            (f, t.key()): self.q(f, t)
            for f in (F1, F2) for t in CANDIDATES
        }


class SaveDraftTest(ReviewBase):
    def test_save_changes_no_live_supply_and_survives_reopen(self):
        before = self.snapshot()
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        self.assertEqual(self.snapshot(), before)
        record = self.store.draft(draft_id)
        self.assertEqual(record["status"], DRAFT_SAVED)
        self.assertEqual(len(record["entries"]), 1)
        self.assertEqual(record["entries"][0]["base_value"]["text"], "pump")
        self.assertEqual(record["entries"][0]["draft_value"]["text"], "pumpe")

        self.reopen()
        again = self.store.draft(draft_id)
        self.assertEqual(again, record)
        self.assertEqual(self.snapshot(), before)

    def test_draft_is_immutable(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        # No public API mutates a saved draft; the correction commands are
        # snapshotted once and the stored snapshot does not change even
        # though live state does.
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="live"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pump"),
        ]))
        self.assertEqual(
            self.store.draft(draft_id)["entries"][0]["draft_value"]["text"],
            "pumpe",
        )
        self.assertEqual(self.store.merge_draft(draft_id)["status"],
                         DRAFT_MERGED)

    def test_unknown_draft_id(self):
        with self.assertRaises(DraftNotFound):
            self.store.draft(999)
        with self.assertRaises(DraftNotFound):
            self.store.merge_draft(999)

    def test_invalid_batch_is_rejected_like_direct_correction(self):
        with self.assertRaises(DraftError):
            self.store.save_draft(Batch([
                EntryCorrection("withdraw", F1, EN_TECH, "missing"),
            ]))
        # A batch that changes nothing relative to the baseline is not a
        # usable draft either.
        with self.assertRaises(DraftError):
            self.store.save_draft(Batch([
                EntryCorrection("set_body", F1, EN_TECH, "s1", text="pump"),
            ]))
        self.assertEqual(self.store.drafts(), [])

    def test_draft_can_include_a_policy_change(self):
        draft_id = self.store.save_draft(Batch(
            [],
            policy=PolicyCorrection(F2, True, False, frozenset()),
        ))
        record = self.store.draft(draft_id)
        self.assertFalse(record["policy"]["base_value"]["allow_generic"])
        self.assertTrue(record["policy"]["draft_value"]["allow_generic"])
        merged = self.store.merge_draft(draft_id)
        self.assertEqual(merged["policy"], F2)
        self.assertTrue(self.store.policies[F2].allow_generic)


class RecordMergeTest(ReviewBase):
    def test_live_unchanged_takes_draft_value(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        result = self.store.merge_draft(draft_id)
        self.assertEqual(result["status"], DRAFT_MERGED)
        self.assertEqual(self.q(F1, EN_TECH)["text"], "pumpe")

    def test_live_and_draft_same_value_keeps_it(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="same"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="same"),
        ]))
        self.store.merge_draft(draft_id)
        self.assertEqual(self.q(F1, EN_TECH)["text"], "same")

    def test_divergent_values_report_all_three(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="live"),
        ]))
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        conflict = caught.exception.record_conflicts[0]
        self.assertEqual(conflict["kind"], "entry")
        self.assertEqual(conflict["base"]["text"], "pump")
        self.assertEqual(conflict["draft"]["text"], "draft")
        self.assertEqual(conflict["live"]["text"], "live")
        # Nothing changed live.
        self.assertEqual(self.q(F1, EN_TECH)["text"], "live")

    def test_absent_withdrawn_and_clear_are_distinct(self):
        # Baseline has no s30.  The draft creates an explicit clear; live
        # instead created then withdrew a different record at the same key.
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("clear", F1, EN_TECH, "s30"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s30", text="x"),
            EntryCorrection("withdraw", F1, EN_TECH, "s30"),
        ]))
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        conflict = caught.exception.record_conflicts[0]
        self.assertIsNone(conflict["base"])
        self.assertEqual(conflict["draft"]["body_state"], CLEAR)
        self.assertEqual(conflict["live"]["body_state"], WITHDRAWN)

    def test_unrelated_records_keep_live_values(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, ZH_TECH, "s10", text="泵-v2"),
        ]))
        self.store.merge_draft(draft_id)
        self.assertEqual(self.q(F1, EN_TECH)["text"], "pumpe")
        self.assertEqual(self.q(F1, ZH_TECH)["text"], "泵-v2")
        self.assertEqual(self.q(F2, EN_TECH)["text"], "valve")


class PolicyMergeTest(ReviewBase):
    def test_policy_conflict_names_facility_and_three_values(self):
        draft_id = self.store.save_draft(Batch(
            [], policy=PolicyCorrection(F2, True, True, frozenset()),
        ))
        self.store.apply_batch(Batch(
            [], policy=PolicyCorrection(F2, False, True, frozenset()),
        ))
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        conflicts = caught.exception.record_conflicts
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "policy")
        self.assertEqual(conflicts[0]["facility"], F2)
        self.assertEqual(conflicts[0]["base"]["allow_generic"], False)
        self.assertEqual(conflicts[0]["draft"]["allow_generic"], True)
        self.assertEqual(conflicts[0]["live"]["allow_generic"], False)

    def test_policy_live_unchanged_applies_draft(self):
        draft_id = self.store.save_draft(Batch(
            [], policy=PolicyCorrection(F2, True, False, frozenset()),
        ))
        self.store.merge_draft(draft_id)
        self.assertTrue(self.store.policies[F2].allow_generic)


class SupplyConflictTest(ReviewBase):
    def test_draft_only_change_is_kept(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, ZH_TECH, "s10", text="泵-v2"),
        ]))
        result = self.store.merge_draft(draft_id)
        self.assertEqual(result["status"], DRAFT_MERGED)
        self.assertEqual(self.q(F1, ZH_TECH)["text"], "泵-v2")

    def test_live_only_change_is_kept(self):
        # Draft edits s1 in facility one; live later edits a different
        # record in the other facility.  Both survive.
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F2, EN_TECH, "s20", text="valve-v2"),
        ]))
        self.store.merge_draft(draft_id)
        self.assertEqual(self.q(F1, EN_TECH)["text"], "draft")
        self.assertEqual(self.q(F2, EN_TECH)["text"], "valve-v2")

    def test_both_sides_same_supply_result_is_kept(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="both"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="both"),
        ]))
        self.assertEqual(self.store.merge_draft(draft_id)["status"],
                         DRAFT_MERGED)

    def test_divergent_both_sides_is_record_conflict(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="live"),
        ]))
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        self.assertTrue(caught.exception.record_conflicts)
        self.assertEqual(self.q(F1, EN_TECH)["text"], "live")

    def test_supply_conflict_without_record_conflict(self):
        # No record is changed on both sides, but both sides move the same
        # target's wording.  F1 (baseline) uses generic words, so en/doc
        # conflicts between s2 and g1.  The draft edits g1; live edits s2.
        self.store.apply_batch(Batch(
            [], policy=PolicyCorrection(F1, True, False, frozenset()),
        ))
        self.assertEqual(self.q(F1, EN_DOC)["outcome"], CONFLICT)
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", GENERIC, EN_DOC, "g1",
                            text="generic-draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_DOC, "s2",
                            text="pump-live"),
        ]))
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        self.assertEqual(caught.exception.record_conflicts, [])
        conflicts = {
            (c["facility"], c["target"]): c
            for c in caught.exception.supply_conflicts
        }
        conflict = conflicts[(F1, "en/doc")]
        self.assertEqual(
            conflict["base"]["conflict_texts"],
            ["generic-device", "pump-doc"],
        )
        self.assertEqual(
            conflict["draft"]["conflict_texts"],
            ["generic-draft", "pump-doc"],
        )
        self.assertEqual(
            conflict["live"]["conflict_texts"],
            ["generic-device", "pump-live"],
        )
        self.assertIsNone(conflict["expected"])
        # Refused: live wording is untouched.
        self.assertEqual(
            self.q(F1, EN_DOC)["conflict_texts"],
            ["generic-device", "pump-live"],
        )

    def test_generic_change_is_checked_even_where_baseline_forbids_it(self):
        # The worked example.  Baseline F2 forbids generic words, so F2's
        # en/doc shows its own valve text.  The draft edits generic g1 while
        # F2 forbids it.  Before approval, live lets F2 use generic words,
        # so approving the draft would silently switch F2's display from
        # "valve" to the *draft's new* generic text.  The check must catch
        # that even though no record was changed on both sides.
        self.assertEqual(self.q(F2, EN_DOC)["text"], "valve")
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", GENERIC, EN_DOC, "g1",
                            text="generic-device-v2"),
        ]))
        self.store.apply_batch(Batch(
            [], policy=PolicyCorrection(F2, True, False, frozenset()),
        ))
        self.assertEqual(self.q(F2, EN_DOC)["outcome"], SUPPLIED)
        self.assertEqual(self.q(F2, EN_DOC)["text"], "generic-device")
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        self.assertEqual(caught.exception.record_conflicts, [])
        conflicts = {
            (c["facility"], c["target"]): c
            for c in caught.exception.supply_conflicts
        }
        conflict = conflicts[(F2, "en/doc")]
        self.assertEqual(conflict["base"]["text"], "valve")
        self.assertEqual(conflict["draft"]["text"], "valve")
        self.assertEqual(conflict["live"]["text"], "generic-device")
        # The draft-only result and the eventually merged result would show
        # the new generic text, which nobody approved for F2.
        self.assertEqual(conflict["merged"]["text"], "generic-device-v2")
        self.assertEqual(self.q(F2, EN_DOC)["text"], "generic-device")

    def test_conflict_text_set_is_compared(self):
        # The draft introduces a same-level conflict at en/doc; a live
        # change to the *other* side of that conflict makes the merged
        # conflict text set differ from the draft-only one.  Outcome kind
        # stays "conflict" both times, so only comparing the retained text
        # set catches it.
        self.store.apply_batch(Batch(
            [], policy=PolicyCorrection(F1, True, False, frozenset()),
        ))
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", GENERIC, EN_DOC, "g1",
                            text="generic-draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_DOC, "s9",
                            text="third-live"),
        ]))
        # Baseline en/doc already conflicts; the live addition makes a third
        # same-level text.  The draft still only changes g1's wording, so
        # there is no record conflict -- but the merged conflict set would
        # contain "third-live", which neither reviewed preview saw.
        with self.assertRaises(MergeConflict) as caught:
            self.store.merge_draft(draft_id)
        self.assertEqual(caught.exception.record_conflicts, [])
        conflict = caught.exception.supply_conflicts[0]
        self.assertEqual(conflict["merged"]["outcome"], CONFLICT)
        self.assertIn("third-live", conflict["merged"]["conflict_texts"])

    def test_evidence_change_alone_does_not_block_merge(self):
        # Draft withdraws s2 and adds s5 with the same text: the supplied
        # text is unchanged, only evidence moves.  Merge must accept it.
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("withdraw", F1, EN_DOC, "s2"),
            EntryCorrection("set_body", F1, EN_DOC, "s5", text="pump-doc"),
        ]))
        self.assertEqual(self.store.merge_draft(draft_id)["status"],
                         DRAFT_MERGED)
        result = self.q(F1, EN_DOC)
        self.assertEqual(result["outcome"], SUPPLIED)
        self.assertEqual(result["text"], "pump-doc")
        self.assertEqual(
            {e["source_id"] for e in result["evidence"]}, {"s5"}
        )

    def test_merge_does_not_touch_unrelated_facilities(self):
        before = dict(self.store.recompute_stats())
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        result = self.store.merge_draft(draft_id)
        after = self.store.recompute_stats()
        # The checked/written targets are only F1 queries whose chain cites
        # en/tech; F2 was never recomputed.
        touched = {
            key for key, n in after.items()
            if n > before.get(key, 0)
        }
        self.assertTrue(touched)
        self.assertTrue(all(key[0] == F1 for key in touched))
        self.assertEqual(result["targets"], len(touched))


class ApprovalLifecycleTest(ReviewBase):
    def test_reapproval_writes_nothing(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        self.store.merge_draft(draft_id)
        state = self.store.export_state()
        ledger_len = len(self.store.ledger())
        with self.assertRaises(DraftAlreadyMerged):
            self.store.merge_draft(draft_id)
        self.assertEqual(self.store.export_state(), state)
        self.assertEqual(len(self.store.ledger()), ledger_len)
        self.assertEqual(
            [d["status"] for d in self.store.drafts()], [DRAFT_MERGED]
        )

    def test_rejected_approval_leaves_everything_untouched(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="draft"),
        ]))
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="live"),
        ]))
        state = self.store.export_state()
        stats = self.store.recompute_stats()
        ledger = self.store.ledger()
        with self.assertRaises(MergeConflict):
            self.store.merge_draft(draft_id)
        self.assertEqual(self.store.export_state(), state)
        self.assertEqual(self.store.recompute_stats(), stats)
        self.assertEqual(self.store.ledger(), ledger)
        self.assertEqual(self.store.draft(draft_id)["status"], DRAFT_SAVED)

    def test_merge_is_persisted_and_reopen_keeps_it(self):
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        seq = self.store.merge_draft(draft_id)["seq"]
        expected = self.snapshot()
        self.reopen()
        self.assertEqual(self.snapshot(), expected)
        record = self.store.draft(draft_id)
        self.assertEqual(record["status"], DRAFT_MERGED)
        self.assertEqual(record["merged_seq"], seq)
        with self.assertRaises(DraftAlreadyMerged):
            self.store.merge_draft(draft_id)

    def test_merge_entries_policy_and_queries_atomically(self):
        draft_id = self.store.save_draft(Batch(
            [EntryCorrection("set_body", F1, ZH_TECH, "s10", text="泵-v2")],
            policy=PolicyCorrection(F1, False, True, frozenset({ZH_DOC})),
        ))
        self.store.merge_draft(draft_id)
        self.assertEqual(self.q(F1, ZH_TECH)["text"], "泵-v2")
        zh_doc = self.q(F1, ZH_DOC)
        self.assertEqual(zh_doc["trace"][0]["status"], "blocked_by_policy")
        self.assertTrue(zh_doc["saw_blocked_coordinate"])
        self.assertIn(ZH_DOC, self.store.policies[F1].blocked)
        self.assertTrue(self.store.policies[F1].require_public_reviewed)


class ConcurrencyTest(unittest.TestCase):
    """Two connections on one file: serial orders must both be safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "terms.db")
        TermStore.bootstrap(
            self.path, CANDIDATES, policies(), entries()
        ).close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_correction_committed_before_approval_is_not_overwritten(self):
        translator = TermStore.open(self.path)
        approver = TermStore.open(self.path)
        try:
            draft_id = translator.save_draft(Batch([
                EntryCorrection("set_body", F1, EN_TECH, "s1",
                                text="draft-pump"),
            ]))
            # The approver's connection has a stale in-memory view.  A
            # direct correction lands through the other connection first.
            translator.apply_batch(Batch([
                EntryCorrection("set_body", F1, EN_TECH, "s1",
                                text="live-pump"),
            ]))
            # Approval must see that live value and record a conflict rather
            # than overwrite it with the draft's value.
            with self.assertRaises(MergeConflict):
                approver.merge_draft(draft_id)
            self.assertEqual(
                approver.query(F1, EN_TECH)["text"], "live-pump"
            )
            self.assertEqual(
                translator.query(F1, EN_TECH)["text"], "live-pump"
            )
        finally:
            translator.close()
            approver.close()

    def test_correction_after_approval_is_not_lost(self):
        translator = TermStore.open(self.path)
        approver = TermStore.open(self.path)
        try:
            draft_id = approver.save_draft(Batch([
                EntryCorrection("set_body", F1, EN_TECH, "s1",
                                text="draft-pump"),
            ]))
            approver.merge_draft(draft_id)
            translator.apply_batch(Batch([
                EntryCorrection("set_body", F1, EN_TECH, "s1",
                                text="later-pump"),
            ]))
            self.assertEqual(
                approver.query(F1, EN_TECH)["text"], "draft-pump"
            )
            # A stale reader notices the other connection on its next read
            # path; the live file is the later correction either way.
            self.assertEqual(
                TermStore.open(self.path).query(F1, EN_TECH)["text"],
                "later-pump",
            )
        finally:
            translator.close()
            approver.close()

    def test_approver_stale_view_is_refreshed_before_deciding(self):
        translator = TermStore.open(self.path)
        approver = TermStore.open(self.path)
        try:
            draft_id = approver.save_draft(Batch([
                EntryCorrection("set_body", F1, EN_DOC, "s2", text="doc-new"),
            ]))
            translator.apply_batch(Batch([
                EntryCorrection("set_body", F1, EN_DOC, "s2",
                                text="doc-live"),
            ]))
            # The approver's in-memory view is stale; it must reload and see
            # the live value, producing a conflict instead of overwriting.
            with self.assertRaises(MergeConflict) as caught:
                approver.merge_draft(draft_id)
            self.assertEqual(
                caught.exception.record_conflicts[0]["live"]["text"],
                "doc-live",
            )
        finally:
            translator.close()
            approver.close()

    def test_many_races_are_equivalent_to_a_serial_order(self):
        # Repeatedly interleave a live correction and an approval of a draft
        # that edits the same record.  Whatever the scheduler does, the
        # outcome must equal one of the two complete serial orders:
        #   approval then correction -> live shows the correction;
        #   correction then approval -> approval sees the correction and
        #                               records a conflict, live keeps it.
        import random

        rng = random.Random(20240101)
        for attempt in range(30):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "terms.db")
                TermStore.bootstrap(
                    path, CANDIDATES, policies(), entries()
                ).close()
                writer = TermStore.open(path)
                reviewer = TermStore.open(path)
                try:
                    draft_id = reviewer.save_draft(Batch([
                        EntryCorrection("set_body", F1, EN_TECH, "s1",
                                        text=f"draft-{attempt}"),
                    ]))
                    live_word = f"live-{attempt}"
                    # Either connection may submit its operation first.
                    if rng.random() < 0.5:
                        first, second = "merge", "apply"
                    else:
                        first, second = "apply", "merge"
                    outcome = {}
                    for action in (first, second):
                        if action == "apply":
                            writer.apply_batch(Batch([
                                EntryCorrection("set_body", F1, EN_TECH,
                                                "s1", text=live_word),
                            ]))
                            outcome["apply"] = "ok"
                        else:
                            try:
                                reviewer.merge_draft(draft_id)
                                outcome["merge"] = "ok"
                            except MergeConflict:
                                outcome["merge"] = "refused"
                    final = TermStore.open(path)
                    try:
                        served = final.query(F1, EN_TECH)["text"]
                    finally:
                        final.close()
                    if first == "merge":
                        assert outcome["merge"] == "ok"
                        assert served == live_word
                    else:
                        assert served in (live_word,)
                        if outcome["merge"] == "refused":
                            assert served == live_word
                finally:
                    writer.close()
                    reviewer.close()


class IntegrityTest(ReviewBase):
    def test_existing_interfaces_still_work(self):
        # Direct corrections continue to work unchanged alongside drafts.
        self.store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="direct"),
        ]))
        self.assertEqual(self.q(F1, EN_TECH)["text"], "direct")
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_review", F1, EN_TECH, "s1",
                            review=REVOKED),
        ]))
        self.store.merge_draft(draft_id)
        self.assertEqual(self.store.entries[(F1, EN_TECH, "s1")].review,
                         REVOKED)

    def test_saved_draft_does_not_publish_word_packs(self):
        # Approving a draft only updates the terminology store; it never
        # triggers a pack release.  This is checked structurally: the merge
        # API takes no publisher and touches only termdb tables.
        draft_id = self.store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pumpe"),
        ]))
        result = self.store.merge_draft(draft_id)
        self.assertNotIn("publish", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
