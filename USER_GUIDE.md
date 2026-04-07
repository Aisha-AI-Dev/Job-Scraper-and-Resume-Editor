# Job Application Pipeline — User Guide

## What Is This?

This is a semi-automated job application pipeline built for Aishani Patil (MS CS, CWRU, May 2026). It handles the mechanical parts of job searching so you can focus on the parts that need human judgment: picking which roles to apply to and reviewing your tailored resume before submitting.

**What it does automatically:**
- Scrapes hundreds of job postings from LinkedIn, Indeed, and Glassdoor every morning
- Scores each job against your profile using Claude AI (fit score 0–10)
- Pushes the scored jobs into a Notion database for you to review
- Tailors your resume bullets to match each job's language and priorities
- Injects tailored content into your resume `.docx` template

**What you do manually:**
- Review scored jobs in Notion and tick the ones you want to apply to
- Review each tailored `.docx` before submitting
- Save as PDF and submit on the company's portal

---

## Project Structure

```
Job_Application/
├── apply.py                  ← Main application trigger (Notion-based)
├── edit.py                   ← Quick editor for external JDs
├── run_daily.py              ← Daily pipeline orchestrator
├── paths.py                  ← Shared directory paths
├── profile.txt               ← Your master candidate profile
│
├── scrapers/
│   └── jobspy_scraper.py     ← Job scraping (LinkedIn, Indeed, Glassdoor)
│
├── scorer/
│   └── score_jobs.py         ← AI scoring with Claude Haiku (Batch API)
│
├── tracker/
│   └── push_to_notion.py     ← Pushes scored jobs to Notion database
│
├── tailor/
│   ├── tailor_resume.py      ← AI resume tailoring with Claude Sonnet
│   └── inject_resume.py      ← Injects tailored content into .docx template
│
├── recover_batch.py          ← Recovers results if scorer is interrupted
│
├── prompts/
│   ├── profile.txt           ← Your candidate profile (facts only)
│   ├── target_roles.txt      ← Tier 1/2/3 role priorities and scoring weights
│   ├── scoring_system_prompt.txt   ← Instructions for the scoring AI
│   └── tailoring_system_prompt.txt ← Instructions for the tailoring AI
│
├── resume_template.docx      ← Your resume with {{PLACEHOLDER}} tags
│
└── data/
    ├── raw/                  ← raw_jobs_YYYY-MM-DD.csv (scraper output)
    ├── scored/               ← scored_jobs_YYYY-MM-DD.csv (scored results)
    ├── blacklisted/          ← blacklisted_YYYY-MM-DD.csv (filtered jobs)
    └── tailored/
        └── CompanyName/
            ├── Company_Role_DATE.txt       ← Tailored draft for review
            └── Company_Role_DATE_RESUME.docx ← Final tailored resume
```

---

## First-Time Setup

### 1. Prerequisites

- Python 3.11 or 3.13
- A virtual environment (`myenv`)
- A Notion account with an integration token
- An Anthropic API key

### 2. Install Dependencies

```bash
cd Job_Application
source myenv/bin/activate
pip install anthropic notion-client jobspy pandas python-docx python-dotenv requests beautifulsoup4
```

### 3. Create Your `.env` File

Create a file called `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
NOTION_DATABASE_ID=33712edd09f5809a9097e9c97b7f60fd
```

**Where to find each:**
- `ANTHROPIC_API_KEY`: console.anthropic.com → API Keys
- `NOTION_TOKEN`: notion.so/my-integrations → New Integration → copy the secret
- `NOTION_DATABASE_ID`: open your Notion database in the browser, the 32-char ID is in the URL before the `?`

### 4. Set Up Your Notion Database

Create a new database in Notion and add these exact property names (case and spacing must match):

| Property Name | Type |
|---|---|
| Role | Title |
| Company | Text |
| Fit score | Number |
| Tier | Select |
| Role family | Select |
| Apply recommendation | Select |
| Overqualified | Checkbox |
| Sponsorship status | Select |
| Matched skills | Text |
| Missing skills | Text |
| Transferable strengths | Text |
| Status | Select |
| Resume version | Text |
| Date applied | Date |
| Follow-up date | Date |
| Notes | Text |
| Job URL | URL |
| Site | Text |
| Date posted | Text |
| Scored at | Text |
| Description | Text |
| Queue for apply | Checkbox |

