# -*- coding: utf-8 -*-
"""APK 智能回归 MVP 的编排层（后台线程跑，HTTP 立即返回）。

链路
----
    upload_apk()        上传 + AXP 静态解析 + 自动建/复用 AppConfig
    start_analysis()    REA 分析（后台）→ paths 落库，状态 analyzed
    approve_paths()     人工确认闸门（绑定路径指纹 SHA-256）
    start_execution()   逐条路径 Midscene 真机执行（后台）→ HTML 报告

线程模型与已有模块保持一致（`app_pipeline.py` / `AICaseViewSet._spawn_midscene_execution`
都是 `threading.Thread(daemon=True)`，因为生产没有 Celery worker）。
每个线程内必须 `close_old_connections()`，否则长时间运行后 MySQL
"server has gone away"。
"""
import logging
import os
import threading
import time
import traceback

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import apk_inspector, apk_rea, apk_yaml
from .models import ApkArtifact, ApkRegressionRun, AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 日志
def _log(run, text):
    """追加一行日志并落库（前端轮询 detail 就能看到实时进度）。"""
    if not text:
        return
    line = '[%s] %s' % (timezone.localtime().strftime('%H:%M:%S'), text)
    run.log = ((run.log or '') + line + '\n')[-200000:]
    try:
        close_old_connections()
        run.save(update_fields=['log'])
    except Exception:  # noqa: BLE001
        logger.warning('[APK] 写日志失败', exc_info=True)


#: 引擎实时日志的最小落库间隔（秒）。
#: Midscene 每步会产生多行日志，若每条都 save() 一次，一条路径能写几十次库，
#: 既拖慢执行也增加 MySQL 压力。这里按时间窗合并：不足间隔的先攒着，
#: 下一条到达或执行结束时一起写。窗口取 1.5s —— 前端轮询约 3s 一次，
#: 更细的粒度用户也感知不到。
_LOG_FLUSH_INTERVAL = 1.5


class _RunLogSink(object):
    """把引擎的过程日志实时写进 run.log（带节流）。

    为什么需要：Midscene 是**整条路径跑完才返回**，中途前端一行日志都没有，
    用户看到真机不动就无法判断"是在执行"还是"已卡死"。
    引擎提供了 log_sink 回调，这里负责落库。
    """

    def __init__(self, run, flush_interval=_LOG_FLUSH_INTERVAL):
        self.run = run
        self.flush_interval = flush_interval
        self._pending = []
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def __call__(self, line):
        with self._lock:
            self._pending.append(line)
            now = time.time()
            if now - self._last_flush < self.flush_interval:
                return
            self._last_flush = now
            batch, self._pending = self._pending, []
        if batch:
            self._append(batch)

    def flush(self):
        """执行结束时把剩余缓冲写出（必须调用，否则最后几行会丢）。"""
        with self._lock:
            batch, self._pending = self._pending, []
        if batch:
            self._append(batch)

    def _append(self, lines):
        if not lines:
            return
        try:
            close_old_connections()
            self.run.log = ((self.run.log or '') + '\n'.join(lines) + '\n')[-200000:]
            self.run.save(update_fields=['log'])
        except Exception:  # noqa: BLE001
            logger.warning('[APK] 实时日志落库失败', exc_info=True)


# ---------------------------------------------------------------- 上传
def ingest_apk(django_file, user=None, name='', project_id=None):
    """保存 APK、解析清单、复用/创建 AppConfig。

    Returns:
        (artifact, created_app_config: bool)

    失败不抛异常：解析失败也照样入库（`parse_error` 有值），
    这样用户能在界面上看到「为什么这个包不能用」，而不是一个 400。
    """
    original = getattr(django_file, 'name', '') or 'upload.apk'
    artifact = ApkArtifact(
        name=name or original,
        original_name=original[:255],
        created_by=user,
    )
    artifact.file = django_file
    artifact.save()

    path = artifact.file.path
    info = apk_inspector.inspect_apk(path)
    artifact.size = info.get('size') or 0
    artifact.sha256 = info.get('sha256') or ''
    artifact.package_name = info.get('package_name') or ''
    artifact.version_name = info.get('version_name') or ''
    artifact.version_code = info.get('version_code')
    artifact.min_sdk = info.get('min_sdk')
    artifact.target_sdk = info.get('target_sdk')
    artifact.launch_activity = info.get('launch_activity') or ''
    artifact.manifest_info = info
    artifact.parse_error = info.get('error') or ''

    created_app = False
    if artifact.package_name:
        app_cfg = AppConfig.objects.filter(package_name=artifact.package_name).first()
        if app_cfg is None:
            app_cfg = AppConfig.objects.create(
                name=(info.get('label') or artifact.name or artifact.package_name)[:200],
                platform='android',
                package_name=artifact.package_name,
                app_activity=(artifact.launch_activity or '')[:200],
                version=(artifact.version_name or '')[:50],
                app_path=path,
            )
            created_app = True
        else:
            updates = []
            # 只回填空字段，绝不覆盖用户手工维护的 activity / 版本
            if not (app_cfg.app_activity or '').strip() and artifact.launch_activity:
                app_cfg.app_activity = artifact.launch_activity[:200]
                updates.append('app_activity')
            if not (app_cfg.app_path or '').strip():
                app_cfg.app_path = path
                updates.append('app_path')
            if updates:
                app_cfg.save(update_fields=updates + ['updated_at'])
        artifact.app_config = app_cfg
    artifact.save()
    return artifact, created_app


