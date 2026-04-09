"""
outreach.py
-----------
Generates a LinkedIn DM and recruiter email from an existing tailored
draft .txt file. Saves both to the company folder alongside the resume.

Can be run standalone or is called automatically by apply.py / edit.py
after the resume is generated.

Usage:
  # From an existing draft:
  python outreach.py --draft "data/tailored/DoorDash/DoorDash_ML_Engineer_2026-04-07.txt"

  # Auto-discover latest draft for a company:
  python outreach.py --company "DoorDash" --role "ML Engineer"

  # With recruiter name (personalises the DM):
  python outreach.py --draft "..." --recruiter "Sarah"

  # Reuse existing JD from draft (default) or supply a new one:
  python outreach.py --draft "..." --jd path/to/jd.txt
"""

import os
import re
import sys
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# PATHS — resolved relative to this file so import always works
# ----------------------------------------------------------------
BASE_DIR  = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Import paths module — try package import, fall back to direct path
try:
    from paths import TAILORED_DIR, LOGS_DIR
except ModuleNotFoundError:
    # Fallback: derive paths directly without the paths module
    _DATA_DIR   = BASE_DIR / "data"
    TAILORED_DIR = _DATA_DIR / "tailored"
    LOGS_DIR     = BASE_DIR / "logs"
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

today_str = date.today().isoformat()

# ----------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / f"outreach_{today_str}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# MODEL
# ----------------------------------------------------------------
MODEL = "claude-sonnet-4-6"

# ----------------------------------------------------------------
# PROMPT
# ----------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert career coach helping a specific job candidate write
concise, authentic recruiter outreach messages. You write two versions:
a LinkedIn DM and a recruiter email. Both must feel genuine, not
templated — they should sound like Aishani wrote them herself.

CANDIDATE: Aishani S. Patil
- MS Computer Science (AI focus), CWRU, graduating May 2026
- 4+ years industry experience as Data Scientist / ML Engineer at IBM/Kyndryl
- Key metrics: 35%+ forecasting improvement, $1M+ NLP savings, $300K+ sentiment engine
- Research: Hebbian learning, NL-to-SPARQL, RAG systems, biologically-inspired NNs
- Referrals at Qualcomm (ML Engineer) and Google (SWE/ML)

LINKEDIN DM RULES:
- 100–150 words maximum — recruiters read on mobile
- CRITICAL: You MUST write actual DM content. Never leave this section empty.
- Lead with a specific hook about the role or company, not "I saw your job posting"
- One concrete credential that proves fit (pick the most relevant metric)
- One specific reason you're interested in this company/team specifically
- Clear ask: a 15-minute call or expressing interest in the role
- Warm but professional tone — not stiff, not overly casual
- No subject line needed
- Do NOT start with "Hi [name]," — start with the hook directly

EMAIL RULES:
- 200–280 words
- Subject line: specific and role-relevant (not "Job Application")
- 3 short paragraphs:
    Para 1: Hook + who you are + why this role/company specifically
    Para 2: Your single strongest proof point for this role (metric + context)
    Para 3: Brief ask + availability + "looking forward to connecting"
- Professional but human — not a cover letter, not a cold sales email
- End with: "Best, Aishani" + contact info

HONESTY RULES:
- Only reference experience that exists in the candidate's profile
- Never claim domain knowledge the candidate doesn't have
- If the candidate has a referral at this company, mention it naturally
- Metrics must come from the profile — do not invent or approximate

OUTPUT FORMAT — follow exactly, the parser depends on it:

---
LINKEDIN DM
---
[DM text — no salutation needed, start with the hook. 100-150 words max.]

---
EMAIL SUBJECT
---
[Subject line only — no "Subject:" prefix]

---
EMAIL BODY
---
[Full email body including salutation and sign-off]

---
WRITER'S NOTES
---
[2–3 sentences on key choices: what hook you used and why,
which credential you led with and why, anything to personalise
before sending]
"""


def build_user_message(
    company: str,
    role: str,
    jd_text: str,
    cover_letter_text: str,
    alignment_analysis: str,
    recruiter_name: str | None,
    has_referral: bool,
) -> str:
    referral_note = (
        f"\nNOTE: Aishani has a referral contact at {company}. "
        "Mention this naturally — e.g. 'I was referred by a colleague' or "
        "'I connected with someone on your team who spoke highly of the work.'"
        if has_referral else ""
    )
    recruiter_note = (
        f"\nRecruiter name: {recruiter_name} — use this in the email salutation."
        if recruiter_name else
        "\nRecruiter name unknown — use 'Hi [Recruiter Name],' as placeholder."
    )

    return f"""Generate a LinkedIn DM and recruiter email for this application.

