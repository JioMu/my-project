"""
通用弹窗自动处理。

背景：自然语言用例执行时，弹窗（权限请求、隐私协议、开屏广告、版本升级、
登录引导……）是随机出现的 —— 不知道会弹在哪一步，也可能全程不弹。一旦弹出
就会挡住目标元素，导致那一步失败，甚至整条用例连锁失败。

这里提供一套与平台无关的处理逻辑，Android（midscene_engine）和
iOS（midscene_ios_engine）共用，行为保持一致。

核心是"事后补救 + 重试"：
    执行某步 -> 失败 -> 关弹窗 -> 重试该步 -> 还失败再关再试 -> 放弃
这样正常路径零额外开销（弹窗本来就不常出现），而一旦真的弹了，也能自己
恢复，不需要人在旁边盯着。

调用方实现 PopupAdapter 接口即可接入：
    query(prompt) -> 字符串         问模型一个问题
    tap(target)  -> None            点击某个目标（自然语言描述）
    log(msg)     -> None            记一条日志
"""

from __future__ import annotations

import os
import re


# ---------------------------------------------------------------------------
# 全局弹窗策略（平台级默认，所有用例共享）
# ---------------------------------------------------------------------------
# 为什么要有"全局策略"而不是每条用例各写一遍：
# 权限框 / 隐私协议 / 开屏广告 几乎每个 App 都有，逐条用例声明既啰嗦又会漏。
# 这里给一套**默认行为**，需要测弹窗的用例再用 POPUP_AUTO_DISMISS=0 关掉。
#
# ⚠️ 关于"预先授予权限"（2026-10-29 真机实测结论）：
#   曾打算在执行前用 `adb shell pm grant` 直接授权，让权限框**根本不弹**，
#   但在 RMX2117(Android 13+) 上被 ROM 拒绝：
#     pm grant    -> SecurityException: Neither user 2000 nor current process
#                    has android.permission.GRANT_RUNTIME_PERMISSIONS
#     appops set  -> SecurityException: uid 2000 does not have
#                    android.permission.MANAGE_APP_OPS_MODES
#   ⇒ **不能**依赖 pm grant / appops 预授权，只能靠"弹出来再点允许"。
#   若某台机器是 userdebug/root 且确实允许 grant，可用环境变量
#   POPUP_PREGRANT=1 打开（默认关，避免每台机器都白跑一次失败的 grant）。

#: 全局开关：是否自动处理弹窗。设 0/false 关闭（专门测弹窗的用例需要）。
POPUP_AUTO_DISMISS = os.environ.get('POPUP_AUTO_DISMISS', '1') not in (
    '0', 'false', 'False', '')

#: 执行前是否先清一轮弹窗（不等失败）。
#: 为什么值得：弹窗若没挡住"当前步骤"而是挡住别处，事后补救不会触发，
#: 那一步照常失败；开跑先清一次成本很低（一次模型调用）。
POPUP_PRECHECK = os.environ.get('POPUP_PRECHECK', '1') not in (
    '0', 'false', 'False', '')

#: 是否尝试执行前预授权（默认关，见文首实测说明）。
POPUP_PREGRANT = os.environ.get('POPUP_PREGRANT', '0') not in (
    '0', 'false', 'False', '')

#: 各类弹窗的**默认处置倾向**。
#: 顺序即优先级：先找"允许/同意"类按钮，再退"跳过/取消"类。
#: 这份清单同时用于①提示词（告诉模型点哪个）②代码侧校验（防止点错到付费）。
POPUP_BUTTON_PRIORITY = (
    # 权限类：一律允许（用户要求"默认允许"）
    'allow', '允许', '始终允许', '使用期间允许', '仅在使用中允许',
    '好', 'OK', '同意', '接受', '我知道了', '知道了', '确定',
    # 系统 Android 权限框的按钮文案（"使用应用时"/"仅限这一次"）
    '使用应用时', '仅限这一次', '仅在使用该应用时', '向应用授予此权限',
    'while using the app', 'only this time', 'grant',
    # 广告/引导类：跳过或关闭
    '跳过', '跳过广告', '关闭广告', 'skip',
    'later', '以后再说', '稍后', '下次再说', '暂不', '取消', 'close', 'x', '×',
    # 登录/升级引导类：**不登录**也能继续用，一律选"取消/关闭"这一侧。
    # 2026-11-02 真机实测（万年历「登录万年历 / 记录查看两不误 / 立即登录 / 取消」）：
    # 自动处理弹窗开着，但模型给出的是「立即登录」—— 它不属禁点按钮，于是照点登录。
    # 登录会切到登录页/拉起第三方 SDK，后续步骤全部在错误的页面上跑。
    '不登录', '暂不登录', '以后登录', '稍后登录', '暂不设置',
)

