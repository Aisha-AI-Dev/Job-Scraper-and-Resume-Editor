"""
scorer/score_jobs.py
--------------------
Scoring module for Aishani's job application pipeline.

What this does:
  1. Reads raw_jobs_YYYY-MM-DD.csv from the scraper
  2. Warms the prompt cache with a single priming API call
  3. Submits all JDs to Claude Haiku via the Batch API (50% discount)
  4. Polls until the batch completes, then parses all JSON results
  5. Writes scored_jobs.csv with all score fields as columns

Architecture note on caching + batching:
  The Batch API processes requests asynchronously and in any order,
  so cache hits are best-effort only. The correct pattern (per Anthropic
  docs) is:
    1. Send ONE synchronous request with a 1-hour cache_control block
       to prime the cache (this is the "priming call")
    2. Immediately submit the full batch — requests will hit the warm cache
  This gives the best cache hit rate for batch workloads.

Usage:
  python scorer/score_jobs.py
  python scorer/score_jobs.py --date 2025-04-03
  python scorer/score_jobs.py --input data/raw_jobs_2025-04-03.csv
  python scorer/score_jobs.py --dry-run   # shows cost estimate, no API calls
"""

import os
import re
import csv
import json
import time
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import anthropic
from anthropic import APIStatusError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR    = Path(__file__).resolve().parent.parent
DATA_DIR    = BASE_DIR / "data"
LOGS_DIR    = BASE_DIR / "logs"
PROMPTS_DIR = BASE_DIR / "prompts"

LOGS_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
today_str = date.today().isoformat()
log_path  = LOGS_DIR / f"scorer_{today_str}.log"

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
MODEL            = "claude-haiku-4-5-20251001"
MAX_TOKENS       = 1500        # scoring output is compact JSON, 600-800 tokens typical
MAX_RETRIES      = 3           # for the priming call and JSON repair
RETRY_BASE_DELAY = 2.0         # seconds, doubles on each retry
BATCH_POLL_INTERVAL = 30       # seconds between batch status checks
BATCH_TIMEOUT_MINS  = 60       # bail out if batch takes longer than this

# Minimum fit_score to include in scored_jobs.csv
# Jobs below this go to scored_jobs_low.csv for reference
MIN_SCORE_THRESHOLD = 4.0

# Approximate token counts for cost estimation
# profile.txt (~10KB) ≈ 2,500 tokens
# target_roles.txt (~8KB) ≈ 2,000 tokens
# scoring_system_prompt.txt (~3KB) ≈ 750 tokens
# Average JD ≈ 600 tokens
# Output ≈ 400 tokens
APPROX_INPUT_TOKENS_FIXED  = 5_250   # system prompt + profile + roles (cached after priming)
APPROX_INPUT_TOKENS_PER_JD =   600
APPROX_OUTPUT_TOKENS_PER_JD =  400

# Haiku 4.5 pricing (as of April 2025)
HAIKU_INPUT_PRICE_PER_M  = 1.00   # $ per million input tokens
HAIKU_OUTPUT_PRICE_PER_M = 5.00   # $ per million output tokens
BATCH_DISCOUNT           = 0.50   # 50% off with Batch API
CACHE_READ_MULTIPLIER    = 0.10   # cache reads cost 10% of normal input price

# ----------------------------------------------------------------
# LOAD STATIC PROMPT FILES
# ----------------------------------------------------------------

def load_prompt_file(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Required prompt file not found: {path}\n"
            f"Make sure you have run the setup steps and {filename} exists in prompts/"
        )
    return path.read_text(encoding="utf-8").strip()


def load_prompts() -> tuple[str, str, str]:
    """Returns (system_prompt, profile_text, target_roles_text)."""
    system_prompt  = load_prompt_file("scoring_system_prompt.txt")
    profile_text   = load_prompt_file("profile.txt")
    target_roles   = load_prompt_file("target_roles.txt")
    log.info(f"Loaded prompt files: system={len(system_prompt)}c  "
             f"profile={len(profile_text)}c  roles={len(target_roles)}c")
    return system_prompt, profile_text, target_roles


