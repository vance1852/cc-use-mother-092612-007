# 建设夜市名中医与志愿团队协同台账基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/night_market_foundation/scheduling.py`：第二届中医文化夜市跨专区协同排班台账；
- `tests/`：基础规则、事务边界、接口路由、协同台账规则和端到端验收测试。

## 协同台账模块

`scheduling.py` 在基础边界之上实现排班负责人需要的协同规则：

- **台账登记**：人员资质（有效期）、可服务时间、专区、班次与岗位（最低在岗数、责任人岗位）、岗位依赖；已确认班次每次变更都会递增版本并保存快照，可按版本回溯。
- **缺员替岗**：迟到、提前离场或缺席时生成替岗方案，包含多种选择（空闲顶替、跨岗借调、分段覆盖）并说明各自影响；只有负责人或有效期内的紧急接管人确认某个完整方案后才更新排班。确认时重新校验资格过期、时间冲突与最低在岗数，任一不满足则整体拒绝，不会留下半套变更。
- **签到事实**：班次开始后的签到、迟到回执、提前离场只能追加，不能回写；重复签到与迟到回执保持幂等（同一事实只留一条记录）。
- **紧急接管**：登记原因、有效期限和交接事项；期限届满后接管人自动失去调度权；服务重启后可通过待办交接列表接续尚未完成的交接事项。
- **最小权限读取**：负责人可读取完整参与者信息，其他岗位必须指定履职视图（`escort`/`supply`/`order`），只返回履职必需字段；审计角色不能读取参与者信息。
- **接口问答**：指定时刻的区域责任人、班次未覆盖依赖、拒绝调度的具体原因（资格过期、时间冲突、可服务时间不足、最低在岗数不足）均可查询。

主要接口（写入接口均需要 `request_id` 幂等键与 `X-Actor-Id`）：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /scheduling/participants`、`/qualifications`、`/availability`、`/zones`、`/shifts` | 台账登记 |
| `POST /scheduling/assignments` | 直接排班（校验资质、时间、冲突） |
| `POST /scheduling/replacement-plans`、`/plan-confirmations` | 生成替岗方案、确认完整方案 |
| `POST /scheduling/checkins` | 追加签到事实（幂等） |
| `POST /scheduling/takeovers`、`/handover-completions` | 紧急接管与交接办结 |
| `GET /scheduling/shifts`、`/shifts/revision` | 班次详情与历史版本快照 |
| `GET /scheduling/zones/responsible` | 指定时刻的区域责任人 |
| `GET /scheduling/shifts/uncovered-dependencies` | 未覆盖的岗位依赖 |
| `GET /scheduling/dispatch-logs` | 调度结果与拒绝原因 |
| `GET /scheduling/handovers/pending` | 待接续的交接事项 |
| `GET /scheduling/participants` | 按岗位履职视图读取参与者信息 |

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
PYTHONPATH=src python3 -m night_market_foundation.scheduling_acceptance
```

基础验收在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链；协同台账验收执行完整的排班协同链（登记、签到、替岗、接管、重启接续交接），成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
