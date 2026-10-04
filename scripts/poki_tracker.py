import argparse
import csv
import json
import re
import sys
import time
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://poki.com"
ALL_GAMES_URL = "https://poki.com/en/all-games"

REQUEST_DELAY = 1.0
PAGE_DELAY = 1.5
TIMEOUT = 30
MAX_CATALOG_PAGES = 100

# Safety checks.
#
# Once you know approximately how many games Poki normally has,
# this protects against a broken scrape being treated as hundreds
# of removals.
MIN_EXPECTED_GAMES = 500

# If current catalog suddenly falls below this fraction of the
# previous snapshot, abort comparisons.
MIN_PREVIOUS_RATIO = 0.80


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

DATA_DIR = ROOT_DIR / "data"
WEEKLY_DIR = DATA_DIR / "weekly"
MONTHLY_DIR = DATA_DIR / "monthly"
QUARTERLY_DIR = DATA_DIR / "quarterly"


# ============================================================
# MASTER CSV
# ============================================================

MASTER_FIELDS = [
    "id",
    "Game Title",
    "Game Link Labels",
    "Game URL",
    "Developer",
    "Likes",
    "Dislikes",
    "Description",
    "Total Votes",
    "Rating",
    "Category Tags",
    "Thumbnail Image URL",
    "Breadcrumb Path",
    "Genre",
    "Poki Release Date",
    "Latest Update",
    "Snapshot Date",
]


# ============================================================
# CATALOG CHANGE CSV
# ============================================================

CATALOG_CHANGE_FIELDS = [
    "Change Type",
    "Game Title",
    "Game URL",
    "Developer",
    "Genre",
    "Poki Release Date",
    "Previous Period",
    "Current Period",
]


# ============================================================
# STAT CHANGE CSV
# ============================================================

STAT_CHANGE_FIELDS = [
    "Game Title",
    "Game URL",
    "Developer",
    "Genre",

    "Previous Likes",
    "Current Likes",
    "Likes Change",

    "Previous Dislikes",
    "Current Dislikes",
    "Dislikes Change",

    "Previous Total Engagement",
    "Current Total Engagement",
    "Total Engagement Change",
    "Engagement Change %",

    "Previous Total Votes",
    "Current Total Votes",
    "Total Votes Change",

    "Previous Rating",
    "Current Rating",
    "Rating Change",

    "Previous Period",
    "Current Period",
]


session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def ensure_directories():
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    QUARTERLY_DIR.mkdir(parents=True, exist_ok=True)


def safe_int(value):
    if value in ("", None):
        return 0

    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def safe_float(value):
    if value in ("", None):
        return 0.0

    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def get_soup(url):
    for attempt in range(1, 4):
        try:
            response = session.get(
                url,
                timeout=TIMEOUT
            )

            response.raise_for_status()

            return BeautifulSoup(
                response.text,
                "html.parser"
            )

        except requests.RequestException as exc:
            print(
                f"Request failed {attempt}/3: {url}"
            )
            print(f"    {exc}")

            if attempt < 3:
                time.sleep(3 * attempt)

    return None


def parse_compact_number(text):
    if not text:
        return ""

    text = (
        str(text)
        .upper()
        .replace(",", "")
        .strip()
    )

    match = re.search(
        r"([\d.]+)\s*([KMB]?)",
        text
    )

    if not match:
        return ""

    number = float(match.group(1))
    suffix = match.group(2)

    multipliers = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }

    return int(
        number * multipliers[suffix]
    )


def extract_json_ld(soup):
    results = []

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):
        try:
            content = (
                script.string
                or script.get_text()
            )

            results.append(
                json.loads(content)
            )

        except Exception:
            continue

    return results


# ============================================================
# DATE HELPERS
# ============================================================

def is_last_day_of_month(date):
    last_day = monthrange(
        date.year,
        date.month
    )[1]

    return date.day == last_day


def iso_week_name(date):
    iso = date.isocalendar()

    return (
        f"{iso.year}-W"
        f"{iso.week:02d}"
    )


def month_name(date):
    return date.strftime("%Y-%m")


