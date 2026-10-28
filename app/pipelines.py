from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
import inspect
import threading

from .discovery import scan_company
from .store import Conflict, Store
from .workers import work_once


class PipelineManager:
    """Runs durable, company-scoped discovery and processing loops."""

    def __init__(self, store: Store, max_workers=3, *, executor: Executor | None = None):
        self.store = store
        self._pool = executor or ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="company-pipeline")
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    def start(self, pipeline_id):
        pipeline = self.store.get_pipeline(pipeline_id)
        if pipeline["status"] != "queued":
            return pipeline
        with self._lock:
            if self._shutdown:
                raise Conflict("The pipeline manager is shutting down.")
            current = self._futures.get(pipeline_id)
            if current and not current.done():
                raise Conflict("This pipeline already has a worker.")
            self._futures[pipeline_id] = self._pool.submit(self._run, pipeline_id)
        return self.store.get_pipeline(pipeline_id)

    def resume(self, pipeline_id):
        with self._lock:
            current = self._futures.get(pipeline_id)
            if current and not current.done():
                pipeline = self.store.get_pipeline(pipeline_id)
                if pipeline["status"] == "awaiting_user":
                    # A human can answer during the tiny interval after the worker
                    # saves its waiting state but before its Future completes.
                    pipeline = self.store.resume_pipeline(pipeline_id)
                    if pipeline["status"] == "queued":
                        current.add_done_callback(lambda _future: self._start_if_queued(pipeline_id))
                    return pipeline
                if pipeline["status"] in {"running", "stopping", "queued"}:
                    # A running or globally paused worker resumes itself at its
                    # durable checkpoint; do not replace its stop intent.
                    return pipeline
        pipeline = self.store.resume_pipeline(pipeline_id)
        if pipeline["status"] == "queued":
            return self.start(pipeline_id)
        return pipeline

    def _start_if_queued(self, pipeline_id):
        try:
            if not self.store.paused() and self.store.get_pipeline(pipeline_id)["status"] == "queued":
                self.start(pipeline_id)
        except (Conflict, KeyError, RuntimeError):
            pass

    def resume_manual_authentication(self, pipeline_id, job_id, provenance):
        """Persist user acknowledgment, then resume only after the prior worker exits."""
        self.store.acknowledge_manual_authentication(job_id, pipeline_id, provenance)
        with self._lock:
            current = self._futures.get(pipeline_id)
            if current is not None and not current.done():
                current.add_done_callback(lambda _future: self._activate_manual_authentication(pipeline_id, job_id))
                return self.store.get_pipeline(pipeline_id)
        self._activate_manual_authentication(pipeline_id, job_id)
        return self.store.get_pipeline(pipeline_id)

    def _activate_manual_authentication(self, pipeline_id, job_id):
        try:
            with self._lock:
                if self._shutdown:
                    return
            self.store.activate_manual_authentication_resume(job_id, pipeline_id)
            pipeline = self.store.get_pipeline(pipeline_id)
            if pipeline["status"] == "queued":
                self.start(pipeline_id)
                return
            if pipeline["status"] in {"blocked", "awaiting_user"}:
                pipeline = self.store.resume_pipeline(pipeline_id)
                if pipeline["status"] == "queued":
                    self.start(pipeline_id)
        except (Conflict, KeyError, RuntimeError):
            # A concurrent stop, stale snapshot or process shutdown keeps the
            # checkpoint blocked; the saved context is never silently reset.
            return

    def stop(self, pipeline_id):
        pipeline = self.store.request_pipeline_stop(pipeline_id)
        with self._lock:
            future = self._futures.get(pipeline_id)
        if future is not None and not future.done() and future.cancel():
            return self.store.finalize_pipeline(pipeline_id, status="stopped", stage="stopped", blockers=[])
        if future is None or future.done():
            if pipeline["status"] == "stopping":
                pipeline = self.store.finalize_pipeline(pipeline_id, status="stopped", stage="stopped", blockers=[])
        return pipeline

    def recover_interrupted(self):
        self.store.recover_interrupted_pipelines()

    def shutdown(self):
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            active = [pipeline_id for pipeline_id, future in self._futures.items() if not future.done()]
        for pipeline_id in active:
            try:
                self.store.request_pipeline_stop(pipeline_id)
            except (KeyError, Conflict):
                pass


    def _stopping(self, pipeline_id):
        try:
            return self.store.pipeline_stop_requested(pipeline_id)
        except KeyError:
            return True

    def _finish_stopped(self, pipeline_id, note=None):
        blockers = [note] if note else []
        try:
            self.store.finalize_pipeline(pipeline_id, status="stopped", stage="stopped", blockers=blockers)
        except KeyError:
            pass

    def _block(self, pipeline_id, note, *, stage=None, discovery_status=None, discovery=None, status="blocked"):
        self.store.finalize_pipeline(pipeline_id, status=status, stage=stage or "blocked",
                                     discovery_status=discovery_status, discovery=discovery,
                                     blockers=[note], respect_stop=True)

    def _pause_at_checkpoint(self, pipeline_id):
        """Hold the current worker at a durable safe boundary until global resume."""
        if not self.store.paused():
            if self._stopping(pipeline_id):
                self._finish_stopped(pipeline_id)
                return True
            return False
        pipeline = self.store.get_pipeline(pipeline_id)
        if pipeline["status"] not in {"stopped", "completed"}:
            self.store.set_pipeline_paused(pipeline_id)
        while self.store.paused():
            if self._stopping(pipeline_id) or self._shutdown:
                self._finish_stopped(pipeline_id, "Stopped at a safe checkpoint while the queue was paused.")
                return True
            threading.Event().wait(0.25)
        if self._stopping(pipeline_id):
            self._finish_stopped(pipeline_id)
            return True
        current = self.store.get_pipeline(pipeline_id)
        if current["status"] == "queued":
            self.store.set_pipeline_progress(pipeline_id, current["stage"], blockers=[])
        return False

    def _run(self, pipeline_id):
        try:
            pipeline = self.store.get_pipeline(pipeline_id)
            if pipeline["status"] == "queued":
                pipeline = self.store.start_pipeline(pipeline_id)
            if pipeline["status"] != "running":
                return
            if self._stopping(pipeline_id):
                self._finish_stopped(pipeline_id)
                return
            if self.store.paused() and self._pause_at_checkpoint(pipeline_id):
                return
            while self.store.get_pipeline(pipeline_id)["discovery_status"] != "complete":
                if self._pause_at_checkpoint(pipeline_id):
                    return
                self.store.set_pipeline_progress(pipeline_id, "discovery", discovery_status="running", blockers=[])
                if self._stopping(pipeline_id):
                    self._finish_stopped(pipeline_id)
                    return
                try:
                    report = scan_company(self.store, pipeline["company_id"], pipeline_id=pipeline_id)
                except Exception as error:
                    if self._stopping(pipeline_id):
                        self._finish_stopped(pipeline_id, "Stopped during discovery; coverage is incomplete.")
                        return
                    self._block(pipeline_id, f"Discovery is blocked: {error}", stage="discovery",
                                discovery_status="failed")
                    return
                self.store.update_pipeline(pipeline_id, discovery_status=report["status"], discovery=report)
                if self._stopping(pipeline_id):
                    self._finish_stopped(pipeline_id, "Stopped during discovery; coverage is incomplete.")
                    return
                if report["status"] != "complete":
                    if self.store.paused():
                        if self._pause_at_checkpoint(pipeline_id):
                            return
                        # A scan interrupted by the global queue pause is not
                        # complete coverage; repeat it after the user resumes.
                        continue
                    detail = report.get("detail") or "The public feed did not return complete coverage."
                    self._block(pipeline_id, f"Discovery {report['status']}: {detail}", stage="discovery",
                                discovery_status=report["status"], discovery=report)
                    return

            attempted_submissions = set()
            while True:
                if self._pause_at_checkpoint(pipeline_id):
                    return
                if self._stopping(pipeline_id):
                    self._finish_stopped(pipeline_id)
                    return
                progressed = False
                # Match the existing offline scheduler order and commit one task at
                # a time so a stop request never starts another job.
                for stage in ("review", "materials", "evaluation"):
                    if self._pause_at_checkpoint(pipeline_id):
                        return
                    if self._stopping(pipeline_id):
                        self._finish_stopped(pipeline_id)
                        return
                    self.store.set_pipeline_progress(pipeline_id, stage)
                    if self._stopping(pipeline_id):
                        self._finish_stopped(pipeline_id)
                        return
                    try:
                        result = work_once(self.store, stage, pipeline_id=pipeline_id)
                    except Exception as error:
                        if self._stopping(pipeline_id):
                            self._finish_stopped(pipeline_id, f"Stopped after the current {stage} task ended with an error.")
                            return
                        self._block(pipeline_id, f"{stage.title()} stopped after an error: {error}", stage=stage)
                        return
                    if result.get("status") == "completed":
                        progressed = True
                        break
                    if self.store.paused():
                        if self._pause_at_checkpoint(pipeline_id):
                            return
                        break
                if progressed:
                    if self._stopping(pipeline_id):
                        self._finish_stopped(pipeline_id)
                        return
                    continue

                # Submit only a reviewed current package. Awaiting-user, blocked,
                # failed and uncertain applications are not blindly retried in this run.
                candidates = self.store.pipeline_submission_jobs(pipeline_id, attempted_submissions)
                if candidates:
                    from .submission import submit_once
                    for job_id in candidates:
                        if self._pause_at_checkpoint(pipeline_id):
                            return
                        if self._stopping(pipeline_id):
                            self._finish_stopped(pipeline_id)
                            return
                        attempted_submissions.add(job_id)
                        self.store.set_pipeline_progress(pipeline_id, "submission")
                        if self._stopping(pipeline_id):
                            self._finish_stopped(pipeline_id)
                            return
                        try:
                            result = submit_once(self.store, job_id, pipeline_id=pipeline_id)
                        except Exception as error:
                            # A thrown error can occur after a final click. Store.fail
                            # converts an owned submission claim to uncertainty.
                            try:
                                job = self.store.get_job(job_id)
                                if job["active_stage"] == "submission" and job["lease_token"]:
                                    self.store.fail({"job_id": job_id, "token": job["lease_token"], "stage": "submission"}, str(error))
                            except (Conflict, KeyError):
                                pass
                            if self._stopping(pipeline_id):
                                self._finish_stopped(pipeline_id, "Stopped after the current submission checkpoint.")
                                return
                            self._block(pipeline_id, f"Submission stopped safely for this job: {error}", stage="submission")
                            return
                        status = result.get("status") if isinstance(result, dict) else None
                        if status not in {"submitted", "awaiting_user", "uncertain", "blocked", "failed"}:
                            self._block(pipeline_id, "Submission worker returned an unsupported status; manual reconciliation is required.",
                                        stage="submission")
                            return
                        if status == "submitted":
                            job = self.store.get_job(job_id)
                            if job["phase"] != "submitted" or not job["evidence"]:
                                self._block(pipeline_id, "Submission worker reported success without verified evidence.",
                                            stage="submission")
                                return
                        if self._stopping(pipeline_id):
                            note = "Stopped after the current submission checkpoint. Check uncertain applications before resuming."
                            self._finish_stopped(pipeline_id, note if status == "uncertain" else None)
                            return
                        if self.store.paused():
                            if self._pause_at_checkpoint(pipeline_id):
                                return
                            break
                    # Keep processing independent jobs after a pending answer,
                    # blocked origin, failed form or uncertain outcome.
                    continue

                pipeline = self.store.get_pipeline(pipeline_id)
                counts = pipeline["counts"]
                if pipeline["discovery_status"] == "complete" and counts["selected"] == counts["submitted"] + counts["discarded"]:
                    self.store.finalize_pipeline(pipeline_id, status="completed", stage="completed",
                                                 blockers=[], respect_stop=True)
                    return
                jobs = self.store.pipeline_jobs(pipeline_id)["jobs"]
                unresolved = [job for job in jobs if job["selected"] and not job["manual_discard_reason"]
                              and job["phase"] not in {"discarded", "submitted"}]
                if any(job["run_status"] in {"awaiting_information", "awaiting_user", "awaiting_material_review", "awaiting_manual_review"}
                       for job in unresolved):
                    self.store.set_pipeline_waiting(pipeline_id, ["One or more selected jobs need a user answer or review."])
                    return
                if any(job["run_status"] == "submission_uncertain" for job in unresolved):
                    note = "A submission outcome is uncertain. Reconcile it before retrying; this run will not resubmit it."
                elif unresolved:
                    details = [f"{job['title']}: {job['reason'] or job['run_status']}" for job in unresolved[:5]]
                    note = "Selected jobs remain unfinished: " + "; ".join(details)
                else:
                    note = "The run is not terminal because discovery coverage is incomplete. Resume to scan again."
                self._block(pipeline_id, note, stage="processing")
                return
        except Exception as error:
            try:
                self._block(pipeline_id, f"Pipeline worker stopped after an error: {error}")
            except Exception:
                pass



