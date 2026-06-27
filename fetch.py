"""
fetch.py — SIS PESRP Scraper v3
Key findings from v2:
  - Page has 0 <select> elements (custom div dropdowns)
  - Data shown via Highcharts (JS chart library)
  - AJAX endpoints are in inline <script> blocks in the HTML
  - Page-specific JS is NOT in external files

New strategy:
  1. Fetch raw HTML with requests → extract ALL inline <script> blocks
  2. Find AJAX URLs inside those scripts
  3. Find Highcharts series data embedded in the page
  4. Use Playwright to find custom div dropdowns and click them
  5. Intercept ALL network responses (not just JSON content-type)
"""

import json, csv, re, time, requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE    = "https://sis.pesrp.edu.pk"
URL     = f"{BASE}/str/analysis"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         BASE,
})

# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(t): return re.sub(r"\s+", " ", (t or "")).strip()
def num(v):
    try: return int(re.sub(r"[^\d]", "", str(v or 0)) or 0)
    except: return 0

# ── Step 1: Fetch raw HTML and extract inline scripts ─────────────────────────
def get_inline_scripts(url):
    print(f"Fetching {url} with requests ...")
    try:
        r = SESSION.get(url, timeout=30)
        html = r.text
        print(f"  Got {len(html)} chars, status {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")
        return "", []

    soup = BeautifulSoup(html, "html.parser")

    scripts = []
    for tag in soup.find_all("script", src=False):
        content = tag.string or tag.get_text() or ""
        if content.strip():
            scripts.append(content)

    print(f"  Found {len(scripts)} inline script blocks")
    for i, s in enumerate(scripts):
        print(f"  Script[{i}]: {len(s)} chars | preview: {s[:120].strip()!r}")

    return html, scripts

# ── Step 2: Extract AJAX URLs from inline scripts ─────────────────────────────
def extract_ajax_urls(scripts):
    urls = set()
    combined = "\n".join(scripts)

    patterns = [
        r"""url\s*:\s*['"`]([^'"`\s]+)['"`]""",
        r"""\$\.(post|get|ajax|getJSON)\s*\(\s*['"`]([^'"`\s]+)['"`]""",
        r"""fetch\s*\(\s*['"`]([^'"`\s]+)['"`]""",
        r"""(?:base_url|site_url)\s*\+?\s*\(?\s*['"`]([^'"`]+)['"`]""",
        r"""action\s*[:=]\s*['"`](/[^'"`\s]+)['"`]""",
        r"""['"`](/(?:str|stats|api|school|district|tehsil|enroll|get|fetch|load|data)[^'"`\s]{2,50})['"`]""",
    ]

    for pat in patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            ep = m.group(m.lastindex or 1).strip()
            if ep and not ep.startswith("//") and len(ep) > 3:
                if ep.startswith("/"):
                    ep = BASE + ep
                if ep.startswith("http") and "pesrp" in ep:
                    urls.add(ep)

    print(f"\nAJAX URLs found in inline scripts: {len(urls)}")
    for u in sorted(urls):
        print(f"  {u}")

    return list(urls)

# ── Step 3: Extract Highcharts data from page ─────────────────────────────────
def extract_highcharts_data(scripts, html):
    combined = "\n".join(scripts) + html
    rows = []

    cats_matches = re.findall(
        r"""categories\s*:\s*\[([^\]]{10,})\]""", combined, re.DOTALL
    )
    series_matches = re.findall(
        r"""series\s*:\s*\[(.{20,5000}?)\](?:\s*[,}])""", combined, re.DOTALL
    )

    categories = []
    for m in cats_matches:
        items = re.findall(r"""['"`]([^'"`]+)['"`]""", m)
        if items:
            categories = items
            print(f"  Highcharts categories: {len(categories)} items")
            break

    all_series = []
    for m in series_matches:
        name_m = re.search(r"""name\s*:\s*['"`]([^'"`]+)['"`]""", m)
        data_m = re.search(r"""data\s*:\s*\[([^\]]+)\]""", m)
        if name_m and data_m:
            name  = name_m.group(1)
            nums  = [num(x.strip()) for x in data_m.group(1).split(",") if x.strip()]
            all_series.append({"name": name, "data": nums})
            print(f"  Series '{name}': {len(nums)} values")

    if categories and all_series:
        boys_data    = next((s["data"] for s in all_series if "boy"   in s["name"].lower()), [])
        girls_data   = next((s["data"] for s in all_series if "girl"  in s["name"].lower()), [])
        total_data   = next((s["data"] for s in all_series if "total" in s["name"].lower()
                             or "enrol" in s["name"].lower()), [])
        teacher_data = next((s["data"] for s in all_series if "teach" in s["name"].lower()), [])

        for i, cat in enumerate(categories):
            b = boys_data[i]    if i < len(boys_data)    else 0
            g = girls_data[i]   if i < len(girls_data)   else 0
            t = total_data[i]   if i < len(total_data)   else b + g
            rows.append({
                "school_id": "", "school_name": cat,
                "district": "", "tehsil": "", "markaz": "",
                "total_students": t, "boys": b, "girls": g,
                "teachers": teacher_data[i] if i < len(teacher_data) else 0,
            })
        print(f"  Built {len(rows)} rows from Highcharts data")

    return rows

