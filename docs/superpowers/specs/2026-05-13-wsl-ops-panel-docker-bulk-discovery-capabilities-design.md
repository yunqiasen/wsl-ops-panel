# WSL Ops Panel Docker 批量控制 / 自动发现 / 项目能力层设计

## 1. 目标

把当前 `wsl-ops-panel` 从“已注册资产列表 + 单资产详情操作”升级成一个更接近真实运维面板的系统，重点补三块能力：

1. **分类页批量控制**
2. **Docker 项目自动发现**
3. **CF / 微信通知 / 开机自启 项目能力层**

本次不是局部 UI 补丁。
本次要把“操作入口位置、资产发现方式、项目附加能力表达方式”一起重构。

---

## 2. 当前现状

### 2.1 资产来源

当前 Docker 分类不是自动发现驱动，而是：

- 先读取 `config/objects/*.yaml`
- 再执行 `docker ps --format '{{json .}}'`
- 用 `compose_working_dir`、容器名、service 名去匹配已注册对象
- 最终只把“注册过的对象”渲染成资产卡片

这意味着：

- 新跑起来的未知 Docker 项目不会自动进入面板
- 自动发现只用于补充运行状态，不用于创建新资产
- 版本回退、更新、删除逻辑都依赖已注册对象的配置完整性

### 2.2 操作入口

当前更新、删除、完全删除、部署版本都在资产详情页右侧。
分类页只有展示，没有多选，也没有批量任务入口。

这会带来两个问题：

- 多项目操作效率低
- 版本切换、删除、自启、通知等动作不能统一排队处理

### 2.3 任务系统

当前任务系统已经具备一个可复用基础：

- 全局串行队列 `GlobalTaskQueue`
- 串行 worker `SerialTaskWorker`
- system terminal 日志桥接
- 每个任务有 stdout/stderr/plan

这意味着：

- 批量操作不需要做并发执行器
- 只需要把多选对象拆成多个排队任务即可
- UI 可以立即返回，不必等前一个任务跑完

### 2.4 CF / 微信通知 / 开机自启

这些能力当前真实存在，但不在面板中抽象成“项目能力”。

当前实现是：

- 各项目 `scripts/cftunnel-start.sh`
- `/etc/systemd/system/*-cftunnel.service`
- `/home/div/1_Project_dir/AI/scripts/startup-notify.sh`
- `startup-notify.service`

问题是：

- `startup-notify.sh` 目前对项目列表和通知内容有明显硬编码
- 每个项目的 CF、自启、通知逻辑是“分散存在”，不是统一能力模型
- 面板无法直接知道“哪个项目支持什么能力、如何执行、如何展示状态” 

---

## 3. 总体方案

采用 **方案 B：统一能力层 + 批量操作 + 自动发现层**。

整体拆成三个子系统：

1. **批量控制界面层**
2. **自动发现与候选资产层**
3. **项目能力层**

这三个子系统共享现有任务队列。
所有外部动作仍然通过队列串行执行。

---

## 4. 批量控制界面层设计

### 4.1 分类页交互模型

分类页中的每张资产卡片右上角增加一个圆形选择控件。

行为规则：

- 点击圆圈：选中/取消选中该资产
- 点击卡片主内容：进入详情页
- 多选后，页面顶部出现批量操作工具栏
- 页面刷新后不保留选择状态
- 分类切换后清空选择状态

### 4.2 批量操作工具栏

工具栏只在“有已选资产”时出现。

工具栏展示：

- 已选择数量
- 更新最新版
- 部署选定版本
- 删除
- 完全删除
- 启动项目
- 停止项目
- 开启自启
- 关闭自启
- 创建 CF 链接
- 刷新 CF 链接
- 关闭 CF
- 发送微信通知
- 编辑通知模板

不是所有分类都显示全部按钮。
按钮根据“当前分类支持动作 + 所选资产共同能力”动态过滤。

### 4.3 版本选择位置

版本选择不再放详情页右侧。

改成：

- 每个资产卡片内部增加一个版本下拉框区域
- 只有该卡片被选中时才展开显示
- 每个卡片可以独立选择一个目标版本
- 顶部工具栏点击“部署选定版本”后，系统对每个已选资产读取自己的目标版本

这样可避免：

