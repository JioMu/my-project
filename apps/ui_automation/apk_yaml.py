# -*- coding: utf-8 -*-
"""把 REA 路径清单翻译成 Midscene YAML 并执行。

为什么要 YAML
-------------
需求文档要求「用例沉淀成 YAML 回归用例」，这样用例可以脱离平台被
midscene CLI 直接跑（`npx midscene ./case.yaml`），也是回归资产。

但要清醒：**平台内执行不依赖 YAML**。真机执行走
`midscene_engine.MidsceneAppEngine`（已验证的 Python 桥），YAML 只作为
「可带走的资产」生成与展示。原因很实际 —— 走 `npx midscene` 等于在
容器里再起一套 Node 工具链，装依赖、版本对齐、设备 env 全要重来一遍，
MVP 阶段没必要，而且 2026-08 已在真机验证过 Python 引擎那条路。

生成规则（与 midscene.yaml 规范对齐）
-----------------------------------
    android:
      launch: com.example.app          # 有 activity 时用 activity
    deviceId: "<udid>"
    tasks:
      - name: <路径名>
        flow:
          - ai: <步骤语义描述>
          - aiQuery: 输出当前页面标题、页面类型和可点击入口
          - aiAssert: <断言文本>

`skip` 不进 YAML（YAML 里没有"跳过"语法），而是转成一句写进首步 prompt
的约束 + 由执行引擎侧 `assert` 前做危险词拦截。这样导出的 YAML 交给
别人跑时，约束仍在提示词里，不会因为脱离平台而丢失。
"""
import logging
import re

logger = logging.getLogger(__name__)

#: 需要转义成合法 YAML 标量的字符（只做最小必要转义，保证可被 PyYAML 读回）
_YAML_SPECIAL = re.compile(r'^[\-\?:,\[\]\{\}#&\*!\|>@`"\'%]|[:#]\s|\s$')


def _q(text):
    """按需给字符串加双引号，并转义内部引号/反斜杠/换行。"""
    s = '' if text is None else str(text)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    if not s:
        return '""'
    if _YAML_SPECIAL.search(s) or '\n' in s or s in ('true', 'false', 'null', '~'):
        return '"%s"' % s
    # 纯数字/布尔样式的字符串必须加引号，否则会被解析成数字
    if re.fullmatch(r'-?\d+(\.\d+)?', s):
        return '"%s"' % s
    return '"%s"' % s if any(c in s for c in '{}[]') else s


def _skip_constraint(skip):
    """把跳过项转成一句写进提示词的约束。"""
    items = [str(s).strip() for s in (skip or []) if str(s or '').strip()]
    if not items:
        return ''
    return '（注意：本路径禁止执行以下操作：%s，遇到这些入口请直接跳过并返回上一页）' % '、'.join(items)


#: 步骤意图 → 动作类型。顺序有意义，先匹配更具体的。
#:
#: 为什么必须分派（2026-10-28 实测）：
#: 之前**所有**步骤都映射成 `aiAct`，而 `aiAct` 是"看截图 → 规划 → 执行 → 再看"
#: 的多轮自主循环。一句「点击底部黄历Tab」要跑十几轮视觉推理，动辄 1~2 分钟，
#: 实测 120s 内**连一次点击都没发出去**就超时断开 —— 用户看到的是"又慢又没点上"。
#:
#: 而 `aiTap` 是**单轮定位 + 点击**：一次模型调用拿到坐标就点，快数倍且必然产生
#: 点击动作；`assert` 只做判断不动作；`aiInput` 专用于输入。
#: 分派后每条路径的模型调用次数和耗时都能显著下降。
_STEP_INTENT_RULES = (
    # (动作, 正则, 说明)
    ('aiInput', re.compile(r'^(输入|填写|键入|录入)'), '输入类'),
    ('assert', re.compile(r'^(验证|确认|检查|断言|校验|确保)'), '验证类'),
    ('aiTap', re.compile(r'^(点击|点按|单击|点一下|轻触|点击进入|进入|打开|切换到)'), '点击/进入类'),
)

#: 以「控件类型词」结尾的短句，本身就是一次点击。
#:
#: 为什么需要：复合句拆分后会产生**没有动词的片段**，
#: 「点击底部「黄历」Tab然后点击「添加」」→ `点击底部「黄历」Tab` + `点击「添加」` 没问题，
#: 但模型也常写出「黄历Tab」「我的页签」这种纯控件名片段（动词被前一段吃掉）。
#: 这类句子走 aiAct 就是多轮自主规划，慢且容易 Replanned 超限 —— 而它语义上
#: 明确就是"点这个控件"。所以带类型后缀、且不是验证/输入语气的，一律判 aiTap。
_TAP_CONTROL_SUFFIX = re.compile(
    r'(?:[Tt][Aa][Bb]|页签|标签页?|按钮|图标|选项卡|菜单项|入口)\s*$')

