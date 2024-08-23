import os
import logging
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from assistant import create_thread, create_message, create_run, wait_on_run, list_messages
from dotenv import load_dotenv
from supabase.client import create_client, Client
from openai import BadRequestError

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize OpenAI API key and Assistant ID from environment variables
openai_api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# Define conversation states
SHARE_CONTACT, GET_AGE, GET_WEIGHT, GET_HEIGHT, ONGOING = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[KeyboardButton("Share Contact", request_contact=True)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    await update.message.reply_text(
        "Welcome to FitAI! To get started, please share your contact information.",
        reply_markup=reply_markup
    )
    return SHARE_CONTACT

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    telegram_id = update.effective_user.id
    context.user_data['telegram_id'] = telegram_id
    context.user_data['phone'] = contact.phone_number
    context.user_data['name'] = f"{contact.first_name or ''} {contact.last_name or ''}".strip()

    # Check if user exists in database
    user_response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if user_response.data:
        user = user_response.data[0]
        context.user_data.update(user)
        await update_or_create_session(update, context)
        await update.message.reply_text(f"Welcome back, {context.user_data['name']}! How can I assist you today?", reply_markup=ReplyKeyboardRemove())
        return ONGOING
    else:
        await update.message.reply_text("Thanks for sharing your contact. Now, please tell me your age.")
        return GET_AGE

async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text)
        if age <= 0 or age > 120:
            raise ValueError()
        context.user_data['age'] = age
        await update.message.reply_text("Great! Now, what's your current weight in kg?")
        return GET_WEIGHT
    except ValueError:
        await update.message.reply_text("Please enter a valid age between 1 and 120.")
        return GET_AGE

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text)
        if weight <= 0 or weight > 500:
            raise ValueError()
        context.user_data['weight'] = weight
        await update.message.reply_text("Excellent! Lastly, what's your height in cm?")
        return GET_HEIGHT
    except ValueError:
        await update.message.reply_text("Please enter a valid weight in kg (e.g., 70.5).")
        return GET_WEIGHT

async def get_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        height = float(update.message.text)
        if height <= 0 or height > 300:
            raise ValueError()
        context.user_data['height'] = height
        return await finalize_profile(update, context)
    except ValueError:
        await update.message.reply_text("Please enter a valid height in cm (e.g., 175.5).")
        return GET_HEIGHT

async def update_or_create_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread = create_thread()
    context.user_data['thread_id'] = thread.id

    session_data = {
        "user_id": context.user_data['id'],
        "thread_id": thread.id,
        "state": "ONGOING"
    }
    session_response = supabase.table("assistant_sessions").insert(session_data).execute()
    context.user_data['session_id'] = session_response.data[0]['id']

async def finalize_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_data = {
            "telegram_id": context.user_data['telegram_id'],
            "name": context.user_data['name'],
            "phone": context.user_data['phone'],
            "age": context.user_data['age'],
            "weight": context.user_data['weight'],
            "height": context.user_data['height'],
        }

        user_response = supabase.table("users").upsert(user_data, on_conflict="telegram_id").execute()
        context.user_data['id'] = user_response.data[0]['id']

        await update_or_create_session(update, context)

        profile_summary = "\n".join([f"{key.capitalize()}: {value}" for key, value in user_data.items()])
        await update.message.reply_text("Thanks for providing your information. I've forwarded it to one of our PTs, they will be with you shortly!")

        create_message(context.user_data['thread_id'], f"New user profile:\n{profile_summary}")
        run = create_run(context.user_data['thread_id'])
        run = wait_on_run(run, context.user_data['thread_id'])

        messages = list_messages(context.user_data['thread_id']).data
        response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

        await update.message.reply_text(response, parse_mode='Markdown')
        return ONGOING

    except Exception as e:
        logger.error(f"Error in finalize_profile: {str(e)}")
        await update.message.reply_text("I'm sorry, but there was an error processing your information. Please try again later or contact support.")
        return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_message = update.message.text
    thread_id = context.user_data['thread_id']

    create_message(thread_id, user_message)
    run = create_run(thread_id)
    run = wait_on_run(run, thread_id)

    messages = list_messages(thread_id).data
    response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

    await update.message.reply_text(response, parse_mode='Markdown')
    return ONGOING

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("The conversation has been cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_KEY")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SHARE_CONTACT: [MessageHandler(filters.CONTACT, handle_contact)],
            GET_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age)],
            GET_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_weight)],
            GET_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_height)],
            ONGOING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()