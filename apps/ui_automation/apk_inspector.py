# -*- coding: utf-8 -*-
"""APK 包信息解析（不依赖 aapt / apktool，纯 zip 结构解析）。

背景
----
MVP 链路「APK 上传 → REA 分析」第一步要拿到「包名 / 版本 / 启动 Activity /
权限清单」。直觉是用 aapt2，但容器里通常没有 Android SDK，装一套
build-tools 又会让后端镜像再胖几百 MB。

AndroidManifest.xml 在 APK 里是 **编译后的二进制 XML（AXML）**，结构本身
很简单，所以这里直接按 AXML 规范解析，零外部依赖：

    ResChunk_header(8)  total_size / header_size / chunk_type
    ├─ STRING_POOL  (0x0001)  UTF-8 或 UTF-16 字符串池
    └─ XML chunk    (0x0003)
         ├─ START_NAMESPACE / END_NAMESPACE
         ├─ START_ELEMENT (0x0102)  body: ns, name, attrStart, attrSize, attrCount, ...
         │                          attrs: ns, name, rawValue, typedValue{size,res0,type,data}
         ├─ END_ELEMENT   (0x0103)
         └─ RESOURCE_MAP  (0x0180)  attr name 索引 → 系统 attr resource id

属性名（如 `package` / `name` / `exported`）在 AXML 里通常以**空字符串**
出现，只有属性索引，必须通过 RESOURCE_MAP 映射成 android:xxx 的资源 id
（0x0101xxxx）才认得出。这是纯手写解析最容易漏的一步。

用法
----
    info = inspect_apk('/app/media/apk/xxx.apk')
    info['package_name'], info['version_name'], info['launch_activity'],
    info['permissions'], info['activities'], info['error']

失败时**不抛异常**，在 `error` 字段给出可读原因（前端直接展示），
因为「上传了一个损坏/非 APK 文件」是常见输入，不该变成 500。
"""
import hashlib
import logging
import re
import struct
import zipfile

logger = logging.getLogger(__name__)

# ---- AXML chunk types ----
_RES_STRING_POOL_TYPE = 0x0001
_RES_XML_TYPE = 0x0003
_RES_XML_START_ELEMENT_TYPE = 0x0102
_RES_XML_END_ELEMENT_TYPE = 0x0103
_RES_XML_RESOURCE_MAP_TYPE = 0x0180

# ---- Res_value data types ----
_TYPE_STRING = 0x03
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOLEAN = 0x12
_TYPE_REFERENCE = 0x01

#: 需要识别为「布尔真」的类型值
_TRUE_TYPES = {_TYPE_INT_BOOLEAN, _TYPE_INT_DEC, _TYPE_INT_HEX}

#: 系统 attr 资源 id → 属性名（只收我们关心的，避免维护一整张表）
_ATTR_ID_TO_NAME = {
    0x01010003: 'name',
    0x01010006: 'permission',
    0x01010010: 'exported',
    0x01010024: 'value',            # meta-data / <data android:value>
    0x01010025: 'resource',
    0x01010027: 'authorities',      # provider
    0x01010026: 'scheme',
    0x0101021B: 'versionCode',
    0x0101021C: 'versionName',
    0x01010270: 'targetActivity',
    0x0101020C: 'minSdkVersion',
    0x0101021D: 'targetSdkVersion',
    0x0101020D: 'maxSdkVersion',
    0x010102B4: 'required',
    0x01010327: 'parentActivityName',
    0x0101036B: 'usesCleartextTraffic',
    0x01010572: 'networkSecurityConfig',
}

#: Android 的危险权限（危险操作清单 / 敏感能力提示用）
DANGEROUS_PERMISSIONS = {
    'android.permission.CAMERA': '相机',
    'android.permission.RECORD_AUDIO': '录音',
    'android.permission.ACCESS_FINE_LOCATION': '精确定位',
    'android.permission.ACCESS_COARSE_LOCATION': '粗略定位',
    'android.permission.READ_CONTACTS': '读联系人',
    'android.permission.WRITE_CONTACTS': '写联系人',
    'android.permission.READ_SMS': '读短信',
    'android.permission.SEND_SMS': '发短信',
    'android.permission.READ_CALL_LOG': '读通话记录',
    'android.permission.CALL_PHONE': '拨打电话',
    'android.permission.READ_PHONE_STATE': '读设备信息',
    'android.permission.READ_EXTERNAL_STORAGE': '读存储',
    'android.permission.WRITE_EXTERNAL_STORAGE': '写存储',
    'android.permission.MANAGE_EXTERNAL_STORAGE': '管理全部文件',
    'android.permission.BODY_SENSORS': '身体传感器',
    'android.permission.SYSTEM_ALERT_WINDOW': '悬浮窗',
    'android.permission.REQUEST_INSTALL_PACKAGES': '安装应用',
    'android.permission.QUERY_ALL_PACKAGES': '读取已安装应用列表',
}

