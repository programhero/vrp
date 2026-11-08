# OR-Tools 通用 VRP HTTP 服务

基于 **Google OR-Tools** 的通用车辆路径规划（Vehicle Routing Problem）服务，使用 **FastAPI** 对外提供 HTTP API。适用于配送、取送货、带时间窗调度等场景。

## 功能特性

- **多仓库 / 异构车队**：每辆车可指定起终点仓库、容量、班次时间窗、成本参数和技能。
- **多维容量**：重量、体积等多维需求同时约束；未填写容量表示不限容量。
- **时间约束**：停靠点时间窗、服务时长、车辆班次、最大线路时长、最大线路距离。
- **取送货**：成对的 pickup / delivery shipment，强制同一车辆且先取后送。
- **可选停靠点**：`drop_penalty > 0` 时允许放弃该点，并在响应中返回被放弃的停靠点。
- **技能约束**：停靠点要求技能，只有具备相应技能的车辆可以服务。
- **多种距离矩阵**：
  - `haversine`：经纬度球面距离
  - `euclidean`：平面坐标距离（米）
  - `precomputed`：请求中直接提供非对称距离/时长矩阵
  - `osrm`：服务端配置的 OSRM `/table` 服务
- **同步 + 异步接口**：短任务同步返回；长任务可提交为后台 Job 轮询。
- **完整响应**：车辆路线、到达/离开时间、等待时间、累计距离、车辆载荷、放弃点、汇总指标。
- **工程化**：Pydantic 请求校验、结构化错误、API Key、OpenAPI 文档、Docker、测试。

## 快速开始

要求 Python 3.10+。本项目已在 Python 3.14 + OR-Tools 9.15.6755 上验证。

```bash
python -m venv .venv
# Windows cmd / PowerShell: .venv\Scripts\activate
# Windows Git Bash:        source .venv/Scripts/activate
# Linux / macOS:           source .venv/bin/activate

pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

打开：

- Swagger UI: <http://127.0.0.1:8000/docs>
- OpenAPI: <http://127.0.0.1:8000/openapi.json>
- 健康检查: <http://127.0.0.1:8000/health>

提交示例请求：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/solve \
  -H "Content-Type: application/json" \
  --data @examples/cvrp.json | python -m json.tool
```

## 目录结构

```text
app/
  main.py                 # FastAPI 应用、异常处理、生命周期
  config.py               # 环境变量配置
  api/
    routes_health.py      # /health、/
    routes_solve.py       # 同步求解
    routes_jobs.py        # 异步任务
    dependencies.py       # API Key、服务依赖
  schemas/
    request.py            # 请求模型与业务校验
    response.py           # 求解响应模型
    jobs.py               # Job 响应模型
  services/
    solver_service.py     # 矩阵构建 + 求解编排
    job_manager.py        # 内存 Job 队列
  vrp/
    matrix.py             # 距离/时长矩阵提供方
    model.py              # OR-Tools 模型构建、求解、结果抽取
    quantities.py         # 多维数量缩放与校验
  core/
    errors.py             # 业务异常
    logging.py            # 日志配置
examples/                 # CVRP、VRPTW、取送货示例
tests/                    # pytest 测试
```

## 单位与基本语义

| 概念 | 单位 / 说明 |
| --- | --- |
| 距离 | 米 |
| 时间 | 秒；时间窗从 0 开始计时 |
| 坐标 | `lat`/`lon` 为 WGS84 度；`x`/`y` 为米 |
| 需求 / 容量 | 任意非负数值，支持小数和最多 10 维 |
| 速度 | `matrix.speed_mps`，默认 10 m/s（36 km/h），仅用于未提供时长矩阵时 |
| 成本 | 由距离、时长权重、车辆成本系数、固定成本、放弃惩罚组成，内部取整 |

**配送点语义**：`stop.demand` 表示从仓库装车、在该点卸下的货物。车辆出发时已装载这些货物，响应 `initial_load` 会展示初始载荷。

