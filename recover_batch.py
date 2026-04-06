"""
recover_batch.py
----------------
Collects results from an already-completed batch without re-running scoring.
Use this when the scorer was interrupted during result collection.

Usage:
  python recover_batch.py --batch-id msgbatch_015L2aw1FMDRmUKUM9SdvCbG \
                          --input data/raw_jobs_2026-04-05.csv
"""

import os
import re
import csv
import json
import argparse
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import anthropic
from dotenv import load_dotenv

load_dotenv()

BASE_DIR  = Path(__file__).resolve().parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

today_str = date.today().isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / f"recover_{today_str}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

MIN_SCORE_THRESHOLD = 4.0


def extract_json(text: str) -> dict | None:
    """Try multiple strategies to extract valid JSON — no LLM calls."""
    if not text:
        return None

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$",          "", text.strip()).strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fix trailing commas
    fixed = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 3. Extract JSON object with regex
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 4. Try fixing trailing commas on the regex-extracted object
    if match:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", match.group()))
        except json.JSONDecodeError:
            pass

    return None


def collect(batch_id: str, input_path: Path, output_date: str) -> None:
    log.info("=" * 60)
    log.info(f"Batch recovery — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Batch ID: {batch_id}")
    log.info("=" * 60)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)

    # Load raw jobs for metadata lookup
    log.info(f"Loading raw jobs from {input_path}...")
    df_raw = pd.read_csv(input_path, dtype=str)
    id_to_row = {}
    for idx, row in df_raw.iterrows():
        key       = str(row.get("_key", idx)).replace("::", "_")[:40]
        custom_id = f"job_{idx}_{key}"
        id_to_row[custom_id] = (idx, row)
    log.info(f"Loaded {len(df_raw):,} raw jobs")

    # Check batch status
    log.info("Checking batch status...")
    batch = client.beta.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    log.info(
        f"Status: {batch.processing_status} | "
        f"succeeded: {counts.succeeded} | "
        f"errored: {counts.errored} | "
        f"processing: {counts.processing}"
    )

    if batch.processing_status != "ended":
        log.error("Batch has not completed yet. Wait and retry.")
        return

    # Stream and collect results
    results = []
    errors  = []
    parse_failures = 0

    log.info("Streaming results (no LLM repair — regex only)...")

    for result in client.beta.messages.batches.results(batch_id):
        custom_id = result.custom_id
        orig_idx, orig_row = id_to_row.get(custom_id, (None, None))

        base = {
            "_key":        str(orig_row.get("_key",    "")) if orig_row is not None else "",
            "job_url":     str(orig_row.get("job_url", "")) if orig_row is not None else "",
            "site":        str(orig_row.get("site",    "")) if orig_row is not None else "",
            "date_posted": str(orig_row.get("date_posted","")) if orig_row is not None else "",
            "_custom_id":  custom_id,
            "_scored_at":  datetime.now().isoformat(),
        }

        if result.result.type == "succeeded":
            raw_text = result.result.message.content[0].text
            parsed   = extract_json(raw_text)

            if parsed:
                base.update(parsed)
                results.append(base)
            else:
                parse_failures += 1
                base["_error"] = f"JSON parse failed. Raw: {raw_text[:200]}"
                errors.append(base)
                if parse_failures <= 5:
                    log.warning(f"  Parse failed: {custom_id} | raw: {raw_text[:80]}")

        elif result.result.type == "errored":
            base["_error"] = str(result.result.error)
            errors.append(base)

    log.info(
        f"Collected: {len(results):,} parsed | "
        f"{len(errors):,} errors | "
        f"{parse_failures} parse failures"
    )

    if not results:
        log.error("No results parsed — check errors file")
        return

    # Save
    df_all = pd.DataFrame(results)

    # Flatten list fields
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

    if "fit_score" in df_all.columns:
        df_all["fit_score"] = pd.to_numeric(df_all["fit_score"], errors="coerce")
        df_all = df_all.sort_values("fit_score", ascending=False)

    mask_high = df_all.get("fit_score", pd.Series(dtype=float)) >= MIN_SCORE_THRESHOLD
    df_high   = df_all[mask_high]
    df_low    = df_all[~mask_high]

    out_path = DATA_DIR / f"scored_jobs_{output_date}.csv"
    df_high.to_csv(out_path, index=False, quoting=csv.QUOTE_ALL)
    log.info(f"Saved {len(df_high):,} scored jobs (fit >= {MIN_SCORE_THRESHOLD}) → {out_path}")

    if not df_low.empty:
        low_path = DATA_DIR / f"scored_jobs_low_{output_date}.csv"
        df_low.to_csv(low_path, index=False, quoting=csv.QUOTE_ALL)
        log.info(f"Saved {len(df_low):,} low-score jobs → {low_path}")

    if errors:
        err_path = DATA_DIR / f"scored_jobs_errors_{output_date}.csv"
        pd.DataFrame(errors).to_csv(err_path, index=False)
        log.info(f"Saved {len(errors):,} errors → {err_path}")

    # Summary
    if "fit_score" in df_all.columns:
        scores = df_all["fit_score"].dropna()
        yes_count = (df_all.get("apply_recommendation","") == "yes").sum()
        mr_count  = (df_all.get("apply_recommendation","") == "manual_review").sum()
        no_count  = (df_all.get("apply_recommendation","") == "no").sum()
        log.info("=" * 60)
        log.info("SUMMARY")
        log.info(f"  Total parsed:     {len(df_all):,}")
        log.info(f"  Parse failures:   {parse_failures}")
        log.info(f"  Avg fit score:    {scores.mean():.1f}")
        log.info(f"  → yes:            {yes_count}")
        log.info(f"  → manual_review:  {mr_count}")
        log.info(f"  → no:             {no_count}")
        log.info(f"  Output:           {out_path}")
        log.info("=" * 60)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Recover results from a completed batch")
    ap.add_argument("--batch-id", required=True, help="Batch ID from the scorer log")
    ap.add_argument("--input",    required=True, type=Path,
                    help="Raw jobs CSV used for the original scoring run")
    ap.add_argument("--date",     type=str, default=date.today().isoformat(),
                    help="Date string for output filename (default: today)")
    args = ap.parse_args()
    collect(args.batch_id, args.input, args.date)
