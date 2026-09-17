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

import sys

from settings import load_settings

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

    url = (api.host or "") + (api.vault_path or "")
    headers = {"accept": "application/json", "Authorization": api.api_key}

    probes: list[tuple[str, dict[str, object]]] = [
        ("no parameters", {}),
        ("limit only", {"limit": 5}),
        ("sort=archiveName", {"limit": 5, "sort": "archiveName ASC"}),
        ("sort=exposureName", {"limit": 5, "sort": "exposureName ASC"}),
    ]

    print(f"GET {url}\n")
    for label, params in probes:
        try:
            r = requests.get(url, headers=headers, params=params, timeout=60)
        except Exception as exc:
            print(f"  {label:<22} could not connect: {exc}")
            continue

        note = ""
        if r.status_code >= 400:
            body = " ".join(r.text.split())[:200]
            if api.api_key and api.api_key in body:
                body = body.replace(api.api_key, "<key>")
            note = f"  {body}"
        elif isinstance(payload := _json(r), list):
            note = f"  {len(payload)} rows"
            if payload and isinstance(payload[0], dict):
                note += f"  fields: {', '.join(sorted(payload[0])[:12])}"
        print(f"  {label:<22} {r.status_code}{note}")

    print(
        "\nFirst 200 wins. If 3 passes and 4 fails, exposureName is not a "
        "sortable field:\nset RECONCILE_VAULT_NAME_FIELD=archiveName in .env."
    )
    return 0

def _json(response: object) -> object:
    try:
        return response.json()
    except Exception:
        return None

if __name__ == "__main__":
    sys.exit(main())
