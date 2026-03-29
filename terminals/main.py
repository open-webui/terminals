"""FastAPI application assembly."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from terminals.backends import create_backend
from terminals.config import settings
from terminals.db.session import close_db, init_db
from terminals.logging import setup_logging
from terminals.middleware import RequestIdMiddleware
from terminals.routers.auth import close_auth_client
from terminals.routers.proxy import close_proxy_client, router as proxy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Run DB migrations first (alembic's fileConfig reconfigures logging).
    init_db()

    # Set up loguru AFTER alembic so our InterceptHandler isn't overwritten.
    setup_logging()

    app.state.backend = create_backend()

    # Recover state from any running containers (survives process restart).
    if hasattr(app.state.backend, "reconcile"):
        await app.state.backend.reconcile()

    app.state.backend.start_reaper()

    yield

    await app.state.backend.stop_reaper()
    await close_proxy_client()
    await close_auth_client()
    await app.state.backend.close()
    await close_db()


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


from terminals.routers.policy import router as policy_router

# Policy CRUD must be before the catch-all proxy.
app.include_router(policy_router)

# Catch-all proxy router must be last so /health and /api are matched first.
app.include_router(proxy_router)