#: 「动作 + 预期结果」的复合句判据。
#:
#: 为什么必须有这条（2026-10-29 实测）：
#: Playground 里「点击右下角的「工具」Tab，进入个人中心」用 aiAct 能点中，
#: 而同样的句子在路径里被 classify_step 判成 aiTap —— aiTap 是**单轮定位**：
#: 只拿 _tap_target 抽出的「工具」去屏幕上匹配、找到就点，然后结束。
#: 结果「进入个人中心」这半句被**静默丢弃**：既不规划、也不校验点完是否到位，
#: 页面没跳转照样判通过。用户看到的正是「Playground 能识别、路径识别不到」。
#:
#: 所以：只要一句话里除了动作还描述了**动作完成后应出现的结果**
#: （进入/跳转/显示/出现/打开…页面），就必须整句交给 aiAct，
#: 让模型多轮「看一眼 → 动作 → 再看一眼确认」，与 Playground 表现一致。
#: 宁可慢一点，也不能静默丢语义。
_COMPOSITE_RESULT = re.compile(
    r'(?:，|,|；|;|然后|随后|接着|之后|再)\s*'
    r'.*(?:进入|跳转|跳转到|显示|出现|弹出|打开|切换|来到|返回|跳至|进入页面)')

#: 「启动应用 xxx」这类步骤。执行层要把它剔掉（旧数据兜底），
#: 理由见 path_to_steps 的说明。
_LAUNCH_STEP = re.compile(
    r'^\s*(?:启动|打开|拉起)(?:应用|App|APP|app)?\s*[：:]?\s*'
    r'(?:com\.[\w.]+|[A-Za-z][\w.]*\.[\w.]+)?\s*(?:应用|App|APP)?\s*$')

#: 连续多个动作的句子（「A然后B」「A再B」）同样是复合句。
#: 这类句子若被判成 aiTap，只会执行第一个动作，后面的全部丢掉 ——
#: 与「进入个人中心」被丢弃是同一类错误，只是丢的是动作而不是结果。
_COMPOSITE_CHAIN = re.compile(r'(?:然后|随后|接着|之后再|再点击|再点)')


def is_composite_step(text):
    """判断是否为复合句（动作+预期结果 / 连续多个动作），必须走 aiAct。"""
    t = str(text or '')
    return bool(_COMPOSITE_RESULT.search(t) or _COMPOSITE_CHAIN.search(t))


def classify_step(text):
    """根据步骤文本判断该用哪个 action。

    Returns:
        (action, reason)  reason 为空表示走默认 aiAct
    """
    t = str(text or '').strip()
    if not t:
        return 'aiAct', ''
    # ⚠️ 复合句优先判 aiAct：整句语义必须完整交给模型，
    # 不能被下面的 aiTap 规则截断成"只点某个控件"。
    if is_composite_step(t):
        return 'aiAct', '含预期结果的复合句'
    for action, pattern, label in _STEP_INTENT_RULES:
        if pattern.search(t):
            return action, label
    # 兜底：纯控件名（带 Tab/按钮/页签 等类型后缀）＝ 点它，不走 aiAct
    if _TAP_CONTROL_SUFFIX.search(t) and len(t) <= 20:
        return 'aiTap', '控件名'
    return 'aiAct', ''


#: 步骤文本里用来标注"控件类型"的后缀词，界面上不会真的写成这些字。
#: 只剥这些词，**不剥位置词**（位置词单独走 _tap_position）。
_TARGET_NOISE_SUFFIX = re.compile(
    r'(?:[Tt][Aa][Bb]|按钮|图标|选项|入口|页签|菜单项)\s*$')

#: 位置词表。这些词只描述"在哪里"，**绝不能拼进元素名**。
#: 2026-10-28 事故：「点击底部「黄历」Tab」被处理成 `底部黄历`，
#: 模型拿着「底部黄历」去屏幕上找，界面只有「黄历」两个字 ⇒ Element not found。
_POSITION_WORDS = (
    '底部', '顶部', '上方', '下方', '左上角', '右上角', '左下角', '右下角',
    '左侧', '右侧', '左边', '右边', '中间', '中央', '页面内', '页面上', '弹窗内',
)


def _strip_quotes(s):
    return (s.replace('「', '').replace('」', '')
             .replace('“', '').replace('”', '').replace('"', '').replace('"', ''))


