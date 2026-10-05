from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.assets import AssetSnapshot
from app.services.source_links import build_source_links


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    install_command: str
    manager: str


NODE_CATALOG = [
    CatalogEntry('@openai/codex', 'Codex CLI，本机和远程设备的 OpenAI 编码助手。', 'npm install -g @openai/codex', 'npm'),
    CatalogEntry('@anthropic-ai/claude-code', 'Claude Code CLI，Agent 工作流常用客户端。', 'npm install -g @anthropic-ai/claude-code', 'npm'),
    CatalogEntry('@google/gemini-cli', 'Gemini CLI，Google Gemini 命令行客户端。', 'npm install -g @google/gemini-cli', 'npm'),
    CatalogEntry('@augmentcode/auggie', 'Augment Code CLI。', 'npm install -g @augmentcode/auggie', 'npm'),
    CatalogEntry('@jackwener/opencli', 'OpenCLI，把网页和本地能力封装成 CLI。', 'npm install -g @jackwener/opencli', 'npm'),
    CatalogEntry('@qingchencloud/openclaw-zh', 'OpenClaw 中文 CLI。', 'npm install -g @qingchencloud/openclaw-zh', 'npm'),
    CatalogEntry('playwright', '浏览器自动化和网页测试工具。', 'npm install -g playwright', 'npm'),
    CatalogEntry('pnpm', 'Node 包管理器。', 'npm install -g pnpm', 'npm'),
    CatalogEntry('yarn', 'Node 包管理器。', 'npm install -g yarn', 'npm'),
    CatalogEntry('typescript', 'TypeScript 编译器。', 'npm install -g typescript', 'npm'),
    CatalogEntry('tsx', '直接运行 TypeScript 的轻量工具。', 'npm install -g tsx', 'npm'),
    CatalogEntry('nodemon', 'Node 开发时自动重启工具。', 'npm install -g nodemon', 'npm'),
    CatalogEntry('pm2', 'Node 进程管理工具。', 'npm install -g pm2', 'npm'),
    CatalogEntry('vite', '前端开发与构建工具。', 'npm install -g vite', 'npm'),
    CatalogEntry('wrangler', 'Cloudflare Worker CLI。', 'npm install -g wrangler', 'npm'),
    CatalogEntry('vercel', 'Vercel CLI。', 'npm install -g vercel', 'npm'),
    CatalogEntry('serve', '静态文件本地服务工具。', 'npm install -g serve', 'npm'),
    CatalogEntry('npm-check-updates', '检查和升级 package.json 依赖版本。', 'npm install -g npm-check-updates', 'npm'),
    CatalogEntry('depcheck', '检查未使用依赖。', 'npm install -g depcheck', 'npm'),
    CatalogEntry('prettier', '代码格式化工具。', 'npm install -g prettier', 'npm'),
]

PYTHON_CATALOG = [
    CatalogEntry('openai', 'OpenAI Python SDK。', 'pip install openai', 'pip'),
    CatalogEntry('fastapi', 'Python API 服务框架。', 'pip install fastapi', 'pip'),
    CatalogEntry('uvicorn', 'FastAPI / ASGI 服务运行器。', 'pip install uvicorn', 'pip'),
    CatalogEntry('playwright', 'Python 浏览器自动化工具。', 'pip install playwright', 'pip'),
    CatalogEntry('httpx', '现代 Python HTTP 客户端。', 'pip install httpx', 'pip'),
    CatalogEntry('requests', '常用 Python HTTP 客户端。', 'pip install requests', 'pip'),
    CatalogEntry('pyyaml', 'YAML 配置解析库。', 'pip install pyyaml', 'pip'),
    CatalogEntry('rich', '终端彩色输出和表格库。', 'pip install rich', 'pip'),
    CatalogEntry('typer', 'Python CLI 应用框架。', 'pip install typer', 'pip'),
    CatalogEntry('ruff', 'Python lint / format 工具。', 'pip install ruff', 'pip'),
    CatalogEntry('black', 'Python formatter。', 'pip install black', 'pip'),
    CatalogEntry('pytest', 'Python 测试框架。', 'pip install pytest', 'pip'),
    CatalogEntry('ipython', '增强型 Python REPL。', 'pip install ipython', 'pip'),
    CatalogEntry('jupyterlab', 'Notebook / 数据分析开发环境。', 'pip install jupyterlab', 'pip'),
    CatalogEntry('pandas', '数据处理库。', 'pip install pandas', 'pip'),
    CatalogEntry('numpy', '科学计算基础库。', 'pip install numpy', 'pip'),
    CatalogEntry('sqlalchemy', 'Python ORM / SQL 工具。', 'pip install sqlalchemy', 'pip'),
    CatalogEntry('alembic', '数据库迁移工具。', 'pip install alembic', 'pip'),
    CatalogEntry('python-dotenv', '读取 .env 环境变量。', 'pip install python-dotenv', 'pip'),
    CatalogEntry('pydantic-settings', 'Pydantic 配置管理。', 'pip install pydantic-settings', 'pip'),
]

