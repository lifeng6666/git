import sys
import time
import json
import random
import string
import requests
import subprocess
import re
import os
import tempfile
import threading
import queue
from datetime import datetime
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from Utils import pwdEncrypt
    print("✅ 成功加载 SM2 加密依赖")
except ImportError:
    print("❌ 错误: 未找到 Utils.py ，请确保同目录下存在该文件")
    sys.exit(1)

def log(msg, show_time=True):
    """带时间戳的日志输出"""
    if show_time:
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    else:
        full_msg = msg
    print(full_msg, flush=True)

def create_chrome_driver():
    """
    创建Chrome浏览器实例
    """
    chrome_options = Options()
    
    # --- 防检测核心配置 ---
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # --- 常规配置 ---
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    
    driver = webdriver.Chrome(options=chrome_options)
    
    # --- CDP 命令防检测 ---
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """
    })
    
    return driver

def call_aliv3min_with_timeout(timeout_seconds=180, max_retries=18):
    """调用 AliV3min.py 获取 captchaTicket - 最多重试18次"""
    for attempt in range(max_retries):
        log(f"📞 正在调用 登录脚本 获取 captchaTicket (尝试 {attempt + 1}/{max_retries})...")
        
        process = None
        output_lines = []  # 存储所有输出
        
        try:
            if not os.path.exists('AliV3min.py'):
                log("❌ 错误: 找不到登录依赖 AliV3min.py")
                log("❌ 登录脚本存在异常")
                sys.exit(1)

            process = subprocess.Popen(
                [sys.executable, 'AliV3min.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            
            q = queue.Queue()
            def enqueue_output(out, queue_obj):
                try:
                    for line in iter(out.readline, ''):
                        queue_obj.put(line)
                except Exception:
                    pass
                finally:
                    try:
                        out.close()
                    except Exception:
                        pass

            t = threading.Thread(target=enqueue_output, args=(process.stdout, q))
            t.daemon = True
            t.start()

            start_time = time.time()
            captcha_ticket = None
            wait_for_next_line = False
            
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout_seconds:
                    # 情况1：超时强制停止，需要打印日志
                    log(f"⏰ 登录脚本超过 {timeout_seconds} 秒未完成，强制终止...")
                    log("=" * 60)
                    log("📋 AliV3min.py 完整日志输出:")
                    log("=" * 60)
                    for line in output_lines:
                        print(line.rstrip())
                    log("=" * 60)
                    
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except:
                        pass
                    break
                
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None and not t.is_alive():
                        # 进程已结束，所有输出已被队列读取完毕
                        break
                    continue
                except Exception as e:
                    log(f"⚠ 读取输出时出错: {e}")
                    time.sleep(0.1)
                    continue

                if line:
                    output_lines.append(line)  # 保存所有输出
                    
                    if wait_for_next_line:
                        captcha_ticket = line.strip()
                        log(f"✅ 成功获取 captchaTicket")
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except:
                            pass
                        return captcha_ticket

                    if "SUCCESS: Obtained CaptchaTicket:" in line:
                        wait_for_next_line = True
                        continue

                    if "captchaTicket" in line:
                        try:
                            match = re.search(r'"captchaTicket"\s*:\s*"([^"]+)"', line)
                            if match:
                                captcha_ticket = match.group(1)
                                log(f"✅ 成功获取 captchaTicket")
                                try:
                                    process.terminate()
                                    process.wait(timeout=5)
                                except:
                                    pass
                                return captcha_ticket
                        except:
                            pass
            
            # 如果没有获取到 captchaTicket
            if not captcha_ticket:
                # 情况2：如果是最后一次重试失败，打印日志
                if attempt == max_retries - 1:
                    log(f"❌ 最终尝试未获取到 captchaTicket")
                    log("=" * 60)
                    log("📋 AliV3min.py 最后一次尝试的完整日志输出:")
                    log("=" * 60)
                    for line in output_lines:
                        print(line.rstrip())
                    log("=" * 60)
                
                # 确保进程已终止
                if process and process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except:
                        pass
                
                if attempt < max_retries - 1:
                    log(f"⚠ 未获取到CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                    time.sleep(5)
            else:
                return captcha_ticket
                
        except Exception as e:
            log(f"❌ 调用登录脚本异常: {e}")
            
            # 情况2：如果是最后一次重试且发生异常，打印日志
            if attempt == max_retries - 1:
                log("=" * 60)
                log("📋 AliV3min.py 最后一次尝试的完整日志输出:")
                log("=" * 60)
                for line in output_lines:
                    print(line.rstrip())
                log("=" * 60)
            
            # 确保进程已终止
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except:
                    pass
            
            if attempt < max_retries - 1:
                log(f"⚠ 未获取到CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                time.sleep(5)
    
    # 18次都失败，程序退出
    log("❌ 登录脚本存在异常")
    sys.exit(1)

def send_request_via_browser(driver, url, method='POST', body=None):
    """通过浏览器控制台发送请求"""
    try:
        if body:
            body_str = json.dumps(body, ensure_ascii=False)
            js_code = """
            var url = arguments[0];
            var bodyData = arguments[1];
            var callback = arguments[2];
            fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'AppId': 'JLC_PORTAL_PC',
                    'ClientType': 'PC-WEB'
                },
                body: bodyData,
                credentials: 'include'
            }).then(response => {
                if (!response.ok) { return JSON.stringify({error: "HTTP Error " + response.status}); }
                return response.json().then(data => JSON.stringify(data));
            }).then(data => callback(data)).catch(error => callback(JSON.stringify({error: error.toString()})));
            """
            result = driver.execute_async_script(js_code, url, body_str)
        else:
            js_code = """
            var url = arguments[0];
            var callback = arguments[1];
            fetch(url, {
                method: 'GET',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*', credentials: 'include'}
            }).then(response => response.json().then(data => JSON.stringify(data))).then(data => callback(data)).catch(error => callback(JSON.stringify({error: error.toString()})));
            """
            result = driver.execute_async_script(js_code, url)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    except Exception as e:
        log(f"❌ 浏览器请求执行失败: {e}")
        return None

def perform_init_session(driver, max_retries=3):
    """执行 Session 初始化"""
    for i in range(max_retries):
        log(f"📡 初始化会话 (尝试 {i + 1}/{max_retries})...")
        response = send_request_via_browser(driver, "https://passport.jlc.com/api/cas/login/get-init-session", 'POST', {"appId": "JLC_PORTAL_PC", "clientType": "PC-WEB"})
        if response and response.get('success') == True and response.get('code') == 200:
            log("✅ 初始化会话成功")
            return True
        else:
            if i < max_retries - 1:
                log(f"⚠ 初始化会话失败，等待2秒后重试...")
                time.sleep(2)
    return False

def login_with_password(driver, username, password, captcha_ticket):
    """登录 API 调用"""
    url = "https://passport.jlc.com/api/cas/login/with-password"
    try:
        encrypted_username = pwdEncrypt(username)
        encrypted_password = pwdEncrypt(password)
    except Exception as e:
        log(f"❌ SM2加密失败: {e}")
        return 'other_error', None
    
    body = {'username': encrypted_username, 'password': encrypted_password, 'isAutoLogin': False, 'captchaTicket': captcha_ticket}
    log(f"📡 发送登录请求...")
    response = send_request_via_browser(driver, url, 'POST', body)
    if not response: return 'other_error', None
    
    if response.get('success') == True and response.get('code') == 2017: return 'success', response
    if response.get('code') == 10208: return 'password_error', response
    return 'other_error', response

def verify_login_on_member_page(driver, max_retries=3):
    """验证登录"""
    for attempt in range(max_retries):
        log(f"🔍 验证登录状态 ({attempt + 1}/{max_retries})...")
        try:
            driver.get("https://member.jlc.com/")
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(5)
            page_source = driver.page_source
            if "客编" in page_source or "customerCode" in page_source:
                log(f"✅ 验证登录成功")
                return True
        except Exception as e:
            log(f"⚠ 验证登录失败: {e}")
        if attempt < max_retries - 1:
            log(f"⏳ 等待2秒后重试...")
            time.sleep(2)
    return False

def perform_login_flow(driver, username, password, max_retries=3):
    
    session_fail_count = 0
    
    for login_attempt in range(max_retries):
        log(f"🔐 开始登录流程 (尝试 {login_attempt + 1}/{max_retries})...")
        
        try:
            # 步骤 1: 打开登录页
            driver.get("https://passport.jlc.com")
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # 步骤 2: 初始化 Session
            if not perform_init_session(driver):
                session_fail_count += 1
                if session_fail_count >= 3:
                    log("❌ 浏览器环境存在异常")
                    sys.exit(1)
                raise Exception("初始化 Session 失败")
            
            # 重置失败计数（成功了就清零）
            session_fail_count = 0
            
            # 步骤 3: 获取 CaptchaTicket（全局重试5次，失败直接退出程序）
            captcha_ticket = call_aliv3min_with_timeout()
            if not captcha_ticket:
                # 这里不会执行到，因为 call_aliv3min_with_timeout 失败会直接 sys.exit(1)
                raise Exception("获取 CaptchaTicket 失败")
            
            # 步骤 4: 登录
            status, login_res = login_with_password(driver, username, password, captcha_ticket)
            if status == 'password_error':
                return 'password_error', driver
            if status != 'success':
                raise Exception("登录失败")
            
            # 步骤 5: 验证登录
            if not verify_login_on_member_page(driver):
                raise Exception("登录验证失败")
            
            log("✅ 登录流程完成")
            return 'success', driver
            
        except Exception as e:
            log(f"❌ 登录流程异常: {e}")
            if login_attempt < max_retries - 1:
                log(f"⏳ 关闭浏览器，等待5秒后重新创建浏览器实例...")
                try:
                    driver.quit()
                except:
                    pass
                time.sleep(5)
                # 重新创建浏览器
                driver = create_chrome_driver()
            else:
                log(f"❌ 登录流程已达最大重试次数")
                return 'login_failed', driver
    
    return 'login_failed', driver


def generate_random_username(length=12):
    """生成指定长度的随机用户名（字母+数字）"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def get_random_inviter():
    """随机返回一个邀请者客编"""
    inviters = ["4330993A", "10697874A", "12112588A", "12112597A", "12112618A", "12112627A", "12112669A", "12118785A", "12119058A", "12119161A"]
    return random.choice(inviters)

