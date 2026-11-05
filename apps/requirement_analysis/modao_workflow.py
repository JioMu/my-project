# -*- coding: utf-8 -*-
"""
墨刀技能 - 5 阶段工作流编排器（后端落地版）

忠实复用现成技能包 modao_skill/ 的 prompts/workflow，映射到 TestHub 后端：
  阶段0 墨刀读取   -> ModaoExtractor（Playwright 逐页 + 注释 + 截图 + 视觉OCR）
  阶段1 需求通读   -> p0_requirement_struct（YAML 结构化摘要）
  阶段2 需求澄清   -> p0 复用（识别模糊/缺失/边界 -> 澄清清单）
  阶段3 用例设计   -> p1拆分 / p2测试点 / p5风险 / p6 PCI / p7三路合并(向量去重) /
                      p3用例 / p4冒烟 / p8质量自检 / Excel导出
  阶段4 冒烟执行   -> 由前端驱动用户决策，后端记录 passed/rejected

向量去重（方案 A）：p7 之前用 Chroma(bge-m3) 对三路测试点做语义近邻去重。
"""
import os
import json
import uuid
import logging
import asyncio
import datetime
import re
import urllib.parse
import time
import functools

import numpy as np

from apps.requirement_analysis.modao_prompts import (
    render_prompt,
    P0_REQUIREMENT_STRUCT, P1_MODULE_SPLIT, P2_TESTPOINT_GEN, P3_CASE_GEN,
    P4_SMOKE_EXTRACT, P5_RISK_IDENTIFY, P6_PCI_IDENTIFY, P7_MERGE_DEDUP, P8_QUALITY_CHECK,
)
from apps.requirement_analysis.vector_store import (
    get_merge_collection, release_merge_collection,
)
from apps.requirement_analysis.models import AIModelService
from apps.requirement_analysis.modao_extractor import ModaoTerminateError
from apps.requirement_analysis.cancel_flags import TERMINATE_REQUESTED

logger = logging.getLogger(__name__)


# ---------- 阶段耗时埋点（写文件，不依赖 Django 日志配置）----------
def _wf_tlog(proto_id, msg, t0=None):
    """把阶段耗时写入 /app/media/modao/proto_<id>_timing.log，供排障分析。"""
    try:
        path = os.path.join('media', 'modao', f'proto_{proto_id}_timing.log')
        if t0 is not None:
            msg = f'{msg} | 耗时 {time.time() - t0:.1f}s'
        line = f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {msg}\n'
        with open(path, 'a') as f:
            f.write(line)
    except Exception:
        pass


