from itsdangerous import URLSafeSerializer

COOKIE_NAME = 'wsl_ops_session'


def build_session_serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret_key=secret, salt='wsl-ops-panel')
