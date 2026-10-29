"""Validate agent-authored fit assessments; never infer applicant eligibility."""

SCORING_VERSION = 1


def submission_candidate(fit):
    return fit["score"] > 0 and fit["eligibility"] == "eligible"


def assessment(payload):
    # Saved workers pinned to the old binary tool contract remain resumable.
    # Their decisions are explicitly unscored, never assigned a fabricated score.
    if "score" not in payload and payload.get("decision") in {"approve", "discard"}:
        return None
    score = payload.get("score")
    if type(score) is not int or not 0 <= score <= 100:
        raise ValueError("Fit score must be an integer from 0 to 100.")
    eligibility = payload.get("eligibility")
    if eligibility not in {"eligible", "blocked"}:
        raise ValueError("Eligibility must be eligible or blocked, based on confirmed hard barriers.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 12000:
        raise ValueError("Explain the fit assessment in 1 to 12000 characters.")
    confidence = payload.get("confidence")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError("Assessment confidence must be high, medium, or low.")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 16:
        raise ValueError("Assess 1 to 16 material requirements, grouping related items.")
    for item in requirements:
        if (not isinstance(item, dict)
                or item.get("importance") not in {"core", "supporting", "preferred", "hard_eligibility"}
                or item.get("match") not in {"met", "mostly_met", "transferable", "missing", "unknown"}
                or any(not isinstance(item.get(k), str) or not item[k].strip() or len(item[k]) > 4000
                       for k in ("requirement", "evidence"))):
            raise ValueError("Each material requirement needs importance, match, and concise requirement/evidence text.")
    blockers = payload.get("hard_blockers")
    if not isinstance(blockers, list) or len(blockers) > 8:
        raise ValueError("Provide an array of confirmed hard blockers, empty when eligible.")
    for blocker in blockers:
        if (not isinstance(blocker, dict)
                or any(not isinstance(blocker.get(k), str) or not blocker[k].strip() or len(blocker[k]) > 4000
                       for k in ("requirement", "job_evidence", "applicant_evidence"))):
            raise ValueError("A hard blocker needs the requirement, exact job evidence, and confirmed applicant evidence.")
    if eligibility == "blocked" and (score != 0 or not blockers):
        raise ValueError("A blocked job must score 0 and cite at least one confirmed hard barrier.")
    if eligibility == "eligible" and blockers:
        raise ValueError("An eligible job cannot contain confirmed hard blockers.")
    return {"scoring_version": SCORING_VERSION, "score": score, "eligibility": eligibility,
            "confidence": confidence, "reason": reason.strip(),
            "requirements": requirements, "hard_blockers": blockers}
