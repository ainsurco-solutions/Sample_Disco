from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

def _registry_path() -> Path:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    url = os.environ.get("MIGRATION_HUB_DATABASE_URL")
    if not url:
        raise SystemExit(
            "MIGRATION_HUB_DATABASE_URL is not set, so there is no registry to back "
            "up.\nRun this from the tree root, where .env is."
        )
    if not url.startswith("sqlite"):
        raise SystemExit(
            f"The registry is not SQLite ({url.split('://')[0]}://...), so this script "
            "does not apply.\nUse that server's own backup instead."
        )

    raw = urlparse(url).path.lstrip("/")
    return Path(unquote(raw))

def main() -> int:
    source = _registry_path()
    if not source.is_file():
        raise SystemExit(f"No registry at {source}\nNothing to back up.")

    dest_dir = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else source.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = dest_dir / f"{source.stem}-{stamp}.db"
    if target.exists():
        raise SystemExit(f"{target} already exists. Not overwriting it.")

    print(f"  source : {source}")
    print(f"  target : {target}")
    print()

    connection = sqlite3.connect(str(source))
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()

    size = target.stat().st_size
    print(f"  [ok] wrote {size:,} bytes")
    print()
    print("  A snapshot beside the original is not yet a backup -- if that folder")
    print("  is the problem, both copies are gone. Copy it somewhere other than")
    print("  the machine that wrote it, or pass a destination folder as the first")
    print("  argument.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
