import os
import io
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, send_file, make_response, send_from_directory, request
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.request import HTTPXRequest
import logging
import asyncio
import nest_asyncio
import time
import httpx

# Apply nest_asyncio for Koyeb compatibility
nest_asyncio.apply()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
BOT_TOKEN        = os.getenv('BOT_TOKEN')
MONGODB_URI      = os.getenv('MONGODB_URI')
DB_NAME          = os.getenv('DB_NAME')
ADMIN_ID         = int(os.getenv('ADMIN_ID'))
MOVIES_CHANNEL_ID = int(os.getenv('MOVIES_CHANNEL_ID'))
ADULT_CHANNEL_ID  = int(os.getenv('ADULT_CHANNEL_ID'))
ANIME_CHANNEL_ID  = int(os.getenv('ANIME_CHANNEL_ID'))
UPDATES_CHANNEL  = os.getenv('UPDATES_CHANNEL')
CAPTCHA_URL      = os.getenv('CAPTCHA_URL')
TUTORIAL_URL     = os.getenv('TUTORIAL_URL')
LOG_CHANNEL      = os.getenv('LOG_CHANNEL')
BOT_USERNAME     = os.getenv('BOT_USERNAME')
KOYEB_PUBLIC_DOMAIN = os.getenv('KOYEB_PUBLIC_DOMAIN')
PORT             = int(os.getenv('PORT', 5000))
VERIFY_INTERVAL  = timedelta(hours=2)
SELF_DESTRUCT    = timedelta(hours=1)

# Initialize Flask app
app = Flask(__name__)

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos
users = db.users

# Initialize Telegram bot with custom HTTPXRequest
application = ApplicationBuilder().token(BOT_TOKEN).request(HTTPXRequest(http_version="1.1", connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)).build()
sync_bot = Bot(token=BOT_TOKEN, request=HTTPXRequest(http_version="1.1", connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0))

# Ensure thumbnails directory exists
THUMBNAILS_DIR = os.path.join('static', 'thumbnails')
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

# Async utilities
async def check_membership(bot, user_id):
    try:
        m = await bot.get_chat_member(UPDATES_CHANNEL, user_id)
        return m.status not in ['left', 'kicked']
    except Exception as e:
        logger.error(f"Error checking membership for user {user_id}: {e}")
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

async def save_thumbnail(file_id, key):
    try:
        file = await sync_bot.get_file(file_id)
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        buf.seek(0)
        thumbnail_path = os.path.join(THUMBNAILS_DIR, f"{key}.jpg")
        with open(thumbnail_path, 'wb') as f:
            f.write(buf.read())
        return f"static/thumbnails/{key}.jpg"
    except Exception as e:
        logger.error(f"Failed to save thumbnail for key {key}: {e}")
        return None

async def validate_token(bot):
    try:
        await bot.get_me()
        logger.info("Bot token validated successfully")
        return True
    except InvalidToken as e:
        logger.error(f"Invalid bot token: {e}")
        return False
    except Exception as e:
        logger.error(f"Error validating bot token: {e}")
        return False

def register_handlers():
    async def channel_media(update: Update, context):
        logger.info(f"Received update: {update.to_dict()}")
        msg = update.message
        if not msg:
            logger.debug("Received update with no message")
            return
        if not msg.chat.type in ['channel', 'supergroup']:
            logger.debug(f"Received non-channel message: chat_type={msg.chat.type}")
            return
        logger.info(f"Processing channel post from chat ID {msg.chat.id} (type: {type(msg.chat.id)})")
        # Map channel ID to category
        channel_map = {
            MOVIES_CHANNEL_ID: 'movies',
            ADULT_CHANNEL_ID: 'adult',
            ANIME_CHANNEL_ID: 'anime'
        }
        logger.info(f"Expected channel IDs: Movies={MOVIES_CHANNEL_ID}, Adult={ADULT_CHANNEL_ID}, Anime={ANIME_CHANNEL_ID}")
        category = channel_map.get(msg.chat.id)
        if not category:
            logger.warning(f"Unknown channel ID {msg.chat.id} (expected: {channel_map})")
            return
        media = msg.video or msg.document
        if not media:
            logger.debug(f"No media in channel post from {msg.chat.id}")
            return
        key = f"file_{media.file_unique_id}"
        # Prevent duplicate indexing
        if videos.find_one({'custom_key': key}):
            logger.info(f"File {key} already indexed, skipping")
            return
        thumbnail_path = None
        thumb_url = f"/thumbnails/{key}.jpg"
        thumbnail_file_id = None
        if msg.video and hasattr(media, 'thumb') and media.thumb:
            thumbnail_file_id = media.thumb.file_id
            thumbnail_path = await save_thumbnail(thumbnail_file_id, key)
        try:
            videos.insert_one({
                'file_id': media.file_id,
                'custom_key': key,
                'title': msg.caption or 'Untitled',
                'thumbnail_url': thumb_url,
                'thumbnail_path': thumbnail_path,
                'thumbnail_file_id': thumbnail_file_id,
                'type': 'video' if msg.video else 'document',
                'category': category
            })
            logger.info(f"Successfully indexed file {key} for category {category}")
        except Exception as e:
            logger.error(f"Failed to index file {key} for category {category}: {e}")

    async def start_command(update: Update, context):
        logger.info(f"Received /start command: {update.to_dict()}")
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
        context.job_queue.run_once(
            delete_message_job,
            when=SELF_DESTRUCT,
            data={'chat_id': update.effective_chat.id, 'message_id': sent.message_id}
        )

    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.VIDEO | filters.Document.ALL), channel_media))
    application.add_handler(CommandHandler('start', start_command))

