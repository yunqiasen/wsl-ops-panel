# WSL Ops Panel openai-cpa Docker Registration and Versioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `openai-cpa` 纳入 Docker 分类，并把 Docker 的版本查询、指定版本部署和页面展示升级为同时支持普通拉镜像型项目和本地构建型项目。

**Architecture:** 继续保留现有 `docker` 分类和 `docker_compose` 对象类型，在对象配置中补 `lifecycle_strategy` / `recipe_id` 等字段，再通过独立 recipe 服务描述项目维护方式。Docker 版本查询统一收敛到一个专用版本服务，页面和 API 统一展示“可部署态”和“运行态”，Docker adapter 按策略分发动作计划，不再临时依赖本机镜像缓存和易碎的字符串脚本。

**Tech Stack:** Python 3.13、FastAPI、Pydantic v2、YAML、httpx、subprocess、pytest、ruff、现有串行 task worker。

---

## File Structure

### Create

- `app/models/recipes.py`
- `app/recipes/loader.py`
- `app/recipes/service.py`
- `app/services/docker_versions.py`
- `config/objects/docker-openai-cpa.yaml`
- `config/recipes/docker/openai-cpa.yaml`
- `config/recipes/docker/overrides/openai-cpa.compose.override.yaml`
- `tests/test_recipe_loader.py`
- `tests/test_docker_versions.py`

### Modify

- `app/models/registry.py`
- `app/models/assets.py`
- `app/main.py`
- `app/api/assets.py`
- `app/adapters/docker_adapter.py`
- `app/services/assets.py`
- `app/templates/category.html`
- `app/templates/asset_detail.html`
- `tests/test_registry_loader.py`
- `tests/test_docker_adapter.py`
- `tests/test_docker_scanner.py`
- `tests/test_package_asset_routes.py`
- `tests/test_overview_routes.py`
- `docs/operations.md`

### Responsibility split

- `app/models/registry.py`：扩展 Docker 对象配置 schema，允许声明 lifecycle / recipe / 服务归属
- `app/models/recipes.py`：定义 Docker recipe 数据模型和 override / healthcheck 结构
- `app/recipes/loader.py`：从 `config/recipes/docker/*.yaml` 读取并校验 recipe
- `app/recipes/service.py`：提供 recipe 查询、快照复制和路径解析
- `app/services/docker_versions.py`：统一处理 Git tags、Docker Hub tags、运行态 OCI 信息
- `app/adapters/docker_adapter.py`：按 `compose_pull` / `compose_local_build_git_tag` 生成动作计划
- `app/services/assets.py`：构建 Docker 资产时补生命周期、版本源状态和运行态版本
- `app/api/assets.py`：返回 richer version payload，并用 recipe + strategy 构建 Docker adapter
- 模板：展示生命周期、可部署版本、运行态版本、openai-cpa 项目信息

## Task 1: 扩展 Docker 对象 schema 并引入 recipe 注册

**Files:**
- Create: `app/models/recipes.py`
- Create: `app/recipes/loader.py`
- Create: `app/recipes/service.py`
- Create: `config/objects/docker-openai-cpa.yaml`
- Create: `config/recipes/docker/openai-cpa.yaml`
- Create: `config/recipes/docker/overrides/openai-cpa.compose.override.yaml`
- Create: `tests/test_recipe_loader.py`
- Modify: `app/models/registry.py`
- Modify: `tests/test_registry_loader.py`

- [ ] **Step 1: 先写 registry / recipe 失败测试，锁定新字段和 openai-cpa 配置入口**

```python
# tests/test_recipe_loader.py
from pathlib import Path

from app.recipes.loader import load_docker_recipes


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_load_docker_recipes_reads_override_and_strategy(tmp_path: Path) -> None:
    _write(
        tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml',
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\n'
        'ignored_services: [watchtower]\n'
        'local_image_repository: local/wenfxl-codex-manager\n'
        'local_image_tag_template: "{version}-overlay"\n'
        'healthcheck:\n'
        '  url: http://127.0.0.1:8128\n'
        '  expect_status: 200\n',
    )

    recipes = load_docker_recipes(tmp_path / 'recipes' / 'docker')

    recipe = recipes['openai-cpa']
    assert recipe.lifecycle_strategy == 'compose_local_build_git_tag'
    assert recipe.override_file.endswith('overrides/openai-cpa.compose.override.yaml')
    assert recipe.managed_services == ['codex-web']
```

