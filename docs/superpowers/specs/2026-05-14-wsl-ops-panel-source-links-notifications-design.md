# WSL Ops Panel 来源链接、SearXNG、批量选择和项目通知设计

## 目标

本次修四个具体问题：

- `searxng-mcp` 在面板里显示成 MCP 相关项目，实际应显示为 **SearXNG**。
- Docker、Project、Node、Python 资产尽量展示来源链接。
- 分类页增加全选和取消选择。
- 微信通知从“重启 startup-notify.service”改成可编辑、可预览、可手动发送的项目通知。

范围只覆盖面板自身。真实 Docker 项目、凭证、原有通知脚本不改。

## 现状

SearXNG 的目录名是 `/home/div/1_Project_dir/AI/searxng-mcp`，但容器名、镜像、OCI label 和维护手册都指向 SearXNG。面板当前靠 Docker runtime 自动发现，所以沿用了目录名。

Docker 容器里已经能拿到 Labels。SearXNG 镜像有 `org.opencontainers.image.source=https://github.com/searxng/searxng`，也有 `org.opencontainers.image.url=https://searxng.org`。这些信息目前没有被统一整理到元数据里。

通知按钮当前在 Docker adapter 里执行 `sudo systemctl restart startup-notify.service`。`startup-notify.sh` 有同一次启动锁，所以手动点按钮会被跳过。它也不是按项目编辑通知内容的模型。

## 设计

### 1. SearXNG 注册对象

新增 `config/objects/docker-searxng.yaml`。

对象 ID 用 `searxng`，名称用 `SearXNG`，真实 `project_dir` 仍指向 `/home/div/1_Project_dir/AI/searxng-mcp`，compose service 和主容器都用 `searxng`。

这样 Docker 资产构建时会把这个目录视为已注册项目，不再生成 `docker__searxng-mcp` 自动发现项。

### 2. 来源链接统一字段

新增 `app/services/source_links.py`，输出统一结构：

```python
{
  "github": "https://github.com/...",
  "docker": "https://hub.docker.com/r/..." 或 registry 页面,
  "npm": "https://www.npmjs.com/package/...",
  "pypi": "https://pypi.org/project/.../",
  "homepage": "https://...",
}
```

页面统一读取 `asset.metadata.source_links`。旧字段继续保留，避免破坏现有逻辑。

识别规则：

- Docker：优先 OCI label 的 `org.opencontainers.image.source` 和 `org.opencontainers.image.url`；镜像仓库生成 Docker Hub / GHCR / registry 链接。
- Project：读取 `.git remote`，再读取 `package.json.repository` 和 `homepage`。
- Node：根据包名生成 npm 页面；不在分类页联网查 repository，避免卡。
- Python：根据包名生成 PyPI 页面；不在分类页联网查 project_urls，避免卡。

### 3. 批量选择

分类页批量栏加两个轻按钮：

- 全选
- 取消选择

JS 只操作当前页面上的资产卡片，不跨分页、不跨分类。

### 4. 项目微信通知 v1

新增 `app/services/notifications.py` 和 `app/api/notifications.py`。

配置文件为 `config/notifications/projects.yaml`。没有配置时自动生成默认模板，不影响页面渲染。

通知配置按 asset id 保存：

```yaml
assets:
  searxng:
    enabled: true
    title: "SearXNG 访问链接"
    template: |
      🔍 {{ asset.name }}
      状态：{{ asset.status }}
      TS：{{ tailscale_url }}
      CF：{{ cf_url }}
      来源：{{ source_url }}
```

模板变量：

- `asset.name`
- `asset.status`
- `tailscale_url`
- `cf_url`
- `source_url`
- `image_repository`
- `project_dir`
- `ports`
- `updated_at`

发送方式：直接调用 PushPlus API，不再重启 `startup-notify.service`。PushPlus token 优先读取 `config/notifications/projects.yaml` 的 `pushplus.token`，没有就从 `/home/div/1_Project_dir/AI/scripts/startup-notify.sh` 的 `TOKEN='...'` 提取。只读提取，不改原脚本。

为了保持任务中心留痕，点击分类页的“发送微信通知”仍入队任务，但 ActionPlan 不再是 systemd restart，而是执行面板提供的轻量 CLI：

```bash
python3 -m app.services.notifications send --config-root config --asset-id <id> --asset-json <snapshot-json>
```

详情页新增通知配置区：启用开关、标题、模板、预览、保存、发送测试通知。保存只写 `config/notifications/projects.yaml`。

## UI 约束

保持现有浅色 SaaS 风格。分类页只增加小型选择按钮和来源链接摘要，不增加重组件。详情页通知区用普通 panel，避免拖慢首屏。

## 验收标准

- Docker 分类出现 `SearXNG`，不再出现 `searxng-mcp` 作为项目名。
- SearXNG 详情页显示 GitHub `https://github.com/searxng/searxng` 和 Docker 仓库链接。
- Node 详情页有 npm 链接，Python 详情页有 PyPI 链接。
- 分类页有全选、取消选择按钮。
- 通知详情页可以保存标题和模板。
- 通知发送不再重启 `startup-notify.service`。
- `ruff` 和 `pytest` 通过。
