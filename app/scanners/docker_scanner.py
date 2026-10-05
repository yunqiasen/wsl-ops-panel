import json
import re
import subprocess
from collections.abc import Callable

from pydantic import ValidationError

from app.models.assets import DockerContainerSnapshot

DockerCommandRunner = Callable[[], subprocess.CompletedProcess[str]]


def parse_docker_ps_lines(lines: list[str]) -> list[DockerContainerSnapshot]:
    containers: list[DockerContainerSnapshot] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
            labels = _parse_labels(row.get('Labels', ''))
            containers.append(
                DockerContainerSnapshot(
                    id=row.get('ID', ''),
                    name=row['Names'],
                    image=row['Image'],
                    image_tag=_extract_image_tag(row['Image']),
                    status=row.get('Status', row.get('State', 'unknown')),
                    state=row.get('State'),
                    ports=row.get('Ports'),
                    compose_project=labels.get('com.docker.compose.project'),
                    compose_service=labels.get('com.docker.compose.service'),
                    compose_working_dir=labels.get('com.docker.compose.project.working_dir'),
                    labels=labels,
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError):
            continue
    return containers


def scan_docker_containers(*, runner: DockerCommandRunner | None = None) -> list[DockerContainerSnapshot]:
    completed = (runner or _run_docker_ps)()
    stdout = completed.stdout.strip()
    if not stdout:
        return []
    return parse_docker_ps_lines(stdout.splitlines())


def _run_docker_ps() -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ['docker', 'ps', '-a', '--format', '{{json .}}'],
        check=True, capture_output=True, text=True, timeout=30,
    )
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    ids = [row['ID'] for row in rows if row.get('ID')]
    if not ids:
        return completed
    # Only request labels, never the container environment or credentials.
    inspected = subprocess.run(
        ['docker', 'container', 'inspect', '--format', '{{json .Id}} {{json .Config.Labels}}', *ids],
        check=False, capture_output=True, text=True, timeout=30,
    )
    labels_by_id = {}
    for line in inspected.stdout.splitlines():
        try:
            raw_id, raw_labels = line.split(' ', 1)
            labels_by_id[json.loads(raw_id)] = json.loads(raw_labels) or {}
        except (ValueError, TypeError):
            continue
    for row in rows:
        for full_id, labels in labels_by_id.items():
            if full_id.startswith(row['ID']):
                row['Labels'] = labels
                break
    return subprocess.CompletedProcess(completed.args, completed.returncode,
        '\n'.join(json.dumps(row) for row in rows), completed.stderr)


def _parse_labels(raw_labels: str | dict[str, str]) -> dict[str, str]:
    if isinstance(raw_labels, dict):
        return dict(raw_labels)
    labels: dict[str, str] = {}
    if not raw_labels:
        return labels

    for item in re.split(r',(?=[A-Za-z0-9_.-]+=)', raw_labels):
        if '=' not in item:
            continue
        key, value = item.split('=', 1)
        labels[key] = value
    return labels


def _extract_image_tag(image: str) -> str | None:
    if '@' in image:
        image = image.split('@', 1)[0]

    last_slash = image.rfind('/')
    last_colon = image.rfind(':')
    if last_colon <= last_slash:
        return None
    return image[last_colon + 1 :]