```python
# tests/test_registry_loader.py
def test_load_registry_accepts_extended_docker_config(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'openai-cpa.yaml',
        _docker_object_yaml(
            object_id='openai_cpa',
            name='openai-cpa',
            config=
            '  project_dir: /srv/openai-cpa\n'
            '  compose_file: docker-compose.yml\n'
            '  primary_container: wenfxl_codex_manager\n'
            '  compose_service: codex-web\n'
            '  lifecycle_strategy: compose_local_build_git_tag\n'
            '  version_source: git_tags\n'
            '  recipe_id: openai-cpa\n'
            '  managed_services:\n'
            '    - codex-web\n'
            '  ignored_services:\n'
            '    - watchtower\n'
            '  healthcheck_url: http://127.0.0.1:8128\n',
        ),
    )

    registry = load_registry(tmp_path)

    assert registry.objects[0].config['recipe_id'] == 'openai-cpa'
    assert registry.objects[0].config['managed_services'] == ['codex-web']
```

- [ ] **Step 2: 运行 focused tests，确认因为 recipe 模块和 schema 缺失而失败**

Run: `pytest tests/test_recipe_loader.py tests/test_registry_loader.py -v`

Expected: FAIL，至少包含 `ModuleNotFoundError: No module named 'app.recipes.loader'`，以及 `recipe_id` / `lifecycle_strategy` 被 registry schema 拒绝。

- [ ] **Step 3: 加入 Docker 对象扩展字段和 recipe 模型**

```python
# app/models/registry.py
class DockerComposeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    project_dir: StrictStr
    compose_file: StrictStr
    primary_container: StrictStr | None = None
    compose_service: StrictStr | None = None
    lifecycle_strategy: StrictStr = 'compose_pull'
    version_source: StrictStr = 'registry_tags'
    recipe_id: StrictStr | None = None
    managed_services: list[StrictStr] = Field(default_factory=list)
    ignored_services: list[StrictStr] = Field(default_factory=list)
    healthcheck_url: StrictStr | None = None
```

```python
# app/models/recipes.py
from pydantic import BaseModel, ConfigDict, Field


class DockerRecipeHealthcheck(BaseModel):
    model_config = ConfigDict(extra='forbid')

    url: str
    expect_status: int = 200


class DockerRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    lifecycle_strategy: str
    version_source: str
    repo_dir: str
    compose_file: str
    compose_service: str
    primary_container: str
    override_file: str
    git_remote: str = 'origin'
    tag_pattern: str = r'^v\d+\.\d+\.\d+$'
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)
    local_image_repository: str | None = None
    local_image_tag_template: str | None = None
    healthcheck: DockerRecipeHealthcheck | None = None
    full_delete_paths: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: 实现 recipe loader / service，并写入 openai-cpa 配置与两个 override 文件**

```python
# app/recipes/loader.py
from pathlib import Path

import yaml

from app.models.recipes import DockerRecipe


def load_docker_recipes(directory: Path) -> dict[str, DockerRecipe]:
    recipes: dict[str, DockerRecipe] = {}
    for path in sorted(directory.glob('*.yaml')):
        payload = yaml.safe_load(path.read_text(encoding='utf-8'))
        recipe = DockerRecipe.model_validate(payload)
        recipe = recipe.model_copy(
            update={
                'override_file': str((path.parent / recipe.override_file).resolve()),
            }
        )
        recipes[recipe.id] = recipe
    return recipes
```

```python
# app/recipes/service.py
from pathlib import Path

from app.models.recipes import DockerRecipe
from app.recipes.loader import load_docker_recipes


class DockerRecipeService:
    def __init__(self, config_root: Path | str) -> None:
        self._config_root = Path(config_root)
        self._recipes = load_docker_recipes(self._config_root / 'recipes' / 'docker')

    def get(self, recipe_id: str | None) -> DockerRecipe | None:
        if recipe_id is None:
            return None
        recipe = self._recipes.get(recipe_id)
        return recipe.model_copy(deep=True) if recipe is not None else None
```

```yaml
# config/objects/docker-openai-cpa.yaml
id: openai_cpa
category: docker
type: docker_compose
name: openai-cpa
enabled: true
config:
  project_dir: "/home/div/1_Project_dir/regmail-2api/资源/openai-cpa"
  compose_file: "docker-compose.yml"
  primary_container: "wenfxl_codex_manager"
  compose_service: "codex-web"
  lifecycle_strategy: "compose_local_build_git_tag"
  version_source: "git_tags"
  recipe_id: "openai-cpa"
  managed_services:
    - "codex-web"
  ignored_services:
    - "watchtower"
  healthcheck_url: "http://127.0.0.1:8128"
