const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { nextAgentClient } = require('../../app/static/app.js');

const template = fs.readFileSync(
  path.join(__dirname, '../../app/templates/agent_category.html'),
  'utf8',
);

test('Agent 390px 布局使用紧凑横向主导航', () => {
  const css = fs.readFileSync(path.join(__dirname, '../../app/static/app.css'), 'utf8');
  assert.match(css, /body\[data-page="agent"\] \.nav-stack \{[^}]*overflow-x: auto/s);
  assert.match(css, /body\[data-page="agent"\] \.nav-group \{[^}]*display: flex/s);
});

test('资源中心严格分开数据库资源库与客户端发现项', () => {
  assert.match(template, /data-agent-library/);
  assert.match(template, /data-agent-discovery/);
  assert.match(template, /data-agent-resource-install/);
  assert.match(template, /data-agent-resource-uninstall/);
  assert.match(template, /data-agent-resource-delete/);
  assert.match(template, /data-agent-resource-sync/);
  assert.match(template, /data-agent-discovery-import/);
});

test('Profiles 使用顶部唯一客户端选择器管理组合与应用', () => {
  assert.match(template, /data-agent-tab-control="profiles"/);
  assert.match(template, /data-agent-profile-list/);
  assert.match(template, /data-agent-profile-new/);
  assert.match(template, /data-agent-profile-save/);
  assert.match(template, /data-agent-profile-apply/);
  assert.match(template, /data-agent-profile-delete/);
  assert.doesNotMatch(template, /data-agent-profile-client-selector/);
});

test('资源状态按当前客户端合并 assignment、observation 与 variant', () => {
  const { getAgentResourceClientState } = require('../../app/static/app.js');
  assert.deepEqual(
    getAgentResourceClientState(
      {
        assignments: [
          { node_id: '__local__', client_id: 'codex', desired_enabled: true },
        ],
        observations: [
          { node_id: '__local__', client_id: 'codex', present: true, status: 'drifted' },
        ],
        variants: [
          { client_id: 'codex', platform: 'linux' },
          { client_id: 'claude', platform: 'linux' },
        ],
      },
      'codex',
    ),
    {
      assigned: true,
      present: true,
      status: 'drifted',
      statusLabel: '漂移',
      tone: 'warn',
      variantClientId: 'codex',
      variantPlatform: 'linux',
      variantLabel: 'codex / linux',
    },
  );

  assert.deepEqual(
    getAgentResourceClientState(
      { assignments: [{ node_id: '__local__', client_id: 'codex' }] },
      'codex',
    ),
    {
      assigned: true,
      present: false,
      status: 'missing',
      statusLabel: '缺失',
      tone: 'danger',
      variantClientId: 'base',
      variantPlatform: 'any',
      variantLabel: '基础定义',
    },
  );
});

test('Profile 保存只替换当前客户端资源并保留其他客户端组合', () => {
  const { buildAgentProfilePayload } = require('../../app/static/app.js');
  assert.deepEqual(
    buildAgentProfilePayload({
      name: '工作环境',
      description: '本地开发',
      clientId: 'codex',
      existingItems: [
        { client_id: 'claude', resource_type: 'prompt', resource_id: 'claude-rules', config: {}, sort_index: 0 },
      ],
      selections: {
        provider: ['relay-main'],
        mcp: ['context7', 'memory'],
        skill: ['review'],
        prompt: ['rules'],
        router: ['default'],
      },
      routerConfig: { provider_id: 'relay-main', takeover: true },
    }),
    {
      name: '工作环境',
      description: '本地开发',
      items: [
        { client_id: 'claude', resource_type: 'prompt', resource_id: 'claude-rules', config: {}, sort_index: 0 },
        { client_id: 'codex', resource_type: 'provider', resource_id: 'relay-main', config: {}, sort_index: 1 },
        { client_id: 'codex', resource_type: 'mcp', resource_id: 'context7', config: {}, sort_index: 2 },
        { client_id: 'codex', resource_type: 'mcp', resource_id: 'memory', config: {}, sort_index: 3 },
        { client_id: 'codex', resource_type: 'skill', resource_id: 'review', config: {}, sort_index: 4 },
        { client_id: 'codex', resource_type: 'prompt', resource_id: 'rules', config: {}, sort_index: 5 },
        { client_id: 'codex', resource_type: 'router', resource_id: 'default', config: { provider_id: 'relay-main', takeover: true }, sort_index: 6 },
      ],
    },
  );
});

test('客户端选择始终保持一个激活项', () => {
  assert.equal(nextAgentClient('codex', 'codex'), 'codex');
  assert.equal(nextAgentClient('codex', 'claude'), 'claude');
  assert.equal(nextAgentClient('', ''), null);
});

