from .model import choice, fields, number, require, token
from .store import one


def note(db, cfg, event):
    data = event["data"]
    fields(data, "zone version state capacity inspector trace")
    zone = choice(data["zone"], cfg["zones"])
    state = choice(data["state"], ("open", "restricted", "closed"))
    ceiling = cfg["zones"][zone]["capacity"]
    capacity = number(data["capacity"], maximum=ceiling)
    require({"open": capacity == ceiling, "restricted": 0 < capacity < ceiling,
             "closed": capacity == 0}[state], "patrol state and capacity disagree")
    inspector = choice(data["inspector"], cfg["guides"])
    trace = token(data["trace"])
    version = number(data["version"], 1)
    at = event["at"]
    last = one(db, "SELECT COALESCE(MAX(version), 0) AS v FROM patrol_notes WHERE zone = ? AND at = ?",
               zone, at)["v"]
    require(version == last + 1, "patrol revision must advance by one")
    db.execute("INSERT INTO patrol_notes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (zone, at, version, state, capacity, inspector, trace, event["id"]))
    return patrol_state(db, cfg, zone, at)


def patrol_state(db, cfg, zone, at):
    selected = one(db, """
        SELECT * FROM patrol_notes WHERE zone = ? AND at <= ? ORDER BY at DESC, version DESC LIMIT 1
    """, zone, at)
    if selected is None:
        return {"decision": "missing_patrol", "capacity": 0, "evidence": None}
    expired = at - selected["at"] > cfg["patrol_ttl"]
    return {"decision": "stale_patrol" if expired else selected["state"],
            "capacity": 0 if expired else selected["capacity"], "evidence": selected["event_id"],
            "observed_at": selected["at"], "trace": selected["trace"]}
