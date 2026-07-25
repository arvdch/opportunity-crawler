"""
filter.py — Three-stage filter pipeline.

Stage 1: Hard exclusion (instant reject, no API needed)
Stage 2: Keyword scoring (pre-compiled regex)
Stage 3: AI scoring — tries Groq models in priority order.

Groq model chain (updated July 21 2026):
  ALL deprecated June 17 2026 (404 or empty responses):
    - meta-llama/llama-4-scout-17b-16e-instruct  → 404
    - qwen/qwen3-32b                              → 404
    - llama-3.3-70b-versatile                     → deprecated
    - llama-3.1-8b-instant                        → deprecated (→ gpt-oss-20b)

  qwen/qwen3.6-27b issue: Groq's hosted version ignores the /no_think soft
  switch and always generates a <think> chain. The chain is ~300-500 tokens.
  With max_tokens=80 it gets truncated — no JSON ever appears, fallback=7 always.
  Fix: set max_tokens=1500 so the think chain completes and JSON is generated.
  At 1500 tok/call × 10 calls/min (6s gap) = 15,000 TPM — just above the 6K
  free-tier cap. So we use 2 models in rotation: qwen primary (until 429),
  then gpt-oss-20b takes over. Both use 1500/200 max_tokens respectively.

  Current working chain (free tier, 1,000 RPD each):
    1. qwen/qwen3.6-27b      — primary: clean JSON, fast, 1K RPD, 6K TPM
    2. openai/gpt-oss-20b    — secondary: fast small model, llama-3.1-8b replacement
    3. openai/gpt-oss-120b   — last resort: reasoning model, use max_tokens=400

  Free tier limits (per model, per org):
    RPM: 30  |  TPM: 6,000  |  RPD: 1,000
    gpt-oss-120b: 8K TPM, 200K TPD (but effective ~600 calls/day at 300tok/call)
    Binding constraint = RPD (1,000/day) for most models at our call volume.
    At 150 calls/run we use 150/1000 = 15% of daily RPD per model. Fine.
"""

import os
import re
import json
import time
import httpx
from pathlib import Path

# ── AI config ─────────────────────────────────────────────────────────────────

AI_THRESHOLD   = 7
MAX_LINKEDIN_AGE_DAYS = 45

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Model chain — based on official Groq deprecation page (July 22 2026):
# Dead/deprecated:
#   gemma2-9b-it          → shutdown Oct 8 2025
#   llama-3.1-8b-instant  → shutdown Aug 16 2026
#   llama-3.3-70b-versatile → shutdown Aug 16 2026
#   qwen/qwen3-32b        → shutdown Jul 17 2026
#   llama-4-scout         → shutdown Jul 17 2026
#
# Currently active and officially recommended:
#   qwen/qwen3.6-27b    — replacement for llama-3.3-70b-versatile, confirmed working
#   openai/gpt-oss-20b  — replacement for llama-3.1-8b-instant
#   openai/gpt-oss-120b — replacement for scout/qwen3-32b, confirmed working
#
# All three are reasoning models (emit <think> chains).
# Strategy: round-robin across all three, 9s effective cadence per model.
# At 3000 max_tokens and 9s/call: 3000 × 6.7 calls/min = 20,000 TPM total
# spread across 3 pools (6,667 TPM each) — under the 8K cap per model.
GROQ_MODEL_CHAIN = [
    "qwen/qwen3.6-27b",       # Primary — confirmed 20/20 scoring accuracy
    "openai/gpt-oss-20b",     # Secondary — replacement for llama-3.1-8b-instant
    "openai/gpt-oss-120b",    # Tertiary — replacement for scout/qwen3-32b
]

GROQ_MODEL_MAX_TOKENS = {
    "qwen/qwen3.6-27b":    3000,
    "openai/gpt-oss-20b":  1500,
    "openai/gpt-oss-120b": 2000,
}

# Round-robin index — advances each call so TPM load is spread across all models.
# Reset to 0 at start of each run (module-level mutable).
_groq_rr_index: int = 0

# OpenRouter / Gemini / Anthropic fallback chains (unchanged)
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openrouter/free"
GEMINI_URL       = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL  = "claude-sonnet-4-6"

# ── Stage 1a: Role blocklist (title only) ─────────────────────────────────────

