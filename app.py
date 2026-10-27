#!/usr/bin/env python3
"""
OCD — Open Current Digest
Plain news reader. Scrapes RSS, summarizes via GLM 5.3, serves minimal HTML.
"""

import json
import os
import time
import threading
import sqlite3
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

import feedparser
import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────
MORDIEM_API_KEY = os.environ.get("MORDIEM_API_KEY", "")
MORDIEM_BASE_URL = "https://api.mordiem.com/api/v1"
MODEL = "z-ai-glm-5-3-flash"
FALLBACK_MODEL = "z-ai-glm-5-3"
DB_PATH = "/opt/ocd/articles.db"
FETCH_INTERVAL = 600  # 10 minutes
PORT = 8099

# RSS feeds — major news sources
FEEDS = [
    ("BBC — World",            "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC — Business",         "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC — Tech",             "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("NPR",                    "https://feeds.npr.org/1001/rss.xml"),
    ("Al Jazeera",             "https://www.aljazeera.com/xml/rss/all.xml"),
    ("AP News",                "https://feedx.net/rss/ap.xml"),
    ("CNN",                    "http://rss.cnn.com/rss/edition.rss"),
    ("NYT",                    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("The Guardian",           "https://www.theguardian.com/world/rss"),
    ("Bloomberg",              "https://feeds.bloomberg.com/markets/news.rss"),
    ("Ars Technica",           "https://feeds.arstechnica.com/arstechnica/index"),
    ("TechCrunch",             "https://techcrunch.com/feed/"),
    ("Hacker News",            "https://hnrss.org/best"),
    ("The Verge",              "https://www.theverge.com/rss/index.xml"),
    ("Wired",                  "https://www.wired.com/feed/rss"),
]

# ── Database ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            link TEXT,
            published TEXT,
            fetched_at TEXT,
            summary TEXT,
            raw_description TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON articles(fetched_at DESC)")
    # Visit tracking tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            path TEXT NOT NULL,
            user_agent TEXT,
            started_at TEXT NOT NULL,
            duration_seconds REAL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_started ON visits(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_session ON visits(session_id)")
    conn.commit()
    conn.close()

def article_exists(article_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def save_article(article_id, source, title, link, published, summary, raw_desc):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO articles (id, source, title, link, published, fetched_at, summary, raw_description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (article_id, source, title, link, published,
          datetime.now(timezone.utc).isoformat(), summary, raw_desc))
    conn.commit()
    conn.close()

def get_articles(limit=100, source_filter=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if source_filter:
        cur = conn.execute(
            "SELECT * FROM articles WHERE source = ? ORDER BY fetched_at DESC LIMIT ?",
            (source_filter, limit))
    else:
        cur = conn.execute(
            "SELECT * FROM articles ORDER BY fetched_at DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_sources():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT DISTINCT source FROM articles ORDER BY source")
    sources = [r[0] for r in cur.fetchall()]
    conn.close()
    return sources

def get_article_count():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM articles")
    count = cur.fetchone()[0]
    conn.close()
    return count

# ── Visit Tracking ──────────────────────────────────────────────────────
def record_visit(session_id, ip, path, user_agent):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO visits (session_id, ip, path, user_agent, started_at)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, ip, path, user_agent, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def update_visit_duration(session_id, duration):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE visits SET duration_seconds = ? WHERE session_id = ?
    """, (duration, session_id))
    conn.commit()
    conn.close()

def get_stats_data(days=30):
    """Get chart data for last N days, card stats for last 24h, and all-time total views."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Daily unique visitors (by IP) — for charts (30d)
    daily_visitors = conn.execute("""
        SELECT DATE(started_at) as day, COUNT(DISTINCT ip) as unique_visitors
        FROM visits
        WHERE started_at >= ? AND path != '/stats' AND path != '/api/heartbeat'
        GROUP BY DATE(started_at)
        ORDER BY day
    """, (cutoff,)).fetchall()

    # Daily average duration — for charts (30d)
    daily_duration = conn.execute("""
        SELECT DATE(started_at) as day, AVG(duration_seconds) as avg_duration
        FROM visits
        WHERE started_at >= ? AND duration_seconds > 0 AND path != '/stats' AND path != '/api/heartbeat'
        GROUP BY DATE(started_at)
        ORDER BY day
    """, (cutoff,)).fetchall()

    # All-time total views
    alltime_total = conn.execute("""
        SELECT COUNT(*) as total_visits
        FROM visits
        WHERE path != '/stats' AND path != '/api/heartbeat'
    """).fetchone()

    # Last 24h stats
    last_24h = conn.execute("""
        SELECT COUNT(*) as visits,
               COUNT(DISTINCT ip) as unique_ips,
               AVG(CASE WHEN duration_seconds > 0 THEN duration_seconds END) as avg_duration
        FROM visits
        WHERE started_at >= ? AND path != '/stats' AND path != '/api/heartbeat'
    """, (cutoff_24h,)).fetchone()

    conn.close()

    return {
        "daily_visitors": [dict(r) for r in daily_visitors],
        "daily_duration": [dict(r) for r in daily_duration],
        "alltime_total": dict(alltime_total) if alltime_total else {"total_visits": 0},
        "last_24h": dict(last_24h) if last_24h else {},
    }

# ── Summarizer ──────────────────────────────────────────────────────────
def summarize(title, text):
    """Ask GLM 5.3 to produce a 3-sentence summary."""
    if not MORDIEM_API_KEY:
        return "No API key configured."
    if not text or len(text.strip()) < 20:
        text = title

    prompt = (
        "Summarize the following news article in exactly 3 sentences. "
        "Sentence 1: a one-sentence introduction that sets the context. "
        "Sentence 2: the core content or key facts. "
        "Sentence 3: the conclusion, impact, or what comes next. "
        "Be concise and factual. No bullet points, no labels, just three plain sentences.\n\n"
        f"Title: {title}\n\nArticle:\n{text[:4000]}"
    )

    models_to_try = [MODEL, FALLBACK_MODEL]
    for model in models_to_try:
        try:
            resp = requests.post(
                f"{MORDIEM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {MORDIEM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.3,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            result = (msg.get("content") or "").strip()
            if result:
                print(f"    [summarize ok via {model}] {len(result)} chars")
                return result
            else:
                print(f"    [summarize empty content via {model}] trying next...")
        except Exception as e:
            print(f"    [summarize error via {model}] {e}")
        time.sleep(2)
    return ""

# ── Scraper ─────────────────────────────────────────────────────────────
def extract_article_text(url):
    """Best-effort extraction of article body text from URL."""
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form"]):
            tag.decompose()

        article = soup.find("article") or soup.find("main") or soup.body
        if article:
            paragraphs = article.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30)
            if len(text) > 200:
                return text
        return soup.get_text(separator=" ", strip=True)[:5000]
    except Exception as e:
        print(f"  [extract error] {url}: {e}")
        return ""

def fetch_feeds():
    """Scrape all RSS feeds, summarize new articles."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching feeds...")
    new_count = 0

    for source_name, feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            entries = feed.entries[:10]
            print(f"  {source_name}: {len(entries)} entries")

            for entry in entries:
                link = entry.get("link", "")
                title = entry.get("title", "Untitled")
                article_id = hashlib.sha256(link.encode()).hexdigest()[:16]

                if article_exists(article_id):
                    continue

                published = entry.get("published", entry.get("updated", ""))
                raw_desc = entry.get("summary", entry.get("description", ""))

                if raw_desc and "<" in raw_desc:
                    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text(strip=True)

                full_text = extract_article_text(link) if link else ""
                if len(full_text) > 200:
                    text_for_summary = full_text
                elif raw_desc and len(raw_desc) > 50:
                    text_for_summary = raw_desc
                else:
                    text_for_summary = f"{title}. {raw_desc}" if raw_desc else title

                summary = summarize(title, text_for_summary)
                save_article(article_id, source_name, title, link, published, summary, raw_desc)
                new_count += 1
                print(f"    + {title[:60]}")

                time.sleep(1)

        except Exception as e:
            print(f"  [feed error] {source_name}: {e}")

    total = get_article_count()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done. {new_count} new articles. {total} total.\n")

def worker():
    """Background worker: fetch feeds every FETCH_INTERVAL seconds."""
    while True:
        try:
            fetch_feeds()
        except Exception as e:
            print(f"[worker error] {e}")
        time.sleep(FETCH_INTERVAL)

# ── HTML Rendering ──────────────────────────────────────────────────────
def escape_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

TRACKING_JS = '''
<script>
(function(){
    var sid = sessionStorage.getItem('ocd_sid');
    if (!sid) { sid = Math.random().toString(36).substr(2,12) + Date.now().toString(36); sessionStorage.setItem('ocd_sid', sid); }
    var start = Date.now();
    // Register visit
    fetch('/api/visit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sid: sid, path: location.pathname})});
    // Send duration on page leave
    function sendDuration() {
        var dur = Math.round((Date.now() - start) / 1000);
        if (dur > 0) { navigator.sendBeacon('/api/heartbeat', JSON.stringify({sid: sid, duration: dur})); }
    }
    window.addEventListener('visibilitychange', function(){ if (document.visibilityState === 'hidden') sendDuration(); });
    window.addEventListener('beforeunload', sendDuration);
    // Also send periodic heartbeats for long sessions
    setInterval(function(){
        var dur = Math.round((Date.now() - start) / 1000);
        fetch('/api/heartbeat', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sid: sid, duration: dur})});
    }, 30000);
})();
</script>
'''

def render_page(articles, sources, current_source=None):
    source_links = []
    for s in sources:
        se = escape_html(s)
        if s == current_source:
            source_links.append(f'<b>{se}</b>')
        else:
            source_links.append(f'<a href="/?source={quote(s)}">{se}</a>')

    nav = " &middot; ".join(source_links)
    if current_source:
        nav = f'<a href="/">All</a> &middot; ' + nav

    article_html = ""
    for a in articles:
        pub = a.get("published", "")
        if pub:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub)
                pub = dt.strftime("%b %d, %H:%M")
            except Exception:
                pub = pub[:25]

        title_e = escape_html(a['title'])
        source_e = escape_html(a['source'])
        summary_text = a['summary'] or a.get('raw_description', '') or ''
        summary_e = escape_html(summary_text)
        link_e = escape_html(a['link'])

        aid = escape_html(a['id'])
        article_html += f'''
        <div id="{aid}" style="margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid #ddd">
            <div style="font-size:18px;font-weight:bold;margin-bottom:4px">
                <a href="#{aid}" style="color:#ccc;text-decoration:none;margin-right:6px;font-size:14px" title="Link to this article">#</a><a href="{link_e}" target="_blank" rel="noopener" style="color:#000;text-decoration:none">{title_e}</a>
            </div>
            <div style="font-size:12px;color:#888;margin-bottom:8px">{source_e} &middot; {pub}</div>
            <div style="font-size:15px;line-height:1.6;color:#222">{summary_e}</div>
        </div>
        '''

    count = get_article_count()
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OCD &mdash; Open Current Digest</title>
    <meta name="description" content="Headlines and a short summary from 16 news sources, on one plain page. Refreshed every 10 minutes.">
    <link rel="canonical" href="https://opencurrentdigest.com/">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="Open Current Digest">
    <meta property="og:title" content="Open Current Digest: news, summarized">
    <meta property="og:description" content="Headlines and a short summary from 16 sources, on one plain page.">
    <meta property="og:url" content="https://opencurrentdigest.com/">
    <meta property="og:image" content="https://opencurrentdigest.com/og.png">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="Open Current Digest: news, summarized">
    <meta name="twitter:description" content="Headlines and a short summary from 16 sources, on one plain page.">
    <meta name="twitter:image" content="https://opencurrentdigest.com/og.png">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        html {{ scroll-behavior: smooth; }}
        :target {{ animation: highlight 2s ease; }}
        @keyframes highlight {{ 0% {{ background-color: #ffffcc; }} 100% {{ background-color: transparent; }} }}
        body {{ font-family: Georgia, "Times New Roman", serif; background:#fff; color:#000; max-width:720px; margin:0 auto; padding:20px; }}
        a {{ color:#000; }}
        a:hover {{ color:#555; }}
        a[href^="#"]:hover {{ color:#000; }}
    </style>
</head>
<body>
    <div style="border-bottom:2px solid #000;padding-bottom:12px;margin-bottom:20px">
        <h1 style="font-size:28px;letter-spacing:1px">OCD</h1>
        <div style="font-size:13px;color:#666">Open Current Digest &mdash; news, summarized</div>
    </div>
    <div style="font-size:13px;margin-bottom:24px;line-height:2">{nav}</div>
    {article_html if articles else '<p style="color:#888">No articles yet. Worker is fetching &mdash; check back in a minute.</p>'}
    <div style="margin-top:40px;padding-top:12px;border-top:1px solid #ddd;font-size:11px;color:#aaa">
        OCD &middot; {count} articles indexed &middot; refreshes every 10 min
    </div>
    {TRACKING_JS}
</body>
</html>'''


def render_stats_page(stats):
    """Render the /stats analytics dashboard."""
    daily_visitors = stats["daily_visitors"]
    daily_duration = stats["daily_duration"]
    alltime = stats["alltime_total"]
    last_24h = stats["last_24h"]

    # Build data for charts
    visitor_labels = json.dumps([d["day"] for d in daily_visitors])
    visitor_data = json.dumps([d["unique_visitors"] for d in daily_visitors])

    duration_labels = json.dumps([d["day"] for d in daily_duration])
    duration_data = json.dumps([round(d["avg_duration"], 1) if d["avg_duration"] else 0 for d in daily_duration])

    alltime_views = alltime.get("total_visits", 0)
    last24_visits = last_24h.get("visits", 0)
    last24_unique = last_24h.get("unique_ips", 0)
    avg_dur = last_24h.get("avg_duration") or 0
    avg_dur_fmt = f"{int(avg_dur // 60)}m {int(avg_dur % 60)}s" if avg_dur > 60 else f"{int(avg_dur)}s"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OCD &mdash; Stats</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family: Georgia, "Times New Roman", serif; background:#fff; color:#000; max-width:900px; margin:0 auto; padding:20px; }}
        a {{ color:#000; }}
        .stat-cards {{ display:flex; gap:20px; margin-bottom:30px; flex-wrap:wrap; }}
        .stat-card {{ flex:1; min-width:150px; padding:20px; border:1px solid #ddd; text-align:center; }}
        .stat-card .number {{ font-size:32px; font-weight:bold; }}
        .stat-card .label {{ font-size:13px; color:#888; margin-top:4px; }}
        .chart-container {{ margin-bottom:40px; padding:20px; border:1px solid #ddd; }}
        .chart-container h2 {{ font-size:18px; margin-bottom:15px; }}
        canvas {{ max-height:300px; }}
    </style>
</head>
<body>
    <div style="border-bottom:2px solid #000;padding-bottom:12px;margin-bottom:20px">
        <h1 style="font-size:28px;letter-spacing:1px"><a href="/" style="text-decoration:none">OCD</a> &mdash; Stats</h1>
        <div style="font-size:13px;color:#666">Visitor analytics &middot; last 30 days</div>
    </div>

    <div class="stat-cards">
        <div class="stat-card">
            <div class="number">{alltime_views}</div>
            <div class="label">Total Views (All Time)</div>
        </div>
        <div class="stat-card">
            <div class="number">{last24_visits}</div>
            <div class="label">Views (24h)</div>
        </div>
        <div class="stat-card">
            <div class="number">{last24_unique}</div>
            <div class="label">Unique Visitors (24h)</div>
        </div>
        <div class="stat-card">
            <div class="number">{avg_dur_fmt}</div>
            <div class="label">Avg Time on Site (24h)</div>
        </div>
    </div>

    <div class="chart-container">
        <h2>Unique Visitors Per Day</h2>
        <canvas id="visitorsChart"></canvas>
    </div>

    <div class="chart-container">
        <h2>Average Time on Site (seconds)</h2>
        <canvas id="durationChart"></canvas>
    </div>

    <div style="margin-top:40px;padding-top:12px;border-top:1px solid #ddd;font-size:11px;color:#aaa">
        <a href="/">&larr; Back to news</a>
    </div>

    <script>
    new Chart(document.getElementById('visitorsChart'), {{
        type: 'bar',
        data: {{
            labels: {visitor_labels},
            datasets: [{{
                label: 'Unique Visitors',
                data: {visitor_data},
                backgroundColor: 'rgba(0,0,0,0.7)',
                borderColor: '#000',
                borderWidth: 1
            }}]
        }},
        options: {{
            responsive: true,
            scales: {{
                y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }},
                x: {{ ticks: {{ maxRotation: 45 }} }}
            }},
            plugins: {{ legend: {{ display: false }} }}
        }}
    }});

    new Chart(document.getElementById('durationChart'), {{
        type: 'line',
        data: {{
            labels: {duration_labels},
            datasets: [{{
                label: 'Avg Duration (s)',
                data: {duration_data},
                borderColor: '#000',
                backgroundColor: 'rgba(0,0,0,0.05)',
                fill: true,
                tension: 0.3
            }}]
        }},
        options: {{
            responsive: true,
            scales: {{
                y: {{ beginAtZero: true }},
                x: {{ ticks: {{ maxRotation: 45 }} }}
            }},
            plugins: {{ legend: {{ display: false }} }}
        }}
    }});
    </script>
</body>
</html>'''


# ── HTTP Server ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _get_client_ip(self):
        # Check X-Forwarded-For (nginx proxy), fall back to direct IP
        forwarded = self.headers.get('X-Forwarded-For')
        if forwarded:
            return forwarded.split(',')[0].strip()
        return self.client_address[0]

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/" or parsed.path == "":
            source_filter = params.get("source", [None])[0]
            articles = get_articles(limit=100, source_filter=source_filter)
            sources = get_sources()
            html = render_page(articles, sources, source_filter)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif parsed.path == "/stats":
            stats = get_stats_data(days=30)
            html = render_stats_page(stats)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        elif parsed.path == "/og.png":
            try:
                with open("/opt/ocd/og.png", "rb") as img:
                    data = img.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))

        if parsed.path == "/api/visit":
            try:
                body = json.loads(self.rfile.read(content_length))
                sid = body.get("sid", "")
                path = body.get("path", "/")
                ip = self._get_client_ip()
                ua = self.headers.get("User-Agent", "")
                record_visit(sid, ip, path, ua)
                self.send_response(204)
                self.end_headers()
            except Exception:
                self.send_response(400)
                self.end_headers()

        elif parsed.path == "/api/heartbeat":
            try:
                body = json.loads(self.rfile.read(content_length))
                sid = body.get("sid", "")
                duration = body.get("duration", 0)
                if sid and duration > 0:
                    update_visit_duration(sid, min(duration, 7200))  # cap at 2h
                self.send_response(204)
                self.end_headers()
            except Exception:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


# ── Main ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"OCD starting on port {PORT}...")
    init_db()

    # Start background worker
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on http://0.0.0.0:{PORT}")
    server.serve_forever()
