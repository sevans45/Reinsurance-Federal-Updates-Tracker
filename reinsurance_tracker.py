#!/usr/bin/env python3
"""
Reinsurance Federal Updates Tracker
Collects bills, regulations, and agency updates related to reinsurance from:
- Congress.gov API
- Federal Register API
- Agency RSS feeds

Automatically populates a Google Sheet for team viewing.
Runs on schedule via GitHub Actions. Also sends email notifications.
"""

import os
import json
import requests
import feedparser
import sqlite3
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Google Sheets API
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None

# ============================================================================
# CONFIGURATION
# ============================================================================

# Congress.gov API
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY", "")
CONGRESS_BASE_URL = "https://api.congress.gov/v3"

# Federal Register API
FED_REG_BASE_URL = "https://www.federalregister.gov/api/v1"

# RAA 2026 Federal Affairs Goals — Key Terms & Legislative Priorities
REINSURANCE_KEYWORDS = [
    # TRIP (Terrorism Risk Insurance Program)
    "TRIP",
    "terrorism risk insurance",
    "terrorist attack",
    
    # NFIP (National Flood Insurance Program)
    "NFIP",
    "National Flood Insurance Program",
    "flood insurance",
    "continuous coverage",
    "private flood insurance",
    "Biggert-Waters",
    "Grimm-Waters",
    
    # EXIM (Export-Import Bank)
    "Export-Import Bank",
    "EXIM",
    "EXIM reinsurance",
    
    # Core reinsurance/capital/risk transfer
    "reinsurance",
    "retrocession",
    "capital requirements",
    "Basel III",
    "credit risk transfer",
    "CRT program",
    "FHFA",
    "Fannie Mae",
    "Freddie Mac",
    "solvency capital",
    "cat bond",
    "catastrophe bond",
    
    # Natural disasters & resilience
    "Community Disaster Resilience Zone",
    "CDRZ",
    "mitigation",
    "community resilience",
    "disaster recovery",
    "post-disaster recovery",
    "public assistance",
    "FEMA",
    
    # Litigation finance
    "litigation finance",
    "litigation funding",
    "lawsuit abuse",
    
    # Cannabis insurance
    "cannabis insurance",
    "cannabis banking",
    "state-legalized cannabis",
    
    # Cyber, data breach, ransomware, AI
    "cyber insurance",
    "cyber reinsurance",
    "data breach",
    "ransomware",
    "artificial intelligence",
    "AI insurance",
    
    # ESG
    "ESG",
    "environmental social governance",
    
    # Crop insurance
    "crop insurance",
    "agricultural insurance",
    
    # Housing/mortgage finance
    "housing finance",
    "mortgage",
    "mortgage insurance",
    "FHA",
    "GSE",
    "government sponsored enterprise",
    
    # Federal (re)insurance programs for disasters
    "federal catastrophe program",
    "federal reinsurance",
    "state insurance",
    
    # Banking/credit risk
    "credit risk transfer",
    "surety bond",
    "SBA",
    "bank reinsurance",
    
    # Federal Insurance Office & Trade
    "Federal Insurance Office",
    "FIO",
    "international insurance",
    "trade agreements",
    "trade barriers",
]

# Agencies to monitor for regulations
AGENCIES = [
    "Federal Reserve System",
    "Treasury Department",
    "Federal Deposit Insurance Corporation",
    "Office of the Comptroller of the Currency",
    "Federal Emergency Management Agency",
    "Department of Housing and Urban Development",
    "Federal Housing Finance Agency",
    "National Oceanic and Atmospheric Administration",
    "National Aeronautics and Space Administration",
    "National Science Foundation",
    "U.S. Geological Survey",
    "Small Business Administration",
]

# RAA key committees & members to track for legislation
KEY_COMMITTEES = [
    "House Financial Services",
    "Senate Banking",
    "House Ways and Means",
    "Senate Finance",
]

# Key RAA coalition names
RAA_COALITIONS = [
    "Americans for Litigation Tax Fairness",
    "Business coalition",
    "EXIM Coalition",
    "Insurance trades",
    "NAIC cannabis coalition",
    "Crop Insurance Coalition",
]