# ----------------------------------------------------------------
# COST ESTIMATION
# ----------------------------------------------------------------

def estimate_cost(n_jobs: int) -> dict:
    """
    Estimate the cost of scoring n_jobs.
    Assumes cache hits for the fixed prefix after the priming call.
    """
    # Priming call: full input price (cache write at 1.25x, but we count as 1x for simplicity)
    priming_input_cost = (APPROX_INPUT_TOKENS_FIXED / 1_000_000) * HAIKU_INPUT_PRICE_PER_M

    # Batch jobs: fixed prefix is cache-read (10% cost), JD is fresh input
    # Each job = (fixed_tokens * 0.10 + jd_tokens) * 0.50 (batch discount)
    per_job_input_tokens = (APPROX_INPUT_TOKENS_FIXED * CACHE_READ_MULTIPLIER
                            + APPROX_INPUT_TOKENS_PER_JD)
    batch_input_cost  = (n_jobs * per_job_input_tokens / 1_000_000) * HAIKU_INPUT_PRICE_PER_M * BATCH_DISCOUNT
    batch_output_cost = (n_jobs * APPROX_OUTPUT_TOKENS_PER_JD / 1_000_000) * HAIKU_OUTPUT_PRICE_PER_M * BATCH_DISCOUNT

    total = priming_input_cost + batch_input_cost + batch_output_cost
    return {
        "n_jobs": n_jobs,
        "priming_call": round(priming_input_cost, 5),
        "batch_input":  round(batch_input_cost, 5),
        "batch_output": round(batch_output_cost, 5),
        "total_usd":    round(total, 4),
    }


# ----------------------------------------------------------------
# BUILD API REQUEST STRUCTURES
# ----------------------------------------------------------------

def build_system_blocks(system_prompt: str, profile_text: str, target_roles: str) -> list:
    """
    Build the system message as a list of content blocks.
    The profile + target_roles block gets cache_control so it's
    cached for 1 hour on the priming call and reused by batch jobs.

    Structure:
      Block 0: Scoring instructions (system prompt) — NOT cached
               (it's short and changes less frequently)
      Block 1: Profile + target roles — CACHED (large, static, reused every call)
    """
    cached_content = (
        "=== CANDIDATE PROFILE ===\n\n"
        + profile_text
        + "\n\n=== TARGET ROLES & SCORING GUIDANCE ===\n\n"
        + target_roles
    )
    return [
        {
            "type": "text",
            "text": system_prompt,
        },
        {
            "type": "text",
            "text": cached_content,
            "cache_control": {"type": "ephemeral"},   # 1-hour cache on priming call
        },
    ]


def build_user_message(row: pd.Series) -> str:
    """Build the user message for a single JD row."""
    title   = str(row.get("title",       "Unknown Title"))
    company = str(row.get("company",     "Unknown Company"))
    location = str(row.get("location",   "Unknown Location"))
    job_url  = str(row.get("job_url",    ""))
    desc     = str(row.get("description", ""))

    return (
        f"Company: {company}\n"
        f"Role: {title}\n"
        f"Location: {location}\n"
        f"URL: {job_url}\n\n"
        f"Job Description:\n{desc}"
    )


# ----------------------------------------------------------------
# PRIMING CALL (warms the 1-hour cache)
# ----------------------------------------------------------------

