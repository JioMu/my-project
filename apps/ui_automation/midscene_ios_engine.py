"""
Midscene iOS UI 自动化执行引擎（WebDriverAgent）。

背景（2026-09-10 实测）：
- Python 包 ``midscene`` 的最新版就是 **0.1.5**，只有 ``agent_android`` / ``agent_web``
  两个子模块，**没有 iOS**。PyPI 上也没有更高版本，所以 iOS 不能走 Python SDK。
- 但官方 JS SDK ``@midscene/ios``（npm 上 1.12.6）是支持 iOS 的，底层同样走
  WebDriverAgent。容器里已经有 Node 22.12（midscene 0.1.5 自带的 node_runtime），
  且 npm registry 可访问。
- 因此本引擎的做法：**自己起一个 @midscene/ios 的 Node JSON-RPC 服务**
  （见同目录 ``midscene_ios_node/service.js``），Python 侧通过 HTTP JSON-RPC 调用它。
  不改动第三方 midscene 包，服务代码随本仓库部署。

模型配置统一取自 ``AIModelConfig(role='midscene_vision', is_active=True)``
（apps.requirement_analysis），与 Web / Android 引擎共用一份配置；
通过环境变量 ``MIDSCENE_MODEL_*`` 传给 Node 服务。

连接参数：``wda_host`` / ``wda_port``（默认 8100），对应 WDA 的 HTTP 端点，
例如 iPhone 插在 192.168.8.71 上并已做端口转发时填 host=192.168.8.71, port=8100。
"""
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

from .midscene_web_engine import (
    MODEL_ROLE,
    load_midscene_model_config,
    _resolve_model_family,
    _to_data_url,
)
# 随机弹窗的自动处理，与 Android 引擎共用 popup_handler。
# ⚠️ 开关与策略统一在 popup_handler 定义（单一事实源），这里只引用，
#    否则改一处不生效（Android/iOS 行为会不一致）。
from .popup_handler import (  # noqa: E402
    dismiss_popups, PopupAdapter, POPUP_AUTO_DISMISS,
)

logger = logging.getLogger(__name__)
POPUP_RETRY_LIMIT = int(os.environ.get('POPUP_RETRY_LIMIT', '2') or 2)

#: 断言的弹窗重试次数比操作类多一些（见 execute_steps 中的说明）。
ASSERT_POPUP_RETRY_LIMIT = int(
    os.environ.get('ASSERT_POPUP_RETRY_LIMIT', '3') or 3)

#: 逻辑点屏幕尺寸兜底（iPhone 15 Pro 393x852）。
#: 仅用于「采样坐标是否越界」的护栏，取不到真实尺寸时也不会误杀。
SCREEN_SIZE_FALLBACK = (393, 852)

#: Node 服务目录（容器内路径，/root/.midscene 已挂卷持久化）
SERVICE_DIR = os.environ.get(
    'MIDSCENE_IOS_SERVICE_DIR', '/root/.midscene/ios/node_service')

#: 服务端口信息文件（由 Node 服务写入）
PORT_FILE = '/root/.midscene/ios/service.json'

SERVICE_SCRIPT = 'service.js'
NPM_INSTALL_TIMEOUT = 900  # 首次 npm install 最多等 15 分钟
SERVICE_START_TIMEOUT = 120


class MidsceneIOSServiceError(RuntimeError):
    """Node 服务不可用（未安装 / 启动失败 / 端口不通）。"""


