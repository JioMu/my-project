# -*- coding: utf-8 -*-
"""AI 智能模式专属模型配置的 CRUD + 连通性测试。

与 `views_config.AIIntelligentModeConfigViewSet`（读写 requirement_analysis.AIModelConfig）
的分工：

    旧入口 /configuration/ai-mode  →  AIModelConfig          →  其它功能继续用，**完全不动**
    新入口 /ai-intelligent-mode/model-config  →  AiModeModelConfig  →  只给 AI 智能模式用

两张表、两套 API、互不影响。这样在 AI 智能模式里"启用"某个模型，
不会把墨刀需求分析的 analyzer 一起禁掉。

路由（挂在 /api/ui-automation/ 下，见 urls.py）：
    GET    ai-mode-models/roles/              角色元数据（前端渲染表单用）
    GET    ai-mode-models/                    列表
    POST   ai-mode-models/                    新增
    GET    ai-mode-models/<pk>/               详情
    PUT    ai-mode-models/<pk>/               全量更新
    PATCH  ai-mode-models/<pk>/               局部更新（含启用切换）
    DELETE ai-mode-models/<pk>/               删除
    POST   ai-mode-models/test/               保存前试连（不落库）
    POST   ai-mode-models/<pk>/test/          已保存配置试连
"""
import logging

import requests
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AiModeModelConfig, get_active_ai_mode_config

logger = logging.getLogger(__name__)

_REQUIRED = ('name', 'role', 'model_type', 'model_name')

#: 32x32 纯色 PNG（data-uri），用于视觉角色的连通性测试。
#: 尺寸必须 > 10x10：实测 qwen3.7-plus 会拒收 1x1 图并返回 400
#: （height:1 or width:1 must be larger than 10），导致"可用"被误判成"失败"。
_TEST_PNG_32 = (
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR42u3N'
    'oQ0AAAgDsP1/LQKB5AoESZP6pnpORSAQCAQCgUAgEHwJFvxmcKYVpnX8AAAAAElFTkSuQmCC'
)


# ------------------------------------------------------------------ 序列化
def _mask(key):
    if not key:
        return ''
    if len(key) <= 8:
        return '*' * len(key)
    return '%s***%s' % (key[:4], key[-4:])


def _dump(cfg):
    return {
        'id': cfg.id,
        'name': cfg.name,
        'role': cfg.role,
        'role_display': cfg.get_role_display(),
        'model_type': cfg.model_type,
        'model_type_display': cfg.get_model_type_display(),
        'model_name': cfg.model_name,
        'base_url': cfg.base_url,
        'resolved_base_url': cfg.resolved_base_url(),
        'model_family': cfg.model_family,
        'resolved_family': cfg.resolved_family(),
        'api_key_masked': _mask(cfg.api_key),
        'has_api_key': bool((cfg.api_key or '').strip()),
        # 不回传明文 Key：前端编辑时留空即表示"不修改"
        'max_tokens': cfg.max_tokens,
        'temperature': cfg.temperature,
        'is_active': cfg.is_active,
        'notes': cfg.notes,
        'created_by_name': getattr(cfg.created_by, 'username', '') if cfg.created_by_id else '',
        'created_at': cfg.created_at,
        'updated_at': cfg.updated_at,
    }


