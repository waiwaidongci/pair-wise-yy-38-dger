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
- `GET /api/audit`
- `GET /api/handovers`，可按`?status=pending|confirmed`过滤
- `POST /api/handovers`，交班人登记接班人、当前库位和未完成指令
- `GET /api/handovers/{id}`
- `POST /api/handovers/{id}/decisions`，接班人逐条确认，提交`item_id`、`decision`（`takeover`或`return`），退回必须附`reason`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 交接确认

换班时由交班值班员（duty_officer）登记交接：接班人、当前库位和未完成指令（`draft`、`checked`、`authorized`状态）。登记后指令立即锁定，禁止状态流转；只有登记的接班人本人能逐条确认：选择`takeover`后指令解锁继续流转；选择`return`必须写明原因，指令退回`draft`重新进入复核，并生成一条待关闭的`handover_return`记录。全部指令确认后交接自动完成。交接记录与指令版本变化按顺序写入同一审计链，原有角色权限和审计校验不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