def _tap_target(text):
    """从「点击底部「黄历」Tab」这类句子里抽出**要点的控件文字**，作为 aiTap 的 target。

    核心规则（2026-10-28 修正，之前写反过）：
    **括号/书名号里的是元素文字，括号外的是位置与类型描述，两者不能粘连。**

      输入：点击底部「黄历」Tab      → 输出：黄历
      输入：点击顶部的“设置”         → 输出：设置
      输入：点击黄历                  → 输出：黄历
      输入：点击底部黄历               → 输出：黄历（无括号时兜底剥位置词）

    为什么必须只给元素文字：`ai_tap` 是拿文字去视觉定位的，
    描述与屏幕文字多一个字都会失配。历史上 `底部「黄历」Tab` → `底部黄历`
    就是这么把一次本该成功的点击变成 `Element not found : 底部黄历` 的。

    抽不出来就原样返回，不冒险改写。
    """
    t = str(text or '').strip()
    m = re.match(r'^(?:点击|点按|单击|点一下|轻触|点击进入|进入|打开|切换到)\s*(.+)$', t)
    raw = m.group(1).strip(' ，,。；;') if m else t
    # 无动词的纯控件名片段（「黄历Tab」「黄历页签」）同样要剥类型词，
    # 否则 selector_hint 会带着 "Tab" 去和屏幕文字做匹配。
    if not m:
        bare = _TARGET_NOISE_SUFFIX.sub('', raw).strip()
        if bare != raw and len(bare) >= 2:
            raw = bare
    if len(raw) < 2:
        return t

    # 1) 优先取括号内的文字 —— 括号是"这是元素名"的显式标记
    quoted = re.search(r'[「“"\']([^」”"\']{1,20})[」”"\']', raw)
    if quoted and len(quoted.group(1).strip()) >= 2:
        candidate = quoted.group(1).strip()
    else:
        # 2) 无括号：整体去引号后，剥掉末尾类型词与首位位置词
        candidate = _strip_quotes(raw)
        candidate = _TARGET_NOISE_SUFFIX.sub('', candidate).strip()
        for word in _POSITION_WORDS:
            if candidate.startswith(word) and len(candidate) - len(word) >= 2:
                candidate = candidate[len(word):].strip()
                break
        candidate = candidate.strip(' ，,。；;的')

    candidate = _TARGET_NOISE_SUFFIX.sub('', _strip_quotes(candidate)).strip()
    return candidate if len(candidate) >= 2 else t


def _tap_position(text):
    """抽出位置词（底部/顶部/右上角…），作为可选方位提示；没有则返回空串。

    位置只用于"同一文字出现多处时选哪个"，**不与元素文字合并**。
    """
    t = str(text or '')
    for word in _POSITION_WORDS:
        if word in t:
            return word
    return ''


def path_to_steps(path):
    """把一条路径翻译成 midscene 引擎可直接吃的 steps 列表。

    每个自然语言步骤会按**意图**分派到不同 action（见 _STEP_INTENT_RULES）：
    点击类 → aiTap（单轮定位，快）、验证类 → assert（只判断）、输入类 → aiInput。
    无法归类的才退回 aiAct（多轮自主执行，最慢但最灵活）。

    额外插入：
      - 末尾加一条 `assert`，用第一步的语义反向校验「已回到可交互页面」

    ⚠️ **不再在首步前加 `launch`**（2026-10-29）。引擎 run() 开跑第一件事就是
    `reset_to_home()` 拉起 App 并等界面稳定，这里再插一条 launch 等于
    「复位启动一次 + 步骤启动一次」，手机上的启动流程演两遍，用户看到的是
    "为什么每个步骤都在重复同样的操作"。语义上也不对：引擎已经保证起点，
    路径描述的是"从首页开始要做什么"。
    """
    steps = []
    skip_note = _skip_constraint(path.get('skip'))

    raw_steps = [str(s).strip() for s in (path.get('steps') or []) if str(s or '').strip()]
    # 兼容旧数据：历史路径可能带着「启动应用 xxx」这一步（老提示词生成的），
    # 在执行层再剔一次，避免"确认过的旧路径"继续重复启动。
    if raw_steps and _LAUNCH_STEP.match(raw_steps[0]):
        raw_steps = raw_steps[1:]
    page_probe_budget = 2  # 页面识别最多采几次（它同样要调视觉模型）
    for i, text in enumerate(raw_steps):
        prompt = text + (skip_note if i == 0 else '')
        action, _reason = classify_step(text)

        if action == 'aiTap':
            steps.append({
                'action': 'aiTap',
                'name': '步骤%d' % (i + 1),
                'prompt': prompt,
                'selector_hint': _tap_target(text),
                # 位置只是"同文字多处时选哪个"的线索，不进元素名
                'selector_position': _tap_position(text),
            })
        elif action == 'assert':
            # 验证类只做判断，不做动作；expect 用原文，失败时报告里能直接读懂
            steps.append({
                'action': 'assert',
                'name': '步骤%d' % (i + 1),
                'prompt': prompt,
                'assert': {'prompt': prompt, 'expect': text},
            })
        elif action == 'aiInput':
            steps.append({
                'action': 'aiInput',
                'name': '步骤%d' % (i + 1),
                'prompt': prompt,
                'selector_hint': _tap_target(text),
            })
        else:
            steps.append({
                'action': 'aiAct',
                'name': '步骤%d' % (i + 1),
                'prompt': prompt,
            })

        # 页面识别：只为报告留"经过的页面"，不是业务必需。
        # 只在开头采 2 次（原来 3 次），并且**点击类步骤后不采** ——
        # 跳转本身已由下一步的定位隐含验证，再单独查一次纯属浪费模型调用。
        # aiAct（含复合句）例外：它本来就是"验证是否到位"，多采一次页面标题
        # 正好把「进入了哪个页面」留进报告，成本相对 aiAct 本身可忽略。
        if page_probe_budget > 0 and action != 'aiTap':
            page_probe_budget -= 1
            steps.append({
                'action': 'aiQuery',
                'name': '页面识别',
                'prompt': '输出当前页面标题、页面类型和可点击入口',
                'extract': [{'key': 'page_%d' % (i + 1),
                             'prompt': '当前页面的标题',
                             'type': 'string',
                             'required': False}],
            })

    steps.append({
        'action': 'assert',
        'name': '路径完成校验',
        'assert': {
            'prompt': '当前是否处于一个可正常交互的页面（有可点击元素、无崩溃提示）',
            'expect': '页面可正常交互',
        },
    })
    return steps


