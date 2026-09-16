#!/usr/bin/env python3
"""
reconcile.py
============

Source-to-target reconciliation: does every in-scope source database exist on
the target platform?

The whole tool's logic lives here, with no Streamlit import, so it can be tested
without a browser and reused from a script. `app.py` is a rendering layer on top.

See `spec` in this directory for the reasoning behind the matching rule and the
outcomes. The short version: matching is exact and case-folded, and nothing is
guessed, because a plausible wrong pairing is harder to spot than an obvious gap.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):  # noqa: UP042 - see below
    """What a reconciliation says about one row.

    Ordered by how much attention it deserves, which is also the order the UI
    shows them in.

    The distinction that matters is ARCHIVED vs IMPORTED_NOT_ARCHIVED. Per
    GLOSSARY.md, Data Bridge is the route in and Data Vault is the
    destination, and archiving is a separate explicit step that is not
    automatic on import (open question 13). A database on the platform but not
    in the vault is imported, not migrated -- reporting it as done would report
    a state the client has not got.

    `str, Enum` rather than `StrEnum` deliberately: StrEnum needs Python 3.11+,
    and these dev-tools get run on whatever interpreter the person has. The
    behaviour is the same for what this uses it for.
    """

    MISSING = "Missing"
    NOT_ARCHIVED = "Not archived"
    ORPHANED = "Orphaned"
    DUPLICATE = "Duplicate"
    NOT_ON_SERVER = "Not on server"
    OUT_OF_SCOPE = "Out of scope"
    ARCHIVED = "Archived"
    IMPORTED = "Imported"


#: Outcomes that mean somebody has to do something. Kept as a set rather than a
#: rule in the UI so a caller reporting on a run does not re-derive it.
FINDINGS: frozenset[Outcome] = frozenset(
    {
        Outcome.MISSING,
        Outcome.NOT_ARCHIVED,
        Outcome.ORPHANED,
        Outcome.DUPLICATE,
    }
)

#: The only outcome meaning a database is fully migrated. `IMPORTED` is
#: deliberately not in here: it is what gets reported when no vault list was
#: loaded, and it says "we confirmed the platform" rather than "this is done".
DONE: frozenset[Outcome] = frozenset({Outcome.ARCHIVED})

#: Column names accepted for each of the three fields the master file must
#: carry. Real spreadsheets spell these inconsistently and a rename upstream
#: should not break the tool, but note this is aliasing *column headers*, not
#: data values -- the same distinction parse_names.py draws when it folds the
#: LIABLITY misspelling and refuses to fold CP into PROPERTY.
MASTER_NAME_COLUMNS = ("database name", "database_name", "name", "db name", "db_name")
EXISTS_COLUMNS = ("exists_on_server", "exists on server", "exists", "on_server")
IN_SCOPE_COLUMNS = ("is_in_scope", "in_scope", "in scope", "is in scope", "scope")

#: Columns *guessed* as the key on a target file, best first. Only a default
#: for the picker -- the person loading the file confirms or overrides it.
#:
#: Nobody has confirmed which field actually holds the source database name.
#: Data Vault carries exposureName, databaseName and exposureSetName; the
#: platform list carries exposureSetName and no exposure name at all. Picking
#: between them in code would be the same confident wrong answer that made TR
#: into "Treaty" -- so this guesses, visibly, and a human confirms.
#: TASK-0054 owns the real answer.
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

#: Values read as "yes" in the Y/N columns. Anything else -- including blank,
#: and including a value nobody anticipated -- is read as "no", because the
#: conservative reading of "we are not sure this is in scope" is to exclude it
#: from the Missing count rather than to manufacture a finding.
TRUE_VALUES = frozenset({"y", "yes", "true", "1", "t"})


def is_yes(value: str | None) -> bool:
    """Read a Y/N spreadsheet cell.

    Unrecognised and blank both read as False. See TRUE_VALUES for why that
    direction rather than the other.
    """
    if value is None:
        return False
    return value.strip().casefold() in TRUE_VALUES


def match_key(name: str) -> str:
    """The key two names are compared on.

    Case-folded and stripped, and *nothing else*. No separator removal, no
    normalisation, no fuzzy distance. The estate writes AMLINUK and Amlin as
    different things and they may well be different things; folding them here
    would invent a match that nobody confirmed.
    """
    return name.strip().casefold()


@dataclass(frozen=True)
class Row:
    """One row from either input file.

    `name` is the key. `data` keeps every original column verbatim, including
    the name, so the UI and the CSV export can show what the file actually said
    rather than this tool's interpretation of it.
    """

    name: str
    data: dict[str, str]

    @property
    def key(self) -> str:
        return match_key(self.name)


@dataclass(frozen=True)
class ResultRow:
    """One row of a finished reconciliation."""

    outcome: Outcome
    name: str
    source: str  # "master" or "target"
    note: str = ""
    data: dict[str, str] = field(default_factory=dict)

    @property
    def is_finding(self) -> bool:
        return self.outcome in FINDINGS


@dataclass(frozen=True)
class Reconciliation:
    """The result of comparing a master register against one or both targets.

    `vault_checked` records whether a Data Vault list was supplied. Without one
    the tool cannot say anything about archiving, and says so -- rows come back
    `IMPORTED`, not `ARCHIVED`. Inferring "archived" from a platform hit would
    assert exactly the thing open question 13 says is not automatic.
    """

    rows: tuple[ResultRow, ...]
    master_count: int
    target_count: int
    vault_count: int = 0
    vault_checked: bool = False

    def counts(self) -> dict[Outcome, int]:
        """Per-outcome totals, including zeros, in Outcome order."""
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
        """True when nothing needs a human. Note this is not "everything matched"
        -- out-of-scope and not-on-server rows are expected, not problems."""
        return not self.findings


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

class LoadError(Exception):
    """A file could not be read as the kind of list it was supposed to be."""


#: Proportion of a flag column that may be blank or unrecognised before the
#: load warns about it. Above this and the column is probably not saying what
#: the reader assumes it says.
FLAG_WARN_THRESHOLD = 0.5


@dataclass(frozen=True)
class FlagColumn:
    """What one Y/N column in the master file actually contained.

    A missing flag column and a blank one fail in *opposite* directions: an
    absent column leaves rows in scope, so they stay in the Missing count,
    while a blank value reads as No and excuses the row entirely. The second is
    the dangerous one -- a register whose blanks mean "unknown" rather than
    "no" empties the Missing count and the run reports clean, which looks
    exactly like success.

    This records enough for the UI to say so before anyone acts on a number.
    """

    field: str
    column: str | None  # the header it matched, or None when absent
    yes: int = 0
    no: int = 0
    blank: int = 0
    unrecognised: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.yes + self.no + self.blank

    @property
    def found(self) -> bool:
        return self.column is not None

    @property
    def should_warn(self) -> bool:
        """True when the column is present but mostly not saying anything."""
        if not self.found or not self.total:
            return False
        return (self.blank / self.total) > FLAG_WARN_THRESHOLD

    def message(self) -> str | None:
        """A sentence for the UI, or None when nothing is worth saying."""
        if not self.found:
            return (
                f"No `{self.field}` column found, so every row is treated as "
                f"{'in scope' if self.field == 'is_in_scope' else 'on the server'}. "
                "Rows stay in the Missing count rather than being excused from it."
            )
        if self.should_warn:
            pct = round(100 * self.blank / self.total)
            excused = (
                "reported Out of scope"
                if self.field == "is_in_scope"
                else "reported Not on server"
            )
            return (
                f"`{self.column}` is blank on {pct}% of rows ({self.blank} of "
                f"{self.total}). Blank reads as No, so those rows are {excused} "
                "and never counted as Missing. If blank means *unknown* in this "
                "register rather than *no*, the findings are understated."
            )
        if self.unrecognised:
            shown = ", ".join(repr(v) for v in self.unrecognised[:5])
            return (
                f"`{self.column}` holds values this tool does not recognise "
                f"({shown}). Anything not a yes-value reads as No."
            )
        return None


@dataclass(frozen=True)
class MasterLoad:
    """Rows from the master register, plus what was observed while reading it.

    Iterable and len()-able so existing callers that just want the rows keep
    working unchanged.
    """

    rows: list[Row]
    flags: tuple[FlagColumn, ...] = ()

    def __iter__(self) -> Iterator[Row]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> Row:
        return self.rows[index]

    def warnings(self) -> list[str]:
        """Everything worth telling the person before they read a count."""
        return [m for m in (flag.message() for flag in self.flags) if m]


def _pick_column(headers: Sequence[str], candidates: Sequence[str]) -> str | None:
    """First header matching any candidate, compared case-insensitively."""
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


def _tally_flag(field: str, column: str | None, values: list[str]) -> FlagColumn:
    """Count what a flag column actually held, for the load's warnings."""
    if column is None:
        return FlagColumn(field=field, column=None)
    yes = no = blank = 0
    unrecognised: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if not cleaned:
            blank += 1
        elif is_yes(cleaned):
            yes += 1
        else:
            no += 1
            folded = cleaned.casefold()
            if folded not in ("n", "no", "false", "0", "f") and cleaned not in unrecognised:
                unrecognised.append(cleaned)
    return FlagColumn(
        field=field,
        column=column,
        yes=yes,
        no=no,
        blank=blank,
        unrecognised=tuple(unrecognised),
    )


