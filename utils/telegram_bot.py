"""
Telegram 告警模块
────────────────
支持异步发送消息到指定 Telegram Chat，用于：
  - 入场 / 止盈 / HIBERNATION 等关键事件推送
  - 看门狗 / 风控异常告警
  - 每日 P&L 摘要 (可选)

配置项 (config/secrets.env):
  TELEGRAM_BOT_TOKEN=123456:ABC-DEF
  TELEGRAM_CHAT_ID=987654321
"""

import asyncio
from typing import Optional

import aiohttp
from loguru import logger


class TelegramBot:
    """轻量级异步 Telegram 告警发送器"""

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, enabled: bool = True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._worker_task: Optional[asyncio.Task] = None
        if self.enabled:
            logger.info("📱 Telegram 告警已启用")
        else:
            logger.warning("📱 Telegram 告警未启用 (token/chat_id 未配置)")

    # ── 公开 API ──────────────────────────────────────────────

    async def start(self):
        """启动后台发送 worker (应在 event loop 内调用)"""
        if not self.enabled:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._send_worker(), name="telegram_worker")

    async def stop(self):
        """优雅停止"""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None

    def send(self, message: str):
        """
        非阻塞投递消息到队列 (fire-and-forget)。
        如果队列满则丢弃最旧消息。
        """
        if not self.enabled:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()  # 丢弃最旧
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    async def send_now(self, message: str):
        """立即发送 (阻塞直到完成，用于 CRITICAL 级别)"""
        if not self.enabled:
            return
        await self._do_send(message)

    # ── 告警快捷方法 ─────────────────────────────────────────

    def alert_entry(self, symbol: str, price: float, qty: float, dynamic_vol: float, atr: float):
        self.send(
            f"🚀 *入场信号*\n"
            f"交易对: `{symbol}`\n"
            f"价格: `{price:.2f}`\n"
            f"数量: `{qty:.4f}`\n"
            f"动态面值: `{dynamic_vol:.1f} USDT`\n"
            f"ATR快照: `{atr:.2f}`"
        )

    def alert_grid_placed(self, symbol: str, grids: list):
        lines = "\n".join(
            f"  T{g.level}: `{g.price:.2f}` × `{g.amount:.4f}`" for g in grids
        )
        self.send(f"📍 *网格挂单*\n交易对: `{symbol}`\n{lines}")

    def alert_ttp_armed(self, symbol: str, profit_pct: float, avg_price: float, hours_held: float):
        self.send(
            f"🔥 *TTP 激活*\n"
            f"交易对: `{symbol}`\n"
            f"浮盈: `{profit_pct:.2f}%`\n"
            f"均价: `{avg_price:.2f}`\n"
            f"持仓时长: `{hours_held:.1f}h`"
        )

    def alert_take_profit(self, symbol: str, price: float, avg_price: float, qty: float):
        pnl_pct = (price - avg_price) / avg_price * 100 if avg_price > 0 else 0
        pnl_usdt = (price - avg_price) * qty
        self.send(
            f"💰 *止盈平仓*\n"
            f"交易对: `{symbol}`\n"
            f"卖出价: `{price:.2f}`\n"
            f"均价: `{avg_price:.2f}`\n"
            f"P&L: `{pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)`"
        )

    async def alert_hibernation(self, symbol: str, avg_price: float, total_amount: float):
        """HIBERNATION 使用 send_now 确保立即发出"""
        await self.send_now(
            f"🚨🚨🚨 *CRITICAL: HIBERNATION*\n"
            f"交易对: `{symbol}`\n"
            f"T4防线已击穿!\n"
            f"均价: `{avg_price:.2f}`\n"
            f"持仓量: `{total_amount:.4f}`\n"
            f"⚠️ 所有策略计算已冻结, 需要人工介入!"
        )

    def alert_watchdog(self, seconds_since_last: float):
        self.send(
            f"🛡️ *看门狗告警*\n"
            f"超过 `{seconds_since_last:.0f}s` 未收到行情数据\n"
            f"WebSocket 可能已假死, 正在尝试自愈..."
        )

    def alert_error(self, context: str, error: str):
        self.send(f"❌ *异常*\n模块: `{context}`\n详情: `{error}`")

    # ── 内部实现 ──────────────────────────────────────────────

    async def _send_worker(self):
        """后台消费队列，合并频率过高的消息"""
        try:
            while True:
                msg = await self._queue.get()
                await self._do_send(msg)
                # 限流: 最快每秒 1 条 (Telegram Bot API 限制 ~30 msg/s)
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            # 退出前尝试清空队列
            while not self._queue.empty():
                try:
                    msg = self._queue.get_nowait()
                    await self._do_send(msg)
                except asyncio.QueueEmpty:
                    break

    async def _do_send(self, text: str):
        """发送单条消息到 Telegram"""
        url = self.BASE_URL.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Telegram 发送失败 [{resp.status}]: {body[:200]}")
                    else:
                        logger.debug("Telegram 消息已发送")
        except Exception as e:
            logger.warning(f"Telegram 发送异常: {e}")