_VALUE_OPTIONS = {
    '--registry', '--proxy', '--https-proxy', '--prefix', '--cache',
    '-i', '--index-url', '--extra-index-url', '--trusted-host', '--find-links', '-f',
}
_COMMAND_WORDS = {
    'npm', 'pnpm', 'yarn', 'node', 'npx', 'pip', 'pip3', 'python', 'python3', '-m',
    'install', 'i', 'add', 'global', '-g', '--global', '-u', '--upgrade', 'upgrade', 'uv', 'pipx', 'brew', 'winget',
}


def package_history_path(config_root: Path | str) -> Path:
    root = Path(config_root)
    base = root.parent if root.name == 'config' else root
    return base / 'data' / 'package_catalog_history.json'


def load_package_history(config_root: Path | str) -> dict[str, list[dict[str, str]]]:
    path = package_history_path(config_root)
    if not path.exists():
        return {'node': [], 'python': []}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {'node': [], 'python': []}
    result: dict[str, list[dict[str, str]]] = {'node': [], 'python': []}
    if not isinstance(payload, dict):
        return result
    for key in result:
        items = payload.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get('name'):
                result[key].append({
                    'name': str(item.get('name', '')).strip(),
                    'description': str(item.get('description', '历史安装记录。')).strip() or '历史安装记录。',
                    'install_command': str(item.get('install_command', '')).strip(),
                    'manager': str(item.get('manager', 'npm' if key == 'node' else 'pip')).strip(),
                })
    return result


def record_package_history(config_root: Path | str, category_id: str, package_names: list[str], install_command: str = '') -> None:
    if category_id not in {'node', 'python'}:
        return
    names = [name.strip() for name in package_names if name.strip()]
    if not names:
        return
    path = package_history_path(config_root)
    history = load_package_history(config_root)
    existing = {item['name'].lower(): item for item in history.get(category_id, [])}
    manager = detect_manager(category_id, install_command)
    for name in names:
        key = name.lower()
        existing[key] = {
            'name': name,
            'description': '历史安装记录。',
            'install_command': install_command.strip() or default_install_command(category_id, name),
            'manager': manager,
        }
    history[category_id] = sorted(existing.values(), key=lambda item: item['name'].lower())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build_package_catalog(category_id: str, assets: list[AssetSnapshot], *, config_root: Path | str = Path('config')) -> dict[str, list[dict[str, Any]]]:
    installed = {asset.name.lower(): asset for asset in assets}
    base = NODE_CATALOG if category_id == 'node' else PYTHON_CATALOG if category_id == 'python' else []
    history = load_package_history(config_root).get(category_id, [])

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in base:
        asset = installed.get(entry.name.lower())
        items.append(_catalog_item(entry.name, entry.description, entry.manager, entry.install_command, asset, source='常用'))
        seen.add(entry.name.lower())
    for item in history:
        name = item['name']
        if name.lower() in seen:
            continue
        asset = installed.get(name.lower())
        items.append(_catalog_item(name, item['description'], item['manager'], item['install_command'] or default_install_command(category_id, name), asset, source='历史'))
        seen.add(name.lower())
    for asset in assets:
        if asset.name.lower() not in seen:
            manager = 'npm' if category_id == 'node' else 'pip'
            items.append(_catalog_item(asset.name, '当前 WSL 已安装，自动加入列表。', manager, default_install_command(category_id, asset.name), asset, source='WSL'))
            seen.add(asset.name.lower())

    suggestions = [item for item in items if item['source'] == '常用' and not item['installed_local']][:20]
    return {'items': items, 'suggestions': suggestions}


