import json
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
    return subprocess.run(
        ['docker', 'ps', '--format', '{{json .}}'],
        check=True,
        capture_output=True,
        text=True,
    )


def _parse_labels(raw_labels: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not raw_labels:
        return labels

    for item in raw_labels.split(','):
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
