import os
import json
import re
import sqlite3
import datetime
import textwrap
from typing import List, Dict, Optional, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(DATA_DIR, "sources.json")
RESUME_PATH = os.path.join(DATA_DIR, "resume.txt")
DB_PATH = os.path.join(DATA_DIR, "jobs.db")

# HR job targeting (edit anytime)
GOOD_ROLE_PHRASES = [
    "hr assistant", "human resources assistant",
    "hr coordinator", "human resources coordinator",
    "hr administrator", "human resources administrator",
    "recruitment coordinator", "talent acquisition",
    "people operations", "people & culture",
    "hr operations", "human resources generalist"
]

# Filter out obvious non-HR roles
BAD_ROLE_PHRASES = [
    "software engineer", "engineer", "developer", "devops", "sre",
    "data scientist", "machine learning", "full stack", "backend", "frontend",
    "product manager", "ux", "ui", "qa", "security engineer"
]

# Keywords that should matter for your resume + HR ops roles
HR_KEYWORDS = [
    "human resources", "hr", "recruitment", "interview", "onboarding", "offboarding",
    "hris", "adp", "payroll", "benefits", "employee relations",
    "policy", "compliance", "records", "documentation", "orientation",
    "employment standards", "performance", "reporting", "audit", "excel",
    "active directory", "talent acquisition", "people operations"
]


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        company TEXT,
        location TEXT,
        url TEXT UNIQUE,
        published TEXT,
        summary TEXT,
        score REAL DEFAULT 0.0,
        source TEXT,
        created_at TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    return conn