#: 包名合法性（Android 反域名规则，允许单段如 `com` 之外常见变体）
_PKG_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')


class _StringPool(object):
    """AXML 字符串池（支持 UTF-8 / UTF-16 两种编码）。"""

    def __init__(self, data, offset, header_size, chunk_size):
        self.strings = []
        try:
            count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
                '<IIIII', data, offset + 8)
        except struct.error:
            return
        self.utf8 = bool(flags & (1 << 8))
        offsets = []
        pos = offset + header_size
        for _ in range(count):
            try:
                (soff,) = struct.unpack_from('<I', data, pos)
            except struct.error:
                break
            offsets.append(soff)
            pos += 4

        base = offset + strings_start
        for soff in offsets:
            try:
                self.strings.append(self._read_one(data, base + soff))
            except Exception:  # noqa: BLE001
                self.strings.append('')

    def _read_one(self, data, pos):
        if self.utf8:
            # UTF-8 变长：先 utf16 字符数（可 1~2 字节），再字节数（1~2 字节）
            _chars, pos = self._read_len8(data, pos)
            blen, pos = self._read_len8(data, pos)
            raw = data[pos:pos + blen]
            return raw.decode('utf-8', 'replace')
        # UTF-16：字符数（2 字节，高位标志表示后续 4 字节）
        (wlen,) = struct.unpack_from('<H', data, pos)
        pos += 2
        if wlen & 0x8000:
            (low,) = struct.unpack_from('<H', data, pos)
            pos += 2
            wlen = ((wlen & 0x7FFF) << 16) | low
        raw = data[pos:pos + wlen * 2]
        return raw.decode('utf-16-le', 'replace')

    @staticmethod
    def _read_len8(data, pos):
        (n,) = struct.unpack_from('<B', data, pos)
        pos += 1
        if n & 0x80:
            (low,) = struct.unpack_from('<B', data, pos)
            pos += 1
            n = ((n & 0x7F) << 8) | low
        return n, pos

    def get(self, idx):
        if 0 <= idx < len(self.strings):
            return self.strings[idx]
        return ''

    # 便于排查：字符串数是判断「池是否解析成功」的第一手证据
    def __len__(self):
        return len(self.strings)


def _parse_start_element(data, pos, header_size, pool, res_map):
    if pool is None:
        return None
    # ⚠️ START_ELEMENT 的 chunk header 是 16 字节（type/size/lineNumber/comment），
    #    不是通用的 8 字节，必须用 chunk 里的 headerSize 而不是文件头的。
    body = pos + header_size
    try:
        _ns, name_idx, attr_start, attr_size, attr_count, _id_idx, _cls_idx, _style_idx = struct.unpack_from(
            '<IIHHHHHH', data, body)
    except struct.error:
        return None
    # 结构体必须先完整解包成 tuple 再赋值：写成多个目标时 Python 逐个取迭代器，
    # 一旦中途某一项取值失败就会留下"部分目标已被写入"的诡异状态，排查极其费时。
    tag = pool.get(name_idx)
    if not tag:
        return None

    attrs = {}
    base = body + attr_start
    for i in range(attr_count):
        off = base + i * attr_size
        try:
            _ans, aname_idx, raw_idx, _vsize, _res0, vtype, vdata = struct.unpack_from(
                '<IIIHBBI', data, off)
        except struct.error:
            break

        # 属性名：优先用 resource map 反查系统 attr；取不到再用字符串池的明文名
        name = _ATTR_ID_TO_NAME.get(res_map[aname_idx]) if aname_idx < len(res_map) else None
        if not name:
            name = pool.get(aname_idx)
            if name.startswith('android:'):
                name = name.split(':', 1)[1]
        if not name:
            # 纯数字名（未映射的内部 attr）跳过，避免污染结果
            continue

        if vtype == _TYPE_STRING:
            value = pool.get(vdata)
        elif vtype == _TYPE_INT_BOOLEAN:
            value = bool(vdata)
        elif vtype in (_TYPE_INT_DEC, _TYPE_INT_HEX):
            value = vdata
        elif vtype == _TYPE_REFERENCE:
            # 引用型（如 @string/app_name）解析不到真实文案，记为引用 id
            value = '#ref:0x%08x' % vdata
        else:
            value = pool.get(raw_idx) if raw_idx else None

        if value in (None, ''):
            continue
        attrs[name] = value

        # 自定义 meta-data 等：同时保留 rawValue 明文（有时 vtype 不是 string）
        if raw_idx and 'raw' not in attrs:
            raw = pool.get(raw_idx)
            if raw and raw != value:
                attrs['raw'] = raw
    return (tag, attrs)


