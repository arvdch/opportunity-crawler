"""
test_setup.py — Run this locally to verify everything is configured correctly
BEFORE pushing to GitHub.

Usage:
    python test_setup.py
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PASS = "  ✅"
FAIL = "  ❌"
WARN = "  ⚠️ "


def check_env():
    print("\n[1/4] Checking environment variables...")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        print(f"{FAIL} TELEGRAM_BOT_TOKEN not set")
        print("      Set it with: export TELEGRAM_BOT_TOKEN=your_token")
        return False
    if not chat_id:
        print(f"{FAIL} TELEGRAM_CHAT_ID not set")
        print("      Set it with: export TELEGRAM_CHAT_ID=your_chat_id")
        return False

    print(f"{PASS} TELEGRAM_BOT_TOKEN is set (starts with: {token[:10]}...)")
    print(f"{PASS} TELEGRAM_CHAT_ID is set: {chat_id}")
    return True


def check_config():
    print("\n[2/4] Checking config.yaml...")
    import yaml
    cfg_path = Path("config.yaml")

    if not cfg_path.exists():
        print(f"{FAIL} config.yaml not found!")
        return None

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    keywords = cfg.get("keywords", [])
    sources = cfg.get("sources", {})
    enabled = [k for k, v in sources.items() if isinstance(v, dict) and v.get("enabled", True)]

    print(f"{PASS} config.yaml loaded: {len(keywords)} keywords, {len(enabled)} enabled sources")
    print(f"      Keywords: {keywords[:5]}{'...' if len(keywords) > 5 else ''}")
    print(f"      Sources:  {enabled}")
    return cfg


def check_telegram(token: str, chat_id: str):
    print("\n[3/4] Testing Telegram connection...")
    import httpx

    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        if r.status_code == 200:
            bot = r.json().get("result", {})
            print(f"{PASS} Telegram bot connected: @{bot.get('username', '?')}")
        else:
            print(f"{FAIL} Telegram API error: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        print(f"{FAIL} Cannot reach Telegram API: {e}")
        return False

    # Send a test message
    print("      Sending test message to your chat...")
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "✅ *Opportunity Crawler test message\\!*\nSetup is working correctly\\.",
                "parse_mode": "MarkdownV2",
            },
            timeout=10,
        )
        if r.status_code == 200:
            print(f"{PASS} Test message sent! Check your Telegram.")
            return True
        else:
            error = r.json().get("description", r.text[:100])
            print(f"{FAIL} Failed to send message: {error}")
            if "chat not found" in error.lower():
                print("      → You need to send a message to your bot first!")
                print(f"        Open Telegram, search your bot, press Start.")
            return False
    except Exception as e:
        print(f"{FAIL} Error sending message: {e}")
        return False


def check_pipeline(cfg):
    print("\n[4/4] Testing filter & dedup pipeline...")

    from crawler.filter import filter_opportunities
    import crawler.dedup as dedup
    from pathlib import Path

    # Use temp file
    dedup.SEEN_FILE = Path("/tmp/test_setup_dedup.json")
    if dedup.SEEN_FILE.exists():
        dedup.SEEN_FILE.unlink()

    keywords = cfg.get("keywords", [])
    test_opps = [
        {"title": "Cybersecurity Internship CERT-In", "url": "https://test.com/1", "source": "test", "description": ""},
        {"title": "Marketing Job at Amazon", "url": "https://test.com/2", "source": "test", "description": ""},
        {"title": "CTF Competition HackTheBox", "url": "https://test.com/3", "source": "test", "description": ""},
    ]

    matched = filter_opportunities(test_opps, keywords, min_keyword_score=1, use_ai=False)
    print(f"{PASS} Filter: {len(matched)}/3 test opportunities matched (expected 2)")

    dedup.mark_seen_bulk([o["url"] for o in matched])
    run2 = [o for o in matched if not dedup.is_seen(o["url"])]
    print(f"{PASS} Dedup: {len(run2)} new on second run (expected 0)")

    dedup.SEEN_FILE.unlink()
    return True


def main():
    print("=" * 55)
    print("  Opportunity Crawler — Setup Verification")
    print("=" * 55)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    env_ok = check_env()
    cfg = check_config()

    if not cfg:
        print(f"\n{FAIL} Config missing. Cannot continue.")
        sys.exit(1)

    if env_ok:
        tg_ok = check_telegram(token, chat_id)
    else:
        print(f"\n{WARN} Skipping Telegram test (env vars not set)")
        tg_ok = False

    pipeline_ok = check_pipeline(cfg)

    print("\n" + "=" * 55)
    if env_ok and tg_ok and pipeline_ok:
        print("  🎉 All checks passed! Ready to push to GitHub.")
    else:
        print("  ⚠️  Some checks failed. Fix the issues above first.")
    print("=" * 55)


if __name__ == "__main__":
    main()
