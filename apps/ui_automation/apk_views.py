# -*- coding: utf-8 -*-
"""APK 智能回归 MVP 的 HTTP 接口。

路由（挂在 /api/ui-automation/ 下，见 urls.py）：
    POST   apk-regression/artifacts/upload/    上传 APK + 静态解析
    GET    apk-regression/artifacts/           APK 列表
    POST   apk-regression/runs/                新建一次运行（绑定 APK / 设备 / 项目）
    GET    apk-regression/runs/                运行列表
    GET    apk-regression/runs/<id>/           详情（前端轮询状态与日志）
    PATCH  apk-regression/runs/<id>/           修改路径清单（人工编辑）
    POST   apk-regression/runs/<id>/analyze/   触发 REA 分析（后台）
    POST   apk-regression/runs/<id>/approve/   人工确认路径（绑定指纹）
    POST   apk-regression/runs/<id>/execute/   执行（需先确认）
    GET    apk-regression/runs/<id>/yaml/      导出 Midscene YAML
    GET    apk-regression/runs/<id>/report/    打开 HTML 报告
    DELETE apk-regression/runs/<id>/           删除运行与报告
"""
import logging
import os

from django.http import FileResponse, HttpResponse, Http404
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import apk_workflow, apk_yaml
from .models import ApkArtifact, ApkRegressionRun, AppDevice

logger = logging.getLogger(__name__)

MAX_APK_BYTES = 600 * 1024 * 1024  # 600MB，覆盖绝大多数商业包


# ---------------------------------------------------------------- 序列化
def _project_name_safe(project_id):
    """按 projects 表取项目名，查不到返回空串。

    `ApkRegressionRun.project` 外键指向 UiProject，而知识库项目在 projects 表，
    两边 id 不对齐时 `run.project` 会抛 DoesNotExist，把整个 detail 接口拖成 500。
    """
    if not project_id:
        return ''
    try:
        from apps.projects.models import Project
        return Project.objects.filter(id=project_id).values_list('name', flat=True).first() or ''
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK] 读取项目名失败 id=%s: %s', project_id, exc)
        return ''


def _artifact_dict(a):
    info = a.manifest_info or {}
    return {
        'id': a.id,
        'name': a.name,
        'display_name': a.display_name,
        'original_name': a.original_name,
        'size': a.size,
        'sha256': a.sha256,
        'package_name': a.package_name,
        'version_name': a.version_name,
        'version_code': a.version_code,
        'min_sdk': a.min_sdk,
        'target_sdk': a.target_sdk,
        'launch_activity': a.launch_activity,
        'parse_error': a.parse_error,
        'permissions': info.get('permissions') or [],
        'dangerous_permissions': info.get('dangerous_permissions') or [],
        'activities': info.get('activities') or [],
        'has_cleartext': info.get('has_cleartext'),
        'dex_count': info.get('dex_count'),
        'file_count': info.get('file_count'),
        'app_config_id': a.app_config_id,
        'created_at': a.created_at,
        'created_by_name': getattr(a.created_by, 'username', '') if a.created_by_id else '',
    }


def _run_dict(run, with_log=False):
    conf = run.confirmation or {}
    data = {
        'id': run.id,
        'artifact': _artifact_dict(run.artifact),
        'device': ({
            'id': run.device_id,
            'name': run.device.name,
            'udid': run.device.udid,
            'platform': run.device.platform,
            'status': run.device.status,
        } if run.device_id else None),
        'project_id': run.project_id,
        # ⚠️ 不能用 run.project：外键指向 UiProject，而知识库项目在 projects 表，
        # 两边 id 不对齐时 getattr 会抛 DoesNotExist 导致整个接口 500。
        'project_name': _project_name_safe(run.project_id),
        # 业务资料概况（前端用来显示"本次分析依据了哪些资料"）
        'context': {
            'doc_count': len(run.context_docs or []),
            'doc_chars': sum(int(d.get('chars') or 0) for d in (run.context_docs or [])),
            'notes_chars': len(run.context_notes or ''),
            'has_context': bool(run.context_docs) or bool(
                (run.context_notes or '').strip()) or bool(run.project_id),
        },
        'status': run.status,
        'status_display': run.get_status_display(),
        'rea_analysis': run.rea_analysis or {},
        'paths': run.paths or [],
        'paths_fingerprint': run.paths_fingerprint(),
        'confirmation': conf,
        'approved': run.is_approved(),
        'approval_blockers': run.approval_blockers(),
        # 明确的"改了执行内容但还没重新确认"标记：
        # 前端据此高亮确认区，不用自己比对 fingerprint 猜。
        'content_changed': bool(conf.get('approved')) and not run.is_approved(),
        'can_edit': run.status != 'analyzing',
        'can_execute': (not run.approval_blockers()) and run.status not in ('analyzing', 'running'),
        'results': run.results or [],
        'summary': run.summary or {},
        'report_url': ('/api/ui-automation/apk-regression/runs/%s/report/' % run.id
                       if run.report_path else ''),
        'report_path': run.report_path,
        'model_calls': run.model_calls,
        'prompt_tokens': run.prompt_tokens,
        'completion_tokens': run.completion_tokens,
        'created_at': run.created_at,
        'started_at': run.started_at,
        'finished_at': run.finished_at,
        'duration': run.duration,
        'log': (run.log or '')[-20000:] if with_log else '',
        'busy': run.status in ('analyzing', 'running'),
    }
    return data