def _parse_with_stack(elements):
    """把 START/END 事件流还原成带路径的节点列表。

    事件流里 None 表示 END_ELEMENT（见 `_parse_axml_events`），
    节点 path 形如 `manifest/application/activity/intent-filter/action`，
    用于判断「这个 action 属于哪个 activity」。
    """
    stack = []
    rows = []
    for item in elements:
        if item is None:
            if stack:
                stack.pop()
            continue
        tag, attrs = item
        rows.append({'tag': tag, 'attrs': attrs, 'depth': len(stack),
                     'path': '/'.join(stack)})
        stack.append(tag)
    return rows


def _parse_axml_events(data):
    """解析 AXML，返回 [(tag, attrs) | None]，None 表示一个 END_ELEMENT。"""
    if len(data) < 8:
        raise ValueError('AndroidManifest.xml 过短，不是有效的二进制 XML')
    (ctype, header_size, total) = struct.unpack_from('<HHI', data, 0)
    if ctype != _RES_XML_TYPE:
        raise ValueError('AndroidManifest.xml 不是二进制 XML（chunk type=%#x）' % ctype)
    total = min(total, len(data))

    pool = None
    res_map = []
    events = []
    pos = header_size
    while pos + 8 <= total:
        try:
            ch_type, ch_header, ch_size = struct.unpack_from('<HHI', data, pos)
        except struct.error:
            break
        if ch_size <= 0 or pos + ch_size > total:
            break
        if ch_type == _RES_STRING_POOL_TYPE and pool is None:
            pool = _StringPool(data, pos, ch_header, ch_size)
        elif ch_type == _RES_XML_RESOURCE_MAP_TYPE:
            n = (ch_size - ch_header) // 4
            res_map = list(struct.unpack_from('<%dI' % n, data, pos + ch_header)) if n else []
        elif ch_type == _RES_XML_START_ELEMENT_TYPE:
            events.append(_parse_start_element(data, pos, ch_header, pool, res_map))
        elif ch_type == _RES_XML_END_ELEMENT_TYPE:
            events.append(None)
        pos += ch_size
    return events


