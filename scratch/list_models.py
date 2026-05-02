import os
from dotenv import load_dotenv
from google import genai

# Try to find .env
load_dotenv()
load_dotenv('../.env')

api_key = os.getenv('GEMINI_API_KEY')
if not api_key:
    print("No API KEY found")
    exit(1)

client = genai.Client(api_key=api_key)
try:
    models = client.models.list()
    for m in models:
        print(m.name)
except Exception as e:
    print(f"Error listing models: {e}")