def _artifact_qs(request):
    qs = ApkArtifact.objects.all()
    kw = (request.query_params.get('search') or '').strip()
    if kw:
        qs = qs.filter(name__icontains=kw) | qs.filter(package_name__icontains=kw)
    return qs.select_related('created_by', 'app_config').order_by('-created_at')


def _run_qs(request):
    qs = ApkRegressionRun.objects.select_related(
        'artifact', 'artifact__created_by', 'device', 'project')
    artifact_id = request.query_params.get('artifact')
    if artifact_id:
        qs = qs.filter(artifact_id=artifact_id)
    st = (request.query_params.get('status') or '').strip()
    if st:
        qs = qs.filter(status=st)
    return qs.order_by('-created_at')


# ---------------------------------------------------------------- 上传
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_apk(request):
    """上传 APK：保存 + 解析清单 + 自动创建/复用 AppConfig。"""
    f = request.FILES.get('file')
    if not f:
        return Response({'error': '缺少上传文件（字段名 file）'}, status=status.HTTP_400_BAD_REQUEST)
    if f.size > MAX_APK_BYTES:
        return Response({'error': 'APK 超过 %d MB 限制（当前 %.1f MB）'
                                  % (MAX_APK_BYTES // 1024 // 1024, f.size / 1024 / 1024)},
                        status=status.HTTP_400_BAD_REQUEST)
    lower = (f.name or '').lower()
    if not (lower.endswith('.apk') or lower.endswith('.aab')):
        return Response({'error': '仅支持 .apk 文件（当前：%s）' % f.name},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        artifact, created_app = apk_workflow.ingest_apk(
            f, user=request.user, name=(request.data.get('name') or '').strip())
    except Exception as exc:  # noqa: BLE001
        logger.error('[APK] 上传失败: %s', exc, exc_info=True)
        return Response({'error': '上传处理失败：%s' % exc},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    payload = _artifact_dict(artifact)
    payload['app_config_created'] = created_app
    if artifact.parse_error:
        # 200 而不是 400：文件已入库，前端要能展示"解析失败详情"并允许重新上传
        payload['warning'] = 'APK 已保存但解析失败：%s' % artifact.parse_error
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def artifact_list(request):
    qs = _artifact_qs(request)
    return Response({'results': [_artifact_dict(a) for a in qs[:200]]})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def artifact_detail(request, pk):
    a = ApkArtifact.objects.filter(id=pk).first()
    if not a:
        return Response({'error': 'APK 不存在'}, status=status.HTTP_404_NOT_FOUND)
    if a.runs.exists():
        return Response({'error': '该 APK 已有关联运行记录，请先删除运行记录'},
                        status=status.HTTP_409_CONFLICT)
    a.delete()
    return Response({'ok': True})


# ---------------------------------------------------------------- 运行
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def run_list(request):
    if request.method == 'GET':
        qs = _run_qs(request)
        return Response({'results': [_run_dict(r) for r in qs[:200]]})

    artifact_id = request.data.get('artifact_id')
    artifact = ApkArtifact.objects.filter(id=artifact_id).first()
    if not artifact:
        return Response({'error': 'artifact_id 无效或 APK 不存在'},
                        status=status.HTTP_400_BAD_REQUEST)
    if artifact.parse_error:
        return Response({'error': '该 APK 解析失败（%s），无法创建运行；请重新上传有效 APK'
                                  % artifact.parse_error},
                        status=status.HTTP_400_BAD_REQUEST)

    device = None
    device_id = request.data.get('device_id')
    if device_id:
        device = AppDevice.objects.filter(id=device_id).first()
        if not device:
            return Response({'error': 'device_id 无效或设备不存在'},
                            status=status.HTTP_400_BAD_REQUEST)
        if (device.platform or '').lower() != 'android':
            return Response({'error': 'APK 回归仅支持 Android 设备（当前设备平台：%s）'
                                      % device.platform},
                            status=status.HTTP_400_BAD_REQUEST)

    run = ApkRegressionRun.objects.create(
        artifact=artifact,
        device=device,
        project_id=request.data.get('project_id') or None,
        status='uploaded',
        created_by=request.user,
    )
    auto = request.data.get('auto_analyze')
    if auto in (True, 'true', '1', 1):
        apk_workflow.start_analysis(run.id)
        run.refresh_from_db()
    return Response(_run_dict(run), status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def run_detail(request, pk):
    run = ApkRegressionRun.objects.select_related(
        'artifact', 'artifact__created_by', 'device', 'project').filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(_run_dict(run, with_log=True))

    if request.method == 'DELETE':
        # 删掉报告目录，避免 media 里堆垃圾
        try:
            import shutil
            d = os.path.join(settings.MEDIA_ROOT, 'apk_regression', str(run.id))
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        run.delete()
        return Response({'ok': True})

    # PATCH：人工编辑路径清单 / 备注
    #
    # 关于「执行中能不能改」：可以改，但**只对下一次执行生效**。
    # 执行线程启动时已经把 paths 读进内存（`for idx, path in enumerate(run.paths)`），
    # 改库不会影响正在跑的这一轮，也不会打断它。
    # 之前这里对 running 直接 409，用户想修正一条路径就得等整轮跑完，体验很差。
    if run.status == 'analyzing':
        return Response({'error': '正在分析中，请等待分析完成后再编辑'},
                        status=status.HTTP_409_CONFLICT)

    changes = []
    if 'paths' in request.data:
        paths = request.data.get('paths')
        if not isinstance(paths, list):
            return Response({'error': 'paths 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        from .apk_rea import sanitize_paths
        cleaned = sanitize_paths(paths, (run.rea_analysis or {}).get('dangerous_actions'))
        was_approved = bool((run.confirmation or {}).get('approved'))
        run.paths = cleaned
        changes.append('paths')
        # 方案 C：指纹只覆盖执行内容（steps/skip/type），不含 name。
        #   - 只改名   → 指纹不变 → 确认继续有效，改完可直接执行
        #   - 改步骤   → 指纹变化 → 原确认失效，必须重新确认
        if was_approved and not run.is_approved():
            conf = dict(run.confirmation or {})
            conf['invalidated_reason'] = (
                '执行步骤已修改，原确认已失效，请重新确认后再执行')
            conf['invalidated_at'] = timezone.now().isoformat()
            run.confirmation = conf
            changes.append('confirmation')
        elif was_approved:
            # 只改了名称：把确认里的指纹刷新成新值，保持一致（虽然值相同）
            conf = dict(run.confirmation or {})
            conf.pop('invalidated_reason', None)
            run.confirmation = conf
            changes.append('confirmation')

    if 'analysis_notes' in request.data:
        rea = dict(run.rea_analysis or {})
        rea['analysis_notes'] = str(request.data.get('analysis_notes') or '')
        run.rea_analysis = rea
        changes.append('rea_analysis')

    if changes:
        run.save(update_fields=changes)
    return Response(_run_dict(run))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_analyze(request, pk):
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    if run.status in ('analyzing', 'running'):
        return Response({'error': '当前状态（%s）不允许重新分析' % run.get_status_display()},
                        status=status.HTTP_409_CONFLICT)
    ok = apk_workflow.start_analysis(run.id)
    if not ok:
        return Response({'error': '分析启动失败（可能已有任务在跑）'},
                        status=status.HTTP_409_CONFLICT)
    run.refresh_from_db()
    return Response(_run_dict(run))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_approve(request, pk):
    """人工确认闸门。

    必须先看过分析结果并确认路径；确认内容以指纹形式绑定，
    之后任何路径修改都会让确认自动失效（与墨刀线同一套语义）。
    """
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    if run.status in ('analyzing', 'running'):
        return Response({'error': '当前状态（%s）不允许确认' % run.get_status_display()},
                        status=status.HTTP_409_CONFLICT)
    if not (run.paths or []):
        return Response({'error': '尚无测试路径，请先完成 REA 分析'}, status=status.HTTP_400_BAD_REQUEST)

    invalid = [p.get('name') for p in run.paths if p.get('invalid_reasons')]
    if invalid and not request.data.get('force'):
        return Response({
            'error': '有 %d 条路径存在问题，需先修正（或勾选强制确认）：%s'
                     % (len(invalid), '、'.join(str(x) for x in invalid)),
            'invalid_paths': invalid,
        }, status=status.HTTP_409_CONFLICT)

    conf = {
        'approved': True,
        'approved_at': timezone.now().isoformat(),
        'approved_by': request.user.id,
        'approved_by_name': request.user.username,
        'fingerprint': run.paths_fingerprint(),
        'notes': str(request.data.get('notes') or ''),
        'force': bool(request.data.get('force')),
    }
    run.confirmation = conf
    run.status = 'confirmed' if run.status in ('uploaded', 'analyzed', 'confirmed',
                                               'analyzing_failed') else run.status
    run.save(update_fields=['confirmation', 'status'])
    return Response(_run_dict(run))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_execute(request, pk):
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)

    device = None
    device_id = request.data.get('device_id') or (run.device_id if run.device_id else None)
    if not device_id:
        return Response({'error': '请指定执行设备（device_id）'},
                        status=status.HTTP_400_BAD_REQUEST)
    device = AppDevice.objects.filter(id=device_id).first()
    if not device:
        return Response({'error': '执行设备不存在'}, status=status.HTTP_400_BAD_REQUEST)
    if (device.platform or '').lower() != 'android':
        return Response({'error': 'APK 回归仅支持 Android 设备（当前：%s）' % device.platform},
                        status=status.HTTP_400_BAD_REQUEST)
    if device.status == 'busy':
        return Response({'error': '设备「%s」正在执行其他任务，请稍后再试' % device.name},
                        status=status.HTTP_409_CONFLICT)

    ok, err = apk_workflow.start_execution(run.id, device, user=request.user)
    if not ok:
        return Response({'error': err}, status=status.HTTP_409_CONFLICT)
    run.refresh_from_db()
    return Response(_run_dict(run))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_abort(request, pk):
    """强制中止卡住的执行（超时兜底上线前的遗留任务需要这个出口）。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    ok, msg = apk_workflow.abort_execution(run.id)
    if not ok:
        return Response({'error': msg}, status=status.HTTP_409_CONFLICT)
    run.refresh_from_db()
    return Response({'ok': True, 'message': msg, 'run': _run_dict(run)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def run_yaml(request, pk):
    """导出 Midscene YAML（单条路径 or 全部）。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    idx = request.query_params.get('path')
    if idx is not None:
        try:
            path = (run.paths or [])[int(idx)]
        except (ValueError, IndexError):
            return Response({'error': 'path 索引越界'}, status=status.HTTP_400_BAD_REQUEST)
        text = apk_yaml.build_yaml(path, run.artifact.package_name,
                                   run.artifact.launch_activity)
        fname = 'path_%s.yaml' % int(idx)
    else:
        text = apk_yaml.build_yaml_bundle(run.paths, run.artifact.package_name,
                                          run.artifact.launch_activity)
        fname = 'apk_run_%s.yaml' % run.id

    resp = HttpResponse(text, content_type='text/yaml; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def run_report(request, pk):
    """打开 HTML 报告：inline 展示（浏览器直接渲染），?download=1 则下载。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        raise Http404('运行记录不存在')
    if not run.report_path:
        return Response({'error': '报告尚未生成（需完成执行）'},
                        status=status.HTTP_404_NOT_FOUND)
    abs_path = os.path.join(settings.MEDIA_ROOT, run.report_path.replace('/', os.sep))
    if not os.path.isfile(abs_path):
        return Response({'error': '报告文件已丢失：%s' % run.report_path},
                        status=status.HTTP_404_NOT_FOUND)
    download = request.query_params.get('download') in ('1', 'true', 'yes')
    resp = FileResponse(open(abs_path, 'rb'), content_type='text/html; charset=utf-8')
    disposition = 'attachment' if download else 'inline'
    resp['Content-Disposition'] = '%s; filename="%s"' % (
        disposition, os.path.basename(abs_path))
    return resp
