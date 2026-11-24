from .escort import group_review, held
from .gate import reconcile
from .handoff import owner, pending_desk
from .model import choice, decode, encode, fields
from .roster import membership_review
from .store import one, rows


def seal_record(db):
    return one(db, "SELECT * FROM closing WHERE status = 'sealed'")


def assessment(db, cfg, at, include_desk=True):
    gate = reconcile(db, at)
    groups = rows(db, "SELECT * FROM groups ORDER BY id")
    entered = sum(len(decode(row["members"])) for row in groups)
    exited = sum(len(decode(row["members"])) for row in groups if row["exited_at"] is not None)
    mismatches = []
    for row in groups:
        item = membership_review(db, row)
        if item["added"] or item["removed"]:
            mismatches.append(item)
    active = group_review(db, cfg, at)
    reasons = []
    if active:
        reasons.append("groups_inside")
    if any(row["pending_transfer"] for row in active):
        reasons.append("custody_unsettled")
    if mismatches:
        reasons.append("roster_discrepancy")
    if gate["decision"] != "covered":
        reasons.append("gate_unproven")
    elif (gate["entries"], gate["exits"]) != (entered, exited):
        reasons.append("gate_escort_disagreement")
    desk = pending_desk(db) if include_desk else None
    if desk:
        reasons.append("desk_handoff_pending")
    return {"decision": "blocked" if reasons else "clear", "at": at, "reasons": reasons,
            "gate": gate, "escort_totals": {"entries": entered, "exits": exited},
            "groups": [{"group": row["id"], "members": decode(row["members"]),
                        "dispatch_event": row["dispatch_event"], "exit_event": row["exit_event"]} for row in groups],
            "active": active, "roster_discrepancies": mismatches, "pending_desk": desk["id"] if desk else None}


def close(db, cfg, event):
    data = event["data"]
    fields(data, "desk")
    desk = choice(data["desk"], cfg["desks"])
    if seal_record(db):
        return held("already_sealed")
    if desk != owner(db, cfg):
        return held("wrong_desk_owner", owner=owner(db, cfg))
    evidence = assessment(db, cfg, event["at"])
    status = "sealed" if evidence["decision"] == "clear" else "blocked"
    db.execute("INSERT INTO closing VALUES (?, ?, ?, ?, ?)", (event["id"], event["at"], desk, status, encode(evidence)))
    return {"decision": status, "evidence": evidence}


def review(db, cfg, at):
    record = seal_record(db)
    if record is None:
        return {"status": "unsealed", "current": assessment(db, cfg, at)}
    current = assessment(db, cfg, record["at"], include_desk=False)
    return {"status": "seal_supported" if current["decision"] == "clear" else "seal_disputed",
            "seal": record["id"], "sealed_by": record["desk"], "frozen": decode(record["evidence"]), "current": current}
