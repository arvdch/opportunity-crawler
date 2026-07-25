"""
scraper.py — Individual scrapers for each opportunity source.

Each scraper returns a list of dicts:
  - title       (str, required)
  - url         (str, required, used as dedup key)
  - description (str)
  - company     (str)
  - location    (str)
  - deadline    (str) — used for date posted on LinkedIn
  - source      (str)
  - tags        (str) — extra text blob for keyword matching

URL STATUS (verified from live runs on Aravind's machine):
  ✅ LinkedIn jobs     — 9-32 items
  ✅ Indeed India      — partial (some 403, some work)
  ✅ Google News/DDG   — 25 items
  ✅ HackerEarth       — 21 items
  ✅ GitHub API        — 8 items
  ✅ Reddit netsec RSS — works with flair filter
  ✅ Sarkari Result    — 170 items (NCS replacement)
  ✅ govt_portals      — 14 items (CDAC, MeitY, NCIIPC, AICTE)
  ❌ NCS .gov URLs     — all 404, removed
  ❌ Employment News   — 404, removed
  ❌ FreshersWorld     — 404, removed
  ❌ Indeed India      — 403 on most queries, kept with fallback
  ❌ MyGov internship  — 404, removed
  ❌ CERT-In           — returns empty (changed structure), replaced
  ❌ LinkedIn Posts    — 0 items (requires login), disabled
"""

import os
import re
import time
import urllib.parse
import feedparser
import httpx
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT = 20


def _get(url: str, **kwargs) -> httpx.Response | None:
    """Safe GET — catches all httpx errors, returns None on failure."""
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT,
                      follow_redirects=True, **kwargs)
        r.raise_for_status()
        return r
    except httpx.TimeoutException:
        print(f"[HTTP] Timeout: {url[:80]}")
    except httpx.ConnectError:
        print(f"[HTTP] Connection failed: {url[:80]}")
    except httpx.HTTPStatusError as e:
        print(f"[HTTP] {e.response.status_code}: {url[:80]}")
    except httpx.HTTPError as e:
        print(f"[HTTP] Error: {url[:80]} — {e}")
    return None


def _make_opp(title: str, url: str, source: str, **kwargs) -> dict | None:
    """Build opportunity dict. Returns None if title or url invalid."""
    title = title.strip()
    url   = url.strip()
    if not title or not url or not url.startswith("http"):
        return None
    return {"title": title, "url": url, "source": source,
            "description": "", "company": "", "location": "",
            "deadline": "", "tags": "", **kwargs}


class _noel:
    """Null-object — avoids repetitive None checks on missing soup elements."""
    def get_text(self, **_): return ""
    def get(self, *_, **__): return ""


# ─────────────────────────────────────────────
#  1. LinkedIn Jobs
#     Two query groups:
#     - Intern/fresher searches (f_E=1,2 = internship+entry-level)
#     - Full-time analyst/SOC searches (f_E=2 = entry-level only, no f_JT filter)
#       so junior/1-2yr analyst roles aren't excluded just for lacking
#       the word "intern" in the title.
#     Title still must contain a security keyword either way.
# ─────────────────────────────────────────────

_LINKEDIN_INTERN_SIGNALS = re.compile(
    r"\b(intern|internship|trainee|fresher|graduate\s+trainee|entry.level|junior)\b",
    re.IGNORECASE
)
_LINKEDIN_SECURITY_SIGNALS = re.compile(
    r"\b(cyber|security|pentest|penetration|soc|infosec|information\s+security|"
    r"network\s+security|ethical\s+hack|forensic|malware|vulnerability|red\s+team|"
    r"blue\s+team|incident\s+response|splunk|siem|appsec|devsecops|grc|"
    r"risk|compliance|threat|cloud\s+security|analyst)\b",
    re.IGNORECASE
)
# Senior-level titles to exclude even from the full-time analyst search —
# the explicit experience-wall regex in filter.py also catches these later,
# but filtering here saves a wasted AI call.
_LINKEDIN_SENIOR_SIGNALS = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head\s+of|director|manager|"
    r"\d{1,2}\+?\s*years?)\b",
    re.IGNORECASE
)

