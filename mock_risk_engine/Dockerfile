FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY mock_risk_engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mock_risk_engine/main.py .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
