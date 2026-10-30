# -*- coding: utf-8 -*-
"""APK 智能回归的自包含 HTML 报告。

为什么不用 Allure
-----------------
Allure 需要 `pytest + allure-pytest + allure CLI` 三段链路，而这条 MVP
链路的执行体是 Python 直调 Midscene 引擎，没有 pytest 环节。硬凑 Allure
等于为了报告再造一条流水线。

所以这里生成**单文件 HTML**：CSS 内联、截图 base64 内联、不依赖任何
外部资源与 JS，可以直接下载/邮件/丢进归档，离线双击即看。

报告内容（对应需求文档「报告归档」行的输出物）：
    - 概览：包名/版本/设备/通过率/耗时/model 调用数
    - 逐条路径：状态、耗时、每步的 prompt 与结果、失败截图
    - 回放：每步截图（Midscene 每步都会留一张）
    - 危险操作跳过清单（证明"该跳的跳了"）
"""
import base64
import html
import json
import logging
import os
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f5f6f8;color:#1f2329;
     font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
          "Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
h1{font-size:22px;margin:0 0 6px}
.sub{color:#8a9099;font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card .k{color:#8a9099;font-size:12px}
.card .v{font-size:24px;font-weight:600;margin-top:2px}
.card.pass .v{color:#12a150}.card.fail .v{color:#e5484d}.card.skip .v{color:#b7791f}.card.off .v{color:#8a9099}
.path{background:#fff;border-radius:10px;margin-bottom:14px;overflow:hidden;
      box-shadow:0 1px 2px rgba(0,0,0,.06)}
.path>summary{cursor:pointer;padding:13px 16px;display:flex;gap:10px;align-items:center;
              font-weight:600;list-style:none;border-bottom:1px solid #eef0f3}
.path>summary::-webkit-details-marker{display:none}
.badge{font-size:12px;padding:1px 8px;border-radius:20px;font-weight:500;white-space:nowrap}
.b-pass{background:#e7f8ee;color:#12a150}.b-fail{background:#fdecec;color:#e5484d}
.b-skip{background:#fdf3e2;color:#b7791f}.b-run{background:#eaf1fd;color:#2f6feb}.b-off{background:#eef0f3;color:#8a9099}
.b-pri{background:#f2f3f5;color:#5c6370}
.body{padding:6px 16px 16px}
.step{border-left:3px solid #eef0f3;padding:8px 0 8px 12px;margin:8px 0}
.step.ok{border-color:#12a150}.step.no{border-color:#e5484d}
.step .h{display:flex;gap:8px;align-items:center;font-weight:500}
.step .p{color:#5c6370;font-size:13px;margin-top:2px;word-break:break-all}
.step .e{color:#e5484d;font-size:13px;margin-top:2px;white-space:pre-wrap}
.shot{margin-top:8px}
.shot img{max-width:260px;border:1px solid #e6e8eb;border-radius:6px;cursor:zoom-in;
          background:#fafbfc}
pre{background:#f7f8fa;border-radius:8px;padding:12px;overflow:auto;font-size:12px;
    line-height:1.5;white-space:pre-wrap;word-break:break-all}
.tabs{display:flex;gap:6px;margin:14px 0 8px;flex-wrap:wrap}
.tab{font-size:12px;padding:3px 10px;border-radius:20px;background:#eef0f3;color:#5c6370}
.empty{color:#8a9099;padding:8px 0}
h2{font-size:15px;margin:24px 0 10px;color:#3d4450}
.kv{font-size:13px;color:#5c6370}
.kv b{color:#1f2329;font-weight:500}
"""


def _e(v):
    return html.escape('' if v is None else str(v))


def _norm_shot(raw):
    """把引擎截图的裸 base64 补成 data URI（与 reports._normalize_shot 同口径）。"""
    if not raw or not isinstance(raw, str):
        return ''
    t = raw.strip()
    if t.startswith('data:'):
        return t
    if len(t) > 128 and re.fullmatch(r'[A-Za-z0-9+/=\s]+', t):
        return 'data:image/png;base64,' + re.sub(r'\s+', '', t)
    return ''


def _status_cls(status):
    # disabled = 用户手动关闭该路径（不是失败也不是危险拦截），用中性灰区分开，
    # 否则看报告的人会误以为"被系统拦下了"。
    return {
        'passed': 'b-pass', 'failed': 'b-fail',
        'skipped': 'b-skip', 'disabled': 'b-off',
    }.get(status, 'b-run')


_STATUS_LABEL = {'disabled': '已关闭'}


def _render_step(s):
    ok = s.get('status') != 'failed'
    shot = _norm_shot(s.get('screenshot') or s.get('fail_screenshot'))
    parts = ['<div class="step %s">' % ('ok' if ok else 'no'),
             '<div class="h">',
             '<span>%s</span>' % ('✓' if ok else '✗'),
             '<span>步骤%d</span>' % (int(s.get('index') or 0) + 1),
             '<span class="badge b-pri">%s</span>' % _e(s.get('action') or ''),
             '<span>%s</span>' % _e(s.get('name') or ''),
             '</div>']
    if s.get('prompt'):
        parts.append('<div class="p">%s</div>' % _e(s['prompt']))
    if s.get('error'):
        parts.append('<div class="e">%s</div>' % _e(s['error']))
    if s.get('assert'):
        parts.append('<div class="kv">断言：<b>%s</b></div>'
                     % ('通过' if s['assert'].get('pass') else '未通过'))
    if s.get('extract'):
        ext = {k: v for k, v in (s['extract'] or {}).items() if v not in (None, '', {})}
        if ext:
            parts.append('<div class="kv">提取：%s</div>' % _e(json.dumps(ext, ensure_ascii=False)[:300]))
    if shot:
        parts.append('<div class="shot"><img src="%s" alt="step-%d" /></div>'
                     % (shot, int(s.get('index') or 0) + 1))
    parts.append('</div>')
    return ''.join(parts)


def _render_result(r):
    status = r.get('status') or 'failed'
    title = '第%d条 %s' % (int(r.get('index') or 0) + 1, r.get('name') or '')
    ys = r.get('yaml') or ''
    body = ['<details class="path"%s>' % (' open' if status == 'failed' else ''),
            '<summary>',
            '<span class="badge %s">%s</span>' % (
                _status_cls(status), _e(_STATUS_LABEL.get(status, status))),
            '<span class="badge b-pri">%s</span>' % _e(r.get('priority') or ''),
            '<span style="flex:1">%s</span>' % _e(title),
            '<span class="kv">%ss</span>' % _e(r.get('duration') or 0),
            '</summary>',
            '<div class="body">']
    if r.get('error'):
        body.append('<div class="step no"><div class="e">%s</div></div>' % _e(r['error']))
    if r.get('skip'):
        body.append('<div class="kv">危险操作跳过：<b>%s</b></div>'
                    % _e('、'.join(str(x) for x in r['skip'])))
    for s in (r.get('steps') or []):
        body.append(_render_step(s))
    if not (r.get('steps') or []):
        body.append('<div class="empty">该路径没有执行步骤（被拦截或未开始）</div>')
    if ys:
        body.append('<div class="tabs"><span class="tab">Midscene YAML</span></div>')
        body.append('<pre>%s</pre>' % _e(ys))
    body.append('</div></details>')
    return ''.join(body)


def render_html_report(run):
    """生成 HTML 报告，返回 (相对 MEDIA_ROOT 的路径, 绝对路径)。"""
    artifact = run.artifact
    summary = run.summary or {}
    results = run.results or []
    generated = timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')

    cards = [
        ('总路径', summary.get('total', len(results)), ''),
        ('通过', summary.get('passed', 0), 'pass'),
        ('失败', summary.get('failed', 0), 'fail'),
        ('跳过', summary.get('skipped', 0), 'skip'),
    ]
    # 「已关闭」只在真有被关闭的路径时才显示，避免正常报告多出一个 0
    if summary.get('disabled'):
        cards.append(('已关闭', summary.get('disabled'), 'off'))
    cards += [
        ('通过率', '%s%%' % summary.get('pass_rate', 0), 'pass'),
        ('模型交互', summary.get('model_calls', run.model_calls or 0), ''),
        ('总耗时', '%.1fs' % (run.duration or 0), ''),
    ]
    card_html = ''.join(
        '<div class="card %s"><div class="k">%s</div><div class="v">%s</div></div>'
        % (cls, _e(k), _e(v)) for k, v, cls in cards)

    paths_html = ''.join(_render_result(r) for r in results) or \
        '<div class="empty">没有执行结果</div>'

    rea = run.rea_analysis or {}
    danger_html = ''
    if rea.get('dangerous_actions'):
        danger_html = '<h2>REA 识别的危险操作</h2><div class="kv">%s</div>' % _e(
            '；'.join('%s（%s）' % (d.get('name', ''), d.get('reason', ''))
                     for d in rea['dangerous_actions'] if isinstance(d, dict)))

    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>APK 智能回归报告 #%s</title><style>%s</style></head>
<body><div class="wrap">
<h1>APK 智能回归报告</h1>
<div class="sub">运行 #%s · 生成于 %s</div>
<div class="cards">%s</div>
<div class="kv" style="margin-bottom:8px">
包名 <b>%s</b> · 版本 <b>%s</b> · 设备 <b>%s</b>（%s） · APK <b>%s</b>
</div>
%s
<h2>执行明细</h2>
%s
<h2>运行日志（尾部 300 行）</h2>
<pre>%s</pre>
</div>
<script>
document.querySelectorAll('.shot img').forEach(function(img){
  img.addEventListener('click', function(){ window.open(img.src, '_blank'); });
});
</script>
</body></html>""" % (
        _e(run.id), _CSS,
        _e(run.id), _e(generated),
        card_html,
        _e(artifact.package_name or '-'), _e(artifact.version_name or '-'),
        _e(summary.get('device') or getattr(run.device, 'name', '') or '-'),
        _e(summary.get('udid') or getattr(run.device, 'udid', '') or '-'),
        _e(artifact.display_name),
        danger_html,
        paths_html,
        _e('\n'.join((run.log or '').splitlines()[-300:])),
    )

    rel_dir = os.path.join('apk_regression', str(run.id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    fname = 'report_%s.html' % timezone.localtime().strftime('%Y%m%d_%H%M%S')
    abs_path = os.path.join(abs_dir, fname)
    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(doc)

    rel = '%s/%s' % (rel_dir.replace(os.sep, '/'), fname)
    return rel, abs_path
