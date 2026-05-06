from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.core.security import COOKIE_NAME, issue_session_token
from app.core.settings import load_panel_config

router = APIRouter(prefix='/auth', tags=['auth'])


@router.post('/login')
def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    cfg = load_panel_config()
    if username != cfg.auth.username or password != cfg.auth.password:
        raise HTTPException(status_code=401, detail='invalid credentials')
    response = RedirectResponse(url='/', status_code=302)
    response.set_cookie(COOKIE_NAME, issue_session_token(username), httponly=True, samesite='lax')
    return response


@router.post('/logout')
def logout() -> RedirectResponse:
    response = RedirectResponse(url='/login', status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
