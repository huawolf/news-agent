FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY prompts ./prompts
COPY resources ./resources
COPY config.json.example README.md LICENSE ./
COPY test_prompt_language.py ./

RUN python -m pip install --no-cache-dir .

EXPOSE 12301

CMD ["python", "-m", "src.main", "serve", "--host", "0.0.0.0", "--port", "12301"]
