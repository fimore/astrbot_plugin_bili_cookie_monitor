"""
B站Cookie状态监控插件
功能：定时检测Cookie有效性，失效时主动通知用户
"""
import os
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig


@register(
    "astrbot_plugin_bili_cookie_monitor",
    "Ayaka",
    "B站Cookie状态监控插件 - 定时检测Cookie有效性，失效时主动通知用户",
    "2.0.1",
    "https://github.com/ayaka/astrbot_plugin_bili_cookie_monitor",
)
class BiliCookieMonitorPlugin(Star):
    """B站Cookie监控插件主类"""
    
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 确保config是字典类型
        if config is None:
            self.config = {}
        elif isinstance(config, dict):
            self.config = config
        else:
            # AstrBotConfig 对象，尝试转换为字典
            try:
                self.config = dict(config) if config else {}
            except:
                self.config = {}
        
        # 配置参数
        self.cookie: str = self.config.get("cookie", "")
        self.check_interval: int = self.config.get("check_interval", 3600)
        self.cookie_file: str = self.config.get("cookie_file", "")
        self.notify_user_id: str = self.config.get("notify_user_id", "")
        
        # 状态记录
        self.last_status: Optional[Dict] = None
        self.last_check_time: Optional[datetime] = None
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._was_invalid: bool = False  # 记录上一次是否失效
        
        # 数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_bili_cookie_monitor")
        self._status_file = self._data_dir / "last_status.json"
    
    async def initialize(self):
        """插件初始化"""
        # 加载上次状态
        await self._load_last_status()
        
        # 启动监控任务
        if self.cookie or self.cookie_file:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info(f"B站Cookie监控插件已启动，检测间隔: {self.check_interval}秒")
        else:
            logger.warning("B站Cookie监控插件: 未配置Cookie或Cookie文件，不会启动监控")
    
    # ==================== 指令处理 ====================
    
    @filter.command("bili_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动检测B站Cookie状态"""
        yield event.plain_result("🔍 正在检测B站Cookie状态...")
        
        result = await self._check_cookie()
        
        if result["valid"]:
            msg = (
                f"✅ B站Cookie有效\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 用户: {result.get('username', '未知')}\n"
                f"🆔 UID: {result.get('uid', 0)}\n"
                f"📊 等级: Lv.{result.get('level', 0)}\n"
                f"💎 硬币: {result.get('money', 0)}\n"
                f"👑 会员: {'大会员' if result.get('vip') else '普通用户'}"
            )
        else:
            msg = (
                f"❌ B站Cookie失效\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ 错误: {result.get('error', '未知错误')}\n"
                f"📋 错误码: {result.get('code', 'N/A')}\n\n"
                f"💡 请及时更新Cookie!"
            )
        
        self.last_status = result
        self.last_check_time = datetime.now()
        await self._save_last_status()
        
        yield event.plain_result(msg)
    
    @filter.command("bili_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看B站Cookie监控状态"""
        status_lines = [
            "📊 **B站Cookie监控状态**",
            "━━━━━━━━━━━━━━━━━━━━",
            f"⏱️ 检测间隔: {self.check_interval}秒 ({self.check_interval // 60}分钟)",
            f"📁 Cookie文件: {self.cookie_file or '未配置'}",
            f"🔔 通知用户: {self.notify_user_id or '未配置'}",
            f"🔄 监控状态: {'运行中' if self._running else '已停止'}",
        ]
        
        if self.last_check_time:
            status_lines.append(f"🕐 上次检测: {self.last_check_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if self.last_status:
            if self.last_status.get("valid"):
                status_lines.append(f"✅ Cookie状态: 有效 ({self.last_status.get('username')})")
            else:
                status_lines.append(f"❌ Cookie状态: 失效 ({self.last_status.get('error')})")
        else:
            status_lines.append("❓ Cookie状态: 尚未检测")
        
        yield event.plain_result("\n".join(status_lines))
    
    @filter.command("bili_update")
    async def cmd_update(self, event: AstrMessageEvent):
        """更新Cookie文件路径（管理员指令）"""
        # 获取参数
        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /bili_update <cookie文件路径>")
            return
        
        new_path = parts[1].strip()
        if os.path.exists(new_path):
            self.cookie_file = new_path
            self.config["cookie_file"] = new_path
            yield event.plain_result(f"✅ 已更新Cookie文件路径: {new_path}")
        else:
            yield event.plain_result(f"❌ 文件不存在: {new_path}")
    
    # ==================== 监控逻辑 ====================
    
    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                # 从文件读取Cookie
                if self.cookie_file and os.path.exists(self.cookie_file):
                    with open(self.cookie_file, 'r', encoding='utf-8') as f:
                        self.cookie = f.read().strip()
                
                # 检测Cookie状态
                result = await self._check_cookie()
                self.last_status = result
                self.last_check_time = datetime.now()
                await self._save_last_status()
                
                if result["valid"]:
                    username = result.get('username', '未知')
                    logger.info(f"B站Cookie有效 - 用户: {username}")
                    
                    # 如果之前是失效状态，现在恢复了，发送通知
                    if self._was_invalid:
                        await self._send_notification(
                            "✅ B站Cookie已恢复!",
                            f"Cookie已恢复有效!\n用户: {username}\nUID: {result.get('uid')}"
                        )
                        self._was_invalid = False
                else:
                    error = result.get("error", "未知错误")
                    logger.warning(f"B站Cookie失效: {error}")
                    
                    # 发送失效通知
                    await self._send_notification(
                        "❌ B站Cookie已失效!",
                        f"B站Cookie已失效!\n错误: {error}\n请及时更新Cookie!"
                    )
                    self._was_invalid = True
                
            except Exception as e:
                logger.error(f"B站Cookie监控出错: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    async def _check_cookie(self) -> dict:
        """检测B站Cookie是否有效"""
        if not self.cookie:
            return {"valid": False, "error": "Cookie为空"}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
                    
                    if data.get("code") == 0:
                        user_data = data.get("data", {})
                        if user_data.get("isLogin"):
                            level_info = user_data.get("level_info", {})
                            return {
                                "valid": True,
                                "username": user_data.get("uname", ""),
                                "uid": user_data.get("mid", 0),
                                "level": level_info.get("current_level", 0),
                                "vip": user_data.get("vipStatus", 0) == 1,
                                "money": user_data.get("money", 0.0),
                                "coins": user_data.get("coins", 0.0)
                            }
                    
                    error_msg = data.get("message", "未知错误")
                    code = data.get("code")
                    if code == -101:
                        error_msg = "账号未登录或Cookie已过期"
                    elif code == -352:
                        error_msg = "请求被风控，Cookie可能已失效"
                    
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
            logger.warning("未配置通知用户ID，无法发送通知")
            return
        
        try:
            # 构建消息
            full_message = f"{title}\n━━━━━━━━━━━━━━━━━━━━\n{message}"
            
            # 解析用户ID，构建umo
            notify_id = self.notify_user_id
            if ":" in notify_id:
                umo = notify_id
            else:
                # 默认使用default平台的私聊
                umo = f"default:FriendMessage:{notify_id}"
            
            # 发送消息
            chain = MessageChain().message(full_message)
            await self.context.send_message(umo, chain)
            logger.info(f"已发送通知到: {umo}")
            
        except Exception as e:
            logger.error(f"发送通知失败: {e}")
    
    # ==================== 持久化 ====================
    
    async def _load_last_status(self):
        """加载上次状态"""
        try:
            if self._status_file.exists():
                with open(self._status_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.last_status = data.get("last_status")
                self._was_invalid = data.get("was_invalid", False)
                if data.get("last_check_time"):
                    self.last_check_time = datetime.fromisoformat(data["last_check_time"])
                logger.info("已加载上次Cookie检测状态")
        except Exception as e:
            logger.error(f"加载上次状态失败: {e}")
    
    async def _save_last_status(self):
        """保存当前状态"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            data = {
                "last_status": self.last_status,
                "last_check_time": self.last_check_time.isoformat() if self.last_check_time else None,
                "was_invalid": self._was_invalid
            }
            with open(self._status_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
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