# ---------------------------------------------------------------- 分析
_LOCK = threading.Lock()
_RUNNING = set()


def _guard(key):
    """同一 run 只允许一个后台任务（重复点击不会跑两遍）。"""
    with _LOCK:
        if key in _RUNNING:
            return False
        _RUNNING.add(key)
        return True


def _release(key):
    with _LOCK:
        _RUNNING.discard(key)


def _log_context(run):
    """在分析日志里说明本次读了哪些业务资料。

    用户最容易困惑的问题就是"我传了操作说明，怎么还是通用路径"，
    所以把资料清单和字数明确打到日志里，一眼能看出是不是资料没生效。
    """
    docs = run.context_docs or []
    notes = (run.context_notes or '').strip()
    kb_hint = ('项目知识库（项目 #%s）' % run.project_id
               if run.project_id else '未关联项目（不读知识库）')
    if not docs and not notes:
        _log(run, '业务资料：无（仅按 APK 静态清单分析，路径会偏通用）')
        return
    parts = []
    if docs:
        parts.append('操作说明 %d 份（%s）' % (
            len(docs), '、'.join((d.get('name') or '?')[:30] for d in docs[:3])))
    if notes:
        parts.append('人工补充 %d 字' % len(notes))
    _log(run, '业务资料：%s；%s' % ('，'.join(parts), kb_hint))


def start_analysis(run_id):
    """后台执行 REA 分析。返回是否成功启动。"""
    run = ApkRegressionRun.objects.filter(id=run_id).first()
    if not run:
        return False
    if run.status in ('analyzing', 'running'):
        return False

    run.status = 'analyzing'
    run.rea_analysis = {}
    run.paths = []
    run.confirmation = {}
    run.log = ''
    run.started_at = timezone.now()
    run.finished_at = None
    run.save(update_fields=['status', 'rea_analysis', 'paths', 'confirmation',
                            'log', 'started_at', 'finished_at'])

    t = threading.Thread(target=_analysis_worker, args=(run_id,), daemon=True)
    t.start()
    return True


def _analysis_worker(run_id):
    if not _guard(('analysis', run_id)):
        return
    try:
        close_old_connections()
        run = ApkRegressionRun.objects.get(id=run_id)
        _log(run, '开始 REA 分析：%s' % run.artifact.display_name)
        _log(run, 'APK 解析：包名=%s 版本=%s 启动Activity=%s' % (
            run.artifact.package_name or '(未解析)',
            run.artifact.version_name or '?',
            run.artifact.launch_activity or '(未解析)'))

        # 把 run 传进去，让分析能读到业务资料（操作说明 / 知识库 / 人工补充）。
        # 不传就退化成"仅 APK 清单"，生成的都是通用路径。
        _log_context(run)
        result = apk_rea.analyze_apk(run.artifact, run=run)

        run.rea_analysis = result
        run.paths = result.get('paths') or []
        run.confirmation = {}
        if result.get('llm_error'):
            _log(run, '[WARN] %s' % result['llm_error'])
            _log(run, '已降级为规则分析，生成的路径需人工补充')
            run.status = 'analyzed'
        elif not run.paths:
            _log(run, '[ERROR] 分析未产出任何路径')
            run.status = 'analyzing_failed'
        else:
            run.status = 'analyzed'

        _log(run, '分析完成：%d 条路径（来源：%s）' % (len(run.paths), result.get('source')))
        for i, p in enumerate(run.paths):
            _log(run, '  %d) [%s/%s] %s%s' % (
                i + 1, p.get('priority'), p.get('type'), p.get('name'),
                '' if not p.get('invalid_reasons') else '  ⚠ %s' % '; '.join(p['invalid_reasons'])))
        run.finished_at = timezone.now()
        if run.started_at:
            run.duration = (run.finished_at - run.started_at).total_seconds()
        run.save(update_fields=['status', 'rea_analysis', 'paths', 'confirmation',
                                'log', 'finished_at', 'duration'])
    except Exception as exc:  # noqa: BLE001
        logger.error('[APK] 分析线程异常: %s', exc, exc_info=True)
        try:
            run = ApkRegressionRun.objects.get(id=run_id)
            run.status = 'analyzing_failed'
            _log(run, '[ERROR] 分析异常：%s' % exc)
            _log(run, traceback.format_exc()[-2000:])
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'log', 'finished_at'])
        except Exception:  # noqa: BLE001
            pass
    finally:
        close_old_connections()
        _release(('analysis', run_id))


