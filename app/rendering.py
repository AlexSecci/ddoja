import hashlib
import json
import os
from pathlib import Path
from functools import lru_cache
import re
import shutil
import subprocess
from typing import Any
from .store import digest

CODE_ROOT = Path(__file__).resolve().parents[1]
MARKER_NAME = "mark_artifact_operation_started.mjs"


def _version_key(path):
    parts = re.findall(r"\d+", path.name)
    return tuple(int(part) for part in parts) if parts else (0,)


def resolve_artifact_marker(configured):
    """Use the configured PDF marker, falling back across installed skill versions."""
    configured_path = Path(configured).expanduser() if configured else None
    if configured_path and configured_path.is_file():
        return configured_path

    roots = []
    if configured_path:
        parts = configured_path.parts
        lowered = [part.casefold() for part in parts]
        for index in range(len(lowered) - 4):
            if lowered[index:index + 5] == [".codex", "plugins", "cache", "openai-primary-runtime", "pdf"]:
                roots.append(Path(*parts[:index + 5]))
                break

    roots.append(Path.home() / ".codex" / "plugins" / "cache" / "openai-primary-runtime" / "pdf")
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.append(Path(codex_home).expanduser() / "plugins" / "cache" / "openai-primary-runtime" / "pdf")

    seen = set()
    candidates = []
    for root in roots:
        try:
            canonical_root = root.resolve()
        except OSError:
            continue
        if canonical_root in seen:
            continue
        seen.add(canonical_root)
        if not canonical_root.is_dir():
            continue
        try:
            versions = [path for path in canonical_root.iterdir() if path.is_dir()]
        except OSError:
            continue
        for version in sorted(versions, key=_version_key, reverse=True):
            candidate = version / "skills" / "pdf" / "container_tools" / MARKER_NAME
            if candidate.is_file():
                candidates.append(candidate)
    if candidates:
        return candidates[0]

    configured_text = str(configured_path) if configured_path else "(not configured)"
    raise ValueError(
        "PDF authoring marker is missing. Configure `artifact_marker` in config/runtime.json "
        f"or install the PDF skill marker under the Codex plugin cache. Configured path: {configured_text}"
    )


def render_package(store, manifest):
    config = json.loads((CODE_ROOT / "config" / "runtime.json").read_text(encoding="utf-8-sig"))
    sources = []
    package_root = (store.root / "artifacts" / "packages" / manifest["package_id"]).resolve()
    for item in manifest["files"]:
        file = (store.root / item["path"]).resolve()
        if not file.is_relative_to(package_root) or file.suffix != ".md":
            raise ValueError("Material path is outside its package or has an unsupported format.")
        if digest(file.read_text(encoding="utf-8")) != item["sha256"]:
            raise ValueError("Draft file changed after it was saved. Regenerate a versioned package before review.")
        sources.append(file)
    if {source.name for source in sources} != {"resume.md", "cover-letter.md"}:
        raise ValueError("A package requires the resume and cover letter drafts.")
    # Mark exactly once, immediately before this package's first PDF authoring command.
    marker = resolve_artifact_marker(config.get("artifact_marker"))
    subprocess.run([config["node"], str(marker), "--operation-kind", "create",
                    "--expected-output-count", "2", "--output-format", "pdf"], check=True, timeout=20)
    rendered = []
    for source in sources:
        command = [config["document_python"], str(CODE_ROOT / "tools" / "render_pdf.py"), str(source), str(source.with_suffix(".pdf")),
                   "--pdftoppm", config["pdftoppm"], "--max-pages", "2" if source.name == "resume.md" else "1",
                   "--font-regular", config["font_regular"], "--font-bold", config["font_bold"]]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if result.returncode:
            raise ValueError("PDF rendering failed: " + result.stderr[-1800:])
        item = json.loads(result.stdout)
        item["sha256"] = hashlib.sha256(Path(item["pdf"]).read_bytes()).hexdigest()
        item["preview_hashes"] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in item["previews"]}
        rendered.append(item)
    return rendered


