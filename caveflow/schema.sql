CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    ordinal INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    event_id TEXT PRIMARY KEY REFERENCES events(id), body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster_sheets (
    id TEXT PRIMARY KEY, guide TEXT NOT NULL, members TEXT NOT NULL,
    at INTEGER NOT NULL, event_id TEXT NOT NULL REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS roster_marks (
    sheet TEXT NOT NULL REFERENCES roster_sheets(id), person TEXT NOT NULL,
    version INTEGER NOT NULL, at INTEGER NOT NULL, mark TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id), PRIMARY KEY (sheet, person, version)
);
CREATE TABLE IF NOT EXISTS gate_anchor (
    id INTEGER PRIMARY KEY CHECK (id = 1), at INTEGER NOT NULL, entries INTEGER NOT NULL,
    exits INTEGER NOT NULL, event_id TEXT NOT NULL REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS gate_reads (
    step INTEGER NOT NULL, version INTEGER NOT NULL, at INTEGER NOT NULL,
    entries INTEGER NOT NULL, exits INTEGER NOT NULL, mode TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(id), PRIMARY KEY (step, version)
);
CREATE TABLE IF NOT EXISTS gate_bridges (
    read_event TEXT PRIMARY KEY REFERENCES gate_reads(event_id), tail_in INTEGER NOT NULL,
    tail_out INTEGER NOT NULL, base_in INTEGER NOT NULL, base_out INTEGER NOT NULL,
    witnesses TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS patrol_notes (
    zone TEXT NOT NULL, at INTEGER NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
    capacity INTEGER NOT NULL, inspector TEXT NOT NULL, trace TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id), PRIMARY KEY (zone, at, version)
);
CREATE TABLE IF NOT EXISTS light_rounds (
    id TEXT PRIMARY KEY, zone TEXT NOT NULL, at INTEGER NOT NULL, tester TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id), UNIQUE (zone, at)
);
CREATE TABLE IF NOT EXISTS light_checks (
    round TEXT NOT NULL REFERENCES light_rounds(id), channel TEXT NOT NULL,
    version INTEGER NOT NULL, reading TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events(id),
    PRIMARY KEY (round, channel, version)
);
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY, sheet TEXT NOT NULL UNIQUE REFERENCES roster_sheets(id),
    members TEXT NOT NULL, roster_evidence TEXT NOT NULL, custodian TEXT NOT NULL,
    zone TEXT NOT NULL, dispatched_at INTEGER NOT NULL, exited_at INTEGER,
    dispatch_event TEXT NOT NULL REFERENCES events(id), exit_event TEXT REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY REFERENCES events(id), group_id TEXT NOT NULL REFERENCES groups(id),
    giver TEXT NOT NULL, receiver TEXT NOT NULL, zone TEXT NOT NULL, at INTEGER NOT NULL,
    status TEXT NOT NULL, resolved_event TEXT REFERENCES events(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_transfer ON transfers(group_id) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS desk_offers (
    id TEXT PRIMARY KEY REFERENCES events(id), giver TEXT NOT NULL, receiver TEXT NOT NULL,
    at INTEGER NOT NULL, manifest TEXT NOT NULL, status TEXT NOT NULL,
    resolved_event TEXT REFERENCES events(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_desk_offer ON desk_offers(status) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS closing (
    id TEXT PRIMARY KEY REFERENCES events(id), at INTEGER NOT NULL, desk TEXT NOT NULL,
    status TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_seal ON closing(status) WHERE status = 'sealed';
CREATE TABLE IF NOT EXISTS operation_clock (
    id INTEGER PRIMARY KEY CHECK (id = 1), at INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id)
);
