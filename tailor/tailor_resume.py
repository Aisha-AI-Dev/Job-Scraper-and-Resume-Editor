"""
tailor/tailor_resume.py
------------------------
On-demand resume tailoring and cover letter generation for
Aishani's job application pipeline.

What this does:
  1. Takes a company name + JD text (or URL to paste manually)
  2. Calls Claude Sonnet synchronously with the tailoring prompt
  3. Calls Claude Sonnet again with the cover letter prompt
  4. Writes both outputs to data/tailored/COMPANY_ROLE_DATE.txt
  5. Opens the file in your default text editor for review

This is NOT the Batch API — it runs on demand, per role, in real time.
You run this when you've decided a role is worth applying to.
Claude drafts. You review, edit, then paste into your resume template.

Usage:
  python tailor/tailor_resume.py
    (interactive mode — prompts you for company, role, JD)

  python tailor/tailor_resume.py --company "Qualcomm" --role "ML Engineer"
    (still prompts for JD text interactively)

  python tailor/tailor_resume.py \\
    --company "Qualcomm" \\
    --role "ML Engineer Tools" \\
    --jd path/to/jd.txt \\
    --bullets "IBM forecasting,FLAN-T5,BlendedRAG"

  python tailor/tailor_resume.py --company "Google" --role "SWE II ML" \\
    --cover-only    # skip tailoring, just generate cover letter

  python tailor/tailor_resume.py --company "Google" --role "SWE II ML" \\
    --tailor-only   # skip cover letter
"""

import os
import sys
import time
import logging
import argparse
import subprocess
import platform
from datetime import date, datetime
from pathlib import Path

import anthropic
from anthropic import APIStatusError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS
# ----------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent.parent
DATA_DIR     = BASE_DIR / "data"
TAILORED_DIR = DATA_DIR / "tailored"
LOGS_DIR     = BASE_DIR / "logs"
PROMPTS_DIR  = BASE_DIR / "prompts"
PROFILE_FILE = PROMPTS_DIR / "profile.txt"

TAILORED_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
today_str = date.today().isoformat()
log_path  = LOGS_DIR / f"tailor_{today_str}.log"

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
MODEL      = "claude-sonnet-4-6"   # Sonnet for quality — this goes to real apps
MAX_TOKENS = 2048                  # tailoring output can be detailed
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 2.0

# ----------------------------------------------------------------
# LOAD PROMPT FILES
# ----------------------------------------------------------------

def load_file(path: Path, label: str) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"{label} not found at {path}\n"
            f"Make sure you've completed the prompt setup steps."
        )
    return path.read_text(encoding="utf-8").strip()


def load_prompts() -> tuple[str, str, str]:
    """Returns (profile_text, tailoring_system_prompt, cover_letter_system_prompt)."""
    profile   = load_file(PROFILE_FILE,
                          "profile.txt")
    tailoring = load_file(PROMPTS_DIR / "tailoring_system_prompt.txt",
                          "tailoring_system_prompt.txt")
    cover     = load_file(PROMPTS_DIR / "cover_letter_system_prompt.txt",
                          "cover_letter_system_prompt.txt")
    return profile, tailoring, cover


# ----------------------------------------------------------------
# API CALL WITH RETRY
# ----------------------------------------------------------------

