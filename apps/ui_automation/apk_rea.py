# -*- coding: utf-8 -*-
"""REA（Requirement / Entry / Action）APK 功能分析器。

职责
----
把「APK 静态解析结果」变成「可执行的测试路径清单」，即 MVP 链路的第二步：

    APK 上传 → **[REA 分析]** → 人工确认路径 → Midscene 执行 → HTML 报告

输入是 `apk_inspector.inspect_apk()` 的产物（包名/版本/权限/Activity/危险权限），
输出严格遵循需求文档里约定的结构：

    {
      "app": "com.example.app",
      "paths": [
        {"name": "...", "priority": "high", "type": "explore" | "regression",
         "steps": ["..."], "skip": ["支付", "删除"], "confirmed": false}
      ],
      "dangerous_actions": [...], "exception_pages": [...],
      "sensitive_pages": [...], "analysis_notes": "..."
    }

设计要点
--------
1. **不臆造页面**：提示词明确要求「只输出可验证的功能信息」，并把
   `evidence` 字段留给模型标注依据（manifest 权限 / Activity / 通用模式）。
2. **不用 analyzer 角色硬绑定**：优先 `role='analyzer'`，缺失时回退
   `writer`，两者都没有就**降级为规则引擎**（不阻断流程）—— 因为 APK 静态
   信息本身就足以生成一条「启动 + 首页入口探索」的兜底路径。
3. **失败可见**：LLM 报错（余额/鉴权/超时）会写进 `analysis_notes` 并在
   `llm_error` 字段返回，前端直接展示，不允许静默返回空路径。
4. **危险操作永远在 skip 里**：无论模型输出什么，都会把 docs 约定的
   支付/下单/删除/退出登录等关键词合并进每条路径的 `skip`，这是兜底护栏，
   也是 MVP 阶段唯一的「危险操作拦截」（完整的自动拦截在第 2 期）。
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

#: docs 约定的默认跳过项（模型漏写时兜底合并）
DEFAULT_SKIP = ['支付', '下单', '删除', '退出登录']

#: 危险操作关键词（用于校验模型输出 + 兜底扫描 steps）
DANGEROUS_KEYWORDS = [
    '支付', '付款', '下单', '提交订单', '购买', '充值', '提现', '转账',
    '删除', '清空', '注销', '解绑', '退出登录', '重置', '恢复出厂',
    '实名', '认证', '绑定银行卡', '修改密码', '发送短信', '拨打电话',
]

#: 路径名称/步骤里出现这些词就认为该步骤是危险动作（需要跳过）
_DANGER_RE = re.compile('|'.join(re.escape(k) for k in DANGEROUS_KEYWORDS))

#: 点击类步骤里出现「或 / ／ / 」并列候选 ⇒ 无法执行。
#:
#: 2026-10-29 真实事故：同一条路径里
#:   验证步骤「存在「择吉日」或「会员」其中一个 Tab」← 合法（任一成立即通过）
#:   点击步骤「点击「择吉日」或「会员」Tab」      ← 非法（点哪个？）
#: 验证允许并列，点击不允许。这里只把**点击动词开头**且目标含并列的句子挑出来，
#: 避免误伤「验证 A 或 B」这类正常断言。
_CLICK_OR_TARGET = re.compile(
    r'^\s*(?:点击|点按|单击|点一下|轻触|进入|打开|切换到)'
    r'[^，,。；;]*'
    r'(?:或|／|/)'
)

#: 「启动应用 / 打开 App」类步骤。
#:
#: 2026-10-29：模型第一条步骤常写「启动App com.youloft.calendar」，而执行引擎
#: 在每条路径开跑前**已经** reset_to_home 拉起过 App（并且每路径都新建引擎），
#: 于是启动流程在手机上重复演一遍。这里在 sanitize 阶段把**首步**的这类步骤剔掉。
_APP_LAUNCH_STEP = re.compile(
    r'^\s*(?:启动|打开|拉起|进入)\s*'
    r'(?:App|APP|app|应用|该应用|这个应用|目标应用)?\s*[：:]?\s*'
    r'(?:com\.[\w.]+|[A-Za-z][\w.]*\.[\w.]+)?\s*'
    r'(?:应用|App|APP)?\s*'
    r'(?:[，,]?\s*并等待.{0,12}加载.{0,8})?$'
)

#: 条件句（若…则点击…）。见 midscene_engine._neutralize_conditional 的事故说明：
#: 条件不成立时模型会离开目标 App 去找那个按钮，表现为"执行时乱启动其他 App"。
_CONDITIONAL_STEP = re.compile(
    r'^\s*(?:若|如果|假如|如出现|当)\s*[^，,。]*?(?:则|就|那么)\s*'
    r'(?:点击|点按|单击|轻触|关闭|返回|输入|填写)'
)

PROMPT_TEMPLATE = """你是一个 App 功能分析助手。请基于 APK 静态分析结果，输出可用于 UI 自动化测试的结构化信息。

