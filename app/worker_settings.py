"""Validated worker preferences and immutable per-invocation execution choices."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROLE_KEYS = {"discovery": "discovery", "evaluator": "evaluation", "document": "materials", "submission": "submission"}
ROLE_LABELS = {"discovery": "Discovery", "evaluation": "Evaluation", "materials": "Résumé and cover letter", "submission": "Submission"}
SETTINGS_KEY = "worker_runtime_settings_v1"
OPENAI_MODELS = {
    "gpt-6-luna": "Luna 6", "gpt-6.1-sol": "Sol 6.1", "gpt-6.1-terra": "Terra 6.1",
    "gpt-6-astra": "Astra 6", "gpt-6-sol": "Sol 6", "gpt-5.6-sol": "Sol 5.6", "gpt-5.6-luna": "Luna 5.6",
}


class SettingsConflict(ValueError):
    pass


def model_catalog(root):
    """Use the local signed-in catalog; never spend a model call to populate UI."""
    cached = {}
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    for filename in ("models_cache.json", "models_cache.codex_work_desktop.json"):
        try:
            for model in json.loads((home / filename).read_text(encoding="utf-8")).get("models", []):
                cached.setdefault(model.get("slug"), model)
        except (OSError, ValueError, TypeError):
            continue
    models = []
    for slug, label in OPENAI_MODELS.items():
        data = cached.get(slug, {})
        efforts = [v["effort"] for v in data.get("supported_reasoning_levels", []) if isinstance(v, dict) and v.get("effort")]
        if not efforts:
            efforts = ["low", "medium", "high", "xhigh", "max"] + ([] if "luna" in slug else ["ultra"])
        models.append({"id": slug, "name": label, "efforts": efforts})
    return [
        {"id": "openai", "name": "Codex subscription", "authentication": "chatgpt", "models": models,
         "browser_backends": ["cua", "playwright"], "default_browser_backend": "cua"},
        {"id": "deepseek", "name": "DeepSeek API", "authentication": "api_key", "models": [
            {"id": "deepseek-flash", "name": "DeepSeek V4.1 Flash 1017 (current alias)", "efforts": ["low", "high", "max"]}],
         "browser_backends": ["playwright"], "default_browser_backend": "playwright"},
    ]


def defaults(root):
    try:
        config = json.loads((Path(root) / "config/workers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    fallback = {"discovery": ("gpt-6-luna", "medium"), "evaluation": ("gpt-6.1-sol", "high"),
                "materials": ("gpt-6.1-sol", "high"), "submission": ("gpt-6-luna", "medium")}
    roles = {}
    for key, (model, effort) in fallback.items():
        value = config.get("models", {}).get(key, {})
        if key == "materials":
            value = config.get("models", {}).get("document", value)
        roles[key] = {"provider": "openai", "model": value.get("model", model), "effort": value.get("effort", effort),
                      "browser_backend": "none" if key == "materials" else "cua"}
    return {"revision": 0, "roles": roles}


def read_settings(root, db):
    row = db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone()
    return json.loads(row[0]) if row else defaults(root)


def validate_roles(root, roles):
    if not isinstance(roles, dict) or set(roles) != set(ROLE_LABELS):
        raise ValueError("Choose settings for all four worker types.")
    providers = {p["id"]: p for p in model_catalog(root)}
    result = {}
    for role, choice in roles.items():
        if not isinstance(choice, dict) or set(choice) - {"provider", "model", "effort", "browser_backend"}:
            raise ValueError("Worker settings contain unsupported fields.")
        provider = providers.get(choice.get("provider"))
        if provider is None:
            raise ValueError("Unsupported model provider.")
        model = next((m for m in provider["models"] if m["id"] == choice.get("model")), None)
        if model is None or choice.get("effort") not in model["efforts"]:
            raise ValueError("Choose a supported model and reasoning effort.")
        backend = choice.get("browser_backend", "none" if role == "materials" else provider["default_browser_backend"])
        if backend not in (["none"] if role == "materials" else provider["browser_backends"]):
            raise ValueError("The browser runtime is not supported for this provider and worker type.")
        result[role] = {"provider": provider["id"], "model": model["id"], "effort": choice["effort"], "browser_backend": backend}
    return result


def save_settings(root, db, revision, roles):
    current = read_settings(root, db)
    if type(revision) is not int or revision != current["revision"]:
        raise SettingsConflict("Settings changed in another window. Reload before saving.")
    result = {"revision": revision + 1, "roles": validate_roles(root, roles)}
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (SETTINGS_KEY, json.dumps(result)))
    return result


def desired_execution(root, db, worker):
    saved = read_settings(root, db)
    choice = dict(saved["roles"][ROLE_KEYS[worker["role"]]])
    context = json.loads(worker["context"]) if isinstance(worker["context"], str) else worker["context"]
    previous = context.get("execution", {})
    session = worker["session_id"]
    if choice["provider"] == "openai" and (not previous or previous.get("provider") == "openai") and session:
        transport = "app_server" if str(session).startswith("app_server:") else "exec"
    else:
        try:
            host = json.loads((Path(root) / "config/workers.json").read_text()).get("execution_host", "exec")
        except (OSError, ValueError):
            host = "exec"
        transport = "app_server" if choice["provider"] == "openai" and worker["role"] == "document" and host != "exec" else "exec"
    return {**choice, "transport": transport, "settings_revision": saved["revision"]}


def pin_execution(root, db, worker, *, may_have_sent=False, now):
    """Called under the scheduler reservation transaction, never on an active turn."""
    choice = desired_execution(root, db, worker)
    context = json.loads(worker["context"]) if isinstance(worker["context"], str) else dict(worker["context"])
    old = context.get("execution")
    if not old and (worker["session_id"] or may_have_sent):
        old = {**defaults(root)["roles"][ROLE_KEYS[worker["role"]]],
               "transport": "app_server" if str(worker["session_id"]).startswith("app_server:") else "exec", "settings_revision": 0}
    if may_have_sent and old and any(choice[k] != old.get(k) for k in ("provider", "model", "effort", "browser_backend")):
        context["execution_change_deferred"] = {"requested": choice, "reason": "Reconcile the possibly sent application on its original runtime first."}
        choice = old
    else:
        context.pop("execution_change_deferred", None)
    switch_session = bool(old and worker["session_id"] and any(choice[k] != old.get(k) for k in ("provider", "transport", "browser_backend")))
    session = worker["session_id"]
    if switch_session:
        archive = {"execution": old, "session_id": session, "checkpoint": worker["checkpoint"], "archived_at": now,
                   "tab_id": context.get("tab_id"), "current_url": context.get("current_url", context.get("tab_url"))}
        context.setdefault("execution_history", []).append(archive)
        context["execution_handoff"] = {"previous_session_id": session, "previous_execution": old,
            "reason": "Worker preferences changed at a stopped invocation boundary.", "reobserve_before_input": True}
        session = None
        context.pop("last_context_revision", None)
        context.pop("instruction_digest", None)
        if choice["browser_backend"] != old.get("browser_backend"):
            context.pop("tab_id", None)
            context.pop("tab_url", None)
    context["execution"] = choice
    db.execute("UPDATE agent_workers SET context=?,session_id=?,resume_mode=? WHERE id=?",
               (json.dumps(context), session, "same_session" if session else "new_session", worker["id"]))
    db.execute("INSERT OR REPLACE INTO agent_execution_runs(worker_id,generation,execution,previous_session_id,started_at) VALUES(?,?,?,?,?)",
               (worker["id"], worker["launch_generation"] + 1, json.dumps(choice), worker["session_id"], now))
    return choice
