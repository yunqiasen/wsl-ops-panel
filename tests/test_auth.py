from fastapi.testclient import TestClient

from app.main import create_app


def test_login_sets_session_cookie() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    response = client.post('/auth/login', data={'username': '875133228', 'password': 'qaz11789652'})
    assert response.status_code == 302
    assert 'wsl_ops_session' in response.cookies


def test_invalid_login_rejected() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    response = client.post('/auth/login', data={'username': '875133228', 'password': 'bad'})
    assert response.status_code == 401


def test_logout_clears_session_cookie() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    client.post('/auth/login', data={'username': '875133228', 'password': 'qaz11789652'})
    response = client.post('/auth/logout')
    assert response.status_code == 302
    assert client.cookies.get('wsl_ops_session') is None
