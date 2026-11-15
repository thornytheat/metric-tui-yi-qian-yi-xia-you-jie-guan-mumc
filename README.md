# metricring · 指标口径注册与依赖门禁

指标体系组的注册表门禁工具：把「同一个 GMV 在三个看板算法不同」这类口径事故挡在发布之前，
并在改口径前算清下游影响面。

## 能力

- **指标注册**：指标名 + 版本 + 表达式（`metric:`/`dim:` 引用、`sum/avg/count` 包裹）+ 单位/聚合/维度/负责人/是否发布，落 SQLite（命名卷 `metricring-data`），启动幂等 seed。
- **修订序号（并发安全）**：每个指标名一条单调递增修订序号 `r1, r2 …`。登记与发布都携带 `expected_revision`（基于哪份修订）；写路径在 `BEGIN IMMEDIATE` 事务里用比较-交换（CAS）推进修订头，绝不直接覆盖。
- **并发候选**：期望修订过期时不写入任何已确认内容，而是把「当前已确认候选」与「本次提交候选」连同逐项差异落成一条冲突记录；复核人选择采用哪份并填写最终依据。采用提交候选时若版本号已被占用，会自动分配新版本号。
- **发布中断恢复**：发布是两段式操作（先落 pending 意图 + prior_state，再应用并标记 committed）。中断后恢复上一份已确认修订；用同一 `op_id` 重试复用原目标修订，不会多出新修订。发布还会核对影响面：若其基于的修订或注册表快照（`registry_digest`）已变化（下游公式被他人改动），会以 `stale_impact` 阻断。
- **公式解析与归一化**：去空白、去冗余括号、去 `*1`、常量化、统一前缀；只做结构等价变换，`a/b` 与 `a/b*100` 归一化后仍然不同。
- **依赖图 + 环检测**：节点是「指标@版本」，边是表达式里的 `metric:` 引用；DFS 三色找环并给出环路径与涉及指标。
- **同名异口径**：同名指标按归一化表达式分组，>1 组即 conflict，列出各组版本与差异摘要。
- **变更门禁**：改表达式/维度/聚合 → 算全部传递下游，分 `breaking`（移除下游依赖的维度、改聚合、引入环、引用未发布版本）与 `requires_sync`。
- **发布校验**：被引用版本必须 `published=true`，且不得处于环中、不得与已发布基线口径不同。

## 核心规则（后端真算）

| 规则 | 模块 | 归一化/判定口径 |
|---|---|---|
| 公式解析 | `app/expr.py` | 递归下降，`+ - * / ( )`、`metric:name[@ver]`、`dim:name`、`sum/avg/count` |
| 归一化 | `app/expr.py` | 去空白、丢冗余括号（按优先级重新加）、消 `*1`/`1*`/`/1`/`+0`、数字常量化、前缀小写 |
| 依赖图 | `app/graph.py` | 节点 `name@version`；裸引用 `metric:x` 解析到「最新已发布版本」，`metric:x@2` 精确解析 |
| 环检测 | `app/graph.py` | DFS 白/灰/黑三色，灰色邻居即环，按节点集合去重 |
| 口径冲突 | `app/conflict.py` | 同名 → 归一化分组 → 组数 > 1 即 conflict |
| 变更门禁 | `app/gate.py` | 传递下游 + `breaking`/`requires_sync` 判定 |
| 发布门禁 | `app/gate.py` | `unpublished_reference` / `missing_reference` / `cycle_member` / `caliber_conflict` |
| 修订并发 | `app/revisions.py` | 修订头 CAS；过期 → `revision_conflict` + 两份候选 + 逐项差异 |
| 发布恢复 | `app/revisions.py` | pending 意图 + `prior_state`；恢复回上一份已确认修订，同 op_id 重试复用目标修订 |

样例数据 `sample_data/metrics.json`：24 个指标版本 / 20 个指标名，故意包含
**3 组同名异口径**（gmv 三口径、uv 两口径、conversion_rate 原值与百分比）、
**2 条依赖环**（cpa↔cpc、unit_price↔gmv_unit）、
**4 个跨版本依赖**（gmv@2→gmv@1、uv@2→uv@1、pay_gmv_share→gmv@3、uv_per_gmv→uv@2）、
**2 个未发布依赖**（gmv@3、uv@2）。

## 启动

```bash
cd batch-20261114b/projects/gb-626
cp .env.example .env          # 可选，默认 18215
docker compose up -d --build
curl -s http://127.0.0.1:18215/health
docker compose down -v        # 重建数据库：删掉 metricring-data 卷即可
```

## 演示步骤

1. 打开 `http://127.0.0.1:18215/` → 点「执行注册表体检」→ 结果页：3 组同名异口径、2 条依赖环、2 个发布阻断、逐条依据（原始表达式 vs 归一化表达式）。
2. `/workbench` 选 `gmv_daily@1`，把维度从 `dt,channel` 改成 `dt` → 「计算变更影响面」：6 个下游中有 2 个 breaking（`gmv_7d`、`pay_gmv_share` 按 channel 分组），其余 4 个 requires_sync。
3. `/review` 登记修正后的 `gmv_daily@2` 并发布 → 门禁放行；再试发布 `gmv@3`（支付口径）→ 被同名异口径拦截。
4. 并发演示：两个负责人同时基于 `gmv_daily` 当前修订（`expected_revision` 相同）各登记一版 →
   后到的一份返回 409 `revision_conflict`，`/conflicts` 页显示两份候选与逐项差异；选择采用并填写依据，
   若提交候选想用的版本号已被占用会自动分配新版本。
