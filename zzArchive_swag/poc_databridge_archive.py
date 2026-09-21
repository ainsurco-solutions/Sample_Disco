from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from settings import load_settings

_FORMAT_CODES = {"mdf": 0, "bak": 1, "dacpac": 2}

_SUCCESS_STATUSES = {"SUCCESS", "Succeeded"}
_FAILURE_STATUSES = {"FAILED", "Failed", "CANCELLED", "Cancelled"}

LARGE_FILE_THRESHOLD_BYTES = 5 * 1024**3
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 900

def _step(n: str, title: str) -> None:
    print(f"\n[{n}] {title}")

def _ok(msg: str) -> None:
    print(f"    -> {msg}")

def _fail(msg: str) -> None:
    print(f"    !! {msg}")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bak",
        help=r"Path to the .bak file -- a UNC network path "
        r"(\\fileserver\share\folder\test.bak) or a mapped drive letter "
        r"(Z:\folder\test.bak) both work; pathlib handles either. Not "
        "needed with --check-job.",
    )
    parser.add_argument("--instance", help="Data Bridge SQL instance name. Not needed with --check-job.")
    parser.add_argument("--database", help="Database name to create. Not needed with --check-job.")
    parser.add_argument(
        "--check-job",
        default="",
        metavar="JOB_ID",
        help="Read-only: check an existing job's status and exit -- no "
        "upload, no import trigger, no archive call. Use this before "
        "re-running the full script if a previous run got a job id but "
        "crashed before confirming it finished, so you don't risk "
        "triggering a second import/archive for the same database.",
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Skip the upload/import steps entirely and go straight to "
        "archiving -- for a database already confirmed imported (e.g. via "
        "--check-job). Needs --instance and --database, not --bak. Still "
        "confirms the database is actually present on Data Bridge (step "
        "6) before archiving it, as a safety check. WRITES DATA: unlike "
        "Data Bridge's import, Data Vault accepts duplicate archive names "
        "-- running this again for an already-archived database creates a "
        "second real archive rather than failing. Use --verify-only "
        "instead if you only need to check the result.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Read-only: run just step 9 (searchArchives) for --database "
        "and exit. No archive call, no risk of a duplicate. Use this to "
        "re-check verification (e.g. after a filter-syntax fix) without "
        "re-running --archive-only.",
    )
    parser.add_argument(
        "--resource-group-id",
        default="",
        help="x-rms-resource-group-id for the archive call. If omitted, "
        "the script tries to resolve one itself via --entitlement before "
        "the archive call (see step 7a) -- pass this explicitly only to "
        "skip that lookup or override it.",
    )
    parser.add_argument(
        "--entitlement",
        default="RI-DATAVAULT",
        help="Entitlement to resolve a resourceGroupId from, if "
        "--resource-group-id isn't given. RI-DATAVAULT is the reference "
        "doc's stated entitlement for the archive call, by analogy with "
        "RI-RISKMODELER for import -- itself part of what this run confirms.",
    )
    parser.add_argument(
        "--retention-date",
        default="",
        help="ISO 8601 expirationDate for the archive call, e.g. "
        "2031-09-21T00:00:00.000. Omit for a permanent archive (per the "
        "reference doc).",
    )
    parser.add_argument(
        "--content-type",
        default="application/octet-stream",
        help="Content-Type header sent on the S3 PUT (step 3). The "
        "presigned URL's X-Amz-SignedHeaders includes 'content-type', so "
        "this must exactly match whatever value the tenant used when it "
        "signed the URL, or S3 returns 403 SignatureDoesNotMatch. Not "
        "documented anywhere -- try a different value (or '' for no "
        "header) against a fresh presigned URL if the default fails.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform the upload/import/archive calls. Without "
        "this flag, only a dry-run summary is printed.",
    )
    args = parser.parse_args()

    try:
        import requests
    except ImportError:
        _fail("requests is not installed: pip install requests")
        return 1

    if args.check_job:
        api = load_settings()
        if not api.has_key:
            _fail("No MOODYS_API_KEY in .env -- copy .env.example and fill it in.")
            return 1
        if not api.host:
            _fail("No RECONCILE_API_HOST in .env, e.g. https://api-euw1.rms.com")
            return 1
        host = api.host.rstrip("/")
        session = requests.Session()
        session.headers.update({"Authorization": api.api_key})
        url = f"{host}/databridge/v1/Jobs/{args.check_job}"
        _step("check", f"GET {url} (read-only, single check)")
        resp = session.get(url, timeout=30)
        print(f"    http status: {resp.status_code}")
        print(f"    body: {resp.text[:1000]}")
        if resp.status_code < 300:
            try:
                body = resp.json()
                status = body.get("status") if isinstance(body, dict) else body
            except ValueError:
                status = resp.text.strip()
            print(f"\n    job status: {status}")
            if status in _SUCCESS_STATUSES:
                print("    -> terminal, succeeded. Safe to move on to archiving; do not re-upload.")
            elif status in _FAILURE_STATUSES:
                print("    -> terminal, failed. The import did not complete -- check with Moody's/MS Amlin before deciding whether re-running is safe, rather than assuming a retry is harmless.")
            else:
                print("    -> not terminal yet, or an unrecognised status string. If the import is actually done, tell me the exact text above and I'll add it to the known-success/failure sets.")
        return 0

    if args.verify_only:
        if not args.database:
            _fail("--database is required with --verify-only.")
            return 1
        api = load_settings()
        if not api.has_key:
            _fail("No MOODYS_API_KEY in .env -- copy .env.example and fill it in.")
            return 1
        if not api.host:
            _fail("No RECONCILE_API_HOST in .env, e.g. https://api-euw1.rms.com")
            return 1
        session = requests.Session()
        session.headers.update({"Authorization": api.api_key})
        return _verify_archive(session, api.host.rstrip("/"), args)

    if not args.instance or not args.database:
        _fail("--instance and --database are required unless using --check-job or --verify-only.")
        return 1

    bak_path: Path | None = None
    size_mb = 0.0
    if not args.archive_only:
        if not args.bak:
            _fail("--bak is required unless --archive-only or --check-job.")
            return 1
        bak_path = Path(args.bak)
        if not bak_path.is_file():
            _fail(
                f"Cannot find {bak_path} -- check the network path is reachable "
                "and you have read access (a UNC path needs the share mounted "
                "or your account to already have permission; a mapped drive "
                "needs the drive connected in this session)."
            )
            return 1
        size_bytes = bak_path.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        print(f"Using .bak file: {bak_path} ({size_mb:.1f} MB)")
        if size_bytes >= LARGE_FILE_THRESHOLD_BYTES:
            _fail(
                "File is >= 5 GB. This POC only implements the small-database "
                "flow -- use the large multipart flow for a file this size "
                "(see client.py::_upload_large)."
            )
            return 1

    api = load_settings()
    if not api.has_key:
        _fail("No MOODYS_API_KEY in .env -- copy .env.example and fill it in.")
        return 1
    if not api.host:
        _fail("No RECONCILE_API_HOST in .env, e.g. https://api-euw1.rms.com")
        return 1

    host = api.host.rstrip("/")

    if not args.confirm:
        print("\nDRY RUN -- pass --confirm to actually upload/import/archive.")
        print("Would run against:")
        print(f"  host              = {host}")
        print(f"  mode              = {'archive-only (no upload/import)' if args.archive_only else 'full pipeline'}")
        print(f"  instance          = {args.instance}")
        print(f"  database          = {args.database}")
        if bak_path is not None:
            print(f"  bak file          = {bak_path} ({size_mb:.1f} MB)")
        print(
            f"  resource-group-id = "
            f"{args.resource_group_id or f'(none given -- will try to resolve via {args.entitlement!r})'}"
        )
        print(f"  retention-date    = {args.retention_date or '(none -- permanent archive)'}")
        return 0

    session = requests.Session()
    session.headers.update({"Authorization": api.api_key})

    if args.archive_only:
        return _archive_and_verify(session, host, args)

    assert bak_path is not None

    _step("1", "GET /databridge/v1/sql-instances")
    resp = session.get(f"{host}/databridge/v1/sql-instances", timeout=30)
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    names = [item.get("name") for item in resp.json()]
    _ok(f"{len(names)} instance(s): {names}")
    if args.instance not in names:
        _fail(f"{args.instance!r} is not in the list above -- check spelling.")
        return 1

    _step("2", f"GET .../Databases/{args.database}/import?importFrom=bak")
    resp = session.get(
        f"{host}/databridge/v1/sql-instances/{args.instance}/Databases/{args.database}/import",
        params={"importFrom": _FORMAT_CODES["bak"]},
        headers={"accept": "text/plain"},
        timeout=30,
    )
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    try:
        body = resp.json()
    except ValueError:
        _ok(f"response is not JSON -- raw text: {resp.text[:500]!r}")
        _fail(
            "Cannot proceed automatically. If the text above is the presigned "
            "URL itself, tell me and I'll change this to use resp.text directly."
        )
        return 1
    mdf_uri = body.get("backupUri") or body.get("mdfUri") if isinstance(body, dict) else None
    if not mdf_uri:
        _fail(
            f"neither 'backupUri' nor 'mdfUri' in the response -- this shape "
            f"is not one we've seen before. Full body: {body!r}"
        )
        _fail(
            "Report the body above and I'll correct the field name in the "
            "script -- same situation this project already hit once with "
            "'archiveName' vs 'exposureName' on the archives endpoint."
        )
        return 1
    _ok(f"got presigned upload URL ({'backupUri' if body.get('backupUri') else 'mdfUri'})")

    _step("3", f"PUT {bak_path.name} to presigned URL ({size_mb:.1f} MB)")
    put_headers = {}
    if args.content_type:
        put_headers["Content-Type"] = args.content_type
        print(f"    sending Content-Type: {args.content_type!r}")
    else:
        print("    sending no Content-Type header")
    with bak_path.open("rb") as fh:
        put_resp = requests.put(mdf_uri, data=fh, headers=put_headers, timeout=None)
    print(f"    status: {put_resp.status_code}")
    if put_resp.status_code == 403 and "SignatureDoesNotMatch" in put_resp.text:
        _fail(
            "S3 SignatureDoesNotMatch -- the presigned URL was signed with "
            "'content-type' as a required header (visible in its "
            "X-Amz-SignedHeaders query param)."
        )
        match = re.search(r"<StringToSign>(.*?)</StringToSign>", put_resp.text, re.S)
        if match:
            print("\n    Full <StringToSign> from AWS (the canonical request it hashed):")
            print("    " + match.group(1).replace("\\n", "\n    "))
            print(
                "\n    Look for a 'content-type:...' line above -- that exact "
                "string is what --content-type must match."
            )
        else:
            print(f"\n    Full response body (no <StringToSign> found):\n    {put_resp.text}")
        print(
            "\n    Each attempt needs a fresh presigned URL -- this one may "
            "now be considered used."
        )
        return 1
    if put_resp.status_code >= 300:
        _fail(put_resp.text[:300])
        return 1
    _ok("upload complete")

    _step("4", "POST .../import (trigger job)")
    resp = session.post(
        f"{host}/databridge/v1/sql-instances/{args.instance}/Databases/{args.database}/import",
        params={"importFrom": _FORMAT_CODES["bak"]},
        headers={"accept": "application/json", "content-type": "application/json"},
        json={"groupIds": []},
        timeout=30,
    )
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    job_id = resp.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
    if not job_id:
        body = resp.json() if resp.content else {}
        job_id = str(body.get("jobId") or body.get("id") or "")
    if not job_id:
        _fail("no Location header and no jobId/id body field -- cannot poll")
        return 1
    _ok(f"import job started: {job_id}")

    _step("5", f"GET /databridge/v1/Jobs/{job_id} (poll)")
    status = _poll(session, f"{host}/databridge/v1/Jobs/{job_id}")
    if status is None:
        _fail("timed out waiting for the import job")
        return 1
    _ok(f"import job terminal status: {status}")
    if status not in _SUCCESS_STATUSES:
        _fail("import did not succeed -- stopping before archiving")
        return 1

    return _archive_and_verify(session, host, args)

