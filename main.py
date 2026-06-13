"""
main.py

FastAPI application entry point.
Mounts all routers and configures CORS.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy on Render:
    Render detects uvicorn from the start command automatically.
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from core.logger import setup_logging
from loguru import logger
from api.verify import router as verify_router
from api.auth import router as auth_router
from api.webhooks import router as webhooks_router
from auth.middleware import JWTMiddleware

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SQLVerify starting up")
    yield
    logger.info("SQLVerify shutting down")


app = FastAPI(
    title="SQLVerify",
    description="Formal verification for AI-generated SQL queries.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — tighten in production to your actual frontend domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# JWT auth middleware
app.add_middleware(JWTMiddleware)

# Routers
app.include_router(verify_router)
app.include_router(auth_router)
app.include_router(webhooks_router)


# Mount static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

# Templates
templates = Jinja2Templates(directory="web/templates")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "{method} {path} → {status} ({duration}ms)",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration=duration_ms,
    )
    return response


@app.on_event("startup")
async def startup():
    logger.info("SQLVerify starting up")


@app.on_event("shutdown")
async def shutdown():
    logger.info("SQLVerify shutting down")


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/verify")
async def verify_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={"user_email": user_email},
    )


@app.get("/pricing")
async def pricing_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context={"user_email": user_email},
    )
