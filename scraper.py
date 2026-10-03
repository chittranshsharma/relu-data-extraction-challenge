#!/usr/bin/env python3
"""
Relu Consultancy Hiring Challenge - Objective 1
Disney Cruise Line scraper  (https://disneycruise.disney.go.com/en-in/)

Flow (follows the challenge doc, Steps 1-6):
  1. Open the en-in homepage.
  2. Click "View Dates" (falls back to the list URL if the click fails).
  3. Wait for cruise cards to load; clear any pre-applied filter.
  4. Scroll to the end of the page until every card is loaded.
  5. Clean: raw text only, no empty fields, no duplicates.
  6. Write results.csv and print the answers to the five questions.

Notes learned from the live site (verified by running against it):
  * Akamai answers 403 "Access Denied" to HEADLESS browsers, so this script runs
    a normal visible Chrome window by default. Do not minimise it while it runs.
    (--headless exists but is expected to be blocked.)
  * Cards load by scrolling (a few dozen per scroll step), there is no pager.
  * The "N Cruises" counter is checked against the sum of every card's
    "Show N Dates" before a final CSV is written. In this deliverable run,
    both were 948. Pacific Coast matches were 2 cards / 5 date options; a
    separate live filter pass was not verified.

Setup:
    pip install playwright pandas
    playwright install chromium        # only needed if Google Chrome is not installed

Run:
    python scraper.py                  # full verified run
    python scraper.py --max-pages 3    # partial diagnostic; no final CSV is written
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BASE = "https://disneycruise.disney.go.com"
HOME_URL = f"{BASE}/en-in/"
LIST_URL = f"{BASE}/en-in/cruises-destinations/list/"  # fallback only

OUT_DIR = Path(__file__).resolve().parent
RAW_CSV = OUT_DIR / "disney_cruises_raw.csv"   # temporary checkpoint
FINAL_CSV = OUT_DIR / "results.csv"

COLUMNS = [
    "title", "theme_badge", "quality_badge", "duration_nights",
    "departure_port", "sailing_to", "price_usd", "price_inr",
    "num_dates", "cta_link", "scroll_step",
]

# "Holiday" = the site's Holiday Cruises category (theme badge "Very Merrytime").
# Halloween is a separate category on the site ("Halloween Cruises" vs "Holiday
# Cruises" in the navigation), so it is counted and printed separately.
HOLIDAY_REGEX = r"holiday|merrytime|christmas|new year|thanksgiving"
HALLOWEEN_REGEX = r"halloween"
PACIFIC_FALLBACK_REGEX = r"pacific coast"   # only used if the site-filter pass fails

TITLE_RE = re.compile(r"^\d+\s*-?\s*Night\b", re.I)
DESC_RE = re.compile(r"stateroom for you|learn more\s*$", re.I)
SHOW_DATES_RE = re.compile(r"Show\s+(\d+)\s+Dates?", re.I)
USD_RE = re.compile(r"\$\s*[\d,]+(?:\.\d+)?\s*USD", re.I)
INR_RE = re.compile(r"\u20b9\s*[\d,]+(?:\.\d+)?\s*INR", re.I)
COUNTER_RE = re.compile(r"^\s*([\d,]+)\s+Cruises?\s*$", re.I | re.M)

log = logging.getLogger("disney")

# One round trip returns every card currently in the DOM.
JS_SNAPSHOT = r"""
() => {
  const els = Array.from(document.querySelectorAll(
    'div.product-card-wrapper:not(.product-card-pricing-wrapper):not(.product-card-primary-wrapper)'));
  return els.map(el => {
    let text = el.innerText || '';
    let href = '';
    for (const a of el.querySelectorAll('a')) {
      if (/Show\s+\d+\s+Dates?/i.test(a.innerText || '') && a.getAttribute('href')) {
        href = a.getAttribute('href'); break;
      }
    }
    const destinations = Array.from(el.querySelectorAll(
      'div.product-card-content__sailing-to .ul-port-list li'))
      .map(x => (x.innerText || '').trim()).filter(Boolean);
    return {text: text, href: href || '', destinations: destinations};
  });
}
"""


# --------------------------------------------------------------------------
# Parsing (pure functions, unit-testable without a browser)
# --------------------------------------------------------------------------
def _first(regex: re.Pattern, text: str) -> str:
    m = regex.search(text)
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def parse_card_text(text: str, href: str = "", scroll_step: int = 1) -> dict | None:
    """Turn one card's visible text into a record of RAW strings.

    Badges are found by position: any line before the "N-Night ..." title
    line is a badge (the line containing "Guaranteed" is the quality badge,
    other lines are the theme badge). The stateroom description line
    ("... will select your stateroom ... Learn More") is ignored.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    t_idx = next((i for i, ln in enumerate(lines) if TITLE_RE.match(ln)), None)
    title = lines[t_idx] if t_idx is not None else ""
    pre = [ln for ln in (lines[:t_idx] if t_idx is not None else []) if not DESC_RE.search(ln)]
    quality = next((ln for ln in pre if re.search(r"guaranteed", ln, re.I)), "")
    theme = " | ".join(ln for ln in pre if ln != quality)

    post = lines[t_idx + 1:] if t_idx is not None else lines
    sailing_to = ""
    for i, ln in enumerate(post):
        if ln.lower() == "sailing to":
            dest = []
            for nxt in post[i + 1:]:
                if re.match(r"price from", nxt, re.I):
                    break
                dest.append(nxt)
            sailing_to = " | ".join(dest)
            break

    dur = re.match(r"^(\d+)", title)
    # "3-Night Cruise from Port Canaveral with 2 Stops ..." / "... from San Diego ending in Vancouver"
    port = re.search(r"\bfrom\s+(.+?)(?:\s+(?:ending|with)\b.*)?$", title, re.I)
    dates = SHOW_DATES_RE.search(text or "")

    return {
        "title": title,
        "theme_badge": theme,
        "quality_badge": quality,
        "duration_nights": int(dur.group(1)) if dur else "",
        "departure_port": port.group(1).strip() if port else "",
        "sailing_to": sailing_to,
        "price_usd": _first(USD_RE, text or ""),   # raw text, e.g. "$1,750 USD"
        "price_inr": _first(INR_RE, text or ""),   # raw text, e.g. "\u20b91,67,726 INR"
        "num_dates": int(dates.group(1)) if dates else "",
        "cta_link": href,
        "scroll_step": scroll_step,
    }