def prime_cache(client: anthropic.Anthropic,
                system_blocks: list) -> bool:
    """
    Send a single synchronous request to warm the 1-hour cache.
    Uses a minimal user message — we only care about the cache write,
    not the response content.
    Returns True on success, False on failure.
    """
    log.info("Priming cache with 1-hour TTL...")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16,           # tiny — we only need the cache write
                system=system_blocks,
                messages=[{
                    "role": "user",
                    "content": "Ready. Awaiting job description to score."
                }],
                extra_headers={
                    "anthropic-beta": "extended-cache-ttl-2025-04-11"
                },
            )
            usage = response.usage
            log.info(
                f"Cache primed successfully. "
                f"Cache write tokens: {getattr(usage, 'cache_creation_input_tokens', '?')} | "
                f"Cache read tokens: {getattr(usage, 'cache_read_input_tokens', '?')}"
            )
            return True

        except (APIStatusError, APITimeoutError, APIConnectionError) as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(f"Priming attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                        f"Retrying in {delay:.0f}s...")
            time.sleep(delay)

    log.error("Cache priming failed after all retries. "
              "Continuing without cache — batch will still work but cost more.")
    return False


# ----------------------------------------------------------------
# BATCH SUBMISSION
# ----------------------------------------------------------------

def build_batch_requests(df: pd.DataFrame,
                         system_blocks: list) -> list:
    """Build the list of batch request dicts for every JD row."""
    requests = []
    for idx, row in df.iterrows():
        custom_id = f"job_{idx}_{str(row.get('_key', idx)).replace('::', '_')[:40]}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "system": system_blocks,
                "messages": [{
                    "role": "user",
                    "content": build_user_message(row),
                }],
            },
        })
    return requests


def submit_batch(client: anthropic.Anthropic,
                 batch_requests: list) -> str:
    """Submit the batch and return the batch ID."""
    log.info(f"Submitting batch of {len(batch_requests):,} jobs...")
    batch = client.beta.messages.batches.create(requests=batch_requests)
    log.info(f"Batch submitted. ID: {batch.id}")
    return batch.id


# ----------------------------------------------------------------
# BATCH POLLING
# ----------------------------------------------------------------

def poll_batch(client: anthropic.Anthropic, batch_id: str) -> bool:
    """
    Poll until the batch is complete (or times out).
    Returns True if succeeded, False if timed out or errored.
    """
    deadline = time.time() + (BATCH_TIMEOUT_MINS * 60)
    log.info(f"Polling batch {batch_id} (timeout: {BATCH_TIMEOUT_MINS} min)...")

    while time.time() < deadline:
        batch = client.beta.messages.batches.retrieve(batch_id)
        status = batch.processing_status

        counts = batch.request_counts
        log.info(
            f"  Status: {status} | "
            f"processing: {counts.processing} | "
            f"succeeded: {counts.succeeded} | "
            f"errored: {counts.errored} | "
            f"expired: {counts.expired}"
        )

        if status == "ended":
            log.info("Batch complete.")
            return True

        time.sleep(BATCH_POLL_INTERVAL)

    log.error(f"Batch {batch_id} did not complete within {BATCH_TIMEOUT_MINS} minutes.")
    return False


# ----------------------------------------------------------------
# RESULT PARSING
# ----------------------------------------------------------------

def extract_json(text: str) -> dict | None:
    """
    Try to parse JSON from the model response text.
    Handles common near-valid JSON issues:
      - Trailing commas
      - Markdown code fences (```json ... ```)
      - Leading/trailing whitespace
    Returns the parsed dict or None on failure.
    """
    if not text:
        return None

    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try fixing trailing commas (common LLM output issue)
    fixed = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Try extracting just the JSON object if there's surrounding text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def repair_json_with_llm(client: anthropic.Anthropic,
                          bad_text: str,
                          original_user_msg: str,
                          system_blocks: list) -> dict | None:
    """
    Fallback: ask Claude to retry with a reminder that JSON is required.
    Used only when extract_json() fails completely.
    """
    log.warning("Attempting JSON repair via re-prompt...")
    repair_prompt = (
        f"Your previous response was not valid JSON. "
        f"Here is what you returned:\n\n{bad_text[:500]}\n\n"
        f"Please score this job description again and return ONLY valid JSON "
        f"with no preamble, no explanation, and no markdown formatting.\n\n"
        f"Original job description:\n{original_user_msg}"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": repair_prompt}],
            )
            result = extract_json(response.content[0].text)
            if result:
                log.info("JSON repair succeeded.")
                return result
        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(f"Repair attempt {attempt} failed: {e}. Sleeping {delay}s...")
            time.sleep(delay)

    log.error("JSON repair failed after all attempts.")
    return None


