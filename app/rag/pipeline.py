from app.rag.rag_retriever import retrieve_chunks
from app.gemini import generate_answer


def rag_pipeline(query, vector_store):

    # Retrieve relevant information
    chunks = retrieve_chunks(
        query,
        vector_store,
        top_k=3
    )

    # Send retrieved information to Gemini
    answer = generate_answer(
        query,
        chunks
    )

    return answer
