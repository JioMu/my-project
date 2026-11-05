# -*- coding: utf-8 -*-
"""
墨刀技能 - 原型读取器（浏览器自动逐页提取需求内容，含注释/标注）

核心能力：
  1. 通过 Playwright 打开墨刀原型 URL（支持 cookie 回填绕过登录墙）。
  2. 枚举原型页面（页面树），逐页点击进入。
  3. 每页提取：可见文本 + 注释/标注（批注浮层）+ 截图。
  4. 对图片型/视觉型内容，调视觉模型（browser_use_vision）做 OCR 补充。
  5. 首次登录：可见浏览器扫码，导出 cookie 回填（用户决策 1）。

注意：墨刀是 SPA，页面结构可能随版本变化；提取规则做了防御性 try/except，
单页失败不影响整体，并在结果中标记 extraction_warnings。
"""
import os
import re
import json
import html
import uuid
import base64
import hashlib
import logging
import asyncio
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, quote

logger = logging.getLogger(__name__)

# 并发视觉 OCR 的最大并行路数（受信号量限制）。
OCR_CONCURRENCY = 8

# OCR 缓存目录（按截图 sha256 文件名存储 JSON 结果，避免同一截图重复调用视觉模型）
OCR_CACHE_DIR = os.path.join(os.environ.get('MEDIA_ROOT', 'media'), 'modao', '_ocr_cache')

# 墨刀自适应 OCR 分类阈值：
#   dom_text 超过此字符数且不含表格/大量图片时，跳过视觉 OCR（直接复用 DOM 文本）
OCR_SKIP_DOM_TEXT_MIN = 400
#   dom_text 超过此字符数时，即使有少量图片也跳过 OCR（DOM 信息已足够）
OCR_SKIP_DOM_TEXT_RICH = 800

# 用户手动终止信号：后台阶段任务在检查点轮询此标记，命中即中断。
from apps.requirement_analysis.cancel_flags import TERMINATE_REQUESTED


class ModaoTerminateError(Exception):
    """用户在前端手动终止任务时抛出，被阶段线程捕获后写 status=failed。"""


def _raise_if_terminated(prototype_id):
    if prototype_id and TERMINATE_REQUESTED.get(prototype_id):
        raise ModaoTerminateError(f'用户手动终止任务(id={prototype_id})')


# 页面树常见选择器（按优先级尝试；命中其一即认为找到页面列表）。
# 既兼容墨刀，也尽量覆盖 webshare / 通用原型 SPA 的左侧页面树（best-effort）。
# 页面项无 data-cid 时（如单文件 HTML 报告页），视口指纹是唯一判据。
# 取 64x64 灰度下"灰度差 > 8 的像素占比"作为差异比例。
# 阈值 0.05 由 proto=93 实测定标（1440x900 截图）：
#   仅左侧导航选中态变化（= 画布其实没切换）= 0.003 ~ 0.005
#   真实画布内容切换                        = 0.14 ~ 0.42
# 两者分界极清晰，取 0.05 既不会误杀真实切换，也能识别"只高亮了导航但画布没动"。
_MIN_VIEWPORT_DIFF_RATIO = 0.05

PAGE_TREE_SELECTORS = [
    # 墨刀当前分享视图：真正可点击的页面行带 data-cid；不要命中其内部
    # `.screen-item-icon`，否则 click() 只会选中图标而不会切换画布。
    '.rn-list-item.page[data-cid][data-interactive-target-type="6"]',
    '[data-cid][data-interactive-target-type="6"][class*="StyledScreenItem"]',
    # 墨刀
    '.page-list .page-item',
    '.page-panel .page-item',
    '[class*="pageList"] [class*="pageItem"]',
    '[class*="page-list"] [class*="page-item"]',
    '[class*="PageList"] [class*="PageItem"]',
    'ul.pages li',
    '.modao-page-item',
    # 通用 SPA / webshare 类左侧页面树（命中即认为找到页面列表）
    '[class*="pageTree"] [class*="pageItem"]',
    '[class*="page-tree"] [class*="page-item"]',
    '[class*="outline"] [class*="item"]',
    '[class*="catalog"] [class*="item"]',
    '[class*="sidebar"] [class*="page"]',
    '[class*="navPage"]',
    '[class*="tree"] li',
    '[class*="menu"] li',
]

# webshare 专用页面树选择器（hash 路由原型：目录页左侧通常列出带 #id= / &p= 的子页面链接）。
# 命中其一即认为找到页面列表；识别不到时回退到通用 PAGE_TREE_SELECTORS。
WEBSHARE_PAGE_TREE_SELECTORS = [
    '[class*="pageTree"] [class*="pageItem"]',
    '[class*="page-tree"] [class*="page-item"]',
    '[class*="pageList"] [class*="pageItem"]',
    '[class*="outline"] [class*="item"]',
    '[class*="catalog"] [class*="item"]',
    '[class*="sidebar"] [class*="page"]',
    '[class*="navPage"]',
    '[class*="tree"] li',
    '[class*="menu"] li',
    # webshare 用 hash 区分页面，目录里通常是带 #id= / &p= 的链接
    'a[href*="#id="]',
    'a[href*="&p="]',
    'a[href*="?p="]',
]

# ---------- webshare / Axure 原型：纯 HTTP 解析页面树（无需浏览器）----------
# Axure 生成的原型把页面树放在 data/document.js（不在 DOM），每个页面是服务端静态
# 渲染的 HTML（文本在 <div id="uN_text"> 容器内）。因此 webshare 走 HTTP 抓取 + 解析，
# 比 Playwright 渲染更稳更快，且能一次自动发现目录下所有子页面。
_AXURE_DOC_URL = 'data/document.js'
_AXURE_VAR_RE = re.compile(r'([\w$]+)\s*=\s*"((?:[^"\\]|\\.)*)"')
_AXURE_PAGE_RE = re.compile(
    r'_\(\s*s\s*,\s*([\w$]+|"[^"]*")\s*,\s*u\s*,\s*([\w$]+|"[^"]*")\s*,\s*w\s*,\s*([\w$]+|"[^"]*")\s*,\s*y\s*,\s*([\w$]+|"[^"]*")'
)
_AXURE_TEXT_DIV_RE = re.compile(r'id="u\d+_text"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r'<[^>]+>')
_UA_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
}

# ---------- 单文件 H5 交互原型（自研格式，如 WebShare 分享的单 HTML 只读页）----------
# 特征：单个 HTML 文件内联 JS 里定义 `const pages = [{id, group, title, states}]` 页面树，
# 另有「页面 id → HTML 模板字符串」的 JSON 对象（键为页面 id）。文本静态内联，纯 HTTP 即可提取。
_SINGLEFILE_H5_PAGES_RE = re.compile(r'(?:const|let|var)\s+pages\s*=\s*(\[)')
_SINGLEFILE_H5_ITEM_RE = re.compile(r'\{([^{}]*)\}')
_SINGLEFILE_H5_FIELD_RE = re.compile(
    r"""([A-Za-z_]\w*)\s*:\s*(?:(['"])(.*?)\2|(\[[^\[\]]*\]))""", re.S)
_SINGLEFILE_H5_TPL_RE = re.compile(r'(?:const|let|var)\s+\w+\s*=\s*(\{)')
_SCRIPT_RE = re.compile(r'<script[^>]*>(.*?)</script>', re.S)

# ---------- PRD 报告页（自研格式，第 4 种来源，如「Lovin1.2.20-PRD.html」）----------
# 与单文件 H5 原型的本质区别：**没有内联 pages 页面树**，也没有手机画布。
# 结构 = 侧边栏目录 + 预览栏（N 张内联截图卡片）+ 正文栏（M 个 requirement-module 需求模块）。
#   · 真正的需求正文 = `.requirement-module` 区块（含「【说明】●分组 ➢条目」/【操作】/验收标准）
#   · `.prototype-item` 区块 = 一张截图卡片，只有标题（页名）—— 作为页面清单与视觉证据
# 注意：这类文件常内联数十张 base64 截图（可达 100MB+），必须先剔除 data URI 再解析，
# 否则逐字符扫描会退化成分钟级。剔除后纯文本通常仅数十 KB。
_PRD_MODULE_CLS = 'requirement-module'
_PRD_ITEM_CLS = 'prototype-item'
# 新版 PRD 报告页（2026-11 实测「心动手表 1.3.10｜Lovin PRD」）：
#   <section class="doc-page" data-doc-page="binding-2">   ← 需求正文载体（一个页面一块）
#     <h2 class="page-heading">页面名</h2>
#     <p class="page-intro">页面概述</p>
#     <section class="state-requirement" data-state="A06">  ← 该页面的一个状态需求
#   <section class="prototype-page" data-page="binding-2">  ← 原型截图（与 doc-page 一一对应）
#     <article class="prototype-state" data-state="A06"><div class="prototype-card">截图</div>
# 旧版用 requirement-module / prototype-item，新版用 doc-page / prototype-card。
_PRD_DOC_PAGE_CLS = 'doc-page'
_PRD_CARD_CLS = 'prototype-card'
# 严格匹配真实 doc-page 容器标签：class 与 data-doc-page 必须同标签出现，
# 避免正文/代码示例里提到该 class 名就被误当切分点。
_PRD_DOC_PAGE_RE = re.compile(
    r'<[a-zA-Z][^>]{0,300}?class="[^"]*\bdoc-page\b[^"]*"[^>]{0,300}?'
    r'data-doc-page="[^"]+"[^>]{0,200}>'
    r'|<[a-zA-Z][^>]{0,300}?data-doc-page="[^"]+"[^>]{0,300}?'
    r'class="[^"]*\bdoc-page\b[^"]*"[^>]{0,200}>'
)
# 内联 base64 图片（data URI）：用于解析前剥离，避免超大字符串拖慢正则
_DATA_URI_RE = re.compile(r'data:image/[^"\')]{200,}')
# 页面标题：优先 <h4 class="prototype-title">NN标题</h4>，兜底 img 的 alt/data-title
_PRD_TITLE_RE = re.compile(
    r'class="prototype-title"[^>]*>(.*?)</h4>|'
    r'<img[^>]*\balt="([^"]*)"',
    re.S | re.I,
)
_PRD_BLOCK_SPLIT_MIN = 1  # 至少要有 1 个需求模块才认为是 PRD 报告页


def _extract_js_balanced(text: str, start: int, open_ch: str = '[', close_ch: str = ']') -> Optional[str]:
    """从 text[start] 起（指向 open_ch）用引号感知括号配对，返回完整 [...]/{...} 文本。"""
    depth = 0
    in_str = None
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in '"\'':
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _axure_resolve(token: str, var_map: Dict[str, str]) -> str:
    token = (token or '').strip()
    if token.startswith('"') and token.endswith('"'):
        return html.unescape(token[1:-1])
    return var_map.get(token, '')


def parse_axure_document(js_text: str) -> List[Dict[str, str]]:
    """从 Axure 的 data/document.js 解析页面树，返回 [{id, pageName, url}]（过滤文件夹节点）。"""
    var_map = {m.group(1): m.group(2) for m in _AXURE_VAR_RE.finditer(js_text)}
    pages: List[Dict[str, str]] = []
    for m in _AXURE_PAGE_RE.finditer(js_text):
        ptype = _axure_resolve(m.group(3), var_map)
        if ptype and ptype != 'Wireframe':
            continue  # 跳过文件夹等非页面节点
        pid = _axure_resolve(m.group(1), var_map)
        name = _axure_resolve(m.group(2), var_map)
        url = _axure_resolve(m.group(4), var_map)
        if pid and url:
            pages.append({'id': pid, 'pageName': name or pid, 'url': url})
    return pages


def extract_axure_text(html_text: str) -> str:
    """从 Axure 页面 HTML 提取可见文本（仅取 _text 容器内的 p/span 文本，保留段落结构）。"""
    texts: List[str] = []
    for block in _AXURE_TEXT_DIV_RE.findall(html_text):
        # 在块级/换行标签处断行，保留段落结构
        block = re.sub(r'</(p|div|br\s*/?)>', '\n', block, flags=re.I)
        block = _TAG_RE.sub('', block)
        block = html.unescape(block)
        block = re.sub(r'[ \t]+', ' ', block)
        block = re.sub(r'\n\s*\n+', '\n', block).strip()
        if block:
            texts.append(block)
    return '\n'.join(texts)

# 注释/标注相关选择器（批注气泡、评论浮层）
ANNOTATION_SELECTORS = [
    '[class*="comment"]',
    '[class*="note"]',
    '[class*="annotation"]',
    '[class*="批注"]',
    '[class*="remark"]',
    '.comment-bubble',
    '.note-item',
]

MEDIA_ROOT = os.environ.get('MEDIA_ROOT', 'media')


