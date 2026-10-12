# SpaceXAI Miami QA Hub

A small QA tool I built for early-stage builders in Miami. Paste a staging URL, and it runs three automated browser checks at the same time, then gives the site a health score and a report you can share with your team.

**Live demo:** https://spacexai-miami-qa-hub.onrender.com/

> Note: Free cloud hosting can't run real browsers, so the live demo shows a simulated run. Click **"Use local sandbox URL"** on the dashboard to try it. The real checks run locally (see below).

## What it checks

- **Chaos Client** acts like a careless user, with rapid clicks, empty form submissions, and fast tabbing, to see if the page breaks.
- **DOM Auditor** looks for basic accessibility issues and overlapping elements that block clicks.
- **Console Detective** listens to the browser console and network for JavaScript errors and failed API calls (400 and 500 errors).

## How the score works

Every site starts at 100 and loses points for each issue found:

- **-12** for each failed API call or crash
- **-11** for each serious structure or accessibility issue
- **-4** for each layout overlap or form error

You get a letter grade, plain-English findings, suggested fixes, and a downloadable PDF report.

## Tech stack

- Python and FastAPI for the backend, which runs the checks in parallel
- Playwright with headless Chromium for the browser checks
- Tailwind CSS for the dashboard

I built this with Grok Bot as my AI coding assistant. I decided what to test and how to score it, then tested and fixed the results.

## Run it locally

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --reload
```

Then open http://localhost:8000.

## What I'd improve next

- Use axe-core for deeper accessibility testing
- Support pages behind a login
- Run it in CI on every deploy

---

Built for the SpaceXAI Miami community. Production is a sacred place. Protect it.
