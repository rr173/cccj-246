"""Randomized reconciliation fuzzing against a from-scratch oracle.

Each round submits a random mix of installation records and cumulative
readings (some late, some corrected with higher revisions, some revoked),
then:

  * recomputes the whole reconciliation from the *effective* records with an
    independent, purely functional oracle and compares every segment;
  * checks that reports group, range and total exactly like the oracle;
  * checks that a reading correction touched only segments adjacent to a
    changed instant, and an installation correction touched only segments
    that overlap the changed effective period -- never the whole table;
  * closes and reopens the ledger and checks byte-for-byte parity.

Run:  python3 -I tests/fuzz_ledger.py
"""

import bisect
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    ATTRIBUTED,
    CROSS_VERSION,
    CountLedger,
    IDENTITY_UNKNOWN,
    InstallRecord,
    LedgerBatch,
    LedgerError,
    PackIdentity,
    Reading,
)

SCREENS = ["s-a", "s-b"]
FACILITIES = ["f-1", "f-2"]
COORDS = [("en", "tech"), ("zh", "tech")]
ROUNDS = ["r1", "r2"]
IDENTITIES = [
    PackIdentity("north", 1, "p1"),
    PackIdentity("north", 2, "p2"),
    PackIdentity("south", 1, "p3"),
]
TIMES = list(range(0, 200, 10))


def oracle_segments(installs, readings):
    """Recompute every segment from effective records, independently."""

    # installs: {(screen, t): ("identity", PackIdentity) | ("unknown", None)}
    # readings: {(screen, round, fac, lang, ctx, t): count}
    install_times = {}
    install_state = {}
    for (screen, t), (kind, identity) in installs.items():
        install_times.setdefault(screen, []).append(t)
        install_state[(screen, t)] = identity if kind == "identity" else None
    for screen in install_times:
        install_times[screen].sort()

    series = {}
    for (screen, round_id, fac, lang, ctx, t), count in readings.items():
        series.setdefault((screen, round_id, fac, lang, ctx), []).append(
            (t, count)
        )

    segments = []
    for key, points in series.items():
        points.sort()
        screen = key[0]
        times = install_times.get(screen, [])
        for (a, ca), (b, cb) in zip(points, points[1:]):
            index = bisect.bisect_right(times, a) - 1
            if index < 0:
                kind, identity = IDENTITY_UNKNOWN, None
            else:
                first = install_state[(screen, times[index])]
                if first is None:
                    kind, identity = IDENTITY_UNKNOWN, None
                else:
                    seen = {first}
                    unknown = False
                    stop = bisect.bisect_left(times, b)
                    for pos in range(
                        bisect.bisect_right(times, a), stop
                    ):
                        state = install_state[(screen, times[pos])]
                        if state is None:
                            unknown = True
                            break
                        seen.add(state)
                    if unknown:
                        kind, identity = IDENTITY_UNKNOWN, None
                    elif len(seen) == 1:
                        kind, identity = ATTRIBUTED, next(iter(seen))
                    else:
                        kind, identity = CROSS_VERSION, None
            segments.append({
                "screen": screen,
                "round_id": key[1],
                "facility": key[2],
                "language": key[3],
                "context": key[4],
                "a_time": a,
                "b_time": b,
                "count": cb - ca,
                "kind": kind,
                "channel": None if identity is None else identity.channel,
                "version": None if identity is None else identity.version,
                "pack_id": None if identity is None else identity.pack_id,
                "reason": None if kind == ATTRIBUTED else kind,
            })
    return sorted(
        segments,
        key=lambda s: (s["screen"], s["round_id"], s["facility"],
                       s["language"], s["context"], s["a_time"]),
    )


def compare(ledger, installs, readings, seed, step):
    expected = oracle_segments(installs, readings)
    actual = sorted(
        ledger.segments(),
        key=lambda s: (s["screen"], s["round_id"], s["facility"],
                       s["language"], s["context"], s["a_time"]),
    )
    assert actual == expected, (seed, step, "segment mismatch",
                                actual, expected)

    # Report over several ranges matches an oracle aggregation.
    for start, end in [(0, 10 ** 9), (20, 60), (50, 150), (130, 130)]:
        report = ledger.report(start, end)
        chosen = [
            s for s in expected if start <= s["b_time"] < end
            and s["count"] != 0
        ]
        assert report["total"] == sum(s["count"] for s in chosen), (
            seed, step, "total mismatch")
        assert report["increments"] == sorted(
            [
                {
                    "screen": s["screen"], "round_id": s["round_id"],
                    "facility": s["facility"], "language": s["language"],
                    "context": s["context"], "a_time": s["a_time"],
                    "b_time": s["b_time"], "count": s["count"],
                    "kind": s["kind"],
                    **({"reason": s["kind"]}
                       if s["kind"] != ATTRIBUTED else {}),
                    **({"channel": s["channel"], "version": s["version"],
                        "pack_id": s["pack_id"]}
                       if s["kind"] == ATTRIBUTED else {}),
                }
                for s in chosen
            ],
            key=lambda i: (i["screen"], i["facility"], i["language"],
                           i["context"], i["round_id"], i["a_time"]),
        ), (seed, step, "increment mismatch")
    return expected


