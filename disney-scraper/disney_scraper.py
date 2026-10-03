import json
import os
import re
import sys

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://disneycruise.disney.go.com/en-in/"
CARD = "dcl-product-card"
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
RAW_PATH = "raw_cruises.json"
OUT_PATH = "results.csv"
HOLIDAY_WORDS = ["merrytime", "halloween", "holiday", "christmas", "festive"]


FIELDS = {
    "title":      "h2.product-card-content__name",
    "sailing_to": "div.product-card-content__sailing-to",
    "badge":      "span.chip-label",
    "price_inr":  "span.wrapper-price__pricing",
    "dates_btn":  "a.product-card-footer-wrapper__btn",
}


SHADOW_TEXT_JS = """
el => {
  const root = el.shadowRoot || el;
  const out = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const p = n.parentElement;
    if (p && (p.tagName === 'STYLE' || p.tagName === 'SCRIPT')) continue;
    const t = n.textContent.trim();
    if (t) out.push(t);
  }
  return out.join(' ');
}
"""


RIBBON_JS = """
el => {
  const p = el.querySelector('.product-card-pizzazz');
  if (!p) return '';
  const alts = [...p.querySelectorAll('img')].map(i => i.alt).filter(Boolean);
  return [p.textContent.trim(), ...alts].filter(Boolean).join(' | ');
}
"""


def block_heavy_resources(route):
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


def accept_cookies_if_present(page):
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=5000)
        print("Cookie banner accepted")
    except PWTimeout:
        print("No cookie banner, continuing")


