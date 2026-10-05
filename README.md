# 海底观测网设备故障管理

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8337`。领域对象包括站点、资产、链路、遥测、故障事件、恢复动作、出海任务和数据缺口。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计、幂等键和共享资源引用计数。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/recovery.py`：恢复链Saga编排、步骤依赖登记、失败反向补偿、断点续跑和幂等补偿。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和恢复链测试。

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
- `GET /api/audit`：读取审计记录。
- `POST /api/recovery-chains`：提交恢复链（登记为`running`，不立即执行），请求体为`{"incident_id":"...","steps":[...]}`，支持`Idempotency-Key`头。
- `GET /api/recovery-chains`：列出恢复链，可用`?incident_id=`过滤。
- `GET /api/recovery-chains/<id>`：读取恢复链详情（含步骤状态和资源占位）。
- `POST /api/recovery-chains/<id>/execute`：执行已提交的恢复链。
- `POST /api/recovery-chains/<id>/resume`：从补偿断点续跑恢复链。
- `GET /api/recovery-resources`：列出共享资源容量、占位和可用量。
- `POST /api/recovery-resources`：登记共享资源容量，请求体为`{"resource_type":"...","resource_key":"...","capacity":数字}`。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

建立站点、资产和链路后记录遥测与故障事件，创建恢复动作并跟踪重启、备用链路、出海任务和数据缺口，最后关闭事件。遥测`revise`动作只接受更高修订号，用于处理迟到数据。

## 恢复链

恢复链把一个事件的多个恢复动作按依赖串成链：每步登记依赖的上一步和占用的共享资源（备用通道、出海任务窗口等）。提交只登记不执行，同事件的链由部分唯一索引保证先入库者生效，重复提交返回`409`。执行时按步骤顺序前转；某步失败则沿依赖反向补偿已生效的步骤：共享资源按引用计数释放，别的事件仍占用的不会被一次补偿放掉。补偿到一半失败则停在断点，重启后自动续跑，也可手动`resume`；重复补偿不会重复释放资源。事件关闭前，恢复链要么全做成（`succeeded`），要么回滚干净（`compensated`），否则`resolve`被拒绝。

步骤定义示例：

```json
{
  "name": "switch_to_backup",
  "depends_on": 0,
  "target": {"kind": "link", "id": "link-1"},
  "action": "activate_backup",
  "compensation": "restore",
  "resources": [{"type": "backup_channel", "key": "station:s1:backup", "units": 1}]
}
```

前转与补偿动作必须是状态机中可互逆的迁移对（如链路`activate_backup`/`restore`、资产`start_reboot`/`abort_reboot`、任务`depart`/`cancel`）。

## 规则重点

- 同一资产和故障类型不能同时有多个活动事件。
- 恢复动作按`dedupe_key`防止重复执行。
- 事件解决前恢复动作、数据缺口和受影响资产必须达到可关闭状态。
- 同一事件只允许一条活动恢复链，先入库者生效；链未到终态（`succeeded`/`compensated`）时事件不能解决。
- 恢复链每步必须登记依赖的上一步，前转与补偿必须是状态机中可互逆的迁移对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
