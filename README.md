# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/backfill`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整抢修流程、重复事件、阀门顺序、试压阈值、现场危险条件、权限、版本冲突和断网补交合并。模型不替代 SCADA、管网水力计算或正式应急预案。

## 断网补交（巡线终端回到值班室后批量上传）

`POST /api/items/<id>/backfill`（角色 `dispatcher`/`responder`/`patrol`/`sensor`）：

- 请求体 `records` 为读数数组，每条含 `source_type`（`sensor` 传感或其他人工类型）、`external_id`（终端外部编号）、`observed_at` 及读数字段。
- 按 `(source_type, external_id)` 幂等合并到同一管段：重传返回 `unchanged`，读数被修正则覆盖更新，不产生重复来源。
- 同一 `external_id`、同一观测时刻的传感版与人工版各自独立保留；管段详情的 `source_coverage.observations` 展示每个时刻到齐了哪几版，`missing_versions` 列出缺的另一版。
- 可选 `expected` 携带终端记录清单（编号字符串或 `{external_id, source_type}`），尚未交回的编号出现在响应和管段详情的 `expected_pending` 中，缺哪条一目了然。
- 每次合并都以建单读数为基线、对全部来源逐项取最大值重算读数与泄漏等级；等级抬升时返回 `level_raised` 和 `triggered_by`（具体哪条补交记录抬高了哪几个指标），并写入 `level_reassessed` 审计事件。
- 管段已隔离且新等级高于隔离时的等级：状态退回 `verified`、已排阀门顺序清空作废（事件 `isolation_voided`，载荷保留作废的顺序与抬级记录）、`reverify_required=true`；重新核验前任何隔离操作返回 409 `reverify_required`，核验后即可按新等级重排阀门。
