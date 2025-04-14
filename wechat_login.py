import requests
import re
import time
import json
import os
import uuid
from io import BytesIO
from PIL import Image
from common.log import logger

class WeChatLogin:
    def __init__(self, config_path):
        """初始化微信登录模块"""
        self.appid = "wxb2df9662bac9d8dc"  # 微信开放平台AppID
        self.redirect_uri = "https://hidreamai.com/login-weixin"  # 回调地址
        self.state = self._generate_state()
        self.config_path = config_path
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://open.weixin.qq.com/"
        })
        self.uuid = None

    def _generate_state(self, length=32):
        """生成随机状态码"""
        import random
        import string
        return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

    def get_qrcode(self, save_path=None):
        """获取微信登录二维码，可选保存到本地文件
        
        Args:
            save_path: 如果提供，二维码将保存到此路径
            
        Returns:
            如果成功，返回(二维码URL, uuid)；失败则返回(None, None)
        """
        params = {
            "appid": self.appid,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "snsapi_login",
            "state": self.state,
            "login_type": "jssdk",
        }
        login_page_url = "https://open.weixin.qq.com/connect/qrconnect?" + "&".join(
            [f"{k}={v}" for k, v in params.items()])

        logger.info(f"[hid] 正在访问微信登录页面")
        try:
            response = self.session.get(login_page_url, allow_redirects=True)
            response.raise_for_status()
            html_content = response.text

            # 尝试从HTML中提取二维码UUID
            uuid_match = re.search(r'window\.QRLogin\.uuid ?= ?"([a-zA-Z0-9_=-]+)"', html_content)
            if uuid_match:
                self.uuid = uuid_match.group(1)
                logger.info(f"[hid] 成功从JS提取UUID: {self.uuid}")
            else:
                uuid_match = re.search(r'/connect/qrcode/([a-zA-Z0-9_=-]+)', html_content)
                if uuid_match:
                    self.uuid = uuid_match.group(1)
                    logger.info(f"[hid] 成功从URL提取UUID: {self.uuid}")
                else:
                    logger.error("[hid] 无法在登录页面HTML中找到UUID")
                    return None, None

            # 构造二维码图片URL
            qrcode_image_url = f"https://open.weixin.qq.com/connect/qrcode/{self.uuid}"
            logger.info(f"[hid] 获取到二维码图片URL")
            
            # 如果需要保存图片
            if save_path:
                try:
                    img_response = self.session.get(qrcode_image_url, stream=True)
                    img_response.raise_for_status()
                    
                    content_type = img_response.headers.get('Content-Type', '')
                    if not content_type.startswith('image/'):
                        logger.error(f"[hid] 获取到的不是图片，Content-Type: {content_type}")
                        return qrcode_image_url, self.uuid
                    
                    logger.info(f"[hid] 正在保存二维码图片到 {save_path}")
                    with open(save_path, 'wb') as f:
                        f.write(img_response.content)
                    logger.info(f"[hid] 二维码图片保存成功")
                except Exception as e:
                    logger.error(f"[hid] 保存二维码图片失败: {e}")
                    # 即使保存失败，仍然返回URL和UUID
            
            return qrcode_image_url, self.uuid
        except requests.exceptions.RequestException as e:
            logger.error(f"[hid] 获取登录页面或提取UUID失败: {e}")
            return None, None
        except Exception as e:
            logger.error(f"[hid] 处理登录页面时发生未知错误: {e}")
            return None, None

    def check_login_status(self):
        """检查登录状态
        
        Returns:
            dict: 包含状态码和授权码的字典
                status: 405=已授权, 404=已扫码等待确认, 403=拒绝授权, 402=二维码过期, 408=等待扫码
                code: 仅在status=405时存在，表示授权码
        """
        if not self.uuid:
            logger.error("[hid] 没有有效的UUID来检查登录状态")
            return {"status": "error", "message": "无效的UUID"}

        check_url = f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={self.uuid}&_={int(time.time()*1000)}"
        
        try:
            response = self.session.get(check_url, timeout=60, headers={
                "Referer": f"https://open.weixin.qq.com/connect/qrconnect?appid={self.appid}&scope=snsapi_login&redirect_uri={self.redirect_uri}&state={self.state}&login_type=jssdk",
                "Accept": "*/*"
            })
            response.raise_for_status()

            errcode_match = re.search(r'window\.wx_errcode=(\d+);', response.text)
            code_match = re.search(r'window\.wx_code=\'([^\']+)\';', response.text)

            if not errcode_match:
                logger.warning(f"[hid] 无法从响应中解析 wx_errcode: {response.text}")
                return {"status": "error", "message": "无法解析响应"}

            errcode = int(errcode_match.group(1))
            code = code_match.group(1) if code_match else None

            return {"status": errcode, "code": code}
        except requests.exceptions.Timeout:
            return {"status": 408}  # 408: Timeout
        except requests.exceptions.RequestException as e:
            logger.error(f"[hid] 检查登录状态时网络错误: {e}")
            return {"status": "error", "message": f"网络错误: {e}"}
        except Exception as e:
            logger.error(f"[hid] 检查登录状态时发生未知错误: {e}")
            return {"status": "error", "message": f"未知错误: {e}"}

    def monitor_login(self, interval=2, timeout=300, status_callback=None):
        """监控登录状态，返回获取到的 code 或 None
        
        Args:
            interval: 轮询间隔(秒)
            timeout: 超时时间(秒)
            status_callback: 状态回调函数，接收 status_code 参数
            
        Returns:
            str: 成功时返回授权码，失败时返回None
        """
        if not self.uuid:
            logger.error("[hid] 无法开始监控，缺少UUID")
            return None

        start_time = time.time()
        logger.info("[hid] 开始监控登录状态")

        while time.time() - start_time < timeout:
            result = self.check_login_status()

            if result is None or result.get("status") == "error":
                logger.error(f"[hid] 监控过程中出现错误: {result.get('message', '未知错误')}")
                return None

            status_code = result.get("status")
            
            # 如果有回调函数，通知状态变化
            if status_callback:
                status_callback(status_code)

            if status_code == 405:  # 扫码成功，已授权
                wx_code = result.get("code")
                if wx_code:
                    logger.info(f"[hid] 扫码并授权成功！获取到授权码")
                    return wx_code
                else:
                    logger.error("[hid] 状态码405但未获取到授权码")
                    return None
            elif status_code == 404:  # 已扫码，等待用户在手机上确认
                pass
            elif status_code == 403:  # 用户拒绝授权 / 取消登录
                logger.info("[hid] 用户取消登录或拒绝授权")
                return None
            elif status_code == 402:  # 二维码已过期
                logger.info("[hid] 二维码已过期")
                return None
            elif status_code == 408:  # 长轮询超时，继续等待
                pass
            else:  # 其他未知状态码
                logger.warning(f"[hid] 收到未知状态码: {status_code}")

            time.sleep(interval)  # 等待一段时间再查询

        logger.info("[hid] 监控超时，用户未在规定时间内扫码登录")
        return None

    def login_to_hidream(self, code):
        """使用微信 code 登录系统
        
        Args:
            code: 微信授权码
            
        Returns:
            dict: 成功时返回包含token等信息的配置字典，失败时返回None
        """
        if not code:
            logger.error("[hid] 没有有效的微信授权码用于登录")
            return None

        url = "https://hidreamai.com/prod-api/user/oauth/callback/wechat"
        request_id = str(uuid.uuid4())
        data = {
            "code": code,
            "channel_id": "qx_pc",
            "max_age": 3600,
            "request_id": request_id,
        }

        logger.info("[hid] 正在使用授权码向后端发起登录请求")
        try:
            response = self.session.post(url, json=data, headers={
                "Content-Type": "application/json",
            })
            response.raise_for_status()

            resp_data = response.json()
            if resp_data.get("code") == 0:  # 成功
                result = resp_data.get("result")
                if result:
                    logger.info("[hid] 后端登录成功！")
                    
                    # 提取关键信息
                    refresh_token = result.get("refresh_token")
                    expire_in = result.get("expire_in", 3600)
                    
                    # 获取ticket (可能在cookie或者响应中)
                    ticket = None
                    for cookie in self.session.cookies:
                        if cookie.name == "ticket":
                            ticket = cookie.value
                            break
                    
                    # 如果没有在cookie中找到ticket，可能需要再次请求
                    if not ticket:
                        logger.info("[hid] Cookie中没有ticket，尝试从响应中获取")
                    
                    # 构建cookies字典，确保包含ticket和refresh_token
                    cookies = {c.name: c.value for c in self.session.cookies}
                    
                    # 确保cookies中有refresh_token，如果不在cookie中，手动添加
                    if "refresh_token" not in cookies and refresh_token:
                        cookies["refresh_token"] = refresh_token
                        # 同时添加到session cookies
                        self.session.cookies.set("refresh_token", refresh_token)
                    
                    # 准备配置信息
                    config = {
                        "cookies": cookies,
                        "refresh_token": refresh_token,
                        "user_id": result.get("id"),
                        "expire_time": int(time.time()) + expire_in,
                        "device_id": str(uuid.uuid4()),  # 生成新的设备ID
                        "username": result.get("username"),
                        "nickname": result.get("nickname"),
                        "last_refresh": int(time.time())  # 记录首次登录时间
                    }
                    
                    # 如果cookie中没有username，手动添加
                    if "username" not in cookies and result.get("username"):
                        cookies["username"] = result.get("username")
                        self.session.cookies.set("username", result.get("username"))
                    
                    logger.info(f"[hid] 保存的cookie信息: {cookies.keys()}")
                    logger.info(f"[hid] 用户ID: {result.get('id')}, 用户名: {result.get('username')}")
                    
                    return config
                else:
                    logger.warning("[hid] 后端响应成功，但结果为空")
                    return None
            else:
                error_msg = resp_data.get("msg") or resp_data.get("message", "未知错误")
                logger.error(f"[hid] 后端登录失败: Code={resp_data.get('code')}, Message={error_msg}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"[hid] 请求后端登录接口失败: {e}")
            return None
        except json.JSONDecodeError:
            logger.error(f"[hid] 解析后端响应失败，不是有效的JSON: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[hid] 处理后端登录响应时发生未知错误: {e}")
            return None

    def login(self, qrcode_path, status_callback=None):
        """执行完整登录流程
        
        Args:
            qrcode_path: 保存二维码的路径
            status_callback: 登录状态变化的回调函数
            
        Returns:
            dict: 成功时返回配置信息，失败时返回None
        """
        # 1. 获取二维码
        logger.info("[hid] 开始微信扫码登录流程")
        qrcode_url, uuid = self.get_qrcode(qrcode_path)
        if not qrcode_url or not uuid:
            logger.error("[hid] 获取二维码失败")
            return None
        
        # 2. 监控扫码状态
        logger.info("[hid] 等待用户扫码并授权...")
        code = self.monitor_login(timeout=300, status_callback=status_callback)
        if not code:
            logger.error("[hid] 未获取到授权码，登录失败")
            return None
            
        # 3. 使用授权码登录后端
        logger.info("[hid] 使用授权码登录后端系统")
        config = self.login_to_hidream(code)
        if not config:
            logger.error("[hid] 后端登录失败")
            return None
            
        logger.info("[hid] 登录成功！")
        return config 