# 传染病暴发调查与接触网络

只使用 Python 标准库和 SQLite 的模块化项目，默认端口 `8303`。通用的实体状态机、乐观锁和审计在底层；县疾控现场流调（多队同链、断网批次合并、随访窗口级联、病例收尾联锁）的业务规则集中在 `src/rules.py` 与 `src/service.py`，`app.py` 只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常（含批次合并异常 `BatchMergeError`）。
- `src/rules.py`：状态机、权限、随访窗口纯规则计算、收尾阻断、跨对象校验。
- `src/repository.py`：SQLite 建表、人员主索引、批次台账、去重合并、乐观锁。
- `src/service.py`：用例编排——断网批次合并、发病日期改动级联重算、收尾联锁、一致性报告。
- `src/http_api.py`：HTTP 路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和流调业务场景（`tests/test_epi.py`）。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时自动建表。`--host` 修改监听地址，`--db` 指定其他 SQLite 文件。

## 业务规则（流调主线）

### 1. 多队同链与断网批次合并

- 一个离线批次（`batch_id`）归属于一条传播链（`chain_id`）上的一个病例，携带流调队（`team_id`）、乡镇（`township`）和接触者条目。
- 人员主索引 `persons` 以**身份证 → 手机号 → 业务 person_id** 的顺序判重，身份证/手机号有数据库唯一索引。同一个人被多个乡镇的多个流调队登记，只产生一个人；同人在同一病例下只产生一条接触者记录，多次报告的暴露日期会合并为**最宽暴露窗口**。
- 合并逐项提交，每条目在台账 `inbound_batch_items` 中有 `applied / pending / rejected` 状态，暴露登记表 `contact_exposures` 对 `(batch_id, item_id)` 唯一。
- **中途失败可按原批次重试**：重试就是用同一个 `batch_id` 再提交同样的条目；已入账的跳过、永久性问题（无身份信息、日期非法、身份证冲突）保持拒绝、失败项继续。任何情况下都不会重复登记同一个人。
- 批次传播链与病例不一致、病例已收尾时，合并直接拒绝。

### 2. 发病日期改动 → 随访窗口级联重算

- 随访窗口（见 `compute_followup`）：起算日 = `max(病例发病日期, 末次暴露日期)`，观察期默认 14 天（可按病种在服务 `policies` 配置，或病例 `observation_days` 覆盖），截止日 = 起算日 + 13 天（起算日算第 1 天，窗口含首尾）。
- 随访状态按当前日期对窗口判定：`pending`（窗口未开始）、`active`（窗口内）、`completed`（已过截止日）。
- 发病日期通过 `POST /api/cases/<id>/onset` 修改后，病例下**所有接触者**的起算日、截止日、状态全部重算落库并写审计。
- 发病日期缺失/非法、暴露日期缺失/非法、观察期天数非法时，不强算，接触者标 `needs_review` 并记录 `review_reasons`，等待人工复核。

### 3. 病例收尾联锁

- 病例 `close` 时先做角色与状态校验，再**用当前病例数据当场重算**全部接触者（不信任存量随访字段）。
- 只要有接触者处于 `active / pending / needs_review`，收尾被拒绝（`409 InvalidTransition`），并在审计里记录每个阻断者；全部 `completed`（或没有接触者）才允许收尾。
- 已收尾病例不允许再改发病日期，也不允许再合并新接触者。

### 4. 两边说法对得上

- `GET /api/cases/<id>/consistency` 给出一致性报告：逐个对比接触者存量随访字段与按当前数据应得的结果，列出所有 `drift`（字段、现值、期望值）、收尾阻断者和总判定。重算后再查应恢复 `consistent=true`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用 `?status=` 过滤。
- `POST /api/<kind>`：创建对象；`Idempotency-Key` 请求头支持创建幂等。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交 `{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/batches/merge`：合并/重试离线批次（investigator、admin）。成功返回 200；中途失败返回 202，响应体 `partial` 给出已入账进度，客户端用同一 `batch_id` 重试。
- `GET /api/batches/<batch_id>`：批次头与逐条台账。
- `POST /api/cases/<id>/onset`：`{"onset_date":"YYYY-MM-DD"}`，级联重算随访。
- `GET /api/cases/<id>/contacts`：病例下接触者。
- `GET /api/cases/<id>/consistency`：随访字段一致性报告。
- `GET /api/audit`：读取审计记录。

请求身份通过 `X-User-Id` 和 `X-Role` 请求头传入。批次合并允许 `investigator/admin`；发病日期修改允许 `clinician/investigator/admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_epi.py` 覆盖：跨队同人合并（身份证/手机号）、同人多病案、并发提交、批次中途失败原批次重试、永久性条目不重试、发病日期改动级联、算不出标 `needs_review`、窗口内/窗口前/待复核阻断收尾、窗口结束后收尾、一致性漂移检测与修复、HTTP 冒烟。

## 局限

随访窗口的 14 天默认值与起算规则是可配置的调查辅助口径，不替代公共卫生部门按病种发布的正式管理规范；`needs_review` 条目必须由流调人员人工核实。
