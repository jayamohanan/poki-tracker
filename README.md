# Poki Tracker

Scrapes the [Poki](https://poki.com/en/all-games) game catalog and tracks what changes.

- **Weekly** (Sundays) -> `data/weekly/<YYYY-Www>/`
- **Monthly** (last day of month) -> `data/monthly/<YYYY-MM>/`
- **Quarterly** (quarter-end months) -> `data/quarterly/<YYYY-Qn>/`

Each period folder contains `master.csv` (full snapshot), `catalog_changes.csv` (ADDED / REMOVED games) and a stats-change CSV (likes, dislikes, votes, rating deltas). Note: Poki rounds likes/dislikes (e.g. `18.2M`), so Total Votes is the more precise activity signal.

Runs daily at 21:00 IST via GitHub Actions (`.github/workflows/poki-tracker.yml`); the script only scrapes on Sundays and month-ends. Snapshots are immutable.

Local run: `pip install -r requirements.txt && python scripts/poki_tracker.py --force both`