def load_master(text: str) -> MasterLoad:
    """Read the master source register.

    Requires a name column; `exists_on_server` and `is_in_scope` are optional.
    A file without them still reconciles -- every row is simply treated as in
    scope and present, which is the reading that puts rows *into* the Missing
    count rather than quietly excusing them from it.

    Returns a `MasterLoad`, which iterates as the rows but also carries what
    the flag columns actually contained. A blank flag reads as No and excuses
    its row from the Missing count, so a register whose blanks mean "unknown"
    reports clean while checking nothing -- see `FlagColumn`.
    """
    headers, raw = _read_csv(text)
    if not headers:
        raise LoadError("The master file has no header row.")

    name_col = _pick_column(headers, MASTER_NAME_COLUMNS)
    if name_col is None:
        raise LoadError(
            "No database-name column found in the master file. Looked for: "
            + ", ".join(MASTER_NAME_COLUMNS)
            + f". Found: {', '.join(headers)}"
        )

    exists_col = _pick_column(headers, EXISTS_COLUMNS)
    scope_col = _pick_column(headers, IN_SCOPE_COLUMNS)

    rows: list[Row] = []
    exists_values: list[str] = []
    scope_values: list[str] = []
    for raw_row in raw:
        name = (raw_row.get(name_col) or "").strip()
        if not name:
            continue  # a blank line in a spreadsheet is not a database
        data = dict(raw_row)
        # Normalise the two flags into known keys so the comparison does not
        # have to care which spelling the file used.
        data["_exists_on_server"] = (
            "yes" if exists_col is None or is_yes(raw_row.get(exists_col)) else "no"
        )
        data["_is_in_scope"] = (
            "yes" if scope_col is None or is_yes(raw_row.get(scope_col)) else "no"
        )
        if exists_col is not None:
            exists_values.append(raw_row.get(exists_col, ""))
        if scope_col is not None:
            scope_values.append(raw_row.get(scope_col, ""))
        rows.append(Row(name=name, data=data))

    return MasterLoad(
        rows=rows,
        flags=(
            _tally_flag("is_in_scope", scope_col, scope_values),
            _tally_flag("exists_on_server", exists_col, exists_values),
        ),
    )