def is_complete(rec: dict) -> bool:
    """A card still rendering has no title / date count yet; wait for the next snapshot."""
    return bool(rec["title"]) and rec["num_dates"] != ""


def record_key(rec: dict) -> tuple:
    """Identity of a card. page_number and cta_link are deliberately excluded
    (the 'Show N Dates' links are just '#', so they carry no identity), and so are
    badges / destination, which sometimes render a moment after the rest of the card."""
    return (rec["title"], rec["departure_port"], rec["price_usd"],
            rec["price_inr"], rec["num_dates"])


LATE_FIELDS = ("theme_badge", "quality_badge", "sailing_to")  # can render after the rest of the card


def merge_richer(old: dict, new: dict) -> None:
    """Same card captured twice: keep the more complete text for late-rendering fields."""
    for f in LATE_FIELDS:
        if len(new[f]) > len(old[f]):
            old[f] = new[f]


# --------------------------------------------------------------------------
# Cleaning (doc Step 5)
# --------------------------------------------------------------------------
def clean(records: list[dict]) -> pd.DataFrame:
    """Dedupe, fill absent fields explicitly, drop unusable rows. Logs everything."""
    df = pd.DataFrame(records, columns=COLUMNS).fillna("")
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
    n0 = len(df)

    # Rows without a title or a "Show N Dates" count are not real cards.
    bad = (df["title"] == "") | (df["num_dates"].astype(str) == "")
    if bad.any():
        log.warning("Dropping %d unparsable rows (no title / no date count)", int(bad.sum()))
    df = df[~bad]

    key_cols = ["title", "departure_port", "price_usd", "price_inr", "num_dates"]
    before = len(df)
    df = df.drop_duplicates(subset=key_cols, keep="first")
    log.info("Duplicates removed: %d", before - len(df))

    # Absent badges are explicit "None" (doc: no empty fields).
    for col in ("theme_badge", "quality_badge"):
        df.loc[df[col] == "", col] = "None"

    # Fields the card genuinely does not show. Filled with a visible marker, never invented.
    placeholders = {
        "departure_port": "Not shown on card",
        "sailing_to": "Not shown on card",
        "price_usd": "Not shown on card",
        "price_inr": "Not shown on card",
        "cta_link": "Not shown on card",
    }
    for col, marker in placeholders.items():
        n_missing = int((df[col] == "").sum())
        if n_missing:
            log.warning("%d rows have no %s on the card -> filled with '%s'", n_missing, col, marker)
            if n_missing <= 10:
                for t in df.loc[df[col] == "", "title"]:
                    log.warning("    missing %s: %s", col, t)
            df.loc[df[col] == "", col] = marker

    df["duration_nights"] = pd.to_numeric(df["duration_nights"], errors="coerce").astype("Int64")
    df["num_dates"] = pd.to_numeric(df["num_dates"], errors="coerce").astype("Int64")

    log.info("Cleaning: %d raw -> %d clean rows", n0, len(df))
    return df.reset_index(drop=True)[COLUMNS]


