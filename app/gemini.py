import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    api_key = api_key.strip()

# Create Gemini client
client = genai.Client(
    api_key=api_key
)


def generate_answer(query, chunks):
    """
    Generate an answer using Gemini based on retrieved RAG chunks.
    """

    context = "\n\n".join(chunks)

    prompt = f"""
You are a helpful AI assistant.

Answer the user's question using the provided context.

Rules:
- Use the context as the primary source of information.
- Do not invent information that is not supported by the context.
- If the answer cannot be found in the context, clearly say that you don't have enough information.
- Give a concise and natural answer.

Context:
{context}

User Question:
{query}

Answer:
"""

    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt
    )

    return response.text