def build_yaml(path, package, activity='', device_id='{{device_id}}'):
    """生成单条路径的 Midscene YAML 文本。"""
    launch_target = (activity or package or '').strip()
    lines = [
        '# Midscene YAML 回归用例（由 TestHub 自动生成）',
        '# 路径：%s    优先级：%s' % (path.get('name', ''), path.get('priority', '')),
        '# 可直接运行：npx midscene ./this-file.yaml',
        'android:',
        '  launch: %s' % _q(launch_target),
        'deviceId: %s' % _q(device_id),
        'tasks:',
        '  - name: %s' % _q(path.get('name', '')),
        '    flow:',
    ]
    skip_note = _skip_constraint(path.get('skip'))
    raw_steps = [str(s).strip() for s in (path.get('steps') or []) if str(s or '').strip()]
    if not raw_steps:
        lines.append('      - ai: %s' % _q('识别当前页面并返回'))
    for i, text in enumerate(raw_steps):
        prompt = text + (skip_note if i == 0 else '')
        lines.append('      - ai: %s' % _q(prompt))
        lines.append('      - aiQuery: %s' % _q('输出当前页面标题、页面类型和可点击入口'))
    lines.append('      - aiAssert: %s' % _q('页面可正常交互'))
    if path.get('skip'):
        lines.append('    # 跳过（危险/敏感）：%s' % '、'.join(str(s) for s in path['skip']))
    return '\n'.join(lines) + '\n'


def build_yaml_bundle(paths, package, activity=''):
    """把多条路径合成一份 YAML（多 task），便于整体带走。"""
    if not paths:
        return ''
    head = [
        '# Midscene YAML 回归用例集（由 TestHub 自动生成，共 %d 条路径）' % len(paths),
        'android:',
        '  launch: %s' % _q((activity or package or '').strip()),
        'deviceId: %s' % _q('{{device_id}}'),
        'tasks:',
    ]
    body = []
    for path in paths:
        body.append('  - name: %s' % _q(path.get('name', '')))
        body.append('    flow:')
        skip_note = _skip_constraint(path.get('skip'))
        raw_steps = [str(s).strip() for s in (path.get('steps') or []) if str(s or '').strip()]
        for i, text in enumerate(raw_steps):
            prompt = text + (skip_note if i == 0 else '')
            body.append('      - ai: %s' % _q(prompt))
            body.append('      - aiQuery: %s' % _q('输出当前页面标题、页面类型和可点击入口'))
        body.append('      - aiAssert: %s' % _q('页面可正常交互'))
    return '\n'.join(head + body) + '\n'


def check_dangerous(text, skip=None):
    """步骤文本是否命中危险操作（MVP 的唯一拦截点，第 2 期再做全自动探索拦截）。

    Returns:
        hit 关键词；None 表示安全。
    """
    s = str(text or '')
    for item in (skip or []):
        item = str(item or '').strip()
        if item and item in s:
            return item
    for kw in ('支付', '付款', '下单', '提交订单', '购买', '充值', '提现', '转账',
               '删除', '清空', '注销', '解绑', '退出登录', '重置', '实名'):
        if kw in s:
            return kw
    return None