Then connect your integration: open the database → `...` menu → Connections → find your integration → Connect.

### 5. Set Up Your Resume Template

Run this once to create `resume_template.docx` from your existing resume:

```bash
python tailor/inject_resume.py --setup
```

Open the file in Word to verify it looks correct. Each section should have a placeholder like `{{GRA_SECTION}}` or `{{SKILLS_SECTION}}` where the bullets will be injected.

### 6. Verify Everything Works

```bash
python -c "
import os; from dotenv import load_dotenv; load_dotenv()
for k in ['ANTHROPIC_API_KEY','NOTION_TOKEN','NOTION_DATABASE_ID']:
    v = os.getenv(k)
    print(k, '→', 'OK' if v else 'MISSING')
"
```

---

## Daily Workflow

### Automatic (Recommended)

Set up a cron job to run the pipeline every morning at 8am:

```bash
crontab -e
```

Add this line (update the paths to match your machine):

```
0 8 * * * /Users/yourname/Job_Application/myenv/bin/python /Users/yourname/Job_Application/run_daily.py >> /Users/yourname/Job_Application/logs/cron.log 2>&1
```

The pipeline runs while you sleep. You wake up to a fresh list of scored jobs in Notion.

### Manual

Run each stage separately when needed:

```bash
# Step 1: Scrape new jobs
python run_daily.py --skip-score --skip-notion

# Step 2: Score the scraped jobs (takes ~10 minutes)
python scorer/score_jobs.py

# Step 3: Push scored jobs to Notion
python tracker/push_to_notion.py
```

Or run everything at once:

```bash
python run_daily.py
```

---

## Applying to Jobs

### Method 1: Batch from Notion (Recommended for Volume)

1. Open your Notion database
2. Sort by **Fit score** descending
3. Filter to **Apply recommendation = yes**
4. Tick the **Queue for apply** checkbox on every role you want to apply to
5. Run:

```bash
python apply.py --batch
```

The script processes all ticked rows automatically — tailors your resume, generates a `.docx` for each, updates Notion status to `Reviewing`, and unticks the checkbox when done.

For a smaller batch or to review files as they're created:

```bash
python apply.py --batch --limit 10           # first 10 only
python apply.py --batch --no-open            # generate all, open none
```

### Method 2: Single Job from Notion

Copy the URL of any Notion page and run:

```bash
python apply.py --notion "https://www.notion.so/..."
```

### Method 3: External Job (Not from Scraper)

For jobs you found yourself on LinkedIn, a company careers page, or anywhere else:

```bash
# Save the JD as a text file, then:
python edit.py --company "Google" --role "ML Engineer" --jd path/to/jd.txt

# Or paste interactively (type END when done):
python edit.py --company "Google" --role "ML Engineer"
```

This skips Notion entirely — just tailors and generates the `.docx`.

### Location Override

Default location is "Open to Relocate". Override with:

```bash
python apply.py --notion "..." --location cleveland
python edit.py --company "Google" --role "ML Engineer" --location sf
```

Available shortcuts: `cleveland`, `relocate`, `remote`, `houston`, `seattle`, `sf`, `nyc`, `boston`, `austin`, `chicago`.

---

## After the Resume is Generated

Every application produces two files in `data/tailored/CompanyName/`:

- **`Company_Role_DATE.txt`** — the raw tailoring draft with analysis, bullet rewrites, cover letter, and gaps notes
- **`Company_Role_DATE_RESUME.docx`** — your final tailored resume, ready to review

**Before submitting, always:**

1. Read every bullet — the AI tailors accurately but you know your work best
2. Check the location line is correct
3. Read the GAPS & HONEST NOTES section in the `.txt` draft — it tells you what the JD requires that you don't have
4. Save as PDF (File → Export → PDF in Word/Pages)
5. Submit the PDF, not the `.docx`
6. Update Notion: fill in **Date applied** and **Follow-up date** (set ~2 weeks out)

---

## Recovering from Errors

### Scorer was interrupted mid-run

The batch results are saved on Anthropic's servers for 29 days. Collect them without re-paying:

```bash
python recover_batch.py \
  --batch-id msgbatch_XXXX \
  --input data/raw/raw_jobs_YYYY-MM-DD.csv \
  --date YYYY-MM-DD
```

