# Job Application Pipeline

A semi-automated job application pipeline I built while searching for ML/AI roles during my MS program at Case Western Reserve University. It handles the mechanical parts of job searching — scraping, scoring, tracking, resume tailoring, cover letters, and recruiter outreach — so I can spend my time on the parts that actually need human judgment.

**Built by [Aishani S. Patil](https://linkedin.com/in/aishani-patil) · MS Computer Science (AI), CWRU · May 2026**

---

## What It Does

```
Every morning, automatically:
  Scrape → Filter → Score → Push to Notion (with JD preview)

When you're ready to apply (one command):
  Tailor resume + Cover letter + LinkedIn DM + Recruiter email → Review → Submit
```

| Stage | Tool | What happens |
|---|---|---|
| **Scrape** | JobSpy | Pulls jobs from LinkedIn, Indeed, Glassdoor by search term |
| **Filter** | Custom blacklist | Removes clearance-required, 10+ year, intern roles |
| **Score** | Claude Haiku (Batch API) | Rates each job 0–10 against your profile and tier priorities |
| **Track** | Notion API | Pushes scored jobs with fit scores, skills analysis, and JD preview |
| **Tailor** | Claude Sonnet | ATS-optimised resume bullets mirroring the JD's exact language |
| **Cover letter** | Claude Sonnet | Role-specific, hook-first cover letter with writer's notes |
| **Outreach** | Claude Sonnet | LinkedIn DM (~150 words) + recruiter email, auto-generated |
| **Inject** | python-docx | Writes tailored content into `.docx` template, formatting preserved |

---

## Architecture

```
run_daily.py          ← Orchestrator (scrape → score → push)
├── scrapers/
│   └── jobspy_scraper.py       ← JobSpy wrapper with dedup + blacklist
├── scorer/
│   └── score_jobs.py           ← Claude Haiku via Batch API (~$2.50/1500 jobs)
├── tracker/
│   └── push_to_notion.py       ← Notion API integration
├── tailor/
│   ├── tailor_resume.py        ← Claude Sonnet resume + cover letter
│   └── inject_resume.py        ← python-docx paragraph injection
├── apply.py                    ← Single or batch trigger (resume + cover letter + outreach)
├── edit.py                     ← External JD trigger (no Notion needed)
├── add_job.py                  ← Manually add any job to Notion
├── outreach.py                 ← Standalone LinkedIn DM + recruiter email generator
└── recover_batch.py            ← Batch result recovery if scorer is interrupted
```

### Scoring System

Each job is scored against a structured candidate profile and tiered role targets using Claude Haiku. Hard constraints are enforced (no security clearance, no 10+ years required, no explicit no-sponsorship). Scoring is tier-weighted:

- **Tier 1** (ML Engineer, Data Scientist, Research Scientist): full weight
- **Tier 2** (Data Engineer, MLOps, Applied Scientist): 0.85x weight
- **Tier 3** (Data Analyst, Forward Deployed Engineer): 0.80x weight

Jobs ≥7.0 → `yes`. Between 4.0–7.0 → `manual_review`. Below 4.0 → filtered out.

The scorer uses the **Batch API** for 50% cost reduction and **prompt caching** to avoid re-sending the large system prompt on every request.

### ATS-Optimised Tailoring

The tailoring prompt runs a structured keyword audit before writing anything:

1. Extracts the exact role title and injects it verbatim into the resume summary
2. Identifies the top 15 hard skills, 5 soft skills, and 5 domain terms from the JD
3. Marks each as *In Profile / Transferable / Genuine Gap*
4. Ensures every matched keyword appears verbatim in the output
5. Uses the skills section as a keyword bank — adds JD terms the candidate genuinely has
6. Runs a self-verification pass before finalising

Tested at **80/100 on Jobscan** for a competitive ML Engineer role (up from 47 before ATS optimisation was added). Genuine gaps are listed honestly and suggested for cover letter treatment.

### Resume Injection

The tailoring prompt specifies how many bullets to write per section based on role type. The injector reads the draft, counts bullets, and expands each `{{PLACEHOLDER}}` to exactly that many XML-cloned paragraphs — preserving numbering, indentation, fonts, and hyperlinks from the source template. Project titles link to GitHub automatically.

### Per-Application Output

Every `apply.py` or `edit.py` run produces three files in `data/tailored/CompanyName/`:

```
Company_Role_DATE.txt           ← Bullets + ATS audit + cover letter + gaps analysis
Company_Role_DATE_RESUME.docx   ← Tailored resume, ready to convert to PDF
Company_Role_DATE_OUTREACH.txt  ← LinkedIn DM (~150 words) + recruiter email
```

---

## Stack

- **Python 3.11+**
- **[Anthropic API](https://docs.anthropic.com)** — Claude Haiku (scoring) + Claude Sonnet (tailoring + outreach)
- **[JobSpy](https://github.com/Bunsly/JobSpy)** — multi-site job scraping
- **[notion-client](https://github.com/ramnes/notion-sdk-py)** — Notion database integration (v3.x)
- **[python-docx](https://python-docx.readthedocs.io)** — `.docx` XML manipulation
- **pandas** — CSV processing
- **python-dotenv** — environment variable management

---

## Results (First Run)

- **1,494 jobs scraped** across LinkedIn, Indeed, Glassdoor in one run
- **1,302 successfully scored** (192 truncated JSON — fixed by increasing `max_tokens`)
- **342 jobs** above threshold pushed to Notion
- **108 `yes` recommendations** including roles at Adobe, Amazon, Meta, Netflix, Apple, Zillow, Glean, Capital One, JPMorgan
- **Scoring cost**: ~$2.34 for 1,494 jobs using Batch API + prompt caching
- **Per-application cost**: ~$0.08–0.15 (resume tailoring + cover letter + outreach)
- **ATS score**: 80/100 on Jobscan for a competitive ML Engineer role

---

## Can I Use This?

Yes, with the understanding that **this pipeline is personalised for my job search** and will need meaningful changes to work for yours.

**You'll need to replace:**
- `profile.txt` — your experience, education, skills, and projects
- `prompts/target_roles.txt` — your tier priorities and hard constraints
- `prompts/scoring_system_prompt.txt` — scoring weights tuned to your situation
- `prompts/tailoring_system_prompt.txt` — bullet count rules per role type
- `resume_template.docx` — your resume with `{{SECTION}}` placeholders
- `tailor/inject_resume.py` → `PROJECT_URLS` dict — your GitHub links
- `outreach.py` → `SYSTEM_PROMPT` candidate block — your background summary

**You'll need to set up:**
- Anthropic API account and key
- Notion integration and database with the required schema (22 properties)
- A `.env` file with your credentials

If you're comfortable with Python and APIs, the architecture is clean enough to adapt. `USER_GUIDE.md` in the repo documents the complete setup process including the Notion schema.

> **Note:** This is not a plug-and-play tool. It's a personal project I'm sharing because the architecture might be useful to others building something similar.

---

## Setup Overview

```bash
git clone https://github.com/Aisha-AI-Dev/Job_Application
cd Job_Application
python -m venv myenv && source myenv/bin/activate
pip install anthropic notion-client jobspy pandas python-docx python-dotenv requests beautifulsoup4
```

Create `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
NOTION_DATABASE_ID=your_32_char_database_id
```

See `USER_GUIDE.md` for the complete walkthrough: Notion schema, resume template setup, first run instructions.

---

## Daily Usage (Once Set Up)

```bash
# Morning pipeline (or automate with cron at 8am):
python run_daily.py

# In Notion: sort by fit score, tick "Queue for apply" on roles you want, then:
python apply.py --batch
# → tailored resume + cover letter + LinkedIn DM + recruiter email for each role

# For roles you found yourself (not from the scraper):
python edit.py --company "Google" --role "ML Engineer" --jd jd.txt

# Add a referral role directly to Notion and queue it:
python add_job.py --company "Qualcomm" --role "ML Engineer" --url "..." --queue
```

---

## Project Background

I built this in April 2026 while finishing my MS and running an active job search simultaneously. The goal was to apply to 50+ roles per day without sacrificing quality: no generic resumes, but also no spending 45 minutes per application hand-tailoring bullets and writing outreach from scratch.

The pipeline handles volume. I handle the quality check. Every `.docx` gets reviewed before it goes anywhere, every outreach message gets read before it's sent.

The tailoring prompt enforces strict honesty guardrails — it cannot invent metrics, upgrade roles, or claim tools that aren't in the profile. It can only reframe real experience in the JD's exact language, and it lists every genuine gap honestly in the output so I know what to address in the cover letter.

---

## Contact

**Aishani S. Patil**  
[linkedin.com/in/aishani-patil](https://linkedin.com/in/aishani-patil) · [github.com/Aisha-AI-Dev](https://github.com/Aisha-AI-Dev) · patil.aishani@gmail.com

---

*If you build something similar using this as a starting point, I'd love to hear about it.*
