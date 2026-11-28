"""a vendored tree claims it is the upstream's files, unchanged. this checks the claim.

`vendor/<name>/UPSTREAM_SHA256SUMS` lists one line per file: `<sha256>  <path>` or, for a symlink,
`link  <path> -> <target>`. a file that was edited, deleted or added without being listed fails here,
so "as is" stays a property the build checks and not a sentence in a provenance file. a package with
no manifest is skipped: the small clients carry their change in prose, because they have one.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "UPSTREAM_SHA256SUMS"
# written by us beside the upstream files, so they are never in the manifest they describe
OURS = {MANIFEST, "PROVENANCE.md"}


def parse(text: str) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    links: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("link  "):
            path, _, target = line[len("link  "):].partition(" -> ")
            if not path or not target:
                raise ValueError(f"{MANIFEST} line {number} is a malformed link entry")
            links[path] = target
            continue
        digest, _, path = line.partition("  ")
        if len(digest) != 64 or not path:
            raise ValueError(f"{MANIFEST} line {number} is not '<sha256>  <path>'")
        files[path] = digest
    return files, links


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(package: Path) -> list[str]:
    """every way this tree stops being the upstream's, one sentence each. empty means unchanged."""
    problems: list[str] = []
    manifest = package / MANIFEST
    try:
        files, links = parse(manifest.read_text())
    except (OSError, ValueError) as error:
        return [f"{package.name}: {error}"]
    seen: set[str] = set()
    for dirpath, _dirs, names in os.walk(package):
        for name in names:
            full = Path(dirpath) / name
            rel = full.relative_to(package).as_posix()
            if rel in OURS:
                continue
            seen.add(rel)
            if full.is_symlink():
                target = os.readlink(full)
                if rel not in links:
                    problems.append(f"{package.name}: {rel} is a symlink the manifest does not list")
                elif links[rel] != target:
                    problems.append(f"{package.name}: {rel} points at {target}, upstream points at {links[rel]}")
            elif rel not in files:
                problems.append(f"{package.name}: {rel} is not an upstream file")
            elif sha256(full) != files[rel]:
                problems.append(f"{package.name}: {rel} differs from upstream")
    for rel in sorted((set(files) | set(links)) - seen):
        problems.append(f"{package.name}: {rel} is listed but missing")
    return problems


def main() -> int:
    checked, problems = 0, []
    for package in sorted((ROOT / "vendor").iterdir()):
        if package.is_dir() and (package / MANIFEST).exists():
            checked += 1
            problems += verify(package)
    if problems:
        print("\n".join(problems[:40]))
        if len(problems) > 40:
            print(f"... and {len(problems) - 40} more")
        return 1
    print(f"vendor verify ok ({checked} tree{'s' if checked != 1 else ''} match upstream)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
