"""Randomized sync fuzzing (local publisher + device, no network).

Each round applies a random terminology batch, publishes a random subset of
facility/coordinate triples, and drives a device through sync, partial page
receipt, optional reopen and install.  After every install the device pack
must be exactly the target version's snapshot; the versions must stay fixed
across later releases and publisher reopens; a download's pages must never
change; and an install must never leave the device on a version newer than
the publisher's latest.

Run:  python3 -I tests/fuzz_sync.py
"""

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    Coord,
    Entry,
    EntryCorrection,
    Policy,
    REVIEWED,
    REVOKED,
    TEXT,
    TermStore,
)
from termdb.sync import (  # noqa: E402
    KEEP_VERSIONS,
    OfflinePublisher,
    RenegotiateError,
    ScreenDevice,
    SyncError,
)

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
FACILITIES = ["f-alpha", "f-beta"]
SOURCES = ["s1", "s2", "s3"]
WORDS = ["pump", "valve", "泵", "ventil"]


def build(rng, path):
    policies = {
        facility: Policy(facility, False, False, frozenset())
        for facility in FACILITIES
    }
    entries = [
        Entry(
            facility=facility,
            coord=rng.choice(COORDS),
            source_id=rng.choice(SOURCES),
            body_state=TEXT,
            text=rng.choice(WORDS),
            is_public=rng.random() < 0.3,
            review=rng.choice([REVIEWED, REVOKED]),
        )
        for facility in FACILITIES
        for _ in range(rng.randrange(1, 5))
    ]
    store = TermStore.bootstrap(
        os.path.join(path, "terms.db"), CANDIDATES, policies, entries
    )
    return store


def run(seed, rounds=60):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        store = build(rng, tmp)
        pub = OfflinePublisher(os.path.join(tmp, "pub.db"), store)
        dev_dir = os.path.join(tmp, "dev")
        dev = ScreenDevice.open(dev_dir, "chan", pub)
        pages_seen = {}          # download -> {page: body}
        try:
            for step in range(rounds):
                # Random terminology correction, possibly for one facility.
                facility = rng.choice(FACILITIES)
                coord = rng.choice(COORDS)
                source = rng.choice(SOURCES)
                kind = rng.choice(["set_body", "clear", "withdraw"])
                if kind == "withdraw" and (
                    (facility, coord, source) not in store.entries
                ):
                    kind = "set_body"
                try:
                    store.apply_batch(Batch([
                        EntryCorrection(
                            kind=kind,
                            facility=facility,
                            coord=coord,
                            source_id=source,
                            text=rng.choice(WORDS),
                            is_public=rng.random() < 0.3,
                            review=rng.choice([REVIEWED, REVOKED]),
                        )
                    ]))
                except Exception:
                    pass

                # Random release list: a subset of triples, sometimes with
                # an unknown coordinate (which must reject the release).
                valid = [
                    (fac, c.lang, c.ctx)
                    for fac in FACILITIES
                    for c in COORDS
                    if rng.random() < 0.6
                ]
                if not valid:
                    valid = [("f-alpha", "en", "tech")]
                triples = valid
                if rng.random() < 0.05:
                    triples = valid + [("f-alpha", "xx", "yy")]
                try:
                    pub.publish("chan", triples)
                except SyncError:
                    # An invalid release changes no version and no range.
                    pass
                triples = valid

                latest = pub.latest("chan")
                assert pub.retained_versions("chan") == list(
                    range(max(1, latest - KEEP_VERSIONS + 1), latest + 1)
                ), (seed, step)

                manifest = dev.sync()
                if manifest["mode"] == "none":
                    assert dev.current_version() == latest
                    continue

                target_entries = pub.version_meta(
                    "chan", manifest["target"]
                )["entries"]
                # Record pages as received so later re-fetches can be
                # compared for immutability.
                download = manifest["download"]
                for number in range(manifest["page_count"]):
                    if number not in pages_seen.setdefault(download, {}):
                        pages_seen[download][number] = pub.fetch_page(
                            download, number
                        )
                # Receive a random prefix/subset of pages, with duplicates.
                order = list(range(manifest["page_count"]))
                rng.shuffle(order)
                take = rng.randrange(0, len(order) + 1)
                for number in order[:take]:
                    page = pub.fetch_page(download, number)
                    assert page == pages_seen[download][number]
                    dev.accept_page(page)
                    if rng.random() < 0.4:
                        assert dev.accept_page(page)["status"] == "duplicate"

                if rng.random() < 0.2:
                    dev.close()
                    dev = ScreenDevice.open(dev_dir, "chan", pub)

                if not dev.complete(download):
                    try:
                        dev.install(download)
                    except SyncError:
                        pass
                    else:
                        raise AssertionError(
                            (seed, step, "incomplete install accepted")
                        )
                    continue

                expected_version = dev.current_version()
                try:
                    result = dev.install(download)
                except SyncError:
                    # A stale delta whose exact base is gone is refused, and
                    # must not change the pack.
                    assert dev.current_version() == expected_version
                    continue
                if result["status"] == "noop":
                    assert dev.current_version() == expected_version
                    continue

                version = result["version"]
                assert version == manifest["target"], (seed, step)
                if manifest["target"] == pub.latest("chan"):
                    assert version <= latest
                # The pack is exactly the version snapshot.
                expected = target_entries
                assert dev.content() == expected, (seed, step, version)
                # Version bytes are immutable across later releases.
                if rng.random() < 0.5:
                    pub.publish("chan", triples)
                assert dev.content() == expected, (seed, step, "mutated")
            return True
        finally:
            dev.close()
            pub.close()
            store.close()


if __name__ == "__main__":
    for seed in range(10):
        run(seed, rounds=60)
        print(f"seed {seed}: 60 sync rounds OK")
    print("all sync fuzz rounds passed")