- 多个资产共用一个版本字段
- 一次批量操作时版本值冲突

### 4.4 详情页改造

详情页改成纯信息页，不再放更新、删除、完全删除、部署版本按钮。

详情页保留：

- 基本信息
- 运行状态
- 当前版本 / 最新版本 / 可部署版本摘要
- 项目路径 / compose / unit / package 信息
- GitHub 地址或 Git remote
- Docker 镜像仓库地址
- 端口信息
- 当前 CF 链接
- 微信通知状态
- 自启状态
- 已启用能力列表

目标是：

- 详情页负责“看”
- 分类页负责“批量操作”

---

## 5. 自动发现与候选资产层设计

### 5.1 Docker 分类的新结构

Docker 分类中的资产拆成两类：

1. **托管资产**
   - 来自 `config/objects/*.yaml`
   - 有完整对象定义
   - 能力最完整

2. **自动发现资产**
   - 通过运行中的容器推断得到
   - 未必有完整对象定义
   - 可能只支持部分操作

UI 上要明显标识这两类来源。

### 5.2 自动发现来源

继续以运行态为主，不盲扫整个磁盘。

自动发现流程：

1. 执行 `docker ps --format '{{json .}}'`
2. 读取 compose labels
3. 按 `com.docker.compose.project.working_dir` 分组
4. 推断 compose project、service、主容器、镜像仓库、端口
5. 尝试读取工作目录下的 Git 信息
6. 形成“候选 Docker 资产”

### 5.3 自动发现资产字段

自动发现资产最少应包含：

- `object_id`
- `name`
- `category=docker`
- `status`
- `discovery_source=runtime_discovered`
- `project_dir`
- `compose_working_dir`
- `compose_project`
- `compose_services`
- `primary_container`
- `image_repository`
- `current_version`
- `git_remote_url`
- `git_branch`
- `head_sha`
- `ports`
- `supported_actions`
- `capabilities`

### 5.4 支持动作判定

自动发现项目不默认开放所有动作。

判定原则：

- 能确定 `project_dir + compose_file + compose_service` → 允许 update/delete/start/stop
- 能确定版本来源 → 才允许 deploy_version
- 能确定端口 → 才允许 CF / 通知能力候选
- 缺关键字段 → 只读展示或部分可操作

### 5.5 不直接写回注册表

自动发现层不会自动生成 `config/objects/*.yaml`。

原因：

- 有误判风险
- 可能扫到临时容器、辅助容器、测试容器
- 用户不一定希望所有运行项都变成长期托管对象

本次只做：

- 自动展示
- 自动识别
- 部分可操作

后续如果需要，再加“转为托管项目”入口。

---

## 6. Project 分类设计

### 6.1 新分类目的

新增 `Project` 分类，用来展示 `/home/div/1_Project_dir` 下的非 Docker 项目。

这类项目不等于进程，不等于容器。
它表示“本机安装的项目源码或项目目录”。

### 6.2 初始扫描范围

一期只扫：

- `/home/div/1_Project_dir/*`
- 排除隐藏目录与明确的备份目录
- 排除已归属 Docker 托管对象的 project_dir

### 6.3 项目识别规则

根据目录中的标志文件判断项目类型：

- `docker-compose.yml` / `docker-compose.yaml` → Docker 项目，不归入 Project 分类
- `package.json` → Node 项目
- `pyproject.toml` / `requirements.txt` → Python 项目
- `.git` → Git 项目
- `go.mod` → Go 项目
- `Cargo.toml` → Rust 项目

### 6.4 展示字段

Project 分类中的卡片展示：

- 项目名
- 路径
- Git remote
- 默认分支
- 检测到的技术栈
- 是否有运行时进程
- 是否存在版本来源
- 是否属于已托管对象

### 6.5 项目操作边界

一期对 Project 分类先偏只读。
可逐步开放：

- 删除
- 完全删除
- Git pull 更新
- 指定 tag / branch 部署

但只有识别精度足够时才开放。

---

## 7. 项目能力层设计

### 7.1 能力模型

新增统一能力结构 `capabilities`。

每个资产可携带以下能力：

- `runtime_control`
- `autostart`
- `versioning`
- `cf_tunnel`
- `wechat_notify`
- `repo_metadata`

