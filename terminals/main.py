"""FastAPI application assembly."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from terminals.backends import create_backend
from terminals.config import settings
from terminals.db.session import close_db, init_db
from terminals.logging import setup_logging
from terminals.middleware import RequestIdMiddleware
from terminals.routers.auth import close_auth_client
from terminals.routers.lifecycle import router as lifecycle_router
from terminals.routers.policy import router as policy_router
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


# Policy CRUD must be before the catch-all proxy.
app.include_router(policy_router)
app.include_router(lifecycle_router)

FRONTEND_BUILD_DIR = Path(__file__).parent / "frontend" / "build"
if FRONTEND_BUILD_DIR.exists():
    if settings.enable_ui:
        app.mount(
            "/_app",
            StaticFiles(directory=str(FRONTEND_BUILD_DIR / "_app")),
            name="frontend-assets",
        )

        @app.get("/")
        async def serve_frontend():
            return FileResponse(FRONTEND_BUILD_DIR / "index.html")

        @app.get("/favicon.svg")
        async def serve_favicon():
            favicon = FRONTEND_BUILD_DIR / "favicon.svg"
            if not favicon.exists():
                raise HTTPException(status_code=404)
            return FileResponse(favicon)
    else:

        @app.get("/")
        async def disabled_frontend():
            raise HTTPException(status_code=404)

        @app.get("/_app/{path:path}")
        async def disabled_frontend_assets(path: str):
            raise HTTPException(status_code=404)

# Catch-all proxy router must be last so /health and /api are matched first.
app.include_router(proxy_router)
