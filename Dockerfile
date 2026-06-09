# Image portable pour Fly.io / Oracle Cloud / Railway / tout hôte Docker.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# DB_PATH permet de pointer vers un volume persistant (sinon ./backend/paper_trading.db)
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
