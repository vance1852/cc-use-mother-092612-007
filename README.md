# 建设夜市名中医与志愿团队协同台账基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

`src/night_market_scheduling/` 是在基础层之上实现的第二届中医文化夜市**跨专区协同排班台账**：

- **台账登记**：参与者（名中医、护理人员、讲解志愿者、后勤）及其资质有效期、可服务时间，专区、岗位（含最低在岗数与履职必需字段）、岗位依赖（防环路）、班次；
- **排班版本**：负责人确认完整方案后生成不可变的已确认版本，历史版本保留可查；资格过期、时间冲突、最低在岗数不足或依赖未覆盖时整体拒绝，不留半套变更；
- **缺员替岗**：迟到/提前离场/缺席上报后生成多种完整替岗方案并说明各自影响（在岗变化、依赖覆盖、备注），同时记录每名候选人被拒绝调度的具体原因；只有负责人（或期限内的紧急接管人）确认某个完整方案后才更新排班，确认时重新全量校验；
- **签到事实**：班次签到与迟到回执写入后不可回写（数据库触发器保证），重复签到与重复回执按请求编号和业务键双重幂等；
- **紧急接管**：登记原因、有效期限与交接事项，期限内接管人获得调度权，期限届满自动失去；交接事项持久化，服务重启后可查询并接续未完成交接；
- **最小知情**：岗位花名册只返回该岗位履职必需的参与者字段（如秩序维护看不到联系电话）；
- **台账查询**：指定时刻的专区责任人与调度权归属、班次未覆盖依赖与在岗缺口、缺员评估的拒绝原因、班次版本历史。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/night_market_scheduling/`：协同排班台账的领域规则、表结构、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、台账规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
PYTHONPATH=src python3 -m night_market_scheduling.acceptance
```

基础层验收在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链。台账验收跑完整值班故事：确认排班、迟到缺员、生成并确认替岗方案、签到幂等、紧急接管到期失效、重启后续接交接，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m night_market_scheduling.api --database night_market.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。协同台账接口统一挂在 `/scheduling/` 前缀下：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /scheduling/participants`、`/qualifications`、`/availability` | 登记参与者、资质有效期、可服务时间 |
| `POST /scheduling/zones`、`/posts`、`/post-dependencies`、`/shifts` | 登记专区、岗位、岗位依赖、班次 |
| `POST /scheduling/shifts/{id}/confirm` | 确认完整排班方案，生成新版本 |
| `POST /scheduling/shortages` | 上报迟到/提前离场/缺席 |
| `POST /scheduling/shortages/{id}/proposals` | 生成多种替岗方案与影响说明 |
| `GET /scheduling/shortages/{id}/proposals`、`/rejections` | 查询待选方案与拒绝调度的具体原因 |
| `POST /scheduling/proposals/{id}/confirm` | 负责人确认某个完整方案后更新排班 |
| `POST /scheduling/checkins` | 记录签到或迟到回执（幂等、不可回写） |
| `POST /scheduling/takeovers` | 登记紧急接管（原因、有效期限、交接事项） |
| `POST /scheduling/handovers/{id}/complete`、`GET /scheduling/handovers/pending` | 完成交接、查询未完成交接 |
| `GET /scheduling/zones/{id}/responsible?at=` | 指定时刻的专区责任人与调度权归属 |
| `GET /scheduling/shifts/{id}/uncovered-dependencies?at=` | 未覆盖依赖与在岗缺口 |
| `GET /scheduling/shifts/{id}/versions` | 已确认班次版本历史 |
| `GET /scheduling/posts/{id}/roster?shift_id=` | 按岗位履职必需字段返回当班花名册 |
| `GET /scheduling/participants/{id}` | 参与者完整台账资料（仅管理角色） |
