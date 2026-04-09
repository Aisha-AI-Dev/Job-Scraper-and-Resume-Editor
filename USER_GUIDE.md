# Job Application Pipeline — User Guide

## What Is This?

A semi-automated job application pipeline I built for myself (Aishani Patil - MS CS, CWRU, May 2026). It handles the mechanical parts of job searching so you can focus on the parts that need human judgment: picking which roles to apply to and reviewing your tailored resume before submitting.

**What it does automatically:**
- Scrapes hundreds of job postings from LinkedIn, Indeed, and Glassdoor every morning
- Scores each job against your profile using Claude AI (fit score 0–10)
- Pushes scored jobs into Notion with JD preview so you can decide at a glance
- Tailors your resume bullets, cover letter, and recruiter outreach to each JD
- Injects tailored content into your `.docx` template with formatting intact
- Generates a LinkedIn DM and email for every application automatically

**What you do manually:**
- Review scored jobs in Notion, tick the ones you want to apply to
- Review each tailored `.docx` before submitting
- Personalise the outreach messages before sending
- Save as PDF and submit on the company's portal

---

## Project Structure

```
Job_Application/
├── apply.py                  ← Main trigger: Notion → resume + cover letter + outreach
├── edit.py                   ← Quick trigger for external JDs (no Notion needed)
├── add_job.py                ← Manually add any job to Notion
├── outreach.py               ← Standalone: generate LinkedIn DM + recruiter email
├── run_daily.py              ← Daily pipeline orchestrator (scrape → score → push)
├── recover_batch.py          ← Recover scorer results if interrupted
├── paths.py                  ← Shared directory paths (single source of truth)
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
│   └── inject_resume.py      ← Injects tailored bullets into .docx template
│
├── prompts/
│   ├── profile.txt                  ← Your candidate profile (facts only)
│   ├── target_roles.txt             ← Tier 1/2/3 role priorities and weights
│   ├── scoring_system_prompt.txt    ← Scoring AI instructions
│   └── tailoring_system_prompt.txt  ← Tailoring AI instructions (ATS-optimised)
│
├── resume_template.docx      ← Your resume with {{PLACEHOLDER}} tags
│
└── data/
    ├── raw/                  ← raw_jobs_YYYY-MM-DD.csv
    ├── scored/               ← scored_jobs_YYYY-MM-DD.csv + low + errors
    ├── blacklisted/          ← blacklisted_YYYY-MM-DD.csv
    └── tailored/
        └── CompanyName/
            ├── Company_Role_DATE.txt              ← Draft: bullets + cover letter
            ├── Company_Role_DATE_RESUME.docx      ← Tailored resume
            └── Company_Role_DATE_OUTREACH.txt     ← LinkedIn DM + recruiter email
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
NOTION_DATABASE_ID=your_32_char_database_id
```

**Where to find each:**
- `ANTHROPIC_API_KEY`: console.anthropic.com → API Keys
- `NOTION_TOKEN`: notion.so/my-integrations → New Integration → copy the secret
- `NOTION_DATABASE_ID`: open your Notion database in a browser — the 32-char ID is in the URL before the `?`

### 4. Set Up Your Notion Database

Create a new database in Notion and add these exact property names (case and spacing must match exactly):

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

Run once to create `resume_template.docx` from your existing resume:

```bash
python tailor/inject_resume.py --setup
```

Open the file in Word to verify it looks correct. Each section should have a placeholder like `{{GRA_SECTION}}` where bullets will be injected.

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

### Manual (Step by Step)

```bash
# Step 1: Scrape new jobs
python run_daily.py --skip-score --skip-notion

# Step 2: Score them (takes ~10 minutes for 1,500 jobs)
python scorer/score_jobs.py

# Step 3: Push to Notion
python tracker/push_to_notion.py

# Or run everything at once:
python run_daily.py
```

---

## Applying to Jobs

Every application method automatically produces three files in `data/tailored/CompanyName/`:

| File | Contents |
|---|---|
| `Company_Role_DATE.txt` | Full tailoring draft: ATS keyword audit, rewritten bullets, cover letter, gaps analysis |
| `Company_Role_DATE_RESUME.docx` | Your tailored resume, ready to review and convert to PDF |
| `Company_Role_DATE_OUTREACH.txt` | LinkedIn DM + recruiter email, ready to personalise and send |

### Method 1: Batch from Notion (Best for High Volume)

1. Open Notion, sort by **Fit score** descending
2. Filter to **Apply recommendation = yes**
3. Tick **Queue for apply** on every role you want
4. Run one command and walk away:

```bash
python apply.py --batch
```

