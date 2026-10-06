"""Read-only adapters for documented public job-board feeds.

No applicant data is sent. Unknown listing facts stay unknown. Raw pages and scan
coverage are retained; failed/partial scans never close existing applications.
"""
from html import unescape
from html.parser import HTMLParser
import json
import time
import uuid

import httpx

from .store import Conflict, digest, timestamp


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "div", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    parser = Text()
    # Greenhouse encodes hosted-editor HTML. Decode before parsing tags.
    parser.feed(unescape(value or ""))
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def normalize(adapter, post, source):
    if adapter == "greenhouse":
        location = (post.get("location") or {}).get("name") or "Not specified"
        # Location names can be ambiguous (e.g. London, Ontario). Do not guess.
        country = "US" if any(term in location.casefold() for term in ("united states", "usa", "u.s.a.")) else "unknown"
        return {"requisition_id": str(post["id"]), "url": post["absolute_url"], "title": post["title"],
                "jd": plain(post.get("content")), "location": location, "country": country,
                "employment": "unknown", "arrangement": "unknown", "source": source}
    categories = post.get("categories") or {}
    country_code = post.get("country")
    country = "US" if country_code == "US" else "other" if country_code else "unknown"
    commitment = (categories.get("commitment") or "").casefold().replace("-", "").replace(" ", "")
    employment = {"fulltime": "full_time", "parttime": "part_time", "intern": "internship",
                  "internship": "internship", "temporary": "temporary", "contract": "temporary"}.get(commitment, "unknown")
    arrangement = {"on-site": "onsite", "remote": "remote", "hybrid": "hybrid"}.get(post.get("workplaceType"), "unknown")
    parts = [post.get("descriptionPlain") or plain(post.get("description"))]
    for section in post.get("lists") or []:
        parts += [section.get("text", ""), plain(section.get("content"))]
    parts += [post.get("additionalPlain") or plain(post.get("additional")),
              post.get("salaryDescriptionPlain") or plain(post.get("salaryDescription"))]
    return {"requisition_id": str(post["id"]), "url": post["hostedUrl"], "title": post["text"],
            "jd": "\n\n".join(part for part in parts if part),
            "location": "; ".join(categories.get("allLocations") or [categories.get("location") or "Not specified"]),
            "country": country, "employment": employment, "arrangement": arrangement, "source": source}


def scan_company(store, company_id, *, client=None, page_size=100, max_pages=50):
    scan_id, start = uuid.uuid4().hex, timestamp()
    with store.connect(write=True) as db:
        company = db.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        source = db.execute("SELECT * FROM company_sources WHERE company_id=?", (company_id,)).fetchone()
        if not company or not company["enabled"] or not source:
            raise Conflict("Enable the company and configure its verified feed source before scanning.")
        if store.paused():
            raise Conflict("Queue is paused.")
        db.execute("UPDATE scans SET status='incomplete',finished_at=? WHERE status='running' AND lease_until<?", (start, time.time()))
        if db.execute("SELECT 1 FROM scans WHERE company_id=? AND status='running'", (company_id,)).fetchone():
            raise Conflict("This company already has a scan in progress.")
        db.execute("INSERT INTO scans VALUES(?,?,?,'{}',?,NULL,?)", (scan_id, company_id, "running", start, time.time() + 300))
        source = dict(source)
    folder = store.root / "artifacts" / "scans" / scan_id
    folder.mkdir(parents=True)
    adapter, token = source["adapter"], source["board_token"]
    base = (f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs" if adapter == "greenhouse" else
            f"https://{'api.eu.lever.co' if adapter == 'lever_eu' else 'api.lever.co'}/v0/postings/{token}")
    owned_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False, headers={"Accept": "application/json"})
    counts = {"new": 0, "changed": 0, "unchanged": 0, "observed_posts": 0, "pages": 0}
    status, detail, seen = "incomplete", "Page cap reached; coverage is incomplete.", set()
    records, deadline = [], time.monotonic() + 120
    try:
        for page in range(1 if adapter == "greenhouse" else max_pages):
            if time.monotonic() > deadline or store.paused():
                detail = "Scan paused or time limit reached; coverage is incomplete."
                break
            params = {"content": "true"} if adapter == "greenhouse" else {"mode": "json", "skip": page * page_size, "limit": page_size}
            with client.stream("GET", base, params=params) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 20_000_000:
                        raise ValueError("Feed response exceeds the supported size; coverage is incomplete.")
            payload = json.loads(raw)
            posts = payload.get("jobs") if adapter == "greenhouse" and isinstance(payload, dict) else payload
            if not isinstance(posts, list):
                raise ValueError("The feed did not return a job-post list.")
            if adapter == "greenhouse" and payload.get("meta", {}).get("total") != len(posts):
                raise ValueError("Greenhouse total does not match the returned posts; coverage is incomplete.")
            path = folder / f"page-{page + 1}.json"
            path.write_bytes(raw)
            counts["pages"] += 1
            for post in posts:
                post_id = str(post["id"])
                if post_id in seen:
                    raise ValueError("Repeated post ID across feed pages; pagination coverage is unverified.")
                seen.add(post_id)
                record = normalize(adapter, post, base)
                if not record["jd"].strip():
                    raise ValueError(f"Post {post_id} has no full description; scan remains incomplete.")
                records.append(record)
            counts["observed_posts"] = len(seen)
            if adapter == "greenhouse" or len(posts) < page_size:
                status, detail = "complete", "All published posts returned by this feed were captured."
                break
        # Commit only after validating every captured record. A partial scan can still
        # add observed posts, but never implies that missing jobs have closed.
        for record in records:
            result = store.discover(company_id, record)
            counts["new" if result["created"] else "changed" if result["changed"] else "unchanged"] += 1
    except Exception as error:
        status, detail = "failed", str(error)
    finally:
        if owned_client:
            client.close()
    report = {"scan_id": scan_id, "company_id": company_id, "adapter": adapter, "feed_url": base,
              "status": status, "detail": detail, **counts,
              "artifact_path": str(folder.relative_to(store.root)), "automatic_closures": 0}
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with store.connect(write=True) as db:
        db.execute("UPDATE scans SET status=?,payload=?,finished_at=? WHERE id=?", (status, json.dumps(report), timestamp(), scan_id))
        db.execute("UPDATE companies SET last_scan_at=?,scan_note=? WHERE id=?", (timestamp(), detail, company_id))
        store.event(db, "discovery.scan_finished", report)
    return report