要求：
1. 只输出可验证的功能信息，不要臆造未确认的页面。
2. 明确标记"已确认"和"未确认"内容。
3. 不要输出绕过登录、支付、风控或破解商业逻辑的内容。
4. 只输出 JSON，不要任何解释文字或 Markdown 代码块。

【APK 静态分析结果】
{apk_info}

{business_section}

请输出如下 JSON（字段名必须完全一致）：
{{
  "requirements": [
    {{"id": "R-01", "name": "功能点名称", "evidence": "依据（manifest 权限/Activity/通用模式）", "confirmed": true}}
  ],
  "pages": [
    {{"name": "页面名称", "entry": "如何进入（哪个 Activity / 哪个入口）", "confirmed": false}}
  ],
  "dangerous_actions": [
    {{"name": "危险操作名", "reason": "为什么危险", "level": "high"}}
  ],
  "exception_pages": [
    {{"name": "异常页面名", "trigger": "触发条件"}}
  ],
  "sensitive_pages": ["需要跳过的敏感页面"],
  "paths": [
    {{
      "name": "首页入口探索",
      "priority": "high",
      "type": "explore",
      "steps": ["识别首页主要入口", "点击「非危险」入口", "验证进入的页面有正确标题", "点击返回键回到首页"],
      "skip": ["支付", "下单", "删除", "退出登录"],
      "confirmed": false
    }}
  ],
  "analysis_notes": "整体说明，特别标注哪些是未确认推断"
}}

生成要求：
- paths 至少 1 条、最多 8 条；priority 只能是 high/medium/low；type 只能是 explore/regression。
- steps 必须是**语义化自然语言**（Midscene 靠语义定位），禁止写坐标、像素、x/y。
- steps 必须**原子化：一条步骤只做一件事**。这一步极其重要 ——
  执行引擎是视觉模型逐步规划，一条步骤里塞多个动作会让它反复重规划直到超限失败。
  ✅ 正确：「点击底部「我的」Tab」「点击「设置」」「关闭「推送通知」开关」
  ❌ 错误：「进入设置并关闭推送通知」「点击我的Tab然后进设置再关通知」
  ⚠️ **元素文字必须用「」括起来，位置和控件类型写在括号外**：
     ✅ 「点击底部「黄历」Tab」「点击右上角「添加」按钮」
     ❌ 「点击底部黄历Tab」（位置词「底部」会和元素名粘成一个不存在的名字）
  ❌ 错误：「切换到天气模式并验证日历网格显示天气图标」（拆成：切换 / 验证 两步）
  原则：**每个点击/输入/滑动/断言各占一条**；导航要写清层级（哪个 Tab → 哪个入口）。
  步骤总数控制在 12 条以内，超过说明该路径该拆成两条 path。
