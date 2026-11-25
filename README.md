# 学术期刊编辑任务栏实验

面向本地演练的 Python API，使用 Lin 交互协变量调整、HC3 区间与分流诊断。业务场景、字段、算法边界和演练剧本见 [BUSINESS.md](BUSINESS.md)。

## 启动

```sh
cp -n .env.example .env
docker compose up --build -d --wait
curl --fail http://127.0.0.1:19333/health
```

API 使用本机端口 `19333`，MySQL 8.0 使用本机端口 `17833`。`/openapi.json` 提供接口描述。所有演示密码为 `Local-demo-2026!`，设置 `SEED_DEMO=false` 后可关闭演示初始化。演示配置用于本地环境。

```sh
curl --fail -X POST http://127.0.0.1:19333/api/auth/login \
  -H 'Content-Type: application/json' \
  --data '{"email":"analyst@demo.local","password":"Local-demo-2026!"}'
```

后续请求携带 `Authorization: Bearer <access_token>`。

| 角色 | 可执行动作 |
|---|---|
| `viewer@demo.local` | 查看版本与分析、读取批准报告及本人收件箱 |
| `analyst@demo.local` | 查看数据与报告、导入快照、发起运算 |
| `reviewer@demo.local` | 查看数据与报告、给出复核、安排投递、查看审计 |

## 业务 API

| 操作 | 路径与输入 |
|---|---|
| 获取领域字段和配置 | `GET /api/method` 返回数据行和选项的 JSON Schema |
| 导入及核对 | `POST /api/datasets` 接受 `{"name":"…","rows":[…]}`，或用 `Content-Type: text/csv` 和 `?name=…` 传原始 CSV。`GET /api/datasets` 列版本，`GET /api/datasets/{id}/rows` 分页读原始行 |
| 修订/补报/撤回 | `POST /api/datasets/{id}/revisions`（兼容 `/records`、`/supplements`、`/withdrawals`）或 `POST /api/streams/{name}/revisions`。记录可带 `revision`、`received_at`、`withdrawn`；也可用 `Idempotency-Key` 或 `batch_id` 重试 |
| 回放 | `GET /api/streams/{name}/replay?as_of_date=2026-11-25&received_cutoff=2026-11-25T10:00:00Z` 预演；`POST /api/replays` 或 `POST /api/streams/{name}/runs` 入队冻结快照。原有 `POST /api/datasets/{id}/runs` 的 `options` 也支持 `received_cutoff`/`received_before` |
| 运算 | `POST /api/datasets/{id}/runs` 提交 `{"options":{}}` 使用默认值，`GET /api/runs/{run_id}` 轮询结果；`POST /api/runs/{run_id}/rerun` 按原冻结快照重跑 |
| 复核 | `POST /api/runs/{run_id}/review` 提交 `{"decision":"approved","note":"已核对来源与口径"}`，决策也可为 `rejected` |
| 报告和分发 | `GET /api/runs/{run_id}/report` 获取带版本校验值的报告，`GET /api/runs/{run_id}/report/csv?table=…` 导出业务表。审批后 `POST /api/runs/{run_id}/deliveries` 提交 `recipient` 与带偏移的 `due_at`，由 worker 到点写入 `GET /api/inbox`；`GET /api/audit` 查询审计 |

修订账本只追加、不覆盖：同一 `(stream, editor_id, revision)` 的同内容重试幂等返回；同号异内容返回 `409 revision_content_conflict` 且整批不写入。回放先按收件截止过滤，再为每名编辑选择最高修订号；撤回修订把四项实验期结果置空，但保留 `arm`、分流日和基线，因此仍计入分流与成熟人数、计为 `missing_outcome`。分析日只决定窗口成熟度，收件截止只决定可见修订。

入队时会把当时可见的修订物化成不可变 `Dataset`/`Observation`，并在 `run.replay.adopted_revisions` 与 `run.result.replay` 中保存采用的修订指纹。之后入库的补报（包括逻辑收件时间早于截止的补报）只会影响新的回放/分析；旧运行及其 `/rerun`、`/retry` 始终读取同一冻结快照。

数据集按同名及规范化内容校验值幂等导入，新增内容形成新版本。任务和定时投递存于数据库，worker 用 Redis 唤醒并定期轮询。收件箱属于本地账号，所有授权登录账号共享分析台账，审批后报告可读取。时刻保存为 UTC，API 以 ISO 时间返回。初始表结构随 API 启动创建，数据保留在命名卷；升级已有结构需要独立迁移。

```sh
docker compose exec -T backend python -m scripts.seed_demo
docker compose down
```

验收脚本与实测证据见 [本项目验收记录](../../test-artifacts/gb-0084/TESTING.md)。