Find the batch ID in the log output from the interrupted run.

### Notion push failed

The Notion push is idempotent — it skips jobs already in the database. Just run again:

```bash
python tracker/push_to_notion.py --input data/scored/scored_jobs_YYYY-MM-DD.csv
```

### JD not found in CSV

If `apply.py` can't find the job description in the CSV, it will either fetch the URL live or prompt you to paste the JD. You can always fall back to:

```bash
python edit.py --company "Company" --role "Role"
```

### Template not found

If the resume template is missing:

```bash
python tailor/inject_resume.py --setup
```

---

## Understanding Scores

Every job gets a **fit score from 0–10** and one of three recommendations:

| Score | Recommendation | Meaning |
|---|---|---|
| 7–10 | `yes` | Strong match — apply immediately |
| 4–7 | `manual_review` | Partial match — worth a look, your call |
| 0–4 | `no` | Poor match — filtered out of Notion |

The scorer also flags:
- **Overqualified**: role is below your target level but still worth considering
- **Sponsorship status**: whether the role explicitly mentions or refuses H1B sponsorship
- **Matched/Missing skills**: what you have and what the JD wants that you don't

A role scoring `manual_review` at 6.8 might be better than a `yes` at 7.0 if it's at a better company or in a preferred location — use the scores as a signal, not a verdict.

---

## Key Files to Know

### `profile.txt`
Your master candidate profile — education, experience, projects, skills, and key metrics. The AI reads this for every scoring and tailoring call. Keep it factual and up to date. If you add a new project or skill, add it here.

### `prompts/target_roles.txt`
Your job search priorities. Defines three tiers:
- **Tier 1**: ML Engineer, AI Engineer, Data Scientist, Research Scientist — apply aggressively
- **Tier 2**: Data Engineer, MLOps, Applied Scientist, DevRel — good secondary options
- **Tier 3**: Data Analyst, Forward Deployed Engineer — stretch/fallback only

Also defines hard constraints (no security clearance, no 10+ years required) and visa handling.

### `resume_template.docx`
Your resume with section placeholders. Never edit this manually — run `--setup` if you want to regenerate it from an updated resume.

### `.env`
Your API keys and tokens. Never commit this to Git.

---

## Cost Estimates

| Operation | Approximate Cost |
|---|---|
| Score 1,500 jobs (Batch API) | ~$2.30 |
| Tailor one resume (Claude Sonnet) | ~$0.05–0.10 |
| Daily run (scrape + score + push) | ~$2.50 |
| 50 tailored resumes | ~$3–5 |

Set a spend limit at console.anthropic.com → Billing to avoid surprises.

---

## Common Commands Reference

```bash
# Full daily pipeline
python run_daily.py

# Skip scraping, use existing raw CSV
python run_daily.py --skip-scrape

# Batch apply (process all queued Notion rows)
python apply.py --batch

# Single Notion job
python apply.py --notion "https://www.notion.so/..."

# External JD from file
python edit.py --company "Google" --role "ML Engineer" --jd jd.txt

# External JD paste interactively
python edit.py --company "Google" --role "ML Engineer"

# Check cost estimate before scoring
python scorer/score_jobs.py --dry-run

# Push scored jobs to Notion
python tracker/push_to_notion.py --input data/scored/scored_jobs_YYYY-MM-DD.csv

# Recover interrupted batch
python recover_batch.py --batch-id msgbatch_XXX --input data/raw/raw_jobs_DATE.csv --date DATE

# Regenerate resume template
python tailor/inject_resume.py --setup
```

---

## Tips for High-Volume Days (50+ Applications)

1. **Run the pipeline the night before** so Notion is populated when you wake up
2. **Filter and queue in the morning** — spend 20 minutes in Notion, tick your Queue for apply checkboxes on 20–30 roles
3. **Run batch while you do other things** — `python apply.py --batch --no-open`
4. **Review stack in the afternoon** — open each `.docx` in `data/tailored/`, read the draft `.txt` notes, save as PDF, submit
5. **For referral roles, use `edit.py`** — fastest path, no Notion overhead
6. **Update Notion after each submission** — fill in Date applied and Follow-up date immediately so nothing falls through

---

*Built April 2026. Pipeline version: Phase 8 complete.*