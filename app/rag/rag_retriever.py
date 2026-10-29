import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

def embed_query(query):
    import time
    max_retries = 3
    delay = 1.0
    for attempt in range(max_retries):
        try:
            response = client.models.embed_content(
                model='gemini-embedding-001',
                contents=query
            )
            return response.embeddings[0].values
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
            else:
                raise exc

def retrieve_chunks(query, vector_store, top_k=3):
    query_embedding = embed_query(query)

    results = vector_store.search(
        query_embedding,
        top_k=top_k
    )

    return [result["text"] for result in results]