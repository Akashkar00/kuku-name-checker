# Name-Drift Checker web app for Cloud Run.
# Only the code and the public demo show are copied in (explicit allowlist), so
# confidential scripts or outputs in this folder can never end up in the image.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --require-hashes --only-binary=:all: -r requirements.txt \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY app.py serve.py name_checker.py llm_judge.py ./
COPY .streamlit/config.toml .streamlit/config.toml
COPY samples/demo_show/ samples/demo_show/

# Code is owned by root and read-only for the app user.
USER 10001
EXPOSE 8080
# serve.py = app.py + security headers (see its docstring)
CMD ["sh", "-c", "exec streamlit run serve.py --server.port=${PORT:-8080} --server.address=0.0.0.0"]
