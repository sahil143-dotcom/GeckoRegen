FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV FLASK_DEBUG=0
ENV PORT=8000
EXPOSE 8000

CMD gunicorn server:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 620