def render_agent_documents(store, published, *, worker_id: str | None = None):
    """Render only artifacts explicitly requested and published by a document child.

    This path intentionally does not reuse the legacy review helper's fixed
    resume-plus-cover-letter requirement. The actual form may support one kind,
    both kinds, or a cover-letter text field; the agent decides that from its
    observed form and Store validates the request scope.
    """
    if not isinstance(published, dict):
        raise ValueError("The document publisher returned an invalid result.")
    package_id = published.get("package_id")
    files = published.get("artifacts")
    if not isinstance(package_id, str) or not package_id or not isinstance(files, list) or not files:
        raise ValueError("The document publisher did not return a package ID and files.")
    store_root = Path(store.root).resolve()
    agent_root = (store_root / "artifacts" / "agent").resolve()
    package_root = (agent_root / package_id).resolve()
    if (not agent_root.is_relative_to(store_root)
            or package_root.parent != agent_root or not package_root.is_dir()):
        raise ValueError("The published package is outside the agent artifact workspace.")
    allowed = {"resume": ("resume.md", "resume.pdf", 2),
               "cover_letter": ("cover-letter.md", "cover-letter.pdf", 1)}
    entries = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("The published document manifest is malformed.")
        kind, filename, relative, expected_hash = (item.get("kind"), item.get("filename"),
                                                   item.get("path"), item.get("sha256"))
        if kind not in allowed or filename != allowed[kind][0] or kind in seen:
            raise ValueError("The published document kind or filename is unsupported.")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise ValueError("The published document path or digest is missing.")
        file = (store.root / relative).resolve()
        if not file.is_relative_to(package_root) or file.name != filename or file.suffix != ".md":
            raise ValueError("Published document path escaped its package or has an unsupported format.")
        if not file.is_file() or digest(file.read_text(encoding="utf-8")) != expected_hash:
            raise ValueError("A published document changed before rendering.")
        seen.add(kind)
        entries.append((kind, item, file, allowed[kind][2]))
    if len(entries) != len(files):
        raise ValueError("The published document list is inconsistent.")

    config = json.loads((CODE_ROOT / "config" / "runtime.json").read_text(encoding="utf-8-sig"))
    from .efficiency_features import features
    config['fit_pages']=features(store.root)['document_edits']
    renderer_sha256 = agent_renderer_fingerprint(config)
    # Render bytes are immutable per source set + renderer. Later content edits
    # must not overwrite a previous version's PDF/previews or poison its cache.
    render_key=hashlib.sha256(json.dumps({'renderer':renderer_sha256,'sources':sorted((kind,item['sha256']) for kind,item,_,_ in entries)},sort_keys=True).encode()).hexdigest()
    rendered_root = (package_root / 'renders' / render_key / "rendered").resolve()
    if not rendered_root.is_relative_to(package_root):
        raise ValueError("Rendered document output escaped its package.")
    rendered_root.mkdir(parents=True, exist_ok=True)

    cache_hits: dict[str, dict] = {}
    worker_id = worker_id or published.get("worker_id")
    lookup = getattr(store, "lookup_agent_render_cache", None)
    if callable(lookup) and isinstance(worker_id, str) and worker_id:
        cached = lookup(worker_id, [item[1]["id"] for item in entries], renderer_sha256)
        for hit in cached.get("hits", []) if isinstance(cached, dict) else []:
            if isinstance(hit, dict) and isinstance(hit.get("artifact_id"), str):
                cache_hits[hit["artifact_id"]] = hit

    rendered = []
    seen_preview_names: set[str] = set()
    misses = []
    for kind, source_item, source, max_pages in entries:
        hit = cache_hits.get(source_item["id"])
        if hit is None or hit.get("source_sha256") != source_item["sha256"] or hit.get("renderer_sha256") != renderer_sha256:
            misses.append((kind, source_item, source, max_pages))
            continue
        pdf_info = hit.get("pdf")
        preview_info = hit.get("previews")
        if not isinstance(pdf_info, dict) or not isinstance(preview_info, list) or not preview_info:
            misses.append((kind, source_item, source, max_pages))
            continue
        try:
            source_pdf = _verified_cached_path(store_root, pdf_info.get("path"), pdf_info.get("sha256"),
                                               pdf_info.get("filename"), suffix=".pdf")
            pdf_filename = allowed[kind][1]
            target_pdf = (rendered_root / pdf_filename).resolve()
            _copy_verified_cache_file(source_pdf, target_pdf, rendered_root)
            copied_previews = []
            preview_hashes = {}
            for preview in preview_info:
                if not isinstance(preview, dict):
                    raise ValueError("Cached preview metadata is malformed.")
                source_preview = _verified_cached_path(store_root, preview.get("path"), preview.get("sha256"),
                                                       preview.get("filename"), suffix=".png")
                output_name = preview["filename"]
                if output_name.casefold() in seen_preview_names:
                    output_name = f"{kind}-{output_name}"
                seen_preview_names.add(output_name.casefold())
                target_preview = (rendered_root / output_name).resolve()
                _copy_verified_cache_file(source_preview, target_preview, rendered_root)
                copied_previews.append(str(target_preview))
                preview_hashes[str(target_preview)] = hashlib.sha256(target_preview.read_bytes()).hexdigest()
            pdf_hash = hashlib.sha256(target_pdf.read_bytes()).hexdigest()
            rendered.append({"kind": kind, "artifact_id": source_item["id"], "source_sha256": source_item["sha256"],
                             "filename": pdf_filename, "pdf": str(target_pdf),
                             "pdf_path": target_pdf.relative_to(store_root).as_posix(), "sha256": pdf_hash,
                             "previews": copied_previews, "preview_hashes": preview_hashes,
                             "renderer_sha256": renderer_sha256, "pages": len(copied_previews),
                             "cache_hit": True})
        except (OSError, ValueError, KeyError, TypeError):
            # A stale or malformed cache entry is a miss. It is never uploaded;
            # render the current source and register its newly checked hashes.
            misses.append((kind, source_item, source, max_pages))

    if misses:
        marker = resolve_artifact_marker(config.get("artifact_marker"))
        subprocess.run([config["node"], str(marker), "--operation-kind", "create",
                        "--expected-output-count", str(len(misses)), "--output-format", "pdf"],
                       check=True, timeout=20)
    for kind, source_item, source, max_pages in misses:
        pdf_filename = allowed[kind][1]
        target = (rendered_root / pdf_filename).resolve()
        if target.parent != rendered_root:
            raise ValueError("Rendered PDF target escaped its package directory.")
        command = [config["document_python"], str(CODE_ROOT / "tools" / "render_pdf.py"),
                   str(source), str(target), "--pdftoppm", config["pdftoppm"],
                   "--max-pages", str(max_pages), "--font-regular", config["font_regular"],
                   "--font-bold", config["font_bold"]]
        from .efficiency_features import features
        if features(store.root)['document_edits']:command.append('--fit-pages')
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=90)
        if result.returncode:
            raise ValueError("PDF rendering failed: " + result.stderr[-1800:])
        item = json.loads(result.stdout)
        item["kind"] = kind
        item["artifact_id"] = source_item["id"]
        item["source_sha256"] = source_item["sha256"]
        item["filename"] = pdf_filename
        pdf_path = Path(item["pdf"]).resolve()
        if pdf_path != target or not pdf_path.is_file() or not pdf_path.is_relative_to(rendered_root):
            raise ValueError("The PDF renderer returned an unexpected output path.")
        item["pdf_path"] = pdf_path.relative_to(store.root.resolve()).as_posix()
        item["sha256"] = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        item["renderer_sha256"] = renderer_sha256
        item["cache_hit"] = False
        item["preview_hashes"] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                  for path in item["previews"]}
        for preview in item["previews"]:
            preview_path = Path(preview).resolve()
            if not preview_path.is_file() or not preview_path.is_relative_to(rendered_root):
                raise ValueError("A rendered preview is missing or outside its package directory.")
        rendered.append(item)
    return sorted(rendered, key=lambda item: next(index for index, entry in enumerate(entries)
                                                   if entry[1]["id"] == item["artifact_id"]))


