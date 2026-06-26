"""
fetch.py  —  SIS PESRP Dashboard Scraper
Scrapes publicly accessible pages on https://sis.pesrp.edu.pk
using a headless Chromium browser (Playwright) so AJAX data is captured.
Writes clean data.json for the GitHub Pages front-end.
"""

import json, re, time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://sis.pesrp.edu.pk"

# ── helpers ───────────────────────────────────────────────────────────────────

def safe_text(page, selector, default="N/A"):
    try:
        el = page.query_selector(selector)
        return el.inner_text().strip() if el else default
    except Exception:
        return default

def safe_all(page, selector):
    try:
        return [el.inner_text().strip() for el in page.query_selector_all(selector)]
    except Exception:
        return []

def clean(text):
    return re.sub(r'\s+', ' ', text).strip()

# ── scraper ───────────────────────────────────────────────────────────────────

def scrape():
    result = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE_URL,
        "etransfer": {},
        "site_info": {},
        "stats": {},
        "str_analysis": {},
        "notices": [],
        "error": None,
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124 Safari/537.36"
        )

        # ── PAGE 1: Home page ────────────────────────────────────────────────
        page = ctx.new_page()
        try:
            page.goto(BASE_URL, wait_until="networkidle", timeout=60_000)
            time.sleep(3)   # let any late AJAX settle

            # Last updated timestamp shown on home page
            last_updated_raw = safe_text(page, "text=/Last Updated/", "")
            if not last_updated_raw:
                # try scanning all visible text
                full_text = page.inner_text("body")
                m = re.search(r'Last Updated.*?(\d{1,2}\s+\w+\s+\d{4}.*?)(?:\n|$)',
                              full_text, re.IGNORECASE)
                last_updated_raw = m.group(0).strip() if m else "Not found"

            result["site_info"]["last_updated_raw"] = last_updated_raw

            # E-Transfer status — parse every notice paragraph
            body_text = page.inner_text("body")

            etransfer_open   = "applications are being accepted" in body_text.lower() \
                               and "not being accepted" not in body_text.lower()
            result["etransfer"]["accepting"] = etransfer_open
            result["etransfer"]["status_label"] = (
                "OPEN" if etransfer_open else "CLOSED"
            )

            # Extract last round dates (e.g. "from 11-Jun-25 to 15-Jun-25")
            round_matches = re.findall(
                r'from\s+(\d{1,2}-\w{3}-\d{2,4})\s+to\s+(\d{1,2}-\w{3}-\d{2,4})',
                body_text, re.IGNORECASE
            )
            if round_matches:
                last = round_matches[-1]
                result["etransfer"]["last_round_start"] = last[0]
                result["etransfer"]["last_round_end"]   = last[1]

            # Collect all notice/alert text blocks
            for sel in ["p", ".alert", ".notice", "div.info"]:
                for el in page.query_selector_all(sel):
                    t = clean(el.inner_text())
                    if len(t) > 30 and t not in result["notices"]:
                        result["notices"].append(t)

            # Grab any headline numbers (cards / stat boxes)
            stat_numbers = {}
            for card in page.query_selector_all(
                    ".stat-box, .info-box, .card, .counter, [class*='count']"):
                label = clean(card.inner_text())
                num_m = re.search(r'[\d,]+', label)
                if num_m and len(label) < 80:
                    key = re.sub(r'\d[\d,]*', '', label).strip()
                    stat_numbers[key] = num_m.group(0)

            result["stats"]["home_cards"] = stat_numbers

        except PWTimeout:
            result["error"] = "Timeout on home page"
        except Exception as e:
            result["error"] = f"Home page error: {e}"

        # ── PAGE 2: /str/analysis (public enrollment/teacher stats) ──────────
        page2 = ctx.new_page()
        try:
            page2.goto(f"{BASE_URL}/str/analysis",
                       wait_until="networkidle", timeout=60_000)
            time.sleep(4)

            body2 = page2.inner_text("body")

            # Description text
            desc_el = page2.query_selector("p, .description, .intro")
            result["str_analysis"]["description"] = (
                clean(desc_el.inner_text()) if desc_el else
                "Free public access to high-level stats tabulated from "
                "self-reported data by public schools in Punjab."
            )

            # Tables — capture any data tables
            tables = []
            for tbl in page2.query_selector_all("table"):
                rows = []
                headers = [clean(th.inner_text())
                           for th in tbl.query_selector_all("th")]
                for tr in tbl.query_selector_all("tbody tr"):
                    cells = [clean(td.inner_text())
                             for td in tr.query_selector_all("td")]
                    if cells:
                        rows.append(cells)
                if rows:
                    tables.append({"headers": headers, "rows": rows[:30]})
            result["str_analysis"]["tables"] = tables

            # Extract any dropdown options (district list)
            districts = []
            for opt in page2.query_selector_all("select option"):
                val = opt.get_attribute("value") or ""
                txt = clean(opt.inner_text())
                if val and txt and txt.lower() not in ("select", "all", "--"):
                    districts.append({"id": val, "name": txt})
            result["str_analysis"]["districts"] = districts

            # Key numbers visible on the analysis page
            nums = {}
            for el in page2.query_selector_all(
                    ".number, .stat, .count, .total, [class*='figure']"):
                t = clean(el.inner_text())
                if re.search(r'\d{3,}', t) and len(t) < 60:
                    nums[t[:40]] = t
            result["str_analysis"]["visible_numbers"] = nums

        except PWTimeout:
            result["str_analysis"]["error"] = "Timeout on /str/analysis"
        except Exception as e:
            result["str_analysis"]["error"] = str(e)

        browser.close()

    # ── deduplicate notices ───────────────────────────────────────────────────
    seen, unique = set(), []
    for n in result["notices"]:
        k = n[:60]
        if k not in seen:
            seen.add(k)
            unique.append(n)
    result["notices"] = unique[:10]   # keep max 10

    return result


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Scraping sis.pesrp.edu.pk …")
    data = scrape()

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✓ Wrote data.json  (E-Transfer: {data['etransfer'].get('status_label','?')})")
    print(f"  Districts found : {len(data['str_analysis'].get('districts', []))}")
    print(f"  Tables found    : {len(data['str_analysis'].get('tables', []))}")
    print(f"  Notices         : {len(data['notices'])}")
    if data.get("error"):
        print(f"  ⚠ Error: {data['error']}")
