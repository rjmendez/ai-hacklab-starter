FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pyotp psycopg2-binary redis requests

# Copy A2A server and agent card
COPY ../../a2a/server.py ./a2a/server.py
COPY agent_card.py .

ENV AGENT_NAME=alpha
ENV A2A_PORT=8200
EXPOSE 8200

CMD ["python", "a2a/server.py"]
