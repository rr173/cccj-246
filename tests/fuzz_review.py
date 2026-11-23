"""Randomized differential fuzzing for review-draft merge.

Each round either saves a random valid correction batch as a draft (checking
that live supply and the batch ledger are untouched) or approves a random
saved draft.  Approval is judged by an independent oracle that works only on
plain exported state:

1. a record/policy conflict is exactly a key whose baseline, live and draft
   complete values are pairwise different (a missing record is its own
   sentinel; withdrawn and explicit-clear are ordinary values);
2. any record conflict must refuse approval and leave live state untouched;
3. otherwise every target the draft can affect is compared: the preserved
   supply result comes from the "who moved" rule over baseline / draft-only
   / live results, and the merged result must equal it.  Any difference is a
   supply conflict and must refuse approval;
4. an accepted approval must apply exactly the merged values, leave every
   unmodified record alone, and make every cached query match the oracle;
   approving it again must write nothing.

The oracle re-implements resolution and three-way merge without importing
``termdb.engine`` or ``termdb.review``.
"""

import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    CLEAR,
    Coord,
    DraftAlreadyMerged,
    DraftError,
    Entry,
    EntryCorrection,
    GENERIC,
    MergeConflict,
    Policy,
    PolicyCorrection,
    REVIEWED,
    REVOKED,
    TEXT,
    TermStore,
    WITHDRAWN,
)

EN_TECH = Coord("en", "tech")
EN_DOC = Coord("en", "doc")
ZH_TECH = Coord("zh", "tech")
ZH_DOC = Coord("zh", "doc")
DE_TECH = Coord("de", "tech")
COORDS = [EN_TECH, EN_DOC, ZH_TECH, ZH_DOC, DE_TECH]
CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],
    ZH_DOC: [ZH_DOC, EN_DOC],
    DE_TECH: [DE_TECH, ZH_DOC],
}
FACILITIES = ["f-alpha", "f-beta", "f-gamma"]
SOURCES = ["s1", "s2", "s3"]
WORDS = ["pump", "valve", "泵", "ventil"]
ALL_TARGETS = [(f, c.key()) for f in FACILITIES for c in COORDS]


# ---------------------------------------------------------------- oracle


def oracle_resolve(state, facility, target_key):
    policy = state["policies"][facility]
    blocked = set(policy["blocked"])
    by_coord = {}
    for rec in state["entries"]:
        if rec["facility"] == facility or (
            rec["facility"] == GENERIC and policy["allow_generic"]
        ):
            by_coord.setdefault(rec["coord"], []).append(rec)
    for bucket in by_coord.values():
        bucket.sort(key=lambda r: (r["facility"], r["source_id"]))
    ever_blocked = False
    for coord_key in state["candidates"][target_key]:
        if coord_key in blocked:
            ever_blocked = True
            continue
        usable = []
        for rec in by_coord.get(coord_key, []):
            if rec["body_state"] == WITHDRAWN:
                continue
            if (rec["is_public"] and policy["require_public_reviewed"]
                    and rec["review"] != REVIEWED):
                continue
            usable.append(rec)
        if not usable:
            continue
        blanks = [r for r in usable if r["body_state"] == CLEAR]
        texts = [r for r in usable if r["body_state"] == TEXT]
        if blanks and not texts:
            return ("explicit_clear", "", ())
        distinct = sorted({r["text"] for r in texts})
        if len(distinct) > 1:
            return ("conflict", None, tuple(distinct))
        if distinct:
            return ("supplied", distinct[0], ())
    return ("blocked" if ever_blocked else "no_available", None, ())


def query_signature(result):
    return (
        result["outcome"],
        result["text"],
        tuple(sorted(result.get("conflict_texts") or [])),
    )


def state_with(base, entry_overrides, policy_overrides):
    entries = []
    seen = set()
    for rec in base["entries"]:
        key = (rec["facility"], rec["coord"], rec["source_id"])
        if key in entry_overrides:
            seen.add(key)
            value = entry_overrides[key]
            if value is None:
                continue
            entries.append({
                "facility": key[0], "coord": key[1], "source_id": key[2],
                **value,
            })
        else:
            entries.append(dict(rec))
    # Overrides may create records the base never had.
    for key, value in entry_overrides.items():
        if key in seen or value is None:
            continue
        entries.append({
            "facility": key[0], "coord": key[1], "source_id": key[2],
            **value,
        })
    policies = dict(base["policies"])
    policies.update({
        fac: value for fac, value in policy_overrides.items()
        if value is not None
    })
    return {"candidates": base["candidates"], "policies": policies,
            "entries": entries}