def handle_jlc_login_center(driver, account_index):
    """处理'嘉立创集团用户登录中心'的中转点击"""
    try:
        title = driver.title
        if "嘉立创集团用户登录中心" in title:
            log(f"账号 {account_index} - 检测到 '嘉立创集团用户登录中心', 尝试点击 '进入系统'...")
            try:
                enter_btn = driver.find_element(By.XPATH, "//button//span[contains(., '进入系统')]")
                driver.execute_script("arguments[0].click();", enter_btn)
                log(f"账号 {account_index} - 已点击，等待页面跳转...")
                time.sleep(5) 
                return True
            except NoSuchElementException:
                log(f"账号 {account_index} - 页面是登录中心，但找不到'进入系统'按钮")
                return False
    except Exception as e:
        log(f"账号 {account_index} - ⚠ 处理登录中心跳转时发生异常(非致命): {e}")
    return False

def get_user_nickname_from_api(driver, account_index):
    """
    通过浏览器控制台调用接口获取用户昵称
    
    """
    try:
        target_url = "https://u.lceda.cn/account/user/account/setting/basic"
        driver.get(target_url)
        
        log(f"账号 {account_index} - 正在打开lceda账户页面，等待5秒页面加载...")
        time.sleep(5)
        
        # 输出当前标题
        log(f"账号 {account_index} - 当前页面标题: {driver.title}")
        
        # 处理可能的中间页跳转 (登录中心)
        handle_jlc_login_center(driver, account_index)
        
        # 发送请求
        log(f"账号 {account_index} - 发送API请求获取用户信息...")
        response = send_request_via_browser(driver, "https://u.lceda.cn/api/user", 'GET')
        
        if response:
            # 优先处理 401 情况
            if response.get('code') == 401:
                log(f"账号 {account_index} - ⚠ API返回 401 未登录: {response.get('message')}")
                return None, 401

            if response.get('success') == True:
                result_data = response.get('result', {})
                nickname = result_data.get('nickname', '')
                if nickname:
                    log(f"账号 {account_index} - ✅ 成功获取昵称: {nickname}")
                    return nickname, 0
        
        log(f"账号 {account_index} - ⚠ 无法获取用户昵称, 返回内容摘要: {str(response)[:100]}...")
        return None, -1
    except Exception as e:
        log(f"账号 {account_index} - ⚠ 获取用户昵称失败: {e}")
        return None, -1

