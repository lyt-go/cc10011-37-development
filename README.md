# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 + 评分快照 + 准入审计 | 四维权重、准入门槛、规则版本、评分周期快照 |

## 数据质量评分准入链

数据服务发布与调用都要经过质量评分准入，相关实现集中在后端 `app/services`：

```text
聚合数据集 dataset_profile
  └─ quality_snapshot 按完整阶段生成评分快照（5 个阶段，可中断恢复）
       ├─ quality_gate   唯一的可审计门槛判定（发布/调用/列表共用同一份逻辑）
       ├─ quality_audit  每次判定留痕，固化权重、维度得分、总分、门槛与阻断原因
       └─ aggregate      评分结果回写聚合记录（快照编号、总分、评分周期）
数据服务 dataservice
  ├─ 上线服务：必须绑定满足当前门槛的有效评分快照，否则阻断发布
  └─ 服务调用：返回结果记录引用的快照；快照失效后阻断新请求
```

- **阶段化快照**：`加载聚合数据集 → 解析生效评分规则 → 计算维度加权得分 → 生成评分周期快照 → 回写聚合记录`。
  快照只能按完整阶段推进，中断后从最后一个完整阶段继续，不回退到半成品；同一（数据集, 评分周期）
  重复计算幂等，只形成一份结果。
- **可追溯**：每条快照固化四维原始分、归一化权重、加权明细、总分与评分周期。
- **规则版本**：同一数据集并发启停时只允许一个规则版本生效（服务内加锁）；版本切换或停用后，
  旧版本产出的有效快照立即失效。
- **冲突权衡**：质量分未达标与发布请求冲突时，发布动作严格按门槛拒绝；已上线服务保留快照绑定关系，
  但在调用入口按快照有效性阻断，即「发布看门槛、运行看快照有效性」。
- **调用链缺口追溯**：`GET /api/qualityscore/trace/gap?dataset=...` 或 `service_id=...`
  沿 `聚合数据集 → 聚合回写 → 生效规则 → 有效快照 → 服务绑定 → 快照引用` 逐节点报告首个缺口。

后端回归测试（覆盖上述规则，纯标准库）：

```bash
cd backend && .venv/bin/python -m unittest discover -s tests
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
