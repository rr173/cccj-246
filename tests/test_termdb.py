"""End-to-end checks against a small, hand-built reference world.

The scenario covers, in order:
  1. explicit clear vs withdrawal (clear stops fallback; withdrawal falls back)
  2. review approval and review revocation inside one batch
  3. a formerly missing high-priority candidate becoming the winner
  4. same-level text conflict retained, lower sources never tried
  5. unchanged output text with a changed source -> evidence updated
plus: scoped recomputation, atomic rejection of bad batches and reopen parity
with the independent reference oracle in ``tests/oracle.py``.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from termdb import (  # noqa: E402
    Batch,
    BatchError,
    BLOCKED,
    CLEAR,
    CONFLICT,
    Coord,
    Entry,
    EntryCorrection,
    EXPLICIT_CLEAR,
    GENERIC,
    NO_AVAILABLE,
    Policy,
    PolicyCorrection,
    REVIEWED,
    REVOKED,
    SUPPLIED,
    TEXT,
    TermStore,
    WITHDRAWN,
)
from oracle import oracle_resolve  # noqa: E402

# Coordinates ---------------------------------------------------------------
EN_TECH = Coord("en", "tech")
EN_DOC = Coord("en", "doc")
ZH_TECH = Coord("zh", "tech")
ZH_DOC = Coord("zh", "doc")
DE_TECH = Coord("de", "tech")

TARGETS = [EN_TECH, EN_DOC, ZH_TECH, ZH_DOC, DE_TECH]

# Fixed, explicit, priority-ordered, non-recursive candidate coordinates.
CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],      # may point at another language
    ZH_DOC: [ZH_DOC, EN_DOC],
    # de/tech's own coordinate has no seed records at all, and its fallback
    # zh/doc is empty too: the first candidate is genuinely "no usable word"
    # until a correction adds one.
    DE_TECH: [DE_TECH, ZH_DOC],
}

F1 = "facility-one"
F2 = "facility-two"


def seed_policies():
    return {
        F1: Policy(
            facility=F1,
            allow_generic=True,
            require_public_reviewed=False,
            blocked=frozenset(),
        ),
        F2: Policy(
            facility=F2,
            allow_generic=False,                  # generic words forbidden
            require_public_reviewed=True,         # public words need review
            blocked=frozenset({EN_DOC}),          # this fallback is forbidden
        ),
    }


def seed_entries():
    return [
        # facility-one ----------------------------------------------------
        Entry(F1, EN_TECH, "s1", TEXT, "pump", False, REVIEWED),
        Entry(F1, EN_DOC, "s2", TEXT, "pump-doc", False, REVIEWED),
        Entry(F1, ZH_TECH, "s10", TEXT, "泵", False, REVIEWED),
        # de/tech s3 deliberately absent: added later at de/tech so a
        # formerly missing high-priority candidate becomes the winner.
        # en/tech s4 deliberately absent: added later to create a conflict.
        # zh/tech s11 deliberately absent: added then withdrawn to show that
        # withdrawal removes only that source and fallback continues.
        # facility-two ----------------------------------------------------
        Entry(F2, EN_TECH, "s20", TEXT, "valve", True, REVOKED),
        Entry(F2, EN_DOC, "s21", TEXT, "valve-doc", False, REVIEWED),
        # generic pseudo-facility ----------------------------------------
        Entry(GENERIC, EN_DOC, "g1", TEXT, "generic-device", False, REVIEWED),
    ]


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "terms.db")
        self.store = TermStore.bootstrap(
            self.db_path, CANDIDATES, seed_policies(), seed_entries()
        )
        self.assert_oracle_parity()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- helpers

    def q(self, facility, target):
        return self.store.query(facility, target)

    def stats_snapshot(self):
        return self.store.recompute_stats()

    def assert_oracle_parity(self):
        state = self.store.export_state()
        for facility in state["policies"]:
            for target_key in state["candidates"]:
                cached = self.store.query(facility, target_key)
                expected = oracle_resolve(state, facility, target_key)
                self.assertEqual(
                    cached,
                    expected,
                    msg=f"oracle mismatch {facility}/{target_key}",
                )

    # ---------------------------------------------------------- seed state

    def test_seed_state_three_no_word_kinds(self):
        # supplied
        r = self.q(F1, EN_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "pump")
        self.assertEqual(r["winning_coord"], "en/tech")
        self.assertEqual(r["evidence"][0]["source_id"], "s1")

        # no_available: de/tech has no record at any candidate coordinate
        r = self.q(F1, DE_TECH)
        self.assertEqual(r["outcome"], NO_AVAILABLE)
        self.assertFalse(r["saw_blocked_coordinate"])

        # blocked: F2's only usable coordinate en/doc is policy-blocked and
        # en/tech has an un-reviewed public entry, so every coordinate is
        # disqualified.  Blocking and review are eligibility gates, not words.
        r = self.q(F2, EN_DOC)
        self.assertEqual(r["outcome"], BLOCKED)
        self.assertTrue(r["saw_blocked_coordinate"])
        statuses = [n["status"] for n in r["trace"]]
        self.assertEqual(statuses[0], "blocked_by_policy")
        self.assertEqual(
            r["trace"][1]["sources"][0]["reason"], "public_not_reviewed"
        )

        # F2 must never see generic words.
        self.assertNotIn(
            GENERIC,
            {s["facility"] for n in r["trace"] for s in n["sources"]},
        )

        # F1 allows generic words: at en/doc its own s2 and the generic g1
        # both supply, with different wording, which is a same-level conflict.
        r = self.q(F1, EN_DOC)
        self.assertEqual(r["outcome"], CONFLICT)
        self.assertEqual(r["winning_coord"], "en/doc")
        self.assertEqual(
            r["conflict_texts"], ["generic-device", "pump-doc"]
        )
        facs = {s["facility"] for n in r["trace"] for s in n["sources"]}
        self.assertIn(GENERIC, facs)

        # No recomputation happened during bootstrap reads.
        self.assertEqual(sum(self.stats_snapshot().values()), 0)

    # -------------------------------------- 1. clear vs withdrawal differ

    def test_1_clear_stops_fallback_withdrawal_continues(self):
        before = self.stats_snapshot()

        # Explicit clear at the highest-priority coordinate of zh/tech.
        batch = Batch(
            entry_corrections=[
                EntryCorrection(
                    "clear", F1, ZH_TECH, "s10", is_public=False,
                    review=REVIEWED,
                )
            ]
        )
        self.store.apply_batch(batch)
        r = self.q(F1, ZH_TECH)
        self.assertEqual(r["outcome"], EXPLICIT_CLEAR)
        self.assertEqual(r["text"], "")
        self.assertEqual(r["winning_coord"], "zh/tech")
        # The lower en/tech coordinate must not have been tried.
        self.assertEqual(r["trace"][1]["status"], "not_reached")

        # Seed a zh/tech record first so that withdrawing it leaves a
        # lower-coordinate record to fall back to.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection(
                        "set_body", F1, ZH_TECH, "s11", text="泵A",
                        is_public=False, review=REVIEWED,
                    )
                ]
            )
        )
        self.assertEqual(self.q(F1, ZH_TECH)["text"], "泵A")

        # Withdrawal removes only that source; fallback continues and
        # reaches the clear record left by the first batch.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("withdraw", F1, ZH_TECH, "s11")
                ]
            )
        )
        r = self.q(F1, ZH_TECH)
        self.assertEqual(r["outcome"], EXPLICIT_CLEAR)
        self.assertEqual(r["winning_coord"], "zh/tech")
        withdrawn = next(
            s for n in r["trace"] for s in n["sources"]
            if s["source_id"] == "s11"
        )
        self.assertEqual(withdrawn["body_state"], WITHDRAWN)
        self.assertFalse(withdrawn["eligible"])

        # Withdrawal with no other usable record keeps falling all the way.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("withdraw", F1, ZH_TECH, "s10")
                ]
            )
        )
        r = self.q(F1, ZH_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "pump")          # from en/tech
        self.assertEqual(r["winning_coord"], "en/tech")

        # Scoped recomputation: only queries whose chain visits zh/tech
        # (zh/tech itself) were touched; unrelated targets stayed untouched.
        after = self.stats_snapshot()
        for key, n in after.items():
            if key == (F1, "zh/tech"):
                self.assertGreater(n, before.get(key, 0))
            else:
                self.assertEqual(n, before.get(key, 0), msg=key)
        self.assert_oracle_parity()

    # ------------------------- 2. approve and revoke review in one batch

    def test_2_same_batch_review_approval_and_revocation(self):
        # Before: F2 cannot use its public en/tech word (review revoked).
        self.assertEqual(self.q(F2, EN_TECH)["outcome"], BLOCKED)

        # Approve s20 while simultaneously revoking review on a second
        # public record added in the very same batch.
        batch = Batch(
            entry_corrections=[
                EntryCorrection("set_review", F2, EN_TECH, "s20",
                                review=REVIEWED),
                EntryCorrection("set_body", F2, EN_TECH, "s22",
                                text="valve-new", is_public=True,
                                review=REVIEWED),
                EntryCorrection("set_review", F2, EN_TECH, "s22",
                                review=REVOKED),
            ]
        )
        self.store.apply_batch(batch)
        r = self.q(F2, EN_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "valve")
        self.assertEqual(r["evidence"][0]["source_id"], "s20")
        s22 = next(
            s for s in r["trace"][0]["sources"] if s["source_id"] == "s22"
        )
        self.assertFalse(s22["eligible"])
        self.assertEqual(s22["reason"], "public_not_reviewed")

        # Review state changes eligibility only: language/context unchanged.
        state = self.store.export_state()
        s22_rec = next(
            e for e in state["entries"]
            if e["facility"] == F2 and e["source_id"] == "s22"
        )
        self.assertEqual(s22_rec["coord"], "en/tech")
        self.assert_oracle_parity()

    # ----------------- 3. missing high-priority candidate becomes winner

    def test_3_formerly_missing_high_priority_candidate_wins(self):
        # de/tech's own (highest-priority) coordinate has no record at all;
        # the lower en/doc coordinate is empty for F1, so there is no word.
        self.assertEqual(self.q(F1, DE_TECH)["outcome"], NO_AVAILABLE)
        self.assertEqual(
            [n["coord"] for n in self.q(F1, DE_TECH)["trace"]],
            ["de/tech", "zh/doc"],
        )

        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, DE_TECH, "s3",
                                    text="pumpe-de", is_public=False,
                                    review=REVIEWED)
                ]
            )
        )
        # The formerly missing highest-priority candidate now wins.
        r = self.q(F1, DE_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "pumpe-de")
        self.assertEqual(r["winning_coord"], "de/tech")
        self.assertEqual(r["evidence"][0]["source_id"], "s3")
        # The lower en/doc coordinate was never tried once de/tech supplied.
        self.assertEqual(r["trace"][1]["status"], "not_reached")

        # Only targets whose candidate chain visits de/tech recomputed:
        # de/tech is referenced by nobody else, so exactly one target moved.
        touched = {
            k for k, n in self.stats_snapshot().items()
            if n > 0 and k[0] == F1
        }
        self.assertEqual(touched, {(F1, "de/tech")})
        # F2 was not recomputed: the batch belongs to F1.
        self.assertTrue(
            all(k[0] != F2 for k in self.stats_snapshot())
        )
        self.assert_oracle_parity()

    # ------------------- 4. same-level conflict retained, no lower try

    def test_4_same_level_conflict_is_retained(self):
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, EN_TECH, "s4",
                                    text="pump-CONFLICT", is_public=False,
                                    review=REVIEWED)
                ]
            )
        )
        r = self.q(F1, EN_TECH)
        self.assertEqual(r["outcome"], CONFLICT)
        self.assertIsNone(r["text"])
        self.assertEqual(r["conflict_texts"], ["pump", "pump-CONFLICT"])
        self.assertEqual(r["winning_coord"], "en/tech")
        # Lower-priority en/doc must never be tried when the level conflicts.
        self.assertEqual(r["trace"][1]["status"], "not_reached")

        # The conflict stays local to queries that actually reach en/tech.
        # zh/tech stops at its own supplied coordinate, so it is unaffected.
        self.assertEqual(self.q(F1, EN_TECH)["outcome"], CONFLICT)
        r = self.q(F1, ZH_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "泵")
        self.assertEqual(r["winning_coord"], "zh/tech")
        self.assert_oracle_parity()

    # ------------------- 5. text unchanged, source changed -> evidence

    def test_5_same_text_different_source_updates_evidence(self):
        # First align s4's wording so en/tech supplies one text backed by
        # two sources.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, EN_TECH, "s4",
                                    text="pump", is_public=False,
                                    review=REVIEWED)
                ]
            )
        )
        r = self.q(F1, EN_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(
            {e["source_id"] for e in r["evidence"]}, {"s1", "s4"}
        )

        # Withdraw s1 and add s5 with the identical text: output text must
        # stay the same but the checkable evidence must change.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("withdraw", F1, EN_TECH, "s1"),
                    EntryCorrection("set_body", F1, EN_TECH, "s5",
                                    text="pump", is_public=True,
                                    review=REVIEWED),
                ]
            )
        )
        r = self.q(F1, EN_TECH)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "pump")
        ids = {e["source_id"] for e in r["evidence"]}
        self.assertEqual(ids, {"s4", "s5"})
        self.assertNotIn("s1", ids)
        s5 = next(e for e in r["evidence"] if e["source_id"] == "s5")
        self.assertTrue(s5["is_public"])
        self.assertEqual(s5["review"], REVIEWED)
        self.assert_oracle_parity()

    # ----------------------------------------------- policy-only batches

    def test_6_policy_change_recomputes_only_governed_contexts(self):
        before = self.stats_snapshot()

        new_policy = PolicyCorrection(
            facility=F2,
            allow_generic=False,
            require_public_reviewed=True,
            blocked=frozenset(),          # unblock en/doc
        )
        self.store.apply_batch(Batch(entry_corrections=[], policy=new_policy))

        # en/doc record s21 now becomes usable.
        r = self.q(F2, EN_DOC)
        self.assertEqual(r["outcome"], SUPPLIED)
        self.assertEqual(r["text"], "valve-doc")

        # Every F2 target was recomputed; no F1 target moved.
        after = self.stats_snapshot()
        for target in TARGETS:
            key = (F2, target.key())
            self.assertEqual(after[key], before.get(key, 0) + 1)
            f1 = (F1, target.key())
            self.assertEqual(after.get(f1, 0), before.get(f1, 0))
        self.assert_oracle_parity()

    # --------------------------------------------- generic-word correction

    def test_7_generic_correction_only_reaches_facilities_allowing_it(self):
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", GENERIC, EN_DOC, "g1",
                                    text="generic-device-v2",
                                    is_public=False, review=REVIEWED)
                ]
            )
        )
        # At en/doc the generic wording is updated; the trace is the
        # checkable evidence even though both words still conflict there.
        r = self.q(F1, EN_DOC)
        gen = next(
            s for s in r["trace"][0]["sources"] if s["facility"] == GENERIC
        )
        self.assertEqual(gen["text"], "generic-device-v2")

        # F2 forbids generic fallback: no generic source appears anywhere.
        r = self.q(F2, EN_DOC)
        facs = {
            s["facility"] for n in r["trace"] for s in n["sources"]
        }
        self.assertNotIn(GENERIC, facs)

        # Recompute scope: only generic-allowing facilities, only targets
        # whose chain actually contains en/doc.
        touched = {k for k, n in self.stats_snapshot().items() if n > 0}
        self.assertEqual(
            touched,
            {(F1, "en/doc"), (F1, "en/tech"), (F1, "zh/doc")},
        )
        self.assertNotIn((F1, "zh/tech"), touched)
        self.assertNotIn((F1, "de/tech"), touched)
        self.assertTrue(all(k[0] != F2 for k in touched))
        self.assert_oracle_parity()

    # --------------------------------------------- bad batch atomicity

    def test_8_bad_batch_leaves_prior_version_serving(self):
        good_result = self.q(F1, EN_TECH)
        before = self.stats_snapshot()
        ledger_len = len(self.store.ledger())

        bad = Batch(
            entry_corrections=[
                EntryCorrection("set_body", F1, EN_TECH, "s9",
                                text="should-not-land", is_public=False,
                                review=REVIEWED),
                EntryCorrection("withdraw", F1, EN_TECH, "does-not-exist"),
            ]
        )
        with self.assertRaises(BatchError):
            self.store.apply_batch(bad)

        # Prior version still serves; nothing recomputed; ledger records the
        # rejection but no state changed.
        self.assertEqual(self.q(F1, EN_TECH), good_result)
        self.assertEqual(self.stats_snapshot(), before)
        self.assertEqual(len(self.store.ledger()), ledger_len + 1)
        self.assertEqual(self.store.ledger()[-1]["status"], "rejected")
        state = self.store.export_state()
        self.assertFalse(
            any(
                e["source_id"] == "s9" and e["text"] == "should-not-land"
                for e in state["entries"]
            )
        )
        self.assert_oracle_parity()

    # ------------------------------------------------------- reopen parity

    def test_9_reopen_serves_identical_results_and_ledger(self):
        # Run a few accepted and one rejected batch first.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, EN_DOC, "s6",
                                    text="pump-doc-v2", is_public=False,
                                    review=REVIEWED)
                ]
            )
        )
        with self.assertRaises(BatchError):
            self.store.apply_batch(
                Batch(
                    entry_corrections=[
                        EntryCorrection("set_body", F1, EN_DOC, "s9",
                                        text="", is_public=False,
                                        review=REVIEWED)
                    ]
                )
            )
        expected_ledger = self.store.ledger()
        expected_stats = self.stats_snapshot()
        expected_state = self.store.export_state()
        expected_results = {
            (f, t.key()): self.q(f, t)
            for f in (F1, F2) for t in TARGETS
        }
        self.store.close()

        reopened = TermStore.open(self.db_path)
        try:
            self.assertEqual(reopened.export_state(), expected_state)
            self.assertEqual(reopened.ledger(), expected_ledger)
            self.assertEqual(reopened.recompute_stats(), expected_stats)
            for key, payload in expected_results.items():
                self.assertEqual(reopened.query(*key), payload)

            # The reopened database keeps accepting corrections, still with
            # scoped recomputation.
            reopened.apply_batch(
                Batch(
                    entry_corrections=[
                        EntryCorrection("set_review", F2, EN_TECH, "s20",
                                        review=REVIEWED)
                    ]
                )
            )
            state = reopened.export_state()
            for facility in state["policies"]:
                for target_key in state["candidates"]:
                    self.assertEqual(
                        reopened.query(facility, target_key),
                        oracle_resolve(state, facility, target_key),
                    )
        finally:
            reopened.close()


class EdgeRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "edge.db")
        self.store = TermStore.bootstrap(
            self.db_path, CANDIDATES, seed_policies(), seed_entries()
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ineligible_clear_does_not_stop_fallback(self):
        # A public clear whose review is revoked is ineligible; the chain
        # must continue to the lower coordinate instead of stopping.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("clear", F2, EN_TECH, "s20",
                                    is_public=True, review=REVOKED)
                ]
            )
        )
        r = self.store.query(F2, EN_TECH)
        # en/tech yields nothing eligible; en/doc is blocked for F2, so the
        # result is blocked — but crucially en/doc was reached and evaluated,
        # not marked not_reached.
        self.assertEqual(r["outcome"], BLOCKED)
        self.assertEqual(r["trace"][0]["status"], "no_eligible_record")
        self.assertEqual(r["trace"][1]["status"], "blocked_by_policy")

    def test_batch_may_not_span_two_facilities(self):
        with self.assertRaises(BatchError):
            self.store.apply_batch(
                Batch(
                    entry_corrections=[
                        EntryCorrection("set_body", F1, EN_TECH, "a",
                                        text="x", is_public=False,
                                        review=REVIEWED),
                        EntryCorrection("set_body", F2, EN_TECH, "b",
                                        text="y", is_public=False,
                                        review=REVIEWED),
                    ]
                )
            )

    def test_unknown_coordinate_rejected(self):
        with self.assertRaises(BatchError):
            self.store.apply_batch(
                Batch(
                    entry_corrections=[
                        EntryCorrection(
                            "set_body", F1, Coord("fr", "tech"), "a",
                            text="x", is_public=False, review=REVIEWED,
                        )
                    ]
                )
            )

    def test_candidate_coordinates_are_not_recursive_by_construction(self):
        # A candidate coordinate is consumed as-is; even if it names another
        # target, the engine never expands it.  zh/tech's chain contains
        # en/tech, whose own chain contains en/doc — but resolving zh/tech
        # must show exactly its own two coordinates, never en/doc.
        r = self.store.query(F1, ZH_TECH)
        self.assertEqual(
            [n["coord"] for n in r["trace"]], ["zh/tech", "en/tech"]
        )

    # ---- regressions surfaced by randomized differential fuzzing ----

    def test_regression_generic_correction_with_policy_batch_fans_out(self):
        # A batch carrying BOTH a facility policy correction and a generic
        # word correction must still fan the generic change out to every
        # other facility whose policy allows generic words.  The policy
        # branch must not short-circuit the generic scope.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("clear", GENERIC, EN_TECH, "g9",
                                    is_public=False, review=REVIEWED)
                ],
                policy=PolicyCorrection(
                    facility=F2,
                    allow_generic=False,
                    require_public_reviewed=True,
                    blocked=frozenset(),
                ),
            )
        )
        state = self.store.export_state()
        # F1 allows generic words: its en/tech query has its own s1 text,
        # and the generic g9 clear must be visible in the same-coordinate
        # trace rather than a stale record.
        r = self.store.query(F1, EN_TECH)
        g9 = next(
            s for s in r["trace"][0]["sources"] if s["source_id"] == "g9"
        )
        self.assertEqual(g9["body_state"], CLEAR)
        self.assertEqual(
            r, oracle_resolve(state, F1, "en/tech")
        )

    def test_regression_two_corrections_on_one_record_in_one_batch(self):
        # set_body followed by set_review on the same record key must apply
        # both: the final record keeps the new text AND the new review state.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, EN_TECH, "s7",
                                    text="pump-new", is_public=True,
                                    review=REVOKED),
                    EntryCorrection("set_review", F1, EN_TECH, "s7",
                                    review=REVIEWED),
                ]
            )
        )
        state = self.store.export_state()
        rec = next(
            e for e in state["entries"]
            if e["facility"] == F1 and e["source_id"] == "s7"
        )
        self.assertEqual(rec["text"], "pump-new")
        self.assertEqual(rec["review"], REVIEWED)
        self.assertTrue(rec["is_public"])
        self.assertEqual(
            self.store.query(F1, EN_TECH),
            oracle_resolve(state, F1, "en/tech"),
        )

    def test_regression_coordinate_used_only_as_a_target(self):
        # Correcting a coordinate that appears as a query target but in no
        # other chain still recomputes that target's own query.
        self.store.apply_batch(
            Batch(
                entry_corrections=[
                    EntryCorrection("set_body", F1, DE_TECH, "s8",
                                    text="drossel", is_public=False,
                                    review=REVIEWED)
                ]
            )
        )
        self.assertEqual(self.store.query(F1, DE_TECH)["text"], "drossel")
        self.assertEqual(
            self.store.recompute_stats().get((F1, "de/tech"), 0), 1
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
