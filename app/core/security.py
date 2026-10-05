from pathlib import Path

from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.settings import load_panel_config

COOKIE_NAME = 'wsl_ops_session'
_SESSION_SALT = 'wsl-ops-panel-session'


def issue_session_token(username: str | None = None, *, config_root: Path | str = Path('config')) -> str:
    cfg = load_panel_config(config_root)
    subject = username or cfg.auth.username
    serializer = _build_serializer(config_root)
    return serializer.dumps({'username': subject})


def is_authenticated_request(request: Request) -> bool:
    return _is_valid_session_token(request.cookies.get(COOKIE_NAME), request.app.state.config_root)


def require_authenticated_request(request: Request) -> None:
    if is_authenticated_request(request):
        return

    headers = None
    if request.headers.get('hx-request') == 'true':
        headers = {'HX-Redirect': '/login', 'X-Login-Redirect': '/login'}
    raise HTTPException(status_code=401, detail='authentication required', headers=headers)


def is_authenticated_websocket(websocket: WebSocket) -> bool:
    return _is_valid_session_token(websocket.cookies.get(COOKIE_NAME), websocket.app.state.config_root)


def _is_valid_session_token(token: str | None, config_root: Path | str = Path('config')) -> bool:
    if not token:
        return False

    serializer = _build_serializer(config_root)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return False

    cfg = load_panel_config(config_root)
    return payload.get('username') == cfg.auth.username


def _build_serializer(config_root: Path | str = Path('config')) -> URLSafeSerializer:
    cfg = load_panel_config(config_root)
    secret = f'{cfg.auth.username}:{cfg.auth.password}:{cfg.host}:{cfg.port}'
    return URLSafeSerializer(secret, salt=_SESSION_SALT)
