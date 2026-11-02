"""
Midscene APP UI 自动化执行引擎

基于 Midscene（Python 包 midscene，底层桥接 @midscene/android）驱动 Android 真机，
执行 AICase（execution_mode='midscene'）的结构化 steps。设计原则（详见 docs/midscene_app_automation_design.md）：

- 步骤业务无关：action/prompt/value/extract/assert 全由用例设计者定义，框架不预置业务语义。
- 业务锚点（上下文传递）：步骤 extract 提取任意结构化数据存入 context，后续步骤用 ${{key}} 引用。
- 确定性操作优先 Instant Action（aiTap/aiInput），需理解页面语义才用 aiAct/aiQuery。
- 视觉模型取自 AIModelConfig(role='midscene_vision')，不写死 Key。

注意：midscene 相关第三方包在运行时（容器）才安装；本模块使用 lazy import，
保证在没有 midscene 包的环境（如本机 py_compile / 部分测试）下仍可导入与静态校验。
"""
import os
import json
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

# 随机弹窗（权限请求 / 隐私协议 / 开屏广告 ……）的自动处理。
# ⚠️ 开关与策略统一定义在 popup_handler（**单一事实源**），这里只做引用，
#    避免两处各自读环境变量导致"改了一处不生效"。
from .popup_handler import (  # noqa: E402
    dismiss_popups, PopupAdapter, POPUP_AUTO_DISMISS, POPUP_PRECHECK,
)

logger = logging.getLogger(__name__)

# ${{key}} 模板占位符正则
CONTEXT_VAR_PATTERN = re.compile(r"\$\{\{(\s*[\w.-]+\s*)\}\}")
# 某一步失败后，最多"关弹窗 + 重试"几次
POPUP_RETRY_LIMIT = int(os.environ.get('POPUP_RETRY_LIMIT', '2') or 2)
#: 断言步骤的弹窗重试次数放宽一些（见 run() 中的说明）。
ASSERT_POPUP_RETRY_LIMIT = int(
    os.environ.get('ASSERT_POPUP_RETRY_LIMIT', '3') or 3)

#: ai_act 单条指令的「重规划上限」。midscene 默认 20，超过就抛
#:   Replanned 20 times, exceeding the limit. Please configure a larger value
#:   for replanningCycleLimit (or use MIDSCENE_REPLANNING_CYCLE_LIMIT)
#: 实测原因几乎都是「一步要干太多事」（如「进入设置并修改XX后返回」），
#: 模型每次规划只能推进一点，20 轮不够就报错。给到 40 能覆盖多数长流程。
#: 注：这个值不是越大越好 —— 每轮重规划都烧一次视觉模型调用，
#: 真定位不到元素时调大会让单步耗时翻倍，所以路径级超时（apk_workflow）仍是兜底。
REPLANNING_CYCLE_LIMIT = int(
    os.environ.get('MIDSCENE_REPLANNING_CYCLE_LIMIT', '40') or 40)

#: midscene Python 客户端调 Node 服务的 RPC 读超时（秒）。
#:
#: 库默认写死 120（`midscene/_internal/base_agent.py:24 _DEFAULT_TIMEOUT = 120`），
#: 而一条 `aiAct` 走多轮视觉推理很容易超过 120s ——
#: 实测「点击底部黄历Tab」从 16:26:01 开始，到 16:28:01 **整整 120 秒后**
#: 客户端断开，期间手机上一次点击都没发生（不是点错，是还没轮到点）。
#: 客户端一断，Node 侧会话就乱了，紧接着下一步直接 `Session not found`。
#:
#: 所以这里把超时提到 300s：不是纵容慢，而是**别在正常执行中途砍断**。
#: 真正的"卡死"兜底仍在路径级超时（apk_workflow.APK_PATH_TIMEOUT，默认 900s）。
RPC_TIMEOUT = int(os.environ.get('MIDSCENE_RPC_TIMEOUT', '300') or 300)


