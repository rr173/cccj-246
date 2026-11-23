"""Reproducible display-count reconciliation walkthrough (purely local).

Run with:  python3 -I demo/ledger_demo.py

It answers the venue's question -- "how many term displays did the old word
pack actually account for, before we push an upgrade?" -- using only local
interfaces: a ledger file, screen telemetry supplied as Python records, and
no network, publisher or screen page.  The script prints:

  1. a first batch of installations and cumulative readings;
  2. a report grouped by screen / term triple / full pack identity;
  3. a *late* installation record that changes historical attribution,
     including an interval whose two endpoints lie outside the changed
     period but whose interior crosses it;
  4. a late reading that splits one interval and re-attributes the parts;
  5. an explicit unknown stretch, reported separately as identity-unknown;
  6. a revocation, a rejected conflicting batch and a rejected backwards
     reading, each leaving the ledger and reconciled result untouched;
  7. reopen parity: the report after reopening is identical.

Pass a directory argument to keep the SQLite ledger for inspection.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termdb import (  # noqa: E402
    CountLedger,
    InstallRecord,
    LedgerBatch,
    LedgerError,
    Reading,
)

SCREEN = "lobby"
FAC = "pump-hall"
LANG, CTX = "en", "tech"
OLD = ("north", 3, "pack-old-3")
NEW = ("north", 4, "pack-new-4")


def line(title):
    print(f"\n--- {title} ---")


def show_report(report, note=""):
    if note:
        print(f"  {note}")
    for row in report["attributed"]:
        print(
            f"  attributed   {row['screen']} {row['facility']}/"
            f"{row['language']}/{row['context']}  "
            f"{row['channel']} v{row['version']} "
            f"{row['pack_id']}  = {row['count']}"
        )
    for row in report["unattributed"]:
        print(
            f"  unattributed {row['screen']} {row['facility']}/"
            f"{row['language']}/{row['context']}  "
            f"{row['reason']}  = {row['count']}"
        )
    print(
        f"  totals       attributed={report['attributed_total']} "
        f"unattributed={report['unattributed_total']} "
        f"total={report['total']}"
    )


def main():
    import tempfile

    directory = sys.argv[1] if len(sys.argv) > 1 else None
    own_tmp = None
    if directory is None:
        own_tmp = tempfile.TemporaryDirectory()
        directory = own_tmp.name
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "counts.db")
    ledger = CountLedger.open(path)

    try:
        line("1. first batch: installation facts + cumulative readings")
        ledger.apply(LedgerBatch(
            installs=[InstallRecord.installed(SCREEN, 0, 1, OLD)],
            readings=[
                Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                                   1000, 1, 0),
                Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                                   2000, 1, 120),
                Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                                   3000, 1, 300),
            ],
        ))
        report = ledger.report()
        show_report(report, "intervals [1000,2000) and [2000,3000)")

        line("2. a LATE installation record changes history")
        # v4 was actually installed at 1500, in the interior of the first
        # interval [1000, 2000).  That interval now crosses old->new and is
        # cross-version; [2000, 3000) now sees only v4 and stays attributed.
        ledger.apply(LedgerBatch(installs=[
            InstallRecord.installed(SCREEN, 1500, 1, NEW),
        ]))
        show_report(ledger.report(),
                    "late v4 at 1500: [1000,2000) crosses, [2000,3000) "
                    "is v4-only")

        line("3. a LATE reading splits an interval and re-attributes it")
        # A previously missing reading at 2500 arrives; the old [2000, 3000)
        # is replaced by [2000, 2500) and [2500, 3000).
        ledger.apply(LedgerBatch(readings=[
            Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                               2500, 2, 200),
        ]))
        report = ledger.report()
        show_report(report, "reading at 2500 splits [2000,3000)")
        print("  increments (right-time ordered):")
        for inc in report["increments"]:
            print(
                f"    [{inc['a_time']},{inc['b_time']}) round={inc['round_id']}"
                f" +{inc['count']} -> {inc['kind']}"
            )

        line("4. an explicit unknown stretch is reported separately")
        # A second screen whose identity is unknown from the start.
        ledger.apply(LedgerBatch(
            installs=[InstallRecord.unknown("annex", 0, 1)],
            readings=[
                Reading.cumulative("annex", "round-1", "gate", "zh", "tech",
                                   1000, 1, 0),
                Reading.cumulative("annex", "round-1", "gate", "zh", "tech",
                                   2000, 1, 45),
            ],
        ))
        report = ledger.report()
        show_report(report, "annex counts are not credited to any pack")

        line("5. range selection by right reading time, no splitting")
        # Select only increments whose right reading instant is in
        # [1500, 2500): that is [1000,2000) and [2000,2500), whole.
        report = ledger.report(1500, 2500)
        show_report(report, "range [1500, 2500) by right endpoint")

        line("6. revisions: conflicts and backwards counts change nothing")
        seq_before = ledger.state_seq
        segments_before = ledger.segments()
        report_before = ledger.report()
        try:
            ledger.apply(LedgerBatch(readings=[
                Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                                   2500, 2, 999),      # same rev, other value
            ]))
        except LedgerError as exc:
            print(f"  conflicting revision rejected: {exc}")
        try:
            ledger.apply(LedgerBatch(readings=[
                Reading.cumulative(SCREEN, "round-a", FAC, LANG, CTX,
                                   2200, 3, 50),       # below 200 at 2000
            ]))
        except LedgerError as exc:
            print(f"  backwards count rejected: {exc}")
        assert ledger.state_seq == seq_before
        assert ledger.segments() == segments_before
        assert ledger.report() == report_before
        print("  ledger, segments and reconciled result unchanged")

        line("7. revoking an installation record (not 'identity unknown')")
        # Revoke the late v4 record; the old pack is in force again across
        # the whole history, so [1000, 2000) becomes attributable to it.
        ledger.apply(LedgerBatch(installs=[
            InstallRecord.revoke(SCREEN, 1500, 2),
        ]))
        show_report(ledger.report(),
                    "v4 record revoked: old pack covers [1000,2000) again")

        line("8. reopen parity")
        report = ledger.report()
        seq = ledger.state_seq
        ledger.close()
        ledger = CountLedger.open(path)
        assert ledger.state_seq == seq
        assert ledger.report() == report
        print(f"  reopened {path}")
        print("  report and commit token identical after reopen")
        print("demo artifacts under:", directory)
        return directory
    finally:
        ledger.close()
        if own_tmp is not None:
            own_tmp.cleanup()


if __name__ == "__main__":
    main()
