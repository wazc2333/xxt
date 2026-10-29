"""Build a source deployment archive from an explicit credential-free allowlist."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    files = [ROOT / name for name in ("README.md", "requirements.txt", "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore")]
    files.extend(p for p in (ROOT / "backend").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".js", ".css", ".html", ".md", ".png"))
    files.extend((ROOT / "playback_simulator").glob("*.py"))
    files.append(ROOT / 'backend/resources/cxsecret-glyphs.json')
    files.extend(p for p in (ROOT / "deploy").iterdir() if p.is_file() and p.suffix in (".py", ".ps1", ".sh", ".service", ".conf"))
    target = ROOT / "dist" / "xxt-backend.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    print(json.dumps({"archive": str(target), "files": len(set(files)), "bytes": target.stat().st_size,
                      "sha256": digest}))


if __name__ == "__main__":
    main()
