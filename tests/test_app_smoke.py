from fastapi.testclient import TestClient

from tests.app_factory import create_app


def test_healthcheck_route_exists() -> None:
    with TestClient(create_app()) as client:
        response = client.get('/healthz')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