**Shipment 语义**：shipment 的 `demand` 在 pickup 点装车、在 delivery 点卸车；两个停靠点必须由同一车辆服务，且 pickup 必须先于 delivery。

**载荷语义**：路线中每个停靠点的 `load` 是**到达该点、开始作业之前**的载荷。取货点作业后载荷增加，送货点作业后载荷减少；线路结束时载荷为 0。

## 请求模型

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `depots` | array | 是 | 至少 1 个仓库。仓库只作为车辆起终点，不会作为途中停靠点。 |
| `stops` | array | 否 | 停靠点列表，默认空。空列表表示没有客户任务。 |
| `vehicles` | array | 是 | 至少 1 辆车。 |
| `shipments` | array | 否 | 成对取送货任务。 |
| `matrix` | object | 否 | 矩阵配置，默认按坐标自动推断。 |
| `objective` | object | 否 | 目标函数权重。 |
| `solver` | object | 否 | 求解器参数。 |

所有对象均禁止未知字段（`extra="forbid"`），拼写错误会返回 422。

### Depot

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 ID，不能与 stop ID 重复。 |
| `name` | string | 否 | 显示名称。 |
| `lat`, `lon` | number | 视矩阵而定 | 经纬度。 |
| `x`, `y` | number | 视矩阵而定 | 平面坐标，单位米。 |

### Stop

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 ID。 |
| `name` | string | 否 | 显示名称。 |
| `lat`, `lon` / `x`, `y` | number | 视矩阵而定 | 坐标。 |
| `demand` | number \| number[] | 否 | 送货需求；默认 0。多维度时与全局维度数一致。 |
| `service_time` | number | 否 | 服务时长（秒），默认 0。 |
| `time_window` | `[start, end]` 或对象 | 否 | 服务开始时间窗；`end` 是最晚开始服务时间。也接受 `{"start":..., "end":...}`。 |
| `required_skills` | string[] | 否 | 服务该点所需技能。 |
| `drop_penalty` | number | 否 | `> 0` 时该点可选，放弃时计入该惩罚；默认 0 表示必须服务。 |

每个 stop 最多被访问一次，且只支持一个时间窗。

### Vehicle

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 ID。 |
| `name` | string | 否 | 显示名称。 |
| `start_depot` | string | 是 | 起点仓库 ID。 |
| `end_depot` | string | 否 | 终点仓库 ID；默认等于 `start_depot`。 |
| `capacity` | number \| number[] | 否 | 容量；不填表示不限容量。多维时与全局维度数一致。 |
| `time_window` | `[start, end]` 或对象 | 否 | 车辆班次；线路必须在窗口内开始并结束。 |
| `max_duration` | number | 否 | 单条线路最大时长（秒），包含行驶、服务、等待。 |
| `max_distance` | number | 否 | 单条线路最大距离（米）。 |
| `fixed_cost` | number | 否 | 使用该车辆时产生的一次性成本，默认 0。 |
| `cost_multiplier` | number | 否 | 该车辆弧成本倍率，默认 1。 |
| `skills` | string[] | 否 | 车辆具备的技能。 |

### Shipment

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 ID。 |
| `name` | string | 否 | 显示名称。 |
| `pickup` | string | 是 | 取货 stop ID。 |
| `delivery` | string | 是 | 送货 stop ID。 |
| `demand` | number \| number[] | 是 | 在该 shipment 中搬运的数量。 |

Shipment 的两个 stop 必须存在、不能相同、不能声明自己的 `demand`，且 `drop_penalty` 必须为 0（shipment 不支持放弃）。一个 stop 最多属于一个 shipment。

### MatrixOptions

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `provider` | `auto` \| `haversine` \| `euclidean` \| `precomputed` \| `osrm` | `auto` | 矩阵来源。 |
| `speed_mps` | number | 10 | 由距离推导时长时使用的速度。 |
| `distances` | number[][] | 无 | 预计算距离矩阵（米）。 |
| `durations` | number[][] | 无 | 预计算时长矩阵（秒）；省略时按距离/速度推导。 |

