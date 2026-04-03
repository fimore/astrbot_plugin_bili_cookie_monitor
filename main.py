"""B站Cookie状态监控插件"""
import os, json, asyncio
from datetime import datetime
from typing import Optional, Dict
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

@register("astrbot_plugin_bili_cookie_monitor", "fimore", "B站Cookie状态监控插件", "2.0.2", "https://github.com/fimore/astrbot_plugin_bili_cookie_monitor")
class BiliCookieMonitorPlugin(Star):
    """B站Cookie监控插件主类"""
    
    # 管理员用户ID白名单
    ADMIN_WHITELIST = ["2AF398285F94FD618716FEA1159F83B7"]
    
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 安全处理config类型
        if config is None:
            self.config = {}
        elif isinstance(config, dict):
            self.config = config
        else:
            try:
                self.config = dict(config)
            except (TypeError, ValueError):
                self.config = {}
        
        # 配置参数 - 校验check_interval
        self.cookie: str = self.config.get("cookie", "")
        raw_interval = self.config.get("check_interval", 3600)
        self.check_interval: int = max(60, int(raw_interval) if raw_interval else 3600)
        self.cookie_file: str = self.config.get("cookie_file", "")
        self.notify_user_id: str = self.config.get("notify_user_id", "")
        
        # 状态记录
        self.last_status: Optional[Dict] = None
        self.last_check_time: Optional[datetime] = None
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._was_invalid: bool = False
        self._last_notify_time: Optional[datetime] = None
        self._notify_cooldown: int = 3600  # 通知冷却时间（秒）
        
        # 并发锁
        self._file_lock = asyncio.Lock()
        
        # 数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_bili_cookie_monitor")
        self._status_file = self._data_dir / "last_status.json"
    
    async def initialize(self):
        """插件初始化"""
        await self._load_last_status()
        if self.cookie or self.cookie_file:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info(f"B站Cookie监控插件已启动，检测间隔: {self.check_interval}秒")
        else:
            logger.warning("B站Cookie监控插件: 未配置Cookie或Cookie文件")
    
    # ==================== 指令处理 ====================
    
    @filter.command("bili_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动检测B站Cookie状态"""
        result = await self._check_cookie()
        
        if result["valid"]:
            msg = f"✅ B站Cookie有效\n用户: {result.get('username', '未知')}\nUID: {result.get('uid', 0)}"
        else:
            msg = f"❌ B站Cookie失效\n错误: {result.get('error', '未知错误')}"
        
        self.last_status = result
        self.last_check_time = datetime.now()
        await self._save_last_status()
        
        yield event.plain_result(msg)
    
    @filter.command("bili_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看B站Cookie监控状态"""
        lines = [
            f"检测间隔: {self.check_interval}秒",
            f"Cookie文件: {self.cookie_file or '未配置'}",
            f"监控状态: {'运行中' if self._running else '已停止'}",
        ]
        if self.last_status:
            status = "有效" if self.last_status.get("valid") else "失效"
            lines.append(f"上次状态: {status}")
        yield event.plain_result("\n".join(lines))
    
    @filter.command("bili_update")
    async def cmd_update(self, event: AstrMessageEvent):
        """更新Cookie文件路径（仅管理员）"""
        # 权限校验
        sender_id = event.get_sender_id()
        if sender_id not in self.ADMIN_WHITELIST:
            yield event.plain_result("❌ 权限不足，仅管理员可使用此指令")
            return
        
        # 获取参数
        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /bili_update <cookie文件路径>")
            return
        
        new_path = parts[1].strip()
        
        # 文件存在性检查
        if not os.path.exists(new_path):
            yield event.plain_result(f"❌ 文件不存在: {new_path}")
            return
        
        # 安全检查：只允许读取Cookie相关文件
        fname = os.path.basename(new_path).lower()
        if "cookie" not in fname:
            yield event.plain_result("❌ 仅允许读取cookie相关文件")
            return
        
        self.cookie_file = new_path
        yield event.plain_result(f"✅ 已更新Cookie文件路径（本次运行有效）")
        logger.info(f"Cookie文件路径已更新为: {new_path}")
    
    # ==================== 监控逻辑 ====================
    
    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                # 从文件读取Cookie
                if self.cookie_file and os.path.exists(self.cookie_file):
                    with open(self.cookie_file, "r", encoding="utf-8") as f:
                        self.cookie = f.read().strip()
                
                # 检测Cookie状态
                result = await self._check_cookie()
                self.last_status = result
                self.last_check_time = datetime.now()
                await self._save_last_status()
                
                if result["valid"]:
                    logger.info(f"B站Cookie有效 - {result.get('username')}")
                    if self._was_invalid:
                        await self._send_notification("✅ Cookie已恢复", f"用户: {result.get('username')}")
                        self._was_invalid = False
                else:
                    logger.warning(f"B站Cookie失效: {result.get('error')}")
                    # 状态变化时才发送通知（边沿触发 + 冷却）
                    if not self._was_invalid or self._should_notify():
                        await self._send_notification("❌ Cookie已失效", f"错误: {result.get('error')}")
                        self._last_notify_time = datetime.now()
                    self._was_invalid = True
                
            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                raise
            except Exception as e:
                logger.error(f"B站Cookie监控出错: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    def _should_notify(self) -> bool:
        """检查是否应该发送通知（冷却机制）"""
        if self._last_notify_time is None:
            return True
        elapsed = (datetime.now() - self._last_notify_time).total_seconds()
        return elapsed >= self._notify_cooldown
    
    async def _check_cookie(self) -> dict:
        """检测B站Cookie是否有效"""
        if not self.cookie:
            return {"valid": False, "error": "Cookie为空"}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": self.cookie,
            "Referer": "https://www.bilibili.com/"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()
                    
                    if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                        u = data["data"]
                        return {
                            "valid": True,
                            "username": u.get("uname", ""),
                            "uid": u.get("mid", 0),
                            "vip": u.get("vipStatus") == 1
                        }
                    
                    error_msg = data.get("message", "未知错误")
                    code = data.get("code")
                    if code == -101:
                        error_msg = "账号未登录或Cookie已过期"
                    elif code == -352:
                        error_msg = "请求被风控"
                    
                    return {"valid": False, "error": error_msg, "code": code}
                    
        except asyncio.TimeoutError:
            return {"valid": False, "error": "请求超时"}
        except aiohttp.ClientError as e:
            return {"valid": False, "error": f"网络错误: {e}"}
        except Exception as e:
            return {"valid": False, "error": f"未知错误: {e}"}
    
    async def _send_notification(self, title: str, message: str):
        """发送通知给用户"""
        if not self.notify_user_id:
            return
        
        try:
            umo = self.notify_user_id if ":" in self.notify_user_id else f"default:FriendMessage:{self.notify_user_id}"
            await self.context.send_message(umo, MessageChain().message(f"{title}\n{message}"))
            logger.info(f"已发送通知到: {umo}")
        except Exception as e:
            logger.error(f"发送通知失败: {e}")
    
    # ==================== 持久化 ====================
    
    async def _load_last_status(self):
        """加载上次状态"""
        async with self._file_lock:
            try:
                if self._status_file.exists():
                    with open(self._status_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.last_status = data.get("last_status")
                    self._was_invalid = data.get("was_invalid", False)
                    if data.get("last_check_time"):
                        self.last_check_time = datetime.fromisoformat(data["last_check_time"])
                    if data.get("last_notify_time"):
                        self._last_notify_time = datetime.fromisoformat(data["last_notify_time"])
                    logger.info("已加载上次Cookie检测状态")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.error(f"加载上次状态失败: {e}")
    
    async def _save_last_status(self):
        """保存当前状态"""
        async with self._file_lock:
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                data = {
                    "last_status": self.last_status,
                    "last_check_time": self.last_check_time.isoformat() if self.last_check_time else None,
                    "was_invalid": self._was_invalid,
                    "last_notify_time": self._last_notify_time.isoformat() if self._last_notify_time else None
                }
                with open(self._status_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except (IOError, OSError) as e:
                logger.error(f"保存状态失败: {e}")
    
    async def terminate(self):
        """插件终止"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("B站Cookie监控插件已停止")
