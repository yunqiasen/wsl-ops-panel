# WSL Ops Panel

WSL 维护更新管理面板。

一个面向 WSL 环境的运维控制面板，当前 Phase 1 已完成：

- Docker 资产扫描、详情、动作与后台串行执行
- systemd 资产扫描与 delete 动作执行
- Node / Python / 宿主机进程 / 系统基础设施只读扫描
- 任务中心、日志中心、终端中心、设置页
- 默认系统终端 + 可交互调试终端
- registry 重载
- 入队任务由后台 worker 自动按顺序执行

## 基本信息

- 项目目录：`/home/div/1_Project_dir/AI/wsl-ops-panel`
- 监听端口：`8328`
- 应用入口：`app.main:app`
- Python 要求：`>=3.13`
- 默认服务地址：`http://127.0.0.1:8328`

## 本地开发

```bash
uv venv
uv sync --extra dev
. .venv/bin/activate
uv run uvicorn app.main:app --host 127.0.0.1 --port 8328 --reload
```

## 测试

```bash
pytest -q
ruff check
```

## 安装为 systemd 服务

```bash
bash scripts/check_sudo_rules.sh
bash scripts/install_systemd_service.sh
curl -I http://127.0.0.1:8328/healthz
```

systemd 服务名：

- `wsl-ops-panel.service`

## 运维文档

详见：

- `docs/operations.md`
