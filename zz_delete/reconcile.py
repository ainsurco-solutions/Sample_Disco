from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

class Outcome(str, Enum):

    MISSING = "Missing"
    NOT_ARCHIVED = "In transit"
    NOT_IN_INVENTORY = "Not on prem"
    ORPHANED = "Orphaned"
    DUPLICATE = "Duplicate"
    OUT_OF_SCOPE = "Not in scope"
    IMPORT_UNCHECKED = "Import unchecked"
    ARCHIVED = "Archived"
    IMPORTED = "Archiving unchecked"

FINDINGS: frozenset[Outcome] = frozenset(
    {
        Outcome.MISSING,
        Outcome.NOT_ARCHIVED,
        Outcome.NOT_IN_INVENTORY,
        Outcome.ORPHANED,
        Outcome.DUPLICATE,
    }
)

DONE: frozenset[Outcome] = frozenset({Outcome.ARCHIVED})

UNCHECKED: frozenset[Outcome] = frozenset(
    {Outcome.IMPORTED, Outcome.IMPORT_UNCHECKED}
)

MASTER_NAME_COLUMNS = ("database name", "database_name", "name", "db name", "db_name")

CREATOR_COLUMNS = ("createdby", "created_by", "created by", "owner", "archivedby")
CREATED_DATE_COLUMNS = ("createdat", "created_at", "created at", "created", "createddate")

AUTOMATED_ACTORS = frozenset({"datavault"})

TARGET_NAME_COLUMNS = (
    "databasename",
    "database name",
    "exposurename",
    "exposure name",
    "exposuresetname",
    "exposure set name",
    "name",
    "exposure",
)

def match_key(name: str) -> str:
    return name.strip().casefold()

@dataclass(frozen=True)
class Row:

    name: str
    data: dict[str, str]

    @property
    def key(self) -> str:
        return match_key(self.name)