def open_results(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    accept_cookies_if_present(page)

    page.get_by_text("View Dates", exact=True).first.click(timeout=30000)
    page.locator(CARD).first.wait_for(timeout=60000)
    print("Results URL:", page.url)


def wait_for_count_to_settle(page, stable_checks=3, interval_ms=1000, max_wait_ms=30000):
    last, stable, waited = -1, 0, 0
    while waited < max_wait_ms:
        current = page.locator(CARD).count()
        if current == last:
            stable += 1
            if stable >= stable_checks:
                return current
        else:
            stable, last = 0, current
        page.wait_for_timeout(interval_ms)
        waited += interval_ms
    return last


def load_all_cards(page, max_rounds=500, grow_timeout_ms=8000):
    previous = page.locator(CARD).count()
    print(f"Initial cards: {previous}")
    misses = 0
    for round_no in range(1, max_rounds + 1):

        page.locator(CARD).last.evaluate("el => el.scrollIntoView({block: 'end'})")
        page.mouse.wheel(0, 3000)
        try:
            page.wait_for_function(
                "n => document.querySelectorAll('dcl-product-card').length > n",
                arg=previous,
                timeout=grow_timeout_ms,
            )
            misses = 0
        except PWTimeout:
            misses += 1
            print(f"Scroll {round_no}: no growth (miss {misses}/2)")
            if misses >= 2:
                break
        previous = page.locator(CARD).count()
        if round_no % 10 == 0:
            print(f"Scroll {round_no}: {previous} cards")
    total = wait_for_count_to_settle(page)
    print(f"Done. Total cards: {total} (loaded in {round_no} scroll batches)")
    return total


def first_text(card, selector):
    loc = card.locator(selector)
    if loc.count() == 0:
        return ""
    return loc.first.inner_text().strip()


def shadow_text(card, selector):
    loc = card.locator(selector)
    if loc.count() == 0:
        return ""
    return loc.first.evaluate(SHADOW_TEXT_JS).strip()


def extract_cards(page):
    records = []
    for i, card in enumerate(page.locator(CARD).all(), start=1):
        rec = {"card_index": i}
        for field, selector in FIELDS.items():
            rec[field] = first_text(card, selector)
        rec["price_usd"] = shadow_text(card, "wdpr-price")
        rec["ribbon"] = card.evaluate(RIBBON_JS)
        html = card.evaluate("el => el.innerHTML.toLowerCase()")
        rec["holiday_keyword"] = next((w for w in HOLIDAY_WORDS if w in html), "")
        records.append(rec)
    print(f"Extracted {len(records)} records")
    return records


def save_json(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"Saved raw data -> {path}")


def tidy_locations(raw):
    parts = [p.strip(" |") for p in re.split(r"[\n|]", raw or "")]
    parts = [p for p in parts if p and p.lower() not in ("sailing to", "sailing to:")]
    return " | ".join(parts)


def squash(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def clean(records):
    df = pd.DataFrame(records)

    df["sailing_to"] = df["sailing_to"].apply(tidy_locations)
    for col in ["title", "badge", "price_inr", "price_usd", "dates_btn", "ribbon", "holiday_keyword"]:
        df[col] = df[col].apply(squash)


    df["departure_port"] = df["title"].str.extract(r"\bfrom\s+(.+)$", flags=re.I)[0].fillna("").str.strip()
    df["nights"] = df["title"].str.extract(r"(\d+)\s*-?\s*Night", flags=re.I)[0].fillna("")
    df["dates_available"] = df["dates_btn"].str.extract(r"(\d+)")[0].fillna(0).astype(int)
    df["holiday_cruise"] = df["holiday_keyword"].ne("").map({True: "Yes", False: "No"})


    no_loc = df["sailing_to"] == ""
    df.loc[no_loc, "sailing_to"] = "Round trip from " + df.loc[no_loc, "departure_port"]
    print(f"Filled {no_loc.sum()} empty 'Sailing To' values with the departure port")


    df["badge"] = df["badge"].replace("", "None")
    df["ribbon"] = df["ribbon"].replace("", "None")
    df["price_usd"] = df["price_usd"].replace("", "Not Available")
    df["departure_port"] = df["departure_port"].replace("", "Not Available")
    df["nights"] = df["nights"].replace("", "Not Available")


    before = len(df)
    df = df.drop(columns=["card_index", "holiday_keyword"]).drop_duplicates().reset_index(drop=True)
    print(f"Removed {before - len(df)} duplicate rows")

    df = df.rename(columns={
        "title": "Title", "departure_port": "Departure Port", "sailing_to": "Sailing To",
        "nights": "Nights", "price_usd": "Price From (USD)", "price_inr": "Price From (INR)",
        "dates_available": "Dates Available", "dates_btn": "Dates Button Text",
        "badge": "Badge", "ribbon": "Holiday Ribbon", "holiday_cruise": "Holiday Cruise",
    })
    df = df[["Title", "Departure Port", "Sailing To", "Nights", "Price From (USD)", "Price From (INR)",
             "Dates Available", "Dates Button Text", "Badge", "Holiday Ribbon", "Holiday Cruise"]]

    empties = (df.astype(str).apply(lambda c: c.str.strip() == "")).sum().sum()
    print(f"Empty cells after cleaning: {empties}")
    return df


def answer_questions(df):
    has = lambda col, word: df[col].str.contains(word, case=False, na=False)
    pacific = df[has("Title", "pacific") | has("Sailing To", "pacific")]
    holiday = df[df["Holiday Cruise"] == "Yes"]
    more_than_2 = df[df["Dates Available"] > 2]
    miami = int(has("Departure Port", "miami").sum())
    london = int(has("Departure Port", "london").sum())

    print("\n" + "=" * 60)
    print("ANSWERS")
    print("=" * 60)
    print(f"(i)   Pacific as destination        : {len(pacific)}")
    print(f"(ii)  Total cruises (itinerary cards): {len(df)}")
    print(f"      Total sailings (sum of dates)  : {int(df['Dates Available'].sum())}  <- site header shows this as 'Cruises'")
    print(f"(iii) Holiday cruises               : {len(holiday)}")
    print(f"(iv)  Cruises with more than 2 dates : {len(more_than_2)}")
    print(f"(v)   Departing Miami               : {miami}")
    print(f"      Departing London              : {london}")
    print(f"      Miami + London                : {miami + london}")
    print("=" * 60)

    print("\nDeparture ports found (for checking v):")
    print(df["Departure Port"].value_counts().to_string())
    print("\nPacific cruises (for checking i):")
    print(pacific["Title"].to_string() if len(pacific) else "  none")
    print("\nHoliday cruises (for checking iii):")
    print(holiday["Title"].to_string() if len(holiday) else "  none")


def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.route("**/*", block_heavy_resources)
        open_results(page)
        load_all_cards(page)
        records = extract_cards(page)
        browser.close()
    save_json(records, RAW_PATH)
    return records


def main():
    if "--clean-only" in sys.argv:
        with open(RAW_PATH, encoding="utf-8") as f:
            records = json.load(f)
        print(f"Loaded {len(records)} raw records from {RAW_PATH}")
    else:
        records = scrape()

    df = clean(records)
    df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"Saved cleaned data -> {OUT_PATH} ({len(df)} rows)")
    answer_questions(df)


if __name__ == "__main__":
    main()