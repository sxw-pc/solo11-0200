from .model import choice, decode, encode, fields, number, require, token, tokens
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


def current_round(db, zone, at):
    return one(db, "SELECT id FROM light_rounds WHERE zone = ? AND at <= ? ORDER BY at DESC LIMIT 1", zone, at)


def power(db, cfg, event):
    data = event["data"]
    fields(data, "lines wiring load")
    lines = data["lines"]
    require(isinstance(lines, dict) and 1 <= len(lines) <= 3, "expected one to three shared power lines")
    for name, capacity in lines.items():
        token(name)
        number(capacity, 1)
    wiring = data["wiring"]
    require(isinstance(wiring, dict) and 1 <= len(wiring) <= 4, "expected one to four wired zones")
    for zone, options in wiring.items():
        choice(zone, cfg["zones"])
        require(isinstance(options, list) and options, "wired zone needs at least one line")
        for line in tokens(options):
            choice(line, lines)
    unit_load = number(data["load"], 1)
    rounds = {}
    for zone in wiring:
        parent = current_round(db, zone, event["at"])
        require(parent is not None, "power evidence needs a current light round for every wired zone")
        rounds[zone] = parent["id"]
    db.execute("INSERT INTO light_power VALUES (?, ?, ?, ?, ?, ?)",
               (event["at"], encode(lines), encode(wiring), unit_load, encode(rounds), event["id"]))
    return power_state(db, cfg, event["at"])


def power_state(db, cfg, at):
    survey = one(db, """
        SELECT p.at, p.lines, p.wiring, p.unit_load, p.rounds, p.event_id
        FROM light_power p JOIN events e ON e.id = p.event_id
        WHERE p.at <= ? ORDER BY p.at DESC, e.ordinal DESC LIMIT 1
    """, at)
    if survey is None:
        return None
    wiring = decode(survey["wiring"])
    certified = decode(survey["rounds"])
    current = {}
    for zone in wiring:
        parent = current_round(db, zone, at)
        current[zone] = parent["id"] if parent else None
    stale = any(current[zone] != certified[zone] for zone in wiring)
    return {"decision": "stale_power_survey" if stale else "power_certified",
            "lines": decode(survey["lines"]), "wiring": wiring, "load": survey["unit_load"],
            "rounds": certified, "current_rounds": current,
            "observed_at": survey["at"], "evidence": [survey["event_id"]]}


def joint_assignment(lines, wiring, unit_load, occupancy):
    """One line per occupied zone per scenario; every scenario must assign completely."""
    loads = {zone: occupancy.get(zone, 0) * unit_load for zone in wiring}
    scenarios = [("normal", ())] + [(f"outage:{line}", (line,)) for line in lines]
    assignments = {}
    for name, down in scenarios:
        assignment = scenario_assignment(lines, wiring, loads, set(down))
        if assignment is None:
            return None
        assignments[name] = assignment
    return assignments


def scenario_assignment(lines, wiring, loads, down):
    capacities = {line: capacity for line, capacity in lines.items() if line not in down}
    pending = {zone: load for zone, load in loads.items() if load > 0}
    options = {zone: [line for line in wiring[zone] if line in capacities] for zone in pending}
    assignment = {}

    def place():
        if not pending:
            return True
        zone = min(pending, key=lambda name: (len(options[name]), name))
        load = pending.pop(zone)
        for line in options[zone]:
            if capacities[line] >= load:
                capacities[line] -= load
                assignment[zone] = line
                if place():
                    return True
                del assignment[zone]
                capacities[line] += load
        pending[zone] = load
        return False

    return assignment if place() else None
