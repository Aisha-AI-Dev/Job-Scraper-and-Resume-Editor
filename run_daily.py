"""
run_daily.py
------------
Main orchestrator for Aishani's job application pipeline.

Runs the full discovery-to-tracker pipeline in one command:
  scrape → deduplicate → score (batch) → filter → push to Notion

Meant to be run once daily, either manually or via cron.
All output is logged to logs/YYYY-MM-DD.log AND printed to terminal.

Usage:
  python run_daily.py                    # full pipeline, today's date
  python run_daily.py --skip-scrape      # score + push only (use existing CSV)
  python run_daily.py --skip-score       # scrape + push only (use existing scores)
  python run_daily.py --skip-notion      # scrape + score only (no Notion push)
  python run_daily.py --dry-run          # full pipeline, no API calls or Notion writes
  python run_daily.py --min-score 6.0    # override Notion push threshold

Cron setup (runs at 8am daily):
  crontab -e
  0 8 * * * /path/to/venv/bin/python /path/to/job_pipeline/run_daily.py

  Note: cron doesn't inherit your shell environment, so use full paths.
  To find them:
    which python  →  use this as the python path
    pwd           →  use this as the base path
"""

import os
import sys
import time
import logging
import argparse
import importlib.util
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------
# LOGGING — shared log file for the full pipeline run
# ----------------------------------------------------------------
today_str = date.today().isoformat()
log_path  = LOGS_DIR / f"{today_str}.log"

# Root logger — all modules that use logging.getLogger() will
# write here too, since they're imported after this is configured.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("run_daily")


# ----------------------------------------------------------------
# DYNAMIC MODULE LOADER
# Imports pipeline modules by file path so run_daily.py works
# regardless of whether the project is installed as a package.
# ----------------------------------------------------------------

def load_module(module_name: str, file_path: Path):
    spec   = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------
# PIPELINE STEP WRAPPER
# Wraps each step with timing, error handling, and clear logging.
# ----------------------------------------------------------------

class PipelineStep:
    def __init__(self, name: str):
        self.name    = name
        self.success = False
        self.elapsed = 0.0
        self.detail  = ""

    def __enter__(self):
        log.info("")
        log.info("─" * 60)
        log.info(f"STEP: {self.name}")
        log.info("─" * 60)
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed = time.time() - self._start
        if exc_type is None:
            self.success = True
            log.info(f"✓ {self.name} completed in {self.elapsed:.1f}s")
        else:
            self.success = False
            log.error(f"✗ {self.name} FAILED after {self.elapsed:.1f}s: {exc_val}")
        return False   # don't suppress exceptions — let main() handle them


# ----------------------------------------------------------------
# ENVIRONMENT CHECK
# ----------------------------------------------------------------

def check_environment() -> list[str]:
    """
    Verify required environment variables and files exist.
    Returns a list of issues (empty = all good).
    """
    issues = []

    # Required env vars
    required_env = {
        "ANTHROPIC_API_KEY": "needed for scoring",
        "NOTION_TOKEN":      "needed for Notion push",
        "NOTION_DATABASE_ID":"needed for Notion push",
    }
    for var, reason in required_env.items():
        if not os.getenv(var):
            issues.append(f"Missing env var: {var} ({reason})")

    # Required prompt files
    prompts_dir = BASE_DIR / "prompts"
    required_files = [
        prompts_dir / "profile.txt",
        prompts_dir / "target_roles.txt",
        prompts_dir / "scoring_system_prompt.txt",
    ]
    for f in required_files:
        if not f.exists():
            issues.append(f"Missing file: {f}")

    # Required pipeline scripts
    required_scripts = [
        BASE_DIR / "scrapers" / "jobspy_scraper.py",
        BASE_DIR / "scorer"   / "score_jobs.py",
        BASE_DIR / "tracker"  / "push_to_notion.py",
    ]
    for s in required_scripts:
        if not s.exists():
            issues.append(f"Missing script: {s}")

    return issues


# ----------------------------------------------------------------
# STEP 1 — SCRAPE
# ----------------------------------------------------------------

def run_scrape(dry_run: bool) -> Path | None:
    """
    Run the job scraper. Returns path to the output CSV, or None on failure.
    """
    scraper_path = BASE_DIR / "scrapers" / "jobspy_scraper.py"
    scraper = load_module("jobspy_scraper", scraper_path)

    if dry_run:
        log.info("DRY RUN — skipping actual scrape. No JobSpy calls made.")
        # Return path that would exist if scrape had run
        return DATA_DIR / f"raw_jobs_{today_str}.csv"

    scraper.main()

    output_path = DATA_DIR / f"raw_jobs_{today_str}.csv"
    if not output_path.exists():
        log.warning("Scraper ran but no output CSV found. Possibly zero new jobs.")
        return None

    import pandas as pd
    df = pd.read_csv(output_path)
    log.info(f"Scrape output: {len(df):,} new jobs → {output_path}")
    return output_path


