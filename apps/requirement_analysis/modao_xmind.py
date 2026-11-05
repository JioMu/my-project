# -*- coding: utf-8 -*-
"""
modao_xmind.py
结构化摘要（requirement_summary YAML）与 XMind 文件的互转工具。

设计目标（方案 A）：
- 在「需求结构化」阶段额外生成一个可编辑的 XMind 图，便于人精准理解需求；
- 下载到本地修改后，可上传回系统，仅覆盖结构化摘要（requirement_summary），不联动下游阶段；
- 不依赖第三方库（xmind 未预装）：XMind 2021+ 的 content.json 格式本质是 zip + JSON，
  标准库 zipfile/json 即可生成与解析，闭环可控。

XMind 树层级约定：
  根节点   : 需求标题（proto.title）
  二级节点 : 每个 module（name）
  三级约定 :
    "⚑ 优先级: P0"             -> 还原 module.priority
    "📌 优先级来源: 原文明示"    -> 还原 module.priority_source
    "🔑 关键特征"              -> 其下子节点为各 key_feature 标题
    "📄 子页面"               -> 其下子节点为各 sub_page
其余未被识别前缀的二级/三级节点一律忽略（允许用户自由增删备注节点）。
"""
import io
import json
import zipfile

# 约定前缀（导入时用于还原；导出时生成）
PRIORITY_PREFIX = "⚑ 优先级: "
PRIORITY_SRC_PREFIX = "📌 优先级来源: "
KEY_FEATURES_LABEL = "🔑 关键特征"
SUB_PAGES_LABEL = "📄 子页面"

MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0">
  <file-entry full-path="content.json" media-type="application/vnd.xmind.workbook; charset=UTF-8"/>
  <file-entry full-path="metadata.json" media-type="application/json"/>
