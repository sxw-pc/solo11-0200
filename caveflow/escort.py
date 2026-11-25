from .conditions import power_admits, power_review, zone_state
from .model import choice, decode, encode, fields, number, require, token
from .roster import membership_review, presence
from .store import one, rows


def held(reason, **details):
    return {"decision": "held", "reason": reason, **details}


def active_groups(db):
    return rows(db, "SELECT * FROM groups WHERE exited_at IS NULL ORDER BY id")


def occupancy(db):
    result = {}
    for group in active_groups(db):
        result[group["zone"]] = result.get(group["zone"], 0) + len(decode(group["members"]))
    return result


def occupied(db, zone):
    return occupancy(db).get(zone, 0)


def landing(db, cfg, zone, at, load, origin=None):
    condition = zone_state(db, cfg, zone, at)
    loads = occupancy(db)
    used = loads.get(zone, 0)
    if origin is not None and origin != zone:
        loads[origin] = loads.get(origin, 0) - load
    loads[zone] = used + load
    power = power_review(db, cfg, at, loads)
    fits = (condition["capacity"] > 0 and used + load <= condition["capacity"]
            and power_admits(power, zone))
    return {"condition": condition, "occupied": used, "requested": load,
            "fits": fits, "power": power}


def landing_plans(db, cfg, at, candidates):
    """Maximal legal whole-group landing plans for simultaneous arriving candidates."""
    require(isinstance(candidates, list) and candidates, "expected a nonempty candidate list")
    normalized = []
    for candidate in candidates:
        require(isinstance(candidate, dict) and {"zone", "load"} <= candidate.keys() <= {"label", "zone", "load"},
                "candidate needs zone and load with an optional label")
        label = token(candidate["label"]) if "label" in candidate else f"candidate-{len(normalized) + 1}"
        normalized.append({"label": label, "zone": choice(candidate["zone"], cfg["zones"]),
                           "load": number(candidate["load"], 1)})
    require(len({c["label"] for c in normalized}) == len(normalized), "candidate labels must be unique")
    base = occupancy(db)
    ceilings = {}
    for candidate in normalized:
        zone = candidate["zone"]
        if zone not in ceilings:
            ceilings[zone] = zone_state(db, cfg, zone, at)["capacity"]
    feasible = []
    for mask in range(1, 1 << len(normalized)):
        chosen = [c for i, c in enumerate(normalized) if mask & (1 << i)]
        projected = dict(base)
        for candidate in chosen:
            zone = candidate["zone"]
            projected[zone] = projected.get(zone, 0) + candidate["load"]
        targets = {c["zone"] for c in chosen}
        if any(ceilings[zone] <= 0 or projected[zone] > ceilings[zone] for zone in targets):
            continue
        power = power_review(db, cfg, at, projected)
        if not all(power_admits(power, zone) for zone in targets):
            continue
        feasible.append((mask, chosen, projected, power))
    plans = []
    for mask, chosen, projected, power in feasible:
        if any(other != mask and mask & other == mask for other, _, _, _ in feasible):
            continue
        plans.append({"landings": chosen, "power": power,
                      "occupancy": {zone: people for zone, people in sorted(projected.items()) if people}})
    return {"decision": "plans_found" if plans else "no_feasible_plan", "at": at,
            "candidates": normalized, "plans": plans}


def dispatch(db, cfg, event):
    data = event["data"]
    fields(data, "group sheet guide")
    group_id, sheet_id = token(data["group"]), token(data["sheet"])
    guide = choice(data["guide"], cfg["guides"])
    at = event["at"]
    if one(db, "SELECT id FROM groups WHERE id = ? OR sheet = ?", group_id, sheet_id):
        return held("group_or_sheet_already_dispatched")
    roster = presence(db, sheet_id, at)
    if roster["decision"] != "ready" or roster["guide"] != guide:
        return held("roster_not_ready" if roster["decision"] != "ready" else "wrong_sheet_guide", roster=roster)
    members = sorted({guide, *roster["present"]})
    for existing in active_groups(db):
        conflicts = sorted(set(members) & {*decode(existing["members"]), existing["custodian"]})
        if conflicts:
            return held("already_under_custody", people=conflicts, existing_group=existing["id"])
    zone = cfg["foyer"]
    admission = landing(db, cfg, zone, at, len(members))
    if not admission["fits"]:
        return held("foyer_admission", landing=admission, roster=roster)
    db.execute("INSERT INTO groups VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
               (group_id, sheet_id, encode(members), encode(roster["evidence"]), guide, zone, at, event["id"]))
    return {"decision": "dispatched", "group": group_id, "members": members, "roster": roster, "landing": admission}


def pending(db, group_id):
    return one(db, "SELECT * FROM transfers WHERE group_id = ? AND status = 'pending'", group_id)


