import json
import os
import requests
import time
import uuid
import re
from datetime import datetime
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
import plugins
from plugins import Plugin, Event, EventAction
from .wechat_login import WeChatLogin

def parse_iso_time(iso_time_str):
    """
    解析ISO 8601格式的日期时间字符串为时间戳
    如果解析失败，返回None
    """
    try:
        # 尝试使用dateutil库（更强大，但可能未安装）
        try:
            import dateutil.parser
            dt = dateutil.parser.parse(iso_time_str)
            return int(dt.timestamp())
        except ImportError:
            pass
        
        # 尝试使用内置的datetime解析（格式必须完全匹配）
        # 处理常见的ISO格式，如 "2025-04-14T03:14:49.753147+00:00"
        formats = [
            "%Y-%m-%dT%H:%M:%S.%f%z",  # 带微秒和时区
            "%Y-%m-%dT%H:%M:%S%z",      # 带时区
            "%Y-%m-%dT%H:%M:%S.%fZ",    # 带微秒的UTC
            "%Y-%m-%dT%H:%M:%SZ",       # UTC
            "%Y-%m-%dT%H:%M:%S",        # 无时区
            "%Y-%m-%d %H:%M:%S%z",      # 空格分隔，带时区
            "%Y-%m-%d %H:%M:%S"         # 空格分隔，无时区
        ]
        
        # 移除可能的小数点后6位以上的数字（Python只支持到微秒，即6位）
        if '.' in iso_time_str:
            parts = iso_time_str.split('.')
            if len(parts) == 2:
                decimal_part = parts[1]
                if '+' in decimal_part:
                    micro_and_tz = decimal_part.split('+')
                    if len(micro_and_tz[0]) > 6:
                        micro_and_tz[0] = micro_and_tz[0][:6]
                    parts[1] = '+'.join(micro_and_tz)
                elif '-' in decimal_part:
                    micro_and_tz = decimal_part.split('-')
                    if len(micro_and_tz[0]) > 6:
                        micro_and_tz[0] = micro_and_tz[0][:6]
                    parts[1] = '-'.join(micro_and_tz)
                elif 'Z' in decimal_part:
                    micro_and_z = decimal_part.split('Z')
                    if len(micro_and_z[0]) > 6:
                        micro_and_z[0] = micro_and_z[0][:6]
                    parts[1] = 'Z'.join(micro_and_z)
                elif len(decimal_part) > 6:
                    parts[1] = decimal_part[:6]
                iso_time_str = '.'.join(parts)
        
        # 尝试不同的格式
        dt = None
        for fmt in formats:
            try:
                dt = datetime.strptime(iso_time_str, fmt)
                break
            except ValueError:
                continue
        
        if dt is None:
            # 如果所有格式都失败，尝试简单地提取日期和时间
            match = re.search(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})', iso_time_str)
            if match:
                date_str, time_str = match.groups()
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        
        if dt is None:
            return None
        
        # 处理无时区的情况，假定为UTC时间
        if dt.tzinfo is None:
            # 时间戳就是自1970年1月1日UTC以来的秒数
            return int(dt.timestamp())
        else:
            # 有时区信息的直接转换为时间戳
            return int(dt.timestamp())
    
    except Exception as e:
        logger.error(f"解析ISO时间字符串出错: {e}")
        return None