</manifest>"""


def _feature_text_of(item) -> str:
    """取 key_features 条目的文本，兼容对象（新格式）与裸字符串（老格式）。"""
    if isinstance(item, dict):
        for key in ("text", "title", "name", "description"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if item is None:
        return ""
    return str(item).strip()


def _node(title, note=None, children=None):
    """构造一个 XMind content.json 节点。"""
    n = {
        "id": "node-%s" % _short_uid(title),
        "title": title,
    }
    if note:
        n["notes"] = {"plain": {"content": note}}
    if children:
        n["children"] = {"attached": children}
    return n


_uid_counter = [0]


def _short_uid(seed=""):
    _uid_counter[0] += 1
    return "%d-%s" % (_uid_counter[0], abs(hash(seed)) % 100000)


def build_xmind_json(proto, summary_yaml):
    """根据 requirement_summary（已解析 dict）生成 XMind content.json 的 workbook 结构。"""
    root_title = (getattr(proto, "title", None) or "需求结构化").strip() or "需求结构化"
    modules = []
    if isinstance(summary_yaml, dict):
        modules = summary_yaml.get("modules", []) or []

    module_nodes = []
    for m in modules:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        if not name:
            continue
        children = []
        priority = (m.get("priority") or "").strip()
        if priority:
            children.append(_node(PRIORITY_PREFIX + priority))
        priority_source = (m.get("priority_source") or "").strip()
        if priority_source:
            children.append(_node(PRIORITY_SRC_PREFIX + priority_source))
        kf = m.get("key_features") or []
        if isinstance(kf, list) and kf:
            kf_children = []
            for f in kf:
                if isinstance(f, dict):
                    # 新格式 key_features 用 text 字段；老格式可能是 title/name。
                    ft = (f.get("text") or f.get("title") or f.get("name") or "").strip()
                    if not ft:
                        continue
                    note = (f.get("desc") or f.get("description") or "").strip()
                    # 有稳定编号时把编号带进 XMind，便于导图与测试点互相回指。
                    fid = (f.get("id") or "").strip() if isinstance(f.get("id"), str) else ""
                    if fid:
                        ft = f"{fid} {ft}"
                    if not note and (f.get("involves_api") is True):
                        note = "涉及接口"
                    kf_children.append(_node(ft, note=note))
                elif isinstance(f, str) and f.strip():
                    kf_children.append(_node(f.strip()))
            if kf_children:
                children.append(_node(KEY_FEATURES_LABEL, children=kf_children))
        sp = m.get("sub_pages") or []
        if isinstance(sp, list) and sp:
            sp_children = [
                _node(s.strip()) for s in sp if isinstance(s, str) and s.strip()
            ]
            # sub_pages 项也可能是 dict
            for s in sp:
                if isinstance(s, dict) and (s.get("title") or s.get("name")):
                    sp_children.append(_node((s.get("title") or s.get("name")).strip()))
            if sp_children:
                children.append(_node(SUB_PAGES_LABEL, children=sp_children))
        module_nodes.append(_node(name, children=children))

    root = _node(root_title, children=module_nodes)
    workbook = [{"id": "sheet-1", "class": "sheet", "title": root_title, "rootTopic": root}]
    return workbook


def export_xmind_bytes(proto, summary_yaml):
    """返回 XMind 文件字节流。"""
    workbook = build_xmind_json(proto, summary_yaml)
    content = json.dumps(workbook, ensure_ascii=False, indent=2)
    metadata = json.dumps({"creator": {"name": "TestHub"}, "dataStructureVersion": 2},
                          ensure_ascii=False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.xml", MANIFEST)
        zf.writestr("content.json", content)
        zf.writestr("metadata.json", metadata)
    return buf.getvalue()


def parse_xmind_bytes(data):
    """解析上传的 XMind 字节流，返回 (root_title, modules_list)。

    modules_list 中每项为 dict: {name, priority, priority_source, key_features:[...], sub_pages:[...]}
    无法解析时返回 (None, [])。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            content_name = "content.json" if "content.json" in names else (
                "content.xml" if "content.xml" in names else None)
            if not content_name:
                return None, []
            raw = zf.read(content_name).decode("utf-8")
    except Exception:
        return None, []

    workbook = None
    try:
        workbook = json.loads(raw)
    except Exception:
        # 旧版 content.xml：不在此实现 XML 解析（仅支持自生成的 content.json），避免误解析
        return None, []

    if isinstance(workbook, list) and workbook:
        sheet = workbook[0]
    elif isinstance(workbook, dict):
        sheet = workbook
    else:
        return None, []
    root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
    if not isinstance(root, dict):
        return None, []

    root_title = (root.get("title") or "").strip()
    modules = []
    attached = (root.get("children") or {}).get("attached") or []
    for mnode in attached:
        if not isinstance(mnode, dict):
            continue
        mname = (mnode.get("title") or "").strip()
        if not mname:
            continue
        mod = {
            "name": mname,
            "priority": "",
            "priority_source": "",
            "key_features": [],
            "sub_pages": [],
        }
        mchildren = (mnode.get("children") or {}).get("attached") or []
        for c in mchildren:
            if not isinstance(c, dict):
                continue
            ctitle = (c.get("title") or "").strip()
            if not ctitle:
                continue
            if ctitle.startswith(PRIORITY_PREFIX):
                mod["priority"] = ctitle[len(PRIORITY_PREFIX):].strip()
            elif ctitle.startswith(PRIORITY_SRC_PREFIX):
                mod["priority_source"] = ctitle[len(PRIORITY_SRC_PREFIX):].strip()
            elif ctitle == KEY_FEATURES_LABEL:
                for kf in (c.get("children") or {}).get("attached") or []:
                    if not isinstance(kf, dict):
                        continue
                    ktitle = (kf.get("title") or "").strip()
                    if not ktitle:
                        continue
                    # 导出时写过 "F-001 功能点文本"，回读时把编号拆回 id，
                    # 并按新格式还原为对象，避免 XMind 往返把 key_features 降级成裸字符串。
                    fid, _, body = ktitle.partition(" ")
                    if fid and body and fid.startswith("F-") and fid[2:].isdigit():
                        mod["key_features"].append({"id": fid, "text": body.strip()})
                    else:
                        mod["key_features"].append(ktitle)
            elif ctitle == SUB_PAGES_LABEL:
                for sp in (c.get("children") or {}).get("attached") or []:
                    if isinstance(sp, dict) and (sp.get("title") or "").strip():
                        mod["sub_pages"].append(sp["title"].strip())
            # 其余前缀节点忽略
        modules.append(mod)
    return root_title, modules


def modules_to_yaml(modules, root_title=""):
    """把解析出的 modules 还原为结构化 YAML 字符串（保留其余顶层字段由调用方合并）。"""
    import yaml
    out = {"modules": [_normalize_module_features(m) for m in (modules or [])]}
    if root_title:
        out["_title"] = root_title
    return yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=120)


def _normalize_module_features(module):
    """把 key_features 的裸字符串项升级为新格式对象，保证下游能拿到 id/text。"""
    if not isinstance(module, dict):
        return module
    feats = module.get("key_features")
    if not isinstance(feats, list):
        return module
    upgraded = []
    for idx, item in enumerate(feats, 1):
        if isinstance(item, dict):
            upgraded.append(item)
            continue
        text = _feature_text_of(item)
        if not text:
            continue
        upgraded.append({"id": "F-%03d" % idx, "text": text})
    if upgraded == feats:
        return module
    out = dict(module)
    out["key_features"] = upgraded
    return out
