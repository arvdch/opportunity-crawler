"""
main.py — Opportunity Crawler entry point.
"""

import sys
import os
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from crawler.scraper import run_all_scrapers
from crawler.filter import filter_opportunities
from crawler.dedup import is_seen, mark_seen_bulk, cleanup, stats
from crawler.notifier import send_batch


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[Config] ERROR: {path} not found!")
        sys.exit(1)
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    if not cfg.get("keywords"):
        print("[Config] WARNING: No keywords configured!")
    return cfg


def main():
    print("=" * 60)
    print("  Opportunity Crawler — Starting Daily Run")
    print("=" * 60)

    cfg               = load_config()
    keywords          = cfg.get("keywords", [])
    sources_cfg       = cfg.get("sources", {})
    notif_cfg         = cfg.get("notifications", {})
    dedup_cfg         = cfg.get("dedup", {})
    ai_cfg            = cfg.get("ai_matching", {})

    min_keyword_score = notif_cfg.get("min_keyword_score", 1)
    batch_size        = notif_cfg.get("batch_size", 50)
    retention_days    = dedup_cfg.get("retention_days", 90)

    # AI enabled if config says so AND at least one API key present
    has_groq       = bool(os.environ.get("GROQ_API_KEY", ""))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    has_gemini     = bool(os.environ.get("GEMINI_API_KEY", ""))
    has_anthropic  = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
    use_ai = ai_cfg.get("enabled", True) and (has_groq or has_openrouter or has_gemini or has_anthropic)

    print(f"[Config] {len(keywords)} keywords, {len(sources_cfg)} sources configured")
    if use_ai:
        provider = ("Groq" if has_groq else
                    "OpenRouter" if has_openrouter else
                    "Gemini" if has_gemini else "Claude")
        print(f"[Config] AI matching: ENABLED via {provider}")
    else:
        print("[Config] AI matching: DISABLED (set GROQ_API_KEY to enable — recommended, free tier)")

    # 1. Cleanup old dedup entries
    removed = cleanup(retention_days)
    if removed:
        print(f"[Dedup] Removed {removed} old entries (>{retention_days} days)")
    print(f"[Dedup] {stats()['total_seen']} URLs in seen-store")

    # 2. Scrape
    print("\n[Scraper] Starting scrape run...")
    all_opps = run_all_scrapers(sources_cfg)
    print(f"[Scraper] Total raw results: {len(all_opps)}")

    # 3. Filter
    print("\n[Filter] Running filter pipeline...")
    ai_budget = ai_cfg.get("daily_budget", 150)
    matched = filter_opportunities(
        all_opps,
        keywords,
        min_keyword_score=min_keyword_score,
        use_ai=use_ai,
        ai_budget=ai_budget,
    )

    # 4. Dedup
    new_opps = [o for o in matched if not is_seen(o.get("url", ""))]
    print(f"[Dedup] {len(new_opps)} new (not seen before)")

    # 5. Sort: AI score desc, then Hyderabad/Telangana first within same score
    def _sort_key(o):
        score = o.get("ai_score", 0) or 0
        loc = (o.get("location") or "").lower()
        is_hyd = 1 if any(c in loc for c in ("hyderabad", "secunderabad", "telangana")) else 0
        return (-score, -is_hyd)
    new_opps.sort(key=_sort_key)

    # 6. Send
    print(f"\n[Notifier] Sending {len(new_opps)} notifications...")
    sent, failed = send_batch(new_opps, batch_size=batch_size)
    print(f"[Notifier] Sent: {sent}  Failed: {failed}")

    # 7. Mark seen
    if new_opps:
        urls = [o["url"] for o in new_opps if o.get("url")]
        mark_seen_bulk(urls, opportunities=new_opps)
        print(f"[Dedup] Marked {len(urls)} URLs as seen")

    print("\n" + "=" * 60)
    print("  Crawl complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
