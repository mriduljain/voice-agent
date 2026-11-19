from fastapi import FastAPI

from app.exotel_adapter import router

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(router)

# uv run uvicorn app.exotel_test_server:app --host 127.0.0.1 --port 8001
# cloudflared tunnel --url http://127.0.0.1:8001
# curl https://random-words.trycloudflare.com/telephony/exotel/health
