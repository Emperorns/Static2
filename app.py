import os
import pathlib
from flask import Flask, render_template, send_from_directory, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import ffmpeg
from threading import Thread
import asyncio

# Load environment variables
load_dotenv()
BOT_TOKEN    = os.getenv('BOT_TOKEN')
MONGODB_URI  = os.getenv('MONGODB_URI')
DB_NAME      = os.getenv('DB_NAME')
ADMIN_ID     = int(os.getenv('ADMIN_ID'))
CHANNEL_ID   = os.getenv('CHANNEL_ID')
BOT_USERNAME = os.getenv('BOT_USERNAME')
PUBLIC_URL   = os.getenv('PUBLIC_URL')
PORT         = int(os.getenv('PORT', 5000))

# Sanity checks
if not MONGODB_URI:
    raise RuntimeError("❌ MONGODB_URI environment variable is not set!")
if not DB_NAME:
    raise RuntimeError("❌ DB_NAME environment variable is not set!")

# Flask setup
app = Flask(__name__)
# Set default cache timeout for static files (thumbnails)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400  # 24 hours

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos

# Thumbnails directory
THUMB_DIR = os.path.join(os.getcwd(), 'thumbnails')
pathlib.Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)

# Telegram bot setup
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Handle video uploads (admin only)
async def handle_video(update: Update, context):
    user = update.effective_user or getattr(update.message, 'from_user', None)
    if not user or user.id != ADMIN_ID:
        return

    await update.message.reply_text("🔄 Processing your video...")

    video = update.message.video
    file = await context.bot.get_file(video.file_id)

    # Download video locally
    local_video_path = os.path.join(THUMB_DIR, f"{video.file_unique_id}.mp4")
    await file.download_to_drive(local_video_path)

    # Upload to your channel to get valid file_id
    with open(local_video_path, 'rb') as f:
        sent_msg = await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=f,
            caption=f"Uploaded by {user.first_name}"
        )

    new_file_id = sent_msg.video.file_id
    print(f"New stable file_id: {new_file_id}")  # Debug print

    # Generate thumbnail
    thumb_path = os.path.join(THUMB_DIR, f"{new_file_id}.jpg")
    ffmpeg.input(local_video_path, ss='00:00:01').output(thumb_path, vframes=1).run(overwrite_output=True)

    # Create custom key
    custom_key = f"file_{video.file_unique_id}"

    # Save to DB
    thumb_url = f"{PUBLIC_URL}/thumbnails/{new_file_id}.jpg"
    videos.insert_one({
        'file_id': new_file_id,
        'custom_key': custom_key,
        'thumbnail_url': thumb_url
    })

    await update.message.reply_text(
        f"✅ Video uploaded and stored.\n\nDeep link:\nhttps://t.me/{BOT_USERNAME}?start={custom_key}"
    )

# Handle /start with custom key
async def start(update: Update, context):
    args = context.args
    if args:
        custom_key = args[0]
        video_data = videos.find_one({'custom_key': custom_key})
        if video_data:
            await context.bot.send_video(update.effective_chat.id, video_data['file_id'])
        else:
            await update.message.reply_text("❌ Video not found or link expired.")
    else:
        await update.message.reply_text(
            "👋 Welcome! Send me a video (admin only) or click a thumbnail on the site to receive a video."
        )

# Register bot handlers
application.add_handler(MessageHandler(filters.VIDEO, handle_video))
application.add_handler(CommandHandler("start", start))

# Flask routes
@app.route('/')
def index():
    all_videos = list(videos.find().sort('_id', -1))
    deep_link_prefix = f"https://t.me/{BOT_USERNAME}?start="
    return render_template('index.html', videos=all_videos, deep_link_prefix=deep_link_prefix)

@app.route('/thumbnails/<path:filename>')
def thumbs(filename):
    # Serve thumbnails with caching to prevent expiration issues
    return send_from_directory(THUMB_DIR, filename, cache_timeout=86400)

@app.route('/api/videos')
def api_videos():
    all_videos = list(videos.find({}, {'_id': 0}).sort('_id', -1))
    return jsonify(all_videos)

# Run bot and web server
if __name__ == "__main__":
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()

    Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
