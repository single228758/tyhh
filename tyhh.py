import json
import logging
import requests
import os
import time
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from plugins import Plugin, Event, EventAction, EventContext, register
from common.log import logger
from PIL import Image
from io import BytesIO
import threading
import uuid
from urllib.parse import unquote
import numpy as np

@register(
    name="TYHH",
    desc="通义绘画插件",
    version="1.2",
    author="your_name",
)
class TongyiDrawingPlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.config = self._load_config()
        
        # 初始化存储路径
        storage_dir = os.path.join(os.path.dirname(__file__), "storage")
        if not os.path.exists(storage_dir):
            os.makedirs(storage_dir)
            
        temp_dir = os.path.join(os.path.dirname(__file__), "temp")
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        # 初始化图片处理器和存储器
        from .image_processor import ImageProcessor
        from .image_storage import ImageStorage
        self.image_processor = ImageProcessor(temp_dir)
        self.image_storage = ImageStorage(os.path.join(storage_dir, "images.db"))
        
        # 添加登录状态标志
        self.need_login = False
        self.login_waiting_users = {}
        self.sms_tokens = {}
        
        # Token相关
        self.last_token_check = 0
        self.xsrf_token = ""
        self.token = ""
        
        # 签到相关
        self.last_sign_in_date = self.config.get("last_sign_in_date", "")
        self.current_credits = 0
        
        # 新增: 手绘和上传状态跟踪
        self.sketch_waiting_users = {}  # 用户ID -> {"prompt": 提示词}
        self.upload_waiting_users = {}  # 用户ID -> {"prompt": 提示词}
        
        # 检查是否需要登录
        if not self.config.get("cookie", ""):
            logger.info("[TYHH] 未检测到cookie配置，需要登录")
            self.need_login = True
        else:
            # 自动签到
            self._auto_sign_in()
        
        logger.info("[TYHH] plugin initialized")

    def _load_config(self):
        """加载配置文件"""
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            else:
                # 创建默认配置文件
                default_config = {
                    "cookie": "",
                    "last_sign_in_date": "",
                    "resolutions": [
                        "1024*1024",
                        "1280*720",
                        "720*1280",
                        "1152*864",
                        "864*1152"
                    ]
                }
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(default_config, f, indent=2, ensure_ascii=False)
                    logger.info("[TYHH] 创建默认配置文件")
                return default_config
        except Exception as e:
            logger.error(f"[TYHH] Failed to load config: {e}")
            return {}
            
    def _save_config(self):
        """保存配置到文件"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
                logger.info("[TYHH] 配置文件保存成功")
        except Exception as e:
            logger.error(f"[TYHH] 保存配置文件失败: {e}")

    def get_help_text(self, **kwargs):
        help_text = "通义绘画插件使用说明：\n"
        help_text += "1. 发送 '通义 [提示词]' 生成图片\n"
        help_text += "2. 发送 '通义 [提示词] -[比例]' 生成指定比例的图片，支持的比例:\n"
        help_text += "   -1:1, -16:9, -9:16, -4:3, -3:4\n"
        help_text += "3. 发送 't [图片ID] [序号]' 查看原图\n"
        help_text += "4. 发送 '通义积分' 查询当前积分\n"
        help_text += "5. 发送 '通义手绘 [提示词] [-比例] [-风格]' 进行手绘创作\n"
        help_text += "   支持的风格: -扁平(默认), -油画, -二次元, -水彩, -3D\n"
        help_text += "6. 发送 '通义上传 [提示词]' 上传图片进行AI创作\n"
        return help_text

    def _auto_sign_in(self):
        """自动执行每日签到"""
        # 检查今天是否已经签到
        today = time.strftime("%Y-%m-%d")
        if self.last_sign_in_date == today:
            logger.info(f"[TYHH] 今日已签到: {today}")
            # 更新积分信息
            self._get_credit_info()
            return
            
        # 尝试签到
        try:
            logger.info("[TYHH] 尝试自动签到")
            self._daily_sign_in()
            # 更新最后签到日期
            self.last_sign_in_date = today
            self.config["last_sign_in_date"] = today
            self._save_config()
            # 获取最新积分
            self._get_credit_info()
        except Exception as e:
            logger.error(f"[TYHH] 自动签到失败: {e}")

    def _daily_sign_in(self):
        """执行每日签到"""
        url = 'https://wanxiang.aliyun.com/wanx/api/common/inspiration/dailySignReward'
        
        # 检查并刷新token
        current_time = time.time()
        if current_time - self.last_token_check > 3600:  # 1小时刷新一次token
            logger.info("[TYHH] 签到前刷新token")
            self._refresh_token()
            self.last_token_check = current_time
        
        # 准备请求头
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://tongyi.aliyun.com',
            'Referer': 'https://tongyi.aliyun.com/wanxiang/videoCreation',
            'sec-ch-ua': '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'x-platform': 'web',
            'Cookie': self.config.get('cookie', '')
        }
        
        # 如果有xsrf token，添加到请求头
        if self.xsrf_token:
            headers['x-xsrf-token'] = self.xsrf_token
        
        # 请求体
        data = {}
        
        try:
            logger.info(f"[TYHH] 发送签到请求: {url}")
            response = requests.post(url, headers=headers, json=data)
            
            logger.info(f"[TYHH] 签到响应状态码: {response.status_code}")
            logger.debug(f"[TYHH] 签到响应内容: {response.text}")
            
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get("success"):
                    logger.info("[TYHH] 签到成功")
                    return True
                else:
                    error_message = response_data.get("errorMsg", "")
                    logger.error(f"[TYHH] 签到失败: {error_message}")
            elif response.status_code == 401 or response.status_code == 403:
                logger.error(f"[TYHH] 签到认证失败，状态码: {response.status_code}")
                self.need_login = True
            else:
                logger.error(f"[TYHH] 签到请求失败，状态码: {response.status_code}")
                
            return False
        except Exception as e:
            logger.error(f"[TYHH] 签到过程中出错: {e}")
            return False

    def _get_credit_info(self):
        """获取账号积分信息"""
        url = 'https://wanxiang.aliyun.com/wanx/api/common/imagineCount'
        
        # 准备请求头
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://tongyi.aliyun.com',
            'Referer': 'https://tongyi.aliyun.com/wanxiang/videoCreation',
            'sec-ch-ua': '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'x-platform': 'web',
            'Cookie': self.config.get('cookie', '')
        }
        
        # 如果有xsrf token，添加到请求头
        if self.xsrf_token:
            headers['x-xsrf-token'] = self.xsrf_token
        
        # 请求体
        data = {}
        
        try:
            logger.info(f"[TYHH] 发送积分查询请求: {url}")
            response = requests.post(url, headers=headers, json=data)
            
            logger.info(f"[TYHH] 积分查询响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get("success"):
                    credit_data = response_data.get("data", {})
                    total_credits = credit_data.get("totalCount", 0)
                    available_credits = credit_data.get("availableCount", 0)
                    
                    self.current_credits = total_credits
                    
                    logger.info(f"[TYHH] 积分查询成功, 总积分: {total_credits}, 可用积分: {available_credits}")
                    return total_credits, available_credits
                else:
                    error_message = response_data.get("errorMsg", "")
                    logger.error(f"[TYHH] 积分查询失败: {error_message}")
            elif response.status_code == 401 or response.status_code == 403:
                logger.error(f"[TYHH] 积分查询认证失败，状态码: {response.status_code}")
                self.need_login = True
            else:
                logger.error(f"[TYHH] 积分查询请求失败，状态码: {response.status_code}")
                
            return 0, 0
        except Exception as e:
            logger.error(f"[TYHH] 积分查询过程中出错: {e}")
            return 0, 0

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type not in [ContextType.TEXT, ContextType.IMAGE]:
            return
            
        content = e_context["context"].content.strip()
        
        # 获取用户ID
        msg = e_context["context"].kwargs.get("msg")
        user_id = None
        if msg:
            user_id = getattr(msg, "from_user_id", None) or getattr(msg, "other_user_id", None)
            
        # 处理图片消息
        if e_context["context"].type == ContextType.IMAGE:
            # 处理手绘等待状态
            if user_id in self.sketch_waiting_users:
                try:
                    # 下载图片
                    image_path = e_context["context"].content
                    if not os.path.exists(image_path):
                        e_context["reply"] = Reply(ReplyType.TEXT, "图片下载失败，请重试")
                        e_context.action = EventAction.BREAK_PASS
                        return
                        
                    # 预处理图片
                    processed_image_path = self._preprocess_sketch_image(image_path)
                    if not processed_image_path:
                        e_context["reply"] = Reply(ReplyType.TEXT, "图片处理失败，请重试")
                        e_context.action = EventAction.BREAK_PASS
                        return
                    
                    user_data = self.sketch_waiting_users[user_id]
                    prompt = user_data["prompt"]
                    resolution = user_data["resolution"]
                    style = user_data["style"]
                    
                    # 发送等待消息
                    wait_reply = Reply(ReplyType.TEXT, "正在处理您的手绘作品，请稍候......")
                    e_context["channel"].send(wait_reply, e_context["context"])
                    
                    # 上传处理后的图片到OSS
                    oss_url = self._upload_image_to_oss(processed_image_path, "sketch_to_image")
                    if not oss_url:
                        raise Exception("图片上传失败")
                        
                    # 清理临时文件
                    try:
                        os.remove(processed_image_path)
                    except:
                        pass
                    
                    # 提交任务
                    task_id = self._send_image_gen_request(
                        self._get_headers(),
                        prompt,
                        resolution,
                        task_type="sketch_to_image",
                        base_image=oss_url,
                        style=style
                    )
                    
                    if not task_id:
                        raise Exception("创建任务失败")
                        
                    # 获取结果
                    original_params = {
                        "prompt": prompt,
                        "resolution": resolution,
                        "task_type": "sketch_to_image",
                        "base_image": oss_url,
                        "style": style
                    }
                    task_result = self._get_task_result(self._get_headers(), task_id, original_params)
                    if not task_result:
                        raise Exception("获取结果失败")
                        
                    # 提取URL并保存
                    download_urls = []
                    for item in task_result:
                        url = item.get("downloadUrl")
                        if url:
                            download_urls.append(url)
                            
                    if not download_urls:
                        raise Exception("未获取到生成图片")
                        
                    # 获取当前积分信息
                    total_credits, _ = self._get_credit_info()
                    
                    # 存储图片信息
                    img_id = str(int(time.time()))
                    self.image_storage.store_image(
                        img_id,
                        download_urls,
                        metadata={
                            "prompt": prompt,
                            "type": "sketch",
                            "style": style,
                            "resolution": resolution
                        }
                    )
                    
                    # 合并并发送图片
                    if len(download_urls) >= 4:
                        if not self._combine_and_send_images(download_urls, e_context, total_credits, img_id):
                            # 如果合并失败,发送单张图片
                            logger.warning("[TYHH] 图片合并失败，发送单张图片")
                            for url in download_urls:
                                e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                            help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                            e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                    else:
                        # 直接发送单张图片
                        logger.info(f"[TYHH] 图片数量少于4张，直接发送 {len(download_urls)} 张单图")
                        for url in download_urls:
                            e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                        help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                        e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                except Exception as e:
                    logger.error(f"[TYHH] 处理手绘图片失败: {e}")
                    e_context["reply"] = Reply(ReplyType.TEXT, f"处理失败: {str(e)}")
                    
                finally:
                    # 清理状态
                    self.sketch_waiting_users.pop(user_id, None)
                    e_context.action = EventAction.BREAK_PASS
                return
                
            # 处理上传等待状态
            elif user_id in self.upload_waiting_users:
                try:
                    # 下载图片
                    image_path = e_context["context"].content
                    if not os.path.exists(image_path):
                        e_context["reply"] = Reply(ReplyType.TEXT, "图片下载失败，请重试")
                        e_context.action = EventAction.BREAK_PASS
                        return
                        
                    prompt = self.upload_waiting_users[user_id]["prompt"]
                    
                    # 发送等待消息
                    wait_reply = Reply(ReplyType.TEXT, "正在处理您上传的图片，请稍候......")
                    e_context["channel"].send(wait_reply, e_context["context"])
                    
                    # 上传图片到OSS
                    oss_url = self._upload_image_to_oss(image_path, "text_to_image_v2")
                    if not oss_url:
                        raise Exception("图片上传失败")
                        
                    # 提交任务
                    task_id = self._send_image_gen_request(
                        self._get_headers(),
                        prompt,
                        "1024*1024",
                        task_type="text_to_image_v2",
                        base_image=oss_url
                    )
                    
                    if not task_id:
                        raise Exception("创建任务失败")
                        
                    # 获取结果
                    original_params = {
                        "prompt": prompt,
                        "resolution": "1024*1024",
                        "task_type": "text_to_image_v2",
                        "base_image": oss_url
                    }
                    task_result = self._get_task_result(self._get_headers(), task_id, original_params)
                    if not task_result:
                        raise Exception("获取结果失败")
                        
                    # 提取URL并保存
                    download_urls = []
                    for item in task_result:
                        url = item.get("downloadUrl")
                        if url:
                            download_urls.append(url)
                            
                    if not download_urls:
                        raise Exception("未获取到生成图片")
                        
                    # 存储图片信息
                    img_id = str(int(time.time()))
                    self.image_storage.store_image(
                        img_id,
                        download_urls,
                        metadata={
                            "prompt": prompt,
                            "type": "upload"
                        }
                    )
                    
                    # 合并并发送图片
                    if len(download_urls) >= 4:
                        if not self._combine_and_send_images(download_urls, e_context, total_credits, img_id):
                            # 如果合并失败,发送单张图片
                            logger.warning("[TYHH] 图片合并失败，发送单张图片")
                            for url in download_urls:
                                e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                            help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                            e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                    else:
                        # 如果合并失败,发送单张图片
                        logger.warning("[TYHH] 图片合并失败，发送单张图片")
                        for url in download_urls:
                            e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                        help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                        e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                except Exception as e:
                    logger.error(f"[TYHH] 处理上传图片失败: {e}")
                    e_context["reply"] = Reply(ReplyType.TEXT, f"处理失败: {str(e)}")
                    
                finally:
                    # 清理状态
                    self.upload_waiting_users.pop(user_id, None)
                    e_context.action = EventAction.BREAK_PASS
                return
                
        # 处理文本消息
        if e_context["context"].type != ContextType.TEXT:
            return
            
        # 处理登录流程
        if self.need_login:
            # 如果是第一次遇到需要登录的情况，并且没有等待手机号的用户
            if user_id and user_id not in self.login_waiting_users:
                self.login_waiting_users[user_id] = "phone"
                login_msg = "通义绘画插件需要登录。\n请输入您的手机号码以接收验证码："
                e_context["reply"] = Reply(ReplyType.TEXT, login_msg)
                e_context.action = EventAction.BREAK_PASS
                return
            
            # 如果用户正在等待输入手机号
            if user_id in self.login_waiting_users and self.login_waiting_users[user_id] == "phone":
                # 检查输入是否是有效的手机号
                if len(content) == 11 and content.isdigit():
                    # 发送验证码
                    try:
                        sms_token = self._send_sms_code(content)
                        if sms_token:
                            self.sms_tokens[user_id] = {"phone": content, "token": sms_token}
                            self.login_waiting_users[user_id] = "sms"
                            e_context["reply"] = Reply(ReplyType.TEXT, "验证码已发送，请输入收到的6位验证码")
                            e_context.action = EventAction.BREAK_PASS
                            return
                        else:
                            e_context["reply"] = Reply(ReplyType.TEXT, "发送验证码失败，请重试")
                            self.login_waiting_users.pop(user_id, None)
                    except Exception as e:
                        logger.error(f"[TYHH] 发送验证码失败: {e}")
                        e_context["reply"] = Reply(ReplyType.TEXT, f"发送验证码失败: {str(e)}")
                        self.login_waiting_users.pop(user_id, None)
                else:
                    e_context["reply"] = Reply(ReplyType.TEXT, "请输入11位手机号码")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 如果用户正在等待输入验证码
            elif user_id in self.login_waiting_users and self.login_waiting_users[user_id] == "sms":
                # 检查验证码格式
                if len(content) == 6 and content.isdigit():
                    # 使用验证码登录
                    try:
                        if user_id in self.sms_tokens:
                            phone = self.sms_tokens[user_id]["phone"]
                            sms_token = self.sms_tokens[user_id]["token"]
                            
                            cookie = self._login_with_sms(phone, content, sms_token)
                            if cookie:
                                # 登录成功，更新配置
                                self.config["cookie"] = cookie
                                self._save_config()
                                
                                # 清理登录状态
                                self.need_login = False
                                self.login_waiting_users.pop(user_id, None)
                                self.sms_tokens.pop(user_id, None)
                                
                                # 登录成功后进行签到
                                self._auto_sign_in()
                                
                                e_context["reply"] = Reply(ReplyType.TEXT, "登录成功！现在可以使用通义绘画了")
                                e_context.action = EventAction.BREAK_PASS
                                return
                            else:
                                e_context["reply"] = Reply(ReplyType.TEXT, "登录失败，请重试")
                                self.login_waiting_users.pop(user_id, None)
                                self.sms_tokens.pop(user_id, None)
                        else:
                            e_context["reply"] = Reply(ReplyType.TEXT, "登录状态已失效，请重新获取验证码")
                            self.login_waiting_users.pop(user_id, None)
                    except Exception as e:
                        logger.error(f"[TYHH] 登录过程中出错: {e}")
                        e_context["reply"] = Reply(ReplyType.TEXT, f"登录失败: {str(e)}")
                        self.login_waiting_users.pop(user_id, None)
                        self.sms_tokens.pop(user_id, None)
                else:
                    e_context["reply"] = Reply(ReplyType.TEXT, "请输入6位验证码")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 如果需要登录但无法获取用户ID，则提示错误
            if not user_id:
                e_context["reply"] = Reply(ReplyType.TEXT, "无法获取用户ID，请联系管理员")
                e_context.action = EventAction.BREAK_PASS
                return
        
        # 处理查询积分命令
        if content == "通义积分":
            try:
                total_credits, available_credits = self._get_credit_info()
                if total_credits > 0:
                    e_context["reply"] = Reply(ReplyType.TEXT, f"账号积分信息：\n总积分：{total_credits}\n可用积分：{available_credits}")
                else:
                    e_context["reply"] = Reply(ReplyType.TEXT, "获取积分信息失败，请稍后重试")
                e_context.action = EventAction.BREAK_PASS
                return
            except Exception as e:
                logger.error(f"[TYHH] 处理积分查询命令出错: {e}")
                e_context["reply"] = Reply(ReplyType.TEXT, f"查询积分失败: {str(e)}")
                e_context.action = EventAction.BREAK_PASS
                return
        
        # 处理放大图片命令
        if content.startswith("t "):
            self._handle_enlarge_command(content, e_context)
            return

        # 处理手绘命令
        if content.startswith("通义手绘"):
            if not user_id:
                e_context["reply"] = Reply(ReplyType.TEXT, "无法获取用户ID")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 检查是否已登录
            if self.need_login:
                e_context["reply"] = Reply(ReplyType.TEXT, "请先完成登录后再使用通义手绘功能")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 解析命令参数
            prompt, resolution, style = self._parse_sketch_command(content)
            
            if not prompt:
                e_context["reply"] = Reply(ReplyType.TEXT, "请输入绘画提示词")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 创建对应分辨率的空白图片
            blank_image_path = self._create_blank_image(resolution)
            if not blank_image_path:
                e_context["reply"] = Reply(ReplyType.TEXT, "创建空白图片失败")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 记录用户状态和参数
            self.sketch_waiting_users[user_id] = {
                "prompt": prompt,
                "resolution": resolution,
                "style": style
            }
            
            # 发送空白图片和提示
            try:
                with open(blank_image_path, 'rb') as f:
                    image_reply = Reply(ReplyType.IMAGE, f)
                    e_context["channel"].send(image_reply, e_context["context"])
                e_context["reply"] = Reply(ReplyType.TEXT, f"请在{resolution.replace('*', 'x')}的空白图片上进行涂鸦，完成后发送给我")
            except Exception as e:
                logger.error(f"[TYHH] 发送空白图片失败: {e}")
                e_context["reply"] = Reply(ReplyType.TEXT, "创建空白画布失败，请重试")
            finally:
                # 清理临时文件
                try:
                    os.remove(blank_image_path)
                except:
                    pass
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 处理上传命令
        if content.startswith("通义上传"):
            if not user_id:
                e_context["reply"] = Reply(ReplyType.TEXT, "无法获取用户ID")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 检查是否已登录
            if self.need_login:
                e_context["reply"] = Reply(ReplyType.TEXT, "请先完成登录后再使用通义上传功能")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 提取提示词
            prompt = content[4:].strip()
            if not prompt:
                e_context["reply"] = Reply(ReplyType.TEXT, "请输入绘画提示词")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 记录用户状态
            self.upload_waiting_users[user_id] = {"prompt": prompt}
            
            # 发送提示
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要处理的图片")
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 处理生成图片命令
        if content.startswith("通义"):
            # 解析命令
            prompt = content[2:].strip()
            resolution = "1024*1024"  # 默认分辨率1:1
            
            # 检查是否包含分辨率参数
            ratio_mapping = {
                "-1:1": "1024*1024",
                "-16:9": "1280*720",
                "-9:16": "720*1280",
                "-4:3": "1152*864",
                "-3:4": "864*1152"
            }
            
            # 检查简写形式
            for ratio, res in ratio_mapping.items():
                if prompt.endswith(ratio):
                    resolution = res
                    prompt = prompt[:-len(ratio)].strip()
                    logger.info(f"[TYHH] 检测到分辨率参数: {ratio}, 使用分辨率: {resolution}")
                    break
            
            if not prompt:
                e_context["reply"] = Reply(ReplyType.ERROR, "请输入绘画提示词")
                e_context.action = EventAction.BREAK_PASS
                return

            # 检查是否已登录
            if self.need_login:
                e_context["reply"] = Reply(ReplyType.TEXT, "请先完成登录后再使用通义绘画功能")
                e_context.action = EventAction.BREAK_PASS
                return

            try:
                # 发送等待消息
                wait_reply = Reply(ReplyType.TEXT, "通义正在绘画,请稍候......")
                e_context["channel"].send(wait_reply, e_context["context"])
                
                # 检查并刷新token
                current_time = time.time()
                if current_time - self.last_token_check > 3600:  # 1小时刷新一次token
                    logger.info("[TYHH] Token超过1小时未刷新，进行刷新")
                    refresh_result = self._refresh_token()
                    self.last_token_check = current_time
                    logger.info(f"[TYHH] Token刷新结果: {refresh_result}")
                
                # 准备请求头
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                    'Content-Type': 'application/json',
                    'Origin': 'https://tongyi.aliyun.com',
                    'Referer': 'https://tongyi.aliyun.com/wanxiang/creation',
                    'x-platform': 'web',
                    'Cookie': self.config.get('cookie', '')
                }
                
                # 如果有xsrf token，添加到请求头
                if self.xsrf_token:
                    headers['x-xsrf-token'] = self.xsrf_token
                    logger.info(f"[TYHH] 使用现有的xsrf-token: {self.xsrf_token}")
                
                # 生成图片
                logger.info(f"[TYHH] 开始生成图片，提示词: {prompt}，分辨率: {resolution}")
                task_id = self._send_image_gen_request(headers, prompt, resolution)
                
                # 如果请求失败且疑似cookie失效，尝试刷新token
                if not task_id:
                    logger.info("[TYHH] 尝试刷新token并重新提交请求")
                    self._refresh_token()
                    # 更新headers中的cookie
                    headers['Cookie'] = self.config.get('cookie', '')
                    if self.xsrf_token:
                        headers['x-xsrf-token'] = self.xsrf_token
                    # 重新尝试生成图片
                    task_id = self._send_image_gen_request(headers, prompt, resolution)
                    
                # 如果仍然失败，标记需要登录，并让用户知道
                if not task_id:
                    logger.error("[TYHH] 图片生成请求两次尝试均失败，需要重新登录")
                    self.need_login = True
                    
                    if user_id:
                        # 清除登录等待状态，以便重新开始登录流程
                        if user_id in self.login_waiting_users:
                            self.login_waiting_users.pop(user_id)
                        if user_id in self.sms_tokens:
                            self.sms_tokens.pop(user_id)
                    
                    e_context["reply"] = Reply(
                        ReplyType.TEXT, 
                        "图片生成失败，登录凭证已过期，需要重新登录。\n请输入手机号码以接收验证码："
                    )
                    self.login_waiting_users[user_id] = "phone"
                    e_context.action = EventAction.BREAK_PASS
                    return
                    
                # 获取任务结果
                logger.info(f"[TYHH] 成功提交任务，任务ID: {task_id}，等待结果")
                original_params = {
                    "prompt": prompt,
                    "resolution": resolution,
                    "task_type": "text_to_image_v2"
                }
                task_result = self._get_task_result(headers, task_id, original_params)
                if not task_result:
                    logger.error(f"[TYHH] 获取任务 {task_id} 结果失败")
                    e_context["reply"] = Reply(ReplyType.TEXT, "获取图片结果失败，请稍后重试")
                    e_context.action = EventAction.BREAK_PASS
                    return
                    
                # 提取下载URL
                logger.info(f"[TYHH] 成功获取任务结果，开始提取图片URL")
                download_urls = []
                for item in task_result:
                    url = item.get("downloadUrl")
                    if url:
                        download_urls.append(url)
                
                if not download_urls:
                    logger.error("[TYHH] 未从任务结果中获取到图片URL")
                    e_context["reply"] = Reply(ReplyType.TEXT, "未获取到图片URL")
                    e_context.action = EventAction.BREAK_PASS
                    return
                    
                # 存储图片信息
                logger.info(f"[TYHH] 成功获取 {len(download_urls)} 张图片的URL，开始存储图片信息")
                img_id = str(int(time.time()))
                self.image_storage.store_image(
                    img_id,
                    download_urls,
                    metadata={
                        "prompt": prompt,
                        "type": "generate"
                    }
                )
                
                logger.info(f"[TYHH] 图片信息存储成功，图片ID: {img_id}")
                
                # 查询当前积分
                total_credits, _ = self._get_credit_info()
                
                # 合并图片并发送
                if len(download_urls) >= 4:
                    if not self._combine_and_send_images(download_urls, e_context, total_credits, img_id):
                        # 如果合并失败,发送单张图片
                        logger.warning("[TYHH] 图片合并失败，发送单张图片")
                        for url in download_urls:
                            e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                        help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                        e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                else:
                    # 直接发送单张图片
                    logger.info(f"[TYHH] 图片数量少于4张，直接发送 {len(download_urls)} 张单图")
                    for url in download_urls:
                        e_context["channel"].send(Reply(ReplyType.IMAGE_URL, url), e_context["context"])
                    help_text = f"图片生成成功！账号积分：{total_credits}\n图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                    e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                
                e_context.action = EventAction.BREAK_PASS
                
            except Exception as e:
                logger.error(f"[TYHH] 生成图片过程中出错: {e}")
                e_context["reply"] = Reply(ReplyType.TEXT, f"生成图片失败: {str(e)}")
                e_context.action = EventAction.BREAK_PASS

    def _handle_enlarge_command(self, content, e_context):
        """处理放大图片命令"""
        try:
            # 解析命令参数
            parts = content.strip().split()
            if len(parts) != 2:
                e_context["reply"] = Reply(ReplyType.TEXT, "请使用正确的格式：'t 图片ID 序号'")
                e_context.action = EventAction.BREAK_PASS
                return
                
            img_id = parts[0]
            index = int(parts[1]) - 1
            
            # 从数据库获取图片信息
            image_info = self.image_storage.get_image(img_id)
            if not image_info:
                e_context["reply"] = Reply(ReplyType.TEXT, "未找到对应的图片记录")
                e_context.action = EventAction.BREAK_PASS
                return
                
            urls = image_info.get("urls", [])
            if not urls or index >= len(urls):
                e_context["reply"] = Reply(ReplyType.TEXT, "图片序号无效")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 获取原始图片URL
            original_url = urls[index]
            
            # 发送等待消息
            wait_reply = Reply(ReplyType.TEXT, "正在处理放大请求，请稍候......")
            e_context["channel"].send(wait_reply, e_context["context"])
            
            # 提交放大任务
            task_id = self._send_image_gen_request(
                self._get_headers(),
                "",  # 放大时不需要prompt
                resolution="2048*2048",  # 放大到更高分辨率
                task_type="image_upscale",
                base_image=original_url
            )
            
            if not task_id:
                e_context["reply"] = Reply(ReplyType.TEXT, "创建放大任务失败")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 获取任务结果
            original_params = {
                "resolution": "2048*2048",
                "task_type": "image_upscale",
                "base_image": original_url
            }
            task_result = self._get_task_result(self._get_headers(), task_id, original_params)
            
            if not task_result:
                e_context["reply"] = Reply(ReplyType.TEXT, "获取放大结果失败")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 提取放大后的图片URL
            enlarged_urls = []
            for item in task_result:
                url = item.get("downloadUrl")
                if url:
                    enlarged_urls.append(url)
                    
            if not enlarged_urls:
                e_context["reply"] = Reply(ReplyType.TEXT, "未获取到放大后的图片")
                e_context.action = EventAction.BREAK_PASS
                return
                
            # 存储放大后的图片信息
            enlarged_img_id = f"{img_id}_enlarged_{int(time.time())}"
            self.image_storage.store_image(
                enlarged_img_id,
                enlarged_urls,
                metadata={
                    "type": "enlarged",
                    "original_id": img_id,
                    "original_index": index
                }
            )
            
            # 发送放大后的图片
            for url in enlarged_urls:
                self._send_image_url(url, e_context)
                
            # 发送提示信息
            help_text = f"图片放大成功！\n放大后图片ID: {enlarged_img_id}"
            e_context["reply"] = Reply(ReplyType.TEXT, help_text)
            e_context.action = EventAction.BREAK_PASS
            
        except Exception as e:
            logger.error(f"[TYHH] 处理放大命令时出错: {str(e)}")
            e_context["reply"] = Reply(ReplyType.TEXT, "处理放大请求时出错")
            e_context.action = EventAction.BREAK_PASS

    def _refresh_token(self):
        """使用新API获取token并更新cookie"""
        try:
            # 生成XSRF-Token
            xsrf_token = str(uuid.uuid4())
            self.xsrf_token = xsrf_token
            
            logger.info(f"[TYHH] 开始刷新token，生成xsrf-token: {xsrf_token}")
            
            # 准备请求头
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Content-Type': 'application/json',
                'Origin': 'https://tongyi.aliyun.com',
                'Referer': 'https://tongyi.aliyun.com/qianwen/',
                'sec-ch-ua': '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'x-platform': 'pc_tongyi',
                'x-xsrf-token': xsrf_token,
                'Cookie': self.config.get('cookie', '')
            }
            
            # 请求体
            data = {"channelId":"","source":"notify"}
            
            logger.info("[TYHH] 发送token获取请求")
            logger.debug(f"[TYHH] Headers: {headers}")
            
            # 发送请求
            response = requests.post(
                'https://qianwen.biz.aliyun.com/dialog/im/getToken',
                headers=headers,
                json=data
            )
            
            logger.info(f"[TYHH] token获取响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    # 获取token
                    token = result['data']['token']
                    self.token = token
                    
                    logger.info(f"[TYHH] 成功获取token: {token[:10]}...")
                    
                    # 如果已有cookie，将token添加到cookie中
                    current_cookie = self.config.get('cookie', '')
                    if current_cookie:
                        # 提取当前cookie中的其他值
                        self.config['cookie'] = current_cookie
                        # 添加或更新token到cookie中的相关字段
                        self._update_cookie_with_token(token)
                    else:
                        # 使用token创建新cookie
                        self._fetch_cookie_with_token(token)
                    
                    # 保存配置
                    self._save_config()
                    logger.info("[TYHH] Token刷新成功")
                    return True
                else:
                    logger.error(f"[TYHH] Token刷新失败: {result.get('errorMsg')}")
            else:
                logger.error(f"[TYHH] Token刷新请求失败，状态码: {response.status_code}")
                
            return False
        except Exception as e:
            logger.error(f"[TYHH] 刷新token时出错: {e}")
            return False
            
    def _update_cookie_with_token(self, token):
        """使用token更新现有cookie"""
        # 这里需要访问通义绘画页面，将返回的cookie与当前cookie合并
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Authorization': f'Bearer {token}',
                'Cookie': self.config.get('cookie', '')
            }
            
            logger.info(f"[TYHH] 尝试使用token更新cookie")
            
            # 访问绘画页面，获取完整cookie
            response = requests.get(
                'https://wanxiang.aliyun.com/wanx/api/common/imagineCount',
                headers=headers
            )
            
            logger.info(f"[TYHH] 更新cookie响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                # 获取响应中的cookie
                cookies = response.cookies
                cookie_str = self.config.get('cookie', '')
                
                # 更新cookie
                for key, value in cookies.items():
                    if key + '=' in cookie_str:
                        # 如果cookie中已包含这个key，更新它
                        parts = cookie_str.split('; ')
                        new_parts = []
                        for part in parts:
                            if part.startswith(key + '='):
                                new_parts.append(f"{key}={value}")
                            else:
                                new_parts.append(part)
                        cookie_str = '; '.join(new_parts)
                    else:
                        # 否则添加这个key
                        if cookie_str:
                            cookie_str += f"; {key}={value}"
                        else:
                            cookie_str = f"{key}={value}"
                
                # 保存更新后的cookie
                self.config['cookie'] = cookie_str
                logger.info("[TYHH] Cookie已使用token成功更新")
            else:
                logger.error(f"[TYHH] 使用token更新cookie失败，状态码: {response.status_code}")
        except Exception as e:
            logger.error(f"[TYHH] 使用token更新cookie时出错: {e}")

    def _fetch_cookie_with_token(self, token):
        """使用token获取完整cookie"""
        try:
            logger.info(f"[TYHH] 尝试使用token获取完整cookie")
            
            # 使用初始cookie访问绘画页面获取完整cookie
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Authorization": f'Bearer {token}'
            }
            
            # 访问一个需要授权的页面
            response = requests.get(
                "https://wanxiang.aliyun.com/wanx/api/common/imagineCount",
                headers=headers,
                allow_redirects=False
            )
            
            logger.info(f"[TYHH] 获取cookie响应状态码: {response.status_code}")
            
            # 提取所有cookie
            cookies = response.cookies
            cookie_str = f"tongyi_token={token}"
            
            if cookies:
                for key, value in cookies.items():
                    if key not in cookie_str:
                        cookie_str += f"; {key}={value}"
                
                # 保存cookie
                self.config['cookie'] = cookie_str
                logger.info("[TYHH] 已成功使用token获取完整cookie")
                return cookie_str
            
            logger.warning("[TYHH] 未能从响应中获取cookie，使用基础token cookie")
            return cookie_str
        except Exception as e:
            logger.error(f"[TYHH] 获取完整cookie时出错: {e}")
            # 返回基础cookie
            return f"tongyi_token={token}"
            
    def _send_sms_code(self, phone):
        """发送短信验证码"""
        url = "https://tongyi-passport.aliyun.com/havanaone/loginLegacy/sms/sendSms.do"
        
        # 公共请求头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://tongyi-passport.aliyun.com",
            "Referer": "https://tongyi-passport.aliyun.com/havanaone/login/login.htm"
        }
        
        # 请求参数
        params = {
            "bizEntrance": "tongyi",
            "bizName": "tongyi"
        }
        
        # 表单数据
        data = {
            "phoneCode": "86",
            "loginId": phone,
            "countryCode": "CN",
            "codeLength": "6",
            "isIframe": "true",
            "bizEntrance": "tongyi",
            "bizName": "tongyi",
            "_csrf": "2d54169f5ec1cc2a9eb32ee90a25cb9f"
        }
        
        try:
            logger.info(f"[TYHH] 尝试向手机号 {phone} 发送验证码")
            response = requests.post(url, headers=headers, params=params, data=data)
            
            logger.info(f"[TYHH] 验证码发送响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                if not result.get("hasError", True):
                    # 返回smsToken
                    logger.info(f"[TYHH] 验证码发送成功")
                    return result["content"]["data"]["smsToken"]
                    
            logger.error(f"[TYHH] 验证码发送请求失败: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[TYHH] 发送验证码过程中出错: {e}")
            return None
            
    def _login_with_sms(self, phone, sms_code, sms_token):
        """使用短信验证码登录"""
        url = "https://tongyi-passport.aliyun.com/havanaone/loginLegacy/sms/login.do"
        
        # 公共请求头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://tongyi-passport.aliyun.com",
            "Referer": "https://tongyi-passport.aliyun.com/havanaone/login/login.htm"
        }
        
        # 请求参数
        params = {
            "bizEntrance": "tongyi",
            "bizName": "tongyi"
        }
        
        # 表单数据
        data = {
            "loginId": phone,
            "phoneCode": "86",
            "countryCode": "CN",
            "smsCode": sms_code,
            "smsToken": sms_token,
            "keepLogin": "false",
            "isIframe": "true",
            "bizEntrance": "tongyi",
            "bizName": "tongyi",
            "_csrf": "2d54169f5ec1cc2a9eb32ee90a25cb9f"
        }
        
        try:
            logger.info(f"[TYHH] 尝试使用短信验证码登录: {phone}")
            response = requests.post(url, headers=headers, params=params, data=data)
            
            logger.info(f"[TYHH] 短信登录响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                if not result.get("hasError", True):
                    # 获取Cookie中的tongyi_sso_ticket
                    sso_ticket = result["content"]["data"]["tongyi_sso_ticket"]
                    
                    logger.info(f"[TYHH] 短信登录成功，获取到sso_ticket")
                    
                    # 构建完整的cookie字符串
                    cookie = f"tongyi_sso_ticket={sso_ticket}"
                    
                    # 使用获取到的cookie获取完整cookie
                    full_cookie = self._get_full_cookie(cookie)
                    if full_cookie:
                        return full_cookie
                    return cookie
                    
            logger.error(f"[TYHH] 登录失败: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[TYHH] 登录过程中出错: {e}")
            return None
            
    def _get_full_cookie(self, initial_cookie):
        """使用初始cookie获取完整cookie"""
        try:
            logger.info(f"[TYHH] 尝试使用初始cookie获取完整cookie")
            
            # 使用初始cookie访问绘画页面获取完整cookie
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Cookie": initial_cookie
            }
            
            # 访问一个需要授权的页面
            response = requests.get(
                "https://wanxiang.aliyun.com/wanx/api/common/imagineCount",
                headers=headers,
                allow_redirects=False
            )
            
            logger.info(f"[TYHH] 获取完整cookie响应状态码: {response.status_code}")
            
            # 提取所有cookie
            cookies = response.cookies
            cookie_str = initial_cookie
            
            if cookies:
                for key, value in cookies.items():
                    if key not in cookie_str:
                        cookie_str += f"; {key}={value}"
                
                logger.info(f"[TYHH] 成功获取完整cookie")
                return cookie_str
            
            logger.info(f"[TYHH] 未能获取额外cookie，使用初始cookie")
            return initial_cookie
        except Exception as e:
            logger.error(f"[TYHH] 获取完整cookie时出错: {e}")
            return initial_cookie

    def generate_images(self, prompt, resolution="1024*1024"):
        """生成图片"""
        # 检查并刷新token
        current_time = time.time()
        if current_time - self.last_token_check > 3600:  # 1小时刷新一次token
            self._refresh_token()
            self.last_token_check = current_time
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://tongyi.aliyun.com',
            'Referer': 'https://tongyi.aliyun.com/wanxiang/creation',
            'x-platform': 'web',
            'Cookie': self.config.get('cookie', '')
        }
        
        if self.xsrf_token:
            headers['x-xsrf-token'] = self.xsrf_token

        # 发送绘画请求
        task_id = self._send_image_gen_request(headers, prompt, resolution)
        if not task_id:
            # 尝试刷新token后重试
            self._refresh_token()
            headers['Cookie'] = self.config.get('cookie', '')
            task_id = self._send_image_gen_request(headers, prompt, resolution)
            if not task_id:
                return []

        # 获取任务结果
        task_result = self._get_task_result(headers, task_id)
        if not task_result:
            return []

        # 提取图片URL
        return self._extract_high_quality_image_urls(task_result)

    def _send_image_gen_request(self, headers, prompt, resolution="1024*1024", task_type="text_to_image_v2", base_image=None, style=None):
        """发送图片生成请求"""
        max_retries = 3
        current_retry = 0
        
        while current_retry < max_retries:
            try:
                url = "https://wanxiang.aliyun.com/wanx/api/common/imageGen"
                
                # 更新headers
                headers.update({
                    "x-platform": "web",
                    "x-xsrf-token": self._get_xsrf_token(),
                    "content-type": "application/json",
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "origin": "https://tongyi.aliyun.com",
                    "referer": "https://tongyi.aliyun.com/wanxiang/app/doodle",
                    "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site"
                })
                
                task_input = {
                    "prompt": prompt,
                    "resolution": resolution
                }
                
                if style:
                    task_input["style"] = style
                    task_input["styleName"] = self._get_style_name(style)
                    
                if base_image:
                    task_input["baseImage"] = base_image
                    
                payload = {
                    "taskType": task_type,
                    "taskInput": task_input
                }
                
                logger.info(f"[TYHH] 发送请求到 {url}")
                logger.info(f"[TYHH] Headers: {headers}")
                logger.info(f"[TYHH] Payload: {payload}")
                
                response = requests.post(url, headers=headers, json=payload, timeout=30)
                logger.info(f"[TYHH] 图片生成响应状态码: {response.status_code}")
                logger.info(f"[TYHH] 响应内容: {response.text[:200]}")
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success"):
                        task_id = result.get("data")
                        logger.info(f"[TYHH] 成功创建新任务: {task_id}")
                        return task_id
                    else:
                        error_msg = result.get("errorMsg", "未知错误")
                        if "人数较多" in error_msg or "请稍后再试" in error_msg:
                            logger.warning(f"[TYHH] 服务繁忙: {error_msg}")
                            time.sleep(5)
                            current_retry += 1
                            continue
                        else:
                            logger.error(f"[TYHH] 创建任务失败: {error_msg}")
                            return None
                
                current_retry += 1
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"[TYHH] 发送请求出错: {str(e)}")
                current_retry += 1
                time.sleep(3)
                
        return None

    def _get_xsrf_token(self):
        """获取新的XSRF token"""
        cookie = self.config.get("cookie", "")
        for item in cookie.split(";"):
            item = item.strip()
            if item.startswith("XSRF-TOKEN="):
                return item.split("=")[1].strip()
        return ""

    def _get_style_name(self, style):
        """获取风格名称"""
        style_map = {
            "<flat illustration>": "扁平插画",
            "<oil painting>": "油画",
            "<anime>": "二次元",
            "<watercolor>": "水彩",
            "<3d cartoon>": "3D卡通"
        }
        return style_map.get(style, "")

    def _get_task_result(self, headers, task_id, original_params=None):
        """获取任务结果"""
        max_retries = 30  # 最大重试次数
        retry_count = 0
        interval = 10  # 轮询间隔
        zero_progress_count = 0  # 连续0%进度计数
        
        while retry_count < max_retries:
            try:
                url = "https://wanxiang.aliyun.com/wanx/api/common/taskResult"
                payload = {
                    "taskId": task_id,
                    "id": original_params.get("id") if original_params else None
                }
                
                response = requests.post(url, headers=headers, json=payload)
                if response.status_code != 200:
                    logger.error(f"[TYHH] 任务查询失败,状态码: {response.status_code}")
                    return None
                    
                result = response.json()
                if not result.get("success"):
                    logger.error(f"[TYHH] 任务查询响应错误: {result}")
                    return None
                    
                task_data = result.get("data", {})
                progress = task_data.get("taskRate", 0)
                status = task_data.get("status")
                
                logger.info(f"[TYHH] 任务进度: {progress}%")
                
                # 检查任务状态
                if progress == 100 or status == 2:  # 成功完成
                    return task_data.get("taskResult", [])
                elif status == 3:  # 失败
                    logger.error(f"[TYHH] 任务失败: {result}")
                    return None
                    
                # 检查连续0%进度
                if progress == 0:
                    zero_progress_count += 1
                    if zero_progress_count >= 2:  # 连续两次0%进度
                        logger.warning("[TYHH] 连续两次0%进度，任务可能被拒绝")
                        return None
                else:
                    zero_progress_count = 0  # 重置计数器
                    
                retry_count += 1
                time.sleep(interval)
                
            except Exception as e:
                logger.error(f"[TYHH] 查询任务出错: {str(e)}")
                retry_count += 1
                if retry_count >= max_retries:
                    return None
                time.sleep(interval)
                
        logger.error("[TYHH] 任务超时")
        return None

    def _extract_high_quality_image_urls(self, task_result):
        """提取高质量图片URL"""
        try:
            high_quality_urls = []
            if not task_result:
                return high_quality_urls
            
            logger.info(f"[TYHH] 开始提取图片URL")
            
            for index, result in enumerate(task_result):
                # 使用downloadUrl获取无水印原图
                download_url = result.get("downloadUrl", "").split("?")[0]  # 去除URL参数
                if download_url:
                    high_quality_urls.append(download_url)
                    logger.info(f"[TYHH] 成功提取第 {index+1} 张图片的URL: {download_url[:50]}...")
                else:
                    logger.warning(f"[TYHH] 第 {index+1} 张图片未找到downloadUrl")
            
            logger.info(f"[TYHH] 成功提取 {len(high_quality_urls)} 个图片URL")
            return high_quality_urls
        except Exception as e:
            logger.error(f"[TYHH] 提取图片URL时出错: {e}")
            return []

    def _create_blank_image(self, resolution="1024*1024"):
        """创建空白图片，支持不同分辨率"""
        try:
            # 解析分辨率
            width, height = map(int, resolution.split('*'))
            
            from PIL import Image
            image = Image.new('RGB', (width, height), 'white')
            temp_path = os.path.join(os.path.dirname(__file__), "temp", f"blank_{int(time.time())}.png")
            
            # 确保temp目录存在
            temp_dir = os.path.dirname(temp_path)
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
                
            image.save(temp_path)
            logger.info(f"[TYHH] 创建空白图片成功: {width}x{height}")
            return temp_path
        except Exception as e:
            logger.error(f"[TYHH] 创建空白图片失败: {e}")
            return None

    def _upload_image_to_oss(self, image_path, task_type):
        """上传图片到OSS"""
        try:
            # 获取上传策略
            policy_url = 'https://wanxiang.aliyun.com/wanx/api/oss/getPolicy'
            policy_data = {
                "fileName": os.path.basename(image_path),
                "taskType": task_type
            }
            
            headers = self._get_headers()
            policy_res = requests.post(policy_url, headers=headers, json=policy_data)
            
            if not policy_res.json().get('success'):
                logger.error(f"[TYHH] 获取上传策略失败: {policy_res.text}")
                return None
                
            policy_info = policy_res.json()['data']
            
            # 构造上传参数
            upload_url = policy_info['host']
            files = {
                'key': (None, policy_info['key']),
                'policy': (None, policy_info['policy']),
                'OSSAccessKeyId': (None, policy_info['accessId']),
                'signature': (None, policy_info['signature']),
                'file': (unquote(os.path.basename(image_path)), open(image_path, 'rb'), 'image/png')
            }
            
            # 发送上传请求
            upload_res = requests.post(upload_url, files=files)
            if upload_res.status_code not in [200, 204]:
                logger.error(f"[TYHH] OSS上传失败: HTTP {upload_res.status_code}")
                return None
                
            # 生成访问链接
            generate_url = 'https://wanxiang.aliyun.com/wanx/api/oss/generateOssUrl'
            generate_data = {
                "key": policy_info['key'],
                "taskType": task_type
            }
            generate_res = requests.post(generate_url, headers=headers, json=generate_data)
            
            if not generate_res.json().get('success'):
                logger.error(f"[TYHH] 生成访问链接失败: {generate_res.text}")
                return None
                
            return generate_res.json()['data']
            
        except Exception as e:
            logger.error(f"[TYHH] 上传图片到OSS失败: {e}")
            return None
            
    def _get_headers(self):
        """获取请求头"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://tongyi.aliyun.com',
            'Referer': 'https://tongyi.aliyun.com/wanxiang/creation',
            'x-platform': 'web',
            'Cookie': self.config.get('cookie', '')
        }
        
        if self.xsrf_token:
            headers['x-xsrf-token'] = self.xsrf_token
            
        return headers

    def _send_local_image(self, image_path, e_context):
        """发送本地图片"""
        try:
            with open(image_path, 'rb') as f:
                image_reply = Reply(ReplyType.IMAGE, f)
                e_context["channel"].send(image_reply, e_context["context"])
        except Exception as e:
            logger.error(f"[TYHH] 发送本地图片失败: {e}")
            
    def _combine_and_send_images(self, download_urls, e_context, total_credits=0, img_id=None):
        """合并并发送图片"""
        temp_files = []
        merged_image_path = None
        try:
            if len(download_urls) < 4:
                logger.warning("[TYHH] 图片数量不足4张，无法合并")
                return False
                
            logger.info("[TYHH] 尝试合并前4张图片")
            
            # 创建临时目录
            temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
                
            # 下载图片到本地临时文件
            for i, url in enumerate(download_urls[:4]):
                temp_file = os.path.join(temp_dir, f'temp_{i}_{time.time()}.png')
                response = requests.get(url, stream=True)
                if response.status_code == 200:
                    with open(temp_file, 'wb') as f:
                        for chunk in response.iter_content(1024):
                            f.write(chunk)
                    temp_files.append(temp_file)
                else:
                    logger.error(f"[TYHH] 下载图片失败: {url}")
                    return False
                    
            # 合并图片
            merged_image_path = os.path.join(temp_dir, f'merged_{time.time()}.png')
            success = self.image_processor.combine_images(temp_files, merged_image_path)
            
            if success:
                # 发送合并后的图片
                self._send_local_image(merged_image_path, e_context)
                
                # 发送提示信息
                help_text = f"[AI] 图片生成成功！账号积分：{total_credits}\n"
                if img_id:
                    help_text += f"图片ID: {img_id}\n使用't {img_id} 序号'可以查看原图"
                e_context["reply"] = Reply(ReplyType.TEXT, help_text)
                
                return True
            else:
                logger.error("[TYHH] 图片合并失败")
                return False
                
        except Exception as e:
            logger.error(f"[TYHH] 合并图片时出错: {str(e)}")
            return False
            
        finally:
            # 清理临时文件
            try:
                for temp_file in temp_files:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                        logger.info(f"[TYHH] 已删除临时文件: {temp_file}")
                if merged_image_path and os.path.exists(merged_image_path):
                    os.remove(merged_image_path)
                    logger.info(f"[TYHH] 已删除合并后的图片: {merged_image_path}")
            except Exception as e:
                logger.error(f"[TYHH] 清理临时文件失败: {str(e)}")

    def _parse_sketch_command(self, content):
        """解析手绘命令参数"""
        # 移除命令前缀
        content = content[4:].strip()
        
        # 默认参数
        resolution = "1024*1024"  # 默认1:1
        style = "<flat illustration>"  # 默认扁平插画
        
        # 风格映射
        style_mapping = {
            "-扁平": "<flat illustration>",
            "-油画": "<oil painting>",
            "-二次元": "<anime>",
            "-水彩": "<watercolor>",
            "-3D": "<3d cartoon>",
            "-彩绘": "<watercolor>"  # 添加彩绘风格映射
        }
        
        # 比例映射 - 只保留支持的比例
        ratio_mapping = {
            "-1:1": "1024*1024",
            "-16:9": "1280*720",
            "-9:16": "720*1280"
        }

        # 先找出所有参数的位置
        param_positions = []
        for param in list(ratio_mapping.keys()) + list(style_mapping.keys()):
            pos = content.find(param)
            if pos != -1:
                param_positions.append((pos, param))
        
        # 按位置排序
        param_positions.sort()
        
        # 如果没有找到任何参数，整个内容就是提示词
        if not param_positions:
            prompt = content
        else:
            # 提取提示词（第一个参数之前的所有内容）
            prompt = content[:param_positions[0][0]].strip()
            
            # 处理参数
            for pos, param in param_positions:
                if param in ratio_mapping:
                    resolution = ratio_mapping[param]
                elif param in style_mapping:
                    style = style_mapping[param]
        
        logger.info(f"[TYHH] 解析命令结果: prompt='{prompt}', resolution='{resolution}', style='{style}'")
        return prompt, resolution, style

    def _preprocess_sketch_image(self, image_path):
        """
        预处理涂鸦图片，将白底彩色线条转换为黑底白线
        """
        try:
            from PIL import Image
            import numpy as np
            
            # 打开图片
            img = Image.open(image_path)
            
            # 转换为RGBA格式（如果不是的话）
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
                
            # 转换为numpy数组以便处理
            data = np.array(img)
            
            # 创建一个新的全黑图像
            new_data = np.zeros_like(data)
            
            # 获取alpha通道
            alpha = data[:, :, 3]
            
            # 计算RGB通道的平均值（用于检测非白色区域）
            rgb_mean = data[:, :, :3].mean(axis=2)
            
            # 创建掩码：
            # 1. alpha > 0 表示非透明区域
            # 2. rgb_mean < 240 表示非白色区域（允许一些容差）
            mask = (alpha > 0) & (rgb_mean < 240)
            
            # 将掩码区域设置为白色，其他区域保持黑色
            new_data[mask] = [255, 255, 255, 255]  # 白色，完全不透明
            new_data[~mask] = [0, 0, 0, 255]  # 黑色，完全不透明
            
            # 创建新图像
            processed_img = Image.fromarray(new_data)
            
            # 保存处理后的图片
            temp_dir = os.path.dirname(image_path)
            processed_path = os.path.join(temp_dir, f"processed_{os.path.basename(image_path)}")
            processed_img.save(processed_path, 'PNG')
            
            return processed_path
        except Exception as e:
            logger.error(f"[TYHH] 处理涂鸦图片失败: {e}")
            return None
