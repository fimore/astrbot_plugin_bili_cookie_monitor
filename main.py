"""B站Cookie状态监控插件 - 增强版（支持扫码登录）"""
import os
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

import aiohttp
import qrcode
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

# B站扫码登录 API
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 扫码状态码
QR_CODE_UNSCANNED = 86101  # 未扫码
QR_CODE_SCANNED = 86090    # 已扫码未确认
QR_CODE_EXPIRED = 86038    # 二维码已过期
QR_CODE_SUCCESS = 0        # 登录成功

# 二维码有效期（秒）
QR_CODE_EXPIRE_TIME = 180

# 轮询间隔（秒）
POLL_INTERVAL = 5


@register("astrbot_plugin_bili_cookie_monitor", "fimore", "B站Cookie状态监控插件(扫码登录增强版)", "3.0.0", "https://github.com/fimore/astrbot_plugin_bili_cookie_monitor")
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
        self.notify_user_id: str = self.config.get("notify_user_id", "")

        # 通知冷却时间（可配置）
        self._notify_cooldown: int = max(60, int(self.config.get("notify_cooldown", 3600) or 3600))

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

        # HTTP会话（复用连接池）
        self._http_session: Optional[aiohttp.ClientSession] = None

        # 数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_bili_cookie_monitor")
        self._status_file = self._data_dir / "last_status.json"

        # 扫码登录状态：记录当前正在扫码的用户，防止重复扫码
        self._login_tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def _parse_check_interval(value) -> int:
        """安全解析检测间隔，返回默认值如果解析失败"""
        try:
            interval = int(value)
            return max(60, interval)
        except (ValueError, TypeError):
            return 3600
    
    async def initialize(self):
        """插件初始化"""
        await self._load_last_status()
        await self._load_cookie_from_data()
        self._http_session = aiohttp.ClientSession()

        if not self.cookie:
            logger.info("B站Cookie监控插件: 未配置Cookie，请使用 /bili_login 扫码登录获取")
            await self._http_session.close()
            self._http_session = None
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"B站Cookie监控插件已启动，检测间隔: {self.check_interval}秒")
    
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
            f"监控状态: {'运行中' if self._running else '已停止'}",
        ]
        if self.last_status:
            status = "有效" if self.last_status.get("valid") else "失效"
            lines.append(f"上次状态: {status}")
        yield event.plain_result("\n".join(lines))
    
    @filter.command("bili_login")
    async def cmd_qr_login(self, event: AstrMessageEvent):
        """B站扫码登录 - 获取二维码图片"""
        sender_id = event.get_sender_id()

        # 检查是否有正在进行的登录
        if sender_id in self._login_tasks and not self._login_tasks[sender_id].done():
            yield event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")
            return

        try:
            if not self._http_session:
                self._http_session = aiohttp.ClientSession()

            # 1. 调用B站API获取二维码
            yield event.plain_result("🔄 正在生成B站登录二维码...")

            async with self._http_session.get(
                BILI_QR_GENERATE_URL,
                headers=self._get_bili_headers(),
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

            if data.get("code") != 0:
                yield event.plain_result(f"❌ 获取二维码失败: {data.get('message', '未知错误')}")
                return

            qrcode_url = data["data"]["url"]
            qrcode_key = data["data"]["qrcode_key"]

            if not qrcode_key:
                yield event.plain_result("❌ 获取qrcode_key失败")
                return

            # 2. 生成二维码图片
            qr_image = await self._generate_qrcode_image(qrcode_url)
            if not qr_image:
                yield event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库")
                return

            # 3. 保存二维码图片并发送给用户
            qr_path = self._data_dir / f"qrcode_{sender_id}.png"
            os.makedirs(self._data_dir, exist_ok=True)
            qr_image.save(str(qr_path), "PNG")

            # 发送二维码图片
            yield event.image_result(str(qr_path))

            yield event.plain_result(
                "📱 请使用 **B站App** 扫描上方二维码登录\n"
                "⏱️ 二维码有效期约3分钟\n"
                "📋 扫码后请在手机上点击「确认登录」"
            )

            logger.info(f"已发送B站登录二维码给用户 {sender_id}，qrcode_key: {qrcode_key[:8]}...")

            # 4. 启动异步轮询任务
            task = asyncio.create_task(
                self._poll_qr_login(sender_id, qrcode_key, qr_path)
            )
            self._login_tasks[sender_id] = task

        except asyncio.TimeoutError:
            yield event.plain_result("❌ 请求超时，请稍后重试")
        except aiohttp.ClientError as e:
            yield event.plain_result(f"❌ 网络错误: {e}")
        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.plain_result(f"❌ 生成二维码失败: {e}")

    async def _poll_qr_login(self, sender_id: str, qrcode_key: str, qr_path: Path):
        """异步轮询扫码状态"""
        try:
            start_time = datetime.now()
            last_notified_status = None

            while True:
                elapsed = (datetime.now() - start_time).total_seconds()

                if elapsed >= QR_CODE_EXPIRE_TIME:
                    logger.info(f"用户 {sender_id} 的二维码已过期")
                    await self._notify_user(sender_id, "⏱️ 二维码已过期\n请重新发送 /bili_login 获取新二维码")
                    break

                if not self._http_session or self._http_session.closed:
                    logger.info(f"用户 {sender_id} 的扫码轮询因会话关闭而终止")
                    break

                try:
                    async with self._http_session.get(
                        BILI_QR_POLL_URL,
                        params={"qrcode_key": qrcode_key},
                        headers=self._get_bili_headers(),
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    logger.warning(f"轮询扫码状态失败: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)

                if code == QR_CODE_UNSCANNED:
                    pass

                elif code == QR_CODE_SCANNED:
                    if last_notified_status != QR_CODE_SCANNED:
                        await self._notify_user(sender_id, "✅ 已扫码\n请在手机上点击「确认登录」完成授权")
                        last_notified_status = QR_CODE_SCANNED

                elif code == QR_CODE_EXPIRED:
                    logger.info(f"用户 {sender_id} 的二维码已被B站标记为过期")
                    await self._notify_user(sender_id, "⏱️ 二维码已过期\n请重新发送 /bili_login 获取新二维码")
                    break

                elif code == QR_CODE_SUCCESS:
                    logger.info(f"用户 {sender_id} 扫码登录成功")

                    # 从响应头Set-Cookie中提取（比resp.cookies更完整可靠）
                    cookie_dict = {}
                    for header in set_cookie_headers:
                        cookie_part = header.split(";")[0].strip()
                        if "=" in cookie_part:
                            name, value = cookie_part.split("=", 1)
                            cookie_dict[name.strip()] = value.strip()

                    if not cookie_dict:
                        logger.error("扫码成功但未获取到Cookie")
                        await self._notify_user(sender_id, "❌ 登录成功但未获取到Cookie，请重试")
                        break

                    # 转换为cookie字符串格式
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

                    # 更新插件状态
                    async with self._cookie_lock:
                        self.cookie = cookie_str

                    # 持久化到配置文件，重启后自动加载
                    await self._save_cookie_to_config(cookie_str)

                    # 如果监控未运行，启动监控
                    if not self._running:
                        if not self._http_session:
                            self._http_session = aiohttp.ClientSession()
                        self._running = True
                        self._task = asyncio.create_task(self._monitor_loop())
                        logger.info("扫码登录后自动启动监控循环")

                    # 验证Cookie是否有效
                    result = await self._check_cookie()
                    self.last_status = result
                    self.last_check_time = datetime.now()
                    await self._save_last_status()

                    if result["valid"]:
                        await self._notify_user(
                            sender_id,
                            f"🎉 登录成功，Cookie已生效！\n"
                            f"👤 用户: {result.get('username', '未知')}\n"
                            f"🆔 UID: {result.get('uid', 0)}\n"
                            f"{'👑 大会员' if result.get('vip') else '🐟 普通用户'}\n"
                            f"{'🚀 监控已自动启动' if self._running else '⚠️ 监控未运行'}"
                        )
                        self._was_invalid = False
                    else:
                        await self._notify_user(
                            sender_id,
                            f"⚠️ Cookie已保存但验证失败: {result.get('error')}\n请检查网络或重新扫码"
                        )
                    break

                else:
                    logger.warning(f"未知扫码状态码: {code}, 数据: {poll_data}")

                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info(f"用户 {sender_id} 的扫码轮询被取消")
        except Exception:
            logger.exception(f"扫码轮询出错 (用户: {sender_id})")
            try:
                await self._notify_user(sender_id, "❌ 扫码登录过程出错，请重新尝试 /bili_login")
            except Exception:
                pass
        finally:
            try:
                if sender_id in self._login_tasks:
                    del self._login_tasks[sender_id]
            except Exception:
                pass
            try:
                if qr_path.exists():
                    qr_path.unlink()
            except Exception:
                pass

    async def _generate_qrcode_image(self, url: str):
        """生成二维码图片（使用qrcode库）"""
        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            return img
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
            return None

    @staticmethod
    def _get_bili_headers() -> dict:
        """获取B站API请求头"""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _notify_user(self, sender_id: str, message: str):
        """向指定用户发送消息"""
        try:
            umo = sender_id if ":" in sender_id else f"default:FriendMessage:{sender_id}"
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送消息给 {sender_id} 失败: {e}")

    # ==================== 监控逻辑 ====================
    
    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
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
                    if not self._was_invalid or self._should_notify():
                        await self._send_notification("❌ Cookie已失效", f"错误: {result.get('error')}")
                        self._last_notify_time = datetime.now()
                    self._was_invalid = True

            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except Exception:
                logger.exception("B站Cookie监控出错")
            finally:
                if self._running:
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
        if not self._http_session:
            return {"valid": False, "error": "HTTP会话未初始化"}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": self.cookie,
            "Referer": "https://www.bilibili.com/"
        }

        try:
            async with self._http_session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

                # 自动刷新Cookie（捕获Set-Cookie）
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                if set_cookie_headers:
                    await self._refresh_cookie_from_headers(set_cookie_headers)

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
    
    async def _refresh_cookie_from_headers(self, set_cookie_headers: list) -> bool:
        """从响应头Set-Cookie中刷新Cookie，返回是否有更新"""
        if not set_cookie_headers:
            return False

        new_cookies = {}
        for header in set_cookie_headers:
            cookie_part = header.split(";")[0].strip()
            if "=" in cookie_part:
                name, value = cookie_part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if value:
                    new_cookies[name] = value

        if not new_cookies:
            return False

        async with self._cookie_lock:
            # 解析现有cookie
            existing = {}
            for part in self.cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    existing[k.strip()] = v.strip()

            # 合并（新cookie覆盖旧的）
            merged = {**existing, **new_cookies}
            new_cookie_str = "; ".join(f"{k}={v}" for k, v in merged.items())

            if new_cookie_str != self.cookie:
                self.cookie = new_cookie_str
                await self._save_cookie_to_config(new_cookie_str)
                logger.info(f"Cookie已自动刷新，更新了 {len(new_cookies)} 个字段")
                return True

        return False

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

    async def _load_cookie_from_data(self):
        """从数据目录加载已保存的Cookie（优先级低于AstrBot配置）"""
        if self.cookie:
            return
        try:
            config_path = self._data_dir / "cookie_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                saved = config_data.get("cookie", "")
                if saved:
                    self.cookie = saved
                    logger.info("已从数据目录加载Cookie")
        except (IOError, OSError, json.JSONDecodeError) as e:
            logger.error(f"加载Cookie失败: {e}")

    async def _save_cookie_to_config(self, cookie_str: str):
        """将Cookie持久化到数据目录"""
        try:
            config_path = self._data_dir / "cookie_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
            else:
                config_data = {}
            config_data["cookie"] = cookie_str
            os.makedirs(self._data_dir, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=4)
            logger.debug("Cookie已保存到数据目录")
        except (IOError, OSError, json.JSONDecodeError) as e:
            logger.error(f"保存Cookie失败: {e}")
    
    async def terminate(self):
        """插件终止"""
        self._running = False

        # 取消所有扫码轮询任务
        for uid, task in self._login_tasks.items():
            if not task.done():
                task.cancel()
        self._login_tasks.clear()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("终止监控任务时发生异常")

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            logger.debug("HTTP会话已关闭")

        logger.info("B站Cookie监控插件已停止")
