FROM python:3.12-slim
WORKDIR /app

# Install dependencies
# Note: Ollama must run on the host or a sidecar — this container calls it via HTTP
RUN pip install --no-cache-dir fastapi uvicorn pyotp redis requests ollama

COPY ../../a2a/server.py ./a2a/server.py
COPY agent_card.py .

ENV AGENT_NAME=beta
ENV A2A_PORT=8200
# Point to your Ollama instance
ENV OLLAMA_HOST=http://host.docker.internal:11434

EXPOSE 8200
CMD ["python", "a2a/server.py"]
