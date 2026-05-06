from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.settings import load_panel_config

COOKIE_NAME = 'wsl_ops_session'
_SESSION_SALT = 'wsl-ops-panel-session'


def issue_session_token(username: str | None = None) -> str:
    cfg = load_panel_config()
    subject = username or cfg.auth.username
    serializer = _build_serializer()
    return serializer.dumps({'username': subject})


def is_authenticated_request(request: Request) -> bool:
    return _is_valid_session_token(request.cookies.get(COOKIE_NAME))


def require_authenticated_request(request: Request) -> None:
    if is_authenticated_request(request):
        return

    headers = None
    if request.headers.get('hx-request') == 'true':
        headers = {'HX-Redirect': '/login', 'X-Login-Redirect': '/login'}
    raise HTTPException(status_code=401, detail='authentication required', headers=headers)


def is_authenticated_websocket(websocket: WebSocket) -> bool:
    return _is_valid_session_token(websocket.cookies.get(COOKIE_NAME))


def _is_valid_session_token(token: str | None) -> bool:
    if not token:
        return False

    serializer = _build_serializer()
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return False

    cfg = load_panel_config()
    return payload.get('username') == cfg.auth.username


def _build_serializer() -> URLSafeSerializer:
    cfg = load_panel_config()
    secret = f'{cfg.auth.username}:{cfg.auth.password}:{cfg.host}:{cfg.port}'
    return URLSafeSerializer(secret, salt=_SESSION_SALT)