def scrape_linkedin(config: dict) -> list[dict]:
    results  = []
    seen_urls: set[str] = set()

    # Group A: intern/fresher specific searches — f_JT=I restricts job type to internship
    intern_searches = [
        ("cybersecurity intern", "India"),
        ("information security intern", "India"),
        ("penetration testing intern", "India"),
        ("SOC analyst intern", "India"),
        ("security analyst intern", "India"),
        ("cyber security fresher", "India"),
        ("network security intern", "India"),
    ]
    # Group B: full-time entry-level roles — no f_JT filter, no "intern" required
    # in title, since plenty of legitimate 0-2yr SOC/analyst roles don't say
    # "intern" or "junior" anywhere in the title.
    fulltime_searches = [
        ("SOC analyst", "India"),
        ("security analyst", "India"),
        ("junior security analyst", "India"),
        ("cybersecurity analyst", "India"),
    ]

    pages = [0, 25]  # LinkedIn guest search paginates via &start=

    def _run_search(kw, loc, job_type_filter, require_intern_signal):
        jt_param = "&f_JT=I" if job_type_filter else ""
        for start in pages:
            url = (f"https://www.linkedin.com/jobs/search/?keywords={urllib.parse.quote(kw)}"
                   f"&location={urllib.parse.quote(loc)}&f_E=1%2C2{jt_param}&sortBy=DD&start={start}")
            resp = _get(url)
            if not resp:
                time.sleep(2)
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select(".job-search-card, .base-card, .jobs-search__results-list li")
            if not cards and start > 0:
                break
            for card in cards:
                try:
                    title_el = card.select_one("h3.base-search-card__title, .job-search-card__title")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)

                    if require_intern_signal and not _LINKEDIN_INTERN_SIGNALS.search(title):
                        continue
                    if not _LINKEDIN_SECURITY_SIGNALS.search(title):
                        continue
                    # Block obviously senior titles even in the full-time group —
                    # cheaper to filter here than waste an AI call later
                    if _LINKEDIN_SENIOR_SIGNALS.search(title):
                        continue

                    link_el = card.select_one("a.base-card__full-link, a")
                    href    = (link_el.get("href", "") if link_el else "").split("?")[0]
                    if not href or href in seen_urls:
                        continue
                    seen_urls.add(href)

                    date_el = card.select_one("time")
                    posted  = date_el.get("datetime", "") if date_el else ""

                    opp = _make_opp(
                        title, href, "linkedin",
                        company =(card.select_one(".base-search-card__subtitle a,.job-search-card__subtitle") or _noel()).get_text(strip=True),
                        location=(card.select_one(".job-search-card__location") or _noel()).get_text(strip=True) or loc,
                        deadline=posted,
                        tags=f"internship {kw}" if require_intern_signal else f"fulltime entrylevel {kw}")
                    if opp: results.append(opp)
                except Exception:
                    continue
            time.sleep(2)

    for kw, loc in intern_searches:
        _run_search(kw, loc, job_type_filter=True, require_intern_signal=True)
    for kw, loc in fulltime_searches:
        _run_search(kw, loc, job_type_filter=False, require_intern_signal=False)

    return results


# ─────────────────────────────────────────────
#  2. Indeed India + TimesJobs India
#     Indeed 403s on most queries — TimesJobs is less aggressive
#     about bot detection and adds coverage when Indeed blocks.
# ─────────────────────────────────────────────