def register_routes():
    @app.route('/')
    def index():
        vids = list(videos.find({'category': 'movies'}).sort('_id', -1))
        return render_template('index.html', videos=vids, bot_username=BOT_USERNAME)

    @app.route('/adult')
    def adult():
        vids = list(videos.find({'category': 'adult'}).sort('_id', -1))
        return render_template('adult.html', videos=vids, bot_username=BOT_USERNAME)

    @app.route('/anime')
    def anime():
        vids = list(videos.find({'category': 'anime'}).sort('_id', -1))
        return render_template('anime.html', videos=vids, bot_username=BOT_USERNAME)

    @app.route('/file/<key>')
    def file_page(key):
        rec = videos.find_one({'custom_key': key})
        if not rec:
            return "File not found", 404
        return render_template('file.html', key=key, thumb_url=rec.get('thumbnail_url', ''), title=rec.get('title', 'Untitled'), bot_username=BOT_USERNAME)

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
            response = make_response(send_from_directory('static/thumbnails', f"{key}.jpg"))
            response.headers['Cache-Control'] = 'public, max-age=31536000'
            logger.info(f"Successfully served thumbnail for key: {key}")
            return response
        except FileNotFoundError:
            logger.error(f"Thumbnail file missing for key: {key}")
            return send_from_directory('static', 'fallback.jpg'), 200

    @app.route('/webhook', methods=['POST'])
    async def webhook():
        update = Update.de_json(request.get_json(), application.bot)
        await application.process_update(update)
        return '', 200

# Migrate thumbnails asynchronously
async def migrate_thumbnails(bot):
    if not await validate_token(bot):
        logger.warning("Skipping thumbnail migration due to invalid token")
        return
    for rec in videos.find({'thumbnail_path': None, 'thumbnail_file_id': {'$exists': True}}):
        key = rec['custom_key']
        try:
            thumbnail_path = await save_thumbnail(rec['thumbnail_file_id'], key)
            if thumbnail_path:
                videos.update_one({'custom_key': key}, {'$set': {'thumbnail_path': thumbnail_path}})
                logger.info(f"Migrated thumbnail for key: {key}")
            else:
                logger.warning(f"Failed to migrate thumbnail for key: {key}")
        except Exception as e:
            logger.error(f"Error migrating thumbnail for key {key}: {e}")

# Run Flask and Telegram bot in webhook mode
def run_flask():
    app.run(host='0.0.0.0', port=PORT)

async def run_webhook():
    if not await validate_token(sync_bot):
        logger.error("Cannot start webhook: Invalid bot token")
        raise InvalidToken("Bot token is invalid")
    if not KOYEB_PUBLIC_DOMAIN:
        logger.error("KOYEB_PUBLIC_DOMAIN not set")
        raise ValueError("KOYEB_PUBLIC_DOMAIN environment variable is required")
    webhook_url = f"https://{KOYEB_PUBLIC_DOMAIN}/webhook"
    try:
        await sync_bot.delete_webhook(drop_pending_updates=True)
        logger.info("Existing webhook cleared successfully")
        await sync_bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook set to {webhook_url}")
        await application.run_webhook(listen='0.0.0.0', port=PORT, webhook_url=webhook_url)
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

async def main():
    logger.info(f"Starting bot with channel IDs: Movies={MOVIES_CHANNEL_ID} (type: {type(MOVIES_CHANNEL_ID)}), Adult={ADULT_CHANNEL_ID} (type: {type(ADULT_CHANNEL_ID)}), Anime={ANIME_CHANNEL_ID} (type: {type(ANIME_CHANNEL_ID)})")
    await migrate_thumbnails(sync_bot)
    register_handlers()
    register_routes()
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    await run_webhook()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
