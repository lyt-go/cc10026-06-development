# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 批量查询收益核验

批量查询执行前先绑定优化计划和实际版本，再用「收益核验样本记录」判断计划是否
达到收益阈值。核验口径：

- 每个比较样本必须对应**同一种查询**（`query_key`，去掉具体分区值）、
  **同一输入规模**（`input_scale`）和**同一实际计划版本**；
- 对照版本与优化版本都要有成功实测耗时，多次实测取中位数；
- 超时、缺少对照或版本不一致的样本进入 `excluded`，**不使用预估收益补入**；
- 报告按「规则编号 + 计划版本」存档到 `data/query_benefit/verifications/`，
  修改计划版本后必须重新核验，旧版本结论仍可追溯；
- 达线结论同时给出可用数据范围（覆盖哪些查询类别、输入规模、排除多少样本）；
- 每次执行把延迟明细、实际版本写入 `data/query_benefit/batches/`，
  同一批查询重复触发时可核对两轮分别使用的计划版本。

接口（`backend/app/routers/benefit.py`）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/benefit/plans` | 读取各计划核验结论与达阈值规则 |
| POST | `/api/benefit/verify` | 按样本记录核算收益并归档 |
| GET | `/api/benefit/verifications/{rule_no}/{version}` | 读取历史核验报告 |
| POST | `/api/benefit/plans/{entry_id}/revise` | 修改计划并切换实际版本 |
| GET | `/api/benefit/batches/{batch_id}/preflight` | 执行前绑定计划与样本入口 |
| POST | `/api/benefit/batches/{batch_id}/execute` | 执行并保留延迟明细 |
| POST | `/api/benefit/batches/{batch_id}/runs` | 补齐实测样本 |
| GET | `/api/benefit/batches/{batch_id}/rounds` | 核对两轮执行版本 |

前端在「查询优化」「批量查询」工作台展示核验结论、数据范围和两轮版本核对。
示例数据里 `BATC-0001` 是混合批次：短查询有成对实测并达阈值，长查询因超时
缺少对照而单独列出、不计入收益。

领域：数据工程
子方向：数据分析与数据应用
