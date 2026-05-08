# WSL Ops Panel openai-cpa 纳管与 Docker 版本链路修正设计

## 1. 目标

在现有 WSL Ops Panel 的 Docker 分类中纳入 `openai-cpa`，同时修正 Docker 分类当前不完整的版本模型，使“更新最新版 / 指定版本部署”对普通拉镜像型项目和本地构建型项目都能成立。

本次子项目只解决：

- `openai-cpa` 注册为 Docker 资产
- Docker 分类版本源模型升级
- Docker `deploy_version` 当前执行链路修复
- Docker 分类页与详情页展示升级

本次子项目明确不解决：

- `freemail` 纳管
- `freemail-proxy` 调整
- Docker 项目 Cloudflare 隧道部署 / 取消 / 刷新
- 微信通知模板与发送
- 其他新分类扩展

## 2. 现状与问题

### 2.1 Docker 版本模型过弱

当前 Docker 版本列表来源仅包含：

- 当前运行容器 tag
- 固定补一个 `latest`
- 本机 `docker image ls` 中恰好已有的 tag

这个模型对以下场景都不成立：

- 普通 `docker compose pull` 型项目，页面看不到完整上游版本列表
- 本地构建型项目，页面版本列表和真实部署语义完全错位

### 2.2 Docker `deploy_version` 当前实现不可靠

当前 Docker adapter 用临时 override 文件切换镜像 tag，但 override 文件生成逻辑存在 bug，`deploy_version` 可能在执行阶段直接失败。

因此现在的 Docker “指定版本”同时存在两个独立问题：

- 版本下拉来源不对或为空
- 选中版本后执行链路仍可能失败

### 2.3 openai-cpa 不是普通拉镜像型项目

`openai-cpa` 当前现场事实与仓库默认 compose 不一致：

- 仓库 `docker-compose.yml` 指向远程镜像 `wenfxl/wenfxl-codex-manager:latest`
- 当前运行容器实际使用本地镜像 `local/wenfxl-codex-manager:<tag>-overlay...`
- 仓库 compose 暴露 `8000:8000`
- 当前运行端口实际是 `8128:8000`
- 仓库 compose 包含 `watchtower`
- 当前面板纳管范围只应覆盖主服务 `codex-web`

因此 `openai-cpa` 不能复用当前“拉镜像 + up -d”的简单动作模型。

## 3. 设计选择

本次采用：

- Docker 仍保持一个分类
- Docker 对象增加生命周期策略字段
- 项目差异通过外置 recipe 表达
- 版本信息拆分为“可部署态”和“运行态”

不采用：

- 为每种 Docker 项目创建全新分类
- 把 `openai-cpa` 的运行差异直接写回上游仓库
- 继续让 Docker adapter 用本地镜像缓存临时拼版本列表

## 4. 范围边界

### 4.1 本次必须完成

- 新增 `openai-cpa` Docker 对象
- 引入 Docker `lifecycle_strategy`
- 引入 Docker recipe 机制
- 引入 Docker 版本提供器
- 修复 Docker `deploy_version` 执行链路
- 升级 Docker 分类页和详情页的版本展示

### 4.2 本次明确不做

- Cloudflare 隧道控制按钮
- 微信通知按钮
- Docker 资产的消息模板编辑
- `freemail` 的 Cloudflare Workers 纳管

## 5. Docker 对象模型升级

### 5.1 继续保留 Docker 分类

`openai-cpa` 继续归属 `docker` 分类，不新增新分类。

### 5.2 对象新增语义字段

Docker 对象在现有字段之外新增：

- `lifecycle_strategy`
- `version_source`
- `recipe_id`
- `managed_services`
- `ignored_services`
- `healthcheck_url`

### 5.3 openai-cpa 的对象口径

`openai-cpa` 的对象语义固定为：