class AgentPipelineManager:
    """Incrementally schedules persistent browser/discovery agent tasks.

    The Store owns task state and atomic concurrency claims. This manager owns
    only live executor futures; a process restart therefore does not erase a
    worker's session or checkpoint and never silently restarts it.
    """

    limits = {
        "discovery_per_company": 1,
        "evaluators_per_pipeline": 3,
        "submissions_per_pipeline": 3,
        "documents_per_parent": 1,
        "document_children_per_parent": 1,
    }

    def __init__(self, store: Store, runtime_factory=None, max_workers=24, document_workers=8,
                 *, executor: Executor | None = None, document_executor: Executor | None = None):
        self.store = store
        self.runtime_factory = runtime_factory
        self._pool = executor or ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-worker")
        self._document_pool = document_executor or ThreadPoolExecutor(
            max_workers=document_workers, thread_name_prefix="agent-document")
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        self._shutdown_event = threading.Event()
        self._scheduling = False
        self._schedule_requested = False
        self._trial_monitor = None

    def _ensure_trial_monitor(self):
        if self._trial_monitor is not None or self._shutdown or not self.store.active_agent_trials():
            return
        with self._lock:
            if self._trial_monitor is None and not self._shutdown:
                self._trial_monitor = threading.Thread(target=self._monitor_trials, name="bounded-agent-trials", daemon=True)
                self._trial_monitor.start()

    def _monitor_trials(self):
        while not self._shutdown_event.wait(1):
            try:
                for policy in self.store.active_agent_trials():
                    reason = self.store.agent_trial_stop_reason(policy["pipeline_id"])
                    if reason:
                        self.stop(policy["pipeline_id"])
                        self.store.close_agent_trial(policy["pipeline_id"], reason)
            except Exception:
                # Admission and final-action checks enforce limits even if this
                # independent stop loop encounters a temporary database lock.
                continue

    def pipeline_view(self, pipeline_id):
        pipeline = self.store.get_pipeline(pipeline_id)
        full_workers = self.store.list_agent_workers(pipeline_id) if pipeline.get("mode") == "agentic" else []
        # Pipeline cards are polled frequently. Keep transcripts, large worker
        # contexts, prepared documents and result observations on the explicit
        # per-pipeline /workers detail route.
        worker_fields = ("id", "parent_id", "pipeline_id", "job_id", "role", "state", "session_id",
                         "resume_mode", "checkpoint", "error", "provider_error", "updated_at",
                         "turn_active", "jd_version", "profile_version")
        workers = [{**{key: worker.get(key) for key in worker_fields},
                    "context": {key: worker.get("context", {}).get(key) for key in ("execution", "execution_change_deferred")
                                if key in worker.get("context", {})}} for worker in full_workers]
        counts = pipeline.get("counts", {})
        return {"pipeline": pipeline, "workers": workers, "counts": counts,
                "limits": dict(self.limits) if pipeline.get("mode") == "agentic" else {},
                "quota": self.store.get_agent_quota(),
                "active_workers": [worker["id"] for worker in workers
                                   if worker["state"] in {"running", "queued", "waiting_child"}
                                   or worker.get("turn_active")],
                "waiting_workers": [worker["id"] for worker in workers
                                    if worker["state"] in {"waiting_question", "waiting_approval", "quota_paused"}]}

    def list_views(self):
        return [self.pipeline_view(item["id"]) for item in self.store.list_pipelines()
                if item.get("mode") == "agentic"]

    def start(self, pipeline_id):
        pipeline = self.store.get_pipeline(pipeline_id)
        if pipeline.get("mode") != "agentic":
            raise Conflict("The agentic manager only starts agentic pipelines.")
        if pipeline["status"] in {"stopped", "stopping", "completed"}:
            return self.pipeline_view(pipeline_id)
        self.schedule()
        return self.pipeline_view(pipeline_id)

    def stop(self, pipeline_id):
        self.store.request_agent_pipeline_stop(pipeline_id)
        with self._lock:
            workers = {worker["id"] for worker in self.store.list_agent_workers(pipeline_id)}
            for worker_id in workers:
                future = self._futures.get(worker_id)
                if future is not None and not future.done():
                    # A queued future is safe to cancel. A running CLI is stopped
                    # cooperatively by should_stop at its next event/checkpoint.
                    if future.cancel():
                        try:
                            self.store.release_agent_worker_reservation(worker_id)
                        except (Conflict, KeyError):
                            pass
        return self.pipeline_view(pipeline_id)

    def resume(self, pipeline_id):
        self.store.resume_agent_pipeline(pipeline_id)
        self.schedule()
        return self.pipeline_view(pipeline_id)

    def refresh_job_profile(self, pipeline_id, job_id, provenance):
        refresh = self.store.refresh_agent_job_profile(pipeline_id, job_id, provenance)
        # Idempotent requests still call the scheduler: a prior request may
        # have queued its evaluator immediately before a process interruption.
        self.schedule()
        view = self.pipeline_view(pipeline_id)
        view["profile_refresh"] = refresh
        return view

    def resume_replacement(self, pipeline_id, worker_id, provenance, replacement_reason):
        worker = self.store.get_agent_worker(worker_id)
        if worker["pipeline_id"] != pipeline_id:
            raise Conflict("The saved worker does not belong to this pipeline.")
        self.store.confirm_agent_session_replacement(worker_id, provenance, replacement_reason)
        self.schedule()
        return self.pipeline_view(pipeline_id)

    def resume_quota(self):
        quota = self.store.resume_agent_quota()
        self.schedule()
        queued = [worker for worker in self.store.list_agent_workers()
                  if worker["state"] == "queued"]
        return {"quota": quota, "workers": queued}

    def recover_interrupted(self):
        # Record process loss for explicit resume; startup never launches saved work.
        return self.store.recover_interrupted_agent_workers()

    def schedule(self):
        self._ensure_trial_monitor()
        with self._lock:
            if self._shutdown:
                return 0
            if self._scheduling:
                self._schedule_requested = True
                return 0
            self._scheduling = True
            submitted = 0
            try:
                while True:
                    self._schedule_requested = False
                    queued = self.store.list_queued_agent_workers()
                    for worker_id in queued:
                        current = self._futures.get(worker_id)
                        if current is not None and not current.done():
                            continue
                        try:
                            worker = self.store.get_agent_worker(worker_id)
                            # Claim the durable role/company/pipeline slot before
                            # putting work into an executor queue. This prevents
                            # duplicate enqueues and cap oversubscription.
                            if not self.store.reserve_agent_worker(worker_id):
                                continue
                            target = self._document_pool if worker["role"] == "document" else self._pool
                            try:
                                future = target.submit(self._run_worker, worker_id)
                            except Exception:
                                self.store.release_agent_worker_reservation(worker_id)
                                raise
                            self._futures[worker_id] = future
                            future.add_done_callback(lambda finished, current_id=worker_id:
                                                     self._future_finished(current_id, finished))
                            submitted += 1
                        except (Conflict, KeyError, RuntimeError):
                            continue
                    if not self._schedule_requested:
                        break
                return submitted
            finally:
                self._scheduling = False

    def _future_finished(self, worker_id, future):
        with self._lock:
            if self._futures.get(worker_id) is future:
                self._futures.pop(worker_id, None)
        # Reserve claims enforce caps. Re-running the scheduler after each turn
        # immediately fills slots freed by completion or a job-local wait.
        try:
            self.schedule()
        except (Conflict, KeyError, RuntimeError):
            pass

    def _runtime(self):
        if self.runtime_factory is None:
            from .agent_runtime import CodexAgentRuntime
            return CodexAgentRuntime(self.store.root)
        if hasattr(self.runtime_factory, "run"):
            return self.runtime_factory
        try:
            signature = inspect.signature(self.runtime_factory)
            signature.bind(self.store)
        except (TypeError, ValueError):
            return self.runtime_factory()
        return self.runtime_factory(self.store)

    def _stopping(self, worker_id, pipeline_id):
        if self._shutdown_event.is_set():
            return True
        try:
            if self.store.agent_pipeline_stop_requested(pipeline_id):
                return True
            trial = self.store.get_agent_trial(pipeline_id)
            if trial and (trial["state"] != "active" or time.time() >= trial["deadline_unix"]):
                return True
            state = self.store.get_agent_worker(worker_id)["state"]
            # Agent actions such as finish_evaluation/finish_submission commit
            # the durable result before the CLI returns. Keep that same turn
            # alive for its acknowledgment and safe browser-tab cleanup.
            return state in {"stopped", "interrupted", "quota_paused"}
        except (KeyError, Conflict):
            return True

    def _run_worker(self, worker_id):
        worker = self.store.get_agent_worker(worker_id)
        pipeline_id = worker["pipeline_id"]
        result = None

        def on_event(event):
            if isinstance(event, dict) and event.get("type") == "usage":
                try:
                    self.store.record_agent_usage(worker_id, event)
                except (ValueError, Conflict, KeyError) as error:
                    # Usage is an efficiency signal, not a work authorization.
                    # Keep a diagnostic transport event while allowing the
                    # actual worker turn to finish normally.
                    event = {**event, "usage_persistence_error": str(error)[:1000]}
            self.store.agent_runtime_event(worker_id, event)
            # MCP tool results are emitted after the scoped Store action. In
            # particular, a discovery listing may have inserted its evaluator;
            # dispatch it now instead of waiting for discovery to finish.
            self.schedule()

        try:
            task = self.store.agent_launch_context(worker_id)
            result = self._runtime().run(task, on_event,
                                         lambda: self._stopping(worker_id, pipeline_id))
            if not isinstance(result, dict):
                result = {"disposition": "failed", "error": "Agent runtime returned an invalid result."}
        except Exception as error:
            latest = self.store.get_agent_worker(worker_id)
            result = {"disposition": "failed", "error": str(error)[:4000],
                      "session_id": latest.get("session_id"), "checkpoint": latest.get("checkpoint")}
        try:
            self.store.finish_agent_turn(worker_id, result)
        except (Conflict, KeyError, RuntimeError) as error:
            # Preserve the visible task rather than launching a replacement on a
            # callback bookkeeping failure. Recovery is explicit on next start.
            try:
                self.store.finish_agent_turn(worker_id, {
                    "disposition": "failed", "error": f"Could not persist worker completion: {error}"[:4000],
                    "session_id": result.get("session_id") or worker.get("session_id"),
                    "checkpoint": result.get("checkpoint") or worker.get("checkpoint"),
                })
            except Exception:
                pass
        finally:
            try:
                self.schedule()
            except (Conflict, KeyError, RuntimeError):
                pass
        return result

    def shutdown(self):
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._shutdown_event.set()
            futures = list(self._futures.values())
        for future in futures:
            future.cancel()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._document_pool.shutdown(wait=True, cancel_futures=True)
        self.store.recover_interrupted_agent_workers()