# ── Step 4: Probe endpoints ───────────────────────────────────────────────────
GUESSED = [
    "/str/get_districts", "/str/get_tehsils", "/str/get_markazs",
    "/str/get_schools",   "/str/get_school_data", "/str/get_enrollment",
    "/str/school_data",   "/str/analysis/data",   "/str/analysis/get",
    "/str/stats",         "/str/chart_data",      "/str/get_chart_data",
    "/str/get_data",      "/str/schools",
    "/stats/get_district","/stats/schools",       "/stats/enrollment",
    "/api/schools",       "/api/districts",       "/api/enrollment",
    "/home/get_stats",    "/home/stats",
    "/str/get_schools?district_id=1",
    "/str/get_schools?district=1",
]

def probe(urls):
    found_data = []
    found_districts = []
    all_urls = list(set(urls + [BASE + g for g in GUESSED]))
    print(f"\nProbing {len(all_urls)} endpoints ...")

    for ep in all_urls:
        for method, data in [
            ("GET",  None),
            ("POST", {"district_id": "", "district": "", "type": "school"}),
            ("POST", {"district_id": "1", "district": "1"}),
        ]:
            try:
                hdrs = {**SESSION.headers,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*"}
                if method == "GET":
                    r = SESSION.get(ep, headers=hdrs, timeout=10)
                else:
                    r = SESSION.post(ep, data=data, headers=hdrs, timeout=10)

                if r.status_code != 200:
                    continue
                body = r.text.strip()
                if not body or body[0] not in ("[", "{"):
                    continue

                parsed = r.json()
                rows   = parse_school_json(parsed)
                if rows:
                    print(f"  OK {method} {ep} -> {len(rows)} school rows!")
                    found_data.extend(rows)
                    break
                elif isinstance(parsed, list) and parsed:
                    first = parsed[0]
                    if isinstance(first, dict):
                        ks = {k.lower() for k in first}
                        if any(k in ks for k in ["district_id","district_name","dist_id"]):
                            print(f"  ~ {method} {ep} -> district list ({len(parsed)})")
                            found_districts = parsed
                        elif any(k in ks for k in ["tehsil_id","tehsil_name"]):
                            print(f"  ~ {method} {ep} -> tehsil list ({len(parsed)})")
                        else:
                            print(f"  ? {method} {ep} -> list of {len(parsed)}, keys={list(first.keys())[:5]}")
                    break
                elif isinstance(parsed, dict):
                    print(f"  ? {method} {ep} -> dict, keys={list(parsed.keys())[:6]}")
                    break

            except Exception:
                pass

    return found_data, found_districts

# ── Step 5: Playwright deep scan ──────────────────────────────────────────────
captured = []

def capture_all(resp):
    try:
        body = resp.text()
        if body and len(body) > 20 and body.strip()[0] in ("[", "{"):
            captured.append({"url": resp.url, "status": resp.status, "body": body[:30000]})
            print(f"  [NET] {resp.status} {resp.url[:90]}")
    except Exception:
        pass

def playwright_deep(ts):
    print("\n--- Playwright deep scan ---")
    rows = []

    with sync_playwright() as pw:
        br  = pw.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=SESSION.headers["User-Agent"])
        pg  = ctx.new_page()
        pg.on("response", capture_all)

        pg.goto(URL, wait_until="networkidle", timeout=90000)
        time.sleep(6)

        clickables = pg.evaluate("""() => {
            const results = [];
            const selectors = [
                '[data-value]','[data-id]','[data-district]',
                '.dropdown-item','[role=option]','[role=listbox]',
                '.select2-selection','[class*=dropdown]',
                '[class*=district]','[class*=tehsil]','[class*=school]',
                'li[onclick]','div[onclick]','span[onclick]','a[data-value]',
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    results.push({
                        tag: el.tagName,
                        cls: el.className,
                        text: el.innerText ? el.innerText.slice(0,50) : '',
                        val: el.getAttribute('data-value') || el.getAttribute('data-id') || '',
                        onclick: el.getAttribute('onclick') ? el.getAttribute('onclick').slice(0,80) : '',
                    });
                });
            });
            return results.slice(0, 50);
        }""")

        print(f"  Custom clickable elements: {len(clickables)}")
        for el in clickables[:20]:
            print(f"    <{el['tag']} cls='{el['cls'][:40]}' val='{el['val']}' "
                  f"text='{el['text']}' onclick='{el['onclick']}'")

        js_vars = pg.evaluate("""() => {
            const found = {};
            for (const key of Object.keys(window)) {
                try {
                    const val = window[key];
                    if (Array.isArray(val) && val.length > 0 && typeof val[0] === 'object') {
                        found[key] = val.slice(0, 3);
                    }
                } catch(e) {}
            }
            return found;
        }""")

        print(f"  JS window arrays: {list(js_vars.keys())[:20]}")
        for k, v in list(js_vars.items())[:10]:
            print(f"    window.{k} = {str(v)[:120]}")

        for el in clickables[:10]:
            try:
                if el['val']:
                    pg.click(f"[data-value='{el['val']}']", timeout=2000)
                    time.sleep(2)
                    print(f"  Clicked data-value={el['val']}")
            except Exception:
                pass

        html = pg.content()
        br.close()

    for entry in captured:
        try:
            parsed = json.loads(entry["body"])
            r = parse_school_json(parsed)
            if r:
                print(f"  Captured {len(r)} school rows from {entry['url'][:70]}")
                rows.extend(r)
        except Exception:
            pass

    return rows, html

