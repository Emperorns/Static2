import os
import pathlib
import asyncio
from threading import Thread
from flask import Flask, render_template, send_from_directory, jsonify, make_response
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters

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

# Flask app
app = Flask(__name__)

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos

# Ensure thumbnails dir
THUMB_DIR = os.path.join(os.getcwd(), 'thumbnails')
pathlib.Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)

# Initialize Telegram bot
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Helper: download Telegram thumbnail
async def download_thumbnail(bot, thumb, key):
    thumb_path = os.path.join(THUMB_DIR, f"{key}.jpg")
    file = await bot.get_file(thumb.file_id)
    await file.download_to_drive(thumb_path)
    return f"{PUBLIC_URL}/thumbnails/{key}.jpg"

# Process uploads via bot (admin only)
async def handle_video(update: Update, context):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    msg = update.message
    video = msg.video
    key = f"file_{video.file_unique_id}"
    print(f"[BOT] Received video from admin {user.id}, processing {key}")
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔄 Processing admin upload {key}...")
    # thumbnail
    thumb_attr = getattr(video, 'thumbnail', None) or getattr(video, 'thumb', None)
    thumb_url = ''
    if thumb_attr:
        photo = thumb_attr[-1] if isinstance(thumb_attr, list) else thumb_attr
        thumb_url = await download_thumbnail(context.bot, photo, key)
    # forward to channel
    sent = await context.bot.forward_message(
        chat_id=CHANNEL_ID,
        from_chat_id=msg.chat.id,
        message_id=msg.message_id
    )
    file_id = sent.video.file_id
    title = msg.caption or 'Untitled'
    videos.insert_one({'file_id': file_id, 'custom_key': key, 'title': title, 'thumbnail_url': thumb_url})
    print(f"[BOT] Admin upload {key} saved")
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Admin video saved: {title} ({key})")

# Process channel posts (any user)
async def channel_video(update: Update, context):
    post = update.channel_post
    if post.chat.id != CHANNEL_ID or not post.video:
        return
    video = post.video
    key = f"file_{video.file_unique_id}"
    print(f"[CHANNEL] New channel video {key}, processing...")
    # notify admin
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔄 Processing channel video {key}...")
    # thumbnail
    thumb_attr = getattr(video, 'thumbnail', None) or getattr(video, 'thumb', None)
    thumb_url = ''
    if thumb_attr:
        photo = thumb_attr[-1] if isinstance(thumb_attr, list) else thumb_attr
        thumb_url = await download_thumbnail(context.bot, photo, key)
    file_id = video.file_id
    title = post.caption or 'Untitled'
    videos.insert_one({'file_id': file_id, 'custom_key': key, 'title': title, 'thumbnail_url': thumb_url})
    print(f"[CHANNEL] Video {key} saved")
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Channel video saved: {title} ({key})")

# /start handler\ nasync def start_command(update: Update, context):
    args = context.args
    if not args:
        return await update.message.reply_text("👋 Send me a video or use a deep link.")
    key = args[0]
    data = videos.find_one({'custom_key': key})
    if not data:
        return await update.message.reply_text("❌ Video not found.")
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

@app.route('/thumbnails/<path:fname>')
def thumbs(fname):
    resp = make_response(send_from_directory(THUMB_DIR, fname))
    resp.headers['Cache-Control'] = 'public, max-age=31536000'
    return resp

@app.route('/api/videos')
def api_videos():
    return jsonify(list(videos.find({}, {'_id':0}).sort('_id', -1)))

# Run bot and web server
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
