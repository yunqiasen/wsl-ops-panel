# WSL Ops Panel Node + Python 可执行管理设计

## 1. 目标

在现有 Phase 1 的基础上，把 `node` 与 `python` 两个分类从只读展示升级为可执行管理，同时保持现有任务队列、系统终端、日志中心和详情页主结构不变。

本次子项目只解决：

- Node 全局包可执行管理
- Python base 环境白名单包可执行管理

本次子项目明确不解决：

- `host` 分类执行能力
- `system` 分类执行能力
- `agent cli` 专项管理
- 多 Python 环境管理

## 2. 范围边界

### 2.1 Node

- 发现来源：`npm list -g --depth=0 --parseable=false --silent`
- 默认策略：扫描到即可展示
- 默认执行范围：普通 npm 全局包
- 保护范围：未来将交给 `agent cli` 分类的 npm 包

首批受保护包：

- `@openai/codex`
- `@anthropic-ai/claude-code`
- `@google/gemini-cli`
- `@jackwener/opencli`
- `@qingchencloud/openclaw-zh`

这些包继续展示在 Node 分类中，但标记为“保留给 agent cli”，当前不允许由 Node 分类执行动作。

### 2.2 Python

- 发现来源：当前扫描器继续使用 `python3 -m pip list --format=json`
- 管理环境：仅 Miniconda base
- 不纳管：
  - `wsl-ops-panel/.venv`
  - `conda AI`
  - 其他未来可能新增的虚拟环境

Python 只对白名单包开放动作。

首批白名单：

- `openai`
- `fastapi`
- `uvicorn`
- `playwright`

### 2.3 host / system

这两个分类保留现状：

- `host`：继续只读
- `system`：继续只读

它们的执行能力放到下一个独立子项目处理。

## 3. 核心模型

### 3.1 保留扫描层

现有扫描器继续只负责发现，不负责决定能否操作：

- `app/scanners/node_scanner.py`
- `app/scanners/python_scanner.py`

扫描结果仍然先生成 `AssetSnapshot`。

### 3.2 新增策略层

增加一个专门的策略解析层，例如：

- `app/services/asset_policies.py`

它基于扫描结果和规则配置，补出以下语义：

- `actionable`
- `supported_actions`
- `blocked_reason`
- `managed_by`
- `policy_source`

策略层的职责是回答“这个对象现在能不能动、能动哪些动作、为什么不能动”，而不是执行动作。

### 3.3 新增适配器层

增加两类适配器：

- `app/adapters/node_adapter.py`
- `app/adapters/python_adapter.py`

它们和现有 Docker / systemd 适配器平级，统一输出 `ActionPlan`。

## 4. 四控语义

### 4.1 Node

支持：

- `update_latest`
- `deploy_version`
- `delete`
- `full_delete`

动作语义：

- 更新最新版：`npm install -g <package>@latest`
- 指定版本部署：`npm install -g <package>@<version>`
- 删除：`npm uninstall -g <package>`
- 完全删除：
  - 卸载全局包
  - 删除规则文件里显式登记的状态目录 / 缓存目录

### 4.2 Python

支持：

- `update_latest`
- `deploy_version`
- `delete`
- `full_delete`

动作语义：

- 更新最新版：`python3 -m pip install -U <package>`
- 指定版本部署：`python3 -m pip install <package>==<version>`
- 删除：`python3 -m pip uninstall -y <package>`
- 完全删除：
  - 卸载包
  - 删除规则文件里显式登记的状态目录 / 缓存目录

说明：

- Python 的 `full_delete` 不允许做模糊路径匹配
- 如果某个白名单包没有定义 `full_delete_paths`，则 `full_delete` 退化为“卸载包 + 可选清 pip cache”，不删除未知目录

## 5. 版本源

### 5.1 Node

- 最新版本：`npm view <package> version`
- 版本列表：`npm view <package> versions --json`

### 5.2 Python

- 最新版本：PyPI JSON API
- 版本列表：PyPI JSON API

不使用 `pip index versions` 作为主要版本源，避免 CLI 输出结构不稳定。

## 6. 规则配置

新增规则目录：

- `config/rules/node-packages.yaml`
- `config/rules/python-packages.yaml`

每个规则条目允许定义：

