"""
edit.py
-------
The shortest path from a job description to a tailored resume.
No scraper, no Notion, no CSV — just paste a JD and get a .docx.

Usage:
  # Paste JD interactively:
  python edit.py --company "Google" --role "ML Engineer"

  # Point to a saved JD file:
  python edit.py --company "Google" --role "ML Engineer" --jd jobs/google.txt

  # With location override (default: relocate):
  python edit.py --company "Google" --role "ML Engineer" --location cleveland

  # Tailor only — skip the .docx injection:
  python edit.py --company "Google" --role "ML Engineer" --tailor-only
"""

import sys
import logging
import argparse
import importlib.util
from datetime import date, datetime
from pathlib import Path

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
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

LOCATION_MAP = {
    "cleveland": "Cleveland, OH",
    "relocate":  "Open to Relocate",
    "remote":    "Remote",
    "houston":   "Houston, TX",
    "seattle":   "Seattle, WA",
    "sf":        "San Francisco, CA",
    "nyc":       "New York, NY",
    "boston":    "Boston, MA",
    "austin":    "Austin, TX",
    "chicago":   "Chicago, IL",
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_jd(jd_path: Path | None, company: str, role: str) -> str:
    """Load JD from file, or prompt for interactive paste."""

    # From file
    if jd_path:
        if not jd_path.exists():
            log.error(f"JD file not found: {jd_path}")
            sys.exit(1)
        text = jd_path.read_text(encoding="utf-8").strip()
        if not text:
            log.error(f"JD file is empty: {jd_path}")
            sys.exit(1)
        log.info(f"JD loaded from {jd_path.name} ({len(text):,} chars)")
        return text

    # Interactive paste
    print()
    print("=" * 60)
    print(f"  {role} @ {company}")
    print("  Paste the job description below.")
    print("  When done, type END on a new line and press Enter.")
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
        log.error("No JD text provided — exiting")
        sys.exit(1)
    log.info(f"JD received ({len(text):,} chars)")
    return text


def main(
    company:     str,
    role:        str,
    jd_path:     Path | None = None,
    location:    str = "relocate",
    tailor_only: bool = False,
    no_open:     bool = False,
) -> None:

    log.info("=" * 60)
    log.info(f"edit.py — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"  {role} @ {company}")
    log.info("=" * 60)

    # 1. Get JD
    jd_text = get_jd(jd_path, company, role)

    # 2. Write JD to temp file for tailor module
    import re, os
    tmp_jd = DATA_DIR / f"_tmp_jd_{today_str}.txt"
    tmp_jd.write_text(jd_text, encoding="utf-8")

    # 3. Tailor
    tailor_path = BASE_DIR / "tailor" / "tailor_resume.py"
    if not tailor_path.exists():
        log.error(f"tailor_resume.py not found at {tailor_path}")
        sys.exit(1)

    log.info("Tailoring resume...")
    try:
        tailor = load_module("tailor_resume", tailor_path)
        tailor.main(
            company=company,
            role=role,
            jd_source=tmp_jd,
            bullets=None,
            hook_angle=None,
            cover_only=False,
            tailor_only=tailor_only,
            no_open=True,
        )
    except Exception as e:
        log.error(f"Tailoring failed: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        tmp_jd.unlink(missing_ok=True)

    # Compute company and role dirs (used throughout)
    safe_company = re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
    safe_role    = re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]
    company_dir  = DATA_DIR / "tailored" / safe_company
    role_dir     = company_dir / safe_role

    if tailor_only:
        # Search role subfolder first, fall back to company dir
        for search in [role_dir, company_dir]:
            drafts = sorted(search.glob("*.txt"), reverse=True)
            if drafts:
                log.info(f"Draft → {drafts[0]}")
                break
        log.info("Tailoring complete. Skipping injection (--tailor-only).")
        return

    # 4. Find draft — role subfolder first, then company dir (backward compat)
    draft_path = None
    for search in [role_dir, company_dir]:
        drafts = sorted(
            [f for f in search.glob("*.txt") if "_OUTREACH" not in f.name],
            reverse=True,
        )
        if drafts:
            draft_path = drafts[0]
            break

    if not draft_path:
        log.error(f"No draft .txt found in {role_dir} or {company_dir} — injection cannot proceed")
        sys.exit(1)
    log.info(f"Draft → {draft_path.name}")

    # 5. Inject
    inject_path = BASE_DIR / "tailor" / "inject_resume.py"
    if not inject_path.exists():
        log.error(f"inject_resume.py not found at {inject_path}")
        sys.exit(1)

    log.info("Injecting into resume template...")
    try:
        inject = load_module("inject_resume", inject_path)
        inject.main(
            draft_path=draft_path,
            company=company,
            role=role,
            location_key=location,
            no_open=no_open,
        )
    except Exception as e:
        log.error(f"Injection failed: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    # 6. Find output docx — role subfolder first, then company dir
    docx_files = []
    for search in [role_dir, company_dir]:
        docx_files = sorted(search.glob("*_RESUME.docx"), reverse=True)
        if docx_files:
            break

    if docx_files:
        log.info("=" * 60)
        log.info("DONE")
        log.info(f"  Resume → {docx_files[0]}")
        log.info("  Review, save as PDF, submit.")
        log.info("=" * 60)

    # Auto-generate outreach messages
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from outreach import generate_outreach

        all_drafts = []
        for d in [role_dir, company_dir]:
            if d.exists():
                all_drafts = sorted(
                    [f for f in d.glob("*.txt") if "_OUTREACH" not in f.name],
                    reverse=True,
                )
                if all_drafts:
                    break

        if all_drafts:
            log.info(f"Generating outreach from: {all_drafts[0].name}")
            generate_outreach(draft_path=all_drafts[0], company=company, role=role)
        else:
            log.warning("No draft .txt found — skipping outreach")
    except Exception as e:
        log.warning(f"Outreach generation failed: {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Tailor + inject resume from a manual JD — no Notion needed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Paste JD interactively:
  python edit.py --company "Google" --role "ML Engineer"

  # From a saved JD file:
  python edit.py --company "Google" --role "ML Engineer" --jd jobs/google_jd.txt

  # Location override:
  python edit.py --company "Google" --role "ML Engineer" --location sf

  # Tailor only (review draft before injecting):
  python edit.py --company "Google" --role "ML Engineer" --tailor-only
        """,
    )
    ap.add_argument("--company",      required=True,  type=str)
    ap.add_argument("--role",         required=True,  type=str)
    ap.add_argument("--jd",           type=Path,      default=None,
                    help="Path to JD .txt file (omit to paste interactively)")
    ap.add_argument("--location",     type=str,       default="relocate",
                    help="Location key: relocate/cleveland/remote/sf/nyc/etc")
    ap.add_argument("--tailor-only",  action="store_true",
                    help="Stop after tailoring — don't inject into .docx")
    ap.add_argument("--no-open",      action="store_true",
                    help="Don't open the output .docx automatically")

    args = ap.parse_args()
    main(
        company=args.company,
        role=args.role,
        jd_path=args.jd,
        location=args.location,
        tailor_only=args.tailor_only,
        no_open=args.no_open,
    )