_EXCLUDE_TITLE_RAW = [
    r"\bsales\b",
    r"\bmarketing\b",
    r"\baccountant\b",
    r"\baccounting\b",
    r"\bfinance\b(?!.*security)",
    r"\bbanking\b(?!.*security)",
    r"\bhrm?\b",
    r"\bhuman resources\b",
    r"\bteacher\b",
    r"\btutor\b",
    r"\bcontent writer\b",
    r"\bcopywriter\b",
    r"\bgraphic design\b",
    r"\bux design\b(?!.*security)",
    r"\bdata entry\b",
    r"\bcustomer support\b",
    r"\bcustomer service\b",
    r"\bcall cent(er|re)\b",
    r"\bmechanical\b",
    r"\bcivil engineer\b",
    r"\belectrical engineer\b",
    r"\bembedded systems\b(?!.*security)",
    r"\bmba\b",
    r"\bca intern\b",
    r"\bpharmac",
    r"\bnursing\b",
    r"\bdental\b",
    r"\bmedical representative\b",
    r"\bfield sales\b",
    r"\bbusiness development\b(?!.*security)",
    r"\breal estate\b",
    r"\binsurance agent\b",
    r"\blogistics\b(?!.*security)",
    r"\bsupply chain\b(?!.*security)",
    r"\bdigital marketing\b",
    r"\bseo\b",
    r"\bsocial media\b",
    r"\bcivil\b(?!.*security)",
    r"\btextile\b",
    r"\bfashion\b",
    r"\binterior design\b",
    # Physical security — not cybersecurity
    r"\bsecurity guard\b",
    r"\bsecurity screener\b",
    r"\bsecurity officer\b(?!.*information|.*cyber|.*IT|.*cloud|.*network)",
    r"\bsocial security\b",
    r"\bphysical security analyst\b",
    # Business/competitive intelligence — not cybersecurity
    r"\bbusiness intelligence\b(?!.*security)",
    r"\bcompetitive intelligence\b",
    r"\bmarket intelligence\b",
    r"\bprocurement intelligence\b",
    r"\bplatform intelligence\b",
    # Generic analyst noise with no security context (caught by keyword miss anyway,
    # but hard-excluding saves AI budget)
    r"^analyst$",                        # bare "Analyst" title — too vague
    r"^intelligence analyst$",           # geopolitical/physical, not cyber
]

# ── Stage 1b: Experience wall (title + description) ───────────────────────────
#
# Goal: pass only internships, fresher roles (0-2 yrs), L0/L1 analyst jobs,
#       and government/academic programmes.
# Strategy:
#   • Title-only patterns: fast rejection of obvious senior/lead/manager titles
#   • Title+desc patterns: catch experience requirements buried in JD text
#
# NOTE: L2/L3 SOC roles in India are NOT always mid-senior — some MSSPs
# use L1/L2/L3 as shift tier labels and hire freshers for L1. So we do NOT
# hard-exclude "L2 SOC" from the title; the AI scores those appropriately.
# We DO hard-exclude "Senior", "Lead", "Manager", "Director", "Principal",
# "Staff", "Head of", "VP", "Architect" combined with security context.

_EXCLUDE_TITLE_SENIOR_RAW = [
    # Seniority prefix + any security-adjacent word
    r"\bsenior\b.{0,30}\b(security|cyber|soc|analyst|engineer|pentest|cloud|infosec|grc|vapt|devsecops)\b",
    r"\blead\b.{0,20}\b(security|cyber|soc|analyst|engineer|infosec)\b",
    r"\bprincipal\b.{0,20}\b(security|cyber|engineer|analyst)\b",
    r"\bstaff\b.{0,20}\b(security|engineer)\b",
    r"\bmanager\b.{0,20}\b(security|cyber|soc|infosec|risk|compliance)\b",
    r"\b(security|cyber|soc|infosec)\b.{0,20}\bmanager\b",
    r"\bdirector\b",        # Any director title — all are senior by definition
    r"\bvice\s+president\b",
    r"\b\bvp\s+(of\s+)?(security|cyber|infosec|engineering)\b",
    r"\bhead\s+of\b.{0,30}\b(security|cyber|infosec)\b",
    r"\b(vp|vice\s+president)\b.{0,30}\b(security|cyber|infosec)\b",
    r"\bsecurity\s+architect\b",
    r"\bcloud\s+security\s+architect\b",
    r"\bchief\s+(information\s+)?security\b",   # CISO
    # Title-level experience clues
    r"\b(sr|sr\.)\s+(security|cyber|soc|analyst|engineer)\b",
    # ── Mid-level suffixes ────────────────────────────────────────────────────
    # "Analyst II", "Security Analyst II", "IT Security Analyst II" etc.
    # Rule: block when a seniority suffix (II/III/L2/L3) follows a role word.
    # Allow: "L2 SOC Analyst", "SOC L2" (Indian MSSP prefix style) — these pass
    #         to AI which scores based on description exp requirements.
    # Block: "Cyber Security Analyst L2", "Cyber Security Analyst L3",
    #         "Security Analyst II", "Analyst II - Information Security"
    #
    # Pattern: role_word ... suffix (suffix at word boundary, not just at end of string)
    r"\b(security|cyber|soc|infosec|analyst|engineer)\\b.{0,50}\bii\b",   # Analyst II anywhere after role word
    r"\b(security|cyber|soc|infosec|analyst|engineer)\\b.{0,50}\biii\b",  # Analyst III
    # "Analyst L2/L3" as trailing suffix = mid-level.
    # Block: "SOC Analyst L3", "Cyber Security Analyst L2", "Security Engineer L2"
    # Allow: "L2 SOC Analyst", "SOC L2", "L3 SOC Analyst" (prefix = Indian MSSP)
    # Allow: bare "SOC L2", "SOC L3" — too ambiguous, let AI decide
    r"\b(?<!l[123]\s)(analyst|engineer|specialist)\s+(l[23456])\b",
    r"\b(soc)\s+(analyst|engineer)\s+(l[23456])\b",           # SOC Analyst L2/L3
    r"\b(cyber|security|infosec)\s+(analyst|engineer)\s+(l[23456])\b",  # Cyber Security Analyst L2/L3
    # Prefix-style L2/L3 before a role word — "L2 SOC Analyst", "L3 Security Engineer"
    # These are still mid-level regardless of word order
    r"\bl[23456]\s+(soc|security|cyber|infosec)\s+(analyst|engineer|specialist)\b",
    # Bare "SOC L2", "SOC L3", "SOC Engineer L2" — mid-level shift tier
    r"\bsoc\s+l[23456]\b",
    # "Intermediate" engineer/analyst titles = mid-level
    r"\b(security|cyber|soc|infosec|it)\b.{0,30}\bintermediate\b",
    r"\bintermediate\b.{0,30}\b(security|cyber|soc|infosec|engineer|analyst)\b",
    # Fleet operations — physical/logistics security, not cybersecurity
    r"\bfleet\s+operations?\b",
    # Explicit "Level 2" / "Level 3" at end of title
    r"\blevel\s+[2-9]\b",
    # "Specialist II", "Specialist III"
    r"\bspecialist\s+(ii|iii)\b",
]

