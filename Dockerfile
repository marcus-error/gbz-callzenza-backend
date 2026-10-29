FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create writable directories for logs and static files
RUN mkdir -p app/static/audio app/static/uploads

EXPOSE 8000

# Railway injects PORT automatically; fall back to 8000 for local Docker usage.
CMD sh -c "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level info"