def _archive_and_verify(session: object, host: str, args: argparse.Namespace) -> int:
    _step("6", "GET .../databases (confirm database landed)")
    resp = session.get(f"{host}/databridge/v1/sql-instances/{args.instance}/databases", timeout=30)
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    landed = any(item.get("name") == args.database for item in resp.json())
    _ok(f"database present on Data Bridge: {landed}")
    if not landed:
        _fail("the database isn't listed on Data Bridge -- investigate before archiving")
        return 1

    resource_group_id = args.resource_group_id
    if not resource_group_id:
        _step(
            "7a",
            f"GET /platform/tenantdata/v1/entitlements/{args.entitlement}/resourcegroups "
            "(resolve resourceGroupId -- inferred by analogy with RI-RISKMODELER, "
            "NOT confirmed for this entitlement)",
        )
        resp = session.get(
            f"{host}/platform/tenantdata/v1/entitlements/{args.entitlement}/resourcegroups",
            timeout=30,
        )
        print(f"    status: {resp.status_code}")
        if resp.status_code >= 400:
            _fail(
                f"{resp.text[:300]}\n"
                "    Could not resolve a resourceGroupId this way. This "
                "confirms the lookup path is wrong for this entitlement, "
                "or the key lacks RI-DATAVAULT -- either way, worth "
                "recording. Proceeding to the archive call without the "
                "header, to see whether it's enforced at all."
            )
        else:
            groups = resp.json()
            if groups:
                resource_group_id = str(groups[0].get("resourceGroupId") or "")
                _ok(f"resolved resourceGroupId: {resource_group_id}")
            else:
                _fail("200 but empty list -- no resource group for this entitlement")

    _step("7b", "POST .../archive (Data Vault) -- not yet built into the adapter, this is the confirmation")
    archive_headers = {"content-type": "application/json"}
    if resource_group_id:
        archive_headers["x-rms-resource-group-id"] = resource_group_id
    else:
        print(
            "    (no resourceGroupId available -- the reference doc says "
            "this header is required; sending without it to see what the "
            "tenant actually does. A 400/403 here confirms the header is "
            "enforced; a 200 confirms it isn't, at least for this key.)"
        )
    archive_body: dict[str, str] = {}
    if args.retention_date:
        archive_body["expirationDate"] = args.retention_date
    resp = session.post(
        f"{host}/databridge/v1/sql-instances/{args.instance}/Databases/{args.database}/archive",
        headers=archive_headers,
        json=archive_body,
        timeout=30,
    )
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    archive_job_id = str(resp.json().get("jobId") or "")
    _ok(f"archive job started: {archive_job_id or '(no jobId returned)'}")

    if archive_job_id:
        _step(
            "8",
            f"GET /databridge/v1/Jobs/{archive_job_id} "
            "(poll archive job -- assumes the same Jobs endpoint as import; unconfirmed)",
        )
        archive_status = _poll(session, f"{host}/databridge/v1/Jobs/{archive_job_id}")
        if archive_status is None:
            _fail(
                "timed out, or this Jobs endpoint doesn't recognise an "
                "archive jobId -- check manually and note the finding"
            )
        else:
            _ok(f"archive job terminal status: {archive_status}")

    return _verify_archive(session, host, args)

