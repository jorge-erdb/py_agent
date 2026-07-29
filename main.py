import logging
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.staticfiles import StaticFiles
import api.main
from api.main import app, SessionManager
from database.db import DatabaseManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

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
    import os
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False)
