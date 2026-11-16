FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ is mounted from the host so the database survives rebuilds.
VOLUME ["/app/data"]

EXPOSE 8765
# Create/upgrade the DB, then run the bot.
CMD ["sh", "-c", "python db/init_db.py && python bot/bot.py"]