def modify_user_info(driver, account_index):
    """修改用户资料流程（不输入随机用户名）"""
    log(f"账号 {account_index} - 开始修改资料流程...")
    wait = WebDriverWait(driver, 15)
    
    try:
        # 1. 先选择个人用户
        try:
            personal_radio = wait.until(
                EC.element_to_be_clickable((By.ID, "personal"))
            )
            personal_radio.click()
            log(f"账号 {account_index} - ✅ 已选择个人用户")
        except Exception as e:
            log(f"账号 {account_index} - ❌ 找不到个人用户选项: {e}")
            return False
        
        time.sleep(2)  # 等待选项生效
        
        # 2. 输入邀请者客编
        inviter = get_random_inviter()
        try:
            inviter_input = wait.until(
                EC.presence_of_element_located((By.ID, "txtInviter"))
            )
            inviter_input.clear()
            inviter_input.send_keys(inviter)
            log(f"账号 {account_index} - ✅ 已输入邀请者客编: {inviter}")
        except Exception as e:
            log(f"账号 {account_index} - ❌ 找不到邀请者输入框: {e}")
            return False
        
        time.sleep(3)
        
        # 3. 点击下一步
        try:
            next_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "下一步")]'))
            )
            next_btn.click()
            log(f"账号 {account_index} - ✅ 已点击下一步")
        except Exception as e:
            log(f"账号 {account_index} - ❌ 找不到下一步按钮: {e}")
            return False
        
        time.sleep(8)
        
        # 4. 点击“完成扫码，下一步”
        try:
            finish_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//a[contains(text(), "完成扫码，下一步")]'))
            )
            finish_btn.click()
            log(f"账号 {account_index} - ✅ 已点击完成扫码下一步")
        except Exception as e:
            log(f"账号 {account_index} - ⚠ 找不到完成扫码按钮，可能已经自动跳过: {e}")
        
        time.sleep(5)
        
        log(f"账号 {account_index} - ✅ 修改资料流程完成")
        return True
        
    except Exception as e:
        log(f"账号 {account_index} - ❌ 修改资料流程出错: {e}")
        return False