`auto` 的推断顺序：提供了 `distances` → `precomputed`；所有点都有 `lat`/`lon` → `haversine`；所有点都有 `x`/`y` → `euclidean`；否则报错。

**预计算矩阵节点顺序**：先按请求顺序排列所有 `depots`，再按请求顺序排列所有 `stops`；矩阵大小为 `(len(depots) + len(stops))²`。支持非对称矩阵。所有值必须为有限非负数。

**OSRM**：必须显式指定 `provider: "osrm"`，且服务端配置 `VRP_OSRM_BASE_URL`。请求中不能填写任意 URL（避免 SSRF）。OSRM 表服务需返回 `distances` 和 `durations`；不可达点（`null`）会返回 502。

### ObjectiveWeights

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `distance` | number | 1 | 每米距离的成本权重。 |
| `duration` | number | 0 | 每秒行驶时长的成本权重。 |

单条弧成本（取整后内部再乘 1000）：

```text
cost = (distance_m * objective.distance + duration_s * objective.duration)
       * vehicle.cost_multiplier
```

另加车辆 `fixed_cost` 和放弃点的 `drop_penalty`。响应中的 `objective_value` 与每条路线的 `cost` 使用同一单位。

### SolverOptions

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `time_limit_seconds` | number | 5 | 求解时间上限；受服务端 `VRP_MAX_TIME_LIMIT_SECONDS` 限制。 |
| `first_solution_strategy` | enum | `AUTOMATIC` | OR-Tools 初始解策略。 |
| `local_search_metaheuristic` | enum | `GUIDED_LOCAL_SEARCH` | 局部搜索元启发式。 |
| `log_search` | boolean | false | 是否输出 OR-Tools 搜索日志。 |

可用初始解策略：`AUTOMATIC`、`PATH_CHEAPEST_ARC`、`PATH_MOST_CONSTRAINED_ARC`、`SAVINGS`、`GLOBAL_CHEAPEST_ARC`、`LOCAL_CHEAPEST_INSERTION`、`PARALLEL_CHEAPEST_INSERTION`、`CHRISTOFIDES`、`SEQUENTIAL_CHEAPEST_INSERTION`。取送货或约束较强的模型建议使用 `AUTOMATIC`。

可用元启发式：`AUTOMATIC`、`GREEDY_DESCENT`、`GUIDED_LOCAL_SEARCH`、`SIMULATED_ANNEALING`、`TABU_SEARCH`、`GENERIC_TABU_SEARCH`。

## 响应模型

### 求解状态

| `status` | 含义 |
| --- | --- |
| `optimal` | OR-Tools 报告已证明最优（通常为小规模或搜索完成）。 |
| `feasible` | 找到可行解，但不保证最优。 |
| `infeasible` | 约束不可行，不存在可行解。 |
| `no_solution` | 在时间上限内未找到可行解。 |

求解器正常执行时，即使状态为 `infeasible` / `no_solution`，HTTP 仍返回 **200**；请检查响应体中的 `status`。Job 的 `status` 只在求解过程发生错误时为 `failed`。

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `objective_value` | OR-Tools 目标值；无解时为 null。 |
| `solve_time_seconds` | 端到端引擎耗时，包含矩阵构建和求解。 |
| `ortools_status` | OR-Tools 原始状态名，例如 `ROUTING_SUCCESS`。 |
| `routes` | 已使用的车辆路线；空路线不返回。 |
| `dropped_stops` | 未被服务的停靠点（包括可选点）。 |
| `summary` | 车辆数、服务点数、总距离/时长/等待/服务时长。 |
| `metadata` | 矩阵来源、点数、车辆数、shipment 数、权重等。 |

每条 `route`：

