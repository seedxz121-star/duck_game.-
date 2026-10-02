import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]          # التوكن من BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]    # رابط HTTPS للعبة، مثل https://yourname.github.io/duck-game/


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🦆 العب الآن", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await update.message.reply_text("أهلاً بك في مزرعة البط! افقس البيض واجمع البط.", reply_markup=kb)


app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.run_polling()
