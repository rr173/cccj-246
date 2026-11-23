"""Runnable small reference: the five required correction checks, end to end.

Run with:  python3 -I demo/demo.py

It builds a fresh SQLite database under a temporary directory, prints the
current wording for each query, applies one correction batch per scenario
and shows how the answer changes.  No external translation and no unbounded
history are involved — only the current records and the finite batch ledger.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (
    Batch,
    CLEAR,
    Coord,
    Entry,
    EntryCorrection,
    GENERIC,
    Policy,
    PolicyCorrection,
    REVIEWED,
    REVOKED,
    SUPPLIED,
    TEXT,
    TermStore,
)

EN_TECH, EN_DOC = Coord("en", "tech"), Coord("en", "doc")
ZH_TECH, ZH_DOC = Coord("zh", "tech"), Coord("zh", "doc")
DE_TECH = Coord("de", "tech")

CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],
    ZH_DOC: [ZH_DOC, EN_DOC],
    DE_TECH: [DE_TECH, ZH_DOC],
}

F1, F2 = "plant-a", "plant-b"


def show(store, facility, target, title):
    r = store.query(facility, target)
    print(f"\n== {title} ==")
    print(f"  query    : {facility} / {target}")
    print(f"  outcome  : {r['outcome']}")
    print(f"  text     : {r['text']!r}")
    print(f"  winner at: {r['winning_coord']}")
    print(f"  evidence : "
          f"{[e['facility'] + ':' + e['source_id'] for e in r['evidence']]}")
    print("  chain    : " + " -> ".join(
        f"{n['coord']}[{n['status']}]" for n in r["trace"]
    ))
    return r


def main():
    tmp = tempfile.TemporaryDirectory()
    db = os.path.join(tmp.name, "terms.db")
    store = TermStore.bootstrap(
        db,
        CANDIDATES,
        {
            F1: Policy(F1, True, False, frozenset()),
            F2: Policy(F2, False, True, frozenset({EN_DOC})),
        },
        [
            Entry(F1, EN_TECH, "src-1", TEXT, "pump", False, REVIEWED),
            Entry(F1, EN_DOC, "src-2", TEXT, "pump-documentation",
                  False, REVIEWED),
            Entry(F1, ZH_TECH, "src-10", TEXT, "泵", False, REVIEWED),
            Entry(F2, EN_TECH, "src-20", TEXT, "valve", True, REVOKED),
            Entry(GENERIC, EN_DOC, "gen-1", TEXT, "generic-device",
                  False, REVIEWED),
        ],
    )

    show(store, F1, ZH_TECH, "baseline: zh/tech supplied by its own word")

    # 1. explicit clear stops fallback; withdrawal only removes one source.
    store.apply_batch(Batch([
        EntryCorrection("clear", F1, ZH_TECH, "src-10",
                        is_public=False, review=REVIEWED),
    ]))
    show(store, F1, ZH_TECH, "1a. explicit clear: fallback stopped")

    store.apply_batch(Batch([
        EntryCorrection("set_body", F1, ZH_TECH, "src-11", text="泵甲",
                        is_public=False, review=REVIEWED),
    ]))
    store.apply_batch(Batch([
        EntryCorrection("withdraw", F1, ZH_TECH, "src-11"),
        EntryCorrection("withdraw", F1, ZH_TECH, "src-10"),
    ]))
    show(store, F1, ZH_TECH,
         "1b. withdrawals remove sources only; fallback reaches en/tech")

    # 2. approve and revoke review in a single batch.
    store.apply_batch(Batch([
        EntryCorrection("set_review", F2, EN_TECH, "src-20",
                        review=REVIEWED),
        EntryCorrection("set_body", F2, EN_TECH, "src-21", text="valve-x",
                        is_public=True, review=REVIEWED),
        EntryCorrection("set_review", F2, EN_TECH, "src-21",
                        review=REVOKED),
    ]))
    show(store, F2, EN_TECH,
         "2. same batch: src-20 approved, src-21 review revoked")

    # 3. formerly missing high-priority candidate becomes the winner.
    show(store, F1, DE_TECH, "3a. de/tech has no usable word")
    store.apply_batch(Batch([
        EntryCorrection("set_body", F1, DE_TECH, "src-30", text="Pumpe",
                        is_public=False, review=REVIEWED),
    ]))
    show(store, F1, DE_TECH,
         "3b. newly added highest-priority candidate wins")

    # 4. same-level conflict is retained; lower sources never tried.
    store.apply_batch(Batch([
        EntryCorrection("set_body", F1, EN_TECH, "src-3",
                        text="pump-unit", is_public=False, review=REVIEWED),
    ]))
    r = show(store, F1, EN_TECH,
             "4. same-level conflict retained, en/doc not tried")
    assert r["conflict_texts"] == ["pump", "pump-unit"]

    # 5. same text, different source: evidence must change.
    store.apply_batch(Batch([
        EntryCorrection("set_body", F1, EN_TECH, "src-3",
                        text="pump", is_public=False, review=REVIEWED),
        EntryCorrection("withdraw", F1, EN_TECH, "src-1"),
        EntryCorrection("set_body", F1, EN_TECH, "src-4", text="pump",
                        is_public=True, review=REVIEWED),
    ]))
    r = show(store, F1, EN_TECH,
             "5. text unchanged, source changed -> evidence updated")
    assert r["outcome"] == SUPPLIED and r["text"] == "pump"
    assert {e["source_id"] for e in r["evidence"]} == {"src-3", "src-4"}

    # Reopen: same answers, same finite ledger.
    store.close()
    reopened = TermStore.open(db)
    assert reopened.query(F1, EN_TECH)["text"] == "pump"
    print("\nreopened ledger:")
    for row in reopened.ledger():
        n = len(row["detail"].get("entries", []))
        print(f"  seq={row['seq']} status={row['status']} "
              f"entries={n} error={row['error']!r}")
    print(f"\nreopen OK: {len(reopened.ledger())} ledger rows, "
          "answers identical")
    reopened.close()
    tmp.cleanup()


if __name__ == "__main__":
    main()
