import os
import feedparser
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Load job sources
def load_sources():
    import json
    with open("sources.json") as f:
        return json.load(f)

# Scan job feeds
def get_jobs():
    jobs = []
    sources = load_sources()

    for source in sources:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:5]:
            jobs.append({
                "title": entry.title,
                "link": entry.link
            })

    return jobs

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Job bot is running.\nUse /scan to find HR jobs.")

# /scan command
async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = get_jobs()

    if not jobs:
        await update.message.reply_text("No jobs found.")
        return

    message = "Top HR Jobs:\n\n"

    for job in jobs[:10]:
        message += f"{job['title']}\n{job['link']}\n\n"

    await update.message.reply_text(message)

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main(app.add_handler(CommandHandler("links", links))
