"""Stage the static site with matching browser-cache versions for its assets."""

import argparse
from hashlib import sha256
from pathlib import Path
import re
import shutil


def stage_site(source: Path, destination: Path) -> str:
    source = source.resolve()
    if destination.resolve().is_relative_to(source):
        raise ValueError("The staging directory must be outside public/")
    assets = source / "assets"
    digest = sha256()
    for file in sorted(path for path in assets.rglob("*") if path.is_file()):
        digest.update(file.relative_to(assets).as_posix().encode() + b"\0")
        digest.update(sha256(file.read_bytes()).digest())
    version = digest.hexdigest()[:16]
    shutil.copytree(source, destination)
    index = destination / "index.html"
    html = re.sub(r'((?:src|href)="assets/[^"?#]+)(")',
                  lambda m: f'{m[1]}?v={version}{m[2]}', index.read_text(encoding="utf-8"))
    index.write_text(html, encoding="utf-8")
    for module in (destination / "assets").glob("*.js"):
        code = re.sub(r'''(\bfrom\s*["']\./[\w./-]+\.js)(["'])''',
                      lambda m: f'{m[1]}?v={version}{m[2]}', module.read_text(encoding="utf-8"))
        module.write_text(code, encoding="utf-8")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, default=Path("public"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Staged dashboard assets", stage_site(args.public, args.output))
