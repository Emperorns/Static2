import os
from flask import Flask, render_template, send_from_directory, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import ffmpeg
from threading import Thread

# Load environment
load_dotenv()
BOT_TOKEN    = os.getenv('BOT_TOKEN')
MONGODB_URI  = os.getenv('MONGODB_URI')            # e.g. "mongodb+srv://user:pass@cluster0.mongodb.net/Cluster0?retryWrites=true&w=majority"
DB_NAME      = os.getenv('DB_NAME')                # e.g. "Cluster0"
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
    if update.effective_user.id != ADMIN_ID:
        return
    video   = update.message.video
    file_id = video.file_id

    # Forward to channel
    await context.bot.send_video(CHANNEL_ID, file_id)

    # Download file
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

async def start(update: Update, context):
    args = context.args
    if args:
        fid = args[0]
        await context.bot.send_video(update.effective_chat.id, fid)
    else:
        await update.message.reply_text("Send me a video (admin only) or click a thumbnail on the site.")

application.add_handler(MessageHandler(filters.VIDEO, handle_video))
application.add_handler(CommandHandler("start", start))

# Flask routes
@app.route('/')
def index():
    all_videos = list(videos.find().sort('_id', -1))
    return render_template('index.html', videos=all_videos, bot_username=BOT_USERNAME)

@app.route('/thumbnails/<path:filename>')
def thumbs(filename):
    return send_from_directory(THUMB_DIR, filename)

@app.route('/api/videos')
def api_videos():
    all_videos = list(videos.find({}, {'_id': 0}).sort('_id', -1))
    return jsonify(all_videos)

if __name__ == '__main__':
    # Start Telegram bot polling
    Thread(target=application.run_polling, daemon=True).start()
    # Run Flask app
    app.run(host='0.0.0.0', port=PORT)
