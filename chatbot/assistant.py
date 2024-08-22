import json
import os
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def show_json(obj):
    return json.dumps(json.loads(obj.model_dump_json()), indent=2)

def create_thread():
    return client.beta.threads.create()

def create_message(thread_id, content):
    return client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=content
    )

def create_run(thread_id):
    return client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=os.getenv("ASSISTANT_ID")
    )

def wait_on_run(run, thread_id):
    while run.status == "queued" or run.status == "in_progress":
        run = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id,
        )
        time.sleep(0.5)
    return run

def list_messages(thread_id):
    return client.beta.threads.messages.list(thread_id=thread_id)