def fingerprint(rows: Iterable[Row]) -> str:
    digest = hashlib.blake2b(digest_size=8)
    for row in rows:
        digest.update(row.key.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()

@dataclass(frozen=True)
class ResultRow:

    outcome: Outcome
    name: str
    source: str
    note: str = ""
    data: dict[str, str] = field(default_factory=dict)

    @property
    def is_finding(self) -> bool:
        return self.outcome in FINDINGS

@dataclass(frozen=True)
class Reconciliation:

    rows: tuple[ResultRow, ...]
    master_count: int
    target_count: int
    vault_count: int = 0
    vault_checked: bool = False
    platform_checked: bool = True
    target_fingerprint: str = ""
    vault_fingerprint: str = ""
    scope_count: int = 0
    scope_checked: bool = False
    scope_names: int = 0

    @property
    def targets_are_identical(self) -> bool:
        return (
            self.vault_checked
            and self.target_count > 0
            and self.vault_count > 0
            and self.target_fingerprint == self.vault_fingerprint
        )

    @property
    def bridge_wholly_inside_vault(self) -> bool:
        if not (self.vault_checked and self.target_count and self.vault_count):
            return False
        tally = self.counts()
        return tally[Outcome.NOT_ARCHIVED] == 0

    def counts(self) -> dict[Outcome, int]:
        tally = dict.fromkeys(Outcome, 0)
        for row in self.rows:
            tally[row.outcome] += 1
        return tally

    def of(self, outcome: Outcome) -> tuple[ResultRow, ...]:
        return tuple(row for row in self.rows if row.outcome is outcome)

    @property
    def findings(self) -> tuple[ResultRow, ...]:
        return tuple(row for row in self.rows if row.is_finding)

    @property
    def is_clean(self) -> bool:
        return not self.findings

class LoadError(Exception):
    pass

def _pick_column(headers: Sequence[str], candidates: Sequence[str]) -> str | None:
    folded = {h.strip().casefold(): h for h in headers if h}
    for candidate in candidates:
        if candidate in folded:
            return folded[candidate]
    return None

def _read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
    return headers, rows

def load_source_list(text: str, kind: str = "inventory") -> list[Row]:
    headers, raw = _read_csv(text)
    if not headers:
        raise LoadError(f"The {kind} file has no header row.")

    name_col = _pick_column(headers, MASTER_NAME_COLUMNS)
    if name_col is None:
        raise LoadError(
            f"No database-name column found in the {kind} file. Looked for: "
            + ", ".join(MASTER_NAME_COLUMNS)
            + f". Found: {', '.join(headers)}"
        )

    rows: list[Row] = []
    for raw_row in raw:
        name = (raw_row.get(name_col) or "").strip()
        if not name:
            continue
        data = dict(raw_row)
        data["_key_column"] = name_col
        rows.append(Row(name=name, data=data))
    return rows

def target_headers(text: str) -> list[str]:
    headers, _ = _read_csv(text)
    return headers

def guess_key_column(headers: Sequence[str]) -> str | None:
    return _pick_column(headers, TARGET_NAME_COLUMNS)

def load_target_from_csv(text: str, key_column: str | None = None) -> list[Row]:
    headers, raw = _read_csv(text)
    if not headers:
        raise LoadError("The target file has no header row.")

    if key_column is None:
        name_col = guess_key_column(headers)
        if name_col is None:
            raise LoadError(
                "Could not guess which column holds the name in this target "
                f"file. Choose one explicitly. Found: {', '.join(headers)}"
            )
    else:
        name_col = _pick_column(headers, (key_column.strip().casefold(),))
        if name_col is None:
            raise LoadError(
                f"Column {key_column!r} is not in this file. "
                f"Found: {', '.join(headers)}"
            )

    rows: list[Row] = []
    for raw_row in raw:
        name = (raw_row.get(name_col) or "").strip()
        if not name:
            continue
        data = dict(raw_row)
        data["_key_column"] = name_col
        rows.append(Row(name=name, data=data))
    return rows

TargetLoader = Callable[[], list[Row]]

class QueryError(Exception):
    pass

def _flatten_archive(item: dict[str, object]) -> dict[str, str]:
    flat: dict[str, str] = {}
    nested: dict[str, str] = {}

    for key, value in item.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if inner_value is None or isinstance(inner_value, (dict, list)):
                    if inner_value is not None:
                        flat[f"{key}.{inner_key}"] = str(inner_value)
                    continue
                flat[f"{key}.{inner_key}"] = str(inner_value)
                nested.setdefault(inner_key, str(inner_value))
        else:
            flat[key] = str(value)

    for key, value in nested.items():
        flat.setdefault(key, value)
    return flat

def load_vault_from_api(
    url: str,
    api_key: str,
    *,
    name_field: str = "archiveName",
    sort_field: str = "archiveId",
    id_field: str = "archiveId",
    label: str = "archive API",
    page_size: int = 1000,
    max_pages: int = 100,
) -> list[Row]:
    try:
        import requests
    except ImportError as exc:
        raise QueryError(
            "requests is not installed, so this tool cannot call the API. "
            "Upload a CSV instead, or `pip install requests`."
        ) from exc

    rows: list[Row] = []
    seen_keys: set[str] = set()
    offset = 0

    for _ in range(max_pages):
        try:
            response = requests.get(
                url,
                headers={"accept": "application/json", "Authorization": api_key},
                params={
                    **({"sort": f"{sort_field} ASC"} if sort_field else {}),
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=60,
            )
        except Exception as exc:
            raise QueryError(f"Could not reach the {label}: {exc}") from exc

        if response.status_code == 401:
            raise QueryError(
                f"The {label} rejected the key (401). Check MOODYS_API_KEY."
            )
        if response.status_code == 403:
            raise QueryError(
                f"The {label} refused the request (403). This endpoint is "
                "admindata/v1 and needs the Data Admin role -- a key that "
                "reads one endpoint in this family may not read another."
            )
        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:400]
            if api_key and api_key in detail:
                detail = detail.replace(api_key, "<key>")
            hint = ""
            if response.status_code == 400:
                culprits = []
                if sort_field and sort_field in detail:
                    culprits.append(f"sorted by '{sort_field}'")
                if name_field and name_field in detail:
                    culprits.append(f"matched on '{name_field}'")
                if culprits:
                    hint = (
                        f" The request {' and '.join(culprits)}. The server "
                        "does not recognise that field, whatever the "
                        "documentation lists -- pass sort_field='' to drop "
                        "the sort, or set the match field to one the endpoint "
                        "returns."
                    )
                else:
                    hint = (
                        f" The request sorted by '{sort_field}' and matched "
                        f"on '{name_field}'."
                    )
            raise QueryError(
                f"The {label} returned {response.status_code}."
                + (f" It said: {detail}" if detail else "")
                + hint
            )

        try:
            page = response.json()
        except ValueError as exc:
            raise QueryError(f"The {label} did not return JSON.") from exc

        if isinstance(page, dict):
            for key in ("searchItems", "items", "data", "results", "archives"):
                if isinstance(page.get(key), list):
                    page = page[key]
                    break
            else:
                raise QueryError(
                    f"The {label} returned an object, not the documented "
                    f"array. Keys: {', '.join(sorted(page))}"
                )
        if not isinstance(page, list):
            raise QueryError(f"The {label} returned an unexpected shape.")

        for item in page:
            if not isinstance(item, dict):
                continue
            flat = _flatten_archive(item)
            name = str(flat.get(name_field) or "").strip()
            if not name:
                continue
            identity = str(flat.get(id_field) or "").strip()
            key = f"id:{identity}" if identity else f"name:{match_key(name)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            flat["_key_column"] = name_field
            rows.append(Row(name=name, data=flat))

        if len(page) < page_size:
            break
        offset += page_size
    else:
        raise QueryError(
            f"The {label} returned more than {max_pages * page_size} rows "
            "without ending. Stopped rather than looping."
        )

    return rows

def load_bridge_from_api(
    url: str,
    api_key: str,
    *,
    name_field: str = "databaseName",
    sort_field: str = "",
    page_size: int = 1000,
    max_pages: int = 100,
) -> list[Row]:
    return load_vault_from_api(
        url,
        api_key,
        name_field=name_field,
        sort_field=sort_field,
        id_field="databaseId",
        label="Data Bridge API",
        page_size=page_size,
        max_pages=max_pages,
    )

def load_inventory_from_sql(
    connection_string: str, query: str, server_label: str = ""
) -> list[Row]:
    try:
        import pyodbc
    except ImportError as exc:
        raise QueryError(
            "pyodbc is not installed, so this tool cannot query a SQL Server. "
            "Upload a CSV instead, or `pip install pyodbc`."
        ) from exc

    try:
        with pyodbc.connect(connection_string, timeout=10) as connection:
            cursor = connection.cursor()
            cursor.execute(query)
            columns = [d[0] for d in cursor.description]
            fetched = [
                dict(zip(columns, row, strict=False)) for row in cursor.fetchall()
            ]
    except Exception as exc:
        raise QueryError(
            f"Could not query {server_label or 'the server'}: {exc}"
        ) from exc

    rows: list[Row] = []
    for record in fetched:
        name = str(next(iter(record.values()), "") or "").strip()
        if not name:
            continue
        data = {"database name": name, "_key_column": "database name"}
        for column, value in list(record.items())[1:]:
            if value is not None:
                data[column] = str(value).strip()
        if server_label:
            data["server"] = server_label
        rows.append(Row(name=name, data=data))
    return rows

SNAPSHOT_SIZE_FIELD = "snapshot_size_mb"
SNAPSHOT_AS_OF_FIELD = "snapshot_size_as_of"
_SNAPSHOT_FIELDS = (SNAPSHOT_SIZE_FIELD, SNAPSHOT_AS_OF_FIELD)

SIZE_UNITS_TO_MB = {"MB": 1.0, "GB": 1024.0, "KB": 1 / 1024, "bytes": 1 / (1024 * 1024)}

SIZE_NAME_COLUMNS = (*MASTER_NAME_COLUMNS, "database", "databasename", "dbname")
SIZE_SERVER_COLUMNS = ("server", "server name", "server_name", "servername", "instance", "host")
SIZE_VALUE_COLUMNS = (
    "size_mb", "size mb", "sizemb", "size (mb)", "size_in_mb", "size in mb", "database_size_mb",
    "size_gb", "size gb", "sizegb", "size (gb)", "size_in_gb", "size in gb",
    "size", "db_size", "database_size", "total_size",
)

@dataclass(frozen=True)
class SizeEntry:

    name: str
    server: str
    size_mb: float

    @property
    def key(self) -> str:
        return match_key(self.name)

@dataclass(frozen=True)
class SizeLoad:

    entries: tuple[SizeEntry, ...]
    skipped: int

def size_columns_guess(headers: Sequence[str]) -> tuple[str | None, str | None, str | None]:
    return (
        _pick_column(headers, SIZE_NAME_COLUMNS),
        _pick_column(headers, SIZE_VALUE_COLUMNS),
        _pick_column(headers, SIZE_SERVER_COLUMNS),
    )

def size_unit_guess(size_column: str | None) -> str:
    label = (size_column or "").casefold()
    if "gb" in label:
        return "GB"
    if "kb" in label:
        return "KB"
    if "byte" in label:
        return "bytes"
    return "MB"

def load_size_reference(
    text: str,
    *,
    name_column: str,
    size_column: str,
    unit: str = "MB",
    server_column: str | None = None,
) -> SizeLoad:
    if unit not in SIZE_UNITS_TO_MB:
        raise LoadError(f"Unknown unit {unit!r}: use one of {', '.join(SIZE_UNITS_TO_MB)}.")
    headers, raw = _read_csv(text)
    if not headers:
        raise LoadError("The size file has no header row.")
    columns = {"name": name_column, "size": size_column}
    if server_column:
        columns["server"] = server_column
    resolved: dict[str, str] = {}
    for role, wanted in columns.items():
        found = _pick_column(headers, (wanted.strip().casefold(),))
        if found is None:
            raise LoadError(f"Column {wanted!r} is not in this file. Found: {', '.join(headers)}")
        resolved[role] = found

    factor = SIZE_UNITS_TO_MB[unit]
    entries: list[SizeEntry] = []
    skipped = 0
    for record in raw:
        name = (record.get(resolved["name"]) or "").strip()
        value = (record.get(resolved["size"]) or "").strip().replace(",", "")
        try:
            size = float(value) * factor
        except ValueError:
            size = -1.0
        if not name or size < 0:
            skipped += 1
            continue
        server = (record.get(resolved["server"]) or "").strip() if "server" in resolved else ""
        entries.append(SizeEntry(name=name, server=server, size_mb=round(size, 3)))
    return SizeLoad(entries=tuple(entries), skipped=skipped)

@dataclass(frozen=True)
class SizeMatch:

    live: int
    matched: int
    ambiguous: int
    not_live: int

    @property
    def unmatched(self) -> int:
        return self.live - self.matched - self.ambiguous

def _same_server(live: str, snapshot: str) -> bool:
    a, b = live.strip().casefold(), snapshot.strip().casefold()
    return bool(a and b) and (a == b or a in b or b in a)

def apply_size_reference(
    rows: Sequence[Row], entries: Sequence[SizeEntry], as_of: str
) -> tuple[list[Row], SizeMatch]:
    by_key: dict[str, list[SizeEntry]] = {}
    for entry in entries:
        by_key.setdefault(entry.key, []).append(entry)

    out: list[Row] = []
    matched = ambiguous = 0
    for row in rows:
        data = {k: v for k, v in row.data.items() if k not in _SNAPSHOT_FIELDS}
        candidates = by_key.get(row.key, [])
        if len(candidates) > 1:
            server = str(row.data.get(SERVER_FIELD, ""))
            candidates = [c for c in candidates if _same_server(server, c.server)]
            if len(candidates) != 1:
                ambiguous += 1
                out.append(Row(name=row.name, data=data))
                continue
        if candidates:
            matched += 1
            data[SNAPSHOT_SIZE_FIELD] = f"{candidates[0].size_mb:.3f}".rstrip("0").rstrip(".")
            data[SNAPSHOT_AS_OF_FIELD] = as_of
        out.append(Row(name=row.name, data=data))

    live_keys = {r.key for r in rows}
    not_live = sum(1 for key in by_key if key not in live_keys)
    return out, SizeMatch(live=len(rows), matched=matched, ambiguous=ambiguous, not_live=not_live)

def _duplicates(rows: Iterable[Row]) -> dict[str, list[Row]]:
    by_key: dict[str, list[Row]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)
    return {key: group for key, group in by_key.items() if len(group) > 1}

def _classify_against(
    key: str,
    dupes: dict[str, list[Row]],
    by_key: dict[str, Row],
) -> tuple[str, Row | None]:
    if key in dupes:
        return "duplicate", None
    hit = by_key.get(key)
    return ("hit", hit) if hit is not None else ("miss", None)

def reconcile(
    inventory: Sequence[Row],
    platform: Sequence[Row] | None = None,
    vault: Sequence[Row] | None = None,
    scope: Sequence[Row] | None = None,
) -> Reconciliation:
    platform_checked = platform is not None
    platform_rows: Sequence[Row] = platform or ()
    vault_checked = vault is not None
    vault_rows: Sequence[Row] = vault or ()
    scope_checked = scope is not None
    scope_rows: Sequence[Row] = scope or ()

    results: list[ResultRow] = []

    inventory_dupes = _duplicates(inventory)
    scope_dupes = _duplicates(scope_rows)
    platform_dupes = _duplicates(platform_rows)
    vault_dupes = _duplicates(vault_rows)

    inventory_by_key = {r.key: r for r in inventory if r.key not in inventory_dupes}
    scope_keys = {r.key for r in scope_rows}
    platform_by_key = {
        r.key: r for r in platform_rows if r.key not in platform_dupes
    }
    vault_by_key = {r.key: r for r in vault_rows if r.key not in vault_dupes}

    seen_platform: set[str] = set()
    seen_vault: set[str] = set()
    seen_source: set[str] = set()

    for row in inventory:
        seen_source.add(row.key)

        if row.key in inventory_dupes:
            results.append(
                ResultRow(
                    outcome=Outcome.DUPLICATE,
                    name=row.name,
                    source="inventory",
                    note=(
                        f"appears {len(inventory_dupes[row.key])} times "
                        "in Source (On Prem)"
                    ),
                    data=row.data,
                )
            )
            continue

        if scope_checked and row.key not in scope_keys:
            results.append(
                ResultRow(
                    Outcome.OUT_OF_SCOPE,
                    row.name,
                    "inventory",
                    "on prem, but not on the Scope list",
                    row.data,
                )
            )
            continue

        p_state, p_hit = _classify_against(row.key, platform_dupes, platform_by_key)
        v_state, v_hit = _classify_against(row.key, vault_dupes, vault_by_key)

        if p_state == "duplicate" or v_state == "duplicate":
            side = "platform" if p_state == "duplicate" else "vault"
            count = len((platform_dupes if side == "platform" else vault_dupes)[row.key])
            if row.key in platform_dupes or row.key in platform_by_key:
                seen_platform.add(row.key)
            if row.key in vault_dupes or row.key in vault_by_key:
                seen_vault.add(row.key)
            results.append(
                ResultRow(
                    outcome=Outcome.DUPLICATE,
                    name=row.name,
                    source=side,
                    note=f"matches {count} {side} rows with the same name",
                    data=row.data,
                )
            )
            continue

        merged = dict(row.data)
        if p_hit is not None:
            seen_platform.add(row.key)
            merged.update(p_hit.data)
        if v_hit is not None:
            seen_vault.add(row.key)
            merged.update(v_hit.data)

        if not platform_checked:
            if v_hit is not None:
                results.append(
                    ResultRow(
                        Outcome.ARCHIVED,
                        row.name,
                        "inventory",
                        "in Data Vault; no Data Bridge list supplied, but "
                        "being archived proves it crossed",
                        merged,
                    )
                )
            else:
                results.append(
                    ResultRow(
                        Outcome.IMPORT_UNCHECKED,
                        row.name,
                        "inventory",
                        "not in Data Vault, and no Data Bridge list was "
                        "supplied — whether it ever imported is unchecked",
                        merged,
                    )
                )
            continue

        if not vault_checked:
            if p_hit is not None:
                results.append(
                    ResultRow(
                        Outcome.IMPORTED,
                        row.name,
                        "inventory",
                        "in Data Bridge; no Data Vault list supplied, so "
                        "archiving is unconfirmed",
                        merged,
                    )
                )
            else:
                results.append(
                    ResultRow(
                        Outcome.MISSING,
                        row.name,
                        "inventory",
                        "not in Data Bridge",
                        merged,
                    )
                )
            continue

        if v_hit is not None:
            results.append(
                ResultRow(
                    Outcome.ARCHIVED,
                    row.name,
                    "inventory",
                    ""
                    if p_hit is not None
                    else "in Data Vault, but not on the Data Bridge list",
                    merged,
                )
            )
        elif p_hit is not None:
            results.append(
                ResultRow(
                    Outcome.NOT_ARCHIVED,
                    row.name,
                    "inventory",
                    "in Data Bridge, not yet archived in Data Vault",
                    merged,
                )
            )
        else:
            results.append(
                ResultRow(
                    Outcome.MISSING,
                    row.name,
                    "inventory",
                    "in neither Data Bridge nor Data Vault",
                    merged,
                )
            )

    reported_scope_dupes: set[str] = set()
    for row in scope_rows:
        if row.key in scope_dupes:
            if row.key not in reported_scope_dupes:
                reported_scope_dupes.add(row.key)
                seen_source.add(row.key)
                results.append(
                    ResultRow(
                        outcome=Outcome.DUPLICATE,
                        name=row.name,
                        source="scope",
                        note=(
                            f"appears {len(scope_dupes[row.key])} times "
                            "on the Scope list"
                        ),
                        data=row.data,
                    )
                )
            continue
        if row.key not in inventory_by_key and row.key not in inventory_dupes:
            seen_source.add(row.key)
            results.append(
                ResultRow(
                    Outcome.NOT_IN_INVENTORY,
                    row.name,
                    "scope",
                    "on the Scope list, absent from Source (On Prem)",
                    row.data,
                )
            )

    for rows_, dupes_, seen_, side in (
        (platform_rows, platform_dupes, seen_platform, "platform"),
        (vault_rows, vault_dupes, seen_vault, "vault"),
    ):
        for row in rows_:
            if row.key in dupes_:
                if row.key not in seen_:
                    results.append(
                        ResultRow(
                            outcome=Outcome.DUPLICATE,
                            name=row.name,
                            source=side,
                            note=(
                                f"appears {len(dupes_[row.key])} times "
                                f"in the {side} list"
                            ),
                            data=row.data,
                        )
                    )
                continue
            if row.key not in seen_ and row.key not in seen_source:
                results.append(
                    ResultRow(
                        Outcome.ORPHANED,
                        row.name,
                        side,
                        f"in {'Data Bridge' if side == 'platform' else 'Data Vault'}"
                        " with no matching row in Source or Scope",
                        row.data,
                    )
                )

    return Reconciliation(
        rows=tuple(results),
        master_count=len(inventory),
        target_count=len(platform_rows),
        vault_count=len(vault_rows),
        vault_checked=vault_checked,
        platform_checked=platform_checked,
        target_fingerprint=fingerprint(platform_rows),
        vault_fingerprint=fingerprint(vault_rows) if vault_checked else "",
        scope_count=len(scope_rows),
        scope_checked=scope_checked,
        scope_names=len(scope_keys),
    )

@dataclass(frozen=True)
class FunnelStage:

    name: str
    count: int
    of_previous: int
    note: str = ""

    unreachable: int = 0

    @property
    def lost(self) -> int:
        return max(self.of_previous - self.count, 0)

    @property
    def percent_of_previous(self) -> float:
        return 100.0 * self.count / self.of_previous if self.of_previous else 0.0

SIZE_FIELDS_MB = ("sizeInMb", "metrics_dbsize_actual", "metrics_size", "snapshot_size_mb")

IMPLAUSIBLE_MB_PER_DATABASE = 10_000_000

@dataclass(frozen=True)
class SizeTotal:

    megabytes: float = 0.0
    rows: int = 0
    rows_with_size: int = 0
    field_used: str = ""

    @property
    def units_look_wrong(self) -> bool:
        if not self.rows_with_size:
            return False
        return (
            self.megabytes / self.rows_with_size
        ) > IMPLAUSIBLE_MB_PER_DATABASE

    @property
    def gigabytes(self) -> float:
        return self.megabytes / 1000.0

    @property
    def complete(self) -> bool:
        return self.rows > 0 and self.rows_with_size == self.rows

    @property
    def missing_rows(self) -> int:
        return max(self.rows - self.rows_with_size, 0)

SERVER_FIELD = "server"

def rows_per_server(rows: Sequence[Row]) -> list[tuple[str, int]]:
    tally: dict[str, int] = {}
    for row in rows:
        label = str(row.data.get(SERVER_FIELD) or "").strip()
        tally[label] = tally.get(label, 0) + 1
    return sorted(
        tally.items(), key=lambda pair: (pair[0] == "", -pair[1], pair[0])
    )

def duplicate_groups(
    rows: Sequence[Row], distinguish_by: Sequence[str] = ()
) -> list[tuple[str, list[Row]]]:
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)
    return sorted(
        ((key, group) for key, group in grouped.items() if len(group) > 1),
        key=lambda pair: pair[0],
    )