```

```yaml
# config/recipes/docker/openai-cpa.yaml
id: openai-cpa
lifecycle_strategy: compose_local_build_git_tag
version_source: git_tags
repo_dir: /home/div/1_Project_dir/regmail-2api/资源/openai-cpa
compose_file: docker-compose.yml
compose_service: codex-web
primary_container: wenfxl_codex_manager
override_file: overrides/openai-cpa.compose.override.yaml
managed_services: [codex-web]
ignored_services: [watchtower]
local_image_repository: local/wenfxl-codex-manager
local_image_tag_template: "{version}-overlay"
healthcheck:
  url: http://127.0.0.1:8128
  expect_status: 200
full_delete_paths:
  - /home/div/1_Project_dir/regmail-2api/资源/openai-cpa
```

```yaml
# config/recipes/docker/overrides/openai-cpa.compose.override.yaml
services:
  codex-web:
    image: ${WSL_OPS_IMAGE}
    ports:
      - "8128:8000"
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      TZ: Asia/Shanghai
      HOST_PROJECT_PATH: /home/div/1_Project_dir/regmail-2api/资源/openai-cpa
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
```

- [ ] **Step 5: 重跑 schema / recipe tests，确认通过后提交**

Run: `pytest tests/test_recipe_loader.py tests/test_registry_loader.py -v`

Expected: PASS，新增 recipe loader 断言和扩展 Docker config 断言都通过。

```bash
git add app/models/registry.py app/models/recipes.py app/recipes/loader.py app/recipes/service.py \
  config/objects/docker-openai-cpa.yaml config/recipes/docker/openai-cpa.yaml \
  config/recipes/docker/overrides/openai-cpa.compose.override.yaml \
  tests/test_recipe_loader.py tests/test_registry_loader.py
git commit -m "feat: add docker recipe registry for openai-cpa"
```

## Task 2: 建立 Docker 统一版本源和 richer version model

**Files:**
- Create: `app/services/docker_versions.py`
- Create: `tests/test_docker_versions.py`
- Modify: `app/models/assets.py`

- [ ] **Step 1: 先写版本源失败测试，锁定 Git tags、Docker Hub tags 和运行态 OCI 提取**

```python
# tests/test_docker_versions.py
from app.models.assets import DockerContainerSnapshot
from app.services.docker_versions import DockerVersionService


def test_git_tag_version_info_prefers_semver_and_current_tag() -> None:
    service = DockerVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return 'v14.2.6\n'
        if command[-2:] == ['tag', '--list']:
            return 'v14.2.4\nv14.2.6\nv14.2.5\n'
        return ''

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, fetch=False)

    assert info.current_version == 'v14.2.6'
    assert info.latest_version == 'v14.2.6'
    assert info.versions[:3] == ['v14.2.6', 'v14.2.5', 'v14.2.4']
    assert info.source_status == 'ok'


def test_runtime_version_info_reads_oci_labels() -> None:
    service = DockerVersionService()
    container = DockerContainerSnapshot(
        id='1',
        name='wenfxl_codex_manager',
        image='local/wenfxl-codex-manager:v14.2.6-overlay-uifix',
        image_tag='v14.2.6-overlay-uifix',
        status='Up 10 minutes',
        ports='8128/tcp',
        labels={
            'org.opencontainers.image.version': '14.2.4',
            'org.opencontainers.image.revision': 'ece08961',
        },
    )

    runtime = service.build_runtime_version_info(container)

    assert runtime.image_tag == 'v14.2.6-overlay-uifix'
    assert runtime.oci_version == '14.2.4'
    assert runtime.oci_revision == 'ece08961'
```

- [ ] **Step 2: 运行 focused tests，确认因为 DockerVersionService 和 richer model 缺失而失败**

Run: `pytest tests/test_docker_versions.py -v`

Expected: FAIL，报 `ModuleNotFoundError: No module named 'app.services.docker_versions'`，以及 `RuntimeVersionInfo` 不存在。

- [ ] **Step 3: 扩展 assets model，让 Node / Python / Docker 都能复用同一个 version payload**

```python
# app/models/assets.py
class RuntimeVersionInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')

    image: str | None = None
    image_tag: str | None = None
    oci_version: str | None = None
    oci_revision: str | None = None
    ports: str | None = None


class PackageVersionInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')

    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
    lifecycle_strategy: str | None = None
    version_source: str | None = None
    runtime: RuntimeVersionInfo | None = None
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: 实现 DockerVersionService，先支持 Git tags、Docker Hub tags、运行态 OCI 信息**

```python
# app/services/docker_versions.py
import json
import re
import subprocess

import httpx
from packaging.version import Version

from app.models.assets import PackageVersionInfo, RuntimeVersionInfo


class DockerVersionService:
    def get_git_tag_version_info(self, repo_dir: str, *, runner=None, fetch: bool = True) -> PackageVersionInfo:
        run = runner or self._run
        try:
            if fetch:
                run(['git', '-C', repo_dir, 'fetch', '--tags', '--force', 'origin'])
            current = run(['git', '-C', repo_dir, 'tag', '--points-at', 'HEAD']).strip().splitlines()
            tags = [tag for tag in run(['git', '-C', repo_dir, 'tag', '--list']).splitlines() if re.match(r'^v\\d+\\.\\d+\\.\\d+$', tag)]
            ordered = sorted(tags, key=lambda item: Version(item[1:]), reverse=True)
            return PackageVersionInfo(
                current_version=current[0] if current else None,
                latest_version=ordered[0] if ordered else None,
                versions=ordered,
                source_status='ok',
            )
        except Exception as exc:
            return PackageVersionInfo(source_status='error', error=str(exc))

    def get_registry_tag_version_info(self, image_repository: str, current_version: str | None = None, *, fetcher=None) -> PackageVersionInfo:
        get_json = fetcher or self._fetch_docker_hub_tags
        try:
            payload = get_json(image_repository)
            versions = [row['name'] for row in payload.get('results', []) if row.get('name')]
            latest = versions[0] if versions else None
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=latest,
                versions=versions,
                source_status='ok',
            )
        except Exception as exc:
            return PackageVersionInfo(
                current_version=current_version,
                source_status='error',
                error=str(exc),
            )

    @staticmethod
    def build_runtime_version_info(container) -> RuntimeVersionInfo:
        if container is None:
            return RuntimeVersionInfo()
        return RuntimeVersionInfo(
            image=container.image,
            image_tag=container.image_tag,
            oci_version=container.labels.get('org.opencontainers.image.version'),
            oci_revision=container.labels.get('org.opencontainers.image.revision'),
            ports=container.ports,
        )
```

- [ ] **Step 5: 重跑 Docker version tests，确认通过后提交**

Run: `pytest tests/test_docker_versions.py tests/test_package_versions.py -v`

Expected: PASS，Git tags 排序、OCI label 提取和原有 Node / Python 版本测试都通过。

```bash
git add app/models/assets.py app/services/docker_versions.py tests/test_docker_versions.py
git commit -m "feat: add docker version sources"
```

## Task 3: 让 Docker adapter 按 strategy 生成动作计划，并修掉 deploy_version 脆弱链路

**Files:**
- Modify: `app/adapters/docker_adapter.py`
- Modify: `tests/test_docker_adapter.py`

- [ ] **Step 1: 先写 adapter 失败测试，锁定 `compose_pull` 的 env override 和 `openai-cpa` 的本地构建流程**

```python
# tests/test_docker_adapter.py
def test_deploy_version_for_compose_pull_uses_safe_generated_override_file() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/tmp/app',
        compose_file='compose.custom.yml',
        primary_container='demo-container',
        compose_service='demo-service',
        image_repository='example/demo',
        lifecycle_strategy='compose_pull',
    )

    plan = adapter.plan_action('deploy_version', version='v1.2.3')

    assert plan.commands[0][:2] == ['python3', '-c']
    assert plan.commands[1] == [
        'docker',
        'compose',
        '-f',
        'compose.custom.yml',
        '-f',
        '.wsl-ops-panel.override.yml',
        'up',
        '-d',
        '--no-build',
        'demo-service',
    ]
    assert plan.commands[2] == ['rm', '-f', '.wsl-ops-panel.override.yml']


def test_openai_cpa_update_latest_builds_local_image_from_git_tag() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
        override_file='/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
        local_image_repository='local/wenfxl-codex-manager',
        local_image_tag_template='{version}-overlay',
        healthcheck_url='http://127.0.0.1:8128',
    )

    plan = adapter.plan_action('deploy_version', version='v14.2.6')

    assert plan.commands[:4] == [
        ['git', '-C', '/srv/openai-cpa', 'fetch', '--tags', '--force', 'origin'],
        ['git', '-C', '/srv/openai-cpa', 'checkout', 'v14.2.6'],
        ['docker', 'build', '-t', 'local/wenfxl-codex-manager:v14.2.6-overlay', '-f', 'Dockerfile', '.'],
        [
            'env',
            'WSL_OPS_IMAGE=local/wenfxl-codex-manager:v14.2.6-overlay',
            'docker',
            'compose',
            '-f',
            'docker-compose.yml',
            '-f',
            '/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
            'up',
            '-d',
            '--no-build',
            'codex-web',
        ],
    ]
```