- ⭐⭐ **禁止生成"启动应用"类步骤，也禁止条件句**（2026-10-29 真实事故）：
  执行引擎在每条路径开跑前**已经自动拉起 App 并等界面稳定**，再写
  「启动App %s」「打开应用」只会让启动流程在手机上重复演一遍。
  ✅ 第一条步骤直接从"当前首页上要做什么"开始：「点击底部「黄历」Tab」
  ❌ 错误首步：「启动App com.xxx」「打开『万年历』应用」「等待首页加载完成」
  ❌ 绝对禁止条件句：「若出现开屏广告则点击「跳过」按钮」「如果有弹窗就关闭」
  —— 实测模型在当前页找不到「跳过」时，会**自己打开系统设置、甚至点广告拉起
  第三方 App** 去找这个按钮，表现为"执行时乱启动其他 App"。弹窗由引擎的
  全局弹窗策略统一处理，路径里不要写。
- ⭐ **候选必须前后一致，不能在"验证"里写「或」而在"点击"里写死其中一个**。
  2026-10-29 真实事故：同一条路径里写成
    验证步骤：「验证底部导航栏存在「择吉日」或「会员」其中一个 Tab」（有会员即通过 ✅）
    点击步骤：「点击底部「择吉日」Tab」（择吉日真机不存在 ❌ 必然失败）
  验证说"二者其一即可"，点击却只认其中一个 —— 路径自相矛盾，执行必挂。
  规则：
  ① **点击/进入类步骤只能写一个确定的入口名**，且必须是你**有把握真实存在**的那个；
  ② 若某入口的存在性不确定（如不同版本叫「择吉日」或「会员」），
     **不要把它写成点击目标**；应改为"验证"类步骤，或选择确定存在的入口；
  ③ 同一路径里，点击目标与验证目标必须指向同一个入口名，不允许一边「或」一边写死；
  ④ 严禁用「或」「/」连接**点击目标**（`点击「A」或「B」` 无法执行），
     这种并列只允许出现在**验证/断言**步骤里。
- 每条 path 的 skip 必须包含所有危险操作。
- **有业务资料时（见上方「业务资料」区块），必须以资料为依据生成针对性路径**：
  覆盖资料里明确写到的核心业务流程（如登录、下单、查询、设置等），
  并在每条 path 的 `evidence` 字段写清依据（引用资料中的页面名/流程名/原文关键词）。
  此时**不允许**只给"启动 + 首页入口探索"这种通用路径，除非资料确实没提到该方面。
- **没有业务资料时**，才退回通用探索路径，并在 analysis_notes 说明"缺少业务说明"，
  且**不要编造页面名称**。
