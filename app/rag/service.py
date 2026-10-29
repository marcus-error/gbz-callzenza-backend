from app.rag.retriver import get_context

user_query = "What is the purpose of CallZenza?"
context = get_context(user_query)

prompt = f"""
Answer the user's question using the following context.

Context:
{context}

Question:
{user_query}
"""

if __name__ == "__main__":
    print("Generated Prompt:\n", prompt)