#: 设备会话失效的特征串。midscene 内部句柄被销毁后抛此错误，
#: 但引擎不会自动重连，后续每一步都会失败。
#:
#: ⚠️ `session not found` 这一条不能漏（2026-10-28 run 14 实测）：
#: 客户端 RPC 超时断开后，Node 侧会话就被清掉了，下一步任何一个动作都抛
#: `Session not found: session_xxx`。它**不是**"设备掉线"，
#: `adb devices` 此时完全正常，重建一次连接就能接着跑。
#: 之前只认 `has been destroyed`，于是这种可自愈的情况被当成致命错误，
#: 整条路径判失败、后续步骤连带停掉、整轮 run 中止，白跑 288 秒。
_SESSION_DEAD_MARKERS = (
    'has been destroyed and cannot execute',
    'AndroidDevice destroyed',
    'device has been disconnected',
    'adb: device offline',
    'device not found',
    'session not found',
    'no such session',
    'session is not available',
)


def _is_session_dead(err):
    text = (err or '').lower()
    return any(m.lower() in text for m in _SESSION_DEAD_MARKERS)


def _abort_requested(run_id, run=None):
    """回查「人工终止」信号（每次都读库，不吃缓存）。

    为什么必须读库而不是读内存：用户点终止是走另一个请求线程，
    执行线程拿不到那个线程的任何对象；DB 是两者唯一共享的通信面。

    为什么每次重读而不是缓存：中止应该"点了就尽快停"，
    少查一次库换来的可能是多跑好几分钟、手机上多点几十下。

    读取失败时返回 False —— 不能因为一次查询异常就把正常执行中断掉。
    """
    try:
        from .models import ApkRegressionRun
        val = (ApkRegressionRun.objects.filter(id=run_id)
               .values_list('abort_requested', flat=True).first())
        return bool(val)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK] 读取中止信号失败 run=%s: %s', run_id, exc)
        return False


def _path_enabled(path):
    """该路径是否启用（默认启用）。

    统一用 apk_rea._coerce_bool 判定，不要写 `path.get('enabled') is not False`：
    JSON 里的 0 / "false" 会被漏判成"启用"，导致关闭开关不生效。
    """
    return apk_rea._coerce_bool((path or {}).get('enabled'), default=True)


#: 单条路径的整体执行上限（秒）。
#:
#: 为什么必须有：Midscene 单个动作有超时，但**设备会话失效后部分调用会无限等待**
#: —— 2026-10-28 实测 run 6 卡在第 6 条路径，9 小时无任何日志、无进度的"假执行中"，
#: 用户只能看着转圈。有了这个兜底，最坏情况是"某条路径 timeout 失败"，
#: 而不是整轮僵死、锁住任务。
#: 可用环境变量 APK_PATH_TIMEOUT 覆盖（部署时想放宽不必改代码）。
DEFAULT_PATH_TIMEOUT = int(os.environ.get('APK_PATH_TIMEOUT', '900'))  # 15 分钟


def _run_with_timeout(fn, timeout_s):
    """在独立线程里跑 fn，超时则放弃等待。

    ⚠️ 注意：Python 无法真正杀死线程。超时后该线程仍在后台（daemon），
    可能还握着设备连接。所以超时分支里会额外杀一次 midscene 的 Node 服务，
    避免下一条路径复用一个已僵死的引擎。
    """
    box = {}

    def _target():
        try:
            box['value'] = fn()
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return None, TimeoutError('单条路径执行超过 %d 秒未结束' % timeout_s)
    if 'error' in box:
        return None, box['error']
    return box.get('value'), None


def _kill_stale_node_service():
    """杀掉可能僵死的 midscene Node 服务，让下一条路径重新拉起干净进程。"""
    try:
        import glob
        import signal
        killed = 0
        for p in glob.glob('/proc/[0-9]*/cmdline'):
            try:
                with open(p, 'rb') as f:
                    cmd = f.read().decode('utf-8', 'ignore')
            except Exception:  # noqa: BLE001
                continue
            if 'midscene' in cmd and 'service.js' in cmd:
                pid = int(p.split('/')[2])
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except Exception:  # noqa: BLE001
                    pass
        return killed
    except Exception:  # noqa: BLE001
        return 0


