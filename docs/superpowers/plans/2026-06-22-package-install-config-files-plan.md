# Package Install Catalog and Config File Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Node/Python 安装区改成“常用未安装下拉框 + 安装命令 + 版本 + 设备安装”的流程，并把系统配置同步中心改成按配置文件展示的模块卡片。

**Architecture:** 包目录服务负责生成常用未安装候选和历史记录；前端负责选择候选、解析安装命令和触发安装/状态刷新；配置同步服务负责扫描本机可编辑配置文件并给远程页面渲染模块。API 继续沿用现有任务队列，不直接同步覆盖远程文件。

**Tech Stack:** FastAPI, Jinja2, Python service layer, vanilla JS, pytest, ruff.

---

### Task 1: 包目录模型与历史记录

**Files:**
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/services/package_catalog.py`
- Test: `/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_remote_nodes.py`

- [ ] 增加 Node/Python 常用候选的 install_command 字段。
- [ ] 增加 `load_package_history()`、`record_package_history()`、`parse_install_request()`。
- [ ] `build_package_catalog()` 返回 `suggestions` 和 `items` 两块数据。
- [ ] 测试：常用下拉只包含未安装项；自定义安装后可记录到历史。

### Task 2: 安装 API 支持命令解析和历史记录

**Files:**
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/api/remote_nodes.py`
- Test: `/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_remote_nodes.py`

- [ ] `PackageInstallRequest` 增加 `install_command` 可选字段。
- [ ] `/api/packages/install` 支持从命令解析包名和版本。
- [ ] 入队成功后记录历史包，避免刷新页面丢失。
- [ ] 测试：`npm install -g pnpm` 能解析为 `pnpm` 并入队。

### Task 3: Node/Python 安装区三输入框 UI

**Files:**
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/templates/category.html`
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/static/app.js`
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/static/app.css`
- Test: `/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_remote_nodes.py`

- [ ] 模板改成：常用未安装下拉框、安装命令输入框、版本输入框。
- [ ] 下拉选中后自动填充命令和版本。
- [ ] 自定义命令安装后自动追加卡片并刷新状态。
- [ ] 测试：页面包含 `data-package-suggestion-select` 和命令输入框。

### Task 4: 配置文件模块扫描服务

**Files:**
- Create: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/services/config_sync.py`
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/api/overview.py`
- Test: `/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_remote_nodes.py`

- [ ] 定义固定配置文件候选：`.zshrc`、`.bashrc`、`.profile`、`.gitconfig`、`.npmrc`、`/etc/wsl.conf`、`/etc/environment`、`/etc/apt/sources.list`、`/etc/apt/sources.list.d`、`/etc/docker/daemon.json`。
- [ ] 扫描存在状态、大小、mtime、是否 sudo、适用系统和摘要。
- [ ] 分类页 context 增加 `config_modules`。
- [ ] 测试：remote 页面渲染配置文件路径。

### Task 5: 配置同步中心改成文件模块卡片

**Files:**
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/templates/remote_category.html`
- Modify: `/home/div/1_Project_dir/AI/wsl-ops-panel/app/static/app.css`
- Test: `/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_remote_nodes.py`

- [ ] 每个配置文件作为一个卡片展示。
- [ ] 卡片显示路径、状态、适用系统、是否 sudo、最近修改。
- [ ] 操作保留 `扫描 / dry-run`，编辑动作先展示入口不直接落盘。
- [ ] 测试：页面不再把 Shell/代理/Git 当作单个 radio 主模块。

### Task 6: 验证和上线

**Files:**
- No code files unless verification exposes failures.

- [ ] Run: `.venv/bin/python -m ruff check app/api/overview.py app/api/remote_nodes.py app/services/package_catalog.py app/services/config_sync.py tests/test_remote_nodes.py`
- [ ] Run: `node --check app/static/app.js`
- [ ] Run: `.venv/bin/python -m pytest tests/test_remote_nodes.py -q`
- [ ] Run: `.venv/bin/python -m pytest -q`
- [ ] Restart: `printf '%s\n' '875133228' | sudo -S systemctl restart wsl-ops-panel.service`
- [ ] Verify: `curl -fsS http://127.0.0.1:8328/healthz`