def collect_batch_results(client: anthropic.Anthropic,
                          batch_id: str,
                          df: pd.DataFrame,
                          system_blocks: list) -> list[dict]:
    """
    Stream batch results, parse JSON, handle errors.
    Returns a list of result dicts (one per job).
    """
    results = []
    errors  = []

    # Build a lookup from custom_id → original row for repair fallback
    id_to_row = {}
    for idx, row in df.iterrows():
        custom_id = f"job_{idx}_{str(row.get('_key', idx)).replace('::', '_')[:40]}"
        id_to_row[custom_id] = (idx, row)

    log.info("Streaming batch results...")
    for result in client.beta.messages.batches.results(batch_id):
        custom_id = result.custom_id
        orig_idx, orig_row = id_to_row.get(custom_id, (None, None))

        # Base metadata from original row
        base = {
            "_key":       str(orig_row.get("_key",    "")) if orig_row is not None else "",
            "job_url":    str(orig_row.get("job_url", "")) if orig_row is not None else "",
            "site":       str(orig_row.get("site",    "")) if orig_row is not None else "",
            "date_posted": str(orig_row.get("date_posted", "")) if orig_row is not None else "",
            "_custom_id": custom_id,
            "_scored_at": datetime.now().isoformat(),
        }

        if result.result.type == "succeeded":
            raw_text = result.result.message.content[0].text
            parsed   = extract_json(raw_text)

            if parsed is None and orig_row is not None:
                # Attempt repair
                user_msg = build_user_message(orig_row)
                parsed   = repair_json_with_llm(client, raw_text, user_msg, system_blocks)

            if parsed:
                base.update(parsed)
                results.append(base)
            else:
                base["_error"] = f"JSON parse failed. Raw: {raw_text[:300]}"
                errors.append(base)
                log.error(f"[{custom_id}] JSON parse failed permanently.")

        elif result.result.type == "errored":
            err_msg = str(result.result.error)
            base["_error"] = err_msg
            errors.append(base)
            log.error(f"[{custom_id}] API error: {err_msg}")

        else:
            base["_error"] = f"Unexpected result type: {result.result.type}"
            errors.append(base)

    log.info(f"Results collected: {len(results):,} successful | {len(errors):,} errors")
    return results, errors


# ----------------------------------------------------------------
# OUTPUT
# ----------------------------------------------------------------

