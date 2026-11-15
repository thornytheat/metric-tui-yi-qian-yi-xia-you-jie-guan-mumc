// 原生 JS：只做 fetch + DOM 渲染，无框架、无打包器、无 CDN。所有结论由后端算。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function tag(text, kind) { return '<span class="tag ' + (kind || '') + '">' + esc(text) + '</span>'; }

  async function postJSON(url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var detail = data && data.detail;
      var msg = typeof detail === 'string' ? detail : (detail && (detail.error || JSON.stringify(detail)));
      var err = new Error(msg || ('请求失败：HTTP ' + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }
  window.postJSON = postJSON;

  function showError(host, err) {
    if (!host) return;
    host.hidden = false;
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err));
  }
  function clearError(host) { if (host) { host.hidden = true; host.textContent = ''; } }

  // ---------------------------------------------------------------- 通用：分段切换
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented, .tabs');
      if (group) {
        qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      }
      btn.classList.add('active');
      var host = btn.closest('.panel') || document;
      qsa('.tab-panel', host).forEach(function (panel) {
        panel.hidden = panel.id !== btn.dataset.tabTarget;
      });
    });
  });

  // ---------------------------------------------------------------- 通用：表格行筛选
  function bindRowFilter(options) {
    var table = qs(options.table);
    if (!table) return null;
    var stat = qs(options.stat);
    var search = options.search ? qs(options.search) : null;
    var activeFilter = 'all';
    var versionSelect = options.version ? qs(options.version) : null;
    var publishedSelect = options.published ? qs(options.published) : null;

    function dataRows() { return qsa('tbody tr', table); }
    function apply() {
      var term = search ? search.value.trim().toLowerCase() : '';
      var version = versionSelect ? versionSelect.value : '';
      var published = publishedSelect ? publishedSelect.value : '';
      var shown = 0;
      var total = dataRows().length;
      dataRows().forEach(function (tr) {
        var ok = true;
        if (term && (tr.getAttribute('data-search') || '').toLowerCase().indexOf(term) === -1) ok = false;
        if (version && tr.getAttribute('data-version') !== version) ok = false;
        if (published && tr.getAttribute('data-published') !== published) ok = false;
        if (activeFilter === 'published' && tr.getAttribute('data-published') !== '1') ok = false;
        if (activeFilter === 'unpublished' && tr.getAttribute('data-published') !== '0') ok = false;
        tr.hidden = !ok;
        if (ok) shown += 1;
      });
      if (stat) stat.textContent = '显示 ' + shown + ' / ' + total + (options.unit || ' 条');
      return shown;
    }
    if (search) search.addEventListener('input', apply);
    if (versionSelect) versionSelect.addEventListener('change', apply);
    if (publishedSelect) publishedSelect.addEventListener('change', apply);
    if (options.filter) {
      var filterHost = qs(options.filter);
      if (filterHost) {
        qsa('button[data-row-filter]', filterHost).forEach(function (btn) {
          btn.addEventListener('click', function () {
            qsa('button', filterHost).forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            activeFilter = btn.getAttribute('data-row-filter') || 'all';
            apply();
          });
        });
      }
    }
    apply();
    return apply;
  }

  // ---------------------------------------------------------------- 工作台
  var metricData = qs('#metric-data');
  if (metricData) { initWorkbench(JSON.parse(metricData.textContent || '{}')); }

  function initWorkbench(versions) {
    var select = qs('#metric-select');
    var exprInput = qs('#expression');
    var aggInput = qs('#aggregation');
    var dimsInput = qs('#dimensions');
    var parseHost = qs('#parse-out');
    var parseError = qs('#parse-error');
    var impactHost = qs('#impact-out');
    var impactError = qs('#impact-error');
    var saveHost = qs('#save-out');
    var focusKey = (function () {
      var el = qs('#focus-key');
      try { return el ? JSON.parse(el.textContent) : ''; } catch (e) { return ''; }
    })();

    var entries = [];
    Object.keys(versions).forEach(function (name) {
      versions[name].forEach(function (item) { entries.push(item); });
    });
    entries.sort(function (a, b) { return a.key < b.key ? -1 : 1; });
    entries.forEach(function (item) {
      var opt = document.createElement('option');
      opt.value = item.key;
      opt.textContent = item.key + (item.published ? '' : '（未发布）');
      select.appendChild(opt);
    });

    function currentEntry() {
      return entries.filter(function (row) { return row.key === select.value; })[0] || {};
    }

    function renderParse(result) {
      var rows = (result.references || []).map(function (ref) {
        return '<li class="small"><code>' + esc(ref.kind) + ':' + esc(ref.name) +
          (ref.version ? '@' + esc(ref.version) : '') + '</code></li>';
      }).join('');
      var unresolved = (result.unresolved || []).map(function (item) {
        return '<li class="small">' + tag('不可解析', 'err') + ' ' + esc(item.target) + ' · ' + esc(item.reason) + '</li>';
      }).join('');
      var unresolvedBlock = (result.unresolved || []).length
        ? '<div class="alert alert-error" style="margin-top:8px">表达式里有不可解析引用：' +
          (result.unresolved || []).map(function (item) { return esc(item.target) + '（' + esc(item.reason) + '）'; }).join('、') + '</div>'
        : '';
      parseHost.innerHTML =
        unresolvedBlock +
        '<table class="dense"><tbody>' +
        '<tr><th style="width:96px">原始</th><td class="mono small">' + esc(result.raw) + '</td></tr>' +
        '<tr><th>归一化</th><td class="mono small"><strong>' + esc(result.normalized) + '</strong> ' +
          (result.changed ? tag('已归一化', 'warn') : tag('无变化', 'ok')) + '</td></tr>' +
        '<tr><th>函数</th><td class="small">' + (result.functions.length ? esc(result.functions.join(', ')) : '（无包裹函数）') + '</td></tr>' +
        '<tr><th>指标引用</th><td class="small">' + (result.metrics.length ? esc(result.metrics.join(', ')) : '（无）') + '</td></tr>' +
        '<tr><th>维度引用</th><td class="small">' + (result.dims.length ? esc(result.dims.join(', ')) : '（无）') + '</td></tr>' +
        '</tbody></table>' +
        (rows ? '<div class="small" style="margin-top:6px">引用清单：</div><ul class="list small" style="margin:0">' + rows + '</ul>' : '') +
        (unresolved ? '<div class="small" style="margin-top:6px">解析告警：</div><ul class="list small" style="margin:0">' + unresolved + '</ul>' : '');
    }

    var parseTimer = null;
    async function runParse() {
      clearError(parseError);
      try {
        var result = await postJSON('/api/parse', { expression: exprInput.value });
        renderParse(result);
        return result;
      } catch (err) {
        parseHost.innerHTML = '<p class="small muted" style="margin:0">表达式解析失败，见上方错误提示。</p>';
        showError(parseError, err);
        return null;
      }
    }

    function scheduleParse() {
      if (parseTimer) clearTimeout(parseTimer);
      parseTimer = setTimeout(runParse, 380);
    }

    function loadEntry(key) {
      var item = entries.filter(function (row) { return row.key === key; })[0];
      if (!item) return;
      exprInput.value = item.expression;
      aggInput.value = item.aggregation;
      dimsInput.value = (item.dimensions || []).join(',');
      parseHost.innerHTML = '<p class="small muted" style="margin:0">已载入 <code>' + esc(item.key) +
        '</code>（' + (item.published ? '已发布' : '未发布') + '），正在解析归一化…</p>';
      impactHost.innerHTML = '<p class="small muted" style="margin:0">点左侧「计算影响面」看传递下游与 breaking 清单。</p>';
      clearError(parseError); clearError(impactError); clearError(saveHost);
      saveHost.innerHTML = '';
      runParse();
    }

    select.addEventListener('change', function () { loadEntry(select.value); });
    exprInput.addEventListener('input', scheduleParse);
    if (entries.length) {
      var initial = focusKey || entries[0].key;
      select.value = initial;
      loadEntry(initial);
    }

    qs('#btn-parse').addEventListener('click', function () { runParse(); });

    qs('#btn-impact').addEventListener('click', function () { runImpact(); });

    async function runImpact() {
      clearError(impactError);
      var name = select.value.split('@')[0];
      var version = parseInt(select.value.split('@')[1], 10);
      var dims = dimsInput.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      impactHost.innerHTML = '<p class="small muted" style="margin:0">计算中…</p>';
      try {
        var imp = await postJSON('/api/impact', {
          name: name, version: version, expression: exprInput.value,
          aggregation: aggInput.value, dimensions: dims
        });
        var breaking = imp.breaking.map(function (item) {
          return '<tr><td><strong>' + esc(item.name) + '</strong>@' + esc(item.version) +
            '<div class="small muted">' + esc(item.owner) + ' · L' + esc(item.depth) + '</div></td>' +
            '<td class="small">' + esc((item.dimensions || []).join(', ')) + '</td>' +
            '<td class="mono small cell-ellipsis">' + esc(item.normalized) + '</td>' +
            '<td>' + tag(item.code, 'err') + '</td><td class="small">' + esc(item.reason) + '</td></tr>';
        }).join('');
        var sync = imp.requires_sync.map(function (item) {
          return '<tr><td><strong>' + esc(item.name) + '</strong>@' + esc(item.version) + '</td>' +
            '<td class="small">' + esc((item.dimensions || []).join(', ')) + '</td>' +
            '<td class="mono small cell-ellipsis">' + esc(item.normalized) + '</td>' +
            '<td class="small">' + esc(item.reason) + '</td></tr>';
        }).join('');
        impactHost.innerHTML =
          '<div class="kpi-strip" style="margin-bottom:10px">' +
          '<div class="kpi"><div class="k-label">传递下游</div><div class="k-value">' + esc(imp.downstream_count) + '</div><div class="k-note">含传递，按最短跳数</div></div>' +
          '<div class="kpi ' + (imp.breaking_count ? 'err' : 'ok') + '"><div class="k-label">breaking</div><div class="k-value">' + esc(imp.breaking_count) + '</div><div class="k-note">阻断发布</div></div>' +
          '<div class="kpi ' + (imp.requires_sync_count ? 'warn' : 'ok') + '"><div class="k-label">requires_sync</div><div class="k-value">' + esc(imp.requires_sync_count) + '</div><div class="k-note">需同步升版</div></div>' +
          '</div>' +
          '<table class="dense"><tbody>' +
          '<tr><th style="width:96px">口径前</th><td class="mono small">' + esc(imp.target.normalized) + '</td></tr>' +
          '<tr><th>口径后</th><td class="mono small"><strong>' + esc(imp.proposed.normalized) + '</strong></td></tr>' +
          '<tr><th>维度</th><td class="small">' + esc((imp.target.dimensions || []).join(',')) + ' → ' +
            esc((imp.proposed.dimensions || []).join(',')) +
            (imp.proposed.removed_dims.length ? ' ' + tag('移除 ' + imp.proposed.removed_dims.join(','), 'err') : '') + '</td></tr>' +
          '</tbody></table>' +
          '<div class="small" style="margin:8px 0 4px"><strong>breaking（' + esc(imp.breaking_count) + '）</strong></div>' +
          (breaking ? '<div class="table-wrap"><table class="dense"><thead><tr><th>下游</th><th>维度</th><th>归一化</th><th>规则</th><th>依据</th></tr></thead><tbody>' + breaking + '</tbody></table></div>'
                    : '<p class="small muted" style="margin:0">无 breaking。</p>') +
          '<div class="small" style="margin:8px 0 4px"><strong>requires_sync（' + esc(imp.requires_sync_count) + '）</strong></div>' +
          (sync ? '<div class="table-wrap"><table class="dense"><thead><tr><th>下游</th><th>维度</th><th>归一化</th><th>下一步</th></tr></thead><tbody>' + sync + '</tbody></table></div>'
                : '<p class="small muted" style="margin:0">无需要同步的下游。</p>') +
          (imp.notes && imp.notes.length ? '<ul class="list small" style="margin:8px 0 0">' + imp.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>' : '');
        saveHost.innerHTML = '<div class="alert alert-info">已登记影响面记录 <code>' + esc(imp.impact_id) +
          '</code>，可在「复核与发布」页查看完整依据。</div>';
      } catch (err) {
        impactHost.innerHTML = '<p class="small muted" style="margin:0">影响面计算失败，见上方错误提示。</p>';
        showError(impactError, err);
      }
    }

    qs('#btn-register').addEventListener('click', async function () {
      var name = select.value.split('@')[0];
      var item = currentEntry();
      var dims = dimsInput.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      var opId = 'op_' + Math.random().toString(16).slice(2, 10);
      saveHost.innerHTML = '<p class="small muted" style="margin:0">登记中…（基于 r' + esc(item.head_revision) + '）</p>';
      try {
        var created = await postJSON('/api/registry', {
          name: name,
          expression: exprInput.value,
          aggregation: aggInput.value,
          dimensions: dims,
          unit: item.unit || '',
          owner: item.owner || '指标体系组',
          description: '工作台登记',
          published: false,
          expected_revision: item.head_revision,
          op_id: opId
        });
        saveHost.innerHTML = '<div class="alert alert-info">已登记 <strong>' + esc(created.key) + '</strong> 为 r' +
          esc(created.revision) + '：归一化 ' + esc(created.normalized) + '；同名口径分组 ' +
          esc(created.caliber_group_count) + ' 组' +
          (created.caliber_conflict ? '（仍存在同名异口径，发布会被拦截）' : '') + '</div>';
      } catch (err) {
        var detail = err.data || {};
        if (detail.status === 'revision_conflict') {
          saveHost.innerHTML = '<div class="alert alert-error"><strong>修订冲突 · 已保留两份候选</strong>' +
            '<p class="small" style="margin:4px 0">' + esc(detail.message) + '</p>' +
            renderConflictDiff(detail.diff) +
            '<p class="small" style="margin:4px 0 0"><a href="/conflicts">去冲突处理页选择采用并填写依据 →</a></p></div>';
          return;
        }
        saveHost.innerHTML = '';
        showError(saveHost, err);
      }
    });

    function renderConflictDiff(diff) {
      var rows = (diff || []).map(function (row) {
        return '<tr><td class="small">' + esc(row.field) + '</td><td class="mono small">' +
          esc(row.before === null ? '—' : row.before) + '</td><td class="mono small">' +
          esc(row.after === null ? '—' : row.after) + '</td><td>' +
          tag(row.changed ? '已变更' : '一致', row.changed ? 'err' : 'ok') + '</td></tr>';
      }).join('');
      return '<div class="table-wrap"><table class="dense"><thead><tr><th>字段</th><th>已确认</th><th>本次提交</th><th>差异</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>';
    }

    function renderPublishConflict(host, detail) {
      var blockers = (detail.blockers || []).map(function (item) {
        return '<li>' + tag(item.code, 'err') + ' ' + esc(item.detail) + '</li>';
      }).join('');
      var conflictBlock = detail.status === 'revision_conflict'
        ? '<p class="small" style="margin:4px 0">' + esc(detail.message) + '</p>' +
          renderConflictDiff(detail.diff) +
          '<p class="small" style="margin:4px 0 0"><a href="/conflicts">去冲突处理页选择采用并填写依据 →</a></p>'
        : '';
      host.innerHTML = '<div class="alert alert-error"><strong>' + esc(detail.key || '发布被阻断') +
        ' · ' + esc(detail.status === 'revision_conflict' ? '修订冲突' : '发布被阻断') +
        '</strong>（HTTP ' + esc(detail.status === 'revision_conflict' ? 409 : 409) + '）' +
        (blockers ? '<ul class="list small" style="margin:4px 0 0">' + blockers + '</ul>' : '') +
        conflictBlock + '</div>';
    }

    var publishButton = qs('#btn-publish');
    if (publishButton) {
      publishButton.addEventListener('click', async function () {
        var parts = select.value.split('@');
        var name = parts[0];
        var version = parseInt(parts[1], 10);
        var item = currentEntry();
        saveHost.innerHTML = '<p class="small muted" style="margin:0">提交发布校验（基于 r' + esc(item.head_revision) + '）…</p>';
        try {
          var result = await postJSON('/api/publish', {
            name: name,
            version: version,
            expected_revision: item.head_revision,
            op_id: 'pub_' + Math.random().toString(16).slice(2, 10)
          });
          saveHost.innerHTML = '<div class="alert alert-info">发布通过：<strong>' + esc(result.key) +
            '</strong>（口径 ' + esc(result.normalized) + '，r' + esc(result.revision) + '）</div>';
        } catch (err) {
          renderPublishConflict(saveHost, err.data || { key: name + '@' + version });
        }
      });
    }

    bindRowFilter({
      table: '#registry-table',
      search: '#registry-search',
      filter: '#registry-filter',
      stat: '#registry-stat',
      unit: ' 个指标版本'
    });
  }

  // ---------------------------------------------------------------- 复核页：筛选 + 统计
  bindRowFilter({
    table: '#review-table',
    search: '#review-search',
    version: '#review-version',
    published: '#review-published',
    stat: '#review-stat',
    unit: ' 个指标版本'
  });

  // ---------------------------------------------------------------- 复核页：快速影响面
  var quickButton = qs('#btn-quick-impact');
  if (quickButton) {
    quickButton.addEventListener('click', async function () {
      var host = qs('#quick-result');
      var errorHost = qs('#quick-error');
      clearError(errorHost);
      var name = qs('#impact-name').value;
      var dims = qs('#impact-dimensions').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      host.innerHTML = '<p class="small muted" style="margin:0">计算中…</p>';
      try {
        var imp = await postJSON('/api/impact', {
          name: name,
          expression: qs('#impact-expression').value,
          dimensions: dims.length ? dims : null
        });
        host.innerHTML = '<p class="small" style="margin:0">下游 <strong>' + esc(imp.downstream_count) + '</strong> 个，breaking <strong>' +
          esc(imp.breaking_count) + '</strong> 个，requires_sync ' + esc(imp.requires_sync_count) + ' 个。</p>' +
          '<ul class="list small" style="margin:4px 0 0">' + imp.breaking.map(function (item) {
            return '<li>' + tag(item.code, 'err') + ' ' + esc(item.name) + '@' + esc(item.version) + ' · ' + esc(item.reason) + '</li>';
          }).join('') + '</ul><p class="small muted" style="margin:4px 0 0">刷新页面可看到完整影响面表格。</p>';
      } catch (err) {
        host.innerHTML = '';
        showError(errorHost, err);
      }
    });
  }

  // ---------------------------------------------------------------- 复核页：发布
  qsa('.btn-publish').forEach(function (button) {
    button.addEventListener('click', async function () {
      var host = qs('#publish-out');
      var errorHost = qs('#publish-error');
      clearError(errorHost);
      host.innerHTML = '<p class="small muted" style="margin:0">提交发布校验…</p>';
      try {
        var result = await postJSON('/api/publish', {
          name: button.getAttribute('data-name'),
          version: parseInt(button.getAttribute('data-version'), 10),
          expected_revision: button.getAttribute('data-expected') === null
            ? null
            : parseInt(button.getAttribute('data-expected'), 10),
          op_id: 'pub_' + Math.random().toString(16).slice(2, 10)
        });
        host.innerHTML = '<div class="alert alert-info">发布通过：<strong>' + esc(result.key) +
          '</strong>（口径 ' + esc(result.normalized) + '，r' + esc(result.revision) + '）</div>';
        button.disabled = true;
        button.textContent = '已发布';
      } catch (err) {
        host.innerHTML = '';
        var detail = err.data || {};
        var blockers = (detail.blockers || []).map(function (item) {
          return '<li>' + tag(item.code, 'err') + ' ' + esc(item.detail) + '</li>';
        }).join('');
        var unresolved = (detail.unresolved || []).map(function (item) {
          return '<li>' + tag(item.reason || 'unresolved', 'err') + ' ' + esc(item.target) + '</li>';
        }).join('');
        host.innerHTML = '<div class="alert alert-error"><strong>' + esc(detail.key || '发布被阻断') +
          ' · ' + esc(detail.status === 'revision_conflict' ? '修订冲突' : '发布被阻断') + '</strong>' +
          (blockers ? '<ul class="list small" style="margin:4px 0 0">' + blockers + '</ul>' : '') +
          (unresolved ? '<ul class="list small" style="margin:4px 0 0">' + unresolved + '</ul>' : '') +
          (detail.status === 'revision_conflict'
            ? '<p class="small" style="margin:4px 0 0"><a href="/conflicts">查看两份候选与逐项差异 →</a></p>'
            : '<div>' + esc(err.message || '') + '</div>') +
          '</div>';
      }
    });
  });

  // ---------------------------------------------------------------- 冲突页：恢复 / 解决
  qsa('.btn-recover').forEach(function (button) {
    button.addEventListener('click', async function () {
      var host = qs('#recover-out');
      host.innerHTML = '<p class="small muted" style="margin:0">恢复中…</p>';
      try {
        var result = await postJSON('/api/publish/' + encodeURIComponent(button.getAttribute('data-op')) + '/recover', {});
        host.innerHTML = '<div class="alert alert-info">已恢复 <strong>' + esc(result.name) + '@' + esc(result.version) +
          '</strong> 到 r' + esc(result.restored_revision) + '；用同一 op_id 重试会复用原目标修订，不会多出新修订。</div>';
        button.disabled = true;
        button.textContent = '已恢复';
      } catch (err) {
        host.innerHTML = '<div class="alert alert-error">' + esc(err.message) + '</div>';
      }
    });
  });

  qsa('.btn-resolve').forEach(function (button) {
    button.addEventListener('click', async function () {
      var card = button.closest('.panel-body');
      var choice = qs('.resolve-choice', card).value;
      var reason = qs('.resolve-reason', card).value.trim();
      var errorHost = qs('.resolve-error', card);
      var out = qs('.resolve-out', card);
      clearError(errorHost);
      out.innerHTML = '';
      if (!reason) {
        errorHost.hidden = false;
        errorHost.textContent = '请填写最终采用依据。';
        return;
      }
      out.innerHTML = '<p class="small muted" style="margin:0">提交中…</p>';
      try {
        var result = await postJSON('/api/conflicts/' + encodeURIComponent(button.getAttribute('data-conflict')) + '/resolve', {
          choice: choice,
          reason: reason,
          expected_revision: parseInt(button.getAttribute('data-expected'), 10)
        });
        out.innerHTML = '<div class="alert alert-info">已处理：采用 <strong>' +
          esc(result.resolution.adopted === 'submitted' ? '本次提交' : '已确认候选') + '</strong>' +
          (result.assigned_version ? '（版本 v' + esc(result.assigned_version) + '）' : '') +
          '，当前 r' + esc(result.revision) + '。</div>';
        button.disabled = true;
      } catch (err) {
        var detail = err.data || {};
        if (detail.status === 'revision_conflict') {
          out.innerHTML = '<div class="alert alert-error">处理期间修订又变化了：' + esc(detail.message) +
            ' <a href="/conflicts">刷新后重新查看差异 →</a></div>';
        } else {
          errorHost.hidden = false;
          errorHost.textContent = '处理失败：' + (err.message || '');
        }
      }
    });
  });
})();
