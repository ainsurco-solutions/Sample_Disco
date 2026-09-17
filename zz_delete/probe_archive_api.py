# Generated copy -- do not edit.
# Source: dev-tools/Reconcile/probe_archive_api.py in the AML-MigHub repo.
# Comments and docstrings are stripped; the reasoning is in master.
# Re-run dev-tools/Reconcile/push_to_demo.py to update.
#
# Finds which parameter the archive API is refusing, by sending the
# same request four times and removing one variable each time.
#
#     python probe_archive_api.py
#
# Read-only: issues GETs, writes nothing, changes no state. Reads the
# key from .env and never prints it.

from __future__ import annotations

import json
import sys

from settings import load_settings

SAMPLE = 25

def main() -> int:
    try:
        import requests
    except ImportError:
        print("requests is not installed: pip install requests")
        return 1

    api = load_settings()
    if not api.has_key:
        print("No MOODYS_API_KEY in .env -- nothing to probe with.")
        return 1

    sample = SAMPLE
    if len(sys.argv) > 1:
        try:
            sample = max(1, int(sys.argv[1]))
        except ValueError:
            print(f"Not a number: {sys.argv[1]!r}")
            return 1
    if sample > 1000:
        print(f"Asking for {sample}; the endpoint caps a page at 1000.\n")
        sample = 1000

    url = (api.host or "") + (api.vault_path or "")
    headers = {"accept": "application/json", "Authorization": api.api_key}

    print(f"GET {url}\n")
    try:
        response = requests.get(
            url, headers=headers, params={"limit": sample}, timeout=60
        )
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 1

    if response.status_code >= 400:
        body = " ".join(response.text.split())[:300]
        if api.api_key and api.api_key in body:
            body = body.replace(api.api_key, "<key>")
        print(f"{response.status_code}: {body}")
        return 1

    rows = response.json()
    if not isinstance(rows, list) or not rows:
        print("No archives returned -- nothing to inspect.")
        return 0

    print(f"{len(rows)} rows.\n")

    print("--- first row, in full " + "-" * 45)
    print(json.dumps(rows[0], indent=2, default=str)[:2000])

    print("\n--- candidate name fields " + "-" * 42)
    columns: dict[str, list[str]] = {}
    for row in rows:
        for key, value in _flatten(row).items():
            if isinstance(value, str) and value.strip():
                columns.setdefault(key, []).append(value.strip())

    for key in sorted(columns):
        values = columns[key]
        distinct = sorted(set(values))
        marker = "  <-- distinct per row" if len(distinct) == len(rows) else ""
        shown = ", ".join(repr(v[:40]) for v in distinct[:3])
        print(f"  {key:<34} {len(distinct):>3} distinct  {shown}{marker}")

    print(
        "\nThe field whose values look like your on-prem database names is the\n"
        "one to set as RECONCILE_VAULT_NAME_FIELD. Nested fields work either\n"
        "bare or dotted."
    )

    _report_name_collisions(rows)
    return 0

def _report_name_collisions(rows: list[dict]) -> None:
    from collections import Counter, defaultdict

    subtypes: dict[str, set[str]] = defaultdict(set)
    counts: Counter[str] = Counter()
    for row in rows:
        name = str(row.get("archiveName") or "").strip().casefold()
        if not name:
            continue
        counts[name] += 1
        subtypes[name].add(str(row.get("archiveSubType") or "?"))

    repeated = {n: c for n, c in counts.items() if c > 1}
    print("\n--- name collisions " + "-" * 48)
    if not repeated:
        print(f"  None in this sample: {len(counts)} archives, all names unique.")
    else:
        print(f"  {len(repeated)} name(s) appear more than once:")
        for name, count in sorted(repeated.items()):
            kinds = ", ".join(sorted(subtypes[name]))
            print(f"    {name!r}  x{count}  subTypes: {kinds}")
        print(
            "\n  These match by name alone, so each would be reported\n"
            "  Duplicate rather than matched. If the pairs are an EDM and an\n"
            "  RDM of the same database, the key needs the subtype in it."
        )

    print(
        "\n  Sample only -- run against the full estate to be sure, since a\n"
        "  collision may not appear in the first "
        f"{len(rows)} rows."
    )

def _flatten(item: object, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    if not isinstance(item, dict):
        return flat
    for key, value in item.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat

if __name__ == "__main__":
    sys.exit(main())