- `id: openai_cpa`
- `category: docker`
- `type: docker_compose`
- `name: openai-cpa`
- `project_dir: /home/div/1_Project_dir/regmail-2api/资源/openai-cpa`
- `compose_file: docker-compose.yml`
- `compose_service: codex-web`
- `primary_container: wenfxl_codex_manager`
- `lifecycle_strategy: compose_local_build_git_tag`
- `version_source: git_tags`
- `recipe_id: openai-cpa`
- `managed_services: [codex-web]`
- `ignored_services: [watchtower]`
- `healthcheck_url: http://127.0.0.1:8128`

说明：

- 分类仍按 Docker 扫描和展示
- 动作执行与版本来源由 strategy + recipe 决定
- `watchtower` 不属于本次纳管范围

## 6. Recipe 机制

### 6.1 目标

对象注册只回答“这是什么项目”，recipe 只回答“这个项目如何维护和部署”。

### 6.2 存放位置

新增 recipe 目录：

- `config/recipes/docker/`

`openai-cpa` 使用：

- `config/recipes/docker/openai-cpa.yaml`

### 6.3 Recipe 内容

`openai-cpa` recipe 至少描述：

- `repo_dir`
- `git_remote`
- `tag_pattern`
- `compose_file`
- `compose_project_name`
- `managed_services`
- `ignored_services`
- `build_context`
- `dockerfile`
- `local_image_repository`
- `local_image_tag_template`
- `deploy_override`
- `healthcheck`
- `delete_image_selector`
- `full_delete_paths`
- `runtime_version_extractors`

### 6.4 deploy_override 的归属

`deploy_override` 由面板维护，属于 panel 自己的部署配方，不写回上游项目仓库。

面板在执行任务时基于 recipe 生成临时 override 文件，并在该次任务完成后清理。

这样可保证：

- 上游仓库更新不覆盖本机运行差异
- 面板始终按固定运行配方重建服务
- 本地运行端口、镜像命名、环境变量等差异可稳定复现

## 7. 生命周期策略

### 7.1 首批支持的策略

本次先支持两种 Docker 生命周期策略：

- `compose_pull`
- `compose_local_build_git_tag`

### 7.2 compose_pull

适用于普通拉镜像型项目。

动作语义：

- 更新最新版：拉最新镜像并重建目标服务
- 指定版本部署：切换到指定镜像 tag 并重建目标服务

现有 `CPA / New API / sub2api / AIClient-2-API` 在本次迁移后默认继续走这条策略或兼容语义。

### 7.3 compose_local_build_git_tag

适用于源码以 Git tag 作为部署版本，本地构建镜像后再部署的项目。

`openai-cpa` 使用这条策略。

## 8. 统一版本模型

### 8.1 拆分两类版本信息

所有支持版本管理的对象统一拆成两组信息：

#### 可部署态

- 当前可部署版本
- 最新可部署版本
- 可选版本列表
- 版本源状态
- 版本源错误

#### 运行态

- 当前运行镜像
- 当前运行镜像 tag
- OCI image version
- OCI image revision

这样可以明确区分：

- “面板现在能部署什么版本”
- “容器当前实际跑的是什么”

### 8.2 统一版本提供器

引入统一版本提供器接口，不再由各 adapter 临时拼版本。

各类对象对应的版本源如下：

- Node：npm registry
- Python：PyPI JSON API
- Docker `compose_pull`：镜像仓库 tags
- Docker `compose_local_build_git_tag`：Git tags

### 8.3 openai-cpa 的版本口径

`openai-cpa` 的版本语义固定为：

- 页面同时显示 Git 版本与运行镜像版本
- 更新最新版默认追 Git 最新 tag
- 指定版本部署按 Git tag 走
- Docker 只作为承载层，不作为主版本源

## 9. openai-cpa 四控语义

### 9.1 更新最新版

执行链路：

1. `git fetch --tags origin`
2. 解析最新 semver tag
3. 切换到该 tag
4. 基于 recipe 生成面板托管的部署 override
5. 构建本地镜像 `local/wenfxl-codex-manager:<tag>-overlay`
6. 只重建 `codex-web`
7. 进行本地健康检查 `http://127.0.0.1:8128`

