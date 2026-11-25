from . import closing, escort, gate, handoff, lighting, patrol, roster
from .conditions import zone_state
from .model import DomainError, choice, decode, encode, fields, minute, require, token
from .store import PROJECTIONS, config, one, rows, transaction

HANDLERS = {
    "roster.sheet": roster.sheet, "roster.mark": roster.mark,
    "gate.anchor": gate.anchor, "gate.read": gate.read, "gate.bridge": gate.bridge,
    "patrol.note": patrol.note, "light.round": lighting.start_round, "light.check": lighting.check,
    "light.power": lighting.power,
    "escort.dispatch": escort.dispatch, "escort.move": escort.move,
    "escort.offer": escort.offer, "escort.resolve": escort.resolve, "escort.exit": escort.exit_group,
    "desk.offer": handoff.offer, "desk.accept": handoff.accept,
    "desk.withdraw": handoff.withdraw, "close.seal": closing.close,
}


def validate_event(event, cfg):
    fields(event, "id shift kind at data")
    token(event["id"])
    require(event["shift"] == cfg["shift"], "event belongs to a different shift")
    choice(event["kind"], HANDLERS)
    minute(event["at"])
    require(isinstance(event["data"], dict), "event payload must be an object")


def apply(db, cfg, event):
    event_id, at, kind = (event[key] for key in ("id", "at", "kind"))
    operation = kind.startswith(("escort.", "desk.", "close."))
    if operation:
        clock = one(db, "SELECT at FROM operation_clock WHERE id = 1")
        require(clock is None or at >= clock["at"], "operations must advance in effective time")
    if kind.startswith("escort.") and closing.seal_record(db):
        result = escort.held("shift_sealed")
    else:
        result = HANDLERS[kind](db, cfg, event)
    if operation:
        db.execute("INSERT OR REPLACE INTO operation_clock VALUES (1, ?, ?)", (at, event_id))
    db.execute("INSERT INTO receipts VALUES (?, ?)", (event_id, encode(result)))
    return result


def record_many(db, events):
    require(isinstance(events, list) and events, "expected a nonempty event list")
    results = []
    with transaction(db, write=True):
        cfg = config(db)
        for event in events:
            validate_event(event, cfg)
            event_id, body = event["id"], encode(event)
            previous = one(db, "SELECT body FROM events WHERE id = ?", event_id)
            if previous:
                require(previous["body"] == body, f"event identifier collision: {event_id}")
                receipt = one(db, "SELECT body FROM receipts WHERE event_id = ?", event_id)
                require(receipt is not None, "receipt missing, rebuild before retry")
                result, duplicate = decode(receipt["body"]), True
            else:
                db.execute("INSERT INTO events(id, body) VALUES (?, ?)", (event_id, body))
                try:
                    result = apply(db, cfg, event)
                except DomainError as exc:
                    raise DomainError(f"{event_id}: {exc}") from exc
                duplicate = False
            results.append({"event": event_id, "duplicate": duplicate, **result})
    return results


def rebuild(db):
    with transaction(db, write=True):
        cfg = config(db)
        events = rows(db, "SELECT id, body FROM events ORDER BY ordinal")
        for table in PROJECTIONS:
            db.execute(f"DELETE FROM {table}")
        for row in events:
            event = decode(row["body"])
            validate_event(event, cfg)
            require(event["id"] == row["id"], "journal row and event identity disagree")
            apply(db, cfg, event)
        return {"decision": "rebuilt", "events": len(events),
                "rows": {table: one(db, f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in PROJECTIONS}}


def status(db, cfg, at):
    clock = one(db, "SELECT at FROM operation_clock WHERE id = 1")
    require(clock is None or at >= clock["at"], "status minute precedes latest operation")
    pending = handoff.pending_desk(db)
    return {"shift": cfg["shift"], "at": at, "desk": handoff.owner(db, cfg),
            "pending_desk": pending["id"] if pending else None,
            "zones": [zone_state(db, cfg, zone, at) for zone in sorted(cfg["zones"])],
            "active_groups": escort.group_review(db, cfg, at), "closing": closing.review(db, cfg, at)}


def receipts(db):
    return [{"event": row["id"], "ordinal": row["ordinal"], **decode(row["body"])} for row in rows(db, """
        SELECT e.id, e.ordinal, r.body FROM events e JOIN receipts r ON r.event_id = e.id ORDER BY e.ordinal
    """)]