def _apply_rpc_timeout():
    """把 RPC_TIMEOUT 应用到期 client 的类属性上（幂等）。"""
    try:
        from midscene._internal.base_agent import BaseAgent
        if getattr(BaseAgent, 'DEFAULT_TIMEOUT', None) != RPC_TIMEOUT:
            BaseAgent.DEFAULT_TIMEOUT = RPC_TIMEOUT
            logger.info('[Midscene] 设置 RPC DEFAULT_TIMEOUT=%ss', RPC_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[Midscene] 设置 RPC 超时失败: %s', exc)

# 坐标/像素写法护栏：用例必须用语义描述，禁止写死坐标，否则换机型必错位
# 命中规则（兼顾 midscene bbox=[x,y,x,y] 与单点 [x,y]，但尽量不误伤业务数字）：
#   1) 四个数字（bbox）：\d,\d,\d,\d
#   2) 括号包裹的两数（单点坐标）：(x, y) / （x，y）
#   3) 显式坐标键：x= / y: / 坐标: / coord= 等
_COORD_HINT = re.compile(
    r'('
    r'\d{1,5}\s*[,，]\s*\d{1,5}\s*[,，]\s*\d{1,5}\s*[,，]\s*\d{1,5}'  # 四数 bbox
    r'|[\(\（]\s*\d{1,5}\s*[,， ]\s*\d{1,5}\s*[\)\）]'              # 括号包裹的两数单点
    r'|(坐标|coord|point|点击|点|tap|click)\D{0,10}\d{2,5}\s*[,，]\s*\d{2,5}'  # 坐标/点击动词后紧跟 数字,数字
    r')'
    r'|(坐标|coord|point)\s*[:=]'
    r'|(?<![A-Za-z0-9_])(x|y)\s*[:=]',                              # x= / y: 等显式坐标键（避免误伤英文单词）
    re.I,
)


def _guard_semantic_prompt(prompt: str, where: str = '') -> None:
    """拦截疑似写死坐标的 prompt，强制用语义描述以保证跨机型兼容性。

    仅对「自然语言操作类」prompt 生效；纯业务数据（如提取金额、手机号）不含坐标形状，
    不会被误伤。命中时抛 ValueError，由调用方转成 failed/错误提示。
    """
    if not prompt or not isinstance(prompt, str):
        return
    m = _COORD_HINT.search(prompt)
    if m:
        hit = m.group(0)
        raise ValueError(
            "【%s】检测到疑似坐标写法「%s」。Midscene 为语义定位引擎，请改用语义描述"
            "（如「点右下角的我的 Tab」「在搜索框输入天气」），不要写死像素/坐标，"
            "否则换机型会失效。" % (where or 'prompt', hit)
        )


#: aiTap「定位不到元素」的失败特征（midscene 从 agent.ts 抛出的原文）。
#:
#: 只有这一类失败才值得降级 aiAct：
#:   - `failed to locate element`：元素树里没有/不可交互 ⇒ 换视觉定位能解决
#:   - `Element not found` / `No element found`：同上
#: **不能**降级的：坐标护栏拦截（ValueError，提示词本身有问题）、
#: 会话失效（Session not found）、网络/模型错误 —— 换 aiAct 只会再失败一次。
_LOCATE_FAILURE_HINTS = (
    'failed to locate element',
    'element not found',
    'no element found',
    '未找到该文字',
    '没有找到',
    # 点击校验（_verify_tap_effect）的判词：ai_tap 没抛异常但画面没变，
    # 等价于"没点到"。底部 Tab 这类零面积节点最容易命中这种情况。
    '未生效',
    '未发生跳转',
)


#: 设备会话彻底失效的特征。与 `run()` 内部 `_is_dead_session_error` 同口径
#: （那边是函数内局部定义，模块级拿不到，所以这里单独写一份；
#: 与 apk_workflow._SESSION_DEAD_MARKERS 也保持一致）。
_SESSION_DEAD_HINTS = (
    'has been destroyed and cannot execute',
    'androiddevice destroyed',
    'device has been disconnected',
    'adb: device offline',
    'device not found',
    'session not found',
    'unable to connect to device',
)


def _is_locate_failure(exc) -> bool:
    """判断异常是否为「aiTap 在元素树里定位不到目标」。

    必须先排除不该降级的情形，否则会把"提示词写了坐标"这类**用户输入错误**
    也拿去跑 aiAct，白烧一次模型调用还掩盖了真正原因。
    """
    msg = str(exc or '')
    low = msg.lower()
    # ① 提示词本身不合法（坐标护栏）——换引擎也没用，必须让用户改提示词
    if '检测到疑似坐标写法' in msg:
        return False
    # ② 设备会话已失效——换动作类型不会让会话复活
    if any(m in low for m in _SESSION_DEAD_HINTS):
        return False
    return any(h in low for h in _LOCATE_FAILURE_HINTS)


def _tap_intent_prompt(target: str, original_prompt: str) -> str:
    """把 aiTap 的目标文字改写成 aiAct 能执行的点击指令。

    为什么要改写而不是直接传 target：aiAct 是自主规划，
    只给它一个词（「天气」）它会不知道要干嘛；给完整句
    （「点击底部「天气」Tab」）它才知道是"点这个入口"。
    优先用原始 prompt（语义更完整），没有才用 target 兜底。
    """
    text = (original_prompt or '').strip()
    if not text:
        text = (target or '').strip()
    if not text:
        return text
    # 原始 prompt 已经是"点击/进入"类动词开头就不用再加
    if re.match(r'^(点击|点按|单击|点一下|轻触|进入|打开|切换|滑动|输入|填写)', text):
        return text
    return '点击「%s」' % text


#: 「条件句」：若/如果……则点击「X」。
#:
#: 2026-10-29 真实事故：REA 生成了一条「若出现开屏广告则点击「跳过」按钮」，
#: 执行时当前页**没有开屏广告**，aiAct 拿这句话去做多轮自主规划，找不到「跳过」
#: 就自己离开目标 App 去找（打开系统设置 / 点广告拉起京东）。用户看到的就是
#: "怎么乱启动其他 App"。
#:
#: 条件句本身在启动后没有意义（启动已完成、页面已可见），而且最容易诱发
#: 模型的"搜索式"行为。这里统一识别，见 `_neutralize_conditional`。
_CONDITIONAL_STEP = re.compile(
    r'^(?:若|如果|假如|如|当)\s*[^，,。]*?(?:则|就|那么|请)?\s*'
    r'(?:点击|点按|单击|轻触|返回|关闭|输入|填写)')

#: 强制"只在本 App 内操作"的提示词约束。
#:
#: 为什么需要：模型找不到元素时默认行为是"继续尝试"，而它能想到的尝试包括
#: 打开系统设置、点广告跳浏览器。这个约束把"离开 App 去找"这条路从提示词侧
#: 就堵住，配合 `_guard_foreground` 的代码侧兜底形成双保险。
#:
#: ⚠️⚠️ 措辞不能写"若看不到目标元素，请直接结束本步"
#: （2026-10-29 事故，record 65「日历模式切换」）：
#:   原措辞「**若看不到目标元素，请直接结束本步**」被模型理解成
#:   "没达到预期就主动放弃"，于是它在**本页内**合法地多试几次都还没到位时
#:   （会员限制/加载慢/需要滚动）就直接抛
#:   `Task failed: 天气模式切换未成功…可能受会员权限限制无法切换`，
#:   用户看到的是"明明切换成功了却报失败"。
#:   同一份用例的另一条记录（没有这句约束）就跑通了。
#: ⇒ 只约束"不许离开 App"，**不要**约束"找不到就放弃"。
#:   "要不要放弃"应该由模型自主判断（aiAct 是自主循环），失败原因也该是
#:   真实的功能问题，而不是被提示词逼出来的假失败。
_STAY_IN_APP_RULE = (
    '（重要：只允许在本 App 内部操作。'
    '禁止打开系统设置、通知栏、浏览器或任何其它 App —— '
    '需要滚动、返回或切换页签时请在本 App 内完成。）'
)


def _looks_like_app_switch(text: str) -> bool:
    """这一步的意图是否涉及"在本 App 内跳转"。

    只用来决定要不要追加 `_STAY_IN_APP_RULE`（多一条约束就多一分
    被模型误读成"放弃"的风险，所以能不加就不加）。
    """
    t = str(text or '')
    return any(w in t for w in (
        'Tab', 'tab', '页签', '底部', '顶部', '导航', '切换', '返回', '进入',
        '点击', '进入页面', '跳转',
    ))


def _neutralize_conditional(text: str) -> str:
    """把条件句改写成一个"纯观察"请求，避免诱发模型离开 App 搜索元素。

    「若出现开屏广告则点击「跳过」按钮」→「观察当前页面是否为开屏广告，
      是则点击「跳过」；当前页没有该元素时不要做任何操作」

    保留"是则点击"的语义，但显式写明"没有就什么都不做"，模型就不会
    为了完成任务跑去别处找。
    """
    t = str(text or '').strip()
    if not t or not _CONDITIONAL_STEP.match(t):
        return t
    return ('观察当前页面：%s。当前页面若没有该元素，请不要做任何操作，'
            '也不要打开其它应用。' % t)


def _resolve_template(text: str, context: Dict[str, Any]) -> str:
    """把字符串中的 ${{key}} 替换为上下文变量值。"""
    if not text or not isinstance(text, str):
        return text

    def _sub(m):
        key = m.group(1).strip()
        if key in context:
            val = context[key]
            return str(val) if not isinstance(val, str) else val
        # 未找到时不替换，保留占位符以便排查
        logger.warning("[Midscene] 上下文变量 ${{%s}} 未找到，保留原样", key)
        return m.group(0)

    return CONTEXT_VAR_PATTERN.sub(_sub, text)


class MidsceneAppEngine:
    """Midscene 移动端执行引擎（Android 真机 / 模拟器）"""

    # 支持的步骤 action
    SUPPORTED_ACTIONS = {
        'aiAct', 'aiQuery', 'aiTap', 'aiInput', 'assert', 'wait',
        'launch', 'back', 'home', 'screenshot', 'aiWaitFor',
    }

    #: 条件等待的默认超时（毫秒）。aiWaitFor 语义是"等到条件成立"，
    #: 不像其他动作那样立刻返回，所以必须有个上限，否则页面一直
    #: 不出现目标元素时会一直卡住（默认 15 秒，可用步骤的
    #: timeout_ms 字段按步骤覆盖）。
    DEFAULT_WAIT_FOR_TIMEOUT_MS = 15000

    #: 视觉模型的来源
    #:   'legacy'  —— AIModelConfig(role='midscene_vision')，原有功能（AICase / Playground / Web）继续用
    #:   'ai_mode' —— AiModeModelConfig(role='vision')，仅 AI 智能模式（APK 回归）用
    #:
    #: 默认 'legacy' 是**故意的**：引擎有多个使用方，改默认值会连带改动其它功能的行为。
    #: 只有明确从 AI 智能模式进来的调用方（apk_workflow）才传 'ai_mode'，
    #: 这样"新入口只影响 AI 智能模式"这条约束才是真的成立。
    MODEL_SOURCE_LEGACY = 'legacy'
    MODEL_SOURCE_AI_MODE = 'ai_mode'

    def __init__(self, app_config, device, model_config: Optional[dict] = None,
                 launch_wait_ms: int = 6000, workspace: Optional[str] = None,
                 auto_dismiss_popup: Optional[bool] = None,
                 popup_precheck: Optional[bool] = None,
                 replay_mode: bool = False,
                 model_source: str = MODEL_SOURCE_LEGACY,
                 log_sink=None):
        """
        Args:
            app_config: AppConfig 实例（含 package_name / app_activity）
            device: AppDevice 实例（含 udid / adb_host）
            model_config: {'api_key','base_url','model_name','model_family'}，
                          为空时按 model_source 去对应配置表读取
            launch_wait_ms: 启动 APP 后等待毫秒
            workspace: 截图/日志工作目录
            auto_dismiss_popup: 弹窗自动处理。None=用全局默认（环境变量），
                                True/False=用例配置优先
            replay_mode: 纯坐标回放。用例已固化时开启，每步直接按坐标点，
                         不调用视觉模型。
            model_source: 视觉模型取自哪张配置表，见 MODEL_SOURCE_* 说明。
                          默认 legacy（原有功能行为不变）。
            log_sink: 可选回调 ``fn(line: str)``，每产生一行日志**立即**回调。
                      为什么不直接用返回的 logs：Midscene 一条 aiAct 可能跑几分钟，
                      整条路径跑完才返回结果 ⇒ 期间前端一行日志都没有，
                      用户看到真机"没反应"也无法区分是卡死还是在执行。
                      有了 sink 就能把「[步骤 3] 点击xx | 开始」这类过程日志实时落库。
        """
        self.app_config = app_config
        self.device = device
        self.launch_wait_ms = launch_wait_ms
        self.workspace = workspace or os.path.join('media', 'midscene')
        os.makedirs(self.workspace, exist_ok=True)

        self.auto_dismiss_popup = (
            POPUP_AUTO_DISMISS if auto_dismiss_popup is None else bool(auto_dismiss_popup)
        )
        #: 执行前是否先清一轮弹窗。None=用全局默认（POPUP_PRECHECK），
        #: 显式传值 = 项目级配置优先（见 apk_workflow）。
        self.popup_precheck = (
            POPUP_PRECHECK if popup_precheck is None else bool(popup_precheck)
        )
        self.replay_mode = bool(replay_mode)
        #: 每个引擎实例只做一次"唤醒+解锁"（`_start_app` 的重试不应反复解锁）
        self._woke = False
        self.model_source = model_source
        self.model_config = model_config or self._load_model_config()
        # 运行期对象（lazy init）
        self._page = None  # Midscene AndroidPage 实例
        self._agent = None  # Midscene agent（若使用）
        self.context: Dict[str, Any] = {}  # 跨步骤业务上下文（业务锚点）
        self.logs: List[str] = []
        # 实时日志回调：每行日志立刻外发（见 __init__ 的 log_sink 说明）
        self._log_sink = log_sink

    # ---------------- 模型配置 ----------------
    def _load_model_config(self) -> dict:
        """按 self.model_source 取视觉模型配置。"""
        if self.model_source == self.MODEL_SOURCE_AI_MODE:
            return self._load_ai_mode_config()
        try:
            from apps.requirement_analysis.models import AIModelConfig
        except Exception as e:  # noqa
            logger.warning("[Midscene] 无法导入 AIModelConfig: %s", e)
            return {}
        cfg = AIModelConfig.objects.filter(role='midscene_vision', is_active=True).first()
        if not cfg:
            return {}
        return {
            'api_key': cfg.api_key,
            'base_url': cfg.base_url,
            'model_name': cfg.model_name,
            'model_family': getattr(cfg, 'model_family', '') or '',
        }

    def _load_ai_mode_config(self) -> dict:
        """AI 智能模式专属视觉模型（AiModeModelConfig, role='vision'）。

        与 legacy 的差别：model_family 由配置自身推断（`resolved_family`），
        因为不同多模态模型的 family 值不同（qwen3-vl / doubao-vision / glm-v …），
        写错 midscene 会直接判非法值。
        """
        try:
            from .models import AiModeModelConfig, get_active_ai_mode_config
        except Exception as e:  # noqa
            logger.warning("[Midscene] 无法导入 AiModeModelConfig: %s", e)
            return {}
        cfg, err = get_active_ai_mode_config(AiModeModelConfig.ROLE_VISION)
        if not cfg:
            # 抛出去比返回 {} 好：{} 会让引擎静默走"无模型"路径，
            # 现场只看到一堆莫名的执行失败。
            raise RuntimeError(err)
        runtime = cfg.as_runtime_config()
        logger.info("[Midscene] 视觉模型来自 AI 智能模式配置「%s」(%s / family=%s)",
                    cfg.name, cfg.model_name, runtime.get('model_family') or '(自动)')
        return runtime

    # ---------------- 设备连接 ----------------
    @staticmethod
    def _reset_node_manager():
        """重置 midscene 的 Node 服务单例，让新模型配置能生效。

        midscene 把 Node 子进程按 spec 名称做成**进程级单例**
        （`NodeServiceManager._instances['android']`），API Key / model family
        等环境变量在**子进程启动那一刻**固化。用户在配置页改模型后：
          - 改数据库：Python 侧的单例 `_config` 仍是旧对象 → 下次 get() 抛
            "already running with a different MidsceneConfig"
          - 只 kill Node 子进程：注册表还指向旧实例，重新拉起的进程仍是旧配置

        两种半吊子做法都不行，必须在**同一个 Python 进程内**调用 reset()，
        让 manager 连同子进程一起重建。这里做三重兜底：
          1. `reset('android')` —— 官方推荐入口
          2. 直接清 `_instances` 字典 —— reset 内部若只 shutdown 不移除（不同版本行为不同），
             残留条目仍会触发配置比对失败
          3. 任何异常都不阻断连接（配置没变时 reset 是多余的，失败也不影响）
        """
        try:
            from midscene._internal.node_service import NodeServiceManager
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Midscene] 无法导入 NodeServiceManager，跳过重置: %s", exc)
            return
        try:
            NodeServiceManager.reset('android')
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Midscene] reset('android') 失败（继续尝试清注册表）: %s", exc)
        # 兜底：确保注册表里没有残留旧实例
        try:
            registry = getattr(NodeServiceManager, '_instances', None)
            if isinstance(registry, dict) and registry.pop('android', None) is not None:
                logger.info("[Midscene] 已从注册表移除残留的 android Node 服务实例")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Midscene] 清理 Node 服务注册表失败: %s", exc)

    def connect(self):
        """建立 Midscene 与真机的连接（lazy import midscene）。

        midscene 的 Python 包入口为 MidsceneAndroidAgent（非 midscene.android.AndroidPage）。
        远程手机架构：手机 USB 插用户电脑，后端经该机 ADB server 控制。
        若 device.adb_host 形如 '192.168.8.71:5037'，则设置 ADB_SERVER_SOCKET 让
        @midscene/android 走该 adb server；device_id 仍用 udid。
        """
        try:
            from midscene import MidsceneAndroidAgent, MidsceneConfig
        except Exception as e:
            raise RuntimeError(
                "未安装 midscene，无法连接真机（%s）。请在后端容器执行 "
                "pip install midscene 后重试。" % e
            )

        adb_host = (self.device.adb_host or '').strip() or ''
        device_id = self.device.udid
        if adb_host:
            # 远程 adb server（如 192.168.8.71:5037）。
            # @midscene/android v0.1.5 读取的环境变量是 MIDSCENE_ADB_REMOTE_HOST /
            # MIDSCENE_ADB_REMOTE_PORT（代码核实：dist/lib/index.js 读
            # getEnvConfigValue(MIDSCENE_ADB_REMOTE_HOST) 而非 ANDROID_ADB_SERVER_HOST），
            # 故必须设这两个变量，midscene 才会把 adb 命令发往用户电脑的 adb server。
            # 该环境变量会被 midscene 的 Node 服务进程继承。
            host = adb_host
            port = '5037'
            if ':' in adb_host:
                host, _, port = adb_host.rpartition(':')
            os.environ['MIDSCENE_ADB_REMOTE_HOST'] = host
            os.environ['MIDSCENE_ADB_REMOTE_PORT'] = port
            # 兼容保留：部分 appium-adb 版本仍读 ANDROID_ADB_SERVER_HOST/PORT
            os.environ['ANDROID_ADB_SERVER_HOST'] = host
            os.environ['ANDROID_ADB_SERVER_PORT'] = port
            logger.info("[Midscene] 设置 MIDSCENE_ADB_REMOTE_HOST=%s PORT=%s", host, port)
        logger.info("[Midscene] 连接设备 udid=%s", device_id)

        # 重规划上限：必须写 os.environ，Node 服务子进程才会继承。
        # 不设时 midscene 默认 20，复杂路径会直接抛 "Replanned 20 times"。
        # 放在 model_config 分支之外，保证任何连接方式下都生效。
        os.environ['MIDSCENE_REPLANNING_CYCLE_LIMIT'] = str(REPLANNING_CYCLE_LIMIT)
        logger.info("[Midscene] 设置 MIDSCENE_REPLANNING_CYCLE_LIMIT=%s",
                    REPLANNING_CYCLE_LIMIT)
        # RPC 读超时：客户端侧断开会连带把 Node 会话搞乱（下一步报 Session not found），
        # 所以必须放宽到 300s，别在正常执行中途砍断。
        _apply_rpc_timeout()

        # 模型配置（视觉模型，OpenAI 兼容）
        cfg = self.model_config or {}
        model_config = None
        if cfg.get('api_key'):
            family = cfg.get('model_family') or ''
            # midscene v1.x 的 Node 服务从 MIDSCENE_MODEL_FAMILY 环境变量读取
            # model family，Python 端 MidsceneConfig(model_family=...) 不会自动
            # 映射到该环境变量，必须显式写入 os.environ 才能被子进程继承。
            if family:
                os.environ['MIDSCENE_MODEL_FAMILY'] = family
                logger.info("[Midscene] 设置 MIDSCENE_MODEL_FAMILY=%s", family)
            model_config = MidsceneConfig(
                base_url=cfg.get('base_url') or '',
                api_key=cfg['api_key'],
                model_name=cfg.get('model_name') or '',
                model_family=family,
            )
        # 切配置前必须重置 Node 服务单例，否则抛
        #   MidsceneConfigError: NodeServiceManager for 'android' is already
        #   running with a different MidsceneConfig...
        #
        # 原因：NodeServiceManager 是**进程级单例**（按 spec.name='android' 存在
        # `_instances` 字典里），config 在 Node 子进程**启动那一刻**就固化成环境变量。
        # 用户在「模型配置」页改了模型/family 后，数据库变了、但 Python 内存里
        # 那个单例的 _config 还是旧的 ⇒ 下一次 connect 一比对就报错。
        # 同理：只 kill Node 子进程也没用——注册表仍指向旧实例，进程拉起后仍是旧配置。
        # 所以每次连接前主动 reset，让 manager 用**当前** config 重新起进程。
        #
        # 代价：reset 会关掉现有 Node 服务，正在跑的路径会被打断。但本项目执行是
        # **串行**的（同一 run 有互斥锁，路径逐条跑），而且每条路径都会新建
        # MidsceneAppEngine 并 connect，所以这里 reset 的实际影响只是"重启一次 Node 服务"，
        # 换来的是"改了配置立刻生效"，这个优先级更高。
        self._reset_node_manager()

        self._agent = MidsceneAndroidAgent(device_id, model_config)
        self._page = self._agent  # 兼容下方 step/playground 逻辑，统一用 self._page
        self._log("已连接设备 %s", device_id)
        return self._page

    # ---------------- 前台包名守卫 ----------------
    #: 步骤执行前后校验"前台仍是目标 App"。
    #: 关掉可用 APP_FOREGROUND_GUARD=0。
    FOREGROUND_GUARD = os.environ.get('APP_FOREGROUND_GUARD', '1') not in (
        '0', 'false', 'False', '')
    #: 这些是"系统级"的前台窗口，不视为"跳到别的 App"：
    #: 权限弹窗（com.android.permissioncontroller / com.lbe.security）、
    #: 输入法、拨号器（clickable 电话号）、系统安装器。
    #:
    #: ⚠️ 2026-10-29 真实事故：aiAct/aiTap 的提示词里写了「点击「跳过」按钮」，
    #: 模型在页面上找不到「跳过」，就自主打开了**系统设置**去找，甚至点广告拉起
    #: 京东等第三方 App。旧实现只靠"路径 skip 约束 + 弹窗禁点清单"，拦不住
    #: "模型自己决定换一个 App 去完成任务"。所以在步骤边界硬校验前台包名。
    FOREGROUND_EXEMPT_PREFIX = (
        'com.android.permissioncontroller',
        'com.google.android.permissioncontroller',
        'com.lbe.security',
        'com.android.packageinstaller',
        'com.google.android.packageinstaller',
        'com.android.settings',
        'com.android.systemui',
        'com.android.inputmethod',
        'com.android.dialer',
        'com.google.android.dialer',
        'com.android.contacts',
        'com.android.phone',
        'android',
    )

    def _foreground_package(self):
        """当前前台 App 的包名；取不到返回空串（调用方据此跳过校验）。"""
        if self._page is None:
            return ''
        try:
            out = self._page.run_adb_shell(
                "dumpsys window 2>/dev/null | grep -m1 -E 'mCurrentFocus|mFocusedApp'")
        except Exception:  # noqa: BLE001
            return ''
        m = re.search(r'([A-Za-z][\w.]*)/[\w.$]+', str(out or ''))
        return m.group(1) if m else ''

    def _is_foreground_drift(self, fg):
        """前台包名是否已经**不是目标 App**（且不是系统级白名单）。"""
        pkg = (getattr(self.app_config, 'package_name', '') or '').strip()
        if not pkg or not fg:
            return False
        if fg == pkg or fg.startswith(pkg + '.'):
            return False
        return not any(fg.startswith(p) for p in self.FOREGROUND_EXEMPT_PREFIX)

    def _guard_foreground(self, where=''):
        """步骤边界守卫：发现跑到别的 App 就**拉回目标 App** 并明确判失败。

        为什么必须判失败而不是"悄悄拉回来继续跑"：跑到别的 App 说明
        这一步的语义已经没完成（模型去别的 App 找按钮了），装作成功只会把
        错误带到后面所有步骤，报告全错。这里拉回前台只是为了让后续步骤
        还有救，判失败才是如实记录。
        """
        if not self.FOREGROUND_GUARD or self._page is None:
            return
        fg = self._foreground_package()
        if not self._is_foreground_drift(fg):
            return
        pkg = self.app_config.package_name
        msg = ('%s期间界面跳到了其它 App（当前前台：%s，目标：%s）。'
               '通常是模型没在目标 App 内找到目标元素，自行去系统/第三方应用里找。'
               % (where, fg, pkg))
        self._log('[前台守卫] %s', msg)
        try:
            # 先返回一次（退出被拉起的 App），再拉起目标 App。
            # 只做 am start 不 force-stop，与 reset_to_home 的口径一致。
            self._page.run_adb_shell("input keyevent KEYCODE_BACK")
            activity = getattr(self.app_config, 'app_activity', '') or ''
            if activity:
                self._page.run_adb_shell("am start -n %s/%s" % (pkg, activity))
            else:
                self._page.run_adb_shell(
                    "monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg)
            time.sleep(1.5)
            self._log('[前台守卫] 已把 %s 拉回前台', pkg)
        except Exception as exc:  # noqa: BLE001
            self._log('[前台守卫] 拉回 %s 失败: %s', pkg, exc)
        self._entry_cache = None
        raise AssertionError(msg)

    #: 启动后确认"App 真的到了前台"的重试次数与间隔。
    APP_LAUNCH_RETRIES = 3
    APP_LAUNCH_RETRY_WAIT_S = 3.0

    # ---------------- 设备唤醒 / 解锁 / 保持亮屏 ----------------
    #: 执行前是否自动唤醒+解锁+保持亮屏。`AUTO_WAKE_DEVICE=0` 可关。
    AUTO_WAKE_DEVICE = os.environ.get('AUTO_WAKE_DEVICE', '1') not in (
        '0', 'false', 'False', '')

    def _device_status(self):
        """读取 (是否亮屏, 是否锁屏)。取不到的项返回 None（不据此下结论）。"""
        awake = locked = None
        try:
            out = str(self._page.run_adb_shell(
                "dumpsys power | grep -E 'mWakefulness=|mStayOn='") or '')
            if 'mWakefulness=' in out:
                awake = 'mWakefulness=Awake' in out
        except Exception:  # noqa: BLE001
            pass
        try:
            out2 = str(self._page.run_adb_shell(
                "dumpsys window | grep -m2 -E 'mDreamingLockscreen|mShowingLockscreen'") or '')
            if out2.strip():
                # 两个标志都为 false 才明确"没锁屏"；出现 true 即认为锁屏中
                locked = ('mDreamingLockscreen=true' in out2
                          or 'mShowingLockscreen=true' in out2)
        except Exception:  # noqa: BLE001
            pass
        return awake, locked

    #: 疑似"手机睡过去了"的错误特征。
    #:
    #: 2026-10-29 实测：锁屏时 `am start` 报
    #:   `ADB error with device ... when calling shell`
    #: 常见还有 `device offline` / `error: closed` / `Input dispatching timed out`。
    #: 这里只用来决定"值不值得唤醒一次"，不做终局判断（终局看 _device_status）。
    _SLEEP_HINTS = (
        'adb error with device',
        'when calling shell',
        'device offline',
        'error: closed',
        'device still authorizing',
        'input dispatching timed out',
        'cannot execute adb commands',
    )

    def _looks_like_sleep(self, err) -> bool:
        text = str(err or '').lower()
        return any(h in text for h in self._SLEEP_HINTS)

    def _wake_and_unlock(self):
        """唤醒屏幕 + 解除锁屏，并把亮屏超时拉长。

        为什么必须自动化（2026-10-29 实测，RMX2117 / Android 12）：
          - 手机锁屏时 `am start` 会报
            `ADB error with device ... when calling shell`，
            而旧逻辑只记一行 warning 继续跑 ⇒ 后面的点击全落在锁屏/桌面上。
          - `svc power stayon true` 在 ColorOS 上**返回 137（被系统杀掉）**，
            不可依赖；`settings put system screen_off_timeout` 可用但需权限，
            所以只作"尽力而为"，失败不报错。
        实测可用的两条：`input keyevent KEYCODE_WAKEUP` rc=0、`wm dismiss-keyguard` rc=0。
        """
        if not self.AUTO_WAKE_DEVICE or self._page is None:
            return
        awake, locked = self._device_status()
        self._log('[唤醒] 状态：亮屏=%s 锁屏=%s', awake, locked)
        if awake is False:
            try:
                self._page.run_adb_shell("input keyevent KEYCODE_WAKEUP")
                self._log('[唤醒] 已发送 KEYCODE_WAKEUP')
                time.sleep(1.0)
            except Exception as exc:  # noqa: BLE001
                self._log('[唤醒] KEYCODE_WAKEUP 失败: %s', exc)
        if locked is not False:
            # locked 为 True 或 None（读不到）都尝试解锁：解锁是幂等的，
            # 没锁屏时执行也不会有副作用。有密码/图案锁的设备上这一步只能
            # 清掉"滑动锁"，无法越过安全锁，届时启动确认会明确判失败。
            try:
                self._page.run_adb_shell("wm dismiss-keyguard")
                self._log('[唤醒] 已发送 wm dismiss-keyguard')
                time.sleep(1.0)
            except Exception as exc:  # noqa: BLE001
                self._log('[唤醒] dismiss-keyguard 失败: %s', exc)
        # 尽力拉长亮屏超时（有权限才生效，失败不影响执行）
        try:
            self._page.run_adb_shell(
                "settings put system screen_off_timeout 1800000")
        except Exception:  # noqa: BLE001
            pass

    def _start_app(self, log_prefix='[启动]'):
        """发启动命令并**确认 App 真的到了前台**；返回 (ok, 前台包名)。

        ⭐⭐⭐ 2026-10-29 真实事故（用例执行，不是 APK 回归）：
            [14:02:58] [复位] 复位失败（不影响执行，起点可能不一致）: ADB error ...
            [14:03:17] [入口清单] 当前页面可交互文字 14 个：
                      @轻练、LoveDay、@Lovin、心动日常、@LinkUp、360手机助手、
                      万年历、相机、ES文件浏览器、浏览器、设置、快捷搜索
            手机当时其实停在**系统桌面/应用列表**上（"相机/浏览器/设置"都是桌面图标）。
            旧实现里复位失败只记一行 warning 就继续跑，于是：
              第 1 步在**桌面上**点「右上角加号」→ 点到了第三方 App「@Lovin 心动日常」
              第 2 步点「关闭」→ 关掉那个 App，回到桌面
              第 3 步再点「加号」→ 又点开另一个 App（地图）
            用户看到的就是"执行时乱启动其他 App"。
            ⇒ 启动/复位失败**绝不能继续执行**，必须判失败并说清原因。
        """
        pkg = (getattr(self.app_config, 'package_name', '') or '').strip()
        if not pkg:
            return False, ''
        # 启动前先唤醒+解锁：锁屏时 am start 会直接失败（见 _wake_and_unlock）。
        if not getattr(self, '_woke', False):
            self._woke = True
            self._wake_and_unlock()
        activity = getattr(self.app_config, 'app_activity', '') or ''
        last_err = ''
        fg = ''
        for attempt in range(1, self.APP_LAUNCH_RETRIES + 1):
            try:
                if activity:
                    self._page.run_adb_shell("am start -n %s/%s" % (pkg, activity))
                else:
                    self._page.run_adb_shell(
                        "monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                self._log('%s 第 %d 次启动命令失败: %s',
                          log_prefix, attempt, last_err[:160])
                # 失败常见于锁屏期间，第二次尝试前再唤醒一次
                if attempt == 1:
                    self._wake_and_unlock()
            wait_ms = self.launch_wait_ms or 3000
            time.sleep(max(wait_ms, 3000) / 1000.0)
            fg = self._foreground_package()
            if not fg:
                # 取不到前台包名（dumpsys 无输出）时不硬判失败，
                # 以免在个别 ROM 上把正常启动误杀成失败。
                self._log('%s 无法读取前台包名，跳过启动确认（device 可能响应慢）',
                          log_prefix)
                return True, ''
            if fg == pkg or fg.startswith(pkg + '.'):
                self._log('%s 已确认应用到达前台：%s（第 %d 次）', log_prefix, fg, attempt)
                return True, fg
            self._log('%s 第 %d 次启动后前台仍是 %s，重试…', log_prefix, attempt, fg)
            time.sleep(self.APP_LAUNCH_RETRY_WAIT_S)
        return False, fg

    def _assert_app_foreground_or_fail(self, where='启动'):
        """启动失败/没到前台 ⇒ 抛异常终止本次执行（不再在桌面或别的 App 上乱点）。"""
        ok, fg = self._start_app(log_prefix='[%s]' % where)
        if ok:
            return
        pkg = (getattr(self.app_config, 'package_name', '') or '').strip()
        msg = ('无法把应用「%s」启动到前台（连续 %d 次后前台仍是 %s）。'
               '为避免在桌面/其它 App 上乱点，本次执行已终止。'
               '请检查：设备是否解锁并亮屏、包名与启动 Activity 是否正确、'
               'App 是否已安装。'
               % (pkg or '(未配置)', self.APP_LAUNCH_RETRIES, fg or '(未知)'))
        self._log('[启动] %s', msg)
        raise AssertionError(msg)

    def launch_app(self):
        """启动目标 App，并**确认它真的到了前台**。

        为什么不能只发 `am start` 就完事（2026-10-28 run 14 真实事故）：
        `am start` 返回成功只代表"启动请求被接受"，不代表目标 App 拿到了前台。
        那次实测 `mCurrentFocus` 一直是
        `com.netease.open.pocoservice/...TestActivity`（一个第三方测试工具），
        万年历在后台，于是：
          - 模型在 PocoService 界面上找「黄历」，当然找不到；
          - 截图判据看到画面一直在变（TestActivity 本身是动态画面），
            误判"点击生效"，把错误一路带到后续步骤；
          - `uiautomator dump` 被它阻塞，60s 超时。
        所以这里复用 `_assert_app_foreground_or_fail`：带重试地核对前台包名，
        确认不了就**直接判失败终止**，绝不在别人的 App 上乱点。
        """
        if self._page is None:
            self.connect()
        pkg = self.app_config.package_name
        activity = self.app_config.app_activity or ''
        self._assert_app_foreground_or_fail(where='启动应用')
        self._log("已启动应用 %s / %s（前台已确认）", pkg, activity)

    def reset_to_home(self):
        """把 App 复位到一致起点（探索与回放都用，保证"采样起点 == 回放起点"）。

        目的：回放按**固化时采到的绝对坐标**点击，隐含假设
          「执行起点页面 == 固化采样时的起点页面」。
          若两边起点不同 → 坐标整体偏掉 → 回放失败 → 降级烧 token
          → 用户以为"固化从没生效"。
          所以**探索和回放都必须先复位到同一起点**。

        ⚠️⚠️ **只做 `am start`，绝不 `am force-stop`**
        （2026-09-22 iOS 实测踩坑后定型，Android 同步该结论）：
          最初实现是"杀进程 + 冷启动"，以为能拿到"最干净的首启状态"。
          但实测该 App 冷启动会落到一个**没有主导航、也没有返回按钮的死路页**
          （iOS 侧是「日记本」页：`back` 手势连点两次元素树完全不变），
          首步固化坐标在此页会落到别的控件上 → 后续必然连锁失败。
          **杀进程恰恰会落到那个死路页，等于把 App 推进更糟的地方。**

          改为只用 `am start`/`monkey`：App 已在前台时通常只是"唤到前台"、
          保留原 Activity 栈，不在前台则冷启动。两种情况**都不由我们决定落在哪一页**，
          起点一致性交给用例自身保证（用例应从它期望的页面开始）。

        失败不抛异常：复位失败只是"起点可能不一致"，
        不能因为复位本身报错把用例直接搞挂。
        """
        if self._page is None:
            self.connect()
        pkg = self.app_config.package_name
        if not pkg:
            self._log('[复位] 未配置应用包名，跳过复位（起点可能不一致）')
            return
        # ⭐ 复位失败对用例执行是**致命**的，不能只记 warning。
        # 复位没成功 ⇒ App 不在前台 ⇒ 接下来的点击全部落在桌面或别的 App 上
        # （2026-10-29 实测：桌面上点「右上角加号」打开了 3 个不同 App）。
        # 复位成功后才允许继续；失败由 _start_app 抛出的异常向上冒泡，
        # 交给 run() 判整条用例失败。
        ok, fg = self._start_app(log_prefix='[复位]')
        if not ok:
            raise AssertionError(
                '复位失败：应用「%s」未能启动到前台（前台仍是 %s）。'
                '为避免在桌面/其它 App 上乱点，本次执行已终止。' % (pkg, fg or '(未知)'))
        self._log('[复位] 已拉起 %s（不清 Activity 栈，起点由用例自身保证）', pkg)

    # ---------------- Playground（自然语言调试） ----------------
    def playground_run(self, prompt: str, action: str = 'aiAct',
                       launch_app: bool = True) -> Dict[str, Any]:
        """
        Playground 单条自然语言调试：连真机执行一条自然语言操作并返回截图(base64)。
        用于验证 Midscene 自然语言操作方式，确认稳定后再沉淀为正式步骤。
        """
        # 坐标护栏放最前：纯文本校验，不连设备即可拦截写死坐标的 prompt（避免无意义真机连接）
        _guard_semantic_prompt(prompt, 'Playground')
        if self._page is None:
            self.connect()
        if launch_app:
            try:
                self.launch_app()
            except Exception as e:
                return {
                    'status': 'failed',
                    'error': '启动应用失败: %s' % e,
                    'logs': list(self.logs),
                    'screenshot_base64': None,
                }

        result = {
            'status': 'passed',
            'action': action,
            'prompt': prompt,
            'logs': [],
            'screenshot_base64': None,
            'error': None,
        }
        try:
            self._log("[Playground] action=%s | prompt=%s", action, prompt)
            if action == 'aiTap':
                self._page.ai_tap(prompt)
            elif action == 'aiInput':
                self._page.ai_input(prompt, '')
            elif action == 'aiQuery':
                data = self._page.ai_query(prompt)
                result['query_result'] = data
                self._log("[Playground] ai_query 返回: %s", data)
            elif action == 'aiAct':
                self._page.ai_action(prompt)
            else:
                raise ValueError("Playground 暂不支持 action: %s（仅 aiAct/aiTap/aiInput/aiQuery）" % action)

            # 执行后截图并转 base64 回传（绕开静态 media 服务，直接走 api 通道）
            # midscene v0.1.5 的 get_screenshot() 不接受路径参数，直接返回 base64 字符串
            import base64
            result['screenshot_base64'] = self._page.get_screenshot()
            self._log("[Playground] 已截图（base64 len=%d）", len(result['screenshot_base64'] or ''))
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            self._log("[Playground] 执行出错: %s", e)
            try:
                result['screenshot_base64'] = self._page.get_screenshot()
            except Exception:
                pass

        result['logs'] = list(self.logs)
        return result

    # ---------------- 步骤执行 ----------------
    def _resolve_business_refs(self, steps: List[dict]) -> List[dict]:
        """把 steps 里的 {ref: 动作名} 展开成完整 step dict（从 BusinessAction 加载）。

        兼容两种写法：
        - {ref: 'login'}：纯引用业务动作
        - {ref: 'login', value: 'xxx'}：引用并覆盖部分字段（如覆盖 value）
        无 ref 的内联 step（含 action/prompt）原样保留，向后兼容。
        """
        from apps.ui_automation.models import BusinessAction

        resolved: List[dict] = []
        for s in steps or []:
            if not isinstance(s, dict):
                resolved.append(s)
                continue
            # A step carrying "ref" is a REFERENCE and gets expanded.
            # The old condition also required "action" to be absent, which
            # silently kept a half-filled step (ref + leftover action) as-is
            # and executed nothing at all - hard to notice, harder to debug.
            ref = (s.get('ref') or '').strip() if isinstance(s.get('ref'), str) else s.get('ref')
            if ref:
                try:
                    action_obj = BusinessAction.objects.get(name=ref)
                except BusinessAction.DoesNotExist:
                    self._log("[警告] 公共步骤「%s」不存在，该步已跳过", ref)
                    resolved.append({
                        'name': '引用公共步骤 %s' % ref,
                        'action': 'screenshot',
                        'prompt': ref,
                        '_ref_missing': ref,
                        '_status': 'failed',
                        '_error': '公共步骤「%s」不存在' % ref,
                    })
                    continue

                merged = action_obj.to_step()
                # 允许内联覆盖（ref 之外传入的字段）
                overrides = [k for k in s.keys() if k not in ('ref',)]
                merged.update({k: v for k, v in s.items() if k not in ('ref',)})
                # 日志里展开引用内容，否则失败时不知道是哪个公共步骤的问题
                self._log("引用公共步骤「%s」-> %s: %s",
                          ref, merged.get('action'),
                          (merged.get('prompt') or '')[:120])
                if overrides:
                    self._log("  覆盖字段: %s", ', '.join(overrides))
                merged['_ref'] = ref
                merged['name'] = merged.get('name') or ref
                resolved.append(merged)
            else:
                resolved.append(s)
        return resolved

    #: 多行 prompt 自动拆分的开关（`STEP_EXPLODE_MULTILINE=0` 可关）。
    EXPLODE_MULTILINE = os.environ.get('STEP_EXPLODE_MULTILINE', '1') not in (
        '0', 'false', 'False', '')

    def _explode_multiline_steps(self, steps: List[dict]) -> List[dict]:
        """把"一个 aiAct 里塞了多行子步骤"拆成多个独立步骤。

        ⭐⭐⭐ 为什么要拆（2026-10-29 事故，record 65「日历模式切换」）：
            用户配的 steps[0] 是这样的（**一个 aiAct，prompt 含 10 行**）：
                点击日历网格中任意一个非今日日期
                验证底部日期详情面板显示该日期的宜忌信息
                ...
                点击「天气」模式
                验证日历主体区域刷新为天气模式视图
            aiAct 是"多轮自主循环"，模型会一口气把这 10 件事都试着做掉。
            结果第 9 件事（切天气）因为会员限制没成功 → 整步抛
            `Task failed: 天气模式切换未成功…可能受会员权限限制` →
            **报告里只看到「步骤 1 failed」，前面 8 件成功的事全被吞掉**，
            用户看到的是"我明明切换成功了却报失败"。
            同一份内容的另一条记录（不带附加约束）跑通了 —— 说明不是功能坏了，
            而是"全部押在一步里"这种结构让任何一环失败都必然整条失败。

            拆开之后：每行一个步骤、各自记 passed/failed，
            报告能精确指出"第 9 步（点天气模式）失败"，前面 8 步的结果也留得住。

        只拆 **action 为 aiAct / aiTap / aiInput** 且 prompt 含换行的步骤；
        拆分后按行内容**重新判定动作类型**（点击→aiTap、验证→assert、输入→aiInput），
        避免把"验证…"也当成动作去执行。

        ⭐ 第二轮（2026-10-29，用户要求"公共步骤也可以拆"）：
            `{ref: '启动 测试'}` 展开后的 prompt 是**一行里的多个动作**：
                「启动万年历 点击底部天气tab  点击左上角+ 进入城市管理页面 点击添加城市」
            这类没有换行、但明显是 4~5 件事的文本，现在也按**动作词**切开
            （见 `_split_action_clauses`），否则它同样会退化成"一挂全挂"。
            公共步骤拆出的子步骤带 `_ref` 标记，报告里能看出它们来自哪个公共步骤。
        """
        if not self.EXPLODE_MULTILINE:
            return steps
        out: List[dict] = []
        for s in (steps or []):
            if not isinstance(s, dict) or s.get('ref'):
                out.append(s)
                continue
            if s.get('action') not in ('aiAct', 'aiTap', 'aiInput'):
                out.append(s)
                continue
            prompt = str(s.get('prompt') or '')
            ref = s.get('_ref') or ''
            base = s.get('name') or (ref or '步骤')

            # ① 先按换行切
            lines = [ln.strip() for ln in re.split(r'[\r\n]+', prompt) if ln.strip()]
            # ② 单行但含多个动作词的，再按动作词切（公共步骤常见）
            if len(lines) == 1:
                clauses = self._split_action_clauses(lines[0])
                if len(clauses) > 1:
                    lines = clauses
            if len(lines) < 2:
                out.append(s)
                continue

            self._log('[拆分] %s「%s」含 %d 个子步骤，拆成独立步骤执行%s',
                      '公共步骤' if ref else '步骤', base, len(lines),
                      '（来源：公共步骤「%s」）' % ref if ref else '')
            for i, line in enumerate(lines):
                sub = dict(s)
                sub['name'] = '%s.%d' % (base, i + 1)
                sub['prompt'] = line
                # 子步骤不能继承父步骤的 selector_hint / 坐标 / 断言（那是父级的）
                for k in ('selector_hint', 'selector_position', 'center_x',
                          'center_y', 'assert', 'extract', 'locate_index',
                          'locate_region', 'locate_by_text', 'locate_element_id'):
                    sub.pop(k, None)
                sub['action'] = self._guess_action(line, s.get('action'))
                # 报告里显示"公共步骤：xxx"，不要只剩一个与公共步骤同名的条目
                if ref:
                    sub['_ref'] = ref
                    sub['_ref_index'] = i + 1
                    sub['_ref_total'] = len(lines)
                out.append(sub)
        return out

    #: 动作起始词。出现在句中时，认为"从这里开始是另一个动作"。
    #:
    #: ⚠️ 只收录「语义上一定开启一个新动作」的词：
    #:    - 不收「取消/关闭/开启」—— 「取消勾选」「关闭开关」里的它们是**宾语的一部分**，
    #:      当成切点会把「点击设置里的取消按钮」切成残片；
    #:    - 不收「查看/确认」—— 它们常做谓语，切开后语义不完整。
    #: 顺序无意义（用 finditer 按出现位置切）。
    _ACTION_VERB = re.compile(
        r'(?:点击|点按|单击|轻触|长按|输入|填写|进入|打开|切换到|返回|滑动|上滑|下滑|'
        r'左滑|右滑|拖拽|向上|向下|验证|检查|确保)')

    #: 拆分后每段的最短长度（太短的多半是虚词碎片，宁可不拆）
    MIN_CLAUSE_LEN = 4

    def _split_action_clauses(self, text: str, max_parts: int = 12):
        """把「A 点击B 进入C」这类**一行多动作**的文本按动作词切开。

        为什么需要（2026-10-29 用户要求）：
            公共步骤「启动 测试」的 prompt 是
                「启动万年历 点击底部天气tab  点击左上角+ 进入城市管理页面 点击添加城市」
            它被当成**一个 aiAct** 执行 —— 与"多行 prompt 塞一步"是同一类问题：
            任何一环失败整步就废，报告里也只显示一条。

        切分规则：
            - 以"动作词"为切点（点击/输入/进入/打开/返回/滑动/验证…）；
            - 只切**句中出现**的动作词（句首的算第一段开头，不切）；
            - 每段必须 ≥ MIN_CLAUSE_LEN 个字符，否则丢弃该切点；
            - 段数与原文相比没有增加 ⇒ 原样返回（不做无意义改写）。

        ⚠️ 保守原则：这是"尽量拆"，不是强制拆。宁可少拆几段让模型多跑一轮，
           也不要把一句话拆成语义残片（残片会让定位直接失败）。
        """
        t = str(text or '').strip()
        if not t:
            return [t]
        # 先把各种分隔符统一成空格，便于按词切
        t = re.sub(r'[，,；;、]\s*', ' ', t)
        hits = list(self._ACTION_VERB.finditer(t))
        if len(hits) <= 1:
            return [t]
        # 第一个动作词在句首（前面只有空白）⇒ 它是第一段的开头，不作为切点
        cuts = []
        for m in hits:
            if t[:m.start()].strip():
                cuts.append(m.start())
        if not cuts:
            return [t]
        bounds = [0] + cuts + [len(t)]
        parts = []
        for a, b in zip(bounds, bounds[1:]):
            seg = t[a:b].strip(' 　')
            if not seg:
                continue
            # 过短的片段（如单独的"并"、"再"）并入上一段，避免产生语义残片
            if len(seg) < self.MIN_CLAUSE_LEN and parts:
                parts[-1] = (parts[-1] + ' ' + seg).strip()
                continue
            parts.append(seg)
        if len(parts) < 2 or len(parts) > max_parts:
            return [t]
        if any(len(p) < self.MIN_CLAUSE_LEN for p in parts):
            return [t]
        return parts

    @staticmethod
    def _guess_action(line: str, default: str = 'aiAct') -> str:
        """按一行文字猜它该用什么 action（只在拆分时用，不改变用户显式配置）。"""
        t = (line or '').strip()
        if re.match(r'^(验证|确认|检查|断言|校验|确保)', t):
            return 'assert'
        if re.match(r'^(输入|填写|键入|录入)', t):
            return 'aiInput'
        if re.match(r'^(点击|点按|单击|点一下|轻触|进入|打开|切换到|滑动|返回|向上|向下)', t):
            return 'aiTap'
        return default

    def run(self, steps: List[dict]) -> Dict[str, Any]:
        """执行整条用例的 steps，返回执行结果摘要。"""
        if self._page is None:
            self.connect()
        # 展开业务动作引用（Prompt 与代码分离）
        steps = self._resolve_business_refs(steps)
        # 多行 prompt 拆成多个步骤（见 _explode_multiline_steps 的事故说明）
        steps = self._explode_multiline_steps(steps)
        # 空步骤必须显式失败：以前 action 会兜底成 aiAct、prompt 兜底成空串，
        # 拿着空提示词问视觉模型，模型随便点一下也返回成功 ——
        # 用例其实什么都没配，执行却显示「通过」。
        empty = [i for i, s in enumerate(steps)
                 if isinstance(s, dict) and not self._step_has_content(s)]
        if empty:
            bad = empty[0]
            msg = ('第 %d 个步骤是空的：既没有 action 也没有 prompt。'
                   '请到「用例编辑」里补全该步骤（或删除它）' % (bad + 1))
            self._log('%s', msg)
            return {
                'status': 'failed',
                'steps': [{'index': bad, 'name': '步骤%d' % (bad + 1),
                           'action': '', 'status': 'failed', 'error': msg}],
                'context': self.context,
                'error': msg,
            }
        result = {
            'status': 'passed',
            'steps': [],
            'context': {},
            'error': None,
        }
        try:
            # ⭐⭐ **两种模式都必须先复位到统一起点**（2026-09-22 与 iOS 侧对齐）。
            #
            # 为什么探索也要复位：固化采到的是**绝对坐标**，隐含假设
            # 「本次起点页面 == 固化采样时的起点页面」。
            # 若探索时从"任意状态"（App 停在上次留下的深层页面）开始采样，
            # 而回放从冷启动首页开始，两边起点不同 → 坐标整体偏掉
            # → 回放必失败 → 降级烧 token → 用户以为固化没用。
            # 规则简化为：**每次执行（探索/回放）都从同一个干净起点开始**。
            #
            # ⚠️ Android 必须 `am force-stop`：monkey/am start 对已在运行的 App
            #    只是"唤到前台"，不会重置 Activity 栈。
            self.reset_to_home()
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = '启动应用失败: %s' % e
            self._log("启动应用失败: %s", e)
            # ⚠️ 启动失败必须让**执行记录**也看到原因，而不是只留一条 engine log。
            # 这里是"在桌面/别的 App 上乱点"的总闸门：App 没到前台就绝不进步骤循环。
            return result

        # ⭐ 双保险：即使 reset_to_home 内部吞掉了异常（历史版本行为），
        # 也再确认一次 App 在前台。不在前台就直接终止，绝不进入步骤循环 ——
        # 2026-10-29 事故就是"复位失败只记 warning"，结果第 1 步在系统桌面上
        # 点「右上角加号」，连续打开了 @Lovin、地图等 3 个第三方 App。
        if self.FOREGROUND_GUARD:
            fg = self._foreground_package()
            pkg = (getattr(self.app_config, 'package_name', '') or '').strip()
            if fg and pkg and self._is_foreground_drift(fg):
                msg = ('启动后应用未在前台（当前前台：%s，目标：%s），'
                       '为避免在桌面/其它 App 上乱点，本次执行已终止。' % (fg, pkg))
                result['status'] = 'failed'
                result['error'] = msg
                self._log('%s', msg)
                return result

        # ⭐ 全局弹窗策略：启动后**立刻**清一轮，不等某一步失败。
        #
        # 为什么要有这一步（原来只在失败后补救）：
        # 冷启动是弹窗最密集的时刻（权限/隐私协议/开屏广告同时来）。
        # 事后补救的前提是"弹窗导致某一步失败"，但若弹窗只挡住页面一部分、
        # 当前步骤恰好还能过，它就会一直挂在那儿，等到某个倒霉步骤才失败，
        # 报告里表现为"莫名其妙的失败"。开跑先清一次成本仅一次模型调用。
        if self.auto_dismiss_popup and self.popup_precheck:
            try:
                n = dismiss_popups(self._popup_adapter())
                if n:
                    self._log('[弹窗] 执行前已清理 %d 个弹窗', n)
                else:
                    self._log('[弹窗] 执行前预检：未发现弹窗')
            except Exception as exc:  # noqa: BLE001
                # 预检失败绝不能影响执行：留着弹窗最多让某步失败，
                # 而这里抛异常会导致整条路径起不来。
                self._log('[弹窗] 执行前预检失败（不影响执行）: %s', exc)

        def _is_dead_session_error(err):
            """设备会话是否已彻底失效（继续跑每步都会瞬间失败）。

            与 apk_workflow._SESSION_DEAD_MARKERS 保持同一口径，
            这里单独写一份是为了不引入跨模块依赖（workflow 依赖 engine，
            反向 import 会成环）。
            """
            text = str(err or '').lower()
            return any(m in text for m in (
                'has been destroyed and cannot execute',
                'androiddevice destroyed',
                'device has been disconnected',
                'adb: device offline',
                'device not found',
                'session not found',
                'unable to connect to device',
            ))

        total = len(steps)
        failed_step = None
        for idx, step in enumerate(steps):
            step_started = time.time()
            step_result = self._run_step(step, idx)

            # 随机弹窗（权限请求 / 隐私协议 / 开屏广告 ……）随时可能冒出来，
            # 一旦挡住目标元素这一步就会失败。这里做「关弹窗 + 重试」，
            # 正常路径（没弹窗）零额外开销。
            # 断言步骤上限单独放宽，理由同 iOS 引擎（弹窗检测与断言各自调模型、
            # 结论可能矛盾，多试几次能让广告页自愈）。
            limit = (ASSERT_POPUP_RETRY_LIMIT
                     if step.get('action') == 'assert' else POPUP_RETRY_LIMIT)
            try_count = 0
            while (step_result.get('status') == 'failed'
                   and self.auto_dismiss_popup
                   and try_count < limit):
                try_count += 1
                self._log('[步骤 %d] 失败，检测是否为弹窗干扰（第 %d/%d 次重试）...',
                          idx + 1, try_count, limit)
                if dismiss_popups(self._popup_adapter()) == 0:
                    self._log('[步骤 %d] 未发现弹窗，不再重试', idx + 1)
                    break
                step_result = self._run_step(step, idx)
                if step_result.get('status') != 'failed':
                    self._log('[步骤 %d] 关闭弹窗后重试成功', idx + 1)
                    break

            # 每步结束都打一条带耗时与进度的日志。
            # 为什么必须打：一条 aiAct 可能跑 1~2 分钟，若只在失败时输出，
            # 用户在中途完全看不到进展，只能看着真机"没反应"瞎猜。
            _elapsed = round(time.time() - step_started, 1)
            _ok = step_result.get('status') != 'failed'
            self._log('  %s [%d/%d] %s（%.1fs）%s',
                      '✓' if _ok else '✗', idx + 1, total,
                      step_result.get('name') or ('步骤%d' % (idx + 1)),
                      _elapsed,
                      '' if _ok else ('| %s' % str(step_result.get('error') or '')[:200]))

            result['steps'].append(step_result)
            if step_result.get('status') == 'failed':
                # 记录首个失败（整条路径最终判 failed），但**不中断后续步骤**。
                #
                # 为什么不再 break（2026-10-28 用户反馈）：
                # 之前任一步失败就直接跳到下一条路径，导致"后面几步根本没跑过"
                # —— 报告只告诉你第 3 步挂了，却不告诉你第 4~10 步是否也坏，
                # 而实际上后续步骤往往能独立跑通，那部分信息白丢了。
                # 现在改成"失败也继续，最后统一判定"，报告里能看到完整覆盖情况。
                #
                # 例外：设备会话已死（AndroidDevice destroyed / Session not found）
                # 时继续毫无意义 —— 每一步都会瞬间失败，还白烧模型调用，直接停。
                if not failed_step:
                    failed_step = step_result
                result['status'] = 'failed'
                if not result.get('error'):
                    result['error'] = step_result.get('error')

                # ⭐ 锁屏/息屏：跑到一半手机锁了，后续点击同样会落在锁屏上，
                # 而且 am start 会失败 ⇒ 与"会话已死"一样继续毫无意义。
                # 尝试唤醒一次；唤不回来就停止（绝不带着锁屏继续点）。
                if self.AUTO_WAKE_DEVICE and self._looks_like_sleep(
                        step_result.get('error')):
                    awake, locked = self._device_status()
                    if awake is False or locked is True:
                        self._log('[步骤 %d] 检测到设备锁屏/息屏，尝试唤醒…', idx + 1)
                        self._wake_and_unlock()
                        ok, fg2 = self._start_app(log_prefix='[唤醒后重启]')
                        awake2, locked2 = self._device_status()
                        if not ok or awake2 is False or locked2 is True:
                            msg = ('设备在步骤 %d 时锁屏/息屏且无法自动解锁'
                                   '（亮屏=%s 锁屏=%s），为避免在锁屏/桌面上乱点，'
                                   '本次执行已终止。请关闭设备自动锁屏或调长亮屏时间。'
                                   % (idx + 1, awake2, locked2))
                            self._log('%s', msg)
                            result['status'] = 'failed'
                            result['error'] = msg
                            break
                        self._log('[步骤 %d] 已恢复前台 %s，继续执行', idx + 1, fg2)

                _dead = _is_dead_session_error(step_result.get('error'))
                if _dead:
                    self._log('[步骤 %d] 设备会话已失效，后续 %d 步不再执行'
                              '（继续跑只会每步瞬间失败）',
                              idx + 1, total - idx - 1)
                    result['error'] = '%s（后续步骤因设备会话失效未执行）' % (
                        result.get('error') or '')
                    break
                if idx + 1 < total:
                    self._log('[步骤 %d] 失败，继续执行后续 %d 步（失败不中断路径）',
                              idx + 1, total - idx - 1)

        result['context'] = self.context
        if failed_step:
            # 汇总：让报告一眼看出"哪几步失败"，而不是只看到第一条错误。
            _failed_list = [s for s in result['steps'] if s.get('status') == 'failed']
            if len(_failed_list) > 1:
                self._log('[汇总] 本路径共 %d/%d 步失败：%s',
                          len(_failed_list), len(result['steps']),
                          '、'.join('第%d步' % (s.get('index', 0) + 1)
                                   for s in _failed_list))
        return result

    def _popup_adapter(self):
        """把本引擎的能力包成 popup_handler 需要的接口。"""
        page = self._page
        return _AndroidPopupAdapter(page, self._log)

    @staticmethod
    def _step_has_content(step: dict) -> bool:
        """步骤是否可执行：引用步骤看 ref，内联步骤看 action/prompt。"""
        if str(step.get('ref') or '').strip():
            return True
        if str(step.get('action') or '').strip():
            return True
        return bool(str(step.get('prompt') or '').strip())

    def _run_step(self, step: dict, idx: int) -> Dict[str, Any]:
        action = step.get('action', 'aiAct')
        name = step.get('name', '步骤%d' % (idx + 1))
        raw_prompt = step.get('prompt', '')
        prompt = _resolve_template(raw_prompt, self.context)
        value = _resolve_template(step.get('value', ''), self.context)
        wait_ms = int(step.get('wait_ms', 0) or 0)

        # 坐标护栏：自然语言操作/定位类步骤禁止写死坐标（launch/back/home/screenshot/wait 无 prompt 不受影响）
        _guard_semantic_prompt(prompt, '步骤%d' % (idx + 1))
        # selector_hint 也用于定位，同样拦截坐标写法（aiTap/aiInput 优先用 selector_hint）
        if step.get('selector_hint'):
            _guard_semantic_prompt(step.get('selector_hint'), '步骤%d.selector_hint' % (idx + 1))

        # ⭐ 条件句归一化 + 「不许离开 App」约束（见 _neutralize_conditional /
        # _STAY_IN_APP_RULE 的事故说明）。只作用于会驱动模型动作的步骤，
        # 断言类不改写（断言本来就是"判断"，没有"跳去找"的语义）。
        #
        # ⚠️ 约束只在**确实涉及 App 内跳转**时才追加：无条件给每个 aiAct 都挂一句
        # 长约束，既会把简单步骤变难（模型要去理解一堆它用不上的规矩），
        # 也更容易触发出"没达到就放弃"的误读（record 65 的事故）。
        if action in ('aiTap', 'aiAct', 'aiInput') and prompt:
            neutralized = _neutralize_conditional(prompt)
            if neutralized != prompt:
                self._log("[步骤 %d] 条件句已归一化（避免模型离开 App 搜索元素）：%s",
                          idx + 1, neutralized[:120])
                prompt = neutralized
            if (_looks_like_app_switch(prompt)
                    and _STAY_IN_APP_RULE.strip('（重要：）') not in prompt):
                prompt = prompt + _STAY_IN_APP_RULE
        self._log("[步骤 %d] %s | action=%s | prompt=%s", idx + 1, name, action, prompt)
        step_result = {
            'index': idx,
            'name': name,
            'action': action,
            'prompt': prompt,
            'status': 'passed',
            'extract': {},
            'assert': None,
            'error': None,
        }

        try:
            if action == 'launch':
                # 幂等：run() 在开跑前已经 reset_to_home 拉起过 App，
                # 再执行一次 monkey/am start 会让"启动"这一步在手机上演两遍
                # （用户看到的就是"为什么每个步骤都在重复同样的操作"）。
                # 只有确实不在前台时才真正拉起。
                fg0 = self._foreground_package()
                pkg0 = (self.app_config.package_name or '').strip()
                if pkg0 and fg0 and (fg0 == pkg0 or fg0.startswith(pkg0 + '.')):
                    self._log("[步骤 %d] App 已在前台（%s），跳过重复启动", idx + 1, fg0)
                else:
                    self.launch_app()
            elif action == 'back':
                self._page.ai_action("返回上一页")
            elif action == 'home':
                self._page.run_adb_shell("input keyevent KEYCODE_HOME")
            elif action == 'screenshot':
                # midscene v0.1.5 的 get_screenshot() 不接受路径参数，直接返回 base64 字符串
                step_result['screenshot'] = self._page.get_screenshot()
            elif action == 'wait':
                time.sleep(wait_ms / 1000.0)
            elif action == 'aiTap':
                # 纯坐标回放：直接用固化坐标点，不调模型
                if self.replay_mode and self._step_has_coords(step):
                    self._replay_tap(step)
                else:
                    # 确定性点击：target 来自 selector_hint 或 prompt
                    target = step.get('selector_hint') or prompt
                    position = step.get('selector_position') or ''
                    before = self._safe_shot()
                    # 点击**前**先取两个基准：
                    #   ① 前台包名 —— 判断"是不是跳到了别的 App/Activity"
                    #   ② 页面主体文字指纹 —— 判断"页面内容有没有真的换"
                    # 顺序放在 ai_tap 之前，否则取到的已是点击后的状态。
                    step['_fg_before'] = self._foreground_package()
                    step['_sig_before'] = self._page_text_signature()
                    # 把"当前页面上有哪些可交互入口"作为提示喂给模型。
                    # 它仍然自己看截图定位，但知道屏幕文字确切叫什么，
                    # 不必靠猜（实测模型对「底部黄历」这种描述会定位失败）。
                    tap_ok = True
                    try:
                        self._page.ai_tap(self._with_entry_hint(target, position))
                        self._count_model_use(prompt)
                        # ⭐ 点击后校验页面是否真的变了。
                        #
                        # 为什么必须有：`ai_tap` 只要 RPC 没抛异常就返回成功，
                        # **点空、点到不可点区域、定位到错误元素但坐标落在空白处，
                        # 都不会报错**。2026-10-28 实测「点击底部黄历Tab」返回 ✓ 仅用
                        # 4.8s，但手机根本没跳页 —— 假成功一路带到后面步骤才暴露。
                        self._verify_tap_effect(step, before, target, idx)
                    except AssertionError as exc:
                        # 视觉定位失败 —— 若目标是底部导航项，先试坐标兜底。
                        # 只在**确认没切页**之后才兜底，绝不无条件盲点。
                        tap_ok = False
                        self._log('[步骤 %d] 视觉点击未生效：%s', idx + 1,
                                  str(exc)[:120])
                        if not self._tab_tap_by_coords(target):
                            raise
                        self._entry_cache = None
                        # 坐标兜底后用**截图差异**单独判一次。
                        # 不能复用 _verify_tap_effect：那里的文字指纹基准是"视觉点击前"
                        # 取的，而这条路上 a11y 会返回缓存（实测切页后指纹纹丝不动），
                        # 再加上它有"导航项直接抛错"的短路逻辑，复用会立刻误判失败。
                        time.sleep(0.8)
                        ratio = self._shot_diff_ratio(before, self._safe_shot())
                        if ratio < self.TAP_EFFECT_MIN_DIFF:
                            raise AssertionError(
                                '点击「%s」未生效：视觉定位失败后已改用坐标兜底，'
                                '画面仍无变化（%.1f%%）' % (target, ratio * 100))
                        self._log('[坐标兜底] 画面变化 %.1f%%，判定点击生效',
                                  ratio * 100)
                        tap_ok = True
            elif action == 'aiInput':
                if self.replay_mode and self._step_has_coords(step):
                    self._replay_input(step, value)
                else:
                    self._page.ai_input(step.get('selector_hint') or prompt, value)
                    self._count_model_use(prompt)
            elif action == 'aiAct':
                if self.replay_mode and self._step_has_coords(step):
                    self._replay_tap(step)
                else:
                    self._page.ai_action(prompt)
                    self._count_model_use(prompt)
            elif action == 'aiQuery':
                # 纯查询（不改动页面），结果存入 extract 或返回
                data = self._page.ai_query(prompt)
                self._count_model_use(prompt)
                step_result['extract']['_query'] = data
            elif action == 'aiWaitFor':
                # 条件等待：等目标元素/文案出现再继续。解决"页面加载慢、
                # 元素还没渲染出来"导致的偶发失败（比固定 sleep 更省时间，
                # 条件一成立立刻返回）。
                self._wait_for(prompt, step.get('timeout_ms'))
                self._count_model_use(prompt)
            elif action == 'assert':
                ok = self._do_assert(step)
                step_result['assert'] = {'pass': ok}
                if not ok:
                    raise AssertionError(step.get('assert', {}).get('expect', '断言未通过'))
            else:
                raise ValueError("不支持的 action: %s" % action)

            # extract：从页面提取业务数据（业务锚点），写入 context
            if step.get('extract'):
                extracted = self._do_extract(step['extract'])
                step_result['extract'].update(extracted)
                self.context.update(extracted)

            # assert（独立字段，非 action=assert 时）
            if step.get('assert') and action != 'assert':
                ok = self._do_assert(step)
                step_result['assert'] = {'pass': ok}
                if not ok:
                    raise AssertionError(step['assert'].get('expect', '断言未通过'))

            if wait_ms and action not in ('wait',):
                time.sleep(wait_ms / 1000.0)

            # ⭐ 前台包名守卫：执行完再确认"还在目标 App 里"。
            # 放在成功路径上而不是只放在 except 里 —— 因为"点着点着跳到了浏览器/
            # 京东/系统设置"这件事，midscene 会**当作成功返回**（它确实点到了），
            # 不主动校验就会一路 passed 把错误带下去。见 _guard_foreground 说明。
            if action in ('aiTap', 'aiAct', 'aiInput', 'back', 'home'):
                try:
                    self._guard_foreground('步骤 %d（%s）' % (idx + 1, name))
                except AssertionError as drift:
                    step_result['status'] = 'failed'
                    step_result['error'] = str(drift)
                    step_result['left_app'] = True
                    self._log('[步骤 %d] %s', idx + 1, drift)
                    return step_result

            # 每步都留一张实拍截图，报告里才能"步骤 ↔ 画面"一一对应。
            # 之前只有显式 screenshot 动作才截、失败才截，成功用例报告里一张图都没有。
            if action != 'screenshot':
                try:
                    step_result['screenshot'] = self._page.get_screenshot()
                except Exception as exc:  # noqa: BLE001
                    self._log("[步骤 %d] 截图失败: %s", idx + 1, exc)

        except Exception as e:
            # ⭐ aiTap 定位失败 → 降级 aiAct 重试一次（2026-10-29 实测根因）。
            #
            # 为什么必须降级：aiTap 是「单轮定位」，它**只在 UI 元素树里按文字匹配
            # 可交互节点**，不看截图。而很多 App 的底部 Tab / 图标按钮是自定义绘制，
            # 实测万年历 5 个底部 Tab 全是：
            #     text='天气'  clickable=false  bounds=[0,0][0,0]   ← 文字节点零面积
            #     真正可点的是 4 个无文字的 FrameLayout
            # ⇒ aiTap 永远点不中，报 "failed to locate element"，
            #   而模型在报错里自己都说了"截图底部导航栏可见「天气」入口"。
            # aiAct 是「看截图 → 规划 → 点击 → 再确认」的多轮循环，能按视觉坐标点中。
            #
            # 只降级「定位失败」这一种：其它失败（如坐标护栏拦截、会话失效）
            # 换 aiAct 也解决不了，白烧一次模型调用。
            if action == 'aiTap' and _is_locate_failure(e):
                self._log("[步骤 %d] aiTap 未在元素树中命中（%s），降级 aiAct 重试",
                          idx + 1, str(e).splitlines()[0][:120])
                try:
                    target = step.get('selector_hint') or prompt
                    before2 = self._safe_shot()
                    self._page.ai_action(_tap_intent_prompt(target, prompt))
                    self._count_model_use(prompt)
                    step_result['degraded_from'] = 'aiTap'
                    # ⚠️ 降级后同样要校验"画面是否真的变了"：
                    # ai_action 也可能"执行完但没点到"，不校验就会把
                    # 又一次假成功写进报告（这正是 aiTap 假成功的翻版）。
                    # 这里只做**判定**不做重试：aiAct 本身已是多轮自主执行，
                    # 再叠一层重试会让耗时翻倍。
                    if before2 is not None:
                        time.sleep(0.6)
                        ratio = self._shot_diff_ratio(before2, self._safe_shot())
                        self._log("[步骤 %d] 降级后画面变化 %.1f%%", idx + 1, ratio * 100)
                        if ratio < self.TAP_EFFECT_MIN_DIFF:
                            raise AssertionError(
                                '点击「%s」降级 aiAct 执行后页面仍未变化（%.1f%%），'
                                '可能该入口不可点或描述不符' % (target, ratio * 100))
                    self._entry_cache = None
                    step_result['status'] = 'passed'
                    step_result['error'] = None
                    self._log("[步骤 %d] 降级 aiAct 成功", idx + 1)
                except Exception as e2:  # noqa: BLE001
                    step_result['status'] = 'failed'
                    step_result['error'] = str(e2)
                    step_result['degraded_from'] = 'aiTap'
                    self._log("[步骤 %d] 降级 aiAct 也失败: %s", idx + 1, e2)
            else:
                step_result['status'] = 'failed'
                step_result['error'] = str(e)
                self._log("[步骤 %d] 失败: %s", idx + 1, e)

            if step_result['status'] == 'failed':
                # 失败时截图留存现场
                try:
                    step_result['fail_screenshot'] = self._page.get_screenshot()
                except Exception:  # noqa
                    pass

        return step_result

    # ---------------- 页面入口清单（给模型"提前知道有哪些入口"） ----------------
    #: 是否从 UI 层级提取入口清单喂给模型（默认开）。
    #: 关掉可用 PAGE_ENTRY_HINT=0。
    USE_ENTRY_HINT = os.environ.get('PAGE_ENTRY_HINT', '1') not in ('0', 'false', 'False', '')

    #: 入口清单缓存有效期（秒）。uiautomator dump 每次要几百毫秒 + 105KB XML，
    #: 每次点击都跑一遍会把执行拖慢；页面通常不会秒级变化，缓存几秒足够。
    ENTRY_CACHE_TTL = 5.0

    def _current_page_entries(self, limit=40):
        """从当前页面 UI 层级里提取**可交互入口的文本清单**（不含坐标）。

        为什么不给坐标（用户 2026-10-28 明确指出）：
          1. 实测底部 Tab 的 `bounds` 全是 `[0,0][0,0]` —— 真正可点的区域在父容器上，
             文本节点本身没有有效坐标，拿它去点必然点空；
          2. 坐标随机型/分辨率/系统版本漂移，写死就不可移植。
        所以只给**文本**，让模型自己看截图按语义定位。它有了确切文字作参照，
        就不必再猜"我这句描述和屏幕上哪个元素对应"。

        Returns:
            list[str]  去重后的入口文字，最多 limit 个
        """
        if not self.USE_ENTRY_HINT or not self._page:
            return []
        now = time.time()
        cached = getattr(self, '_entry_cache', None)
        if cached and now - cached[0] < self.ENTRY_CACHE_TTL:
            return cached[1]
        try:
            xml = self._page.run_adb_shell(
                'uiautomator dump /sdcard/_ms_ui.xml >/dev/null 2>&1; '
                'cat /sdcard/_ms_ui.xml')
        except Exception as exc:  # noqa: BLE001
            self._log('[入口清单] 获取 UI 层级失败（不影响执行）: %s', exc)
            return []
        if not isinstance(xml, str) or '<node' not in xml:
            return []
        out, seen = [], set()
        # 只取有 text 或 content-desc 的节点 —— 那就是"人能看到的名字"
        for m in re.finditer(
                r'<node[^>]*?(?:text="([^"]+)"|content-desc="([^"]+)")[^>]*?>', xml):
            name = (m.group(1) or m.group(2) or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= limit:
                break
        self._entry_cache = (now, out)
        return out

    #: 底部导航栏的常见项名。命中这些名字时，提示词会额外说明
    #: "这是一排导航项中的第 N 个"，让模型靠**兄弟项的相对位置**锁定，
    #: 而不是把一个孤立词丢给视觉模型去猜。
    #:
    #: 为什么需要（2026-11-02 真机实测）：
    #:   同一个「天气」Tab，给这些描述的效果天差地别 ——
    #:     `天气`            -> 失败
    #:     `底部「天气」Tab`  -> 失败
    #:     `天气 Tab`        -> 失败
    #:     `底部导航栏中最右侧区域的天气标签，灰色云朵图标下方写着天气` -> 成功
    #:   结论不是"模型看不见"，而是**孤立词信息量不足**：页面里有多处「天气」
    #:   （天气日历、16°阴…），短描述会指向别处或直接放弃。
    #:   把同一排的兄弟项一起告诉它，它就能靠排序定位。
    NAV_ITEM_NAMES = frozenset((
        '万年历', '黄历', '天气', '会员', '工具', '择吉日', '我的',
        '首页', '发现', '消息', '设置', '推荐', '关注', '视频', '直播',
        '日历', '日程', '待办', '笔记', '统计',
    ))

    def _nav_siblings(self, target, entries):
        """若 target 属于底部导航类的项，返回同一排的兄弟项（按清单顺序）。

        Returns:
            list[str]  兄弟项；不属于导航类或兄弟不足 2 个时返回空列表。
        """
        if target not in self.NAV_ITEM_NAMES:
            return []
        sibs = [e for e in entries if e in self.NAV_ITEM_NAMES]
        return sibs if len(sibs) >= 2 else []

    def _with_entry_hint(self, target, position=''):
        """把入口清单拼进点击描述，重点是把**位置与兄弟项**讲清楚。

        只在清单确实非空且数量合理时拼；拼不上就原样返回，绝不因此让点击失败。

        `position` 只作为"同文字出现多处时选哪个"的方位线索**单独陈述**，
        绝不和元素文字拼成一个词 —— 拼起来就是 `底部黄历` 这种屏幕上不存在的名字，
        模型必然 `Element not found`（2026-10-28 事故）。

        2026-11-02 补充：仅仅"告诉它文字是什么"是不够的（实测三种短描述全失败）。
        现在额外给出 ①位置 ②同一排的兄弟项及其顺序 ③它是第几个，
        让模型用相对关系定位，不再依赖对单个词的视觉联想。
        """
        entries = self._current_page_entries()
        if not entries:
            return target
        listing = '、'.join(entries)
        self._log('[入口清单] 当前页面可交互文字 %d 个：%s',
                  len(entries), listing[:300])

        exact = target in entries
        pos_note = ('该元素位于页面%s区域，' % position) if position else ''

        # ---- 导航类项：给出兄弟项与序号，这是实测唯一稳定的写法 ----
        #
        # ⚠️ 这里**绝不附加全量清单**（2026-11-02 真机对比）：
        #   失败版：4 行、200+ 字，后面拖着 35 个无关文字
        #           （京东 / 红果免费短剧 / 北京笔墨留香科技有限公司 / 63MB …）
        #   成功版：`底部导航栏中最右侧区域的天气标签，灰色云朵图标下方写着天气`
        #   结论：**短、只讲这一排**才点得中；无关文字会把关键信息淹没。
        siblings = self._nav_siblings(target, entries)
        if siblings:
            idx = siblings.index(target) + 1
            sib_text = '、'.join(siblings)
            # 位置词本身就是"底部"时，不要再拼成"页面底部的底部导航栏"
            if position in ('底部', '顶部'):
                where = ''
            elif position:
                where = '页面%s的' % position
            else:
                where = ''
            if exact:
                return ('在%s底部导航栏（一排并列的图标按钮）中点击「%s」。'
                        '这一排从左到右共 %d 项：%s；「%s」是第 %d 项。'
                        '只需定位这一排的第 %d 项并点击，不要在其它的文字上找。'
                        % (where, target, len(siblings), sib_text,
                           target, idx, idx))
            return ('在%s底部导航栏中点击「%s」，它应与 %s 并排出现。'
                    '只需在这一排图标按钮里定位「%s」。'
                    % (where, target, sib_text, target))

        # ---- 非导航项：精确命中时明确"就是这个名字" ----
        if exact:
            return ('需要点击的元素文字是「%s」，%s它与当前页面上的其中一个可交互文字完全一致，'
                    '请直接定位并点击该文字对应的元素。\n'
                    '（当前页面可交互元素的文字依次为：%s）'
                    % (target, pos_note, listing))
        return ('需要点击的元素文字是「%s」，%s请在下列可交互文字中找到与之对应的元素并点击；'
                '若确实没有对应项，说明本页没有该入口。\n'
                '（当前页面可交互元素的文字依次为：%s）'
                % (target, pos_note, listing))

    # ---------------- 底部 Tab 坐标兜底 ----------------
    #
    # 为什么需要（2026-11-02 真机实测，万年历 RMX2117）：
    #   底部导航 Tab 的 `ai_tap` 视觉定位**必失败**，提示词怎么改都没用：
    #     `天气` / `底部「天气」Tab` / `天气 Tab` / 带兄弟项的长描述 —— 全部点不中，
    #     校验日志稳定是「页面文字保留 100%」，最后靠降级 aiAct 才成功（耗时 70s）。
    #   但同一位置用 `adb shell input tap 540 2270` **一次即中**。
    #   ⇒ 不是模型看不见，而是这类虚拟布局节点（dump 里 bounds 全为 [0,0][0,0]）
    #     拿不到有效 bbox，视觉定位这条路对它天然不通。
    #
    # 适用范围**严格限定**：只有当
    #   ① 目标是已知的底部导航项名（NAV_ITEM_NAMES）
    #   ② 页面里能数出 >=2 个同类导航项（确认这一排确实存在）
    #   ③ 用户/用例没给坐标（不覆盖人为指定）
    # 时才启用。其它任何位置一律走原视觉路径，不做坐标猜测。
    #
    #: 底部导航栏中心线占屏高的比例。
    #: 实测（1080x2400）可点区间是 y∈[2200, 2340]，中心 2270 = 94.6%。
    #: ⚠️ 比例不能取更高：>=2360 落在系统手势导航区，点了完全无效。
    TAB_ROW_RATIO = 0.946

    def _screen_size(self):
        """读屏幕分辨率 (W, H)；失败返回 None。"""
        if self._page is None:
            return None
        try:
            out = str(self._page.run_adb_shell('wm size') or '')
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r'(\d+)x(\d+)', out)
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))

    def _tab_tap_by_coords(self, target):
        """按「均分导航栏」推算 target 的坐标并点击。

        Returns:
            True  已点击（**是否真的切页由调用方校验**，这里不做判断）
            False 前置条件不满足，未点击

        横向：x = W * (i + 0.5) / n  —— 实测 5 等分下 5/5 全部命中正确页。
        纵向：H * TAB_ROW_RATIO。
        """
        if target not in self.NAV_ITEM_NAMES:
            return False
        entries = self._current_page_entries()
        siblings = self._nav_siblings(target, entries)
        if not siblings:
            # 数不出这一排 ⇒ 不确定是不是底部导航，别猜坐标
            return False
        # target 必须在当前这一排里。`设置`/`我的` 等名字也在 NAV_ITEM_NAMES 中，
        # 但它们可能是页面内的普通入口（不是底部 Tab）—— 此时绝不能按序号盲点。
        if target not in siblings:
            return False
        size = self._screen_size()
        if not size:
            return False
        w, h = size
        n = len(siblings)
        idx = siblings.index(target)
        cx = int(w * (idx + 0.5) / n)
        cy = int(h * self.TAB_ROW_RATIO)
        self._log('[坐标兜底] 底部导航共 %d 项，点第 %d 项「%s」 -> (%d, %d)',
                  n, idx + 1, target, cx, cy)
        try:
            self._page.run_adb_shell('input tap %d %d' % (cx, cy))
            return True
        except Exception as exc:  # noqa: BLE001
            self._log('[坐标兜底] 点击失败: %s', exc)
            return False

    # ---------------- 点击生效校验 ----------------
    def _safe_shot(self):
        """取当前截图（base64），失败返回 None。取不到就不做校验，不能因此中断。"""
        try:
            return self._page.get_screenshot() if self._page else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _shot_diff_ratio(a, b):
        """粗略比较两张 base64 截图是否明显不同。

        为什么不用完整像素比对：截图是 base64 字符串，解码+逐像素在 Python 里
        太慢（1080x2400 一次几十毫秒到几百毫秒，每步都做会拖垮执行）。
        这里用「长度差 + 采样字符差异」快速估计 —— 对"页面是否跳转"这个判断
        完全够用：真跳页时整屏内容都变了，base64 差异会非常显著；
        而纯静态页面（只有时钟/动画微动）差异很小。

        Returns:
            float 0~1，越大差异越明显。任一张取不到时返回 1.0（视为有变化，不误报）。
        """
        if not a or not b:
            return 1.0
        if a == b:
            return 0.0
        la, lb = len(a), len(b)
        base = max(la, lb) or 1
        len_ratio = abs(la - lb) / base
        # 采样比较：每隔 step 取一个字符，统计不同比例（避免逐字符全量比较）
        step = max(1, min(la, lb) // 800)
        n = min(la, lb) // step
        if n <= 0:
            return 1.0
        diff = 0
        for i in range(n):
            if a[i * step] != b[i * step]:
                diff += 1
        char_ratio = diff / n
        # 长度差通常比字符差更敏感（页面跳转时 PNG 体积明显变化）
        return max(len_ratio, char_ratio)

    #: 点击前后画面差异低于该阈值 ⇒ 判定"点击没生效"。
    #:
    #: ⚠️ 这个阈值**不能单独用作判据**（2026-10-28 run 14 实测）：
    #: 万年历首页的时钟每秒跳、宜忌/每日一言轮播，同一页前后两次截图差异
    #: 恒 > 2%，于是"点了黄历"那一步拿到 98.2% 直接判生效，实际页面没动。
    #: 现在截图差异只作为**辅助信号**，主判据见 `_page_text_signature`。
    TAP_EFFECT_MIN_DIFF = 0.02

    #: 页面主体文字指纹：当前页面的可见文字集合（排除固定导航项）。
    #:
    #: 为什么用文字而不是像素：文字集合对"时钟跳动 / 轮播 / 动画"免疫
    #: （`14:11`→`14:12` 只差一项，但 Tab 切换会整体换掉一大半），
    #: 而像素对任何动态元素都敏感 —— 两者的抗噪性正好相反。
    #:
    #: ⚠️ **不能按 y 坐标排除底部导航区**（2026-11-02 真机实测）：
    #: 万年历底部 5 个 Tab 在 uiautomator 里的 bounds 全是 `[0,0][0,0]`
    #: （虚拟布局节点，没有真实坐标）。按比例切分会把它们当成"正文"，
    #: 于是"切页前后 Tab 不变"反而把相似度拉高，导致切换页面被判成没换页。
    #: 改为**按内容过滤**：固定导航项从集合里剔除，与坐标无关。
    PAGE_SIGNATURE_NAV_LABELS = {
        '万年历', '黄历', '天气', '会员', '工具', '择吉日', '我的',
        '首页', '发现', '消息', '设置', '推荐', '关注', '视频', '直播',
    }

    def _page_text_signature(self):
        """取当前页面主体区域的文字集合（用于判断页面是否真的换了）。

        Returns:
            set[str]；取不到时返回空集合（调用方据此退回截图判据）。
        """
        if self._page is None:
            return set()
        try:
            xml = self._page.run_adb_shell(
                'uiautomator dump /sdcard/_ms_sig.xml >/dev/null 2>&1; '
                'cat /sdcard/_ms_sig.xml')
        except Exception as exc:  # noqa: BLE001
            self._log('[点击校验] 取页面层级失败（退回截图判据）: %s', exc)
            return set()
        if not isinstance(xml, str) or '<node' not in xml:
            return set()

        out = set()
        for m in re.finditer(r'<node[^>]*?/?>', xml):
            tag = m.group(0)
            text = re.search(r'text="([^"]*)"', tag)
            desc = re.search(r'content-desc="([^"]*)"', tag)
            name = ((text.group(1) if text else '') or
                    (desc.group(1) if desc else '')).strip()
            if not name:
                continue
            # 固定导航项对"是否换页"没有信息量（切页前后都在），
            # 留下它们只会稀释差异，必须剔除。
            if name in self.PAGE_SIGNATURE_NAV_LABELS:
                continue
            out.add(name)
        return out

    @staticmethod
    def _stable_ratio(a, b):
        """两组文字里"没变"的占比（以较大的集合为分母）。

        ⚠️ 这里**不能用 Jaccard**（2026-10-28 自测发现）：
        Jaccard = |交集|/|并集| 在小集合上极度敏感 —— 4 个元素变 1 个就是 0.60，
        而整页 20 个元素变 1 个是 0.95。同一个"只变一项"的事实，
        在小页面上会被判成"整页换了"。
        改用 |交集|/max(|A|,|B|) 后，"变了几项"与集合大小脱钩，口径一致。

        Returns:
            float 0~1；任一侧为空返回 1.0（视为无变化，不据此判成功）。
        """
        if not a or not b:
            return 1.0
        return len(a & b) / max(len(a), len(b))

    #: 主体文字"未变占比"低于该值 ⇒ 判定"页面真的换了"。
    #:
    #: 为什么取 0.60：
    #:   - 同一页只跳时钟/轮播/数值：实测稳定在 0.8 以上；
    #:   - Tab 切换后主体内容整体换掉：实测稳定在 0.3 以下；
    #:   - 取 0.60 落在中间，且允许"一页里换掉近一半文字"仍算同页。
    #: 这两个数都是 2026-10-28 在万年历上实测的，不是拍脑袋。
    PAGE_SIGNATURE_SAME_MIN = 0.60

    def _shot_is_stale(self, sig_before, sig_after):
        """两次文字指纹完全相同且集合非空 —— 多半是 dump 返回了缓存。

        2026-11-02 实测（万年历）：坐标点击已成功切到天气页，
        但切页前后 dump 出来的文字集合**完全一致**（stable_ratio=1.000），
        模型却确认"当前是天气页"。⇒ a11y 层级有缓存/滞后，
        这种"指纹毫无变化"的情况不能当作"没换页"的证据。
        """
        return bool(sig_before) and sig_before == sig_after

    def _verify_tap_effect(self, step, before, target, idx):
        """点击后校验页面是否**真的换了**；没换则重试一次。

        判据（按可靠性排序，2026-10-28 run 14 实测后重写）：
          1. **前台包名**：跳到别的 App 或换了 Activity —— 最硬，直接算生效；
          2. **主体文字指纹**：点击前后文字集合的 Jaccard < 0.75 —— 算生效；
          3. 截图差异：前两条都拿不到时才用，且只作为"没换"的旁证。

        为什么不继续用截图做主判据：日历首页每秒都在变，同页 diff 恒 >2%，
        它必然把"没点上"误报成"生效"。这一条曾经让整个报告结论全错。
        """
        time.sleep(0.6)  # 给页面切换留一点时间（不做无谓长等待）
        before_fg = step.get('_fg_before') or ''
        after_fg = self._foreground_package()
        fg_changed = bool(
            before_fg and after_fg and before_fg != after_fg)

        before_sig = step.get('_sig_before')
        after_sig = self._page_text_signature() if before_sig is not None else None
        sig_ratio = (self._stable_ratio(before_sig, after_sig)
                     if before_sig is not None else None)

        if fg_changed:
            self._log('[点击校验] 前台 %s → %s，判定点击生效', before_fg, after_fg)
            self._entry_cache = None
            return

        # 截图差异：**实时**反映画面，不受 a11y 缓存影响，优先采用。
        shot_ratio = self._shot_diff_ratio(before, self._safe_shot())
        shot_changed = shot_ratio >= self.TAP_EFFECT_MIN_DIFF

        if sig_ratio is not None and sig_ratio < self.PAGE_SIGNATURE_SAME_MIN:
            self._log('[点击校验] 页面文字仅保留 %.0f%%（<%.0f%%），判定点击生效',
                      sig_ratio * 100, self.PAGE_SIGNATURE_SAME_MIN * 100)
            self._entry_cache = None
            return

        # 文字指纹"完全没变"时不能信它（dump 缓存），改用截图差异判。
        if shot_changed and self._shot_is_stale(before_sig, after_sig):
            self._log('[点击校验] 文字层级疑似缓存未刷新，改看截图：画面变化 %.1f%%'
                      '（≥%.0f%%）判定点击生效',
                      shot_ratio * 100, self.TAP_EFFECT_MIN_DIFF * 100)
            self._entry_cache = None
            return

        # 到这里：没有前台变化，主体文字也基本没变 ⇒ 极可能"没点上"
        detail = ('页面文字保留 %.0f%%' % (sig_ratio * 100)
                  if sig_ratio is not None else '未能取到页面层级')

        # ⭐ 底部导航项：**不再用视觉重试**，直接抛错交给坐标兜底。
        # 依据（2026-11-02 实测）：这类节点 bounds 全 [0,0][0,0]，视觉定位怎么试都
        # 点不中，再重试一次只是白花 ~20s；而坐标兜底 2 秒就能成。
        #
        # 注意：只有 target **确实在当前这一排导航项里** 才这样走。
        # `设置`/`我的` 等名字虽在 NAV_ITEM_NAMES 中，但可能只是页面内的普通入口，
        # 那种情况仍应按原逻辑重试（不能因为名字像导航项就放弃视觉路径）。
        if target in self._nav_siblings(target, self._current_page_entries()):
            raise AssertionError(
                '点击「%s」未生效（%s）；底部导航项视觉定位不可靠，'
                '改由坐标兜底处理' % (target, detail))

        self._log('[点击校验] %s，疑似没点上（target=%s），重试一次',
                  detail, target)
        try:
            # 重试用更直白的描述：很多"点不上"是因为描述与屏幕文字对不上。
            # 位置词（底部/上方）单独成句，**绝不拼进元素名**，模型才容易定位。
            self._page.ai_tap(
                self._simple_tap_hint(target, step.get('selector_position') or ''))
            self._count_model_use(target)
        except Exception as exc:  # noqa: BLE001
            self._log('[点击校验] 重试失败: %s', exc)
            raise AssertionError(
                '点击「%s」未生效（%s），重试也失败：%s' % (target, detail, exc))
        finally:
            # 重试可能改变了页面，缓存一律作废（宁可多 dump 一次，也不用过期清单）
            self._entry_cache = None

        time.sleep(0.8)
        after_fg2 = self._foreground_package()
        after_sig2 = self._page_text_signature() if before_sig is not None else None
        sig_ratio2 = (self._stable_ratio(before_sig, after_sig2)
                      if before_sig is not None else None)

        if before_fg and after_fg2 and before_fg != after_fg2:
            self._log('[点击校验] 重试后前台变为 %s，点击生效', after_fg2)
            return
        if sig_ratio2 is not None and sig_ratio2 < self.PAGE_SIGNATURE_SAME_MIN:
            self._log('[点击校验] 重试后页面文字仅保留 %.0f%%，点击生效',
                      sig_ratio2 * 100)
            return

        # 重试后仍未换页 ⇒ 明确判失败，别让后续步骤在错页面上空跑
        raise AssertionError(
            '点击「%s」未生效：页面内容没有变化（%s）。'
            '可能原因：该元素不存在、文字描述与界面不符、或点击区域不可点。'
            % (target,
               '文字保留 %.0f%%' % (sig_ratio2 * 100)
               if sig_ratio2 is not None else '未能取到页面层级'))

    @staticmethod
    def _simple_tap_hint(target, position=''):
        """把点击描述简化成模型最容易匹配的形式：**一个纯元素名 + 一句方位说明**。

        实测问题：给模型 `底部「黄历」Tab`，屏幕上那个控件只写着「黄历」，
        书名号 / "Tab" / 位置词都是我们自己加的修饰。描述与界面文字对不上，
        模型就会 `Element not found`（2026-10-28 实测报的正是 `底部黄历`）。

        所以这里只输出元素文字；位置词单独放括号里说明用途，不做拼接。
        """
        t = str(target or '').strip()
        # 去掉书名号/引号，只留里面的文字
        t = (t.replace('「', '').replace('」', '').replace('“', '')
              .replace('”', '').replace('"', ''))
        # 去掉英文 Tab 后缀。注意不能要求前置空白 ——
        # 中文和英文直接相连（如"底部黄历Tab"）时 \b 也能匹配，但 \s+ 不行，踩过一次。
        t = re.sub(r'[Tt]ab\s*$', '', t).strip()
        if len(t) < 2:
            t = str(target or '').strip()
        # 兜底：若仍以位置词开头，剥掉（`底部黄历` → `黄历`）
        for word in ('底部', '顶部', '上方', '下方', '左侧', '右侧', '左边', '右边'):
            if t.startswith(word) and len(t) - len(word) >= 2:
                t = t[len(word):].strip()
                break
        return '%s（%s）' % (t, position) if position else t

    @staticmethod
    def _step_has_coords(step: dict) -> bool:
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

    @staticmethod
    def _count_model_use(prompt: str = ''):
        """估算并累加一次模型调用的 token。

        真实 usage 由 LLM 代理记录；这里只是兜底，保证即使代理没生效，
        "这次执行花了多少"也有个可比的数字。
        """
        try:
            from .llm_usage import record_step_estimate
            record_step_estimate(prompt=prompt)
        except Exception:  # noqa: BLE001
            pass

    def _replay_tap(self, step: dict):
        """纯坐标点击：不经过视觉模型。"""
        cx = int(step['center_x'])
        cy = int(step['center_y'])
        self._log('[回放] tap -> (%d, %d)  [零模型]', cx, cy)
        page = self._page
        # midscene 的 page 对象暴露的坐标点击入口随版本略有差异，按优先级尝试
        for name in ('tap', 'click', 'tap_point', 'click_point'):
            fn = getattr(page, name, None)
            if callable(fn):
                fn(cx, cy)
                return
        # 兜底：走 adb 的 input tap（完全绕开模型）
        self._page.run_adb_shell('input tap %d %d' % (cx, cy))

    def _replay_input(self, step: dict, value: str):
        """点输入框（坐标）后输入文本，不经过视觉模型。"""
        self._replay_tap(step)
        time.sleep(0.4)
        page = self._page
        for name in ('input_text', 'inputText'):
            fn = getattr(page, name, None)
            if callable(fn):
                fn(value or '')
                return
        # 兜底：adb 文本输入（注意 adb input text 不支持中文/空格，需要时由
        # 使用者改用 aiInput 的 AI 路径）
        safe = (value or '').replace(' ', '%s')
        self._page.run_adb_shell('input text "%s"' % safe)

    def _do_extract(self, extract_list: List[dict]) -> Dict[str, Any]:
        """调用 ai_query 提取结构化业务数据，按 type 解析后返回 {key: value}。"""
        out: Dict[str, Any] = {}
        for item in extract_list:
            key = item.get('key')
            prompt = _resolve_template(item.get('prompt', ''), self.context)
            typ = item.get('type', 'string')
            required = item.get('required', False)
            if not key:
                continue
            try:
                raw = self._page.ai_query(prompt)
                out[key] = self._coerce(raw, typ)
                self._log("extract %s(%s) = %s", key, typ, out[key])
            except Exception as e:
                self._log("extract %s 失败: %s", key, e)
                if required:
                    raise
        return out

    def _wait_for(self, prompt: str, timeout_ms=None):
        """等到 prompt 描述的元素/条件出现，超时则抛错。

        为什么需要它：很多失败不是用例写错，而是"页面还没渲染完就去点"。
        固定 sleep（wait 动作）要么等太久浪费时间，要么等太短仍然失败；
        条件等待是"条件一成立立刻继续，最多等 timeout_ms"。

        实现上优先用底层 SDK 的 waitFor 能力；若当前版本没有该方法，
        退化成"轮询 ai_query 直到答案命中"的通用实现，保证多环境可用。
        """
        try:
            timeout_ms = int(timeout_ms or 0) or self.DEFAULT_WAIT_FOR_TIMEOUT_MS
        except (TypeError, ValueError):
            timeout_ms = self.DEFAULT_WAIT_FOR_TIMEOUT_MS
        timeout_ms = max(1000, timeout_ms)
        timeout_s = timeout_ms / 1000.0

        # ---- 路径 1：SDK 原生条件等待 ----
        for name in ('ai_wait_for', 'ai_waitfor', 'aiWaitFor'):
            fn = getattr(self._page, name, None)
            if callable(fn):
                self._log('[等待元素] 等待「%s」出现（超时 %ds）',
                          prompt, int(timeout_s))
                try:
                    fn(prompt, timeout_ms)
                except TypeError:
                    # 有些实现只接受 prompt，超时走内部默认值
                    fn(prompt)
                return

        # ---- 路径 2：轮询通用实现 ----
        self._log('[等待元素] SDK 无原生 waitFor，改用轮询等待「%s」（超时 %ds）',
                  prompt, int(timeout_s))
        deadline = time.time() + timeout_s
        ask = ('回答 true 或 false：当前屏幕是否满足下面这个条件？\n%s' % prompt)
        last = ''
        while time.time() < deadline:
            try:
                res = self._page.ai_query(ask)
                last = str(res).strip()
            except Exception as exc:  # noqa: BLE001
                self._log('[等待元素] 查询失败（继续重试）: %s', exc)
                last = ''
            if last.lower().startswith('true'):
                self._log('[等待元素] 条件已成立，继续执行', )
                return
            time.sleep(1)
        raise AssertionError(
            '等待超时（%ds）：元素/条件未出现 —— %s。最后一次判断结果：%s'
            % (int(timeout_s), prompt, last or '(无返回)')
        )

    @staticmethod
    def _normalize_assert_prompt(prompt: str) -> str:
        """把歧义的「A/B」断言改写成明确的「A 或 B」，避免模型当成一个整体名。

        实测问题（2026-10-28 真机）：断言写成
            「验证主页面底部存在「择吉日/会员」Tab」
        视觉模型会把「择吉日/会员」理解成**一个名为"择吉日/会员"的 Tab**，
        于是回答"不存在该组合 Tab"，而实际底部是「万年历/黄历/天气/会员/工具」，
        断言本意（有择吉日或会员都算过）被完全执行成了另一种语义。

        根因不在 AI 能力，而在中文里 `/` 既可能表示"或"也可能只是名字里的
        分隔符 —— 尤其被引号包起来时，歧义更大。

        这里只在**能确定是并列项**时改写，避免把正常名字改坏：
          「A/B」Tab        → 「A」或「B」
          A、B 其中之一     → 原样保留（已经足够清楚）
        """
        text = str(prompt or '')

        def _repl(m):
            left = (m.group(1) or '').strip()
            right = (m.group(2) or '').strip()
            if not left or not right:
                return m.group(0)
            # 输出干净的「A」或「B」，不额外包一层引号
            return '「%s」或「%s」' % (left, right)

        # 只处理「A/B」这种**同一个引号内**的并列写法：
        #   「择吉日/会员」 → 「择吉日」或「会员」
        #   “A／B”（全角斜杠）同样处理
        # 两边都不含斜杠/引号的普通名字（如「万年历」）原样不动。
        pattern = re.compile(
            r'[「“"]([^「」“”"/／]{1,30})\s*[/／]\s*([^「」“”"/／]{1,30})[」”"]'
        )
        return pattern.sub(_repl, text)

    @staticmethod
    def _split_or_assert(prompt: str) -> List[str]:
        """把「…「A」或「B」…」拆成 ["…「A」…", "…「B」…"]，无法拆则返回 [原文]。

        只处理**明确并列**的写法，宁可少拆也不要错拆：
          - 「A」或「B」          → 2 项
          - 「A」、「B」或「C」   → 3 项（顿号连接的同一组并列）
        其它情况（单个名字、普通句子）原样返回，避免把正常断言改坏。
        """
        text = str(prompt or '').strip()
        if not text:
            return [text]

        # 收集所有被引号包住的片段及其位置
        quoted = list(re.finditer(r'[「“"]([^「」“”"]{1,30})[」”"]', text))
        if len(quoted) < 2:
            return [text]

        # 依次检查相邻两段之间的连接符是否为并列词
        groups, cur = [], [quoted[0]]
        for prev, nxt in zip(quoted, quoted[1:]):
            between = text[prev.end():nxt.start()]
            # 只认「或」以及紧接着的顿号/空格；顿号本身也视作并列
            if re.search(r'(或|或者|／|/)', between) or between.strip('　 、,，') == '':
                cur.append(nxt)
            else:
                groups.append(cur)
                cur = [nxt]
        groups.append(cur)

        # 取第一个"多元素组"来展开；只展开一组，避免多重括号嵌套把语义搞乱
        target = next((g for g in groups if len(g) > 1), None)
        if not target:
            return [text]

        head = text[:target[0].start()]
        tail = text[target[-1].end():]
        out = []
        for m in target:
            # 保留引号：模型对「X」这种明确引用更容易正确判断，
            # 去掉引号会让「验证存在A」这种句子读起来没边界。
            out.append('%s「%s」%s' % (head, m.group(1), tail))
        return [o.strip() for o in out]

    def _do_assert(self, step: dict) -> bool:
        spec = step.get('assert') or {}
        prompt = _resolve_template(spec.get('prompt', ''), self.context)
        expect = spec.get('expect', '')
        if not prompt:
            # ⚠️ 没有 assert 规格时**回落到 prompt 本身**，不能直接判通过。
            # 2026-10-29：多行 prompt 拆分后会产生 `action=assert` 但只带 prompt
            # 的子步骤（「验证日历主体区域刷新为天气模式视图」）。原来这里
            # `return True` 会让这类断言**永远通过**，报告显示 ✓ 却什么都没验证 ——
            # 比误报失败更危险，因为它把"没测"伪装成"测过了"。
            prompt = _resolve_template(step.get('prompt', ''), self.context)
            expect = expect or (prompt[:80] if prompt else '')
        if not prompt:
            self._log('[断言] 第 %s 步缺少断言内容，按"无法验证"处理',
                      step.get('index', 0) + 1 if isinstance(step.get('index'), int)
                      else step.get('name') or '?')
            return False
        # 「A/B」这种并列写法会被视觉模型当成一个整体名字，先规范化再问
        normalized = self._normalize_assert_prompt(prompt)
        if normalized != prompt:
            self._log('[断言] 原文含歧义并列写法，已改写为：%s', normalized)
            prompt = normalized

        # 「或」语义：逐个候选单独断言，任一成立即通过。
        #
        # 为什么不能只靠改写文案交给模型一次判断：模型仍可能把
        # 「A」或「B」当成"需要同时看到 A 和 B"来回答（实测就会）。
        # 拆成多次独立断言，每次只问一个明确对象，语义没有歧义空间。
        alternatives = self._split_or_assert(prompt)
        if len(alternatives) > 1:
            self._log('[断言] 检测到并列条件 %d 个，逐个验证（任一成立即通过）：%s',
                      len(alternatives), ' / '.join(alternatives))
            last_err = ''
            for i, alt in enumerate(alternatives):
                try:
                    self._page.ai_assert(alt)
                    self._log('[断言] 第 %d/%d 个条件成立：%s',
                              i + 1, len(alternatives), alt)
                    return True
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)
                    self._log('[断言] 第 %d/%d 个条件不成立，继续尝试下一个', i + 1,
                              len(alternatives))
            self._log('[断言] 所有并列条件都不成立，最后一个错误：%s', last_err[:300])
            return False

        try:
            res = self._page.ai_assert(prompt)
        except Exception as e:
            self._log("assert 异常: %s", e)
            return False
        # ⚠️ @midscene/android 的 ai_assert 语义是「不成立就抛 AssertionError」，
        # **成立时返回 None**（实测 v0.1.5：成功 -> None，失败 -> 抛异常）。
        # 旧写法 `str(res).strip().lower().startswith('true')` 会把 None 变成
        # 字符串 'none'，永远不以 'true' 开头 —— 于是断言**永远判失败**。
        # 2026-10-28 真机验证：整条路径（launch/aiAct/aiQuery 全 passed）卡在
        # 最后这条 assert 上，报错文案就是断言本身的文本。
        # 现在只把「明确的否定」当失败，其余（None / True / 'true' / ''）都算通过。
        if isinstance(res, bool):
            return res
        text = str(res).strip().lower() if res is not None else ''
        if text in ('false', 'no', '0', '否'):
            return False
        return True

    @staticmethod
    def _coerce(val: Any, typ: str) -> Any:
        if typ == 'number':
            try:
                return float(val)
            except (TypeError, ValueError):
                return val
        if typ == 'boolean':
            return str(val).strip().lower() in ('true', '1', '是', 'yes')
        if typ == 'json':
            try:
                return json.loads(val) if isinstance(val, str) else val
            except (TypeError, ValueError):
                return val
        return val

    # ---------------- 日志 ----------------
    def _log(self, fmt: str, *args):
        msg = fmt % args if args else fmt
        ts = datetime.now().strftime('%H:%M:%S')
        line = "[%s] %s" % (ts, msg)
        self.logs.append(line)
        logger.info("[Midscene] %s", msg)
        # 实时外发：让调用方（apk_workflow）能边跑边落库，
        # 前端因此能看到「正在执行第 N 步」而不是整条路径跑完才刷新。
        # 回调本身出错绝不能影响执行（日志是辅助信息，不是业务逻辑）。
        sink = self._log_sink
        if sink is not None:
            try:
                sink(line)
            except Exception:  # noqa: BLE001
                pass


class _AndroidPopupAdapter(PopupAdapter):
    """把 midscene 的 page 对象适配成 popup_handler 的接口。"""

    def __init__(self, page, log_func):
        self._page = page
        self._log_func = log_func

    def query(self, prompt: str):
        return self._page.ai_query(prompt)

    def tap(self, target: str) -> None:
        self._page.ai_tap(target)

    def log(self, msg: str) -> None:
        self._log_func('%s', msg)
