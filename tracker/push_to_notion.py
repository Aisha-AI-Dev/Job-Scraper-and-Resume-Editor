"""
tracker/push_to_notion.py
--------------------------
Notion tracker integration for Aishani's job pipeline.

What this does:
  1. Reads scored_jobs_YYYY-MM-DD.csv from the scorer
  2. Filters to fit_score >= threshold (configurable, default 4.0)
  3. Checks each job against existing Notion pages by job_url
     (skips duplicates — safe to run multiple times)
  4. Pushes new jobs as Notion database pages, fully populated
  5. Logs a summary of what was pushed, skipped, and failed

Prerequisites:
  - NOTION_TOKEN in .env        (your integration secret, starts with ntn_...)
  - NOTION_DATABASE_ID in .env  (32-char ID from your database URL)
  - notion-client installed:    pip install notion-client

Notion DB property names must match EXACTLY what you created.
If you named a field differently, update the PROPERTY_MAP below.

Usage:
  python tracker/push_to_notion.py
  python tracker/push_to_notion.py --date 2025-04-03
  python tracker/push_to_notion.py --input data/scored_jobs_2025-04-03.csv
  python tracker/push_to_notion.py --min-score 6.0
  python tracker/push_to_notion.py --dry-run
"""

import os
import csv
import time
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from notion_client import Client
from notion_client.errors import APIResponseError
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
# BASE_DIR  = Path(__file__).resolve().parent.parent
# DATA_DIR  = BASE_DIR / "data"
# LOGS_DIR  = BASE_DIR / "logs"
# LOGS_DIR.mkdir(exist_ok=True)

BASE_DIR  = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE_DIR))
from paths import LOGS_DIR, scored_jobs_path, latest_raw_jobs
DATA_DIR  = BASE_DIR / "data"

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
today_str = date.today().isoformat()
log_path  = LOGS_DIR / f"notion_{today_str}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------

# Minimum fit_score to push to Notion.
# Jobs below this stay in scored_jobs_low.csv and never hit Notion.
DEFAULT_MIN_SCORE = 4.0

# Notion API rate limit is 3 requests/second.
# Sleep briefly between page creates to stay well under it.
RATE_LIMIT_SLEEP = 0.4   # seconds between API calls

# Max retries for transient Notion API errors
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# ----------------------------------------------------------------
# PROPERTY MAP
# These are the EXACT property names as they appear in your
# Notion database. If you named something differently when setting
# up the DB, change the value here (not the key).
#
# Key   = what the code uses internally
# Value = the property name in your Notion DB
# ----------------------------------------------------------------
PROPERTY_MAP = {
    "role":                  "Role",
    "company":               "Company",
    "fit_score":             "Fit score",
    "tier":                  "Tier",
    "role_family":           "Role family",
    "apply_recommendation":  "Apply recommendation",
    "overqualified":         "Overqualified",
    "sponsorship_status":    "Sponsorship status",
    "matched_skills":        "Matched skills",
    "missing_skills":        "Missing skills",
    "transferable_strengths":"Transferable strengths",
    "status":                "Status",
    "resume_version":        "Resume version",
    "date_applied":          "Date applied",
    "follow_up_date":        "Follow-up date",
    "notes":                 "Notes",
    "job_url":               "Job URL",
    "site":                  "Site",
    "date_posted":           "Date posted",
    "scored_at":             "Scored at",
    "description":           "Description",
}


# ----------------------------------------------------------------
# NOTION PROPERTY BUILDERS
# Each function returns a Notion property value dict for its type.
# ----------------------------------------------------------------

def prop_title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": _safe_str(value, 2000)}}]}

def prop_text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": _safe_str(value, 2000)}}]}

def prop_number(value) -> dict:
    try:
        return {"number": float(value)}
    except (TypeError, ValueError):
        return {"number": None}

def prop_select(value: str) -> dict:
    s = _safe_str(value, 100)
    if not s:
        return {"select": None}
    return {"select": {"name": s}}

def prop_checkbox(value) -> dict:
    if isinstance(value, bool):
        return {"checkbox": value}
    if isinstance(value, str):
        return {"checkbox": value.lower() in ("true", "1", "yes")}
    return {"checkbox": bool(value)}

def prop_url(value: str) -> dict:
    s = _safe_str(value, 2000)
    if not s or s in ("nan", "none", ""):
        return {"url": None}
    return {"url": s}

def prop_date(value: str) -> dict:
    """Parse a date string into Notion date format. Returns None if unparseable."""
    s = _safe_str(value)
    if not s or s in ("nan", "none", "nat", ""):
        return {"date": None}
    # Try common formats
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s[:19], fmt[:len(s[:19])])
            return {"date": {"start": dt.strftime("%Y-%m-%d")}}
        except ValueError:
            continue
    return {"date": None}

