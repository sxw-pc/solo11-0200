"""Check independent probe readings against the shift and calibration evidence."""

from .conditions import zone_state
from .model import choice, fields, minute, number, require, token


def review(db, cfg, packet):
    fields(packet, "survey calibrations samples")
    survey = token(packet["survey"])
    require(isinstance(packet["calibrations"], list), "expected a calibration list")
    require(isinstance(packet["samples"], list), "expected a sample list")
    calibrated = {}
    for row in packet["calibrations"]:
        fields(row, "id probe starts ends offset")
        evidence = token(row["id"])
        probe = token(row["probe"])
        starts, ends = minute(row["starts"]), minute(row["ends"])
        offset = row["offset"]
        require(starts < ends and type(offset) is int and -100 <= offset <= 100, "invalid calibration")
        require(evidence not in calibrated, "duplicate calibration identifier")
        calibrated[evidence] = (probe, starts, ends, offset)

    originals = {}
    report = []
    for row in packet["samples"]:
        fields(row, "id probe calibration zone at raw inspector")
        sample, probe, reference = (token(row[key]) for key in ("id", "probe", "calibration"))
        zone = choice(row["zone"], cfg["zones"])
        at = minute(row["at"])
        raw = number(row["raw"], maximum=1000)
        inspector = choice(row["inspector"], cfg["guides"])
        if sample in originals:
            require(originals[sample] == row, "sample identifier conflict")
            continue
        originals[sample] = row
        local = zone_state(db, cfg, zone, at)
        result = {"id": sample, "probe": probe, "zone": zone, "at": at, "raw": raw,
                  "inspector": inspector, "calibration": reference, "access": local}
        calibration = calibrated.get(reference)
        if local["capacity"] == 0:
            result.update(state="ZONE_UNAVAILABLE", value=None)
        elif calibration is None or calibration[0] != probe or not calibration[1] <= at < calibration[2]:
            result.update(state="UNCALIBRATED", value=None)
        else:
            result.update(state="MEASURED", value=raw + calibration[3])
        report.append(result)
    return {"survey": survey, "samples": report}
