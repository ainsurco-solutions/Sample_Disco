from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
VERSION_NAME = "VERSION.txt"

_OK = "  [ok]"
_NO = "  [!!]"
_WARN = "  [??]"

class Entry:

    __slots__ = ("digest", "kind", "lines", "path", "size")

    def __init__(self, kind: str, size: str, lines: str, digest: str, path: str) -> None:
        self.kind = kind
        self.size = int(size)
        self.lines = int(lines)
        self.digest = digest
        self.path = path

def _find_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()

    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent, Path.cwd()):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    return here.parent

def _read_manifest(path: Path) -> tuple[list[Entry], str]:
    entries: list[Entry] = []
    revision = "unknown"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# source commit:"):
            revision = line.split(":", 1)[1].strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        entries.append(Entry(*parts))
    return entries, revision

def _digest(path: Path) -> tuple[str, int, int]:
    raw = path.read_bytes()
    normalised = raw.replace(b"\r\n", b"\n")
    text = normalised.decode("utf-8", errors="replace")
    canonical = normalised if (not normalised or normalised.endswith(b"\n")) else normalised + b"\n"
    return hashlib.sha256(canonical).hexdigest(), len(normalised), len(text.splitlines())

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check this copy of AMIGO against the manifest it shipped with."
    )
    parser.add_argument("--root", help="the tree to check (default: found automatically)")
    parser.add_argument(
        "--all", action="store_true", help="include config files, which are skipped by default"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="only report problems, not files that match"
    )
    args = parser.parse_args()

    root = _find_root(args.root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"{_NO} No {MANIFEST_NAME} found at {root}")
        print(f"{_NO} Give the tree explicitly:  python check_copy.py --root C:\\path\\to\\AMIGO")
        return 2

    entries, revision = _read_manifest(manifest_path)
    print(f"Checking {root}")
    print(f"  against {MANIFEST_NAME} from commit {revision[:12]}")

    version_file = root / VERSION_NAME
    if version_file.is_file():
        stamp = version_file.read_text(encoding="utf-8")
        if "DIRTY" in stamp:
            print(f"{_WARN} published from a dirty tree -- it matches no commit")
    print()

    manifest_mtime = datetime.fromtimestamp(manifest_path.stat().st_mtime)

    missing: list[str] = []
    differs: list[tuple[Entry, int, int]] = []
    edited: list[str] = []
    empty: list[str] = []
    checked = skipped = 0

    for entry in sorted(entries, key=lambda e: e.path):
        if entry.kind == "config" and not args.all:
            skipped += 1
            continue

        local = root / entry.path
        if not local.is_file():
            missing.append(entry.path)
            continue

        checked += 1
        digest, size, lines = _digest(local)

        if size == 0 and entry.size > 0:
            empty.append(entry.path)
            continue

        if digest == entry.digest:
            if not args.quiet:
                print(f"{_OK} {entry.path}")
            continue

        differs.append((entry, size, lines))
        if datetime.fromtimestamp(local.stat().st_mtime) > manifest_mtime:
            edited.append(entry.path)

    print()
    if empty:
        print(f"{_NO} {len(empty)} file(s) are EMPTY -- created but never pasted:")
        for path in empty:
            print(f"       {path}")
        print()

    if missing:
        print(f"{_NO} {len(missing)} file(s) MISSING:")
        for path in missing:
            print(f"       {path}")
        print()

    if differs:
        print(f"{_NO} {len(differs)} file(s) DIFFER from the published copy:")
        print(f"       {'size':>9}  {'lines':>7}   path")
        for entry, size, lines in differs:
            note = "  <- edited here" if entry.path in edited else ""
            print(
                f"       {size:>9}  {lines:>7}   {entry.path}{note}\n"
                f"       {entry.size:>9}  {entry.lines:>7}   (published)"
            )
        print()
        if edited:
            print(
                f"{_WARN} {len(edited)} of those are NEWER than the manifest, so they were\n"
                "       edited on this machine. Fixes made here are lost on the next\n"
                "       publish, and made blind -- the explanatory comments are stripped.\n"
                "       Move the change into the AML-MigHub repo instead."
            )
            print()

    summary = f"{checked} checked"
    if skipped:
        summary += f", {skipped} config skipped (--all to include)"
    print(summary)

    problems = len(missing) + len(differs) + len(empty)
    if problems:
        print(f"{_NO} {problems} problem(s). This copy is not what was published.")
        return 1

    print(f"{_OK} This copy matches the manifest exactly.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
