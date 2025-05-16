import os
import io
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, send_from_directory, make_response
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import logging
import asyncio
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
BOT_TOKEN       = os.getenv('BOT_TOKEN')
MONGODB_URI     = os.getenv('MONGODB_URI')
DB_NAME         = os.getenv('DB_NAME')
ADMIN_ID        = int(os.getenv('ADMIN_ID'))
CHANNEL_ID      = int(os.getenv('CHANNEL_ID'))
UPDATES_CHANNEL = os.getenv('UPDATES_CHANNEL')
CAPTCHA_URL     = os.getenv('CAPTCHA_URL')
TUTORIAL_URL    = os.getenv('TUTORIAL_URL')
LOG_CHANNEL     = os.getenv('LOG_CHANNEL')
BOT_USERNAME    = os.getenv('BOT_USERNAME')
PORT            = int(os.getenv('PORT', 5000))
VERIFY_INTERVAL = timedelta(hours=2)
SELF_DESTRUCT   = timedelta(hours=1)

# Initialize Flask app
app = Flask(__name__)

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos
users = db.users

# Initialize Telegram bot
application = ApplicationBuilder().token(BOT_TOKEN).build()
sync_bot = Bot(token=BOT_TOKEN)

# Ensure thumbnails directory exists
THUMBNAILS_DIR = os.path.join('static', 'thumbnails')
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

def get_thumbnail_path(key):
    return os.path.join(THUMBNAILS_DIR, f"{key}.jpg")

async def save_thumbnail(file_id, key):
    try:
        file = await sync_bot.get_file(file_id)
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        buf.seek(0)
        path = get_thumbnail_path(key)
        with open(path, 'wb') as f:
            f.write(buf.read())
        return path
    except Exception as e:
        logger.error(f"Failed to save thumbnail for {key}: {e}")
        return None

async def check_membership(bot, user_id):
    try:
        m = await bot.get_chat_member(UPDATES_CHANNEL, user_id)
        return m.status not in ['left', 'kicked']
    except Exception as e:
        logger.error(f"Membership check error for {user_id}: {e}")
        return False

async def is_verified(user_id):
    rec = users.find_one({'user_id': user_id})
    if not rec or 'last_verified' not in rec:
        return False
    return datetime.utcnow() - rec['last_verified'] < VERIFY_INTERVAL

async def require_access(update: Update, context):
    uid = update.effective_user.id
    if not await check_membership(context.bot, uid):
        btn = InlineKeyboardButton("Join Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL.strip('@')}")
        await update.message.reply_text("🚨 Please join the updates channel.", reply_markup=InlineKeyboardMarkup([[btn]]))
        return False
    if not await is_verified(uid):
        btn1 = InlineKeyboardButton("Verify Human", url=CAPTCHA_URL)
        btn2 = InlineKeyboardButton("How to Solve Captcha", url=TUTORIAL_URL)
        await update.message.reply_text("🛡️ Please verify human to proceed.", reply_markup=InlineKeyboardMarkup([[btn1], [btn2]]))
        return False
    return True

async def delete_message_job(context):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data['chat_id'], message_id=data['message_id'])
    except Exception as e:
        logger.error(f"Error deleting message: {e}")

async def handle_media(update: Update, context):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    msg = update.message
    media = msg.video or msg.document
    if not media:
        return
    key = f"file_{media.file_unique_id}"

    # extract thumbnail_file_id
    thumb_id = None
    if msg.video and msg.video.thumb:
        thumb_id = msg.video.thumb.file_id
    elif msg.document and getattr(msg.document, 'thumb', None):
        thumb_id = msg.document.thumb.file_id

    # save thumbnail locally
    thumb_path = None
    if thumb_id:
        thumb_path = await save_thumbnail(thumb_id, key)

    # forward to channel
    sent = await context.bot.forward_message(CHANNEL_ID, msg.chat.id, msg.message_id)
    fid = sent.video.file_id if sent.video else sent.document.file_id

    videos.insert_one({
        'file_id': fid,
        'custom_key': key,
        'title': msg.caption or 'Untitled',
        'type': 'video' if sent.video else 'document',
        'thumbnail_file_id': thumb_id,
        'thumbnail_path': thumb_path,
        'thumbnail_url': f"/thumbnails/{key}.jpg"
    })
    await context.bot.send_message(ADMIN_ID, f"✅ Saved {key}")