_EXCLUDE_EXP_DESC_RAW = [
    # Raw year counts in description
    r"\b[3-9]\+?\s*years?\s*(of\s+)?(relevant\s+)?experience\b",
    r"\b1[0-9]\+?\s*years?\s*(of\s+)?experience\b",
    r"\bminimum\s+[3-9]\s*years?\b",
    r"\bat\s+least\s+[3-9]\s*years?\b",
    r"\b[3-9]\+?\s*yrs?\s*(of\s+)?experience\b",
    # Team management signals — definitely not entry-level
    r"\bmanage\s+a\s+team\b",
    r"\blead\s+a\s+team\b",
    r"\bteam\s+lead(er)?\s+(of|for|with)\b",
    r"\bpeople\s+management\b",
    r"\bdirect\s+reports\b",
]

_EXCLUDE_TITLE_SENIOR_RE = [re.compile(p, re.IGNORECASE) for p in _EXCLUDE_TITLE_SENIOR_RAW]
_EXCLUDE_EXP_RAW = _EXCLUDE_EXP_DESC_RAW  # keep old name so rest of code works

# ── Stage 1c–f: Other hard blocks ────────────────────────────────────────────

_BLOCKED_SOURCES = {"internshala"}

# Static pages / social profiles / news blogs that scraper picks up — not job listings
_BLOCKED_URL_PATTERNS = [
    re.compile(r"facebook\.com", re.IGNORECASE),
    re.compile(r"twitter\.com|x\.com", re.IGNORECASE),
    re.compile(r"cybercrime\.gov\.in$", re.IGNORECASE),
    re.compile(r"sarkariresult\.com/.*(jan2[0-3]|nov2[0-2]|2019|2020|2021|2022|rbi-security-guards)", re.IGNORECASE),
    # Blog/news aggregator spam from google_news — these are articles ABOUT
    # internships, not actual internship listings. Only block when they're
    # clearly article/blog URLs, not government portal pages.
    re.compile(r"(myinternships\.in|skillwalaglobal\.com|cguru\.co\.in|cyberdeepakyadav\.com|news\.lawfoyer\.in|cyber-security\.co\.in)", re.IGNORECASE),
    re.compile(r"glassdoor\.co\.in/Job/.*internship", re.IGNORECASE),    # Glassdoor listing pages, not jobs
    re.compile(r"medium\.com/", re.IGNORECASE),
    re.compile(r"linkedin\.com/pulse/", re.IGNORECASE),    # LinkedIn articles, not jobs
    re.compile(r"linkedin\.com/posts/", re.IGNORECASE),    # LinkedIn posts, not jobs
    re.compile(r"linkedin\.com/jobs/.*internship.*jobs$", re.IGNORECASE),  # LinkedIn search page not a job
    # Reddit hiring megathreads — community threads, not job listings
    re.compile(r"reddit\.com/r/\w+/comments/.*hiring", re.IGNORECASE),
    re.compile(r"reddit\.com/r/\w+/comments/.*information.security.hiring", re.IGNORECASE),
]

# Govt internship URL patterns — these get a score boost signal for AI
# Only genuinely cyber-related govt portals
_GOVT_CYBER_URLS = re.compile(
    r"(nciipc\.gov\.in|cdac\.in|nicsi\.nic\.in|meity\.gov\.in|aicte-india\.org|cert-in\.org\.in|dsci\.in|mha\.gov\.in/.*cyber)",
    re.IGNORECASE
)
_CTFTIME_RE = re.compile(r"^CTFTime$", re.IGNORECASE)
_FAKE_COMPANY_RE = re.compile(
    r"^(.*\s)?(intern|interns|internworld|internhub|internhq|internplace)(\s.*)?$",
    re.IGNORECASE
)
_FAKE_COMPANY_NAMES = {
    "wake up whistle", "skillfied mentor jobs", "skillfied",
    "internworld", "internhub", "letsintern", "freshersworld", "wisdomjobs",
}
# Companies whose listings are consistently noise — AI annotation jobs,
# bulk identical postings, or non-cyber roles disguised as security titles.
_BLOCKED_COMPANIES = {
    "alignerr",   # AI training annotation jobs (not real security roles)
    "scoutit",    # Bulk identical "Security Analyst" postings, unverifiable
    "max security",  # Physical security firm, not cyber
    "hakatemia",  # CTF/training platform, not hiring
    "pinkerton",  # Physical security/investigations firm, not cyber
}
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.IGNORECASE)
_INDIA_CITIES = {
    "hyderabad","bangalore","bengaluru","mumbai","delhi","chennai",
    "pune","kolkata","noida","gurugram","gurgaon","india","ahmedabad",
    "kochi","jaipur","bhubaneswar","chandigarh","trivandrum","coimbatore",
}