def _apply_payload(cfg, data, creating=False):
    """把请求数据写进配置对象，返回错误信息（None 表示通过）。

    API Key 特殊处理：编辑时前端不传或传空串 = 保持原值，避免掩码回写把 Key 清空。
    """
    for f in _REQUIRED:
        if creating and not str(data.get(f) or '').strip():
            return '字段 %s 必填' % f

    if 'role' in data:
        role = (data.get('role') or '').strip()
        if role not in (AiModeModelConfig.ROLE_ANALYSIS, AiModeModelConfig.ROLE_VISION):
            return 'role 必须是 %s 或 %s' % (AiModeModelConfig.ROLE_ANALYSIS,
                                             AiModeModelConfig.ROLE_VISION)
        cfg.role = role

    if 'model_type' in data:
        mt = (data.get('model_type') or '').strip()
        valid = [c[0] for c in AiModeModelConfig.MODEL_CHOICES]
        if mt not in valid:
            return 'model_type 必须是 %s 之一' % '/'.join(valid)
        cfg.model_type = mt

    for f in ('name', 'model_name', 'base_url', 'model_family', 'notes'):
        if f in data:
            setattr(cfg, f, str(data.get(f) or '').strip())

    if 'api_key' in data:
        key = str(data.get('api_key') or '').strip()
        # 空串/纯掩码 → 视为"不修改"
        if key and '*' not in key:
            cfg.api_key = key

    for f, caster in (('max_tokens', int), ('temperature', float)):
        if f in data and data.get(f) not in (None, ''):
            try:
                setattr(cfg, f, caster(data.get(f)))
            except (TypeError, ValueError):
                return '字段 %s 必须是数字' % f

    if 'is_active' in data:
        cfg.is_active = bool(data.get('is_active'))

    if creating:
        if not (cfg.api_key or '').strip():
            return 'API Key 必填'
        if not cfg.model_name:
            return '模型名称必填'
        if not cfg.resolved_base_url():
            return 'Base URL 必填（该提供商没有内置默认地址，请手动填写）'
    return None


def _activate_exclusively(cfg):
    """启用一条配置时，禁用**同角色**其它配置。

    只动本表同角色 —— 这正是与 AIModelConfig 的关键差别：
    不会波及需求分析/用例生成等其它功能在用的模型。
    """
    if cfg.is_active:
        AiModeModelConfig.objects.filter(role=cfg.role).exclude(id=cfg.id).update(
            is_active=False)


# ------------------------------------------------------------------ 连通性
def _probe(model_type, base_url, api_key, model_name, want_vision=False):
    """用最小的 chat/completions 请求验证可用性。

    want_vision=True 时发一张 1x1 PNG 的 data-uri，顺带验证多模态链路
    —— 视觉角色配了纯文本模型，只有真发图才能提前发现（否则要等真机执行才炸）。
    """
    base = (base_url or '').strip().rstrip('/')
    if not base:
        return False, 'Base URL 为空'
    if not api_key:
        return False, 'API Key 为空'
    if not model_name:
        return False, '模型名称为空'

    url = base + '/chat/completions'
    headers = {'Authorization': 'Bearer %s' % api_key, 'Content-Type': 'application/json'}

    if want_vision:
        # ⚠️ 不能用 1x1 图：实测 qwen3.7-plus 直接拒收
        #    (<400> InvalidParameter: height:1 or width:1 must be larger than 10)，
        # 会把"模型可用"误报成"连接失败"。这里用 32x32 纯色 PNG，
        # 既满足各家最小尺寸要求，也不会浪费带宽。
        png = _TEST_PNG_32
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': '这是一张图片，只回答"OK"'},
            {'type': 'image_url', 'image_url': {'url': png}},
        ]}]
    else:
        messages = [{'role': 'user', 'content': 'Hi'}]

    payload = {'model': model_name, 'messages': messages, 'max_tokens': 8}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=(20, 120))
    except requests.exceptions.Timeout:
        return False, '连接超时（检查 Base URL 与网络）'
    except Exception as exc:  # noqa: BLE001
        return False, '请求异常：%s' % str(exc)[:200]

    if resp.status_code == 200:
        return True, '连接成功'
    body = (resp.text or '')[:400]
    if resp.status_code == 401:
        return False, 'API Key 无效（401）'
    if resp.status_code == 402:
        return False, '余额不足（402）：%s' % body
    if resp.status_code == 404:
        return False, '接口不存在（404），可能是 Base URL 不对：%s' % body
    if want_vision and resp.status_code == 400 and (
            'image' in body.lower() or 'multimodal' in body.lower()):
        # 模型收到了请求但拒收图片 —— 多半是配了纯文本模型，而不是网络/Key 问题。
        # 这个区分很重要：否则用户会去反复检查 Key。
        return False, ('该模型不接受图片输入，不能用于真机执行（视觉角色需多模态模型）：%s'
                       % body[:200])
    return False, 'HTTP %s：%s' % (resp.status_code, body)