def call_claude(
    client: anthropic.Anthropic,
    system_prompt: str,
    user_message: str,
    label: str,
) -> str:
    """
    Synchronous Claude Sonnet call with exponential backoff retry.
    Returns the response text.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"  Calling Claude Sonnet ({label}, attempt {attempt})...")
            start = time.time()

            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            elapsed = time.time() - start
            tokens_in  = response.usage.input_tokens
            tokens_out = response.usage.output_tokens
            cost_est   = (tokens_in * 3 + tokens_out * 15) / 1_000_000

            log.info(
                f"  Done in {elapsed:.1f}s | "
                f"in: {tokens_in:,} | out: {tokens_out:,} | "
                f"~${cost_est:.4f}"
            )
            return response.content[0].text

        except (APIStatusError, APITimeoutError, APIConnectionError) as e:
            if attempt == MAX_RETRIES:
                log.error(f"  {label} failed after {MAX_RETRIES} attempts: {e}")
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(f"  {label} attempt {attempt} failed: {e}. Retry in {delay:.0f}s...")
            time.sleep(delay)


# ----------------------------------------------------------------
# BUILD USER MESSAGES
# ----------------------------------------------------------------

def build_tailoring_message(
    profile: str,
    jd_text: str,
    bullets_to_rewrite: list[str] | None,
    company: str,
    role: str,
) -> str:
    """Build the user message for the tailoring call."""

    bullets_section = ""
    if bullets_to_rewrite:
        formatted = "\n".join(f"  - {b}" for b in bullets_to_rewrite)
        bullets_section = (
            f"\n\nBULLETS TO REWRITE (focus on these specifically):\n"
            f"{formatted}\n\n"
            f"If no bullets are listed above, rewrite the most relevant "
            f"bullets from the candidate's experience for this role."
        )
    else:
        bullets_section = (
            "\n\nNo specific bullets specified. Please identify and rewrite "
            "the 4–6 bullets from the candidate's experience that are most "
            "relevant to this JD, focusing on the strongest matches."
        )

    return (
        f"Target company: {company}\n"
        f"Target role: {role}\n\n"
        f"=== CANDIDATE PROFILE ===\n\n{profile}\n\n"
        f"=== JOB DESCRIPTION ===\n\n{jd_text}"
        f"{bullets_section}"
    )


def build_cover_letter_message(
    profile: str,
    jd_text: str,
    company: str,
    role: str,
    hook_angle: str | None,
) -> str:
    """Build the user message for the cover letter call."""

    angle_section = ""
    if hook_angle:
        angle_section = f"\n\nPREFERRED HOOK ANGLE:\n{hook_angle}"

    return (
        f"Target company: {company}\n"
        f"Target role: {role}\n\n"
        f"=== CANDIDATE PROFILE ===\n\n{profile}\n\n"
        f"=== JOB DESCRIPTION ===\n\n{jd_text}"
        f"{angle_section}"
    )


# ----------------------------------------------------------------
# OUTPUT FILE
# ----------------------------------------------------------------

def build_output_filename(company: str, role: str) -> Path:
    """
    Generate output path: data/tailored/CompanyName/Company_Role_DATE.txt
    Creates the company subfolder if it doesn't exist.
    """
    import re as _re

    def slugify(s: str) -> str:
        return (
            s.strip()
             .replace(" ", "_")
             .replace("/", "-")
             .replace("\\", "-")
             .replace(":", "")
             .replace(",", "")
        )[:40]

    # Company folder: human-readable name, path-safe
    safe_company = _re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
    company_dir  = TAILORED_DIR / safe_company
    company_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{slugify(company)}_{slugify(role)}_{today_str}.txt"
    return company_dir / filename


def write_output(
    output_path: Path,
    company: str,
    role: str,
    jd_text: str,
    tailoring_output: str | None,
    cover_letter_output: str | None,
) -> None:
    """Write the combined tailoring + cover letter output to file."""

    separator = "\n" + "=" * 70 + "\n"

    lines = [
        "=" * 70,
        f"TAILORED APPLICATION DRAFT",
        f"Company:    {company}",
        f"Role:       {role}",
        f"Generated:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Model:      {MODEL}",
        "=" * 70,
        "",
        "*** REVIEW CAREFULLY BEFORE USING ***",
        "Claude drafts. You approve. Never submit raw output.",
        "Edit for accuracy, tone, and fit before pasting into your resume.",
        "",
    ]

    if tailoring_output:
        lines += [
            separator,
            "SECTION 1 — RESUME BULLET TAILORING",
            separator,
            tailoring_output,
            "",
        ]

    if cover_letter_output:
        lines += [
            separator,
            "SECTION 2 — COVER LETTER DRAFT",
            separator,
            cover_letter_output,
            "",
        ]

    lines += [
        separator,
        "JOB DESCRIPTION (for reference)",
        separator,
        jd_text[:3000] + ("..." if len(jd_text) > 3000 else ""),
        "",
    ]

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Output written → {output_path}")


def open_in_editor(path: Path) -> None:
    """Open the output file in the system's default text editor."""
    system = platform.system()
    try:
        if system == "Darwin":      # macOS
            subprocess.run(["open", str(path)], check=True)
        elif system == "Windows":
            subprocess.run(["notepad", str(path)], check=True)
        else:                       # Linux
            # Try common editors in order
            for editor in ["xdg-open", "gedit", "nano", "vim"]:
                try:
                    subprocess.run([editor, str(path)], check=True)
                    break
                except FileNotFoundError:
                    continue
        log.info("Opened output file in editor.")
    except Exception as e:
        log.warning(f"Could not open file automatically: {e}")
        log.info(f"Open manually: {path}")


# ----------------------------------------------------------------
# INTERACTIVE INPUT HELPERS
# ----------------------------------------------------------------

def prompt_multiline(prompt_text: str) -> str:
    """
    Read multiline input from stdin until the user enters 'END'
    on a line by itself.
    """
    print(f"\n{prompt_text}")
    print("(Paste your text, then type END on a new line and press Enter)\n")
    lines = []
    while True:
        try:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
        except EOFError:
            break
    return "\n".join(lines).strip()


