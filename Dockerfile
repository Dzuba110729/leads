FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База и telethon-сессии живут в /data (volume) — не в слое образа
VOLUME ["/data"]
ENV DB_PATH=/data/og1_leads.db

EXPOSE 8080

CMD ["python", "main.py"]
