import os
import sys
import time
import json
import tempfile
import random
import requests
import subprocess
import threading
import queue
import re
import shutil
import psutil
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from Utils import pwdEncrypt


# ======================== 全局变量与日志收集 ========================
in_summary = False
summary_logs = []

class ProxyConnectionError(Exception):
    """代理失效或网络连接异常时抛出，用于触发重连环境"""
    pass

def log(msg, show_time=True):
    if show_time:
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    else:
        full_msg = msg
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)

def cleanup_zombie_chrome():
    """清理超时残留的 Chrome 及相关进程"""
    current_time = time.time()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            name = proc.info.get('name')
            if name and ('chrome' in name.lower() or 'chromedriver' in name.lower()):
                cmdline = proc.info.get('cmdline')
                if cmdline:
                    cmd_str = ' '.join(cmdline)
                    # 只清理 headless 下或本程序产生残留的超时进程，设置 30 分钟防误杀
                    if '--headless' in cmd_str or 'user-data-dir' in cmd_str:
                        create_time = proc.info.get('create_time', current_time)
                        if current_time - create_time > 1800:
                            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

def get_valid_proxy(timeout=None):
    """从代理池 API 获取代理并进行连通性测试"""
    apikey = os.getenv('DM_APIKEY')
    pwd = os.getenv('DM_PWD')
    api_url = f"http://api.dmdaili.com/dmgetip.asp?apikey={apikey}&pwd={pwd}&getnum=1&httptype=1&geshi=2&fenge=1&fengefu=&operate=all"
    start_time = time.time()
    
    while True:
        if timeout and (time.time() - start_time) > timeout:
            log(f"❌ 代理API: 获取或测试代理已达到设定的超时时间 ({timeout}秒)")
            return None
            
        try:
            resp = requests.get(api_url, timeout=10)
            data = resp.json()
            
            if data.get("code") == 605:
                log(f"⚠ 代理API: 白名单未生效或需等待 ({data.get('msg')})，等待15秒...")
                time.sleep(15)
                continue
            elif data.get("code") == 1 and "Too Many Requests" in data.get("msg", ""):
                time.sleep(5)
                continue
            elif data.get("code") == 0 and data.get("data"):
                p_info = data["data"][0]
                ip, port, city = p_info.get("ip"), p_info.get("port"), p_info.get("city", "未知")
                proxy_str = f"{ip}:{port}" 
                log(f"🔗 获取到代理: {proxy_str} [位置: {city}]，正在测试...")
                
                try:
                    test_resp = requests.get("https://passport.jlc.com", proxies={"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}, timeout=5)
                    if test_resp.status_code == 200:
                        log("✅ 代理测试成功，延迟正常")
                        return proxy_str
                    else:
                        log(f"⚠ 代理测试失败，返回状态码: {test_resp.status_code}，重新获取...")
                        continue
                except requests.exceptions.ConnectTimeout:
                    log("⚠ 代理测试连接超时，重新获取...")
                    continue
                except requests.exceptions.ReadTimeout:
                    log("⚠ 代理测试读取超时，重新获取...")
                    continue
                except requests.exceptions.ProxyError as e:
                    log(f"⚠ 代理拒绝连接或代理错误 ({e})，重新获取...")
                    continue
                except requests.exceptions.ConnectionError as e:
                    log(f"⚠ 代理连接失败 ({e})，重新获取...")
                    continue
                except Exception as e:
                    log(f"⚠ 代理测试未知错误 ({type(e).__name__}: {e})，重新获取...")
                    continue
            else:
                log(f"❌ 代理API返回异常内容: {data}")
                time.sleep(3)
        except Exception as e:
            time.sleep(3)

def force_kill_driver(driver):
    """彻底强杀 Chrome 以释放系统资源"""
    if not driver:
        return
    try:
        driver_pid = driver.service.process.pid
        try:
            parent = psutil.Process(driver_pid)
            for child in parent.children(recursive=True):
                try: child.kill()
                except: pass
            try: parent.kill()
            except: pass
        except: pass
    except Exception: pass
    finally:
        try: driver.quit()
        except: pass

class DriverWrapper:
    """包装 Driver 与 Cookie，用于在代理失效时重建会话断点续连"""
    def __init__(self, user_data_dir):
        self.user_data_dir = user_data_dir
        self.driver = None
        self.saved_cookies = []
    
    def set_driver(self, driver):
        self.driver = driver
    
    def get_driver(self):
        return self.driver
        
    def save_cookies(self):
        """分别访问 passport 和 m 站，抓取所有域下的完整 Cookie"""
        if self.driver:
            self.saved_cookies = []
            domains_to_fetch = ["https://passport.jlc.com/favicon.ico", "https://m.jlc.com/favicon.ico"]
            for d in domains_to_fetch:
                try:
                    self.driver.get(d)
                    time.sleep(0.5)
                    cookies = self.driver.get_cookies()
                    for c in cookies:
                        # 防止重复保存同名同域的 cookie
                        if not any(sc['name'] == c['name'] and sc['domain'] == c['domain'] for sc in self.saved_cookies):
                            self.saved_cookies.append(c)
                except: pass
                
    def reconnect_proxy(self):
        """换新代理并恢复跨域的 Cookies"""
        self.save_cookies()
        if self.driver:
            try:
                # 优雅退出，确保缓存/LocalStorage/Cookie能够写回到 user_data_dir 中
                self.driver.quit()
            except: pass
            force_kill_driver(self.driver)
            self.driver = None
            
        proxy_str = get_valid_proxy(timeout=300)
        if not proxy_str:
            raise Exception("获取有效代理超时")
            
        self.driver = create_chrome_driver(self.user_data_dir, proxy_str)
        self.driver.set_page_load_timeout(40)
        self.driver.set_script_timeout(40)
        
        valid_keys = ['name', 'value', 'domain', 'path', 'secure', 'httpOnly', 'expiry', 'sameSite']
        
        # 恢复 cookie，预热 M站 和 Passport 站并分别注入以规避 webdriver 跨域限制
        domains_to_inject = ["https://passport.jlc.com/favicon.ico", "https://m.jlc.com/favicon.ico"]
        for d in domains_to_inject:
            try:
                self.driver.get(d)
                for c in self.saved_cookies:
                    clean_c = {k: v for k, v in c.items() if k in valid_keys}
                    try: self.driver.add_cookie(clean_c)
                    except: pass
            except: pass

# ======================== 浏览器与登录验证核心逻辑 ========================

def create_chrome_driver(user_data_dir=None, proxy_str=None):
    """创建 Chrome 浏览器实例（启用性能日志以抓取 header）"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    if proxy_str:
        chrome_options.add_argument(f"--proxy-server=http://{proxy_str}")

    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")

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
    """通用的 JS Fetch 发包方法，加入超时保护返回 _fetch_error 以供网络重连使用"""
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

    var isDone = false;
    var timer = setTimeout(function() {
        if (!isDone) {
            isDone = true;
            callback(JSON.stringify({_fetch_error: "JS内部fetch超时 (15s)"}));
        }
    }, 15000);

    fetch(url, fetchOpts)
    .then(async r => {
        const text = await r.text();
        if (isDone) return;
        isDone = true;
        clearTimeout(timer);
        callback(text);
    })
    .catch(e => {
        if (isDone) return;
        isDone = true;
        clearTimeout(timer);
        callback(JSON.stringify({_fetch_error: e.toString()}));
    });
    """
    
    try:
        result = driver.execute_async_script(js_code, url, method, body_str, headers_json)
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"_raw": result}
        return None
    except TimeoutException as e:
        return {"_fetch_error": f"JS执行超时: {e}"}
    except Exception as e:
        return {"_fetch_error": f"底层执行异常: {e}"}

def api_with_retry(driver, url, method, payload, headers, max_retries=3):
    for i in range(max_retries):
        res = send_api_request(driver, url, method, payload, headers)
        if isinstance(res, dict) and "_fetch_error" not in res and "_raw" not in res:
            return res
        if i < max_retries - 1:
            time.sleep(1.5)
            
    if isinstance(res, dict) and "_fetch_error" in res:
        raise ProxyConnectionError(res["_fetch_error"])
    elif isinstance(res, dict) and "_raw" in res:
        raise ProxyConnectionError(f"返回非JSON响应，可能被代理页面拦截: {str(res)[:100]}")
    elif res is None:
        raise ProxyConnectionError("请求执行失败，返回为空，可能是浏览器崩溃或完全断连")
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

def api_check_vote(driver, headers):
    """查询是否已投票"""
    url = "https://m.jlc.com/api/activity/member/day/activity/ns/selectVoteConfig"
    payload = {"activityAccessId": "fc7534debba644c5a0d26af52651d16f"}
    voteProductConfigList = {
        "SKUJY5": "京东京造户外露营车",
        "SKUJLF": "金士顿U盘 64GB",
        "SKUJ3Y": "绿林开口梅花扳手套装",
        "SKUJ35": "三层落地置物架"
    }
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        needUseVoucher = res.get("data").get("needUseVoucher", False)
        myVotedProductSku = res.get("data").get("myVotedProductSku", "")
        if needUseVoucher:
            return False
        else:
            log(f"  [-] 已投票: {voteProductConfigList.get(myVotedProductSku, '未知商品')}")
            return True

    log(f"  [x] 查询投票状态失败: {str(res)[:150]}")
    return False

def api_do_vote(driver, headers):
    """执行活动投票"""
    url = "https://m.jlc.com/api/activity/member/day/activity/vote"
    payload = {"activityAccessId": "fc7534debba644c5a0d26af52651d16f", "productSku": "SKUJY5"}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return True
    log(f"  [x] 投票失败: {str(res)[:150]}")
    return False

def api_get_draw_chances(driver, headers):
    """查询剩余抽奖次数"""
    url = "https://m.jlc.com/api/cgi/operationService/front/lottery/getLuckyKeyCount"
    payload = {"activityCode": "LAKU"}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        return int(res.get("data", {}).get("count", 0))
    log(f"  [x] 查询抽奖次数失败: {str(res)[:150]}")
    return 0

def api_get_exchange_status(driver, headers):
    """查询兑换抽奖次数的状态 (兑换了多少次/最大多少次)"""
    url = "https://m.jlc.com/api/activity/brand/activity/ns/getVoucherLotteryDetail"
    res = api_with_retry(driver, url, "POST", {}, headers)
    if res and res.get("success") and res.get("code") == 200:
        data = res.get("data", {})
        return {
            "exc_num": int(data.get("exchangeNum", 0)),
            "exc_max": int(data.get("exchangeMaxNum", 3))
        }
    log(f"  [x] 查询兑换状态失败: {str(res)[:150]}")
    return {"exc_num": 0, "exc_max": 3}

def api_do_exchange(driver, headers):
    """使用5金豆兑换一次抽奖机会"""
    url = "https://m.jlc.com/api/activity/brand/activity/exchangeLotteryChance"
    res = api_with_retry(driver, url, "POST", {}, headers)
    if res and res.get("success") and res.get("code") == 200:
        return True
    log(f"  [x] 兑换抽奖机会失败: {str(res)[:150]}")
    return False

def api_do_draw(driver, headers):
    """执行抽奖"""
    url = "https://m.jlc.com/api/cgi/operationService/front/lottery/turn"
    payload = {"clientType": "WEB", "activityCode": "LAKU"}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if res and res.get("success") and res.get("code") == 200:
        prize_list = res.get("data", {}).get("prizeList", [])
        if prize_list:
            return prize_list[0].get("prizeTitle", "未知奖品")
    log(f"  [x] 抽奖失败: {str(res)[:150]}")
    return None

def api_query_wins(driver, headers):
    """查询该账号所有中奖记录"""
    url = "https://m.jlc.com/api/cgi/operationService/front/lottery/queryWins"
    payload = {"pageNum": 1, "pageSize": 1000, "activityCode": "LAMD"}
    res = api_with_retry(driver, url, "POST", payload, headers)
    if isinstance(res, dict) and res.get("success") and res.get("code") == 200:
        return res.get("data", {}).get("data", [])
    log(f"  [x] 查询中奖记录发包失败: {str(res)[:150] if res else 'None'}")
    return None

# ======================== 核心活动编排逻辑 ========================

def perform_brand_activities(driver_wrapper, account_index, username, activity_period):
    """执行活动流程 (已包含自动从断点重接代理的机制)"""
    result = {
        'customer_code': username,  # 客户编号就是输入的账号
        'initial_jindou_fetched': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'vote_status': '未投票',
        'exchange_count': 0,
        'exchange_max': 3,
        'lottery_results': [],
        'all_wins': [],
        'success': True,
        'error_msg': None
    }
    
    max_proxy_retries = 10
    proxy_retry = 0
    activity_period = "1"
    while proxy_retry < max_proxy_retries:
        try:
            if proxy_retry > 0:
                log(f"账号 {account_index} - (代理重试: {proxy_retry}/{max_proxy_retries}) 恢复环境状态继续执行...")
            else:
                log(f"账号 {account_index} - 客户编号: {result['customer_code']}")

            driver = driver_wrapper.get_driver()

            # 1. 访问活动页面收集凭证
            activity_url = "https://m.jlc.com/pages/web-view/index?url=https%253A%252F%252Fm.jlc.com%252Fpages-promo%252Fbrand-campaign%252Findex%253Fsource%253Djlc_mobile_app%2526clientType%253DWEB&title=%E5%98%89%E7%AB%8B%E5%88%9B%E9%9B%86%E5%9B%A2&syncLogin="
            log(f"账号 {account_index} - 正在打开活动页以同步环境凭证...")
            
            try:
                driver.get("https://m.jlc.com/pages-promo/brand-campaign/index") # 预热
                time.sleep(3)
                driver.get('chrome://network-errors/#') # 清理网络日志
                driver.get_log('performance') 
                driver.get(activity_url)
            except TimeoutException as te:
                raise ProxyConnectionError(f"代理网络或页面加载超时: {te}")
            except Exception as e:
                raise ProxyConnectionError(f"代理网络底层异常: {e}")
            
            log(f"账号 {account_index} - 等待 8 秒让 SSO Token 生成...")
            time.sleep(8)
            
            # 提取必须的鉴权头
            headers = extract_custom_headers_from_logs(driver, ['secretkey', 'x-jlc-accesstoken', 'x-jlc-clienttype'])
            if not headers.get('secretkey'):
                raise ProxyConnectionError("未能从浏览器日志提取到 secretkey, 可能页面未正确加载完毕")
            
            # 2. 获取初始金豆
            if not result['initial_jindou_fetched']:
                result['initial_jindou'] = api_get_beans(driver, headers)
                # 校验是否真因为没登入导致失败 401
                if result['initial_jindou'] == 0:
                    pass # api 异常打印会显示，且流程继续走如果需要
                result['initial_jindou_fetched'] = True
                log(f"账号 {account_index} - 当前金豆: {result['initial_jindou']}")

            # 3. 处理投票
            if activity_period == "1":
                log(f"账号 {account_index} - 活动蓄力期，进入投票流程 ...")

                if result['vote_status'] in ['未投票']:
                    is_voted = api_check_vote(driver, headers)
                    if is_voted:
                        result['vote_status'] = '已投票'
                        log(f"账号 {account_index} - 状态: {result['vote_status']}")
                    else:
                        log(f"账号 {account_index} - 未投票，准备发包投票...")
                        if api_do_vote(driver, headers):
                            result['vote_status'] = '投票成功'
                            log(f"账号 {account_index} - ✅ 投票成功")
                        else:
                            result['vote_status'] = '投票失败'
                            raise Exception("投票接口调用逻辑拒绝（可能是未登录导致 401 或活动已结束）")
            
            # 4. 智能循环: 抽奖 <-> 兑换
            elif activity_period == "2":
                log(f"账号 {account_index} - 活动进行期，进入兑换次数和抽奖流程 ...")
                
                exc_status = api_get_exchange_status(driver, headers)
                result['exchange_count'] = exc_status['exc_num']
                result['exchange_max'] = exc_status['exc_max']
                
                loop_guard = 0
                while loop_guard < 15: # 防死循环
                    loop_guard += 1
                    
                    # 第一步：查抽奖次数
                    draw_chances = api_get_draw_chances(driver, headers)
                    
                    if draw_chances > 0:
                        # 优先把所有次数抽光
                        log(f"账号 {account_index} - 发现 {draw_chances} 次抽奖机会，开始抽奖...")
                        prize = api_do_draw(driver, headers)
                        if prize:
                            log(f"账号 {account_index} - 🎉 抽奖获得: {prize}")
                            result['lottery_results'].append(prize)
                        time.sleep(2)
                        continue # 抽奖后直接进行下一次循环（可能还有剩余次数）
                        
                    else:
                        # 次数为0，判断是否能兑换
                        exc_status = api_get_exchange_status(driver, headers)
                        result['exchange_count'] = exc_status['exc_num']
                        
                        if exc_status['exc_num'] >= exc_status['exc_max']:
                            log(f"账号 {account_index} - 兑换次数已达上限 ({exc_status['exc_num']}/{exc_status['exc_max']})，结束流程。")
                            break
                        
                        # 检查金豆是否够5个
                        current_beans = api_get_beans(driver, headers)
                        if current_beans < 5:
                            log(f"账号 {account_index} - 剩余金豆不足 5 个 (当前 {current_beans})，无法继续兑换，结束流程。")
                            break
                            
                        # 发包兑换
                        log(f"账号 {account_index} - 花费 5 金豆兑换抽奖机会 (已兑换: {exc_status['exc_num']}/{exc_status['exc_max']})...")
                        if api_do_exchange(driver, headers):
                            log(f"账号 {account_index} - ✅ 兑换成功！")
                            result['exchange_count'] += 1
                            time.sleep(1)
                            result['success'] = True
                            continue # 兑换成功后，进入下一个循环即可触发上面的抽奖逻辑
                        else:
                            log(f"账号 {account_index} - ❌ 兑换失败，中断流程。")
                            break

            # 5. 获取所有中奖记录
                log(f"账号 {account_index} - 正在查询所有中奖记录...")
                query_wins = None
                for attempt in range(3):
                    try:
                        query_wins = api_query_wins(driver, headers)
                        if query_wins is not None:
                            break
                    except ProxyConnectionError as e:
                        log(f"  [x] 查询中奖记录时遇到网络异常: {e}")
                    except Exception as e:
                        log(f"  [x] 查询中奖记录时遇到未知异常: {e}")
                        
                    if attempt < 2:
                        log(f"账号 {account_index} - ❌ 查询中奖记录失败，刷新页面并重试 ({attempt+1}/3)...")
                        try:
                            driver.refresh()
                            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        except Exception:
                            pass
                        time.sleep(5)
                    
                result['all_wins'] = query_wins
                log(f"账号 {account_index} - 查询到 {len(query_wins)} 条中奖记录。")

            else:
                log("活动锁定期，不处理投票、兑换和抽奖")


            # 6. 获取最终金豆
            result['final_jindou'] = api_get_beans(driver, headers)
            log(f"账号 {account_index} - ✅ 该账号处理完成")
            break # 成功完成退出 proxy 循环

        except ProxyConnectionError as e:
            proxy_retry += 1
            log(f"账号 {account_index} - ⚠ 代理失效或网络异常，准备重新获取代理重试 ({proxy_retry}/{max_proxy_retries}): {e}")
            if proxy_retry >= max_proxy_retries:
                result['success'] = False
                result['error_msg'] = f"连续 {max_proxy_retries} 次代理失效重试均失败，放弃该账号"
                break
            driver_wrapper.reconnect_proxy()
            
        except Exception as e:
            log(f"账号 {account_index} - ❌ 活动处理业务异常: {e}")
            result['success'] = False
            result['error_msg'] = str(e)
            break
            
    return result

def sign_in_account(username, password, account_index, total_accounts, activity_period):
    log(f"开始处理账号 {account_index}/{total_accounts}")
    
    result = {
        'account_index': account_index,
        'customer_code': username,  # 客户编号就是输入的账号
        'login_success': False,
        'activity_success': False,
        'password_error': False,
        # 活动明细数据
        'vote_status': '未投票',
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_change': 0,
        'exchange_count': 0,
        'exchange_max': 3,
        'lottery_results': [],
        'all_wins': [],
        'error_msg': None
    }

    driver_wrapper = DriverWrapper(tempfile.mkdtemp())

    try:
        # 1. 登录时不需要使用代理
        driver = create_chrome_driver(driver_wrapper.user_data_dir, proxy_str=None)
        driver_wrapper.set_driver(driver)

        # 登录流程
        login_status = perform_login_flow(driver_wrapper.get_driver(), username, password, max_retries=3)
        if login_status == "password_error":
            result['password_error'] = True
            return result
        if login_status != "success":
            result['error_msg'] = "登录失败"
            return result

        result['login_success'] = True

        # ====================================================
        # 关键修正：预热 m.jlc.com，确保本地网络完成 SSO 跨域登录状态的同步
        # 因为后续所有的发包都在 m.jlc.com，在此处访问触发通行证凭证写入 M 站
        # ====================================================
        log(f"账号 {account_index} - 正在同步移动端 M站 的 SSO 登录状态...")
        try:
            driver_wrapper.get_driver().get("https://m.jlc.com/pages/my/index")
            time.sleep(3) # 留时间给 SSO 重定向完成写入 cookie
        except: pass

        # 2. 登录成功后，挂上代理网络去进行活动请求
        log(f"账号 {account_index} - 准备切换到代理网络进行活动流程...")
        driver_wrapper.reconnect_proxy()

        # 活动流程，传入 username 作为 customer_code 并在内部处理异常断开等
        act_res = perform_brand_activities(driver_wrapper, account_index, username, activity_period)
        
        result['activity_success'] = act_res['success']
        result['customer_code'] = act_res['customer_code']
        result['vote_status'] = act_res['vote_status']
        result['initial_jindou'] = act_res['initial_jindou']
        result['final_jindou'] = act_res['final_jindou']
        result['jindou_change'] = act_res['final_jindou'] - act_res['initial_jindou']
        result['exchange_count'] = act_res['exchange_count']
        result['exchange_max'] = act_res['exchange_max']
        result['lottery_results'] = act_res['lottery_results']
        result['all_wins'] = act_res.get('all_wins', [])
        
        if not act_res['success']:
            result['error_msg'] = act_res.get('error_msg')

    except Exception as e:
        log(f"账号 {account_index} - ❌ 账号整体执行异常: {e}")
        result['error_msg'] = str(e)
    finally:
        if driver_wrapper.driver:
            force_kill_driver(driver_wrapper.driver)
        if os.path.exists(driver_wrapper.user_data_dir):
            try: shutil.rmtree(driver_wrapper.user_data_dir, ignore_errors=True)
            except: pass
    
    return result

# ======================== 推送功能 ========================

def push_summary():
    if not summary_logs:
        return
    title = "嘉立创盛夏福利日活动总结"
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
    
    if len(sys.argv) < 4:
        print("用法: python summer.py 账号1,账号2... 密码1,密码2... 活动时期 [失败退出标志true/false]")
        sys.exit(1)
    
    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    activity_period = sys.argv[3].strip() if len(sys.argv) >= 4 else "0"
    enable_failure_exit = (sys.argv[4].lower() == 'true') if len(sys.argv) >= 5 else False
    
    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)
    
    total_accounts = len(usernames)
    all_results = []
    
    for i, (u, p) in enumerate(zip(usernames, passwords), 1):
        # 运行时防止僵尸残留造成内存泄漏
        cleanup_zombie_chrome()
        
        log(f"\n{'='*50}", show_time=False)
        
        max_attempts = 4
        res = None
        
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                log(f"🔄 账号 {i} 正在进行第 {attempt - 1} 次重试...")
                
            res = sign_in_account(u, p, i, total_accounts, activity_period)
            
            if res.get('password_error'):
                break
            if res.get('login_success') and res.get('activity_success'):
                break
                
            if attempt < max_attempts:
                log(f"⚠ 账号 {i} 执行意外失败，等待后重试...")
                time.sleep(random.randint(5, 10))
                
        all_results.append(res)
        
        if i < total_accounts:
            time.sleep(random.randint(3, 5))
            
    # 输出详细总结
    log("\n" + "=" * 60, show_time=False)
    in_summary = True  # 启用总结收集
    log("📊 嘉立创盛夏福利日活动任务完成总结", show_time=False)
    log("=" * 60, show_time=False)
    
    vote_ok = login_ok = activity_ok = total_jindou = total_exc = total_wins = 0
    failed_accs = []
    
    for r in all_results:
        idx = r['account_index']
        code = r['customer_code']
        
        if r['password_error']:
            log(f"账号 {idx} ({code}) 详细结果: [密码错误]", show_time=False)
            log("  └── 状态: ❌ 账号或密码错误，跳过此账号", show_time=False)
            failed_accs.append(idx)
        else:
            log(f"账号 {idx} ({code}) 详细结果:", show_time=False)
            err_msg = r.get('error_msg')
            login_str = "✅ 成功" if r['login_success'] else f"❌ 失败 ({err_msg})"
            log(f"  ├── 登录状态: {login_str}", show_time=False)
            
            if r['login_success']:
                login_ok += 1
                act_str = "✅ 成功" if r['activity_success'] else f"❌ 失败 ({err_msg})"

                log(f"  ├── 活动状态: {act_str}", show_time=False)

                if r['activity_success'] or r['vote_status'] == '已投票':
                    if r['vote_status'] == '已投票':
                        vote_ok += 1
                    if r['activity_success']:
                        activity_ok += 1
                        
                    log(f"  ├── 投票状态: {r['vote_status']}", show_time=False)
                    log(f"  ├── 金豆变化: {r['initial_jindou']} → {r['final_jindou']} ({r['jindou_change']:+d})", show_time=False)
                    log(f"  ├── 兑换进度: {r['exchange_count']}/{r['exchange_max']} 次", show_time=False)
                    log(f"  ├── 中奖记录: 共 {len(r['all_wins'])} 条", show_time=False)
                    
                    for idx_p, win in enumerate(r['all_wins'], 1):
                        prize_title = win.get("prizeTitle", "未知奖品")
                        create_time_ms = win.get("createTime")
                        if create_time_ms:
                            time_str = datetime.fromtimestamp(create_time_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
                            log(f"  │   ├── [{time_str}] 获得: {prize_title}", show_time=False)
                        else:
                            log(f"  │   ├── 获得: {prize_title}", show_time=False)
                        
                    total_jindou += r['jindou_change']
                    total_exc += r['exchange_count']
                    total_wins += len(r['all_wins'])
                else:
                    failed_accs.append(idx)
            else:
                failed_accs.append(idx)
        log("-" * 60, show_time=False)
    
    # 总体统计
    log("📈 总体统计:", show_time=False)
    log(f"  ├── 总账号数: {total_accounts}", show_time=False)
    log(f"  ├── 登录成功: {login_ok}/{total_accounts}", show_time=False)
    log(f"  ├── 投票成功: {vote_ok}/{total_accounts}", show_time=False)
    log(f"  ├── 活动成功: {activity_ok}/{total_accounts}", show_time=False)
    log(f"  ├── 总计金豆变化: {total_jindou:+d}", show_time=False)
    log(f"  ├── 总计兑换次数: {total_exc}", show_time=False)
    log(f"  ├── 总计中奖记录: {total_wins} 条", show_time=False)
    
    if failed_accs:
        log(f"  ⚠ 存在异常或失败账号: {', '.join(map(str, failed_accs))}", show_time=False)
    else:
        log("  🎉 所有账号全部活动成功!", show_time=False)
        
    log("=" * 60, show_time=False)
    # push_summary()
    
    if enable_failure_exit and failed_accs:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