5. 发布恢复演示：`expected_revision` 过期的发布被拒；`simulate_interrupt=true` 的发布中断后，
   `/conflicts` 页点「恢复上一份已确认修订」，再用同一 `op_id` 重试会复用原目标修订（修订台账里不会多出一条）。

## 页面

| 路由 | 内容 |
|---|---|
| `/` | 使用者/场景/输入、注册表概览、冲突与环预览、演示步骤、体检按钮 |
| `/workbench` | 选指标、编辑表达式/维度/聚合、解析归一化、影响面、上游依赖树、注册表全量 |
| `/result/{check_id}` | 冲突 3 组、环 2 条、发布阻断、跨版本依赖、全部指标归一化前后对比 |
| `/review` | 最近体检、变更影响面（breaking / requires_sync）、发布按钮（带修订）、版本历史与历史体检 |
| `/conflicts` | 待处理并发冲突（两份候选 + 逐项差异 + 采用依据）、待恢复发布、恢复记录与已处理冲突 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/registry` | 注册表 + 统计（冲突/环/阻断计数） |
| GET | `/api/metrics` | 指标列表 |
| POST | `/api/parse` | `{expression}` → 归一化、引用清单、不可解析引用；语法错误 400 |
| POST | `/api/check` | 执行体检并落库，返回完整结论 |
| GET | `/api/check/{id}` | 读取体检结果；不存在 404 |
| POST | `/api/impact` | `{name,version?,expression,aggregation?,dimensions?}` → 影响面；指标不存在 404、表达式非法 400 |
| POST | `/api/registry` | 登记新版本；带 `expected_revision`/`op_id`；修订过期 409 `revision_conflict`（含两份候选与 diff）、重复版本 409、字段非法或缺引用 400 |
| POST | `/api/publish` | 发布校验；带 `expected_revision`/`op_id`/`impact_id`；被拦截或修订过期 409、中断 503 |
| POST | `/api/publish/{op_id}/recover` | 恢复中断的发布到上一份已确认修订 |
| GET | `/api/publish/{op_id}` | 发布操作台账（status、基线/目标修订、prior_state） |
| GET | `/api/revision/{name}` | 某指标名的修订台账与当前修订头 |
| GET | `/api/revisions` | 修订台账列表（可按 name 过滤） |
| GET | `/api/conflicts` | 冲突列表（默认 `status=open`） |
| GET | `/api/conflicts/{id}` | 冲突详情：两份候选、逐项差异、处理记录 |
| POST | `/api/conflicts/{id}/resolve` | `{choice, reason, expected_revision}` → 采用候选并记录依据 |

```bash
# 注册表与体检
curl -s http://127.0.0.1:18215/api/registry | python3 -m json.tool | head -20
curl -s -X POST http://127.0.0.1:18215/api/check | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['stats'])"
```

## 目录

```
app/
  main.py        # FastAPI 路由（薄）
  expr.py        # 公式解析 + 归一化（纯函数）
  graph.py       # 依赖图 + DFS 三色环检测
  conflict.py    # 同名异口径
  gate.py        # 变更门禁 + 发布门禁
  revisions.py   # 修订台账 + 并发候选 + 发布恢复
  registry.py    # 指标注册校验/索引/版本解析
  report.py      # 体检编排
  store.py       # SQLite（含 metric_heads / revisions / conflicts / publish_ops）
  seed.py        # 幂等 seed
  settings.py    # 路径常量
  templates/     # base/index/workbench/result/review/conflicts
  static/        # app.js（原生）、style.css
sample_data/metrics.json
tests/           # test_expr / test_graph / test_conflict / test_gate / test_api / test_revisions
```

## 测试

```bash
docker compose run --rm app python -m pytest -q
```

## 已知限制

- 归一化只做结构等价（去空白/括号/`*1`/常量化），不做代数化简：`a*2/2`、`a+a` 不会被合并成 `a` 或 `2*a`。
- 依赖解析是静态的：裸引用固定解析到「最新已发布版本」，不回放历史时点（想固定就写 `@版本`）。
- 环检测覆盖指标引用环；`dim:` 维度引用不参与图，维度冲突靠「变更门禁的维度移除判定」兜底。
- 表达式不含窗口/时间过滤语法（`dt=` 之类），明细口径差异由 `dim:` 与描述字段表达。
- 单进程 SQLite（WAL）。修订头用 `BEGIN IMMEDIATE` + CAS 保证同进程/多连接下不覆盖，但未做跨主机的分布式锁；
  `simulate_interrupt` 只注入「应用阶段中断」，不会真的杀进程。
- 恢复语义是「回到上一份已确认修订」：中断期间写入的 pending 修订会标记 `rolled_back`，不会成为已确认内容。