每个能力包含：

- `enabled`
- `supported_actions`
- `status`
- `metadata`

### 7.2 runtime_control

表示该对象是否可启动 / 停止 / 重启。

Docker 对象：

- start
- stop
- restart

systemd 对象：

- enable/disable 之外，还可扩展 start/stop/restart

Node / Python / Project 分类只在明确有启动脚本时开放。

### 7.3 autostart

表示该对象是否支持开机自启。

Linux / WSL / VPS：

- 优先映射到 systemd unit `enabled` 状态
- Docker `restart: unless-stopped` 也要作为容器级自启状态展示

mac：

- 预留 `launchd` 适配器
- 本次只定义接口，不做当前环境真实执行

展示字段：

- 自启策略来源
- 当前是否 enabled
- 关联 unit 或配置来源

### 7.4 cf_tunnel

表示该项目是否支持 Cloudflare Quick Tunnel 或等价反代方式。

一期只支持现有的 Quick Tunnel 风格。

字段：

- `current_url`
- `domain_file`
- `script_path`
- `unit_name`
- `listen_port`
- `provider=cloudflared_quick_tunnel`

动作：

- create
- refresh
- disable
- status

### 7.5 wechat_notify

表示该项目是否接入微信通知。

字段：

- `provider=pushplus`
- `template_id` 或内联模板内容
- `send_on_boot`
- `last_sent_at`
- `linked_cf_url`
- `linked_tailscale_url`

动作：

- send_now
- edit_template
- enable_boot_send
- disable_boot_send

### 7.6 repo_metadata

表示该对象是否能可靠获得仓库信息。

字段：

- `git_remote_url`
- `git_branch`
- `head_sha`
- `repo_host`
- `docker_image_repository`

---

## 8. CF / 微信通知 / 自启的落地方式

### 8.1 不直接复用当前硬编码通知脚本作为唯一逻辑

当前 `startup-notify.sh` 是真实可运行实现，但项目列表和通知内容有硬编码。

本次改造原则：

- 保留现有脚本可继续跑
- 面板侧新增“项目能力配置”与“通知模板配置”
- 后续逐步把 `startup-notify.sh` 改成读取配置生成通知内容

### 8.2 CF 能力落地

面板不直接发明新的 tunnel 体系。
先兼容现有结构：

- 识别 `scripts/cftunnel-start.sh`
- 识别 `/etc/systemd/system/*-cftunnel.service`
- 识别 `logs/cftunnel-domain.txt`

在识别成功时，对该对象展示和开放相关操作。

### 8.3 微信通知落地

通知模板应从“脚本中硬编码 HTML”迁移到面板配置层。

建议新增配置文件或数据库表保存：

- 模板标题
- 模板 HTML
- 是否开机自动发送
- 替换变量

变量至少包括：

- 项目名称
- CF URL
- Tailscale URL
- API Base
- 后台 URL
- 当前时间

### 8.4 自启落地

开机自启状态需要分两层展示：

1. **服务级自启**
   - systemd enabled / disabled
   - launchd loaded / unloaded

2. **容器级自启**
   - Docker restart policy

对于 Docker 项目：

- 先展示 restart policy
- 若存在关联 cftunnel unit，也展示该 unit 的自启状态

---

## 9. API 与任务设计

### 9.1 批量任务 API

新增批量接口，而不是前端循环调单项目接口。

建议新增：

- `POST /api/bulk/actions/update-latest`
- `POST /api/bulk/actions/deploy-version`
- `POST /api/bulk/actions/delete`
- `POST /api/bulk/actions/full-delete`
- `POST /api/bulk/actions/start`
- `POST /api/bulk/actions/stop`
- `POST /api/bulk/actions/autostart-enable`
- `POST /api/bulk/actions/autostart-disable`
- `POST /api/bulk/actions/cf-create`
- `POST /api/bulk/actions/cf-refresh`
- `POST /api/bulk/actions/cf-disable`
- `POST /api/bulk/actions/notify-send`

请求体包含：

- `asset_ids`
- 可选 `version_map`
- 可选 `options`

### 9.2 批量任务执行方式

批量操作不会变成一个“巨任务”。

而是：

- 先生成批次记录
- 再拆成多个普通任务依次入队
- 任务继续由现有串行 worker 按顺序执行

