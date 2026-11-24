from .lighting import light_state
from .patrol import patrol_state


def zone_state(db, cfg, zone, at):
    patrol = patrol_state(db, cfg, zone, at)
    lighting = light_state(db, cfg, zone, at)
    capacity = min(patrol["capacity"], lighting["capacity"])
    decision = "unavailable" if capacity == 0 else "limited" if capacity < cfg["zones"][zone]["capacity"] else "open"
    return {"zone": zone, "decision": decision, "capacity": capacity, "patrol": patrol, "lighting": lighting}
