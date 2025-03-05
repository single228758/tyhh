from plugins.tyhh.tyhh import TongyiDrawingPlugin

def login_cli():
    """命令行登录功能"""
    try:
        plugin = TongyiDrawingPlugin()
        print("通义绘画插件 - 命令行登录工具")
        print("============================")
        
        # 手机号输入
        phone = input("请输入11位手机号码: ").strip()
        while len(phone) != 11 or not phone.isdigit():
            phone = input("无效的手机号，请重新输入: ").strip()
            
        # 发送验证码
        print("正在发送验证码...")
        sms_token = plugin._send_sms_code(phone)
        if not sms_token:
            print("发送验证码失败，请检查网络连接或手机号码是否正确")
            return False
            
        # 输入验证码
        sms_code = input("请输入收到的6位验证码: ").strip()
        while len(sms_code) != 6 or not sms_code.isdigit():
            sms_code = input("验证码格式错误，请重新输入6位数字验证码: ").strip()
            
        # 执行登录
        print("正在登录...")
        cookie = plugin._login_with_sms(phone, sms_code, sms_token)
        if cookie:
            # 更新配置
            plugin.config["cookie"] = cookie
            plugin._save_config()
            print("登录成功！配置已更新")
            return True
        else:
            print("登录失败，请重试")
            return False
    except Exception as e:
        print(f"登录过程出错: {e}")
        return False
        
if __name__ == "__main__":
    login_cli()