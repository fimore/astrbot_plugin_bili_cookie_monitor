"""B站Cookie状态监控插件"""
import os
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict, Set, List
from pathlib import Path

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

@register("astrbot_plugin_bili_cookie_monitor", "fimore", "B站Cookie状态监控插件", "2.0.5", "https://github.com/fimore/astrbot_plugin_bili_cookie_monitor")
class BiliCookieMonitorPlugin(Star):
    """B站Cookie监控插件主类"""

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

        # 配置参数 - 校验check_interval（安全处理类型转换）
        self.cookie: str = self.config.get("cookie", "")
        self.check_interval: int = self._parse_check_interval(
            self.config.get("check_interval", 3600)
        )
        self.cookie_file: str = self.config.get("cookie_file", "")
        self.notify_user_id: str = self.config.get("notify_user_id", "")

        # 管理员白名单（必须从配置文件读取）
        admin_whitelist = self.config.get("admin_whitelist", [])
        if not admin_whitelist:
            logger.warning("未配置admin_whitelist，/bili_update指令将无法使用")
        self.ADMIN_WHITELIST: Set[str] = set(admin_whitelist) if isinstance(admin_whitelist, list) else {admin_whitelist}

        # 通知冷却时间（可配置）
        self._notify_cooldown: int = max(60, int(self.config.get("notify_cooldown", 3600) or 3600))

        # 允许的Cookie文件读取目录（安全限制）
        self._allowed_cookie_dirs: List[str] = self.config.get("allowed_cookie_dirs", [])

        # 状态记录
        self.last_status: Optional[Dict] = None
        self.last_check_time: Optional[datetime] = None
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._was_invalid: bool = False
        self._last_notify_time: Optional[datetime] = None

        # 并发锁
        self._file_lock = asyncio.Lock()
        self._cookie_lock = asyncio.Lock()

        # 数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_bili_cookie_monitor")
        self._status_file = self._data_dir / "last_status.json"

    @staticmethod
    def _parse_check_interval(value) -> int:
        """安全解析检测间隔，返回默认值如果解析失败"""
        try:
            interval = int(value)
            return max(60, interval)  # 最小60秒
        except (ValueError, TypeError):
            return 3600  # 默认1小时
    
    async def initialize(self):
        """插件初始化"""
        await self._load_last_status()

        # 检查Cookie配置
        has_cookie = bool(self.cookie)
        has_cookie_file = bool(self.cookie_file)

        if has_cookie and has_cookie_file:
            logger.warning("同时配置了 cookie 和 cookie_file，将优先使用 cookie_file 中的内容")
        elif not has_cookie and not has_cookie_file:
            logger.warning("B站Cookie监控插件: 未配置Cookie或Cookie文件")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

        source = "cookie_file" if has_cookie_file else "cookie"
        logger.info(f"B站Cookie监控插件已启动，数据源: {source}，检测间隔: {self.check_interval}秒")
    
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

        # 路径安全验证
        error_msg = self._validate_cookie_path(new_path)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        # 检查文件是否存在且可读
        if not await self._is_readable_file(new_path):
            yield event.plain_result(f"❌ 文件不存在或不可读: {new_path}")
            return

        self.cookie_file = new_path
        yield event.plain_result(
            f"✅ Cookie文件路径已更新（本次运行有效）\n"
            f"路径: {new_path}\n"
            f"⚠️ 重启后将失效，请在配置文件中永久设置 cookie_file 项"
        )
        logger.info(f"Cookie文件路径已更新为: {new_path} by {sender_id}")

    def _validate_cookie_path(self, file_path: str) -> Optional[str]:
        """
        验证Cookie文件路径是否安全
        返回错误信息，None表示验证通过
        """
        try:
            path = Path(file_path).resolve()
            fname = path.name.lower()
            normalized_path = str(path).lower()  # 提前定义，供后续检查使用

            # 1. 文件名必须包含cookie（防御性命名约定）
            if "cookie" not in fname:
                return "❌ 仅允许读取cookie相关文件（文件名需包含'cookie'）"

            # 2. 检查文件扩展名
            allowed_extensions = {".txt", ".json", ".cookie", ""}
            if path.suffix.lower() not in allowed_extensions:
                return f"❌ 不支持的文件类型，仅允许: {', '.join(allowed_extensions)}"

            # 3. 如果配置了允许目录，检查路径是否在允许范围内
            if self._allowed_cookie_dirs:
                allowed = False
                for allowed_dir in self._allowed_cookie_dirs:
                    allowed_path = Path(allowed_dir).resolve()
                    try:
                        path.relative_to(allowed_path)
                        allowed = True
                        break
                    except ValueError:
                        continue
                if not allowed:
                    return "❌ 文件路径不在允许的目录范围内"

            # 4. 拒绝敏感路径
            sensitive_patterns = ["passwd", "shadow", "hosts", "system32", "windows/system"]
            for pattern in sensitive_patterns:
                if pattern in normalized_path:
                    return f"❌ 拒绝访问敏感路径"

            return None

        except (OSError, ValueError) as e:
            return f"❌ 路径验证失败: {e}"

    async def _is_readable_file(self, file_path: str) -> bool:
        """异步检查文件是否可读"""
        try:
            path = Path(file_path)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: path.is_file() and os.access(path, os.R_OK))
        except (OSError, ValueError):
            return False
    
    # ==================== 监控逻辑 ====================
    
    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                # 从文件读取Cookie（异步加锁）
                await self._load_cookie_from_file()

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

                # 等待下一次检测
                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except Exception:
                logger.exception("B站Cookie监控出错")

    async def _load_cookie_from_file(self):
        """从文件加载Cookie（优先级高于配置中的cookie）"""
        if not self.cookie_file:
            return

        async with self._cookie_lock:
            try:
                path = Path(self.cookie_file)
                if not path.is_file():
                    logger.warning(f"Cookie文件不存在: {self.cookie_file}")
                    return

                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(None, self._read_file_sync, path)
                self.cookie = content.strip()
                logger.debug(f"已从文件加载Cookie，长度: {len(self.cookie)}")

            except (IOError, OSError) as e:
                logger.error(f"读取Cookie文件失败: {e}")

    @staticmethod
    def _read_file_sync(path: Path) -> str:
        """同步读取文件内容（在线程池中执行）"""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    
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
                # 任务被取消，正常退出
                pass
            except Exception:
                # 捕获其他可能的异常，避免传播
                logger.exception("终止监控任务时发生异常")
        logger.info("B站Cookie监控插件已停止")
