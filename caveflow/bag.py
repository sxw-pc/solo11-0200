"""Author-scoped witness bags with evidence-aware merge plans and signed choices.

A bag stores its raw field originals before any of them enter the ordinary event
journal.  Plans are replayed against an in-memory copy first; signing replays the
chosen set inside the same transaction that writes events and locks originals.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from . import conditions, journal
from .model import DomainError, choice, decode, encode, minute, require
from .store import config, connect, one, transaction

MAX_BAGS = 8
BAG_KINDS = ("patrol.note", "light.round", "light.check")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bag_bags (
    bag_id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bag_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bag_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    author TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at INTEGER NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    event_id TEXT NOT NULL,
    UNIQUE (bag_id, ordinal)
);
CREATE INDEX IF NOT EXISTS bag_receipt_original
    ON bag_receipts(author, external_id, digest);
CREATE TABLE IF NOT EXISTS bag_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bag_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_author TEXT,
    source_id TEXT,
    relation TEXT NOT NULL,
    target_author TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bag_decisions (
    decision_id TEXT PRIMARY KEY,
    desk TEXT NOT NULL,
    at INTEGER,
    choice TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bag_bag_locks (
    bag_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES bag_decisions(decision_id)
);
CREATE TABLE IF NOT EXISTS bag_locks (
    author TEXT NOT NULL,
    external_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES bag_decisions(decision_id),
    bag_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (author, external_id)
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)


def digest(value):
    """Stable hex SHA-256 digest of a JSON-like value."""
    raw = value if isinstance(value, str) else encode(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


body_digest = digest
content_digest = digest
digest_body = digest
body_summary = digest
summary_digest = digest
hash_body = digest
sha256 = digest


def _norm_digest(value):
    if not isinstance(value, str):
        return value
    return value[7:] if value.startswith("sha256:") else value


def _digest_eq(left, right):
    return _norm_digest(left) == _norm_digest(right)


def _label(value, name="identifier"):
    """A non-token-restricted external/business identifier."""
    require(isinstance(value, str) and value.strip() and len(value) <= 160,
            f"{name} must be a non-empty string of at most 160 characters")
    return value


def original_event_id(author, external_id):
    """Global, token-safe event ID for an author-scoped external original."""
    return "bag-" + hashlib.sha256(encode([author, external_id]).encode()).hexdigest()[:32]


event_id_for = original_event_id


def _pick(data, *keys):
    for key in keys:
        if isinstance(data, dict) and data.get(key) is not None:
            return data[key]
    return None


def _as_bag_list(payload):
    if isinstance(payload, (str, Path)):
        with Path(payload).open(encoding="utf-8") as stream:
            payload = json.load(stream)
    if isinstance(payload, list):
        bags = payload
    elif isinstance(payload, dict) and isinstance(payload.get("bags"), list):
        bags = payload["bags"]
    elif isinstance(payload, dict) and (_pick(payload, "author") is not None or
                                       _pick(payload, "receipts", "items", "records") is not None):
        bags = [payload]
    else:
        raise DomainError("expected a witness bag or a list of at most eight bags")
    require(1 <= len(bags) <= MAX_BAGS, f"expected 1–{MAX_BAGS} witness bags")
    return bags


def _parse_target(raw):
    if isinstance(raw, str):
        # Accept the compact form author/id/digest.
        parts = raw.split("/", 2)
        require(len(parts) == 3, "string citation must be author/id/digest")
        return {"author": parts[0], "id": parts[1], "digest": parts[2]}
    require(isinstance(raw, dict), "citation must be an object")
    target = _pick(raw, "target", "to", "citation", "cites", "reference")
    if target is None:
        target = raw
    if isinstance(target, str):
        return _parse_target(target)
    require(isinstance(target, dict), "citation target must be an object")
    author = _pick(target, "author", "by", "target_author")
    external_id = _pick(target, "id", "external_id", "externalId", "original_id", "originalId",
                        "target_id", "target_external_id")
    body_digest_value = _pick(target, "digest", "body_digest", "sha256", "summary",
                              "target_digest", "body_summary")
    require(isinstance(author, str) and isinstance(external_id, str),
            "citation requires exact author and original id")
    require(body_digest_value is not None, "citation requires the exact body digest")
    return {"author": author, "id": external_id, "digest": body_digest_value}


def _parse_ref(raw, bag_author, source=None):
    if isinstance(raw, str):
        target = _parse_target(raw)
        relation = "reference"
        source_value = source
    else:
        require(isinstance(raw, dict), "reference must be an object")
        target = _parse_target(raw)
        relation = _pick(raw, "relation", "rel", "type", "kind") or "reference"
        source_value = source
        explicit_source = _pick(raw, "source", "from", "source_id", "item")
        if source is None and explicit_source is not None:
            if isinstance(explicit_source, dict):
                source_value = (
                    _pick(explicit_source, "author", "by") or bag_author,
                    _pick(explicit_source, "id", "external_id", "original_id"),
                )
            else:
                source_value = (bag_author, explicit_source)
    require(isinstance(relation, str) and relation, "reference relation is invalid")
    if source_value is not None:
        require(isinstance(source_value, tuple) and len(source_value) == 2, "invalid reference source")
        source_author, source_id = source_value
    else:
        source_author = source_id = None
    return {
        "source_author": source_author,
        "source_id": source_id,
        "relation": relation,
        "target_author": target["author"],
        "target_id": target["id"],
        "target_digest": target["digest"],
    }


def _receipt_parts(raw):
    require(isinstance(raw, dict), "receipt must be an object")
    nested = _pick(raw, "receipt", "event", "body")
    source = nested if isinstance(nested, dict) and (
        "kind" in nested or "data" in nested) else raw
    kind = _pick(source, "kind")
    at = _pick(source, "at", "minute")
    data = _pick(source, "data", "payload")
    external_id = _pick(raw, "external_id", "externalId", "receipt_id", "original_id", "id")
    if external_id is None and source is not raw:
        external_id = _pick(source, "external_id", "externalId", "receipt_id", "original_id", "id")
    require(isinstance(external_id, str) and external_id, "receipt requires an external id")
    require(kind in BAG_KINDS, f"receipt kind must be one of {', '.join(BAG_KINDS)}")
    minute(at)
    require(isinstance(data, dict), "receipt data must be an object")
    core = {"kind": kind, "at": at, "data": data}
    declared = _pick(raw, "digest", "body_digest", "sha256", "summary", "body_summary")
    if declared is not None:
        accepted_digests = {digest(core), digest(data), digest({"id": external_id, **core})}
        require(_norm_digest(declared) in accepted_digests or declared == encode(core),
                "receipt body digest does not match its text")
    return external_id, kind, at, data, core


def receive_bags(db, payload):
    """Store up to eight raw bags idempotently and return bag summaries."""
    ensure_schema(db)
    bags = _as_bag_list(payload)
    results = []
    with transaction(db, write=True):
        cfg = config(db)
        base = one(db, "SELECT COALESCE(MAX(ordinal), -1) AS n FROM bag_bags")["n"] + 1
        for offset, raw_bag in enumerate(bags):
            results.append(_store_bag(db, cfg, raw_bag, base + offset))
    return results


receive = receive_bags
ingest_bags = receive_bags
upload_bags = receive_bags
receive_witness_bags = receive_bags
load_bags = receive_bags
accept_bags = receive_bags


def _store_bag(db, cfg, raw_bag, ordinal):
    require(isinstance(raw_bag, dict), "witness bag must be an object")
    author_value = _pick(raw_bag, "author", "by", "field_author", "field", "operator", "reporter")
    if isinstance(author_value, dict):
        author_value = _pick(author_value, "id", "author", "name", "by")
    author = _label(author_value, "bag author")
    supplied_id = _pick(raw_bag, "id", "bag_id", "bag")
    bag_id = _label(supplied_id, "bag id") if supplied_id is not None else "bag-" + digest(raw_bag)[:24]
    body = encode(raw_bag)
    bag_digest = digest(body)

    old = one(db, "SELECT * FROM bag_bags WHERE bag_id = ?", bag_id)
    if old is not None:
        require(old["body"] == body or old["digest"] == bag_digest,
                f"bag identifier collision: {bag_id}")
        summary = _bag_summary(db, old["bag_id"])
        summary["duplicate"] = True
        return summary

    receipt_rows = _pick(raw_bag, "receipts", "items", "records", "evidence") or []
    require(isinstance(receipt_rows, list), "bag receipts must be a list")
    db.execute("INSERT INTO bag_bags VALUES (?, ?, ?, ?, ?, ?)",
               (bag_id, author, bag_digest, body, ordinal, encode({"issues": []})))

    issues = []
    ref_count = 0
    stored_receipts = 0
    for index, raw_receipt in enumerate(receipt_rows):
        try:
            external_id, kind, at, data, core = _receipt_parts(raw_receipt)
            _label(external_id, "external receipt id")
            receipt_digest = digest(core)
            event_id = original_event_id(author, external_id)
            db.execute("""
                INSERT INTO bag_receipts
                    (bag_id, ordinal, author, external_id, kind, at, digest, body, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bag_id, index, author, external_id, kind, at, receipt_digest, encode(core), event_id))
            stored_receipts += 1
            source = (author, external_id)
            raw_refs = _pick(raw_receipt, "references", "citations", "refs") or []
            require(isinstance(raw_refs, list), "receipt references must be a list")
            for ref_index, raw_ref in enumerate(raw_refs):
                ref = _parse_ref(raw_ref, author, source)
                db.execute("""
                    INSERT INTO bag_refs
                      (bag_id, ordinal, source_author, source_id, relation,
                       target_author, target_id, target_digest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (bag_id, ref_index, ref["source_author"], ref["source_id"], ref["relation"],
                      ref["target_author"], ref["target_id"], ref["target_digest"]))
                ref_count += 1
        except DomainError as exc:
            issues.append({"code": "invalid_receipt", "receipt_index": index, "message": str(exc)})

    bag_refs = _pick(raw_bag, "references", "citations", "refs") or []
    require(isinstance(bag_refs, list), "bag references must be a list")
    for ref_index, raw_ref in enumerate(bag_refs):
        try:
            ref = _parse_ref(raw_ref, author, None)
            db.execute("""
                INSERT INTO bag_refs
                  (bag_id, ordinal, source_author, source_id, relation,
                   target_author, target_id, target_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (bag_id, 10000 + ref_index, ref["source_author"], ref["source_id"], ref["relation"],
                  ref["target_author"], ref["target_id"], ref["target_digest"]))
            ref_count += 1
        except DomainError as exc:
            issues.append({"code": "invalid_reference", "reference_index": ref_index, "message": str(exc)})

    # Author + external id is the field namespace. Different text under one key
    # is retained in the raw bag but makes every plan using it ambiguous.
    reused = db.execute("""
        SELECT external_id, COUNT(DISTINCT digest) AS n FROM bag_receipts
        WHERE author = ? AND external_id IN (
            SELECT external_id FROM bag_receipts WHERE bag_id = ?
        )
        GROUP BY external_id HAVING n > 1 LIMIT 1
    """, (author, bag_id)).fetchone()
    if reused is not None:
        issues.append({"code": "external_id_not_unique_for_author",
                       "external_id": reused["external_id"],
                       "message": "author-scoped external id has more than one body"})

    report = {"issues": issues, "receipt_count": stored_receipts, "reference_count": ref_count}
    db.execute("UPDATE bag_bags SET report = ? WHERE bag_id = ?", (encode(report), bag_id))
    return _bag_summary(db, bag_id)


def _bag_summary(db, bag_id):
    row = one(db, """
        SELECT b.*, l.decision_id AS locked_by FROM bag_bags b
        LEFT JOIN bag_bag_locks l ON l.bag_id = b.bag_id
        WHERE b.bag_id = ?
    """, bag_id)
    require(row is not None, f"unknown witness bag: {bag_id}")
    stored = decode(row["report"])
    receipt_count = one(db, "SELECT COUNT(*) n FROM bag_receipts WHERE bag_id = ?", bag_id)["n"]
    ref_count = one(db, "SELECT COUNT(*) n FROM bag_refs WHERE bag_id = ?", bag_id)["n"]
    return {
        "id": row["bag_id"], "bag_id": row["bag_id"], "author": row["author"],
        "digest": row["digest"], "ordinal": row["ordinal"],
        "receipt_count": receipt_count, "reference_count": ref_count,
        "locked_by": row["locked_by"], "status": "locked" if row["locked_by"] else "pending",
        "issues": stored.get("issues", []), "duplicate": False,
    }


def list_bags(db):
    ensure_schema(db)
    rows = db.execute("SELECT bag_id FROM bag_bags ORDER BY ordinal, bag_id").fetchall()
    return [_bag_summary(db, row["bag_id"]) for row in rows]


def bags(db):
    return list_bags(db)


def get_bag(db, bag_id):
    ensure_schema(db)
    _label(bag_id, "bag id")
    row = one(db, "SELECT * FROM bag_bags WHERE bag_id = ?", bag_id)
    require(row is not None, f"unknown witness bag: {bag_id}")
    result = _bag_summary(db, bag_id)
    result["raw"] = decode(row["body"])
    result["receipts"] = [dict(r) for r in db.execute(
        "SELECT ordinal, author, external_id, kind, at, digest, event_id, body "
        "FROM bag_receipts WHERE bag_id = ? ORDER BY ordinal", (bag_id,)).fetchall()]
    for item in result["receipts"]:
        item["body"] = decode(item["body"])
    result["references"] = [dict(r) for r in db.execute(
        "SELECT ordinal, source_author, source_id, relation, target_author, target_id, target_digest "
        "FROM bag_refs WHERE bag_id = ? ORDER BY ordinal", (bag_id,)).fetchall()]
    review = plan_selection(db, [bag_id])
    result["mergeable"] = review["valid"]
    result["conflicts"] = review["issues"]
    result["dependencies"] = review["dependencies"]
    return result


show_bag = get_bag
review_bag = get_bag
bag_report = get_bag


def _find_exact(db, author, external_id, body_digest_value):
    rows = db.execute(
        "SELECT * FROM bag_receipts WHERE author = ? AND external_id = ?",
        (author, external_id)).fetchall()
    for row in rows:
        core = decode(row["body"])
        candidates = {
            row["digest"],
            digest(core),
            digest(core.get("data", {})),
            digest({"id": row["external_id"], **core}),
            digest([author, external_id, core]),
        }
        normalized = _norm_digest(body_digest_value)
        if normalized in candidates or body_digest_value == row["body"]:
            return row
    return None


def _journal_has_original(db, author, external_id):
    event_id = original_event_id(author, external_id)
    row = one(db, "SELECT 1 FROM events WHERE id = ?", event_id)
    receipt = one(db, "SELECT 1 FROM receipts WHERE event_id = ?", event_id)
    return row is not None and receipt is not None


def _refs_for(db, bag_ids):
    if not bag_ids:
        return []
    marks = ",".join("?" for _ in bag_ids)
    return [dict(row) for row in db.execute(
        f"SELECT * FROM bag_refs WHERE bag_id IN ({marks}) ORDER BY id", tuple(bag_ids)).fetchall()]


def _round_identity(db, row, refs_by_source):
    """Return exact field-round identity without trusting a round name alone."""
    core = decode(row["body"])
    data = core["data"]
    source = (row["author"], row["external_id"])
    for ref in refs_by_source.get(source, []):
        target = _find_exact(db, ref["target_author"], ref["target_id"], ref["target_digest"])
        if target is not None and target["kind"] == "light.round":
            target_core = decode(target["body"])
            if target_core["data"].get("round") == data.get("round"):
                return ("original", target["author"], target["external_id"], target["digest"])
    candidates = {}
    for candidate in db.execute(
            "SELECT * FROM bag_receipts WHERE author = ? AND kind = 'light.round'",
            (row["author"],)).fetchall():
        candidate_core = decode(candidate["body"])
        if candidate_core["data"].get("round") == data.get("round"):
            candidates[(candidate["author"], candidate["external_id"], candidate["digest"])] = candidate
    if len(candidates) == 1:
        candidate = next(iter(candidates.values()))
        return ("original", candidate["author"], candidate["external_id"], candidate["digest"])
    return ("legacy", str(data.get("round")))


def _resolve_round_value(db, row, refs_by_source):
    identity = _round_identity(db, row, refs_by_source)
    if identity[0] == "original":
        return original_event_id(identity[1], identity[2]), identity
    core = decode(row["body"])
    return core["data"].get("round"), identity


def plan_selection(db, selection=None, at=None, **kwargs):
    """Replay a proposed bag selection in a throwaway database copy."""
    ensure_schema(db)
    cfg = config(db)
    name = kwargs.get("name")
    if selection is None:
        selection = kwargs
    if isinstance(selection, str):
        bag_ids = [selection]
    elif isinstance(selection, (list, tuple)):
        bag_ids = list(selection)
    elif isinstance(selection, dict):
        if at is None:
            at = selection.get("at")
        name = _pick(selection, "name", "plan_name") or name
        if "choice" in selection and "plans" in selection:
            chosen = _chosen_spec(selection)
            bag_ids = chosen.get("bags") or chosen.get("bag_ids") or []
            name = chosen.get("name", selection.get("choice"))
            if at is None:
                at = chosen.get("at")
        else:
            bag_ids = _pick(selection, "bags", "bag_ids", "selection", "selected_bags", "choices") or []
    else:
        raise DomainError("invalid merge plan")
    bag_ids = [str(x) for x in bag_ids]
    require(len(bag_ids) <= MAX_BAGS, f"expected at most {MAX_BAGS} selected bags")
    if at is not None:
        at = minute(at)
    return _plan_ids(db, cfg, bag_ids, at=at, name=name)


plan = plan_selection
review_plan = plan_selection
evaluate = plan_selection
build_plan = plan_selection
create_plan = plan_selection
make_plan = plan_selection
merge_plan = plan_selection


def _chosen_spec(payload):
    plans = payload.get("plans")
    choice = payload.get("choice") or payload.get("selected") or payload.get("plan")
    if isinstance(plans, dict):
        require(choice in plans, f"unknown signed plan: {choice}")
        spec = plans[choice]
        if isinstance(spec, list):
            return {"name": choice, "bags": spec}
        spec = dict(spec)
        spec.setdefault("name", choice)
        return spec
    require(isinstance(plans, list), "plans must be a list or object")
    for spec in plans:
        if isinstance(spec, dict) and spec.get("name") == choice:
            return spec
    raise DomainError(f"unknown signed plan: {choice}")


def _plan_ids(db, cfg, bag_ids, at=None, name=None, dry_run=True):
    report = {
        "name": name, "bags": bag_ids, "at": at, "valid": True, "replayable": False,
        "issues": [], "dependencies": [], "satisfied_dependencies": [],
        "originals": [], "events": [], "event_ids": [],
        "zones": [], "stopped_zones": [], "blocked_zones": [], "stopped_segments": [],
        "failed_zones": [], "failed_segments": [], "lighting_hold_zones": [],
        "non_passing_zones": [], "unpassed_zones": [], "stalled_zones": [], "capacity": {}, "decisions": {},
        "locked_by": {},
    }

    def issue(code, message, **extra):
        item = {"code": code, "message": message}
        item.update(extra)
        report["issues"].append(item)
        report["valid"] = False

    if not bag_ids:
        issue("empty_selection", "at least one witness bag is required")
        return report

    seen = set()
    unique_ids = []
    for bag_id in bag_ids:
        if bag_id not in seen:
            seen.add(bag_id)
            unique_ids.append(bag_id)
    report["bags"] = unique_ids

    marks = ",".join("?" for _ in unique_ids)
    bag_rows = db.execute(f"SELECT * FROM bag_bags WHERE bag_id IN ({marks})", tuple(unique_ids)).fetchall()
    found = {row["bag_id"]: row for row in bag_rows}
    for bag_id in unique_ids:
        if bag_id not in found:
            issue("unknown_bag", f"unknown witness bag: {bag_id}", bag_id=bag_id)
        else:
            stored = decode(found[bag_id]["report"])
            for item in stored.get("issues", []):
                issue(item.get("code", "invalid_bag"), item["message"], bag_id=bag_id)
            lock = one(db, "SELECT decision_id FROM bag_bag_locks WHERE bag_id = ?", bag_id)
            if lock:
                report["locked_by"][bag_id] = lock["decision_id"]

    if report["issues"] and any(x["code"] == "unknown_bag" for x in report["issues"]):
        return report

    receipt_rows = db.execute(
        f"SELECT * FROM bag_receipts WHERE bag_id IN ({marks}) ORDER BY bag_id, ordinal",
        tuple(unique_ids)).fetchall()
    refs = _refs_for(db, unique_ids)
    refs_by_source = {}
    for ref in refs:
        if ref["source_author"] is not None:
            refs_by_source.setdefault((ref["source_author"], ref["source_id"]), []).append(ref)

    originals = {}
    for row in receipt_rows:
        key = (row["author"], row["external_id"])
        if key in originals:
            if originals[key]["digest"] != row["digest"]:
                issue("external_id_collision",
                      f"author-scoped external id has different bodies: {key[1]}",
                      author=row["author"], external_id=row["external_id"])
            continue
        originals[key] = row

    # Resolve every directed citation. Exact author + id + digest is mandatory;
    # same version numbers under another author are never substitutes.
    for ref in refs:
        target = _find_exact(db, ref["target_author"], ref["target_id"], ref["target_digest"])
        dependency = {
            "relation": ref["relation"],
            "source_author": ref["source_author"], "source_id": ref["source_id"],
            "target_author": ref["target_author"], "target_id": ref["target_id"],
            "target_digest": ref["target_digest"], "satisfied": False, "via": None,
        }
        if target is None:
            same_id = db.execute(
                "SELECT 1 FROM bag_receipts WHERE author = ? AND external_id = ? LIMIT 1",
                (ref["target_author"], ref["target_id"])).fetchone()
            if same_id:
                code, message = "citation_digest_mismatch", "citation author/id exists but body digest differs"
            else:
                code, message = "missing_reference", "cited original has not arrived"
            dependency["status"] = code
            issue(code, message, target={"author": ref["target_author"], "id": ref["target_id"]})
            report["dependencies"].append(dependency)
            continue

        target_key = (target["author"], target["external_id"])
        if target_key in originals:
            via = "selected"
        elif one(db, "SELECT 1 FROM bag_locks WHERE author = ? AND external_id = ?", *target_key):
            via = "locked"
        elif _journal_has_original(db, *target_key):
            via = "journal"
        else:
            via = None
        dependency["target_kind"] = target["kind"]
        dependency["event_id"] = target["event_id"]
        dependency["via"] = via
        dependency["satisfied"] = via is not None
        dependency["status"] = "satisfied" if via else "waiting_for_selection"
        report["dependencies"].append(dependency)
        if via is None:
            issue("unsatisfied_reference",
                  "cited receipt is raw but is not selected or already merged",
                  target={"author": target["author"], "id": target["external_id"]})

    report["satisfied_dependencies"] = [item for item in report["dependencies"] if item["satisfied"]]

    events = {}
    round_dependency = {}
    for key, row in originals.items():
        core = decode(row["body"])
        event = {
            "id": row["event_id"], "shift": cfg["shift"], "kind": row["kind"],
            "at": row["at"], "data": deepcopy(core["data"]),
        }
        if row["kind"] == "light.round":
            # The global event id becomes the internal round key, while the raw
            # author-scoped round id remains in bag_receipts for traceability.
            event["data"]["round"] = row["event_id"]
        elif row["kind"] == "light.check":
            round_value, identity = _resolve_round_value(db, row, refs_by_source)
            event["data"]["round"] = round_value
            if identity[0] == "original":
                round_dependency[row["event_id"]] = original_event_id(identity[1], identity[2])
        events[row["event_id"]] = event
        report["originals"].append({
            "bag_id": row["bag_id"], "author": row["author"], "external_id": row["external_id"],
            "kind": row["kind"], "at": row["at"], "digest": row["digest"], "event_id": row["event_id"],
        })

    # Expose normalized events even when replay later rejects the arrangement;
    # this makes exact-round remapping and conflict reasons inspectable.
    report["events"] = list(events.values())

    _validate_previous_references(db, report, originals, refs_by_source, events)
    _projection_conflicts(db, report, originals, events)

    if not report["valid"]:
        return report

    ordered, cycle = _ordered_events(report, originals, events, report["dependencies"], round_dependency)
    if cycle:
        issue("reference_cycle", "directed references contain a cycle")
        return report
    report["events"] = ordered
    report["event_ids"] = [event["id"] for event in ordered]
    if not dry_run:
        # The caller is already inside the authoritative write transaction.
        # journal.apply below performs the actual same-transaction replay.
        report["replayable"] = True
        return report

    clone = connect(":memory:")
    try:
        db.backup(clone)
        try:
            journal.record_many(clone, ordered)
        except Exception as exc:  # replay must become a queryable plan conflict
            issue("replay_failed", str(exc))
            return report
        effective_at = at if at is not None else max(event["at"] for event in ordered)
        report["at"] = effective_at
        zones = [conditions.zone_state(clone, cfg, zone, effective_at) for zone in sorted(cfg["zones"])]
        report["zones"] = zones
        report["capacity"] = {zone["zone"]: zone["capacity"] for zone in zones}
        report["decisions"] = {zone["zone"]: zone["decision"] for zone in zones}
        stopped = [zone for zone in zones if zone["decision"] != "open"]
        lighting_hold = [zone for zone in zones if zone["lighting"].get("decision") != "light_passed"]
        failed = [zone for zone in zones if zone["decision"] == "unavailable" or zone["capacity"] == 0]
        report["stopped_zones"] = stopped
        report["blocked_zones"] = stopped
        report["stopped_segments"] = stopped
        report["non_passing_zones"] = stopped
        report["unpassed_zones"] = stopped
        report["stalled_zones"] = stopped
        report["lighting_hold_zones"] = lighting_hold
        report["failed_zones"] = failed
        report["failed_segments"] = failed
        report["replayable"] = True
        report["valid"] = True
        return report
    finally:
        clone.close()


def _validate_previous_references(db, report, originals, refs_by_source, events):
    for key, row in originals.items():
        core = decode(row["body"])
        kind = core["kind"]
        if kind not in ("patrol.note", "light.check"):
            continue
        version = core["data"].get("version", 1)
        if not isinstance(version, int) or version <= 1:
            continue
        source = (row["author"], row["external_id"])
        found_previous = False
        for ref in refs_by_source.get(source, []):
            target = _find_exact(db, ref["target_author"], ref["target_id"], ref["target_digest"])
            if target is None:
                continue
            target_core = decode(target["body"])
            if target_core["kind"] != kind:
                continue
            if target_core["data"].get("version") != version - 1:
                continue
            same_domain = False
            if kind == "patrol.note":
                same_domain = (
                    target_core["data"].get("zone") == core["data"].get("zone")
                    and target_core["at"] == core["at"]
                )
            else:
                source_round = events[row["event_id"]]["data"].get("round")
                target_round, _ = _resolve_round_value(db, target, refs_by_source)
                same_domain = (
                    target_core["data"].get("channel") == core["data"].get("channel")
                    and target_round == source_round
                )
            if same_domain:
                found_previous = True
                break
        if not found_previous:
            report["issues"].append({
                "code": "missing_same_domain_previous_reference",
                "message": f"{kind} version {version} must cite the exact same-domain version {version - 1}",
                "author": row["author"], "external_id": row["external_id"],
            })
            report["valid"] = False


def _projection_conflicts(db, report, originals, events):
    for key, row in originals.items():
        event = events[row["event_id"]]
        existing_event = one(db, "SELECT body FROM events WHERE id = ?", row["event_id"])
        same_event = existing_event is not None and existing_event["body"] == encode(event)
        if existing_event is not None and not same_event:
            report["issues"].append({
                "code": "event_identity_collision",
                "message": "deterministic bag event id is already used by different text",
                "event_id": row["event_id"],
            })
            report["valid"] = False
            continue
        if same_event:
            continue

        core = decode(row["body"])
        data = core["data"]
        version = data.get("version")
        if row["kind"] == "patrol.note" and isinstance(version, int):
            current = one(db, """
                SELECT MAX(version) AS v FROM patrol_notes WHERE zone = ? AND at = ?
            """, data.get("zone"), row["at"])
            max_version = current["v"] if current else None
            if max_version is not None and version < max_version:
                report["issues"].append({
                    "code": "higher_version_exists",
                    "message": f"patrol {data.get('zone')} minute {row['at']} already has version {max_version}",
                    "zone": data.get("zone"), "at": row["at"], "version": version,
                    "current_version": max_version,
                })
                report["valid"] = False
            elif max_version is not None and version == max_version:
                report["issues"].append({
                    "code": "version_already_adopted",
                    "message": "that patrol zone/minute/version is already adopted",
                    "zone": data.get("zone"), "at": row["at"], "version": version,
                })
                report["valid"] = False
        elif row["kind"] == "light.check" and isinstance(version, int):
            round_id = event["data"].get("round")
            parent = one(db, "SELECT id FROM light_rounds WHERE id = ?", round_id)
            if parent is not None:
                current = one(db, """
                    SELECT MAX(version) AS v FROM light_checks WHERE round = ? AND channel = ?
                """, round_id, data.get("channel"))
                max_version = current["v"] if current else None
                if max_version is not None and version <= max_version:
                    report["issues"].append({
                        "code": "higher_version_exists" if version < max_version else "version_already_adopted",
                        "message": f"light check already has version {max_version} for this exact round/channel",
                        "round": round_id, "channel": data.get("channel"), "version": version,
                    })
                    report["valid"] = False


def _ordered_events(report, originals, events, dependencies, round_dependency):
    id_by_original = {key: row["event_id"] for key, row in originals.items()}
    deps = {event_id: set() for event_id in events}
    for dep in dependencies:
        if not dep["satisfied"] or dep["via"] != "selected":
            continue
        source_eid = id_by_original.get((dep["source_author"], dep["source_id"]))
        target_eid = id_by_original.get((dep["target_author"], dep["target_id"]))
        if source_eid and target_eid and source_eid != target_eid:
            deps[source_eid].add(target_eid)
    for source_eid, target_eid in round_dependency.items():
        if target_eid in events:
            deps[source_eid].add(target_eid)

    order_rank = {row["event_id"]: index for index, row in enumerate(originals.values())}
    ordered = []
    remaining = set(events)
    while remaining:
        ready = [eid for eid in remaining if deps[eid] <= set(ordered)]
        if not ready:
            return None, True
        ready.sort(key=lambda eid: (events[eid]["at"], events[eid]["kind"], order_rank.get(eid, 0), eid))
        chosen = ready[0]
        ordered.append(chosen)
        remaining.remove(chosen)
    return [events[eid] for eid in ordered], False


def compare_plans(db, payload, at=None):
    ensure_schema(db)
    if isinstance(payload, dict):
        if at is None:
            at = payload.get("at")
        specs = payload.get("plans")
        if specs is None and isinstance(payload.get("choices"), list):
            specs = payload["choices"]
    elif isinstance(payload, list):
        specs = payload
    else:
        raise DomainError("compare expects a plans list or {'plans': [...]} object")
    require(isinstance(specs, list) and 1 <= len(specs) <= MAX_BAGS, "expected 1–8 merge arrangements")
    results = []
    for index, spec in enumerate(specs):
        if isinstance(spec, list):
            spec = {"name": f"plan-{index + 1}", "bags": spec}
        require(isinstance(spec, dict), "each arrangement must be an object")
        spec_at = spec.get("at", at)
        results.append(plan_selection(db, spec.get("bags") or spec.get("bag_ids")
                                      or spec.get("selected_bags") or [],
                                      at=spec_at, name=spec.get("name", f"plan-{index + 1}")))
    return {
        "decision": "plans_compared", "at": at, "plans": results,
        "all_valid": all(item["valid"] for item in results),
        "all_replayable": all(item["replayable"] for item in results),
    }


compare = compare_plans
compare_choices = compare_plans
compare_arrangements = compare_plans


def arrange(db, bag_ids=None, at=None):
    """Enumerate at most eight bags and return maximal replayable choices."""
    ensure_schema(db)
    if bag_ids is None:
        bag_ids = [row["bag_id"] for row in db.execute("""
            SELECT b.bag_id FROM bag_bags b
            LEFT JOIN bag_bag_locks l ON l.bag_id = b.bag_id
            WHERE l.bag_id IS NULL ORDER BY b.ordinal, b.bag_id
        """).fetchall()]
    if isinstance(bag_ids, dict):
        at = at if at is not None else bag_ids.get("at")
        bag_ids = _pick(bag_ids, "bags", "bag_ids", "selection") or []
    require(isinstance(bag_ids, (list, tuple)) and 1 <= len(bag_ids) <= MAX_BAGS,
            f"expected 1–{MAX_BAGS} bags for arrangement enumeration")
    ids = list(dict.fromkeys(bag_ids))
    valid = []
    for mask in range(1, 1 << len(ids)):
        chosen = [bag_id for index, bag_id in enumerate(ids) if mask & (1 << index)]
        report = plan_selection(db, chosen, at=at, name="+".join(chosen))
        if report["valid"]:
            valid.append((mask, report))
    maximal = []
    for index, (mask, report) in enumerate(valid):
        dominated = any(
            other_mask != mask and (mask & ~other_mask) == 0
            for other_index, (other_mask, _) in enumerate(valid) if other_index != index
        )
        if not dominated:
            maximal.append(report)
    maximal.sort(key=lambda item: (-sum(item["capacity"].values()), item["bags"]))
    return {"decision": "arrangements_ready", "count": len(maximal), "plans": maximal}


propose_plans = arrange
arrangements = arrange


def sign_selection(db, payload=None, **kwargs):
    """Validate, journal, and lock one chosen arrangement atomically.

    Accepts either a decision object or the more explicit keyword form
    ``sign_selection(db, bags=[...], desk=..., choice=..., at=...)``.
    """
    ensure_schema(db)
    if isinstance(payload, (str, Path)) and Path(payload).is_file():
        with Path(payload).open(encoding="utf-8") as stream:
            payload = json.load(stream)
    elif isinstance(payload, str):
        payload = {"bags": [payload]}
    if payload is None:
        payload = {}
    elif isinstance(payload, (list, tuple)) and all(isinstance(item, str) for item in payload):
        payload = {"bags": list(payload)}
    elif isinstance(payload, (list, tuple)) and payload and all(isinstance(item, dict) for item in payload):
        received = receive_bags(db, list(payload))
        payload = {"bags": [item["id"] for item in received]}
    require(isinstance(payload, dict), "signing requires a decision object")
    payload = {**kwargs, **{key: value for key, value in payload.items() if value is not None}}
    require(isinstance(payload, dict), "signing requires a decision object")
    desk = _pick(payload, "desk", "signer", "signed_by", "desk_id")
    require(isinstance(desk, str) and desk, "signing requires a desk name")

    at = payload.get("at")
    if at is not None:
        at = minute(at)
    choice_name = payload.get("choice") or payload.get("selected") or payload.get("plan")
    plans = payload.get("plans")
    if plans is not None:
        chosen = _chosen_spec(payload)
        bag_ids = chosen.get("bags") or chosen.get("bag_ids") or chosen.get("selected_bags") or []
        choice_name = chosen.get("name", choice_name)
        if at is None:
            at = chosen.get("at")
            if at is not None:
                at = minute(at)
    else:
        chosen = payload
        bag_ids = (payload.get("bags") or payload.get("bag_ids")
                   or payload.get("selected_bags") or [])
        choice_name = choice_name or payload.get("name") or "selection"
    require(isinstance(bag_ids, list) and bag_ids, "signed choice must name selected bags")
    require(len(bag_ids) <= MAX_BAGS, f"expected at most {MAX_BAGS} bags")

    decision_id = _pick(payload, "decision_id", "id", "signature_id")
    request_digest = digest(payload)
    if decision_id is None:
        decision_id = "decision-" + request_digest[:24]
    else:
        decision_id = _label(decision_id, "decision id")

    cfg = config(db)
    choice(desk, cfg["desks"])
    old = one(db, "SELECT * FROM bag_decisions WHERE decision_id = ?", decision_id)
    if old is not None:
        require(old["digest"] == request_digest or old["body"] == encode(payload),
                "decision identifier collision with different signed content")
        result = decode(old["report"])
        result["duplicate"] = True
        return result

    # The copy-based replay is prepared before taking the write transaction.
    # Inside that transaction every event is applied again through journal.apply,
    # which is the authoritative replay/current-state check.
    chosen_plan = _plan_ids(db, cfg, list(dict.fromkeys(bag_ids)), at=at, name=choice_name)
    comparison = compare_plans(db, payload, at=at) if plans is not None else None
    held_bags = []
    selected_set = set(chosen_plan["bags"])
    for row in db.execute("SELECT bag_id FROM bag_bags ORDER BY ordinal").fetchall():
        bag_id = row["bag_id"]
        if bag_id in selected_set:
            continue
        lock = one(db, "SELECT decision_id FROM bag_bag_locks WHERE bag_id = ?", bag_id)
        if lock is not None:
            continue
        held_review = _plan_ids(db, cfg, [bag_id], at=at)
        held_bags.append({
            "bag_id": bag_id, "valid": held_review["valid"], "issues": held_review["issues"],
            "dependencies": held_review["dependencies"], "capacity": held_review["capacity"],
            "decisions": held_review["decisions"],
        })

    require(chosen_plan["valid"], "chosen merge arrangement is not replayable: " +
            "; ".join(item["message"] for item in chosen_plan["issues"][:5]))

    with transaction(db, write=True):
        old = one(db, "SELECT * FROM bag_decisions WHERE decision_id = ?", decision_id)
        if old is not None:
            require(old["digest"] == request_digest or old["body"] == encode(payload),
                    "decision identifier collision with different signed content")
            result = decode(old["report"])
            result["duplicate"] = True
            return result

        marks = ",".join("?" for _ in bag_ids)
        locks = db.execute(
            f"SELECT bag_id, decision_id FROM bag_bag_locks WHERE bag_id IN ({marks})",
            tuple(bag_ids)).fetchall() if bag_ids else []
        foreign = [dict(row) for row in locks if row["decision_id"] != decision_id]
        require(not foreign, f"bags already locked by another decision: {foreign}")

        for original in chosen_plan["originals"]:
            existing_lock = one(
                db,
                "SELECT decision_id FROM bag_locks WHERE author = ? AND external_id = ?",
                original["author"], original["external_id"])
            require(existing_lock is None or existing_lock["decision_id"] == decision_id,
                    "field original is already locked by another signed choice")

        transaction_plan = _plan_ids(db, cfg, list(dict.fromkeys(bag_ids)), at=at,
                                     name=choice_name, dry_run=False)
        require(transaction_plan["valid"],
                "chosen arrangement is no longer replayable in the signing transaction: " +
                "; ".join(item["message"] for item in transaction_plan["issues"][:5]))

        # Apply through the same event/journal rules used by ordinary uploads.
        applied = []
        for event in transaction_plan["events"]:
            previous = one(db, "SELECT body FROM events WHERE id = ?", event["id"])
            if previous is not None:
                require(previous["body"] == encode(event), f"event identifier collision: {event['id']}")
                receipt = one(db, "SELECT body FROM receipts WHERE event_id = ?", event["id"])
                require(receipt is not None, "receipt missing, rebuild before retry")
                applied.append({"event": event["id"], "duplicate": True, **decode(receipt["body"])})
            else:
                db.execute("INSERT INTO events(id, body) VALUES (?, ?)", (event["id"], encode(event)))
                result = journal.apply(db, cfg, event)
                applied.append({"event": event["id"], "duplicate": False, **result})

        report = {
            "decision": "bags_signed", "decision_id": decision_id, "desk": desk, "at": at,
            "choice": choice_name, "plan": chosen_plan, "plans_compared": comparison,
            "dependencies": chosen_plan["dependencies"],
            "satisfied_dependencies": [d for d in chosen_plan["dependencies"] if d["satisfied"]],
            "stopped_zones": chosen_plan["stopped_zones"],
            "stopped_segments": chosen_plan.get("stopped_segments", chosen_plan["stopped_zones"]),
            "failed_zones": chosen_plan["failed_zones"],
            "failed_segments": chosen_plan.get("failed_segments", chosen_plan["failed_zones"]),
            "lighting_hold_zones": chosen_plan["lighting_hold_zones"],
            "capacity": chosen_plan["capacity"], "decisions": chosen_plan["decisions"],
            "held_bags": held_bags, "event_ids": chosen_plan["event_ids"], "results": applied,
            "originals": chosen_plan["originals"], "duplicate": False, "verified": True,
            "transaction_check": {"valid": transaction_plan["valid"], "replayable": True},
        }
        db.execute("INSERT INTO bag_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (decision_id, desk, at, choice_name, request_digest, encode(payload), encode(report)))
        for bag_id in chosen_plan["bags"]:
            db.execute("INSERT OR REPLACE INTO bag_bag_locks VALUES (?, ?)", (bag_id, decision_id))
        for original in chosen_plan["originals"]:
            db.execute("INSERT OR REPLACE INTO bag_locks VALUES (?, ?, ?, ?, ?, ?)", (
                original["author"], original["external_id"], decision_id, original["bag_id"],
                original["digest"], original["event_id"]))
        return report


sign = sign_selection
sign_plan = sign_selection
sign_choice = sign_selection
choose = sign_selection
merge = sign_selection
merge_bags = sign_selection
commit_selection = sign_selection
approve = sign_selection


def get_decision(db, decision_id):
    ensure_schema(db)
    _label(decision_id, "decision id")
    row = one(db, "SELECT * FROM bag_decisions WHERE decision_id = ?", decision_id)
    require(row is not None, f"unknown signed bag decision: {decision_id}")
    result = decode(row["report"])
    result["body"] = decode(row["body"])
    return result


def list_decisions(db):
    ensure_schema(db)
    rows = db.execute("SELECT decision_id, desk, at, choice FROM bag_decisions ORDER BY rowid").fetchall()
    return [dict(row) for row in rows]


def verify_decision(db, decision_id, strict_state=False):
    """Verify signed receipts, journal rows, raw originals, and locks.

    Receipt equality is invariant after rebuild.  Zone state can legally move
    when later late evidence arrives, so state comparison is informational unless
    strict_state is requested.
    """
    ensure_schema(db)
    _label(decision_id, "decision id")
    row = one(db, "SELECT * FROM bag_decisions WHERE decision_id = ?", decision_id)
    require(row is not None, f"unknown signed bag decision: {decision_id}")
    stored = decode(row["report"])
    signed_results = {item.get("event"): item for item in stored.get("results", []) if item.get("event")}
    locks = db.execute("SELECT * FROM bag_locks WHERE decision_id = ?", (decision_id,)).fetchall()
    require(locks, "signed decision locks no originals")
    trace = []
    for lock in locks:
        event = one(db, "SELECT body FROM events WHERE id = ?", lock["event_id"])
        receipt = one(db, "SELECT body FROM receipts WHERE event_id = ?", lock["event_id"])
        raw = _find_exact(db, lock["author"], lock["external_id"], lock["digest"])
        bag = one(db, "SELECT bag_id FROM bag_bags WHERE bag_id = ?", lock["bag_id"])
        require(event is not None and receipt is not None, "locked original lacks event/receipt")
        require(raw is not None and bag is not None, "locked raw original is no longer traceable")
        event_body = decode(event["body"])
        require(event_body["id"] == lock["event_id"], "event mapping disagreement")
        receipt_body = decode(receipt["body"])
        signed_result = signed_results.get(lock["event_id"])
        if signed_result is not None:
            expected_receipt = {
                key: value for key, value in signed_result.items()
                if key not in ("event", "duplicate")
            }
            require(expected_receipt == receipt_body,
                    f"rebuilt receipt differs from signed receipt for {lock['event_id']}")
        trace.append({
            "author": lock["author"], "external_id": lock["external_id"], "digest": lock["digest"],
            "bag_id": lock["bag_id"], "event_id": lock["event_id"], "receipt": True, "raw": True,
        })

    zone_check = None
    state_matches = True
    if row["at"] is not None:
        cfg = config(db)
        at = row["at"]
        current = {z: conditions.zone_state(db, cfg, z, at) for z in sorted(cfg["zones"])}
        zone_check = {z: {"decision": state["decision"], "capacity": state["capacity"]}
                      for z, state in current.items()}
        expected = stored.get("capacity", {})
        expected_decisions = stored.get("decisions", {})
        for zone, capacity in expected.items():
            if current[zone]["capacity"] != capacity:
                state_matches = False
            if zone in expected_decisions and current[zone]["decision"] != expected_decisions[zone]:
                state_matches = False
        if strict_state:
            require(state_matches, "rebuilt zone state differs from signed arrangement")
    return {
        "verified": True, "decision_id": decision_id, "desk": row["desk"], "choice": row["choice"],
        "originals": len(locks), "event_ids": stored.get("event_ids", []), "trace": trace,
        "zones": zone_check, "state_matches_signed": state_matches,
        "signed_capacity": stored.get("capacity", {}),
        "signed_decisions": stored.get("decisions", {}),
    }


verify = verify_decision
verify_bag_decision = verify_decision
verify_signature = verify_decision


def verify_all(db):
    ensure_schema(db)
    ids = [row["decision_id"] for row in db.execute("SELECT decision_id FROM bag_decisions ORDER BY rowid")]
    details = [verify_decision(db, decision_id) for decision_id in ids]
    return {"verified": True, "decisions": len(ids), "decision_ids": ids, "details": details}
