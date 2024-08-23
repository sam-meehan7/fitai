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

# Set up logging for console output only
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Reduce noise from httpx library
logging.getLogger("httpx").setLevel(logging.WARNING)

# Initialize OpenAI API key and Assistant ID from environment variables
openai_api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# Define conversation states
SHARE_CONTACT, GET_AGE, GET_WEIGHT, GET_HEIGHT, ONGOING = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) started the conversation")
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

    logger.info(f"Received contact information for user {telegram_id}: {context.user_data['name']}, {context.user_data['phone']}")

    # Check if user exists in database
    user_response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if user_response.data:
        user = user_response.data[0]
        context.user_data.update(user)
        logger.info(f"Existing user {telegram_id} found in database")
        await get_or_create_session(update, context)
        await update.message.reply_text(f"Welcome back, {context.user_data['name']}! How can I assist you today?", reply_markup=ReplyKeyboardRemove())
        return ONGOING
    else:
        logger.info(f"New user {telegram_id} - requesting age")
        await update.message.reply_text("Thanks for sharing your contact. Now, please tell me your age.")
        return GET_AGE

async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text)
        if age <= 0 or age > 120:
            raise ValueError()
        context.user_data['age'] = age
        logger.info(f"User {context.user_data['telegram_id']} age: {age}")
        await update.message.reply_text("Great! Now, what's your current weight in kg?")
        return GET_WEIGHT
    except ValueError:
        logger.warning(f"Invalid age input from user {context.user_data['telegram_id']}: {update.message.text}")
        await update.message.reply_text("Please enter a valid age between 1 and 120.")
        return GET_AGE

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text)
        if weight <= 0 or weight > 500:
            raise ValueError()
        context.user_data['weight'] = weight
        logger.info(f"User {context.user_data['telegram_id']} weight: {weight} kg")
        await update.message.reply_text("Excellent! Lastly, what's your height in cm?")
        return GET_HEIGHT
    except ValueError:
        logger.warning(f"Invalid weight input from user {context.user_data['telegram_id']}: {update.message.text}")
        await update.message.reply_text("Please enter a valid weight in kg (e.g., 70.5).")
        return GET_WEIGHT

async def get_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        height = float(update.message.text)
        if height <= 0 or height > 300:
            raise ValueError()
        context.user_data['height'] = height
        logger.info(f"User {context.user_data['telegram_id']} height: {height} cm")
        return await finalize_profile(update, context)
    except ValueError:
        logger.warning(f"Invalid height input from user {context.user_data['telegram_id']}: {update.message.text}")
        await update.message.reply_text("Please enter a valid height in cm (e.g., 175.5).")
        return GET_HEIGHT

async def get_or_create_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = context.user_data['id']

    # Try to get the most recent session for the user
    session_response = supabase.table("assistant_sessions").select("*").eq("user_id", user_id).order("id", desc=True).limit(1).execute()

    if session_response.data:
        # Existing session found
        session = session_response.data[0]
        context.user_data['thread_id'] = session['thread_id']
        context.user_data['session_id'] = session['id']
        logger.info(f"Retrieved existing session {session['id']} with thread {session['thread_id']} for user {user_id}")

        # If the session state is not 'ONGOING', update it
        if session['state'] != 'ONGOING':
            supabase.table("assistant_sessions").update({"state": "ONGOING"}).eq("id", session['id']).execute()
            logger.info(f"Updated session {session['id']} state to ONGOING for user {user_id}")
    else:
        # No existing session, create a new one
        thread = create_thread()
        context.user_data['thread_id'] = thread.id
        logger.info(f"Created new thread {thread.id} for user {user_id}")

        session_data = {
            "user_id": user_id,
            "thread_id": thread.id,
            "state": "ONGOING"
        }
        session_response = supabase.table("assistant_sessions").insert(session_data).execute()
        context.user_data['session_id'] = session_response.data[0]['id']
        logger.info(f"Created new session {context.user_data['session_id']} for user {user_id}")

    # Ensure the thread exists and is accessible
    try:
        # might want to add a function to check if the thread exists in your assistant module
        # For now, we'll just log that we're using this thread
        logger.info(f"Using thread {context.user_data['thread_id']} for user {user_id}")
    except Exception as e:
        logger.error(f"Error accessing thread {context.user_data['thread_id']} for user {user_id}: {str(e)}")
        # If there's an error, create a new thread
        thread = create_thread()
        context.user_data['thread_id'] = thread.id
        supabase.table("assistant_sessions").update({"thread_id": thread.id}).eq("id", context.user_data['session_id']).execute()
        logger.info(f"Created new thread {thread.id} for user {user_id} due to error")

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
        logger.info(f"User profile finalized and saved to database: {user_data}")

        await get_or_create_session(update, context)

        profile_summary = "\n".join([f"{key.capitalize()}: {value}" for key, value in user_data.items()])
        await update.message.reply_text("Thanks for providing your information. I've forwarded it to one of our PTs, they will be with you shortly!")

        create_message(context.user_data['thread_id'], f"New user profile:\n{profile_summary}")
        run = create_run(context.user_data['thread_id'])
        run = wait_on_run(run, context.user_data['thread_id'])

        messages = list_messages(context.user_data['thread_id']).data
        response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

        logger.info(f"Assistant response for user {context.user_data['telegram_id']}: {response[:100]}...")  # Log first 100 chars of response
        await update.message.reply_text(response, parse_mode='Markdown')
        return ONGOING

    except Exception as e:
        logger.error(f"Error in finalize_profile for user {context.user_data['telegram_id']}: {str(e)}")
        await update.message.reply_text("I'm sorry, but there was an error processing your information. Please try again later or contact support.")
        return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_message = update.message.text
    thread_id = context.user_data['thread_id']
    user_id = context.user_data['telegram_id']

    logger.info(f"Received message from user {user_id}: {user_message[:50]}...")  # Log first 50 chars of user message

    create_message(thread_id, user_message)
    run = create_run(thread_id)
    run = wait_on_run(run, thread_id)

    messages = list_messages(thread_id).data
    response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

    logger.info(f"Assistant response for user {user_id}: {response[:100]}...")  # Log first 100 chars of response
    await update.message.reply_text(response, parse_mode='Markdown')
    return ONGOING

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} cancelled the conversation")
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
    logger.info("FitAI Telegram Bot started")
    application.run_polling()

if __name__ == '__main__':
    main()