# --------------------------------------------------------------------------
# Answers (doc "Submission Guidelines")
# --------------------------------------------------------------------------
def print_answers(df: pd.DataFrame, site_total: int | None, pacific: dict | None) -> None:
    port = df["departure_port"].str.lower()
    miami = int(port.str.contains("miami").sum())
    london = int(port.str.contains("london").sum())
    both = int(port.str.contains("miami|london").sum())
    holiday_text = (df["theme_badge"] + " " + df["title"]).str.lower()
    holiday = int(holiday_text.str.contains(HOLIDAY_REGEX, regex=True).sum())
    halloween = int(holiday_text.str.contains(HALLOWEEN_REGEX, regex=True).sum())
    more_than_2 = int((df["num_dates"].fillna(0) > 2).sum())
    cards = len(df)
    sailings = int(df["num_dates"].fillna(0).sum())

    print("\n" + "=" * 66)
    print("CHALLENGE ANSWERS  (computed from results.csv)")
    print("=" * 66)
    print("NOTE: each card is an itinerary and may offer multiple booking dates.")
    print("      The live site counter matched the sum of those date options.")
    print("-" * 66)
    if pacific:
        print(f"(i)   Pacific (site 'Sailing to: Pacific Coast' filter):")
        print(f"        cards = {pacific['cards']}   |  site counter = {pacific['counter']}"
              f"   |  sum of dates = {pacific['sailings']}")
    else:
        text = (df["sailing_to"] + " " + df["title"]).str.lower()
        pacific_rows = text.str.contains(PACIFIC_FALLBACK_REGEX, regex=True)
        n = int(pacific_rows.sum())
        pacific_dates = int(df.loc[pacific_rows, "num_dates"].fillna(0).sum())
        print(f"(i)   Pacific Coast match: itinerary cards = {n} | booking-date options = {pacific_dates}")
    holiday_dates = int(df.loc[holiday_text.str.contains(HOLIDAY_REGEX, regex=True), "num_dates"].fillna(0).sum())
    print(f"(ii)  Total cruises: itinerary cards = {cards}   |  booking-date options = {sailings}"
          f"   |  site counter = {site_total}")
    if site_total is not None:
        print("        completeness check (sum of dates == site counter): "
              + ("OK" if sailings == site_total else "MISMATCH - investigate"))
    print(f"(iii) Holiday keyword/theme match: itinerary cards = {holiday} | booking-date options = {holiday_dates}"
          f"      [Halloween separately: {halloween} itinerary cards]")
    print(f"(iv)  Itinerary cards offering more than 2 dates: {more_than_2}")
    print(f"(v)   Departing from Miami          : {miami}")
    print(f"      Departing from London         : {london}")
    print(f"      Miami + London combined       : {both}")
    print("=" * 66)
    print("Theme badge counts:")
    print(df["theme_badge"].value_counts().to_string())
    print("Departure port counts:")
    print(df["departure_port"].value_counts().to_string())
    print("=" * 66 + "\n")


