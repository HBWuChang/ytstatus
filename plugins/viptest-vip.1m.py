#!/usr/bin/env python3
"""<xbar.title>VIPTest 一天会员</xbar.title>
<xbar.desc>通过 Keycloak + TOTP 为配置邮箱添加一天 premium</xbar.desc>
"""
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import ssl
import traceback
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, Request, BaseHandler

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'viptest-config.json'
STATE = ROOT / '.viptest-state.json'
BASE = 'https://www.viptest.co'
AUTH = 'https://auth.successfulmatch.com'
REASON = 'Test account / CS account.'
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36'
DEBUG = False
STAGE = '启动'


def log(message):
    if DEBUG:
        print(f'[{time.strftime("%H:%M:%S")}] {message}', file=sys.stderr, flush=True)


def stage(message):
    global STAGE
    STAGE = message
    log(message)


def safe_url(url):
    p = urlsplit(url)
    return f'{p.scheme}://{p.hostname}{p.path}'  # Never log query credentials/codes.


def describe_error(exc):
    if isinstance(exc, PluginError):
        return str(exc)
    if isinstance(exc, HTTPError):
        detail = getattr(exc, 'diagnostic', '')
        return f'HTTP {exc.code}，地址 {safe_url(exc.url)}' + (f'；{detail}' if detail else '')
    if isinstance(exc, URLError):
        return '连接失败：' + describe_error(exc.reason)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f'TLS 证书验证失败（verify_code={exc.verify_code}），请检查 Python CA 证书及代理证书信任'
    if isinstance(exc, ssl.SSLError):
        return f'TLS 错误（{exc.reason}）'
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return type(exc).__name__ + '：连接超时或中断'
    if isinstance(exc, json.JSONDecodeError):
        return f'JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列'
    if isinstance(exc, OSError):
        return f'{type(exc).__name__}（errno={exc.errno}）'
    # Arbitrary exception text can contain passwords, response bodies or tokens.
    return f'{type(exc).__name__}（--debug 可查看代码位置）'


def log_exception(exc):
    log(describe_error(exc))
    for frame in traceback.extract_tb(exc.__traceback__):
        log(f'  {Path(frame.filename).name}:{frame.lineno} in {frame.name}')


class PluginError(Exception):
    pass


def notify(title, message):
    """Match adb-shot's macOS notification behavior; failures are non-fatal."""
    script = [
        '-e', 'on run argv',
        '-e', 'display notification (item 1 of argv) with title (item 2 of argv) sound name "default"',
        '-e', 'end run',
    ]
    try:
        subprocess.run(['/usr/bin/osascript', *script, message[:400], title],
                       check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def pb_fields(data):
    def varint(pos):
        value = 0
        for shift in range(0, 70, 7):
            b = data[pos]
            pos += 1
            value |= (b & 127) << shift
            if b < 128:
                return value, pos
        raise ValueError('Invalid protobuf')
    pos = 0
    while pos < len(data):
        tag, pos = varint(pos)
        if tag & 7 == 0:
            value, pos = varint(pos)
        elif tag & 7 == 2:
            size, pos = varint(pos)
            value = data[pos:pos + size]
            if len(value) != size:
                raise ValueError('Truncated protobuf')
            pos += size
        else:
            raise ValueError('Unsupported protobuf field')
        yield tag >> 3, value


def otp_settings(value):
    if value.startswith('otpauth-migration://'):
        # Query '+' may be literal base64; do not turn it into a space.
        q = parse_qs(urlsplit(value).query.replace('+', '%2B'))
        raw = q['data'][0]
        fields = list(pb_fields(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))))
        entries = [dict(pb_fields(v)) for k, v in fields if k == 1]
        if len(entries) != 1:
            raise PluginError('TOTP 迁移链接必须仅包含一个账号')
        entry = entries[0]
        if entry.get(6) != 2 or entry.get(4) not in (1, 2, 3) or entry.get(5) not in (1, 2):
            raise PluginError('不支持的 OTP 类型、算法或位数')
        return entry[1], {1: 'sha1', 2: 'sha256', 3: 'sha512'}[entry[4]], {1: 6, 2: 8}[entry[5]], 30
    if value.startswith('otpauth://totp/'):
        q = parse_qs(urlsplit(value).query)
        secret = q['secret'][0]
        algorithm = q.get('algorithm', ['SHA1'])[0].lower()
        digits = int(q.get('digits', ['6'])[0])
        period = int(q.get('period', ['30'])[0])
    else:
        secret, algorithm, digits, period = value, 'sha1', 6, 30
    if algorithm not in ('sha1', 'sha256', 'sha512') or digits not in (6, 8) or period <= 0:
        raise PluginError('不支持的 TOTP 参数')
    secret = secret.replace(' ', '').upper()
    return base64.b32decode(secret + '=' * (-len(secret) % 8)), algorithm, digits, period