test('客户端状态卡读取当前客户端的配置路径和计数', () => {
  const { getAgentClientStatus } = require('../../app/static/app.js');
  assert.deepEqual(
    getAgentClientStatus({
      dataset: {
        agentFeatureCount: '5',
        agentProviderCount: '2',
        agentSkillCount: '3',
        agentMcpPath: '~/.codex/config.toml',
      },
    }),
    {
      featureCount: 5,
      providerCount: 2,
      skillCount: 3,
      mcpPath: '~/.codex/config.toml',
    },
  );
});


test('客户端切换同步状态卡图标和配置来源', () => {
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  const syncBlock = script.slice(
    script.indexOf('const syncClientView = () => {'),
    script.indexOf('buttons.forEach((button) => {', script.indexOf('const syncClientView = () => {')),
  );
  assert.match(template, /data-agent-active-glyph/);
  assert.match(template, /data-agent-active-source/);
  assert.match(syncBlock, /agent-client-glyph--\$\{appId\}/);
  assert.match(syncBlock, /dataset\.agentDetectionSource/);
});

test('客户端顶部 Skill 数量跟随资源库回读并去重发现项', () => {
  const { getAgentInstalledSkillCount } = require('../../app/static/app.js');
  assert.equal(getAgentInstalledSkillCount({
    skills: [
      { id: 'review-kit', observations: [{ node_id: '__local__', client_id: 'hermes', present: true, status: 'installed' }] },
      { id: 'missing-kit', assignments: [{ node_id: '__local__', client_id: 'hermes' }], observations: [] },
    ],
    discovery: {
      skills: [
        { id: 'review-kit', client_id: 'hermes' },
        { id: 'native-kit', client_id: 'hermes' },
        { id: 'other-kit', client_id: 'codex' },
      ],
    },
  }, 'hermes'), 2);
});

test('保存 MCP 定义不伪造客户端已安装状态', () => {
  const { buildMcpSavePayload } = require('../../app/static/app.js');
  assert.deepEqual(
    buildMcpSavePayload({
      serverId: 'context7',
      name: 'Context7',
      spec: { command: 'npx' },
    }),
    {
      server_id: 'context7',
      name: 'Context7',
      spec: { command: 'npx' },
      apps: {},
    },
  );
});

test('Provider 路由保留完整端点和出站代理开关', () => {
  const { buildAgentProviderRouting } = require('../../app/static/app.js');
  assert.deepEqual(
    buildAgentProviderRouting({
      baseUrl: 'https://relay.example/custom/responses?tenant=one',
      apiFormat: 'openai_responses',
      authMode: 'bearer',
      model: 'relay-model',
      modelMap: { client: 'relay' },
      headers: { 'X-Tenant': 'team-a' },
      apiKey: 'secret',
      fullUrl: true,
      useOutboundProxy: false,
    }),
    {
      base_url: 'https://relay.example/custom/responses?tenant=one',
      api_format: 'openai_responses',
      auth_mode: 'bearer',
      model: 'relay-model',
      model_map: { client: 'relay' },
      headers: { 'X-Tenant': 'team-a' },
      full_url: true,
      use_outbound_proxy: false,
      api_key: 'secret',
    },
  );
});

test('Router 策略按当前客户端独立保存并保留零重试', () => {
  const { buildAgentRouterPolicyPayload, getAgentRouterPolicy } = require('../../app/static/app.js');
  const clientButton = {
    dataset: {
      agentRouterConfigured: 'true',
      agentRouterAutoFailover: 'true',
      agentRouterMaxRetries: '0',
      agentRouterFailureThreshold: '2',
      agentRouterCooldownSeconds: '45',
    },
  };

  assert.deepEqual(getAgentRouterPolicy(clientButton), {
    configured: true,
    autoFailover: true,
    maxRetries: 0,
    failureThreshold: 2,
    cooldownSeconds: 45,
  });
  assert.deepEqual(
    buildAgentRouterPolicyPayload({
      autoFailover: true,
      maxRetries: '0',
      failureThreshold: '2',
      cooldownSeconds: '45',
    }),
    {
      auto_failover: true,
      max_retries: 0,
      failure_threshold: 2,
      cooldown_seconds: 45,
    },
  );
});

