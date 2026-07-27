# Grok Build 与 Hermes 原生接入设计

**日期**：2026-07-28  
**项目**：WSL Ops Panel  
**参考实现**：CC Switch `878c26f`  
**状态**：已确认，进入实现

## 1. 目标

在本地 WSL Agent 工作台中补齐 Grok Build 与 Hermes 的真实配置管理。客户端入口仍以本机二进制或配置路径为准；测试只使用临时 Home，不向真实 Home 写入演示配置。

## 2. 能力边界

| 客户端 | Provider | Route | MCP | Skill | Prompt |
|---|---|---|---|---|---|
| Grok Build | 原生 TOML | `/grokbuild/v1` 接管 | 原生 TOML | `~/.grok/skills` | `~/.grok/AGENTS.md` |
| Hermes | 原生 YAML | 直连 Provider | 原生 YAML | `~/.hermes/skills` | `~/.hermes/AGENTS.md` |

Hermes Memory、Hermes Web UI、额度与账号页继续留在后续专属模块，不混入本次客户端基础接入。

## 3. Grok Build

### 3.1 检测与能力

检测源为 `~/.grok/config.toml`、`~/.grok`、`grok` 或 `grok-build`。检测成功后开放 Provider、Route、MCP、Skill、Prompt 写入能力。

### 3.2 MCP

配置位于 `~/.grok/config.toml` 顶层 `[mcp_servers]`。统一格式转换规则：

- 有 `url` 时导入为 `type = http`，否则为 `type = stdio`；
- Grok 写回时移除统一格式中的 `type`；
- `http_headers` 与 `headers` 统一为 Grok 的 `headers`；
- 安装、更新、卸载只修改 `[mcp_servers]`，保留模型、注释之外的可解析配置内容；
- 写入前备份，原子替换，重新解析并回读目标 ID。

### 3.3 Provider

Grok 自定义 Provider 使用：

```toml
[models]
default = "PROFILE"

[model.PROFILE]
model = "MODEL"
base_url = "BASE_URL"
name = "NAME"
env_key = "ENV_KEY"
api_backend = "responses"
context_window = 500000
```

官方 OAuth 状态允许配置中仅有 MCP 或空 TOML。导入时保留完整原生快照；能解析出自定义模型时，同时生成统一 `routing` 摘要。直连切换只更新当前 profile 的模型字段并保留其他 TOML。密钥优先使用 `env_key`，显式写入凭证时才使用 `api_key`。

### 3.4 Route

接管只修改当前 `[model.PROFILE]` 的 `base_url` 和接管凭证占位字段，保存原始快照与写后哈希。恢复时优先整文件恢复；检测到外部修改时只恢复接管拥有的字段。

## 4. Hermes

### 4.1 MCP

配置位于 `~/.hermes/config.yaml` 的 `mcp_servers`。导入到统一格式时提取核心字段；写回同名条目时：

- 覆盖 `command`、`args`、`env`、`url`、`headers`；
- 保留 `enabled`、`timeout`、`connect_timeout`、`tools`、`sampling`、`roots`、`auth`；
- stdio 与 HTTP 互换时移除旧传输核心字段；
- 保留其他顶层 YAML 配置；
- 备份、原子写入、YAML 校验和回读验证。

### 4.2 Provider

读取 `custom_providers` 列表和 Hermes v12+ `providers` 字典，生成多条本地 Provider：

- `custom_providers` 条目可写；
- `providers` 字典条目标记为原生覆盖来源，导入和展示保留；
- snake_case 字段为写回格式；
- 切换 Provider 时更新 `model.provider`，存在模型列表时同步 `model.default`；
- 写回 `custom_providers` 时保留未知字段和其他 Provider；
- 旧的整文件本地快照仍可导入和原样恢复。

## 5. UI 与数据流

现有单客户端 UI 复用客户端能力声明，无新增重复选择器。Grok Build 或 Hermes 被真实检测后自动出现在顶部按钮；各页签按 `features` 与 `write_support` 显示。MCP 状态来自对应文件扫描，数据库旧 `apps` 标记不作为安装状态。

## 6. 错误与回滚

- TOML/YAML 解析失败时保留原文件并返回明确错误；
- 每次写入先备份，临时文件 `0600` 后原子替换；
- 回读失败时恢复写前内容；
- Provider/MCP 公共响应继续递归脱敏；
- 日志只记录客户端、配置路径和操作结果。

## 7. 验收

1. 临时 Home 中完成 Grok Provider、Route、MCP、Skill、Prompt 的增删改与恢复。
2. 临时 Home 中完成 Hermes Provider、MCP、Skill、Prompt 的增删改与恢复。
3. Grok TOML 的模型区和 Hermes YAML 的未知字段在往返操作后保留。
4. 真实 Home 缺少 Grok/Hermes 时，线上 UI 仍只显示真实客户端。
5. Python、前端、静态检查、健康检查与浏览器 UI 验收通过。