- [ ] **Step 2: 运行 focused tests，确认当前 adapter 还不支持 strategy 和安全生成的 override 文件**

Run: `pytest tests/test_docker_adapter.py -v`

Expected: FAIL，`DockerComposeAdapter.__init__()` 不接受 `lifecycle_strategy` / `override_file` / `recipe_repo_dir`，并且 `plan.commands` 仍然包含旧的 `.wsl-ops-panel.override.yml`。

- [ ] **Step 3: 重构 adapter，按 `compose_pull` / `compose_local_build_git_tag` 分支生成动作计划**

```python
# app/adapters/docker_adapter.py
class DockerComposeAdapter:
    def __init__(
        self,
        *,
        project_dir: str,
        compose_file: str,
        primary_container: str,
        compose_service: str,
        image_repository: str | None = None,
        current_version: str | None = None,
        lifecycle_strategy: str = 'compose_pull',
        override_file: str | None = None,
        recipe_repo_dir: str | None = None,
        local_image_repository: str | None = None,
        local_image_tag_template: str | None = None,
        healthcheck_url: str | None = None,
    ) -> None:
        ...

    def plan_action(self, action: DockerAction, version: str | None = None) -> ActionPlan:
        if self.lifecycle_strategy == 'compose_pull':
            return self._plan_compose_pull(action, version)
        if self.lifecycle_strategy == 'compose_local_build_git_tag':
            return self._plan_compose_local_build_git_tag(action, version)
        raise ValueError(f'unsupported lifecycle strategy: {self.lifecycle_strategy}')
```

```python
def _plan_compose_pull(self, action: DockerAction, version: str | None) -> ActionPlan:
    if action == 'deploy_version':
        image_ref = f'{self.image_repository}:{version}'
        return ActionPlan(
            commands=[
                [
                    'python3',
                    '-c',
                    _OVERRIDE_WRITER,
                    _OVERRIDE_FILE,
                    self.compose_service,
                    image_ref,
                ],
                [
                    'docker',
                    'compose',
                    '-f',
                    self.compose_file,
                    '-f',
                    _OVERRIDE_FILE,
                    'up',
                    '-d',
                    '--no-build',
                    self.compose_service,
                ],
                ['rm', '-f', _OVERRIDE_FILE],
            ],
            working_dir=self.project_dir,
            preview_objects=[image_ref],
        )
```

```python
def _plan_compose_local_build_git_tag(self, action: DockerAction, version: str | None) -> ActionPlan:
    target = version or 'latest'
    if version is None:
        raise ValueError('compose_local_build_git_tag requires a resolved git tag')
    image_ref = f'{self.local_image_repository}:{self.local_image_tag_template.format(version=version)}'
    if action in {'update_latest', 'deploy_version'}:
        return ActionPlan(
            commands=[
                ['git', '-C', self.recipe_repo_dir, 'fetch', '--tags', '--force', 'origin'],
                ['git', '-C', self.recipe_repo_dir, 'checkout', version],
                ['docker', 'build', '-t', image_ref, '-f', 'Dockerfile', '.'],
                [
                    'env',
                    f'WSL_OPS_IMAGE={image_ref}',
                    'docker',
                    'compose',
                    '-f',
                    self.compose_file,
                    '-f',
                    self.override_file,
                    'up',
                    '-d',
                    '--no-build',
                    self.compose_service,
                ],
                [
                    'python3',
                    '-c',
                    'import sys,urllib.request; r=urllib.request.urlopen(sys.argv[1], timeout=10); sys.exit(0 if r.status == 200 else 1)',
                    self.healthcheck_url,
                ],
            ],
            working_dir=self.project_dir,
            preview_objects=[image_ref, version],
        )
```

- [ ] **Step 4: 为 `get_version_info()` 接入 DockerVersionService，确保 strategy 和版本源一一对应**

