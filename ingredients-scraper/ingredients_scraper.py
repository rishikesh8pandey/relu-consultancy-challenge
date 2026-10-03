import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://www.ingredientsnetwork.com/"
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
MAX_PAGES = int(os.environ.get("MAX_PAGES", "300"))
MAX_COMPANIES = int(os.environ.get("MAX_COMPANIES", "0"))
DELAY_S = 0.5
RAW_PATH = "raw_companies.csv"
OUT_PATH = "results.csv"
COMPANY_LINK = re.compile(r"-comp\d+\.html$")
SEARCH_JSON_PATH = "search_response.json"

REQUIRED = ["Company Name", "Company Description", "Sales Markets", "Primary Business Activity",
            "Categories", "Events", "Address", "Email", "Telephone", "Website"]


PHYSICAL_FORMATS = ["Physical Formats", "Blanched", "Chunked", "Concentrated", "Diced", "Dried - air dried",
                    "Dried - freeze dried", "Extracted", "Extruded", "Flaked", "Germinated", "Ground",
                    "Infused", "Liquid", "Milled", "Other Physical Formats", "Powdered", "Roasted",
                    "Shelled", "Sliced", "Solid"]


COMPANY_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const isHeading = n => n.nodeType === 1 && /^H[1-6]$/.test(n.tagName);
  const level = h => parseInt(h.tagName[1], 10);
  const textOf = n => n.nodeType === 3 ? n.textContent : (n.nodeType === 1 ? (n.innerText || n.textContent) : '');
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5')];

  const after = h => {
    const out = [];
    if (!h) return out;
    let n = h.nextSibling;
    while (n) {
      if (isHeading(n) && level(n) <= level(h)) break;
      out.push(n);
      n = n.nextSibling;
    }
    return out;
  };
  const textAfter = h => clean(after(h).map(textOf).join(' '));
  const find = (tags, re) => headings.find(h => tags.includes(h.tagName) && re.test(clean(h.textContent)));

  const h1 = document.querySelector('h1');
  const name = clean(h1 ? h1.textContent : document.title.replace(/^About\s+/i, ''));
  const description = textAfter(find(['H2','H3','H4'], /^company description$/i));

  const facts = {};
  document.querySelectorAll('tr').forEach(tr => {
    const c = tr.querySelectorAll('th,td');
    if (c.length >= 2) facts[clean(c[0].textContent).replace(/:$/, '').toLowerCase()] = clean(c[1].textContent);
  });
  document.querySelectorAll('dt').forEach(dt => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') facts[clean(dt.textContent).replace(/:$/, '').toLowerCase()] = clean(dd.textContent);
  });

  let categories = '';
  const catH = find(['H2','H3'], /^categories affiliated/i);
  if (catH) {
    let nodes = after(catH).filter(n => n.nodeType === 1);
    let links = nodes.flatMap(e => [...(e.tagName === 'A' ? [e] : []), ...e.querySelectorAll('a')]);
    if (!links.length && catH.parentElement) links = [...catH.parentElement.querySelectorAll('a')];
    categories = [...new Set(links.map(a => clean(a.textContent)).filter(Boolean))].join(' | ');
  }
  if (!categories) categories = (facts['affiliated categories'] || '').replace(/\s*More$/i, '');

  let events = '';
  const evH = find(['H2'], /^upcoming events$/i);
  if (evH) {
    const nodes = after(evH).filter(n => n.nodeType === 1);
    let hs = nodes.flatMap(e => [...(e.tagName === 'H3' ? [e] : []), ...e.querySelectorAll('h3')]);
    if (!hs.length && evH.parentElement) hs = [...evH.parentElement.querySelectorAll('h3')];
    events = [...new Set(hs.map(h => clean(h.textContent)).filter(t => t && !/^recently at$/i.test(t)))].join(' | ');
  }

  const contact = (label, firstOnly) => {
    for (const tag of ['H3', 'H4', 'H5']) {
      for (const h of headings.filter(x => x.tagName === tag && clean(x.textContent).toLowerCase() === label)) {
        const parts = after(h).map(n => clean(textOf(n))).filter(t => t && !/view all contact/i.test(t));
        if (parts.length) return firstOnly ? parts[0] : parts.join(' ');
      }
    }
    return '';
  };

  const cf = document.querySelector('[data-cfemail]');
  const cfLink = document.querySelector('a[href*="email-protection#"]');
  const cfHex = cf ? cf.getAttribute('data-cfemail') : (cfLink ? cfLink.getAttribute('href').split('#')[1] : '');

  return {
    "Company Name": name,
    "Company Description": description,
    "Sales Markets": facts['sales markets'] || '',
    "Primary Business Activity": facts['primary business activity'] || '',
    "Categories": categories,
    "Events": events,
    "Address": contact('address', false),
    "Email": contact('email', true),
    "Email CF Hex": cfHex || '',
    "Telephone": contact('telephone', true),
    "Website": contact('website', true),
  };
}
"""


def block_heavy_resources(route):
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept all')",
    "button:has-text('Allow all')",
    "button:has-text('Accept')",
    "a:has-text('Accept')",
    "button:has-text('I agree')",
]


def accept_cookies_if_present(page):
    page.wait_for_timeout(2000)
    for sel in COOKIE_SELECTORS:
        loc = page.locator(sel).filter(visible=True)
        if loc.count():
            try:
                loc.first.click(timeout=5000)
                print(f"Cookie banner accepted ({sel})")
                page.wait_for_timeout(1000)
                return
            except Exception:
                continue
    print("No cookie banner, continuing")


def decode_cf_email(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def company_links_on_page(page):
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    return {h.split("#")[0].split("?")[0] for h in hrefs if COMPANY_LINK.search(h.split("#")[0].split("?")[0])}


def scroll_until_stable(page, tries=3):
    last = -1
    for _ in range(tries):
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(800)
        n = len(company_links_on_page(page))
        if n == last:
            break
        last = n


SHOW_MORE_RESULTS = re.compile(r"show\s+more\s+results", re.I)
OTHER_NEXT = ["a[rel='next']", "a[aria-label*='next' i]", "li.next a", "a:has-text('Next')",
              "button:has-text('Load more')", "a:has-text('Load more')"]


def go_next(page):
    before_count = len(company_links_on_page(page))
    before_url = page.url
    loc = page.get_by_text(SHOW_MORE_RESULTS).filter(visible=True)
    if loc.count() == 0:
        loc = None
        for sel in OTHER_NEXT:
            cand = page.locator(sel).filter(visible=True)
            if cand.count():
                loc = cand
                break
        if loc is None:
            return False
    target = loc.last
    try:
        target.scroll_into_view_if_needed(timeout=5000)
        target.click(timeout=10000)
    except Exception:
        target.evaluate("el => el.click()")
    for _ in range(30):
        page.wait_for_timeout(500)
        if len(company_links_on_page(page)) > before_count or page.url != before_url:
            page.wait_for_timeout(500)
            return True
    return False


def run_search(page):
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    accept_cookies_if_present(page)
    home_links = company_links_on_page(page)
    btn = page.locator("button:has-text('Search'), input[type='submit'][value*='Search' i], "
                       "a:has-text('Search')").filter(visible=True).first
    body = ""
    try:
        with page.expect_response(lambda r: "search46json" in r.url, timeout=30000) as info:
            btn.click(timeout=30000)
        body = info.value.text()
        print("Captured search JSON:", info.value.url)
    except Exception:
        page.wait_for_timeout(3000)
    if page.url.rstrip("/") == BASE.rstrip("/"):
        print("URL did not change after clicking Search - pressing Enter in the search box instead")
        page.locator("input[type='text'], input[type='search']").filter(visible=True).first.press("Enter")
        page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)
    print("Search results URL:", page.url)

    if not body:
        rel = page.evaluate("() => { const e = document.querySelector('[data-json-url]'); "
                            "return e ? e.getAttribute('data-json-url') : ''; }")
        if rel:
            try:
                body = page.request.get(urljoin(BASE, rel), timeout=60000).text()
                print("Fetched search JSON directly:", urljoin(BASE, rel))
            except Exception as e:
                print("Could not fetch search JSON:", e.__class__.__name__)
    if body:
        with open(SEARCH_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"Saved {SEARCH_JSON_PATH} ({len(body):,} characters)")
    else:
        print("Search JSON not captured - totals for (i)/(ii) will be missing")
    return home_links


def collect_all_company_links(page, home_links):
    all_links, page_no = [], 1
    seen = set()
    while page_no <= MAX_PAGES:
        scroll_until_stable(page)
        found = company_links_on_page(page)
        new = [l for l in sorted(found) if l not in seen]
        seen.update(new)
        all_links.extend(new)
        print(f"Results page {page_no}: {len(found)} company links, {len(new)} new, total {len(all_links)}")
        if MAX_COMPANIES and len(all_links) >= MAX_COMPANIES:
            break
        if not new and page_no > 1:
            print("No new companies on this page - stopping")
            break
        if not go_next(page):
            print("No next-page control found - reached the last page")
            break
        page_no += 1
    if MAX_COMPANIES:
        all_links = all_links[:MAX_COMPANIES]
    return all_links


def scrape_companies(page, links):
    rows = []
    for i, url in enumerate(links, start=1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            rec = page.evaluate(COMPANY_JS)
        except Exception as e:
            print(f"  [{i}] FAILED {url}: {e.__class__.__name__}")
            rec = {k: "" for k in REQUIRED}
            rec["Email CF Hex"] = ""
        rec["Profile URL"] = url
        rows.append(rec)
        if i % 10 == 0 or i == len(links):
            print(f"  scraped {i}/{len(links)} companies")
        time.sleep(DELAY_S)
    return rows


def clean(df):
    df = df.copy().fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()


    bad_email = ~df["Email"].str.contains("@", na=False) | df["Email"].str.contains("protected", case=False)
    df.loc[bad_email, "Email"] = df.loc[bad_email, "Email CF Hex"].apply(decode_cf_email)
    df = df.drop(columns=["Email CF Hex"])


    before = len(df)
    df = df.drop_duplicates(subset=["Profile URL"]).drop_duplicates(subset=["Company Name", "Address"])
    print(f"Removed {before - len(df)} duplicate companies")


    no_name = df["Company Name"] == ""
    if no_name.any():
        print(f"Dropped {no_name.sum()} rows with no company name (page failed to load)")
    df = df[~no_name]


    for col in REQUIRED:
        missing = df[col] == ""
        if missing.any():
            print(f"  {col}: {missing.sum()} not listed on the site -> 'Not Available'")
        df.loc[missing, col] = "Not Available"

    return df[REQUIRED + ["Profile URL"]].reset_index(drop=True)


def load_search_json():
    if not os.path.exists(SEARCH_JSON_PATH):
        return None
    text = open(SEARCH_JSON_PATH, encoding="utf-8").read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None


def facet_label(name):
    return html.unescape(name.replace("&nbsp;", "")).strip()


def facet_level(name):
    return name.count("&nbsp;") // 3


def facet_positions(facets, group, top_name=None):
    filters = facets.get(group, [])
    if top_name is None:
        return [f["value"] for f in filters]
    out, base = [], None
    for f in filters:
        if base is None:
            if facet_label(f["name"]).lower() == top_name.lower():
                base = facet_level(f["name"])
                out.append(f["value"])
        elif facet_level(f["name"]) > base:
            out.append(f["value"])
        else:
            break
    return out


def json_counts():
    data = load_search_json()
    if not data or "facets" not in data or "results" not in data:
        return None
    facets = {facet_label(g["title"]): g["filters"] for g in data["facets"]}
    results = data["results"]

    def count(positions, kind):
        return sum(1 for r in results if r.get("type") == kind
                   and any(p < len(r["filterVal"]) and r["filterVal"][p] == "1" for p in positions))

    return {
        "results": len(results),
        "products": sum(1 for r in results if r.get("type") == "product"),
        "companies": sum(1 for r in results if r.get("type") == "company"),
        "ingredients": count(facet_positions(facets, "Ingredients"), "product"),
        "finished": count(facet_positions(facets, "Finished Products"), "product"),
        "herbs": count(facet_positions(facets, "Ingredients", "Herbs, Spices"), "company"),
        "physical": count(facet_positions(facets, "Delivery Formats", "Physical Formats"), "company"),
        "cognitive": count(facet_positions(facets, "Health & Wellness", "Cognitive & Mental Health"), "company"),
    }


def answer_questions(df):
    cats = df["Categories"].str.lower()
    herbs = int(cats.str.contains(r"\bherbs\b|\bspices\b", regex=True).sum())
    physical = int(cats.apply(lambda c: any(f.lower() in c.split(" | ") for f in PHYSICAL_FORMATS)).sum())
    cognitive = int(cats.str.contains("cognitive & mental health", regex=False).sum())
    j = json_counts()

    print("\n" + "=" * 60)
    print("ANSWERS")
    print("=" * 60)
    if j:
        print(f"(i)   Total ingredients (products in Ingredients categories)      : {j['ingredients']}")
        print(f"(ii)  Total finished products (products in Finished Products cats): {j['finished']}")
    else:
        print(f"(i)/(ii) {SEARCH_JSON_PATH} missing - run the full scrape to capture it")
    print(f"(iii) Companies with Herbs & Spices            : {herbs}")
    print(f"(iv)  Companies with Physical delivery formats : {physical}")
    print(f"(v)   Companies in Cognitive & Mental Health   : {cognitive}")
    print(f"      Total companies in results.csv           : {len(df)}")
    if j:
        print("\nCross-check against the site's own filter data:")
        print(f"      Search results: {j['results']} ({j['products']} products, {j['companies']} suppliers)")
        for label, mine, site in [("Suppliers", len(df), j["companies"]), ("Herbs & Spices", herbs, j["herbs"]),
                                  ("Physical formats", physical, j["physical"]),
                                  ("Cognitive & Mental Health", cognitive, j["cognitive"])]:
            print(f"      {label:27} scraped={mine:<5} site={site:<5} {'MATCH' if mine == site else 'DIFFERENT'}")
    print("=" * 60)


def main():
    if "--clean-only" in sys.argv:
        raw = pd.read_csv(RAW_PATH, dtype=str, encoding="utf-8-sig").fillna("")
        print(f"Loaded {len(raw)} raw rows from {RAW_PATH}")
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.route("**/*", block_heavy_resources)
            home_links = run_search(page)
            links = collect_all_company_links(page, home_links)
            print(f"Collected {len(links)} company profile links")
            rows = scrape_companies(page, links)
            browser.close()
        raw = pd.DataFrame(rows)
        raw.to_csv(RAW_PATH, index=False, encoding="utf-8-sig")
        print(f"Saved raw data -> {RAW_PATH}")

    df = clean(raw)
    df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"Saved cleaned data -> {OUT_PATH} ({len(df)} rows)")
    answer_questions(df)


if __name__ == "__main__":
    main()