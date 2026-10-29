"""Scoped stdio MCP bridge for persistent agent workers.

The bridge never accepts a pipeline/company/job scope from the model. A short-lived
worker capability is injected by CodexAgentRuntime and Store.agent_action performs
the authoritative scope and snapshot checks for every call.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Callable
import base64
import hashlib


PROTOCOL_VERSION = "2024-11-05"


def _object(properties: dict[str, Any] | None = None, required: tuple[str, ...] = (), *, additional=False):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": additional,
    }


_TEXT = {"type": "string"}
_NONEMPTY_TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
_CHECKPOINT = {"type": "string", "minLength": 1, "maxLength": 256}
_OBJ = {"type": "object"}
_BOOL = {"type": "boolean"}
_ARTIFACT = {"type": "string", "enum": ["resume", "cover_letter"]}
_CONTEXT_SECTIONS = ["pipeline", "job", "applicant_index", "inbox", "prepared_documents", "checkpoint",
                     "prepared_document_handles", "submission_state", "submission_history",
                     "discovery_frontier", "parent_context"]
_LISTING_IDENTITY = {
    **_object({"requisition_id": _TEXT, "url": _TEXT}, additional=False),
    "anyOf": [{"required": ["requisition_id"]}, {"required": ["url"]}],
}
_LISTING_RECORD = _object({
    "requisition_id": {"type": ["string", "null"]}, "url": _TEXT, "title": _TEXT,
    "jd": _TEXT, "metadata": _OBJ, "selected": _BOOL,
    "selection_reason": _TEXT, "observation": _OBJ,
}, ("url", "title", "jd", "selected"))
_DESCRIPTION_POINTER = _object({
    "text_block": {"type": "integer", "minimum": 0},
    "start_marker": {"type": "string", "minLength": 1},
    "end_marker": {"type": "string", "minLength": 1},
}, ("text_block", "start_marker", "end_marker"))
_LISTING_CAPTURE = _object({
    "requisition_id": {"type": ["string", "null"]}, "url": _TEXT, "title": _TEXT,
    "description_pointer": _DESCRIPTION_POINTER, "metadata": _OBJ, "selected": _BOOL,
    "selection_reason": _TEXT, "observation": _OBJ,
}, ("url", "title", "description_pointer", "selected"))


# Tool descriptions keep agents oriented; Store remains the authority for values,
# membership, snapshots, policy approvals and state transitions.
TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "get_browser_guidance": {
        "description": "Read guidance for this worker's assigned browser runtime. Native CUA returns its installed skill/API; Playwright returns its runtime adapter. Shell/file execution is not needed and arbitrary files cannot be read.",
        "inputSchema": _object({"document": {"type": "string", "enum": ["skill", "guidance", "confirmations", "api"]}}, ("document",)),
        "annotations": {"readOnlyHint": True},
    },
    "save_resume_base": {
        "description": "After inspecting a rendered resume, optionally register it as a reusable base for an agent-chosen broad role family. It becomes available only after this child successfully completes. A later writer must tailor it and check current facts; no approval or screening answer is inherited.",
        "inputSchema": _object({"artifact_id": _TEXT, "family": _TEXT, "summary": _TEXT}, ("artifact_id", "family", "summary")),
    },
    "browser_control": {
        "description": "Acquire the company login owner before login/code flows, or wait behind it. Complete auth only after a fresh observation of the correct account and exact application form, not merely a profile/dashboard. A mouse-input CDP timeout is scoped to the job and returns keyboard_fallback or job_recovery_exhausted without opening a global circuit; re-observe before a bounded authorized keyboard alternative. A possible send returns reconciliation_only. Other genuine connector failures may open a shared recovery checkpoint. Credentials are never applicant facts.",
        "inputSchema": _object({"operation": {"type": "string", "enum": ["acquire_auth", "complete_auth", "open_circuit", "complete_recovery"]},
                                 "reason": _TEXT}, ("operation",)),
    },
    "get_context": {
        "description": "Read live controls, bounded updates and only named sections needed. Optionally include source_ids to retrieve up to 16 exact indexed sources in the SAME call; prefer this over a separate controls call followed by retrieve_sources. Do not reload supplied launch sections.",
        "inputSchema": _object({
            "sections": {"type": "array", "uniqueItems": True,
                          "items": {"type": "string", "enum": _CONTEXT_SECTIONS}},
            "after_revision": {"type": "integer", "minimum": 0},
            "source_ids": {"type": "array", "items": _TEXT, "minItems": 1, "maxItems": 16, "uniqueItems": True},
        }),
        "annotations": {"readOnlyHint": True},
    },
    "lookup_listing": {
        "description": "Look up a listing already recorded in this worker's company pipeline by observed requisition ID or URL.",
        "inputSchema": _object({"requisition_id": _TEXT, "url": _TEXT}),
        "annotations": {"readOnlyHint": True},
    },
    "lookup_listings": {
        "description": "Check up to 50 observed listing identities in one scoped call. Each identity needs a requisition ID or exact URL; results preserve input order.",
        "inputSchema": _object({"listings": {"type": "array", "items": _LISTING_IDENTITY,
                                              "minItems": 1, "maxItems": 50}}, ("listings",)),
        "annotations": {"readOnlyHint": True},
    },
    "retrieve_sources": {
        "description": "Retrieve exact applicant source documents or indexed sections by opaque IDs/handles supplied in this worker's scoped applicant index. Up to 16 handles per call. Do not request unrelated sources or disclose their contents in the UI.",
        "inputSchema": _object({"source_ids": {"type": "array", "items": _TEXT,
                                               "minItems": 1, "maxItems": 16, "uniqueItems": True},
                                 "force": {"type":"boolean","description":"Return complete content again if it was lost to conversation compaction."}}, ("source_ids",)),
        "annotations": {"readOnlyHint": True},
    },
    "record_listing": {
        "description": "Incrementally record one listing observed by the discovery agent, including its full description and the agent's selection decision.",
        "inputSchema": _object({
            "requisition_id": {"type": ["string", "null"]}, "url": _TEXT, "title": _TEXT,
            "jd": _TEXT, "metadata": _OBJ, "selected": _BOOL,
            "selection_reason": _TEXT, "observation": _OBJ,
        }, ("url", "title", "jd", "selected")),
    },
    "record_listing_batch": {
        "description": "Atomically record up to 10 listings observed and interpreted by discovery. Each item needs title, exact URL, full observed description, and the agent's selected decision. Serialized batch size is limited to 1 MiB.",
        "inputSchema": _object({"listings": {"type": "array", "items": _LISTING_RECORD,
                                              "minItems": 1, "maxItems": 10}}, ("listings",)),
    },
    "capture_listing_batch": {
        "description": "Capture up to 10 listings from exact text blocks in the latest successful browser result for this worker/session. Supply unique exact start/end markers around each full job description; do not copy the JD into tool arguments. Store extracts the exact observed slice, hashes and versions it, and binds it to the actual browser event. Selection remains your decision.",
        "inputSchema": _object({"listings": {"type": "array", "items": _LISTING_CAPTURE,
                                              "minItems": 1, "maxItems": 10}}, ("listings",)),
    },
    "finish_discovery": {
        "description": "Report career-site discovery progress. Use complete only after all relevant coverage is finished. Incomplete is a durable progress yield, not a terminal failure: first save a short checkpoint with the owned tab/page context and make new listing or page progress; Store will queue this same worker and CLI session to continue. Use blocked only for an actual blocker that prevents further progress.",
        "inputSchema": _object({"status": {"type": "string", "enum": ["complete", "incomplete", "blocked"]},
                                 "detail": _TEXT, "coverage": _OBJ}, ("status", "detail")),
    },
    "update_job_description": {
        "description": "Save a fuller or corrected job description observed at the listing URL, with supporting observation metadata.",
        "inputSchema": _object({"jd": _TEXT, "source_url": _TEXT, "observation": _OBJ}, ("jd",)),
    },
    "ask_question": {
        "description": "Interrupt only for an unavoidable blocker after checking saved facts and relevant sources: a genuinely mandatory observed form answer, necessary personal decision/conflicting fact, or real authentication handoff. Do not survey JD skills, durations or depth; evaluate stretch fit with recorded uncertainties and omit unsupported optional claims. Include the exact blocker and why existing evidence or truthful omission cannot resolve it in context. Only this job pauses.",
        "inputSchema": _object({"question": _TEXT, "context": _OBJ, "dedupe_key": _TEXT,
                                 "response_hint": _TEXT,
                                 "fact_key": {"type": "string", "minLength": 1, "maxLength": 160},
                                 "fact_scope": {"type": "string", "enum": ["applicant", "job"]}}, ("question",)),
    },
    "lookup_workflow_plan": {
        "description": "Look up an agent-authored navigation/form plan only for the exact observed origin, role and structural fingerprint. Verify the current page structure before reusing any step; plans never authorize uploads, legal actions, CAPTCHA, or final submission.",
        "inputSchema": _object({"origin": _NONEMPTY_TEXT,
                                 "role": {"type": "string", "enum": ["discovery", "evaluator", "submission"]},
                                 "structure_fingerprint": _NONEMPTY_TEXT},
                               ("origin", "role", "structure_fingerprint"), additional=False),
        "annotations": {"readOnlyHint": True},
    },
    "save_workflow_plan": {
        "description": "Save a reusable agent-authored navigation/form plan after observing this exact origin and structural fingerprint. The plan describes page structure only; recheck the current page and follow all current action approvals.",
        "inputSchema": _object({"origin": _NONEMPTY_TEXT,
                                 "role": {"type": "string", "enum": ["discovery", "evaluator", "submission"]},
                                 "structure_fingerprint": _NONEMPTY_TEXT,
                                 "plan": _OBJ, "observation": _OBJ},
                                ("origin", "role", "structure_fingerprint", "plan", "observation"),
                                additional=False),
    },
    "finish_evaluation": {
        "description": "Score this one job's fit from 0 to 100 using the full JD and documented applicant evidence. 100 means all material requirements met or exceeded; near misses and transferable skills count. Only confirmed non-negotiable barriers block a role (score 0). All positive eligible scores enter the ranked backlog, including very low scores; 0 is not submitted. Explain material matches/gaps and uncertainty without skill surveys. This records an assessment; it does not launch a submission worker.",
        "inputSchema": _object({
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "eligibility": {"type": "string", "enum": ["eligible", "blocked"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": _TEXT,
            "requirements": {"type": "array", "minItems": 1, "maxItems": 16, "items": _object({
                "requirement": _TEXT,
                "importance": {"type": "string", "enum": ["core", "supporting", "preferred", "hard_eligibility"]},
                "match": {"type": "string", "enum": ["met", "mostly_met", "transferable", "missing", "unknown"]},
                "evidence": _TEXT}, ("requirement", "importance", "match", "evidence"))},
            "hard_blockers": {"type": "array", "maxItems": 8, "items": _object({
                "requirement": _TEXT, "job_evidence": _TEXT, "applicant_evidence": _TEXT},
                ("requirement", "job_evidence", "applicant_evidence"))},
            "observations": _OBJ}, ("score", "eligibility", "confidence", "reason", "requirements", "hard_blockers")),
    },
    "checkpoint": {
        "description": "Persist a short checkpoint label (1–256 characters) and resumable context such as the owned tab ID, current URL, page cursor, active category/filters, last processed listing, and remaining paths. Put detailed notes in context rather than in the checkpoint label.",
        "inputSchema": _object({"checkpoint": _CHECKPOINT, "context": _OBJ}, ("checkpoint",)),
    },
    "request_documents": {
        "description": "Request a dedicated document child using the configured materials model for artifacts supported by the current application form. This returns promptly; hand off and wait for that child.",
        "inputSchema": _object({"artifacts": {"type": "array", "items": _ARTIFACT, "minItems": 1, "uniqueItems": True},
                                 "reason": _TEXT, "form_context": _OBJ}, ("artifacts", "reason")),
    },
    "publish_documents": {
        "description": "Publish the document child draft for this job using applicant facts, source notes, and the application's existing Markdown/PDF style. Only requested artifact types are allowed.",
        "inputSchema": _object({"artifacts": {"type": "array", "minItems": 1, "items": _object({
            "kind": _ARTIFACT, "markdown": _TEXT, "source_notes": _OBJ,
        }, ("kind", "markdown"))}}, ("artifacts",)),
    },
    "edit_documents": {
        "description":"Tailor a current own draft (artifact:ID) or indexed verified resume_base:ID with exact grouped replacements. Supply its source hash; unchanged text is preserved. New immutable PDFs and real preview images are returned for review. Use a full publish only for a new document or a substantial restructuring.",
        "inputSchema":_object({'edits':{'type':'array','minItems':1,'maxItems':2,'items':_object({
            'kind':_ARTIFACT,'base_source_id':_TEXT,'expected_sha256':_TEXT,
            'replacements':{'type':'array','minItems':1,'maxItems':40,'items':_object({'old':_TEXT,'new':_TEXT},('old','new'))},
            'source_notes':_OBJ},('kind','base_source_id','expected_sha256','replacements'))}},('edits',)),
    },
    "request_approval": {
        "description": "Create a user-visible checkpoint only when a current action actually requires confirmation, or for first-application review. Routine resume/cover-letter uploads covered by runtime_controls.routine_document_uploads and the exact ready artifact upload_authorization use standing user authorization and do not need this tool. Other files/destinations, binding agreements and CAPTCHAs retain their actual policy gates. Use company_authentication or browser_circuit_recovery for the corresponding recovery gate.",
        "inputSchema": _object({"action": _TEXT, "summary": _TEXT, "observation": _OBJ,
                                 "dedupe_key": _TEXT}, ("action", "summary")),
    },
    "begin_final_action": {
        "description": "Immediately before the one final application click, atomically check the pipeline stop and current snapshot, enforce first-application review, and durably mark that the send may occur. Call only when the current form is complete and all required action approvals are resolved.",
        "inputSchema": _object(),
    },
    "finish_submission": {
        "description": "Record the submission agent's interpreted outcome and observations. For submitted, cite the actual current-session browser observations; the host attaches successful tool-result events separately.",
        "inputSchema": _object({
            "status": {"type": "string", "enum": ["submitted", "discarded", "uncertain", "failed"]},
            "detail": _TEXT, "observations": _OBJ,
        }, ("status", "detail")),
    },
}


ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "discovery": ("get_context", "get_browser_guidance", "lookup_listing", "lookup_listings", "capture_listing_batch",
                  "record_listing", "record_listing_batch", "finish_discovery",
                  "ask_question", "checkpoint", "request_approval", "lookup_workflow_plan", "save_workflow_plan", "browser_control"),
    "evaluator": ("get_context", "get_browser_guidance", "lookup_listing", "lookup_listings", "retrieve_sources", "update_job_description", "ask_question",
                  "finish_evaluation", "checkpoint", "request_approval", "lookup_workflow_plan", "save_workflow_plan", "browser_control"),
    "submission": ("get_context", "get_browser_guidance", "lookup_listing", "retrieve_sources", "ask_question", "checkpoint", "request_documents",
                   "request_approval", "begin_final_action", "finish_submission", "lookup_workflow_plan",
                   "save_workflow_plan", "browser_control"),
    "document": ("get_context", "retrieve_sources", "ask_question", "checkpoint", "publish_documents", "edit_documents", "save_resume_base"),
}


class AgentMCPBridge:
    def __init__(self, worker_id: str, capability: str, role: str,
                 action_handler: Callable[[str, str, str, dict[str, Any]], Any],
                 document_renderer: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None, *, root=None):
        if role not in ROLE_TOOLS:
            raise ValueError("Unknown agent role.")
        if not isinstance(worker_id, str) or not worker_id or not isinstance(capability, str) or not capability:
            raise ValueError("A scoped worker identity and capability are required.")
        self.worker_id = worker_id
        self._capability = capability
        self.role = role
        self._sent_preview_hashes = set()
        self._served_sources={}
        from app.efficiency_features import features
        context_root=root or os.environ.get('AGENT_ROOT')
        self._compact=bool(context_root and features(context_root)['compact_worker_context'])
        self._action_handler = action_handler
        self._document_renderer = document_renderer
        self._catalog = None
        if os.environ.get('AGENT_CONTRACT_ID') and context_root:
            from app.worker_contracts import load
            contract = load(context_root, worker_id, os.environ['AGENT_CONTRACT_ID'])
            if contract['role'] != role:raise ValueError('Pinned tool role does not match worker.')
            self._catalog = contract['agent_tools']

    def tools(self) -> list[dict[str, Any]]:
        return self._catalog if self._catalog is not None else [{"name": name, **TOOL_DEFINITIONS[name]} for name in ROLE_TOOLS[self.role]]

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name not in ROLE_TOOLS[self.role]:
            raise ValueError("This tool is not available to the current worker role.")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        # The model cannot smuggle task identity or authority fields through a
        # generic payload. Store validates the operation's complete schema.
        if any(key in arguments for key in ("worker_id", "pipeline_id", "job_id", "company_id", "capability", "token")):
            raise ValueError("Worker identity and scope are supplied by the bridge, not tool arguments.")
        if name == "lookup_listings":
            entries = arguments.get("listings")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
                raise ValueError("A batch lookup must contain between 1 and 50 listing identities.")
        if name == "record_listing_batch":
            entries = arguments.get("listings")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
                raise ValueError("A listing batch must contain between 1 and 10 records.")
            if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1024 * 1024:
                raise ValueError("A listing batch cannot exceed 1 MiB of serialized UTF-8 data.")
        if name == "capture_listing_batch":
            entries = arguments.get("listings")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
                raise ValueError("A capture batch must contain between 1 and 10 listings.")
            if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1024 * 1024:
                raise ValueError("A capture batch cannot exceed 1 MiB of serialized UTF-8 data.")
            for index, entry in enumerate(entries):
                pointer = entry.get("description_pointer") if isinstance(entry, dict) else None
                if (not isinstance(pointer, dict) or isinstance(pointer.get("text_block"), bool)
                        or not isinstance(pointer.get("text_block"), int) or pointer["text_block"] < 0
                        or not isinstance(pointer.get("start_marker"), str) or not pointer["start_marker"]
                        or not isinstance(pointer.get("end_marker"), str) or not pointer["end_marker"]):
                    raise ValueError(f"Listing {index + 1} needs a text-block index and exact nonempty JD markers.")
        if name == "retrieve_sources":
            source_ids = arguments.get("source_ids")
            if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 16:
                raise ValueError("A source retrieval must contain between 1 and 16 indexed IDs or section handles.")
        if name == "ask_question":
            has_key = "fact_key" in arguments
            has_scope = "fact_scope" in arguments
            if has_key != has_scope:
                raise ValueError("Reusable fact questions must include both fact_key and fact_scope, or neither.")
        if name == "finish_evaluation":
            tool = next(item for item in self.tools() if item["name"] == name)
            if "score" in tool["inputSchema"].get("required", []):
                from app.fit_scoring import assessment
                if assessment(arguments) is None:
                    raise ValueError("This evaluator contract requires a 0–100 fit assessment, not a binary decision.")
        arguments=dict(arguments)
        force=arguments.pop('force',False) if name=='retrieve_sources' else False
        value = self._action_handler(self.worker_id, self._capability, name, arguments)
        if self._compact and isinstance(value,dict) and isinstance(value.get('sources'),list):
            sources=[]
            for source in value['sources']:
                fingerprint=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                identity=source.get('id')
                if not force and self._served_sources.get(identity)==fingerprint:
                    sources.append({'id':identity,'unchanged_this_invocation':True,'sha256':source.get('sha256'),
                                    'retrieve_full':'retrieve_sources with force=true if previous content is unavailable'})
                else:sources.append(source)
                self._served_sources[identity]=fingerprint
            value={**value,'sources':sources}
        if name == 'get_context' and self.role in {'submission','discovery'} and os.environ.get('AGENT_ROOT'):
            from app.ephemeral_auth import hint
            instructions=hint(os.environ['AGENT_ROOT'],self.worker_id)
            if instructions:
                value['email_token_retrieval']=instructions
        if name=='checkpoint' and self.role in {'submission','discovery'} and arguments.get('checkpoint')=='awaiting_email_token_handoff' and os.environ.get('AGENT_ROOT'):
            from app.ephemeral_auth import wait_for_handoff
            value['authentication_handoff']=wait_for_handoff(os.environ['AGENT_ROOT'],self.worker_id)
        return value

    def invoke(self, name: str, arguments: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
        value = self.call(name, arguments)
        image_blocks: list[dict[str, str]] = []
        if name in {"publish_documents","edit_documents"} and self._document_renderer:
            rendered = self._document_renderer(value)
            summaries = []
            for item in rendered:
                summaries.append({key: item[key] for key in ("kind", "filename", "pages", "text_matches", "sha256")
                                  if key in item})
                for preview in item.get("previews", []):
                    path = Path(preview)
                    contents = path.read_bytes()
                    preview_hash = hashlib.sha256(contents).hexdigest()
                    if preview_hash in self._sent_preview_hashes:
                        value["unchanged_preview_already_returned_this_turn"] = True
                        continue
                    self._sent_preview_hashes.add(preview_hash)
                    image_blocks.append({"type": "image", "data": base64.b64encode(contents).decode("ascii"),
                                         "mimeType": "image/png"})
            value["rendered_previews"] = summaries
        return value, image_blocks


class _SmokeBridge:
    """Harmless local handshake fixture; deliberately unavailable in production runs."""
    role = "smoke"

    def tools(self):
        return [{"name": "ping", "description": "Return the harmless MCP bridge fixture response.",
                 "inputSchema": _object(), "annotations": {"readOnlyHint": True}}]

    def call(self, name, arguments):
        if name != "ping" or arguments != {}:
            raise ValueError("The smoke bridge exposes only an empty ping call.")
        return {"ok": True, "fixture": "agent-mcp-smoke", "transport": "stdio"}


def render_and_register_documents(store, worker_id: str, capability: str,
                                  published: dict[str, Any]) -> list[dict[str, Any]]:
    """Render published Markdown, then bind the resulting files to Store state.

    The document child can see the rendered page images, while the host records
    the canonical PDF/preview paths and hashes before that child can complete.
    """
    from app.rendering import render_agent_documents

    rendered = render_agent_documents(store, published, worker_id=worker_id)
    records = []
    root = Path(store.root).resolve()
    for item in rendered:
        previews = []
        for path in item["previews"]:
            preview_path = Path(path).resolve()
            try:
                relative_preview = preview_path.relative_to(root).as_posix()
            except ValueError as error:
                raise RuntimeError("Rendered preview escaped the application workspace.") from error
            previews.append({"path": relative_preview, "sha256": item["preview_hashes"][path]})
        records.append({
            "artifact_id": item["artifact_id"],
            "source_sha256": item["source_sha256"],
            "pdf_path": item["pdf_path"],
            "pdf_sha256": item["sha256"],
            "previews": previews,
        })
    renderer_sha256 = rendered[0].get("renderer_sha256") if rendered else None
    if not isinstance(renderer_sha256, str) or len(renderer_sha256) != 64:
        raise RuntimeError("Renderer did not return its content-addressed version.")
    registration = store.agent_action(worker_id, capability, "record_document_renders",
                                      {"renderer_sha256": renderer_sha256, "artifacts": records})
    expected = {record["artifact_id"]: record for record in records}
    actual = registration.get("recorded") if isinstance(registration, dict) else None
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise RuntimeError("Store did not confirm every rendered document registration.")
    for record in actual:
        source = expected.get(record.get("artifact_id")) if isinstance(record, dict) else None
        if (not source or record.get("pdf_sha256") != source["pdf_sha256"]
                or record.get("source_sha256") != source["source_sha256"]
                or record.get("pdf_path") != source["pdf_path"]):
            raise RuntimeError("Store returned a mismatched rendered document registration.")
    return rendered


def _text_result(value: Any, image_blocks: list[dict[str, str]] | None = None) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    result = {"content": [{"type": "text", "text": encoded}, *(image_blocks or [])], "isError": False}
    # Newer MCP clients can use structuredContent; the text block remains the
    # interoperable representation for Codex versions that ignore it.
    if isinstance(value, dict) and not (image_blocks and os.environ.get("AGENT_IMAGE_CONTENT_ONLY") == "1"):
        result["structuredContent"] = value
    return result


def serve(bridge) -> int:
    """Serve newline-delimited JSON-RPC MCP messages on stdio."""
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw.decode("utf-8"))
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                continue
            method = message.get("method")
            request_id = message.get("id")
            if request_id is None:  # notification
                continue
            params = message.get("params") or {}
            try:
                if method == "initialize":
                    requested = params.get("protocolVersion")
                    response = {"protocolVersion": requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION,
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": "job-application-agent-bridge", "version": "1.0.0"}}
                elif method == "ping":
                    response = {}
                elif method == "tools/list":
                    response = {"tools": bridge.tools()}
                elif method == "tools/call":
                    name = params.get("name")
                    arguments = params.get("arguments", {})
                    try:
                        if hasattr(bridge, "invoke"):
                            value, images = bridge.invoke(name, arguments)
                        else:
                            value, images = bridge.call(name, arguments), []
                        response = _text_result(value, images)
                    except Exception as error:
                        response = {"content": [{"type": "text", "text": str(error)}], "isError": True}
                else:
                    error = {"code": -32601, "message": "Method not found"}
                    sys.stdout.buffer.write((json.dumps({"jsonrpc": "2.0", "id": request_id,
                                                        "error": error}) + "\n").encode("utf-8"))
                    sys.stdout.buffer.flush()
                    continue
                outgoing = {"jsonrpc": "2.0", "id": request_id, "result": response}
            except Exception as error:
                outgoing = {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32602, "message": str(error)}}
            sys.stdout.buffer.write((json.dumps(outgoing, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        except (UnicodeDecodeError, json.JSONDecodeError):
            # A malformed frame receives no response; continue so one bad line
            # cannot terminate the long-lived server process.
            continue
    return 0


def main() -> int:
    if os.environ.get("AGENT_BRIDGE_SMOKE") == "1":
        return serve(_SmokeBridge())

    root = os.environ.get("AGENT_ROOT")
    worker_id = os.environ.get("AGENT_WORKER_ID")
    capability = os.environ.get("AGENT_CAPABILITY")
    role = os.environ.get("AGENT_ROLE")
    if not root or not worker_id or not capability or not role:
        print("Agent bridge launch context is incomplete.", file=sys.stderr)
        return 2
    # Import lazily so the harmless MCP fixture doesn't initialize or inspect
    # the user's production ledger.
    sys.path.insert(0, str(Path(root).resolve()))
    from app.store import Store
    store = Store(root)
    renderer = None
    if role == "document":
        renderer = lambda result: render_and_register_documents(store, worker_id, capability, result)
    return serve(AgentMCPBridge(worker_id, capability, role, store.agent_action, renderer))


if __name__ == "__main__":
    raise SystemExit(main())
