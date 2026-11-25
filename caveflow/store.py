import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .model import decode, encode, fields, number, require, token, tokens

SCHEMA = Path(__file__).with_name("schema.sql")
PROJECTIONS = (
    "receipts", "operation_clock", "closing", "desk_offers", "transfers", "groups", "light_power",
    "light_checks", "light_rounds", "patrol_notes", "gate_bridges", "gate_reads", "gate_anchor",
    "roster_marks", "roster_sheets",
)


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def transaction(db, write=False):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def one(db, sql, *values):
    return db.execute(sql, values).fetchone()


def rows(db, sql, *values):
    return db.execute(sql, values).fetchall()


def config(db):
    row = one(db, "SELECT body FROM config WHERE id = 1")
    require(row is not None, "initialize the shift first")
    return decode(row["body"])


def validate_config(value):
    fields(value, "shift foyer guides desks patrol_ttl light_ttl zones")
    token(value["shift"])
    foyer = token(value["foyer"])
    guides = tokens(value["guides"])
    desks = tokens(value["desks"])
    require(len(guides) >= 2 and len(desks) >= 2 and not set(guides) & set(desks),
            "need two or more distinct guides and desks")
    number(value["patrol_ttl"], 1, 1440)
    number(value["light_ttl"], 1, 1440)
    zones = value["zones"]
    require(isinstance(zones, dict) and foyer in zones, "a foyer zone is required")
    for name, zone in zones.items():
        token(name)
        fields(zone, "capacity neighbors")
        number(zone["capacity"], 1, 1000)
        require(all(neighbor in zones and neighbor != name for neighbor in tokens(zone["neighbors"])),
                "unknown zone neighbor or self-link")
    for name, zone in zones.items():
        require(all(name in zones[neighbor]["neighbors"] for neighbor in zone["neighbors"]),
                "zone links must be reciprocal")


def initialize(db, value):
    validate_config(value)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    body = encode(value)
    with transaction(db, write=True):
        old = one(db, "SELECT body FROM config WHERE id = 1")
        if old:
            require(old["body"] == body, "database already initialized with a different shift")
            return False
        db.execute("INSERT INTO config VALUES (1, ?)", (body,))
        return True

