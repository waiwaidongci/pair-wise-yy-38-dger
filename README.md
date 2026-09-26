# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/handovers`（可按`status=open|completed`过滤）
- `POST /api/handovers`，登记交接：交班人、接班人、值班人员、当前库位和未完成指令
- `GET /api/handovers/{id}`（含每条指令的确认状态）
- `POST /api/handovers/{id}/items/{handover_item_id}`，接班人逐条决策
- `GET /api/audit`（可按`entity_type`过滤，如`交接单`）

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 汛期交接确认

- 交班人调用`POST /api/handovers`登记人员名单（`personnel`）、当前库位（`reservoir_level`）和未完成指令（`item_ids`，已关闭指令不可登记）。
- 登记后未确认指令立即**锁定**：流转（transition）和追加记录（records）返回409，查询不受影响并带`handover_lock`标记；数据库部分唯一索引保证同一指令只有一个待确认锁定。
- 接班人对每条明细选择`accepted`（接手）或`returned`（退回，必须填写`reason`）：
  - 接手后该条立即解锁，可继续正常流转。
  - 退回后指令状态回到`draft`重新进入复核、`version`加1，交接明细记录退回原因。
  - 全部条目确认后交接单自动置为`completed`。
- 交接登记、接手、退回及指令版本变化统一写入原有SHA-256审计链（`handover_register`/`handover_accept`/`handover_return`），角色矩阵不变，登记与决策均限`duty_officer`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
