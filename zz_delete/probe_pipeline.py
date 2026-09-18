# Documents the upload/verify pipeline and tests the read-only
# parts of it.
#
#     python probe_pipeline.py
#
# Never moves a file and never creates anything: the four POST/PUT
# steps are printed with what they would do and are not issued.
# Reads the key from .env and never prints it.

from __future__ import annotations

import json
import sys

from settings import load_settings

PIPELINE = [
    {
        "step": "1",
        "phase": "session",
        "method": "GET",
        "path": "/platform/tenantdata/v1/entitlements/{entitlement}/resourcegroups",
        "gives": "resourceGroupId",
        "note": "Cached per worker process. Take [0].resourceGroupId -- "
        "whether a tenant can return more than one is open question 3.1.",
        "reads": True,
    },
    {
        "step": "2",
        "phase": "session",
        "method": "GET",
        "path": "/platform/riskdata/v1/dataservers",
        "params": {"filter": "servertype=PLATFORM"},
        "gives": "serverId",
        "note": "Cached per worker process. The import job needs it.",
        "reads": True,
    },
    {
        "step": "3",
        "phase": "upload",
        "method": "POST",
        "path": "/platform/import/v1/folders",
        "gives": "folderId + presigned S3 credentials",
        "note": "CREATES an upload target. accessKeyId/secretAccessKey/"
        "sessionToken/region arrive base64-encoded and must be decoded.",
        "reads": False,
    },
    {
        "step": "4",
        "phase": "upload",
        "method": "PUT",
        "path": "(presigned S3 URL -- not a Moody's endpoint)",
        "gives": "the .BAK in place",
        "note": "WRITES DATA. AWS SigV4 with the ephemeral credentials from "
        "step 3. Token lifetime against a 4.4 GB average file is open "
        "question 5.1.",
        "reads": False,
    },
    {
        "step": "5",
        "phase": "upload",
        "method": "POST",
        "path": "/platform/riskdata/v1/exposuresets",
        "gives": "exposureSetId",
        "note": "CREATES an exposure set. The ID is in the Location response "
        "HEADER, not the body -- reading the body returns nothing useful.",
        "reads": False,
    },
    {
        "step": "6",
        "phase": "upload",
        "method": "POST",
        "path": "/platform/import/v1/jobs",
        "gives": "jobId",
        "note": "STARTS A REAL IMPORT. Location header again, not the body.",
        "reads": False,
    },
    {
        "step": "7",
        "phase": "upload",
        "method": "GET",
        "path": "/platform/import/v1/jobs/{jobId}",
        "gives": "job status",
        "note": "Polls until terminal. FINISHED is success -- not SUCCESS, "
        "which the collection's prose walkthrough implied. Needs a real job "
        "id, so this script cannot exercise it without one.",
        "reads": False,
    },
    {
        "step": "8",
        "phase": "verify",
        "method": "GET",
        "path": "/platform/riskdata/v1/exposures",
        "params": {"filter": "exposureName = 'PROBE_DOES_NOT_EXIST'"},
        "gives": "the exposure, if it landed",
        "note": "Backs the VERIFYING state. Probed with a name that cannot "
        "exist: an empty result proves the endpoint answers, without "
        "depending on any particular database being present.",
        "reads": True,
    },
]

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

    entitlement = api.entitlement or "RI-RISKMODELER"
    host = (api.host or "").rstrip("/")
    headers = {"accept": "application/json", "Authorization": api.api_key}

    print(f"Host: {host}")
    print(f"Entitlement: {entitlement}")
    print("\nRead-only. The four POST/PUT steps are documented, not issued.\n")

    for call in PIPELINE:
        path = call["path"].replace("{entitlement}", entitlement)
        label = f"{call['step']}. {call['method']:<4} {path}"
        print("-" * 74)
        print(label)
        print(f"   phase   : {call['phase']}")
        print(f"   gives   : {call['gives']}")
        print(f"   note    : {call['note']}")

        if not call["reads"]:
            why = (
                "this step changes state"
                if call["method"] in ("POST", "PUT")
                else "needs an id from an earlier step"
            )
            print(f"   NOT CALLED -- {why}.")
            continue

        try:
            response = requests.get(
                host + path,
                headers=headers,
                params=call.get("params"),
                timeout=60,
            )
        except Exception as exc:
            print(f"   result  : could not connect -- {exc}")
            continue

        body = " ".join(response.text.split())
        if api.api_key and api.api_key in body:
            body = body.replace(api.api_key, "<key>")
        print(f"   result  : {response.status_code}")
        if response.status_code >= 400:
            print(f"             {body[:200]}")
            continue

        _describe(response, call)

    print("-" * 74)
    print(
        "\nA 403 on steps 1, 2 or 8 with a working key elsewhere means the "
        "entitlement\ncovers one API family and not another -- worth knowing "
        "before a wave, not during."
    )
    return 0

def _describe(response: object, call: dict) -> None:
    try:
        payload = response.json()
    except Exception:
        print("             (not JSON)")
        return

    rows = payload if isinstance(payload, list) else payload.get("data", payload)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        print(f"             unexpected shape: {type(payload).__name__}")
        return

    print(f"             {len(rows)} row(s)")
    if not rows:
        return

    first = rows[0] if isinstance(rows[0], dict) else {}
    wanted = call["gives"].split(" + ")[0]
    for key in (wanted, "resourceGroupId", "serverId", "id", "name"):
        if key in first:
            print(f"             {key} = {first[key]!r}")
            break
    else:
        print(f"             fields: {', '.join(sorted(first)[:10])}")
    print(f"             sample: {json.dumps(first, default=str)[:180]}")

if __name__ == "__main__":
    sys.exit(main())