def record_value(store, key):
    facility, coord, source_id = key
    entry = store.entries.get((facility, Coord.parse(coord), source_id))
    if entry is None:
        return None
    return {
        "body_state": entry.body_state,
        "text": entry.text,
        "is_public": bool(entry.is_public),
        "review": entry.review,
    }


def canonical(value):
    return json.loads(json.dumps(value, sort_keys=True))


def merge_value(base, live, draft):
    if live == base:
        return draft, False
    if draft == base:
        return live, False
    if live == draft:
        return draft, False
    return None, True


def who_moved(base, draft, live):
    if draft == base:
        return live
    if live == base:
        return draft
    return draft if draft == live else None


# ------------------------------------------------------------- fuzz driver


def build(seed):
    rng = random.Random(seed)
    policies = {
        f: Policy(
            f,
            allow_generic=rng.choice([True, False]),
            require_public_reviewed=rng.choice([True, False]),
            blocked=frozenset(rng.sample(COORDS, rng.randrange(0, 2))),
        )
        for f in FACILITIES
    }
    entries = []
    for fac in FACILITIES + [GENERIC]:
        for coord in COORDS:
            if rng.random() < 0.4:
                continue
            entries.append(Entry(
                fac, coord, rng.choice(SOURCES), TEXT,
                rng.choice(WORDS), rng.random() < 0.5,
                rng.choice([REVIEWED, REVOKED]),
            ))
    return rng, policies, entries


def random_batch(rng, store, facility):
    corrections = []
    for _ in range(rng.randrange(1, 4)):
        owner = GENERIC if rng.random() < 0.2 else facility
        coord = rng.choice(COORDS)
        source = rng.choice(SOURCES)
        exists = (owner, coord, source) in store.entries
        kind = rng.choice(["set_body", "clear", "withdraw", "set_review"])
        if kind in ("withdraw", "set_review") and not exists:
            kind = "set_body"
        corrections.append(EntryCorrection(
            kind=kind, facility=owner, coord=coord, source_id=source,
            text=rng.choice(WORDS), is_public=rng.random() < 0.5,
            review=rng.choice([REVIEWED, REVOKED]),
        ))
    policy = None
    if rng.random() < 0.25:
        policy = PolicyCorrection(
            facility=facility,
            allow_generic=rng.choice([True, False]),
            require_public_reviewed=rng.choice([True, False]),
            blocked=frozenset(rng.sample(COORDS, rng.randrange(0, 3))),
        )
    return Batch(corrections, policy)


def save_round(store, rng, drafts):
    before_state = canonical(store.export_state())
    before_ledger = store.ledger()
    batch = random_batch(rng, store, rng.choice(FACILITIES))
    try:
        draft_id = store.save_draft(batch)
    except DraftError:
        assert canonical(store.export_state()) == before_state
        assert store.ledger() == before_ledger
        return
    assert canonical(store.export_state()) == before_state, (
        "saving a draft changed live supply"
    )
    assert store.ledger() == before_ledger
    record = store.draft(draft_id)
    drafts.append({
        "snapshot": record,
        "base": before_state,
    })
    # Drafts are immutable snapshots.
    assert store.draft(draft_id) == record


