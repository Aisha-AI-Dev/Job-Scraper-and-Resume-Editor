"""
scrapers/jobspy_scraper.py
--------------------------
Job discovery module for Aishani's job application pipeline.

What this does:
  1. Scrapes LinkedIn + Indeed for target roles using JobSpy
  2. Pre-filters out hard disqualifiers (no LLM call needed)
  3. Deduplicates against previously seen jobs
  4. Outputs a clean CSV of NEW jobs ready for scoring

Usage:
  python scrapers/jobspy_scraper.py
  python scrapers/jobspy_scraper.py --results 50 --hours 48

Output:
  data/raw_jobs_YYYY-MM-DD.csv   — new jobs found today
  data/seen_ids.txt              — cumulative seen job IDs (updated)
  logs/scraper_YYYY-MM-DD.log    — run log
"""

import os
import csv
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from jobspy import scrape_jobs
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = BASE_DIR / "data"
LOGS_DIR   = BASE_DIR / "logs"
SEEN_FILE  = DATA_DIR / "seen_ids.txt"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
today_str = date.today().isoformat()
log_path  = LOGS_DIR / f"scraper_{today_str}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(),          # also print to terminal
    ],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# CONFIG — edit these to change search behaviour
# ----------------------------------------------------------------

# Search terms — each gets its own JobSpy call so results don't
# bleed together. JobSpy searches title + description on Indeed,
# title-only on LinkedIn.
SEARCH_TERMS = [
    "ML Engineer",
    "AI Engineer",
    "Machine Learning Engineer",
    "Data Scientist",
    "Research Scientist machine learning",
    "Data Engineer",
    "MLOps Engineer",
    "Applied Scientist",
    "Developer Advocate AI",
    "Forward Deployed Engineer",
    "Data Analyst",
]

# Locations — scrape remote + major US hubs.
# LinkedIn ignores distance and searches globally; Indeed uses it.
LOCATIONS = [
    "Remote",
    "United States",
    "San Francisco, CA",
    "New York, NY",
    "Seattle, WA",
    "Chicago, IL",
    "Boston, MA",
    "Austin, TX",
    "Cleveland, OH",
]

# Job boards — Indeed is most permissive; LinkedIn rate-limits
# around page 10. Zip recruiter is a decent supplement.
SITES = ["indeed", "linkedin", "zip_recruiter"]

# How many results per (search_term × site) call
RESULTS_PER_SEARCH = 25

# Only pull jobs posted in the last N hours
HOURS_OLD = 24

# Columns to keep in the output CSV
KEEP_COLS = [
    "id",          # JobSpy's internal job ID
    "site",        # which board it came from
    "title",
    "company",
    "location",
    "city",
    "state",
    "is_remote",
    "job_type",
    "date_posted",
    "job_url",
    "description",
    "min_amount",
    "max_amount",
    "interval",
]

# ----------------------------------------------------------------
# BLACKLIST FILTERS — hard disqualifiers, checked before scoring
# These mirror the hard constraints in target_roles.txt.
# All checks are case-insensitive substring matches on the
# combined job title + description text.
# ----------------------------------------------------------------
BLACKLIST_PHRASES = [
    # experience requirements
    "10+ years",
    "10 or more years",
    "12+ years",
    "15+ years",
    "minimum 10 years",
    "at least 10 years",
    # clearance — require explicit job-level language, not incidental mentions
    # "security clearance" alone is too broad (catches boilerplate like
    # "background checks may vary by clearance level held")
    "clearance required",
    "clearance is required",
    "must have an active clearance",
    "must hold a clearance",
    "active secret clearance",
    "active top secret",
    "ts/sci",
    "ts/sci required",
    "requires clearance",
    "obtain a security clearance",
    "must be clearance eligible",
    # explicit no sponsorship — keep these tight, silence ≠ rejection
    "no sponsorship",
    "will not sponsor",
    "cannot sponsor",
    "must be authorized to work without sponsorship",
    "sponsorship not available",
    "sponsorship is not available",
    "u.s. citizen only",
    "us citizen only",
    "must be a u.s. citizen",
    "requires u.s. citizenship",
    # seniority — only catch explicit role-requirement language,
    # not incidental mentions ("presenting to VP-level stakeholders"
    # is fine — "this is a VP-level role" is not)
    "c-suite only",
    "vice president role",
    "vp-level role",
    "this is a vp position",
]

