# MOCK演示人员名单

创建日期：2026-11-02。仅本地`127.0.0.1:5433/store_settle`，3队/3队长/12队员；姓名均虚构，不用于真实结算或生产。

- H5：`http://127.0.0.1:8000/my/work`；管理：`http://127.0.0.1:8000/zones`。
- 统一初始密码：`MockOnly2026!`，首登必须改密；改过后初始口令不再适用。账号创建时15人均登录通过并被首登改密拦截，未替用户改密。
- 团队指派随正式版本变化，当前状态见[片区交接](会话交接-片区作业闭环-20261102.md)，不在名单重复维护。

| 团队（编号 / ID） | 演示姓名 | 员工编号 | 登录账号 | 角色 | 账号 ID |
|---|---|---|---|---|---|
| MOCK演示A队（MOCK-A / 1） | MOCK-A队队长 | `MOCK-A-000` | `mock-a-leader` | 队长 | 2 |
| MOCK演示A队（MOCK-A / 1） | MOCK-A队队员01 | `MOCK-A-001` | `mock-a-member-01` | 队员 | 3 |
| MOCK演示A队（MOCK-A / 1） | MOCK-A队队员02 | `MOCK-A-002` | `mock-a-member-02` | 队员 | 4 |
| MOCK演示A队（MOCK-A / 1） | MOCK-A队队员03 | `MOCK-A-003` | `mock-a-member-03` | 队员 | 5 |
| MOCK演示A队（MOCK-A / 1） | MOCK-A队队员04 | `MOCK-A-004` | `mock-a-member-04` | 队员 | 6 |
| MOCK演示B队（MOCK-B / 2） | MOCK-B队队长 | `MOCK-B-000` | `mock-b-leader` | 队长 | 7 |
| MOCK演示B队（MOCK-B / 2） | MOCK-B队队员01 | `MOCK-B-001` | `mock-b-member-01` | 队员 | 8 |
| MOCK演示B队（MOCK-B / 2） | MOCK-B队队员02 | `MOCK-B-002` | `mock-b-member-02` | 队员 | 9 |
| MOCK演示B队（MOCK-B / 2） | MOCK-B队队员03 | `MOCK-B-003` | `mock-b-member-03` | 队员 | 10 |
| MOCK演示B队（MOCK-B / 2） | MOCK-B队队员04 | `MOCK-B-004` | `mock-b-member-04` | 队员 | 11 |
| MOCK演示C队（MOCK-C / 3） | MOCK-C队队长 | `MOCK-C-000` | `mock-c-leader` | 队长 | 12 |
| MOCK演示C队（MOCK-C / 3） | MOCK-C队队员01 | `MOCK-C-001` | `mock-c-member-01` | 队员 | 13 |
| MOCK演示C队（MOCK-C / 3） | MOCK-C队队员02 | `MOCK-C-002` | `mock-c-member-02` | 队员 | 14 |
| MOCK演示C队（MOCK-C / 3） | MOCK-C队队员03 | `MOCK-C-003` | `mock-c-member-03` | 队员 | 15 |
| MOCK演示C队（MOCK-C / 3） | MOCK-C队队员04 | `MOCK-C-004` | `mock-c-member-04` | 队员 | 16 |

开户与团队变更沿用既有服务并留审计；创建前备份`data/backup/before-mock-staff-20261102.dump`。未新建管理员或重置其口令，不导入结算记录。测试结束需停用时走停用/移出流程保留历史；本次文档整理不执行账号操作。