Processes all ticked rows in order — tailors, injects, generates outreach, updates Notion status to `Reviewing`, unticks the checkbox. Come back to a folder of finished files.

```bash
python apply.py --batch --limit 10     # first 10 only
python apply.py --batch --no-open      # don't open each file as it's created
```

### Method 2: Single Job from Notion

```bash
python apply.py --notion "https://www.notion.so/..."
```

### Method 3: External JD — Full Pipeline

For jobs you found yourself (company website, LinkedIn, referral). Produces all three files, no Notion involvement:

```bash
# From a saved JD file:
python edit.py --company "Google" --role "ML Engineer" --jd path/to/jd.txt

# Paste interactively (type END on a new line when done):
python edit.py --company "Google" --role "ML Engineer"
```

### Method 4: Add a Job to Notion Manually

Found a role you want to track but it didn't come through the scraper? Add it to Notion directly:

```bash
# Fully interactive — prompts for everything:
python add_job.py

# With CLI args:
python add_job.py --company "Anthropic" --role "Research Engineer" --url "https://..."

# With JD from file (saves description to Notion):
python add_job.py --company "Anthropic" --role "Research Engineer" --jd jobs/anthropic.txt

# Queue it immediately so apply.py --batch picks it up:
python add_job.py --company "Anthropic" --role "Research Engineer" --queue

# Log a referral with a note:
python add_job.py --company "Qualcomm" --role "ML Engineer" \
  --site "Referral" --notes "Referred by [name] — reach out first" --queue
```

### Method 5: Outreach Only (Standalone)

Regenerate or personalise outreach for a role that's already been tailored:

```bash
# From existing draft:
python outreach.py --company "DoorDash" --role "ML Engineer"

# With recruiter's name (personalises the email salutation):
python outreach.py --company "DoorDash" --role "ML Engineer" --recruiter "Sarah"

# From a specific draft file:
python outreach.py --draft "data/tailored/DoorDash/DoorDash_ML_Engineer_2026-04-07.txt"
```

### Location Override

Default is "Open to Relocate". Override with `--location`:

```bash
python apply.py --notion "..." --location cleveland
python edit.py --company "Google" --role "ML Engineer" --location sf
```

Available shortcuts: `cleveland`, `relocate`, `remote`, `houston`, `seattle`, `sf`, `nyc`, `boston`, `austin`, `chicago`.

---

## After the Files Are Generated

**Resume (`.docx`):**
1. Read every bullet — AI tailors accurately but you know your work best
2. Check the location line is correct
3. Read the **GAPS & HONEST NOTES** section in the `.txt` — tells you what the JD needs that you don't have (address in cover letter)
4. Save as PDF → File → Export → PDF
5. Submit the PDF, not the `.docx`

**Cover letter (inside the `.txt` draft, Section 2):**
1. Read the Writer's Notes — explains the hook choice and what was deliberately omitted
2. Check the Things to Verify list at the bottom
3. Personalise for the specific office or hiring manager if you know them

**Outreach (`.OUTREACH.txt`):**
1. Replace any `[placeholder]` text
2. For the LinkedIn DM — send within 24 hours of applying
3. For the email — use if you have a specific recruiter's address, otherwise hold it for follow-up

**In Notion after submitting:**
- Set **Status** → `Applied`
- Fill in **Date applied** and **Follow-up date** (~2 weeks out)
- Add **Resume version** filename so you know which version you submitted

---

## Recovering from Errors

### Scorer was interrupted mid-run

Batch results stay on Anthropic's servers for 29 days. Collect without re-paying:

```bash
python recover_batch.py \
  --batch-id msgbatch_XXXX \
  --input data/raw/raw_jobs_YYYY-MM-DD.csv \
  --date YYYY-MM-DD
```

Find the batch ID in the terminal output from the interrupted run.

### Notion push failed

The push is idempotent — skips jobs already in the database. Just re-run:

```bash
python tracker/push_to_notion.py --input data/scored/scored_jobs_YYYY-MM-DD.csv
```

### JD not found in CSV

`apply.py` tries the CSV first, then fetches the URL live, then prompts you to paste. You can always fall back to:

```bash
python edit.py --company "Company" --role "Role"
```

### Template not found

```bash
python tailor/inject_resume.py --setup
```

---

## Understanding Scores

Every job gets a **fit score 0–10** and a recommendation:

| Score | Recommendation | Meaning |
|---|---|---|
| 7–10 | `yes` | Strong match — apply |
| 4–7 | `manual_review` | Partial match — your call |
| 0–4 | `no` | Poor match — filtered out |

The scorer also flags:
- **Overqualified**: role is below your trajectory but still worth considering
- **Sponsorship status**: whether the role mentions or refuses H1B sponsorship
- **Matched/Missing skills**: what you have vs what the JD wants

