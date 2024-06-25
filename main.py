import os
import logging
import asyncio
import time
import random
import requests
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, ChatMemberHandler
from dotenv import load_dotenv
import uvicorn

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
FLOWISE_BOT_1_URL = os.getenv('FLOWISE_BOT_1_URL')
FLOWISE_BOT_1_TOKEN = os.getenv('FLOWISE_BOT_1_TOKEN')
FLOWISE_BOT_2_URL = os.getenv('FLOWISE_BOT_2_URL')
FLOWISE_BOT_2_TOKEN = os.getenv('FLOWISE_BOT_2_TOKEN')
PORT = int(os.getenv('PORT', 10001))

user_data = {}

app = FastAPI()

class FlowiseBot:
    def __init__(self, api_url, api_token):
        self.api_url = api_url
        self.api_token = api_token

    async def get_response(self, question, user_id, session_id):
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
        }
        payload = {
            "question": question,
            "userId": str(user_id),
            "overrideConfig": {
                "sessionId": session_id,
                "returnSourceDocuments": True
            }
        }

        max_retries = 5
        base_delay = 1

        for attempt in range(max_retries):
            try:
                response = await asyncio.to_thread(requests.post, self.api_url, json=payload, headers=headers)
                response.raise_for_status()
                return response.json().get('text', 'No response text in Flowise API response')
            except requests.RequestException as error:
                if attempt == max_retries - 1:
                    logging.error('Error sending to Flowise API after %d attempts: %s', max_retries, error)
                    if error.response:
                        logging.error('Response content: %s', error.response.content)
                    return "Error occurred while contacting Flowise API."
                else:
                    delay = (2 ** attempt) * base_delay + random.uniform(0, 1)
                    logging.warning(f'Attempt {attempt + 1} failed, retrying in {delay:.2f} seconds...')
                    await asyncio.sleep(delay)

        return "Maximum retries reached. Unable to contact Flowise API."

def get_user_bots(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            'bot1': FlowiseBot(api_url=FLOWISE_BOT_1_URL, api_token=FLOWISE_BOT_1_TOKEN),
            'bot2': FlowiseBot(api_url=FLOWISE_BOT_2_URL, api_token=FLOWISE_BOT_2_TOKEN),
            'waiting': False,
            'last_wait_time': 0,
            'message_queue': [],
            'wait_task': None,
            'session_id': str(user_id)
        }
    return user_data[user_id]

def clean_response(text):
    cleaned_text = text.replace('#', '').replace('*', '')
    cleaned_text = '\n'.join(line.strip() for line in cleaned_text.split('\n'))
    return cleaned_text.strip()

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    text = update.message.text

    try:
        logging.info('Incoming message from user %d: %s', user_id, text)

        user_bots = get_user_bots(user_id)
        bot1 = user_bots['bot1']
        session_id = user_bots['session_id']

        response1 = await bot1.get_response(text, user_id, session_id)
        logging.info('Bot1 response: %s', response1)

        if "Спецзапрос" in response1:
            if user_bots['wait_task']:
                user_bots['wait_task'].cancel()
            user_bots['message_queue'].append(text)
            await process_bot2_messages(update, context, user_id)
        elif response1.strip() == "Уточнение":
            if user_bots['wait_task']:
                user_bots['wait_task'].cancel()
            user_bots['message_queue'].append(f"{text} Уточнение")
            await process_bot2_messages(update, context, user_id)
        elif response1 == "Ожидание":
            user_bots['waiting'] = True
            user_bots['last_wait_time'] = time.time()
            user_bots['message_queue'].append(text)
            if user_bots['wait_task']:
                user_bots['wait_task'].cancel()
            user_bots['wait_task'] = asyncio.create_task(wait_and_check(update, context, user_id))
        else:
            cleaned_response = clean_response(response1)
            await context.bot.send_message(chat_id=chat_id, text=cleaned_response)

    except Exception as error:
        logging.error('Error handling Telegram message for user %d: %s', user_id, error)

async def wait_and_check(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user_bots = user_data[user_id]
    while True:
        await asyncio.sleep(10)
        if time.time() - user_bots['last_wait_time'] >= 10:
            user_bots['waiting'] = False
            await process_bot2_messages(update, context, user_id)
            break

async def process_bot2_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user_bots = user_data[user_id]
    bot2 = user_bots['bot2']
    chat_id = update.message.chat_id
    session_id = user_bots['session_id']

    messages = user_bots['message_queue']
    user_bots['message_queue'] = []

    combined_question = " ".join(messages)

    response2 = await bot2.get_response(combined_question, user_id, session_id)
    logging.info('Bot2 response: %s', response2)

    if response2:
        cleaned_response = clean_response(response2)
        await context.bot.send_message(chat_id=chat_id, text=cleaned_response)

async def chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chat_member
    if result.new_chat_member.user.id == context.bot.id:
        return
    
    if result.new_chat_member.status == "member":
        context.user_data['typing'] = True
    else:
        context.user_data['typing'] = False

@app.get("/")
async def root():
    return {"message": "Hello, this is the FastAPI application"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

async def run_telegram_bot():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
    application.add_handler(ChatMemberHandler(chat_member_updated))

    await application.initialize()
    await application.start()
    
    print("Starting Telegram bot")
    await application.run_polling(drop_pending_updates=True)

def run_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

async def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    print("Starting the application...")
    
    # Run the FastAPI app in a separate thread
    fastapi_thread = asyncio.to_thread(run_fastapi)
    
    # Run both the FastAPI app and Telegram bot concurrently
    await asyncio.gather(
        fastapi_thread,
        run_telegram_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
