"""Real HTTP/worker/subprocess round trip against a file-backed Docker fixture."""
import json
from pathlib import Path
import sys
from time import monotonic, sleep

from fastapi.testclient import TestClient

from app.adapters.base import ActionPlan
from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.models.assets import DockerContainerSnapshot
from tests.test_runtime_reliability import config_at


DOCKER_FIXTURE = r'''
import json, os, sys
from pathlib import Path
path = Path(os.environ['WSL_DOCKER_FIXTURE'])
data = json.loads(path.read_text())
args = sys.argv[1:]
rows = data['containers']
if args[:2] == ['ps', '-aq']:
    print('\n'.join(c['Id'] for c in rows))
elif args[:2] == ['container', 'inspect']:
    print(json.dumps([c for c in rows if c['Id'] in args[2:]]))
elif args[:1] == ['start']:
    for c in rows:
        if c['Id'] in args[1:]: c['State']['Running'] = True
elif args[:2] == ['rm', '-f']:
    data['containers'] = [c for c in rows if c['Id'] not in args[2:]]
elif args[:1] == ['compose']:
    if 'up' in args:
        data['generation'] += 1
        c = json.loads(json.dumps(data['template']))
        c['Id'] = format(data['generation'], '064x')
        c['State']['Running'] = True
        data['containers'] = [c]
    elif 'config' not in args and 'pull' not in args:
        raise SystemExit(11)
elif args[:2] not in (['network', 'ls'], ['volume', 'ls']):
    raise SystemExit(12)
path.write_text(json.dumps(data))
'''


def test_api_worker_start_update_delete_and_restart_after_bad_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    config = config_at(tmp_path / 'config')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'compose.yml').write_text('services:\n  web:\n    image: fixture:latest\n')
    (config / 'categories/docker.yaml').write_text('id: docker\nlabel: Docker\norder: 1\n')
    (config / 'objects/demo.yaml').write_text(
        'id: demo\ncategory: docker\ntype: docker_compose\nname: Demo\nconfig:\n'
        f'  project_dir: {project}\n  compose_file: compose.yml\n  compose_project: fixture\n'
        '  compose_service: web\n  primary_container: fixture-web\n'
    )
    template = {
        'Id': '0' * 64, 'Name': '/fixture-web', 'Image': 'fixture:latest',
        'Config': {'Labels': {'com.docker.compose.project': 'fixture',
            'com.docker.compose.project.working_dir': str(project), 'com.docker.compose.service': 'web'}},
        'State': {'Running': False}, 'Mounts': [],
    }
    state_file = tmp_path / 'docker-fixture.json'
    state_file.write_text(json.dumps({'containers': [template], 'template': template, 'generation': 0}))
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    docker = binary_dir / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + DOCKER_FIXTURE)
    docker.chmod(0o700)
    monkeypatch.setenv('WSL_DOCKER_FIXTURE', str(state_file))
    # Only the fixture executable is visible to the Docker lifecycle subprocess.
    monkeypatch.setenv('PATH', str(binary_dir))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))

    def scan():
        return [DockerContainerSnapshot(id=c['Id'], name='fixture-web', image='fixture:latest',
                status='running' if c['State']['Running'] else 'exited', compose_project='fixture',
                compose_service='web', compose_working_dir=str(project), labels=c['Config']['Labels'])
                for c in json.loads(state_file.read_text())['containers']]

    app = create_app(config_root=config, docker_scanner=scan)
    bad = app.state.task_queue.enqueue('broken', 'start', plan=ActionPlan(commands=[]))
    Path(bad.plan_path).write_text('{bad')
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, issue_session_token(config_root=config))
        for action in ['start', 'update-latest', 'delete', 'start', 'full-delete']:
            response = client.post(f'/api/assets/demo/actions/{action}')
            assert response.status_code == 202, response.text
            task_id = response.json()['task']['id']
            deadline = monotonic() + 15
            while monotonic() < deadline:
                task = app.state.task_store.get(task_id)
                if task.status not in {'queued', 'running'}:
                    break
                sleep(.02)
            assert task.status == 'succeeded', Path(task.stderr_log_path).read_text() if Path(task.stderr_log_path).exists() else task.status
            rows = json.loads(state_file.read_text())['containers']
            if action in {'delete', 'full-delete'}:
                assert not rows
            else:
                assert rows and rows[0]['State']['Running']
            assert client.get('/healthz').status_code == 200
        assert app.state.task_store.get(bad.id).status == 'failed'
        assert not project.exists()
        assert (tmp_path / 'data/deleted_assets.yaml').is_file()
        assert client.post('/api/assets/demo/actions/start').status_code == 404
    assert TestClient(app).get('/healthz').status_code == 503
