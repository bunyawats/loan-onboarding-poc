FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY loan_onboarding/ ./loan_onboarding/

RUN pip install --no-cache-dir .

# No CMD here on purpose -- docker-compose.yml sets the command per
# service (uvicorn for the web process, `python -m
# loan_onboarding.worker_main` for the workers), same convention as
# review-approval-temporal's Dockerfile: one image, several entrypoints.
