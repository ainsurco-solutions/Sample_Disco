from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ENV_KEY = "MIGRATION_HUB_DATABASE_URL"
_SQLITE_PREFIX = "sqlite:///"

_MB = 1024 * 1024
_OK_PUT = "t.method = 'PUT' AND t.status_code BETWEEN 200 AND 299"

_FILES_SQL = """
WITH put AS (
  SELECT t.file_id,
         COUNT(*) AS parts,
         SUM(CAST(json_extract(t.request_body, '$.bytes') AS REAL)) AS bytes,
         SUM(t.duration_ms) / 1000.0 AS put_s
  FROM api_transaction t
  WHERE {ok_put}
  GROUP BY t.file_id),
bad AS (
  SELECT file_id, COUNT(*) AS n FROM api_transaction
  WHERE method = 'PUT' AND (status_code IS NULL OR status_code >= 300)
  GROUP BY file_id),
url AS (
  SELECT file_id, SUM(duration_ms) / 1000.0 AS s FROM api_transaction
  WHERE method = 'GET' AND url LIKE '%/upload-part/%'
  GROUP BY file_id)
SELECT f.batch_id, f.file_id, f.size_bytes, p.parts, p.bytes, p.put_s,
       (julianday(f.uploaded_at) - julianday(f.upload_started_at)) * 86400,
       COALESCE(u.s, 0), COALESCE(b.n, 0),
       (julianday(f.archived_at) - julianday(f.uploaded_at)) * 86400,
       f.source_path
FROM migration_file f
JOIN put p ON p.file_id = f.file_id
LEFT JOIN bad b ON b.file_id = f.file_id
LEFT JOIN url u ON u.file_id = f.file_id
{where}
ORDER BY f.upload_started_at
"""

_PART_SPEEDS_SQL = f"""
SELECT CAST(json_extract(t.request_body, '$.bytes') AS REAL), t.duration_ms
FROM api_transaction t
WHERE t.file_id = ? AND {_OK_PUT} AND t.duration_ms > 0
"""

_HOURLY_SQL = f"""
SELECT strftime('%Y-%m-%d %H:00', t.occurred_at),
       SUM(CAST(json_extract(t.request_body, '$.bytes') AS REAL)),
       COUNT(DISTINCT t.file_id)
FROM api_transaction t
JOIN migration_file f ON f.file_id = t.file_id
WHERE {_OK_PUT} {{batch_filter}}
GROUP BY 1 ORDER BY 1
"""

def registry_from_env(root: Path = _ROOT) -> Path | None:
    env = root / ".env"
    value = os.environ.get(_ENV_KEY)
    if value is None and env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, rest = line.partition("=")
            if sep and key.strip() == _ENV_KEY:
                value = rest.strip().strip('"').strip("'")
    if not value or not value.startswith(_SQLITE_PREFIX):
        return None
    return Path(value[len(_SQLITE_PREFIX) :])

def _placeholders(batches: Sequence[str]) -> str:
    return ",".join("?" * len(batches))

def report(con: sqlite3.Connection, batches: Sequence[str], out: Callable[[str], None]) -> int:
    where = f"WHERE f.batch_id IN ({_placeholders(batches)})" if batches else ""
    rows = con.execute(_FILES_SQL.format(ok_put=_OK_PUT, where=where), list(batches)).fetchall()

    out("== Per file: wire speed against upload time ==")
    out("wire MB/s = bytes / time inside successful PUTs -- the network's own speed")
    out("busy %    = PUT time / upload time -- the rest is URL fetches, retries, reads, gaps")
    out("")
    out(
        f"{'batch':<14}{'file':>6}{'GB':>8}{'parts':>7}{'wireMB/s':>10}{'GB/h':>7}"
        f"{'upload h':>9}{'busy%':>7}{'urlGET s':>9}{'badPUT':>7}{'imp+arch h':>11}  name"
    )
    for batch, fid, size, parts, sent, put_s, wall, url_s, bad, post, path in rows:
        mbs = sent / _MB / put_s if put_s else 0.0
        busy = f"{100 * put_s / wall:.0f}" if wall else "-"
        name = str(path).replace("/", "\\").rsplit("\\", 1)[-1]
        out(
            f"{str(batch)[:13]:<14}{fid:>6}{size / 1024**3:>8.1f}{parts:>7}{mbs:>10.1f}"
            f"{mbs * 3600 / 1024:>7.1f}{(wall or 0) / 3600:>9.2f}{busy:>7}{url_s:>9.0f}"
            f"{bad:>7}{(post or 0) / 3600:>11.2f}  {name}"
        )
    if not rows:
        out("(no successful uploads recorded for this selection)")

    out("")
    out("== Per part: is the speed steady, or does it sag? (multipart files) ==")
    for fid in [r[1] for r in rows if r[3] > 1]:
        speeds = sorted(b / _MB / (ms / 1000.0) for b, ms in con.execute(_PART_SPEEDS_SQL, (fid,)))
        if not speeds:
            continue

        def pick(q: float, s: list[float] = speeds) -> float:
            return s[min(len(s) - 1, int(q * len(s)))]

        out(
            f"file {fid:>6}: {len(speeds)} parts  MB/s min {speeds[0]:.1f}  p10 {pick(0.1):.1f}"
            f"  median {pick(0.5):.1f}  p90 {pick(0.9):.1f}  max {speeds[-1]:.1f}"
        )

    out("")
    out("== Per hour: total sent, and how many files were uploading ==")
    batch_filter = f"AND f.batch_id IN ({_placeholders(batches)})" if batches else ""
    for hour, sent, files in con.execute(
        _HOURLY_SQL.format(batch_filter=batch_filter), list(batches)
    ):
        gb = sent / 1024**3
        out(f"{hour}  {gb:>7.1f} GB  {files} file(s)  {'#' * int(gb)}")
    return len(rows)

def main(argv: Sequence[str] | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(
        description="Upload speed per file and per hour, from the registry's upload log."
    )
    parser.add_argument("batches", nargs="*", help="batch ids to report (default: all)")
    parser.add_argument("--db", type=Path, help=f"registry .db file (default: {_ENV_KEY} in .env)")
    args = parser.parse_args(argv)

    db = args.db or registry_from_env()
    if db is None:
        out(f"No registry given: pass --db, or set {_ENV_KEY}=sqlite:///... in .env")
        return 2
    if not db.is_file():
        out(f"No registry at {db}")
        return 2

    out(f"Registry: {db}")
    out("")
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        report(con, args.batches, out)
    finally:
        con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
