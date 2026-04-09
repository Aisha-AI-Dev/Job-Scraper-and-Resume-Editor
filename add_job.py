"""
add_job.py
----------
Manually add a job to your Notion tracker without going through
the scraper or scorer. Useful for:
  - Roles you found on a company's careers page directly
  - Referral roles shared by a contact
  - Jobs from newsletters, Slack communities, etc.

The Notion page is created instantly with whatever you provide.
Scoring fields (fit score, matched skills etc.) are left blank
for you to fill in manually, or just use it to track the role.

Usage:
  # Interactive — prompts for everything:
  python add_job.py

  # With CLI args:
  python add_job.py --company "Anthropic" --role "Research Engineer" --url "https://..."

  # With JD from file:
  python add_job.py --company "Anthropic" --role "Research Engineer" --jd jobs/anthropic.txt

  # With JD paste (type END on new line when done):
  python add_job.py --company "Anthropic" --role "Research Engineer" --paste-jd

  # Mark as already applied:
  python add_job.py --company "Anthropic" --role "Research Engineer" --status Applied
"""

import os
import sys
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

from notion_client import Client as NotionClient
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    from paths import LOGS_DIR, RAW_DIR
except ModuleNotFoundError:
    LOGS_DIR = BASE_DIR / "logs"
    RAW_DIR  = BASE_DIR / "data" / "raw"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

today_str = date.today().isoformat()

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / f"add_job_{today_str}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# NOTION PROPERTY MAP — must match your DB exactly
# ----------------------------------------------------------------
PROP = {
    "role":             "Role",
    "company":          "Company",
    "fit_score":        "Fit score",
    "tier":             "Tier",
    "role_family":      "Role family",
    "apply_rec":        "Apply recommendation",
    "overqualified":    "Overqualified",
    "sponsorship":      "Sponsorship status",
    "matched_skills":   "Matched skills",
    "missing_skills":   "Missing skills",
    "strengths":        "Transferable strengths",
    "status":           "Status",
    "resume_version":   "Resume version",
    "date_applied":     "Date applied",
    "follow_up":        "Follow-up date",
    "notes":            "Notes",
    "job_url":          "Job URL",
    "site":             "Site",
    "date_posted":      "Date posted",
    "scored_at":        "Scored at",
    "description":      "Description",
    "queue":            "Queue for apply",
}

VALID_STATUSES = ["New", "Reviewing", "Applied", "Interviewing", "Rejected", "Offer"]


# ----------------------------------------------------------------
# NOTION PROPERTY BUILDERS
# ----------------------------------------------------------------

