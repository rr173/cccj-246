"""Reproducible review-draft save + approval example.

Run with:  python3 -I demo/review_demo.py            # temporary directory
           python3 -I demo/review_demo.py /tmp/keep  # keep the SQLite files

Only local Python interfaces are used.  The walkthrough prints, step by step:

  1. a reviewer saves a correction batch as a draft: live supply and the
     batch ledger are unchanged, and the draft has a stable id;
  2. the draft survives a reopen and can still be approved;
  3. a record the draft never touched keeps its live value;
  4. approval merges into live state (base/live/draft three-way); the draft
     is marked merged and approving again writes nothing;
  5. a divergent live correction makes a draft a record conflict, naming the
     baseline, draft and live values, and the live store is untouched;
  6. the worked supply hazard: a draft edits a generic word that the
     baseline policy forbids one facility to use; by approval time that
     facility may use generic words and currently shows the *old* text.
     Approving would silently switch its display, so the merge is refused
     with the four supply results spelled out;
  7. direct corrections, queries and word-pack publishing keep working; a
     draft approval never publishes a pack by itself.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    Coord,
    DraftAlreadyMerged,
    Entry,
    EntryCorrection,
    GENERIC,
    MergeConflict,
    Policy,
    PolicyCorrection,
    REVIEWED,
    TEXT,
    TermStore,
)

EN_TECH, EN_DOC = Coord("en", "tech"), Coord("en", "doc")
ZH_TECH = Coord("zh", "tech")

CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],
}

F1, F2 = "pump-hall", "gate"


def show(store, facility, target, title):
    result = store.query(facility, target)
    print(f"\n== {title} ==")
    print(f"  query   : {facility} / {target}")
    print(f"  outcome : {result['outcome']}")
    print(f"  text    : {result['text']!r}")
    print(f"  evidence: "
          f"{[e['facility'] + ':' + e['source_id'] for e in result['evidence']]}")
    return result


def build_store(db_path):
    return TermStore.bootstrap(
        db_path,
        CANDIDATES,
        {
            F1: Policy(F1, allow_generic=False, require_public_reviewed=False,
                        blocked=frozenset()),
            F2: Policy(F2, allow_generic=False, require_public_reviewed=False,
                        blocked=frozenset()),
        },
        [
            Entry(F1, EN_TECH, "s1", TEXT, "pump", False, REVIEWED),
            Entry(F1, EN_DOC, "s2", TEXT, "pump-doc", False, REVIEWED),
            Entry(F1, ZH_TECH, "s10", TEXT, "泵", False, REVIEWED),
            # F2 deliberately has no record of its own at en/doc, so its
            # display there comes only from the generic word once policy
            # allows it.
            Entry(GENERIC, EN_DOC, "g1", TEXT, "generic-device", False,
                  REVIEWED),
        ],
    )


def main(directory=None):
    owned = None
    if directory is None:
        owned = tempfile.TemporaryDirectory()
        directory = owned.name
    os.makedirs(directory, exist_ok=True)
    db_path = os.path.join(directory, "terms.db")

    store = build_store(db_path)
    try:
        seed = {
            (f, t.key()): store.query(f, t)
            for f in (F1, F2) for t in CANDIDATES
        }
        draft_id = store.save_draft(Batch([
            EntryCorrection("set_body", F1, ZH_TECH, "s10", text="泵-v2"),
        ]))
        print(f"1. saved draft #{draft_id}; live supply unchanged: "
              f"{store.query(F1, ZH_TECH)['text']!r}")
        print(f"   live queries unchanged: "
              f"{all(store.query(*k) == v for k, v in seed.items())}")

        store.close()
        store = TermStore.open(db_path)
        print(f"2. reopened; draft #{draft_id} status = "
              f"{store.draft(draft_id)['status']}")

        result = store.merge_draft(draft_id)
        print(f"3. approved: seq={result['seq']} records={result['records']} "
              f"targets={result['targets']}")
        show(store, F1, ZH_TECH, "after approval")
        print(f"   untouched F1/en/tech still "
              f"{store.query(F1, EN_TECH)['text']!r}")
        try:
            store.merge_draft(draft_id)
        except DraftAlreadyMerged:
            print("4. re-approving the same draft writes nothing")

        # -- record conflict -------------------------------------------------
        draft_id = store.save_draft(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pump-draft"),
        ]))
        store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_TECH, "s1", text="pump-live"),
        ]))
        try:
            store.merge_draft(draft_id)
        except MergeConflict as exc:
            conflict = exc.record_conflicts[0]
            print(f"\n5. record conflict at {conflict['facility']} / "
                  f"{conflict['coord']} / {conflict['source_id']}")
            print(f"   baseline = {conflict['base']['text']!r}")
            print(f"   draft    = {conflict['draft']['text']!r}")
            print(f"   live     = {conflict['live']['text']!r}")
            print(f"   live still serves {store.query(F1, EN_TECH)['text']!r}")

        # -- supply hazard: generic word, policy changed meanwhile -----------
        draft_id = store.save_draft(Batch([
            EntryCorrection("set_body", GENERIC, EN_DOC, "g1",
                            text="generic-device-v2"),
        ]))
        print(f"\n6. saved generic-word draft #{draft_id} while both "
              "facilities forbid generic words")
        print(f"   gate's en/doc display: "
              f"{store.query(F2, EN_DOC)['outcome']}")
        store.apply_batch(Batch(
            [], policy=PolicyCorrection(F2, True, False, frozenset()),
        ))
        print("   meanwhile live policy lets gate use generic words; it now "
              f"shows {store.query(F2, EN_DOC)['text']!r}")
        try:
            store.merge_draft(draft_id)
        except MergeConflict as exc:
            conflict = next(
                c for c in exc.supply_conflicts
                if c["facility"] == F2 and c["target"] == "en/doc"
            )
            print(f"   refused: no record conflict, but supply at "
                  f"{conflict['facility']} / {conflict['target']} would move")
            for label in ("base", "draft", "live", "merged"):
                shown = conflict[label]["text"]
                print(f"     {label:<7}= {shown!r} "
                      f"({conflict[label]['outcome']})")
            print("   gate still shows "
                  f"{store.query(F2, EN_DOC)['text']!r} -- approval cannot "
                  "smuggle in the new generic text")

        # -- direct correction and publishing still work ---------------------
        store.apply_batch(Batch([
            EntryCorrection("set_body", F1, EN_DOC, "s2", text="pump-doc-v3"),
        ]))
        show(store, F1, EN_DOC, "7. a direct correction still applies")

        print("\nDraft statuses:")
        for row in store.drafts():
            print(f"  #{row['draft_id']}: {row['status']}"
                  + (f" (seq {row['merged_seq']})"
                     if row["merged_seq"] is not None else ""))
        print(f"\ndemo artifacts under: {directory}")
        return directory
    finally:
        store.close()
        if owned is not None:
            owned.cleanup()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