# --------------------------------------------------------------------------
# Browser steps
# --------------------------------------------------------------------------
def snapshot(page) -> list[dict]:
    return page.evaluate(JS_SNAPSHOT)


def snap_sig(snap: list[dict]) -> tuple:
    if not snap:
        return (0, "", "")
    return (len(snap), snap[0]["text"][:80], snap[-1]["text"][:80])


def read_site_total(page) -> int | None:
    """The 'N Cruises' counter (counts sailing dates, see module docstring)."""
    try:
        m = COUNTER_RE.search(page.locator("h2.available-cruises-wrapper__title").inner_text(timeout=1000))
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None


def accept_consent(page) -> None:
    """Click an Accept/Agree button if one shows up. Safe to skip."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"^(accept|agree|i agree|ok)\b", re.I))
        if btn.count() and btn.first.is_visible():
            btn.first.click(timeout=3000)
            log.info("Consent banner accepted")
    except Exception:
        pass


def wait_for_cards(page, timeout: float = 45.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if snapshot(page):
                return True
        except Exception:
            pass
        page.mouse.wheel(0, 600)  # cards can hydrate lazily below the fold
        time.sleep(1)
    return False


def wait_for_results_ready(page, timeout: float = 90.0) -> int:
    """Do not start a scrape until the live result counter and cards are rendered."""
    end = time.time() + timeout
    while time.time() < end:
        total = read_site_total(page)
        if total is not None and snapshot(page):
            return total
        time.sleep(1)
    raise RuntimeError("Results did not expose both cruise cards and the site counter; refusing an unverified CSV.")


def debug_dump(page, tag: str) -> None:
    """Save screenshot + visible text so we can see WHY a step failed
    (blank page vs. 'Access Denied' vs. different layout)."""
    try:
        page.screenshot(path=f"debug_{tag}.png")
        body = page.inner_text("body")
        Path(f"debug_{tag}.txt").write_text(
            f"url={page.url}\ntitle={page.title()}\n---\n{body[:3000]}", encoding="utf-8")
        log.error("DEBUG[%s] url=%s | title=%r | text=%r", tag, page.url, page.title(),
                  body[:300].replace("\n", " | "))
    except Exception as exc:
        log.error("DEBUG[%s] dump failed: %s", tag, exc)


def goto_home(page) -> None:
    resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
    status = resp.status if resp else None
    log.info("Homepage HTTP status: %s", status)
    if status == 403:
        debug_dump(page, "blocked")
        raise RuntimeError("HTTP 403 Access Denied: the site blocks headless/automated-looking "
                           "browsers. Run without --headless (visible Chrome window).")
    time.sleep(4)
    accept_consent(page)


def click_view_dates(page) -> bool:
    try:
        btn = page.locator("button.view-cruises-button")
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"view dates", re.I))
        if not btn.count():
            btn = page.get_by_text(re.compile(r"^\s*view dates\s*$", re.I))
        btn.first.click(timeout=15000)
        log.info("Clicked 'View Dates'")
        return True
    except Exception as exc:
        log.warning("Could not click 'View Dates' (%s)", exc)
        return False


def open_search_results(page) -> None:
    """Doc Steps 1-3: homepage -> View Dates -> wait for cards."""
    goto_home(page)
    if click_view_dates(page) and wait_for_cards(page, 45):
        return
    log.warning("Falling back to the list URL")
    resp = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    log.info("List URL HTTP status: %s", resp.status if resp else "n/a")
    accept_consent(page)
    if not wait_for_cards(page, 45):
        debug_dump(page, "no_cards")
        raise RuntimeError("No cruise cards appeared. See debug_home.* and debug_no_cards.* "
                           "(screenshot + text) to tell a block from a layout change.")


def clear_filters(page) -> None:
    """If a filter chip is pre-applied, click 'Clear All' so every cruise shows."""
    body = page.inner_text("body")
    m = re.search(r"(\d+)\s+Filters?\s+Applied", body, re.I)
    if not m:
        log.info("No pre-applied filter detected")
        return
    log.warning("'%s Filter(s) Applied' detected, clearing", m.group(1))
    try:
        before = snap_sig(snapshot(page))
        page.get_by_text(re.compile(r"^\s*Clear (All|Filters)\s*$", re.I)).first.click(timeout=5000)
        end = time.time() + 15
        while time.time() < end and snap_sig(snapshot(page)) == before:
            time.sleep(0.5)
        time.sleep(1.5)
    except Exception as exc:
        log.error("Clear filters failed: %s", exc)


def _scroll_bottom(page) -> None:
    for _ in range(3):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.7)


def _wait_change(page, before: tuple, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if snap_sig(snapshot(page)) != before:
            return True
    return False


def harvest(page, max_steps: int, delay: float, site_total: int | None,
            save_raw: bool = True) -> list[dict]:
    """Doc Step 4: scroll to the end and collect every card.

    Stops when scrolling adds nothing new AND the collected 'Show N Dates'
    total matches the site counter (or after several extra patient attempts).
    """
    seen: dict[tuple, dict] = {}
    step, stale = 1, 0
    while True:
        snap = snapshot(page)
        new = skipped = 0
        for item in snap:
            # `#` is the live button's actual href; keep it as observed, not as a fake URL.
            rec = parse_card_text(item["text"], item["href"], step)
            if rec is None:
                continue
            if item.get("destinations"):
                rec["sailing_to"] = " | ".join(item["destinations"])
            if not is_complete(rec):      # still rendering; will be picked up next time
                skipped += 1
                continue
            k = record_key(rec)
            if k not in seen:
                seen[k] = rec
                new += 1
            else:
                merge_richer(seen[k], rec)
        sailings = sum(int(r["num_dates"]) for r in seen.values())
        log.info("step %3d | cards in DOM=%4d | new=%3d | unique=%4d | dates=%4d%s%s",
                 step, len(snap), new, len(seen), sailings,
                 f"/{site_total}" if site_total else "",
                 f" | skipped(rendering)={skipped}" if skipped else "")
        stale = stale + 1 if new == 0 else 0
        if save_raw and step % 5 == 0:
            pd.DataFrame(list(seen.values()), columns=COLUMNS).to_csv(RAW_CSV, index=False, encoding="utf-8-sig")
        complete = site_total is not None and sailings == site_total
        if sailings > (site_total or float("inf")):
            raise RuntimeError(f"Scraped sailing-date sum {sailings} exceeds live site counter {site_total}; refusing final CSV.")
        if complete and stale >= 3:
            break
        if stale >= 6:
            raise RuntimeError(f"Scrape stalled at {len(seen)} cards / {sailings} dates; live counter is {site_total}. Refusing incomplete CSV.")
        if step >= max_steps:
            raise RuntimeError(f"Reached max scroll steps ({max_steps}) at {len(seen)} cards / {sailings} dates; live counter is {site_total}. Refusing incomplete CSV.")
        _scroll_bottom(page)
        _wait_change(page, snap_sig(snap), 8 if stale else 5)
        step += 1
        time.sleep(delay)
    records = list(seen.values())
    if save_raw:
        pd.DataFrame(records, columns=COLUMNS).to_csv(RAW_CSV, index=False, encoding="utf-8-sig")
        log.info("Raw data saved to %s (%d records)", RAW_CSV, len(records))
    return records


def pacific_filter_pass(ctx, delay: float) -> dict | None:
    """Apply the site's own 'Sailing to: Pacific Coast' filter (homepage filter bar,
    then View Dates) and count what the site returns. Non-fatal on failure."""
    page = ctx.new_page()
    page.set_default_timeout(30000)
    try:
        goto_home(page)
        page.get_by_text(re.compile(r"^\s*Sailing to\s*$", re.I)).first.click()
        time.sleep(1.5)
        page.locator("button.image-picker-item:has-text('Pacific Coast')").first.click()
        time.sleep(1)
        if not click_view_dates(page) or not wait_for_cards(page, 45):
            raise RuntimeError("filtered results did not load")
        time.sleep(5)  # let the counter settle
        counter = read_site_total(page)
        recs = harvest(page, 30, delay, counter, save_raw=False)
        res = {"cards": len(recs), "counter": counter,
               "sailings": sum(int(r["num_dates"]) for r in recs)}
        log.info("Pacific Coast filter -> cards=%s counter=%s sum_dates=%s",
                 res["cards"], res["counter"], res["sailings"])
        return res
    except Exception as exc:
        log.error("Pacific filter pass failed: %s", exc)
        return None
    finally:
        page.close()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def launch(p, headless: bool, channel: str | None):
    """Prefer the installed Google Chrome; fall back to bundled Chromium."""
    if headless:
        log.warning("--headless: the site is known to answer 403 to headless browsers")
    if channel:
        try:
            return p.chromium.launch(headless=headless, channel=channel)
        except Exception as exc:
            log.warning("Could not start channel '%s' (%s); using bundled Chromium", channel, exc)
    return p.chromium.launch(headless=headless)


def main() -> int:
    ap = argparse.ArgumentParser(description="Disney Cruise Line scraper (Relu challenge, Objective 1)")
    ap.add_argument("--max-pages", type=int, default=500, help="maximum scroll steps; incomplete runs fail the completeness gate")
    ap.add_argument("--headless", action="store_true", help="hidden browser (the site normally blocks this)")
    ap.add_argument("--channel", default="chrome", help="browser channel; default 'chrome' (installed Google Chrome)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between steps (be polite)")
    ap.add_argument("--pacific-filter-pass", action="store_true", help="optionally open/apply the Pacific Coast site filter (may fail if Disney changes its UI)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    from playwright.sync_api import sync_playwright

    pacific = None
    with sync_playwright() as p:
        browser = launch(p, args.headless, args.channel)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="en-IN")
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            open_search_results(page)
            clear_filters(page)
            time.sleep(2)
            site_total = wait_for_results_ready(page)
            log.info("Site counter shows: %s (sailing-date total)", site_total)
            records = harvest(page, args.max_pages, args.delay, site_total)
            if args.pacific_filter_pass and args.max_pages >= 500:
                pacific = pacific_filter_pass(ctx, args.delay)
        finally:
            browser.close()

    df = clean(records)
    if df.empty or df["title"].eq("").any() or df["num_dates"].isna().any():
        raise RuntimeError("CSV quality gate failed: empty dataset, title, or booking-date count.")
    if site_total is not None and int(df["num_dates"].sum()) != site_total:
        raise RuntimeError(f"CSV quality gate failed: dates sum to {int(df['num_dates'].sum())}, site counter is {site_total}.")
    df.to_csv(FINAL_CSV, index=False, encoding="utf-8-sig")
    log.info("Wrote %s (%d rows)", FINAL_CSV, len(df))
    print_answers(df, site_total if args.max_pages >= 500 else None, pacific)
    return 0


if __name__ == "__main__":
    sys.exit(main())
