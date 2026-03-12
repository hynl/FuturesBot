
# V2.0 动态防御马丁格尔策略 (Agent 代码审查标准)

## 一、 全局配置与环境常量 (Global Configurations)

Agent 需检查配置模块是否硬编码，要求以下参数通过配置文件读取：

* **交易对 (Symbol):** `ETHUSDT`
* **杠杆倍数 (Leverage):** `5` (需代码验证并调用 API 设定)
* **持仓模式 (Margin Mode):** 单向持仓 (One-Way) / 全仓模式 (Cross Margin)
* **基础运行资金 (Base Capital):** `800 USDT`
* **装死备用金 (Reserve Capital):** `1700 USDT` (留存现货账户)
* **基准波动率常数 (Baseline ATR):** 设定 ETH 15 分钟标准 ATR 经验值为 `15` (用于计算动态仓位)。
* **基准首单面值 (Standard Base Volume):** `300 USDT`

---

## 二、 核心算法模块规范 (Algorithm Modules)

Agent 需检查项目中是否独立实现了以下纯函数/模块，要求数据解耦：

### 1. 宏观趋势过滤器 (Trend Filter)

* **输入:** 4 小时级别 K 线数据 (`kline_4h`)。
* **计算:** `EMA(200)`。
* **输出/判断:** 返回 `True` 当且仅当 `当前实时价格 > 4H_EMA200`。

### 2. 动态首单计算器 (Volatility-Scaled Sizer)

* **输入:** 实时 15 分钟 `ATR(14)` 数值。
* **计算公式:** `动态首单面值 = 基准首单面值 * (基准波动率常数 / 实时 ATR)`
*(逻辑：ATR 越大，波动越剧烈，首单越小)*
* **风控约束 (Clamp):** 无论计算结果如何，强制约束 `动态首单面值` 的范围在 `[150 USDT, 450 USDT]` 之间。

---

## 三、 状态机流转与执行引擎 (State Machine Engine)

Agent 需严格审查主循环 (Event Loop)，确保代码必须且只能处于以下 4 个枚举状态之一，且状态切换严格遵循条件：

### State 0: HUNTING (寻猎模式)

* **前提条件:** 账户 `ETHUSDT` 持仓量为 0，且无未成交挂单。
* **监听条件 (AND 逻辑):**
1. 趋势过滤通过 (`Current_Price > 4H_EMA200`)。
2. 超卖信号触发 (`15m_RSI(14) < 40`)。


* **执行动作:**
1. 调用 **[动态首单计算器]** 确定本轮 `Base_Volume`。
2. 发送市价买入订单 (Market Buy)，面值为 `Base_Volume`。
3. 快照当前的 15 分钟 ATR 数值，存入内存，记为 `Snapshot_ATR`。
4. 流转至 **State 1 (GRID_ACTIVE)**。



### State 1: GRID_ACTIVE (网格防御与时间熔断)

* **进入动作 (入场挂单):** 根据 `Snapshot_ATR`，调用 API 挂出 4 笔限价买单 (Limit Buy)。
* T1挂单价格: `首单均价 - (1.0 * Snapshot_ATR)` | 面值: `Base_Volume * 1.5`
* T2挂单价格: `首单均价 - (1.8 * Snapshot_ATR)` | 面值: `T1_Volume * 1.5`
* T3挂单价格: `首单均价 - (3.0 * Snapshot_ATR)` | 面值: `T2_Volume * 1.5`
* T4挂单价格: `首单均价 - (5.0 * Snapshot_ATR)` | 面值: `T3_Volume * 1.5`


* **循环监听与流转条件:**
* **更新均价:** 任何底层网格成交（含部分成交），必须通过 API 同步最新 `Average_Price`。
* **条件 A (常规止盈激活):** 如果持仓时间 `<= 72小时` 且 `Current_Price >= Average_Price * 1.015`。流转至 **State 2 (TTP_ARMED)**。
* **条件 B (时间惩罚止盈/Time Decay):** 如果持仓时间 `> 72小时` 且 `Current_Price >= Average_Price * 1.002` (下调激活线至 0.2%)。流转至 **State 2 (TTP_ARMED)**。
* **条件 C (防线击穿):** 如果 T4 挂单完全成交，且 `Current_Price < T4挂单价格 * 0.995` (给予 0.5% 缓冲防止插针误判)。流转至 **State 3 (HIBERNATION)**。



### State 2: TTP_ARMED (追踪猎杀)

* **内存初始化:** 记录进入此状态时的实时价格为 `High_Water_Mark` (最高水位线)。
* **循环监听与流转条件:**
* 如果 `Current_Price > High_Water_Mark`，则更新 `High_Water_Mark = Current_Price`。
* **猎杀触发:** 实时计算撤退线 `Trailing_Stop = High_Water_Mark * (1 - 0.003)`。
* 如果 `Current_Price <= Trailing_Stop`:
1. 发送市价卖出订单 (Market Sell)，平掉 100% 仓位。
2. **【必选动作】** 调用 API 批量撤销该交易对下所有的未成交限价单 (`Cancel All Open Orders`)。
3. 记录日志/推送收益，流转至 **State 0 (HUNTING)**。





### State 3: HIBERNATION (深度休眠)

* **触发动作 (按严格顺序执行且不可逆):**
1. 挂起线程锁，停止一切策略计算和 WebSocket K 线解析。
2. 调用 `POST /sapi/v1/asset/transfer`，将 1700 USDT 从 Spot (现货) 划转至 USDⓈ-M (U本位合约)。
3. 撤销盘面所有剩余挂单（如果有）。
4. 发送最高级别 CRITICAL 报警给管理员。
5. 进入死循环 `await asyncio.sleep(86400)`，仅维持心跳，直到人工重启进程。



---

## 四、 给 Agent 的硬核代码审查清单 (Code Review Checklist)

请 Agent 在审查用户的 Python 代码时，**必须**高亮标记以下维度的错误或遗漏：

1. **精度异常拦截 (Filter Failures):** 检查代码在调用 `ccxt.create_order` 之前，是否对 `price` 和 `amount` 应用了币安的 `tickSize` 和 `stepSize` 进行强制向下取整 (Floor/Round)。若无，直接报错。
2. **WebSocket 断流假死 (Connection Drops):** 检查 `websockets` 监听循环中是否实现了 `Ping/Pong` 心跳检测或 `asyncio.wait_for` 超时断开重连逻辑。若只是简单的 `while True: await ws.recv()`，必须标记为高危漏洞。
3. **部分成交污染 (Partial Fills):** 检查 State 1 中计算 `Average_Price` 的逻辑。如果代码是通过本地公式 `(P1*V1 + P2*V2)/(V1+V2)` 计算均价，标记为警告；强制要求使用 API 实时获取的 `position['entryPrice']`。
4. **死锁与权重超限 (Rate Limit):** 检查主循环是否使用了无阻塞的 `asyncio.sleep(0)` 或事件驱动。如果发现使用 `time.sleep()` 或频繁的 `while True` 中调用 REST API (`fetch_ticker`) 获取价格，必须拦截，要求改为订阅 `@bookTicker`。