# Pre-compile
_EXCLUDE_TITLE_RE = [re.compile(p, re.IGNORECASE) for p in _EXCLUDE_TITLE_RAW]
_EXCLUDE_EXP_RE   = [re.compile(p, re.IGNORECASE) for p in _EXCLUDE_EXP_RAW]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_blob(opp: dict) -> str:
    return " ".join(str(opp.get(f,"")) for f in
                    ["title","description","tags","company","location"] if opp.get(f))

def _build_keyword_index(keywords: list[str]) -> list[re.Pattern]:
    patterns = []
    for kw in keywords:
        kw_norm = kw.lower().strip()
        if len(kw_norm) <= 5:
            patterns.append(re.compile(r"\b" + re.escape(kw_norm) + r"\b", re.IGNORECASE))
        else:
            patterns.append(re.compile(re.escape(kw_norm), re.IGNORECASE))
    return patterns


# ── Stage 1: Hard exclusion ───────────────────────────────────────────────────

def hard_exclude(opp: dict) -> tuple[bool, str]:
    source  = opp.get("source","")
    title   = opp.get("title","")
    company = opp.get("company","").strip()
    desc    = opp.get("description","")
    loc     = opp.get("location","")

    if source in _BLOCKED_SOURCES:
        return True, f"blocked source: {source}"

    url = opp.get("url", "")
    for pat in _BLOCKED_URL_PATTERNS:
        if pat.search(url):
            return True, f"blocked URL pattern: {pat.pattern}"
    if _CTFTIME_RE.match(company):
        return True, "CTFTime event"
    co_lower = company.lower()
    if co_lower in _FAKE_COMPANY_NAMES:
        return True, f"known fake/low-quality company: {company}"
    if co_lower in _BLOCKED_COMPANIES or any(b in co_lower for b in _BLOCKED_COMPANIES):
        return True, f"blocked company (noise source): {company}"
    if company and _FAKE_COMPANY_RE.match(company):
        return True, f"suspicious company name: {company}"

    for pat in _EXCLUDE_TITLE_RE:
        if pat.search(title):
            return True, f"blocked role: {pat.pattern}"
    # Senior/lead/manager title check — title only, surgical
    for pat in _EXCLUDE_TITLE_SENIOR_RE:
        if pat.search(title):
            return True, f"senior role: {pat.pattern}"
    # Experience wall — title + description
    for pat in _EXCLUDE_EXP_RE:
        if pat.search(title + " " + desc):
            return True, f"exp wall: {pat.pattern}"

    if source == "linkedin":
        loc_lower = loc.lower()
        if _REMOTE_RE.search(loc_lower) and not any(c in loc_lower for c in _INDIA_CITIES):
            return True, f"remote-only LinkedIn, no real location: {loc}"
        # Non-India physical locations — India-only target
        _NON_INDIA_MARKERS = (
            "saudi", "riyadh", "jeddah", "al khobar", "dubai", "abu dhabi",
            "singapore", "malaysia", "philippines", "indonesia", "vietnam",
            "united states", "united kingdom", "canada", "australia",
            "germany", "france", "netherlands", "ireland", "poland",
        )
        if loc_lower and not any(c in loc_lower for c in _INDIA_CITIES) and \
                any(m in loc_lower for m in _NON_INDIA_MARKERS):
            return True, f"non-India location: {loc}"
        # Scraper sometimes returns empty location — check title as fallback
        # e.g. "SOC Analyst L1 Al Khobar Saudi National"
        title_lower = title.lower()
        if not loc_lower and any(m in title_lower for m in _NON_INDIA_MARKERS):
            return True, f"non-India location in title: {title}"
        # Always check URL slug regardless of location field — scraper sometimes
        # fills location='India' (generic) while the URL contains the real location.
        # e.g. /soc-analyst-l1-al-khobar-saudi-national-at-...
        url_lower = opp.get("url", "").lower()
        if any(m.replace(" ", "-") in url_lower for m in _NON_INDIA_MARKERS):
            return True, f"non-India location in URL: {opp.get('url','')}"

    if source == "linkedin":
        posted = opp.get("deadline", "")
        if posted:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - dt).days
                if age_days > MAX_LINKEDIN_AGE_DAYS:
                    return True, f"stale LinkedIn listing: {age_days} days old"
            except (ValueError, AttributeError, TypeError):
                pass

    if source == "github":
        return True, "GitHub repos are writeups/portfolios, not open positions"

    if source == "rss" and "hiring thread" in title.lower():
        posted = opp.get("deadline", "")
        if posted:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - dt).days
                if age_days > 60:
                    return True, f"stale hiring megathread: {age_days} days old"
            except (ValueError, AttributeError, TypeError):
                pass

    return False, ""


# ── Stage 2: Keyword scoring ──────────────────────────────────────────────────

def keyword_score(opp: dict, kw_patterns: list[re.Pattern]) -> int:
    blob = _build_blob(opp)
    return sum(1 for pat in kw_patterns if pat.search(blob))


# ── Stage 3: AI scoring ───────────────────────────────────────────────────────

_resume_cache: str = ""

