from app.rag.rag_retriever import retrieve_chunks

class MockVectorStore:
    def search(self, query_embedding, top_k=3):
        return [
            {"text": "CallZenza is an advanced AI calling agent platform."},
            {"text": "RAG enables CallZenza to answer accurately using custom uploaded files."}
        ]

vector_store = MockVectorStore()

def get_context(query):
    chunks = retrieve_chunks(
        query,
        vector_store,
        top_k=3
    )

    return "\n\n".join(chunks)