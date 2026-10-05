from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.docker_adapter import DockerComposeAdapter
from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.models.assets import AssetSnapshot
from app.services.notifications import NotificationService, render_notification_template
from app.tasks.store import InMemoryTaskStore


def _write_registry(root: Path) -> None:
    (root / 'categories').mkdir(parents=True, exist_ok=True)
    (root / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (root / 'objects').mkdir(parents=True, exist_ok=True)
    (root / 'objects' / 'cpa.yaml').write_text(
        'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\nconfig:\n'
        '  project_dir: /srv/cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: cli-proxy-api\n  compose_service: cli-proxy-api\n'
        '  endpoints:\n'
        '    - label: API / 管理面板入口\n'
        '      host_port: "8317"\n'
        '      container_port: "8317"\n'
        '      path: /management.html\n'
        '      primary: true\n',
        encoding='utf-8',
    )


def _docker_containers():
    from app.scanners.docker_scanner import parse_docker_ps_lines

    return parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"eceasy/cli-proxy-api:latest",'
            '"Labels":"com.docker.compose.project=cpa,com.docker.compose.project.working_dir=/srv/cpa,'
            'com.docker.compose.service=cli-proxy-api,org.opencontainers.image.source=https://github.com/itseasy21/CLIProxyAPI",'
            '"Names":"cli-proxy-api","State":"running","Status":"Up 1 hour","Ports":"0.0.0.0:8317->8317/tcp"}'
        ]
    )


def test_render_notification_template_replaces_project_fields() -> None:
    asset = AssetSnapshot(
        object_id='cpa',
        category='docker',
        name='CPA',
        status='running',
        metadata={
            'project_dir': '/srv/cpa',
            'ports': '0.0.0.0:8317->8317/tcp',
            'service_endpoints': [
                {
                    'label': 'API / 管理面板入口',
                    'host_port': '8317',
                    'container_port': '8317',
                    'path': '/management.html',
                    'tailscale_url': 'http://100.126.43.55:8317/management.html',
                    'primary': True,
                }
            ],
            'image_repository': 'eceasy/cli-proxy-api',
            'source_links': {'github': 'https://github.com/itseasy21/CLIProxyAPI'},
        },
    )

    rendered = render_notification_template(
        '项目 {{ asset.name }}\n状态 {{ asset.status }}\nTS {{ tailscale_url }}\n端点 {{ endpoint_lines }}\n来源 {{ source_url }}',
        asset,
        tailscale_ip='100.126.43.55',
    )

    assert '项目 CPA' in rendered
    assert '状态 running' in rendered
    assert 'TS http://100.126.43.55:8317/management.html' in rendered
    assert 'API / 管理面板入口：http://100.126.43.55:8317/management.html' in rendered
    assert '来源 https://github.com/itseasy21/CLIProxyAPI' in rendered


def test_notification_service_saves_config_and_returns_preview(tmp_path: Path) -> None:
    service = NotificationService(tmp_path)
    asset = AssetSnapshot(object_id='cpa', category='docker', name='CPA', status='running', metadata={'ports': '8317/tcp'})

    saved = service.save_project_config('cpa', enabled=True, title='CPA 链接', template='{{ asset.name }} {{ ports }}')
    preview = service.preview(asset)

    assert saved.title == 'CPA 链接'
    assert preview.title == 'CPA 链接'
    assert preview.content == 'CPA 8317/tcp'
    assert (tmp_path / 'notifications' / 'projects.yaml').exists()


def test_docker_notify_plan_uses_panel_notification_cli_not_startup_service() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/cpa',
        compose_file='docker-compose.yml',
        primary_container='cli-proxy-api',
        compose_service='cli-proxy-api',
        config_root='/panel/config',
        asset_snapshot_json='{"object_id":"cpa","category":"docker","name":"CPA","status":"running"}',
    )

    plan = adapter.plan_action('notify_send')

    joined = ' '.join(plan.commands[0])
    assert 'app.services.notifications' in joined
    assert 'startup-notify.service' not in joined
    assert '--asset-id' in plan.commands[0]
    assert 'cpa' in plan.commands[0]


def test_notification_api_save_preview_and_send_enqueue(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=_docker_containers, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    get_response = client.get('/api/notifications/cpa')
    assert get_response.status_code == 200
    assert get_response.json()['asset_id'] == 'cpa'
    assert 'CPA' in get_response.json()['preview']['content']

    save_response = client.post(
        '/api/notifications/cpa',
        json={'enabled': True, 'title': 'CPA 通知', 'template': '链接 {{ tailscale_url }}'},
    )
    assert save_response.status_code == 200
    assert save_response.json()['config']['title'] == 'CPA 通知'
    assert '链接 http://100.126.43.55:8317/management.html' in save_response.json()['preview']['content']

    send_response = client.post('/api/notifications/cpa/send')
    assert send_response.status_code == 202
    assert send_response.json()['task']['action'] == 'notify_send'
    assert store.list_all()[0].action == 'notify_send'


def test_registered_docker_notification_reads_cf_file_and_formats_ports(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    docker_assets = _docker_containers()
    docker_assets[0] = docker_assets[0].model_copy(
        update={'ports': '0.0.0.0:8317->8317/tcp, [::]:8317->8317/tcp, 0.0.0.0:11451->11451/tcp'}
    )
    service = NotificationService(tmp_path)
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: docker_assets, task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    cf_logs = tmp_path / 'fake-cpa' / 'logs'
    cf_logs.mkdir(parents=True)
    (cf_logs / 'cftunnel-domain.txt').write_text('https://demo.trycloudflare.com\n', encoding='utf-8')
    asset = client.app.state.asset_service.get_asset('cpa')
    assert asset is not None
    asset = asset.model_copy(
        update={
            'metadata': {
                **asset.metadata,
                'project_dir': str(cf_logs.parent),
            }
        }
    )

    rendered = service.preview(asset).content

    assert '首选TS：http://100.126.43.55:8317/management.html' in rendered
    assert 'CF：https://demo.trycloudflare.com' in rendered
    assert 'API / 管理面板入口：http://100.126.43.55:8317/management.html' in rendered
    assert '原始端口：8317/tcp, 11451/tcp' in rendered