async def channel_media(update: Update, context):
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return
    media = post.video or post.document
    key = f"file_{media.file_unique_id}"

    thumb_id = None
    if post.video and post.video.thumb:
        thumb_id = post.video.thumb.file_id
    elif post.document and getattr(post.document, 'thumb', None):
        thumb_id = post.document.thumb.file_id

    thumb_path = None
    if thumb_id:
        thumb_path = await save_thumbnail(thumb_id, key)

    videos.insert_one({
        'file_id': media.file_id,
        'custom_key': key,
        'title': post.caption or 'Untitled',
        'type': 'video' if post.video else 'document',
        'thumbnail_file_id': thumb_id,
        'thumbnail_path': thumb_path,
        'thumbnail_url': f"/thumbnails/{key}.jpg"
    })

async def start_command(update: Update, context):
    args = context.args
    uid = update.effective_user.id
    if args and args[0] == 'verified':
        users.update_one({'user_id': uid}, {'$set': {'last_verified': datetime.utcnow()}}, upsert=True)
        await context.bot.send_message(LOG_CHANNEL, f"🔐 User {uid} verified")
        await update.message.reply_text("✅ Verified for 2 hours")
        return
    if not await require_access(update, context):
        return
    if not args:
        btn = InlineKeyboardButton("How to Use", url=TUTORIAL_URL)
        await update.message.reply_text("👋 Welcome!", reply_markup=InlineKeyboardMarkup([[btn]]))
        return
    key = args[0]
    rec = videos.find_one({'custom_key': key})
    if not rec:
        await update.message.reply_text("❌ Media not found.")
        return
    send_fn = context.bot.send_video if rec['type'] == 'video' else context.bot.send_document
    sent = await send_fn(update.effective_chat.id, rec['file_id'], caption=rec['title'], protect_content=True)
    context.job_queue.run_once(delete_message_job, when=SELF_DESTRUCT,
                               data={'chat_id': update.effective_chat.id, 'message_id': sent.message_id})


def register_handlers():
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.ALL), handle_media))
    application.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & (filters.VIDEO | filters.Document.ALL), channel_media))
    application.add_handler(CommandHandler('start', start_command))


def register_routes():
    @app.route('/')
    def index():
        vids = list(videos.find().sort('_id', -1))
        return render_template('index.html', videos=vids, bot_username=BOT_USERNAME)

    @app.route('/file/<key>')
    def file_page(key):
        rec = videos.find_one({'custom_key': key})
        if not rec:
            return "File not found", 404
        return render_template('file.html', key=key,
                               thumb_url=rec.get('thumbnail_url', ''),
                               title=rec.get('title', 'Untitled'), bot_username=BOT_USERNAME)

    @app.route('/api/videos')
    def api_videos():
        return jsonify(list(videos.find({}, {'_id': 0}).sort('_id', -1)))

    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory('static', 'fallback.jpg', mimetype='image/jpeg')

    @app.route('/thumbnails/<key>.jpg')
    def serve_thumbnail(key):
        logger.info(f"Requested thumbnail for key: {key}")
        rec = videos.find_one({'custom_key': key})
        if not rec or not rec.get('thumbnail_path'):
            logger.error(f"No thumbnail for key: {key}")
            return send_from_directory('static', 'fallback.jpg'), 200
        try:
            resp = make_response(send_from_directory('static/thumbnails', f"{key}.jpg"))
            resp.headers['Cache-Control'] = 'public, max-age=31536000'
            return resp
        except FileNotFoundError:
            logger.error(f"Thumbnail missing for {key}")
            return send_from_directory('static', 'fallback.jpg'), 200

async def migrate_thumbnails():
    for rec in videos.find({'thumbnail_path': None, 'thumbnail_file_id': {'$exists': True}}):
        key = rec['custom_key']
        try:
            path = await save_thumbnail(rec['thumbnail_file_id'], key)
            if path:
                videos.update_one({'custom_key': key}, {'$set': {'thumbnail_path': path}})
                logger.info(f"Migrated thumbnail for {key}")
        except Exception as e:
            logger.error(f"Migration error for {key}: {e}")


def run_flask():
    app.run(host='0.0.0.0', port=PORT)

async def main():
    await migrate_thumbnails()
    register_handlers()
    register_routes()
    Thread(target=run_flask, daemon=True).start()
    await application.run_polling()

if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())