test('设置当前 Provider 后把 Router 返回策略写回客户端状态', () => {
  const { applyAgentRouterProfileDataset } = require('../../app/static/app.js');
  const button = { dataset: {} };

  applyAgentRouterProfileDataset(button, 'relay-main', {
    auto_failover: true,
    max_retries: 1,
    failure_threshold: 2,
    cooldown_seconds: 90,
  });

  assert.deepEqual(button.dataset, {
    agentRouterProvider: 'relay-main',
    agentRouterConfigured: 'true',
    agentRouterAutoFailover: 'true',
    agentRouterMaxRetries: '1',
    agentRouterFailureThreshold: '2',
    agentRouterCooldownSeconds: '90',
  });
});

test('Router 接管态锁定 Provider Live 操作并可即时恢复', () => {
  const { getAgentProviderOwnershipState, applyAgentRouterTakeoverDataset } = require('../../app/static/app.js');
  const clientButton = { dataset: { agentRouterTakeover: 'true' } };

  assert.deepEqual(getAgentProviderOwnershipState(clientButton), {
    takeover: true,
    disabled: true,
    title: 'Router 接管中，先关闭当前客户端接管',
  });

  applyAgentRouterTakeoverDataset(clientButton, false);

  assert.equal(clientButton.dataset.agentRouterTakeover, 'false');
  assert.deepEqual(getAgentProviderOwnershipState(clientButton), {
    takeover: false,
    disabled: false,
    title: '',
  });
});


test('Router 接管切换即时刷新 Provider 主动作文案', () => {
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  const ownershipBlock = script.slice(
    script.indexOf('const providerOwnershipControls'),
    script.indexOf('const routerPolicyControls'),
  );
  assert.match(ownershipBlock, /getAgentProviderPrimaryAction/);
  assert.match(ownershipBlock, /data-agent-provider-activate/);
  assert.match(ownershipBlock, /primary\.label/);
  assert.match(ownershipBlock, /syncProviderPrimaryActions\(\)/);
});

test('资源 reconcile 无差异时只报告数据库一致', () => {
  const { describeAgentReconcileResult } = require('../../app/static/app.js');
  assert.deepEqual(
    describeAgentReconcileResult({ changed: false, operations: [], already_consistent: ['codex:mcp:context7'], verified: true }),
    { message: '已与数据库一致', success: true },
  );
  assert.deepEqual(
    describeAgentReconcileResult({ changed: true, operations: [{ action: 'update' }], updated: ['context7'], verified: true }),
    { message: '已按数据库更新 1 项', success: true },
  );
});

test('Profile 混合安装和卸载时按动作分别汇总', () => {
  const { describeAgentReconcileResult } = require('../../app/static/app.js');
  assert.deepEqual(
    describeAgentReconcileResult({
      changed: true,
      operations: [
        { action: 'uninstall', resource_id: 'native-codex' },
        { action: 'uninstall', resource_id: 'native-prompt' },
        { action: 'install', resource_id: 'context7' },
        { action: 'install', resource_id: 'review-kit' },
        { action: 'install', resource_id: 'work-rules' },
      ],
      verified: true,
    }),
    { message: '已完成 5 项：安装 3，卸载 2', success: true },
  );
});

