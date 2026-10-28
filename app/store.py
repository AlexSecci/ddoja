from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identity(text):
    return re.sub(r"\s+", " ", text.strip().casefold())


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


LISTING_FACT_VALUES = {
    "country": ("US", "other"),
    "employment": ("full_time", "internship", "temporary", "part_time"),
    "arrangement": ("onsite", "hybrid", "remote"),
}

# This is agent guidance, not a scripted listing classifier. Keep the policy
# available even to legacy agentic runs whose stored filter JSON predates it.
AGENT_CAREER_SCOPE = {
    "required_scope": "Only consider roles involving substantive computer-science, computing, software, or closely related technical work.",
    "selection_basis": "Judge the actual duties and full job description, not title wording, department labels, or isolated keywords. A missing title does not relax this scope; use the duties. A generic 'engineer' label alone is not enough.",
    "examples": [
        "Software engineering or development, including backend, frontend, and full-stack work.",
        "Systems, platform, infrastructure, distributed systems, networking, operating systems, runtime, or reliability work when the duties involve substantive computing or software.",
        "GPU or other accelerated computing, high-performance computing, graphics, simulation, compilers, and related software.",
        "Data engineering, developer tools, build or release tooling, and software automation.",
    ],
    "examples_are_non_exhaustive": True,
    "unrelated_work": "Do not select clearly unrelated work such as janitorial, retail, manual production, or general engineering with no substantive software/computing duties.",
    "borderline_work": "If the full description makes a plausible case for substantive CS/software work but leaves genuine ambiguity, select it for evaluation rather than rejecting it on a keyword or title rule.",
    "career_change": "The user is open to career changes within this CS-related scope; do not require the role's title or department to match the applicant's past title.",
    "optional_title_focus": "If title_contains is nonempty, treat it as a narrower semantic role focus: duties, synonyms, and equivalent wording may match without a literal title substring. If blank, it adds no extra narrowing beyond the required CS scope.",
}

AGENT_HOST_CONTEXT_KEYS = {
    "final_action_may_have_been_sent", "final_action_authorized_at", "final_action_review_item_id",
    "final_action_jd_version", "final_action_profile_version", "requested_artifacts",
    "document_package_id", "document_request_reason", "form_context", "draft_artifacts",
    "manifest_path", "manifest_sha256", "documents_ready", "ready_artifacts",
    "prepared_documents", "inbox_history", "current_approved_actions",
    "first_application_review_required", "parent_context", "snapshot_supersession",
    "profile_refresh_supersession",
    "discovery_turn_baseline", "last_context_revision", "browser_wait_scope", "instruction_digest",
    "reconciliation_recovery", "execution", "execution_history", "execution_handoff", "execution_change_deferred",
    "email_verification_final_scope",
    "instruction_contract_id", "context_handoff_pending", "context_handoffs",
}

AGENT_DISCOVERY_PROGRESS_FIELDS = {
    "tab_url", "current_url", "current_page", "page", "page_cursor", "pages",
    "remaining_pages", "results_count", "reviewed_requisitions", "last_requisition_id",
    "last_listing_url", "search_filters", "search_paths", "category",
}


class Conflict(ValueError):
    pass


class Store:
    def configure_agent_trial(self, pipeline_id, job_ids, *, seconds=900, max_invocations=12, max_discovery_invocations=1):
        from .trial_limits import PREFIX
        if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 3 or len(set(job_ids)) != len(job_ids)
                or (seconds is not None and (type(seconds) is not int or not 30 <= seconds <= 1800))
                or (max_invocations is not None and (type(max_invocations) is not int or not 1 <= max_invocations <= 30))
                or type(max_discovery_invocations) is not int or not 0 <= max_discovery_invocations <= 1):
            raise ValueError("Invalid bounded trial limits.")
        with self.connect(write=True) as db:
            pipeline = db.execute("SELECT * FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not pipeline or pipeline["status"] == "completed":
                raise Conflict("A bounded trial requires an unfinished agentic campaign.")
            if db.execute("SELECT 1 FROM agent_workers WHERE pipeline_id=? AND turn_active=1", (pipeline_id,)).fetchone():
                raise Conflict("Prepare trial limits only while workers are stopped.")
            for job_id in job_ids:
                if not db.execute("SELECT 1 FROM pipeline_jobs p JOIN jobs j ON j.id=p.job_id WHERE p.pipeline_id=? AND p.job_id=? AND p.selected=1 AND j.phase NOT IN ('submitted','discarded')", (pipeline_id, job_id)).fetchone():
                    raise Conflict("Each trial job must be selected and unfinished in this campaign.")
                for worker in db.execute("SELECT * FROM agent_workers WHERE pipeline_id=? AND job_id=? AND role='submission'", (pipeline_id, job_id)):
                    if self._context_may_have_sent_final(self._agent_decode(worker["context"], {}), worker["checkpoint"]):
                        raise Conflict("A possibly sent application cannot enter a new-provider trial.")
            now = time.time()
            policy = {"state": "active", "pipeline_id": pipeline_id, "job_ids": job_ids, "provider": "deepseek", "model": "deepseek-flash",
                      "started_at": timestamp(), "deadline_unix": now + seconds if seconds is not None else None, "max_invocations": max_invocations,
                      "max_discovery_invocations": max_discovery_invocations,
                      "execution_watermark": db.execute("SELECT COALESCE(MAX(rowid),0) FROM agent_execution_runs").fetchone()[0]}
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PREFIX + pipeline_id, json.dumps(policy)))
            self.event(db, "agent.trial_configured", policy, actor="user")
            return policy

    def get_agent_trial(self, pipeline_id):
        from .trial_limits import read, runs
        with self.connect() as db:
            policy = read(db, pipeline_id)
            return {**policy, "invocations": len(runs(db, policy))} if policy else None

    def active_agent_trials(self):
        from .trial_limits import PREFIX
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT value FROM meta WHERE key LIKE ?", (PREFIX + '%',)) if json.loads(r[0]).get("state") == "active"]

    def agent_trial_stop_reason(self, pipeline_id):
        from .trial_limits import read, stop_reason
        with self.connect() as db:
            policy = read(db, pipeline_id)
            return stop_reason(db, policy) if policy and policy["state"] == "active" else None

    def close_agent_trial(self, pipeline_id, reason):
        from .trial_limits import PREFIX, read
        with self.connect(write=True) as db:
            policy = read(db, pipeline_id)
            if policy and policy["state"] == "active":
                policy.update(state="ended", ended_at=timestamp(), stop_reason=reason)
                db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(policy), PREFIX + pipeline_id))
                self.event(db, "agent.trial_ended", policy, actor="agent_scheduler")

    def get_worker_settings(self):
        from .worker_settings import read_settings, model_catalog, ROLE_LABELS
        from .provider_credentials import ProviderCredentials
        from .browser_backends import playwright_status
        with self.connect() as db:
            saved = read_settings(self.root, db)
        return {**saved, "providers": model_catalog(self.root), "role_labels": ROLE_LABELS,
                "credentials": {"deepseek": ProviderCredentials(self.root).status()},
                "playwright": playwright_status(self.root)}

    def save_worker_settings(self, revision, roles):
        from .worker_settings import save_settings
        with self.connect(write=True) as db:
            saved = save_settings(self.root, db, revision, roles)
            self.event(db, "agent.worker_settings_changed", saved, actor="user")
        # A preferences change never schedules, resumes, stops or replaces work.
        return self.get_worker_settings()

    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.path = self.root / "data" / "pipeline.sqlite"
        with self.connect() as db:
            db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            from .request_metrics import SCHEMA as request_metrics_schema
            db.executescript(request_metrics_schema)
            columns = {row[1] for row in db.execute("PRAGMA table_info(pipeline_jobs)")}
            for name, declaration in (
                    ("manual_discard_reason", "TEXT NOT NULL DEFAULT ''"),
                    ("manual_discard_provenance", "TEXT NOT NULL DEFAULT ''"),
                    ("manual_discard_actor", "TEXT NOT NULL DEFAULT ''"),
                    ("manual_discarded_at", "TEXT"),
                    ("agent_decision", "TEXT NOT NULL DEFAULT ''"),
                    ("agent_reason", "TEXT NOT NULL DEFAULT ''"),
                    ("agent_status", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE pipeline_jobs ADD COLUMN {name} {declaration}")
            pipeline_columns = {row[1] for row in db.execute("PRAGMA table_info(pipelines)")}
            for name, declaration in (
                    ("mode", "TEXT NOT NULL DEFAULT 'legacy'"),
                    ("adopted_from_pipeline_id", "TEXT"),
                    ("agent_test_only", "INTEGER NOT NULL DEFAULT 0"),
                    ("dashboard_hidden", "INTEGER NOT NULL DEFAULT 0")):
                if name not in pipeline_columns:
                    db.execute(f"ALTER TABLE pipelines ADD COLUMN {name} {declaration}")
            worker_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_workers)")}
            if "turn_active" not in worker_columns:
                db.execute("ALTER TABLE agent_workers ADD COLUMN turn_active INTEGER NOT NULL DEFAULT 0")
            if "execution" not in {row[1] for row in db.execute("PRAGMA table_info(agent_usage)")}:
                db.execute("ALTER TABLE agent_usage ADD COLUMN execution TEXT NOT NULL DEFAULT '{}'")
            artifact_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_artifacts)")}
            for name, declaration in (("pdf_path", "TEXT NOT NULL DEFAULT ''"),
                                      ("pdf_sha256", "TEXT NOT NULL DEFAULT ''"),
                                      ("renderer_sha256", "TEXT NOT NULL DEFAULT ''"),
                                      ("preview_paths", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in artifact_columns:
                    db.execute(f"ALTER TABLE agent_artifacts ADD COLUMN {name} {declaration}")
            capture_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_listing_captures)")}
            for name, declaration in (("capture_ref", "TEXT NOT NULL DEFAULT ''"),
                                      ("jd_version", "INTEGER NOT NULL DEFAULT 1"),
                                      ("description_sha256", "TEXT NOT NULL DEFAULT ''")):
                if name not in capture_columns:
                    db.execute(f"ALTER TABLE agent_listing_captures ADD COLUMN {name} {declaration}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_listing_capture_ref ON agent_listing_captures(capture_ref) "
                       "WHERE capture_ref!=''")

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, kind, payload, job_id=None, actor="service"):
        db.execute("INSERT INTO events(job_id,actor,kind,payload,created_at) VALUES(?,?,?,?,?)",
                   (job_id, actor, kind, json.dumps(payload, ensure_ascii=False), timestamp()))

    def profile_version(self, db=None):
        if db is None:
            with self.connect() as conn:
                return self.profile_version(conn)
        return int(db.execute("SELECT value FROM meta WHERE key='profile_version'").fetchone()[0])

    def company(self, company_id, name, url, enabled=False):
        company_id = identity(company_id)
        if company_id == "tesla" and enabled:
            raise Conflict("Tesla is excluded from the pilot.")
        with self.connect(write=True) as db:
            old = db.execute("SELECT name,careers_url,enabled FROM companies WHERE id=?", (company_id,)).fetchone()
            active = db.execute("SELECT 1 FROM pipelines WHERE company_id=? AND status IN ('queued','running','stopping','awaiting_user') LIMIT 1",
                                (company_id,)).fetchone()
            if old and active and (old["name"] != name or old["careers_url"] != url or old["enabled"] != int(bool(enabled))):
                raise Conflict("Company details cannot change while its pipeline is queued, running or awaiting user input.")
            db.execute("INSERT INTO companies(id,name,careers_url,enabled) VALUES(?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,careers_url=excluded.careers_url,enabled=excluded.enabled",
                       (company_id, name, url, int(enabled)))
        return company_id

    @staticmethod
    def infer_source_from_url(url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.casefold()
        parts = [part for part in parsed.path.split("/") if part]
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
            token = parts[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
                return {"adapter": "greenhouse", "board_token": token}
        if host == "jobs.lever.co" and parts:
            token = parts[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
                return {"adapter": "lever", "board_token": token}
        if host == "jobs.eu.lever.co" and parts:
            token = parts[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
                return {"adapter": "lever_eu", "board_token": token}
        return None

    def source(self, company_id, adapter, board_token):
        if adapter not in {"greenhouse", "lever", "lever_eu"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", board_token):
            raise ValueError("Use a supported adapter and the verified board token from the employer's career site.")
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
                raise KeyError(company_id)
            if db.execute("SELECT 1 FROM scans WHERE company_id=? AND status='running'", (company_id,)).fetchone():
                raise Conflict("Cannot change a source while its scan is running.")
            if db.execute("SELECT 1 FROM pipelines WHERE company_id=? AND status IN ('queued','running','stopping','awaiting_user') LIMIT 1",
                          (company_id,)).fetchone():
                raise Conflict("Cannot change a company source while its pipeline is queued, running or awaiting user input.")
            db.execute("INSERT INTO company_sources VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET "
                       "adapter=excluded.adapter,board_token=excluded.board_token,updated_at=excluded.updated_at",
                       (company_id, adapter, board_token, timestamp()))
            self.event(db, "company.source_configured", {"company_id": company_id, "adapter": adapter, "board_token": board_token}, actor="user")

    def discover(self, company_id, record):
        required = ("url", "title", "jd", "country", "employment", "arrangement", "location")
        if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
            raise ValueError("A complete description and explicit structured listing fields are required.")
        parsed = urlsplit(record["url"])
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("An absolute HTTP(S) listing URL is required.")
        url = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))
        requisition = str(record.get("requisition_id") or "").strip() or None
        external = "req:" + requisition if requisition else "url:" + url
        metadata = {key: record[key] for key in required if key not in ("jd", "url")}
        checksum = digest(json.dumps({"jd": record["jd"], **metadata}, sort_keys=True))
        now = timestamp()
        with self.connect(write=True) as db:
            old = db.execute("SELECT * FROM jobs WHERE company_id=? AND external_key=?",
                             (company_id, external)).fetchone()
            if old:
                previous = db.execute("SELECT sha256 FROM job_versions WHERE job_id=? AND version=?",
                                      (old["id"], old["jd_version"])).fetchone()[0]
                if previous == checksum:
                    db.execute("UPDATE jobs SET last_seen_at=?,url=? WHERE id=?", (now, url, old["id"]))
                    return {"id": old["id"], "created": False, "changed": False}
                job_id, version = old["id"], old["jd_version"] + 1
                # Never erase a submission or an unresolved possible submission on rediscovery.
                interrupted_submission = old["active_stage"] == "submission" and old["run_status"] == "running"
                protected = old["phase"] == "submitted" or old["run_status"] == "submission_uncertain" or interrupted_submission
                db.execute("UPDATE jobs SET title=?,url=?,location=?,country=?,employment=?,arrangement=?,"
                           "jd_version=?,last_seen_at=?,phase=?,run_status=?,reason=?,active_stage=NULL,"
                           "lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                           (record["title"], url, record["location"], record["country"], record["employment"],
                            record["arrangement"], version, now, old["phase"] if protected else "new",
                            "submission_uncertain" if interrupted_submission else old["run_status"] if protected else "idle",
                            "Description changed; reassessment or outcome reconciliation required.", job_id))
            else:
                job_id, version = uuid.uuid4().hex, 1
                db.execute("INSERT INTO jobs(id,company_id,external_key,requisition_id,url,title,location,"
                           "country,employment,arrangement,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, company_id, external, requisition, url, record["title"], record["location"],
                            record["country"], record["employment"], record["arrangement"], now, now))
            db.execute("INSERT INTO job_versions VALUES(?,?,?,?,?,?,?)",
                       (job_id, version, record["jd"], checksum, json.dumps(metadata), record.get("source", url), now))
            self.event(db, "job.changed" if old else "job.discovered", {"jd_version": version}, job_id)
            return {"id": job_id, "created": old is None, "changed": old is not None}

    def get_job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = dict(row)
            job["description"] = dict(db.execute("SELECT * FROM job_versions WHERE job_id=? AND version=?",
                                                  (job_id, job["jd_version"])).fetchone())
            job["evaluations"] = [dict(r) for r in db.execute("SELECT * FROM evaluations WHERE job_id=? ORDER BY created_at,rowid", (job_id,))]
            job["packages"] = [dict(r) for r in db.execute("SELECT * FROM packages WHERE job_id=? ORDER BY created_at,rowid", (job_id,))]
            job["evidence"] = [dict(r) for r in db.execute("SELECT * FROM evidence WHERE job_id=?", (job_id,))]
            outcome = db.execute("SELECT * FROM application_outcomes WHERE job_id=?", (job_id,)).fetchone()
            job["application_outcome"] = dict(outcome) if outcome else None
            return job

    def listing(self, table):
        if table not in {"companies", "jobs", "technologies", "questions", "events", "source_documents", "evidence", "company_sources", "scans", "pipelines", "pipeline_jobs", "application_outcomes", "submission_approvals", "submission_questions", "listing_fact_questions", "agent_workers", "agent_inbox", "agent_runtime_events", "agent_submission_results", "agent_artifacts", "agent_adoptions", "agent_legacy_question_links"}:
            raise ValueError("Invalid table.")
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def create_pipeline(self, company_id, title_contains="", location_contains=""):
        company_id = identity(company_id)
        title_contains = " ".join(str(title_contains or "").split())
        location_contains = " ".join(str(location_contains or "").split())
        if len(title_contains) > 120 or len(location_contains) > 120:
            raise ValueError("Title and location filters must be 120 characters or fewer.")
        filters = {"country": "US", "employment": "full_time", "arrangement": ["onsite", "hybrid"],
                   "title_contains": title_contains, "location_contains": location_contains}
        pipeline_id, now = uuid.uuid4().hex, timestamp()
        with self.connect(write=True) as db:
            company = db.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
            if not company:
                raise KeyError(company_id)
            source = db.execute("SELECT adapter FROM company_sources WHERE company_id=?", (company_id,)).fetchone()
            blockers = []
            if company_id == "tesla":
                blockers.append("Tesla is excluded from the pilot.")
            if not company["enabled"]:
                blockers.append("Enable this company before starting discovery.")
            if not source:
                blockers.append("No supported public job-board feed is configured. Configure a verified Greenhouse or Lever board source.")
            if not blockers:
                active = db.execute("SELECT 1 FROM pipelines WHERE company_id=? AND status IN ('queued','running','stopping','awaiting_user') LIMIT 1",
                                    (company_id,)).fetchone()
                if active:
                    raise Conflict("This company already has an active pipeline. Resume or stop it before starting another.")
            status = "blocked" if blockers else "queued"
            discovery_status = "blocked" if blockers else "pending"
            discovery_payload = {"status": discovery_status, "detail": blockers[0] if blockers else ""}
            db.execute("INSERT INTO pipelines(id,company_id,status,stage,filters,discovery_status,discovery_payload,blockers,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (pipeline_id, company_id, status, "discovery", json.dumps(filters), discovery_status,
                        json.dumps(discovery_payload), json.dumps(blockers), now, now))
            self.event(db, "pipeline.created", {"pipeline_id": pipeline_id, "company_id": company_id,
                       "status": status, "filters": filters, "blockers": blockers})
        return self.get_pipeline(pipeline_id)

    def _pipeline_counts(self, db, pipeline_id):
        current_profile = self.profile_version(db)
        rows = db.execute("SELECT j.*,pj.selected,pj.manual_discard_reason,o.status AS application_status,"
                          "EXISTS(SELECT 1 FROM evidence e WHERE e.job_id=j.id) AS has_submission_evidence "
                          "FROM pipeline_jobs pj "
                          "JOIN jobs j ON j.id=pj.job_id LEFT JOIN application_outcomes o ON o.job_id=j.id "
                          "WHERE pj.pipeline_id=?", (pipeline_id,)).fetchall()
        counts = {key: 0 for key in ("discovered", "evaluated", "accepted", "discarded", "submitted",
                                     "recruiter_pending", "recruiter_rejected", "recruiter_accepted",
                                     "recruiter_unknown", "awaiting_user", "uncertain", "excluded", "selected")}
        counts["discovered"] = len(rows)
        for row in rows:
            if not row["selected"]:
                counts["excluded"] += 1
                continue
            counts["selected"] += 1
            evaluation = db.execute("SELECT decision FROM evaluations WHERE job_id=? AND jd_version=? "
                                    "AND profile_version=? ORDER BY rowid DESC LIMIT 1",
                                    (row["id"], row["jd_version"], current_profile)).fetchone()
            decision = evaluation["decision"] if evaluation else None
            if decision:
                counts["evaluated"] += 1
            if decision == "approve":
                counts["accepted"] += 1
            if row["phase"] == "submitted" and row["has_submission_evidence"]:
                counts["submitted"] += 1
                outcome = row["application_status"] or "unknown"
                counts["recruiter_" + outcome] += 1
                # Evaluated/accepted are cumulative funnel counts; only the terminal
                # discarded bucket is exclusive with verified submitted.
                continue
            if row["manual_discard_reason"]:
                counts["discarded"] += 1
                continue
            if decision == "discard" or row["phase"] == "discarded":
                counts["discarded"] += 1
            if row["run_status"] in {"awaiting_information", "awaiting_user", "awaiting_material_review", "awaiting_manual_review"}:
                counts["awaiting_user"] += 1
            if row["run_status"] == "submission_uncertain":
                counts["uncertain"] += 1
        return counts

    def _pipeline_record(self, db, row):
        result = dict(row)
        result["filters"] = json.loads(result["filters"])
        result["blockers"] = json.loads(result["blockers"])
        result["discovery"] = json.loads(result.pop("discovery_payload"))
        if not isinstance(result["discovery"], dict):
            result["discovery"] = {}
        result["discovery"]["status"] = result["discovery_status"]
        if not result["discovery"].get("detail"):
            result["discovery"]["detail"] = result["blockers"][0] if result["blockers"] else ""
        company = db.execute("SELECT name,careers_url FROM companies WHERE id=?", (result["company_id"],)).fetchone()
        result["company_name"] = company[0] if company else result["company_id"]
        result["careers_url"] = company["careers_url"] if company else ""
        result["stop_requested"] = bool(result["stop_requested"])
        result["dashboard_hidden"] = bool(result.get("dashboard_hidden", 0))
        result["counts"] = (self._agent_pipeline_counts(db, result["id"])
                            if result.get("mode") == "agentic" else self._pipeline_counts(db, result["id"]))
        return result

    def get_pipeline(self, pipeline_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            return self._pipeline_record(db, row)

    def list_pipelines(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM pipelines ORDER BY created_at DESC,id DESC").fetchall()
            return [self._pipeline_record(db, row) for row in rows]

    def set_pipeline_visibility(self, pipeline_id, hidden):
        if not isinstance(hidden, bool):
            raise ValueError("Choose whether this campaign is hidden.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,dashboard_hidden FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if hidden and row["status"] not in {"completed", "stopped"}:
                raise Conflict("Only completed or stopped campaigns can be hidden. Stop this company first.")
            if bool(row["dashboard_hidden"]) != hidden:
                db.execute("UPDATE pipelines SET dashboard_hidden=? WHERE id=?", (int(hidden), pipeline_id))
                self.event(db, "pipeline.visibility_changed", {"pipeline_id": pipeline_id, "hidden": hidden}, actor="user")
        return self.get_pipeline(pipeline_id)

    @staticmethod
    def _context_may_have_sent_final(context, checkpoint):
        state = context if isinstance(context, dict) else {}
        return bool(state.get("final_action_may_have_been_sent")) or checkpoint in {
            "final_action_in_flight", "final_action_uncertain", "submission_uncertain"}

    def _manual_auth_state(self, db, job_id, pipeline_id):
        row = db.execute(
            "SELECT j.phase,j.run_status,j.jd_version AS current_jd_version,j.active_stage,j.lease_token,"
            "p.status AS pipeline_status,p.stop_requested,pj.selected,"
            "c.job_id AS context_job_id,c.pipeline_id AS context_pipeline_id,c.session_id AS saved_session_id,"
            "c.tab_id AS saved_tab_id,c.checkpoint AS saved_checkpoint,c.package_id AS saved_package_id,"
            "c.jd_version AS saved_jd_version,c.profile_version AS saved_profile_version,"
            "c.package_sha256 AS saved_package_sha256,c.context AS saved_context "
            "FROM jobs j "
            "JOIN pipeline_jobs pj ON pj.job_id=j.id AND pj.pipeline_id=? "
            "JOIN pipelines p ON p.id=pj.pipeline_id "
            "LEFT JOIN submission_contexts c ON c.job_id=j.id AND c.pipeline_id=p.id "
            "WHERE j.id=?", (pipeline_id, job_id)).fetchone()
        if not row:
            raise KeyError(job_id)
        if not row["selected"]:
            return None, "This job is no longer selected in the pipeline."
        if row["pipeline_status"] in {"stopped", "stopping", "completed"} or row["stop_requested"]:
            return None, "This pipeline is stopped or complete; resume it separately before continuing."
        if row["phase"] != "ready_for_submission" or row["run_status"] != "submission_blocked":
            return None, "This job is not waiting at a safe manual sign-in checkpoint."
        if row["active_stage"] is not None or row["lease_token"] is not None:
            return None, "A worker still owns this job. Wait for its safe checkpoint."
        if db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt' LIMIT 1", (job_id,)).fetchone():
            return None, "A receipt already exists; reconcile it instead of resuming this application."
        if row["run_status"] == "submission_uncertain":
            return None, "The submission outcome is uncertain; reconcile it before any retry."
        if not row["saved_checkpoint"]:
            return None, "No saved manual sign-in checkpoint is available."
        try:
            context = json.loads(row["saved_context"] or "{}")
        except (TypeError, ValueError):
            return None, "The saved browser checkpoint is unreadable."
        if row["saved_checkpoint"] != "manual_authentication" or not isinstance(context, dict) or context.get("manual_handoff") is not True:
            return None, "The saved checkpoint is not a manual sign-in handoff."
        if self._context_may_have_sent_final(context, row["saved_checkpoint"]):
            return None, "A final action may already have been sent; reconcile it before any retry."
        if not row["saved_session_id"] or not row["saved_tab_id"]:
            return None, "The saved sign-in checkpoint lacks its owned browser session or tab."
        current_package = self._current_upload_package(
            db, job_id, row["current_jd_version"], self.profile_version(db), row["saved_package_id"])
        if (not current_package or row["saved_jd_version"] != row["current_jd_version"]
                or row["saved_profile_version"] != self.profile_version(db)
                or row["saved_package_sha256"] not in current_package["hashes"]):
            return None, "The applicant profile or reviewed upload package changed; this saved tab cannot be resumed."
        if context.get("manual_auth_acknowledged"):
            return dict(row), "Your sign-in acknowledgment is saved; the pipeline is reaching its safe checkpoint."
        return dict(row), "Sign-in can resume in the preserved employer tab."

    def manual_auth_resume_status(self, job_id, pipeline_id):
        with self.connect() as db:
            row, note = self._manual_auth_state(db, job_id, pipeline_id)
            return {"available": row is not None,
                    "checkpoint": "manual_authentication" if row is not None else None,
                    "note": note}

    def acknowledge_manual_authentication(self, job_id, pipeline_id, provenance, actor="user"):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Confirm that sign-in is complete and include acknowledgment provenance.")
        with self.connect(write=True) as db:
            row, note = self._manual_auth_state(db, job_id, pipeline_id)
            if row is None:
                raise Conflict(note)
            context = json.loads(row["saved_context"] or "{}")
            if context.get("manual_auth_acknowledged"):
                return {"job_id": job_id, "pipeline_id": pipeline_id, "status": "acknowledged"}
            now = timestamp()
            context["manual_auth_acknowledged"] = True
            context["manual_auth_acknowledged_at"] = now
            context["manual_auth_provenance"] = provenance.strip()
            db.execute("UPDATE submission_contexts SET context=?,updated_at=? WHERE job_id=? AND pipeline_id=?",
                       (json.dumps(context, ensure_ascii=False), now, job_id, pipeline_id))
            self.event(db, "submission.manual_auth_acknowledged",
                       {"pipeline_id": pipeline_id, "provenance": provenance.strip()}, job_id, actor)
        return {"job_id": job_id, "pipeline_id": pipeline_id, "status": "acknowledged"}

    def activate_manual_authentication_resume(self, job_id, pipeline_id):
        with self.connect(write=True) as db:
            raw = db.execute("SELECT c.job_id,c.pipeline_id,c.session_id,c.tab_id,c.checkpoint,c.package_id,"
                             "c.jd_version AS saved_jd_version,c.profile_version AS saved_profile_version,"
                             "c.package_sha256,c.context,j.phase,j.run_status,j.jd_version AS current_jd_version,"
                             "j.active_stage,j.lease_token,p.status AS pipeline_status,p.stop_requested,pj.selected "
                             "FROM submission_contexts c JOIN jobs j ON j.id=c.job_id "
                             "JOIN pipelines p ON p.id=c.pipeline_id "
                             "JOIN pipeline_jobs pj ON pj.pipeline_id=c.pipeline_id AND pj.job_id=c.job_id "
                             "WHERE c.job_id=? AND c.pipeline_id=?", (job_id, pipeline_id)).fetchone()
            if not raw:
                raise Conflict("No saved manual sign-in checkpoint is available.")
            try:
                context = json.loads(raw["context"] or "{}")
            except (TypeError, ValueError):
                context = {}
            if (not isinstance(context, dict) or context.get("manual_auth_acknowledged") is not True
                    or raw["checkpoint"] != "manual_authentication" or context.get("manual_handoff") is not True
                    or self._context_may_have_sent_final(context, raw["checkpoint"])
                    or raw["phase"] != "ready_for_submission" or raw["run_status"] != "submission_blocked"
                    or raw["active_stage"] is not None or raw["lease_token"] is not None or not raw["selected"]
                    or raw["pipeline_status"] in {"stopped", "stopping", "completed"} or raw["stop_requested"]
                    or raw["saved_jd_version"] != raw["current_jd_version"]
                    or not raw["session_id"] or not raw["tab_id"]
                    or db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt' LIMIT 1", (job_id,)).fetchone()):
                raise Conflict("The manual sign-in checkpoint is no longer safe to resume.")
            current_package = self._current_upload_package(
                db, job_id, raw["current_jd_version"], self.profile_version(db), raw["package_id"])
            if (not current_package or raw["saved_profile_version"] != self.profile_version(db)
                    or raw["package_sha256"] not in current_package["hashes"]):
                raise Conflict("The reviewed package or applicant profile changed; this saved tab cannot be resumed.")
            if raw["pipeline_status"] == "running":
                raise Conflict("The pipeline is still working; wait for its safe checkpoint before resuming sign-in.")
            now = timestamp()
            context["manual_auth_acknowledged"] = False
            context["manual_auth_resumed_at"] = now
            provenance = context.get("manual_auth_provenance", "")
            db.execute("UPDATE submission_contexts SET context=?,updated_at=? WHERE job_id=? AND pipeline_id=?",
                       (json.dumps(context, ensure_ascii=False), now, job_id, pipeline_id))
            db.execute("UPDATE jobs SET run_status='idle',reason='User confirmed manual sign-in; resuming the saved browser session.' WHERE id=?",
                       (job_id,))
            self.event(db, "submission.manual_auth_resumed",
                       {"pipeline_id": pipeline_id, "provenance": provenance}, job_id, "user")
            return {"job_id": job_id, "pipeline_id": pipeline_id, "status": "ready"}

    def _manual_discard_status(self, db, job_id, pipeline_id):
        row = db.execute("SELECT j.phase,j.run_status,j.jd_version,j.active_stage,j.lease_token,p.status AS pipeline_status,"
                         "p.stop_requested,pj.selected,pj.manual_discard_reason,c.context,c.checkpoint "
                         "FROM jobs j JOIN pipeline_jobs pj ON pj.job_id=j.id AND pj.pipeline_id=? "
                         "JOIN pipelines p ON p.id=pj.pipeline_id "
                         "LEFT JOIN submission_contexts c ON c.job_id=j.id AND c.pipeline_id=p.id "
                         "WHERE j.id=?", (pipeline_id, job_id)).fetchone()
        if not row:
            raise KeyError(job_id)
        if row["manual_discard_reason"]:
            return None, "This job was already discarded in this pipeline."
        if not row["selected"]:
            return None, "This job is excluded from the pipeline."
        if row["pipeline_status"] == "completed":
            return None, "A completed pipeline cannot be changed."
        if row["run_status"] == "submission_uncertain":
            return None, "The submission outcome is uncertain; reconcile it before discarding."
        if row["phase"] == "submitted" or db.execute(
                "SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt' LIMIT 1", (job_id,)).fetchone():
            return None, "A verified application receipt exists; recruiter outcome must be reconciled instead."
        if row["active_stage"] is not None or row["lease_token"] is not None or row["run_status"] == "running":
            return None, "A worker still owns this job. Wait for its safe checkpoint."
        try:
            context = json.loads(row["context"] or "{}") if row["context"] else {}
        except (TypeError, ValueError):
            return None, "The saved browser checkpoint is unreadable; reconcile it before discarding."
        if self._context_may_have_sent_final(context, row["checkpoint"]):
            return None, "A final action may already have been sent; reconcile it before discarding."
        return dict(row), "This unsent listing can be discarded for this pipeline."

    def manual_discard_status(self, job_id, pipeline_id):
        with self.connect() as db:
            row, note = self._manual_discard_status(db, job_id, pipeline_id)
            return {"available": row is not None, "note": note}

    def discard_pipeline_job(self, job_id, pipeline_id, reason, provenance, actor="user"):
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise ValueError("A discard reason of 1 to 500 characters is required.")
        if not isinstance(provenance, str) or not provenance.strip() or len(provenance.strip()) > 500:
            raise ValueError("Discard provenance of 1 to 500 characters is required.")
        with self.connect(write=True) as db:
            row, note = self._manual_discard_status(db, job_id, pipeline_id)
            if row is None:
                raise Conflict(note)
            now = timestamp()
            db.execute("UPDATE pipeline_jobs SET manual_discard_reason=?,manual_discard_provenance=?,"
                       "manual_discard_actor=?,manual_discarded_at=? WHERE pipeline_id=? AND job_id=? AND selected=1",
                       (reason.strip(), provenance.strip(), actor, now, pipeline_id, job_id))
            self.event(db, "pipeline.job_discarded", {"pipeline_id": pipeline_id, "reason": reason.strip(),
                       "provenance": provenance.strip()}, job_id, actor)
        return {"job_id": job_id, "pipeline_id": pipeline_id, "reason": reason.strip(),
                "provenance": provenance.strip(), "actor": actor, "discarded_at": now}

    def pipeline_jobs(self, pipeline_id):
        with self.connect() as db:
            pipeline = db.execute("SELECT mode FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not pipeline:
                raise KeyError(pipeline_id)
            rows = db.execute("SELECT j.*,o.status AS application_status,o.provenance AS application_provenance,"
                              "pj.agent_decision,pj.agent_reason,pj.agent_status "
                              "FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id "
                              "LEFT JOIN application_outcomes o ON o.job_id=j.id "
                              "WHERE pj.pipeline_id=? ORDER BY pj.discovered_at,j.first_seen_at,j.id", (pipeline_id,)).fetchall()
            result = []
            for row in rows:
                job = dict(row)
                membership = db.execute("SELECT selected,excluded_reason,manual_discard_reason,manual_discard_provenance,"
                                        "manual_discard_actor,manual_discarded_at FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                        (pipeline_id, job["id"])).fetchone()
                job["selected"] = bool(membership["selected"])
                job["excluded_reason"] = membership["excluded_reason"]
                job["manual_discard_reason"] = membership["manual_discard_reason"]
                job["manual_discard_provenance"] = membership["manual_discard_provenance"]
                job["manual_discard_actor"] = membership["manual_discard_actor"]
                job["manual_discarded_at"] = membership["manual_discarded_at"]
                if pipeline["mode"] == "agentic":
                    job["profile_refresh"] = self._agent_profile_refresh_assessment(db, pipeline_id, job["id"])
                evaluation = db.execute("SELECT decision,payload,created_at FROM evaluations WHERE job_id=? AND jd_version=? "
                                        "AND profile_version=? ORDER BY rowid DESC LIMIT 1",
                                        (job["id"], job["jd_version"], self.profile_version(db))).fetchone()
                job["evaluation_status"] = evaluation["decision"] if evaluation else "pending"
                job["evaluation"] = json.loads(evaluation["payload"]) if evaluation else None
                job["application_status"] = job["application_status"] or ("unknown" if job["phase"] == "submitted" else None)
                job["application_provenance"] = job["application_provenance"] or ("No recruiter outcome has been recorded." if job["phase"] == "submitted" else None)
                auth, auth_note = self._manual_auth_state(db, job["id"], pipeline_id)
                job["manual_auth_resume"] = {"available": auth is not None,
                    "checkpoint": "manual_authentication" if auth is not None else None,
                    "note": auth_note}
                discard, discard_note = self._manual_discard_status(db, job["id"], pipeline_id)
                job["manual_discard"] = {"available": discard is not None, "note": discard_note}
                result.append(job)
            return {"pipeline_id": pipeline_id, "jobs": result}

    def _agent_profile_refresh_assessment(self, db, pipeline_id, job_id):
        """Return a read-only explanation of whether one selected job can be refreshed."""
        row = db.execute(
            "SELECT p.mode,p.status AS pipeline_status,p.stop_requested,p.discovery_status,"
            "pj.selected,j.jd_version,j.phase,j.run_status "
            "FROM pipelines p LEFT JOIN pipeline_jobs pj ON pj.pipeline_id=p.id AND pj.job_id=? "
            "LEFT JOIN jobs j ON j.id=pj.job_id WHERE p.id=?",
            (job_id, pipeline_id)).fetchone()
        if not row:
            return {"eligible": False, "reason": "This pipeline does not exist."}
        if row["mode"] != "agentic":
            return {"eligible": False, "reason": "Profile refresh is available only for agentic pipelines."}
        if row["selected"] is None:
            return {"eligible": False, "reason": "This job is not a member of the pipeline."}
        if not row["selected"]:
            return {"eligible": False, "reason": "This job is excluded from the pipeline."}
        if row["stop_requested"] or row["pipeline_status"] in {"stopped", "stopping", "completed"}:
            return {"eligible": False, "reason": "Resume or replace the stopped pipeline before refreshing a job."}
        if row["phase"] == "submitted" or row["run_status"] == "submission_uncertain":
            return {"eligible": False, "reason": "Submitted or uncertain applications cannot be refreshed."}
        if db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='receipt' LIMIT 1", (job_id,)).fetchone():
            return {"eligible": False, "reason": "A saved application receipt makes this job read-only."}
        if db.execute("SELECT 1 FROM agent_submission_results WHERE job_id=? AND state IN ('submitted','uncertain') LIMIT 1",
                      (job_id,)).fetchone():
            return {"eligible": False, "reason": "A submitted or uncertain agent result must be reconciled first."}
        if db.execute("SELECT 1 FROM agent_inbox WHERE pipeline_id=? AND job_id=? "
                      "AND kind='question' AND state='pending' LIMIT 1",
                      (pipeline_id, job_id)).fetchone():
            return {"eligible": False, "reason": "Answer the pending job question before refreshing its profile evaluation."}

        current_jd = row["jd_version"]
        current_profile = self.profile_version(db)
        if db.execute("SELECT 1 FROM agent_workers WHERE job_id=? "
                      "AND (state='running' OR turn_active=1) LIMIT 1",
                      (job_id,)).fetchone():
            return {"eligible": False, "reason": "A worker is still active for this job."}
        if db.execute("SELECT 1 FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                      "WHERE p.id=? AND w.role='discovery' AND (w.state='running' OR w.turn_active=1) LIMIT 1",
                      (pipeline_id,)).fetchone():
            return {"eligible": False, "reason": "Wait for this pipeline's active discovery turn to finish."}

        for worker in db.execute("SELECT id,role,state,jd_version,profile_version,checkpoint,context "
                                 "FROM agent_workers WHERE job_id=? AND role='submission'",
                                 (job_id,)).fetchall():
            context = self._agent_decode(worker["context"], {})
            if self._context_may_have_sent_final(context, worker["checkpoint"]):
                return {"eligible": False, "reason": "A final application action may have been sent; reconcile it first."}
        current_evaluator = db.execute(
            "SELECT id,state FROM agent_workers WHERE pipeline_id=? AND job_id=? AND role='evaluator' "
            "AND jd_version=? AND profile_version=? AND state!='superseded' ORDER BY created_at DESC,id DESC LIMIT 1",
            (pipeline_id, job_id, current_jd, current_profile)).fetchone()
        if current_evaluator:
            return {"eligible": False, "reason": "An evaluator already exists for the current job and profile versions.",
                    "already_current": True, "evaluator_worker_id": current_evaluator["id"]}
        return {"eligible": True, "reason": "A fresh evaluation can be queued for the current applicant profile.",
                "already_current": False, "evaluator_worker_id": None}

    def agent_profile_refresh_eligibility(self, pipeline_id, job_id):
        with self.connect() as db:
            return self._agent_profile_refresh_assessment(db, pipeline_id, job_id)

    def refresh_agent_job_profile(self, pipeline_id, job_id, provenance):
        """Invalidate stale unsent work for one selected job and queue one evaluator."""
        if not isinstance(provenance, str) or not provenance.strip() or len(provenance.strip()) > 1000:
            raise ValueError("A reason of 1–1000 characters is required for a profile refresh.")
        with self.connect(write=True) as db:
            assessment = self._agent_profile_refresh_assessment(db, pipeline_id, job_id)
            if assessment.get("already_current"):
                return {"pipeline_id": pipeline_id, "job_id": job_id,
                        "profile_version": self.profile_version(db),
                        "evaluator_worker_id": assessment["evaluator_worker_id"],
                        "created": False, "already_current": True,
                        "stale_inbox_ids": [], "superseded_worker_ids": [],
                        "status": "already_current"}
            if not assessment["eligible"]:
                raise Conflict(assessment["reason"])

            now = timestamp()
            current_profile = self.profile_version(db)
            current_jd = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()["jd_version"]
            stale_inbox_ids = []
            inbox_rows = db.execute(
                "SELECT i.*,w.state AS worker_state,w.role,w.jd_version AS worker_jd_version,"
                "w.profile_version AS worker_profile_version,w.context AS worker_context "
                "FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id "
                "WHERE i.pipeline_id=? AND i.job_id=? AND i.state IN ('pending','stale')",
                (pipeline_id, job_id)).fetchall()
            for inbox in inbox_rows:
                if inbox["state"] == "stale":
                    stale_inbox_ids.append(inbox["id"])
                    continue
                if inbox["kind"] == "question":
                    # An unanswered question is preserved as historical context; a profile
                    # refresh never claims that applicant supplied an answer.
                    continue
                context = self._agent_decode(inbox["context"], {})
                if (context.get("jd_version") == current_jd
                        and context.get("profile_version") == current_profile
                        and inbox["worker_jd_version"] == current_jd
                        and inbox["worker_profile_version"] == current_profile):
                    continue
                stale_reason = "Invalidated because applicant profile version " \
                    f"{current_profile} was imported before this approval was resolved."
                context["stale_reason"] = stale_reason
                db.execute("UPDATE agent_inbox SET state='stale',context=?,response='',provenance=?,"
                           "updated_at=?,resolved_at=? WHERE id=? AND state='pending'",
                           (json.dumps(context, ensure_ascii=False), "system: " + stale_reason,
                            now, now, inbox["id"]))
                stale_inbox_ids.append(inbox["id"])
                self.event(db, "agent.inbox_staled", {"pipeline_id": pipeline_id,
                           "worker_id": inbox["worker_id"], "item_id": inbox["id"],
                           "reason": stale_reason, "profile_version": current_profile}, job_id,
                           "profile_refresh")

            superseded_worker_ids = []
            workers = db.execute(
                "SELECT * FROM agent_workers WHERE pipeline_id=? AND job_id=? "
                "AND role IN ('evaluator','submission') "
                "AND state NOT IN ('superseded','uncertain','completed','discarded')",
                (pipeline_id, job_id)).fetchall()
            for worker in workers:
                if worker["state"] == "running" or worker["turn_active"]:
                    # Recheck within the same write transaction even though the
                    # assessment above already rejected this race.
                    raise Conflict("A worker became active; no profile refresh was applied.")
                context = self._agent_decode(worker["context"], {})
                if (worker["role"] == "submission"
                        and self._context_may_have_sent_final(context, worker["checkpoint"])):
                    raise Conflict("A final application action may have been sent; reconcile it first.")
                audit = {"reason": "profile_refreshed_before_unsent_work",
                         "superseded_at": now, "provenance": provenance.strip(),
                         "role": worker["role"],
                         "saved_snapshot": {"jd_version": worker["jd_version"],
                                            "profile_version": worker["profile_version"]},
                         "current_snapshot": {"jd_version": current_jd,
                                              "profile_version": current_profile}}
                context["profile_refresh_supersession"] = audit
                message = (f"Superseded after profile version {current_profile} was imported; "
                           "this stale task will not resume.")
                db.execute("UPDATE agent_workers SET state='superseded',context=?,error=?,turn_active=0,"
                           "completed_at=COALESCE(completed_at,?),updated_at=?,capability_sha256='',"
                           "capability_expires_at=NULL WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), message, now, now, worker["id"]))
                superseded_worker_ids.append(worker["id"])
                self.event(db, "agent.worker_superseded_by_profile_refresh",
                           {"pipeline_id": pipeline_id, "worker_id": worker["id"],
                            "saved_snapshot": audit["saved_snapshot"],
                            "current_snapshot": audit["current_snapshot"],
                            "provenance": provenance.strip()}, job_id, "profile_refresh")

            db.execute("UPDATE pipeline_jobs SET agent_decision='',agent_reason='',agent_status='selected' "
                       "WHERE pipeline_id=? AND job_id=? AND selected=1", (pipeline_id, job_id))
            evaluator_worker_id = self._agent_insert_worker(
                db, pipeline_id, "evaluator", job_id=job_id,
                context={"profile_refresh": {"provenance": provenance.strip(),
                                              "superseded_worker_ids": superseded_worker_ids,
                                              "stale_inbox_ids": stale_inbox_ids}})
            self.event(db, "agent.profile_refresh_requested",
                       {"pipeline_id": pipeline_id, "job_id": job_id,
                        "profile_version": current_profile, "jd_version": current_jd,
                        "evaluator_worker_id": evaluator_worker_id,
                        "superseded_worker_ids": superseded_worker_ids,
                        "stale_inbox_ids": stale_inbox_ids,
                        "provenance": provenance.strip()}, job_id, "profile_refresh")
            self._agent_refresh_pipeline(db, pipeline_id)
            return {"pipeline_id": pipeline_id, "job_id": job_id,
                    "profile_version": current_profile, "evaluator_worker_id": evaluator_worker_id,
                    "created": True, "already_current": False,
                    "stale_inbox_ids": stale_inbox_ids,
                    "superseded_worker_ids": superseded_worker_ids,
                    "status": "queued"}

    def pipeline_submission_jobs(self, pipeline_id, excluded=()):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM pipelines WHERE id=?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            exclusion = " AND j.id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
            rows = db.execute("SELECT j.id FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id "
                              "WHERE pj.pipeline_id=? AND pj.selected=1 AND pj.manual_discard_reason='' "
                              "AND j.phase='ready_for_submission' "
                              "AND j.run_status IN ('idle','awaiting_user')" + exclusion +
                              " ORDER BY j.first_seen_at,j.id", (pipeline_id, *excluded)).fetchall()
            return [row[0] for row in rows]

    def record_application_outcome(self, job_id, status, provenance, actor="user"):
        if status not in {"pending", "rejected", "accepted", "unknown"} or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Choose pending, rejected, accepted or unknown and include outcome provenance.")
        with self.connect(write=True) as db:
            job = db.execute("SELECT phase FROM jobs WHERE id=?", (job_id,)).fetchone()
            evidence = db.execute("SELECT 1 FROM evidence WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
            if not job:
                raise KeyError(job_id)
            verified_agent_submission = False
            if job["phase"] == "submitted" and not evidence:
                submissions = db.execute("SELECT worker_id,event_ids FROM agent_submission_results "
                                         "WHERE job_id=? AND state='submitted' ORDER BY created_at DESC",
                                         (job_id,)).fetchall()
                for submission in submissions:
                    saved_ids = set(self._agent_decode(submission["event_ids"], []))
                    if saved_ids.intersection(self._agent_event_results(db, submission["worker_id"])):
                        verified_agent_submission = True
                        break
            if job["phase"] != "submitted" or not (evidence or verified_agent_submission):
                raise Conflict("Recruiter outcomes can only be recorded for a verified submitted application.")
            now = timestamp()
            db.execute("INSERT INTO application_outcomes VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                       "status=excluded.status,provenance=excluded.provenance,actor=excluded.actor,updated_at=excluded.updated_at",
                       (job_id, status, provenance.strip(), actor, now))
            self.event(db, "application.outcome_updated", {"status": status, "provenance": provenance.strip()},
                       job_id, actor)
        return {"job_id": job_id, "application_status": status, "provenance": provenance.strip(),
                "updated_at": now, "actor": actor}

    def _current_upload_package(self, db, job_id, jd_version, profile_version, package_id=None):
        sql = "SELECT id,manifest FROM packages WHERE job_id=? AND jd_version=? AND profile_version=?"
        args = [job_id, jd_version, profile_version]
        if package_id:
            sql += " AND id=?"
            args.append(package_id)
        sql += " ORDER BY created_at DESC,rowid DESC LIMIT 1"
        row = db.execute(sql, args).fetchone()
        if not row:
            return None
        manifest = json.loads(row["manifest"])
        if not manifest.get("upload_ready"):
            return None
        hashes = {item.get("sha256") for item in manifest.get("pdf_files", []) if item.get("sha256")}
        hashes.add(digest(json.dumps({"files": manifest.get("files", []), "pdf_files": manifest.get("pdf_files", [])},
                                    sort_keys=True, ensure_ascii=False)))
        return {"id": row["id"], "manifest": manifest, "hashes": hashes}

    def _package_snapshot_hash(self, db, job_id, package_id, jd_version, profile_version):
        package = self._current_upload_package(db, job_id, jd_version, profile_version, package_id)
        if not package:
            return None
        manifest = package["manifest"]
        return digest(json.dumps({"files": manifest.get("files", []), "pdf_files": manifest.get("pdf_files", [])},
                                sort_keys=True, ensure_ascii=False))

    def save_submission_context(self, job_id, pipeline_id, claim, *, session_id=None, tab_id=None,
                                checkpoint, package_id, package_sha256, context=None):
        if not isinstance(checkpoint, str) or not checkpoint.strip() or not re.fullmatch(r"[a-fA-F0-9]{64}", package_sha256 or ""):
            raise ValueError("A checkpoint and package SHA-256 are required.")
        with self.connect(write=True) as db:
            row = self.require_claim(db, claim, "submission")
            if row["id"] != job_id or claim.get("pipeline_id") != pipeline_id:
                raise Conflict("Submission context does not match the current job and pipeline claim.")
            package = self._current_upload_package(db, job_id, claim["jd_version"], claim["profile_version"], package_id)
            if not package or package_sha256 not in package["hashes"]:
                raise Conflict("Submission context package is stale or does not match the reviewed package.")
            now = timestamp()
            db.execute("INSERT INTO submission_contexts VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(job_id,pipeline_id) DO UPDATE SET session_id=excluded.session_id,tab_id=excluded.tab_id,"
                       "checkpoint=excluded.checkpoint,package_id=excluded.package_id,jd_version=excluded.jd_version,"
                       "profile_version=excluded.profile_version,package_sha256=excluded.package_sha256,"
                       "context=excluded.context,updated_at=excluded.updated_at",
                       (job_id, pipeline_id, session_id, tab_id, checkpoint.strip(), package_id, claim["jd_version"],
                        claim["profile_version"], package_sha256.lower(), json.dumps(context or {}, ensure_ascii=False), now))
            self.event(db, "submission.checkpoint_saved", {"pipeline_id": pipeline_id, "checkpoint": checkpoint.strip()}, job_id)
        return self.get_submission_context(job_id, pipeline_id)

    def get_submission_context(self, job_id, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM submission_contexts WHERE job_id=? AND pipeline_id=?",
                             (job_id, pipeline_id)).fetchone()
            if not row:
                return None
            snapshot = self._package_snapshot_hash(db, job_id, row["package_id"], row["jd_version"], row["profile_version"])
            current = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (not snapshot or row["package_sha256"] not in self._current_upload_package(
                    db, job_id, row["jd_version"], row["profile_version"], row["package_id"])["hashes"]
                    or not current or current["jd_version"] != row["jd_version"]
                    or self.profile_version(db) != row["profile_version"]):
                db.execute("DELETE FROM submission_contexts WHERE job_id=? AND pipeline_id=?", (job_id, pipeline_id))
                self.event(db, "submission.stale_context_cleared", {"pipeline_id": pipeline_id}, job_id)
                return None
            result = dict(row)
            result["context"] = json.loads(result["context"])
            return result

    def clear_submission_context(self, job_id, pipeline_id):
        with self.connect(write=True) as db:
            db.execute("DELETE FROM submission_contexts WHERE job_id=? AND pipeline_id=?", (job_id, pipeline_id))
            self.event(db, "submission.context_cleared", {"pipeline_id": pipeline_id}, job_id)

    def request_approval(self, claim, action, package_id, terms_hash, terms_text):
        if not isinstance(action, str) or not action.strip() or not isinstance(terms_text, str) or not terms_text.strip():
            raise ValueError("An action name and exact user-visible terms snapshot are required.")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", terms_hash or "") or digest(terms_text) != terms_hash.lower():
            raise ValueError("The terms hash must match the exact terms snapshot text.")
        with self.connect(write=True) as db:
            job = self.require_claim(db, claim, "submission")
            pipeline_id = claim.get("pipeline_id")
            if not pipeline_id:
                raise Conflict("Action approvals are only available for a pipeline submission.")
            package = self._current_upload_package(db, job["id"], claim["jd_version"], claim["profile_version"], package_id)
            if not package:
                raise Conflict("Action approval requires the current reviewed package.")
            old = db.execute("SELECT * FROM submission_approvals WHERE job_id=? AND pipeline_id=? AND action=? "
                             "AND jd_version=? AND profile_version=? AND package_id=? AND terms_hash=? "
                             "AND status IN ('pending','approved') ORDER BY created_at DESC LIMIT 1",
                             (job["id"], pipeline_id, action, claim["jd_version"], claim["profile_version"],
                              package_id, terms_hash.lower())).fetchone()
            if old:
                return dict(old)
            now, approval_id = timestamp(), uuid.uuid4().hex
            db.execute("INSERT INTO submission_approvals(id,job_id,pipeline_id,action,jd_version,profile_version,package_id,"
                       "terms_hash,terms_text,status,provenance,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'pending','','',?,?)",
                       (approval_id, job["id"], pipeline_id, action.strip(), claim["jd_version"], claim["profile_version"],
                        package_id, terms_hash.lower(), terms_text, now, now))
            self.event(db, "submission.action_requested", {"pipeline_id": pipeline_id, "approval_id": approval_id,
                       "action": action.strip(), "package_id": package_id, "terms_hash": terms_hash.lower()}, job["id"])
            return dict(db.execute("SELECT * FROM submission_approvals WHERE id=?", (approval_id,)).fetchone())

    def get_approval(self, job_id, pipeline_id, action, package_id, jd_version, profile_version, terms_hash):
        with self.connect() as db:
            row = db.execute("SELECT * FROM submission_approvals WHERE job_id=? AND pipeline_id=? AND action=? "
                             "AND package_id=? AND jd_version=? AND profile_version=? AND terms_hash=? ORDER BY created_at DESC LIMIT 1",
                             (job_id, pipeline_id, action, package_id, jd_version, profile_version, terms_hash.lower())).fetchone()
            if not row:
                return None
            package = self._current_upload_package(db, job_id, jd_version, profile_version, package_id)
            current = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not package or not current or current["jd_version"] != jd_version or self.profile_version(db) != profile_version:
                return None
            return dict(row)

    def list_approvals(self, pipeline_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM pipelines WHERE id=?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            rows = db.execute("SELECT a.*,j.title AS job_title,j.url,j.phase AS current_phase,"
                              "j.jd_version AS current_jd_version,pj.selected AS selected "
                              "FROM submission_approvals a JOIN jobs j ON j.id=a.job_id "
                              "JOIN pipeline_jobs pj ON pj.pipeline_id=a.pipeline_id AND pj.job_id=a.job_id "
                              "WHERE a.pipeline_id=? ORDER BY a.created_at,a.id",
                              (pipeline_id,)).fetchall()
            result = []
            current_profile = self.profile_version(db)
            for row in rows:
                item = dict(row)
                reason = ""
                if row["status"] != "pending":
                    reason = f"This action is already {row['status']}."
                elif not row["selected"]:
                    reason = "This job is no longer selected in the pipeline."
                elif row["jd_version"] != row["current_jd_version"] or row["profile_version"] != current_profile:
                    reason = "The job or applicant profile changed after this action was requested."
                elif row["current_phase"] != "ready_for_submission":
                    reason = "This job is no longer ready for submission."
                elif not self._current_upload_package(db, row["job_id"], row["current_jd_version"],
                                                       current_profile, row["package_id"]):
                    reason = "The reviewed upload package is no longer current."
                item["is_current"] = not reason
                item["stale_reason"] = reason
                result.append(item)
            return {"pipeline_id": pipeline_id, "approvals": result}

    def resolve_approval(self, approval_id, approved, provenance, actor="user"):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Approval or rejection needs provenance.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT a.*,j.jd_version AS current_jd_version,j.phase AS current_phase "
                             "FROM submission_approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
                             (approval_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise Conflict("This action approval is no longer pending.")
            current_profile = self.profile_version(db)
            package = self._current_upload_package(db, row["job_id"], row["current_jd_version"], current_profile, row["package_id"])
            if (not package or row["jd_version"] != row["current_jd_version"]
                    or row["profile_version"] != current_profile or row["current_phase"] != "ready_for_submission"):
                raise Conflict("This action approval is stale; current package or applicant facts changed.")
            status, now = "approved" if approved else "rejected", timestamp()
            db.execute("UPDATE submission_approvals SET status=?,provenance=?,actor=?,updated_at=? WHERE id=?",
                       (status, provenance.strip(), actor, now, approval_id))
            if not approved:
                db.execute("UPDATE jobs SET run_status='submission_blocked',reason=? WHERE id=? AND run_status='awaiting_user'",
                           ("User declined the requested submission action. " + provenance.strip(), row["job_id"]))
            self.event(db, "submission.action_resolved", {"approval_id": approval_id, "pipeline_id": row["pipeline_id"],
                       "action": row["action"], "status": status, "provenance": provenance.strip()}, row["job_id"], actor)
            return dict(db.execute("SELECT * FROM submission_approvals WHERE id=?", (approval_id,)).fetchone())

    def consume_approval(self, claim, approval_id):
        with self.connect(write=True) as db:
            job = self.require_claim(db, claim, "submission")
            row = db.execute("SELECT * FROM submission_approvals WHERE id=?", (approval_id,)).fetchone()
            if (not row or row["job_id"] != job["id"] or row["pipeline_id"] != claim.get("pipeline_id")
                    or row["jd_version"] != claim["jd_version"] or row["profile_version"] != claim["profile_version"]
                    or row["status"] != "approved"):
                raise Conflict("This action approval is missing, stale or already consumed.")
            package = self._current_upload_package(db, job["id"], claim["jd_version"], claim["profile_version"], row["package_id"])
            if not package:
                raise Conflict("This action approval no longer matches the current reviewed package.")
            now = timestamp()
            db.execute("UPDATE submission_approvals SET status='consumed',consumed_at=?,updated_at=? WHERE id=? AND status='approved'",
                       (now, now, approval_id))
            self.event(db, "submission.action_consumed", {"approval_id": approval_id, "pipeline_id": row["pipeline_id"],
                       "action": row["action"], "terms_hash": row["terms_hash"]}, job["id"], "submission_worker")
            return dict(db.execute("SELECT * FROM submission_approvals WHERE id=?", (approval_id,)).fetchone())

    def request_submission_answer(self, claim, prompt):
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("The exact required application question is needed.")
        with self.connect(write=True) as db:
            job = self.require_claim(db, claim, "submission")
            pipeline_id = claim.get("pipeline_id")
            if not pipeline_id:
                raise Conflict("Application answers are only available for a pipeline submission.")
            prompt_hash, now = digest(prompt), timestamp()
            db.execute("INSERT OR IGNORE INTO submission_questions(id,job_id,pipeline_id,prompt,prompt_hash,status,created_at) "
                       "VALUES(?,?,?,?,?,'pending',?)", (uuid.uuid4().hex, job["id"], pipeline_id, prompt, prompt_hash, now))
            row = db.execute("SELECT * FROM submission_questions WHERE job_id=? AND pipeline_id=? AND prompt_hash=?",
                             (job["id"], pipeline_id, prompt_hash)).fetchone()
            self.event(db, "submission.question_requested", {"pipeline_id": pipeline_id, "question_id": row["id"]}, job["id"])
            return dict(row)

    def get_submission_answer(self, job_id, pipeline_id, prompt):
        with self.connect() as db:
            row = db.execute("SELECT * FROM submission_questions WHERE job_id=? AND pipeline_id=? AND prompt_hash=? AND status='answered'",
                             (job_id, pipeline_id, digest(str(prompt or "").strip()))).fetchone()
            return dict(row) if row else None

    def list_submission_questions(self, pipeline_id):
        with self.connect() as db:
            rows = db.execute("SELECT q.*,j.title AS job_title,j.url FROM submission_questions q JOIN jobs j ON j.id=q.job_id "
                              "WHERE q.pipeline_id=? ORDER BY q.created_at,q.id", (pipeline_id,)).fetchall()
            return {"pipeline_id": pipeline_id, "questions": [dict(row) for row in rows]}

    def list_pipeline_questions(self, pipeline_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM pipelines WHERE id=?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            application = [dict(row) for row in db.execute(
                "SELECT q.*,j.title AS job_title,j.url,j.phase AS current_phase,j.jd_version AS current_jd_version,"
                "pj.selected AS selected,c.name AS company_name FROM submission_questions q "
                "JOIN jobs j ON j.id=q.job_id JOIN companies c ON c.id=j.company_id "
                "JOIN pipeline_jobs pj ON pj.pipeline_id=q.pipeline_id AND pj.job_id=q.job_id "
                "WHERE q.pipeline_id=? ORDER BY q.created_at,q.id", (pipeline_id,))]
            for row in application:
                row["kind"] = "application"
                reason = ""
                if row["status"] != "pending":
                    reason = f"This application question is already {row['status']}."
                elif not row["selected"]:
                    reason = "This job is no longer selected in the pipeline."
                elif row["current_phase"] != "ready_for_submission":
                    reason = "This job is no longer ready for submission."
                elif not self._current_upload_package(db, row["job_id"], row["current_jd_version"],
                                                       self.profile_version(db)):
                    reason = "The reviewed upload package is no longer current."
                row["is_current"] = not reason
                row["stale_reason"] = reason
            listing_rows = [dict(row) for row in db.execute(
                "SELECT q.*,j.title AS job_title,j.url,j.jd_version AS current_jd_version,"
                "j.phase AS current_phase,j.run_status AS current_run_status,pj.selected AS selected,c.name AS company_name "
                "FROM listing_fact_questions q JOIN jobs j ON j.id=q.job_id JOIN companies c ON c.id=j.company_id "
                "JOIN pipeline_jobs pj ON pj.pipeline_id=q.pipeline_id AND pj.job_id=q.job_id "
                "WHERE q.pipeline_id=? ORDER BY q.created_at,q.id", (pipeline_id,))]
            for row in listing_rows:
                row["kind"] = "listing_fact"
                row["allowed_values"] = list(LISTING_FACT_VALUES[row["field"]])
                reason = ""
                if row["status"] != "pending":
                    reason = f"This listing question is already {row['status']}."
                elif not row["selected"]:
                    reason = "This job is no longer selected in the pipeline."
                elif row["jd_version"] != row["current_jd_version"]:
                    reason = "The public listing changed after this question was created."
                elif row["current_phase"] != "new" or row["current_run_status"] != "awaiting_information":
                    reason = "This job no longer needs listing verification."
                row["is_current"] = not reason
                row["stale_reason"] = reason
            questions = application + listing_rows
            questions.sort(key=lambda row: (row.get("created_at", ""), row.get("id", "")))
            return {"pipeline_id": pipeline_id, "questions": questions}

    def answer_listing_fact_question(self, question_id, value, evidence, provenance, actor="user"):
        if not isinstance(value, str) or not isinstance(evidence, str) or not evidence.strip() \
                or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("A listing fact, exact public-description excerpt and provenance are required.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT q.*,j.jd_version AS current_jd_version,j.location,j.phase AS current_phase,"
                             "j.run_status AS current_run_status,pj.selected AS selected FROM listing_fact_questions q "
                             "JOIN jobs j ON j.id=q.job_id JOIN pipeline_jobs pj ON pj.pipeline_id=q.pipeline_id AND pj.job_id=q.job_id "
                             "WHERE q.id=?", (question_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise Conflict("This listing verification question is no longer pending.")
            if row["jd_version"] != row["current_jd_version"]:
                raise Conflict("The public listing changed; verify its current version instead.")
            if not row["selected"] or row["current_phase"] != "new" or row["current_run_status"] != "awaiting_information":
                raise Conflict("This job no longer needs listing verification in this pipeline.")
            if value not in LISTING_FACT_VALUES.get(row["field"], ()):
                raise ValueError("Choose a supported value for this listing field.")
            jd_row = db.execute("SELECT jd FROM job_versions WHERE job_id=? AND version=?",
                                (row["job_id"], row["jd_version"])).fetchone()
            if not jd_row or evidence.strip() not in jd_row["jd"] + "\n" + row["location"]:
                raise ValueError("The evidence must quote the current job description or location exactly.")
            now = timestamp()
            db.execute("UPDATE listing_fact_questions SET status='answered',answer_value=?,answer_evidence=?,"
                       "provenance=?,actor=?,resolved_at=? WHERE id=? AND status='pending'",
                       (value, evidence.strip(), provenance.strip(), actor, now, question_id))
            pending_tech = db.execute("SELECT 1 FROM questions q JOIN question_jobs qj ON qj.question_id=q.id "
                                      "WHERE qj.job_id=? AND q.status='pending' LIMIT 1", (row["job_id"],)).fetchone()
            pending_listing = db.execute("SELECT 1 FROM listing_fact_questions WHERE job_id=? AND pipeline_id=? "
                                         "AND jd_version=? AND status='pending' LIMIT 1",
                                         (row["job_id"], row["pipeline_id"], row["jd_version"])).fetchone()
            if not pending_tech and not pending_listing:
                db.execute("UPDATE jobs SET run_status='idle',reason='Public listing facts were verified; reassessment queued.' "
                           "WHERE id=? AND phase='new' AND run_status='awaiting_information'", (row["job_id"],))
            self.event(db, "listing_fact.verified", {"question_id": question_id, "pipeline_id": row["pipeline_id"],
                       "field": row["field"], "value": value, "evidence": evidence.strip(),
                       "provenance": provenance.strip()}, row["job_id"], actor)
            result = dict(db.execute("SELECT * FROM listing_fact_questions WHERE id=?", (question_id,)).fetchone())
            result["kind"] = "listing_fact"
            return result

    def answer_submission_question(self, question_id, answer, provenance, actor="user"):
        if not isinstance(answer, str) or not answer.strip() or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("A user answer and its provenance are required.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT q.*,j.phase AS current_phase,j.jd_version AS current_jd_version,"
                             "pj.selected AS selected FROM submission_questions q JOIN jobs j ON j.id=q.job_id "
                             "JOIN pipeline_jobs pj ON pj.pipeline_id=q.pipeline_id AND pj.job_id=q.job_id "
                             "WHERE q.id=?", (question_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise Conflict("This application question is no longer pending.")
            current_profile = self.profile_version(db)
            if (not row["selected"] or row["current_phase"] != "ready_for_submission"
                    or not self._current_upload_package(db, row["job_id"], row["current_jd_version"], current_profile)):
                raise Conflict("This application question is stale; the selected job or reviewed package changed.")
            now = timestamp()
            db.execute("UPDATE submission_questions SET status='answered',answer=?,provenance=?,actor=?,resolved_at=? WHERE id=?",
                       (answer.strip(), provenance.strip(), actor, now, question_id))
            self.event(db, "submission.question_answered", {"question_id": question_id, "provenance": provenance.strip()}, row["job_id"], actor)
            return dict(db.execute("SELECT * FROM submission_questions WHERE id=?", (question_id,)).fetchone())

    def finish_submission(self, claim, status, reason):
        valid = {"submitted", "awaiting_user", "uncertain", "blocked", "failed"}
        if status not in valid or not isinstance(reason, str) or not reason.strip():
            raise ValueError("A supported submission result and reason are required.")
        with self.connect(write=True) as db:
            job = self.require_claim(db, claim, "submission")
            pipeline_id = claim.get("pipeline_id")
            if status == "submitted":
                evidence = db.execute("SELECT 1 FROM evidence WHERE job_id=? LIMIT 1", (job["id"],)).fetchone()
                if job["phase"] != "submitted" or not evidence:
                    raise Conflict("A submitted result requires verified application evidence first.")
                outcome = db.execute("SELECT 1 FROM application_outcomes WHERE job_id=?", (job["id"],)).fetchone()
                if not outcome:
                    db.execute("INSERT INTO application_outcomes VALUES(?,?,?,?,?)",
                               (job["id"], "pending", "Verified submission; recruiter outcome not yet recorded.", "submission_worker", timestamp()))
                db.execute("DELETE FROM submission_contexts WHERE job_id=? AND pipeline_id=?", (job["id"], pipeline_id))
                run_status, phase = "idle", "submitted"
            elif status == "awaiting_user":
                run_status, phase = "awaiting_user", job["phase"]
            elif status == "uncertain":
                run_status, phase = "submission_uncertain", job["phase"]
            elif status == "blocked":
                run_status, phase = "submission_blocked", job["phase"]
            else:
                run_status, phase = "failed", job["phase"]
            db.execute("UPDATE jobs SET phase=?,run_status=?,reason=?,active_stage=NULL,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                       (phase, run_status, reason.strip(), job["id"]))
            self.event(db, "submission.finished", {"pipeline_id": pipeline_id, "status": status, "reason": reason.strip()}, job["id"])
        return {"status": status, "job_id": claim["job_id"], "reason": reason.strip()}

    def add_pipeline_job(self, pipeline_id, job_id, *, selected=True, excluded_reason=""):
        with self.connect(write=True) as db:
            pipeline = db.execute("SELECT company_id FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            job = db.execute("SELECT company_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not pipeline or not job:
                raise KeyError(pipeline_id if not pipeline else job_id)
            if pipeline["company_id"] != job["company_id"]:
                raise Conflict("A pipeline can only include jobs from its selected company.")
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,excluded_reason) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(pipeline_id,job_id) DO UPDATE SET selected=excluded.selected,excluded_reason=excluded.excluded_reason",
                       (pipeline_id, job_id, timestamp(), int(bool(selected)), excluded_reason.strip()))

    def update_pipeline(self, pipeline_id, *, status=None, stage=None, discovery_status=None,
                        discovery=None, blockers=None, stop_requested=None, finished=False):
        assignments, values = [], []
        for key, value in (("status", status), ("stage", stage), ("discovery_status", discovery_status)):
            if value is not None:
                assignments.append(f"{key}=?")
                values.append(value)
        if discovery is not None:
            payload = dict(discovery)
            if discovery_status is not None:
                payload["status"] = discovery_status
            assignments.append("discovery_payload=?")
            values.append(json.dumps(payload, ensure_ascii=False))
        elif discovery_status is not None:
            with self.connect() as db:
                row = db.execute("SELECT discovery_payload FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            payload = json.loads(row[0]) if row else {}
            payload = payload if isinstance(payload, dict) else {}
            payload["status"] = discovery_status
            if discovery_status in {"pending", "running"}:
                payload["detail"] = ""
            elif blockers:
                payload["detail"] = list(blockers)[0]
            assignments.append("discovery_payload=?")
            values.append(json.dumps(payload, ensure_ascii=False))
        if blockers is not None:
            assignments.append("blockers=?")
            values.append(json.dumps(list(blockers), ensure_ascii=False))
        if stop_requested is not None:
            assignments.append("stop_requested=?")
            values.append(int(bool(stop_requested)))
        if status == "running":
            assignments.append("started_at=COALESCE(started_at,?)")
            values.append(timestamp())
        assignments.append("updated_at=?")
        values.append(timestamp())
        if finished:
            assignments.append("finished_at=?")
            values.append(timestamp())
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM pipelines WHERE id=?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            db.execute(f"UPDATE pipelines SET {','.join(assignments)} WHERE id=?", (*values, pipeline_id))
            if status or stage or discovery_status or blockers is not None:
                self.event(db, "pipeline.updated", {"pipeline_id": pipeline_id, "status": status,
                           "stage": stage, "discovery_status": discovery_status, "blockers": blockers})
        return self.get_pipeline(pipeline_id)

    def finalize_pipeline(self, pipeline_id, *, status, stage, blockers, discovery_status=None,
                          discovery=None, respect_stop=False):
        """Write a terminal state without losing a concurrently requested stop."""
        if status not in {"completed", "blocked", "stopped"}:
            raise ValueError("A terminal pipeline status is required.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT stop_requested FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if respect_stop and row["stop_requested"]:
                status, stage = "stopped", "stopped"
                blockers = ["Stop was requested while the current task was ending; no further tasks were scheduled."]
                discovery_status = discovery = None
                stop_requested = 1
            else:
                stop_requested = int(status == "stopped")
            assignments = ["status=?", "stage=?", "blockers=?", "stop_requested=?", "updated_at=?", "finished_at=?"]
            values = [status, stage, json.dumps(list(blockers), ensure_ascii=False), stop_requested, timestamp(), timestamp()]
            if discovery_status is not None:
                assignments.append("discovery_status=?")
                values.append(discovery_status)
            if discovery is not None:
                assignments.append("discovery_payload=?")
                values.append(json.dumps(discovery, ensure_ascii=False))
            db.execute(f"UPDATE pipelines SET {','.join(assignments)} WHERE id=?", (*values, pipeline_id))
            self.event(db, "pipeline.finalized", {"pipeline_id": pipeline_id, "status": status, "stage": stage,
                       "blockers": blockers})
        return self.get_pipeline(pipeline_id)

    def pipeline_stop_requested(self, pipeline_id):
        with self.connect() as db:
            row = db.execute("SELECT stop_requested FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            return bool(row[0])

    def request_pipeline_stop(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["status"] == "completed":
                raise Conflict("A completed pipeline cannot be stopped.")
            status = "stopping" if row["status"] in {"running", "stopping"} else "stopped"
            now = timestamp()
            db.execute("UPDATE pipelines SET stop_requested=1,status=?,updated_at=?,finished_at=? WHERE id=?",
                       (status, now, None if status == "stopping" else now, pipeline_id))
            self.event(db, "pipeline.stop_requested", {"pipeline_id": pipeline_id, "status": status})
        return self.get_pipeline(pipeline_id)

    def resume_pipeline(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT p.*,c.enabled FROM pipelines p JOIN companies c ON c.id=p.company_id WHERE p.id=?",
                             (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["status"] == "completed":
                raise Conflict("A completed pipeline cannot be resumed.")
            if row["status"] == "running":
                raise Conflict("This pipeline is already running.")
            db.execute("UPDATE pipelines SET dashboard_hidden=0 WHERE id=?", (pipeline_id,))
            peer = db.execute("SELECT id FROM pipelines WHERE company_id=? AND id!=? AND status IN ('queued','running','stopping','awaiting_user') LIMIT 1",
                              (row["company_id"], pipeline_id)).fetchone()
            if peer:
                raise Conflict("This company already has another active pipeline.")
            if row["company_id"] == "tesla" or not row["enabled"]:
                blockers = ["Tesla is excluded from the pilot."] if row["company_id"] == "tesla" else ["Enable this company before resuming."]
                db.execute("UPDATE pipelines SET status='blocked',blockers=?,stop_requested=0,updated_at=?,finished_at=NULL WHERE id=?",
                           (json.dumps(blockers), timestamp(), pipeline_id))
            else:
                source = db.execute("SELECT 1 FROM company_sources WHERE company_id=?", (row["company_id"],)).fetchone()
                if not source:
                    blockers = ["No supported public job-board feed is configured. Configure a verified Greenhouse or Lever board source."]
                    db.execute("UPDATE pipelines SET status='blocked',discovery_status='blocked',blockers=?,stop_requested=0,updated_at=?,finished_at=NULL WHERE id=?",
                               (json.dumps(blockers), timestamp(), pipeline_id))
                else:
                    discovery_status = row["discovery_status"]
                    if discovery_status not in {"complete"}:
                        discovery_status = "pending"
                    db.execute("UPDATE pipelines SET status='queued',stage=?,discovery_status=?,blockers='[]',stop_requested=0,finished_at=NULL,updated_at=? WHERE id=?",
                               ("processing" if discovery_status == "complete" else "discovery", discovery_status, timestamp(), pipeline_id))
                    self.event(db, "pipeline.resumed", {"pipeline_id": pipeline_id})
        return self.get_pipeline(pipeline_id)

    def start_pipeline(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,stop_requested,discovery_status FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["status"] == "queued" and not row["stop_requested"]:
                stage = "processing" if row["discovery_status"] == "complete" else "discovery"
                now = timestamp()
                db.execute("UPDATE pipelines SET status='running',stage=?,started_at=COALESCE(started_at,?),"
                           "blockers='[]',updated_at=? WHERE id=? AND status='queued' AND stop_requested=0",
                           (stage, now, now, pipeline_id))
                self.event(db, "pipeline.started", {"pipeline_id": pipeline_id, "stage": stage})
        return self.get_pipeline(pipeline_id)

    def set_pipeline_progress(self, pipeline_id, stage, *, discovery_status=None, discovery=None, blockers=None):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,stop_requested,discovery_payload FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["stop_requested"] or row["status"] in {"stopping", "stopped", "completed"}:
                return self.get_pipeline(pipeline_id)
            now = timestamp()
            assignments = ["status='running'", "stage=?", "updated_at=?", "started_at=COALESCE(started_at,?)"]
            values = [stage, now, now]
            if discovery_status is not None:
                assignments.append("discovery_status=?")
                values.append(discovery_status)
            if discovery is not None:
                payload = dict(discovery)
                if discovery_status is not None:
                    payload["status"] = discovery_status
                assignments.append("discovery_payload=?")
                values.append(json.dumps(payload, ensure_ascii=False))
            elif discovery_status is not None:
                payload = json.loads(row["discovery_payload"] or "{}")
                payload = payload if isinstance(payload, dict) else {}
                payload["status"] = discovery_status
                if discovery_status in {"pending", "running"}:
                    payload["detail"] = ""
                assignments.append("discovery_payload=?")
                values.append(json.dumps(payload, ensure_ascii=False))
            if blockers is not None:
                assignments.append("blockers=?")
                values.append(json.dumps(list(blockers), ensure_ascii=False))
            db.execute(f"UPDATE pipelines SET {','.join(assignments)} WHERE id=?", (*values, pipeline_id))
            self.event(db, "pipeline.progressed", {"pipeline_id": pipeline_id, "stage": stage,
                       "discovery_status": discovery_status})
        return self.get_pipeline(pipeline_id)

    def set_pipeline_waiting(self, pipeline_id, blockers):
        with self.connect(write=True) as db:
            row = db.execute("SELECT stop_requested FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["stop_requested"]:
                status, stage = "stopped", "stopped"
                blockers = ["Stop was requested before the pipeline entered its waiting state."]
                stop_requested, finished = 1, timestamp()
            else:
                status, stage, stop_requested, finished = "awaiting_user", "awaiting_user", 0, None
            now = timestamp()
            db.execute("UPDATE pipelines SET status=?,stage=?,blockers=?,stop_requested=?,updated_at=?,finished_at=? WHERE id=?",
                       (status, stage, json.dumps(list(blockers), ensure_ascii=False), stop_requested, now, finished, pipeline_id))
            self.event(db, "pipeline.waiting_state", {"pipeline_id": pipeline_id, "status": status,
                       "blockers": blockers})
        return self.get_pipeline(pipeline_id)

    def set_pipeline_paused(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT status,stop_requested FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            now = timestamp()
            if row["stop_requested"] or row["status"] in {"stopping", "stopped"}:
                status, stage = "stopped", "stopped"
                blockers = ["Stop was requested while the queue was paused; no further tasks were scheduled."]
                stop_requested, finished = 1, now
            else:
                status, stage = "queued", None
                blockers = ["Queue paused; the current safe checkpoint is saved."]
                stop_requested, finished = 0, None
            if stage is None:
                db.execute("UPDATE pipelines SET status=?,blockers=?,stop_requested=?,updated_at=?,finished_at=? WHERE id=?",
                           (status, json.dumps(blockers), stop_requested, now, finished, pipeline_id))
            else:
                db.execute("UPDATE pipelines SET status=?,stage=?,blockers=?,stop_requested=?,updated_at=?,finished_at=? WHERE id=?",
                           (status, stage, json.dumps(blockers), stop_requested, now, finished, pipeline_id))
            self.event(db, "pipeline.pause_checkpoint", {"pipeline_id": pipeline_id, "status": status})
        return self.get_pipeline(pipeline_id)

    def recover_interrupted_pipelines(self):
        """Mark in-memory runs left by a prior process for explicit user resume."""
        with self.connect(write=True) as db:
            now = timestamp()
            db.execute("UPDATE scans SET status='incomplete',finished_at=? WHERE status='running'", (now,))
            running_jobs = db.execute("SELECT id,active_stage FROM jobs WHERE run_status='running'").fetchall()
            for job in running_jobs:
                submission = job["active_stage"] == "submission"
                status = "submission_uncertain" if submission else "awaiting_material_review" if job["active_stage"] == "review" else "idle"
                reason = "Server stopped during submission; reconcile the employer account before any retry." if submission else "Worker stopped during server restart; task is available for explicit resume."
                db.execute("UPDATE jobs SET run_status=?,reason=?,active_stage=NULL,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                           (status, reason, job["id"]))
                self.event(db, "lease.recovered_after_restart", {"submission_uncertain": submission}, job["id"])
            rows = db.execute("SELECT id,status,stop_requested FROM pipelines WHERE status IN ('queued','running','stopping')").fetchall()
            for row in rows:
                status = "stopped" if row["stop_requested"] or row["status"] == "queued" else "blocked"
                reason = ("Pipeline stop was saved while the server shut down." if status == "stopped" and row["stop_requested"]
                          else "Server restarted before this queued pipeline began; resume to start it." if row["status"] == "queued"
                          else "Server stopped while this pipeline was working; resume to continue.")
                db.execute("UPDATE pipelines SET status=?,blockers=?,stop_requested=?,updated_at=?,finished_at=? WHERE id=?",
                           (status, json.dumps([reason]), int(bool(row["stop_requested"])), now, now, row["id"]))
                self.event(db, "pipeline.recovered", {"pipeline_id": row["id"], "status": status})


    def bump_profile(self, db):
        version = self.profile_version(db) + 1
        db.execute("UPDATE meta SET value=? WHERE key='profile_version'", (str(version),))
        db.execute("UPDATE jobs SET run_status='submission_uncertain',reason='Profile changed during submission; reconcile outcome.' "
                   "WHERE active_stage='submission' AND run_status='running'")
        db.execute("UPDATE jobs SET phase='new',run_status='idle',active_stage=NULL,lease_owner=NULL,"
                   "lease_token=NULL,lease_until=NULL,reason='Applicant facts changed; reassessment required.' "
                   "WHERE phase IN ('approved','ready_for_submission','form_prepared') AND run_status!='submission_uncertain'")
        self.event(db, "profile.changed", {"version": version})
        return version

    def import_document(self, document_id, path):
        path = Path(path).resolve()
        content = path.read_text(encoding="utf-8-sig")
        checksum = digest(content)
        with self.connect(write=True) as db:
            old = db.execute("SELECT sha256 FROM source_documents WHERE id=?", (document_id,)).fetchone()
            if old and old[0] == checksum:
                return False
            db.execute("INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?)",
                       (document_id, checksum, content, str(path), timestamp()))
            db.execute("INSERT INTO source_documents VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "source_path=excluded.source_path,sha256=excluded.sha256,content=excluded.content,imported_at=excluded.imported_at",
                       (document_id, str(path), checksum, content, timestamp()))
            self.bump_profile(db)
            return True

    def import_canonical_profile_documents(self, master_resume_path, application_profile_path):
        """Import only the two canonical applicant documents in one profile revision.

        This deliberately excludes the broader legacy importer, which also
        reconciles historical employer-account data.
        """
        documents = (
            ("master_resume", Path(master_resume_path).resolve()),
            ("application_profile", Path(application_profile_path).resolve()),
        )
        loaded = [(document_id, path, path.read_text(encoding="utf-8-sig"))
                  for document_id, path in documents]
        prepared = [(document_id, path, content, digest(content))
                    for document_id, path, content in loaded]
        now = timestamp()
        changed_documents = []
        with self.connect(write=True) as db:
            for document_id, path, content, checksum in prepared:
                old = db.execute("SELECT sha256 FROM source_documents WHERE id=?", (document_id,)).fetchone()
                if old and old["sha256"] == checksum:
                    continue
                db.execute("INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?)",
                           (document_id, checksum, content, str(path), now))
                db.execute("INSERT INTO source_documents VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                           "source_path=excluded.source_path,sha256=excluded.sha256,content=excluded.content,"
                           "imported_at=excluded.imported_at",
                           (document_id, str(path), checksum, content, now))
                changed_documents.append(document_id)
            if changed_documents:
                profile_version = self.bump_profile(db)
                self.event(db, "profile.canonical_documents_imported",
                           {"document_ids": changed_documents,
                            "sha256": {document_id: checksum for document_id, _path, _content, checksum in prepared
                                       if document_id in changed_documents},
                            "profile_version": profile_version}, actor="profile_import")
            else:
                profile_version = self.profile_version(db)
        return {"changed": bool(changed_documents), "changed_documents": changed_documents,
                "profile_version": profile_version}

    def learn(self, name, answer, context, source, aliases=()):
        if answer not in ("yes", "no", "unknown") or not context.strip() or not source.strip():
            raise ValueError("Knowledge needs yes/no/unknown, context and a source.")
        key = identity(name)
        with self.connect(write=True) as db:
            old = db.execute("SELECT * FROM technologies WHERE key=?", (key,)).fetchone()
            if old and (old["answer"], old["context"], old["source"]) == (answer, context, source):
                return False
            db.execute("INSERT INTO technologies VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "answer=excluded.answer,context=excluded.context,source=excluded.source,updated_at=excluded.updated_at",
                       (key, name, answer, context, source, timestamp()))
            for alias in (name, *aliases):
                alias_key = identity(alias)
                if not alias_key or alias_key in {"—", "-"}:
                    continue
                # Ambiguous aliases never overwrite another recorded technology.
                db.execute("INSERT OR IGNORE INTO technology_aliases VALUES(?,?)", (alias_key, key))
            self.event(db, "technology.updated", {"key": key, "answer": answer, "context": context,
                       "source": source, "previous": dict(old) if old else None})
            self.bump_profile(db)
        return True

    def technology(self, name, db=None):
        if db is None:
            with self.connect() as conn:
                return self.technology(name, conn)
        key = identity(name)
        row = db.execute("SELECT * FROM technologies WHERE key=?", (key,)).fetchone()
        if not row:
            row = db.execute("SELECT t.* FROM technologies t JOIN technology_aliases a ON t.key=a.technology_key WHERE a.alias=?", (key,)).fetchone()
        return dict(row) if row else {"key": key, "name": name, "answer": "unknown", "context": "No confirmed answer.", "source": ""}

    def paused(self, value=None):
        with self.connect(write=value is not None) as db:
            if value is not None:
                db.execute("UPDATE meta SET value=? WHERE key='paused'", (json.dumps(bool(value)),))
                self.event(db, "queue.paused", {"paused": bool(value)})
            return json.loads(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0])

    def recover(self, db):
        expired = db.execute("SELECT id,active_stage FROM jobs WHERE run_status='running' AND lease_until<?", (time.time(),)).fetchall()
        for row in expired:
            uncertain = row["active_stage"] == "submission"
            db.execute("UPDATE jobs SET run_status=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                       "active_stage=NULL,reason=? WHERE id=?", ("submission_uncertain" if uncertain else "awaiting_material_review" if row["active_stage"] == "review" else "idle",
                       "Worker interrupted; outcome reconciliation required." if uncertain else "Expired worker lease; available for retry.", row["id"]))
            self.event(db, "lease.expired", {"submission_uncertain": uncertain}, row["id"])

    def claim(self, stage, owner, lease_seconds=300, *, pipeline_id=None, job_id=None):
        phases = {"evaluation": "new", "materials": "approved", "review": "approved", "submission": "ready_for_submission"}
        if stage not in phases or not 5 <= lease_seconds <= 3600 or not owner.strip():
            raise ValueError("Invalid stage, owner or lease duration.")
        with self.connect(write=True) as db:
            self.recover(db)
            if json.loads(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0]):
                return None
            # Submission claims are only available for an exact job already enrolled
            # in a user-started pipeline. The legacy global worker stays disabled.
            if stage == "submission" and (not pipeline_id or not job_id):
                raise Conflict("Live submission is not enabled in this foundation release.")
            statuses = (("awaiting_material_review", "retryable_review_error") if stage == "review" else
                        ("idle", "retryable_error", "awaiting_material_correction") if stage == "materials" else ("idle", "retryable_error"))
            if stage == "submission":
                statuses = ("idle", "awaiting_user")
            placeholders = ",".join("?" for _ in statuses)
            scope = " AND EXISTS(SELECT 1 FROM pipeline_jobs pj WHERE pj.job_id=j.id AND pj.pipeline_id=? " \
                    "AND pj.selected=1 AND pj.manual_discard_reason='')" if pipeline_id else ""
            global_scope = (" AND NOT EXISTS(SELECT 1 FROM pipeline_jobs managed WHERE managed.job_id=j.id) "
                            "AND NOT EXISTS(SELECT 1 FROM pipelines company_run WHERE company_run.company_id=j.company_id "
                            "AND company_run.status!='completed')") if not pipeline_id else ""
            exact = " AND j.id=?" if job_id else ""
            if pipeline_id:
                pipeline = db.execute("SELECT p.status,p.stop_requested,c.enabled,c.id FROM pipelines p "
                                      "JOIN companies c ON c.id=p.company_id WHERE p.id=?", (pipeline_id,)).fetchone()
                if not pipeline:
                    raise KeyError(pipeline_id)
                if pipeline["status"] not in {"running", "queued"} or pipeline["stop_requested"]:
                    return None
                if not pipeline["enabled"] or (stage == "submission" and pipeline["id"] == "tesla"):
                    raise Conflict("This company is not enabled for pipeline work.")
            skipped = []
            while True:
                skip_sql = " AND j.id NOT IN (" + ",".join("?" for _ in skipped) + ")" if skipped else ""
                parameters = (phases[stage], *statuses, *((pipeline_id,) if pipeline_id else ()),
                              *((job_id,) if job_id else ()), *skipped)
                row = db.execute("SELECT j.* FROM jobs j JOIN companies c ON j.company_id=c.id "
                                 f"WHERE j.phase=? AND j.run_status IN ({placeholders}) AND c.enabled=1{scope}{global_scope}{exact}{skip_sql} "
                                 "ORDER BY j.first_seen_at,j.id LIMIT 1", parameters).fetchone()
                if not row:
                    return None
                if stage != "materials" or row["run_status"] != "awaiting_material_correction":
                    break
                count = db.execute("SELECT COUNT(*) FROM packages WHERE job_id=? AND jd_version=? AND profile_version=?",
                                   (row["id"], row["jd_version"], self.profile_version(db))).fetchone()[0]
                if count < 3:
                    break
                db.execute("UPDATE jobs SET run_status='awaiting_manual_review',reason='Three material versions failed review; manual correction required.' WHERE id=?", (row["id"],))
                self.event(db, "materials.revision_limit", {"versions": count}, row["id"])
                skipped.append(row["id"])
            if stage == "submission":
                packages = db.execute("SELECT manifest FROM packages WHERE job_id=? AND jd_version=? AND profile_version=? "
                                      "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                      (row["id"], row["jd_version"], self.profile_version(db))).fetchone()
                if not packages or not json.loads(packages["manifest"]).get("upload_ready"):
                    raise Conflict("Submission requires a current package that passed source and visual review.")
                if not db.execute("SELECT 1 FROM pipelines WHERE id=? AND company_id=?", (pipeline_id, row["company_id"])).fetchone():
                    raise Conflict("The job is not part of this company's pipeline.")
            token = uuid.uuid4().hex
            until = time.time() + lease_seconds
            db.execute("UPDATE jobs SET run_status='running',active_stage=?,lease_owner=?,lease_token=?,lease_until=? WHERE id=?",
                       (stage, owner, token, until, row["id"]))
            self.event(db, "job.claimed", {"stage": stage, "owner": owner, "lease_until": until,
                       "pipeline_id": pipeline_id}, row["id"])
            return {"job_id": row["id"], "token": token, "stage": stage,
                    "jd_version": row["jd_version"], "profile_version": self.profile_version(db),
                    "pipeline_id": pipeline_id, "lease_until": until}

    def require_claim(self, db, claim, stage):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (claim["job_id"],)).fetchone()
        if not row or row["run_status"] != "running" or row["active_stage"] != stage \
                or row["lease_token"] != claim["token"] or row["lease_until"] <= time.time():
            raise Conflict("Worker claim is missing, expired or no longer owns this job.")
        if row["jd_version"] != claim["jd_version"] or self.profile_version(db) != claim["profile_version"]:
            raise Conflict("Job or applicant snapshot changed during worker execution.")
        pipeline_id = claim.get("pipeline_id")
        if pipeline_id and not db.execute("SELECT 1 FROM pipeline_jobs WHERE pipeline_id=? AND job_id=? AND selected=1",
                                          (pipeline_id, claim["job_id"])).fetchone():
            raise Conflict("This job is no longer selected in the claimed pipeline.")
        return row

    def release(self, claim, phase, status, reason, db):
        db.execute("UPDATE jobs SET phase=?,run_status=?,reason=?,active_stage=NULL,lease_owner=NULL,"
                   "lease_token=NULL,lease_until=NULL WHERE id=?", (phase, status, reason, claim["job_id"]))
        self.event(db, "job.transition", {"phase": phase, "run_status": status, "reason": reason}, claim["job_id"])

    def fail(self, claim, reason):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND run_status='running'",
                             (claim["job_id"], claim["token"])).fetchone()
            if not row:
                raise Conflict("This worker no longer owns the job.")
            status = "submission_uncertain" if claim["stage"] == "submission" else "retryable_review_error" if claim["stage"] == "review" else "retryable_error"
            self.release(claim, row["phase"], status, reason, db)

    def finish_review(self, claim, package_id, review, rendered):
        with self.connect(write=True) as db:
            self.require_claim(db, claim, "review")
            row = db.execute("SELECT * FROM packages WHERE id=? AND job_id=?", (package_id, claim["job_id"])).fetchone()
            if not row or row["jd_version"] != claim["jd_version"] or row["profile_version"] != claim["profile_version"]:
                raise Conflict("The reviewed package is no longer current.")
            manifest = json.loads(row["manifest"])
            folder = (self.root / "artifacts" / "packages" / package_id).resolve()
            for file in manifest["files"]:
                path = (self.root / file["path"]).resolve()
                if not path.is_relative_to(folder) or digest(path.read_text(encoding="utf-8")) != file["sha256"]:
                    raise Conflict("The material file changed during review.")
            if {Path(item["pdf"]).name for item in rendered} != {"resume.pdf", "cover-letter.pdf"}:
                raise ValueError("Review requires both rendered documents.")
            for item in rendered:
                pdf = Path(item["pdf"]).resolve()
                if (not pdf.is_relative_to(folder) or not item["text_matches"]
                        or len(item["previews"]) != item["pages"]
                        or hashlib.sha256(pdf.read_bytes()).hexdigest() != item["sha256"]):
                    raise Conflict("Rendered document evidence changed or is incomplete.")
                for path, checksum in item["preview_hashes"].items():
                    preview = Path(path).resolve()
                    if not preview.is_relative_to(folder) or hashlib.sha256(preview.read_bytes()).hexdigest() != checksum:
                        raise Conflict("Rendered preview changed during review.")
                if set(item["preview_hashes"]) != set(item["previews"]):
                    raise ValueError("Every visually reviewed page needs a preview hash.")
            documents = {doc["id"]: doc["content"] for doc in db.execute("SELECT id,content FROM source_documents")}
            documents["job_description"] = db.execute("SELECT jd FROM job_versions WHERE job_id=? AND version=?", (claim["job_id"], claim["jd_version"])).fetchone()[0]
            materials = "\n".join((self.root / file["path"]).read_text(encoding="utf-8") for file in manifest["files"])
            materials += "\n" + json.dumps(manifest.get("answers", []), ensure_ascii=False)
            for evidence in review.get("claims", []):
                if not evidence.get("material_excerpt", "").strip() or evidence["material_excerpt"] not in materials:
                    raise ValueError("Review claim does not quote the material it checked.")
                if evidence["verdict"] == "supported" and (not evidence.get("source_excerpt", "").strip()
                        or evidence["source_excerpt"] not in documents.get(evidence["document_id"], "")):
                    raise ValueError("A supported review claim lacks an exact current source excerpt.")
            passed = (review.get("decision") == "pass" and review.get("claims") and not review.get("issues")
                      and not review.get("layout_issues") and all(item["verdict"] == "supported" for item in review["claims"]))
            (folder / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
            manifest["review"] = review
            manifest["rendered"] = rendered
            manifest["upload_ready"] = bool(passed)
            manifest["format"] = "pdf" if passed else "markdown_and_pdf_draft"
            manifest["review_note"] = "Source and visual review passed." if passed else "Review requires correction or clarification."
            if passed:
                manifest["pdf_files"] = [{"path": str(Path(item["pdf"]).relative_to(self.root)),
                    "sha256": hashlib.sha256(Path(item["pdf"]).read_bytes()).hexdigest()} for item in rendered]
                manifest["frozen_at"] = timestamp()
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            db.execute("UPDATE packages SET manifest=? WHERE id=?", (json.dumps(manifest), package_id))
            self.release(claim, "ready_for_submission" if passed else "approved",
                         "idle" if passed else "awaiting_material_correction", manifest["review_note"], db)
            return manifest

    def evaluate(self, claim, proposal):
        if proposal.get("decision") not in {"approve", "discard", "ask"} or not proposal.get("reason", "").strip():
            raise ValueError("Evaluation must contain a decision and reason.")
        with self.connect(write=True) as db:
            job = self.require_claim(db, claim, "evaluation")
            decision, reason = proposal["decision"], proposal["reason"]
            # Keep derived facts in the evaluation, separate from raw feed metadata.
            facts = {key: job[key] for key in ("country", "employment", "arrangement")}
            derived = proposal.get("listing_facts", {})
            jd = db.execute("SELECT jd FROM job_versions WHERE job_id=? AND version=?", (job["id"], job["jd_version"])).fetchone()[0]
            verified_facts = db.execute("SELECT field,answer_value,answer_evidence FROM listing_fact_questions "
                                         "WHERE job_id=? AND jd_version=? AND status='answered' "
                                         "ORDER BY resolved_at DESC,rowid DESC",
                                         (job["id"], job["jd_version"])).fetchall()
            for verified in verified_facts:
                if (facts.get(verified["field"]) == "unknown" and verified["answer_value"] in LISTING_FACT_VALUES.get(verified["field"], ())
                        and verified["answer_evidence"] in jd + "\n" + job["location"]):
                    facts[verified["field"]] = verified["answer_value"]
            for key in facts:
                proposed = derived.get(key, "unknown")
                if facts[key] == "unknown" and proposed != "unknown":
                    evidence = derived.get(key + "_evidence", "")
                    if not evidence.strip() or evidence not in jd + "\n" + job["location"]:
                        raise ValueError("A derived listing fact needs an exact description/location excerpt.")
                    facts[key] = proposed
            # Enforce explicit preferences even when a model proposes a conflicting approval.
            if facts["country"] not in {"US", "unknown"} or facts["employment"] not in {"full_time", "unknown"} or facts["arrangement"] not in {"onsite", "hybrid", "unknown"}:
                decision, reason = "discard", "Listing conflicts with US/full-time/on-site-or-hybrid preferences."
            if proposal.get("sponsorship") == "excluded":
                decision, reason = "discard", "Description explicitly excludes required sponsorship."
            unknown = []
            if decision != "discard":
                unknown = [self.technology(name, db) for name in proposal.get("required_technologies", [])
                           if self.technology(name, db)["answer"] == "unknown"]
                missing_facts = [key for key in ("country", "employment", "arrangement") if facts[key] == "unknown"]
                if unknown or missing_facts:
                    decision = "ask"
                if missing_facts:
                    reason = "Listing facts require verification: " + ", ".join(missing_facts) + ". " + reason
                    pipeline_id = claim.get("pipeline_id")
                    if pipeline_id:
                        prompt_prefix = {
                            "country": "Verify the country for this role from the public listing.",
                            "employment": "Verify the employment type for this role from the public listing.",
                            "arrangement": "Verify whether this role is on-site, hybrid or remote from the public listing.",
                        }
                        now = timestamp()
                        for field in missing_facts:
                            question_id = uuid.uuid4().hex
                            allowed = ", ".join(LISTING_FACT_VALUES[field])
                            prompt = f"{prompt_prefix[field]} Choose one of: {allowed}. Include an exact quote from the current description or location."
                            db.execute("INSERT OR IGNORE INTO listing_fact_questions(id,job_id,pipeline_id,field,jd_version,prompt,status,created_at) "
                                       "VALUES(?,?,?,?,?,?,'pending',?)",
                                       (question_id, job["id"], pipeline_id, field, job["jd_version"], prompt, now))
                            question = db.execute("SELECT id FROM listing_fact_questions WHERE job_id=? AND pipeline_id=? "
                                                  "AND jd_version=? AND field=?",
                                                  (job["id"], pipeline_id, job["jd_version"], field)).fetchone()
                            self.event(db, "listing_fact.question_requested", {"pipeline_id": pipeline_id,
                                       "question_id": question[0], "field": field}, job["id"])
                for tech in unknown:
                    existing = db.execute("SELECT id FROM questions WHERE key=? AND status='pending'", ("tech:" + tech["key"],)).fetchone()
                    question_id = existing[0] if existing else uuid.uuid4().hex
                    if not existing:
                        db.execute("INSERT INTO questions(id,key,technology_key,prompt,created_at) VALUES(?,?,?,?,?)",
                                   (question_id, "tech:" + tech["key"], tech["key"],
                                    f"Do you know {tech['name']}? Yes/No and brief hands-on context, please.", timestamp()))
                    db.execute("INSERT OR IGNORE INTO question_jobs VALUES(?,?)", (question_id, job["id"]))
            payload = {**proposal, "decision": decision, "reason": reason, "effective_listing_facts": facts}
            db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, job["id"],
                       claim["jd_version"], claim["profile_version"], decision, json.dumps(payload), timestamp()))
            self.release(claim, {"approve": "approved", "discard": "discarded", "ask": "new"}[decision],
                         "awaiting_information" if decision == "ask" else "idle", reason, db)
            return payload

    def answer_question(self, question_id, answer, context):
        if answer not in ("yes", "no") or not context.strip():
            raise ValueError("A yes/no answer and context are required.")
        with self.connect(write=True) as db:
            question = db.execute("SELECT * FROM questions WHERE id=? AND status='pending'", (question_id,)).fetchone()
            if not question or not question["technology_key"]:
                raise Conflict("This pending technology question was not found.")
            key = question["technology_key"]
            db.execute("INSERT INTO technologies VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "answer=excluded.answer,context=excluded.context,source=excluded.source,updated_at=excluded.updated_at",
                       (key, key, answer, context, "user answer:" + question_id, timestamp()))
            db.execute("INSERT OR IGNORE INTO technology_aliases VALUES(?,?)", (key, key))
            db.execute("UPDATE questions SET status='answered',answer=?,resolved_at=? WHERE id=?",
                       (json.dumps({"answer": answer, "context": context}), timestamp(), question_id))
            self.bump_profile(db)
            for row in db.execute("SELECT job_id FROM question_jobs WHERE question_id=?", (question_id,)).fetchall():
                pending = db.execute("SELECT 1 FROM questions q JOIN question_jobs l ON q.id=l.question_id WHERE l.job_id=? AND q.status='pending'", (row[0],)).fetchone()
                if not pending:
                    db.execute("UPDATE jobs SET run_status='idle',reason='Question answered; reassessment queued.' "
                               "WHERE id=? AND phase='new' AND run_status='awaiting_information'", (row[0],))
            self.event(db, "question.answered", {"question_id": question_id, "answer": answer, "context": context}, actor="user")

    def browser_lease(self, owner, seconds=300):
        if not owner.strip() or not 5 <= seconds <= 3600:
            raise ValueError("Invalid browser lease.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM resources WHERE key='browser:chrome'").fetchone()
            if row and row["lease_until"] > time.time():
                raise Conflict("The shared Chrome connection is already leased.")
            token = uuid.uuid4().hex
            db.execute("INSERT INTO resources VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "owner=excluded.owner,token=excluded.token,lease_until=excluded.lease_until",
                       ("browser:chrome", owner, token, time.time() + seconds))
            self.event(db, "browser.claimed", {"owner": owner, "token": token})
            return token

    def release_browser(self, token):
        with self.connect(write=True) as db:
            db.execute("DELETE FROM resources WHERE key='browser:chrome' AND token=?", (token,))

    def import_submission(self, job_id, path, kind, source, attribution="unknown", *, claim=None,
                          confirmation_text=None, requisition_id=None, job_title=None, source_url=None):
        if kind not in {"receipt", "employer_account"}:
            raise ValueError("Unsupported submission evidence type.")
        content = Path(path).read_text(encoding="utf-8-sig")
        job = self.get_job(job_id)
        requisition = job["requisition_id"]
        if not requisition or not re.search(r"(?<!\d)" + re.escape(requisition) + r"(?!\d)", content):
            raise ValueError("Submission evidence does not identify this requisition.")
        if kind == "receipt":
            if claim is None:
                if "Your Application Has Been Received" not in content:
                    raise ValueError("Receipt lacks the saved confirmation text.")
            else:
                with self.connect() as db:
                    active = self.require_claim(db, claim, "submission")
                if active["id"] != job_id or requisition_id != requisition or job_title != job["title"]:
                    raise Conflict("Receipt identifiers do not match the current submission claim.")
                if not confirmation_text or confirmation_text not in content or job_title not in content:
                    raise ValueError("Receipt needs the exact confirmation text, current role title and requisition.")
                parsed = urlsplit(source_url or "")
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("Receipt evidence must include the verified HTTPS confirmation URL.")
                origin = f"https://{parsed.hostname.casefold()}" + (f":{parsed.port}" if parsed.port and parsed.port != 443 else "")
                with self.connect() as db:
                    origins = {urlsplit(job["url"]).hostname.casefold() if urlsplit(job["url"]).hostname else ""}
                    career = db.execute("SELECT c.careers_url FROM companies c JOIN jobs j ON j.company_id=c.id WHERE j.id=?",
                                        (job_id,)).fetchone()
                    if career:
                        career_host = urlsplit(career[0]).hostname
                        if career_host:
                            origins.add(career_host.casefold())
                    approved = db.execute("SELECT 1 FROM submission_approvals WHERE job_id=? AND pipeline_id=? "
                                          "AND action='origin' AND status='consumed' AND terms_text LIKE ? LIMIT 1",
                                          (job_id, claim.get("pipeline_id"), f"%{origin}%")).fetchone()
                    if parsed.hostname.casefold() not in origins and not approved:
                        raise Conflict("Receipt URL is outside the job/careers origin and no consumed origin approval matches it.")
        if kind == "employer_account" and not re.search(r"^\s*- generic: .*/" + re.escape(requisition) + r"\s*$", content, re.M):
            raise ValueError("Account snapshot does not list this application.")
        checksum = digest(content)
        evidence_root = self.root / "artifacts" / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        artifact = evidence_root / f"{checksum}.md"
        artifact.write_text(content, encoding="utf-8")
        with self.connect(write=True) as db:
            old = db.execute("SELECT id FROM evidence WHERE job_id=? AND kind=? AND sha256=?", (job_id, kind, checksum)).fetchone()
            evidence_id = old[0] if old else uuid.uuid4().hex
            if not old:
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)", (evidence_id, job_id, kind,
                           str(artifact.relative_to(self.root)), checksum, source_url or source, attribution, timestamp()))
            # A pipeline worker keeps its claim until it has closed/reconciled the
            # owned browser tab and calls finish_submission. Legacy evidence imports
            # have no active claim and can settle immediately.
            active = db.execute("SELECT active_stage,run_status FROM jobs WHERE id=?", (job_id,)).fetchone()
            owned_submission = active and active["active_stage"] == "submission" and active["run_status"] == "running"
            db.execute("UPDATE jobs SET phase='submitted',run_status=?,reason=?,active_stage=?,"
                       "lease_owner=?,lease_token=?,lease_until=? WHERE id=?",
                       ("running" if owned_submission else "idle",
                        f"Confirmed by imported {kind} evidence; submission attribution {attribution}.",
                        "submission" if owned_submission else None,
                        db.execute("SELECT lease_owner FROM jobs WHERE id=?", (job_id,)).fetchone()[0] if owned_submission else None,
                        db.execute("SELECT lease_token FROM jobs WHERE id=?", (job_id,)).fetchone()[0] if owned_submission else None,
                        db.execute("SELECT lease_until FROM jobs WHERE id=?", (job_id,)).fetchone()[0] if owned_submission else None,
                        job_id))
            self.event(db, "submission.imported", {"evidence_id": evidence_id, "kind": kind, "attribution": attribution}, job_id,
                       "submission_worker" if owned_submission else "legacy_import")
            return evidence_id

    def store_package(self, claim, package):
        resume, letter = package.get("resume_markdown", ""), package.get("cover_letter_markdown", "")
        if not resume.strip() or not letter.strip() or not package.get("sources"):
            raise ValueError("A package needs a resume, cover letter and source references.")
        package_id = uuid.uuid4().hex
        with self.connect(write=True) as db:
            self.require_claim(db, claim, "materials")
            for reference in package["sources"]:
                row = db.execute("SELECT content FROM source_documents WHERE id=?", (reference.get("document_id"),)).fetchone()
                excerpt = reference.get("excerpt", "")
                if not row or not excerpt.strip() or excerpt not in row[0]:
                    raise ValueError("Material reference is not an exact excerpt from a current source document.")
            folder = self.root / "artifacts" / "packages" / package_id
            folder.mkdir(parents=True)
            files = []
            for name, text in (("resume.md", resume), ("cover-letter.md", letter)):
                file = folder / name
                file.write_text(text, encoding="utf-8", newline="\n")
                files.append({"path": str(file.relative_to(self.root)), "sha256": digest(text), "hash_mode": "normalized_utf8_text"})
            manifest = {"package_id": package_id, "job_id": claim["job_id"], "jd_version": claim["jd_version"],
                        "profile_version": claim["profile_version"], "files": files, "sources": package["sources"],
                        "answers": package.get("answers", {}), "format": "markdown", "upload_ready": False,
                        "review_note": "Draft package. PDF rendering and factual quality review still required."}
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)", (package_id, claim["job_id"], claim["jd_version"],
                       claim["profile_version"], json.dumps(manifest), timestamp()))
            # A Markdown draft is not upload-ready. Hold until PDF/QA integration is implemented.
            self.release(claim, "approved", "awaiting_material_review", "Draft materials saved; PDF rendering and quality review required.", db)
            return manifest

    # The agent-driven path below is additive. It deliberately does not call the
    # legacy evaluator, deterministic eligibility gates, or legacy submission
    # worker. A worker's decision is retained verbatim and scoped to its task.

    @staticmethod
    def _agent_decode(value, fallback=None):
        try:
            return json.loads(value) if value is not None else ({} if fallback is None else fallback)
        except (TypeError, ValueError):
            return {} if fallback is None else fallback

    @staticmethod
    def _agent_validate_notes(context):
        if not isinstance(context, dict):
            raise ValueError("Worker context notes must be an object.")
        protected = AGENT_HOST_CONTEXT_KEYS.intersection(context)
        if protected:
            raise Conflict("Worker context cannot overwrite host-owned safety or artifact fields: "
                           + ", ".join(sorted(protected)))
        return context

    @staticmethod
    def _agent_url_allowed(raw_url, *, test_only=False):
        try:
            parsed = urlsplit(str(raw_url or ""))
            if not parsed.hostname or parsed.username or parsed.password:
                return False
            if parsed.scheme == "https":
                return True
            return bool(test_only and parsed.scheme == "http"
                        and parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"})
        except ValueError:
            return False

    @staticmethod
    def _agent_worker_record(row):
        result = dict(row)
        result["context"] = Store._agent_decode(result.get("context"), {})
        result["result"] = Store._agent_decode(result.get("result"), {})
        # Turn baselines are host bookkeeping used to decide whether a saved
        # discovery checkpoint made progress. They are never agent instructions.
        result["context"].pop("discovery_turn_baseline", None)
        result["context"].pop("last_context_revision", None)
        result.pop("capability_sha256", None)
        result.pop("capability_expires_at", None)
        return result

    def _agent_insert_worker(self, db, pipeline_id, role, *, job_id=None, parent_id=None,
                             context=None, resume_mode="new_session"):
        now = timestamp()
        worker_id = uuid.uuid4().hex
        job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
        db.execute("INSERT INTO agent_workers(id,pipeline_id,job_id,parent_id,role,state,resume_mode,context,"
                   "jd_version,profile_version,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)",
                   (worker_id, pipeline_id, job_id, parent_id, role, resume_mode,
                    json.dumps(context or {}, ensure_ascii=False), job["jd_version"] if job else None,
                    self.profile_version(db), now, now))
        self.event(db, "agent.worker_queued", {"pipeline_id": pipeline_id, "worker_id": worker_id,
                   "parent_id": parent_id, "job_id": job_id, "role": role}, job_id, "agent_scheduler")
        return worker_id

    def create_agent_pipeline(self, company_name, careers_url, filters=None, *, company_id=None,
                              allow_http_loopback_test=False, test_only=False):
        """Create a browser-discovery pipeline for any user-supplied HTTPS career site."""
        name = " ".join(str(company_name or "").split())
        parsed = urlsplit(str(careers_url or "").strip())
        if not name or len(name) > 160:
            raise ValueError("A company name of 1–160 characters is required.")
        if test_only and not allow_http_loopback_test:
            raise ValueError("Test-only pipelines require the explicit loopback HTTP opt-in.")
        test_only = bool(test_only and allow_http_loopback_test)
        if not self._agent_url_allowed(careers_url, test_only=test_only):
            raise ValueError("An absolute HTTPS careers URL is required.")
        if len(str(careers_url)) > 2048:
            raise ValueError("The careers URL is too long.")
        career_url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        normalized_filters = dict(filters or {})
        for key in ("title_contains", "location_contains"):
            value = " ".join(str(normalized_filters.get(key, "") or "").split())
            if len(value) > 120:
                raise ValueError("Title and location filters must be 120 characters or fewer.")
            normalized_filters[key] = value
        # These are user-selected pipeline facts supplied to the discovery
        # agent, not host-side listing rejection rules.
        normalized_filters.update({"country": "US", "employment": "full_time",
                                   "arrangement": ["onsite", "hybrid"]})
        normalized_filters["career_scope"] = self._agent_career_scope()
        company_key = identity(company_id or name)
        if not company_key or len(company_key) > 160:
            raise ValueError("Company identity is invalid.")
        pipeline_id, now = uuid.uuid4().hex, timestamp()
        with self.connect(write=True) as db:
            company = db.execute("SELECT name,careers_url FROM companies WHERE id=?", (company_key,)).fetchone()
            if company and company["careers_url"] != career_url:
                active = db.execute("SELECT 1 FROM pipelines WHERE company_id=? AND mode='agentic' "
                                    "AND status IN ('queued','running','stopping','awaiting_user','quota_paused') LIMIT 1",
                                    (company_key,)).fetchone()
                if active:
                    raise Conflict("A running agentic pipeline has snapshotted this company's careers URL.")
            db.execute("INSERT INTO companies(id,name,careers_url,enabled) VALUES(?,?,?,0) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,careers_url=excluded.careers_url",
                       (company_key, name, career_url))
            active = db.execute("SELECT id FROM pipelines WHERE company_id=? AND mode='agentic' "
                                "AND status IN ('queued','running','stopping','awaiting_user','quota_paused') LIMIT 1",
                                (company_key,)).fetchone()
            if active:
                raise Conflict("This company already has an active agentic pipeline. Resume or stop it first.")
            db.execute("INSERT INTO pipelines(id,company_id,status,stage,filters,discovery_status,discovery_payload,"
                       "blockers,stop_requested,mode,agent_test_only,created_at,updated_at) VALUES(?,?, 'queued','discovery',?,"
                       "'pending','{}','[]',0,'agentic',?,?,?)",
                       (pipeline_id, company_key, json.dumps(normalized_filters, ensure_ascii=False),
                        int(test_only), now, now))
            discovery_worker = self._agent_insert_worker(db, pipeline_id, "discovery")
            self.event(db, "agent.pipeline_created", {"pipeline_id": pipeline_id, "company_id": company_key,
                       "careers_url": career_url, "filters": normalized_filters,
                       "discovery_worker_id": discovery_worker}, actor="user")
        return self.get_pipeline(pipeline_id)

    def _agent_pipeline_counts(self, db, pipeline_id):
        rows = db.execute("SELECT pj.selected,pj.agent_decision,pj.agent_status,j.id,j.phase,j.run_status,"
                          "o.status AS application_status FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id "
                          "LEFT JOIN application_outcomes o ON o.job_id=j.id WHERE pj.pipeline_id=?",
                          (pipeline_id,)).fetchall()
        counts = {key: 0 for key in ("discovered", "selected", "excluded", "evaluated", "accepted",
                                      "discarded", "submitted", "awaiting_user", "uncertain",
                                      "recruiter_pending", "recruiter_rejected", "recruiter_accepted",
                                      "recruiter_unknown")}
        counts["discovered"] = len(rows)
        for row in rows:
            if not row["selected"]:
                counts["excluded"] += 1
                continue
            counts["selected"] += 1
            decision = row["agent_decision"]
            if decision in {"approve", "discard"}:
                counts["evaluated"] += 1
            if decision == "approve":
                counts["accepted"] += 1
            # A verified submission is exclusive with discard, but remains in the
            # cumulative evaluated/accepted funnel above.
            if row["agent_status"] == "submitted" or row["phase"] == "submitted":
                counts["submitted"] += 1
                outcome = row["application_status"] or "unknown"
                counts["recruiter_" + outcome] += 1
                continue
            if row["agent_status"] == "discarded" or decision == "discard":
                counts["discarded"] += 1
            if row["agent_status"] == "uncertain" or row["run_status"] == "submission_uncertain":
                counts["uncertain"] += 1
        counts["awaiting_user"] = db.execute("SELECT COUNT(*) FROM agent_inbox WHERE pipeline_id=? AND state='pending'",
                                             (pipeline_id,)).fetchone()[0]
        return counts

    def agent_pipeline_counts(self, pipeline_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            return self._agent_pipeline_counts(db, pipeline_id)

    def list_agent_workers(self, pipeline_id=None):
        with self.connect() as db:
            if pipeline_id is not None and not db.execute("SELECT 1 FROM pipelines WHERE id=?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            sql = "SELECT * FROM agent_workers"
            args = ()
            if pipeline_id is not None:
                sql += " WHERE pipeline_id=?"
                args = (pipeline_id,)
            sql += " ORDER BY created_at,id"
            return [self._agent_worker_record(row) for row in db.execute(sql, args)]

    def get_agent_worker(self, worker_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if not row:
                raise KeyError(worker_id)
            return self._agent_worker_record(row)

    def _agent_insert_inbox(self, db, worker, kind, *, dedupe_key="", question="", context=None,
                            response_hint="", summary="", action=""):
        if kind not in {"question", "approval", "first_application_review"}:
            raise ValueError("Unsupported agent inbox kind.")
        now = timestamp()
        if dedupe_key:
            old = db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND dedupe_key=? AND state='pending'",
                             (worker["id"], dedupe_key)).fetchone()
            if old:
                return dict(old), False
        item_id = uuid.uuid4().hex
        db.execute("INSERT INTO agent_inbox(id,pipeline_id,worker_id,job_id,kind,dedupe_key,question,context,"
                   "response_hint,summary,action,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                   (item_id, worker["pipeline_id"], worker["id"], worker["job_id"], kind,
                    dedupe_key, question, json.dumps(context or {}, ensure_ascii=False), response_hint,
                    summary, action, now, now))
        wait_state = "waiting_question" if kind == "question" else "waiting_approval"
        db.execute("UPDATE agent_workers SET state=?,checkpoint=?,updated_at=? WHERE id=?",
                   (wait_state, "inbox:" + item_id, now, worker["id"]))
        self.event(db, "agent.inbox_created", {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
                   "item_id": item_id, "kind": kind, "action": action}, worker["job_id"], "agent")
        return dict(db.execute("SELECT * FROM agent_inbox WHERE id=?", (item_id,)).fetchone()), True

    def agent_inbox(self, pipeline_id):
        with self.connect() as db:
            pipeline = db.execute("SELECT status,stop_requested FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not pipeline:
                raise KeyError(pipeline_id)
            rows = db.execute("SELECT i.*,w.role,w.jd_version AS worker_jd_version,"
                              "w.profile_version AS worker_profile_version,j.title AS job_title,j.url AS job_url "
                              "FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id "
                              "LEFT JOIN jobs j ON j.id=i.job_id WHERE i.pipeline_id=? ORDER BY i.created_at,i.id",
                              (pipeline_id,)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["context"] = self._agent_decode(item["context"], {})
                item["response"] = self._agent_decode(item["response"], "")
                if item["kind"] in {"approval", "first_application_review"}:
                    item["stale_reason"] = self._agent_inbox_stale_reason(db, item)
                    item["is_current"] = not bool(item["stale_reason"])
                    item["scope_token"] = self._agent_approval_scope_token(item)
                    item["batch_eligible"] = (item["kind"] == "approval" and item["state"] == "pending"
                                              and item["is_current"]
                                              and not pipeline["stop_requested"]
                                              and pipeline["status"] not in {"stopped", "completed"})
                items.append(item)
            return items

    def _agent_approval_scope_token(self, row):
        """Fingerprint the exact displayed request, never a standing/future approval."""
        scope = {key: row.get(key) for key in ("id", "pipeline_id", "worker_id", "job_id", "kind",
                                              "action", "summary", "question", "response_hint", "created_at")}
        scope["context"] = row.get("context") if isinstance(row.get("context"), dict) else self._agent_decode(row.get("context"), {})
        return digest(json.dumps(scope, sort_keys=True, ensure_ascii=False, separators=(",", ":")))

    def _agent_grant_browser_recovery_attempt(self, db, item_id, now):
        """A real exact recovery approval grants one new attempt, once per approval."""
        item = db.execute("SELECT i.*,w.jd_version AS worker_jd_version,w.profile_version AS worker_profile_version "
                          "FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=?", (item_id,)).fetchone()
        if (not item or item["state"] != "approved"
                or item["action"] not in {"browser_circuit_recovery", "browser_tab_recovery"}):
            raise Conflict("A recorded exact browser recovery approval is required.")
        reason = self._agent_inbox_stale_reason(db, dict(item))
        if reason:
            raise Conflict(reason)
        worker = db.execute("SELECT w.*,p.stop_requested,p.status AS pipeline_status,c.careers_url,j.url AS job_url "
                            "FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id JOIN companies c ON c.id=p.company_id "
                            "LEFT JOIN jobs j ON j.id=w.job_id WHERE w.id=?", (item["worker_id"],)).fetchone()
        context = self._agent_decode(worker["context"], {})
        if worker["stop_requested"] or worker["pipeline_status"] in {"stopped", "stopping", "completed"}:
            raise Conflict("A stopped or possibly sent application cannot receive a tab retry.")
        if worker["state"] == "uncertain" or self._context_may_have_sent_final(context, worker["checkpoint"]):
            if item["action"] != "browser_circuit_recovery" or worker["role"] != "submission":
                raise Conflict("A stopped or possibly sent application cannot receive a tab retry.")
            if context.get("reconciliation_recovery", {}).get("approval_item_id") == item_id:
                return False
            context["reconciliation_recovery"] = {"approval_item_id": item_id,
                "authorized_at": item["resolved_at"] or now, "read_only": True,
                "fresh_tab_budget": 0, "final_action_replay_authorized": False,
                "instruction": "One bounded read-only inspection of the same application tab. No editing, uploads, new tab, or Submit replay. Record a verified receipt or preserve an uncertain outcome."}
            db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?",
                       (json.dumps(context), now, worker["id"]))
            self.event(db, "agent.browser_reconciliation_recovery_granted", {
                "pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
                "approval_item_id": item_id, "read_only": True, "fresh_tab_budget": 0,
                "final_action_replay_authorized": False}, worker["job_id"], "approval_recovery")
            return True
        if context.get("tab_recovery", {}).get("approval_item_id") == item_id:
            return False
        context["tab_recovery"] = {"attempt_id": uuid.uuid4().hex, "approval_item_id": item_id,
            "authorized_at": item["resolved_at"] or now, "granted_at": now,
            "source": "recorded exact human browser recovery approval", "fresh_tab_budget": 1,
            "canonical_url": worker["job_url"] or worker["careers_url"]}
        db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?",
                   (json.dumps(context), now, worker["id"]))
        self.event(db, "agent.browser_recovery_granted", {"pipeline_id": worker["pipeline_id"],
                   "worker_id": worker["id"], "approval_item_id": item_id,
                   "attempt_id": context["tab_recovery"]["attempt_id"], "fresh_tab_budget": 1}, worker["job_id"], "approval_recovery")
        return True

    def repair_approved_missing_tab_recovery(self, item_id):
        """Repair the pre-fix spent-budget bug using its existing human decision."""
        with self.connect(write=True) as db:
            item = db.execute("SELECT * FROM agent_inbox WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise KeyError(item_id)
            worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (item["worker_id"],)).fetchone()
            result = db.execute("SELECT observations FROM agent_submission_results WHERE worker_id=? AND state='failed' "
                                "ORDER BY rowid DESC LIMIT 1", (worker["id"],)).fetchone()
            observed = self._agent_decode(result[0] if result else "{}", {})
            context = self._agent_decode(worker["context"], {})
            if (worker["state"] != "failed" or worker["turn_active"] or worker["checkpoint"] != "recovery_tab_missing"
                    or observed.get("tab_missing_from_fresh_inventory") is not True
                    or observed.get("final_action_started") is not False
                    or observed.get("connector_recovery_approval_id") != item_id
                    or observed.get("tab_recovery_attempt_id") != context.get("tab_recovery", {}).get("attempt_id")):
                raise Conflict("This worker is not the inactive, unsent, approved missing-tab recovery failure.")
            now = timestamp()
            if not self._agent_grant_browser_recovery_attempt(db, item_id, now):
                raise Conflict("This approval already granted its bounded recovery attempt; do not retry it again.")
            db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,error='',completed_at=NULL,"
                       "capability_sha256='',capability_expires_at=NULL,updated_at=? WHERE id=?",
                       ("same_session" if worker["session_id"] else "new_session", now, worker["id"]))
            self.event(db, "agent.browser_recovery_repaired", {"pipeline_id": worker["pipeline_id"],
                       "worker_id": worker["id"], "approval_item_id": item_id,
                       "reason": "Existing approval had incorrectly reused an earlier spent attempt."}, worker["job_id"], "supervisor")
        return self.get_agent_worker(worker["id"])

    def reconcile_rejected_greenhouse_code(self, worker_id, event_id):
        """Reconcile a proved code rejection, never an ambiguous application send.

        This operator recovery preserves the prior send history and human scope.
        It prepares an inactive worker for the same application; it does not
        launch work, click Submit, grant consent or claim a receipt.
        """
        from .ephemeral_auth import AUTH_ACTIONS
        with self.connect(write=True) as db:
            worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if (not worker or worker['role'] != 'submission' or worker['turn_active']
                    or worker['state'] not in {'stopped', 'uncertain', 'failed', 'interrupted'}):
                raise Conflict('Code rejection recovery requires an inactive submission worker.')
            job = db.execute('SELECT * FROM jobs WHERE id=?', (worker['job_id'],)).fetchone()
            if (not job or job['phase'] == 'submitted'
                    or db.execute("SELECT 1 FROM agent_submission_results WHERE job_id=? AND state='submitted'",
                                  (worker['job_id'],)).fetchone()):
                raise Conflict('A submitted application cannot enter code rejection recovery.')
            observed = self._agent_latest_cua_event(db, worker, success=True)
            proof = (observed or {}).get('verification') or {}
            if (not observed or observed['event_id'] != event_id
                    or observed.get('application_url') != job['url']
                    or not proof.get('present') or not proof.get('invalid_code')
                    or proof.get('submit_enabled') is not False):
                raise Conflict('A fresh exact-page Invalid security code observation is required; ambiguous sends stay uncertain.')
            current_profile = self.profile_version(db)
            approved = False
            for item in db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND state='approved'", (worker_id,)):
                context = self._agent_decode(item['context'], {})
                if (item['action'] in AUTH_ACTIONS and context.get('job_url') == job['url']
                        and context.get('jd_version') == worker['jd_version'] == job['jd_version']
                        and context.get('profile_version') == worker['profile_version'] == current_profile):
                    approved = True
            if not approved:
                raise Conflict('The same current email-verification step needs its exact human approval.')
            context = self._agent_decode(worker['context'], {})
            history = context.setdefault('verified_code_rejections', [])
            if any(h['source_event_id'] == event_id for h in history):
                raise Conflict('This rejection observation already granted its recovery.')
            history.append({'source_event_id': event_id, 'reconciled_at': timestamp(),
                            'prior_send_may_have_occurred': self._context_may_have_sent_final(context, worker['checkpoint']),
                            'prior_authorized_at': context.get('final_action_authorized_at'),
                            'prior_verification_scope': context.get('email_verification_final_scope'),
                            'outcome': 'Greenhouse explicitly rejected the security code; no submission accepted.'})
            context['final_action_may_have_been_sent'] = False
            context.pop('email_verification_final_scope', None)
            context.pop('reconciliation_recovery', None)
            now = timestamp()
            db.execute("UPDATE agent_workers SET state='stopped',checkpoint='security_code_rejection_reconciled',"
                       "context=?,error='',completed_at=NULL,capability_sha256='',capability_expires_at=NULL,updated_at=? WHERE id=?",
                       (json.dumps(context), now, worker_id))
            db.execute("UPDATE jobs SET run_status='idle',reason='Verified Greenhouse security-code rejection; same-application recovery pending.' WHERE id=?",
                       (worker['job_id'],))
            db.execute("UPDATE pipeline_jobs SET agent_status='approved' WHERE pipeline_id=? AND job_id=?",
                       (worker['pipeline_id'], worker['job_id']))
            self.event(db, 'agent.security_code_rejection_reconciled', {'worker_id': worker_id,
                       'source_event_id': event_id, 'prior_send_history_preserved': True,
                       'human_approval_unchanged': True, 'application_accepted': False}, worker['job_id'], 'supervisor')
        return self.get_agent_worker(worker_id)

    def recover_current_code_rejection(self, worker_id):
        """Resume at most twice, only after exact negative proof in an active run."""
        from .trial_limits import read as read_trial, expired
        with self.connect() as db:
            worker=db.execute('SELECT w.*,p.stop_requested,p.status AS pipeline_status FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(worker_id,)).fetchone()
            if (not worker or worker['role']!='submission' or worker['turn_active'] or worker['stop_requested']
                    or worker['pipeline_status'] in {'stopped','stopping','completed'}
                    or worker['state'] not in {'uncertain','failed','interrupted'}):return False
            quota=db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
            if quota and self._agent_decode(quota[0],{}).get('paused'):return False
            trial=read_trial(db,worker['pipeline_id'])
            if trial and (trial['state']!='active' or expired(trial) or worker['job_id'] not in trial['job_ids']):return False
            context=self._agent_decode(worker['context'],{})
            if len(context.get('verified_code_rejections',[]))>=2:return False
            evidence=self._agent_latest_cua_event(db,worker,success=True)
            if not (evidence or {}).get('verification',{}).get('invalid_code'):return False
        try:self.reconcile_rejected_greenhouse_code(worker_id,evidence['event_id'])
        except Conflict:return False
        request_path=self.root/'data/auth-handoffs'/(worker_id+'.request.json')
        if request_path.exists():
            request=json.loads(request_path.read_text());request['cancelled_at_unix']=time.time()
            temporary=request_path.with_suffix('.tmp');temporary.write_text(json.dumps(request));temporary.replace(request_path)
        with self.connect(write=True) as db:
            # A simultaneous user stop always wins over recovery.
            changed=db.execute("UPDATE agent_workers SET state='queued',error='' WHERE id=? AND state='stopped' AND checkpoint='security_code_rejection_reconciled' AND EXISTS(SELECT 1 FROM pipelines p WHERE p.id=agent_workers.pipeline_id AND p.stop_requested=0 AND p.status NOT IN ('stopped','stopping','completed'))",(worker_id,)).rowcount
            if changed:self.event(db,'agent.security_code_recovery_queued',{'worker_id':worker_id,'source_event_id':evidence['event_id']},worker['job_id'],'scheduler')
        return bool(changed)

    def release_completed_auth_controls(self):
        """Retire orphan auth locks only after an inactive committed submission."""
        with self.connect(write=True) as db:
            rows=db.execute("SELECT c.* FROM agent_browser_controls c JOIN agent_workers w ON w.id=c.worker_id WHERE c.state='open' AND c.control_type='company_auth' AND w.state='completed' AND w.turn_active=0 AND EXISTS(SELECT 1 FROM agent_submission_results r WHERE r.worker_id=w.id AND r.state='submitted')").fetchall()
            for row in rows:
                db.execute("UPDATE agent_browser_controls SET state='closed' WHERE scope_key=? AND worker_id=?",(row['scope_key'],row['worker_id']))
                self.event(db,'agent.completed_auth_control_released',{'scope_key':row['scope_key'],'worker_id':row['worker_id']},actor='scheduler')
            return len(rows)

    def _agent_record_inbox_decision(self, db, row, decision, provenance, now, *, batch_id=None):
        """Caller holds the write transaction and has validated this checkpoint."""
        db.execute("UPDATE agent_inbox SET state=?,response=?,provenance=?,updated_at=?,resolved_at=? WHERE id=?",
                   ("approved" if decision == "approve" else "rejected", json.dumps(decision), provenance, now, now, row["id"]))
        if decision == "approve" and row["action"] in {"browser_circuit_recovery", "browser_tab_recovery"}:
            self._agent_grant_browser_recovery_attempt(db, row["id"], now)
        if decision == 'approve':
            from .ephemeral_auth import bind_human_decision
            bind_human_decision(self,db,db.execute('SELECT * FROM agent_inbox WHERE id=?',(row['id'],)).fetchone())
        db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,error='',capability_sha256='',"
                   "capability_expires_at=NULL,updated_at=? WHERE id=? AND state='waiting_approval'",
                   ("same_session" if row["session_id"] else "new_session", now, row["worker_id"]))
        payload = {"pipeline_id": row["pipeline_id"], "worker_id": row["worker_id"],
                   "item_id": row["id"], "decision": decision, "provenance": provenance}
        if batch_id:
            payload.update(batch_id=batch_id, scope_token=self._agent_approval_scope_token(dict(row)))
        self.event(db, "agent.inbox_decided", payload, row["job_id"], "user")
        db.execute("UPDATE pipelines SET status='running',stage=?,blockers='[]',finished_at=NULL,updated_at=? "
                   "WHERE id=? AND stop_requested=0", (row["role"], now, row["pipeline_id"]))
        return dict(db.execute("SELECT * FROM agent_inbox WHERE id=?", (row["id"],)).fetchone())

    def decide_agent_inbox_batch(self, pipeline_id, items, decision, provenance=""):
        """All-or-nothing human decision for explicitly selected current requests."""
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject.")
        if not isinstance(provenance, str) or len(provenance) > 1000:
            raise ValueError("An optional decision note must be at most 1000 characters.")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("Select between 1 and 100 exact requests.")
        ids = [item.get("id") for item in items]
        if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
            raise ValueError("Each selected request must appear exactly once.")
        batch_id = uuid.uuid4().hex
        note = provenance.strip() or f"User explicitly selected {decision} for these exact checkpoints in dashboard batch {batch_id}."
        with self.connect(write=True) as db:
            pipeline = db.execute("SELECT status,stop_requested,mode FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not pipeline:
                raise KeyError(pipeline_id)
            if pipeline["mode"] != "agentic" or pipeline["stop_requested"] or pipeline["status"] in {"stopped", "completed"}:
                raise Conflict("The pipeline is stopped or complete; no batch decisions were recorded.")
            rows = []
            for item in items:
                row = db.execute("SELECT i.*,w.session_id,w.role,w.jd_version AS worker_jd_version,"
                                 "w.profile_version AS worker_profile_version FROM agent_inbox i "
                                 "JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=? AND i.pipeline_id=?",
                                 (item["id"], pipeline_id)).fetchone()
                if not row or row["kind"] != "approval" or row["state"] != "pending":
                    raise Conflict("A selected request is no longer batch eligible. No decisions were recorded; review the inbox.")
                if item.get("scope_token") != self._agent_approval_scope_token(dict(row)):
                    raise Conflict("A selected request changed after selection. No decisions were recorded; review and select it again.")
                stale_reason = self._agent_inbox_stale_reason(db, dict(row))
                if stale_reason:
                    raise Conflict(stale_reason + " No batch decisions were recorded.")
                rows.append(row)
            now = timestamp()
            results = [self._agent_record_inbox_decision(db, row, decision, note, now, batch_id=batch_id) for row in rows]
            self.event(db, "agent.inbox_batch_decided", {"batch_id": batch_id, "pipeline_id": pipeline_id,
                       "decision": decision, "items": items, "provenance": note}, actor="user")
        return {"batch_id": batch_id, "items": results}

    def _agent_inbox_stale_reason(self, db, inbox):
        """Validate the immutable job/profile snapshot attached to an approval."""
        context = inbox.get("context") if isinstance(inbox.get("context"), dict) else self._agent_decode(inbox.get("context"), {})
        if inbox.get("action") == "browser_circuit_recovery":
            control = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=?", (context.get("scope") or "connector:cua_repl",)).fetchone()
            if (not control or control["state"] != "open" or control["worker_id"] != inbox.get("worker_id")
                    or context.get("source_event_id") not in self._agent_decode(control["event_ids"], [])):
                return "This connector recovery checkpoint has been superseded."
            return ""
        if inbox.get("state") == "stale":
            return context.get("stale_reason") or inbox.get("provenance") or "This approval was invalidated."
        if not inbox.get("job_id"):
            return "A job-scoped approval must identify its job." \
                if inbox.get("kind") == "first_application_review" else ""
        job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (inbox["job_id"],)).fetchone()
        membership = db.execute("SELECT selected FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                (inbox["pipeline_id"], inbox["job_id"])).fetchone()
        if not job or not membership or not membership["selected"]:
            return "The job is missing or is no longer selected in this pipeline."
        current_profile = self.profile_version(db)
        if context.get("jd_version") != job["jd_version"]:
            return "The job description changed after this approval was prepared."
        if context.get("profile_version") != current_profile:
            return "The applicant profile changed after this approval was prepared."
        if inbox.get("worker_jd_version") != job["jd_version"]:
            return "The worker's saved job description version is stale."
        if inbox.get("worker_profile_version") != current_profile:
            return "The worker's saved applicant profile version is stale."
        if "ready_artifacts" in context:
            current_artifacts = self._agent_artifact_snapshot(self._agent_ready_artifacts(
                db, inbox["pipeline_id"], inbox["job_id"], job["jd_version"], current_profile))
            if context.get("ready_artifacts") != current_artifacts:
                return "Prepared documents changed after this approval was requested."
        return ""

    def reconsider_evaluator_questions(self, item_ids, reason):
        """Withdraw operator-reviewed questions without inventing applicant answers.

        This maintenance action does not classify question text, make an
        eligibility decision, approve an action or start any worker.
        """
        if (not isinstance(item_ids, list) or not item_ids or len(item_ids) > 200
                or any(not isinstance(value, str) or not value for value in item_ids)):
            raise ValueError("Provide the exact reviewed evaluator-question IDs.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Record the user's reason for reconsidering these questions.")
        with self.connect(write=True) as db:
            now = timestamp()
            affected = set()
            for item_id in dict.fromkeys(item_ids):
                row = db.execute("SELECT i.*,w.role,w.turn_active FROM agent_inbox i "
                                 "JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=?", (item_id,)).fetchone()
                if not row:
                    raise KeyError(item_id)
                if row["kind"] != "question" or row["state"] != "pending" or row["role"] != "evaluator":
                    raise Conflict("Only explicitly reviewed pending evaluator questions can be reconsidered.")
                workers = {row["worker_id"]}
                fact = db.execute("SELECT id FROM agent_fact_questions WHERE inbox_item_id=? AND state='pending'",
                                  (item_id,)).fetchone()
                if fact:
                    workers.update(value[0] for value in db.execute(
                        "SELECT worker_id FROM agent_fact_subscribers WHERE fact_id=?", (fact["id"],)))
                for worker_id in workers:
                    worker = db.execute("SELECT role,turn_active FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
                    if worker["role"] != "evaluator" or worker["turn_active"]:
                        raise Conflict("An active worker or shared non-evaluator question must keep its current checkpoint.")
                context = self._agent_decode(row["context"], {})
                context["withdrawal"] = {"reason": reason.strip(), "actor": "user", "at": now,
                                         "applicant_answer_supplied": False}
                db.execute("UPDATE agent_inbox SET state='stale',context=?,provenance=?,resolved_at=?,updated_at=? WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), reason.strip(), now, now, item_id))
                if fact:
                    db.execute("UPDATE agent_fact_questions SET state='cancelled',resolved_at=? WHERE id=?", (now, fact["id"]))
                affected.update(workers)
                self.event(db, "agent.question_withdrawn", {"pipeline_id": row["pipeline_id"],
                           "worker_id": row["worker_id"], "item_id": item_id, "reason": reason.strip(),
                           "applicant_answer_supplied": False}, row["job_id"], "user")
            for worker_id in affected:
                worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
                if worker["state"] not in {"waiting_question", "interrupted"}:
                    continue
                if db.execute("SELECT 1 FROM agent_inbox i WHERE i.state='pending' AND (i.worker_id=? OR EXISTS("
                              "SELECT 1 FROM agent_fact_questions f JOIN agent_fact_subscribers s ON s.fact_id=f.id "
                              "WHERE f.inbox_item_id=i.id AND s.worker_id=?)) LIMIT 1", (worker_id, worker_id)).fetchone():
                    continue
                context = self._agent_decode(worker["context"], {})
                context["question_reconsideration"] = {"reason": reason.strip(), "at": now,
                    "instruction": "Assess from existing evidence under the current hands-free policy. The withdrawn question has no applicant answer."}
                # Explicit operator reconsideration deliberately refreshes this
                # role's contract; routine prompt edits leave other sessions pinned.
                context.pop('instruction_contract_id',None)
                db.execute("UPDATE agent_workers SET state='interrupted',context=?,resume_mode=?,error='',"
                           "capability_sha256='',capability_expires_at=NULL,updated_at=? WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), "same_session" if worker["session_id"] else "new_session",
                            now, worker_id))
            return {"withdrawn": len(set(item_ids)), "affected_workers": sorted(affected), "workers_started": 0}

    def set_routine_document_uploads(self, enabled, provenance):
        """Operator-only standing permission; agents cannot grant this themselves."""
        if not isinstance(enabled, bool) or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Record the user's document-upload preference and instruction.")
        policy = {"enabled": enabled, "artifact_kinds": ["resume", "cover_letter"],
                  "scope": "approved jobs in manually selected company pipelines; their official job/application sites",
                  "provenance": provenance.strip(), "authorized_at": timestamp(), "actor": "user"}
        with self.connect(write=True) as db:
            db.execute("INSERT INTO meta(key,value) VALUES('routine_document_uploads',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(policy, ensure_ascii=False),))
            self.event(db, "policy.routine_document_uploads_updated", policy, actor="user")
        return policy

    def routine_document_uploads(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='routine_document_uploads'").fetchone()
            return self._agent_decode(row["value"], {}) if row else {"enabled": False}

    def supersede_routine_upload_requests(self, item_ids):
        """Retire exact operator-reviewed upload prompts under standing scope.

        This records no per-file human approval and cannot retire legal,
        CAPTCHA, first-review, rejected or unrelated-destination checkpoints.
        """
        if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 100:
            raise ValueError("Provide the exact reviewed upload-request IDs.")
        with self.connect(write=True) as db:
            now = timestamp()
            workers = set()
            for item_id in dict.fromkeys(item_ids):
                item = db.execute("SELECT * FROM agent_inbox WHERE id=?", (item_id,)).fetchone()
                if not item:
                    raise KeyError(item_id)
                if (item["kind"] != "approval" or item["state"] != "pending" or item["action"] not in {
                        "upload", "upload_resume", "resume_upload", "upload_cover_letter", "cover_letter_upload", "document_upload"}):
                    raise Conflict("Only exact pending routine document-upload requests can be superseded.")
                worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (item["worker_id"],)).fetchone()
                policy = self._agent_routine_upload_policy(db, worker)
                if worker["turn_active"] or not policy.get("enabled"):
                    raise Conflict("The worker must be inactive and the current job must have standing upload authorization.")
                context = self._agent_decode(item["context"], {})
                parsed = urlsplit(context.get("page_url") or context.get("page_origin") or "")
                host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
                if parsed.scheme != "https" or not any(host == allowed or host.endswith("." + allowed) for allowed in policy["allowed_hosts"]):
                    raise Conflict("The observed destination is outside the standing upload scope.")
                current = self._agent_artifact_snapshot(self._agent_ready_artifacts(db, worker["pipeline_id"], worker["job_id"],
                                                                                  worker["jd_version"], worker["profile_version"]))
                if not current or context.get("ready_artifacts") != current:
                    raise Conflict("The upload request does not match the current ready documents.")
                context["superseded_by"] = {"authorization_type": "user_standing_instruction", "at": now,
                    "reason": "Routine upload is covered by the user's standing instruction; no separate per-file approval was given."}
                db.execute("UPDATE agent_inbox SET state='stale',context=?,provenance=?,updated_at=?,resolved_at=? WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), policy["provenance"], now, now, item_id))
                workers.add(worker["id"])
                self.event(db, "agent.routine_upload_request_superseded", {"item_id": item_id, "worker_id": worker["id"],
                           "pipeline_id": worker["pipeline_id"], "authorization_type": "user_standing_instruction"}, worker["job_id"], "user")
            for worker_id in workers:
                state = self._agent_pending_inbox_wait(db, worker_id) or "interrupted"
                db.execute("UPDATE agent_workers SET state=?,updated_at=? WHERE id=? AND state IN ('waiting_approval','stopped','interrupted')",
                           (state, now, worker_id))
            return {"superseded": len(set(item_ids)), "per_file_human_approvals_recorded": 0}

    def _agent_routine_upload_policy(self, db, worker, pipeline=None):
        if worker["role"] != "submission" or not worker["job_id"]:
            return {"enabled": False}
        row = db.execute("SELECT value FROM meta WHERE key='routine_document_uploads'").fetchone()
        policy = self._agent_decode(row["value"], {}) if row else {}
        if policy.get("enabled") is not True:
            return {"enabled": False}
        job = db.execute("SELECT j.url,j.title,j.jd_version,pj.selected,pj.agent_decision FROM jobs j "
                         "JOIN pipeline_jobs pj ON pj.job_id=j.id WHERE pj.pipeline_id=? AND j.id=?",
                         (worker["pipeline_id"], worker["job_id"])).fetchone()
        if (not job or not job["selected"] or job["agent_decision"] != "approve"
                or job["jd_version"] != worker["jd_version"] or self.profile_version(db) != worker["profile_version"]):
            return {"enabled": False, "reason": "Current selected job and approved applicant snapshot are required."}
        rejected = db.execute("SELECT 1 FROM agent_inbox WHERE worker_id=? AND state='rejected' "
                              "AND kind IN ('approval','first_application_review') AND json_extract(context,'$.jd_version')=? "
                              "AND json_extract(context,'$.profile_version')=? LIMIT 1",
                              (worker["id"], worker["jd_version"], worker["profile_version"])).fetchone()
        if rejected:
            return {"enabled": False, "reason": "An explicit current user rejection must be resolved; standing permission does not override it."}
        pipeline = pipeline if pipeline is not None else db.execute(
            "SELECT p.*,c.name AS company_name,c.careers_url FROM pipelines p JOIN companies c ON c.id=p.company_id WHERE p.id=?",
            (worker["pipeline_id"],)).fetchone()
        hosts = set()
        for url in (job["url"], pipeline["careers_url"]):
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname:
                hosts.add(parsed.hostname.lower().removeprefix("www.").rstrip("."))
        if not hosts:
            return {"enabled": False, "reason": "No HTTPS employer/application site is in standing scope."}
        return {"enabled": True, "authorization_type": "user_standing_instruction", "actor": "user",
                "company_id": pipeline["company_id"], "company_name": pipeline["company_name"],
                "job_id": worker["job_id"], "job_url": job["url"], "artifact_kinds": ["resume", "cover_letter"],
                "allowed_hosts": sorted(hosts), "include_subdomains": True,
                "scope": "Only current ready tailored resume/cover-letter artifacts for this approved job, on its verified employer application page. Other files or unrelated destinations are excluded.",
                "jd_version": worker["jd_version"], "profile_version": worker["profile_version"],
                "provenance": policy.get("provenance"), "authorized_at": policy.get("authorized_at")}

    def authorize_agent_signin(self, pipeline_id, mailbox, provenance, handoff_item_id=None):
        """Record explicit user permission for one company's routine browser login."""
        if not isinstance(mailbox, str) or "@" not in mailbox or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Record the authorized mailbox and the user's instruction.")
        with self.connect(write=True) as db:
            pipeline = db.execute("SELECT * FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not pipeline:
                raise KeyError(pipeline_id)
            now = timestamp()
            policy = {"enabled": True, "saved_browser_autofill": True, "mailbox": mailbox.strip(),
                "mail_origin": "https://mail.google.com", "email_otp": True, "scope": "this company's active application sign-in only",
                "authorized_at": now, "provenance": provenance.strip(), "credential_changes": False}
            if handoff_item_id:
                item = db.execute("SELECT i.*,w.role,w.turn_active FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=?",
                                  (handoff_item_id,)).fetchone()
                gate = db.execute("SELECT worker_id FROM agent_browser_controls WHERE scope_key=? AND state='open' AND control_type='company_auth'",
                                  ("company:" + pipeline["company_id"],)).fetchone()
                if (not item or item["pipeline_id"] != pipeline_id or item["kind"] != "question" or item["state"] != "pending"
                        or item["role"] != "submission" or item["turn_active"] or not gate or gate["worker_id"] != item["worker_id"]
                        or self._agent_decode(item["context"], {}).get("fact_scope")):
                    raise Conflict("Only the exact reviewed operational sign-in handoff of the inactive login owner can be superseded.")
                context = self._agent_decode(item["context"], {})
                context["withdrawal"] = {"reason": "User authorized automatic routine sign-in; sign-in has not been claimed complete.", "at": now}
                db.execute("UPDATE agent_inbox SET state='stale',context=?,provenance=?,updated_at=?,resolved_at=? WHERE id=?",
                           (json.dumps(context), provenance.strip(), now, now, handoff_item_id))
                db.execute("UPDATE agent_workers SET state='interrupted',updated_at=? WHERE id=?",
                           (now, item["worker_id"]))
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("agent_signin_automation:" + pipeline_id, json.dumps(policy)))
            self.event(db, "agent.signin_automation_authorized", {"pipeline_id": pipeline_id, "mail_origin": policy["mail_origin"],
                       "handoff_item_id": handoff_item_id, "provenance": provenance.strip(), "authentication_verified": False}, actor="user")
            return policy

    def answer_agent_inbox(self, item_id, response, provenance=""):
        if not isinstance(response, str) or not response.strip() or len(response) > 5000:
            raise ValueError("A nonblank user answer of at most 5000 characters is required.")
        if not isinstance(provenance, str) or len(provenance) > 1000:
            raise ValueError("An optional answer note must be at most 1000 characters.")
        provenance = provenance.strip() or "Direct answer from the applicant in the dashboard."
        with self.connect(write=True) as db:
            row = db.execute("SELECT i.*,w.state AS worker_state,w.session_id,w.role,w.pipeline_id AS worker_pipeline_id "
                             "FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id WHERE i.id=?",
                             (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            if row["kind"] != "question" or row["state"] != "pending":
                raise Conflict("This inbox item is not a pending question.")
            pipeline = db.execute("SELECT status,stop_requested,mode FROM pipelines WHERE id=?",
                                  (row["pipeline_id"],)).fetchone()
            if pipeline["mode"] != "agentic" or pipeline["stop_requested"] or pipeline["status"] in {"stopped", "completed"}:
                raise Conflict("The pipeline is stopped or complete; the question cannot resume its worker.")
            now = timestamp()
            answer = json.dumps(response, ensure_ascii=False) if not isinstance(response, str) else json.dumps(response, ensure_ascii=False)
            db.execute("UPDATE agent_inbox SET state='answered',response=?,provenance=?,updated_at=?,resolved_at=? WHERE id=?",
                       (answer, provenance.strip(), now, now, item_id))
            fact = db.execute("SELECT * FROM agent_fact_questions WHERE inbox_item_id=? AND state='pending'",
                              (item_id,)).fetchone()
            subscribers = ([dict(value) for value in db.execute(
                "SELECT s.worker_id,s.pipeline_id,s.job_id,w.session_id,w.state,p.stop_requested,p.status "
                "FROM agent_fact_subscribers s JOIN agent_workers w ON w.id=s.worker_id "
                "JOIN pipelines p ON p.id=s.pipeline_id WHERE s.fact_id=? ORDER BY s.created_at,s.worker_id",
                (fact["id"],)).fetchall()] if fact else [])
            if fact:
                db.execute("UPDATE agent_fact_questions SET state='answered',resolved_at=? WHERE id=?",
                           (now, fact["id"]))
                for subscriber in subscribers:
                    if subscriber["stop_requested"] or subscriber["status"] in {"stopped", "completed"}:
                        continue
                    db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,error='',capability_sha256='',"
                               "capability_expires_at=NULL,updated_at=? WHERE id=? AND state='waiting_question'",
                               ("same_session" if subscriber["session_id"] else "new_session", now,
                                subscriber["worker_id"]))
                    db.execute("UPDATE pipelines SET status='running',stage='processing',blockers='[]',finished_at=NULL,updated_at=? "
                               "WHERE id=? AND stop_requested=0", (now, subscriber["pipeline_id"]))
                    self.event(db, "agent.fact_answer_shared", {"fact_id": fact["id"],
                               "item_id": item_id, "worker_id": subscriber["worker_id"],
                               "provenance": provenance.strip()}, subscriber["job_id"], "dashboard_operator")
            else:
                db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,error='',capability_sha256='',"
                           "capability_expires_at=NULL,updated_at=? WHERE id=? "
                           "AND state='waiting_question'",
                           ("same_session" if row["session_id"] else "new_session", now, row["worker_id"]))
            self.event(db, "agent.inbox_answered", {"pipeline_id": row["pipeline_id"], "worker_id": row["worker_id"],
                       "item_id": item_id, "provenance": provenance.strip(),
                       "fact_id": fact["id"] if fact else None}, row["job_id"], "dashboard_operator")
            if not fact:
                db.execute("UPDATE pipelines SET status='running',stage=?,blockers='[]',finished_at=NULL,updated_at=? "
                           "WHERE id=? AND stop_requested=0", (row["role"], now, row["pipeline_id"]))
            return dict(db.execute("SELECT * FROM agent_inbox WHERE id=?", (item_id,)).fetchone())

    def decide_agent_inbox(self, item_id, decision, provenance="", scope_token=""):
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject.")
        if not isinstance(provenance, str) or len(provenance) > 1000:
            raise ValueError("An optional decision note must be at most 1000 characters.")
        provenance = provenance.strip() or f"User explicitly selected {decision} for this checkpoint in the dashboard."
        stale_reason = ""
        with self.connect(write=True) as db:
            row = db.execute("SELECT i.*,w.session_id,w.role,w.jd_version AS worker_jd_version,"
                             "w.profile_version AS worker_profile_version "
                             "FROM agent_inbox i JOIN agent_workers w ON w.id=i.worker_id "
                             "WHERE i.id=?", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            if row["kind"] not in {"approval", "first_application_review"} or row["state"] != "pending":
                raise Conflict("This inbox item is not a pending approval or review.")
            if scope_token and scope_token != self._agent_approval_scope_token(dict(row)):
                raise Conflict("This request changed after it was displayed. Review the inbox before deciding.")
            pipeline = db.execute("SELECT status,stop_requested,mode FROM pipelines WHERE id=?", (row["pipeline_id"],)).fetchone()
            if pipeline["mode"] != "agentic" or pipeline["stop_requested"] or pipeline["status"] in {"stopped", "completed"}:
                raise Conflict("The pipeline is stopped or complete; the decision cannot resume its worker.")
            now = timestamp()
            stale_reason = self._agent_inbox_stale_reason(db, dict(row))
            if stale_reason:
                context = self._agent_decode(row["context"], {})
                context["stale_reason"] = stale_reason
                db.execute("UPDATE agent_inbox SET state='stale',context=?,response='',provenance=?,updated_at=?,resolved_at=? "
                           "WHERE id=? AND state='pending'",
                           (json.dumps(context, ensure_ascii=False), "system: " + stale_reason, now, now, item_id))
                self.event(db, "agent.inbox_staled", {"pipeline_id": row["pipeline_id"],
                           "worker_id": row["worker_id"], "item_id": item_id,
                           "reason": stale_reason}, row["job_id"], "profile_refresh")
                self._agent_refresh_pipeline(db, row["pipeline_id"])
                result = dict(db.execute("SELECT * FROM agent_inbox WHERE id=?", (item_id,)).fetchone())
            else:
                result = self._agent_record_inbox_decision(db, row, decision, provenance.strip(), now)
        if stale_reason:
            raise Conflict(stale_reason)
        return result

    def get_agent_quota(self):
        with self.connect() as db:
            raw = db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
            state = self._agent_decode(raw[0] if raw else "{}", {})
            if not isinstance(state, dict):
                state = {}
            state.setdefault("paused", False)
            state.setdefault("provider_error", None)
            state.setdefault("reason", None)
            state.setdefault("paused_at", None)
            state["worker_ids"] = [row[0] for row in db.execute(
                "SELECT id FROM agent_workers WHERE state='quota_paused' ORDER BY updated_at,id")]
            state["affected_count"] = len(state["worker_ids"])
            return state

    def set_agent_quota(self, paused, *, provider_error=None, reason=None):
        with self.connect(write=True) as db:
            previous_raw = db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
            previous = self._agent_decode(previous_raw[0] if previous_raw else "{}", {})
            now = timestamp()
            if paused:
                state = {"paused": True, "provider_error": str(provider_error or "")[:4000],
                         "reason": str(reason or "Account quota requires manual resume.")[:4000],
                         "paused_at": previous.get("paused_at") or now}
            else:
                state = {"paused": False, "provider_error": None, "reason": None, "paused_at": None}
            db.execute("INSERT INTO meta(key,value) VALUES('agent_quota_latch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(state, ensure_ascii=False),))
            self.event(db, "agent.quota_latch", state, actor="agent_runtime" if paused else "user")
        return self.get_agent_quota()

    def _agent_adoption_eligibility(self, db, pipeline_id, job_id):
        row = db.execute("SELECT p.mode,p.company_id,p.status,j.company_id AS job_company,j.phase,j.run_status "
                         "FROM pipelines p JOIN pipeline_jobs pj ON pj.pipeline_id=p.id "
                         "JOIN jobs j ON j.id=pj.job_id WHERE p.id=? AND j.id=?",
                         (pipeline_id, job_id)).fetchone()
        if not row:
            return False, "The job is not a member of this pipeline."
        if row["mode"] != "legacy":
            return False, "Only a legacy pipeline job can be explicitly adopted."
        if row["company_id"] != row["job_company"]:
            return False, "The job belongs to another company."
        if row["phase"] == "submitted" or db.execute("SELECT 1 FROM evidence WHERE job_id=? LIMIT 1", (job_id,)).fetchone():
            return False, "A recorded application already exists."
        if row["run_status"] == "submission_uncertain":
            return False, "The prior submission is uncertain and must be reconciled before adoption."
        if db.execute("SELECT 1 FROM agent_adoptions WHERE source_pipeline_id=? AND source_job_id=?",
                      (pipeline_id, job_id)).fetchone():
            return False, "This legacy job was already adopted."
        return True, "Eligible for explicit agentic re-evaluation; no legacy decision or answer will be inherited."

    def agent_adoption_eligibility(self, pipeline_id, job_id):
        with self.connect() as db:
            eligible, reason = self._agent_adoption_eligibility(db, pipeline_id, job_id)
            return {"eligible": eligible, "reason": reason}

    def adopt_legacy_job(self, pipeline_id, job_id, provenance):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Explicit adoption requires user provenance.")
        with self.connect(write=True) as db:
            eligible, reason = self._agent_adoption_eligibility(db, pipeline_id, job_id)
            if not eligible:
                raise Conflict(reason)
            legacy = db.execute("SELECT p.*,c.name AS company_name,c.careers_url FROM pipelines p "
                                "JOIN companies c ON c.id=p.company_id WHERE p.id=?", (pipeline_id,)).fetchone()
            new_id, now = uuid.uuid4().hex, timestamp()
            db.execute("INSERT INTO pipelines(id,company_id,status,stage,filters,discovery_status,discovery_payload,"
                       "blockers,stop_requested,mode,adopted_from_pipeline_id,created_at,updated_at) "
                       "VALUES(?,?, 'queued','discovery',?,'pending','{}','[]',0,'agentic',?,?,?)",
                       (new_id, legacy["company_id"], legacy["filters"], pipeline_id, now, now))
            self._agent_insert_worker(db, new_id, "discovery")
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                       "VALUES(?,?,?,1,'selected')", (new_id, job_id, now))
            job = db.execute("SELECT phase,run_status,jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job["phase"] == "submitted" or job["run_status"] == "submission_uncertain":
                db.execute("UPDATE pipeline_jobs SET agent_status=? WHERE pipeline_id=? AND job_id=?",
                           ("submitted" if job["phase"] == "submitted" else "uncertain", new_id, job_id))
            else:
                self._agent_insert_worker(db, new_id, "evaluator", job_id=job_id,
                                          context={"adopted_from_pipeline_id": pipeline_id,
                                                   "re_evaluation": True})
            legacy_questions = db.execute("SELECT q.id FROM questions q JOIN question_jobs qj ON qj.question_id=q.id "
                                          "WHERE qj.job_id=? AND q.status='pending'", (job_id,)).fetchall()
            for question in legacy_questions:
                db.execute("INSERT OR IGNORE INTO agent_legacy_question_links VALUES(?,?,?,'superseded','system',?,?)",
                           (new_id, job_id, question["id"],
                            "Superseded for this adopted run by explicit agentic re-evaluation; the historical question remains unanswered.", now))
            db.execute("INSERT INTO agent_adoptions VALUES(?,?,?,?,?)",
                       (pipeline_id, job_id, new_id, provenance.strip(), now))
            self.event(db, "agent.legacy_job_adopted", {"source_pipeline_id": pipeline_id, "source_job_id": job_id,
                       "pipeline_id": new_id, "provenance": provenance.strip(),
                       "legacy_questions_superseded": [item["id"] for item in legacy_questions]}, job_id, "user")
            result = self._pipeline_record(db, db.execute("SELECT * FROM pipelines WHERE id=?", (new_id,)).fetchone())
            result["source_pipeline_id"] = pipeline_id
            result["source_job_id"] = job_id
            return result

    def _agent_evaluation_cache_snapshot(self, db, pipeline_id, job_id, jd_version, execution=None):
        """Build a fail-closed key for reusable agent-authored decisions."""
        pipeline = db.execute("SELECT p.filters,p.company_id FROM pipelines p WHERE p.id=? AND p.mode='agentic'",
                              (pipeline_id,)).fetchone()
        job = db.execute("SELECT j.id,j.external_key,j.requisition_id,j.url,j.title,jd.sha256 AS jd_sha256 "
                         "FROM jobs j JOIN job_versions jd ON jd.job_id=j.id AND jd.version=? WHERE j.id=?",
                         (jd_version, job_id)).fetchone()
        if not pipeline or not job:
            return None
        prompt_path = self.root / "prompts" / "evaluation_agent.md"
        config_path = self.root / "config" / "workers.json"
        try:
            core = self.root / "prompts/agent_core.md"
            prompt_hash = hashlib.sha256(prompt_path.read_bytes() + (core.read_bytes() if core.is_file() else b"")).hexdigest()
            worker_config = json.loads(config_path.read_text(encoding="utf-8"))
            from .worker_settings import read_settings
            model_config = dict(execution or read_settings(self.root, db)["roles"]["evaluation"])
            model_config.pop("settings_revision", None)
            model_config.pop("transport", None)
            if model_config.get("browser_backend") == "playwright":
                prompt_hash = hashlib.sha256((prompt_hash + (self.root / "prompts/browser_playwright.md").read_text(encoding="utf-8")).encode()).hexdigest()
            if not isinstance(model_config, dict) or not isinstance(model_config.get("model"), str):
                return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        answered_facts = []
        for row in db.execute("SELECT i.id,i.question,i.response,i.context,i.job_id FROM agent_inbox i "
                              "WHERE i.kind='question' AND i.state='answered' ORDER BY i.id"):
            context = self._agent_decode(row["context"], {})
            if (isinstance(context, dict) and
                    (context.get("fact_scope") == "applicant" or
                     context.get("fact_scope") == "job" and row["job_id"] == job_id)):
                answered_facts.append([row["id"], row["question"], row["response"], context])
        profile_content = {
            "sources": [list(row) for row in db.execute("SELECT id,sha256 FROM source_documents ORDER BY id")],
            "technologies": [list(row) for row in db.execute(
                "SELECT key,name,answer,context,source FROM technologies ORDER BY key")],
            "aliases": [list(row) for row in db.execute(
                "SELECT alias,technology_key FROM technology_aliases ORDER BY alias")],
            "answered_questions": [list(row) for row in db.execute(
                "SELECT q.id,q.prompt,q.answer FROM questions q WHERE q.status='answered' AND "
                "(NOT EXISTS(SELECT 1 FROM question_jobs qj WHERE qj.question_id=q.id) OR EXISTS("
                "SELECT 1 FROM question_jobs qj WHERE qj.question_id=q.id AND qj.job_id=?)) ORDER BY q.id",
                (job_id,))],
            "answered_facts": answered_facts,
        }
        filters = self._agent_pipeline_filters({"filters": pipeline["filters"],
                                                "company_id": pipeline["company_id"],
                                                "adopted_from_pipeline_id": None})
        career_scope = self._agent_career_scope()
        policy = {"career_scope": career_scope, "filters": filters,
                  "evaluation_prompt_sha256": prompt_hash}
        snapshot = {"job_id": job["id"], "company_id": pipeline["company_id"],
                    "external_key": job["external_key"],
                    "requisition_id": job["requisition_id"], "url": job["url"], "title": job["title"],
                    "jd_sha256": job["jd_sha256"], "jd_version": jd_version,
                    "profile_version": self.profile_version(db),
                    "profile_sha256": digest(json.dumps(profile_content, ensure_ascii=False, sort_keys=True)),
                    "filters": filters,
                    "career_scope_sha256": digest(json.dumps(career_scope, ensure_ascii=False, sort_keys=True)),
                    "policy_sha256": digest(json.dumps(policy, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":"))),
                    "prompt_sha256": prompt_hash,
                    "model": model_config["model"], "effort": model_config.get("effort", "medium"),
                    "provider": model_config.get("provider", "openai"), "browser_backend": model_config.get("browser_backend", "cua")}
        key = digest(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return {"cache_key": key, "snapshot": snapshot,
                "policy_sha256": snapshot["policy_sha256"], "model": snapshot["model"]}

    def _agent_reuse_cached_evaluation(self, db, pipeline_id, job_id, jd_version):
        snapshot = self._agent_evaluation_cache_snapshot(db, pipeline_id, job_id, jd_version)
        cache_key = snapshot["cache_key"] if snapshot else "unavailable-policy"
        row = (db.execute("SELECT * FROM agent_evaluation_cache WHERE cache_key=?", (cache_key,)).fetchone()
               if snapshot else None)
        if not row:
            db.execute("INSERT INTO agent_cache_observations(pipeline_id,worker_id,cache_kind,hit,cache_key,created_at) "
                       "VALUES(?,NULL,'evaluation',0,?,?)", (pipeline_id, cache_key, timestamp()))
            return None
        cached = self._agent_decode(row["payload"], {})
        decision = row["decision"]
        if decision not in {"approve", "discard"} or not isinstance(cached, dict):
            db.execute("INSERT INTO agent_cache_observations(pipeline_id,worker_id,cache_kind,hit,cache_key,created_at) "
                       "VALUES(?,NULL,'evaluation',0,?,?)", (pipeline_id, cache_key, timestamp()))
            return None
        worker_id = self._agent_insert_worker(db, pipeline_id, "evaluator", job_id=job_id,
                                              context={"reused_cache_key": snapshot["cache_key"],
                                                       "cached_source_worker_id": row["source_worker_id"]})
        result = {**cached, "worker_id": worker_id, "cached": True,
                  "cached_source_worker_id": row["source_worker_id"], "cache_key": snapshot["cache_key"],
                  "completed_at": timestamp()}
        state = "completed" if decision == "approve" else "discarded"
        db.execute("UPDATE agent_workers SET state=?,result=?,completed_at=?,updated_at=? WHERE id=?",
                   (state, json.dumps(result, ensure_ascii=False), timestamp(), timestamp(), worker_id))
        db.execute("UPDATE pipeline_jobs SET agent_decision=?,agent_reason=?,agent_status=? WHERE pipeline_id=? AND job_id=?",
                   (decision, str(cached.get("reason") or "")[:12000],
                    "approved" if decision == "approve" else "discarded", pipeline_id, job_id))
        submission_id = None
        if decision == "approve":
            submission_id = self._agent_insert_worker(db, pipeline_id, "submission", job_id=job_id,
                                                      context={"evaluation_worker_id": worker_id,
                                                               "evaluation_cache_key": snapshot["cache_key"]})
        now = timestamp()
        db.execute("INSERT INTO agent_cache_observations(pipeline_id,worker_id,cache_kind,hit,cache_key,created_at) "
                   "VALUES(?,?,'evaluation',1,?,?)",
                   (pipeline_id, worker_id, snapshot["cache_key"], now))
        self.event(db, "agent.evaluation_cache_hit", {"pipeline_id": pipeline_id, "job_id": job_id,
                   "worker_id": worker_id, "source_worker_id": row["source_worker_id"],
                   "cache_key": snapshot["cache_key"], "decision": decision,
                   "submission_worker_id": submission_id}, job_id, "agent_cache")
        return {"worker_id": worker_id, "submission_worker_id": submission_id, "decision": decision}

    def _agent_cache_metrics(self, db, pipeline_id=None):
        where, params = (("WHERE pipeline_id=?", (pipeline_id,)) if pipeline_id else ("", ()))
        rows = db.execute("SELECT cache_kind,hit,COUNT(*) AS count FROM agent_cache_observations "
                          + where + " GROUP BY cache_kind,hit", params).fetchall()
        result = {"evaluation_hits": 0, "evaluation_misses": 0,
                  "render_hits": 0, "render_misses": 0,
                  "evaluation_entries": 0, "render_entries": 0}
        for row in rows:
            prefix = "evaluation" if row["cache_kind"] == "evaluation" else "render"
            result[prefix + ("_hits" if row["hit"] else "_misses")] = int(row["count"])
        result["evaluation_entries"] = db.execute("SELECT COUNT(*) FROM agent_evaluation_cache").fetchone()[0]
        result["render_entries"] = db.execute("SELECT COUNT(*) FROM agent_render_cache").fetchone()[0]
        if pipeline_id:
            result["scope"] = "pipeline"
        else:
            result["scope"] = "all_pipelines"
        return result

    def _agent_record_listing(self, db, worker, payload):
        title = payload.get("title")
        jd = payload.get("jd")
        raw_url = payload.get("url")
        if not isinstance(title, str) or not title.strip() or not isinstance(jd, str) or not jd.strip():
            raise ValueError("A listing requires its actual title and complete job description.")
        test_only_row = db.execute("SELECT agent_test_only FROM pipelines WHERE id=?",
                                   (worker["pipeline_id"],)).fetchone()
        test_only = bool(test_only_row and test_only_row[0])
        if not self._agent_url_allowed(raw_url, test_only=test_only):
            raise ValueError("A listing requires its exact HTTPS URL or an opted-in loopback fixture URL.")
        parsed = urlsplit(str(raw_url or ""))
        # The local browser harness opts in to loopback HTTP; do not silently
        # rewrite its exact fixture origin to HTTPS. Production rows are HTTPS.
        scheme = parsed.scheme.lower() if test_only else "https"
        url = urlunsplit((scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        if len(url) > 4096 or len(jd) > 200000:
            raise ValueError("Listing URL or description exceeds the supported size.")
        requisition = str(payload.get("requisition_id") or "").strip() or None
        if requisition and len(requisition) > 256:
            raise ValueError("Requisition ID is too long.")
        external_key = "req:" + requisition if requisition else "url:" + url
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        selection_observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        checksum_data = json.dumps({"title": title.strip(), "jd": jd, "url": url,
                                    "metadata": metadata}, ensure_ascii=False, sort_keys=True)
        checksum = digest(checksum_data)
        now = timestamp()
        pipeline = db.execute("SELECT p.company_id,p.status,p.stop_requested,p.agent_test_only FROM pipelines p "
                              "WHERE p.id=? AND p.mode='agentic'",
                              (worker["pipeline_id"],)).fetchone()
        if not pipeline:
            raise Conflict("The discovery capability is not scoped to an agentic pipeline.")
        old = db.execute("SELECT * FROM jobs WHERE company_id=? AND external_key=?",
                         (pipeline["company_id"], external_key)).fetchone()
        created = old is None
        changed = False
        if old:
            job_id = old["id"]
            previous = db.execute("SELECT sha256,jd,metadata FROM job_versions WHERE job_id=? AND version=?",
                                  (job_id, old["jd_version"])).fetchone()
            # A re-observation is not a new qualification snapshot. Compare
            # actual content, including all employer metadata; host repair
            # provenance lives in immutable events and must not invalidate an
            # in-flight assessment or its cache.
            prior_metadata = self._agent_decode(previous["metadata"], {}) if previous else {}
            prior_metadata = {k: v for k, v in prior_metadata.items() if k not in {"repaired_by_worker", "observation"}}
            changed = (not previous or previous["jd"] != jd or old["title"] != title.strip()
                       or old["url"] != url or prior_metadata != metadata)
            if changed:
                version = old["jd_version"] + 1
                protected = old["phase"] == "submitted" or old["run_status"] == "submission_uncertain"
                db.execute("UPDATE jobs SET title=?,url=?,location=?,country=?,employment=?,arrangement=?,jd_version=?,"
                           "last_seen_at=?,phase=?,run_status=?,reason=? WHERE id=?",
                           (title.strip(), url, str(metadata.get("location") or ""),
                            str(metadata.get("country") or "unknown"), str(metadata.get("employment") or "unknown"),
                            str(metadata.get("arrangement") or "unknown"), version, now,
                            old["phase"] if protected else "new",
                            "submission_uncertain" if protected else "idle",
                            "Current listing description updated by discovery." if not protected else old["reason"], job_id))
                db.execute("INSERT INTO job_versions(job_id,version,jd,sha256,metadata,source,observed_at) VALUES(?,?,?,?,?,?,?)",
                           (job_id, version, jd, checksum, json.dumps(metadata, ensure_ascii=False), url, now))
        else:
            job_id, version = uuid.uuid4().hex, 1
            db.execute("INSERT INTO jobs(id,company_id,external_key,requisition_id,url,title,location,country,employment,arrangement,"
                       "jd_version,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, pipeline["company_id"], external_key, requisition, url, title.strip(),
                        str(metadata.get("location") or ""), str(metadata.get("country") or "unknown"),
                        str(metadata.get("employment") or "unknown"), str(metadata.get("arrangement") or "unknown"),
                        version, now, now))
            db.execute("INSERT INTO job_versions(job_id,version,jd,sha256,metadata,source,observed_at) VALUES(?,?,?,?,?,?,?)",
                       (job_id, version, jd, checksum, json.dumps(metadata, ensure_ascii=False), url, now))
        if not changed:
            db.execute("UPDATE jobs SET last_seen_at=? WHERE id=?", (now, job_id))
        selected = bool(payload.get("selected"))
        reason = str(payload.get("selection_reason") or "")[:4000]
        membership = db.execute("SELECT * FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                (worker["pipeline_id"], job_id)).fetchone()
        if membership:
            db.execute("UPDATE pipeline_jobs SET selected=?,excluded_reason=?,discovered_at=? WHERE pipeline_id=? AND job_id=?",
                       (int(selected), "" if selected else reason or "Not selected by discovery agent.", now,
                        worker["pipeline_id"], job_id))
        else:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,excluded_reason,agent_status) "
                       "VALUES(?,?,?,?,?,?)",
                       (worker["pipeline_id"], job_id, now, int(selected),
                        "" if selected else reason or "Not selected by discovery agent.", "selected" if selected else "excluded"))
        created_evaluator = None
        job = db.execute("SELECT phase,run_status,jd_version FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not selected:
            db.execute("UPDATE pipeline_jobs SET agent_status='excluded' WHERE pipeline_id=? AND job_id=? "
                       "AND agent_status NOT IN ('submitted','uncertain')",
                       (worker["pipeline_id"], job_id))
        if selected:
            if job["phase"] == "submitted":
                db.execute("UPDATE pipeline_jobs SET agent_status='submitted' WHERE pipeline_id=? AND job_id=?",
                           (worker["pipeline_id"], job_id))
            elif job["run_status"] == "submission_uncertain":
                db.execute("UPDATE pipeline_jobs SET agent_status='uncertain' WHERE pipeline_id=? AND job_id=?",
                           (worker["pipeline_id"], job_id))
            else:
                if changed:
                    db.execute("UPDATE pipeline_jobs SET agent_decision='',agent_reason='' WHERE pipeline_id=? AND job_id=?",
                               (worker["pipeline_id"], job_id))
                current_profile = self.profile_version(db)
                current = db.execute("SELECT id FROM agent_workers WHERE pipeline_id=? AND job_id=? AND role='evaluator' "
                                     "AND jd_version=? AND profile_version=? "
                                     "AND state IN ('queued','running','waiting_question','waiting_approval','completed','discarded') "
                                     "ORDER BY created_at DESC LIMIT 1",
                                     (worker["pipeline_id"], job_id, job["jd_version"], current_profile)).fetchone()
                if not current:
                    db.execute("UPDATE pipeline_jobs SET agent_decision='',agent_reason='',agent_status='selected' "
                               "WHERE pipeline_id=? AND job_id=? AND agent_status NOT IN ('submitted','uncertain')",
                               (worker["pipeline_id"], job_id))
                    db.execute("UPDATE agent_workers SET state='interrupted',error='Snapshot changed; explicit fresh evaluation is queued.',"
                               "completed_at=?,capability_sha256='',capability_expires_at=NULL,updated_at=? "
                               "WHERE pipeline_id=? AND job_id=? AND role='evaluator' AND state IN ('queued','waiting_question','waiting_approval') "
                               "AND (jd_version!=? OR profile_version!=?)",
                               (now, now, worker["pipeline_id"], job_id, job["jd_version"], current_profile))
                    cached = self._agent_reuse_cached_evaluation(db, worker["pipeline_id"], job_id,
                                                                 job["jd_version"])
                    created_evaluator = (cached["worker_id"] if cached else
                                         self._agent_insert_worker(db, worker["pipeline_id"], "evaluator", job_id=job_id))
                db.execute("UPDATE pipeline_jobs SET agent_status='selected' WHERE pipeline_id=? AND job_id=? "
                           "AND agent_status NOT IN ('submitted','uncertain')",
                           (worker["pipeline_id"], job_id))
        self.event(db, "agent.listing_recorded", {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
                   "job_id": job_id, "requisition_id": requisition, "created": created, "changed": changed,
                   "selected": selected, "selection_reason": reason, "observation": selection_observation,
                   "description_sha256": checksum}, job_id, "agent_discovery")
        return {"job_id": job_id, "created": created, "changed": changed, "selected": selected,
                "selection_reason": reason, "jd_version": job["jd_version"],
                "evaluator_worker_id": created_evaluator}

    def _agent_capture_ref(self, db, worker):
        session_id = worker["session_id"]
        if not session_id:
            return None
        row = db.execute("SELECT id,session_id,payload FROM agent_runtime_events WHERE worker_id=? AND session_id=? "
                         "AND (tool_name='cua_repl.js' OR tool_name LIKE 'playwright.%') AND success=1 ORDER BY rowid DESC LIMIT 1",
                         (worker["id"], session_id)).fetchone()
        if not row:
            return None
        payload = self._agent_decode(row["payload"], {})
        transport = payload.get("transport_event") if isinstance(payload, dict) else None
        item = transport.get("item") if isinstance(transport, dict) else None
        result = item.get("result") if isinstance(item, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        if (payload.get("transport_event_truncated") or not isinstance(item, dict)
                or item.get("type") != "mcp_tool_call"
                or not (item.get("server") == "cua_repl" and item.get("tool") == "js" or item.get("server") == "playwright")
                or not isinstance(content, list)):
            return None
        return {"event_id": row["id"], "session_id": row["session_id"], "content": content}

    @staticmethod
    def _agent_capture_description(capture, pointer):
        if not capture:
            raise Conflict("Capture a complete job description from a successful browser result in this worker's current session first.")
        if not isinstance(pointer, dict):
            raise ValueError("A description pointer must identify one exact text block and unique start/end markers.")
        block_index = pointer.get("text_block")
        if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 0:
            raise ValueError("description_pointer.text_block must be a nonnegative text-block index.")
        start_marker, end_marker = pointer.get("start_marker"), pointer.get("end_marker")
        if (not isinstance(start_marker, str) or not start_marker.strip() or len(start_marker) > 512
                or not isinstance(end_marker, str) or not end_marker.strip() or len(end_marker) > 512):
            raise ValueError("Description markers must be non-empty exact strings of at most 512 characters.")
        content = capture["content"]
        if block_index >= len(content):
            raise ValueError("The description text-block index is outside the captured CUA result.")
        block = content[block_index]
        text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
        if not isinstance(text, str):
            raise ValueError("The selected CUA result block does not contain text.")
        if text.count(start_marker) != 1 or text.count(end_marker) != 1:
            raise Conflict("Description markers must each occur exactly once in the captured result block.")
        start = text.index(start_marker)
        end = text.index(end_marker)
        if end < start or end + len(end_marker) - start > 200_000:
            raise Conflict("The captured description markers are reversed or exceed the supported size.")
        description = text[start:end + len(end_marker)]
        if not description.strip():
            raise Conflict("The captured description is empty.")
        return description

    def _agent_capture_listing_batch(self, db, worker, payload):
        if worker["role"] != "discovery":
            raise Conflict("Only company discovery may capture a listing batch.")
        listings = payload.get("listings")
        if not isinstance(listings, list) or not 1 <= len(listings) <= 10:
            raise ValueError("A capture batch must contain between one and ten listings.")
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Capture batch must contain JSON-compatible values.") from error
        if len(serialized) > 1024 * 1024:
            raise ValueError("A capture batch cannot exceed 1 MiB of serialized UTF-8 data.")
        capture = self._agent_capture_ref(db, worker)
        recorded = []
        for index, item in enumerate(listings):
            if not isinstance(item, dict):
                raise ValueError(f"Listing at index {index} must be an object.")
            if (not isinstance(item.get("title"), str) or not item["title"].strip()
                    or not isinstance(item.get("url"), str) or not item["url"].strip()
                    or not isinstance(item.get("selected"), bool)):
                raise ValueError(f"Listing at index {index} needs a title, exact URL, and boolean selection.")
            jd = self._agent_capture_description(capture, item.get("description_pointer"))
            normalized = {key: item[key] for key in ("requisition_id", "url", "title", "metadata",
                                                        "selected", "selection_reason", "observation")
                          if key in item}
            normalized["jd"] = jd
            result = self._agent_record_listing(db, worker, normalized)
            version = db.execute("SELECT sha256 FROM job_versions WHERE job_id=? AND version=?",
                                 (result["job_id"], result["jd_version"])).fetchone()
            capture_ref = uuid.uuid4().hex
            observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
            db.execute("INSERT INTO agent_listing_captures(capture_ref,pipeline_id,job_id,worker_id,event_id,session_id,"
                       "jd_version,description_sha256,observation,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(pipeline_id,job_id,event_id) DO UPDATE SET capture_ref=excluded.capture_ref,"
                       "session_id=excluded.session_id,jd_version=excluded.jd_version,"
                       "description_sha256=excluded.description_sha256,observation=excluded.observation,created_at=excluded.created_at",
                       (capture_ref, worker["pipeline_id"], result["job_id"], worker["id"], capture["event_id"],
                        capture["session_id"], result["jd_version"], version["sha256"] if version else "",
                        json.dumps(observation, ensure_ascii=False), timestamp()))
            self.event(db, "agent.listing_capture_linked", {"pipeline_id": worker["pipeline_id"],
                       "worker_id": worker["id"], "job_id": result["job_id"],
                       "capture_ref": capture_ref, "source_event_id": capture["event_id"],
                       "jd_version": result["jd_version"],
                       "description_sha256": version["sha256"] if version else ""},
                       result["job_id"], "agent_discovery")
            recorded.append({"index": index, "job_id": result["job_id"], "capture_ref": capture_ref,
                             "source_event_id": capture["event_id"], "jd_version": result["jd_version"],
                             "description_sha256": version["sha256"] if version else None,
                             "selected": result["selected"], "created": result["created"],
                             "changed": result["changed"],
                             "evaluator_worker_id": result["evaluator_worker_id"]})
        self._agent_refresh_pipeline(db, worker["pipeline_id"])
        return {"recorded": recorded, "context_revision": self._agent_context_revision(db)}

    def _agent_record_listing_batch(self, db, worker, payload):
        if worker["role"] != "discovery":
            raise Conflict("Only company discovery may record listing batches.")
        listings = payload.get("listings")
        if not isinstance(listings, list) or not 1 <= len(listings) <= 10:
            raise ValueError("A listing batch must contain between one and ten listings.")
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Listing batch must contain JSON-compatible values.") from error
        if len(serialized) > 1024 * 1024:
            raise ValueError("A listing batch cannot exceed 1 MiB of serialized UTF-8 data.")
        for index, listing in enumerate(listings):
            if not isinstance(listing, dict):
                raise ValueError(f"Listing at index {index} must be an object.")
            if (not isinstance(listing.get("title"), str) or not listing["title"].strip()
                    or not isinstance(listing.get("jd"), str) or not listing["jd"].strip()
                    or not isinstance(listing.get("url"), str) or not listing["url"].strip()
                    or not isinstance(listing.get("selected"), bool)):
                raise ValueError(f"Listing at index {index} needs title, exact URL, full description, and a boolean selection.")
            if len(listing["jd"]) > 200_000 or len(listing["url"]) > 4096:
                raise ValueError(f"Listing at index {index} exceeds the supported URL or description size.")

        capture_ref = self._agent_capture_ref(db, worker)
        recorded = []
        for index, listing in enumerate(listings):
            result = self._agent_record_listing(db, worker, listing)
            job = db.execute("SELECT requisition_id,url FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
            version = db.execute("SELECT sha256 FROM job_versions WHERE job_id=? AND version=?",
                                 (result["job_id"], result["jd_version"])).fetchone()
            if capture_ref:
                observation = listing.get("observation") if isinstance(listing.get("observation"), dict) else {}
                db.execute("INSERT OR IGNORE INTO agent_listing_captures"
                           "(pipeline_id,job_id,worker_id,event_id,session_id,observation,created_at) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (worker["pipeline_id"], result["job_id"], worker["id"], capture_ref["event_id"],
                            capture_ref["session_id"], json.dumps(observation, ensure_ascii=False), timestamp()))
                self.event(db, "agent.listing_capture_linked",
                           {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
                            "job_id": result["job_id"], "capture_ref": capture_ref},
                           result["job_id"], "agent_discovery")
            recorded.append({"index": index, "job_id": result["job_id"], "created": result["created"],
                             "changed": result["changed"], "selected": result["selected"],
                             "jd_version": result["jd_version"],
                             "description_sha256": version["sha256"] if version else None,
                             "capture_ref": capture_ref,
                             "evaluator_worker_id": result["evaluator_worker_id"]})
        self._agent_refresh_pipeline(db, worker["pipeline_id"])
        return {"recorded": recorded, "context_revision": self._agent_context_revision(db)}

    @staticmethod
    def _agent_source_sections(source_id, source_sha256, content):
        """Index every Markdown section with a compact, exact excerpt and stable handle."""
        content = str(content or "")
        lines = content.splitlines(keepends=True)
        starts = []
        offset = 0
        for line in lines:
            match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
            if match:
                starts.append((offset, len(match.group(1)), match.group(2).strip()))
            offset += len(line)
        if not starts:
            starts = [(0, 0, "Document")]
        elif starts[0][0] > 0 and content[:starts[0][0]].strip():
            starts.insert(0, (0, 0, "Preamble"))

        sections = []
        heading_stack = []
        for index, (start, level, title) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(content)
            chunk = content[start:end]
            if level:
                heading_stack = [entry for entry in heading_stack if entry[0] < level]
                heading_stack.append((level, title))
                heading_path = [entry[1] for entry in heading_stack]
            else:
                heading_path = [title]
            excerpt = chunk[:120]
            section_ref = f"{source_id}#section:{source_sha256[:12]}:{index}"
            sections.append({"ref": section_ref, "heading": title,
                             "path": " / ".join(heading_path), "excerpt": excerpt,
                             "_start": start, "_end": end})
        return sections

    def _agent_profile_context(self, db):
        """Compact fact index with source handles; never inline source-document bodies."""
        docs = []
        for row in db.execute("SELECT id,source_path,sha256,content,imported_at FROM source_documents ORDER BY id"):
            sections = self._agent_source_sections(row["id"], row["sha256"], row["content"])
            docs.append({"id": row["id"], "name": Path(row["source_path"]).name, "sha256": row["sha256"],
                         "imported_at": row["imported_at"],
                         "sections": [{key: section[key] for key in ("ref", "heading")}
                                      for section in sections]})
        technologies = {row["key"]: {"name": row["name"], "answer": row["answer"],
                                      "context": row["context"], "source_ref": "technology:" + row["key"]}
                        for row in db.execute("SELECT key,name,answer,context,source FROM technologies ORDER BY key")}
        # Canonical names and exact depth remain inline. Verbose provenance,
        # aliases and historical question bodies are available by scoped handle
        # rather than replayed on every inference request for every job.
        answered_questions = []
        for row in db.execute("SELECT q.id,q.key,q.technology_key,q.prompt,q.answer,q.created_at,q.resolved_at,"
                              "GROUP_CONCAT(qj.job_id) AS linked_job_ids FROM questions q "
                              "LEFT JOIN question_jobs qj ON qj.question_id=q.id WHERE q.status='answered' "
                              "GROUP BY q.id ORDER BY q.resolved_at,q.id"):
            item = {"id": row["id"], "ref": "answer:legacy:" + row["id"], "question": row["prompt"],
                    "source": "user_answered_legacy_question",
                    "technology_key": row["technology_key"]}
            answered_questions.append(item)
        for row in db.execute("SELECT i.id,i.question,i.response,i.provenance,i.job_id,i.pipeline_id,"
                              "i.created_at,i.resolved_at,i.context,j.title AS job_title FROM agent_inbox i "
                              "LEFT JOIN jobs j ON j.id=i.job_id WHERE i.kind='question' AND i.state='answered' "
                              "ORDER BY i.resolved_at,i.id"):
            item = dict(row)
            scope = self._agent_decode(item.get("context"), {})
            # Only facts explicitly assigned applicant scope can cross jobs.
            # Form answers, operational sign-in and unknown-scope history stay
            # out of the reusable applicant index without text heuristics.
            if not isinstance(scope, dict) or scope.get("fact_scope") != "applicant":
                continue
            answered_questions.append({"id": row["id"], "ref": "answer:agent:" + row["id"],
                                       "source": "answered_agent_question",
                                       "question": row["question"], "fact_key": scope.get("fact_key")})
        return {"profile_version": self.profile_version(db), "source_documents": docs,
                "confirmed_knowledge": technologies,
                "technology_aliases": {"ref": "knowledge_aliases", "retrieve_if_needed": True},
                "answered_questions": answered_questions,
                "templates": {"renderer": "existing Markdown/PDF renderer",
                              "verified_resume_bases": [dict(row) for row in db.execute(
                                  "SELECT 'resume_base:'||b.id AS ref,b.family,b.summary,a.sha256 FROM agent_resume_bases b "
                                  "JOIN agent_artifacts a ON a.id=b.artifact_id WHERE b.profile_version=? AND a.status='ready' "
                                  "ORDER BY b.created_at DESC LIMIT 20", (self.profile_version(db),))],
                              "reference_documents": [{"id": doc["id"], "name": doc["name"],
                                                       "sha256": doc["sha256"]} for doc in docs]}}

    def _agent_retrieve_sources(self, db, worker, payload):
        if worker["role"] == "discovery":
            raise Conflict("Company discovery cannot retrieve applicant source documents.")
        source_ids = payload.get("source_ids")
        if (not isinstance(source_ids, list) or not source_ids or len(source_ids) > 16
                or any(not isinstance(value, str) or not value or len(value) > 512 for value in source_ids)):
            raise ValueError("Retrieve one to sixteen source-document or section handles from the applicant index.")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Source document IDs must be unique.")
        sources = []
        total_bytes = 0
        for source_ref in source_ids:
            if source_ref == "knowledge_aliases":
                sources.append({"id": source_ref, "aliases": {row["alias"]: row["technology_key"]
                    for row in db.execute("SELECT alias,technology_key FROM technology_aliases ORDER BY alias")}})
                continue
            if source_ref.startswith("technology:"):
                row = db.execute("SELECT key,name,answer,context,source FROM technologies WHERE key=?",
                                 (source_ref.removeprefix("technology:"),)).fetchone()
                if not row:
                    raise KeyError(source_ref)
                sources.append({"id": source_ref, **dict(row)})
                continue
            if source_ref.startswith("answer:"):
                if source_ref.startswith("answer:legacy:"):
                    row = db.execute("SELECT * FROM questions WHERE id=? AND status='answered'",
                                     (source_ref.removeprefix("answer:legacy:"),)).fetchone()
                elif source_ref.startswith("answer:agent:"):
                    row = db.execute("SELECT * FROM agent_inbox WHERE id=? AND kind='question' AND state='answered' "
                                     "AND (json_extract(context,'$.fact_scope')='applicant' OR worker_id=?)",
                                     (source_ref.removeprefix("answer:agent:"), worker["id"])).fetchone()
                else:
                    row = None
                if not row:
                    raise KeyError(source_ref)
                sources.append({"id": source_ref, "record": dict(row)})
                continue
            if source_ref.startswith("resume_base:"):
                row = db.execute("SELECT a.path,a.sha256,a.status,b.family,b.summary,b.profile_version FROM agent_resume_bases b "
                                 "JOIN agent_artifacts a ON a.id=b.artifact_id WHERE b.id=?",
                                 (source_ref.removeprefix("resume_base:"),)).fetchone()
                if not row or row["status"] != "ready" or row["profile_version"] != self.profile_version(db):
                    raise Conflict("This resume base is unavailable or predates the current applicant profile.")
                path = (self.root / row["path"]).resolve()
                area = (self.root / "artifacts/agent").resolve()
                if area not in path.parents or not path.is_file() or digest(path.read_text(encoding="utf-8")) != row["sha256"]:
                    raise Conflict("The verified resume base no longer matches its artifact.")
                content = path.read_text(encoding="utf-8")
                total_bytes += len(content.encode("utf-8"))
                if total_bytes > 600_000:
                    raise ValueError("Requested source material exceeds the retrieval size limit.")
                sources.append({"id": source_ref, "content": content, "sha256": row["sha256"],
                                "family": row["family"], "summary": row["summary"], "profile_version": row["profile_version"]})
                continue
            source_id = source_ref
            section_digest = None
            section_ordinal = None
            match = re.fullmatch(r"(.+)#section:([a-f0-9]{12}):(\d+)", source_ref)
            if match:
                source_id, section_digest, ordinal_text = match.groups()
                section_ordinal = int(ordinal_text)
            row = db.execute("SELECT id,source_path,sha256,content,imported_at FROM source_documents WHERE id=?",
                             (source_id,)).fetchone()
            if not row:
                raise KeyError(source_ref)
            source = dict(row)
            if section_digest is not None:
                if not row["sha256"].startswith(section_digest):
                    raise Conflict("The indexed source section is stale; request its current handle.")
                sections = self._agent_source_sections(row["id"], row["sha256"], row["content"])
                if section_ordinal is None or section_ordinal >= len(sections):
                    raise KeyError(source_ref)
                section = sections[section_ordinal]
                starts = [value for value in sections]
                start = 0 if section_ordinal == 0 else starts[section_ordinal]["_start"]
                end = (len(row["content"]) if section_ordinal + 1 == len(starts)
                       else starts[section_ordinal + 1]["_start"])
                text = row["content"][start:end]
                total_bytes += len(text.encode("utf-8"))
                source = {"id": source_ref, "source_id": row["id"], "source_path": row["source_path"],
                          "sha256": row["sha256"], "imported_at": row["imported_at"],
                          "content_sha256": digest(text),
                          "section": {key: section[key] for key in ("heading", "path")},
                          "content": text}
            else:
                total_bytes += len(row["content"].encode("utf-8"))
            if total_bytes > 600_000:
                raise ValueError("Requested source material exceeds the retrieval size limit.")
            sources.append(source)
        if len(json.dumps(sources, ensure_ascii=False).encode("utf-8")) > 600_000:
            raise ValueError("Requested source material exceeds the retrieval size limit.")
        return {"sources": sources, "profile_version": self.profile_version(db)}

    def _agent_prepared_documents(self, db, worker, current_jd_version, current_profile_version):
        """Return only requested, ready, snapshot-current Markdown artifacts to a submission parent."""
        if worker["role"] != "submission" or not worker["job_id"]:
            return []
        context = self._agent_decode(worker["context"], {})
        requested = set(context.get("requested_artifacts") or [])
        if not requested:
            return []
        parent_context = context
        package_id = parent_context.get("document_package_id")
        if not package_id:
            return []
        latest = self._agent_ready_artifacts(db, worker["pipeline_id"], worker["job_id"],
                                             current_jd_version, current_profile_version)
        latest = {kind: row for kind, row in latest.items() if kind in requested}
        if set(latest) != requested:
            return []
        prepared = []
        for kind in sorted(requested):
            row = latest[kind]
            path = (self.root / row["path"]).resolve()
            package_root = (self.root / "artifacts" / "agent" / str(package_id)).resolve()
            if package_root not in path.parents or not path.is_file():
                return []
            try:
                markdown = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return []
            if digest(markdown) != row["sha256"]:
                return []
            pdf_path = (self.root / row.get("pdf_path", "")).resolve()
            if (not row.get("pdf_path") or package_root not in pdf_path.parents or not pdf_path.is_file()
                    or hashlib.sha256(pdf_path.read_bytes()).hexdigest() != row.get("pdf_sha256")):
                return []
            prepared.append({"kind": kind, "filename": row["filename"], "markdown": markdown,
                             "sha256": row["sha256"], "package_id": package_id,
                             "version": row["version"], "jd_version": row["jd_version"],
                             "profile_version": row["profile_version"],
                             "upload_path": str(pdf_path), "upload_filename": pdf_path.name,
                             "upload_sha256": row["pdf_sha256"],
                             "preview_paths": self._agent_decode(row.get("preview_paths"), [])})
        return prepared

    def _agent_ready_artifacts(self, db, pipeline_id, job_id, jd_version, profile_version):
        rows = db.execute("SELECT a.*,w.parent_id FROM agent_artifacts a JOIN agent_workers w ON w.id=a.worker_id "
                          "WHERE a.pipeline_id=? AND a.job_id=? AND a.jd_version=? AND a.profile_version=? "
                          "AND a.status='ready' ORDER BY a.kind,a.version DESC",
                          (pipeline_id, job_id, jd_version, profile_version)).fetchall()
        latest = {}
        for row in rows:
            if row["kind"] not in latest:
                item = dict(row)
                parent = db.execute("SELECT context FROM agent_workers WHERE id=?", (row["parent_id"],)).fetchone()
                parent_context = self._agent_decode(parent["context"] if parent else "{}", {})
                item["package_id"] = parent_context.get("document_package_id")
                latest[row["kind"]] = item
        return latest

    @staticmethod
    def _agent_artifact_snapshot(artifacts):
        return [{key: item.get(key) for key in ("kind", "filename", "sha256", "version", "package_id",
                                                "pdf_sha256")}
                for _, item in sorted(artifacts.items())]

    @staticmethod
    def _agent_render_root(package_root,pdf_path):
        legacy=(package_root/'rendered').resolve();proposed=pdf_path.parent
        if proposed==legacy:return legacy
        if (proposed.name=='rendered' and proposed.parent.parent.name=='renders'
                and proposed.parent.parent.parent==package_root
                and re.fullmatch(r'[0-9a-f]{64}',proposed.parent.name)):
            return proposed
        raise Conflict('The rendered artifact is outside the canonical package path.')

    def _agent_review_artifacts(self, db, artifacts):
        """Build a review-only presentation separate from the approval snapshot."""
        result = []
        source_names = {"resume": "resume.md", "cover_letter": "cover-letter.md"}
        pdf_names = {"resume": "resume.pdf", "cover_letter": "cover-letter.pdf"}
        for kind, row in sorted(artifacts.items()):
            artifact_id = row.get("id")
            package_id = row.get("package_id")
            if not artifact_id or not package_id or kind not in source_names:
                raise Conflict("A current ready artifact is missing its host-issued identity.")
            package_root = (self.root / "artifacts" / "agent" / str(package_id)).resolve()
            if self.root not in package_root.parents:
                raise Conflict("The review artifact package escaped the application workspace.")
            source_path = (self.root / str(row.get("path") or "")).resolve()
            pdf_path = (self.root / str(row.get("pdf_path") or "")).resolve()
            rendered_root=self._agent_render_root(package_root,pdf_path)
            if (package_root not in source_path.parents or not source_path.is_file()
                    or source_path.name != source_names[kind]
                    or digest(source_path.read_text(encoding="utf-8")) != row.get("sha256")):
                raise Conflict("A current Markdown review artifact is missing or has changed.")
            if (pdf_path.parent != rendered_root or pdf_path.name != pdf_names[kind] or not pdf_path.is_file()
                    or hashlib.sha256(pdf_path.read_bytes()).hexdigest() != row.get("pdf_sha256")):
                raise Conflict("A current rendered PDF review artifact is missing or has changed.")
            previews = []
            for index, preview in enumerate(self._agent_decode(row.get("preview_paths"), [])):
                if not isinstance(preview, dict):
                    raise Conflict("A rendered preview record is malformed.")
                preview_path = (self.root / str(preview.get("path") or "")).resolve()
                expected_hash = preview.get("sha256")
                if (preview_path.parent != rendered_root or preview_path.suffix.lower() not in
                        {".png", ".jpg", ".jpeg", ".webp"} or not preview_path.is_file()
                        or hashlib.sha256(preview_path.read_bytes()).hexdigest() != expected_hash):
                    raise Conflict("A current rendered preview is missing or has changed.")
                previews.append({"index": index, "path": preview_path.relative_to(self.root).as_posix(),
                                 "filename": preview_path.name, "sha256": expected_hash,
                                 "url": f"/api/agent-artifacts/{artifact_id}?variant=preview&index={index}"})
            result.append({
                "id": artifact_id, "kind": kind, "filename": row["filename"],
                "version": row["version"], "package_id": package_id,
                "markdown": source_path.read_text(encoding="utf-8"), "sha256": row["sha256"],
                "source_path": source_path.relative_to(self.root).as_posix(),
                "source_url": f"/api/agent-artifacts/{artifact_id}?variant=markdown",
                "upload": {"filename": pdf_path.name, "path": pdf_path.relative_to(self.root).as_posix(),
                           "sha256": row["pdf_sha256"],
                           "url": f"/api/agent-artifacts/{artifact_id}?variant=pdf"},
                "previews": previews,
            })
        return result

    def _agent_current_approved_actions(self, db, worker):
        if worker["role"] != "submission":
            return []
        current_jd = worker["jd_version"]
        current_profile = self.profile_version(db)
        result = []
        for row in db.execute("SELECT id,kind,action,state,context,provenance,resolved_at FROM agent_inbox "
                              "WHERE worker_id=? AND kind IN ('approval','first_application_review') "
                              "AND state IN ('approved','rejected') ORDER BY resolved_at,id", (worker["id"],)):
            item = dict(row)
            item["context"] = self._agent_decode(item["context"], {})
            snapshot = item["context"]
            if (snapshot.get("jd_version") == current_jd
                    and snapshot.get("profile_version") == current_profile):
                result.append(item)
        return result

    @staticmethod
    def _agent_discovery_checkpoint_progress(context):
        if not isinstance(context, dict):
            return {}
        progress = {}
        for key in AGENT_DISCOVERY_PROGRESS_FIELDS:
            if key not in context:
                continue
            value = context[key]
            try:
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            # Checkpoint notes are agent-authored metadata. Keep only bounded,
            # structured fields needed to compare page/filter progress.
            if len(encoded.encode("utf-8")) <= 50000:
                progress[key] = json.loads(encoded)
        return progress

    def _agent_discovery_progress_snapshot(self, db, pipeline_id, context, coverage=None):
        listings = [
            {"job_id": row["job_id"], "url": row["url"], "requisition_id": row["requisition_id"],
             "jd_version": row["jd_version"], "selected": bool(row["selected"])}
            for row in db.execute(
                "SELECT pj.job_id,j.url,j.requisition_id,j.jd_version,pj.selected "
                "FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id "
                "WHERE pj.pipeline_id=? ORDER BY pj.job_id", (pipeline_id,))
        ]
        return {"listings": listings,
                "checkpoint_progress": self._agent_discovery_checkpoint_progress(context),
                "coverage_progress": self._agent_discovery_checkpoint_progress(coverage)}

    @staticmethod
    def _agent_discovery_has_progress(baseline, current):
        if not isinstance(baseline, dict):
            baseline = {}
        if baseline.get("listings", []) != current.get("listings", []):
            return True
        for key in ("checkpoint_progress", "coverage_progress"):
            before = dict(baseline.get(key)) if isinstance(baseline.get(key), dict) else {}
            after = dict(current.get(key)) if isinstance(current.get(key), dict) else {}
            # A tab id change alone is not progress. Tab URLs, page cursors,
            # reviewed requisitions, search paths and filters are meaningful
            # only when they actually advance or change.
            before.pop("tab_id", None)
            after.pop("tab_id", None)
            if before != after:
                return True
        return False

    def _agent_discovery_continuation(self, db, worker, context, coverage, test_only):
        if not worker["checkpoint"].strip():
            return {"state": "interrupted", "resume_mode": "same_session",
                    "reason": "missing_checkpoint", "error":
                    "Discovery reported incomplete coverage without a saved checkpoint; explicit resume is required."}
        if not worker["session_id"]:
            return {"state": "interrupted", "resume_mode": "new_session",
                    "reason": "missing_saved_session", "error":
                    "Discovery made progress but has no saved runtime session; explicit resume is required."}
        tab_id = context.get("tab_id")
        tab_url = context.get("tab_url") or context.get("current_url")
        if (tab_id is None or isinstance(tab_id, bool) or not str(tab_id).strip()
                or not tab_url or not self._agent_url_allowed(tab_url, test_only=test_only)):
            return {"state": "interrupted", "resume_mode": "same_session",
                    "reason": "missing_owned_tab", "error":
                    "Discovery made progress but its checkpoint lacks a valid owned tab and URL; explicit resume is required."}
        prior_context = context.get("discovery_turn_baseline")
        baseline = prior_context if isinstance(prior_context, dict) else {}
        current = self._agent_discovery_progress_snapshot(db, worker["pipeline_id"], context, coverage)
        if not self._agent_discovery_has_progress(baseline, current):
            return {"state": "interrupted", "resume_mode": "same_session",
                    "reason": "no_durable_progress", "error":
                    "Discovery reported incomplete coverage without new listing or page progress; explicit resume is required."}
        return {"state": "queued", "resume_mode": "same_session",
                "reason": "durable_progress"}

    def agent_launch_context(self, worker_id):
        token = uuid.uuid4().hex + uuid.uuid4().hex
        expiry = time.time() + 3600
        with self.connect(write=True) as db:
            worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            pipeline = db.execute("SELECT p.*,c.name AS company_name,c.careers_url FROM pipelines p "
                                  "JOIN companies c ON c.id=p.company_id WHERE p.id=?", (worker["pipeline_id"],)).fetchone()
            if not pipeline or pipeline["mode"] != "agentic" or pipeline["stop_requested"] or worker["state"] != "running":
                raise Conflict("This agent worker is not in a launchable running state.")
            worker_record = dict(worker)
            if worker["role"] == "discovery":
                worker_context = self._agent_decode(worker["context"], {})
                previous_discovery = self._agent_decode(pipeline["discovery_payload"], {})
                worker_context["discovery_turn_baseline"] = self._agent_discovery_progress_snapshot(
                    db, worker["pipeline_id"], worker_context,
                    previous_discovery.get("coverage", {}) if isinstance(previous_discovery, dict) else {})
                worker_record["context"] = json.dumps(worker_context, ensure_ascii=False)
                db.execute("UPDATE agent_workers SET context=? WHERE id=?", (worker_record["context"], worker_id))
                worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            worker_view = self._agent_worker_record(worker_record)
            worker_view.pop("result", None)
            notes = worker_view.get("context", {})
            bounded, used = {}, 0
            priority = ["execution", "execution_handoff", "execution_change_deferred", "final_action_may_have_been_sent", "final_action_authorized_at", "tab_recovery", "question_reconsideration", "requested_artifacts",
                        "tab_id", "tab_url", "current_url", "page_cursor", "current_page", "remaining_paths"]
            for key in dict.fromkeys([*priority, *notes]):
                if key not in notes or key == "instruction_digest":
                    continue
                value = notes[key]
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                if used + size <= 8000:
                    bounded[key] = value
                    used += size
                else:
                    bounded[key] = {"omitted_from_launch": True, "retrieve": "get_context sections=['checkpoint']"}
            worker_view["context"] = bounded
            saved_context = self._agent_decode(worker["context"], {})
            from .efficiency_features import features
            from .worker_contracts import capture, load
            contract_id = saved_context.get('instruction_contract_id')
            contract = load(self.root, worker_id, contract_id) if contract_id else None
            backend = saved_context.get('execution', {}).get('browser_backend', 'cua')
            if not contract or contract['backend'] != backend:
                scoped = dict(worker);scoped['context'] = saved_context
                contract = capture(self.root, scoped, features(self.root))
            instruction_digest = contract['id']
            instructions_changed = bool(saved_context.get("instruction_digest") != instruction_digest)
            worker_view['context']['instruction_contract_id'] = contract['id']
            current_job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone() if worker["job_id"] else None
            current_snapshot = (worker["profile_version"] == self.profile_version(db)
                                and (not current_job or worker["jd_version"] == current_job["jd_version"]))
            same_saved_session = bool(worker["session_id"] and worker["resume_mode"] == "same_session"
                                      and isinstance(saved_context.get("last_context_revision"), int)
                                      and current_snapshot and not instructions_changed)
            context_request = {"sections": [] if same_saved_session else self._agent_initial_sections(worker["role"])}
            if same_saved_session:
                context_request["after_revision"] = saved_context["last_context_revision"]
            context_packet = self._agent_context_for_worker(db, worker, context_request)
            task_context = {
                "context_contract": "agent-context-v2",
                "instructions_changed": instructions_changed,
                "instruction_contract_id": contract['id'],
                "context_revision": context_packet["context_revision"],
                "runtime_controls": context_packet["runtime_controls"],
                **({"constraints": context_packet["constraints"]} if "constraints" in context_packet else {}),
                "updates": context_packet["updates"],
                **context_packet["sections"],
            }
            # One compact envelope per turn. The bridge's default get_context
            # response is controls plus event deltas, not another full bundle.
            task = {"worker": worker_view, "task_context": task_context}
            latest = self._agent_decode(db.execute("SELECT context FROM agent_workers WHERE id=?", (worker_id,)).fetchone()[0], {})
            latest["instruction_digest"] = instruction_digest
            latest['instruction_contract_id'] = contract['id']
            db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps(latest), worker_id))
            db.execute("UPDATE agent_workers SET capability_sha256=?,capability_expires_at=?,launch_generation=launch_generation+1,"
                       "updated_at=? WHERE id=? AND state='running'",
                       (hashlib.sha256(token.encode("utf-8")).hexdigest(), expiry, timestamp(), worker_id))
        return {"task": task, "capability": token}

    def _agent_isolated_form(self,db,worker):
        from .efficiency_features import features
        if worker['role']!='submission' or not features(self.root)['isolated_form_slots']:return False
        from .worker_settings import desired_execution
        execution=self._agent_decode(worker['context'],{}).get('execution',{})
        if not self._context_may_have_sent_final(self._agent_decode(worker['context'],{}),worker['checkpoint']):
            execution=desired_execution(self.root,db,worker)
        return execution.get('browser_backend')=='playwright'

    def _agent_capacity_wait(self, db, worker):
        """Backpressure is scheduling, never an assessment or a failed job.

        Retain a small ready queue so slow/broken browser work cannot spend the
        whole allowance assessing more jobs. Waiting work wakes through the
        existing completion/answer/resume scheduler, without a model poll.
        """
        # The recovery owner is what releases the downstream queue. Holding it
        # behind that same queue deadlocks an otherwise authorized resume.
        # Role caps, pending approvals, quota and stop checks still apply.
        try:
            settings = json.loads((self.root / "config/workers.json").read_text(encoding="utf-8")).get("efficiency", {})
        except (OSError, ValueError, TypeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        writer_limit = settings.get("max_concurrent_form_writers", 3)
        if type(writer_limit) is not int or not 1 <= writer_limit <= 3:
            writer_limit = 3
        if worker["role"] == "submission" and not self._agent_isolated_form(db,worker):
            from .efficiency_features import features
            isolate=features(self.root)['isolated_form_slots']
            active_writers = db.execute("SELECT COUNT(*) FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                "WHERE p.company_id=(SELECT company_id FROM pipelines WHERE id=?) AND w.role='submission' "
                "AND (w.state='running' OR w.turn_active=1) AND w.id!=? "
                "AND (?=0 OR COALESCE(json_extract(w.context,'$.execution.browser_backend'),'cua')!='playwright')", (worker["pipeline_id"], worker["id"],int(isolate))).fetchone()[0]
            if active_writers >= writer_limit:
                return {"reason": "Waiting for this company's active application-form writer.",
                        "active_form_writers": active_writers, "limit": writer_limit}
        if db.execute("SELECT 1 FROM agent_browser_controls WHERE state='open' AND worker_id=?",
                      (worker["id"],)).fetchone():
            return None
        # Queued work outside a bounded trial must not deadlock its admitted
        # discovery/evaluations through ordinary upstream backlog throttling.
        # The company form-writer limit above and role caps still apply.
        from .trial_limits import read as read_trial
        trial = read_trial(db, worker["pipeline_id"])
        if trial and trial["state"] == "active":
            return None
        approved_limit = settings.get("max_approved_backlog", 6)
        discovery_limit = settings.get("max_discovery_backlog", 12)
        if type(approved_limit) is not int or not 3 <= approved_limit <= 100:
            approved_limit = 6
        if type(discovery_limit) is not int or not 3 <= discovery_limit <= 500:
            discovery_limit = 12
        backlog = db.execute("SELECT COUNT(*) FROM agent_workers WHERE pipeline_id=? AND role='submission' "
                             "AND state NOT IN ('completed','discarded','stopped','superseded')",
                             (worker["pipeline_id"],)).fetchone()[0]
        # A known global outage warrants one buffered wave, not an unlimited
        # series of otherwise valid offline evaluations.
        circuit = db.execute("SELECT 1 FROM agent_browser_controls WHERE scope_key='connector:cua_repl' "
                             "AND control_type='connector_circuit' AND state='open'").fetchone()
        from .worker_settings import read_settings
        if circuit and read_settings(self.root, db)["roles"]["submission"]["browser_backend"] == "cua":
            approved_limit = min(approved_limit, 3)
        if worker["role"] == "evaluator" and backlog >= approved_limit:
            return {"reason": "Approved applications are waiting for submission capacity.",
                    "approved_backlog": backlog, "limit": approved_limit}
        if worker["role"] == "discovery":
            pending = db.execute("SELECT COUNT(*) FROM agent_workers WHERE pipeline_id=? AND role='evaluator' "
                                 "AND state NOT IN ('completed','discarded','stopped','superseded')",
                                 (worker["pipeline_id"],)).fetchone()[0]
            if pending >= discovery_limit and backlog >= approved_limit:
                return {"reason": "Discovery is buffered behind evaluation and submission work.",
                        "evaluation_backlog": pending, "approved_backlog": backlog,
                        "limit": discovery_limit}
        return None

    def reserve_agent_worker(self, worker_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT w.*,p.company_id,p.stop_requested,p.status AS pipeline_status,p.mode "
                             "FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?",
                             (worker_id,)).fetchone()
            if not row:
                raise KeyError(worker_id)
            if row["state"] != "queued" or row["mode"] != "agentic" or row["stop_requested"]:
                return False
            if row["pipeline_status"] in {"stopped", "stopping", "completed"}:
                return False
            from .trial_limits import allows as trial_allows
            from .worker_settings import desired_execution
            trial_desired = desired_execution(self.root, db, row)
            if not trial_allows(db, row, trial_desired,
                    may_have_sent=self._context_may_have_sent_final(self._agent_decode(row["context"], {}), row["checkpoint"])):
                return False
            quota = self._agent_decode(db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()[0], {})
            if quota.get("paused"):
                return False
            if self._agent_capacity_wait(db, row):
                return False
            circuit = db.execute("SELECT state,worker_id FROM agent_browser_controls WHERE scope_key='connector:cua_repl' "
                                 "AND control_type='connector_circuit'").fetchone()
            from .worker_settings import desired_execution
            desired = desired_execution(self.root, db, row)
            prior_execution = self._agent_decode(row["context"], {}).get("execution", {})
            backend = (prior_execution if self._context_may_have_sent_final(self._agent_decode(row["context"], {}), row["checkpoint"]) else desired).get("browser_backend", "cua")
            if (backend == "cua" and circuit and circuit["state"] == "open" and row["role"] in {"discovery", "submission"}
                    and circuit["worker_id"] != worker_id):
                return False
            if row["role"] == "submission":
                company_auth = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? "
                                          "AND control_type='company_auth'",
                                          ("company:" + str(row["company_id"]),)).fetchone()
                if company_auth and company_auth["state"] == "open" and company_auth["worker_id"] != worker_id:
                    auth_owner = db.execute("SELECT state,turn_active FROM agent_workers WHERE id=?", (company_auth["worker_id"],)).fetchone()
                    recovery_preempts_idle_owner = (circuit and circuit["state"] == "open" and circuit["worker_id"] == worker_id
                        and auth_owner and not auth_owner["turn_active"] and auth_owner["state"] in {"queued", "waiting_browser"}
                        and not self._agent_pending_inbox_wait(db, company_auth["worker_id"]))
                    if not self._agent_auth_owner_unavailable(db, company_auth) and not recovery_preempts_idle_owner and not self._agent_isolated_form(db,row):
                        return False
            if row["role"] == "discovery":
                busy = db.execute("SELECT 1 FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                  "WHERE p.company_id=? AND p.mode='agentic' AND w.role='discovery' "
                                  "AND (w.state='running' OR w.turn_active=1) AND w.id!=? LIMIT 1",
                                  (row["company_id"], worker_id)).fetchone()
                if busy:
                    return False
            elif row["role"] in {"evaluator", "submission"}:
                cap = 3
                states = "('running')" if row["role"] == "evaluator" else "('running','waiting_child')"
                active = db.execute(f"SELECT COUNT(*) FROM agent_workers WHERE pipeline_id=? AND role=? "
                                    f"AND (state IN {states} OR turn_active=1) AND id!=?",
                                    (row["pipeline_id"], row["role"], worker_id)).fetchone()[0]
                if active >= cap:
                    return False
            elif row["role"] == "document":
                parent = db.execute("SELECT state,jd_version,profile_version FROM agent_workers WHERE id=? AND pipeline_id=? AND role='submission'",
                                    (row["parent_id"], row["pipeline_id"])).fetchone()
                if (not parent or parent["state"] != "waiting_child" or parent["jd_version"] != row["jd_version"]
                        or parent["profile_version"] != row["profile_version"]):
                    return False
            if row["role"] == "submission" and company_auth and company_auth["state"] == "open" and company_auth["worker_id"] != worker_id and not self._agent_isolated_form(db,row):
                self._agent_transfer_auth_owner(db, company_auth, row)
            now = timestamp()
            from .worker_settings import pin_execution
            from .context_budget import rotate
            row = rotate(self, db, row)
            execution = pin_execution(self.root, db, row,
                may_have_sent=self._context_may_have_sent_final(self._agent_decode(row["context"], {}), row["checkpoint"]), now=now)
            db.execute("UPDATE agent_workers SET state='running',turn_active=1,started_at=COALESCE(started_at,?),"
                       "updated_at=?,error='',provider_error='' WHERE id=? AND state='queued'",
                       (now, now, worker_id))
            db.execute("UPDATE pipelines SET status='running',finished_at=NULL,updated_at=? WHERE id=? AND stop_requested=0",
                       (now, row["pipeline_id"]))
            self.event(db, "agent.worker_started", {"worker_id": worker_id, "pipeline_id": row["pipeline_id"],
                       "role": row["role"], "resume_mode": row["resume_mode"], "execution": execution}, row["job_id"], "agent_scheduler")
            return True

    def release_agent_worker_reservation(self, worker_id):
        """Return a pre-submission claim to queued only if no runtime has started."""
        with self.connect(write=True) as db:
            worker = db.execute("SELECT w.*,p.stop_requested,p.mode FROM agent_workers w "
                                "JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?", (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            if worker["mode"] != "agentic" or worker["state"] != "running":
                return False
            if db.execute("SELECT 1 FROM agent_runtime_events WHERE worker_id=? LIMIT 1", (worker_id,)).fetchone():
                raise Conflict("A worker with runtime events cannot have its claim released.")
            now = timestamp()
            state = "stopped" if worker["stop_requested"] else "queued"
            db.execute("UPDATE agent_workers SET state=?,turn_active=0,started_at=NULL,updated_at=?,capability_sha256='',"
                       "capability_expires_at=NULL WHERE id=? AND state='running'",
                       (state, now, worker_id))
            self.event(db, "agent.worker_reservation_released", {"worker_id": worker_id, "state": state},
                       worker["job_id"], "agent_scheduler")
            self._agent_refresh_pipeline(db, worker["pipeline_id"])
            return True

    def _agent_refresh_pipeline(self, db, pipeline_id):
        pipeline = db.execute("SELECT * FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
        if not pipeline or pipeline["mode"] != "agentic":
            return
        if pipeline["stop_requested"]:
            running = db.execute("SELECT 1 FROM agent_workers WHERE pipeline_id=? AND (state='running' OR turn_active=1) LIMIT 1",
                                 (pipeline_id,)).fetchone()
            status, stage, blockers = (("stopping", "stopping", ["Waiting for active workers to reach a safe stop boundary."])
                                       if running else ("stopped", "stopped", ["Pipeline stop was requested."]))
        else:
            workers = db.execute("SELECT role,state,turn_active,context,error,result FROM agent_workers WHERE pipeline_id=?",
                                 (pipeline_id,)).fetchall()
            # Superseded rows remain in the audit history but no longer
            # represent work that can affect the current pipeline snapshot.
            current_workers = [row for row in workers
                               if not self._agent_decode(row["context"], {}).get("snapshot_supersession")]
            active = any(row["state"] in {"queued", "running", "waiting_child"} or row["turn_active"]
                         for row in current_workers)
            pending_inbox = db.execute("SELECT 1 FROM agent_inbox WHERE pipeline_id=? AND state='pending' LIMIT 1",
                                       (pipeline_id,)).fetchone()
            counts = self._agent_pipeline_counts(db, pipeline_id)
            selected_terminal = counts["selected"] == counts["submitted"] + counts["discarded"]
            quota = self._agent_decode(db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()[0], {})
            if quota.get("paused"):
                status, stage, blockers = "quota_paused", "quota_paused", [quota.get("reason") or "Account quota pause."]
            elif active:
                status, stage, blockers = "running", "processing", []
                running_now = any(row["turn_active"] or row["state"] == "running" for row in current_workers)
                if not running_now and pending_inbox:
                    status, stage = "awaiting_user", "awaiting_user"
                    blockers = ["Applications need user responses; queued upstream work is buffered to conserve tokens."]
                queued_roles = [row["role"] for row in current_workers if row["state"] == "queued"]
                gate = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? AND control_type='company_auth' AND state='open'",
                                  ("company:" + pipeline["company_id"],)).fetchone()
                if (not running_now and queued_roles and all(role == "submission" for role in queued_roles)
                        and gate and not self._agent_auth_owner_unavailable(db, gate)):
                    owner = db.execute("SELECT state FROM agent_workers WHERE id=?", (gate["worker_id"],)).fetchone()
                    if owner and owner["state"] != "queued":
                        status, stage, blockers = "awaiting_user", "browser_gate", ["Submissions are waiting for the company sign-in owner to finish authentication."]
                circuit = db.execute("SELECT worker_id,reason FROM agent_browser_controls WHERE scope_key='connector:cua_repl' "
                                     "AND control_type='connector_circuit' AND state='open'").fetchone()
                if not running_now and circuit:
                    owner = db.execute("SELECT state FROM agent_workers WHERE id=?", (circuit["worker_id"],)).fetchone()
                    owner_state = owner["state"] if owner else "missing"
                    needs_decision = db.execute("SELECT 1 FROM agent_inbox WHERE worker_id=? AND state='pending'", (circuit["worker_id"],)).fetchone()
                    status, stage = (("awaiting_user", "browser_gate") if needs_decision
                                     else ("blocked", "browser_fault") if owner_state in {"failed", "completed", "discarded", "stopped", "missing", "uncertain"}
                                     else ("running", "browser_gate"))
                    blockers = ["Browser connector recovery is required; its owner is " + owner_state + ". "
                                + str(circuit["reason"] or "")]
            elif pipeline["discovery_status"] == "complete" and selected_terminal:
                status, stage, blockers = "completed", "completed", []
            elif pending_inbox:
                status, stage, blockers = "awaiting_user", "awaiting_user", ["One or more workers are waiting for a user response."]
            elif any(row["state"] == "waiting_browser" for row in current_workers):
                status, stage, blockers = "awaiting_user", "browser_gate", ["Waiting for shared authentication or connector recovery."]
            elif any(row["state"] == "interrupted" for row in current_workers):
                status, stage, blockers = "interrupted", "interrupted", ["One or more workers need explicit resume."]
            elif any(row["state"] in {"failed", "uncertain"} for row in current_workers):
                status, stage, blockers = "blocked", "needs_attention", []
                if any(row["state"] == "uncertain" for row in current_workers):
                    blockers.append("An application outcome is uncertain and must be reconciled before retrying.")
                failed_discovery = [row for row in current_workers
                                    if row["role"] == "discovery" and row["state"] == "failed"]
                for row in failed_discovery:
                    discovery_result = self._agent_decode(row["result"], {})
                    detail = row["error"] or (discovery_result.get("detail")
                                               if isinstance(discovery_result, dict) else "")
                    blockers.append("Discovery needs attention: " +
                                    (detail or "the worker reported a blocker without details."))
                if any(row["state"] == "failed" and row["role"] != "discovery"
                       for row in current_workers):
                    blockers.append("A worker failed; inspect its saved error before resuming.")
            else:
                status, stage, blockers = "queued", "processing", []
        now = timestamp()
        finished = now if status in {"completed", "stopped", "blocked"} else None
        db.execute("UPDATE pipelines SET status=?,stage=?,blockers=?,updated_at=?,finished_at=? WHERE id=?",
                   (status, stage, json.dumps(blockers, ensure_ascii=False), now, finished, pipeline_id))

    def request_agent_pipeline_stop(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT mode,status FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["mode"] != "agentic":
                raise Conflict("This is a legacy pipeline.")
            if row["status"] == "completed":
                raise Conflict("A completed pipeline cannot be stopped.")
            now = timestamp()
            db.execute("UPDATE pipelines SET stop_requested=1,status='stopping',stage='stopping',updated_at=?,finished_at=NULL WHERE id=?",
                       (now, pipeline_id))
            db.execute("UPDATE agent_workers SET state='stopped',completed_at=?,updated_at=?,capability_sha256='',capability_expires_at=NULL "
                       "WHERE pipeline_id=? AND state IN ('queued','waiting_question','waiting_approval','waiting_child','waiting_browser')",
                       (now, now, pipeline_id))
            self.event(db, "agent.pipeline_stop_requested", {"pipeline_id": pipeline_id}, actor="user")
            self._agent_refresh_pipeline(db, pipeline_id)
        return self.get_pipeline(pipeline_id)

    def _agent_prepare_browser_resume(self, db, worker, epoch, now, source):
        """Keep model memory; invalidate the old application page, not its approvals."""
        if worker["role"] not in {"discovery", "submission", "evaluator"}:
            return False
        context = self._agent_decode(worker["context"], {})
        if (not worker["session_id"] and not context.get("tab_id")
                and worker["state"] not in {"stopped", "interrupted", "failed", "quota_paused"}):
            return False  # A new worker has no old website session to invalidate.
        if (context.get("snapshot_supersession") or worker["state"] == "uncertain"
                or self._context_may_have_sent_final(context, worker["checkpoint"])):
            return False
        previous = context.get("tab_recovery") or {}
        if previous.get("browser_resume_epoch") == epoch:
            return False
        job = db.execute("SELECT url FROM jobs WHERE id=?", (worker["job_id"],)).fetchone() if worker["job_id"] else None
        company = db.execute("SELECT c.careers_url FROM companies c JOIN pipelines p ON p.company_id=c.id WHERE p.id=?",
                             (worker["pipeline_id"],)).fetchone()
        context["tab_recovery"] = {"attempt_id": uuid.uuid4().hex, "browser_resume_epoch": epoch,
            "authorized_at": now, "source": source, "fresh_tab_budget": 1,
            "canonical_url": job["url"] if job else company["careers_url"]}
        if worker["role"] == "submission" and (worker["session_id"] or context.get("tab_id")):
            last = db.execute("SELECT id FROM agent_runtime_events WHERE worker_id=? AND (tool_name='cua_repl.js' OR tool_name LIKE 'playwright.%') "
                              "ORDER BY rowid DESC LIMIT 1", (worker["id"],)).fetchone()
            context["browser_freshness"] = {"pending": True, "epoch": epoch, "issued_at": now,
                "reason": source, "previous_browser_event_id": last["id"] if last else None,
                "required_action": "Refresh/reopen the actual job, verify the intended account and saved values, then complete_auth."}
            if context.get('execution',{}).get('browser_backend')=='playwright':
                from .browser_sessions import BrowserSessions
                retained=BrowserSessions(self.root).inspect(worker['id'])
                context['browser_freshness'].update(browser_generation=retained.get('generation'),
                    live_state_preserved=retained.get('live_state_preserved',False),
                    required_action='Inspect the retained owned page first. Verify current account, exact job and saved/unsaved values with a fresh observation, then complete_auth. Reload only if the actual page/session expired or the browser was lost; never replay an uncertain send.')
        db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?", (json.dumps(context), now, worker["id"]))
        self.event(db, "agent.browser_resume_prepared", {"pipeline_id": worker["pipeline_id"],
                   "worker_id": worker["id"], "epoch": epoch, "source": source,
                   "application_refresh_required": bool(context.get("browser_freshness", {}).get("pending"))}, worker["job_id"], "agent_scheduler")
        return True

    def resume_agent_pipeline(self, pipeline_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            if row["status"] == "completed":
                raise Conflict("A completed pipeline cannot be resumed.")
            from .trial_limits import read as read_trial, expired
            trial = read_trial(db, pipeline_id)
            if trial and (trial["state"] != "active" or expired(trial)):
                raise Conflict("The bounded trial ended. Prepare fresh limits before resuming this campaign.")
            quota = self._agent_decode(db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()[0], {})
            if quota.get("paused"):
                raise Conflict("Clear the account quota latch with the explicit quota-resume action first.")
            now = timestamp()
            db.execute("UPDATE pipelines SET stop_requested=0,status='queued',stage='processing',blockers='[]',dashboard_hidden=0,finished_at=NULL,updated_at=? WHERE id=?",
                       (now, pipeline_id))
            resume_epoch = uuid.uuid4().hex
            for saved in db.execute("SELECT * FROM agent_workers WHERE pipeline_id=? AND turn_active=0 "
                                    "AND state NOT IN ('completed','discarded','superseded','uncertain','session_missing')", (pipeline_id,)).fetchall():
                if trial and trial.get('allowed_worker_ids') and saved['id'] not in trial['allowed_worker_ids']:
                    continue
                if trial and (saved['role']=='discovery' and trial['max_discovery_invocations']==0 or saved['role']!='discovery' and saved['job_id'] not in trial['job_ids']):
                    continue
                self._agent_prepare_browser_resume(db, saved, resume_epoch, now, "user explicitly resumed this pipeline")
            for worker in db.execute("SELECT * FROM agent_workers WHERE pipeline_id=? AND state IN ('stopped','interrupted','failed','quota_paused')",
                                     (pipeline_id,)).fetchall():
                if trial and trial.get('allowed_worker_ids') and worker['id'] not in trial['allowed_worker_ids']:
                    continue
                if trial and (worker['role']=='discovery' and trial['max_discovery_invocations']==0 or worker['role']!='discovery' and worker['job_id'] not in trial['job_ids']):
                    continue
                context = self._agent_decode(worker["context"], {})
                if context.get("snapshot_supersession"):
                    continue
                # A terminal-looking row can briefly still belong to a live
                # transport callback. Leave it alone until that callback exits.
                if worker["turn_active"]:
                    continue
                if worker["role"] == "submission" and self._context_may_have_sent_final(context, worker["checkpoint"]):
                    db.execute("UPDATE agent_workers SET state='uncertain',error=?,updated_at=?,completed_at=COALESCE(completed_at,?),"
                               "capability_sha256='',capability_expires_at=NULL WHERE id=?",
                               ("A final application action may have been sent; reconcile it before retrying.", now, now, worker["id"]))
                    continue
                pending = self._agent_pending_inbox_wait(db, worker["id"])
                if pending:
                    db.execute("UPDATE agent_workers SET state=?,updated_at=? WHERE id=?", (pending, now, worker["id"]))
                    continue
                if worker["role"] == "submission":
                    replacement = self._agent_current_submission_replacement(db, worker)
                    if replacement:
                        snapshot = {"jd_version": worker["jd_version"],
                                    "profile_version": worker["profile_version"]}
                        current_snapshot = {"jd_version": replacement["jd_version"],
                                            "profile_version": replacement["profile_version"]}
                        audit = {"reason": "stale_unsent_submission_replaced",
                                 "superseded_at": now,
                                 "replacement_worker_id": replacement["id"],
                                 "saved_snapshot": snapshot,
                                 "current_snapshot": current_snapshot}
                        context["snapshot_supersession"] = audit
                        message = ("Superseded by current-snapshot submission worker "
                                   f"{replacement['id']}; this unsent snapshot will not resume.")
                        db.execute("UPDATE agent_workers SET state='superseded',context=?,error=?,turn_active=0,"
                                   "completed_at=COALESCE(completed_at,?),updated_at=?,capability_sha256='',"
                                   "capability_expires_at=NULL WHERE id=?",
                                   (json.dumps(context, ensure_ascii=False), message, now, now, worker["id"]))
                        self.event(db, "agent.worker_superseded_by_current_snapshot",
                                   {"pipeline_id": pipeline_id, "worker_id": worker["id"],
                                    "replacement_worker_id": replacement["id"],
                                    "saved_snapshot": snapshot, "current_snapshot": current_snapshot,
                                    "checkpoint": worker["checkpoint"]}, worker["job_id"], "agent_scheduler")
                        continue
                db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,context=?,completed_at=NULL,error='',provider_error='',updated_at=? WHERE id=?",
                           ("same_session" if worker["session_id"] else "new_session", json.dumps(context, ensure_ascii=False), now, worker["id"]))
            # Restore the dependency before dispatching any resumed child.
            # Parent and child were both stopped; independently queuing both
            # makes the child reject its parent's non-waiting state.
            for parent in db.execute("SELECT * FROM agent_workers WHERE pipeline_id=? AND role='submission' AND state='queued'",
                                     (pipeline_id,)).fetchall():
                context = self._agent_decode(parent["context"], {})
                if not context.get("requested_artifacts") or self._context_may_have_sent_final(context, parent["checkpoint"]):
                    continue
                child = db.execute("SELECT id FROM agent_workers WHERE parent_id=? AND role='document' "
                                   "AND state IN ('queued','running','waiting_question','waiting_approval','interrupted','quota_paused') "
                                   "AND jd_version=? AND profile_version=? LIMIT 1",
                                   (parent["id"], parent["jd_version"], parent["profile_version"])).fetchone()
                if child:
                    active_parents = db.execute("SELECT COUNT(*) FROM agent_workers WHERE pipeline_id=? AND role='submission' "
                                                "AND (state IN ('running','waiting_child') OR turn_active=1)", (pipeline_id,)).fetchone()[0]
                    if active_parents < 3:
                        db.execute("UPDATE agent_workers SET state='waiting_child',checkpoint='waiting_child',updated_at=? WHERE id=?", (now, parent["id"]))
            self._agent_wake_orphaned_auth_waiters(db)
            self.event(db, "agent.pipeline_resumed", {"pipeline_id": pipeline_id}, actor="user")
            self._agent_refresh_pipeline(db, pipeline_id)
        return self.get_pipeline(pipeline_id)

    def _agent_current_submission_replacement(self, db, worker):
        """Find a same-job submission worker bound to the current JD/profile snapshot."""
        if not worker["job_id"]:
            return None
        current = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        if not current:
            return None
        current_profile = self.profile_version(db)
        if (worker["jd_version"] == current["jd_version"]
                and worker["profile_version"] == current_profile):
            return None
        candidates = db.execute(
            "SELECT id,jd_version,profile_version,context FROM agent_workers "
            "WHERE pipeline_id=? AND job_id=? AND role='submission' AND id!=? "
            "AND jd_version=? AND profile_version=? ORDER BY created_at DESC,id DESC",
            (worker["pipeline_id"], worker["job_id"], worker["id"],
             current["jd_version"], current_profile)).fetchall()
        for candidate in candidates:
            candidate_context = self._agent_decode(candidate["context"], {})
            if not candidate_context.get("snapshot_supersession"):
                return {"id": candidate["id"], "jd_version": candidate["jd_version"],
                        "profile_version": candidate["profile_version"]}
        return None

    def confirm_agent_session_replacement(self, worker_id, provenance, replacement_reason):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("Session replacement requires user provenance.")
        if not isinstance(replacement_reason, str) or not replacement_reason.strip():
            raise ValueError("Explain why the saved session must be replaced.")
        with self.connect(write=True) as db:
            worker = db.execute("SELECT w.*,p.stop_requested,p.status AS pipeline_status,p.mode "
                                "FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?",
                                (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            if worker["mode"] != "agentic" or worker["state"] != "session_missing":
                raise Conflict("Only a definitively missing saved session can be replaced.")
            if worker["stop_requested"] or worker["pipeline_status"] in {"stopped", "stopping", "completed"}:
                raise Conflict("Resume the pipeline explicitly before replacing a worker session.")
            context = self._agent_decode(worker["context"], {})
            if worker["role"] == "submission" and self._context_may_have_sent_final(context, worker["checkpoint"]):
                raise Conflict("The final application action may have been sent; the worker is reconciliation-only.")
            old_session = worker["session_id"]
            context["session_replacement"] = {"previous_session_id": old_session,
                                               "reason": replacement_reason.strip()[:4000],
                                               "provenance": provenance.strip()[:1000],
                                               "confirmed_at": timestamp()}
            now = timestamp()
            db.execute("UPDATE agent_workers SET state='queued',session_id=NULL,resume_mode='replacement_session',"
                       "context=?,error='',provider_error='',completed_at=NULL,capability_sha256='',"
                       "capability_expires_at=NULL,updated_at=? WHERE id=?",
                       (json.dumps(context, ensure_ascii=False), now, worker_id))
            db.execute("UPDATE pipelines SET status='queued',finished_at=NULL,blockers='[]',updated_at=? WHERE id=?",
                       (now, worker["pipeline_id"]))
            self.event(db, "agent.session_replacement_confirmed", {"worker_id": worker_id,
                       "previous_session_id": old_session, "resume_mode": "replacement_session",
                       "reason": replacement_reason.strip(), "provenance": provenance.strip()},
                       worker["job_id"], "user")
            return {"worker_id": worker_id, "pipeline_id": worker["pipeline_id"], "state": "queued",
                    "resume_mode": "replacement_session", "previous_session_id": old_session}

    def resume_agent_quota(self):
        with self.connect(write=True) as db:
            raw = db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
            state = self._agent_decode(raw[0] if raw else "{}", {})
            now = timestamp()
            db.execute("UPDATE meta SET value=? WHERE key='agent_quota_latch'", (json.dumps({
                "paused": False, "provider_error": None, "reason": None, "paused_at": None}),))
            rows = db.execute("SELECT w.*,p.stop_requested,p.status AS pipeline_status FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                              "WHERE w.state='quota_paused'").fetchall()
            eligible_paused = [w for w in rows if not w["stop_requested"] and w["pipeline_status"] not in {"stopped", "stopping", "completed"}]
            if state.get("paused") or eligible_paused:
                resume_epoch = uuid.uuid4().hex
                # Pending questions/approvals may outlive the same quota pause.
                # Mark their pages stale too, without resolving or resuming them.
                saved_rows = db.execute("SELECT w.* FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                    "WHERE p.mode='agentic' AND p.stop_requested=0 AND p.status NOT IN ('stopped','stopping','completed') "
                    "AND w.state NOT IN ('completed','discarded','superseded','uncertain','session_missing','stopped')").fetchall()
                for saved in saved_rows:
                    self._agent_prepare_browser_resume(db, saved, resume_epoch, now, "user resumed after account quota pause; website session may have expired")
                # The following loop must use the freshly persisted contexts.
                rows = db.execute("SELECT w.*,p.stop_requested,p.status AS pipeline_status FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                  "WHERE w.state='quota_paused'").fetchall()
            requeued = 0
            for worker in rows:
                context = self._agent_decode(worker["context"], {})
                if worker["stop_requested"] or worker["turn_active"] or worker["pipeline_status"] in {"stopped", "stopping", "completed"}:
                    continue
                if worker["role"] == "submission" and self._context_may_have_sent_final(context, worker["checkpoint"]):
                    db.execute("UPDATE agent_workers SET state='uncertain',error=?,updated_at=?,completed_at=? WHERE id=?",
                               ("Quota/interruption occurred after final action authorization; reconcile before retry.", now, now, worker["id"]))
                    if worker["job_id"]:
                        db.execute("UPDATE pipeline_jobs SET agent_status='uncertain' WHERE pipeline_id=? AND job_id=?",
                                   (worker["pipeline_id"], worker["job_id"]))
                    continue
                pending = self._agent_pending_inbox_wait(db, worker["id"])
                if pending:
                    db.execute("UPDATE agent_workers SET state=?,updated_at=? WHERE id=?", (pending, now, worker["id"]))
                    continue
                db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,provider_error='',error='',updated_at=? WHERE id=?",
                           ("same_session" if worker["session_id"] else "new_session", now, worker["id"]))
                requeued += 1
            pipeline_ids = {row["pipeline_id"] for row in rows if not row["stop_requested"] and row["pipeline_status"] not in {"stopped", "stopping", "completed"}}
            for pipeline_id in pipeline_ids:
                self._agent_refresh_pipeline(db, pipeline_id)
            self.event(db, "agent.quota_resumed", {"requeued": requeued,
                       "preserved_stopped": sum(1 for row in rows if row["stop_requested"])}, actor="user")
        return self.get_agent_quota()

    def recover_interrupted_agent_workers(self):
        """Persist process loss for explicit, safe user resume; never auto-launch."""
        with self.connect(write=True) as db:
            rows = db.execute("SELECT w.*,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                              "WHERE w.state='running' OR w.turn_active=1").fetchall()
            now = timestamp()
            affected = set()
            for worker in rows:
                context = self._agent_decode(worker["context"], {})
                uncertain = worker["role"] == "submission" and self._context_may_have_sent_final(context, worker["checkpoint"])
                if worker["state"] in {"completed", "discarded", "uncertain", "failed", "stopped"}:
                    state = worker["state"]
                    error = worker["error"]
                else:
                    state = "stopped" if worker["stop_requested"] else "uncertain" if uncertain else "interrupted"
                    error = ("Final application action may have been sent before process loss; reconcile before retry."
                             if uncertain else "Process ended; checkpoint is saved for explicit resume.")
                db.execute("UPDATE agent_workers SET state=?,turn_active=0,error=?,updated_at=?,completed_at=?,capability_sha256='',"
                           "capability_expires_at=NULL WHERE id=?",
                           (state, error, now, now, worker["id"]))
                affected.add(worker["pipeline_id"])
                self.event(db, "agent.worker_recovered", {"worker_id": worker["id"], "state": state,
                           "session_id": worker["session_id"]}, worker["job_id"], "agent_scheduler")
            for pipeline_id in affected:
                self._agent_refresh_pipeline(db, pipeline_id)
            return {"recovered": len(rows), "pipeline_ids": sorted(affected)}

    def set_agent_worker_checkpoint(self, worker_id, checkpoint, context=None):
        if not isinstance(checkpoint, str) or not checkpoint.strip() or len(checkpoint) > 256:
            raise ValueError("Checkpoint must be a short non-empty string.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT w.*,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?",
                             (worker_id,)).fetchone()
            if not row:
                raise KeyError(worker_id)
            if row["state"] != "running":
                raise Conflict("Only a running worker can checkpoint.")
            if row["stop_requested"]:
                raise Conflict("The pipeline is stopped; no additional agent action is allowed.")
            old = self._agent_decode(row["context"], {})
            if context is not None:
                old.update(self._agent_validate_notes(context))
            db.execute("UPDATE agent_workers SET checkpoint=?,context=?,updated_at=? WHERE id=?",
                       (checkpoint.strip(), json.dumps(old, ensure_ascii=False), timestamp(), worker_id))
            return {"worker_id": worker_id, "checkpoint": checkpoint.strip()}

    def agent_runtime_event(self, worker_id, event):
        if not isinstance(event, dict):
            raise ValueError("Runtime events must be objects.")
        event_type = str(event.get("type") or event.get("event_type") or "event")[:120]
        tool_name = str(event.get("tool_name") or "")[:160]
        success_value = event.get("success")
        success = int(success_value) if isinstance(success_value, bool) else None
        session_value = event.get("session_id")
        if not session_value and event_type in {"thread.started", "session.started", "thread.resumed"}:
            session_value = event.get("thread_id")
            thread = event.get("thread")
            if not session_value and isinstance(thread, dict):
                session_value = thread.get("id") or thread.get("thread_id") or thread.get("session_id")
        session_id = str(session_value or "")[:256] or None
        payload_data = event
        payload = json.dumps(payload_data, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > 1_800_000:
            # CUA responses can contain screenshots or serialized image data. Keep
            # the actual-success envelope and a digest of the full transport result
            # without duplicating a multi-megabyte image into the durable ledger.
            transport = event.get("transport_event")
            if transport is not None:
                transport_bytes = json.dumps(transport, ensure_ascii=False, sort_keys=True).encode("utf-8")
                payload_data = {key: value for key, value in event.items() if key != "transport_event"}
                payload_data["transport_event_sha256"] = hashlib.sha256(transport_bytes).hexdigest()
                payload_data["transport_event_bytes"] = len(transport_bytes)
                payload_data["transport_event_truncated"] = True
            else:
                raw_bytes = payload.encode("utf-8")
                payload_data = {"type": event_type, "tool_name": tool_name,
                                "session_id": session_id, "success": success_value,
                                "transport_event_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                                "transport_event_bytes": len(raw_bytes), "transport_event_truncated": True}
            payload = json.dumps(payload_data, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > 2_000_000:
            raise ValueError("Runtime event metadata exceeds the supported size.")
        event_id = uuid.uuid4().hex
        with self.connect(write=True) as db:
            row = db.execute("SELECT state,pipeline_id,job_id,session_id,turn_active,role FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if not row:
                raise KeyError(worker_id)
            if (row["state"] not in {"running", "waiting_question", "waiting_approval", "waiting_child", "waiting_browser",
                                      "quota_paused", "completed", "discarded", "uncertain", "failed"}
                    and not (row["state"] == "queued" and row["turn_active"]
                             and (not session_id or session_id == row["session_id"]))
                    and not (row['state'] in {'stopped', 'interrupted'} and not row['turn_active']
                             and event.get('supervisor_read_only_observation') is True
                             and tool_name == 'playwright.browser_snapshot' and success == 1
                             and session_id == row['session_id']
                             and self._agent_observed_application_url(db, row, event))):
                raise Conflict("Runtime events can only be attached to a running worker.")
            # Mail bodies/codes must not be persisted as application evidence.
            # Retain the genuine event's identity, result status and hash so a
            # fresh browser observation can still close the sign-in mutex.
            # A worker can acknowledge account freshness before the final
            # Greenhouse click. Its still-visible code boxes remain sensitive
            # even after that acknowledgement releases the auth mutex.
            verification_visible = ('enter the 8-character code to confirm' in json.dumps(event)
                                    and self._agent_observed_application_url(db,row,event))
            if row["role"] in {"submission","discovery"} and (verification_visible or (db.execute("SELECT 1 FROM meta WHERE key=?",
                    ("agent_signin_automation:" + row["pipeline_id"],)).fetchone() or db.execute("SELECT 1 FROM meta WHERE key='routine_email_tokens'").fetchone()) and db.execute(
                    "SELECT 1 FROM agent_browser_controls WHERE control_type='company_auth' AND state='open' AND worker_id=?", (worker_id,)).fetchone()):
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
                metadata = {"type": event_type, "session_id": session_id or row["session_id"], "tool_name": tool_name,
                            "success": success_value, "authentication_redacted": True,
                            "event_sha256": hashlib.sha256(raw).hexdigest(), "event_bytes": len(raw)}
                if (event_type == "timing_summary" and isinstance(event.get("timing"), dict)
                        and event["timing"].get("schema") == "worker-timing-v1"):
                    # Host timing has numeric durations/tool identities only,
                    # never arguments, page text, mail bodies or credentials.
                    metadata["timing"] = event["timing"]
                transport = event.get("transport_event", {})
                item = transport.get("item", {}) if isinstance(transport, dict) else {}
                if item.get("type") == "mcp_tool_call" and (item.get("server") == "playwright" or item.get("server") == "cua_repl" and item.get("tool") == "js"):
                    metadata["transport_event"] = {"type": transport.get("type"), "item": {
                        "type": "mcp_tool_call", "server": item.get("server"), "tool": item.get("tool"), "status": item.get("status")}}
                    safe_url = self._agent_observed_application_url(db, row, event)
                    if safe_url:
                        metadata["observed_application_url"] = safe_url
                    verification=self._agent_verification_observation(db,row,event)
                    if verification:
                        metadata['verification_observation']=verification
                    if success is not None and not success and self._agent_is_input_timeout(event):
                        metadata["browser_input_timeout"] = True
                    if success is not None and not success and self._agent_is_observation_timeout(event):
                        metadata["browser_observation_timeout"] = True
                payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            db.execute("INSERT INTO agent_runtime_events(id,worker_id,session_id,event_type,tool_name,success,payload,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (event_id, worker_id, session_id or row["session_id"], event_type, tool_name, success, payload, timestamp()))
            if session_id:
                db.execute("UPDATE agent_workers SET session_id=?,updated_at=? WHERE id=?", (session_id, timestamp(), worker_id))
            db.execute("UPDATE agent_workers SET capability_expires_at=? WHERE id=? AND capability_sha256!=''",
                       (time.time() + 3600, worker_id))
            if event_type.casefold() in {"quota_error", "usage_limit_exceeded"} or event.get("quota_exceeded") is True:
                reason = str(event.get("message") or event.get("error") or "Account quota was reached.")
                quota = {"paused": True, "provider_error": reason[:4000], "reason": reason[:4000], "paused_at": timestamp()}
                db.execute("INSERT INTO meta(key,value) VALUES('agent_quota_latch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(quota, ensure_ascii=False),))
                db.execute("UPDATE agent_workers SET state='quota_paused',provider_error=?,checkpoint=COALESCE(NULLIF(?,''),checkpoint),"
                           "capability_sha256='',capability_expires_at=NULL,updated_at=? WHERE id=?",
                           (reason[:4000], str(event.get("checkpoint") or ""), timestamp(), worker_id))
                self.event(db, "agent.quota_latch", quota, row["job_id"], "agent_runtime")
            self.event(db, "agent.runtime_event", {"worker_id": worker_id, "event_id": event_id,
                       "type": event_type, "tool_name": tool_name, "success": success}, row["job_id"], "agent_runtime")
        return event_id

    @staticmethod
    def _agent_usage_int(value, name):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Usage counter {name} must be a nonnegative integer or null.")
        return value

    def record_agent_usage(self, worker_id, usage):
        """Persist actual CLI usage once per logical turn and derive cumulative deltas safely."""
        if not isinstance(usage, dict):
            raise ValueError("Usage events must be objects.")
        session_id = str(usage.get("session_id") or "").strip()
        turn_id = str(usage.get("turn_id") or "").strip()
        if not session_id or not turn_id or len(session_id) > 256 or len(turn_id) > 256:
            raise ValueError("Usage events need the actual session ID and turn ID.")
        counter_kind = usage.get("counter_kind")
        if counter_kind not in {"turn", "cumulative"}:
            raise ValueError("Usage counter_kind must be turn or cumulative.")
        counters = usage.get("counters")
        if not isinstance(counters, dict):
            raise ValueError("Usage events need a counters object.")
        values = {name: self._agent_usage_int(counters.get(name), name) for name in
                  ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
        if (values["input_tokens"] is not None and values["cached_input_tokens"] is not None
                and values["cached_input_tokens"] > values["input_tokens"]):
            raise ValueError("Cached input tokens are a subset of input tokens and cannot exceed them.")
        tool_calls = self._agent_usage_int(usage.get("tool_call_count"), "tool_call_count")
        elapsed = usage.get("elapsed_seconds")
        if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0):
            raise ValueError("Elapsed seconds must be a nonnegative number or null.")
        elapsed = float(elapsed) if elapsed is not None else None
        source = usage.get("counter_source")
        if source is not None and not isinstance(source, str):
            raise ValueError("counter_source must be a string or null.")
        source = str(source)[:256] if source else None

        names = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
        with self.connect(write=True) as db:
            worker = db.execute("SELECT pipeline_id,role,context FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            prior_turn = db.execute("SELECT * FROM agent_usage WHERE worker_id=? AND session_id=? AND turn_id=?",
                                    (worker_id, session_id, turn_id)).fetchone()
            if prior_turn:
                delta = {name: prior_turn["delta_" + name] for name in names}
                return {"recorded": True, "duplicate": True, "delta": self._agent_usage_delta_view(delta, tool_calls=prior_turn["tool_call_count"],
                                                                                                       elapsed=prior_turn["elapsed_seconds"])}

            previous = None
            if counter_kind == "cumulative":
                previous = db.execute("SELECT * FROM agent_usage WHERE worker_id=? AND session_id=? "
                                      "ORDER BY created_at DESC,rowid DESC LIMIT 1", (worker_id, session_id)).fetchone()
                if previous is None:
                    # Upgrade a resumed legacy session from its prior raw CLI
                    # snapshot; do not count its entire historical context again.
                    raw = db.execute("SELECT payload FROM agent_runtime_events WHERE worker_id=? AND session_id=? "
                                     "AND event_type='turn.completed' ORDER BY rowid DESC LIMIT 2",
                                     (worker_id, session_id)).fetchall()
                    if len(raw) > 1:
                        prior = self._agent_decode(raw[1]["payload"], {}).get("usage", {})
                        if isinstance(prior, dict) and all(type(prior.get(name)) is int for name in ("input_tokens", "output_tokens")):
                            previous = {name: prior.get("reasoning_output_tokens") if name == "reasoning_tokens"
                                        else prior.get(name) for name in names}
            delta = {}
            for name in names:
                current = values[name]
                previous_value = previous[name] if previous else None
                if counter_kind == "turn":
                    delta[name] = current
                elif current is None:
                    delta[name] = None
                elif previous is None:
                    delta[name] = current
                elif previous_value is None:
                    delta[name] = None
                else:
                    if current < previous_value:
                        raise Conflict(f"Cumulative usage counter {name} decreased within one CLI session.")
                    delta[name] = current - previous_value
            if (delta["input_tokens"] is not None and delta["cached_input_tokens"] is not None
                    and delta["cached_input_tokens"] > delta["input_tokens"]):
                raise Conflict("Cumulative cached-input delta exceeds the input delta.")
            now = timestamp()
            db.execute("INSERT INTO agent_usage(worker_id,session_id,turn_id,counter_kind,input_tokens,output_tokens,"
                       "cached_input_tokens,reasoning_tokens,delta_input_tokens,delta_output_tokens,"
                       "delta_cached_input_tokens,delta_reasoning_tokens,tool_call_count,elapsed_seconds,counter_source,created_at,execution) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (worker_id, session_id, turn_id, counter_kind, values["input_tokens"], values["output_tokens"],
                        values["cached_input_tokens"], values["reasoning_tokens"], delta["input_tokens"],
                        delta["output_tokens"], delta["cached_input_tokens"], delta["reasoning_tokens"],
                        tool_calls, elapsed, source, now, json.dumps(self._agent_decode(worker["context"], {}).get("execution", {}))))
            usage_delta = self._agent_usage_delta_view(delta, tool_calls=tool_calls, elapsed=elapsed)
            self.event(db, "agent.usage_delta", {"worker_id": worker_id, "pipeline_id": worker["pipeline_id"],
                       "role": worker["role"], "session_id": session_id, "turn_id": turn_id,
                       "counter_kind": counter_kind, "counter_source": source, "delta": usage_delta},
                       actor="agent_runtime")
            return {"recorded": True, "duplicate": False, "delta": usage_delta}

    @staticmethod
    def _agent_usage_delta_view(delta, *, tool_calls=None, elapsed=None):
        input_tokens = delta.get("input_tokens")
        cached = delta.get("cached_input_tokens")
        uncached = input_tokens - cached if input_tokens is not None and cached is not None else None
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "uncached_input_tokens": uncached, "output_tokens": delta.get("output_tokens"),
                "reasoning_tokens": delta.get("reasoning_tokens"),
                "tool_call_count": tool_calls, "model_elapsed_seconds": elapsed}

    def get_agent_efficiency(self, pipeline_id=None):
        with self.connect() as db:
            if pipeline_id and not db.execute("SELECT 1 FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            where, params = ("WHERE w.pipeline_id=?", (pipeline_id,)) if pipeline_id else ("", ())
            rows = db.execute("SELECT w.role,u.* FROM agent_usage u JOIN agent_workers w ON w.id=u.worker_id "
                              + where + " ORDER BY u.created_at,u.worker_id,u.session_id,u.turn_id", params).fetchall()
            from .request_metrics import effective_rows, diagnostics
            rows = effective_rows(db, rows, pipeline_id)
            roles = sorted({row["role"] for row in rows})
            execution_groups = {}
            for row in rows:
                execution = self._agent_decode(row["execution"], {})
                identity = tuple(execution.get(k) for k in ("provider", "model", "effort", "browser_backend"))
                execution_groups.setdefault(identity, []).append(row)
            result = {"pipeline_id": pipeline_id,
                      "totals": self._agent_usage_totals(rows),
                      "by_role": {role: self._agent_usage_totals([row for row in rows if row["role"] == role])
                                  for role in roles},
                      "cache": self._agent_cache_metrics(db, pipeline_id)}
            result['provider_requests'] = diagnostics(db, pipeline_id)
            result["by_execution"] = [{"execution": dict(zip(("provider", "model", "effort", "browser_backend"), key)),
                                        **self._agent_usage_totals(group)} for key, group in execution_groups.items()]
            from .timing import summarize_timings
            timing_rows = db.execute("SELECT e.payload FROM agent_runtime_events e JOIN agent_workers w ON w.id=e.worker_id "
                                     "WHERE e.event_type='timing_summary' " + ("AND w.pipeline_id=? " if pipeline_id else "")
                                     + "ORDER BY e.rowid", (pipeline_id,) if pipeline_id else ()).fetchall()
            result["timing"] = summarize_timings(self._agent_decode(row["payload"], {}).get("timing") for row in timing_rows)
            wait_rows = db.execute("SELECT created_at,resolved_at FROM agent_inbox " +
                                   ("WHERE pipeline_id=?" if pipeline_id else ""), (pipeline_id,) if pipeline_id else ()).fetchall()
            intervals = []
            now = datetime.now(timezone.utc).timestamp()
            for row in wait_rows:
                try:
                    begin = datetime.fromisoformat(row["created_at"]).timestamp()
                    end = datetime.fromisoformat(row["resolved_at"]).timestamp() if row["resolved_at"] else now
                    if begin <= end:
                        intervals.append((begin, end))
                except (TypeError, ValueError):
                    pass
            merged = []
            for begin, end in sorted(intervals):
                if merged and begin <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([begin, end])
            result["human_wait_seconds"] = sum(end - begin for begin, end in merged)
            result["measurement_notes"] = ["Human wait is the union of recorded inbox waits, including pending waits.",
                                            "Agent execution seconds sum worker turns; parallel turns overlap in wall time.",
                                            "The legacy model_elapsed_seconds field is worker execution time, including tools; it is not inference time.",
                                            "Cached input is part of input. Reasoning is part of output."]
            return result

    def agent_runtime_status(self, pipeline_id):
        with self.connect() as db:
            pipeline = db.execute("SELECT * FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not pipeline:
                raise KeyError(pipeline_id)
            states = dict(db.execute("SELECT state,COUNT(*) FROM agent_workers WHERE pipeline_id=? GROUP BY state",
                                     (pipeline_id,)).fetchall())
            auth = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=?",
                              ("company:" + pipeline["company_id"],)).fetchone()
            circuit = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key='connector:cua_repl'").fetchone()
            buffered = []
            for worker in db.execute("SELECT * FROM agent_workers WHERE pipeline_id=? AND state='queued'", (pipeline_id,)):
                wait = self._agent_capacity_wait(db, worker)
                if wait:
                    buffered.append({"worker_id": worker["id"], "role": worker["role"], **wait})
            return {"pipeline_id": pipeline_id, "quota": self.get_agent_quota(),
                    "company_gate": self._agent_browser_control_view(auth, "company_auth"),
                    "circuits": [self._agent_browser_control_view(circuit, "connector_circuit")],
                    "capacity_waits": buffered,
                    "workers": {"active": states.get("running", 0), "queued": states.get("queued", 0),
                                "waiting": sum(count for state, count in states.items() if state.startswith("waiting")),
                                "failed": states.get("failed", 0), "uncertain": states.get("uncertain", 0)},
                    "caps": {"discovery": 1, "evaluator": 3, "submission": 3}}

    @staticmethod
    def _agent_usage_totals(rows):
        result = {"turns": len(rows), "input_tokens": 0, "uncached_input_tokens": 0,
                  "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                  "tool_call_count": 0, "model_elapsed_seconds": 0.0,
                  "human_wait_seconds": None, "known": {}, "missing": {}}
        result['partial_usage_turns'] = sum(bool(dict(row).get('partial_usage')) for row in rows)
        result['token_totals_are_lower_bounds'] = bool(result['partial_usage_turns'])
        fields = {"input_tokens": "delta_input_tokens", "cached_input_tokens": "delta_cached_input_tokens",
                  "output_tokens": "delta_output_tokens", "reasoning_tokens": "delta_reasoning_tokens"}
        for output, column in fields.items():
            present = [row[column] for row in rows if row[column] is not None]
            result[output] = sum(present) if present else None
            result["known"][output] = len(present)
            result["missing"][output] = len(rows) - len(present)
        uncached = [row["delta_input_tokens"] - row["delta_cached_input_tokens"] for row in rows
                    if row["delta_input_tokens"] is not None and row["delta_cached_input_tokens"] is not None]
        result["uncached_input_tokens"] = sum(uncached) if uncached else None
        result["known"]["uncached_input_tokens"] = len(uncached)
        result["missing"]["uncached_input_tokens"] = len(rows) - len(uncached)
        tool_calls = [row["tool_call_count"] for row in rows if row["tool_call_count"] is not None]
        elapsed = [row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None]
        result["tool_call_count"] = sum(tool_calls) if tool_calls else None
        result["model_elapsed_seconds"] = sum(elapsed) if elapsed else None
        result["agent_execution_seconds"] = result["model_elapsed_seconds"]
        result["known"]["tool_call_count"] = len(tool_calls)
        result["missing"]["tool_call_count"] = len(rows) - len(tool_calls)
        result["known"]["model_elapsed_seconds"] = len(elapsed)
        result["missing"]["model_elapsed_seconds"] = len(rows) - len(elapsed)
        return result

    def _agent_event_results(self, db, worker_id):
        rows = db.execute("SELECT id,session_id,event_type,tool_name,success,payload FROM agent_runtime_events "
                          "WHERE worker_id=? ORDER BY created_at,id", (worker_id,)).fetchall()
        worker = db.execute("SELECT session_id FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
        valid = []
        for row in rows:
            if ((row["tool_name"] == "cua_repl.js" or row["tool_name"].startswith("playwright.")) and row["success"] == 1
                    and row["session_id"] and row["session_id"] == worker["session_id"]):
                valid.append(row["id"])
        return valid

    def _agent_edit_documents(self,db,worker,payload):
        if worker['role']!='document':raise Conflict('Only the document child can edit its requested documents.')
        from .efficiency_features import features
        if not features(self.root)['document_edits']:raise Conflict('Document edits are disabled.')
        edits=payload.get('edits')
        if not isinstance(edits,list) or not 1<=len(edits)<=2:raise ValueError('Edit one or two requested documents.')
        artifacts=[]
        for edit in edits:
            identity=str(edit.get('base_source_id',''))
            if identity.startswith('resume_base:'):
                source=self._agent_retrieve_sources(db,worker,{'source_ids':[identity]})['sources'][0]
                markdown=source['content'];sha=source['sha256']
            elif identity.startswith('artifact:'):
                row=db.execute('SELECT * FROM agent_artifacts WHERE id=? AND worker_id=? AND kind=? AND jd_version=? AND profile_version=?',
                    (identity.removeprefix('artifact:'),worker['id'],edit.get('kind'),worker['jd_version'],worker['profile_version'])).fetchone()
                if not row:raise Conflict('The base artifact is not this child\'s current scoped draft.')
                path=(self.root/row['path']).resolve()
                if not path.is_relative_to(self.root/'artifacts/agent'):raise Conflict('The base artifact escaped its package.')
                markdown=path.read_text(encoding='utf-8');sha=digest(markdown)
                if sha!=row['sha256']:raise Conflict('The source draft changed outside the recorded artifact.')
            else:raise ValueError('Use an own artifact:ID or indexed resume_base:ID.')
            if sha!=edit.get('expected_sha256'):raise Conflict('The source hash changed; retrieve the actual current draft before editing.')
            replacements=edit.get('replacements')
            if not isinstance(replacements,list) or not 1<=len(replacements)<=40:raise ValueError('Supply 1–40 exact grouped replacements.')
            for change in replacements:
                old,new=change.get('old'),change.get('new')
                if not isinstance(old,str) or not old or not isinstance(new,str) or markdown.count(old)!=1:
                    raise Conflict('An edit target is absent or ambiguous; no document version was published.')
                markdown=markdown.replace(old,new,1)
            artifacts.append({'kind':edit.get('kind'),'markdown':markdown,
                'source_notes':{**(edit.get('source_notes') or {}),'edit_base':identity,'edit_base_sha256':sha}})
        return self._agent_publish_documents(db,worker,{'artifacts':artifacts})

    def _agent_publish_documents(self, db, worker, payload):
        if worker["role"] != "document" or not worker["parent_id"] or not worker["job_id"]:
            raise Conflict("Only a scoped document child can publish documents.")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("Publish at least one requested document artifact.")
        parent = db.execute("SELECT context,state FROM agent_workers WHERE id=? AND parent_id IS NULL",
                            (worker["parent_id"],)).fetchone()
        if not parent or parent["state"] != "waiting_child":
            raise Conflict("The submission parent is not waiting for this document child.")
        parent_context = self._agent_decode(parent["context"], {})
        requested = set(parent_context.get("requested_artifacts") or [])
        package_id = str(parent_context.get("document_package_id") or uuid.uuid4().hex)
        parent_context["document_package_id"] = package_id
        root = (self.root / "artifacts" / "agent" / package_id).resolve()
        if self.root not in root.parents:
            raise Conflict("Artifact path escaped the application data directory.")
        root.mkdir(parents=True, exist_ok=True)
        published, manifest_files = [], []
        allowed = {"resume": ("resume.md", "text/markdown"),
                   "cover_letter": ("cover-letter.md", "text/markdown")}
        current_job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        current_profile = self.profile_version(db)
        if not current_job or worker["jd_version"] != current_job["jd_version"] or worker["profile_version"] != current_profile:
            raise Conflict("The job description or applicant profile changed; this document draft is stale.")
        seen = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("Each document artifact must be an object.")
            kind = artifact.get("kind")
            markdown = artifact.get("markdown")
            if kind not in allowed or kind not in requested or kind in seen:
                raise ValueError("Only the requested resume or cover-letter artifact may be published once per call.")
            if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > 300000:
                raise ValueError("Document markdown must be non-empty and within the size limit.")
            seen.add(kind)
            filename, media_type = allowed[kind]
            old_version = db.execute("SELECT MAX(version) FROM agent_artifacts WHERE worker_id=? AND kind=?",
                                     (worker["id"], kind)).fetchone()[0]
            version = int(old_version or 0) + 1
            version_root = (root / f"v{version}").resolve()
            if root not in version_root.parents:
                raise Conflict("Artifact version path escaped its package directory.")
            version_root.mkdir(parents=True, exist_ok=True)
            target = (version_root / filename).resolve()
            if version_root != target.parent:
                raise Conflict("Artifact path escaped its package directory.")
            target.write_text(markdown, encoding="utf-8", newline="\n")
            checksum = digest(markdown)
            artifact_id = uuid.uuid4().hex
            relative = str(target.relative_to(self.root))
            notes = artifact.get("source_notes") if isinstance(artifact.get("source_notes"), dict) else {}
            now = timestamp()
            db.execute("INSERT INTO agent_artifacts(id,pipeline_id,worker_id,job_id,kind,version,path,sha256,filename,"
                       "media_type,source_notes,jd_version,profile_version,status,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?)",
                       (artifact_id, worker["pipeline_id"], worker["id"], worker["job_id"], kind, version,
                        relative, checksum, filename, media_type, json.dumps(notes, ensure_ascii=False),
                        current_job["jd_version"], current_profile, now))
            record = {"id": artifact_id, "kind": kind, "path": relative, "filename": filename,
                      "media_type": media_type, "sha256": checksum, "version": version, "status": "draft"}
            published.append(record)
            manifest_files.append(record)
        parent_context["draft_artifacts"] = manifest_files
        manifest = {"package_id": package_id, "pipeline_id": worker["pipeline_id"], "job_id": worker["job_id"],
                    "parent_worker_id": worker["parent_id"], "document_worker_id": worker["id"],
                    "requested_artifacts": sorted(requested), "jd_version": current_job["jd_version"],
                    "profile_version": current_profile, "artifacts": manifest_files, "status": "draft"}
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
        manifest_path = (root / "manifest.json").resolve()
        manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")
        parent_context["manifest_path"] = str(manifest_path.relative_to(self.root))
        parent_context["manifest_sha256"] = digest(manifest_text)
        db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?",
                   (json.dumps(parent_context, ensure_ascii=False), timestamp(), worker["parent_id"]))
        self.event(db, "agent.documents_published", {"worker_id": worker["id"], "package_id": package_id,
                   "artifacts": published, "status": "draft"}, worker["job_id"], "agent_document")
        return {"package_id": package_id, "artifacts": published,
                "manifest_path": parent_context["manifest_path"],
                "manifest_sha256": parent_context["manifest_sha256"],
                "jd_version": current_job["jd_version"], "profile_version": current_profile}

    def lookup_agent_render_cache(self, worker_id, artifact_ids, renderer_sha256):
        """Return only current, content-addressed renders verified on disk.

        Returned paths are workspace-relative. Callers must copy them into the
        new package and re-hash the copies before registering them.
        """
        if (not isinstance(renderer_sha256, str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", renderer_sha256)):
            raise ValueError("renderer_sha256 must identify the exact renderer/style configuration.")
        renderer_sha256 = renderer_sha256.casefold()
        if (not isinstance(artifact_ids, list) or not artifact_ids or len(artifact_ids) > 8
                or any(not isinstance(value, str) or not value for value in artifact_ids)):
            raise ValueError("Provide one to eight current artifact IDs for render-cache lookup.")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Render-cache artifact IDs must be unique.")
        hits, misses = [], []
        with self.connect(write=True) as db:
            worker = db.execute("SELECT w.*,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                "WHERE w.id=?", (worker_id,)).fetchone()
            if not worker or worker["role"] != "document" or worker["state"] != "running" or worker["stop_requested"]:
                raise Conflict("Render-cache lookup requires the current running document child.")
            parent = db.execute("SELECT context,state FROM agent_workers WHERE id=?", (worker["parent_id"],)).fetchone()
            if not parent or parent["state"] != "waiting_child":
                raise Conflict("The submission parent is not waiting for this document child.")
            parent_context = self._agent_decode(parent["context"], {})
            package_id = parent_context.get("document_package_id")
            requested = set(parent_context.get("requested_artifacts") or [])
            current_job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
            if (not package_id or not current_job or current_job["jd_version"] != worker["jd_version"]
                    or self.profile_version(db) != worker["profile_version"]):
                raise Conflict("The document child snapshot is stale or its package is unavailable.")
            for artifact_id in artifact_ids:
                row = db.execute("SELECT * FROM agent_artifacts WHERE id=? AND worker_id=? AND pipeline_id=? "
                                 "AND job_id=? AND status='draft'",
                                 (artifact_id, worker_id, worker["pipeline_id"], worker["job_id"])).fetchone()
                if (not row or row["kind"] not in requested or row["jd_version"] != worker["jd_version"]
                        or row["profile_version"] != worker["profile_version"]):
                    raise Conflict("Render-cache lookup is not scoped to a current requested draft.")
                source = (self.root / row["path"]).resolve()
                current_package = (self.root / "artifacts" / "agent" / str(package_id)).resolve()
                if (current_package not in source.parents or not source.is_file()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]):
                    raise Conflict("The current Markdown draft is missing, outside its package, or changed.")
                cache_key = digest(row["sha256"] + ":" + renderer_sha256)
                cached = db.execute("SELECT * FROM agent_render_cache WHERE cache_key=?", (cache_key,)).fetchone()
                verified = None
                if cached and cached["source_sha256"] == row["sha256"] and cached["renderer_sha256"] == renderer_sha256:
                    pdf = (self.root / cached["pdf_path"]).resolve()
                    old_package_root = (self.root / "artifacts" / "agent").resolve()
                    if (old_package_root not in pdf.parents or "rendered" not in pdf.parts or not pdf.is_file()
                            or hashlib.sha256(pdf.read_bytes()).hexdigest() != cached["pdf_sha256"]):
                        cached = None
                    else:
                        previews = self._agent_decode(cached["previews"], [])
                        normalized_previews = []
                        for preview in previews if isinstance(previews, list) else []:
                            if not isinstance(preview, dict):
                                cached = None
                                break
                            path = (self.root / str(preview.get("path") or "")).resolve()
                            if (old_package_root not in path.parents or "rendered" not in path.parts
                                    or not path.is_file()
                                    or hashlib.sha256(path.read_bytes()).hexdigest() != preview.get("sha256")):
                                cached = None
                                break
                            normalized_previews.append({"path": path.relative_to(self.root).as_posix(),
                                                        "sha256": preview["sha256"],
                                                        "filename": path.name})
                        if cached:
                            verified = {"artifact_id": artifact_id, "source_sha256": row["sha256"],
                                        "renderer_sha256": renderer_sha256,
                                        "pdf": {"path": pdf.relative_to(self.root).as_posix(),
                                                "sha256": cached["pdf_sha256"], "filename": pdf.name},
                                        "previews": normalized_previews}
                if verified:
                    hits.append(verified)
                    hit, key = 1, cache_key
                else:
                    misses.append(artifact_id)
                    hit, key = 0, cache_key
                db.execute("INSERT INTO agent_cache_observations(pipeline_id,worker_id,cache_kind,hit,cache_key,created_at) "
                           "VALUES(?,?,'render',?,?,?)",
                           (worker["pipeline_id"], worker_id, hit, key, timestamp()))
            return {"hits": hits, "misses": misses, "renderer_sha256": renderer_sha256}

    def _agent_record_document_renders(self, db, worker, payload):
        """Attach renderer-produced PDF/preview hashes to this child's Markdown drafts."""
        if worker["role"] != "document" or not worker["parent_id"] or not worker["job_id"]:
            raise Conflict("Only a document child can register rendered artifacts.")
        parent = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker["parent_id"],)).fetchone()
        if not parent or parent["state"] != "waiting_child":
            raise Conflict("The submission parent is not waiting for this document child.")
        parent_context = self._agent_decode(parent["context"], {})
        package_id = str(parent_context.get("document_package_id") or "")
        if not package_id:
            raise Conflict("The parent has not published a document package yet.")
        current_job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        current_profile = self.profile_version(db)
        if (not current_job or worker["jd_version"] != current_job["jd_version"]
                or worker["profile_version"] != current_profile):
            raise Conflict("The job or applicant snapshot changed before render registration.")
        package_root = (self.root / "artifacts" / "agent" / package_id).resolve()
        if self.root not in package_root.parents:
            raise Conflict("The rendered package directory escaped the application workspace.")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("Rendered artifacts must be a non-empty list.")
        renderer_sha256 = payload.get("renderer_sha256")
        if renderer_sha256 is not None and (not isinstance(renderer_sha256, str)
                                             or not re.fullmatch(r"[a-fA-F0-9]{64}", renderer_sha256)):
            raise ValueError("renderer_sha256 must be a SHA-256 digest when provided.")
        renderer_sha256 = renderer_sha256.casefold() if renderer_sha256 else ""
        requested = set(parent_context.get("requested_artifacts") or [])
        recorded = []
        seen = set()
        for item in artifacts:
            if not isinstance(item, dict):
                raise ValueError("Each render record must be an object.")
            artifact_id = str(item.get("artifact_id") or "")
            if not artifact_id or artifact_id in seen:
                raise ValueError("Each rendered artifact must identify a unique published draft.")
            seen.add(artifact_id)
            row = db.execute("SELECT * FROM agent_artifacts WHERE id=? AND worker_id=? AND pipeline_id=? "
                             "AND job_id=? AND status='draft'",
                             (artifact_id, worker["id"], worker["pipeline_id"], worker["job_id"])).fetchone()
            if not row or row["kind"] not in requested or row["jd_version"] != current_job["jd_version"] \
                    or row["profile_version"] != current_profile:
                raise Conflict("Render registration does not match a current requested draft.")
            if item.get("source_sha256") != row["sha256"]:
                raise Conflict("The rendered PDF does not cite the current Markdown source hash.")

            def verified_package_file(value, expected_sha, label):
                if not isinstance(value, str) or not value or Path(value).is_absolute():
                    raise ValueError(f"{label} must be a relative package path.")
                candidate = (self.root / value).resolve()
                if package_root not in candidate.parents or not candidate.is_file():
                    raise Conflict(f"{label} is missing or outside the current document package.")
                actual_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if not isinstance(expected_sha, str) or actual_sha != expected_sha:
                    raise Conflict(f"{label} does not match its recorded SHA-256.")
                return candidate.relative_to(self.root).as_posix(), actual_sha

            pdf_relative, pdf_sha256 = verified_package_file(item.get("pdf_path"), item.get("pdf_sha256"), "Rendered PDF")
            previews = item.get("previews") if isinstance(item.get("previews"), list) else []
            if len(previews) > 32:
                raise ValueError("A document render may include at most 32 previews.")
            normalized_previews = []
            for preview in previews:
                if not isinstance(preview, dict):
                    raise ValueError("Each preview record must include a relative path and hash.")
                preview_relative, preview_sha256 = verified_package_file(
                    preview.get("path"), preview.get("sha256"), "Rendered preview")
                normalized_previews.append({"path": preview_relative, "sha256": preview_sha256})
            db.execute("UPDATE agent_artifacts SET pdf_path=?,pdf_sha256=?,renderer_sha256=?,preview_paths=? WHERE id=?",
                       (pdf_relative, pdf_sha256, renderer_sha256,
                        json.dumps(normalized_previews, ensure_ascii=False), artifact_id))
            if renderer_sha256:
                cache_key = digest(row["sha256"] + ":" + renderer_sha256)
                db.execute("INSERT OR IGNORE INTO agent_render_cache(cache_key,source_sha256,renderer_sha256,pdf_path,"
                           "pdf_sha256,previews,created_at) VALUES(?,?,?,?,?,?,?)",
                           (cache_key, row["sha256"], renderer_sha256, pdf_relative, pdf_sha256,
                            json.dumps(normalized_previews, ensure_ascii=False), timestamp()))
            recorded.append({"artifact_id": artifact_id, "kind": row["kind"], "package_id": package_id,
                             "source_sha256": row["sha256"], "pdf_path": pdf_relative,
                             "pdf_sha256": pdf_sha256, "renderer_sha256": renderer_sha256,
                             "previews": normalized_previews})
        self.event(db, "agent.document_renders_recorded", {"worker_id": worker["id"],
                   "package_id": package_id, "artifacts": recorded}, worker["job_id"], "agent_document_renderer")
        return {"recorded": recorded}

    def _agent_finalize_document_child(self, db, worker_id):
        worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
        if not worker or worker["role"] != "document":
            return False
        parent = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker["parent_id"],)).fetchone()
        if not parent or parent["state"] != "waiting_child":
            return False
        context = self._agent_decode(parent["context"], {})
        requested = set(context.get("requested_artifacts") or [])
        package_id = context.get("document_package_id")
        current_job = db.execute("SELECT jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        profile_version = self.profile_version(db)
        latest = self._agent_ready_artifacts(db, worker["pipeline_id"], worker["job_id"],
                                             current_job["jd_version"], profile_version)
        # Drafts from this child are not ready until the child turn completes.
        draft_rows = db.execute("SELECT * FROM agent_artifacts WHERE worker_id=? AND jd_version=? "
                                "AND profile_version=? AND status='draft' ORDER BY kind,version DESC",
                                (worker_id, current_job["jd_version"], profile_version)).fetchall()
        for row in draft_rows:
            if row["kind"] not in latest:
                latest[row["kind"]] = dict(row)
        if not requested or not requested.issubset(latest.keys()):
            return False
        for kind in requested:
            latest_row = latest[kind]
            path = (self.root / latest_row["path"]).resolve()
            package_root = (self.root / "artifacts" / "agent" / package_id).resolve()
            pdf_path = (self.root / latest_row.get("pdf_path", "")).resolve()
            if (package_root not in path.parents or not path.is_file()
                    or digest(path.read_text(encoding="utf-8")) != latest_row["sha256"]
                    or not latest_row.get("pdf_path") or package_root not in pdf_path.parents or not pdf_path.is_file()
                    or hashlib.sha256(pdf_path.read_bytes()).hexdigest() != latest_row.get("pdf_sha256")):
                return False
            db.execute("UPDATE agent_artifacts SET status='ready',ready_at=? WHERE id=?", (timestamp(), latest_row["id"]))
            db.execute("UPDATE agent_artifacts SET status='superseded' WHERE pipeline_id=? AND job_id=? AND kind=? "
                       "AND jd_version=? AND profile_version=? AND status='ready' AND id!=?",
                       (worker["pipeline_id"], worker["job_id"], kind, current_job["jd_version"],
                        profile_version, latest_row["id"]))
        context["documents_ready"] = True
        context["ready_artifacts"] = [{"id": latest[kind]["id"], "kind": kind,
                                        "path": latest[kind]["path"], "filename": latest[kind]["filename"],
                                        "media_type": latest[kind]["media_type"], "sha256": latest[kind]["sha256"],
                                        "pdf_path": latest[kind]["pdf_path"],
                                        "pdf_sha256": latest[kind]["pdf_sha256"],
                                        "preview_paths": self._agent_decode(latest[kind]["preview_paths"], []),
                                        "version": latest[kind]["version"],
                                        "package_id": latest[kind].get("package_id") or package_id,
                                        "status": "ready"}
                                       for kind in sorted(requested)]
        if context.get("manifest_path"):
            manifest_path = (self.root / context["manifest_path"]).resolve()
            if self.root in manifest_path.parents and manifest_path.is_file():
                manifest = self._agent_decode(manifest_path.read_text(encoding="utf-8"), {})
                manifest["status"] = "ready"
                manifest["artifacts"] = context["ready_artifacts"]
                manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
                manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")
                context["manifest_sha256"] = digest(manifest_text)
        db.execute("UPDATE agent_workers SET context=?,state='completed',completed_at=?,updated_at=? WHERE id=?",
                   (json.dumps(context, ensure_ascii=False), timestamp(), timestamp(), worker_id))
        db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,checkpoint='documents_ready',updated_at=?,error='' WHERE id=?",
                   ("same_session" if parent["session_id"] else "new_session", timestamp(), parent["id"]))
        self.event(db, "agent.documents_ready", {"parent_worker_id": parent["id"], "document_worker_id": worker_id,
                   "package_id": package_id, "artifacts": context["ready_artifacts"]}, worker["job_id"], "agent_scheduler")
        return True

    def get_agent_artifact(self, artifact_id):
        with self.connect() as db:
            row = db.execute("SELECT a.*,w.parent_id FROM agent_artifacts a JOIN agent_workers w ON w.id=a.worker_id "
                             "WHERE a.id=?", (artifact_id,)).fetchone()
            if not row:
                raise KeyError(artifact_id)
            result = dict(row)
            path = (self.root / result["path"]).resolve()
            if self.root not in path.parents or not path.is_file():
                raise Conflict("The saved agent artifact is unavailable or outside the workspace.")
            if hashlib.sha256(path.read_bytes()).hexdigest() != result["sha256"]:
                raise Conflict("The saved agent artifact no longer matches its recorded hash.")
            result["path"] = str(path)
            result["source_notes"] = self._agent_decode(result["source_notes"], {})
            result["preview_paths"] = self._agent_decode(result.get("preview_paths"), [])
            if result.get("pdf_path"):
                pdf_path = (self.root / result["pdf_path"]).resolve()
                parent = db.execute("SELECT context FROM agent_workers WHERE id=?", (result["parent_id"],)).fetchone()
                parent_context = self._agent_decode(parent["context"] if parent else "{}", {})
                package_root = (self.root / "artifacts" / "agent" / str(parent_context.get("document_package_id") or "")).resolve()
                if package_root not in pdf_path.parents or not pdf_path.is_file():
                    raise Conflict("The rendered PDF is missing or outside the saved package.")
                if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != result.get("pdf_sha256"):
                    raise Conflict("The rendered PDF no longer matches its recorded hash.")
                result["upload_path"] = str(pdf_path)
            return result

    def get_agent_artifact_variant(self, artifact_id, variant="markdown", index=None):
        """Return a current ready package file after validating its path and digest."""
        if variant not in {"markdown", "pdf", "preview"}:
            raise ValueError("Artifact variant must be markdown, pdf, or preview.")
        with self.connect() as db:
            row = db.execute("SELECT a.*,w.parent_id,j.jd_version AS current_jd_version "
                             "FROM agent_artifacts a JOIN agent_workers w ON w.id=a.worker_id "
                             "JOIN jobs j ON j.id=a.job_id WHERE a.id=?", (artifact_id,)).fetchone()
            if not row:
                raise KeyError(artifact_id)
            if (row["status"] != "ready" or row["jd_version"] != row["current_jd_version"]
                    or row["profile_version"] != self.profile_version(db)):
                raise Conflict("This agent artifact is not current and ready for review.")
            parent = db.execute("SELECT context FROM agent_workers WHERE id=?", (row["parent_id"],)).fetchone()
            parent_context = self._agent_decode(parent["context"] if parent else "{}", {})
            package_id = str(parent_context.get("document_package_id") or "")
            package_root = (self.root / "artifacts" / "agent" / package_id).resolve()
            if not package_id or self.root not in package_root.parents:
                raise Conflict("The saved artifact package is unavailable or outside the workspace.")

            source_path = (self.root / row["path"]).resolve()
            source_names = {"resume": "resume.md", "cover_letter": "cover-letter.md"}
            if (row["kind"] not in source_names or package_root not in source_path.parents
                    or source_path.name != source_names[row["kind"]] or not source_path.is_file()
                    or hashlib.sha256(source_path.read_bytes()).hexdigest() != row["sha256"]):
                raise Conflict("The Markdown artifact no longer matches its current package hash.")

            if variant == "markdown":
                path = source_path
                filename = row["filename"]
                media_type = row["media_type"] or "text/markdown"
                checksum = row["sha256"]
            else:
                rendered_root = self._agent_render_root(package_root,(self.root/str(row['pdf_path'] or '')).resolve())
                if variant == "pdf":
                    expected_name = {"resume": "resume.pdf", "cover_letter": "cover-letter.pdf"}[row["kind"]]
                    path = (self.root / str(row["pdf_path"] or "")).resolve()
                    filename = expected_name
                    media_type = "application/pdf"
                    checksum = row["pdf_sha256"]
                    if path.parent != rendered_root or path.name != expected_name:
                        raise Conflict("The rendered PDF is outside the canonical package path.")
                else:
                    if type(index) is not int or index < 0:
                        raise ValueError("A preview variant needs a non-negative integer index.")
                    previews = self._agent_decode(row["preview_paths"], [])
                    if index >= len(previews) or not isinstance(previews[index], dict):
                        raise KeyError("preview")
                    preview = previews[index]
                    path = (self.root / str(preview.get("path") or "")).resolve()
                    filename = path.name
                    media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                  ".webp": "image/webp"}.get(path.suffix.lower(), "")
                    checksum = preview.get("sha256")
                    if path.parent != rendered_root or not media_type:
                        raise Conflict("The rendered preview is outside the canonical package path.")
            if (not path.is_file() or not isinstance(checksum, str)
                    or self.root not in path.parents
                    or hashlib.sha256(path.read_bytes()).hexdigest() != checksum):
                raise Conflict("The requested artifact variant is missing or no longer matches its saved hash.")
            return {"id": artifact_id, "kind": row["kind"], "variant": variant,
                    "path": str(path), "filename": filename, "media_type": media_type,
                    "sha256": checksum, "package_id": package_id}

    def _agent_action_allowed(self, role, action):
        if action == "get_browser_guidance":
            return role in {"discovery", "evaluator", "submission"}
        if action == "save_resume_base":
            return role == "document"
        if action == "browser_control":
            return role in {"discovery", "evaluator", "submission"}
        allowed = {
            "discovery": {"get_context", "lookup_listing", "lookup_listings", "record_listing", "record_listing_batch",
                          "capture_listing_batch", "lookup_workflow_plan", "save_workflow_plan",
                          "finish_discovery", "ask_question", "request_approval", "checkpoint"},
            "evaluator": {"get_context", "retrieve_sources", "lookup_listing", "lookup_listings",
                          "lookup_workflow_plan", "save_workflow_plan",
                          "update_job_description", "ask_question", "finish_evaluation",
                          "request_approval", "checkpoint"},
            "submission": {"get_context", "retrieve_sources", "lookup_listing", "lookup_listings",
                           "lookup_workflow_plan", "save_workflow_plan",
                           "ask_question", "request_documents", "request_approval", "begin_final_action",
                           "finish_submission", "checkpoint"},
            "document": {"get_context", "retrieve_sources", "ask_question", "publish_documents", "edit_documents",
                         "record_document_renders", "checkpoint"},
        }
        return action in allowed.get(role, set())

    def agent_action(self, worker_id, token, action, payload):
        if not isinstance(payload, dict):
            raise ValueError("Agent action payload must be an object.")
        if not isinstance(action, str) or len(action) > 64:
            raise ValueError("Agent action is invalid.")
        now_epoch = time.time()
        with self.connect(write=True) as db:
            worker = db.execute("SELECT w.*,p.mode,p.stop_requested,p.status AS pipeline_status,p.company_id,p.agent_test_only "
                                "FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?",
                                (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
            if (worker["mode"] != "agentic" or worker["state"] not in {
                        "running", "waiting_question", "waiting_approval", "waiting_child"}
                    or not worker["capability_sha256"] or worker["capability_sha256"] != token_hash
                    or not worker["capability_expires_at"] or worker["capability_expires_at"] < now_epoch):
                raise Conflict("Agent capability is invalid, expired, or no longer active.")
            if worker["state"] != "running" and action != "checkpoint":
                raise Conflict("This worker is already at a durable wait checkpoint.")
            db.execute("UPDATE agent_workers SET capability_expires_at=?,updated_at=? WHERE id=?",
                       (now_epoch + 3600, timestamp(), worker_id))
            if worker["stop_requested"] or worker["pipeline_status"] in {"stopping", "stopped", "completed"}:
                if action not in {"get_context", "checkpoint"}:
                    raise Conflict("Pipeline stop was recorded; no further work action may start.")
            if not self._agent_action_allowed(worker["role"], action):
                raise Conflict("This worker role is not allowed to perform that action.")
            recovery = self._agent_decode(worker["context"], {}).get("reconciliation_recovery", {})
            if recovery.get("read_only"):
                allowed = {"get_context", "retrieve_sources", "get_browser_guidance", "checkpoint",
                           "browser_control", "finish_submission", "request_approval"}
                if (action not in allowed
                        or action == "browser_control" and payload.get("operation") != "complete_recovery"
                        or action == "finish_submission" and payload.get("status") not in {"submitted", "uncertain"}):
                    raise Conflict("This approved recovery is read-only reconciliation; no tab retry or application replay is authorized.")
            if action == "get_browser_guidance":
                if self._agent_decode(worker["context"], {}).get("execution", {}).get("browser_backend") == "playwright":
                    path = self.root / "prompts/browser_playwright.md"
                    content = path.read_text(encoding="utf-8")
                    return {"document": payload.get("document"), "backend": "playwright", "source_path": str(path),
                            "sha256": digest(content), "content": content}
                document = payload.get("document")
                if document not in {"skill", "guidance", "confirmations", "api"}:
                    raise ValueError("Choose an installed browser-guidance document.")
                plugin_root = Path.home() / ".codex/plugins/cache/openai-bundled/computer-use"
                candidates = sorted(plugin_root.glob("*/skills/computer-use/SKILL.md"), key=lambda path: path.stat().st_mtime, reverse=True)
                if not candidates:
                    raise Conflict("The installed Computer Use skill was not found.")
                skill = candidates[0].resolve()
                path = skill if document == "skill" else (skill.parent / "../../docs" / (document + ".md")).resolve()
                if plugin_root.resolve() not in path.parents:
                    raise Conflict("Browser guidance resolved outside the installed plugin.")
                content = path.read_text(encoding="utf-8-sig")
                return {"document": document, "source_path": str(path), "sha256": digest(content), "content": content}
            if action == "get_context":
                return self._agent_context_for_worker(db, worker, payload)
            if action == "browser_control":
                return self._agent_browser_control_action(db, worker, payload)
            if action == "retrieve_sources":
                return self._agent_retrieve_sources(db, worker, payload)
            if action == "save_resume_base":
                family, summary = payload.get("family"), payload.get("summary")
                if not isinstance(family, str) or not 1 <= len(family.strip()) <= 100 or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 600:
                    raise ValueError("Give an agent-chosen role family and concise base description.")
                artifact = db.execute("SELECT * FROM agent_artifacts WHERE id=? AND worker_id=? AND kind='resume'",
                                      (payload.get("artifact_id"), worker_id)).fetchone()
                if not artifact or not artifact["pdf_sha256"] or not artifact["preview_paths"] or artifact["profile_version"] != self.profile_version(db):
                    raise Conflict("A resume base must be this child's current rendered and visually reviewed resume.")
                existing = db.execute("SELECT id FROM agent_resume_bases WHERE artifact_id=?", (artifact["id"],)).fetchone()
                base_id = existing[0] if existing else uuid.uuid4().hex
                db.execute("INSERT INTO agent_resume_bases VALUES(?,?,?,?,?,?) ON CONFLICT(artifact_id) DO UPDATE SET "
                           "family=excluded.family,summary=excluded.summary",
                           (base_id, artifact["id"], family.strip(), summary.strip(), self.profile_version(db), timestamp()))
                return {"ref": "resume_base:" + base_id, "published_when_child_completes": True}
            if action == "checkpoint":
                checkpoint = payload.get("checkpoint")
                if not isinstance(checkpoint, str) or not checkpoint.strip() or len(checkpoint) > 256:
                    raise ValueError("Checkpoint must be a short non-empty string.")
                context = self._agent_decode(worker["context"], {})
                if payload.get("context") is not None:
                    notes = self._agent_validate_notes(payload["context"])
                    for reserved in ("browser_freshness", "input_recovery", "reconciliation_recovery"):
                        if reserved in notes and (reserved not in context or notes[reserved] != context[reserved]):
                            raise Conflict(reserved + " is host-managed; checkpoint ordinary progress separately and use the browser control acknowledgement.")
                    context.update(notes)
                db.execute("UPDATE agent_workers SET checkpoint=?,context=?,updated_at=? WHERE id=?",
                           (checkpoint.strip(), json.dumps(context, ensure_ascii=False), timestamp(), worker_id))
                return {"worker_id": worker_id, "checkpoint": checkpoint.strip()}
            if action == "lookup_listing":
                return self._agent_lookup_listing(db, worker, payload)
            if action == "lookup_listings":
                return self._agent_lookup_listings(db, worker, payload)
            if action == "lookup_workflow_plan":
                return self._agent_lookup_workflow_plan(db, worker, payload)
            if action == "save_workflow_plan":
                return self._agent_save_workflow_plan(db, worker, payload)
            if action == "record_listing":
                return self._agent_record_listing(db, worker, payload)
            if action == "record_listing_batch":
                return self._agent_record_listing_batch(db, worker, payload)
            if action == "capture_listing_batch":
                return self._agent_capture_listing_batch(db, worker, payload)
            if action == "finish_discovery":
                status = payload.get("status")
                if status not in {"complete", "incomplete", "blocked"}:
                    raise ValueError("Discovery status must be complete, incomplete, or blocked.")
                detail = str(payload.get("detail") or "")[:4000]
                coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
                now = timestamp()
                continuation = None
                if status == "incomplete":
                    context = self._agent_decode(worker["context"], {})
                    continuation = self._agent_discovery_continuation(
                        db, worker, context, coverage, bool(worker["agent_test_only"]))
                elif status == "blocked" and not detail:
                    detail = "Discovery reported a blocker without a specific explanation."
                discovery = {"status": status, "detail": detail, "coverage": coverage,
                             "worker_id": worker_id, "reported_at": now}
                if continuation is not None:
                    discovery["continuation"] = {key: value for key, value in continuation.items()
                                                 if key != "error"}
                db.execute("UPDATE pipelines SET discovery_status=?,discovery_payload=?,stage='processing',updated_at=? WHERE id=?",
                           (status, json.dumps(discovery, ensure_ascii=False), now, worker["pipeline_id"]))
                if status == "incomplete":
                    # Do not queue yet: the current CLI turn still needs to
                    # acknowledge this tool call and exit. finish_agent_turn
                    # promotes this durable handoff to queued after transport
                    # cleanup, avoiding a second process resuming the same tab.
                    db.execute("UPDATE agent_workers SET result=?,resume_mode=?,updated_at=?,capability_sha256='',"
                               "capability_expires_at=NULL,error=? WHERE id=?",
                               (json.dumps(discovery, ensure_ascii=False), continuation["resume_mode"], now,
                                continuation.get("error", ""), worker_id))
                else:
                    state = "completed" if status == "complete" else "failed"
                    db.execute("UPDATE agent_workers SET state=?,result=?,completed_at=?,updated_at=?,capability_sha256='',"
                               "capability_expires_at=NULL,error=? WHERE id=?",
                               (state, json.dumps(discovery, ensure_ascii=False), now, now,
                                "" if status == "complete" else detail, worker_id))
                self.event(db, "agent.discovery_finished", discovery, actor="agent_discovery")
                self._agent_refresh_pipeline(db, worker["pipeline_id"])
                return discovery
            if action == "update_job_description":
                return self._agent_update_description(db, worker, payload)
            if action == "ask_question":
                question = payload.get("question")
                if not isinstance(question, str) or not question.strip() or len(question) > 12000:
                    raise ValueError("A specific question is required.")
                context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
                fact_key = payload.get("fact_key")
                fact_scope = payload.get("fact_scope")
                if fact_key is not None:
                    if (not isinstance(fact_key, str) or not fact_key.strip() or len(fact_key.strip()) > 160
                            or fact_scope not in {"applicant", "job"}):
                        raise ValueError("A shared fact question needs an exact agent-chosen fact_key and applicant/job scope.")
                    if worker["role"] == "discovery" and fact_scope == "applicant":
                        raise Conflict("Company discovery cannot ask for applicant facts.")
                    if fact_scope == "job" and not worker["job_id"]:
                        raise Conflict("A job-scoped fact question needs a job-bound worker.")
                    scope_id = "" if fact_scope == "applicant" else f"{worker['pipeline_id']}:{worker['job_id']}"
                    prior = db.execute("SELECT f.*,i.response,i.provenance FROM agent_fact_questions f "
                                       "JOIN agent_inbox i ON i.id=f.inbox_item_id WHERE f.fact_scope=? "
                                       "AND f.scope_id=? AND f.fact_key=? AND f.state='answered' "
                                       "ORDER BY f.resolved_at DESC LIMIT 1",
                                       (fact_scope, scope_id, fact_key.strip())).fetchone()
                    if prior:
                        return {"state": "fact_already_answered", "fact_key": fact_key.strip(),
                                "fact_scope": fact_scope,
                                "answer": self._agent_decode(prior["response"], prior["response"]),
                                "provenance": prior["provenance"], "source_item_id": prior["inbox_item_id"]}
                    pending_fact = db.execute("SELECT f.*,i.question,i.context,i.response_hint,i.summary,i.action "
                                              "FROM agent_fact_questions f JOIN agent_inbox i ON i.id=f.inbox_item_id "
                                              "WHERE f.fact_scope=? AND f.scope_id=? AND f.fact_key=? AND f.state='pending'",
                                              (fact_scope, scope_id, fact_key.strip())).fetchone()
                    if pending_fact:
                        db.execute("INSERT OR IGNORE INTO agent_fact_subscribers(fact_id,worker_id,pipeline_id,job_id,created_at) "
                                   "VALUES(?,?,?,?,?)", (pending_fact["id"], worker_id, worker["pipeline_id"],
                                                          worker["job_id"], timestamp()))
                        db.execute("UPDATE agent_workers SET state='waiting_question',checkpoint=?,updated_at=? WHERE id=?",
                                   ("shared_fact:" + pending_fact["id"], timestamp(), worker_id))
                        self.event(db, "agent.fact_question_subscribed", {"fact_id": pending_fact["id"],
                                   "worker_id": worker_id, "fact_key": fact_key.strip(),
                                   "fact_scope": fact_scope}, worker["job_id"], "agent")
                        inbox_id = pending_fact["inbox_item_id"] if "inbox_item_id" in pending_fact.keys() else None
                        if not inbox_id:
                            inbox_id = db.execute("SELECT inbox_item_id FROM agent_fact_questions WHERE id=?",
                                                  (pending_fact["id"],)).fetchone()[0]
                        result = {"inbox_item_id": inbox_id, "state": "waiting_question",
                                  "created": False, "shared": True, "fact_key": fact_key.strip()}
                    else:
                        question_context = {**context, "fact_key": fact_key.strip(), "fact_scope": fact_scope}
                        inbox, created = self._agent_insert_inbox(db, worker, "question",
                            dedupe_key="fact:" + fact_scope + ":" + scope_id + ":" + fact_key.strip(),
                            question=question.strip(), context=question_context,
                            response_hint=str(payload.get("response_hint") or "")[:2000])
                        fact_id = uuid.uuid4().hex
                        db.execute("INSERT INTO agent_fact_questions(id,fact_scope,scope_id,fact_key,state,inbox_item_id,created_at) "
                                   "VALUES(?,?,?,?,'pending',?,?)",
                                   (fact_id, fact_scope, scope_id, fact_key.strip(), inbox["id"], timestamp()))
                        db.execute("INSERT INTO agent_fact_subscribers(fact_id,worker_id,pipeline_id,job_id,created_at) "
                                   "VALUES(?,?,?,?,?)", (fact_id, worker_id, worker["pipeline_id"],
                                                          worker["job_id"], timestamp()))
                        result = {"inbox_item_id": inbox["id"], "state": "waiting_question", "created": created,
                                  "shared": True, "fact_key": fact_key.strip()}
                else:
                    inbox, created = self._agent_insert_inbox(db, worker, "question",
                        dedupe_key=str(payload.get("dedupe_key") or "")[:256], question=question.strip(),
                        context=context, response_hint=str(payload.get("response_hint") or "")[:2000])
                    result = {"inbox_item_id": inbox["id"], "state": "waiting_question", "created": created}
                self._agent_refresh_pipeline(db, worker["pipeline_id"])
                return result
            if action == "finish_evaluation":
                decision = payload.get("decision")
                if decision not in {"approve", "discard", "ask"}:
                    raise ValueError("Evaluation decision must be approve, discard, or ask.")
                if decision == "ask":
                    pending = db.execute("SELECT id FROM agent_inbox WHERE worker_id=? AND kind='question' AND state='pending' "
                                         "ORDER BY created_at DESC LIMIT 1", (worker_id,)).fetchone()
                    if not pending:
                        raise Conflict("An ask decision needs a pending job-local question.")
                    return {"decision": "ask", "inbox_item_id": pending["id"], "state": "waiting_question"}
                return self._agent_finish_evaluation(db, worker, payload, decision)
            if action == "request_documents":
                return self._agent_request_documents(db, worker, payload)
            if action == 'edit_documents':
                return self._agent_edit_documents(db,worker,payload)
            if action == "publish_documents":
                return self._agent_publish_documents(db, worker, payload)
            if action == "record_document_renders":
                return self._agent_record_document_renders(db, worker, payload)
            if action == "request_approval":
                summary = str(payload.get("summary") or "")[:12000]
                action_name = str(payload.get("action") or "")[:256]
                if not action_name:
                    raise ValueError("An approval action name is required.")
                kind = "first_application_review" if action_name == "first_application_review" else "approval"
                observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
                if worker["job_id"]:
                    job_snapshot = db.execute("SELECT title,url,jd_version FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
                    current_profile = self.profile_version(db)
                    if (worker["jd_version"] != job_snapshot["jd_version"]
                            or worker["profile_version"] != current_profile):
                        raise Conflict("This worker's job or applicant snapshot is stale; refresh it before requesting approval.")
                    observation = {**observation, "job_title": job_snapshot["title"], "job_url": job_snapshot["url"],
                                   "jd_version": job_snapshot["jd_version"], "profile_version": current_profile}
                    ready = self._agent_ready_artifacts(db, worker["pipeline_id"], worker["job_id"],
                                                        job_snapshot["jd_version"], current_profile)
                    observation["ready_artifacts"] = self._agent_artifact_snapshot(ready)
                    if ready:
                        # Keep reviewable text and links separate from the
                        # compact snapshot that begin_final_action compares.
                        observation["review_artifacts"] = self._agent_review_artifacts(db, ready)
                inbox, created = self._agent_insert_inbox(db, worker, kind,
                    dedupe_key="approval:" + action_name + ":" + str(payload.get("dedupe_key") or ""),
                    context=observation,
                    summary=summary, action=action_name)
                self._agent_refresh_pipeline(db, worker["pipeline_id"])
                return {"inbox_item_id": inbox["id"], "kind": kind, "state": "waiting_approval", "created": created}
            if action == "begin_final_action":
                return self._agent_begin_final_action(db, worker, payload)
            if action == "finish_submission":
                return self._agent_finish_submission(db, worker, payload)
            raise ValueError("Unsupported agent action.")

    @staticmethod
    def _agent_career_scope():
        # Return a detached object so serializers/callers cannot alter the
        # canonical policy used for later worker contexts.
        return json.loads(json.dumps(AGENT_CAREER_SCOPE, ensure_ascii=False))

    def _agent_pipeline_filters(self, pipeline):
        filters = self._agent_decode(pipeline["filters"], {})
        if not isinstance(filters, dict):
            filters = {}
        else:
            filters = dict(filters)
        # Always inject the current mandatory scope in future/resumed agent
        # context, without rewriting old pipeline history or stored decisions.
        filters["career_scope"] = self._agent_career_scope()
        return filters

    def set_browser_grouping(self, provenance):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("The user authorization for shared browser grouping is required.")
        policy = {"enabled": True, "applications": "Pipeline · Applications", "discovery": "Pipeline · Discovery",
                  "max_groups": 2, "authorized_at": timestamp(), "provenance": provenance.strip()}
        with self.connect(write=True) as db:
            db.execute("INSERT INTO meta(key,value) VALUES('browser_grouping',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(policy),))
            self.event(db, "policy.browser_grouping_updated", policy, actor="user")
        return policy

    def set_amazon_application_error_recovery(self, company_id, provenance):
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("User authorization for application-page recovery is required.")
        with self.connect(write=True) as db:
            company = db.execute("SELECT careers_url FROM companies WHERE id=?", (company_id,)).fetchone()
            host = (urlsplit(company[0]).hostname or "").removeprefix("www.") if company else ""
            if host != "amazon.jobs":
                raise Conflict("This recovery route is scoped to Amazon.jobs only.")
            policy = {"enabled": True, "epoch": uuid.uuid4().hex,
                      "dashboard_url": "https://www.amazon.jobs/en-US/applicant",
                      "reload_limit": 1, "dashboard_reopen_limit": 1,
                      "scope": "same job and saved worker; no replay of a possibly sent application",
                      "authorized_at": timestamp(), "provenance": provenance.strip()}
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("application_error_recovery:" + company_id, json.dumps(policy)))
            self.event(db, "policy.application_error_recovery_updated", {"company_id": company_id, **policy}, actor="user")
        return policy

    def _agent_runtime_controls(self, db, worker, pipeline):
        """Read live host controls on the same connection as agent context."""
        raw = db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
        stored_quota = self._agent_decode(raw[0] if raw else "{}", {})
        if not isinstance(stored_quota, dict):
            stored_quota = {}
        quota = {
            "paused": bool(stored_quota.get("paused")),
            "provider_error": stored_quota.get("provider_error"),
            "reason": stored_quota.get("reason"),
            "paused_at": stored_quota.get("paused_at"),
        }
        company_auth = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? AND control_type='company_auth'",
                                  ("company:" + str(pipeline["company_id"]),)).fetchone()
        connector_circuit = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=? "
                                       "AND control_type='connector_circuit'", (self._agent_connector_scope(worker),)).fetchone()
        company_control = self._agent_browser_control_view(company_auth, "company_auth")
        circuit_control = self._agent_browser_control_view(connector_circuit, "connector_circuit")
        signin = db.execute("SELECT value FROM meta WHERE key=?", ("agent_signin_automation:" + worker["pipeline_id"],)).fetchone() if worker["role"] == "submission" else None
        grouping = db.execute("SELECT value FROM meta WHERE key='browser_grouping'").fetchone()
        page_recovery = db.execute("SELECT value FROM meta WHERE key=?", ("application_error_recovery:" + pipeline["company_id"],)).fetchone()
        email_policy=db.execute("SELECT value FROM meta WHERE key='routine_email_tokens'").fetchone()
        browser_session={}
        if self._agent_decode(worker['context'],{}).get('execution',{}).get('browser_backend')=='playwright':
            from .browser_sessions import BrowserSessions
            state=BrowserSessions(self.root).inspect(worker['id'])
            browser_session={'browser_session':{k:state.get(k) for k in ('state','generation','previous_generation','live_state_preserved','worker_id')}}
        return {"quota_paused": quota["paused"], "quota": quota,
                **browser_session,
                **({'routine_email_tokens':self._agent_decode(email_policy[0],{})} if email_policy else {}),
                "browser_freshness": self._agent_decode(worker["context"], {}).get("browser_freshness", {}),
                "reconciliation_recovery": self._agent_decode(worker["context"], {}).get("reconciliation_recovery", {}),
                **({"browser_grouping": self._agent_decode(grouping[0], {})} if grouping else {}),
                **({"application_error_recovery": self._agent_decode(page_recovery[0], {})} if page_recovery else {}),
                **({"signin_automation": self._agent_decode(signin["value"], {})} if signin else {}),
                **({"routine_document_uploads": self._agent_routine_upload_policy(db, worker, pipeline)} if worker["role"] == "submission" else {}),
                "pipeline_status": pipeline["status"], "stop_requested": bool(pipeline["stop_requested"]),
                "worker_state": worker["state"], "browser_controls": {
                    "company_authentication": company_control, "connector_circuit": circuit_control,
                    "company_authentication_required": company_control["required"],
                    "connector_circuit_open": circuit_control["open"]}, "checked_at": timestamp()}

    @staticmethod
    def _agent_is_input_timeout(payload):
        if payload.get("authentication_redacted"):
            return payload.get("browser_input_timeout") is True
        item = payload.get("transport_event", payload).get("item", {})
        result = item.get("result") or {}
        texts = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if re.search(r"permission denied|access denied|access was declined|user denied|blocked by policy|browser use was stopped", texts, re.I):
            return False
        return bool(re.search(r"Timed out after \d+ms waiting for CDP command Input\.dispatchMouseEvent\.", texts))

    @staticmethod
    def _agent_is_observation_timeout(payload):
        if payload.get("authentication_redacted"):
            return payload.get("browser_observation_timeout") is True
        item = payload.get("transport_event", payload).get("item", {})
        texts = "\n".join(c.get("text", "") for c in (item.get("result") or {}).get("content", []) if c.get("type") == "text")
        if re.search(r"permission denied|access denied|access was declined|user denied|blocked by policy|browser use was stopped", texts, re.I):
            return False
        return bool(re.search(r"Timed out after \d+ms waiting for CDP command (?:Page\.getFrameTree|Runtime\.evaluate)\.", texts))

    def _agent_scope_postsend_observation_recheck(self, db, worker, event_id):
        context = self._agent_decode(worker["context"], {})
        if (worker["role"] != "submission" or not self._context_may_have_sent_final(context, worker["checkpoint"])
                or context.get("reconciliation_recovery", {}).get("read_only")):
            return False
        event = db.execute("SELECT payload FROM agent_runtime_events WHERE id=? AND worker_id=? AND session_id=? AND tool_name='cua_repl.js' AND success=0",
                           (event_id, worker["id"], worker["session_id"])).fetchone()
        if not event or not self._agent_is_observation_timeout(self._agent_decode(event[0], {})):
            return False
        context["reconciliation_recovery"] = {"read_only": True, "source_event_id": event_id,
            "authorization_type": "existing_application_scope_read_only", "fresh_tab_budget": 0,
            "final_action_replay_authorized": False, "issued_at": timestamp(),
            "instruction": "One bounded read-only re-observation of the same application after a technical read timeout; prefer a documented screenshot if DOM reading failed. No editing, upload, fresh tab, CAPTCHA, binding acceptance or Submit replay. Record a verified receipt or finish uncertain."}
        db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?", (json.dumps(context), timestamp(), worker["id"]))
        self.event(db, "agent.postsend_observation_scoped", {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
            "source_event_id": event_id, "read_only": True, "human_approval_claimed": False,
            "final_action_replay_authorized": False}, worker["job_id"], "agent_runtime")
        return True

    def _agent_observed_application_url(self, db, worker, payload):
        if not worker["job_id"]:
            return None
        job = db.execute("SELECT url,requisition_id FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        if not job or (urlsplit(job["url"]).hostname or "").removeprefix("www.") not in {"amazon.jobs","job-boards.greenhouse.io"}:
            return None
        item = payload.get("transport_event", payload).get("item", {})
        result = item.get("result") or {}
        raw = result.get("_meta", {}).get("browser_use", {}).get("url")
        if item.get("server") == "playwright":
            # Only tool-generated page metadata, before any site snapshot or
            # code block; never infer the application URL from arbitrary text.
            for block in result.get("content", []):
                text = block.get("text", "") if isinstance(block, dict) else ""
                page = text.split("### Page\n", 1)[-1] if "### Page\n" in text else ""
                page = page.split("### Snapshot", 1)[0].split("```", 1)[0]
                match = re.search(r"(?m)^- Page URL: (https://[^\s]+)$", page)
                if match:
                    raw = match.group(1)
                    break
        if payload.get("authentication_redacted"):
            raw = payload.get("observed_application_url")
        if not isinstance(raw, str):
            return None
        parsed = urlsplit(raw)
        expected=urlsplit(job['url'])
        if expected.hostname=='job-boards.greenhouse.io':
            if (parsed.scheme=='https' and not parsed.username and not parsed.password
                    and parsed.hostname==expected.hostname and parsed.path.rstrip('/')==expected.path.rstrip('/')):
                return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
            return None
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or (parsed.hostname or "").removeprefix("www.") != "amazon.jobs"
                or not re.search(r"/applicant/jobs/" + re.escape(str(job["requisition_id"])) + r"/(?:apply|summary)/?$", parsed.path)):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _agent_latest_cua_event(self, db, worker, *, success):
        if not worker["session_id"]:
            return None
        row = db.execute("SELECT id,session_id,payload FROM agent_runtime_events WHERE worker_id=? AND session_id=? "
                         "AND (tool_name='cua_repl.js' OR tool_name LIKE 'playwright.%') AND success=? ORDER BY rowid DESC LIMIT 1",
                         (worker["id"], worker["session_id"], int(bool(success)))).fetchone()
        if not row:
            return None
        payload = self._agent_decode(row["payload"], {})
        if not isinstance(payload, dict) or payload.get("transport_event_truncated"):
            return None
        transport = payload.get("transport_event")
        item = transport.get("item") if isinstance(transport, dict) else None
        if (not isinstance(item, dict) or item.get("type") != "mcp_tool_call"
                or not (item.get("server") == "cua_repl" and item.get("tool") == "js" or item.get("server") == "playwright")):
            return None
        return {"event_id": row["id"], "session_id": row["session_id"],
                "application_url": self._agent_observed_application_url(db, worker, payload),
                "verification": payload.get('verification_observation') or self._agent_verification_observation(db,worker,payload)}

    def _agent_verification_observation(self,db,worker,payload):
        url=self._agent_observed_application_url(db,worker,payload)
        if not url or urlsplit(url).hostname!='job-boards.greenhouse.io':
            return None
        item=payload.get('transport_event',payload).get('item',{})
        blocks=(item.get('result') or {}).get('content',[])
        for block in blocks:
            text=block.get('text','') if isinstance(block,dict) else ''
            if '### Snapshot' not in text:
                continue
            snapshot=text.split('### Snapshot',1)[1]
            if 'enter the 8-character code to confirm' not in snapshot:
                continue
            group=snapshot.split('enter the 8-character code to confirm',1)[1]
            button=re.search(r'(?m)^\s*- button "Submit application"([^\n]*)',group)
            if not button:
                continue
            fields=group[:button.start()]
            count=len(re.findall(r'(?m)^\s*- textbox(?: "Security code")? \[[^\n]+?\]: (?:"[A-Za-z0-9]"|[A-Za-z0-9])\s*$',fields))
            proof = {'present':True,'filled_boxes':count,'submit_enabled':'[disabled]' not in button.group(1)}
            if re.search(r'(?m)^\s*- (?:paragraph|alert)(?: \[[^\n]+\])?: Invalid security code\s*$', fields):
                proof['invalid_code'] = True
            return proof
        return None

    def _agent_scope_isolated_tab_approval(self, db, worker_id):
        """Keep an isolated job retry actionable without tying it to a later global outage."""
        worker = db.execute("SELECT w.*,j.url FROM agent_workers w JOIN jobs j ON j.id=w.job_id WHERE w.id=?", (worker_id,)).fetchone()
        context = self._agent_decode(worker["context"], {}) if worker else {}
        proof_id = context.get("independent_browser_health_event_id")
        proof = db.execute("SELECT e.id FROM agent_runtime_events e JOIN agent_workers w ON w.id=e.worker_id "
                           "WHERE e.id=? AND e.success=1 AND e.tool_name='cua_repl.js' AND w.id!=? AND w.pipeline_id=?",
                           (proof_id, worker_id, worker["pipeline_id"] if worker else "")).fetchone()
        if (not worker or worker["turn_active"] or context.get("browser_failure_scope") != "job" or not proof
                or self._context_may_have_sent_final(context, worker["checkpoint"])):
            raise Conflict("Only a verified isolated, inactive, unsent job timeout can be scoped locally.")
        changed = []
        for item in db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND state='pending' AND action='browser_circuit_recovery'", (worker_id,)).fetchall():
            details = self._agent_decode(item["context"], {})
            failure = db.execute("SELECT 1 FROM agent_runtime_events WHERE id=? AND worker_id=? AND success=0",
                                 (details.get("source_event_id"), worker_id)).fetchone()
            if not failure:
                raise Conflict("The isolated request must retain its actual failed browser observation.")
            details.update(scope="job:" + worker["job_id"], previous_scope=details.get("scope"),
                           independent_health_event_id=proof_id, jd_version=worker["jd_version"],
                           profile_version=worker["profile_version"], job_url=worker["url"])
            summary = ("Allow one bounded retry of this job's worker-owned browser tab at its canonical job URL. "
                       "Its earlier navigation timeout was isolated after another application page worked. "
                       "This request covers tab recovery only; application agreements and CAPTCHAs keep their own exact approvals.")
            db.execute("UPDATE agent_inbox SET action='browser_tab_recovery',context=?,summary=?,updated_at=? WHERE id=?",
                       (json.dumps(details), summary, timestamp(), item["id"]))
            self.event(db, "agent.tab_recovery_approval_scoped", {"pipeline_id": worker["pipeline_id"],
                       "worker_id": worker_id, "item_id": item["id"], "source_event_id": details.get("source_event_id"),
                       "independent_health_event_id": proof_id, "decision_unchanged": True}, worker["job_id"], "supervisor")
            changed.append(item["id"])
        return changed

    def isolate_tab_navigation_timeout(self, worker_id, healthy_event_id):
        """Operator recovery: quarantine one tab, not a verified healthy connector.

        Requires the exact per-tab CDP timeout and a later, independent actual
        application-page observation. Does not retry the failed job, approve
        any inbox item, or claim that its page recovered.
        """
        with self.connect(write=True) as db:
            gate = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key='connector:cua_repl' AND state='open'").fetchone()
            owner = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker_id,)).fetchone()
            if (not gate or gate["worker_id"] != worker_id or not owner or owner["turn_active"]
                    or owner["state"] != "waiting_approval"):
                raise Conflict("Only an inactive, waiting navigation owner can be isolated.")
            context = self._agent_decode(owner["context"], {})
            if self._context_may_have_sent_final(context, owner["checkpoint"]):
                raise Conflict("A possibly sent application needs reconciliation, not navigation isolation.")
            failures = self._agent_decode(gate["event_ids"], [])
            failure = db.execute("SELECT rowid AS sequence,* FROM agent_runtime_events WHERE id=? AND worker_id=? AND success=0",
                                 (failures[0] if failures else "", worker_id)).fetchone()
            healthy = db.execute("SELECT e.rowid AS sequence,e.*,w.pipeline_id,j.requisition_id FROM agent_runtime_events e "
                                 "JOIN agent_workers w ON w.id=e.worker_id LEFT JOIN jobs j ON j.id=w.job_id "
                                 "WHERE e.id=? AND e.tool_name='cua_repl.js' AND e.success=1", (healthy_event_id,)).fetchone()

            def text_result(row):
                if not row:
                    return ""
                payload = self._agent_decode(row["payload"], {})
                item = payload.get("transport_event", payload).get("item", {})
                return "\n".join(b.get("text", "") for b in item.get("result", {}).get("content", []) if b.get("type") == "text")

            if not re.fullmatch(r"Timed out after \d+ms waiting for CDP command Page\.getFrameTree\.", text_result(failure).strip()):
                raise Conflict("This failure is not the known per-tab navigation timeout.")
            observed = text_result(healthy)
            if (not healthy or healthy["worker_id"] == worker_id or healthy["pipeline_id"] != owner["pipeline_id"]
                    or healthy["sequence"] <= failure["sequence"] or healthy["created_at"] < gate["updated_at"]
                    or not observed.startswith("Browser tab:")
                    or not ("AXWebArea" in observed or "The following is a diff" in observed)
                    or not healthy["requisition_id"] or str(healthy["requisition_id"]) not in observed.splitlines()[0]):
                raise Conflict("A later independent application-page observation is required.")
            self._agent_set_browser_control(db, gate["scope_key"], gate["control_type"], "closed",
                "Independent application page is working; the failed job remains paused for its own tab navigation.",
                "Supervisor verified actual independent browser evidence; no job retry or user approval was granted.",
                worker_id, [healthy_event_id])
            context["browser_failure_scope"] = "job"
            context["independent_browser_health_event_id"] = healthy_event_id
            db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?", (json.dumps(context), timestamp(), worker_id))
            self._agent_scope_isolated_tab_approval(db, worker_id)
            for waiter in db.execute("SELECT w.* FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                     "WHERE w.state='waiting_browser' AND w.turn_active=0 AND p.stop_requested=0").fetchall():
                notes = self._agent_decode(waiter["context"], {})
                if notes.get("browser_wait_scope") != gate["scope_key"] or self._agent_pending_inbox_wait(db, waiter["id"]):
                    continue
                notes.pop("browser_wait_scope", None)
                db.execute("UPDATE agent_workers SET state='queued',context=?,updated_at=? WHERE id=?",
                           (json.dumps(notes), timestamp(), waiter["id"]))
            self.event(db, "agent.tab_navigation_isolated", {"worker_id": worker_id,
                "pipeline_id": owner["pipeline_id"], "failure_event_ids": failures,
                "independent_health_event_id": healthy_event_id, "job_still_waiting": True}, owner["job_id"], "supervisor")
            self._agent_refresh_pipeline(db, owner["pipeline_id"])
            return {"connector_state": "closed", "job_state": "waiting_approval", "job_retry_authorized": False}

    def _agent_connector_scope(self, worker):
        execution = self._agent_decode(worker["context"], {}).get("execution", {})
        return "browser:playwright:" + worker["id"] if execution.get("browser_backend") == "playwright" else "connector:cua_repl"

    def _agent_browser_control_action(self, db, worker, payload):
        operation = payload.get("operation")
        if operation not in {"acquire_auth", "complete_auth", "open_circuit", "complete_recovery"}:
            raise ValueError("Unsupported browser control operation.")
        auth = operation in {"acquire_auth", "complete_auth"}
        scope = "company:" + worker["company_id"] if auth else self._agent_connector_scope(worker)
        kind = "company_auth" if auth else "connector_circuit"
        prior = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=?", (scope,)).fetchone()
        reason = str(payload.get("reason") or "")[:4000]
        if operation in {"acquire_auth", "open_circuit"}:
            if auth and prior and prior["state"] == "open" and prior["worker_id"] != worker["id"] and self._agent_auth_owner_unavailable(db, prior):
                self._agent_transfer_auth_owner(db, prior, worker)
                prior = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key=?", (scope,)).fetchone()
            if prior and prior["state"] == "open" and prior["worker_id"] != worker["id"]:
                context = self._agent_decode(worker["context"], {})
                context["browser_wait_scope"] = scope
                db.execute("UPDATE agent_workers SET state='waiting_browser',context=?,checkpoint='shared_browser_gate',"
                           "capability_sha256='',capability_expires_at=NULL,updated_at=? WHERE id=?",
                           (json.dumps(context), timestamp(), worker["id"]))
                return {"state": "waiting_browser", "owner_worker_id": prior["worker_id"], "scope": scope}
            evidence = self._agent_latest_cua_event(db, worker, success=auth)
            if not evidence:
                raise Conflict("The browser gate needs a current-session observed CUA result.")
            if operation == "open_circuit" or not prior or prior["state"] != "open":
                if operation == "open_circuit":
                    raw_event = db.execute("SELECT payload FROM agent_runtime_events WHERE id=?", (evidence["event_id"],)).fetchone()
                    if raw_event and self._agent_scope_postsend_observation_recheck(db, worker, evidence["event_id"]):
                        return {"state": "reconciliation_only", "scope": "job:" + str(worker["job_id"]),
                                "read_only_recheck_allowed": True, "requires_confirmation": False, "global_circuit_opened": False,
                                "reason": "Technical post-send read timeout: make one bounded read-only observation, preferably a screenshot if DOM reading failed. Never replay Submit; finish uncertain without a verified receipt."}
                    if raw_event and self._agent_is_input_timeout(self._agent_decode(raw_event[0], {})):
                        context = self._agent_decode(worker["context"], {})
                        if self._context_may_have_sent_final(context, worker["checkpoint"]):
                            return {"state": "reconciliation_only", "scope": "job:" + str(worker["job_id"]),
                                    "reason": "Input timed out after a possible final action; never replay it."}
                        epoch = context.get("browser_freshness", {}).get("epoch") or worker["session_id"]
                        old = context.get("input_recovery", {})
                        exhausted = old.get("epoch") == epoch and old.get("attempts", 0) >= 1
                        context["input_recovery"] = {"epoch": epoch, "attempts": 1, "exhausted": exhausted,
                                                     "source_event_id": evidence["event_id"]}
                        db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?", (json.dumps(context), timestamp(), worker["id"]))
                        self.event(db, "agent.input_recovery_scoped", {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
                                   "event_id": evidence["event_id"], "exhausted": exhausted}, worker["job_id"], "agent_runtime")
                        return {"state": "job_recovery_exhausted" if exhausted else "keyboard_fallback", "scope": "job:" + str(worker["job_id"]),
                                "requires_confirmation": False, "global_circuit_opened": False,
                                "reason": "Re-observe first; use documented keyboard input once if the required state did not change. Stop only this job if it still fails."}
                self._agent_set_browser_control(db, scope, kind, "open", reason, "agent observed browser boundary",
                                                worker["id"], [evidence["event_id"]])
            if operation == "open_circuit":
                item, _ = self._agent_insert_inbox(db, worker, "approval", dedupe_key="connector_recovery",
                    action="browser_circuit_recovery", summary=reason or "Browser connector failed. Confirm recovery before a bounded recheck.",
                    context={"scope": scope, "source_event_id": evidence["event_id"]})
                self._agent_refresh_pipeline(db, worker["pipeline_id"])
                return {"state": "waiting_approval", "inbox_item_id": item["id"], "scope": scope}
            self.event(db, "agent.browser_gate_acquired", {"worker_id": worker["id"], "scope": scope,
                       "pipeline_id": worker["pipeline_id"]}, actor="agent")
            return {"state": "owned", "scope": scope, "owner_worker_id": worker["id"]}
        if not prior or prior["state"] != "open" or prior["worker_id"] != worker["id"]:
            raise Conflict("Only the current browser-gate owner can complete recovery.")
        evidence = self._agent_latest_cua_event(db, worker, success=True)
        earlier = self._agent_decode(prior["event_ids"], [])
        if not evidence or evidence["event_id"] in earlier:
            raise Conflict("Completion needs a new successful CUA observation after the gate was opened.")
        if not auth:
            approvals = db.execute("SELECT context FROM agent_inbox WHERE worker_id=? AND action='browser_circuit_recovery' "
                                   "AND state='approved'", (worker["id"],)).fetchall()
            if not any(self._agent_decode(row["context"], {}).get("source_event_id") in earlier for row in approvals):
                raise Conflict("Connector recovery still needs its current recorded user decision.")
        if auth:
            context = self._agent_decode(worker["context"], {})
            freshness = context.get("browser_freshness", {})
            if freshness.get("pending"):
                job = db.execute("SELECT url FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
                if (job and (urlsplit(job["url"]).hostname or "").removeprefix("www.") == "amazon.jobs"
                        and not evidence.get("application_url")):
                    raise Conflict("Refresh must be observed on this exact job's application page. A profile or dashboard page does not refresh its form; reopen the draft, observe it, then complete_auth.")
                observed = db.execute("SELECT created_at FROM agent_runtime_events WHERE id=?", (evidence["event_id"],)).fetchone()
                if (evidence["event_id"] == freshness.get("previous_browser_event_id") or not observed
                        or observed["created_at"] < freshness.get("issued_at", "")):
                    raise Conflict("Application refresh needs a new successful browser observation after resume.")
                freshness.update(pending=False, confirmed_at=timestamp(), evidence_id=evidence["event_id"])
                context["browser_freshness"] = freshness
                db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?", (json.dumps(context), timestamp(), worker["id"]))
                self.event(db, "agent.application_session_refreshed", {"pipeline_id": worker["pipeline_id"],
                           "worker_id": worker["id"], "epoch": freshness["epoch"], "event_id": evidence["event_id"]}, worker["job_id"], "agent_runtime")
        self._agent_set_browser_control(db, scope, kind, "closed", reason, "agent verified recovery on the current page",
                                        worker["id"], [evidence["event_id"]])
        for waiting in db.execute("SELECT w.*,p.stop_requested FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                  "WHERE w.state='waiting_browser'").fetchall():
            context = self._agent_decode(waiting["context"], {})
            if context.get("browser_wait_scope") != scope or waiting["stop_requested"]:
                continue
            context.pop("browser_wait_scope", None)
            db.execute("UPDATE agent_workers SET state='queued',context=?,resume_mode=?,updated_at=? WHERE id=?",
                       (json.dumps(context), "same_session" if waiting["session_id"] else "new_session",
                        timestamp(), waiting["id"]))
            self._agent_refresh_pipeline(db, waiting["pipeline_id"])
        self.event(db, "agent.browser_gate_completed", {"worker_id": worker["id"], "scope": scope,
                   "pipeline_id": worker["pipeline_id"]}, actor="agent")
        return {"state": "closed", "scope": scope}

    @staticmethod
    def _agent_pending_inbox_wait(db, worker_id):
        item = db.execute("SELECT i.kind FROM agent_inbox i WHERE i.state='pending' AND (i.worker_id=? OR EXISTS("
                          "SELECT 1 FROM agent_fact_questions f JOIN agent_fact_subscribers s ON s.fact_id=f.id "
                          "WHERE f.inbox_item_id=i.id AND s.worker_id=?)) ORDER BY i.created_at LIMIT 1",
                          (worker_id, worker_id)).fetchone()
        return ("waiting_question" if item["kind"] == "question" else "waiting_approval") if item else None

    @staticmethod
    def _agent_auth_owner_unavailable(db, control):
        owner = db.execute("SELECT state,turn_active FROM agent_workers WHERE id=?", (control["worker_id"],)).fetchone()
        return not owner or (not owner["turn_active"] and owner["state"] in {
            "failed", "completed", "discarded", "stopped", "superseded", "session_missing", "uncertain"})

    def _agent_transfer_auth_owner(self, db, control, worker):
        """Transfer only the mutex: authentication remains unverified and open."""
        baseline = self._agent_latest_cua_event(db, worker, success=True)
        db.execute("UPDATE agent_browser_controls SET worker_id=?,event_ids=?,provenance=?,updated_at=? WHERE scope_key=?",
                   (worker["id"], json.dumps([baseline["event_id"]] if baseline else []),
                    "Scheduler transferred abandoned sign-in ownership; authentication is not verified.",
                    timestamp(), control["scope_key"]))
        self.event(db, "agent.auth_owner_transferred", {"scope": control["scope_key"],
                   "previous_worker_id": control["worker_id"], "worker_id": worker["id"],
                   "pipeline_id": worker["pipeline_id"], "authentication_verified": False}, actor="agent_scheduler")

    def _agent_wake_orphaned_auth_waiters(self, db):
        for gate in db.execute("SELECT * FROM agent_browser_controls WHERE control_type='company_auth' AND state='open'").fetchall():
            if not self._agent_auth_owner_unavailable(db, gate):
                continue
            for waiter in db.execute("SELECT w.* FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                    "WHERE w.state='waiting_browser' AND w.turn_active=0 AND p.stop_requested=0 "
                                    "AND p.status NOT IN ('stopped','stopping','completed') ORDER BY w.created_at,w.id").fetchall():
                context = self._agent_decode(waiter["context"], {})
                if context.get("browser_wait_scope") != gate["scope_key"]:
                    continue
                db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,updated_at=? WHERE id=?",
                           ("same_session" if waiter["session_id"] else "new_session", timestamp(), waiter["id"]))
                self._agent_transfer_auth_owner(db, gate, waiter)
                break

    def _agent_set_browser_control(self, db, scope_key, control_type, state, reason, provenance,
                                   worker_id=None, event_ids=None):
        if control_type not in {"company_auth", "connector_circuit"} or state not in {"open", "closed"}:
            raise ValueError("Unsupported browser control transition.")
        now = timestamp()
        old = db.execute("SELECT opened_at FROM agent_browser_controls WHERE scope_key=?", (scope_key,)).fetchone()
        opened_at = (old["opened_at"] if old and state == "open" else now if state == "open" else None)
        db.execute("INSERT INTO agent_browser_controls(scope_key,control_type,state,reason,provenance,worker_id,event_ids,"
                   "opened_at,closed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET "
                   "control_type=excluded.control_type,state=excluded.state,reason=excluded.reason,"
                   "provenance=excluded.provenance,worker_id=excluded.worker_id,event_ids=excluded.event_ids,"
                   "opened_at=CASE WHEN excluded.state='open' THEN COALESCE(agent_browser_controls.opened_at,excluded.opened_at) "
                   "ELSE agent_browser_controls.opened_at END,closed_at=excluded.closed_at,updated_at=excluded.updated_at",
                   (scope_key, control_type, state, str(reason or "")[:4000], str(provenance or "")[:1000],
                    worker_id, json.dumps(event_ids or []), opened_at, now if state == "closed" else None, now))
        return now

    @staticmethod
    def _agent_browser_control_view(row, control_type):
        raw = dict(row) if row else {}
        state = raw.get("state", "closed")
        is_open = state == "open"
        return {"state": state, "open": is_open, "required": is_open,
                "owner_worker_id": raw.get("worker_id"),
                "reason": raw.get("reason") or None, "provenance": raw.get("provenance") or None,
                "opened_at": raw.get("opened_at"), "updated_at": raw.get("updated_at"),
                "control_type": control_type}

    @staticmethod
    def _agent_initial_sections(role):
        common = ["pipeline"]
        if role == "discovery":
            return [*common, "discovery_frontier"]
        if role == "evaluator":
            return [*common, "job", "applicant_index"]
        if role == "submission":
            return [*common, "job", "applicant_index", "inbox", "prepared_document_handles",
                    "submission_state", "submission_history"]
        if role == "document":
            return ["job", "applicant_index", "parent_context"]
        raise Conflict("Unknown agent context role.")

    @staticmethod
    def _agent_context_revision(db):
        row = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()
        return int(row[0] or 0)

    def _agent_context_updates(self, db, worker, after_revision, current_revision):
        if after_revision is None:
            return {"events": [], "truncated": False}
        if isinstance(after_revision, bool) or not isinstance(after_revision, int) or after_revision < 0:
            raise ValueError("after_revision must be a nonnegative event ID.")
        if after_revision > current_revision:
            raise ValueError("after_revision is newer than the current context revision.")
        candidates = db.execute("SELECT id,job_id,actor,kind,payload,created_at FROM events WHERE id>? "
                               "ORDER BY id LIMIT 2000", (after_revision,)).fetchall()
        selected = []
        payload_keys = {"worker_id", "pipeline_id", "job_id", "role", "status", "state", "reason",
                        "decision", "selected", "created", "changed", "item_id", "question_id",
                        "jd_version", "profile_version", "result_id", "submission_worker_id"}
        for row in candidates:
            payload = self._agent_decode(row["payload"], {})
            is_relevant = row["job_id"] == worker["job_id"] if worker["job_id"] else False
            if isinstance(payload, dict):
                is_relevant = is_relevant or payload.get("worker_id") == worker["id"] \
                    or payload.get("pipeline_id") == worker["pipeline_id"]
            if not is_relevant:
                continue
            compact = {key: payload[key] for key in payload_keys if isinstance(payload, dict) and key in payload}
            selected.append({"id": row["id"], "kind": row["kind"], "actor": row["actor"],
                             "job_id": row["job_id"], "created_at": row["created_at"], "data": compact})
        return {"events": selected[-50:], "truncated": len(selected) > 50 or len(candidates) == 2000,
                "from_revision": after_revision, "through_revision": current_revision}

    def _agent_pipeline_context(self, pipeline):
        discovery = self._agent_decode(pipeline["discovery_payload"], {})
        if not isinstance(discovery, dict):
            discovery = {}
        coverage = discovery.get("coverage") if isinstance(discovery.get("coverage"), dict) else {}
        # Coverage is agent supplied; keep the frontier compact and bounded.
        compact_coverage, coverage_bytes = {}, 0
        for key, value in list(coverage.items())[:40]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact_value = value if not isinstance(value, str) else value[:1000]
            elif isinstance(value, list):
                compact_value = value[:20]
            else:
                continue
            size = len(json.dumps(compact_value, ensure_ascii=False).encode("utf-8"))
            if size > 1200 or coverage_bytes + size > 8000:
                compact_value = {"omitted_from_launch": True, "retrieve": "get_context sections=['checkpoint']"}
            compact_coverage[str(key)[:80]] = compact_value
            coverage_bytes += len(json.dumps(compact_value).encode("utf-8"))
        if len(coverage) > 40:
            compact_coverage["additional_fields"] = {"count": len(coverage) - 40,
                                                       "retrieve": "get_context sections=['checkpoint']"}
        return {"id": pipeline["id"], "company_id": pipeline["company_id"],
                "company_name": pipeline["company_name"], "careers_url": pipeline["careers_url"],
                "filters": self._agent_pipeline_filters(pipeline),
                "adopted_from_pipeline_id": pipeline["adopted_from_pipeline_id"],
                "test_only": bool(pipeline["agent_test_only"]),
                "discovery": {"status": discovery.get("status", pipeline["discovery_status"]),
                              "detail": str(discovery.get("detail") or "")[:2000],
                              "coverage": compact_coverage}}

    def _agent_current_job(self, db, worker):
        if not worker["job_id"]:
            return None
        row = db.execute("SELECT j.*,v.jd,v.metadata,v.sha256 AS description_sha256 FROM jobs j "
                         "JOIN job_versions v ON v.job_id=j.id AND v.version=j.jd_version WHERE j.id=?",
                         (worker["job_id"],)).fetchone()
        if not row:
            return None
        job = dict(row)
        job["metadata"] = self._agent_decode(job["metadata"], {})
        job["application_outcome"] = dict(db.execute("SELECT * FROM application_outcomes WHERE job_id=?",
                                                      (job["id"],)).fetchone() or {})
        if worker["role"] in {"submission", "document"}:
            evaluation = db.execute("SELECT id,result FROM agent_workers WHERE pipeline_id=? AND job_id=? AND role='evaluator' "
                                    "AND state='completed' AND jd_version=? AND profile_version=? ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                                    (worker["pipeline_id"], job["id"], job["jd_version"], self.profile_version(db))).fetchone()
            if evaluation:
                findings = self._agent_decode(evaluation["result"], {})
                if not findings.get("decision"):
                    # Older transports replaced result with their exit summary.
                    # The immutable agent commit event remains authoritative.
                    source = db.execute("SELECT payload FROM events WHERE kind='agent.evaluation_finished' AND job_id=? "
                                        "AND json_extract(payload,'$.worker_id')=? ORDER BY id DESC LIMIT 1",
                                        (job["id"], evaluation["id"])).fetchone()
                    findings = self._agent_decode(source["payload"], {}) if source else findings
                job["evaluation_findings"] = findings
        return job

    def _agent_inbox_context(self, db, worker):
        rows = db.execute("SELECT i.* FROM agent_inbox i WHERE worker_id=? OR EXISTS("
                          "SELECT 1 FROM agent_fact_questions f JOIN agent_fact_subscribers s ON s.fact_id=f.id "
                          "WHERE f.inbox_item_id=i.id AND s.worker_id=?) ORDER BY "
                          "CASE WHEN state='pending' THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT 30",
                          (worker["id"], worker["id"])).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["context"] = self._agent_decode(item["context"], {})
            item["response"] = self._agent_decode(item["response"], "")
            items.append(item)
        items.reverse()
        return items

    def _agent_prepared_document_handles(self, db, worker, job):
        if worker["role"] != "submission" or not job:
            return []
        context = self._agent_decode(worker["context"], {})
        requested = set(context.get("requested_artifacts") or [])
        if not requested:
            return []
        profile_version = self.profile_version(db)
        ready = self._agent_ready_artifacts(db, worker["pipeline_id"], worker["job_id"],
                                            job["jd_version"], profile_version)
        handles = []
        for kind in sorted(requested):
            row = ready.get(kind)
            if not row:
                continue
            package_id = context.get("document_package_id")
            package_root = (self.root / "artifacts" / "agent" / str(package_id)).resolve()
            source_path = (self.root / row["path"]).resolve()
            pdf_path = (self.root / str(row.get("pdf_path") or "")).resolve()
            if (not package_id or package_root not in source_path.parents or not source_path.is_file()
                    or digest(source_path.read_text(encoding="utf-8")) != row["sha256"]
                    or package_root not in pdf_path.parents or not pdf_path.is_file()
                    or hashlib.sha256(pdf_path.read_bytes()).hexdigest() != row.get("pdf_sha256")):
                continue
            handles.append({"artifact_id": row["id"], "kind": kind, "filename": row["filename"],
                            "source_sha256": row["sha256"], "package_id": package_id,
                            "version": row["version"], "jd_version": row["jd_version"],
                            "profile_version": row["profile_version"], "upload_path": str(pdf_path),
                            "upload_filename": pdf_path.name, "upload_sha256": row["pdf_sha256"]})
            policy = self._agent_routine_upload_policy(db, worker)
            if policy.get("enabled"):
                handles[-1]["upload_authorization"] = {**policy, "artifact_id": row["id"],
                    "kind": kind, "upload_path": str(pdf_path), "upload_filename": pdf_path.name,
                    "upload_sha256": row["pdf_sha256"]}
        return handles

    def _agent_context_for_worker(self, db, worker, request=None):
        request = request if isinstance(request, dict) else {}
        pipeline = db.execute("SELECT p.*,c.name AS company_name,c.careers_url FROM pipelines p "
                              "JOIN companies c ON c.id=p.company_id WHERE p.id=?", (worker["pipeline_id"],)).fetchone()
        if not pipeline:
            raise KeyError(worker["pipeline_id"])
        sections = request.get("sections") or []
        if not isinstance(sections, list) or len(sections) > 12 or any(not isinstance(item, str) for item in sections):
            raise ValueError("Context sections must be a list of known section names.")
        allowed_sections = {"pipeline", "job", "applicant_index", "inbox", "prepared_documents", "checkpoint",
                            "prepared_document_handles", "submission_state", "submission_history",
                            "discovery_frontier", "parent_context"}
        unknown = set(sections) - allowed_sections
        if unknown:
            raise ValueError("Unknown context section: " + ", ".join(sorted(unknown)))
        if worker["role"] == "discovery" and set(sections) & {"job", "applicant_index", "prepared_documents",
                                                               "prepared_document_handles", "submission_state",
                                                               "submission_history", "parent_context"}:
            raise Conflict("Discovery context cannot include applicant or application materials.")
        if worker["role"] == "discovery" and "inbox" in sections:
            # Company-scoped discovery questions may be included; they never
            # expose another worker's applicant facts or job materials.
            pass
        revision = self._agent_context_revision(db)
        result = {"runtime_controls": self._agent_runtime_controls(db, worker, pipeline),
                  "context_revision": revision,
                  "updates": self._agent_context_updates(db, worker, request.get("after_revision"), revision),
                  "sections": {}}
        if request.get("source_ids") is not None:
            # One roundtrip can check live controls and hydrate the exact source
            # evidence the agent selected from the already supplied index.
            result.update(self._agent_retrieve_sources(db, worker, {"source_ids": request["source_ids"]}))
        saved_context = self._agent_decode(worker["context"], {})
        if saved_context.get("last_context_revision") != revision:
            saved_context["last_context_revision"] = revision
            db.execute("UPDATE agent_workers SET context=?,updated_at=? WHERE id=?",
                       (json.dumps(saved_context, ensure_ascii=False), timestamp(), worker["id"]))
        if not sections:
            return result
        result["constraints"] = {"country": "US", "employment": "full_time", "arrangement": ["onsite", "hybrid"],
                                 "career_scope": self._agent_career_scope()}
        job = self._agent_current_job(db, worker) if set(sections) & {"job", "prepared_documents",
                                                                      "prepared_document_handles", "submission_state"} else None
        profile = None
        if "pipeline" in sections:
            result["sections"]["pipeline"] = self._agent_pipeline_context(pipeline)
        if "checkpoint" in sections:
            result["sections"]["checkpoint"] = {"context": self._agent_worker_record(worker)["context"],
                                                 "historical_result": self._agent_decode(worker["result"], {}),
                                                 "historical_result_is_not_live_controls": True}
            if worker["role"] == "discovery":
                result["sections"]["checkpoint"]["discovery_history"] = self._agent_decode(pipeline["discovery_payload"], {})
        if "job" in sections:
            result["sections"]["job"] = job
        if "applicant_index" in sections:
            if worker["role"] == "discovery":
                raise Conflict("Company discovery cannot retrieve applicant facts.")
            profile = self._agent_profile_context(db)
            result["sections"]["applicant_index"] = profile
        if "inbox" in sections:
            result["sections"]["inbox"] = self._agent_inbox_context(db, worker)
        if "prepared_document_handles" in sections:
            result["sections"]["prepared_document_handles"] = self._agent_prepared_document_handles(db, worker, job)
        if "prepared_documents" in sections:
            profile_version = self.profile_version(db)
            result["sections"]["prepared_documents"] = self._agent_prepared_documents(
                db, worker, job["jd_version"] if job else None, profile_version)
        if "submission_state" in sections:
            result["sections"]["submission_state"] = {
                "current_approved_actions": self._agent_current_approved_actions(db, worker),
                "first_application_review_required": bool(worker["role"] == "submission" and not db.execute(
                    "SELECT 1 FROM agent_submission_results WHERE state='submitted' LIMIT 1").fetchone())}
        if "submission_history" in sections:
            if worker["job_id"]:
                result["sections"]["submission_history"] = [dict(row) for row in db.execute(
                    "SELECT state,detail,observations,created_at FROM agent_submission_results WHERE job_id=? "
                    "ORDER BY created_at DESC LIMIT 10", (worker["job_id"],)).fetchall()][::-1]
            else:
                result["sections"]["submission_history"] = []
        if "discovery_frontier" in sections:
            result["sections"]["discovery_frontier"] = self._agent_pipeline_context(pipeline)["discovery"]
        if "parent_context" in sections:
            parent = db.execute("SELECT context FROM agent_workers WHERE id=? AND parent_id IS NULL",
                                (worker["parent_id"],)).fetchone() if worker["parent_id"] else None
            result["sections"]["parent_context"] = self._agent_decode(parent["context"] if parent else "{}", {})
        return result

    def _agent_lookup_listing(self, db, worker, payload):
        req = str(payload.get("requisition_id") or "").strip()
        url = str(payload.get("url") or "").strip()
        if not req and not url:
            raise ValueError("Lookup needs a published requisition ID or exact URL.")
        if worker["role"] == "discovery":
            company_scope = worker["company_id"]
            where, values = ("j.company_id=? AND j.requisition_id=?", (company_scope, req)) if req else ("j.company_id=? AND j.url=?", (company_scope, url))
        else:
            if req:
                where, values = ("j.requisition_id=? AND EXISTS(SELECT 1 FROM pipeline_jobs pj WHERE pj.pipeline_id=? AND pj.job_id=j.id)",
                                 (req, worker["pipeline_id"]))
            else:
                where, values = ("j.url=? AND EXISTS(SELECT 1 FROM pipeline_jobs pj WHERE pj.pipeline_id=? AND pj.job_id=j.id)",
                                 (url, worker["pipeline_id"]))
        row = db.execute("SELECT j.*,v.jd,v.metadata,v.sha256 AS description_sha256 FROM jobs j "
                         "JOIN job_versions v ON v.job_id=j.id AND v.version=j.jd_version WHERE " + where + " LIMIT 1", values).fetchone()
        if not row:
            return {"found": False, "listing": None}
        listing = dict(row)
        listing["metadata"] = self._agent_decode(listing["metadata"], {})
        return {"found": True, "listing": listing}

    def _agent_lookup_listings(self, db, worker, payload):
        listings = payload.get("listings")
        if not isinstance(listings, list) or not 1 <= len(listings) <= 50:
            raise ValueError("A batch lookup must contain between one and fifty identities.")
        identities = []
        reqs, urls = set(), set()
        for index, item in enumerate(listings):
            if not isinstance(item, dict):
                raise ValueError(f"Identity at index {index} must be an object.")
            req = str(item.get("requisition_id") or "").strip()
            url = str(item.get("url") or "").strip()
            if not req and not url:
                raise ValueError(f"Identity at index {index} needs a requisition ID or exact URL.")
            identities.append((req, url))
            if req:
                reqs.add(req)
            else:
                urls.add(url)
        if worker["role"] == "discovery":
            scope_sql = "j.company_id=?"
            scope_values = [worker["company_id"]]
        else:
            scope_sql = "EXISTS(SELECT 1 FROM pipeline_jobs pj WHERE pj.pipeline_id=? AND pj.job_id=j.id)"
            scope_values = [worker["pipeline_id"]]
        identity_clauses, identity_values = [], []
        if reqs:
            ordered = sorted(reqs)
            identity_clauses.append("j.requisition_id IN (" + ",".join("?" for _ in ordered) + ")")
            identity_values.extend(ordered)
        if urls:
            ordered = sorted(urls)
            identity_clauses.append("j.url IN (" + ",".join("?" for _ in ordered) + ")")
            identity_values.extend(ordered)
        query = ("SELECT j.id,j.requisition_id,j.url,j.title,j.location,j.country,j.employment,j.arrangement,"
                 "j.jd_version,v.sha256 AS description_sha256,pj.selected AS selected_in_pipeline "
                 "FROM jobs j JOIN job_versions v ON v.job_id=j.id AND v.version=j.jd_version "
                 "LEFT JOIN pipeline_jobs pj ON pj.pipeline_id=? AND pj.job_id=j.id WHERE " + scope_sql +
                 " AND (" + " OR ".join(identity_clauses) + ")")
        rows = db.execute(query, [worker["pipeline_id"], *scope_values, *identity_values]).fetchall()
        by_req, by_url = {}, {}
        for row in rows:
            item = dict(row)
            if item["requisition_id"]:
                by_req.setdefault(item["requisition_id"], item)
            by_url.setdefault(item["url"], item)
        matches = []
        for index, (req, url) in enumerate(identities):
            listing = by_req.get(req) if req else by_url.get(url)
            if not listing:
                matches.append({"index": index, "known": False})
                continue
            listing = dict(listing)
            selected = listing.pop("selected_in_pipeline", None)
            matches.append({"index": index, "known": True, **listing,
                            "selected_in_pipeline": bool(selected) if selected is not None else False})
        return {"matches": matches}

    @staticmethod
    def _agent_plan_origin(raw_origin, *, test_only=False):
        if not isinstance(raw_origin, str) or len(raw_origin) > 512:
            raise ValueError("Workflow plan origin must be an absolute HTTPS origin.")
        parsed = urlsplit(raw_origin.strip())
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not Store._agent_url_allowed(
                    f"{parsed.scheme}://{parsed.netloc}/", test_only=test_only)):
            raise ValueError("Workflow plan origin must be an absolute HTTPS origin or opted-in loopback fixture origin.")
        scheme = parsed.scheme.casefold()
        host = parsed.hostname.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if port and not (scheme == "https" and port == 443 or scheme == "http" and port == 80):
            host += f":{port}"
        return f"{scheme}://{host}"

    def _agent_workflow_plan_key(self, db, worker, payload):
        pipeline = db.execute("SELECT agent_test_only FROM pipelines WHERE id=?", (worker["pipeline_id"],)).fetchone()
        origin = self._agent_plan_origin(payload.get("origin"), test_only=bool(pipeline and pipeline[0]))
        role = payload.get("role") or worker["role"]
        if role != worker["role"] or role not in {"discovery", "evaluator", "submission"}:
            raise Conflict("A workflow plan must match this worker's browser role.")
        fingerprint = payload.get("structure_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
            raise ValueError("structure_fingerprint must be a SHA-256 hex digest of the observed page structure.")
        return origin, role, fingerprint.casefold()

    def _agent_lookup_workflow_plan(self, db, worker, payload):
        origin, role, fingerprint = self._agent_workflow_plan_key(db, worker, payload)
        row = db.execute("SELECT id,origin,role,structure_fingerprint,plan,observation,worker_id,event_id,created_at,updated_at "
                         "FROM agent_workflow_plans WHERE origin=? AND role=? AND structure_fingerprint=?",
                         (origin, role, fingerprint)).fetchone()
        if not row:
            return {"found": False, "plan": None}
        plan = dict(row)
        plan["plan"] = self._agent_decode(plan["plan"], {})
        plan["observation"] = self._agent_decode(plan["observation"], {})
        return {"found": True, "plan": plan}

    def _agent_save_workflow_plan(self, db, worker, payload):
        origin, role, fingerprint = self._agent_workflow_plan_key(db, worker, payload)
        plan = payload.get("plan")
        if not isinstance(plan, (dict, list)):
            raise ValueError("Workflow plan must be a JSON object or array authored from the observed page.")
        encoded_plan = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded_plan.encode("utf-8")) > 30000:
            raise ValueError("Workflow plan exceeds the 30 KiB limit.")
        observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        encoded_observation = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded_observation.encode("utf-8")) > 12000:
            raise ValueError("Workflow plan observation exceeds the 12 KiB limit.")
        capture = self._agent_capture_ref(db, worker)
        if not capture:
            raise Conflict("A workflow plan needs an actual successful CUA result from this worker's current session.")
        existing = db.execute("SELECT id FROM agent_workflow_plans WHERE origin=? AND role=? AND structure_fingerprint=?",
                              (origin, role, fingerprint)).fetchone()
        plan_id = existing["id"] if existing else uuid.uuid4().hex
        now = timestamp()
        db.execute("INSERT INTO agent_workflow_plans(id,origin,role,structure_fingerprint,plan,observation,event_id,worker_id,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(origin,role,structure_fingerprint) DO UPDATE SET "
                   "plan=excluded.plan,observation=excluded.observation,event_id=excluded.event_id,"
                   "worker_id=excluded.worker_id,updated_at=excluded.updated_at",
                   (plan_id, origin, role, fingerprint, encoded_plan, encoded_observation,
                    capture["event_id"], worker["id"], now, now))
        self.event(db, "agent.workflow_plan_saved", {"worker_id": worker["id"], "plan_id": plan_id,
                   "origin": origin, "role": role, "structure_fingerprint": fingerprint,
                   "source_event_id": capture["event_id"]}, worker["job_id"], "agent_workflow")
        return {"saved": True, "plan_id": plan_id, "origin": origin, "role": role,
                "structure_fingerprint": fingerprint, "source_event_id": capture["event_id"]}

    def _agent_update_description(self, db, worker, payload):
        if not worker["job_id"] or worker["role"] not in {"evaluator", "submission"}:
            raise Conflict("Only an evaluator or submission worker can update its scoped job description.")
        jd = payload.get("jd")
        if not isinstance(jd, str) or not jd.strip() or len(jd) > 200000:
            raise ValueError("Updated job description must be non-empty and within the size limit.")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        if job["phase"] == "submitted" or job["run_status"] == "submission_uncertain":
            raise Conflict("Submitted or uncertain jobs cannot be rewritten by description repair.")
        source_url = payload.get("source_url") or job["url"]
        test_only_row = db.execute("SELECT agent_test_only FROM pipelines WHERE id=?",
                                   (worker["pipeline_id"],)).fetchone()
        test_only = bool(test_only_row and test_only_row[0])
        if not self._agent_url_allowed(source_url, test_only=test_only):
            raise ValueError("Description source must be an absolute HTTPS URL or an opted-in loopback fixture URL.")
        parsed_source = urlsplit(source_url)
        source_url = urlunsplit((parsed_source.scheme.lower(), parsed_source.netloc.lower(),
                                 parsed_source.path or "/", parsed_source.query, ""))
        current = db.execute("SELECT sha256,jd,metadata FROM job_versions WHERE job_id=? AND version=?",
                             (worker["job_id"], job["jd_version"])).fetchone()
        metadata = self._agent_decode(current["metadata"], {}) if current else {}
        metadata = {k: v for k, v in metadata.items() if k not in {"repaired_by_worker", "observation"}}
        observation = payload.get("observation") or {}
        checksum = digest(json.dumps({"title": job["title"], "jd": jd, "url": source_url,
                                     "metadata": metadata}, ensure_ascii=False, sort_keys=True))
        if current and current["jd"] == jd and job["url"] == source_url:
            self.event(db, "agent.job_description_reobserved", {"worker_id": worker["id"],
                       "jd_version": job["jd_version"], "observation": observation}, worker["job_id"], "agent")
            return {"job_id": worker["job_id"], "jd_version": job["jd_version"], "changed": False}
        version = job["jd_version"] + 1
        now = timestamp()
        db.execute("INSERT INTO job_versions(job_id,version,jd,sha256,metadata,source,observed_at) VALUES(?,?,?,?,?,?,?)",
                   (worker["job_id"], version, jd, checksum, json.dumps(metadata, ensure_ascii=False), source_url, now))
        db.execute("UPDATE jobs SET jd_version=?,url=?,last_seen_at=?,phase='new',reason='Job description repaired by scoped agent.' "
                   "WHERE id=?", (version, source_url, now, worker["job_id"]))
        db.execute("UPDATE agent_workers SET jd_version=?,updated_at=? WHERE id=?",
                   (version, now, worker["id"]))
        db.execute("UPDATE pipeline_jobs SET agent_decision='',agent_reason='',agent_status='selected' "
                   "WHERE pipeline_id=? AND job_id=?", (worker["pipeline_id"], worker["job_id"]))
        self.event(db, "agent.job_description_repaired", {"worker_id": worker["id"], "jd_version": version,
                   "source_url": source_url, "sha256": checksum, "observation": observation},
                   worker["job_id"], "agent")
        return {"job_id": worker["job_id"], "jd_version": version, "changed": True}

    def _agent_finish_evaluation(self, db, worker, payload, decision):
        job = db.execute("SELECT jd_version,phase,run_status FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        if not job:
            raise KeyError(worker["job_id"])
        if job["jd_version"] != worker["jd_version"] or self.profile_version(db) != worker["profile_version"]:
            raise Conflict("Job or applicant snapshot changed during evaluation; resume with fresh context.")
        reason = str(payload.get("reason") or "")[:12000]
        observation = payload.get("observations") if isinstance(payload.get("observations"), dict) else {}
        result = {"decision": decision, "reason": reason, "observations": observation,
                  "jd_version": worker["jd_version"], "profile_version": worker["profile_version"],
                  "worker_id": worker["id"], "completed_at": timestamp()}
        now = timestamp()
        state = "completed" if decision == "approve" else "discarded"
        db.execute("UPDATE pipeline_jobs SET agent_decision=?,agent_reason=?,agent_status=? WHERE pipeline_id=? AND job_id=?",
                   (decision, reason, "approved" if decision == "approve" else "discarded",
                    worker["pipeline_id"], worker["job_id"]))
        db.execute("UPDATE agent_workers SET state=?,result=?,completed_at=?,updated_at=?,capability_sha256='',"
                   "capability_expires_at=NULL,error='' WHERE id=?",
                   (state, json.dumps(result, ensure_ascii=False), now, now, worker["id"]))
        if decision in {"approve", "discard"}:
            cache_snapshot = self._agent_evaluation_cache_snapshot(
                db, worker["pipeline_id"], worker["job_id"], worker["jd_version"], self._agent_decode(worker["context"], {}).get("execution"))
            if cache_snapshot:
                cache_payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                # Cache only compact, complete agent findings. Oversized or
                # unavailable prompt/config snapshots remain ordinary misses.
                if len(cache_payload.encode("utf-8")) <= 64_000:
                    db.execute("INSERT OR IGNORE INTO agent_evaluation_cache(cache_key,pipeline_id,job_id,jd_version,"
                               "profile_version,policy_sha256,model,decision,payload,source_worker_id,created_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (cache_snapshot["cache_key"], worker["pipeline_id"], worker["job_id"],
                                worker["jd_version"], worker["profile_version"], cache_snapshot["policy_sha256"],
                                cache_snapshot["model"], decision, cache_payload, worker["id"], now))
                    self.event(db, "agent.evaluation_cache_stored", {"pipeline_id": worker["pipeline_id"],
                               "job_id": worker["job_id"], "worker_id": worker["id"],
                               "cache_key": cache_snapshot["cache_key"], "decision": decision},
                               worker["job_id"], "agent_cache")
        new_worker = None
        if decision == "approve":
            new_worker = self._agent_insert_worker(db, worker["pipeline_id"], "submission", job_id=worker["job_id"],
                                                   context={"evaluation_worker_id": worker["id"]})
        self.event(db, "agent.evaluation_finished", {**result, "submission_worker_id": new_worker},
                   worker["job_id"], "agent_evaluator")
        self._agent_refresh_pipeline(db, worker["pipeline_id"])
        return {**result, "state": state, "submission_worker_id": new_worker}

    def _agent_request_documents(self, db, worker, payload):
        if worker["role"] != "submission" or not worker["job_id"]:
            raise Conflict("Only a submission parent can request documents for its job.")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts or any(kind not in {"resume", "cover_letter"} for kind in artifacts):
            raise ValueError("Requested document kinds must be resume and/or cover_letter.")
        requested = sorted(set(artifacts))
        now = timestamp()
        old = db.execute("SELECT * FROM agent_workers WHERE parent_id=? AND role='document' AND state NOT IN ('failed','stopped','completed','superseded') "
                         "ORDER BY created_at DESC LIMIT 1", (worker["id"],)).fetchone()
        if old:
            db.execute("UPDATE agent_workers SET state='waiting_child',checkpoint='waiting_child',updated_at=? WHERE id=?", (now, worker["id"]))
            if old["state"] == "interrupted" and not old["turn_active"] and not self._agent_pending_inbox_wait(db, old["id"]):
                db.execute("UPDATE agent_workers SET state='queued',resume_mode=?,error='',updated_at=? WHERE id=?",
                           ("same_session" if old["session_id"] else "new_session", now, old["id"]))
            return {"child_worker_id": old["id"], "state": "waiting_child", "created": False}
        context = self._agent_decode(worker["context"], {})
        context.update({"requested_artifacts": requested,
                        "document_request_reason": str(payload.get("reason") or "")[:4000],
                        "form_context": payload.get("form_context") if isinstance(payload.get("form_context"), dict) else {}})
        db.execute("UPDATE agent_workers SET state='waiting_child',checkpoint='waiting_child',context=?,updated_at=? WHERE id=?",
                   (json.dumps(context, ensure_ascii=False), now, worker["id"]))
        child = self._agent_insert_worker(db, worker["pipeline_id"], "document", job_id=worker["job_id"],
                                          parent_id=worker["id"], context={"requested_artifacts": requested})
        self.event(db, "agent.documents_requested", {"parent_worker_id": worker["id"], "child_worker_id": child,
                   "requested_artifacts": requested, "reason": context["document_request_reason"]},
                   worker["job_id"], "agent_submission")
        self._agent_refresh_pipeline(db, worker["pipeline_id"])
        return {"child_worker_id": child, "state": "waiting_child", "created": True}

    def _agent_release_missing_reconciliation_tab(self, db, worker):
        """An absent post-send tab is job-local when the same Chrome is present.

        Preserve the uncertain outcome and human decision; release only the
        obsolete connector lock, never grant a tab or application retry.
        """
        context = self._agent_decode(worker["context"], {})
        recovery = context.get("reconciliation_recovery", {})
        if not recovery.get("read_only") or not self._context_may_have_sent_final(context, worker["checkpoint"]):
            return False
        gate = db.execute("SELECT * FROM agent_browser_controls WHERE scope_key='connector:cua_repl' AND state='open' AND worker_id=?", (worker["id"],)).fetchone()
        approval = db.execute("SELECT * FROM agent_inbox WHERE id=? AND worker_id=? AND state='approved' AND action='browser_circuit_recovery'",
                              (recovery.get("approval_item_id"), worker["id"])).fetchone()
        if not gate or not approval:
            return False
        source_id = self._agent_decode(approval["context"], {}).get("source_event_id")
        if source_id not in self._agent_decode(gate["event_ids"], []):
            return False
        failed = db.execute("SELECT payload FROM agent_runtime_events WHERE id=? AND worker_id=? AND success=0", (source_id, worker["id"])).fetchone()
        failure = self._agent_decode(failed[0], {}) if failed else {}
        failure_item = failure.get("transport_event", {}).get("item", {})
        text = "\n".join(b.get("text", "") for b in failure_item.get("result", {}).get("content", []) if b.get("type") == "text")
        if (re.search(r"permission denied|access denied|access was declined|user denied|blocked by policy|browser use was stopped", text, re.I)
                or not re.search(r"Timed out after \d+ms waiting for CDP command (?:Page\.getFrameTree|Runtime\.evaluate)\.", text)):
            return False
        evidence = self._agent_latest_cua_event(db, worker, success=True)
        observed = db.execute("SELECT payload,created_at FROM agent_runtime_events WHERE id=?", (evidence["event_id"],)).fetchone() if evidence else None
        tab_id = str(context.get("tab_id") or "")
        if not observed or not tab_id or observed["created_at"] < approval["resolved_at"]:
            return False
        item = self._agent_decode(observed["payload"], {}).get("transport_event", {}).get("item", {})
        inventories = []
        for block in item.get("result", {}).get("content", []):
            if block.get("type") == "text":
                value = self._agent_decode(block.get("text", ""), {})
                if isinstance(value, dict) and isinstance(value.get("browsers"), list) and not value.get("errors"):
                    inventories.extend(b for b in value["browsers"] if isinstance(b, dict) and b.get("family") == "chrome" and b.get("type") in {"extension", "cdp"} and isinstance(b.get("tabs"), list))
        if not inventories or any(str(tab.get("id")) == tab_id for browser in inventories for tab in browser["tabs"]):
            return False
        self._agent_set_browser_control(db, gate["scope_key"], gate["control_type"], "closed",
            "Approved read-only reconciliation found the original tab absent from working Chrome inventory. Application remains uncertain; no retry is authorized.",
            "Host isolated a missing application tab using genuine current-session inventory after the exact human decision; this is not a submission receipt.", worker["id"], [evidence["event_id"]])
        self.event(db, "agent.missing_reconciliation_tab_isolated", {"pipeline_id": worker["pipeline_id"], "worker_id": worker["id"],
            "approval_item_id": approval["id"], "inventory_event_id": evidence["event_id"], "tab_id": tab_id,
            "final_action_replay_authorized": False}, worker["job_id"], "agent_runtime")
        return True

    def _agent_finish_submission(self, db, worker, payload):
        if worker["role"] != "submission" or not worker["job_id"]:
            raise Conflict("Only a scoped submission worker can finish a submission.")
        existing = db.execute("SELECT * FROM agent_submission_results WHERE job_id=? AND state='submitted' ORDER BY rowid LIMIT 1",
                              (worker['job_id'],)).fetchone()
        if existing:
            if existing['worker_id'] != worker['id']:
                raise Conflict('This application already has a committed submission receipt.')
            return {'result_id':existing['id'],'status':'submitted','detail':existing['detail'],
                    'observations':self._agent_decode(existing['observations'],{}),
                    'tool_event_ids':self._agent_decode(existing['event_ids'],[]),'already_committed':True}
        status = payload.get("status")
        if status not in {"submitted", "discarded", "uncertain", "failed"}:
            raise ValueError("Submission status must be submitted, discarded, uncertain, or failed.")
        context = self._agent_decode(worker["context"], {})
        if status in {"failed", "discarded"} and self._context_may_have_sent_final(context, worker["checkpoint"]):
            status = "uncertain"
        if status == "submitted" and not context.get("final_action_may_have_been_sent"):
            raise Conflict("A submitted outcome requires the durable pre-send authorization checkpoint.")
        event_ids = self._agent_event_results(db, worker["id"]) if status == "submitted" else []
        if status == "submitted" and not event_ids:
            raise Conflict("A submitted outcome needs a successful CUA tool result from this worker's current session.")
        detail = str(payload.get("detail") or "")[:12000]
        observations = payload.get("observations") if isinstance(payload.get("observations"), dict) else {}
        result_id = uuid.uuid4().hex
        now = timestamp()
        db.execute("INSERT INTO agent_submission_results(id,pipeline_id,worker_id,job_id,state,detail,observations,event_ids,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (result_id, worker["pipeline_id"], worker["id"], worker["job_id"], status, detail,
                    json.dumps(observations, ensure_ascii=False), json.dumps(event_ids), now))
        terminal_state = {"submitted": "completed", "discarded": "discarded", "uncertain": "uncertain", "failed": "failed"}[status]
        db.execute("UPDATE pipeline_jobs SET agent_status=? WHERE pipeline_id=? AND job_id=?",
                   (status, worker["pipeline_id"], worker["job_id"]))
        if status == "submitted":
            db.execute("UPDATE agent_browser_controls SET state='closed' WHERE control_type='company_auth' AND worker_id=?",(worker['id'],))
            db.execute("UPDATE jobs SET phase='submitted',run_status='idle',active_stage=NULL,lease_owner=NULL,lease_token=NULL,"
                       "lease_until=NULL,reason=? WHERE id=?", (detail or "Agent recorded a submitted application with successful CUA evidence.", worker["job_id"]))
            # A verified new submission is awaiting a recruiter response until
            # the user records a different outcome. Preserve any status that
            # already exists (including an imported unknown outcome).
            db.execute("INSERT INTO application_outcomes(job_id,status,provenance,actor,updated_at) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING",
                       (worker["job_id"], "pending",
                        f"Verified agent submission result {result_id}; awaiting recruiter response. "
                        "Automatic recruiter status polling is not configured.",
                        "agent_submission", now))
        elif status == "uncertain":
            db.execute("UPDATE jobs SET run_status='submission_uncertain',active_stage=NULL,lease_owner=NULL,lease_token=NULL,"
                       "lease_until=NULL,reason=? WHERE id=?", (detail or "Application send may have occurred; reconcile before any retry.", worker["job_id"]))
        result = {"result_id": result_id, "status": status, "detail": detail,
                  "observations": observations, "tool_event_ids": event_ids}
        db.execute("UPDATE agent_workers SET state=?,result=?,completed_at=?,updated_at=?,capability_sha256='',"
                   "capability_expires_at=NULL,error=? WHERE id=?",
                   (terminal_state, json.dumps(result, ensure_ascii=False), now, now,
                    detail if status in {"uncertain", "failed"} else "", worker["id"]))
        self.event(db, "agent.submission_finished", result, worker["job_id"], "agent_submission")
        if status == "uncertain":
            self._agent_release_missing_reconciliation_tab(db, worker)
        self._agent_refresh_pipeline(db, worker["pipeline_id"])
        return result

    def _agent_begin_final_action(self, db, worker, payload):
        from .trial_limits import read as read_trial, expired
        trial = read_trial(db, worker['pipeline_id'])
        if trial and (trial['state'] != 'active' or expired(trial) or worker['job_id'] not in trial['job_ids']):
            raise Conflict('The bounded trial does not authorize another final application action.')
        if worker['role'] != 'submission' or not worker['job_id']:
            raise Conflict('Only a scoped submission worker can begin its final application action.')
        if worker['stop_requested']:
            raise Conflict('Pipeline stop was recorded before the final application action.')
        snapshot = db.execute('SELECT jd_version,phase FROM jobs WHERE id=?',(worker['job_id'],)).fetchone()
        if not snapshot or snapshot['jd_version'] != worker['jd_version'] or self.profile_version(db) != worker['profile_version']:
            raise Conflict('Job or applicant snapshot changed before the final application action.')
        if snapshot['phase'] == 'submitted' or db.execute("SELECT 1 FROM agent_submission_results WHERE job_id=? AND state='submitted'",(worker['job_id'],)).fetchone():
            raise Conflict('This application already has a committed submission receipt.')
        from .ephemeral_auth import permitted_final
        email_verification=permitted_final(self,db,worker)
        current_context = self._agent_decode(worker['context'], {})
        observed = self._agent_latest_cua_event(db, worker, success=True)
        verification = (observed or {}).get('verification') or {}
        if (verification.get('present') and verification.get('filled_boxes') == 8
                and verification.get('submit_enabled') and not email_verification):
            from .ephemeral_auth import AUTH_ACTIONS
            already = [i for i in db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND state='approved'",(worker['id'],))
                       if i['action'] in AUTH_ACTIONS and self._agent_decode(i['context'],{}).get('jd_version') == worker['jd_version']
                       and self._agent_decode(i['context'],{}).get('profile_version') == worker['profile_version']]
            if already:
                return {'authorized':False,'verification_refresh_required':True,
                        'instruction':'The exact approval exists, but its token handoff is not current. Request a fresh handoff for this same step; do not ask for the same approval again.'}
            job_view=db.execute('SELECT title,url FROM jobs WHERE id=?',(worker['job_id'],)).fetchone()
            ready=self._agent_ready_artifacts(db,worker['pipeline_id'],worker['job_id'],worker['jd_version'],worker['profile_version'])
            item,_=self._agent_insert_inbox(db,worker,'approval',dedupe_key='approval:current_email_verification_continuation',
                action='enter_emailed_human_verification_code_to_submit',
                summary='Complete the currently displayed email-code verification and submit '+job_view['title']+'. Eight boxes are filled; no receipt is confirmed. Only this current step is requested.',
                context={'job_title':job_view['title'],'job_url':job_view['url'],'jd_version':worker['jd_version'],
                         'profile_version':worker['profile_version'],'source_event_id':observed['event_id'],
                         'ready_artifacts':self._agent_artifact_snapshot(ready),'verification_observation':verification})
            return {'authorized':False,'state':'waiting_approval','inbox_item_id':item['id'],
                    'instruction':'Return the final JSON with disposition waiting. Preserve this page; the human will approve the exact displayed step.'}
        from .context_budget import last_input, limit
        if (worker['session_id'] and not self._context_may_have_sent_final(current_context,worker['checkpoint'])
                and current_context.get('execution',{}).get('browser_backend')=='playwright'
                and last_input(db,worker['id'])>=limit(self.root)):
            current_context['context_handoff_pending']=True
            db.execute("UPDATE agent_workers SET state='queued',checkpoint='safe_context_handoff',context=?,capability_sha256='',capability_expires_at=NULL WHERE id=?",
                       (json.dumps(current_context),worker['id']))
            return {'authorized':False,'state':'waiting','context_handoff':True,
                    'instruction':'Return final JSON disposition waiting. The host will hand off a compact model context and retain the same browser, documents and exact approvals; no application action has started.'}
        gates=db.execute("SELECT * FROM agent_browser_controls WHERE state='open' AND scope_key IN (?,?)",
                      (self._agent_connector_scope(worker), "company:" + worker["company_id"])).fetchall()
        if any(not(email_verification and gate['control_type']=='company_auth' and gate['worker_id']==worker['id']) for gate in gates):
            raise Conflict("Authentication or connector recovery is still pending; final action cannot start.")
        if worker["role"] != "submission" or not worker["job_id"]:
            raise Conflict("Only a scoped submission worker can begin its final application action.")
        if worker["stop_requested"]:
            raise Conflict("Pipeline stop was recorded before the final application action.")
        current_context=self._agent_decode(worker['context'],{})
        freshness=current_context.get('browser_freshness',{})
        if freshness.get('pending'):
            observation=self._agent_latest_cua_event(db,worker,success=True) if email_verification else None
            job_url=db.execute('SELECT url FROM jobs WHERE id=?',(worker['job_id'],)).fetchone()
            observed_at=db.execute('SELECT created_at FROM agent_runtime_events WHERE id=?',(observation['event_id'],)).fetchone() if observation else None
            if not (observation and job_url and observation.get('application_url')==job_url[0]
                    and observed_at and observed_at[0]>=freshness.get('issued_at','')):
                raise Conflict("The saved application page may have expired. Refresh its browser/account session before final action.")
            freshness.update(pending=False,confirmed_at=timestamp(),evidence_id=observation['event_id'])
            current_context['browser_freshness']=freshness
            db.execute('UPDATE agent_workers SET context=? WHERE id=?',(json.dumps(current_context),worker['id']))
        job = db.execute("SELECT jd_version,phase FROM jobs WHERE id=?", (worker["job_id"],)).fetchone()
        profile_version = self.profile_version(db)
        if not job or job["jd_version"] != worker["jd_version"] or profile_version != worker["profile_version"]:
            raise Conflict("Job or applicant snapshot changed before the final application action.")
        membership = db.execute("SELECT selected FROM pipeline_jobs WHERE pipeline_id=? AND job_id=?",
                                (worker["pipeline_id"], worker["job_id"])).fetchone()
        if not membership or not membership["selected"]:
            raise Conflict("This job is no longer selected in the current pipeline.")
        previous_submissions = db.execute("SELECT 1 FROM agent_submission_results WHERE state='submitted' LIMIT 1").fetchone()
        review_id = None
        if not previous_submissions:
            review = db.execute("SELECT * FROM agent_inbox WHERE worker_id=? AND pipeline_id=? AND job_id=? "
                                "AND kind='first_application_review' AND state='approved' ORDER BY resolved_at DESC LIMIT 1",
                                (worker["id"], worker["pipeline_id"], worker["job_id"])).fetchone()
            if not review:
                raise Conflict("The first application needs the user's explicit review before its final action.")
            review_context = self._agent_decode(review["context"], {})
            if (review_context.get("jd_version") != worker["jd_version"]
                    or review_context.get("profile_version") != worker["profile_version"]):
                raise Conflict("The first-application review is stale; request review again with current details.")
            current_artifacts = self._agent_artifact_snapshot(self._agent_ready_artifacts(
                db, worker["pipeline_id"], worker["job_id"], worker["jd_version"], worker["profile_version"]))
            if review_context.get("ready_artifacts", []) != current_artifacts:
                raise Conflict("The first-application review no longer matches the current prepared documents.")
            review_id = review["id"]
        context = current_context
        if context.get("final_action_may_have_been_sent") or self._context_may_have_sent_final(context, worker["checkpoint"]):
            from .ephemeral_auth import final_scope
            scope=final_scope(self.root,worker['id']) if email_verification else None
            observation=self._agent_latest_cua_event(db,worker,success=True) if scope else None
            verified=(observation or {}).get('verification') or {}
            if not (scope and context.get('email_verification_final_scope')!=scope and verified.get('present')
                    and verified.get('filled_boxes')==8 and verified.get('submit_enabled')):
                if scope and not verified.get('present'):
                    raise Conflict('The approved verification needs a FULL current browser_snapshot with empty arguments, no target/depth/filename, before begin_final_action. Do not retry Submit or request the same approval.')
                raise Conflict("A final action may already have started; reconcile it instead of authorizing another click.")
            context['email_verification_final_scope']=scope
            self.event(db,'agent.email_verification_continuation_authorized',{'worker_id':worker['id'],'scope':scope,'browser_event_id':observation['event_id']},worker['job_id'],'agent_runtime')
        context["final_action_may_have_been_sent"] = True
        context["final_action_authorized_at"] = timestamp()
        context["final_action_review_item_id"] = review_id
        context["final_action_jd_version"] = worker["jd_version"]
        context["final_action_profile_version"] = worker["profile_version"]
        now = timestamp()
        db.execute("UPDATE agent_workers SET checkpoint='final_action_in_flight',context=?,updated_at=? WHERE id=?",
                   (json.dumps(context, ensure_ascii=False), now, worker["id"]))
        self.event(db, "agent.final_action_authorized", {"worker_id": worker["id"], "pipeline_id": worker["pipeline_id"],
                   "job_id": worker["job_id"], "review_item_id": review_id,
                   "jd_version": worker["jd_version"], "profile_version": worker["profile_version"]},
                   worker["job_id"], "agent_safety_boundary")
        return {"authorized": True, "checkpoint": "final_action_in_flight", "review_item_id": review_id,
                "jd_version": worker["jd_version"], "profile_version": worker["profile_version"]}

    def finish_agent_turn(self, worker_id, result):
        if not isinstance(result, dict):
            result = {"disposition": "failed", "error": "Worker returned a malformed transport result."}
        disposition = result.get("disposition") or "failed"
        if disposition not in {"completed", "waiting", "interrupted", "session_missing", "quota_paused", "failed"}:
            disposition = "failed"
        with self.connect(write=True) as db:
            worker = db.execute("SELECT w.*,p.stop_requested,p.mode FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                                "WHERE w.id=?", (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            if worker["mode"] != "agentic":
                raise Conflict("Worker belongs to a legacy pipeline.")
            context = self._agent_decode(worker["context"], {})
            if isinstance(result.get("context"), dict):
                # Final prose is model-authored. Preserve it only as a namespaced
                # observation; it cannot replace host-owned checkpoint fields.
                observations = context.setdefault("agent_observations", {})
                observations["runtime_context"] = {
                    key: value for key, value in result["context"].items()
                    if key not in AGENT_HOST_CONTEXT_KEYS}
            if result.get("session_id"):
                session_id = str(result["session_id"])[:256]
            else:
                session_id = worker["session_id"]
            if isinstance(result.get("checkpoint"), str) and result["checkpoint"]:
                checkpoint = result["checkpoint"][:256]
            else:
                checkpoint = worker["checkpoint"]
            existing_terminal = worker["state"] in {"completed", "discarded", "uncertain", "failed", "stopped", "quota_paused"}
            state = worker["state"]
            # A dashboard/tool action may have committed a more useful durable
            # error before the outer CLI turn returned (for example, an
            # incomplete discovery checkpoint requiring explicit resume).
            error = str(worker["error"] or result.get("error") or "")[:4000]
            provider_error = str(result.get("provider_error") or "")[:4000]
            if existing_terminal:
                # Tool actions own durable transitions; transport completion may
                # not overwrite their decision or waiting checkpoint.
                state = worker["state"]
            elif worker["stop_requested"]:
                state = "stopped"
                error = error or "Pipeline stopped at a safe worker boundary."
            elif worker["state"] == "queued" and (disposition in {"waiting", "completed"} or context.get('context_handoff_pending')):
                # A user may answer a just-created inbox item before the old
                # process exits. Keep the newly queued same-session turn intact.
                state = "queued"
            elif worker["role"] == "discovery" and worker["state"] == "running" and disposition in {"waiting", "completed"}:
                discovery = self._agent_decode(worker["result"], {})
                if not isinstance(discovery, dict):
                    discovery = {}
                continuation = discovery.get("continuation")
                if discovery.get("status") == "incomplete" and isinstance(continuation, dict):
                    if continuation.get("state") == "queued":
                        state = "queued"
                        error = ""
                    elif continuation.get("state") == "interrupted":
                        state = "interrupted"
                        error = error or str(continuation.get("error") or
                                             "Discovery requires explicit resume before continuing.")[:4000]
                    else:
                        state = "interrupted"
                        error = error or "Discovery continuation state was not recognized; explicit resume is required."
                elif disposition == "completed":
                    state = "failed"
                    error = error or "Discovery turn ended without a durable finish action."
                else:
                    state = "interrupted"
                    error = error or "Runtime paused without a durable inbox or child checkpoint."
            elif disposition == "waiting":
                if worker["state"] not in {"waiting_question", "waiting_approval", "waiting_child", "waiting_browser", "quota_paused"}:
                    state = "interrupted"
                    error = error or "Runtime paused without a durable inbox or child checkpoint."
            elif disposition == "quota_paused":
                state = "quota_paused"
                provider_error = provider_error or error or "Account quota was reached."
                quota = {"paused": True, "provider_error": provider_error,
                         "reason": provider_error, "paused_at": timestamp()}
                db.execute("INSERT INTO meta(key,value) VALUES('agent_quota_latch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(quota, ensure_ascii=False),))
            elif disposition == "interrupted":
                state = "uncertain" if worker["role"] == "submission" and self._context_may_have_sent_final(context, checkpoint) else "interrupted"
                error = error or "Worker was interrupted; resume is explicit."
            elif disposition == "session_missing":
                state = "uncertain" if worker["role"] == "submission" and self._context_may_have_sent_final(context, checkpoint) else "session_missing"
                if state == "session_missing":
                    error = error or "The runtime definitively confirmed the saved session is unavailable; replacement requires user confirmation."
            elif disposition == "failed":
                state = "uncertain" if worker["role"] == "submission" and self._context_may_have_sent_final(context, checkpoint) else "failed"
                error = error or "Agent worker failed."
            elif disposition == "completed":
                if worker["role"] == "document":
                    # Only a completed child turn promotes drafts and wakes parent.
                    temp_worker = dict(worker)
                    temp_worker["context"] = json.dumps(context)
                    db.execute("UPDATE agent_workers SET context=? WHERE id=?", (json.dumps(context, ensure_ascii=False), worker_id))
                    if not self._agent_finalize_document_child(db, worker_id):
                        state = "failed"
                        error = "Document child completed without publishing every requested current artifact."
                    else:
                        state = "completed"
                elif worker["state"] in {"completed", "discarded", "uncertain", "failed"}:
                    state = worker["state"]
                else:
                    state = "failed"
                    error = "Agent turn ended without its required durable finish action."
            now = timestamp()
            committed = self._agent_decode(worker["result"], {})
            has_commit = (worker["role"] == "evaluator" and committed.get("decision") in {"approve", "discard"}
                          or worker["role"] == "discovery" and committed.get("status") in {"complete", "incomplete", "blocked"}
                          or worker["role"] == "submission" and committed.get("status") in {"submitted", "discarded", "uncertain", "failed"})
            persisted_result = {**committed, "transport_result": result} if has_commit else result
            db.execute("UPDATE agent_workers SET state=?,turn_active=0,session_id=?,resume_mode=CASE WHEN ?='session_missing' THEN 'not_resumable' "
                       "WHEN ? IS NULL THEN resume_mode ELSE 'same_session' END,"
                       "checkpoint=?,context=?,error=?,provider_error=?,result=?,updated_at=?,"
                       "completed_at=CASE WHEN ? IN ('completed','discarded','uncertain','failed','stopped') THEN COALESCE(completed_at,?) ELSE completed_at END,"
                       "capability_sha256='',capability_expires_at=NULL WHERE id=?",
                       (state, session_id, disposition, session_id, checkpoint, json.dumps(context, ensure_ascii=False), error,
                        provider_error, json.dumps(persisted_result, ensure_ascii=False), now, state, now, worker_id))
            self.event(db, "agent.worker_turn_finished", {"worker_id": worker_id, "disposition": disposition,
                       "state": state, "error": error}, worker["job_id"], "agent_scheduler")
            db.execute("UPDATE agent_execution_runs SET finished_at=?,state=? WHERE worker_id=? AND generation=?",
                       (now, state, worker_id, worker["launch_generation"]))
            self._agent_wake_orphaned_auth_waiters(db)
            self._agent_refresh_pipeline(db, worker["pipeline_id"])
            return {"worker_id": worker_id, "state": state, "disposition": disposition,
                    "pipeline_id": worker["pipeline_id"], "error": error}

    def agent_pipeline_stop_requested(self, pipeline_id):
        with self.connect() as db:
            row = db.execute("SELECT stop_requested FROM pipelines WHERE id=? AND mode='agentic'", (pipeline_id,)).fetchone()
            if not row:
                raise KeyError(pipeline_id)
            return bool(row[0])

    def list_queued_agent_workers(self):
        with self.connect() as db:
            rows = db.execute("SELECT w.id FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id "
                              "WHERE w.state='queued' AND p.mode='agentic' AND p.stop_requested=0 "
                              "AND p.status NOT IN ('stopped','completed','stopping') "
                              "ORDER BY CASE w.role WHEN 'document' THEN 0 WHEN 'submission' THEN 1 WHEN 'evaluator' THEN 2 ELSE 3 END,"
                              "w.created_at,w.id").fetchall()
            return [row[0] for row in rows]
