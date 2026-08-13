FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY sql ./sql
CMD ["sh", "-c", "attempt=1; while ! alembic upgrade head; do if [ \"$attempt\" -ge 5 ]; then echo 'Migration failed after 5 attempts'; exit 1; fi; delay=$((attempt * 5)); echo \"Migration attempt $attempt failed; retrying in ${delay}s\"; sleep \"$delay\"; attempt=$((attempt + 1)); done; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

