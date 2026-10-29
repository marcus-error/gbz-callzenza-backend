from app.services.stt_service import get_genai_client

client = get_genai_client()
for m in client.models.list():
    if "generateContent" in (m.supported_actions or []):
        print(m.name)