def _load_resume() -> str:
    """
    Compact, accurate resume snapshot for AI prompt.
    Hardcoded from actual resume — update here when resume changes.
    Kept short to stay under Groq free-tier TPM limits (~12K TPM).
    """
    global _resume_cache
    if _resume_cache:
        return _resume_cache
    _resume_cache = (
        "B.Tech CSE (Cybersecurity), JNTUH Hyderabad, 2023-present (3rd year). "
        "Intern @ JD Infotech (June 2025): web VAPT, Burp Suite, Nmap, Hydra, SQLi/XSS/IDOR. "
        "Projects: Splunk SOC home lab (correlation searches, SPL, Sysmon, detection engineering); "
        "HEXFORGE (Python forensics tool). "
        "Skills: Splunk/SPL, Wireshark, Metasploit, Python, Bash, Kali Linux, "
        "IAM/cloud security foundations, digital forensics, CTF top-15% TryHackMe. "
        "Target roles: SOC L1 analyst, cloud security intern, detection engineering intern, "
        "cybersecurity fresher/trainee, govt cybersecurity internships (NCIIPC/CDAC/NICSI). "
        "India only. Entry-level or internship ONLY — 0-2 yrs experience max."
    )
    return _resume_cache


def _build_prompt(opp: dict) -> str:
    """
    Strict scoring prompt. Ends with /no_think to disable Qwen3's reasoning
    chain — without this, qwen3.6-27b outputs <think>...</think> blocks that
    exceed max_tokens and produce unparseable/truncated responses.
    /no_think is a Qwen3 soft switch; other models ignore it harmlessly.
    Threshold is 7 — score 7+ passes, 6 and below is dropped.
    """
    govt_hint = ""
    if _GOVT_CYBER_URLS.search(opp.get("url", "") + opp.get("description", "")):
        govt_hint = "\nNOTE: Official Indian govt cybersecurity portal — score 9 if internship/training.\n"

    return (
        f"You are a strict job filter. Score this listing 1-10 for this candidate.\n"
        f"Candidate: {_load_resume()}\n\n"
        f"Listing:\nTitle: {opp.get('title','')}\n"
        f"Company: {opp.get('company','')}\n"
        f"Location: {opp.get('location','')}\n"
        f"Desc: {opp.get('description','')[:180]}\n"
        f"{govt_hint}\n"
        f"DISQUALIFIERS — score 1-5 if ANY apply:\n"
        f"• Senior/Lead/Manager/Principal/Architect/VP/Director in title or desc\n"
        f"• Title ends in II, III, L2, L3 (mid-level suffix)\n"
        f"• Requires 3+ years experience\n"
        f"• Outside India, physical security, non-cybersecurity\n\n"
        f"QUALIFIERS — score 7-10 only if NO disqualifiers AND:\n"
        f"• Internship/fresher/trainee/associate/entry-level/L0/L1/0-2yr\n"
        f"• SOC/cloud security/VAPT/GRC/detection/SIEM/pentest/infosec\n"
        f"• India-based\n"
        f"Score 7=entry-level OK; 8-9=strong fit; 10=perfect intern.\n"
        f"Score 6=borderline REJECTED. Score 1-5=disqualified.\n"
        f'Output ONLY valid JSON: {{"score":<1-10>,"reason":"<5 words>"}}\n'
        f"/no_think"
    )


# ── Groq model state tracking ─────────────────────────────────────────────────

# Models confirmed dead (404) this run — skip immediately, don't retry
_groq_dead_models: set[str] = set()
# Models that hit their daily RPD quota this run
_groq_exhausted_models: set[str] = set()


