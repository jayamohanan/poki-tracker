"""Decide which Poki snapshot(s) are due, tolerating late runs.

Used only by the automatic GitHub Actions run. GitHub can start a
scheduled run hours late, so instead of "is it Sunday right now?" this
asks "is the latest Sunday / month-end snapshot still missing?":

  - weekly:  due on a Sunday, or on the Monday right after one, if
             that Sunday's ISO-week folder does not exist yet.
  - monthly: due on the last day of a month, or on the 1st right
             after it, if that month's folder does not exist yet.

A late run therefore still creates the snapshot, labelled with the
Sunday / month-end it belongs to. Dates are IST.

Prints a summary and, when running in Actions, writes `period`,
`weekly_date` and `monthly_date` to $GITHUB_OUTPUT.
"""

import os
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def is_last_day_of_month(day):
    return day.day == monthrange(day.year, day.month)[1]


def week_name(day):
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_target(today, data_dir=DATA_DIR):
    """Sunday this run should snapshot, or None."""
    if today.weekday() == 6:
        sunday = today
    elif today.weekday() == 0:
        sunday = today - timedelta(days=1)
    else:
        return None

    if (data_dir / "weekly" / week_name(sunday)).exists():
        return None

    return sunday


def monthly_target(today, data_dir=DATA_DIR):
    """Month-end this run should snapshot, or None."""
    if is_last_day_of_month(today):
        month_end = today
    elif today.day == 1:
        month_end = today - timedelta(days=1)
    else:
        return None

    if (data_dir / "monthly" / month_end.strftime("%Y-%m")).exists():
        return None

    return month_end


def due(today, data_dir=DATA_DIR):
    weekly = weekly_target(today, data_dir)
    monthly = monthly_target(today, data_dir)

    if weekly and monthly:
        period = "both"
    elif weekly:
        period = "weekly"
    elif monthly:
        period = "monthly"
    else:
        period = "none"

    return period, weekly, monthly


if __name__ == "__main__":
    today = datetime.now(TIMEZONE).date()
    period, weekly, monthly = due(today)

    print(
        f"Date (IST): {today} -> {period} "
        f"(weekly snapshot date: {weekly}, "
        f"monthly snapshot date: {monthly})"
    )

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as file:
            file.write(f"period={period}\n")
            file.write(f"weekly_date={weekly or ''}\n")
            file.write(f"monthly_date={monthly or ''}\n")
