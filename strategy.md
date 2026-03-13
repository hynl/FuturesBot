# V2.0 动态防御马丁格尔策略 — 完整技术文档

> **版本:** V2.0 · **最后更新:** 2026-03-13 · **交易对:** ETHUSDT (U本位合约)
>
> 本文档既是策略设计手册，也是代码审查对照标准。
> 所有参数均可通过 `config/settings.yaml` 热更新，无需重启。

---

## 目录

1. [策略概览与设计理念](#一策略概览与设计理念)
2. [全局配置与环境常量](#二全局配置与环境常量)
3. [核心算法模块](#三核心算法模块)
4. [状态机完整定义](#四状态机完整定义-4-状态-5-枚举)
5. [状态流转详解](#五状态流转详解)
6. [系统架构与并发模型](#六系统架构与并发模型-6-轨道)
7. [订单成交处理与均价同步](#七订单成交处理与均价同步)
8. [数据恢复与对账机制](#八启动对账与状态恢复)
9. [告警与监控体系](#九告警与监控体系)
10. [热更新机制](#十热更新机制)
11. [代码审查清单](#十一代码审查清单-checklist)
12. [数值示例](#十二数值示例-以-eth--250000-为例)

---

## 一、策略概览与设计理念

**一句话总结:** 在宏观上涨趋势中，利用 RSI 超卖信号入场做多，以 ATR 自适应网格逐层接单降低均价，通过追踪止盈 (TTP) 锁定利润，并在极端行情下自动触发深度休眠保护本金。

### 核心思路

```
上涨趋势 + 超卖回调 → 入场做多
      ↓ 价格继续下跌
  网格层层接单 (T1→T4)，摊低均价
      ↓ 价格反弹
  浮盈达标 → TTP 追踪猎杀 → 止盈平仓
      ↓ 极端情况：T4 也被击穿
  HIBERNATION 深度休眠 → 人工介入
```

### 关键设计原则

| 原则 | 实现方式 |
|------|----------|
| **不逆势** | 4H EMA200 趋势过滤，仅在 `price > EMA200` 时允许入场 |
| **自适应** | 首单面值随 ATR 动态缩放：波动大则仓位小，波动小则仓位大 |
| **防插针** | T4 击穿判定带 0.5% 缓冲，避免瞬间插针触发极端风控 |
| **时间惩罚** | 持仓超过 72 小时自动降低止盈门槛，加速脱手 |
| **状态持久化** | 每次状态变更自动写盘 (`state_backup.json`)，重启后可恢复 |
| **零权重监控** | 实时价格通过 @bookTicker WebSocket 获取，不消耗 API 权重 |

---

## 二、全局配置与环境常量

所有参数均通过配置文件读取，**禁止硬编码**。

### 配置文件结构

| 文件 | 用途 | 是否上传 Git |
|------|------|:---:|
| `config/secrets.env` | API 密钥、Telegram Token | ❌ |
| `config/settings.yaml` | 策略参数、运行参数 | ❌ |
| `config/settings.example.yaml` | 配置模板 | ✅ |

### 核心常量一览

| 参数 | 默认值 | 配置路径 | 说明 |
|------|--------|----------|------|
| 交易对 | `ETHUSDT` | `trade.symbol` | U本位合约 |
| 杠杆 | `5x` | `position.leverage` | 启动时通过 API 设定 |
| 持仓模式 | `one_way` | `position.mode` | 单向持仓 + 全仓保证金 |
| 运行资金 | `800 USDT` | `capital.running_pool_usdt` | 合约账户运行池 |
| 装死备用金 | `1700 USDT` | `capital.reserve_spot_usdt` | 现货账户，仅 HIBERNATION 时划入 |
| 基准首单面值 | `300 USDT` | `trade.base_notional` | 动态缩放基准值 |
| 基准波动率 | `15` | `dynamic_sizer.baseline_atr` | ETH 15m ATR 经验值 |
| K线周期 | `15m` | `runtime.kline_interval` | 主策略运行周期 |
| 趋势周期 | `4h` | `runtime.kline_interval_4h` | EMA200 计算周期 |

---

## 三、核心算法模块

> 代码位置: `strategy/eth_grid_ttp.py` — 均为纯函数，数据解耦

### 3.1 宏观趋势过滤器 (Trend Filter)

**目的:** 避免在下降趋势中做多，只在宏观多头环境入场。

```
输入:  当前实时价格 (curr_price)
数据:  4H K线 → 计算 EMA(200)，由 main.py 每 15 分钟注入
判定:  curr_price > 4H_EMA200 → 返回 True (允许入场)
       curr_price ≤ 4H_EMA200 → 返回 False (禁止入场)
```

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 是否启用 | `true` | `trend_filter.enabled` |
| EMA 周期 | `200` | `trend_filter.ema_period` |
| 4H 数据量 | `210` 根 | `runtime.ohlcv_limit_4h` |

**特殊处理:**
- 启动时预热：首次拉取 210 根 4H K线计算 EMA200
- EMA 未就绪时（如启动瞬间）：返回 `False`，拒绝入场，**宁可错过不冒进**
- 由 `trend_update_worker` 每 15 分钟刷新一次

### 3.2 动态首单计算器 (Volatility-Scaled Sizer)

**目的:** 根据当前市场波动率自动调整首单大小。ATR 越大（越剧烈），首单越小；ATR 越小（越平静），首单越大。

```
公式:  动态首单面值 = 基准首单面值 × (基准波动率常数 / 实时ATR)
风控:  强制约束结果在 [min_notional, max_notional] 范围内
```

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 基准首单面值 | `300 USDT` | `trade.base_notional` |
| 基准波动率 | `15` | `dynamic_sizer.baseline_atr` |
| 面值下限 | `150 USDT` | `dynamic_sizer.min_notional` |
| 面值上限 | `450 USDT` | `dynamic_sizer.max_notional` |

**数值举例:**

| 实时 ATR | 原始计算 | Clamp 后 | 含义 |
|----------|----------|----------|------|
| `10` (低波动) | 300×(15/10) = `450` | `450` | 市场平静，满额出手 |
| `15` (标准) | 300×(15/15) = `300` | `300` | 正常水平 |
| `30` (高波动) | 300×(15/30) = `150` | `150` | 市场剧烈，最小仓位 |
| `5` (极低波动) | 300×(15/5) = `900` | `450` | 触碰上限，截断 |
| `60` (极端行情) | 300×(15/60) = `75` | `150` | 触碰下限，截断 |

### 3.3 TTP 时间衰减激活线

**目的:** 持仓时间越长，止盈门槛越低，避免深套仓位永远无法出局。

```
如果 持仓时长 ≤ 72小时:  激活线 = 1.5% (常规)
如果 持仓时长 > 72小时:  激活线 = 0.2% (衰减)
```

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 常规激活线 | `1.5%` | `ttp.activation_profit_pct` |
| 衰减阈值 | `72h` | `ttp.time_decay_hours` |
| 衰减后激活线 | `0.2%` | `ttp.time_decay_profit_pct` |

---

## 四、状态机完整定义 (4 状态 + 5 枚举)

> 代码位置: `core/position_manager.py` → `TradeState` 枚举

```
┌──────────────────────────────────────────────────────────────┐
│                    状态机流转总览                              │
│                                                              │
│  ┌─────────┐   RSI超卖    ┌─────────────┐                    │
│  │ HUNTING │ ──────────→  │ ENTRY_      │                    │
│  │ State 0 │  + 趋势通过  │ SUBMITTING  │ (过渡态)            │
│  └────▲────┘              └──────┬──────┘                    │
│       │                         │ 挂单完成                    │
│       │                         ▼                            │
│       │                  ┌─────────────┐    T4击穿    ┌──────────────┐
│       │                  │ GRID_ACTIVE │ ──────────→  │ HIBERNATION  │
│       │                  │   State 1   │  + 0.5%缓冲  │   State 3    │
│       │                  └──────┬──────┘              └──────────────┘
│       │                         │ 浮盈达标                    │
│       │                         ▼                    死循环等待 │
│       │                  ┌─────────────┐             人工重启   │
│       │   止盈平仓       │  TTP_ARMED  │                       │
│       └───────────────── │   State 2   │                       │
│                          └─────────────┘                       │
└──────────────────────────────────────────────────────────────┘
```

### 枚举定义

| 枚举值 | 中文名 | 含义 |
|--------|--------|------|
| `HUNTING` | 寻猎模式 | State 0: 空仓，等待入场信号 |
| `ENTRY_SUBMITTING` | 进场下单中 | 过渡态: 首单已提交，网格挂单进行中 |
| `GRID_ACTIVE` | 网格防御中 | State 1: 持仓 + 网格挂单活跃 |
| `TTP_ARMED` | 追踪猎杀 | State 2: 追踪止盈已激活，监控最高水位 |
| `HIBERNATION` | 深度休眠 | State 3: 极限风控锁定，仅维持心跳 |

### 内存状态 (`SessionState`)

每次状态变更时自动调用 `save_to_disk()` 写入 `state_backup.json`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | str | 交易对 |
| `state` | TradeState | 当前状态枚举 |
| `entry_price` | float | 首单成交价 |
| `avg_price` | float | 整体持仓均价 (API 同步) |
| `total_amount` | float | 总持仓数量 (币) |
| `entry_timestamp` | float | 入场时间戳 (72h 衰减用) |
| `snapshot_atr` | float | 入场快照 ATR (网格定价用) |
| `dynamic_base_volume` | float | 本轮动态首单面值 (USDT) |
| `highest_price` | float | TTP_ARMED 后的最高水位线 |
| `active_grids` | List[GridOrder] | 活跃网格列表 (含层级、价格、数量、成交状态) |

---

## 五、状态流转详解

### State 0: HUNTING → 寻猎模式

**前提条件:** 账户无持仓，无未成交挂单。

**触发时机:** 每根 15m K线闭合时检查。

**入场条件 (AND 逻辑，缺一不可):**

| # | 条件 | 数据源 | 代码 |
|---|------|--------|------|
| 1 | 趋势过滤通过 | `curr_price > 4H_EMA200` | `check_trend_filter()` |
| 2 | RSI 超卖 | `15m RSI(14) < 40` | `curr_rsi < self.rsi_oversold` |

**两个条件都满足时，执行入场动作 → 流转到 State 1**

---

### State 0 → State 1: 入场执行 (`execute_entry`)

按严格顺序执行以下步骤:

#### Step 1: 计算动态首单面值
```
动态面值 = calc_dynamic_base_volume(当前ATR)
首单数量 = 动态面值 / 当前价格
```

#### Step 2: 市价买入
```
调用 exchange.create_market_order(symbol, 'buy', 首单数量)
记录: fill_price (实际成交均价), fill_qty (实际成交量)
```
- 精度处理: 自动应用 `amount_to_precision()` (tickSize/stepSize 向下取整)
- 失败处理: 下单异常则 **不进入** GRID_ACTIVE，保持 HUNTING

#### Step 3: 快照存储
```
entry_price     = fill_price      # 首单成交价
avg_price       = fill_price      # 初始均价 = 首单价
total_amount    = fill_qty        # 总持仓量
entry_timestamp = time.time()     # 入场时间戳 (72h 衰减基准)
snapshot_atr    = 当前ATR          # 快照 ATR (后续网格定价全部基于此值)
state           = ENTRY_SUBMITTING # 过渡态
```

#### Step 4: 挂出 4 层网格限价单

以 **入场成交价** 和 **快照 ATR** 为基准，挂出 4 笔限价买单:

| 层级 | 挂单价格公式 | 默认间距系数 | 数量公式 | 默认数量倍数 |
|------|-------------|:---:|---------|:---:|
| **T1** | `fill_price - 1.0 × ATR` | `1.0` | `fill_qty × multiplier[0]` | `1.5` |
| **T2** | `fill_price - 1.8 × ATR` | `1.8` | `fill_qty × multiplier[1]` | `2.25` |
| **T3** | `fill_price - 3.0 × ATR` | `3.0` | `fill_qty × multiplier[2]` | `3.375` |
| **T4** | `fill_price - 5.0 × ATR` | `5.0` | `fill_qty × multiplier[3]` | `5.0625` |


> **注意:** 间距系数 `grid_ratios` 和数量倍数 `grid_multipliers` 均可通过 `settings.yaml` 自定义。
> 默认 `multipliers = [1.5^1, 1.5^2, 1.5^3, 1.5^4]`，即每层面值是首单的几何递增。

#### Step 5: 完成
```
state = GRID_ACTIVE              # 正式进入网格防御
save_to_disk()                   # 持久化
Telegram 推送入场 + 网格挂单消息
```

**配置参数:**

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 间距系数 | `[1.0, 1.8, 3.0, 5.0]` | `grid.ratios` |
| 数量倍数 | `[1.5, 2.25, 3.375, 5.0625]` | `grid.multipliers` |
| 递增乘数 | `1.5` | `safety.volume_multiplier` |
| 最大网格层数 | `4` | `safety.max_trades` |

---

### State 1: GRID_ACTIVE → 网格防御与监控

**双重价格源:**
- **K线闭合:** 每根 15m K线闭合时调用一次 `check_grid_and_ttp(close_price)`
- **实时 BookTicker:** @bookTicker WebSocket 推送 `(best_bid + best_ask) / 2` 每笔更新都调用

**每次收到价格更新时，按优先级依次检查:**

#### ① 检查 T4 击穿 (最高优先级)

```
T4击穿线 = T4挂单价格 × (1 - 0.5%)

如果 curr_price < T4击穿线:
    → 触发 HIBERNATION (State 3)
```

> 0.5% 缓冲是为了防止币安常见的瞬间插针导致误判。

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| T4 缓冲百分比 | `0.5%` | `t4_breach.buffer_pct` |

#### ② 检查 TTP 激活 (浮盈达标)

```
浮盈百分比 = (curr_price - avg_price) / avg_price × 100%

有效激活线 = 
    1.5%   (持仓 ≤ 72小时)
    0.2%   (持仓 > 72小时，时间衰减)

如果 浮盈百分比 ≥ 有效激活线:
    highest_price = curr_price   # 初始化最高水位
    state → TTP_ARMED (State 2)
```

**这里体现了条件 A 和条件 B 的统一:**
- **条件 A (常规):** 持仓 ≤ 72h → 浮盈需达到 `avg_price × 1.015`
- **条件 B (衰减):** 持仓 > 72h → 浮盈仅需达到 `avg_price × 1.002`

---

### State 2: TTP_ARMED → 追踪猎杀

**目的:** 在确认盈利后，不立即平仓，而是追踪价格上涨获取更多利润。仅当价格从最高点回撤超过阈值时才执行平仓。

**每次收到价格更新时:**

```
如果 curr_price > highest_price:
    highest_price = curr_price       # 更新最高水位

trailing_stop = highest_price × (1 - 0.3%)

如果 curr_price ≤ trailing_stop:
    → 触发猎杀，执行止盈平仓
```

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 回撤止盈线 | `0.3%` | `ttp.trailing_loss_pct` |

**图解 TTP 追踪过程:**
```
价格 ↑
  │         ╱╲
  │        ╱  ╲ ← highest_price (最高水位线)
  │       ╱    ╲
  │      ╱      ╲───── trailing_stop = highest × 0.997
  │     ╱        ╲
  │    ╱          ● ← 价格跌破 trailing_stop → 触发止盈!
  │   ╱
  │  ● ← TTP_ARMED 激活点 (浮盈 ≥ 1.5%)
  │ ╱
  │╱
  └──────────────────────── 时间 →
```

### State 2 → State 0: 止盈平仓 (`execute_take_profit`)

**按严格顺序执行:**

| 步骤 | 动作 | 失败处理 |
|------|------|----------|
| 1 | Telegram 推送止盈消息 (含 P&L) | — |
| 2 | 市价卖出 100% 仓位 | 失败则 **不清状态**，下次循环重试 |
| 3 | **【必选】** 撤销所有未成交限价挂单 | 记录错误日志 |
| 4 | `state.reset()` → 回到 HUNTING | — |
| 5 | `save_to_disk()` 持久化 | — |

---

### State 3: HIBERNATION → 深度休眠

**触发条件:** T4 击穿 — `curr_price < T4_price × (1 - 0.5%)`

**这是系统的极限风控机制，按严格顺序执行且不可逆:**

| 步骤 | 动作 | 说明 |
|:---:|------|------|
| **1/5** | `_is_frozen = True` | 冻结一切策略计算，拒绝所有 K线/价格回调 |
| | `state = HIBERNATION` | 状态切换并立即写盘 |
| **2/5** | `transfer(1700 USDT, spot→future)` | 将现货备用金划转至合约账户，注入保证金 |
| | | 划转失败不影响后续步骤 (记录日志继续) |
| **3/5** | `cancel_all_orders(symbol)` | 撤销所有剩余挂单 |
| **4/5** | `CRITICAL` 级别日志 + Telegram 立即推送 | 使用 `send_now()` 确保立即发出 |
| **5/5** | 通知 main.py 进入死循环 | `await asyncio.sleep(86400)` 每 24h 输出一次心跳 |

**死循环行为:**
- 取消所有 6 个 worker 任务
- 仅保留心跳日志: `💀 HIBERNATION 心跳: 进程存活, 等待人工介入...`
- **必须人工重启进程才能恢复交易**

---

## 六、系统架构与并发模型 (6 轨道)

> 代码位置: `main.py` → `TradingBot.run()`

系统采用 `asyncio` 协程并发，启动后同时运行 6 个独立 worker:

```
┌──────────────────────────────────────────────────────────────┐
│                     main.py (TradingBot)                     │
│                                                              │
│  ┌─────────────────┐  ┌─────────────────┐                    │
│  │ 轨道1: K线闭合   │  │ 轨道2: BookTicker│                    │
│  │ @kline_15m WS   │  │ @bookTicker WS  │                    │
│  │ → on_kline_closed│  │ → check_grid_ttp│                    │
│  └────────┬────────┘  └────────┬────────┘                    │
│           │                    │                             │
│           ▼                    ▼                             │
│  ┌──────────────────────────────────────┐                    │
│  │      strategy/eth_grid_ttp.py        │                    │
│  │      EthGridStrategy (状态机)         │                    │
│  └──────────────────────────────────────┘                    │
│           ▲                    ▲                             │
│           │                    │                             │
│  ┌────────┴────────┐  ┌───────┴─────────┐                    │
│  │ 轨道4: User      │  │ 轨道6: 趋势更新  │                    │
│  │ Stream WS       │  │ 4H EMA200       │                    │
│  │ → on_order_update│  │ 每15min刷新      │                    │
│  └─────────────────┘  └─────────────────┘                    │
│                                                              │
│  ┌─────────────────┐  ┌─────────────────┐                    │
│  │ 轨道3: 看门狗    │  │ 轨道5: 配置监听  │                    │
│  │ Watchdog        │  │ settings.yaml   │                    │
│  │ 每30s巡查一次    │  │ 每5s检查变更     │                    │
│  └─────────────────┘  └─────────────────┘                    │
└──────────────────────────────────────────────────────────────┘
```

### 各轨道详解

| 轨道 | Worker | 数据源 | 职责 |
|:---:|--------|--------|------|
| **1** | `kline_closed_worker` | Binance K线 WS (`@kline_15m`) | 15m K线闭合时拉取历史数据，计算 RSI/ATR，触发入场判断 |
| **2** | `real_time_price_worker` | Binance BookTicker WS (`@bookTicker`) | 毫秒级实时价格推送，驱动 TTP 追踪 / T4 击穿检测 |
| **3** | `monitor_health` | 定时巡检 (30s) | 看门狗：检测 WS 假死，超时后强制断开重连 + Telegram 告警 |
| **4** | `user_stream_worker` | Binance User Data Stream | 监听订单成交回调，同步均价，标记网格成交 |
| **5** | `config_watcher_worker` | `settings.yaml` 文件监听 (5s) | 配置文件变更时触发策略参数热更新 |
| **6** | `trend_update_worker` | REST API (15min) | 拉取 4H K线计算 EMA200，注入策略趋势过滤器 |

### 启动顺序

```
1. 初始化交易所连接 (加载市场、设置杠杆/持仓模式)
2. 对账: 本地状态 vs 交易所实际仓位
3. 预热: 首次拉取 4H EMA200
4. 启动 Telegram 后台发送 worker
5. 并发启动 6 个 worker (asyncio.create_task)
6. asyncio.wait(FIRST_COMPLETED) — 任何 worker 异常退出触发全局停机
```

---

## 七、订单成交处理与均价同步

> 代码位置: `strategy/eth_grid_ttp.py` → `on_order_update()`

当 User Data Stream 推送 `ORDER_TRADE_UPDATE` (状态为 `FILLED`) 时:

### 处理流程

```
1. 解析成交信息: side, price, qty, timestamp
2. 记录交易到 metrics (P&L 统计)
3. 均价同步 (见下方)
4. 更新总持仓量
5. 标记对应网格单为已成交 (价格偏差 < 0.5% 即匹配)
6. save_to_disk() 持久化
```

### 均价同步规则 (审查清单 #3)

```
优先: 调用 API 获取 position['entryPrice'] → 作为 avg_price
降级: API 失败时才用本地公式 (P1×V1 + P2×V2) / (V1+V2)
```

**这是策略文档的硬性要求** — 必须使用 API 实时均价，不能依赖本地公式（部分成交场景下本地公式会累积误差）。

---

## 八、启动对账与状态恢复

> 代码位置: `main.py` → `init_state_and_sync()`

系统重启时，需要将本地状态与交易所实际仓位对账:

| 场景 | 本地备份 | 交易所仓位 | 处理 |
|------|:---:|:---:|------|
| 正常恢复 | ✅ 有 | ✅ 有 | 加载本地状态，继续运行 |
| 本地丢失 | ❌ 无 | ✅ 有 | 逆向恢复: 设为 GRID_ACTIVE，从 API 读取均价和持仓量 |
| 已平仓 | ✅ 有 (非HUNTING) | ❌ 无 | 状态重置为 HUNTING |
| 全新启动 | ❌ 无 | ❌ 无 | 正常 HUNTING |

---

## 九、告警与监控体系

### Telegram 异步告警

> 代码位置: `utils/telegram_bot.py`

| 事件 | 级别 | 发送方式 |
|------|------|----------|
| 入场信号 | INFO | 队列异步 (`send`) |
| 网格挂单 | INFO | 队列异步 |
| TTP 激活 | WARNING | 队列异步 |
| 止盈平仓 (含 P&L) | SUCCESS | 队列异步 |
| 看门狗假死 | WARNING | 队列异步 |
| **HIBERNATION** | **CRITICAL** | **立即发送** (`send_now`) |

**架构特点:**
- 消息队列 (maxsize=200)，满队时丢弃最旧消息
- 限流: 最快每秒 1 条 (符合 Telegram Bot API 限制)
- 异步后台 worker，不阻塞策略主逻辑

### 看门狗 (Watchdog)

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 无数据超时 | `60s` | `health.timeout_seconds` |
| 巡查间隔 | `30s` | `health.check_seconds` |

**自愈逻辑:**
```
超时 → 记录 CRITICAL 日志 → Telegram 告警 → 强制断开 K线/BookTicker WS
      → WS 内部重连机制自动恢复连接 → 重置超时计时器
```

### 交易指标采集

> 代码位置: `utils/metrics.py`

- 每笔成交自动记录到 `metrics.jsonl` (JSONL 格式)
- 统计: 总交易数、买卖单数、均买价/均卖价、已实现 P&L
- 查看工具: `python3 check_metrics.py ETHUSDT`

---

## 十、热更新机制

> 代码位置: `utils/config_watcher.py`

修改 `config/settings.yaml` 后，无需重启即可生效:

```
文件修改 → ConfigWatcher 检测 (5s轮询)
         → 解析新 YAML
         → 回调 strategy.update_parameters()
         → 策略参数即时更新
```

**可热更新的参数:**

| 模块 | 参数 |
|------|------|
| RSI | `period`, `oversold` |
| ATR | `period` |
| 动态仓位 | `baseline_atr`, `min_notional`, `max_notional` |
| 网格 | `ratios`, `multipliers`, `volume_multiplier`, `max_trades` |
| TTP | `activation_profit_pct`, `trailing_loss_pct`, `time_decay_hours`, `time_decay_profit_pct` |
| T4 | `buffer_pct` |

> ⚠️ **注意:** 热更新仅影响**下一轮**入场。已在 GRID_ACTIVE 的仓位使用的是入场时的快照参数。

---

## 十一、代码审查清单 (Checklist)

以下 4 项均已在代码中实现，审查时需逐一验证:

### ✅ #1 精度异常拦截 (Filter Failures)

| 检查点 | 实现 |
|--------|------|
| `create_order` 前是否对 price 做了精度处理? | ✅ `exchange.price_to_precision()` |
| `create_order` 前是否对 amount 做了精度处理? | ✅ `exchange.amount_to_precision()` |
| 是否使用交易所的 tickSize/stepSize? | ✅ ccxt 内置 `market['precision']` |

> 代码位置: `core/exchange.py` → `create_limit_order()` / `create_market_order()`

### ✅ #2 WebSocket 断流假死 (Connection Drops)

| 检查点 | 实现 |
|--------|------|
| 是否有 Ping/Pong 心跳? | ✅ `aiohttp ws_connect(heartbeat=20)` |
| 是否有超时检测? | ✅ `asyncio.wait_for(ws.receive(), timeout=N)` |
| 超时后是否自动重连? | ✅ `while is_running` 外层循环 + `break` 后重连 |
| 是否有看门狗兜底? | ✅ `monitor_health` 轨道 30s 巡查 |

> 代码位置: 所有 3 个 WS 类 (`binance_ws_server.py`, `binance_user_server.py`, `binance_bookticker_ws.py`)

### ✅ #3 部分成交污染 (Partial Fills)

| 检查点 | 实现 |
|--------|------|
| 均价计算是否依赖本地公式? | ❌ 优先使用 API |
| 是否调用 API 获取 entryPrice? | ✅ `exchange.get_api_entry_price()` |
| API 失败时有降级方案吗? | ✅ 降级到本地公式 + 日志警告 |

> 代码位置: `strategy/eth_grid_ttp.py` → `on_order_update()` 第 346-358 行

### ✅ #4 死锁与权重超限 (Rate Limit)

| 检查点 | 实现 |
|--------|------|
| 实时价格是否使用 REST 轮询? | ❌ 已弃用 |
| 是否使用 @bookTicker WS? | ✅ `BinanceBookTickerWS` |
| 是否有 `time.sleep()` 阻塞? | ❌ 全部使用 `asyncio.sleep()` |
| REST 调用是否带自动重试和退避? | ✅ `@retry_on_failure` 装饰器，指数退避 |

> 代码位置: `core/websocket/binance_bookticker_ws.py`, `core/exchange.py`

---

## 十二、数值示例 (以 ETH = 2500.00 为例)

### 场景: 正常入场 → 网格接单 → TTP 止盈

**条件:** 4H EMA200 = 2400，当前价 = 2500 (趋势通过)，15m RSI = 38 (超卖)，15m ATR = 15

#### Step 1: 入场

```
动态首单面值 = 300 × (15/15) = 300 USDT
首单数量 = 300 / 2500 = 0.1200 ETH
→ 市价买入 0.1200 ETH @ 2500.00
→ snapshot_atr = 15
```

#### Step 2: 网格挂单

| 层级 | 价格 | 数量 | 面值 (USDT) |
|------|------|------|-------------|
| T0 (首单) | 2500.00 | 0.1200 | 300 |
| T1 | 2500 - 1.0×15 = **2485.00** | 0.1200 × 1.5 = **0.1800** | ≈ 448 |
| T2 | 2500 - 1.8×15 = **2473.00** | 0.1200 × 2.25 = **0.2700** | ≈ 668 |
| T3 | 2500 - 3.0×15 = **2455.00** | 0.1200 × 3.375 = **0.4050** | ≈ 995 |
| T4 | 2500 - 5.0×15 = **2425.00** | 0.1200 × 5.0625 = **0.6075** | ≈ 1473 |

#### 场景 A: T1 成交后反弹止盈

```
T1 成交 @ 2485.00, 数量 0.1800
API 返回均价: avg_price = 2494.00 (交易所精确计算)
总持仓: 0.1200 + 0.1800 = 0.3000 ETH

等待反弹...
当价格 ≥ 2494.00 × 1.015 = 2531.41 时 → TTP_ARMED
假设价格涨到 2540.00 → highest = 2540.00
trailing_stop = 2540.00 × 0.997 = 2532.38
价格回落至 2532.38 → 触发止盈!
→ 市价卖出 0.3000 ETH
→ 撤销 T2/T3/T4 挂单
→ P&L ≈ (2532.38 - 2494.00) × 0.3000 = +11.51 USDT
→ 回到 HUNTING
```

#### 场景 B: T4 击穿触发 HIBERNATION

```
所有网格被击穿，价格继续下跌
T4 击穿线 = 2425.00 × 0.995 = 2412.88
当价格 < 2412.88 → HIBERNATION!
→ Step 1: 冻结策略
→ Step 2: 划转 1700 USDT (Spot → Futures)
→ Step 3: 撤销所有挂单
→ Step 4: CRITICAL 告警 + Telegram
→ Step 5: 死循环等待人工介入
```

---

## 附录: 文件与代码对照表

| 文件 | 对应文档章节 |
|------|-------------|
| `strategy/eth_grid_ttp.py` | 三 (算法模块) + 五 (状态流转) + 七 (订单处理) |
| `core/position_manager.py` | 四 (状态机定义) |
| `core/exchange.py` | 十一 #1 (精度处理) + #4 (重试机制) |
| `core/websocket/binance_ws_server.py` | 六 轨道1 + 十一 #2 (断流检测) |
| `core/websocket/binance_bookticker_ws.py` | 六 轨道2 + 十一 #4 (零权重) |
| `core/websocket/binance_user_server.py` | 六 轨道4 + 七 (订单回调) |
| `main.py` | 六 (并发模型) + 八 (对账) |
| `utils/telegram_bot.py` | 九 (Telegram 告警) |
| `utils/config_watcher.py` | 十 (热更新) |
| `utils/metrics.py` | 九 (指标采集) |
| `config/settings.yaml` | 二 (全局配置) |
