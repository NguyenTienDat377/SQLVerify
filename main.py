"""
main.py

FastAPI application entry point.
Mounts all routers and configures CORS.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy on Railway:
    Railway detects uvicorn from Procfile automatically.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.verify import router as verify_router
from api.auth import router as auth_router
from auth.middleware import JWTMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="SQLVerify",
    description="Formal verification for AI-generated SQL queries.",
    version="0.1.0",
)

# CORS — tighten in production to your actual frontend domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# JWT auth middleware (replaces static API key middleware)
app.add_middleware(JWTMiddleware)

# Routers
app.include_router(auth_router)
app.include_router(verify_router)

# Mount static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

# Templates
templates = Jinja2Templates(directory="web/templates")


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