def _ai_score_groq(opp: dict, api_key: str, _model_index: int = 0, _rpm_strikes: int = 0) -> tuple[int, str]:
    """
    Score using Groq Cloud. Walks GROQ_MODEL_CHAIN in priority order.

    Model skipping:
      - 404 (model removed/renamed) → mark dead, skip to next immediately
      - RPD exhausted (daily quota) → mark exhausted, skip to next
      - RPM (per-minute) → 3-strike escalating wait before giving up on model

    RPM 3-strike system:
      Strike 0: wait retry-after + 1s
      Strike 1: wait 65s (full window guarantee)
      Strike 2: treat as saturated, move to next model
    """
    if _model_index >= len(GROQ_MODEL_CHAIN):
        return 7, "AI skipped — all Groq models exhausted/unavailable today"

    model = GROQ_MODEL_CHAIN[_model_index]

    # Skip models confirmed dead or daily-exhausted this run
    if model in _groq_dead_models or model in _groq_exhausted_models:
        return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

    try:
        # NOTE: response_format=json_object is intentionally NOT used here.
        # gpt-oss-120b (and some other Groq models) return HTTP 400
        # json_validate_failed when this parameter is set, even though they
        # can generate valid JSON without it. We parse JSON from the text
        # response ourselves with regex fallback, which is robust enough.
        max_tok = GROQ_MODEL_MAX_TOKENS.get(model, 80)
        resp = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _build_prompt(opp)}],
                "max_tokens": max_tok,
                "temperature": 0.1,
            },
            timeout=30,
        )

        # ── 404: model removed/renamed — mark dead, move on immediately ──
        if resp.status_code == 404:
            print(f"  [Groq] {model}: 404 — model removed/renamed, skipping permanently")
            _groq_dead_models.add(model)
            return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

        # ── 400 json_validate_failed: model doesn't support json_object mode
        #    or our prompt triggered a content filter. Mark dead for this run.
        if resp.status_code == 400:
            body_text = resp.text
            if "json_validate_failed" in body_text or "json" in body_text.lower():
                print(f"  [Groq] {model}: 400 json_validate_failed — marking dead, trying next model")
                _groq_dead_models.add(model)
                return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)
            # Other 400 — log and fall through to raise_for_status
            print(f"  [Groq] {model}: HTTP 400 — {body_text[:200]}")

        if resp.status_code == 429:
            error_text  = resp.text.lower()
            retry_after = resp.headers.get("retry-after")
            is_daily    = bool(re.search(
                r"per day|requests per day|tokens per day|\brpd\b|\btpd\b",
                error_text
            ))

            if is_daily:
                print(f"  [Groq] {model}: daily quota exhausted — trying next model")
                _groq_exhausted_models.add(model)
                return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

            # RPM/TPM hit.
            # Strategy: switch to next model immediately on strike 0.
            # Reason: retry-after from Groq is often 2-5s which isn't enough
            # to clear the 60s RPM window, causing strike 1 spam as seen in logs.
            # Moving to the next model is faster and avoids the spam entirely.
            # We only wait on strike 0 if retry_after > 30s (genuine long backoff).
            ra = float(retry_after) if retry_after else 0
            if ra > 30:
                # Long backoff — worth waiting once
                print(f"  [Groq] {model}: rate limit — waiting {ra:.0f}s (long backoff)")
                time.sleep(ra + 1)
                return _ai_score_groq(opp, api_key, _model_index=_model_index, _rpm_strikes=_rpm_strikes + 1)
            else:
                # Short retry-after (≤30s) — not worth waiting, switch model now
                print(f"  [Groq] {model}: rate limit (retry-after={ra:.0f}s) — switching to next model")
                _groq_exhausted_models.add(model)
                return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

        if resp.status_code >= 400:
            print(f"  [Groq] {model}: HTTP {resp.status_code} — {resp.text[:150]}")
        resp.raise_for_status()

        body    = resp.json()
        choices = body.get("choices") or []
        if not choices:
            return 7, "AI error — empty response (included by default)"

        raw = choices[0].get("message", {}).get("content")
        if not raw:
            print(f"  [Groq] {model}: null content — trying next model")
            _groq_dead_models.add(model)
            return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

        # Strip complete <think>...</think> blocks (reasoning models)
        raw_stripped = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw_stripped = re.sub(r"```json|```", "", raw_stripped).strip()

        # If <think> block was truncated (no closing tag) — response is all thinking,
        # no JSON was ever generated. Try to recover JSON from the tail, else next model.
        if "<think>" in raw_stripped:
            last_json = raw_stripped.rfind('{"score"')
            if last_json != -1:
                raw_stripped = raw_stripped[last_json:]
            else:
                # Truncated thinking chain — model ran out of tokens before JSON.
                # Mark this model as needing more tokens and try next.
                print(f"  [Groq] {model}: thinking chain truncated (no JSON) — trying next model")
                _groq_dead_models.add(model)
                return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)

        raw = raw_stripped
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        candidate = match.group(0) if match else raw

        try:
            data  = json.loads(candidate)
            score = max(1, min(10, int(data.get("score", 5))))
            return score, str(data.get("reason", ""))[:80]
        except (json.JSONDecodeError, ValueError):
            score_match = re.search(r'"score"\s*:\s*(\d+)', raw)
            if score_match:
                return max(1, min(10, int(score_match.group(1)))), "score recovered from partial JSON"
            print(f"  [Groq] {model}: unparseable: {raw[:100]}")
            return 7, "AI error — unparseable (included by default)"

    except Exception as e:
        err_str = str(e)
        # Connection-level errors (reset, timeout, refused) — try next model.
        # These are transient network failures, not scoring failures; burning
        # budget on a fallback=7 here masks real scoring and exhausts budget fast.
        is_conn_err = any(s in err_str for s in (
            "Connection reset", "ConnectionReset", "Errno 104",
            "ConnectionRefused", "Errno 111",
            "timed out", "Timeout", "ReadTimeout",
            "RemoteDisconnected", "BrokenPipe",
            "UNEXPECTED_EOF_WHILE_READING", "EOF occurred",  # SSL connection drops
            "SSL", "SSLError",
        ))
        if is_conn_err and _model_index + 1 < len(GROQ_MODEL_CHAIN):
            print(f"  [Groq] {model}: conn error ({err_str[:60]}) — trying next model")
            return _ai_score_groq(opp, api_key, _model_index=_model_index + 1, _rpm_strikes=0)
        print(f"  [Groq] {model}: error on '{opp.get('title','')[:40]}': {e}")
        return 7, "AI error — exception (included by default)"


# ── OpenRouter fallback ───────────────────────────────────────────────────────

_openrouter_daily_quota_exhausted = False

