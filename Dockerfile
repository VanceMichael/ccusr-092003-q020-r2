
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
RUN python -m scripts.migrate
ENV PORT=8080 DATABASE_PATH=/data/app.sqlite3
EXPOSE 8080
CMD ["python", "-m", "app.main"]