def distinguishing_values(group: Sequence[Row], field_name: str) -> list[str]:
    values: list[str] = []
    for row in group:
        raw = row.data.get(field_name)
        text = "" if raw is None else str(raw).strip()
        if text and text not in values:
            values.append(text)
    return sorted(values)

ARCHIVE_TIME_FIELDS = ("archivedAt", "createdAt")

def newest_first(group: Sequence[Row]) -> list[Row]:

    def stamp(row: Row) -> str:
        for field_name in ARCHIVE_TIME_FIELDS:
            value = str(row.data.get(field_name) or "").strip()
            if value:
                return value
        return ""

    dated = [row for row in group if stamp(row)]
    undated = [row for row in group if not stamp(row)]
    return sorted(dated, key=stamp, reverse=True) + undated

def compare_to_newest(
    group: Sequence[Row], ignore: Sequence[str] = ()
) -> list[tuple[str, str, str]]:
    ordered = newest_first(group)
    if len(ordered) < 2:
        return []
    newest, older = ordered[0], ordered[1]
    skip = {name.casefold() for name in ignore}

    differences: list[tuple[str, str, str]] = []
    for field_name in sorted(set(newest.data) | set(older.data)):
        if field_name.startswith("_") or field_name.casefold() in skip:
            continue
        new_value = str(newest.data.get(field_name) or "").strip()
        old_value = str(older.data.get(field_name) or "").strip()
        if new_value != old_value:
            differences.append((field_name, new_value, old_value))
    return differences

