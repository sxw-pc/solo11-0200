import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import conditions, gate, journal, roster
from .model import DomainError, choice, decode, minute, require, token
from .store import config, connect, initialize, one, transaction

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def unique_object(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "duplicate JSON object key")
    return result


def reject_constant(value):
    raise DomainError(f"invalid JSON number: {value}")


def load(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)


def parser():
    root = argparse.ArgumentParser(description="Synthetic offline cave tour evidence desk")
    root.add_argument("--db", required=True, help="path to the shift SQLite file")
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init", "initialize a shift from a configuration file"),
        ("record", "record one evidence or operation event"),
        ("load", "record a whole JSON event list atomically"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("file")
    for name, help_text in (
        ("roster", "review a roll call"), ("gate", "reconcile a meter cutoff"),
        ("zone", "combine patrol and lighting for a zone"), ("status", "review the desk and closure evidence"),
    ):
        sub = commands.add_parser(name, help=help_text)
        if name in ("roster", "zone"):
            sub.add_argument("identifier")
        sub.add_argument("--at", type=int, required=True)
    receipt = commands.add_parser("receipt", help="read the original decision for one event")
    receipt.add_argument("identifier")
    commands.add_parser("log", help="list original decision receipts")
    commands.add_parser("rebuild", help="atomically rebuild views from the event journal")
    commands.add_parser("demo", help="run the bundled synthetic shift")
    return root


def reading(db, args):
    with transaction(db):
        cfg = config(db)
        if args.command == "log":
            return journal.receipts(db)
        if args.command == "receipt":
            row = one(db, "SELECT body FROM receipts WHERE event_id = ?", token(args.identifier))
            require(row is not None, "unknown event receipt")
            return decode(row["body"])
        at = minute(args.at)
        if args.command == "roster":
            return roster.presence(db, token(args.identifier), at)
        if args.command == "gate":
            return gate.reconcile(db, at)
        if args.command == "zone":
            return conditions.zone_state(db, cfg, choice(args.identifier, cfg["zones"]), at)
        return journal.status(db, cfg, at)


def demo(db):
    cfg = load(FIXTURES / "shift.json")
    initialize(db, cfg)
    events = [event for packet in sorted(FIXTURES.glob("[0-9][0-9]-*.json")) for event in load(packet)]
    results = journal.record_many(db, events)
    with transaction(db):
        report = journal.status(db, cfg, 26)
    return {"shift": report["shift"], "event_count": len(results),
            "checkpoints": [{key: row[key] for key in ("event", "decision", "reason") if key in row}
                            for row in results if row["event"] in (
                                "dispatch-amber-early", "amber", "amber-echo-retry", "desk-stale",
                                "seal-first", "seal-final", "late-roll-correction")],
            "desk": report["desk"], "seal": report["closing"]["status"],
            "frozen_gate": report["closing"]["frozen"]["gate"]["through_step"],
            "review_reasons": report["closing"]["current"]["reasons"]}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command not in ("init", "demo"):
            require(Path(args.db).is_file(), "database does not exist, initialize the shift first")
        with closing(connect(args.db)) as db:
            if args.command == "init":
                result = {"decision": "initialized" if initialize(db, load(args.file)) else "already_initialized"}
            elif args.command == "record":
                result = journal.record_many(db, [load(args.file)])[0]
            elif args.command == "load":
                result = journal.record_many(db, load(args.file))
            elif args.command == "rebuild":
                result = journal.rebuild(db)
            elif args.command == "demo":
                result = demo(db)
            else:
                result = reading(db, args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