Company: {company}
Role: {role}
{referral_note}
{recruiter_note}

=== JD SUMMARY (key requirements) ===
{jd_text[:2000]}

=== JD ALIGNMENT ANALYSIS (from resume tailoring) ===
{alignment_analysis}

=== COVER LETTER DRAFT (for tone and hook reference) ===
{cover_letter_text}

Use the cover letter for tone reference and key talking points, but
the LinkedIn DM and email must be distinctly shorter and more direct.
Do not copy sentences from the cover letter verbatim.

CRITICAL: Start your response IMMEDIATELY with the section header below.
Do not write any preamble. Your response must begin with exactly:

---
LINKEDIN DM
---
"""


# ----------------------------------------------------------------
# DRAFT PARSER — extract sections from the tailoring draft
# ----------------------------------------------------------------

def extract_from_draft(draft_path: Path) -> dict:
    """
    Extract JD text, cover letter, and alignment analysis from
    a tailored draft .txt file.

    Draft structure (separated by ={70} lines):
      [0] header block (Company: / Role: / Generated:)
      [1] "SECTION 1 — RESUME BULLET TAILORING" label
      [2] Section 1 content (tailoring output from Claude)
      [3] "SECTION 2 — COVER LETTER DRAFT" label
      [4] Section 2 content (cover letter output from Claude)
      [5] "JOB DESCRIPTION (for reference)" label
      [6] JD text
    """
    text = draft_path.read_text(encoding="utf-8")
    chunks = re.split(r"={40,}", text)

    result = {
        "cover_letter":      "",
        "alignment_analysis": "",
        "jd_text":           "",
        "company":           "",
        "role":              "",
    }

    # Extract company and role from the header block
    company_match = re.search(r"Company:\s*(.+)", text)
    role_match    = re.search(r"Role:\s*(.+)", text)
    if company_match:
        result["company"] = company_match.group(1).strip()
    if role_match:
        result["role"] = role_match.group(1).strip()

    for i, chunk in enumerate(chunks):
        chunk_label = chunk.strip()

        # Section 1 label → next chunk is the tailoring output
        if "SECTION 1" in chunk_label and i + 1 < len(chunks):
            section1 = chunks[i + 1]
            # Try to extract JD alignment analysis block if present
            alignment_match = re.search(
                r"(?:---\s*\n)?JD ALIGNMENT ANALYSIS\s*\n(?:---\s*\n)?(.*?)(?:\n---|\Z)",
                section1,
                re.DOTALL | re.IGNORECASE,
            )
            if alignment_match:
                result["alignment_analysis"] = alignment_match.group(1).strip()
            else:
                # Fall back: use the first paragraph of section 1 as alignment context
                first_para = section1.strip().split("\n\n")[0]
                result["alignment_analysis"] = first_para[:800]

        # Section 2 label → next chunk is the cover letter output
        if "SECTION 2" in chunk_label and i + 1 < len(chunks):
            section2 = chunks[i + 1].strip()
            # Strip any "COVER LETTER DRAFT" header lines Claude may have added
            section2 = re.sub(
                r"^(?:---\s*\n)?COVER LETTER DRAFT\s*\n(?:---\s*\n)?",
                "",
                section2,
                flags=re.IGNORECASE,
            )
            # Extract up to the WRITER'S NOTES block if present
            writer_split = re.split(r"\n---\s*\nWRITER", section2, maxsplit=1, flags=re.IGNORECASE)
            result["cover_letter"] = writer_split[0].strip()

        # JD section label → next chunk is the JD text
        if "JOB DESCRIPTION" in chunk_label and i + 1 < len(chunks):
            result["jd_text"] = chunks[i + 1].strip()[:3000]

    return result


def find_latest_draft(company: str, role: str) -> Path | None:
    """Find the most recent draft .txt for a given company + role."""
    safe_company = re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
    safe_role    = re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]

    # Search in role subfolder first, then company folder (backward compat)
    for search_dir in [
        TAILORED_DIR / safe_company / safe_role,
        TAILORED_DIR / safe_company,
    ]:
        if search_dir.exists():
            matches = sorted(
                [f for f in search_dir.glob("*.txt") if "_OUTREACH" not in f.name],
                reverse=True,
            )
            if matches:
                return matches[0]
    return None


# ----------------------------------------------------------------
# OUTPUT PARSER
# ----------------------------------------------------------------

def parse_outreach_output(text: str) -> dict:
    """
    Parse the structured outreach output from Claude.
    Robust to empty sections, merged sections, and formatting variations.
    Uses a stricter split pattern that requires the header between two --- lines.
    """
    result = {
        "linkedin_dm":   "",
        "email_subject": "",
        "email_body":    "",
        "writers_notes": "",
    }

    # Normalise 3+ dashes to exactly 3
    normalised = re.sub(r"-{3,}", "---", text)
    # Also normalise 2-dash variants
    normalised = re.sub(r"(?m)^--$", "---", normalised)

    # Split on the strict pattern: newline, ---, newline, HEADER, newline, ---
    section_pattern = re.compile(
        r"\n?---\s*\n"
        r"(LINKEDIN DM|EMAIL SUBJECT|EMAIL BODY|WRITER(?:'?S)? NOTES?)\s*"
        r"\n---\s*\n?",
        re.IGNORECASE,
    )

    parts = section_pattern.split(normalised)

    i = 1
    while i < len(parts) - 1:
        header  = parts[i].strip().upper()
        raw_content = parts[i + 1] if i + 1 < len(parts) else ""

        # Strip any accidentally nested section headers out of content
        content = re.sub(
            r"\n?---\s*\n(?:LINKEDIN DM|EMAIL SUBJECT|EMAIL BODY|WRITER(?:'?S)? NOTES?)\s*\n---.*",
            "", raw_content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()

        if "LINKEDIN" in header and content:
            result["linkedin_dm"] = content
        elif "EMAIL SUBJECT" in header and content:
            result["email_subject"] = content
        elif "EMAIL BODY" in header and content:
            result["email_body"] = content
        elif "WRITER" in header and content:
            result["writers_notes"] = content

        i += 2

    # Fallback: extract subject from email body if EMAIL SUBJECT was empty
    if not result["email_subject"] and result["email_body"]:
        subj_match = re.match(r"Subject:\s*(.+)\n", result["email_body"])
        if subj_match:
            result["email_subject"] = subj_match.group(1).strip()
            result["email_body"] = result["email_body"][subj_match.end():].strip()

    # Log what was found to help debug
    for k, v in result.items():
        if v:
            log.debug(f"  parsed {k}: {len(v)} chars")
        else:
            log.warning(f"  parsed {k}: EMPTY")

    return result


# ----------------------------------------------------------------
# SAVE OUTPUT
# ----------------------------------------------------------------

def save_outreach(
    company: str,
    role: str,
    linkedin_dm: str,
    email_subject: str,
    email_body: str,
    writers_notes: str,
) -> Path:
    """Save outreach messages to the company/role subfolder."""
    safe_company = re.sub(r'[/\\:*?"<>|]', '', company.strip())[:50]
    safe_role    = re.sub(r'[/\\:*?"<>|]', '', role.strip()).replace(' ', '_')[:40]
    role_dir     = TAILORED_DIR / safe_company / safe_role
    role_dir.mkdir(parents=True, exist_ok=True)

    role_slug = role.strip().replace(" ", "_").replace("/", "-")[:30]
    out_path  = role_dir / f"{safe_company}_{role_slug}_{today_str}_OUTREACH.txt"

    separator = "\n" + "=" * 70 + "\n"

    content = "\n".join([
        "=" * 70,
        f"OUTREACH MESSAGES",
        f"Company:  {company}",
        f"Role:     {role}",
        f"Date:     {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 70,
        "",
        "*** REVIEW AND PERSONALISE BEFORE SENDING ***",
        "Replace any [placeholder] text. Verify all claims are accurate.",
        "",
        separator,
        "LINKEDIN DM",
        separator,
        linkedin_dm,
        "",
        separator,
        "EMAIL",
        separator,
        f"Subject: {email_subject}",
        "",
        email_body,
        "",
        separator,
        "WRITER'S NOTES",
        separator,
        writers_notes,
    ])

    out_path.write_text(content, encoding="utf-8")
    return out_path


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

def generate_outreach(
    draft_path: Path | None = None,
    company:    str | None = None,
    role:       str | None = None,
    jd_path:    Path | None = None,
    recruiter:  str | None = None,
) -> Path | None:
    """
    Main entry point — usable as a function from apply.py / edit.py
    or run standalone.
    Returns path to the saved outreach file, or None on failure.
    """

    # Resolve draft path
    if draft_path is None:
        if not company or not role:
            log.error("Provide --draft or both --company and --role")
            return None
        draft_path = find_latest_draft(company, role)
        if not draft_path:
            log.error(f"No draft found for {role} @ {company}")
            return None

    if not draft_path.exists():
        log.error(f"Draft not found: {draft_path}")
        return None

    log.info(f"Reading draft: {draft_path.name}")
    extracted = extract_from_draft(draft_path)

    # Override company/role from draft if not provided
    company = company or extracted["company"]
    role    = role    or extracted["role"]

    if not company or not role:
        log.error("Could not determine company or role from draft")
        return None

    # JD override
    jd_text = extracted["jd_text"]
    if jd_path and jd_path.exists():
        jd_text = jd_path.read_text(encoding="utf-8")[:3000]
        log.info(f"JD loaded from {jd_path.name}")

    if not jd_text:
        log.warning("No JD text found — outreach will be less specific")

    if not extracted["cover_letter"]:
        log.warning("No cover letter found in draft — outreach will be generated from JD only")

    # Check for known referrals
    referral_companies = {"qualcomm", "google"}
    has_referral = company.lower() in referral_companies

    log.info(f"Generating outreach for: {role} @ {company}")
    if recruiter:
        log.info(f"Recruiter: {recruiter}")
    if has_referral:
        log.info("Referral noted — will be mentioned in outreach")

    # Call Claude
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    user_msg = build_user_message(
        company=company,
        role=role,
        jd_text=jd_text,
        cover_letter_text=extracted["cover_letter"],
        alignment_analysis=extracted["alignment_analysis"],
        recruiter_name=recruiter,
        has_referral=has_referral,
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        log.debug(f"Raw outreach response (first 300 chars):\n{raw[:300]}")
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return None

    # Parse and save
    parsed   = parse_outreach_output(raw)
    out_path = save_outreach(
        company=company,
        role=role,
        linkedin_dm=parsed["linkedin_dm"],
        email_subject=parsed["email_subject"],
        email_body=parsed["email_body"],
        writers_notes=parsed["writers_notes"],
    )

    log.info(f"Outreach saved → {out_path}")

    # Print to terminal for immediate review
    print()
    print("=" * 60)
    print("LINKEDIN DM")
    print("=" * 60)
    print(parsed["linkedin_dm"])
    print()
    print("=" * 60)
    print(f"EMAIL — Subject: {parsed['email_subject']}")
    print("=" * 60)
    print(parsed["email_body"])
    print()

    return out_path


# ----------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Generate LinkedIn DM + recruiter email from a tailored draft",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From a specific draft:
  python outreach.py --draft "data/tailored/DoorDash/DoorDash_ML_Engineer_2026-04-07.txt"

  # Auto-discover latest draft:
  python outreach.py --company "DoorDash" --role "ML Engineer"

  # With recruiter name:
  python outreach.py --company "DoorDash" --role "ML Engineer" --recruiter "Sarah"

  # With custom JD file:
  python outreach.py --draft "..." --jd jobs/doordash.txt
        """,
    )
    ap.add_argument("--draft",     type=Path, default=None,
                    help="Path to tailored draft .txt file")
    ap.add_argument("--company",   type=str,  default=None)
    ap.add_argument("--role",      type=str,  default=None)
    ap.add_argument("--jd",        type=Path, default=None,
                    help="Optional JD .txt file to override extracted JD")
    ap.add_argument("--recruiter", type=str,  default=None,
                    help="Recruiter's first name for personalisation")

    args = ap.parse_args()
    generate_outreach(
        draft_path=args.draft,
        company=args.company,
        role=args.role,
        jd_path=args.jd,
        recruiter=args.recruiter,
    )