def size_of(reconciliation: Reconciliation, outcome: Outcome) -> SizeTotal:
    total = 0.0
    rows = 0
    with_size = 0
    field_used = ""

    for row in reconciliation.of(outcome):
        rows += 1
        for field_name in SIZE_FIELDS_MB:
            raw = row.data.get(field_name)
            if raw in (None, ""):
                continue
            try:
                value = float(str(raw).strip())
            except ValueError:
                continue
            total += value
            with_size += 1
            field_used = field_used or field_name
            break

    return SizeTotal(
        megabytes=total,
        rows=rows,
        rows_with_size=with_size,
        field_used=field_used,
    )

def funnel(reconciliation: Reconciliation) -> tuple[FunnelStage, ...]:
    counts = reconciliation.counts()

    inventory = reconciliation.master_count
    out_of_scope = counts[Outcome.OUT_OF_SCOPE]
    in_scope = inventory - out_of_scope
    not_in_inventory = counts[Outcome.NOT_IN_INVENTORY]

    bridge = (
        counts[Outcome.ARCHIVED]
        + counts[Outcome.NOT_ARCHIVED]
        + counts[Outcome.IMPORTED]
    )
    vault = counts[Outcome.ARCHIVED]

    scope_note = (
        f"{out_of_scope} on prem but not wanted"
        if reconciliation.scope_checked
        else "no to-migrate list supplied — every on-prem row treated as wanted"
    )
    if not_in_inventory:
        scope_note += (
            f"; {not_in_inventory} wanted but absent from on prem "
            "(not counted here)"
        )

    stages = [
        FunnelStage(
            "On Prem",
            inventory,
            inventory,
            "databases on the on-prem SQL servers",
        ),
        FunnelStage(
            "In scope",
            in_scope,
            inventory,
            scope_note,
            unreachable=not_in_inventory,
        ),
        FunnelStage(
            "Data Bridge",
            bridge,
            in_scope,
            "confirmed in Data Bridge"
            if reconciliation.platform_checked
            else "inferred from Data Vault — no Data Bridge list supplied",
        ),
    ]

    if reconciliation.vault_checked:
        stages.append(
            FunnelStage(
                "Data Vault",
                vault,
                bridge,
                f"archived in Data Vault — {counts[Outcome.NOT_ARCHIVED]} still in transit",
            )
        )
    else:
        stages.append(
            FunnelStage(
                "Data Vault",
                0,
                bridge,
                "no Data Vault list supplied — archiving unconfirmed, not zero",
            )
        )
    return tuple(stages)

def funnel_to_csv(reconciliation: Reconciliation) -> str:
    stages = funnel(reconciliation)
    scope_base = next((s.count for s in stages if s.name == "In scope"), 0)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "stage",
            "count",
            "lost_at_this_stage",
            "percent_of_scope",
            "percent_of_previous",
            "note",
        ]
    )
    for stage in stages:
        share = (
            f"{100.0 * stage.count / scope_base:.1f}"
            if scope_base and stage.name not in ("On Prem",)
            else ""
        )
        writer.writerow(
            [
                stage.name,
                stage.count,
                stage.lost,
                share,
                f"{stage.percent_of_previous:.1f}",
                stage.note,
            ]
        )
    return out.getvalue()

def to_csv(reconciliation: Reconciliation) -> str:
    extra_keys: list[str] = []
    for row in reconciliation.rows:
        for key in row.data:
            if not key.startswith("_") and key not in extra_keys:
                extra_keys.append(key)

    header = ["outcome", "name", "side", "note", *extra_keys]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in reconciliation.rows:
        writer.writerow(
            [row.outcome.value, row.name, row.source, row.note]
            + [row.data.get(key, "") for key in extra_keys]
        )
    return out.getvalue()
