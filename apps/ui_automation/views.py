from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.db import models
from django.utils import timezone
from apps.core.mixins import (
    OwnerOrMemberMixin, filter_by_owner_or_member,
    filter_visible, can_manage_project, can_delete_project,
)
import logging
import json
import os
import re
import random
import threading
import time
import subprocess
from urllib.parse import urlsplit

from .smart_locator import smart_match_locator, parse_swipe_pattern, get_wheel_coords, dump_page_elements

from .models import (
    UiProject, LocatorStrategy, Element, ElementGroup, TestScript, TestSuite,
    TestSuiteScript, TestExecution, Screenshot,
    PageObject, PageObjectElement, ScriptStep, ScriptElementUsage,
    TestCase, TestCaseStep, TestCaseExecution, OperationRecord,
    TestCase, TestCaseStep, TestCaseExecution, OperationRecord,
    UiScheduledTask, UiNotificationLog, UiTaskNotificationSetting,
    AICase, AIExecutionRecord,
    AppDevice, AppConfig, BusinessAction
)
from .serializers import (
    UiProjectSerializer, UiProjectCreateSerializer, UiProjectUpdateSerializer,
    LocatorStrategySerializer,
    ElementSerializer, ElementEnhancedSerializer,
    TestScriptSerializer, TestScriptCreateSerializer, TestScriptUpdateSerializer,
    TestSuiteSerializer, TestSuiteCreateSerializer, TestSuiteUpdateSerializer, TestSuiteWithScriptsSerializer,
    TestSuiteScriptSerializer, TestSuiteTestCaseSerializer,
    TestExecutionSerializer, TestExecutionCreateSerializer,
    ScreenshotSerializer,
    ElementGroupSerializer, ElementGroupCreateSerializer,
    PageObjectSerializer, PageObjectCreateSerializer, PageObjectElementSerializer,
    ScriptStepSerializer, ScriptElementUsageSerializer,
    ScriptAnalysisSerializer, ElementValidationSerializer, CodeGenerationSerializer,
    TestCaseSerializer, TestCaseStepSerializer, TestCaseExecutionSerializer, TestCaseRunSerializer,
    OperationRecordSerializer,
    UiScheduledTaskSerializer, UiNotificationLogSerializer, UiTaskNotificationSettingSerializer,
    AICaseSerializer, AIExecutionRecordSerializer,
    AppDeviceSerializer, AppConfigSerializer, BusinessActionSerializer
)
from .operation_logger import log_operation

logger = logging.getLogger(__name__)
User = get_user_model()

# 同一个 Midscene 用例一次最多并行跑几台设备。
# 限制的理由：每台设备都会各起一个 iOS Node 服务、各拉一份视觉模型调用，
# 同时跑太多会把服务器和第三方模型配额吃满。3 台是实测比较稳的档位。
MAX_PARALLEL_DEVICES = 3


def infer_appium_action_type(text):
    """根据步骤文字推断 Appium 操作类型（click/fill/assert/wait/screenshot）"""
    t = (text or '').lower()
    if any(k in t for k in ['输入', '填写', '录入', 'input']):
        return 'fill'
    if any(k in t for k in ['点击', '单击', '按下', '选择', 'click', 'tap', '点']):
        return 'click'
    if any(k in t for k in ['验证', '断言', '检查', '确认', '应该', '预期', 'assert', '校验']):
        return 'assert'
    if any(k in t for k in ['等待', 'wait', 'sleep']):
        return 'wait'
    if any(k in t for k in ['截图', 'screenshot']):
        return 'screenshot'
    return 'click'


def _infer_web_action_type(text):
    """根据步骤文字推断 Web/Playwright 操作类型（与 Appium 版基本一致，不含 launch_app/swipe 等）"""
    return infer_appium_action_type(text)


def extract_step_input_value(text):
    """从步骤文字中提取输入值（冒号/引号后的内容）"""
    if not text:
        return ''
    m = re.search(r'[：:]\s*["\']?(.+?)["\']?$', text.strip())
    if m:
        return m.group(1).strip()
    return ''


def extract_step_info(s, step_index):
    """提取步骤信息的辅助函数，确保返回可读的步骤描述"""
    step_info = {'step': step_index}

    # 尝试多种方式提取可读信息
    if hasattr(s, 'action'):
        # 如果有action属性
        action_data = s.action
        if isinstance(action_data, str):
            step_info['action'] = action_data
        elif hasattr(action_data, '__dict__'):
            # 如果是对象，提取关键属性
            attrs = {}
            for key in ['type', 'description', 'goal', 'coordinate', 'text', 'output', 'result']:
                if hasattr(action_data, key):
                    value = getattr(action_data, key)
                    if isinstance(value, str):
                        attrs[key] = value
                    elif callable(value):
                       attrs[key] = getattr(value, '__name__', str(value))
                    else:
                        attrs[key] = str(value)
            if attrs:
                step_info['action'] = attrs
        else:
            step_info['action'] = str(action_data)
    elif hasattr(s, 'model_output'):
        # 如果有model_output属性
        output_data = s.model_output
        if isinstance(output_data, str):
            step_info['action'] = output_data
        elif hasattr(output_data, '__dict__'):
            # 提取model_output的关键信息
            attrs = {'type': 'model_output'}
            for key in ['action', 'description', 'goal', 'coordinate', 'text']:
                if hasattr(output_data, key):
                    value = getattr(output_data, key)
                    attrs[key] = str(value) if value else None
            step_info['action'] = attrs
        else:
            step_info['action'] = str(output_data)
    elif hasattr(s, '__dict__'):
        # 通用的对象提取
        attrs = {}
        for key in dir(s):
            if not key.startswith('_'):
                try:
                    value = getattr(s, key)
                    if not callable(value):
                        attrs[key] = str(value)
                except:
                    pass
        if attrs:
            step_info['action'] = attrs
    else:
        # 最后回退，但检查是否是函数对象
        if callable(s):
            step_info['action'] = f"<Action: {getattr(s, '__name__', 'unknown action')}>"
        else:
            step_info['action'] = str(s)

    return step_info


from rest_framework.pagination import PageNumberPagination

class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 1000


# AI 项目状态 → UI 项目状态 映射
_UI_AI_STATUS_MAP = {
    'active': 'IN_PROGRESS',
    'paused': 'NOT_STARTED',
    'completed': 'COMPLETED',
    'archived': 'COMPLETED',
}