| 字段 | 说明 |
| --- | --- |
| `vehicle_id`, `vehicle_index` | 车辆 ID 与请求中的索引。 |
| `start_time_s`, `end_time_s`, `duration_s` | 线路开始、结束时间与总时长。 |
| `distance_m` | 总距离。 |
| `travel_time_s`, `waiting_time_s`, `service_time_s` | 行驶、等待、服务时长；三者之和等于总时长。 |
| `initial_load` | 出发时载荷向量；只包含送货任务时为线路总送货量。 |
| `capacity` | 该车容量；不限容量时为 null。 |
| `fixed_cost`, `cost` | 固定成本与线路成本；线路成本不含放弃点惩罚。 |
| `stops` | 包含起点仓库、客户停靠点、终点仓库的完整序列。 |

每个 `stop`：

| 字段 | 说明 |
| --- | --- |
| `sequence` | 在路线中的顺序，从 0 开始。 |
| `kind` | `depot` / `stop`。 |
| `id`, `name` | 仓库或停靠点 ID / 名称。 |
| `shipment_id`, `shipment_role` | 若属于 shipment，则为 shipment ID 和 `pickup` / `delivery`。 |
| `arrival_time_s` | 到达/服务开始时间（已完成可能的前置等待）。 |
| `waiting_time_s` | 相对“上一站离开 + 行驶时间”的等待时间。 |
| `service_time_s` | 服务时长。 |
| `departure_time_s` | 离开时间 = `arrival_time_s + service_time_s`。 |
| `cumulative_distance_m` | 从线路起点开始的累计距离。 |
| `load` | 到达该点时的载荷向量（作业前）。 |

路线总会包含起点和终点仓库；即使起终点是同一个仓库，也会分别出现一条 depot 记录。

### 响应示例（节选）

```json
{
  "status": "feasible",
  "objective_value": 12200.0,
  "solve_time_seconds": 5.01,
  "ortools_status": "ROUTING_SUCCESS",
  "routes": [
    {
      "vehicle_id": "truck-1",
      "vehicle_index": 0,
      "start_time_s": 0.0,
      "end_time_s": 5400.0,
      "duration_s": 5400.0,
      "distance_m": 12000.0,
      "travel_time_s": 1200.0,
      "waiting_time_s": 3000.0,
      "service_time_s": 1200.0,
      "initial_load": [40.0],
      "capacity": [60.0],
      "fixed_cost": 200.0,
      "cost": 12200.0,
      "stops": [
        {
          "sequence": 0,
          "kind": "depot",
          "id": "depot",
          "name": "上海中心仓",
          "arrival_time_s": 0.0,
          "waiting_time_s": 0.0,
          "service_time_s": 0.0,
          "departure_time_s": 0.0,
          "cumulative_distance_m": 0.0,
          "load": [40.0]
        }
      ]
    }
  ],
  "dropped_stops": [],
  "summary": {
    "vehicles_used": 1,
    "stops_total": 6,
    "stops_served": 6,
    "stops_dropped": 0,
    "total_distance_m": 12000.0,
    "total_duration_s": 5400.0,
    "total_travel_time_s": 1200.0,
    "total_waiting_time_s": 3000.0,
    "total_service_time_s": 1200.0
  }
}
```

> 以上仅为字段示例，`stops` 数组已截断；实际响应会包含完整的起点、所有服务点和终点。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查与版本。 |
| `GET` | `/` | 服务信息。 |
| `POST` | `/api/v1/solve` | 同步求解。 |
| `POST` | `/api/v1/jobs` | 提交异步求解任务，返回 202。 |
| `GET` | `/api/v1/jobs` | 列出最近任务，`limit` 默认 50，最大 200。 |
| `GET` | `/api/v1/jobs/{job_id}` | 查询任务状态与结果。 |
| `GET` | `/docs` | Swagger UI。 |

### 同步求解

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/solve \
  -H "Content-Type: application/json" \
  --data @examples/vrptw.json | python -m json.tool
