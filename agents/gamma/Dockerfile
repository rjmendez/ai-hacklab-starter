FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY a2a/server.py       ./a2a/server.py
COPY a2a/watchdog.py     ./a2a/watchdog.py
COPY agents/gamma/agent_card.py ./agent_card.py
COPY agents/gamma/skill_handlers.py ./skill_handlers.py

ENV AGENT_NAME=gamma
ENV A2A_PORT=8203
EXPOSE 8203

CMD ["python", "a2a/server.py"]