# Email/Slack config
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
SLACK_ENABLED = os.getenv("SLACK_ENABLED", "false").lower() == "true"
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

# Google Sheets config
SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "false").lower() == "true"
SHEETS_SPREADSHEET_ID = os.getenv("SHEETS_SPREADSHEET_ID", "")
SHEETS_WORKSHEET_NAME = "Federal Updates"
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

# Database for deduplication
DB_FILE = "reinsurance_updates.db"

# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_database():
    """Create SQLite database for tracking seen updates."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS updates (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            url TEXT,
            date_published TEXT,
            date_added TEXT,
            keywords TEXT
        )
    ''')
    columns = {row[1] for row in c.execute("PRAGMA table_info(updates)")}
    if "sheets_exported_at" not in columns:
        c.execute("ALTER TABLE updates ADD COLUMN sheets_exported_at TEXT")
    conn.commit()
    return conn

def is_duplicate(conn, update_id):
    """Check if we've already recorded this update."""
    c = conn.cursor()
    c.execute("SELECT 1 FROM updates WHERE id = ?", (update_id,))
    return c.fetchone() is not None

def add_update(conn, update_id, source, title, url, date_published, matched_keywords):
    """Store a new update in database."""
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO updates 
        (id, source, title, url, date_published, date_added, keywords)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        update_id,
        source,
        title,
        url,
        date_published,
        datetime.now().isoformat(),
        ",".join(matched_keywords)
    ))
    conn.commit()

# ============================================================================
# CONGRESS.GOV API
# ============================================================================

def fetch_congressional_bills(conn):
    """Fetch recent bills mentioning reinsurance keywords."""
    if not CONGRESS_API_KEY:
        print("⚠️  CONGRESS_API_KEY not set. Skipping Congress.gov.")
        return []

    results = []
    
    for keyword in REINSURANCE_KEYWORDS:
        try:
            url = f"{CONGRESS_BASE_URL}/bill/119"
            params = {
                "api_key": CONGRESS_API_KEY,
                "limit": 50,
                "query": keyword,
            }
            resp = requests.get(url, params=params, timeout=10)
            
            if resp.status_code != 200:
                print(f"⚠️  Congress.gov returned {resp.status_code} for '{keyword}'")
                continue
            
            data = resp.json()
            bills = data.get("results", [])
            
            for bill in bills:
                bill_num = bill.get("number", "")
                bill_title = bill.get("title", "")
                bill_url = bill.get("url", "")
                bill_id = f"congress_{bill_num}"
                
                if not is_duplicate(conn, bill_id):
                    results.append({
                        "id": bill_id,
                        "source": "Congress.gov",
                        "title": f"H.R. {bill_num}: {bill_title}",
                        "url": bill_url,
                        "date_published": datetime.now().isoformat(),
                        "keywords": [keyword],
                    })
                    add_update(conn, bill_id, "Congress.gov", f"H.R. {bill_num}: {bill_title}", bill_url, datetime.now().isoformat(), [keyword])
            
            print(f"✓ Congress.gov: Found {len(bills)} bills for '{keyword}'")
            
        except Exception as e:
            print(f"✗ Error fetching Congress.gov for '{keyword}': {e}")
    
    return results

# ============================================================================
# FEDERAL REGISTER API
# ============================================================================

def fetch_federal_register(conn):
    """Fetch recent Federal Register notices from reinsurance-related agencies."""
    results = []
    
    try:
        url = f"{FED_REG_BASE_URL}/documents"
        
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        
        for keyword in REINSURANCE_KEYWORDS:
            params = {
                "conditions[publication_date][gte]": seven_days_ago,
                "conditions[term]": keyword,
                "per_page": 50,
                "order": "newest",
            }
            
            resp = requests.get(url, params=params, timeout=10)
            
            if resp.status_code != 200:
                print(f"⚠️  Federal Register returned {resp.status_code} for '{keyword}'")
                continue
            
            data = resp.json()
            documents = data.get("results", [])
            
            for doc in documents:
                doc_id = f"fedreg_{doc.get('document_number', '')}"
                doc_title = doc.get("title", "")
                doc_url = doc.get("html_url", "")
                doc_agency = doc.get("agency_names", ["Unknown"])[0]
                pub_date = doc.get("publication_date", "")
                
                if not is_duplicate(conn, doc_id):
                    results.append({
                        "id": doc_id,
                        "source": f"Federal Register ({doc_agency})",
                        "title": doc_title,
                        "url": doc_url,
                        "date_published": pub_date,
                        "keywords": [keyword],
                    })
                    add_update(conn, doc_id, "Federal Register", doc_title, doc_url, pub_date, [keyword])
            
            print(f"✓ Federal Register: Found {len(documents)} notices for '{keyword}'")
        
    except Exception as e:
        print(f"✗ Error fetching Federal Register: {e}")
    
    return results