```

### 异步任务

```bash
# 提交
curl -s -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  --data @examples/cvrp.json
# => {"job_id":"...","status":"queued", ...}

# 轮询
curl -s http://127.0.0.1:8000/api/v1/jobs/<job_id> | python -m json.tool
```

Job 状态：`queued` → `running` → `succeeded` / `failed`。

`succeeded` 时读取 `result`（完整 `SolveResponse`）；`failed` 时读取 `error`。任务仅保存在进程内存中，服务重启后丢失；完成后按 `VRP_JOB_TTL_SECONDS` 过期。默认最多保存 `VRP_MAX_JOBS` 条，达到上限时会先淘汰最老的已完成任务。

### API Key

设置 `VRP_API_KEY` 后，所有 `/api/v1/*` 接口需要请求头：

```http
X-API-Key: <your-key>
```

`/health`、`/`、`/docs` 不受限制。

### 错误格式

```json
{
  "error": {
    "code": "invalid_problem",
    "message": "stop 'a' requires skill(s) ['cold'] that no vehicle provides",
    "details": null
  }
}
```

常见 `code`：

| HTTP | code | 说明 |
| --- | --- | --- |
| 401 | `unauthorized` | API Key 缺失或错误。 |
| 404 | `not_found` | Job 或路由不存在。 |
| 422 | `validation_error` | 请求结构/字段校验失败，`details` 含字段路径。 |
| 422 | `invalid_problem` | 结构合法但问题不可建模，如时间上限超限、技能无匹配车辆。 |
| 422 | `invalid_matrix` | 矩阵缺失、形状错误、负值等。 |
| 429 | `job_capacity_reached` | Job 数量达到上限且没有可淘汰的已完成任务。 |
| 502 | `matrix_provider_error` | OSRM 请求失败或返回无效数据。 |
| 503 | `osrm_not_configured` | 使用 OSRM 但服务端未配置地址。 |
| 500 | `internal_error` | 未预期的内部错误。 |

## 配置

通过进程环境变量配置（参考 `.env.example`）。服务不会自动加载 `.env` 文件；如使用 `.env`，请通过 Docker Compose 的 `environment` / `env_file`、systemd、shell（`set -a; source .env`）或 IDE 运行配置注入。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VRP_APP_NAME` | `OR-Tools VRP Service` | 服务名称。 |
| `VRP_HOST` | `0.0.0.0` | `python -m app.main` / Docker 的监听地址。 |
| `VRP_PORT` | `8000` | 监听端口。 |
| `VRP_LOG_LEVEL` | `INFO` | 日志级别。 |
| `VRP_API_KEY` | 未设置 | 设置后启用 API Key 鉴权。 |
| `VRP_MAX_LOCATIONS` | `1000` | 仓库 + 停靠点数量上限。 |
| `VRP_MAX_VEHICLES` | `1000` | 车辆数量上限。 |
| `VRP_MAX_TIME_LIMIT_SECONDS` | `300` | 单次请求允许的最大求解时间。 |
| `VRP_MAX_JOBS` | `500` | 内存中最多保存的 Job 数。 |
| `VRP_JOB_TTL_SECONDS` | `3600` | 已完成 Job 的保留时间；0 表示查询时立即清理已完成 Job。 |
| `VRP_JOB_WORKERS` | `2` | 后台求解线程数。 |
| `VRP_OSRM_BASE_URL` | 未设置 | OSRM 服务基础 URL，如 `http://osrm:5000`。 |
| `VRP_OSRM_PROFILE` | `driving` | OSRM profile。 |
| `VRP_OSRM_TIMEOUT_SECONDS` | `15` | OSRM 请求超时。 |

## Docker

```bash
docker build -t ortools-vrp .
docker run --rm -p 8000:8000 \
  -e VRP_API_KEY=change-me \
  ortools-vrp
```

或：

```bash
docker compose up --build
```

`docker-compose.yml` 默认将容器 `8000` 端口映射到宿主机。生产环境建议在反向代理后启用 TLS，并限制请求体大小。

> 异步 Job 存储在本进程内。若使用多 worker / 多副本部署，Job 不会跨进程共享；需要跨进程任务时请使用外部队列或粘性路由。

## 示例文件

| 文件 | 说明 |
| --- | --- |
| `examples/cvrp.json` | 多车辆容量约束配送，haversine 矩阵。 |
| `examples/vrptw.json` | 时间窗、服务时长、车辆班次。 |
| `examples/pickup_delivery.json` | 带普通送货点的取送货混合问题。 |

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/solve \
  -H "Content-Type: application/json" \
  --data @examples/pickup_delivery.json | python -m json.tool
```

## 测试

```bash
pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
```

测试覆盖矩阵提供方、CVRP/VRPTW/取送货、时间窗与等待、技能、可选点、固定成本、多维容量、多仓库、不可行实例、HTTP 校验/鉴权/异步 Job 等。

## 设计说明与限制

- **单次访问**：每个 stop 在一次求解中最多服务一次；不支持多次访问或拆分需求。
- **单时间窗**：每个 stop 只支持一个时间窗；车辆班次也是一个连续窗口。
- **Shipment 必做**：shipment 不支持放弃；只能通过普通 stop 的 `drop_penalty` 表达可选点。
- **普通 stop 只表示送货**：纯取货或取送一体请使用 shipment。
- **线路闭合**：车辆必须从 `start_depot` 出发并回到 `end_depot`；不支持开放线路。每辆车（包括未使用的车）的空驶回路也受班次、最大时长/距离约束；如果车辆起终点不同且不需要使用时，建议放宽约束，或让起终点相同。
- **仓库不可途中访问**：仓库仅作为车辆的起点/终点。
- **固定矩阵**：距离和时长矩阵在求解期间不变；不支持时变交通、分时段速度或按车辆切换矩阵。矩阵值会四舍五入为整数米/秒。
- **无多行程 / 中途回仓**：一辆车只走一条从起点到终点的连续线路。
- **无休息/充电等复杂约束**：可通过时间窗和服务时长近似，未内置 break 约束。
- **容量维度**：最多 10 维；同一请求内所有 demand/capacity 的维度数必须一致（标量 0 会自动补零）。
- **目标取整**：内部成本乘以 1000 后取整，以兼顾精度和 OR-Tools 的 int64 成本。
- **技能约束**：如果某个必做 stop 没有任何车辆具备其所需技能，请求直接返回 422，而不是进入不可行求解；可选 stop 同样会在建模前报错。shipment 两端也必须存在共同可用车辆。
- **Job 存储**：内存实现，单进程；不支持取消正在执行的 OR-Tools 搜索。
- **OSRM**：请求只引用服务端配置的 OSRM，不接受任意 URL。大规模矩阵请评估 OSRM `table` 的规模限制。
- **求解时间**：OR-Tools 的时间上限是软限制，实际可能略超；长任务请使用异步 Job 或调大客户端超时。

## 性能建议

- 长耗时求解使用 `/api/v1/jobs`，避免 HTTP 连接超时。
- 大规模问题优先使用 `precomputed` 矩阵，避免每次请求重复计算或调用 OSRM。
- 在合理范围内调大 `time_limit_seconds` 通常能显著改善解质量；默认 5 秒适合中小规模。
- `GUIDED_LOCAL_SEARCH` 适合作为通用默认元启发式；可尝试 `SIMULATED_ANNEALING`、`TABU_SEARCH` 对比。
- 每个进程的后台求解并发由 `VRP_JOB_WORKERS` 控制；过多并发会争抢 CPU。生产环境建议结合 CPU 核数和问题规模调整。
- 请求中的点/车辆数受 `VRP_MAX_LOCATIONS`、`VRP_MAX_VEHICLES` 保护，避免单请求占用过多内存。
