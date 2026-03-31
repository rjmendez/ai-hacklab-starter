FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY a2a/server.py       ./a2a/server.py
COPY a2a/watchdog.py     ./a2a/watchdog.py
COPY agents/delta/agent_card.py ./agent_card.py
COPY agents/delta/skill_handlers.py ./skill_handlers.py

ENV AGENT_NAME=delta
ENV A2A_PORT=8204
EXPOSE 8204

CMD ["python", "a2a/server.py"]