# ------------------------------------------------------------------ 接口
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def role_meta(request):
    """角色 + 提供商元数据，前端据此渲染表单与默认值。"""
    return Response({
        'roles': [
            {'value': AiModeModelConfig.ROLE_ANALYSIS,
             'label': '分析与编排（文本推理）',
             'description': '负责解析 APK 清单、生成测试路径。纯文本模型即可，便宜快优先。',
             'need_vision': False},
            {'value': AiModeModelConfig.ROLE_VISION,
             'label': '真机执行（视觉多模态）',
             'description': '负责看手机截图做元素定位与断言。必须是多模态模型，否则执行必失败。',
             'need_vision': True},
        ],
        'model_types': [{'value': v, 'label': l} for v, l in AiModeModelConfig.MODEL_CHOICES],
        'default_base_urls': AiModeModelConfig.DEFAULT_BASE_URLS,
    })


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def config_list(request):
    if request.method == 'GET':
        qs = AiModeModelConfig.objects.select_related('created_by')
        role = (request.query_params.get('role') or '').strip()
        if role:
            qs = qs.filter(role=role)
        return Response({'results': [_dump(c) for c in qs]})

    data = request.data or {}
    cfg = AiModeModelConfig(created_by=request.user)
    err = _apply_payload(cfg, data, creating=True)
    if err:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        cfg.save()
        _activate_exclusively(cfg)
    return Response(_dump(cfg), status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def config_detail(request, pk):
    cfg = AiModeModelConfig.objects.filter(id=pk).first()
    if not cfg:
        return Response({'error': '配置不存在'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(_dump(cfg))

    if request.method == 'DELETE':
        cfg.delete()
        return Response({'ok': True})

    err = _apply_payload(cfg, request.data or {}, creating=False)
    if err:
        return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        cfg.save()
        _activate_exclusively(cfg)
    return Response(_dump(cfg))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_unsaved(request):
    """保存前试连。数据库里已有同 id 配置时，允许沿用它的 Key（前端留空编辑时）。"""
    data = request.data or {}
    api_key = str(data.get('api_key') or '').strip()
    if not api_key or '*' in api_key:
        existing = AiModeModelConfig.objects.filter(id=data.get('id')).first()
        api_key = (existing.api_key if existing else '') or ''
    model_type = (data.get('model_type') or '').strip()
    base_url = (data.get('base_url') or '').strip() or \
        AiModeModelConfig.DEFAULT_BASE_URLS.get(model_type, '')
    need_vision = (data.get('role') == AiModeModelConfig.ROLE_VISION)
    ok, msg = _probe(model_type, base_url, api_key,
                     (data.get('model_name') or '').strip(), need_vision)
    return Response({'success': ok, 'message': msg},
                    status=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_saved(request, pk):
    cfg = AiModeModelConfig.objects.filter(id=pk).first()
    if not cfg:
        return Response({'error': '配置不存在'}, status=status.HTTP_404_NOT_FOUND)
    ok, msg = _probe(cfg.model_type, cfg.resolved_base_url(), cfg.api_key,
                     cfg.model_name, cfg.role == AiModeModelConfig.ROLE_VISION)
    return Response({'success': ok, 'message': msg},
                    status=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def active_configs(request):
    """给前端/排障用：当前 AI 智能模式实际生效的是哪两个模型。"""
    out = {}
    for role in (AiModeModelConfig.ROLE_ANALYSIS, AiModeModelConfig.ROLE_VISION):
        cfg, err = get_active_ai_mode_config(role)
        out[role] = {'configured': cfg is not None, 'error': err,
                     'config': _dump(cfg) if cfg else None}
    return Response(out)