def _ai_score_openrouter(opp: dict, api_key: str, _retry: bool = True) -> tuple[int, str]:
    global _openrouter_daily_quota_exhausted
    if _openrouter_daily_quota_exhausted:
        return 7, "AI skipped — OpenRouter daily quota exhausted"

    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/opportunity-crawler",
                "X-Title": "Opportunity Crawler",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": _build_prompt(opp)}],
                "max_tokens": 100,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=25,
        )
        if resp.status_code == 429 and "free-models-per-day" in resp.text:
            print("  [OpenRouter] Daily free quota exhausted")
            _openrouter_daily_quota_exhausted = True
            return 7, "AI skipped — OpenRouter daily quota exhausted"
        if resp.status_code == 429 and _retry:
            time.sleep(15)
            return _ai_score_openrouter(opp, api_key, _retry=False)
        resp.raise_for_status()

        choices = resp.json().get("choices") or []
        if not choices and _retry:
            time.sleep(1)
            return _ai_score_openrouter(opp, api_key, _retry=False)

        raw = (choices[0].get("message", {}).get("content") or "") if choices else ""
        if not raw and _retry:
            time.sleep(1)
            return _ai_score_openrouter(opp, api_key, _retry=False)

        raw = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        candidate = match.group(0) if match else raw
        try:
            data  = json.loads(candidate)
            return max(1, min(10, int(data.get("score", 5)))), str(data.get("reason",""))[:80]
        except (json.JSONDecodeError, ValueError):
            score_match = re.search(r'"score"\s*:\s*(\d+)', raw)
            if score_match:
                return max(1, min(10, int(score_match.group(1)))), "score recovered"
            return 7, "AI error — unparseable"
    except Exception as e:
        print(f"  [OpenRouter] Error: {e}")
        return 7, "AI error — exception"


# ── Gemini / Anthropic fallbacks (unchanged) ──────────────────────────────────

