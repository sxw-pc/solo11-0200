from .lighting import joint_assignment, light_state, power_state
from .patrol import patrol_state


def zone_state(db, cfg, zone, at):
    patrol = patrol_state(db, cfg, zone, at)
    lighting = light_state(db, cfg, zone, at)
    capacity = min(patrol["capacity"], lighting["capacity"])
    decision = "unavailable" if capacity == 0 else "limited" if capacity < cfg["zones"][zone]["capacity"] else "open"
    return {"zone": zone, "decision": decision, "capacity": capacity, "patrol": patrol, "lighting": lighting}


def power_review(db, cfg, at, occupancy):
    """Joint reserve margin for zones wired to shared lines, given people per zone."""
    survey = power_state(db, cfg, at)
    if survey is None:
        return {"decision": "no_shared_power", "managed": [], "loads": {}, "survey": None}
    managed = sorted(survey["wiring"])
    review = {"managed": managed, "survey": survey,
              "loads": {zone: occupancy.get(zone, 0) for zone in managed}}
    if survey["decision"] != "power_certified":
        return {"decision": survey["decision"], **review}
    scenarios = joint_assignment(survey["lines"], survey["wiring"], survey["load"], review["loads"])
    if scenarios is None:
        return {"decision": "power_overload", **review}
    return {"decision": "power_supported", "scenarios": scenarios, **review}


def power_admits(power, zone):
    return zone not in power["managed"] or power["decision"] == "power_supported"
