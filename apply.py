"""
apply.py
--------
One-command trigger for the full application workflow.

Given a Notion page URL (or --company + --role directly), this script:
  1. Pulls job details from the Notion page  (or uses CLI args)
  2. Finds the JD description from the raw_jobs CSV via _key
     Falls back to fetching the job URL directly if CSV lookup fails
  3. Runs the tailoring call (Claude Sonnet)  → draft .txt
  4. Runs the resume injector                → tailored .docx
  5. Updates the Notion page: status → Reviewing, resume_version → filename
  6. Opens the .docx for your review

Usage:
  # From a Notion page URL (copy from browser):
  python apply.py --notion "https://www.notion.so/PageTitle-abc123def456..."

  # From company + role directly (Notion page looked up by matching):
  python apply.py --company "Qualcomm" --role "ML Engineer Tools"

  # With location override (default is relocate):
  python apply.py --notion "https://..." --location cleveland

  # Skip cover letter (tailoring only):
  python apply.py --notion "https://..." --tailor-only

  # Skip Notion update (dry run the workflow):
  python apply.py --notion "https://..." --no-notion-update

  # Don't open the docx automatically:
  python apply.py --notion "https://..." --no-open
"""

import os
import re
import sys
import glob
import time
import logging
import argparse
import importlib.util
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from notion_client import Client as NotionClient
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR    = Path(__file__).resolve().parent
DATA_DIR    = BASE_DIR / "data"
LOGS_DIR    = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

today_str = date.today().isoformat()

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / f"apply_{today_str}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# NOTION PROPERTY MAP
# Must match push_to_notion.py exactly
# ----------------------------------------------------------------
PROP = {
    "role":             "Role",
    "company":          "Company",
    "fit_score":        "Fit score",
    "tier":             "Tier",
    "role_family":      "Role family",
    "apply_rec":        "Apply recommendation",
    "status":           "Status",
    "resume_version":   "Resume version",
    "job_url":          "Job URL",
    "site":             "Site",
    "notes":            "Notes",
    "key":              "Scored at",   # we store _key in scored_at field
}

# ----------------------------------------------------------------
# MODULE LOADER
# ----------------------------------------------------------------

def load_module(name: str, path: Path):
    spec   = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# ----------------------------------------------------------------
# STEP 1 — RESOLVE JOB DETAILS
# ----------------------------------------------------------------

def extract_notion_page_id(url_or_id: str) -> str:
    """
    Extract the 32-char page ID from a Notion URL or plain ID.
    Handles formats:
      https://www.notion.so/workspace/Title-abc123def456...
      https://notion.so/abc123def456...
      abc123def456...  (plain ID)
    """
    # Strip query params and fragments
    clean = url_or_id.split("?")[0].split("#")[0].rstrip("/")
    # Last path segment, then take last 32 hex chars
    segment = clean.split("/")[-1]
    # Notion IDs are 32 hex chars, sometimes with hyphens
    hex_part = re.sub(r"[^a-f0-9]", "", segment.lower())
    if len(hex_part) >= 32:
        raw = hex_part[-32:]
        # Format as UUID: 8-4-4-4-12
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    # Maybe it's already a UUID
    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        url_or_id.lower()
    )
    if uuid_match:
        return uuid_match.group(0)
    raise ValueError(f"Could not extract Notion page ID from: {url_or_id}")


def get_text(prop: dict) -> str:
    """Extract plain text from a Notion property value."""
    t = prop.get("type", "")
    if t == "title":
        return "".join(r["plain_text"] for r in prop.get("title", []))
    if t == "rich_text":
        return "".join(r["plain_text"] for r in prop.get("rich_text", []))
    if t == "number":
        v = prop.get("number")
        return str(v) if v is not None else ""
    if t == "select":
        s = prop.get("select")
        return s["name"] if s else ""
    if t == "url":
        return prop.get("url") or ""
    if t == "checkbox":
        return str(prop.get("checkbox", False))
    if t == "date":
        d = prop.get("date")
        return d["start"] if d else ""
    return ""


def fetch_notion_page(notion: NotionClient, page_id: str) -> dict:
    """
    Fetch a Notion page and return a clean dict of its key fields.
    """
    page = notion.pages.retrieve(page_id)
    props = page.get("properties", {})

    def p(name):
        return get_text(props.get(name, {}))

    return {
        "page_id":      page_id,
        "company":      p(PROP["company"]),
        "role":         p(PROP["role"]),
        "fit_score":    p(PROP["fit_score"]),
        "tier":         p(PROP["tier"]),
        "role_family":  p(PROP["role_family"]),
        "apply_rec":    p(PROP["apply_rec"]),
        "status":       p(PROP["status"]),
        "job_url":      p(PROP["job_url"]),
        "site":         p(PROP["site"]),
    }