def force_relogin_and_update(driver, account_index):
    """
    执行重新登录并跳转到修改资料页的流程 (处理 401 问题)
    """
    log(f"账号 {account_index} - 🔄 检测到账号异常(401)，开始执行完善资料...")
    
    force_url = "https://passport.jlc.com/login?appId=JLC_EDA_U&redirectUrl=https%3A%2F%2Fu.lceda.cn%2Flogin%3Ffrom%3Dhttps%253A%252F%252Fu.lceda.cn%252Faccount%252Fuser%252Fprojects%252Fall&backCode=1"
    
    try:
        driver.get(force_url)
        log(f"账号 {account_index} - 已打开确认登录页面，等待跳转或操作 (限时3分钟)...")
        
        start_time = time.time()
        max_wait = 180 # 3分钟
        
        clicked_enter = False
        redirected_to_update = False
        
        while time.time() - start_time < max_wait:
            current_url = driver.current_url
            current_title = driver.title
            
            # 1. 检查是否重定向到了 update_user_info
            if "cas/update_user_info" in current_url:
                log(f"账号 {account_index} - ✅ 成功重定向到修改资料页面")
                redirected_to_update = True
                break
                
            # 2. 检查是否有 "进入系统" 按钮 (如果是嘉立创登录中心)
            if "嘉立创集团用户登录中心" in current_title and not clicked_enter:
                try:
                    enter_btn = driver.find_element(By.XPATH, "//button//span[contains(., '进入系统')]")
                    driver.execute_script("arguments[0].click();", enter_btn)
                    log(f"账号 {account_index} - 点击了 '进入系统' 按钮")
                    clicked_enter = True
                    time.sleep(3) # 点击后稍等
                    continue
                except:
                    pass
            
            # 每秒检查一次
            time.sleep(1)
            
        if redirected_to_update:
            log(f"账号 {account_index} - 跳转成功，立即开始完善资料...")
            # 立即调用修改资料
            if modify_user_info(driver, account_index):
                log(f"账号 {account_index} - ✅ 资料完善成功")
                return True
            else:
                log(f"账号 {account_index} - ❌ 资料完善失败")
                return False
        else:
            log(f"账号 {account_index} - ❌ 等待重定向流程超时 (3分钟未进入修改资料页)")
            return False
            
    except Exception as e:
        log(f"账号 {account_index} - ❌ 完善资料流程异常: {e}")
        return False