def _safe_str(value, max_len: int = 500) -> str:
    """Convert value to string, strip NaN/None, truncate to max_len."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return ""
    return s[:max_len]


# ----------------------------------------------------------------
# BUILD NOTION PAGE PROPERTIES FROM A CSV ROW
# ----------------------------------------------------------------

def build_properties(row: pd.Series) -> dict:
    """
    Map a scored_jobs.csv row to a Notion properties dict.
    Only includes fields that have actual values — Notion ignores
    empty fields gracefully but errors on malformed ones.
    """
    pm = PROPERTY_MAP

    props = {
        # Title field — required
        pm["role"]: prop_title(
            row.get("role_title") or row.get("title") or "Unknown Role"
        ),

        # Text fields
        pm["company"]:               prop_text(row.get("company", "")),
        pm["matched_skills"]:        prop_text(row.get("matched_skills", "")),
        pm["missing_skills"]:        prop_text(row.get("missing_skills", "")),
        pm["transferable_strengths"]:prop_text(row.get("transferable_strengths", "")),
        pm["site"]:                  prop_text(row.get("site", "")),
        pm["date_posted"]:           prop_text(row.get("date_posted", "")),
        pm["scored_at"]:             prop_text(row.get("_scored_at", "")),
        pm["notes"]:                 prop_text(""),   # blank — you fill this in Notion

        # Number
        pm["fit_score"]: prop_number(row.get("fit_score")),

        # Select fields
        pm["tier"]:                 prop_select(row.get("tier", "")),
        pm["role_family"]:          prop_select(row.get("role_family", "")),
        pm["apply_recommendation"]: prop_select(row.get("apply_recommendation", "")),
        pm["sponsorship_status"]:   prop_select(row.get("sponsorship_status", "")),
        pm["status"]:               prop_select("New"),   # always starts as New

        # Checkbox
        pm["overqualified"]: prop_checkbox(row.get("overqualified_flag", False)),

        # URL
        pm["job_url"]: prop_url(row.get("job_url", "")),

        # Dates — left blank for you to fill in when you apply
        pm["date_applied"]:   prop_date(""),
        pm["follow_up_date"]: prop_date(""),
        pm["resume_version"]: prop_text(""),   # fill in when you apply
        pm["description"]: prop_text(str(row.get("description", ""))[:2000]),
    }

    return props


def build_page_body(row: pd.Series, database_id: str) -> dict:
    """Build the full Notion page creation payload."""
    return {
        "parent": {"database_id": database_id},
        "properties": build_properties(row),
    }


# ----------------------------------------------------------------
# DEDUPLICATION — check existing Notion pages by job_url
# ----------------------------------------------------------------

def fetch_existing_urls(client: Client, database_id: str) -> set:
    """
    Query all existing pages in the Notion DB and return a set of
    job_url values already present. Uses pagination to handle large DBs.

    Compatible with notion-client 3.x which removed databases.query()
    in favour of the raw request() method.
    """
    existing_urls = set()
    url_prop = PROPERTY_MAP["job_url"]

    log.info("Fetching existing job URLs from Notion DB...")
    cursor = None

    while True:
        try:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            response = client.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body=body,
            )
        except APIResponseError as e:
            log.error(f"Failed to query Notion DB: {str(e)}")
            break
        except Exception as e:
            log.error(f"Failed to query Notion DB: {str(e)}")
            break

        for page in response.get("results", []):
            url_val = (
                page.get("properties", {})
                    .get(url_prop, {})
                    .get("url")
            )
            if url_val:
                existing_urls.add(url_val.strip())

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    log.info(f"Found {len(existing_urls):,} existing jobs in Notion")
    return existing_urls


# ----------------------------------------------------------------
# PAGE CREATION WITH RETRY
# ----------------------------------------------------------------

def create_page_with_retry(
    client: Client,
    page_body: dict,
    job_label: str,
) -> bool:
    """
    Create a Notion page with exponential backoff retry.
    Returns True on success, False on permanent failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client.request(
                path="pages",
                method="POST",
                body=page_body,
            )
            return True
        except APIResponseError as e:
            # 409 = conflict (rare with Notion), 400 = bad request (don't retry)
            if e.status in (400, 409):
                log.error(f"  [{job_label}] Permanent error ({e.status}): {str(e)}")
                return False
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            log.warning(
                f"  [{job_label}] Attempt {attempt}/{MAX_RETRIES} failed "
                f"({e.status}): {str(e)}. Retrying in {delay:.0f}s..."
            )
            time.sleep(delay)
        except Exception as e:
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            log.warning(
                f"  [{job_label}] Attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                f"Retrying in {delay:.0f}s..."
            )
            time.sleep(delay)

    log.error(f"  [{job_label}] Failed after {MAX_RETRIES} attempts. Skipping.")
    return False


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    input_path: Path | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    dry_run: bool = False,
    target_date: str | None = None,
) -> None:

    log.info("=" * 60)
    log.info(f"Notion pusher starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # 1. Resolve input file
    if input_path is None:
        date_str   = target_date or today_str
        # input_path = DATA_DIR / f"scored_jobs_{date_str}.csv"
        input_path = scored_jobs_path(date_str)
        if not input_path.exists():
            input_path = scored_jobs_path(date_str)  # backward compat

    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        log.error("Run the scorer first: python scorer/score_jobs.py")
        return

    # 2. Load scored jobs
    df = pd.read_csv(input_path, dtype=str)
    # Join descriptions from raw_jobs (scorer strips them out)
    if "description" not in df.columns or df["description"].isna().all():
        raw_path = latest_raw_jobs()
        if raw_path and raw_path.exists():
            log.info(f"Loading descriptions from {raw_path.name}...")
            df_raw = pd.read_csv(raw_path, usecols=["job_url", "description"], dtype=str)
            df = df.merge(df_raw, on="job_url", how="left", suffixes=("", "_raw"))
            if "description_raw" in df.columns:
                df["description"] = df["description_raw"].fillna("")
                df.drop(columns=["description_raw"], inplace=True)
            log.info("Descriptions joined")
        else:
            log.warning("No raw_jobs CSV found — descriptions will be empty")
            
    log.info(f"Loaded {len(df):,} scored jobs from {input_path}")

    # 3. Filter by score threshold
    df["fit_score"] = pd.to_numeric(df.get("fit_score", pd.Series()), errors="coerce")
    df_eligible = df[df["fit_score"] >= min_score].copy()
    df_below    = df[df["fit_score"] < min_score]

    log.info(
        f"Score filter (>= {min_score}): "
        f"{len(df_eligible):,} eligible | {len(df_below):,} below threshold"
    )

    if df_eligible.empty:
        log.info("No eligible jobs to push. Exiting.")
        return

    if dry_run:
        log.info(f"DRY RUN — would push {len(df_eligible):,} jobs to Notion:")
        for _, row in df_eligible.iterrows():
            company = row.get("company", "?")
            title   = row.get("role_title") or row.get("title", "?")
            score   = row.get("fit_score", "?")
            rec     = row.get("apply_recommendation", "?")
            log.info(f"  [{score}] {title} @ {company} — {rec}")
        return

    # 4. Init Notion client
    notion_token = os.getenv("NOTION_TOKEN")
    database_id  = os.getenv("NOTION_DATABASE_ID")

    if not notion_token:
        raise EnvironmentError("NOTION_TOKEN not set in .env file")
    if not database_id:
        raise EnvironmentError("NOTION_DATABASE_ID not set in .env file")

    client = Client(auth=notion_token)

    # 5. Fetch existing URLs for deduplication
    existing_urls = fetch_existing_urls(client, database_id)

    # 6. Push new jobs
    pushed  = 0
    skipped = 0
    failed  = 0

    for _, row in df_eligible.iterrows():
        job_url = _safe_str(row.get("job_url", ""))
        company = _safe_str(row.get("company", "Unknown"))
        title   = _safe_str(row.get("role_title") or row.get("title", "Unknown"))
        score   = row.get("fit_score", "?")
        job_label = f"{title} @ {company} [{score}]"

        # Dedup check
        if job_url and job_url in existing_urls:
            log.info(f"  SKIP (already in Notion): {job_label}")
            skipped += 1
            continue

        # Build page
        try:
            page_body = build_page_body(row, database_id)
        except Exception as e:
            log.error(f"  [{job_label}] Failed to build page body: {e}")
            failed += 1
            continue

        # Push to Notion
        success = create_page_with_retry(client, page_body, job_label)

        if success:
            log.info(f"  PUSHED: {job_label}")
            pushed += 1
            if job_url:
                existing_urls.add(job_url)   # prevent same-run duplicates
        else:
            failed += 1

        # Rate limit courtesy sleep
        time.sleep(RATE_LIMIT_SLEEP)

    # 7. Summary
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info(f"  Total eligible:  {len(df_eligible):,}")
    log.info(f"  Pushed:          {pushed:,}")
    log.info(f"  Skipped (dupe):  {skipped:,}")
    log.info(f"  Failed:          {failed:,}")
    log.info(f"  Below threshold: {len(df_below):,}")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Push scored jobs to Notion database"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to scored_jobs CSV (default: data/scored_jobs_TODAY.csv)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date string YYYY-MM-DD to load scored_jobs for (default: today)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help=f"Minimum fit_score to push to Notion (default: {DEFAULT_MIN_SCORE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be pushed without making any Notion API calls",
    )
    args = parser.parse_args()
    main(
        input_path=args.input,
        min_score=args.min_score,
        dry_run=args.dry_run,
        target_date=args.date,
    )