test('Agent 异步按钮在 await 前缓存目标以正确解除忙碌态', () => {
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  const workbench = script.slice(script.indexOf('function initAgentWorkbench()'), script.indexOf('function initNotificationPanel()'));
  assert.doesNotMatch(workbench, /setPending\(event\.currentTarget/);
});

test('发现区只显示顶部当前客户端的真实现场项', () => {
  const { getAgentDiscoveryForClient } = require('../../app/static/app.js');
  const discovery = {
    mcp: [
      { id: 'codex-only', observations: { codex: { present: true } } },
      { id: 'claude-only', observations_by_target: { __local__: { claude: { present: true } } } },
    ],
    skills: [
      { id: 'review', client_id: 'codex' },
      { id: 'writing', client_id: 'claude' },
    ],
    prompts: [{ id: 'codex-current', client_id: 'codex' }],
  };
  assert.deepEqual(getAgentDiscoveryForClient(discovery, 'mcp', 'codex').map((item) => item.id), ['codex-only']);
  assert.deepEqual(getAgentDiscoveryForClient(discovery, 'skills', 'codex').map((item) => item.id), ['review']);
  assert.deepEqual(getAgentDiscoveryForClient(discovery, 'prompts', 'codex').map((item) => item.id), ['codex-current']);
});

test('五类数据库资源都有明确新增入口和描述字段', () => {
  for (const resourceType of ['provider', 'mcp', 'skill', 'prompt', 'profile']) {
    assert.match(template, new RegExp(`data-agent-resource-new="${resourceType}"`));
  }
  for (const selector of [
    'data-agent-provider-description',
    'data-agent-mcp-description',
    'data-agent-skill-description',
    'data-agent-prompt-description',
    'data-agent-profile-description',
  ]) {
    assert.match(template, new RegExp(selector));
  }
});

test('资源顺序 helper 支持首尾边界和上下移动', () => {
  const { moveAgentResourceId } = require('../../app/static/app.js');
  assert.deepEqual(moveAgentResourceId(['one', 'two', 'three'], 'one', 'up'), ['one', 'two', 'three']);
  assert.deepEqual(moveAgentResourceId(['one', 'two', 'three'], 'three', 'down'), ['one', 'two', 'three']);
  assert.deepEqual(moveAgentResourceId(['one', 'two', 'three'], 'two', 'up'), ['two', 'one', 'three']);
  assert.deepEqual(moveAgentResourceId(['one', 'two', 'three'], 'two', 'down'), ['one', 'three', 'two']);
});

test('Provider 排序请求携带当前客户端，其他分类保持全局作用域', () => {
  const { buildAgentResourceOrderPayload } = require('../../app/static/app.js');
  assert.deepEqual(
    buildAgentResourceOrderPayload('provider', ['second', 'first'], 'codex'),
    { resource_type: 'provider', resource_ids: ['second', 'first'], client_id: 'codex' },
  );
  assert.deepEqual(
    buildAgentResourceOrderPayload('mcp', ['second', 'first'], 'codex'),
    { resource_type: 'mcp', resource_ids: ['second', 'first'] },
  );
});

test('五类资源卡提供拖拽手柄和可触控的上下移动按钮', () => {
  assert.ok((template.match(/data-agent-drag-handle/g) || []).length >= 5);
  assert.ok((template.match(/data-agent-resource-move="up"/g) || []).length >= 5);
  assert.ok((template.match(/data-agent-resource-move="down"/g) || []).length >= 5);
  assert.ok((template.match(/draggable="true"/g) || []).length >= 5);
});

test('MCP 编辑器显式清空描述时仍把空值发送给数据库', () => {
  const { buildMcpSavePayload } = require('../../app/static/app.js');
  assert.deepEqual(
    buildMcpSavePayload({ serverId: 'demo', name: 'Demo', description: '', spec: { command: 'demo' } }),
    { server_id: 'demo', name: 'Demo', description: '', spec: { command: 'demo' }, apps: {} },
  );
});

test('Provider 卡片直接展示端点模型协议并提供完整动作', () => {
  for (const selector of [
    'data-provider-base-url',
    'data-provider-model',
    'data-provider-format',
    'data-provider-auth-state',
    'data-agent-provider-activate',
    'data-agent-provider-edit',
    'data-agent-provider-duplicate',
    'data-agent-provider-test',
    'data-agent-provider-delete',
    'data-agent-provider-remove-live',
  ]) {
    assert.match(template, new RegExp(selector));
  }
  assert.doesNotMatch(template, /data-agent-provider-current-check/);
  assert.doesNotMatch(template, /data-agent-provider-apply(?:\s|>)/);
});

test('Provider 编辑器包含七客户端专属字段区', () => {
  for (const clientId of ['claude', 'codex', 'gemini', 'grokbuild', 'opencode', 'openclaw', 'hermes']) {
    assert.match(template, new RegExp(`data-agent-provider-fields="${clientId}"`));
  }
  assert.match(template, /data-agent-provider-website/);
  assert.match(template, /data-agent-provider-models/);
  assert.match(template, /data-agent-provider-env-key/);
  assert.match(template, /data-agent-provider-npm/);
  assert.match(template, /data-agent-provider-profile/);
});

test('Provider 保存发送客户端 form 和 Router meta，不再组装 routing 空壳', () => {
  const { buildAgentProviderPayload } = require('../../app/static/app.js');
  assert.deepEqual(
    buildAgentProviderPayload({
      appId: 'codex',
      providerId: 'relay',
      name: 'Relay',
      description: '团队线路',
      websiteUrl: 'https://relay.example',
      form: {
        provider_key: 'relay',
        base_url: 'https://relay.example/v1',
        api_key: 'secret',
        model: 'gpt-5.6',
        api_format: 'openai_responses',
        auth_mode: 'bearer',
        models: {},
        headers: {},
      },
      meta: {
        model_map: { 'gpt-5.6': 'upstream' },
        full_url: false,
        use_outbound_proxy: true,
      },
    }),
    {
      app_id: 'codex',
      provider_id: 'relay',
      name: 'Relay',
      notes: '团队线路',
      website_url: 'https://relay.example',
      form: {
        provider_key: 'relay',
        base_url: 'https://relay.example/v1',
        api_key: 'secret',
        model: 'gpt-5.6',
        api_format: 'openai_responses',
        auth_mode: 'bearer',
        models: {},
        headers: {},
      },
      meta: {
        model_map: { 'gpt-5.6': 'upstream' },
        full_url: false,
        use_outbound_proxy: true,
      },
    },
  );
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  const providerBlock = script.slice(script.indexOf('// Provider editor'), script.indexOf('// Router control plane.'));
  assert.doesNotMatch(providerBlock, /settings\.routing/);
  assert.match(providerBlock, /\/activate/);
  assert.match(providerBlock, /\/duplicate/);
  assert.match(providerBlock, /\/remove-live/);
});

test('Provider 主动作按客户端模式和现场状态给出明确文案', () => {
  const { getAgentProviderPrimaryAction } = require('../../app/static/app.js');
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'exclusive', isCurrent: false, liveState: 'saved', takeover: false }),
    { action: 'activate', label: '启用', disabled: false },
  );
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'exclusive', isCurrent: true, liveState: 'current', takeover: false }),
    { action: 'activate', label: '当前', disabled: true },
  );
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'additive', isCurrent: false, liveState: 'saved', takeover: false }),
    { action: 'activate', label: '添加', disabled: false },
  );
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'additive', isCurrent: false, liveState: 'added', takeover: false }),
    { action: 'remove-live', label: '移除', disabled: false },
  );
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'exclusive', isCurrent: false, liveState: 'saved', takeover: true }),
    { action: 'activate', label: '切换路由', disabled: false },
  );
  assert.deepEqual(
    getAgentProviderPrimaryAction({ mode: 'additive', isCurrent: false, liveState: 'unknown', takeover: false }),
    { action: 'activate', label: '状态未知', disabled: true },
  );
});