def parse_install_request(category_id: str, install_command: str, package_names: list[str] | None = None, version: str = '') -> tuple[list[str], dict[str, str], str]:
    names = [name.strip() for name in package_names or [] if name.strip()]
    version_map = {name: version.strip() for name in names if version.strip()}
    command = install_command.strip()
    if command:
        parsed_names, parsed_versions = parse_install_command(category_id, command)
        if parsed_names:
            names = parsed_names
        version_map.update(parsed_versions)
    return list(dict.fromkeys(names)), version_map, command


def parse_install_command(category_id: str, command: str) -> tuple[list[str], dict[str, str]]:
    tokens = _shell_words(command)
    packages: list[str] = []
    version_map: dict[str, str] = {}
    for index, token in enumerate(tokens):
        lower = token.lower()
        if lower in _VALUE_OPTIONS:
            continue
        if index > 0 and tokens[index - 1].lower() in _VALUE_OPTIONS:
            continue
        if lower in _COMMAND_WORDS or token.startswith('-') or token in {'&&', ';', '|'}:
            continue
        if category_id == 'python' and token.startswith(('http://', 'https://', 'git+')) and '#egg=' in token:
            token = token.rsplit('#egg=', 1)[-1]
        elif token.startswith(('http://', 'https://', 'git+', 'file:')):
            if category_id == 'node':
                packages.append(token)
            continue
        name, parsed_version = split_name_version(category_id, token)
        if not name:
            continue
        packages.append(name)
        if parsed_version:
            version_map[name] = parsed_version
    return list(dict.fromkeys(packages)), version_map


def split_name_version(category_id: str, value: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        return '', ''
    if category_id == 'node':
        if value.startswith('@'):
            scope_sep = value.find('/', 1)
            version_sep = value.find('@', scope_sep + 1 if scope_sep >= 0 else 1)
        else:
            version_sep = value.rfind('@')
        if version_sep > 0:
            return value[:version_sep], value[version_sep + 1:]
        return value, ''
    for sep in ('==', '>=', '<=', '~=', '!=', '>', '<'):
        if sep in value:
            name, version = value.split(sep, 1)
            return name.strip(), version.strip()
    return value, ''


def default_install_command(category_id: str, package_name: str) -> str:
    if category_id == 'node':
        return f'npm install -g {package_name}'
    return f'pip install {package_name}'


def detect_manager(category_id: str, install_command: str) -> str:
    command = install_command.strip().lower()
    if command.startswith('pnpm '):
        return 'pnpm'
    if command.startswith('yarn '):
        return 'yarn'
    if 'pipx ' in command:
        return 'pipx'
    if 'uv pip ' in command:
        return 'uv pip'
    return 'npm' if category_id == 'node' else 'pip'


def _catalog_item(name: str, description: str, manager: str, install_command: str, asset: AssetSnapshot | None, *, source: str) -> dict[str, Any]:
    source_links = build_source_links(package_name=name, package_manager='npm' if manager in {'npm', 'pnpm', 'yarn'} else 'pip')
    return {
        'name': name,
        'description': description,
        'manager': manager,
        'install_command': install_command,
        'object_id': asset.object_id if asset is not None else None,
        'installed_local': asset is not None,
        'current_version': asset.current_version if asset is not None else None,
        'actionable': asset.actionable if asset is not None else False,
        'managed_by': asset.managed_by if asset is not None else None,
        'blocked_reason': asset.blocked_reason if asset is not None else None,
        'source_links': source_links,
        'source': source,
    }


def _shell_words(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()