# Title-level blacklist — if the job TITLE contains any of these,
# skip it without even checking the description.
# Titles are short and unambiguous — substring matching is safe here.
TITLE_BLACKLIST = [
    "principal engineer",    # usually 8-12 yrs
    "staff engineer",
    "distinguished engineer",
    "director of",
    "vp of",
    "vice president of",
    "head of",
    "chief data",
    "chief ai",
    "chief machine",
    "chief analytics",
    "c.t.o",
    "cto,",                  # comma prevents matching "ctor" etc.
    "intern,",               # comma prevents "internal", "international"
    "internship",
    "co-op",
    "coop",
]

# ----------------------------------------------------------------
# CORE FUNCTIONS
# ----------------------------------------------------------------

def load_seen_ids() -> set:
    """Load the set of job IDs already processed in previous runs."""
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE, "r") as f:
        ids = {line.strip() for line in f if line.strip()}
    log.info(f"Loaded {len(ids):,} previously seen job IDs")
    return ids


def save_seen_ids(seen: set) -> None:
    """Append new IDs to the seen file (never overwrites existing)."""
    with open(SEEN_FILE, "a") as f:
        for job_id in sorted(seen):
            f.write(job_id + "\n")


def make_job_key(row: pd.Series) -> str:
    """
    Stable dedup key = site + job_id.
    Falls back to site + company + title if job_id is missing.
    """
    job_id = str(row.get("id", "")).strip()
    site   = str(row.get("site", "")).strip().lower()
    if job_id and job_id not in ("nan", "none", ""):
        return f"{site}::{job_id}"
    # fallback: normalise company + title
    company = str(row.get("company", "")).strip().lower()
    title   = str(row.get("title",   "")).strip().lower()
    return f"{site}::{company}::{title}"


def is_blacklisted(row: pd.Series) -> tuple[bool, str]:
    """
    Returns (True, reason) if the job matches any hard disqualifier,
    (False, "") otherwise.
    """
    title = str(row.get("title", "")).lower()
    desc  = str(row.get("description", "")).lower()
    combined = title + " " + desc

    # Title-level check first (cheap)
    for phrase in TITLE_BLACKLIST:
        if phrase.lower() in title:
            return True, f"title contains '{phrase}'"

    # Full-text check
    for phrase in BLACKLIST_PHRASES:
        if phrase.lower() in combined:
            return True, f"text contains '{phrase}'"

    return False, ""


def scrape_all(results_per_search: int, hours_old: int) -> pd.DataFrame:
    """
    Run JobSpy for every (search_term × location) pair and return
    a combined DataFrame. Handles per-call errors gracefully so one
    failed query doesn't abort the whole run.
    """
    all_frames = []
    total_calls = len(SEARCH_TERMS) * len(LOCATIONS)
    call_num = 0

    for term in SEARCH_TERMS:
        for location in LOCATIONS:
            call_num += 1
            log.info(f"[{call_num}/{total_calls}] Scraping: '{term}' @ '{location}'")
            try:
                df = scrape_jobs(
                    site_name=SITES,
                    search_term=term,
                    location=location,
                    results_wanted=results_per_search,
                    hours_old=hours_old,
                    country_indeed="USA",
                    job_type="fulltime",
                    linkedin_fetch_description=True,  # needed for full desc
                    description_format="markdown",
                )
                if df is not None and not df.empty:
                    df["_search_term"] = term      # track which query found it
                    all_frames.append(df)
                    log.info(f"  → {len(df)} results")
                else:
                    log.info("  → 0 results")

            except Exception as e:
                log.warning(f"  → Scrape failed: {e}")
                continue

    if not all_frames:
        log.warning("No results returned from any search.")
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)
    log.info(f"Total raw results (before dedup): {len(combined):,}")
    return combined


