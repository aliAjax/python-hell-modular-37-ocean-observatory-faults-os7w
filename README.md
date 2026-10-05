# 海底观测网设备故障管理

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8337`。领域对象包括站点、资产、链路、遥测、故障事件、恢复动作、恢复处理链、出海任务和数据缺口。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计、幂等键、处理链守卫和资源持有记账。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/recovery.py`：恢复处理链编排：依赖串链、共享资源引用计数、失败补偿与断点恢复。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和恢复处理链测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8337
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8337/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/resources/<id>/holds`：查看共享资源的引用计数与持有方（处理链、步骤）。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

建立站点、资产和链路后记录遥测与故障事件，创建恢复动作并跟踪重启、备用链路、出海任务和数据缺口，最后关闭事件。遥测`revise`动作只接受更高修订号，用于处理迟到数据。

## 恢复处理链

恢复动作按依赖串成链：通过`POST /api/recovery_plans`提交`{"incident_id":"...","steps":[...]}`，每步登记`depends_on`（依赖的上一步，首步为空）和`resources`（占用的共享资源实体id，如备用链路、出海任务）。同一故障事件同时只受理一条处理链：两名值班员并发提交时，由数据库守卫约束保证先入库的生效，后到者收到冲突错误；链进入终态（`succeeded`/`compensated`/`cancelled`）后才允许提交新链。

处理链动作（`POST /api/entities/<plan_id>/actions`）：

- `start_plan` / `cancel_plan`：启动或撤销未启动的链。
- `start_step`：依赖的上一步已成功才能启动；启动时占用步骤声明的共享资源。
- `complete_step`：步骤生效；全部步骤成功后链为`succeeded`。
- `fail_step`：步骤失败，链转入`compensating`。
- `compensate` / `resume_compensation`：沿依赖逆序回滚已生效步骤，未启动的步骤标记`skipped`。

补偿语义：

- 共享资源按持有记录引用计数，补偿只释放本链本步骤的持有；其他故障事件仍在占用的资源不会被一次补偿放掉。
- 补偿每步一个事务，中途失败（或达到`max_steps`批次上限）停在断点，链记为`compensation_failed`并记录`breakpoint_step`；重启后调用`resume_compensation`从断点继续。
- 重复补偿是幂等的：已补偿步骤自动跳过，资源持有只释放一次，不会重复释放。
- 事件`resolve`前，处理链必须`succeeded`（全做成）或`compensated`（回滚干净），否则冲突拒绝。

## 规则重点

- 同一资产和故障类型不能同时有多个活动事件。
- 恢复动作按`dedupe_key`防止重复执行。
- 事件解决前恢复动作、恢复处理链、数据缺口和受影响资产必须达到可关闭状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
