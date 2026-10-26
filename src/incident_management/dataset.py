from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The Kaggle archive is committed as-is; pandas reads the single CSV inside the zip directly.
DATASET_PATH = PROJECT_ROOT / "data" / "incident_event_log.csv.zip"

TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"
CSV_SEPARATOR = ";"

REQUIRED_COLUMNS = {
    "case_id",
    "variant",
    "priority",
    "reporter",
    "timestamp",
    "event",
    "issue_type",
    "resolver",
    "report_channel",
    "short_description",
    "customer_satisfaction",
}

# Process-mining aliases layered on top of the source columns.
CASE_COLUMN = "case_id"
ACTIVITY_COLUMN = "event"
TIMESTAMP_COLUMN = "timestamp"

DATE_COLUMNS = ("timestamp",)
NUMERIC_COLUMNS = ("customer_satisfaction",)

# The source log carries no running reassignment/reopen counters; both are derived here from the activity
# sequence so query.py's counter-distribution tool needs no schema-specific logic.
# A ticket's very first assignment is not a *re*-assignment, so only a handoff to a
# support level/agent other than the first one counts.
REASSIGNMENT_EVENTS = frozenset(
    {
        "Level 1 escalates to level 2 support",
        "Ticket escalated to level 2 support",
        "Ticket assigned to level 2 support",
        "Level 2 escalates to level 3 support",
        "Ticket rejected by level 1 support",
        "Level 2 support rejects the ticket",
    }
)
REOPEN_EVENT = "Ticket reopened by customer"


def load_event_log(path: str | Path = DATASET_PATH) -> pd.DataFrame:
    """Load the incident event log with process-mining-friendly column names."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Dataset not found: {source}")

    frame = pd.read_csv(source, sep=CSV_SEPARATOR, low_memory=False)
    frame.columns = [column.strip().lower().replace(" ", "_") for column in frame.columns]
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")

    # A handful of malformed rows carry a blank activity; they cannot be placed in
    # a case's process flow, so they are dropped rather than parsed as an event.
    frame = frame[frame["event"].astype(str).str.strip() != ""].copy()

    for column in DATE_COLUMNS:
        frame[column] = pd.to_datetime(frame[column], format=TIMESTAMP_FORMAT, errors="coerce")
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")

    frame = frame.sort_values(["case_id", "timestamp"], kind="stable").reset_index(drop=True)

    is_reassignment = frame["event"].isin(REASSIGNMENT_EVENTS)
    is_reopen = frame["event"] == REOPEN_EVENT
    frame["reassignment_count"] = is_reassignment.groupby(frame["case_id"]).cumsum().astype("Int64")
    frame["reopen_count"] = is_reopen.groupby(frame["case_id"]).cumsum().astype("Int64")

    return frame


def to_pm4py(frame: pd.DataFrame):
    """Convert a loaded frame to a PM4Py event log."""
    import pm4py

    return pm4py.convert_to_event_log(
        frame.rename(
            columns={
                CASE_COLUMN: "case:concept:name",
                ACTIVITY_COLUMN: "concept:name",
                TIMESTAMP_COLUMN: "time:timestamp",
            }
        )
    )