def totp(value, now=None):
    secret, algorithm, digits, period = otp_settings(value)
    digest = hmac.new(secret, struct.pack('>Q', int(time.time() if now is None else now) // period), algorithm).digest()
    offset = digest[-1] & 15
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff
    return str(number % (10 ** digits)).zfill(digits)


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms, self.links, self.emails = {}, [], {}
        self.current = None
        self.email_id = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag == 'form':
            self.current = {'action': a.get('action', ''), 'fields': {}}
            self.forms[a.get('id', '')] = self.current
        if tag == 'input' and self.current is not None and a.get('name') and 'disabled' not in a:
            kind = a.get('type', 'text')
            if kind not in ('submit', 'button') and (kind not in ('radio', 'checkbox') or 'checked' in a):
                self.current['fields'][a['name']] = a.get('value', '')
        if tag == 'span' and re.fullmatch(r'email_\d+', a.get('id', '')):
            self.email_id = a['id'][6:]
            self.emails[self.email_id] = ''

    def handle_data(self, data):
        if self.email_id is not None:
            self.emails[self.email_id] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None
        if tag == 'span':
            self.email_id = None

    def form(self, name):
        if name not in self.forms:
            raise PluginError('未找到预期表单：' + name + '；请检查登录权限或页面变更')
        return self.forms[name]


def checked_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.netloc not in ('www.viptest.co', 'auth.successfulmatch.com'):
        raise PluginError('服务跳转到了非预期地址，已停止')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        log(f'HTTP {code} 跳转 → {safe_url(newurl)}')
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GatewayAuth(BaseHandler):
    def __init__(self, config):
        username = config.get('basic_username', '')
        password = config.get('basic_password', '')
        self.value = None
        if username or password:
            if not username or not password or ':' in username:
                raise PluginError('请完整配置 basic_username / basic_password，用户名不能包含冒号')
            self.value = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode('ascii')

    def https_request(self, req):
        # Applied anew to each redirect, never copied to the Keycloak host.
        if self.value and urlsplit(req.full_url).netloc == 'www.viptest.co':
            req.add_unredirected_header('Authorization', self.value)
        return req


class Client:
    def __init__(self, config=None):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), SafeRedirect(), GatewayAuth(config or {}))

    def request(self, url, fields=None, ajax=False, referer=None):
        headers = {'User-Agent': USER_AGENT,
                   'Accept': 'application/json, text/javascript, */*; q=0.01' if ajax else 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                   'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
        if fields is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Origin'] = AUTH if urlsplit(url).netloc == urlsplit(AUTH).netloc else BASE
        if ajax:
            headers['X-Requested-With'] = 'XMLHttpRequest'
        if referer:
            headers['Referer'] = referer
        req = Request(checked_url(url), data=None if fields is None else urlencode(fields).encode(), headers=headers)
        started = time.monotonic()
        log(f'{req.get_method()} {safe_url(url)}')
        try:
            response = self.opener.open(req, timeout=30)
        except HTTPError as exc:
            # Classify known gateway responses, never print raw HTML or headers.
            body = exc.read(65536).lower()
            if exc.headers.get('WWW-Authenticate', '').lower().startswith('basic'):
                exc.diagnostic = '站点网关要求 HTTP Basic 认证，尚未进入 Keycloak'
            elif b'human verification' in body or b'captcha' in body:
                exc.diagnostic = '站点网关要求人机验证，尚未完成当前请求'
            log(f'HTTP {exc.code}，{time.monotonic() - started:.2f}s')
            exc.close()
            raise
        with response:
            body = response.read()
            log(f'HTTP {response.status}，{len(body)} bytes，{time.monotonic() - started:.2f}s，{safe_url(response.geturl())}')
            return response.geturl(), body.decode('utf-8')

    def login(self, config):
        stage('读取 Censor 登录入口')
        url, html = self.request(BASE + '/censor/login')
        links = [v for v in Page(html).links if v.startswith(AUTH + '/realms/ms/protocol/openid-connect/auth?')]
        if len(links) != 1:
            raise PluginError('找不到 Keycloak 登录入口')
        stage('读取 Keycloak 密码表单')
        url, html = self.request(links[0])
        form = Page(html).form('kc-form-login')
        fields = dict(form['fields'], username=config['username'], password=config['password'])
        stage('提交用户名和密码，等待 TOTP 表单')
        url, html = self.request(urljoin(url, form['action']), fields, referer=url)
        form = Page(html).form('kc-otp-login-form')
        period = otp_settings(config['totp'])[3]
        remaining = period - time.time() % period
        if remaining < 3:
            time.sleep(remaining + 0.2)
        fields = dict(form['fields'], otp=totp(config['totp']), login='Sign In')
        stage('提交 TOTP，建立 Censor 会话')
        url, html = self.request(urljoin(url, form['action']), fields, referer=url)
        if urlsplit(url).netloc != 'www.viptest.co' or '/censor/login' in url or 'kc-otp-login-form' in html:
            raise PluginError('Keycloak/TOTP 登录失败')

    def prepare(self, email):
        stage('按邮箱搜索并精确核对目标账号')
        url, html = self.request(BASE + '/censor/search/?' + urlencode({'type': 'usernameoremail', 'arg': email}))
        matches = [uid for uid, value in Page(html).emails.items() if value.strip().casefold() == email.casefold()]
        if len(matches) != 1:
            raise PluginError('邮箱没有唯一精确匹配的用户，已停止')
        uid = matches[0]
        log(f'精确匹配用户 ID：{uid}')
        stage('读取会员编辑表单和 CSRF')
        edit = BASE + '/censor/profile/edit?' + urlencode({'userId': uid, 'type': 'membership'})
        _, html = self.request(edit, ajax=True, referer=url)
        form = Page(html).form('edit_membership_form')
        fields = form['fields']
        if fields.get('Profile[user_id]') != uid or not fields.get('_csrf') or not fields.get('Profile[username]'):
            raise PluginError('会员表单的用户或 CSRF 校验失败')
        if urljoin(edit, form['action']) != edit:
            raise PluginError('会员表单地址不匹配')
        payload = membership_payload(fields)
        return edit, payload


def membership_payload(fields):
    # Use only captured fields, with dynamic identity/CSRF/current membership.
    payload = {key: fields[key] for key in ('_csrf', 'Profile[username]', 'Profile[user_id]', 'Profile[membership]', 'auto_renewing')}
    payload.update({'Profile[add_boost_coins]': '', 'Profile[grade]': '0',
                    'Profile[member_duration]': '1', 'Profile[member_type]': '1',
                    'Profile[make_guest]': '0', 'Profile[add_livestream_coins]': '',
                    'Profile[cancel_auto_renew]': '0', 'Profile[add_super_likes_count]': '0',
                    'note': REASON, 'addBoostsNote': '', 'addSuperLikesNote': ''})
    return payload


def read_config():
    config = json.loads(CONFIG.read_text())
    if CONFIG.stat().st_mode & 0o077:
        raise PluginError('请先执行 chmod 600 viptest-config.json')
    for key in ('username', 'password', 'target_email', 'totp'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise PluginError('请配置 ' + key)
    config['target_email'] = config['target_email'].strip()
    if not re.fullmatch(r'[^\s@|]+@[^\s@|]+\.[^\s@|]+', config['target_email']):
        raise PluginError('目标邮箱格式不正确')
    otp_settings(config['totp'])
    return config


def read_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(value):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def run(action):
    os.umask(0o077)
    stage('读取配置并检查操作锁')
    log(f'Python {sys.version.split()[0]}，解释器 {sys.executable}，{ssl.OPENSSL_VERSION}')
    with (ROOT / '.viptest.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PluginError('已有操作正在进行')
        config = read_config()
        email = config['target_email']
        state = read_state()
        if action == '--add' and state.get('uncertain'):
            raise PluginError('上次提交结果不明，请先到后台核查，再点击“已核查，解除提交锁”')
        if action == '--add' and time.time() - state.get('submitted_at', 0) < 60:
            raise PluginError('刚刚已提交，请等待 60 秒后再操作')
        client = Client(config)
        client.login(config)
        edit, payload = client.prepare(email)
        if action == '--check':
            message = f'连接检查通过：{email}，用户 {payload["Profile[user_id]"]}'
            save_state(dict(state, message=message))
            return message
        # Persist before POST: timeouts/crashes must never trigger an automatic retry.
        state = {'uncertain': True, 'submitted_at': time.time(), 'message': f'{email} 提交中或结果待核查'}
        save_state(state)
        stage('提交一天 premium（测试账号）')
        try:
            _, body = client.request(edit, payload, ajax=True, referer=edit)
            result = json.loads(body)
            if type(result.get('ret')) is not int or result['ret'] != 1:
                raise PluginError('服务未返回成功标志')
        except Exception as exc:
            log_exception(exc)
            raise PluginError('提交结果不明：' + describe_error(exc) + '；请到后台核查，勿直接重复添加') from None
        message = f'{email}：已添加 1 天 premium（测试账号）'
        save_state(dict(state, uncertain=False, message=message))
        log('成功：后台返回 ret=1，已保存结果')
        return message


def clean(value):
    return re.sub(r'[\r\n|]', ' ', str(value))


def menu(error=None):
    print('VIP +1')
    print('---')
    try:
        config = read_config()
        print('账号：' + clean(config['target_email']))
        action('添加一天会员（测试账号）', '--add')
        action('检查登录和目标账号', '--check')
    except Exception as exc:
        print(clean(str(exc) if isinstance(exc, PluginError) else '请先填写 viptest-config.json') + ' | color=orange')
    if error:
        print(clean(error) + ' | color=red')
    state = read_state()
    if state.get('message'):
        print(clean(state['message']))
    if state.get('uncertain'):
        action('已核查，解除提交锁', '--acknowledge')
    print('---')
    print('打开 Censor | href=https://www.viptest.co/censor/login')
    print(f'编辑配置 | shell=/usr/bin/open param1=-e param2={quote(str(CONFIG))} terminal=false')
    print('刷新菜单 | refresh=true')


def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def action(label, flag):
    print(f'{label} | shell={quote(sys.executable)} param1={quote(str(Path(__file__).resolve()))} param2={flag} terminal=false refresh=true')


if __name__ == '__main__':
    DEBUG = '--debug' in sys.argv[1:]
    sys.argv = [arg for arg in sys.argv if arg != '--debug']
    error = None
    try:
        if len(sys.argv) > 1:
            if sys.argv[1] in ('--add', '--check'):
                message = run(sys.argv[1])
                notify('VIPTest 会员添加成功' if sys.argv[1] == '--add' else 'VIPTest 连接检查通过', message)
            elif sys.argv[1] == '--acknowledge':
                with (ROOT / '.viptest.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    state = read_state()
                    save_state(dict(state, uncertain=False, message='已确认上次结果，可再次手动操作'))
            else:
                raise PluginError('未知操作')
    except Exception as exc:
        log_exception(exc)
        error = f'{STAGE}失败：{describe_error(exc)}'
        notify('VIPTest 操作失败', error)
        # Persist safe errors so SwiftBar's refresh does not hide them.
        with (ROOT / '.viptest.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                save_state(dict(read_state(), message=error))
            except BlockingIOError:
                pass
    menu(error)
    if error:
        sys.exit(1)