def _ai_score_gemini(opp: dict, api_key: str) -> tuple[int, str]:
    try:
        resp = httpx.post(
            f"{GEMINI_URL}?key={api_key}",
            json={
                "contents": [{"parts": [{"text": _build_prompt(opp)}]}],
                "generationConfig": {"maxOutputTokens": 80, "temperature": 0.1},
            },
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = re.sub(r"```json|```", "", raw).strip()
        data  = json.loads(raw)
        return max(1, min(10, int(data.get("score", 5)))), str(data.get("reason",""))[:80]
    except Exception as e:
        print(f"  [Gemini] Error: {e}")
        return 7, "AI error — included by default"


def _ai_score_anthropic(opp: dict, api_key: str) -> tuple[int, str]:
    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 80,
                "messages": [{"role": "user", "content": _build_prompt(opp)}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        raw = re.sub(r"```json|```", "",
                     resp.json()["content"][0]["text"]).strip()
        data  = json.loads(raw)
        return max(1, min(10, int(data.get("score", 5)))), str(data.get("reason",""))[:80]
    except Exception as e:
        print(f"  [Claude] Error: {e}")
        return 7, "AI error — included by default"


def ai_score_opportunity(opp: dict) -> tuple[int, str]:
    global _groq_rr_index
    groq_key       = os.environ.get("GROQ_API_KEY","")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY","")
    gemini_key     = os.environ.get("GEMINI_API_KEY","")
    anthropic_key  = os.environ.get("ANTHROPIC_API_KEY","")

    if groq_key:
        # Start from round-robin position, advance after each successful call
        available = [m for m in GROQ_MODEL_CHAIN
                     if m not in _groq_dead_models and m not in _groq_exhausted_models]
        if available:
            start_model = available[_groq_rr_index % len(available)]
            start_index = GROQ_MODEL_CHAIN.index(start_model)
            result = _ai_score_groq(opp, groq_key, _model_index=start_index)
            # Advance RR pointer only on non-error responses
            if "AI error" not in result[1] and "skipped" not in result[1]:
                _groq_rr_index = (_groq_rr_index + 1) % len(available)
            return result
        return _ai_score_groq(opp, groq_key)  # all exhausted — let it handle

    if openrouter_key: return _ai_score_openrouter(opp, openrouter_key)
    if gemini_key:     return _ai_score_gemini(opp, gemini_key)
    if anthropic_key:  return _ai_score_anthropic(opp, anthropic_key)
    return 0, "AI scoring skipped — no API key"


# ── Source priority order for sorting ────────────────────────────────────────

SOURCE_PRIORITY = {
    "linkedin":       1,
    "wellfound":      2,
    "unstop":         3,
    "hackerearth":    3,
    "google_news":    4,
    "govt_portals":   4,
    "ncs":            4,
    "certifications": 4,
    "rss":            5,
    "github":         6,
    "unknown":        7,
}


# ── Master pipeline ───────────────────────────────────────────────────────────

def filter_opportunities(
    opportunities: list[dict],
    keywords: list[str],
    min_keyword_score: int = 1,
    use_ai: bool = True,
    ai_budget: int = 80,
) -> list[dict]:
    global _groq_rr_index, _groq_dead_models, _groq_exhausted_models
    # Reset per-run state so dead/exhausted models from a previous test run
    # don't bleed into the main run
    _groq_rr_index = 0
    _groq_dead_models = set()
    _groq_exhausted_models = set()
    kw_patterns    = _build_keyword_index(keywords)
    has_groq       = bool(os.environ.get("GROQ_API_KEY",""))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY",""))
    has_gemini     = bool(os.environ.get("GEMINI_API_KEY",""))
    has_anthropic  = bool(os.environ.get("ANTHROPIC_API_KEY",""))
    run_ai = use_ai and (has_groq or has_openrouter or has_gemini or has_anthropic)

    if run_ai:
        provider = ("Groq" if has_groq else
                    "OpenRouter" if has_openrouter else
                    "Gemini" if has_gemini else "Claude")
        print(f"  [Filter] AI scoring ENABLED via {provider} (budget: {ai_budget} calls/run)")

    # ── Stage 1 + 2 (fast, no API calls) ──
    stage12_passed = []
    n_hard = n_kw = 0
    excluded_log: list[dict] = []
    for opp in opportunities:
        excluded, reason = hard_exclude(opp)
        if excluded:
            n_hard += 1
            excluded_log.append({
                "title": opp.get("title", ""), "company": opp.get("company", ""),
                "source": opp.get("source", ""), "url": opp.get("url", ""),
                "reason": reason,
            })
            continue
        ks = keyword_score(opp, kw_patterns)
        opp["keyword_score"] = ks
        if ks < min_keyword_score:
            n_kw += 1
            excluded_log.append({
                "title": opp.get("title", ""), "company": opp.get("company", ""),
                "source": opp.get("source", ""), "url": opp.get("url", ""),
                "reason": "keyword-miss (score 0)",
            })
            continue
        stage12_passed.append(opp)

    try:
        with open("debug_excluded.json", "w") as f:
            json.dump(excluded_log, f, indent=2)
    except Exception:
        pass

    stage12_passed.sort(key=lambda x: x.get("keyword_score", 0), reverse=True)

    n_ai = 0
    passed = []

    if run_ai:
        to_score    = stage12_passed[:ai_budget]
        rest        = stage12_passed[ai_budget:]
        budget_used = 0
        quota_died_mid_loop = False
        ai_score_log: list[dict] = []   # debug log

        for i, opp in enumerate(to_score):
            ai_s, ai_reason = ai_score_opportunity(opp)
            opp["ai_score"]  = ai_s
            opp["ai_reason"] = ai_reason
            budget_used += 1

            ai_score_log.append({
                "title": opp.get("title",""), "company": opp.get("company",""),
                "source": opp.get("source",""), "score": ai_s, "reason": ai_reason,
                "passed": ai_s >= AI_THRESHOLD,
            })

            if ai_s < AI_THRESHOLD:
                n_ai += 1
            else:
                opp["score"] = opp["ai_score"] or opp["keyword_score"]
                passed.append(opp)

            # Check if all providers are exhausted
            groq_all_gone = has_groq and (
                len(_groq_exhausted_models) + len(_groq_dead_models) >= len(GROQ_MODEL_CHAIN)
            )
            quota_exhausted = (
                groq_all_gone or
                (has_openrouter and _openrouter_daily_quota_exhausted)
            )
            if quota_exhausted:
                quota_died_mid_loop = True
                rest = to_score[i+1:] + rest
                break

            # Inter-call delay: 3s explicit + ~6s reasoning model response = ~9s/call.
            # Round-robin across 3 models: each model gets 1 call per 27s = 2.2/min.
            # 2.2 × 3,000 tok = 6,600 TPM per model — under the 8K free cap.
            # Total: 80 calls × 9s = ~12 min run time.
            if has_groq:
                time.sleep(3.0)
            elif has_openrouter:
                time.sleep(2.0)
            elif has_gemini:
                time.sleep(4.1)
            else:
                time.sleep(0.3)

        for opp in rest:
            opp["ai_score"]  = 0
            opp["ai_reason"] = ("Not AI-scored — provider quota exhausted"
                                if quota_died_mid_loop else
                                "Not AI-scored — daily budget reached")
            opp["score"]     = opp["keyword_score"]
            passed.append(opp)

        if rest:
            print(f"  [Filter] AI budget used: {budget_used}/{ai_budget} — "
                  f"{len(rest)} items passed on keyword score only"
                  f"{' (quota exhausted mid-run)' if quota_died_mid_loop else ''}")

        # Write AI score debug log — shows every score assigned this run
        # Useful for diagnosing "0 AI-filtered" (all scores ≥ threshold)
        try:
            ai_score_log.sort(key=lambda x: x["score"])
            with open("debug_ai_scores.json", "w") as f:
                json.dump(ai_score_log, f, indent=2)
            filtered_count = sum(1 for x in ai_score_log if not x["passed"])
            passed_count   = sum(1 for x in ai_score_log if x["passed"])
            if ai_score_log:
                scores = [x["score"] for x in ai_score_log]
                print(f"  [Filter] AI scores: min={min(scores)} max={max(scores)} "
                      f"avg={sum(scores)/len(scores):.1f} | "
                      f"passed={passed_count} filtered={filtered_count}")
        except Exception:
            pass
    else:
        for opp in stage12_passed:
            opp["ai_score"]  = 0
            opp["ai_reason"] = "AI not enabled"
            opp["score"]     = opp["keyword_score"]
            passed.append(opp)

    passed.sort(key=lambda x: (
        SOURCE_PRIORITY.get(x.get("source",""), 7),
        -(x.get("ai_score", 0) * 10 + x.get("keyword_score", 0)),
    ))

    print(f"  [Filter] {len(opportunities)} raw → "
          f"{n_hard} hard-excluded, {n_kw} keyword-miss, "
          f"{n_ai} AI-filtered → {len(passed)} passed")

    return passed
