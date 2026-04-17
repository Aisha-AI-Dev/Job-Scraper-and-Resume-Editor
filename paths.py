"""
paths.py
--------
Single source of truth for all file paths in the job pipeline.

Import from here instead of constructing paths inline in each module.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# Sub-directories
RAW_DIR         = DATA_DIR / "raw"
SCORED_DIR      = DATA_DIR / "scored"
TAILORED_DIR    = DATA_DIR / "tailored"
BLACKLISTED_DIR = DATA_DIR

# Persistent state file
SEEN_FILE = DATA_DIR / "seen_ids.txt"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)
SCORED_DIR.mkdir(exist_ok=True)
TAILORED_DIR.mkdir(exist_ok=True)


# ----------------------------------------------------------------
# PATH HELPERS — accept a date string (YYYY-MM-DD) and return a Path
# ----------------------------------------------------------------

def raw_jobs_path(date_str: str) -> Path:
    return RAW_DIR / f"raw_jobs_{date_str}.csv"

def scored_jobs_path(date_str: str) -> Path:
    return SCORED_DIR / f"scored_jobs_{date_str}.csv"

def scored_jobs_low_path(date_str: str) -> Path:
    return SCORED_DIR / f"scored_jobs_low_{date_str}.csv"

def scored_jobs_errors_path(date_str: str) -> Path:
    return SCORED_DIR / f"scored_jobs_errors_{date_str}.csv"

def blacklisted_path(date_str: str) -> Path:
    return BLACKLISTED_DIR / f"blacklisted_{date_str}.csv"


# ----------------------------------------------------------------
# LATEST FILE HELPERS
# ----------------------------------------------------------------

def latest_raw_jobs() -> Path | None:
    """Return the most recently modified raw_jobs_*.csv in RAW_DIR, or None."""
    candidates = sorted(RAW_DIR.glob("raw_jobs_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None
