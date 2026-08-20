# 工作记录 — 单例服务的压力评估

> 分析记录（针对「对于多节点集群.md」中标记为**必须 replicas=1** 的服务，评估其运行/并发压力，供后续扩容与性能优化参考）。原「询问挂起/恢复」实现记录已由本次覆盖。

## 关键发现：事件总线为全量广播

`task-scheduler-service/app/event_bus.py` 的 `publish()` 对每个事件**遍历全部订阅者**做频道过滤后入队：

```python
def publish(self, event: dict):
    for subscriber_id, (channels, q) in items:      # O(订阅者)
        if channels and event_channel not in channels:
            continue
        q.put(event)                                 # 命中频道才入队
```

一轮对话至少产生 `task_queued → task_started → assistant_message×N → task_completed` 多个事件，每个事件成本 = O(订阅者数)。**订阅者越多，单事件广播成本线性上涨**——这是 scheduler 的首要瓶颈。

## 压力评级总览

| 服务 | 压力 | 主要压力点 |
|------|------|-----------|
| task-scheduler-service | ⭐⭐⭐⭐⭐ | 全局唯一任务入口 + 事件全量广播 + 内存态队列 + Python GIL |
| openviking-server | ⭐⭐⭐⭐ | 每轮对话 SearchContext/AppendTurn + embedding 计算密集 + `workers:1` |
| qq-llbot-service | ⭐⭐⭐ | 单 Satori 连接上限 + 图片下载转 base64 |
| timer-task-service | ⭐⭐ | 日常 IO 轮询轻量，整点批量触发时脉冲式压力（传导给 scheduler） |

## 逐项分析

### ⭐⭐⭐⭐⭐ task-scheduler-service

- **任务入口中心**：Web + QQ 所有消息先 `CreateTask`，全局唯一入口
- **事件广播放大**：`publish` 对每个事件 O(订阅者) 广播（代码已核实），订阅者含 gateway / qq-llbot
- **内存态队列**：用户队列 + 订阅者队列全在进程内，高并发下内存/GC 压力
- **Python GIL**：调度循环、广播、gRPC 处理共享 GIL，无法并行榨 CPU

**结论**：全系统最需要关注的单例瓶颈；消息量上来后事件广播放大最先触顶。

### ⭐⭐⭐⭐ openviking-server

- 每轮对话都调用（orchestrator 的 SearchContext + AppendTurn）
- embedding 语义向量化调宿主机 Ollama（`host.docker.internal:11434`），单点且并发有限
- **配置瓶颈**：`config/openviking/ov.conf` 中 `server.workers: 1`，单 worker 串行处理 HTTP——最容易调的参数（调大即可缓解）

### ⭐⭐⭐ qq-llbot-service

- 单 Satori WebSocket 连接，吞吐受 QQ 机器人本身限制
- 主要开销：多群 @消息 + 私聊汇聚、图片下载转 base64（每条带图消息 HTTP 抓图再转码）
- 事件订阅转发本身轻

### ⭐⭐ timer-task-service

- 扫描线程轮询 NFS 任务目录（5s 快扫 / 60s 慢扫），IO 密集、日常压力小
- 突发场景：大量任务同一时刻到期（整点批量提醒 / 集中 commit）→ 瞬间并发回调 scheduler，形成脉冲式压力（压力传导给 scheduler）

## 应对方向

| 服务 | 短期（零改造） | 中期（需改造） |
|------|---------------|---------------|
| task-scheduler | 给足 CPU/内存；监控队列长度与事件延迟；减少不必要订阅 | 事件总线外置（Redis pub/sub）支持多副本；或广播改按订阅者批量推送 |
| openviking-server | **ov.conf 的 `workers` 调大**（最易）；Ollama embedding 独立节点 | 独立向量库节点 |
| qq-llbot | 图片结果缓存 | 多 bot 分群（换 subscriber_id 分频道） |
| timer-task | 分散任务触发时间 | — |