def actor_group(db, cfg, data):
    group_id = token(data["group"])
    guide = choice(data["guide"], cfg["guides"])
    return guide, one(db, "SELECT * FROM groups WHERE id = ? AND exited_at IS NULL", group_id)


def movement_block(db, group, guide):
    if group is None:
        return held("group_not_inside")
    if group["custodian"] != guide:
        return held("wrong_custodian", custodian=group["custodian"])
    offer = pending(db, group["id"])
    if offer:
        return held("transfer_pending", offer=offer["id"])
    return None


def move(db, cfg, event):
    data = event["data"]
    fields(data, "group guide destination")
    destination = choice(data["destination"], cfg["zones"])
    guide, group = actor_group(db, cfg, data)
    blocked = movement_block(db, group, guide)
    if blocked:
        return blocked
    if destination not in cfg["zones"][group["zone"]]["neighbors"]:
        return held("not_adjacent", origin=group["zone"], destination=destination)
    admission = landing(db, cfg, destination, event["at"], len(decode(group["members"])), origin=group["zone"])
    if not admission["fits"]:
        return held("destination_admission", landing=admission, origin=group["zone"])
    db.execute("UPDATE groups SET zone = ? WHERE id = ?", (destination, group["id"]))
    return {"decision": "moved", "group": group["id"], "origin": group["zone"], "landing": admission}


def staff_busy(db, guide, group_id):
    return any(row["id"] != group_id and (row["custodian"] == guide or guide in decode(row["members"]))
               for row in active_groups(db))


def offer(db, cfg, event):
    data = event["data"]
    fields(data, "group guide receiver")
    receiver = choice(data["receiver"], cfg["guides"])
    guide, group = actor_group(db, cfg, data)
    require(receiver != guide, "custody receiver must differ from giver")
    blocked = movement_block(db, group, guide)
    if blocked:
        return blocked
    if staff_busy(db, receiver, group["id"]):
        return held("receiver_busy", receiver=receiver)
    db.execute("INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
               (event["id"], group["id"], guide, receiver, group["zone"], event["at"]))
    return {"decision": "transfer_offered", "offer": event["id"], "group": group["id"], "zone": group["zone"]}


def resolve(db, cfg, event):
    data = event["data"]
    fields(data, "offer guide response")
    offer_id = token(data["offer"])
    guide = choice(data["guide"], cfg["guides"])
    response = choice(data["response"], ("accept", "decline", "withdraw"))
    record = one(db, "SELECT * FROM transfers WHERE id = ? AND status = 'pending'", offer_id)
    if record is None:
        return held("no_pending_transfer", offer=offer_id)
    expected = record["giver"] if response == "withdraw" else record["receiver"]
    if guide != expected:
        return held("wrong_transfer_signer", expected=expected)
    status = {"accept": "accepted", "decline": "declined", "withdraw": "withdrawn"}[response]
    custodian = record["giver"]
    if response == "accept":
        if staff_busy(db, guide, record["group_id"]):
            return held("receiver_busy", receiver=guide)
        custodian = guide
        db.execute("UPDATE groups SET custodian = ? WHERE id = ?", (custodian, record["group_id"]))
    db.execute("UPDATE transfers SET status = ?, resolved_event = ? WHERE id = ?", (status, event["id"], offer_id))
    return {"decision": f"transfer_{status}", "offer": offer_id, "group": record["group_id"], "custodian": custodian}


def exit_group(db, cfg, event):
    data = event["data"]
    fields(data, "group guide")
    guide, group = actor_group(db, cfg, data)
    blocked = movement_block(db, group, guide)
    if blocked:
        return blocked
    if group["zone"] != cfg["foyer"]:
        return held("exit_requires_foyer", zone=group["zone"])
    db.execute("UPDATE groups SET exited_at = ?, exit_event = ? WHERE id = ?",
               (event["at"], event["id"], group["id"]))
    return {"decision": "exited", "group": group["id"], "members": decode(group["members"]), "custodian": guide}


def group_review(db, cfg, at):
    active = []
    groups = active_groups(db)
    used_by_zone = {}
    for group in groups:
        used_by_zone[group["zone"]] = used_by_zone.get(group["zone"], 0) + len(decode(group["members"]))
    for group in groups:
        condition = zone_state(db, cfg, group["zone"], at)
        transfer = pending(db, group["id"])
        used = used_by_zone[group["zone"]]
        active.append({"group": group["id"], "members": decode(group["members"]), "custodian": group["custodian"],
                       "zone": group["zone"], "occupied": used, "condition": condition,
                       "over_capacity": used > condition["capacity"],
                       "pending_transfer": dict(transfer) if transfer else None,
                       "roster": membership_review(db, group)})
    return active