test('Provider 卡片在桌面紧凑分区并在 390px 使用触控按钮网格', () => {
  const css = fs.readFileSync(path.join(__dirname, '../../app/static/app.css'), 'utf8');
  assert.match(css, /\.agent-provider-facts\s*\{[^}]*grid-template-columns:\s*repeat\(4/s);
  assert.match(css, /@media \(max-width:\s*700px\)[\s\S]*\.agent-provider-actions\s*\{[^}]*grid-template-columns:\s*repeat\(3/s);
  assert.match(css, /@media \(max-width:\s*700px\)[\s\S]*\.agent-provider-actions \.button\s*\{[^}]*min-height:\s*42px/s);
});

test('Provider 空状态跟随当前客户端切换，不使用初始客户端的全局占位', () => {
  assert.match(template, /data-agent-provider-empty[^>]*data-provider-app=/);
  assert.doesNotMatch(template, /\{% if not active_providers %\}/);
});

test('Provider 现场状态未知时禁止直接删除数据库记录', () => {
  assert.match(template, /data-agent-provider-delete[^\n]*live_state == 'unknown'/);
});

test('Provider UI 重构保留 Router 故障转移队列交互', () => {
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  assert.match(script, /failoverQueueControls\.list\?\.addEventListener\('click'/);
  assert.match(script, /failoverQueueControls\.addButton\?\.addEventListener\('click'/);
  assert.match(script, /failoverQueueControls\.save\?\.addEventListener\('click'/);
  assert.match(script, /\/failover-queue/);
});

test('Provider 编辑器初始化客户端归属，重复点击当前客户端不清空草稿', () => {
  const script = fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8');
  assert.match(script, /if \(providerEditor && !providerEditor\.dataset\.agentProviderApp\) providerEditor\.dataset\.agentProviderApp = activeClient\(\);/);
  assert.match(script, /providerEditor\?\.dataset\.agentProviderApp !== button\.value/);
});

test('Provider 卡片按身份、摘要、动作三行排布，避免信息和按钮挤在同一行', () => {
  const css = fs.readFileSync(path.join(__dirname, '../../app/static/app.css'), 'utf8');
  const start = css.indexOf('/* Provider workbench:');
  const end = css.indexOf('.agent-provider-row.is-live', start);
  const baseCardCss = css.slice(start, end);
  assert.match(baseCardCss, /\.agent-provider-row\s*\{[^}]*grid-template-areas:\s*\n\s*"order identity state"\s*\n\s*"order facts facts"\s*\n\s*"order actions actions"/s);
});

test('Provider 身份区左对齐，不受全局按钮居中样式影响', () => {
  const css = fs.readFileSync(path.join(__dirname, '../../app/static/app.css'), 'utf8');
  assert.match(css, /\.agent-provider-identity\s*\{[^}]*justify-content:\s*flex-start/s);
});