def clean_and_filter(
    df: pd.DataFrame,
    seen_ids: set,
) -> tuple[pd.DataFrame, pd.DataFrame, set]:
    """
    1. Normalise columns
    2. Drop dupes within this batch (same job from multiple queries)
    3. Drop jobs already seen in previous runs
    4. Apply blacklist pre-filters
    5. Return (new_jobs_df, filtered_out_df, new_seen_ids)
    """
    if df.empty:
        return df, df, set()

    # --- Normalise columns ---
    # JobSpy returns different column sets depending on what it finds.
    # Add missing columns as None so downstream code doesn't break.
    for col in KEEP_COLS:
        if col not in df.columns:
            df[col] = None

    # Build dedup key column
    df["_key"] = df.apply(make_job_key, axis=1)

    # --- Drop within-batch duplicates ---
    before = len(df)
    df = df.drop_duplicates(subset="_key", keep="first")
    log.info(f"Within-batch dedup: {before:,} → {len(df):,} ({before - len(df):,} removed)")

    # --- Drop already-seen jobs ---
    mask_new = ~df["_key"].isin(seen_ids)
    df_new   = df[mask_new].copy()
    df_old   = df[~mask_new].copy()
    log.info(f"Already-seen filter: {len(df):,} → {len(df_new):,} new ({len(df_old):,} previously seen)")

    # --- Apply blacklist ---
    blacklist_rows  = []
    keep_rows       = []

    for _, row in df_new.iterrows():
        flagged, reason = is_blacklisted(row)
        if flagged:
            row_dict = row.to_dict()
            row_dict["_blacklist_reason"] = reason
            blacklist_rows.append(row_dict)
        else:
            keep_rows.append(row.to_dict())

    df_keep   = pd.DataFrame(keep_rows)   if keep_rows   else pd.DataFrame()
    df_filter = pd.DataFrame(blacklist_rows) if blacklist_rows else pd.DataFrame()

    log.info(
        f"Blacklist filter: {len(df_new):,} → {len(df_keep):,} kept "
        f"({len(df_filter):,} disqualified)"
    )

    # Collect the keys of all new jobs (kept + blacklisted) to mark as seen
    new_seen = set(df_new["_key"].tolist())

    return df_keep, df_filter, new_seen


def save_output(df: pd.DataFrame, df_filtered: pd.DataFrame) -> Path | None:
    """
    Write the clean jobs CSV to data/raw_jobs_YYYY-MM-DD.csv.
    Also writes a separate blacklisted CSV for auditing.
    Returns the path to the main output file, or None if empty.
    """
    if df.empty:
        log.info("No new jobs to save.")
        return None

    # Keep only the columns we care about (plus internal tracking cols)
    save_cols = [c for c in KEEP_COLS + ["_search_term", "_key"] if c in df.columns]
    df_out = df[save_cols].copy()

    # Trim description to 8,000 chars — enough context for the scorer,
    # won't blow out the CSV or the API context window
    if "description" in df_out.columns:
        df_out["description"] = df_out["description"].str[:8000]

    output_path = DATA_DIR / f"raw_jobs_{today_str}.csv"
    df_out.to_csv(
        output_path,
        index=False,
        quoting=csv.QUOTE_ALL,
        escapechar="\\",
    )
    log.info(f"Saved {len(df_out):,} new jobs → {output_path}")

    # Save blacklisted jobs separately for auditing
    if not df_filtered.empty:
        blacklist_path = DATA_DIR / f"blacklisted_{today_str}.csv"
        bl_cols = [c for c in ["title", "company", "location", "job_url", "_blacklist_reason"]
                   if c in df_filtered.columns]
        df_filtered[bl_cols].to_csv(blacklist_path, index=False)
        log.info(f"Saved {len(df_filtered):,} blacklisted jobs → {blacklist_path}")

    return output_path


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(results_per_search: int = RESULTS_PER_SEARCH, hours_old: int = HOURS_OLD):
    log.info("=" * 60)
    log.info(f"Job scraper starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Search terms: {len(SEARCH_TERMS)} | Locations: {len(LOCATIONS)} | "
             f"Results/search: {results_per_search} | Hours old: {hours_old}")
    log.info("=" * 60)

    # 1. Load seen IDs
    seen_ids = load_seen_ids()

    # 2. Scrape
    df_raw = scrape_all(results_per_search, hours_old)

    if df_raw.empty:
        log.info("Nothing scraped. Exiting.")
        return

    # 3. Clean, dedup, pre-filter
    df_new, df_filtered, new_seen_ids = clean_and_filter(df_raw, seen_ids)

    # 4. Save output
    output_path = save_output(df_new, df_filtered)

    # 5. Update seen IDs file (mark everything we looked at, even blacklisted)
    save_seen_ids(new_seen_ids)
    log.info(f"Updated seen_ids.txt (+{len(new_seen_ids):,} new entries)")

    # 6. Summary
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info(f"  Raw scraped:     {len(df_raw):,}")
    log.info(f"  New (unseen):    {len(df_new) + len(df_filtered):,}")
    log.info(f"  Pre-filtered:    {len(df_filtered):,}  (blacklisted, not scored)")
    log.info(f"  Ready to score:  {len(df_new):,}")
    if output_path:
        log.info(f"  Output file:     {output_path}")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job scraper for Aishani's pipeline")
    parser.add_argument(
        "--results",
        type=int,
        default=RESULTS_PER_SEARCH,
        help=f"Results per search term per site (default: {RESULTS_PER_SEARCH})",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=HOURS_OLD,
        help=f"Only include jobs posted within this many hours (default: {HOURS_OLD})",
    )
    args = parser.parse_args()
    main(results_per_search=args.results, hours_old=args.hours)