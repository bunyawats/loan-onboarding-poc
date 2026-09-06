FROM python:3.12-slim

# Python block-buffers stdout when it isn't attached to a TTY (true of
# every process in this image) -- print()-based output (this project's
# deliberate choice over the logging module, see notifications/service.py's
# send_verification_code) can sit invisible in that buffer indefinitely
# under low output volume, never reaching `docker compose logs` even
# though the code ran. Found live in P20-3's own verification. Set
# unconditionally, not per-service, since every process here relies on
# print() being actually visible.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
COPY loan_onboarding/ ./loan_onboarding/

RUN pip install --no-cache-dir .

# No CMD here on purpose -- docker-compose.yml sets the command per
# service (uvicorn for the web process, `python -m
# loan_onboarding.worker_main` for the workers), same convention as
# review-approval-temporal's Dockerfile: one image, several entrypoints.