def abort_execution(run_id):
    """强制中止一条卡住的执行，把状态落成 failed。

    为什么需要：Python 无法杀死挂住的工作线程，虽然已加路径级超时，
    但**超时机制上线前遗留的卡死任务**没有任何出口——它永远是 running，
    既不能重新执行也不能编辑。这里给一个显式的"人工终止"通道：
      - 置 abort_requested 信号，让执行线程在下一条路径前自行停止
      - 状态置 failed，保留已完成的 results（报告仍可查）
      - 清理僵死的 midscene Node 进程，避免污染下一轮

    ⚠️ 为什么要置信号而不是只杀进程：杀 Node 进程有窗口期 —— 已经发出的
    `input tap` 会立刻到手机，`aiAct` 当前那一轮也可能已执行完。更重要的是
    执行线程是同步阻塞的（`_run_with_timeout` 只是放弃等待，线程还在跑），
    杀掉 Node 后它发现连接失败会继续处理**下一条路径** ⇒ 手机上继续动。
    有了 abort_requested，线程在 4 个检查点（每路径前 / 重试前 / 重试后 /
    路径结果落库后）都会 break。

    Returns:
        (ok: bool, message: str)
    """
    run = ApkRegressionRun.objects.filter(id=run_id).first()
    if not run:
        return False, '运行记录不存在'
    if run.status not in ('running', 'analyzing'):
        return False, '当前状态（%s）无需中止' % run.get_status_display()

    # ① 先置信号再杀进程：顺序反过来的话，杀进程到置信号之间执行线程
    #    可能刚好进入下一条路径，导致又跑起来一条。
    try:
        ApkRegressionRun.objects.filter(id=run_id).update(abort_requested=True)
        run.abort_requested = True
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK] 置中止信号失败 run=%s: %s', run_id, exc)

    killed = _kill_stale_node_service()
    done = len(run.results or [])
    # 直接追加日志，不用 _log()：_log 会立刻 save(update_fields=['log'])，
    # 那样状态/报告会在中间态先落一次库，容易读到"已 failed 但没报告"的脏状态。
    _ts = timezone.localtime().strftime('%H:%M:%S')
    _lines = ['[%s] [中止] 用户手动中止执行（已完成 %d 条路径，结果保留）' % (_ts, done)]
    _lines.append('[%s] [中止] 已置中止信号，执行线程将在当前路径结束后停止' % _ts)
    if killed:
        _lines.append('[%s] [中止] 已清理 %d 个僵死的 midscene 进程' % (_ts, killed))
    run.log = ((run.log or '') + '\n'.join(_lines) + '\n')[-200000:]

    run.status = 'failed'
    run.finished_at = timezone.now()
    if run.started_at:
        run.duration = (run.finished_at - run.started_at).total_seconds()

    # 中止时也要出报告：已经跑完的路径结果是有价值的证据，
    # 没有报告的话用户在界面上只能看到一行行 results，没法交付/复盘。
    results = run.results or []
    if results:
        summary = {k: v for k, v in (run.summary or {}).items()}
        passed = sum(1 for x in results if x.get('status') == 'passed')
        failed = sum(1 for x in results if x.get('status') == 'failed')
        skipped = sum(1 for x in results if x.get('status') == 'skipped')
        disabled = sum(1 for x in results if x.get('status') == 'disabled')
        executed = passed + failed
        summary.update({
            'total': len(results), 'executed': executed, 'passed': passed,
            'failed': failed, 'skipped': skipped, 'disabled': disabled,
            'pass_rate': round(passed * 100.0 / executed, 1) if executed else 0,
            'aborted': True,
        })
        run.summary = summary
        try:
            from .apk_report import render_html_report
            rel, _abs = render_html_report(run)
            run.report_path = rel
            # 直接追加日志行，不走 _log()：_log 会单独 save(update_fields=['log'])，
            # 在状态还没落库时可能把并发读到的中间态写脏。
            ts = timezone.localtime().strftime('%H:%M:%S')
            run.log = ((run.log or '') +
                       '[%s] [中止] 已生成部分报告：%s（含已完成 %d 条路径）\n'
                       % (ts, rel, len(results)))[-200000:]
        except Exception as exc:  # noqa: BLE001
            logger.warning('[APK] 中止时生成报告失败: %s', exc)
            ts = timezone.localtime().strftime('%H:%M:%S')
            run.log = ((run.log or '') +
                       '[%s] [中止] 生成报告失败：%s\n' % (ts, str(exc)[:200]))[-200000:]

    run.save(update_fields=['status', 'log', 'finished_at', 'duration',
                            'summary', 'report_path'])

    # ⚠️ 这里**不能**释放 ('exec', run_id) 互斥锁：
    # 执行线程是同步阻塞的，此刻它可能还在跑当前路径。若此时放开锁，
    # 用户可以立刻点「执行」→ 两个线程同时操作同一台手机（真机上是灾难）。
    # 锁由执行线程自己在 finally 里释放（见 _execution_worker），
    # 它真正退出后才允许下一轮。这里只释放分析锁（分析线程与设备无关）。
    _release(('analysis', run_id))
    return True, ('已中止，已完成 %d 条路径的结果保留；'
                  '当前路径收尾后执行线程即停止（如需立即执行请稍等几秒）' % done)