def save_results(results: list[dict],
                 errors:  list[dict],
                 output_path: Path) -> None:
    """
    Save scored results to CSV.
    High-scoring jobs → scored_jobs.csv
    Low-scoring jobs  → scored_jobs_low.csv
    Errors            → scored_jobs_errors.csv
    """
    if not results:
        log.warning("No scored results to save.")
        return

    df_all = pd.DataFrame(results)

    # Flatten list fields (matched_skills etc.) to pipe-separated strings
    list_cols = [
        "matched_skills", "missing_skills", "nice_to_have_gaps",
        "transferable_strengths", "hard_constraint_violations",
        "manual_review_reasons",
    ]
    for col in list_cols:
        if col in df_all.columns:
            df_all[col] = df_all[col].apply(
                lambda x: " | ".join(x) if isinstance(x, list) else str(x)
            )

    # Sort by fit_score descending
    if "fit_score" in df_all.columns:
        df_all["fit_score"] = pd.to_numeric(df_all["fit_score"], errors="coerce")
        df_all = df_all.sort_values("fit_score", ascending=False)

    # Split by threshold
    mask_high = df_all.get("fit_score", pd.Series(dtype=float)) >= MIN_SCORE_THRESHOLD
    df_high   = df_all[mask_high]
    df_low    = df_all[~mask_high]

    # Save
    df_high.to_csv(output_path, index=False, quoting=csv.QUOTE_ALL)
    log.info(f"Saved {len(df_high):,} scored jobs (fit >= {MIN_SCORE_THRESHOLD}) → {output_path}")

    if not df_low.empty:
        low_path = output_path.parent / output_path.name.replace("scored_jobs", "scored_jobs_low")
        df_low.to_csv(low_path, index=False, quoting=csv.QUOTE_ALL)
        log.info(f"Saved {len(df_low):,} low-score jobs → {low_path}")

    if errors:
        err_path = output_path.parent / output_path.name.replace("scored_jobs", "scored_jobs_errors")
        pd.DataFrame(errors).to_csv(err_path, index=False)
        log.info(f"Saved {len(errors):,} errors → {err_path}")


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(input_path: Path | None = None,
         dry_run: bool = False,
         target_date: str | None = None) -> None:

    log.info("=" * 60)
    log.info(f"Scorer starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # 1. Resolve input file
    if input_path is None:
        date_str  = target_date or today_str
        input_path = DATA_DIR / f"raw_jobs_{date_str}.csv"

    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        log.error("Run the scraper first: python scrapers/jobspy_scraper.py")
        return

    # 2. Load jobs
    df = pd.read_csv(input_path, dtype=str)
    log.info(f"Loaded {len(df):,} jobs from {input_path}")

    if df.empty:
        log.info("No jobs to score. Exiting.")
        return

    # 3. Cost estimate
    est = estimate_cost(len(df))
    log.info(
        f"Cost estimate for {est['n_jobs']} jobs:\n"
        f"  Priming call:  ${est['priming_call']:.5f}\n"
        f"  Batch input:   ${est['batch_input']:.5f}\n"
        f"  Batch output:  ${est['batch_output']:.5f}\n"
        f"  TOTAL:         ~${est['total_usd']:.4f}"
    )

    if dry_run:
        log.info("Dry run mode — no API calls made. Exiting.")
        return

    # 4. Load prompts
    system_prompt, profile_text, target_roles = load_prompts()
    system_blocks = build_system_blocks(system_prompt, profile_text, target_roles)

    # 5. Init client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in environment or .env file")
    client = anthropic.Anthropic(api_key=api_key)

    # 6. Prime the cache (1-hour TTL)
    prime_cache(client, system_blocks)

    # 7. Build + submit batch
    batch_requests = build_batch_requests(df, system_blocks)
    batch_id = submit_batch(client, batch_requests)

    # 8. Poll until complete
    success = poll_batch(client, batch_id)
    if not success:
        log.error(f"Batch {batch_id} timed out. "
                  f"You can resume result collection manually with --batch-id {batch_id}")
        return

    # 9. Collect + parse results
    results, errors = collect_batch_results(client, batch_id, df, system_blocks)

    # 10. Save output
    output_path = DATA_DIR / f"scored_jobs_{today_str}.csv"
    save_results(results, errors, output_path)

    # 11. Final summary
    if results:
        df_res = pd.DataFrame(results)
        if "fit_score" in df_res.columns:
            scores = pd.to_numeric(df_res["fit_score"], errors="coerce").dropna()
            yes_count = (df_res.get("apply_recommendation", "") == "yes").sum()
            mr_count  = (df_res.get("apply_recommendation", "") == "manual_review").sum()
            no_count  = (df_res.get("apply_recommendation", "") == "no").sum()
            log.info(
                f"\nSCORING SUMMARY\n"
                f"  Total scored:     {len(results):,}\n"
                f"  Errors:           {len(errors):,}\n"
                f"  Avg fit score:    {scores.mean():.1f}\n"
                f"  Max fit score:    {scores.max():.1f}\n"
                f"  → yes:            {yes_count}\n"
                f"  → manual_review:  {mr_count}\n"
                f"  → no:             {no_count}\n"
                f"  Output:           {output_path}"
            )

    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score jobs via Claude Haiku Batch API")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to raw_jobs CSV (default: data/raw_jobs_TODAY.csv)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date string YYYY-MM-DD to load raw_jobs for (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cost estimate and exit without making API calls",
    )
    args = parser.parse_args()
    main(
        input_path=args.input,
        dry_run=args.dry_run,
        target_date=args.date,
    )