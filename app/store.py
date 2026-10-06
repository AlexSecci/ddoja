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


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.path = self.root / "data" / "pipeline.sqlite"
        with self.connect() as db:
            db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

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
            db.execute("INSERT INTO companies(id,name,careers_url,enabled) VALUES(?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,careers_url=excluded.careers_url,enabled=excluded.enabled",
                       (company_id, name, url, int(enabled)))
        return company_id

    def source(self, company_id, adapter, board_token):
        if adapter not in {"greenhouse", "lever", "lever_eu"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", board_token):
            raise ValueError("Use a supported adapter and the verified board token from the employer's career site.")
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
                raise KeyError(company_id)
            if db.execute("SELECT 1 FROM scans WHERE company_id=? AND status='running'", (company_id,)).fetchone():
                raise Conflict("Cannot change a source while its scan is running.")
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
            return job

    def listing(self, table):
        if table not in {"companies", "jobs", "technologies", "questions", "events", "source_documents", "evidence", "company_sources", "scans"}:
            raise ValueError("Invalid table.")
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

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

    def claim(self, stage, owner, lease_seconds=300):
        phases = {"evaluation": "new", "materials": "approved", "review": "approved", "submission": "ready_for_submission"}
        if stage not in phases or not 5 <= lease_seconds <= 3600 or not owner.strip():
            raise ValueError("Invalid stage, owner or lease duration.")
        with self.connect(write=True) as db:
            self.recover(db)
            if json.loads(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0]):
                return None
            # Submission will be wired only after the browser/approval integration is tested.
            if stage == "submission":
                raise Conflict("Live submission is not enabled in this foundation release.")
            statuses = (("awaiting_material_review", "retryable_review_error") if stage == "review" else
                        ("idle", "retryable_error", "awaiting_material_correction") if stage == "materials" else ("idle", "retryable_error"))
            placeholders = ",".join("?" for _ in statuses)
            while True:
                row = db.execute("SELECT j.* FROM jobs j JOIN companies c ON j.company_id=c.id "
                                 f"WHERE j.phase=? AND j.run_status IN ({placeholders}) AND c.enabled=1 "
                                 "ORDER BY j.first_seen_at,j.id LIMIT 1", (phases[stage], *statuses)).fetchone()
                if not row:
                    return None
                if stage == "materials" and row["run_status"] == "awaiting_material_correction":
                    count = db.execute("SELECT COUNT(*) FROM packages WHERE job_id=? AND jd_version=? AND profile_version=?",
                                       (row["id"], row["jd_version"], self.profile_version(db))).fetchone()[0]
                    if count >= 3:
                        db.execute("UPDATE jobs SET run_status='awaiting_manual_review',reason='Three material versions failed review; manual correction required.' WHERE id=?", (row["id"],))
                        self.event(db, "materials.revision_limit", {"versions": count}, row["id"])
                        continue
                break
            token = uuid.uuid4().hex
            until = time.time() + lease_seconds
            db.execute("UPDATE jobs SET run_status='running',active_stage=?,lease_owner=?,lease_token=?,lease_until=? WHERE id=?",
                       (stage, owner, token, until, row["id"]))
            self.event(db, "job.claimed", {"stage": stage, "owner": owner, "lease_until": until}, row["id"])
            return {"job_id": row["id"], "token": token, "stage": stage,
                    "jd_version": row["jd_version"], "profile_version": self.profile_version(db), "lease_until": until}

    def require_claim(self, db, claim, stage):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (claim["job_id"],)).fetchone()
        if not row or row["run_status"] != "running" or row["active_stage"] != stage \
                or row["lease_token"] != claim["token"] or row["lease_until"] <= time.time():
            raise Conflict("Worker claim is missing, expired or no longer owns this job.")
        if row["jd_version"] != claim["jd_version"] or self.profile_version(db) != claim["profile_version"]:
            raise Conflict("Job or applicant snapshot changed during worker execution.")
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

    def import_submission(self, job_id, path, kind, source, attribution="unknown"):
        if kind not in {"receipt", "employer_account"}:
            raise ValueError("Unsupported submission evidence type.")
        content = Path(path).read_text(encoding="utf-8-sig")
        job = self.get_job(job_id)
        requisition = job["requisition_id"]
        if not requisition or not re.search(r"(?<!\d)" + re.escape(requisition) + r"(?!\d)", content):
            raise ValueError("Submission evidence does not identify this requisition.")
        if kind == "receipt" and "Your Application Has Been Received" not in content:
            raise ValueError("Receipt lacks the saved confirmation text.")
        if kind == "employer_account" and not re.search(r"^\s*- generic: .*/" + re.escape(requisition) + r"\s*$", content, re.M):
            raise ValueError("Account snapshot does not list this application.")
        checksum = digest(content)
        evidence_root = self.root / "artifacts" / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        artifact = evidence_root / f"{checksum}.md"
        artifact.write_text(content, encoding="utf-8")
        with self.connect(write=True) as db:
            old = db.execute("SELECT id FROM evidence WHERE job_id=? AND kind=? AND sha256=?", (job_id, kind, checksum)).fetchone()
            if old:
                return old[0]
            evidence_id = uuid.uuid4().hex
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)", (evidence_id, job_id, kind,
                       str(artifact.relative_to(self.root)), checksum, source, attribution, timestamp()))
            db.execute("UPDATE jobs SET phase='submitted',run_status='idle',reason=?,active_stage=NULL,"
                       "lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                       (f"Confirmed by imported {kind} evidence; submission attribution {attribution}.", job_id))
            self.event(db, "submission.imported", {"evidence_id": evidence_id, "kind": kind, "attribution": attribution}, job_id, "legacy_import")
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