class UiProjectViewSet(viewsets.ModelViewSet):
    """
    UI 自动化项目 CRUD。

    2026-09-20 用户要求「每个人都能查看别人创建的项目，只是不能删除」：
      - list/retrieve 查看：全员可见
      - create/update    ：登录即可（update 额外要求负责人/成员/管理员）
      - destroy 删除     ：仅负责人 + 管理员
    """
    queryset = UiProject.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'owner', 'members']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']

    def get_queryset(self):
        # 查看放开：所有登录用户可见全部 UI 项目。
        # ⚠️ 这里不能用 OwnerOrMemberMixin，否则用户看不到别人创建的项目。
        qs = super().get_queryset()
        user = getattr(self.request, 'user', None)
        if user is None or not user.is_authenticated:
            return qs.none()
        return qs.distinct()

    def _deny_if_not_manageable(self, obj):
        """改/删前校验：负责人、成员或管理员。"""
        if can_manage_project(self.request.user, obj):
            return None
        return Response(
            {'error': '只有项目负责人、项目成员或管理员可以修改该项目'},
            status=status.HTTP_403_FORBIDDEN
        )

    def update(self, request, *args, **kwargs):
        denied = self._deny_if_not_manageable(self.get_object())
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        denied = self._deny_if_not_manageable(self.get_object())
        if denied:
            return denied
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        # 删除从严：仅负责人 + 管理员（成员也不行），与主 Project 保持一致
        if not can_delete_project(request.user, obj):
            return Response(
                {'error': '只有项目负责人或管理员可以删除项目'},
                status=status.HTTP_403_FORBIDDEN
            )
        return super().destroy(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.action == 'create':
            return UiProjectCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return UiProjectUpdateSerializer
        return UiProjectSerializer

    def list(self, request, *args, **kwargs):
        """
        列表：合并 UI 自动化项目 + AI 用例生成项目（全局统一项目源）
        AI 项目标记 source='ai' 且 id 带 proj_ 前缀，前端据此禁用编辑/删除
        """
        from rest_framework.response import Response

        # 1) 原生 UI 项目（全部取出，后续与 AI 项目一起手动分页）
        queryset = self.filter_queryset(self.get_queryset())
        ui_results = self.get_serializer(queryset, many=True).data

        # 2) AI 用例生成项目（apps.projects.Project）
        from apps.projects.models import Project as AiProject
        from apps.testcases.models import TestCase as AiTestCase
        search = request.query_params.get('search', '').strip()
        status = request.query_params.get('status', '').strip()
        try:
            ai_qs = AiProject.objects.all().prefetch_related('members', 'owner')
            if search:
                ai_qs = ai_qs.filter(name__icontains=search)
            ai_projects = ai_qs
        except Exception as e:
            logger.warning('UI项目管理加载AI项目失败: %s', e)
            ai_projects = []

        ai_results = []
        for p in ai_projects:
            mapped_status = _UI_AI_STATUS_MAP.get(p.status, 'IN_PROGRESS')
            if status and mapped_status != status:
                continue
            ai_results.append({
                'id': f'proj_{p.id}',
                'real_id': p.id,
                'name': p.name,
                'description': p.description or '',
                'status': mapped_status,
                'base_url': '',
                'start_date': None,
                'end_date': None,
                'owner_name': p.owner.username if p.owner else None,
                'member_count': p.members.count(),
                'test_case_count': AiTestCase.objects.filter(project=p).count(),
                'created_at': p.created_at.isoformat() if p.created_at else None,
                'updated_at': p.updated_at.isoformat() if p.updated_at else None,
                'source': 'ai',
            })

        # 3) 合并（UI 项目在前，AI 项目在后）
        merged = list(ui_results) + ai_results

        # 4) 手动分页（保持与前端分页参数兼容）
        try:
            page_size = int(request.query_params.get('page_size', 20))
            page = int(request.query_params.get('page', 1))
        except (TypeError, ValueError):
            page_size, page = 20, 1
        page_size = max(page_size, 1)
        page = max(page, 1)
        start = (page - 1) * page_size
        end = start + page_size
        page_data = merged[start:end]

        return Response({
            'count': len(merged),
            'next': None,
            'previous': None,
            'results': page_data,
        })

    def perform_create(self, serializer):
        # 创建项目时，当前用户自动成为负责人
        instance = serializer.save(owner=self.request.user)
        # 记录操作
        log_operation('create', 'project', instance.id, instance.name, self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        # 记录操作
        log_operation('edit', 'project', instance.id, instance.name, self.request.user)

    def perform_destroy(self, instance):
        # 记录操作（在删除前记录）
        log_operation('delete', 'project', instance.id, instance.name, self.request.user)
        instance.delete()


class LocatorStrategyViewSet(viewsets.ModelViewSet):
    queryset = LocatorStrategy.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = LocatorStrategySerializer
    ordering = ['id']


class ElementViewSet(viewsets.ModelViewSet):
    queryset = Element.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'locator_strategy', 'element_type', 'validation_status', 'group']
    search_fields = ['name', 'description', 'page', 'component_name']

    def get_serializer_class(self):
        if self.action in ['list', 'retrieve']:
            return ElementEnhancedSerializer
        return ElementSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的项目的元素
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return Element.objects.filter(project__in=accessible_projects).select_related(
            'project', 'group', 'locator_strategy', 'created_by', 'parent_element'
        ).prefetch_related('script_usages__script').order_by('page', 'name')

    def filter_queryset(self, queryset):
        # 先应用默认的过滤器
        queryset = super().filter_queryset(queryset)

        # 处理页面筛选（使用page_name参数避免与分页page冲突）
        page_name = self.request.query_params.get('page_name', None)
        if page_name:
            queryset = queryset.filter(page=page_name)

        return queryset

    def perform_create(self, serializer):
        # 创建元素时自动设置创建人
        instance = serializer.save(created_by=self.request.user)
        # 记录操作
        log_operation('create', 'element', instance.id, instance.name, self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        # 记录操作
        log_operation('edit', 'element', instance.id, instance.name, self.request.user)

    def perform_destroy(self, instance):
        # 记录操作（在删除前记录）
        log_operation('delete', 'element', instance.id, instance.name, self.request.user)
        instance.delete()

    @action(detail=True, methods=['post'])
    def validate_locator(self, request, pk=None):
        """验证元素定位器有效性"""
        element = self.get_object()

        # 这里可以集成实际的浏览器验证逻辑
        # 现在只是模拟验证
        validation_result = self._perform_element_validation(element)

        element.validation_status = 'VALID' if validation_result['is_valid'] else 'INVALID'
        element.validation_message = validation_result['validation_message']
        element.last_validated = timezone.now()
        element.save()

        serializer = ElementValidationSerializer(validation_result)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def usages(self, request, pk=None):
        """获取元素在脚本中的使用情况"""
        element = self.get_object()
        usages = ScriptElementUsage.objects.filter(element=element).select_related('script')
        serializer = ScriptElementUsageSerializer(usages, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def tree(self, request):
        """获取元素树形结构"""
        project_id = request.query_params.get('project')
        if not project_id:
            return Response({'error': '需要指定项目ID'}, status=status.HTTP_400_BAD_REQUEST)

        elements = self.get_queryset().filter(project_id=project_id)
        tree_data = self._build_element_tree(elements)
        return Response(tree_data)

    @action(detail=True, methods=['post'])
    def add_backup_locator(self, request, pk=None):
        """添加备用定位器"""
        element = self.get_object()
        strategy = request.data.get('strategy')
        value = request.data.get('value')

        if not strategy or not value:
            return Response({'error': '策略和值都是必需的'}, status=status.HTTP_400_BAD_REQUEST)

        backup_locators = element.backup_locators or []
        backup_locators.append({'strategy': strategy, 'value': value})
        element.backup_locators = backup_locators
        element.save()

        return Response({'message': '备用定位器添加成功'})

    @action(detail=True, methods=['post'])
    def generate_suggestions(self, request, pk=None):
        """生成元素使用建议"""
        element = self.get_object()
        suggestions = self._generate_element_suggestions(element)
        return Response({'suggestions': suggestions})

    def _perform_element_validation(self, element):
        """执行元素验证（模拟实现）"""
        try:
            # 这里可以集成实际的浏览器自动化工具进行验证
            # 现在只是简单的语法检查
            is_valid = True
            message = "定位器验证通过"
            suggestions = []

            # 简单的语法检查
            if element.locator_strategy.name == 'css':
                if not element.locator_value.strip():
                    is_valid = False
                    message = "CSS选择器不能为空"
            elif element.locator_strategy.name == 'xpath':
                if not element.locator_value.strip():
                    is_valid = False
                    message = "XPath表达式不能为空"

            return {
                'is_valid': is_valid,
                'validation_message': message,
                'suggestions': suggestions
            }
        except Exception as e:
            return {
                'is_valid': False,
                'validation_message': f'验证过程中出现错误: {str(e)}',
                'suggestions': []
            }

    def _build_element_tree(self, elements):
        """构建元素树形结构 - 返回元素列表而不是页面分组，因为前端会自己处理页面关联"""
        element_data_list = []
        for element in elements:
            element_data = {
                'id': element.id,
                'name': element.name,
                'type': 'element',
                'element_type': element.element_type,
                'locator_strategy': element.locator_strategy.name if element.locator_strategy else None,
                'locator_value': element.locator_value,
                'validation_status': element.validation_status,
                'usage_count': element.usage_count,
                'group_id': element.group_id,  # 用于前端关联到页面
                'page': element.page,  # 保留向后兼容
                'children': []
            }
            element_data_list.append(element_data)

        return element_data_list

    def _generate_element_suggestions(self, element):
        """生成元素使用建议"""
        suggestions = []

        # 基于元素类型生成建议
        if element.element_type == 'INPUT':
            suggestions.append("建议为输入框元素添加清空和输入验证操作")
        elif element.element_type == 'BUTTON':
            suggestions.append("建议验证按钮点击后的页面跳转或状态变化")
        elif element.element_type == 'DROPDOWN':
            suggestions.append("建议测试下拉框的所有选项")

        # 基于使用频率生成建议
        if element.usage_count == 0:
            suggestions.append("此元素尚未在任何脚本中使用，考虑是否需要删除")
        elif element.usage_count > 10:
            suggestions.append("此元素使用频率较高，建议添加到页面对象中以提高复用性")

        return suggestions


class ElementGroupViewSet(viewsets.ModelViewSet):
    queryset = ElementGroup.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'parent_group']
    search_fields = ['name', 'description']

    def get_serializer_class(self):
        if self.action == 'create':
            return ElementGroupCreateSerializer
        return ElementGroupSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的项目的元素分组
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return ElementGroup.objects.filter(project__in=accessible_projects).select_related('project', 'parent_group').order_by('order', 'name')

    @action(detail=False, methods=['get'])
    def tree(self, request):
        """获取分组树形结构"""
        project_id = request.query_params.get('project')
        if not project_id:
            return Response({'error': '需要指定项目ID'}, status=status.HTTP_400_BAD_REQUEST)

        groups = self.get_queryset().filter(project_id=project_id, parent_group__isnull=True)
        serializer = ElementGroupSerializer(groups, many=True)
        return Response(serializer.data)


class PageObjectViewSet(viewsets.ModelViewSet):
    queryset = PageObject.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project']
    search_fields = ['name', 'class_name', 'description']

    def get_serializer_class(self):
        if self.action == 'create':
            return PageObjectCreateSerializer
        return PageObjectSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的项目的页面对象
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return PageObject.objects.filter(project__in=accessible_projects).select_related(
            'project', 'created_by'
        ).prefetch_related('page_object_elements__element').order_by('-created_at')

    @action(detail=True, methods=['post'])
    def generate_code(self, request, pk=None):
        """生成页面对象代码"""
        page_object = self.get_object()
        serializer = CodeGenerationSerializer(data=request.data)

        if serializer.is_valid():
            language = serializer.validated_data['language']
            framework = serializer.validated_data['framework']
            include_comments = serializer.validated_data['include_comments']

            try:
                generated_code = page_object.generate_code(language)

                # 保存生成的代码模板
                page_object.template_code = generated_code
                page_object.save()

                return Response({
                    'code': generated_code,
                    'language': language,
                    'framework': framework
                })
            except Exception as e:
                return Response({
                    'error': f'代码生成失败: {str(e)}'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_element(self, request, pk=None):
        """向页面对象添加元素"""
        page_object = self.get_object()
        serializer = PageObjectElementSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save(page_object=page_object)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def elements(self, request, pk=None):
        """获取页面对象的所有元素"""
        page_object = self.get_object()
        po_elements = page_object.page_object_elements.select_related('element').all()
        serializer = PageObjectElementSerializer(po_elements, many=True)
        return Response(serializer.data)


class PageObjectElementViewSet(viewsets.ModelViewSet):
    queryset = PageObjectElement.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = PageObjectElementSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的页面对象元素
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return PageObjectElement.objects.filter(
            page_object__project__in=accessible_projects
        ).select_related('page_object', 'element').order_by('id')


class ScriptStepViewSet(viewsets.ModelViewSet):
    queryset = ScriptStep.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = ScriptStepSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['script', 'action_type', 'target_element', 'page_object']

    def get_queryset(self):
        # 只显示用户有权限访问的脚本步骤
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return ScriptStep.objects.filter(
            script__project__in=accessible_projects
        ).select_related('script', 'target_element', 'page_object').order_by('step_order')

    @action(detail=False, methods=['post'])
    def batch_create(self, request):
        """批量创建脚本步骤"""
        steps_data = request.data.get('steps', [])
        created_steps = []

        for step_data in steps_data:
            serializer = ScriptStepSerializer(data=step_data)
            if serializer.is_valid():
                step = serializer.save()
                created_steps.append(step)
            else:
                return Response({
                    'error': f'步骤创建失败: {serializer.errors}'
                }, status=status.HTTP_400_BAD_REQUEST)

        response_serializer = ScriptStepSerializer(created_steps, many=True)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ScriptElementUsageViewSet(viewsets.ModelViewSet):
    queryset = ScriptElementUsage.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = ScriptElementUsageSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['script', 'element', 'usage_type']

    def get_queryset(self):
        # 只显示用户有权限访问的脚本元素使用记录
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return ScriptElementUsage.objects.filter(
            script__project__in=accessible_projects
        ).select_related('script', 'element').order_by('script', 'line_number')

    @action(detail=False, methods=['post'])
    def analyze_script(self, request):
        """分析脚本中的元素使用情况"""
        script_id = request.data.get('script_id')
        if not script_id:
            return Response({'error': '需要指定脚本ID'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            script = TestScript.objects.get(id=script_id)
            analysis_result = self._analyze_script_elements(script)

            serializer = ScriptAnalysisSerializer(analysis_result)
            return Response(serializer.data)
        except TestScript.DoesNotExist:
            return Response({'error': '脚本不存在'}, status=status.HTTP_404_NOT_FOUND)

    def _analyze_script_elements(self, script):
        """分析脚本中的元素使用"""
        # 解析脚本内容，查找元素使用情况
        content = script.content
        usages = []
        missing_elements = []
        recommendations = []

        # 简单的元素使用分析（实际实现会更复杂）
        if script.script_type == 'CODE':
            # 分析代码中的定位器使用
            locator_patterns = [
                r'locator\(["\']([^"\']+)["\']\)',
                r'findElement\(["\']([^"\']+)["\']\)',
                r'css\(["\']([^"\']+)["\']\)',
                r'xpath\(["\']([^"\']+)["\']\)'
            ]

            for pattern in locator_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    # 查找对应的元素
                    try:
                        element = Element.objects.get(
                            project=script.project,
                            locator_value=match
                        )
                        usage, created = ScriptElementUsage.objects.get_or_create(
                            script=script,
                            element=element,
                            defaults={
                                'usage_type': 'CLICK',  # 默认类型
                                'line_number': 1,  # 需要实际解析
                                'frequency': 1
                            }
                        )
                        if not created:
                            usage.frequency += 1
                            usage.save()

                        element.increment_usage_count()
                        usages.append(usage)
                    except Element.DoesNotExist:
                        missing_elements.append(match)

        # 生成建议
        if missing_elements:
            recommendations.append(f"发现 {len(missing_elements)} 个未定义的元素定位器")

        if len(usages) > 20:
            recommendations.append("脚本复杂度较高，建议拆分为多个小脚本")

        complexity_score = min(100, len(usages) * 5)

        return {
            'element_usages': usages,
            'missing_elements': missing_elements,
            'recommendations': recommendations,
            'complexity_score': complexity_score
        }


class TestScriptViewSet(viewsets.ModelViewSet):
    queryset = TestScript.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'script_type']
    search_fields = ['name', 'description']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'create':
            return TestScriptCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TestScriptUpdateSerializer
        return TestScriptSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的项目的测试脚本
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return TestScript.objects.filter(project__in=accessible_projects)


class TestSuiteViewSet(viewsets.ModelViewSet):
    queryset = TestSuite.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project']
    search_fields = ['name', 'description']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'create':
            return TestSuiteCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TestSuiteUpdateSerializer
        elif self.action == 'retrieve':
            return TestSuiteWithScriptsSerializer
        return TestSuiteSerializer

    def get_queryset(self):
        # 只显示用户有权限访问的项目的测试套件
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return TestSuite.objects.filter(project__in=accessible_projects)

    def perform_create(self, serializer):
        instance = serializer.save()
        # 记录操作
        log_operation('create', 'suite', instance.id, instance.name, self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        # 记录操作
        log_operation('edit', 'suite', instance.id, instance.name, self.request.user)

    def perform_destroy(self, instance):
        # 记录操作（在删除前记录）
        log_operation('delete', 'suite', instance.id, instance.name, self.request.user)
        instance.delete()

    @action(detail=True, methods=['get'])
    def scripts(self, request, pk=None):
        """获取测试套件中的所有脚本"""
        test_suite = self.get_object()
        scripts = test_suite.suite_scripts.all()
        serializer = TestSuiteScriptSerializer(scripts, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def add_script(self, request, pk=None):
        """向测试套件添加脚本"""
        test_suite = self.get_object()
        data = request.data
        data['test_suite'] = pk
        serializer = TestSuiteScriptSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['delete'])
    def remove_script(self, request, pk=None, script_id=None):
        """从测试套件移除脚本"""
        test_suite = self.get_object()
        try:
            suite_script = TestSuiteScript.objects.get(test_suite=test_suite, id=script_id)
            suite_script.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except TestSuiteScript.DoesNotExist:
            return Response({'error': '脚本不存在于该测试套件中'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['get'])
    def test_cases(self, request, pk=None):
        """获取测试套件中的所有测试用例"""
        test_suite = self.get_object()
        test_cases = test_suite.suite_test_cases.all()
        serializer = TestSuiteTestCaseSerializer(test_cases, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def add_test_case(self, request, pk=None):
        """向测试套件添加测试用例"""
        test_suite = self.get_object()
        test_case_id = request.data.get('test_case_id')
        order = request.data.get('order', 0)

        try:
            from .models import TestSuiteTestCase
            suite_test_case = TestSuiteTestCase.objects.create(
                test_suite=test_suite,
                test_case_id=test_case_id,
                order=order
            )
            serializer = TestSuiteTestCaseSerializer(suite_test_case)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['delete'])
    def remove_test_case(self, request, pk=None):
        """从测试套件移除测试用例"""
        test_suite = self.get_object()
        test_case_id = request.data.get('test_case_id')

        try:
            from .models import TestSuiteTestCase
            suite_test_case = TestSuiteTestCase.objects.get(
                test_suite=test_suite,
                test_case_id=test_case_id
            )
            suite_test_case.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except TestSuiteTestCase.DoesNotExist:
            return Response({'error': '测试用例不存在于该测试套件中'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['post'])
    def update_test_case_order(self, request, pk=None):
        """更新测试套件中测试用例的顺序"""
        test_suite = self.get_object()
        test_case_orders = request.data.get('test_case_orders', [])

        try:
            from .models import TestSuiteTestCase
            for item in test_case_orders:
                TestSuiteTestCase.objects.filter(
                    test_suite=test_suite,
                    test_case_id=item['test_case_id']
                ).update(order=item['order'])

            return Response({'message': '顺序更新成功'}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def run_suite(self, request, pk=None):
        """执行测试套件"""
        test_suite = self.get_object()

        # 传统模式执行（Playwright/Selenium）
        # 检查是否包含测试用例
        test_case_count = test_suite.suite_test_cases.count()
        if test_case_count == 0:
            return Response({
                'error': '该测试套件未包含任何测试用例，无法执行'
            }, status=status.HTTP_400_BAD_REQUEST)

        engine = request.data.get('engine', 'playwright')
        browser = request.data.get('browser', 'chrome')
        headless = request.data.get('headless', False)
        device_id = request.data.get('device_id', None)
        app_config_id = request.data.get('app_config_id', None)

        # Appium 引擎必须指定设备和应用
        if engine == 'appium' and (not device_id or not app_config_id):
            return Response({
                'error': '使用 Appium 引擎时必须选择设备和应用配置'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 更新套件执行状态为运行中
        test_suite.execution_status = 'running'
        test_suite.save()

        try:
            # 在后台线程中执行测试
            import threading
            from .test_executor import TestExecutor

            def run_test():
                executor = TestExecutor(
                    test_suite=test_suite,
                    engine=engine,
                    browser=browser,
                    headless=headless,
                    executed_by=request.user,
                    device_id=device_id,
                    app_config_id=app_config_id
                )
                executor.run()

            # 启动后台线程执行测试
            thread = threading.Thread(target=run_test)
            thread.daemon = True
            thread.start()

            # 记录运行操作
            log_operation('run', 'suite', test_suite.id, test_suite.name, request.user)

            response_data = {
                'message': '测试套件开始执行',
                'suite_id': test_suite.id,
                'test_case_count': test_case_count,
                'engine': engine,
                'browser': browser,
                'headless': headless
            }
            if device_id:
                response_data['device_id'] = device_id
            if app_config_id:
                response_data['app_config_id'] = app_config_id
            return Response(response_data, status=status.HTTP_200_OK)
        except Exception as e:
            test_suite.execution_status = 'failed'
            test_suite.save()
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TestExecutionViewSet(viewsets.ModelViewSet):
    queryset = TestExecution.objects.all()
    permission_classes = [IsAuthenticated]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'test_suite', 'test_script', 'status', 'environment', 'executed_by']
    search_fields = ['error_message']
    ordering = ['-created_at']
    ordering = ['-created_at']
    pagination_class = StandardPagination

    def get_queryset(self):
        # 只显示用户有权限访问的项目的测试执行记录
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return TestExecution.objects.filter(
            project__in=accessible_projects
        ).select_related('project', 'test_suite', 'test_script', 'executed_by')

    def get_serializer_class(self):
        if self.action == 'create':
            return TestExecutionCreateSerializer
        return TestExecutionSerializer



    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """运行测试执行"""
        execution = self.get_object()
        execution.status = 'RUNNING'
        execution.started_at = timezone.now()
        execution.save()
        
        # 这里应该有实际的测试运行逻辑，这里只是模拟
        # 在实际实现中，这里可能会启动一个异步任务来执行测试
        
        # 模拟测试运行结果
        import time
        time.sleep(2)  # 模拟测试执行时间
        
        execution.status = 'SUCCESS'  # 假设测试成功
        execution.finished_at = timezone.now()
        execution.result_data = {
            'steps': [
                {'name': 'Step 1', 'status': 'PASS', 'duration': 1.2},
                {'name': 'Step 2', 'status': 'PASS', 'duration': 0.8},
                {'name': 'Step 3', 'status': 'PASS', 'duration': 1.5},
            ],
            'total_steps': 3,
            'passed_steps': 3,
            'failed_steps': 0,
            'duration': 3.5
        }
        execution.save()
        
        serializer = TestExecutionSerializer(execution)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def abort(self, request, pk=None):
        """中止测试执行"""
        execution = self.get_object()
        if execution.status == 'RUNNING':
            execution.status = 'ABORTED'
            execution.finished_at = timezone.now()
            execution.save()
            return Response(TestExecutionSerializer(execution).data)
        return Response({'error': '测试执行未在运行中'}, status=status.HTTP_400_BAD_REQUEST)

    def perform_destroy(self, instance):
        # 记录操作（删除测试报告）
        suite_name = instance.test_suite.name if instance.test_suite else f"执行记录#{instance.id}"
        log_operation('delete', 'report', instance.id, suite_name, self.request.user)
        instance.delete()


class ScreenshotViewSet(viewsets.ModelViewSet):
    queryset = Screenshot.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = ScreenshotSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['execution']

    def get_queryset(self):
        # 只显示用户有权限访问的项目的截图
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        executions = TestExecution.objects.filter(project__in=accessible_projects)
        return Screenshot.objects.filter(execution__in=executions)


class TestCaseViewSet(viewsets.ModelViewSet):
    """测试用例视图集"""
    queryset = TestCase.objects.all()
    serializer_class = TestCaseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name', 'priority', 'status']
    ordering = ['-created_at']
    filterset_fields = ['project', 'status', 'priority', 'created_by']

    def get_queryset(self):
        # 只显示用户有权限访问的项目的测试用例
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        qs = TestCase.objects.filter(project__in=accessible_projects).select_related('project', 'created_by')
        # 按 tags 筛选（如 tags=冒烟）
        tag_filter = self.request.query_params.get('tags')
        if tag_filter:
            qs = qs.filter(tags__contains=[tag_filter])
        return qs

    def perform_create(self, serializer):
        # 创建测试用例
        instance = serializer.save(created_by=self.request.user)

        # 记录操作
        log_operation('create', 'test_case', instance.id, instance.name, self.request.user)

        # 处理步骤数据
        steps_data = self.request.data.get('steps', [])
        logger.info(f"创建测试用例 {instance.id} 的步骤数据: {len(steps_data)} 个步骤")

        if steps_data:
            # 创建新步骤
            created_count = 0
            for i, step_data in enumerate(steps_data):
                # 确保步骤数据结构正确
                step_data = dict(step_data)  # 创建副本避免修改原数据
                step_data['test_case'] = instance.id  # 使用测试用例ID
                step_data['step_number'] = i + 1  # 确保步骤序号正确

                # 处理元素ID
                if 'element_id' in step_data:
                    step_data['element'] = step_data.pop('element_id')

                # 移除只读字段
                step_data.pop('id', None)
                step_data.pop('element_name', None)
                step_data.pop('element_locator', None)
                step_data.pop('created_at', None)
                step_data.pop('expanded', None)  # 前端UI状态字段

                # 使用模型直接创建，避免序列化器的复杂性
                try:
                    TestCaseStep.objects.create(
                        test_case=instance,
                        step_number=step_data.get('step_number', i + 1),
                        action_type=step_data.get('action_type', 'click'),
                        element_id=step_data.get('element') if step_data.get('element') else None,
                        input_value=step_data.get('input_value', ''),
                        wait_time=step_data.get('wait_time', 1000),
                        assert_type=step_data.get('assert_type', ''),
                        assert_value=step_data.get('assert_value', ''),
                        description=step_data.get('description', '')
                    )
                    created_count += 1
                except Exception as e:
                    logger.error(f"创建步骤 {i+1} 失败: {str(e)}")
                    logger.error(f"步骤数据: {step_data}")

            logger.info(f"成功创建了 {created_count} 个新步骤")

    @action(detail=True, methods=['post'])
    def copy_case(self, request, pk=None):
        """复制测试用例"""
        test_case = self.get_object()
        
        try:
            # 1. 复制测试用例基本信息
            new_case = TestCase.objects.create(
                project=test_case.project,
                name=f"{test_case.name}_copy",
                description=test_case.description,
                priority=test_case.priority,
                status=test_case.status,
                created_by=request.user
            )
            
            # 2. 复制测试步骤
            steps = test_case.steps.all().order_by('step_number')
            new_steps = []
            for step in steps:
                new_steps.append(TestCaseStep(
                    test_case=new_case,
                    step_number=step.step_number,
                    action_type=step.action_type,
                    element=step.element,
                    input_value=step.input_value,
                    wait_time=step.wait_time,
                    assert_type=step.assert_type,
                    assert_value=step.assert_value,
                    description=step.description
                ))
            
            if new_steps:
                TestCaseStep.objects.bulk_create(new_steps)
            
            # 记录操作
            log_operation('create', 'test_case', new_case.id, new_case.name, request.user)
            
            serializer = self.get_serializer(new_case)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"复制测试用例失败: {str(e)}")
            return Response({'error': f"复制失败: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def perform_update(self, serializer):
        # 更新测试用例步骤
        instance = serializer.save()

        # 记录操作
        log_operation('edit', 'test_case', instance.id, instance.name, self.request.user)

        # 处理步骤数据
        steps_data = self.request.data.get('steps', [])
        logger.info(f"更新测试用例 {instance.id} 的步骤数据: {len(steps_data)} 个步骤")

        if steps_data:
            # 删除现有步骤
            existing_steps_count = instance.steps.count()
            instance.steps.all().delete()
            logger.info(f"删除了 {existing_steps_count} 个现有步骤")

            # 创建新步骤
            created_count = 0
            for i, step_data in enumerate(steps_data):
                # 确保步骤数据结构正确
                step_data = dict(step_data)  # 创建副本避免修改原数据
                step_data['test_case'] = instance.id  # 使用测试用例ID
                step_data['step_number'] = i + 1  # 确保步骤序号正确

                # 处理元素ID
                if 'element_id' in step_data:
                    step_data['element'] = step_data.pop('element_id')

                # 移除只读字段
                step_data.pop('id', None)
                step_data.pop('element_name', None)
                step_data.pop('element_locator', None)
                step_data.pop('created_at', None)
                step_data.pop('expanded', None)  # 前端UI状态字段

                # 使用模型直接创建，避免序列化器的复杂性
                try:
                    TestCaseStep.objects.create(
                        test_case=instance,
                        step_number=step_data.get('step_number', i + 1),
                        action_type=step_data.get('action_type', 'click'),
                        element_id=step_data.get('element') if step_data.get('element') else None,
                        input_value=step_data.get('input_value', ''),
                        wait_time=step_data.get('wait_time', 1000),
                        assert_type=step_data.get('assert_type', ''),
                        assert_value=step_data.get('assert_value', ''),
                        description=step_data.get('description', '')
                    )
                    created_count += 1
                except Exception as e:
                    logger.error(f"创建步骤 {i+1} 失败: {str(e)}")
                    logger.error(f"步骤数据: {step_data}")

            logger.info(f"成功创建了 {created_count} 个新步骤")

    def _generate_step_log(self, step, step_result='success'):
        """根据测试步骤生成执行日志"""
        import time

        # 模拟执行时间（0.1秒到2秒之间）
        execution_time = round(random.uniform(0.1, 2.0), 2)

        # 构建基础日志
        log_parts = []

        # 步骤信息
        if step.element:
            element_name = step.element.name
            locator_info = f"{step.element.locator_strategy.name}={step.element.locator_value}"
        else:
            element_name = "页面"
            locator_info = "无"

        # 根据操作类型生成具体日志
        if step.action_type == 'click':
            log_parts.append(f"点击元素 '{element_name}'")
            log_parts.append(f"- 使用定位器: {locator_info}")
            if step_result == 'success':
                log_parts.append(f"- 元素点击成功 - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 元素点击失败 - 元素未找到或不可点击")

        elif step.action_type == 'fill':
            log_parts.append(f"在元素 '{element_name}' 中输入文本")
            log_parts.append(f"- 使用定位器: {locator_info}")
            log_parts.append(f"- 输入值: '{step.input_value}'")
            if step_result == 'success':
                log_parts.append(f"- 文本输入成功 - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 文本输入失败 - 元素未找到或不可编辑")

        elif step.action_type == 'getText':
            log_parts.append(f"获取元素 '{element_name}' 的文本内容")
            log_parts.append(f"- 使用定位器: {locator_info}")
            if step_result == 'success':
                # 模拟获取到的文本
                mock_text = f"示例文本内容_{step.id}" if step.id else "示例文本内容"
                log_parts.append(f"- 获取到文本: '{mock_text}' - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 获取文本失败 - 元素未找到")

        elif step.action_type == 'waitFor':
            log_parts.append(f"等待元素 '{element_name}' 出现")
            log_parts.append(f"- 使用定位器: {locator_info}")
            log_parts.append(f"- 超时时间: {step.wait_time/1000}秒")
            if step_result == 'success':
                log_parts.append(f"- 元素在 {execution_time}s 后出现")
            else:
                log_parts.append(f"- 等待超时 - 元素未在指定时间内出现")

        elif step.action_type == 'hover':
            log_parts.append(f"在元素 '{element_name}' 上悬停")
            log_parts.append(f"- 使用定位器: {locator_info}")
            if step_result == 'success':
                log_parts.append(f"- 悬停操作成功 - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 悬停操作失败 - 元素未找到")

        elif step.action_type == 'scroll':
            log_parts.append(f"滚动到元素 '{element_name}'")
            log_parts.append(f"- 使用定位器: {locator_info}")
            if step_result == 'success':
                log_parts.append(f"- 滚动操作成功 - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 滚动操作失败 - 元素未找到")

        elif step.action_type == 'screenshot':
            log_parts.append(f"执行截图操作")
            if step.element:
                log_parts.append(f"- 截图范围: 元素 '{element_name}'")
            else:
                log_parts.append(f"- 截图范围: 整个页面")
            if step_result == 'success':
                screenshot_name = f"screenshot_{int(time.time())}.png"
                log_parts.append(f"- 截图保存成功: {screenshot_name} - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 截图保存失败")

        elif step.action_type == 'assert':
            log_parts.append(f"执行断言验证")
            log_parts.append(f"- 断言类型: {step.assert_type}")
            if step.assert_value:
                log_parts.append(f"- 期望值: '{step.assert_value}'")
            if step_result == 'success':
                log_parts.append(f"- 断言通过 - 耗时 {execution_time}s")
            else:
                log_parts.append(f"- 断言失败 - 实际值与期望值不匹配")

        elif step.action_type == 'wait':
            log_parts.append(f"固定等待")
            log_parts.append(f"- 等待时间: {step.wait_time/1000}秒")
            log_parts.append(f"- 等待完成")

        else:
            # 默认处理其他操作类型
            log_parts.append(f"执行操作: {step.action_type}")
            if step.element:
                log_parts.append(f"- 目标元素: {element_name}")
            if step.input_value:
                log_parts.append(f"- 输入值: {step.input_value}")
            log_parts.append(f"- 操作{'成功' if step_result == 'success' else '失败'} - 耗时 {execution_time}s")

        # 如果步骤有描述，添加到日志中
        if step.description:
            log_parts.insert(0, f"说明: {step.description}")

        return '\n'.join(log_parts)

    def _generate_failure_screenshot(self, step_number, step_description):
        """生成失败截图的模拟数据（base64格式）"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import io
            import base64

            # 创建一个模拟的失败截图
            # 实际应用中，这里应该是通过Playwright/Selenium捕获真实的页面截图
            width, height = 1280, 720
            img = Image.new('RGB', (width, height), color=(240, 240, 245))
            draw = ImageDraw.Draw(img)

            # 绘制标题区域
            draw.rectangle([0, 0, width, 80], fill=(220, 53, 69))

            # 添加文本信息（使用默认字体）
            try:
                # 尝试使用系统字体
                font_title = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 40)
                font_text = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 24)
            except:
                # 如果系统字体不可用，使用默认字体
                font_title = ImageFont.load_default()
                font_text = ImageFont.load_default()

            # 标题
            draw.text((40, 20), "测试步骤执行失败", fill=(255, 255, 255), font=font_title)

            # 失败信息
            info_y = 120
            draw.text((40, info_y), f"失败步骤: 步骤 {step_number}", fill=(50, 50, 50), font=font_text)
            draw.text((40, info_y + 40), f"步骤说明: {step_description}", fill=(50, 50, 50), font=font_text)
            draw.text((40, info_y + 80), f"失败时间: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                     fill=(50, 50, 50), font=font_text)

            # 绘制一个模拟的浏览器窗口
            browser_y = info_y + 140
            draw.rectangle([40, browser_y, width-40, height-40], outline=(200, 200, 200), width=2)
            draw.rectangle([40, browser_y, width-40, browser_y + 40], fill=(200, 200, 200))
            draw.text((60, browser_y + 10), "模拟浏览器页面 - 失败截图", fill=(80, 80, 80), font=font_text)

            # 在浏览器窗口中绘制错误提示
            error_y = browser_y + 80
            draw.text((60, error_y), "× 元素定位失败或操作执行异常", fill=(220, 53, 69), font=font_text)
            draw.text((60, error_y + 40), "× 请检查元素定位器是否正确", fill=(220, 53, 69), font=font_text)
            draw.text((60, error_y + 80), "× 或页面加载是否完成", fill=(220, 53, 69), font=font_text)

            # 转换为base64
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            img_base64 = base64.b64encode(buffer.getvalue()).decode()

            return f"data:image/png;base64,{img_base64}"

        except Exception as e:
            logger.error(f"生成失败截图时出错: {str(e)}")
            # 返回一个简单的错误占位符
            return None

    @action(detail=False, methods=['post'], url_path='convert-from-testcases')
    def convert_from_testcases(self, request):
        """将用例库用例转换为 UI 自动化结构化用例。
        mode='web'(默认): 生成 Web/Playwright 用例（通用 click/fill/assert）
        mode='appium':     生成 Appium App 用例（含 launch_app、swipe 等）
        """
        from apps.testcases.models import TestCase as LibTestCase

        case_ids = request.data.get('case_ids', [])
        ui_project_id = request.data.get('ui_project_id')
        mode = request.data.get('mode', 'web')  # 'web' | 'appium'

        if not case_ids:
            return Response({'success': False, 'message': '请选择要转换的用例'}, status=status.HTTP_400_BAD_REQUEST)
        if not ui_project_id:
            return Response({'success': False, 'message': '请选择目标 UI 自动化项目'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            ui_project = UiProject.objects.get(id=ui_project_id)
        except UiProject.DoesNotExist:
            return Response({'success': False, 'message': '目标 UI 自动化项目不存在'}, status=status.HTTP_400_BAD_REQUEST)

        default_strategy = LocatorStrategy.objects.filter(name='xpath').first() or LocatorStrategy.objects.first()
        if not default_strategy:
            return Response(
                {'success': False, 'message': '定位策略未初始化，请先在「UI自动化测试→元素管理」初始化定位策略'},
                status=status.HTTP_400_BAD_REQUEST
            )

        priority_map = {'critical': 'high', 'high': 'high', 'medium': 'medium', 'low': 'low'}

        created = []
        for cid in case_ids:
            try:
                src = LibTestCase.objects.get(id=cid)
            except LibTestCase.DoesNotExist:
                continue

            # 同项目同名用例自动加序号后缀，避免被跳过
            base_name = src.title
            name_suffix = ''
            existing_names = set(TestCase.objects.filter(project=ui_project).values_list('name', flat=True))
            if base_name in existing_names:
                for i in range(1, 100):
                    candidate = f"{base_name}({i})"
                    if candidate not in existing_names:
                        name_suffix = f"({i})"
                        break

            desc_parts = []
            if src.preconditions:
                desc_parts.append(f"前置条件：{src.preconditions}")
            if src.expected_result:
                desc_parts.append(f"预期结果：{src.expected_result}")
            if src.description:
                desc_parts.append(src.description)

            ui_case = TestCase.objects.create(
                name=base_name + name_suffix,
                description="\n".join(desc_parts),
                project=ui_project,
                priority=priority_map.get(src.priority, 'medium'),
                status='draft',
                created_by=request.user
            )

            # 收集步骤：优先用结构化 step_details，否则解析 steps 文本
            step_objs = list(src.step_details.all().order_by('step_number'))
            if not step_objs and src.steps:
                step_objs = [
                    type('S', (), {'action': line.strip(), 'expected': '', 'step_number': i + 1})()
                    for i, line in enumerate(l for l in src.steps.splitlines() if l.strip())
                ]

            step_num = 1

            # === Appium 模式：插入启动 App 步骤 ===
            if mode == 'appium':
                TestCaseStep.objects.create(
                    test_case=ui_case, step_number=step_num,
                    action_type='launch_app',
                    description='启动应用',
                    wait_time=2000,
                )
                step_num += 1

            # 预创建高频元素（仅 appium 模式）
            def get_sure_btn():
                return Element.objects.get_or_create(
                    project=ui_project,
                    name='确认按钮',
                    defaults={
                        'locator_strategy': default_strategy,
                        'locator_value': 'com.youloft.calendar:id/dc_sureBtn',
                        'element_type': 'BUTTON',
                    }
                )[0]

            for s in step_objs:
                action_text = getattr(s, 'action', '') or ''
                expected_text = getattr(s, 'expected', '') or ''

                # === Appium 模式：滑动模式解析 ===
                if mode == 'appium':
                    swipe_pattern = parse_swipe_pattern(action_text)
                    if swipe_pattern:
                        wheel_type, times = swipe_pattern
                        coords = get_wheel_coords().get(wheel_type, (585, 2228, 585, 2078))
                        swipe_input = f"{coords[0]},{coords[1]},{coords[2]},{coords[3]}"
                        wheel_label = {"month":"月","year":"年","day":"日"}.get(wheel_type, "月")
                        for i in range(times):
                            TestCaseStep.objects.create(
                                test_case=ui_case, step_number=step_num,
                                action_type='swipe',
                                description=f'在{wheel_label}滚轮上滑动1个{wheel_label}({i+1}/{times})',
                                input_value=swipe_input,
                                wait_time=500,
                            )
                            step_num += 1
                        sure_btn = get_sure_btn()
                        TestCaseStep.objects.create(
                            test_case=ui_case, step_number=step_num,
                            action_type='click', element=sure_btn,
                            description='点击确认按钮，关闭选择器',
                        )
                        step_num += 1
                        continue

                # 通用步骤生成（Web 和 Appium 共用）
                atype = infer_appium_action_type(action_text) if mode == 'appium' else _infer_web_action_type(action_text)

                matched_strategy, matched_value = smart_match_locator(action_text)
                elem = Element.objects.create(
                    project=ui_project,
                    name=(action_text[:50] or f'步骤{step_num}')[:50],
                    locator_strategy=LocatorStrategy.objects.filter(name=matched_strategy.upper()).first() if matched_strategy else default_strategy,
                    locator_value=matched_value if matched_value else ('TODO_待补充元素定位器' if mode == 'web' else 'TODO_待补充控件定位器'),
                    element_type=('INPUT' if atype in ('fill', 'input') else 'TEXT' if atype == 'assert' else 'BUTTON'),
                    description=action_text
                )
                TestCaseStep.objects.create(
                    test_case=ui_case,
                    step_number=step_num,
                    action_type=atype,
                    element=elem,
                    input_value=extract_step_input_value(action_text),
                    wait_time=1000,
                    assert_type='exists' if atype == 'assert' else '',
                    assert_value=expected_text if atype == 'assert' else '',
                    description=action_text
                )
                step_num += 1

            # 用例级预期结果追加为断言步骤
            if src.expected_result:
                matched_strategy2, matched_value2 = smart_match_locator(src.expected_result or src.title)
                elem = Element.objects.create(
                    project=ui_project,
                    name=f'断言_{src.title[:30]}'[:50],
                    locator_strategy=LocatorStrategy.objects.filter(name=matched_strategy2.upper()).first() if matched_strategy2 else default_strategy,
                    locator_value=matched_value2 if matched_value2 else ('TODO_待补充元素定位器' if mode == 'web' else 'TODO_待补充控件定位器'),
                    element_type='TEXT',
                    description='用例预期结果断言'
                )
                TestCaseStep.objects.create(
                    test_case=ui_case,
                    step_number=step_num,
                    action_type='assert',
                    element=elem,
                    assert_type='textContains',
                    assert_value=src.expected_result[:200],
                    description=f'断言预期结果：{src.expected_result[:100]}'
                )

            log_operation('create', 'test_case', ui_case.id, ui_case.name, request.user)
            created.append({'id': ui_case.id, 'name': ui_case.name})

        return Response({
            'success': True,
            'count': len(created),
            'created': created,
            'message': f'成功转换 {len(created)} 条用例到项目「{ui_project.name}」。请到「UI自动化测试→用例管理」为占位元素回填控件定位器，再选 Appium 引擎执行。'
        })

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """运行单个测试用例 - 支持选择Playwright或Selenium执行引擎"""
        test_case = self.get_object()

        try:
            # 获取执行引擎选择，默认使用playwright
            engine_type = request.data.get('engine', 'playwright')

            # 创建执行记录
            execution = TestCaseExecution.objects.create(
                test_case=test_case,
                project=test_case.project,
                execution_source='manual',
                status='running',
                engine=engine_type,
                browser=request.data.get('browser', 'chrome'),
                headless=request.data.get('headless', False),
                created_by=request.user,
                started_at=timezone.now()
            )

            # ========== 所有引擎共用的步骤数据（提前构建，避免各分支重复） ==========
            test_steps = list(test_case.steps.all().order_by('step_number'))
            steps_data = []
            for step in test_steps:
                step_data = {
                    'step': step,
                    # 各引擎（含 Midscene Web 步骤转换）都可能用到，缺了会 KeyError
                    'step_number': step.step_number,
                    'action_type': step.action_type,
                    'description': step.description,
                    'input_value': step.input_value,
                    'wait_time': step.wait_time,
                    'assert_type': step.assert_type,
                    'assert_value': step.assert_value,
                    'center_x': step.center_x,
                    'center_y': step.center_y,
                }
                if step.element:
                    step_data['element_data'] = {
                        'locator_strategy': step.element.locator_strategy.name if step.element.locator_strategy else ('css' if engine_type != 'appium' else 'xpath'),
                        'locator_value': step.element.locator_value,
                        'name': step.element.name,
                        'element_id': step.element.id,
                        'wait_timeout': getattr(step.element, 'wait_timeout', None),
                        'force_action': getattr(step.element, 'force_action', False),
                    }
                else:
                    step_data['element_data'] = None
                steps_data.append(step_data)

            # 共享结果容器（Appium / Playwright / Selenium 各分支共用）
            step_results = []
            execution_logs = []
            screenshots = []
            detailed_errors = []
            execution_result = {'status': 'passed', 'error_message': None}

            # 根据引擎类型导入对应的执行引擎
            if engine_type == 'selenium':
                from .selenium_engine import SeleniumTestEngine

                # Selenium 引擎需要预先检查浏览器是否可用
                browser_type = request.data.get('browser', 'chrome')
                is_available, error_msg = SeleniumTestEngine.check_browser_available(browser_type)
                if not is_available:
                    # 浏览器不可用，立即返回错误
                    logger.error(f"Selenium 浏览器检查失败: {error_msg}")
                    execution.status = 'failed'
                    execution.error_message = error_msg
                    execution.execution_logs = f"浏览器检查失败\n\n{error_msg}\n\n建议：\n1. 请确认已安装 {browser_type.capitalize()} 浏览器\n2. 或者尝试使用其他浏览器（Chrome、Firefox、Edge）\n3. 或者使用 Playwright 引擎（支持自动下载浏览器）"
                    execution.finished_at = timezone.now()
                    execution.save()

                    return Response({
                        'success': False,
                        'logs': execution.execution_logs,
                        'screenshots': [],
                        'execution_time': 0,
                        'errors': [{
                            'message': f'{browser_type.capitalize()} 浏览器不可用',
                            'details': error_msg,
                            'step_number': None,
                            'action_type': '浏览器检查',
                            'element': '',
                            'description': '执行前浏览器环境检查'
                        }]
                    }, status=status.HTTP_400_BAD_REQUEST)
            elif engine_type == 'appium':
                # Appium 引擎：使用 Appium 执行 App 自动化测试
                from .appium_engine import AppiumTestEngine
                import threading as thread_mod

                device_id = request.data.get('device_id')
                app_config_id = request.data.get('app_config_id')

                if not device_id or not app_config_id:
                    execution.status = 'failed'
                    execution.error_message = '使用 Appium 引擎时必须选择设备和应用配置'
                    execution.execution_logs = '使用 Appium 引擎时必须选择设备和应用配置'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({
                        'success': False,
                        'logs': execution.execution_logs,
                        'screenshots': [],
                        'execution_time': 0,
                        'errors': [{
                            'message': '缺少必要参数',
                            'details': '使用 Appium 引擎时必须选择设备和应用配置',
                            'step_number': None,
                            'action_type': '参数检查',
                            'element': '',
                            'description': '执行前参数检查'
                        }]
                    }, status=status.HTTP_400_BAD_REQUEST)

                # 加载设备和应用配置（统一使用 app_automation 设备表）
                try:
                    from apps.app_automation.models import AppDevice as AutoAppDevice
                    _raw_device = AutoAppDevice.objects.get(device_id=device_id)
                    app_config = AppConfig.objects.get(id=app_config_id)
                    # 字段映射：app_automation.AppDevice → 执行引擎期望的接口
                    class _DeviceAdapter:
                        def __init__(self, d):
                            self._d = d
                        @property
                        def udid(self): return self._d.device_id
                        @property
                        def platform(self): return 'android'
                        @property
                        def appium_server_url(self): return ''
                        @property
                        def capabilities(self): return {}
                        def __getattr__(self, name): return getattr(self._d, name)
                    device = _DeviceAdapter(_raw_device)
                except Exception:
                    execution.status = 'failed'
                    execution.error_message = f'设备不存在 (device_id: {device_id})'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({'success': False, 'logs': execution.error_message}, status=status.HTTP_400_BAD_REQUEST)
                except AppConfig.DoesNotExist:
                    execution.status = 'failed'
                    execution.error_message = f'应用配置不存在 (ID: {app_config_id})'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({'success': False, 'logs': execution.error_message}, status=status.HTTP_400_BAD_REQUEST)

                # 异步执行 Appium 测试
                def run_appium_test():
                    aborted = False
                    try:
                        engine_config = {
                            'appium_server_url': get_appium_server_url(device),
                            'platform': device.platform,
                            'device_udid': device.udid,
                            'app_package': app_config.package_name,
                            'app_activity': app_config.app_activity,
                            'bundle_id': app_config.package_name if device.platform == 'ios' else None,
                            'app_path': app_config.app_path or None,
                        }
                        if device.capabilities:
                            engine_config.update(device.capabilities)

                        app_engine = AppiumTestEngine(**engine_config)
                        # 连接前清理设备端残留的 UiAutomator2 进程
                        if device.udid:
                            import subprocess as _sp
                            try:
                                _sp.run(
                                    ['adb', '-s', device.udid, 'shell', 'am', 'force-stop',
                                     'io.appium.uiautomator2.server'],
                                    capture_output=True, timeout=5,
                                )
                                logger.info(f"执行前已清理设备 {device.udid} 上的残留 UiAutomator2 进程")
                            except Exception:
                                pass
                        app_engine.connect()

                        execution_logs.append(f"========== Appium 引擎初始化 ==========")
                        execution_logs.append(f"✓ Appium 连接成功: {device.platform} 设备 {device.name}")
                        execution_logs.append(f"  应用: {app_config.name}")
                        execution_logs.append(f"  Server: {engine_config['appium_server_url']}")
                        execution_logs.append("")

                        if steps_data:
                            execution_logs.append("========== 执行测试步骤 ==========")
                            execution_logs.append(f"共有 {len(steps_data)} 个步骤需要执行")
                            execution_logs.append("")

                            for i, step_info in enumerate(steps_data, 1):
                                # 检查点①：步骤开始前。用户可能在上一步执行期间点了「停止执行」，
                                # 这时不应再启动新步骤（否则要多等一整步才生效）。
                                if _abort_requested(execution.id):
                                    aborted = True
                                    execution_logs.append(
                                        f"[中止] 收到终止信号，步骤 {i}-{len(steps_data)} 未执行")
                                    break
                                execution_logs.append(f"--- 步骤 {i}/{len(steps_data)} ---")
                                execution_logs.append(f"操作: {step_info['action_type']} - {step_info['description']}")

                                # element 字段兼容：录制存储可能是 'element' 或 'element_data'
                                el = step_info.get('element_data') or step_info.get('element')
                                # 自动捕获步骤的坐标可能在顶层
                                if el is None or (not el.get('center_x') and not el.get('center_y')):
                                    cx = step_info.get('center_x')
                                    cy = step_info.get('center_y')
                                    if cx is not None and cy is not None:
                                        el = {'center_x': cx, 'center_y': cy,
                                              'locator_strategy': 'coordinate',
                                              'value': f'{cx},{cy}',
                                              'name': f'坐标({cx},{cy})'}
                                    elif el is None:
                                        el = {}

                                step_data = {
                                    'step_number': i,
                                    'action_type': step_info['action_type'],
                                    'description': step_info['description'],
                                    'input_value': step_info.get('input_value', ''),
                                    'wait_time': step_info.get('wait_time', 0),
                                    'assert_type': step_info.get('assert_type', ''),
                                    'assert_value': step_info.get('assert_value', ''),
                                    'element': el
                                }

                                result = app_engine.execute_step(step_data)
                                step_result_entry = {
                                    'step_number': i,
                                    'action_type': step_info['action_type'],
                                    'description': step_info['description'],
                                    'success': result.get('success', False),
                                    'error': result.get('error', ''),
                                    'screenshot': result.get('screenshot', None)
                                }
                                step_results.append(step_result_entry)

                                if result.get('success'):
                                    execution_logs.append(f"  ✓ 步骤 {i} 执行成功")
                                else:
                                    execution_logs.append(f"  ✗ 步骤 {i} 执行失败: {result.get('error', '')}")
                                    if result.get('screenshot'):
                                        screenshots.append(result['screenshot'])
                                    execution_result['status'] = 'failed'
                                    detailed_errors.append({
                                        'step_number': i,
                                        'action_type': step_info['action_type'],
                                        'element': step_info['element_data']['name'] if step_info.get('element_data') else '',
                                        'message': f'步骤 {i} 执行失败',
                                        'details': result.get('error', ''),
                                        'description': step_info['description']
                                    })
                                    break

                                execution_logs.append("")

                                # 检查点②：步骤结束后。
                                # 本步已成功但用户已点停止 → 不再执行下一步。
                                if _abort_requested(execution.id):
                                    aborted = True
                                    execution_logs.append(
                                        f"[中止] 收到终止信号，停止执行剩余 {len(steps_data) - i} 个步骤")
                                    break

                        app_engine.disconnect()
                        execution_logs.append("========== 测试执行完成 ==========")

                        # 更新执行记录
                        if aborted:
                            execution_result['status'] = 'failed'
                            execution_result['error_message'] = '用户中止执行'
                            execution_logs.append(
                                f"[中止] 已执行 {len(step_results)}/{len(steps_data)} 个步骤，"
                                '已完成步骤的结果保留')
                        execution.execution_logs = '\n'.join(execution_logs)
                        execution.screenshots = screenshots
                        execution.step_results = step_results
                        execution.status = execution_result['status']
                        execution.error_message = execution_result.get('error_message', '')
                        execution.finished_at = timezone.now()
                        execution.save()

                    except Exception as e:
                        error_msg = f"Appium 执行异常: {str(e)}"
                        execution_logs.append(f"✗ {error_msg}")
                        execution.execution_logs = '\n'.join(execution_logs)
                        execution.status = 'failed'
                        execution.error_message = error_msg
                        execution.finished_at = timezone.now()
                        execution.save()

                # 启动后台线程
                thread = thread_mod.Thread(target=run_appium_test)
                thread.daemon = True
                thread.start()

                return Response({
                    'success': True,
                    'message': 'App 自动化测试已启动',
                    'execution_id': execution.id,
                    'engine': 'appium',
                    'device': device.name,
                    'app_config': app_config.name
                })
            elif engine_type == 'airtest':
                # Airtest 引擎：使用 Airtest 直接连接设备执行 App 自动化测试
                from .airtest_engine import AirtestRecordingEngine
                import threading as _thread_mod

                device_id = request.data.get('device_id')
                app_config_id = request.data.get('app_config_id')

                if not device_id or not app_config_id:
                    execution.status = 'failed'
                    execution.error_message = '使用 Airtest 引擎时必须选择设备和应用配置'
                    execution.execution_logs = '使用 Airtest 引擎时必须选择设备和应用配置'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({
                        'success': False,
                        'logs': execution.execution_logs,
                        'screenshots': [],
                        'execution_time': 0,
                        'errors': [{
                            'message': '缺少必要参数',
                            'details': '使用 Airtest 引擎时必须选择设备和应用配置',
                            'step_number': None,
                            'action_type': '参数检查',
                            'element': '',
                            'description': '执行前参数检查'
                        }]
                    }, status=status.HTTP_400_BAD_REQUEST)

                try:
                    from apps.app_automation.models import AppDevice as AutoAppDevice
                    _raw_device = AutoAppDevice.objects.get(device_id=device_id)
                    app_config = AppConfig.objects.get(id=app_config_id)
                    # 字段映射：app_automation.AppDevice → 执行引擎期望的接口
                    class _DeviceAdapter:
                        def __init__(self, d):
                            self._d = d
                        @property
                        def udid(self): return self._d.device_id
                        @property
                        def platform(self): return 'android'
                        @property
                        def appium_server_url(self): return ''
                        @property
                        def capabilities(self): return {}
                        def __getattr__(self, name): return getattr(self._d, name)
                    device = _DeviceAdapter(_raw_device)
                except Exception:
                    execution.status = 'failed'
                    execution.error_message = f'设备不存在 (device_id: {device_id})'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({'success': False, 'logs': execution.error_message}, status=status.HTTP_400_BAD_REQUEST)
                except AppConfig.DoesNotExist:
                    execution.status = 'failed'
                    execution.error_message = f'应用配置不存在 (ID: {app_config_id})'
                    execution.finished_at = timezone.now()
                    execution.save()
                    return Response({'success': False, 'logs': execution.error_message}, status=status.HTTP_400_BAD_REQUEST)

                def run_airtest_test():
                    aborted = False
                    try:
                        _adb_host = getattr(device, 'adb_host', None) or None
                        at_engine = AirtestRecordingEngine(
                            serial=device.udid,
                            platform=device.platform,
                            app_package=app_config.package_name,
                            wda_url=get_appium_server_url(device),
                            adb_host=_adb_host,
                        )
                        at_engine.connect()

                        # 启动被测应用
                        try:
                            at_engine.activate_app(app_config.package_name)
                        except Exception:
                            pass
                        time.sleep(1)

                        execution_logs.append("========== Airtest 引擎初始化 ==========")
                        execution_logs.append(f"✓ Airtest 连接成功: {device.platform} 设备 {device.name}")
                        execution_logs.append(f"  应用: {app_config.name}")
                        execution_logs.append(f"  设备UDID: {device.udid}")
                        execution_logs.append("")

                        if steps_data:
                            execution_logs.append("========== 执行测试步骤 ==========")
                            execution_logs.append(f"共有 {len(steps_data)} 个步骤需要执行")
                            execution_logs.append("")

                            for i, step_info in enumerate(steps_data, 1):
                                # 检查点①：步骤开始前（同 Appium 分支）
                                if _abort_requested(execution.id):
                                    aborted = True
                                    execution_logs.append(
                                        f"[中止] 收到终止信号，步骤 {i}-{len(steps_data)} 未执行")
                                    break
                                execution_logs.append(f"--- 步骤 {i}/{len(steps_data)} ---")
                                execution_logs.append(f"操作: {step_info['action_type']} - {step_info['description']}")

                                # element 字段兼容：录制存储可能是 'element' 或 'element_data'
                                el = step_info.get('element_data') or step_info.get('element')
                                # 自动捕获步骤的坐标可能在顶层
                                if el is None or (not el.get('center_x') and not el.get('center_y')):
                                    cx = step_info.get('center_x')
                                    cy = step_info.get('center_y')
                                    if cx is not None and cy is not None:
                                        el = {'center_x': cx, 'center_y': cy,
                                              'locator_strategy': 'coordinate',
                                              'value': f'{cx},{cy}',
                                              'name': f'坐标({cx},{cy})'}
                                    elif el is None:
                                        el = {}

                                step_data = {
                                    'step_number': i,
                                    'action_type': step_info['action_type'],
                                    'description': step_info['description'],
                                    'input_value': step_info.get('input_value', ''),
                                    'wait_time': step_info.get('wait_time', 0),
                                    'assert_type': step_info.get('assert_type', ''),
                                    'assert_value': step_info.get('assert_value', ''),
                                    'element': el
                                }

                                result = at_engine.execute_step(step_data)
                                step_result_entry = {
                                    'step_number': i,
                                    'action_type': step_info['action_type'],
                                    'description': step_info['description'],
                                    'success': result.get('success', False),
                                    'error': result.get('error', ''),
                                    'screenshot': result.get('screenshot', None)
                                }
                                step_results.append(step_result_entry)

                                if result.get('success'):
                                    execution_logs.append(f"  ✓ 步骤 {i} 执行成功")
                                else:
                                    execution_logs.append(f"  ✗ 步骤 {i} 执行失败: {result.get('error', '')}")
                                    if result.get('screenshot'):
                                        screenshots.append(result['screenshot'])
                                    execution_result['status'] = 'failed'
                                    detailed_errors.append({
                                        'step_number': i,
                                        'action_type': step_info['action_type'],
                                        'element': step_info.get('element_data', {}).get('name', '') if step_info.get('element_data') else '',
                                        'message': f'步骤 {i} 执行失败',
                                        'details': result.get('error', ''),
                                        'description': step_info['description']
                                    })
                                    break

                                execution_logs.append("")

                                # 检查点②：步骤结束后（同 Appium 分支）
                                if _abort_requested(execution.id):
                                    aborted = True
                                    execution_logs.append(
                                        f"[中止] 收到终止信号，停止执行剩余 {len(steps_data) - i} 个步骤")
                                    break

                        at_engine.disconnect()
                        execution_logs.append("========== 测试执行完成 ==========")

                        # 更新执行记录
                        if aborted:
                            execution_result['status'] = 'failed'
                            execution_result['error_message'] = '用户中止执行'
                            execution_logs.append(
                                f"[中止] 已执行 {len(step_results)}/{len(steps_data)} 个步骤，"
                                '已完成步骤的结果保留')
                        execution.execution_logs = '\n'.join(execution_logs)
                        execution.screenshots = screenshots
                        execution.step_results = step_results
                        execution.status = execution_result['status']
                        execution.error_message = execution_result.get('error_message', '')
                        execution.finished_at = timezone.now()
                        execution.save()

                    except Exception as e:
                        error_msg = f"Airtest 执行异常: {str(e)}"
                        execution_logs.append(f"✗ {error_msg}")
                        execution.execution_logs = '\n'.join(execution_logs)
                        execution.status = 'failed'
                        execution.error_message = error_msg
                        execution.finished_at = timezone.now()
                        execution.save()

                _thread_mod.Thread(target=run_airtest_test, daemon=True).start()

                return Response({
                    'success': True,
                    'message': 'Airtest 自动化测试已启动',
                    'execution_id': execution.id,
                    'engine': 'airtest',
                    'device': device.name,
                    'app_config': app_config.name
                })
            elif engine_type == 'midscene_web':
                # Midscene Web 引擎：使用 AI 语义定位执行测试
                import threading
                from .midscene_web_engine import MidsceneWebEngine
                # 本分支自己统计耗时（通用流程的 start_time 在后面才设置，会算成 ~0）
                midscene_start_time = time.time()

                # 获取 Midscene 模型配置（真源：AIModelConfig role=midscene_vision）
                try:
                    from .midscene_web_engine import load_midscene_model_config
                    model_config = load_midscene_model_config()
                except Exception as e:
                    model_config = {}
                    execution_logs.append(f"✗ 读取 Midscene 模型配置失败: {e}")
                if not model_config.get('api_key'):
                    execution_logs.append(
                        "✗ 未配置 Midscene 视觉模型：请在「AI 模型配置」中新增 role=midscene_vision "
                        "且已启用的多模态模型（需含 api_key / base_url / model_name）。")

                execution_logs.append(f"测试用例 '{test_case.name}' 开始执行 (Midscene Web)")
                execution_logs.append(f"执行时间: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}")
                execution_logs.append(f"执行引擎: MIDSCENE_WEB")
                execution_logs.append(f"执行用户: {request.user.username}")
                execution_logs.append(f"项目基础URL: {test_case.project.base_url}")
                execution_logs.append("")

                def run_midscene_web_test():
                    """使用 Midscene Web 引擎执行测试"""
                    try:
                        # 创建 Midscene Web 引擎实例
                        engine = MidsceneWebEngine(
                            base_url=test_case.project.base_url or '',
                            model_config=model_config,
                            headless=request.data.get('headless', True)
                        )

                        # 转换步骤为 Midscene 格式
                        from .test_executor import TestExecutor
                        midscene_steps = TestExecutor._convert_to_midscene_steps(steps_data)

                        # 如果项目有基础 URL 且第一步不是导航，自动添加导航步骤
                        base_url = test_case.project.base_url or ''
                        if base_url and (not midscene_steps or midscene_steps[0].get('action') != 'navigate'):
                            midscene_steps.insert(0, {
                                'action': 'navigate',
                                'prompt': base_url,
                                'name': '导航到项目基础URL',
                            })

                        execution_logs.append(f"共 {len(midscene_steps)} 个步骤待执行")
                        execution_logs.append("")

                        # 执行步骤
                        result = engine.execute_steps(midscene_steps)

                        # 记录结果
                        execution_logs.extend(engine.logs)
                        execution_logs.append("")
                        execution_logs.append(f"执行结束: {result.get('status', 'unknown')}")

                        if result['status'] == 'success':
                            execution_result['status'] = 'passed'
                            execution_logs.append("✓ 测试通过")
                        else:
                            execution_result['status'] = 'failed'
                            execution_result['error_message'] = result.get('error', '执行失败')
                            execution_logs.append(f"✗ 测试失败: {result.get('error', '未知错误')}")

                        # 收集步骤结果和截图
                        for step_res in result['steps']:
                            step_info = {
                                'step_number': step_res.get('index', 0) + 1,
                                'action_type': step_res.get('action', ''),
                                'description': step_res.get('name', ''),
                                'success': step_res.get('status') == 'success',
                                'log': f"{step_res.get('action', '')}: {step_res.get('name', '')}",
                            }
                            step_results.append(step_info)

                            # 截图统一成 {url, description, step_number, timestamp}，
                            # 与 Playwright 引擎一致，前端按 :src="screenshot.url" 渲染
                            for key, prefix in (('screenshot', '步骤'), ('fail_screenshot', '步骤失败')):
                                shot = step_res.get(key)
                                if not shot:
                                    continue
                                screenshots.append({
                                    'url': shot if str(shot).startswith('data:') else f'data:image/png;base64,{shot}',
                                    'description': f'{prefix} {step_info["step_number"]}: {step_info["description"]}',
                                    'step_number': step_info['step_number'],
                                    'timestamp': timezone.now().isoformat(),
                                })

                            if not step_info['success']:
                                detailed_errors.append({
                                    'step_number': step_info['step_number'],
                                    'action_type': step_info['action_type'],
                                    'element': '',
                                    'message': step_res.get('error', '步骤执行失败'),
                                    'details': step_res.get('error', ''),
                                    'description': step_info['description']
                                })

                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        execution_result['status'] = 'failed'
                        execution_result['error_message'] = str(e)
                        execution_logs.append(f"✗ 执行异常: {str(e)}")
                        detailed_errors.append({
                            'step_number': None,
                            'action_type': '执行异常',
                            'element': '',
                            'message': str(e),
                            'details': str(e),
                            'description': 'Midscene Web 执行异常'
                        })

                # 启动后台线程执行
                # 注意：首次运行需要下载 Node runtime + npm install @midscene/web（实测约 4 分钟），
                # 5 分钟的上限会在冷启动时被击穿，这里放宽到 15 分钟。
                thread = threading.Thread(target=run_midscene_web_test)
                thread.daemon = True
                thread.start()
                thread.join(timeout=900)

                # 收尾并直接返回：不能再往下走 Playwright/Selenium 的通用流程，
                # 否则日志头会重复打印、总耗时被重算成 ~0。
                total_time = round(time.time() - midscene_start_time, 2)
                execution_logs.append("")
                execution_logs.append(f"- 总执行时间: {total_time}秒")
                if screenshots:
                    execution_logs.append(f"- 截图数量: {len(screenshots)} 张")

                execution.status = execution_result['status']
                execution.error_message = execution_result['error_message'] or ''
                execution.execution_logs = json.dumps(step_results, ensure_ascii=False)
                execution.step_results = step_results
                execution.execution_time = total_time
                execution.finished_at = timezone.now()
                execution.screenshots = screenshots
                execution.save()

                log_operation('run', 'test_case', test_case.id, test_case.name, request.user)

                errors = [{
                    'message': e['message'],
                    'details': e['details'],
                    'step_number': e['step_number'],
                    'action_type': e['action_type'],
                    'element': e['element'],
                    'description': e['description'],
                } for e in detailed_errors]
                if not errors and execution.error_message:
                    errors.append({'message': execution.error_message, 'details': ''})

                return Response({
                    'success': execution.status == 'passed',
                    'logs': execution.execution_logs,
                    'screenshots': screenshots,
                    'execution_time': execution.execution_time,
                    'errors': errors
                })

            else:
                import asyncio
                import threading
                from .playwright_engine import PlaywrightTestEngine

            start_time = time.time()

            # 执行日志头部（Playwright/Selenium 专用，Appium 在闭包内自己写）
            if engine_type != 'appium':
                execution_logs.append(f"测试用例 '{test_case.name}' 开始执行")
                execution_logs.append(f"执行时间: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}")
                execution_logs.append(f"执行引擎: {engine_type.upper()}")
                execution_logs.append(f"浏览器: {request.data.get('browser', 'chrome').capitalize()}")
                headless_mode = request.data.get('headless', False)
                mode_text = "无头模式" if headless_mode else "有头模式"
                execution_logs.append(f"执行模式: {mode_text}")
                execution_logs.append(f"执行用户: {request.user.username}")
                execution_logs.append(f"项目基础URL: {test_case.project.base_url}")
                execution_logs.append("")

            # 根据引擎类型选择执行方式
            if engine_type == 'selenium':
                # Selenium同步执行
                def run_test_selenium():
                    """使用Selenium执行测试"""
                    browser_type = request.data.get('browser', 'chrome')
                    headless = request.data.get('headless', False)

                    # 创建Selenium引擎实例
                    engine = SeleniumTestEngine(browser_type=browser_type, headless=headless)

                    try:
                        # 启动浏览器
                        execution_logs.append("========== 初始化浏览器 ==========")
                        try:
                            engine.start()
                            mode_text = "无头模式" if headless else "有头模式"
                            execution_logs.append(f"✓ {browser_type.capitalize()} 浏览器启动成功 (Selenium, {mode_text})")
                            execution_logs.append("")
                        except Exception as browser_error:
                            # 浏览器启动失败
                            execution_logs.append(f"✗ {browser_type.capitalize()} 浏览器启动失败")
                            execution_logs.append(f"  错误: {str(browser_error)}")
                            execution_logs.append("")
                            execution_result['status'] = 'failed'
                            execution_result['error_message'] = f"{browser_type.capitalize()} 浏览器启动失败: {str(browser_error)}"

                            # 添加详细错误信息
                            detailed_errors.append({
                                'step_number': None,
                                'action_type': '浏览器启动',
                                'element': '',
                                'message': f"{browser_type.capitalize()} 浏览器启动失败",
                                'details': str(browser_error),
                                'description': '执行前浏览器启动检查'
                            })

                            return False

                        # 导航到项目基础URL
                        if test_case.project.base_url:
                            execution_logs.append("========== 导航到测试页面 ==========")
                            success, nav_log = engine.navigate(test_case.project.base_url)
                            execution_logs.append(nav_log)
                            execution_logs.append("")

                            if not success:
                                execution_result['status'] = 'failed'
                                execution_result['error_message'] = "导航到测试页面失败"
                                return False

                        if steps_data:
                            execution_logs.append("========== 执行测试步骤 ==========")
                            step_count = len(steps_data)
                            execution_logs.append(f"共有 {step_count} 个步骤需要执行")
                            execution_logs.append("")

                            for i, step_info in enumerate(steps_data, 1):
                                execution_logs.append(f"========== 开始执行步骤 {i}/{step_count} ==========")
                                execution_logs.append(f"步骤 {i}/{step_count}:")

                                step = step_info['step']
                                action_type = step_info['action_type']
                                description = step_info['description']
                                element_data = step_info['element_data']

                                action_type_text = {
                                    'click': '点击',
                                    'tap': '轻触',
                                    'double_tap': '双击',
                                    'long_press': '长按',
                                    'fill': '输入文本',
                                    'input': '输入',
                                    'clear': '清空',
                                    'getText': '获取文本',
                                    'waitFor': '等待元素',
                                    'hover': '悬停',
                                    'scroll': '滚动',
                                    'swipe': '滑动',
                                    'screenshot': '截图',
                                    'assert': '断言',
                                    'wait': '等待',
                                    'switchTab': '切换标签页',
                                    'launch_app': '启动应用',
                                    'close_app': '关闭应用',
                                    'back': '返回',
                                    'home': '主页'
                                }.get(action_type, action_type)
                                execution_logs.append(f"  操作: {action_type_text}")

                                if description:
                                    execution_logs.append(f"  说明: {description}")

                                if element_data:
                                    execution_logs.append(f"  元素: {element_data['name']}")
                                    execution_logs.append(f"  定位器: {element_data['locator_strategy']}={element_data['locator_value']}")
                                else:
                                    execution_logs.append(f"  (此步骤不需要元素)")

                                try:
                                    success, step_log, screenshot_base64 = engine.execute_step(step, element_data or {})
                                    execution_logs.append(f"  {step_log}")
                                    execution_logs.append("")

                                    # 记录步骤执行结果（用于JSON格式）
                                    step_results.append({
                                        'step_number': i,
                                        'action_type': action_type,
                                        'description': description or '',
                                        'success': success,
                                        'error': None if success else step_log,
                                        'screenshot': f'data:image/png;base64,{screenshot_base64}' if screenshot_base64 else None
                                    })

                                    if not success:
                                        logger.info(f"[调试-Selenium] 步骤 {i} 执行失败，设置状态为 failed")
                                        execution_result['status'] = 'failed'
                                        element_info = element_data['name'] if element_data else "未知元素"
                                        execution_result['error_message'] = step_log  # 使用step_log作为错误信息
                                        logger.info(f"[调试-Selenium] execution_result = {execution_result}")

                                        detailed_errors.append({
                                            'step_number': i,
                                            'action_type': action_type_text,
                                            'element': element_info,
                                            'message': f"步骤 {i}/{step_count} 执行失败",
                                            'details': step_log,
                                            'description': description or ''
                                        })

                                        if not screenshot_base64:
                                            screenshot_base64 = engine.capture_screenshot()

                                        if screenshot_base64:
                                            screenshots.append({
                                                'url': screenshot_base64,
                                                'description': f'步骤 {i} 失败截图: {description or action_type_text}',
                                                'step_number': i,
                                                'timestamp': timezone.now().isoformat()
                                                # 移除 loaded 和 error 字段，让前端自行处理
                                            })
                                            execution_logs.append(f"  📸 失败截图已捕获")

                                        return False

                                    if action_type == 'screenshot' and screenshot_base64:
                                        screenshots.append({
                                            'url': screenshot_base64,
                                            'description': f'步骤 {i}: {description or "手动截图"}',
                                            'step_number': i,
                                            'timestamp': timezone.now().isoformat()
                                            # 移除 loaded 和 error 字段，让前端自行处理
                                        })

                                except Exception as e:
                                    execution_logs.append(f"  ✗ 步骤执行异常: {str(e)}")
                                    import traceback
                                    tb_str = traceback.format_exc()
                                    execution_logs.append(f"  [调试] 异常堆栈:\n{tb_str}")

                                    # 记录步骤执行结果（异常情况）
                                    step_results.append({
                                        'step_number': i,
                                        'action_type': action_type,
                                        'description': description or '',
                                        'success': False,
                                        'error': str(e)
                                    })

                                    execution_result['status'] = 'failed'
                                    execution_result['error_message'] = f"步骤 {i} 执行异常: {str(e)}"

                                    element_info = element_data['name'] if element_data else "未知元素"
                                    detailed_errors.append({
                                        'step_number': i,
                                        'action_type': action_type_text,
                                        'element': element_info,
                                        'message': f"步骤 {i}/{step_count} 执行异常",
                                        'details': f"异常: {str(e)}\n\n堆栈跟踪:\n{tb_str}",
                                        'description': description or ''
                                    })

                                    try:
                                        screenshot_base64 = engine.capture_screenshot()
                                        if screenshot_base64:
                                            screenshots.append({
                                                'url': screenshot_base64,
                                                'description': f'步骤 {i} 异常截图: {str(e)}',
                                                'step_number': i,
                                                'timestamp': timezone.now().isoformat()
                                                # 移除 loaded 和 error 字段，让前端自行处理
                                            })
                                    except:
                                        pass

                                    return False

                            execution_logs.append(f"========== 执行完成 ({step_count} 个步骤全部通过) ==========")
                            return True
                        else:
                            execution_logs.append("警告: 测试用例没有定义任何步骤")
                            execution_result['status'] = 'failed'
                            execution_result['error_message'] = "测试用例没有定义任何步骤，无法执行"
                            return False

                    finally:
                        execution_logs.append("")
                        execution_logs.append("========== 清理资源 ==========")
                        engine.stop()
                        execution_logs.append("✓ 浏览器已关闭")

                # 在独立线程中运行Selenium测试
                import threading
                test_thread = threading.Thread(target=run_test_selenium)
                test_thread.start()
                test_thread.join()

            else:
                # Playwright异步执行
                def run_test_in_thread():
                    """在独立线程中运行异步测试"""
                    async def run_test():
                        """异步执行测试"""
                        # 根据浏览器类型选择
                        browser_map = {
                            'chrome': 'chromium',
                            'firefox': 'firefox',
                            'safari': 'webkit'
                        }
                        browser_type = browser_map.get(request.data.get('browser', 'chrome'), 'chromium')
                        headless = request.data.get('headless', False)

                        # 创建Playwright引擎实例
                        engine = PlaywrightTestEngine(browser_type=browser_type, headless=headless)

                        try:
                            # 启动浏览器
                            execution_logs.append("========== 初始化浏览器 ==========")
                            await engine.start()
                            mode_text = "无头模式" if headless else "有头模式"
                            execution_logs.append(f"✓ {browser_type.capitalize()} 浏览器启动成功 (Playwright, {mode_text})")
                            execution_logs.append("")

                            # 导航到项目基础URL
                            if test_case.project.base_url:
                                execution_logs.append("========== 导航到测试页面 ==========")
                                success, nav_log = await engine.navigate(test_case.project.base_url)
                                execution_logs.append(nav_log)
                                execution_logs.append("")

                                if not success:
                                    execution_result['status'] = 'failed'
                                    execution_result['error_message'] = "导航到测试页面失败"
                                    return False

                            if steps_data:
                                execution_logs.append("========== 执行测试步骤 ==========")
                                step_count = len(steps_data)
                                execution_logs.append(f"共有 {step_count} 个步骤需要执行")
                                execution_logs.append("")

                                for i, step_info in enumerate(steps_data, 1):
                                    execution_logs.append(f"========== 开始执行步骤 {i}/{step_count} ==========")
                                    execution_logs.append(f"步骤 {i}/{step_count}:")

                                    # 从预先获取的数据中获取信息
                                    step = step_info['step']
                                    action_type = step_info['action_type']
                                    description = step_info['description']
                                    element_data = step_info['element_data']

                                    # 获取操作类型的中文显示
                                    action_choices_dict = dict(TestCaseStep.ACTION_TYPE_CHOICES)
                                    action_type_text = action_choices_dict.get(action_type, action_type)
                                    execution_logs.append(f"  操作: {action_type_text}")

                                    if description:
                                        execution_logs.append(f"  说明: {description}")

                                    if element_data:
                                        execution_logs.append(f"  元素: {element_data['name']}")
                                        execution_logs.append(f"  定位器: {element_data['locator_strategy']}={element_data['locator_value']}")
                                    else:
                                        execution_logs.append(f"  (此步骤不需要元素)")

                                    # 执行步骤
                                    try:
                                        execution_logs.append(f"  [调试] 准备执行步骤...")
                                        success, step_log, screenshot_base64 = await engine.execute_step(step, element_data or {})
                                        execution_logs.append(f"  [调试] 步骤执行完成, success={success}")

                                        execution_logs.append(f"  {step_log}")
                                        execution_logs.append("")

                                        # 记录步骤执行结果（用于JSON格式）
                                        step_results.append({
                                            'step_number': i,
                                            'action_type': action_type,
                                            'description': description or '',
                                            'success': success,
                                            'error': None if success else step_log,
                                            'screenshot': f'data:image/png;base64,{screenshot_base64}' if screenshot_base64 else None
                                        })

                                        # 如果步骤失败,保存截图
                                        if not success:
                                            execution_logs.append(f"  [调试] 检测到步骤失败,准备处理...")
                                            execution_result['status'] = 'failed'

                                            # 获取失败的元素信息
                                            element_info = element_data['name'] if element_data else "未知元素"

                                            execution_result['error_message'] = step_log  # 使用step_log作为错误信息

                                            # 添加详细错误信息
                                            detailed_errors.append({
                                                'step_number': i,
                                                'action_type': action_type_text,
                                                'element': element_info,
                                                'message': f"步骤 {i}/{step_count} 执行失败",
                                                'details': step_log,  # 包含详细的错误日志
                                                'description': description or ''
                                            })

                                            # 如果没有截图,捕获一张
                                            if not screenshot_base64:
                                                screenshot_base64 = await engine.capture_screenshot()

                                        if screenshot_base64:
                                            screenshots.append({
                                                'url': screenshot_base64,
                                                'description': f'步骤 {i} 失败截图: {description or action_type_text}',
                                                'step_number': i,
                                                'timestamp': timezone.now().isoformat()
                                                # 移除 loaded 和 error 字段，让前端自行处理
                                            })
                                            execution_logs.append(f"  📸 失败截图已捕获")

                                            execution_logs.append(f"  [调试] 步骤失败,准备退出执行...")
                                            return False

                                        # 如果是截图步骤且成功,也保存截图
                                        if action_type == 'screenshot' and screenshot_base64:
                                            screenshots.append({
                                                'url': screenshot_base64,
                                                'description': f'步骤 {i}: {description or "手动截图"}',
                                                'step_number': i,
                                                'timestamp': timezone.now().isoformat()
                                                # 移除 loaded 和 error 字段，让前端自行处理
                                            })

                                        execution_logs.append(f"  [调试] 步骤 {i} 成功完成,准备执行下一步...")

                                    except Exception as e:
                                        execution_logs.append(f"  ✗ 步骤执行异常: {str(e)}")
                                        execution_logs.append(f"  [调试] 异常详情: {repr(e)}")
                                        import traceback
                                        tb_str = traceback.format_exc()
                                        execution_logs.append(f"  [调试] 异常堆栈:\n{tb_str}")

                                        # 记录步骤执行结果（异常情况）
                                        step_results.append({
                                            'step_number': i,
                                            'action_type': action_type,
                                            'description': description or '',
                                            'success': False,
                                            'error': str(e)
                                        })

                                        execution_result['status'] = 'failed'
                                        execution_result['error_message'] = f"步骤 {i} 执行异常: {str(e)}"

                                        # 添加详细错误信息
                                        element_info = element_data['name'] if element_data else "未知元素"
                                        detailed_errors.append({
                                            'step_number': i,
                                            'action_type': action_type_text,
                                            'element': element_info,
                                            'message': f"步骤 {i}/{step_count} 执行异常",
                                            'details': f"异常: {str(e)}\n\n堆栈跟踪:\n{tb_str}",
                                            'description': description or ''
                                        })

                                        # 捕获异常截图
                                        try:
                                            screenshot_base64 = await engine.capture_screenshot()
                                            if screenshot_base64:
                                                screenshots.append({
                                                    'url': screenshot_base64,
                                                    'description': f'步骤 {i} 异常截图: {str(e)}',
                                                    'step_number': i,
                                                    'timestamp': timezone.now().isoformat()
                                                    # 移除 loaded 和 error 字段，让前端自行处理
                                                })
                                        except:
                                            pass

                                        execution_logs.append(f"  [调试] 发生异常,准备退出执行...")
                                        return False

                                # 所有步骤都成功
                                execution_logs.append(f"========== 执行完成 ({step_count} 个步骤全部通过) ==========")
                                return True

                            else:
                                execution_logs.append("警告: 测试用例没有定义任何步骤")
                                execution_result['status'] = 'failed'
                                execution_result['error_message'] = "测试用例没有定义任何步骤，无法执行"
                                return False

                        finally:
                            # 关闭浏览器
                            execution_logs.append("")
                            execution_logs.append("========== 清理资源 ==========")
                            await engine.stop()
                            execution_logs.append("✓ 浏览器已关闭")

                    # 在新的事件循环中运行测试
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(run_test())
                    finally:
                        loop.close()

                # 在独立线程中运行Playwright测试
                import threading
                test_thread = threading.Thread(target=run_test_in_thread)
                test_thread.start()
                test_thread.join()  # 等待测试完成

            # 计算总执行时间
            total_time = round(time.time() - start_time, 2)
            execution_logs.append("")
            execution_logs.append("执行环境信息:")
            execution_logs.append(f"- 执行引擎: {engine_type.upper()}")
            execution_logs.append(f"- 浏览器: {request.data.get('browser', 'chrome').capitalize()}")
            execution_logs.append(f"- 屏幕分辨率: 1920x1080")
            execution_logs.append(f"- 总执行时间: {total_time}秒")

            if screenshots:
                execution_logs.append(f"- 截图数量: {len(screenshots)} 张")

            # 保存执行日志和截图
            logger.info(f"[调试] 准备保存执行结果: execution_result['status'] = {execution_result['status']}")
            execution.status = execution_result['status']

            # 保存error_message（step_log已经是简洁的错误信息）
            execution.error_message = execution_result['error_message'] or ''

            # 保存步骤执行结果为JSON格式
            execution.execution_logs = json.dumps(step_results, ensure_ascii=False)
            execution.step_results = step_results
            execution.execution_time = total_time
            execution.finished_at = timezone.now()
            execution.screenshots = screenshots
            execution.save()
            logger.info(f"[调试] 执行结果已保存: execution.status = {execution.status}")

            serializer = TestCaseExecutionSerializer(execution)
            # 格式化错误信息为统一的对象格式
            errors = []
            if detailed_errors:
                # 使用详细的错误信息
                for error in detailed_errors:
                    errors.append({
                        'message': error['message'],
                        'details': error['details'],
                        'step_number': error['step_number'],
                        'action_type': error['action_type'],
                        'element': error['element'],
                        'description': error['description']
                    })
            elif execution.error_message:
                # 如果没有详细错误信息，使用简单格式
                errors.append({
                    'message': execution.error_message,
                    'details': ''
                })

            # 记录运行操作
            log_operation('run', 'test_case', test_case.id, test_case.name, request.user)

            return Response({
                'success': execution.status == 'passed',
                'logs': execution.execution_logs,
                'screenshots': screenshots,
                'execution_time': execution.execution_time,
                'errors': errors
            })

        except Exception as e:
            logger.error(f"执行测试用例失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return Response({
                'success': False,
                'logs': f"执行失败: {str(e)}\n\n{traceback.format_exc()}",
                'screenshots': [],
                'execution_time': 0,
                'errors': [{'message': str(e), 'stack': traceback.format_exc()}]
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def batch_run(self, request):
        """批量运行测试用例"""
        test_case_ids = request.data.get('test_case_ids', [])
        project_id = request.data.get('project_id')

        if not test_case_ids:
            return Response({'error': '请选择要运行的测试用例'}, status=status.HTTP_400_BAD_REQUEST)

        results = []
        for test_case_id in test_case_ids:
            try:
                test_case = TestCase.objects.get(id=test_case_id)
                # 这里调用单个运行逻辑
                # 简化处理，实际应该异步执行
                results.append({
                    'test_case_id': test_case_id,
                    'test_case_name': test_case.name,
                    'status': 'passed'
                })
            except TestCase.DoesNotExist:
                results.append({
                    'test_case_id': test_case_id,
                    'test_case_name': '未知',
                    'status': 'error',
                    'error': '测试用例不存在'
                })

        return Response({'results': results})

    def perform_destroy(self, instance):
        # 记录操作（在删除前记录）
        log_operation('delete', 'test_case', instance.id, instance.name, self.request.user)
        instance.delete()


class TestCaseStepViewSet(viewsets.ModelViewSet):
    """测试用例步骤视图集"""
    queryset = TestCaseStep.objects.all()
    serializer_class = TestCaseStepSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['step_number']
    ordering = ['step_number']
    filterset_fields = ['test_case', 'action_type']

    def get_queryset(self):
        # 只显示用户有权限访问的测试用例的步骤
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        accessible_test_cases = TestCase.objects.filter(project__in=accessible_projects)
        return TestCaseStep.objects.filter(test_case__in=accessible_projects)


class TestCaseExecutionViewSet(viewsets.ModelViewSet):
    """测试用例执行记录视图集"""
    queryset = TestCaseExecution.objects.all().select_related(
        'test_case', 'project', 'test_suite', 'executed_by'
    )
    serializer_class = TestCaseExecutionSerializer

    def get_serializer_class(self):
        """列表用轻量序列化器（不含日志/截图），详情用完整序列化器"""
        from .serializers import TestCaseExecutionListSerializer
        if self.action == 'list':
            return TestCaseExecutionListSerializer
        return TestCaseExecutionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['test_case__name', 'error_message']
    ordering_fields = ['created_at', 'started_at', 'finished_at', 'status']
    ordering = ['-created_at']
    filterset_fields = ['project', 'test_suite', 'test_case', 'status', 'execution_source']
    pagination_class = StandardPagination

    def get_queryset(self):
        # 只显示用户有权限访问的项目的执行记录
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return TestCaseExecution.objects.filter(
            project__in=accessible_projects
        ).select_related(
            'test_case', 'project', 'test_suite', 'created_by'
        )

    def perform_destroy(self, instance):
        # 记录操作
        name = instance.test_case.name if instance.test_case else f"执行记录#{instance.id}"
        log_operation('delete', 'report', instance.id, name, self.request.user)
        instance.delete()

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量删除执行记录"""
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': '未提供要删除的记录ID'}, status=status.HTTP_400_BAD_REQUEST)

        # 确保只能删除有权限的记录
        queryset = self.get_queryset()
        deleted_count, _ = queryset.filter(id__in=ids).delete()

        return Response({'message': f'成功删除 {deleted_count} 条记录'})

    @action(detail=True, methods=['post'], url_path='abort')
    def abort(self, request, pk=None):
        """请求中止一条正在执行的记录。

        语义与 APK 智能回归的 abort 一致：**置信号，不粗暴杀线程**。
        执行线程在「每个步骤前后」重读 `abort_requested`，命中即停剩余步骤，
        已完成的步骤结果保留（这些是排查问题的证据，不能丢）。

        ⚠️ 能力边界（必须让用户知道）：
        这只能停在「步骤之间」。如果某个步骤自身卡在 webdriver 的连接/命令上
        （例如 Appium 连不上，urllib3 正在重试），要等该步骤自己的超时结束
        才会走到检查点。所以文案不能承诺"立刻停止"。
        """
        execution = self.get_object()

        if execution.status in ('passed', 'failed', 'error'):
            return Response({
                'success': False,
                'message': f'该执行已结束（{execution.get_status_display()}），无需中止',
                'status': execution.status,
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            TestCaseExecution.objects.filter(id=execution.id).update(abort_requested=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[中止] 置信号失败 execution=%s: %s', execution.id, exc)
            return Response({'success': False, 'message': f'中止失败：{exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        done = len(execution.step_results or [])
        logger.warning('[中止] 用户请求终止 execution=%s（已完成 %d 步）', execution.id, done)
        return Response({
            'success': True,
            'message': f'已发送终止信号（已完成 {done} 步，结果保留）。'
                       '执行线程将在当前步骤结束后停止。',
            'status': execution.status,
            'abort_requested': True,
        })




class OperationRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """操作记录视图集（只读）"""
    queryset = OperationRecord.objects.all()
    serializer_class = OperationRecordSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['operation_type', 'resource_type', 'user']

    def get_queryset(self):
        # 返回最近的操作记录，按创建时间倒序
        # 过滤掉AI智能模式相关的操作记录
        queryset = OperationRecord.objects.exclude(
            resource_type__in=['ai_case', 'ai_execution']
        ).order_by('-created_at')

        # 支持通过查询参数限制返回数量
        limit = self.request.query_params.get('limit', None)
        if limit:
            try:
                limit = int(limit)
                queryset = queryset[:limit]
            except ValueError:
                pass

        return queryset


# ==================== 定时任务和通知相关视图 ====================

class UiScheduledTaskViewSet(viewsets.ModelViewSet):
    """UI定时任务视图集"""
    queryset = UiScheduledTask.objects.all()
    serializer_class = UiScheduledTaskSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['task_type', 'status', 'trigger_type', 'project']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'next_run_time', 'last_run_time']
    ordering = ['-created_at']

    def get_queryset(self):
        """只显示用户有权限访问的项目的定时任务"""
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        return UiScheduledTask.objects.filter(project__in=accessible_projects)

    def perform_create(self, serializer):
        """创建定时任务"""
        instance = serializer.save(created_by=self.request.user)
        log_operation('create', 'scheduled_task', instance.id, instance.name, self.request.user)

    def perform_update(self, serializer):
        """更新定时任务"""
        instance = serializer.save()
        log_operation('edit', 'scheduled_task', instance.id, instance.name, self.request.user)

    def perform_destroy(self, instance):
        """删除定时任务"""
        log_operation('delete', 'scheduled_task', instance.id, instance.name, self.request.user)
        instance.delete()

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """暂停定时任务"""
        task = self.get_object()
        task.status = 'PAUSED'
        task.save()
        return Response({'message': '任务已暂停', 'status': task.status})

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        """恢复定时任务"""
        task = self.get_object()
        task.status = 'ACTIVE'
        task.next_run_time = task.calculate_next_run()
        task.save()
        return Response({'message': '任务已恢复', 'status': task.status})

    @action(detail=True, methods=['post'])
    def run_now(self, request, pk=None):
        """立即运行任务"""
        task = self.get_object()

        try:
            # 更新任务执行时间和次数
            task.last_run_time = timezone.now()
            task.total_runs += 1
            # 重新计算下次运行时间
            task.next_run_time = task.calculate_next_run()
            task.save()

            # 根据任务类型执行不同的逻辑
            if task.task_type == 'TEST_SUITE':
                # 执行测试套件
                if not task.test_suite:
                    return Response({
                        'error': '该任务未配置测试套件'
                    }, status=status.HTTP_400_BAD_REQUEST)

                test_suite = task.test_suite
                test_case_count = test_suite.suite_test_cases.count()

                if test_case_count == 0:
                    return Response({
                        'error': '该测试套件未包含任何测试用例，无法执行'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # 更新套件执行状态
                test_suite.execution_status = 'running'
                test_suite.save()

                # 在后台线程中执行测试
                import threading
                from .test_executor import TestExecutor

                def run_test():
                    try:
                        executor = TestExecutor(
                            test_suite=test_suite,
                            engine=task.engine,
                            browser=task.browser,
                            headless=task.headless,
                            executed_by=task.created_by
                        )
                        executor.run()

                        # 更新任务执行结果
                        task.successful_runs += 1
                        task.last_result = {'status': 'success', 'message': '测试套件执行成功'}
                        task.error_message = ''
                        task.save()

                        # 发送成功通知
                        self._send_task_notification(task, success=True)

                    except Exception as e:
                        task.failed_runs += 1
                        task.last_result = {'status': 'failed', 'message': str(e)}
                        task.error_message = str(e)
                        test_suite.execution_status = 'failed'
                        test_suite.save()
                        task.save()

                        # 发送失败通知
                        self._send_task_notification(task, success=False)

                # 启动后台线程执行测试
                thread = threading.Thread(target=run_test)
                thread.daemon = True
                thread.start()

                log_operation('run', 'scheduled_task', task.id, task.name, request.user)

                return Response({
                    'message': '测试套件开始执行',
                    'task_id': task.id,
                    'task_name': task.name,
                    'test_suite': test_suite.name,
                    'test_case_count': test_case_count,
                    'engine': task.engine,
                    'browser': task.browser,
                    'headless': task.headless
                }, status=status.HTTP_200_OK)

            elif task.task_type == 'TEST_CASE':
                # 执行测试用例
                if not task.test_cases or len(task.test_cases) == 0:
                    return Response({
                        'error': '该任务未配置测试用例'
                    }, status=status.HTTP_400_BAD_REQUEST)

                test_case_ids = task.test_cases
                test_cases = TestCase.objects.filter(id__in=test_case_ids)
                test_case_count = test_cases.count()

                if test_case_count == 0:
                    return Response({
                        'error': '找不到配置的测试用例'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # 在后台线程中执行测试用例
                import threading

                def run_test_cases():
                    """在后台线程中执行测试用例"""
                    success_count = 0
                    failed_count = 0

                    try:
                        for test_case in test_cases:
                            # 创建执行记录
                            execution = TestCaseExecution.objects.create(
                                test_case=test_case,
                                project=task.project,
                                execution_source='scheduled',
                                status='running',
                                engine=task.engine,
                                browser=task.browser,
                                headless=task.headless,
                                created_by=task.created_by,
                                started_at=timezone.now()
                            )

                            # 实际执行测试用例
                            try:
                                logger.info(f"开始执行定时任务的测试用例: {test_case.name} (ID: {test_case.id})")

                                start_time = time.time()

                                # 获取测试用例的所有步骤
                                test_steps = list(test_case.steps.all().order_by('step_number'))

                                # 预先获取所有步骤的数据
                                steps_data = []
                                for step in test_steps:
                                    step_data = {
                                        'step': step,
                                        'action_type': step.action_type,
                                        'description': step.description,
                                        'input_value': step.input_value,
                                        'wait_time': step.wait_time,
                                        'assert_type': step.assert_type,
                                        'assert_value': step.assert_value,
                                    }

                                    if step.element:
                                        step_data['element_data'] = {
                                            'locator_strategy': step.element.locator_strategy.name if step.element.locator_strategy else 'css',
                                            'locator_value': step.element.locator_value,
                                            'name': step.element.name,
                                            'wait_timeout': step.element.wait_timeout,
                                            'force_action': step.element.force_action
                                        }
                                    else:
                                        step_data['element_data'] = None

                                    steps_data.append(step_data)

                                # 存储步骤执行结果和截图
                                step_results = []
                                screenshots = []
                                execution_logs = []
                                execution_result = {'status': 'passed', 'error_message': None}

                                # 根据引擎类型执行
                                if task.engine == 'selenium':
                                    from .selenium_engine import SeleniumTestEngine

                                    # 检查浏览器是否可用
                                    is_available, error_msg = SeleniumTestEngine.check_browser_available(task.browser)
                                    if not is_available:
                                        execution.status = 'failed'
                                        execution.error_message = error_msg
                                        execution.execution_logs = json.dumps([{
                                            'step_number': 0,
                                            'action_type': '浏览器检查',
                                            'description': '执行前浏览器环境检查',
                                            'success': False,
                                            'error': error_msg
                                        }], ensure_ascii=False)
                                        execution.finished_at = timezone.now()
                                        execution.save()
                                        failed_count += 1
                                        continue

                                    # 创建Selenium引擎实例并执行
                                    engine = SeleniumTestEngine(browser_type=task.browser, headless=task.headless)

                                    try:
                                        # 启动浏览器
                                        engine.start()
                                        execution_logs.append("✓ 浏览器启动成功")

                                        # 导航到项目基础URL
                                        if test_case.project.base_url:
                                            success, nav_log = engine.navigate(test_case.project.base_url)
                                            execution_logs.append(nav_log)
                                            if not success:
                                                execution_result['status'] = 'failed'
                                                execution_result['error_message'] = "导航到测试页面失败"
                                                raise Exception("导航到测试页面失败")

                                        # 执行测试步骤
                                        for i, step_info in enumerate(steps_data, 1):
                                            step = step_info['step']
                                            action_type = step_info['action_type']
                                            element_data = step_info['element_data']

                                            success, step_log, screenshot_base64 = engine.execute_step(step, element_data or {})

                                            step_results.append({
                                                'step_number': i,
                                                'action_type': action_type,
                                                'description': step_info['description'] or '',
                                                'success': success,
                                                'error': None if success else step_log,
                                                'screenshot': f'data:image/png;base64,{screenshot_base64}' if screenshot_base64 else None
                                            })

                                            if not success:
                                                execution_result['status'] = 'failed'
                                                execution_result['error_message'] = step_log

                                                if not screenshot_base64:
                                                    screenshot_base64 = engine.capture_screenshot()

                                                if screenshot_base64:
                                                    screenshots.append({
                                                        'url': screenshot_base64,
                                                        'description': f'步骤 {i} 失败截图',
                                                        'step_number': i,
                                                        'timestamp': timezone.now().isoformat()
                                                    })

                                                break

                                            if action_type == 'screenshot' and screenshot_base64:
                                                screenshots.append({
                                                    'url': screenshot_base64,
                                                    'description': f'步骤 {i}: {step_info["description"] or "手动截图"}',
                                                    'step_number': i,
                                                    'timestamp': timezone.now().isoformat()
                                                })

                                    finally:
                                        engine.stop()

                                else:  # Playwright
                                    import asyncio
                                    from asgiref.sync import sync_to_async
                                    from .playwright_engine import PlaywrightTestEngine

                                    async def run_playwright_test():
                                        browser_map = {
                                            'chrome': 'chromium',
                                            'firefox': 'firefox',
                                            'safari': 'webkit'
                                        }
                                        browser_type = browser_map.get(task.browser, 'chromium')

                                        engine = PlaywrightTestEngine(browser_type=browser_type, headless=task.headless)

                                        try:
                                            # 启动浏览器
                                            await engine.start()
                                            execution_logs.append("✓ 浏览器启动成功")

                                            # 获取项目基础URL（同步操作）
                                            base_url = await sync_to_async(lambda: test_case.project.base_url)()

                                            # 导航到项目基础URL
                                            if base_url:
                                                success, nav_log = await engine.navigate(base_url)
                                                execution_logs.append(nav_log)
                                                if not success:
                                                    execution_result['status'] = 'failed'
                                                    execution_result['error_message'] = "导航到测试页面失败"
                                                    return False

                                            # 执行测试步骤
                                            for i, step_info in enumerate(steps_data, 1):
                                                step = step_info['step']
                                                action_type = step_info['action_type']
                                                element_data = step_info['element_data']

                                                success, step_log, screenshot_base64 = await engine.execute_step(step, element_data or {})

                                                step_results.append({
                                                    'step_number': i,
                                                    'action_type': action_type,
                                                    'description': step_info['description'] or '',
                                                    'success': success,
                                                    'error': None if success else step_log,
                                                    'screenshot': f'data:image/png;base64,{screenshot_base64}' if screenshot_base64 else None
                                                })

                                                if not success:
                                                    execution_result['status'] = 'failed'
                                                    execution_result['error_message'] = step_log

                                                    if not screenshot_base64:
                                                        screenshot_base64 = await engine.capture_screenshot()

                                                    if screenshot_base64:
                                                        screenshots.append({
                                                            'url': screenshot_base64,
                                                            'description': f'步骤 {i} 失败截图',
                                                            'step_number': i,
                                                            'timestamp': timezone.now().isoformat()
                                                        })

                                                    return False

                                                if action_type == 'screenshot' and screenshot_base64:
                                                    screenshots.append({
                                                        'url': screenshot_base64,
                                                        'description': f'步骤 {i}: {step_info["description"] or "手动截图"}',
                                                        'step_number': i,
                                                        'timestamp': timezone.now().isoformat()
                                                    })

                                            return True

                                        finally:
                                            await engine.stop()

                                    # 在新的事件循环中运行Playwright测试
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    try:
                                        loop.run_until_complete(run_playwright_test())
                                    finally:
                                        loop.close()

                                # 计算执行时间
                                total_time = round(time.time() - start_time, 2)

                                # 保存执行结果
                                execution.status = execution_result['status']
                                execution.error_message = execution_result['error_message'] or ''
                                execution.execution_logs = json.dumps(step_results, ensure_ascii=False)
                                execution.step_results = step_results
                                execution.execution_time = total_time
                                execution.screenshots = screenshots
                                execution.finished_at = timezone.now()
                                execution.save()

                                if execution.status == 'passed':
                                    success_count += 1
                                    logger.info(f"测试用例 {test_case.name} 执行成功")
                                else:
                                    failed_count += 1
                                    logger.warning(f"测试用例 {test_case.name} 执行失败: {execution.error_message}")

                            except Exception as e:
                                logger.error(f"执行测试用例 {test_case.name} 时发生异常: {str(e)}")
                                execution.status = 'failed'
                                execution.error_message = str(e)
                                execution.finished_at = timezone.now()
                                execution.save()
                                failed_count += 1

                        # 更新任务执行结果
                        if failed_count == 0:
                            task.successful_runs += 1
                            task.last_result = {
                                'status': 'success',
                                'message': f'执行完成: {success_count}个成功',
                                'success_count': success_count,
                                'failed_count': failed_count
                            }
                            task.error_message = ''
                            task.save()

                            # 发送成功通知
                            self._send_task_notification(task, success=True)
                        else:
                            task.failed_runs += 1
                            task.last_result = {
                                'status': 'partial',
                                'message': f'执行完成: {success_count}个成功, {failed_count}个失败',
                                'success_count': success_count,
                                'failed_count': failed_count
                            }
                            task.error_message = f'{failed_count}个测试用例执行失败'
                            task.save()

                            # 发送失败通知
                            self._send_task_notification(task, success=False)

                    except Exception as e:
                        logger.error(f"执行定时任务测试用例时发生异常: {str(e)}")
                        task.failed_runs += 1
                        task.last_result = {'status': 'failed', 'message': str(e)}
                        task.error_message = str(e)
                        task.save()

                        # 发送失败通知
                        self._send_task_notification(task, success=False)

                # 启动后台线程执行测试
                thread = threading.Thread(target=run_test_cases)
                thread.daemon = True
                thread.start()

                log_operation('run', 'scheduled_task', task.id, task.name, request.user)

                return Response({
                    'message': '测试用例开始执行',
                    'task_id': task.id,
                    'task_name': task.name,
                    'test_case_count': test_case_count,
                    'engine': task.engine,
                    'browser': task.browser,
                    'headless': task.headless
                }, status=status.HTTP_200_OK)

            else:
                return Response({
                    'error': '不支持的任务类型'
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f'执行定时任务失败: {str(e)}')
            return Response({
                'error': f'执行失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _send_task_notification(self, task, success):
        """发送任务执行通知"""
        try:
            logger.info(f"准备发送任务 {task.id} 的通知，执行结果: {'成功' if success else '失败'}")

            # 检查是否需要发送通知
            if success and not task.notify_on_success:
                logger.info("任务执行成功但未启用成功通知")
                return

            if not success and not task.notify_on_failure:
                logger.info("任务执行失败但未启用失败通知")
                return

            # 检查通知类型
            if not task.notification_type:
                logger.info("未设置通知类型")
                return

            logger.info(f"通知类型: {task.notification_type}")

            # 根据通知类型发送不同的通知
            if task.notification_type in ['webhook', 'both']:
                logger.info("发送Webhook通知")
                self._send_webhook_notification(task, success)

            if task.notification_type in ['email', 'both']:
                logger.info("发送邮件通知")
                self._send_email_notification(task, success)

        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}", exc_info=True)

    def _send_webhook_notification(self, task, success):
        """发送Webhook通知"""
        try:
            import requests
            import json

            logger.info("=== 开始发送Webhook通知 ===")

            # 使用统一的通知配置
            try:
                from apps.core.models import UnifiedNotificationConfig
                all_webhook_configs = UnifiedNotificationConfig.objects.filter(
                    config_type__in=['webhook_wechat', 'webhook_feishu', 'webhook_dingtalk'],
                    is_active=True
                )
                logger.info("使用统一通知配置 (UnifiedNotificationConfig)")
            except ImportError as e:
                # 如果 core 模块不可用，记录错误并返回
                logger.error(f"无法导入统一通知配置: {e}")
                logger.warning("通知发送失败：无法找到通知配置模块")
                return
            except Exception as e:
                logger.error(f"获取通知配置时出错: {e}")
                return

            all_webhook_bots = []
            for config in all_webhook_configs:
                bots = config.get_webhook_bots()
                if bots:
                    for bot in bots:
                        # 只添加启用了"UI自动化测试"的机器人
                        if bot.get('enabled', True) and bot.get('enable_ui_automation', True):
                            all_webhook_bots.append(bot)
                            logger.info(f"添加机器人: {bot.get('name')} (UI自动化测试已启用)")
                        elif bot.get('enabled', True):
                            logger.info(f"配置中心机器人 {bot.get('name')} 未启用UI自动化测试，跳过")

            if not all_webhook_bots:
                logger.warning("没有找到任何启用的webhook机器人配置")
                return

            logger.info(f"找到 {len(all_webhook_bots)} 个启用的webhook机器人配置")

            # 准备通知内容
            status_text = '成功' if success else '失败'
            task_type_text = '测试套件执行' if task.task_type == 'TEST_SUITE' else '测试用例执行'

            # 获取最后执行结果的详细信息
            last_result = task.last_result or {}
            result_message = last_result.get('message', '')
            success_count = last_result.get('success_count', 0)
            failed_count = last_result.get('failed_count', 0)

            # 为不同的机器人平台准备消息格式
            for bot in all_webhook_bots:
                if not bot.get('enabled', True) or not bot.get('webhook_url'):
                    logger.info(f"跳过未启用或无URL的机器人: {bot.get('name', 'Unknown')}")
                    continue

                bot_type = bot.get('type', 'unknown')
                webhook_url = bot['webhook_url']
                logger.info(f"发送通知到 {bot_type} 机器人: {bot.get('name', 'Unknown')}")

                # 构造详细内容
                # 转换执行时间到本地时区
                local_run_time = timezone.localtime(task.last_run_time).strftime('%Y-%m-%d %H:%M:%S') if task.last_run_time else '未知'
                detail_content = f"""任务名称: {task.name}

执行状态: {status_text}

执行时间: {local_run_time}

任务类型: {task_type_text}

执行引擎: {task.engine.upper()}

浏览器: {task.browser.capitalize()}"""

                if result_message:
                    detail_content += f"\n\n执行结果: {result_message}"

                if success_count > 0 or failed_count > 0:
                    detail_content += f"\n\n成功: {success_count} 个，失败: {failed_count} 个"

                # 根据机器人类型构造消息格式
                if bot_type == 'wechat':  # 企业微信
                    message_data = {
                        "msgtype": "markdown",
                        "markdown": {
                            "content": f"""**UI自动化定时任务执行{status_text}**

{detail_content}"""
                        }
                    }
                elif bot_type == 'feishu':  # 飞书
                    message_data = {
                        "msg_type": "interactive",
                        "card": {
                            "elements": [{
                                "tag": "div",
                                "text": {
                                    "content": f"**UI自动化定时任务执行{status_text}**\n\n{detail_content}",
                                    "tag": "lark_md"
                                }
                            }],
                            "header": {
                                "title": {
                                    "content": f"UI自动化定时任务执行{status_text}",
                                    "tag": "plain_text"
                                },
                                "template": "green" if success else "red"
                            }
                        }
                    }
                elif bot_type == 'dingtalk':  # 钉钉
                    message_data = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": f"UI自动化定时任务执行{status_text}",
                            "text": f"""**UI自动化定时任务执行{status_text}**

{detail_content}"""
                        }
                    }

                    # 钉钉机器人签名验证
                    secret = bot.get('secret')
                    if secret:
                        import time
                        import hmac
                        import hashlib
                        import base64
                        import urllib.parse

                        timestamp = str(round(time.time() * 1000))
                        string_to_sign = f'{timestamp}\n{secret}'
                        string_to_sign_enc = string_to_sign.encode('utf-8')
                        secret_enc = secret.encode('utf-8')
                        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

                        # 在URL中添加签名参数
                        if '?' in webhook_url:
                            webhook_url += f'&timestamp={timestamp}&sign={sign}'
                        else:
                            webhook_url += f'?timestamp={timestamp}&sign={sign}'
                else:
                    logger.warning(f"未知的机器人类型: {bot_type}")
                    continue

                # 发送webhook请求
                try:
                    logger.info(f"发送请求到: {webhook_url}")
                    logger.info(f"消息数据: {json.dumps(message_data, ensure_ascii=False, indent=2)}")

                    response = requests.post(
                        webhook_url,
                        json=message_data,
                        headers={'Content-Type': 'application/json'},
                        timeout=10
                    )

                    logger.info(f"响应状态码: {response.status_code}")
                    logger.info(f"响应内容: {response.text}")

                    if response.status_code == 200:
                        logger.info(f"成功发送通知到 {bot.get('name', 'Unknown')}")

                        # 记录通知日志
                        UiNotificationLog.objects.create(
                            task=task,
                            task_name=task.name,
                            task_type=task.task_type,
                            notification_type='task_execution',
                            sender_name='系统Webhook通知',
                            sender_email='system@notification.com',
                            recipient_info=[{'name': bot.get('name', 'Unknown'), 'webhook_url': webhook_url}],
                            webhook_bot_info=bot,
                            notification_content=json.dumps(message_data, ensure_ascii=False),
                            status='success',
                            response_info={'status_code': response.status_code, 'response': response.text},
                            sent_at=timezone.now()
                        )
                    else:
                        logger.error(f"发送通知失败，状态码: {response.status_code}, 响应: {response.text}")

                        # 记录失败日志
                        UiNotificationLog.objects.create(
                            task=task,
                            task_name=task.name,
                            task_type=task.task_type,
                            notification_type='task_execution',
                            sender_name='系统Webhook通知',
                            sender_email='system@notification.com',
                            recipient_info=[{'name': bot.get('name', 'Unknown'), 'webhook_url': webhook_url}],
                            webhook_bot_info=bot,
                            notification_content=json.dumps(message_data, ensure_ascii=False),
                            status='failed',
                            error_message=f'HTTP {response.status_code}: {response.text}',
                            response_info={'status_code': response.status_code, 'response': response.text}
                        )

                except requests.exceptions.RequestException as e:
                    logger.error(f"发送webhook请求失败: {str(e)}")

                    # 记录失败日志
                    UiNotificationLog.objects.create(
                        task=task,
                        task_name=task.name,
                        task_type=task.task_type,
                        notification_type='task_execution',
                        sender_name='系统Webhook通知',
                        sender_email='system@notification.com',
                        recipient_info=[{'name': bot.get('name', 'Unknown'), 'webhook_url': webhook_url}],
                        webhook_bot_info=bot,
                        notification_content=json.dumps(message_data, ensure_ascii=False),
                        status='failed',
                        error_message=str(e)
                    )

        except Exception as e:
            logger.error(f"发送Webhook通知失败: {str(e)}", exc_info=True)

    def _send_email_notification(self, task, success):
        """发送邮件通知"""
        try:
            from django.core.mail import send_mail
            from django.conf import settings

            logger.info("=== 开始发送邮件通知 ===")

            # 获取收件人列表
            recipients = []
            if task.notify_emails:
                if isinstance(task.notify_emails, list):
                    recipients = task.notify_emails
                else:
                    recipients = [task.notify_emails]

            if not recipients:
                logger.warning("没有找到任何邮件收件人")
                return

            # 准备邮件内容
            status_text = '成功' if success else '失败'
            task_type_text = '测试套件执行' if task.task_type == 'TEST_SUITE' else '测试用例执行'

            subject = f"UI自动化定时任务执行{status_text}: {task.name}"

            last_result = task.last_result or {}
            result_message = last_result.get('message', '')

            # 转换执行时间到本地时区
            local_run_time = timezone.localtime(task.last_run_time).strftime('%Y-%m-%d %H:%M:%S') if task.last_run_time else '未知'

            message = f"""
任务名称: {task.name}
执行状态: {status_text}
执行时间: {local_run_time}
任务类型: {task_type_text}
执行引擎: {task.engine.upper()}
浏览器: {task.browser.capitalize()}

执行结果:
{result_message if result_message else '无详细信息'}

错误信息:
{task.error_message if task.error_message else '无错误信息'}
            """

            # 发送邮件
            from_email = settings.DEFAULT_FROM_EMAIL
            logger.info(f"准备发送邮件，发件人: {from_email}, 收件人: {recipients}")

            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=recipients,
                fail_silently=False,
            )
            logger.info("邮件发送成功")

            # 记录通知日志
            UiNotificationLog.objects.create(
                task=task,
                task_name=task.name,
                task_type=task.task_type,
                notification_type='task_execution',
                sender_name='系统邮件通知',
                sender_email=from_email,
                recipient_info=[{'email': email} for email in recipients],
                notification_content=message,
                status='success',
                sent_at=timezone.now()
            )

        except Exception as e:
            logger.error(f"发送邮件通知失败: {str(e)}", exc_info=True)

            # 记录失败日志
            try:
                UiNotificationLog.objects.create(
                    task=task,
                    task_name=task.name,
                    task_type=task.task_type,
                    notification_type='task_execution',
                    sender_name='系统邮件通知',
                    sender_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_info=[{'email': email} for email in recipients] if recipients else [],
                    notification_content=f"发送邮件通知失败: {str(e)}",
                    status='failed',
                    error_message=str(e)
                )
            except:
                pass


class UiNotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """UI通知日志视图集（只读）"""
    queryset = UiNotificationLog.objects.all()
    serializer_class = UiNotificationLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'notification_type']
    search_fields = ['task_name', 'notification_content']
    ordering_fields = ['created_at', 'sent_at']
    ordering = ['-created_at']

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """重试发送通知"""
        log = self.get_object()
        if log.status == 'failed':
            # 这里应该触发实际的重试逻辑
            log.retry_count += 1
            log.is_retried = True
            log.save()
            return Response({'message': '通知已加入重试队列'})
        return Response({'error': '只能重试失败的通知'}, status=status.HTTP_400_BAD_REQUEST)


class UiTaskNotificationSettingViewSet(viewsets.ModelViewSet):
    """UI任务通知设置视图集"""
    queryset = UiTaskNotificationSetting.objects.all()
    serializer_class = UiTaskNotificationSettingSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['task', 'is_enabled', 'notification_type']


class AICaseViewSet(viewsets.ModelViewSet):
    queryset = AICase.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = AICaseSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project']
    search_fields = ['name', 'description', 'task_description']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        # 返回用户有权限的项目下的AI用例，以及没有关联项目的AI用例
        return AICase.objects.filter(
            models.Q(project__in=accessible_projects) | models.Q(project__isnull=True)
        ).distinct()

    def perform_create(self, serializer):
        instance = serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()

    @action(detail=False, methods=['post'], url_path='import-from-generated')
    def import_from_generated(self, request):
        """将AI生成的测试用例（需求分析模块的 TestCaseGenerationTask）批量转换为UI自动化AI用例(AICase)，转换后可直接执行"""
        from apps.requirement_analysis.models import TestCaseGenerationTask

        task_id = request.data.get('task_id')
        if not task_id:
            return Response({'error': '缺少 task_id 参数'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            task = TestCaseGenerationTask.objects.get(task_id=task_id)
        except TestCaseGenerationTask.DoesNotExist:
            return Response({'error': '未找到对应的生成任务'}, status=status.HTTP_404_NOT_FOUND)

        if not task.final_test_cases:
            return Response({'error': '该任务没有最终生成的测试用例'}, status=status.HTTP_404_NOT_FOUND)

        # 支持只转换前端勾选的指定用例；未指定 cases 时转换全部
        selected_cases = request.data.get('cases')
        if selected_cases is not None:
            if not isinstance(selected_cases, list) or len(selected_cases) == 0:
                return Response({'error': 'cases 为空，请至少选择一条用例'}, status=status.HTTP_400_BAD_REQUEST)
            test_cases = selected_cases
        else:
            test_cases = self._parse_generation_task_cases(task.final_test_cases)
            if not test_cases:
                # 解析失败时，将整个内容作为一个 AICase 导入
                ai_case = AICase.objects.create(
                    name=task.title or f'AI生成用例-{task.task_id}',
                    description=f'由AI生成任务导入（{task.task_id}），原始文本模式',
                    task_description=f"任务标题：{task.title or '未命名'}\n\n{task.final_test_cases}",
                    created_by=request.user
                )
                return Response({
                    'message': '已将全部测试用例内容导入为1条UI自动化AI用例（因无法自动拆分，整段文本作为任务描述）',
                    'created_ids': [ai_case.id],
                    'count': 1,
                    'mode': 'raw'
                }, status=status.HTTP_201_CREATED)

        created_ids = []
        for idx, case in enumerate(test_cases):
            scenario = case.get('scenario', case.get('title', f'用例{idx+1}'))
            precondition = case.get('precondition', '')
            steps = case.get('steps', case.get('test_steps', ''))
            expected = case.get('expected', case.get('expected_result', ''))
            priority = case.get('priority', '')

            task_description = (
                f"任务目标：{scenario}\n"
                f"前置条件：{precondition or '无'}\n"
                f"测试步骤：\n{steps or '无'}\n"
                f"预期结果：{expected or '无'}"
            )
            case_id = case.get('caseId', str(idx + 1))
            description = f'由AI生成任务导入（{task.task_id}/{case_id}）'
            if priority:
                description += f'，优先级 {priority}'

            ai_case = AICase.objects.create(
                name=scenario,
                description=description,
                task_description=task_description,
                created_by=request.user
            )
            created_ids.append(ai_case.id)

        return Response({
            'message': f'成功导入 {len(created_ids)} 条AI生成用例为UI自动化AI用例',
            'created_ids': created_ids,
            'count': len(created_ids)
        }, status=status.HTTP_201_CREATED)

    @staticmethod
    def _parse_generation_task_cases(content):
        """解析 TestCaseGenerationTask.final_test_cases 文本，返回用例列表"""
        import re
        if not content or not content.strip():
            return []

        clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
        cases = []

        # 表格格式
        if '|' in clean:
            lines = [l.strip() for l in clean.split('\n') if l.strip() and '|' in l]
            rows = []
            for line in lines:
                cells = [c.strip() for c in line.split('|')]
                while cells and cells[0] == '':
                    cells = cells[1:]
                while cells and cells[-1] == '':
                    cells.pop()
                if len(cells) > 1:
                    rows.append(cells)
            if len(rows) >= 2:
                headers = [h.lower() for h in rows[0]]
                for row in rows[1:]:
                    tc = {}
                    for i, h in enumerate(headers):
                        v = row[i] if i < len(row) else ''
                        if any(k in h for k in ['编号','id','序号']):
                            tc['caseId'] = v
                        elif any(k in h for k in ['场景','标题','名称','title','目标']):
                            tc['scenario'] = v
                        elif any(k in h for k in ['前置','前提','precondition']):
                            tc['precondition'] = v
                        elif any(k in h for k in ['步骤','step']):
                            tc['steps'] = v
                        elif any(k in h for k in ['预期','结果','expected','result']):
                            tc['expected'] = v
                        elif 'priority' in h or '优先级' in h:
                            tc['priority'] = v
                    if tc.get('scenario') or tc.get('steps'):
                        cases.append(tc)
                return cases

        # 文本格式
        lines = clean.split('\n')
        current = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            is_start = (
                '测试用例' in line or 'Test Case' in line
                or bool(re.match(r'^\d+[\.\)、]', line))
                or line.startswith(('一、','二、','三、','四、','五、'))
            )
            if is_start:
                if current:
                    cases.append(current)
                title = re.sub(r'^\d+[\.\)、]\s*', '', line.replace('测试用例','').replace('Test Case','').replace(':','').replace('：','').strip())
                current = {'scenario': title}
            elif current:
                for keywords, key in [(['前置条件','前提条件','前置','前提'],'precondition'),
                                      (['测试步骤','操作步骤','执行步骤','步骤'],'steps'),
                                      (['预期结果','期望结果','预期'],'expected'),
                                      (['优先级'],'priority')]:
                    if any(kw in line for kw in keywords):
                        for sep in [':','：','】']:
                            if sep in line:
                                current[key] = line.split(sep, 1)[-1].strip()
                                break
                        else:
                            for pfx in keywords:
                                if line.startswith(pfx):
                                    current[key] = line[len(pfx):].strip()
                                    break
        if current:
            cases.append(current)
        return cases

    @action(detail=False, methods=['post'], url_path='import-from-testcases')
    def import_from_testcases(self, request):
        """将用例库（apps.testcases.TestCase）中的用例批量转为UI自动化AI用例(AICase)，转换后可直接执行"""
        from apps.testcases.models import TestCase

        case_ids = request.data.get('case_ids')
        if not case_ids or not isinstance(case_ids, list) or len(case_ids) == 0:
            return Response({'error': '请提供 case_ids 列表'}, status=status.HTTP_400_BAD_REQUEST)

        testcases_qs = TestCase.objects.filter(id__in=case_ids)
        if not testcases_qs.exists():
            return Response({'error': '未找到对应的用例库用例'}, status=status.HTTP_404_NOT_FOUND)

        created_ids = []
        for case in testcases_qs:
            # 拼接步骤：优先用步骤明细，其次用 steps 文本
            steps_text = (case.steps or '').strip()
            if case.step_details.exists():
                detail_lines = []
                for s in case.step_details.all().order_by('step_number'):
                    line = f"{s.step_number}. {s.action or ''}"
                    if s.expected:
                        line += f" → 预期：{s.expected}"
                    detail_lines.append(line)
                if detail_lines:
                    steps_text = '\n'.join(detail_lines)
            if not steps_text:
                steps_text = '参考测试目标执行相应操作'

            task_description = (
                f"任务目标：{case.title}\n"
                f"前置条件：{case.preconditions or '无'}\n"
                f"测试步骤：\n{steps_text}\n"
                f"预期结果：{case.expected_result or '无'}"
            )
            priority_text = dict(TestCase.PRIORITY_CHOICES).get(case.priority, case.priority)
            description = f"由用例库导入（用例ID {case.id}）"
            if priority_text:
                description += f"，优先级 {priority_text}"
            if case.test_type:
                type_map = dict(TestCase.TYPE_CHOICES)
                description += f"，测试类型 {type_map.get(case.test_type, case.test_type)}"

            ai_case = AICase.objects.create(
                name=case.title,
                description=description,
                task_description=task_description,
                created_by=request.user
            )
            created_ids.append(ai_case.id)

        return Response({
            'message': f'成功导入 {len(created_ids)} 条用例库用例为UI自动化AI用例',
            'created_ids': created_ids,
            'count': len(created_ids)
        }, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        instance.delete()

    @action(detail=False, methods=['post'], url_path='midscene-playground')
    def midscene_playground(self, request):
        """
        Midscene Playground：自然语言单条调试（连真机执行并返回截图 base64）。
        用于验证 Midscene 自然语言操作方式，确认稳定后再沉淀为正式步骤。
        """
        data = request.data
        app_config_id = data.get('app_config_id')
        device_id = data.get('device_id')
        prompt = (data.get('prompt') or '').strip()
        action = data.get('action') or 'aiAct'
        launch_app = data.get('launch_app', True)

        if not app_config_id or not device_id:
            return Response(
                {'error': 'app_config_id 与 device_id 均为必填'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not prompt:
            return Response(
                {'error': 'prompt（自然语言操作指令）不能为空'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if action not in ('aiAct', 'aiTap', 'aiInput', 'aiQuery'):
            return Response(
                {'error': 'action 仅支持 aiAct/aiTap/aiInput/aiQuery'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from apps.ui_automation.models import AppConfig, AppDevice
            app_config = AppConfig.objects.get(id=app_config_id)
            device = AppDevice.objects.get(id=device_id)
        except (AppConfig.DoesNotExist, AppDevice.DoesNotExist):
            return Response(
                {'error': '应用配置或设备不存在'},
                status=status.HTTP_404_NOT_FOUND
            )

        # 同一用户不允许并行跑多条 Playground：
        # ① 同一台设备上两条指令会互相抢输入，结果不可信；
        # ② 终止只能按用户定位，并行时"停哪一条"无法回答。
        _key = _playground_key(request)
        if _key in PLAYGROUND_ACTIVE:
            return Response({
                'status': 'failed',
                'error': '上一条 Playground 指令还在执行中，请先点「终止执行」或等待其结束',
            }, status=status.HTTP_409_CONFLICT)

        _slot = {
            'device_id': device_id,
            'action': action,
            'prompt': prompt[:80],
            'aborted': False,
            'started_at': timezone.now(),
        }
        PLAYGROUND_ACTIVE[_key] = _slot
        try:
            if (device.platform or '').strip().lower() == 'ios':
                # iOS 真机走远端 WDA（设备配置的 wda_host / wda_port）
                result = self._ios_playground_run(device, app_config, prompt, action, launch_app)
            else:
                from .midscene_engine import MidsceneAppEngine
                engine = MidsceneAppEngine(app_config=app_config, device=device)
                result = engine.playground_run(prompt=prompt, action=action, launch_app=launch_app)
        except Exception as e:
            logger.warning('[Midscene Playground] 执行失败: %s', e)
            # 被人工终止时 Node 服务被杀，引擎侧会抛连接类异常。
            # 这时不能报"执行失败"（用户会以为是指令有问题），要说清是被终止。
            if _slot['aborted']:
                return Response({
                    'status': 'aborted',
                    'error': '已被人工终止执行',
                    'logs': ['[终止] 用户手动终止，已停止本次 Playground 执行'],
                })
            return Response(
                {'status': 'failed', 'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            PLAYGROUND_ACTIVE.pop(_key, None)

        # 正常返回路径同样要标 aborted（Node 被杀后引擎可能返回空结果而非抛异常）
        if _slot['aborted']:
            result['status'] = 'aborted'
            result['error'] = '已被人工终止执行'

        return Response(result)

    @action(detail=False, methods=['post'], url_path='midscene-playground/abort')
    def midscene_playground_abort(self, request):
        """终止正在执行的 Playground 指令。

        ⚠️ 能力边界（必须让用户知道）：
        midscene 的 Node 服务没有"取消当前动作"的接口，Python 线程又无法从外部
        打断一个卡在 HTTP 等待上的调用。所以这里只能**杀掉 Node 服务进程**来硬停。
        代价是：该引擎实例的会话一起失效，下次执行会自动重新拉起新进程（首次会慢几秒）。
        这不是优雅取消，是"拔电源"，但对"卡在 Replanned 40 times 干等 4 分钟"
        这种场景是唯一有效的手段。
        """
        key = _playground_key(request)
        active = PLAYGROUND_ACTIVE.get(key)
        if not active:
            return Response({
                'success': False,
                'message': '当前没有正在执行的 Playground 指令',
            }, status=status.HTTP_400_BAD_REQUEST)

        active['aborted'] = True
        killed = _playground_kill_node()
        logger.warning('[Playground] 用户终止执行 device=%s killed_node=%d',
                       active.get('device_id'), killed)
        return Response({
            'success': True,
            'message': ('已终止执行' if killed
                        else '已置终止标记（未找到运行中的 Node 服务，可能在下次检查点生效）'),
            'killed_node': killed,
        })

    @staticmethod
    def _ios_playground_run(device, app_config, prompt, action, launch_app):
        """iOS 真机 Playground：连远端 WDA 执行单条自然语言指令。

        与 Android 分支的区别只是引擎不同，返回值结构保持一致，
        前端（截图 / 日志 / query_result）无需区分平台。
        """
        from .midscene_ios_engine import MidsceneIOSEngine

        wda_host = (getattr(device, 'wda_host', '') or '').strip()
        wda_port = getattr(device, 'wda_port', None) or 8100
        if not wda_host:
            raise ValueError(
                '设备「%s」未配置 WDA 地址：请在「设备管理」里填写 WDA主机'
                '（手机USB所在电脑的 IP，如 192.168.8.71）' % device.name
            )

        bundle_id = (app_config.package_name if app_config else '') or ''
        engine = MidsceneIOSEngine(wda_host=wda_host, wda_port=wda_port)
        return engine.playground_run(
            prompt=prompt, action=action,
            bundle_id=bundle_id, launch_first=bool(launch_app),
        )

    @action(detail=False, methods=['post'], url_path='freeze-to-script')
    def freeze_to_script(self, request):
        """把「AI 探索成功」的执行结果固化成确定性脚本（Record & Replay）。

        背景：Midscene 每次执行都要调视觉模型（每步一次），又慢又费钱。
        但第一次跑通后，每一步的「目标元素在屏幕哪个位置」就已经知道了 ——
        把坐标固化下来，后续回放直接按坐标点，**完全不需要模型**。

        入参：
          execution_id  执行记录 id（必填），取其 steps_completed 里的 location
          test_case_id  可选，写入已有的 TestCase；不传则新建
          case_name     新建时的用例名（可选）

        产出：一条 TestCase（含若干 TestCaseStep，带 center_x/center_y）。
        之后用 test-case 的回放链路执行，不再调用任何视觉模型。

        注意：**只固化采到坐标的步骤**。没坐标的步骤（launch/back/wait 这类
        本来就不需要定位，或定位采样失败的）如实报告，不静默丢失。
        """
        from .models import TestCase, TestCaseStep

        data = request.data
        execution_id = data.get('execution_id')
        if not execution_id:
            return Response({'success': False, 'message': '请提供 execution_id'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            record = AIExecutionRecord.objects.get(id=execution_id)
        except AIExecutionRecord.DoesNotExist:
            return Response({'success': False, 'message': '执行记录不存在 id=%s' % execution_id},
                            status=status.HTTP_404_NOT_FOUND)

        steps_completed = record.steps_completed or []
        if not steps_completed:
            return Response(
                {'success': False, 'message': '该执行记录没有步骤，无法固化'},
                status=status.HTTP_400_BAD_REQUEST)

        ai_case = record.ai_case
        if ai_case is None:
            return Response({'success': False, 'message': '该执行记录未关联 AI 用例'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not ai_case.device:
            return Response(
                {'success': False,
                 'message': '该 AI 用例未关联执行设备，回放时无法确定目标机型'},
                status=status.HTTP_400_BAD_REQUEST)

        # 记录执行时的屏幕尺寸（用于换机型时按比例换算坐标）
        device = ai_case.device
        src_resolution = (device.resolution or '').strip()

        # ---- 逐条转换：AI 步骤 → TestCaseStep（带坐标）----
        ACTION_MAP = {
            'aiTap': 'tap', 'aiAct': 'tap', 'aiInput': 'input',
            'aiQuery': 'getText', 'aiAssert': 'assert',
            'launch': 'launch_app', 'back': 'back', 'home': 'home',
            'wait': 'wait', 'screenshot': 'screenshot',
        }
        converted, skipped = [], []
        for i, s in enumerate(steps_completed):
            if not isinstance(s, dict):
                continue
            ai_action = (s.get('action') or '').strip()
            loc = s.get('location') or {}
            center = loc.get('center') or []
            cx = cy = None
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                try:
                    cx, cy = int(center[0]), int(center[1])
                except (TypeError, ValueError):
                    cx = cy = None
            elif isinstance(center, dict):
                try:
                    cx, cy = int(center.get('x')), int(center.get('y'))
                except (TypeError, ValueError):
                    cx = cy = None

            target_action = ACTION_MAP.get(ai_action, 'tap')
            # 需要定位才能回放的动作，必须有坐标
            needs_location = ai_action in ('aiTap', 'aiAct', 'aiInput')
            if needs_location and (cx is None or cy is None):
                skipped.append({
                    'step': i + 1, 'action': ai_action,
                    'name': s.get('name') or '',
                    'reason': '未采集到坐标（首次执行时定位采样失败或被跳过）',
                })
                continue

            converted.append({
                'step_number': len(converted) + 1,
                'action_type': target_action,
                'center_x': cx,
                'center_y': cy,
                'input_value': (s.get('prompt') if ai_action == 'aiInput' else '') or '',
                'wait_time': int(s.get('wait_ms') or 1000),
                'description': s.get('name') or ('步骤%d' % (i + 1)),
                'assert_value': ((s.get('assert') or {}).get('reason') or '') or '',
            })

        if not converted:
            return Response({
                'success': False,
                'message': '没有任何步骤采集到坐标，无法固化。请先成功执行一次'
                           '（执行时会自动采集每步的目标坐标）',
                'skipped': skipped,
            }, status=status.HTTP_400_BAD_REQUEST)

        # ---- 落库 ----
        test_case_id = data.get('test_case_id')
        created_case = False
        if test_case_id:
            try:
                test_case = TestCase.objects.get(id=test_case_id)
            except TestCase.DoesNotExist:
                return Response({'success': False, 'message': '目标测试用例不存在'},
                                status=status.HTTP_404_NOT_FOUND)
        else:
            base_name = (data.get('case_name') or '').strip() \
                or ('%s（固化脚本）' % ai_case.name)
            # TestCase.project / created_by 都是 NOT NULL，必须兜住：
            # 先取 AI 用例的项目，再退到其应用配置的项目，最后取全局第一个 UI 项目。
            project = ai_case.project
            if project is None and ai_case.app_config is not None:
                project = getattr(ai_case.app_config, 'project', None)
            if project is None:
                project = UiProject.objects.order_by('id').first()
            if project is None:
                return Response(
                    {'success': False,
                     'message': '系统里还没有任何 UI 项目，无法创建测试用例，请先建一个项目'},
                    status=status.HTTP_400_BAD_REQUEST)

            creator = request.user if request.user.is_authenticated else None
            if creator is None:
                creator = ai_case.created_by or get_user_model().objects.filter(
                    is_active=True).order_by('id').first()
            if creator is None:
                return Response({'success': False, 'message': '找不到可用的创建人账号'},
                                status=status.HTTP_400_BAD_REQUEST)

            test_case = TestCase.objects.create(
                name=base_name,
                description='由 AI 探索执行固化而来（execution#%s），'
                            '按坐标回放，不再调用视觉模型' % record.id,
                project=project,
                status='ready',
                priority='medium',
                created_by=creator,
                tags=['frozen', 'ai-explored'],
            )
            created_case = True

        start_no = (TestCaseStep.objects.filter(test_case=test_case)
                    .order_by('-step_number').values_list('step_number', flat=True).first() or 0)
        objs = [
            TestCaseStep(test_case=test_case, **{**c, 'step_number': start_no + c['step_number']})
            for c in converted
        ]
        TestCaseStep.objects.bulk_create(objs)
        # 记录来源，便于审计与换机型换算
        try:
            tags = list(test_case.tags or [])
            for t in ('frozen', 'ai-explored'):
                if t not in tags:
                    tags.append(t)
            test_case.tags = tags
            test_case.save(update_fields=['tags', 'updated_at'])
        except Exception:  # noqa: BLE001
            pass

        logger.info('[固化脚本] execution#%s -> TestCase#%s，写入 %d 步，跳过 %d 步',
                    record.id, test_case.id, len(converted), len(skipped))
        return Response({
            'success': True,
            'message': '已固化 %d 个步骤到用例「%s」%s'
                       % (len(converted), test_case.name,
                          ('，%d 步因无坐标被跳过' % len(skipped)) if skipped else ''),
            'created_case': created_case,
            'test_case_id': test_case.id,
            'test_case_name': test_case.name,
            'frozen_steps': len(converted),
            'skipped': skipped,
            'source_resolution': src_resolution,
            'steps': converted,
        })

    @action(detail=False, methods=['post'], url_path='generate-steps')
    def generate_steps(self, request):
        """自然语言 → 结构化步骤（直接生成用例步骤，不必先建公共步骤）。

        入参：
          text        自然语言操作描述（必填，可多行）
          app_config  可选，被测应用配置 id（有则带上应用名做上下文）
          用例名/描述等其余信息由调用方自己保存，这里只负责产出 steps。

        返回 steps 数组，元素与编辑器同结构：
          {action, name, prompt, value, wait_ms, extract: [], assert: {prompt, expect}}

        设计取舍：
        - **只生成、不落库**：调用方拿到 steps 后在编辑器里预览/修改，确认再保存。
          避免模型幻觉直接污染用例数据。
        - **拆步而不是一整句**：每个原子操作独立一步，执行日志才能定位到具体哪步失败
          （把 7 个动作塞进一句 prompt 交给视觉模型自己拆，漏做/做错无法察觉）。
        - 模型不可用/返回不合法时**明确报错**，不静默返回空 steps。
        """
        data = request.data
        text = (data.get('text') or data.get('task_description') or '').strip()
        if not text:
            return Response({'success': False, 'message': '请填写自然语言操作描述'},
                            status=status.HTTP_400_BAD_REQUEST)

        app_name = ''
        app_config_id = data.get('app_config')
        if app_config_id:
            cfg = AppConfig.objects.filter(id=app_config_id).first()
            if cfg:
                app_name = cfg.name or ''

        try:
            steps = self._generate_steps_by_llm(text, app_name)
        except _StepsGenerationUnavailable as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[生成步骤] 失败: %s', exc)
            return Response({'success': False, 'message': '生成失败：%s' % exc},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'success': True,
            'message': '已生成 %d 个步骤，请预览确认后保存' % len(steps),
            'steps': steps,
            'raw_text': text,
        })

    def _generate_steps_by_llm(self, text, app_name=''):
        """调 LLM 把自然语言拆成结构化步骤，返回规范化后的 steps。"""
        import json as _json
        import re as _re

        from apps.requirement_analysis.models import AIModelConfig

        # 优先级：midscene_vision（懂真机操作）→ writer → analyzer
        config = None
        for role in ('midscene_vision', 'writer', 'analyzer'):
            config = AIModelConfig.objects.filter(role=role, is_active=True).first()
            if config:
                break
        if config is None:
            raise _StepsGenerationUnavailable(
                '未配置可用于生成步骤的模型：请在「模型配置」里启用 midscene_vision '
                '或 writer / analyzer 角色'
            )

        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=config.model_name,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=0.0,
        )

        app_hint = ('被测应用：%s\n' % app_name) if app_name else ''
        system = (
            '你是移动端 UI 自动化步骤拆解专家。把用户的自然语言操作描述，'
            '拆解成一条条**原子**操作步骤，供视觉驱动引擎逐步执行。\n\n'
            '硬性要求：\n'
            '1. 只输出 JSON 数组，不要任何解释、不要 markdown 代码块围栏。\n'
            '2. 数组每个元素结构：\n'
            '   {"action":"动作","name":"步骤简称","prompt":"给视觉模型的自然语言指令",'
            '"value":"","wait_ms":0}\n'
            '3. action 只能取：launch / aiTap / aiInput / aiAct / aiQuery / '
            'aiWaitFor / back / home / wait / screenshot\n'
            '   - launch：启动应用，prompt 写应用名\n'
            '   - aiTap：明确点击某个控件，prompt 写"点击XX"\n'
            '   - aiInput：输入文本，prompt 写"在XX输入框输入"，value 写要输入的内容\n'
            '   - aiAct：无法归入以上的复合操作\n'
            '   - aiQuery：只读取信息、不做操作，prompt 写要查询的内容\n'
            '   - aiWaitFor：需要**等待某个元素/页面出现**时用它，prompt 写"XX 出现"\n'
            '     （例如"首页的搜索框出现"）；比固定等待更省时间，优先用它而不是 wait\n'
            '   - back / home：返回、回主屏（prompt 留空）\n'
            '4. **一个步骤只做一件事**。遇到"点击A，然后点击B，再进入C"，必须拆成 3 个步骤，'
            '禁止把多个动作塞进同一句 prompt（否则失败时无法定位）。\n'
            '5. prompt 要用**界面上的可见文字**描述目标，例如"点击底部「天气」标签"、'
            '"点击左上角的「+」按钮"，不要写坐标（如 (100,200)）。\n'
            '6. name 用 4-10 个字概括该步，方便在报告里辨认。\n'
            '7. 用户描述里没提到的中间跳转，不要凭空编造步骤。\n'
        )
        user = (
            '%s请拆解下面这段操作描述：\n"""\n%s\n"""\n\n'
            '直接输出 JSON 数组。' % (app_hint, text)
        )

        resp = llm.invoke([
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ])
        content = getattr(resp, 'content', '') or ''
        if isinstance(content, list):
            content = ''.join(
                c.get('text', '') if isinstance(c, dict) else str(c) for c in content
            )

        # 去掉可能存在的 ```json 围栏
        content = _re.sub(r'^\s*```(?:json)?|```\s*$', '', content.strip(), flags=_re.M).strip()
        match = _re.search(r'\[.*\]', content, _re.DOTALL)
        if not match:
            raise ValueError('模型没有返回可解析的步骤数组，原始输出：%s' % content[:200])

        try:
            parsed = _json.loads(match.group(0))
        except _json.JSONDecodeError as exc:
            raise ValueError('步骤 JSON 解析失败：%s' % exc) from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError('模型返回的步骤为空')

        allowed = {'launch', 'aiTap', 'aiInput', 'aiAct', 'aiQuery',
                   'aiWaitFor', 'back', 'home', 'wait', 'screenshot', 'assert'}
        steps = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                continue
            action = str(item.get('action') or '').strip()
            if action not in allowed:
                action = 'aiAct'
            prompt = str(item.get('prompt') or '').strip()
            value = str(item.get('value') or '').strip()
            # 需要 prompt 的动作没给 prompt 就丢掉，避免产出"空步骤"
            if action in ('launch', 'aiTap', 'aiInput', 'aiAct', 'aiQuery',
                          'aiWaitFor') and not prompt:
                continue
            try:
                wait_ms = int(item.get('wait_ms') or 0)
            except (TypeError, ValueError):
                wait_ms = 0
            try:
                timeout_ms = int(item.get('timeout_ms') or 0)
            except (TypeError, ValueError):
                timeout_ms = 0
            steps.append({
                'action': action,
                'name': str(item.get('name') or '').strip() or ('步骤 %d' % (i + 1)),
                'prompt': prompt,
                'value': value,
                'wait_ms': max(0, wait_ms),
                # aiWaitFor 的等待上限；0 = 用引擎默认值
                'timeout_ms': max(0, timeout_ms),
                'extract': [],
                'assert': {'prompt': '', 'expect': ''},
            })

        if not steps:
            raise ValueError('模型返回的步骤都不合法（缺少 action 或 prompt）')
        return steps

    @action(detail=False, methods=['post'], url_path='midscene-save-step')
    def midscene_save_step(self, request):
        """
        Midscene 步骤沉淀（B 方案）：把 Playground 验证过的自然语言步骤，
        直接 append 进指定 AICase 的 steps（结构化固化），后续走 _run_midscene 重放免重复试错。

        入参：
          ai_case_id  目标用例（不传则新建一条）
          action/prompt/selector_hint/value/name/wait_ms/extract/assert 步骤字段

        批量：传 `steps: [...]` 可一次性追加多条（AI 拆步后整体写入），
        比前端循环调用更安全 —— 中途失败不会写出半截用例。
        """
        data = request.data
        ai_case_id = data.get('ai_case_id')

        # ---- 组装待写入的步骤列表：批量优先，否则按单条处理 ----
        batch = data.get('steps')
        if isinstance(batch, list) and batch:
            incoming = batch
        else:
            incoming = [{
                'action': data.get('action') or 'aiAct',
                'prompt': data.get('prompt'),
                'name': data.get('name'),
                'value': data.get('value'),
                'selector_hint': data.get('selector_hint'),
                # 手动从元素库选定的元素 id（引擎不消费，仅记录来源）
                'locate_element_id': data.get('locate_element_id'),
                # 精确定位（仅 iOS）：走 WDA 元素树按文本取坐标
                'locate_by_text': data.get('locate_by_text'),
                'locate_region': data.get('locate_region'),
                'locate_index': data.get('locate_index'),
                'wait_ms': data.get('wait_ms'),
                'extract': data.get('extract'),
                'assert': data.get('assert'),
            }]

        allowed_actions = ('aiAct', 'aiTap', 'aiInput', 'aiQuery', 'assert', 'wait',
                           'launch', 'back', 'home', 'screenshot', 'aiWaitFor')

        # 这些动作天然不需要 prompt（返回/回主屏/等待/截图），
        # 强制要求 prompt 会把 AI 拆出来的 back 步骤全部拦下来。
        # 注意 aiWaitFor 不在其中：它必须有 prompt（要等什么出现）。
        PROMPTLESS_ACTIONS = ('back', 'home', 'wait', 'screenshot')

        prepared = []
        for raw in incoming:
            if not isinstance(raw, dict):
                continue
            prompt = (raw.get('prompt') or '').strip()
            action = raw.get('action') or 'aiAct'
            if action not in allowed_actions:
                return Response(
                    {'error': 'action 不支持: %s' % action},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if not prompt and action not in PROMPTLESS_ACTIONS:
                return Response(
                    {'error': 'prompt（自然语言操作指令）不能为空'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            prepared.append((action, prompt, raw))

        if not prepared:
            return Response(
                {'error': '没有可写入的步骤'},
                status=status.HTTP_400_BAD_REQUEST
            )

        created_case = False
        if ai_case_id:
            try:
                ai_case = AICase.objects.get(id=ai_case_id)
            except AICase.DoesNotExist:
                return Response(
                    {'error': 'AI 用例不存在 id=%s' % ai_case_id},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:
            # 未指定用例 → 现场新建一条独立用例。
            # 场景：Playground 试出一个全新的操作，还没有任何用例承载它。
            # 设备/应用配置直接沿用 Playground 当前选择，省得再选一遍。
            new_name = (data.get('new_case_name') or '').strip()
            if not new_name:
                new_name = 'Playground 用例 %s' % timezone.now().strftime('%m-%d %H:%M')
            ai_case = AICase.objects.create(
                name=new_name,
                project_id=data.get('project_id') or None,
                app_config_id=data.get('app_config_id') or None,
                device_id=data.get('device_id') or None,
                execution_mode='midscene',
                steps=[],
                # 必须显式给值：老环境（如生产）该列是 NOT NULL（初始迁移 0002 建表时是
                # TextField(blank=True, default='')），而当前模型是 null=True，
                # 不传时 Django 会显式写 NULL，直接报
                # (1048, "Column 'task_description' cannot be null") → 保存步骤 500。
                task_description='',
                created_by=request.user if request.user.is_authenticated else None,
            )
            created_case = True

        # 沉淀到 midscene 用例：若用例仍是 text 模式，自动切换为 midscene（steps 才有意义）
        if ai_case.execution_mode != 'midscene':
            ai_case.execution_mode = 'midscene'

        existing = ai_case.steps or []
        new_steps = []
        for action, prompt, raw in prepared:
            step = {
                'name': (raw.get('name') or '').strip()
                        or '步骤%d' % (len(existing) + len(new_steps) + 1),
                'action': action,
                'prompt': prompt,
                'selector_hint': (raw.get('selector_hint') or '').strip(),
                # 手动从元素库选定的元素 id（引擎不消费，仅记录来源）。
                # 必须显式透传，否则用户"重新打开用例"时选择丢失、又要重挑一遍。
                'locate_element_id': raw.get('locate_element_id') or None,
                # 精确定位（仅 iOS）：按下标/区域在元素树里消歧同名元素。
                # 同样必须显式透传 —— 这里是逐字段重建 step 字典。
                'locate_by_text': bool(raw.get('locate_by_text')),
                'locate_region': (raw.get('locate_region') or '').strip(),
                'locate_index': int(raw.get('locate_index') or 0),
                'value': (raw.get('value') or '').strip(),
                'wait_ms': int(raw.get('wait_ms') or 0),
                # aiWaitFor 的等待上限（0 = 引擎默认 15 秒）。
                # 这里必须显式透传：本方法是在重建 step 字典（不是直接把
                # raw 存库），漏一个字段就会被静默丢掉 —— 前端填了超时值
                # 却存成默认值，用户会以为设置没生效。
                'timeout_ms': int(raw.get('timeout_ms') or 0),
            }
            extract = raw.get('extract')
            if extract:
                step['extract'] = extract
            assert_cfg = raw.get('assert')
            if assert_cfg:
                step['assert'] = assert_cfg
            new_steps.append(step)

        ai_case.steps = existing + new_steps
        ai_case.save(update_fields=['steps', 'execution_mode', 'updated_at'])

        logger.info('[Midscene] %d 条步骤已沉淀到 AICase#%s，当前 steps 数=%d',
                    len(new_steps), ai_case.id, len(ai_case.steps))
        if created_case:
            message = '已新建用例「%s」并写入 %d 条步骤' % (ai_case.name, len(new_steps))
        else:
            message = '已追加 %d 条步骤到用例「%s」' % (len(new_steps), ai_case.name)
        return Response({
            'message': message,
            'ai_case_id': ai_case.id,
            'ai_case_name': ai_case.name,
            'created_case': created_case,
            'added_count': len(new_steps),
            'steps_count': len(ai_case.steps),
            'steps': ai_case.steps,
        })

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """执行 AI 用例"""
        ai_case = self.get_object()

        # Midscene APP 自动化模式：走结构化 steps + 真机引擎
        if ai_case.execution_mode == 'midscene':
            return self._run_midscene(request, ai_case)

        # Midscene Web 自动化模式：走结构化 steps + Playwright 浏览器引擎
        if ai_case.execution_mode == 'midscene_web':
            return self._run_midscene_web(request, ai_case)

        # 创建执行记录
        execution_record = AIExecutionRecord.objects.create(
            project=ai_case.project,
            ai_case=ai_case,
            case_name=ai_case.name,
            task_description=ai_case.task_description,
            status='running',
            executed_by=request.user,
            logs="正在分析任务...\n"
        )
        
        # 异步执行
        import threading
        from asgiref.sync import sync_to_async
        from .ai_agent import run_full_process_sync
        
        def run_task():
            # 注册停止信号
            STOP_SIGNALS[execution_record.id] = False

            try:
                def should_stop():
                    return STOP_SIGNALS.get(execution_record.id, False)

                async def on_analysis_complete(planned_tasks):
                    execution_record.planned_tasks = planned_tasks
                    execution_record.logs += "任务分析完成，开始执行...\n"
                    await sync_to_async(execution_record.save)()
                    
                async def on_step_update(step_info):
                    try:
                        # 处理日志
                        if step_info.get('type') == 'log':
                            content = step_info.get('content')
                            if content:
                                execution_record.logs += content
                                await sync_to_async(execution_record.save)()
                            return

                        # 处理任务状态
                        task_id = step_info.get('task_id')
                        status = step_info.get('status')
                        if task_id and status:
                            updated = False
                            for task in execution_record.planned_tasks:
                                if task['id'] == task_id:
                                    task['status'] = status
                                    updated = True
                                    break
                            if updated:
                                await sync_to_async(execution_record.save)()
                    except Exception as e:
                        print(f"更新步骤状态失败: {e}")

                history = run_full_process_sync(
                    ai_case.task_description, 
                    analysis_callback=on_analysis_complete, 
                    step_callback=on_step_update,
                    should_stop=should_stop
                )
                
                # 检查是否是手动停止
                if should_stop():
                    execution_record.status = 'stopped'
                    execution_record.logs += "\n[System] 任务已由用户停止。"
                else:
                    # 更新成功状态
                    execution_record.status = 'passed'
                    execution_record.logs += "\n执行完成。"

                    # 记录任务完成统计信息
                    if execution_record.planned_tasks:
                        total_tasks = len(execution_record.planned_tasks)
                        completed_tasks = len([t for t in execution_record.planned_tasks if t.get('status') == 'completed'])
                        pending_tasks = len([t for t in execution_record.planned_tasks if t.get('status') == 'pending'])
                        logger.info(f"🏁 Task completion summary: {completed_tasks}/{total_tasks} tasks completed, {pending_tasks} pending")
                
                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                
                # 格式化 history 为日志 (如果不是停止状态)
                steps = []
                if history:
                    if hasattr(history, 'steps'):
                        steps = [extract_step_info(s, i) for i, s in enumerate(history.steps)]

                execution_record.steps_completed = steps

                # 自动标记已完成的任务
                if execution_record.planned_tasks:
                    self._auto_mark_completed_tasks(execution_record)

                # 处理GIF录制文件
                self._process_gif_recording(execution_record, history)

                execution_record.save()

            except Exception as e:
                execution_record.status = 'failed'
                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                execution_record.logs += f"\n执行出错: {str(e)}"
                execution_record.save()
            finally:
                # 清理停止信号
                if execution_record.id in STOP_SIGNALS:
                    del STOP_SIGNALS[execution_record.id]

        thread = threading.Thread(target=run_task)
        thread.daemon = True
        thread.start()
        
        return Response({
            'message': 'AI 用例开始执行',
            'execution_id': execution_record.id
        })

    @action(detail=False, methods=['post'], url_path='midscene-web-playground')
    def midscene_web_playground(self, request):
        """
        Midscene Web Playground: 自然语言单条调试（连 Playwright 浏览器执行并返回截图 base64）。
        """
        data = request.data
        url = (data.get('url') or '').strip()
        prompt = (data.get('prompt') or '').strip()
        action = data.get('action') or 'aiAct'
        value = data.get('value') or ''
        selector_hint = data.get('selector_hint') or ''

        if not url:
            return Response({'error': 'url (目标网址) 不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        if not prompt:
            return Response({'error': 'prompt (自然语言操作指令) 不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        if action not in ('aiAct', 'aiTap', 'aiInput', 'aiQuery'):
            return Response({'error': 'action 仅支持 aiAct/aiTap/aiInput/aiQuery'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取模型配置（真源：AIModelConfig role=midscene_vision）
        from .midscene_web_engine import MidsceneWebEngine, load_midscene_model_config
        model_config = load_midscene_model_config()
        if not model_config.get('api_key'):
            return Response(
                {'error': '未配置 Midscene 视觉模型：请在「AI 模型配置」中新增 role=midscene_vision '
                          '且已启用的多模态模型（需含 api_key / base_url / model_name）。'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            engine = MidsceneWebEngine(model_config=model_config, headless=True)
            result = engine.playground_run(url=url, prompt=prompt, action=action, value=value, selector_hint=selector_hint)
        except Exception as e:
            logger.warning('[MidsceneWeb Playground] 执行失败: %s', e)
            return Response({'status': 'failed', 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(result)

    @action(detail=False, methods=['post'], url_path='midscene-web-save-step')
    def midscene_web_save_step(self, request):
        """
        Midscene Web 步骤沉淀: 把 Playground 验证过的自然语言步骤 append 进 AICase 的 steps。
        """
        data = request.data
        ai_case_id = data.get('ai_case_id')
        prompt = (data.get('prompt') or '').strip()
        action = data.get('action') or 'aiAct'

        if not ai_case_id:
            return Response({'error': 'ai_case_id 必填'}, status=status.HTTP_400_BAD_REQUEST)
        if not prompt:
            return Response({'error': 'prompt 不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        if action not in ('aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiAssert', 'assert', 'wait', 'navigate', 'screenshot'):
            return Response({'error': 'action 不支持 %s' % action}, status=status.HTTP_400_BAD_REQUEST)

        try:
            ai_case = AICase.objects.get(id=ai_case_id)
        except AICase.DoesNotExist:
            return Response({'error': 'AI 用例不存在'}, status=status.HTTP_404_NOT_FOUND)

        if ai_case.execution_mode not in ('midscene_web', 'text'):
            return Response({'error': '目标用例已绑定 APP 模式，不能沉淀 Web 步骤'}, status=status.HTTP_400_BAD_REQUEST)

        if ai_case.execution_mode != 'midscene_web':
            ai_case.execution_mode = 'midscene_web'

        step = {
            'name': (data.get('name') or '').strip() or '步骤%d' % (len(ai_case.steps) + 1),
            'action': action,
            'prompt': prompt,
            'selector_hint': data.get('selector_hint', ''),
            'value': data.get('value', ''),
            'wait_ms': data.get('wait_ms', 0),
            'extract': data.get('extract', []),
            'assert': data.get('assert', {}),
        }
        ai_case.steps = (ai_case.steps or []) + [step]
        ai_case.save(update_fields=['steps', 'execution_mode', 'updated_at'])
        logger.info('[MidsceneWeb] 步骤已沉淀到 AICase#%s, 当前 steps 数 %d', ai_case.id, len(ai_case.steps))
        return Response({
            'message': '步骤已追加到用例「%s」' % ai_case.name,
            'ai_case_id': ai_case.id,
            'steps_count': len(ai_case.steps),
        })

    def _run_midscene_web(self, request, ai_case):
        """Midscene Web 自动化执行分支: 读取结构化 steps, 驱动 Playwright 浏览器执行"""
        if not ai_case.steps:
            return Response({'error': '该 Midscene Web 用例没有步骤 (steps) 配置'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取模型配置（真源：AIModelConfig role=midscene_vision）
        from .midscene_web_engine import load_midscene_model_config
        model_config = load_midscene_model_config()
        if not model_config.get('api_key'):
            return Response(
                {'error': '未配置 Midscene 视觉模型：请在「AI 模型配置」中新增 role=midscene_vision '
                          '且已启用的多模态模型（需含 api_key / base_url / model_name）。'},
                status=status.HTTP_400_BAD_REQUEST)

        execution_record = AIExecutionRecord.objects.create(
            project=ai_case.project,
            ai_case=ai_case,
            case_name=ai_case.name,
            task_description=ai_case.task_description or '(Midscene Web 结构化步骤)',
            execution_mode='midscene_web',
            status='running',
            executed_by=request.user,
            logs='[MidsceneWeb] 正在启动 Playwright 浏览器...\n'
        )

        def run_task():
            STOP_SIGNALS[execution_record.id] = False
            try:
                from .midscene_web_engine import MidsceneWebEngine
                engine = MidsceneWebEngine(model_config=model_config, headless=True)
                result = engine.execute_steps(ai_case.steps)

                logs = list(engine.logs)
                logs.append('\n[MidsceneWeb] 执行结束: %s' % result.get('status', 'unknown'))
                if result.get('error'):
                    logs.append('错误: %s' % result['error'])

                context_dump = result.get('context', {})
                if context_dump:
                    logs.append('\n[MidsceneWeb] 业务上下文 (extract 结果):')
                    for k, v in context_dump.items():
                        logs.append('  ${{%s}} = %s' % (k, v))

                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                execution_record.logs += '\n'.join(logs)
                execution_record.status = 'completed' if result.get('status') == 'success' else 'failed'
                execution_record.error_message = result.get('error', '')

                if execution_record.planned_tasks:
                    self._auto_mark_completed_tasks(execution_record)

                execution_record.save()
            except Exception as e:
                execution_record.status = 'failed'
                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                execution_record.logs += '\n[MidsceneWeb] 执行出错: %s' % str(e)
                execution_record.error_message = str(e)
                execution_record.save()
                logger.warning('[MidsceneWeb] 执行失败: %s', e)
            finally:
                STOP_SIGNALS.pop(execution_record.id, None)

        import threading
        thread = threading.Thread(target=run_task)
        thread.daemon = True
        thread.start()

        return Response({
            'message': 'Midscene Web 用例开始执行',
            'execution_id': execution_record.id
        })

    def _execute_midscene_case(self, ai_case, device, force_ai=False):
        """执行一次用例，返回 (engine, result)。

        是否走纯坐标回放由「用例是否已固化」决定：
          * ai_case.steps_frozen_at 非空且步骤带坐标 -> 回放模式（不调视觉模型）
          * 其余情况                                 -> AI 探索模式
        force_ai=True 用于"回放失败后降级重跑"，强制走 AI 模式。
        """
        is_ios = (device.platform or '').strip().lower() == 'ios'
        steps = ai_case.steps or []

        want_replay = bool(
            not force_ai
            and getattr(ai_case, 'steps_frozen_at', None)
            and steps
            and all(
                (not isinstance(s, dict))
                or (s.get('action') not in ('aiTap', 'aiAct', 'aiInput'))
                or (s.get('center_x') is not None and s.get('center_y') is not None)
                for s in steps
            )
        )

        if is_ios:
            engine, result = self._run_ios_engine(
                ai_case, device, replay_mode=want_replay)
            self._log_replay_decision(engine, ai_case, want_replay, force_ai)
            # ⚠️ 必须**在** _log_replay_decision 之后刷新快照：
            #    _run_ios_engine 里 `result['engine_logs']` 是那一刻的拷贝，
            #    之后 append 进 engine.logs 的行不会自动进快照 ——
            #    结果就是「[固化] 本次走回放/AI」那行**永远不出现在执行日志里**
            #    （2026-09-22 实测：日志里一行 [固化] 都没有，
            #     害得排查时完全看不出这次到底走了哪条路）。
            result['engine_logs'] = list(engine.logs)
            return engine, result

        from .midscene_engine import MidsceneAppEngine
        engine = MidsceneAppEngine(
            app_config=ai_case.app_config,
            device=device,
            # 用例级开关：默认自动关弹窗，专门测弹窗的用例可关掉
            auto_dismiss_popup=getattr(ai_case, 'auto_dismiss_popup', None),
            replay_mode=want_replay,
        )
        engine.connect()
        result = engine.run(steps)
        self._log_replay_decision(engine, ai_case, want_replay, force_ai)
        return engine, result

    @staticmethod
    def _log_replay_decision(engine, ai_case, want_replay, force_ai=False):
        """在执行日志开头写明"这次走的是回放还是 AI 探索，以及依据"。

        为什么需要它（2026-09-22 实际踩到的坑）：
          固化是在**上一次执行结束、结果落库之后**才写入的
          （`_freeze_case_steps` 在 run_task 尾段调用）。
          用户看到「成功」立刻点「重跑」，很可能**赶在固化写库之前** ——
          于是那次仍是 AI 探索（12 次调用），用户看到的就是
          "我明明跑通了，为什么重跑还是烧 token"，而且**日志里没有任何线索**，
          只能去比对 `steps_frozen_at` 时间戳才能发现。

        所以这里把判定依据直接打出来：已固化就报固化时间，
        未固化就**说明原因**（没固化 / 缺坐标 / 强制 AI），
        一眼就能分清"固化没生效"和"还没轮到固化"。
        """
        try:
            if want_replay:
                frozen = getattr(ai_case, 'steps_frozen_at', None)
                engine.logs.append(
                    '[固化] 本用例已固化 %s → 本次走**纯坐标回放**（定位类步骤不调用视觉模型）'
                    % (frozen.strftime('%Y-%m-%d %H:%M:%S') if frozen else '?'))
                return

            if force_ai:
                engine.logs.append(
                    '[固化] 回放失败降级 → 本次强制走 AI 探索模式（跑通后会重新固化）')
                return

            steps = ai_case.steps or []
            need = ('aiTap', 'aiAct', 'aiInput')
            missing = [
                i + 1 for i, s in enumerate(steps)
                if isinstance(s, dict) and s.get('action') in need
                and (s.get('center_x') is None or s.get('center_y') is None)
            ]
            if not getattr(ai_case, 'steps_frozen_at', None):
                engine.logs.append(
                    '[固化] 本用例**尚未固化** → 本次走 AI 探索模式，'
                    '跑通后会自动固化为可复用脚本（下次执行起走纯坐标回放）')
            elif missing:
                engine.logs.append(
                    '[固化] 用例已固化但第 %s 步缺坐标 → 本次退回 AI 探索模式'
                    % '、'.join(str(m) for m in missing))
            else:
                engine.logs.append(
                    '[固化] 判定异常：未走回放但条件似乎满足，请检查日志')
        except Exception as exc:  # noqa: BLE001
            logger.warning('[固化] 写回放判定日志失败（不影响执行）: %s', exc)

    def _freeze_case_steps(self, ai_case, executed_steps, logs):
        """把本次执行采到的坐标写回用例，并打上固化时间戳。

        只固化"确实需要定位"的动作（aiTap/aiAct/aiInput）。launch/back/wait
        这类本来就不需要坐标，如实跳过即可。

        只要有一个需要定位的步骤没采到坐标，就整体不固化 —— 半固化的用例
        回放时会在那一步点空，反而比不固化更糟（等于把失败固化了下来）。
        """
        if not executed_steps:
            logs.append('[固化] 本次执行没有步骤结果，跳过固化')
            return

        need = ('aiTap', 'aiAct', 'aiInput')
        patched = []
        missing = []
        for idx, s in enumerate(executed_steps):
            if not isinstance(s, dict):
                continue
            if s.get('action') not in need:
                continue
            loc = s.get('location') or {}
            center = loc.get('center') or []
            cx = cy = None
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                cx, cy = center[0], center[1]
            elif isinstance(center, dict):
                cx, cy = center.get('x'), center.get('y')
            if cx is None or cy is None:
                missing.append(idx + 1)
            else:
                patched.append((idx, int(cx), int(cy)))

        if missing:
            # ⚠️ 这里必须把**真实原因**说清楚。旧文案只说"没采到坐标"，
            #    用户会以为"这次运气不好，再跑一次就好了"——
            #    但如果原因是"引擎没有采坐标的能力"，重跑多少次都不会成功，
            #    白烧 token 还以为是偶发问题。
            logs.append(
                '[固化] 第 %s 步没有采到坐标，本次不固化（避免固化出会点空的半成品）\n'
                '        说明：坐标来自"定位采样"。精确元素定位（元素树）命中的步骤天然有坐标；\n'
                '        走 AI 视觉定位的步骤需要引擎调用 aiLocate 采样 —— 若该能力不可用\n'
                '        （或采样失败），这些步骤就永远没有坐标，**重复执行也不会改善**。\n'
                '        请检查执行日志里是否有"已采样坐标"记录；若无，说明本次运行的引擎\n'
                '        版本未启用 aiLocate 采样。'
                % '、'.join(str(m) for m in missing))
            return

        if not patched:
            logs.append('[固化] 本次没有任何"需要定位"的步骤，无需固化')
            return

        # 按原用例的步骤顺序回填坐标（executed_steps 与 ai_case.steps 同序）。
        # 这里显式复制每个 dict：list() 只是浅拷贝，直接改元素会连带改到
        # 调用方传进来的那份 JSON，出现"没保存却已经变了"的诡异现象。
        steps = []
        for s in (ai_case.steps or []):
            steps.append(dict(s) if isinstance(s, dict) else s)

        patched_n = 0
        for idx, cx, cy in patched:
            if 0 <= idx < len(steps) and isinstance(steps[idx], dict):
                steps[idx]['center_x'] = cx
                steps[idx]['center_y'] = cy
                patched_n += 1

        ai_case.steps = steps
        ai_case.steps_frozen_at = timezone.now()
        ai_case.save(update_fields=['steps', 'steps_frozen_at', 'updated_at'])
        logs.append('[固化] 已固化 %d 步坐标，下次执行将走纯坐标回放（不调用视觉模型）'
                    % patched_n)

    @staticmethod
    def _dismiss_popups_safely(engine, label=''):
        """清一轮弹窗，任何异常都不能影响用例执行。

        应用冷启动后最容易弹权限请求 / 隐私协议 / 开屏广告，这时候提前清掉
        能省掉第一个步骤的一次失败重试。步骤级别的失败重试仍然是主要保障
        （见 midscene_engine / midscene_ios_engine 的 execute 循环）。
        """
        try:
            from .popup_handler import dismiss_popups
            adapter = engine._popup_adapter() if hasattr(engine, '_popup_adapter') else None
            if adapter is None:
                return
            n = dismiss_popups(adapter)
            if n:
                logger.info('[Midscene%s] 启动后清掉了 %d 个弹窗', label, n)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[Midscene%s] 启动后弹窗清理失败（不影响执行）: %s', label, exc)

    def _run_ios_engine(self, ai_case, device, replay_mode=False):
        """iOS 真机执行分支：连远端 WebDriverAgent 跑结构化 steps。

        WDA 服务跑在「插着手机的电脑」上，地址取自设备的 wda_host / wda_port
        （与 Android 的 adb_host 对称）。返回 (engine, result)，result 结构与
        MidsceneAppEngine.run() 对齐，便于 _run_midscene 统一消费。
        """
        from .midscene_ios_engine import MidsceneIOSEngine

        wda_host = (getattr(device, 'wda_host', '') or '').strip()
        wda_port = getattr(device, 'wda_port', None) or 8100
        if not wda_host:
            raise ValueError(
                '设备「%s」未配置 WDA 地址：请在「设备管理」中填写 WDA主机'
                '（手机USB所在电脑的 IP，如 192.168.8.71）' % device.name
            )

        engine = MidsceneIOSEngine(
            wda_host=wda_host,
            wda_port=wda_port,
            # 用例绑定的被测应用。步骤里写「launch: 万年历」这种人类可读名字时，
            # WDA 只认 bundle id 会返回 400，这里用它的 bundle id 兜底。
            default_bundle_id=(ai_case.app_config.package_name if ai_case.app_config else '') or '',
            # 用例级开关：默认自动关弹窗，专门测弹窗的用例可关掉
            auto_dismiss_popup=getattr(ai_case, 'auto_dismiss_popup', None),
            # 用例已固化 -> 走纯坐标回放（不调视觉模型）
            replay_mode=replay_mode,
        )

        # iOS 的 execute_steps 是「直执行」，不认 {ref: 公共步骤名}。
        # 引用必须在进引擎前展开成完整 step（Android 侧由
        # MidsceneAppEngine.run 内部调 _resolve_business_refs 完成，
        # iOS 这条路原来漏了 —— 结果就是选了公共步骤的用例
        # 把 {'ref': 'xxx'} 当成一个空步骤执行，报「没有填写提示词 prompt」）。
        steps = ai_case.steps or []
        try:
            from .midscene_engine import MidsceneAppEngine
            resolver = MidsceneAppEngine.__new__(MidsceneAppEngine)
            resolver.logs = []
            steps = resolver._resolve_business_refs(steps)
            for line in resolver.logs:
                engine.logs.append(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[Midscene-iOS] 展开公共步骤引用失败: %s', exc)

        engine.start()
        try:
            bundle_id = (ai_case.app_config.package_name if ai_case.app_config else '') or ''
            if bundle_id:
                # ⭐⭐ **探索与回放都先复位到同一起点**（2026-09-22 实测定型）。
                #
                # 为什么要复位：固化采到的是**绝对坐标**，隐含假设
                #   「本次起点页面 == 固化采样时的起点页面」。
                #   若探索从"任意残留状态"采样、回放从另一起点开始，两边页面不同
                #   → 坐标整体偏掉 → 回放必失败 → 降级烧 token
                #   → 用户误判"固化从没生效、永远省不了 token"。
                #
                # ⚠️ 复位**只重新拉起 App，不 home/不杀进程**（实测坑）：
                #   最初实现是 `home` + 冷启动，以为能拿到"最干净首启状态"，
                #   但该 App 冷启动会落到一个**既无主导航、也无返回按钮的死路页**
                #   （iOS「日记本」页：`back` 两次元素树完全不变），
                #   首步固化坐标 (40,794) 在此页落到「前一天」按钮 → 连锁失败。
                #   **杀进程同样会落到那个死路页，所以也不做。**
                #   起点一致性改由用例自身保证（用例应从期望的页面开始）。
                engine.reset_to_home(bundle_id)
                # 冷启动是最容易弹权限/协议/开屏广告的时刻，先清一轮再开始执行，
                # 免得第一个步骤就被弹窗挡住（步骤级已有失败重试兜底，这里只是
                # 提前把最常见的情况处理掉）。
                self._dismiss_popups_safely(engine, 'iOS')
            raw = engine.execute_steps(steps)
        finally:
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                pass

        result = {
            'status': 'passed' if raw.get('status') == 'success' else 'failed',
            'steps': raw.get('steps') or [],
            'error': raw.get('error') or '',
            'context': {},
        }
        # 把引擎日志挂到 result 上。
        #
        # ⚠️ 为什么必须挂（2026-09-22 踩到）：
        #   `_run_midscene` 里的回放降级分支会 `engine = engine2` **换掉** engine 对象，
        #   然后 `logs = list(engine.logs)` 取的是**新引擎**的日志。
        #   iOS 路径里「步骤 1 精确定位命中、步骤 2 失败」这类关键行都只在
        #   **第一个** engine.logs 里 —— 不挂过来就会**整段丢失**，
        #   用户看到的故障日志直接从「已启动」跳到「[步骤 2] 失败」，
        #   完全无法判断第 1 步到底做了什么（这正是排查卡住的原因）。
        result['engine_logs'] = list(engine.logs)
        return engine, result

    def _run_midscene(self, request, ai_case):
        """Midscene APP 自动化执行分支：读取结构化 steps，驱动真机执行

        设备解析规则：
          * 请求体里带了 device_id  -> 用请求指定的设备（执行时临时覆盖，
                                      不会改写用例本身的 device 配置）
          * 没带                    -> 用用例绑定的 device（向后兼容）
        无论哪种来源，都必须是「在线」设备。
        """
        if not ai_case.steps:
            return Response(
                {'error': '该 Midscene 用例没有步骤（steps）配置'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not ai_case.app_config:
            return Response(
                {'error': 'Midscene 用例必须关联应用配置(app_config)'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ---- 解析本次执行使用的设备（支持多选，最多 MAX_PARALLEL_DEVICES 台） ----
        #
        # 请求体格式（两种都支持）：
        #   {"device_id": 12}                单台（向后兼容）
        #   {"device_ids": [12, 15]}         多台，同一用例并行跑
        #
        # 多台的前提是这些设备 WDA 地址各不相同。同一台电脑的 8100 同一时间
        # 只能转发一台真机，所以同地址的两台设备并行跑必然互相干扰，这里直接拦掉。
        from .models import AppDevice

        raw_ids = None
        try:
            data = request.data or {}
            if data.get('device_ids'):
                raw_ids = data.get('device_ids')
                if isinstance(raw_ids, str):
                    raw_ids = [x.strip() for x in raw_ids.split(',') if x.strip()]
            elif data.get('device_id'):
                raw_ids = [data.get('device_id')]
        except Exception:  # noqa: BLE001
            raw_ids = None

        override_device_ids = []
        if raw_ids:
            # 去重并保持顺序，顺手过滤掉非法值
            for rid in raw_ids:
                try:
                    rid = int(rid)
                except (TypeError, ValueError):
                    continue
                if rid not in override_device_ids:
                    override_device_ids.append(rid)

        if override_device_ids:
            if len(override_device_ids) > MAX_PARALLEL_DEVICES:
                return Response(
                    {
                        'error': '一次最多同时执行 %d 台设备（当前选中 %d 台）。'
                                 '请减少选择或分批执行。'
                                 % (MAX_PARALLEL_DEVICES, len(override_device_ids)),
                        'max_parallel': MAX_PARALLEL_DEVICES,
                        'selected': len(override_device_ids),
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            found = {d.id: d for d in AppDevice.objects.filter(id__in=override_device_ids)}
            run_devices = []
            missing = []
            for rid in override_device_ids:
                if rid in found:
                    run_devices.append(found[rid])
                else:
                    missing.append(rid)
            if missing:
                return Response(
                    {'error': '以下执行设备不存在：%s' % ', '.join(str(m) for m in missing)},
                    status=status.HTTP_400_BAD_REQUEST
                )
        elif ai_case.device:
            run_devices = [ai_case.device]
        else:
            run_devices = []

        if not run_devices:
            return Response(
                {'error': '未指定执行设备，请在选择设备后重试'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ---- 同一 WDA 地址只能有一台设备（并行会互相踩） ----
        addr_seen = {}
        for _d in run_devices:
            if (_d.platform or '').lower() == 'ios':
                _addr = ((_d.wda_host or '').strip(), _d.wda_port or 8100)
            else:
                _addr = ((_d.adb_host or '').strip(), 5037)
            if _addr[0] and _addr in addr_seen:
                return Response(
                    {
                        'error': '设备「%s」与「%s」使用同一个服务地址（%s:%s），'
                                 '同一时间只能跑一台。请只保留其中一台，'
                                 '或把这些手机分别插到不同的电脑上。'
                                 % (_d.name, addr_seen[_addr].name, _addr[0], _addr[1]),
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            if _addr[0]:
                addr_seen[_addr] = _d

        # ---- 只允许在线设备执行 + 执行前真实预检 ----
        # 用离线设备跑用例只会得到一串无意义的失败日志，这里统一拦截。
        #
        # 注意：不能只信 DB 里的 status —— 那只是个会被写脏的缓存字段。
        # 设备关机/WDA 停掉之后没人去改它，列表会一直显示「在线」，用户就会
        # 选到一台其实已经下线的设备。所以这里做一次真实探测（iOS 探
        # /status 的 ready，Android 跑 adb devices），并且把探测结论回写库，
        # 让列表状态跟着刷新。
        #
        # 多选时**整批校验**：任何一台不可用就整批拒绝，不会出现"只跑了一半"
        # 这种让人困惑的中间状态。
        for _d in run_devices:
            if _d.status == 'busy':
                return Response(
                    {
                        'error': '执行设备「%s」正在执行其他用例，请稍后再试或改选其他设备。'
                                 % _d.name,
                        'device_id': _d.id,
                        'device_name': _d.name,
                        'device_status': _d.status,
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ⚠️ 不能因为 DB 里写着 offline 就直接拒绝（2026-10-29 实测踩坑）：
            # status 是个会被写脏的缓存字段 —— 设备重插/重启后没人去改它，
            # 就会一直停在 offline。旧写法 `if status != 'online': 直接判失败`
            # 导致**明明 adb devices 能看到设备，却永远执行不了**，
            # 而且因为不再探测，状态也永远不会被纠正回来（死锁）。
            # 正确做法：只要不是 busy，一律做真实探测；探测成功就回写 online。
            probe_ok, probe_detail = probe_device_reachable(_d)

            if probe_ok:
                # 探测通过 -> 顺手把过期状态纠正回来，避免列表一直显示离线
                if _d.status != 'online':
                    _old_status = _d.status
                    try:
                        _d.status = 'online'
                        _d.save(update_fields=['status', 'updated_at'])
                        logger.info('[执行预检] 设备 %s 状态由 %r 纠正为 online（%s）',
                                    _d.name, _old_status, probe_detail)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                # 探测失败 -> 把真实结论落库，避免列表继续骗人
                try:
                    if _d.status != 'busy':
                        _d.status = 'offline'
                        _d.save(update_fields=['status', 'updated_at'])
                except Exception:  # noqa: BLE001
                    pass
                if (_d.platform or '').lower() == 'ios':
                    hint = '请确认手机已解锁、WDA 已启动（在那台电脑上跑 ios_wda_host.bat），或改选一台在线设备。'
                else:
                    hint = '请确认设备已连接并授权 USB 调试，或改选一台在线设备。'
                return Response(
                    {
                        'error': '执行设备「%s」当前不可用：%s。%s' % (
                            _d.name, probe_detail, hint),
                        'device_id': _d.id,
                        'device_name': _d.name,
                        'device_status': 'offline',
                        'probe_detail': probe_detail,
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

        # ---- 先占位标记 busy，避免两台并发请求抢到同一台设备 ----
        # （下面 run_task 还会再置一次，这里是为了在创建执行记录之前就把
        #  设备"锁"住，否则两个请求同时校验通过会导致同一台跑两份。）
        for _d in run_devices:
            try:
                _d.status = 'busy'
                _d.save(update_fields=['status', 'updated_at'])
            except Exception:  # noqa: BLE001
                pass

        multi = len(run_devices) > 1
        batch_id = timezone.now().strftime('%Y%m%d%H%M%S') if multi else ''
        created = []

        for _idx, run_device in enumerate(run_devices):
            created.append(
                self._spawn_midscene_execution(
                    ai_case, run_device, request.user,
                    override=bool(override_device_ids),
                    batch_id=batch_id,
                    batch_index=_idx + 1 if multi else 0,
                    batch_total=len(run_devices) if multi else 0,
                )
            )

        if multi:
            return Response({
                'message': '已开始执行：同一用例在 %d 台设备上并行运行' % len(created),
                'execution_ids': [r.id for r in created],
                'execution_id': created[0].id,
                'device_count': len(created),
                'batch_id': batch_id,
            })
        return Response({
            'message': 'Midscene 用例开始执行',
            'execution_id': created[0].id,
        })

    def _spawn_midscene_execution(self, ai_case, run_device, user,
                                  override=False, batch_id='',
                                  batch_index=0, batch_total=0):
        """为「用例 × 单台设备」创建一条执行记录并起线程执行。

        多设备并行时每台一条独立记录（各自有日志/步骤/截图/token），
        用 batch_id + batch_index 关联，方便在列表里看出是同一批。
        返回创建好的 AIExecutionRecord。
        """
        _batch_line = ''
        if batch_total:
            _batch_line = '[Midscene] 本批第 %d/%d 台（批次 %s）\n' % (
                batch_index, batch_total, batch_id)

        execution_record = AIExecutionRecord.objects.create(
            project=ai_case.project,
            ai_case=ai_case,
            case_name=ai_case.name,
            task_description=ai_case.task_description or '(Midscene 结构化步骤)',
            execution_mode='midscene',
            status='running',
            executed_by=user,
            # 批次信息必须落库，否则列表页看不出"这是同一批并行里的第几台"，
            # 会被误认为重复执行（见 models.AIExecutionRecord.batch_id 说明）。
            batch_id=batch_id or '',
            batch_index=batch_index if batch_total else 0,
            batch_total=batch_total or 0,
            device_name=(getattr(run_device, 'name', '') or '')[:120],
            logs=(
                _batch_line +
                '[Midscene] 执行设备：%s（%s %s，UDID %s）%s\n'
                '[Midscene] 正在连接真机并启动应用...\n'
            ) % (
                run_device.name,
                run_device.platform,
                run_device.platform_version or '',
                run_device.udid,
                '  [本次临时指定]' if override else '',
            )
        )

        def run_task():
            STOP_SIGNALS[execution_record.id] = False
            # 本次执行真正使用的设备（可能是请求里临时指定的，不一定等于
            # 用例绑定的 ai_case.device）。
            _device = run_device
            # 执行期间标记占用，避免「刷新状态」探活把正在跑用例的设备误判为离线；
            # 执行结束后无论成败都按真实结果回调状态（失败=设备不可达/异常）。
            try:
                _device.status = 'busy'
                _device.save(update_fields=['status', 'updated_at'])
            except Exception:  # noqa: BLE001
                pass
            try:
                # 把本线程绑定到这条执行记录，之后所有 token 统计都记到它头上
                from .llm_usage import set_current_execution, clear_current_execution
                set_current_execution(execution_record.id)

                replay_used = False
                replay_degraded = False
                replay_fail_reason = ''
                engine, result = self._execute_midscene_case(ai_case, _device)
                replay_used = bool(getattr(engine, 'replay_mode', False))
                # ⚠️ 先把本次执行的日志**抓在手里**再谈降级。
                #    降级分支会 `engine = engine2` 覆盖掉 engine，届时 `engine.logs`
                #    只剩第二个引擎的 —— iOS 上「要素定位命中/回退」这些只在第一个
                #    引擎里记的行会**静默丢失**（见 _run_ios_engine 里 result['engine_logs']）。
                attempt_logs = list(result.get('engine_logs') or engine.logs)

                # ---- 回放失败 -> 自动降级回 AI 模式重跑 ----
                # 坐标固化后如果界面改版/换机型，回放会点空。这时不能直接判失败，
                # 而是退回 AI 探索模式重跑一次 —— 跑通后会自动重新固化，
                # 下次又能走回放。这样"重复跑省 token"和"界面变了也能过"两者兼得。
                if replay_used and result.get('status') != 'passed':
                    logger.info('[Midscene] 回放失败，降级为 AI 探索模式重跑 (case#%s)', ai_case.id)
                    # 记下"回放确实试过了、失败原因是什么"，供报告展示。
                    # ⚠️ 旧代码只把 replay_used 置 False，报告就只剩「AI 探索」，
                    #    用户看不出曾经走过回放 → 误判"固化没生效、永远省不了 token"。
                    replay_degraded = True
                    replay_fail_reason = str(result.get('error') or '')[:500]
                    attempt_logs.append(
                        '\n[Midscene] 固化坐标回放失败（界面可能已改版或换了机型），'
                        '自动切换为 AI 探索模式重跑...\n'
                        '       回放失败原因：%s' % (replay_fail_reason or '(未提供)')
                    )
                    engine2, result2 = self._execute_midscene_case(
                        ai_case, _device, force_ai=True)
                    result2['steps'] = list(result2.get('steps') or [])
                    attempt_logs += [''] + list(result2.get('engine_logs') or engine2.logs)
                    engine = engine2
                    result = result2
                    replay_used = False

                # ---- 跑通且尚未固化 -> 自动固化坐标 ----
                if result.get('status') == 'passed' and not getattr(engine, 'replay_mode', False):
                    try:
                        self._freeze_case_steps(ai_case, result.get('steps') or [], engine.logs)
                    except Exception as fexc:  # noqa: BLE001
                        engine.logs.append('[固化] 自动固化失败（不影响用例结果）: %s' % fexc)
                        logger.warning('[Midscene] 自动固化失败 case#%s: %s', ai_case.id, fexc)

                # 合并引擎日志与每步结果。
                # 用 attempt_logs（含被替换掉的那次引擎日志），不是 engine.logs ——
                # 降级后 engine 是第二个引擎，直接取会把第一次执行的日志丢掉。
                logs = list(attempt_logs)
                logs.append('\n[Midscene] 执行结束：%s' % result.get('status', 'unknown'))
                if result.get('error'):
                    logs.append('错误：%s' % result['error'])

                # 汇总截图序列
                screenshots = []
                for s in result.get('steps', []):
                    if s.get('screenshot'):
                        screenshots.append(s['screenshot'])
                    if s.get('fail_screenshot'):
                        screenshots.append(s['fail_screenshot'])

                # 汇总跨步骤业务上下文（业务锚点 extract 结果）
                context_dump = result.get('context', {})
                if context_dump:
                    logs.append('\n[Midscene] 业务上下文(extract 结果)：')
                    for k, v in context_dump.items():
                        logs.append('  ${{%s}} = %s' % (k, v))

                execution_record.status = 'passed' if result.get('status') == 'passed' else 'failed'
                execution_record.logs = '\n'.join(logs)
                execution_record.steps_completed = result.get('steps', [])
                execution_record.screenshots_sequence = screenshots
                execution_record.error_message = result.get('error') or ''
                execution_record.replay_mode = replay_used
                execution_record.replay_degraded = replay_degraded
                execution_record.replay_fail_reason = replay_fail_reason
                execution_record.end_time = timezone.now()
                execution_record.duration = (
                    execution_record.end_time - execution_record.start_time
                ).total_seconds()
                # token 统计由 llm_usage 旁路累加，这里刷新一下拿最新值
                execution_record.refresh_from_db(fields=[
                    'model_calls', 'prompt_tokens', 'completion_tokens', 'tokens_estimated'])
                execution_record.save()
            except Exception as e:
                execution_record.status = 'failed'
                execution_record.end_time = timezone.now()
                execution_record.duration = (
                    execution_record.end_time - execution_record.start_time
                ).total_seconds()
                execution_record.logs += '\n[Midscene] 执行出错: %s' % str(e)
                execution_record.error_message = str(e)
                execution_record.save()
                logger.warning('[Midscene] 执行失败: %s', e)
            finally:
                # 解绑线程的 token 统计目标，避免串到别的执行上
                try:
                    from .llm_usage import clear_current_execution
                    clear_current_execution()
                except Exception:  # noqa: BLE001
                    pass
                # 释放占用。
                #
                # 注意：**用例失败 != 设备离线**。断言不通过、元素没找到、
                # 某一步超时，设备本身都是好好的。早期版本在这里写
                #   status = 'online' if passed else 'offline'
                # 结果每失败一次就把设备标成离线，跑几轮之后全部设备都变成
                # offline，下拉里一台可选设备都没有了。
                #
                # 现在只有两种情况标 offline：
                #   1) 设备连接彻底失败（连不上 WDA / 设备不在线）
                #   2) 执行线程抛了异常（多半是通信层问题）
                # 其余情况恢复 online 并刷新心跳。
                _conn_failed = False
                try:
                    err_text = (execution_record.error_message or '')
                    _conn_failed = any(k in err_text for k in (
                        'connection', 'Connection', 'connect', 'timed out',
                        'Timeout', 'refused', 'unreachable', 'WDA',
                        'Failed to launch', 'device not found', 'Device not found',
                    ))
                except Exception:  # noqa: BLE001
                    _conn_failed = False

                try:
                    _device.status = 'offline' if _conn_failed else 'online'
                    if _device.status == 'online':
                        _device.last_heartbeat = timezone.now()
                    _device.save(update_fields=['status', 'last_heartbeat', 'updated_at'])
                except Exception:  # noqa: BLE001
                    pass
                STOP_SIGNALS.pop(execution_record.id, None)

        thread = threading.Thread(target=run_task)
        thread.daemon = True
        thread.start()

        return execution_record

    def _process_gif_recording(self, execution_record, history):
        """
        处理GIF录制文件
        在执行完成后查找生成的GIF文件并保存路径到数据库
        """
        try:
            import os
            from django.conf import settings
            from datetime import datetime

            # browser-use 默认生成的GIF文件名（固定为agent_history.gif）
            default_gif_path = os.path.join(os.getcwd(), 'agent_history.gif')

            # 如果找到GIF文件，移动到media/ai_recording目录并重命名
            if os.path.exists(default_gif_path):
                import shutil

                # 创建录制文件目录
                gif_dir = os.path.join(settings.MEDIA_ROOT, 'ai_recording')
                os.makedirs(gif_dir, exist_ok=True)

                # 生成新的文件名：用例名称+年月日时分秒
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                # 清理用例名称中的非法字符
                safe_case_name = "".join([c if c.isalnum() or c in (' ', '_', '-') else '_' for c in execution_record.case_name])
                new_gif_filename = f"{safe_case_name}_{timestamp}.gif"
                new_gif_path = os.path.join(gif_dir, new_gif_filename)

                # 移动并重命名文件
                shutil.move(default_gif_path, new_gif_path)

                # 保存相对路径到数据库
                relative_path = os.path.join('media', 'ai_recording', new_gif_filename)
                execution_record.gif_path = relative_path

                logger.info(f"✅ GIF recording saved to: {relative_path}")
            else:
                logger.warning(f"⚠️ GIF file not found at: {default_gif_path}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to process GIF recording: {e}")

    def _auto_mark_completed_tasks(self, execution_record):
        """
        自动标记已完成的任务
        通过分析执行历史和当前任务状态，自动标记那些已经执行但未被标记完成的任务
        """
        try:
            # 记录初始状态
            initial_completed = 0
            initial_pending = 0
            if execution_record.planned_tasks:
                initial_completed = len([t for t in execution_record.planned_tasks if t.get('status') == 'completed'])
                initial_pending = len([t for t in execution_record.planned_tasks if t.get('status') == 'pending'])
                logger.info(f"📊 Before auto-mark: {initial_completed} completed, {initial_pending} pending tasks")

            # 如果执行成功，标记所有任务为完成
            if execution_record.status == 'passed' and execution_record.planned_tasks:
                auto_marked_count = 0
                for task in execution_record.planned_tasks:
                    # 只对标记为pending的任务进行处理
                    if task.get('status') == 'pending':
                        task['status'] = 'completed'
                        auto_marked_count += 1
                        logger.info(f"🔒 Auto-marked task {task['id']} as completed")

                if auto_marked_count > 0:
                    logger.info(f"✨ Auto-marked {auto_marked_count} tasks as completed")
                else:
                    logger.info("📋 No pending tasks needed auto-marking")

            # TODO: 可以添加更智能的分析逻辑来识别部分完成的任务

        except Exception as e:
            logger.warning(f"⚠️ Failed to auto-mark completed tasks: {e}")


# 全局停止信号字典 {execution_id: bool}
STOP_SIGNALS = {}

class AIExecutionRecordViewSet(viewsets.ModelViewSet):
    """AI执行记录视图集"""
    queryset = AIExecutionRecord.objects.all()
    serializer_class = AIExecutionRecordSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['project', 'ai_case', 'status']
    ordering = ['-start_time']

    def get_queryset(self):
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        # 返回用户有权限的项目下的执行记录，以及没有关联项目的执行记录
        return AIExecutionRecord.objects.filter(
            models.Q(project__in=accessible_projects) | models.Q(project__isnull=True)
        ).distinct()

    def perform_destroy(self, instance):
        instance.delete()

    @action(detail=True, methods=['post'], url_path='stop')
    def stop(self, request, pk=None):
        """停止一条正在执行的 AI 用例。支持两种力度：

        **优雅停止（默认）** —— 置 `STOP_SIGNALS` 信号。
        执行线程在 `should_stop()` 回调里读到就退出，但该回调**只在步骤之间**被检查，
        所以当前这一步会跑完。midscene 单步 aiAct 可能跑几十秒到几分钟，
        用户会觉得"点了停止没反应"。

        **立即停止（force=true）** —— 额外杀掉 midscene 的 Node 服务进程。
        这是唯一能让卡在单步 `ai_action()` 里的调用立刻返回的手段
        （Python 线程无法从外部打断阻塞中的 HTTP 等待）。
        代价：该引擎会话失效，下次执行要重新拉起 Node（首次慢几秒）。
        做法与 Playground 的终止完全一致，`_playground_kill_node()` 已真机验证。

        ⚠️ STOP_SIGNALS 是**进程内内存字典**：多 worker / 重启后会丢。
        因此两种力度都会把 status 落成 failed，
        保证即使信号丢失，列表也不会永远停在「执行中」。
        """
        record = self.get_object()

        if record.status not in ('pending', 'running'):
            return Response({
                'success': False,
                'message': '该执行已结束（%s），无需停止' % record.get_status_display(),
                'status': record.status,
            }, status=status.HTTP_400_BAD_REQUEST)

        force = str(request.data.get('force', '')).lower() in ('1', 'true', 'yes')

        # ① 置内存信号：让执行线程在下一个检查点退出（两种力度都要）
        STOP_SIGNALS[record.id] = True

        # ② 立即停止：杀掉执行该指令的 Node 服务，让卡住的调用当场返回
        killed = 0
        if force:
            killed = _playground_kill_node()

        # ③ 兜底落库。即使线程没读到信号（进程重启等），
        #    也不该让记录永远显示"执行中"——那既不能重跑也看不出发生了什么。
        done = len(record.steps_completed or [])
        try:
            _mode = '立即停止' if force else '停止'
            record.logs = (record.logs or '') + (
                '\n[%s] 用户手动%s执行（已完成 %d 个步骤，结果保留）%s\n'
                % (_mode, _mode, done,
                   ('，已终止 %d 个 midscene 进程' % killed) if killed else ''))
            record.status = 'failed'
            record.error_message = '用户手动停止执行'
            record.end_time = timezone.now()
            if record.start_time:
                record.duration = (record.end_time - record.start_time).total_seconds()
            record.save(update_fields=[
                'logs', 'status', 'error_message', 'end_time', 'duration'])
        except Exception as exc:  # noqa: BLE001
            logger.warning('[停止] 落库失败 execution=%s: %s', record.id, exc)

        logger.warning('[停止] 用户%s AI 执行 execution=%s（已完成 %d 步，killed=%d）',
                       '立即停止' if force else '停止', record.id, done, killed)

        if force:
            msg = '已立即停止（已完成 %d 个步骤，结果保留）' % done
            if killed:
                msg += '，已终止 %d 个执行进程' % killed
            else:
                msg += '（未发现运行中的进程，可能已经结束）'
        else:
            msg = ('已发送停止信号（已完成 %d 个步骤，结果保留）。'
                   '执行线程会在当前步骤结束后停止；如需立刻停止请用「立即停止」。'
                   % done)

        return Response({
            'success': True,
            'message': msg,
            'status': 'failed',
            'force': force,
            'killed_node': killed,
        })

    @action(detail=False, methods=['post'])
    def batch_delete(self, request):
        """批量删除AI执行记录"""
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': '请选择要删除的记录'}, status=status.HTTP_400_BAD_REQUEST)

        # 确保只能删除有权限的记录，重新构建查询以避免 distinct() 限制
        user = self.request.user
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        deleted_count, _ = AIExecutionRecord.objects.filter(
            id__in=ids
        ).filter(
            models.Q(project__in=accessible_projects) | models.Q(project__isnull=True)
        ).delete()

        return Response({'message': f'成功删除 {deleted_count} 条记录'})

    @action(detail=False, methods=['post'], url_path='run_adhoc')
    def run_adhoc(self, request):
        """执行临时 AI 任务"""
        project_id = request.data.get('project_id')
        task_description = request.data.get('task_description')
        execution_mode = request.data.get('execution_mode', 'text')  # 默认文本模式
        enable_gif = request.data.get('enable_gif', True)  # GIF录制开关，默认开启

        if not task_description:
            return Response({'error': '缺少任务描述参数'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取项目对象（如果提供了project_id）
        project = None
        if project_id:
            try:
                project = UiProject.objects.get(id=project_id)
            except UiProject.DoesNotExist:
                return Response({'error': '项目不存在'}, status=status.HTTP_404_NOT_FOUND)

        # 创建执行记录
        execution_record = AIExecutionRecord.objects.create(
            project=project,
            case_name="Adhoc Task",
            task_description=task_description,
            execution_mode=execution_mode,
            status='running',
            executed_by=request.user,
            logs="正在分析任务...\n"
        )

        # Midscene Web 模式：使用 MidsceneWebEngine 执行
        if execution_mode == 'midscene_web':
            import threading
            
            # 提取 URL（第一行）和任务描述（剩余部分）
            lines = task_description.strip().split('\n', 1)
            url = lines[0].strip() if lines else ''
            task_desc = lines[1].strip() if len(lines) > 1 else task_description
            
            def run_midscene_web_task():
                STOP_SIGNALS[execution_record.id] = False
                try:
                    from .midscene_web_engine import MidsceneWebEngine
                    
                    # 获取模型配置（真源：AIModelConfig role=midscene_vision）
                    from .midscene_web_engine import load_midscene_model_config
                    model_config = load_midscene_model_config()

                    engine = MidsceneWebEngine(base_url=url, model_config=model_config, headless=True)
                    
                    # 创建步骤：先导航到 URL，然后执行任务描述
                    steps = [
                        {'action': 'navigate', 'prompt': url, 'name': '导航到目标网址'},
                        {'action': 'aiAct', 'prompt': task_desc, 'name': '执行任务'},
                    ]
                    
                    execution_record.logs += '[MidsceneWeb] 启动浏览器...\n'
                    execution_record.save(update_fields=['logs'])
                    
                    result = engine.execute_steps(steps)
                    
                    # 合并日志
                    logs = list(engine.logs)
                    logs.append('\n[MidsceneWeb] 执行结束: %s' % result.get('status', 'unknown'))
                    if result.get('error'):
                        logs.append('错误: %s' % result['error'])
                    
                    execution_record.end_time = timezone.now()
                    execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                    execution_record.logs += '\n'.join(logs)
                    execution_record.status = 'passed' if result.get('status') == 'success' else 'failed'
                    execution_record.error_message = result.get('error', '')
                    execution_record.save()
                    
                except Exception as e:
                    execution_record.status = 'failed'
                    execution_record.end_time = timezone.now()
                    execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                    execution_record.logs += '\n[MidsceneWeb] 执行出错: %s' % str(e)
                    execution_record.error_message = str(e)
                    execution_record.save()
                    logger.warning('[MidsceneWeb Adhoc] 执行失败: %s', e)
                finally:
                    STOP_SIGNALS.pop(execution_record.id, None)
            
            thread = threading.Thread(target=run_midscene_web_task)
            thread.daemon = True
            thread.start()
            
            return Response({
                'message': 'Midscene Web 任务开始执行',
                'execution_id': execution_record.id
            })

        # 文本模式：使用 browser-use 执行
        # 异步执行
        import threading
        from asgiref.sync import sync_to_async
        from .ai_agent import run_full_process_sync

        def run_task():
            # 注册停止信号
            STOP_SIGNALS[execution_record.id] = False

            try:
                # 定义异步安全的 should_stop
                async def should_stop_async():
                    # 优先检查内存信号
                    if STOP_SIGNALS.get(execution_record.id, False):
                        return True
                    # 兜底检查数据库状态 (使用 sync_to_async 避免异步上下文错误)
                    await sync_to_async(execution_record.refresh_from_db)()
                    return execution_record.status == 'stopped'

                # 定义同步版本的 should_stop 用于最后检查
                def should_stop_sync():
                    if STOP_SIGNALS.get(execution_record.id, False):
                        return True
                    execution_record.refresh_from_db()
                    return execution_record.status == 'stopped'

                async def on_analysis_complete(planned_tasks):
                    execution_record.planned_tasks = planned_tasks
                    execution_record.logs += "任务分析完成，开始执行...\n"
                    await sync_to_async(execution_record.save)()
                    
                async def on_step_update(step_info):
                    try:
                        # 处理日志
                        if step_info.get('type') == 'log':
                            content = step_info.get('content')
                            if content:
                                execution_record.logs += content
                                # 立即保存到数据库，确保前端轮询能看到最新日志
                                await sync_to_async(execution_record.save)(update_fields=['logs'])
                            return

                        # 处理任务状态
                        task_id = step_info.get('task_id')
                        status = step_info.get('status')
                        logger.info(f"DEBUG: on_step_update received: task_id={task_id}, status={status}")
                        
                        if task_id and status:
                            updated = False
                            if execution_record.planned_tasks:
                                for task in execution_record.planned_tasks:
                                    # 确保类型一致进行比较
                                    if str(task['id']) == str(task_id):
                                        old_status = task.get('status', 'pending')
                                        task['status'] = status
                                        updated = True
                                        logger.info(f"DEBUG: Updated task {task_id} from {old_status} to {status}")
                                        break
                            if updated:
                                # 立即保存到数据库，确保前端轮询能看到最新状态
                                await sync_to_async(execution_record.save)(update_fields=['planned_tasks'])
                            else:
                                logger.warning(f"DEBUG: Task ID {task_id} not found in planned_tasks: {execution_record.planned_tasks}")
                    except Exception as e:
                        logger.error(f"更新步骤状态失败: {e}", exc_info=True)

                history = run_full_process_sync(
                    task_description,
                    analysis_callback=on_analysis_complete,
                    step_callback=on_step_update,
                    should_stop=should_stop_async, # 传递异步版本
                    execution_mode=execution_mode,
                    enable_gif=enable_gif,  # 传递GIF录制开关
                    case_name=task_description[:50] if task_description else "Adhoc Task"  # 传递用例名称用于GIF文件命名
                )

                # 检查是否是手动停止 (使用同步版本)
                if should_stop_sync():
                    execution_record.status = 'stopped'
                    execution_record.logs += "\n[System] 任务已由用户停止。"
                else:
                    # 更新成功状态
                    execution_record.status = 'passed'
                    execution_record.logs += "\n执行完成。"

                    # 记录任务完成统计信息
                    if execution_record.planned_tasks:
                        total_tasks = len(execution_record.planned_tasks)
                        completed_tasks = len([t for t in execution_record.planned_tasks if t.get('status') == 'completed'])
                        pending_tasks = len([t for t in execution_record.planned_tasks if t.get('status') == 'pending'])
                        logger.info(f"🏁 Task completion summary: {completed_tasks}/{total_tasks} tasks completed, {pending_tasks} pending")
                
                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                
                # 格式化 history 为日志 (如果不是停止状态)
                steps = []
                if history:
                    if hasattr(history, 'steps'):
                        steps = [extract_step_info(s, i) for i, s in enumerate(history.steps)]

                execution_record.steps_completed = steps

                # 自动标记已完成的任务
                if execution_record.planned_tasks:
                    self._auto_mark_completed_tasks(execution_record)

                # 处理GIF录制文件
                self._process_gif_recording(execution_record, history)

                execution_record.save()

            except Exception as e:
                execution_record.status = 'failed'
                execution_record.end_time = timezone.now()
                execution_record.duration = (execution_record.end_time - execution_record.start_time).total_seconds()
                execution_record.logs += f"\n执行出错: {str(e)}"
                execution_record.save()
            finally:
                # 清理停止信号
                if execution_record.id in STOP_SIGNALS:
                    del STOP_SIGNALS[execution_record.id]

        thread = threading.Thread(target=run_task)
        thread.daemon = True
        thread.start()
        
        return Response({
            'message': 'AI 任务开始执行',
            'execution_id': execution_record.id
        })


    @action(detail=True, methods=['post'], url_path='stop')
    def stop_task(self, request, pk=None):
        """停止正在执行的任务"""
        try:
            execution_id = int(pk)
            if execution_id in STOP_SIGNALS:
                STOP_SIGNALS[execution_id] = True
                return Response({'message': '已发送停止信号'})
            else:
                # 如果不在内存中，可能已经结束，或者重启过服务
                # 尝试直接更新数据库状态
                record = self.get_object()
                if record.status == 'running':
                    record.status = 'stopped'
                    record.end_time = timezone.now()
                    record.logs += "\n[System] 任务被强制标记为停止（未在运行队列中找到）。"
                    record.save()
                    return Response({'message': '任务已标记为停止'})
                return Response({'message': '任务不在运行中'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _process_gif_recording(self, execution_record, history):
        """
        处理GIF录制文件
        在执行完成后查找生成的GIF文件并保存路径到数据库
        """
        try:
            import os
            from django.conf import settings
            from datetime import datetime

            # browser-use 默认生成的GIF文件名（固定为agent_history.gif）
            default_gif_path = os.path.join(os.getcwd(), 'agent_history.gif')

            # 如果找到GIF文件，移动到media/ai_recording目录并重命名
            if os.path.exists(default_gif_path):
                import shutil

                # 创建录制文件目录
                gif_dir = os.path.join(settings.MEDIA_ROOT, 'ai_recording')
                os.makedirs(gif_dir, exist_ok=True)

                # 生成新的文件名：用例名称+年月日时分秒
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                # 清理用例名称中的非法字符
                safe_case_name = "".join([c if c.isalnum() or c in (' ', '_', '-') else '_' for c in execution_record.case_name])
                new_gif_filename = f"{safe_case_name}_{timestamp}.gif"
                new_gif_path = os.path.join(gif_dir, new_gif_filename)

                # 移动并重命名文件
                shutil.move(default_gif_path, new_gif_path)

                # 保存相对路径到数据库
                relative_path = os.path.join('media', 'ai_recording', new_gif_filename)
                execution_record.gif_path = relative_path

                logger.info(f"✅ GIF recording saved to: {relative_path}")
            else:
                logger.warning(f"⚠️ GIF file not found at: {default_gif_path}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to process GIF recording: {e}")

    def _auto_mark_completed_tasks(self, execution_record):
        """
        自动标记已完成的任务
        通过分析执行历史和当前任务状态，自动标记那些已经执行但未被标记完成的任务
        """
        try:
            # 记录初始状态
            initial_completed = 0
            initial_pending = 0
            if execution_record.planned_tasks:
                initial_completed = len([t for t in execution_record.planned_tasks if t.get('status') == 'completed'])
                initial_pending = len([t for t in execution_record.planned_tasks if t.get('status') == 'pending'])
                logger.info(f"📊 Before auto-mark: {initial_completed} completed, {initial_pending} pending tasks")

            # 如果执行成功，标记所有任务为完成
            if execution_record.status == 'passed' and execution_record.planned_tasks:
                auto_marked_count = 0
                for task in execution_record.planned_tasks:
                    # 只对标记为pending的任务进行处理
                    if task.get('status') == 'pending':
                        task['status'] = 'completed'
                        auto_marked_count += 1
                        logger.info(f"🔒 Auto-marked task {task['id']} as completed")

                if auto_marked_count > 0:
                    logger.info(f"✨ Auto-marked {auto_marked_count} tasks as completed")
                else:
                    logger.info("📋 No pending tasks needed auto-marking")

            # TODO: 可以添加更智能的分析逻辑来识别部分完成的任务

        except Exception as e:
            logger.warning(f"⚠️ Failed to auto-mark completed tasks: {e}")

    @action(detail=True, methods=['get'], url_path='report')
    def generate_report(self, request, pk=None):
        """
        生成AI执行报告

        Query Parameters:
            report_type: 报告类型 (summary/detailed/performance)，默认为 summary

        Returns:
            执行报告数据
        """
        try:
            record = self.get_object()
            report_type = request.query_params.get('report_type', 'summary')

            # 导入报告生成器
            from .reports import AIExecutionReportGenerator

            # 生成报告
            generator = AIExecutionReportGenerator(record)

            if report_type == 'detailed':
                report = generator.generate_detailed_report()
            elif report_type == 'performance':
                report = generator.generate_performance_report()
            else:  # summary
                report = generator.generate_summary_report()

            return Response({
                'success': True,
                'data': report,
                'report_type': report_type
            })

        except Exception as e:
            logger.error(f"生成AI执行报告失败: {e}", exc_info=True)
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='export-pdf')
    def export_pdf(self, request, pk=None):
        """
        导出AI执行报告为PDF

        Query Parameters:
            report_type: 报告类型 (summary/detailed/performance)，默认为 summary

        Returns:
            PDF文件下载
        """
        try:
            record = self.get_object()
            report_type = request.query_params.get('report_type', 'summary')

            # 导入报告生成器
            from .reports import AIExecutionReportGenerator
            from .pdf_generator import AIReportPDFGenerator

            # 生成报告数据
            generator = AIExecutionReportGenerator(record)

            if report_type == 'detailed':
                report_data = generator.generate_detailed_report()
            elif report_type == 'performance':
                report_data = generator.generate_performance_report()
            else:  # summary
                report_data = generator.generate_summary_report()

            # 生成PDF
            pdf_generator = AIReportPDFGenerator(report_data, report_type)
            pdf_buffer = pdf_generator.generate()

            # 生成文件名
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            safe_case_name = "".join([c if c.isalnum() or c in (' ', '_', '-') else '_' for c in record.case_name])
            filename = f"AI_Report_{safe_case_name}_{timestamp}.pdf"

            # 返回PDF文件
            response = HttpResponse(
                pdf_buffer.getvalue(),
                content_type='application/pdf'
            )
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            response['Content-Length'] = len(pdf_buffer.getvalue())

            return response

        except ImportError as e:
            logger.error(f"PDF生成库未安装: {e}")
            return Response({
                'success': False,
                'error': 'PDF生成功能需要安装 reportlab 库，请运行: pip install reportlab'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.error(f"导出PDF失败: {e}", exc_info=True)
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UiDashboardViewSet(viewsets.ViewSet):
    """UI自动化仪表盘视图集"""
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """获取仪表盘统计数据"""
        user = request.user
        
        # 获取用户可访问的项目ID列表
        accessible_projects = filter_by_owner_or_member(UiProject.objects.all(), user)
        project_ids = accessible_projects.values_list('id', flat=True)

        # 统计数据
        project_count = accessible_projects.count()
        
        # 测试用例数量
        test_case_count = TestCase.objects.filter(project_id__in=project_ids).count()
        
        # 测试套件数量（包含用例总数）
        suite_count = TestSuite.objects.filter(project_id__in=project_ids).count()
        
        from .models import TestSuiteTestCase
        suite_test_case_count = TestSuiteTestCase.objects.filter(
            test_suite__project_id__in=project_ids
        ).count()

        # 测试执行数量（传统+新版）
        execution_count = TestExecution.objects.filter(project_id__in=project_ids).count()
        test_case_execution_count = TestCaseExecution.objects.filter(project_id__in=project_ids).count()
        total_execution_count = execution_count + test_case_execution_count

        return Response({
            'project_count': project_count,
            'test_case_count': test_case_count,
            'suite_count': suite_test_case_count,
            'execution_count': total_execution_count
        })


# ==================== App 自动化 ViewSet ====================

class _StepsGenerationUnavailable(Exception):
    """生成步骤所需的前置条件不满足（如没有可用模型配置）。

    与「调用失败」区分开：这类问题返回 503 并告诉用户怎么修，
    而不是丢一个 500 让人以为是平台坏了。
    """


def _probe_error_reason(exc):
    """把底层网络异常翻译成用户能看懂的一句话（探活/发现设备共用）。

    直接把 requests 的 `HTTPConnectionPool(... Max retries exceeded ...)`
    丢给用户没有任何意义，这里只保留结论。
    """
    text = str(exc)
    # 顺序有讲究：RemoteDisconnected / ConnectionReset 的原文较长，先匹配
    if 'RemoteDisconnected' in text or 'Remote end closed connection' in text:
        return '连接被对端关闭：端口有人监听但不是 WDA（多为端口转发已失效）'
    if 'Connection reset' in text or 'ConnectionReset' in text:
        return '连接被重置：端口转发异常，建议重跑 ios_wda_host.bat'
    if 'Connection refused' in text or 'Errno 111' in text:
        return '连接被拒绝：那台电脑上的 WDA 服务没在跑'
    if 'timed out' in text.lower() or 'timeout' in text.lower():
        return '连接超时：那台电脑不可达（检查 IP 是否正确、是否同一网段）'
    if 'No route to host' in text or 'Errno 113' in text:
        return '网络不可达：IP 写错或不在同一网段'
    if 'Name or service not known' in text or 'getaddrinfo' in text:
        return '主机名无法解析：请填 IP 而不是计算机名'
    return text[:120] if text else '探测失败'


#: 对"共享测试平台"无意义的 adb_host 取值：它们只会解析到后端自己。
_ADB_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}


def probe_device_reachable(device, timeout=5):
    """真实探测一台设备当前是否可用，返回 (online: bool, detail: str)。

    这是 `refresh_status` 的单设备版本，抽出来给「执行前预检」复用。

    背景：`AppDevice.status` 只是一个会被写脏的缓存字段 —— 设备关机或
    WDA 停掉之后没人去改它，列表就会一直显示「在线」。如果执行入口只看
    这个字段，用户就会选到一台其实已经下线的设备，然后拿到一串毫无意义
    的超时日志。所以执行前必须用真实探测兜底。

    探测方式与 refresh_status 保持一致：
      - iOS   ：GET http://<wda_host>:<wda_port>/status，value.ready 为真即在线
      - Android：ADB_SERVER_SOCKET=tcp:<adb_host> 跑 `adb devices`，
                 序列号存在且 state=device 即在线
    """
    import socket
    import subprocess as _sp
    import requests as _requests

    platform = (device.platform or '').lower()

    if platform == 'ios':
        host = (device.wda_host or '').strip()
        port = device.wda_port or 8100
        if not host:
            return False, '未配置 WDA 主机（请在设备编辑里填写手机所插电脑的 IP）'
        try:
            resp = _requests.get('http://%s:%s/status' % (host, port), timeout=timeout)
            value = (resp.json() or {}).get('value') or {}
            if value.get('ready'):
                return True, 'WDA ready'
            return False, 'WDA 可达但未就绪（ready=%s）' % value.get('ready')
        except Exception as exc:  # noqa: BLE001
            reason = _probe_error_reason(exc)
            hint = '，请在该电脑上重跑 ios_wda_host.bat' if reason.startswith('连接被拒绝') else ''
            return False, '%s（%s:%s）%s' % (reason, host, port, hint)

    if platform == 'android':
        host = (device.adb_host or '').strip()
        adb_port = '5037'
        # 兼容历史数据里写成 host:port 的形式；把端口拆出来供 socket 探测
        # 与 adb -P 共用，避免两处一个用 5037、一个用别的值。
        if host and host.count(':') == 1 and host.rsplit(':', 1)[1].isdigit():
            host, adb_port = host.rsplit(':', 1)
        # 公共平台关键防线：回环地址只对"设备所插的那台电脑"自己有意义。
        # 后端（或容器）拿到 127.0.0.1 只会探测自己 —— 要么连不上，
        # 要么更糟：恰好本机也跑着 adb server，于是显示在线，
        # 但换成任何**其他用户**执行时都必然失败。
        # 所以这里直接判为不可用，并明确告诉用户该填什么。
        if host in _ADB_LOOPBACK_HOSTS:
            return False, ('adb_host 不能是回环地址（%s）—— 公共平台上它只会指向服务器自己，'
                           '其他用户执行必然失败。请改填「手机所插电脑」的局域网 IP:5037，'
                           '并在那台电脑上运行 adb_remote_server.bat 自动注册。' % host)
        if host:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            try:
                sock.connect((host, int(adb_port)))
            except Exception as exc:  # noqa: BLE001
                return False, 'adb 服务 %s:%s 不可达（%s）' % (host, adb_port, exc)
            finally:
                sock.close()
        # ⚠️ 用 `-H <host> -P <port>` 而不是环境变量 ADB_SERVER_SOCKET。
        #
        # 2026-10-29 实测踩坑：原来写 `ADB_SERVER_SOCKET=tcp:<host>`（**漏了端口**），
        # adb 会把 `<host>:` 后半段当端口解析，实际去连 `host:5555`：
        #     * cannot start server on remote host
        #     adb: failed to check server version: ... host.docker.internal:5555
        # 结果是**设备明明在线（adb devices 能看到）却永远探测失败**，
        # 用户看到「记录状态为离线」被拦住无法执行。
        # 用 -H/-P 显式指定最稳，也与 airtest_engine 侧的连法保持一致。
        cmd = ['adb']
        if host:
            cmd += ['-H', host, '-P', adb_port]
        cmd.append('devices')
        env = os.environ.copy()
        env.pop('ADB_SERVER_SOCKET', None)  # 避免与 -H/-P 冲突
        try:
            proc = _sp.run(cmd, capture_output=True, text=True,
                           timeout=10, env=env)
        except _sp.TimeoutExpired:
            return False, 'adb devices 超时（%s:%s）' % (host or '本机', adb_port)
        except Exception as exc:  # noqa: BLE001
            return False, 'adb 不可用：%s' % exc
        states = {}
        for line in (proc.stdout or '').splitlines():
            line = line.strip()
            if not line or line.startswith('List of devices') or line.startswith('*'):
                continue
            parts = [p for p in line.split('\t') if p.strip()]
            if len(parts) >= 2:
                states[parts[0].strip()] = parts[1].strip()
        state = states.get(device.udid)
        if state == 'device':
            return True, 'adb state=device'
        if state:
            return False, 'adb state=%s（设备未授权或离线）' % state
        return False, 'adb devices 中未找到该序列号（%s）' % device.udid

    return False, '不支持的平台：%s' % (device.platform or '(空)')


class AppDeviceViewSet(viewsets.ModelViewSet):
    """移动设备管理"""
    queryset = AppDevice.objects.all()
    serializer_class = AppDeviceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['platform', 'device_type', 'status']
    search_fields = ['name', 'udid']
    ordering = ['platform', 'name']

    @action(detail=True, methods=['post'])
    def heartbeat(self, request, pk=None):
        """设备心跳上报（设备端还活着才会调到这里）"""
        device = self.get_object()
        device.last_heartbeat = timezone.now()
        if device.status == 'offline':
            device.status = 'online'
        device.save()
        return Response({'status': 'ok', 'last_heartbeat': device.last_heartbeat})

    @action(detail=True, methods=['post'])
    def set_status(self, request, pk=None):
        """手动设置设备状态"""
        device = self.get_object()
        new_status = request.data.get('status')
        if new_status not in dict(AppDevice.STATUS_CHOICES):
            return Response({'error': '无效的状态值'}, status=status.HTTP_400_BAD_REQUEST)
        device.status = new_status
        # 手设为在线等同于一次成功心跳，否则前端「最后心跳」会显示成很久以前
        if new_status == 'online':
            device.last_heartbeat = timezone.now()
        device.save()
        return Response(AppDeviceSerializer(device).data)

    @action(detail=False, methods=['post'])
    def refresh_status(self, request):
        """实时探测设备可达性，修正 status / last_heartbeat。

        背景：status 过去只在「扫描入库 / 设备端注册」时被写成 online，
        设备关机或 WDA 停了之后没人改它，列表就永远显示「在线」。
        这里做一次真实探测：
          - Android：对该设备的 adb_host 跑 `adb devices`，序列号出现且 state=device 即在线
          - iOS：GET http://<wda_host>:<wda_port>/status，ready 即在线
        busy（用例执行中）的设备跳过，避免误降级。

        参数：ids（可选，逗号分隔或列表）只刷新指定设备；不传则刷新全部。
        """
        import requests as _requests
        from django.db.models import Q

        qs = self.get_queryset()
        raw_ids = request.data.get('ids') or request.query_params.get('ids')
        if raw_ids:
            if isinstance(raw_ids, str):
                raw_ids = [i.strip() for i in raw_ids.split(',') if i.strip().isdigit()]
            qs = qs.filter(id__in=raw_ids)

        # busy 表示正在跑用例，探测会误报离线，直接跳过
        qs = qs.exclude(status='busy')

        # ---- Android：按 adb_host 分组，一次 adb devices 覆盖多台设备 ----
        adb_cache = {}

        def _adb_devices(host):
            if host in adb_cache:
                return adb_cache[host]
            # 先用短超时探一下 adb 服务的 TCP 端口：主机不可达时直接判离线，
            # 不必等 adb 自己超时（多台设备累计会拖到几十秒）。
            if host:
                host_only, _, port_str = host.partition(':')
                probe_port = int(port_str) if port_str.isdigit() else 5037
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.5)
                try:
                    sock.connect((host_only, probe_port))
                    reachable = True
                except Exception as exc:  # noqa: BLE001
                    reachable, err = False, 'adb 服务 %s 不可达（%s）' % (host, exc)
                finally:
                    sock.close()
                if not reachable:
                    adb_cache[host] = (None, err)
                    return adb_cache[host]

            env = os.environ.copy()
            if host:
                env['ADB_SERVER_SOCKET'] = 'tcp:%s' % host
            else:
                env.pop('ADB_SERVER_SOCKET', None)
            try:
                proc = subprocess.run(['adb', 'devices'], capture_output=True,
                                      text=True, timeout=10, env=env)
            except subprocess.TimeoutExpired:
                adb_cache[host] = (None, 'adb devices 超时（%s）' % host)
                return adb_cache[host]
            except Exception as exc:  # noqa: BLE001
                adb_cache[host] = (None, str(exc))
                return adb_cache[host]
            states = {}
            for line in (proc.stdout or '').splitlines():
                line = line.strip()
                if not line or line.startswith('List of devices') or line.startswith('*'):
                    continue
                parts = [p for p in line.split('\t') if p.strip()]
                if len(parts) >= 2:
                    states[parts[0].strip()] = parts[1].strip()
            adb_cache[host] = (states, (proc.stderr or '').strip() or None)
            return adb_cache[host]

        devices = list(qs.filter(Q(platform='android') | Q(platform='ios')))
        changed = []
        now = timezone.now()

        # ---- iOS 地址冲突预判：同一个 host:port 只能对应一台真机 ----
        # 一台手机插在一台电脑上，那台电脑只转发自己的 8100，所以
        # 「两台 iOS 设备共用同一个 wda_host:wda_port」在物理上不可能同时成立，
        # 必然有一条是残留数据（换电脑/换设备时没更新地址）。
        # 但 /status 响应里【没有 UDID】（只有 os.version/sdkVersion/device），
        # 无法用它反查是哪台设备 —— 只能按"谁最近上报过"来裁决：
        # last_heartbeat 最新的那台赢，其余标记离线并说明「地址冲突」。
        ios_addr_owner = {}
        for device in devices:
            if device.platform != 'ios':
                continue
            addr = ((device.wda_host or '').strip(), device.wda_port or 8100)
            if not addr[0]:
                continue
            prev = ios_addr_owner.get(addr)
            if prev is None:
                ios_addr_owner[addr] = device
            else:
                # None 表示「从没成功上报过」，排在任何真实时间之前
                cur_key = device.last_heartbeat
                prev_key = prev.last_heartbeat
                if cur_key and (prev_key is None or cur_key > prev_key):
                    ios_addr_owner[addr] = device
        ios_conflict_loser = {}
        for device in devices:
            if device.platform != 'ios':
                continue
            addr = ((device.wda_host or '').strip(), device.wda_port or 8100)
            if not addr[0]:
                continue
            if ios_addr_owner.get(addr) is not None and ios_addr_owner[addr].id != device.id:
                winner = ios_addr_owner[addr]
                # 不要提示"手工改 IP"：地址是设备端上报时自动覆盖的
                # （device_agent_register -> _upsert_app_device 按 UDID 无条件刷新 wda_host），
                # 这台设备换到别的电脑后跑一次 ios_wda_host.bat 地址就会自己更新。
                ios_conflict_loser[device.id] = (
                    '当前记录的 %s:%s 上是「%s」的 WDA（该地址同一时间只能对应一台真机）；'
                    '本机若已改插别的电脑，在该电脑上跑一次 ios_wda_host.bat，'
                    '地址会自动更新为本机；若未接入任何电脑，则保持离线' % (
                        addr[0], addr[1], winner.name))

        for device in devices:
            if device.id in ios_conflict_loser:
                online, detail = False, ios_conflict_loser[device.id]
                new_status = 'offline'
                if device.status != new_status:
                    device.status = new_status
                # 冲突失败不推进心跳，保留最后一次真实成功时间
                device.save(update_fields=['status', 'updated_at'])
                changed.append({
                    'id': device.id, 'name': device.name, 'platform': device.platform,
                    'status': new_status, 'online': False, 'detail': detail,
                    'last_heartbeat': device.last_heartbeat,
                })
                continue
            if device.platform == 'android':
                host = (device.adb_host or '').strip()
                # 兼容历史数据里写成 host:port 的形式
                if host and host.count(':') == 1 and host.rsplit(':', 1)[1].isdigit():
                    host = host.rsplit(':', 1)[0]
                states, err = _adb_devices(host)
                if states is None:
                    online, detail = False, 'adb 不可用：%s' % err
                else:
                    state = states.get(device.udid)
                    online = state == 'device'
                    detail = ('adb state=%s' % state) if state else 'adb devices 中未找到该序列号'
            else:
                host = (device.wda_host or '').strip()
                port = device.wda_port or 8100
                if not host:
                    online, detail = False, '未配置 WDA 主机（请在编辑里填写手机USB所在电脑的 IP）'
                else:
                    try:
                        resp = _requests.get('http://%s:%s/status' % (host, port), timeout=5)
                        value = (resp.json() or {}).get('value') or {}
                        online = bool(value.get('ready'))
                        detail = 'WDA ready=%s' % value.get('ready')
                    except Exception as exc:  # noqa: BLE001
                        # 只给可读结论，不要把 requests 的 ConnectionError 原文抛给用户
                        reason = _probe_error_reason(exc)
                        online = False
                        detail = '%s（%s:%s）%s' % (reason, host, port,
                                                   '，请在该电脑上重跑 ios_wda_host.bat' if reason.startswith('连接被拒绝') else '')

            new_status = 'online' if online else 'offline'
            if device.status != new_status:
                device.status = new_status
            # 心跳只在探测成功时推新；探活失败保留上一次时间，便于判断「多久没成功了」
            if online:
                device.last_heartbeat = now
            device.save(update_fields=['status', 'last_heartbeat', 'updated_at'])
            changed.append({
                'id': device.id, 'name': device.name, 'platform': device.platform,
                'status': new_status, 'online': online, 'detail': detail,
                'last_heartbeat': device.last_heartbeat,
            })

        online_count = sum(1 for c in changed if c['online'])
        return Response({
            'success': True,
            'message': '已探测 %d 台设备，在线 %d 台' % (len(changed), online_count),
            'total': len(changed),
            'online': online_count,
            'offline': len(changed) - online_count,
            'results': changed,
            'devices': [AppDeviceSerializer(d).data
                        for d in AppDevice.objects.filter(id__in=[c['id'] for c in changed])],
        })

    @action(detail=False, methods=['post'])
    def discover(self, request):
        """按 IP 扫描远程设备并自动入库（无需手填 UDID）。

        iOS：探测 http://<host>:<port>/status（WDA），ready 即建/更新设备。
        Android：通过 ADB_SERVER_SOCKET=tcp:<host>:<port> 跑 `adb devices`。

        设备以 UDID 为唯一身份：新设备新建，老设备只把 wda_host/adb_host 刷成
        **本次扫描到的电脑**并置为在线，记录永不删除。
        """
        platform = (request.data.get('platform') or 'ios').strip().lower()
        host = (request.data.get('host') or '').strip()
        try:
            port = int(request.data.get('port') or (8100 if platform == 'ios' else 5037))
        except (TypeError, ValueError):
            port = 8100 if platform == 'ios' else 5037

        if not host:
            return Response({'success': False, 'message': '请填写设备所在电脑的 IP'},
                            status=status.HTTP_400_BAD_REQUEST)

        if platform == 'android':
            return self._discover_android(host, port)

        # ---- iOS: probe WebDriverAgent ----
        try:
            import requests as _requests
            resp = _requests.get('http://%s:%s/status' % (host, port), timeout=8)
            value = (resp.json() or {}).get('value') or {}
        except Exception as exc:  # noqa: BLE001
            return Response({
                'success': False,
                'message': '连不上 %s:%s —— %s（确认那台电脑已跑 ios_wda_host.bat 且防火墙放行 %s 端口）'
                           % (host, port, _probe_error_reason(exc), port),
            }, status=status.HTTP_502_BAD_GATEWAY)

        if not value.get('ready'):
            return Response({
                'success': False,
                'message': 'http://%s:%s 返回了响应但 WDA 未就绪（ready=%s）'
                           % (host, port, value.get('ready')),
            }, status=status.HTTP_502_BAD_GATEWAY)

        os_info = value.get('os') or {}
        build = value.get('build') or {}
        device, created = _upsert_app_device(
            platform='ios',
            wda_host=host,
            wda_port=port,
            platform_version=os_info.get('version') or '',
            name='iPhone (%s)' % host,
            notes='WDA %s · 由平台自动发现' % (build.get('version') or ''),
        )
        return Response({
            'success': True,
            'created': created,
            'message': '%s设备「%s」' % ('新增' if created else '更新', device.name),
            'data': AppDeviceSerializer(device).data,
            'devices': [AppDeviceSerializer(device).data],
        })

    def _discover_android(self, host, port):
        """通过远程 ADB 列出设备并入库"""
        import subprocess

        env = os.environ.copy()
        env['ADB_SERVER_SOCKET'] = 'tcp:%s:%s' % (host, port)
        try:
            proc = subprocess.run(['adb', 'devices'], capture_output=True,
                                  text=True, timeout=25, env=env)
        except FileNotFoundError:
            return Response({'success': False, 'message': '服务器未安装 adb，无法扫描 Android 设备'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except subprocess.TimeoutExpired:
            return Response({
                'success': False,
                'message': '连接 tcp:%s:%s 超时（确认那台电脑已启动 adb -a -P %s nodaemon server 并放行端口）'
                           % (host, port, port),
            }, status=status.HTTP_502_BAD_GATEWAY)

        serials = []
        for line in (proc.stdout or '').splitlines():
            line = line.strip()
            if not line or line.startswith('List of devices') or line.startswith('*'):
                continue
            parts = [p.strip() for p in line.split('\t') if p.strip()]
            if len(parts) >= 2:
                serials.append((parts[0], parts[1]))

        if not serials:
            return Response({
                'success': False,
                'message': 'adb 未列出任何设备。输出：%s %s'
                           % ((proc.stdout or '').strip()[:200], (proc.stderr or '').strip()[:200]),
            }, status=status.HTTP_502_BAD_GATEWAY)

        payload, offline = [], []
        for serial, state in serials:
            if state != 'device':
                offline.append('%s(%s)' % (serial, state))
                continue
            device, created = _upsert_app_device(
                platform='android',
                udid=serial,
                adb_host='%s:%s' % (host, port),
                name='Android (%s)' % serial,
            )
            payload.append({'created': created, 'device': AppDeviceSerializer(device).data})

        return Response({
            'success': True,
            'created': sum(1 for p in payload if p['created']),
            'message': '发现 %d 台设备%s' % (len(payload), ('，忽略：' + ', '.join(offline)) if offline else ''),
            'devices': [p['device'] for p in payload],
            'data': payload[0]['device'] if payload else None,
        })


def _upsert_app_device(platform='ios', udid=None, wda_host=None, wda_port=None,
                       adb_host=None, name=None, platform_version=None,
                       device_type='real', notes=None, status='online'):
    """定位或创建 AppDevice，返回 (device, created)。

    **设备以 UDID 为唯一身份，记录只增不删；IP 随"插在哪台电脑上"实时更新。**

    定位顺序：
      1. 按 UDID 精确匹配 —— 命中就是同一台设备，刷新它的地址与状态
      2. 没命中时，仅当能匹配到一条**占位记录**（平台扫描时造的 ios-<host>-<port>）
         才复用并纠正 UDID（这是占位纠正，不是设备顶替）
      3. 否则**新建一条**

    ⚠️ 关键约束：**绝不允许"真实 UDID → 另一个真实 UDID"的改写**。
    同一个 WDA 地址（同一台电脑）上插不同的手机是常态，
    早期版本按 (platform, wda_host, wda_port) 无条件复用记录并改写 UDID，
    导致"换设备 = 顶替老设备记录"，列表里永远只剩一条、老设备凭空消失。
    """
    platform = (platform or 'ios').lower()
    wda_port = int(wda_port or 8100)
    device = None

    # ---- 1) 按 UDID 精确匹配（设备唯一身份）----
    if udid:
        device = AppDevice.objects.filter(udid=udid).first()

    # ---- 2) 只允许"占位记录"被纠正为真实 UDID ----
    if device is None and udid and wda_host:
        placeholder = AppDevice.objects.filter(
            platform=platform, wda_host=wda_host, wda_port=wda_port,
            udid__startswith='ios-%s-%s' % (wda_host, wda_port),
        ).first()
        if placeholder is not None:
            # 占位记录是平台扫描造的，没有真实身份，可以安全纠正
            placeholder.udid = udid
            device = placeholder
            logger.info('[设备] 占位记录纠正为真实 UDID：%s -> %s', wda_host, udid)

    # ---- 3) 没有 udid：平台扫描（WDA /status 拿不到真机 UDID）----
    # 只复用**占位记录**（或该主机上的第一条占位），避免反复扫描越扫越多；
    # **绝不认领真实 UDID 的记录** —— 那条记录属于某台具体设备，
    # 扫描只是在探测"这台电脑上有台 iOS 设备"，不该冒名顶替任何已有设备。
    if device is None and not udid and wda_host:
        placeholder_udid = 'ios-%s-%s' % (wda_host, wda_port)
        device = AppDevice.objects.filter(udid=placeholder_udid).first()

    # ---- 4) 新建 ----
    if device is None:
        udid = udid or 'ios-%s-%s' % (wda_host or 'unknown', wda_port)
        device = AppDevice(platform=platform, udid=udid,
                           device_type=device_type or 'real')

    created = device.pk is None

    # 设备插在哪台电脑：实时覆盖为最新值，保证平台连的是当前主机
    if wda_host:
        device.wda_host = wda_host
        device.wda_port = wda_port
    if adb_host:
        device.adb_host = adb_host
    if platform_version and device.platform_version != platform_version:
        device.platform_version = platform_version
    # 设备名：显式传入的优先；否则按 UDID 尾号生成，换设备能一眼区分
    if name and device.name != name:
        device.name = name
    if not device.name:
        tail = (udid or 'unknown').replace('-', '')[-6:].upper()
        device.name = 'iPhone %s' % tail if platform == 'ios' else 'Android %s' % tail
    if notes:
        device.notes = notes
    device.status = status or 'online'
    if device.status == 'online':
        device.last_heartbeat = timezone.now()
    device.save()
    return device, created


@api_view(['POST'])
@permission_classes([AllowAny])
def device_agent_register(request):
    """设备端 agent 自动注册 / 心跳 / 下线（免登录）。

    scripts/ios_wda_host.bat 跑通 WDA 后调用此接口把设备登记上平台；
    scripts/ios_device_stop.bat 停机时再调一次（status=offline）把状态推下来。

    约定：**设备以 UDID 为唯一身份**
      - 新设备（UDID 没见过）→ 新建一条
      - 老设备（UDID 已存在）→ 只更新状态 + **最新连接电脑的 IP**
        （换电脑时 wda_host/wda_port/adb_host 被覆盖为新值，不会产生第二条记录）
      - 记录永不删除，设备拔掉后仍在列表里，只是状态变 offline

    鉴权：settings.DEVICE_AGENT_TOKEN 非空时必须带同值 X-Device-Token 头。
    """
    expected = getattr(django_settings, 'DEVICE_AGENT_TOKEN', '') or ''
    if expected:
        provided = request.META.get('HTTP_X_DEVICE_TOKEN', '') or (request.data.get('token') or '')
        if provided != expected:
            return Response({'success': False, 'message': 'device agent token 无效'},
                            status=status.HTTP_403_FORBIDDEN)

    platform = (request.data.get('platform') or 'ios').strip().lower()
    udid = (request.data.get('udid') or '').strip()
    wda_host = (request.data.get('wda_host') or '').strip()
    try:
        wda_port = int(request.data.get('wda_port') or 8100)
    except (TypeError, ValueError):
        wda_port = 8100
    adb_host = (request.data.get('adb_host') or '').strip() or None

    # 公共平台约束：Android 设备上报的 adb_host 必须是"设备所插电脑"的
    # 局域网地址。回环地址（127.0.0.1/localhost）在共享平台上是个陷阱 ——
    # 它会指向后端自己，别的用户执行时必然失败且很难看出原因。
    # 这里静默纠正（丢掉地址）而不是报错：设备本身仍然要能登记上，
    # 只是标记成"地址待补"，管理员在设备编辑里补正确 IP 即可。
    adb_host_rejected = ''
    if platform == 'android' and adb_host:
        bare = adb_host.rsplit(':', 1)[0] if adb_host.count(':') == 1 else adb_host
        if bare.strip() in _ADB_LOOPBACK_HOSTS:
            adb_host_rejected = adb_host
            adb_host = None

    if not udid and not wda_host:
        return Response({'success': False, 'message': 'udid 与 wda_host 至少需要提供一个'},
                        status=status.HTTP_400_BAD_REQUEST)

    reported = (request.data.get('status') or '').strip().lower()
    if reported not in ('online', 'offline'):
        reported = 'online'

    device, created = _upsert_app_device(
        platform=platform,
        udid=udid or None,
        wda_host=wda_host or None,
        wda_port=wda_port,
        adb_host=adb_host,
        name=(request.data.get('name') or '').strip() or None,
        platform_version=(request.data.get('platform_version') or '').strip() or None,
        device_type=(request.data.get('device_type') or 'real'),
        notes=(request.data.get('notes') or '').strip() or None,
        status=reported,
    )
    if created:
        action = '新增'
    elif reported == 'offline':
        action = '标记离线'
    else:
        action = '更新'
    message = '%s设备「%s」' % (action, device.name)
    if adb_host_rejected:
        message += '（已忽略回环 adb_host「%s」，请补填该电脑的局域网 IP）' % adb_host_rejected
    return Response({
        'success': True,
        'created': created,
        'online': reported == 'online',
        'message': message,
        'adb_host_rejected': adb_host_rejected,
        'data': AppDeviceSerializer(device).data,
    })


class AppConfigViewSet(viewsets.ModelViewSet):
    """应用配置管理"""
    queryset = AppConfig.objects.all()
    serializer_class = AppConfigSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    # project/ai_project 不放进 filterset_fields：
    # django-filter 会为外键生成 ModelChoiceFilter，无法接收 "ui_<id>"/"proj_<id>" 这种合成 id，
    # 会抛「选择一个有效的选项」400。统一由 get_queryset 解析。
    filterset_fields = ['platform']
    search_fields = ['name', 'package_name']
    ordering = ['platform', 'name']

    def get_queryset(self):
        """支持按 project 参数筛选（同时匹配 project 和 ai_project）"""
        qs = super().get_queryset()
        project_param = (self.request.query_params.get('project') or '').strip()
        if project_param:
            # project 参数可能是 "ui_xxx" 或 "proj_xxx" 格式
            if project_param.startswith('ui_'):
                tail = project_param[3:]
                if tail.isdigit():
                    qs = qs.filter(project_id=tail)
            elif project_param.startswith('proj_'):
                tail = project_param[6:]
                if tail.isdigit():
                    qs = qs.filter(ai_project_id=tail)
            elif project_param.isdigit():
                # 兼容纯数字（旧的 UiProject id）
                qs = qs.filter(project_id=project_param)
            # 非合法格式（空/含非数字字符）直接忽略，避免 500
        return qs


class BusinessActionViewSet(viewsets.ModelViewSet):
    """业务动作库（Prompt 与代码分离）"""
    queryset = BusinessAction.objects.all()
    serializer_class = BusinessActionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['action']
    search_fields = ['name', 'description', 'prompt']
    ordering = ['name']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_all_projects_unified(request):
    """
    获取合并后的项目列表（UI自动化项目 + AI用例模块项目）
    用于 UI 自动化模块中所有项目下拉选择框
    返回格式：[{id: "ui_1", name: "xxx", source: "ui"}, {id: "proj_1", name: "yyy", source: "proj"}]
    """
    from apps.projects.models import Project

    result = []

    # 加载 UI自动化项目
    ui_projects = UiProject.objects.all().values('id', 'name')
    for p in ui_projects:
        result.append({
            'id': f'ui_{p["id"]}',
            'name': p['name'],
            'source': 'ui',
            'real_id': p['id']
        })

    # 加载 AI用例模块项目
    ai_projects = Project.objects.all().values('id', 'name')
    for p in ai_projects:
        result.append({
            'id': f'proj_{p["id"]}',
            'name': p['name'],
            'source': 'proj',
            'real_id': p['id']
        })

    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ensure_ui_project(request):
    """
    确保 AI项目有对应的 UiProject
    传入 {ai_project_id: xxx}，返回对应的 UiProject ID
    如果不存在则自动创建
    """
    from apps.projects.models import Project

    ai_project_id = request.data.get('ai_project_id')
    if not ai_project_id:
        return Response({'error': 'ai_project_id is required'}, status=400)

    try:
        ai_project = Project.objects.get(id=ai_project_id)
    except Project.DoesNotExist:
        return Response({'error': 'AI project not found'}, status=404)

    # 查找是否已有同名 UiProject
    ui_project = UiProject.objects.filter(name=ai_project.name).first()
    if not ui_project:
        ui_project = UiProject.objects.create(
            name=ai_project.name,
            description=ai_project.description or '',
            status='IN_PROGRESS',
            base_url='',
            owner=request.user
        )
        # 同步成员
        if hasattr(ai_project, 'members'):
            for member in ai_project.members.all():
                ui_project.members.add(member)

    return Response({
        'id': ui_project.id,
        'name': ui_project.name,
        'created': True
    })



def _probe_device_online(device, timeout=8):
    """轻量探测设备是否真的能被操作，返回 (ok, reason)。

    为什么必须有这一步（2026-10-28 真实故障）：
    Airtest 的 `connect_device()` 在设备**不在线时依然返回设备对象**（不抛异常），
    后续 `start_app` / `screenshot` 全部静默失败。于是「开始录制」在设备
    拔掉的情况下仍返回 `success=True`，耗时 70+ 秒，弹窗里 elements 为空、
    截图空白 —— 用户看到的是「点了没反应」，完全不知道是设备掉了。
    宁可这里明确失败，也不要给一个空壳会话。
    """
    platform = (getattr(device, 'platform', '') or 'android').lower()
    udid = getattr(device, 'udid', '') or ''

    if platform == 'ios':
        wda = (getattr(device, 'appium_server_url', '') or '').strip()
        if not wda:
            return False, 'iOS 设备未配置 WDA 地址'
        return True, ''  # iOS 由 WDA 侧报错，这里不阻塞

    # Android：用 adb devices 核对 udid 是否真的在列表里
    adb_host = (getattr(device, 'adb_host', '') or '').strip() or 'host.docker.internal'
    port = '5037'
    if ':' in adb_host:
        adb_host, _, _p = adb_host.rpartition(':')
        if _p.isdigit():
            port = _p

    # 用 Airtest 自带的 linux adb（容器里 PATH 中通常没有 adb）
    try:
        import airtest.core.android.static as _at_static
        adb_bin = os.path.join(
            os.path.dirname(_at_static.__file__), 'adb', 'linux', 'adb')
        if not os.path.exists(adb_bin):
            adb_bin = 'adb'
    except Exception:
        adb_bin = 'adb'

    try:
        out = subprocess.run(
            [adb_bin, '-H', adb_host, '-P', port, 'devices'],
            capture_output=True, timeout=timeout,
        ).stdout.decode('utf-8', 'ignore')
    except Exception as exc:  # noqa: BLE001
        logger.warning('[设备探测] adb devices 执行失败: %s', exc)
        return True, ''  # 探测本身失败不阻塞，交给后续真实连接报错

    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == udid:
            if parts[1] == 'device':
                return True, ''
            return False, f'设备 {udid} 当前状态为 “{parts[1]}”（未就绪）'

    return False, (f'设备 {udid} 未出现在 adb 设备列表中（adb 服务 {adb_host}:{port}）。'
                   '请检查：① USB 线是否插好、手机是否已授权 USB 调试；'
                   '② 插手机那台电脑上的 adb 服务是否已用 adb -a 启动并允许本机访问。')


#: Playground 进行中的任务登记表。
#:
#: 为什么需要：Playground 是**前台同步执行** —— 请求线程里直接调
#: `MidsceneAppEngine.playground_run()` → `page.ai_action()`，而这个调用
#: 内部是 midscene 的 Node 服务在做多轮视觉推理，**没有可设置的取消位**，
#: 一跑就可能几分钟（实测 aiAct 撞 replanning 上限要 4 分钟）。
#: Python 无法从外部停掉一个卡在 HTTP 等待上的线程，唯一可行的硬中断是
#: 杀掉正在执行这条指令的 Node 服务进程（与 APK 回归 `_kill_stale_node_service`
#: 同一手法）。所以这里登记"谁的请求正在哪个线程里跑"，供终止接口定位。
#: key = 用户 id（同一用户同时只允许一条 Playground 执行）
PLAYGROUND_ACTIVE = {}


def _playground_key(request):
    return getattr(request.user, 'id', None)


def _playground_kill_node():
    """杀掉 midscene Node 服务，让卡住的 Playground 立刻返回。"""
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
            # 只杀 midscene 自己的 Node 服务，不碰其它进程
            if 'midscene' in cmd and ('node_service' in cmd or 'service.js' in cmd):
                pid = int(p.split('/')[2])
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except Exception:  # noqa: BLE001
                    pass
        return killed
    except Exception:  # noqa: BLE001
        return 0


def _abort_requested(execution_id, execution=None):
    """检查某条执行记录是否被要求中止。

    为什么每次都重读数据库而不是读缓存：
    终止信号来自**另一个请求线程**，同进程内共享 dict 虽然可行，但执行线程
    可能重启过（进程重启 / 多 worker），只有 DB 是唯一可靠的事实源。
    读失败时按「未中止」处理 —— 宁可多跑一步，也不能因为一次查询抖动
    让用户的正常执行莫名其妙被打断。
    """
    if not execution_id:
        return False
    try:
        return bool(
            TestCaseExecution.objects.filter(id=execution_id)
            .values_list('abort_requested', flat=True)
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[中止] 读取中止信号失败 execution=%s: %s', execution_id, exc)
        return False


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def learn_elements(request):
    """dump 当前页面控件，返回元素列表供前端选择。
    根据请求参数 engine 选择 AppiumTestEngine 或 AirtestRecordingEngine，
    默认 Airtest（无需起 Appium Server）。"""
    from .smart_locator import dump_page_elements

    device_id = request.data.get('device_id')
    app_config_id = request.data.get('app_config_id')
    app_name = request.data.get('app_name', '万年历')
    engine_type = (request.data.get('engine') or 'airtest').lower()

    if not device_id or not app_config_id:
        return Response({'success': False, 'message': '缺少 device_id 或 app_config_id'}, status=400)

    _appium_url = ''

    try:
        device = _resolve_recording_device(device_id)
        app_config = get_object_or_404(AppConfig, id=app_config_id)
        _appium_url = get_appium_server_url(device)

        _online, _why = _probe_device_online(device)
        if not _online:
            logger.warning('[learn_elements] 设备不可用: %s', _why)
            return Response({'success': False, 'message': f'设备不可用：{_why}'}, status=400)

        if engine_type == 'airtest':
            from .airtest_engine import AirtestRecordingEngine
            _adb_host = getattr(device, 'adb_host', None) or None
            engine = AirtestRecordingEngine(
                serial=device.udid,
                platform=device.platform,
                app_package=app_config.package_name,
                wda_url=get_appium_server_url(device),
                adb_host=_adb_host,
            )
        else:
            from .appium_engine import AppiumTestEngine
            engine = AppiumTestEngine(
                appium_server_url=_appium_url,
                platform=device.platform,
                device_udid=device.udid,
                app_package=app_config.package_name,
                app_activity=app_config.app_activity,
                no_reset=True,
                new_command_timeout=60,
            )
        engine.connect()
        try:
            engine.driver.activate_app(app_config.package_name)
        except Exception:
            pass
        import time
        time.sleep(1)

        elements = dump_page_elements(engine)

        if engine.driver:
            engine.driver.quit()

        return Response({
            'success': True,
            'count': len(elements),
            'elements': elements,
            'message': f'发现 {len(elements)} 个控件'
        })
    except Exception as e:
        err_msg = str(e).lower()
        hint = ''
        if 'instrumentation' in err_msg or 'not running' in err_msg or 'crashed' in err_msg:
            hint = ' 设备连接可能已断开，建议重新开始录制。'
        elif 'connection refused' in err_msg or 'max retries exceeded' in err_msg:
            hint = (f' 连不上 Appium Server（{_appium_url}）。请确认：'
                    '① 宿主机已启动 Appium（appium --address 0.0.0.0 --port 4723）；'
                    '② 端口 4723 对后端容器可达；'
                    '③ 该录制引擎为 Appium。若只用真机直连，请把录制引擎切换为 Airtest。')
        return Response({'success': False, 'message': f'获取页面控件失败: {e}{hint}'}, status=500)



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def save_knowledge(request):
    """保存控件到 JSON 知识库"""
    from .smart_locator import add_knowledge

    app_name = request.data.get('app_name', '万年历')
    items = request.data.get('items', [])  # [{chinese_name, value}]

    saved = 0
    for item in items:
        add_knowledge(app_name, item.get('chinese_name', ''), item.get('value', ''))
        saved += 1

    return Response({
        'success': True,
        'saved': saved,
        'message': f'已保存 {saved} 个控件到 "{app_name}" 知识库'
    })


# ==================== 操作录制（自动生成可运行用例） ====================
# 录制会话：每个用户同时只能有一个进行中的录制会话。
# key = request.user.id, value 见下方 dict 结构。
RECORDING_SESSIONS = {}


def _recording_session_key(request):
    return request.user.id


def _resolve_recording_device(device_id):
    """兼容新旧两套设备表解析设备（录制/探测共用）。
    优先查新表 app_automation.AppDevice(device_id)，回退旧表 ui_automation.AppDevice(udid)。
    返回统一对象（含 udid/platform/appium_server_url/adb_host/name）。找不到抛 DoesNotExist。
    """
    import types as _types
    from apps.app_automation.models import AppDevice as NewAppDevice

    # 优先新表（主表，新版设备都在这里）
    try:
        nd = NewAppDevice.objects.get(device_id=device_id)
        return _types.SimpleNamespace(
            udid=nd.device_id,
            platform='android',
            appium_server_url='',
            adb_host=getattr(nd, 'adb_host', '') or '',
            name=nd.name or nd.device_id,
            _source='new',
        )
    except NewAppDevice.DoesNotExist:
        pass

    # 回退旧表（ui_automation.AppDevice，用 udid 查询）
    od = AppDevice.objects.get(udid=device_id)  # 找不到抛 DoesNotExist
    return _types.SimpleNamespace(
        udid=od.udid,
        platform=od.platform or 'android',
        appium_server_url=od.appium_server_url or '',
        adb_host=getattr(od, 'adb_host', '') or '',
        name=od.name,
        _source='old',
    )


_LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '0.0.0.0', '::1', ''}


def get_appium_server_url(device):
    """解析 Appium Server 地址，避免把容器内的回环地址当成本机。

    优先级：
      1. 设备记录里的 appium_server_url（旧表字段，非回环地址才算数）
      2. 环境变量 APPIUM_SERVER_URL
      3. 设备 adb_host（手机 USB 所在电脑，Appium 通常同一台）上的 4723
      4. 兜底 host.docker.internal:4723

    背景：后端跑在 Docker 里，`localhost:4723` 指的是**容器自己**，
    宿主机上的 Appium 永远连不上（报 Errno 111 Connection refused）。
    """
    import os

    configured = (getattr(device, 'appium_server_url', '') or '').strip().rstrip('/')
    if configured:
        host = (urlsplit(configured).hostname or '').lower()
        if host not in _LOOPBACK_HOSTS:
            return configured

    env_url = (os.environ.get('APPIUM_SERVER_URL') or '').strip().rstrip('/')
    if env_url:
        return env_url

    adb_host = (getattr(device, 'adb_host', '') or '').strip()
    # ⚠️ adb_host 可能是「host:port」形式（如 host.docker.internal:5037，
    #    这是 ADB 端口 5037，与 Appium 的 4723 完全无关）。直接拼会得到
    #    `host.docker.internal:5037:4723` 这种非法 URL，报
    #    "Failed to parse: .../session"。必须先剥掉端口只留主机名。
    adb_port = 4723
    if adb_host:
        if ':' in adb_host:
            _host, _, _port = adb_host.rpartition(':')
            if _port.isdigit() and _host:
                adb_host = _host
        adb_host = adb_host.strip('/')
    if adb_host and adb_host.lower() not in _LOOPBACK_HOSTS:
        configured_port = urlsplit(configured).port if configured else None
        return f'http://{adb_host}:{configured_port or adb_port}'

    return 'http://host.docker.internal:4723'


def _close_recording_session(key):
    """关闭录制会话并断开连接（含自动触摸监听）"""
    sess = RECORDING_SESSIONS.pop(key, None)
    if sess and sess.get('engine'):
        engine = sess['engine']
        # 停止自动触摸捕获
        if hasattr(engine, 'stop_auto_capture'):
            try: engine.stop_auto_capture()
            except Exception: pass
        try:
            engine.disconnect()
        except Exception:
            pass
    return sess


def _get_screenshot_base64(engine):
    """获取设备截图 base64（失败返回空串，不影响主流程）。

    ⚠️ Appium 引擎截图挂在 `engine.driver` 上，Airtest 引擎**没有 driver 属性**，
    截图方法直接挂在 engine 上（`AirtestRecordingEngine.get_screenshot_base64`）。
    旧实现只认 `engine.driver` ⇒ Airtest 分支的截图恒为空串，
    表现为「录制弹窗打开了但画面一片空白」，看起来像按钮没生效。
    """
    if not engine:
        return ''
    try:
        if getattr(engine, 'driver', None):
            return engine.driver.get_screenshot_base64()
        # Airtest 等其他引擎：方法挂在 engine 自身
        if hasattr(engine, 'get_screenshot_base64'):
            return engine.get_screenshot_base64()
        return ''
    except Exception as e:
        logger.warning('获取设备截图失败: %s', e)
        return ''


def _airtest_script_for_session(sess, steps):
    """把当前录制会话的步骤实时编译为可直接 `airtest run` 的脚本文本（仅 airtest 引擎）"""
    if not sess or sess.get('engine_type') != 'airtest':
        return ''
    try:
        from django.conf import settings
        from .airtest_engine import build_airtest_script
        return build_airtest_script(
            steps,
            app_package=sess['app_config'].package_name,
            serial=sess['device'].udid,
            adb_host=getattr(settings, 'AIRTEST_ADB_HOST', 'host.docker.internal'),
            adb_port=int(getattr(settings, 'AIRTEST_ADB_PORT', 5037)),
            platform=sess['device'].platform,
            wda_url=sess['device'].appium_server_url,
            display_size=sess.get('display'),
        )
    except Exception:
        return ''


def _default_desc(action_type, element_data, input_value, assert_value):
    """为录制步骤生成默认描述"""
    name = element_data.get('name') if element_data else ''
    if action_type in ('click', 'tap'):
        return f"点击{name or '元素'}"
    if action_type == 'double_tap':
        return f"双击{name or '元素'}"
    if action_type == 'long_press':
        return f"长按{name or '元素'}"
    if action_type in ('fill', 'input'):
        return f"在{name or '输入框'}输入「{input_value}」"
    if action_type == 'clear':
        return f"清空{name or '输入框'}"
    if action_type == 'swipe':
        return f"滑动{input_value or '屏幕'}"
    if action_type == 'scroll':
        return f"滚动{name or '页面'}"
    if action_type == 'screenshot':
        return "截图"
    if action_type == 'wait':
        return f"等待{input_value or '1'}秒"
    if action_type == 'assert':
        return f"断言{name or '元素'}{assert_value or ''}"
    if action_type == 'getText':
        return f"获取{name or '元素'}文本"
    if action_type == 'back':
        return "返回上一页"
    if action_type == 'home':
        return "回到主页"
    if action_type == 'launch_app':
        return "启动应用"
    if action_type == 'close_app':
        return "关闭应用"
    return action_type


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_recording(request):
    """开始录制：建立并保持 Appium 会话，后续人工操作将被记录为步骤"""
    device_id = request.data.get('device_id')
    app_config_id = request.data.get('app_config_id')
    project_id = request.data.get('project_id')
    case_name = (request.data.get('case_name') or '').strip()
    engine_type = (request.data.get('engine') or 'appium').lower()
    logger.info(f"[start_recording] 收到请求: device_id={device_id}, engine={engine_type}, raw_engine={request.data.get('engine')!r}")
    continue_case_id = request.data.get('continue_case_id')  # 继续录制：已有用例 ID

    if not device_id or not app_config_id or not project_id:
        return Response({'success': False, 'message': '缺少 device_id / app_config_id / project_id'}, status=400)

    try:
        device = _resolve_recording_device(device_id)
        app_config = get_object_or_404(AppConfig, id=app_config_id)
        project = get_object_or_404(UiProject, id=project_id)
    except Exception as e:
        return Response({'success': False, 'message': f'参数无效: {e}'}, status=400)

    # 先确认设备真的在线。否则 Airtest 的 connect_device 会"假装成功"，
    # 最后返回一个 elements 为空、截图空白、还耗时 70 秒的空壳会话，
    # 用户只会看到"点了没反应"。
    _online, _why = _probe_device_online(device)
    if not _online:
        logger.warning('[start_recording] 设备不可用: %s', _why)
        return Response({'success': False, 'message': f'设备不可用：{_why}'}, status=400)

    # 若已有会话，先关闭旧会话
    key = _recording_session_key(request)
    _close_recording_session(key)

    # 继续录制：预加载已有用例的步骤
    existing_steps = []
    existing_case = None
    if continue_case_id:
        try:
            existing_case = get_object_or_404(TestCase, id=continue_case_id)
            case_name = case_name or existing_case.name
            project = existing_case.project  # 强制用用例所属项目
            # 将 DB 步骤转为录制步骤同构格式（与 _gesture_to_step / _build_test_case_from_steps 兼容）
            for s in existing_case.steps.all().order_by('step_number'):
                el_data = None
                if s.element:
                    el_data = {
                        'locator_strategy': s.element.locator_strategy.name if s.element.locator_strategy else 'coordinate',
                        'locator_value': s.element.locator_value or '',
                        'name': s.element.name,
                    }
                    if s.center_x is not None and s.center_y is not None:
                        el_data['center_x'] = s.center_x
                        el_data['center_y'] = s.center_y
                step_dict = {
                    'step_number': s.step_number,
                    'action_type': s.action_type,
                    'description': s.description or '',
                    'input_value': s.input_value or '',
                    'wait_time': s.wait_time or 0,
                    'assert_type': s.assert_type or '',
                    'assert_value': s.assert_value or '',
                    'element': el_data,
                    'center_x': s.center_x,
                    'center_y': s.center_y,
                }
                existing_steps.append(step_dict)
        except Exception as e:
            return Response({'success': False, 'message': f'加载已有用例失败: {e}'}, status=400)

    # Appium 模式：连接前清理设备端残留的 UiAutomator2 进程，
    # 避免 "UiAutomation not connected" 导致新建 Session 失败
    if engine_type != 'airtest' and device.udid:
        import subprocess
        try:
            subprocess.run(
                ['adb', '-s', device.udid, 'shell', 'am', 'force-stop',
                 'io.appium.uiautomator2.server'],
                capture_output=True, timeout=5,
            )
            logger.info(f"已清理设备 {device.udid} 上的残留 UiAutomator2 进程")
        except Exception:
            pass  # 清理失败不阻塞后续连接

    _appium_url = get_appium_server_url(device)

    try:
        if engine_type == 'airtest':
            from .airtest_engine import AirtestRecordingEngine
            # iOS 通过 Airtest 连接时不依赖 Appium，复用 appium_server_url 字段存放
            # WDA(WebDriverAgent) 的 HTTP 代理地址（如 http://host.docker.internal:8100）。
            # adb_host：手机USB所在电脑的IP，后端通过该电脑的ADB服务控制设备
            _adb_host = getattr(device, 'adb_host', None) or None  # None 则用默认 host.docker.internal
            engine = AirtestRecordingEngine(
                serial=device.udid,
                platform=device.platform,
                app_package=app_config.package_name,
                wda_url=_appium_url,
                adb_host=_adb_host,
            )
            engine.connect()
            try:
                engine.activate_app(app_config.package_name)
            except Exception:
                pass
            time.sleep(1)
            elements = dump_page_elements(engine)
            screenshot = _get_screenshot_base64(engine)
            display = engine.display_size
        else:
            from .appium_engine import AppiumTestEngine
            engine = AppiumTestEngine(
                appium_server_url=_appium_url,
                platform=device.platform,
                device_udid=device.udid,
                app_package=app_config.package_name,
                app_activity=app_config.app_activity,
                bundle_id=app_config.package_name if device.platform == 'ios' else None,
                app_path=app_config.app_path or None,
                no_reset=True,
                new_command_timeout=600,
            )
            engine.connect()
            try:
                engine.driver.activate_app(app_config.package_name)
            except Exception:
                pass
            time.sleep(1)
            elements = dump_page_elements(engine)
            screenshot = _get_screenshot_base64(engine)
            display = None
    except Exception as e:
        err_msg = str(e).lower()
        hint = ''
        if 'instrumentation' in err_msg or 'not running' in err_msg or 'crashed' in err_msg:
            hint = (
                ' 设备端 instrumentation 进程异常，建议：1) 重启 Appium Server；'
                '2) 在设备上重新打开被测 App 后重试；3) 若仍失败，尝试 adb 重启设备。'
            )
        elif 'connection refused' in err_msg or 'max retries exceeded' in err_msg:
            hint = (f' 连不上 Appium Server（{_appium_url}）。请确认：'
                    '① 宿主机已启动 Appium（appium --address 0.0.0.0 --port 4723）；'
                    '② 端口 4723 对后端容器可达；'
                    '③ 若只用真机直连，请把录制引擎切换为 Airtest。')
        return Response({'success': False, 'message': f'建立录制会话失败: {e}{hint}'}, status=500)

    RECORDING_SESSIONS[key] = {
        'engine': engine,
        'engine_type': engine_type,
        'display': display,
        'device': device,
        'app_config': app_config,
        'project': project,
        'case_name': case_name or f'录制用例_{timezone.now():%Y%m%d_%H%M%S}',
        'steps': existing_steps,  # 继续录制时预加载已有步骤，否则为空列表
        'started_at': timezone.now(),
        'continue_case_id': continue_case_id,  # 非空表示继续录制模式
    }

    # 纯 Airtest 模式：启动自动触摸捕获（真机上操作自动记录步骤）
    auto_capture_ok = False
    if engine_type == 'airtest' and hasattr(engine, 'start_auto_capture'):
        sess_ref = RECORDING_SESSIONS[key]  # 闭包引用

        def _auto_step_callback(step_dict):
            """自动捕获回调：把手势转成的 step 追加到会话 steps"""
            steps = sess_ref.get('steps', [])
            step_dict['step_number'] = len(steps) + 1
            steps.append(step_dict)
            # 同时更新实时代码（下次 refreshRecordPage 会带上）

        try:
            auto_capture_ok = engine.start_auto_capture(_auto_step_callback)
        except Exception:
            auto_capture_ok = False

    msg = '录制已开始，自动捕获已启用 — 直接在真机上操作即可自动记录每一步' \
        if auto_capture_ok else '录制已开始，请在真机上操作，并通过面板记录每一步'

    return Response({
        'success': True,
        'message': msg,
        'auto_capture': auto_capture_ok,
        'elements': elements,
        'count': len(elements),
        'screenshot': screenshot,
        'case_name': RECORDING_SESSIONS[key]['case_name'],
        'steps': existing_steps,  # 继续录制时返回预加载的步骤
        'airtest_script': _airtest_script_for_session(RECORDING_SESSIONS[key], existing_steps),
        'continue_case_id': continue_case_id or None,  # 前端据此判断模式
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recording_page(request):
    """获取当前录制会话的实时页面控件（重新 dump）"""
    key = _recording_session_key(request)
    sess = RECORDING_SESSIONS.get(key)
    if not sess or not sess.get('engine'):
        return Response({'success': False, 'message': '没有进行中的录制会话，请先开始录制'}, status=400)
    try:
        time.sleep(0.3)
        elements = dump_page_elements(sess['engine'])
        screenshot = _get_screenshot_base64(sess['engine'])
        return Response({
            'success': True,
            'count': len(elements),
            'elements': elements,
            'screenshot': screenshot,
            'steps': sess['steps'],
            'case_name': sess.get('case_name'),
            'airtest_script': _airtest_script_for_session(sess, sess['steps']),
        })
    except Exception as e:
        err_msg = str(e).lower()
        hint = ''
        if 'instrumentation' in err_msg or 'not running' in err_msg or 'crashed' in err_msg:
            hint = ' 设备连接可能已断开，建议重新开始录制。'
        elif 'connection refused' in err_msg or 'max retries exceeded' in err_msg:
            hint = (' 连不上 Appium Server。请确认宿主机 Appium 已启动且容器可达 4723；'
                    '若只用真机直连，请把录制引擎切换为 Airtest。')
        return Response({'success': False, 'message': f'获取页面控件失败: {e}{hint}'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_action(request):
    """记录一个操作：先在真机上真实执行，成功后保存为步骤"""
    key = _recording_session_key(request)
    sess = RECORDING_SESSIONS.get(key)
    if not sess or not sess.get('engine'):
        return Response({'success': False, 'message': '没有进行中的录制会话，请先开始录制'}, status=400)

    engine = sess['engine']
    action_type = (request.data.get('action_type') or '').lower()
    element = request.data.get('element') or {}
    input_value = request.data.get('input_value', '') or ''
    assert_type = request.data.get('assert_type', '') or ''
    assert_value = request.data.get('assert_value', '') or ''
    wait_time = int(request.data.get('wait_time', 0) or 0)

    valid_actions = {
        'click', 'tap', 'double_tap', 'long_press', 'fill', 'input', 'clear',
        'swipe', 'scroll', 'screenshot', 'wait', 'assert', 'getText',
        'launch_app', 'close_app', 'back', 'home'
    }
    if action_type not in valid_actions:
        return Response({'success': False, 'message': f'不支持的操作类型: {action_type}'}, status=400)

    # 需要目标元素的操作
    element_data = None
    need_element = action_type in (
        'click', 'tap', 'double_tap', 'long_press', 'fill', 'input',
        'clear', 'assert', 'getText'
    )
    # 滑动/滚动不需要元素定位，但需保留坐标信息（Airtest 引擎按坐标滑动依赖 center_x/center_y）
    need_coords = action_type in ('swipe', 'scroll')
    if need_element:
        if not (element and element.get('value')):
            return Response({'success': False, 'message': '该操作需要提供目标元素'}, status=400)
        element_data = {
            'locator_strategy': element.get('strategy', 'xpath'),
            'locator_value': element.get('value'),
            'name': element.get('name') or element.get('chinese_name') or element.get('text') or '未命名元素',
            # 保留坐标信息，用于坐标点击（比 el.click() 更可靠）
            'center_x': element.get('center_x'),
            'center_y': element.get('center_y'),
        }
    elif need_coords:
        element_data = {
            'locator_strategy': element.get('strategy') or element.get('locator_strategy'),
            'locator_value': element.get('value') or element.get('locator_value'),
            'name': element.get('name') or element.get('chinese_name') or element.get('text'),
            'center_x': element.get('center_x'),
            'center_y': element.get('center_y'),
        }

    step_data = {
        'step_number': len(sess['steps']) + 1,
        'action_type': action_type,
        'description': request.data.get('description', '') or _default_desc(
            action_type, element_data, input_value, assert_value
        ),
        'input_value': input_value,
        'wait_time': wait_time * 1000,
        'assert_type': assert_type,
        'assert_value': assert_value,
        'element': element_data,
    }

    # 在真机上真实执行
    try:
        result = engine.execute_step(step_data)
    except Exception as e:
        return Response({'success': False, 'message': f'执行操作失败: {e}'}, status=500)

    if not result.get('success'):
        return Response({
            'success': False,
            'message': f"操作未在真机上成功执行，已跳过记录: {result.get('error')}",
            'error': result.get('error'),
        }, status=400)

    recorded = {
        'step_number': step_data['step_number'],
        'action_type': action_type,
        'description': step_data['description'],
        'input_value': input_value,
        'wait_time': wait_time * 1000,
        'assert_type': assert_type,
        'assert_value': assert_value,
        'element': element_data,
    }
    sess['steps'].append(recorded)

    # 刷新页面，便于记录下一步
    try:
        time.sleep(0.3)
        elements = dump_page_elements(engine)
    except Exception:
        elements = []
    # 记录操作后页面可能已变化，重新截图便于在变化后的页面继续点选元素
    try:
        screenshot = _get_screenshot_base64(engine)
    except Exception:
        screenshot = ''

    return Response({
        'success': True,
        'message': f"已记录步骤 {recorded['step_number']}: {recorded['description']}",
        'recorded': recorded,
        'elements': elements,
        'count': len(elements),
        'screenshot': screenshot,
        'steps': sess['steps'],
        'airtest_script': _airtest_script_for_session(sess, sess['steps']),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def stop_recording(request):
    """停止录制（放弃生成用例）"""
    key = _recording_session_key(request)
    sess = _close_recording_session(key)
    # 会话已关闭也视为成功（可能已被 generate 自动关闭）
    return Response({'success': True, 'message': '录制已停止'})


def _build_test_case_from_steps(user, project, case_name, steps, app_package=""):
    """把归一化步骤落库为 TestCase（含自动建元素、前插启动应用步骤）。

    归一化步骤格式与 generate_recording_case 录制的步骤完全一致：
    {'action_type','element':{locator_strategy,value,name,center_x,center_y}|None,
     'input_value','wait_time','assert_type','assert_value','description','center_x','center_y'}
    录制的步骤与 Airtest 导入的步骤共用此逻辑。
    """
    test_case = TestCase.objects.create(
        name=case_name,
        description=f"由自动化工具于 {timezone.now():%Y-%m-%d %H:%M:%S} 自动生成"
                    f"（{'Appium 真机录制' if app_package else '脚本导入'}）",
        project=project,
        status='ready',
        priority='medium',
        created_by=user,
    )

    created_elements = 0
    # 自动在最前面加一步「启动应用」，保证回放从干净的起点开始
    all_steps = [{'action_type': 'launch_app', 'element': None, 'input_value': '',
                  'wait_time': 1000, 'assert_type': '', 'assert_value': '',
                  'description': f'启动应用 {app_package}' if app_package else '启动应用',
                  'center_x': None, 'center_y': None}] + list(steps)
    for idx, st in enumerate(all_steps, 1):
        ed = st.get('element')
        element_obj = None
        # 兼容两种归一化格式：record_action 存 locator_value/value，
        # airtest_importer 可能直接存 value
        if ed and (ed.get('value') or ed.get('locator_value')):
            strategy_name = ed.get('locator_strategy', ed.get('strategy', 'xpath'))
            locator_val = ed.get('value') or ed.get('locator_value')
            locator_strategy, _ = LocatorStrategy.objects.get_or_create(name=strategy_name)
            element_obj, el_created = Element.objects.get_or_create(
                project=project,
                locator_strategy=locator_strategy,
                locator_value=locator_val,
                defaults={
                    'name': ed.get('name') or ed.get('chinese_name') or '未命名元素',
                    'element_type': 'INPUT' if st['action_type'] in ('fill', 'input', 'clear') else 'BUTTON',
                    'created_by': user,
                }
            )
            if el_created:
                created_elements += 1

        TestCaseStep.objects.create(
            test_case=test_case,
            step_number=idx,
            action_type=st['action_type'],
            element=element_obj,
            input_value=st.get('input_value', ''),
            wait_time=st.get('wait_time', 1000) or 1000,
            assert_type=st.get('assert_type', ''),
            assert_value=st.get('assert_value', ''),
            description=st.get('description', ''),
            # 坐标既可能在 element 内（录制步骤），也可能在 step 顶层（Airtest 导入的
            # touch((x,y)) 坐标点击，element=None）。顶层优先，避免导入的坐标被静默丢弃。
            center_x=st.get('center_x') if st.get('center_x') is not None else (ed.get('center_x') if ed else None),
            center_y=st.get('center_y') if st.get('center_y') is not None else (ed.get('center_y') if ed else None),
        )

    return test_case, created_elements, len(all_steps)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_recording_case(request):
    """结束录制并生成/更新测试用例（继续录制时追加步骤到已有用例）"""
    key = _recording_session_key(request)
    sess = RECORDING_SESSIONS.get(key)
    if not sess:
        return Response({'success': False, 'message': '没有进行中的录制会话'}, status=400)

    project = sess['project']
    case_name = (request.data.get('case_name') or '').strip() or sess['case_name']
    steps = sess['steps']
    continue_case_id = sess.get('continue_case_id')

    if not steps:
        _close_recording_session(key)
        return Response({'success': False, 'message': '尚未记录任何操作步骤，无法生成用例'}, status=400)

    try:
        # ---- 继续录制模式：删除旧步骤后重建全部步骤（旧+新） ----
        if continue_case_id:
            test_case = get_object_or_404(TestCase, id=continue_case_id)
            test_case.name = case_name
            test_case.save()
            # 删除旧步骤（元素保留，因为可能被其他用例引用）
            test_case.steps.all().delete()
            # 直接在原用例上重建全部步骤（旧+新）；不要调用
            # _build_test_case_from_steps（它会另建一个多余的 TestCase）
            created_elements = 0
            for idx, st in enumerate(steps, 1):
                ed = st.get('element')
                element_obj = None
                if ed and (ed.get('value') or ed.get('locator_value')):
                    strategy_name = ed.get('locator_strategy', ed.get('strategy', 'xpath'))
                    locator_val = ed.get('value') or ed.get('locator_value')
                    ls, _ = LocatorStrategy.objects.get_or_create(name=strategy_name)
                    element_obj, el_created = Element.objects.get_or_create(
                        project=project,
                        locator_strategy=ls,
                        locator_value=locator_val,
                        defaults={
                            'name': ed.get('name') or ed.get('chinese_name') or '未命名元素',
                            'element_type': 'INPUT' if st['action_type'] in ('fill', 'input', 'clear') else 'BUTTON',
                        }
                    )
                    if el_created:
                        created_elements += 1
                TestCaseStep.objects.update_or_create(
                    test_case=test_case,
                    step_number=idx,
                    defaults={
                        'action_type': st['action_type'],
                        'description': st.get('description', ''),
                        'input_value': st.get('input_value', ''),
                        'wait_time': st.get('wait_time', 1000) or 1000,
                        'assert_type': st.get('assert_type', ''),
                        'assert_value': st.get('assert_value', ''),
                        'element': element_obj,
                        'center_x': st.get('center_x') if st.get('center_x') is not None else (ed.get('center_x') if ed else None),
                        'center_y': st.get('center_y') if st.get('center_y') is not None else (ed.get('center_y') if ed else None),
                    }
                )
            step_count = len(steps)
            msg = f'已更新用例「{test_case.name}」，共 {step_count} 个步骤'
        else:
            test_case, created_elements, step_count = _build_test_case_from_steps(
                request.user, project, case_name, steps, sess['app_config'].package_name)
            msg = f'已生成用例「{test_case.name}」，包含 {step_count} 个步骤（含启动应用）'
    except Exception as e:
        _close_recording_session(key)
        return Response({'success': False, 'message': f'生成用例失败: {e}'}, status=500)
    finally:
        _close_recording_session(key)

    # 纯 Airtest 录制模式：额外导出可直接 `airtest run` 的脚本
    airtest_script = _airtest_script_for_session(sess, steps)

    return Response({
        'success': True,
        'message': msg,
        'case_id': test_case.id,
        'case_name': test_case.name,
        'step_count': step_count,
        'created_elements': created_elements,
        'engine_type': sess.get('engine_type', 'appium'),
        'airtest_script': airtest_script,
        'is_update': bool(continue_case_id),  # 告知前端是追加还是新建
    })


def _extract_airtpl_files(script_file):
    """若上传的是 .air 压缩包(zip)，解压提取模板图（tpl*.png）并写入 media/airtest_templates/<uuid>/。

    返回 (py_script_text, {模板文件名: 相对路径})。非 zip 返回 (原文件文本, {})。
    """
    import io as _io
    import os as _os
    import zipfile
    import uuid as _uuid
    from django.conf import settings as _settings

    raw = script_file.read()
    # 尝试按 zip 解析（.air 压缩包 / .zip）
    tpl_map = {}
    try:
        zf = zipfile.ZipFile(_io.BytesIO(raw))
        # 找 .py 脚本
        py_name = next((n for n in zf.namelist() if n.endswith('.py')), None)
        py_text = zf.read(py_name).decode('utf-8', 'ignore') if py_name else ''
        # 找模板图 tpl*.png
        imgs = [n for n in zf.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg'))]
        if imgs:
            sub = f"airtest_templates/{_uuid.uuid4().hex}"
            base_dir = _os.path.join(str(_settings.MEDIA_ROOT), sub)
            _os.makedirs(base_dir, exist_ok=True)
            for n in imgs:
                fname = n.split('/')[-1]
                fpath = _os.path.join(base_dir, fname)
                with open(fpath, 'wb') as f:
                    f.write(zf.read(n))
                tpl_map[fname] = f"{sub}/{fname}"
        zf.close()
        return py_text, tpl_map
    except (zipfile.BadZipFile, Exception):
        # 非 zip：当作纯文本
        return raw.decode('utf-8', 'ignore'), {}


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def import_airtest_script(request):
    """导入 Airtest / Poco 录制脚本（.air / .py），解析为统一步骤并生成 TestCase

    入参：
      - script: Airtest 脚本文本（与 script_file 二选一）
      - script_file: 上传的 .air/.zip 压缩包 或 .py 文件
      - project_id: 目标 UI 项目 ID（必填）
      - case_name: 用例名称（可选，默认自动命名）
      - app_package: 被测应用包名（可选，用于「启动应用」步骤描述）
    返回生成的 case_id 与解析出的步骤列表。
    """
    script = request.data.get('script') or ''
    tpl_map = {}
    sf = request.FILES.get('script_file')
    if not script and sf:
        try:
            # 尝试解压 .air 压缩包提取模板图；非 zip 则读纯文本
            script, tpl_map = _extract_airtpl_files(sf)
        except Exception:
            try:
                script = sf.read().decode('utf-8', 'ignore')
            except Exception:
                script = ''
    if not script.strip():
        return Response({'success': False, 'message': '请提供 script 文本或上传 script_file（.air/.py）'}, status=400)

    project_id = request.data.get('project_id')
    if not project_id:
        return Response({'success': False, 'message': '缺少 project_id'}, status=400)
    try:
        project = get_object_or_404(UiProject, id=project_id)
    except Exception as e:
        return Response({'success': False, 'message': f'project_id 无效: {e}'}, status=400)

    case_name = (request.data.get('case_name') or '').strip() or f'Airtest导入_{timezone.now():%Y%m%d_%H%M%S}'

    try:
        from .airtest_importer import parse_airtest_script
        steps = parse_airtest_script(script)
    except Exception as e:
        return Response({'success': False, 'message': f'脚本解析失败: {e}'}, status=400)

    if not steps:
        return Response({'success': False, 'message': '未能从脚本中解析出任何操作步骤'}, status=400)

    # 图像步骤：把模板图文件名映射为已保存的相对路径（airtest_templates/<uuid>/xxx.png），
    # 供执行时用 Airtest 图像识别定位。找不到对应图时保留原文件名（执行时跳过）。
    if tpl_map:
        for st in steps:
            ed = st.get('element') or {}
            if (ed.get('locator_strategy') or '').lower() == 'image':
                fname = (ed.get('value') or '').split('/')[-1]
                if fname in tpl_map:
                    ed['value'] = tpl_map[fname]
                    ed['locator_value'] = tpl_map[fname]

    try:
        test_case, created_elements, step_count = _build_test_case_from_steps(
            request.user, project, case_name, steps, request.data.get('app_package', ''))
    except Exception as e:
        return Response({'success': False, 'message': f'生成用例失败: {e}'}, status=500)

    return Response({
        'success': True,
        'message': f'已生成用例「{test_case.name}」，包含 {step_count} 个步骤（含启动应用）',
        'case_id': test_case.id,
        'case_name': test_case.name,
        'step_count': step_count,
        'created_elements': created_elements,
        'parsed_steps': steps,
    })


# ==================== LLM 用量统计代理 ====================
#
# midscene 内部把 LLM 响应吃掉了，usage 拿不到。这里提供一个 OpenAI 兼容的
# 转发端点：把 midscene 的 base_url 指到它，所有请求经此转发，我们顺手把
# usage 记到当前执行记录上。
#
# 用法：
#   在 _prepare_engine_env 里把 MIDSCENE_MODEL_BASE_URL 改成
#       http://127.0.0.1:<backend_port>/api/ui-automation/llm-proxy/<真实base64>
#   或者更简单：请求头带 X-LLM-Upstream。
#
# 安全性：只允许转发到 AIModelConfig 里配置过的 base_url，避免变成任意
# 请求转发器（SSRF）。


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ios_dump_elements(request):
    """
    导出 iOS 设备当前页面的可访问性元素树。

    背景（2026-09-21）：iOS 的「精确定位」是按元素树里的文案匹配的，但**不同 app
    暴露的文案差异极大** —— 有的给中文标签（如「添加心情」），有的只有拼音英文 id
    （如 `diary media font`）。用户看不见元素树，就不知道"该填什么、为什么定位失败"。
    这个接口把设备当前可见的元素（文案 + 坐标）返回给前端展示。

    请求体: { "device_id": 8, "only_named": true }
    返回:   { "total": N, "count": M, "nodes": [{type,name,label,value,rect,center}] }
    """
    from .midscene_ios_engine import MidsceneIOSEngine

    device_id = request.data.get('device_id')
    if not device_id:
        return Response({'error': '缺少 device_id'}, status=400)

    device = AppDevice.objects.filter(id=device_id).first()
    if not device:
        return Response({'error': '设备不存在'}, status=404)
    if (device.platform or '').lower() != 'ios':
        return Response({'error': '该功能仅支持 iOS 设备'}, status=400)

    engine = MidsceneIOSEngine(
        wda_host=(device.wda_host or '').strip(),
        wda_port=device.wda_port or 8100,
    )
    try:
        engine.start()
        res = engine._rpc(
            'dumpTree',
            {'sessionId': engine._session_id,
             'onlyNamed': bool(request.data.get('only_named', True))},
            timeout=120)
        return Response({
            'total': res.get('total'),
            'count': res.get('count'),
            'nodes': res.get('nodes') or [],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning('[iOS] dump 元素树失败: %s', exc)
        return Response({'error': str(exc)}, status=500)
    finally:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass


@api_view(['POST'])
@permission_classes([AllowAny])
def llm_proxy(request):
    """OpenAI 兼容的 LLM 转发端点，顺带统计 token 用量。

    上游地址按以下优先级解析：
      1. 请求头 X-LLM-Upstream（执行时由后端注入）
      2. 请求参数 upstream
    只允许转发白名单内的地址（取自 AIModelConfig.base_url）。

    统计是旁路：转发失败只记录日志，绝不影响模型调用本身。
    """
    import requests as _requests

    upstream = (request.headers.get('X-LLM-Upstream')
                or request.query_params.get('upstream') or '').strip()
    if not upstream:
        return Response({'error': '缺少上游地址（X-LLM-Upstream）'},
                        status=status.HTTP_400_BAD_REQUEST)

    # ---- 白名单校验：只转发到系统里配置过的模型服务 ----
    try:
        from apps.requirement_analysis.models import AIModelConfig
        allowed = set()
        for cfg in AIModelConfig.objects.all():
            base = (getattr(cfg, 'base_url', '') or '').strip()
            if base:
                allowed.add(base.rstrip('/'))
        if allowed and not any(upstream.rstrip('/').startswith(a) for a in allowed):
            logger.warning('[LLMProxy] 拒绝转发到未配置的地址: %s', upstream)
            return Response({'error': '该上游地址不在系统配置中'}, status=403)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[LLMProxy] 白名单校验失败，放行: %s', exc)

    url = upstream.rstrip('/') + '/' + request.path.split('llm-proxy/', 1)[-1].lstrip('/') \
        if 'llm-proxy/' in request.path else upstream
    # 保留 querystring
    if request.META.get('QUERY_STRING'):
        clean_qs = '&'.join(
            p for p in request.META['QUERY_STRING'].split('&')
            if not p.startswith('upstream=')
        )
        if clean_qs:
            url = url + ('&' if '?' in url else '?') + clean_qs

    headers = {
        'Content-Type': request.headers.get('Content-Type', 'application/json'),
        'Authorization': request.headers.get('Authorization', ''),
        'Accept': 'application/json',
    }
    headers = {k: v for k, v in headers.items() if v}

    try:
        resp = _requests.post(url, data=request.body, headers=headers, timeout=300)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[LLMProxy] 转发失败: %s', exc)
        return Response({'error': '上游请求失败: %s' % exc}, status=502)

    # ---- 旁路统计 usage ----
    try:
        payload = resp.json()
        usage = (payload or {}).get('usage') or {}
        if usage:
            from .llm_usage import record_usage
            record_usage(
                prompt_tokens=usage.get('prompt_tokens') or 0,
                completion_tokens=usage.get('completion_tokens') or 0,
                calls=1,
                estimated=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug('[LLMProxy] 解析 usage 失败（忽略）: %s', exc)

    return HttpResponse(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get('Content-Type', 'application/json'),
    )