def target_headers(text: str) -> list[str]:
    """The column names in a target file, for the UI's key picker."""
    headers, _ = _read_csv(text)
    return headers


def guess_key_column(headers: Sequence[str]) -> str | None:
    """Best guess at which column holds the source database name.

    A *default for a human to confirm*, never a decision. See
    TARGET_NAME_COLUMNS for why this is not settled in code.
    """
    return _pick_column(headers, TARGET_NAME_COLUMNS)


def load_target_from_csv(text: str, key_column: str | None = None) -> list[Row]:
    """Read a target list (platform or vault) from a CSV.

    `key_column` names the column to match on. When omitted, the guess is used
    -- convenient for scripts and tests, but the UI always passes an explicit
    choice, because which field holds the source name is unconfirmed and a
    silent guess is exactly what this tool is built not to do.

    This is today's only implementation of the target-loader seam -- see
    `TargetLoader` below.
    """
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


#: The seam.
#:
#: Everything downstream of this takes a list of Rows and does not care where
#: they came from. Today the only implementation is `load_target_from_csv`,
#: reading a file somebody uploaded. When TASK-0054 lands, a second
#: implementation calls `GET /platform/riskdata/v1/exposures` and returns the
#: same Rows -- and nothing else in this file, in app.py, or in the tests has
#: to change. CONTRIBUTING.md's rule that only adapters/ knows Moody's exists
#: is the same discipline; this is where that boundary sits for this tool.
TargetLoader = Callable[[], list[Row]]


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _duplicates(rows: Iterable[Row]) -> dict[str, list[Row]]:
    """Group rows whose keys collide. Only groups of 2+ are returned."""
    by_key: dict[str, list[Row]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)
    return {key: group for key, group in by_key.items() if len(group) > 1}


def _classify_against(
    key: str,
    dupes: dict[str, list[Row]],
    by_key: dict[str, Row],
) -> tuple[str, Row | None]:
    """Look one key up in one target side.

    Returns ("duplicate" | "hit" | "miss", the row when there is one).
    Duplicates never count as a hit: a name appearing twice cannot be said to
    identify one database, and treating it as found is the silent failure the
    DUPLICATE outcome exists to prevent.
    """
    if key in dupes:
        return "duplicate", None
    hit = by_key.get(key)
    return ("hit", hit) if hit is not None else ("miss", None)


