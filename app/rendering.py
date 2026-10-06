import hashlib
import json
from pathlib import Path
import subprocess
from .store import digest

CODE_ROOT = Path(__file__).resolve().parents[1]


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
    subprocess.run([config["node"], config["artifact_marker"], "--operation-kind", "create",
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