def inspect_apk(path, deep=True):
    """解析 APK，返回包信息字典（永不抛异常）。

    Args:
        path: APK 本地路径
        deep: 是否解析 AndroidManifest 拿 Activity/权限（大包更耗时，但通常 <1s）

    Returns:
        {
          ok, error, package_name, version_name, version_code, min_sdk, target_sdk,
          launch_activity, activities[], permissions[], dangerous_permissions[],
          has_cleartext, sha256, size, file_count, dex_count,
          label(尝试解析 app_name 字符串)
        }
    """
    info = {
        'ok': False, 'error': None,
        'package_name': '', 'version_name': '', 'version_code': None,
        'min_sdk': None, 'target_sdk': None,
        'launch_activity': '', 'activities': [],
        'permissions': [], 'dangerous_permissions': [],
        'has_cleartext': None, 'sha256': '', 'size': 0,
        'file_count': 0, 'dex_count': 0, 'has_native_libs': False,
        'label': '',
    }
    try:
        with open(path, 'rb') as f:
            info['size'] = f.seek(0, 2)
    except Exception as exc:  # noqa: BLE001
        info['error'] = '无法读取文件: %s' % exc
        return info

    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        info['sha256'] = h.hexdigest()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK] 计算 sha256 失败: %s', exc)

    if not zipfile.is_zipfile(path):
        info['error'] = '不是有效的 APK/ZIP 文件'
        return info

    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:  # noqa: BLE001
        info['error'] = '打开 APK 失败: %s' % exc
        return info

    with zf:
        names = zf.namelist()
        info['file_count'] = len(names)
        info['dex_count'] = len([n for n in names if re.match(r'^classes\d*\.dex$', n)])
        info['has_native_libs'] = any(n.startswith('lib/') for n in names)
        if 'AndroidManifest.xml' not in names:
            info['error'] = 'APK 内缺少 AndroidManifest.xml'
            return info
        if not deep:
            info['ok'] = True
            return info
        try:
            raw = zf.read('AndroidManifest.xml')
        except Exception as exc:  # noqa: BLE001
            info['error'] = '读取 AndroidManifest.xml 失败: %s' % exc
            return info

    try:
        events = _parse_axml_events(raw)
    except Exception as exc:  # noqa: BLE001
        info['error'] = '解析 AndroidManifest.xml 失败: %s' % exc
        return info

    rows = _parse_with_stack(events)
    if not rows:
        info['error'] = 'AndroidManifest.xml 解析结果为空'
        return info

    # ---- manifest 根节点 ----
    root = next((r for r in rows if r['tag'] == 'manifest'), None)
    if root:
        attrs = root['attrs']
        info['package_name'] = str(attrs.get('package') or '')
        info['version_name'] = str(attrs.get('versionName') or '')
        vc = attrs.get('versionCode')
        info['version_code'] = vc if isinstance(vc, int) else None

    # ---- uses-sdk / uses-permission / application ----
    activities = []
    launchers = []
    permissions = []
    app_attrs = {}
    for r in rows:
        tag, attrs = r['tag'], r['attrs']
        if tag == 'uses-sdk':
            info['min_sdk'] = attrs.get('minSdkVersion') or info['min_sdk']
            info['target_sdk'] = attrs.get('targetSdkVersion') or info['target_sdk']
        elif tag == 'uses-permission' or tag == 'uses-permission-sdk-23':
            perm = str(attrs.get('name') or '')
            if perm:
                permissions.append(perm)
        elif tag == 'application':
            app_attrs = attrs
            info['has_cleartext'] = attrs.get('usesCleartextTraffic')
        elif tag in ('activity', 'activity-alias'):
            name = str(attrs.get('name') or '')
            if not name:
                continue
            if name.startswith('.'):
                name = (info['package_name'] or '') + name
            activities.append(name)
            if attrs.get('exported') is not True:
                # 未显式 exported 的主 Activity 也很常见，保留但标记，用于后续
                # 「入口探索」判断；只把 exported=True 的当作确定入口。
                pass
            
    # 找 LAUNCHER 入口：path 里 activity → intent-filter → action 且 name 是 MAIN/LAUNCHER
    # 用简化扫描：相邻出现 action android.intent.action.MAIN + category LAUNCHER 的祖先 activity
    stack = []
    seen_categories = {}
    for r in rows:
        tag, attrs, path = r['tag'], r['attrs'], r['path']
        if tag in ('activity', 'activity-alias'):
            stack.append(str(attrs.get('name') or ''))
        if tag == 'action':
            aname = str(attrs.get('name') or '')
            if aname == 'android.intent.action.MAIN' and stack:
                seen_categories.setdefault(stack[-1], set()).add('MAIN')
        if tag == 'category':
            cname = str(attrs.get('name') or '')
            if cname == 'android.intent.category.LAUNCHER' and stack:
                seen_categories.setdefault(stack[-1], set()).add('LAUNCHER')
    for act, kinds in seen_categories.items():
        if 'MAIN' in kinds and 'LAUNCHER' in kinds:
            full = act if not act.startswith('.') else (info['package_name'] or '') + act
            launchers.append(full)

    info['activities'] = activities[:200]
    info['launch_activity'] = launchers[0] if launchers else (activities[0] if activities else '')
    info['permissions'] = sorted(set(permissions))
    info['dangerous_permissions'] = [
        {'name': p, 'label': DANGEROUS_PERMISSIONS[p]}
        for p in info['permissions'] if p in DANGEROUS_PERMISSIONS
    ]
    # application label 常是 @string/app_name 引用，解析不到明文时留空
    label = str(app_attrs.get('label') or '')
    if label and not label.startswith('#ref:'):
        info['label'] = label
    elif label.startswith('#ref:'):
        info['label'] = ''
        info['label_ref'] = label

    if not info['package_name']:
        info['error'] = '未能从 AndroidManifest.xml 解析出包名'
        return info

    if info['package_name'] and not _PKG_RE.match(info['package_name']):
        info['error'] = '包名格式异常：%s' % info['package_name']
        return info

    info['ok'] = True
    return info
