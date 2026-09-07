FROM python:3.12-slim

# Same reasoning as the main Dockerfile's own comment: print()/logging
# output must not sit invisible in Python's stdout block-buffer when
# not attached to a TTY.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY risk_adapter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY risk_adapter/main.py .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
