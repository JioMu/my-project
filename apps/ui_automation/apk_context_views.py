# -*- coding: utf-8 -*-
"""APK 回归的「业务资料」接口：操作说明文档 + 项目知识库。

路由（挂在 /api/ui-automation/ 下，见 urls.py）：
    GET    apk-regression/projects/                     可选项目列表（选知识库前要先选项目）
    GET    apk-regression/projects/<id>/knowledge/      该项目的知识库条目（预览用）
    POST   apk-regression/runs/<pk>/context/upload/     上传操作说明文档（解析成文本入库）
    DELETE apk-regression/runs/<pk>/context/docs/<idx>/ 删除某份文档
    PATCH  apk-regression/runs/<pk>/context/            保存关联项目 / 人工补充说明
    GET    apk-regression/runs/<pk>/context/preview/    预览拼接后的资料（排障/确认用）

设计要点
--------
- 文档**抽取纯文本后存 JSON 字段**，不依赖原文件长期存在 —— 重新分析要反复读，
  且文件被清理后仍能复现"当时依据了什么"。
- 所有写操作都拒绝在 analyzing / running 状态下进行，避免分析线程读到半截数据。
"""
import logging
import os
import time

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import apk_context
from .models import ApkRegressionRun

logger = logging.getLogger(__name__)

#: 单个文档大小上限（解析成文本后还会再截到 DOC_CHAR_BUDGET）
MAX_DOC_BYTES = 20 * 1024 * 1024
#: 一次运行最多挂几份文档
MAX_DOCS = 10


def _busy(run):
    return run.status in ('analyzing', 'running')


def _project_name(project_id):
    """取项目名（projects 表）。

    ⚠️ 不能用 `run.project`：`ApkRegressionRun.project` 的外键指向 **UiProject**，
    而知识库来源是 **apps.projects.Project**。用户选「万年历」(projects.id=1) 后，
    `ui_projects` 里没有 id=1 的记录 ⇒ `run.project` 抛 DoesNotExist ⇒ 接口 500。
    这里统一按 projects 表查名字，查不到返回空串（不抛异常）。
    """
    if not project_id:
        return ''
    try:
        from apps.projects.models import Project
        return Project.objects.filter(id=project_id).values_list('name', flat=True).first() or ''
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 读取项目名失败 id=%s: %s', project_id, exc)
        return ''


def _doc_meta(d):
    """文档摘要（不回传全文，避免 detail 接口被几十万字撑爆）。"""
    return {
        'name': d.get('name') or '',
        'chars': int(d.get('chars') or len(d.get('text') or '')),
        'truncated': bool(d.get('truncated')),
        'uploaded_at': d.get('uploaded_at') or '',
        # 预览前 200 字，方便用户确认传对了没有
        'preview': (d.get('text') or '')[:200],
    }