def prop_title(v: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": str(v)[:2000]}}]}

def prop_text(v: str) -> dict:
    s = str(v).strip() if v else ""
    return {"rich_text": [{"type": "text", "text": {"content": s[:2000]}}] if s else []}

def prop_url(v: str) -> dict:
    s = str(v).strip() if v else ""
    return {"url": s if s and s not in ("nan", "none", "") else None}

def prop_select(v: str) -> dict:
    s = str(v).strip() if v else ""
    return {"select": {"name": s} if s else None}

def prop_checkbox(v: bool) -> dict:
    return {"checkbox": bool(v)}

def prop_date(v: str) -> dict:
    return {"date": {"start": v} if v else None}


# ----------------------------------------------------------------
# INPUT HELPERS
# ----------------------------------------------------------------

def prompt_required(label: str, current: str = "") -> str:
    """Prompt user for a required field, showing current value if set."""
    display = f" [{current}]" if current else ""
    while True:
        val = input(f"  {label}{display}: ").strip()
        if val:
            return val
        if current:
            return current
        print(f"    ✗ {label} is required.")


def prompt_optional(label: str, default: str = "") -> str:
    """Prompt user for an optional field."""
    display = f" [{default}]" if default else " (optional, Enter to skip)"
    val = input(f"  {label}{display}: ").strip()
    return val or default


def paste_jd() -> str:
    """Prompt user to paste JD text interactively."""
    print()
    print("  Paste the job description below.")
    print("  When done, type END on a new line and press Enter.")
    print("  " + "-" * 50)
    lines = []
    try:
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        pass
    return "\n".join(lines).strip()


# ----------------------------------------------------------------
# CORE LOGIC
# ----------------------------------------------------------------

def build_page(
    database_id: str,
    company: str,
    role: str,
    job_url: str,
    description: str,
    status: str,
    notes: str,
    site: str,
    date_posted: str,
    queue: bool,
) -> dict:
    """Build the Notion page creation payload."""
    return {
        "parent": {"database_id": database_id},
        "properties": {
            PROP["role"]:           prop_title(role),
            PROP["company"]:        prop_text(company),
            PROP["job_url"]:        prop_url(job_url),
            PROP["description"]:    prop_text(description[:2000] if description else ""),
            PROP["status"]:         prop_select(status),
            PROP["notes"]:          prop_text(notes),
            PROP["site"]:           prop_text(site or "Manual"),
            PROP["date_posted"]:    prop_text(date_posted or today_str),
            PROP["scored_at"]:      prop_text(f"Manual entry — {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
            PROP["queue"]:          prop_checkbox(queue),
            # Leave scoring fields blank — fill manually in Notion
            PROP["fit_score"]:      {"number": None},
            PROP["tier"]:           prop_select(""),
            PROP["role_family"]:    prop_select(""),
            PROP["apply_rec"]:      prop_text(""),
            PROP["overqualified"]:  prop_checkbox(False),
            PROP["sponsorship"]:    prop_select(""),
            PROP["matched_skills"]: prop_text(""),
            PROP["missing_skills"]: prop_text(""),
            PROP["strengths"]:      prop_text(""),
            PROP["resume_version"]: prop_text(""),
            PROP["date_applied"]:   prop_date(""),
            PROP["follow_up"]:      prop_date(""),
        }
    }


def add_to_notion(
    company: str,
    role: str,
    job_url: str = "",
    description: str = "",
    status: str = "New",
    notes: str = "",
    site: str = "",
    date_posted: str = "",
    queue: bool = False,
) -> bool:
    """Create the Notion page. Returns True on success."""
    notion_token = os.getenv("NOTION_TOKEN")
    database_id  = os.getenv("NOTION_DATABASE_ID")

    if not notion_token:
        log.error("NOTION_TOKEN not set in .env")
        return False
    if not database_id:
        log.error("NOTION_DATABASE_ID not set in .env")
        return False

    notion = NotionClient(auth=notion_token)

    page = build_page(
        database_id=database_id,
        company=company,
        role=role,
        job_url=job_url,
        description=description,
        status=status,
        notes=notes,
        site=site,
        date_posted=date_posted,
        queue=queue,
    )

    try:
        notion.request(path="pages", method="POST", body=page)
        log.info(f"✓ Added to Notion: {role} @ {company} (status: {status})")
        return True
    except Exception as e:
        log.error(f"Failed to create Notion page: {e}")
        return False


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    company:     str | None = None,
    role:        str | None = None,
    job_url:     str = "",
    jd_path:     Path | None = None,
    do_paste_jd: bool = False,
    status:      str = "New",
    notes:       str = "",
    site:        str = "",
    date_posted: str = "",
    queue:       bool = False,
    interactive: bool = False,
) -> None:

    log.info("=" * 60)
    log.info(f"add_job.py — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # Interactive mode if no company/role provided
    if interactive or not company or not role:
        print()
        print("  Add a job to Notion manually.")
        print("  Required fields marked with *")
        print()
        company     = prompt_required("Company *", company or "")
        role        = prompt_required("Role *", role or "")
        job_url     = prompt_optional("Job URL", job_url)
        status      = prompt_optional(
            f"Status [{'/'.join(VALID_STATUSES)}]", status or "New"
        )
        if status not in VALID_STATUSES:
            log.warning(f"Unknown status '{status}' — defaulting to New")
            status = "New"
        queue_input = prompt_optional("Queue for apply? [y/N]", "n")
        queue       = queue_input.lower() in ("y", "yes")
        notes       = prompt_optional("Notes")
        site        = prompt_optional("Source (e.g. LinkedIn, Company site)", "Manual")
        date_posted = prompt_optional("Date posted [YYYY-MM-DD]", today_str)

        if not jd_path and not do_paste_jd:
            jd_choice = prompt_optional(
                "Add job description? [file path / paste / skip]", "skip"
            )
            if jd_choice.lower() == "paste":
                do_paste_jd = True
            elif jd_choice.lower() not in ("skip", "", "s"):
                jd_path = Path(jd_choice)

    # Load JD text
    description = ""
    if jd_path:
        if not jd_path.exists():
            log.error(f"JD file not found: {jd_path}")
        else:
            description = jd_path.read_text(encoding="utf-8").strip()
            log.info(f"JD loaded from {jd_path.name} ({len(description):,} chars)")
    elif do_paste_jd:
        description = paste_jd()
        if description:
            log.info(f"JD pasted ({len(description):,} chars)")

    # Validate status
    if status not in VALID_STATUSES:
        log.warning(f"Unknown status '{status}' — defaulting to New")
        status = "New"

    # Summary before pushing
    print()
    print("  " + "─" * 50)
    print(f"  Role:    {role}")
    print(f"  Company: {company}")
    print(f"  URL:     {job_url or '(none)'}")
    print(f"  Status:  {status}")
    print(f"  Queue:   {'Yes' if queue else 'No'}")
    print(f"  JD:      {f'{len(description):,} chars' if description else '(none)'}")
    if notes:
        print(f"  Notes:   {notes}")
    print("  " + "─" * 50)
    print()

    confirm = input("  Add to Notion? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        log.info("Cancelled.")
        return

    success = add_to_notion(
        company=company,
        role=role,
        job_url=job_url,
        description=description,
        status=status,
        notes=notes,
        site=site or "Manual",
        date_posted=date_posted or today_str,
        queue=queue,
    )

    if success:
        print()
        print("  ✓ Job added to Notion.")
        print(f"  Open Notion to fill in fit score, skills, and other fields.")
        if queue:
            print(f"  'Queue for apply' is ticked — run `python apply.py --batch` when ready.")
        print()


# ----------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Add a job manually to your Notion tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fully interactive:
  python add_job.py

  # With CLI args:
  python add_job.py --company "Anthropic" --role "Research Engineer" --url "https://..."

  # With JD from file:
  python add_job.py --company "Anthropic" --role "Research Engineer" --jd jobs/anthropic.txt

  # With JD paste:
  python add_job.py --company "Anthropic" --role "Research Engineer" --paste-jd

  # Already applied:
  python add_job.py --company "Anthropic" --role "Research Engineer" --status Applied

  # Queue immediately for apply.py --batch:
  python add_job.py --company "Anthropic" --role "Research Engineer" --queue
        """,
    )
    ap.add_argument("--company",   type=str,  default=None)
    ap.add_argument("--role",      type=str,  default=None)
    ap.add_argument("--url",       type=str,  default="",
                    help="Job posting URL")
    ap.add_argument("--jd",        type=Path, default=None,
                    help="Path to JD .txt file")
    ap.add_argument("--paste-jd",  action="store_true",
                    help="Paste JD interactively")
    ap.add_argument("--status",    type=str,  default="New",
                    choices=VALID_STATUSES,
                    help="Initial status (default: New)")
    ap.add_argument("--notes",     type=str,  default="",
                    help="Notes to add to the Notion page")
    ap.add_argument("--site",      type=str,  default="Manual",
                    help="Source (e.g. LinkedIn, Company site, Referral)")
    ap.add_argument("--date",      type=str,  default="",
                    help="Date posted YYYY-MM-DD (default: today)")
    ap.add_argument("--queue",     action="store_true",
                    help="Tick 'Queue for apply' checkbox immediately")

    args = ap.parse_args()

    # If no company or role given, go interactive
    interactive = not args.company or not args.role

    main(
        company=args.company,
        role=args.role,
        job_url=args.url,
        jd_path=args.jd,
        do_paste_jd=args.paste_jd,
        status=args.status,
        notes=args.notes,
        site=args.site,
        date_posted=args.date,
        queue=args.queue,
        interactive=interactive,
    )