# ----------------------------------------------------------------
# STEP 2 — SCORE
# ----------------------------------------------------------------

def run_score(raw_csv: Path | None, dry_run: bool) -> Path | None:
    """
    Run the scorer on the raw CSV. Returns path to scored CSV, or None on failure.
    """
    scorer_path = BASE_DIR / "scorer" / "score_jobs.py"
    scorer = load_module("score_jobs", scorer_path)

    scored_path = DATA_DIR / f"scored_jobs_{today_str}.csv"

    if raw_csv is None or not raw_csv.exists():
        log.warning("No raw jobs CSV to score. Skipping scoring step.")
        return None

    import pandas as pd
    df_raw = pd.read_csv(raw_csv)
    if df_raw.empty:
        log.info("Raw jobs CSV is empty. Nothing to score.")
        return None

    log.info(f"Scoring {len(df_raw):,} jobs from {raw_csv}...")
    scorer.main(input_path=raw_csv, dry_run=dry_run)

    if dry_run:
        log.info("DRY RUN — no actual scoring API calls made.")
        return scored_path   # might not exist but that's fine in dry-run

    if not scored_path.exists():
        log.warning("Scorer ran but no scored_jobs CSV found.")
        return None

    df_scored = pd.read_csv(scored_path)
    log.info(f"Score output: {len(df_scored):,} jobs scored → {scored_path}")
    return scored_path


# ----------------------------------------------------------------
# STEP 3 — PUSH TO NOTION
# ----------------------------------------------------------------

def run_notion(scored_csv: Path | None,
               min_score: float,
               dry_run: bool) -> None:
    """
    Push scored jobs meeting the threshold to Notion.
    """
    notion_path = BASE_DIR / "tracker" / "push_to_notion.py"
    pusher = load_module("push_to_notion", notion_path)

    if scored_csv is None or not scored_csv.exists():
        log.warning("No scored jobs CSV to push. Skipping Notion step.")
        return

    import pandas as pd
    df = pd.read_csv(scored_csv)
    eligible = df[
        pd.to_numeric(df.get("fit_score", ""), errors="coerce") >= min_score
    ]
    log.info(
        f"Notion push: {len(eligible):,} jobs eligible "
        f"(fit_score >= {min_score}) out of {len(df):,} scored"
    )

    pusher.main(
        input_path=scored_csv,
        min_score=min_score,
        dry_run=dry_run,
    )


# ----------------------------------------------------------------
# PIPELINE SUMMARY
# ----------------------------------------------------------------