def find_notion_page_by_company_role(
    notion: NotionClient,
    database_id: str,
    company: str,
    role: str,
) -> dict | None:
    """
    Query Notion DB to find a page matching company + role.
    Returns the first match or None.
    """
    log.info(f"Searching Notion for: {role} @ {company}...")
    response = notion.databases.query(
        database_id=database_id,
        filter={
            "and": [
                {
                    "property": PROP["company"],
                    "rich_text": {"contains": company},
                },
            ]
        },
        page_size=20,
    )
    results = response.get("results", [])
    role_lower = role.lower()
    for page in results:
        props  = page.get("properties", {})
        p_role = get_text(props.get(PROP["role"], {})).lower()
        if any(word in p_role for word in role_lower.split()):
            return {
                "page_id":   page["id"],
                "company":   get_text(props.get(PROP["company"], {})),
                "role":      get_text(props.get(PROP["role"], {})),
                "fit_score": get_text(props.get(PROP["fit_score"], {})),
                "tier":      get_text(props.get(PROP["tier"], {})),
                "apply_rec": get_text(props.get(PROP["apply_rec"], {})),
                "status":    get_text(props.get(PROP["status"], {})),
                "job_url":   get_text(props.get(PROP["job_url"], {})),
                "site":      get_text(props.get(PROP["site"], {})),
            }
    return None

# ----------------------------------------------------------------
# STEP 2 — FIND JD FROM CSV
# ----------------------------------------------------------------

def find_jd_in_csv(job_url: str, company: str, role: str) -> str | None:
    """
    Look up JD description in raw_jobs CSVs.
    Tries today's file first, then walks back through available files.
    Matches by job_url (most reliable) or company + title fuzzy match.
    Loads only needed columns to avoid memory pressure.
    """
    csv_files = sorted(
        DATA_DIR.glob("raw_jobs_*.csv"),
        reverse=True   # newest first
    )

    if not csv_files:
        log.warning("No raw_jobs CSV files found in data/")
        return None

    for csv_path in csv_files:
        log.info(f"Searching {csv_path.name}...")
        try:
            df = pd.read_csv(
                csv_path,
                usecols=["job_url", "title", "company", "description"],
                dtype=str,
            )
        except Exception as e:
            log.warning(f"Could not read {csv_path.name}: {e}")
            continue

        # Try exact job_url match first
        if job_url:
            match = df[df["job_url"] == job_url]
            if not match.empty:
                desc = match.iloc[0]["description"]
                if pd.notna(desc) and len(str(desc)) > 100:
                    log.info(f"Found JD by URL match in {csv_path.name}")
                    return str(desc)

        # Fuzzy fallback: company name + role title substring
        company_lower = company.lower()
        role_lower    = role.lower().split()[0]   # first word of role
        mask = (
            df["company"].str.lower().str.contains(company_lower, na=False) &
            df["title"].str.lower().str.contains(role_lower, na=False)
        )
        match = df[mask]
        if not match.empty:
            desc = match.iloc[0]["description"]
            if pd.notna(desc) and len(str(desc)) > 100:
                log.info(
                    f"Found JD by fuzzy match ({match.iloc[0]['title']} @ "
                    f"{match.iloc[0]['company']}) in {csv_path.name}"
                )
                return str(desc)

    return None


