"""SQLite memory for the HW4 exchange (lives on Maritime's persistent disk).

Tables
  meta      key/value: own user id, topic id, failure counter, halt flag, cached offer list
  seen      forum entries already processed
  writes    every write attempted, recorded BEFORE the POST with an idempotency key
  requests  the requests we sent to providers (one row per provider attempt) and their fate
  services  requests other agents sent to our offer and what we answered
  runs      one row per cycle
"""
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen (entry_id INTEGER PRIMARY KEY, parent_id INTEGER, user_id INTEGER, author TEXT,
  created_at TEXT, first_seen_run TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS writes (id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT UNIQUE, kind TEXT,
  target_entry_id INTEGER, message TEXT, status TEXT, canvas_entry_id INTEGER, run_id TEXT,
  created_at REAL, updated_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, offer_entry_id INTEGER, provider_user_id INTEGER,
  provider_name TEXT, request_entry_id INTEGER, deadline REAL, status TEXT, result_entry_id INTEGER,
  verification TEXT, corrections INTEGER DEFAULT 0, followups INTEGER DEFAULT 0, tip INTEGER, created_at REAL, updated_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS services (request_entry_id INTEGER PRIMARY KEY, requester_user_id INTEGER, requester_name TEXT,
  status TEXT, reply_entry_id INTEGER, created_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, outcome TEXT, detail TEXT);
"""
COUNTED = ("pending", "ambiguous", "posted", "verified")   # anything that may have reached Canvas


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def idem_key(kind, target, message):
    return hashlib.sha256(f"{kind}|{target}|{normalize(message)}".encode()).hexdigest()[:24]


class Store:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_dir / "hw4.db", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # meta
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, default=str)))

    # runs
    def start_run(self, run_id):
        # close any run the previous cycle never ended (agent stopped mid-way); it is evidence, not a failure
        self.db.execute("UPDATE runs SET ended_at=?, outcome='incomplete' WHERE ended_at IS NULL", (time.time(),))
        self.db.execute("INSERT INTO runs(run_id, started_at) VALUES(?,?)", (run_id, time.time()))

    def end_run(self, run_id, outcome, **detail):
        self.db.execute("UPDATE runs SET ended_at=?, outcome=?, detail=? WHERE run_id=?",
                        (time.time(), outcome, json.dumps(detail, default=str), run_id))

    def current_run(self):
        return self.db.execute("SELECT * FROM runs WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()

    # seen
    def seen_ids(self):
        return {r[0] for r in self.db.execute("SELECT entry_id FROM seen")}

    def mark_seen(self, entries, run_id):
        with self.db:
            self.db.execute("BEGIN")
            for e in entries:
                self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?,?,?,?)",
                                (e["id"], e["parent_id"], e["user_id"], e["author"], e["created_at"], run_id, e["text"][:200]))

    # writes
    def record_intent(self, kind, target, message, run_id):
        key, now = idem_key(kind, target, message), time.time()
        try:
            self.db.execute("INSERT INTO writes(idem_key,kind,target_entry_id,message,status,run_id,created_at,updated_at)"
                            " VALUES(?,?,?,?,'pending',?,?,?)", (key, kind, target, message, run_id, now, now))
        except sqlite3.IntegrityError:
            return None
        return key

    def update_write(self, key, status, canvas_entry_id=None, note=None):
        self.db.execute("UPDATE writes SET status=?, canvas_entry_id=COALESCE(?,canvas_entry_id), note=COALESCE(?,note),"
                        " updated_at=? WHERE idem_key=?", (status, canvas_entry_id, note, time.time(), key))

    def write_by_key(self, key):
        return self.db.execute("SELECT * FROM writes WHERE idem_key=?", (key,)).fetchone()

    def unresolved_writes(self):
        return self.db.execute("SELECT * FROM writes WHERE status IN ('pending','ambiguous','posted')").fetchall()

    def writes_in_last_hour(self):
        q = f"SELECT COUNT(*) FROM writes WHERE created_at > ? AND status IN ({','.join('?' * len(COUNTED))})"
        return self.db.execute(q, (time.time() - 3600, *COUNTED)).fetchone()[0]

    def writes_in_run(self, run_id):
        q = f"SELECT COUNT(*) FROM writes WHERE run_id=? AND status IN ({','.join('?' * len(COUNTED))})"
        return self.db.execute(q, (run_id, *COUNTED)).fetchone()[0]

    def own_messages(self):
        q = f"SELECT message FROM writes WHERE status IN ({','.join('?' * len(COUNTED))})"
        return [r[0] for r in self.db.execute(q, COUNTED)]

    def verified_write(self, kind, target=None):
        if target is None:
            return self.db.execute("SELECT * FROM writes WHERE kind=? AND status IN ('verified','posted') ORDER BY id DESC LIMIT 1",
                                   (kind,)).fetchone()
        return self.db.execute("SELECT * FROM writes WHERE kind=? AND target_entry_id=? AND status IN ('verified','posted')"
                               " ORDER BY id DESC LIMIT 1", (kind, target)).fetchone()

    def all_writes(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM writes ORDER BY id")]

    # requests (our outgoing delegation)
    def new_request(self, offer_entry_id, provider_user_id, provider_name, request_entry_id, deadline, note=None):
        now = time.time()
        self.db.execute("INSERT INTO requests(offer_entry_id,provider_user_id,provider_name,request_entry_id,deadline,status,"
                        "created_at,updated_at,note) VALUES(?,?,?,?,?,'awaiting_result',?,?,?)",
                        (offer_entry_id, provider_user_id, provider_name, request_entry_id, deadline, now, now, note))

    def active_request(self):
        return self.db.execute("SELECT * FROM requests WHERE status IN ('awaiting_result','result_received','correction_requested',"
                               "'followed_up') ORDER BY id DESC LIMIT 1").fetchone()

    def accepted_request(self):
        return self.db.execute("SELECT * FROM requests WHERE status='accepted' ORDER BY id DESC LIMIT 1").fetchone()

    def update_request(self, rid, **fields):
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE requests SET {cols} WHERE id=?", (*[json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
                                                                        for v in fields.values()], rid))

    def tried_offer_ids(self):
        return {r[0] for r in self.db.execute("SELECT offer_entry_id FROM requests")}

    def all_requests(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM requests ORDER BY id")]

    # services (incoming requests on our offer)
    def service(self, request_entry_id):
        return self.db.execute("SELECT * FROM services WHERE request_entry_id=?", (request_entry_id,)).fetchone()

    def upsert_service(self, request_entry_id, requester_user_id, requester_name, status, reply_entry_id=None, note=None):
        self.db.execute("INSERT INTO services VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_entry_id) DO UPDATE SET status=excluded.status,"
                        " reply_entry_id=COALESCE(excluded.reply_entry_id, services.reply_entry_id), note=COALESCE(excluded.note, services.note)",
                        (request_entry_id, requester_user_id, requester_name, status, reply_entry_id, time.time(), note))

    def all_services(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM services ORDER BY created_at")]

    def recent_runs(self, n=20):
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (n,))]