```python
def get_version_info(self) -> PackageVersionInfo:
    if self.lifecycle_strategy == 'compose_local_build_git_tag':
        info = self._version_service.get_git_tag_version_info(self.recipe_repo_dir)
    else:
        info = self._version_service.get_registry_tag_version_info(self.image_repository, current_version=self.current_version)
    return info.model_copy(
        update={
            'lifecycle_strategy': self.lifecycle_strategy,
            'version_source': 'git_tags' if self.lifecycle_strategy == 'compose_local_build_git_tag' else 'registry_tags',
        }
    )
```

- [ ] **Step 5: 重跑 adapter tests，确认新旧两类 Docker 项目都能出稳定 plan**

Run: `pytest tests/test_docker_adapter.py -v`

Expected: PASS，旧 `.wsl-ops-panel.override.yml` 相关断言改为“安全生成 override 文件 + compose up + 清理文件”，`openai-cpa` 的 build / checkout / env compose plan 通过。

```bash
git add app/adapters/docker_adapter.py tests/test_docker_adapter.py
git commit -m "feat: add strategy-aware docker action plans"
```

## Task 4: 把 recipe 和版本源接到资产构建与版本 API

**Files:**
- Modify: `app/main.py`
- Modify: `app/services/assets.py`
- Modify: `app/api/assets.py`
- Modify: `tests/test_docker_scanner.py`
- Modify: `tests/test_package_asset_routes.py`

- [ ] **Step 1: 先写接口失败测试，锁定 openai-cpa 在资产列表和 `/versions` API 中的内容**

```python
# tests/test_package_asset_routes.py
def test_docker_versions_route_returns_strategy_and_runtime_info(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'objects' / 'openai-cpa.yaml').write_text(
        'id: openai_cpa\ncategory: docker\ntype: docker_compose\nname: openai-cpa\nconfig:\n'
        '  project_dir: /srv/openai-cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n  version_source: git_tags\n  recipe_id: openai-cpa\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml').write_text(
        'id: openai-cpa\nlifecycle_strategy: compose_local_build_git_tag\nversion_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\ncompose_file: docker-compose.yml\ncompose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\noverride_file: overrides/openai-cpa.compose.override.yaml\n',
        encoding='utf-8',
    )

    from app.api import assets as assets_api

    monkeypatch.setattr(
        assets_api.DockerComposeAdapter,
        'get_version_info',
        lambda self: PackageVersionInfo(
            current_version='v14.2.6',
            latest_version='v14.2.7',
            versions=['v14.2.7', 'v14.2.6'],
            source_status='ok',
            lifecycle_strategy='compose_local_build_git_tag',
            version_source='git_tags',
            runtime=RuntimeVersionInfo(
                image='local/wenfxl-codex-manager:v14.2.6-overlay',
                image_tag='v14.2.6-overlay',
                oci_version='14.2.4',
                oci_revision='ece08961',
            ),
        ),
    )
```

```python
# tests/test_docker_scanner.py
def test_build_docker_asset_snapshots_attach_strategy_and_recipe_metadata() -> None:
    ...
    openai_object = ObjectDefinition(
        id='openai_cpa',
        category='docker',
        type='docker_compose',
        name='openai-cpa',
        config={
            'project_dir': '/srv/openai-cpa',
            'compose_file': 'docker-compose.yml',
            'primary_container': 'wenfxl_codex_manager',
            'compose_service': 'codex-web',
            'lifecycle_strategy': 'compose_local_build_git_tag',
            'version_source': 'git_tags',
            'recipe_id': 'openai-cpa',
        },
    )
    ...
    assert openai_asset.metadata['recipe_id'] == 'openai-cpa'
    assert openai_asset.metadata['lifecycle_strategy'] == 'compose_local_build_git_tag'
```

- [ ] **Step 2: 运行 focused tests，确认目前 app 还没把 recipe service / richer version model 注入进来**

Run: `pytest tests/test_docker_scanner.py tests/test_package_asset_routes.py -v`

Expected: FAIL，`create_app()` 还没有 recipe service，`AssetVersionsResponse` 也不返回 `runtime` / `lifecycle_strategy`。

- [ ] **Step 3: 在 app startup 中注册 recipe service 和 Docker version service**