def process_single_account(username, password, account_index, total_accounts, retry_count=0, is_final_retry=False):
    """处理单个账号"""
    retry_label = ""
    if retry_count > 0:
        retry_label = f" (重试{retry_count})"
    if is_final_retry:
        retry_label = " (最终重试)"
    
    log(f"开始处理账号 {account_index}/{total_accounts}{retry_label}")
    
    # 初始化浏览器 
    driver = create_chrome_driver()
    
    result = {
        'account_index': account_index,
        'username': username,
        'password': password,
        'is_valid': False,
        'modified': False,
        'nickname': None,
        'error': None,
        'password_error': False,
        'retry_count': retry_count,
        'is_final_retry': is_final_retry
    }
    
    try:

        login_status, driver = perform_login_flow(driver, username, password, max_retries=3)
        
        if login_status == 'password_error':
            log(f"账号 {account_index} - ❌ 密码错误")
            result['password_error'] = True
            result['error'] = "密码错误"
            return result
            
        if login_status != 'success':
            log(f"账号 {account_index} - ❌ 登录流程失败")
            result['error'] = "登录失败"
            return result
        
        log(f"账号 {account_index} - 登录成功，验证账号有效性...")
        
        # 1. 直接尝试获取昵称
        nickname, status_code = get_user_nickname_from_api(driver, account_index)
        
        # 2. 如果是 401，立即执行完善资料流程
        if status_code == 401:
            log(f"账号 {account_index} - ⚠ 触发 401 资料未完善逻辑...")
            if force_relogin_and_update(driver, account_index):
                # 完善成功，标记已修改，并再次尝试获取昵称
                result['modified'] = True
                log(f"账号 {account_index} - 资料完善完成，再次尝试获取昵称...")
                time.sleep(5)
                nickname, status_code = get_user_nickname_from_api(driver, account_index)
            else:
                log(f"账号 {account_index} - ❌ 完善资料逻辑执行失败")
        
        # 3. 检查最终结果
        if nickname:
            result['is_valid'] = True
            result['nickname'] = nickname
            log(f"账号 {account_index} - ✅ 账号验证成功")
        else:
            # 如果不是401但失败了，尝试常规刷新重试
            if status_code != 401:
                log(f"账号 {account_index} - 第一次获取昵称失败(非401)，刷新页面重试...")
                driver.refresh()
                time.sleep(5)
                handle_jlc_login_center(driver, account_index)
                
                nickname, status_code = get_user_nickname_from_api(driver, account_index)
                if nickname:
                    result['is_valid'] = True
                    result['nickname'] = nickname
                    log(f"账号 {account_index} - ✅ 账号验证成功（刷新后）")
                else:
                    result['error'] = "无法获取昵称"
                    log(f"账号 {account_index} - ❌ 账号验证失败")
            else:
                 # 依然是 401，说明完善资料失败或再次失败
                 result['error'] = "无法获取昵称(401)"
                 log(f"账号 {account_index} - ❌ 账号验证失败 (401)")

        
    except Exception as e:
        log(f"账号 {account_index} - ❌ 处理过程出错: {e}")
        result['error'] = str(e)
    finally:
        if driver:
            try:
                driver.quit()
                log(f"账号 {account_index} - 浏览器已关闭")
            except:
                pass
    
    return result