- 资料中未提及、只靠 manifest 推断的内容，`confirmed` 必须为 false，
  并在 analysis_notes 里单独列出"未确认内容"。"""


class _RuntimeModel(object):
    """把 AiModeModelConfig 适配成 `AIModelService` 认识的属性形态。

    `AIModelService.call_openai_compatible_api` 读的是 `config.api_key /
    config.base_url / config.model_name / config.max_tokens / config.temperature`，
    并且它是需求分析模块的公共方法（很多地方在用），不能为了这里去改它。
    用这个薄适配层既不动公共代码，也不用把 AI 智能模式的配置写进 AIModelConfig 表。
    """

    __slots__ = ('api_key', 'base_url', 'model_name', 'max_tokens', 'temperature',
                 'top_p', 'role', 'name', 'model_type', 'model_family')

    def __init__(self, cfg):
        self.api_key = cfg.api_key
        self.base_url = cfg.resolved_base_url()
        self.model_name = cfg.model_name
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature
        # AIModelService.call_openai_compatible_api 会读 config.top_p；
        # 缺这个属性时它不是回退默认值，而是直接 AttributeError
        # （实测报 "'_RuntimeModel' object has no attribute 'top_p'"，
        #  表现为模型调用失败 → 整条链路降级成规则兜底）。
        self.top_p = getattr(cfg, 'top_p', 0.9)
        self.role = cfg.role
        self.name = cfg.name
        self.model_type = cfg.model_type
        self.model_family = cfg.resolved_family()

    def get_model_type_display(self):
        # 延迟导入：模块加载期不碰 ORM（apk_rea 有纯函数单测场景）
        try:
            from .models import AiModeModelConfig
            return dict(AiModeModelConfig.MODEL_CHOICES).get(self.model_type, self.model_type)
        except Exception:  # noqa: BLE001
            return self.model_type


def _model_config():
    """取分析用模型配置。

    2026-10-28 起**只认 AI 智能模式自己的配置**（AiModeModelConfig，role='analysis'）。
    不再读 requirement_analysis.AIModelConfig 的 analyzer/writer —— 用户明确要求
    「新配置只针对 AI 智能模式，旧配置继续服务其它功能」，两条链路必须彻底隔离，
    否则在任一入口启用模型都会影响另一条链路。
    """
    try:
        from .models import AiModeModelConfig, get_active_ai_mode_config
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-REA] 无法导入 AiModeModelConfig: %s', exc)
        return None, '无法加载 AI 智能模式模型配置（导入失败）'

    cfg, err = get_active_ai_mode_config(AiModeModelConfig.ROLE_ANALYSIS)
    if not cfg:
        return None, err
    return _RuntimeModel(cfg), ''


def _apk_info_block(artifact):
    """把 APK 解析结果整理成给模型的紧凑文本。"""
    info = artifact.manifest_info or {}
    lines = [
        '包名: %s' % (artifact.package_name or '(未解析)'),
        '版本: %s (versionCode=%s)' % (artifact.version_name or '?', artifact.version_code),
        'minSdk/targetSdk: %s/%s' % (artifact.min_sdk, artifact.target_sdk),
        '启动 Activity: %s' % (artifact.launch_activity or '(未解析)'),
        'DEX 数量: %s，含原生库: %s' % (info.get('dex_count'), info.get('has_native_libs')),
        '声明权限(%d): %s' % (len(info.get('permissions') or []),
                            ', '.join((info.get('permissions') or [])[:40])),
        '危险权限: %s' % ', '.join(
            '%s(%s)' % (d.get('name', '').split('.')[-1], d.get('label', ''))
            for d in (info.get('dangerous_permissions') or [])) or '无',
        'Activity(%d): %s' % (len(info.get('activities') or []),
                              ', '.join((info.get('activities') or [])[:30])),
        '允许明文流量: %s' % info.get('has_cleartext'),
    ]
    return '\n'.join(lines)


def _extract_json(text):
    """从模型回复里抠出 JSON（兼容 ```json 包裹 / 前后有解释文字）。"""
    if not text:
        return None
    text = text.strip()
    # 去 markdown 代码块
    fence = re.match(r'^```(?:json)?\s*(.+?)\s*```$', text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # 兜底：截取第一个 { 到最后一个 }
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _norm_priority(value):
    v = str(value or '').strip().lower()
    if v in ('high', 'medium', 'low'):
        return v
    # 中文/其他写法归一
    if v in ('高', 'p0', 'p1', 'critical'):
        return 'high'
    if v in ('中', 'p2', 'normal'):
        return 'medium'
    if v in ('低', 'p3', 'minor'):
        return 'low'
    return 'medium'


def _norm_path_type(value):
    v = str(value or '').strip().lower()
    if v in ('explore', 'regression'):
        return v
    if '回归' in v:
        return 'regression'
    return 'explore'


#: 视为"关闭"的字符串取值（前端/JSON 可能传字符串）
_FALSE_TOKENS = {'false', '0', 'no', 'off', 'n', ''}


def _coerce_bool(value, default=True):
    """把 JSON/表单里的各种真假值统一成 bool。

    为什么不用 `value is not False`：`0`、`'false'`、`''` 都是"假值"但
    `0 is not False` 为 True —— 会让"关闭"静默失效（踩过一次）。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TOKENS
    return bool(value)


#: 复合步骤的「连接词」模式：一句里出现这些词，往往藏着 2 个以上动作。
#: 例：「点击设置并关闭通知」= 点设置 + 关通知 两件事，
#: 交给 Midscene 一条 aiAct 去干，它要自己规划 20 轮才可能做完
#: （实测就抛 Replanned 20 times）。拆成两条后每步 2~3 轮即可。
_STEP_SPLIT_PATTERN = re.compile(
    r'(?:，|,|；|;|。)?\s*(?:然后|随后|接着|之后|再|并且|并|同时|最后|以及)\s*'
)

#: 拆分后仍然算"一个动作"的最短长度；太短的多半是虚词，不拆
_MIN_ATOM_LEN = 4

#: sanitize_paths 的副产品：索引 → 该路径被拆开的复合步骤数。
#: 不放进 path 字典，是因为 paths 参与确认指纹（FINGERPRINT_FIELDS），
#: 多一个统计字段会让指纹口径变脏、无谓触发"需重新确认"。
#: 用法：paths = sanitize_paths(...); n = sum(split_stats.values())
split_stats = {}


def split_compound_step(text, max_parts=4):
    """把一条复合步骤拆成若干原子步骤。

    只在「确实能拆干净」时才拆：拆出的片段数量超过 max_parts、
    或有片段短于 _MIN_ATOM_LEN，都退回原文 —— 宁可让模型多跑几轮，
    也不要把一句话拆成语义残缺的碎片（碎片会让定位直接失败）。

    Returns:
        list[str]  至少 1 项；无法拆分时返回 [原文]
    """
    text = str(text or '').strip()
    if not text:
        return []
    parts = [p.strip(' ，,；;。') for p in _STEP_SPLIT_PATTERN.split(text)]
    parts = [p for p in parts if p]
    if len(parts) < 2 or len(parts) > max_parts:
        return [text]
    if any(len(p) < _MIN_ATOM_LEN for p in parts):
        return [text]
    # 拆完如果是同一句话被重复切出（连接词误伤），也退回原文
    if len(set(parts)) != len(parts):
        return [text]
    return parts


def sanitize_paths(raw_paths, dangerous_actions=None):
    """规范化模型输出的 paths，并强制合并危险跳过项。

    这一步是「模型输出不可信」的落地防线：
    - 缺字段的路径补齐默认值，而不是丢弃（丢一条 = 少测一块功能）
    - 步骤里出现坐标形状的内容直接标 invalid（交人工在确认页改）
    - skip 一定包含 DEFAULT_SKIP + 模型给出的危险操作名
    """
    extra_skip = []
    for d in (dangerous_actions or []):
        name = str((d or {}).get('name') or '').strip()
        if name:
            extra_skip.append(name)

    out = []
    dropped_launch = 0
    # 索引 → 该路径被拆开的复合步骤数。只在本函数内累积，由返回值外的
    # 调用方按 out 的索引读取（见 split_stats 的用法说明）。
    split_stats.clear()
    for i, p in enumerate(raw_paths or []):
        if not isinstance(p, dict):
            continue
        steps = []
        invalid_reasons = []
        split_count = 0
        for s in (p.get('steps') or []):
            text = str(s or '').strip()
            if not text:
                continue
            # 与 midscene_engine._guard_semantic_prompt 同口径：坐标会换机型失效
            if re.search(r'\d{1,5}\s*[,，]\s*\d{1,5}', text):
                invalid_reasons.append('步骤含坐标写法：%s' % text[:40])
            # 点击目标不能是并列候选（`点击「A」或「B」` 无法执行）。
            # 2026-10-29 事故的同源问题：模型在"验证"里写「A」或「B」是对的
            # （任一成立即通过），但把同样的并列写进**点击**就必然失败 ——
            # 执行时只能点一个具体元素，二选一的句子没有可执行目标。
            # 这里只标记不丢弃：交人工在确认页改成一个确定入口。
            if _CLICK_OR_TARGET.search(text):
                invalid_reasons.append(
                    '点击目标含并列候选（无法执行，请改成确定的一个入口）：%s'
                    % text[:40])
            # ⭐ 剔除"启动应用"类步骤：引擎在每条路径开跑前已经 reset_to_home
            # 拉起过 App，路径里再写一次会让启动流程演两遍（用户反馈"为什么每步
            # 都在重复同样的操作"）。只在**第一步**剔除是安全的 —— 中途的"返回
            # 首页/重新进入"仍有意义，不动。
            if not steps and _APP_LAUNCH_STEP.search(text):
                dropped_launch += 1
                continue
            # ⭐ 条件句（「若出现开屏广告则点击「跳过」」）会把模型推向
            # "找不到就去别的 App 找"，见 apk_rea 顶部说明。这里只标记不丢弃：
            # 交人工在确认页决定改还是删，静默删除会让用户以为路径被吞了。
            if _CONDITIONAL_STEP.search(text):
                invalid_reasons.append(
                    '条件句易诱发模型离开本 App（建议改为确定步骤或删除）：%s'
                    % text[:40])
            # 原子化：把「点击设置并关闭通知」这类复合句拆成两句。
            # 交给 Midscene 一条 aiAct 干多件事时它会反复重规划，
            # 超过 20 轮就整步失败（Replanned 20 times）。
            atoms = split_compound_step(text)
            if len(atoms) > 1:
                split_count += len(atoms) - 1
            steps.extend(atoms)
        if not steps:
            continue

        skip = []
        for s in list(p.get('skip') or []) + DEFAULT_SKIP + extra_skip:
            s = str(s or '').strip()
            if s and s not in skip:
                skip.append(s)

        out.append({
            'name': str(p.get('name') or '路径%d' % (i + 1)).strip()[:100],
            'priority': _norm_priority(p.get('priority')),
            'type': _norm_path_type(p.get('type')),
            'steps': steps,
            'skip': skip,
            'confirmed': bool(p.get('confirmed')),
            # 路径启停开关：默认启用。关闭后真机执行直接跳过该路径（不计入失败），
            # 名称/步骤都保留，方便临时排除某条不稳定路径而不删掉它。
            # 注意：**不参与确认指纹**（见 ApkRegressionRun.FINGERPRINT_FIELDS），
            # 开关是运行策略而不是执行内容，打开/关闭不该被要求重新确认。
            #
            # ⚠️ 不能用 `p.get('enabled', True) is not False`：`0`、`'false'`、`''`
            # 这些"假值"都会漏过（`0 is not False` 为 True），导致前端传 0 时关不掉。
            # 统一走 _coerce_bool 做显式转换。
            'enabled': _coerce_bool(p.get('enabled'), default=True),
            'evidence': str(p.get('evidence') or '').strip()[:300],
            'invalid_reasons': invalid_reasons,
        })
        # ⚠️ 拆分数量不能塞进 path 字典里：paths 会参与确认指纹
        # （FINGERPRINT_FIELDS），多一个统计字段会把指纹口径搞脏。
        # 用 out 的长度做稳定索引，交给调用方按需读取。
        split_stats[len(out) - 1] = split_count
    if dropped_launch:
        logger.info('[REA] 已剔除 %d 条冗余的"启动应用"步骤（引擎会自动启动）',
                    dropped_launch)
    return out


def _rule_based_analysis(artifact, reason=''):
    """规则兜底：不依赖 LLM 也能产出一条可执行路径。

    为什么必须要它：MVP 的第一价值是「能跑通一次真机回归」，
    如果因为模型余额/网络问题就完全没有路径，用户连「人工确认」这一步
    都看不到，整个功能等于不可用。兜底路径只依赖 manifest 里确实存在的
    信息（包名 + 启动 Activity），不臆造任何页面。
    """
    info = artifact.manifest_info or {}
    pkg = artifact.package_name or ''
    launch = artifact.launch_activity or ''
    acts = info.get('activities') or []
    dangerous = [{'name': d.get('label') or d.get('name', '').split('.')[-1],
                  'reason': 'APK 声明了危险权限 %s' % d.get('name'),
                  'level': 'high'}
                 for d in (info.get('dangerous_permissions') or [])]

    # 首步不写「启动应用」：执行引擎每条路径开跑前已经拉起 App 并等界面稳定，
    # 再写一次会让启动流程演两遍（2026-10-29 用户反馈"每步都在重复同样的操作"）。
    paths = [{
        'name': '启动与首页入口探索',
        'priority': 'high',
        'type': 'explore',
        'steps': [
            '识别当前页面标题',
            '识别首页的主要可点击入口（最多列出 5 个）',
            '点击第一个非危险入口并识别进入的页面类型',
            '返回首页',
        ],
        'skip': list(DEFAULT_SKIP),
        'confirmed': False,
        'evidence': 'manifest 启动 Activity: %s；Activity 共 %d 个' % (launch or '(未解析)', len(acts)),
        'invalid_reasons': [],
    }]
    if len(acts) > 1:
        paths.append({
            'name': 'Activity 可达性巡检',
            'priority': 'medium',
            'type': 'explore',
            'steps': [
                '依次访问清单中声明的非危险页面入口：%s' % ', '.join(acts[1:6]),
                '每进入一个页面，记录页面标题与可点击元素数量',
                '返回首页',
            ],
            'skip': list(DEFAULT_SKIP),
            'confirmed': False,
            'evidence': 'manifest 声明了 %d 个 Activity' % len(acts),
            'invalid_reasons': [],
        })

    return {
        'app': pkg,
        'paths': sanitize_paths(paths, dangerous),
        'requirements': [],
        'pages': [],
        'dangerous_actions': dangerous,
        'exception_pages': [],
        'sensitive_pages': [],
        'analysis_notes': ('已降级为规则分析（未调用大模型）：%s。'
                           '生成的路径仅基于 APK 清单中确认存在的信息，'
                           '请人工补充具体业务路径后再确认。' % (reason or '未提供原因')),
        'llm_error': reason or '',
        'source': 'rule',
        # 规则兜底不消费业务资料，但把"有没有资料"如实标出来，
        # 否则用户会觉得"我明明传了操作说明，怎么还是通用路径"。
        'context_sources': [],
        'context_chars': 0,
        'context_empty': True,
        'context_note': '规则兜底不解析业务资料，仅模型分析时会使用',
    }


def analyze_apk(artifact, timeout=180.0, run=None):
    """对 APK 做 REA 分析，返回路径清单字典（永不抛异常）。

    Args:
        artifact: ApkArtifact 实例（必须已有 manifest_info）
        timeout: LLM 调用读取超时（秒）。分析比生成用例轻，180s 足够。
        run: 可选的 ApkRegressionRun —— 提供时会把它的业务资料
             （context_docs / context_notes / project 知识库）拼进提示词，
             生成针对真实业务的路径；不提供则退化为"仅 APK 清单"模式。

    Returns:
        dict，含 app/paths/requirements/pages/dangerous_actions/exception_pages/
        sensitive_pages/analysis_notes/llm_error/source/context_sources
    """
    if artifact.parse_error:
        result = _rule_based_analysis(artifact, 'APK 解析失败：%s' % artifact.parse_error)
        result['paths'] = []
        return result

    cfg, cfg_err = _model_config()
    if cfg is None:
        return _rule_based_analysis(artifact, cfg_err)

    # 业务资料（操作说明 / 知识库 / 人工补充）。为空时明确告诉模型"没有资料"，
    # 让它退回通用路径而不是凭空编页面名 —— 与加资料前的行为保持一致。
    try:
        from .apk_context import build_business_context
        ctx = build_business_context(run) if run is not None else {
            'text': '', 'sources': [], 'chars': 0, 'empty': True}
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-REA] 读取业务资料失败（按无资料继续）: %s', exc)
        ctx = {'text': '', 'sources': [], 'chars': 0, 'empty': True}

    if ctx.get('text'):
        business_section = (
            '【业务资料】（以下内容来自用户提供的操作说明/需求文档、项目知识库条目'
            '与项目文件知识库文档，是生成测试路径的**主要依据**）\n' + ctx['text'])
    else:
        business_section = (
            '【业务资料】无。本次只有 APK 静态清单可用，'
            '请按"静态信息不足"的要求生成通用路径，并说明缺少哪些业务信息。')

    prompt = PROMPT_TEMPLATE.format(apk_info=_apk_info_block(artifact),
                                    business_section=business_section)

    import asyncio
    from apps.requirement_analysis.ai_model_service import AIModelService

    async def _call():
        return await asyncio.wait_for(
            AIModelService.call_openai_compatible_api(
                cfg,
                [{'role': 'user', 'content': prompt}],
                timeout=timeout,
            ),
            timeout=timeout + 30,
        )

    try:
        resp = asyncio.run(_call())
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-REA] 模型调用失败: %s', exc)
        return _rule_based_analysis(artifact, '模型调用失败：%s' % str(exc)[:200])

    try:
        content = resp['choices'][0]['message']['content']
    except Exception:  # noqa: BLE001
        return _rule_based_analysis(artifact, '模型返回格式异常（缺少 choices[0].message.content）')

    data = _extract_json(content)
    if not isinstance(data, dict):
        logger.warning('[APK-REA] 模型输出无法解析为 JSON，前 200 字: %s', (content or '')[:200])
        return _rule_based_analysis(
            artifact, '模型输出不是合法 JSON（前 120 字：%s）' % (content or '')[:120])

    dangerous = data.get('dangerous_actions') or []
    paths = sanitize_paths(data.get('paths') or [], dangerous)
    if not paths:
        fallback = _rule_based_analysis(artifact, '模型未给出可用路径')
        fallback['analysis_notes'] = (
            '模型已返回结果但没有可用路径（steps 为空或全被过滤），'
            '已自动补充规则兜底路径。' + (fallback.get('analysis_notes') or ''))
        # 保留模型给出的其他有效信息
        for key in ('requirements', 'pages', 'exception_pages', 'sensitive_pages'):
            if data.get(key):
                fallback[key] = data[key]
        return fallback

    notes = str(data.get('analysis_notes') or '').strip()
    unconfirmed = [p['name'] for p in paths if not p.get('confirmed')]
    if unconfirmed:
        notes = (notes + '\n' if notes else '') + (
            '以下路径为未确认推断，请人工核对后再批准：%s' % '、'.join(unconfirmed))

    # 复合步骤被自动拆开时告知用户（拆得对不对需要人看一眼）
    split_total = sum(int(v or 0) for v in split_stats.values())
    if split_total:
        notes = (notes + '\n' if notes else '') + (
            '已自动把 %d 处复合步骤拆成原子步骤（避免真机执行时单步反复重规划超限），'
            '建议在确认页核对拆分是否符合预期。' % split_total)

    return {
        'app': str(data.get('app') or artifact.package_name or ''),
        'paths': paths,
        'requirements': data.get('requirements') or [],
        'pages': data.get('pages') or [],
        'dangerous_actions': dangerous,
        'exception_pages': data.get('exception_pages') or [],
        'sensitive_pages': data.get('sensitive_pages') or [],
        'analysis_notes': notes,
        'llm_error': '',
        # 明确标注来源是「AI 智能模式的独立配置」，排障时一眼能看出用的哪套
        'source': 'ai-mode:%s/%s' % (cfg.role, cfg.model_name),
        'model_config_id': getattr(cfg, 'id', None),
        'model_config_name': getattr(cfg, 'name', ''),
        # 本次分析用到了哪些业务资料（前端展示"依据"，也便于复盘"为什么生成这些路径"）
        'context_sources': ctx.get('sources') or [],
        'context_chars': ctx.get('chars') or 0,
        'context_empty': bool(ctx.get('empty')),
    }