这样可复用现有日志、任务状态、失败隔离。

### 9.3 单项失败策略

批量任务中的单个对象失败时：

- 该对象任务标记 failed
- 后续对象继续排队执行
- 批次状态标记为 partial_failed

不能因为一个项目失败就阻塞后续全部项目。

---

## 10. UI 结构调整

### 10.1 分类页

新增区域：

- `selection-toolbar`
- `asset-selection-toggle`
- `asset-inline-version-selector`
- `asset-capability-row`
- `asset-source-badge`

### 10.2 详情页

删除：

- 右侧操作按钮区
- 版本部署表单

新增：

- 项目能力状态区
- Git / repo 信息区
- CF / 通知 / 自启状态摘要

### 10.3 导航

新增分类：

- `project`

现有分类保留。

---

## 11. 数据模型与文件改动范围

### 11.1 预计新增或修改的核心后端文件

- `app/models/assets.py`
  - 扩展资产能力字段
- `app/services/assets.py`
  - 支持托管资产 + 自动发现资产混合构建
- `app/scanners/docker_scanner.py`
  - 增加候选资产推断能力
- `app/scanners/project_scanner.py`
  - 新增 Project 分类扫描器
- `app/api/assets.py`
  - 拆出只读详情与能力信息接口
- `app/api/bulk_actions.py`
  - 新增批量动作接口
- `app/tasks/*`
  - 增加批次任务编排
- `app/adapters/*`
  - 扩展 start/stop/autostart/cf/notify 计划生成

### 11.2 预计新增或修改的前端文件

- `app/templates/category.html`
- `app/templates/asset_detail.html`
- `app/static/app.js`
- `app/static/app.css`
- 可能新增资产工具栏局部模板

### 11.3 预计新增配置

- `config/categories/project.yaml`
- 可能新增 `config/capabilities/*.yaml`
- 可能新增 `config/project-capabilities/*.yaml`
- 或者改为 SQLite 持久化项目能力配置与通知模板

本次优先建议：

- **静态配置 + 运行态发现混合**
- 通知模板和用户可编辑项进入 SQLite

---

## 12. 测试策略

### 12.1 后端测试

必须覆盖：

- Docker 自动发现项目建模
- 已注册资产与自动发现资产共存
- 多选批量动作请求验证
- 批次拆任务逻辑
- 单任务失败不阻断后续任务
- 项目能力过滤与 UI 可见性

### 12.2 前端测试

至少覆盖：

- 分类页出现多选控件
- 选中后工具栏出现
- 详情页不再渲染更新/删除操作区
- 选中卡片后版本下拉展开
- Project 分类可渲染

### 12.3 手工验证

重点验证：

- Docker 页能显示自动发现新项目
- 批量更新不会卡页面
- 批量删除会串行排队
- 自启状态显示正确
- CF 当前域名能读到
- 微信通知能手动发送

---

## 13. 分阶段实施

### Phase 1

先做批量 UI 和详情页只读化：

- 分类页多选
- 批量工具栏
- 卡片级版本选择
- 详情页移除操作区

### Phase 2

再做 Docker 自动发现和 Project 分类：

- Docker 托管资产 + 自动发现资产合并展示
- 新增 Project 分类
- Git remote / image repository 识别

### Phase 3

最后做项目能力层：

- start/stop
- autostart
- CF tunnel
- wechat notify
- 批量能力任务

---

## 14. 验收标准

这轮完成后，应满足：

1. 分类页能多选并批量操作
2. 详情页只负责查看信息
3. Docker 分类能自动展示未注册运行项目
4. 项目若能识别 Git / image / port，应尽量展示出来
5. 面板能表达项目是否具备 CF / 微信通知 / 自启能力
6. 批量动作全部走串行队列，不阻塞页面
7. 失败任务不会中断整个批次

---

## 15. 结论

这轮改造的核心，不是多加几个按钮。
而是把 `wsl-ops-panel` 从“静态注册资产面板”推进到“可发现、可批量控制、可表达项目能力的运维面板”。

优先顺序应是：

1. 批量控制入口
2. 自动发现层
3. 能力层

不能反过来。
否则只会继续堆单项目详情页按钮，复杂度越来越高。
