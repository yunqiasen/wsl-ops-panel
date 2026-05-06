# WSL Ops Panel Phase 1 运维说明

## 1. 服务安装与启动

### 1.1 安装 systemd 服务

```bash
bash scripts/install_systemd_service.sh
```

安装后服务名：

- `wsl-ops-panel.service`

默认监听：

- `127.0.0.1:8328`

### 1.2 检查服务状态

```bash
systemctl status wsl-ops-panel.service --no-pager
curl -I http://127.0.0.1:8328/healthz
```

## 2. sudo 权限要求

Phase 1 需要通过单独的 sudoers include 提供 passwordless sudo。

建议最少白名单覆盖：

- `docker`
- `systemctl`
- `journalctl`

当前仓库自带预检脚本：

```bash
bash scripts/check_sudo_rules.sh
```

该脚本会：

- 从 `config/objects/*.yaml` 读取已注册的 systemd `unit_name`
- 检查 `systemctl disable --now <unit>` 的 sudo 权限
- 检查 `docker ps` 的 sudo 权限

## 3. Phase 1 支持范围

### 3.1 Docker

Phase 1 支持：

- 更新最新版
- 指定版本部署
- 删除
- 完全删除

说明：

- Docker 在 Phase 1 支持 `full_delete`
- full delete 只对已注册对象生效
- 提交后的动作会进入全局串行后台队列自动执行
- stdout / stderr 会同时写入任务日志和系统终端

### 3.2 systemd

Phase 1 支持：

- 删除

说明：

- systemd 删除映射为：`sudo systemctl disable --now <unit>`
- systemd 在 Phase 1 **不支持** `full_delete`
- 提交后的动作会进入全局串行后台队列自动执行
- `update_latest` / `deploy_version` 暂未实现

### 3.3 只读分类

以下分类当前仅展示，不提供执行：

- Node
- Python
- 宿主机进程
- 系统基础设施
- agent cli
- agent

## 4. 常用命令

### 4.1 全量测试

```bash
pytest -q
```

### 4.2 代码检查

```bash
ruff check
```

### 4.3 本地开发启动

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8328 --reload
```

## 5. 重要路径

- 项目目录：`/home/div/1_Project_dir/AI/wsl-ops-panel`
- systemd unit：`/etc/systemd/system/wsl-ops-panel.service`
- SQLite：`data/tasks.sqlite3`
- 操作日志：`data/operations/`
- 终端日志：`data/terminals/`
