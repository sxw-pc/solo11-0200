from .escort import group_review, held
from .gate import reconcile
from .model import choice, decode, encode, fields, token
from .roster import membership_review
from .store import one, rows


def owner(db, cfg):
    accepted = one(db, """
        SELECT d.receiver FROM desk_offers d JOIN events e ON e.id = d.resolved_event
        WHERE d.status = 'accepted' ORDER BY e.ordinal DESC LIMIT 1
    """)
    return accepted["receiver"] if accepted else cfg["desks"][0]


def pending_desk(db):
    return one(db, "SELECT * FROM desk_offers WHERE status = 'pending'")


def manifest(db, cfg, at, gate_cut):
    departed = [membership_review(db, group) for group in rows(db, """
        SELECT * FROM groups WHERE exited_at IS NOT NULL ORDER BY id
    """)]
    seal = one(db, "SELECT id, at FROM closing WHERE status = 'sealed'")
    return {"groups": group_review(db, cfg, at), "gate": reconcile(db, gate_cut),
            "departed_discrepancies": [item for item in departed if item["added"] or item["removed"]],
            "seal": {"event": seal["id"], "gate": reconcile(db, seal["at"])} if seal else None}


def offer(db, cfg, event):
    data = event["data"]
    fields(data, "desk receiver")
    desk = choice(data["desk"], cfg["desks"])
    receiver = choice(data["receiver"], cfg["desks"])
    if desk != owner(db, cfg) or desk == receiver:
        return held("invalid_desk_giver_or_receiver", owner=owner(db, cfg))
    at = event["at"]
    snapshot = manifest(db, cfg, at, at)
    db.execute("UPDATE desk_offers SET status = 'superseded', resolved_event = ? WHERE status = 'pending'", (event["id"],))
    db.execute("INSERT INTO desk_offers VALUES (?, ?, ?, ?, ?, 'pending', NULL)",
               (event["id"], desk, receiver, at, encode(snapshot)))
    return {"decision": "desk_offered", "offer": event["id"], "manifest": snapshot}


def accept(db, cfg, event):
    data = event["data"]
    fields(data, "offer desk")
    offer_id = token(data["offer"])
    desk = choice(data["desk"], cfg["desks"])
    record = pending_desk(db)
    if not record or record["id"] != offer_id or record["receiver"] != desk:
        return held("wrong_desk_offer_or_signer", owner=owner(db, cfg))
    snapshot = decode(record["manifest"])
    current = manifest(db, cfg, event["at"], record["at"])
    if current != snapshot:
        changed = [key for key in current if current[key] != snapshot[key]]
        return held("desk_manifest_changed", owner=owner(db, cfg), changed=changed, current=current)
    db.execute("UPDATE desk_offers SET status = 'accepted', resolved_event = ? WHERE id = ?", (event["id"], offer_id))
    return {"decision": "desk_accepted", "owner": desk, "offer": offer_id}



def withdraw(db, cfg, event):
    data = event["data"]
    fields(data, "offer desk")
    offer_id = token(data["offer"])
    desk = choice(data["desk"], cfg["desks"])
    record = pending_desk(db)
    if not record or record["id"] != offer_id or record["giver"] != desk or desk != owner(db, cfg):
        return held("wrong_desk_offer_or_signer", owner=owner(db, cfg))
    db.execute("UPDATE desk_offers SET status = 'withdrawn', resolved_event = ? WHERE id = ?", (event["id"], offer_id))
    return {"decision": "desk_withdrawn", "owner": desk, "offer": offer_id}