def scrape_wellfound(config: dict) -> list[dict]:
    """
    Scrapes Indeed India + TimesJobs India for cybersecurity internships.
    Source key kept as 'wellfound' so config doesn't need changing.
    """
    results  = []
    seen_urls: set[str] = set()

    # ── 2a. Indeed India (works intermittently — keep trying) ──
    indeed_queries = [
        "cybersecurity+internship",
        "cyber+security+internship",
        "security+analyst+intern",
        "ethical+hacking+intern",
    ]
    for query in indeed_queries:
        url  = f"https://in.indeed.com/jobs?q={query}&l=India&fromage=30"
        resp = _get(url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for card in soup.select(".job_seen_beacon, .tapItem, [class*='jobCard'], .result"):
            try:
                title_el = card.select_one("h2.jobTitle a, .jobTitle a, h2 a, [class*='title'] a")
                if not title_el:
                    continue
                title    = title_el.get_text(strip=True)
                href     = title_el.get("href", "")
                full_url = f"https://in.indeed.com{href}" if href.startswith("/") else href
                if not full_url or full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                company_el  = card.select_one("[data-testid='company-name'], .companyName")
                location_el = card.select_one("[data-testid='text-location'], .companyLocation")
                date_el     = card.select_one("[data-testid='myJobsStateDate'], .date")

                opp = _make_opp(
                    title, full_url, "wellfound",
                    company =(company_el.get_text(strip=True) if company_el else ""),
                    location=(location_el.get_text(strip=True) if location_el else "India"),
                    deadline=(date_el.get_text(strip=True) if date_el else ""),
                    tags="internship india cybersecurity indeed")
                if opp: results.append(opp)
            except Exception:
                continue
        time.sleep(2)

    # ── 2b. TimesJobs India (fallback — less aggressive bot blocking) ──
    timesjobs_queries = [
        "Cyber+Security+Intern",
        "Information+Security+Intern",
        "Penetration+Testing+Intern",
        "SOC+Analyst+Intern",
    ]
    for query in timesjobs_queries:
        url  = (f"https://www.timesjobs.com/candidate/job-search.html"
                f"?searchType=personalizedSearch&from=submit&txtKeywords={query}"
                f"&txtLocation=India")
        resp = _get(url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for card in soup.select("li.clearfix.job-bx, .job-bx"):
            try:
                title_el = card.select_one("h2 a, .joblist-comp-name + a, a.heading")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href  = title_el.get("href", "")
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                company_el  = card.select_one(".joblist-comp-name, .comp-name")
                location_el = card.select_one(".srp-zindex span, .top-jd-dtl span")

                opp = _make_opp(
                    title, href, "wellfound",
                    company =(company_el.get_text(strip=True) if company_el else ""),
                    location=(location_el.get_text(strip=True) if location_el else "India"),
                    tags="internship india cybersecurity timesjobs")
                if opp: results.append(opp)
            except Exception:
                continue
        time.sleep(1.5)

    return results


# ─────────────────────────────────────────────
#  3. News & DuckDuckGo search + DevPost
#     Google News RSS → 403 from cloud IPs, DDG works
# ─────────────────────────────────────────────

def scrape_google_news(config: dict) -> list[dict]:
    results = []

    queries = config.get("queries", [
        "cybersecurity internship India 2026",
        "APCSIP 2026 cybersecurity internship",
        "India government cybersecurity internship program 2026",
        "NCIIPC internship 2026",
        "MeitY cybersecurity program 2026",
        "free cybersecurity certification India 2026",
        "GSoC 2026 cybersecurity",
        "CDAC internship 2026 cybersecurity",
    ])

    for query in queries:
        url  = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        resp = _get(url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for result in soup.select(".result, .web-result")[:5]:
            try:
                title_el   = result.select_one(".result__title a, .result__a")
                snippet_el = result.select_one(".result__snippet")
                if not title_el:
                    continue
                href = title_el.get("href", "")
                if "uddg=" in href:
                    href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
                opp = _make_opp(
                    title_el.get_text(strip=True), href, "google_news",
                    description=(snippet_el.get_text(strip=True) if snippet_el else "")[:300],
                    company="Web Search", tags=query)
                if opp: results.append(opp)
            except Exception:
                continue
        time.sleep(1.5)

    # DevPost hackathons (free JSON API, works from cloud)
    try:
        resp = _get("https://devpost.com/api/hackathons?status=upcoming&order_by=deadline&per_page=15")
        if resp:
            for hack in resp.json().get("hackathons", []):
                opp = _make_opp(
                    hack.get("title",""), hack.get("url","https://devpost.com"),
                    "google_news",
                    description=hack.get("tagline","")[:200],
                    company="DevPost",
                    deadline=hack.get("submission_period_dates",""),
                    tags="hackathon competition programming")
                if opp: results.append(opp)
    except Exception as e:
        print(f"[DevPost] Error: {e}")

    return results


# ─────────────────────────────────────────────
#  4. NCS replacement — Sarkari Result + govt job aggregators
#     Original NCS portal URLs all 404. Sarkari Result works (170 items).
# ─────────────────────────────────────────────

def scrape_ncs(config: dict) -> list[dict]:
    results   = []
    seen_urls: set[str] = set()
    # Tightened: require "cyber" or "security" explicitly, not generic "it "/"digital"
    # which matched almost every govt notice and got dropped later in keyword filter anyway
    required  = {"cyber", "security"}
    bonus     = {"intern", "trainee", "information technology", "network"}

    sources = [
        ("https://www.sarkariresult.com/latestjob/",                  "https://www.sarkariresult.com"),
        ("https://sarkarialert.in/category/internship/",              "https://sarkarialert.in"),
        ("https://www.freejobalert.com/government-jobs/it-computer/", "https://www.freejobalert.com"),
    ]

    for url, base in sources:
        resp = _get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or not 8 <= len(text) <= 200:
                continue
            text_lower = text.lower()
            # Must match at least one required term — cuts noise dramatically
            if not any(t in text_lower for t in required):
                continue
            href     = a.get("href","")
            full_url = href if href.startswith("http") else f"{base}{href}" if href.startswith("/") else url
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            opp = _make_opp(text[:150], full_url, "ncs",
                tags="government job internship india cyber security")
            if opp: results.append(opp)
        time.sleep(1)
    return results


# ─────────────────────────────────────────────
#  5. CERT-In replacement
#     Old scraper returned 0. Now scrapes CERT-In news/advisories page
#     + MHA Cyber Crime portal for internship announcements.
# ─────────────────────────────────────────────

def scrape_certin(config: dict) -> list[dict]:
    results  = []
    seen_urls: set[str] = set()
    relevant = {"intern","training","program","workshop","certification","cyber","security","fellow","scheme"}

    sources = [
        ("https://www.cert-in.org.in",                              "CERT-In"),
        ("https://www.cert-in.org.in/s2cMainServlet?pageid=PUBPRGMLIST", "CERT-In"),
        ("https://cybercrime.gov.in",                               "MHA CyberCrime"),
        ("https://www.dsci.in/page/internship",                     "DSCI"),   # Data Security Council of India
    ]

    for url, name in sources:
        resp = _get(url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.select("a[href]"):
            text = a.get_text(strip=True)
            if not text or not 10 <= len(text) <= 200:
                continue
            if not any(kw in text.lower() for kw in relevant):
                continue
            href = a.get("href","")
            base = "/".join(url.split("/")[:3])
            full_url = href if href.startswith("http") else f"{base}{href}" if href.startswith("/") else url
            # For DSCI, skip site navigation/content pages — only keep
            # actual internship, job, fellowship, or program listing URLs
            if "dsci.in" in full_url:
                path = full_url.lower()
                keep_paths = ("internship", "job", "career", "fellowship", "hiring",
                              "recruit", "opportunity", "placement", "trainee", "apprentice")
                if not any(p in path for p in keep_paths):
                    continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            opp = _make_opp(text[:150], full_url, "certifications",
                company=name, tags="cybersecurity government india cert-in")
            if opp: results.append(opp)
        time.sleep(1)
    return results


# ─────────────────────────────────────────────
#  7. HackerEarth — hackathons and challenges
# ─────────────────────────────────────────────

def scrape_hackerearth(config: dict) -> list[dict]:
    results = []
    for url in ["https://www.hackerearth.com/challenges/hackathon/",
                "https://www.hackerearth.com/challenges/competitive/"]:
        resp = _get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for card in soup.select(".challenge-card,.challenge-card-modern,.hackathon-card"):
            try:
                title_el = card.select_one("h3,h4,.title,.challenge-name")
                if not title_el:
                    continue
                link_el  = card.select_one("a")
                href     = link_el.get("href","") if link_el else ""
                full_url = f"https://www.hackerearth.com{href}" if href.startswith("/") else href or url
                deadline_el = card.select_one(".date,.deadline,.end-date")
                opp = _make_opp(
                    title_el.get_text(strip=True), full_url, "hackerearth",
                    deadline=(deadline_el.get_text(strip=True) if deadline_el else ""),
                    company="HackerEarth", tags="hackathon competition challenge")
                if opp: results.append(opp)
            except Exception:
                continue
        time.sleep(1)
    return results


# ─────────────────────────────────────────────
#  8. Government portals
#     Dead URLs removed (MyGov /internship/ → 404, Internshala-Govt removed)
#     Working: CDAC, MeitY, NCIIPC, AICTE
#     Added: NICSI (NIC Services India — posts CS/IT internships)
# ─────────────────────────────────────────────

def scrape_govt_portals(config: dict) -> list[dict]:
    results   = []
    seen_urls: set[str] = set()
    relevant  = {"intern","trainee","cyber","security","program","scheme",
                 "fellowship","stipend","digital","it ","scholarship","apply"}

    portals = [
        ("CDAC",   "https://www.cdac.in/index.aspx?id=careers_internship",
                   "government india cdac internship cybersecurity"),
        ("MeitY",  "https://www.meity.gov.in/content/internship-0",
                   "government india meity digital cybersecurity"),
        ("NCIIPC", "https://nciipc.gov.in/",
                   "government cybersecurity internship india nciipc"),
        ("AICTE",  "https://internship.aicte-india.org/",
                   "government india aicte internship engineering"),
        ("NICSI",  "https://www.nicsi.com/career.php",
                   "government india nicsi nic internship it"),
        ("MyGov",  "https://www.mygov.in/group-issue/internships-fellowships/",
                   "government india internship program scheme fellowship"),
    ]

    for name, base_url, tags in portals:
        resp = _get(base_url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or not 10 <= len(text) <= 200:
                continue
            if not any(t in text.lower() for t in relevant):
                continue
            href = a.get("href","")
            base = "/".join(base_url.split("/")[:3])
            full_url = href if href.startswith("http") else f"{base}{href}" if href.startswith("/") else base_url
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            opp = _make_opp(f"[{name}] {text[:120]}", full_url, "govt_portals",
                company=name, tags=tags)
            if opp: results.append(opp)
        time.sleep(1.5)
    return results


# ─────────────────────────────────────────────
#  9. GitHub API
# ─────────────────────────────────────────────

def scrape_github_opportunities(config: dict) -> list[dict]:
    results = []
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "opportunity-crawler/1.0",
    }
    gh_token = os.environ.get("GITHUB_TOKEN","")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    queries = [
        "cybersecurity internship India 2026",
        "security internship program India",
        "gsoc 2026 security",
        "apcsip 2026",
        "NICSI internship",
    ]

    for query in queries:
        api_url = (f"https://api.github.com/search/repositories"
                   f"?q={urllib.parse.quote(query)}&sort=updated&per_page=5")
        try:
            resp = httpx.get(api_url, headers=headers, timeout=10)
            if resp.status_code == 403:
                print("[GitHub] Rate limited — add GITHUB_TOKEN secret for higher limits")
                break
            if resp.status_code != 200:
                continue
            for repo in resp.json().get("items",[]):
                opp = _make_opp(
                    f"[GitHub] {repo.get('full_name','')}",
                    repo.get("html_url",""), "github",
                    description=(repo.get("description") or "")[:200],
                    company=repo.get("owner",{}).get("login",""),
                    tags=f"open source {query}")
                if opp: results.append(opp)
        except Exception as e:
            print(f"[GitHub] Error: {e}")
        time.sleep(1)
    return results


# ─────────────────────────────────────────────
#  10. RSS feeds
#      Fixed: Reddit search now uses subreddit-only + flair filters
#      Strips HTML from Reddit summaries (previously passed raw HTML)
# ─────────────────────────────────────────────

_PERSONAL_POST_RE = re.compile(
    r"\b(boyfriend|girlfriend|relationship|dating|advice|vent|frustrated|"
    r"anxiety|depression|am i wrong|aita|salary negotiation help|help me)\b",
    re.IGNORECASE
)

def scrape_rss_feeds(config: dict) -> list[dict]:
    results = []
    feedparser.USER_AGENT = _HEADERS["User-Agent"]

    feeds = [
        # r/netsec quarterly hiring thread — actual job listings
        ("Reddit netsec hiring",  "https://www.reddit.com/r/netsec/search.rss?q=flair%3AHiring&restrict_sr=1&sort=new"),
        # r/cybersecurity jobs flair
        ("Reddit cybersec jobs",  "https://www.reddit.com/r/cybersecurity/search.rss?q=flair%3AJobs+India&restrict_sr=1&sort=new"),
        # HackerNews jobs — security filtered
        ("HN security jobs",      "https://hnrss.org/jobs?q=security+internship"),
        # InfoSec Jobs board
        ("InfoSec Jobs",          "https://infosec-jobs.com/feed/"),
    ]

    for source_name, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue
            count = 0
            for entry in feed.entries[:8]:
                title = entry.get("title","")
                if _PERSONAL_POST_RE.search(title):
                    continue
                # Strip HTML tags from Reddit summaries
                raw_desc   = entry.get("summary","")
                clean_desc = re.sub(r"<[^>]+>", " ", raw_desc)
                clean_desc = re.sub(r"\s+", " ", clean_desc).strip()[:300]

                opp = _make_opp(
                    title, entry.get("link", feed_url), "rss",
                    description=clean_desc,
                    company=source_name,
                    deadline=str(entry.get("published","")),
                    tags=f"cybersecurity internship india {source_name.lower()}")
                if opp:
                    results.append(opp)
                    count += 1
            if count:
                print(f"  [RSS] {source_name}: {count} entries")
        except Exception as e:
            print(f"  [RSS] Error {source_name}: {e}")
    return results


# ─────────────────────────────────────────────
#  Master dispatcher — runs all enabled scrapers concurrently
# ─────────────────────────────────────────────

SCRAPERS: dict = {
    "linkedin":       scrape_linkedin,
    "wellfound":      scrape_wellfound,       # Actually Indeed India
    "google_news":    scrape_google_news,
    "ncs":            scrape_ncs,             # Sarkari Result + govt aggregators
    "certifications": scrape_certin,          # CERT-In + DSCI + MHA
    "hackerearth":    scrape_hackerearth,
    "govt_portals":   scrape_govt_portals,    # CDAC, MeitY, NCIIPC, AICTE, NICSI
    "github":         scrape_github_opportunities,
    "rss":            scrape_rss_feeds,
}


def run_all_scrapers(sources_config: dict) -> list[dict]:
    """
    Run enabled scrapers concurrently (max 4 threads).
    Includes cross-scraper URL dedup.
    """
    tasks = {
        name: (fn, sources_config.get(name, {}) if isinstance(sources_config.get(name), dict) else {})
        for name, fn in SCRAPERS.items()
        if not (isinstance(sources_config.get(name), dict)
                and not sources_config[name].get("enabled", True))
    }

    skipped = set(SCRAPERS) - set(tasks)
    if skipped:
        print(f"[Scraper] Disabled: {', '.join(sorted(skipped))}")

    raw_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, cfg): name for name, (fn, cfg) in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                print(f"[Scraper] {name}: {len(items)} items")
                raw_results.extend(items)
            except Exception as e:
                print(f"[Scraper] ERROR in {name}: {e}")

    # Cross-scraper URL dedup — keeps first occurrence
    seen: set[str] = set()
    all_results: list[dict] = []
    for item in raw_results:
        url = item.get("url","")
        if url and url not in seen:
            seen.add(url)
            all_results.append(item)

    url_dupes = len(raw_results) - len(all_results)

    # Content-based dedup — same title + company + location but a DIFFERENT
    # URL still gets through the check above. This happens for real:
    # companies often repost the identical role multiple times for visibility,
    # each getting its own LinkedIn job ID (e.g. "Security Operations Center
    # Associate @ ECI, Bengaluru" appearing twice with different post times
    # and different URLs was observed in production). Keep the first/oldest
    # occurrence — earliest posting is the canonical one.
    content_seen: set[tuple] = set()
    deduped_results: list[dict] = []
    for item in all_results:
        key = (
            item.get("title","").strip().lower(),
            item.get("company","").strip().lower(),
            item.get("location","").strip().lower(),
        )
        # Only dedup on this key when title+company are both non-empty —
        # avoids accidentally merging unrelated listings that both happen
        # to have blank company/location fields.
        if key[0] and key[1] and key in content_seen:
            continue
        if key[0] and key[1]:
            content_seen.add(key)
        deduped_results.append(item)

    content_dupes = len(all_results) - len(deduped_results)

    if url_dupes or content_dupes:
        parts = []
        if url_dupes:
            parts.append(f"{url_dupes} exact-URL")
        if content_dupes:
            parts.append(f"{content_dupes} same-listing-different-URL")
        print(f"[Scraper] Removed {' + '.join(parts)} duplicates → {len(deduped_results)} unique")

    return deduped_results