def _verify_archive(session: object, host: str, args: argparse.Namespace) -> int:
    _step("9", f'GET /platform/admindata/v1/archives?filter=archiveName="{args.database}" (verify)')
    resp = session.get(
        f"{host}/platform/admindata/v1/archives",
        params={"filter": f'archiveName="{args.database}"', "limit": 100, "offset": 0},
        timeout=30,
    )
    print(f"    status: {resp.status_code}")
    if resp.status_code >= 400:
        _fail(resp.text[:300])
        return 1
    rows = resp.json()
    _ok(f"{len(rows)} archive row(s) matching archiveName == {args.database!r}")
    for row in rows:
        print(
            f"       archiveId={row.get('archiveId')} "
            f"archivedAt={row.get('archivedAt')} "
            f"expirationDate={row.get('expirationDate')} "
            f"sizeInMb={row.get('sizeInMb')}"
        )
    if not rows:
        print(
            "    No matching row found. The archive job reported success, "
            "so either the name doesn't match exactly (check case/exact "
            "spelling), or it hasn't propagated to this list yet."
        )
    if len(rows) > 1:
        print(
            "    More than one row -- Data Vault accepts duplicate archive "
            "names by design (data-vault-archive-api-findings.md §5). If "
            "you ran --archive-only more than once for this database, this "
            "is exactly that, not a bug."
        )

    print("\nDone. Compare expirationDate above against the intended retention.")
    return 0

def _poll(session: object, url: str) -> str | None:
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = session.get(url, timeout=30)
        print(f"    ... http status: {response.status_code}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            print(f"    ... body: {response.text[:300]!r}")
            return None
        try:
            body = response.json()
            raw_status = str(body.get("status") if isinstance(body, dict) else body)
        except ValueError:
            raw_status = response.text.strip()
        print(f"    ... status: {raw_status}")
        if raw_status in _SUCCESS_STATUSES or raw_status in _FAILURE_STATUSES:
            return raw_status
        if not raw_status:
            print(f"    ... empty body, treating as not-ready-yet: {response.text[:300]!r}")
        time.sleep(POLL_INTERVAL_SECONDS)
    return None

if __name__ == "__main__":
    sys.exit(main())
