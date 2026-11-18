# 供水账户用量明细服务

把换表前后的**累计抄表读数**转换为可装载的**账户用量明细**区间结果。
技术栈：FastAPI + SQLite + 原生 HTML/JS 前端，可用 Docker Compose 一键启动。

## 1. 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `water-data`（容器内 `/data`），容器重建后数据保留。

### 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 默认数据目录 /data，可用环境变量覆盖：
#   WATER_DATA_DIR=./data  WATER_DB_PATH=./data/water.db
```

健康检查：`GET /healthz`；交互式 API 文档：`GET /docs`。

## 2. 输入文件格式（均为 CSV，UTF-8 编码，兼容带 BOM 与 GB18030）

### 读数 CSV（增量上传，可多次）

| 列名 | 含义 |
| --- | --- |
| `account` | 账户号 |
| `meter` | 表号 |
| `read_at` | 抄表时刻，ISO-8601；无时区后缀按 UTC 处理，统一以 UTC 存储 |
| `reading_cum` | 累计立方米读数，非负数 |

### 换表 CSV（整版上传，**每户一条**）

| 列名 | 含义 |
| --- | --- |
| `account` | 账户号 |
| `old_meter` | 旧表表号 |
| `new_meter` | 新表表号 |
| `switched_at` | 切换时刻（UTC） |

换表文件为**整版快照**：每次成功上传都会整体替换旧换表数据（换表更正场景）。

样例见 `samples/` 目录。

## 3. 业务规则

1. **仅同户、同表、相邻的两笔读数可相减**，且区间不得跨越该户的换表切换时刻；
   用量 = 后笔累计读数 − 前笔累计读数，**归属到后笔所在的 UTC 日**。
2. 以下读数标 `REVIEW`（待核），不产生用量：
   - `unpaired_reading`：无法配对（每只表的第一笔、或前序读数已被待核拦截）；
   - `reversed_reading`：累计读数倒退（后笔 < 前笔）；
   - `crosses_switchover`：相邻区间跨越切换时刻；
   - `outside_meter_service_window`：旧表读数晚于切换时刻，或新表读数早于切换时刻；
   - `no_replacement_record`：账户（或该表）在换表整版中无记录，表归属无法确认。
3. **逐条来源与重传去重**：读数 CSV 的**每一条数据行**都写入来源台账 `reading_sources`，
   包括同户同表同时刻同值的重传（批内与跨批均逐条留痕）；同表同时刻**异值**则**整批拒收**
   （HTTP 422，不写入任何数据，拒收批次留审计记录）。一条读数只要存在任一未撤回来源即为有效读数。
4. **批次撤回（网页可操作）**：读数批次可整体撤回，撤回在单个 `BEGIN IMMEDIATE` 事务内原子完成：
   - 该批的逐条来源全部停用；读数若仍有其他未撤回批次供数，则继续参与计算；
   - 仅失去**最后一个有效来源**的读数退出后续生成（原始读数与来源行永久保留，可追溯）；
   - 撤回或重传**未改变有效读数**时，现役结果保持 `active` 不过期；一旦有效读数变化，旧代际标 `expired`；
   - 旧版数据库迁入、缺少逐条来源的批次自动标注 `withdrawable=0`（不可撤回），其读数仍照常供数。
5. **迟到读数 / 换表更正**：任何使有效输入发生变化的新提交都会使当前现役结果自动转为 `expired`（过期），
   需重新生成才会有新的现役结果；过期结果及其明细行仍保留、仍可下载。
6. **生成与上传/撤回交错**：所有写操作（上传、撤回、生成）均在单个 `BEGIN IMMEDIATE` 事务内完成，
   生成只基于生成时**已提交**的输入产出完整快照，期间提交的输入不会被半包纳入；
   撤回与旧结果过期同事务提交，失败整体回滚，不会出现"批次已撤回而旧用量仍现役"的中间状态。
7. **生成失败**（如无任何有效读数）：不发布新结果；旧结果保留可下载，并标记为
   `expired`（`status_detail` 记录失败原因），系统中不会存在声称"现役"的错误结果。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads/readings` | 增量上传读数 CSV（multipart 字段 `file`），逐行记录来源 |
| POST | `/api/uploads/replacements` | 整版上传换表 CSV |
| POST | `/api/batches/{id}/withdraw` | 撤回一个已提交的读数批次（404 不存在；409 已撤回/已拒收/换表批/旧库不可撤回） |
| GET | `/api/batches` | 上传批次审计（含 rejected/withdrawn、逐条来源有效数/总数、`withdrawable` 标识） |
| GET | `/api/batches/{id}/sources` | 查看某读数批次的逐条来源台账（含已撤回行；旧库批次返回空列表与说明） |
| POST | `/api/generations` | 基于当前全部已提交输入生成新一代结果 |
| GET | `/api/generations` | 列出全部结果代际（active/expired/failed、行数、待核数、输入批次） |
| GET | `/api/generations/{id}/rows` | 查看某代明细（JSON，分页参数 `limit`） |
| GET | `/api/generations/{id}/download` | 下载某代结果 CSV（响应头 `X-Generation-Status` 标明状态） |
| GET | `/api/generations/latest/download` | 下载最新一代结果 |
| GET | `/healthz` | 健康检查 |

### 下载 CSV 列

`generation_id, account, meter, interval_start_utc, interval_end_utc,
start_reading_m3, end_reading_m3, consumption_m3, attributed_utc_day,
status, review_reason, review_reason_detail, source_reading_ids`

其中 `source_reading_ids` 为产生该行的来源读数主键（待核行为单笔，正常区间为前后两笔）。

## 5. 数据模型（SQLite）

- `upload_batches`：每次上传的批次与审计（committed/rejected/withdrawn、拒收原因、
  `withdrawable` 是否可撤回、`withdrawn_at` 撤回时间）；
- `reading_sources`：**逐条来源台账**，每条上传 CSV 数据行一条（含同值重传），
  记录所属批次、对应读数、行号、值与 `active` 状态，撤回仅停用不删除；
- `readings`：去重后的累计读数，`(account, meter, read_at)` 唯一，`active=1`
  表示至少有一条有效来源；读数永不删除，失去全部来源时停用，重传可重新生效；
- `replacements`：当前生效的换表整版快照；
- `generations`：结果代际及生成时所用的输入批次清单；
- `result_rows`：每代的区间明细与待核原因。

旧版数据库（无 `reading_sources`、无 `active`/`withdrawable` 列）在服务启动时
自动迁移：表结构原地升级，历史批次标注 `withdrawable=0` 不可撤回，历史读数按有效处理。

## 6. 测试

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q
```

覆盖：正常配对与 UTC 归属日、首笔无法配对、倒退待核、跨切换与服务窗口、
同值重传逐条留痕（批内/跨批）、异值整批拒收、迟到读数过期、换表更正过期、
生成失败保留旧结果、CSV 下载内容、无换表记录待核、朴素时间按 UTC 处理，
以及：撤回共享来源（其他批次供数的读数保留、仅失最后来源的读数退出）、
撤回后历史代际仍可下载、重传重新生效、无变化不过期、撤回守卫（404/409/重复撤回）、
旧库迁移批次不可撤回但照常供数，以及多线程上传/撤回/生成交错的串行化一致性。