def print_summary(
    steps: list[PipelineStep],
    raw_csv: Path | None,
    scored_csv: Path | None,
    start_time: float,
    dry_run: bool,
) -> None:
    total_elapsed = time.time() - start_time

    log.info("")
    log.info("=" * 60)
    log.info("DAILY PIPELINE SUMMARY")
    log.info(f"Date:      {today_str}")
    log.info(f"Dry run:   {dry_run}")
    log.info(f"Total time: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
    log.info("")

    for step in steps:
        status = "✓" if step.success else "✗"
        log.info(f"  {status}  {step.name:<30} {step.elapsed:.1f}s")

    if raw_csv and raw_csv.exists():
        try:
            import pandas as pd
            n = len(pd.read_csv(raw_csv))
            log.info(f"\n  Raw jobs scraped:   {n:,}")
        except Exception:
            pass

    if scored_csv and scored_csv.exists():
        try:
            import pandas as pd
            df = pd.read_csv(scored_csv)
            if "fit_score" in df.columns:
                scores = pd.to_numeric(df["fit_score"], errors="coerce").dropna()
                yes = (df.get("apply_recommendation", "") == "yes").sum()
                mr  = (df.get("apply_recommendation", "") == "manual_review").sum()
                log.info(f"  Jobs scored:        {len(df):,}")
                log.info(f"  Avg fit score:      {scores.mean():.1f}")
                log.info(f"  → yes:              {yes}")
                log.info(f"  → manual_review:    {mr}")
        except Exception:
            pass

    log.info("")
    log.info(f"  Log file: {log_path}")
    if scored_csv:
        log.info(f"  Scores:   {scored_csv}")
    log.info("=" * 60)


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    skip_scrape: bool = False,
    skip_score:  bool = False,
    skip_notion: bool = False,
    dry_run:     bool = False,
    min_score:   float = 4.0,
) -> None:

    pipeline_start = time.time()

    log.info("=" * 60)
    log.info("JOB PIPELINE — DAILY RUN")
    log.info(f"Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Dry run:  {dry_run}")
    log.info(f"Min score for Notion: {min_score}")
    log.info("=" * 60)

    # 0. Environment check
    issues = check_environment()
    if issues:
        log.error("Environment check failed:")
        for issue in issues:
            log.error(f"  - {issue}")
        if not dry_run:
            log.error("Fix the above issues and re-run.")
            sys.exit(1)
        else:
            log.warning("Dry run — continuing despite environment issues.")

    steps:      list[PipelineStep] = []
    raw_csv:    Path | None = None
    scored_csv: Path | None = None

    # ── STEP 1: SCRAPE ───────────────────────────────────────────
    if skip_scrape:
        log.info("STEP: Scrape — SKIPPED (--skip-scrape)")
        # Look for existing raw CSV from today
        raw_csv = DATA_DIR / f"raw_jobs_{today_str}.csv"
        if raw_csv.exists():
            log.info(f"Using existing raw CSV: {raw_csv}")
        else:
            log.warning(f"No existing raw CSV found for today: {raw_csv}")
            raw_csv = None
    else:
        step = PipelineStep("Scrape (JobSpy)")
        steps.append(step)
        with step:
            raw_csv = run_scrape(dry_run)

        if not step.success:
            log.error("Scrape step failed. Aborting pipeline.")
            print_summary(steps, raw_csv, scored_csv, pipeline_start, dry_run)
            sys.exit(1)

    # ── STEP 2: SCORE ────────────────────────────────────────────
    if skip_score:
        log.info("STEP: Score — SKIPPED (--skip-score)")
        scored_csv = DATA_DIR / f"scored_jobs_{today_str}.csv"
        if scored_csv.exists():
            log.info(f"Using existing scored CSV: {scored_csv}")
        else:
            log.warning(f"No existing scored CSV found for today: {scored_csv}")
            scored_csv = None
    else:
        step = PipelineStep("Score (Claude Haiku batch)")
        steps.append(step)
        with step:
            scored_csv = run_score(raw_csv, dry_run)

        if not step.success:
            log.error("Score step failed. Skipping Notion push.")
            print_summary(steps, raw_csv, scored_csv, pipeline_start, dry_run)
            sys.exit(1)

    # ── STEP 3: NOTION ───────────────────────────────────────────
    if skip_notion:
        log.info("STEP: Notion push — SKIPPED (--skip-notion)")
    else:
        step = PipelineStep("Push to Notion")
        steps.append(step)
        with step:
            run_notion(scored_csv, min_score, dry_run)

        if not step.success:
            log.error("Notion push failed. Check logs for details.")
            # Don't exit — scrape and score succeeded, partial win

    # ── SUMMARY ──────────────────────────────────────────────────
    print_summary(steps, raw_csv, scored_csv, pipeline_start, dry_run)

    failed_steps = [s for s in steps if not s.success]
    if failed_steps:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Daily job pipeline: scrape → score → push to Notion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_daily.py                     # full pipeline
  python run_daily.py --dry-run           # full pipeline, no API calls
  python run_daily.py --skip-scrape       # score + Notion only (existing CSV)
  python run_daily.py --skip-notion       # scrape + score only
  python run_daily.py --min-score 6.0     # only push high-confidence jobs

Cron (runs at 8am daily):
  0 8 * * * /path/to/venv/bin/python /path/to/job_pipeline/run_daily.py >> /path/to/logs/cron.log 2>&1
        """,
    )
    parser.add_argument("--skip-scrape",  action="store_true",
                        help="Skip scraping, use existing raw_jobs CSV from today")
    parser.add_argument("--skip-score",   action="store_true",
                        help="Skip scoring, use existing scored_jobs CSV from today")
    parser.add_argument("--skip-notion",  action="store_true",
                        help="Skip Notion push (scrape + score only)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Run pipeline without making real API calls or Notion writes")
    parser.add_argument("--min-score",    type=float, default=4.0,
                        help="Minimum fit_score to push to Notion (default: 4.0)")

    args = parser.parse_args()
    main(
        skip_scrape=args.skip_scrape,
        skip_score=args.skip_score,
        skip_notion=args.skip_notion,
        dry_run=args.dry_run,
        min_score=args.min_score,
    )