**最需盯防**：task-scheduler（事件放大）与 openviking-server（workers=1 + embedding 单点）。

---

# 远期扩容计划（当前无扩容需求，仅为后续开发预留方向）

> 目标：后续开发新功能时**不因单例/有状态设计受限**。各「建议 replicas=1」服务的扩容路径分析，暂不落地。

## 一、gateway-backend-service — 最容易扩容（几乎零代码改动）

**现状（已核实）**：`SUBSCRIBER_ID` 来自环境变量（默认 `web-gateway-1`）；`sse_hub.py` 的 `publish()` 按 user_id 查本地队列，**无该用户连接则直接丢弃**（天然本地过滤）。

**方案**：多副本 + 每副本唯一 `SUBSCRIBER_ID` + 本地过滤。

- scheduler 的 `event_bus.publish` 本就是全量广播，各副本都收到事件
- 各副本 SSEHub 只推「连在本副本」的用户，其余丢弃 → **不重复、不丢失**
- 前端 Service 轮询负载均衡即可（用户建立 SSE 后固定在某副本）

**硬性要求**：`SUBSCRIBER_ID` 每副本唯一（event_bus 按 subscriber_id 存队列，重复会互相覆盖）。部署模板用 `SUBSCRIBER_ID=web-gateway-$(POD_NAME)`。

## 二、agent-orchestrator-service — 会话一致性哈希路由

**瓶颈状态**：`default_agent[session_id]`（跨任务会话状态）、`PENDING_TASKS`（询问挂起注册表）在进程内。

**方案 A（推荐）**：scheduler 的 `orchestrator_client` 按 `hash(user_id + session_id) % 副本数` 选地址。

- 同一会话的 ExecuteTask / ResumeTask 永远到同一副本 → 状态一致
- 不同会话分布到不同副本 → 水平扩展
- 实现：orchestrator 改 headless Service + scheduler 侧一致性哈希（TaskRuntime 是任务级、创建于 ExecuteTask 时，无需改造）

**方案 B（远期）**：状态外置——`default_agent` 存 Redis；`PENDING_TASKS` 挂起任务状态持久化 NFS + 恢复时重建。

## 三、tool-runtime-service — 按用户哈希路由

- workspace / PROCESS 已按用户分目录/分文件 → 同一用户工具执行路由到固定副本即可（同用户串行、跨用户并行）
- **难点**：端口代理（`port-expose` 5800-5899）为副本内监听，多副本端口重叠 → 需副本级端口段分配，或改集群级统一入口代理

## 四、user-service — per-user 分片

- 按 `user_id` 哈希分片（每副本管一部分用户，互不写同一文件）
- 或加 per-user 文件锁后放开多副本

## 五、后续开发的约束清单（避免受限）

| 服务 | 开发时必须保持的设计 |
|------|----------------------|
| gateway-backend | ① `SUBSCRIBER_ID` 保持环境变量可配置（勿硬编码唯一 ID）② 事件推送保持「本地按 user_id 过滤」模式（新增推送逻辑勿做全量推/勿引入副本间直接耦合）③ SSE 连接注册表（sse_hub）留在进程内即可，勿设计成跨副本共享 |
| agent-orchestrator | ① 新增跨请求状态时，必须带 session_id 可定位（保持能按会话路由的 key 结构）② 挂起注册表（PENDING_TASKS）的 key 保持 `task_id → 会话信息` 可恢复结构 ③ 勿在模块级持有「所有用户」的全局可变状态 |
| scheduler | ① 事件总线广播是单例瓶颈：新增事件类型时考虑广播成本（每事件 O(订阅者)）② 勿给 scheduler 增加更多进程内全局状态（队列/注册表已足够多）③ 新订阅者机制保持 subscriber_id 语义 |
| tool-runtime | ① workspace / PROCESS 写入保持按用户隔离（勿引入跨用户共享写）② 端口代理状态若扩展，设计为可分配/可路由 |
| user-service | 用户文件保持 per-user 单文件粒度（勿引入跨用户共享文件） |