def quarter_number(month):
    return ((month - 1) // 3) + 1


def quarter_name(date):
    return (
        f"{date.year}-Q"
        f"{quarter_number(date.month)}"
    )


def determine_modes(date, force):
    if force == "weekly":
        return True, False

    if force == "monthly":
        return False, True

    if force == "both":
        return True, True

    weekly = date.weekday() == 6
    monthly = is_last_day_of_month(date)

    return weekly, monthly


# ============================================================
# CSV HELPERS
# ============================================================

def read_csv(path):
    if not path.exists():
        return []

    with path.open(
        "r",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        return list(
            csv.DictReader(file)
        )


def write_csv(path, rows, fields):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# CATALOG DISCOVERY
# ============================================================

def find_all_games_heading(soup):
    for heading in soup.find_all(
        ["h1", "h2"]
    ):
        text = clean_text(
            heading.get_text(
                " ",
                strip=True
            )
        )

        if text.lower() == "all games":
            return heading

    return None


def extract_game_links(soup):
    games = {}

    heading = find_all_games_heading(soup)

    if heading is None:
        print(
            "WARNING: Could not find "
            "'All Games' heading."
        )
        return games

    for link in heading.find_all_next(
        "a",
        href=True
    ):
        href = link.get("href", "")

        if not re.match(
            r"^/en/g/[^/?#]+/?$",
            href
        ):
            continue

        url = urljoin(
            BASE_URL,
            href
        )

        title = clean_text(
            link.get("aria-label")
            or link.get("title")
            or link.get_text(
                " ",
                strip=True
            )
        )

        if not title:
            continue

        if url not in games:
            games[url] = title

    return games


def discover_catalog():
    all_games = {}

    print()
    print("=" * 60)
    print("DISCOVERING POKI CATALOG")
    print("=" * 60)

    consecutive_empty_pages = 0

    for page_number in range(
        1,
        MAX_CATALOG_PAGES + 1
    ):

        if page_number == 1:
            url = ALL_GAMES_URL
        else:
            url = (
                f"{ALL_GAMES_URL}/"
                f"{page_number}"
            )

        print()
        print(
            f"Catalog page {page_number}: "
            f"{url}"
        )

        soup = get_soup(url)

        if soup is None:
            raise RuntimeError(
                f"Could not load catalog page "
                f"{page_number}."
            )

        page_games = extract_game_links(soup)

        new_count = 0

        for game_url, title in page_games.items():
            if game_url not in all_games:
                all_games[game_url] = title
                new_count += 1

        print(
            f"    Links found: "
            f"{len(page_games)}"
        )

        print(
            f"    New unique games: "
            f"{new_count}"
        )

        print(
            f"    Running total: "
            f"{len(all_games)}"
        )

        if new_count == 0:
            consecutive_empty_pages += 1
        else:
            consecutive_empty_pages = 0

        # Two empty/no-new pages gives us a little more
        # protection against an unusual single page.
        if (
            page_number > 1
            and consecutive_empty_pages >= 2
        ):
            break

        time.sleep(PAGE_DELAY)

    if len(all_games) < MIN_EXPECTED_GAMES:
        raise RuntimeError(
            f"Only {len(all_games)} games were "
            f"discovered. Expected at least "
            f"{MIN_EXPECTED_GAMES}. "
            f"Aborting to protect historical data."
        )

    print()
    print(
        f"TOTAL UNIQUE GAMES: "
        f"{len(all_games)}"
    )

    return all_games


# ============================================================
# GAME PAGE EXTRACTION
# ============================================================

def extract_title(soup):
    h1 = soup.find("h1")

    if h1:
        return clean_text(
            h1.get_text(
                " ",
                strip=True
            )
        )

    return ""


def extract_developer(text):
    patterns = [
        r"\bDeveloper\s+(.+?)(?=\s+Genre\b)",
        r"\bby\s+(.+?)(?=\s+\d[\d.,]*[KMB]?\s+Like\b)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.I
        )

        if match:
            return clean_text(
                match.group(1)
            )

    return ""


def extract_likes(text):
    match = re.search(
        r"([\d.,]+\s*[KMB]?)\s+Like\b",
        text,
        flags=re.I
    )

    if match:
        return parse_compact_number(
            match.group(1)
        )

    return ""


def extract_dislikes(text):
    match = re.search(
        r"([\d.,]+\s*[KMB]?)\s+Dislike\b",
        text,
        flags=re.I
    )

    if match:
        return parse_compact_number(
            match.group(1)
        )

    return ""


def extract_rating_and_votes(text):
    matches = re.findall(
        r"Rating\s+"
        r"([0-5](?:\.\d+)?)"
        r"\s*\("
        r"([\d,]+)\s+votes?"
        r"\)",
        text,
        flags=re.I
    )

    if not matches:
        return "", ""

    rating, votes = matches[-1]

    return (
        float(rating),
        int(votes.replace(",", ""))
    )


def extract_genre(text):
    match = re.search(
        r"\bGenre\s+(.+?)"
        r"(?="
        r"\s+Release Date\b"
        r"|\s+Latest update\b"
        r"|\s+Rating\b"
        r")",
        text,
        flags=re.I
    )

    if match:
        return clean_text(
            match.group(1)
        )

    return ""


def extract_release_date(text):
    match = re.search(
        r"\bRelease Date\s+(.+?)"
        r"(?="
        r"\s+Latest update\b"
        r"|\s+Rating\b"
        r")",
        text,
        flags=re.I
    )

    if match:
        return clean_text(
            match.group(1)
        )

    return ""


def extract_latest_update(text):
    match = re.search(
        r"\bLatest update\s+(.+?)"
        r"(?="
        r"\s+Rating\b"
        r"|\s+Related categories\b"
        r"|\s+FAQ\b"
        r")",
        text,
        flags=re.I
    )

    if match:
        return clean_text(
            match.group(1)
        )

    return ""


def extract_description(soup):
    meta = soup.find(
        "meta",
        attrs={"name": "description"}
    )

    if meta and meta.get("content"):
        return clean_text(
            meta["content"]
        )

    return ""


def extract_thumbnail(soup):
    meta = soup.find(
        "meta",
        attrs={"property": "og:image"}
    )

    if meta and meta.get("content"):
        return meta["content"].strip()

    return ""


def extract_breadcrumbs(soup):
    breadcrumbs = []

    for data in extract_json_ld(soup):

        if isinstance(data, dict):
            candidates = [data]

        elif isinstance(data, list):
            candidates = data

        else:
            continue

        for item in candidates:
            if not isinstance(item, dict):
                continue

            if item.get("@type") != "BreadcrumbList":
                continue

            for crumb in item.get(
                "itemListElement",
                []
            ):
                if not isinstance(crumb, dict):
                    continue

                name = crumb.get("name")

                if name:
                    breadcrumbs.append(
                        clean_text(name)
                    )

    return " > ".join(breadcrumbs)


def extract_category_tags(soup, genre):
    categories = []

    heading = soup.find(
        lambda tag:
        tag.name in ["h2", "h3"]
        and
        "related categories"
        in clean_text(
            tag.get_text(
                " ",
                strip=True
            )
        ).lower()
    )

    if heading:
        element = heading.find_next()
        checked = 0

        while element and checked < 60:

            if (
                element.name in ["h2", "h3"]
                and element is not heading
            ):
                break

            if element.name == "a":

                value = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                value = re.sub(
                    r"\s*\d+\s*$",
                    "",
                    value
                ).strip()

                if (
                    value
                    and value not in categories
                ):
                    categories.append(value)

            element = element.find_next()
            checked += 1

    if genre and genre not in categories:
        categories.insert(0, genre)

    return " | ".join(categories)


def scrape_game(
    game_url,
    catalog_label,
    snapshot_date
):
    soup = get_soup(game_url)

    if soup is None:
        return None

    text = clean_text(
        soup.get_text(
            " ",
            strip=True
        )
    )

    rating, votes = (
        extract_rating_and_votes(text)
    )

    genre = extract_genre(text)

    return {
        "Game Title":
            extract_title(soup),

        "Game Link Labels":
            catalog_label,

        "Game URL":
            game_url,

        "Developer":
            extract_developer(text),

        "Likes":
            extract_likes(text),

        "Dislikes":
            extract_dislikes(text),

        "Description":
            extract_description(soup),

        "Total Votes":
            votes,

        "Rating":
            rating,

        "Category Tags":
            extract_category_tags(
                soup,
                genre
            ),

        "Thumbnail Image URL":
            extract_thumbnail(soup),

        "Breadcrumb Path":
            extract_breadcrumbs(soup),

        "Genre":
            genre,

        "Poki Release Date":
            extract_release_date(text),

        "Latest Update":
            extract_latest_update(text),

        "Snapshot Date":
            snapshot_date,
    }


# ============================================================
# FULL SCRAPE
# ============================================================

def scrape_full_catalog(date):
    catalog = discover_catalog()

    rows = []

    total = len(catalog)

    print()
    print("=" * 60)
    print("SCRAPING GAME PAGES")
    print("=" * 60)

    for index, (
        game_url,
        label
    ) in enumerate(
        catalog.items(),
        start=1
    ):

        print(
            f"[{index}/{total}] "
            f"{label}"
        )

        row = scrape_game(
            game_url,
            label,
            date.isoformat()
        )

        if row is None:
            print("    FAILED")
        else:
            rows.append(row)

        time.sleep(REQUEST_DELAY)

    # Strong safety check.
    success_ratio = (
        len(rows) / len(catalog)
    )

    if success_ratio < 0.95:
        raise RuntimeError(
            f"Only {len(rows)} of "
            f"{len(catalog)} games were "
            f"successfully scraped "
            f"({success_ratio:.1%}). "
            f"Aborting snapshot."
        )

    # Stable order for each snapshot.
    rows.sort(
        key=lambda row:
        row["Game Title"].lower()
    )

    for index, row in enumerate(
        rows,
        start=1
    ):
        row["id"] = index

    return rows


# ============================================================
# SNAPSHOT DISCOVERY
# ============================================================

def find_previous_snapshot(
    base_dir,
    current_folder_name
):
    if not base_dir.exists():
        return None, None

    folders = sorted(
        [
            folder
            for folder in base_dir.iterdir()
            if folder.is_dir()
            and folder.name < current_folder_name
            and (folder / "master.csv").exists()
        ],
        key=lambda path: path.name
    )

    if not folders:
        return None, None

    previous = folders[-1]

    return (
        previous.name,
        previous / "master.csv"
    )


# ============================================================
# VALIDATE COMPARISON
# ============================================================

def validate_against_previous(
    current_rows,
    previous_rows
):
    if not previous_rows:
        return

    previous_count = len(previous_rows)
    current_count = len(current_rows)

    minimum_allowed = (
        previous_count
        * MIN_PREVIOUS_RATIO
    )

    if current_count < minimum_allowed:
        raise RuntimeError(
            f"Current snapshot has only "
            f"{current_count} games versus "
            f"{previous_count} previously. "
            f"Comparison aborted to prevent "
            f"false REMOVED records."
        )


# ============================================================
# CATALOG CHANGES
# ============================================================

def create_catalog_changes(
    previous_rows,
    current_rows,
    previous_period,
    current_period
):
    if not previous_rows:
        return []

    previous = {
        row["Game URL"]: row
        for row in previous_rows
        if row.get("Game URL")
    }

    current = {
        row["Game URL"]: row
        for row in current_rows
        if row.get("Game URL")
    }

    changes = []

    # ADDED
    for url in sorted(
        current.keys() - previous.keys()
    ):
        row = current[url]

        changes.append({
            "Change Type": "ADDED",
            "Game Title":
                row.get("Game Title", ""),
            "Game URL": url,
            "Developer":
                row.get("Developer", ""),
            "Genre":
                row.get("Genre", ""),
            "Poki Release Date":
                row.get(
                    "Poki Release Date",
                    ""
                ),
            "Previous Period":
                previous_period,
            "Current Period":
                current_period,
        })

    # REMOVED
    for url in sorted(
        previous.keys() - current.keys()
    ):
        row = previous[url]

        changes.append({
            "Change Type": "REMOVED",
            "Game Title":
                row.get("Game Title", ""),
            "Game URL": url,
            "Developer":
                row.get("Developer", ""),
            "Genre":
                row.get("Genre", ""),
            "Poki Release Date":
                row.get(
                    "Poki Release Date",
                    ""
                ),
            "Previous Period":
                previous_period,
            "Current Period":
                current_period,
        })

    return changes


# ============================================================
# STAT / ENGAGEMENT CHANGES
# ============================================================

def create_stat_changes(
    previous_rows,
    current_rows,
    previous_period,
    current_period
):
    if not previous_rows:
        return []

    previous = {
        row["Game URL"]: row
        for row in previous_rows
        if row.get("Game URL")
    }

    current = {
        row["Game URL"]: row
        for row in current_rows
        if row.get("Game URL")
    }

    rows = []

    common_urls = (
        previous.keys()
        & current.keys()
    )

    for url in common_urls:
        old = previous[url]
        new = current[url]

        old_likes = safe_int(
            old.get("Likes")
        )

        new_likes = safe_int(
            new.get("Likes")
        )

        old_dislikes = safe_int(
            old.get("Dislikes")
        )

        new_dislikes = safe_int(
            new.get("Dislikes")
        )

        old_votes = safe_int(
            old.get("Total Votes")
        )

        new_votes = safe_int(
            new.get("Total Votes")
        )

        old_rating = safe_float(
            old.get("Rating")
        )

        new_rating = safe_float(
            new.get("Rating")
        )

        old_engagement = (
            old_likes
            + old_dislikes
        )

        new_engagement = (
            new_likes
            + new_dislikes
        )

        engagement_change = (
            new_engagement
            - old_engagement
        )

        if old_engagement > 0:
            engagement_percent = round(
                (
                    engagement_change
                    / old_engagement
                ) * 100,
                2
            )
        else:
            engagement_percent = ""

        rows.append({
            "Game Title":
                new.get("Game Title", ""),

            "Game URL":
                url,

            "Developer":
                new.get("Developer", ""),

            "Genre":
                new.get("Genre", ""),

            "Previous Likes":
                old_likes,

            "Current Likes":
                new_likes,

            "Likes Change":
                new_likes - old_likes,

            "Previous Dislikes":
                old_dislikes,

            "Current Dislikes":
                new_dislikes,

            "Dislikes Change":
                new_dislikes - old_dislikes,

            "Previous Total Engagement":
                old_engagement,

            "Current Total Engagement":
                new_engagement,

            "Total Engagement Change":
                engagement_change,

            "Engagement Change %":
                engagement_percent,

            "Previous Total Votes":
                old_votes,

            "Current Total Votes":
                new_votes,

            "Total Votes Change":
                new_votes - old_votes,

            "Previous Rating":
                old_rating,

            "Current Rating":
                new_rating,

            "Rating Change":
                round(
                    new_rating - old_rating,
                    3
                ),

            "Previous Period":
                previous_period,

            "Current Period":
                current_period,
        })

    # Most engagement growth first.
    rows.sort(
        key=lambda row:
        row["Total Engagement Change"],
        reverse=True
    )

    return rows


# ============================================================
# CREATE WEEKLY / MONTHLY PERIOD
# ============================================================

def create_period(
    base_dir,
    folder_name,
    current_rows,
    stat_filename
):
    folder = base_dir / folder_name

    if folder.exists():
        raise RuntimeError(
            f"{folder} already exists. "
            f"Snapshots are immutable; "
            f"refusing to overwrite it."
        )

    previous_name, previous_file = (
        find_previous_snapshot(
            base_dir,
            folder_name
        )
    )

    if previous_file:
        previous_rows = read_csv(
            previous_file
        )

        validate_against_previous(
            current_rows,
            previous_rows
        )

    else:
        previous_rows = []
        previous_name = ""

    folder.mkdir(
        parents=True,
        exist_ok=False
    )

    # Raw snapshot
    write_csv(
        folder / "master.csv",
        current_rows,
        MASTER_FIELDS
    )

    # Additions / removals
    catalog_changes = (
        create_catalog_changes(
            previous_rows,
            current_rows,
            previous_name,
            folder_name
        )
    )

    write_csv(
        folder / "catalog_changes.csv",
        catalog_changes,
        CATALOG_CHANGE_FIELDS
    )

    # Likes / dislikes / engagement / votes / rating
    stat_changes = (
        create_stat_changes(
            previous_rows,
            current_rows,
            previous_name,
            folder_name
        )
    )

    write_csv(
        folder / stat_filename,
        stat_changes,
        STAT_CHANGE_FIELDS
    )

    print()
    print(
        f"Created: {folder}"
    )

    print(
        f"    master.csv: "
        f"{len(current_rows)} games"
    )

    print(
        f"    catalog_changes.csv: "
        f"{len(catalog_changes)} changes"
    )

    print(
        f"    {stat_filename}: "
        f"{len(stat_changes)} games"
    )


# ============================================================
# QUARTERLY REPORT
# ============================================================

def create_quarterly_report(date):
    if date.month not in (3, 6, 9, 12):
        return

    quarter = quarter_name(date)

    quarter_folder = (
        QUARTERLY_DIR / quarter
    )

    if quarter_folder.exists():
        print(
            f"Quarterly report already exists: "
            f"{quarter}"
        )
        return

    quarter_start_month = (
        (quarter_number(date.month) - 1)
        * 3
        + 1
    )

    month_names = [
        f"{date.year}-{month:02d}"
        for month in range(
            quarter_start_month,
            quarter_start_month + 3
        )
    ]

    available = []

    for name in month_names:
        master = (
            MONTHLY_DIR
            / name
            / "master.csv"
        )

        if master.exists():
            available.append(
                (name, master)
            )

    if not available:
        print(
            f"No monthly snapshots "
            f"to create {quarter}."
        )
        return

    # Baseline is the last monthly snapshot BEFORE the quarter, so
    # the whole quarter is covered. Fall back to the first month
    # in the quarter if no earlier snapshot exists.
    prior_name, prior_file = find_previous_snapshot(
        MONTHLY_DIR,
        month_names[0]
    )

    if prior_file:
        first_name, first_file = prior_name, prior_file
    else:
        first_name, first_file = available[0]

    last_name, last_file = available[-1]

    if first_file == last_file:
        print(
            f"Only one monthly snapshot available; "
            f"cannot create {quarter}."
        )
        return

    first_rows = read_csv(first_file)
    last_rows = read_csv(last_file)

    validate_against_previous(
        last_rows,
        first_rows
    )

    quarter_folder.mkdir(
        parents=True,
        exist_ok=False
    )

    # Statistical quarter comparison.
    stat_changes = create_stat_changes(
        first_rows,
        last_rows,
        first_name,
        last_name
    )

    write_csv(
        quarter_folder
        / "quarterly_changes.csv",
        stat_changes,
        STAT_CHANGE_FIELDS
    )

    # Aggregate all monthly catalog events
    # belonging to the quarter.
    catalog_events = []

    for month_name_value in month_names:
        change_file = (
            MONTHLY_DIR
            / month_name_value
            / "catalog_changes.csv"
        )

        if change_file.exists():
            catalog_events.extend(
                read_csv(change_file)
            )

    write_csv(
        quarter_folder
        / "catalog_changes.csv",
        catalog_events,
        CATALOG_CHANGE_FIELDS
    )

    print()
    print(
        f"Created quarterly report: "
        f"{quarter}"
    )

    print(
        f"    Comparison: "
        f"{first_name} -> {last_name}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--force",
        choices=[
            "weekly",
            "monthly",
            "both"
        ],
        help=(
            "Force a run for manual testing. "
            "Normally omitted in GitHub Actions."
        )
    )

    args = parser.parse_args()

    ensure_directories()

    now = datetime.now()
    today = now.date()

    weekly, monthly = determine_modes(
        today,
        args.force
    )

    print()
    print("=" * 60)
    print("POKI TRACKER")
    print("=" * 60)
    print(f"Date: {today}")
    print(f"Weekly run: {weekly}")
    print(f"Monthly run: {monthly}")

    if not weekly and not monthly:
        print()
        print(
            "Today is neither Sunday nor "
            "month-end."
        )
        print(
            "No Poki scrape required."
        )
        return

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Only ONE Poki scrape happens here.
    #
    # If today is both Sunday and month-end,
    # this one snapshot is reused for both.
    # --------------------------------------------------------

    rows = scrape_full_catalog(today)

    # WEEKLY
    if weekly:
        week = iso_week_name(today)

        create_period(
            WEEKLY_DIR,
            week,
            rows,
            "weekly_changes.csv"
        )

    # MONTHLY
    if monthly:
        month = month_name(today)

        create_period(
            MONTHLY_DIR,
            month,
            rows,
            "monthly_changes.csv"
        )

        # Quarter-end report uses monthly data.
        if today.month in (3, 6, 9, 12):
            create_quarterly_report(today)

    print()
    print("=" * 60)
    print("COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print()
        print("=" * 60)
        print("FATAL ERROR")
        print("=" * 60)
        print(str(exc))
        print()
        print(
            "No unreliable comparison should "
            "be committed."
        )

        sys.exit(1)