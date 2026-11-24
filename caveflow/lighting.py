from .model import choice, fields, number, require, token
from .store import one, rows

CHANNELS = ("marker", "reserve")


def start_round(db, cfg, event):
    data = event["data"]
    fields(data, "round zone tester")
    round_id = token(data["round"])
    zone = choice(data["zone"], cfg["zones"])
    tester = choice(data["tester"], cfg["guides"])
    at = event["at"]
    require(not one(db, "SELECT id FROM light_rounds WHERE id = ? OR (zone = ? AND at = ?)", round_id, zone, at),
            "round identifier or zone/minute already exists")
    db.execute("INSERT INTO light_rounds VALUES (?, ?, ?, ?, ?)", (round_id, zone, at, tester, event["id"]))
    return light_state(db, cfg, zone, at)


def check(db, cfg, event):
    data = event["data"]
    fields(data, "round channel version reading")
    round_id = token(data["round"])
    channel = choice(data["channel"], CHANNELS)
    reading = choice(data["reading"], ("pass", "weak", "fail"))
    version = number(data["version"], 1)
    parent = one(db, "SELECT * FROM light_rounds WHERE id = ?", round_id)
    require(parent and parent["at"] == event["at"], "check must belong to a round at the same effective minute")
    last = one(db, "SELECT COALESCE(MAX(version), 0) AS v FROM light_checks WHERE round = ? AND channel = ?",
               round_id, channel)["v"]
    require(version == last + 1, "check revision must advance by one")
    db.execute("INSERT INTO light_checks VALUES (?, ?, ?, ?, ?)", (round_id, channel, version, reading, event["id"]))
    return light_state(db, cfg, parent["zone"], parent["at"])


def light_state(db, cfg, zone, at):
    parent = one(db, "SELECT * FROM light_rounds WHERE zone = ? AND at <= ? ORDER BY at DESC LIMIT 1", zone, at)
    if parent is None:
        return {"decision": "missing_light_round", "capacity": 0, "evidence": []}
    checks = rows(db, """
        SELECT channel, reading, event_id FROM light_checks c WHERE round = ?
        AND version = (SELECT MAX(version) FROM light_checks WHERE round = c.round AND channel = c.channel)
        ORDER BY channel
    """, parent["id"])
    readings = {row["channel"]: row["reading"] for row in checks}
    decision, capacity = "light_unproven", 0
    if at - parent["at"] > cfg["light_ttl"]:
        decision = "stale_light_round"
    elif any(value == "fail" for value in readings.values()) or readings.get("marker") == "weak":
        decision = "light_failed"
    elif len(readings) == len(CHANNELS):
        if readings["reserve"] == "weak":
            decision, capacity = "light_reduced", min(2, cfg["zones"][zone]["capacity"])
        else:
            decision, capacity = "light_passed", cfg["zones"][zone]["capacity"]
    return {"decision": decision, "capacity": capacity, "round": parent["id"], "readings": readings,
            "evidence": [parent["event_id"], *(row["event_id"] for row in checks)], "observed_at": parent["at"]}
