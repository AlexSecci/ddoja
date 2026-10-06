PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta VALUES ('profile_version', '0');
INSERT OR IGNORE INTO meta VALUES ('paused', 'false');

CREATE TABLE IF NOT EXISTS companies(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, careers_url TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, last_scan_at TEXT,
 scan_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_documents(
 id TEXT PRIMARY KEY, source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 content TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions(
 document_id TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL,
 source_path TEXT NOT NULL, imported_at TEXT NOT NULL, PRIMARY KEY(document_id,sha256)
);
CREATE TABLE IF NOT EXISTS technologies(
 key TEXT PRIMARY KEY, name TEXT NOT NULL, answer TEXT NOT NULL
 CHECK(answer IN ('yes','no','unknown')), context TEXT NOT NULL,
 source TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS technology_aliases(
 alias TEXT PRIMARY KEY, technology_key TEXT NOT NULL REFERENCES technologies(key)
);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 external_key TEXT NOT NULL, requisition_id TEXT, url TEXT NOT NULL, title TEXT NOT NULL,
 location TEXT NOT NULL, country TEXT NOT NULL, employment TEXT NOT NULL,
 arrangement TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'new',
 run_status TEXT NOT NULL DEFAULT 'idle', jd_version INTEGER NOT NULL DEFAULT 1,
 reason TEXT NOT NULL DEFAULT '', active_stage TEXT, lease_owner TEXT, lease_token TEXT,
 lease_until REAL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(company_id, external_key)
);
CREATE TABLE IF NOT EXISTS job_versions(
 job_id TEXT NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
 jd TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
 source TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(job_id,version)
);
CREATE TABLE IF NOT EXISTS evaluations(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), jd_version INTEGER NOT NULL,
 profile_version INTEGER NOT NULL, decision TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), jd_version INTEGER NOT NULL,
 profile_version INTEGER NOT NULL, manifest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions(
 id TEXT PRIMARY KEY, key TEXT NOT NULL, technology_key TEXT,
 prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', answer TEXT,
 created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_question ON questions(key) WHERE status='pending';
CREATE TABLE IF NOT EXISTS question_jobs(
 question_id TEXT NOT NULL REFERENCES questions(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(question_id,job_id)
);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL,
 artifact_path TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
 attribution TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_evidence ON evidence(job_id,kind,sha256);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), state TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), action TEXT NOT NULL,
 jd_version INTEGER NOT NULL, package_id TEXT, terms_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources(
 key TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, lease_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT REFERENCES jobs(id), actor TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_sources(
 company_id TEXT PRIMARY KEY REFERENCES companies(id),
 adapter TEXT NOT NULL CHECK(adapter IN ('greenhouse','lever','lever_eu')),
 board_token TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 status TEXT NOT NULL, payload TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, lease_until REAL NOT NULL
);
UPDATE meta SET value='2' WHERE key='schema_version';
