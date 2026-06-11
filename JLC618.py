import os
import sys
import time
import json
import tempfile
import requests
import subprocess
import threading
import queue
import re
import shutil
from datetime import datetime, timedelta
import random

# 设置标准输出为 UTF-8 编码，解决 Windows 命令行中文和表情符号乱码问题
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
else:
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from Utils import pwdEncrypt

# ======================== 全局变量与日志收集 ========================
in_summary = False
summary_logs = []

def log(msg, show_time=True):
    if show_time:
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    else:
        full_msg = msg
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)

# ======================== 浏览器与登录验证核心逻辑 ========================

def create_chrome_driver(user_data_dir=None):
    """创建 Chrome 浏览器实例（启用性能日志以抓取 header）"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")


    # chromedriver_path = 'chromedriver.exe'
    # service = Service(executable_path=chromedriver_path)

    # driver = webdriver.Chrome(service=service, options=chrome_options)
    driver = webdriver.Chrome(options=chrome_options)

    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
    )
    return driver

def call_aliv3min_with_timeout(timeout_seconds=180, max_retries=18):
    for attempt in range(max_retries):
        log(f"📞 正在调用登录脚本获取 captchaTicket (尝试 {attempt + 1}/{max_retries})...")
        process = None
        try:
            if not os.path.exists("AliV3min.py"):
                log("❌ 错误: 找不到登录依赖 AliV3min.py")
                sys.exit(1)
            process = subprocess.Popen(
                [sys.executable, "AliV3min.py"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
            )
            q = queue.Queue()
            def enqueue_output(out, queue_obj):
                try:
                    for line in iter(out.readline, ''): queue_obj.put(line)
                except: pass
                finally:
                    try: out.close()
                    except: pass
            t = threading.Thread(target=enqueue_output, args=(process.stdout, q))
            t.daemon = True
            t.start()
            start_time = time.time()
            wait_for_next_line = False

            while True:
                if time.time() - start_time > timeout_seconds:
                    log(f"⏰ 登录脚本超过 {timeout_seconds} 秒未完成，强制终止...")
                    try: process.kill(); process.wait(timeout=5)
                    except: pass
                    break
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None and not t.is_alive(): break
                    continue
                if line:
                    if wait_for_next_line:
                        captcha_ticket = line.strip()
                        log("✅ 成功获取 captchaTicket")
                        try: process.terminate(); process.wait(timeout=5)
                        except: pass
                        return captcha_ticket
                    if "SUCCESS: Obtained CaptchaTicket:" in line:
                        wait_for_next_line = True
                        continue
                    if "captchaTicket" in line:
                        match = re.search(r'"captchaTicket"\s*:\s*"([^"]+)"', line)
                        if match:
                            log("✅ 成功获取 captchaTicket")
                            try: process.terminate(); process.wait(timeout=5)
                            except: pass
                            return match.group(1)

            if process and process.poll() is None:
                try: process.kill(); process.wait(timeout=5)
                except: pass
            if attempt < max_retries - 1:
                log(f"⚠ 未获取到 CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                time.sleep(5)
        except Exception as e:
            log(f"❌ 调用登录脚本异常: {e}")
            if process and process.poll() is None:
                try: process.kill(); process.wait(timeout=5)
                except: pass
            if attempt < max_retries - 1:
                time.sleep(5)
    return None

def send_login_request(driver, url, method="POST", body=None):
    try:
        if body:
            body_str = json.dumps(body, ensure_ascii=False)
            js_code = """
            var url=arguments[0],bodyData=arguments[1],cb=arguments[2];
            fetch(url,{method:'POST',headers:{'Content-Type':'application/json',
            'Accept':'application/json, text/plain, */*','AppId':'JLC_PORTAL_PC',
            'ClientType':'PC-WEB'},body:bodyData,credentials:'include'})
            .then(r=>r.json().then(d=>cb(JSON.stringify(d))))
            .catch(e=>cb(JSON.stringify({error:e.toString()})));
            """
            result = driver.execute_async_script(js_code, url, body_str)
        else:
            js_code = """
            var url=arguments[0],cb=arguments[1];
            fetch(url,{method:'GET',headers:{'Content-Type':'application/json',
            'Accept':'application/json, text/plain, */*'},credentials:'include'})
            .then(r=>r.json().then(d=>cb(JSON.stringify(d))))
            .catch(e=>cb(JSON.stringify({error:e.toString()})));
            """
            result = driver.execute_async_script(js_code, url)
        return json.loads(result) if result else None
    except Exception as e:
        log(f"❌ 登录请求发包失败: {e}")
        return None

def perform_init_session(driver, max_retries=3):
    for i in range(max_retries):
        resp = send_login_request(driver, "https://passport.jlc.com/api/cas/login/get-init-session", "POST", {"appId": "JLC_PORTAL_PC", "clientType": "PC-WEB"})
        if resp and resp.get("success") and resp.get("code") == 200:
            return True
        if i < max_retries - 1: time.sleep(2)
    return False

def login_with_password(driver, username, password, captcha_ticket):
    try:
        enc_user = pwdEncrypt(username)
        enc_pass = pwdEncrypt(password)
    except Exception as e:
        return "other_error", None

    body = {"username": enc_user, "password": enc_pass, "isAutoLogin": False, "captchaTicket": captcha_ticket}
    resp = send_login_request(driver, "https://passport.jlc.com/api/cas/login/with-password", "POST", body)
    if not resp: return "other_error", None
    if resp.get("success") and resp.get("code") == 2017: return "success", resp
    if resp.get("code") == 10208: return "password_error", resp
    return "other_error", resp

def verify_login_on_member_page(driver, max_retries=3):
    for attempt in range(max_retries):
        try:
            try: driver.get("https://member.jlc.com/")
            except TimeoutException: driver.execute_script("window.stop();")
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)
            if "客编" in driver.page_source or "customerCode" in driver.page_source:
                return True
        except: pass
        if attempt < max_retries - 1: time.sleep(2)
    return False

def perform_login_flow(driver, username, password, max_retries=3):
    session_fail_count = 0
    for login_attempt in range(max_retries):
        log(f"🔐 开始登录流程 (尝试 {login_attempt + 1}/{max_retries})...")
        try:
            try: driver.get("https://passport.jlc.com")
            except TimeoutException: driver.execute_script("window.stop();")
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            if not perform_init_session(driver):
                session_fail_count += 1
                if session_fail_count >= 3: raise Exception("初始化 Session 失败")

            captcha_ticket = call_aliv3min_with_timeout()
            if not captcha_ticket: raise Exception("获取 CaptchaTicket 失败")

            status, resp = login_with_password(driver, username, password, captcha_ticket)
            if status == "password_error": return "password_error"
            if status != "success": raise Exception(f"登录失败，状态: {status}")

            if not verify_login_on_member_page(driver): raise Exception("登录验证失败")

            log("✅ 登录流程完成")
            return "success"
        except Exception as e:
            log(f"❌ 登录流程异常: {e}")
            if login_attempt < max_retries - 1: time.sleep(3)
            else: return "login_failed"
    return "login_failed"

# ======================== API 注入器与底层发包逻辑 ========================

def extract_custom_headers_from_logs(driver, header_keys):
    """从浏览器性能日志中提取指定的请求标头"""
    found_headers = {}
    keys_lower = [k.lower() for k in header_keys]
    try:
        logs = driver.get_log('performance')
        for entry in logs:
            try:
                log_entry = json.loads(entry['message'])
                message = log_entry.get('message', {})
                method = message.get('method', '')
                params = message.get('params', {})

                headers = {}
                if method == 'Network.requestWillBeSent':
                    headers = params.get('request', {}).get('headers', {})
                elif method == 'Network.requestWillBeSentExtraInfo':
                    headers = params.get('headers', {})

                for key, value in headers.items():
                    if key.lower() in keys_lower and value:
                        found_headers[key.lower()] = value
            except:
                continue
    except Exception as e:
        pass
    return found_headers

def send_api_request(driver, url, method="POST", body_dict=None, extra_headers=None):
    """通用的 JS Fetch 发包方法"""
    if extra_headers is None:
        extra_headers = {}
        
    headers_json = json.dumps(extra_headers)
    body_str = json.dumps(body_dict, ensure_ascii=False) if body_dict is not None else ""

    js_code = """
    var url = arguments[0];
    var method = arguments[1];
    var bodyData = arguments[2];
    var extraHeaders = JSON.parse(arguments[3]);
    var callback = arguments[4];

    var headersObj = {
        'Accept': 'application/json, text/plain, */*'
    };
    if(method === 'POST') {
        headersObj['Content-Type'] = 'application/json';
    }

    var xsrfToken = '';
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
        var cookie = cookies[i].trim();
        if (cookie.indexOf('XSRF-TOKEN=') === 0 || cookie.indexOf('xsrf-token=') === 0) {
            xsrfToken = decodeURIComponent(cookie.substring(11));
            break;
        }
    }
    if (xsrfToken) {
        headersObj['x-xsrf-token'] = xsrfToken;
    }

    for (var key in extraHeaders) {
        headersObj[key] = extraHeaders[key];
    }

    var fetchOpts = {
        method: method,
        headers: headersObj,
        credentials: 'include'
    };
    if(method === 'POST' && bodyData) {
        fetchOpts.body = bodyData;
    }

    fetch(url, fetchOpts)
    .then(r => r.text())
    .then(d => callback(d))
    .catch(e => callback(JSON.stringify({_fetch_error: e.toString()})));
    """
    
    try:
        result = driver.execute_async_script(js_code, url, method, body_str, headers_json)
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"_raw": result}
        return None
    except Exception as e:
        log(f"❌ 请求执行失败: {e}")
        return None

def api_with_retry(driver, url, method, payload, headers, max_retries=3):
    for i in range(max_retries):
        res = send_api_request(driver, url, method, payload, headers)
        if isinstance(res, dict) and "_fetch_error" not in res and "_raw" not in res:
            return res
        if i < max_retries - 1:
            time.sleep(1.5)
    return res

# ======================== 嘉立创活动 API 接口 ========================

def api_get_beans(driver, headers):
    """获取当前金豆数量"""
    url = "https://m.jlc.com/api/activity/front/getCustomerIntegral"
    res = api_with_retry(driver, url, "GET", None, headers)
    if res and res.get("success") and res.get("code") == 200:
        beans = res.get("data", {}).get("integralVoucher", 0)
        return int(float(beans))
    log(f"  [x] 获取金豆失败: {str(res)[:150]}")
    return 0

def api_get_CouponPackageList(driver, headers):
    """查询可兑换券包"""
    url = "https://m.jlc.com/api/activity/couponPackage/selectCouponPackageList"
    payload = {'limit': 1000}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return res.get("data", [])
    log(f"  [x] 查询可兑换券包失败: {str(res)[:150]}")
    return None

def api_get_CouponList(driver, headers):
    """查询可兑换优惠券"""
    url = "https://m.jlc.com/api/activity/front/selectGoodsList"
    payload = {'goodsType': 0}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return res.get("data", [])
    log(f"  [x] 查询可兑换优惠券失败: {str(res)[:150]}")
    return None

def api_do_exchange_package(driver, headers, goodsCode):
    """执行兑换券包"""
    url = "https://m.jlc.com/api/activity/couponPackage/exchangeCouponPackage"
    payload = {'goodsCode': goodsCode, 'source': 2}
    res = send_api_request(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return True
    log(f"  [x] 兑换券包失败: {str(res)[:150]}")
    return False

def api_do_exchange_coupon(driver, headers, goodsId):
    """执行兑换优惠券"""
    url = "https://m.jlc.com/api/activity/front/exchangeGoods"
    payload = {'goodsId': goodsId}
    res = send_api_request(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return True
    log(f"  [x] 兑换优惠券失败: {str(res)[:150]}")
    return False

# ======================== 核心活动编排逻辑 ========================

def perform_brand_activities(driver, username):
    """执行活动流程"""
    result = {
        'success': False,
        'error_msg': None,
        'initialGoldbean': 0,
        'finalGoldbean': 0,
        'exchangePackageCount': 0,
        'exchangeCouponCount': 0,
        'choosePackageCode': None,
        'choosePackageTitle': None,
        'chooseCouponSku': None,
        'chooseCouponTitle': None,
    }
    
    try:
        log(f"客户编号: {username}")

        # 1. 访问活动页面收集凭证
        activity_url = "https://m.jlc.com/pages-common/integral/goods?goodsType=1"
        log("- 正在打开活动页以同步环境凭证...")
        driver.get("https://m.jlc.com/pages-common/integral/index") # 预热
        time.sleep(3)
        driver.get('chrome://network-errors/#') # 清理网络日志
        driver.get_log('performance') 
        
        try: 
            driver.get(activity_url)
        except TimeoutException: 
            driver.execute_script("window.stop();")
        
        log("- 等待 8 秒让 SSO Token 生成...")
        time.sleep(8)
        
        # 提取必须的鉴权头
        headers = extract_custom_headers_from_logs(driver, ['secretkey', 'x-jlc-accesstoken', 'x-jlc-clienttype'])
        if not headers.get('secretkey'):
            raise Exception("未能从浏览器日志提取到 secretkey")

        result['initialGoldbean'] = api_get_beans(driver, headers)
        log(f"- 初始金豆: {result['initialGoldbean']}")

        # 2. 兑换券包
        currentGoldbean = 0
        failCount = 0
        while True:
            currentGoldbean = api_get_beans(driver, headers)
            log(f"- 当前金豆: {currentGoldbean}，开始匹配可兑换券包")

            couponPackageList = api_get_CouponPackageList(driver, headers)
            
            # 根据金豆数量选择可兑换券包
            if couponPackageList and currentGoldbean > 0:
                # 筛选出可兑换的商品（priceNum <= 当前金豆数量）
                affordable_package = [package for package in couponPackageList if package.get('priceBean', 9999) <= currentGoldbean]
                
                if affordable_package:
                    # 选择金豆价值最高的商品（priceBean最大的）
                    choosePackage = max(affordable_package, key=lambda x: x.get('priceBean', 0))
                    result['choosePackageTitle'] = choosePackage.get('goodsTitle')
                    result['choosePackageCode'] = choosePackage.get('goodsCode')

                    log(f"选择券包: {result['choosePackageTitle']}({result['choosePackageCode']}) (消耗金豆: {choosePackage.get('priceBean')})")
                else:
                    log(f"- 当前金豆 {currentGoldbean} 已不足以兑换任何券包，兑换券包结束")
                    break
            else:
                log(f"- 无可兑换券包或已无金豆，兑换券包结束")
                break
            
            if api_do_exchange_package(driver, headers, result['choosePackageCode']):
                log(f"- 兑换券包 {result['choosePackageTitle']} 成功")
                result['exchangePackageCount'] += 1
            else:
                log(f"- 兑换券包 {result['choosePackageTitle']} 失败，跳过当前券包")
                failCount += 1
                if failCount >= 3:
                    log(f"- 兑换券包 {result['choosePackageTitle']} 失败 3 次，跳过当前券包")
                    break
            time.sleep(random.randint(2, 5))
    
        # 3. 兑换优惠券
        failCount = 0
        while True:  
            currentGoldbean = api_get_beans(driver, headers)
            log(f"- 当前金豆: {currentGoldbean}，开始匹配可兑换优惠券")

            couponList = api_get_CouponList(driver, headers)
            
            # 根据金豆数量选择可兑换优惠券
            if couponList and currentGoldbean > 0:
                # 筛选出可兑换的商品（priceNum <= 当前金豆数量）
                affordableCoupon = [package for package in couponList if package.get('priceBean', 9999) <= currentGoldbean]
                
                if affordableCoupon:
                    # 选择金豆价值最高的商品（priceBean最大的）
                    chooseCoupon = max(affordableCoupon, key=lambda x: x.get('priceBean', 0))
                    result['chooseCouponTitle'] = chooseCoupon.get('goodsTitle')
                    result['chooseCouponID'] = chooseCoupon.get('goodsId')

                    log(f"选择优惠券: {result['chooseCouponTitle']}({result['chooseCouponID']}) (消耗金豆: {chooseCoupon.get('priceBean')})")
                else:
                    log(f"- 当前金豆 {currentGoldbean} 已不足以兑换任何优惠券，兑换优惠券结束")
                    break
            else:
                log(f"- 无可兑换优惠券或已无金豆，兑换优惠券结束")
                break
            
            if api_do_exchange_coupon(driver, headers, result['chooseCouponID']):
                log(f"- 兑换优惠券 {result['chooseCouponTitle']} 成功")
                result['exchangeCouponCount'] += 1
            else:
                log(f"- 兑换优惠券 {result['chooseCouponTitle']} 失败，跳过当前优惠券")
                failCount += 1
                if failCount >= 3:
                    log(f"- 兑换优惠券 {result['chooseCouponTitle']} 失败 3 次，跳过当前优惠券")
                    break

            time.sleep(random.randint(2, 5))

    except Exception as e:
        log(f"- ❌ 活动处理异常: {e}")
        result['success'] = False
        result['error_msg'] = str(e)
        
    return result

def sign_in_account(username, password, account_index, total_accounts):
    log(f"开始处理账号 {username}（{account_index}/{total_accounts}）")
    
    result = {
        'account_index': account_index,
        'customer_code': username,
        'login_success': False,
        'password_error': False,
        'error_msg': None
    }

    driver = None
    user_data_dir = tempfile.mkdtemp()

    try:
        driver = create_chrome_driver(user_data_dir)
        log(f"账号 {username} - 正在创建 Chrome 浏览器实例...")
        
        # 登录流程
        login_status = perform_login_flow(driver, username, password, max_retries=3)
        if login_status == "password_error":
            result['password_error'] = True
            return result
        if login_status != "success":
            result['error_msg'] = "登录失败"
            return result

        result['login_success'] = True

        # 活动流程
        act_res = perform_brand_activities(driver, username)
        
        result['initialGoldbean'] = act_res['initialGoldbean']
        result['finalGoldbean'] = act_res['finalGoldbean']
        result['exchangePackageCount'] = act_res['exchangePackageCount']
        result['exchangeCouponCount'] = act_res['exchangeCouponCount']

        if not act_res['success']:
            result['error_msg'] = act_res.get('error_msg')

    except Exception as e:
        log(f"账号 {username} - ❌ 账号整体执行异常: {e}")
        result['error_msg'] = str(e)
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if os.path.exists(user_data_dir):
            try: shutil.rmtree(user_data_dir, ignore_errors=True)
            except: pass
    
    return result

# ======================== 推送功能 ========================

def push_summary():
    if not summary_logs:
        return
    title = "嘉立创金豆活动总结"
    text = "\n".join(summary_logs)
    full_text = f"{title}\n{text}"
    
    channels = [
        ('TELEGRAM_BOT_TOKEN', lambda: requests.get(f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage", params={'chat_id': os.getenv('TELEGRAM_CHAT_ID'), 'text': full_text})),
        ('WECHAT_WEBHOOK_KEY', lambda: requests.post(os.getenv('WECHAT_WEBHOOK_KEY') if os.getenv('WECHAT_WEBHOOK_KEY').startswith('http') else f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={os.getenv('WECHAT_WEBHOOK_KEY')}", json={"msgtype": "text", "text": {"content": full_text}})),
        ('DINGTALK_WEBHOOK', lambda: requests.post(os.getenv('DINGTALK_WEBHOOK') if os.getenv('DINGTALK_WEBHOOK').startswith('http') else f"https://oapi.dingtalk.com/robot/send?access_token={os.getenv('DINGTALK_WEBHOOK')}", json={"msgtype": "text", "text": {"content": full_text}})),
        ('PUSHPLUS_TOKEN', lambda: requests.post("http://www.pushplus.plus/send", json={"token": os.getenv('PUSHPLUS_TOKEN'), "title": title, "content": text})),
        ('SERVERCHAN_SCKEY', lambda: requests.post(f"https://sctapi.ftqq.com/{os.getenv('SERVERCHAN_SCKEY')}.send", data={"title": title, "desp": text}))
    ]
    
    for key, req_func in channels:
        if os.getenv(key):
            try:
                if req_func().status_code == 200:
                    log(f"{key.split('_')[0]}-日志已推送", show_time=False)
            except: pass

def main():
    global in_summary
    
    if len(sys.argv) < 3:
        print("用法: python JLC618.py 账号 密码")
        sys.exit(1)
    
    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    
    log(f"\n{'='*50}", show_time=False)
    log(f"🔄 开始任务...")

    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)

    total_accounts = len(usernames)
    all_results = []

    for i, (u, p) in enumerate(zip(usernames, passwords), 1):
        log(f"\n{'='*50}", show_time=False)
        
        max_attempts = 2
        res = None
        
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                log(f"🔄 账号 {i} 正在进行第 {attempt - 1} 次重试...")
                
            res = sign_in_account(u, p, i, total_accounts)
            
            if res.get('password_error'):
                break
            if res.get('login_success') and (res.get('exchangePackageCount') or res.get('exchangeCouponCount')):
                break
                
            if attempt < max_attempts:
                log(f"⚠ 账号 {i} 执行意外失败，等待后重试...")
                time.sleep(random.randint(5, 10))
                
        all_results.append(res)
        
        if i < total_accounts:
            time.sleep(random.randint(3, 5))

    # 输出详细总结
    in_summary = True  # 启用总结收集
    log("\n" + "=" * 60, show_time=False)
    log("📊 嘉立创金豆消耗任务完成总结", show_time=False)
    log("=" * 60, show_time=False)
    
    for r in all_results:
        idx = r['account_index']
        code = r['customer_code']
        
        if r['password_error']:
            log(f"账号  {idx} ({code}) 详细结果: [密码错误]", show_time=False)
            log("  └── 状态: ❌ 账号或密码错误，跳过", show_time=False)
        else:
            log(f"账号  {idx} ({code}) 详细结果:", show_time=False)
            
            err_msg = r.get('error_msg')
            login_str = "✅ 成功" if r['login_success'] else f"❌ 失败 ({err_msg})"
            log(f"  ├── 登录状态: {login_str}", show_time=False)
            
            if r['exchangePackageCount'] or r['exchangeCouponCount']:
                log(f"  ├── ✅ 已兑换 {r['exchangePackageCount']} 个商品，{r['exchangeCouponCount']} 个优惠券")
                log(f"  ├── 初始金豆: {r['initialGoldbean']}, 兑换后金豆: {r['finalGoldbean']},  消耗金豆: {r['initialGoldbean'] - r['finalGoldbean']}")
            else:
                log(f"  ├── ❌ 兑换商品失败: {r['error_msg']}")



    log("=" * 60, show_time=False)
    push_summary()
    
    sys.exit(0)

if __name__ == "__main__":
    main()