#: 绝不允许点击的按钮关键词（付费 / 订阅 / 危险），命中即放弃关闭该弹窗。
#: 只靠提示词约束不够 —— 模型偶尔会把"开通会员"当成"同意"。
POPUP_FORBIDDEN_BUTTONS = (
    '支付', '付款', '购买', '开通', '订阅', '立即续费', '充值', '升级会员',
    '同意并支付', '免费试用后付费', 'buy', 'subscribe', 'purchase',
    '删除', '清空', '退出登录', '注销',
    # 登录/注册类：自动化用例不该替用户建立账号或切换登录态。
    # 2026-11-02 实测踩到「登录万年历」弹窗被点「立即登录」，后续步骤全跑在登录页上。
    '登录', '立即登录', '注册', '立即注册', '一键登录', '手机号登录',
    'login', 'sign in', 'sign up', 'register',
)


# ---------------------------------------------------------------------------
# 提示词：告诉模型"什么是弹窗"以及"该点哪里"
# ---------------------------------------------------------------------------
# 这份清单是弹窗识别的全部依据。要支持新类型的弹窗，加在对应的分类里即可。
POPUP_DETECT_PROMPT = (
    'Look at the current screen and tell me whether a POPUP / DIALOG / OVERLAY '
    'is currently blocking the main interface.\n'
    '\n'
    'A popup is one of:\n'
    '  - a permission request (notifications / location / camera / photos / '
    'microphone / cellular data / storage)\n'
    '  - a user agreement, privacy policy or terms-of-service dialog\n'
    '  - an app update prompt or a rating / review prompt\n'
    '  - a splash ad or an interstitial ad\n'
    '  - a login or sign-up guide that can be skipped\n'
    '  - a system alert shown on top of the app\n'
    '\n'
    'NOT a popup (never report these):\n'
    '  - the app\'s own loading / progress indicator\n'
    '  - a normal page, list, form or navigation bar\n'
    '  - a toast that disappears by itself\n'
    '\n'
    'Answer with EXACTLY one line, no explanation:\n'
    '  none              -> there is no popup\n'
    '  <button text>     -> there IS a popup; give the text printed INSIDE the '
    'button that dismisses it\n'
    '\n'
    'Rules for choosing the button:\n'
    '  1. Copy the button text EXACTLY as it appears on screen. Do not translate, '
    'do not add explanations, do not number the options.\n'
    '  2. **Permission dialogs must ALWAYS be allowed** (never deny a permission).\n'
    '     On Android 12+ this is usually "使用应用时" / "仅限这一次" / "允许".\n'
    '  3. For a login / sign-up / member guide, choose the button that SKIPS it '
    '("取消" / "暂不" / "以后再说" / "跳过" / "关闭"). NEVER choose '
    '"立即登录" / "登录" / "注册" — it must not log in.\n'
    '  4. For ads or update prompts choose "跳过" / "关闭" / "X" / "以后再说".\n'
    '  5. Never choose a button that buys something or starts a subscription '
    '(支付/开通/订阅/购买/续费).'
)

# 关掉一个弹窗后再问一次，看有没有第二个（广告 + 权限经常连着弹）。
POPUP_MAX_LAYERS = 4

# 提示词里出现过这些词，说明模型没有按格式回答（例如把整个句子的解释
# 都吐出来了），这种情况当作"识别失败"，不要拿它去点击。
_ANSWER_REJECT = (
    'none', 'no popup', 'n/a', 'na', 'unknown', 'nothing',
    '没有', '无', '无弹窗',
)


class PopupAdapter:
    """平台适配层。Android / iOS 引擎各自实现这三个方法。"""

    def query(self, prompt: str):
        """问模型一个问题，返回字符串结果。"""
        raise NotImplementedError

    def tap(self, target: str) -> None:
        """点击由自然语言描述的目标。"""
        raise NotImplementedError

    def tap_text(self, text: str):
        """按**屏幕上的确切文字**点击，返回是否点中。

        与 `tap()` 的区别很关键：`tap()` 是自然语言描述走视觉定位，要调模型、
        要几十秒，且模型可能把「立即登录」当成"接受"；`tap_text()` 是拿
        UI 层级里的真实节点文字做**精确比对**再点它的中心坐标，不调模型、
        毫秒级，也不可能理解错语义。

        默认返回 False（未实现），调用方会退回 `tap()`。
        """
        return False

    def log(self, msg: str) -> None:
        """记录日志。"""
        raise NotImplementedError


