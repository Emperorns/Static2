import os
import io
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, send_from_directory, make_response
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, File
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
import logging
import asyncio
import nest_asyncio
import subprocess

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

def get_thumb_path(key):
    return os.path.join(THUMBNAILS_DIR, f"{key}.jpg")

async def save_thumbnail(file_id, key):
    try:
        tg_file: File = await sync_bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        buf.seek(0)
        path = get_thumb_path(key)
        with open(path, 'wb') as f:
            f.write(buf.read())
        return path
    except Exception as e:
        logger.error(f"Failed to save thumbnail for {key}: {e}")
        return None

async def extract_thumbnail(media, key):
    # Try Telegram thumbnail
    thumb_id = None
    if hasattr(media, 'thumbnail') and media.thumbnail:
        thumb = media.thumbnail if not isinstance(media.thumbnail, list) else media.thumbnail[-1]
        thumb_id = thumb.file_id
    elif hasattr(media, 'thumb') and media.thumb:
        thumb_id = media.thumb.file_id
    if thumb_id:
        return await save_thumbnail(thumb_id, key)
    # Fallback: generate from video via ffmpeg
    try:
        video_info = await sync_bot.get_file(media.file_id)
        temp = io.BytesIO()
        await video_info.download_to_memory(out=temp)
        temp_path = os.path.join(THUMBNAILS_DIR, f"{key}_tmp.mp4")
        with open(temp_path, 'wb') as f:
            f.write(temp.getvalue())
        out_path = get_thumb_path(key)
        subprocess.run([
            'ffmpeg', '-i', temp_path, '-ss', '00:00:01', '-vframes', '1', out_path
        ], check=True)
        os.remove(temp_path)
        return out_path
    except Exception as e:
        logger.error(f"FFmpeg thumbnail generation failed for {key}: {e}")
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
    return rec and 'last_verified' in rec and (datetime.utcnow() - rec['last_verified'] < VERIFY_INTERVAL)

async def require_access(update: Update, context):
    uid = update.effective_user.id
    if not await check_membership(context.bot, uid):
        btn = InlineKeyboardButton("Join Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL.strip('@')}")
        await update.message.reply_text("🚨 Please join the updates channel.", reply_markup=InlineKeyboardMarkup([[btn]]))
        return False
    if not await is_verified(uid):
        btns = [[InlineKeyboardButton("Verify Human", url=CAPTCHA_URL)],
                [InlineKeyboardButton("How to Solve Captcha", url=TUTORIAL_URL)]]
        await update.message.reply_text("🛡️ Please verify human to proceed.", reply_markup=InlineKeyboardMarkup(btns))
        return False
    return True

async def delete_job(context):
    d = context.job.data
    try:
        await context.bot.delete_message(chat_id=d['chat_id'], message_id=d['message_id'])
    except Exception as e:
        logger.error(f"Delete job error: {e}")

async def handle_media(update: Update, context):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    msg = update.message
    media = msg.video or msg.document
    if not media:
        return
    key = f"file_{media.file_unique_id}"
    # forward to channel
    sent = await context.bot.forward_message(CHANNEL_ID, msg.chat.id, msg.message_id)
    fid = sent.video.file_id if sent.video else sent.document.file_id
    # extract thumb
    thumb_path = await extract_thumbnail(media, key)
    videos.insert_one({
        'file_id': fid,
        'custom_key': key,
        'title': msg.caption or 'Untitled',
        'type': 'video' if sent.video else 'document',
        'thumbnail_path': thumb_path
    })
    await context.bot.send_message(ADMIN_ID, f"✅ Indexed {key}")

async def channel_media(update: Update, context):
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return
    media = post.video or post.document
    key = f"file_{media.file_unique_id}"
    fid = media.file_id
    thumb_path = await extract_thumbnail(media, key)
    videos.insert_one({
        'file_id': fid,
        'custom_key': key,
        'title': post.caption or 'Untitled',
        'type': 'video' if post.video else 'document',
        'thumbnail_path': thumb_path
    })

async def start_cmd(update: Update, context):
    args = context.args
    uid = update.effective_user.id
    if args and args[0]=='verified':
        users.update_one({'user_id':uid},{'$set':{'last_verified':datetime.utcnow()}},upsert=True)
        await context.bot.send_message(LOG_CHANNEL, f"🔐 {uid} verified")
        await update.message.reply_text("✅ Verified for 2h")
        return
    if not await require_access(update, context): return
    if not args:
        btn=InlineKeyboardButton("How to Use",url=TUTORIAL_URL)
        await update.message.reply_text("👋 Welcome!",reply_markup=InlineKeyboardMarkup([[btn]]))
        return
    key=args[0]
    rec=videos.find_one({'custom_key':key})
    if not rec:
        await update.message.reply_text("❌ Media not found.")
        return
    send= context.bot.send_video if rec['type']=='video' else context.bot.send_document
    sent = await send(update.effective_chat.id,rec['file_id'],caption=rec['title'],protect_content=True)
    context.job_queue.run_once(delete_job,when=SELF_DESTRUCT,data={'chat_id':update.effective_chat.id,'message_id':sent.message_id})


def register_handlers():
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.ALL),handle_media))
    application.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & (filters.VIDEO | filters.Document.ALL),channel_media))
    application.add_handler(CommandHandler('start',start_cmd))


def register_routes():
    @app.route('/')
    def index():
        vids=list(videos.find().sort('_id',-1))
        return render_template('index.html',videos=vids,bot_username=BOT_USERNAME)
    @app.route('/file/<key>')
    def file_page(key):
        rec=videos.find_one({'custom_key':key})
        if not rec: return "Not found",404
        return render_template('file.html',key=key,thumb_url=f"/thumbnails/{key}.jpg",title=rec['title'],bot_username=BOT_USERNAME)
    @app.route('/thumbnails/<key>.jpg')
    def serve_thumb(key):
        path=get_thumb_path(key)
        if os.path.exists(path):
            return send_from_directory('static/thumbnails',f"{key}.jpg")
        return send_from_directory('static','fallback.jpg')

async def migrate_thumbs():
    for r in videos.find({'thumbnail_path':None}):
        key=r['custom_key']
        await extract_thumbnail(type('M',(object,),{'file_id':r['file_id']})(),key)


def run_flask(): app.run(host='0.0.0.0',port=PORT)
async def main():
    await migrate_thumbs()
    register_handlers()
    register_routes()
    Thread(target=run_flask,daemon=True).start()
    await application.run_polling()
if __name__=='__main__': asyncio.run(main())
```
