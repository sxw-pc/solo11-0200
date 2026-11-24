from .model import choice, decode, encode, fields, number, require, token, tokens
from .store import one, rows


def sheet(db, cfg, event):
    data = event["data"]
    fields(data, "sheet guide members")
    sheet_id = token(data["sheet"])
    guide = choice(data["guide"], cfg["guides"])
    members = tokens(data["members"])
    require(members and not set(members) & (set(cfg["guides"]) | set(cfg["desks"])),
            "sheet needs visitor identifiers distinct from staff")
    require(not one(db, "SELECT id FROM roster_sheets WHERE id = ?", sheet_id),
            "roster sheet already exists")
    db.execute("INSERT INTO roster_sheets VALUES (?, ?, ?, ?, ?)",
               (sheet_id, guide, encode(sorted(members)), event["at"], event["id"]))
    return {"decision": "sheet_opened", "sheet": sheet_id}


def mark(db, cfg, event):
    data = event["data"]
    fields(data, "sheet person version mark")
    sheet_id, person = token(data["sheet"]), token(data["person"])
    version = number(data["version"], 1)
    mark_value = choice(data["mark"], ("present", "absent", "excused"))
    parent = one(db, "SELECT * FROM roster_sheets WHERE id = ?", sheet_id)
    require(parent and person in decode(parent["members"]) and event["at"] >= parent["at"],
            "mark must name a member at or after sheet opening")
    last = one(db, "SELECT COALESCE(MAX(version), 0) AS v FROM roster_marks WHERE sheet = ? AND person = ?",
               sheet_id, person)["v"]
    require(version == last + 1, "mark revision must advance by one")
    db.execute("INSERT INTO roster_marks VALUES (?, ?, ?, ?, ?, ?)",
               (sheet_id, person, version, event["at"], mark_value, event["id"]))
    return {"decision": "mark_recorded" if last == 0 else "mark_revised",
            "roster": presence(db, sheet_id, event["at"])}


def presence(db, sheet_id, at):
    parent = one(db, "SELECT * FROM roster_sheets WHERE id = ? AND at <= ?", sheet_id, at)
    if parent is None:
        return {"decision": "unknown_sheet", "sheet": sheet_id, "evidence": []}
    marks = rows(db, """
        SELECT person, mark, event_id FROM roster_marks m WHERE sheet = ? AND at <= ?
        AND version = (SELECT MAX(version) FROM roster_marks
                       WHERE sheet = m.sheet AND person = m.person AND at <= ?)
        ORDER BY person
    """, sheet_id, at, at)
    present = [row["person"] for row in marks if row["mark"] == "present"]
    unmarked = sorted(set(decode(parent["members"])) - {row["person"] for row in marks})
    decision = "ready" if present else "no_visitors_present"
    if unmarked:
        decision = "unmarked_visitors"
    return {"decision": decision, "sheet": sheet_id, "guide": parent["guide"],
            "present": present, "unmarked": unmarked, "marks": [dict(row) for row in marks],
            "evidence": [parent["event_id"], *(row["event_id"] for row in marks)]}


def membership_review(db, group):
    replayed = presence(db, group["sheet"], group["dispatched_at"])
    frozen = set(decode(group["members"]))
    current = set(replayed["present"]) | {replayed["guide"]}
    return {"group": group["id"], "added": sorted(current - frozen),
            "removed": sorted(frozen - current), "dispatch_event": group["dispatch_event"],
            "frozen_evidence": decode(group["roster_evidence"]), "current_evidence": replayed["evidence"]}