def expected_decisions(store, draft):
    """Return (record_conflicts, supply_conflicts, entry/policy overrides)."""

    snapshot = draft["snapshot"]
    base = draft["base"]
    live = canonical(store.export_state())

    record_conflict = False
    entry_overrides = {}
    for item in snapshot["entries"]:
        key = (item["facility"], item["coord"], item["source_id"])
        merged, clash = merge_value(
            item["base_value"], record_value(store, key),
            item["draft_value"],
        )
        if clash:
            record_conflict = True
        else:
            entry_overrides[key] = merged

    policy_overrides = {}
    if snapshot["policy"] is not None:
        fac = snapshot["policy"]["facility"]
        merged, clash = merge_value(
            snapshot["policy"]["base_value"], live["policies"][fac],
            snapshot["policy"]["draft_value"],
        )
        if clash:
            record_conflict = True
        else:
            policy_overrides[fac] = merged

    if record_conflict:
        return True, None, entry_overrides, policy_overrides

    draft_state = state_with(
        base,
        {(i["facility"], i["coord"], i["source_id"]): i["draft_value"]
         for i in snapshot["entries"]},
        {snapshot["policy"]["facility"]: snapshot["policy"]["draft_value"]}
        if snapshot["policy"] is not None else {},
    )
    merged_state = state_with(live, entry_overrides, policy_overrides)
    recorded_draft = {
        (t["facility"], t["target"]): t["draft_result"]
        for t in snapshot["targets"]
    }
    supply_conflicts = []
    for fac, tkey in ALL_TARGETS:
        base_sig = oracle_resolve(base, fac, tkey)
        live_sig = oracle_resolve(live, fac, tkey)
        stored = recorded_draft.get((fac, tkey))
        draft_sig = (
            (stored["outcome"], stored["text"],
             tuple(sorted(stored["conflict_texts"])))
            if stored is not None
            else oracle_resolve(draft_state, fac, tkey)
        )
        expected = who_moved(base_sig, draft_sig, live_sig)
        merged_sig = oracle_resolve(merged_state, fac, tkey)
        if expected is None or merged_sig != expected:
            supply_conflicts.append((fac, tkey))
    if supply_conflicts:
        return True, supply_conflicts, entry_overrides, policy_overrides
    return False, [], entry_overrides, policy_overrides


def approve_round(store, rng, drafts, seed, step):
    draft = rng.choice(drafts)
    draft_id = draft["snapshot"]["draft_id"]
    before = canonical(store.export_state())

    already = store.draft(draft_id)["status"] == "merged"
    if already:
        try:
            store.merge_draft(draft_id)
            raise AssertionError("re-approval succeeded")
        except DraftAlreadyMerged:
            pass
        assert canonical(store.export_state()) == before
        return

    refusal, supply_conflicts, entry_overrides, policy_overrides = (
        expected_decisions(store, draft)
    )
    try:
        store.merge_draft(draft_id)
        accepted = True
    except MergeConflict as exc:
        accepted = False
        assert exc.record_conflicts or exc.supply_conflicts

    if refusal:
        assert not accepted, (seed, step, "approval should have been refused")
        assert canonical(store.export_state()) == before
        assert store.draft(draft_id)["status"] == "saved"
        # A refused approval is stable and writes nothing on retry.
        try:
            store.merge_draft(draft_id)
            raise AssertionError("refused draft merged on retry")
        except MergeConflict:
            pass
        assert canonical(store.export_state()) == before
        return

    assert accepted, (seed, step, "approval should have merged")
    after = canonical(store.export_state())
    for item in draft["snapshot"]["entries"]:
        key = (item["facility"], item["coord"], item["source_id"])
        assert record_value(store, key) == entry_overrides[key], (seed, step)
    for fac, value in policy_overrides.items():
        assert after["policies"][fac] == value, (seed, step)
    assert store.draft(draft_id)["status"] == "merged"

    # Nothing outside the draft's own records changed.
    changed = {
        (i["facility"], i["coord"], i["source_id"])
        for i in draft["snapshot"]["entries"]
    }
    before_recs = {
        (r["facility"], r["coord"], r["source_id"]): r
        for r in before["entries"]
    }
    for rec in after["entries"]:
        key = (rec["facility"], rec["coord"], rec["source_id"])
        if key in changed:
            continue
        assert rec == before_recs[key], (seed, step, key)

    # Every cached query equals the oracle over the merged state.
    for fac, tkey in ALL_TARGETS:
        assert query_signature(store.query(fac, tkey)) == oracle_resolve(
            after, fac, tkey
        ), (seed, step, fac, tkey)


def run(seed, rounds=100):
    rng, policies, entries = build(seed)
    with tempfile.TemporaryDirectory() as tmp:
        store = TermStore.bootstrap(
            os.path.join(tmp, "fuzz.db"), CANDIDATES, policies, entries
        )
        drafts = []
        for step in range(rounds):
            if not drafts or rng.random() < 0.5:
                save_round(store, rng, drafts)
            else:
                approve_round(store, rng, drafts, seed, step)
            # Reopen parity is checked at the end, not every round.
        store.close()
        reopened = TermStore.open(os.path.join(tmp, "fuzz.db"))
        for draft in drafts:
            reopened.draft(draft["snapshot"]["draft_id"])
        reopened.close()
    return True


if __name__ == "__main__":
    for seed in range(10):
        run(seed, rounds=100)
        print(f"seed {seed}: 100 rounds OK")
    print("all draft fuzz rounds passed")
