import os
import pathlib
from flask import Flask, render_template, send_from_directory, jsonify, make_response
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import asyncio
from threading import Thread

# Load environment variables
load_dotenv()
BOT_TOKEN    = os.getenv('BOT_TOKEN')
MONGODB_URI  = os.getenv('MONGODB_URI')
DB_NAME      = os.getenv('DB_NAME')
ADMIN_ID     = int(os.getenv('ADMIN_ID'))
CHANNEL_ID   = int(os.getenv('CHANNEL_ID'))
BOT_USERNAME = os.getenv('BOT_USERNAME')
PUBLIC_URL   = os.getenv('PUBLIC_URL')
PORT         = int(os.getenv('PORT', 5000))

# Sanity checks
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI env var not set")
if not DB_NAME:
    raise RuntimeError("DB_NAME env var not set")

# Flask setup
app = Flask(__name__)

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos

# Thumbnails directory
THUMB_DIR = os.path.join(os.getcwd(), 'thumbnails')
pathlib.Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)

# Telegram bot setup
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Helper to download thumbnail
async def download_thumbnail(bot, thumb, key):
    thumb_path = os.path.join(THUMB_DIR, f"{key}.jpg")
    file = await bot.get_file(thumb.file_id)
    await file.download_to_drive(thumb_path)
    return f"{PUBLIC_URL}/thumbnails/{key}.jpg"

# Admin video handler
async def handle_video(update: Update, context):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    msg = update.message
    video = msg.video
    key = f"file_{video.file_unique_id}"
    # download telegram thumbnail
    thumb = video.thumbnail or video.thumb
    thumb_url = await download_thumbnail(context.bot, thumb, key) if thumb else ''
    # forward to channel
    sent = await context.bot.forward_message(
        chat_id=CHANNEL_ID,
        from_chat_id=msg.chat.id,
        message_id=msg.message_id
    )
    file_id = sent.video.file_id
    title = msg.caption or 'Untitled'
    videos.insert_one({'file_id': file_id, 'custom_key': key, 'title': title, 'thumbnail_url': thumb_url})
    await msg.reply_text(f"Stored '{title}'. Deep link: https://t.me/{BOT_USERNAME}?start={key}")

# Channel post handler
async def channel_video(update: Update, context):
    post = update.channel_post
    if post.chat.id != CHANNEL_ID or not post.video:
        return
    video = post.video
    key = f"file_{video.file_unique_id}"
    thumb = video.thumbnail or video.thumb
    thumb_url = await download_thumbnail(context.bot, thumb, key) if thumb else ''
    file_id = video.file_id
    title = post.caption or 'Untitled'
    videos.insert_one({'file_id': file_id, 'custom_key': key, 'title': title, 'thumbnail_url': thumb_url})

# Start command
async def start_command(update: Update, context):
    args = context.args
    if not args:
        return await update.message.reply_text("Welcome! Use a deep link to get a video.")
    key = args[0]
    data = videos.find_one({'custom_key': key})
    if not data:
        return await update.message.reply_text("Video not found.")
    await context.bot.send_video(update.effective_chat.id, data['file_id'], caption=data.get('title', ''))

# Register handlers
application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video))
application.add_handler(MessageHandler(filters.VIDEO & filters.Chat(CHANNEL_ID), channel_video))
application.add_handler(CommandHandler("start", start_command))

# Flask routes
@app.route('/')
def index():
    vids = list(videos.find().sort('_id', -1))
    return render_template('index.html', videos=vids, bot_username=BOT_USERNAME)

@app.route('/thumbnails/<path:fn>')
def thumbs(fn):
    resp = make_response(send_from_directory(THUMB_DIR, fn))
    resp.headers['Cache-Control'] = 'public, max-age=31536000'
    return resp

@app.route('/api/videos')
def api_videos():
    lst = list(videos.find({}, {'_id': 0}).sort('_id', -1))
    return jsonify(lst)

if __name__ == '__main__':
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()
    Thread(target=run_bot, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