### 9.2 指定版本部署

与“更新最新版”相同，只是目标 tag 由用户选择。

页面选择来源固定为 Git tags，而不是本机镜像缓存。

### 9.3 删除

删除只针对运行实体，不删除项目家底。

保留：

- Git 仓库
- `data/`
- 对象注册
- recipe 配置

动作仅删除主服务运行实体，不处理 `watchtower`。

### 9.4 完全删除

完全删除会清除：

- 主服务容器
- recipe 匹配到的本地构建镜像
- 项目目录
- `data/`

必须提供：

- 强确认
- 删除预览
- 明确列出将删除的路径与镜像对象

## 10. 页面与 API 行为

### 10.1 Docker 分类页升级

Docker 分类页增加以下展示：

- 生命周期策略
- 当前可部署版本
- 最新可部署版本
- 运行镜像 tag
- 版本源状态
- 管理服务名

### 10.2 Docker 详情页升级

Docker 详情页拆为四块：

#### 对象信息

- 名称
- 分类
- 生命周期策略
- recipe id
- 项目目录
- compose 服务
- 主容器
- 管理服务
- 忽略服务

#### 可部署版本

- 当前可部署版本
- 最新可部署版本
- 版本列表
- 版本源状态
- 版本源错误

#### 运行态版本

- 当前运行镜像
- 运行镜像 tag
- OCI version
- OCI revision
- 容器状态
- 端口映射

#### 操作与健康检查

- 更新最新版
- 指定版本部署
- 删除
- 完全删除
- 健康检查 URL
- 最近一次健康检查结果

### 10.3 版本接口统一口径

版本接口继续服务现有详情页，但返回结构需要扩展到能够表达：

- 可部署版本信息
- 运行态版本信息
- lifecycle strategy
- source status

前端按统一数据结构渲染，不再假设 Docker 只有单一 `current_version`。

## 11. 失败模型

### 11.1 版本源失败

例如：

- Git tag 拉取失败
- registry tags 查询失败
- 版本解析失败

处理方式：

- 资产仍可展示
- 版本列表为空
- 页面明确展示 `source_status` 与错误

### 11.2 执行期失败

例如：

- checkout tag 失败
- 本地 build 失败
- compose up 失败
- 健康检查失败

处理方式：

- 任务标记失败
- stdout / stderr 全量进入任务日志与系统终端
- 不做静默回滚

### 11.3 运行态漂移

例如：

- Git 当前 tag 与运行镜像 tag 不一致
- 运行镜像 tag 与 OCI version 不一致

处理方式：

- 页面明着展示两套信息
- 不强制自动修正
- 由用户显式触发更新或指定版本部署

## 12. 兼容与迁移

### 12.1 现有 Docker 对象

现有 Docker 对象在未显式声明新字段时，默认走兼容口径：

- `lifecycle_strategy = compose_pull`
- `version_source = registry_tags`

这样可避免本次改动要求一次性重写全部对象。

### 12.2 openai-cpa 的引入方式

本次仅新增 `openai-cpa` 对象与对应 recipe，不调整 `freemail`、`freemail-proxy`。

### 12.3 未来扩展方向

后续如果新增类似项目，可继续复用：

- Docker 分类
- lifecycle strategy
- recipe 配方
- 统一版本提供器

而不需要再新增专用分类或继续堆硬编码分支。

## 13. 验收标准

完成本次子项目后，应满足：

- Docker 分类中出现 `openai-cpa`
- `openai-cpa` 详情页能同时看到 Git 版本与运行镜像版本
- `openai-cpa` 的指定版本下拉来自 Git tags
- Docker 分类现有“指定版本”链路不再因为旧 override bug 直接失败
- Docker 分类页与详情页能区分“可部署态”和“运行态”
- `freemail`、CF、微信通知功能未被误纳入本次实现范围
