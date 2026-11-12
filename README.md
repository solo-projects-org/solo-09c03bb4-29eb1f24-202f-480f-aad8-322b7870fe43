# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务，并提供主题导航的在架书目计数。核心保证：**翻页失败绝不让半批记录入库；任何主题计数都与已提交的目录和词表严格一致**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 馆员提交 JSON 对照表，原词精确匹配，未收录原词不计 | `PUT /api/v1/subject-mapping`，请求体 `{dc:subject 原词: 标准主题}`；只收录表内原词 |
| 同一条书目在同一主题下只计一次（旧词/新词混用） | `record_subjects` 去重索引以 `(identifier, standard_subject)` 为主键；多原词归并后同主题只占一行 |
| SQLite 保存词表和计数，换表按全部在架书目完整 dc:subject 重建 | `subject_mapping` / `subject_mapping_version` / `record_subjects` / `subject_counts` 四表，同事务整体替换，原书目不改 |
| 补采只按本批书目前后状态调整计数，不重扫全库 | 每条记录先取变更前快照，按标准主题差集对 `subject_counts` 做 ±1，随书目事务提交 |
| 计数随书目和续采截止日一起提交，失败则都不改 | 计数调整与 upsert/撤下、`advance_progress` 在同一个事务内 |
| 换表和补采撞单时查询看不到不一致的中间态 | 补采与换表共用进程内写锁（撞单返回 409）+ SQLite WAL 快照隔离，读者只能看到某次已提交的“词表+计数”整体 |
| 导航服务查询各标准主题的非零计数 | `GET /api/v1/subject-navigation` 只返回 `count > 0` 的标准主题并带词表版本 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集、换词表并查询计数 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/目录/进度/运行历史/主题导航/健康检查）
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务、计数增量
  subjects.py         换表编排：词表与计数同事务整体替换
  locking.py          补采与换表共用的进程内写锁
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构与事务操作（含词表、去重索引、计数）
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条样例记录（含多值 dc:subject、旧词/新词混用），rec-013 删除
  Dockerfile
tests/                pytest 端到端测试（25 个用例，含主题导航 13 个）
scripts/              辅助脚本（故障回滚、主题计数回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

## 主题导航：词表、去重计数与补采增量

样例源的每条在架书目带一到多个 `dc:subject` 原词，刻意混用旧词/新词：
`电脑/计算机`、`机械/机械工程`、`医学/医药`；其中 `rec-016` 同带“电脑+计算机”两个原词、
`rec-020` 带未收录原词“杂项”、`rec-024` 同时属于两个标准主题。

### 5. 馆员提交对照表（换表）

```bash
curl -X PUT http://localhost:8000/api/v1/subject-mapping \
  -H 'Content-Type: application/json' \
  -d '{"mapping": {
        "电脑": "信息技术", "计算机": "信息技术",
        "机械": "工程技术", "机械工程": "工程技术",
        "医学": "医药卫生", "医药": "医药卫生"
      }}'
```

服务端按**全部在架书目**的完整多值 `dc:subject` 精确匹配并重建计数，响应即新计数：

```json
{
  "status": "success",
  "version": 1,
  "entries": 6,
  "standard_subjects": 3,
  "active_records_indexed": 26,
  "subject_counts": {"信息技术": 9, "工程技术": 9, "医药卫生": 8}
}
```

- 同一条书目两个原词归入同一主题时**只计一次**（`rec-016` 在 `record_subjects` 中只有一行）；
- 未收录的原词“杂项”不计入任何主题；跨两个主题的 `rec-024` 在两个主题下各计一次；
- 换表是整体替换：词表、去重索引、计数、版本号在**同一事务**内提交，失败全部回滚，原书目不动。

### 6. 查询导航计数（只返回非零主题）

```bash
curl http://localhost:8000/api/v1/subject-navigation
# {"version":1,"subjects":[
#   {"standard_subject":"信息技术","count":9},
#   {"standard_subject":"工程技术","count":9},
#   {"standard_subject":"医药卫生","count":8}]}
curl http://localhost:8000/api/v1/subject-mapping   # 查看当前生效的词表与版本
```

### 7. 补采只做增量，不重扫全库

换表之后再补采，服务端对本批每条书目取**变更前快照**（在架状态 + 已挂标准主题），
按变更后差集对计数做 ±1，并与书目 upsert/撤下、截止日推进在同一事务提交：

- 新上在架书目：命中的标准主题各 +1；
- 在架书目的主题发生变化：旧主题 -1、新主题 +1；
- 遇删除标记撤下：原挂主题各 -1，归零的主题从导航中消失；
- 重复采集且主题不变：差集为空，计数不动（幂等）。

补采失败（如 resumptionToken 持续失效）时计数随书目和截止日一起回滚，
可用 `python scripts/test_subject_rollback.py` 复现验证。

### 8. 换表与补采撞单

两类写操作共用一把进程内写锁：并发时后到的请求立即返回 `409`，不排队。
SQLite 运行在 WAL 模式，读请求通过快照隔离只能看到某次已提交的“词表 + 计数”整体，
绝不会看到与已提交目录或词表不符的中间计数。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、主题计数增量、
  截止日推进、运行历史更新；
- 换表在单个 `BEGIN IMMEDIATE` 事务内完成词表替换、去重索引重建、计数重建与版本递增；
- 事务中任何一步失败都会 rollback，库状态回到操作前（计数、截止日、词表均不变）；
- 进程内互斥锁保证同一时刻只有一个写操作（补采或换表），并发请求返回 `409`；
- WAL 模式下读不阻塞写、写不阻塞读，读连接看到的是语句开始时的已提交快照。

### SQLite 主题相关表

| 表 | 作用 |
| --- | --- |
| `subject_mapping` | 词表：`raw_subject`（原词，主键）→ `standard_subject` |
| `subject_mapping_version` | 单行版本号，每次换表 +1，导航响应该版本 |
| `record_subjects` | 去重索引，`(identifier, standard_subject)` 联合主键，同书目同主题只一行 |
| `subject_counts` | 标准主题的在架计数，归零即删行，导航只查非零 |

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