def _node_bin() -> str:
    """定位容器内的 node 可执行文件。

    midscene 0.1.5 会把 Node 22 装到 /root/.midscene/node_runtime，系统 PATH 里没有。
    """
    candidates = [
        os.environ.get('MIDSCENE_NODE_BIN', ''),
        '/root/.midscene/node_runtime/bin/node',
        '/usr/local/bin/node',
        '/usr/bin/node',
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return ''


def _npm_cli() -> str:
    """定位 npm 的 cli 入口（node_runtime 里的 npm 脚本没有执行权限，需用 node 调）。"""
    for p in (
        os.environ.get('MIDSCENE_NPM_CLI', ''),
        '/root/.midscene/node_runtime/npm/bin/npm-cli.js',
    ):
        if p and os.path.exists(p):
            return p
    return ''


class MidsceneIOSEngine:
    """iOS 真机 AI 自动化引擎（自然语言 aiAct / aiTap / aiInput / aiQuery / aiAssert）。

    接口对齐 :class:`~apps.ui_automation.midscene_web_engine.MidsceneWebEngine`，
    便于 views 层与测试执行器按同一套契约调用。
    """

    SUPPORTED_ACTIONS = {
        'aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiAssert', 'aiWaitFor',
        'launch', 'wait', 'screenshot',
        # 系统级动作（不走视觉模型）。
        # ⚠️ 曾经漏了这两个：前端把 back/home 当作「无提示词动作」提供给用户，
        #    Android 引擎支持而 iOS 不支持 → 用户在 iOS 用例里加"返回"步骤，
        #    执行到那步直接报「不支持的 action: back」整条用例失败。
        #    iOS 的等价实现见 service.js：back = 左边缘右滑手势，home = WDA home。
        'back', 'home',
        # ⚠️ 命名兼容：前端的动作下拉里断言写的是 **`assert`**（Android 引擎用
        #    `assert`），而本引擎内部与 RPC 方法叫 `aiAssert`。两边不一致导致
        #    iOS 用例加「断言」步骤同样报「不支持的 action: assert」。
        #    这里把 `assert` 也纳入白名单，并在分派时映射到 aiAssert。
        'assert',
    }

    #: 前端动作名 → 本引擎/RPC 内部方法名的映射（目前只有断言这一处不一致）
    ACTION_ALIASES = {'assert': 'aiAssert'}

    #: 回放模式下能用纯坐标完成、不需要模型的动作。
    #: aiAssert / aiQuery / aiWaitFor 是有意排除的 —— 断言、取值、条件等待
    #: 本质上是语义判断，坐标固化不了，必须继续调模型（刻意的取舍，不是遗漏）。
    REPLAY_ACTIONS = {'aiTap', 'aiAct', 'aiInput'}

    #: 条件等待默认超时（毫秒）。aiWaitFor 要等到条件成立才返回，必须有上限，
    #: 否则页面一直不出现目标元素就会长时间卡住（可用步骤的 timeout_ms 覆盖）。
    DEFAULT_WAIT_FOR_TIMEOUT_MS = 15000

    def __init__(self, wda_host='', wda_port=8100, model_config=None,
                 ai_action_context='', default_bundle_id='',
                 auto_dismiss_popup=None, replay_mode=False):
        self.wda_host = (wda_host or '').strip()
        self.wda_port = int(wda_port or 8100)
        self.model_config = model_config or load_midscene_model_config()
        self.ai_action_context = ai_action_context or (
            'If any location, permission, user agreement popup appears, click agree. '
            'If a login page appears, close it.'
        )
        # 用例绑定的被测应用 bundle id。步骤里写 "launch: 万年历" 这种人类可读
        # 名字时，WDA 会直接 400（它只认 bundle id 或 URL），所以需要有个兜底。
        self.default_bundle_id = (default_bundle_id or '').strip()
        # 弹窗自动处理。None = 用全局默认（环境变量），True/False = 用例配置优先。
        self.auto_dismiss_popup = (
            POPUP_AUTO_DISMISS if auto_dismiss_popup is None else bool(auto_dismiss_popup)
        )
        # 纯坐标回放：用例已固化过，每个 aiTap/aiAct/aiInput 都用上次采到的
        # 坐标直接点，不调模型。任何一步失败由调用方降级回 AI 模式重跑。
        self.replay_mode = bool(replay_mode)
        self.logs = []
        self._session_id = None
        self._port = None
        # 逻辑点屏幕尺寸：采样坐标越界时用来丢弃明显错误的固化数据。
        # 默认按 iPhone 15 Pro（393x852）兜底，start() 拿到真实尺寸后会覆盖。
        self.SCREEN_W, self.SCREEN_H = SCREEN_SIZE_FALLBACK
        self._screen_size_known = False

    # ---------------- 基础设施：Node 服务 ----------------
    def _log(self, fmt, *args):
        msg = fmt % args if args else fmt
        ts = datetime.now().strftime('%H:%M:%S')
        self.logs.append('[%s] %s' % (ts, msg))
        logger.info('[MidsceneIOS] %s', msg)

    def _service_ready(self, port):
        try:
            req = urllib.request.Request(
                'http://127.0.0.1:%s/health' % port, method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _read_port_file(self):
        try:
            with open(PORT_FILE, 'r', encoding='utf-8') as f:
                info = json.load(f)
            port = int(info.get('port') or 0)
            if port and self._service_ready(port):
                return port
        except Exception:
            return None
        return None

    def _ensure_service(self) -> int:
        """确保 @midscene/ios 的 Node 服务在跑，返回端口。"""
        port = self._read_port_file()
        if port:
            self._port = port
            return port

        node = _node_bin()
        if not node:
            raise MidsceneIOSServiceError(
                '未找到 Node 运行时（期望 /root/.midscene/node_runtime/bin/node）。'
                'iOS 引擎依赖 Node 侧的 @midscene/ios，请先跑一次 Midscene Web 让容器初始化 Node。'
            )

        os.makedirs(SERVICE_DIR, exist_ok=True)
        script = os.path.join(SERVICE_DIR, SERVICE_SCRIPT)
        if not os.path.exists(script):
            raise MidsceneIOSServiceError(
                'Node 服务脚本缺失: %s。请把 apps/ui_automation/midscene_ios_node/service.js '
                '复制到容器内该目录。' % script)

        if not os.path.isdir(os.path.join(SERVICE_DIR, 'node_modules')):
            self._log('首次使用，安装 @midscene/ios（约 1-3 分钟）...')
            npm_cli = _npm_cli()
            if not npm_cli:
                raise MidsceneIOSServiceError(
                    '未找到 npm-cli.js，无法安装 @midscene/ios。')
            try:
                proc = subprocess.run(
                    [node, npm_cli, 'install', '--no-audit', '--no-fund'],
                    cwd=SERVICE_DIR, capture_output=True, timeout=NPM_INSTALL_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise MidsceneIOSServiceError('npm install @midscene/ios 超时')
            if proc.returncode != 0:
                raise MidsceneIOSServiceError(
                    'npm install @midscene/ios 失败: %s'
                    % (proc.stderr or b'').decode('utf-8', 'ignore')[-500:])
            self._log('@midscene/ios 安装完成')

        env = os.environ.copy()
        cfg = self.model_config or {}
        if not cfg.get('api_key') or not cfg.get('model_name'):
            raise MidsceneIOSServiceError(
                '未配置 Midscene 视觉模型：请在「AI 模型配置」中添加 role=%s 且已启用的多模态模型。'
                % MODEL_ROLE)
        base_url = cfg.get('base_url') or ''
        env['MIDSCENE_MODEL_BASE_URL'] = base_url
        env['MIDSCENE_MODEL_API_KEY'] = cfg.get('api_key') or ''
        env['MIDSCENE_MODEL_NAME'] = cfg.get('model_name') or ''
        env['MIDSCENE_MODEL_FAMILY'] = cfg.get('model_family') or _resolve_model_family(cfg) or ''
        # 让 Node 服务把每次 LLM 请求都带上真实上游地址，后端据此转发并统计
        # token（见 views.llm_proxy）。统计是旁路，拿不到也不影响调用。
        env['MIDSCENE_LLM_UPSTREAM'] = base_url
        # 代理地址由 Django 侧注入；为空表示直连，改用估算统计。
        proxy = os.environ.get('MIDSCENE_LLM_PROXY_URL', '').strip()
        if proxy:
            env['MIDSCENE_MODEL_BASE_URL'] = proxy
            self._log('LLM 走用量统计代理: %s -> %s', proxy, base_url)

        port = self._pick_free_port()
        log_path = '/tmp/midscene_ios_service.log'
        logf = open(log_path, 'ab')
        subprocess.Popen(
            [node, script, '--port=%d' % port],
            cwd=SERVICE_DIR, env=env, stdout=logf, stderr=logf, start_new_session=True)

        deadline = time.time() + SERVICE_START_TIMEOUT
        while time.time() < deadline:
            if self._service_ready(port):
                self._port = port
                self._log('iOS Node 服务已就绪 (port=%s)', port)
                return port
            time.sleep(1)
        raise MidsceneIOSServiceError(
            'iOS Node 服务启动超时，详见容器日志 %s' % log_path)

    @staticmethod
    def _pick_free_port():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _rpc(self, method, params=None, timeout=600):
        if not self._port:
            raise MidsceneIOSServiceError('Node 服务未启动')
        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': int(time.time() * 1000),
            'method': method,
            'params': params or {},
        }).encode('utf-8')
        req = urllib.request.Request(
            'http://127.0.0.1:%s/rpc' % self._port,
            data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raise MidsceneIOSServiceError('RPC %s 失败: HTTP %s' % (method, e.code))
        except Exception as e:
            raise MidsceneIOSServiceError('RPC %s 失败: %s' % (method, e))
        if data.get('error'):
            raise RuntimeError(data['error'].get('message') or str(data['error']))
        return data.get('result') or {}

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._session_id:
            return self._session_id
        self._ensure_service()
        if not self.wda_host:
            raise ValueError('缺少 wda_host：请在设备配置中填写 WDA 地址（如 192.168.8.71）')
        self._log('连接 WDA: %s:%s', self.wda_host, self.wda_port)
        res = self._rpc('createSession', {
            'wdaHost': self.wda_host,
            'wdaPort': self.wda_port,
            'aiActionContext': self.ai_action_context,
        })
        self._session_id = res.get('sessionId')
        self._log('iOS 会话已建立: %s', self._session_id)
        self._load_screen_size()
        return self._session_id

    def _load_screen_size(self):
        """记录本机逻辑点屏幕尺寸（仅用于采样坐标的越界护栏，失败不阻断）。"""
        try:
            res = self._rpc('screenSize', {'sessionId': self._session_id}, timeout=30)
            w, h = int(res.get('width') or 0), int(res.get('height') or 0)
            if w > 0 and h > 0:
                self.SCREEN_W, self.SCREEN_H = w, h
                self._screen_size_known = True
                self._log('屏幕逻辑尺寸: %dx%d（坐标采样越界护栏）', w, h)
        except Exception as exc:  # noqa: BLE001
            self._log('取屏幕尺寸失败，按 %dx%d 兜底: %s',
                      self.SCREEN_W, self.SCREEN_H, str(exc)[:120])

    def close(self):
        if not self._session_id:
            return
        try:
            self._rpc('destroySession', {'sessionId': self._session_id}, timeout=60)
            self._log('已断开 iOS 会话')
        except Exception as e:  # noqa: BLE001
            logger.warning('[MidsceneIOS] 断开会话失败: %s', e)
        finally:
            self._session_id = None

    _cleanup = close

    @staticmethod
    def _looks_like_bundle_id(text: str) -> bool:
        """粗略判断是不是 bundle id（com.xxx.yyy 这种反向域名）。"""
        t = (text or '').strip()
        if not t or ' ' in t:
            return False
        return bool(re.match(r'^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$', t))

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        t = (text or '').strip().lower()
        return t.startswith('http://') or t.startswith('https://') or '://' in t

    def resolve_launch_target(self, raw_target: str) -> str:
        """把步骤里写的启动目标规范化成 WDA 能接受的 uri。

        WDA 的 /wda/apps/launch 只认真实 bundle id 或 URL。用例里常写
        「launch: 万年历」这种人类可读名字，直接透传会得到
        HTTP 400 (Failed to launch 万年历) —— 这是历史上真实踩过的坑。

        规则：
          1. 已经是 bundle id  -> 原样使用
          2. 是 URL            -> 原样使用
          3. 其他（中文名等）  -> 回退到用例绑定的被测应用 bundle id
        """
        t = (raw_target or '').strip()
        if t and (self._looks_like_bundle_id(t) or self._looks_like_url(t)):
            return t
        if self.default_bundle_id:
            if t:
                self._log('launch 目标「%s」不是 bundle id/URL，改用被测应用: %s',
                          t, self.default_bundle_id)
            return self.default_bundle_id
        raise ValueError(
            '无法启动「%s」：它既不是 bundle id / URL，用例也没有绑定被测应用'
            '（applicationId）。请在用例的「应用配置」里选好被测应用，'
            '或把该步骤的 prompt 改成真实 bundle id。' % (t or '(空)')
        )

    def launch_app(self, bundle_id=''):
        """启动 App（bundle id）或打开 URL。"""
        target = self.resolve_launch_target(bundle_id)
        if not target:
            return
        self._rpc('launch', {'sessionId': self._session_id, 'uri': target}, timeout=180)
        self._log('已启动: %s', target)

    def reset_to_home(self, bundle_id=''):
        """把 App 复位到一致起点（探索与回放都用，保证"采样起点 == 回放起点"）。

        目的（2026-09-22 生产实测，用例 73）：
          回放按**固化时采到的绝对坐标**点击，隐含假设
          「执行起点页面 == 固化采样时的起点页面」。
          若探索时从"任意残留状态"采样、回放时从另一起点开始，
          两边页面不同 → 坐标整体偏掉 → 回放失败 → 降级烧 token
          → 用户以为"固化从没生效、永远省不了 token"。
          所以**探索和回放都必须先复位到同一起点**。

        ⚠️⚠️ **复位只做「重新拉起 App」，绝不主动 `home` 回桌面**
        （2026-09-22 踩坑后定型）：
          最初实现是 `home` + `launch`，以为能拿到"最干净的首启状态"。
          但实测这个 App（万年历）**冷启动默认落在一个叫「日记本」的页面，
          该页既没有底部主 tab 栏、也没有任何返回按钮**（`back` 手势连点
          两次元素树完全不变）→ 它成了一个**死路页**：
          首步固化坐标 (40,794)（原本是底部 tab）在此页会落到「前一天」按钮上，
          后续步骤必然连锁失败。**等于把 App 推进了比原残留状态更糟的地方。**

          改为只用 `launch`：App 已在前台时它通常只是"唤到前台"、保留原导航栈，
          App 不在前台则冷启动。两种情况下**都不由我们决定落在哪一页**，
          起点一致性交给用户用例自己保证（用例应从它期望的页面开始）。

          注意：**不能改用「杀进程」来强制冷启动** —— 杀进程恰恰会落到
          那个「日记本」死路页，与上面实测的坏结果等价。

        失败不抛异常：复位失败只是"起点可能不一致"，
        不能因为复位本身报错把用例直接搞挂。
        """
        target = ''
        try:
            target = self.resolve_launch_target(bundle_id)
        except Exception as exc:  # noqa: BLE001
            self._log('[复位] 解析被测应用失败（跳过复位）: %s', str(exc)[:160])
            return
        if not target:
            self._log('[复位] 未配置被测应用，跳过复位（起点可能不一致）')
            return
        try:
            self._rpc('launch', {'sessionId': self._session_id, 'uri': target},
                      timeout=180)
            # 等界面稳定，否则紧接着的坐标点击会落在启动过渡画面上
            time.sleep(2.0)
            self._log('[复位] 已拉起 %s（不重建导航栈，起点由用例自身保证）', target)
        except Exception as exc:  # noqa: BLE001
            self._log('[复位] 复位失败（不影响执行，起点可能不一致）: %s',
                      str(exc)[:160])

    def screenshot(self) -> str:
        res = self._rpc('screenshot', {'sessionId': self._session_id}, timeout=120)
        return _to_data_url(res.get('base64') or '')

    # ---------------- 执行 ----------------
    def execute_steps(self, steps):
        """串行执行一组步骤（与 Web 引擎同契约）。

        空步骤（既没有 action 也没有 prompt）直接**判失败并说明原因**：
        以前会给 action 兜底成 aiAct、prompt 兜底成空串，于是拿着空提示词去问
        视觉模型，模型随便点一下就返回 success —— 用例明明什么都没配，执行却显示
        「通过」，用户看到的就是"步骤没按我写的执行"。
        """
        steps = [s for s in (steps or []) if isinstance(s, dict)]
        # 空步骤不启动会话，省一次真机连接
        invalid = [i for i, s in enumerate(steps) if not self._step_has_content(s)]
        if invalid:
            bad = invalid[0]
            msg = ('第 %d 个步骤是空的：既没有 action 也没有 prompt。'
                   '请到「用例编辑」里补全该步骤（或删除它）' % (bad + 1))
            self._log('%s', msg)
            return {
                'status': 'failed',
                'steps': [{'index': bad, 'name': 'Step %d' % (bad + 1),
                           'action': '', 'status': 'failed', 'error': msg,
                           'extract': {}, 'assert': {}}],
                'error': msg,
            }

        started_here = self._session_id is None
        if started_here:
            self.start()
        result = {'status': 'success', 'steps': [], 'error': None}
        try:
            for idx, step in enumerate(steps):
                sr = self._execute_step(idx, step)

                # 随机弹窗（权限请求 / 隐私协议 / 开屏广告 ……）随时可能冒出来，
                # 挡住目标元素就会让这一步失败。这里做「关弹窗 + 重试」，
                # 正常路径（没弹窗）零额外开销。Android 侧同一套逻辑。
                #
                # ⚠️ 断言步骤的重试上限单独放宽（2026-09-22 生产实测）：
                #   弹窗检测与断言**各自调一次视觉模型**，两者结论可能矛盾 ——
                #   检测器认为"没有弹窗"（大广告页它常判成"普通页面"），
                #   断言却明确说"页面上全是广告"。此时若沿用 2 次上限，
                #   第 2 次照样报"未发现弹窗"就放弃，用户看到的就是
                #   "你说会自动关弹窗，结果没关"。断言多试几次成本很低
                #   （一次 aiQuery），换来的是广告页能自愈。
                limit = (ASSERT_POPUP_RETRY_LIMIT
                         if step.get('action') == 'assert' else POPUP_RETRY_LIMIT)
                try_count = 0
                while (sr['status'] == 'failed'
                       and self.auto_dismiss_popup
                       and try_count < limit):
                    try_count += 1
                    self._log('[步骤 %d] 失败，检测是否为弹窗干扰（第 %d/%d 次重试）...',
                              idx + 1, try_count, limit)
                    if dismiss_popups(self._popup_adapter()) == 0:
                        self._log('[步骤 %d] 未发现弹窗，不再重试', idx + 1)
                        break
                    sr = self._execute_step(idx, step)
                    if sr['status'] != 'failed':
                        self._log('[步骤 %d] 关闭弹窗后重试成功', idx + 1)
                        break

                result['steps'].append(sr)
                if sr['status'] == 'failed':
                    result['status'] = 'failed'
                    result['error'] = sr.get('error', '步骤执行失败')
                    break
        except Exception as e:  # noqa: BLE001
            result['status'] = 'failed'
            result['error'] = str(e)
            self._log('执行异常: %s', e)
        finally:
            if started_here:
                self.close()
        return result

    @staticmethod
    def _step_has_content(step):
        """步骤是否可执行：引用步骤看 ref，内联步骤看 action/prompt。"""
        if not isinstance(step, dict):
            return False
        if str(step.get('ref') or '').strip():
            return True
        if str(step.get('action') or '').strip():
            return True
        return bool(str(step.get('prompt') or '').strip())

    def _execute_by_text(self, step, action_impl, sr, prompt, value):
        """精确定位路径：按文本查 WDA 元素树拿坐标，再点击/输入。

        与视觉定位（aiTap 让模型看图猜）的区别是**确定性**：
          - 命中失败会**明确报错并列出页面上实际有的文案**，不会静默点错；
          - 命中成功使用元素自身的精确坐标，不受模型视觉偏差影响。

        文本来源：优先 selector_hint（定位字段），没填才用 prompt。
        **传的是完整描述**（如「底部导航栏从左数第1个「万年历」图标」），
        由 Node 侧 extractTextCandidates() 提取候选文案逐个试 ——
        因为元素树里按钮的 name 往往只是「万年历」，整句去匹配必然找不到。

        region 可选（bottom/top/lowest/highest），用于页面存在同名元素时
        （如顶部按钮与底部 tab 同名）消歧。
        """
        text = (step.get('selector_hint') or '').strip() or (prompt or '').strip()
        if not text:
            raise ValueError(
                '没有可用于定位的文本：请在「定位」或「提示词」里'
                '填写元素上的文字（如：万年历）'
            )
        params = {
            'sessionId': self._session_id,
            'text': text,
            'index': int(step.get('locate_index') or 0),
            'region': (step.get('locate_region') or '').strip(),
        }

        if action_impl == 'aiTap':
            res = self._rpc('tapByText', params, timeout=120)
            center = res.get('center') or []
            self._log('[步骤] 精确定位命中「%s」→ 点击 (%s)',
                      text, ','.join(str(c) for c in center))
            sr['location'] = {
                'center': center, 'rect': res.get('rect'),
                'dpr': 1, 'target': text,
                'matchedBy': res.get('matchedBy') or 'text',
            }
            return

        # aiInput：先按文本把焦点定位到输入框并点一下，再输入
        res = self._rpc('findByText', params, timeout=120)
        center = res.get('center') or []
        self._rpc('tapPoint',
                  {'sessionId': self._session_id,
                   'x': center[0], 'y': center[1]}, timeout=60)
        self._rpc('inputText',
                  {'sessionId': self._session_id, 'text': value or ''}, timeout=120)
        self._log('[步骤] 精确定位命中输入框「%s」→ 点击 (%s) 并输入',
                  text, ','.join(str(c) for c in center))
        sr['location'] = {
            'center': center, 'rect': res.get('rect'),
            'dpr': 1, 'target': text,
            'matchedBy': res.get('matchedBy') or 'text',
        }

    def _capture_location(self, action_impl, sr, prompt, step, rpc_params):
        """视觉模型操作**之前**采一次坐标，写进 sr['location']（供固化用）。

        背景（2026-09-22）：
          用户的核心诉求是"首次探索跑通后自动转成可复用脚本"（零模型回放）。
          但 `_freeze_case_steps` 取坐标的来源是 `steps_completed[i].location`，
          而**只有元素树路径会写它**；视觉模型路径的 `aiTap` 返回 void，
          于是 17 步里 12 步 location=null → 一条缺坐标就整体不固化 →
          每次重跑都重新烧 29k tokens，固化形同虚设。

        为什么用 aiLocate 而不是让 aiTap 返回坐标：
          `agent.aiTap()` 是 SDK 的 void 方法，内部定位结果不对外暴露；
          `agent.aiLocate()` 是同一套视觉定位的"纯查询"入口，返回 {center, rect}。

        代价与边界（都是刻意的）：
          - 每步多一次视觉定位请求 —— **只发生在首次探索**，
            固化后走 replay 分支不会进来，所以常态开销为 0。
          - 失败**绝不抛异常**：采不到坐标只是"这次固化不了"，
            不能因此把本来能跑通的用例搞失败。
        """
        text = (step.get('selector_hint') or '').strip() or (prompt or '').strip()
        if not text:
            return
        try:
            res = self._rpc('aiLocate',
                            {'sessionId': self._session_id, 'prompt': text},
                            timeout=180)
        except Exception as exc:  # noqa: BLE001
            self._log('[步骤] 坐标采样失败（不影响执行）: %s', str(exc)[:160])
            return

        if not (isinstance(res, dict) and res.get('ok') and res.get('center')):
            self._log('[步骤] 坐标采样未命中（不影响执行）: %s',
                      str((res or {}).get('error') or '')[:160])
            return

        center = res.get('center') or []
        # 采到的坐标必须在屏幕内，否则固化下来回放必然点空。
        # 2026-09-22 实测：不校验时会把「万年历」采成 (590,1280)，超出 393x852。
        try:
            cx, cy = int(center[0]), int(center[1])
        except (TypeError, ValueError, IndexError):
            self._log('[步骤] 坐标采样结果非法（不影响执行）: %s', center)
            return
        if cx < 0 or cy < 0 or cx > self.SCREEN_W or cy > self.SCREEN_H:
            self._log('[步骤] 采样坐标 (%d,%d) 超出屏幕 %dx%d，丢弃（不固化该步）',
                      cx, cy, self.SCREEN_W, self.SCREEN_H)
            return

        sr['location'] = {
            'center': [cx, cy],
            'rect': res.get('rect'),
            'dpr': res.get('dpr') or 1,
            'target': text,
            'matchedBy': 'aiLocate',
        }
        self._log('[步骤] 已采样坐标 %d,%d（用「%s」，用于固化为脚本）',
                  cx, cy, res.get('usedText') or text)

    def _execute_step(self, idx, step):
        action = step.get('action', 'aiAct')
        # 前端动作名 → 内部方法名（assert → aiAssert）。保留原始名用于日志与
        # 报告展示，避免用户看到自己没写过的动作名。
        action_impl = self.ACTION_ALIASES.get(action, action)
        prompt = step.get('prompt', '') or ''
        value = step.get('value', '') or ''
        name = step.get('name', 'Step %d' % (idx + 1))
        wait_ms = step.get('wait_ms', 0)
        sr = {'index': idx, 'name': name, 'action': action,
              'status': 'success', 'extract': {}, 'assert': {}}
        self._log('[步骤 %d] %s: %s', idx + 1, action, prompt or name)
        try:
            # 需要视觉模型的动作必须有 prompt，否则是在"盲操作"，
            # 必须报错而不是让模型瞎猜后返回成功。
            if action_impl in ('aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiAssert', 'aiWaitFor') \
                    and not str(prompt).strip():
                raise ValueError(
                    '步骤 %d（%s）没有填写提示词 prompt，无法执行' % (idx + 1, action)
                )
            if action == 'launch':
                self.launch_app(prompt or value)
            elif action == 'back':
                # iOS 无全局返回键 → Node 侧用「左边缘右滑」系统返回手势实现
                self._rpc('back', {'sessionId': self._session_id}, timeout=60)
                self._log('[步骤 %d] 已执行返回（左边缘右滑手势）', idx + 1)
            elif action == 'home':
                self._rpc('home', {'sessionId': self._session_id}, timeout=60)
                self._log('[步骤 %d] 已回到桌面', idx + 1)
            elif action == 'screenshot':
                sr['screenshot'] = self.screenshot()
            elif action == 'wait':
                time.sleep(wait_ms / 1000.0 if wait_ms else 1)
            elif action_impl in self.SUPPORTED_ACTIONS:
                # ---- 纯坐标回放 ----
                # steps_frozen_at 非空表示这条用例已经固化过，每步都带着
                # 上次成功执行时采到的坐标。用坐标直接点，完全不经过视觉
                # 模型（也不用跑定位采样），这是"重复跑零 token"的关键路径。
                if self.replay_mode and self._step_has_coords(step) \
                        and action_impl in self.REPLAY_ACTIONS:
                    self._replay_step(action_impl, step, prompt, value)
                    return sr

                # ⭐⭐ 回放模式下本步没有可用坐标 → **必须报错，绝不能回退视觉模型**
                #
                # 2026-09-22 生产实测（用例 id=73 / 记录 #53）：
                #   回放模式第 1 步正常点了 (40,794)，第 2 步 assert 失败 → 触发
                #   "回放失败降级 AI 重跑" → 用户看到报告是「AI 探索」，
                #   误以为**回放从来没生效过**，反复重跑都"省不了 token"。
                #
                # 更隐蔽的坑：回放模式下若某定位步骤**恰好没坐标**，旧逻辑会继续往下走，
                # 落进"元素树 → 视觉模型"分支 —— 于是"纯回放"的用例里
                # **悄悄又烧起了模型**，且没有任何日志提示这次不是零 token。
                # 现在明确判失败，由调用方走"降级重跑"这条唯一出口，
                # 保证"走了回放"和"烧了 token"不会同时发生、不会互相掩盖。
                if self.replay_mode and action_impl in self.REPLAY_ACTIONS:
                    raise ValueError(
                        '回放模式但第 %d 步（%s）没有可用坐标：'
                        '该步未采到坐标或被清除固化。请重新执行一次 AI 探索以补齐坐标。'
                        % (idx + 1, action)
                    )

                # ---- 精确定位（元素树）----
                # 依据：WDA /source 的 accessibility 树里有每个元素的精确坐标。
                # 在底部这种 79px 一个的密集图标条上，让视觉模型"看图猜坐标"
                # 会猜偏（实测「万年历」被点到相邻的「黄历」，中心仅差 78px）。
                #
                # ⚠️⚠️ 只要步骤是 aiTap/aiInput，就**一律先试元素树**，命中就用精确
                #    坐标，没命中才回退视觉模型。
                #    这里**故意不看 locate_by_text 的值**（2026-09-21 第二次踩坑）：
                #    前端 checkbox 未勾选时保存的就是 `false`，与"新步骤默认值"无法区分，
                #    于是 `is not False` 判断把用户的步骤**静默降级成视觉定位** ——
                #    表现是"有时准有时不准"，极难排查。
                #    ⇒ 语义简化为：元素树能命中就命中，命中不了自然会回退，不需要开关。
                #      locate_by_text 字段保留仅用于**报告标记**，不参与分支。
                if action_impl in ('aiTap', 'aiInput'):
                    try:
                        self._execute_by_text(step, action_impl, sr, prompt, value)
                        return sr
                    except Exception as exc:  # noqa: BLE001
                        # 元素树没命中/元素不可见等 → 回退视觉模型，保证旧用例仍能跑。
                        self._log('[步骤 %d] 元素定位未命中，回退 AI 视觉定位: %s',
                                  idx + 1, str(exc)[:200])



                rpc_params = {
                    'sessionId': self._session_id,
                    'prompt': prompt,
                    'value': value,
                    'locate': step.get('selector_hint') or '',
                }
                # 条件等待：SDK 的 aiWaitFor 要等到条件成立才返回，必须带超时，
                # 否则页面一直不出现目标元素就会长时间挂住。Android/Web 侧同语义。
                if action_impl == 'aiWaitFor':
                    rpc_params['timeoutMs'] = self._wait_for_timeout(step)
                    self._log('[步骤 %d] 等待元素「%s」出现（超时 %dms）',
                              idx + 1, prompt, rpc_params['timeoutMs'])

                # ⭐⭐ 采坐标：视觉模型路径要"固化为可复用脚本"就必须留下坐标。
                #    `aiTap` 返回 void，拿不到"点到哪了"，所以这里额外调一次
                #    `aiLocate`（纯定位、不点击）。**只在非回放路径发生**，
                #    固化后走 replay 分支根本不会到这里，所以不会增加常态开销。
                #    ❗ 必须在**操作之前**采：点击可能切页，之后坐标就没意义了。
                if not self.replay_mode and action_impl in ('aiTap', 'aiAct', 'aiInput'):
                    self._capture_location(action_impl, sr, prompt, step, rpc_params)

                # 用内部方法名调 RPC（Node 服务只认 aiAssert，不认前端的 assert）
                res = self._rpc(action_impl, rpc_params)
                # 真实 usage 由 LLM 代理记录；代理没生效时按截图尺寸估算，
                # 保证"这次执行花了多少"永远有数字可看。
                try:
                    from .llm_usage import record_step_estimate
                    record_step_estimate(prompt=prompt)
                except Exception:  # noqa: BLE001
                    pass
                if action_impl == 'aiQuery':
                    sr['extract']['_query'] = res.get('result')
                elif action_impl in ('aiAssert',):
                    sr['assert'] = {'pass': True, 'reason': ''}

                # Node 侧若在操作结果里直接带了坐标（tapByText 之类），优先用它
                loc = res.get('location') or {}
                if loc.get('ok') and loc.get('center'):
                    sr['location'] = {
                        'center': loc.get('center'),
                        'rect': loc.get('rect'),
                        'dpr': loc.get('dpr'),
                        'target': loc.get('prompt') or prompt,
                    }
                elif loc.get('error'):
                    self._log('[步骤 %d] 定位采样未成功（不影响执行）: %s',
                              idx + 1, loc.get('error'))
            else:
                raise ValueError('不支持的 action: %s' % action)

            if action_impl == 'aiAssert':
                sr['assert'] = {'pass': True, 'reason': ''}

            if wait_ms and action_impl != 'wait':
                time.sleep(wait_ms / 1000.0)

            # 每步执行后都留一张实拍截图：报告里要能"步骤 ↔ 画面"一一对应。
            # 之前只有显式 screenshot 动作才截、失败才截，所以成功的用例
            # 报告里一张图都没有，用户无法确认这一步到底做成什么样。
            if action not in ('screenshot',):
                try:
                    sr['screenshot'] = self.screenshot()
                except Exception as exc:  # noqa: BLE001
                    # 截图失败不能影响用例结果，记一笔即可
                    self._log('[步骤 %d] 截图失败: %s', idx + 1, exc)
        except Exception as e:  # noqa: BLE001
            sr['status'] = 'failed'
            sr['error'] = str(e)
            self._log('[步骤 %d] 失败: %s', idx + 1, e)
            try:
                sr['fail_screenshot'] = self.screenshot()
            except Exception:  # noqa: BLE001
                pass
        return sr

    @staticmethod
    def _step_has_coords(step):
        """该步是否已经固化过坐标。"""
        if not isinstance(step, dict):
            return False
        cx, cy = step.get('center_x'), step.get('center_y')
        if cx is None or cy is None:
            return False
        try:
            int(cx), int(cy)
            return True
        except (TypeError, ValueError):
            return False

    def _replay_step(self, action, step, prompt, value):
        """纯坐标回放一步：直接按固化坐标点击，不调用任何视觉模型。

        失败就抛异常，由调用方（_run_midscene）决定是否降级回 AI 模式。
        """
        cx = int(step['center_x'])
        cy = int(step['center_y'])
        self._log('[回放] %s -> (%d, %d)  [零模型]', action, cx, cy)

        if action == 'aiInput':
            # 先点输入框获得焦点，再输入文本
            self._rpc('tapPoint', {'sessionId': self._session_id, 'x': cx, 'y': cy})
            time.sleep(0.4)
            self._rpc('inputText', {'sessionId': self._session_id, 'text': value or ''})
        else:
            # aiTap / aiAct 都退化成"点这个坐标"
            self._rpc('tapPoint', {'sessionId': self._session_id, 'x': cx, 'y': cy})

        wait_ms = int(step.get('wait_ms') or 0)
        if wait_ms:
            time.sleep(wait_ms / 1000.0)

    def _wait_for_timeout(self, step):
        """取本步条件等待的超时（毫秒）：优先步骤的 timeout_ms，否则用默认值。"""
        try:
            val = int(step.get('timeout_ms') or 0)
        except (TypeError, ValueError):
            val = 0
        return val or self.DEFAULT_WAIT_FOR_TIMEOUT_MS

    def _popup_adapter(self):
        """把本引擎的能力包成 popup_handler 需要的接口。"""
        return _IOSPopupAdapter(self)

    def playground_run(self, prompt, action='aiAct', value='', bundle_id='',
                       launch_first=True):
        """Playground 单条自然语言调试（iOS）。"""
        result = {
            'status': 'success', 'logs': [],
            'screenshot_base64': '', 'query_result': None, 'error': None,
        }
        started_here = self._session_id is None
        try:
            if started_here:
                self.start()
            if launch_first and bundle_id:
                self.launch_app(bundle_id)
                time.sleep(3)

            if action == 'aiAct':
                self._rpc('aiAct', {'sessionId': self._session_id, 'prompt': prompt})
            elif action == 'aiTap':
                self._rpc('aiTap', {'sessionId': self._session_id, 'prompt': prompt})
            elif action == 'aiInput':
                self._rpc('aiInput', {
                    'sessionId': self._session_id, 'prompt': prompt, 'value': value})
            elif action == 'aiQuery':
                res = self._rpc('aiQuery', {'sessionId': self._session_id, 'prompt': prompt})
                result['query_result'] = res.get('result')
                self._log('aiQuery 结果: %s', json.dumps(res.get('result'), ensure_ascii=False)[:500])
            elif action == 'aiAssert':
                self._rpc('aiAssert', {'sessionId': self._session_id, 'prompt': prompt})
                result['query_result'] = {'pass': True}
                self._log('AI 断言通过')
            elif action == 'aiWaitFor':
                self._rpc('aiWaitFor', {'sessionId': self._session_id, 'prompt': prompt})
            else:
                raise ValueError('Playground 不支持的 action: %s' % action)

            result['screenshot_base64'] = self.screenshot()
        except Exception as e:  # noqa: BLE001
            result['status'] = 'failed'
            result['error'] = str(e)
            self._log('执行失败: %s', e)
            try:
                result['screenshot_base64'] = self.screenshot()
            except Exception:  # noqa: BLE001
                pass
        finally:
            result['logs'] = list(self.logs)
            if started_here:
                self.close()
        return result


class _IOSPopupAdapter(PopupAdapter):
    """把 iOS 引擎的 JSON-RPC 调用适配成 popup_handler 的接口。"""

    def __init__(self, engine):
        self._engine = engine

    def query(self, prompt: str):
        res = self._engine._rpc('aiQuery', {
            'sessionId': self._engine._session_id,
            'prompt': prompt,
        })
        return res.get('result')

    def tap(self, target: str) -> None:
        self._engine._rpc('aiTap', {
            'sessionId': self._engine._session_id,
            'prompt': target,
        })

    def log(self, msg: str) -> None:
        self._engine._log('%s', msg)


__all__ = ['MidsceneIOSEngine', 'MidsceneIOSServiceError']