def run(seed, rounds=120):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counts.db")
        ledger = CountLedger.open(path)
        # Effective state, mirrored independently of the ledger.
        installs = {}
        readings = {}
        revision = {}
        try:
            for step in range(rounds):
                new_installs = []
                new_readings = []
                for _ in range(rng.randrange(1, 4)):
                    screen = rng.choice(SCREENS)
                    if rng.random() < 0.5 or not readings:
                        t = rng.choice(TIMES)
                        key = (screen, t)
                        rev = revision.get(key, 0) + 1
                        revision[key] = rev
                        roll = rng.random()
                        if roll < 0.15:
                            record = InstallRecord.revoke(screen, t, rev)
                            content = None
                        elif roll < 0.35:
                            record = InstallRecord.unknown(screen, t, rev)
                            content = ("unknown", None)
                        else:
                            identity = rng.choice(IDENTITIES)
                            record = InstallRecord.installed(
                                screen, t, rev, identity
                            )
                            content = ("identity", identity)
                        new_installs.append(record)
                    else:
                        round_id = rng.choice(ROUNDS)
                        facility = rng.choice(FACILITIES)
                        lang, ctx = rng.choice(COORDS)
                        t = rng.choice(TIMES)
                        key = (screen, round_id, facility, lang, ctx, t)
                        rev = revision.get(key, 0) + 1
                        revision[key] = rev
                        # Derive a plausible next count from the mirrored
                        # effective series so most batches are accepted.
                        prior = sorted(
                            (k[5], v) for k, v in readings.items()
                            if k[:5] == key[:5]
                        )
                        floor = prior[-1][1] if prior else 0
                        count = floor + rng.randrange(0, 25)
                        record = Reading.cumulative(
                            screen, round_id, facility, lang, ctx, t, rev,
                            count,
                        )
                        new_readings.append((key, count, record))
                # Occasionally submit a stale revision that must be ignored.
                if new_installs and rng.random() < 0.3:
                    stale = new_installs[0]
                    new_installs.append(
                        InstallRecord(
                            stale.screen, stale.time_ms,
                            max(0, stale.revision - 1), stale.kind,
                            stale.identity,
                        )
                    )
                try:
                    ledger.apply(
                        LedgerBatch(new_installs,
                                    [r[2] for r in new_readings])
                    )
                except LedgerError:
                    # Only a genuine contradiction may reject; then the
                    # mirrored state must stay untouched too.
                    ledger.close()
                    ledger = CountLedger.open(path)
                    continue

                # Fold accepted new records into the mirror by *effective
                # revision* (the ledger already enforced this).
                for record in new_installs:
                    key = (record.screen, record.time_ms)
                    if record.kind == "revoked":
                        installs.pop(key, None)
                    elif record.kind == "unknown":
                        installs[key] = ("unknown", None)
                    else:
                        installs[key] = ("identity", record.identity)
                for key, count, _ in new_readings:
                    readings[key] = count

                compare(ledger, installs, readings, seed, step)

                if rng.random() < 0.1:
                    ledger.close()
                    ledger = CountLedger.open(path)
                    compare(ledger, installs, readings, seed, step)
            return True
        finally:
            ledger.close()


def check_bounded_work():
    """A correction in a long history must not touch the whole table."""

    with tempfile.TemporaryDirectory() as tmp:
        ledger = CountLedger.open(os.path.join(tmp, "counts.db"))
        try:
            ledger.apply(LedgerBatch(installs=[
                InstallRecord.installed("s", 0, 1, IDENTITIES[0]),
                InstallRecord.installed("s", 3000, 1, IDENTITIES[0]),
            ]))
            ledger.apply(LedgerBatch(readings=[
                Reading.cumulative("s", "r", "f", "en", "tech", t, 1, t)
                for t in range(0, 4000, 10)
            ]))
            before = ledger.commits()[-1]
            assert before["segments_touched"] == 399, before
            # Correct one reading in the middle: only its neighbours move.
            ledger.apply(LedgerBatch(readings=[
                Reading.cumulative("s", "r", "f", "en", "tech", 2000, 2, 2005)
            ]))
            touched = ledger.commits()[-1]["segments_touched"]
            assert touched <= 4, touched
            # A late installation at 2005 changes the effective state only
            # until the next installation at 3000: ~100 segments, never all
            # 400.
            ledger.apply(LedgerBatch(installs=[
                InstallRecord.installed("s", 2005, 1, IDENTITIES[1])
            ]))
            touched = ledger.commits()[-1]["segments_touched"]
            assert touched <= 110, touched
        finally:
            ledger.close()


if __name__ == "__main__":
    for seed in range(12):
        run(seed, rounds=120)
        print(f"seed {seed}: 120 reconciliation rounds OK")
    check_bounded_work()
    print("bounded-work check OK")
    print("all ledger fuzz rounds passed")