```python
# app/main.py
from app.recipes.service import DockerRecipeService
from app.services.docker_versions import DockerVersionService


def create_app(...):
    ...
    docker_recipe_service = DockerRecipeService(Path(config_root))
    docker_version_service = DockerVersionService()
    asset_service = AssetService(
        registry_service,
        docker_scanner=docker_scanner,
        ...
        docker_recipe_service=docker_recipe_service,
        docker_version_service=docker_version_service,
        config_root=Path(config_root),
    )
    ...
    app.state.docker_recipe_service = docker_recipe_service
    app.state.docker_version_service = docker_version_service
```

- [ ] **Step 4: 在资产构建和版本 API 中接入 strategy / runtime / recipe 元数据**

```python
# app/services/assets.py
def build_docker_asset_snapshots(
    registry_snapshot: RegistrySnapshot,
    containers: list[DockerContainerSnapshot],
    *,
    recipe_service: DockerRecipeService,
    version_service: DockerVersionService,
) -> list[AssetSnapshot]:
    ...
    recipe = recipe_service.get(obj.config.get('recipe_id'))
    lifecycle_strategy = obj.config.get('lifecycle_strategy', 'compose_pull')
    runtime = version_service.build_runtime_version_info(primary)
    version_info = (
        version_service.get_git_tag_version_info(recipe.repo_dir, fetch=False)
        if lifecycle_strategy == 'compose_local_build_git_tag' and recipe is not None
        else version_service.get_registry_tag_version_info(image_repository, current_version=primary.image_tag if primary else None)
    )
    assets.append(
        AssetSnapshot(
            ...,
            current_version=version_info.current_version,
            latest_version=version_info.latest_version,
            metadata={
                ...,
                'recipe_id': recipe.id if recipe else obj.config.get('recipe_id'),
                'lifecycle_strategy': lifecycle_strategy,
                'version_source': obj.config.get('version_source', 'registry_tags'),
                'version_source_status': version_info.source_status,
                'runtime_image_tag': runtime.image_tag,
                'runtime_oci_version': runtime.oci_version,
            },
        )
    )
```

```python
# app/api/assets.py
class AssetVersionsResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    object_id: str
    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
    lifecycle_strategy: str | None = None
    version_source: str | None = None
    runtime: RuntimeVersionInfo | None = None
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: 重跑 Docker asset / version route tests，并提交**

Run: `pytest tests/test_docker_scanner.py tests/test_package_asset_routes.py -v`

Expected: PASS，Docker 资产元数据包含 strategy / recipe，`/api/assets/openai_cpa/versions` 能返回 `runtime` 和 `version_source`。

```bash
git add app/main.py app/services/assets.py app/api/assets.py tests/test_docker_scanner.py tests/test_package_asset_routes.py
git commit -m "feat: wire docker recipes into assets and version api"
```

## Task 5: 升级 Docker 分类页和详情页展示，并把 openai-cpa 真正露出到页面

**Files:**
- Modify: `app/templates/category.html`
- Modify: `app/templates/asset_detail.html`
- Modify: `tests/test_overview_routes.py`
- Modify: `tests/test_docker_scanner.py`
- Modify: `docs/operations.md`

- [ ] **Step 1: 先写页面失败测试，锁定分类页和详情页都能看见 lifecycle / deployable / runtime 信息**

```python
# tests/test_overview_routes.py
def test_openai_cpa_detail_shows_strategy_and_runtime_versions(tmp_path: Path, monkeypatch) -> None:
    ...
    response = client.get('/assets/openai_cpa')

    assert response.status_code == 200
    assert 'compose_local_build_git_tag' in response.text
    assert 'v14.2.6' in response.text
    assert 'v14.2.7' in response.text
    assert 'v14.2.6-overlay' in response.text
    assert '14.2.4' in response.text
```

```python
# tests/test_docker_scanner.py
def test_category_route_shows_docker_strategy_and_latest_version(tmp_path: Path, monkeypatch) -> None:
    ...
    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'compose_local_build_git_tag' in response.text
    assert 'Git 当前：v14.2.6' in response.text
    assert 'Git 最新：v14.2.7' in response.text
    assert '运行镜像：v14.2.6-overlay' in response.text