def prompt_input(prompt_text: str, default: str = "") -> str:
    """Single-line input with optional default."""
    if default:
        result = input(f"{prompt_text} [{default}]: ").strip()
        return result if result else default
    return input(f"{prompt_text}: ").strip()


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def main(
    company: str | None = None,
    role: str | None = None,
    jd_source: Path | None = None,
    bullets: list[str] | None = None,
    hook_angle: str | None = None,
    cover_only: bool = False,
    tailor_only: bool = False,
    no_open: bool = False,
) -> None:

    log.info("=" * 60)
    log.info(f"Tailor starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # 1. Gather inputs interactively if not provided via CLI
    if not company:
        company = prompt_input("Company name")
    if not role:
        role = prompt_input("Role title")

    if jd_source and jd_source.exists():
        jd_text = jd_source.read_text(encoding="utf-8").strip()
        log.info(f"Loaded JD from file: {jd_source}")
    else:
        jd_text = prompt_multiline("Paste the full job description:")

    if not jd_text:
        log.error("No JD text provided. Exiting.")
        sys.exit(1)

    if not company or not role:
        log.error("Company and role are required. Exiting.")
        sys.exit(1)

    log.info(f"Target: {role} @ {company} | JD length: {len(jd_text):,} chars")

    # 2. Load prompts and profile
    log.info("Loading prompt files...")
    profile, tailoring_system, cover_system = load_prompts()

    # 3. Init Claude client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in .env file")
    client = anthropic.Anthropic(api_key=api_key)

    tailoring_output    = None
    cover_letter_output = None

    # 4. Resume tailoring call
    if not cover_only:
        log.info("Running resume tailoring...")
        user_msg = build_tailoring_message(
            profile, jd_text, bullets, company, role
        )
        tailoring_output = call_claude(
            client, tailoring_system, user_msg, "tailoring"
        )

    # 5. Cover letter call
    if not tailor_only:
        log.info("Running cover letter generation...")
        user_msg = build_cover_letter_message(
            profile, jd_text, company, role, hook_angle
        )
        cover_letter_output = call_claude(
            client, cover_system, user_msg, "cover letter"
        )

    # 6. Write output file
    output_path = build_output_filename(company, role)
    write_output(
        output_path,
        company,
        role,
        jd_text,
        tailoring_output,
        cover_letter_output,
    )

    # 7. Open in editor (human review checkpoint)
    if not no_open:
        log.info("Opening output in editor for your review...")
        open_in_editor(output_path)

    # 8. Summary
    log.info("=" * 60)
    log.info("DONE")
    log.info(f"  Output file: {output_path}")
    log.info("  Next steps:")
    log.info("  1. Review the tailored bullets for accuracy")
    log.info("  2. Edit anything that doesn't sound like you")
    log.info("  3. Paste into your resume template (.docx)")
    log.info("  4. Review the cover letter draft")
    log.info("  5. Personalise the hook if you know the hiring manager")
    log.info("  6. Update Notion: set resume_version and date_applied")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tailor resume and generate cover letter for a specific role",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tailor/tailor_resume.py
      (fully interactive — prompts for everything)

  python tailor/tailor_resume.py --company "Qualcomm" --role "ML Engineer Tools"
      (prompts for JD text interactively)

  python tailor/tailor_resume.py --company "Google" --role "SWE II ML" --jd jd.txt
      (loads JD from file)

  python tailor/tailor_resume.py --company "Cleveland Clinic" --role "Data Scientist" \\
    --bullets "IBM forecasting pipeline" "FLAN-T5 fine-tuning" "churn modeling"
      (focuses tailoring on specific bullets)

  python tailor/tailor_resume.py --company "Palantir" --role "FDE" --cover-only
      (cover letter only, skip bullet tailoring)
        """,
    )
    parser.add_argument("--company",     type=str,  default=None)
    parser.add_argument("--role",        type=str,  default=None)
    parser.add_argument("--jd",          type=Path, default=None,
                        help="Path to a .txt file containing the JD")
    parser.add_argument("--bullets",     type=str,  nargs="+", default=None,
                        help="Specific bullet topics to focus tailoring on")
    parser.add_argument("--hook",        type=str,  default=None,
                        help="Preferred angle for the cover letter hook")
    parser.add_argument("--cover-only",  action="store_true",
                        help="Generate cover letter only, skip bullet tailoring")
    parser.add_argument("--tailor-only", action="store_true",
                        help="Tailor bullets only, skip cover letter")
    parser.add_argument("--no-open",     action="store_true",
                        help="Don't open the output file automatically")

    args = parser.parse_args()
    main(
        company=args.company,
        role=args.role,
        jd_source=args.jd,
        bullets=args.bullets,
        hook_angle=args.hook,
        cover_only=args.cover_only,
        tailor_only=args.tailor_only,
        no_open=args.no_open,
    )