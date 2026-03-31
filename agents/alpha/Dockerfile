FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# A2A server (shared) + agent card
COPY a2a/server.py       ./a2a/server.py
COPY a2a/watchdog.py     ./a2a/watchdog.py
COPY agents/alpha/agent_card.py ./agent_card.py
COPY agents/alpha/skill_handlers.py ./skill_handlers.py

ENV AGENT_NAME=alpha
ENV A2A_PORT=8201
EXPOSE 8201

CMD ["python", "a2a/server.py"]