@lru_cache(maxsize=128)
def _hash_file_cached(path_text: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    digest_value = hashlib.sha256()
    with open(path_text, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_value.update(chunk)
    return digest_value.hexdigest()


def _file_sha256(path_value: str | Path) -> str:
    path = Path(path_value).expanduser().resolve(strict=True)
    stat = path.stat()
    if not path.is_file():
        raise ValueError(f"Renderer dependency is not a file: {path.name}")
    return _hash_file_cached(str(path), stat.st_size, stat.st_mtime_ns)


def agent_renderer_fingerprint(config: dict | None = None) -> str:
    """Hash the actual renderer, marker, fonts, binaries, and style configuration."""
    if config is None:
        config = json.loads((CODE_ROOT / "config" / "runtime.json").read_text(encoding="utf-8-sig"))
    marker = resolve_artifact_marker(config.get("artifact_marker"))
    dependencies = {
        "renderer_script": CODE_ROOT / "tools" / "render_pdf.py",
        "artifact_marker": marker,
        "node": config["node"],
        "document_python": config["document_python"],
        "pdftoppm": config["pdftoppm"],
        "font_regular": config["font_regular"],
        "font_bold": config["font_bold"],
    }
    payload = {
        "fingerprint_version": 1,
        "dependencies": {key: {"path": str(Path(path).expanduser().resolve()), "sha256": _file_sha256(path)}
                          for key, path in sorted(dependencies.items())},
        "style": {key: config.get(key) for key in ("font_regular", "font_bold")},
        "formats": {"resume_max_pages": 2, "cover_letter_max_pages": 1, "format": "pdf", "fit_pages":config.get('fit_pages',False)},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verified_cached_path(store_root: Path, relative: Any, expected_sha: Any,
                          filename: Any, *, suffix: str) -> Path:
    if (not isinstance(relative, str) or "\\" in relative or not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
            or not isinstance(filename, str) or Path(filename).name != filename
            or Path(filename).suffix.casefold() != suffix):
        raise ValueError("Cached render metadata is invalid.")
    path = (store_root / relative).resolve(strict=True)
    agent_root = (store_root / "artifacts" / "agent").resolve()
    if not path.is_relative_to(agent_root) or not path.is_file() or path.name != filename:
        raise ValueError("Cached render path escaped the agent artifact area.")
    if _file_sha256(path) != expected_sha:
        raise ValueError("Cached render bytes no longer match their saved hash.")
    return path


def _copy_verified_cache_file(source: Path, destination: Path, rendered_root: Path) -> None:
    if destination.parent != rendered_root.resolve():
        raise ValueError("Cached render destination escaped the current package.")
    temporary = destination.with_name(destination.name + ".cachetmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