def _timed_stage(label):
    """装饰器：记录某个工作流阶段的开始/结束与墙钟耗时。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **k):
            pid = self.prototype.id
            t0 = time.time()
            _wf_tlog(pid, f'▶ STAGE {label} START')
            try:
                return fn(self, *a, **k)
            finally:
                _wf_tlog(pid, f'■ STAGE {label} END', t0)
        return wrapper
    return deco


MEDIA_ROOT = os.environ.get('MEDIA_ROOT', 'media')
DEDUP_THRESHOLD = 0.80  # 余弦相似度阈值（中文需求语义去重，优化后降低避免漏网）


# ---------- 解析工具 ----------
def _extract_block(content: str, lang: str = None) -> str:
    """从 LLM 输出中提取首个 ```lang 代码块。宽松兼容：
    - 标记后不紧跟换行（如 ` ```json{...} `）
    - 缺少结束 ``` 标记（截至文末）
    - 无代码块时返回原文（供 _parse_json/_parse_yaml 直接解析裸 JSON/YAML）

    ⚠️ 2026-11-04 修正（proto=92 事故）：文档**本身已是完整裸 YAML/JSON** 时，
    绝不能因为正文中途出现 ```（例如某个字段原文里带了代码围栏）就把它当边界截断。
    旧行为在这些场景下只返回「首个围栏内的几千字」，把 27k 行的全局摘要截成 1 个模块。
    现策略：整份内容本身能解析成功就直接用整份；解析不了才回退到「提取首个代码块」。
    """
    if not content:
        return ''
    import re
    # 先判断整份内容是否本身就是合法载荷（裸 YAML/JSON），是则不做任何截取
    body = content.strip()
    if body and not body.startswith('```'):
        try:
            if lang and re.search(r'(yaml|yml)', lang, re.I):
                import yaml as _yaml
                if isinstance(_yaml.safe_load(_strip_yaml_fence(body)), (dict, list)):
                    return _strip_yaml_fence(body)
            elif lang and re.search(r'json', lang, re.I):
                if isinstance(json.loads(body), (dict, list)):
                    return body
        except Exception:
            pass
    # 结束定界符：优先 ```，否则匹配到字符串末尾（兼容未闭合代码块）
    end = r'(?:```|$)'
    if lang:
        pat = re.compile(r'```' + lang + r'\s*\n?' + r'(.*?)' + end, re.DOTALL | re.IGNORECASE)
        m = pat.search(content)
        if m:
            return m.group(1).strip()
    # 通用：匹配 ```yaml / ```json / ```yml 或无语言标记的 ``` 代码块
    pat = re.compile(r'```(?:yaml|json|yml)?\s*\n?' + r'(.*?)' + end, re.DOTALL | re.IGNORECASE)
    m = pat.search(content)
    if m:
        return m.group(1).strip()
    return content.strip()


def _repair_yaml_text(text: str) -> str:
    """修复 LLM 常见的「引号半包裹」坏行。

    形如 ``- "即将上新"占位选项，灰色不可选`` 的行：引号只包住了前半段，
    后半段裸文本紧跟在闭合引号之后，属于非法 YAML，会让 ``yaml.safe_load``
    直接抛 ``ParserError``，进而整份文档解析失败（所有 module 被丢弃）。

    这里只对这类「引号成对出现、但闭合引号后仍有内容」的行做整体加引号修复，
    合法 YAML 行原样透传，不做任何其他改动。

    另修一类（2026-11-04 proto=92 实测，画布「个人-个人中心」整段因此解析失败）：
    ``priority_reason: "…新增"编辑篇章"入口…"`` —— 双引号字符串**内部又出现未转义双引号**。
    判据：值以引号开头且以同种引号结尾，但中间还存在该引号。
    修法：保留首尾引号，把中间的同种引号替换成中文引号（不改语义、不引入转义转义问题）。
    """
    import json as _json
    import re as _re
    out = []
    for line in (text or '').split('\n'):
        # 兼容列表项形如 `- "xxx" yyy` 或 键值形如 `key: "xxx" yyy`
        m = _re.match(r'^(\s*(?:-\s*|[\w_]+:\s*))(["\'])(.*?)\2(.*)$', line)
        # ⚠️ 排除 YAML 单引号转义串 `'''xxx''yy'`：它以两个同种引号开头，
        # 属于合法写法，不能按「引号半包裹」重写（2026-11-04 proto=92 画布1 误伤）
        # 判据直接看「前缀之后是否紧跟两个同种引号」（group(3) 可能为空串，不能用它判）
        if m:
            q_m = m.group(2)
            rest = line[len(m.group(1)):]
            if rest.startswith(q_m * 2):
                out.append(line)
                continue
        if m and m.group(4).strip():
            # 引号后还有裸文本：把引号内 + 引号外拼接后整体 JSON 转义（JSON 字符串是合法 YAML 标量）
            out.append(m.group(1) + _json.dumps(m.group(3) + m.group(4), ensure_ascii=False))
            continue
        # 值已用引号包裹，但内部还夹着同种引号 → 内部引号换成中文引号
        # 形态覆盖：`key: "…"`、`- "…"`、`- key: "…"`（含尾随多余引号）
        # ⚠️ 必须严格限定「值紧跟分隔符就是引号」，否则会误伤：
        #   - `- 角色创作模块新增"心动角色测验"草稿类型…`（裸值，引号在中间，本已合法）
        #   - `issue: '''下次进入页面''是否指…'`（三引号，本已合法）
        m2 = _re.match(r'^(\s*(?:-\s+)?[\w_\u4e00-\u9fa5]+\s*:\s*|^\s*-\s+)(["\'])(.*)$', line)
        if m2 and m2.group(2) in m2.group(3):
            q, body = m2.group(2), m2.group(3)
            # 三引号开头（YAML 里等价于单引号转义串）不处理，避免语义被改
            if body.startswith(q):
                out.append(line)
                continue
            # body 形如 `内容"编辑篇章""` 或 `内容"` —— 去掉尾部所有多余的同类引号，
            # 只保留我们最后补的那一个
            while body.endswith(q):
                body = body[:-1]
            fixed = body.replace(q, '\u201c' if q == '"' else '\u2018')
            out.append(m2.group(1) + q + fixed + q)
            continue
        out.append(line)
    return '\n'.join(out)


def _strip_yaml_fence(text: str) -> str:
    """剥掉整份文档外层残留的 ```yaml / ``` 代码围栏。

    背景（2026-11-04 生产 proto=92 实测）：落库的 requirement_summary / page_summary
    偶发把 LLM 原样输出连代码围栏一起存了下来，`yaml.safe_load` 在围栏行直接抛
    ``found character '`' that cannot start any token``，整份 27k 行文档解析失败，
    阶段3 模块拆分会退化成「0 个模块」或只吃到某段围栏内的内容。

    只处理「首行是围栏」与「末行是围栏」两种情形：
    - 首行围栏 + 末行围栏 → 去掉首尾行；
    - 仅首行围栏（末行被截断）→ 去掉首行。
    文档中间的围栏不动（那是内容，不是边界），以免误删正文。
    """
    if not text:
        return ''
    lines = text.split('\n')
    # 找到首个非空行
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].strip().startswith('```'):
        lines = lines[start + 1:]
        # 末尾若也是围栏则一起去掉
        end = len(lines) - 1
        while end >= 0 and not lines[end].strip():
            end -= 1
        if end >= 0 and lines[end].strip().startswith('```'):
            lines = lines[:end]
        return '\n'.join(lines).strip()
    return text


_PAGE_MARK_RE = re.compile(r'^#\s*=====\s*画布：(.*?)\s*=====\s*$', re.MULTILINE)


def _split_page_blocks(text: str):
    """把「多画布拼接视图」切成 [(画布名, yaml 片段), ...]。

    背景（2026-11-04 生产 proto=92 实测）：`_merge_page_summaries()` 把各画布 page_summary
    用 ``# ===== 画布：<名字> =====`` 注释直接拼成一段文本存进 requirement_summary。
    这意味着 requirement_summary **不是单个 YAML 文档**，而是 19 份文档首尾相接，
    每份都从 ``version:`` 重新开始（顶层键重复 19 次）。
    整份 `yaml.safe_load` 的后果：
      ① 只要任一段带代码围栏/非法行，整份解析失败 → modules=0 → 阶段3 只拆出 0 个模块；
      ② 即使侥幸解析成功，重复顶层键也只保留最后一份 → 只有 1 个画布的模块。
    所以必须先切段、逐段解析、再合并。
    """
    if not text:
        return []
    marks = list(_PAGE_MARK_RE.finditer(text))
    if not marks:
        return []
    blocks = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        blocks.append((m.group(1).strip(), text[start:end]))
    return blocks


def _merge_parsed_docs(docs):
    """把多份画布 YAML 合并成单一 dict：modules/key_features 等列表累加，标量取首个非空。"""
    merged = {}
    for name, doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if isinstance(val, list):
                merged.setdefault(key, [])
                if isinstance(merged[key], list):
                    merged[key].extend(val)
            elif isinstance(val, dict):
                bucket = merged.setdefault(key, {})
                if isinstance(bucket, dict):
                    for k2, v2 in val.items():
                        bucket.setdefault(k2, v2)
            else:
                # 标量：保留首个非空值，不覆盖
                if key not in merged or merged[key] in (None, '', []):
                    merged[key] = val
    return merged


def _parse_yaml_docs(text: str):
    """解析可能由多画布拼接而成的 YAML 视图。

    返回 (merged_dict, blocks_info)。blocks_info 记录每段是否解析成功，供上层日志/审计。
    单文档输入时等价于 _parse_yaml。
    """
    raw = text or ''
    blocks = _split_page_blocks(raw)
    if not blocks:
        return None, []
    info, docs = [], []
    for name, body in blocks:
        body_clean = _strip_yaml_fence(body)
        parsed = _parse_yaml(body_clean)
        ok = isinstance(parsed, dict)
        info.append({'page': name, 'ok': ok, 'bytes': len(body_clean)})
        if ok:
            parsed.setdefault('_page_name', name)
            docs.append((name, parsed))
    if not docs:
        return None, info
    return _merge_parsed_docs(docs), info


def _parse_yaml(text: str):
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception as e:
        # 容错一：外层代码围栏（落库内容带 ```yaml 时会导致整份解析失败）
        stripped = _strip_yaml_fence(text or '')
        if stripped != (text or ''):
            try:
                import yaml
                result = yaml.safe_load(stripped)
                if result is not None:
                    logger.warning('YAML 外层代码围栏已自动剥离后解析成功')
                    return result
            except Exception:
                pass
        # 容错二：修复「引号半包裹 / 引号内嵌引号」坏行后重试
        # （基于剥完围栏的文本修，避免围栏行干扰；两者都不需要时返回原文）
        base = stripped if stripped != (text or '') else (text or '')
        try:
            import yaml
            repaired = _repair_yaml_text(base)
            if repaired != base:
                result = yaml.safe_load(repaired)
                if result is not None:
                    logger.warning('YAML 引号坏行已自动修复后解析成功')
                    return result
        except Exception:
            pass
        logger.warning(f'YAML 解析失败: {e}')
        return None


def _parse_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        # 容错：尝试提取首个 { ... } 块
        import re
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return None

def _normalize_evidence_text(value) -> str:
    """Normalize text for conservative evidence matching."""
    text = str(value or '').lower()
    return re.sub(r'[\s，。；：、,.!?！？:;（）()【】\[\]"""\'''_—-]+', '', text)


def _page_has_requirement_annotation(page: dict) -> bool:
    """判断一个画布是否带有**本期需求增量证据**（用于 G6 未覆盖分母）。

    实测口径（proto=70/71/82/85/88/89/92/93 全量核对）：
      - ``annotations`` 字段在所有实测任务中恒为空列表，批注实际被并入 ``text``
        的 ``[便利贴批注]`` 段落，因此必须查 text 而不是只看该字段；
      - ``visual_artifacts`` **不能整体当判据**：其字段名是 ``artifact_type``（不是 type），
        且大量条目是 ``ui_screen``（底模线框图外观，按 p0 硬约束 16 不是业务规则）
        与 ``list_spec``（如各画布重复出现的「HanaKoi V1.10 导航菜单」全局导航清单，
        不属于任何单个画布的变更）。若整体计入会把全部画布判成"有变更"
        （实测 proto=93 会算出 63% 误阻断）。

    因此依次检查：
      1) ``annotations`` 非空；
      2) ``text`` 含 ``[便利贴批注]`` / ``[需求说明]`` 等段落且段落内确有内容；
      3) ``visual_artifacts`` 中存在**规则型**结构：annotation / rule_list /
         state_matrix / data_table / decision_table / interaction_flow，
         且其 raw_text 有实质长度（排除仅复述页面标题的空壳结构）。
    """
    if not isinstance(page, dict):
        return False
    if page.get('annotations'):
        return True

    text = str(page.get('text') or '')
    for marker in ('[便利贴批注]', '[批注]', '[需求说明]'):
        idx = text.find(marker)
        if idx >= 0 and len(text[idx + len(marker):].strip()) >= 8:
            return True

    _RULE_TYPES = {
        'annotation', 'rule_list', 'state_matrix', 'data_table',
        'decision_table', 'interaction_flow', 'pricing_matrix',
    }
    for va in (page.get('visual_artifacts') or []):
        if not isinstance(va, dict):
            continue
        atype = str(va.get('artifact_type') or va.get('type') or '').lower()
        if atype not in _RULE_TYPES:
            continue
        if len(str(va.get('raw_text') or '').strip()) >= 10:
            return True
        if va.get('rows') or va.get('states') or va.get('steps') or va.get('rules'):
            return True
    return False


def _repair_source_coverage(all_fused_requirements, valid_pages):
    """Stage-0 G6 确定性覆盖补全。

    融合模型常把同一画布内靠 tab 切换显示的子页内容并入父页需求，
    却不把每个子页名写进 ``source_pages``。这些子页内容其实已存在于融合结果中，
    仅归因标签缺失，却会让 G6（未覆盖页占比 >20%）误判阻断。

    本函数在 G6 检查前，把"确有 DOM/视觉内容、但未被任何需求标注"的页面，
    归因到内容最匹配的需求上，确保每一页都在融合结果中有来源证据。

    ⚠️ 2026-11-04 修正（proto=93「搜索」模块实测）：
    ``source_pages`` 里实际混了两种东西：
      ① 主画布名，如 ``浏览-搜索``；
      ② 该画布的**页面状态按钮名**，如 未聚焦 / 点击聚焦 / 用户输入 / 搜索结果 / 无结果
         （融合模型把 tab 按钮当页名写了进来，它们不是独立画布）。
    一条 fused 需求的 ``interaction`` 本身就是「整个模块在所有状态下的完整需求文本」，
    所以 source_pages 列多个状态名是正确的、必须保留，不能按"来源页数量"过滤。

    真正有害的是原第 3) 兜底"归因到文本最长的需求"：它会把**与该模块毫无业务关系的
    其它主画布**（annotations=0、本期无任何变更标注）也追加进 source_pages，
    而下游阶段1 逐画布结构化正是凭 source_pages 取交集（见 _source_text_for_pages），
    于是该模块需求被注入到每一个画布，造成跨画布串内容
    （实测 page5「浏览-创作者信息」摘要混入搜索模块、产出 19 条无关测试点）。

    修正后只保留**有真实内容关联**的归因：
      1) 需求正文直接包含该页名；
      2) 已标注页与该页存在父子/包含关系（tab 状态名/子页的典型形态）。
    原来的"最长需求"兜底改为**只打 ``coverage_unattributed`` 标记、不写入 source_pages**。
    """
    if not isinstance(all_fused_requirements, list) or not valid_pages:
        return
    covered = set()
    for item in all_fused_requirements:
        if isinstance(item, dict):
            for pn in item.get('source_pages') or []:
                covered.add(_normalize_evidence_text(pn))

    uncovered = [
        p for p in valid_pages
        if isinstance(p, dict)
        and _normalize_evidence_text(p.get('page_name')) not in covered
    ]
    if not uncovered:
        return

    repaired, weak = [], []
    for page in uncovered:
        pname = page.get('page_name', '')
        norm = _normalize_evidence_text(pname)
        if not norm:
            continue
        target = None
        # 1) 某条需求文本直接包含该页名（强信号）
        for i, item in enumerate(all_fused_requirements):
            if not isinstance(item, dict):
                continue
            if norm in json.dumps(item, ensure_ascii=False).lower():
                target = i
                break
        # 2) 某条需求已标注的页面与该页存在父子/包含关系（子页名包含父页名或反之）
        if target is None:
            for i, item in enumerate(all_fused_requirements):
                if not isinstance(item, dict):
                    continue
                for sp in (item.get('source_pages') or []):
                    spn = _normalize_evidence_text(sp)
                    if spn and (norm.startswith(spn) or spn.startswith(norm)
                                or norm in spn or spn in norm):
                        target = i
                        break
                if target is not None:
                    break
        # 3) 无任何内容关联：只打弱覆盖标记，绝不写入 source_pages
        #    （原"归因到文本最长的需求"会把无关画布全部挂到同一模块上，污染下游画布归属）
        if target is not None and isinstance(all_fused_requirements[target], dict):
            sp = all_fused_requirements[target].setdefault('source_pages', [])
            if pname not in sp:
                sp.append(pname)
                repaired.append(pname)
        else:
            page['coverage_unattributed'] = True
            weak.append(pname)
    if repaired:
        logging.getLogger(__name__).info(
            '[STAGE0] 覆盖补全：将 %d 个未被标注的页面归因到融合需求：%s',
            len(repaired), '、'.join(repaired)
        )
    if weak:
        logging.getLogger(__name__).warning(
            '[STAGE0] 覆盖补全：%d 个页面无同源证据，仅标记 coverage_unattributed，'
            '不写入 source_pages（避免污染下游画布归属）：%s',
            len(weak), '、'.join(weak)
        )


def _evidence_quotes(value):
    """Yield quote strings from legacy strings or structured evidence lists."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        quote = value.get('quote') or value.get('evidence') or value.get('text')
        if quote:
            yield str(quote)
    elif isinstance(value, list):
        for item in value:
            yield from _evidence_quotes(item)

def _evidence_items(value):
    """Keep structured page/quote evidence intact while merging modules."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _validate_evidence(value, source_text: str) -> bool:
    """Evidence is valid only when every meaningful quote occurs in source."""
    source = _normalize_evidence_text(source_text)
    quotes = [
        _normalize_evidence_text(q)
        for q in _evidence_quotes(value)
        if q and '未找到直接原文' not in str(q)
    ]
    # Short fragments are too weak to be trustworthy evidence.
    if not quotes or any(len(q) < 8 for q in quotes):
        return False
    return all(q in source for q in quotes)


def _feature_text(item) -> str:
    """取 key_features 条目的文本。

    兼容两种形态：
    1. 新格式（p0 已升级为对象）：{'id': 'F-001', 'text': '...', 'involves_api': True, ...}
    2. 老格式（裸字符串）：'支持新增状态记录'
    老数据仍在库里，两种都必须能读，否则模块去重签名会失真、
    甚至把整个 dict 的 repr 当成功能点文本参与相似度计算。
    """
    if isinstance(item, dict):
        for key in ('text', 'title', 'name', 'description'):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''
    if item is None:
        return ''
    return str(item).strip()


def _feature_dedup_key(item):
    """key_features 去重用的键：新格式优先用稳定 id，老格式用文本。"""
    if isinstance(item, dict):
        fid = item.get('id')
        if isinstance(fid, str) and fid.strip():
            return ('id', fid.strip())
        return ('text', _feature_text(item))
    return ('text', str(item).strip())


def _ordered_union(left, right, key=None):
    """保序并集。传入 key 时按 key 判重（用于 dict 条目，避免 unhashable / 误判）。"""
    result = []
    seen = set()
    for item in list(left or []) + list(right or []):
        if key is not None:
            if not item:
                continue
            marker = key(item)
            if marker in seen:
                continue
            seen.add(marker)
        elif item in result:
            continue
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# feature_ref / api_ref 事后回填
# ---------------------------------------------------------------------------
# 为什么用代码回填而不是让模型透传：
#   阶段3.3.7 三路合并时，combined 只挑选 id/title/description/priority/source/
#   pass_criteria 六个字段送给 P7（见 run_design_phase1），feature_ref / api_ref
#   在这一步就被丢弃，P7 提示词里写再多"必须无损透传"也执行不到。
#   模型复述 id 也不可靠（会改写/漏写），所以改为在落库前用代码建立
#   「测试点 ↔ 功能点 / 接口契约」的关联，结果可重复、可解释。

_FEATURE_REF_MIN_SIM = 0.34     # 低于该相似度视为未命中，留空待人工
_FEATURE_REF_MARGIN = 0.06      # 次优与最优太接近时视为歧义，留空


def _ref_tokens(text: str) -> set:
    """中文 2-gram + 英文/数字串，用于轻量相似度（不依赖 embedding）。"""
    s = re.sub(r'\s+', '', str(text or ''))
    if not s:
        return set()
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    grams |= set(re.findall(r'[A-Za-z0-9_]{2,}', s))
    return grams


def _ref_similarity(a: set, b: set) -> float:
    """以较短一方为分母的覆盖率，偏好"被测文本包含功能点语义"。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _collect_feature_index(summary_data) -> list:
    """从阶段1 结构化结果收集功能点索引。

    返回 [{'module','id','text','involves_api'}...]；兼容 key_features 为
    对象（新格式）或裸字符串（老数据）。

    ⚠️ id 全局唯一化：新提示词产出 `M1-F001` 这类带模块前缀的 id；
    老数据是模块内 `F-001`，两个模块会重复。这里做一次升格，
    保证返回的 id 在整个 summary 内唯一 —— 否则下游按 id 匹配会跨模块误挂。
    """
    index = []
    if not isinstance(summary_data, dict):
        return index
    modules = [m for m in (summary_data.get('modules') or []) if isinstance(m, dict)]
    seen = {}
    for m_idx, m in enumerate(modules, 1):
        mod_name = str(m.get('name') or '').strip()
        for pos, item in enumerate(m.get('key_features') or [], 1):
            text = _feature_text(item)
            if not text:
                continue
            fid = ''
            api_flag = False
            if isinstance(item, dict):
                fid = str(item.get('id') or '').strip()
                api_flag = item.get('involves_api') is True
            raw_id = fid  # 原始 id（老数据为模块内 F-001，可能跨模块重复）
            if not fid:
                # 老数据没有 id：按模块序号+位置生成
                fid = 'M%d-F%03d' % (m_idx, pos)
            elif re.fullmatch(r'F-\d+', fid):
                # 老数据的裸 id（模块内编号）：**一律**加模块前缀升格为全局唯一。
                # 不能只处理"重复的那些"——首个模块的 F-001 会保留裸 id，
                # 与后来升格的 id 混用，仍会跨模块误配。
                num = int(fid.split('-')[1])
                fid = 'M%d-F%03d' % (m_idx, num)
            if fid in seen:
                # 仍冲突（如两个模块同名且序号相同已被占用）：追加序号兜底
                base = fid
                n = 2
                while fid in seen:
                    fid = '%s#%d' % (base, n)
                    n += 1
            seen[fid] = True
            index.append({'module': mod_name, 'id': fid, 'raw_id': raw_id,
                          'text': text, 'involves_api': api_flag})
    return index


def _collect_api_index(summary_data, id_map=None) -> list:
    """从阶段1 结果的 api_contracts 收集接口索引。

    id_map: {原 feature id -> 升格后的唯一 id}，由 _collect_feature_index 的
    调用方提供，用于把契约里的 feature_ids 同步改成唯一 id（老数据裸 id 会重复）。
    """
    out = []
    if not isinstance(summary_data, dict):
        return out
    id_map = id_map or {}
    for a in (summary_data.get('api_contracts') or []):
        if not isinstance(a, dict):
            continue
        aid = str(a.get('id') or '').strip()
        if not aid:
            continue
        fids = []
        for x in (a.get('feature_ids') or []):
            k = str(x).strip()
            if not k:
                continue
            # 只映射唯一命中的（映射表里一对多时无法判断，原样保留）
            mapped = id_map.get(k)
            fids.append(mapped if isinstance(mapped, str) else k)
        # 接口自身可检索的文本：路径、触发点、字段名
        parts = [a.get('path') or '', a.get('trigger') or '', a.get('method') or '']
        for f in (a.get('request_fields') or []):
            if isinstance(f, dict):
                parts.append(str(f.get('name') or ''))
        for f in (a.get('response_fields') or []):
            if isinstance(f, dict):
                parts.append(str(f.get('name') or ''))
        out.append({'id': aid, 'feature_ids': fids,
                    'text': ' '.join(x for x in parts if x)})
    return out


def backfill_testpoint_refs(test_points, summary_data, page_name='') -> int:
    """给测试点回填 feature_ref / api_ref（原地修改，返回回填条数）。

    规则（保守，宁缺勿错）：
      1. 测试点归属模块与功能点模块不同则不匹配 —— 避免跨模块误挂；
         模块名取"短的那个包含长的"即视为同模块（兼容「运动连胜」vs「运动连胜-日历」）。
      2. 相似度 = 以功能点分词语料为分母的覆盖率，取最高分；
         且必须 >= _FEATURE_REF_MIN_SIM，并且比次优高出 _FEATURE_REF_MARGIN，
         否则视为歧义留空（避免为了"每条都有值"而强行乱挂）。
      3. api_ref 只从 feature_ref 命中且 involves_api=True 的功能点反查，
         不额外猜测接口。
    """
    feats = _collect_feature_index(summary_data)
    if not feats or not isinstance(test_points, list):
        return 0

    # 原 id -> 唯一 id 映射：老数据两个模块都有 F-001 时无法判断契约指哪个模块，
    # 这种重复原 id 不映射（契约里的引用原样保留，不会误挂到某一侧）。
    raw_counter = {}
    for f in feats:
        raw_counter[f.get('raw_id')] = raw_counter.get(f.get('raw_id'), 0) + 1
    id_map = {}
    for f in feats:
        rid = f.get('raw_id')
        if rid and rid != f['id'] and raw_counter.get(rid) == 1:
            id_map[rid] = f['id']
    apis = _collect_api_index(summary_data, id_map)

    feat_tokens = [(f, _ref_tokens(f['text']), _ref_tokens(f['text']) | {f['id'].lower()})
                   for f in feats]
    # feature id -> api ids
    api_by_feature = {}
    for a in apis:
        for fid in a['feature_ids']:
            api_by_feature.setdefault(fid, []).append(a['id'])

    def same_module(tp_module, feat_module):
        """判断测试点模块与功能点模块是否同一模块。

        ⚠️ 不能简单用"互相包含"：实测存在「运动连胜」与「运动连胜-日历」这种
        父子关系模块，且两个模块的 feature id 都从 F-001 起 —— 互相包含会把
        子模块的测试点挂到父模块同名 id 的功能点上，产生跨模块误挂。
        规则：归一化后完全相等 → 同模块；一方为空 → 不限制；
        一方是另一方以固定分隔符切分后的完整前缀/子项 → 才算同族。
        """
        def _n(x):
            return re.sub(r'[\s_/、,，。|·]+', '', str(x or ''))

        a, b = _n(tp_module), _n(feat_module)
        if not a or not b or a == b:
            return True
        # 以原串（保留 '-'）按分隔符切段，要求段序列前缀一致
        def _segs(x):
            return [s for s in re.split(r'[\s\-_/、,，。|·]+', str(x or '')) if s]
        sa, sb = _segs(tp_module), _segs(feat_module)
        if not sa or not sb:
            return False
        short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
        # 只在"短的是长的完整前缀"且短的一方至少 2 段时才认为同族；
        # 1 段 vs 2 段（如「运动连胜」vs「运动连胜-日历」）属于父子模块，严格区分。
        return len(short) >= 2 and long_[:len(short)] == short

    filled = 0
    for tp in test_points:
        if not isinstance(tp, dict):
            continue
        if tp.get('feature_ref') or tp.get('api_ref'):
            # 模型已经给出（例如降级路径直通），尊重原值
            continue
        blob = ' '.join(str(tp.get(k) or '') for k in
                        ('title', 'description', 'pass_criteria', 'page_path'))
        t_tokens = _ref_tokens(blob)
        if not t_tokens:
            continue
        tp_mod = tp.get('module')

        def _score(allow_other_modules: bool):
            out = []
            for f, f_tokens, f_full in feat_tokens:
                if not allow_other_modules and not same_module(tp_mod, f['module']):
                    continue
                sim = max(_ref_similarity(f_tokens, t_tokens),
                          _ref_similarity(f_full, t_tokens) * 0.95)
                if sim > 0:
                    out.append((sim, f))
            out.sort(key=lambda x: -x[0])
            return out

        # 先在本模块内找；找不到再放开模块限制（P7 合并会重写 module 字段，
        # 实测存在"日历模块的测试点被标成目标模块"的错标，严格按模块过滤会整批漏配）。
        scored = _score(allow_other_modules=False)
        relaxed = False
        if not scored or scored[0][0] < _FEATURE_REF_MIN_SIM:
            wider = _score(allow_other_modules=True)
            if wider and (not scored or wider[0][0] > scored[0][0] + _FEATURE_REF_MARGIN):
                scored, relaxed = wider, True
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        best_sim, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best_sim < _FEATURE_REF_MIN_SIM:
            continue
        if best_sim - second < _FEATURE_REF_MARGIN:
            # 歧义：留空，交给人工/前端提示，不硬挂
            tp['_ref_ambiguous'] = [
                {'id': f['id'], 'sim': round(s, 3)} for s, f in scored[:3]
            ]
            continue
        tp['feature_ref'] = [best['id']]
        tp['_feature_ref_sim'] = round(best_sim, 3)
        # 记录功能点所属模块，便于前端/审计发现 P7 的 module 错标
        tp['_feature_ref_module'] = best['module']
        if relaxed:
            tp['_feature_ref_relaxed'] = True
        if best['involves_api']:
            api_ids = api_by_feature.get(best['id']) or []
            if api_ids:
                tp['api_ref'] = api_ids
        filled += 1
    return filled


# ---------------------------------------------------------------------------
# P0 占比收敛（代码侧硬约束）
# ---------------------------------------------------------------------------
# 为什么必须在代码里做：p7 提示词写了「P0 占比 ≤15%」，但没有可靠的执行体
# —— 实测同一批数据 P0 到 25%~45%，模型会为了"重要"给几乎所有点打 P0。
# 质量检查 _code_quality_check 里的 C5 也只做 WARNING 检测、不修正数据。
# 因此改为落库前按配额下调，并把每次降级写进审计字段，人工可复核。

P0_RATIO_LIMIT = 0.15      # P0 上限（与 p7 硬约束 4 / 质量检查 C5 保持一致）
P0_MIN_KEEP = 3            # 小批量时至少保留的 P0 条数（避免 5 条用例被压成 0 条 P0）


def _priority_rank(v) -> int:
    """P0=0 / P1=1 / P2=2；非法值（如 'P'、空）按 P1 处理。"""
    s = str(v or '').strip().upper()
    return {'P0': 0, 'P1': 1, 'P2': 2}.get(s, 1)


_CAT_SYNONYMS = {
    'main_flow': 'main_flow', 'branch_flow': 'branch_flow', 'boundary': 'boundary',
    'exception': 'exception', 'permission': 'permission', 'ui': 'UI',
    'performance': 'performance', 'compatibility': 'UI', '兼容': 'UI',
    '功能': 'main_flow', '主流程': 'main_flow', '边界': 'boundary',
    '异常': 'exception', '权限': 'permission', '性能': 'performance',
}


def _infer_category(tp) -> str:
    """推断 category，优先用 type/category 的显式值，其次按标题关键词判定。

    ⚠️ 历史 bug：原实现只查中文 `type`（功能/边界/异常），而 p2 实际输出英文
    `category` 或英文 `type`，映射全部落空 → 统一回落到 `main_flow`/`branch_flow`，
    导致 boundary/exception/permission/UI 被整体吞掉（实测某画布 6 类退化到 2 类）。
    """
    if not isinstance(tp, dict):
        return 'branch_flow'

    def _try(v):
        k = str(v or '').strip()
        if not k:
            return None
        direct = _CAT_SYNONYMS.get(k) or _CAT_SYNONYMS.get(k.lower())
        if direct:
            return direct
        # 兼容「边界值测试」「异常场景」这类带修饰的写法
        for syn, cat in _CAT_SYNONYMS.items():
            if syn and syn in k:
                return cat
        return None

    for key in ('category', 'type', 'tp_type'):
        got = _try(tp.get(key))
        if got:
            return got

    # 标题/描述关键词兜底（顺序即优先级：越"专"越先判）
    blob = ' '.join(str(tp.get(k) or '') for k in ('title', 'description'))
    # 否定式先剔除：「布局无异常」「不出现报错」这类不是在测异常
    NEG = ('无异常', '没有异常', '无报错', '不报错', '无错误', '不出现错误',
           '无崩溃', '不崩溃', '无失败', '不失败')
    for n in NEG:
        blob = blob.replace(n, '')
    RULES = [
        ('permission', ('权限', '登录态', '会员', '非会员', 'VIP', '越权', '角色', '未登录', '付费')),
        ('exception', ('异常', '断网', '弱网', '超时', '失败', '重试', '中断', '崩溃',
                       '错误', '无法', '异常处理', '超时重试', '杀进程')),
        ('boundary', ('边界', '上限', '下限', '最大值', '最小值', '超长', '空值', '为空',
                      '特殊字符', '字符数', '取值范围', '数量上限', '范围')),
        ('performance', ('性能', '大数据量', '加载速度', '耗时', '内存', '滚动性能', '并发')),
        ('UI', ('UI', '样式', '布局', '文案', '图标', '动画', '适配', '屏', '视觉', '展示正常', '截断')),
    ]
    for cat, words in RULES:
        if any(w in blob for w in words):
            return cat

    return 'main_flow' if str(tp.get('priority', '')).upper() == 'P0' else 'branch_flow'


def ensure_pass_criteria(test_points) -> dict:
    """兜底补齐空 pass_criteria，避免「假覆盖」测试点落库（原地修改）。

    背景：P7 输出可能解析失败（实测有 YAML 引号未闭合导致整份 JSON 丢弃），
    此时 workflow 会 `final_tps = deduped` 降级使用合并前的输入；而 P5 风险扩展点
    历史上不带 pass_criteria，降级路径就把空判据原样写库了 —— 这类测试点在
    Bug 存在时同样会通过（假覆盖）。

    策略（不编造业务断言，只做可信推导）：
      1. 已有非空 pass_criteria → 保留
      2. 否则若 description 里含可观测片段（「展示/提示/跳转/返回/变为/出现」等），
         取包含这些词的句子作为判据
      3. 都没有 → 标记 `_criteria_missing=True`，并写一句明确的缺口说明，
         **不伪造**「页面正常」这类看似可判伪的句子，交由人工补
    """
    audit = {'total': 0, 'filled_from_desc': 0, 'still_missing': []}
    if not isinstance(test_points, list):
        return audit

    OBSERVABLE = ('展示', '显示', '提示', '跳转', '进入', '返回', '出现', '消失',
                  '变为', '变为0', 'Toast', 'toast', '文案', '数值', '字段',
                  '报错', '错误码', '状态码', '刷新', '置灰', '不可点击')
    PLACEHOLDER = ('见描述', '同上', '略', 'TBD', '无', '-', '—')

    for t in test_points:
        if not isinstance(t, dict):
            continue
        audit['total'] += 1
        pc = str(t.get('pass_criteria') or '').strip()
        if pc and pc not in PLACEHOLDER:
            continue

        desc = str(t.get('description') or '').strip()
        picked = ''
        if desc:
            # 按中英文句号/分号切句，取第一句含可观测词的
            for seg in re.split(r'(?<=[。；;.!?！？\n])', desc):
                seg = seg.strip().rstrip('。；;')
                if seg and any(k in seg for k in OBSERVABLE):
                    picked = seg
                    break
        if picked:
            t['pass_criteria'] = picked
            t['_criteria_filled_from'] = 'description'
            audit['filled_from_desc'] += 1
        else:
            # 不伪造判据：明确标出缺口，供前端/人工补
            t['pass_criteria'] = (
                '【判据缺失·待人工补充】本测试点未产出可观测判据，'
                '在缺陷存在时可能无法判伪，需补充具体页面/元素/数值/接口断言。'
            )
            t['_criteria_missing'] = True
            audit['still_missing'].append({
                'id': t.get('id'), 'title': (t.get('title') or '')[:60]})
    return audit


def enforce_p0_ratio(test_points, limit=P0_RATIO_LIMIT, min_keep=P0_MIN_KEEP) -> dict:
    """把 P0 占比压到 limit 以内（原地修改，返回审计信息）。

    保护顺序（越靠前越优先保留为 P0）：
      1. category == main_flow（核心主流程）
      2. status == active / blocked 无关；source 含 risk 或 pci 的视为风险驱动，稍后降级
      3. 其余按原顺序
    只下调 P0 -> P1，不动 P1/P2，也不改 description/pass_criteria。
    """
    audit = {'before': 0, 'after': 0, 'limit': limit, 'demoted': []}
    if not isinstance(test_points, list) or not test_points:
        return audit

    tps = [t for t in test_points if isinstance(t, dict)]
    total = len(tps)
    if total == 0:
        return audit

    p0_idx = [i for i, t in enumerate(tps)
              if str(t.get('priority') or '').strip().upper() == 'P0']
    audit['before'] = len(p0_idx)

    # 配额优先：先按比例算，向下取整（保证不超上限）；
    # min_keep 只在"按比例会压到 0 条"的极小批量下兜底，
    # 否则 14 条 × 15% = 2.1 → 取 2 条，不应被 min_keep=3 抬到 3 条而破坏 15% 上限。
    quota = int(total * limit)
    if quota <= 0:
        allowed = min(min_keep, len(p0_idx))
    else:
        allowed = min(quota, len(p0_idx))
    if len(p0_idx) <= allowed:
        audit['after'] = len(p0_idx)
        return audit

    # 排序：main_flow 优先保留；其次非 risk/pci 来源（基础功能点）优先；
    # 最后按原顺序（保持稳定）
    def _keep_score(i):
        t = tps[i]
        cat = str(t.get('category') or '').strip().lower()
        src = [str(s).lower() for s in (t.get('source') or [])]
        return (0 if cat == 'main_flow' else 1,
                1 if ('risk' in src or 'pci' in src) else 0,
                i)

    keep = set(sorted(p0_idx, key=_keep_score)[:allowed])
    for i in p0_idx:
        if i in keep:
            continue
        t = tps[i]
        t['priority'] = 'P1'
        t['_priority_demoted'] = True
        t['_priority_demote_reason'] = (
            'P0 配额收敛：P0 占比超过 %.0f%%（原 %d/%d → 保留 %d 条）'
            % (limit * 100, len(p0_idx), total, allowed)
        )
        audit['demoted'].append({
            'id': t.get('id'), 'title': (t.get('title') or '')[:60],
            'category': t.get('category'),
        })

    audit['after'] = len(keep)
    return audit


def _module_signature(module):
    parts = [
        str(module.get('name', '') or ''),
        str(module.get('description', '') or ''),
        *[_feature_text(x) for x in module.get('key_features', []) if x],
    ]
    text = _normalize_evidence_text(''.join(parts))
    for word in ('模块', '功能', '新增', '修改', '优化', '修订', '补充', '逻辑', '页面'):
        text = text.replace(word, '')
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def _overlap_coefficient(left, right) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _should_merge_modules(left, right) -> bool:
    left_name = _normalize_evidence_text(left.get('name'))
    right_name = _normalize_evidence_text(right.get('name'))
    generic_words = ('模块', '功能', '新增', '修改', '优化', '修订', '补充', '逻辑', '页面')
    for word in generic_words:
        left_name = left_name.replace(word, '')
        right_name = right_name.replace(word, '')
    if (len(left_name) >= 2 and len(right_name) >= 2
            and (left_name in right_name or right_name in left_name)):
        return True

    left_sources = {str(x) for x in left.get('source_locations', []) if x}
    right_sources = {str(x) for x in right.get('source_locations', []) if x}
    source_overlap = _overlap_coefficient(left_sources, right_sources)
    content_overlap = _overlap_coefficient(
        _module_signature(left), _module_signature(right)
    )
    return source_overlap >= 0.5 and content_overlap >= 0.45


def _merge_module_pair(primary, duplicate):
    merged = dict(primary)
    merged['merged_from'] = _ordered_union(
        primary.get('merged_from') or [primary.get('name')],
        duplicate.get('merged_from') or [duplicate.get('name')],
    )
    merged['source_locations'] = _ordered_union(
        primary.get('source_locations'), duplicate.get('source_locations')
    )
    merged['key_features'] = _ordered_union(
        primary.get('key_features'), duplicate.get('key_features'),
        key=_feature_dedup_key,
    )

    merged['source_evidence'] = _ordered_union(
        _evidence_items(primary.get('source_evidence')),
        _evidence_items(duplicate.get('source_evidence')),
    )

    priority_rank = {'P0': 0, 'P1': 1, 'P2': 2}
    if priority_rank.get(duplicate.get('priority'), 9) < priority_rank.get(
        primary.get('priority'), 9
    ):
        merged['priority'] = duplicate.get('priority')
        merged['priority_source'] = duplicate.get('priority_source')
        merged['priority_reason'] = duplicate.get('priority_reason')

    for key in ('business_flow',):
        left_value = str(primary.get(key) or '').strip()
        right_value = str(duplicate.get(key) or '').strip()
        if right_value and right_value not in left_value:
            merged[key] = '\n'.join(x for x in (left_value, right_value) if x)
    return merged


def _postprocess_requirement_summary(data, source_text: str):
    """Merge probable duplicate modules and annotate evidence validity."""
    if not isinstance(data, dict):
        return data, 0, 0

    merged_modules = []
    merge_count = 0
    for raw_module in data.get('modules') or []:
        if not isinstance(raw_module, dict):
            continue
        module = dict(raw_module)
        for index, existing in enumerate(merged_modules):
            if _should_merge_modules(existing, module):
                merged_modules[index] = _merge_module_pair(existing, module)
                merge_count += 1
                break
        else:
            merged_modules.append(module)

    invalid_count = 0
    checked_count = 0
    converged_conclusions = []

    def _classify(item, module_name):
        """按「先推测后提问」判定歧义点是否可收敛：
        current_guess 唯一且无待确认问题（suggested_question 为空）→ converged，不再追问。"""
        guess = str(item.get('current_guess') or '').strip()
        question = str(item.get('suggested_question') or '').strip()
        status = str(item.get('status') or '').strip()
        converged = status in ('converged', '已确定', '确定') or (guess and not question)
        item['status'] = 'converged' if converged else 'pending'
        if converged:
            converged_conclusions.append({
                'module': module_name,
                'point': item.get('issue') or item.get('scenario') or item.get('description') or '',
                'conclusion': guess or item.get('suggested_answer') or '',
                'evidence': item.get('evidence') or item.get('basis') or '',
            })
        return converged

    priority_is_explicit = bool(re.search(r'(?<![A-Za-z0-9])P[012](?![A-Za-z0-9])', source_text, re.I))
    for module in merged_modules:
        if not priority_is_explicit:
            module['priority_source'] = '模型建议'
        module['business_flow_source'] = (
            '原文明示' if _validate_evidence(module.get('business_flow'), source_text)
            else '模型推演'
        )
        valid = _validate_evidence(module.get('source_evidence'), source_text)
        module['evidence_valid'] = valid
        module['evidence_level'] = 'A' if valid else 'D'
        module['human_confirmation_required'] = not valid
        checked_count += 1
        if not valid:
            invalid_count += 1
        for key in ('ambiguous_points', 'boundary_points'):
            for item in module.get(key) or []:
                if not isinstance(item, dict):
                    continue
                valid = _validate_evidence(item.get('evidence'), source_text)
                item['evidence_valid'] = valid
                item['evidence_level'] = 'A' if valid else 'D'
                converged = _classify(item, module.get('name', ''))
                item['human_confirmation_required'] = (not converged) or (not valid)
                checked_count += 1
                invalid_count += int(not valid)

    for key in ('ambiguous_points', 'boundary_points'):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                valid = _validate_evidence(item.get('evidence'), source_text)
                item['evidence_valid'] = valid
                item['evidence_level'] = 'A' if valid else 'D'
                converged = _classify(item, '全局')
                item['human_confirmation_required'] = (not converged) or (not valid)
                checked_count += 1
                if not valid:
                    invalid_count += 1

    data['modules'] = merged_modules
    data['evidence_validation'] = {
        'checked_count': checked_count,
        'invalid_count': invalid_count,
        'rule': 'normalized_exact_quote_match',
        'levels': {
            'A': '原文或视觉元素精确验证，可进入测试设计',
            'B': '跨页面推导，需保留推导标记',
            'C': '模型建议或需求缺失，仅用于探索性分析',
            'D': '证据验证失败，必须人工确认后才能生成最终用例',
        },
    }
    for item in data.get('missing_points') or []:
        if isinstance(item, dict):
            item['evidence_level'] = 'C'
            converged = _classify(item, '全局')
            item['human_confirmation_required'] = not converged
    for key in ('ambiguous_points', 'missing_points', 'boundary_points'):
        data.setdefault(key, [])
    data['converged_conclusions'] = converged_conclusions
    return data, merge_count, invalid_count


def _normalize_pci_dispositions(all_pci: list) -> dict:
    """机器侧收敛：low/medium 且 suggested_answer 是具体结论的 PCI，
    自动标记 disposition=assumed（按默认假设处理），不计入人工确认负担；
    high/blocker 或建议答案空泛的保持 disposition=confirm；
    含「修改/新增规则、数值阈值、文案」等可能改变产品决策的答案标记 suspicious（需人工确认）；
    assumed 中跨模块重复的问题合并为一条（标记 dedup_assumed）。"""
    import difflib
    import re
    stats = {
        'total': 0, 'assumed': 0, 'confirm': 0, 'suspicious': 0, 'dedup': 0,
        'by_impact': {}, 'by_disposition': {},
    }
    vague_markers = ('待确认', '待定', '需产品', '需要产品', '无法给出', '待进一步', '确认后', '待明确')
    # 答案若涉及修改/新增产品规则、具体数值阈值或文案，属于产品决策，不静默收敛
    change_markers = ('改为', '修改为', '调整为', '新增', '上限', '阈值', '固定为', '统一为', '设为', '文案')

    def _norm(text: str) -> str:
        text = re.sub(r'「[^」]*」', '', text or '')
        text = re.sub(r'\s+', '', text)
        text = re.sub(r'[，。；：、,.!?！？:;（）()【】\[\]"\'\-—_]+', '', text)
        return text

    seen_keys = []
    for mod in all_pci:
        data = mod.get('data') if isinstance(mod, dict) else None
        if not isinstance(data, dict):
            continue
        items = data.get('pci_list')
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            impact = str(it.get('impact') or '').lower()
            stats['total'] += 1
            stats['by_impact'][impact] = stats['by_impact'].get(impact, 0) + 1
            if impact in ('low', 'medium'):
                answer = str(it.get('suggested_answer') or '').strip()
                if not answer or len(answer) < 4 or any(m in answer for m in vague_markers):
                    it['disposition'] = 'confirm'
                    stats['confirm'] += 1
                elif any(m in answer for m in change_markers):
                    it['disposition'] = 'suspicious'
                    stats['suspicious'] += 1
                else:
                    # 跨模块去重：规范化后与已有 assumed 问题高度相似则合并
                    key = _norm(it.get('description') or '')
                    dup = None
                    for k in seen_keys:
                        if difflib.SequenceMatcher(None, k, key).ratio() >= 0.9:
                            dup = k
                            break
                    if dup is not None:
                        it['disposition'] = 'dedup_assumed'
                        it['merged_into'] = '见首个模块的同类假设'
                        stats['dedup'] += 1
                    else:
                        seen_keys.append(key)
                        it['disposition'] = 'assumed'
                        it['assumed_answer'] = answer
                        stats['assumed'] += 1
            else:
                it['disposition'] = 'confirm'
                stats['confirm'] += 1
            disp = it.get('disposition')
            stats['by_disposition'][disp] = stats['by_disposition'].get(disp, 0) + 1
    return stats


# ---------- 画布精确定位（同名画布修复） ----------
# 墨刀原型允许多个画布同名（如任务 #105 的 p15/p17 都叫「连胜状态」），
# 所以 page_name 只能当「显示名」，不能当身份键。下面两个工具把画布标识统一成
# (page_name, page_no) 二元组：page_no 为 None 表示「所有同名画布」（兼容历史
# 的批量调用），带上 page_no 时精确定位到唯一一张画布。
def _norm_page_keys(page_names):
    """把画布标识集合归一化为 {(page_name, page_no)}。

    入参元素可以是 '连胜状态'（纯名字）或 ('连胜状态', 17)（精确二元组）。
    """
    keys = set()
    for item in (page_names or set()):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            keys.add((str(item[0]), item[1]))
        else:
            keys.add((str(item), None))
    return keys


def _page_key_hit(page_name, page_no, keys) -> bool:
    """extracted_json 里的一条画布是否命中 keys（精确 page_no 优先，其次纯名字）。"""
    if not keys:
        return True
    name = str(page_name)
    if (name, page_no) in keys:
        return True
    return (name, None) in keys


def _page_names_of(keys) -> set:
    """从 {(name, no)} 里取出纯名字集合（用于只能按名字匹配的 fused 条目）。"""
    return {name for (name, _no) in (keys or set())}


class ModaoWorkflow:
    """墨刀技能工作流编排。"""

    def __init__(self, prototype, analyzer_config, writer_config, embedder_config,
                 vision_config=None, ocr_vision_config=None, dedup_threshold=DEDUP_THRESHOLD,
                 page_ids=None):
        self.prototype = prototype
        self.analyzer_config = analyzer_config
        self.writer_config = writer_config
        self.embedder_config = embedder_config
        self.vision_config = vision_config
        self.ocr_vision_config = ocr_vision_config or vision_config
        self.dedup_threshold = dedup_threshold
        self.session_id = prototype.uuid
        # 画布级独立运行覆盖：非 None 时所有「选中画布」语义收敛为这些画布 id，
        # 实现「每个画布作为独立任务分支，互不干扰」。None 维持原 selected/rerun 聚合逻辑。
        self.page_ids = list(page_ids) if page_ids else None

    # ---------- 配置解析 ----------
    @staticmethod
    def resolve_configs(analyzer_id=None, writer_id=None, embedder_id=None):
        from apps.requirement_analysis.models import AIModelConfig
        analyzer = (AIModelConfig.objects.filter(id=analyzer_id).first()
                    if analyzer_id else AIModelConfig.get_active_config('siliconflow', 'analyzer')
                    or AIModelConfig.objects.filter(role='analyzer', is_active=True).first())
        writer = (AIModelConfig.objects.filter(id=writer_id).first()
                  if writer_id else AIModelConfig.objects.filter(role='writer', is_active=True).first())
        embedder = (AIModelConfig.objects.filter(id=embedder_id).first()
                    if embedder_id else AIModelConfig.get_active_embedder())
        # fallback: analyzer 缺失时复用 writer（两者都是 LLM 文本生成，可共用模型）
        if not analyzer and writer:
            analyzer = writer
        return analyzer, writer, embedder

    # ---------- LLM / 向量 基础 ----------
    def _llm(self, config, prompt: str, max_tokens: int = 8000, timeout: int = 600, temperature: float = None, return_meta: bool = False):
        """调用 LLM。带超时保护：单条调用超过 timeout 秒即抛 asyncio.TimeoutError，
        由上层（run_design_phase1 模块级 try/except）捕获并降级，避免窗口永久卡死。

        timeout 默认 600s：阶段1 结构化分析（analyzer=DeepSeek deepseek-chat）在大型原型上
        单页 prompt 可达数万字符，供应商偶发慢响应（瞬时繁忙）会在 300s 内无返回而被误杀，
        加长到 600s 可覆盖"慢但能返回"的正常情况，仅真正卡死才超时降级。

        temperature: None=沿用config.temperature, 0=确定性输出(推荐用于结构化生成)。
        return_meta: True 时返回 dict(content, finish_reason)，便于上层检测 max_tokens 截断；
                     默认 False 维持仅返回 content 字符串，不影响其他调用方。"""
        import time as _time
        if not config:
            raise Exception('缺少可用的 AI 模型配置（analyzer/writer）')
        t0 = _time.time()
        model_label = f'{config.model_type}/{config.model_name}'
        role_label = getattr(config, 'role', '?')
        temp_label = f't={temperature}' if temperature is not None else f't={config.temperature}'
        prompt_line1 = (prompt.strip() or '').split('\n')[0][:60]
        print(f'[LLM] ▶ 调用 {model_label} (role={role_label}) | {temp_label} | max_tokens={max_tokens} | 提示: {prompt_line1}...', flush=True)
        messages = [{'role': 'user', 'content': prompt}]
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    AIModelService.call_openai_compatible_api(config, messages, max_tokens=max_tokens, temperature=temperature),
                    timeout=timeout,
                )
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f'LLM 调用超时（>{timeout}s），请检查模型服务是否可用或网络是否通畅'
            )
        content = result['choices'][0]['message']['content']
        finish_reason = None
        try:
            finish_reason = result['choices'][0].get('finish_reason')
        except Exception:
            pass
        usage = result.get('usage') or {}
        p_tokens = usage.get('prompt_tokens', 0)
        c_tokens = usage.get('completion_tokens', 0)
        t_tokens = usage.get('total_tokens', 0)
        elapsed = _time.time() - t0
        print(f'[LLM] ✓ {model_label} 返回 {len(content)}字符 耗时 {elapsed:.1f}s tokens(in={p_tokens},out={c_tokens},total={t_tokens}) finish_reason={finish_reason}', flush=True)
        if getattr(self, 'prototype', None):
            _wf_tlog(self.prototype.id, f'[LLM_METRICS] {model_label} role={role_label} in_tok={p_tokens} out_tok={c_tokens} total_tok={t_tokens} p_len={len(prompt)} r_len={len(content)} elapsed={elapsed:.2f}s finish_reason={finish_reason}')
        if return_meta:
            return {'content': content, 'finish_reason': finish_reason}
        return content

    def _embed(self, texts):
        if not self.embedder_config:
            return None
        try:
            return asyncio.run(
                asyncio.wait_for(
                    AIModelService.embed_texts(self.embedder_config, texts),
                    timeout=300,
                )
            )
        except asyncio.TimeoutError:
            logger.warning(f'向量化超时（降级为无去重，>300s）')
            return None
        except Exception as e:
            logger.warning(f'向量化失败（降级为无去重）: {e}')
            return None

    def _cosine_sim(self, texts_a, texts_b):
        """两组文本的余弦相似度矩阵（len(a) x len(b)）；embedder 不可用或失败返回 None，调用方自行降级。"""
        if not self.embedder_config or not texts_a or not texts_b:
            return None
        vecs = self._embed(list(texts_a) + list(texts_b))
        if not vecs:
            return None
        try:
            arr = np.array(vecs, dtype=float)
            arr = arr / np.clip(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9, None)
            na = len(texts_a)
            return arr[:na] @ arr[na:].T
        except Exception as e:
            logger.warning(f'PCI 语义相似度计算失败: {e}')
            return None

    # ---------- 向量语义去重（方案 A）----------
    def _vector_dedup(self, items):
        """
        items: [{'id','title','description','source'(list),'priority'}]
        返回 (deduped_items, dedup_count, audit_records)

        预排序：按 priority (P0>P1>P2) → id/title 字母序，确保同输入下结果稳定。
        """
        if not items:
            return items, 0, []
        # 稳定排序：高优先级在前 → 同优先级按 id/title 字典序
        prio_w = {'P0': 0, 'P1': 1, 'P2': 2}
        items = sorted(items, key=lambda it: (
            prio_w.get(it.get('priority', '').upper() if isinstance(it.get('priority'), str) else 'P9', 9),
            it.get('id', '') or it.get('title', '') or '',
        ))
        texts = [it.get('description') or it.get('title') or '' for it in items]
        vecs = self._embed(texts)
        if vecs is None:
            logger.info('无 embedder，跳过向量去重')
            return items, 0, []
        try:
            arr = np.array(vecs, dtype=float)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            arr = arr / np.clip(norms, 1e-9, None)
            sim = arr @ arr.T
        except Exception as e:
            logger.warning(f'向量去重计算失败: {e}')
            return items, 0, []

        n = len(items)
        keep = [True] * n
        dedup = 0
        audit_records = []

        def better(a, b):
            return prio_w.get(a.get('priority'), 9) <= prio_w.get(b.get('priority'), 9)

        for i in range(n):
            if not keep[i]:
                continue
            for j in range(i + 1, n):
                if keep[j] and sim[i][j] >= self.dedup_threshold:
                    audit_records.append({
                        'kept_id': items[i].get('id'),
                        'kept_title': items[i].get('title', ''),
                        'removed_id': items[j].get('id'),
                        'removed_title': items[j].get('title', ''),
                        'similarity': round(float(sim[i][j]), 6),
                        'threshold': self.dedup_threshold,
                        'reason': '同模块测试点描述的向量相似度达到阈值',
                    })
                    # 合并来源标记，保留高优先级代表
                    src = list(set((items[i].get('source') or []) + (items[j].get('source') or [])))
                    if better(items[j], items[i]):
                        items[i]['priority'] = items[j]['priority']
                    items[i]['source'] = src
                    items[i]['description'] = items[i].get('description') or items[j].get('description')
                    keep[j] = False
                    dedup += 1
        result = [items[i] for i in range(n) if keep[i]]
        return result, dedup, audit_records

    def _pci_dedup_against_clarifications(self, pci_list, clarification_log):
        """
        后处理去重：把 PCI 列表中与已澄清问题（resolution 非空）高度相似的条目剔除。

        pci_list: [{'title','description','priority',...}, ...]
        clarification_log: 字符串（JSON 数组）或已解析列表
        返回 (filtered_pci_list, removed_count, removed_items)
        """
        if not pci_list:
            return pci_list, 0, []
        # 解析澄清清单，只取 resolution 非空的条目
        try:
            clarify_items = json.loads(clarification_log) if isinstance(clarification_log, str) else (clarification_log or [])
        except Exception:
            clarify_items = []
        if not isinstance(clarify_items, list):
            clarify_items = []
        resolved = []
        for item in clarify_items:
            if not isinstance(item, dict):
                continue
            resolution = str(item.get('resolution') or item.get('answer') or '').strip()
            if resolution:
                resolved.append(item)
        if not resolved:
            return pci_list, 0, []

        # 构建已澄清问题的文本集合（issue + suggested_question + resolution 摘要）
        def _clean(s):
            return re.sub(r'\s+', '', str(s or ''))
        resolved_texts = []
        for r in resolved:
            parts = [r.get('issue', ''), r.get('suggested_question', ''), r.get('resolution', '')]
            resolved_texts.append(_clean(' '.join(parts)))

        # 对每条 PCI，计算其与已澄清问题的最优相似度（向量余弦主判抓改写重复，字面 difflib 兜底）
        import difflib
        pci_raw_texts = [(pci.get('description') or '') + ' ' + (pci.get('title') or '')
                         for pci in pci_list if isinstance(pci, dict)]
        ref_question_texts = [((r.get('issue') or '') + ' ' + (r.get('suggested_question') or '')).strip()
                              for r in resolved]
        cos_sim = self._cosine_sim(pci_raw_texts, ref_question_texts)
        keep = []
        removed = []
        raw_idx = -1
        for pci in pci_list:
            if not isinstance(pci, dict):
                keep.append(pci)
                continue
            raw_idx += 1
            pci_text = _clean((pci.get('description') or '') + ' ' + (pci.get('title') or ''))
            if not pci_text:
                keep.append(pci)
                continue
            max_ratio = 0.0
            for rt in resolved_texts:
                if not rt:
                    continue
                ratio = difflib.SequenceMatcher(None, pci_text, rt).ratio()
                if ratio > max_ratio:
                    max_ratio = ratio
            max_cos = 0.0
            if cos_sim is not None:
                try:
                    max_cos = float(max(cos_sim[raw_idx]))
                except Exception:
                    max_cos = 0.0
            # 判重：向量余弦 >= dedup_threshold（改写重复）或字面 >= 0.55
            if max_cos >= self.dedup_threshold or max_ratio >= 0.55:
                removed.append({'title': pci.get('title', ''), 'description': pci.get('description', ''),
                                'max_similarity': round(max_ratio, 3), 'max_cosine': round(max_cos, 3)})
            else:
                keep.append(pci)
        return keep, len(removed), removed

    def _log(self, msg: str):
        """追加工作日志到 prototype.work_log（带时间戳）。"""
        from datetime import datetime as dt
        ts = dt.now().strftime('%H:%M:%S')
        entry = f"[{ts}] {msg}\n"
        self.prototype.work_log = (self.prototype.work_log or '') + entry
        # 同时更新 updated_at，保证超时看门狗能感知任务仍在推进，避免误判为卡死
        self.prototype.save(update_fields=['work_log', 'updated_at'])

    def ask_question(self, stage: int, question: str, max_tokens: int = 4000) -> str:
        """针对某阶段产物进行答疑（人工疑点解答），返回 AI 回答文本。"""
        ctx_map = {
            0: (self.prototype.extracted_json or '') + '\n' + (self.prototype.requirement_summary or ''),
            1: self.prototype.requirement_summary or '',
            2: self.prototype.clarification_log or '',
            3: '\n'.join([
                self.prototype.testcases_json or '',
                self.prototype.final_testpoints_json or '',
                self.prototype.risks_json or '',
                self.prototype.pci_json or '',
            ]),
        }
        context = (ctx_map.get(stage, self.prototype.requirement_summary) or '').strip()
        if not context:
            return '该阶段暂无内容可答疑，请先运行对应阶段生成产物。'
        prompt = (
            '你是一名资深测试分析师。用户对工作流产物有疑问，请用 **2-4 句话** 简洁应答。\n\n'
            f'===== 产物内容 =====\n{context}\n===== 结束 =====\n\n'
            f'用户疑问：{question}\n\n'
            '要求：直接给结论（不说「需确认」、不用反问）。简述依据。'
            '末尾用 [推断] 标记不确定的结论。禁止 4 段式结构、禁止大段论述、禁止行业科普。'
        )
        return self._llm(self.analyzer_config or self.writer_config, prompt, max_tokens=max_tokens)

    # ---------- 阶段0：原型读取（支持多地址批量 + 单地址自动发现页面树）----------
    @staticmethod
    def _page_label_from_url(u: str) -> str:
        """从 URL 推断页面名：优先取 hash 中的 p= 参数（已 URL 解码），否则取路径末段。"""
        try:
            frag = u.split('#', 1)[1] if '#' in u else ''
            m = re.search(r'[?&]p=([^&]+)', frag)
            if not m:
                m = re.search(r'[?&]p=([^&]+)', u)
            if m:
                return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
        path = u.split('#')[0].rstrip('/')
        return path.split('/')[-1] or u

    @_timed_stage('阶段0 读取')
    def run_extract(self, urls=None, url=None, cookies=None, headless=True, source_type=None,
                    dual_fusion=False, fusion_config=None, target_page_names=None):
        from apps.requirement_analysis.modao_extractor import ModaoExtractor
        source_type = source_type or self.prototype.source_type or 'modao'
        # 单画布重读：只更新指定 page_name 的画布，不重建全部。
        _target_names = set()
        if target_page_names:
            _target_names = {str(n).strip() for n in target_page_names if str(n).strip()}
        _single_mode = bool(_target_names)
        # 双路融合：未显式指定 fusion_config 时，复用分析用的 analyzer 配置做交叉融合
        if dual_fusion and fusion_config is None:
            fusion_config = self.analyzer_config
        if dual_fusion and not self.vision_config:
            self.prototype.status = 'failed'
            self.prototype.error_message = '双路融合需要启用 writer_vision 视觉模型配置，读取已中止。'
            self.prototype.save(update_fields=['status', 'error_message'])
            return {'pages': [], 'blocked': True, 'error': 'missing_vision_config'}
        if dual_fusion and not fusion_config:
            self.prototype.status = 'failed'
            self.prototype.error_message = '双路融合需要可用的 analyzer/writer 文本模型配置，读取已中止。'
            self.prototype.save(update_fields=['status', 'error_message'])
            return {'pages': [], 'blocked': True, 'error': 'missing_fusion_config'}
        # 规范化 URL 列表（兼容单字符串 / 列表 / 换行或逗号分隔）
        if urls is None:
            urls = [url] if url else []
        elif isinstance(urls, str):
            urls = [x for x in urls.replace(',', '\n').split('\n') if x.strip()]
        urls = [x.strip() for x in urls if isinstance(x, str) and x.strip()]
        urls = list(dict.fromkeys(urls))  # 去重保序
        if not urls:
            self.prototype.status = 'failed'
            self.prototype.error_message = '未提供有效的需求地址'
            self.prototype.save(update_fields=['status', 'error_message'])
            self._log('未提供有效的需求地址，读取中止')
            return {'pages': []}

        self.prototype.status = 'extracting'
        self.prototype.save(update_fields=['status'])
        self._log(f'开始需求读取，共 {len(urls)} 个地址...')
        extractor = ModaoExtractor(run_id=self.session_id)
        all_pages = []
        all_warnings = []
        all_fused_requirements = []
        partial_flag = False
        for ui, u in enumerate(urls, 1):
            try:
                # webshare：始终枚举页面树（目录地址自动发现所有子页面）。
                # 墨刀多地址：每个当单页抓（避免页面树枚举导致跨地址重复）；
                # 墨刀单地址：尝试枚举页面树自动发现目录内多页。
                if source_type == 'webshare':
                    enumerate_pages = True
                    single = False
                else:
                    single = len(urls) > 1
                    enumerate_pages = not single
                data = extractor.extract_sync(url=u, cookies=cookies, headless=headless,
                                              enumerate_pages=enumerate_pages,
                                              source_type=source_type,
                                              vision_config=self.vision_config,
                                              ocr_vision_config=self.ocr_vision_config,
                                              dual_fusion=dual_fusion,
                                              fusion_config=fusion_config,
                                              prototype_id=self.prototype.id,
                                              target_page_names=(list(_target_names) if _single_mode else None))
                for p in data['pages']:
                    if single:
                        # 单页模式 extractor 给的是默认"第N页"，用 URL 推断更可读的页面名
                        p['page_name'] = self._page_label_from_url(u) or p.get('page_name') or f'地址{ui}'
                    all_pages.append(p)
                all_warnings.extend(data.get('warnings') or [])
                all_fused_requirements.extend(data.get('fused_requirements') or [])
                # 超时导致的部分成功：明确告知用户已读 N 页、可继续读取补齐
                if data.get('partial'):
                    partial_flag = True
                    self._log(f'地址{ui}读取超时，已保留部分结果：{len(data["pages"])} 页'
                              f'（可点击「继续读取」补齐剩余画布）')
                else:
                    self._log(f'地址{ui}读取完成：{len(data["pages"])} 页')
            except ModaoTerminateError as te:
                # 用户在前端点「终止」：立即收尾为 failed，避免被外层 except 吞掉后当正常跑完
                self._log(f'任务已被用户手动终止：{te}')
                TERMINATE_REQUESTED.pop(self.prototype.id, None)
                self.prototype.refresh_from_db()
                self.prototype.status = 'failed'
                self.prototype.error_message = '任务已被用户手动终止'
                self.prototype.save(update_fields=['status', 'error_message'])
                return {'pages': all_pages, 'terminated': True}
            except Exception as e:
                logger.error(f'地址[{u}]读取失败: {e}')
                self._log(f'地址{ui}读取失败: {e}')
                continue

        # 重新编号并落库（避免重跑累积）
        for idx, p in enumerate(all_pages, 1):
            p['page_no'] = idx
        # 单画布重读模式：只覆盖目标画布，其余画布原样保留（不删除重建）
        if _single_mode:
            # 宽松匹配：支持「group-title」前缀差异 / 编码差异，只要目标名与返回画布名
            # 互相包含（按 title 段）即视为同一画布，避免命名体系不一致导致 targeted 为空、
            # 既没更新画布也没收敛 OCR。
            def _name_match(pn: str) -> bool:
                pn = (pn or '').strip()
                if not pn:
                    return False
                if pn in _target_names:
                    return True
                for tn in _target_names:
                    # 去掉可能的 group 前缀后比较 title 段
                    _pt = pn.split('-', 1)[-1] if '-' in pn else pn
                    _tt = tn.split('-', 1)[-1] if '-' in tn else tn
                    if _pt and _tt and (_pt in _tt or _tt in _pt):
                        return True
                return False
            targeted = [p for p in all_pages if _name_match(p.get('page_name'))]
            # 解析每张目标画布对应的「前端传入 target 名」，作为落库 page_name 主键，
            # 避免 extractor 内部 page_name（无 group 前缀 / 编码差异）与 DB 既有记录键不一致
            # 导致 update_or_create 新建而非更新。
            def _target_name_of(pn: str):
                pn = (pn or '').strip()
                if pn in _target_names:
                    return pn
                for tn in _target_names:
                    _pt = pn.split('-', 1)[-1] if '-' in pn else pn
                    _tt = tn.split('-', 1)[-1] if '-' in tn else tn
                    if _pt and _tt and (_pt in _tt or _tt in _pt):
                        return tn
                return pn
            if not targeted:
                self._log('单画布重读：未在读取结果中找到匹配画布，未做任何修改')
                self.prototype.status = 'extracted'
                self.prototype.save(update_fields=['status'])
                return {'pages': all_pages, 'single_mode': True, 'updated': 0}
            auto_filtered_names = []
            updated = 0
            for p in targeted:
                # 单画布重读：用户主动要求重读该画布，尊重意图，不再自动过滤取消选中。
                # 仅记录自动过滤建议作为 note，便于前端提示，但不改变 selected。
                should_filter, reason = self._auto_filter_page(p)
                note = f'自动过滤建议：{reason}' if should_filter else ''
                if should_filter:
                    auto_filtered_names.append(p.get('page_name', ''))
                _name = _target_name_of(p.get('page_name'))
                # 同名画布修复：update_or_create 默认只拿 page_name 当唯一键，重读 p17
                # 「连胜状态」时会误更新到 p15 那一行（两张画布内容被揉进同一行）。
                # 仅当该名字在本任务下存在多行时，才追加 page_no 精确定位；
                # 名字唯一时保持原行为，避免页码漂移导致凭空新建行。
                _same = list(self.prototype.pages.filter(page_name=_name))
                _hit = next((x for x in _same if x.page_no == p['page_no']), None) if len(_same) > 1 else None
                _lookup = {'pk': _hit.pk} if _hit is not None else {'page_name': _name}
                self.prototype.pages.update_or_create(
                    defaults={
                        'page_no': p['page_no'],
                        'parent_name': p.get('parent_name', '') or '',
                        'text': p.get('text', ''),
                        'annotations': json.dumps(p.get('annotations', []), ensure_ascii=False),
                        'screenshot': p.get('screenshot', ''),
                        'screenshot_segments': json.dumps(p.get('screenshot_segments', []), ensure_ascii=False),
                        'selected': True,
                        'recognition_note': note,
                        'current_stage': 0,
                        'stage_status': 'done',
                        'rerun_stages': '',
                        'error_message': '',
                    },
                    **_lookup,
                )
                updated += 1
            self._log(f'单画布重读完成：已更新 {updated} 张画布（{", ".join(_target_names)}），其余画布保持不变')
            # 单画布重读成功后：重置该画布级回退标记，任务级 status 统一复位为 extracted
            # （run_extract 开头无条件置 extracting，单模式提前 return 跳过全量收尾，必须在此复位）
            self.prototype.pages.filter(page_name__in=_target_names).update(
                stage_status='done', rerun_stages='', error_message='')
            self.prototype.status = 'extracted'
            self.prototype.error_message = ''
            self.prototype.save(update_fields=['status', 'error_message'])
            # 合并 extracted_json（只更新目标画布条目）
            self._merge_extracted_json(all_pages, _target_names)
            return {'pages': all_pages, 'single_mode': True, 'updated': updated}
        # 全量模式：清空旧 pages 后重建
        self.prototype.pages.all().delete()
        auto_filtered_names = []
        for p in all_pages:
            should_filter, reason = self._auto_filter_page(p)
            note = f'自动过滤：{reason}' if should_filter else ''
            if should_filter:
                auto_filtered_names.append(p.get('page_name', ''))
            self.prototype.pages.create(
                page_no=p['page_no'],
                page_name=p['page_name'],
                parent_name=p.get('parent_name', '') or '',
                folder=p.get('folder', '') or '',
                text=p.get('text', ''),
                annotations=json.dumps(p.get('annotations', []), ensure_ascii=False),
                screenshot=p.get('screenshot', ''),
                screenshot_segments=json.dumps(p.get('screenshot_segments', []), ensure_ascii=False),
                selected=False,  # 首次提取默认不全选，由用户在看板显式执行「全部勾选」或按需挑选
                recognition_note=note,
                # A2-b：提取完成即标记画布「已读取」
                current_stage=0,
                stage_status='done',
            )
        if auto_filtered_names:
            self._log(
                f'已自动过滤 {len(auto_filtered_names)} 个无效画布（默认不纳入结构化）：'
                + '、'.join(auto_filtered_names)
                + '；如需保留可在「校验画布模块」中手动勾选恢复'
            )
        valid_pages = [p for p in all_pages if not p.get('duplicate_of_page')]
        duplicate_count = len(all_pages) - len(valid_pages)
        self._log(f'全部读取完成，共 {len(all_pages)} 页 / {len(valid_pages)} 个独立画布')
        # 阶段0来源覆盖补全：在落库前执行，使修复后的 source_pages 持久化；
        # 避免动态子页内容被并入父页需求时误触发 G6（>20% 未覆盖）阻断。融合内容本身完整。
        _repair_source_coverage(all_fused_requirements, valid_pages)
        self.prototype.extracted_json = json.dumps({
            'urls': urls,
            'pages': [{
                'page_no': p['page_no'],
                'page_name': p['page_name'],
                'item_cid': p.get('item_cid', ''),   # 画布唯一锚点（续读/同名画布定位用）
                'text': p.get('text', ''),
                'dom_text': p.get('dom_text', ''),
                'vision_text': p.get('vision_text', ''),
                'dom_text_len': p.get('dom_text_len', 0),
                'vision_attempted': bool(p.get('vision_attempted')),
                'vision_success': bool(p.get('vision_success')),
                'annotations': p.get('annotations', []),
                'screenshot': p.get('screenshot', ''),
                'screenshot_segments': p.get('screenshot_segments', []),
                'visual_summary': p.get('visual_summary', ''),
                'visual_artifacts': p.get('visual_artifacts', []),
                'screenshot_sha256': p.get('screenshot_sha256', ''),
                'duplicate_of_page': p.get('duplicate_of_page'),
                'extraction_warnings': p.get('extraction_warnings', []),
            } for p in all_pages],
            'dual_fusion': bool(dual_fusion),
            'fused_requirements': all_fused_requirements,
            'warnings': all_warnings,
        }, ensure_ascii=False)
        self.prototype.source_type = source_type
        # 多页原型如果超过 20% 的页面是完全相同截图，说明画布切换不可信；
        # 保留所有读取产物供排查，但阻断结构化和后续用例生成。
        duplicate_ratio = duplicate_count / len(all_pages) if all_pages else 0
        block_reasons = []

        # ── 阻断 1: 空页 ──
        if not all_pages:
            # extractor 的 warnings 里往往已经有**精确原因**（如「目录浏览已关闭，
            # 请改填 HTML 文件完整地址」）。旧逻辑只抛一条泛化文案，把真正可操作的
            # 信息丢了，用户只能看到「请检查链接是否有效」而无法定位。
            # 这里优先透出 extractor 的告警，泛化文案仅作兜底。
            _detail = [str(w).strip() for w in all_warnings if str(w).strip()]
            if _detail:
                block_reasons.append(
                    '未能从链接中读取到任何页面内容：\n    ' + '\n    '.join(_detail[:4]))
            else:
                block_reasons.append(
                    '未能从链接中读取到任何页面内容，请检查链接是否有效、是否需要登录')

        # ── 阻断 2: 截图重复 ──
        if len(all_pages) > 1 and duplicate_ratio > 0.2:
            block_reasons.append(
                f'有 {duplicate_count} 页截图重复（共 {len(all_pages)} 页），可能是墨刀画布切换失败，'
                f'建议关闭双路融合后重试，或手动逐页截图上传'
            )

        # ── 视觉识别校验 ──
        #
        # ⚠️ PRD 报告页必须排除在视觉校验之外。
        # 这类来源走 modao_extractor.extract_prd_report_sync()，是**纯 HTTP 解析**：
        # 不启动浏览器、不截图、不做视觉 OCR，页面带 prd_module=True 显式标记，
        # 且 vision_attempted=False / screenshot=''。
        # 而双路融合开关（前端默认开）会要求「视觉覆盖率」达标，于是
        # len(attempted)=0 != len(valid)=N 恒成立 → 无论文档多完整都必然阻断。
        # 提取端早就预料到这点（见 _parse_prd_report 的注释），却没有同步改这里的
        # 判据，导致该格式「支持了但永远读不出来」。
        # ⇒ 视觉相关校验一律只在"非 PRD"页面上做；PRD 页走纯文本语义。
        prd_pages = [p for p in valid_pages if p.get('prd_module')]
        vision_scope = [p for p in valid_pages if not p.get('prd_module')]
        if prd_pages:
            self._log(
                f'检测到 {len(prd_pages)} 个 PRD 报告页：该来源为纯 HTTP 文本解析、'
                f'不含截图与视觉识别，已跳过视觉覆盖率校验'
            )

        attempted_pages = [p for p in vision_scope if p.get('vision_attempted')]
        vision_failed = [p for p in attempted_pages if not p.get('vision_success')]
        vision_failure_ratio = len(vision_failed) / len(attempted_pages) if attempted_pages else 0
        is_small_prototype = len(all_pages) <= 2  # 1~2 页的小原型不因单页视觉失败而阻断

        if dual_fusion and vision_failure_ratio > 0.2 and not is_small_prototype:
            failed_names = '、'.join(p.get('page_name', '') for p in vision_failed[:8])
            block_reasons.append(
                f'有 {len(vision_failed)} 个页面的视觉识别未返回有效结果（{failed_names}），'
                f'请检查视觉模型配置是否正常、API 额度是否充足'
            )

        # ── 小原型视觉失败 → 降级为告警，不阻断 ──
        if is_small_prototype and vision_failed:
            self._log(
                f'小原型（{len(all_pages)} 页）中有 {len(vision_failed)} 页视觉识别未返回内容，'
                f'已自动降级为纯 DOM 文本模式继续处理，不影响后续流程'
            )

        # 覆盖率只针对"本来该有视觉数据"的页面（vision_scope）；PRD 页不计入分母。
        if dual_fusion and vision_scope and len(attempted_pages) != len(vision_scope):
            block_reasons.append(
                f'视觉识别覆盖率不足：{len(vision_scope)} 个页面中仅 {len(attempted_pages)} 个完成了识别，'
                f'请检查视觉模型是否可用'
            )

        # ── 阻断 5: 融合需求为空 ──
        # PRD 页产出的融合需求只可能来自 DOM（无视觉路），由
        # extract_prd_report_sync 的 _prd_fuse_requirements 负责；
        # 它的结果同样写入 all_fused_requirements，所以这里无需为 PRD 开特例。
        if dual_fusion and not all_fused_requirements:
            block_reasons.append(
                '双路融合（DOM + 视觉）未能生成有效的结构化需求，'
                '可能是原型内容过少或格式不支持，建议改用纯文本模式上传需求'
            )

        # ── 阻断 6: 页面覆盖不足 ──
        fused_source_pages = set()
        for item in all_fused_requirements:
            if not isinstance(item, dict):
                continue
            for page_name in item.get('source_pages') or []:
                fused_source_pages.add(_normalize_evidence_text(page_name))
        uncovered_pages = [
            p for p in valid_pages
            if _normalize_evidence_text(p.get('page_name')) not in fused_source_pages
        ]
        # ⚠️ 2026-11-04：G6 只应统计「本期确实有需求变更、却未被融合需求覆盖」的页面。
        # 判据是**该页面是否带有需求增量证据**，实测口径如下：
        #   ① ``annotations`` 字段在全部实测任务（proto=70/71/82/85/88/89/92/93）中恒为空，
        #      批注实际被并入 ``text`` 的 ``[便利贴批注]`` 段落，因此不能只看该字段；
        #   ② 不能用 ``visual_artifacts`` 判定 —— 它是视觉结构识别产物（ui_screen 线框图等），
        #      按 p0 硬约束 16，ui_screen 只是底模外观，既不是业务规则也不是本期新增需求；
        #      用它当"有变更"会把全部画布都算进来（实测 proto=93 会算出 63% 误阻断）。
        # 无任何批注证据且被 _repair_source_coverage 打了 coverage_unattributed 的页面，
        # 视为「本期无需求」，不计入分母。
        _changed_pages = [
            p for p in uncovered_pages
            if _page_has_requirement_annotation(p)
        ]
        _no_change_pages = [p for p in uncovered_pages if p not in _changed_pages]
        if _no_change_pages:
            logging.getLogger(__name__).info(
                '[STAGE0] G6：%d 个画布本期无需求批注，不计入未覆盖分母：%s',
                len(_no_change_pages),
                '、'.join(p.get('page_name', '') for p in _no_change_pages[:8]),
            )
        uncovered_pages_g6 = _changed_pages
        uncovered_ratio = len(uncovered_pages_g6) / len(valid_pages) if valid_pages else 0
        if dual_fusion and all_fused_requirements and uncovered_ratio > 0.2:
            uncovered_names = '、'.join(p.get('page_name', '') for p in uncovered_pages_g6[:8])
            block_reasons.append(
                f'有 {len(uncovered_pages_g6)} 个页面（含本期变更标注）的内容未被融合需求覆盖：'
                f'{uncovered_names}，建议检查这些页面是否有效、是否需要重新读取'
            )
        extraction_blocked = bool(block_reasons)
        # 部分成功（读取超时但有已读页）：**不算失败**，落库已读部分并提示可继续读取。
        # 这样超时不再让用户白等 30 分钟，也避免「已读的画布被丢弃」。
        if partial_flag and all_pages:
            self.prototype.status = 'partial'
            self.prototype.error_message = (
                '原型读取未完成（超时中断）：已保留 %d 个画布。'
                '请点击「继续读取」从断点补齐剩余画布（已读画布会自动跳过，不会重复读取）。'
                % len(valid_pages or all_pages))
            self.prototype.current_stage = 0
            self.prototype.pages.all().update(stage_status='done', error_message='')
            self.prototype.save()
            self._log(self.prototype.error_message)
            return {
                'pages': all_pages,
                'valid_pages': len(valid_pages),
                'vision_attempted': len(attempted_pages),
                'vision_failed': len(vision_failed),
                'fused_requirements': len(all_fused_requirements),
                'uncovered_pages': len(uncovered_pages),
                'blocked': False,
                'partial': True,
            }
        self.prototype.status = 'failed' if extraction_blocked else 'extracted'
        if extraction_blocked:
            self.prototype.error_message = '原型读取失败：\n' + '\n'.join(f'  · {r}' for r in block_reasons)
        else:
            self.prototype.error_message = ''
        self.prototype.current_stage = 0
        # A2-b：同步画布级状态（提取成功=已读取 done；失败=failed）
        page_status = 'failed' if extraction_blocked else 'done'
        self.prototype.pages.all().update(
            stage_status=page_status,
            error_message=(self.prototype.error_message if extraction_blocked else ''),
        )
        self.prototype.save()
        if extraction_blocked:
            self._log(self.prototype.error_message)
        return {
            'pages': all_pages,
            'valid_pages': len(valid_pages),
            'vision_attempted': len(attempted_pages),
            'vision_failed': len(vision_failed),
            'fused_requirements': len(all_fused_requirements),
            'uncovered_pages': len(uncovered_pages),
            'blocked': extraction_blocked,
        }

    # ---------- 阶段0 画布自动过滤 ----------
    # 墨刀原型常自带模板/引导/封面/副本页（如「总览/演示/标注/登录/免费使用」等），
    # 这些页对需求结构化无意义，读取后自动取消勾选，避免污染后续流程；
    # 用户仍可在画布校验卡片上手改回勾选。
    _USELESS_PAGE_HINTS = (
        '总览', '演示', '标注', '登录', '免费使用', '引导', '封面', '示例', '帮助',
        'about', 'intro', 'start', 'cover', 'template', 'demo', 'guide', 'tour',
        '欢迎', '首页引导', '使用说明', '新手', '教程', '预览',
    )

    @classmethod
    def _auto_filter_page(cls, p: dict):
        """判断某画布是否应自动过滤（不纳入后续流程）。

        返回 (should_filter: bool, reason: str)。保守策略，仅命中明确无效特征才过滤，
        避免误杀真实业务页面。
        """
        # 1) 副本页（与某页重复）
        dup = p.get('duplicate_of_page')
        if dup:
            return True, f'画布副本(与「{dup}」重复)'

        name = (p.get('page_name') or '').strip().lower()

        # 2) 页面名命中无用模板关键词（整名或包含关键词）
        #    ⚠️ 例外：PRD 报告页的"页"是需求模块，模块名可能天然含「引导/登录」等词
        #    （如「新用户剧情与首登引导」），但它们是真实需求正文，不能按模板页丢弃。
        #    这类条目由 extract_prd_report_sync 打上 prd_module=True 显式豁免，
        #    不改变原型画布（墨刀/webshare/Axure）原有的过滤行为。
        if not p.get('prd_module'):
            for hint in cls._USELESS_PAGE_HINTS:
                hl = hint.lower()
                if name == hl or hl in name:
                    return True, f'模板/引导页(命中「{hint}」)'

        # 3) 内容过少：DOM 文本极短且视觉也未提供有效内容，疑似空页/封面
        dom_len = int(p.get('dom_text_len') or 0)
        vision_ok = bool(p.get('vision_success'))
        vision_text = (p.get('vision_text') or '').strip()
        if dom_len < 15 and (not vision_ok or len(vision_text) < 15):
            return True, '内容过少(疑似空页/封面)'

        return False, ''

    def _merge_extracted_json(self, new_pages, target_names):
        """单画布重读：把新读取的目标画布条目合并进已有 extracted_json，保留其它画布。"""
        try:
            data = json.loads(self.prototype.extracted_json) if self.prototype.extracted_json else {}
        except Exception:
            data = {}
        old_pages = data.get('pages') or []
        _tn_set = set(target_names) if isinstance(target_names, (list, set, tuple)) else set()

        def _norm(n):
            n = (n or '').strip()
            if not n:
                return n
            return n.split('-', 1)[-1] if '-' in n else n

        # 宽松匹配：返回的 page_name 可能缺 group 前缀（无 cookie 时编码差异），
        # 按 title 段与 target 互相包含判定是否为同一画布。
        def _match_target(pn):
            pn = (pn or '').strip()
            if pn in _tn_set:
                return pn
            pn_n = _norm(pn)
            for tn in _tn_set:
                tn_n = _norm(tn)
                if pn_n and tn_n and (pn_n in tn_n or tn_n in pn_n):
                    return tn
            return None
        new_by_name = {}
        for p in new_pages:
            t = _match_target(p.get('page_name'))
            if t:
                new_by_name[t] = p
        merged = [p for p in old_pages if _match_target(p.get('page_name')) is None]
        for name, p in new_by_name.items():
            merged.append({
                'page_no': p.get('page_no'),
                'page_name': name,
                'text': p.get('text', ''),
                'dom_text': p.get('dom_text', ''),
                'vision_text': p.get('vision_text', ''),
                'dom_text_len': p.get('dom_text_len', 0),
                'vision_attempted': bool(p.get('vision_attempted')),
                'vision_success': bool(p.get('vision_success')),
                'annotations': p.get('annotations', []),
                'screenshot': p.get('screenshot', ''),
                'screenshot_segments': p.get('screenshot_segments', []),
                'visual_summary': p.get('visual_summary', ''),
                'visual_artifacts': p.get('visual_artifacts', []),
                'screenshot_sha256': p.get('screenshot_sha256', ''),
                'duplicate_of_page': p.get('duplicate_of_page'),
                'extraction_warnings': p.get('extraction_warnings', []),
            })
        # 同步 fused_requirements 中目标画布的 source_pages 覆盖（保留其它）
        try:
            old_fused = data.get('fused_requirements') or []
            new_fused = [p for p in (new_pages if isinstance(new_pages, list) else [])]
        except Exception:
            old_fused = data.get('fused_requirements') or []
            new_fused = []
        data['pages'] = merged
        # fused_requirements 不在此处重写（单画布重读只刷新原始文本，融合在结构化阶段重算）
        data['fused_requirements'] = old_fused
        data['warnings'] = data.get('warnings') or []
        self.prototype.extracted_json = json.dumps(data, ensure_ascii=False)
        self.prototype.save(update_fields=['extracted_json'])

    # ---------- 阶段1：需求结构化 ----------
    def _rerun_page_names(self) -> set:
        """存在画布级回退（needs_rerun）时，返回需要参与本次重跑的画布名集合。

        A2-b 画布回退语义：用户在画布卡片上点「↩ 回退」只标记该画布
        current_stage 降级 + stage_status='needs_rerun'，并不改动其它画布的
        selected。若重跑结构化仍按全部 selected=True 聚合，需求摘要会被未回退
        画布的内容污染（表现为「只选了两个画布回退，摘要却还是整个版本」）。

        因此：当任务下存在 needs_rerun 画布时，本次重跑的聚合范围强制收敛为
        这些待重跑画布（它们才是用户真正要重算的范围），忽略其余已完成画布。
        没有待重跑画布时返回空集，调用方退化到「全部 selected」逻辑。
        """
        try:
            pages = self.prototype.pages.all()
            rerun = {p.page_name for p in pages if getattr(p, 'stage_status', '') == 'needs_rerun'}
            return rerun
        except Exception:
            return set()

    def _selected_page_names(self) -> set:
        """参与后续流程的画布名集合。

        优先级：
        0) 若构造时显式传入 page_ids（画布级独立运行），只取这些画布——实现单画布
           作为独立任务分支运行，互不干扰；
        1) 若任务下存在 needs_rerun 画布（曾发生画布级回退/重跑），范围收敛为
           这些待重跑画布——保证「只回退/只选部分画布重跑」时结构化只含这部分；
        2) 否则取全部 selected=True 的画布；没有任何 selected 记录时视为全选。
        """
        if self.page_ids is not None:
            try:
                return {p.page_name for p in self.prototype.pages.all() if p.id in self.page_ids}
            except Exception:
                return set()
        try:
            pages = self.prototype.pages.all()
            if not pages:
                return set()
            rerun = self._rerun_page_names()
            if rerun:
                return rerun
            selected = {p.page_name for p in pages if getattr(p, 'selected', True)}
            # 若没有任何一条 selected 记录（历史数据），视为全选
            if not selected:
                return {p.page_name for p in pages}
            return selected
        except Exception:
            return set()

    def _target_pages_qs(self):
        """本次运行真正要处理的画布 QuerySet（**尽量按 id 精确定位**）。

        背景（同名画布 bug）：墨刀原型允许多个画布取同一个名字，例如任务
        #105 的 p15/p17 都叫「连胜状态」、p16/p18 都叫「目标」、p6/p12 都叫
        「运动连胜」。因此 ``page_name`` **不是唯一键**，不能当作画布身份使用。

        此前所有调用点都是「先用 page_ids 选出 id → 再降维成 page_name 集合 →
        最后 filter(page_name__in=名字集合)」，同名画布会被一并命中，导致：
          · 点 p17「连胜状态」结构化 → p15 被一起重跑、一起推进阶段；
          · _merge_page_summaries 把两张同名画布的摘要一起拼进全局摘要
            → 界面上直接出现「两个一样的模块」。
        这里把身份收敛回 id：画布级独立运行（构造时传了 page_ids）时按 id 精确取；
        任务级批量运行（无 page_ids）时才退回按名字，保持历史行为。
        """
        qs = self.prototype.pages.all()
        if self.page_ids is not None:
            return qs.filter(id__in=self.page_ids)
        sel_names = self._selected_page_names()
        if sel_names:
            return qs.filter(page_name__in=sel_names)
        return qs

    def _advance_selected_pages(self, stage, status='done', error=None):
        """A2-b：把任务下所有「已勾选」画布的画布级进度推进到指定阶段。

        结构化/澄清/测试点合并是任务级全局聚合操作，完成后所有参与画布统一推进；
        回退时由回退接口单独调用本方法降级。
        """
        try:
            pages = self._target_pages_qs()
            for p in pages:
                # 只在前进时取 max；回退（stage 更小）时由调用方直接赋值
                if stage >= p.current_stage:
                    p.current_stage = stage
                if status:
                    p.stage_status = status
                if error is not None:
                    p.error_message = error
                p.save(update_fields=['current_stage', 'stage_status', 'error_message'])
        except Exception as e:
            logger.warning('[_advance_selected_pages] 同步画布进度失败: %s', e)

    def _source_text(self, page_names=None) -> str:
        if self.prototype.extracted_json:
            selected_names = page_names if page_names is not None else self._selected_page_names()
            data = json.loads(self.prototype.extracted_json)
            parts = []
            fused = data.get('fused_requirements') or []
            if fused:
                if selected_names:
                    fused = [
                        x for x in fused
                        if isinstance(x, dict)
                        and (selected_names & set(str(s) for s in (x.get('source_pages') or [])))
                    ]
                parts.append(
                    '## DOM + 视觉双路交叉融合需求（结构化主输入）\n'
                    + json.dumps(fused, ensure_ascii=False)
                )
            for p in data.get('pages', []):
                if p.get('duplicate_of_page'):
                    continue
                if selected_names and p.get('page_name') not in selected_names:
                    continue
                anno_str = json.dumps(p.get('annotations', []), ensure_ascii=False) if p.get('annotations') else '[]'
                va_str = json.dumps(p.get('visual_artifacts', []), ensure_ascii=False) if p.get('visual_artifacts') else '[]'
                parts.append(
                    f"### 画布：{p['page_name']}\n"
                    f"【核心需求标注/便利贴批注（本次需求增量核心，权重大）】：\n{anno_str}\n\n"
                    f"【结构化视觉内容 visual_artifacts（注意：ui_screen 仅代表底模线框图外观截图，非业务规则；rule_list/state_matrix/annotation 为规则批注）】：\n{va_str}\n\n"
                    f"【画布全景 OCR / 背景底模文字（仅供了解界面视觉上下文，未批注提及的底图固有文字非本次需求）】：\n{p.get('text','')}"
                )
            return '\n\n'.join(parts)
        return self.prototype.requirement_summary or ''

    def _match_page_text_for_module(self, mod_name: str, mod: dict = None, page_names=None) -> str:
        """注入该模块的结构化需求到 P2 prompt。

        优先使用 fused_requirements（阶段0 已由 LLM+OCR 逐页提取、按 module 归类的结构化需求），
        它们含 feature + interaction + evidence，是测试点生成的最高质量输入。
        fused 无匹配时才回退到页面 text 匹配。
        """
        if not self.prototype.extracted_json or not mod_name:
            return ''
        try:
            data = json.loads(self.prototype.extracted_json)
        except Exception:
            return ''
        # ① 优先：fused_requirements 按 module 匹配（兼容细分后的「父模块-来源页」子模块名）
        rel = self._fused_entries_for_module(mod_name)
        if rel:
            parts = [f'\n\n【该模块已提取的结构化需求（LLM+OCR 逐条）】以下为模块「{mod_name}」的 {len(rel)} 条需求，'
                     f'请严格基于这些具体需求生成测试点，不要泛化或臆造：']
            used_len = 0
            max_total = 5000
            for i, r in enumerate(rel, 1):
                if used_len >= max_total:
                    break
                feature = r.get('feature', '') or ''
                interaction = r.get('interaction', '') or ''
                ev = r.get('evidence', '') or ''
                block = f'{i}. 【{feature}】{interaction}'
                if ev:
                    block += f'（证据等级：{ev}）'
                if len(block) > 600:
                    block = block[:600]
                if used_len + len(block) > max_total:
                    block = block[:max_total - used_len]
                used_len += len(block)
                parts.append(block)
            return '\n'.join(parts)
        # ② 回退：页面 text 匹配（fused 无该模块时用）
        pages = [p for p in data.get('pages', []) if not p.get('duplicate_of_page')]
        if page_names is not None:
            pages = [p for p in pages if p.get('page_name') in page_names]
        if not pages:
            return ''
        import re
        mod_words = set(re.findall(r'[\u4e00-\u9fa5]{2,4}', mod_name))
        if not mod_words:
            mod_words = {mod_name}
        matched = []
        for p in pages:
            page_name = p.get('page_name', '') or ''
            text_head = (p.get('text', '') or '')[:500]
            if any(w in page_name for w in mod_words) or any(w in text_head for w in mod_words):
                matched.append(p)
        if not matched:
            for p in pages:
                if any(w in p.get('page_name', '') for w in mod_words):
                    matched.append(p)
        if not matched:
            return ''
        import re as _re
        _noise = {'总览', '演示', '标注', '登录', '免费使用', '画布', '页面', '图层', '批注', '评论',
                  '展开全部', '展开', '登录后导出', '欢迎来到墨刀', '立即登录', '预览', '分享', '收起'}
        mod_keywords = set()
        for f in (mod.get('features', []) if isinstance(mod, dict) else []):
            mod_keywords.update(_re.findall(r'[\u4e00-\u9fa5]{2,6}', str(f)))
        if not mod_keywords:
            mod_keywords = mod_words
        parts = [f'\n\n【对应原型的页面关键需求（便签/批注，已按模块相关性抽取）】以下内容属于模块「{mod_name}」，请基于这些具体需求生成测试点：']
        used_len = 0
        max_total = 4000
        for p in matched[:3]:
            if used_len >= max_total:
                break
            txt = (p.get('text', '') or '').strip()
            if not txt:
                continue
            lines = []
            for ln in txt.split('\n'):
                ln_s = ln.strip()
                if not ln_s:
                    continue
                if ln_s in _noise or ln_s.startswith('墨刀用户'):
                    continue
                lines.append(ln_s)
            relevant = [ln for ln in lines if any(k in ln for k in mod_keywords)]
            pick = relevant if len(relevant) >= 3 else lines
            block = '\n'.join(pick)
            if len(block) > 1800:
                block = block[:1800]
            if used_len + len(block) > max_total:
                block = block[:max_total - used_len]
            used_len += len(block)
            parts.append(f"\n### 页面「{p.get('page_name','未命名')}」（第{p.get('page_no')}页）\n{block}")
        return '\n'.join(parts) if len(parts) > 1 else ''

    def _extract_module_context(self, mod_name: str, summary_text=None) -> str:
        """从阶段1 requirement_summary 提取当前模块的跨模块关联影响与特有风险，
        注入 PCI 生成 prompt，让 PCI 具备全局关联视角（类似 PDF 的『待确认风险列+跨模块影响』）。
        """
        summary = (summary_text if summary_text is not None else self.prototype.requirement_summary or '').strip()
        if not summary:
            return ''
        # ⚠️ 2026-11-04：多画布拼接视图必须逐段合并解析，整份 safe_load 会失败 → PCI 丢失全局关联视角
        data, _ = _parse_yaml_docs(summary)
        if not isinstance(data, dict):
            data = _parse_yaml(summary)
        if not isinstance(data, dict):
            return ''
        modules = data.get('modules') or []
        if not isinstance(modules, list):
            return ''
        target = None
        for m in modules:
            if not isinstance(m, dict):
                continue
            name = str(m.get('name', '') or '').strip()
            if name == mod_name or mod_name in name or name in mod_name:
                target = m
                break
        if not target:
            return ''
        impact = target.get('cross_module_impact') or []
        risks = target.get('module_risks') or []
        parts = []
        if isinstance(impact, list) and impact:
            parts.append('跨模块关联影响：\n' + '\n'.join(f'- {i}' for i in impact))
        if isinstance(risks, list) and risks:
            parts.append('本模块特有风险：\n' + '\n'.join(f'- {r}' for r in risks))
        if not parts:
            return ''
        return '\n'.join(parts)

    def _fused_entries_for_module(self, mod_name: str, page_names=None) -> list:
        """按模块名匹配返回 fused_requirements 条目列表。

        兼容两种形式：
        1) 阶段1 原生模块名（如「日记贴纸模板」）→ 精确匹配 module 字段
        2) 细分后的子模块名（如「日记贴纸模板-删除」）→ 剥离「-来源页」后缀匹配父模块，
           并按来源页过滤出该子模块对应的 fused 条目
        """
        if not self.prototype.extracted_json:
            return []
        try:
            data = json.loads(self.prototype.extracted_json)
        except Exception:
            return []
        fused = data.get('fused_requirements') or []
        if not isinstance(fused, list):
            return []
        if page_names is not None:
            fused = [x for x in fused if isinstance(x, dict)
                     and (page_names & set(str(s) for s in (x.get('source_pages') or [])))]
        base = mod_name
        sub_page = None
        if '-' in mod_name:
            base, _, sub_page = mod_name.rpartition('-')
        rel = [x for x in fused if isinstance(x, dict) and (x.get('module') or '').strip() == base]
        if sub_page and rel:
            rel = [x for x in rel if x.get('source_pages') and str(x.get('source_pages')[0]) == sub_page]
        return rel


    def _split_heavy_module_by_fused(self, modules: list) -> list:
        """对「细节特别多」的模块做再细分，缓解 P2 单次生成超容导致的漏子功能。

        背景：阶段1 会把多个页面（如贴纸的 切换模板/删除/添加/编辑/分享）锁成 1 个业务模块，
        P1「阶段1对齐铁律」强制保留该粗粒度模块名，导致 P2 一次要吞几十条 fused 需求，
        max_tokens 装不下 → LLM 聚焦主流程、静默丢弃删除/添加等子功能。

        本方法：对 fused 条目数超过阈值的模块，按其 fused 条目的 feature/source 聚类拆成子模块，
        （类似部署前的细粒度拆分），P2 将逐个细模块生成，避免超容漏项。
        父模块名保留（作为统一前缀），不破坏阶段1 业务语义。
        """
        threshold = 12  # 单模块 fused 条目超过该值则细分
        if not modules:
            return modules

        out = []
        for mod in modules:
            if not isinstance(mod, dict):
                out.append(mod)
                continue
            name = str(mod.get('name', '') or '').strip()
            rel = self._fused_entries_for_module(name)
            if len(rel) < threshold:
                out.append(mod)
                continue

            # 按 source_pages 首个来源页聚类（删除/添加/编辑/分享/切换模板...）
            groups = {}
            for r in rel:
                pages = r.get('source_pages') or []
                key = str(pages[0]) if isinstance(pages, list) and pages else '其他'
                groups.setdefault(key, []).append(r)

            # 生成子模块：父模块名 + 来源页作为子模块标识
            sub_modules = []
            for i, (page_key, entries) in enumerate(groups.items(), 1):
                sub_name = f"{name}-{page_key}" if page_key != '其他' else f"{name}-其他{i}"
                sub_features = [e.get('feature', '') for e in entries if isinstance(e, dict) and e.get('feature')]
                sub = {
                    'name': sub_name,
                    'priority': mod.get('priority', 'P0'),
                    'sub_pages': [page_key] if page_key != '其他' else [],
                    'features': sub_features or [],
                    'risks': mod.get('risks', []),
                    'estimated_testcases': max(len(entries) * 2, 5),
                    'estimated_smoke': 1,
                    '_parent_module': name,
                    '_fused_source': page_key,
                    '_fused_count': len(entries),
                }
                sub_modules.append(sub)

            if len(sub_modules) <= 1:
                # 聚类没有区分度（几乎全挤在一个来源页），退回原模块，交由 P2 分批处理
                out.append(mod)
                continue

            self._log(f'  ⚙️ 模块[{name}] 细节过多（{len(rel)} 条 fused），细分为 {len(sub_modules)} 个子模块：'
                      + ', '.join(s['name'] for s in sub_modules))
            out.extend(sub_modules)

        return out

    def _source_decision_tables(self, page_names=None):
        """Return decision tables from vision extraction as authoritative source data."""
        if not self.prototype.extracted_json:
            return []
        try:
            data = json.loads(self.prototype.extracted_json)
        except (TypeError, ValueError):
            return []
        tables = []
        _keys = _norm_page_keys(page_names) if page_names is not None else None
        for page in data.get('pages') or []:
            if _keys is not None and not _page_key_hit(page.get('page_name'), page.get('page_no'), _keys):
                continue
            for artifact in page.get('visual_artifacts') or []:
                if not isinstance(artifact, dict) or artifact.get('artifact_type') != 'decision_table':
                    continue
                table = json.loads(json.dumps(artifact, ensure_ascii=False))
                table.setdefault('source_page', f"第{page.get('page_no')}页")
                table.setdefault('name', table.get('title') or table.get('artifact_id') or '决策表')
                tables.append(table)
        return tables

    def _source_visual_structures(self, page_names=None):
        """Return all non-trivial visual structures with page traceability."""
        if not self.prototype.extracted_json:
            return []
        try:
            data = json.loads(self.prototype.extracted_json)
        except (TypeError, ValueError):
            return []
        trivial = {'ui', 'annotation'}
        structures = []
        _keys = _norm_page_keys(page_names) if page_names is not None else None
        for page in data.get('pages') or []:
            if _keys is not None and not _page_key_hit(page.get('page_name'), page.get('page_no'), _keys):
                continue
            for artifact in page.get('visual_artifacts') or []:
                if not isinstance(artifact, dict) or artifact.get('artifact_type') in trivial:
                    continue
                item = json.loads(json.dumps(artifact, ensure_ascii=False))
                item.setdefault('source_page', f"第{page.get('page_no')}页")
                item.setdefault('source_artifact_id', item.get('artifact_id'))
                structures.append(item)
        return structures

    def _restore_authoritative_decision_tables(self, parsed, page_names=None):
        """Keep LLM module association, but never trust it to rewrite visual matrix cells.
        2026-08-31 修复：透传 page_names，逐画布结构化时只恢复当前画布的决策表，
        避免把其他未勾选画布（如会员购买页）的视觉产物注入每个画布摘要。"""
        source_tables = self._source_decision_tables(page_names)
        if not isinstance(parsed, dict) or not source_tables:
            return parsed
        generated = {
            item.get('source_artifact_id') or item.get('artifact_id'): item
            for item in (parsed.get('decision_tables') or []) if isinstance(item, dict)
        }
        restored = []
        for source in source_tables:
            artifact_id = source.get('artifact_id')
            enriched = generated.get(artifact_id) or {}
            table = dict(source)
            table['source_artifact_id'] = artifact_id
            if enriched.get('related_module'):
                table['related_module'] = enriched['related_module']
            restored.append(table)
        parsed['decision_tables'] = restored
        return parsed

    def _restore_authoritative_visual_structures(self, parsed, page_names=None):
        """Preserve source structures; LLM may only enrich their module association/name.
        2026-08-31 修复：透传 page_names，逐画布结构化时只恢复当前画布的视觉结构，
        避免把其他未勾选画布（如会员购买页）的视觉产物注入每个画布摘要。"""
        if not isinstance(parsed, dict):
            return parsed
        source_items = self._source_visual_structures(page_names)
        generated_items = []
        for key in ('visual_structure_links', 'visual_structures', 'state_matrices', 'decision_tables'):
            generated_items.extend(parsed.get(key) or [])
        generated = {
            item.get('source_artifact_id') or item.get('artifact_id'): item
            for item in generated_items if isinstance(item, dict)
        }
        modules = [item for item in (parsed.get('modules') or []) if isinstance(item, dict)]
        restored = []
        for source in source_items:
            artifact_id = source.get('artifact_id')
            enrichment = generated.get(artifact_id) or {}
            item = dict(source)
            item['source_artifact_id'] = artifact_id
            if enrichment.get('name'):
                item['name'] = enrichment['name']
            source_page = str(item.get('source_page') or '')
            page_candidates = [
                module.get('name') for module in modules
                if source_page and source_page in [str(x) for x in (module.get('source_locations') or [])]
            ]
            proposed = enrichment.get('related_module')
            if proposed in page_candidates:
                item['related_module'] = proposed
            elif len(page_candidates) == 1:
                item['related_module'] = page_candidates[0]
            elif proposed:
                item['related_module'] = proposed
                item['module_association_warning'] = '所属模块未通过来源页交叉校验'
            restored.append(item)
        parsed['visual_structures'] = restored
        parsed['state_matrices'] = [
            item for item in restored if item.get('artifact_type') == 'state_matrix'
        ]
        return parsed

    # ---------- 项目知识库（辅助生成 + 自适应闭环）----------
    def _kb_context(self, query_hint: str = '') -> str:
        """读取本项目知识库上下文，按 query_hint 做语义检索 top-3 最相关片段。
        query_hint 为空时退化为全量读取（兼容旧调用）；非空时走 RAG 精准检索。"""
        cache_key = f'_kb_cache_{hash(query_hint)}'
        if getattr(self, cache_key, None) is not None:
            return getattr(self, cache_key)

        ctx = ''
        try:
            if not self.prototype.project_id:
                return ''
            from apps.projects.models import Project
            from apps.requirement_analysis.models import AIModelService
            project = Project.objects.filter(id=self.prototype.project_id).first()
            if not project:
                return ''

            if query_hint:
                # RAG 精准检索：按当前模块/测试点语义检索 top-3
                ctx = AIModelService.build_knowledge_context_from_project(
                    project, query_text=query_hint, max_chunks=3) or ''
            else:
                # 全量读取（兼容旧调用）
                ctx = AIModelService.build_knowledge_context_from_project(
                    project, query_text=self._source_text()) or ''
        except Exception as e:
            logger.warning(f'读取项目知识库失败（降级为无知识库）: {e}')
            ctx = ''
        setattr(self, cache_key, ctx)
        return ctx

    def _kb_suffix(self, query_hint: str = '') -> str:
        """拼接到各阶段 prompt 末尾的知识库参考段（无知识库时返回空串）。
        query_hint: 可选，用于按需精准检索相关片段。"""
        ctx = self._kb_context(query_hint=query_hint)
        if not ctx:
            return ''
        return (
            "\n\n### 项目知识库参考\n"
            + ctx
            + "\n（请结合上述项目业务背景与既有测试约束来辅助本次分析与生成；"
              "不要重复知识库中已明确的内容，聚焦于本次需求新增/变化的部分）"
        )

    def _build_focused_split_prompt(self, name: str, expected_names, expected_module_list: str) -> str:
        """为【单个】遗漏模块构造聚焦补全 prompt。

        关键治本思路：整列拆分时，LLM 常在长文本中被主要模块淹没而主动删除次要模块
        （即便已把 name 预填进骨架也会整项删掉）。改为对每个遗漏模块单独发一次
        「只生成这一个」的聚焦调用——单任务场景下 LLM 不会被其他模块挤掉注意力，
        漏拆率从约 50% 降到接近 0。
        """
        single_skeleton = '\n'.join([
            'modules:',
            f'  - name: {name}',
            '    sub_pages:',
            '      - <根据需求摘要填入该模块的页面/子流程>',
            '    priority: <P0/P1/P2>',
            '    features:',
            '      - <该模块核心功能1>',
            '      - <该模块核心功能2>',
            '    entry_path: |',
            '      <APP内入口路径>',
            '    core_data: |',
            '      <核心数据>',
            '    risks:',
            '      - <风险点，可为空>',
            '    estimated_testcases: <5-50>',
            '    estimated_smoke: <1-5>',
        ])
        focus_instruction = (
            f'【单次聚焦模式·最高优先级】本次你只需输出模块「{name}」这【一个】拆分项，'
            f'禁止输出其他模块。其余阶段1业务模块（{"、".join(expected_names)}）已由其他步骤处理，'
            f'你只需专注、基于上方需求摘要把「{name}」这一个模块补全。'
        )
        return render_prompt(
            P1_MODULE_SPLIT,
            requirement_yaml=self.prototype.requirement_summary,
            expected_module_list=expected_module_list,
            module_skeleton=single_skeleton,
            focus_instruction=focus_instruction,
        ) + self._kb_suffix()


    @_timed_stage('阶段1 结构化')
    def run_struct(self, force=False):
        """阶段1：按当前选中画布子集**逐画布独立**生成 page_summary；
        全局 requirement_summary 维护为已结构化画布的合并视图（不覆盖未参与画布的产物）。

        A2-c：你勾哪几个画布，那几个就作为一个批次一起走结构化；每个画布独立存 page_summary，
        未被选中的其他画布保留旧 page_summary，重跑时也不会被清掉。

        2026-08-31 增量：默认跳过已生成 page_summary 且阶段1完成的画布，避免分批勾选重跑时
        对旧画布重复调用模型烧 token（如第一次勾2个、第二次再加2个，第二次只跑新增的2个，
        澄清/设计时仍按全部勾选画布合并）。force=True 时强制全量重跑（画布内容变更需重新结构化时用）。
        注意：重跑阶段0读取会删除并重建画布（page_summary 随之清空），因此重新提取后的画布必然重新结构化。
        """
        self.prototype.status = 'structuring'
        self.prototype.save(update_fields=['status'])
        self._log('开始需求结构化分析（按选中画布子集逐画布独立生成）...')
        # 同名画布修复：统一走 _target_pages_qs()（画布级按 id 精确取），
        # 不再 filter(page_name__in=...)，否则同名画布会被一起结构化。
        pages = list(self._target_pages_qs())
        if not pages:
            self._log('没有可选画布，结构化中止')
            return ''
        # 增量：已有摘要且阶段1完成的画布跳过；force=True 时忽略跳过，全部重新结构化。
        pending, skipped = [], []
        for _p in pages:
            if not force and _p.page_summary and _p.current_stage and _p.current_stage >= 1:
                skipped.append(_p)
            else:
                pending.append(_p)
        per_page_summaries = [{'page': _p.page_name, 'summary': _p.page_summary or ''} for _p in skipped]
        if skipped:
            self._log(f'  ⏭ 增量跳过已结构化画布（不重跑）：{", ".join(_p.page_name for _p in skipped)}')
        for p in pending:
            # 终止检查点：用户可在阶段1中途停止，已结构化的画布产物保留，可后续续跑
            if TERMINATE_REQUESTED.get(self.prototype.id):
                TERMINATE_REQUESTED.pop(self.prototype.id, None)
                self._log('收到用户终止信号，提前停止（已完成画布产物已保留，可重新运行续跑）')
                self.prototype.requirement_summary = self._merge_page_summaries()
                self.prototype.status = 'extracted'
                self.prototype.save(update_fields=['status', 'requirement_summary'])
                return self.prototype.requirement_summary
            self._log(f'  ▶ 结构化画布 [{p.page_name}] ...')
            # 同名画布修复：带上 page_no 组成精确键，只取「这一张」画布的原文，
            # 否则 p15/p17 两个「连胜状态」的原文会一起进 LLM，摘要里就出现两个一样的模块。
            text = self._source_text_for_pages({(p.page_name, p.page_no)})
            prompt = render_prompt(P0_REQUIREMENT_STRUCT, requirement_text=text) + self._kb_suffix()
            out = self._llm(self.analyzer_config, prompt, max_tokens=8000, temperature=0)
            parsed = _parse_yaml(_extract_block(out, 'yaml'))
            if isinstance(parsed, dict):
                missing_sections = [
                    k for k in ('ambiguous_points', 'missing_points', 'boundary_points') if k not in parsed
                ]
                if missing_sections:
                    self._log(f'  [{p.page_name}] 结构化输出缺少 {"、".join(missing_sections)} 字段，自动重试一次...')
                    retry_prompt = (
                        prompt
                        + '\n\n【格式校验反馈】你上一次输出缺少以下字段：'
                        + '、'.join(missing_sections)
                        + '。请重新输出完整 YAML，这三个字段必须出现（确实没有时写空列表 []），并严格按模板结构输出。'
                    )
                    out = self._llm(self.analyzer_config, retry_prompt, max_tokens=8000, temperature=0)
                    parsed = _parse_yaml(_extract_block(out, 'yaml'))
            if isinstance(parsed, dict):
                # 逐画布独立：restore 只恢复「当前画布」的权威视觉/决策结构，
                # 防止把未勾选画布（如会员购买页优化、升级付费-Android）的视觉产物注入本画布摘要。
                parsed = self._restore_authoritative_decision_tables(parsed, {(p.page_name, p.page_no)})
                parsed = self._restore_authoritative_visual_structures(parsed, {(p.page_name, p.page_no)})
                parsed, merge_count, invalid_count = _postprocess_requirement_summary(
                    parsed, text
                )
                import yaml
                out = yaml.safe_dump(
                    parsed, allow_unicode=True, sort_keys=False, width=120
                )
                if merge_count:
                    self._log(f'  [{p.page_name}] 结构化后处理：合并 {merge_count} 个重复模块')
                if invalid_count:
                    self._log(f'  [{p.page_name}] 证据校验：{invalid_count} 项未匹配原始页面，已标记 evidence_valid=false')
            # 画布级独立产物
            p.page_summary = out or ''
            p.mark_stage(1, status='done', log=f'结构化完成（{len(out or "")} 字符）')
            p.save(update_fields=['page_summary', 'current_stage', 'stage_status', 'work_log'])
            per_page_summaries.append({'page': p.page_name, 'summary': out or ''})
            self._log(f'  ✔ 画布 [{p.page_name}] 结构化完成')
        # 维护全局合并视图：所有已有 page_summary 的画布合并（含本次未参与的）
        self.prototype.requirement_summary = self._merge_page_summaries()
        self.prototype.status = 'extracted'
        self.prototype.current_stage = 1
        self.prototype.save()
        self._advance_selected_pages(1)
        self._log('需求结构化完成（各画布独立摘要已生成，全局为合并视图）')
        return self.prototype.requirement_summary

    def _source_text_for_pages(self, page_names: set) -> str:
        """针对指定画布集合生成结构化输入（阶段1逐画布结构化时使用）。

        ⚠️ 2026-11-04 语义澄清：fused 条目的 ``source_pages`` 里混着两类名字——
          ① 主画布名（如 ``浏览-搜索``）；
          ② 该画布的**页面状态按钮名**（如 未聚焦 / 点击聚焦 / 用户输入 / 搜索结果 / 无结果）。
        一条 fused 需求的 ``interaction`` 本身就是「整个模块在所有状态下的完整需求」，
        所以**不能按"来源页数量"过滤**（那会把整模块需求误杀，导致状态覆盖不全）。
        画布归属的正确性由阶段0 的 ``_repair_source_coverage`` 保证：
        它只允许有真实内容关联的页写入 source_pages，无关画布仅打
        ``coverage_unattributed`` 标记而不再污染该字段。
        """
        if self.prototype.extracted_json:
            data = json.loads(self.prototype.extracted_json)
            parts = []
            _keys = _norm_page_keys(page_names)
            # fused_requirements 的 source_pages 只有名字、没有 page_no，无法区分同名画布，
            # 这里只能按名字匹配（已知局限）；下面 pages[] 有 page_no，走精确定位。
            _names = _page_names_of(_keys)
            fused = data.get('fused_requirements') or []
            if fused:
                fused = [
                    x for x in fused
                    if isinstance(x, dict)
                    and (_names & set(str(s) for s in (x.get('source_pages') or [])))
                ]
                if fused:
                    parts.append(
                        '## DOM + 视觉双路交叉融合需求（结构化主输入）\n'
                        + json.dumps(fused, ensure_ascii=False)
                    )
            for p in data.get('pages', []):
                if p.get('duplicate_of_page'):
                    continue
                if not _page_key_hit(p.get('page_name'), p.get('page_no'), _keys):
                    continue
                anno_str = json.dumps(p.get('annotations', []), ensure_ascii=False) if p.get('annotations') else '[]'
                va_str = json.dumps(p.get('visual_artifacts', []), ensure_ascii=False) if p.get('visual_artifacts') else '[]'
                parts.append(
                    f"### 画布：{p['page_name']}\n"
                    f"【核心需求标注/便利贴批注（本次需求增量核心，权重大）】：\n{anno_str}\n\n"
                    f"【结构化视觉内容 visual_artifacts（注意：ui_screen 仅代表底模线框图外观截图，非业务规则；rule_list/state_matrix/annotation 为规则批注）】：\n{va_str}\n\n"
                    f"【画布全景 OCR / 背景底模文字（仅供了解界面视觉上下文，未批注提及的底图固有文字非本次需求）】：\n{p.get('text','')}"
                )
            return '\n\n'.join(parts)
        return ''

    def _merge_page_summaries(self) -> str:
        """把所有已有 page_summary 的画布合并为全局 requirement_summary 视图（YAML 多个文档拼合）。

        2026-08-31 按用户诉求收紧：只合并「当前选中/参与批次」画布的摘要。
        此前合并所有历史画布，导致只选一个画布重跑结构化时，全局需求摘要仍显示整个版本。
        未选中画布的 page_summary 仍保留在库中，之后勾选并重跑即重新纳入。
        """
        # 同名画布修复：用 _target_pages_qs()（画布级按 id 精确取）取代按名字过滤，
        # 否则点 p17 结构化时，p15 与 p17 两张「连胜状态」的摘要会一起拼进全局摘要
        # → 界面出现两个一样的模块。
        pages = list(self._target_pages_qs().exclude(page_summary='').exclude(page_summary__isnull=True))
        if not pages:
            return ''
        blocks = []
        for p in pages:
            blocks.append(f'# ===== 画布：{p.page_name} =====\n{p.page_summary}')
        return '\n\n'.join(blocks)

    def _converged_conclusions(self) -> list:
        """读取阶段1已收敛为确定结论的歧义点清单（供阶段2/阶段3避免重复追问）。

        ⚠️ 2026-11-04：requirement_summary 是多画布拼接视图，整份 safe_load 会失败
        （中间带代码围栏时直接抛错 → 这里恒返回空，收敛结论全部丢失）。
        """
        data, _ = _parse_yaml_docs(self.prototype.requirement_summary or '')
        if not isinstance(data, dict):
            return []
        return data.get('converged_conclusions') or []

    # ---------- 阶段2：需求澄清 ----------
    @_timed_stage('阶段2 澄清')
    def run_clarify(self):
        self.prototype.status = 'clarifying'
        self.prototype.save(update_fields=['status'])
        self._log('开始需求澄清（识别模糊/缺失/边界）...')
        # 终止检查点：阶段2为单次 LLM 调用，触发后直接退出并回退为可续跑态
        if TERMINATE_REQUESTED.get(self.prototype.id):
            TERMINATE_REQUESTED.pop(self.prototype.id, None)
            self._log('收到用户终止信号，提前停止（已完成的画布产物已保留）')
            self.prototype.status = 'extracted'
            self.prototype.save(update_fields=['status'])
            return ''
        sel_names = self._selected_page_names()
        # 画布级隔离：若限定了画布子集（如单画布流水线触发澄清），结构化范围收敛为该画布集合的摘要，
        # 避免把其他画布（如定制mini形象）的摘要作为已确认范围送入当前画布审查。
        if sel_names:
            pages_to_clarify = list(self._target_pages_qs())
            summaries = [p.page_summary.strip() for p in pages_to_clarify if (p.page_summary or '').strip()]
            structured = '\n\n'.join(summaries) if summaries else ''
        else:
            structured = (self.prototype.requirement_summary or '').strip()
        source_text = self._source_text().strip()
        confirmed_scope = structured or source_text
        scope_note = (
            '结构化摘要是本阶段的需求范围边界。原始内容仅用于验证摘要是否有遗漏或无依据表述；'
            '不要针对摘要中已经排除、标记不做或超出范围的功能提问。'
            if structured and source_text else
            '当前没有独立的结构化摘要，请直接基于原始内容审查。'
        )
        converged = self._converged_conclusions()
        converged_note = ''
        if converged:
            converged_note = (
                '\n### 已收敛结论（阶段1已按默认假设确定，直接采纳、不得重复追问）\n'
                + json.dumps(converged, ensure_ascii=False)
                + '\n\n'
            )
        prompt = (
            '你是一名严格、保守的需求审查员。你的任务不是补写需求，而是对照原始内容审查'
            '已确认范围中的模糊点、完成当前流程所必需但缺失的信息，以及功能边界。\n\n'
            f'===== 已确认的结构化范围 =====\n{confirmed_scope}\n===== 范围结束 =====\n\n'
            f'===== 原始原型/需求内容（仅用于证据核对与查漏） =====\n'
            f'{source_text}\n===== 原始内容结束 =====\n\n'
            f'### 范围规则\n{scope_note}\n\n'
            f'{converged_note}'
            '### 审查规则\n'
            '1. 每个问题必须能指向具体页面、标注、字段或结构化条目。\n'
            '2. 原文已明确写出的内容不是缺失点，也不得拆成"模糊点"重复追问。尤其：当原文已清楚描述某交互行为，'
            '其未写明的部分属于该行为/该控件的【标准实现细节】时，禁止就此生成澄清问题，直接采纳行业通用实现作为默认结论。'
            '已明确的常见标准交互（均禁止追问）：\n'
            '   (a) 关闭类："点击关闭或空白处，关闭弹窗"（含"不保存选择"）→ 关闭按钮的位置/样式、"X"是否为关闭按钮、'
            '空白处的判定范围（弹窗外还是弹窗内非交互区）等均按行业惯例默认（点 X 或遮罩外区域=关闭并放弃未保存），不提问。\n'
            '   (b) 滚动选择器/列表/日历类：当原文形如"默认选中【外面/当前展示的 X】"+"上下滚动可切换"+"往前最多 N 年/不展示未来"，'
            '它就是一个【标准滚轮式/滚动式选择器】。此时：① 弹窗里"举例展示"的几个选项（如 7/8/9 月）只是当前上下文附近的快照，'
            '不能当作"固定只展示这几项"来追问"是只展示最近几个月还是展示完整 N 年列表"——可滚动列表初始展示当前/附近选项本就是标准，'
            '"往前最多 N 年"是滚动上边界，不是初始展示范围；② "默认选中外面展示的 X"+"上下滚动切换"已完整描述选中方式'
            '（滚动即切换选中项、选中项跟随滚动位置），不得追问"滚动时自动选中居中项还是需用户点击某一行"——这是同一回事，'
            '按行业惯例（滚轮选择器选中=当前位置）默认即可，不提问。\n'
            '   (c) 默认/重置类：当原文写明"每次进入/每次打开/每次刷新都默认展示 X"这类带【全称量词】的默认行为时，'
            '重置语义已被【每次】二字完整定义——无论用户此前做了什么选择、通过何种方式离开（切 Tab、返回上一页、刷新、关应用），'
            '重新进入一律重置为默认值 X。此时不得追问"用户选择后离开再返回，是保持上次选择还是重置为默认"——'
            '"保持上次选择"与"重置"根本不构成两种产品口径，原文已用"每次"拍板。同理，已写明的默认值/默认排序/默认选中，'
            '其反向场景（用户改过后回到入口）按"重新应用默认值"默认，不提问。\n'
            '3. 只有完成当前已确认流程确实必需、且原文未覆盖的信息，才能判为缺失点。\n'
            '4. 如果结构化摘要遗漏了范围内的原文，audit_kind 写 "结构化遗漏"。\n'
            '5. 如果结构化摘要中的表述在原文找不到依据，audit_kind 写 "结构化无依据"。\n'
            '6. 仅当某处存在【两种及以上产品意义上不同的理解、且会直接影响测试用例设计】时，才作为模糊点提出；'
            '标准控件（弹窗关闭、滚动选择器/列表/日历、开关、Tab、下拉、分页等）的默认交互方式不算模糊，'
            '其未写明的细节按行业惯例默认，不提问；边界约束（如"最多往前 N 年""不展示未来"）是滚动/可选范围的上下限，'
            '不是需要澄清的歧义点。已写明默认值、或带全称量词（每次/所有/一律/自动）的行为同样不构成歧义——量词本身就是产品拍板。\n'
            '单纯"实现细节未写"不算模糊（实现细节按行业惯例默认，不提问）。边界值/默认值/排序规则/异常分支/'
            '跨页面一致性等，仅在确实可能存在不同产品口径时才提，不得为找问题而放大。\n'
            '7. 严格避免凭空编造，但允许在已确认范围内指出"待确认的合理假设"。\n'
            '8. 质量优先于数量：只输出真正有价值、需要产品拍板的问题。若范围内确无值得提问的点，如实输出空数组；'
            '严禁为凑数生成已由原文覆盖的低质量/显然问题（例如对已写明"点击关闭或空白处关闭弹窗"再追问关闭按钮细节）。\n\n'
            '### 输出要求\n'
            '**必须且仅**输出一个 JSON 数组，不要使用 markdown 代码块或附加说明。'
            '数组每项字段如下：\n'
            '- type: 仅限 "模糊点" / "缺失点" / "边界点"\n'
            '- audit_kind: 仅限 "原文歧义" / "结构化遗漏" / "结构化无依据" / "信息缺失" / "边界不清"\n'
            '- location: 具体页面、标注、字段或结构化条目\n'
            '- evidence: 支持判断的简短原文；找不到直接依据时写 "未找到直接原文"\n'
            '- issue: 客观描述问题，不得把推断写成事实\n'
            '- suggested_question: 可由产品明确回答的单一问题\n'
            '- priority: "高" / "中" / "低"\n'
            '- impact: 受影响的测试场景或模块\n\n'
            '示例（仅表示格式，不得复制内容）：\n'
            '[{"type":"缺失点","audit_kind":"信息缺失","location":"登录页/密码字段",'
            '"evidence":"原文仅写登录密码","issue":"密码规则未说明",'
            '"suggested_question":"密码允许的长度和字符范围是什么？","priority":"中","impact":"登录校验"}]\n'
            '反例（禁止输出，属于对已明确标准交互的冗余追问）：\n'
            '[{"type":"模糊点","audit_kind":"原文歧义","location":"便利贴批注2/页面2",'
            '"evidence":"原文已写：点击关闭或空白处，不保存选择，关闭弹窗","issue":"关闭按钮的具体位置和样式未明确",'
            '"suggested_question":"弹窗中关闭按钮的具体位置和样式是什么？X 是否为关闭按钮？点击空白处的判定范围是弹窗外还是弹窗内非交互区？",'
            '"priority":"中","impact":"弹窗关闭"}]'
            '—— 这类问题原文已完整描述行为，关闭按钮/X/空白处均属标准实现，按行业惯例默认即可，禁止输出。\n'
            '[{"type":"模糊点","audit_kind":"原文歧义","location":"便利贴批注3/页面2",'
            '"evidence":"原文已写：默认选中外面展示的月份；上下滚动可以切换；往前最多可以三年，不展示未来月份",'
            '"issue":"弹窗初始展示范围与滚动选中方式存在歧义","'
            '"suggested_question":"弹窗打开时月份列表初始展示范围是什么（仅最近3个月还是完整3年列表）？滚动时是自动选中居中月份还是需要点击？",'
            '"priority":"高","impact":"弹窗交互"}]'
            '—— 这类问题把标准滚轮式选择器的"举例快照(7/8/9月)"误读为固定列表，把边界约束"往前最多3年"误读为初始展示范围，'
            '又把"默认选中+滚动切换"拆成两种选中方式追问；均属标准控件行为，按行业惯例默认（可滚动列表、当前月预选、滚动即切换选中、上边界3年/无未来月），禁止输出。'
            '[{"type":"模糊点","audit_kind":"原文歧义","location":"日历页面",'
            '"evidence":"原文已写：每次进入日历默认展示当前月份","issue":"用户切换月份后离开再返回，月份是保持还是重置未明确",'
            '"suggested_question":"用户选择历史月份后离开日历页面（如切换到其他Tab）再返回，月份和内容应保持上次选择还是重置为当前月份？",'
            '"priority":"中","impact":"跨页面状态保持"}]'
            '—— 原文"每次进入默认展示当前月份"中的【每次】已定义重置语义：无论切 Tab、返回还是刷新，重新进入一律重置为当前月，'
            '"保持上次选择"与"重置"不构成两种产品口径，禁止输出。'
        )
        out = self._llm(self.analyzer_config, prompt, max_tokens=8000, temperature=0)
        # 先剥除 markdown code block 包裹（temperature=0 下模型更倾向用 ```json 包裹）
        stripped = _extract_block(out, 'json')
        if not stripped:
            stripped = _extract_block(out, None)
        raw_to_parse = stripped or out or ''
        try:
            clarify_items = json.loads(raw_to_parse)
        except Exception:
            # 容错：当条数过多命中 max_tokens 导致 JSON 列表被截断（末尾缺少 ]）时，截取至最后一个完整对象闭合
            try:
                last_brace = raw_to_parse.rfind('},')
                if last_brace != -1:
                    clarify_items = json.loads(raw_to_parse[:last_brace+1] + '\n]')
                else:
                    clarify_items = None
            except Exception:
                clarify_items = None
        if isinstance(clarify_items, list):
            invalid_count = 0
            for item in clarify_items:
                if not isinstance(item, dict):
                    continue
                valid = _validate_evidence(item.get('evidence'), source_text)
                item['evidence_valid'] = valid
                invalid_count += int(not valid)
            out = json.dumps(clarify_items, ensure_ascii=False, indent=2)
            if invalid_count:
                self._log(f'澄清证据校验：{invalid_count} 项未匹配原始页面')
        self.prototype.clarification_log = out
        # A2-c：澄清清单写回画布时按画布归属隔离（各画布只保留归属于自己的澄清项，防止跨画布串台）
        sel_names = self._selected_page_names()
        # 同名画布修复：按 id 精确取，避免同名画布一起被写回/一起推进阶段。
        pages = list(self._target_pages_qs()) if sel_names else list(self.prototype.pages.all())
        all_pages_list = list(self.prototype.pages.all())
        for p in pages:
            if isinstance(clarify_items, list):
                matched = []
                for item in clarify_items:
                    if not isinstance(item, dict):
                        continue
                    loc = str(item.get('location') or '')
                    # 同名画布修复：优先按「第N页」精确定位。location 里带了页码但与
                    # 本画布 page_no 不符时，不再用「同名」兜底，否则 p15/p17 两个
                    # 「连胜状态」会同时认领同一条澄清项 → 界面出现两个一样的模块。
                    no_hit = bool(p.page_no) and f'第{p.page_no}页' in loc
                    numbered_elsewhere = (not no_hit) and ('页' in loc) and any(
                        f'第{other.page_no}页' in loc
                        for other in all_pages_list
                        if other.id != p.id and other.page_no
                    )
                    is_match = no_hit or ((not numbered_elsewhere) and p.page_name in loc)
                    # 单画布独立运行模式兜底：如果该条目并未明确指向其他已知画布，也归属当前画布
                    if not is_match and len(pages) == 1:
                        other_names = [other.page_name for other in all_pages_list if other.id != p.id and other.page_name]
                        if not any(on in loc for on in other_names):
                            is_match = True
                    if is_match:
                        matched.append(item)
                p_out = json.dumps(matched, ensure_ascii=False, indent=2) if matched else '[]'
                log_cnt = len(matched)
            else:
                p_out = out or ''
                log_cnt = 0
            p.page_clarification = p_out
            p.mark_stage(2, status='done', log=f'澄清完成（{log_cnt} 项）')
            p.save(update_fields=['page_clarification', 'current_stage', 'stage_status', 'work_log'])
        self.prototype.status = 'extracted'
        self.prototype.current_stage = 2
        self.prototype.save()
        self._advance_selected_pages(2)
        self._log('需求澄清完成（已按画布隔离归属写回各画布）')
        return out

    # ---------- 阶段3：用例设计（核心）----------
    def run_design(self):
        """完整跑阶段3（兼容旧入口）：第一段测试点合并 + 第二段用例生成，一口气到底。"""
        self.run_design_phase1()
        return self.run_design_phase2()

    # ---------- 阶段3 画布级编排（A2-c：按选中子集逐画布生成，产物按画布存）----------
    def _selected_pages_qs(self):
        sel_names = self._selected_page_names()
        qs = self.prototype.pages.all()
        if sel_names:
            qs = self._target_pages_qs()
        return qs

    def _merge_page_testpoints(self) -> str:
        """合并所有已有 page_testpoints_json 的画布为全局 final_testpoints_json 视图。"""
        pages = list(self.prototype.pages.exclude(page_testpoints_json='').exclude(page_testpoints_json__isnull=True))
        merged = []
        for p in pages:
            try:
                tps = json.loads(p.page_testpoints_json or '[]')
            except Exception:
                continue
            if isinstance(tps, list):
                merged.extend(tps)
        return json.dumps(merged, ensure_ascii=False)

    @staticmethod
    def _clarify_resolutions_text(raw: str) -> str:
        """把澄清清单里人工填写的结论抽成「位置：结论」清单（阶段3 兜底输入用）。

        兼容纯 JSON / ```json 代码块 / 首个 [...] 片段三种写法（与前端同一套策略）。
        """
        import re as _re
        text = (raw or '').strip()
        if not text:
            return ''
        data = None
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is None:
            m = _re.search(r'```(?:json|yaml|yml)?\s*\n([\s\S]*?)(?:```|$)', text, _re.I)
            if m:
                try:
                    data = json.loads(m.group(1).strip())
                except Exception:
                    data = None
        if data is None:
            m2 = _re.search(r'\[[\s\S]*?\](?=\s*[\n]|$)', text)
            if m2:
                try:
                    data = json.loads(m2.group(0))
                except Exception:
                    data = None
        if not isinstance(data, list):
            return ''
        lines = []
        for it in data:
            if not isinstance(it, dict):
                continue
            r = str(it.get('resolution') or it.get('answer') or '').strip()
            if not r:
                continue
            lines.append(f"- {it.get('location') or it.get('type') or '待确认项'}：{r}")
        return '\n'.join(lines)

    def _merge_page_testcases(self) -> tuple:
        """合并所有已有 page_testcases_json 的画布为全局 testcases/smoke 视图。"""
        cases, smokes = [], []
        for p in self.prototype.pages.exclude(page_testcases_json='').exclude(page_testcases_json__isnull=True):
            try:
                cs = json.loads(p.page_testcases_json or '[]')
            except Exception:
                cs = []
            if isinstance(cs, list):
                cases.extend(cs)
            try:
                sj = json.loads(p.page_smoke_json or '{}')
            except Exception:
                sj = {}
            if isinstance(sj, dict) and sj.get('smoke_testcases'):
                smokes.extend(sj['smoke_testcases'])
        return cases, ({'smoke_testcases': smokes} if smokes else {})

    def run_design_phase1_for_selection(self):
        """A2-c：按当前选中画布子集，逐个画布独立生成测试点，写各画布 page_testpoints_json；
        全局 final_testpoints_json 维护为已生成画布的合并视图（不覆盖未参与画布）。"""
        self.prototype.status = 'designing'
        self.prototype.save(update_fields=['status'])
        self._log('开始按选中画布逐画布生成测试点（阶段3 · 第一段）...')
        pages = list(self._selected_pages_qs())
        if not pages:
            self._log('没有可选画布，测试点生成中止')
            return {'final_testpoints': 0, 'phase': 1}
        total = 0
        for p in pages:
            # 终止检查点：用户可在阶段3第一段中途停止，已生成测试点的画布保留
            if TERMINATE_REQUESTED.get(self.prototype.id):
                TERMINATE_REQUESTED.pop(self.prototype.id, None)
                self._log('收到用户终止信号，提前停止（已完成画布产物已保留，可重新运行续跑）')
                self.prototype.final_testpoints_json = self._merge_page_testpoints()
                self.prototype.status = 'extracted'
                self.prototype.save(update_fields=['status', 'final_testpoints_json'])
                return {'final_testpoints': len(json.loads(self.prototype.final_testpoints_json or '[]')), 'phase': 1, 'terminated': True}
            self._log(f'  ▶ 画布[{p.page_name}] 测试点合并...')
            try:
                res = self.run_design_phase1(page=p)
                cnt = res.get('final_testpoints', 0) if isinstance(res, dict) else 0
                total += cnt
                self._log(f'  ✔ 画布[{p.page_name}] 测试点完成（{cnt} 条）')
            except Exception as e:
                logger.error(f'画布[{p.page_name}] 测试点生成失败: {e}')
                self._log(f'  ✗ 画布[{p.page_name}] 测试点生成失败: {e}')
                p.mark_stage(3, status='failed', error=str(e))
                p.save(update_fields=['current_stage', 'stage_status', 'error_message'])
                continue
        # 维护全局合并视图
        self.prototype.final_testpoints_json = self._merge_page_testpoints()
        self.prototype.status = 'testpoints_ready'
        self.prototype.current_stage = 3
        self.prototype.save()
        self._advance_selected_pages(3)
        self._log(f'第一段（画布级）完成！共覆盖 {len(pages)} 个画布，全局合并 {len(json.loads(self.prototype.final_testpoints_json or "[]"))} 测试点。')
        return {'final_testpoints': len(json.loads(self.prototype.final_testpoints_json or '[]')), 'phase': 1}

    def run_design_phase2_for_selection(self):
        """A2-c：按当前选中画布子集，逐个画布独立生成用例，写各画布 page_testcases_json；
        全局 testcases_json/smoke_json 维护为已生成画布的合并视图。"""
        self.prototype.status = 'designing'
        self.prototype.save(update_fields=['status'])
        self._log('开始按选中画布逐画布生成用例（阶段3 · 第二段）...')
        pages = list(self._selected_pages_qs())
        if not pages:
            self._log('没有可选画布，用例生成中止')
            return {'testcases': 0, 'phase': 2}
        for p in pages:
            # 终止检查点：用户可在阶段3第二段中途停止，已生成用例的画布保留
            if TERMINATE_REQUESTED.get(self.prototype.id):
                TERMINATE_REQUESTED.pop(self.prototype.id, None)
                self._log('收到用户终止信号，提前停止（已完成画布产物已保留，可重新运行续跑）')
                cases, smoke = self._merge_page_testcases()
                self.prototype.testcases_json = json.dumps(cases, ensure_ascii=False)
                self.prototype.smoke_json = json.dumps(smoke, ensure_ascii=False) if smoke else self.prototype.smoke_json
                self.prototype.status = 'extracted'
                self.prototype.save(update_fields=['status', 'testcases_json', 'smoke_json'])
                return {'testcases': len(cases), 'phase': 2, 'terminated': True}
            if not (p.page_testpoints_json or '').strip() or p.page_testpoints_json.strip() in ('[]', '{}'):
                self._log(f'  画布[{p.page_name}] 暂无测试点，跳过')
                continue
            self._log(f'  ▶ 画布[{p.page_name}] 用例生成...')
            try:
                self.run_design_phase2(page=p)
                self._log(f'  ✔ 画布[{p.page_name}] 用例完成')
            except Exception as e:
                logger.error(f'画布[{p.page_name}] 用例生成失败: {e}')
                self._log(f'  ✗ 画布[{p.page_name}] 用例生成失败: {e}')
                p.mark_stage(4, status='failed', error=str(e))
                p.save(update_fields=['current_stage', 'stage_status', 'error_message'])
                continue
        # 维护全局合并视图
        cases, smoke = self._merge_page_testcases()
        self.prototype.testcases_json = json.dumps(cases, ensure_ascii=False)
        self.prototype.smoke_json = json.dumps(smoke, ensure_ascii=False) if smoke else self.prototype.smoke_json
        # 自愈闭环(P0)：聚合各画布 AI 原始快照为任务级快照（此时人工尚未编辑，内容即 AI 首版）
        ai_agg = []
        for _p in pages:
            try:
                _ai = json.loads(_p.page_ai_testcases_json or '[]')
            except Exception:
                _ai = []
            if isinstance(_ai, list):
                ai_agg.extend(_ai)
        self.prototype.ai_testcases_json = json.dumps(ai_agg, ensure_ascii=False)
        self.prototype.status = 'done'
        self.prototype.current_stage = 3
        self.prototype.save()
        self._advance_selected_pages(4)
        self._log(f'第二段（画布级）完成！共 {len(cases)} 条用例。可分别/一起采纳。')
        return {'testcases': len(cases), 'phase': 2}

    # ---------- A2-c 采纳与汇总 ----------
    def set_pages_approved(self, page_ids=None, approve_all_selected=False):
        """标记画布用例为已采纳。

        - page_ids: 指定画布 id 列表，置 page_approved=True；
        - approve_all_selected: True 时把当前选中且已生成用例的画布全部置为已采纳。
        返回受影响的画布数。
        """
        pages = self.prototype.pages.all()
        if page_ids:
            pages = pages.filter(id__in=page_ids)
        elif approve_all_selected:
            sel = self._selected_page_names()
            if sel:
                pages = self._target_pages_qs()
            pages = pages.exclude(page_testcases_json='').exclude(page_testcases_json__isnull=True)
        else:
            return 0
        n = 0
        for p in pages:
            p.page_approved = True
            p.save(update_fields=['page_approved'])
            n += 1
        self._log(f'已标记 {n} 个画布用例为「已采纳」')
        self.merge_approved()
        return n

    @staticmethod
    def _case_dedup_key(c):
        """(title, module) 去重键，用于人工补充用例的幂等合并。"""
        if not isinstance(c, dict):
            return None
        inner = c.get('testcase', c)
        inner = inner if isinstance(inner, dict) else c
        return (str(inner.get('title') or '').strip(),
                str(c.get('module') or inner.get('module') or '').strip())

    def merge_approved(self):
        """把所有 page_approved=True 的画布用例合并去重，写入全局 merged_testcases_json / merged_smoke_json。"""
        approved = list(self.prototype.pages.filter(page_approved=True))
        merged_cases, merged_smoke = [], []
        # 跨画布向量去重：复用 _vector_dedup（按标题/描述语义去重）
        def _case_inner(c):
            """用例可能是 {testcase:{...}, module, _raw} 嵌套结构，统一展开取真实用例体。"""
            if not isinstance(c, dict):
                return {}
            inner = c.get('testcase', c)
            return inner if isinstance(inner, dict) else c
        all_for_dedup = []
        for p in approved:
            try:
                cs = json.loads(p.page_testcases_json or '[]')
            except Exception:
                cs = []
            if not isinstance(cs, list):
                continue
            for c in cs:
                if isinstance(c, dict):
                    inner = _case_inner(c)
                    all_for_dedup.append({
                        'id': inner.get('id') or c.get('id') or f"{p.page_name}-{len(all_for_dedup)}",
                        'title': inner.get('title') or '',
                        'description': inner.get('description') or '',
                        'priority': inner.get('priority', c.get('priority', 'P1')),
                        'source': ['page', p.page_name],
                        '_page': p.page_name,
                    })
        deduped = all_for_dedup
        if self.embedder_config and all_for_dedup:
            deduped, _, _ = self._vector_dedup(all_for_dedup)
        # 还原为原始用例结构（去掉内部字段）
        for d in deduped:
            page_name = d.get('_page')
            p = next((pp for pp in approved if pp.page_name == page_name), None)
            if not p:
                continue
            try:
                cs = json.loads(p.page_testcases_json or '[]')
            except Exception:
                cs = []
            match = None
            for c in cs:
                if not isinstance(c, dict):
                    continue
                inner = _case_inner(c)
                if (inner.get('title') or '') == (d.get('title') or '') and (inner.get('description') or '') == (d.get('description') or ''):
                    match = c
                    break
            if match:
                merged_cases.append(match)
        for p in approved:
            try:
                sj = json.loads(p.page_smoke_json or '{}')
            except Exception:
                sj = {}
            if isinstance(sj, dict) and sj.get('smoke_testcases'):
                merged_smoke.extend(sj['smoke_testcases'])
        # 保留「人工补充」用例（_manual=True）：否则重新汇总会把人工新增的用例冲掉，
        # 偏差对比就永远看不到人工补充，自愈闭环断在第 2 环。
        try:
            prev = json.loads(self.prototype.merged_testcases_json or '[]')
        except Exception:
            prev = []
        if isinstance(prev, list):
            base_keys = {self._case_dedup_key(c) for c in merged_cases}
            for c in prev:
                if not (isinstance(c, dict) and c.get('_manual')):
                    continue
                k = self._case_dedup_key(c)
                if k is None or k in base_keys:
                    continue  # 画布里已有同名同模块用例，不重复并入
                base_keys.add(k)
                merged_cases.append(c)
        self.prototype.merged_testcases_json = json.dumps(merged_cases, ensure_ascii=False)
        self.prototype.merged_smoke_json = json.dumps({'smoke_testcases': merged_smoke}, ensure_ascii=False) if merged_smoke else ''
        self.prototype.save(update_fields=['merged_testcases_json', 'merged_smoke_json'])
        self._log(f'汇总完成：{len(merged_cases)} 条用例（跨画布去重后），来自 {len(approved)} 个已采纳画布')
        return {'merged_cases': len(merged_cases), 'approved_pages': len(approved)}

    # ---------- 阶段3 第一段：模块拆分→测试点→风险→PCI→三路合并 ----------
    @_timed_stage('阶段3-1 测试点合并')
    def run_design_phase1(self, page=None):
        """产出 module_split / risks_json / pci_json / final_testpoints_json，
        完成后 status='testpoints_ready'，等待人工确认/编辑测试点后再调 phase2 生成用例。

        A2-c 画布级：若传入 page（ModaoPage 实例），则仅基于该画布的 page_summary / page_clarification
        独立生成测试点，return 给调用方写 page.page_testpoints_json；不落全局字段。
        不传 page 时维持原「选中子集联合」语义（落库全局 final_testpoints_json）。
        """
        self.prototype.status = 'designing'
        self.prototype.save(update_fields=['status'])
        summary_is_fallback = False
        if page is not None:
            summary_text = page.page_summary or ''
            clarification_text = page.page_clarification or ''
            page_names = {page.page_name}
            scope_label = f'画布[{page.page_name}]'
        else:
            summary_text = self.prototype.requirement_summary or ''
            clarification_text = self.prototype.clarification_log or ''
            page_names = None
            scope_label = '任务级'
        # 2026-09-14 治本修复：需求输入为空时【不能照跑】。
        # 旧行为：page_summary 为空 → 需求输入是空串，但 prompt 末尾照旧拼
        # _kb_suffix(query_hint='')，而空 query 会退化为 **全量项目知识库**注入，
        # 模型没有需求可依据，只能顺着知识库编模块和测试点。
        # 实测任务70 画布「人物定制流程修改」（从未跑①结构化，page_summary 为空）生成出
        # 知识库里的「装扮 / 互动 / 通用付费页」共 50 条与本画布无关的测试点。
        if not summary_text.strip():
            fallback_src = (
                self._source_text_for_pages(page_names) if page is not None
                else self._source_text(page_names)
            ).strip()
            resolved = self._clarify_resolutions_text(clarification_text)
            if not fallback_src and not resolved:
                msg = (f'{scope_label} 缺少需求内容（结构化摘要、原文、澄清结论均为空），'
                       f'已中止测试点生成以免凭空编造，请先运行①结构化。')
                self._log(msg)
                self.prototype.status = 'extracted'
                self.prototype.error_message = msg
                self.prototype.save(update_fields=['status', 'error_message'])
                return {'modules': 0, 'final_testpoints': 0,
                        'error': 'no_requirement_source', 'phase': 1}
            summary_text = (
                f'### 需求原文（本范围尚未生成结构化摘要，以下直接按原文兜底）\n{fallback_src}'
                + (f'\n\n### 已确认澄清结论（人工填写，视为已拍板，不再追问）\n{resolved}' if resolved else '')
            )
            summary_is_fallback = True
            self._log(f'{scope_label} 未生成结构化摘要，已用「原文+澄清结论」兜底（建议先运行①结构化，模块拆分更准）')
        self._log('开始用例设计（阶段3 · 第一段：测试点合并）...')

        # 3.1 模块拆分
        self._log('3.1 正在拆分模块...')
        # 治本修复：提前提取阶段1已锁定的业务模块清单，作为硬约束注入拆分 prompt，
        # 防止 LLM 在长文本中被细节淹没而漏拆相对次要的业务模块。
        # 兜底模式下 summary_text 是「原文 + 澄清结论」的自由文本，不是 YAML，不要拿去解析
        # （否则会把原文里的 "key: value" 行误解析成模块清单）。此时按「无阶段1清单」逻辑走。
        # ⚠️ 2026-11-04：任务级 summary_text 是「多画布 YAML 拼接视图」，
        # 必须逐段解析后合并，不能用 _parse_yaml/_extract_block 处理整份
        # （实测 proto=92：整份解析失败 → expected_names 为空 → 模块拆分退化为自由发挥，
        #   或只命中某段围栏内的 1 个画布）。
        if summary_is_fallback:
            summary_yaml = {}
        elif page is not None:
            summary_yaml = _parse_yaml(_extract_block(summary_text or '', 'yaml')) or {}
        else:
            merged_summary, _blocks_info = _parse_yaml_docs(summary_text or '')
            if merged_summary:
                summary_yaml = merged_summary
                ok_n = sum(1 for b in _blocks_info if b['ok'])
                if len(_blocks_info) > 1:
                    self._log(
                        f'需求摘要多画布合并视图：{ok_n}/{len(_blocks_info)} 段解析成功，'
                        f'合并模块 {len(summary_yaml.get("modules") or [])} 个'
                    )
                if ok_n < len(_blocks_info):
                    bad = [b['page'] for b in _blocks_info if not b['ok']]
                    self._log(f'⚠️ 以下画布摘要解析失败（已跳过，建议重跑①结构化）：{"、".join(bad)}')
            else:
                summary_yaml = _parse_yaml(_extract_block(summary_text or '', 'yaml')) or {}
        summary_modules = summary_yaml.get('modules', []) if isinstance(summary_yaml, dict) else []
        expected_names = [m.get('name', '').strip() for m in summary_modules if isinstance(m, dict) and m.get('name')]
        if expected_names:
            expected_module_list = '\n'.join(f'- {name}' for name in expected_names)
            # 治本修复（预填骨架）：把阶段1锁定的模块名直接写进输出 YAML 的 modules: 列表，
            # 逼 LLM 做「补全」而非「自由决定列哪些模块」。completion 偏置远强于指令，
            # 可彻底规避 LLM 在长输入前忽略铁律而漏拆次要模块的问题。
            skeleton_lines = ['modules:']
            for name in expected_names:
                skeleton_lines.append(f'  - name: {name}')
                skeleton_lines.append('    sub_pages:')
                skeleton_lines.append('      - <待补充：根据需求摘要填入该模块的页面/子流程>')
                skeleton_lines.append('    priority: <P0/P1/P2>')
                skeleton_lines.append('    features:')
                skeleton_lines.append('      - <待补充：该模块核心功能1>')
                skeleton_lines.append('      - <待补充：该模块核心功能2>')
                skeleton_lines.append('    entry_path: |')
                skeleton_lines.append('      <待补充：APP内入口路径>')
                skeleton_lines.append('    core_data: |')
                skeleton_lines.append('      <待补充：核心数据>')
                skeleton_lines.append('    risks:')
                skeleton_lines.append('      - <待补充，可为空>')
                skeleton_lines.append('    decision_tables:')
                skeleton_lines.append('      - <若该模块有decision_table则原样复制，否则删除此字段>')
                skeleton_lines.append('    visual_structures:')
                skeleton_lines.append('      - <若该模块有visual_structure则原样复制，否则删除此字段>')
                skeleton_lines.append('    estimated_testcases: <待补充 5-50>')
                skeleton_lines.append('    estimated_smoke: <待补充 1-5>')
            module_skeleton = '\n'.join(skeleton_lines)
        else:
            expected_module_list = '（阶段1未提取到明确模块清单，请基于需求摘要自行识别全部业务模块）'
            module_skeleton = 'modules:\n  - name: <待补充：基于需求摘要自行识别全部业务模块，逐个列出>'
        split_prompt = render_prompt(
            P1_MODULE_SPLIT,
            requirement_yaml=summary_text,
            expected_module_list=expected_module_list,
            module_skeleton=module_skeleton,
        ) + self._kb_suffix(query_hint=summary_text[:500] if summary_text else '')
        split_out, split_yaml, modules, missing_modules = '', {}, [], []
        if expected_names:
            # 整列 LLM 拆分（单次）。覆盖交由下方「单模块聚焦」安全网保证——
            # 实测整列重试仍会在长文本中被主要模块淹没而漏拆次要模块，不可依赖。
            split_out = self._llm(self.analyzer_config, split_prompt, max_tokens=6000, temperature=0, timeout=600)
            split_yaml = _parse_yaml(_extract_block(split_out, 'yaml')) or {}
            modules = split_yaml.get('modules', []) if isinstance(split_yaml, dict) else []
            actual_names = [m.get('name', '').strip() for m in modules if isinstance(m, dict) and m.get('name')]
            missing_modules = [
                name for name in expected_names
                if not any(name in actual or actual in name for actual in actual_names)
            ]
            # 治本安全网：对仍遗漏的模块逐个做「单模块聚焦」补全（每次只问一个模块，
            # LLM 不会被其他模块挤掉注意力）；聚焦仍失败的则合成占位模块，保证覆盖不阻断下游。
            for name in list(missing_modules):
                focused_prompt = self._build_focused_split_prompt(name, expected_names, expected_module_list)
                fout = self._llm(self.analyzer_config, focused_prompt, max_tokens=3000, temperature=0)
                fy = _parse_yaml(_extract_block(fout, 'yaml')) or {}
                fm = [m for m in (fy.get('modules', []) if isinstance(fy, dict) else [])
                      if isinstance(m, dict) and m.get('name')]
                if fm:
                    fm[0]['name'] = name  # 强制 name 与阶段1一致
                    modules.append(fm[0])
                    self._log(f'聚焦补全模块：{name}')
                else:
                    modules.append({
                        'name': name, 'priority': 'P1', 'sub_pages': [], 'features': [],
                        'risks': [], 'estimated_testcases': 5, 'estimated_smoke': 1,
                        '_synthesized': True,
                    })
                    self._log(f'警告：模块「{name}」LLM 无法生成，已合成占位模块（覆盖已保证，内容需人工确认）')
                missing_modules = [n for n in missing_modules if n != name]
        else:
            # 无阶段1清单：维持原整列逻辑（无强制覆盖目标）
            for attempt in range(2):  # 首次拆分 + 遗漏时自动重试一次
                prompt = split_prompt
                if attempt and missing_modules:
                    prompt += (
                        '\n\n【重要·覆盖校验反馈】你上一次拆分遗漏了以下模块，'
                        '本次输出的 modules 列表必须包含它们（模块名称保持与下列名称一致，不得改名或合并省略）：'
                        + '、'.join(missing_modules)
                    )
                split_out = self._llm(self.analyzer_config, prompt, max_tokens=6000, temperature=0)
                split_yaml = _parse_yaml(_extract_block(split_out, 'yaml')) or {}
                modules = split_yaml.get('modules', []) if isinstance(split_yaml, dict) else []
                actual_names = [m.get('name', '').strip() for m in modules if isinstance(m, dict) and m.get('name')]
                missing_modules = [
                    name for name in expected_names
                    if not any(name in actual or actual in name for actual in actual_names)
                ]
                if not missing_modules:
                    break
                if attempt == 0:
                    self._log('模块覆盖校验：拆分遗漏 ' + '、'.join(missing_modules) + '，自动补全重试一次...')
        # 解析保险：模型明明产出了可观内容，却解析出 0 个模块 → 判定为「解析失败」，
        # 显式报错而非静默按 0 条继续（静默 0 会让下游 0 测试点/0 用例，且日志仍显示“完成”）。
        if (not modules) and split_out and len(split_out) > 200 and 'modules' in split_out:
            self.prototype.module_split = split_out
            self.prototype.status = 'failed'
            self.prototype.error_message = (
                '模块拆分结果解析失败：模型已产出内容，但无法解析为有效模块列表'
                '（常见原因为 YAML 非法，如字符串引号半包裹）。请检查原始输出后重试。'
            )
            self.prototype.save(update_fields=['module_split', 'status', 'error_message'])
            self._log(self.prototype.error_message)
            return {'modules': 0, 'final_testpoints': 0,
                    'error': 'module_split_parse_failed', 'phase': 1}
        decision_tables = (
            summary_yaml.get('decision_tables', [])
            if isinstance(summary_yaml, dict) else []
        ) or self._source_decision_tables(page_names)
        visual_structures = (
            summary_yaml.get('visual_structures', [])
            if isinstance(summary_yaml, dict) else []
        ) or self._source_visual_structures(page_names)
        for table in decision_tables:
            related = _normalize_evidence_text(table.get('related_module'))
            candidates = [
                module for module in modules
                if related and (
                    related in _normalize_evidence_text(module.get('name'))
                    or _normalize_evidence_text(module.get('name')) in related
                )
            ]
            if not candidates and len(modules) == 1:
                candidates = modules
            for module in candidates:
                module.setdefault('decision_tables', []).append(table)
        for structure in visual_structures:
            if structure.get('artifact_type') == 'decision_table':
                continue
            related = _normalize_evidence_text(structure.get('related_module'))
            candidates = [
                module for module in modules
                if related and (
                    related in _normalize_evidence_text(module.get('name'))
                    or _normalize_evidence_text(module.get('name')) in related
                )
            ]
            if not candidates and len(modules) == 1:
                candidates = modules
            for module in candidates:
                module.setdefault('visual_structures', []).append(structure)
        # 模块再细分：对细节特别多的阶段1模块（如贴纸的 切换/删除/添加/编辑 被锁成 1 个粗模块）
        # 按 fused 来源聚类拆成细模块，避免 P2 单次生成超容漏子功能（方案2）
        if not missing_modules:
            sub_modules = self._split_heavy_module_by_fused(modules)
            if len(sub_modules) != len(modules):
                modules = sub_modules
                # 重新序列化回 module_split
                if isinstance(split_yaml, dict):
                    split_yaml['modules'] = modules
                import yaml
                split_out = yaml.safe_dump(split_yaml, allow_unicode=True, sort_keys=False, width=120)
        self.prototype.module_split = split_out

        if missing_modules:
            self.prototype.status = 'failed'
            self.prototype.error_message = (
                '模块覆盖校验失败，模块拆分遗漏：' + '、'.join(missing_modules)
                + '。请修正模块拆分后再生成测试点。'
            )
            self.prototype.save(update_fields=['module_split', 'status', 'error_message'])
            self._log(self.prototype.error_message)
            return {'modules': len(modules), 'final_testpoints': 0, 'error': 'module_coverage_failed',
                    'missing_modules': missing_modules, 'phase': 1}
        self._log(f'3.1 模块拆分完成，共 {len(modules)} 个模块')


        all_final_tps = []      # 全部模块的最终测试点
        all_risks = []
        all_pci = []
        dedup_audit = []

        for idx, mod in enumerate(modules, 1):
            mod_name = mod.get('name', '未命名模块')
            self._log(f'3.2 开始处理模块[{idx}/{len(modules)}] {mod_name} ...')
            mod_ctx = json.dumps(mod, ensure_ascii=False)
            try:
                # 3.3 基础测试点 (draft)
                mod_features = mod.get('features', [])
                feature_count_hint = ''
                if isinstance(mod_features, list) and mod_features:
                    feature_count_hint = (
                        f'\n\n【覆盖要求】本模块有 {len(mod_features)} 个新增/变更功能点（features），你必须覆盖这些功能点。'
                        f'此外还需覆盖：① 该模块修改后可能影响的老功能回归验证；② 跨模块联动影响。'
                        f'模块 features 列表：\n{chr(10).join("- " + str(f) for f in mod_features)}'
                        f'\n请确保生成的 test_points 涵盖功能测试 + 回归验证 + 联动场景，不要生成脱离本模块具体场景的通用模板。'
                    )
                # 注入对应原型的页面 text（按模块名模糊匹配），让 LLM 看到便签/批注等具体需求
                page_text_hint = self._match_page_text_for_module(mod_name, mod, page_names)
                # 方案3：P2 自动分批。若本模块 fused 条目过多，拆成多批分别生成测试点再合并，
                # 避免单次 max_tokens=6000 装不下导致 LLM 聚焦主流程、静默丢弃删除/添加等子功能。
                fused_rel = self._fused_entries_for_module(mod_name, page_names)
                batch_size = 15  # 每批最多注入的 fused 条目数
                if len(fused_rel) > batch_size:
                    # 分批：每批取一个连续切片，单独生成该子集的测试点
                    batches = [fused_rel[i:i + batch_size] for i in range(0, len(fused_rel), batch_size)]
                    draft_tps = []
                    for bi, batch in enumerate(batches, 1):
                        batch_hint = (
                            f'\n\n【该模块已提取的结构化需求（第 {bi}/{len(batches)} 批，共 {len(batch)} 条）】'
                            f'请严格基于以下具体需求生成该批测试点，不要泛化或臆造，也不要把本批内容漏掉：\n'
                            + '\n'.join(
                                f'{j}. 【{r.get("feature", "")}】{r.get("interaction", "")}'
                                f'（证据等级：{r.get("evidence", "")}）'
                                for j, r in enumerate(batch, 1)
                            )
                        )
                        batch_prompt = render_prompt(P2_TESTPOINT_GEN, module_info_yaml=mod_ctx) + feature_count_hint + batch_hint + self._kb_suffix(query_hint=mod_name)
                        tp_out = self._llm(self.analyzer_config, batch_prompt, max_tokens=6000, temperature=0)
                        tp_yaml = _parse_yaml(_extract_block(tp_out, 'yaml')) or {}
                        batch_tps = tp_yaml.get('test_points', []) if isinstance(tp_yaml, dict) else []
                        batch_tps = [t for t in batch_tps if isinstance(t, dict)]
                        draft_tps.extend(batch_tps)
                        self._log(f'  模块[{mod_name}] 第{bi}批（{len(batch)}条fused）生成 {len(batch_tps)} 条测试点')
                    self._log(f'  模块[{mod_name}] 分批生成汇总：{len(draft_tps)} 条测试点（{len(fused_rel)}条fused / 每批{batch_size}条）')
                else:
                    tp_prompt = render_prompt(P2_TESTPOINT_GEN, module_info_yaml=mod_ctx) + feature_count_hint + page_text_hint + self._kb_suffix(query_hint=mod_name)
                    tp_out = self._llm(self.analyzer_config, tp_prompt, max_tokens=6000, temperature=0)
                    tp_yaml = _parse_yaml(_extract_block(tp_out, 'yaml')) or {}
                    draft_tps = tp_yaml.get('test_points', []) if isinstance(tp_yaml, dict) else []
                    self._log(f'  模块[{mod_name}] 基础测试点生成：{len(draft_tps)} 条')
                if isinstance(mod_features, list) and len(mod_features) > 0 and len(draft_tps) < max(len(mod_features) * 2, 5):
                    self._log(f'  ⚠️ 模块[{mod_name}] 测试点({len(draft_tps)})偏少（功能点{len(mod_features)}个），可能覆盖不足或产生泛化输出')
                # 过滤非 dict 的测试点（LLM 偶发返回字符串元素）
                draft_tps = [t for t in draft_tps if isinstance(t, dict)]
                # 空结果重试：LLM 偶发返回空/无效格式导致该模块无基础测试点，重试一次（精简 prompt，强调必须输出具体 YAML）
                if not draft_tps:
                    self._log(f'  ⚠️ 模块[{mod_name}] 基础测试点为空，重试一次...')
                    retry_prompt = (
                        f'为模块「{mod_name}」生成测试点（test_points），必须输出有效的 YAML 列表，禁止返回空。\n'
                        f'模块信息：\n{mod_ctx}\n'
                        f'要求：每个测试点必须是 dict（含 title/type/priority/description/category），数量 5-20 条，'
                        f'基于模块的具体功能，不要泛化。\n'
                        f'输出格式（直接输出 YAML）：\nmodule: {mod_name}\ntest_points:\n'
                        f'  - id: TP_{mod_name}_001\n    title: ...\n    type: <功能/边界/异常/UI/权限/性能/兼容>\n'
                        f'    category: <main_flow/branch_flow/boundary/exception/permission/UI/performance>\n'
                        f'    priority: P0\n    description: ...\n'
                        f'category 必须按场景选：正常路径=main_flow，分支/可选路径=branch_flow，'
                        f'空/超长/上下限=boundary，断网/超时/失败=exception，'
                        f'登录态/权益=permission，文案/布局/动画=UI。不要全部写 branch_flow。'
                    ) + page_text_hint
                    try:
                        tp_out2 = self._llm(self.analyzer_config, retry_prompt, max_tokens=6000, temperature=0)
                        tp_yaml2 = _parse_yaml(_extract_block(tp_out2, 'yaml')) or {}
                        draft_tps = tp_yaml2.get('test_points', []) if isinstance(tp_yaml2, dict) else []
                        draft_tps = [t for t in draft_tps if isinstance(t, dict)]
                        self._log(f'  模块[{mod_name}] 重试后基础测试点生成：{len(draft_tps)} 条')
                    except Exception as _re:
                        self._log(f'  模块[{mod_name}] 重试失败: {_re}')
                for t in draft_tps:
                    t['source'] = ['draft']
                    # 后处理：LLM 经常漏 category 字段，按 type/priority 推断补全
                    cat = (t.get('category') or '').strip()
                    if not cat:
                        t['category'] = _infer_category(t)

                # 3.3.5 风险识别 (risk)
                risk_prompt = render_prompt(P5_RISK_IDENTIFY,
                                            test_points_json=json.dumps(draft_tps, ensure_ascii=False))
                risk_out = self._llm(self.analyzer_config, risk_prompt, max_tokens=6000, temperature=0)
                risk_json = _parse_json(_extract_block(risk_out, 'json')) or {}
                all_risks.append({'module': mod_name, 'data': risk_json})
                risk_ext = []
                for rp in risk_json.get('risk_points', []):
                    if not isinstance(rp, dict):
                        continue
                    for etp in rp.get('extended_test_points', []):
                        if not isinstance(etp, dict):
                            continue
                        ecat = (etp.get('category') or '').strip() or 'exception'
                        risk_ext.append({
                            'id': etp.get('id', f"{mod_name}-EXT"),
                            'title': etp.get('title', ''),
                            'description': etp.get('description', ''),
                            'priority': etp.get('priority_hint', 'P1'),
                            'category': ecat,
                            # 透传功能级判据：此前这里没取 pass_criteria，导致风险扩展点
                            # 全部以空判据进入 P7；P7 解析失败降级时更是原样落库，
                            # 产生大量「假覆盖」测试点（历史实测 29% 为空）。
                            'pass_criteria': etp.get('pass_criteria') or '',
                            'source': ['risk'],
                        })

                # 3.3.6 PCI 识别 (pci)
                cross_module_impact = self._extract_module_context(mod_name, summary_text)
                converged = self._converged_conclusions()
                clarified_qa = []
                for _ci in self.prototype.clarification_items():
                    _cq = (_ci.get('issue') or _ci.get('suggested_question') or '').strip()
                    _ca = str(_ci.get('resolution') or _ci.get('answer') or '').strip()
                    if _cq and _ca:
                        clarified_qa.append(f'Q: {_cq}\nA: {_ca}')
                stage2_text = clarification_text or ''
                if clarified_qa:
                    stage2_text += (
                        '\n\n### 产品已回答的确认结论（以下答案视为已确认需求，'
                        '禁止就同一问题再生成 PCI，不得出现在 pci_list 中）：\n'
                        + '\n'.join(clarified_qa)
                    )
                pci_prompt = render_prompt(P6_PCI_IDENTIFY,
                                           test_points_json=json.dumps(draft_tps, ensure_ascii=False),
                                           requirement_text=self._source_text(page_names),
                                           stage2_issues=stage2_text,
                                           cross_module_impact=cross_module_impact or '无（该模块未分析出跨模块影响）',
                                           converged_conclusions=(
                                               json.dumps(converged, ensure_ascii=False) if converged else '无'
                                           ))
                pci_out = self._llm(self.analyzer_config, pci_prompt, max_tokens=6000, temperature=0)
                pci_json = _parse_json(_extract_block(pci_out, 'json')) or {}
                if not pci_json:
                    pci_json = _parse_yaml(_extract_block(pci_out, 'yaml')) or {}
                if not pci_json:
                    logger.warning(f'模块[{mod_name}] P6 JSON 解析失败，自动重试一次...')
                    self._log(f'  模块[{mod_name}] P6 输出解析失败，自动重试...')
                    repair_prompt = (
                        pci_prompt
                        + '\n\n【格式校验反馈】你上一次输出的 JSON 无法解析（可能混入解释文字、被截断或缺少 pci_list）。'
                        '请重新输出完整 JSON，严格按模板结构，只输出 JSON 本身，不要任何前言或结尾说明。'
                    )
                    pci_out = self._llm(self.analyzer_config, repair_prompt, max_tokens=6000, temperature=0)
                    pci_json = _parse_json(_extract_block(pci_out, 'json')) or {}
                    if not pci_json:
                        pci_json = _parse_yaml(_extract_block(pci_out, 'yaml')) or {}
                # 后处理去重：剔除与已澄清问题（resolution 非空）重复的 PCI 条目
                pci_raw_list = pci_json.get('pci_list') or pci_json.get('data', {}).get('pci_list') or []
                if pci_raw_list:
                    filtered_list, removed_count, removed_items = self._pci_dedup_against_clarifications(
                        pci_raw_list, self.prototype.clarification_log)
                    if removed_count > 0:
                        # 写回 PCI JSON（兼容两种包裹结构）
                        if 'pci_list' in pci_json:
                            pci_json['pci_list'] = filtered_list
                        elif 'data' in pci_json and isinstance(pci_json['data'], dict):
                            pci_json['data']['pci_list'] = filtered_list
                        pci_json['_dedup_removed'] = removed_count
                        pci_json['_dedup_removed_items'] = removed_items
                        self._log(f'PCI 去重：从已澄清问题中剔除了 {removed_count} 条重复 PCI（{mod_name}）')
                all_pci.append({'module': mod_name, 'data': pci_json})

                # 3.3.7 三路合并去重（向量去重 + p7 最终化）
                combined = []
                for t in (draft_tps + risk_ext):
                    combined.append({
                        'id': t.get('id', f"{mod_name}-{len(combined)}"),
                        'title': t.get('title', ''),
                        'description': t.get('description', ''),
                        'priority': t.get('priority', 'P1'),
                        'source': t.get('source', ['draft']),
                        # 透传 p2 生成的功能级判据：此前该字段在此处被静默丢弃，
                        # 导致 p7/p3 只能自行编造断言，最终回退为「页面展示正常」类兜底句。
                        'pass_criteria': t.get('pass_criteria') or '',
                    })
                self._log(f'  模块[{mod_name}] 合并前：{len(draft_tps)} draft + {len(risk_ext)} risk = {len(combined)}')
                deduped, dedup_count, audit = self._vector_dedup(combined)
                for record in audit:
                    record.update({'stage': 'pre_merge', 'module': mod_name})
                dedup_audit.extend(audit)
                merge_prompt = render_prompt(
                    P7_MERGE_DEDUP,
                    draft_testpoints=json.dumps(deduped, ensure_ascii=False),
                    risk_extended_testpoints='[]',
                    pci_blocked_scenarios=json.dumps(
                        [p for p in pci_json.get('pci_list', [])], ensure_ascii=False),
                )
                merge_out = self._llm(self.analyzer_config, merge_prompt, max_tokens=8000, temperature=0)
                merge_json = _parse_json(_extract_block(merge_out, 'json')) or {}
                # 兼容部分模型返回 YAML 而非 JSON 的情况
                if not merge_json:
                    merge_json = _parse_yaml(_extract_block(merge_out, 'yaml')) or {}
                final_tps = merge_json.get('test_points', []) if isinstance(merge_json, dict) else []
                # 安全网：若 P7 未按约定返回测试点，但去重后输入非空，则降级使用输入。
                # ⚠️ 降级路径会跳过 P7 的合并/去重/补齐，必须把原因写进日志与审计，
                # 否则「P7 输出解析失败」会静默变成「用例质量下降」，排查时无从下手
                # （实测：模型输出了未闭合的引号，整份 JSON 被丢弃，降级后 7 条空判据原样落库）。
                if not final_tps and deduped:
                    parse_failed = not merge_json
                    logger.warning(
                        f'模块[{mod_name}] P7 返回空测试点'
                        f'（{"输出解析失败" if parse_failed else "模型未给出 test_points"}），'
                        f'降级使用去重后输入({len(deduped)}条)')
                    self._log(
                        f'  模块[{mod_name}] P7 {"输出解析失败" if parse_failed else "未返回测试点"}，'
                        f'降级使用去重后输入 {len(deduped)} 条（本次跳过合并/补齐）')
                    dedup_audit.append({
                        'stage': 'p7_fallback', 'module': mod_name,
                        'reason': 'parse_failed' if parse_failed else 'empty_testpoints',
                        'input_count': len(deduped),
                        'raw_len': len(merge_out or ''),
                        'raw_head': (merge_out or '')[:200],
                    })
                    final_tps = deduped
                for t in final_tps:
                    t['module'] = mod_name
                    # P7 合并后也补全 category（PCI 扩展点经常漏填）
                    if not (t.get('category') or '').strip():
                        t['category'] = _infer_category(t)
                all_final_tps.extend(final_tps)
                logger.info(f'模块[{mod_name}] 三路合并：输入{len(combined)} 去重{dedup_count} 最终{len(final_tps)}')
                self._log(f'  模块[{mod_name}] 合并完成：{len(final_tps)} 测试点')
            except Exception as e:
                logger.error(f'模块[{mod_name}] 测试点合并失败: {e}')
                self._log(f'  模块[{mod_name}] 失败: {e}')
                continue

        # P7 合并后全局二次去重：消除各模块合并可能 reintroduce 的近重复
        # 按 module 分组去重，仅消同模块内的近重复，避免跨模块同功能被误删
        global_dedup = 0
        if self.embedder_config and all_final_tps:
            grouped = {}
            for t in all_final_tps:
                grouped.setdefault(t.get('module', '') or '', []).append(t)
            merged = []
            for mod_name, mod_items in grouped.items():
                deduped, cnt, audit = self._vector_dedup(mod_items)
                for record in audit:
                    record.update({'stage': 'post_merge', 'module': mod_name})
                dedup_audit.extend(audit)
                global_dedup += cnt
                merged.extend(deduped)
            all_final_tps = merged
            if global_dedup:
                self._log(f'  全局二次去重：消除 {global_dedup} 条合并重新引入的近重复')

        # 跨模块 PCI 聚类去重：同一问题在不同模块重复出现（含语义相同但措辞不同）时，标记为重复
        import difflib
        import re as _re

        def _pci_keywords(text):
            """提取 PCI 描述中的核心业务词（去停用词、去常见动词/连接词），用于语义聚类。"""
            if not text:
                return set()
            stops = {'是否', '什么', '如何', '怎么', '需要', '是否需', '确认', '该', '问', '配置',
                     '定义', '明确', '规则', '要求', '存在', '以及', '还是', '及', '与', '并', '或', '问题',
                     '场景', '情况', '展示', '界面', '页面', '中', '的', '和', '个'}
            # 提取 2-6 字中文词（名词/业务概念）
            words = set(_re.findall(r'[\u4e00-\u9fa5]{2,6}', text))
            # 过滤掉停用词，保留有业务含义的词
            return {w for w in words if w not in stops and len(w) >= 2}

        def _pci_similar(a, b, ta, tb, cos=None):
            """判定两条 PCI 的相似等级：
            - 'merge': 高置信重复（字面 >=0.7，或字面 0.55~0.7 且关键词高度重叠，或向量余弦 >=0.85）→ 自动合并
            - 'mark': 中等相似（字面 0.45~0.55，或关键词中等重叠，或向量余弦 >=0.75）→ 标记供人工核对
            - 'none': 不相似
            """
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            ka, kb = _pci_keywords(ta), _pci_keywords(tb)
            shared = ka & kb
            shared_ratio = len(shared) / max(len(ka), 1) if ka else 0
            kw_overlap = len(shared) >= 2 and shared_ratio >= 0.4
            # 高置信重复 → 自动合并
            if ratio >= 0.7 or (ratio >= 0.55 and kw_overlap) or (cos is not None and cos >= 0.85):
                return 'merge', ratio, shared_ratio
            # 中等相似 → 标记保留供人工核对
            if ratio >= 0.45 or (kw_overlap and ratio >= 0.35) or (cos is not None and cos >= 0.75):
                return 'mark', ratio, shared_ratio
            return 'none', ratio, shared_ratio

        # 预计算全部 PCI 描述的向量（余弦相似度捕捉跨模块改写重复）
        _pci_vec_list = []
        _pci_vec_index = {}
        for _mp in all_pci:
            for _pc in (_mp.get('data', {}).get('pci_list') or _mp.get('data', {}).get('data', {}).get('pci_list') or []):
                if isinstance(_pc, dict) and ((_pc.get('description') or '').strip() or (_pc.get('title') or '').strip()):
                    _pci_vec_index[id(_pc)] = len(_pci_vec_list)
                    _pci_vec_list.append((_pc.get('description') or '').strip() or (_pc.get('title') or '').strip())
        _pci_vecs = self._embed(_pci_vec_list) if _pci_vec_list else None
        if _pci_vecs is not None:
            try:
                _varr = np.array(_pci_vecs, dtype=float)
                _varr = _varr / np.clip(np.linalg.norm(_varr, axis=1, keepdims=True), 1e-9, None)
                _pci_vecs = _varr
            except Exception:
                _pci_vecs = None
        _seen_vecs = []  # 代表向量，与 seen_pci_texts 对齐
        seen_pci_texts = []   # 保留组的代表文本
        seen_pci_keywords = []  # 保留组的关键词集合
        merged_count = 0
        marked_count = 0
        merged_items = []
        marked_items = []
        for mod_pci in all_pci:
            pci_list = (mod_pci.get('data', {}).get('pci_list') or mod_pci.get('data', {}).get('data', {}).get('pci_list') or [])
            keep = []
            for pci in pci_list:
                if not isinstance(pci, dict):
                    keep.append(pci)
                    continue
                desc = (pci.get('description') or '').strip()
                text = desc or (pci.get('title') or '')
                if not text:
                    keep.append(pci)
                    continue
                # 判断与之前 PCI（代表）的相似等级
                level = 'none'
                dup_ref = None
                dup_sim = 0
                dup_kw = 0
                for i, prev_text in enumerate(seen_pci_texts):
                    cos = None
                    if _pci_vecs is not None and id(pci) in _pci_vec_index and i < len(_seen_vecs) and _seen_vecs[i] is not None:
                        cos = float(np.dot(_pci_vecs[_pci_vec_index[id(pci)]], _seen_vecs[i]))
                    lv, ratio, kratio = _pci_similar(desc, prev_text, text, prev_text, cos)
                    if lv != 'none':
                        level = lv
                        dup_ref = i
                        dup_sim = round(ratio, 3)
                        dup_kw = round(kratio, 3)
                        break
                if level == 'merge':
                    # 高置信重复 → 真正合并，只保留代表条（当前条剔除）
                    merged_count += 1
                    merged_items.append({'module': mod_pci.get('module'), 'sim': dup_sim, 'kw': dup_kw, 'desc': desc[:100]})
                    # 被合并条不加入 keep，也不作为新代表
                    continue
                elif level == 'mark':
                    # 中等相似 → 标记保留供人工核对
                    pci['_pci_dup'] = True
                    pci['_pci_dup_ref'] = dup_ref
                    pci['_pci_dup_similarity'] = dup_sim
                    marked_count += 1
                    marked_items.append({'module': mod_pci.get('module'), 'sim': dup_sim, 'desc': desc[:100]})
                else:
                    # 不相似 → 作为新代表
                    seen_pci_texts.append(desc)
                    seen_pci_keywords.append(_pci_keywords(text))
                    _seen_vecs.append(_pci_vecs[_pci_vec_index[id(pci)]] if (_pci_vecs is not None and id(pci) in _pci_vec_index) else None)
                keep.append(pci)  # 保留非重复 + 标记条（供人工核对）
            # 写回
            if 'pci_list' in (mod_pci.get('data') or {}):
                mod_pci['data']['pci_list'] = keep
            elif 'data' in mod_pci and isinstance(mod_pci['data'], dict) and 'pci_list' in mod_pci['data'].get('data', {}):
                mod_pci['data']['data']['pci_list'] = keep
        if merged_count:
            self._log(f'  跨模块 PCI 智能合并：合并 {merged_count} 条高置信重复 PCI（仅保留代表，字段已剔除）')
            if merged_items:
                import json as _json
                self._log('  已合并 PCI：' + _json.dumps(merged_items[:10], ensure_ascii=False))
        if marked_count:
            self._log(f'  跨模块 PCI 待核对：标记 {marked_count} 条中等相似 PCI（保留供人工核对，字段 _pci_dup）')
            if marked_items:
                import json as _json
                self._log('  待核对 PCI：' + _json.dumps(marked_items[:10], ensure_ascii=False))

        # 第一段产物落库（测试点合并完成，等待人工确认）
        pci_stats = _normalize_pci_dispositions(all_pci)
        self._log(
            f'PCI 机器收敛：共 {pci_stats["total"]} 条，'
            f'按默认假设收敛 {pci_stats["assumed"]} 条，'
            f'疑似改动产品规则 {pci_stats["suspicious"]} 条，'
            f'跨模块重复合并 {pci_stats["dedup"]} 条，'
            f'需人工确认 {pci_stats["confirm"]} 条'
        )
        # 第一段产物落库（测试点合并完成，等待人工确认）
        if page is not None:
            # 单画布模式：把该画布的测试点写入 page.page_testpoints_json
            # 回填 feature_ref / api_ref（P7 合并会丢这两列，只能事后用代码建立关联）
            try:
                _sum_for_ref = _parse_yaml(_extract_block(page.page_summary or '', 'yaml')) or {}
                _n = backfill_testpoint_refs(all_final_tps, _sum_for_ref, page.page_name)
                self._log(f'  追溯回填：{_n}/{len(all_final_tps)} 条测试点已关联功能点/接口')
            except Exception as _e:
                logger.warning(f'feature_ref 回填失败（不影响主流程）: {_e}')
            # pass_criteria 兜底补齐（先补判据，再收敛优先级）
            try:
                _pc = ensure_pass_criteria(all_final_tps)
                if _pc['filled_from_desc'] or _pc['still_missing']:
                    self._log('  判据兜底：从描述补齐 %d 条，仍缺失 %d 条'
                              % (_pc['filled_from_desc'], len(_pc['still_missing'])))
                    dedup_audit.append({'stage': 'pass_criteria', 'page': page.page_name,
                                        'filled_from_desc': _pc['filled_from_desc'],
                                        'still_missing': _pc['still_missing']})
            except Exception as _e:
                logger.warning(f'pass_criteria 兜底失败（不影响主流程）: {_e}')
            # P0 占比收敛（画布级：按本画布全部测试点计算配额）
            try:
                _pa = enforce_p0_ratio(all_final_tps)
                if _pa['demoted']:
                    self._log(
                        '  P0 配额收敛：%d → %d 条（上限 %.0f%%），降级 %d 条'
                        % (_pa['before'], _pa['after'], _pa['limit'] * 100, len(_pa['demoted'])))
                    dedup_audit.append({'stage': 'p0_ratio', 'page': page.page_name, **_pa})
            except Exception as _e:
                logger.warning(f'P0 配额收敛失败（不影响主流程）: {_e}')
            page.page_testpoints_json = json.dumps(all_final_tps, ensure_ascii=False)
            page.mark_stage(3, status='done', log=f'测试点合并完成（{len(all_final_tps)} 条）')
            page.save(update_fields=['page_testpoints_json', 'current_stage', 'stage_status', 'work_log'])
            release_merge_collection(self.session_id)
            return {
                'modules': len(modules),
                'final_testpoints': len(all_final_tps),
                'page_name': page.page_name,
                'phase': 1,
            }
        # 全局模式同样回填追溯字段（用合并后的 requirement_summary）
        # ⚠️ 2026-11-04：必须走 _parse_yaml_docs 逐段合并，否则多画布视图只命中 1 个画布，
        # feature_ref 回填率会被严重低估（实测 proto=92 会只拿到「个人-个人中心」的功能点）。
        try:
            _sum_for_ref, _ = _parse_yaml_docs(self.prototype.requirement_summary or '')
            _sum_for_ref = _sum_for_ref or {}
            _n = backfill_testpoint_refs(all_final_tps, _sum_for_ref)
            self._log(f'  追溯回填：{_n}/{len(all_final_tps)} 条测试点已关联功能点/接口')
        except Exception as _e:
            logger.warning(f'feature_ref 回填失败（不影响主流程）: {_e}')
        # pass_criteria 兜底补齐（任务级）
        try:
            _pc = ensure_pass_criteria(all_final_tps)
            if _pc['filled_from_desc'] or _pc['still_missing']:
                self._log('  判据兜底：从描述补齐 %d 条，仍缺失 %d 条'
                          % (_pc['filled_from_desc'], len(_pc['still_missing'])))
                dedup_audit.append({'stage': 'pass_criteria', 'scope': 'prototype',
                                    'filled_from_desc': _pc['filled_from_desc'],
                                    'still_missing': _pc['still_missing']})
        except Exception as _e:
            logger.warning(f'pass_criteria 兜底失败（不影响主流程）: {_e}')
        # P0 占比收敛（任务级：按全部模块合计计算配额）
        try:
            _pa = enforce_p0_ratio(all_final_tps)
            if _pa['demoted']:
                self._log(
                    '  P0 配额收敛：%d → %d 条（上限 %.0f%%），降级 %d 条'
                    % (_pa['before'], _pa['after'], _pa['limit'] * 100, len(_pa['demoted'])))
                dedup_audit.append({'stage': 'p0_ratio', 'scope': 'prototype', **_pa})
        except Exception as _e:
            logger.warning(f'P0 配额收敛失败（不影响主流程）: {_e}')
        self.prototype.risks_json = json.dumps(all_risks, ensure_ascii=False)
        self.prototype.pci_json = json.dumps(all_pci, ensure_ascii=False)
        self.prototype.final_testpoints_json = json.dumps(all_final_tps, ensure_ascii=False)
        self.prototype.dedup_audit_json = json.dumps(dedup_audit, ensure_ascii=False)
        self.prototype.status = 'testpoints_ready'
        self.prototype.current_stage = 3
        self.prototype.save()
        self._advance_selected_pages(3)
        self._log(f'第一段完成！共 {len(modules)} 模块 / {len(all_final_tps)} 测试点。请确认/编辑测试点后再生成测试用例。')
        release_merge_collection(self.session_id)
        return {
            'modules': len(modules),
            'final_testpoints': len(all_final_tps),
            'phase': 1,
        }

    # ---------- 阶段3 第二段：基于（已确认的）测试点生成用例→冒烟→质量→Excel ----------
    @_timed_stage('阶段3-2 用例生成')
    def _code_quality_check(self, all_cases, smoke_json, all_final_tps):
        #type: (...) -> dict   
        import re as _re
        # C1 兼容性归一化：把「{module:X, testcase:{...}}」嵌套结构中外层 module 同步到内层 testcase，
        # 避免 LLM 把 module 放外层而 C1 只查内层导致误判"缺 module"。
        for tc in all_cases:
            if not isinstance(tc, dict):
                continue
            inner = tc.get('testcase', tc) if isinstance(tc, dict) else tc
            if isinstance(inner, dict) and not inner.get('module') and tc.get('module'):
                inner['module'] = tc.get('module')
        total_cases = len(all_cases)
        smoke_cases = smoke_json.get('smoke_testcases',[]) if isinstance(smoke_json,dict) else (smoke_json if isinstance(smoke_json,list) else [])
        total_smoke = len(smoke_cases)
        # C1 (兼容 {module, testcase:{id,title,...}} 嵌套结构)
        REQ_F = ('id','module','title','precondition','steps','expected','priority','type')
        c1_failed = []
        for tc in all_cases:
            if not isinstance(tc,dict): continue
            inner = tc.get('testcase', tc) if isinstance(tc, dict) else tc
            if not isinstance(inner, dict): continue
            missing = [k for k in REQ_F if not inner.get(k)]
            if missing: c1_failed.append({'case_id': inner.get('id','?'), 'missing_fields': missing})
        c1_r = 'FAIL' if c1_failed else 'PASS'
        # C2
        c2_ratio = total_smoke / total_cases if total_cases > 0 else 0
        if total_smoke==0 or c2_ratio<0.05: c2_r,c2_n = 'WARNING',f'{c2_ratio:.1%}'
        elif c2_ratio>0.25: c2_r,c2_n = 'WARNING',f'{c2_ratio:.1%}'
        else: c2_r,c2_n = 'PASS',f'{c2_ratio:.1%}'
        # C3
        c3_mods = {}
        for tp in all_final_tps:
            if not isinstance(tp,dict): continue
            if str(tp.get('priority','')).upper()!='P0': continue
            m = tp.get('module') or tp.get('feature_module') or '?'
            c = tp.get('category') or ''
            c3_mods.setdefault(m,{'t':0,'m':0})
            c3_mods[m]['t']+=1
            if c=='main_flow': c3_mods[m]['m']+=1
        c3_ok = all(v['m']>=1 for v in c3_mods.values()) if c3_mods else True
        c3_r = 'PASS' if c3_ok else 'FAIL'
        c3_miss = [m for m,v in c3_mods.items() if v['m']==0]
        # C4
        VAGUE = ('测试一下','检查功能','验证功能','随便','正常','一般')
        c4_t,c4_b = 0,0
        for tc in all_cases:
            if not isinstance(tc,dict): continue
            steps = tc.get('steps',[])
            if isinstance(steps,str): steps = [s.strip() for s in _re.split(r'[\n\r]+',steps) if s.strip()]
            if not isinstance(steps,list) or len(steps)<3: c4_b+=1
            elif any(w in '|'.join(str(s) for s in steps) for w in VAGUE): c4_b+=1
            c4_t+=1
        c4_r = 'WARNING' if c4_t>0 and c4_b/c4_t>0.3 else 'PASS'
        # C5
        p0c = sum(1 for t in all_final_tps if isinstance(t,dict) and str(t.get('priority','')).upper()=='P0')
        p2c = sum(1 for t in all_final_tps if isinstance(t,dict) and str(t.get('priority','')).upper()=='P2')
        ttp = len(all_final_tps) or 1
        c5_p0,c5_p2 = p0c/ttp, p2c/ttp
        c5_i = []
        if c5_p0>0.15: c5_i.append(f'P0%={c5_p0:.1%}>15%')
        if c5_p2<0.20: c5_i.append(f'P2%={c5_p2:.1%}<20%')
        c5_r = 'WARNING' if c5_i else 'PASS'
        # C6
        c6_b = sum(1 for t in all_final_tps if isinstance(t,dict) and len(t.get('description','') or '')<80)
        c6_r = 'WARNING' if c6_b/ttp>0.3 else 'PASS'
        # blocking
        bi = []
        if c1_r=='FAIL': bi.append({'check':'C1','message':f'{len(c1_failed)}用例缺必填字段'})
        if c3_r=='FAIL': bi.append({'check':'C3','message':f'缺main_flow: {",".join(c3_miss[:5])}'})
        wn = []
        if c2_r=='WARNING': wn.append(f'C2: {c2_n}')
        if c4_r=='WARNING': wn.append(f'C4: {c4_b}/{c4_t}')
        if c5_r=='WARNING': wn.append(f'C5: {";".join(c5_i)}')
        if c6_r=='WARNING': wn.append(f'C6: {c6_b}/{ttp}')
        qr = {
            'quality_report': {
                'overall_result': 'PASS' if not bi else 'FAIL',
                'checks': {
                    'C1': {'result': c1_r, 'issues': c1_failed},
                    'C2': {'result': c2_r, 'smoke_count': total_smoke, 'total_count': total_cases, 'ratio': round(c2_ratio,4)},
                    'C3': {'result': c3_r, 'p0_modules': list(c3_mods.keys()), 'missing_main_flow': c3_miss},
                    'C4': {'result': c4_r, 'sampled': c4_t, 'bad': c4_b},
                    'C5': {'result': c5_r, 'p0_ratio': round(c5_p0,4), 'p2_ratio': round(c5_p2,4)},
                    'C6': {'result': c6_r, 'sampled': ttp, 'bad': c6_b},
                },
                'blocking_issues': bi,
                'warnings': wn,
            }
        }
        return {
            'quality_json': qr,
            'blocking': bi,
            'warnings': wn,
            'smoke_count': total_smoke,
            'c1_failed': len(c1_failed),
        }
    def _repair_missing_steps(self, case: dict, mod_name: str) -> dict:
        """缺步骤用例自愈：把该条用例回喂 LLM 重生成一次操作步骤；仍失败则标记缺失。"""
        title = case.get('title') or case.get('scenario') or case.get('name') or '该用例'
        repair_prompt = (
            '以下是生成的一条测试用例，但缺少「操作步骤」。'
            '请根据用例标题、前置条件和预期结果，补充 2~5 条具体的、可执行的测试操作步骤。\n\n'
            f'模块：{mod_name}\n'
            f'标题：{title}\n'
            f'前置条件：{case.get("precondition") or ""}\n'
            f'预期结果：{case.get("expected") or ""}\n\n'
            '只输出 JSON，格式：{"steps": ["步骤1", "步骤2", ...]}\n'
            '不要输出任何解释文字。'
        )
        try:
            out = self._llm(self.writer_config or self.analyzer_config, repair_prompt,
                            max_tokens=1500, temperature=0, timeout=120)
            data = _parse_json(_extract_block(out, 'json')) or {}
            steps_new = data.get('steps') or []
            if isinstance(steps_new, list) and any(str(s).strip() for s in steps_new):
                case['steps'] = [str(s).strip() for s in steps_new if str(s).strip()]
                case['steps_repaired'] = True
                self._log(f'  用例「{title}」步骤缺失，已自动补全 {len(case["steps"])} 条')
                return case
        except Exception as e:
            logger.warning(f'用例步骤自愈失败（{title}）: {e}')
        case['steps'] = '【步骤缺失，请补充操作步骤】'
        case['steps_missing'] = True
        case['notes'] = (
            ((case.get('notes') or '') + '；' if case.get('notes') else '')
            + '步骤缺失，需人工补充'
        )
        self._log(f'  ⚠️ 用例「{title}」步骤自愈失败，标记待人工补充')
        return case

    def run_design_phase2(self, page=None):
        """读取（可能被人工编辑过的）final_testpoints_json，生成测试用例、冒烟、质量报告、Excel。

        A2-c 画布级：传入 page 时，读 page.page_testpoints_json、写 page.page_testcases_json /
        page.page_smoke_json，不落全局字段；不传时维持原「选中子集联合」语义。
        """
        self.prototype.status = 'designing'
        self.prototype.save(update_fields=['status'])
        self._log('开始生成测试用例（阶段3 · 第二段）...')

        # 读取已确认的最终测试点
        if page is not None:
            tp_src = page.page_testpoints_json or '[]'
        else:
            tp_src = self.prototype.final_testpoints_json or '[]'
        try:
            all_final_tps = json.loads(tp_src or '[]')
            if not isinstance(all_final_tps, list):
                all_final_tps = []
        except Exception:
            all_final_tps = []
        if not all_final_tps:
            self.prototype.status = 'failed'
            self.prototype.error_message = '没有可用的测试点，请先运行第一段生成测试点'
            self.prototype.save(update_fields=['status', 'error_message'])
            self._log('第二段中止：测试点为空')
            return {'testcases': 0, 'error': 'no_testpoints'}

        # 重新解析模块拆分，供用例生成提供 module_context
        if page is not None:
            # 单画布：模块上下文直接来自该画布 page_summary 的模块清单
            _sum = _parse_yaml(_extract_block(page.page_summary or '', 'yaml')) or {}
            modules = _sum.get('modules', []) if isinstance(_sum, dict) else []
        else:
            split_yaml = _parse_yaml(_extract_block(self.prototype.module_split, 'yaml')) or {}
            modules = split_yaml.get('modules', []) if isinstance(split_yaml, dict) else []
        mod_map = {m.get('name', ''): m for m in modules if isinstance(m, dict)}

        # 按模块分组测试点（保持原始顺序）
        tps_by_mod = {}
        for tp in all_final_tps:
            mod_name = (tp.get('module') if isinstance(tp, dict) else None) or '未分组'
            tps_by_mod.setdefault(mod_name, []).append(tp)

        # 3.4 用例生成（按模块批量，模块内再按 CASE_BATCH_SIZE 拆批，避免 max_tokens 截断残缺）
        CASE_BATCH_SIZE = 12  # 单批测试点上限（线上实测单模块 p95=30，12 为安全余量与调用次数的甜点）
        module_testcases = []
        for mod_name, final_tps in tps_by_mod.items():
            if not final_tps:
                continue
            mod = mod_map.get(mod_name, {'name': mod_name})
            mod_ctx = json.dumps(mod, ensure_ascii=False)
            # 注入人工已确认的 PCI 答案 + 风险处置结论，让 AI 基于确认结论精准出例
            confirmed_ctx = self._build_confirmed_context()
            # 模块内拆批
            batches = [final_tps[i:i + CASE_BATCH_SIZE] for i in range(0, len(final_tps), CASE_BATCH_SIZE)]
            mod_cases = []
            try:
                for bi, batch_tps in enumerate(batches):
                    self._log(f'  模块[{mod_name}] 正在生成测试用例（第 {bi + 1}/{len(batches)} 批，本批 {len(batch_tps)} 测试点，共 {len(final_tps)}）...')
                    case_prompt = (render_prompt(P3_CASE_GEN,
                                                 test_point_yaml=json.dumps(batch_tps, ensure_ascii=False),
                                                 module_context=mod_ctx)
                                  + "\n\n### 附加指令\n请为上面列出的【每一条】最终测试点生成一条结构化测试用例，"
                                    "以 YAML 列表返回，列表项为 `testcase:` 字典（字段同 p3 输出规范）。"
                                    "每条用例必须额外携带 `testpoint` 字段，值为该用例来源测试点的 title（原样照抄，便于追溯）。"
                                  + self._kb_suffix(query_hint=mod_name))
                    if confirmed_ctx:
                        case_prompt += (
                            "\n\n### 已确认的待确认问题答案与风险处置结论（人工已确认，请务必作为用例设计依据）\n"
                            + confirmed_ctx)
                    meta = self._llm(self.writer_config, case_prompt, max_tokens=16000, temperature=0, return_meta=True)
                    case_out = meta.get('content', '') if isinstance(meta, dict) else meta
                    finish_reason = meta.get('finish_reason') if isinstance(meta, dict) else None
                    if finish_reason == 'length':
                        # max_tokens 用满被截断：本批用例可能残缺，记录告警但不静默丢弃已生成部分
                        logger.warning(f'模块[{mod_name}] 第{bi + 1}批用例生成被 max_tokens 截断(finish_reason=length)，本批可能残缺')
                        self._log(f'  ⚠ 模块[{mod_name}] 第{bi + 1}批输出被 max_tokens 截断，本批用例可能不完整（请检查该模块用例覆盖）')
                    case_yaml = _parse_yaml(_extract_block(case_out, 'yaml')) or {}
                    # 兼容三种 LLM 输出形态：
                    #   1) {testcases: [...]}  推荐，列表
                    #   2) {testcase: {...}}   旧 prompt，单条
                    #   3) [...]               裸数组
                    cases = []
                    if isinstance(case_yaml, dict):
                        cases = case_yaml.get('testcases') or []
                        if not cases and 'testcase' in case_yaml:
                            cases = [case_yaml['testcase']]
                    elif isinstance(case_yaml, list):
                        cases = case_yaml
                    # 过滤掉空字典（LLM 偶发返回的空占位）
                    cases = [c for c in cases if isinstance(c, dict) and c]
                    for c in cases:
                        if not c.get('module'):
                            c['module'] = mod_name
                    mod_cases.extend(cases)
                module_testcases.append((mod_name, mod_cases))
            except Exception as e:
                logger.error(f'模块[{mod_name}] 用例生成失败: {e}')
                self._log(f'  模块[{mod_name}] 用例生成失败: {e}')
                continue

        # 汇总全部用例
        all_cases = []
        for _, cases in module_testcases:
            all_cases.extend(cases)
        # 用例归一化：统一为扁平结构（解包 {module, testcase:{...}}），
        # 并给缺失操作步骤的用例补兜底步骤，避免入库后出现「无步骤用例」。
        flat_cases = []
        for tc in all_cases:
            if not isinstance(tc, dict):
                continue
            inner = tc.get('testcase', tc) if isinstance(tc.get('testcase'), dict) else tc
            if isinstance(inner, dict):
                if not inner.get('module') and tc.get('module'):
                    inner['module'] = tc.get('module')
                steps = inner.get('steps')
                steps_txt = ''
                if isinstance(steps, list):
                    steps_txt = ';'.join(
                        str(x.get('content') or x.get('action') or x) if isinstance(x, dict) else str(x)
                        for x in steps
                    )
                elif isinstance(steps, str):
                    steps_txt = steps
                if not steps_txt.strip():
                    # 先自愈：回喂 LLM 重生成一次操作步骤；仍失败才显式标记缺失
                    inner = self._repair_missing_steps(inner, inner.get('module') or '?')
                flat_cases.append(inner)
        all_cases = flat_cases
        if page is not None:
            page.page_testcases_json = json.dumps(all_cases, ensure_ascii=False)
            # 自愈闭环(P0)：快照 AI 原始用例（人工编辑前一次性写入，此后不随人工修改变化）
            page.page_ai_testcases_json = json.dumps(all_cases, ensure_ascii=False)
            page.save(update_fields=['page_testcases_json', 'page_ai_testcases_json'])
        else:
            self.prototype.testcases_json = json.dumps(all_cases, ensure_ascii=False)
        self._log(f'3.4 用例生成完成，共 {len(all_cases)} 条用例')

        # 3.5 冒烟提取（p4 技能输出 YAML，必须用 _parse_yaml 解析）
        # 关键：输入必须是「已生成的全部测试用例」，从中筛选 P0 用例作为冒烟，
        # 直接复用其 steps/expected，避免从测试点现编步骤导致与正式用例脱节、id 对不上。
        self._log('3.5 正在从已生成用例中筛选 P0 冒烟用例...')
        smoke_prompt = render_prompt(P4_SMOKE_EXTRACT,
                                     generated_testcases_json=json.dumps(all_cases, ensure_ascii=False))
        smoke_out = self._llm(self.analyzer_config, smoke_prompt, max_tokens=6000, temperature=0)
        smoke_json = _parse_yaml(_extract_block(smoke_out, 'yaml')) or {}
        # 兼容模型直接返回数组的情况，统一包装为 {smoke_overview, smoke_testcases, execution_suggestion}
        if isinstance(smoke_json, list):
            smoke_json = {'smoke_testcases': smoke_json}
        if page is not None:
            page.page_smoke_json = json.dumps(smoke_json, ensure_ascii=False)
            page.save(update_fields=['page_smoke_json'])
        else:
            self.prototype.smoke_json = json.dumps(smoke_json, ensure_ascii=False)

        # 3.6 质量自检（代码级 C1-C6，省掉 P8 LLM 调用）
        self._log('3.6 正在执行质量自检（代码级 C1-C6）...')
        q = self._code_quality_check(all_cases, smoke_json, all_final_tps)
        if page is None:
            self.prototype.quality_report_json = json.dumps(q['quality_json'], ensure_ascii=False)
        self._log('3.6 质量自检完成（代码级，0 次 LLM 调用）')
        if q['blocking']:
            reasons = '；'.join(b.get('message',str(b)) for b in q['blocking'])
            if page is not None:
                page.mark_stage(4, status='failed', error=f'质量门禁阻断：{reasons}')
                page.save(update_fields=['current_stage', 'stage_status', 'error_message'])
                self._log(f'画布[{page.page_name}] 质量门禁未通过：{reasons}')
                return {'final_testpoints': len(all_final_tps), 'testcases': len(all_cases),
                        'smoke': q['smoke_count'], 'excel': '', 'quality': 'failed',
                        'blocking_issues': q['blocking'], 'page_name': page.page_name, 'phase': 2}
            self.prototype.status = 'failed'
            self.prototype.error_message = f'质量门禁未通过（阻断项）：{reasons}。已保留测试用例和质量报告，但未导出正式 Excel。'
            self.prototype.excel_path = ''
            self.prototype.save()
            self._advance_selected_pages(self.prototype.current_stage, status='failed', error=self.prototype.error_message)
            self._log(self.prototype.error_message)
            return {'final_testpoints': len(all_final_tps), 'testcases': len(all_cases),
                    'smoke': q['smoke_count'], 'excel': '', 'quality': 'failed', 'blocking_issues': q['blocking'], 'phase': 2}
        if q['warnings']:
            self.prototype.error_message = '质量门禁告警（已放行导出）：' + '；'.join(q['warnings'])
            self._log('质量门禁软告警（已放行导出）：' + '；'.join(q['warnings']))
        else:
            self.prototype.error_message = ''
            self._log('质量门禁全部通过')

        # 3.7 Excel 导出
        self._log('3.7 正在导出 Excel...')
        excel_path = self._export_excel(all_cases, smoke_json) if page is None else None

        if page is not None:
            # 单画布模式：仅推进该画布进度，不落全局字段
            page.mark_stage(4, status='done', log=f'用例生成完成（{len(all_cases)} 条）')
            page.save(update_fields=['current_stage', 'stage_status', 'work_log'])
            self.prototype.status = 'designing'
            self.prototype.save(update_fields=['status'])
            self._log(f'画布[{page.page_name}] 用例生成完成！共 {len(all_final_tps)} 测试点 / {len(all_cases)} 用例')
            return {'final_testpoints': len(all_final_tps), 'testcases': len(all_cases),
                    'smoke': q['smoke_count'], 'excel': '', 'quality': 'passed' if not q['blocking'] else 'failed',
                    'page_name': page.page_name, 'phase': 2}
        self.prototype.excel_path = excel_path or ''
        self.prototype.status = 'done'
        self.prototype.current_stage = 3
        self.prototype.save()
        self._advance_selected_pages(4)
        self._log(f'用例生成全部完成！共 {len(all_final_tps)} 测试点 / {len(all_cases)} 用例 / Excel: {excel_path or "无"}')

        # 自适应沉淀（方案C闭环）：本次流程发现的风险处置/PCI处置/质量缺口/澄清结论，
        # 自动回填项目知识库（带 proto 的版本/功能前缀），供后续同类需求 RAG 召回。
        # 仅质量门禁通过（status='done'）时沉淀，避免把失败产物写入知识库。
        try:
            from apps.requirement_analysis.models import AIModelService
            kb_result = AIModelService.auto_fill_knowledge_from_modao_products(
                self.prototype,
                model_config=self.analyzer_config,
                user_id=self.prototype.created_by_id,
            )
            self._log(
                f'[知识沉淀] 自动回填 {kb_result.get("created_count", 0)} 条新知识'
                f'（跳过已存在 {kb_result.get("skipped_count", 0)} 条）'
            )
        except Exception as e:
            self._log(f'[知识沉淀] 自动回填失败（可忽略，不影响用例产出）: {e}')

        # 确认问答沉淀（方案C闭环补充）：把澄清阶段用户确认过的问答对精确沉淀为知识，
        # 与上面 AI 从全文提炼的约束知识互补（前者是「用户怎么确认的」，后者是「AI 总结的约束」）。
        try:
            from apps.requirement_analysis.models import AIModelService
            confirmed_answers = []
            for _item in self.prototype.clarification_items():
                if not isinstance(_item, dict):
                    continue
                _resolution = (str(_item.get('resolution') or _item.get('answer') or '')).strip()
                _q = (str(_item.get('suggested_question') or _item.get('issue') or _item.get('question') or '')).strip()
                if _q and _resolution:
                    confirmed_answers.append({'question': _q, 'answer': _resolution})
            if confirmed_answers:
                _ver = getattr(self.prototype, 'version', None)
                _mod = getattr(self.prototype, 'feature_module', None)
                conf_result = AIModelService.auto_fill_knowledge_from_confirmations(
                    confirmed_answers=confirmed_answers,
                    project_id=self.prototype.project_id,
                    user_id=self.prototype.created_by_id,
                    model_config=self.analyzer_config,
                    version_tag=getattr(_ver, 'name', '') or '',
                    module_tag=getattr(_mod, 'name', '') or '',
                )
                self._log(
                    f'[知识沉淀·确认问答] 自动回填 {conf_result.get("created_count", 0)} 条'
                    f'（跳过已存在 {conf_result.get("skipped_count", 0)} 条）'
                )
        except Exception as e:
            self._log(f'[知识沉淀·确认问答] 自动回填失败（可忽略，不影响用例产出）: {e}')

        return {
            'final_testpoints': len(all_final_tps),
            'testcases': len(all_cases),
            'smoke': len(smoke_json.get('smoke_testcases', []) if isinstance(smoke_json, dict) else []),
            'excel': excel_path,
            'phase': 2,
        }

    # ---------- 收集人工已确认的 PCI 答案 + 风险处置结论，供用例生成参考 ----------
    def _build_confirmed_context(self):
        """从 pci_json / risks_json 提取人工已确认的答案，拼接为上下文文本。
        用例生成(p3)注入后 AI 可基于确认结论精准出例。兼容多种结构：
        顶层数组 [{module, data:{pci_list/risk_points}}] / 单层 {data:{...}} / 裸数组。
        """
        parts = []
        # 1) PCI 确认答案
        try:
            pci_raw = json.loads(self.prototype.pci_json or '[]')
            pci_list = []
            if isinstance(pci_raw, list):
                for m in pci_raw:
                    lst = None
                    if isinstance(m, dict):
                        lst = (m.get('data', {}) or {}).get('pci_list') or m.get('pci_list')
                    if isinstance(lst, list):
                        pci_list.extend(lst)
            elif isinstance(pci_raw, dict):
                pci_list = (pci_raw.get('data', {}) or {}).get('pci_list') or pci_raw.get('pci_list') or []
            for pci in pci_list:
                if not isinstance(pci, dict):
                    continue
                desc = (pci.get('description') or '').strip()
                if not desc:
                    continue
                lines = []
                subs = pci.get('sub_questions') or []
                if isinstance(subs, list) and subs:
                    for s in subs:
                        q = (s.get('question') or '').strip()
                        a = (s.get('answer') or '').strip()
                        if q and a:
                            lines.append(f'  - Q: {q}\n    A: {a}')
                else:
                    a = (pci.get('answer') or pci.get('resolution_condition') or '').strip()
                    if a:
                        lines.append(f'  - A: {a}')
                if lines:
                    parts.append(f'【待确认问题 {pci.get("id", "")}】{desc}\n' + '\n'.join(lines))
        except Exception:
            pass
        # 2) 风险处置结论
        try:
            risk_raw = json.loads(self.prototype.risks_json or '[]')
            risk_list = []
            if isinstance(risk_raw, list):
                for m in risk_raw:
                    lst = None
                    if isinstance(m, dict):
                        lst = (m.get('data', {}) or {}).get('risk_points') or m.get('risk_points')
                    if isinstance(lst, list):
                        risk_list.extend(lst)
            elif isinstance(risk_raw, dict):
                risk_list = (risk_raw.get('data', {}) or {}).get('risk_points') or risk_raw.get('risk_points') or []
            for r in risk_list:
                if not isinstance(r, dict):
                    continue
                desc = (r.get('description') or r.get('risk_point') or '').strip()
                mit = (r.get('mitigation') or '').strip()
                if desc and mit:
                    parts.append(f'【风险 {r.get("id", "")}】{desc}\n  - 处置: {mit}')
        except Exception:
            pass
        if not parts:
            return ''
        return '\n\n'.join(parts)

    # ---------- Excel 导出（复用技能 module_exporter 样式）----------
    def _export_excel(self, testcases, smoke_json):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            from openpyxl.utils import get_column_letter
        except Exception as e:
            logger.warning(f'openpyxl 不可用，跳过 Excel: {e}')
            return ''

        out_dir = os.path.join(MEDIA_ROOT, 'modao', self.session_id)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, 'testcases_all.xlsx')

        wb = Workbook()
        if 'Sheet' in wb.sheetnames:
            del wb['Sheet']

        HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
        HEADER_FONT = Font(name='微软雅黑', size=11, bold=True, color='FFFFFF')
        SMOKE_FILL = PatternFill(start_color='FFE699', end_color='FFE699', fill_type='solid')
        P0_FILL = PatternFill(start_color='F8CBAD', end_color='F8CBAD', fill_type='solid')
        P1_FILL = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')
        CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
        LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)
        THIN = Border(*[Side(style='thin', color='BFBFBF')] * 4)

        # 总览
        ws = wb.create_sheet('总览', 0)
        ws.append(['需求梳理与用例生成 - 总览'])
        ws['A1'].font = Font(name='微软雅黑', size=14, bold=True)
        p0 = sum(1 for c in testcases if c.get('priority') == 'P0')
        p1 = sum(1 for c in testcases if c.get('priority') == 'P1')
        p2 = sum(1 for c in testcases if c.get('priority') == 'P2')
        smoke_cases = smoke_json.get('smoke_testcases', []) if isinstance(smoke_json, dict) else []
        smoke_cnt = len(smoke_cases)
        stats = [
            ('模块数', len({c.get('module') for c in testcases})),
            ('用例总数', len(testcases)),
            ('P0', p0), ('P1', p1), ('P2', p2),
            ('冒烟用例数', smoke_cnt),
            ('生成时间', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ]
        ws.append([])
        ws.append(['统计项', '数值'])
        for c in (1, 2):
            ws.cell(row=3, column=c).font = HEADER_FONT
            ws.cell(row=3, column=c).fill = HEADER_FILL
        for label, val in stats:
            ws.append([label, val])
        ws.column_dimensions['A'].width = 20
        ws.column_dimensions['B'].width = 15

        # 用例 sheet（按模块）
        modules = {}
        for c in testcases:
            modules.setdefault(c.get('module', '未命名'), []).append(c)
        cols = [('ID', 14), ('模块', 15), ('标题', 35), ('前置条件', 25), ('测试步骤', 50),
                ('预期结果', 30), ('优先级', 8), ('类型', 10), ('关联需求', 15), ('是否冒烟', 10), ('备注', 20)]
        for mod_name, cases in modules.items():
            ws2 = wb.create_sheet(mod_name[:31])
            ws2.append([c[0] for c in cols])
            for ci, _ in enumerate(cols, 1):
                cell = ws2.cell(row=1, column=ci)
                cell.font = HEADER_FONT
                cell.fill = HEADER_FILL
                cell.alignment = CENTER
                cell.border = THIN
            for c in cases:
                steps = c.get('steps', [])
                steps_txt = '\n'.join(f'{i+1}. {s}' for i, s in enumerate(steps)) if isinstance(steps, list) else str(steps)
                ws2.append([
                    c.get('id', ''), c.get('module', ''), c.get('title', ''),
                    c.get('precondition', ''), steps_txt, c.get('expected', ''),
                    c.get('priority', ''), c.get('type', ''), c.get('requirement_ref', ''),
                    '是' if c.get('is_smoke') else '否', c.get('notes', ''),
                ])
                r = ws2.max_row
                for ci in range(1, len(cols) + 1):
                    cell = ws2.cell(row=r, column=ci)
                    cell.alignment = LEFT
                    cell.border = THIN
                    cell.font = Font(name='微软雅黑', size=10)
                    if ci == 7 and c.get('priority') in ('P0', 'P1'):
                        cell.fill = P0_FILL if c.get('priority') == 'P0' else P1_FILL
                        cell.alignment = CENTER
                    if ci == 10 and c.get('is_smoke'):
                        cell.fill = SMOKE_FILL
                        cell.alignment = CENTER
                ws2.row_dimensions[r].height = 60
            for ci, (_, w) in enumerate(cols, 1):
                ws2.column_dimensions[get_column_letter(ci)].width = w
            ws2.freeze_panes = 'A2'

        # 冒烟 sheet
        if smoke_cases:
            ws3 = wb.create_sheet('冒烟用例')
            shead = ['序号', '用例ID', '所属模块', '冒烟标题', '前置条件', '测试步骤', '通过标准', '执行结果', '执行人', '备注']
            ws3.append(shead)
            for ci, h in enumerate(shead, 1):
                cell = ws3.cell(row=1, column=ci)
                cell.font = HEADER_FONT
                cell.fill = HEADER_FILL
                cell.alignment = CENTER
                cell.border = THIN
            for i, sc in enumerate(smoke_cases, 1):
                steps = sc.get('test_steps') or sc.get('steps') or []
                steps_txt = '\n'.join(f'{j+1}. {s}' for j, s in enumerate(steps)) if isinstance(steps, list) else str(steps)
                expected_val = sc.get('pass_criteria') or sc.get('expected') or ''
                if isinstance(expected_val, list):
                    expected_val = '\n'.join(str(x) for x in expected_val)
                ws3.append([i, sc.get('id', ''), sc.get('module', ''), sc.get('title', ''),
                            sc.get('precondition', ''), steps_txt,
                            expected_val, '', '', sc.get('notes', '')])
                for ci in range(1, len(shead) + 1):
                    cell = ws3.cell(row=ws3.max_row, column=ci)
                    cell.alignment = LEFT
                    cell.border = THIN
                    cell.font = Font(name='微软雅黑', size=10)
            ws3.freeze_panes = 'A2'

        wb.save(out_path)
        logger.info(f'Excel 导出成功: {out_path}')
        return out_path

    # ---------- 阶段4：冒烟执行决策（前端驱动）----------
    def record_smoke_decision(self, decision: str, reject_email: str = ''):
        self.prototype.stage4_decision = decision  # passed / rejected
        if reject_email:
            self.prototype.clarification_log = (self.prototype.clarification_log or '') + '\n\n' + reject_email
        self.prototype.current_stage = 4
        self.prototype.save()
