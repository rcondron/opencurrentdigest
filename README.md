# OCD — Open Current Digest

**[opencurrentdigest.com](https://opencurrentdigest.com)** — news, summarized.

Headlines and a short summary from 16 news sources, on one plain page. Refreshed every 10 minutes.

## What it does

- Scrapes RSS feeds from 16 major news sources (BBC, Bloomberg, NYT, TechCrunch, The Verge, etc.)
- Summarizes each article into 3 concise sentences using GLM 5.3 via the [Mordiem API](https://api.mordiem.com)
- Serves a minimal, fast-loading HTML page — no JavaScript frameworks, no bloat
- Tracks basic visitor analytics (page views, session duration)
- Filter by source, link directly to any article via `#article-id` hash

## Stack

- **Python 3** — single-file app (`app.py`), stdlib `http.server`
- **SQLite** — article storage + visit tracking
- **feedparser** — RSS parsing
- **requests + BeautifulSoup** — article text extraction
- **Mordiem API** — AI summarization (GLM 5.3 Flash with full fallback)
- **nginx** — reverse proxy with Let's Encrypt SSL
- **systemd** — process management

## Setup

```bash
# Install dependencies
pip3 install -r requirements.txt

# Configure API key
cp .env.example .env
# Edit .env with your Mordiem API key

# Initialize the database (happens automatically on first run)
# Or manually: sqlite3 articles.db < schema.sql

# Run
python3 app.py
```

## Deployment

Runs on port 8099 behind nginx. See `ocd.service` for the systemd unit and `nginx/` for the site config.

```bash
# Install service
sudo cp ocd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocd

# Always restart via systemd (never run manually with nohup!)
sudo systemctl restart ocd
```

## Linking to articles

Each article has a stable hash ID. Link directly with:

```
https://opencurrentdigest.com#<article-id>
```

The page will smooth-scroll to the article and briefly highlight it.

## Sources

AP News · Al Jazeera · Ars Technica · BBC (World, Business, Tech) · Bloomberg · CNN · Hacker News · NPR · NYT · TechCrunch · The Guardian · The Verge · Wired

## License

MIT
