import os
from flask import Flask, render_template, send_from_directory, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import ffmpeg
from threading import Thread
import asyncio

# Load environment
load_dotenv()
BOT_TOKEN    = os.getenv('BOT_TOKEN')
MONGODB_URI  = os.getenv('MONGODB_URI')  # e.g. "mongodb+srv://user:pass@cluster0.mongodb.net/Cluster0?retryWrites=true&w=majority"
DB_NAME      = os.getenv('DB_NAME')      # e.g. "Cluster0"
ADMIN_ID     = int(os.getenv('ADMIN_ID'))
CHANNEL_ID   = os.getenv('CHANNEL_ID')
BOT_USERNAME = os.getenv('BOT_USERNAME')
PUBLIC_URL   = os.getenv('PUBLIC_URL')
PORT         = int(os.getenv('PORT', 5000))

# Sanity checks
if not MONGODB_URI:
    raise RuntimeError("❌ MONGODB_URI environment variable is not set!")
if not DB_NAME:
    raise RuntimeError("❌ DB_NAME environment variable is not set! Add DB_NAME to .env")

# Flask setup
app = Flask(__name__)

# MongoDB (direct client)
client = MongoClient(MONGODB_URI)
db     = client[DB_NAME]
videos = db.videos

# Ensure thumbnails folder exists
THUMB_DIR = os.path.join(os.getcwd(), 'thumbnails')
import pathlib; pathlib.Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)

# Telegram bot
application = ApplicationBuilder().token(BOT_TOKEN).build()

async def handle_video(update: Update, context):
    # Safely get user
    user = update.effective_user or getattr(update.message, 'from_user', None)
    if not user or user.id != ADMIN_ID:
        return

    # Acknowledge receipt
    await update.message.reply_text("🔄 Processing your video...")

    video = update.message.video
    file_id = video.file_id

    # Forward to channel
    await context.bot.send_video(CHANNEL_ID, file_id)

    # Download video file
    file = await context.bot.get_file(file_id)
    video_path = os.path.join(THUMB_DIR, f"{file_id}.mp4")
    await file.download_to_drive(video_path)

    # Extract thumbnail at 1 second
    thumb_path = os.path.join(THUMB_DIR, f"{file_id}.jpg")
    ffmpeg.input(video_path, ss='00:00:01').output(thumb_path, vframes=1).run(overwrite_output=True)

    # Save metadata
    thumb_url = f"{PUBLIC_URL}/thumbnails/{file_id}.jpg"
    videos.insert_one({'file_id': file_id, 'thumbnail_url': thumb_url})
    await update.message.reply_text("✅ Video uploaded and processed.")

# Register video handler
application.add_handler(MessageHandler(filters.VIDEO, handle_video))

async def start(update: Update, context):
    args = context.args
    if args:
        file_id = args[0]
        await update.message.reply_text("🔄 Retrieving your video...")
        try:
            await context.bot.send_video(update.effective_chat.id, file_id)
        except Exception:
            await update.message.reply_text("❌ Could not send video. It may have expired or the file_id is invalid.")
    else:
        await update.message.reply_text(
            "👋 Welcome! Send me a video (admin only) or click a thumbnail on the site to receive a video."
        )

# Register start handler
application.add_handler(CommandHandler("start", start))

# Flask routes
@app.route('/')
def index():
    all_videos = list(videos.find().sort('_id', -1))
    deep_link_prefix = f"tg://resolve?domain={BOT_USERNAME}&start="
    return render_template(
        'index.html', videos=all_videos,
        bot_username=BOT_USERNAME,
        deep_link_prefix=deep_link_prefix
    )

@app.route('/thumbnails/<path:filename>')
def thumbs(filename):
    return send_from_directory(THUMB_DIR, filename)

@app.route('/api/videos')
def api_videos():
    all_videos = list(videos.find({}, {'_id': 0}).sort('_id', -1))
    return jsonify(all_videos)

# Entry point
if __name__ == "__main__":
    def run_bot():
        # Each thread needs its own event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Initialize and start Telegram application
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()

    # Start Telegram polling in a separate daemon thread
    Thread(target=run_bot, daemon=True).start()

    # Start the Flask web server
    app.run(host="0.0.0.0", port=PORT)
