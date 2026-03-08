"""FastAPI application assembly."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from terminals.backends import create_backend
from terminals.config import settings
from terminals.logging import setup_logging
from terminals.middleware import RequestIdMiddleware
from terminals.routers.proxy import close_proxy_client, router as proxy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    setup_logging()

    app.state.backend = create_backend()

    yield

    await close_proxy_client()
    await app.state.backend.close()


app = FastAPI(
    title="Terminals",
    description="Multi-tenant terminal orchestrator for Open Terminal.",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url=None,  # Disable built-in OpenAPI; proxy router serves the terminal spec
)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": True}


# Catch-all proxy router must be last so /health is matched first.
app.include_router(proxy_router)


# ---------------------------------------------------------------------------
# Serve the SvelteKit static frontend (must be last — catch-all mount)
# ---------------------------------------------------------------------------
_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend" / "build"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
