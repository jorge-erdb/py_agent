import logging
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path

# Configure logging before importing project modules — they log at import time
# (config reports which file it loaded), and those records are dropped if no
# handler is installed yet.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from fastapi.staticfiles import StaticFiles  # noqa: E402
import api.main  # noqa: E402
from api.main import app, SessionManager  # noqa: E402
from database.db import DatabaseManager  # noqa: E402

@asynccontextmanager
async def lifespan(app):
    # Initialize database
    db_dir = Path(__file__).parent / "data"
    db_dir.mkdir(exist_ok=True)
    db_path = str(db_dir / "agent.db")
    db = DatabaseManager(db_path)
    await db.initialize()

    # Create session manager with database and wire it into api.main
    api.main.session_manager = SessionManager(db=db)
    sm = api.main.session_manager

    # Load existing sessions from database
    await sm.load_sessions()

    # Start the session cleanup background task
    asyncio.create_task(sm.cleanup_task())
    yield

    # Close database connection on shutdown
    await db.close()

# Serve the frontend at the root URL
FRONTEND_DIR = Path(__file__).parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logging.info("Frontend mounted at /")
else:
    logging.warning(f"Frontend directory not found at {FRONTEND_DIR}")

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    import config
    host = config.get("server", "host", "SERVER_HOST", "0.0.0.0")
    port = config.get("server", "port", "SERVER_PORT", 8000, cast=int)
    # Inside a container, binding 0.0.0.0 is correct and expected — what the
    # port is published to on the host is what governs exposure, and this
    # process cannot see that. Only warn when running directly on the host.
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if host == "0.0.0.0" and not in_container:
        logging.warning(
            "Binding to 0.0.0.0 — the agent and its shell tool are reachable "
            "from your network, with no authentication. Set SERVER_HOST=127.0.0.1 "
            "to restrict to this machine. See SECURITY.md."
        )
    uvicorn.run(app, host=host, port=port, reload=False)