def should_retry(result):
    """判断是否需要重试：只要有任何错误（is_valid为False）"""

    return not result['is_valid']

def process_account_with_retry(username, password, account_index, total_accounts):
    """处理单个账号，包含重试机制，并合并多次尝试的最佳结果"""
    max_retries = 2
    merged_result = {
        'account_index': account_index,
        'username': username,
        'password': password,
        'is_valid': False,
        'modified': False,
        'nickname': None,
        'error': None,
        'password_error': False,
        'retry_count': 0,
        'is_final_retry': False
    }
    
    for attempt in range(max_retries + 1):  # 第一次执行 + 重试次数
        result = process_single_account(username, password, account_index, total_accounts, retry_count=attempt)
        
        # 合并结果：如果本次成功且之前未成功，则更新
        if result['is_valid'] and not merged_result['is_valid']:
            merged_result['is_valid'] = True
            merged_result['modified'] = result['modified']
            merged_result['nickname'] = result['nickname']
            merged_result['error'] = None
        
        # 如果是账密错误，更新但继续重试
        if result.get('password_error'):
            merged_result['password_error'] = True
            merged_result['error'] = "账密错误"
        
        # 更新retry_count为最后一次尝试的
        merged_result['retry_count'] = result['retry_count']
        
        # 检查是否还需要重试
        # 如果是密码错误，其实没必要重试了，但为了保持逻辑结构，或者防止网络误报，可以选择继续
        if not should_retry(merged_result) or attempt >= max_retries:
            break
        else:
            log(f"账号 {account_index} - 🔄 准备第 {attempt + 1} 次重试，等待 {random.randint(2, 6)} 秒后重新开始...")
            time.sleep(random.randint(2, 6))
    
    return merged_result

def execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts):
    """对失败的账号执行最终重试"""
    log("=" * 70)
    log("🔄 执行最终重试 - 处理所有重试后仍失败的账号")
    log("=" * 70)
    
    # 找出需要最终重试的账号
    failed_accounts = []
    for i, result in enumerate(all_results):
        if should_retry(result):  # 只要is_valid为False就重试，包括账密错误
            failed_accounts.append({
                'index': i,
                'account_index': result['account_index'],
                'username': usernames[result['account_index'] - 1],
                'password': passwords[result['account_index'] - 1],
                'previous_retry_count': result['retry_count']
            })
    
    if not failed_accounts:
        log("✅ 没有需要最终重试的账号，所有账号都已成功")
        return all_results
    
    log(f"📋 需要最终重试的账号: {', '.join(str(acc['account_index']) for acc in failed_accounts)}")
    
    # 等待一段时间再开始最终重试
    wait_time = random.randint(2, 3)
    log(f"⏳ 等待 {wait_time} 秒后开始最终重试...")
    time.sleep(wait_time)
    
    # 执行最终重试
    for failed_acc in failed_accounts:
        log(f"🔄 开始最终重试账号 {failed_acc['account_index']}")
        
        final_result = process_single_account(
            failed_acc['username'], 
            failed_acc['password'], 
            failed_acc['account_index'], 
            total_accounts, 
            retry_count=failed_acc['previous_retry_count'] + 1,
            is_final_retry=True
        )
        
        original_result = all_results[failed_acc['index']]
        
        # 如果最终重试成功，更新原结果
        if final_result['is_valid'] and not original_result['is_valid']:
            original_result['is_valid'] = True
            original_result['modified'] = final_result['modified']
            original_result['nickname'] = final_result['nickname']
            original_result['error'] = None
            log(f"✅ 账号 {failed_acc['account_index']} - 验证成功")
        
        # 如果是账密错误，更新
        if final_result.get('password_error'):
            original_result['password_error'] = True
            original_result['error'] = "账密错误"
        
        original_result['is_final_retry'] = True
        original_result['retry_count'] = failed_acc['previous_retry_count'] + 1
        
        # 如果不是最后一个账号，等待一段时间
        if failed_acc != failed_accounts[-1]:
            wait_time = random.randint(3, 5)
            log(f"⏳ 等待 {wait_time} 秒后处理下一个重试账号...")
            time.sleep(wait_time)
    
    log("✅ 最终重试完成")
    return all_results

def save_valid_accounts(valid_accounts, filename="valid_accounts.txt"):
    """保存有效账号到文件"""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("有效账号列表（账号:密码）\n")
        f.write("=" * 50 + "\n")
        for account in valid_accounts:
            f.write(f"{account['username']}:{account['password']}\n")
            if account['nickname']:
                f.write(f"# 昵称: {account['nickname']}\n")
            f.write("\n")

def main():
    if len(sys.argv) < 3:
        print("用法: python lceda_account.py 账号1,账号2,账号3... 密码1,密码2,密码3...")
        print("示例: python lceda_account.py user1,user2,user3 pwd1,pwd2,pwd3")
        sys.exit(1)
    
    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    
    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)
    
    total_accounts = len(usernames)
    log(f"开始处理 {total_accounts} 个账号")
    
    # 存储所有账号的结果
    all_results = []
    
    for i, (username, password) in enumerate(zip(usernames, passwords), 1):
        result = process_account_with_retry(username, password, i, total_accounts)
        all_results.append(result)
        
        if i < total_accounts:
            wait_time = random.randint(1, 2)
            log(f"等待 {wait_time} 秒后处理下一个账号...")
            time.sleep(wait_time)
    
    # 检查是否有失败的账号，执行最终重试
    has_failed_accounts = any(should_retry(result) for result in all_results)
    
    if has_failed_accounts:
        all_results = execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts)
    
    # 输出总结
    valid_accounts = []
    modified_count = 0
    failed_count = 0
    password_error_count = 0
    
    for result in all_results:
        if result['is_valid']:
            valid_accounts.append({
                'username': result['username'],
                'password': result['password'],
                'nickname': result['nickname']
            })
            if result['modified']:
                modified_count += 1
        else:
            if result.get('password_error'):
                password_error_count += 1
            else:
                failed_count += 1
    
    log("=" * 60)
    log("📊 任务完成总结")
    log("=" * 60)
    log(f"总账号数: {total_accounts}")
    log(f"有效账号: {len(valid_accounts)}")
    log(f"修改资料: {modified_count}")
    log(f"账密错误: {password_error_count}")
    log(f"其他失败: {failed_count}")
    
    # 保存有效账号到文件
    if valid_accounts:
        save_valid_accounts(valid_accounts)
        log(f"✅ 有效账号信息已保存到 valid_accounts.txt")
    else:
        log("⚠ 没有有效账号，未生成账号文件")
    
    log("程序执行完成")
    
    # 根据有效账号数与总账号数的比较返回退出码
    if len(valid_accounts) < total_accounts:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
