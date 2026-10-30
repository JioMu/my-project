# -*- coding: utf-8 -*-
"""APK 回归的「业务资料」输入源：操作说明文档 + 项目知识库（DB 条目 + 文件知识库）。

为什么需要这一层
----------------
REA 分析原本只有一块输入 —— APK 静态清单（包名/版本/权限/Activity）。
只靠它无法知道「首页有哪些 tab」「下单要几步」，提示词里因此写死了
"信息不足就产出通用探索路径，**不要编造页面名称**"。
结果是路径都正确但很泛（启动探索 / 设置回归），对具体业务几乎没有覆盖。

本模块把另外几类真实资料接进来：
  1. 用户上传的操作说明 / 需求文档（docx / pdf / md / txt）
  2. 项目知识库里已维护的条目（ProjectKnowledge，纯文本，按项目取）
  3. 项目「文件知识库」目录下的 Markdown 文档
     （Project.knowledge_base_path 指向的目录，如 /app/knowledge-bases/万年历/）

注意 2 与 3 是**两套独立存储**：2 在 MySQL 表里，3 在磁盘目录里（项目页「知识库 →
文件知识库」Tab 展示的就是 3）。只读 2 时，用户在页面上明明看到几十个 md 文件、
生成路径却依然全是通用路径 —— 这就是补上 3 的原因。

设计约束（重要）
----------------
- **不臆造**：没有资料时返回空串，由 apk_rea 维持原有的"通用路径 + 明确
  说明未确认内容"行为，不能因为加了资料通道就放松对模型的要求。
- **可溯源**：每一块资料都带来源标签（文件名 / 知识条目标题），
  拼进提示词后模型被要求标注 evidence，报告里能看到"这条路径来自哪份资料"。
- **有上限**：文档动辄几十页，直接塞进提示词会超上下文。按字符预算截断，
  并在返回里记录"是否被截断"，让用户知道模型没看到全文。
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

#: 单份文档 / 全部资料的字符预算。
#: deepseek-chat 上下文 64K，提示词还有 APK 清单和输出要求要占，
#: 资料部分留 20000 字符（约 1.3 万汉字）足够覆盖一份操作手册的要点。
DOC_CHAR_BUDGET = 20000
KB_CHAR_BUDGET = 8000
#: 单条知识库条目上限（防止某条超长把预算吃光）
KB_ITEM_BUDGET = 2000

#: 文件知识库（磁盘 md 目录）的字符预算。
#: 单独给预算而不与 DB 条目共用，避免一边把另一边挤没；
#: 合计仍受 kb_total_chars() 的总闸限制。
KB_FILE_CHAR_BUDGET = 12000
#: 单份 md 文件最多取多少字符（取开头 —— 知识库 md 通常"概述在前、细节在后"）
KB_FILE_ITEM_BUDGET = 3000
#: 一次最多并入几份 md（按相关性排序后取前 N）
KB_FILE_MAX_FILES = 6

SUPPORTED_EXTS = ('.docx', '.pdf', '.md', '.txt', '.markdown')
#: 文件知识库里视为知识文档的扩展名
KB_FILE_EXTS = ('.md', '.markdown')


# ------------------------------------------------------------------ 文档解析
def extract_text(file_path, original_name=''):
    """从上传文件提取纯文本。不支持的格式抛 ValueError（由调用方转成可读错误）。"""
    ext = os.path.splitext(original_name or file_path)[1].lower()
    if ext == '.docx':
        return _extract_docx(file_path)
    if ext == '.pdf':
        return _extract_pdf(file_path)
    if ext in ('.md', '.txt', '.markdown'):
        return _extract_plain(file_path)
    raise ValueError('不支持的文件类型 %s（支持 %s）'
                     % (ext or '(无扩展名)', ' / '.join(SUPPORTED_EXTS)))


def _extract_docx(path):
    try:
        import docx
    except ImportError:
        raise ValueError('服务端未安装 python-docx，无法解析 .docx')
    doc = docx.Document(path)
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    # 表格经常承载关键业务信息（字段说明、状态流转），必须一起取
    for table in getattr(doc, 'tables', []):
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(' | '.join(cells))
    return '\n'.join(parts).strip()


def _extract_pdf(path):
    # 优先 pymupdf（对中文/排版更稳），退回 PyPDF2
    try:
        import fitz  # pymupdf
        doc = fitz.open(path)
        text = '\n'.join(page.get_text() for page in doc)
        doc.close()
        if text.strip():
            return text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.info('[APK-CTX] pymupdf 解析失败，回退 PyPDF2: %s', exc)
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise ValueError('服务端未安装 PDF 解析库')
    reader = PdfReader(path)
    return '\n'.join((p.extract_text() or '') for p in reader.pages).strip()


def _extract_plain(path):
    for enc in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030'):
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read().strip()
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise ValueError('读取文件失败：%s' % exc)
    raise ValueError('无法识别文件编码（请另存为 UTF-8）')


# ------------------------------------------------------------------ 清洗
def _clean(text):
    """去掉多余空行/行尾空白，压缩连续空白。保留段落边界。"""
    if not text:
        return ''
    lines = [ln.strip() for ln in str(text).replace('\r\n', '\n').split('\n')]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append('')
            blank = True
        else:
            out.append(ln)
            blank = False
    # 去掉连续重复行（PDF 抽出的页眉页脚常整页重复）
    dedup, seen_run = [], 0
    for ln in out:
        if dedup and ln == dedup[-1]:
            seen_run += 1
            if seen_run >= 2:
                continue
        else:
            seen_run = 0
        dedup.append(ln)
    return '\n'.join(dedup).strip()


def _truncate(text, budget):
    """按字符预算截断，尽量在段落边界断开。返回 (text, truncated)。"""
    if len(text) <= budget:
        return text, False
    cut = text[:budget]
    # 回退到最后一个换行，避免切在句子中间
    pos = cut.rfind('\n')
    if pos > budget * 0.6:
        cut = cut[:pos]
    return cut.rstrip() + '\n…（内容过长，已截断）', True


# ------------------------------------------------------------------ 知识库
def collect_knowledge(project_id, max_chars=KB_CHAR_BUDGET):
    """取项目知识库中启用的条目，按分类汇总成一段文本。

    Args:
        project_id: 项目 ID（可为空 —— 为空时直接返回空，不猜测项目）

    Returns:
        (text, meta)  meta = {'items': [...标题...], 'truncated': bool}
    """
    if not project_id:
        return '', {'items': [], 'truncated': False}
    try:
        from apps.projects.models import ProjectKnowledge
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 无法导入 ProjectKnowledge: %s', exc)
        return '', {'items': [], 'truncated': False}

    qs = ProjectKnowledge.objects.filter(project_id=project_id, is_active=True)
    # 分类顺序：背景 → 术语 → 测试重点 → 约束 → 注意事项，与人的阅读习惯一致
    order = {'background': 0, 'terminology': 1, 'test_focus': 2,
             'constraints': 3, 'notes': 4}
    items = sorted(qs, key=lambda k: (order.get(k.category, 9), k.sort_order or 0))

    blocks, titles, used = [], [], 0
    truncated = False
    for k in items:
        content = _clean(k.content)[:KB_ITEM_BUDGET]
        if not content:
            continue
        label = k.get_category_display()
        block = '【%s】%s\n%s' % (label, k.title, content)
        if used + len(block) > max_chars:
            truncated = True
            break
        blocks.append(block)
        titles.append(k.title)
        used += len(block)

    return '\n\n'.join(blocks), {'items': titles, 'truncated': truncated}


# ------------------------------------------------------------- 文件知识库（磁盘 md）
def _scan_kb_files(kb_path):
    """递归列出目录下的知识文档。

    Returns:
        [(相对路径, 绝对路径, 标题, mtime), ...]，按相对路径排序（前缀 01-/02- 即阅读顺序）。
    """
    found = []
    for root, dirs, files in os.walk(kb_path):
        # 跳过隐藏目录与常见无关目录，避免读到 .git / 附件缓存
        dirs[:] = sorted(d for d in dirs if not d.startswith(('.', '__pycache__')))
        for name in sorted(files):
            if not name.lower().endswith(KB_FILE_EXTS):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, kb_path).replace('\\', '/')
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0
            # 标题优先取文件 H1，回退文件名
            title = os.path.splitext(name)[0]
            found.append((rel, full, title, mtime))
    found.sort(key=lambda x: x[0])
    return found


def _file_title(full_path, fallback):
    """展示名以**文件名**为准（与页面左侧目录树一致，用户按路径找得到）。

    不用 md 的 H1：实测 `02-feature-map.md` 的 H1 是「广告配置规则」，
    名字对不上会让人以为读错了文件。
    """
    return fallback


def _score_file(rel, content, tokens):
    """按查询词命中次数打分（目录名权重更高：业务常按模块分目录）。"""
    if not tokens:
        return 0
    path_low = rel.lower()
    text_low = content[:6000].lower()
    score = 0
    for t in tokens:
        if t in path_low:
            score += 5
        score += text_low.count(t)
    return score


def _query_tokens(query_text):
    """把查询文本切成检索词：中文按 2 字滑窗 + 英文/数字整词。"""
    text = (query_text or '').strip()
    if not text:
        return []
    tokens = set()
    for word in re.findall(r'[A-Za-z0-9_]{2,}', text):
        tokens.add(word.lower())
    han = re.findall(r'[\u4e00-\u9fff]+', text)
    for seg in han:
        if len(seg) <= 4:
            tokens.add(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i + 2])
    # 去掉过泛的词，避免"页面/功能/校验"把打分拉平
    noise = {'页面', '功能', '校验', '验证', '测试', '规则', '数据', '说明'}
    return [t for t in tokens if t not in noise and len(t) >= 2]


def resolve_kb_path(project):
    """解析项目的文件知识库目录（复用主服务同一套解析，保证口径一致）。"""
    if not project:
        return ''
    raw = (getattr(project, 'knowledge_base_path', '') or '').strip()
    if not raw:
        return ''
    try:
        from apps.requirement_analysis.ai_model_service import AIModelService
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 无法导入 AIModelService: %s', exc)
        return ''
    return AIModelService._resolve_knowledge_path(project)


def collect_kb_files(project_id, query_text='', max_chars=KB_FILE_CHAR_BUDGET):
    """读项目「文件知识库」目录下的 md，按相关性挑若干篇拼成文本。

    为什么不整目录全读：`万年历` 就有 23 篇、Bodii健康 72 篇，全读会把
    提示词预算吃光且大量无关。这里按查询词（APK 包名/Activity/人工补充说明）
    打分取前 KB_FILE_MAX_FILES 篇。

    Args:
        project_id: 项目 ID，空则直接返回空（不猜项目）
        query_text: 检索词来源；为空时按目录顺序取前 N 篇（前缀编号即作者排序）

    Returns:
        (text, meta)  meta = {'items': [相对路径...], 'total_files': n,
                              'truncated': bool, 'path': 目录}
    """
    empty = {'items': [], 'total_files': 0, 'truncated': False, 'path': ''}
    if not project_id:
        return '', empty
    try:
        from apps.projects.models import Project
        project = Project.objects.filter(id=project_id).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 无法导入 Project: %s', exc)
        return '', empty
    kb_path = resolve_kb_path(project)
    if not kb_path or not os.path.isdir(kb_path):
        return '', empty

    entries = _scan_kb_files(kb_path)
    if not entries:
        return '', dict(empty, path=kb_path)

    tokens = _query_tokens(query_text)
    scored = []
    for rel, full, title, mtime in entries:
        try:
            with open(full, 'r', encoding='utf-8', errors='ignore') as fh:
                raw = fh.read()
        except OSError as exc:
            logger.warning('[APK-CTX] 读取知识文件失败 %s: %s', rel, exc)
            continue
        body = _clean(raw)
        if not body:
            continue
        scored.append({
            'rel': rel,
            'title': _file_title(full, title),
            'body': body,
            'score': _score_file(rel, body, tokens),
            'mtime': mtime,
        })

    if tokens:
        # 有检索词：按分数降序，同分按路径（保持 01- 在前）
        picked = sorted(scored, key=lambda x: (-x['score'], x['rel']))[:KB_FILE_MAX_FILES]
        # 全 0 分说明词完全不命中，退回按目录顺序取前 N，避免"什么都没读到"
        if all(p['score'] == 0 for p in picked):
            picked = scored[:KB_FILE_MAX_FILES]
    else:
        picked = scored[:KB_FILE_MAX_FILES]

    blocks, used, truncated = [], 0, False
    for item in picked:
        body = item['body']
        if len(body) > KB_FILE_ITEM_BUDGET:
            body = body[:KB_FILE_ITEM_BUDGET].rstrip() + '\n…（单篇过长，已截断）'
            truncated = True
        block = '【文档】%s\n%s' % (item['rel'], body)
        if used + len(block) > max_chars:
            truncated = True
            break
        blocks.append(block)
        used += len(block)

    meta = {
        'items': [p['rel'] for p in picked][:len(blocks)],
        'total_files': len(scored),
        'truncated': truncated,
        'path': kb_path,
    }
    return '\n\n'.join(blocks), meta


def kb_inventory(project_id):
    """给前端/接口用的文件知识库概览：目录、总篇数、前若干篇标题。"""
    try:
        from apps.projects.models import Project
        project = Project.objects.filter(id=project_id).first()
    except Exception:  # noqa: BLE001
        return {'path': '', 'file_count': 0, 'files': []}
    kb_path = resolve_kb_path(project)
    if not kb_path or not os.path.isdir(kb_path):
        return {'path': kb_path or '', 'file_count': 0, 'files': []}
    entries = _scan_kb_files(kb_path)
    return {
        'path': kb_path,
        'file_count': len(entries),
        'files': [{'path': rel, 'title': _file_title(full, title)} for rel, full, title, _ in entries],
    }


# ------------------------------------------------------------------ 拼装
def build_business_context(run):
    """给 apk_rea 用的业务资料文本。

    顺序：上传的操作说明 → 项目知识库(DB 条目) → 文件知识库(md) → 用户手填补充说明。
    每一块都带来源标签，便于模型标注 evidence。

    Returns:
        dict: {
          'text': 拼好的资料文本（可能为空串）,
          'sources': [{'kind','name','chars','truncated'}...],
          'chars': 总字符数,
          'empty': 是否完全没有资料,
        }
    """
    sources, blocks = [], []

    # 1) 上传的操作说明文档
    for doc in (getattr(run, 'context_docs', None) or []):
        if not isinstance(doc, dict):
            continue
        text = _clean(doc.get('text') or '')
        if not text:
            continue
        text, truncated = _truncate(text, DOC_CHAR_BUDGET)
        name = doc.get('name') or '说明文档'
        blocks.append('=== 操作说明：%s ===\n%s' % (name, text))
        sources.append({'kind': 'doc', 'name': name, 'chars': len(text),
                        'truncated': truncated})

    # 2) 项目知识库
    kb_project = getattr(run, 'project_id', None)
    kb_text, kb_meta = collect_knowledge(kb_project)
    if kb_text:
        kb_text, truncated = _truncate(kb_text, KB_CHAR_BUDGET)
        blocks.append('=== 项目知识库（共 %d 条） ===\n%s'
                      % (len(kb_meta['items']), kb_text))
        for title in kb_meta['items']:
            sources.append({'kind': 'kb', 'name': title, 'chars': 0,
                            'truncated': False})
        sources.append({'kind': 'kb_summary', 'name': '知识库整体',
                        'chars': len(kb_text),
                        'truncated': truncated or kb_meta['truncated']})

    # 3) 文件知识库（项目目录下的 md，与 DB 条目是两套独立存储）
    #    检索词用「人工补充说明」+ 前端传的 run.context_query（如有），
    #    这样用户写"日历模式切换"就能优先命中同名文档。
    query = ' '.join(filter(None, [
        _clean(getattr(run, 'context_notes', '') or '')[:200],
        _clean(str(getattr(run, 'context_query', '') or ''))[:200],
    ]))
    file_text, file_meta = collect_kb_files(kb_project, query_text=query)
    if file_text:
        file_text, truncated = _truncate(file_text, KB_FILE_CHAR_BUDGET)
        blocks.append('=== 项目文件知识库（共 %d 篇，取 %d 篇） ===\n%s'
                      % (file_meta['total_files'], len(file_meta['items']), file_text))
        for rel in file_meta['items']:
            sources.append({'kind': 'kb_file', 'name': rel, 'chars': 0,
                            'truncated': False})
        sources.append({'kind': 'kb_file_summary',
                        'name': '文件知识库（%s）' % (file_meta['path'] or '-'),
                        'chars': len(file_text),
                        'truncated': truncated or file_meta['truncated']})

    # 4) 用户手填补充说明
    extra = _clean(getattr(run, 'context_notes', '') or '')
    if extra:
        extra, truncated = _truncate(extra, 4000)
        blocks.append('=== 人工补充说明 ===\n%s' % extra)
        sources.append({'kind': 'notes', 'name': '人工补充', 'chars': len(extra),
                        'truncated': truncated})

    text = '\n\n'.join(blocks).strip()
    return {
        'text': text,
        'sources': sources,
        'chars': len(text),
        'empty': not text,
    }
