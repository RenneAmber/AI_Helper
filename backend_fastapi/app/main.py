from fastapi import FastAPI

from .config import settings
from .core.logging_setup import configure_logging
from .database import engine
from .middleware.request_context import add_trace_and_logs
from .models import Base
from .decision.models import Base as DecisionBase
from .routers.chat import router as chat_router
from .routers.health import router as health_router
from .routers.medical import router as medical_router
from .routers.decisions import router as decisions_router
from .routers.copilot import router as copilot_router
from .routers.capability_planning import router as capability_planning_router

configure_logging()
app = FastAPI(title=settings.app_name)
app.middleware("http")(add_trace_and_logs)


@app.on_event("startup")
async def on_startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(DecisionBase.metadata.create_all)


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(medical_router)
app.include_router(decisions_router)
app.include_router(copilot_router)
app.include_router(capability_planning_router)