def reconcile(
    master: Sequence[Row],
    platform: Sequence[Row],
    vault: Sequence[Row] | None = None,
) -> Reconciliation:
    """Compare a master register against the platform and, optionally, the vault.

    Every master row produces exactly one result row; every target row with no
    master counterpart produces one more.

    The three-way part is the point. Per GLOSSARY.md, Data Bridge is the route
    in and Data Vault is the destination, and archiving is a separate explicit
    step -- so "on the platform" and "archived" are different claims, and only
    the second means migrated. Pass `vault=None` when no vault list is
    available and the tool reports IMPORTED instead of ARCHIVED, stating what
    it actually checked rather than implying the vault was confirmed.
    """
    vault_checked = vault is not None
    vault_rows: Sequence[Row] = vault or ()

    results: list[ResultRow] = []

    master_dupes = _duplicates(master)
    platform_dupes = _duplicates(platform)
    vault_dupes = _duplicates(vault_rows)

    platform_by_key = {r.key: r for r in platform if r.key not in platform_dupes}
    vault_by_key = {r.key: r for r in vault_rows if r.key not in vault_dupes}

    seen_platform: set[str] = set()
    seen_vault: set[str] = set()

    for row in master:
        if row.key in master_dupes:
            results.append(
                ResultRow(
                    outcome=Outcome.DUPLICATE,
                    name=row.name,
                    source="master",
                    note=(
                        f"appears {len(master_dupes[row.key])} times "
                        "in the master register"
                    ),
                    data=row.data,
                )
            )
            continue

        if not is_yes(row.data.get("_is_in_scope")):
            results.append(
                ResultRow(
                    Outcome.OUT_OF_SCOPE,
                    row.name,
                    "master",
                    "is_in_scope is not Y",
                    row.data,
                )
            )
            continue

        if not is_yes(row.data.get("_exists_on_server")):
            results.append(
                ResultRow(
                    Outcome.NOT_ON_SERVER,
                    row.name,
                    "master",
                    "in scope, but exists_on_server is not Y",
                    row.data,
                )
            )
            continue

        p_state, p_hit = _classify_against(row.key, platform_dupes, platform_by_key)
        v_state, v_hit = _classify_against(row.key, vault_dupes, vault_by_key)

        if p_state == "duplicate" or v_state == "duplicate":
            side = "platform" if p_state == "duplicate" else "vault"
            count = len((platform_dupes if side == "platform" else vault_dupes)[row.key])
            # Mark only the sides that actually carry this key, so a duplicate
            # on one side cannot suppress a genuine orphan on the other.
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

        if not vault_checked:
            # No vault list supplied: say what was actually checked.
            if p_hit is not None:
                results.append(
                    ResultRow(
                        Outcome.IMPORTED,
                        row.name,
                        "master",
                        "on the platform; no vault list supplied, so archiving is unconfirmed",
                        merged,
                    )
                )
            else:
                results.append(
                    ResultRow(
                        Outcome.MISSING,
                        row.name,
                        "master",
                        "not on the platform",
                        merged,
                    )
                )
            continue

        if v_hit is not None:
            results.append(
                ResultRow(
                    Outcome.ARCHIVED,
                    row.name,
                    "master",
                    ""
                    if p_hit is not None
                    else "in the vault, but not on the platform list",
                    merged,
                )
            )
        elif p_hit is not None:
            results.append(
                ResultRow(
                    Outcome.NOT_ARCHIVED,
                    row.name,
                    "master",
                    "imported to the platform but not archived in the vault",
                    merged,
                )
            )
        else:
            results.append(
                ResultRow(
                    Outcome.MISSING,
                    row.name,
                    "master",
                    "on neither the platform nor the vault",
                    merged,
                )
            )

    # Orphans and unmatched duplicates, per target side.
    for rows_, dupes_, seen_, side in (
        (platform, platform_dupes, seen_platform, "platform"),
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
            if row.key not in seen_:
                results.append(
                    ResultRow(
                        Outcome.ORPHANED,
                        row.name,
                        side,
                        f"on the {side} with no matching row in the master register",
                        row.data,
                    )
                )

    return Reconciliation(
        rows=tuple(results),
        master_count=len(master),
        target_count=len(platform),
        vault_count=len(vault_rows),
        vault_checked=vault_checked,
    )


def to_csv(reconciliation: Reconciliation) -> str:
    """Flatten a reconciliation to CSV, original columns preserved.

    Internal `_`-prefixed keys are dropped -- they are this tool's
    normalisation of the Y/N flags, not something the source file said.
    """
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