# ── Parse school JSON ─────────────────────────────────────────────────────────
NAME_K  = ["school_name","name","school","sch_name","school_title","sname","s_name"]
TOT_K   = ["total","total_students","enrollment","students","enrolled","total_enrol","tot","enrol"]
BOYS_K  = ["boys","male","male_enrollment","boy_count","male_count","boys_enrol","male_enrol"]
GIRLS_K = ["girls","female","female_enrollment","girl_count","female_count","girls_enrol","female_enrol"]
TCH_K   = ["teachers","teacher_count","allocated_teachers","staff","tch_count","tch"]
DIST_K  = ["district","district_name","dist_name","dname","dist"]
TEH_K   = ["tehsil","tehsil_name","teh_name","tname","teh"]
MRK_K   = ["markaz","markaz_name","mrk_name","mrk"]
ID_K    = ["school_id","id","emis","emis_code","school_code","scode","s_id"]

def gf(row, keys):
    rl = {k.lower(): v for k, v in row.items()}
    for k in keys: 
        if k in rl: return rl[k]
    return ""

def parse_school_json(data, dist="", teh=""):
    rows = []
    if isinstance(data, dict):
        for key in ("data","result","schools","rows","records","items","list","response"):
            if key in data and isinstance(data[key], list):
                data = data[key]; break
        else:
            return rows
    if not isinstance(data, list): return rows

    for item in data:
        if not isinstance(item, dict): continue
        ks = {k.lower() for k in item}
        has_school = any(k in ks for k in ["school_name","school","emis","sch_name","sname"])
        has_data   = any(k in ks for k in ["enrollment","students","boys","girls","total","enrol"])
        if not (has_school or has_data): continue
        b = num(gf(item, BOYS_K))
        g = num(gf(item, GIRLS_K))
        t = num(gf(item, TOT_K)) or b + g
        rows.append({
            "school_id":      str(gf(item, ID_K) or ""),
            "school_name":    clean(gf(item, NAME_K)) or "Unknown",
            "district":       clean(gf(item, DIST_K)) or dist,
            "tehsil":         clean(gf(item, TEH_K))  or teh,
            "markaz":         clean(gf(item, MRK_K)),
            "total_students": t, "boys": b, "girls": g,
            "teachers":       num(gf(item, TCH_K)),
        })
    return rows

# ── Save ──────────────────────────────────────────────────────────────────────
FIELDS = ["school_id","school_name","district","tehsil","markaz",
          "total_students","boys","girls","teachers","scraped_at"]

def save(rows, ts):
    for r in rows: r["scraped_at"] = ts
    with open("schools.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tot = sum(r.get("total_students",0) for r in rows)
    out = {
        "scraped_at": ts, "source": BASE,
        "summary": {
            "total_schools":  len(rows),
            "total_students": tot,
            "total_boys":     sum(r.get("boys",0)     for r in rows),
            "total_girls":    sum(r.get("girls",0)    for r in rows),
            "total_teachers": sum(r.get("teachers",0) for r in rows),
        },
        "schools": rows,
        "captured_endpoints": [{"url":e["url"],"status":e["status"]} for e in captured],
    }
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nschools.csv  -- {len(rows)} rows")
    print(f"data.json    -- {len(rows)} schools | {tot:,} students")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ts = datetime.now(timezone.utc).isoformat()
    print("="*55)
    print("  SIS PESRP Scraper v3")
    print("="*55)
    all_rows = []

    html, scripts = get_inline_scripts(URL)
    ajax_urls = extract_ajax_urls(scripts)

    print("\n--- Highcharts data scan ---")
    all_rows.extend(extract_highcharts_data(scripts, html))

    found_rows, _ = probe(ajax_urls)
    all_rows.extend(found_rows)

    if not all_rows:
        pw_rows, pw_html = playwright_deep(ts)
        all_rows.extend(pw_rows)
        if not all_rows:
            print("\n--- Highcharts scan on rendered HTML ---")
            all_rows.extend(extract_highcharts_data([], pw_html))

    seen, unique = set(), []
    for r in all_rows:
        k = (r.get("school_name",""), r.get("district",""))
        if k not in seen:
            seen.add(k); unique.append(r)

    print(f"\nTotal unique rows: {len(unique)}")
    save(unique, ts)

    if not unique:
        print("\nStill 0 rows. Check log above for:")
        print("  Script[N] previews -- shows inline JS content")
        print("  Custom clickable elements -- shows dropdown structure")
        print("  JS window arrays -- shows if data is in window variables")