class ModaoExtractor:
    """墨刀原型逐页读取器。"""

    def __init__(self, run_id: Optional[str] = None, media_root: str = MEDIA_ROOT):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.media_root = media_root
        self.shot_dir = os.path.join(media_root, 'modao', self.run_id)
        os.makedirs(self.shot_dir, exist_ok=True)

    # ---------- 断点续读检查点 ----------
    # 背景：Playwright 逐页读取有 1800s 整体超时（见 extract_sync）。旧实现把 pages
    # 只攒在内存里、循环结束后才落库，因此**中途超时会丢掉已读的全部画布**
    # （实例：30 页读到第 15 页超时 → status=failed、0 页，前 15 页白读）。
    # 这里在每一页读完时立刻把结果写入检查点文件（按 data-cid 标识），
    # 供下次「继续读取」时跳过已完成的画布，只补缺失的部分。
    @property
    def _ckpt_path(self) -> str:
        return os.path.join(self.shot_dir, '_checkpoint.json')

    def _load_checkpoint(self) -> Dict[str, Any]:
        """读取检查点：{url_key: {'pages': [...], 'done_cids': [...], 'total': int}}"""
        try:
            if os.path.exists(self._ckpt_path):
                with open(self._ckpt_path, 'r', encoding='utf-8') as f:
                    return json.load(f) or {}
        except Exception as e:
            logger.warning(f'读取续读检查点失败（忽略，按全新读取）: {e}')
        return {}

    def _save_checkpoint(self, url_key: str, pages: List[Dict[str, Any]],
                         done_cids: List[str], total: int) -> None:
        """把已完成的画布写入检查点（原子写，避免半截文件）。"""
        try:
            data = self._load_checkpoint()
            # 落盘前剔除不可序列化/体积过大的字段（shot_path 为内部临时字段）
            slim = []
            for p in pages:
                q = {k: v for k, v in p.items() if k != 'shot_path'}
                slim.append(q)
            data[url_key] = {
                'pages': slim,
                'done_cids': list(done_cids),
                'total': int(total),
                'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            tmp = self._ckpt_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._ckpt_path)
        except Exception as e:
            logger.warning(f'写入续读检查点失败（不影响本次读取）: {e}')

    def _get_resume_state(self, url_key: str):
        """返回 (已读画布列表, 已完成 data-cid 集合)；无检查点则 ([], set())。"""
        ck = self._load_checkpoint().get(url_key) or {}
        pages = ck.get('pages') or []
        cids = set(ck.get('done_cids') or [])
        return pages, cids

    @staticmethod
    def _ckpt_key(url: str) -> str:
        """检查点按 URL 归档（去 hash），不同地址互不干扰。"""
        return (url or '').split('#')[0]

    # ---------- 公共入口 ----------
    async def extract(
        self,
        url: str,
        cookies: Optional[str] = None,
        headless: bool = True,
        vision_config=None,
        max_pages: int = 200,
        enumerate_pages: bool = True,
        source_type: str = 'modao',
        dual_fusion: bool = False,
        fusion_config=None,
        ocr_vision_config=None,
        prototype_id=None,
    ) -> Dict[str, Any]:
        """
        逐页提取墨刀原型需求内容。

        Returns:
            {
              'run_id': str,
              'url': str,
              'pages': [ {page_no, page_name, text, annotations:[...], screenshot, extraction_warnings:[...]} ],
              'warnings': [...]
            }
        """
        ocr_vision_config = ocr_vision_config or vision_config
        from playwright.async_api import async_playwright

        warnings: List[str] = []
        pages: List[Dict[str, Any]] = []

        cookie_list = self._parse_cookies(cookies)
        if cookies and not cookie_list:
            warnings.append('cookie 解析失败，将尝试匿名访问（可能遇到登录墙）')

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1200},
                device_scale_factor=4,
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                            '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
            )
            if cookie_list:
                try:
                    await context.add_cookies(cookie_list)
                except Exception as e:
                    warnings.append(f'注入 cookie 失败: {e}')

            page = await context.new_page()
            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
            except Exception as e:
                warnings.append(f'页面加载超时/异常（继续尝试提取）: {e}')
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                except Exception as e2:
                    warnings.append(f'二次加载失败，终止: {e2}')
                    await browser.close()
                    return {'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}
            await page.wait_for_timeout(2000)

            # [密码墙/未加载检测] 墨刀分享链接可能变为「需密码访问」或登录墙，
            # 此时 SPA 不渲染画布/页面树，若静默走空提取会误导用户以为"提取成功但无内容"。
            # 这里主动探测并给出明确 warning，便于前端/用户识别。
            try:
                body_text = (await page.evaluate(
                    "document.body ? document.body.innerText : ''")).strip()
                if '请输入密码访问项目' in body_text or '密码访问' in body_text:
                    warnings.append(
                        '墨刀分享链接已变为「需密码访问」，无法读取原型内容；'
                        '请在墨刀分享设置中关闭密码，或在调用时提供访问密码后重试')
                    await browser.close()
                    return {'run_id': self.run_id, 'url': url,
                            'pages': pages, 'warnings': warnings,
                            'blocked_by_password': True}
                # 通用未加载兜底：body 文本极少且无任何画布/页面节点
                if len(body_text) < 60 and await page.locator(
                        ".rn-list-item.page[data-cid]").count() == 0:
                    warnings.append(
                        f'原型页面 SPA 未正常渲染（body 仅 {len(body_text)} 字），'
                        f'可能需登录/密码或链接已失效')
            except Exception as e:
                warnings.append(f'密码墙检测异常(已忽略): {e}')

            # 枚举页面（多 URL 批量场景由调用方传 enumerate_pages=False，避免重复抓取）
            page_items = await self._enumerate_pages(page, source_type=source_type) if enumerate_pages else []
            if not page_items:
                warnings.append('未识别到页面树，将把当前单页作为唯一页面提取')
                page_items = [None]  # 单页模式

            # 提取每个画布所属文件夹路径（用于按文件夹批量勾选）
            canvas_folders = await self._extract_canvas_folders(page) if page_items else {}

            ocr_jobs: List[Dict[str, Any]] = []
            _tlog_path = os.path.join(self.shot_dir, '_timing.log')
            _nav_t0 = time.time()
            _nav_count = 0
            # ── 断点续读：载入上次检查点，按 data-cid 跳过已完成的画布 ──
            _ckpt_key = self._ckpt_key(url)
            _prev_pages, _done_cids = self._get_resume_state(_ckpt_key)
            _total_pages = len(page_items[:max_pages])
            if _prev_pages:
                # 复用上次已读页：先放进 pages，后续对新页 append
                pages.extend(_prev_pages)
                # cid 也一并补进 done 集合（老检查点可能没记 cid，用页名兜底）
                for _pp in _prev_pages:
                    _c = (_pp.get('item_cid') or '').strip()
                    if _c:
                        _done_cids.add(_c)
                logger.info(f'[续读] 命中检查点：已完成 {len(_prev_pages)}/{_total_pages} 页，'
                            f'将跳过这些画布继续读取剩余部分')
                print(f'[EXTRACT_LOOP] 续读模式：已完成 {len(_prev_pages)}/{_total_pages} 页，'
                      f'跳过已读画布', flush=True)
                _nav_count += len(_prev_pages)
            _new_cids: List[str] = []
            for idx, item in enumerate(page_items[:max_pages], start=1):
                try:
                    _nav_count += 1
                    _pg_t0 = time.time()
                    print(f'[EXTRACT_LOOP] ▶ page {idx} START', flush=True)
                    try:
                        with open(os.path.join(self.shot_dir, '_stage.log'), 'a') as _sf:
                            _sf.write(f'[STAGE][{time.strftime("%H:%M:%S")}] page {idx} START\n')
                    except Exception:
                        pass
                    extraction_warnings: List[str] = []
                    page_name = f'第{idx}页'
                    parent_name = ''
                    _item_cid = ''   # 当前画布的 data-cid（唯一，用于同名画布精确定位）
                    # [修复] 每轮重新枚举页面树：上一轮 _extract_layer_text 内部会整页
                    # goto 切换 view_mode，触发墨刀 SPA 重渲染，使循环前持有的 item element
                    # handle 失效；不重枚举则除首个画布外全部激活失败被跳过（只识别出 1 个画布）。
                    # 每轮用新鲜 handle 即可对该整页重载免疫。
                    try:
                        _fresh_items = await self._enumerate_pages(page, source_type=source_type)
                        if _fresh_items:
                            item = _fresh_items[min(idx, len(_fresh_items)) - 1]
                    except Exception as e:
                        warnings.append(f'第{idx}页重新枚举页面树失败: {e}')
                    if item is not None:
                        # data-cid 全局唯一，用于「同名画布」场景精确定位（见 _extract_layer_text）
                        _item_cid = ''
                        try:
                            _item_cid = (await item.get_attribute('data-cid') or '').strip()
                        except Exception:
                            _item_cid = ''
                        # ── 断点续读：该画布上次已读完 → 直接跳过（不再重复取文本/截图/OCR）──
                        if _item_cid and _item_cid in _done_cids:
                            print(f'[EXTRACT_LOOP] page {idx} SKIP (已读, cid={_item_cid})',
                                  flush=True)
                            try:
                                with open(os.path.join(self.shot_dir, '_stage.log'), 'a') as _sf:
                                    _sf.write(f'[STAGE][{time.strftime("%H:%M:%S")}] '
                                              f'page {idx} SKIP(已读)\n')
                            except Exception:
                                pass
                            continue
                        try:
                            page_name = (await item.inner_text()).strip() or page_name
                            # [父级留空] 按用户约定不再自动识别/拆分父级分组：
                            # 画布名直接保留页面树读取的完整原始名（如「引导-身体数据」），
                            # parent_name 留空，由用户在前端校验界面手动填写（用于区分重名画布）。
                            parent_name = ''
                        except Exception as e:
                            warnings.append(f'第{idx}页读取页名/父级失败: {e}')
                            parent_name = ''
                        # 把"其它画布项"一并传入：函数内部重试时会先点其它画布制造
                        # 状态差再点回目标，解决墨刀「当前已在该画布状态时对相邻/同名项
                        # 点击不重新渲染」导致的切换粘滞与撞图。
                        _others = []
                        try:
                            _others = [x for x in (_fresh_items or []) if x is not item]
                        except Exception:
                            _others = []
                        print(f'[EXTRACT_LOOP] page {idx} activating...', flush=True)
                        switched = await asyncio.wait_for(
                            self._activate_page_item(
                                page, item, other_items=_others, item_cid=_item_cid,
                                on_note=extraction_warnings.append),
                            timeout=120)
                        if not switched:
                            # 点击后画布未变化：可能是纯分组/容器节点，也可能是墨刀渲染延迟
                            # 导致视口指纹尚未变化。不再直接跳过，而是保留该页并标记警告，
                            # 后续用截图 SHA256 重复检测来剔除真正重复的画布；
                            # 这样可以避免把「内容相似的真实画布」误判为分组节点而丢失。
                            logger.warning(
                                f'页面项"{page_name}"点击后画布未变化，仍保留并计入重复检测')
                            extraction_warnings.append(
                                '点击后未能确认画布切换（无 data-cid 且视口差异不足），'
                                '已保留待截图重复检测判定')
                    else:
                        # 单页模式：确保回到原型根视图
                        pass

                    # Modao may show a welcome/login overlay on read-only shares.
                    # Escape closes the transient overlay without changing the canvas.
                    try:
                        await page.keyboard.press('Escape')
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass

                    print(f'[EXTRACT_LOOP] page {idx} extracting text...', flush=True)
                    dom_text = await asyncio.wait_for(
                        self._extract_visible_text(page, retry=dual_fusion), timeout=120)
                    # [墨刀图层树增强] 第三路来源：inspect 模式读文本图层名（无 OCR 幻觉）。
                    # 并入 dom_text，使 OCR 跳过判定与 dual_fusion 的 DOM 路都能受益；
                    # 失败静默回退（OCR 仍兜底）。
                    print(f'[EXTRACT_LOOP] page {idx} extracting layer text...', flush=True)
                    layer_text = await asyncio.wait_for(
                        self._extract_layer_text(
                            page, item, warnings, source_type=source_type,
                            page_name=page_name, item_cid=_item_cid),
                        timeout=120)
                    if layer_text:
                        extraction_warnings.append(f'图层树提取到 {len(layer_text)} 字符文本')
                        dom_text = ((dom_text + '\n[图层树文本]\n' + layer_text).strip()
                                   if dom_text else layer_text)
                    print(f'[EXTRACT_LOOP] page {idx} extracting annotations...', flush=True)
                    annotations = await asyncio.wait_for(
                        self._extract_annotations(page, warnings), timeout=120)
                    # [撞图修复] _extract_layer_text 内部用 page.goto 整页重载切回 read_only，
                    # 重载后画布状态不稳定：可能回落到错误画布（→ 与前一页截图字节相同，
                    # 表现为「撞图」），也可能主画布 img 尚未渲染（→ ROI 取不到走全页兜底）。
                    # 因此截图前按 data-cid 精确重新激活目标画布，并等待主画布渲染就绪，
                    # 确保截到的确实是当前 idx 对应的画布。
                    if source_type == 'modao' and _item_cid:
                        try:
                            _fi = await self._enumerate_pages(page, source_type=source_type)
                            for f in _fi:
                                try:
                                    if (await f.get_attribute('data-cid')) == _item_cid:
                                        await f.scroll_into_view_if_needed()
                                        await f.click(force=True, timeout=5000)
                                        break
                                except Exception:
                                    continue
                            # 等主画布 img 出现，保证 ROI 能取到，避免退化为 full_page 兜底
                            try:
                                await page.wait_for_selector(
                                    '#screens img, [class*="ScreenContainer"] img',
                                    timeout=8000, state='visible')
                            except Exception:
                                pass
                            await page.wait_for_timeout(1200)
                        except Exception:
                            pass
                    shot_path = os.path.join(self.shot_dir, f'page_{idx}.jpg')
                    shot_segments: List[str] = []
                    try:
                        # 精准截图：裁剪到设备画布(rResCanvas) + 周边缓冲，提升画布内图片/插画 OCR 分辨率；
                        # 超长/超宽画布自动拆成多段截图，返回 (主图路径, 分段图路径列表，含主图)。
                        print(f'[EXTRACT_LOOP] page {idx} screenshotting...', flush=True)
                        shot_path, shot_segments = await asyncio.wait_for(
                            self._screenshot_canvas_roi(page, shot_path), timeout=180)
                    except Exception as e:
                        try:
                            await page.screenshot(path=shot_path, full_page=True, type='jpeg', quality=85)
                            shot_segments = [shot_path]
                        except Exception:
                            shot_path = ''
                            shot_segments = []
                            warnings.append(f'第{idx}页截图失败: {e}')

                    # 视觉 OCR 补充（按页面情况自动切换）：
                    # - 墨刀(modao)：画布用 canvas/SVG 渲染，文字/图片/表格根本不进入 DOM 文本，
                    #   也无法用 <img>/<table> 检测命中，原『文本少 / has_visual』判定对其完全失效。
                    #   因此墨刀页面只要有截图，就默认做一次视觉 OCR，把画布里的全部需求内容
                    #   （文字段落 + 图片 + 表格）都识别出来，与左侧导航 DOM 文本合并。
                    # - 其他源(webshare 等)：沿用『文本<30字 或 页面含图片/表格』判定，
                    #   纯文本无图无表的页面不触发，避免无谓的视觉模型开销。
                    # 性能优化：OCR 不在循环内串行调用，改为收集任务、循环结束后并发批量识别
                    # （见 _run_ocr_batch），把 N 次串行视觉模型调用压成 ≈ N/OCR_CONCURRENCY 并行调用。
                    if source_type == 'modao':
                        # 自适应 OCR 分类器：根据 DOM 特征判断是否需要视觉 OCR
                        need_vision = bool(shot_path and os.path.exists(shot_path))
                        if need_vision:
                            dom_len = len((dom_text or '').strip())
                            has_img = await page.locator('img').count() > 2
                            has_canvas = await page.locator('canvas').count() > 0
                            has_svg = await page.locator('svg').count() > 10
                            visual_heavy = has_img or has_canvas or has_svg

                            if dom_len >= OCR_SKIP_DOM_TEXT_RICH:
                                need_vision = False
                                extraction_warnings.append(
                                    f'DOM文字充足({dom_len}字)，跳过视觉OCR')
                            elif dom_len >= OCR_SKIP_DOM_TEXT_MIN and not visual_heavy:
                                need_vision = False
                                extraction_warnings.append(
                                    f'DOM文字足够({dom_len}字)且无大量视觉元素，跳过视觉OCR')
                            # 其他情况：文字少或有大量 svg/canvas → 保持 need_vision=True
                            # 跳过 OCR 时也标记为 vision_attempted=True，避免覆盖率检查误报
                            if not need_vision:
                                vision_skipped_by_classifier = True
                            else:
                                vision_skipped_by_classifier = False
                    else:
                        has_visual = await self._page_has_visual(page)
                        need_vision = len((dom_text or '').strip()) < 30 or has_visual
                    vision_attempted = bool(
                        vision_config and shot_path and os.path.exists(shot_path) and need_vision
                    )
                    # 分类器主动跳过 OCR 时也标记为已尝试+已成功（DOM文字足够，无需视觉识别）
                    if not need_vision and shot_path and os.path.exists(shot_path):
                        vision_attempted = True
                        vision_skipped_by_classifier = True

                    print(f'[EXTRACT_LOOP] ✓ page {idx} DONE ({time.time()-_pg_t0:.1f}s)', flush=True)
                    try:
                        with open(os.path.join(self.shot_dir, '_stage.log'), 'a') as _sf:
                            _sf.write(f'[STAGE][{time.strftime("%H:%M:%S")}] page {idx} DONE\n')
                    except Exception:
                        pass
                    print(f'[EXTRACT_LOOP] ✓ page {idx} DONE ({time.time()-_pg_t0:.1f}s)', flush=True)
                    try:
                        with open(os.path.join(self.shot_dir, '_stage.log'), 'a') as _sf:
                            _sf.write(f'[STAGE][{time.strftime("%H:%M:%S")}] page {idx} DONE\n')
                    except Exception:
                        pass
                    pages.append({
                        'page_no': idx,
                        'page_name': page_name,
                        'parent_name': parent_name,
                        'folder': canvas_folders.get(_item_cid, ''),
                        'item_cid': _item_cid,   # 续读锚点：data-cid 全局唯一
                        'text': dom_text,
                        'dom_text': dom_text,
                        'layer_text': layer_text,
                        'vision_text': '',
                        'dom_text_len': len(dom_text or ''),
                        'vision_attempted': vision_attempted,
                        'vision_success': False,
                        'shot_path': shot_path,
                        'annotations': annotations,
                        'screenshot': os.path.relpath(shot_path, self.media_root) if shot_path else '',
                        'screenshot_segments': (
                            [os.path.relpath(sp, self.media_root) for sp in shot_segments]
                            if shot_segments else []
                        ),
                        'visual_summary': '',
                        'visual_artifacts': [],
                        'extraction_warnings': extraction_warnings,
                    })
                    if vision_attempted:
                        ocr_jobs.append(pages[-1])
                    # ── 逐页写检查点：中途超时/OOM/重启时保住已读画布，下次可续读 ──
                    if _item_cid:
                        _new_cids.append(_item_cid)
                    self._save_checkpoint(
                        _ckpt_key, pages, list(_done_cids) + _new_cids, _total_pages)
                    # 手动终止检查点：用户在前端点「终止」后退出逐页读取
                    _raise_if_terminated(prototype_id)
                except Exception as e:
                    warnings.append(f'第{idx}页提取异常: {e}')
                    print(f'[EXTRACT_LOOP] ✓ page {idx} DONE ({time.time()-_pg_t0:.1f}s)', flush=True)
                    try:
                        with open(os.path.join(self.shot_dir, '_stage.log'), 'a') as _sf:
                            _sf.write(f'[STAGE][{time.strftime("%H:%M:%S")}] page {idx} DONE\n')
                    except Exception:
                        pass
                    pages.append({
                        'page_no': idx,
                        'page_name': f'第{idx}页',
                        'parent_name': '',
                        'text': '',
                        'annotations': [],
                        'screenshot': '',
                        'extraction_warnings': [str(e)],
                        'visual_summary': '',
                        'visual_artifacts': [],
                    })

            # 并发批量视觉 OCR（核心提速点）：把每页串行的视觉模型调用改为限流并发，
            # 整体耗时 ≈ 单页 OCR 耗时 × (页数 / OCR_CONCURRENCY)。
            _nav_elapsed = time.time() - _nav_t0
            with open(_tlog_path, 'a') as _tf:
                _tf.write(f'[TIMING] 导航+截图循环: {_nav_count}页 耗时 {_nav_elapsed:.1f}s ({(_nav_elapsed/max(_nav_count,1)):.2f}s/页)\n')
            _debug_path = os.path.join(self.shot_dir, '_ocr_debug.log')
            _ocr_cfg_name = getattr(ocr_vision_config, 'model_name', '?') if ocr_vision_config else None
            _dbg = f'[EXTRACT] ocr_jobs={len(ocr_jobs)} ocr_model={_ocr_cfg_name} type(ocr)={type(ocr_vision_config).__name__}\n'
            print(_dbg, flush=True)
            with open(_debug_path, 'a') as _df: _df.write(_dbg)
            _ocr_t0 = time.time()
            if ocr_jobs and ocr_vision_config:
                await self._run_ocr_batch(ocr_jobs, ocr_vision_config, warnings, prototype_id)
            else:
                _dbg2 = f'[EXTRACT] OCR SKIPPED! jobs={len(ocr_jobs)} config={ocr_vision_config}\n'
                print(_dbg2, flush=True)
                with open(_debug_path, 'a') as _df: _df.write(_dbg2)
            _ocr_elapsed = time.time() - _ocr_t0
            with open(_tlog_path, 'a') as _tf:
                _tf.write(f'[TIMING] 视觉OCR并发批次: {len(ocr_jobs)}页 耗时 {_ocr_elapsed:.1f}s ({(_ocr_elapsed/max(len(ocr_jobs),1)):.2f}s/页)\n')
            # 清理内部临时字段（_run_ocr_batch 已对参与 OCR 的页面 pop，这里兜底全部页面）
            for p in pages:
                p.pop('shot_path', None)

            duplicate_groups = self._mark_duplicate_screenshots(pages)
            if duplicate_groups:
                duplicate_count = sum(len(group) - 1 for group in duplicate_groups)
                warnings.append(
                    f'检测到 {duplicate_count} 个重复画布截图，重复页不应视为读取成功：'
                    + '；'.join(','.join(str(no) for no in group) for group in duplicate_groups)
                )
                # ⚠️ 2026-11-04：重复截图必须阻断，否则"点击画布未真正切换"会被静默当成成功。
                # 实测 proto=93（单文件 HTML 报告页）：19 个画布只截出 8 张不同图，
                # 两组分别 5 个 / 8 个画布共用同一张截图（重复率 58%），但此前只写 warning，
                # 导致每个画布的 visual_artifacts 都混入同一份内容 → 阶段1 逐画布结构化时
                # 搜索模块需求被注入每个画布、每个画布产出同一批无关测试点。
                # 判据与 CODEBUDDY.md 约定一致：重复率 > 20% 即判定阶段0 失败。
                _dup_ratio = duplicate_count / float(len(pages) or 1)
                if _dup_ratio > 0.2:
                    warnings.append(
                        f'画布重复截图比例 {_dup_ratio:.0%} 超过 20% 阈值：'
                        f'点击画布可能未真正切换（{duplicate_count}/{len(pages)} 页重复），'
                        f'本次读取结果不可信，请检查原型页面结构或改用逐页链接'
                    )
                    logger.error(
                        '[EXTRACT] 重复截图比例 %.0f%%，阶段0 判定失败（%d/%d 页重复）',
                        _dup_ratio * 100, duplicate_count, len(pages))
                    result_blocked = {
                        'run_id': self.run_id, 'url': url, 'pages': pages,
                        'warnings': warnings, 'duplicate_blocked': True,
                        'duplicate_ratio': _dup_ratio,
                    }
                    if dual_fusion:
                        result_blocked['dual_fusion'] = True
                        result_blocked['fused_requirements'] = []
                    return result_blocked

            await browser.close()

        # 可选增强策略：双路 DOM + 视觉 交叉融合（仅 dual_fusion=True 且提供 fusion_config 时触发）
        fused_requirements: List[Dict[str, Any]] = []
        if dual_fusion and fusion_config:
            try:
                fused_requirements = await self.dual_fusion_requirements(pages, fusion_config, prototype_id)
            except Exception as e:
                logger.warning(f'双路融合失败（已跳过，保留分路径数据）: {e}')
                warnings.append(f'双路融合失败（已跳过）: {e}')

        result: Dict[str, Any] = {
            'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}
        if dual_fusion:
            result['dual_fusion'] = True
            result['fused_requirements'] = fused_requirements
        return result

    @staticmethod
    def _is_axure_url(url: str, cookies: Optional[str] = None) -> bool:
        """快速探测 URL 是否为 Axure/webshare 原型：尝试抓取 data/document.js 并解析页面树。
        命中则返回 True（说明该地址是 webshare 目录，应走多页 HTTP 解析而非单页浏览器抓取）。"""
        import requests
        try:
            base = url.split('#')[0]
            if not base.endswith('/'):
                last_seg = base.rsplit('/', 1)[-1]
                base = base.rsplit('/', 1)[0] + '/' if '.' in last_seg else base + '/'
            doc_url = urljoin(base, _AXURE_DOC_URL)
            resp = requests.get(doc_url, cookies=ModaoExtractor._cookie_dict(cookies),
                                timeout=15, headers=_UA_HEADERS)
            if resp.status_code != 200:
                return False
            if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                resp.encoding = 'utf-8'
            return bool(parse_axure_document(resp.text))
        except Exception:
            return False

    def extract_sync(self, *args, **kwargs) -> Dict[str, Any]:
        """同步包装（供 Django 视图在线程中调用）。
        webshare 走纯 HTTP 解析路径；墨刀来源若检测到实际是 Axure/webshare 原型，
        也自动路由到 webshare 多页解析（避免把 webshare 地址误粘到墨刀框时只读出单页）。"""
        kwargs.setdefault('dual_fusion', False)
        kwargs.setdefault('fusion_config', None)
        source_type = kwargs.get('source_type', 'modao')
        url = kwargs.get('url')
        # 墨刀 sharing 链接若带 screen= 单画布参数，会锁定到单个画布、页面树不展开，
        # 导致只读到 1 页而非完整原型的全部画布。剥离该参数，加载显示完整页面树的根视图，
        # 使 _enumerate_pages 能自动发现目录下所有子页面。
        if url and 'modao.cc' in url and 'screen=' in url:
            try:
                from urllib.parse import urlparse, parse_qs, urlencode
                _pu = urlparse(url)
                _qs = parse_qs(_pu.query, keep_blank_values=True)
                _qs.pop('screen', None)
                url = _pu._replace(query=urlencode(_qs, doseq=True)).geturl()
                kwargs['url'] = url
            except Exception:
                pass
        # 自动识别：墨刀来源但 URL 实为 Axure/webshare 原型时，自动走多页解析
        if source_type != 'webshare' and url:
            import logging
            _logger = logging.getLogger('modao')
            _logger.info(f'[AUTO-DETECT] source_type={source_type}, url={url[:120]}')
            try:
                is_axure = self._is_axure_url(url, kwargs.get('cookies'))
                _logger.info(f'[AUTO-DETECT] _is_axure_url returned {is_axure}')
                if is_axure:
                    source_type = 'webshare'
                    _logger.info('[AUTO-DETECT] -> routing to webshare path')
                elif self._detect_singlefile_h5(url, kwargs.get('cookies')):
                    # 非 Axure 目录但内联 pages 数组（单文件 H5 交互原型）→ 也走 webshare 纯 HTTP 路径
                    source_type = 'webshare'
                    _logger.info('[AUTO-DETECT] -> routing to singlefile-H5 webshare path')
                elif self._is_prd_report_url(url, kwargs.get('cookies')):
                    # 自研 PRD 报告页（requirement-module + prototype-item，无 pages 数组）
                    # → 同样走 webshare 纯 HTTP 路径，由 extract_webshare_sync 内部再分流
                    source_type = 'webshare'
                    _logger.info('[AUTO-DETECT] -> routing to PRD-report webshare path')
                elif (self._resolve_single_html_in_dir(
                        url, self._cookie_dict(kwargs.get('cookies')))[0]
                        or self._guess_dir_file_from_history(url)):
                    # 目录地址且目录内只有一个单文件 HTML（新版导出常见）→ 走 webshare 纯 HTTP 路径。
                    # 目录浏览被关闭时用历史记录里的直链兜底判断，避免误落到 Playwright 路径。
                    source_type = 'webshare'
                    _logger.info('[AUTO-DETECT] -> routing to dir-single-html webshare path')
            except Exception as detect_err:
                _logger.warning(f'[AUTO-DETECT] _is_axure_url raised {detect_err}')
        else:
            import logging
            logging.getLogger('modao').info(f'[AUTO-DETECT] skipped: source_type={source_type}, url={bool(url)}')
        if source_type == 'webshare':
            # webshare / Axure 原型：解析 data/document.js 拿页面树，逐个 HTTP 抓取页面 HTML，
            # 不需要启动浏览器，更稳更快，且一次自动发现目录下所有子页面。
            import logging
            logging.getLogger('modao').info(f'[extract_sync] calling extract_webshare_sync(url={url[:100]})')
            return self.extract_webshare_sync(
                url=url,
                cookies=kwargs.get('cookies'),
                vision_config=kwargs.get('vision_config'),
                dual_fusion=kwargs.get('dual_fusion'),
                fusion_config=kwargs.get('fusion_config'),
                target_page_names=kwargs.get('target_page_names'),
            )
        import logging
        logging.getLogger('modao').info(f'[extract_sync] falling through to Playwright path, source_type={source_type}')
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        # Playwright 路径的 extract() 不支持 target_page_names（该参数为 webshare 分支专用，
        # 单画布重读由工作流在落库阶段按名过滤，无需 extractor 感知），透传会触发 TypeError。
        kwargs.pop('target_page_names', None)
        # 超时兜底：浏览器/页面加载卡死时避免整阶段永久 hang（历史 bug：extract 无整体超时，
        # 卡死后 daemon 线程永久占用资源、拖垮容器，且前端无失败日志）。
        # ⚠️ 超时**不再直接抛错丢掉全部结果**：逐页读取过程中已把每页写入检查点
        # （_checkpoint.json），这里把检查点里的已读画布捞回来，按「部分成功」返回，
        # 让上层能落库已读部分并提示用户「继续读取」补齐剩余画布。
        # 阈值可用环境变量 MODAO_EXTRACT_TIMEOUT 覆盖（默认 1800s），便于演练/特殊环境调整。
        try:
            _timeout = int(os.environ.get('MODAO_EXTRACT_TIMEOUT') or 1800)
        except Exception:
            _timeout = 1800
        _url_for_ck = kwargs.get('url') or ''
        try:
            return loop.run_until_complete(
                asyncio.wait_for(self.extract(*args, **kwargs), timeout=_timeout)
            )
        except asyncio.TimeoutError:
            _partial, _ = self._get_resume_state(self._ckpt_key(_url_for_ck))
            _n = len(_partial or [])
            _msg = (f'墨刀原型浏览器读取超时（>{_timeout}s/{_timeout // 60}分钟）。'
                    f'已保留超时前读取到的 {_n} 个画布，可点击「继续读取」补齐剩余画布。'
                    f'若反复超时，请检查页面是否卡死或容器资源（/dev/shm、内存）是否不足。')
            logger.warning(f'[读取超时] {_msg}')
            if _n:
                # 有部分结果 → 交给上层按「部分成功」处理（不抛异常，避免进度全丢）
                return {
                    'run_id': self.run_id,
                    'url': _url_for_ck,
                    'pages': _partial,
                    'warnings': [_msg],
                    'partial': True,
                }
            raise TimeoutError(_msg)

    # ---------- webshare / Axure：纯 HTTP 解析（无需浏览器）----------
    @staticmethod
    def _cookie_dict(cookies: Optional[str]) -> Dict[str, str]:
        """把 cookie 字符串（JSON 列表 / JSON 对象 / name=value;...）转成 requests 用的 dict。"""
        if not cookies:
            return {}
        try:
            data = json.loads(cookies)
            if isinstance(data, list):
                return {c.get('name'): c.get('value') for c in data if isinstance(c, dict)}
            if isinstance(data, dict):
                if isinstance(data.get('cookies'), list):
                    return {c.get('name'): c.get('value') for c in data['cookies'] if isinstance(c, dict)}
                return {k: v for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
        d: Dict[str, str] = {}
        for part in cookies.split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                d[k.strip()] = v.strip()
        return d

    def _webshare_fallback_pure_http(self, url: str, cookie_dict, warnings: List[str],
                                     pages: List[Dict[str, Any]], vision_config=None,
                                     dual_fusion: bool = False, fusion_config=None,
                                     target_page_names=None) -> Dict[str, Any]:
        """非 Axure 目录时的纯 HTTP 回退链：单文件 H5 原型 → PRD 报告页。

        两者都不命中则原样返回空 pages（调用方会走 Playwright 路径或报读取失败）。
        注意顺序：单文件 H5 判定更严格（必须有内联 pages 数组），放前面不会误吞 PRD 文档。
        """
        h5 = self._extract_singlefile_h5(url, cookie_dict, vision_config, dual_fusion,
                                         fusion_config, target_page_names=target_page_names)
        if h5.get('pages'):
            h5['warnings'] = warnings + h5.get('warnings', [])
            return h5
        # PRD 报告页（自研文档格式，第 4 种来源）：纯 HTTP、不做视觉 OCR。
        # dual_fusion / fusion_config 必须透传：该格式没有视觉路，但阶段0 在
        # dual_fusion=True 时会校验「融合需求是否为空」，不透传就必然阻断
        # （这是"格式支持了却永远读不出来"的根因）。
        prd = self.extract_prd_report_sync(url, None,
                                           dual_fusion=dual_fusion,
                                           fusion_config=fusion_config)
        if prd.get('pages'):
            prd['warnings'] = warnings + prd.get('warnings', [])
            logger.info(f'[webshare回退] 识别为 PRD 报告页，共 {len(prd["pages"])} 页')
            return prd
        # 全部纯 HTTP 路径都没读出内容：给一条**说明已尝试过哪些格式**的提示，
        # 便于用户判断下一步（是改填文件直链、还是内容确实为空）。
        _tail = url.rstrip('/')
        warnings.append(
            '未能识别该地址的内容格式（已依次尝试：Axure 多页目录 / 单文件 H5 交互原型 / '
            f'PRD 报告页）。请确认 {_tail} 是原型或需求文档地址；'
            f'若只知道目录，请改为填写目录内具体 HTML 文件的完整地址')
        return {'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}

    def extract_webshare_sync(self, url: str, cookies: Optional[str] = None, timeout: int = 30,
                              vision_config=None, dual_fusion: bool = False,
                              fusion_config=None, target_page_names=None) -> Dict[str, Any]:
        """webshare / Axure 原型读取：解析 data/document.js 拿页面树，逐个 HTTP 抓取页面 HTML 提取文本。

        返回结构与 extract() 一致：{'run_id','url','pages':[{page_no,page_name,text,annotations,screenshot,extraction_warnings}],'warnings'}
        """
        import requests
        warnings: List[str] = []
        pages: List[Dict[str, Any]] = []
        cookie_dict = self._cookie_dict(cookies)

        # 规范化目录地址：去掉 hash 片段，补尾部斜杠（兼容直接粘贴某页面 .html 链接）
        base = url.split('#')[0]
        if not base.endswith('/'):
            last_seg = base.rsplit('/', 1)[-1]
            if '.' in last_seg:
                base = base.rsplit('/', 1)[0] + '/'
            else:
                base = base + '/'

        # ── 目录地址解析：用户粘的常是「目录」，需要找出目录内的单文件 HTML ──
        # 新版导出不再生成 Axure 的 data/document.js（实测返回 404），
        # 因此先尝试把目录地址解析成目录内那个唯一的 .html 直链，
        # 再按「单文件 H5 原型 / PRD 报告页」解析；解析不到才回落到 Axure 流程。
        _raw_url = url.split('#')[0]
        _is_dir_like = _raw_url.endswith('/') or '.' not in _raw_url.rsplit('/', 1)[-1]
        if _is_dir_like:
            _resolved, _reason = self._resolve_single_html_in_dir(
                base, cookie_dict, timeout=timeout)
            if not _resolved:
                # 兜底 1：从历史成功记录里取同目录下的文件直链
                _hist = self._guess_dir_file_from_history(base)
                if _hist:
                    _resolved, _reason = _hist, ''
                    warnings.append(f'目录浏览不可用，已复用历史记录中的文件直链：{_hist}')
            if _resolved:
                logger.info(f'[目录解析] {base} -> {_resolved}')
                _fb = self._webshare_fallback_pure_http(
                    _resolved, cookie_dict, list(warnings), pages, vision_config,
                    dual_fusion, fusion_config, target_page_names)
                if _fb.get('pages'):
                    _fb['warnings'] = [f'已从目录地址定位到单文件：{_resolved}'] + (
                        _fb.get('warnings') or [])
                    return _fb
                warnings.append(
                    f'目录 {base} 内定位到 {_resolved}，但未能识别其页面结构')
            elif _reason:
                # 目录解析失败：给出**准确可操作**的提示，不要落到下面那条
                # 「data/document.js」的通用兜底文案上（在目录浏览关闭的场景下完全误导）。
                warnings.append(self._dir_fail_hint(base, _reason))

        try:
            doc_url = urljoin(base, _AXURE_DOC_URL)
            resp = requests.get(doc_url, cookies=cookie_dict, timeout=timeout, headers=_UA_HEADERS)
            if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                resp.encoding = 'utf-8'
            doc_js = resp.text
        except Exception as e:
            # Axure 路径不通（新版导出已无 data/document.js）。这里**不再输出
            # "请确认链接为 Axure/webshare 原型目录" 这类误导文案**——
            # 该场景绝大多数是「新版单文件导出」，由下方回退链正常处理即可。
            logger.info(f'[webshare] 获取 {doc_url} 失败（将回退单文件/PRD 解析）: {e}')
            return self._webshare_fallback_pure_http(
                url, cookie_dict, warnings, pages, vision_config, dual_fusion,
                fusion_config, target_page_names)

        axure_pages = parse_axure_document(doc_js)
        if not axure_pages:
            # 同上：不发误导文案，交给回退链（单文件 H5 / PRD 报告页）
            logger.info('[webshare] data/document.js 无有效页面树，回退单文件/PRD 解析')
            return self._webshare_fallback_pure_http(
                url, cookie_dict, warnings, pages, vision_config, dual_fusion,
                fusion_config, target_page_names)

        for idx, ap in enumerate(axure_pages, 1):
            try:
                page_url = urljoin(base, quote(ap['url'], safe=''))
                r = requests.get(page_url, cookies=cookie_dict, timeout=timeout, headers=_UA_HEADERS)
                if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'ascii'):
                    r.encoding = 'utf-8'
                dom_text = extract_axure_text(r.text)
                screenshot_path = ''
                vision_result = {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
                vision_text = ''
                vision_attempted = False
                # 视觉 OCR 补充（自动切换）。触发条件满足其一：
                #   1) 文本极少(<30字) → 纯图片/截图/表格页；
                #   2) HTML 含 <img> / <table> / 背景图 → 混合页（文字+图片+表格），
                #      需视觉补充图片/表格内容。纯文本页不受影响。
                if vision_config and (
                    dual_fusion or len(dom_text.strip()) < 30 or self._html_has_visual(r.text)
                ):
                    vision_attempted = True
                    try:
                        vision_result = asyncio.run(
                            self._ocr_webshare_page_async(page_url, cookie_dict, vision_config, idx)
                        )
                    except Exception as e:
                        vision_result = {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
                        logger.warning(f'webshare 页面[{ap.get("pageName")}]视觉OCR失败: {e}')
                    vision_text = vision_result.get('ocr_text', '')
                vision_success = bool(
                    vision_text
                    or vision_result.get('visual_summary')
                    or vision_result.get('visual_artifacts')
                )
                # 兼容字段：DOM 文本 + 视觉 OCR 合并，维持 run_struct 既有消费方式
                text = (dom_text or '')
                if vision_text:
                    text = text + '\n[视觉OCR补充]\n' + vision_text
                rendered_shot = os.path.join(self.shot_dir, f'ws_page_{idx}.png')
                if os.path.exists(rendered_shot):
                    screenshot_path = os.path.relpath(rendered_shot, self.media_root)
                page_vision_text = vision_text or ''
                if vision_result.get('visual_summary'):
                    page_vision_text = (page_vision_text + '\n' + vision_result['visual_summary']).strip()
                pages.append({
                    'page_no': idx,
                    'page_name': ap.get('pageName') or f'第{idx}页',
                    'text': text,
                    'dom_text': dom_text,
                    'vision_text': page_vision_text,
                    'dom_text_len': len(dom_text or ''),
                    'vision_attempted': vision_attempted,
                    'vision_success': vision_success,
                    'annotations': [],
                    'screenshot': screenshot_path,
                    'visual_summary': vision_result.get('visual_summary', ''),
                    'visual_artifacts': vision_result.get('visual_artifacts', []),
                    'extraction_warnings': (
                        ['视觉识别未返回有效内容']
                        if vision_attempted and not vision_success else []
                    ),
                })
            except Exception as e:
                warnings.append(f"页面[{ap.get('pageName')}]抓取失败: {e}")
                pages.append({
                    'page_no': idx,
                    'page_name': ap.get('pageName') or f'第{idx}页',
                    'text': '',
                    'annotations': [],
                    'screenshot': '',
                    'visual_summary': '',
                    'visual_artifacts': [],
                    'extraction_warnings': [str(e)],
                })

        # webshare/Axure：多个页面可能共用同一份导航/页头/页脚，
        # 这些公共元素无独立信息价值 → 从每个画布文本中剔除。
        # 策略：统计每行在多少画布出现，出现在绝大多数(>=80% 且 >=2)画布的行视为全局公共资源并删除。
        if len(pages) >= 3:
            from collections import Counter
            line_counter: Counter = Counter()
            for pg in pages:
                seen = set()
                for ln in (pg.get('text') or '').splitlines():
                    s = ln.strip()
                    if s:
                        seen.add(s)
                for s in seen:
                    line_counter[s] += 1
            threshold = max(2, int(0.8 * len(pages)))
            global_lines = {ln for ln, c in line_counter.items() if c >= threshold}
            if global_lines:
                for pg in pages:
                    kept = [ln for ln in (pg.get('text') or '').splitlines()
                            if ln.strip() not in global_lines]
                    pg['text'] = '\n'.join(kept).strip()
                    kept_dom = [ln for ln in (pg.get('dom_text') or '').splitlines()
                                if ln.strip() not in global_lines]
                    pg['dom_text'] = '\n'.join(kept_dom).strip()
                    pg['dom_text_len'] = len(pg['dom_text'] or '')



        fused_requirements: List[Dict[str, Any]] = []
        if dual_fusion and fusion_config:
            try:
                fused_requirements = asyncio.run(self.dual_fusion_requirements(pages, fusion_config))
            except Exception as e:
                logger.warning(f'webshare 双路融合失败（已跳过）: {e}')
                warnings.append(f'双路融合失败（已跳过）: {e}')
        result: Dict[str, Any] = {
            'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}
        if dual_fusion:
            result['dual_fusion'] = True
            result['fused_requirements'] = fused_requirements
        return result

    # ---------- 单文件 H5 交互原型（自研格式）：纯 HTTP 解析 ----------
    @staticmethod
    def _parse_singlefile_h5(html_text: str):
        """解析单文件 H5 原型的内联 JS，返回 (pages_meta, templates)。

        pages_meta: [{id, group, title, states}]（页面树，group 可作父级分组）；
        templates:  {页面id: HTML模板字符串}。非该格式返回 (None, None)。
        """
        if not html_text:
            return None, None
        pages_meta: List[Dict[str, Any]] = []
        for script in _SCRIPT_RE.findall(html_text):
            # 1) pages 数组（含 id/group/title/states 的扁平对象列表）
            m = _SINGLEFILE_H5_PAGES_RE.search(script)
            if m:
                arr_text = _extract_js_balanced(script, m.start(1))
                if arr_text:
                    for im in _SINGLEFILE_H5_ITEM_RE.finditer(arr_text):
                        item = im.group(1)
                        fields: Dict[str, Any] = {}
                        for fm in _SINGLEFILE_H5_FIELD_RE.finditer(item):
                            key = fm.group(1)
                            if fm.group(3) is not None:
                                fields[key] = fm.group(3)
                            elif fm.group(4):
                                fields[key] = [s.strip().strip('\'"')
                                               for s in fm.group(4)[1:-1].split(',')]
                        if fields.get('id'):
                            pages_meta.append(fields)
        if not pages_meta:
            return None, None
        ids = {m.get('id') for m in pages_meta if m.get('id')}

        # 2) 页面模板 JSON 对象（键=页面 id，值=HTML 字符串）。可能存在多种形态：
        #    整个 <script> 就是 JSON 对象（无 const 前缀），或 const xxx = {...}。
        #    逐个候选尝试 json.loads，取「key 命中页面 id 最多」的对象作为模板源，
        #    避免把 block 里其他数据对象（spec/状态等，如本原型 294 个 key）误当模板。
        best_obj: Dict[str, Any] = {}
        best_hit = 0
        for script in _SCRIPT_RE.findall(html_text):
            cands = [script.strip().rstrip(';').strip()]
            for tm in _SINGLEFILE_H5_TPL_RE.finditer(script):
                obj_text = _extract_js_balanced(script, tm.start(1), '{', '}')
                if obj_text:
                    cands.append(obj_text)
            for cand in cands:
                try:
                    obj = json.loads(cand)
                except Exception:
                    continue
                if not isinstance(obj, dict) or not obj:
                    continue
                hit = len(ids & set(obj.keys()))
                if hit > best_hit:
                    best_obj, best_hit = obj, hit
        templates = {k: v for k, v in best_obj.items()
                     if isinstance(v, str) and k in ids}
        return pages_meta, templates

    # 单文件 H5 探测器能接受的最大体积：正常交互原型都是百 KB 级。
    # 超过此值基本是「PRD 报告页」这类内嵌大量 base64 截图的文档，读全文只会白白超时
    # （实测 124MB 文件会耗尽 15s 超时后返回 False，浪费 7.5s）。
    SINGLEFILE_H5_MAX_BYTES = 20 * 1024 * 1024

    @staticmethod
    def _detect_singlefile_h5(url: str, cookies: Optional[str] = None) -> bool:
        """快速探测 URL 是否为单文件 H5 交互原型（内联 pages 数组且 >=2 页）。

        先看 Content-Length：超大文件直接判定「非单文件 H5 原型」，
        避免为 100MB+ 文档空等 15s 超时。
        """
        import requests
        try:
            resp = requests.get(url, cookies=ModaoExtractor._cookie_dict(cookies),
                                timeout=15, headers=_UA_HEADERS, stream=True)
            if resp.status_code != 200:
                return False
            try:
                length = int(resp.headers.get('Content-Length') or 0)
            except Exception:
                length = 0
            if length > ModaoExtractor.SINGLEFILE_H5_MAX_BYTES:
                logger.info(
                    f'[单文件H5探测] 跳过：Content-Length={length} 超过 '
                    f'{ModaoExtractor.SINGLEFILE_H5_MAX_BYTES} 上限（疑似 PRD 报告页）')
                return False
            if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                resp.encoding = 'utf-8'
            html_text = resp.text
            meta, _tpl = ModaoExtractor._parse_singlefile_h5(html_text)
            return bool(meta and len(meta) >= 2)
        except Exception:
            return False

    # ---------- 目录地址 → 目录内单文件 HTML 解析 ----------
    # WebShare/静态文件服务器（IIS/Apache/nginx autoindex）的「目录地址」会返回一个
    # 文件列表页。用户习惯粘目录地址（原型导出目录下通常只有一个 .html），
    # 而 Axure 的 data/document.js 在新版导出里不存在（已改为单文件 HTML），
    # 因此必须能从目录页里找出那个 .html 并改用其直链。
    _DIR_INDEX_HREF_RE = re.compile(r'HREF="([^"]+\.html?)"', re.I)
    _DIR_INDEX_HREF_RE2 = re.compile(r'href="([^"]+\.html?)"')

    @classmethod
    def _resolve_single_html_in_dir(cls, base_url: str, cookie_dict=None,
                                    timeout: int = 20):
        """解析「目录地址」→ 目录内单文件 HTML 直链。

        返回 (直链 or None, 原因码)。原因码用于给用户**准确可操作**的提示：
          ''            —— 解析成功
          'not_dir'     —— 传进来的已经是文件直链，无需解析
          'no_browsing' —— **目录浏览被关闭**：目录 URL 返回 404/403，但这类服务器
                           通常仍可直接访问具体文件。这是最常见的失败原因
                           （实测 IIS 关闭 Directory Browsing 时，同目录下
                            `xxx.html` 直链 200、而目录 URL 稳定 404）。
          'http_<code>' —— 其它 HTTP 错误（如 401/500）
          'network'     —— 网络异常
          'not_index'   —— 拿到了 200 但不是目录索引页（可能是 SPA 首页）
          'no_html'     —— 索引页里没有 .html 链接
          'ambiguous'   —— 有多个 html 且无法唯一确定

        判定「是目录索引页」：响应体含 `[To Parent Directory]`（IIS）或 <pre> + 文件链接。
        """
        import requests
        target = base_url
        # 墨刀分享链接（modao.cc / 带 view_mode/screen 查询串）**天然不是文件目录**，
        # 直接判 not_dir 返回，避免为墨刀链接多打一次无谓 HTTP 请求。
        # （墨刀来源走 Playwright 路径，与目录解析无关。）
        if 'modao.cc' in base_url or 'view_mode=' in base_url:
            return None, 'not_dir'
        if not target.endswith('/'):
            last_seg = target.rsplit('/', 1)[-1]
            if '.' in last_seg:
                return None, 'not_dir'  # 已经是具体文件，不需要解析
            target = target + '/'
        body = ''
        last_code = ''
        # ⚠️ 这类内网文件服务器可能抖动，做有限重试（顺带区分「关闭目录浏览」与「临时故障」）。
        for attempt in range(3):
            try:
                r = requests.get(target, cookies=cookie_dict or {}, timeout=timeout,
                                 headers=_UA_HEADERS)
                if r.status_code == 200:
                    if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'ascii'):
                        r.encoding = 'utf-8'
                    body = r.text
                    break
                last_code = r.status_code
                logger.info(f'[目录解析] {target} 第 {attempt + 1} 次返回 HTTP {last_code}，重试')
            except Exception as e:
                last_code = 'network'
                logger.info(f'[目录解析] {target} 第 {attempt + 1} 次异常 {e}，重试')
            time.sleep(0.6 * (attempt + 1))
        if not body:
            # 404/403 → 极可能是「目录浏览被关闭」而非「内容不存在」：
            # 同目录下的具体文件往往仍能访问，所以给出"改填直链"的可操作提示。
            if last_code in (403, 404):
                logger.warning(
                    f'[目录解析] {target} 返回 HTTP {last_code}，判定为「目录浏览已关闭」'
                    f'（同目录下的 .html 直链通常仍可访问），建议直接使用文件直链')
                return None, 'no_browsing'
            if last_code == 'network':
                return None, 'network'
            return None, f'http_{last_code}'
        # 只在「索引页」上做解析：IIS 有 [To Parent Directory]，其它常见的是 <h1> + <pre>
        low = body.lower()
        if '[to parent directory]' not in low and '<pre' not in low:
            return None, 'not_index'
        hrefs = cls._DIR_INDEX_HREF_RE.findall(body) or cls._DIR_INDEX_HREF_RE2.findall(body)
        if not hrefs:
            return None, 'no_html'
        # 去重保序，剔除外部链接
        uniq = []
        for h in hrefs:
            if h not in uniq and not h.lower().startswith(('http://', 'https://', '//')):
                uniq.append(h)
        if not uniq:
            return None, 'no_html'
        if len(uniq) > 1:
            # 多个 html 时按常见原型/文档命名优先，仍无法唯一确定则不猜
            prio = ('prototype', 'prd', 'readonly', 'index', 'start')
            ranked = [h for h in uniq if any(k in h.lower() for k in prio)]
            if len(ranked) != 1:
                logger.info(f'[目录解析] {len(uniq)} 个 html，无法唯一确定，放弃：{uniq[:6]}')
                return None, 'ambiguous'
            uniq = ranked
        return urljoin(target, uniq[0]), ''

    @staticmethod
    def _dir_fail_hint(base_url: str, reason: str) -> str:
        """把目录解析失败原因翻译成用户可执行的提示。"""
        base = base_url.rstrip('/')
        if reason == 'no_browsing':
            return (f'该地址是**目录**，但服务器关闭了目录浏览（访问目录返回 404，'
                    f'目录下的具体文件仍可访问）。请改为填写目录内 HTML 文件的完整地址，'
                    f'形如 {base}/xxx.html')
        if reason == 'ambiguous':
            return (f'目录 {base} 下有多个 HTML 文件，无法自动确定用哪一个。'
                    f'请直接填写目标 HTML 文件的完整地址')
        if reason == 'no_html':
            return f'目录 {base} 下未找到 HTML 文件，请确认地址是否正确'
        if reason == 'not_index':
            return f'该地址返回的不是文件目录索引，请确认是否为原型/文档地址'
        if reason == 'network':
            return f'无法访问 {base}（网络异常），请确认服务器可达'
        if reason.startswith('http_'):
            return f'访问目录 {base} 返回 {reason.replace("http_", "HTTP ")}，请确认地址与权限'
        return f'目录解析失败（{reason}）'

    @staticmethod
    def _guess_dir_file_from_history(base_url: str) -> Optional[str]:
        """兜底：从历史成功读取的记录里，找同一目录下的文件直链。

        场景：用户一直粘目录地址，之前能成功（那时 data/document.js 还在），
        现在失败。若历史 extracted_json 里记录过该目录下用过的文件 URL，直接复用。
        注意：只做**同目录前缀匹配**，不跨目录猜；查不到返回 None。
        """
        try:
            from apps.requirement_analysis.models import ModaoPrototype
        except Exception:
            return None
        # 墨刀/非目录地址不做历史兜底（避免无谓的 DB 扫描）
        if 'modao.cc' in base_url or 'view_mode=' in base_url:
            return None
        norm = base_url.rstrip('/').lower() + '/'
        try:
            qs = (ModaoPrototype.objects
                  .exclude(extracted_json='')
                  .order_by('-id')
                  .values_list('extracted_json', flat=True)[:80])
        except Exception:
            return None
        for ej in qs:
            try:
                data = json.loads(ej or '{}')
            except Exception:
                continue
            # 候选来源：urls 列表 / pages 里记录过的文件级 URL / warnings 里的定位结果
            cands = list(data.get('urls') or [])
            for w in (data.get('warnings') or []):
                m = re.search(r'已从目录地址定位到单文件：(\S+)', str(w))
                if m:
                    cands.append(m.group(1))
            for c in cands:
                c = str(c or '').strip()
                if not c.lower().startswith(norm):
                    continue
                if '.' not in c.rsplit('/', 1)[-1]:
                    continue  # 仍是目录，不要
                logger.info(f'[目录解析] 从历史记录兜底命中直链：{c}')
                return c
        return None

    # ---------- PRD 报告页（第 4 种来源，纯 HTTP）----------
    @staticmethod
    def _is_prd_report_html(html_text: str) -> bool:
        """判断 HTML 是否为「PRD 报告页」格式。

        特征：有 `.requirement-module`（需求模块）或 ≥2 个 `.prototype-item`（截图卡片），
        且**没有**内联 pages 页面树（有 pages 的是单文件 H5 原型，走另一条路径）。
        用字符串计数而非正则扫描，避免在 100MB+ 文档上退化。
        """
        if not html_text:
            return False
        # 有内联页面树 → 属于单文件 H5 原型，不是 PRD 报告页
        if _SINGLEFILE_H5_PAGES_RE.search(html_text):
            return False
        n_mod = html_text.count(_PRD_MODULE_CLS)
        n_item = html_text.count(_PRD_ITEM_CLS)
        if n_mod >= _PRD_BLOCK_SPLIT_MIN or n_item >= 2:
            return True
        # ⚠️ 2026-11-05：新版 PRD 报告页（如「心动手表 1.3.10｜Lovin PRD」）用的是
        # `<section class="doc-page" data-doc-page="...">` 承载需求正文、
        # `.prototype-page` / `.prototype-card` 承载截图，类名与旧版完全不同，
        # 上面两个判据均为 0，导致三条识别路径全失效、报「未能识别该地址的内容格式」。
        # 实测该类页面：doc-page=7、prototype-card=22、prototype-title=23。
        # 要求**真实结构**：class="doc-page" 必须与 data-doc-page= 同标签出现，
        # 避免正文/代码示例里提到该 class 名就被误判（实测会造成垃圾模块）。
        n_doc = len(_PRD_DOC_PAGE_RE.findall(html_text))
        if n_doc >= 2:
            return True
        n_card = html_text.count(_PRD_CARD_CLS)
        return n_card >= 2

    @staticmethod
    def _html_to_text(fragment: str) -> str:
        """HTML 片段 → 纯文本。保留段落/列表层级与「●➢」等项目符号。

        仅用于小片段（已剥离 data URI），不做 100MB 级全文处理。
        """
        if not fragment:
            return ''
        s = _SCRIPT_RE.sub(' ', fragment)
        s = re.sub(r'<(style|template)[^>]*>.*?</\1>', ' ', s, flags=re.S | re.I)
        s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
        s = re.sub(r'</(p|div|li|h[1-6]|tr|section|td)>', '\n', s, flags=re.I)
        s = _TAG_RE.sub('', s)
        s = html.unescape(s)
        s = s.replace('\u00a0', ' ')
        s = re.sub(r'[ \t]+', ' ', s)
        s = re.sub(r'\n\s*\n+', '\n', s)
        return s.strip()

    @classmethod
    def _parse_prd_report(cls, html_text: str):
        """解析 PRD 报告页，返回 (modules, proto_titles)。

        modules: [{'id','name','owner','summary','text'}] —— 真正的需求正文
        proto_titles: ['01新用户引导—剧情开始页', ...] —— 截图卡片的页名清单
        调用前请先剥离 data URI，否则超长字符串会显著拖慢处理。
        """
        modules: List[Dict[str, str]] = []
        # ⚠️ 2026-11-05：新版 PRD 报告页优先按 `<section class="doc-page" data-doc-page="...">`
        # 切分。该类页面的需求正文与状态都挂在 doc-page 内：
        #   h2.page-heading   = 页面名（业务模块名）
        #   p.page-intro      = 页面概述
        #   section.state-requirement[data-state] = 每个页面状态的需求（badge 为状态名）
        # 旧版判据（requirement-module）在该格式下计数为 0，会产出空 modules。
        doc_starts = []
        # ⚠️ 必须验证是**真实结构**而不是正文里提到的字样：要求同一标签内同时出现
        # `class="doc-page"` 与 `data-doc-page=`，否则旧格式文档里引用
        # 「新版结构使用 class="doc-page"」这类说明文字会被误当模块切分点（实测会产出
        # `name=class="doc-page"` 这种垃圾模块，把正文拆碎）。
        for m in _PRD_DOC_PAGE_RE.finditer(html_text):
            doc_starts.append(m.start())
        # 旧格式优先级更高：只要存在 requirement-module，就不走新分支，
        # 确保历史 PRD 文档解析结果与改动前完全一致。
        if doc_starts and html_text.count(_PRD_MODULE_CLS) >= _PRD_BLOCK_SPLIT_MIN:
            doc_starts = []
        if doc_starts:
            doc_starts.append(len(html_text))
            for i in range(len(doc_starts) - 1):
                seg = html_text[doc_starts[i]:doc_starts[i + 1]]
                text = cls._html_to_text(seg)
                if not text:
                    continue
                hid = ''
                m_dp = re.search(r'data-doc-page="([^"]+)"', seg[:400])
                if m_dp:
                    hid = m_dp.group(1)
                name = ''
                m_h = re.search(r'class="page-heading"[^>]*>(.*?)</h[1-6]>', seg, re.S)
                if m_h:
                    name = cls._html_to_text(m_h.group(1))
                summary = ''
                m_i = re.search(r'class="page-intro[^"]*"[^>]*>(.*?)</p>', seg, re.S)
                if m_i:
                    summary = cls._html_to_text(m_i.group(1))
                # 状态清单：badge 文本 + 该状态的需求正文
                states = []
                # 与模块切片同理：切分点回退到所在标签的 '<'，
                # 否则每段开头会混入 `class="state-requirement" data-state=...>` 属性残片。
                s_positions = []
                for mm in re.finditer(r'class="[^"]*\bstate-requirement\b[^"]*"', seg):
                    lt = seg.rfind('<', max(0, mm.start() - 400), mm.start())
                    s_positions.append(lt if lt >= 0 else mm.start())
                for si, sp in enumerate(s_positions):
                    s_end = s_positions[si + 1] if si + 1 < len(s_positions) else len(seg)
                    sseg = seg[sp:s_end]
                    st_name = ''
                    m_b = re.search(r'class="state-badge"[^>]*>(.*?)</span>', sseg, re.S)
                    if m_b:
                        st_name = cls._html_to_text(m_b.group(1))
                    m_st = re.search(r'data-state="([^"]+)"', sseg[:200])
                    states.append({
                        'state': st_name or (m_st.group(1) if m_st else f'状态{si + 1}'),
                        'state_id': m_st.group(1) if m_st else '',
                        'text': cls._html_to_text(sseg),
                    })
                if not name:
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    name = lines[0] if lines else f'需求模块{i + 1}'
                # 把状态需求并入模块正文，确保测试点能覆盖每个状态
                if states:
                    st_block = '\n'.join(
                        '[状态:%s]\n%s' % (st.get('state') or '', st.get('text') or '')
                        for st in states)
                    text = (text + '\n' + st_block).strip()
                modules.append({
                    'id': hid or f'module-{i + 1}',
                    'name': name,
                    'owner': '',
                    'summary': summary,
                    'text': text,
                    'states': states,
                })
            if modules:
                # 截图卡片页名清单（新版用 prototype-card + prototype-title）
                proto_titles2: List[str] = []
                for m in re.finditer(r'class="%s"' % _PRD_CARD_CLS, html_text):
                    seg = html_text[max(0, m.start() - 200): m.start() + 1200]
                    m_t = re.search(r'class="prototype-title"[^>]*>(.*?)</h[1-6]>', seg, re.S)
                    if m_t:
                        t = cls._html_to_text(m_t.group(1))
                        if t:
                            proto_titles2.append(re.sub(r'^[0-9０-９]+\s*', '', t))
                return modules, proto_titles2

        # 需求模块：按 class 位置切片（同构、不嵌套），避免逐字符括号配对。
        # 切片起点回退到所在标签的 '<'，否则首段会混入 `class="..." id="...">` 属性文本。
        starts = []
        for m in re.finditer(r'class="%s"' % _PRD_MODULE_CLS, html_text):
            lt = html_text.rfind('<', max(0, m.start() - 400), m.start())
            starts.append(lt if lt >= 0 else m.start())
        if starts:
            starts.append(len(html_text))
            for i in range(len(starts) - 1):
                seg = html_text[starts[i]:starts[i + 1]]
                text = cls._html_to_text(seg)
                if not text:
                    continue
                hid = ''
                m_id = re.search(r'id="([^"]+)"', seg[:400])
                if m_id:
                    hid = m_id.group(1)
                # 模块名 = 标题元素文本，兜底取正文首行
                name = ''
                m_h = re.search(r'<(h[1-6])[^>]*>(.*?)</\1>', seg, re.S)
                if m_h:
                    name = cls._html_to_text(m_h.group(2))
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if not name and lines:
                    name = lines[0]
                # 模块标题行常与「负责人：X」同处一段（如「新用户剧情与首登引导负责人：孙玲」），
                # 拆开，保证 page_name 干净、owner 单独留证。
                if '负责人' in name:
                    name = re.split(r'负责人[:：]', name)[0].strip()
                owner = ''
                m_ow = re.search(r'负责人[:：]\s*([^\s\n]+)', text)
                if m_ow:
                    owner = m_ow.group(1)
                summary = ''
                if lines:
                    # 概述取「负责人/模块名」行之后、下一个『【/●/➢』标记之前的那一行。
                    for ln in lines[:6]:
                        if ('负责人' in ln or ln == name
                                or ln.startswith(('class=', '<'))):
                            continue
                        if ln.startswith(('【', '●', '➢', '·', '-')):
                            break
                        summary = ln
                        break
                modules.append({
                    'id': hid or f'module-{i + 1}',
                    'name': name or f'需求模块{i + 1}',
                    'owner': owner,
                    'summary': summary,
                    'text': text,
                })
        # 截图卡片页名清单
        proto_titles: List[str] = []
        for m in re.finditer(r'class="%s"' % _PRD_ITEM_CLS, html_text):
            seg = html_text[m.start():m.start() + 1200]
            m_t = re.search(r'class="prototype-title"[^>]*>(.*?)</h4>', seg, re.S)
            if m_t:
                t = cls._html_to_text(m_t.group(1))
                if t:
                    proto_titles.append(re.sub(r'^[0-9０-９]+\s*', '', t))
                continue
            m_a = re.search(r'<img[^>]*\balt="([^"]*)"', seg)
            if m_a and m_a.group(1).strip():
                proto_titles.append(m_a.group(1).strip())
        return modules, proto_titles

    @classmethod
    def _prd_page_specs(cls, modules, proto_titles):
        """把 (需求模块, 页面标题) 组装成落库页规格。

        关键决策：**需求模块才是信息载体**。每个模块单独成一"页/画布"（携带其正文），
        截图卡片的页名清单不单独成页，而是并入「需求模块索引」页——
        因为 38 张卡片本身只有标题没有正文，若各自成页会产出大量空壳画布，
        反而污染后续结构化与质量门禁（覆盖率/融合都会把它们判为无效内容）。
        """
        specs: List[Dict[str, str]] = []
        for mod in modules:
            title = mod.get('name') or '需求模块'
            body = mod.get('text') or ''
            # 正文已含「负责人：X」（来自原文），不重复补；仅在原文未出现时补一行，
            # 避免同一信息出现两次干扰后续结构化。
            if mod.get('owner') and '负责人' not in body:
                body = f'负责人：{mod["owner"]}\n{body}'
            specs.append({
                'page_name': title,
                'text': body,
                'group': '',
            })
        if proto_titles:
            lines = [f'{i}. {t}' for i, t in enumerate(proto_titles, 1)]
            specs.append({
                'page_name': '原型页面清单',
                'text': ('本文档附带 %d 张原型截图页面（仅标题，无正文，'
                         '截图内容见文档内嵌图片）：\n' % len(proto_titles))
                        + '\n'.join(lines),
                'group': '',
            })
        return specs

    def _is_prd_report_url(self, url: str, cookies: Optional[str] = None) -> bool:
        """轻量探测 URL 是否为 PRD 报告页（只读流式前若干 KB，避免拉取 100MB+ 全文）。"""
        import requests
        try:
            resp = requests.get(url, cookies=self._cookie_dict(cookies),
                                timeout=20, headers=_UA_HEADERS, stream=True)
            if resp.status_code != 200:
                return False
            try:
                length = int(resp.headers.get('Content-Length') or 0)
            except Exception:
                length = 0
            # 超大文件也要扫完才有结论，因此仍读全文，但用 iter_content 提前中断：
            # 命中 PRD 特征即可返回，避免无谓读完 100MB。
            buf = []
            size = 0
            limit = 8 * 1024 * 1024  # 最多扫 8MB 就够命中特征（模块/卡片通常在文档中前部）
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    break
                buf.append(chunk)
                size += len(chunk)
                joined = b''.join(buf).decode('utf-8', 'ignore')
                if not ModaoExtractor._is_prd_report_html(joined):
                    if size >= limit:
                        return False
                    continue
                return True
            joined = b''.join(buf).decode('utf-8', 'ignore')
            if length and size < length:
                # 未读完且特征未命中：降级为一次全文判断（少数模块在文档尾部的情况）
                return False
            return ModaoExtractor._is_prd_report_html(joined)
        except Exception:
            return False

    def extract_prd_report_sync(self, url: str, cookies: Optional[str] = None,
                                timeout: int = 120, dual_fusion: bool = False,
                                fusion_config=None) -> Dict[str, Any]:
        """PRD 报告页读取（纯 HTTP，不启动浏览器、不做视觉 OCR）。

        适用于「Lovin1.2.20-PRD.html」这类自研 PRD 文档：正文是 requirement-module 需求模块，
        内嵌截图体积极大（可达 100MB+）但信息量极低 → 解析前先剥离 data URI，
        处理耗时通常 < 2s。返回结构与 extract_webshare_sync 一致。

        关于 dual_fusion：本路径**没有视觉路**（截图已被剥离），所以融合退化为
        「纯 DOM 单路 → 融合」。之所以仍然产出 fused_requirements，是因为阶段0 的
        「融合需求为空」门禁在 dual_fusion=True 时会阻断 —— 不产出就等于该格式永远
        读不出来（这正是修复前的实际状况）。这里复用 dual_fusion_requirements()，
        它本来就能处理「只有 dom_text、vision_text 为空」的输入。
        """
        import requests
        warnings: List[str] = []
        pages: List[Dict[str, Any]] = []
        # 同 _resolve_single_html_in_dir：内网 IIS 文件服务器存在间歇性 404，
        # 大文档（100MB+）读取耗时长也更容易被打断，故统一做有限重试。
        html_text = ''
        last_err = ''
        for attempt in range(3):
            try:
                resp = requests.get(url, cookies=self._cookie_dict(cookies),
                                    timeout=timeout, headers=_UA_HEADERS)
                if resp.status_code == 200:
                    if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                        resp.encoding = 'utf-8'
                    html_text = resp.text
                    break
                last_err = f'HTTP {resp.status_code}'
                logger.info(f'[PRD报告页] 第 {attempt + 1} 次获取返回 {last_err}，重试')
            except Exception as e:
                last_err = str(e)
                logger.info(f'[PRD报告页] 第 {attempt + 1} 次获取异常 {last_err}，重试')
            time.sleep(0.8 * (attempt + 1))
        if not html_text:
            return {'run_id': self.run_id, 'url': url, 'pages': pages,
                    'warnings': [f'获取失败（已重试 3 次）: {last_err}']}

        raw_len = len(html_text)
        # 先剥离内联 base64 图片：这类文档 99%+ 的体积是 data URI，剥后可降到数十 KB
        html_text = _DATA_URI_RE.sub(' [IMG] ', html_text)
        if raw_len > 8 * 1024 * 1024:
            warnings.append(
                f'文档较大（{raw_len // 1024 // 1024}MB，其中绝大部分为内嵌截图），'
                f'已跳过截图仅提取需求正文')

        if not self._is_prd_report_html(html_text):
            return {'run_id': self.run_id, 'url': url, 'pages': pages,
                    'warnings': warnings + ['未识别为 PRD 报告页格式']}

        modules, proto_titles = self._parse_prd_report(html_text)
        if not modules and not proto_titles:
            return {'run_id': self.run_id, 'url': url, 'pages': pages,
                    'warnings': warnings + ['未解析出需求模块或原型页面清单']}

        logger.info(f'[PRD报告页] 解析到 {len(modules)} 个需求模块、'
                    f'{len(proto_titles)} 个原型页面标题')

        specs = self._prd_page_specs(modules, proto_titles)
        for idx, sp in enumerate(specs, 1):
            text = sp.get('text') or ''
            pages.append({
                'page_no': idx,
                'page_name': sp.get('page_name') or f'第{idx}页',
                'parent_name': sp.get('group') or '',
                'folder': '',
                'text': text,
                'dom_text': text,
                'layer_text': '',
                'vision_text': '',
                'dom_text_len': len(text),
                # PRD 文档截图未做识别 → 标记为未尝试，避免阶段0「视觉覆盖率不足」误阻断
                'vision_attempted': False,
                'vision_success': False,
                # ⚠️ 本字段只由 PRD 报告页路径设置：告诉工作流的「画布自动过滤」
                # 这些条目是需求模块（名字可能含「引导/登录」等模板关键词，
                # 但内容是真实需求），不要按原型模板页的启发式丢弃。
                # 原型画布（墨刀/webshare/Axure）不带此标记，行为完全不变。
                'prd_module': True,
                'annotations': [],
                'screenshot': '',
                'screenshot_segments': [],
                'visual_summary': '',
                'visual_artifacts': [],
                'extraction_warnings': [],
            })
        warnings.append(
            f'PRD 报告页解析完成：{len(modules)} 个需求模块'
            + (f'、{len(proto_titles)} 个原型页面标题' if proto_titles else '')
            + '（内嵌截图未做识别，如需图片内容请另行提供）')

        result: Dict[str, Any] = {
            'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}

        # 纯 DOM 融合：本格式没有截图，融合实际只跑 DOM 一路。
        # 必须产出 fused_requirements，否则阶段0「融合需求为空」门禁会阻断该格式。
        if dual_fusion:
            result['dual_fusion'] = True
            fused: List[Dict[str, Any]] = []
            if fusion_config and pages:
                try:
                    fused = asyncio.run(
                        self.dual_fusion_requirements(pages, fusion_config))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f'PRD 报告页 DOM 融合失败（已跳过）: {e}')
                    warnings.append(f'PRD 报告页需求抽取失败（已跳过）: {e}')
            if not fused:
                # 融合失败/为空时，直接把 DOM 正文兜底成"每条需求"，避免门禁误判。
                # 宁可给下游一份粗糙但完整的需求，也不要让整轮读取白跑。
                fused = self._prd_dom_fallback_requirements(pages)
                if fused:
                    warnings.append(
                        f'PRD 报告页未走 LLM 融合，已按需求模块原文兜底生成 '
                        f'{len(fused)} 条需求')
            result['fused_requirements'] = fused
        return result

    @staticmethod
    def _prd_dom_fallback_requirements(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """融合不可用时的兜底：把每个 PRD 需求模块的正文原样包装成一条需求。

        这些模块本身就是人工整理好的文字需求（requirement-module），
        直接交给下游比"因为抽取失败而整轮判错"更有价值。
        """
        out: List[Dict[str, Any]] = []
        for p in pages or []:
            text = (p.get('text') or p.get('dom_text') or '').strip()
            if not text:
                continue
            name = (p.get('page_name') or '').strip() or '未命名需求模块'
            out.append({
                'source_pages': [name],
                'module': p.get('parent_name') or name,
                'feature': name,
                'interaction': text,
                'evidence': 'A',
            })
        return out

    async def _read_singlefile_h5_screen_texts(self, url: str,
                                               pages_meta: List[Dict[str, Any]]) -> Dict[str, str]:
        """单文件 H5：单次 Playwright 会话逐页 render(pid)，读取手机界面 .phone/.screen 的 innerText。

        返回 {页面id: 文本}。这类原型「状态按钮」(默认/登录中/登录失败)切换时**纯文本内容不变，
        变化的只是图片/图标**，因此 DOM 文本只读默认态一次即可，无需逐状态重复读取（避免重复
        文本叠加，也省 Playwright 交互开销）。每个状态的「图」差异由视觉 OCR 截图层单独保留。
        """
        out: Dict[str, str] = {}
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = await browser.new_context(
                    viewport={'width': 1440, 'height': 900},
                    user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'))
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=60000)
                # 左侧目录懒渲染：先等 .nav-item 出现，再交给 _read_nav_directory 做稳定性采样
                try:
                    await page.wait_for_selector('.nav-item', timeout=15000, state='attached')
                except Exception:
                    logger.info('[单文件H5] 未等到 .nav-item 出现（可能非目录式原型）')

                async def _read_screen():
                    return await page.evaluate('''() => {
                        const el = document.querySelector('.phone') || document.querySelector('.screen');
                        return el ? (el.innerText || el.textContent || '') : '';
                    }''')

                for idx, meta in enumerate(pages_meta, 1):
                    pid = meta.get('id') or f'page{idx}'
                    try:
                        # 只读取默认态文本（状态间纯文本不变，逐状态重复读取无意义）
                        await page.evaluate(f"render('{pid}')")
                        await page.wait_for_timeout(400)
                        default_txt = (await _read_screen()).strip()
                        if default_txt:
                            out[pid] = default_txt
                    except Exception:
                        out[pid] = out.get(pid, '')
                await browser.close()
        except Exception as e:
            logger.warning(f'单文件H5 手机界面文本读取失败: {e}')
        return out

    _RUNTIME_PAGES_JS = '''() => {
        // 从单文件原型的运行时变量里取权威页面清单，避免正则命中已废弃的内联数组。
        // 已知形态：
        //   scopeStates : { <pageId>: {...状态...} }   —— 旧版画布的页面集合
        //   newPages    : [{id, title, group, states}] —— 新增画布的页面集合
        //   retained    : Set<pageId>                  —— 页面真正可达的白名单
        const out = { pages: [], retained: [], source: '' };
        const push = (id, title, group, states) => {
            if (!id) return;
            out.pages.push({
                id: String(id),
                title: String(title || ''),
                group: String(group || ''),
                states: Array.isArray(states) ? states.map(String) : [],
            });
        };
        try { out.retained = Array.from(retained); } catch (e) {}
        try {
            for (const id of Object.keys(scopeStates)) {
                let st = [];
                try {
                    const v = scopeStates[id];
                    if (Array.isArray(v)) st = v;
                    else if (v && typeof v === 'object') st = Object.keys(v);
                } catch (e) {}
                push(id, id, '', st);
            }
            if (out.pages.length) out.source = 'scopeStates';
        } catch (e) {}
        try {
            if (typeof newPages !== 'undefined' && Array.isArray(newPages)) {
                for (const p of newPages) {
                    if (!p || !p.id) continue;
                    // 去掉与 scopeStates 重复的 id
                    if (out.pages.some(x => x.id === p.id)) continue;
                    push(p.id, p.title, p.group, p.states);
                }
                out.source += (out.source ? '+' : '') + 'newPages';
            }
        } catch (e) {}
        return out;
    }'''

    _NAV_DIRECTORY_JS = '''() => {
        // 从左侧目录（.nav-group 分组标题 + .nav-item 可点击条目）读取权威页面清单。
        // 单文件报告页的 .nav-item 形如：
        //   <button class="nav-item" onclick="vDirectory('search')">搜索</button>
        // onclick 里的参数就是页面真实 id（与 render()/retained 一致），
        // 而显示文本是作者定义的模块名 —— 这就是需求的真实目录结构。
        const groups = [];
        const nodes = Array.from(document.querySelectorAll('.nav-group, .nav-item'));
        let cur = '';
        for (const el of nodes) {
            if (el.classList && el.classList.contains('nav-group')) {
                cur = (el.innerText || '').trim();
                continue;
            }
            const oc = el.getAttribute('onclick') || '';
            const m = oc.match(/v(?:Directory|Go)\\(['"]([^'"]+)['"]\\)/);
            groups.push({
                group: cur,
                title: (el.innerText || '').trim(),
                id: m ? m[1] : '',
                active: el.classList.contains('active'),
            });
        }
        return groups;
    }'''

    async def _read_nav_directory(self, page) -> Optional[List[Dict[str, Any]]]:
        """读取左侧目录（nav-group + nav-item），作为**权威页面清单**。

        ⚠️ 2026-11-04 新增（hanakoi v1.10 单文件报告页实测）：
        该格式此前用正则解析内联 ``pages`` 数组取页面 id，结果拿到 19 个 id，
        其中 11 个（home/role/creator/chats/appearance…）是**已废弃 id**，
        不在页面 ``retained`` 白名单内，``render(id)`` 会被回落成
        ``create-method`` → 11 个画布截到同一张图，视觉产物大面积串页。

        真正权威的是左侧目录：
          - ``.nav-group``  = 模块分组（如「V1.10 · 聊天」「心动角色测验」）；
          - ``.nav-item``   = 页面条目，显示名是作者定义的模块名，
                              ``onclick="vDirectory('<真实id>')"`` 给出真实 id。
        实测 33 个条目可切出 29 个不同画面，且与业务模块边界完全对应。
        未被目录收录的 id（qa-*/heart-*/studio47-start 等流程中间态与废弃页）
        一律不纳入本期 —— 这与「走最新版」的口径一致。

        返回 None 表示页面没有这种目录结构（非本格式），调用方沿用原路径。
        """
        # ⚠️ 左侧目录是**懒渲染**的：实测 networkidle 后立即读到 28 个 .nav-item，
        # +1s 变 29，+3s 变 31，之后稳定在 30。直接读会漏页面。
        # 因此先等节点数量连续两次采样不变（最多约 8 秒）再取。
        try:
            _prev = -1
            for _ in range(8):
                _cnt = await page.evaluate(
                    "() => document.querySelectorAll('.nav-group, .nav-item').length")
                if _cnt == _prev and _cnt > 0:
                    break
                _prev = _cnt
                await page.wait_for_timeout(1000)
            logger.info('[单文件H5] 左侧目录渲染稳定，节点数=%s', _prev)
        except Exception as e:
            logger.info('[单文件H5] 等待左侧目录渲染稳定失败（继续读取）: %s', e)

        try:
            rows = await page.evaluate(self._NAV_DIRECTORY_JS)
        except Exception as e:
            logger.info('[单文件H5] 读取左侧目录异常: %s', e)
            return None
        if not isinstance(rows, list) or not rows:
            return None
        kept = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = str(r.get('title') or '').strip()
            pid = str(r.get('id') or '').strip()
            if not title:
                continue
            kept.append({
                'id': pid or title,          # 无 onclick 时用显示名兜底
                'title': title,
                'group': str(r.get('group') or '').strip(),
                'states': [],
                '_nav_entry': True,
            })
        if not kept:
            return None
        logger.info('[单文件H5] 左侧目录读取成功：%d 条目 / %d 个分组',
                    len(kept), len({k['group'] for k in kept if k['group']}))
        return kept

    async def _click_nav_entry(self, page, meta: Dict[str, Any]) -> bool:
        """点击左侧目录中与 meta 对应的 ``.nav-item`` 条目，完成画布切换。

        按 ``title``（显示名）+ ``group``（所属分组）**精确**定位，不用模糊匹配：
        该页面存在同名不同组的条目风险极低，而模糊匹配会把「角色创建方式」误配到
        「创建/编辑角色」。定位成功后比对 ``.phone`` 文本是否变化以确认真的切换。

        失败返回 False，由调用方回退 ``render(id)``。
        """
        title = str(meta.get('title') or '').strip()
        group = str(meta.get('group') or '').strip()
        if not title:
            return False
        try:
            ok = await page.evaluate("""([t, g]) => {
                const nodes = Array.from(document.querySelectorAll('.nav-group, .nav-item'));
                let cur = '';
                const hits = [];
                for (const el of nodes) {
                    if (el.classList && el.classList.contains('nav-group')) {
                        cur = (el.innerText || '').trim();
                        continue;
                    }
                    if ((el.innerText || '').trim() === t) hits.push({el, g: cur});
                }
                if (!hits.length) return false;
                const pick = g ? (hits.find(h => h.g === g) || hits[0]) : hits[0];
                pick.el.click();
                return true;
            }""", [title, group])
        except Exception as e:
            logger.info('[单文件H5] 点击目录条目「%s」异常: %s', title, e)
            return False
        if not ok:
            return False
        await page.wait_for_timeout(500)
        return True

    async def _read_runtime_pages(self, page) -> Optional[List[Dict[str, Any]]]:
        """从单文件原型的运行时 JS 变量读取页面清单（用于补充 states）。

        ⚠️ 2026-11-04 新增（hanakoi v1.10 单文件报告页实测）：
        该类页面会同时保留「旧版内联 pages 数组」与「运行时 newPages/scopeStates」。
        正则解析往往先命中旧数组，拿到一堆**已废弃 id**；
        这些 id 不在页面 ``retained`` 白名单内，``render(id)`` 会被回落成
        ``create-method``，导致多个画布截到同一张图（视觉产物串页）。

        本函数只用于**补充每个页面的 states**（页面状态清单），
        页面清单本身以 ``_read_nav_directory`` 为准。

        返回 None 表示该页面没有这些运行时变量（非本格式），调用方沿用正则结果。
        """
        try:
            data = await page.evaluate(self._RUNTIME_PAGES_JS)
        except Exception as e:
            logger.info('[单文件H5] 运行时页面清单读取异常: %s', e)
            return None
        if not isinstance(data, dict):
            return None
        pages = data.get('pages') or []
        if not pages:
            return None

        retained = set(str(x) for x in (data.get('retained') or []))
        kept, dropped = [], []
        for p in pages:
            pid = str(p.get('id') or '')
            if retained and pid not in retained:
                dropped.append(pid)
                continue
            kept.append(p)
        if dropped:
            logger.warning(
                '[单文件H5] 运行时清单中 %d 个页面不在 retained 白名单，已剔除（render 会回落）: %s',
                len(dropped), '、'.join(dropped[:10]))
        logger.info('[单文件H5] 运行时页面清单：%d 条（来源 %s，原始 %d）',
                    len(kept), data.get('source') or '?', len(pages))
        return kept or None

    async def _capture_singlefile_h5_session(self, url: str, pages_meta: List[Dict[str, Any]],
                                            cookie_dict: Optional[Dict[str, str]] = None,
                                            prototype_id: Optional[int] = None):
        """单次 Playwright 会话快速连拍所有主页面与子状态全景截图，同时读取屏幕 innerText。
        避免对每个状态反复 launch 浏览器。保持全景视口截图，绝不裁切需求信息。

        ⚠️ 2026-11-04：返回值新增第三项 ``effective_pages_meta`` —— 会话内以左侧目录
        覆盖后的**实际使用清单**。此前只返回 (screen_texts, shot_manifest)，
        内部重赋值 ``pages_meta = nav_meta`` 只作用于局部变量，调用方
        ``_extract_singlefile_h5`` 仍用正则解析出的旧清单去组装页面，
        导致目录纠正完全失效（表现为页面名仍是「浏览-搜索」这种旧 group-title 格式、
        且 13 个页面因 screen_texts 取不到而判成「无有效内容」）。
        """
        from playwright.async_api import async_playwright
        screen_texts: Dict[str, str] = {}
        shot_manifest: Dict[int, Dict[str, Any]] = {}

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = await browser.new_context(
                    viewport={'width': 1440, 'height': 900},
                    user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'))
                if cookie_dict:
                    cookies = [{'name': k, 'value': v, 'url': url} for k, v in cookie_dict.items()]
                    await context.add_cookies(cookies)

                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=60000)
                # 左侧目录懒渲染：先等 .nav-item 出现，再交给 _read_nav_directory 做稳定性采样
                try:
                    await page.wait_for_selector('.nav-item', timeout=15000, state='attached')
                except Exception:
                    logger.info('[单文件H5] 未等到 .nav-item 出现（可能非目录式原型）')

                # ⚠️ 2026-11-04：页面清单以**左侧目录（nav-group + nav-item）为准**。
                #
                # 背景（hanakoi v1.10 单文件报告页实测）：
                #   ① 正则从内联 pages 数组解析出 19 个 id，其中 home/role/creator/
                #      chats/chatsettings/announcement/likes/favorites/blocked/feedback/
                #      appearance 共 11 个是**已废弃 id**，不在 retained 白名单里，
                #      render 时被回落成 'create-method' → 11 个画布截到同一张图；
                #   ② 运行时 newPages/scopeStates 只有 6+13 条，也覆盖不全；
                #   ③ 左侧目录 33 条（含 nav-group 分组名）才是作者定义的真实模块目录，
                #      实测可切出 29 个不同画面，且与业务模块边界完全对应。
                # 因此：目录优先；目录读不到时才退回运行时清单。
                nav_meta = None
                try:
                    nav_meta = await self._read_nav_directory(page)
                except Exception as e:
                    logger.warning('读取左侧目录失败: %s', e)
                if not nav_meta:
                    try:
                        _c = await page.evaluate(
                            "() => ({nav: document.querySelectorAll('.nav-item').length,"
                            " grp: document.querySelectorAll('.nav-group').length})")
                    except Exception:
                        _c = {}
                    logger.warning('[单文件H5] 左侧目录不可用（nav-item=%s, nav-group=%s），'
                                   '退回运行时清单', _c.get('nav'), _c.get('grp'))
                if nav_meta:
                    pages_meta = nav_meta
                    # 用运行时清单补充每个页面的 states（状态清单）
                    try:
                        runtime_meta = await self._read_runtime_pages(page)
                    except Exception:
                        runtime_meta = None
                    if runtime_meta:
                        _st_map = {str(m.get('id')): (m.get('states') or [])
                                   for m in runtime_meta if m.get('id')}
                        for m in pages_meta:
                            if not m.get('states'):
                                m['states'] = _st_map.get(str(m.get('id')), [])
                    logger.info(
                        '[单文件H5] 页面清单以左侧目录为准：%d 条（正则原为 %d 条）',
                        len(pages_meta), len(pages_meta))
                else:
                    try:
                        runtime_meta = await self._read_runtime_pages(page)
                    except Exception as e:
                        runtime_meta = None
                        logger.warning('读取运行时页面清单失败（沿用正则解析结果）: %s', e)
                    if runtime_meta:
                        pages_meta = runtime_meta
                        logger.info('[单文件H5] 左侧目录不可用，退回运行时清单：%d 条',
                                    len(pages_meta))

                _blank_pages = []
                for idx, meta in enumerate(pages_meta, 1):
                    _raise_if_terminated(prototype_id)
                    pid = meta.get('id') or f'page{idx}'
                    states = meta.get('states') or []
                    page_entry = {'default_shot': None, 'state_shots': []}

                    # 1. 切换并拍摄默认态全景
                    # 说明（2026-11-04 修正）：优先点击左侧目录条目（真实用户操作路径，
                    # 实测 33/33 可点），render() 仅作兜底 —— 该页面对 qa-* 有答题守卫、
                    # 对已废弃 id 有 retained 回落，直接 render 并不可靠。
                    switched = False
                    try:
                        switched = await self._click_nav_entry(page, meta)
                    except Exception as e:
                        logger.warning(f"页面[{pid}] 点击目录条目失败: {e}")
                    if not switched:
                        try:
                            await page.evaluate(f"render('{pid}')")
                            await page.wait_for_timeout(250)
                        except Exception as e:
                            logger.warning(f"页面[{pid}] render 失败: {e}")

                    # 读取屏幕文本
                    try:
                        stxt = await page.evaluate("""() => {
                            const el = document.querySelector('.phone') || document.querySelector('.screen') || document.body;
                            return el ? (el.innerText || el.textContent || '') : '';
                        }""")
                        screen_texts[pid] = (stxt or '').strip()
                    except Exception:
                        screen_texts[pid] = ''

                    default_shot = os.path.join(self.shot_dir, f'ws_page_{idx}.png')
                    try:
                        await page.screenshot(path=default_shot, full_page=False)
                        page_entry['default_shot'] = default_shot
                    except Exception as e:
                        logger.warning(f"默认态截图失败 ws_page_{idx}.png: {e}")

                    # 2. 遍历状态快速切态连拍
                    for si, st in enumerate(states, 1):
                        _raise_if_terminated(prototype_id)
                        st = str(st).strip()
                        if not st:
                            continue
                        switched = False
                        for js in (
                            f"render('{pid}');setState('{pid}', '{st}')",
                            f"render('{pid}');setState('{st}')",
                            f"setState('{st}')",
                        ):
                            try:
                                await page.evaluate(js)
                                switched = True
                                break
                            except Exception:
                                continue
                        if switched:
                            await page.wait_for_timeout(200)
                            st_shot = os.path.join(self.shot_dir, f'ws_page_{idx}_st{si}.png')
                            try:
                                await page.screenshot(path=st_shot, full_page=False)
                                page_entry['state_shots'].append({'state': st, 'shot_path': st_shot})
                            except Exception as e:
                                logger.warning(f"状态态截图失败 {st_shot}: {e}")

                    shot_manifest[idx] = page_entry

                await browser.close()
        except Exception as e:
            logger.error(f"单文件H5 极速连拍会话异常: {e}")

        return screen_texts, shot_manifest, pages_meta

    @staticmethod
    def _is_image_file_different(path_a: str, path_b: str) -> bool:
        """轻量级图片差异比对：
        1. 路径或文件不存在时视为差异（兜底保护）；
        2. sha256 相同判定为完全无差异；
        3. 文件大小差异微弱且内容哈希匹配时判定无差异。
        """
        if not path_a or not path_b or not os.path.exists(path_a) or not os.path.exists(path_b):
            return True
        try:
            import hashlib
            with open(path_a, 'rb') as fa, open(path_b, 'rb') as fb:
                ha = hashlib.sha256(fa.read()).hexdigest()
                hb = hashlib.sha256(fb.read()).hexdigest()
                if ha == hb:
                    return False
        except Exception:
            pass
        return True

    def _plan_visual_jobs(self, shot_manifest: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """为所有页面与状态编排视觉识别任务：
        主页面默认图 100% 触发 full 视觉识别；
        子状态图比对默认图，存在差异才派发 incremental 任务，否则直接标记 inherit。
        """
        jobs = []
        for idx, entry in shot_manifest.items():
            def_shot = entry.get('default_shot')
            if def_shot:
                jobs.append({
                    'page_idx': idx,
                    'job_type': 'full',
                    'shot_path': def_shot,
                    'state': None,
                })

            for st_info in entry.get('state_shots', []):
                st_shot = st_info.get('shot_path')
                st_name = st_info.get('state')
                if st_shot:
                    if self._is_image_file_different(def_shot, st_shot):
                        jobs.append({
                            'page_idx': idx,
                            'job_type': 'incremental',
                            'shot_path': st_shot,
                            'state': st_name,
                        })
                    else:
                        jobs.append({
                            'page_idx': idx,
                            'job_type': 'inherit',
                            'shot_path': st_shot,
                            'state': st_name,
                        })
        return jobs

    async def _execute_visual_jobs_pool(self, jobs: List[Dict[str, Any]], vision_config,
                                       prototype_id: Optional[int] = None,
                                       concurrency: int = 6) -> Dict[int, Dict[str, Any]]:
        """使用 asyncio.Semaphore 并发调度视觉分析任务。
        并发调用阿里 DashScope 多模态大模型，复用已有 sha256 缓存。
        """
        if not jobs or not vision_config:
            return {}

        sem = asyncio.Semaphore(concurrency)
        results_by_page: Dict[int, Dict[str, Any]] = {}

        async def _run_one(job):
            _raise_if_terminated(prototype_id)
            pidx = job['page_idx']
            jtype = job['job_type']
            spath = job['shot_path']
            st_name = job.get('state')

            if jtype == 'inherit':
                return pidx, {'job_type': 'inherit', 'state': st_name, 'result': None}

            async with sem:
                _raise_if_terminated(prototype_id)
                try:
                    res = await self._ocr_screenshot(vision_config, spath)
                except Exception as e:
                    logger.warning(f"视觉分析失败 ({spath}): {e}")
                    res = {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
                return pidx, {'job_type': jtype, 'state': st_name, 'result': res}

        tasks = [_run_one(j) for j in jobs]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.warning(f"视觉并发分析单项发生异常: {item}")
                continue
            pidx, data = item
            if pidx not in results_by_page:
                results_by_page[pidx] = {
                    'ocr_text': '',
                    'visual_summary': '',
                    'visual_artifacts': [],
                    'states_data': []
                }

            jtype = data['job_type']
            res = data.get('result') or {}
            st_name = data.get('state')

            if jtype == 'full':
                results_by_page[pidx]['ocr_text'] = res.get('ocr_text', '')
                results_by_page[pidx]['visual_summary'] = res.get('visual_summary', '')
                results_by_page[pidx]['visual_artifacts'] = res.get('visual_artifacts', [])
            elif jtype == 'incremental' and res:
                results_by_page[pidx]['states_data'].append({
                    'state': st_name,
                    'summary': res.get('visual_summary', ''),
                    'new_text': res.get('ocr_text', ''),
                    'artifacts': res.get('visual_artifacts', [])
                })
            elif jtype == 'inherit':
                results_by_page[pidx]['states_data'].append({
                    'state': st_name,
                    'summary': '(无结构变更)',
                    'new_text': '',
                    'artifacts': []
                })

        return results_by_page


    def _extract_singlefile_h5(self, url: str, cookie_dict: Optional[Dict[str, str]] = None,
                               vision_config=None, dual_fusion: bool = False,
                               fusion_config=None, target_page_names=None) -> Dict[str, Any]:
        """单文件 H5 交互原型：纯 HTTP 抓取 + 解析内联 pages 数组与页面模板。

        返回结构与 extract_webshare_sync 一致：
        {'run_id','url','pages':[{page_no,page_name,text,dom_text,vision_text,...}],'warnings'}。
        非该格式返回空 pages + warnings（调用方据此回退其他路径）。
        """
        import requests
        warnings: List[str] = []
        pages: List[Dict[str, Any]] = []
        try:
            resp = requests.get(url, cookies=cookie_dict or {}, timeout=30, headers=_UA_HEADERS)
            if resp.status_code != 200:
                return {'run_id': self.run_id, 'url': url, 'pages': pages,
                        'warnings': [f'获取失败: HTTP {resp.status_code}']}
            if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                resp.encoding = 'utf-8'
            html_text = resp.text
        except Exception as e:
            return {'run_id': self.run_id, 'url': url, 'pages': pages,
                    'warnings': [f'获取失败: {e}']}

        pages_meta, templates = self._parse_singlefile_h5(html_text)
        if not pages_meta:
            return {'run_id': self.run_id, 'url': url, 'pages': pages,
                    'warnings': ['未识别为单文件 H5 原型（缺内联 pages 数组）']}

        # 单文件 H5 左侧全局导航文本：所有 group/title 都会作为目录项重复出现在每个画布；
        # 单画布重读时 pages 只有 1 张，无法靠频率统计发现公共行，因此用页面树先验知识强制剔除。
        h5_global_lines = set()
        for _m in pages_meta:
            for _k in ('title', 'group'):
                _v = str(_m.get(_k) or '').strip()
                if _v:
                    h5_global_lines.add(_v)
        if h5_global_lines:
            h5_global_lines.update({'目录', 'V1 核心版', 'V1 核心版目录'})


        # 单画布重读：只处理目标画布（按 page_name 匹配），避免对全部画布逐页 + 逐 state
        # 调视觉 OCR 导致后台任务长时间卡死。其余画布原样保留（由 workflow 单模式落库分支处理）。
        _target_set = set()
        if target_page_names:
            _target_set = {str(n).strip() for n in target_page_names if str(n).strip()}
        if _target_set:
            _named = []
            _title_set = {(_t.get('title') or '').strip() for _t in pages_meta if (_t.get('title') or '').strip()}
            # 精确匹配（page_name = group-title 拼接）；命中即收敛到目标画布。
            for _m in pages_meta:
                _t = _m.get('title') or ''
                _g = _m.get('group') or ''
                _pn = f'{_g}-{_t}' if _g else _t
                if _pn in _target_set:
                    _named.append(_m)
            # 兜底：精确匹配全部失败时，用 title 包含匹配（避免因 group 解析/编码差异
            # 导致匹配落空而退化全量 OCR 再次卡死）。
            if not _named:
                for _m in pages_meta:
                    _t = (_m.get('title') or '').strip()
                    if any(_t and _t in _tn for _tn in _target_set) or any(_tn and _tn in _t for _tn in _target_set):
                        _named.append(_m)
            # 至少命中一张才收敛；一张都没命中（命名体系完全错位）时退化全量，避免空结果误判
            if _named:
                pages_meta = _named
                logger.info(f'[单文件H5单画布重读] 目标画布收敛为 {len(pages_meta)} 张: {sorted(_target_set)}')

        # 1. 单会话复用快速连拍全景截图与屏幕文本提取 (耗时从 ~8分钟骤降至 ~20秒)
        _proto_id = getattr(self, 'prototype_id', None)
        screen_texts: Dict[str, str] = {}
        shot_manifest: Dict[int, Dict[str, Any]] = {}
        try:
            screen_texts, shot_manifest, _effective_meta = asyncio.run(
                self._capture_singlefile_h5_session(url, pages_meta, cookie_dict=cookie_dict, prototype_id=_proto_id)
            )
            # 会话内可能用左侧目录覆盖了页面清单，必须取回实际使用的那一份，
            # 否则下面组装仍用正则解析出的旧清单（含大量已废弃 id）。
            if _effective_meta:
                pages_meta = _effective_meta
        except Exception as e:
            logger.warning(f'单文件H5 会话截图连拍失败，回退为空: {e}')

        # 2. 编排视觉识别任务：主图全量深度识别 + 子状态智能差分
        visual_jobs = self._plan_visual_jobs(shot_manifest) if vision_config else []

        # 3. 异步并发池化推理 (Semaphore=6 并发加速，彻底消灭串行排队)
        visual_results: Dict[int, Dict[str, Any]] = {}
        if visual_jobs and vision_config:
            try:
                visual_results = asyncio.run(
                    self._execute_visual_jobs_pool(visual_jobs, vision_config, prototype_id=_proto_id)
                )
            except Exception as e:
                logger.warning(f'视觉并发分析执行异常: {e}')

        # 4. 组装全景 ModaoPage 结构 (保持 100% 格式对齐，包含完整 bbox 坐标与组件)
        for idx, meta in enumerate(pages_meta, 1):
            try:
                pid = meta.get('id') or f'page{idx}'
                title = meta.get('title') or f'第{idx}页'
                group = meta.get('group') or ''
                page_name = f'{group}-{title}' if group else title
                tpl_html = templates.get(pid, '')
                dom_text = ''
                if isinstance(tpl_html, str):
                    dom_text = html.unescape(_TAG_RE.sub(' ', tpl_html)).strip()
                stxt = (screen_texts.get(pid) or '').strip()
                if stxt:
                    dom_text = (dom_text + '\n[手机界面] ' + stxt).strip()

                vdata = visual_results.get(idx) or {}
                vision_text = vdata.get('ocr_text', '')
                page_vision_summary = vdata.get('visual_summary', '')
                page_vision_artifacts = list(vdata.get('visual_artifacts') or [])

                # 融合子状态增量视觉文本与组件坐标
                states_info = vdata.get('states_data', [])
                for st_item in states_info:
                    st_name = st_item.get('state')
                    st_sum = st_item.get('summary')
                    st_txt = st_item.get('new_text')
                    if st_sum and st_sum != '(无结构变更)':
                        page_vision_summary = (page_vision_summary + f'\n[状态:{st_name}]\n' + st_sum).strip()
                    if st_txt:
                        vision_text = (vision_text + f'\n[状态:{st_name}]\n' + st_txt).strip()
                    if st_item.get('artifacts'):
                        page_vision_artifacts.extend(st_item['artifacts'])

                # 代表截图优先取状态截图或默认全景
                entry = shot_manifest.get(idx) or {}
                st_shots = entry.get('state_shots', [])
                shot_file = st_shots[-1]['shot_path'] if st_shots else entry.get('default_shot')
                if not shot_file or not os.path.exists(shot_file):
                    shot_file = entry.get('default_shot')
                screenshot_path = os.path.relpath(shot_file, self.media_root) if shot_file and os.path.exists(shot_file) else ''

                vision_attempted = bool(vision_config)
                vision_success = bool(vision_text or page_vision_summary or page_vision_artifacts)

                text = (dom_text or '')
                if vision_text:
                    text = text + '\n[视觉OCR补充]\n' + vision_text

                page_vision_text = vision_text or ''
                if page_vision_summary:
                    page_vision_text = (page_vision_text + '\n' + page_vision_summary).strip()

                pages.append({
                    'page_no': idx,
                    'page_name': page_name,
                    '_src_id': pid,          # 内部字段：用于按 screen_texts 归并同画面入口
                    'text': text,
                    'dom_text': dom_text,
                    'vision_text': page_vision_text,
                    'dom_text_len': len(dom_text or ''),
                    'vision_attempted': vision_attempted,
                    'vision_success': vision_success,
                    'annotations': [],
                    'screenshot': screenshot_path,
                    'visual_summary': page_vision_summary,
                    'visual_artifacts': page_vision_artifacts,
                    'states': meta.get('states') or [],
                    'extraction_warnings': (
                        ['视觉识别未返回有效内容']
                        if vision_attempted and not vision_success else []
                    ),
                })
            except Exception as e:
                warnings.append(f"页面[{meta.get('title', idx)}]解析失败: {e}")
                pages.append({
                    'page_no': idx,
                    'page_name': meta.get('title') or f'第{idx}页',
                    'text': '',
                    'annotations': [],
                    'screenshot': '',
                    'visual_summary': '',
                    'visual_artifacts': [],
                    'extraction_warnings': [str(e)],
                })

        # ── B 方案：合并「多个入口指向同一画面」的重复页 ──
        # 实测 hanakoi v1.10：33 个导航入口里只有 29 个唯一画面，
        # 「个人中心 / 创作者主页 / 用户资料编辑」三个入口点开是同一屏，
        # 「启动页 / 搜索」、「首页·每日花瓣领取 / 首页角色分类设置」同理。
        # 若保留多份，会让测试点重复生成（同一画面产出 3 套用例）。
        # 处理：按屏幕文本指纹归并，保留首条为主页，其余记为 alias；
        # 主页面名称取该组第一个入口（导航顺序即业务顺序）。
        # 空内容入口（如「1.0遗留问题」无有效正文）单独告警，不参与需求。
        if len(pages) > 1 and screen_texts:
            _sig_map: Dict[str, List[Dict[str, Any]]] = {}
            for pg in pages:
                _pid = str(pg.get('_src_id') or '')
                _stxt = (screen_texts.get(_pid) or '').strip()
                # 文本过短视为无效页（空壳/仅状态栏），不参与合并判定
                if len(_stxt) < 20:
                    pg.setdefault('extraction_warnings', []).append(
                        '画布无有效正文内容，未纳入需求分析')
                    _sig_map.setdefault('__blank__' + str(pg.get('page_no')), []).append(pg)
                    continue
                _sig = hashlib.sha256(_stxt.encode('utf-8')).hexdigest()
                _sig_map.setdefault(_sig, []).append(pg)

            _merged: List[Dict[str, Any]] = []
            _blank: List[Dict[str, Any]] = []
            _dup_group_count = 0
            for _sig, _group in _sig_map.items():
                if _sig.startswith('__blank__'):
                    _blank.extend(_group)
                    continue
                _head = _group[0]
                if len(_group) > 1:
                    _dup_group_count += 1
                    _head['aliases'] = [g.get('page_name') for g in _group[1:]]
                    _head.setdefault('extraction_warnings', []).append(
                        '与 %s 为同一画面（多入口指向同一页），已合并为一条'
                        % '、'.join(g.get('page_name') for g in _group[1:]))
                    logger.info('[单文件H5] 合并同画面入口：%s ← %s',
                                _head.get('page_name'),
                                [_g.get('page_name') for _g in _group[1:]])
                _merged.append(_head)
            if _blank:
                warnings.append(
                    '有 %d 个入口无有效画面内容，未纳入需求分析：%s'
                    % (len(_blank), '、'.join(str(b.get('page_name')) for b in _blank)))
            if _dup_group_count:
                logger.info('[单文件H5] 页面去重：%d → %d（合并同画面入口 %d 组）',
                            len(pages), len(_merged), _dup_group_count)
            pages = _merged
            for _i, _pg in enumerate(pages, 1):
                _pg['page_no'] = _i
                _pg.pop('_src_id', None)

        # 单文件 H5：所有画布共用同一份左侧全局导航（目录列表），
        # 而画布本就是按模块拆分的，导航无独立信息价值 → 从每个画布文本中剔除。
        # 策略1：用页面树已知的 group/title 作为先验导航集合，>=1 页即执行（兼容单画布重读）。
        # 策略2：统计每行在多少画布出现，>=80% 视为全局公共资源（捕获状态栏/固定水印等）。
        global_lines = set(h5_global_lines)
        if len(pages) >= 3:
            from collections import Counter
            line_counter: Counter = Counter()
            for pg in pages:
                seen = set()
                for ln in (pg.get('text') or '').splitlines():
                    s = ln.strip()
                    if s:
                        seen.add(s)
                for s in seen:
                    line_counter[s] += 1
            threshold = max(2, int(0.8 * len(pages)))
            global_lines.update({ln for ln, c in line_counter.items() if c >= threshold})
        if global_lines:
            for pg in pages:
                for key in ('text', 'dom_text', 'vision_text'):
                    val = (pg.get(key) or '')
                    kept = [ln for ln in val.splitlines() if ln.strip() not in global_lines]
                    pg[key] = '\n'.join(kept).strip()
                pg['dom_text_len'] = len(pg.get('dom_text') or '')


        fused_requirements: List[Dict[str, Any]] = []
        if dual_fusion and fusion_config:
            try:
                fused_requirements = asyncio.run(self.dual_fusion_requirements(pages, fusion_config))
            except Exception as e:
                logger.warning(f'单文件H5 双路融合失败（已跳过）: {e}')
                warnings.append(f'双路融合失败（已跳过）: {e}')
        result: Dict[str, Any] = {
            'run_id': self.run_id, 'url': url, 'pages': pages, 'warnings': warnings}
        if dual_fusion:
            result['dual_fusion'] = True
            result['fused_requirements'] = fused_requirements
        return result

    # ---------- webshare / Axure：纯图片页视觉 OCR 兜底 ----------
    async def _ocr_webshare_page_async(self, page_url: str, cookie_dict: Dict[str, str],
                                       vision_config, idx: int,
                                       post_js: Optional[str] = None,
                                       shot_name: Optional[str] = None,
                                       skip_ocr: bool = False,
                                       summary_only: bool = False) -> Dict[str, Any]:
        """对 webshare 单个页面用 Playwright 渲染、截图，再调视觉模型 OCR（纯图片/截图/表格页兜底）。

        仅在 extract_webshare_sync 中『文本极少』时调用，纯文本页不会进入此分支。
        post_js：可选，goto 后执行的 JS（单文件 H5 需 render('页面id') 切到对应页）。
        shot_name：可选，自定义截图文件名（单文件 H5 多 state 切换时避免覆盖默认页截图）。
        skip_ocr：True 时只渲染+截图、不调视觉模型（用于纯保留截图证据场景）。
        summary_only：True 时仍调视觉模型，但只保留 visual_summary、丢弃 ocr_text/artifacts
                      （用于单文件 H5 逐状态「只识别图差异」：状态间纯文本相同，仅需模型
                      描述该状态相对默认态的视觉区别，避免重复合并 OCR 文本）。
        """
        from playwright.async_api import async_playwright
        if not vision_config:
            return {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
        shot_path = os.path.join(self.shot_dir, shot_name or f'ws_page_{idx}.png')
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1200},
                    device_scale_factor=4,
                    user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'))
                if cookie_dict:
                    try:
                        await context.add_cookies(
                            [{'name': k, 'value': v, 'url': page_url} for k, v in cookie_dict.items()])
                    except Exception:
                        pass
                page = await context.new_page()
                await page.goto(page_url, wait_until='networkidle', timeout=60000)
                if post_js:
                    try:
                        await page.evaluate(post_js)
                        await page.wait_for_timeout(500)
                    except Exception:
                        pass
                await page.wait_for_timeout(2000)
                await page.screenshot(path=shot_path, full_page=True)
                await browser.close()
            if skip_ocr:
                # 仅保留截图作为视觉证据，不调视觉模型（省调用）
                return {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
            ocr_out = await self._ocr_screenshot(vision_config, shot_path)
            if summary_only:
                # 只保留状态视觉描述，不合并 OCR 文本（状态间纯文本相同，合并文本无意义）
                return {'ocr_text': '', 'visual_summary': ocr_out.get('visual_summary', ''),
                        'visual_artifacts': []}
            return ocr_out
        except Exception as e:
            logger.warning(f'webshare 页面渲染/截图失败（跳过视觉OCR）: {e}')
            return {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}

    # ---------- 首次登录：可见浏览器扫码导 cookie ----------
    async def login_and_export_cookie(self, url: str, wait_seconds: int = 120) -> str:
        """
        启动可见浏览器，用户扫码登录；等待 wait_seconds 秒（或检测到登录态）后导出 cookie。
        Returns: cookie JSON 字符串（供后续回填）。
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=['--no-sandbox'])
            context = await browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
            page = await context.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            logger.info(f'请在打开的浏览器中扫码登录墨刀，将在 {wait_seconds}s 后自动导出 cookie…')
            await page.wait_for_timeout(wait_seconds * 1000)
            cookies = await context.cookies()
            await browser.close()
            return json.dumps(cookies, ensure_ascii=False)

    # ---------- 内部辅助 ----------
    @staticmethod
    def _parse_cookies(cookies: Optional[str]) -> List[Dict[str, Any]]:
        """解析 cookie 为 Playwright 可用的列表。

        支持三种来源：
        1) JSON 列表：[{"name": "x", "value": "y", ...}, ...]
        2) JSON 对象：{"cookies": [{"name":..,"value":..}, ...]}
        3) 浏览器复制的 raw 字符串："csrftoken=abc; sessionid=xyz; ..."（最常见，
           用户在开发者工具 Request Headers 里直接复制的那一行）
        未识别格式返回空列表（调用方据此降级匿名访问）。
        """
        if not cookies:
            return []
        cookies = cookies.strip()
        # 1) / 2) JSON 格式
        try:
            data = json.loads(cookies)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and 'cookies' in data:
                return data['cookies']
        except Exception:
            pass
        # 3) raw "k=v; k2=v2" 格式
        try:
            out = []
            for part in cookies.split(';'):
                part = part.strip()
                if not part or '=' not in part:
                    continue
                k, v = part.split('=', 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    out.append({'name': k, 'value': v, 'domain': '.modao.cc', 'path': '/'})
            if out:
                return out
        except Exception:
            pass
        return []

    async def _enumerate_pages(self, page, source_type: str = None,
                               wait_timeout: int = 8000) -> List[Any]:
        """枚举页面树元素，返回可点击的页面项列表（为空表示未识别）。

        注意：墨刀页面树里顶层画布展开后的「竖版/横版/弹窗」也是各自独立、点击会切换画布的
        真实画布，必须保留，不能按 DOM 层级过滤（否则会遗漏）。真正要剔除的是「点击后画布
        不变化」的纯分组/容器节点，那在抓取循环中用视口指纹判定并跳过（见 _run_extract 循环）。

        [稳健化] 墨刀 read_only 分享页的页面树是懒加载/按需渲染的，初始可能尚未把
        `.rn-list-item` 注入 DOM（脚本探测偶发拿到 0 节点）。因此不再“同步轮询、空即返回”，
        而是对每个候选选择器先用 wait_for_selector 短暂等待其出现（带超时轮询），命中即返回；
        全部候选都等不到再返回空，避免把“渲染未就绪”误判为“没有页面”。
        """
        selectors = list(PAGE_TREE_SELECTORS)
        if source_type == 'webshare':
            # webshare 优先用专门的页面树选择器，再回退通用选择器
            selectors = list(WEBSHARE_PAGE_TREE_SELECTORS) + list(PAGE_TREE_SELECTORS)
        for sel in selectors:
            try:
                # 先等该选择器出现；超时则换下一个候选（不抛错）
                try:
                    await page.wait_for_selector(sel, timeout=wait_timeout, state='attached')
                except Exception:
                    continue
                items = await page.query_selector_all(sel)
                if items:
                    logger.info(f'页面树命中选择器: {sel}，共 {len(items)} 页')
                    return items
            except Exception:
                continue
        return []

    async def _detect_parent_name(self, item, retries: int = 3,
                                  wait_timeout: int = 4000) -> str:
        """[稳健化] 读取画布页 item 的上级分组标题（如「前置付费页」）。

        墨刀分组项是 `.rn-list-item.folder`（**实测真实 class 是 folder，不是 fo**；
        此前用 `.rn-list-item.fo` 恒匹配不到，导致误判"只读页不渲染分组"、父级永远为空），
        与画布页（`.rn-list-item.page`）是**兄弟节点**而非祖先，故靠 DOM 顺序中「当前
        画布页之前、最近的那个分组项」作为上级标题，拼成「父-子」层级名（如 前置付费页-横版）。

        页面树懒加载未就绪时，分组节点可能尚未出现；这里做：① 重试 evaluate 最多 retries 次、
        每次失败前等待短暂时间；② 若 evaluate 抛错（句柄失效/节点未渲染）则降级返回空串，
        由调用方决定是否拼前缀。不直接抛错，避免中断整页抓取。
        """
        if item is None:
            return ''
        last_err = ''
        for attempt in range(1, retries + 1):
            try:
                parent_name = await item.evaluate(
                    """el => {
                        try {
                            // 分组节点真实 class 为 folder（兼容历史 fo 写法）
                            const folders = Array.from(
                                document.querySelectorAll('.rn-list-item.folder, .rn-list-item.fo'));
                            let best = null;
                            for (const f of folders) {
                                // f 必须在 el 之前（PRECEDING = f 先于 el）
                                if (el.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_PRECEDING) {
                                    // 取"最靠后的前置分组"= 离 el 最近的父分组：
                                    // 仅当 f 在 best 之后（FOLLOWING = best 先于 f）时才更新 best。
                                    // （此前误用 PRECEDING 导致取到最靠前的分组，
                                    //   使所有画布都归属第一个分组，如「通用付费页」下的画布
                                    //   被错标为「前置付费页」。）
                                    if (!best || (best.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING)) {
                                        best = f;
                                    }
                                }
                            }
                            if (best) {
                                const t = (best.innerText || '').trim().split('\\n')[0];
                                if (t && t !== (el.innerText||'').trim().split('\\n')[0]) return t;
                            }
                        } catch (e) {}
                        return '';
                    }"""
                )
                parent_name = (parent_name or '').strip()
                if parent_name:
                    return parent_name
                # 拿到空：可能是分组节点还没渲染出来，等一会再试
                if attempt < retries:
                    await asyncio.sleep(0.4)
            except Exception as e:  # 句柄失效/节点未就绪
                last_err = str(e)
                if attempt < retries:
                    await asyncio.sleep(0.4)
        if last_err:
            logger.warning(f'检测上级分组失败(已降级为空): {last_err}')
        return ''

    @staticmethod
    def _split_page_hierarchy(page_name: str) -> tuple:
        """从页面名本身提取层级前缀作为父级分组（页面树无 folder 节点时的兜底）。

        墨刀部分原型在只读分享页中不渲染 folder 分组节点，但会把层级关系直接
        编码在页面名里，例如「引导-身体数据」「升级付费-Android」。此时从 DOM
        读不到父级，需要按常见分隔符拆分，返回 (parent_name, child_page_name)。

        拆分规则：
          - 分隔符：- / > ： : | （按优先级尝试）
          - 前缀/后缀都非空，且长度在合理范围
          - 避免把日期/版本号等纯数字片段当作父级分组
          - 仅拆一次，保留后缀中可能存在的剩余分隔符
        """
        if not page_name:
            return '', ''
        delimiters = ['-', '/', '>', '：', ':', '|']
        for d in delimiters:
            if d in page_name:
                parts = page_name.split(d, 1)
                if len(parts) == 2:
                    parent = parts[0].strip()
                    child = parts[1].strip()
                    if (len(parent) >= 2 and len(parent) <= 40 and
                            len(child) >= 1 and len(child) <= 100):
                        # 过滤掉像 "03.15"、"V1.0" 这类非分组前缀
                        if re.fullmatch(r'\d{1,2}[./]\d{1,2}', parent):
                            continue
                        if re.fullmatch(r'[vV]\d+(?:\.\d+)*', parent):
                            continue
                        return parent, child
        return '', page_name

    @staticmethod
    async def _viewport_fingerprint(page) -> str:
        """返回当前稳定视口指纹，用于验证点击页面树后画布确实发生变化。"""
        try:
            payload = await page.screenshot(full_page=False)
            return hashlib.sha256(payload).hexdigest()
        except Exception:
            return ''

    async def _fingerprint_distance(page, ref_bytes: bytes = None) -> float:
        """估算当前视口相对参照图的差异比例（0~1），用于排除"微小动画差异"误判切换。

        取参照图 ``ref_bytes``（点击前截的图）与当前视口各降采样为 32x32 灰度，
        统计灰度差 > 8 的像素占比，即为差异比例。

        同时再取一张"当前 vs 当前（相隔 400ms）"的抖动基线：若页面本身在动
        （加载动画/时钟/光标），基线抖动会偏高，此时把有效阈值抬高到
        ``max(_MIN_VIEWPORT_DIFF_RATIO, jitter * 2)``，避免被噪声蒙混过关。

        返回实际差异比例；调用方用 ``returned >= effective_threshold`` 判定。
        截图失败返回 1.0（宁可放过，也不误杀真实切换）。
        """
        try:
            import io
            from PIL import Image
            ref_bytes = ref_bytes or b''
            cur1 = await page.screenshot(full_page=False)
            await page.wait_for_timeout(400)
            cur2 = await page.screenshot(full_page=False)

            def _gray(buf):
                im = Image.open(io.BytesIO(buf)).convert('L')
                # 裁掉左侧导航栏（占宽约 22%）：单文件 HTML 报告页的导航选中态
                # 会随点击变化，但画布可能根本没切换，直接全图比较会被导航差异干扰。
                # 同时提高采样到 64x64，避免画布细节被降采样平均掉。
                w, h = im.size
                if w > 200:
                    im = im.crop((int(w * 0.22), 0, w, h))
                return im.resize((64, 64))

            def _ratio(im_a, im_b):
                da, db = list(im_a.getdata()), list(im_b.getdata())
                n = float(len(da) or 1)
                return sum(1 for a, b in zip(da, db) if abs(a - b) > 8) / n

            g_ref, g_c1, g_c2 = _gray(ref_bytes), _gray(cur1), _gray(cur2)
            jitter = _ratio(g_c1, g_c2)
            actual = _ratio(g_ref, g_c1)
            self._last_jitter = jitter
            logger.info('[切换] 视口差异=%.4f 同页抖动=%.4f（抖动高说明页面在动）',
                        actual, jitter)
            return actual
        except Exception as e:
            logger.info('[切换] 视口差异估算失败（按通过处理）: %s', e)
            return 1.0

    def _effective_diff_threshold(self) -> float:
        """有效视口差异阈值：页面本身在动时抬高门槛，避免被动画噪声蒙混。"""
        jitter = float(getattr(self, '_last_jitter', 0.0) or 0.0)
        return max(_MIN_VIEWPORT_DIFF_RATIO, jitter * 2.0)

    _MODAO_ACTIVE_CID_JS = '''() => {
        const a = document.querySelector('.rn-list-item.active, li.rn-content-item.active');
        if (!a) return null;
        const li = a.closest('li') || a;
        return li.getAttribute('data-cid') || a.getAttribute('data-cid') || null;
    }'''
    # 提取每个画布节点所属文件夹路径（按左侧页面树层级拼接）
    # 只认 data-interactive-target-type=6 的节点，其余 target-type 视为文件夹节点
    # 文件夹名取 .editable-span 文本，按 '/' 拼接成 一级/二级 路径
    # 画布节点(target-type=6)自身不计入路径，只取它归属的 folder
    _MODAO_CANVAS_FOLDERS_JS = '''() => {
        const rows = [];
        const ttOf = (li) => {
            const n = li.querySelector('[data-interactive-target-type]');
            return n ? n.getAttribute('data-interactive-target-type') : '';
        };
        const nameOf = (li) => {
            const n = li.querySelector('.editable-span');
            return n ? n.innerText.trim() : '';
        };
        for (const li of document.querySelectorAll('li.rn-content-item')) {
            if (ttOf(li) !== '6') continue;
            const cid = li.getAttribute('data-cid');
            if (!cid) continue;
            const path = [];
            let cur = li.parentElement;
            while (cur) {
                const g = cur.closest('li.rn-content-item');
                if (!g) break;
                const tt = ttOf(g);
                const nm = nameOf(g);
                if (tt !== '6' && nm) path.unshift(nm);
                cur = g.parentElement;
            }
            rows.push({ cid, folder: path.join('/') });
        }
        return rows;
    }'''
    async def _extract_canvas_folders(self, page) -> dict:
        """提取每个画布所属文件夹，返回 {cid: folder} 映射"""
        try:
            rows = await page.evaluate(self._MODAO_CANVAS_FOLDERS_JS)
            return {r['cid']: r.get('folder', '') for r in (rows or []) if isinstance(r, dict)}
        except Exception as e:
            logger.warning(f'提取画布文件夹失败: {e}')
            return {}



    # 按墨刀内容元素(.widget)提取画布文本，分两组：
    #   body   = 画布正文文字（按位置 y→x 排序）
    #   sticky = 便利贴批注（wSticky，按编号升序，无编号排最后）
    # 相比整页 body.innerText，这样天然不含顶栏/导航/缩放比例等 UI 控件文字，
    # 且能区分「界面文案」与「批注说明」。
    _MODAO_EXTRACT_TEXTS_JS = '''() => {
        const scope = document.querySelector('.mb-screen') || document;
        const body = [], sticky = [];
        const seenBody = new Set(), seenSticky = new Set();
        for (const w of scope.querySelectorAll('.widget')) {
            const r = w.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const cls = typeof w.className === 'string' ? w.className : '';
            const t = (w.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!t || t.length < 2) continue;
            // 纯署名/日期（如"墨刀用户 2026.07.24"）无需求内容，跳过
            const cleaned = t.replace(/\\d{4}[./]\\d{1,2}[./]\\d{1,2}/g, '')
                             .replace(/墨刀用户/g, '').trim();
            if (!cleaned) continue;
            // 手机状态栏时间与纯符号装饰对需求无用
            if (/^\\d{1,2}:\\d{2}$/.test(t) || /^[…...·•+×✓-]+$/.test(t)) continue;
            const isSticky = /(^|\\s)wSticky(\\s|$)/.test(cls);
            const item = {text: t.slice(0, 1500), x: Math.round(r.x), y: Math.round(r.y)};
            if (isSticky) {
                const key = t.slice(0, 60);
                if (seenSticky.has(key)) continue;
                seenSticky.add(key);
                sticky.push(item);
            } else {
                const key = t.slice(0, 60);
                if (seenBody.has(key)) continue;
                seenBody.add(key);
                body.push(item);
            }
        }
        const byPos = (a, b) => (a.y - b.y) || (a.x - b.x);
        body.sort(byPos);
        const stickyNum = (s) => {
            const m = (s.text || '').match(/^(\\d+)[.、\\s]/);
            return m ? parseInt(m[1], 10) : 1e9;
        };
        sticky.sort((a, b) => (stickyNum(a) - stickyNum(b)) || byPos(a, b));
        return {body: body.slice(0, 300), sticky: sticky.slice(0, 60)};
    }'''

    async def _activate_page_item(self, page, item, other_items=None, item_cid: str = '',
                                  on_note=None) -> bool:
        """点击页面树条目，并确认画布确实切换到了目标。

        【校验优先级】active_cid 精确比对 > 视口指纹差异（需足够大）。

        ⚠️ 2026-11-04 修正（proto=93 单文件 HTML 报告页实测）：
        该格式下页面树元素**没有 data-cid**，``item_cid`` 恒为空，于是原有
        「精确校验」整段被 ``if item_cid:`` 跳过，只剩视口指纹兜底；
        而报告页的视口几乎不变，光标记时/加载动画/滚动条产生的微小差异
        又足以让 ``before != after`` 成立 → 19 个画布全部误判"切换成功"、
        无任何 warning，最终截图/visual_artifacts 大范围串页
        （19 个画布只截出 8 张不同图）。

        现在 ``item_cid`` 为空时启用**强证据模式**：视口差异必须超过
        ``_MIN_VIEWPORT_DIFF_RATIO`` 才算切换成功，否则继续重试并最终返回 False，
        由上层保留该页并交给「截图重复检测」（_mark_duplicate_screenshots）
        做最后判定，不再静默当成成功。

        重试策略（单纯重复点击同一 item 对墨刀无效——墨刀判定「当前已在该画布
        状态」时对同名/相邻项点击不会重新渲染）：
          1. 首轮：Escape → 点击目标 → 等待 → 校验。
          2. 重试轮：先点一个**其他**画布制造状态差，等墨刀渲染，**再点回目标**。

        item_cid 为目标画布的 data-cid（调用方从 item 上读取后传入）；为空时
        退化为强证据指纹对比（兼容无 data-cid 的原型格式）。
        on_note: 可选回调，用于把降级说明写进该页的 extraction_warnings。
        """
        require_strong_proof = not item_cid
        if require_strong_proof:
            logger.warning(
                '[切换] 目标页面项无 data-cid，改用强证据视口差异判定（阈值 %.3f）',
                _MIN_VIEWPORT_DIFF_RATIO)
        try:
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(200)
        except Exception:
            pass

        async def _active_cid() -> str:
            try:
                return (await page.evaluate(self._MODAO_ACTIVE_CID_JS) or '').strip()
            except Exception:
                return ''
        before = await self._viewport_fingerprint(page)
        # 强证据模式需要原始字节做像素级差异比较（sha256 无法反推图像）
        before_bytes = b''
        if require_strong_proof:
            try:
                before_bytes = await page.screenshot(full_page=False)
            except Exception:
                before_bytes = b''

        async def _emit_click(target, mode: int) -> None:
            """按 mode 用不同方式触发点击，逐级兜底。

            mode 0: Playwright 常规点击（最贴近真实用户操作）。
            mode 1: 直接派发 DOM click 事件（绕过遮挡/坐标偏移，
                    能触发墨刀 React 绑定在节点上的 handler，对分组内
                    被遮挡或坐标计算偏差的条目更有效）。
            mode 2: Playwright 强制坐标点击（force=True）。
            """
            if mode == 1:
                await target.evaluate('el => el.click()')
            elif mode == 2:
                await target.click(force=True, timeout=5000)
            else:
                await target.click(timeout=5000)

        for attempt in range(4):
            for click_mode in (0, 1, 2):
                try:
                    await item.scroll_into_view_if_needed()
                    # 重试轮：先切到"其他"画布制造状态差，再点回目标，强制墨刀重新渲染
                    if attempt > 0 and other_items and click_mode == 0:
                        for oi in other_items:
                            if oi is item:
                                continue
                            try:
                                await oi.scroll_into_view_if_needed()
                                await oi.click(force=True, timeout=5000)
                                await page.wait_for_timeout(1000)
                                break
                            except Exception:
                                continue
                    await _emit_click(item, click_mode)
                    await page.wait_for_timeout(1200 + attempt * 600)
                    try:
                        await page.keyboard.press('Escape')
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
                    # 1) 精确校验：当前 active 项的 cid 是否就是目标 cid
                    if item_cid:
                        ac = await _active_cid()
                        if ac and ac == item_cid:
                            return True
                        if ac:
                            logger.info(f'[切换] 点击后激活了 {ac}，目标为 {item_cid}，继续重试')
                    # 2) 兜底：视口指纹对比
                    #    ⚠️ 2026-11-04：视口指纹是**弱判据**，单文件 HTML 报告页里
                    #    光标记时、加载动画、滚动条等都会让截图产生微小差异，
                    #    从而把"没切换"误判成"切换成功"。因此要求指纹差异足够大
                    #    （见 _fingerprint_distance），且 item_cid 为空时记录告警，
                    #    让上层"截图重复检测"成为最后一道防线。
                    after = await self._viewport_fingerprint(page)
                    if before and after and before != after:
                        if require_strong_proof:
                            dist = await self._fingerprint_distance(page, before_bytes)
                            if dist < self._effective_diff_threshold():
                                logger.info(
                                    '[切换] 视口差异过小(%.4f < %.4f)，判定未真正切换画布',
                                    dist, self._effective_diff_threshold())
                                continue
                            if on_note:
                                on_note('已通过视口差异判定切换（该原型无 data-cid 精确校验）')
                        return True
                except Exception:
                    continue
        # 确实未变化：很可能是纯分组/容器节点，记录 warning 便于追溯，仍返回 False
        logger.info('页面项点击后视口指纹未变化，判定为未切换画布（可能为非独立分组节点）')
        return False

    def _mark_duplicate_screenshots(self, pages: List[Dict[str, Any]]) -> List[List[int]]:
        """标记内容完全相同的截图；保留页面供审计，但明确标为读取异常。

        ⚠️ 2026-11-04 修正（proto=93 实测）：原实现只看截图文件字节哈希，
        而 ``MEDIA_ROOT`` 是**相对路径**（值为 ``'media'``），一旦调用方工作目录
        不是项目根，``os.path.exists`` 全部返 False 就 ``continue`` ——
        结果 ``screenshot_sha256`` 全为空、重复组为空、warnings 为空，
        重复检测**完全静默失效**（实测 19 画布只截出 8 张不同图，重复率 58%，
        却一路判成功，导致每个画布的 visual_artifacts 混入同一份内容）。

        现在改为三重判据，任一命中即视为重复：
          1) 截图文件字节 sha256（文件可读时最可靠）；
          2) 截图相对路径本身相同（文件不可读时的兜底，同名字即同图）；
          3) ``visual_artifacts`` 内容签名相同（内容级判据，不依赖文件系统）。
        并统计"无法读取文件的页数"，超过半数时显式告警，不再静默跳过。
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        unreadable = 0
        for page_data in pages:
            rel_path = page_data.get('screenshot') or ''
            abs_path = os.path.join(self.media_root, rel_path) if rel_path else ''
            digest = ''
            if abs_path and os.path.exists(abs_path):
                try:
                    with open(abs_path, 'rb') as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                    page_data['screenshot_sha256'] = digest
                except Exception as e:
                    logger.warning('截图哈希计算失败（回退内容判据）: %s', e)
                    digest = ''
            if not digest:
                unreadable += 1
                # 兜底判据：内容签名（visual_artifacts 归一化）优先，其次相对路径
                va = page_data.get('visual_artifacts') or []
                if va:
                    digest = 'artifacts:' + hashlib.sha256(
                        json.dumps(va, ensure_ascii=False, sort_keys=True).encode('utf-8')
                    ).hexdigest()
                elif rel_path:
                    digest = 'path:' + hashlib.sha256(rel_path.encode('utf-8')).hexdigest()
                else:
                    continue
            groups.setdefault(digest, []).append(page_data)

        if pages and unreadable > len(pages) / 2:
            logger.warning(
                '[EXTRACT] %d/%d 页截图文件不可读，重复检测已回退到内容/路径判据'
                '（media_root=%s，请确认工作目录与 MEDIA_ROOT 一致）',
                unreadable, len(pages), self.media_root)

        duplicates = []
        for same_pages in groups.values():
            if len(same_pages) < 2:
                continue
            page_nos = [int(page_data.get('page_no') or 0) for page_data in same_pages]
            duplicates.append(page_nos)
            for page_data in same_pages[1:]:
                page_data.setdefault('extraction_warnings', []).append(
                    f'画布内容与第{page_nos[0]}页完全一致，未计为独立有效页面'
                )
                page_data['duplicate_of_page'] = page_nos[0]
        return duplicates

    async def _extract_widget_text(self, page) -> str:
        """按墨刀内容元素(.widget)提取画布文本，分「正文 / 便利贴批注」两组。

        相比整页 body.innerText 的优点：天然不含顶栏、导航、缩放比例等 UI 控件
        文字（它们不是 .widget），无需依赖后置噪声过滤；且保留批注与正文的区分。
        提取失败或画布无 widget 时返回空串，由调用方回退到 innerText。
        """
        try:
            data = await page.evaluate(self._MODAO_EXTRACT_TEXTS_JS)
        except Exception:
            return ''
        if not data:
            return ''
        body = data.get('body') or []
        sticky = data.get('sticky') or []
        if not body and not sticky:
            return ''
        parts = []
        if body:
            parts.append('[画布正文]\n' + '\n'.join(x['text'] for x in body))
        if sticky:
            parts.append('[便利贴批注]\n' + '\n'.join(x['text'] for x in sticky))
        return '\n'.join(parts)

    async def _extract_visible_text(self, page, retry: bool = False,
                                    min_chars: int = 60, max_retries: int = 2) -> str:
        """提取页面可见文本。retry=True 时（墨刀 DOM 异步渲染）首抓偏短则等待重试，
        对抗偶发 0 字符 / 未就绪，返回最终文本。

        优先走 widget 分组提取（_extract_widget_text，天然无 UI 噪声且区分批注）；
        该路取不到内容时回退整页 innerText，并用 _strip_ui_noise 剔除
        「总览/演示/标注/登录/62%」等界面控件文字。
        """
        # 1) 优先：按 .widget 分组提取
        widget_text = await self._extract_widget_text(page)
        if widget_text and len(widget_text) >= min_chars:
            return widget_text
        # widget 路内容偏少时，也重试等待一次（墨刀 DOM 异步渲染）
        if widget_text and retry:
            for _ in range(max_retries):
                await page.wait_for_timeout(1500)
                _t = await self._extract_widget_text(page)
                if _t and len(_t) > len(widget_text):
                    widget_text = _t
            if len(widget_text) >= min_chars:
                return widget_text

        # 2) 回退：整页 innerText + UI 噪声过滤
        try:
            text = (await page.evaluate('() => document.body.innerText') or '').strip()
        except Exception as e:
            logger.warning(f'提取可见文本失败: {e}')
            return widget_text
        tries = 1
        while retry and len(text) < min_chars and tries <= max_retries:
            await page.wait_for_timeout(1500)
            try:
                text = (await page.evaluate('() => document.body.innerText') or '').strip()
            except Exception:
                break
            tries += 1
        cleaned = self._strip_ui_noise(text)
        return cleaned or widget_text

    # 墨刀只读分享页自身的 UI 控件文字（整行精确匹配即剔除）。
    _UI_NOISE_EXACT = frozenset({
        '总览', '演示', '标注', '登录', '注册', '分享', '评论', '导出', '预览',
        '原型', '全屏', '适应屏幕', '回到顶部', '退出全屏', '缩放', '画布',
        '返回', '更多', '设置', '帮助', '搜索',
    })
    # 整行命中的噪声模式：缩放比例（62% / 100.0%）、纯页码/标尺数字。
    _UI_NOISE_PATTERNS = (
        r'^\s*\d+(?:\.\d+)?\s*%\s*$',
        r'^\s*[+\-]?\d+(?:\.\d+)?\s*(?:px|pt)?\s*$',
    )

    @classmethod
    def _strip_ui_noise(cls, text: str) -> str:
        """剔除墨刀 UI 控件噪声行，保留业务文本（含画布批注/便签）。

        同时用于 DOM 文本路（_extract_visible_text）与视觉 OCR 路（_parse_vision_payload），
        保证两路输入到 LLM 的文本都不含「总览/演示/标注/登录/62%」这类界面控件文字。
        """
        if not text:
            return ''
        kept = []
        for line in text.split('\n'):
            s = line.strip()
            if not s:
                continue
            if s in cls._UI_NOISE_EXACT:
                continue
            if any(re.match(p, s) for p in cls._UI_NOISE_PATTERNS):
                continue
            kept.append(s)
        return '\n'.join(kept)

    # 形状/图标/装饰类图层名关键词（无业务文本，应跳过）
    _NON_TEXT_LAYER_HINTS = (
        '矩形', '圆形', '椭圆', '线条', '箭头', '图片', '图像', '图标', '底栏', '状态栏',
        '背景', '遮罩', '按钮', 'md-', 'shape', 'rect', 'icon', 'image', 'bg',
        'group', '分组', '画板', 'frame',
    )

    async def _extract_layer_text(self, page, item, warnings: List[str],
                                  source_type: str = 'modao',
                                  page_name: str = '', item_cid: str = '') -> str:
        """[墨刀图层树增强] 在 inspect 模式下读取图层面板的文本图层名（100% 命中 DOM 文本，
        无 OCR 幻觉），与 DOM innerText + 视觉 OCR 形成第三路来源。

        流程：切 inspect → 点「图层」tab → 逐个展开分组(JS 标记 dataset.__exp + 数量信号
              防 expander toggle 横跳) → 取文本图层名
              → 切回 read_only 并重新激活当前画布（保证后续截图/OCR 仍在 read_only 下进行）。

        仅对墨刀 source 启用；失败静默回退（OCR 仍兜底），不影响主流程。
        """
        if source_type != 'modao':
            return ''
        try:
            # 1) 切换到 inspect 模式（墨刀 sharing 已开放 view_mode=inspect）
            #    注：墨刀分享页有长轮询心跳，networkidle 永不触发，必须用 domcontentloaded。
            current = page.url
            if 'view_mode=inspect' not in current:
                base = current.split('?')[0]
                await page.goto(base + '?view_mode=inspect',
                                wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(1500)

            # 2) 点「图层」tab（如存在）
            try:
                layer_tab = page.locator('text=图层').first
                if await layer_tab.is_visible(timeout=2000):
                    await layer_tab.click()
                    await page.wait_for_timeout(400)
            except Exception:
                pass

            # 3) 逐个展开所有分组。墨刀分享态的 expander 是同一 DOM 节点的 toggle，
            #    点击已展开节点会折叠，且展开后不添加 expanded 类（无法用 :not() 排除），
            #    直接循环点第一个会反复横跳。这里用 JS 给已点 expander 打 dataset.__exp
            #    标记避免重复点击，并以 layer-item 总数是否增长作为展开成功信号；若某轮
            #    点击后总数不增（误触已展开节点的折叠），立即再点恢复并停止，防止死循环。
            for _ in range(4):
                before = await page.locator(".rn-list-item.layer-item").count()
                clicked = await page.evaluate(
                    """() => {
                        const exps = Array.from(
                            document.querySelectorAll('.rn-list-item.layer-item .expander'));
                        let n = 0;
                        for (const e of exps) {
                            if (e.dataset.__exp) continue;
                            e.dataset.__exp = '1';
                            e.click();
                            n++;
                        }
                        return n;
                    }"""
                )
                await page.wait_for_timeout(500)
                after = await page.locator(".rn-list-item.layer-item").count()
                if not clicked:
                    break
                if after <= before:
                    # 误折叠已展开节点：撤销点击恢复展开，并停止（无新分组可展开）
                    await page.evaluate(
                        """() => {
                            const exps = Array.from(
                                document.querySelectorAll('.rn-list-item.layer-item .expander'));
                            for (const e of exps) {
                                if (e.dataset.__exp) { e.click(); delete e.dataset.__exp; }
                            }
                        }"""
                    )
                    await page.wait_for_timeout(500)
                    break
            await page.wait_for_timeout(300)

            # 4) 收集文本图层名（去形状/图标/空名；兼容编辑态 T 前缀）
            texts: List[str] = []
            # 墨刀分享态真实 class 为 .rn-list-item.layer-item；其余为兼容其他版本兜底
            layer_items = page.locator(
                ".rn-list-item.layer-item, div[class*='layer-item'], "
                "[class*='layer-tree'] [class*='item'], [class*='tree-item']")
            n = await layer_items.count()
            for i in range(n):
                try:
                    raw = (await layer_items.nth(i).inner_text()).strip()
                except Exception:
                    continue
                if not raw:
                    continue
                # inner_text 可能含子节点名（如「365天会员 / 文本」），取第一段
                name = raw.split('\n')[0].strip()
                low = name.lower()
                if any(k in low for k in self._NON_TEXT_LAYER_HINTS):
                    continue
                if re.fullmatch(r'[\d\s\-_/]+', name):
                    continue
                if name.startswith('T '):
                    name = name[2:].strip()
                if name:
                    texts.append(name)
            layer_text = '\n'.join(texts)

            # 5) 切回 read_only 并重新激活当前画布（保证后续截图在 read_only 下进行）
            if 'view_mode=inspect' in page.url:
                base = page.url.split('?')[0]
                await page.goto(base + '?view_mode=read_only',
                                wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(800)
            # 5) 切回 read_only 并重新激活当前画布（保证后续截图在 read_only 下进行）
            #    注意：上面的 goto 已整页重载，主循环传入的 item handle 此时已失效，
            #    直接点击会无效→画布停在默认视图→后续截图与上一画布重复。
            #    因此这里重新枚举页面树，按 page_name 匹配新鲜 handle 再激活。
            if 'view_mode=inspect' in page.url:
                base = page.url.split('?')[0]
                await page.goto(base + '?view_mode=read_only',
                                wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(800)
            reactivated = False
            try:
                fresh_items = await self._enumerate_pages(page, source_type=source_type)
                target_cid = (item_cid or '').strip()
                target = (page_name or '').strip()
                # [关键] 优先按 data-cid 匹配：页面树常存在同名画布（如两个「竖版」），
                # 仅按 page_name 匹配会命中第一个同名项，导致后续页被激活成前一同名
                # 画布 → 截图与前一页字节相同（表现为「撞图 / 切换失败」的假象）。
                # data-cid 全局唯一，可精确还原到切换前的那个画布。
                matched = None
                if target_cid:
                    for fi in fresh_items:
                        try:
                            if (await fi.get_attribute('data-cid')) == target_cid:
                                matched = fi
                                break
                        except Exception:
                            continue
                if matched is None and target:
                    for fi in fresh_items:
                        try:
                            fi_name = (await fi.inner_text()).strip().replace('\n', '/')
                        except Exception:
                            fi_name = ''
                        if target and (target in fi_name or fi_name in target):
                            matched = fi
                            break
                if matched is None and item is not None:
                    matched = item
                if matched is not None:
                    await matched.scroll_into_view_if_needed()
                    await matched.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1500)
                    reactivated = True
            except Exception as e:
                warnings.append(f'图层提取后重新激活画布失败: {e}')
            try:
                await page.keyboard.press('Escape')
                await page.wait_for_timeout(200)
            except Exception:
                pass
            return layer_text
        except Exception as e:
            warnings.append(f'图层树文本提取失败（已回退OCR）: {e}')
            return ''

    async def _extract_annotations(self, page, warnings: List[str]) -> List[str]:
        """尽力提取注释/标注文本。"""
        results: List[str] = []
        for sel in ANNOTATION_SELECTORS:
            try:
                nodes = await page.query_selector_all(sel)
                for node in nodes:
                    try:
                        t = (await node.inner_text()).strip()
                        if t and t not in results:
                            results.append(t)
                    except Exception:
                        continue
            except Exception:
                continue
        # 若命中了批注切换按钮，尝试展开评论面板再抓一次
        try:
            toggle = await page.query_selector('[class*="comment-toggle"], [class*="note-toggle"], [aria-label*="评论"], [aria-label*="批注"]')
            if toggle:
                await toggle.click()
                await page.wait_for_timeout(800)
                panel_text = await self._extract_visible_text(page)
                if panel_text:
                    results.append('[评论面板] ' + panel_text[:1000])
        except Exception as e:
            warnings.append(f'展开评论面板失败（可忽略）: {e}')
        return results

    # ---------- 墨刀截图：UI 隐藏 / 缩放归一化 / 内容包围盒 ----------
    # 移植自参考实现（经实测验证）。核心解决「画布被墨刀 CSS 缩放导致截图模糊」：
    # 墨刀用 .zoom-area 的 transform: scale(0.46) 之类把画布缩小显示，直接截图得到
    # 的就是缩小后的画面（文字糊）。归一化即反向补偿回 scale(1)，配合高倍采样
    # 可得到原始尺寸的清晰截图。

    _MODAO_HIDE_UI_JS = '''() => {
        const selectors = [
            '#fixed-area, .fixed_area, [class*="StyledSignUpPrompt"]',
            '.ruler, .rulerH, .rulerV, #mb-ruler, [class*="Ruler"], [class*="StyledRulerContainer"]',
            '[class*="ToolBar"], [class*="toolbar-left"], [class*="toolbar"], [class*="StyledToolbar"]',
            '[class*="comment"], [class*="annotation"], [class*="note-panel"], [class*="right-panel"]',
            '[class*="thumbnail"], [class*="status-bar"], [class*="bottom-bar"]',
            '[class*="LeftSidePanel"], [class*="left-panel"], [class*="LeftPane"], [class*="CanvasListPanel"], [class*="ScreenList"], .canvas-scroll-list, .toggleable-zone',
        ];
        const saved = window.__modaoHiddenUI || [];
        document.querySelectorAll(selectors.join(',')).forEach(el => {
            if (el.style.display !== 'none') {
                saved.push({el, display: el.style.display});
                el.style.display = 'none';
            }
        });
        window.__modaoHiddenUI = saved;
        return saved.length;
    }'''

    _MODAO_RESTORE_UI_JS = '''() => {
        const saved = window.__modaoHiddenUI || [];
        for (const item of saved) { item.el.style.display = item.display; }
        window.__modaoHiddenUI = [];
        return saved.length;
    }'''

    # 把画布缩放反向补偿回 100%（向上查找 transform scale / CSS zoom 的来源节点）
    _MODAO_NORMALIZE_ZOOM_JS = '''() => {
        const findZoomSource = (el) => {
            let node = el;
            while (node && node !== document.body) {
                const cs = getComputedStyle(node);
                const m = cs.transform.match(/^matrix\\(([^)]+)\\)$/);
                if (m) {
                    const p = m[1].split(',').map(Number);
                    const sx = Math.hypot(p[0], p[1]);
                    if (sx > 0.05 && Math.abs(sx - 1) > 0.02) return {el: node, scale: sx};
                }
                const z = parseFloat(cs.zoom);
                if (Number.isFinite(z) && z > 0.05 && Math.abs(z - 1) > 0.02) return {el: node, scale: z, isZoom: true};
                node = node.parentElement;
            }
            return null;
        };
        const sample = document.querySelectorAll('.widget, [id^="text-dom-"], .wImage');
        let source = null;
        for (const w of Array.from(sample).slice(0, 20)) {
            source = findZoomSource(w);
            if (source) break;
        }
        if (!source || source.scale >= 0.9) {
            return {scale: source ? source.scale : 1, normalized: false};
        }
        try {
            if (source.isZoom) {
                window.__modaoZoomState = {el: source.el, isZoom: true, originalZoom: source.el.style.zoom};
                source.el.style.zoom = String(1 / source.scale);
            } else {
                window.__modaoZoomState = {el: source.el, isZoom: false, originalTransform: source.el.style.transform};
                const cur = getComputedStyle(source.el).transform;
                source.el.style.transform = (cur && cur !== 'none' ? cur + ' ' : '') +
                    'scale(' + (1 / source.scale).toFixed(6) + ')';
            }
            return {scale: source.scale, normalized: true};
        } catch (e) {
            return {scale: source.scale, normalized: false};
        }
    }'''

    _MODAO_REVERT_ZOOM_JS = '''() => {
        const st = window.__modaoZoomState;
        if (!st) return false;
        if (st.isZoom) st.el.style.zoom = st.originalZoom;
        else st.el.style.transform = st.originalTransform;
        delete window.__modaoZoomState;
        return true;
    }'''

    # 取画布真实内容元素（.widget 等）的包围盒，而非 img/canvas 猜测
    _MODAO_BOUNDS_JS = '''() => {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, count = 0;
        const vw = window.innerWidth;
        // 归属判断优先用 #screens 主画布容器（探针实测：横向多列页面全部被它包含，
        // 页面树/右侧批注面板元素不在其中）；容器缺失时退回视口比例判断。
        const screens = document.querySelector('#screens');
        document.querySelectorAll('.widget, [id^="text-dom-"], .wImage').forEach(w => {
            const b = w.getBoundingClientRect();
            if (b.width <= 0 || b.height <= 0) return;
            if (screens) {
                if (!screens.contains(w)) return;   // 非画布内元素（页面树/批注面板）跳过
            } else if (b.left > vw * 0.85) {
                return;   // 兜底：跳过疑似右侧面板内的元素
            }
            count++;
            minX = Math.min(minX, b.left);  minY = Math.min(minY, b.top);
            maxX = Math.max(maxX, b.right); maxY = Math.max(maxY, b.bottom);
        });
        if (count === 0) return null;
        return {x: minX, y: minY, w: maxX - minX, h: maxY - minY, count};
    }'''

    # 归一化后画布可能落到负坐标/超出视口，重定位到 (pad,pad) 并按 fit 等比适配
    _MODAO_REPOSITION_JS = '''(arg) => {
        const {fit, padX, padY} = arg;
        const za = document.querySelector('.zoom-area');
        if (!za) return false;
        const cur = getComputedStyle(za).transform;
        za.style.transform = (cur && cur !== 'none' ? cur + ' ' : '') + 'scale(' + fit + ')';
        const b = document.querySelector('.mb-screen, .zoom-area');
        if (!b) return false;
        const r = b.getBoundingClientRect();
        const dx = padX - r.left, dy = padY - r.top;
        za.style.transform = (cur && cur !== 'none' ? cur + ' ' : '') +
            'scale(' + fit + ') translate(' + dx + 'px,' + dy + 'px)';
        return true;
    }'''

    # 截图输出上限：模型单图总像素上限（按总像素而非长边，避免正方形大图超限）
    _SHOT_MAX_PIXELS = 16_000_000
    _SHOT_MAX_EDGE = 25_000
    # 与 new_context 的 device_scale_factor 保持一致（4x 采样提升清晰度）
    _SHOT_SCALE = 4

    @staticmethod
    async def _safe_restore_ui(page) -> None:
        """恢复被隐藏的墨刀 UI（左侧页面树等），不恢复会导致后续点击画布失效。"""
        try:
            await page.evaluate(ModaoExtractor._MODAO_RESTORE_UI_JS)
        except Exception:
            pass

    @staticmethod
    async def _safe_revert_zoom(page) -> None:
        """回退截图时对画布做的缩放归一化。"""
        try:
            await page.evaluate(ModaoExtractor._MODAO_REVERT_ZOOM_JS)
        except Exception:
            pass

    def _postprocess_shot(self, shot_bytes: bytes, out_path: str) -> bool:
        """PIL 后处理：内存保护 → 背景裁剪去白边 → 降采样 → 高质量 JPEG。

        返回 True 表示成功写出文件。
        """
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(shot_bytes))
            # 内存保护：原始像素超上限时先等比压到上限
            if max(img.size) > self._SHOT_MAX_EDGE:
                ratio = self._SHOT_MAX_EDGE / max(img.size)
                img = img.resize(
                    (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                    Image.LANCZOS)

            # 背景裁剪：四角采样背景色，向内收敛去掉大片空白边
            try:
                gray = img.convert('L')
                arr = gray.load()
                pw, ph = img.size
                corners = []
                for cxp, cyp in [(5, 5), (pw - 6, 5), (5, ph - 6), (pw - 6, ph - 6)]:
                    sample = [arr[cxp + dx, cyp + dy]
                              for dx in (-2, 0, 2) for dy in (-2, 0, 2)]
                    sample.sort()
                    corners.append(sample[len(sample) // 2])
                bg_level = sum(corners) // len(corners)
                threshold = min(bg_level + 10, 252)

                def is_bg(px, py):
                    return arr[px, py] >= threshold

                top = 0
                while top < ph and all(is_bg(px, top) for px in range(pw)):
                    top += 1
                bottom = ph - 1
                while bottom > top and all(is_bg(px, bottom) for px in range(pw)):
                    bottom -= 1
                left = 0
                while left < pw and all(is_bg(left, py) for py in range(top, bottom + 1)):
                    left += 1
                right = pw - 1
                while right > left and all(is_bg(right, py) for py in range(top, bottom + 1)):
                    right -= 1
                crop_pad = 5
                crop = (max(0, left - crop_pad), max(0, top - crop_pad),
                        min(pw, right + 1 + crop_pad), min(ph, bottom + 1 + crop_pad))
                if crop[0] > 0 or crop[1] > 0 or crop[2] < pw or crop[3] < ph:
                    img = img.crop(crop)
            except Exception:
                pass

            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            # 按总像素等比缩放（≤1600 万），避免超出模型单图限制
            img_pixels = img.width * img.height
            if img_pixels > self._SHOT_MAX_PIXELS:
                ratio = (self._SHOT_MAX_PIXELS / img_pixels) ** 0.5
                img = img.resize(
                    (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                    Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=92, subsampling=0)
            with open(out_path, 'wb') as f:
                f.write(buf.getvalue())
            logger.info(f'[截图] 输出: {img.width}×{img.height}, {len(buf.getvalue()) / 1024:.0f}KB')
            return True
        except Exception as e:
            logger.warning(f'[截图] PIL 后处理失败，保留原图: {e}')
            try:
                with open(out_path, 'wb') as f:
                    f.write(shot_bytes)
                return True
            except Exception:
                return False

    async def _screenshot_canvas_roi(self, page, shot_path: str) -> tuple:
        """清理浮层 + 主画布区域并集截图，保证『一个画布 = 一张完整图』。

        【DOM 事实（探针核实）】墨刀 read_only 视图中：
        - 主画布渲染在 `#screens` / `.styles__StyledScreenContainer-*` 容器内，是页面主体区域；
        - class 含 `rResCanvas` 的节点其实是**页面树里的缩略图预览**（约 114x246），并非主画布；
          父画布下挂多个子视图时，页面树会并排显示多个 rResCanvas 缩略图，但它们都只是小预览。
        - 顶部 `#fixed-area`（"欢迎来到墨刀 登录后可使用标注…"）登录提示条、右侧 240px 批注/
          评论面板会挤压主画布，且进入截图后成为污染浮层。

        旧逻辑误把每个 rResCanvas 缩略图当主画布逐个截图，产出一堆带浮层的小图、且主画布被截断，
        与用户实际打开看到的（page_4/page_5 有浮层、截断）一致。

        新逻辑：
        1. 先 `_clean_overlays()` 隐藏登录条 / 批注面板 / 模态遮罩等浮层，并 Esc 收起展开态；
        2. 在**主区域**（排除左侧页面树、右侧批注面板）内收集所有可见画布节点（rResCanvas 缩略图、
           原生 canvas、#screens 主画布 img）的**并集外接框**；
        3. 单段可容纳（高 ≤ SEG_H 且宽 ≤ MAX_VIEWPORT）则输出**一张**完整主画布截图；
           仅在并集真正超高/超宽时才纵向切片（_segN 后缀），恢复直观的「一画布一图」体验。

        clip 使用页面坐标（非视口坐标），纵向切片无需滚动/重载页面；
        截图前把视口临时放大到能容纳并集框 + 缓冲，避免固定 900px 视口截断。

        返回 (main_path, segments_list)，main_path 为首段（向后兼容），
        segments_list 含全部分段（含主图）。
        """
        import math
        BUF = 60
        MAX_VIEWPORT = 6500   # 单段宽/高安全上限，避免生成巨型截图
        SEG_H = 4500          # 并集框最大高度；超过此值才纵向切片

        def _clean_overlays():
            return """() => {
                const hide = (el) => { if (el) { el.style.display = 'none'; } };
                // 顶部登录提示条
                hide(document.querySelector('#fixed-area'));
                hide(document.querySelector('.fixed_area'));
                // 登录/注册引导浮层
                document.querySelectorAll('[class*="SignUpPrompt"], [class*="signup"], [class*="LoginPrompt"], [class*="login-prompt"]')
                    .forEach(hide);
                // 右侧批注/评论面板（约 240px 宽，含关键字）
                const panels = Array.from(document.querySelectorAll('div, aside, section'));
                for (const p of panels) {
                    const w = p.getBoundingClientRect().width;
                    const t = (p.textContent || '').slice(0, 40);
                    if (w >= 180 && w <= 280 &&
                        (/批注|评论|展开全部|收起|留言|comment|note/i.test(t))) {
                        hide(p);
                    }
                }
                // 模态遮罩 / 全局弹层
                document.querySelectorAll('[class*="ModalMask"], [class*="modal-mask"], [class*="Overlay"], [class*="overlay"]')
                    .forEach(hide);
                return true;
            }"""

        def _pick_main_box():
            return """() => {
                const W = window.innerWidth;
                // 优先取墨刀主画布容器 #screens / StyledScreenContainer 的 bbox：
                // 它天然不包含左侧页面树和右侧固定批注面板，又能把画布周围悬浮的
                // 批注便签包含进来（这些便签是需求来源，不能当"浮层"截掉）。
                const mainContainers = [
                    document.querySelector('#screens'),
                    document.querySelector('[class*="StyledScreenContainer"]'),
                    document.querySelector('[class*="ScreenContainer"]'),
                ].filter(Boolean);
                for (const el of mainContainers) {
                    const st = getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    const r = el.getBoundingClientRect();
                    if (r && r.width >= 300 && r.height >= 300) {
                        return [r.x, r.y, r.width, r.height];
                    }
                }
                // 退化：取主区域内可见画布节点的并集（保留批注便签，只硬切左右极端）
                const leftCut = Math.min(260, W * 0.18);   // 只排除左侧页面树
                const rightCut = W - 40;                    // 保留右侧悬浮便签，只留 40px 边距
                const candidates = [];
                const consider = (sel, minSize) => {
                    const els = Array.from(document.querySelectorAll(sel));
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < minSize || r.height < minSize) continue;
                        const st = getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') continue;
                        const cx = r.x + r.width / 2;
                        // 只保留主区域内部的画布节点
                        if (r.x < leftCut || cx > rightCut) continue;
                        candidates.push([r.x, r.y, r.width, r.height]);
                    }
                };
                // 【关键】阈值必须够高以过滤「页面树缩略图」：
                // 墨刀 rResCanvas / 小 canvas 常是页面树里的预览缩略图（约 114x246），
                // 它不随画布切换而变化；若被并进 ROI，不同画布会裁出相同区域 →
                // 多张截图 sha 完全相同（表现为「撞图 / 切换失败」的假象）。
                consider('[class*="rResCanvas"]', 400);
                consider('canvas', 400);
                consider('#screens img', 200);
                consider('.styles__StyledScreenContainer img', 200);
                consider('[class*="ScreenContainer"] img', 200);
                if (candidates.length === 0) return null;
                // 取并集外接框
                let minX = Infinity, minY = Infinity, maxR = -Infinity, maxB = -Infinity;
                for (const [x, y, w, h] of candidates) {
                    minX = Math.min(minX, x); minY = Math.min(minY, y);
                    maxR = Math.max(maxR, x + w); maxB = Math.max(maxB, y + h);
                }
                return [minX, minY, maxR - minX, maxB - minY];
            }"""

        # 1) 隐藏墨刀 UI（顶栏/标尺/工具栏/缩略图/面板…），截图后必须恢复
        try:
            await page.evaluate(self._MODAO_HIDE_UI_JS)
        except Exception:
            pass
        try:
            await page.keyboard.press('Escape')
        except Exception:
            pass
        await page.wait_for_timeout(300)

        # 2) 【关键】把画布缩放归一化回 100%
        #    墨刀用 .zoom-area 的 transform: scale(0.46x) 缩小画布显示，直接截图得到
        #    的就是缩小后的画面（文字糊、多图看不清）。反向补偿后按原始尺寸截图。
        zoom_probe = None
        try:
            zoom_probe = await page.evaluate(self._MODAO_NORMALIZE_ZOOM_JS)
            if zoom_probe and zoom_probe.get('normalized'):
                await page.wait_for_timeout(600)
                logger.info(f"[截图] 画布缩放已归一化到100%: 原scale={zoom_probe['scale']:.3f}")
        except Exception as e:
            logger.warning(f'[截图] 缩放归一化失败，按原缩放截图: {e}')
            zoom_probe = None

        # 3) 取画布真实内容(.widget)包围盒；取不到再退回旧的 img/canvas 并集
        bounds = None
        try:
            bounds = await page.evaluate(self._MODAO_BOUNDS_JS)
        except Exception:
            bounds = None
        if bounds and bounds.get('w', 0) > 0 and bounds.get('h', 0) > 0:
            x, y, w, h = bounds['x'], bounds['y'], bounds['w'], bounds['h']
        else:
            box = await page.evaluate(_pick_main_box())
            if not box:
                await page.screenshot(path=shot_path, full_page=True, type='jpeg', quality=85)
                await self._safe_restore_ui(page)
                return shot_path, [shot_path]
            x, y, w, h = box

        # 4) 视口扩展：包围盒超出视口时扩大视口，避免只截到视口内部分
        vs = page.viewport_size or {}
        if w > vs.get('width', 1440) - 40 or h > vs.get('height', 900) - 40:
            try:
                new_w = min(max(int(w) + 80, 1280), MAX_VIEWPORT)
                new_h = min(max(int(h) + 80, 900), MAX_VIEWPORT)
                if new_w != vs.get('width') or new_h != vs.get('height'):
                    await page.set_viewport_size({'width': new_w, 'height': new_h})
                    await page.wait_for_timeout(500)
                    vs = page.viewport_size or {}
                    _b = await page.evaluate(self._MODAO_BOUNDS_JS)
                    if _b:
                        x, y, w, h = _b['x'], _b['y'], _b['w'], _b['h']
            except Exception as e:
                logger.warning(f'[截图] 视口扩展失败: {e}')

        # 5) 重定位：归一化后画布可能落在负坐标或超出视口（clip 超出会被静默裁掉）
        vs = page.viewport_size or {}
        vw_cur, vh_cur = vs.get('width', 1440), vs.get('height', 900)
        if (x < 0 or y < 0 or x + w + 20 > vw_cur or y + h + 20 > vh_cur):
            try:
                pad_align = 10
                fit = min(1.0,
                          (vw_cur - pad_align * 2 - 4) / max(w, 1),
                          (vh_cur - pad_align * 2 - 4) / max(h, 1))
                fit = max(fit, 0.01)
                if await page.evaluate(self._MODAO_REPOSITION_JS,
                                       {'fit': fit, 'padX': pad_align, 'padY': pad_align}):
                    await page.wait_for_timeout(300)
                    _b = await page.evaluate(self._MODAO_BOUNDS_JS)
                    if _b:
                        x, y, w, h = _b['x'], _b['y'], _b['w'], _b['h']
            except Exception as e:
                logger.warning(f'[截图] 画布重定位失败: {e}')

        # 6) 仍超出视口：回退原缩放，避免截取部分内容
        vs = page.viewport_size or {}
        if (w > vs.get('width', 1440) + 5 or h > vs.get('height', 900) + 5
                or x < -0.5 or y < -0.5):
            try:
                await page.evaluate(self._MODAO_REVERT_ZOOM_JS)
                await page.wait_for_timeout(400)
                _b = await page.evaluate(self._MODAO_BOUNDS_JS)
                if _b:
                    x, y, w, h = _b['x'], _b['y'], _b['w'], _b['h']
                logger.warning('[截图] 画布超出视口上限，已回退原缩放截图')
            except Exception:
                pass

        # 7) 等字体加载完成，避免 fallback 字体导致模糊/缺字
        try:
            await page.evaluate(
                'document.fonts ? Promise.race([document.fonts.ready, '
                'new Promise(r => setTimeout(r, 2000))]) : true'
            )
        except Exception:
            pass

        # 8) 截图（PNG 无损；超大图走 CDP 避免 Chrome 普通截图失败）
        pad = 10
        clip_region = {
            'x': max(0, math.floor(x) - pad),
            'y': max(0, math.floor(y) - pad),
            'width': min(math.ceil(w) + pad * 2, 12000),
            'height': min(math.ceil(h) + pad * 2, 12000),
        }

        async def _capture_region(cx, cy, cw, ch):
            """对页面坐标区域截图，返回 PNG bytes；长边超采样上限时走 CDP。"""
            _clip = {
                'x': max(0, math.floor(cx)),
                'y': max(0, math.floor(cy)),
                'width': min(math.ceil(cw), 12000),
                'height': min(math.ceil(ch), 12000),
            }
            if _clip['width'] < 10 or _clip['height'] < 10:
                return None
            try:
                if max(_clip['width'], _clip['height']) * self._SHOT_SCALE > 8192:
                    cdp = await page.context.new_cdp_session(page)
                    import base64 as _b64
                    _r = await cdp.send('Page.captureScreenshot', {
                        'format': 'png',
                        'clip': {'x': _clip['x'], 'y': _clip['y'],
                                 'width': _clip['width'], 'height': _clip['height'],
                                 'scale': 1},
                        'captureBeyondViewport': False,
                    })
                    return _b64.b64decode(_r['data'])
                return await page.screenshot(clip=_clip, type='png')
            except Exception:
                try:
                    return await page.screenshot(clip=_clip, type='png')
                except Exception:
                    return None

        if clip_region['width'] < 10 or clip_region['height'] < 10:
            await page.screenshot(path=shot_path, full_page=True, type='jpeg', quality=85)
            await self._safe_restore_ui(page)
            await self._safe_revert_zoom(page)
            return shot_path, [shot_path]

        # 9) 超宽/超高画布按段截图（横向/纵向都要切），每段独立后处理再喂视觉模型，
        #     避免把整幅大图降采样后细节丢失（这就是"改了包围盒还有截断"的根源）。
        SEG_W, SEG_H = 2000, 2000   # 单段 CSS 边长上限（4x 采样 ≈ 8000 物理像素，低于 CDP 8192 上限）
        OVERLAP = 60                # 相邻段重叠，避免切缝丢失内容
        _step_w = SEG_W - OVERLAP
        _step_h = SEG_H - OVERLAP
        _n_cols = max(1, math.ceil(clip_region['width'] / _step_w))
        _n_rows = max(1, math.ceil(clip_region['height'] / _step_h))
        _need_slice = _n_cols > 1 or _n_rows > 1

        # 主图始终截取（前端展示 screenshot 字段用），后处理降采样到模型可接受尺寸
        _main_bytes = await _capture_region(clip_region['x'], clip_region['y'],
                                            clip_region['width'], clip_region['height'])
        if _main_bytes:
            self._postprocess_shot(_main_bytes, shot_path)

        segments_paths: List[str] = []
        if _need_slice:
            _stem = os.path.splitext(os.path.basename(shot_path))[0]
            _seg_no = 1
            for _r in range(_n_rows):
                for _c in range(_n_cols):
                    _sx = clip_region['x'] + _c * _step_w
                    _sy = clip_region['y'] + _r * _step_h
                    _sw = min(SEG_W, clip_region['x'] + clip_region['width'] - _sx)
                    _sh = min(SEG_H, clip_region['y'] + clip_region['height'] - _sy)
                    if _sw < 10 or _sh < 10:
                        continue
                    _seg_bytes = await _capture_region(_sx, _sy, _sw, _sh)
                    if not _seg_bytes:
                        continue
                    _seg_path = os.path.join(self.shot_dir, f'{_stem}_seg{_seg_no}.jpg')
                    if self._postprocess_shot(_seg_bytes, _seg_path):
                        segments_paths.append(_seg_path)
                    _seg_no += 1
            if segments_paths:
                logger.info(f'[截图] 超宽/超高画布 {w:.0f}×{h:.0f} 切片为 {len(segments_paths)} 段'
                            f'（网格 {_n_cols}×{_n_rows}）')
                await self._safe_restore_ui(page)
                await self._safe_revert_zoom(page)
                # 主图仅用于前端展示；OCR 逐段识别各段，避免重复识别整幅压缩图
                return shot_path, segments_paths

        # 10) 单段（画布未超限或切片失败）：主图后处理已完成，直接返回
        if _main_bytes:
            await self._safe_restore_ui(page)
            await self._safe_revert_zoom(page)
            return shot_path, [shot_path]

        # 兜底：整页截图
        await page.screenshot(path=shot_path, full_page=True, type='jpeg', quality=85)
        await self._safe_restore_ui(page)
        await self._safe_revert_zoom(page)
        return shot_path, [shot_path]

    async def _page_has_visual(self, page) -> bool:
        """检测当前页面是否含实质图片或表格元素（用于判定『混合页』是否需要视觉补充）。

        - 图片：<img>（naturalWidth/Height > 60px，排除图标/占位）或有 background-image 的较大区块；
        - 表格：<table>，或墨刀/Axure 常见的多行多列网格结构。
        任一命中即返回 True。检测失败时保守返回 False（不影响主流程）。
        """
        try:
            return bool(await page.evaluate(
                """() => {
                    const bigImg = Array.from(document.images || [])
                        .some(im => (im.naturalWidth||im.width) > 60 && (im.naturalHeight||im.height) > 60);
                    if (bigImg) return true;
                    if (document.querySelector('table')) return true;
                    // 较大的背景图区块（排除极小图标）
                    const bgBlock = Array.from(document.querySelectorAll('*')).some(el => {
                        const bg = getComputedStyle(el).backgroundImage;
                        if (!bg || bg === 'none' || bg.indexOf('url(') === -1) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 120 && r.height > 120;
                    });
                    return bgBlock;
                }"""
            ))
        except Exception as e:
            logger.warning(f'页面图片/表格检测失败（按无视觉处理）: {e}')
            return False

    @staticmethod
    def _html_has_visual(html: str) -> bool:
        """检测 webshare/Axure 页面 HTML 是否含图片或表格（用于『混合页』视觉补充判定）。"""
        if not html:
            return False
        low = html.lower()
        return ('<img' in low) or ('<table' in low) or ('background-image' in low)

    @staticmethod
    def _parse_visual_payload(content: str) -> Dict[str, Any]:
        text = (content or '').strip()
        match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
        candidates = (text, text[text.find('{'):text.rfind('}') + 1])
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        # Vision models occasionally omit quotes around an ASCII JSON key while
        # otherwise returning a complete payload (for example `,result_fields:`).
        # Repair keys only; never alter cell values or synthesize missing content.
        for candidate in candidates:
            repaired = re.sub(
                r'([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:',
                r'\1"\2":',
                candidate,
            )
            try:
                value = json.loads(repaired)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        # JSON is a YAML subset; SafeLoader also accepts harmless omissions
        # such as an unquoted mapping key that strict JSON rejects.
        try:
            import yaml
            for candidate in candidates:
                value = yaml.safe_load(candidate)
                if isinstance(value, dict):
                    return value
        except Exception:
            pass
        return {}

    @staticmethod
    def _validate_visual_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
        rows = artifact.get('rows') if isinstance(artifact.get('rows'), list) else []
        columns = artifact.get('columns') if isinstance(artifact.get('columns'), list) else []
        result = {
            'row_count': len(rows),
            'column_count': len(columns),
            'shape_valid': bool(rows),
            'duplicate_combinations': [],
            'invalid_rule_references': [],
        }
        artifact_type = artifact.get('artifact_type')
        if artifact_type == 'state_matrix':
            states = artifact.get('states') if isinstance(artifact.get('states'), list) else []
            state_names = [str(item.get('state') or '').strip() for item in states if isinstance(item, dict)]
            transitions = artifact.get('transitions') if isinstance(artifact.get('transitions'), list) else []
            invalid_transitions = []
            known = set(state_names)
            for index, transition in enumerate(transitions, 1):
                if not isinstance(transition, dict):
                    invalid_transitions.append(index)
                    continue
                source = str(transition.get('from') or '').strip()
                target = str(transition.get('to') or '').strip()
                if (source and source not in known) or (target and target not in known):
                    invalid_transitions.append(index)
            result.update({
                'state_count': len(states),
                'state_names': state_names,
                'duplicate_states': sorted({name for name in state_names if state_names.count(name) > 1}),
                'invalid_transitions': invalid_transitions,
                'structure_valid': bool(states) and all(state_names),
                'transitions_traceable': not invalid_transitions,
            })
            result['shape_valid'] = result['structure_valid']
            return result
        if artifact_type == 'state_machine':
            states = artifact.get('states') if isinstance(artifact.get('states'), list) else []
            transitions = artifact.get('transitions') if isinstance(artifact.get('transitions'), list) else []
            result.update({'state_count': len(states), 'transition_count': len(transitions),
                           'structure_valid': bool(states) and bool(transitions)})
            result['shape_valid'] = result['structure_valid']
            return result
        if artifact_type != 'decision_table':
            if rows and columns and all(isinstance(row, list) for row in rows):
                result['shape_valid'] = all(len(row) == len(columns) for row in rows)
            elif artifact_type in {'ui_screen', 'interaction_flow', 'rule_list', 'timeline',
                                   'permission_matrix', 'pricing_matrix', 'form_schema',
                                   'list_spec', 'flowchart', 'annotation'}:
                result['shape_valid'] = bool(
                    artifact.get('elements') or artifact.get('rules') or artifact.get('nodes')
                    or artifact.get('steps') or rows or artifact.get('raw_text')
                )
            return result

        dimensions = artifact.get('dimensions') if isinstance(artifact.get('dimensions'), list) else []
        names = [x.get('name') if isinstance(x, dict) else str(x) for x in dimensions]
        names = [x for x in names if x]
        result_fields = artifact.get('result_fields') or ['user_a_result', 'user_b_result']
        seen = {}
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                result['shape_valid'] = False
                continue
            key = tuple(str(row.get(name, '')) for name in names)
            if key in seen:
                result['duplicate_combinations'].append([seen[key], index])
            seen[key] = index
            if names and any(not row.get(name) for name in names):
                result['shape_valid'] = False
            if any(field not in row for field in result_fields):
                result['shape_valid'] = False

        valid_rows = set(range(1, len(rows) + 1))
        for index, rule in enumerate(artifact.get('derived_rules') or [], 1):
            refs = rule.get('source_rows') if isinstance(rule, dict) else None
            if not isinstance(refs, list) or not refs or not set(refs).issubset(valid_rows):
                result['invalid_rule_references'].append(index)

        expanded = list(rows)
        pair_keys = {'user_a_platform', 'user_a_type', 'user_b_platform', 'user_b_type'}
        if pair_keys.issubset(set(names)) and all(isinstance(row, dict) for row in rows):
            existing = {tuple(str(row.get(name, '')) for name in names) for row in rows}
            for row in rows:
                swapped = dict(row)
                swapped['user_a_platform'], swapped['user_b_platform'] = row.get('user_b_platform'), row.get('user_a_platform')
                swapped['user_a_type'], swapped['user_b_type'] = row.get('user_b_type'), row.get('user_a_type')
                swapped['user_a_result'], swapped['user_b_result'] = row.get('user_b_result'), row.get('user_a_result')
                key = tuple(str(swapped.get(name, '')) for name in names)
                if key not in existing:
                    existing.add(key)
                    expanded.append(swapped)

        expected = 1
        for dimension in dimensions:
            values = dimension.get('values') if isinstance(dimension, dict) else None
            if not values:
                expected = None
                break
            expected *= len(values)
        result.update({
            'raw_combination_count': len(rows),
            'expanded_combination_count': len(expanded),
            'expected_combination_count': expected,
            'coverage_complete': len(expanded) >= expected if expected else None,
            'symmetry_expanded': len(expanded) > len(rows),
            'expanded_rows': expanded,
            'rules_traceable': not result['invalid_rule_references'],
        })
        return result

    @classmethod
    def _merge_decision_table_fragments(cls, raw_artifacts: List[Any]) -> List[Any]:
        """Merge adjacent 4-cell identity/result cards into one decision table."""
        fragments = []
        for index, artifact in enumerate(raw_artifacts):
            if not isinstance(artifact, dict) or artifact.get('artifact_type') != 'table':
                continue
            columns = artifact.get('columns') or []
            rows = artifact.get('rows') or []
            results = rows[0] if len(rows) == 1 and isinstance(rows[0], list) else []
            if len(columns) != 4 or len(results) != 4:
                continue
            identities = []
            for cell in columns:
                match = re.fullmatch(r'\s*(Android|iOS)\s*(新用户|老用户)\s*', str(cell), re.I)
                if not match:
                    identities = []
                    break
                platform = 'Android' if match.group(1).lower() == 'android' else 'iOS'
                identities.append((platform, match.group(2)))
            if identities and all(str(value).strip() in ('屏蔽', '不屏蔽') for value in results):
                fragments.append((index, artifact, identities, [str(x).strip() for x in results]))
        if len(fragments) < 2:
            return raw_artifacts

        combined_rows = []
        bboxes = []
        source_ids = []
        for index, artifact, identities, results in fragments:
            source_ids.append(artifact.get('artifact_id') or f'fragment-{index + 1}')
            if isinstance(artifact.get('bbox'), list) and len(artifact['bbox']) == 4:
                bboxes.append(artifact['bbox'])
            for offset in (0, 2):
                combined_rows.append({
                    'user_a_platform': identities[offset][0],
                    'user_a_type': identities[offset][1],
                    'user_b_platform': identities[offset + 1][0],
                    'user_b_type': identities[offset + 1][1],
                    'user_a_result': results[offset],
                    'user_b_result': results[offset + 1],
                    'source_fragment': source_ids[-1],
                })
        bbox = None
        if bboxes:
            bbox = [min(x[0] for x in bboxes), min(x[1] for x in bboxes),
                    max(x[2] for x in bboxes), max(x[3] for x in bboxes)]
        decision_table = {
            'artifact_type': 'decision_table',
            'title': next((item[1].get('title') for item in fragments if item[1].get('title')), '决策表'),
            'bbox': bbox,
            'confidence': min(float(item[1].get('confidence') or 0) for item in fragments),
            'preconditions': {'对新用户屏蔽': '是'},
            'dimensions': [
                {'name': 'user_a_platform', 'values': ['Android', 'iOS']},
                {'name': 'user_a_type', 'values': ['新用户', '老用户']},
                {'name': 'user_b_platform', 'values': ['Android', 'iOS']},
                {'name': 'user_b_type', 'values': ['新用户', '老用户']},
            ],
            'result_fields': ['user_a_result', 'user_b_result'],
            'rows': combined_rows,
            'derived_rules': [],
            'source_fragments': source_ids,
        }
        fragment_indexes = {item[0] for item in fragments}
        return [item for index, item in enumerate(raw_artifacts) if index not in fragment_indexes] + [decision_table]

    @classmethod
    def _promote_numbered_state_tables(cls, raw_artifacts: List[Any]) -> List[Any]:
        """Promote a numbered state specification table without rewriting its cells."""
        promoted = []
        for artifact in raw_artifacts:
            if not isinstance(artifact, dict) or artifact.get('artifact_type') != 'table':
                promoted.append(artifact)
                continue
            columns = [str(value).strip() for value in (artifact.get('columns') or [])]
            rows = artifact.get('rows') or []
            if len(columns) != 2 or columns[0] not in ('编号', '序号') or not rows:
                promoted.append(artifact)
                continue
            states = []
            rules = []
            for row_index, row in enumerate(rows, 1):
                if not isinstance(row, list) or len(row) != 2:
                    continue
                raw_spec = str(row[1] or '').strip()
                match = re.search(r'标题\s*[：:]\s*([^\n，。,；;]+)', raw_spec)
                if match:
                    states.append({
                        'state': match.group(1).strip(),
                        'raw_spec': raw_spec,
                        'source_row': row_index,
                    })
                else:
                    rules.append({'raw_rule': raw_spec, 'source_row': row_index})
            if len(states) < 2:
                promoted.append(artifact)
                continue
            upgraded = dict(artifact)
            upgraded.update({
                'artifact_type': 'state_matrix',
                'title': artifact.get('title') or '状态矩阵',
                'states': states,
                'rules': rules,
                'transitions': artifact.get('transitions') or [],
                'source_table': {'columns': artifact.get('columns'), 'rows': rows},
            })
            promoted.append(upgraded)
        return promoted

    @staticmethod
    def _state_matrix_from_text(text: str) -> Optional[Dict[str, Any]]:
        """Build a minimal traceable state matrix from repeated literal title specs."""
        content = str(text or '')
        matches = list(re.finditer(r'标题\s*[：:]\s*([^\\"\n，。,；;]+)', content))
        states = []
        seen = set()
        for index, match in enumerate(matches):
            state_name = match.group(1).strip()
            if not state_name or state_name in seen:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else min(len(content), match.end() + 500)
            raw_spec = content[match.start():end].strip(' ,]}')
            seen.add(state_name)
            states.append({'state': state_name, 'raw_spec': raw_spec, 'source': 'literal_text_fallback'})
        if len(states) < 2:
            return None
        return {
            'artifact_type': 'state_matrix',
            'title': '状态矩阵',
            'confidence': 0.6,
            'raw_text': content,
            'states': states,
            'transitions': [],
            'rules': [],
            'source_elements': [],
            'extraction_warning': '由视觉模型原始文字确定性恢复，字段待人工复核',
        }

    @classmethod
    def _prepare_visual_payload(cls, payload: Dict[str, Any], shot_path: str) -> Dict[str, Any]:
        with open(shot_path, 'rb') as image_file:
            digest = hashlib.sha256(image_file.read()).hexdigest()
        artifacts = []
        allowed = {
            'ui', 'ui_screen', 'table', 'data_table', 'decision_table', 'state_matrix',
            'state_machine', 'state_diagram', 'flowchart', 'interaction_flow', 'rule_list',
            'timeline', 'permission_matrix', 'pricing_matrix', 'form_schema', 'list_spec',
            'annotation', 'unknown',
        }
        raw_artifacts = cls._merge_decision_table_fragments(payload.get('visual_artifacts') or [])
        raw_artifacts = cls._promote_numbered_state_tables(raw_artifacts)
        for index, raw in enumerate(raw_artifacts, 1):
            if not isinstance(raw, dict):
                continue
            artifact = dict(raw)
            if artifact.get('artifact_type') not in allowed:
                artifact['artifact_type'] = 'unknown'
            artifact['artifact_id'] = artifact.get('artifact_id') or f'VA-{index:03d}'
            artifact['source_elements'] = artifact.get('source_elements') or []
            artifact['raw_text'] = str(artifact.get('raw_text') or '').strip()
            artifact['visual_evidence'] = {
                'screenshot': os.path.basename(shot_path),
                'image_sha256': digest,
                'bbox': artifact.get('bbox'),
            }
            artifact['validation'] = cls._validate_visual_artifact(artifact)
            artifacts.append(artifact)
        return {
            # OCR 路同样剔除墨刀界面控件文字（总览/演示/标注/登录/62% 等），
            # 否则截图里的这些元素会被视觉模型忠实识别出来污染需求文本。
            'ocr_text': cls._strip_ui_noise(str(payload.get('ocr_text') or '')),
            'visual_summary': str(payload.get('visual_summary') or '').strip(),
            'visual_artifacts': artifacts,
        }

    async def _ocr_screenshot(self, vision_config, shot_path: str) -> Dict[str, Any]:
        empty = {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
        try:
            import time as _t, hashlib as _hlib
            _ocr_t0 = _t.time()

            # === OCR 缓存：按截图 sha256 去重，避免同一画布重复调用视觉模型 ===
            _cache_hit = False
            try:
                os.makedirs(OCR_CACHE_DIR, exist_ok=True)
                with open(shot_path, 'rb') as _cf:
                    _shot_digest = _hlib.sha256(_cf.read()).hexdigest()
                _cache_file = os.path.join(OCR_CACHE_DIR, f'{_shot_digest}.json')
                if os.path.exists(_cache_file):
                    with open(_cache_file, 'r', encoding='utf-8') as _cf:
                        _cached = json.loads(_cf.read())
                    print(f'[OCR_CACHE] ✓ HIT {os.path.basename(shot_path)} sha256={_shot_digest[:8]}', flush=True)
                    return _cached
                _cache_hit = False
            except Exception:
                _shot_digest = None

            from apps.requirement_analysis.models import AIModelService
            _model_label = f"{vision_config.model_type}/{vision_config.model_name}"
            print(f'[OCR_LLM] ▶ 调用 {_model_label} (role=vision_ocr) | shot={os.path.basename(shot_path)}', flush=True)
            with open(shot_path, 'rb') as image_file:
                b64 = base64.b64encode(image_file.read()).decode('utf-8')
            instruction = (
                '你是产品原型视觉结构提取器。只识别截图中实际可见的内容，禁止任何脑补/补写。\n'
                '【严格规则】\n'
                '1. 每条文字必须逐字来自截图。若截图未显示某字段（如手机号、奖励明细），该字段留空或输出空字符串，禁止推断。\n'
                '2. raw_text 必须是截图中真实存在的文字片段，禁止拼接/总结/扩展。\n'
                '3. 截图模糊/看不清的字段写"无法识别"，confidence 设 0.0-0.3。\n'
                '4. 不要输出页面中没有的逻辑、规则、奖励、限制条件等假设性内容。\n'
                '5. 忽略浏览器工具栏、墨刀页面导航、登录提示。\n'
                '6. 如果截图就是一张静态 UI（如按钮、输入框、表单），visual_artifacts 输出为空数组。\n'
                '输出JSON：{"ocr_text":"截图中所有可见文字（忠实）","visual_summary":"可见布局（不超过50字）",'
                '"visual_artifacts":[{"artifact_type":"ui_screen|data_table|decision_table|state_matrix|state_machine|flowchart|interaction_flow|rule_list|timeline|permission_matrix|pricing_matrix|form_schema|list_spec|annotation|unknown",'
                '"title":"（仅在截图可见时填）","bbox":[x1,y1,x2,y2],"confidence":0.0,"raw_text":"（仅截图中真实文字）",'
                '"source_elements":[{"element_id":"E-001","text":"逐字文字","bbox":[x1,y1,x2,y2]}]}]}\n'
                '表格类型补充：\n'
                '- 普通表格：rows 是与 columns 等长的二维数组。\n'
                '- 决策表：rows 是对象数组，每行对应一个原始小方框/组合，不得省略。\n'
                '- state_matrix：states 每项必须包含 state（截图中的状态原名），其他字段看不清留空但必须保留 raw_spec。\n'
                '- pricing_matrix / permission_matrix / interaction_flow / timeline / form_schema / list_spec 同理。\n'
                '一个区域可拆成多个互补产物；无法分类时输出 unknown 但 raw_text 留空（不脑补）。'
            )
            messages = [{'role': 'user', 'content': [
                {'type': 'text', 'text': instruction},
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ]}]
            result = await AIModelService.call_openai_compatible_api(
                vision_config, messages, max_tokens=4000
            )
            raw = result['choices'][0]['message']['content'].strip()
            _ocr_elapsed = _t.time() - _ocr_t0
            print(f'[OCR_LLM] ✓ {_model_label} 返回 {len(raw)}字符 耗时 {_ocr_elapsed:.1f}s', flush=True)
            # 诊断日志：记录视觉模型原始返回的片段（长度+前200字符），便于排查
            # 「视觉识别未返回有效内容」时模型到底返回了空串还是空 JSON，不记录任何密钥。
            try:
                _dbg_raw = f'[OCR_RAW] len={len(raw)} head={raw[:200]!r}\n'
                with open(os.path.join(self.shot_dir, '_ocr_debug.log'), 'a') as _df:
                    _df.write(_dbg_raw)
            except Exception:
                pass
            logger.info(f'[OCR_RAW] len={len(raw)} head={raw[:120]!r}')
            payload = self._parse_visual_payload(raw)
            if not payload:
                logger.warning('视觉模型未返回有效JSON，降级为普通OCR文本')
                payload = {'ocr_text': self._strip_ui_noise(raw), 'visual_artifacts': []}
            artifacts = payload.get('visual_artifacts') or []
            has_decision_table = any(
                isinstance(item, dict) and item.get('artifact_type') == 'decision_table'
                for item in artifacts
            )
            matrix_hint = any(word in raw for word in ('屏蔽', '决策表', '新用户', '老用户'))
            if matrix_hint and not has_decision_table:
                retry_instruction = (
                    '上一次未将截图中由多个小方框组成的双用户屏蔽矩阵输出为decision_table。'
                    '请重新仅输出严格JSON：每个原始小方框是rows中一行，不得用derived_rules代替rows，'
                    '不得遗漏对称或重复布局中的原始组合。artifact_type必须是decision_table；'
                    'rows字段必须为对象数组，每行含user_a_platform、user_a_type、user_b_platform、'
                    'user_b_type、user_a_result、user_b_result。仅识别截图可见内容，禁止推测。'
                )
                retry_messages = [{'role': 'user', 'content': [
                    {'type': 'text', 'text': retry_instruction},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
                ]}]
                retry_result = await AIModelService.call_openai_compatible_api(
                    vision_config, retry_messages, max_tokens=4000
                )
                retry_raw = retry_result['choices'][0]['message']['content'].strip()
                retry_payload = self._parse_visual_payload(retry_raw)
                if any(
                    isinstance(item, dict) and item.get('artifact_type') == 'decision_table'
                    for item in (retry_payload.get('visual_artifacts') or [])
                ):
                    retry_payload.setdefault('ocr_text', payload.get('ocr_text') or raw)
                    payload = retry_payload
            current_artifacts = payload.get('visual_artifacts') or []
            has_state_matrix = any(
                isinstance(item, dict) and item.get('artifact_type') == 'state_matrix'
                for item in current_artifacts
            )
            state_hint = sum(word in raw for word in (
                '断食中', '饮食中', '待开始', '迟到', '状态', '实时活动', '小组件'
            )) >= 2
            if state_hint and not has_state_matrix:
                state_instruction = (
                    '识别截图中同一功能的所有状态卡片，只输出严格JSON且保持简洁。输出格式：'
                    '{"ocr_text":"逐字文字","visual_summary":"布局","visual_artifacts":['
                    '{"artifact_type":"state_matrix","title":"原文标题","bbox":[0,0,0,0],'
                    '"confidence":0.0,"raw_text":"相关原文","source_elements":[],'
                    '"states":[{"state":"状态原名","title":"","subtitle":"",'
                    '"timer_type":"","timer_source":"","fixed_text":"",'
                    '"display_condition":"","click_action":"","raw_spec":"逐字原文",'
                    '"source_elements":[]}],"transitions":[],"rules":[]}]}。'
                    '每种可见状态必须各一项；字段看不清则留空，禁止猜测；不要输出UI坐标细节以免截断。'
                )
                state_messages = [{'role': 'user', 'content': [
                    {'type': 'text', 'text': state_instruction},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
                ]}]
                state_result = await AIModelService.call_openai_compatible_api(
                    vision_config, state_messages, max_tokens=4000
                )
                state_raw = state_result['choices'][0]['message']['content'].strip()
                state_payload = self._parse_visual_payload(state_raw)
                if any(
                    isinstance(item, dict) and item.get('artifact_type') == 'state_matrix'
                    for item in (state_payload.get('visual_artifacts') or [])
                ):
                    state_payload.setdefault('ocr_text', payload.get('ocr_text') or raw)
                    payload = state_payload
            if state_hint and not any(
                isinstance(item, dict) and item.get('artifact_type') == 'state_matrix'
                for item in (payload.get('visual_artifacts') or [])
            ):
                recovered = self._state_matrix_from_text(raw)
                if recovered:
                    payload.setdefault('visual_artifacts', []).append(recovered)
            result = self._prepare_visual_payload(payload, shot_path)
            # === 写入 OCR 缓存 ===
            if _shot_digest and result.get('ocr_text'):
                try:
                    os.makedirs(OCR_CACHE_DIR, exist_ok=True)
                    with open(_cache_file, 'w', encoding='utf-8') as _cf:
                        json.dump(result, _cf, ensure_ascii=False)
                except Exception:
                    pass
            return result
        except Exception as exc:
            logger.warning(f'视觉结构提取失败（可忽略）: {exc}')
            return empty

    # ---------- 可选增强策略：双路 DOM + 视觉 交叉融合 ----------
    # 背景：跨多原型验证（canvas 训练页 / 文本层设置页 / 排行榜活动页 / 测试+贴纸+日历页）表明，
    # 墨刀原型「DOM 文本路径」与「视觉截图路径」互补且不可互相替代：DOM 能抽到精确数值/次数/规则，
    # 视觉能理解布局/纯图/全局结构；纯 DOM 在墨刀上偶发 0 字符（异步渲染），需重试兜底。
    # 本策略把两路作为独立输入，分别抽需求后 LLM 交叉融合，标注
    # sources(dom/vision/both) / confidence(high/medium/low) / evidence(A/B/C/D)。
    # 作为可选策略：默认关闭（dual_fusion=False），保持既有行为不变；由调用方（如 modao_workflow）
    # 在采用 analyzer 配置时开启并传入 fusion_config。

    async def _run_ocr_batch(self, pages, vision_config, warnings, prototype_id=None):
        """并发批量视觉 OCR：把原本每页串行的一次视觉模型调用，改为受信号量限流的并发调用。

        视觉 OCR 仅依赖截图文件（shot_path），与浏览器页面状态无关，因此可在所有页面
        截图完成后统一并发执行，网络 IO 并行；整体耗时 ≈ 单页 OCR 耗时 × (页数 / OCR_CONCURRENCY)。
        pages: extract 主循环收集的需要 OCR 的页面字典（含临时字段 'shot_path'）。
        """
        logger.info(f'[OCR_BATCH] START pages={len(pages)} vision_config={vision_config is not None} '
                     f'pid={prototype_id} sample_shot={pages[0].get("shot_path", "")[:60] if pages else "N/A"}')
        _dbg = f'[OCR_BATCH] ENTER pages={len(pages)} config_ok={vision_config is not None}\n'
        print(_dbg, flush=True)
        with open(os.path.join(self.shot_dir, '_ocr_debug.log'), 'a') as _df: _df.write(_dbg)
        sem = asyncio.Semaphore(OCR_CONCURRENCY)

        async def _one(p):
            _pn = p.get('page_name', '?')
            _sp = str(p.get('shot_path', ''))[:80]
            _dbg0 = f'[OCR_ONE_ENTER] {_pn} shot={_sp}\n'
            print(_dbg0, flush=True)
            with open(os.path.join(self.shot_dir, '_ocr_debug.log'), 'a') as _df: _df.write(_dbg0)
            _raise_if_terminated(prototype_id)
            async with sem:
                # 超长画布分段截图后，逐段 OCR 再合并：segments 为相对 media_root 的路径列表（含主图）；
                # 无分段时回退单张 shot_path。任何一段失败都不影响其他段。
                seg_rel = p.get('screenshot_segments') or []
                if seg_rel and isinstance(seg_rel, list):
                    seg_abs_list = [os.path.join(self.media_root, s) for s in seg_rel]
                elif p.get('shot_path'):
                    seg_abs_list = [p.get('shot_path')]
                else:
                    seg_abs_list = []
                ocr_parts: List[str] = []
                summary_parts: List[str] = []
                artifacts_all: List[Dict[str, Any]] = []
                seg_ok = 0
                for seg_abs in seg_abs_list:
                    if not seg_abs or not os.path.exists(seg_abs):
                        continue
                    try:
                        _vr = await self._ocr_screenshot(vision_config, seg_abs)
                        seg_ok += 1
                    except Exception as e:
                        logger.warning(f"页面[{p.get('page_name')}] 分段图[{os.path.basename(seg_abs)}] OCR失败（跳过）: {e}")
                        _vr = {'ocr_text': '', 'visual_summary': '', 'visual_artifacts': []}
                    if _vr.get('ocr_text'):
                        ocr_parts.append(_vr['ocr_text'])
                    if _vr.get('visual_summary'):
                        summary_parts.append(_vr['visual_summary'])
                    artifacts_all.extend(_vr.get('visual_artifacts') or [])
                if seg_ok:
                    _dbg = f'[OCR_ONE] {p.get("page_name")} OK segs={seg_ok}/{len(seg_abs_list)} ocr_len={sum(len(x) for x in ocr_parts)}\n'
                else:
                    _dbg = f'[OCR_ONE] {p.get("page_name")} FAIL(no segment OCRed)\n'
                print(_dbg, flush=True)
                with open(os.path.join(self.shot_dir, '_ocr_debug.log'), 'a') as _df: _df.write(_dbg)
            ocr_text = '\n'.join(ocr_parts)
            summary_text = '\n'.join(summary_parts)
            vision_text = ocr_text
            if summary_text:
                vision_text = (vision_text + '\n' + summary_text).strip()
            p['vision_text'] = vision_text
            p['visual_summary'] = summary_text
            p['visual_artifacts'] = artifacts_all
            p['vision_success'] = bool(ocr_text or summary_text or artifacts_all)
            if not p['vision_success']:
                p.setdefault('extraction_warnings', []).append('视觉识别未返回有效内容')
            # 兼容字段 text = DOM 文本 + 视觉 OCR 补充，维持 run_struct 既有消费方式
            base = (p.get('dom_text') or '')
            p['text'] = (base + '\n[视觉OCR补充]\n' + ocr_text) if ocr_text else base
            p.pop('shot_path', None)

        await asyncio.gather(*[_one(p) for p in pages])
        logger.info(f'[OCR_BATCH] DONE success_count='
                     f'{sum(1 for p in pages if p.get("vision_success"))}/{len(pages)}')
        _dbg = f'[OCR_BATCH] DONE ok={sum(1 for p in pages if p.get("vision_success"))}/{len(pages)}\n'
        print(_dbg, flush=True)
        with open(os.path.join(self.shot_dir, '_ocr_debug.log'), 'a') as _df: _df.write(_dbg)

    async def dual_fusion_requirements(self, pages, fusion_config, prototype_id=None):
        """对已被分别记录 dom_text / vision_text 的各页，做双路 LLM 交叉融合。

        pages: extract / extract_webshare_sync 在 dual_fusion=True 时产出的页面列表，
               每项含 'page_name' / 'dom_text' / 'vision_text' / 'dom_text_len'。
        fusion_config: 具备文本能力的 AIModelConfig（如 analyzer）。
        返回融合需求数组（含 sources/confidence/evidence）。失败返回空列表，不抛异常。
        """
        _raise_if_terminated(prototype_id)
        if not fusion_config:
            return []
        from apps.requirement_analysis.models import AIModelService
        dom_all, vis_all = [], []
        dom_total = 0
        for p in pages:
            name = p.get('page_name') or f"第{p.get('page_no', '?')}页"
            dom = (p.get('dom_text') or '').strip()
            vis = (p.get('vision_text') or '').strip()
            # 右侧批注面板/评论内容：extract 已抓取进 annotations 但此前未进分析链路，
            # 这里拼入 DOM 文本块，确保 LLM 能看到画布外的批注（画布内便签已在 dom_text 中）。
            ann = p.get('annotations') or []
            if isinstance(ann, list):
                ann_text = '\n'.join(str(a) for a in ann if a).strip()
            else:
                ann_text = ''
            if dom:
                block = f"### {name}\n{dom}"
                if ann_text:
                    block += f"\n\n[批注/评论]\n{ann_text}"
                dom_all.append(block)
                dom_total += len(block)
            if vis:
                vis_all.append(f"### {name}\n{vis}")
        if not dom_all and not vis_all:
            _dbg = '[FUSION] both dom/vis empty -> return []\n'
            print(_dbg, flush=True)
            with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg)
            return []
        _dbg = f'[FUSION] START dom_pages={len(dom_all)} vis_pages={len(vis_all)} dom_total={dom_total}\n'
        print(_dbg, flush=True)
        with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg)
        # 并行优化：DOM 抽取与视觉抽取两路输入完全独立，用 gather 同时发起，
        # 把两次串行 LLM 调用压缩为一次（耗时 ≈ max(dom, vis) 而非 sum），最后再融合。
        if dom_all and vis_all:
            dom_reqs, vis_reqs = await asyncio.gather(
                self._dual_extract_one(AIModelService, fusion_config, dom_all),
                self._dual_extract_one(AIModelService, fusion_config, vis_all),
            )
        elif dom_all:
            dom_reqs = await self._dual_extract_one(AIModelService, fusion_config, dom_all)
            vis_reqs = []
        else:
            dom_reqs = []
            vis_reqs = await self._dual_extract_one(AIModelService, fusion_config, vis_all)
        fused = await self._dual_fuse(AIModelService, fusion_config, dom_reqs, vis_reqs, dom_total)
        _dbg = f'[FUSION] END total_fused={len(fused)}\n'
        print(_dbg, flush=True)
        with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg)
        return fused

    async def _dual_extract_one(self, aim_service, fusion_config, blocks):
        """单路分批抽取；按完整页面分组，禁止整体截断导致尾部页面丢失。"""
        batches = []
        current = []
        current_len = 0
        for block in blocks:
            # 单页异常超长时按字符切片，但每片都保留原页面标题。
            pieces = [block[i:i + 10000] for i in range(0, len(block), 10000)] or ['']
            for piece in pieces:
                if current and current_len + len(piece) > 12000:
                    batches.append(current)
                    current, current_len = [], 0
                current.append(piece)
                current_len += len(piece)
        if current:
            batches.append(current)

        requirements = []
        for batch_no, batch in enumerate(batches, 1):
            messages = [
                {'role': 'system', 'content': DUAL_EXTRACT_PROMPT},
                {'role': 'user', 'content': f'批次 {batch_no}/{len(batches)} 页面内容:\n' + '\n\n'.join(batch)},
            ]
            try:
                result = await aim_service.call_openai_compatible_api(
                    fusion_config, messages, max_tokens=16000)
                content = (result['choices'][0]['message']['content'] or '') if result else ''
                parsed = _parse_llm_json_array(content)
                _dbg = (f'[FUSION] extract_batch {batch_no}/{len(batches)} '
                        f'raw_len={len(content)} parsed={len(parsed)}\n')
                print(_dbg, flush=True)
                with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg)
                requirements.extend(parsed)
            except Exception as e:
                raise RuntimeError(f'双路抽取第 {batch_no}/{len(batches)} 批失败: {e}') from e
        return requirements

    async def _dual_fuse(self, aim_service, fusion_config, dom_reqs, vis_reqs, dom_len):
        """交叉融合两路需求数组；解析失败/截断时降级为两路并集，避免整体读取被阻断。"""
        if not dom_reqs and not vis_reqs:
            return []
        user = (
            f"DOM 可见文本长度 = {dom_len} 字符。\n\n"
            f"===== 方式A（DOM 文本）原始结果 =====\n{json.dumps(dom_reqs, ensure_ascii=False)}\n\n"
            f"===== 方式B（视觉截图）原始结果 =====\n{json.dumps(vis_reqs, ensure_ascii=False)}\n"
        )
        messages = [
            {'role': 'system', 'content': DUAL_FUSE_PROMPT},
            {'role': 'user', 'content': user},
        ]
        try:
            result = await aim_service.call_openai_compatible_api(
                fusion_config, messages, max_tokens=16000)
            content = (result['choices'][0]['message']['content'] or '') if result else ''
            parsed = _parse_llm_json_array(content)
            _dbg = (f'[FUSION] fuse_reqs dom={len(dom_reqs)} vis={len(vis_reqs)} '
                    f'raw_len={len(content)} parsed={len(parsed)}\n')
            print(_dbg, flush=True)
            with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg)
            if parsed:
                return parsed
            # 融合输出无法解析（多为输出被 max_tokens 截断）：降级为两路并集，
            # 保证阶段0门禁不因一次性解析失败而把整轮读取判失败。
            _dbg2 = '[FUSION] fuse parse failed -> fallback to union of two paths\n'
            print(_dbg2, flush=True)
            with open(os.path.join(self.shot_dir, '_fusion_debug.log'), 'a') as _df: _df.write(_dbg2)
            return list(dom_reqs or []) + list(vis_reqs or [])
        except Exception as e:
            raise RuntimeError(f'双路交叉融合失败: {e}') from e


# ---------- 模块级常量与工具（双路融合用） ----------
DUAL_EXTRACT_PROMPT = (
    "你是资深需求分析师。请根据提供的页面内容，提取其中的功能需求点。"
    "输出 JSON 数组，每条包含：source_pages(证据页面名称数组)、module(所属模块)、feature(功能点)、"
    "interaction(关键交互/边界/数值规则)、evidence(证据等级 A/B/C/D)。"
    "只输出 JSON 数组，不要解释。"
)
DUAL_FUSE_PROMPT = (
    "你是资深需求分析架构师。下面是对同一个产品原型，分别用两种方式提取的需求：\n"
    "- 方式A（DOM 文本）：基于页面 DOM 可见文本（含精确数值/次数/规则）\n"
    "- 方式B（视觉截图）：基于页面截图（含布局/纯图/全局结构）\n"
    "请融合两份结果，输出去重后的功能需求数组。每条包含：\n"
    "  source_pages(合并两路证据页面名称数组), module(所属模块), feature(功能点), interaction(关键交互/边界),\n"
    "  evidence(取两种方式中最严格的证据等级 A/B/C/D),\n"
    "  sources(数组，元素取自 'dom' 或 'vision'，表示该需求被哪种方式支持),\n"
    "  confidence('high' 若 dom 与 vision 都支持 / 'medium' 若仅一种且 evidence>=B / 'low' 若仅一种且 evidence 为 C 或 D)\n"
    "只输出 JSON 数组，不要解释。"
)


def _parse_llm_json_array(raw):
    """鲁棒解析 LLM 返回的 JSON 数组；失败返回空列表。"""
    if not raw:
        return []
    s = (raw or '').strip()
    try:
        val = json.loads(s)
        if isinstance(val, list):
            return val
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(1).strip())
            if isinstance(val, list):
                return val
        except Exception:
            pass
    i, j = s.find("["), s.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            val = json.loads(s[i:j + 1])
            if isinstance(val, list):
                return val
        except Exception:
            pass
    return []