def set_note(key: str, value: str) -> None:
    conn = db()
    with conn:
        conn.execute(
            "INSERT INTO notes(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    conn.close()


def get_note(key: str) -> Optional[str]:
    conn = db()
    cur = conn.execute("SELECT value FROM notes WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# =========================
# FILES
# =========================
def read_resume() -> str:
    if not os.path.exists(RESUME_PATH):
        return ""
    with open(RESUME_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_sources() -> List[Dict]:
    if not os.path.exists(SOURCES_PATH):
        return []
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Allow both formats:
    # 1) {"name":"x","type":"rss","url":"..."}
    # 2) {"name":"x","url":"..."}  -> treated as rss
    for s in data:
        if "type" not in s:
            s["type"] = "rss"
    return data


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# =========================
# FETCHERS
# =========================
def fetch_rss(url: str) -> List[Dict]:
    feed = feedparser.parse(url)
    jobs: List[Dict] = []
    for e in feed.entries[:60]:
        title = getattr(e, "title", "") or ""
        link = getattr(e, "link", "") or ""
        published = getattr(e, "published", "") or getattr(e, "updated", "") or ""

        raw_summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        summary = BeautifulSoup(raw_summary, "lxml").get_text(" ", strip=True)

        jobs.append({
            "title": title.strip(),
            "company": "",     # RSS often doesn't provide this reliably
            "location": "",
            "url": link.strip(),
            "published": published.strip(),
            "summary": summary.strip(),
        })
    return jobs


def fetch_html_links(url: str) -> List[Dict]:
    """
    Very simple career-page link collector.
    Use this for small company career pages (not for LinkedIn/Google Jobs).
    """
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    page_title = (soup.title.get_text(strip=True) if soup.title else "").strip()
    jobs: List[Dict] = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        blob = normalize(f"{href} {text}")

        if any(w in blob for w in ["job", "career", "posting", "apply", "vacancy", "opportunity"]):
            if href.startswith("/"):
                base = re.match(r"^(https?://[^/]+)", url)
                if base:
                    href = base.group(1) + href
            if href.startswith("http"):
                jobs.append({
                    "title": (text[:140] or page_title or "Job link"),
                    "company": "",
                    "location": "",
                    "url": href,
                    "published": "",
                    "summary": f"Found on: {page_title}" if page_title else ""
                })
        if len(jobs) >= 40:
            break

    return jobs


# =========================
# MATCHING + SCORING
# =========================
def looks_like_hr_job(title: str, summary: str) -> bool:
    blob = normalize(f"{title} {summary}")
    # filter obvious irrelevant roles
    if any(bad in blob for bad in BAD_ROLE_PHRASES):
        return False
    # must have at least one HR-ish hint
    return any(k in blob for k in ["hr", "human resources", "recruit", "people", "talent", "payroll", "benefits"])


def compute_score(title: str, summary: str, resume_text: str) -> float:
    blob = normalize(f"{title} {summary}")
    resume = normalize(resume_text)

    # Strong role match bonus
    role_bonus = 0.0
    for phrase in GOOD_ROLE_PHRASES:
        if phrase in blob:
            role_bonus += 4.0

    # Keyword overlap with resume
    overlap = 0
    for kw in HR_KEYWORDS:
        if kw in blob and kw in resume:
            overlap += 1

    # Demand: HR keywords mentioned in posting (even if not in resume)
    demand = sum(1 for kw in HR_KEYWORDS if kw in blob)

    score = role_bonus + (overlap * 1.2) + (demand * 0.2)
    return round(score, 2)


def missing_keywords(title: str, summary: str, resume_text: str) -> List[str]:
    blob = normalize(f"{title} {summary}")
    resume = normalize(resume_text)
    miss = [kw for kw in HR_KEYWORDS if kw in blob and kw not in resume]
    return miss[:12]


# =========================
# STORE + QUERY
# =========================
def store_jobs(new_jobs: List[Dict], resume_text: str, source_name: str) -> Tuple[int, int]:
    conn = db()
    inserted = skipped = 0
    now = datetime.datetime.now().isoformat(timespec="seconds")

    with conn:
        for j in new_jobs:
            if not j.get("url"):
                skipped += 1
                continue

            title = j.get("title", "")
            summary = j.get("summary", "")

            # filter
            if not looks_like_hr_job(title, summary):
                continue

            score = compute_score(title, summary, resume_text)

            try:
                conn.execute("""
                    INSERT INTO jobs(title, company, location, url, published, summary, score, source, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (
                    title,
                    j.get("company", ""),
                    j.get("location", ""),
                    j.get("url", ""),
                    j.get("published", ""),
                    summary,
                    score,
                    source_name,
                    now
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1

    conn.close()
    return inserted, skipped


def top_matches(limit: int = 10) -> List[Tuple]:
    conn = db()
    cur = conn.execute("""
        SELECT id, title, company, location, score, url, source
        FROM jobs
        ORDER BY score DESC, id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_job(job_id: int) -> Optional[Dict]:
    conn = db()
    cur = conn.execute("""
        SELECT id, title, company, location, url, published, summary, score, source
        FROM jobs WHERE id=?
    """, (job_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["id", "title", "company", "location", "url", "published", "summary", "score", "source"]
    return dict(zip(keys, row))


# =========================
# TAILORING
# =========================
def tailor_cover_letter(resume_text: str, job_title: str, company: str, job_desc: str) -> str:
    company = company or "the organization"
    job_title = job_title or "the role"

    miss = missing_keywords(job_title, job_desc, resume_text)

    # pull a few relevant resume lines
    highlights = []
    for line in resume_text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        low = ln.lower()
        if any(k in low for k in ["recruit", "onboard", "offboard", "hris", "payroll", "policy", "compliance", "records", "employee relations", "active directory", "adp"]):
            highlights.append(ln)
        if len(highlights) >= 4:
            break

    body = f"""Dear Hiring Manager,

I’m applying for {job_title} at {company}. I bring HR operations and administration experience supporting recruitment coordination, onboarding/offboarding, HRIS and records maintenance, policy-compliant documentation, and service-focused support.

Highlights I would bring to this role:
- {highlights[0] if len(highlights) > 0 else "Recruitment coordination, interview scheduling, and candidate follow-up support."}
- {highlights[1] if len(highlights) > 1 else "Onboarding/offboarding support, orientation scheduling, and documentation processing."}
- {highlights[2] if len(highlights) > 2 else "HRIS and employee records maintenance aligned with policy and privacy standards."}
- {highlights[3] if len(highlights) > 3 else "Professional handling of confidential information and stakeholder support."}

I’m interested in this opportunity because it aligns with my strengths in HR administration, compliance, and supporting a positive employee experience. I would welcome the chance to discuss how I can contribute to your HR team.

Sincerely,
Alfred Bosah
"""
    if miss:
        body += "\n(Keyword checklist: only include these if they truthfully apply to you: " + ", ".join(miss) + ")\n"
    return body


def resume_suggestions(resume_text: str, job_desc: str, job_title: str) -> List[str]:
    miss = missing_keywords(job_title, job_desc, resume_text)
    tips = []
    for kw in miss[:8]:
        tips.append(f"If it’s true for you, add evidence of: **{kw}**.")
    tips.append("Rewrite bullets to start with action verbs (Coordinated, Maintained, Supported, Delivered) instead of 'I ...'.")
    tips.append("Add small metrics where true (e.g., interviews scheduled/week, files maintained, departments supported).")
    return tips[:10]


# =========================
# TELEGRAM COMMANDS
# =========================
HELP_TEXT = """Commands:
/start
/help
/sources
/scan
/matches [n]
/tailor <job_id>   (I’ll ask you to paste the job description text)
/links
/whoami
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ JobBot v2 is running.\n\n" + HELP_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat_id is: {update.effective_chat.id}")

async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sources = load_sources()
    if not sources:
        await update.message.reply_text("No sources found. Add RSS sources in sources.json.")
        return
    lines = ["Sources:"]
    for i, s in enumerate(sources, 1):
        lines.append(f"{i}. [{s.get('type','rss')}] {s.get('name','(no name)')}")
        lines.append(f"   {s.get('url')}")
    await update.message.reply_text("\n".join(lines))

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resume_text = read_resume()
    if not resume_text.strip():
        await update.message.reply_text("resume.txt is missing/empty. Add your resume as plain text.")
        return

    sources = load_sources()
    if not sources:
        await update.message.reply_text("No sources in sources.json. Add a few RSS feeds first.")
        return

    total_inserted = 0
    total_skipped = 0
    errors = 0

    for s in sources:
        src_name = s.get("name", s.get("url", "source"))
        src_type = s.get("type", "rss")
        try:
            if src_type == "html":
                jobs = fetch_html_links(s["url"])
            else:
                jobs = fetch_rss(s["url"])
            ins, sk = store_jobs(jobs, resume_text, src_name)
            total_inserted += ins
            total_skipped += sk
        except Exception:
            errors += 1

    await update.message.reply_text(
        f"Scan done ✅\nInserted: {total_inserted}\nSkipped/duplicates: {total_skipped}\nSource errors: {errors}"
    )

async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 10
    if context.args:
        try:
            n = max(1, min(25, int(context.args[0])))
        except Exception:
            n = 10

    rows = top_matches(n)
    if not rows:
        await update.message.reply_text("No jobs saved yet. Run /scan first.")
        return

    msg_lines = [f"Top {len(rows)} matches:"]
    for (jid, title, company, location, score, url, source) in rows:
        company = company or "Company?"
        location = location or ""
        msg_lines.append(f"\n#{jid} | score {score} | {title}\n{company} {('- ' + location) if location else ''}\nSource: {source}\n{url}")

    text = "\n".join(msg_lines)
    # Telegram message safety (split long messages)
    for chunk in textwrap.wrap(text, width=3500, replace_whitespace=False, drop_whitespace=False):
        await update.message.reply_text(chunk)

async def tailor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /tailor <job_id>")
        return
    try:
        job_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Job id must be a number.")
        return

    job = get_job(job_id)
    if not job:
        await update.message.reply_text("Couldn’t find that job id. Try /matches")
        return

    set_note("awaiting_job_text", str(job_id))
    await update.message.reply_text(
        f"Paste the FULL job description text for:\n{job['title']}\n{job['url']}\n\n(Your next message should be the job description.)"
    )

async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Job search links (open manually):\n\n"

        "🌎 REMOTE HR JOBS\n"
        "RemoteOK HR:\nhttps://remoteok.com/remote-hr-jobs\n\n"
        "Remotive HR:\nhttps://remotive.com/remote-jobs/hr\n\n"
        "WeWorkRemotely HR:\nhttps://weworkremotely.com/categories/remote-human-resources-jobs\n\n"
        "Google Remote HR:\nhttps://www.google.com/search?q=remote+human+resources+jobs\n\n"

        "🇨🇦 BC / CANADA\n"
        "LinkedIn HR Kelowna:\nhttps://www.linkedin.com/jobs/search/?keywords=human%20resources&location=Kelowna%2C%20British%20Columbia\n\n"
        "LinkedIn HR Vancouver:\nhttps://www.linkedin.com/jobs/search/?keywords=human%20resources&location=Vancouver%2C%20British%20Columbia\n\n"
        "Google Jobs Kelowna:\nhttps://www.google.com/search?q=human+resources+jobs+Kelowna+BC\n\n"
        "Google Jobs Vancouver:\nhttps://www.google.com/search?q=human+resources+jobs+Vancouver+BC\n\n"
        "WorkBC:\nhttps://www.workbc.ca/search-and-prepare-job/find-jobs\n"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    awaiting_job_id = get_note("awaiting_job_text")

    if awaiting_job_id and awaiting_job_id.isdigit() and int(awaiting_job_id) > 0:
        job_id = int(awaiting_job_id)
        job = get_job(job_id)
        set_note("awaiting_job_text", "0")

        resume_text = read_resume()
        if not resume_text.strip():
            await update.message.reply_text("resume.txt missing. Add your resume text.")
            return

        job_desc = text.strip()
        score = compute_score(job.get("title", ""), job_desc, resume_text)
        miss = missing_keywords(job.get("title", ""), job_desc, resume_text)

        cover = tailor_cover_letter(
            resume_text=resume_text,
            job_title=job.get("title", ""),
            company=job.get("company", ""),
            job_desc=job_desc,
        )
        tips = resume_suggestions(resume_text, job_desc, job.get("title", ""))

        out = (
            f"Tailoring for job #{job_id}\n"
            f"Title: {job.get('title','')}\n"
            f"Match score (rough): {score}\n"
            f"Keyword checklist (only if true): {', '.join(miss) if miss else 'None'}\n\n"
            f"COVER LETTER DRAFT:\n{cover}\n\n"
            f"RESUME SUGGESTIONS:\n- " + "\n- ".join(tips)
        )

        for chunk in textwrap.wrap(out, width=3500, replace_whitespace=False, drop_whitespace=False):
            await update.message.reply_text(chunk)
        return

    await update.message.reply_text("Use /help for commands.")


# =========================
# MAIN
# =========================
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing (set it in Railway Variables).")

    # Ensure sources.json exists to avoid crashes
    if not os.path.exists(SOURCES_PATH):
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            f.write("[]")

    _ = db()  # init DB

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("sources", sources_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("tailor", tailor_cmd))
    app.add_handler(CommandHandler("links", links_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
