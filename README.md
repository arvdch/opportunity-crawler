# 🔍 Opportunity Crawler

A free, automated daily job/internship/CTF crawler that sends alerts to your Telegram — no server needed, runs on GitHub Actions.

## What It Does

Every day at **8:00 AM IST**, this bot:
1. Scrapes Internshala, Unstop, Google News, NCS, CERT-In, LinkedIn
2. Filters results by your keywords (cybersecurity, CTF, govt internships, etc.)
3. Deduplicates — never sends the same opportunity twice
4. Sends a formatted Telegram message to your phone

You'll never miss an opportunity like APCSIP again.

---

## Setup Guide (30 minutes, one-time)

### Step 1 — Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Give it a name: e.g., `My Opportunity Crawler`
4. Give it a username: e.g., `my_opp_crawler_bot` (must end in `bot`)
5. BotFather gives you a **token** like: `123456789:ABCdef...`
6. **Save this token** — you'll need it in Step 4

### Step 2 — Get Your Telegram Chat ID

1. Start a conversation with your new bot (click the link BotFather gives you, press Start)
2. Send any message to the bot (e.g., "hello")
3. Open this URL in your browser (replace `YOUR_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
4. Look for `"chat":{"id":XXXXXXXX}` — that number is your **Chat ID**
5. **Save the Chat ID**

### Step 3 — Fork/Clone This Repo

```bash
# Option A: Fork on GitHub (recommended — enables GitHub Actions)
# Go to this repo → click Fork → it appears in your GitHub account

# Option B: Push to your own new repo
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/opportunity-crawler.git
git push -u origin main
```

### Step 4 — Add GitHub Secrets

Your bot token and chat ID must **never** be in your code. Store them as GitHub Secrets:

1. Go to your repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** and add these two:

| Secret Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | The token from BotFather (e.g., `123456789:ABCdef...`) |
| `TELEGRAM_CHAT_ID` | Your chat ID number (e.g., `987654321`) |

### Step 5 — Enable GitHub Actions

1. Go to your repo → **Actions** tab
2. If prompted, click **"I understand my workflows, enable them"**
3. Click on **Daily Opportunity Crawler** in the left sidebar
4. Click **Run workflow** → **Run workflow** (green button) to test immediately

You should get a Telegram message within 2-3 minutes!

---

## Customizing Keywords & Sources

Edit `config.yaml` — no Python knowledge needed:

```yaml
keywords:
  - cybersecurity internship
  - CTF
  - APCSIP
  - your custom keyword here

sources:
  internshala:
    enabled: true   # Set to false to disable
  linkedin:
    enabled: false  # Disable if getting blocked
```

After editing, commit and push:
```bash
git add config.yaml
git commit -m "Update keywords"
git push
```

---

## Running Locally (for testing)

```bash
# Install dependencies
pip install -r requirements.txt

# Set your credentials (Linux/Mac)
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"

# Run the crawler
python main.py
```

On Windows (Command Prompt):
```cmd
set TELEGRAM_BOT_TOKEN=your_token_here
set TELEGRAM_CHAT_ID=your_chat_id_here
python main.py
```

---

## Project Structure

```
opportunity-crawler/
├── main.py                    # Entry point — orchestrates everything
├── config.yaml                # Your keywords and source settings
├── requirements.txt           # Python dependencies
├── .gitignore
├── .github/
│   └── workflows/
│       └── daily.yml          # GitHub Actions cron job (runs daily at 8 AM IST)
└── crawler/
    ├── __init__.py
    ├── scraper.py             # Individual scrapers per source
    ├── filter.py              # Keyword matching and scoring
    ├── dedup.py               # Seen-URL store (prevents duplicate alerts)
    └── notifier.py            # Telegram Bot sender
```

---

## How Deduplication Works

- Every sent opportunity URL is saved to `seen_urls.json`
- In GitHub Actions, this file is preserved between daily runs using **Actions Cache**
- Entries older than 90 days are automatically cleaned up
- This means you'll **never get the same alert twice**, even across weeks

---

## Troubleshooting

**Bot token error:**
- Double-check the secret name is exactly `TELEGRAM_BOT_TOKEN` (case-sensitive)

**No messages received:**
- Make sure you sent at least one message to your bot first (to start the chat)
- Verify your Chat ID by visiting `https://api.telegram.org/botYOUR_TOKEN/getUpdates`

**LinkedIn scraper not working:**
- LinkedIn blocks scrapers aggressively. Disable it in `config.yaml` if needed.
- Google News RSS is a much more reliable alternative.

**Too many / too few alerts:**
- Increase `min_score` in `config.yaml` to reduce noise (1 = any match, 3 = stricter)
- Add or remove keywords to tune relevance

**Check logs:**
- Go to Actions → Daily Opportunity Crawler → click the latest run → click "crawl" job
- You'll see exactly what was scraped, filtered, and sent

---

## Schedule

The crawler runs at **02:30 UTC = 08:00 IST** every day.

To change the time, edit `.github/workflows/daily.yml`:
```yaml
- cron: "30 2 * * *"   # UTC time: minute hour * * *
```

UTC → IST converter: IST = UTC + 5:30, so 02:30 UTC = 08:00 IST

---

## Adding New Sources

To add a new scraper, edit `crawler/scraper.py`:

```python
def scrape_my_new_source(config: dict) -> list[dict]:
    results = []
    # ... your scraping logic ...
    results.append({
        "title": "Example Internship",
        "url": "https://example.com/internship/123",
        "company": "Example Corp",
        "description": "Short description",
        "source": "my_new_source",
    })
    return results

# Then add it to the SCRAPERS dict at the bottom of scraper.py:
SCRAPERS = {
    ...
    "my_new_source": scrape_my_new_source,
}
```

---

## Free Tier Usage

- GitHub Actions: ~5 minutes/day × 30 days = **150 minutes/month** (well within 2000 free minutes)
- Telegram Bot API: **completely free forever**
- Total cost: **₹0**
