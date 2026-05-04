from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.auth import router as auth_router
from app.api.terminals import router as terminals_router

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


def create_app() -> FastAPI:
    app = FastAPI(title='WSL Ops Panel')
    app.include_router(auth_router)
    app.include_router(terminals_router)

    @app.get('/healthz')
    def healthcheck() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/login', response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, 'login.html')

    @app.get('/terminals', response_class=HTMLResponse)
    def terminals_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, 'terminals.html')

    return app


app = create_app()