def fetch_jd_from_url(job_url: str) -> str | None:
    """
    Fallback: fetch JD text directly from the job URL.
    Uses a simple requests call — works for most job boards.
    """
    if not job_url:
        return None
    log.info(f"Fetching JD from URL: {job_url}")
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(job_url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script/style
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Trim to 8000 chars
        return text[:8000] if text else None
    except Exception as e:
        log.warning(f"URL fetch failed: {e}")
        return None

# ----------------------------------------------------------------
# STEP 3 — TAILOR
# ----------------------------------------------------------------

def run_tailoring(
    company: str,
    role: str,
    jd_text: str,
    location_key: str,
    tailor_only: bool,
    no_open: bool,
) -> Path | None:
    """
    Run the tailoring module inline (imports tailor_resume.main).
    Returns the path to the output .txt draft, or None on failure.
    """
    tailor_path = BASE_DIR / "tailor" / "tailor_resume.py"
    if not tailor_path.exists():
        log.error(f"tailor_resume.py not found at {tailor_path}")
        return None

    log.info("Running tailoring...")
    try:
        tailor = load_module("tailor_resume", tailor_path)

        # Write JD to a temp file so tailor.main() can read it
        tmp_jd = DATA_DIR / f"_tmp_jd_{today_str}.txt"
        tmp_jd.write_text(jd_text, encoding="utf-8")

        tailor.main(
            company=company,
            role=role,
            jd_source=tmp_jd,
            bullets=None,
            hook_angle=None,
            cover_only=False,
            tailor_only=tailor_only,
            no_open=True,    # don't open the .txt — we open the .docx
        )
        tmp_jd.unlink(missing_ok=True)

        # Find the draft that was just written
        import re as _re
        safe_company = _re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
        company_dir  = DATA_DIR / "tailored" / safe_company
        drafts = sorted(company_dir.glob(f"*{today_str}.txt"), reverse=True)
        if drafts:
            log.info(f"Draft written → {drafts[0]}")
            return drafts[0]
        else:
            log.error("Tailoring ran but no draft .txt found")
            return None

    except Exception as e:
        log.error(f"Tailoring failed: {e}")
        import traceback
        traceback.print_exc()
        return None

# ----------------------------------------------------------------
# STEP 4 — INJECT
# ----------------------------------------------------------------

def run_injection(
    draft_path: Path,
    company: str,
    role: str,
    location_key: str,
    no_open: bool,
) -> Path | None:
    """
    Run the resume injector inline (imports inject_resume.main).
    Returns the path to the output .docx, or None on failure.
    """
    inject_path = BASE_DIR / "tailor" / "inject_resume.py"
    if not inject_path.exists():
        log.error(f"inject_resume.py not found at {inject_path}")
        return None

    log.info("Running injector...")
    try:
        inject = load_module("inject_resume", inject_path)
        inject.main(
            draft_path=draft_path,
            company=company,
            role=role,
            location_key=location_key,
            no_open=no_open,
        )

        # Find the docx that was just written
        import re as _re
        safe_company = _re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
        company_dir  = DATA_DIR / "tailored" / safe_company
        docx_files   = sorted(
            company_dir.glob(f"*{today_str}_RESUME.docx"), reverse=True
        )
        if docx_files:
            log.info(f"Resume written → {docx_files[0]}")
            return docx_files[0]
        else:
            log.error("Injection ran but no .docx found")
            return None

    except Exception as e:
        log.error(f"Injection failed: {e}")
        import traceback
        traceback.print_exc()
        return None

# ----------------------------------------------------------------
# STEP 5 — UPDATE NOTION
# ----------------------------------------------------------------

def update_notion_page(
    notion: NotionClient,
    page_id: str,
    resume_filename: str,
) -> None:
    """
    Update the Notion page:
      - Status → Reviewing
      - Resume version → resume filename (without full path)
    """
    log.info(f"Updating Notion page {page_id}...")
    try:
        notion.pages.update(
            page_id=page_id,
            properties={
                PROP["status"]: {
                    "select": {"name": "Reviewing"}
                },
                PROP["resume_version"]: {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": resume_filename[:500]}
                    }]
                },
            }
        )
        log.info("Notion page updated: status=Reviewing, resume_version set")
    except Exception as e:
        log.error(f"Notion update failed: {e}")
        log.warning("Resume was generated successfully — update Notion manually")

# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    notion_url:       str | None,
    company:          str | None,
    role:             str | None,
    location_key:     str = "relocate",
    tailor_only:      bool = False,
    no_notion_update: bool = False,
    no_open:          bool = False,
) -> None:

    log.info("=" * 60)
    log.info(f"apply.py starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # ── Init Notion client ───────────────────────────────────────
    notion_token = os.getenv("NOTION_TOKEN")
    database_id  = os.getenv("NOTION_DATABASE_ID")
    if not notion_token:
        log.error("NOTION_TOKEN not set in .env")
        sys.exit(1)
    notion = NotionClient(auth=notion_token)

    # ── Step 1: Resolve job details ──────────────────────────────
    page_id  = None
    job_info = {}

    if notion_url:
        log.info("Resolving from Notion URL...")
        page_id  = extract_notion_page_id(notion_url)
        job_info = fetch_notion_page(notion, page_id)
        company  = job_info["company"]
        role     = job_info["role"]
        log.info(f"Found: {role} @ {company} (score={job_info.get('fit_score','?')})")

    elif company and role:
        log.info(f"Searching Notion for {role} @ {company}...")
        if database_id:
            job_info = find_notion_page_by_company_role(
                notion, database_id, company, role
            )
            if job_info:
                page_id = job_info["page_id"]
                log.info(f"Found in Notion: {job_info['role']} @ {job_info['company']}")
            else:
                log.warning("No matching Notion page found — will skip Notion update")
        else:
            log.warning("NOTION_DATABASE_ID not set — cannot look up page")
    else:
        log.error("Provide --notion URL or both --company and --role")
        sys.exit(1)

    if not company or not role:
        log.error("Could not determine company or role")
        sys.exit(1)

    # ── Step 2: Get JD text ───────────────────────────────────────
    job_url = job_info.get("job_url", "")
    log.info("Looking up JD in raw_jobs CSV...")
    jd_text = find_jd_in_csv(job_url, company, role)

    if not jd_text:
        log.warning("JD not found in CSV — trying live URL fetch...")
        jd_text = fetch_jd_from_url(job_url)

    if not jd_text:
        log.error(
            "Could not retrieve JD text from CSV or URL.\n"
            "You can paste the JD manually:\n"
            f"  python tailor/tailor_resume.py --company '{company}' --role '{role}'"
        )
        sys.exit(1)

    log.info(f"JD retrieved ({len(jd_text):,} chars)")

    # ── Step 3: Tailor ────────────────────────────────────────────
    draft_path = run_tailoring(
        company=company,
        role=role,
        jd_text=jd_text,
        location_key=location_key,
        tailor_only=tailor_only,
        no_open=True,
    )

    if not draft_path:
        log.error("Tailoring failed — aborting")
        sys.exit(1)

    # ── Step 4: Inject ────────────────────────────────────────────
    docx_path = run_injection(
        draft_path=draft_path,
        company=company,
        role=role,
        location_key=location_key,
        no_open=no_open,
    )

    if not docx_path:
        log.error("Injection failed — check logs")
        sys.exit(1)

    # ── Step 5: Update Notion ─────────────────────────────────────
    if page_id and not no_notion_update:
        update_notion_page(notion, page_id, docx_path.name)
    elif no_notion_update:
        log.info("Notion update skipped (--no-notion-update)")
    else:
        log.info("No Notion page ID — skipping update")

    # ── Summary ───────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("DONE")
    log.info(f"  Draft:   {draft_path}")
    log.info(f"  Resume:  {docx_path}")
    if page_id and not no_notion_update:
        log.info(f"  Notion:  {page_id} → Reviewing")
    log.info("")
    log.info("  Review checklist:")
    log.info("  1. Check every bullet for accuracy")
    log.info("  2. Personalise cover letter hook if you know the hiring manager")
    log.info("  3. Save as PDF before submitting")
    log.info("  4. Update Notion: date_applied + follow_up_date after submitting")
    log.info("=" * 60)


# ----------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="One-command application workflow: Notion → tailor → inject → review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From Notion URL:
  python apply.py --notion "https://notion.so/workspace/Title-abc123..."

  # From CLI args:
  python apply.py --company "Qualcomm" --role "ML Engineer Tools"

  # With location override:
  python apply.py --notion "https://..." --location cleveland

  # Tailoring only (no docx injection):
  python apply.py --notion "https://..." --tailor-only

  # Skip Notion status update:
  python apply.py --notion "https://..." --no-notion-update
        """,
    )
    ap.add_argument("--notion",            type=str, default=None,
                    help="Notion page URL or page ID")
    ap.add_argument("--company",           type=str, default=None)
    ap.add_argument("--role",              type=str, default=None)
    ap.add_argument("--location",          type=str, default="relocate",
                    help="Location key (default: relocate)")
    ap.add_argument("--tailor-only",       action="store_true",
                    help="Run tailoring only, skip resume injection")
    ap.add_argument("--no-notion-update",  action="store_true",
                    help="Don't update Notion page status")
    ap.add_argument("--no-open",           action="store_true",
                    help="Don't open the output .docx automatically")

    args = ap.parse_args()
    main(
        notion_url=args.notion,
        company=args.company,
        role=args.role,
        location_key=args.location,
        tailor_only=args.tailor_only,
        no_notion_update=args.no_notion_update,
        no_open=args.no_open,
    )