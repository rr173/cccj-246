"""Reproducible offline-sync example, in the same style as demo/demo.py.

Run with:  python3 -I demo/sync_demo.py

Every step uses only local interfaces -- a local release publisher and a local
screen device; there is no network service and no screen UI.  The script
prints the protocol facts that matter for an operator:

  1. a release snapshots one committed state and pushes display text only;
  2. a mid-download disconnect resumes the very same download;
  3. a correction never rewrites an already published version;
  4. a retained base gets a net delta; an evicted base is told to renegotiate;
  5. only three versions stay syncable per channel;
  6. corrupt / missing pages never install, and a retry resumes;
  7. after installation the pack is readable offline across a reopen.

A fresh SQLite store, publisher database and device directory are created in a
temporary directory; pass a directory argument to keep the artifacts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    Batch,
    Coord,
    Entry,
    EntryCorrection,
    Policy,
    REVIEWED,
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

EN_TECH, EN_DOC = Coord("en", "tech"), Coord("en", "doc")
ZH_TECH, ZH_DOC = Coord("zh", "tech"), Coord("zh", "doc")

CANDIDATES = {
    EN_TECH: [EN_TECH, EN_DOC],
    EN_DOC: [EN_DOC, EN_TECH],
    ZH_TECH: [ZH_TECH, EN_TECH],
    ZH_DOC: [ZH_DOC, EN_DOC],
}
F1, F2 = "pump-hall", "gate"
COORDS = [EN_TECH, EN_DOC, ZH_TECH, ZH_DOC]
CHANNEL = "north-wing"


def line(title):
    print(f"\n--- {title} ---")


def main():
    import tempfile

    directory = sys.argv[1] if len(sys.argv) > 1 else None
    own_tmp = None
    if directory is None:
        own_tmp = tempfile.TemporaryDirectory()
        directory = own_tmp.name
    os.makedirs(directory, exist_ok=True)

    store = TermStore.bootstrap(
        os.path.join(directory, "terms.db"),
        CANDIDATES,
        {f: Policy(f, False, False, frozenset()) for f in (F1, F2)},
        [
            Entry(F1, EN_TECH, "src-1", TEXT, "pump", False, REVIEWED),
            Entry(F1, EN_DOC, "src-2", TEXT, "pump-documentation",
                  False, REVIEWED),
            Entry(F1, ZH_TECH, "src-10", TEXT, "泵", False, REVIEWED),
            Entry(F2, EN_TECH, "src-20", TEXT, "valve", False, REVIEWED),
            Entry(F2, ZH_TECH, "src-21", TEXT, "阀", False, REVIEWED),
        ],
    )
    pub = OfflinePublisher(os.path.join(directory, "publisher.db"), store)
    dev = ScreenDevice.open(
        os.path.join(directory, "screen"), CHANNEL, pub
    )
    release = [
        (facility, coord.lang, coord.ctx)
        for facility in (F1, F2)
        for coord in COORDS
    ]

    def receive(device, manifest):
        for number in range(manifest["page_count"]):
            device.accept_page(
                pub.fetch_page(manifest["download"], number)
            )

    try:
        line("1. release v1: one committed state, display text only")
        published = pub.publish(CHANNEL, release)
        print(f"  version    : {published['version']}")
        print(f"  entries    : {published['entries']}")
        print(f"  pack id    : {published['pack_id'][:16]}...")
        entries = pub.version_meta(CHANNEL, 1)["entries"]
        print(f"  fields sent: {sorted(entries)[:2]} ...")
        print("  (no source ids, review flags or evidence travel in a pack)")

        line("2. first sync; the screen disconnects before page 0")
        start = dev.sync()
        print(f"  mode       : {start['mode']}")
        print(f"  pages      : {start['page_count']}")
        print(f"  download   : {start['download'][:16]}...")
        print(f"  digest     : {start['digest'][:16]}...")

        line("3. reconnect: same download identity, pages unchanged")
        resumed = dev.sync()
        assert resumed["download"] == start["download"]
        receive(dev, resumed)
        first_install = dev.install(resumed["download"])
        print(f"  install    : {first_install['status']} "
              f"v{first_install['version']}")
        print(f"  offline F1/en/tech -> {dev.lookup(F1, EN_TECH)!r}")

        line("4. correct wording; already-published v1 is untouched")
        v1_page = pub.fetch_page(start["download"], 0)
        for facility, coord, old_source, new_source, new_text in [
            (F1, EN_TECH, "src-1", "src-3", "pump-unit"),
            (F1, ZH_TECH, "src-10", "src-11", "泵甲"),
            (F2, EN_TECH, "src-20", "src-22", "valve-unit"),
            (F2, ZH_TECH, "src-21", "src-23", "阀甲"),
        ]:
            store.apply_batch(Batch([
                EntryCorrection("withdraw", facility, coord, old_source),
                EntryCorrection("set_body", facility, coord, new_source,
                                text=new_text, is_public=False,
                                review=REVIEWED),
            ]))
        assert pub.fetch_page(start["download"], 0) == v1_page
        print("  v1 page bytes identical after correction:",
              pub.fetch_page(start["download"], 0) == v1_page)

        line("5. retained base -> net delta, not a full pack")
        pub.publish(CHANNEL, release)
        delta = dev.sync()
        print(f"  mode       : {delta['mode']} (base={delta['base']}, "
              f"target={delta['target']})")
        items = [
            item
            for number in range(delta["page_count"])
            for item in pub.fetch_page(delta["download"], number)["items"]
        ]
        print(f"  delta items: {len(items)}")
        receive(dev, delta)
        applied = dev.install(delta["download"])
        print(f"  install    : {applied['status']} v{applied['version']}")
        print(f"  offline F1/en/tech -> {dev.lookup(F1, EN_TECH)!r}")

        line("6. same-text source swap sends no delta line at all")
        store.apply_batch(Batch([
            EntryCorrection("withdraw", F1, EN_TECH, "src-3"),
            EntryCorrection("set_body", F1, EN_TECH, "src-4", text="pump-unit",
                            is_public=True, review=REVIEWED),
        ]))
        pub.publish(CHANNEL, release)
        empty = dev.sync()
        print(f"  mode       : {empty['mode']}, pages={empty['page_count']}")
        receive(dev, empty)
        empty_install = dev.install(empty["download"])
        print(f"  install    : {empty_install['status']} "
              f"v{empty_install['version']} "
              f"(no page changed, still switched version)")

        line(f"7. retention: only the recent {KEEP_VERSIONS} versions survive")
        for _ in range(3):
            next_version = pub.latest(CHANNEL) + 1
            store.apply_batch(Batch([
                EntryCorrection("set_body", F2, ZH_TECH, "src-23",
                                text=f"阀-{next_version}", is_public=False,
                                review=REVIEWED),
            ]))
            pub.publish(CHANNEL, release)
        print(f"  retained   : {pub.retained_versions(CHANNEL)}")
        try:
            pub.negotiate(CHANNEL, 1)
        except RenegotiateError as exc:
            print(f"  base v1    : renegotiate ({exc})")
            fresh = pub.negotiate(CHANNEL, None)
            print(f"  fallback   : {fresh['mode']} to v{fresh['target']}")

        line("8. corrupt and incomplete downloads never install")
        screen2 = ScreenDevice.open(
            os.path.join(directory, "screen-2"), CHANNEL, pub
        )
        try:
            manifest = screen2.sync()
            if manifest["page_count"]:
                page = pub.fetch_page(manifest["download"], 0)
                page["items"] = [{
                    "facility": F1, "coord": "en/tech", "text": "tampered",
                }]
                try:
                    screen2.accept_page(page)
                except SyncError as exc:
                    print(f"  corrupt    : refused ({exc})")
            try:
                screen2.install(manifest["download"])
            except SyncError as exc:
                print(f"  incomplete : refused ({exc})")
            print("  installed  :", screen2.installed_version())
            receive(screen2, manifest)
            print("  retry      :",
                  screen2.install(manifest["download"])["status"])
        finally:
            screen2.close()

        line("9. reopen offline: no publisher, still readable")
        dev.close()
        dev = ScreenDevice.open(
            os.path.join(directory, "screen"), CHANNEL, publisher=None
        )
        print(f"  version    : {dev.current_version()}")
        print(f"  F1/en/tech : {dev.lookup(F1, EN_TECH)!r}")
        print(f"  F2/zh/tech : {dev.lookup(F2, ZH_TECH)!r}")

        print(f"\nartifacts under: {directory}")
    finally:
        dev.close()
        pub.close()
        store.close()
        if own_tmp is not None:
            own_tmp.cleanup()


if __name__ == "__main__":
    main()