Use scores as signals, not verdicts. A `manual_review` at 6.8 at a great company beats a `yes` at 7.0 at one you don't care about.

---

## ATS Optimisation

The tailoring prompt runs a keyword audit before writing a single bullet. For every JD it:

1. Extracts the exact role title and injects it verbatim into the summary
2. Identifies the top 15 hard skills, 5 soft skills, and 5 domain terms from the JD
3. Marks each as In Profile / Transferable / Genuine Gap
4. Ensures every In Profile and Transferable keyword appears verbatim in the output
5. Uses the skills section as a keyword bank — adds JD terms the candidate genuinely has
6. Runs a self-verification pass before finalising

**Target ATS score: 75–85** on Jobscan for core ML/DS roles. Tested at 80 on DoorDash ML Engineer (up from 47 before ATS optimisation was added).

Genuine gaps — skills the candidate doesn't have — are listed honestly in the GAPS & HONEST NOTES section and suggested for cover letter treatment. The prompt will not fabricate.

---

## Key Files to Know

### `profile.txt`
Your master candidate profile — education, experience, projects, skills, key metrics. Every scoring and tailoring call reads this. Update it when your experience changes.

### `prompts/target_roles.txt`
Your search priorities in three tiers. Also defines hard constraints (no clearance, no 10+ years) and visa handling logic.

### `prompts/tailoring_system_prompt.txt`
Instructions for the resume tailoring AI. Contains the ATS keyword audit rules, honesty guardrails, bullet count guidelines per role type, and output format spec. Edit with care — the output format drives the injector parser.

### `resume_template.docx`
Your resume with `{{SECTION}}` placeholders. Never edit manually — regenerate with `--setup` if your base resume changes.

### `.env`
API keys and tokens. Never commit to Git. Add to `.gitignore`.

---

## Cost Estimates

| Operation | Approximate Cost |
|---|---|
| Score 1,500 jobs (Batch API + caching) | ~$2.30 |
| Tailor one resume + cover letter (Sonnet) | ~$0.05–0.10 |
| Generate outreach messages (Sonnet) | ~$0.02–0.05 |
| Full daily run (scrape + score + push) | ~$2.50 |
| 50 complete applications (resume + outreach) | ~$5–8 |

Set a spend limit at console.anthropic.com → Billing.

---

## Common Commands Reference

```bash
# ── Daily pipeline ─────────────────────────────────────────────
python run_daily.py                          # full pipeline
python run_daily.py --skip-scrape            # score + push existing CSV

# ── Applying ──────────────────────────────────────────────────
python apply.py --batch                      # process all queued Notion rows
python apply.py --batch --limit 10           # first 10 only
python apply.py --notion "https://..."       # single Notion job
python edit.py --company X --role Y          # external JD, paste interactively
python edit.py --company X --role Y --jd f   # external JD from file

# ── Adding jobs manually ───────────────────────────────────────
python add_job.py                            # fully interactive
python add_job.py --company X --role Y --url "..." --queue
python add_job.py --company X --role Y --jd jd.txt --queue

# ── Outreach ──────────────────────────────────────────────────
python outreach.py --company X --role Y      # generate from latest draft
python outreach.py --draft path/to/draft.txt # from specific draft
python outreach.py --company X --role Y --recruiter "Sarah"

# ── Scoring & Notion ──────────────────────────────────────────
python scorer/score_jobs.py --dry-run        # cost estimate only
python scorer/score_jobs.py                  # run scoring
python tracker/push_to_notion.py --input data/scored/scored_jobs_DATE.csv

# ── Recovery & maintenance ────────────────────────────────────
python recover_batch.py --batch-id msgbatch_XXX --input data/raw/raw_jobs_DATE.csv --date DATE
python tailor/inject_resume.py --setup       # regenerate resume template
```

---

## Tips for High-Volume Days (50+ Applications)

1. **Run the pipeline the night before** so Notion is full when you wake up
2. **Spend 20 minutes in Notion** — sort by fit score, read the Description column, tick Queue for apply on 20–30 roles
3. **Run batch, walk away** — `python apply.py --batch --no-open`
4. **Review the stack in the afternoon** — open each `.docx`, check accuracy, save as PDF, submit
5. **Send outreach the same day** — LinkedIn DMs are in the `_OUTREACH.txt` file, ready to copy-paste
6. **For referral roles, use `add_job.py --queue`** — add to Notion, queue it, let `apply.py --batch` handle the rest
7. **Update Notion after each submission** — Date applied + Follow-up date, every time

---

*Built April 2026. Pipeline version: Phase 9 complete.*