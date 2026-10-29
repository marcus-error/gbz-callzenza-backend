import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    api_key = api_key.strip()

client = genai.Client(
    api_key=api_key
)

# Use client.models.generate_content(model="gemini-1.5-flash", contents=...) for calls
