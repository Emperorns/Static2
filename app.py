import os
import pathlib
import asyncio
from threading import Thread
from datetime import datetime, timedelta
from flask import Flask, render_template, send_from_directory, jsonify, make_response
from pymongo import MongoClient
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters

# Load environment variables
load_dotenv()
BOT_TOKEN        = os.getenv('BOT_TOKEN')
MONGODB_URI      = os.getenv('MONGODB_URI')
DB_NAME          = os.getenv('DB_NAME')
ADMIN_ID         = int(os.getenv('ADMIN_ID'))
CHANNEL_ID       = int(os.getenv('CHANNEL_ID'))
UPDATES_CHANNEL  = os.getenv('UPDATES_CHANNEL')        # e.g. '@updates_channel'
CAPTCHA_URL      = os.getenv('CAPTCHA_URL')            # Link to captcha verification endpoint
TUTORIAL_URL     = os.getenv('TUTORIAL_URL') 
H_URL            = os.getenv('H_URL')# Tutorial on solving captcha
LOG_CHANNEL      = os.getenv('LOG_CHANNEL')            # Chat ID or @username for logging verifications
BOT_USERNAME     = os.getenv('BOT_USERNAME')
PUBLIC_URL       = os.getenv('PUBLIC_URL')
PORT             = int(os.getenv('PORT', 5000))
VERIFY_INTERVAL  = timedelta(hours=2)

# Flask app
app = Flask(__name__)

# MongoDB setup
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
videos = db.videos
users = db.users    # store user verification timestamps

# Thumbnails directory
THUMB_DIR = os.path.join(os.getcwd(), 'thumbnails')
pathlib.Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)

# Initialize Telegram bot
application = ApplicationBuilder().token(BOT_TOKEN).build()

async def download_thumbnail(bot, thumb, key):
    thumb_path = os.path.join(THUMB_DIR, f"{key}.jpg")
    file = await bot.get_file(thumb.file_id)
    await file.download_to_drive(thumb_path)
    return f"{PUBLIC_URL}/thumbnails/{key}.jpg"

async def check_membership(bot, user_id):
    try:
        member = await bot.get_chat_member(UPDATES_CHANNEL, user_id)
        return member.status not in ['left', 'kicked']
    except Exception:
        return False

async def is_verified(user_id):
    record = users.find_one({'user_id': user_id})
    if not record:
        return False
    last = record.get('last_verified')
    if not last:
        return False
    return datetime.utcnow() - last < VERIFY_INTERVAL

async def require_access(update: Update, context):
    user_id = update.effective_user.id
    # 1. Ensure user is in updates channel
    if not await check_membership(context.bot, user_id):
        join_button = InlineKeyboardButton(
            text="Join Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL.strip('@')}"
        )
        markup = InlineKeyboardMarkup([[join_button]])
        await update.message.reply_text(
            "🚨 You must join our updates channel to use this bot,then again send /start command. ", reply_markup=markup
        )
        return False
    # 2. Ensure user has passed captcha within last 2 hours
    if not await is_verified(user_id):
        verify_btn = InlineKeyboardButton(text="Get file", url=CAPTCHA_URL)
        tutorial_btn = InlineKeyboardButton(text="How to open✅", url=TUTORIAL_URL)
        markup = InlineKeyboardMarkup([[verify_btn], [tutorial_btn]])
        await update.message.reply_text(
            "🛡️ click on get file button to download your file. ", 
            reply_markup=markup
        )
        return False
    return True

# Handler for admin uploads
async def handle_media(update: Update, context):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    msg = update.message
    media = msg.video or msg.document
    if not media:
        return
    key = f"file_{media.file_unique_id}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔄 Processing admin upload {key}...")
    thumb_url = ''
    if msg.video:
        thumb_attr = getattr(media, 'thumbnail', None) or getattr(media, 'thumb', None)
        if thumb_attr:
            photo = thumb_attr[-1] if isinstance(thumb_attr, list) else thumb_attr
            thumb_url = await download_thumbnail(context.bot, photo, key)
    sent = await context.bot.forward_message(
        chat_id=CHANNEL_ID,
        from_chat_id=msg.chat.id,
        message_id=msg.message_id
    )
    file_id = sent.video.file_id if sent.video else sent.document.file_id
    media_type = 'video' if sent.video else 'document'
    title = msg.caption or (getattr(sent.document, 'file_name', '') if sent.document else 'Untitled')
    videos.insert_one({
        'file_id': file_id,
        'custom_key': key,
        'title': title,
        'thumbnail_url': thumb_url,
        'type': media_type
    })
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Admin {media_type} saved: {title} ({key})")

# Handler for channel posts
async def channel_media(update: Update, context):
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return
    media = post.video or post.document
    if not media:
        return
    key = f"file_{media.file_unique_id}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔄 Processing channel media {key}...")
    thumb_url = ''
    if post.video:
        thumb_attr = getattr(media, 'thumbnail', None) or getattr(media, 'thumb', None)
        if thumb_attr:
            photo = thumb_attr[-1] if isinstance(thumb_attr, list) else thumb_attr
            thumb_url = await download_thumbnail(context.bot, photo, key)
    file_id = media.file_id
    media_type = 'video' if post.video else 'document'
    title = post.caption or (getattr(media, 'file_name', '') if media else 'Untitled')
    videos.insert_one({
        'file_id': file_id,
        'custom_key': key,
        'title': title,
        'thumbnail_url': thumb_url,
        'type': media_type
    })
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Channel {media_type} saved: {title} ({key})")

# /start handler
async def start_command(update: Update, context):
    args = context.args
    user_id = update.effective_user.id
    # Catch verification callback
    if args and args[0] == 'verified':
        users.update_one(
            {'user_id': user_id},
            {'$set': {'last_verified': datetime.utcnow()}},
            upsert=True
        )
        # Log to channel
        await context.bot.send_message(
            chat_id=LOG_CHANNEL,
            text=f"🔐 User {user_id} verified at {datetime.utcnow().isoformat()}"
        )
        await update.message.reply_text("✅ You Earned a token! You can now use the bot for the next 2 hours without any ads. ")
        return
    # Check membership & captcha
    if not await require_access(update, context):
        return
    # Serve media if valid key, or send welcome
    if not args:
        tutorial_btn = InlineKeyboardButton(text="Movies", url=H_URL)
        markup = InlineKeyboardMarkup([[tutorial_btn]])
        await update.message.reply_text("👋 Welcome! Use a valid deep link to access a file.", reply_markup=markup)
        return
    key = args[0]
    data = videos.find_one({'custom_key': key})
    if not data:
        await update.message.reply_text("❌ Media not found.")
        return
    send_kwargs = {'caption': data.get('title', ''), 'protect_content': True}
    if data['type'] == 'video':
        await context.bot.send_video(update.effective_chat.id, data['file_id'], **send_kwargs)
    else:
        await context.bot.send_document(update.effective_chat.id, data['file_id'], **send_kwargs)

# Register handlers
application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.ALL), handle_media))
application.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & (filters.VIDEO | filters.Document.ALL), channel_media))
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
    return jsonify(list(videos.find({}, {'_id': 0}).sort('_id', -1)))

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