# ------------------------------------------------------------------ 项目 / 知识库
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_options(request):
    """可关联的项目列表（供前端选择知识库来源）。"""
    try:
        from apps.projects.models import Project
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 无法导入 Project: %s', exc)
        return Response({'results': []})

    qs = Project.objects.all().order_by('-updated_at' if hasattr(Project, 'updated_at')
                                       else '-id')[:200]
    out = []
    for p in qs:
        try:
            kb_count = p.knowledge_base.filter(is_active=True).count()
        except Exception:  # noqa: BLE001
            kb_count = 0
        # 文件知识库（磁盘 md）与 DB 条目是两套存储，必须分别统计，
        # 否则「万年历」这种 DB 0 条、md 23 篇的项目在下拉框里会显得"没有知识"。
        try:
            file_info = apk_context.kb_inventory(p.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[APK-CTX] 统计文件知识库失败 project=%s: %s', p.id, exc)
            file_info = {'path': '', 'file_count': 0, 'files': []}
        out.append({
            'id': p.id,
            'name': getattr(p, 'name', '') or ('项目%d' % p.id),
            'knowledge_count': kb_count,
            'kb_file_count': file_info['file_count'],
            'kb_file_path': file_info['path'],
            'has_knowledge': bool(kb_count or file_info['file_count']),
        })
    return Response({'results': out})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_knowledge(request, pk):
    """项目知识库条目概览（只回标题/分类/字数，不回全文）。"""
    try:
        from apps.projects.models import ProjectKnowledge
    except Exception as exc:  # noqa: BLE001
        return Response({'error': '无法读取知识库模型：%s' % exc},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    items = ProjectKnowledge.objects.filter(project_id=pk, is_active=True)
    out = [{
        'id': k.id,
        'title': k.title,
        'category': k.category,
        'category_display': k.get_category_display(),
        'chars': len(k.content or ''),
    } for k in items]

    # 文件知识库（磁盘 md）：一并返回，前端「查看条目」才能看到页面里那批文件
    try:
        file_info = apk_context.kb_inventory(pk)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[APK-CTX] 读取文件知识库失败 project=%s: %s', pk, exc)
        file_info = {'path': '', 'file_count': 0, 'files': []}

    return Response({
        'results': out,
        'total': len(out),
        'total_chars': sum(x['chars'] for x in out),
        'kb_file_path': file_info['path'],
        'kb_file_count': file_info['file_count'],
        'kb_files': file_info['files'],
    })


# ------------------------------------------------------------------ 运行级资料
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_doc(request, pk):
    """上传一份操作说明文档 → 抽文本 → 存进 run.context_docs。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    if _busy(run):
        return Response({'error': '正在分析/执行中，请完成后再上传资料'},
                        status=status.HTTP_409_CONFLICT)

    f = request.FILES.get('file')
    if not f:
        return Response({'error': '缺少上传文件（字段名 file）'},
                        status=status.HTTP_400_BAD_REQUEST)
    if f.size > MAX_DOC_BYTES:
        return Response({'error': '文件超过 %d MB 限制' % (MAX_DOC_BYTES // 1024 // 1024)},
                        status=status.HTTP_400_BAD_REQUEST)

    docs = list(run.context_docs or [])
    if len(docs) >= MAX_DOCS:
        return Response({'error': '最多关联 %d 份文档，请先删除不用的' % MAX_DOCS},
                        status=status.HTTP_400_BAD_REQUEST)

    name = os.path.basename(getattr(f, 'name', '') or 'doc')
    # 落到临时文件再解析：docx/pdf 库都需要真实路径（不能只吃内存流）
    tmp_dir = os.path.join('/tmp', 'apk_ctx')
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, 'ctx_%s_%s' % (run.id, int(time.time() * 1000)))
    try:
        with open(tmp_path, 'wb') as out:
            for chunk in f.chunks():
                out.write(chunk)
        text = apk_context.extract_text(tmp_path, name)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        logger.error('[APK-CTX] 文档解析失败: %s', exc, exc_info=True)
        return Response({'error': '文档解析失败：%s' % str(exc)[:200]},
                        status=status.HTTP_400_BAD_REQUEST)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    text = apk_context._clean(text)
    if not text:
        return Response({'error': '未能从文档中提取到文字（可能是扫描版 PDF，需先 OCR）'},
                        status=status.HTTP_400_BAD_REQUEST)

    full_len = len(text)
    text, truncated = apk_context._truncate(text, apk_context.DOC_CHAR_BUDGET)
    docs.append({
        'name': name,
        'text': text,
        'chars': len(text),
        'source_chars': full_len,
        'truncated': truncated,
        'uploaded_at': timezone.now().isoformat(),
    })
    run.context_docs = docs
    run.save(update_fields=['context_docs'])
    return Response({
        'ok': True,
        'doc': _doc_meta(docs[-1]),
        'docs': [_doc_meta(d) for d in docs],
        'message': '已上传「%s」，提取 %d 字%s' % (
            name, full_len, '（超出预算已截断）' if truncated else ''),
    })


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_doc(request, pk, idx):
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    if _busy(run):
        return Response({'error': '正在分析/执行中，请完成后再修改资料'},
                        status=status.HTTP_409_CONFLICT)

    docs = list(run.context_docs or [])
    try:
        removed = docs.pop(int(idx))
    except (ValueError, IndexError):
        return Response({'error': '文档索引越界'}, status=status.HTTP_400_BAD_REQUEST)
    run.context_docs = docs
    run.save(update_fields=['context_docs'])
    return Response({'ok': True, 'docs': [_doc_meta(d) for d in docs],
                     'message': '已删除「%s」' % (removed.get('name') or '')})


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def run_context(request, pk):
    """GET: 查看当前资料配置；PATCH: 保存关联项目 / 人工补充说明。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(_context_payload(run))

    if _busy(run):
        return Response({'error': '正在分析/执行中，请完成后再修改资料'},
                        status=status.HTTP_409_CONFLICT)

    changes = []
    if 'project_id' in request.data:
        pid = request.data.get('project_id')
        if pid in (None, '', 0):
            run.project_id = None
        else:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                return Response({'error': 'project_id 必须是整数'},
                                status=status.HTTP_400_BAD_REQUEST)
            # 知识库来自 projects 表，先确认项目存在，别把无效 id 存进去
            # （存进去后 run.project 会因为外键指向 ui_projects 而抛异常）
            try:
                from apps.projects.models import Project
                exists = Project.objects.filter(id=pid_int).exists()
            except Exception as exc:  # noqa: BLE001
                logger.warning('[APK-CTX] 校验项目存在性失败 id=%s: %s', pid_int, exc)
                exists = True
            if not exists:
                return Response({'error': '项目不存在（id=%s）' % pid_int},
                                status=status.HTTP_400_BAD_REQUEST)
            run.project_id = pid_int
        changes.append('project')

    if 'context_notes' in request.data:
        notes = str(request.data.get('context_notes') or '')
        if len(notes) > 40000:
            return Response({'error': '补充说明过长（上限 40000 字）'},
                            status=status.HTTP_400_BAD_REQUEST)
        run.context_notes = notes
        changes.append('context_notes')

    if changes:
        run.save(update_fields=changes)
    return Response(_context_payload(run))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def context_preview(request, pk):
    """预览最终拼进提示词的资料内容（排障 + 让用户确认"模型到底看到了什么"）。"""
    run = ApkRegressionRun.objects.filter(id=pk).first()
    if not run:
        return Response({'error': '运行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
    built = apk_context.build_business_context(run)
    return Response({
        'chars': built['chars'],
        'empty': built['empty'],
        'sources': built['sources'],
        # 预览只给前 4000 字，够确认内容对不对，又不至于把接口响应撑大
        'text': built['text'][:4000],
        'truncated_preview': built['chars'] > 4000,
    })


def _context_payload(run):
    docs = run.context_docs or []
    kb_total = 0
    kb_chars = 0
    kb_file_count = 0
    kb_file_path = ''
    if run.project_id:
        try:
            from apps.projects.models import ProjectKnowledge
            qs = ProjectKnowledge.objects.filter(project_id=run.project_id, is_active=True)
            kb_total = qs.count()
            kb_chars = sum(len(k.content or '') for k in qs)
        except Exception:  # noqa: BLE001
            pass
        try:
            file_info = apk_context.kb_inventory(run.project_id)
            kb_file_count = file_info['file_count']
            kb_file_path = file_info['path']
        except Exception as exc:  # noqa: BLE001
            logger.warning('[APK-CTX] 统计文件知识库失败 run=%s: %s', run.id, exc)
    return {
        'project_id': run.project_id,
        'project_name': _project_name(run.project_id),
        'context_notes': run.context_notes or '',
        'docs': [_doc_meta(d) for d in docs],
        'doc_count': len(docs),
        'doc_chars': sum(int(d.get('chars') or 0) for d in docs),
        'knowledge_count': kb_total,
        'knowledge_chars': kb_chars,
        'kb_file_count': kb_file_count,
        'kb_file_path': kb_file_path,
        # 关联了项目即视为"已备料"：文件知识库也算资料，否则万年历这类
        # DB 0 条但 md 23 篇的项目会一直显示「未提供」。
        'has_context': bool(docs) or bool(kb_total) or bool(kb_file_count) or bool(
            (run.context_notes or '').strip()),
    }
