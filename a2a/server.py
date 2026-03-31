from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional, Dict
import os
import pyotp
import importlib

app = FastAPI()

KNOWN_PEERS = {}  # Populate with peer agent tokens

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/.well-known/agent-card.json")
async def agent_card():
    return {
        "name": os.getenv("AGENT_NAME", "Unnamed Agent"),
        "url": os.getenv("AGENT_URL", "http://localhost"),
    }

@app.post("/a2a")
async def a2a_post(skill_id: str, input: Dict, authorization: Optional[str] = Header(None)):
    if authorization != f"Bearer {os.getenv('AGENT_TOKEN', '')}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if os.getenv("AGENT_TOTP_SEED"):
        otp = pyotp.TOTP(os.getenv("AGENT_TOTP_SEED")).now()
        if input.get("totp") != otp:
            raise HTTPException(status_code=403, detail="TOTP verification failed")

    response = {"skill_id": skill_id, "input": input}
    return JSONResponse(content=response)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("A2A_HOST", "0.0.0.0"), port=int(os.getenv("A2A_PORT", "8000")))