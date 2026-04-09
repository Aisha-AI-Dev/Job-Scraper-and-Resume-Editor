"""
apply.py
--------
One-command trigger for the full application workflow.

SINGLE APPLICATION:
  # From Notion URL (scraped job):
  python apply.py --notion "https://www.notion.so/..."

  # From company + role (looks up Notion automatically):
  python apply.py --company "Qualcomm" --role "ML Engineer Tools"

  # External JD from file (not from scraper):
  python apply.py --company "Google" --role "ML Engineer" --jd path/to/jd.txt

  # External JD — paste interactively:
  python apply.py --company "Google" --role "ML Engineer"
  (if JD not found in CSV, it will prompt you to paste)

  # Location override (default: relocate):
  python apply.py --notion "https://..." --location cleveland

BATCH MODE (Notion-based):
  # Process all 'yes' rows with status 'New' automatically:
  python apply.py --batch

  # Batch with limit (do first 10 only):
  python apply.py --batch --limit 10

  # Batch but don't open each docx (review later):
  python apply.py --batch --no-open

OTHER OPTIONS:
  --tailor-only        Tailor only, skip resume injection
  --no-notion-update   Don't update Notion page status
  --no-open            Don't open the .docx automatically
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
BASE_DIR = Path(__file__).resolve().parent
import sys; sys.path.insert(0, str(BASE_DIR))

try:
    from paths import RAW_DIR, TAILORED_DIR, LOGS_DIR, latest_raw_jobs
except ModuleNotFoundError:
    DATA_DIR_    = BASE_DIR / "data"
    RAW_DIR      = DATA_DIR_ / "raw"
    TAILORED_DIR = DATA_DIR_ / "tailored"
    LOGS_DIR     = BASE_DIR / "logs"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def latest_raw_jobs():
        files = sorted(RAW_DIR.glob("raw_jobs_*.csv"), reverse=True)
        if not files:
            files = sorted((BASE_DIR / "data").glob("raw_jobs_*.csv"), reverse=True)
        return files[0] if files else None

DATA_DIR = BASE_DIR / "data"

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
    response = notion.request(
        path=f"databases/{database_id}/query",
        method="POST",
        body={
            "filter": {
                "and": [
                    {
                        "property": PROP["company"],
                        "rich_text": {"contains": company},
                    },
                ]
            },
            "page_size": 20,
        }
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


def paste_jd_interactively(company: str, role: str) -> str | None:
    """
    Prompt the user to paste a JD directly into the terminal.
    Type END on its own line to finish.
    """
    print()
    print("=" * 60)
    print(f"JD not found in CSV for: {role} @ {company}")
    print("Paste the job description below.")
    print("When done, type END on a new line and press Enter.")
    print("=" * 60)
    lines = []
    try:
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        pass
    text = "\n".join(lines).strip()
    if not text:
        log.warning("No JD text entered")
        return None
    log.info(f"JD received ({len(text):,} chars from paste)")
    return text


def load_jd_from_file(jd_path: Path) -> str | None:
    """Load JD text from a file."""
    if not jd_path.exists():
        log.error(f"JD file not found: {jd_path}")
        return None
    text = jd_path.read_text(encoding="utf-8").strip()
    if not text:
        log.error(f"JD file is empty: {jd_path}")
        return None
    log.info(f"JD loaded from file ({len(text):,} chars): {jd_path.name}")
    return text


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
        # Search role subfolder first, then company dir (backward compat)
        import re as _re
        safe_company = _re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
        safe_role    = _re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]
        company_dir  = DATA_DIR / "tailored" / safe_company
        role_dir     = company_dir / safe_role

        draft = None
        for search in [role_dir, company_dir]:
            found = sorted(
                [f for f in search.glob("*.txt") if "_OUTREACH" not in f.name],
                reverse=True,
            )
            if found:
                draft = found[0]
                break

        if draft:
            log.info(f"Draft written → {draft}")
            return draft
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
        # Search role subfolder first, then company dir (backward compat)
        import re as _re
        safe_company = _re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
        safe_role    = _re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]
        company_dir  = DATA_DIR / "tailored" / safe_company
        role_dir     = company_dir / safe_role

        docx = None
        for search in [role_dir, company_dir]:
            found = sorted(search.glob("*_RESUME.docx"), reverse=True)
            if found:
                docx = found[0]
                break

        if docx:
            log.info(f"Resume written → {docx}")
            return docx
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
        notion.request(
            path=f"pages/{page_id}",
            method="PATCH",
            body={
                "properties": {
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
            }
        )
        log.info("Notion page updated: status=Reviewing, resume_version set")
    except Exception as e:
        log.error(f"Notion update failed: {e}")
        log.warning("Resume was generated successfully — update Notion manually")

# ----------------------------------------------------------------
# BATCH MODE — process all 'yes' + 'New' rows from Notion
# ----------------------------------------------------------------

def run_batch(
    notion: NotionClient,
    database_id: str,
    location_key: str,
    jd_path: Path | None,
    tailor_only: bool,
    no_notion_update: bool,
    no_open: bool,
    limit: int | None,
) -> None:
    """
    Query Notion for all pages where 'Queue for apply' checkbox is ticked.
    Runs tailor → inject → Notion update for each, then unticks the checkbox.
    """
    if not database_id:
        log.error("NOTION_DATABASE_ID not set — cannot run batch mode")
        sys.exit(1)

    log.info("Batch mode: fetching queued jobs from Notion...")
    try:
        response = notion.request(
            path=f"databases/{database_id}/query",
            method="POST",
            body={
                "filter": {
                    "property": "Queue for apply",
                    "checkbox": {"equals": True},
                },
                "sorts": [
                    {"property": PROP["fit_score"], "direction": "descending"}
                ],
                "page_size": 100,
            }
        )
    except Exception as e:
        log.error(f"Failed to query Notion: {e}")
        sys.exit(1)

    pages = response.get("results", [])
    if not pages:
        log.info("No jobs queued in Notion. Tick 'Queue for apply' on the rows you want.")
        return

    if limit:
        pages = pages[:limit]

    log.info(f"Found {len(pages)} job(s) queued — starting...")
    log.info("=" * 60)

    succeeded = []
    failed    = []

    for i, page in enumerate(pages, 1):
        props   = page.get("properties", {})
        company = get_text(props.get(PROP["company"], {}))
        role    = get_text(props.get(PROP["role"], {}))
        page_id = page["id"]
        job_url = get_text(props.get(PROP["job_url"], {}))

        log.info(f"[{i}/{len(pages)}] {role} @ {company}")

        job_info = {
            "page_id": page_id,
            "company": company,
            "role":    role,
            "job_url": job_url,
        }

        result = _apply_single(
            notion=notion,
            company=company,
            role=role,
            page_id=page_id,
            job_info=job_info,
            jd_path=jd_path,
            location_key=location_key,
            tailor_only=tailor_only,
            no_notion_update=no_notion_update,
            no_open=no_open,
            batch_mode=True,
        )

        if result:
            succeeded.append(f"{role} @ {company}")
            # Untick the checkbox so it won't be picked up again
            if not no_notion_update:
                try:
                    notion.request(
                        path=f"pages/{page_id}",
                        method="PATCH",
                        body={
                            "properties": {
                                "Queue for apply": {"checkbox": False}
                            }
                        }
                    )
                except Exception as e:
                    log.warning(f"  Could not untick checkbox for {company}: {e}")
        else:
            failed.append(f"{role} @ {company}")

        if i < len(pages):
            time.sleep(2)

    log.info("=" * 60)
    log.info(f"BATCH COMPLETE: {len(succeeded)} succeeded | {len(failed)} failed")
    for s in succeeded:
        log.info(f"  ✓ {s}")
    for f in failed:
        log.info(f"  ✗ {f}")
    log.info("=" * 60)


def _apply_single(
    notion, company, role, page_id, job_info,
    jd_path, location_key, tailor_only,
    no_notion_update, no_open, batch_mode=False,
) -> Path | None:
    """Core single-application logic, shared by single and batch modes."""

    # JD resolution
    jd_text = None
    if jd_path and jd_path.exists():
        jd_text = load_jd_from_file(jd_path)
    if not jd_text:
        job_url = job_info.get("job_url", "")
        jd_text = find_jd_in_csv(job_url, company, role)
    if not jd_text:
        job_url = job_info.get("job_url", "")
        if job_url:
            jd_text = fetch_jd_from_url(job_url)
    if not jd_text and not batch_mode:
        jd_text = paste_jd_interactively(company, role)
    if not jd_text:
        log.error(f"  Could not retrieve JD for {role} @ {company} — skipping")
        return None

    log.info(f"  JD: {len(jd_text):,} chars")

    # Tailor
    draft_path = run_tailoring(
        company=company, role=role, jd_text=jd_text,
        location_key=location_key, tailor_only=tailor_only, no_open=True,
    )
    if not draft_path:
        log.error(f"  Tailoring failed for {role} @ {company}")
        return None

    if tailor_only:
        return draft_path

    # Inject
    docx_path = run_injection(
        draft_path=draft_path, company=company, role=role,
        location_key=location_key, no_open=no_open,
    )
    if not docx_path:
        log.error(f"  Injection failed for {role} @ {company}")
        return None

    # Update Notion
    if page_id and not no_notion_update:
        update_notion_page(notion, page_id, docx_path.name)

    # Auto-generate outreach messages
    try:
        # Ensure project root is on path before importing outreach
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from outreach import generate_outreach
        safe_co     = re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
        safe_role_o = re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]
        co_dir      = DATA_DIR / "tailored" / safe_co
        role_dir_o  = co_dir / safe_role_o

        all_drafts = []
        for d in [role_dir_o, co_dir]:
            if d.exists():
                all_drafts = sorted(
                    [f for f in d.glob("*.txt") if "_OUTREACH" not in f.name],
                    reverse=True
                )
                if all_drafts:
                    break

        if all_drafts:
            log.info(f"Generating outreach from: {all_drafts[0].name}")
            generate_outreach(draft_path=all_drafts[0], company=company, role=role)
        else:
            log.warning(f"No draft .txt found in {role_dir_o} — skipping outreach")
    except Exception as e:
        log.warning(f"Outreach generation failed: {e}")
        import traceback; traceback.print_exc()

    return docx_path


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    notion_url:       str | None,
    company:          str | None,
    role:             str | None,
    location_key:     str = "relocate",
    jd_path:          Path | None = None,
    tailor_only:      bool = False,
    no_notion_update: bool = False,
    no_open:          bool = False,
    batch_mode:       bool = False,
    batch_limit:      int | None = None,
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

    # ── Batch mode ───────────────────────────────────────────────
    if batch_mode:
        run_batch(notion, database_id, location_key,
                  jd_path, tailor_only, no_notion_update,
                  no_open, batch_limit)
        return

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

    docx_path = _apply_single(
        notion=notion,
        company=company,
        role=role,
        page_id=page_id,
        job_info=job_info,
        jd_path=jd_path,
        location_key=location_key,
        tailor_only=tailor_only,
        no_notion_update=no_notion_update,
        no_open=no_open,
        batch_mode=False,
    )

    if not docx_path:
        sys.exit(1)

    log.info("=" * 60)
    log.info("DONE")
    log.info(f"  Resume: {docx_path}")
    if page_id and not no_notion_update:
        log.info(f"  Notion: → Reviewing")
    log.info("")
    log.info("  Review checklist:")
    log.info("  1. Check every bullet for accuracy")
    log.info("  2. Personalise cover letter hook if relevant")
    log.info("  3. Save as PDF before submitting")
    log.info("  4. Update Notion: date_applied + follow_up_date after submitting")
    log.info("=" * 60)


# ----------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="One-command application workflow: tailor → inject → Notion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scraped job from Notion URL:
  python apply.py --notion "https://notion.so/..."

  # External JD from file:
  python apply.py --company "Google" --role "ML Engineer" --jd jobs/google.txt

  # External JD — paste interactively:
  python apply.py --company "Google" --role "ML Engineer"

  # Batch — process all yes+New rows from Notion:
  python apply.py --batch

  # Batch with limit:
  python apply.py --batch --limit 10 --no-open
        """,
    )
    ap.add_argument("--notion",           type=str,  default=None,
                    help="Notion page URL or page ID")
    ap.add_argument("--company",          type=str,  default=None)
    ap.add_argument("--role",             type=str,  default=None)
    ap.add_argument("--jd",              type=Path,  default=None,
                    help="Path to a JD .txt file (for external roles not in scraper)")
    ap.add_argument("--location",         type=str,  default="relocate",
                    help="Location key (default: relocate)")
    ap.add_argument("--batch",            action="store_true",
                    help="Batch mode: process all yes+New Notion rows automatically")
    ap.add_argument("--limit",            type=int,  default=None,
                    help="Max number of roles to process in batch mode")
    ap.add_argument("--tailor-only",      action="store_true",
                    help="Run tailoring only, skip resume injection")
    ap.add_argument("--no-notion-update", action="store_true",
                    help="Don't update Notion page status")
    ap.add_argument("--no-open",          action="store_true",
                    help="Don't open the .docx automatically")

    args = ap.parse_args()
    main(
        notion_url=args.notion,
        company=args.company,
        role=args.role,
        location_key=args.location,
        jd_path=args.jd,
        tailor_only=args.tailor_only,
        no_notion_update=args.no_notion_update,
        no_open=args.no_open,
        batch_mode=args.batch,
        batch_limit=args.limit,
    )