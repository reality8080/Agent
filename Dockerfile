FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/agent-system
COPY src/adapters/requirements-adapters.txt /tmp/requirements.txt
RUN sed '/^# /d; /^$/d' /tmp/requirements.txt > /tmp/requirements-clean.txt \
    && pip install --no-cache-dir -r /tmp/requirements-clean.txt pytest

# Test target is intentionally non-root. Runtime tests mount only the workspace read-only.
RUN useradd --create-home --uid 10001 agent
USER 10001:10001
WORKDIR /workspace
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short"]
