FROM python:3.12-slim

WORKDIR /app

# Cài dependency ở layer riêng: đổi code không phải cài lại toàn bộ.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Chạy bằng user thường thay vì root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
