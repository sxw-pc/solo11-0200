from .model import choice, decode, encode, fields, number, require, token, tokens
from .store import one, rows


def anchor(db, cfg, event):
    data = event["data"]
    fields(data, "entries exits")
    entries, exits = number(data["entries"]), number(data["exits"])
    require(event["at"] == 0, "opening meter anchor must be at minute zero")
    require(not one(db, "SELECT id FROM gate_anchor"), "opening meter anchor already exists")
    db.execute("INSERT INTO gate_anchor VALUES (1, ?, ?, ?, ?)",
               (event["at"], entries, exits, event["id"]))
    return {"decision": "anchor_recorded"}


def read(db, cfg, event):
    data = event["data"]
    fields(data, "step version entries exits mode")
    step, version = number(data["step"], 1), number(data["version"], 1)
    entries, exits = number(data["entries"]), number(data["exits"])
    mode = choice(data["mode"], ("normal", "reset"))
    previous = one(db, "SELECT * FROM gate_reads WHERE step = ? ORDER BY version DESC LIMIT 1", step)
    at = event["at"]
    require(at > 0 and (previous is None or at == previous["at"]), "step capture minute is fixed and positive")
    require(version == (previous["version"] + 1 if previous else 1), "step revision must advance by one")
    db.execute("INSERT INTO gate_reads VALUES (?, ?, ?, ?, ?, ?, ?)",
               (step, version, at, entries, exits, mode, event["id"]))
    return {"decision": "step_recorded", "gate": reconcile(db, at)}


def bridge(db, cfg, event):
    data = event["data"]
    fields(data, "read tail_in tail_out base_in base_out witnesses")
    read_id = token(data["read"])
    witnesses = tokens(data["witnesses"])
    require(len(witnesses) == 2 and set(witnesses) <= set(cfg["desks"] + cfg["guides"]),
            "a reset bridge needs two different shift staff witnesses")
    tail_in, tail_out, base_in, base_out = (number(data[key]) for key in ("tail_in", "tail_out", "base_in", "base_out"))
    target = one(db, "SELECT * FROM gate_reads WHERE event_id = ? AND mode = 'reset'", read_id)
    require(target and event["at"] == target["at"], "bridge must name a reset read at the same capture minute")
    require(base_in <= target["entries"] and base_out <= target["exits"], "bridge base exceeds reset read")
    require(not one(db, "SELECT read_event FROM gate_bridges WHERE read_event = ?", read_id),
            "bridge already exists for this read revision")
    db.execute("INSERT INTO gate_bridges VALUES (?, ?, ?, ?, ?, ?, ?)",
               (read_id, tail_in, tail_out, base_in, base_out, encode(sorted(witnesses)), event["id"]))
    return {"decision": "bridge_recorded", "gate": reconcile(db, event["at"])}


def reconcile(db, at):
    result = {"entries": 0, "exits": 0, "through_step": 0, "through_at": None,
              "issue": None, "evidence": [], "bridges": []}
    previous = one(db, "SELECT * FROM gate_anchor WHERE at <= ?", at)
    if previous is None:
        return interrupted(result, "missing_anchor")
    result["through_at"] = previous["at"]
    result["evidence"].append(previous["event_id"])
    observations = rows(db, """
        SELECT * FROM gate_reads g WHERE at <= ?
        AND version = (SELECT MAX(version) FROM gate_reads WHERE step = g.step)
        ORDER BY step
    """, at)
    for current in observations:
        step = current["step"]
        evidence = [current["event_id"]]
        if step != result["through_step"] + 1:
            return interrupted(result, "step_gap", step, evidence)
        if current["at"] <= previous["at"]:
            return interrupted(result, "capture_order", step, evidence)
        if current["mode"] == "reset":
            reset = one(db, "SELECT * FROM gate_bridges WHERE read_event = ?", current["event_id"])
            if not reset:
                return interrupted(result, "unwitnessed_reset", step, evidence)
            evidence.append(reset["event_id"])
            if reset["tail_in"] < previous["entries"] or reset["tail_out"] < previous["exits"]:
                return interrupted(result, "bridge_tail_regression", step, evidence)
            added_in = reset["tail_in"] - previous["entries"] + current["entries"] - reset["base_in"]
            added_out = reset["tail_out"] - previous["exits"] + current["exits"] - reset["base_out"]
            result["bridges"].append({"step": step, "witnesses": decode(reset["witnesses"]),
                                      "event": reset["event_id"]})
        else:
            added_in = current["entries"] - previous["entries"]
            added_out = current["exits"] - previous["exits"]
            if min(added_in, added_out) < 0:
                return interrupted(result, "counter_regression", step, evidence)
        result["entries"] += added_in
        result["exits"] += added_out
        result["through_step"], result["through_at"] = step, current["at"]
        result["evidence"].extend(evidence)
        previous = current
    if result["through_at"] != at:
        return interrupted(result, "cutoff_not_sampled")
    result["decision"] = "covered"
    return result


def interrupted(result, reason, observed_step=None, evidence=()):
    result["decision"] = "partial"
    result["issue"] = {"reason": reason, "expected_step": result["through_step"] + 1,
                       "observed_step": observed_step, "evidence": list(evidence)}
    return result