- `name`
- `managed_by`
- `protected`
- `allowed_actions`
- `full_delete_paths`
- `blocked_reason`

### 6.1 Node 规则

Node 默认可操作，除非规则显式把包标为：

- `protected: true`
- `managed_by: agent_cli`

### 6.2 Python 规则

Python 默认只读。

只有命中白名单规则的包才可操作。

未命中规则的 Python 包仍展示在页面里，但显示“白名单外”。

## 7. API 与页面行为

### 7.1 复用现有动作主链路

继续使用现有动作流程：

1. 页面提交动作
2. `/api/assets/...` 生成 `ActionPlan`
3. 进入全局串行队列
4. 后台 worker 顺序执行
5. stdout / stderr 写入任务日志和系统终端

### 7.2 详情页展示

详情页除当前版本外，新增展示：

- 是否可操作
- `managed_by`
- `blocked_reason`
- 可用动作列表
- 版本源状态

不可操作对象不隐藏，只隐藏动作按钮，并显示原因。

### 7.3 列表页展示

分类页为 Node / Python 对象增加策略徽章：

- 可操作
- 白名单外
- 受保护
- 保留给 agent cli
- 版本源不可用

## 8. 失败模型

### 8.1 策略层失败

例如：

- Python 包不在白名单
- Node 包被标为 `managed_by: agent_cli`
- 某动作不在 `allowed_actions`

处理方式：

- 不入执行器
- 由 API 直接返回 `400` 或 `409`
- 页面显示明确原因

### 8.2 版本源失败

例如：

- npm registry / PyPI 请求失败
- 版本列表解析失败

处理方式：

- 不影响资产展示
- `latest_version` 留空
- 版本下拉留空
- 页面显示“版本源不可用”

### 8.3 执行期失败

例如：

- `npm install -g` 失败
- `pip uninstall` 失败
- `full_delete` 的某个显式路径删除失败

处理方式：

- 任务状态标记为 `failed`
- stderr 写入任务日志和系统终端
- 不做伪回滚

## 9. 组件划分

建议的改动边界：

- 扫描层：保留现有 Node / Python 扫描器
- 策略层：新增 `app/services/asset_policies.py`
- 适配器层：新增 `app/adapters/node_adapter.py` / `app/adapters/python_adapter.py`
- API 层：扩展 `app/api/assets.py` 的适配器选择逻辑
- 服务层：调整 `app/services/assets.py`，让 Node / Python 资产在扫描后进入策略解析
- 模板层：扩展分类页 / 详情页策略徽章与只读原因展示

本次不重写页面主骨架，不改任务系统架构。

## 10. 测试策略

测试拆成四层：

### 10.1 策略层单测

- Node 包受保护时应只读
- Python 包不在白名单时应只读
- `supported_actions` 应按规则收缩
- `blocked_reason` 应可预测

### 10.2 适配器单测

- NodeAdapter 生成的 `ActionPlan` 正确
- PythonAdapter 生成的 `ActionPlan` 正确
- 版本列表解析正确
- `full_delete` 只删除显式路径

### 10.3 路由 / 页面测试

- 可操作对象显示按钮
- 只读对象不显示按钮但显示原因
- 版本源失败时页面仍能打开
- `full_delete` 预览返回正确内容

### 10.4 执行闭环测试

- 入队后自动执行
- stdout / stderr 写入任务日志
- 执行失败时任务状态变 `failed`
- 多任务保持串行顺序

所有测试都使用 fake runner / monkeypatch / 临时规则文件，不执行真实 npm 或 pip 改系统。

## 11. 非目标

本次不做：

- Python 多环境切换
- 宿主机基础设施卸载或高风险删除
- `host` 分类进程 stop / restart
- `agent cli` 专项依赖管理
- 自动推导未知 Python 包的状态目录

## 12. 设计结论

本子项目采用“扫描发现 + 策略判定 + 适配器执行”的混合模型：

- Node：扫描到即展示，普通包默认可控，agent CLI 相关包先保护
- Python：继续扫描 base 环境，但仅白名单包可控
- host / system：延后到下一个子项目

这样可以在不破坏现有任务与页面框架的前提下，把 Node / Python 从只读平滑升级为可执行管理，同时保留后续 `agent cli` 和 `host/system` 专项扩展的清晰边界。