# ---------------------------------------------------------------- 执行
def start_execution(run_id, device, user=None):
    """人工确认通过后，后台逐条路径执行。

    Returns:
        (ok: bool, error: str)
    """
    run = ApkRegressionRun.objects.filter(id=run_id).first()
    if not run:
        return False, '运行记录不存在'
    blockers = run.approval_blockers()
    if blockers:
        return False, '；'.join(blockers)
    if run.status in ('analyzing', 'running'):
        return False, '当前状态（%s）不允许执行，请等待或重新分析' % run.get_status_display()
    if not run.artifact.app_config_id:
        return False, 'APK 未关联应用配置（AppConfig），无法执行；请先确认包名与启动 Activity'

    # 公共平台前置校验：设备地址必须是"别人也能连"的局域网地址。
    # 放在这里（而不是等引擎 connect 超时）是因为这两种情况差别很大：
    #   - 地址为空      → 后端会用容器本地 adb，容器里没有设备，必然白跑
    #   - 地址是回环    → 只指向服务器自己，在共享平台上对其他用户毫无意义
    # 提前拦住，用户看到的是一句能照着改的话，而不是一串 adb 超时日志。
    adb_host = (getattr(device, 'adb_host', '') or '').strip()
    bare = adb_host.rsplit(':', 1)[0] if adb_host.count(':') == 1 else adb_host
    if not adb_host:
        return False, ('设备「%s」未配置 adb_host，后端无法定位手机。'
                       '请在该设备所插的电脑上运行 scripts\\adb_remote_server.bat 自动注册，'
                       '或在设备编辑里填写该电脑的局域网 IP:5037' % getattr(device, 'name', ''))
    if bare in ('127.0.0.1', 'localhost', '0.0.0.0', '::1'):
        return False, ('设备「%s」的 adb_host 是 %s（回环地址），公共平台上它只会指向服务器自己，'
                       '其他用户执行必然失败。请改填手机所插电脑的局域网 IP:5037'
                       % (getattr(device, 'name', ''), adb_host))

    # 全部路径被关闭时直接拒绝：否则会跑一轮"0 条执行"的空任务，
    # 用户以为是真机出问题，实际是自己把开关都关了。
    enabled_count = sum(1 for p in (run.paths or []) if _path_enabled(p))
    if enabled_count == 0:
        return False, '所有路径都已关闭，没有可执行的路径。请至少启用一条，或删除无用的路径。'

    run.device = device
    run.status = 'running'
    run.results = []
    run.summary = {}
    run.started_at = timezone.now()
    run.finished_at = None
    run.duration = 0
    run.report_path = ''
    run.log = ''
    # 复位中止信号：同一条 run 中止后再点执行，必须能正常跑，
    # 否则上次残留的 True 会让执行线程第一步就 break（表现为"一点执行就结束"）。
    run.abort_requested = False
    run.save(update_fields=['device', 'status', 'results', 'summary', 'started_at',
                            'finished_at', 'duration', 'report_path', 'log',
                            'abort_requested'])
    _log(run, '开始执行：%d 条路径（其中 %d 条已关闭将跳过），设备=%s（%s）' % (
        len(run.paths), len(run.paths) - enabled_count,
        getattr(device, 'name', ''), getattr(device, 'udid', '')))

    t = threading.Thread(target=_execution_worker, args=(run_id,), daemon=True)
    t.start()
    return True, ''