# ============================================================================
# RSS FEEDS
# ============================================================================

def fetch_rss_feeds(conn):
    """Fetch updates from agency RSS feeds."""
    results = []
    
    feeds = [
        ("Treasury Press Releases", "https://home.treasury.gov/feeds/press-releases.xml"),
        ("FDIC Press Releases", "https://www.fdic.gov/news/news-press-releases.xml"),
        ("Federal Register", "https://www.federalregister.gov/documents.rss"),
    ]
    
    for feed_name, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            
            for entry in feed.entries[:20]:
                entry_id = f"rss_{feed_name}_{entry.get('id', entry.get('link', ''))}"
                entry_title = entry.get("title", "")
                entry_link = entry.get("link", "")
                entry_published = entry.get("published", datetime.now().isoformat())
                
                matched_keywords = []
                summary = entry.get("summary", "").lower()
                title_lower = entry_title.lower()
                
                for keyword in REINSURANCE_KEYWORDS:
                    if keyword.lower() in title_lower or keyword.lower() in summary:
                        matched_keywords.append(keyword)
                
                if matched_keywords and not is_duplicate(conn, entry_id):
                    results.append({
                        "id": entry_id,
                        "source": f"RSS: {feed_name}",
                        "title": entry_title,
                        "url": entry_link,
                        "date_published": entry_published,
                        "keywords": matched_keywords,
                    })
                    add_update(conn, entry_id, f"RSS: {feed_name}", entry_title, entry_link, entry_published, matched_keywords)
            
            print(f"✓ RSS: Checked {feed_name}")
            
        except Exception as e:
            print(f"✗ Error fetching RSS {feed_name}: {e}")
    
    return results

# ============================================================================
# GOOGLE SHEETS
# ============================================================================

def load_google_credentials(value):
    """Accept raw service-account JSON or its legacy base64 representation."""
    try:
        value = value.strip().lstrip("\ufeff")
        if not value.startswith("{"):
            value = base64.b64decode("".join(value.split()), validate=True).decode("utf-8-sig")
        info = json.loads(value)
        required = ("client_email", "private_key", "token_uri")
        if not isinstance(info, dict) or info.get("type") != "service_account" or not all(info.get(k) for k in required):
            raise ValueError()
        return info
    except (ValueError, UnicodeError):
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON must contain complete service-account JSON "
            "or its base64 encoding. Replace the repository Actions secret."
        ) from None


def pending_sheet_updates(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM updates WHERE sheets_exported_at IS NULL ORDER BY date_added, id"
    ).fetchall()
    return [dict(row, keywords=(row["keywords"] or "").split(",")) for row in rows]


