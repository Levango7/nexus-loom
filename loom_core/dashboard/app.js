/* ============================================================
   Doc-Pipeline v3 Dashboard — Vanilla JS
   ============================================================ */

(function () {
  'use strict';

  // ----------------------------------------------------------
  // Configuration
  // ----------------------------------------------------------
  const API_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : 'http://127.0.0.1:8910';
  const REFRESH_MS = 5000;

  /* 交付账本一次拉几条：面板要能一眼看完，明细是点开才取的。 */
  const DELIVERIES_LIMIT = 8;

  /* Runs 面深链（评审 §4.2）：#/runs/<task_id> 直达运行详情，浏览器后退回列表。 */
  const RUN_HASH_PREFIX = '#/runs/';

  // ----------------------------------------------------------
  // State
  // ----------------------------------------------------------
  let state = {
    health: null,
    tasks: null,
    agents: null,
    deliveries: null,
    error: null,
    lastUpdated: null,
    loaded: false,
    taskFilter: 'all',
    settings: null,
    workflows: null,
  };

  let refreshTimer = null;

  /* 账本明细开着时，5 秒轮询不得回画列表——否则点开即被覆盖（实测缺陷：
     明细在下一轮轮询后消失）。返回列表时显式清旗。 */
  let deliveriesDetailOpen = false;

  // ----------------------------------------------------------
  // DOM cache
  // ----------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const els = {};
  function cacheDom () {
    els.errorOverlay   = $('#error-overlay');
    els.errorMsg       = $('#error-msg');
    els.retryBtn       = $('#retry-btn');
    els.dashboard      = $('#dashboard');
    els.lastUpdated    = $('#last-updated');
    els.tokenPrompt    = $('#token-prompt');
    els.tokenInput     = $('#token-input');
    els.tokenSave      = $('#token-save');
    els.themeToggle    = $('#theme-toggle');

    // Panel containers
    els.statusPanel    = $('#panel-status .card-body');
    els.tasksPanel     = $('#panel-tasks .card-body');
    els.agentsPanel    = $('#panel-agents .card-body');
    els.metricsPanel   = $('#panel-metrics .card-body');
    els.qualityPanel   = $('#panel-quality .card-body');
    els.costPanel      = $('#panel-cost .card-body');
    els.logsPanel      = $('#panel-logs .card-body');
    els.taskFilter     = $('#task-filter');

    // New task form
    els.newTaskForm    = $('#newtask-form');
    els.ntQuery        = $('#nt-query');
    els.ntPipeline     = $('#nt-pipeline');
    els.ntOutput       = $('#nt-output');
    els.ntSubmit       = $('#nt-submit');
    els.ntMsg          = $('#nt-msg');

    // Badge counters
    els.badgeTasks     = $('#badge-tasks');
    els.badgeAgents    = $('#badge-agents');
    els.badgeStatus    = $('#badge-status');
    els.badgeMetrics   = $('#badge-metrics');
    els.approvalsPanel = $('#panel-approvals .card-body');
    els.badgeApprovals = $('#badge-approvals');
    els.deliveriesPanel = $('#panel-deliveries .card-body');
    els.badgeDeliveries = $('#badge-deliveries');
    els.badgeQuality   = $('#badge-quality');
    els.badgeCost      = $('#badge-cost');
    els.badgeLogs      = $('#badge-logs');
    els.settingsPanel  = $('#panel-settings .card-body');
    els.badgeSettings  = $('#badge-settings');
    els.workflowsPanel = $('#panel-workflows .card-body');
    els.badgeWorkflows = $('#badge-workflows');
  }

  // ----------------------------------------------------------
  // Utility
  // ----------------------------------------------------------
  function pluralize (n, s) {
    return n + ' ' + (n === 1 ? s : s + 's');
  }

  /* DOM 构建辅助：所有动态数据一律走 textContent，禁止字符串拼 HTML */
  function elt (tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function iconEl (id, cls) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'ico' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function emptyState (iconId, msg) {
    const box = elt('div', 'empty-state');
    const wrap = elt('span', 'icon');
    wrap.appendChild(iconEl(iconId, 'ico-lg'));
    box.appendChild(wrap);
    box.appendChild(document.createTextNode(msg));
    return box;
  }

  /* 键盘可达（评审 10-20 要求）：行/节点等"点击即操作"的元素要能 Tab 到、Enter/Space 触发。 */
  function makeKeyboardActivatable (el, activate) {
    el.tabIndex = 0;
    el.setAttribute('role', 'button');
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate();
      }
    });
    return el;
  }

  function timeAgo (date) {
    const diff = Date.now() - date.getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 5) return '刚刚';
    if (sec < 60) return sec + ' 秒前';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + ' 分钟前';
    return date.toLocaleTimeString();
  }

  function fmtUptime (sec) {
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h + 'h ' + m + 's';
  }

  function formatNum (n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  function statusClass (status) {
    switch ((status || '').toLowerCase()) {
      case 'running': case 'processing': return 'running';
      case 'pending': case 'waiting':    return 'pending';
      case 'done': case 'completed':
      case 'success': case 'ok':         return 'done';
      case 'failed': case 'error':
      case 'dead': case 'unhealthy':     return 'failed';
      default:                           return 'pending';
    }
  }

  /* 状态点：替代 emoji 指示符，颜色由 token 化 CSS 类控制 */
  function statusDot (status) {
    return elt('span', 'dot dot-' + statusClass(status));
  }

  // ----------------------------------------------------------
  // API fetcher
  // ----------------------------------------------------------
  const TOKEN_KEY = 'docpipe_token';

  function getToken () { return sessionStorage.getItem(TOKEN_KEY) || ''; }

  /* ---- 主题（浅色默认；data-theme 切换；偏好持久化到 localStorage） ---- */
  const THEME_KEY = 'loom_dashboard_theme';

  function applyTheme (theme) {
    const t = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', t);
    if (els.themeToggle) {
      els.themeToggle.textContent = t === 'dark' ? '浅色' : '深色';
    }
  }

  function initTheme () {
    let stored = '';
    try { stored = localStorage.getItem(THEME_KEY) || ''; } catch (e) { /* 隐私模式 */ }
    applyTheme(stored === 'dark' ? 'dark' : 'light');
    if (els.themeToggle) {
      els.themeToggle.addEventListener('click', () => {
        const now = document.documentElement.getAttribute('data-theme') === 'dark'
          ? 'light' : 'dark';
        applyTheme(now);
        try { localStorage.setItem(THEME_KEY, now); } catch (e) { /* 忽略 */ }
      });
    }
  }

  async function fetchJson (url) {
    const token = getToken();
    const headers = token ? { 'Authorization': 'Bearer ' + token } : {};
    const resp = await fetch(url, { signal: AbortSignal.timeout(4000), headers });
    if (resp.status === 401 && !url.includes('/health')) {
      // 凭证缺失/失效 → 请求 token 后重试一次
      requestToken();
      throw new Error('HTTP 401（需要 API Token）');
    }
    if (!resp.ok) {
      const err = new Error('HTTP ' + resp.status);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  function requestToken () {
    if (els.tokenPrompt && els.tokenPrompt.style.display !== 'flex') {
      els.tokenPrompt.style.display = 'flex';
      if (els.tokenInput) els.tokenInput.focus();
    }
  }

  async function fetchAll () {
    const results = await Promise.allSettled([
      fetchJson(API_BASE + '/health'),
      fetchJson(API_BASE + '/api/dashboard'),
      fetchJson(API_BASE + '/api/cost'),
      fetchJson(API_BASE + '/api/quality/feedback'),
      fetchJson(API_BASE + '/api/logs?limit=30&since=86400'),
      fetchJson(API_BASE + '/api/approvals'),
      fetchJson(API_BASE + '/api/deliveries?limit=' + DELIVERIES_LIMIT),
      /* Settings 面（评审 §4.2，只读 v1）的四源 */
      fetchJson(API_BASE + '/api/config'),
      fetchJson(API_BASE + '/api/webhooks'),
      fetchJson(API_BASE + '/api/versions/stats'),
      fetchJson(API_BASE + '/api/config/audit'),
      /* Workflows & Schedules 面的两源 */
      fetchJson(API_BASE + '/api/locks'),
      fetchJson(API_BASE + '/api/schedules'),
    ]);
    const settle = (r) => {
      if (r.status === 'fulfilled') return r.value;
      if (r.reason && r.reason.status === 403) return { __forbidden: true };
      return { __error: r.reason && r.reason.message || '请求失败' };
    };
    const health = results[0].status === 'fulfilled' ? results[0].value : null;
    const dash = results[1].status === 'fulfilled' ? results[1].value : null;
    return {
      health,
      tasks: dash || null,           // /api/dashboard 含 tasks[].progress/steps 与 agents
      agents: dash || null,
      cost: settle(results[2]),
      quality: settle(results[3]),
      logs: settle(results[4]),
      approvals: settle(results[5]),
      deliveries: settle(results[6]),
      settings: {
        config: settle(results[7]),
        webhooks: settle(results[8]),
        versionStats: settle(results[9]),
        configAudit: settle(results[10]),
      },
      workflows: {
        locks: settle(results[11]),
        schedules: settle(results[12]),
      },
      failedCount: results.filter(r => r.status === 'rejected'
        && (!r.reason || r.reason.status !== 403)).length,
    };
  }

  // ----------------------------------------------------------
  // Renderers
  // ----------------------------------------------------------

  /* ---- Pipeline Status (top-left) ---- */
  function renderStatus (health) {
    if (!health || !health.status) {
      els.statusPanel.replaceChildren(emptyState('i-status', '暂无状态数据'));
      return;
    }

    const ok = health.status === 'ok';
    const statusLabel = ok ? 'ok' : 'error';
    const subs = health.subscribers != null ? health.subscribers : 0;
    const metrics = health.metrics || {};

    // Queue depth: /health 的顶层字段（metrics 子对象里没有该字段）
    const queueDepth = health.queue_depth != null ? health.queue_depth : 0;

    // DB store: messages 为条目数，db_size 为字节数
    let dbLabel = '—';
    if (health.store) {
      const msgs = typeof health.store.messages === 'number' ? health.store.messages : null;
      const bytes = typeof health.store.db_size === 'number' ? health.store.db_size : null;
      if (msgs != null && bytes != null) {
        dbLabel = pluralize(msgs, 'msg') + ' · ' + (bytes / (1024 * 1024)).toFixed(1) + 'MB';
      } else if (msgs != null) {
        dbLabel = pluralize(msgs, 'entry');
      }
    }

    function statCell (label, value, cls) {
      const item = elt('div', 'stat-item');
      item.appendChild(elt('div', 'stat-label', label));
      item.appendChild(elt('div', 'stat-value ' + cls, String(value)));
      return item;
    }

    const grid = elt('div', 'status-grid');

    /* status pill spans 2 cols */
    const pillWrap = elt('div');
    pillWrap.style.cssText = 'grid-column:1/-1;display:flex;justify-content:center;margin-bottom:4px;';
    const pill = elt('span', 'status-pill ' + statusLabel);
    pill.appendChild(elt('span', 'dot'));
    pill.appendChild(document.createTextNode(statusLabel));
    pillWrap.appendChild(pill);
    grid.appendChild(pillWrap);

    /* subtitle stats */
    grid.appendChild(statCell('Subscribers', subs, 'ok'));
    grid.appendChild(statCell('Queue Depth', queueDepth, queueDepth > 10 ? 'warn' : 'ok'));
    grid.appendChild(statCell('DB Store', dbLabel, 'ok'));
    grid.appendChild(statCell('Uptime', metrics.uptime ? fmtUptime(metrics.uptime) : '—', 'ok'));

    els.statusPanel.replaceChildren(grid);

    // Update badge
    if (els.badgeStatus) els.badgeStatus.textContent = statusLabel;
  }

      /* ---- Tasks (top-right) ---- */
  function renderTasks (data) {
    const all = (data && data.tasks) || [];
    const count = data && data.task_count != null ? data.task_count : all.length;
    // Runs 面状态过滤（评审 §4.2）：只影响列表，徽章仍报总任务数。
    const filter = state.taskFilter || 'all';
    const tasks = filter === 'all' ? all
      : all.filter(t => statusClass(t.status) === filter);

    // Update badge
    if (els.badgeTasks) els.badgeTasks.textContent = count;

    if (!tasks.length) {
      els.tasksPanel.replaceChildren(emptyState(
        'i-tasks', filter === 'all' ? '暂无活跃任务' : '该状态下没有任务'));
      return;
    }

    const table = elt('table', 'tasks-table');
    const thead = elt('thead');
    const headRow = elt('tr');
    for (const h of ['ID', 'Pipeline', 'Status', 'Progress']) {
      headRow.appendChild(elt('th', null, h));
    }
    thead.appendChild(headRow);

    const tbody = elt('tbody');
    for (const t of tasks) {
      const sCls = statusClass(t.status);
      // 终态完成的运行没有"进行中的进度"可报（投影里 progress=0）：画满而非 0%，
      // 否则一个 done 的任务常年显示 0%，看起来像卡死。
      const isDone = sCls === 'done';
      const progress = t.progress != null ? Number(t.progress) || 0 : 0;
      const pct = isDone ? 100 : Math.min(100, Math.max(0, Math.round(progress)));

      const tdId = elt('td', 'task-id', t.id == null ? '' : String(t.id));
      tdId.title = tdId.textContent;

      const badge = elt('span', 'badge-status ' + sCls);
      badge.appendChild(statusDot(t.status));
      badge.appendChild(document.createTextNode(' ' + (t.status || 'unknown')));
      const tdStatus = elt('td');
      tdStatus.appendChild(badge);

      const wrap = elt('div');
      wrap.style.cssText = 'display:flex;align-items:center;gap:6px;';
      const pwrap = elt('div', 'progress-wrap');
      pwrap.style.flex = '1';
      const fill = elt('div', 'progress-fill ' + sCls);
      fill.style.width = pct + '%';
      pwrap.appendChild(fill);
      wrap.appendChild(pwrap);
      wrap.appendChild(elt('span', 'progress-text', pct + '%'));
      const tdProgress = elt('td');
      tdProgress.appendChild(wrap);

      const tr = elt('tr');
      tr.dataset.taskId = t.id == null ? '' : String(t.id);
      // S2 入口：任务 ID 可点 → 打开运行详情（DAG + 审批操作）
      if (tdId) {
        tdId.classList.add('task-id-link');
        tdId.title = '查看运行详情';
      }
      tr.appendChild(tdId);
      tr.appendChild(elt('td', 'task-pipeline', t.pipeline || '—'));
      tr.appendChild(tdStatus);
      tr.appendChild(tdProgress);
      if ((t.status || '').toLowerCase() === 'running') {
        tr.classList.add('expandable');
        tr.title = '点击展开实时进度';
        tr.addEventListener('click', () => toggleLiveStream(String(t.id)));
        makeKeyboardActivatable(tr, () => toggleLiveStream(String(t.id)));
      } else {
        tr.classList.add('row-clickable');
        tr.title = '查看运行详情';
        tr.addEventListener('click', () => openRun(String(t.id)));
        makeKeyboardActivatable(tr, () => openRun(String(t.id)));
      }
      tbody.appendChild(tr);
      if (liveStreams.has(String(t.id))) {
        tbody.appendChild(liveStreams.get(String(t.id)).row);
      }
    }

    table.appendChild(thead);
    table.appendChild(tbody);
    els.tasksPanel.replaceChildren(table);
  }

  /* ==========================================================
     S2 运行详情（spec §4.2「最关键一屏，运行时卖点全靠它可见」）
     —— DAG 有向图：节点四态 + when 跳过虚线分支 + 检查点续跑标 +
        每节点耗时/重试次数；等待中的审批闸门带放行/驳回操作。
     数据契约：GET /tasks/<id> 的 dag{nodes[],layers[]} 与
     awaiting_approval / approvals（tests/test_task_dag_view.py 钉住）。
     ========================================================== */
  const runPanel = $('#panel-run');
  const runBody = $('#run-body');
  const runMeta = $('#run-meta');
  const runBack = $('#run-back');
  let runTimer = null;
  let currentRunId = null;

  const NODE_STATE_LABEL = {
    success: '成功', skipped: '跳过', failed: '失败', retried: '重试后成功',
    running: '运行中', pending: '等待',
  };

  function nodeVisualState (n) {
    // retried 是派生态（attempts>1 且最终成功）——引擎不另存"重试态"字段
    if (n.retried) return 'retried';
    const s = (n.status || 'pending').toLowerCase();
    if (s === 'success' || s === 'done') return 'success';
    if (s === 'failed' || s === 'error') return 'failed';
    if (s === 'skipped') return 'skipped';
    if (s === 'running') return 'running';
    return 'pending';
  }

  function fmtDuration (ms) {
    if (ms == null) return '—';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(1) + 's';
  }

  // ── Runs 面深链（评审 §4.2）：#/runs/<task_id> 与列表点击共用一条路由 ──
  function runIdFromHash () {
    const h = String(location.hash || '');
    if (!h.startsWith(RUN_HASH_PREFIX)) return null;
    const raw = h.slice(RUN_HASH_PREFIX.length);
    try { return decodeURIComponent(raw) || null; } catch (e) { return raw || null; }
  }

  function setRunHash (taskId) {
    const want = RUN_HASH_PREFIX + encodeURIComponent(String(taskId));
    if (location.hash === want) return;
    try {
      location.hash = want;   // 进历史栈：浏览器后退即回列表
    } catch (e) { /* file:// 等环境限制：退化但不阻断打开 */ }
  }

  function clearRunHash () {
    if (!location.hash) return;
    try {
      history.replaceState(null, '', location.pathname + location.search);
    } catch (e) {
      location.hash = '';
    }
  }

  /* hash → 视图：深链直达、浏览器前进/后退都只走这一处 */
  function routeFromHash () {
    const id = runIdFromHash();
    if (id) {
      if (currentRunId !== id) openRun(id, { fromHash: true });
    } else if (currentRunId) {
      closeRun({ fromHash: true });
    }
  }

  async function openRun (taskId, opts) {
    if (!taskId) return;
    if (!opts || !opts.fromHash) setRunHash(taskId);
    if (currentRunId === taskId && !runPanel.hidden) return;
    currentRunId = taskId;
    document.querySelectorAll('.card').forEach(c => {
      if (c.id !== 'panel-run') c.hidden = true;
    });
    $('#error-overlay').style.display = 'none';
    runPanel.hidden = false;
    runBody.replaceChildren(elt('div', 'run-loading', '加载运行详情…'));
    await refreshRun();
    if (runTimer) clearInterval(runTimer);
    runTimer = setInterval(refreshRun, 3000);
  }

  function closeRun (opts) {
    if (!opts || !opts.fromHash) clearRunHash();
    if (runTimer) { clearInterval(runTimer); runTimer = null; }
    if (inspectorTimer) { clearInterval(inspectorTimer); inspectorTimer = null; }
    currentRunId = null;
    runPanel.hidden = true;
    if (inspector) inspector.hidden = true;
    document.querySelectorAll('.card').forEach(c => {
      if (c.id !== 'panel-run') c.hidden = false;
    });
    runBody.replaceChildren(emptyState('i-inbox', '选择一条任务查看运行详情'));
    runMeta.textContent = '--';
  }

  async function refreshRun () {
    if (!currentRunId) return;
    try {
      const d = await fetchJson(API_BASE + '/tasks/' + encodeURIComponent(currentRunId));
      renderRun(d);
    } catch (e) {
      runBody.replaceChildren(emptyState('i-warn', '读取运行详情失败：' + e.message));
    }
  }

  function renderRun (d) {
    const dag = d.dag || { nodes: [], layers: [] };
    runMeta.textContent = (d.pipeline || '—') + ' · ' + (d.status || '—');

    const frag = document.createDocumentFragment();

    // ── 审批闸门操作条（等待中的 approval 节点） ──
    const gate = d.awaiting_approval;
    if (gate && Array.isArray(gate.nodes) && gate.nodes.length) {
      frag.appendChild(renderApprovalBar(d.id, gate));
    }

    // ── DAG 分层图 ──
    if (!dag.nodes.length) {
      frag.appendChild(emptyState('i-inbox', '该任务没有 DAG 节点信息'));
    } else {
      const byName = {};
      dag.nodes.forEach(n => { byName[n.name] = n; });
      const layers = (dag.layers && dag.layers.length) ? dag.layers
        : dag.nodes.map(n => [n.name]);
      const flow = elt('div', 'dag-flow');
      renderCheckpointMarkers(dag, flow);
      layers.forEach((layer, li) => {
        if (li > 0) flow.appendChild(elt('div', 'dag-arrow', '▼'));
        const col = elt('div', 'dag-layer');
        layer.forEach(name => {
          const node = byName[name];
          if (node) col.appendChild(renderNode(node, d.id));
        });
        flow.appendChild(col);
      });
      frag.appendChild(flow);
    }

    // ── 决策流水 ──
    if (Array.isArray(d.approvals) && d.approvals.length) {
      frag.appendChild(renderApprovalHistory(d.approvals));
    }

    // ── 错误与产物 ──
    if (d.error) {
      const box = elt('div', 'run-error');
      box.appendChild(iconEl('i-warn'));
      box.appendChild(document.createTextNode(d.error));
      frag.appendChild(box);
    }
    if (d.output_path) {
      const p = elt('div', 'run-output');
      p.appendChild(iconEl('i-download'));
      p.appendChild(document.createTextNode('产物：' + d.output_path));
      frag.appendChild(p);
    }

    runBody.replaceChildren(frag);
  }

  function renderNode (n, taskId) {
    const state = nodeVisualState(n);
    const box = elt('div', 'dag-node dag-' + state);
    // S3：节点可点 → 打开右侧检查器抽屉（spec §4.2 统一骨架）
    box.classList.add('dag-node-clickable');
    box.addEventListener('click', (e) => {
      e.stopPropagation();
      openInspector(taskId, n.name);
    });
    makeKeyboardActivatable(box, () => openInspector(taskId, n.name));
    // when 不成立 = 作者声明的可选分支 → 虚线边框（spec §4.2 S2）
    if (state === 'skipped' && n.skip_reason === 'condition') {
      box.classList.add('dag-skip-condition');
      box.title = 'when 条件不成立——作者声明的可选分支（下游不受影响）· 点击查看节点明细';
    } else if (state === 'skipped') {
      box.title = '依赖失败被级联跳过 · 点击查看节点明细';
    } else if (n.error) {
      box.title = n.error + ' · 点击查看节点明细';
    } else {
      box.title = '点击查看节点明细';
    }
    const head = elt('div', 'dag-node-head');
    head.appendChild(statusDot(state));
    head.appendChild(elt('span', 'dag-node-name', n.name || '—'));
    if (n.retried) head.appendChild(elt('span', 'dag-retried-flag', '重试'));
    box.appendChild(head);
    const meta = elt('div', 'dag-node-meta');
    meta.textContent = NODE_STATE_LABEL[state] || state;
    if (n.attempts > 1) meta.textContent += ' · ' + n.attempts + ' 次尝试';
    if (n.duration_ms != null) meta.textContent += ' · ' + fmtDuration(n.duration_ms);
    box.appendChild(meta);
    if (n.error) {
      const err = elt('div', 'dag-node-err');
      err.textContent = n.error;
      box.appendChild(err);
    }
    return box;
  }

  /* ==========================================================
     S3 节点检查器（spec §4.2）——右侧抽屉：artifact 结构预览 /
     幂等键 / 熔断限流态 / 质量门明细 / 断点视图。
     数据契约：GET /tasks/<id>/nodes/<name>（tests/test_node_inspector.py）。
     "取不到就不编"：熔断/限流/质量门为 null 时抽屉如实显示"未建立"，
     不像旧监控台那样给一个看起来有值的假读数。
     ========================================================== */
  const inspector = $('#node-inspector');
  const inspectorBody = $('#inspector-body');
  const inspectorTitle = $('#inspector-title');
  let inspectorTimer = null;

  function openInspector (taskId, nodeName) {
    if (!taskId || !nodeName) return;
    if (inspectorTimer) { clearInterval(inspectorTimer); inspectorTimer = null; }
    if (inspector) inspector.hidden = false;
    if (inspectorTitle) inspectorTitle.textContent = nodeName;
    if (inspectorBody) inspectorBody.replaceChildren(elt('div', 'run-loading', '加载节点明细…'));
    const tick = async () => {
      try {
        const d = await fetchJson(API_BASE + '/tasks/' + encodeURIComponent(taskId)
          + '/nodes/' + encodeURIComponent(nodeName));
        renderInspector(d);
      } catch (e) {
        if (inspectorBody) {
          inspectorBody.replaceChildren(emptyState('i-warn', '读取节点明细失败：' + e.message));
        }
      }
    };
    tick();
    // 运行中的任务：跟着刷新，让重试计数/熔断态实时可见
    inspectorTimer = setInterval(tick, 3000);
  }

  function closeInspector () {
    if (inspectorTimer) { clearInterval(inspectorTimer); inspectorTimer = null; }
    if (inspector) inspector.hidden = true;
    if (inspectorBody) inspectorBody.replaceChildren();
  }

  function inspectorRow (label, value, cls) {
    const row = elt('div', 'insp-row');
    row.appendChild(elt('span', 'insp-label', label));
    const v = elt('span', 'insp-value' + (cls ? ' ' + cls : ''), value == null ? '—' : value);
    row.appendChild(v);
    return row;
  }

  function renderInspector (d) {
    const frag = document.createDocumentFragment();
    const node = d.node || {};
    // 跨进程运行从 RunStore 投影答（admin_api._node_inspector_from_projection）：
    // 投影只存节点摘要——四类进程内明细不落投影，文案要如实换，不能把 None 说成"没有"。
    const projection = d.source === 'runstore';

    // ── 状态与执行 ──
    const st = elt('div', 'insp-block');
    st.appendChild(elt('h4', null, '执行'));
    st.appendChild(inspectorRow('Agent', node.agent));
    st.appendChild(inspectorRow('状态', (NODE_STATE_LABEL[node.status] || node.status)));
    st.appendChild(inspectorRow('尝试次数', node.attempts + ' / ' + (node.max_retries != null ? node.max_retries : '?')));
    st.appendChild(inspectorRow('依赖', (node.dependencies || []).join(', ') || '无'));
    if (node.skip_reason) {
      st.appendChild(inspectorRow('跳过原因',
        node.skip_reason === 'condition' ? 'when 条件不成立（作者声明的可选分支）' : '依赖失败级联跳过'));
    }
    if (node.error) {
      const err = elt('div', 'insp-error');
      err.textContent = node.error;
      st.appendChild(err);
    }
    frag.appendChild(st);

    if (projection) {
      frag.appendChild(elt('div', 'insp-note',
        '跨进程运行：以下为 RunStore 投影摘要。节点正文、幂等落库态、熔断/限流与质量门'
        + '明细为进程内状态、不落投影——按「取不到就不编」显示为不可用。'));
    }

    // ── artifact 结构预览 ──
    const art = d.artifact || {};
    const ab = elt('div', 'insp-block');
    ab.appendChild(elt('h4', null, '产物（artifact）'));
    if (projection) {
      const keys = node.artifact_keys || [];
      if (!keys.length) {
        ab.appendChild(elt('div', 'insp-empty', '该节点没有产物键'));
      } else {
        const table = elt('div', 'insp-artifact');
        for (const k of keys) {
          const row = elt('div', 'insp-art-row');
          row.appendChild(elt('span', 'insp-art-key', k));
          row.appendChild(elt('span', 'insp-art-type', '—'));
          row.appendChild(elt('span', 'insp-art-val', '投影不存正文（跨进程运行）'));
          table.appendChild(row);
        }
        ab.appendChild(table);
      }
    } else if (!art.keys || !art.keys.length) {
      ab.appendChild(elt('div', 'insp-empty', '该节点没有产出'));
    } else {
      const table = elt('div', 'insp-artifact');
      for (const k of art.keys) {
        const p = (art.preview || {})[k] || {};
        const row = elt('div', 'insp-art-row');
        row.appendChild(elt('span', 'insp-art-key', k));
        row.appendChild(elt('span', 'insp-art-type', p.type || '—'));
        const val = p.type === 'str'
          ? (p.value || '') + (p.length > 200 ? '（共 ' + p.length + ' 字符，已截断）' : '')
          : (p.type === 'list' ? p.length + ' 项'
            : p.type === 'dict' ? (p.keys || []).join(', ')
            : JSON.stringify(p.value));
        row.appendChild(elt('span', 'insp-art-val', val));
        table.appendChild(row);
      }
      ab.appendChild(table);
      if (art.truncated) {
        ab.appendChild(elt('div', 'insp-note', '长文本已截断显示'));
      }
    }
    frag.appendChild(ab);

    // ── 幂等键 ──
    const idem = d.idempotency || {};
    const ib = elt('div', 'insp-block');
    ib.appendChild(elt('h4', null, '幂等键'));
    const key = elt('code', 'insp-key', idem.key || '—');
    ib.appendChild(key);
    ib.appendChild(inspectorRow('已记录',
      idem.recorded == null
        ? (projection ? '不落投影（跨进程运行）' : '查不到（store 未就绪）')
        : (idem.recorded ? '是——重复执行会被跳过' : '否——首次执行'),
      idem.recorded ? 'insp-ok' : 'insp-muted'));
    frag.appendChild(ib);

    // ── 熔断 / 限流 ──
    const cbv = d.circuit_breaker, rlv = d.rate_limit;
    const rb = elt('div', 'insp-block');
    rb.appendChild(elt('h4', null, '熔断与限流'));
    if (cbv) {
      rb.appendChild(inspectorRow('熔断器', cbv.state + '（连续失败 ' + (cbv.failure_count || 0) + ' 次）',
        cbv.state === 'CLOSED' ? 'insp-ok' : 'insp-warn'));
    } else {
      rb.appendChild(elt('div', 'insp-empty',
        projection ? '不落投影（跨进程运行）' : '熔断器未建立（该 Agent 尚未被熔断逻辑接管）'));
    }
    if (rlv) {
      rb.appendChild(inspectorRow('限流', 'rate ' + rlv.rate + ' / burst ' + rlv.burst
        + ' / 余量 ' + rlv.available_tokens));
      rb.appendChild(inspectorRow('累计', '放行 ' + rlv.total_acquired + ' / 阻塞 ' + rlv.total_blocked));
    } else {
      rb.appendChild(elt('div', 'insp-empty',
        projection ? '不落投影（跨进程运行）' : '限流器未建立（该 Agent 未配 rate_limit）'));
    }
    frag.appendChild(rb);

    // ── 质量门明细 ──
    if (d.quality) {
      const q = d.quality;
      const qb = elt('div', 'insp-block');
      qb.appendChild(elt('h4', null, '质量门明细'));
      qb.appendChild(inspectorRow('总分', q.overall_score + (q.threshold != null ? ' / 阈值 ' + q.threshold : ''),
        (q.threshold != null && q.overall_score < q.threshold) ? 'insp-warn' : 'insp-ok'));
      if (q.scores) {
        for (const [dim, sc] of Object.entries(q.scores)) {
          qb.appendChild(inspectorRow(dim, sc));
        }
      }
      if (q.profile) qb.appendChild(inspectorRow('画像', q.profile));
      if (q.needs_regenerate) qb.appendChild(inspectorRow('需重生成', '是'));
      if (q.style_issues && q.style_issues.length) {
        const issues = elt('div', 'insp-issues');
        q.style_issues.forEach(s => issues.appendChild(elt('div', 'insp-issue', s)));
        qb.appendChild(issues);
      }
      frag.appendChild(qb);
    }

    // ── 断点视图 ──
    const cp = d.checkpoint;
    if (cp) {
      const cpb = elt('div', 'insp-block');
      cpb.appendChild(elt('h4', null, '断点续跑'));
      cpb.appendChild(inspectorRow('断点文件', cp.exists ? '已落盘' : '未落盘',
        cp.exists ? 'insp-ok' : 'insp-muted'));
      cpb.appendChild(inspectorRow('本次为续跑', cp.resumed ? '是' : '否', cp.resumed ? 'insp-warn' : 'insp-muted'));
      cpb.appendChild(inspectorRow('已完成节点', cp.completed_nodes));
      frag.appendChild(cpb);
    }

    if (inspectorBody) inspectorBody.replaceChildren(frag);
  }

  // S2 DAG 上的断点续跑标记：有断点且是续跑时在层流顶部打一个角标
  function renderCheckpointMarkers (dag, flowEl) {
    const cp = dag && dag.checkpoint;
    if (!cp || !flowEl) return;
    if (cp.exists) {
      const badge = elt('div', 'dag-checkpoint-badge'
        + (cp.resumed ? ' dag-checkpoint-resumed' : ''));
      badge.textContent = cp.resumed
        ? '断点续跑（已完成 ' + cp.completed_nodes + ' 节点）'
        : '断点已落盘（' + cp.completed_nodes + ' 节点完成）';
      flowEl.insertBefore(badge, flowEl.firstChild);
    }
  }

  function renderApprovalBar (taskId, gate) {
    const bar = elt('div', 'approval-bar');
    const left = elt('div', 'approval-msg');
    left.appendChild(iconEl('i-key'));
    const msgs = gate.messages || {};
    const first = gate.nodes.map(n => msgs[n]).find(Boolean);
    left.appendChild(document.createTextNode('等待审批：' + (first || gate.nodes.join(', '))));
    bar.appendChild(left);
    const right = elt('div', 'approval-actions');
    right.appendChild(approvalButton(taskId, 'approve', '放行', 'btn-approve'));
    right.appendChild(approvalButton(taskId, 'reject', '驳回', 'btn-reject'));
    bar.appendChild(right);
    return bar;
  }

  function approvalButton (taskId, action, label, cls) {
    const b = elt('button', cls, label);
    b.type = 'button';
    b.disabled = true; // 有副作用：先要二次确认，避免手滑
    b.title = action === 'reject' ? '终止任务（可填原因）' : '继续执行后续节点';
    b.addEventListener('click', async () => {
      let note = '';
      if (action === 'reject') {
        note = window.prompt('驳回原因（可留空）：');
        if (note === null) return;
      }
      if (!window.confirm(action === 'approve' ? '确认放行该审批？' : '确认驳回并终止该任务？')) return;
      b.disabled = true;
      b.textContent = '提交中…';
      try {
        await postJson(API_BASE + '/tasks/' + encodeURIComponent(taskId) + '/' + action,
                      action === 'reject' ? { reason: note } : {});
        await refreshRun();
      } catch (e) {
        b.textContent = label;
        b.disabled = false;
        window.alert('审批失败：' + e.message);
      }
    });
    // 守卫：1.5 秒后才能点（防误触）；仍保留 confirm 二次确认
    setTimeout(() => { b.disabled = false; }, 1500);
    return b;
  }

  function renderApprovalHistory (approvals) {
    const wrap = elt('div', 'approval-history');
    wrap.appendChild(elt('h3', null, '审批决策流水'));
    const ul = elt('ul');
    approvals.forEach(a => {
      const li = elt('li');
      const tag = elt('span', 'decision decision-' + (a.decision || 'pending'),
                      a.decision || 'pending');
      li.appendChild(tag);
      let text = (a.nodes || []).join(', ');
      if (a.note) text += ' —— ' + a.note;
      if (a.reason) text += ' —— ' + a.reason;
      li.appendChild(document.createTextNode(text));
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    return wrap;
  }

  /* ---- Agents (bottom-left) ---- */
  function renderAgents (data) {
    // agents can be an object { name: {...} } or an array
    let list = [];
    if (data && data.agents) {
      if (Array.isArray(data.agents)) {
        list = data.agents;
      } else if (typeof data.agents === 'object') {
        list = Object.entries(data.agents).map(([name, info]) => ({
          name: name,
          ...(typeof info === 'object' ? info : { status: String(info) }),
        }));
      }
    }

    const count = data && data.count != null ? data.count : list.length;
    if (els.badgeAgents) els.badgeAgents.textContent = count;

    if (!list.length) {
      els.agentsPanel.replaceChildren(emptyState('i-agents', 'No agents registered'));
      return;
    }

    const container = elt('div', 'agents-list');
    for (const a of list) {
      const s = (a.status || 'ok').toLowerCase();
      const sTag = s === 'ok' || s === 'healthy' ? 'ok'
                 : s === 'warn' || s === 'warning' || s === 'degraded' ? 'warn'
                 : 'error';
      const itemCls = sTag === 'error' ? 'agent-item error'
                    : sTag === 'warn' ? 'agent-item warn'
                    : 'agent-item';

      const detail = a.type || a.role || a.pipeline || a.host || '';

      const item = elt('div', itemCls);
      const info = elt('div', 'agent-info');
      info.appendChild(elt('span', 'agent-name', a.name || 'agent'));
      if (detail) info.appendChild(elt('span', 'agent-detail', detail));
      item.appendChild(info);
      item.appendChild(elt('span', 'agent-status-tag ' + sTag, a.status || 'ok'));
      container.appendChild(item);
    }
    els.agentsPanel.replaceChildren(container);
  }

  /* ---- Metrics (bottom-right) ---- */
  function renderMetrics (health) {
    const metrics = (health && health.metrics) || {};

    // Normalise metric names (accept both camelCase and snake_case)
    const getMetric = (key) => {
      const val = metrics[key];
      if (val != null) return val;
      // Try alternate naming
      const alt = key.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
      return metrics[alt];
    };

    const sent     = getMetric('sent') ?? 0;
    const received = getMetric('received') ?? 0;
    const failed   = getMetric('failed') ?? 0;
    const retried  = getMetric('retried') ?? 0;
    const dlq      = (metrics.dlq != null ? metrics.dlq
                    : (metrics.dlq_count != null ? metrics.dlq_count
                    : (health.dlq_count != null ? health.dlq_count
                    : (health.dlq_entries != null ? health.dlq_entries.length
                    : 0))));

    const items = [
      { icon: 'i-send',     label: 'Sent',     value: sent,     cls: 'ok' },
      { icon: 'i-download', label: 'Received', value: received, cls: 'ok' },
      { icon: 'i-x',        label: 'Failed',   value: failed,   cls: failed > 0 ? 'error' : 'ok' },
      { icon: 'i-refresh',  label: 'Retried',  value: retried,  cls: retried > 0 ? 'warn' : 'ok' },
      { icon: 'i-trash',    label: 'DLQ',      value: dlq,      cls: dlq > 0 ? 'error' : 'ok' },
    ];

    const grid = elt('div', 'metrics-grid');
    for (const m of items) {
      const card = elt('div', 'metric-card');
      const iconWrap = elt('div', 'metric-icon');
      iconWrap.appendChild(iconEl(m.icon));
      card.appendChild(iconWrap);
      const body = elt('div', 'metric-body');
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', formatNum(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }
    els.metricsPanel.replaceChildren(grid);
  }

  function renderPanelError (data, panel, badge, icon) {
    if (!data || (!data.__forbidden && !data.__error)) return false;
    panel.replaceChildren(emptyState(icon,
      data.__forbidden ? '需要 admin 权限' : '读取失败：' + data.__error));
    if (badge) badge.textContent = '--';
    return true;
  }

  /* ---- Quality (bottom) ---- */
  function renderQuality (data) {
    if (renderPanelError(data, els.qualityPanel, els.badgeQuality, 'i-quality')) return;
    if (!data || data.total_records == null) {
      els.qualityPanel.replaceChildren(emptyState('i-quality', '暂无质量数据'));
      if (els.badgeQuality) els.badgeQuality.textContent = '--';
      return;
    }
    if (els.badgeQuality) {
      els.badgeQuality.textContent = 'avg ' + (data.avg_score ?? 0);
    }

    const grid = elt('div', 'metrics-grid');
    const items = [
      { label: 'Avg Score', value: data.avg_score ?? 0, cls: (data.avg_score ?? 0) >= 70 ? 'ok' : 'warn' },
      { label: 'Tasks',     value: data.total_tasks ?? 0, cls: 'ok' },
      { label: 'Records',   value: data.total_records ?? 0, cls: 'ok' },
      { label: 'Weak',      value: data.total_weak ?? 0, cls: (data.total_weak ?? 0) > 0 ? 'error' : 'ok' },
    ];
    for (const m of items) {
      const card = elt('div', 'metric-card');
      card.appendChild(elt('div', 'metric-body'));
      const body = card.firstChild;
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', formatNum(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }

    // 弱项模式提示（最多 3 条）
    const weak = Array.isArray(data.weak_patterns) ? data.weak_patterns.slice(0, 3) : [];
    if (weak.length) {
      const list = elt('div', 'agents-list');
      for (const w of weak) {
        const name = typeof w === 'string' ? w : (w.pattern || w.name || JSON.stringify(w).slice(0, 40));
        list.appendChild(elt('div', 'agent-item warn', String(name)));
      }
      grid.appendChild(list);
    }
    els.qualityPanel.replaceChildren(grid);
  }

  /* ---- Cost (bottom) ---- */
  function renderCost (data) {
    if (renderPanelError(data, els.costPanel, els.badgeCost, 'i-cost')) return;
    if (!data || data.total_cost == null) {
      els.costPanel.replaceChildren(emptyState('i-cost', '暂无成本数据'));
      if (els.badgeCost) els.badgeCost.textContent = '--';
      return;
    }
    const total = Number(data.total_cost) || 0;
    if (els.badgeCost) els.badgeCost.textContent = '$' + total.toFixed(4);

    const grid = elt('div', 'metrics-grid');
    let remainingCls = 'ok';
    let remainingText = '不限';
    if (data.budget_remaining != null) {
      remainingText = '$' + Number(data.budget_remaining).toFixed(4);
      remainingCls = data.budget_exceeded ? 'error' : (Number(data.budget_remaining) / (Number(data.budget) || 1) < 0.2 ? 'warn' : 'ok');
    }
    const items = [
      { label: 'Total',     value: '$' + total.toFixed(4), cls: 'ok' },
      { label: 'Budget',    value: data.budget > 0 ? '$' + Number(data.budget).toFixed(2) : '不限', cls: 'ok' },
      { label: 'Remaining', value: remainingText, cls: remainingCls },
      { label: 'Exceeded',  value: data.budget_exceeded ? 'Yes' : 'No', cls: data.budget_exceeded ? 'error' : 'ok' },
    ];
    for (const m of items) {
      const card = elt('div', 'metric-card');
      const body = elt('div', 'metric-body');
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', String(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }

    // 按供应商成本（最多 5 行）
    const byProvider = data.by_provider && typeof data.by_provider === 'object' ? data.by_provider : {};
    const rows = Object.entries(byProvider).slice(0, 5);
    if (rows.length) {
      const list = elt('div', 'agents-list');
      for (const [name, info] of rows) {
        const cost = typeof info === 'number' ? info : (info && (info.cost ?? info.total_cost)) || 0;
        const item = elt('div', 'agent-item');
        const infoEl = elt('div', 'agent-info');
        infoEl.appendChild(elt('span', 'agent-name', name));
        item.appendChild(infoEl);
        item.appendChild(elt('span', 'agent-detail', '$' + (Number(cost) || 0).toFixed(4)));
        list.appendChild(item);
      }
      grid.appendChild(list);
    }
    els.costPanel.replaceChildren(grid);
  }

  /* ---- Logs (full-width bottom) ---- */
  function renderLogs (data) {
    if (renderPanelError(data, els.logsPanel, els.badgeLogs, 'i-logs')) return;
    const logs = (data && Array.isArray(data.logs)) ? data.logs : [];
    if (els.badgeLogs) els.badgeLogs.textContent = String(logs.length);

    if (!logs.length) {
      els.logsPanel.replaceChildren(emptyState('i-logs', '暂无日志'));
      return;
    }

    const table = elt('table', 'tasks-table logs-table');
    const thead = elt('thead');
    const headRow = elt('tr');
    for (const h of ['Time', 'Level', 'Agent', 'Message']) {
      headRow.appendChild(elt('th', null, h));
    }
    thead.appendChild(headRow);

    const tbody = elt('tbody');
    for (const entry of logs.slice(0, 30)) {
      const tr = elt('tr');
      const ts = Number(entry.timestamp) || 0;
      const time = ts > 0 ? new Date(ts * 1000).toLocaleTimeString() : '--';
      tr.appendChild(elt('td', 'task-id', time));
      const level = String(entry.level || 'info').toLowerCase();
      tr.appendChild(elt('td', 'log-level ' + level, level.toUpperCase()));
      tr.appendChild(elt('td', 'task-pipeline', String(entry.agent || '-')));
      const msg = String(entry.message || entry.event || entry.msg || '');
      const tdMsg = elt('td', 'log-msg', msg.slice(0, 160));
      tdMsg.title = msg;
      tr.appendChild(tdMsg);
      tbody.appendChild(tr);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
    els.logsPanel.replaceChildren(table);
  }

  // ----------------------------------------------------------
  // Live SSE progress (running task rows)
  // ----------------------------------------------------------
  const liveStreams = new Map();

  function toggleLiveStream (id) {
    if (liveStreams.has(id)) {
      closeLiveStream(id, 0);
      return;
    }
    const cell = elt('td');
    cell.colSpan = 4;
    const wrap = elt('div', 'live-stream');
    wrap.appendChild(elt('div', 'live-label', '实时进度 · ' + id));
    const meta = elt('div', 'live-meta');
    const bar = elt('div', 'progress-wrap live-bar');
    const fill = elt('div', 'progress-fill running');
    fill.style.width = '0%';
    bar.appendChild(fill);
    const text = elt('span', 'progress-text', '0%');
    const sectionName = elt('span', 'live-section', '等待章节事件…');
    meta.appendChild(bar);
    meta.appendChild(text);
    meta.appendChild(sectionName);
    wrap.appendChild(meta);
    cell.appendChild(wrap);
    const row = elt('tr', 'stream-row');
    row.appendChild(cell);

    const token = getToken();
    const url = API_BASE + '/stream?task_id=' + encodeURIComponent(id)
      + (token ? '&token=' + encodeURIComponent(token) : '');
    const es = new EventSource(url);

    liveStreams.set(id, { es, row, fill, text, sectionName });

    es.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      handleLiveEvent(id, msg);
    };
    es.onerror = () => {
      sectionName.textContent = '连接断开';
      closeLiveStream(id, 0);
    };
  }

  function handleLiveEvent (id, msg) {
    const h = liveStreams.get(id);
    if (!h) return;
    const d = (msg && msg.data) || {};
    switch (msg && msg.type) {
      case 'section': {
        const total = Number(msg.total) > 0 ? Number(msg.total) : 0;
        const idx = Number(msg.section) >= 0 ? Number(msg.section) : 0;
        const pctv = total ? Math.min(100, Math.max(0, Math.round(((idx + 1) / total) * 100))) : 0;
        h.fill.style.width = pctv + '%';
        h.text.textContent = pctv + '%';
        h.sectionName.textContent = (d.section_name || ('section ' + (idx + 1)))
          + (d.char_count != null ? ' · ' + formatNum(d.char_count) + ' chars' : '');
        break;
      }
      case 'progress': {
        const total = Number(d.total) > 0 ? Number(d.total) : (Number(msg.total) || 0);
        const cur = Number(d.current) || 0;
        const pctv = total ? Math.min(100, Math.max(0, Math.round((cur / total) * 100))) : 0;
        h.fill.style.width = pctv + '%';
        h.text.textContent = pctv + '%';
        if (d.message) h.sectionName.textContent = d.message;
        break;
      }
      case 'complete':
        h.fill.style.width = '100%';
        h.fill.className = 'progress-fill done';
        h.text.textContent = '100%';
        h.sectionName.textContent = '完成';
        closeLiveStream(id, 1500);
        break;
      case 'error':
        h.fill.className = 'progress-fill failed';
        h.sectionName.textContent = (d.error || '生成失败');
        closeLiveStream(id, 1500);
        break;
      default:
        break;
    }
  }

  function closeLiveStream (id, keepRowMs) {
    const h = liveStreams.get(id);
    if (!h) return;
    liveStreams.delete(id);
    setTimeout(() => {
      try { h.es.close(); } catch (_) { /* noop */ }
      if (h.row.parentNode) h.row.parentNode.removeChild(h.row);
    }, keepRowMs || 0);
  }

  // ----------------------------------------------------------
  // New task form
  // ----------------------------------------------------------
  async function postJson (url, body) {
    const token = getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const resp = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      requestToken();
      throw new Error('HTTP 401（需要 API Token）');
    }
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    return data;
  }

  async function loadPipelines () {
    if (!els.ntPipeline) return;
    try {
      const info = await fetchJson(API_BASE + '/api/pipeline');
      const files = (info && info.pipeline_files) || [];
      els.ntPipeline.replaceChildren();
      for (const f of files) {
        const name = String(f).replace(/\.ya?ml$/i, '');
        // 与 CLI 的 --pipeline choices 同口径：下划线前缀是内部片段
        // （如 _quality-tail，供 pack 内联拼装用），不是可独立提交的流水线。
        if (name.startsWith('_')) continue;
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        els.ntPipeline.appendChild(opt);
      }
    } catch (err) {
      console.warn('[Dashboard] 加载流水线列表失败:', err);
    }
  }

  function showTaskMsg (text, isError) {
    if (!els.ntMsg) return;
    els.ntMsg.hidden = false;
    els.ntMsg.textContent = text;
    els.ntMsg.classList.toggle('is-error', !!isError);
  }

  async function submitTask (ev) {
    ev.preventDefault();
    if (!els.ntQuery || !els.ntSubmit) return;
    const query = (els.ntQuery.value || '').trim();
    if (!query) {
      showTaskMsg('请填写 Query', true);
      return;
    }
    const body = { query };
    if (els.ntPipeline && els.ntPipeline.value) body.pipeline = els.ntPipeline.value;
    const output = (els.ntOutput && els.ntOutput.value || '').trim();
    if (output) body.output = output;

    els.ntSubmit.disabled = true;
    showTaskMsg('提交中…', false);
    try {
      const resp = await postJson(API_BASE + '/api/tasks', body);
      showTaskMsg('已提交任务 ' + (resp.task_id || '')
        + '（' + (resp.status || 'pending') + '）', false);
      els.ntQuery.value = '';
      update().catch(() => {});
    } catch (err) {
      showTaskMsg('提交失败：' + (err.message || err), true);
    } finally {
      els.ntSubmit.disabled = false;
    }
  }

  // ----------------------------------------------------------
  // Main update loop
  // ----------------------------------------------------------
  async function update () {
    try {
      state.error = null;
      state.lastUpdated = new Date();
      const data = await fetchAll();
      state.health = data.health;
      state.tasks = data.tasks;
      state.agents = data.agents;
      state.cost = data.cost;
      state.quality = data.quality;
      state.logs = data.logs;
      state.approvals = data.approvals;
      state.deliveries = data.deliveries;
      state.settings = data.settings;
      state.workflows = data.workflows;
      state.partialFail = data.failedCount > 0;

      // If all endpoints failed, treat it as offline
      if (!data.health && !data.tasks) {
        throw new Error('All API endpoints unreachable');
      }

      state.loaded = true;
      render();
    } catch (err) {
      console.warn('[Dashboard] Fetch error:', err);
      state.error = err.message || 'Connection failed';
      renderError();
    }
  }

  function render () {
    if (state.error) return; // error overlay is shown by renderError
    hideError();
    if (!state.loaded) return;

    els.lastUpdated.textContent = '更新于 ' + timeAgo(state.lastUpdated)
      + (state.partialFail ? ' · 部分数据不可用' : '');

    renderStatus(state.health);
    renderTasks(state.tasks);
    renderAgents(state.agents);
    renderApprovals(state.approvals);
    renderMetrics(state.health);
    renderQuality(state.quality);
    renderCost(state.cost);
    renderLogs(state.logs);
    renderDeliveries(state.deliveries);
    renderSettings(state.settings);
    renderWorkflows(state.workflows);
  }

  /* ---- Approvals（跨运行待批收件箱） ---- */
  function renderApprovals (data) {
    if (renderPanelError(data, els.approvalsPanel, els.badgeApprovals, 'i-check')) return;
    const items = (data && Array.isArray(data.approvals)) ? data.approvals : [];
    if (els.badgeApprovals) els.badgeApprovals.textContent = String(items.length);
    if (!items.length) {
      els.approvalsPanel.replaceChildren(emptyState('i-check', '没有等待审批的运行'));
      return;
    }
    const table = elt('table', 'tasks-table');
    const thead = elt('thead');
    const hr = elt('tr');
    for (const h of ['运行', '流水线', '闸门', '等待', '操作']) hr.appendChild(elt('th', null, h));
    thead.appendChild(hr);
    const tbody = elt('tbody');
    for (const a of items) {
      const tr = elt('tr');
      tr.appendChild(elt('td', 'mono', a.run_id || ''));
      tr.appendChild(elt('td', null, a.pipeline || ''));
      tr.appendChild(elt('td', null, (a.nodes || []).join(', ')));
      tr.appendChild(elt('td', null, Math.round(a.waiting_sec || 0) + 's'));
      const td = elt('td');
      const ok = elt('button', 'retry-btn', '放行');
      ok.type = 'button';
      ok.onclick = () => decideApproval(a.run_id, 'approve', '');
      const no = elt('button', 'retry-btn', '驳回');
      no.type = 'button';
      no.onclick = () => {
        const reason = prompt('驳回原因（可空）');
        if (reason === null) return;  // 取消不是"空原因"
        decideApproval(a.run_id, 'reject', reason);
      };
      td.appendChild(ok);
      td.appendChild(no);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
    els.approvalsPanel.replaceChildren(table);
  }

  async function decideApproval (runId, action, text) {
    const token = getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const payload = action === 'approve' ? { note: text || '' } : { reason: text || '' };
    try {
      const resp = await fetch(API_BASE + '/tasks/' + encodeURIComponent(runId) + '/' + action, {
        method: 'POST', headers, body: JSON.stringify(payload),
        signal: AbortSignal.timeout(8000),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
      await update();
    } catch (err) {
      alert('审批操作失败：' + (err.message || err));
    }
  }

  /* ---- Deliveries（交付账本：内容摘要 / 逐节点产物 / 逐运行锁指纹） ---- */

  function fmtBytes (n) {
    // null = 没记录；-1 = 去查过、文件读不到；0 = 空文件。三种真相各自有文案，
    // 合并成"未知"就是把账本的诚实性抹平。
    if (n == null) return '未记录';
    if (n < 0) return '读不到文件';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtShortSha (sha) {
    if (!sha) return null;
    return sha.slice(0, 12) + '…';
  }

  function renderDeliveries (data) {
    if (renderPanelError(data, els.deliveriesPanel, els.badgeDeliveries, 'i-download')) return;
    if (deliveriesDetailOpen) return;  // 明细视图持有面板：轮询只更新数据、不回画
    const items = (data && Array.isArray(data.deliveries)) ? data.deliveries : [];
    if (els.badgeDeliveries) els.badgeDeliveries.textContent = String(items.length);
    if (!items.length) {
      els.deliveriesPanel.replaceChildren(emptyState('i-download', '还没有交付记录'));
      return;
    }
    const table = elt('table', 'tasks-table');
    const thead = elt('thead');
    const hr = elt('tr');
    for (const h of ['运行', '流水线', '状态', '完成', '交付物摘要', '节点 / 产物']) {
      hr.appendChild(elt('th', null, h));
    }
    thead.appendChild(hr);
    const tbody = elt('tbody');
    for (const item of items) {
      const tr = elt('tr', 'row-clickable');
      tr.onclick = () => openDelivery(String(item.run_id || ''));
      tr.appendChild(elt('td', 'mono task-id-link', item.run_id || ''));
      tr.appendChild(elt('td', 'task-pipeline', item.pipeline || ''));
      tr.appendChild(elt('td', 'badge-status badge-' + (item.status || 'unknown'), item.status || ''));
      tr.appendChild(elt('td', null, item.finished_at
        ? new Date(item.finished_at * 1000).toLocaleTimeString() : '--'));
      const short = fmtShortSha(item.output_sha256);
      const shaTd = elt('td', 'mono deliv-sha', short || '未记录');
      if (!short) shaTd.title = '这一跑没有落盘产物，或写在摘要面之前';
      tr.appendChild(shaTd);
      tr.appendChild(elt('td', null, (item.node_count || 0) + ' / ' + (item.artifact_count || 0)));
      tbody.appendChild(tr);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
    els.deliveriesPanel.replaceChildren(table);
  }

  async function openDelivery (runId) {
    try {
      const data = await fetchJson(API_BASE + '/api/deliveries/' + encodeURIComponent(runId));
      deliveriesDetailOpen = true;
      renderDeliveryDetail((data && data.delivery) || {});
    } catch (err) {
      els.deliveriesPanel.replaceChildren(emptyState('i-download',
        '明细读取失败：' + (err.message || err)));
    }
  }

  function inspBlock (title, rows, note) {
    const block = elt('div', 'insp-block');
    block.appendChild(elt('h4', null, title));
    for (const [label, value, cls] of rows) {
      const row = elt('div', 'insp-row');
      row.appendChild(elt('span', 'insp-label', label));
      row.appendChild(elt('span', 'insp-value ' + (cls || ''), value));
      block.appendChild(row);
    }
    if (note) block.appendChild(elt('div', 'insp-note', note));
    return block;
  }

  function artifactDigestBlock (n) {
    /* 逐节点产物摘要：沿用运行详情检查器的三列行（键 / 量 / 摘要），
       同一个"产物"在两处必须长得一样。 */
    const arts = n.artifacts || {};
    const keys = Object.keys(arts).sort();
    const block = elt('div', 'insp-block');
    block.appendChild(elt('h4', null, '节点 ' + n.node + ' 的产物'));
    if (!keys.length) {
      const hadKeys = (n.artifact_keys || []).length > 0;
      block.appendChild(elt('div', 'insp-empty', hadKeys
        ? '这一跑写在摘要面之前：产物键有，摘要没记'
        : '这个节点没有产出物'));
      return block;
    }
    const list = elt('div', 'insp-artifact');
    for (const k of keys) {
      const row = elt('div', 'insp-art-row');
      row.appendChild(elt('span', 'insp-art-key', k));
      row.appendChild(elt('span', 'insp-art-type', fmtBytes(arts[k].bytes)));
      const val = elt('span', 'insp-art-val mono', fmtShortSha(arts[k].sha256) || '未记录');
      val.title = arts[k].sha256 || '';
      row.appendChild(val);
      list.appendChild(row);
    }
    block.appendChild(list);
    block.appendChild(elt('div', 'insp-note',
      '来源节点：' + n.node + '（' + (n.status || '') + '）'));
    return block;
  }

  function renderDeliveryDetail (delivery) {
    const wrap = elt('div', 'deliv-detail');
    const bar = elt('div', 'deliv-bar');
    const back = elt('button', 'run-back', '返回账本列表');
    back.type = 'button';
    back.onclick = () => {
      deliveriesDetailOpen = false;
      renderDeliveries(state.deliveries);
    };
    bar.appendChild(back);
    bar.appendChild(elt('span', 'insp-muted', String(delivery.run_id || '')));
    wrap.appendChild(bar);

    const short = fmtShortSha(delivery.output_sha256);
    wrap.appendChild(inspBlock('交付物', [
      ['路径', delivery.output_path || '未落盘'],
      ['sha256', short || '未记录（没有落盘产物，或写在摘要面之前）', short ? 'mono' : 'insp-muted'],
      ['节点 / 产物', (delivery.node_count || 0) + ' / ' + (delivery.artifact_count || 0)],
      ['耗时', delivery.duration_sec == null ? '--' : (delivery.duration_sec + 's')],
    ], '摘要是这一跑收尾时算的：账本行按保留期回收后，磁盘上的交付物文件不受影响。'));
    if (short) {
      // 短摘要供肉眼比，整串留在这里供复制粘贴核对
      wrap.appendChild(elt('div', 'insp-note mono', delivery.output_sha256));
    }

    const topo = delivery.topology_hash || '';
    const confHashes = delivery.config_hashes || {};
    const names = Object.keys(confHashes).sort();
    if (!topo && !names.length) {
      wrap.appendChild(inspBlock('锁指纹', [['核对', '未记录', 'insp-muted']],
        '这条记录没有指纹：它写在 A5 指纹落地之前，或者那次解析没带锁信息。'));
    } else {
      const rows = [['拓扑指纹', topo || '--', 'mono']];
      for (const name of names) {
        rows.push([name, confHashes[name] || '--', 'mono']);
      }
      wrap.appendChild(inspBlock('锁指纹（当时算的，事后重锁不动它）', rows,
        '逐节点是 config_hash 前 12 位：拿它和当前锁文件比，就能回答"跑的是不是锁住的那件事"。'));
    }

    for (const n of (delivery.nodes || [])) {
      const block = artifactDigestBlock(n);
      if (block) wrap.appendChild(block);
    }
    els.deliveriesPanel.replaceChildren(wrap);
  }

  /* ---- Settings（评审 §4.2 Settings 面 v1：只读） ----
     数据源：/api/config（运行配置快照）、/api/webhooks（集成）、
     /api/versions/stats + /api/config/audit（版本与配置审计）；预算复用 /api/cost。
     只读不写：写操作（配置更新/调预算/版本回滚）要 X-Confirm + admin，留待后续批。 */
  function maskConfigValue (key, value) {
    // 防御性掩码：即便后端将来把密钥放进快照，这里也不把值原样画出来
    if (/key|secret|token|password|passwd|credential/i.test(String(key))) {
      return '***（已掩码）';
    }
    if (Array.isArray(value)) return value.join(', ');
    if (value == null) return '—';
    return String(value);
  }

  function flattenConfig (obj, prefix, out) {
    for (const k of Object.keys(obj || {}).sort()) {
      const v = obj[k];
      const path = prefix ? prefix + '.' + k : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        flattenConfig(v, path, out);
      } else {
        out.push([path, maskConfigValue(path, v)]);
      }
    }
    return out;
  }

  function settingsSection (title, rows, note) {
    const block = elt('div', 'insp-block');
    block.appendChild(elt('h4', null, title));
    for (const [label, value, cls] of rows) {
      block.appendChild(inspectorRow(label, value, cls));
    }
    if (note) block.appendChild(elt('div', 'insp-note', note));
    return block;
  }

  function renderSettings (data) {
    if (!data || !els.settingsPanel) return;
    const wrap = document.createDocumentFragment();

    /* 运行配置快照（敏感键已掩码） */
    const cfg = data.config || {};
    if (cfg.__error || cfg.__forbidden) {
      wrap.appendChild(settingsSection('运行配置',
        [['读取', cfg.__forbidden ? '需要 admin 权限' : '失败：' + cfg.__error, 'insp-muted']]));
    } else {
      wrap.appendChild(settingsSection('运行配置（GET /api/config · 敏感键已掩码）',
        flattenConfig(cfg, '', []),
        '进程当前的运行配置快照；修改走 POST /api/config（需 X-Confirm + admin），本面板只读。'));
    }

    /* 预算（复用 cost 载荷） */
    const cost = state.cost || {};
    if (cost.__error || cost.__forbidden) {
      wrap.appendChild(settingsSection('预算',
        [['读取', cost.__forbidden ? '需要 admin 权限' : '失败：' + cost.__error, 'insp-muted']]));
    } else {
      const budget = Number(cost.budget) > 0 ? '$' + Number(cost.budget).toFixed(2) : '不限';
      const remaining = cost.budget_remaining == null
        ? '不限' : '$' + Number(cost.budget_remaining).toFixed(4);
      wrap.appendChild(settingsSection('预算', [
        ['总预算', budget, 'mono'],
        ['剩余', remaining, 'mono'],
        ['已超支', cost.budget_exceeded ? '是' : '否',
          cost.budget_exceeded ? 'insp-warn' : 'insp-ok'],
      ], '预算熔断线来自 /api/cost；调预算走 POST /api/cost/budget（本面板只读）。'));
    }

    /* 集成 */
    const hooks = data.webhooks || {};
    const items = Array.isArray(hooks.webhooks) ? hooks.webhooks : [];
    if (hooks.__error || hooks.__forbidden) {
      wrap.appendChild(settingsSection('集成',
        [['webhook', hooks.__forbidden ? '需要 admin 权限' : '失败：' + hooks.__error, 'insp-muted']]));
    } else if (!items.length) {
      wrap.appendChild(settingsSection('集成', [['webhook', '未配置', 'insp-muted']],
        'MCP 与队列没有暴露读端点（如实标注，不在这里编造）。'));
    } else {
      const rows = items.map((w, i) => ['webhook ' + (i + 1),
        String(w.name || '') + ' → ' + String(w.pipeline || '—')
        + (w.enabled === false ? '（已停用）' : '')]);
      wrap.appendChild(settingsSection('集成', rows,
        'MCP 与队列没有暴露读端点（如实标注，不在这里编造）。'));
    }

    /* 版本与配置审计 */
    const vs = data.versionStats || {};
    const ca = data.configAudit || {};
    const vrows = [];
    if (vs.__error || vs.__forbidden) {
      vrows.push(['版本', vs.__forbidden ? '需要 admin 权限' : '失败：' + vs.__error, 'insp-muted']);
    } else {
      vrows.push(['受跟踪文件', String(vs.tracked_files == null ? '—' : vs.tracked_files), 'mono']);
      vrows.push(['版本总数', String(vs.total_versions == null ? '—' : vs.total_versions), 'mono']);
      vrows.push(['占用', fmtBytes(vs.total_size_bytes), 'mono']);
      vrows.push(['单文件上限',
        String(vs.max_versions_per_file == null ? '—' : vs.max_versions_per_file) + ' 版', 'mono']);
    }
    if (ca.__error || ca.__forbidden) {
      vrows.push(['配置审计', ca.__forbidden ? '需要 admin 权限' : '失败：' + ca.__error, 'insp-muted']);
    } else {
      vrows.push(['配置审计记录', String(ca.count == null ? '—' : ca.count), 'mono']);
    }
    wrap.appendChild(settingsSection('版本与配置审计', vrows,
      '来源：/api/versions/stats 与 /api/config/audit；只读展示，不触发回滚。'));

    els.settingsPanel.replaceChildren(wrap);
    if (els.badgeSettings) els.badgeSettings.textContent = '只读';
  }

  /* ---- Workflows & Schedules（评审 §4.2 六面之五，只读） ----
     工作流：/api/pipeline 的名单 + /api/locks 的漂移三值（matches: true=比过且一致 /
     false=漂移 / null=没法判断）——徽章三色，不把"没法判断"画成"一致"。
     排程：/api/schedules 的租约三态（held/stale/free，服务端算好）。 */
  function lockBadgeClass (matches) {
    if (matches === true) return 'done';
    if (matches === false) return 'failed';
    return 'pending';   // null = 没法判断（没有锁可比 / 解析失败）
  }

  function lockBadgeLabel (matches) {
    if (matches === true) return '一致';
    if (matches === false) return '漂移';
    return '未验证';
  }

  function fmtEpoch (v) {
    if (!v) return '—';
    return new Date(v * 1000).toLocaleString();
  }

  function leaseLabel (s) {
    const state = s.lease_state || 'free';   // free/stale/held：服务端按时点算好
    if (state === 'held') return '持有中（' + s.lease_owner + '）';
    if (state === 'stale') return '残留（' + s.lease_owner + '，租约已过点）';
    return '空闲';
  }

  function renderWorkflows (data) {
    if (!data || !els.workflowsPanel) return;
    const wrap = document.createDocumentFragment();

    /* 工作流（当前文件 vs 当前锁） */
    const locks = data.locks || {};
    const wfb = elt('div', 'insp-block');
    wfb.appendChild(elt('h4', null, '工作流（当前文件 vs 当前锁）'));
    if (locks.__error || locks.__forbidden) {
      wfb.appendChild(elt('div', 'insp-empty',
        locks.__forbidden ? '需要 admin 权限' : '读取失败：' + locks.__error));
    } else {
      const pipelines = Array.isArray(locks.pipelines) ? locks.pipelines : [];
      if (!pipelines.length) {
        wfb.appendChild(emptyState('i-wrench', '没有可显示的流水线'));
      } else {
        const table = elt('table', 'tasks-table');
        const thead = elt('thead');
        const hr = elt('tr');
        for (const h of ['Pipeline', '锁文件', '锁状态', '问题']) hr.appendChild(elt('th', null, h));
        thead.appendChild(hr);
        const tbody = elt('tbody');
        for (const e of pipelines) {
          const tr = elt('tr');
          tr.appendChild(elt('td', 'mono task-pipeline', e.pipeline || ''));
          tr.appendChild(elt('td', 'mono', e.lock_file || '—'));
          const cls = lockBadgeClass(e.matches);
          const badge = elt('span', 'badge-status ' + cls);
          badge.appendChild(statusDot(cls));
          badge.appendChild(document.createTextNode(' ' + lockBadgeLabel(e.matches)));
          const tdLock = elt('td');
          tdLock.appendChild(badge);
          tr.appendChild(tdLock);
          tr.appendChild(elt('td', null,
            e.matches === false && e.issues && e.issues.length
              ? e.issues.length + ' 项' : '—'));
          if (e.matches === false && e.issues && e.issues.length) {
            tr.title = e.issues.join('\n');
          }
          tbody.appendChild(tr);
        }
        table.appendChild(thead);
        table.appendChild(tbody);
        wfb.appendChild(table);
        const drifted = Array.isArray(locks.drifted) ? locks.drifted.length : 0;
        if (drifted) {
          wfb.appendChild(elt('div', 'insp-note',
            drifted + ' 条流水线的当前文件与锁不一致——先 loom plan --diff 看差异，再决定重锁。'));
        }
      }
    }
    wrap.appendChild(wfb);

    /* 排程（cron 触发租约） */
    const sch = data.schedules || {};
    const sb = elt('div', 'insp-block');
    sb.appendChild(elt('h4', null, '排程（cron 触发租约）'));
    if (sch.__error || sch.__forbidden) {
      sb.appendChild(elt('div', 'insp-empty',
        sch.__forbidden ? '需要 admin 权限' : '读取失败：' + sch.__error));
    } else {
      const rows = Array.isArray(sch.schedules) ? sch.schedules : [];
      if (!rows.length) {
        sb.appendChild(elt('div', 'insp-empty',
          '没有已登记的排程（triggers.yaml 未配，或服务未带 triggers 启动）'));
      } else {
        const table = elt('table', 'tasks-table');
        const thead = elt('thead');
        const hr = elt('tr');
        for (const h of ['名称', 'Pipeline', 'cron', '启停', '上次触发', '下次触发', '租约'])
          hr.appendChild(elt('th', null, h));
        thead.appendChild(hr);
        const tbody = elt('tbody');
        for (const s of rows) {
          const tr = elt('tr');
          tr.appendChild(elt('td', 'mono task-pipeline', s.name || ''));
          tr.appendChild(elt('td', null, s.pipeline || '—'));
          tr.appendChild(elt('td', 'mono', s.cron || '—'));
          const en = elt('td');
          const enBadge = elt('span', 'badge-status ' + (s.enabled ? 'done' : 'pending'));
          enBadge.textContent = s.enabled ? '启用' : '停用';
          en.appendChild(enBadge);
          tr.appendChild(en);
          tr.appendChild(elt('td', null, fmtEpoch(s.last_fire)));
          tr.appendChild(elt('td', null, fmtEpoch(s.next_fire)));
          tr.appendChild(elt('td', null, leaseLabel(s)));
          tbody.appendChild(tr);
        }
        table.appendChild(thead);
        table.appendChild(tbody);
        sb.appendChild(table);
      }
      sb.appendChild(elt('div', 'insp-note',
        '租约三态（held/stale/free）由服务端按时点算好；裁决与释放归 triggers 域，本面板只读。'));
    }
    wrap.appendChild(sb);

    els.workflowsPanel.replaceChildren(wrap);
    if (els.badgeWorkflows) {
      const wc = data.locks && data.locks.count != null ? data.locks.count : '--';
      const sc = data.schedules && data.schedules.count != null ? data.schedules.count : '--';
      els.badgeWorkflows.textContent = wc + ' / ' + sc;
    }
  }

  function renderError () {
    if (!els.errorOverlay) return;
    els.errorOverlay.style.display = 'flex';
    els.dashboard.style.display = 'none';
    if (els.errorMsg) {
      els.errorMsg.textContent =
        state.error || 'Unable to connect to the Loom API at ' + API_BASE;
    }
  }

  function hideError () {
    if (!els.errorOverlay) return;
    els.errorOverlay.style.display = 'none';
    els.dashboard.style.display = '';
  }

  // ----------------------------------------------------------
  // Retry
  // ----------------------------------------------------------
  function handleRetry () {
    if (refreshTimer) clearTimeout(refreshTimer);
    update().finally(scheduleRefresh);
  }

  // ----------------------------------------------------------
  // Scheduler
  // ----------------------------------------------------------
  function scheduleRefresh () {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      await update();
      scheduleRefresh();
    }, REFRESH_MS);
  }

  // ----------------------------------------------------------
  // Init
  // ----------------------------------------------------------
  function init () {
    cacheDom();
    initTheme();
    if (els.retryBtn) els.retryBtn.addEventListener('click', handleRetry);
    if (els.newTaskForm) els.newTaskForm.addEventListener('submit', submitTask);
    if (runBack) runBack.addEventListener('click', closeRun);
    const inspClose = document.querySelector('#inspector-close');
    if (inspClose) inspClose.addEventListener('click', closeInspector);
    loadPipelines();
    if (els.tokenSave) {
      const saveToken = () => {
        const v = (els.tokenInput.value || '').trim();
        if (!v) return;
        sessionStorage.setItem(TOKEN_KEY, v);
        els.tokenPrompt.style.display = 'none';
        loadPipelines();
        handleRetry();
      };
      els.tokenSave.addEventListener('click', saveToken);
      els.tokenInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') saveToken();
      });
    }
    // Runs 面状态过滤（评审 §4.2）：选择即重画列表，不等下一轮轮询。
    if (els.taskFilter) {
      state.taskFilter = els.taskFilter.value || 'all';
      els.taskFilter.addEventListener('change', () => {
        state.taskFilter = els.taskFilter.value || 'all';
        renderTasks(state.tasks);
      });
    }
    // 深链：URL 带 #/runs/<id> 首屏直达；前进/后退同步视图。
    window.addEventListener('hashchange', routeFromHash);
    els.lastUpdated.textContent = 'Loading…';
    update().then(scheduleRefresh).catch(() => scheduleRefresh());
    routeFromHash();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