@plugins.register(
    name="hid",
    desire_priority=60,
    hidden=False,
    desc="使用hidreamai API生成图片",
    version="0.1",
    author="八戒",
)
class hid(Plugin):
    def __init__(self):
        super().__init__()
        self.config_path = os.path.join(os.path.dirname(__file__), "config.json")
        self.config = self.load_config()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        logger.info("[hid] 插件已加载。")
        self.priority = 60
        
        # 登录相关属性
        self.logged_in = False
        self.login_qrcode_path = os.path.join(os.path.dirname(__file__), "login_qrcode.png")
        self.login_status_messages = {
            404: "已扫码，请在手机上确认登录",
            405: "扫码成功，正在登录...",
            403: "用户取消或拒绝登录",
            402: "二维码已过期",
            408: "等待扫码..."
        }
        
        # Hidreamai API常量
        self.BASE_URL = 'https://hidreamai.com'
        self.HEADERS = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9',
            'content-type': 'application/json;charset=UTF-8',
            'origin': self.BASE_URL,
            'priority': 'u=1, i',
            'referer': f'{self.BASE_URL}/img-generation',
            'sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
            'x-accept-language': 'zh'
        }
        
        # 初始化cookies和用户信息
        self.COOKIES = self.config.get("cookies", {})
        self.USER_ID = self.config.get("user_id", "")
        self.DEVICE_ID = self.config.get("device_id", "")
        
        # 如果配置中有refresh_token但不在cookies中，确保添加
        if self.config.get("refresh_token") and "refresh_token" not in self.COOKIES:
            self.COOKIES["refresh_token"] = self.config.get("refresh_token")
        
        # 如果配置中有username但不在cookies中，确保添加  
        if self.config.get("username") and "username" not in self.COOKIES:
            self.COOKIES["username"] = self.config.get("username")
        
        # 更新HEADERS中的refresh_token
        if self.config.get("refresh_token"):
            self.HEADERS["refresh-token"] = self.config.get("refresh_token")
        
        # 检查登录状态
        self.check_login_status()
        
        # 尝试刷新token，无论检查登录状态结果如何
        if self.config.get("refresh_token"):
            logger.info("[hid] 初始化时尝试刷新token（无论当前登录状态如何）")
            if self.refresh_token():
                logger.info("[hid] 初始化时token刷新成功，现在应该已登录")
                self.logged_in = True
                # 更新实例属性，确保使用最新配置
                self.COOKIES = self.config.get("cookies", {})
                self.USER_ID = self.config.get("user_id", "")
                if self.config.get("refresh_token"):
                    self.HEADERS["refresh-token"] = self.config.get("refresh_token")
        
        # 登录状态提示
        if self.logged_in:
            # 显示登录信息
            username = self.config.get("username", "")
            nickname = self.config.get("nickname", "")
            display_name = nickname or username or "用户"
            logger.info(f"[hid] 当前登录用户: {display_name}")
        else:
            logger.warning("[hid] 缺少必要的登录信息，请使用 'h 登录' 命令登录系统")

    def load_config(self):
        """加载配置文件"""
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                # 记录关键信息，即使配置过期也需要保留
                refresh_token = config.get("refresh_token")
                user_id = config.get("user_id")
                device_id = config.get("device_id", str(uuid.uuid4()))
                username = config.get("username")
                nickname = config.get("nickname")
                cookies = config.get("cookies", {})
                
                # 检查配置是否过期
                if config.get("expire_time", 0) > time.time():
                    logger.info("[hid] 成功加载有效的配置文件")
                    self.logged_in = True
                    return config
                else:
                    logger.warning("[hid] 配置文件中的登录信息已过期")
                    
                    # 返回包含refresh_token的配置，以便后续能够自动刷新
                    return {
                        "cookies": cookies,
                        "refresh_token": refresh_token,
                        "user_id": user_id,
                        "device_id": device_id,
                        "expire_time": 0,
                        "username": username,
                        "nickname": nickname
                    }
            else:
                logger.warning(f"[hid] 配置文件不存在，使用默认配置")
                return {
                    "cookies": {},
                    "user_id": "",
                    "device_id": str(uuid.uuid4())
                }
        except Exception as e:
            logger.error(f"[hid] 加载配置文件失败: {e}")
            return {
                "cookies": {},
                "user_id": "",
                "device_id": str(uuid.uuid4())
            }
            
    def save_config(self, config):
        """保存配置到文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            logger.info(f"[hid] 配置已保存到 {self.config_path}")
            return True
        except IOError as e:
            logger.error(f"[hid] 保存配置文件失败: {e}")
            return False

    def refresh_token(self):
        """使用refresh_token刷新access token"""
        if not self.config.get("refresh_token"):
            logger.error("[hid] 无法刷新token: 缺少refresh_token")
            return False
            
        # 检查上次刷新时间，避免频繁刷新
        current_time = int(time.time())
        last_refresh = self.config.get("last_refresh", 0)
        if current_time - last_refresh < 60:  # 至少间隔60秒刷新一次
            logger.debug(f"[hid] 刷新请求过于频繁，距上次刷新仅过去 {current_time - last_refresh} 秒")
            return True  # 直接返回成功，避免频繁刷新
            
        refresh_token = self.config.get("refresh_token")
        logger.debug(f"[hid] 准备使用refresh_token刷新: {refresh_token[:10]}...{refresh_token[-10:]}")
        
        # 使用正确的API端点
        url = f"{self.BASE_URL}/prod-api/user/apikey2token"
        logger.debug(f"[hid] 刷新token API请求URL: {url}")
        
        try:
            logger.info("[hid] 正在刷新token...")
            # 不使用当前的cookies进行请求，因为ticket可能已经过期
            temp_headers = self.HEADERS.copy()
            # 添加refresh_token到请求头
            temp_headers["refresh-token"] = refresh_token
            
            temp_cookies = {
                "refresh_token": refresh_token
            }
            # 保留其他非票据cookie，如username
            for name, value in self.config.get("cookies", {}).items():
                if name not in ["ticket"] and name not in temp_cookies:
                    temp_cookies[name] = value
            
            logger.debug(f"[hid] 发送API请求，头部: {list(temp_headers.keys())}, Cookies: {list(temp_cookies.keys())}")
            
            # 发送GET请求，不需要JSON数据
            response = requests.get(url, headers=temp_headers, cookies=temp_cookies, timeout=30)
            
            logger.debug(f"[hid] 收到API响应: 状态码 {response.status_code}, 内容长度: {len(response.text)}")
            
            # 检查HTTP状态码
            if response.status_code != 200:
                error_msg = f"HTTP错误: {response.status_code}"
                try:
                    # 尝试解析错误消息
                    error_data = response.json()
                    if "message" in error_data:
                        error_msg = error_data["message"]
                    elif "msg" in error_data:
                        error_msg = error_data["msg"]
                except:
                    error_msg = response.text
                
                # 判断是否是token不存在错误
                if "token not existed" in response.text or "token not found" in response.text or "token invalid" in response.text:
                    logger.error("[hid] refresh_token已失效或过期，需要重新登录")
                    # 清除失效的token信息
                    self._clear_invalid_credentials()
                    return False
                    
                logger.error(f"[hid] 刷新token请求失败: {response.status_code} - {error_msg}")
                return False
                
            result = response.json()
            logger.debug(f"[hid] API响应JSON: {result.keys()}")
            
            if result.get("code") != 0:
                error_msg = result.get("msg", "未知错误") or result.get("message", "未知错误")
                logger.error(f"[hid] 刷新token API返回错误: {error_msg}")
                
                # 判断是否是token相关错误
                if "token" in error_msg.lower() and ("invalid" in error_msg.lower() or "not exist" in error_msg.lower() or "expired" in error_msg.lower()):
                    logger.error("[hid] refresh_token已失效或过期，需要重新登录")
                    # 清除失效的token信息
                    self._clear_invalid_credentials()
                
                return False
                
            # 获取新的token信息
            token_data = result.get("result", {})
            logger.debug(f"[hid] 刷新token响应中的result字段: {token_data.keys() if token_data else 'None'}")
            
            if not token_data:
                logger.error("[hid] 刷新token响应中缺少必要信息")
                logger.debug(f"[hid] 响应内容: {result}")
                return False
            
            # 从token字段获取新的ticket
            new_ticket = token_data.get("token")
            if not new_ticket:
                logger.error("[hid] 刷新token响应中没有新的token")
                logger.debug(f"[hid] 响应内容: {token_data}")
                return False
            
            logger.debug(f"[hid] 成功获取新的ticket: {new_ticket[:10]}...{new_ticket[-10:] if len(new_ticket) > 20 else new_ticket}")
                
            # 使用原来的refresh_token，apikey2token接口不会返回新的refresh_token
            new_refresh_token = refresh_token
            
            # 解析expire_time
            expire_time_str = token_data.get("expire_time")
            if expire_time_str:
                logger.debug(f"[hid] 收到expire_time: {expire_time_str}")
                try:
                    # 解析ISO格式的时间字符串
                    expire_timestamp = parse_iso_time(expire_time_str)
                    if expire_timestamp:
                        expire_in = expire_timestamp - current_time
                        logger.info(f"[hid] 解析到过期时间: {expire_time_str}, 转换为时间戳: {expire_timestamp}, 有效期: {expire_in}秒")
                    else:
                        logger.warning(f"[hid] 无法解析过期时间字符串 '{expire_time_str}'，使用默认过期时间")
                        expire_in = 3600  # 默认1小时
                except Exception as e:
                    logger.warning(f"[hid] 无法解析过期时间字符串 '{expire_time_str}': {e}")
                    expire_in = 3600  # 默认1小时
            else:
                logger.info("[hid] 响应中没有过期时间，使用默认过期时间1小时")
                expire_in = 3600  # 默认1小时
            
            # 更新cookies中的ticket和refresh_token
            self.config["cookies"]["ticket"] = new_ticket
            self.config["cookies"]["refresh_token"] = new_refresh_token
            
            # 更新refresh_token和过期时间
            self.config["refresh_token"] = new_refresh_token
            self.config["expire_time"] = current_time + expire_in
            self.config["last_refresh"] = current_time  # 记录本次刷新时间
            
            logger.debug(f"[hid] 更新后的配置：expire_time={self.config['expire_time']}, last_refresh={self.config['last_refresh']}")
            
            # 保存到配置文件
            if self.save_config(self.config):
                # 更新实例变量
                self.COOKIES = self.config.get("cookies", {})
                logger.info(f"[hid] token刷新成功，新的过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.config['expire_time']))}")
                return True
            else:
                logger.error("[hid] token刷新成功但保存配置失败")
                return False
                
        except Exception as e:
            logger.error(f"[hid] 刷新token时出错: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _clear_invalid_credentials(self):
        """清除无效的凭证信息"""
        logger.info("[hid] 清除无效的登录凭证")
        # 保留设备ID，但清除其他凭证
        device_id = self.config.get("device_id", "")
        
        # 创建新的配置
        new_config = {
            "cookies": {},
            "token": None,
            "refresh_token": "",
            "user_id": "",
            "device_id": device_id,
            "expire_time": 0,
            "username": "",
            "nickname": None,
            "last_refresh": 0
        }
        
        # 更新config
        self.config = new_config
        # 更新实例属性
        self.COOKIES = {}
        self.USER_ID = ""
        self.DEVICE_ID = device_id
        self.logged_in = False
        
        # 保存到配置文件
        self.save_config(new_config)
    
    def validate_token(self):
        """验证当前token是否有效，如果即将过期则尝试刷新"""
        # 如果未登录但有refresh_token，尝试刷新
        if not self.logged_in and self.config.get("refresh_token"):
            logger.info("[hid] 当前未登录但有refresh_token，尝试刷新")
            refresh_result = self.refresh_token()
            if refresh_result:
                logger.info("[hid] 使用refresh_token成功恢复登录状态")
                self.logged_in = True
                # 更新实例属性
                self.COOKIES = self.config.get("cookies", {})
                self.USER_ID = self.config.get("user_id", "")
                if self.config.get("refresh_token"):
                    self.HEADERS["refresh-token"] = self.config.get("refresh_token")
            else:
                logger.error("[hid] 刷新token失败，仍然未登录")
            return self.logged_in
            
        # 如果未登录且没有refresh_token，直接返回False
        if not self.logged_in:
            return False
            
        current_time = time.time()
        expire_time = self.config.get("expire_time", 0)
        
        logger.debug(f"[hid] 验证token：当前时间={current_time}，过期时间={expire_time}，剩余={expire_time-current_time}秒")
        
        # 如果token已经过期，尝试刷新
        if current_time >= expire_time:
            logger.warning(f"[hid] Token已过期，尝试刷新 (过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_time))})")
            refresh_result = self.refresh_token()
            if not refresh_result:
                logger.error("[hid] Token刷新失败，用户需要重新登录")
                self.logged_in = False
            else:
                # 刷新成功，确保更新实例属性
                self.COOKIES = self.config.get("cookies", {})
                if self.config.get("refresh_token"):
                    self.HEADERS["refresh-token"] = self.config.get("refresh_token")
                logger.info(f"[hid] Token已刷新，新的过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.config.get('expire_time', 0)))}")
            return refresh_result
            
        # 如果token即将在5分钟内过期，提前刷新
        if expire_time - current_time < 300:  # 5分钟 = 300秒
            logger.info(f"[hid] Token即将过期 (剩余 {expire_time - current_time:.1f} 秒)，提前刷新")
            refresh_result = self.refresh_token()
            if not refresh_result:
                logger.warning("[hid] Token提前刷新失败，将在下次API请求时重试")
            else:
                # 刷新成功，确保更新实例属性
                self.COOKIES = self.config.get("cookies", {})
                if self.config.get("refresh_token"):
                    self.HEADERS["refresh-token"] = self.config.get("refresh_token")
                logger.info(f"[hid] Token已提前刷新，新的过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.config.get('expire_time', 0)))}")
            return True  # 即使刷新失败，但当前token仍然有效，返回True
            
        # token有效且不需要刷新
        return True
            
    def check_login_status(self):
        """检查登录状态"""
        current_time = time.time()
        
        # 检查是否有有效的登录凭证
        if self.config.get("expire_time", 0) > current_time and self.config.get("cookies") and self.config.get("user_id"):
            logger.info("[hid] 检测到有效的登录凭证")
            self.logged_in = True
            # 更新实例属性
            self.COOKIES = self.config.get("cookies", {})
            self.USER_ID = self.config.get("user_id", "")
            self.DEVICE_ID = self.config.get("device_id", "")
            
            # 打印用户信息
            username = self.config.get("username", "")
            nickname = self.config.get("nickname", "")
            display_name = nickname or username or "用户"
            logger.info(f"[hid] 当前登录用户: {display_name}")
            
            # 检查token是否即将过期，如果是，则尝试刷新
            # 但在这里只做检查，不立即刷新，避免干扰初始化流程
            if self.config.get("expire_time", 0) - current_time < 300:  # 如果5分钟内过期
                logger.info("[hid] 登录凭证即将过期，稍后将刷新")
            
            return True
        else:
            # 登录已过期，但不立即刷新，将刷新操作延迟到初始化方法中
            if self.config.get("refresh_token"):
                logger.info("[hid] 登录凭证已过期或不完整，稍后将尝试刷新")
                # 设置未登录状态，但保留refresh_token
                self.logged_in = False
                return False
            else:
                logger.warning("[hid] 没有有效的登录凭证和refresh_token，需要重新登录")
                self.logged_in = False
                return False

    def _send_local_image(self, image_path, e_context):
        """发送本地图片"""
        try:
            with open(image_path, 'rb') as f:
                image_reply = Reply(ReplyType.IMAGE, f)
                e_context["channel"].send(image_reply, e_context["context"])
                logger.info(f"[hid] 发送本地图片: {image_path}")
                return True
        except Exception as e:
            logger.error(f"[hid] 发送本地图片失败: {e}")
            return False
            
    def _login_status_callback(self, status_code, e_context, context):
        """登录状态回调，用于向用户报告扫码状态"""
        if status_code in self.login_status_messages:
            status_msg = self.login_status_messages[status_code]
            reply = Reply(ReplyType.TEXT, status_msg)
            e_context["channel"].send(reply, context)

    def on_handle_context(self, e_context):
        """处理用户消息"""
        context = e_context['context']
        
        if context.type != ContextType.TEXT:
            return
            
        content = context.content
        if not content:
            return
            
        # 检查是否是触发命令
        if not content.startswith('h '):
            return
            
        logger.info(f"[hid] 收到命令: {content}")
        
        # 解析命令
        command = content[2:].strip()  # 去掉'h '前缀
        
        # 处理登录命令
        if command == "登录" or command == "login":
            self._handle_login_command(e_context, context)
            return
        
        # 处理登出命令
        if command == "登出" or command == "logout":
            self._handle_logout_command(e_context, context)
            return
        
        # 处理图片生成命令
        prompt, ratio, refine, negative_prompt = self.parse_input(command)
        
        reply = Reply()
        
        # 非空检查
        if not prompt:
            reply.type = ReplyType.TEXT
            reply.content = "请提供有效的提示词。\n用法: h 提示词-比例-润色<反向提示词\n例如: h 一只猫-4:3-润色<毛绒绒，不真实"
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return
        
        # 检查是否已登录，如果未登录但有refresh_token，尝试刷新
        if not self.logged_in:
            if self.config.get("refresh_token") and self.refresh_token():
                logger.info("[hid] 使用refresh_token自动登录成功")
                self.logged_in = True
            else:
                reply.type = ReplyType.TEXT
                reply.content = "您尚未登录或登录已过期，请先使用 'h 登录' 命令登录系统。"
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return
        else:
            # 已登录状态下，验证token是否需要刷新
            if not self.validate_token():
                reply.type = ReplyType.TEXT
                reply.content = "您的登录已过期，请使用 'h 登录' 命令重新登录。"
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
        # 生成中提示 - 立即发送
        reply.type = ReplyType.TEXT
        reply.content = f"正在生成: {prompt} (比例: {ratio}{', 已优化' if refine else ''}"
        if negative_prompt:
            reply.content += f", 反向提示词: {negative_prompt}"
        reply.content += ")"
        e_context['reply'] = reply
        e_context.action = EventAction.BREAK_PASS
        e_context["channel"].send(reply, context)  # 立即发送消息
        
        try:
            # 润色提示词
            final_prompt = prompt
            if refine:
                refined_prompt = self.refine_prompt(prompt)
                if refined_prompt and refined_prompt != prompt:
                    final_prompt = refined_prompt
                    logger.info(f"[hid] 润色后的提示词: {final_prompt}")
                
            # 记录事件
            self.log_event()
            
            # 生成图片
            task_id = self.generate_images(final_prompt, ratio, refine, negative_prompt)
            
            if not task_id:
                reply.type = ReplyType.TEXT
                reply.content = "生成任务创建失败，请稍后再试。"
                e_context["channel"].send(reply, context)
                # 清除回复，防止重复发送
                e_context['reply'] = None
                return
                
            # 等待图片生成完成
            image_urls = self.get_image_results(task_id)
            
            if not image_urls:
                reply.type = ReplyType.TEXT
                reply.content = "获取图片失败，请稍后再试。"
                e_context["channel"].send(reply, context)
                # 清除回复，防止重复发送
                e_context['reply'] = None
                return
                
            # 发送每张图片
            for url in image_urls:
                image_reply = Reply()
                image_reply.type = ReplyType.IMAGE_URL
                image_reply.content = url
                e_context["channel"].send(image_reply, context)
            
            # 清除回复，防止重复发送
            e_context['reply'] = None
                
        except Exception as e:
            logger.error(f"[hid] 处理图片生成请求出错: {e}")
            reply.type = ReplyType.TEXT
            reply.content = f"生成图片时发生错误: {str(e)}"
            e_context["channel"].send(reply, context)
            # 清除回复，防止重复发送
            e_context['reply'] = None
            
        return
        
    def _handle_login_command(self, e_context, context):
        """处理登录命令"""
        logger.info("[hid] 处理登录命令")
        
        # 检查是否已登录
        if self.logged_in:
            reply = Reply(ReplyType.TEXT, "您已经登录了系统，无需重新登录。如需重新登录，请先注销或等待登录过期。")
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 发送初始提示
        reply = Reply(ReplyType.TEXT, "正在获取微信登录二维码，请稍候...")
        e_context["channel"].send(reply, context)
        
        # 创建微信登录实例
        wx_login = WeChatLogin(self.config_path)
        
        # 获取并保存二维码
        qrcode_url, uuid = wx_login.get_qrcode(self.login_qrcode_path)
        
        if not qrcode_url or not uuid:
            reply = Reply(ReplyType.TEXT, "获取登录二维码失败，请稍后再试。")
            e_context["channel"].send(reply, context)
            e_context['reply'] = None
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 发送二维码图片
        reply = Reply(ReplyType.TEXT, "请使用微信扫描以下二维码登录:")
        e_context["channel"].send(reply, context)
        
        # 发送本地图片
        if not self._send_local_image(self.login_qrcode_path, e_context):
            reply = Reply(ReplyType.TEXT, "发送二维码图片失败，但您可以尝试使用浏览器访问二维码URL: " + qrcode_url)
            e_context["channel"].send(reply, context)
        
        # 定义状态回调，用于将登录进度通知给用户
        def status_callback(status):
            self._login_status_callback(status, e_context, context)
        
        # 监控登录状态
        code = wx_login.monitor_login(interval=3, timeout=180, status_callback=status_callback)
        
        if not code:
            reply = Reply(ReplyType.TEXT, "登录失败或超时，请重新尝试。")
            e_context["channel"].send(reply, context)
            e_context['reply'] = None
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 使用授权码登录后端
        config = wx_login.login_to_hidream(code)
        
        if not config:
            reply = Reply(ReplyType.TEXT, "后端登录失败，请重新尝试。")
            e_context["channel"].send(reply, context)
            e_context['reply'] = None
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 保存配置
        if self.save_config(config):
            # 更新当前实例的配置
            self.config = config
            self.COOKIES = config.get("cookies", {})
            self.USER_ID = config.get("user_id", "")
            self.DEVICE_ID = config.get("device_id", "")
            self.logged_in = True
            
            # 获取用户名
            username = config.get("username", "")
            nickname = config.get("nickname", "")
            display_name = nickname or username or "用户"
            
            reply = Reply(ReplyType.TEXT, f"登录成功！欢迎 {display_name}，现在您可以使用hid生成图片了。")
            e_context["channel"].send(reply, context)
        else:
            reply = Reply(ReplyType.TEXT, "登录成功但保存配置失败，请重新尝试。")
            e_context["channel"].send(reply, context)
            
        # 清除二维码文件
        try:
            if os.path.exists(self.login_qrcode_path):
                os.remove(self.login_qrcode_path)
        except Exception as e:
            logger.warning(f"[hid] 清除二维码文件失败: {e}")
            pass
            
        e_context['reply'] = None
        e_context.action = EventAction.BREAK_PASS

    def _handle_logout_command(self, e_context, context):
        """处理登出命令"""
        if not self.logged_in:
            reply = Reply(ReplyType.TEXT, "您当前未登录系统。")
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 清除凭证
        self._clear_invalid_credentials()
        
        reply = Reply(ReplyType.TEXT, "您已成功登出系统。")
        e_context['reply'] = reply
        e_context.action = EventAction.BREAK_PASS

    def parse_input(self, user_input):
        """解析用户输入，提取提示词、比例、润色标志和负向提示词"""
        # 检查是否包含负向提示词（<后面的内容）
        negative_prompt = ""
        if "<" in user_input:
            parts = user_input.split("<", 1)
            user_input = parts[0].strip()
            negative_prompt = parts[1].strip()
        
        parts = [p.strip() for p in user_input.split('-')]
        prompt_parts = []
        ratio = "16:9"  # 默认比例
        refine = False
        
        for part in parts:
            # 支持更多比例格式，包括纵向比例(9:16)
            if part in ["16:9", "4:3", "1:1", "9:16", "3:4"]:
                ratio = part
            elif part == "润色":
                refine = True
            else:
                prompt_parts.append(part)
        
        return ' '.join(prompt_parts), ratio, refine, negative_prompt

    def refine_prompt(self, prompt):
        """优化提示词"""
        url = f"{self.BASE_URL}/api/gw/v2/text/prompt_refine/sync"
        data = {
            "prompt": prompt,
            "lang": "Chinese"
        }
        
        try:
            logger.debug(f"[hid] 发送提示词优化请求: {prompt}")
            response = self._api_request_with_refresh('post', url, json_data=data, timeout=30)
            
            if not response:
                logger.warning("[hid] 提示词优化请求失败，无响应")
                return prompt
                
            if response.status_code == 200:
                result = response.json()
                if 'result' in result and 'prompt' in result['result']:
                    return result['result']['prompt']
                else:
                    logger.warning(f"[hid] 提示词优化返回不完整: {result}")
            else:
                logger.error(f"[hid] 提示词优化请求失败: {response.status_code} - {response.text}")
                
            return prompt
        except Exception as e:
            logger.error(f"[hid] 优化提示词时出错: {e}")
            return prompt

    def log_event(self):
        """记录生成事件"""
        url = f"{self.BASE_URL}/api/dc/v1/events"
        event_time = int(time.time())
        
        data = {
            "channel_id": "qx_pc",
            "user_id": self.USER_ID,
            "device_id": self.DEVICE_ID,
            "event_time": event_time,
            "event_id": "txt2img",
            "page_name": "aigc_studio",
            "device": "pc",
            "custom_paras": {}
        }
        
        try:
            logger.debug(f"[hid] 发送事件记录请求")
            response = self._api_request_with_refresh('post', url, json_data=data, timeout=30)
            
            if not response or response.status_code != 200:
                logger.warning(f"[hid] 事件记录请求可能失败: {response.status_code if response else 'No response'}")
        except Exception as e:
            logger.error(f"[hid] 记录事件时出错: {e}")

    def _api_request_with_refresh(self, method, url, json_data=None, params=None, timeout=30, max_retries=2):
        """执行API请求，自动处理401错误和token刷新
        
        Args:
            method: 请求方法，'get' 或 'post'
            url: 请求URL
            json_data: POST请求的JSON数据
            params: GET请求的参数
            timeout: 超时时间
            max_retries: 最大重试次数（刷新token后）
            
        Returns:
            requests.Response or None: 成功返回响应对象，失败返回None
        """
        # 首先确保token有效
        if not self.validate_token():
            logger.warning("[hid] API请求前发现token无效，尝试刷新")
            # 即使validate_token返回False，也要尝试刷新一次（因为可能是token过期）
            if self.config.get("refresh_token") and self.refresh_token():
                logger.info("[hid] 成功刷新token，继续请求")
                self.logged_in = True
            else:
                logger.error("[hid] Token无效且无法刷新，无法执行API请求")
                return None
        
        # 计算最大重试次数 (包括初始请求)
        retry_count = 0
        retry_queue = []  # 类似于JavaScript中的重试队列
        
        while retry_count <= max_retries:
            try:
                # 更新请求头中的一些关键信息
                updated_headers = self.HEADERS.copy()
                
                # 确保请求头中有ticket作为Bearer认证
                if 'ticket' in self.config.get("cookies", {}):
                    updated_headers["Authorization"] = f"Bearer {self.config['cookies']['ticket']}"
                
                # 添加refresh_token到请求头（根据用户提供的curl命令）
                if self.config.get("refresh_token"):
                    updated_headers["refresh-token"] = self.config.get("refresh_token")
                
                # 准备cookie，确保包含refresh_token和ticket
                request_cookies = self.COOKIES.copy()
                
                # 确保cookies中有refresh_token
                if self.config.get("refresh_token") and "refresh_token" not in request_cookies:
                    request_cookies["refresh_token"] = self.config.get("refresh_token")
                
                # 确保cookies中有ticket
                if 'ticket' in self.config.get("cookies", {}) and "ticket" not in request_cookies:
                    request_cookies["ticket"] = self.config["cookies"]["ticket"]
                
                # 记录请求信息，用于调试
                if url.endswith("apikey2token"):
                    logger.debug(f"[hid] 发送apikey2token请求。headers: {list(updated_headers.keys())}, cookies: {list(request_cookies.keys())}")
                
                # 发送请求 - 类似于JavaScript XHR adapter
                if method.lower() == 'get':
                    response = requests.get(
                        url, 
                        headers=updated_headers, 
                        cookies=request_cookies, 
                        params=params, 
                        timeout=timeout
                    )
                elif method.lower() == 'post':
                    response = requests.post(
                        url, 
                        headers=updated_headers, 
                        cookies=request_cookies, 
                        json=json_data, 
                        timeout=timeout
                    )
                else:
                    logger.error(f"[hid] 不支持的请求方法: {method}")
                    return None
                
                # 处理401错误 - 模拟JavaScript的XHR adapter中的响应处理
                if response.status_code in [401, 403] and retry_count < max_retries:
                    logger.warning(f"[hid] 收到{response.status_code}未授权响应，尝试刷新token: {url}")
                    
                    # 检查是否包含token失效信息
                    try:
                        error_data = response.json() if response.text else {}
                        error_msg = error_data.get("msg", "") or error_data.get("message", "") or response.text
                        logger.debug(f"[hid] 错误响应内容: {error_msg}")
                        
                        # 检查是否为refresh_token本身已失效
                        if ("refresh" in error_msg.lower() and "token" in error_msg.lower() and 
                            ("invalid" in error_msg.lower() or "not exist" in error_msg.lower() or 
                             "expired" in error_msg.lower())):
                            logger.error(f"[hid] Refresh token已失效: {error_msg}")
                            self._clear_invalid_credentials()
                            self.logged_in = False
                            return None
                            
                        # 检查是否为普通token失效
                        if "token" in error_msg.lower() and ("invalid" in error_msg.lower() or 
                                                           "not exist" in error_msg.lower() or 
                                                           "expired" in error_msg.lower()):
                            # 尝试刷新token，类似JavaScript中的刷新队列
                            retry_config = {
                                "method": method,
                                "url": url,
                                "json_data": json_data,
                                "params": params,
                                "timeout": timeout
                            }
                            retry_queue.append(retry_config)
                            
                            # 刷新token
                            if self.refresh_token():
                                logger.info("[hid] token刷新成功，重试请求")
                                # 更新retry_count并继续循环
                                retry_count += 1
                                
                                # 更新请求参数中的token (如果需要)
                                if isinstance(json_data, dict) and "ticket" in json_data:
                                    json_data["ticket"] = self.config["cookies"].get("ticket", "")
                                
                                # 短暂等待，确保服务端已更新token状态
                                time.sleep(1.0)
                                continue
                            else:
                                logger.error("[hid] token刷新失败，无法继续请求")
                                self.logged_in = False
                                return None
                    except Exception as token_error:
                        logger.error(f"[hid] 处理401响应时出错: {token_error}")
                        # 默认尝试刷新token
                        if self.refresh_token():
                            logger.info("[hid] token刷新成功，重试请求")
                            retry_count += 1
                            # 短暂等待，确保服务端已更新token状态
                            time.sleep(1.0)
                            continue
                        else:
                            logger.error("[hid] 默认token刷新失败，无法继续请求")
                            return None
                            
                # 处理其他HTTP错误
                if response.status_code >= 400:
                    try:
                        error_data = response.json() if response.text else {}
                        error_msg = error_data.get("msg", "") or error_data.get("message", "") or response.text
                        logger.warning(f"[hid] API请求返回错误: {response.status_code} - {error_msg}")
                    except:
                        logger.warning(f"[hid] API请求返回错误: {response.status_code} - {response.text}")
                
                # 返回响应
                return response
                
            except requests.exceptions.RequestException as e:
                logger.error(f"[hid] API请求异常: {e}")
                if retry_count < max_retries:
                    retry_count += 1
                    logger.info(f"[hid] 请求异常，将在1秒后重试 ({retry_count}/{max_retries})")
                    time.sleep(1.0)
                    continue
                return None
            
            except Exception as e:
                logger.error(f"[hid] 处理API请求时出错: {e}")
                if retry_count < max_retries:
                    retry_count += 1
                    logger.info(f"[hid] 处理请求出错，将在1秒后重试 ({retry_count}/{max_retries})")
                    time.sleep(1.0)
                    continue
                return None
                
        # 达到最大重试次数后仍未成功
        logger.error(f"[hid] 达到最大重试次数 {max_retries}，请求失败: {url}")
        return None

    def generate_images(self, prompt, ratio="16:9", refined=False, negative_prompt=""):
        """生成图像"""
        request_id = str(uuid.uuid4())
        logger.info(f"[hid] 开始生成图片，请求ID: {request_id}")
        
        # 根据比例设置宽度和高度
        if ratio == "16:9":
            width, height = 512, 288
        elif ratio == "9:16":  # 添加纵向比例9:16
            width, height = 288, 512  # 交换宽高比
        elif ratio == "4:3":
            width, height = 512, 384
        elif ratio == "3:4":  # 添加纵向比例3:4
            width, height = 384, 512  # 交换宽高比
        else:  # 默认为正方形(1:1)
            width, height = 512, 512
        
        # 修复: magic_prompt在不需要润色时应为空字符串
        magic_prompt = ""
        if refined:
            magic_prompt = prompt
            
        url = f"{self.BASE_URL}/api/gw/v2/image/txt2img/async"
        data = {
            "app": None,
            "image": None,
            "mask": None,
            "module": "txt2img",
            "negative_prompt": negative_prompt,
            "prompt": prompt,
            "params": {
                "batch_count": 1,
                "batch_size": 2,
                "guidance_scale": 7.5,
                "height": height,
                "image_guidance_scale": 1.5,
                "sample_steps": 40,
                "sampler": "Euler a",
                "seed": -1,
                "strength": 0.8,
                "style": "default",
                "wh_ratio": ratio,
                "width": width,
                "relevance": []
            },
            "role": "general",
            "version": "v3",
            "magic_prompt": magic_prompt,
            "request_id": request_id
        }
        
        try:
            logger.debug(f"[hid] 发送图片生成请求: {json.dumps(data, ensure_ascii=False)}")
            
            # 使用支持token刷新的API请求
            response = self._api_request_with_refresh('post', url, json_data=data, timeout=30)
            
            if not response:
                logger.error("[hid] 生成图片请求失败，未收到响应")
                return None
                
            if response.status_code == 200:
                result = response.json()
                if 'result' in result and 'task_id' in result['result']:
                    task_id = result['result']['task_id']
                    logger.info(f"[hid] 获取任务ID: {task_id}")
                    return task_id
                else:
                    logger.error(f"[hid] 响应缺少任务ID: {result}")
            else:
                logger.error(f"[hid] 生成图片请求失败: {response.status_code} - {response.text}")
                
            return None
        except Exception as e:
            logger.error(f"[hid] 生成图片时出错: {e}")
            return None

    def get_image_results(self, task_id, max_retries=20, delay=10):
        """轮询获取图像结果"""
        poll_url = f"{self.BASE_URL}/api/gw/v2/image/image/async/results/batch"
        poll_data = {"task_id_list": [task_id]}
        
        logger.info(f"[hid] 开始轮询任务结果, 任务ID: {task_id}, 最大重试次数: {max_retries}, 轮询间隔: {delay}s")
        
        # 状态码解释
        # 1: 成功完成
        # 2: 正在生成中
        # 4: 生成失败或其他终止状态
        
        last_successful_result = None  # 存储最后一次成功的响应结果
        consecutive_errors = 0  # 连续错误计数
        last_progress = {}  # 存储每个子任务的上一次进度
        history_id = None  # 存储历史记录ID，用于备选查询
        
        # 记录已知的子任务ID和状态
        known_subtasks = {}  # {sub_task_id: {'status': status, 'image': image_id}}
        
        for retry in range(max_retries):
            try:
                logger.debug(f"[hid] 获取结果第 {retry+1}/{max_retries} 次尝试")
                
                # 避免过多失败请求导致API封禁
                if consecutive_errors >= 3:
                    logger.warning(f"[hid] 连续出现 {consecutive_errors} 次错误，暂停请求10秒")
                    time.sleep(10)  # 额外等待，避免过快请求
                
                # 优先使用历史ID获取结果 (如果已知)
                if history_id:
                    logger.info(f"[hid] 已获取 history_id: {history_id}，尝试通过历史记录API获取最终结果")
                    history_urls = self._get_images_from_history(history_id)
                    if history_urls:
                        # 检查是否获取到了预期的图片数量 (batch_size=2)
                        if len(history_urls) >= 2:  # 假设batch_size=2
                            logger.info(f"[hid] 通过历史记录API成功获取到 {len(history_urls)} 张图片")
                            return history_urls
                        else:
                            logger.warning(f"[hid] 历史记录API暂时只返回 {len(history_urls)} 张图片，继续轮询")
                    else:
                        logger.warning(f"[hid] 通过 history_id {history_id} 获取历史记录失败或无结果")
                
                # 开始轮询
                response = self._api_request_with_refresh('post', poll_url, json_data=poll_data, timeout=30)
                
                # 处理无响应或405错误
                if not response:
                    logger.warning(f"[hid] 获取结果请求失败, 尝试次数 {retry+1}/{max_retries}")
                    consecutive_errors += 1
                    time.sleep(delay + consecutive_errors * 2)
                    continue
                
                if response.status_code == 405:
                    logger.warning(f"[hid] 遇到405错误 (Method Not Allowed)，轮询端点可能不再接受POST。尝试次数 {retry+1}/{max_retries}")
                    consecutive_errors += 1
                    
                    # 尝试获取history_id (如果还不知道)
                    if history_id is None:
                        logger.info("[hid] 405后尝试通过专用API获取 history_id")
                        extracted_history_id = self._extract_history_id_from_task(task_id)
                        if extracted_history_id:
                            history_id = extracted_history_id
                            logger.info(f"[hid] 成功获取到 history_id: {history_id}")
                            # 获取到history_id后，下一次循环会优先使用它
                        else:
                            logger.warning("[hid] 尝试获取 history_id 失败")
                    
                    # 如果已有部分结果，可以尝试返回
                    if known_subtasks:
                        partial_urls = []
                        for sub_id, info in known_subtasks.items():
                            if info['status'] == 1 and info['image']:
                                url = f"https://storage-cdn.hidreamai.com/image/{info['image']}.jpg"
                                if url not in partial_urls:
                                    partial_urls.append(url)
                        
                        if partial_urls:
                            logger.info(f"[hid] 405错误后，从已知子任务中提取到 {len(partial_urls)} 张图片")
                            if retry >= max_retries // 2:  # 如果已经轮询了一半次数，可以返回部分结果
                                return partial_urls
                    
                    # 增加等待时间再继续循环
                    wait_time = delay + consecutive_errors * 5  # 递增等待时间
                    logger.warning(f"[hid] 405错误后，等待 {wait_time} 秒后重试")
                    time.sleep(wait_time)
                    continue  # 继续下一次循环
                
                # 处理其他非200错误
                elif response.status_code != 200:
                    logger.error(f"[hid] 获取结果请求失败: {response.status_code} - {response.text}")
                    consecutive_errors += 1
                    time.sleep(delay + consecutive_errors * 2)  # 发生错误，稍作等待
                    continue  # 继续下一次循环
                
                # 处理200 OK响应
                result = response.json()
                last_successful_result = result  # 保存最后一次成功的响应
                consecutive_errors = 0  # 重置连续错误计数
                
                # 尝试从成功响应中提取history_id (如果之前不知道)
                if history_id is None:
                    extracted_history_id = self._extract_history_id(result)
                    if extracted_history_id:
                        history_id = extracted_history_id
                        logger.info(f"[hid] 从轮询响应中获取到 history_id: {history_id}")
                
                # 解析子任务状态
                if 'result' in result and len(result['result']) > 0:
                    task_result = result['result'][0]
                    sub_tasks = task_result.get('sub_task_results', [])
                    
                    if not sub_tasks and retry < max_retries - 1:  # 如果没有子任务信息且未到最后一次尝试，则可能任务在排队
                        logger.debug("[hid] 轮询结果中暂无子任务信息，可能仍在排队或初始化")
                        time.sleep(delay)
                        continue
                    
                    success_count = 0
                    failed_count = 0
                    in_progress_count = 0
                    total_tasks = len(sub_tasks)
                    current_image_urls = []
                    
                    for sub_task in sub_tasks:
                        sub_task_id = sub_task.get('sub_task_id')
                        status = sub_task.get('task_status')
                        progress = sub_task.get('task_completion', 0)
                        image_id = sub_task.get('image', '')
                        
                        # 更新已知子任务状态
                        if sub_task_id:
                            if sub_task_id not in known_subtasks:
                                known_subtasks[sub_task_id] = {'status': status, 'image': image_id}
                            elif status == 1:  # 如果任务完成，更新状态和图片ID
                                known_subtasks[sub_task_id] = {'status': status, 'image': image_id}
                        
                        # 记录任务状态
                        if status == 1:  # 成功
                            success_count += 1
                            if image_id:
                                url = f"https://storage-cdn.hidreamai.com/image/{image_id}.jpg"
                                current_image_urls.append(url)
                            logger.info(f"[hid] 子任务 {sub_task_id} 成功完成，图片ID: {image_id}")
                        elif status == 4:  # 失败
                            failed_count += 1
                            logger.warning(f"[hid] 子任务 {sub_task_id} 生成失败")
                        elif status == 2:  # 进行中
                            in_progress_count += 1
                            # 检查进度是否有变化
                            if sub_task_id in last_progress and progress > last_progress[sub_task_id]:
                                logger.debug(f"[hid] 子任务 {sub_task_id} 进度: {progress*100:.1f}%")
                        
                        if sub_task_id:
                            last_progress[sub_task_id] = progress
                    
                    # 日志记录任务整体状态
                    logger.debug(f"[hid] 任务状态: 成功={success_count}, 失败={failed_count}, 进行中={in_progress_count}, 总数={total_tasks}")
                    
                    # 如果有任务成功并且成功获取到历史ID，优先通过历史API获取所有图片
                    if success_count > 0 and history_id:
                        logger.info(f"[hid] 已有任务成功且有历史ID，尝试通过历史API获取所有图片")
                        history_urls = self._get_images_from_history(history_id)
                        if history_urls and len(history_urls) >= success_count:
                            logger.info(f"[hid] 从历史记录中获取到 {len(history_urls)} 张图片")
                            return history_urls
                    
                    # 检查是否所有任务都已终结 (成功或失败)
                    if in_progress_count == 0 and total_tasks > 0:
                        # 收集所有成功的图片URL
                        final_urls = []
                        for sub_id, info in known_subtasks.items():
                            if info['status'] == 1 and info['image']:
                                url = f"https://storage-cdn.hidreamai.com/image/{info['image']}.jpg"
                                if url not in final_urls:
                                    final_urls.append(url)
                        
                        if final_urls:
                            logger.info(f"[hid] 所有任务已完成，通过轮询获取到 {len(final_urls)} 张图片")
                            # 此时如果history_id已知，可以再用历史记录API确认一下
                            if history_id:
                                history_check_urls = self._get_images_from_history(history_id)
                                if history_check_urls and len(history_check_urls) > len(final_urls):
                                    logger.info(f"[hid] 历史记录API确认了更多图片 ({len(history_check_urls)}张)，使用历史记录结果")
                                    return history_check_urls
                            return final_urls
                        else:
                            logger.error("[hid] 所有任务已结束，但未能获取到任何成功的图片URL")
                            # 尝试最后一次通过history_id获取 (如果已知)
                            if history_id:
                                logger.info("[hid] 所有任务结束但无图片，最后尝试通过历史记录API获取")
                                final_history_urls = self._get_images_from_history(history_id)
                                if final_history_urls:
                                    logger.info(f"[hid] 最后从历史记录中获取到 {len(final_history_urls)} 张图片")
                                    return final_history_urls
                            return None  # 确实失败了
                    
                    # 如果有部分任务成功，并且轮询已经超过一定次数，可以提前返回部分结果
                    if success_count > 0 and retry >= max_retries // 3:
                        logger.info(f"[hid] 已经完成 {success_count} 个任务，还有 {in_progress_count} 个任务进行中，但已轮询 {retry+1} 次，返回已完成的图片")
                        return current_image_urls
                
                # 非最终状态，等待后进行下一次轮询
                if retry < max_retries - 1:
                    time.sleep(delay)
                    
            except requests.exceptions.Timeout:
                logger.warning(f"[hid] 获取结果请求超时 (尝试次数 {retry+1}/{max_retries})")
                consecutive_errors += 1
                # 超时后等待时间可以长一点
                time.sleep(delay * 2)
            except Exception as e:
                logger.error(f"[hid] 获取结果时发生异常: {e}", exc_info=True)
                consecutive_errors += 1
                time.sleep(delay + consecutive_errors * 2)  # 发生异常，也增加等待
            
            # 检查连续错误次数，防止死循环或过多无效请求
            if consecutive_errors >= 5:  # 例如连续5次错误就放弃
                logger.error(f"[hid] 连续发生 {consecutive_errors} 次错误，停止轮询")
                break
        
        # 轮询结束或超时后的最终处理
        logger.warning(f"[hid] 轮询结束或超时 (尝试 {max_retries} 次)")
        
        # 1. 优先尝试使用history_id获取
        if history_id:
            logger.info(f"[hid] 最后尝试通过 history_id {history_id} 获取最终结果")
            final_history_urls = self._get_images_from_history(history_id)
            if final_history_urls:
                logger.info(f"[hid] 最终从历史记录中获取到 {len(final_history_urls)} 张图片")
                return final_history_urls
            else:
                logger.warning(f"[hid] 最后尝试通过 history_id {history_id} 未获取到结果")
        
        # 2. 尝试从最后一次成功的轮询结果中提取
        if last_successful_result:
            logger.info("[hid] 尝试从最后一次成功的轮询响应中提取图片")
            final_urls_from_last = self._extract_image_urls_from_result(last_successful_result)
            if final_urls_from_last:
                logger.info(f"[hid] 从最后成功的轮询响应中提取到 {len(final_urls_from_last)} 张图片")
                return final_urls_from_last
        
        # 3. 尝试从已知子任务状态中提取 (作为最后的保障)
        logger.info("[hid] 尝试从记录的子任务状态中提取已完成的图片")
        final_urls_from_known = []
        for sub_id, info in known_subtasks.items():
            if info['status'] == 1 and info['image']:
                url = f"https://storage-cdn.hidreamai.com/image/{info['image']}.jpg"
                if url not in final_urls_from_known:
                    final_urls_from_known.append(url)
        if final_urls_from_known:
            logger.info(f"[hid] 从已知子任务记录中提取到 {len(final_urls_from_known)} 张图片")
            return final_urls_from_known
        
        logger.error(f"[hid] 最终未能获取到任务 {task_id} 的图片结果")
        return None
    
    def _extract_history_id_from_task(self, task_id):
        """通过任务ID获取历史ID"""
        url = f"{self.BASE_URL}/api/gw/v2/history/image/tasks"
        params = {"task_id": task_id}
        
        try:
            logger.debug(f"[hid] 请求历史任务API (GET): {url} with params {params}")
            # 使用支持token刷新的API请求
            response = self._api_request_with_refresh('get', url, params=params, timeout=30)
            
            if not response:
                logger.error("[hid] 历史任务请求失败，无响应")
                return None
                
            if response.status_code != 200:
                logger.error(f"[hid] 历史任务请求失败: {response.status_code} - {response.text}")
                return None
            
            result = response.json()
            logger.debug(f"[hid] 历史任务API响应: {result}")  # 打印响应帮助调试
            
            # 在result['result']列表中查找匹配task_id的项
            if 'result' in result and isinstance(result['result'], list):
                for item in result['result']:
                    # 确保item是字典并且包含所需键
                    if isinstance(item, dict) and item.get('main_task_id') == task_id and 'id' in item:
                        history_id = item.get('id')
                        if history_id is not None:
                            logger.info(f"[hid] 从历史任务API提取到history_id: {history_id}")
                            return str(history_id)  # 确保返回字符串
                
            # 如果上述方法未找到，尝试直接从响应文本解析
            try:
                import re
                if isinstance(response.text, str) and "history_id" in response.text:
                    matches = re.search(r'"history_id"\s*:\s*(\d+)', response.text)
                    if matches:
                        history_id = matches.group(1)
                        logger.info(f"[hid] 通过文本匹配提取到history_id: {history_id}")
                        return history_id
            except Exception as text_err:
                logger.error(f"[hid] 尝试从文本提取history_id时出错: {text_err}")
            
            logger.warning(f"[hid] 未能在历史任务响应中找到与task_id {task_id} 匹配的history_id")
            return None
        except Exception as e:
            logger.error(f"[hid] 获取历史任务出错: {e}", exc_info=True)
            return None
        
    def _extract_image_urls_from_result(self, result):
        """从API响应中提取图片URL"""
        image_urls = []
        try:
            if 'result' in result and len(result['result']) > 0:
                task_result = result['result'][0]
                sub_tasks = task_result.get('sub_task_results', [])
                
                for sub_task in sub_tasks:
                    if sub_task.get('task_status') == 1 and sub_task.get('image'):
                        image_id = sub_task.get('image')
                        url = f"https://storage-cdn.hidreamai.com/image/{image_id}.jpg"
                        image_urls.append(url)
        except Exception as e:
            logger.error(f"[hid] 提取图片URL出错: {e}")
        
        return image_urls
        
    def _extract_history_id(self, result):
        """从API响应中提取历史记录ID"""
        try:
            if 'result' in result and len(result['result']) > 0:
                task_result = result['result'][0]
                # 直接获取history_id字段
                if 'history_id' in task_result:
                    history_id = task_result.get('history_id')
                    # 确保返回字符串类型
                    return str(history_id) if history_id is not None else None
        except Exception as e:
            logger.error(f"[hid] 提取历史ID出错: {e}")
        
        # 尝试从原始响应文本中提取
        try:
            import re
            response_text = str(result)
            if "history_id" in response_text:
                matches = re.search(r'"history_id"\s*:\s*(\d+)', response_text)
                if matches:
                    return matches.group(1)
        except Exception as text_err:
            logger.error(f"[hid] 尝试从文本提取历史ID时出错: {text_err}")
        
        return None
        
    def _get_images_from_history(self, history_id):
        """通过历史记录API获取图片URL"""
        url = f"{self.BASE_URL}/api/gw/v2/history/image/image/{history_id}"
        
        try:
            logger.debug(f"[hid] 请求历史记录API: {url}")
            # 使用支持token刷新的API请求
            response = self._api_request_with_refresh('get', url, timeout=30)
            
            if not response:
                logger.error("[hid] 历史记录请求失败，无响应")
                return None
                
            if response.status_code != 200:
                logger.error(f"[hid] 历史记录请求失败: {response.status_code} - {response.text}")
                return None
                
            result = response.json()
            
            if 'result' not in result:
                logger.error(f"[hid] 历史记录响应缺少结果: {result}")
                return None
                
            # 从历史记录中提取图片URL
            image_urls = []
            history_result = result.get('result', {})
            
            # 处理不同的响应格式
            # 格式1: result.result 是图片结果数组
            if 'result' in history_result and isinstance(history_result['result'], list):
                results = history_result.get('result', [])
                
                if not results:
                    logger.warning("[hid] 历史记录中没有结果")
                else:
                    for item in results:
                        if item.get('task_status') == 1 and item.get('image'):
                            image_id = item.get('image')
                            url = f"https://storage-cdn.hidreamai.com/image/{image_id}.jpg"
                            image_urls.append(url)
            
            # 格式2: result 直接包含图片信息数组
            elif isinstance(history_result, list):
                for item in history_result:
                    if item.get('task_status') == 1 and item.get('image'):
                        image_id = item.get('image')
                        url = f"https://storage-cdn.hidreamai.com/image/{image_id}.jpg"
                        image_urls.append(url)
            
            # 如果上述方法都未找到图片，尝试直接从响应文本解析
            if not image_urls:
                try:
                    import re
                    response_text = response.text
                    # 查找所有图片ID模式: p_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
                    matches = re.findall(r'p_[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', response_text)
                    if matches:
                        for image_id in matches:
                            url = f"https://storage-cdn.hidreamai.com/image/{image_id}.jpg"
                            if url not in image_urls:  # 避免重复
                                image_urls.append(url)
                except Exception as e:
                    logger.error(f"[hid] 尝试从响应文本解析图片ID时出错: {e}")
            
            if image_urls:
                logger.info(f"[hid] 从历史记录中成功提取 {len(image_urls)} 张图片URL")
            
            return image_urls
            
        except Exception as e:
            logger.error(f"[hid] 获取历史记录出错: {e}")
            return None
        
    def _count_completed_tasks(self, result):
        """统计任务完成状态"""
        counts = {
            'success': 0,
            'failed': 0,
            'in_progress': 0,
            'total': 0
        }
        
        try:
            if 'result' in result and len(result['result']) > 0:
                task_result = result['result'][0]
                sub_tasks = task_result.get('sub_task_results', [])
                
                counts['total'] = len(sub_tasks)
                
                for sub_task in sub_tasks:
                    status = sub_task.get('task_status')
                    if status == 1:
                        counts['success'] += 1
                    elif status == 4:
                        counts['failed'] += 1
                    elif status == 2:
                        counts['in_progress'] += 1
        except Exception as e:
            logger.error(f"[hid] 统计任务状态出错: {e}")
            
        return counts

    def get_help_text(self, **kwargs):
        help_text = "hid插件使用方法：\n"
        help_text += "h 登录 - 通过微信扫码登录Hidreamai系统\n"
        help_text += "h 提示词 - 使用默认16:9比例生成图片\n"
        help_text += "h 提示词-4:3 - 使用4:3比例生成图片\n"
        help_text += "h 提示词-1:1 - 使用1:1比例生成图片\n"
        help_text += "h 提示词-9:16 - 使用9:16竖屏比例生成图片\n"
        help_text += "h 提示词-3:4 - 使用3:4竖屏比例生成图片\n"
        help_text += "h 提示词-润色 - 对提示词进行优化后生成图片\n"
        help_text += "h 提示词-4:3-润色 - 使用4:3比例并优化提示词生成图片\n"
        help_text += "h 提示词<反向提示词 - 使用反向提示词控制生成效果\n"
        help_text += "h 提示词-4:3-润色<反向提示词 - 组合使用所有参数\n"
        help_text += "\n参数说明：\n"
        help_text += "- 提示词：描述你想要生成的图片内容\n"
        help_text += "- 比例：可选 16:9, 4:3, 1:1, 9:16(竖屏), 3:4(竖屏)\n"
        help_text += "- 润色：优化提示词以获得更好的效果\n"
        help_text += "- 反向提示词：描述你不想在图片中出现的内容\n"
        
        # 添加登录状态信息
        if hasattr(self, 'logged_in') and self.logged_in:
            username = self.config.get("username", "")
            nickname = self.config.get("nickname", "")
            display_name = nickname or username or "用户"
            help_text += f"\n当前登录用户: {display_name}"
        else:
            help_text += "\n您尚未登录，请使用 'h 登录' 命令登录系统"
            
        return help_text