```

- [ ] **Step 2: 运行 focused page tests，确认当前模板还没有这些字段**

Run: `pytest tests/test_overview_routes.py tests/test_docker_scanner.py -v`

Expected: FAIL，现有模板只会输出单一 `当前版本`、项目目录和容器镜像。

- [ ] **Step 3: 修改 Docker 分类页模板，加入 lifecycle / deployable / runtime 三组摘要信息**

```html
{% if asset.category == 'docker' %}
<div class="asset-meta">策略：{{ asset.metadata.lifecycle_strategy or 'compose_pull' }}</div>
<div class="asset-meta">当前版本：{{ asset.current_version or 'unknown' }}</div>
<div class="asset-meta">最新版本：{{ asset.latest_version or 'unknown' }}</div>
<div class="asset-meta">运行镜像：{{ asset.metadata.runtime_image_tag or 'unknown' }}</div>
<div class="asset-meta">版本源：{{ asset.metadata.version_source_status or 'unknown' }}</div>
<div class="asset-meta">主容器：{{ asset.primary_container_name or 'none' }}</div>
{% endif %}
```

- [ ] **Step 4: 修改 Docker 详情页模板，并补运维说明文档**

```html
{% if asset.category == 'docker' %}
<p>生命周期策略：{{ version_info.lifecycle_strategy if version_info else asset.metadata.lifecycle_strategy }}</p>
<p>版本来源：{{ version_info.version_source if version_info else asset.metadata.version_source }}</p>
<p>当前可部署版本：{{ version_info.current_version if version_info else asset.current_version }}</p>
<p>最新可部署版本：{{ version_info.latest_version if version_info else asset.latest_version }}</p>
{% if version_info and version_info.runtime %}
<p>运行镜像：{{ version_info.runtime.image or 'unknown' }}</p>
<p>运行镜像 Tag：{{ version_info.runtime.image_tag or 'unknown' }}</p>
<p>OCI Version：{{ version_info.runtime.oci_version or 'unknown' }}</p>
<p>OCI Revision：{{ version_info.runtime.oci_revision or 'unknown' }}</p>
{% endif %}
{% endif %}
```

```markdown
# docs/operations.md
### 3.6 Docker strategy notes

- `compose_pull`：普通镜像型项目，版本列表来自镜像仓库 tags
- `compose_local_build_git_tag`：本地构建型项目，版本列表来自 Git tags
- `openai-cpa`：使用 panel 托管 override，不直接信任上游 compose 的运行差异
```

- [ ] **Step 5: 重跑页面 tests，并做全量验证后提交**

Run: `pytest tests/test_overview_routes.py tests/test_docker_scanner.py -v`

Expected: PASS，分类页和详情页都能看到 strategy / deployable / runtime 信息。

Run: `pytest -q`

Expected: PASS，完整测试套件保持全绿。

Run: `ruff check app tests`

Expected: PASS，0 errors。

```bash
git add app/templates/category.html app/templates/asset_detail.html tests/test_overview_routes.py \
  tests/test_docker_scanner.py docs/operations.md
git commit -m "feat: surface docker lifecycle and runtime versions"
```

## Task 6: 做一次实施前的完整自检并准备执行分支

**Files:**
- Modify: `docs/superpowers/plans/2026-05-08-openai-cpa-docker-registration-and-versioning.md`

- [ ] **Step 1: 回读 spec，对照 plan 检查覆盖关系**

```text
Spec section 5 → Task 1
Spec section 6 → Task 1 / Task 4
Spec section 7 → Task 3
Spec section 8 → Task 2 / Task 4 / Task 5
Spec section 9 → Task 3
Spec section 10 → Task 4 / Task 5
Spec section 11 / 12 / 13 → Task 3 / Task 4 / Task 5
```

- [ ] **Step 2: 搜索占位词，确认 plan 没有空洞步骤**

Run: `python3 -c "from pathlib import Path; text = Path('docs/superpowers/plans/2026-05-08-openai-cpa-docker-registration-and-versioning.md').read_text(encoding='utf-8'); hits = []; banned = ['T' 'ODO', 'TB' 'D', 'implement' ' later', 'fill in' ' details']; [hits.append(item) for item in banned if item in text]; raise SystemExit(1 if hits else 0)"`

Expected: exit code 0，无输出。

- [ ] **Step 3: 检查关键命名是否前后一致**

```text
Object id: openai_cpa
Recipe id: openai-cpa
Strategy: compose_local_build_git_tag
Version source: git_tags / registry_tags
Primary container: wenfxl_codex_manager
Compose service: codex-web
Override path: config/recipes/docker/overrides/openai-cpa.compose.override.yaml
```

- [ ] **Step 4: 提交计划文档**

```bash
git add docs/superpowers/plans/2026-05-08-openai-cpa-docker-registration-and-versioning.md
git commit -m "docs: add openai-cpa implementation plan"
```
