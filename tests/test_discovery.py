import json

import httpx
import pytest

from app.discovery import normalize, scan_company
from app.store import Conflict, Store
from app.workers import work_once, work_batch


def configured(tmp_path, adapter="lever"):
    store = Store(tmp_path)
    store.company("example", "Fictional example", "https://example.test/careers", True)
    store.source("example", adapter, "fictional-board")
    return store


def post(post_id="id1", **changes):
    return {"id": post_id, "text": "C++ Engineer", "hostedUrl": f"https://jobs.lever.co/fictional/{post_id}",
            "descriptionPlain": "Full-time C++ engineering. No applicant data in this fixture.", "country": "US",
            "categories": {"location": "Austin, Texas", "commitment": "Full-time"},
            "lists": [{"text": "Requirements", "content": "<ul><li>C++</li></ul>"}],
            "workplaceType": "hybrid", **changes}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lever_pagination_repeat_scan_and_raw_evidence(tmp_path):
    store = configured(tmp_path)
    requests = []
    def handler(request):
        assert request.method == "GET" and request.url.host == "api.lever.co"
        assert not request.content and "authorization" not in request.headers
        skip = int(request.url.params["skip"])
        requests.append(skip)
        return httpx.Response(200, json=[post(f"id{skip}")] if skip < 2 else [])
    with client(handler) as feed:
        first = scan_company(store, "example", client=feed, page_size=1)
        second = scan_company(store, "example", client=feed, page_size=1)
    assert first["status"] == second["status"] == "complete"
    assert requests == [0, 1, 2, 0, 1, 2]
    assert first["new"] == second["unchanged"] == 2 and second["new"] == 0
    assert len(store.listing("jobs")) == 2
    assert (tmp_path / first["artifact_path"] / "page-1.json").exists()
    assert all(job["phase"] == "new" for job in store.listing("jobs"))


def test_scan_cap_preserves_incomplete_coverage(tmp_path):
    store = configured(tmp_path)
    with client(lambda req: httpx.Response(200, json=[post()])) as feed:
        result = scan_company(store, "example", client=feed, page_size=1, max_pages=1)
    assert result["status"] == "incomplete" and result["new"] == 1
    assert result["automatic_closures"] == 0


def test_repeated_pagination_never_claims_complete_or_closes_prior_job(tmp_path):
    store = configured(tmp_path)
    original = store.discover("example", normalize("lever", post("prior"), "fixture"))
    with client(lambda req: httpx.Response(200, json=[post()])) as feed:
        result = scan_company(store, "example", client=feed, page_size=1)
    assert result["status"] == "failed" and "Repeated" in result["detail"]
    assert store.get_job(original["id"])["phase"] == "new"


@pytest.mark.parametrize("payload", [
    {"jobs": [], "meta": {"total": 1}},
    {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://example.test/1", "content": ""}], "meta": {"total": 1}},
])
def test_greenhouse_incomplete_description_or_wrong_total_is_rejected(tmp_path, payload):
    store = configured(tmp_path, "greenhouse")
    with client(lambda req: httpx.Response(200, json=payload)) as feed:
        result = scan_company(store, "example", client=feed)
    assert result["status"] == "failed" and not store.listing("jobs")


def test_greenhouse_unknown_facts_are_not_invented(tmp_path):
    store = configured(tmp_path, "greenhouse")
    data = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://example.test/1",
                      "location": {"name": "London"}, "content": "&lt;p&gt;Build C++ software.&lt;/p&gt;"}], "meta": {"total": 1}}
    with client(lambda req: httpx.Response(200, json=data)) as feed:
        result = scan_company(store, "example", client=feed)
    job = store.listing("jobs")[0]
    assert result["status"] == "complete" and job["country"] == job["employment"] == job["arrangement"] == "unknown"
    assert store.get_job(job["id"])["description"]["jd"] == "Build C++ software."


def test_disabled_company_and_overlapping_scan_are_blocked(tmp_path):
    store = configured(tmp_path)
    store.company("example", "Fictional example", "https://example.test/careers", False)
    with pytest.raises(Conflict, match="Enable"):
        scan_company(store, "example")
    store.company("example", "Fictional example", "https://example.test/careers", True)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO scans VALUES('owned','example','running','{}','now',NULL,9999999999)")
    with pytest.raises(Conflict, match="in progress"):
        scan_company(store, "example")


def test_explicit_non_us_listing_is_discarded_without_model_call(tmp_path):
    store = configured(tmp_path)
    store.discover("example", normalize("lever", post(country="IT"), "fixture"))
    result = work_once(store, "evaluation", codex_bin="DO-NOT-LAUNCH")
    assert result["status"] == "completed" and result["result"]["decision"] == "discard"
    assert result["run"]["model_used"] is False


def test_offline_batch_stops_at_limit_and_never_claims_submitted(tmp_path):
    store = configured(tmp_path)
    for n in range(3):
        store.discover("example", normalize("lever", post(f"id{n}", country="IT"), "fixture"))
    result = work_batch(store, max_tasks=2)
    assert result["tasks_completed"] == 2 and "limit" in result["stop_reason"]
    assert sum(job["phase"] == "discarded" for job in store.listing("jobs")) == 2
    assert work_batch(store)["tasks_completed"] == 1
    assert work_batch(store)["tasks_completed"] == 0
