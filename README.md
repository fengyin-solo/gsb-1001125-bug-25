# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 隧道现场检修（通风/消防 · 离线草稿 · 检修单回写）

针对「弱网下隧道详情页切到消防工作表后通风笔记丢失、重连重复显示检修单、
回写后列表仍残留草稿」的端到端修复，代码位于
`backend/app/inspection_store.py`、`backend/app/services/inspection.py`、
`backend/app/routers/inspection.py`，前端位于
`frontend/src/stores/inspection.ts`、`views/tunnel_detail/`、`views/tunnel_fire/`。

复现/操作路径（三入口数据同源）：

1. 隧道管养列表页 `/tunnel`：`GET /api/tunnel/inspection/digest` 给出每座
   隧道的未收口草稿、设施待办、检修单计数；回写后同一草稿立即消失。
2. 隧道详情页 `/tunnel/detail?code=隧道编号`：写通风笔记并「现场确认」。
3. 消防工作表 `/tunnel/fire?code=隧道编号`：写消防检查项 → 合并草稿 →
   回写检修单；弱网时命令进 localStorage 重传队列，重连后自动按序排空。

规则口径：

- 草稿先落本机（localStorage），通风/消防同一份工作草稿，切页不丢；
- 合并以检查员最后确认的现场版本为准（`drafts/{id}/confirm`），被并草稿
  保留 `provenance` 来源；旧草稿迁移必须带 `origin`（`/migrations/legacy-draft`）；
- 草稿、附件（提交瞬间冻结为 `attachments_snapshot`）、工单号在同一事务提交，
  任一步失败整体回滚（含隧道档案 `pending` 标记）；
- 离线重传以「隧道编号 + 本地序列号 `client_seq`」幂等，重传返回原工单
  （`created=false`），不重复建单；
- 并发补写现场证据必须带 `base_version`，过期写入返回 `409` 并附带服务端
  当前版本，绝不覆盖他人现场证据；
- 一致性自检：`GET /api/tunnel/inspection/tunnels/{code}/consistency`
  同时核对隧道档案、设施待办、工单、未收口草稿。

后端回归：`cd backend && python3 -m pytest tests/`。