def append_to_google_sheet(updates, conn):
    """Retry pending exports and reconcile URLs after partial/uncertain writes."""
    if not SHEETS_ENABLED:
        print("Google Sheets export disabled; updates remain pending.")
        return
    if not SHEETS_SPREADSHEET_ID or not gspread or not Credentials:
        raise RuntimeError("Sheets export enabled but spreadsheet ID or dependencies are missing.")
    info = load_google_credentials(GOOGLE_CREDENTIALS_JSON)
    try:
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        worksheet = gspread.authorize(creds).open_by_key(
            SHEETS_SPREADSHEET_ID
        ).worksheet(SHEETS_WORKSHEET_NAME)
        existing_urls = set(worksheet.col_values(4))
        added = 0
        for start in range(0, len(updates), 100):
            batch = updates[start:start + 100]
            rows = []
            batch_urls = set(existing_urls)
            for update in batch:
                if not update["url"]:
                    raise ValueError("Cannot export an update without a URL.")
                if update["url"] not in batch_urls:
                    rows.append([
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        update["source"], update["title"], update["url"],
                        ", ".join(update.get("keywords", [])), update["date_published"], "",
                    ])
                    batch_urls.add(update["url"])
            if rows:
                worksheet.append_rows(rows, value_input_option="RAW")
                added += len(rows)
            # Mark only confirmed batches. On an uncertain write, the next run
            # re-reads Sheet URLs before appending any pending rows.
            conn.executemany("UPDATE updates SET sheets_exported_at = ? WHERE id = ?",
                             [(datetime.now(timezone.utc).isoformat(), u["id"]) for u in batch])
            conn.commit()
            existing_urls = batch_urls
        print(f"Google Sheet updated ({added} rows added; {len(updates)} pending items reconciled)")
    except Exception:
        # API errors may contain request data. Do not log credentials or payloads.
        raise RuntimeError(
            "Google Sheets export failed; unconfirmed items remain pending. "
            "Check service-account JSON, Sheet ID, worksheet name and Editor access."
        ) from None


# ============================================================================
# NOTIFICATIONS
# ============================================================================

def send_slack_notification(updates):
    """Send updates to Slack webhook."""
    if not SLACK_ENABLED or not SLACK_WEBHOOK or not updates:
        return
    
    try:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reinsurance Federal Updates* — {len(updates)} new items\n_Last run: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}_"
                }
            }
        ]
        
        for update in updates[:10]:
            keywords_str = ", ".join(update.get("keywords", []))
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{update['source']}*\n{update['title']}\n<{update['url']}|View>\n_Keywords: {keywords_str}_"
                }
            })
        
        payload = {"blocks": blocks}
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        
        if resp.status_code == 200:
            print(f"✓ Slack notification sent ({len(updates)} updates)")
        else:
            print(f"✗ Slack returned {resp.status_code}")
    
    except Exception as e:
        print(f"✗ Error sending Slack notification: {e}")

def send_email_notification(updates):
    """Send updates via email."""
    if not EMAIL_ENABLED or not EMAIL_TO or not updates:
        return
    
    try:
        msg = MIMEMultipart("html")
        msg["Subject"] = f"Reinsurance Federal Updates — {len(updates)} new items"
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        
        html_body = f"""
        <html>
        <head></head>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <h2>Reinsurance Federal Updates</h2>
            <p><strong>{len(updates)} new items</strong> — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
            <hr>
        """
        
        for update in updates:
            keywords_str = ", ".join(update.get("keywords", []))
            html_body += f"""
            <div style="margin: 15px 0; padding: 10px; border-left: 4px solid #0066cc;">
                <strong>{update['source']}</strong><br>
                <a href="{update['url']}" style="color: #0066cc; text-decoration: none;"><b>{update['title']}</b></a><br>
                <small style="color: #666;">Keywords: {keywords_str} | Published: {update['date_published']}</small>
            </div>
            """
        
        html_body += """
        </body>
        </html>
        """
        
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"✓ Email sent to {EMAIL_TO} ({len(updates)} updates)")
    
    except Exception as e:
        print(f"✗ Error sending email: {e}")

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Run the full collection pipeline."""
    print("=" * 70)
    print("REINSURANCE FEDERAL UPDATES TRACKER")
    print(f"Run started: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    
    conn = init_database()
    
    all_updates = []
    
    print("\n[1/3] Fetching from Congress.gov...")
    all_updates.extend(fetch_congressional_bills(conn))
    
    print("\n[2/3] Fetching from Federal Register...")
    all_updates.extend(fetch_federal_register(conn))
    
    print("\n[3/3] Fetching from RSS feeds...")
    all_updates.extend(fetch_rss_feeds(conn))
    
    print("\n" + "=" * 70)
    if all_updates:
        print(f"Found {len(all_updates)} new updates.\n")
        
        send_slack_notification(all_updates)
        send_email_notification(all_updates)
    else:
        print("No new updates found.\n")
    
    try:
        append_to_google_sheet(pending_sheet_updates(conn), conn)
    finally:
        conn.close()
    print("=" * 70)
    print("Run completed successfully.\n")

if __name__ == "__main__":
    main()