def _normalise(answer) -> str:
    """把模型的回答收拾成一个短字符串，失败时返回空串。

    模型有时会把整段思考过程返回回来，直接拿去 ai_tap 会乱点，所以这里
    做两层限制：取第一行、限制长度、过滤明显的"解释性"回答。
    """
    if answer is None:
        return ''
    if isinstance(answer, (list, tuple)):
        answer = answer[0] if answer else ''
    if isinstance(answer, dict):
        # 有些实现会包一层 {"result": ...} 或 {"text": ...}
        for key in ('result', 'text', 'value', 'answer'):
            if key in answer:
                answer = answer[key]
                break
        else:
            return ''
    text = str(answer).strip()
    if not text:
        return ''
    # 只取第一行
    text = text.splitlines()[0].strip()
    # 去掉常见的包裹符号
    text = text.strip('`"\'。.,;: ')
    if len(text) > 40:
        return ''
    lowered = text.lower()
    if lowered in _ANSWER_REJECT:
        return ''
    # "none" 开头的长句子通常是解释，不是按钮名
    if lowered.startswith('none'):
        return ''

    # 去掉模型随手带上的**编号/序号**（2026-11-02 真机实测）：
    # 问"有弹窗吗、点哪个按钮关闭"，模型答 `跳过 1` —— 前面是按钮名，后面是它自己对
    # 候选列表的编号。原样拿去 ai_tap 会去找一个屏幕上不存在的"跳过 1"，必然点空，
    # 表现为「检测到了弹窗但 closed=0」，用户看到的就是"勾了自动处理弹窗却没效果"。
    # 只剥尾部的编号（不碰 "5分钟后" 这类有语义的内容）。
    # 带单位后缀的也要剥：真机实测出现过 `跳过 1s`（编号 + 秒数混在一起），
    # 只认纯数字会漏，于是又去找一个屏幕上不存在的"跳过 1s"。
    text = re.sub(r'\s+\d{1,2}[sS]?$', '', text).strip()
    if not text:
        return ''
    lowered = text.lower()
    if lowered in _ANSWER_REJECT:
        return ''
    return text


def _is_forbidden_button(button: str) -> bool:
    """该按钮是否属于"绝不能点"（付费/订阅/删除/退出登录）。

    为什么代码还要拦一遍：提示词已写明"不要选付费按钮"，但模型仍可能把
    「开通会员」理解成「同意」—— 尤其弹窗文案是「开通会员享 3 天免费」时。
    点下去的后果是真实扣费/退登，代价远大于"少关一个弹窗"，所以硬拦。
    """
    text = str(button or '').lower()
    return any(w in text for w in POPUP_FORBIDDEN_BUTTONS)


def dismiss_popups(adapter: PopupAdapter, max_layers: int = POPUP_MAX_LAYERS) -> int:
    """检测并关闭当前屏幕上的弹窗，返回关掉的数量。

    循环处理，因为弹窗常常是连续出现的：先弹权限请求，点掉之后再弹广告。
    任何一步出错都立刻停止（宁可少关一个，也不要把正常界面点乱）。
    """
    closed = 0
    for layer in range(max_layers):
        try:
            answer = adapter.query(POPUP_DETECT_PROMPT)
        except Exception as exc:  # noqa: BLE001
            adapter.log('[弹窗] 第 %d 层检测失败，停止处理: %s' % (layer + 1, exc))
            break

        button = _normalise(answer)
        if not button:
            if closed:
                adapter.log('[弹窗] 已关闭 %d 个弹窗' % closed)
            break

        # 禁点校验：宁可留着这个弹窗（交人工处理），也不能点到付费/删除上
        if _is_forbidden_button(button):
            adapter.log('[弹窗] 模型建议点击「%s」，属禁点按钮（付费/删除类），'
                        '已放弃关闭该弹窗，请人工确认' % button)
            break

        try:
            # ⭐ 先按屏幕真实文字精确点击（不调模型、秒级、语义不会跑偏）。
            # 为什么这是首选（2026-11-02 真机实测）：走视觉 `tap()` 时模型把
            # 「登录万年历」弹窗理解成"同意"→ 点「立即登录」；而且一次视觉点击
            # 要几十秒，冷启动预检 + 每步重试叠加起来会把一条用例拖到十几分钟。
            # 拿 dump 里的真实节点文字做精确比对，既有确定性又几乎零成本。
            tapped = False
            try_text = getattr(adapter, 'tap_text', None)
            if callable(try_text):
                tapped = bool(try_text(button))
                if tapped:
                    adapter.log('[弹窗] 已按文字精确点击「%s」' % button)
        except Exception as exc:  # noqa: BLE001
            adapter.log('[弹窗] 按文字点击「%s」失败: %s' % (button, exc))
            tapped = False

        if not tapped:
            try:
                adapter.tap(button)
            except Exception as exc:  # noqa: BLE001
                adapter.log('[弹窗] 点击「%s」失败: %s' % (button, exc))
                break

        closed += 1
        adapter.log('[弹窗] 已关闭第 %d 个弹窗（点击「%s」）' % (closed, button))

    else:
        # 循环正常跑完 = 层数用尽了还没关完，提示一下便于排查
        adapter.log('[弹窗] 连续关闭 %d 个后仍然检测到弹窗，可能存在弹窗循环' % closed)

    return closed
