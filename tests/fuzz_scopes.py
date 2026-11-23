"""Randomized differential fuzzing.

Hundreds of random batches are applied.  After every batch:
  * every cached query must equal the independent from-scratch oracle,
    which would fail if the recompute scope ever missed an affected target;
  * the recompute counter must increase exactly on the predicted scope.
"""

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from termdb import (  # noqa: E402
    Batch,
    BatchError,
    Coord,
    Entry,
    EntryCorrection,
    GENERIC,
    Policy,
    PolicyCorrection,
    REVIEWED,
    REVOKED,
    TEXT,
    TermStore,
)
from oracle import oracle_resolve  # noqa: E402

COORDS = [
    Coord("en", "tech"), Coord("en", "doc"),
    Coord("zh", "tech"), Coord("zh", "doc"),
    Coord("de", "tech"),
]
CANDIDATES = {
    COORDS[0]: [COORDS[0], COORDS[1]],
    COORDS[1]: [COORDS[1], COORDS[0]],
    COORDS[2]: [COORDS[2], COORDS[0]],
    COORDS[3]: [COORDS[3], COORDS[1]],
    COORDS[4]: [COORDS[4], COORDS[3]],
}
FACILITIES = ["f-alpha", "f-beta", "f-gamma"]
SOURCES = ["s1", "s2", "s3", "s4"]
WORDS = ["pump", "valve", "泵", "ventil", "pump"]


def build(seed):
    rng = random.Random(seed)
    policies = {
        f: Policy(
            facility=f,
            allow_generic=rng.choice([True, False]),
            require_public_reviewed=rng.choice([True, False]),
            blocked=frozenset(
                rng.sample(COORDS, rng.randrange(0, 2))
            ),
        )
        for f in FACILITIES
    }
    entries = []
    for fac in FACILITIES + [GENERIC]:
        for coord in COORDS:
            if rng.random() < 0.35:
                continue
            entries.append(
                Entry(
                    facility=fac,
                    coord=coord,
                    source_id=rng.choice(SOURCES),
                    body_state=TEXT,
                    text=rng.choice(WORDS),
                    is_public=rng.random() < 0.5,
                    review=rng.choice([REVIEWED, REVOKED]),
                )
            )
    return rng, policies, entries


def expected_scope(store, facility, touched, generic_touched, policy_changed):
    universe = set(store.candidates)
    scope = set()
    reverse = {}
    # Mirror the store's universe exactly: every target coordinates maps to
    # itself, plus every chain edge target <- coordinate.
    for target, chain in store.candidates.items():
        reverse.setdefault(target, set()).add(target)
        for coord in chain:
            reverse.setdefault(coord, set()).add(target)
    affected = set()
    for coord in touched:
        affected |= reverse.get(coord, set())
    if policy_changed:
        for t in universe:
            scope.add((facility, t.key()))
    if generic_touched:
        # Mirrors the store: generic corrections fan out to every facility
        # whose policy allows generic words, even when the batch has no
        # concrete-facility correction (facility is then None).
        for fac, pol in store.policies.items():
            if not pol.allow_generic:
                continue
            if policy_changed and fac == facility:
                continue
            for target in affected:
                scope.add((fac, target.key()))
    if not policy_changed and facility is not None:
        for target in affected:
            scope.add((facility, target.key()))
    return scope


def run(seed, rounds=250):
    rng, policies, entries = build(seed)
    with tempfile.TemporaryDirectory() as tmp:
        store = TermStore.bootstrap(
            os.path.join(tmp, "fuzz.db"), CANDIDATES, policies, entries
        )
        for step in range(rounds):
            fac = rng.choice(FACILITIES)
            corrections = []
            touched = set()
            generic_touched = False
            for _ in range(rng.randrange(1, 4)):
                use_generic = rng.random() < 0.2
                owner = GENERIC if use_generic else fac
                coord = rng.choice(COORDS)
                source = rng.choice(SOURCES)
                kind = rng.choice(
                    ["set_body", "clear", "withdraw", "set_review"]
                )
                exists = (owner, coord, source) in store.entries
                if kind in ("withdraw", "set_review") and not exists:
                    kind = "set_body"
                corr = EntryCorrection(
                    kind=kind,
                    facility=owner,
                    coord=coord,
                    source_id=source,
                    text=rng.choice(WORDS),
                    is_public=rng.random() < 0.5,
                    review=rng.choice([REVIEWED, REVOKED]),
                )
                corrections.append(corr)
                touched.add(coord)
                generic_touched = generic_touched or use_generic

            new_policy = None
            policy_changed = False
            if rng.random() < 0.25:
                policy_changed = True
                new_policy = PolicyCorrection(
                    facility=fac,
                    allow_generic=rng.choice([True, False]),
                    require_public_reviewed=rng.choice([True, False]),
                    blocked=frozenset(rng.sample(COORDS, rng.randrange(0, 3))),
                )

            before = dict(store.recompute_stats())
            batch = Batch(corrections, new_policy)
            try:
                store.apply_batch(batch)
                accepted = True
            except BatchError:
                accepted = False

            # Full oracle parity after every attempt, rejected or accepted.
            state = store.export_state()
            for f in FACILITIES:
                for target in CANDIDATES:
                    assert store.query(f, target) == oracle_resolve(
                        state, f, target.key()
                    ), (seed, step, f, target.key())

            if accepted:
                # The batch has a concrete facility only if at least one
                # correction (or the policy) belongs to one.
                concrete = fac if (
                    any(c.facility != GENERIC for c in corrections)
                    or policy_changed
                ) else None
                scope = expected_scope(
                    store, concrete, touched, generic_touched, policy_changed
                )
                after = store.recompute_stats()
                for key in scope:
                    assert after.get(key, 0) == before.get(key, 0) + 1, (
                        seed, step, "missing recompute", key,
                    )
                for key, n in after.items():
                    if key not in scope:
                        assert n == before.get(key, 0), (
                            seed, step, "unexpected recompute", key,
                        )
            else:
                # Rejected batches never recompute anything.
                assert store.recompute_stats() == before, (seed, step)
        store.close()
    return True


if __name__ == "__main__":
    for seed in range(12):
        run(seed, rounds=400)
        print(f"seed {seed}: 400 batches OK")
    print("all fuzz rounds passed")
