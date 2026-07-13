# 分类运行状态筛选设计

**日期：** 2026-07-13
**项目：** WSL Ops Panel

## 目标

在通用资产分类页的搜索框右侧增加“运行中”和“已关闭”两个筛选按钮，帮助用户快速区分正在提供服务的资产与已经停止的资产。

## 范围

- 覆盖通用卡片页：Docker、Project、systemd、宿主机进程。
- 系统基础设施不展示运行状态按钮，因为 `available/missing/error` 表示安装或检测状态，不表示进程是否运行。
- Node、Python、Agent、SSH 与配置同步使用专用页面或专用目录，本次不改。
- 宿主机进程扫描器只返回当前监听项；关闭后的进程不会保留历史卡片，因此“已关闭”结果通常为空。本次不增加历史数据库。

## 状态模型

模板为每张通用资产卡输出 `data-runtime-state`：

- `running`：Docker 状态以 `Up` 开头，或状态为 `running`、`active`、`listening`。
- `stopped`：状态为或以 `Exited`、`not running`、`inactive`、`failed`、`dead`、`stopped` 开头。
- `unknown`：`present`、`available`、`missing`、`error`、`scan_failed` 以及无法确认运行态的其他值。

`unknown` 资产在默认视图中显示；启用“运行中”或“已关闭”筛选后排除，避免把“只发现目录”误报为已经关闭。

## 交互

- 两个按钮位于搜索输入框右侧、匹配计数左侧。
- 按钮文案为“运行中”和“已关闭”。
- 两按钮互斥；点击未激活按钮切换筛选，点击已激活按钮恢复“全部”。
- 使用 `aria-pressed` 暴露激活状态，支持键盘操作。
- 搜索词与运行状态使用 AND 组合：卡片必须同时满足文字搜索和状态筛选。
- 匹配计数始终显示最终可见卡片数。
- 移动端将搜索框、按钮组和匹配计数分行，按钮保持等宽并避免挤压输入框。

## 视觉

- 未激活按钮沿用浅色描边按钮。
- “运行中”激活时使用绿色状态色。
- “已关闭”激活时使用中性灰蓝状态色。
- 不新增图标库，不改变卡片布局。

## 最小实现边界

- `app/templates/category.html`：增加按钮组、分类显示条件和卡片标准状态属性。
- `app/static/app.js`：扩展 `initAssetSearch()`，统一处理搜索词与运行状态。
- `app/static/app.css`：增加按钮组、激活态和响应式样式。
- `tests/test_overview_routes.py`：验证模板状态属性、按钮可访问属性和分类显示边界。
- `tests/frontend/category-runtime-filter.test.js`：使用 Node 内置测试验证组合筛选状态机，不引入新依赖。
- `docs/operations.md`：记录分类页筛选规则和状态语义。

## 验收标准

1. Docker 页面可分别筛出 `Up` 与 `Exited/not running` 卡片。
2. Project/systemd 使用相同按钮和统一语义。
3. 系统基础设施不显示两个运行状态按钮。
4. 搜索词与状态筛选可组合，计数准确。
5. 再次点击激活按钮恢复全部。
6. 键盘可操作，`aria-pressed` 与视觉状态一致。
7. 桌面和窄屏下不遮挡搜索输入框或匹配计数。
8. 现有批量选择、卡片点击和搜索行为不受影响。
