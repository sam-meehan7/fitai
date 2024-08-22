import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from assistant import create_thread, create_message, create_run, wait_on_run, list_messages
from dotenv import load_dotenv
from supabase.client import create_client, Client
from openai import BadRequestError
from openai.types.beta.threads.run import Run

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

# Define user profile storage
user_profiles = {}

# Define questions and their corresponding states
QUESTIONS = {
    'NAME': "What's your name?",
    'EMAIL': "Please provide your email address.",
    'AGE': "How old are you?",
    'WEIGHT': "What is your current weight?",
    'HEIGHT': "What is your height?",
}

# Create conversation states dynamically
STATES = {key: i for i, key in enumerate(QUESTIONS.keys())}
STATES['ONGOING'] = len(STATES)

async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_profiles[chat_id] = {}

    # Create the thread at the start of the conversation
    thread = create_thread()
    context.user_data['thread'] = thread

    await context.bot.send_message(chat_id=chat_id, text="Hi there! Welcome to FitAI. Let's get started with some basic information about you and we can get it over to one of our Personal Trainers and get you started.")
    return await ask_next_question(update, context)

async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    current_state = context.user_data.get('state', -1)

    if current_state >= 0:
        # Save the answer to the previous question
        question_key = list(QUESTIONS.keys())[current_state]
        answer = update.message.text

        # Validate the answer if the question is 'AGE'
        if question_key == 'AGE':
            try:
                age = int(answer)
                if age <= 0:
                    raise ValueError("Age must be a positive integer.")
                user_profiles[chat_id][question_key.lower()] = age
            except ValueError:
                await context.bot.send_message(chat_id=chat_id, text="Please enter a valid age (must be a number).")
                return current_state
        else:
            user_profiles[chat_id][question_key.lower()] = answer

    next_state = current_state + 1
    if next_state < len(QUESTIONS):
        question = list(QUESTIONS.values())[next_state]
        await context.bot.send_message(chat_id=chat_id, text=question)
        context.user_data['state'] = next_state
        return next_state
    else:
        return await finalize_profile(update, context)

async def finalize_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_profile = user_profiles[chat_id]

    try:
        # Create user in Supabase with collected information
        user_data = {
            "name": user_profile['name'],
            "email": user_profile['email'],
            "age": int(user_profile['age']),
            "weight": user_profile['weight'],
            "height": user_profile['height'],
        }

        user_response = supabase.table("users").upsert(user_data, on_conflict="email").execute()
        user_id = user_response.data[0]['id']

        # Get the thread that was created at the start of the conversation
        thread = context.user_data['thread']
        if not thread or not thread.id:
            raise Exception("Thread not found or invalid")

        # Create assistant session in Supabase with initial state 'STARTED'
        session_data = {
            "user_id": user_id,
            "thread_id": thread.id,
            "state": "STARTED"
        }
        session_response = supabase.table("assistant_sessions").insert(session_data).execute()
        session_id = session_response.data[0]['id']

        context.user_data['user_id'] = user_id
        context.user_data['session_id'] = session_id

        profile_summary = "\n".join([f"{key.capitalize()}: {value}" for key, value in user_profile.items()])
        await context.bot.send_message(chat_id=chat_id, text=f"Thanks for providing your information. I've forwarded it to one of our PTs, they will be with you shortly!")

        # Proceed to interacting with the LLM
        create_message(thread.id, profile_summary)
        logger.info(f"Created message in thread ID: {thread.id}")

        run = create_run(thread.id)
        run = wait_on_run(run, thread)
        logger.info(f"Run status: {run.status}")

        messages_page = list_messages(thread.id)
        messages = messages_page.data if hasattr(messages_page, 'data') else []
        logger.info(f"Retrieved messages: {messages}")

        response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

        # Update the state to 'ONGOING' after LLM has responded
        supabase.table("assistant_sessions").update({"state": "ONGOING"}).eq("id", session_id).execute()

        logger.info(f"Assistant's response: {response}")
        await context.bot.send_message(chat_id=chat_id, text=response, parse_mode='Markdown')

        return STATES['ONGOING']

    except Exception as e:
        logger.error(f"Error in finalize_profile: {str(e)}")
        await context.bot.send_message(chat_id=chat_id, text="I'm sorry, but there was an error processing your information. Please try again later or contact support.")
        return ConversationHandler.END

async def handle_ongoing_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_message = update.message.text

    thread = context.user_data['thread']
    create_message(thread.id, user_message)
    logger.info(f"Created message in thread ID: {thread.id}")

    try:
        run = create_run(thread.id)
    except BadRequestError as e:
        if "already has an active run" in str(e):
            # Get the active run
            runs = client.beta.threads.runs.list(thread_id=thread.id, limit=1)
            if runs.data:
                active_run = runs.data[0]
                # Wait for the active run to complete
                run = wait_on_run(active_run, thread)
            else:
                logger.error(f"No active run found for thread {thread.id}")
                await context.bot.send_message(chat_id=chat_id, text="I'm sorry, but there was an error processing your request. Please try again later.")
                return STATES['ONGOING']
        else:
            raise e

    run = wait_on_run(run, thread)
    logger.info(f"Run status: {run.status}")

    messages_page = list_messages(thread.id)
    messages = messages_page.data if hasattr(messages_page, 'data') else []
    logger.info(f"Retrieved messages: {messages}")

    response = next((msg.content[0].text.value for msg in messages if msg.role == 'assistant'), "No response from assistant.")

    logger.info(f"Assistant's response: {response}")
    await context.bot.send_message(chat_id=chat_id, text=response, parse_mode='Markdown')

    return STATES['ONGOING']

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await context.bot.send_message(chat_id=update.effective_chat.id, text="The conversation has been cancelled.")
    return ConversationHandler.END

def main():
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_KEY")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_conversation)],
        states={
            **{state: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_next_question)] for state in STATES.values() if state != STATES['ONGOING']},
            STATES['ONGOING']: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ongoing_conversation)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()