def _execution_worker(run_id):
    if not _guard(('exec', run_id)):
        return
    run = None
    try:
        close_old_connections()
        run = ApkRegressionRun.objects.get(id=run_id)
        from .midscene_engine import MidsceneAppEngine

        app_config = run.artifact.app_config
        device = run.device
        results = []
        total_calls = 0
        workspace = os.path.join(settings.MEDIA_ROOT, 'apk_regression', str(run.id))

        # 项目级弹窗策略（取不到项目时退回引擎全局默认）。
        #
        # ⚠️⚠️ 这里有过两次踩坑（2026-10-29 线上实测）：
        #   ① `getattr(run, 'project', None)` **不防外键**：project 是可空外键，
        #      NULL 时访问属性抛 `RelatedObjectDoesNotExist`，它是
        #      ObjectDoesNotExist 的子类、**不是 AttributeError**，
        #      getattr 的默认值根本不会生效 → 整轮执行直接打挂。
        #   ② 只判断 `run.project_id` 也**不够**：project_id 有值但对应
        #      UiProject 记录已被删除时（脏数据/SET_NULL 未生效），
        #      访问 run.project 照样抛 "UiProject matching query does not exist"。
        # ⇒ 唯一稳妥的取法：自己查，查不到就 None。
        #   多一次查询可接受：整轮执行每条路径都要跑几十秒到几分钟，
        #   而"因为读个配置把整轮打挂"的代价高得多。
        # 注意：弹窗配置读不到**绝不能阻断执行**，它只是锦上添花。
        project = None
        if run.project_id:
            try:
                from .models import UiProject
                project = UiProject.objects.filter(id=run.project_id).first()
            except Exception as exc:  # noqa: BLE001
                _log(run, '[弹窗] 读取项目配置失败（不影响执行）: %s' % exc)
                project = None
        _auto_popup = getattr(project, 'auto_dismiss_popup', None) if project else None
        _popup_precheck = getattr(project, 'popup_precheck', None) if project else None
        if project:
            _log(run, '[弹窗] 策略：自动处理=%s，执行前预检=%s（来源：项目「%s」配置）'
                 % ('开' if _auto_popup else '关',
                    '开' if _popup_precheck else '关', project.name))
        elif run.project_id:
            _log(run, '[弹窗] 项目(id=%s)已不存在，使用平台默认策略'
                 '（自动处理/预检均开启）' % run.project_id)
        else:
            _log(run, '[弹窗] 未关联项目，使用平台默认策略（自动处理/预检均开启）')

        aborted = False
        for idx, path in enumerate(run.paths):
            # ① 每条路径开始前先看有没有被要求中止。
            #    这是最关键的一个检查点：用户点终止后，剩下的路径一条都不该再启动，
            #    否则手机上会继续出现点击动作（"点了终止手机还在动"就是这么来的）。
            if _abort_requested(run_id):
                aborted = True
                _log(run, '[中止] 检测到终止信号，停止执行剩余 %d 条路径'
                     % (len(run.paths) - idx))
                break

            name = path.get('name') or '路径%d' % (idx + 1)
            _log(run, '\n=== [%d/%d] %s ===' % (idx + 1, len(run.paths), name))

            # 路径级开关：用户手动关闭的路径直接跳过。
            # 用独立状态 disabled（不是 skipped）—— skipped 表示"被危险拦截"，
            # 两者混在一起会让报告看不出"是我自己关的"还是"系统拦的"。
            if not _path_enabled(path):
                _log(run, '[已关闭] 该路径已停用，本次跳过')
                results.append({
                    'index': idx, 'name': name, 'priority': path.get('priority'),
                    'type': path.get('type'), 'status': 'disabled',
                    'error': '路径已关闭，本次未执行',
                    'steps': [], 'duration': 0,
                })
                run.results = results
                run.save(update_fields=['results'])
                continue

            # 最后一道危险拦截：即使人工确认过，步骤里仍出现危险词也直接跳过。
            # （MVP 只拦"步骤自身是危险操作"，第 2 期才做自动探索期的全量拦截）
            danger = None
            for s in (path.get('steps') or []):
                danger = apk_yaml.check_dangerous(s, path.get('skip'))
                if danger:
                    break
            if danger:
                _log(run, '[SKIP] 步骤含危险操作「%s」，按危险操作拦截策略跳过本路径' % danger)
                results.append({
                    'index': idx, 'name': name, 'priority': path.get('priority'),
                    'type': path.get('type'), 'status': 'skipped',
                    'error': '步骤含危险操作「%s」，已拦截' % danger,
                    'steps': [], 'duration': 0,
                })
                run.results = results
                run.save(update_fields=['results'])
                continue

            steps = apk_yaml.path_to_steps(path)
            started = time.time()
            engine = None
            _already_retried = False

            def _attempt(ws_suffix, label=''):
                """执行一次（连接 + run），返回 (result, error)。"""
                # 实时日志通道：引擎每产生一行就回调，攒够一个时间窗就落库。
                # 这样前端能看到「[步骤 3] 点击xx | action=aiAct」这类过程，
                # 而不是等整条路径跑完（可能 5 分钟）才一次性出现。
                sink = _RunLogSink(run)
                eng = MidsceneAppEngine(
                    app_config=app_config,
                    device=device,
                    workspace=os.path.join(workspace, ws_suffix),
                    # 视觉模型只从「AI 智能模式」自己的配置读，不碰 AIModelConfig，
                    # 保证其它功能（AICase / Playground）的模型配置不受本模块影响。
                    model_source=MidsceneAppEngine.MODEL_SOURCE_AI_MODE,
                    log_sink=sink,
                    # 项目级弹窗策略；未关联项目时为 None，引擎会退回全局默认
                    auto_dismiss_popup=_auto_popup,
                    popup_precheck=_popup_precheck,
                )
                try:
                    eng.connect()
                    out = eng.run(steps)
                finally:
                    # 必须 flush：最后不足一个时间窗的日志否则会丢，
                    # 而恰恰最后几行（失败原因）是最需要看到的。
                    sink.flush()
                if label:
                    _log(run, '[%s] 结果：%s' % (label, (out or {}).get('status')))
                # ⚠️ 只返回结果本身。`_run_with_timeout` 的约定是
                # "返回值放 value、异常放 error"，这里若再包一层 (out, None)
                # 外层拿到的就是元组，后续 out.get() 会报
                # "'tuple' object has no attribute 'get'"（踩过一次）。
                return out

            status, err, step_rows, calls = 'failed', '', [], 0
            try:
                # 超时保护：整条路径最多跑 DEFAULT_PATH_TIMEOUT 秒。
                # 超时不再无限等待（这正是 run 6 卡死 9 小时的根因——没有这层兜底）。
                _log(run, '开始执行（上限 %d 秒）' % DEFAULT_PATH_TIMEOUT)
                result, timeout_err = _run_with_timeout(
                    lambda: _attempt('run_%d' % idx), DEFAULT_PATH_TIMEOUT)

                if timeout_err is not None:
                    if isinstance(timeout_err, TimeoutError):
                        status = 'failed'
                        err = '%s，已中止本路径（可能设备无响应或页面卡死）' % timeout_err
                        _log(run, '[超时] %s' % err)
                        # 杀掉可能僵死的 Node 服务，别让它污染下一条路径
                        n = _kill_stale_node_service()
                        if n:
                            _log(run, '[超时] 已清理 %d 个僵死的 midscene 进程' % n)
                    else:
                        raise timeout_err
                else:
                    status = 'passed' if result.get('status') == 'passed' else 'failed'
                    err = result.get('error') or ''
                    step_rows = result.get('steps') or []
                    calls = sum(1 for s in step_rows if s.get('action') in
                                ('aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiWaitFor', 'assert'))
                    total_calls += calls

                    # ---- 设备会话失效 → 重建连接重试一次 ----
                    # 实测 run 6：midscene 抛
                    #   "AndroidDevice <udid> has been destroyed and cannot execute ADB commands"
                    # 之后每一步都注定失败，但引擎不会自己重连。重建一次常能救回整条路径。
                    if status != 'passed' and _is_session_dead(err):
                        # ② 重试前也要查中止信号。
                        #    这是最容易被忽略、后果最明显的一个点：用户正是在
                        #    "重试"这一轮点的终止，若不拦，重试会重新 connect +
                        #    重新跑整条路径 ⇒ 手机上继续点一遍。
                        if _abort_requested(run_id):
                            aborted = True
                            _log(run, '[中止] 检测到终止信号，跳过本路径的重试')
                            status, err = 'failed', '用户中止执行'
                            step_rows = step_rows or []
                        else:
                            _log(run, '[重试] 检测到设备会话失效，重建连接后重跑本路径…')
                            _kill_stale_node_service()
                            retry_result, retry_err = _run_with_timeout(
                                lambda: _attempt('run_%d_retry' % idx, '重试'),
                                DEFAULT_PATH_TIMEOUT)
                            if retry_err is None and retry_result:
                                status = ('passed' if retry_result.get('status') == 'passed'
                                          else 'failed')
                                err = retry_result.get('error') or ''
                                step_rows = retry_result.get('steps') or []
                                total_calls += sum(
                                    1 for s in step_rows if s.get('action') in
                                    ('aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiWaitFor', 'assert'))
                            else:
                                _log(run, '[重试] 仍未成功：%s' % str(retry_err)[:200])
                            # ③ 重试刚跑完，再查一次：用户可能在这期间点了终止，
                            #    此时不该再进入下一条路径。
                            if _abort_requested(run_id):
                                aborted = True
                                _log(run, '[中止] 检测到终止信号，停止执行剩余路径')
            except Exception as exc:  # noqa: BLE001
                logger.warning('[APK] 路径执行失败 %s: %s', name, exc)
                status = 'failed'
                err = str(exc)[:1000]
                step_rows = []
                calls = 0
                _log(run, traceback.format_exc()[-1200:])

            elapsed = round(time.time() - started, 1)
            for s in step_rows:
                icon = '✓' if s.get('status') != 'failed' else '✗'
                _log(run, '  %s 步骤%s %s %s' % (
                    icon, s.get('index', 0) + 1, s.get('name', ''),
                    ('| %s' % s.get('error')) if s.get('error') else ''))
            _log(run, '  → %s（%ss，%d 次模型交互）' % (status, elapsed, calls))

            results.append({
                'index': idx,
                'name': name,
                'priority': path.get('priority'),
                'type': path.get('type'),
                'status': status,
                'error': err,
                'duration': elapsed,
                'model_calls': calls,
                'steps': step_rows,
                'skip': path.get('skip') or [],
                'yaml': apk_yaml.build_yaml(path, run.artifact.package_name,
                                            run.artifact.launch_activity),
            })
            run.results = results
            run.model_calls = total_calls
            run.save(update_fields=['results', 'model_calls'])

            # ④ 本路径结果已落库，若期间被要求中止就退出循环。
            #    放在 save 之后，保证"已跑完的这条路径结果不丢"（报告仍完整）。
            if aborted or _abort_requested(run_id):
                aborted = True
                remain = len(run.paths) - (idx + 1)
                if remain > 0:
                    _log(run, '[中止] 停止执行，剩余 %d 条路径未执行' % remain)
                break

        # ---- 汇总 ----
        passed = len([r for r in results if r['status'] == 'passed'])
        failed = len([r for r in results if r['status'] == 'failed'])
        skipped = len([r for r in results if r['status'] == 'skipped'])
        disabled = len([r for r in results if r['status'] == 'disabled'])
        # 通过率的分母只算"真正执行过"的路径（passed + failed）。
        # 用户关闭的路径不该拉低通过率，被危险拦截的也不该 —— 否则
        # "关掉两条不稳定的路径"反而让数字变好看/变难看，都是误导。
        executed = passed + failed
        run.summary = {
            'total': len(results),
            'executed': executed,
            'passed': passed,
            'failed': failed,
            'skipped': skipped,
            'disabled': disabled,
            'pass_rate': round(passed * 100.0 / executed, 1) if executed else 0.0,
            'model_calls': total_calls,
            'package': run.artifact.package_name,
            'version': run.artifact.version_name,
            'device': getattr(device, 'name', ''),
            'udid': getattr(device, 'udid', ''),
        }

        # ---- HTML 报告 ----
        try:
            from .apk_report import render_html_report
            rel, abs_path = render_html_report(run)
            run.report_path = rel
            _log(run, '报告已生成：%s' % rel)
        except Exception as exc:  # noqa: BLE001
            logger.error('[APK] 报告生成失败: %s', exc, exc_info=True)
            _log(run, '[ERROR] 报告生成失败：%s' % exc)

        # 被人工中止的轮次不收尾成 done：用户点的是「终止」，
        # 结果应该显示为失败/中止，而不是一个看起来"正常跑完"的 done
        # （否则中止后报告统计与用户预期不符）。
        if aborted or _abort_requested(run_id):
            run.status = 'failed'
            _log(run, '\n执行已中止：通过 %d / 失败 %d / 跳过 %d / 已关闭 %d'
                 % (passed, failed, skipped, disabled))
        else:
            run.status = 'done'
            _log(run, '\n执行结束：通过 %d / 失败 %d / 跳过 %d / 已关闭 %d'
                 % (passed, failed, skipped, disabled))
    except Exception as exc:  # noqa: BLE001
        logger.error('[APK] 执行线程异常: %s', exc, exc_info=True)
        try:
            if run is None:
                run = ApkRegressionRun.objects.get(id=run_id)
            run.status = 'failed'
            _log(run, '[ERROR] 执行异常：%s' % exc)
            _log(run, traceback.format_exc()[-2000:])
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if run is not None:
                run.finished_at = timezone.now()
                if run.started_at:
                    run.duration = (run.finished_at - run.started_at).total_seconds()
                run.save(update_fields=['status', 'finished_at', 'duration', 'summary',
                                        'report_path', 'model_calls', 'results', 'log'])
        except Exception:  # noqa: BLE001
            pass
        close_old_connections()
        _release(('exec', run_id))
