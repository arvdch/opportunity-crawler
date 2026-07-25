"""
notifier.py — Telegram Bot notification sender.
"""

import os
import time
import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

SOURCE_EMOJI = {
    "internshala":    "🎓",
    "unstop":         "🏆",
    "linkedin":       "💼",
    "wellfound":      "🚀",
    "hackerearth":    "⚡",
    "govt_portals":   "🏛️",
    "github":         "🐙",
    "ncs":            "🏛️",
    "google_news":    "📰",
    "certifications": "🔐",
    "rss":            "📡",
    "unknown":        "🔍",
}


def _get_credentials() -> tuple[str, str]:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise EnvironmentError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.\n"
            "Set them as env vars locally or GitHub Secrets in CI."
        )
    return token, chat_id


def _escape_md(text: str) -> str:
    """
    Escape MarkdownV2 special chars for plain text spans only.
    Never call this on URLs — they go into link syntax raw.
    """
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _format_message(opp: dict) -> str:
    source   = opp.get("source", "unknown").lower()
    emoji    = SOURCE_EMOJI.get(source, SOURCE_EMOJI["unknown"])
    title    = opp.get("title", "Untitled")
    url      = opp.get("url", "")
    desc     = opp.get("description", "")
    company  = opp.get("company", "")
    deadline = opp.get("deadline", "")   # used as "date posted" from LinkedIn
    location = opp.get("location", "")
    ai_score = opp.get("ai_score", 0)
    ai_reason= opp.get("ai_reason", "")
    kw_score = opp.get("keyword_score", opp.get("score", 0))

    if desc and len(desc) > 180:
        desc = desc[:180].rsplit(" ", 1)[0] + "…"

    lines = [f"{emoji} *{_escape_md(title)}*"]
    if company:  lines.append(f"🏢 {_escape_md(company)}")
    if location: lines.append(f"📍 {_escape_md(location)}")
    if desc:     lines.append(f"_{_escape_md(desc)}_")

    # Date posted — shown as "📅 Posted: ..." for LinkedIn datetime strings
    # and "⏰ Deadline: ..." for competitions/programs
    if deadline:
        deadline_str = str(deadline).strip()
        # LinkedIn gives ISO datetime like "2026-06-10T00:00:00.000Z" or "2 weeks ago"
        if "T" in deadline_str and deadline_str[0].isdigit():
            # Parse ISO date to readable format
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                days_ago = (now - dt).days
                if days_ago == 0:
                    age = "Today"
                elif days_ago == 1:
                    age = "Yesterday"
                elif days_ago <= 30:
                    age = f"{days_ago} days ago"
                elif days_ago <= 60:
                    age = "1 month ago"
                else:
                    age = f"{days_ago // 30} months ago"
                formatted = dt.strftime("%d %b %Y")
                lines.append(f"📅 Posted: {_escape_md(formatted)} \\({_escape_md(age)}\\)")
            except Exception:
                lines.append(f"📅 Posted: {_escape_md(deadline_str)}")
        elif any(x in deadline_str.lower() for x in ["ago", "week", "day", "month", "hour"]):
            lines.append(f"📅 Posted: {_escape_md(deadline_str)}")
        else:
            # Competitions / programs show as deadline
            lines.append(f"⏰ Deadline/Start: {_escape_md(deadline_str[:40])}")

    if ai_score > 0:
        dot = "🟢" if ai_score >= 7 else ("🟡" if ai_score >= 5 else "🔴")
        lines.append(f"{dot} AI: *{ai_score}/10* — {_escape_md(ai_reason)}")
    else:
        lines.append(f"{'⭐' * min(kw_score, 5)} Keyword score: {kw_score}")

    lines.append(f"🔗 [View Opportunity]({url})")
    lines.append(f"_Source: {_escape_md(source)}_")

    return "\n".join(lines)


def _post(token: str, chat_id: str, text: str, retries: int = 2) -> bool:
    """
    POST one Telegram message.
    On 429: waits retry_after seconds, then retries (counts as an attempt).
    On other errors: retries up to `retries` times with 2s backoff.
    """
    url = TELEGRAM_API.format(token=token)
    attempt = 0
    while attempt <= retries:
        try:
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text,
                      "parse_mode": "MarkdownV2",
                      "disable_web_page_preview": False},
                timeout=15,
            )
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 5)
                print(f"  [Telegram] Rate limited — waiting {wait}s")
                time.sleep(wait)
                attempt += 1
                continue
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            attempt += 1
            if attempt > retries:
                print(f"  [Telegram] Failed after {retries+1} attempts: {e}")
                return False
            time.sleep(2)
    return False


def send_batch(opportunities: list[dict], batch_size: int = 15) -> tuple[int, int]:
    """Send batch to Telegram. Returns (sent, failed)."""
    token, chat_id = _get_credentials()

    if not opportunities:
        _post(token, chat_id, "✅ Daily crawl complete — no new opportunities today\\.")
        return 0, 0

    total = len(opportunities)
    _post(token, chat_id,
          f"🚀 *Daily Opportunity Report*\n"
          f"Found *{total}* new opportunit{'y' if total == 1 else 'ies'}\\!")
    time.sleep(1)

    sent = failed = 0
    for i, opp in enumerate(opportunities[:batch_size]):
        if _post(token, chat_id, _format_message(opp)):
            sent += 1
        else:
            failed += 1
        if i < min(total, batch_size) - 1:
            time.sleep(0.5)

    if total > batch_size:
        _post(token, chat_id,
              f"ℹ️ \\+{total - batch_size} more not shown\\. "
              f"Raise `batch_size` in config\\.yaml to see all\\.")

    return sent, failed
