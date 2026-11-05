import asyncio
import logging
import re
import os  # Added import
import json
import time
import threading
from rest_framework import viewsets, status
from django.conf import settings  # Added import
from django.db.models import Q
from rest_framework.decorators import action, permission_classes
from rest_framework.response import Response
from rest_framework.renderers import BaseRenderer
from rest_framework.permissions import IsAuthenticated


class PassThroughRenderer(BaseRenderer):
    """直接透传StreamingHttpResponse，不进行任何渲染处理"""
    media_type = 'text/event-stream'
    format = 'event-stream'
    render_level = 0

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # 直接返回data，不做任何处理
        return data


class _GenerationCancelled(Exception):
    """内部信号：用户取消用例生成时由 cancel_event 触发，except 块捕获后写 cancelled 状态。"""


# 用例生成任务的取消事件表：task_id -> threading.Event
# 后台线程在流式回调和阶段之间检查该 Event，实现"协作式取消"（不能强杀线程，只能让它尽快退出）
_RUNNING_GENERATION: dict = {}
_RUNNING_GENERATION_LOCK = threading.Lock()


def _register_generation_task(task_id: str) -> threading.Event:
    """注册一个正在运行的生成任务，返回用于协作式取消的 Event"""
    event = threading.Event()
    with _RUNNING_GENERATION_LOCK:
        _RUNNING_GENERATION[task_id] = event
    return event


def _unregister_generation_task(task_id: str) -> None:
    """任务结束（完成/失败/取消）后从注册表移除"""
    with _RUNNING_GENERATION_LOCK:
        _RUNNING_GENERATION.pop(task_id, None)


def _request_cancel_generation(task_id: str) -> bool:
    """通知后台线程停止。返回是否命中了一个正在运行的任务"""
    with _RUNNING_GENERATION_LOCK:
        event = _RUNNING_GENERATION.get(task_id)
    if event is None:
        return False
    event.set()
    return True
from rest_framework.parsers import MultiPartParser, FormParser
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from asgiref.sync import sync_to_async

from .models import (
    RequirementDocument, RequirementAnalysis, BusinessRequirement,
    GeneratedTestCase, AnalysisTask, AIModelConfig, PromptConfig, TestCaseGenerationTask,
    GenerationConfig, GeneratedRequirementDoc, RequirementAnalysisResult,
    ClarificationQuestion, AIModelService
)
from apps.feature_modules.models import FeatureModule, TestPoint
from .serializers import (
    RequirementDocumentSerializer, RequirementAnalysisSerializer,
    BusinessRequirementSerializer, GeneratedTestCaseSerializer,
    AnalysisTaskSerializer, DocumentUploadSerializer,
    TestCaseGenerationRequestSerializer, TestCaseReviewRequestSerializer,
    AIModelConfigSerializer, PromptConfigSerializer, TestCaseGenerationTaskSerializer,
    GenerationConfigSerializer,
    GeneratedRequirementDocSerializer, GeneratedRequirementDocListSerializer,
    RequirementAnalysisResultSerializer, RequirementAnalysisResultListSerializer,
    ClarificationQuestionSerializer
)
from .services import RequirementAnalysisService, DocumentProcessor

logger = logging.getLogger(__name__)


def _thread_with_conn_cleanup(target):
    """把后台线程 target 包一层：进入时 + 结束时都关闭数据库连接。

    ⚠️ 2026-11-05（生产事故）：本模块有多个 ``threading.Thread`` 后台任务
    （用例生成 / 重新评审 / 用例审计 / 重新生成），它们**都不受 HTTP 请求
    生命周期管理**，而 ``DATABASES`` 未配 ``CONN_MAX_AGE``（默认 0，只在请求
    结束时关连接）—— 线程跑完连接一直挂在 MySQL 侧，累积到
    ``max_connections``（151）后全站请求报
    ``OperationalError (1040, 'Too many connections')``，
    前端表现为「加载失败: Request failed with status code 500」，
    并伴随 ``(2013, 'Lost connection to MySQL server during query')``。

    用法：``threading.Thread(target=_thread_with_conn_cleanup(fn), daemon=True)``
    与 ``apps/ui_automation/apk_workflow.py`` 的做法保持一致。
    """
    from django.db import close_old_connections

    def wrapper(*args, **kwargs):
        close_old_connections()   # 进入前清一次，避免复用失效连接
        try:
            return target(*args, **kwargs)
        finally:
            # 关键：线程退出前必须归还连接，否则连接泄漏直到打满上限
            close_old_connections()

    return wrapper


def _merge_human_feedback(old_feedback: str, new_feedback: str) -> str:
    """
    合并旧的人工确认回复和新提取的不确定项。
    规则：
    1. 保留用户已回复的旧项（确认回复不是「(未填写)」）
    2. 追加新的不确定项（来自本轮评审）
    3. 去重：如果新问题与已有问题相似，跳过
    """
    import re
    
    if not old_feedback:
        return new_feedback
    if not new_feedback:
        return old_feedback
    
    # 解析旧项，找出已回复的
    answered_blocks = re.split(r'\n\n(?=【确认项)', old_feedback)
    answered_items = []
    for block in answered_blocks:
        match = re.search(r'确认回复[：:]\s*(.+)', block, re.DOTALL)
        if match and match.group(1).strip() not in ('(未填写)', ''):
            # 提取问题文本，用于后续去重
            q_match = re.search(r'不确定需求[：:]\s*(.+?)(?:\n确认回复|\Z)', block, re.DOTALL)
            question = q_match.group(1).strip() if q_match else ""
            answered_items.append({
                'question': question,
                'block': block.strip()
            })
    
    # 解析新项
    new_blocks = re.split(r'\n\n(?=【确认项)', new_feedback)
    merged_blocks = []
    
    # 先放入已回复的旧项
    for item in answered_items:
        merged_blocks.append(item['block'])
    
    # 再追加新项（去重）
    for block in new_blocks:
        q_match = re.search(r'不确定需求[：:]\s*(.+?)(?:\n确认回复|\Z)', block, re.DOTALL)
        new_question = q_match.group(1).strip() if q_match else ""
        
        # 检查是否与已回复的问题相似
        is_duplicate = False
        for answered in answered_items:
            if _questions_similar(new_question, answered['question']):
                is_duplicate = True
                break
        
        if not is_duplicate:
            merged_blocks.append(block.strip())
    
    return "\n\n".join(merged_blocks)


def _questions_similar(q1: str, q2: str) -> bool:
    """
    判断两个问题是否相似（字符级 bigram 重叠，适合中文）。
    中文句子没有空格，不能用 .split() 分词；改用相邻两字的 bigram 集合来计算重叠率。
    分母取两个集合大小的最小值（比 max 更宽松），阈值 0.3。
    """
    if not q1 or not q2:
        return False
    # 清洗标点与空格
    clean = lambda s: s.replace('？', '').replace('?', '').replace(' ', '').strip()
    q1, q2 = clean(q1), clean(q2)
    if not q1 or not q2:
        return False
    # 完全相同直接返回
    if q1 == q2:
        return True
    # 子串包含（一个问题是另一个的精确子串）
    if q1 in q2 or q2 in q1:
        return True
    # 字符级 bigram 相似度（分母用 min，对短句更友好）
    def bigrams(s):
        return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else {s}
    bg1, bg2 = bigrams(q1), bigrams(q2)
    if not bg1 or not bg2:
        return False
    similarity = len(bg1 & bg2) / min(len(bg1), len(bg2))
    return similarity > 0.25


class RequirementDocumentViewSet(viewsets.ModelViewSet):
    """需求文档视图集"""
    queryset = RequirementDocument.objects.all()
    serializer_class = RequirementDocumentSerializer
    parser_classes = [MultiPartParser, FormParser]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return DocumentUploadSerializer
        return RequirementDocumentSerializer
    
    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """分析需求文档"""
        document = self.get_object()
        
        if document.status == 'analyzing':
            return Response(
                {'error': '文档正在分析中，请稍后再试'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if document.status == 'analyzed':
            return Response(
                {'message': '文档已经分析过了', 'analysis_id': document.analysis.id},
                status=status.HTTP_200_OK
            )
        
        try:
            # 更新状态为分析中
            document.status = 'analyzing'
            document.save()
            
            # 异步执行分析
            def run_analysis():
                try:
                    # 简化版同步分析
                    # 提取文档文本
                    if not document.extracted_text:
                        document.extracted_text = DocumentProcessor.extract_text(document)
                        document.save()
                    
                    # 创建模拟分析结果
                    analysis_result = {
                        'analysis_report': f'对文档"{document.title}"的需求分析已完成。\n\n文档内容：{document.extracted_text[:200]}...\n\n识别到若干功能性需求。',
                        'requirements_count': 2,
                        'requirements': [
                            {
                                'requirement_id': 'REQ001',
                                'requirement_name': '基础功能需求',
                                'requirement_type': 'functional',
                                'module': '核心模块',
                                'requirement_level': 'high',
                                'estimated_hours': 8,
                                'description': '基于文档内容识别的功能需求',
                                'acceptance_criteria': '功能正常运行，满足用户需求'
                            },
                            {
                                'requirement_id': 'REQ002', 
                                'requirement_name': '用户交互需求',
                                'requirement_type': 'usability',
                                'module': '前端模块',
                                'requirement_level': 'medium',
                                'estimated_hours': 6,
                                'description': '用户界面和交互相关需求',
                                'acceptance_criteria': '界面友好，操作简单'
                            }
                        ]
                    }
                    
                    # 创建分析记录
                    analysis = RequirementAnalysis.objects.create(
                        document=document,
                        analysis_report=analysis_result['analysis_report'],
                        requirements_count=analysis_result['requirements_count'],
                        analysis_time=2.5
                    )
                    
                    # 保存需求数据
                    for req_data in analysis_result['requirements']:
                        BusinessRequirement.objects.create(
                            analysis=analysis,
                            **req_data
                        )
                    
                    # 更新文档状态
                    document.status = 'analyzed'
                    document.save()
                    
                    return analysis
                    
                except Exception as e:
                    logger.error(f"分析失败: {e}")
                    document.status = 'failed'
                    document.save()
                    raise e
            
            analysis = run_analysis()
            
            return Response({
                'message': '分析完成',
                'analysis_id': analysis.id,
                'requirements_count': analysis.requirements_count
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"分析文档时出错: {e}")
            return Response(
                {'error': f'分析失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def extract_text(self, request, pk=None):
        """提取文档文本"""
        document = self.get_object()
        
        try:
            if not document.extracted_text:
                text = DocumentProcessor.extract_text(document)
                document.extracted_text = text
                document.save()
            
            return Response({
                'extracted_text': document.extracted_text,
                'text_length': len(document.extracted_text)
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"提取文本时出错: {e}")
            return Response(
                {'error': f'提取文本失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RequirementAnalysisViewSet(viewsets.ReadOnlyModelViewSet):
    """需求分析视图集"""
    queryset = RequirementAnalysis.objects.all()
    serializer_class = RequirementAnalysisSerializer
    
    @action(detail=True, methods=['get'])
    def requirements(self, request, pk=None):
        """获取分析的需求列表"""
        analysis = self.get_object()
        requirements = analysis.requirements.all()
        serializer = BusinessRequirementSerializer(requirements, many=True)
        return Response(serializer.data)


class BusinessRequirementViewSet(viewsets.ReadOnlyModelViewSet):
    """业务需求视图集"""
    queryset = BusinessRequirement.objects.all()
    serializer_class = BusinessRequirementSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        analysis_id = self.request.query_params.get('analysis_id')
        if analysis_id:
            queryset = queryset.filter(analysis_id=analysis_id)
        return queryset
    
    @classmethod 
    def _generate_test_case_content(cls, requirement, case_number, test_level):
        """根据需求类型和序号生成不同的测试用例内容"""
        
        # 基础测试场景模板
        test_scenarios = {
            1: {
                'type': '正常路径测试',
                'focus': '基本功能验证',
                'steps_template': [
                    "准备测试环境和数据",
                    "执行正常业务流程",
                    "验证功能执行结果",
                    "检查系统状态"
                ]
            },
            2: {
                'type': '异常路径测试',
                'focus': '异常情况处理',
                'steps_template': [
                    "准备异常测试数据",
                    "触发异常业务场景",
                    "验证异常处理机制",
                    "确认系统状态正常"
                ]
            },
            3: {
                'type': '边界值测试',
                'focus': '边界条件验证',
                'steps_template': [
                    "设置边界值测试条件",
                    "执行边界值操作",
                    "验证边界值处理",
                    "检查结果准确性"
                ]
            },
            4: {
                'type': '性能测试',
                'focus': '性能指标验证',
                'steps_template': [
                    "配置性能测试环境",
                    "执行性能测试操作",
                    "监控性能指标",
                    "验证性能要求"
                ]
            },
            5: {
                'type': '安全测试',
                'focus': '安全机制验证',
                'steps_template': [
                    "设置安全测试环境",
                    "执行安全相关操作",
                    "验证安全控制机制",
                    "确认安全合规性"
                ]
            }
        }
        
        # 循环使用测试场景
        scenario_key = ((case_number - 1) % 5) + 1
        scenario = test_scenarios[scenario_key]
        
        # 根据需求名称生成具体内容
        req_name = requirement.requirement_name
        req_module = requirement.module
        req_type = requirement.requirement_type
        
        # 生成标题
        title = f"{req_name} - {scenario['type']}用例"
        
        # 生成前置条件
        if "登录" in req_name:
            precondition = f"1. 系统正常运行\n2. 测试用户账号已准备\n3. {req_module}模块可访问"
        elif "数据" in req_name:
            precondition = f"1. 系统正常运行\n2. 数据库连接正常\n3. 测试数据已准备\n4. {req_module}模块可访问"
        elif "支付" in req_name:
            precondition = f"1. 系统正常运行\n2. 支付接口连接正常\n3. 测试账户余额充足\n4. {req_module}模块可访问"
        else:
            precondition = f"1. 系统正常运行\n2. 用户已登录系统\n3. {req_module}模块可访问\n4. 相关权限已配置"
        
        # 生成测试步骤
        steps = []
        for i, step_template in enumerate(scenario['steps_template'], 1):
            if "登录" in req_name:
                if i == 1:
                    steps.append(f"{i}. 打开登录页面，准备测试用户凭证")
                elif i == 2:
                    if scenario_key == 1:
                        steps.append(f"{i}. 输入正确的用户名和密码，点击登录")
                    elif scenario_key == 2:
                        steps.append(f"{i}. 输入错误的用户名或密码，点击登录")
                    else:
                        steps.append(f"{i}. 执行{scenario['focus']}相关的登录操作")
                elif i == 3:
                    steps.append(f"{i}. 验证登录结果和页面跳转")
                else:
                    steps.append(f"{i}. 检查用户登录状态和系统响应")
            elif "数据" in req_name:
                if i == 1:
                    steps.append(f"{i}. 进入{req_module}，准备数据操作")
                elif i == 2:
                    if scenario_key == 1:
                        steps.append(f"{i}. 执行正常的数据录入/查询操作")
                    elif scenario_key == 2:
                        steps.append(f"{i}. 执行异常数据操作（如格式错误、超长数据等）")
                    else:
                        steps.append(f"{i}. 执行{scenario['focus']}相关的数据操作")
                elif i == 3:
                    steps.append(f"{i}. 验证数据操作结果和完整性")
                else:
                    steps.append(f"{i}. 检查数据状态和系统响应")
            else:
                steps.append(f"{i}. {step_template}（针对{req_name}）")
        
        test_steps = "\n".join(steps)
        
        # 生成预期结果
        if scenario_key == 1:  # 正常路径
            expected_result = f"{req_name}功能正常执行，满足业务需求，系统响应正确"
        elif scenario_key == 2:  # 异常路径
            expected_result = f"系统正确处理异常情况，给出适当提示，{req_name}功能保持稳定"
        elif scenario_key == 3:  # 边界值
            expected_result = f"{req_name}在边界条件下正常工作，数据处理准确，无异常错误"
        elif scenario_key == 4:  # 性能测试
            expected_result = f"{req_name}性能满足要求，响应时间在可接受范围内，系统稳定运行"
        else:  # 安全测试
            expected_result = f"{req_name}安全机制有效，权限控制正常，敏感信息得到保护"
        
        return {
            'title': title,
            'precondition': precondition,
            'test_steps': test_steps,
            'expected_result': expected_result
        }
    
    @action(detail=False, methods=['post'])
    def generate_test_cases(self, request):
        """为选中的需求生成测试用例"""
        serializer = TestCaseGenerationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            requirement_ids = serializer.validated_data['requirement_ids']
            test_level = serializer.validated_data['test_level']
            test_priority = serializer.validated_data['test_priority']
            test_case_count = serializer.validated_data['test_case_count']
            
            # 生成唯一case_id的辅助函数
            def generate_unique_case_id(requirement, base_index):
                """生成唯一的测试用例ID"""
                base_case_id = f"TC{requirement.requirement_id}_{base_index:03d}"
                case_id = base_case_id
                counter = 1
                
                # 检查是否已存在，如果存在则添加后缀
                while GeneratedTestCase.objects.filter(requirement=requirement, case_id=case_id).exists():
                    case_id = f"{base_case_id}_{counter}"
                    counter += 1
                
                return case_id
            
            # 同步生成测试用例
            def run_generation():
                try:
                    # 获取需求数据
                    requirements = BusinessRequirement.objects.filter(id__in=requirement_ids)
                    generated_test_cases = []
                    
                    for requirement in requirements:
                        # 获取该需求现有测试用例的数量，作为起始索引
                        existing_count = GeneratedTestCase.objects.filter(requirement=requirement).count()
                        
                        for i in range(test_case_count):
                            # 生成唯一的case_id
                            case_id = generate_unique_case_id(requirement, existing_count + i + 1)
                            
                            # 根据需求类型和序号生成不同的测试用例内容
                            test_case_content = BusinessRequirementViewSet._generate_test_case_content(requirement, i + 1, test_level)
                            
                            # 创建测试用例
                            test_case = GeneratedTestCase.objects.create(
                                requirement=requirement,
                                case_id=case_id,
                                title=test_case_content['title'],
                                priority=test_priority,
                                precondition=test_case_content['precondition'],
                                test_steps=test_case_content['test_steps'],
                                expected_result=test_case_content['expected_result'],
                                status='generated',
                                generated_by_ai='AI-Generator-v1.0'
                            )
                            generated_test_cases.append(test_case)
                    
                    return generated_test_cases
                    
                except Exception as e:
                    logger.error(f"生成测试用例失败: {e}")
                    raise e
            
            test_cases = run_generation()
            
            # 序列化返回结果
            test_case_serializer = GeneratedTestCaseSerializer(test_cases, many=True)
            
            return Response({
                'message': f'成功生成{len(test_cases)}个测试用例',
                'test_cases': test_case_serializer.data
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"生成测试用例时出错: {e}")
            return Response(
                {'error': f'生成测试用例失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


from rest_framework.pagination import PageNumberPagination


class GeneratedTestCasePagination(PageNumberPagination):
    """生成测试用例分页器"""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class TestCaseGenerationTaskPagination(PageNumberPagination):
    """测试用例生成任务分页器"""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class GeneratedTestCaseViewSet(viewsets.ModelViewSet):
    """生成的测试用例视图集"""
    queryset = GeneratedTestCase.objects.all()
    serializer_class = GeneratedTestCaseSerializer
    pagination_class = GeneratedTestCasePagination
    http_method_names = ['get', 'patch']  # 只允许GET和PATCH方法
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # 按需求ID过滤
        requirement_id = self.request.query_params.get('requirement_id')
        if requirement_id:
            queryset = queryset.filter(requirement_id=requirement_id)
        
        # 按状态过滤
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        
        # 按优先级过滤
        priority_param = self.request.query_params.get('priority')
        if priority_param:
            queryset = queryset.filter(priority=priority_param)
            
        return queryset
    
    @action(detail=False, methods=['post'])
    def review_test_cases(self, request):
        """评审测试用例"""
        serializer = TestCaseReviewRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            test_case_ids = serializer.validated_data['test_case_ids']
            review_criteria = serializer.validated_data['review_criteria']
            
            # 同步执行评审
            def run_review():
                try:
                    # 获取测试用例
                    test_cases = GeneratedTestCase.objects.filter(id__in=test_case_ids)
                    
                    passed_count = 0
                    reviewed_cases = []
                    
                    for test_case in test_cases:
                        # 模拟评审逻辑
                        is_passed = len(test_case.title) > 10 and len(test_case.test_steps) > 20
                        
                        if is_passed:
                            passed_count += 1
                            test_case.status = 'approved'
                            test_case.review_comments = '测试用例设计合理，满足评审标准'
                        else:
                            test_case.status = 'rejected'
                            test_case.review_comments = '测试用例需要完善，请补充详细的测试步骤'
                        
                        test_case.reviewed_by_ai = 'AI-Reviewer-v1.0'
                        test_case.save()
                        
                        reviewed_cases.append({
                            'id': test_case.id,
                            'case_id': test_case.case_id,
                            'title': test_case.title,
                            'status': test_case.status,
                            'review_comments': test_case.review_comments
                        })
                    
                    total_count = len(test_cases)
                    pass_rate = (passed_count / total_count * 100) if total_count > 0 else 0
                    
                    return {
                        'total_count': total_count,
                        'passed_count': passed_count,
                        'pass_rate': pass_rate,
                        'reviewed_cases': reviewed_cases
                    }
                    
                except Exception as e:
                    logger.error(f"评审测试用例失败: {e}")
                    raise e
            
            review_result = run_review()
            
            return Response({
                'message': f'评审完成，通过率: {review_result["pass_rate"]:.2f}%',
                'review_result': review_result
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"评审测试用例时出错: {e}")
            return Response(
                {'error': f'评审失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AnalysisTaskViewSet(viewsets.ReadOnlyModelViewSet):
    """分析任务视图集"""
    queryset = AnalysisTask.objects.all()
    serializer_class = AnalysisTaskSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        document_id = self.request.query_params.get('document_id')
        if document_id:
            queryset = queryset.filter(document_id=document_id)
        return queryset
    
    @action(detail=True, methods=['get'])
    def progress(self, request, pk=None):
        """获取任务进度"""
        task = self.get_object()
        return Response({
            'task_id': task.task_id,
            'status': task.status,
            'progress': task.progress,
            'error_message': task.error_message
        })


from rest_framework.decorators import api_view
from django.views.decorators.csrf import csrf_exempt


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_and_analyze(request):
    """上传文档并立即开始分析"""
    try:
        # 创建文档
        serializer = DocumentUploadSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({'error': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        
        document = serializer.save()
        
        # 立即开始分析
        document.status = 'analyzing' 
        document.save()
        
        def run_analysis():
            try:
                # 简化版同步分析
                # 提取文档文本
                if not document.extracted_text:
                    document.extracted_text = DocumentProcessor.extract_text(document)
                    document.save()
                
                # 创建模拟分析结果
                analysis_result = {
                    'analysis_report': f'对文档"{document.title}"的需求分析已完成。\n\n文档内容：{document.extracted_text[:200]}...\n\n识别到若干功能性需求。',
                    'requirements_count': 2,
                    'requirements': [
                        {
                            'requirement_id': 'REQ001',
                            'requirement_name': '基础功能需求',
                            'requirement_type': 'functional',
                            'module': '核心模块',
                            'requirement_level': 'high',
                            'estimated_hours': 8,
                            'description': '基于文档内容识别的功能需求',
                            'acceptance_criteria': '功能正常运行，满足用户需求'
                        },
                        {
                            'requirement_id': 'REQ002', 
                            'requirement_name': '用户交互需求',
                            'requirement_type': 'usability',
                            'module': '前端模块',
                            'requirement_level': 'medium',
                            'estimated_hours': 6,
                            'description': '用户界面和交互相关需求',
                            'acceptance_criteria': '界面友好，操作简单'
                        }
                    ]
                }
                
                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=2.5
                )
                
                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )
                
                # 更新文档状态
                document.status = 'analyzed'
                document.save()
                
                return analysis
                
            except Exception as e:
                logger.error(f"分析失败: {e}")
                document.status = 'failed'
                document.save()
                raise e
        
        analysis = run_analysis()
        
        return Response({
            'message': '上传并分析完成',
            'document_id': document.id,
            'analysis_id': analysis.id,
            'requirements_count': analysis.requirements_count
        })
        
    except Exception as e:
        logger.error(f"上传并分析失败: {e}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def analyze_text(request):
    """分析手动输入的需求文本"""
    try:
        title = request.data.get('title')
        description = request.data.get('description')
        project_id = request.data.get('project')
        
        if not title or not description:
            return Response({'error': '需求标题和描述不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 创建一个虚拟的需求文档记录
        document = RequirementDocument.objects.create(
            title=title,
            file=None,  # 手动输入没有文件
            document_type='txt',
            status='analyzing',
            uploaded_by=request.user,
            project_id=project_id if project_id else None,
            extracted_text=description
        )
        
        # 立即开始分析
        def run_analysis():
            try:
                # 使用新的先进分析系统
                import asyncio
                from .services import AIService
                
                logger.info(f"开始使用先进分析器分析需求: {title}")
                
                # 调用先进的需求分析
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    analysis_result = loop.run_until_complete(
                        AIService.analyze_requirements(description, title)
                    )
                    logger.info(f"先进分析完成，识别需求: {analysis_result.get('requirements_count', 0)}个")
                finally:
                    loop.close()
                
                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=analysis_result.get('analysis_time', 2.0)
                )
                
                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )
                
                # 更新文档状态
                document.status = 'analyzed'
                document.save()
                
                return analysis
                
            except Exception as e:
                logger.error(f"先进分析失败: {e}，使用备用分析")
                # fallback到简单分析
                analysis_result = {
                    'analysis_report': f'对需求"{title}"的分析已完成。\n\n需求描述：{description[:200]}...\n\n基于描述内容识别到若干功能性需求。',
                    'requirements_count': 2,
                    'requirements': [
                        {
                            'requirement_id': 'REQ001',
                            'requirement_name': title + ' - 核心功能',
                            'requirement_type': 'functional',
                            'module': '核心模块',
                            'requirement_level': 'high',
                            'estimated_hours': 8,
                            'description': description[:100] + '...',
                            'acceptance_criteria': '功能正常运行，满足用户需求'
                        },
                        {
                            'requirement_id': 'REQ002', 
                            'requirement_name': title + ' - 交互功能',
                            'requirement_type': 'usability',
                            'module': '前端模块',
                            'requirement_level': 'medium',
                            'estimated_hours': 6,
                            'description': '用户界面和交互相关需求',
                            'acceptance_criteria': '界面友好，操作简单'
                        }
                    ]
                }
                
                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=1.5
                )
                
                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )
                
                # 更新文档状态
                document.status = 'analyzed'
                document.save()
                
                return analysis
                
            except Exception as e:
                logger.error(f"分析失败: {e}")
                document.status = 'failed'
                document.save()
                raise e
        
        analysis = run_analysis()
        
        return Response({
            'message': '文本分析完成',
            'document_id': document.id,
            'analysis_id': analysis.id,
            'requirements_count': analysis.requirements_count
        })
        
    except Exception as e:
        logger.error(f"文本分析失败: {e}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AIModelConfigViewSet(viewsets.ModelViewSet):
    """AI模型配置视图集"""
    queryset = AIModelConfig.objects.all()
    serializer_class = AIModelConfigSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # 按模型类型过滤
        model_type = self.request.query_params.get('model_type')
        if model_type:
            queryset = queryset.filter(model_type=model_type)
        
        # 按角色过滤
        role = self.request.query_params.get('role')
        if role:
            queryset = queryset.filter(role=role)
        else:
            # 如果没有指定角色，默认排除 AI智能模式专用模型
            queryset = queryset.exclude(role__in=['browser_use_text', 'browser_use_vision'])
        
        # 按是否启用过滤
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset.order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """测试模型连接"""
        try:
            config = self.get_object()

            logger.info(f"=== 开始测试模型连接 ===")
            logger.info(f"模型类型: {config.model_type}")
            logger.info(f"模型名称: {config.model_name}")
            logger.info(f"API URL: {config.base_url}")
            logger.info(f"API Key前缀: {config.api_key[:10]}..." if len(config.api_key) > 10 else f"API Key: {config.api_key}")

            logger.info(f"角色: {config.role}")

            # 准备测试消息
            test_messages = [
                {"role": "system", "content": "你是一个AI助手"},
                {"role": "user", "content": "请回复'连接成功'"}
            ]

            # 异步测试连接 - 统一使用OpenAI兼容API
            def test_api_connection():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    try:
                        logger.info("开始调用API...")
                        # 向量化模型（embedder）不支持 /chat/completions，
                        # 必须走 /embeddings 接口测试，否则必然报 model_not_supported
                        if config.role == 'embedder':
                            vectors = loop.run_until_complete(
                                asyncio.wait_for(
                                    AIModelService.embed_texts(config, ['连接测试']),
                                    timeout=60.0
                                )
                            )
                            dim = len(vectors[0]) if vectors else 0
                            logger.info(f"Embedding 测试成功，向量维度: {dim}")
                            return {
                                'success': True,
                                'message': f'连接测试成功（Embedding 接口可用，向量维度 {dim}）',
                                'response': f'dim={dim}',
                            }
                        # 设置60秒超时，统一使用OpenAI兼容API
                        result = loop.run_until_complete(
                            asyncio.wait_for(
                                AIModelService.call_openai_compatible_api(config, test_messages),
                                timeout=60.0
                            )
                        )

                        logger.info(f"API调用成功: {result}")
                        return {
                            'success': True,
                            'message': '连接测试成功',
                            'response': result.get('choices', [{}])[0].get('message', {}).get('content', '')
                        }
                    except asyncio.TimeoutError:
                        logger.error(f"API连接测试超时 (60秒), URL: {config.base_url}, Model: {config.model_name}")
                        return {
                            'success': False,
                            'message': '连接测试超时: 请检查网络连接或API地址是否正确'
                        }
                    finally:
                        try:
                            loop.run_until_complete(loop.shutdown_asyncgens())
                        except Exception:
                            pass
                        finally:
                            loop.close()

                except Exception as e:
                    logger.error(f"API连接测试异常: {repr(e)}, URL: {config.base_url}, Model: {config.model_name}")
                    import traceback
                    logger.error(f"详细错误堆栈:\n{traceback.format_exc()}")
                    return {
                        'success': False,
                        'message': f'连接测试失败: {str(e)}'
                    }

            result = test_api_connection()
            
            if result['success']:
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            logger.error(f"测试连接时出错: {e}")
            return Response(
                {'success': False, 'message': f'测试失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            config = self.get_object()
            config.is_active = True
            config.save()
            return Response({
                'message': 'AI模型配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用AI模型配置失败: {e}")
            return Response({
                'error': f'启用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()
            return Response({
                'message': 'AI模型配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用AI模型配置失败: {e}")
            return Response({
                'error': f'禁用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PromptConfigViewSet(viewsets.ModelViewSet):
    """提示词配置视图集"""
    queryset = PromptConfig.objects.all()
    serializer_class = PromptConfigSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # 按提示词类型过滤
        prompt_type = self.request.query_params.get('prompt_type')
        if prompt_type:
            queryset = queryset.filter(prompt_type=prompt_type)
        
        # 按是否启用过滤
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['get'])
    def load_defaults(self, request):
        """加载默认提示词"""
        try:
            # 独立线 Skill 包内 prompts 目录（运行期真源，已进镜像）
            _SKILL_PROMPTS_DIR = os.path.join(
                settings.BASE_DIR, 'apps', 'requirement_analysis',
                'independent_skill', 'prompts'
            )
            # 读取用例编写提示词
            writer_prompt_path = os.path.join(_SKILL_PROMPTS_DIR, 'tester.md')
            # 读取用例评审提示词
            reviewer_prompt_path = os.path.join(_SKILL_PROMPTS_DIR, 'tester_pro.md')
            
            defaults = {}

            # 用例编写提示词
            try:
                with open(writer_prompt_path, 'r', encoding='utf-8') as f:
                    defaults['writer'] = f.read()
            except FileNotFoundError:
                defaults['writer'] = """你是一名资深的QA高级专家，擅长编写高质量的测试用例。

请根据以下需求描述，生成详细的测试用例。

要求：
1. 测试用例应该覆盖正常流程、异常流程和边界条件
2. 每个测试用例包含：用例编号、用例标题、前置条件、测试步骤、预期结果
3. 测试步骤要详细、清晰、可执行
4. 考虑不同的用户角色和权限
5. 关注数据验证和错误处理

请以结构化的格式输出测试用例。"""

            # 用例评审提示词
            try:
                with open(reviewer_prompt_path, 'r', encoding='utf-8') as f:
                    defaults['reviewer'] = f.read()
            except FileNotFoundError:
                defaults['reviewer'] = """你是一名资深的测试经理，负责评审测试用例的质量。

请对以下测试用例进行评审，并提供改进意见。

评审要点：
1. 测试用例是否覆盖了主要功能点
2. 测试步骤是否清晰、完整、可执行
3. 预期结果是否准确、具体
4. 是否遗漏了重要的测试场景
5. 是否需要补充边界条件测试

请提供：
1. 总体评价
2. 具体的改进建议
3. 补充的测试场景（如有）
4. 修改后的测试用例（如需要）"""

            # 需求分析提示词（独立线 Skill 包内）
            analyzer_prompt_path = os.path.join(_SKILL_PROMPTS_DIR, 'tester_analyzer.md')
            try:
                with open(analyzer_prompt_path, 'r', encoding='utf-8') as f:
                    defaults['analyzer'] = f.read()
            except FileNotFoundError:
                defaults['analyzer'] = """你是一名资深的需求分析与拆解专家（Test Architect）。

你的唯一任务是：把用户提供的"人话/截图/零散描述"翻译成"机器和人都能看懂的结构化逻辑表"。

## 核心能力

1. **需求理解**：从模糊的自然语言中提取明确的业务规则和功能点
2. **结构化拆解**：将复杂需求拆解为可验证的功能模块、用户故事、验收标准
3. **补全隐含信息**：识别用户未明说但必需的前置条件、依赖关系、异常路径
4. **输出标准化**：输出结构化的分析表，供后续步骤（如AI写用例）直接消费

## 输入可能的形式

- 一段自然语言需求描述
- 产品原型/设计稿截图及说明
- 几条零散的用户反馈或会议纪要
- 已有的半成品文档

## 输出格式要求

请严格按以下 Markdown 表格格式输出：

### 1. 需求概述
| 字段 | 内容 |
|------|------|
| 需求名称 | （概括性名称） |
| 优先级 | P0/P1/P2/P3 |
| 涉及模块 | （涉及的系统/模块列表） |
| 前置依赖 | （前置条件或依赖项） |

### 2. 功能拆解表
| 编号 | 功能点 | 详细描述 | 验收标准 | 优先级 |
|------|--------|----------|----------|--------|
| F-01 | ... | ... | ... | ... |

### 3. 深度业务逻辑拆解与插接矩阵（核心输出）
> 此表是测试设计的核心输入，每个功能点必须逐行展开。聚焦于：**数据流转、状态转换、边界条件、模块间联动**。

| 模块 | 功能点 | 前置条件 | 触发动作 | 预期结果 | 测试关注点与数据边界(关键) | 插接/关联影响 |
|------|--------|---------|---------|---------|---------------------------|-------------|
| [模块名] | [功能名] | [环境/状态] | [操作] | [正常反馈] | 1. 输入值边界：如时长0~24h<br>2. 状态流转：A→B→C<br>3. 精度要求：秒级/毫秒级<br>4. 特殊场景：跨天/闰年 | [此处填写：是否联动其他模块？是否改变历史数据？] |

**填写要求：**
- 「测试关注点与数据边界」用**编号列表**列出至少3个关键测试维度（输入值边界、状态流转、精度/格式、特殊场景）
- 「插接/关联影响」用方括号标注该功能对其他模块的副作用或数据一致性影响
- 每个有业务价值的功能点都应有一行或多行，不要留空占位行

### 4. 用户角色与权限矩阵（如适用）
| 角色 | 可操作功能 | 数据可见范围 | 特殊约束 |
|------|-----------|-------------|---------|
| ... | ... | ... | ... |

### 5. 异常场景清单
| 编号 | 异常场景 | 触发条件 | 预期处理方式 |
|------|---------|---------|-------------|
| E-01 | ... | ... | ... |

### 6. 待确认事项
| 编号 | 问题 | 建议 | 阻塞风险 |
|------|------|------|---------|
| Q-01 | ... | ... | 高/中/低 |

## 工作原则

- 不做假设，不确定的标记为"待确认"
- 保持颗粒度一致：每个功能点应在一个合理范围内可被独立测试
- 使用用户能理解的业务语言，而非纯技术术语
- 如输入含截图描述，将视觉元素映射到对应的功能交互
- **插接矩阵是重点**：务必把每个功能的"上下游关系""数据边界""联动副作用"写清楚，这是测试用例设计的直接依据"""
            
            return Response({
                'message': '默认提示词加载成功',
                'defaults': defaults
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"加载默认提示词失败: {e}")
            return Response(
                {'error': f'加载失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            config = self.get_object()
            config.is_active = True
            config.save()
            return Response({
                'message': '提示词配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用提示词配置失败: {e}")
            return Response({
                'error': f'启用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()
            return Response({
                'message': '提示词配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用提示词配置失败: {e}")
            return Response({
                'error': f'禁用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class GenerationConfigViewSet(viewsets.ModelViewSet):
    """生成行为配置视图集"""
    queryset = GenerationConfig.objects.all()
    serializer_class = GenerationConfigSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.order_by('-created_at')

    @action(detail=False, methods=['get'])
    def active(self, request):
        """获取活跃的生成配置"""
        try:
            config = GenerationConfig.get_active_config()
            if not config:
                return Response({
                    'error': '未找到活跃的生成配置，请先创建并启用一个配置'
                }, status=status.HTTP_404_NOT_FOUND)

            serializer = self.get_serializer(config)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"获取活跃生成配置失败: {e}")
            return Response({
                'error': f'获取失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            # 禁用其他所有配置
            GenerationConfig.objects.all().update(is_active=False)

            # 启用当前配置
            config = self.get_object()
            config.is_active = True
            config.save()

            return Response({
                'message': '生成配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用生成配置失败: {e}")
            return Response({
                'error': f'启用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()

            return Response({
                'message': '生成配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用生成配置失败: {e}")
            return Response({
                'error': f'禁用失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _recover_stale_task(task, stale_minutes=30):
    """自动回收卡死的生成任务：容器重启/进程被杀后，后台线程已死。
    若进行中状态超过 stale_minutes 无更新，按已有产物恢复，避免前端永久转圈。"""
    from django.utils import timezone as _tz
    if task.status not in ('generating', 'reviewing', 'revising'):
        return
    if not task.updated_at:
        return
    elapsed = (_tz.now() - task.updated_at).total_seconds()
    if elapsed < stale_minutes * 60:
        return
    if task.final_test_cases or task.review_feedback:
        task.status = 'completed'
        task.progress = 100
        task.save(update_fields=['status', 'progress'])
        logger.warning('[stale-recover] task %s stale %.0fs -> completed（保留已有产物）', task.task_id, elapsed)
    else:
        task.status = 'failed'
        task.error_message = (
            f'任务执行超时（已超过 {stale_minutes} 分钟无更新，'
            f'可能因线程中断/容器重启导致）。请重新触发生成。'
        )
        task.save(update_fields=['status', 'error_message'])
        logger.warning('[stale-recover] task %s stale %.0fs -> failed', task.task_id, elapsed)


class TestCaseGenerationTaskViewSet(viewsets.ModelViewSet):
    """测试用例生成任务视图集"""
    queryset = TestCaseGenerationTask.objects.all()
    serializer_class = TestCaseGenerationTaskSerializer
    pagination_class = TestCaseGenerationTaskPagination
    http_method_names = ['get', 'post', 'patch', 'delete']  # 允许GET、POST、PATCH和DELETE方法
    lookup_field = 'task_id'  # 使用task_id作为查找字段

    def retrieve(self, request, *args, **kwargs):
        task = self.get_object()
        _recover_stale_task(task)
        return super().retrieve(request, *args, **kwargs)
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # 安全检查：确保request有query_params属性
        if not hasattr(self.request, 'query_params'):
            return queryset.order_by('-created_at')
        
        # 按状态过滤
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        
        # 按创建者过滤
        created_by = self.request.query_params.get('created_by')
        if created_by:
            queryset = queryset.filter(created_by_id=created_by)
        
        # 按版本过滤
        version_param = self.request.query_params.get('version')
        if version_param:
            queryset = queryset.filter(version_id=version_param)
        
        # 按项目过滤
        project_param = self.request.query_params.get('project')
        if project_param:
            queryset = queryset.filter(project_id=project_param)
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'])
    def analyze(self, request):
        """需求拆解：把需求文本/截图翻译成结构化逻辑表（需求分析与拆解专家角色）

        与 generate 不同，analyze 不创建生成任务、不写用例，仅即时返回
        由 analyzer 提示词驱动的结构化拆解结果，供人工确认后再决定后续动作。
        """
        try:
            requirement_text = (request.data.get('requirement_text') or '').strip()
            project_id = request.data.get('project_id')
            images = request.data.get('images') or []  # base64 data url 列表

            if not requirement_text and not images:
                return Response(
                    {'error': '请提供需求文本或截图'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 取需求分析提示词（核心：需求分析与拆解专家角色）
            analyzer_prompt = PromptConfig.get_active_config('analyzer')
            if not analyzer_prompt:
                return Response(
                    {'error': '未找到可用的需求分析提示词配置，请先在「提示词配置」中创建并启用「需求分析提示词」'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 取模型：有图片优先视觉模型，否则 analyzer 角色，再 fallback writer
            if images:
                model_config = AIModelConfig.objects.filter(role='writer_vision', is_active=True).first()
            else:
                model_config = AIModelConfig.objects.filter(role='analyzer', is_active=True).first()
            if not model_config:
                model_config = AIModelConfig.objects.filter(role='writer', is_active=True).first()
            if not model_config:
                return Response(
                    {'error': '未找到可用的AI模型配置（需求拆解专家/视觉分析专家/用例编写）'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 组装 user message（支持多模态：文本 + 截图）
            if images:
                user_content = [{
                    "type": "text",
                    "text": requirement_text or "请分析以下截图中的需求，并拆解为结构化逻辑表"
                }]
                for img in images:
                    if isinstance(img, str) and img.startswith('data:'):
                        url = img
                    elif isinstance(img, str):
                        url = f"data:image/png;base64,{img}"
                    else:
                        continue
                    user_content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                user_content = requirement_text

            messages = [
                {"role": "system", "content": analyzer_prompt.content},
                {"role": "user", "content": user_content},
            ]

            # 同步执行 AI 调用（复用现有事件循环模式，即时返回结果）
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                api_result = loop.run_until_complete(
                    AIModelService.call_openai_compatible_api(model_config, messages)
                )
            finally:
                loop.close()

            result_text = api_result['choices'][0]['message']['content']
            return Response({'result': result_text}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"需求拆解失败: {e}")
            return Response(
                {'error': f'需求拆解失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """创建新的测试用例生成任务"""
        try:
            user_info = f"user={request.user.username if request.user.is_authenticated else 'anon'}"
            logger.info(f"[CASE-GEN] 开始创建任务 - {user_info}, title={request.data.get('title', '?')[:50]}")
            serializer = TestCaseGenerationRequestSerializer(data=request.data)
            if not serializer.is_valid():
                logger.warning(f"[CASE-GEN] 参数校验失败: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            validated_data = serializer.validated_data
            
            # 获取活跃的配置
            writer_config = None
            reviewer_config = None
            writer_prompt = None
            reviewer_prompt = None
            
            if validated_data.get('use_writer_model', True):
                # 优先查找任意启用的编写模型配置
                writer_config = AIModelConfig.objects.filter(role='writer', is_active=True).first()
                
                if not writer_config:
                    logger.error(f"[CASE-GEN] writer 模型未配置或未启用")
                    return Response(
                        {'error': '未找到可用的测试用例编写模型配置'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                writer_prompt = PromptConfig.get_active_config('writer')
                if not writer_prompt:
                    logger.error(f"[CASE-GEN] writer prompt 未配置或未启用")
                    return Response(
                        {'error': '未找到可用的测试用例编写提示词配置'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            if validated_data.get('use_reviewer_model', True):
                # 优先查找任意启用的评审模型配置
                reviewer_config = AIModelConfig.objects.filter(role='reviewer', is_active=True).first()
                
                if not reviewer_config:
                    logger.error(f"[CASE-GEN] reviewer 模型未配置或未启用")
                    return Response(
                        {'error': '未找到可用的测试用例评审模型配置'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                reviewer_prompt = PromptConfig.get_active_config('reviewer')
                if not reviewer_prompt:
                    logger.error(f"[CASE-GEN] reviewer prompt 未配置或未启用")
                    return Response(
                        {'error': '未找到可用的测试用例评审提示词配置'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # 创建任务
            task_data = {
                'title': validated_data['title'],
                'requirement_text': validated_data['requirement_text'],
                'writer_model_config': writer_config.id if writer_config else None,
                'reviewer_model_config': reviewer_config.id if reviewer_config else None,
                'writer_prompt_config': writer_prompt.id if writer_prompt else None,
                'reviewer_prompt_config': reviewer_prompt.id if reviewer_prompt else None,
            }

            # 如果请求中包含项目ID，添加到任务数据中
            if 'project' in validated_data and validated_data['project']:
                task_data['project'] = validated_data['project']

            # 如果请求中包含版本ID，添加到任务数据中
            if 'version' in validated_data and validated_data['version']:
                task_data['version'] = validated_data['version']

            # 如果请求中包含功能模块ID，添加到任务数据中
            if 'feature_module' in validated_data and validated_data['feature_module']:
                task_data['feature_module'] = validated_data['feature_module']

            # 如果请求中包含测试点ID，添加到任务数据中
            if 'test_point' in validated_data and validated_data['test_point']:
                task_data['test_point'] = validated_data['test_point']

            # 历史缺陷复盘注入（方案 A）：仅在显式传入时绑定；不传则为 NULL，行为不变
            defect_review_id = validated_data.get('defect_review')
            if defect_review_id:
                from apps.zentao.models import DefectReviewReport
                review = DefectReviewReport.objects.filter(
                    id=defect_review_id, status='success'
                ).first()
                if not review:
                    return Response(
                        {'error': '关联的历史缺陷复盘不存在或尚未完成分析'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                task_data['defect_review'] = review.id
                task_data['defect_review_detail_level'] = (
                    validated_data.get('defect_review_detail_level') or ''
                )
                logger.info(
                    f"[CASE-GEN] 关联历史缺陷复盘 review={review.id} tps={len(review.testpoints_json or [])}"
                )

            # 处理生成模式
            generation_mode = validated_data.get('generation_mode', 'smart')
            task_data['generation_mode'] = generation_mode

            # 处理需求拆解阶段确认的问答对（用于用例生成上下文注入）
            confirmed_answers = request.data.get('confirmed_answers', '')
            if confirmed_answers:
                task_data['confirmed_answers'] = confirmed_answers

            # 处理输出模式：优先使用用户指定的，否则使用生成行为配置的默认值
            output_mode = request.data.get('output_mode')
            if output_mode and output_mode in ['stream', 'complete']:
                task_data['output_mode'] = output_mode
            else:
                # 从生成行为配置中读取默认值
                from .models import GenerationConfig
                gen_config = GenerationConfig.get_active_config()
                if gen_config:
                    task_data['output_mode'] = gen_config.default_output_mode
                else:
                    # 如果没有配置，默认使用流式输出
                    task_data['output_mode'] = 'stream'

            
            task_serializer = TestCaseGenerationTaskSerializer(
                data=task_data, 
                context={'request': request}
            )
            
            if task_serializer.is_valid():
                task = task_serializer.save()
                logger.info(f"[CASE-GEN] 任务创建成功 task_id={task.task_id}, mode={task.output_mode}, gen_mode={task.generation_mode}")
                
                # 异步执行生成任务
                def run_generation_task():
                    try:
                        import threading
                        
                        def execute_task():
                            try:
                                import django.db
                                # 关闭主线程的数据库连接，让新线程创建自己的连接
                                django.db.connections.close_all()

                                # 注册取消事件：/cancel/ 接口会 set 它，本线程在阶段之间和流式回调中检查
                                cancel_event = _register_generation_task(task.task_id)

                                def _raise_if_cancelled():
                                    """阶段之间调用：用户已取消则抛内部信号，跳过后续所有 LLM 调用"""
                                    if cancel_event.is_set():
                                        raise _GenerationCancelled()

                                # 更新任务状态
                                task.status = 'generating'
                                task.progress = 10
                                logger.info(f"[CASE-GEN-THREAD] 后台线程启动 task_id={task.task_id}")
                                task.save()

                                # 读取生成行为配置
                                from .models import GenerationConfig
                                gen_config = GenerationConfig.get_active_config()

                                # 获取配置参数，设置默认值
                                enable_auto_review = gen_config.enable_auto_review if gen_config else True
                                review_timeout = gen_config.review_timeout if gen_config else 120

                                logger.info(f"任务 {task.task_id} 使用生成配置: auto_review={enable_auto_review}, review_timeout={review_timeout}s")

                                # 预取所有 ForeignKey 数据 + 知识库上下文 + 异常场景统计
                                # 必须在 async 上下文之前完成，避免 async def 中触发同步 ORM 懒加载导致 SynchronousOnlyOperation
                                # 全部挂到 task 实例上，供下方 generate_test_cases_stream / review_test_cases_stream 在 async 内只读缓存属性
                                task._pre_writer_prompt = task.writer_prompt_config.content if task.writer_prompt_config else ""
                                task._pre_reviewer_prompt = task.reviewer_prompt_config.content if task.reviewer_prompt_config else ""
                                # 触发 FK 缓存（避免 async 内懒加载）
                                task._pre_writer_model = task.writer_model_config
                                task._pre_reviewer_model = task.reviewer_model_config
                                # 知识库上下文（含 project FK + ProjectKnowledge 查询）
                                task._pre_fetched_knowledge_context = AIModelService.build_knowledge_context(task)
                                # 异常场景统计（查 CoverageReport，必须在 async 外完成，否则 async 内 SynchronousOnlyOperation）
                                task._pre_abnormal_scene = AIModelService._build_abnormal_scene_section(task)
                                # 历史缺陷复盘测试点注入（方案 A：仅当本任务显式关联了复盘报告）
                                # 必须在 async 外完成查询；未关联时为空串，行为与旧版完全一致
                                task._pre_defect_testpoints = AIModelService._build_defect_review_section(task)
                                logger.info(f"[PREFETCH] writer_prompt={bool(task._pre_writer_prompt)}, reviewer_prompt={bool(task._pre_reviewer_prompt)}, kb={len(task._pre_fetched_knowledge_context)}, abnormal_scenes={len(task._pre_abnormal_scene)}, defect_tp={len(task._pre_defect_testpoints)}")

                                loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop)

                                try:
                                    # 根据输出模式选择不同的生成方式
                                    if task.output_mode == 'stream':
                                        # 流式模式：实时保存到stream_buffer
                                        # 生成前先设置初始状态
                                        task.stream_buffer = ''
                                        task.stream_position = 0
                                        task.save()

                                        # 定义同步保存函数
                                        def save_stream_buffer(content):
                                            """同步保存流式内容到数据库。

                                            ⚠️ 2026-11-05：该函数被 `sync_to_async` 在
                                            **线程池的不同线程**中调用，而流式生成每 10 个
                                            chunk 就会触发一次。线程池线程复用 + 未清理连接，
                                            会让连接在 MySQL 侧快速堆积（实测短时间冲到 150+，
                                            伴随 `(2013, 'Lost connection to MySQL server
                                            during query')`）。这里保存完立即归还连接。
                                            """
                                            from django.db import close_old_connections
                                            try:
                                                task.stream_buffer = content
                                                task.stream_position = len(content)
                                                task.last_stream_update = timezone.now()
                                                task.save(update_fields=['stream_buffer', 'stream_position', 'last_stream_update'])
                                            finally:
                                                close_old_connections()

                                        # 转换为异步函数
                                        async_save_stream_buffer = sync_to_async(save_stream_buffer)

                                        async def stream_callback(chunk):
                                            """流式回调：实时保存每个chunk到数据库"""
                                            # 用户已取消：中断流式读取，避免继续消耗 token
                                            if cancel_event.is_set():
                                                raise _GenerationCancelled()
                                            # 先追加到内存中的buffer
                                            task.stream_buffer += chunk
                                            task.stream_position = len(task.stream_buffer)
                                            task.last_stream_update = timezone.now()

                                            # 每10个chunk或当chunk较大时保存一次
                                            if task.stream_position % 500 < 20 or len(chunk) > 100:
                                                try:
                                                    await async_save_stream_buffer(task.stream_buffer)
                                                except Exception as save_error:
                                                    logger.warning(f"保存流式内容失败: {save_error}")

                                        # 生成测试用例
                                        task.progress = 30
                                        task.save()

                                        generated_cases = loop.run_until_complete(
                                            AIModelService.generate_test_cases_stream(task, callback=stream_callback)
                                        )

                                        # 生成完成后，确保最终的流式内容被保存
                                        if task.stream_buffer:
                                            save_stream_buffer(task.stream_buffer)

                                        task.generated_test_cases = generated_cases
                                        task.progress = 60
                                        task.save()

                                        # 进入评审前检查：用户已取消则不再调用评审/改进模型
                                        _raise_if_cancelled()

                                        # 流式评审和改进（根据生成配置决定是否执行）
                                        # 注意：必须用 _id 后缀检查，不能用懒加载 ForeignKey（线程中连接可能失效）
                                        if enable_auto_review and task.reviewer_model_config_id and task.reviewer_prompt_config_id:
                                            try:
                                                task.status = 'reviewing'
                                                task.progress = 70
                                                task.save()

                                                logger.info(f"开始流式评审任务 {task.task_id}")

                                                # 评审内容缓存
                                                review_buffer = []

                                                def save_review_buffer(content):
                                                    """同步保存评审内容"""
                                                    task.review_feedback = content
                                                    task.save(update_fields=['review_feedback'])

                                                async_save_review = sync_to_async(save_review_buffer)

                                                async def review_stream_callback(chunk):
                                                    """流式评审回调"""
                                                    if cancel_event.is_set():
                                                        raise _GenerationCancelled()
                                                    review_buffer.append(chunk)
                                                    current_length = sum(len(c) for c in review_buffer)

                                                    # 每100字符保存一次
                                                    if current_length % 100 < 20 or len(chunk) > 50:
                                                        try:
                                                            content = ''.join(review_buffer)
                                                            await async_save_review(content)
                                                        except Exception as save_error:
                                                            logger.warning(f"保存评审内容失败: {save_error}")

                                                try:
                                                    # 移除超时限制，允许大文档完整评审
                                                    review_feedback = loop.run_until_complete(
                                                        AIModelService.review_test_cases_stream(
                                                            task, generated_cases, callback=review_stream_callback
                                                        )
                                                    )
                                                    # 保存最终评审内容
                                                    if review_buffer:
                                                        task.review_feedback = ''.join(review_buffer)
                                                        task.review_count = 1
                                                        task.review_updated_at = timezone.now()
                                                        AIModelService.append_review_history(task, task.review_feedback)
                                                        task.save(update_fields=['review_feedback', 'review_count', 'review_updated_at', 'generation_log'])
                                                    logger.info(f"任务 {task.task_id} 流式评审完成")

                                                    # 自动提取不确定项并填充 human_feedback
                                                    try:
                                                        auto_feedback = AIModelService.extract_uncertain_items_from_review(task.review_feedback)
                                                        if auto_feedback:
                                                            task.human_feedback = auto_feedback
                                                            task.save(update_fields=['human_feedback'])
                                                            logger.info(f"任务 {task.task_id} 自动提取不确定项完成")
                                                        else:
                                                            logger.info(f"任务 {task.task_id} 评审中未发现不确定项")
                                                    except Exception as extract_error:
                                                        logger.warning(f"自动提取不确定项失败（非关键错误）: {extract_error}")

                                                    # 根据评审意见改进测试用例（自动执行）
                                                    logger.info(f"任务 {task.task_id} 开始根据评审意见改进测试用例")
                                                    # 进入改进阶段前检查：已取消则跳过改写（改写最耗 token）
                                                    _raise_if_cancelled()

                                                    task.status = 'revising'
                                                    task.progress = 85
                                                    task.final_test_cases = ''  # 清空，准备流式写入
                                                    task.save()

                                                    try:
                                                        # 定义同步保存函数
                                                        def save_final_buffer(content):
                                                            """同步保存最终用例内容"""
                                                            task.final_test_cases = content
                                                            task.save(update_fields=['final_test_cases'])

                                                        # 转换为异步函数
                                                        async_save_final = sync_to_async(save_final_buffer)

                                                        # 创建流式回调函数，实时更新final_test_cases
                                                        async def final_callback(chunk):
                                                            """流式回调：实时保存最终用例到数据库"""
                                                            if cancel_event.is_set():
                                                                raise _GenerationCancelled()
                                                            # 实时追加到final_test_cases并保存
                                                            task.final_test_cases = (task.final_test_cases or '') + chunk

                                                            # 每100字符或chunk较大时保存一次
                                                            current_length = len(task.final_test_cases)
                                                            if current_length % 100 < 20 or len(chunk) > 50:
                                                                try:
                                                                    await async_save_final(task.final_test_cases)
                                                                except Exception as save_error:
                                                                    logger.warning(f"保存最终用例失败: {save_error}")

                                                        # 添加超时保护，避免任务一直卡住（使用配置的超时时间）
                                                        try:
                                                            revised_cases = loop.run_until_complete(
                                                                asyncio.wait_for(
                                                                    AIModelService.revise_test_cases_based_on_review(
                                                                        task, generated_cases, task.review_feedback,
                                                                        callback=final_callback
                                                                    ),
                                                                    timeout=review_timeout  # 使用配置的超时时间（秒）
                                                                )
                                                            )
                                                        except asyncio.TimeoutError:
                                                            logger.error(f"任务 {task.task_id} 改进阶段超时（{review_timeout}秒），使用原始用例")
                                                            # 超时时使用原始生成的用例，不再抛出异常
                                                            revised_cases = generated_cases
                                                        # 始终使用返回的完整内容，避免流式输出被截断导致数据丢失
                                                        # revised_cases 是完整的返回值，task.final_test_cases 只是流式回调的中间状态
                                                        if revised_cases and len(revised_cases) > 0:
                                                            # 检测并修复不完整的最后一条用例
                                                            revised_cases = AIModelService.fix_incomplete_last_case(revised_cases)

                                                            # 按用例编号排序后再保存
                                                            sorted_cases = AIModelService.sort_test_cases_by_id(revised_cases)
                                                            # 重新编号使编号连续
                                                            renumbered_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                            task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                                renumbered_cases, task.generation_mode)[0]
                                                            logger.info(f"任务 {task.task_id} 测试用例改进完成 (revised_cases长度: {len(revised_cases)}, 最终保存长度: {len(task.final_test_cases)})")
                                                            task.status = 'completed'
                                                            task.progress = 100
                                                            task.completed_at = timezone.now()
                                                            task.save()
                                                        else:
                                                            # 如果返回为空，保留流式回调保存的内容
                                                            logger.warning(f"任务 {task.task_id} 改进返回为空，使用流式回调保存的内容 (长度: {len(task.final_test_cases) if task.final_test_cases else 0})")
                                                    except Exception as revise_error:
                                                        logger.warning(f"任务 {task.task_id} 改进测试用例失败: {revise_error}，使用原始用例")
                                                        # 按用例编号排序后再保存
                                                        sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                        # 重新编号使编号连续
                                                        task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                        AIModelService.renumber_test_cases(sorted_cases),
                                                        task.generation_mode)[0]
                                                        task.status = 'completed'
                                                        task.progress = 100
                                                        task.completed_at = timezone.now()
                                                        task.save()

                                                except Exception as inner_error:
                                                    logger.warning(f"任务 {task.task_id} 流式评审过程异常: {inner_error}")
                                                    task.review_feedback = f"评审过程出现异常: {str(inner_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                    # 按用例编号排序后再保存
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    # 重新编号使编号连续
                                                    task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                        AIModelService.renumber_test_cases(sorted_cases),
                                                        task.generation_mode)[0]
                                                    task.status = 'completed'
                                                    task.progress = 100
                                                    task.completed_at = timezone.now()
                                                    task.save()

                                            except Exception as review_error:
                                                logger.error(f"流式评审任务 {task.task_id} 失败: {review_error}")
                                                # 按用例编号排序后再保存
                                                sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                    AIModelService.renumber_test_cases(sorted_cases),
                                                    task.generation_mode)[0]
                                                task.review_feedback = f"评审失败: {str(review_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                task.status = 'completed'
                                                task.progress = 100
                                                task.completed_at = timezone.now()
                                                task.save()
                                        else:
                                            # 按用例编号排序后再保存
                                            sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                            # 重新编号使编号连续 + 按生成模式截断
                                            task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                AIModelService.renumber_test_cases(sorted_cases),
                                                task.generation_mode)[0]
                                            logger.info(f"任务 {task.task_id} 跳过评审，直接使用生成的测试用例")
                                            task.status = 'completed'
                                            task.progress = 100
                                            task.completed_at = timezone.now()
                                            task.save()

                                    else:
                                        # 完整模式：原有逻辑
                                        task.progress = 30
                                        task.save()

                                        generated_cases = loop.run_until_complete(
                                            AIModelService.generate_test_cases(task)
                                        )

                                        task.generated_test_cases = generated_cases
                                        task.progress = 60
                                        task.save()

                                        # 评审和改进测试用例（根据生成配置决定是否执行）
                                        # 注意：必须用 _id 后缀检查，不能用懒加载 ForeignKey（线程中连接可能失效）
                                        if enable_auto_review and task.reviewer_model_config_id and task.reviewer_prompt_config_id:
                                            try:
                                                task.status = 'reviewing'
                                                task.progress = 70
                                                task.save()

                                                logger.info(f"开始评审任务 {task.task_id}")

                                                # 移除超时限制，允许大文档完整评审
                                                try:
                                                    review_feedback = loop.run_until_complete(
                                                        AIModelService.review_test_cases(task, generated_cases)
                                                    )
                                                    task.review_feedback = review_feedback
                                                    task.review_count = 1
                                                    task.review_updated_at = timezone.now()
                                                    AIModelService.append_review_history(task, review_feedback)
                                                    task.save(update_fields=['review_feedback', 'review_count', 'review_updated_at', 'generation_log'])
                                                    logger.info(f"任务 {task.task_id} 评审完成")

                                                    # 自动提取不确定项并填充 human_feedback
                                                    try:
                                                        auto_feedback = AIModelService.extract_uncertain_items_from_review(task.review_feedback)
                                                        if auto_feedback:
                                                            task.human_feedback = auto_feedback
                                                            task.save(update_fields=['human_feedback'])
                                                            logger.info(f"任务 {task.task_id} 自动提取不确定项完成")
                                                        else:
                                                            logger.info(f"任务 {task.task_id} 评审中未发现不确定项")
                                                    except Exception as extract_error:
                                                        logger.warning(f"自动提取不确定项失败（非关键错误）: {extract_error}")

                                                    # 根据评审意见改进测试用例（自动执行）
                                                    logger.info(f"任务 {task.task_id} 开始根据评审意见改进测试用例")
                                                    # 进入改进阶段前检查：已取消则跳过改写（改写最耗 token）
                                                    _raise_if_cancelled()

                                                    task.status = 'revising'
                                                    task.progress = 85
                                                    task.final_test_cases = ''  # 清空，准备流式写入
                                                    task.save()

                                                    try:
                                                        # 定义同步保存函数
                                                        def save_final_buffer_full(content):
                                                            """同步保存最终用例内容"""
                                                            task.final_test_cases = content
                                                            task.save(update_fields=['final_test_cases'])

                                                        # 转换为异步函数
                                                        async_save_final_full = sync_to_async(save_final_buffer_full)

                                                        # 创建流式回调函数，实时更新final_test_cases
                                                        async def final_callback_full(chunk):
                                                            """流式回调：实时保存最终用例到数据库"""
                                                            if cancel_event.is_set():
                                                                raise _GenerationCancelled()
                                                            # 实时追加到final_test_cases并保存
                                                            task.final_test_cases = (task.final_test_cases or '') + chunk

                                                            # 每100字符或chunk较大时保存一次
                                                            current_length = len(task.final_test_cases)
                                                            if current_length % 100 < 20 or len(chunk) > 50:
                                                                try:
                                                                    await async_save_final_full(task.final_test_cases)
                                                                except Exception as save_error:
                                                                    logger.warning(f"保存最终用例失败: {save_error}")

                                                        # 添加超时保护，避免任务一直卡住（使用配置的超时时间）
                                                        try:
                                                            revised_cases = loop.run_until_complete(
                                                                asyncio.wait_for(
                                                                    AIModelService.revise_test_cases_based_on_review(
                                                                        task, generated_cases, task.review_feedback,
                                                                        callback=final_callback_full
                                                                    ),
                                                                    timeout=review_timeout  # 使用配置的超时时间（秒）
                                                                )
                                                            )
                                                        except asyncio.TimeoutError:
                                                            logger.error(f"任务 {task.task_id} 改进阶段超时（{review_timeout}秒），使用原始用例")
                                                            # 超时时使用原始生成的用例，不再抛出异常
                                                            revised_cases = generated_cases
                                                        # 始终使用返回的完整内容，避免流式输出被截断导致数据丢失
                                                        # revised_cases 是完整的返回值，task.final_test_cases 只是流式回调的中间状态
                                                        if revised_cases and len(revised_cases) > 0:
                                                            # 检测并修复不完整的最后一条用例
                                                            revised_cases = AIModelService.fix_incomplete_last_case(revised_cases)

                                                            # 按用例编号排序后再保存
                                                            sorted_cases = AIModelService.sort_test_cases_by_id(revised_cases)
                                                            # 重新编号使编号连续
                                                            renumbered_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                            task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                                renumbered_cases, task.generation_mode)[0]
                                                            logger.info(f"任务 {task.task_id} 测试用例改进完成 (revised_cases长度: {len(revised_cases)}, 最终保存长度: {len(task.final_test_cases)})")
                                                            task.status = 'completed'
                                                            task.progress = 100
                                                            task.completed_at = timezone.now()
                                                            task.save()
                                                        else:
                                                            # 如果返回为空，保留流式回调保存的内容
                                                            logger.warning(f"任务 {task.task_id} 改进返回为空，使用流式回调保存的内容 (长度: {len(task.final_test_cases) if task.final_test_cases else 0})")
                                                    except Exception as revise_error:
                                                        logger.warning(f"任务 {task.task_id} 改进测试用例失败: {revise_error}，使用原始用例")
                                                        # 按用例编号排序后再保存
                                                        sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                        # 重新编号使编号连续
                                                        task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                        AIModelService.renumber_test_cases(sorted_cases),
                                                        task.generation_mode)[0]
                                                        task.status = 'completed'
                                                        task.progress = 100
                                                        task.completed_at = timezone.now()
                                                        task.save()

                                                except Exception as inner_error:
                                                    logger.warning(f"任务 {task.task_id} 评审过程异常: {inner_error}")
                                                    task.review_feedback = f"评审过程出现异常: {str(inner_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                    # 按用例编号排序后再保存
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    # 重新编号使编号连续
                                                    task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                        AIModelService.renumber_test_cases(sorted_cases),
                                                        task.generation_mode)[0]
                                                    task.status = 'completed'
                                                    task.progress = 100
                                                    task.completed_at = timezone.now()
                                                    task.save()

                                            except Exception as review_error:
                                                logger.error(f"评审任务 {task.task_id} 失败: {review_error}")
                                                # 评审失败时，仍然使用生成的测试用例作为最终结果
                                                # 按用例编号排序后再保存
                                                sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                    AIModelService.renumber_test_cases(sorted_cases),
                                                    task.generation_mode)[0]
                                                task.review_feedback = f"评审失败: {str(review_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                task.status = 'completed'
                                                task.progress = 100
                                                task.completed_at = timezone.now()
                                                task.save()
                                        else:
                                            # 按用例编号排序后再保存
                                            sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                            # 重新编号使编号连续 + 按生成模式截断
                                            task.final_test_cases = AIModelService.enforce_case_count_limit(
                                                AIModelService.renumber_test_cases(sorted_cases),
                                                task.generation_mode)[0]
                                            logger.info(f"任务 {task.task_id} 跳过评审，直接使用生成的测试用例")
                                            task.status = 'completed'
                                            task.progress = 100
                                            task.completed_at = timezone.now()
                                            task.save()

                                    # 完成任务
                                    # 注意：不要直接调用task.save()，因为这会覆盖流式回调保存的final_test_cases
                                    # 从数据库重新获取最新的任务对象
                                    task.refresh_from_db()

                                    task.status = 'completed'
                                    task.progress = 100
                                    task.completed_at = timezone.now()
                                    task.save(update_fields=['status', 'progress', 'completed_at', 'final_test_cases'])
                                    logger.info(f"任务 {task.task_id} 已完成")
                                    
                                finally:
                                    try:
                                        # 清理异步生成器，防止 "Task was destroyed but it is pending" 警告
                                        loop.run_until_complete(loop.shutdown_asyncgens())
                                    except Exception as e:
                                        logger.warning(f"Error shutting down asyncgens: {e}")
                                    finally:
                                        loop.close()
                                    
                            except _GenerationCancelled:
                                # 用户主动取消：停止后续所有 LLM 调用，已生成的部分内容保留在库里
                                logger.info(f"任务 {task.task_id} 已被用户取消，停止生成")
                                try:
                                    task.refresh_from_db()
                                    task.status = 'cancelled'
                                    task.error_message = '用户取消生成'
                                    task.completed_at = timezone.now()
                                    task.save(update_fields=['status', 'error_message', 'completed_at'])
                                except Exception as cancel_save_error:
                                    logger.warning(f"任务 {task.task_id} 保存取消状态失败: {cancel_save_error}")
                            except Exception as e:
                                logger.error(f"生成任务执行失败: {e}")
                                task.status = 'failed'
                                task.error_message = str(e)
                                task.save()
                            finally:
                                # 无论成功/失败/取消，都要摘掉取消事件，避免字典无限增长
                                _unregister_generation_task(task.task_id)
                                # ⚠️ 2026-11-05：线程结束必须关闭数据库连接。
                                # Django 的 CONN_MAX_AGE=0 只在 HTTP 请求结束时关连接，
                                # 后台线程不受请求生命周期管理，连接会一直挂在 MySQL，
                                # 累积到 max_connections 后全站报
                                # OperationalError (1040, 'Too many connections') / 500。
                                from django.db import close_old_connections
                                close_old_connections()

                        # 在新线程中执行任务
                        thread = threading.Thread(
                            target=_thread_with_conn_cleanup(execute_task))
                        thread.daemon = True
                        thread.start()
                        
                    except Exception as e:
                        logger.error(f"[CASE-GEN-THREAD] 启动生成任务失败 task_id={task.task_id}: {e}", exc_info=True)
                        task.status = 'failed'
                        task.error_message = str(e)
                        task.save()
                
                # 启动异步任务
                run_generation_task()
                
                return Response({
                    'message': '测试用例生成任务已创建',
                    'task_id': task.task_id,
                    'task': task_serializer.data
                }, status=status.HTTP_201_CREATED)
            else:
                logger.error(f"[CASE-GEN] task_serializer 校验失败: {task_serializer.errors}")
                return Response(task_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            logger.error(f"[CASE-GEN] 创建生成任务时出错: {e}", exc_info=True)
            return Response(
                {'error': f'创建任务失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def progress(self, request, task_id=None):
        """获取任务进度"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()
            _recover_stale_task(task)

            return Response({
                'task_id': task.task_id,
                'status': task.status,
                'progress': task.progress,
                'generated_test_cases': task.generated_test_cases,
                'review_feedback': task.review_feedback,
                'final_test_cases': task.final_test_cases,
                'error_message': task.error_message,
                'completed_at': task.completed_at,
                # 注入审计：前端据此展示「本次实际用了哪几条历史测试点」
                'defect_review': task.defect_review_id,
                'defect_review_detail_level': task.defect_review_detail_level,
                'defect_review_used': task.defect_review_used,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"获取任务进度时出错: {e}")
            return Response(
                {'error': f'获取进度失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(
        detail=True,
        methods=['get'],
        url_path='stream_progress',
        renderer_classes=[PassThroughRenderer],
        permission_classes=[IsAuthenticated]  # 需要认证访问
    )
    def stream_progress_sse(self, request, task_id=None):
        """
        SSE流式进度推送接口
        实时推送任务的流式输出和进度更新
        不使用DRF的Response，避免content negotiation问题
        注意：EventSource不支持自定义headers，无法发送JWT token，所以允许通过session cookie访问
        """
        try:
            # 记录请求信息（用于调试）
            logger.info(f"SSE连接请求: task_id={task_id}, user={request.user}, authenticated={request.user.is_authenticated}, path={request.path}, origin={request.META.get('HTTP_ORIGIN', 'unknown')}")
    
            # 处理 CORS 预检请求
            if request.method == 'OPTIONS':
                from django.http import HttpResponse
                response = HttpResponse()
                response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
                response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response['Access-Control-Allow-Headers'] = 'Content-Type'
                response['Access-Control-Allow-Credentials'] = 'true'
                response['Access-Control-Max-Age'] = '86400'
                return response
    
            # 获取任务对象
            task = TestCaseGenerationTask.objects.filter(task_id=task_id).first()
            if not task:
                logger.warning(f"SSE连接失败: 任务未找到, task_id={task_id}")
                # 返回JSON错误而不是SSE
                from django.http import HttpResponse
                response = HttpResponse(
                    json.dumps({'error': '任务未找到'}),
                    status=404,
                    content_type='application/json'
                )
                response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
                response['Access-Control-Allow-Credentials'] = 'true'
                return response
    
            # 记录上次发送的stream_position
            last_sent_position = 0
            loop_count = 0  # 循环计数器
            last_review_length = 0  # 记录上次发送的评审内容长度
            last_final_length = 0  # 记录上次发送的最终用例长度
            last_status = ''  # 记录上次的任务状态
    
            def event_stream():
                nonlocal last_sent_position, loop_count, last_review_length, last_final_length, last_status
    
                while True:
                    loop_count += 1
    
                    # 从数据库重新获取任务状态
                    task.refresh_from_db()
    
                    # 检测状态变化，如果进入revising阶段，重置last_final_length
                    if task.status != last_status:
                        logger.info(f"SSE检测到状态变化: {last_status} -> {task.status}")
                        if task.status == 'revising':
                            logger.info(f"SSE: 进入revising阶段，重置last_final_length")
                            last_final_length = 0
                        last_status = task.status
    
                    # 每10次循环记录一次日志
                    if loop_count % 10 == 0:
                        logger.info(f"SSE stream loop #{loop_count}: task_status={task.status}, progress={task.progress}%, buffer_len={len(task.stream_buffer) if task.stream_buffer else 0}")
    
                    # 检查任务是否已完成或失败
                    if task.status in ['completed', 'failed', 'cancelled']:
                        logger.info(f"SSE任务结束: status={task.status}")
                        # 发送最终状态
                        final_status = json.dumps({'type': 'status', 'status': task.status, 'progress': task.progress}, ensure_ascii=False)
                        logger.info(f"SSE发送最终状态: {final_status}")
                        yield f"data: {final_status}\n\n"
    
                        # 如果是流式模式且有缓冲区内容，发送剩余内容
                        if task.output_mode == 'stream' and task.stream_buffer:
                            if last_sent_position < len(task.stream_buffer):
                                new_content = task.stream_buffer[last_sent_position:]
                                content_data = json.dumps({'type': 'content', 'content': new_content}, ensure_ascii=False)
                                logger.info(f"SSE发送剩余内容: {len(new_content)} 字符")
                                yield f"data: {content_data}\n\n"
                                last_sent_position = len(task.stream_buffer)
    
                        # 发送剩余的评审内容
                        if task.review_feedback:
                            if len(task.review_feedback) > last_review_length:
                                remaining_review = task.review_feedback[last_review_length:]
                                if remaining_review:
                                    review_data = json.dumps({'type': 'review_content', 'content': remaining_review}, ensure_ascii=False)
                                    logger.info(f"SSE发送剩余评审内容: {len(remaining_review)} 字符, 总长度: {len(task.review_feedback)}")
                                    yield f"data: {review_data}\n\n"
                                    last_review_length = len(task.review_feedback)
    
                        # 发送剩余的最终用例内容
                        if task.final_test_cases:
                            if len(task.final_test_cases) > last_final_length:
                                remaining_final = task.final_test_cases[last_final_length:]
                                if remaining_final:
                                    final_data = json.dumps({'type': 'final_content', 'content': remaining_final}, ensure_ascii=False)
                                    logger.info(f"SSE发送剩余最终用例: {len(remaining_final)} 字符, 总长度: {len(task.final_test_cases)}")
                                    yield f"data: {final_data}\n\n"
                                    last_final_length = len(task.final_test_cases)
    
                        # 发送完成信号
                        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                        logger.info(f"SSE流结束，总循环次数: {loop_count}")
    
                        # 添加短暂延迟，确保done信号被发送
                        time.sleep(0.1)
                        break
    
                    # 如果是流式模式，发送新增的内容
                    if task.output_mode == 'stream' and task.stream_buffer:
                        current_position = task.stream_position
                        if current_position > last_sent_position:
                            # 提取新增内容
                            new_content = task.stream_buffer[last_sent_position:current_position]
                            if new_content:
                                content_data = json.dumps({'type': 'content', 'content': new_content}, ensure_ascii=False)
                                logger.info(f"SSE发送新增内容: {len(new_content)} 字符, 总位置: {current_position}")
                                yield f"data: {content_data}\n\n"
                                last_sent_position = current_position
    
                    # 如果是评审阶段，发送评审内容
                    if task.status == 'reviewing' and task.review_feedback:
                        review_feedback = task.review_feedback
                        if review_feedback:
                            # 计算评审内容的增量
                            if len(review_feedback) > last_review_length:
                                new_review = review_feedback[last_review_length:]
                                if new_review:
                                    review_data = json.dumps({'type': 'review_content', 'content': new_review}, ensure_ascii=False)
                                    logger.info(f"SSE发送评审内容: {len(new_review)} 字符")
                                    yield f"data: {review_data}\n\n"
                                    last_review_length = len(review_feedback)
    
                    # 如果有最终用例，发送最终用例内容（在reviewing、revising或completed阶段）
                    if task.status in ['reviewing', 'revising', 'completed'] and task.final_test_cases:
                        final_cases = task.final_test_cases
                        if final_cases:
                            # 计算最终用例的增量
                            if len(final_cases) > last_final_length:
                                new_final = final_cases[last_final_length:]
                                if new_final:
                                    final_data = json.dumps({'type': 'final_content', 'content': new_final}, ensure_ascii=False)
                                    logger.info(f"SSE发送最终用例: {len(new_final)} 字符, 总长度: {len(final_cases)}, 阶段: {task.status}")
                                    yield f"data: {final_data}\n\n"
                                    last_final_length = len(final_cases)
    
                    # 发送进度更新
                    progress_data = json.dumps({'type': 'progress', 'status': task.status, 'progress': task.progress}, ensure_ascii=False)
                    yield f"data: {progress_data}\n\n"
    
                    # 短暂休眠，避免过度消耗资源
                    time.sleep(0.3)
    
            # 返回SSE流式响应
            response = StreamingHttpResponse(
                event_stream(),
                content_type='text/event-stream'
            )
    
            # 设置SSE相关的响应头（注意：不能设置Connection等hop-by-hop头部）
            response['Cache-Control'] = 'no-cache'
            response['X-Accel-Buffering'] = 'no'
    
            # 设置CORS头部
            origin = request.META.get('HTTP_ORIGIN', 'http://localhost:3000')
            if origin in ['http://localhost:3000', 'http://127.0.0.1:3000']:
                response['Access-Control-Allow-Origin'] = origin
            else:
                response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
            response['Access-Control-Allow-Credentials'] = 'true'
    
            logger.info(f"SSE连接建立成功: task_id={task_id}")
            return response
    
        except Exception as e:
            logger.error(f"SSE流式推送出错: {e}")
            import traceback
            traceback.print_exc()
            from django.http import HttpResponse
            response = HttpResponse(
                json.dumps({'error': f'流式推送失败: {str(e)}'}),
                status=500,
                content_type='application/json'
            )
            response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
            response['Access-Control-Allow-Credentials'] = 'true'
            return response

    @action(detail=True, methods=['post'])
    def cancel(self, request, task_id=None):
        """取消正在运行的任务"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()

            if task.status in ['completed', 'failed', 'cancelled']:
                return Response(
                    {'error': f'任务已经{task.get_status_display()}，无法取消'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 先通知后台线程停止（协作式取消：线程在流式回调和阶段之间检查该事件）
            # 线程退出时会自己写 cancelled 状态；这里再置一次，覆盖线程尚未启动等边界情况
            notified = _request_cancel_generation(task.task_id)

            task.status = 'cancelled'
            task.save(update_fields=['status'])

            return Response({
                'message': '任务已取消，正在停止生成' if notified else '任务已标记为取消',
                'task_id': task.task_id,
                'status': task.status,
                'notified': notified
            })

        except Exception as e:
            logger.error(f"取消任务时出错: {e}")
            return Response(
                {'error': f'取消任务失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def save_to_records(self, request, task_id=None):
        """保存测试用例到AI生成用例记录并导入到测试用例管理系统"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()

            if task.status != 'completed':
                return Response(
                    {'error': '只能保存已完成的测试用例生成任务'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not task.final_test_cases:
                return Response(
                    {'error': '没有最终测试用例可以保存'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 检查是否已经保存过
            if hasattr(task, 'is_saved_to_records') and task.is_saved_to_records:
                return Response(
                    {'message': '测试用例已经保存到记录中', 'already_saved': True}, 
                    status=status.HTTP_200_OK
                )
            
            # 解析并导入测试用例到测试用例管理系统
            test_cases = self._parse_test_cases_content(task.final_test_cases)
            
            if test_cases:
                try:
                    from apps.testcases.models import TestCase
                    from apps.projects.models import Project
                    from apps.core.mixins import get_accessible_projects_for_user
                    from django.db import models
                    
                    # 优先使用任务关联的项目
                    if task.project:
                        project = task.project
                        logger.info(f"使用任务关联的项目: {project.name}")
                    else:
                        # 回退到项目选择逻辑
                        user = task.created_by
                        accessible_projects = get_accessible_projects_for_user(user)
                        
                        # 尝试从前端获取项目ID
                        project_id = request.data.get('project_id')
                        
                        if project_id:
                            try:
                                project = accessible_projects.get(id=project_id)
                            except Project.DoesNotExist:
                                # 如果指定项目不存在或无权限，使用第一个可访问的项目
                                project = accessible_projects.first()
                                if not project:
                                    # 如果用户没有任何项目，创建默认项目
                                    project = Project.objects.create(
                                        name="默认项目",
                                        owner=user,
                                        description='系统自动创建的默认项目'
                                    )
                        else:
                            # 没有指定项目，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )
                    
                    adopted_count = 0
                    batch_time = timezone.now()  # 同一批采纳共享创建时间，保证整块排序
                    for sort_idx, test_case in enumerate(test_cases, 1):
                        tc = TestCase.objects.create(
                            project=project,
                            author=task.created_by,
                            source='ai',
                            title=test_case.get('scenario', '测试用例'),
                            description=test_case.get('scenario', ''),
                            preconditions=test_case.get('precondition', ''),
                            steps=test_case.get('steps', ''),
                            expected_result=test_case.get('expected', ''),
                            priority=self._map_priority(test_case.get('priority', '中')),
                            test_type='functional',
                            status='draft',
                            sort_order=sort_idx,
                            created_at=batch_time,
                        )
                        # 关联版本
                        if task.version_id:
                            tc.versions.add(task.version_id)
                        # 关联功能模块
                        if task.feature_module_id:
                            tc.feature_modules.add(task.feature_module_id)
                        # 关联测试点
                        if task.test_point_id:
                            tc.test_point_id = task.test_point_id
                            tc.save(update_fields=['test_point'])
                        adopted_count += 1
                    
                    logger.info(f"成功导入 {adopted_count} 条测试用例到项目 {project.name}（关联版本: {task.version_id}）")
                    
                except Exception as import_error:
                    logger.error(f"导入测试用例失败: {import_error}")
                    # 即使导入失败，仍然标记为已保存
            
            # 标记任务为已保存
            task.is_saved_to_records = True
            task.saved_at = timezone.now()
            task.save(update_fields=['is_saved_to_records', 'saved_at'])
            
            return Response({
                'message': '测试用例已成功保存到AI生成用例记录并导入到测试用例管理系统',
                'task_id': task.task_id,
                'saved_at': task.saved_at,
                'imported_count': adopted_count if test_cases else 0
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"保存测试用例到记录时出错: {e}")
            return Response(
                {'error': f'保存失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def saved_records(self, request):
        """获取已保存的测试用例记录列表"""
        try:
            # 获取已保存到记录的任务
            saved_tasks = TestCaseGenerationTask.objects.filter(
                is_saved_to_records=True,
                status='completed'
            ).order_by('-saved_at')
            
            # 序列化数据
            serializer = TestCaseGenerationTaskSerializer(saved_tasks, many=True)
            
            return Response({
                'message': '获取已保存记录成功',
                'records': serializer.data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"获取已保存记录时出错: {e}")
            return Response(
                {'error': f'获取记录失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='batch-adopt')
    def batch_adopt(self, request, task_id=None):
        """批量采纳任务的所有测试用例"""
        try:
            task = self.get_object()
            
            if task.status != 'completed':
                return Response(
                    {'error': '只能采纳已完成的测试用例生成任务'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not task.final_test_cases:
                return Response(
                    {'error': '没有最终测试用例可以采纳'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 解析最终测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)
            
            if not test_cases:
                return Response(
                    {'error': '无法解析测试用例内容'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 导入到testcases应用（使用与单条采纳相同的逻辑）
            try:
                from apps.testcases.models import TestCase
                from apps.projects.models import Project
                from apps.core.mixins import get_accessible_projects_for_user
                from django.db import models
                
                # 优先使用任务关联的项目
                if task.project:
                    project = task.project
                    logger.info(f"使用任务关联的项目: {project.name}")
                else:
                    # 回退到项目选择逻辑
                    user = task.created_by
                    accessible_projects = get_accessible_projects_for_user(user)
                    
                    # 尝试从前端获取项目ID
                    project_id = request.data.get('project_id')
                    
                    if project_id:
                        try:
                            project = accessible_projects.get(id=project_id)
                        except Project.DoesNotExist:
                            # 如果指定项目不存在或无权限，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )
                    else:
                        # 没有指定项目，使用第一个可访问的项目
                        project = accessible_projects.first()
                        if not project:
                            # 如果用户没有任何项目，创建默认项目
                            project = Project.objects.create(
                                name="默认项目",
                                owner=user,
                                description='系统自动创建的默认项目'
                            )
                
                adopted_count = 0
                batch_time = timezone.now()  # 同一批采纳共享创建时间，保证整块排序
                for sort_idx, test_case in enumerate(test_cases, 1):
                    tc = TestCase.objects.create(
                        project=project,  # 使用统一的项目选择逻辑
                        author=task.created_by,
                        source='ai',
                        title=test_case.get('scenario', '测试用例'),
                        description=test_case.get('scenario', ''),  # 使用scenario作为描述
                        preconditions=test_case.get('precondition', ''),
                        steps=test_case.get('steps', ''),
                        expected_result=test_case.get('expected', ''),
                        priority=self._map_priority(test_case.get('priority', '中')),
                        test_type='functional',
                        status='draft',
                        sort_order=sort_idx,
                        created_at=batch_time,
                    )
                    # 关联版本
                    if task.version_id:
                        tc.versions.add(task.version_id)
                    # 关联功能模块
                    if task.feature_module_id:
                        tc.feature_modules.add(task.feature_module_id)
                    # 关联测试点
                    if task.test_point_id:
                        tc.test_point_id = task.test_point_id
                        tc.save(update_fields=['test_point'])
                    adopted_count += 1
                
                return Response({
                    'message': f'成功采纳 {adopted_count} 条测试用例到项目 "{project.name}"（关联版本: {task.version_id}）',
                    'adopted_count': adopted_count,
                    'project_name': project.name
                }, status=status.HTTP_200_OK)
                
            except Exception as import_error:
                logger.error(f"导入测试用例失败: {import_error}")
                return Response(
                    {'error': f'导入测试用例失败: {str(import_error)}'}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"批量采纳测试用例时出错: {e}")
            return Response(
                {'error': f'批量采纳失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='batch-adopt-selected')
    def batch_adopt_selected(self, request, task_id=None):
        """批量采纳选中的测试用例"""
        try:
            task = self.get_object()
            test_cases_data = request.data.get('test_cases', [])
            
            if not test_cases_data:
                return Response(
                    {'error': '没有提供要采纳的测试用例数据'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 导入到testcases应用
            try:
                from apps.testcases.models import TestCase
                from apps.projects.models import Project
                from apps.core.mixins import get_accessible_projects_for_user
                from django.db import models
                
                # 优先使用任务关联的项目
                if task.project:
                    project = task.project
                    logger.info(f"使用任务关联的项目: {project.name}")
                else:
                    # 回退到项目选择逻辑
                    user = task.created_by
                    accessible_projects = get_accessible_projects_for_user(user)
                    
                    # 尝试从前端获取项目ID
                    project_id = request.data.get('project_id')
                    
                    if project_id:
                        try:
                            project = accessible_projects.get(id=project_id)
                        except Project.DoesNotExist:
                            # 如果指定项目不存在或无权限，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )
                    else:
                        # 没有指定项目，使用第一个可访问的项目
                        project = accessible_projects.first()
                        if not project:
                            # 如果用户没有任何项目，创建默认项目
                            project = Project.objects.create(
                                name="默认项目",
                                owner=user,
                                description='系统自动创建的默认项目'
                            )
                
                adopted_count = 0
                batch_time = timezone.now()  # 同一批采纳共享创建时间，保证整块排序
                for sort_idx, case_data in enumerate(test_cases_data, 1):
                    # 缺操作步骤的用例显式标记，避免伪装成有效步骤
                    if not (case_data.get('steps') or '').strip() and (case_data.get('title') or case_data.get('description')):
                        case_data['steps'] = '【步骤缺失，请补充操作步骤】'
                        case_data['steps_missing'] = True
                    tc = TestCase.objects.create(
                        project=project,  # 使用统一的项目选择逻辑
                        author=task.created_by,
                        source='ai',
                        title=case_data.get('title', '测试用例'),
                        description=case_data.get('description', ''),
                        preconditions=case_data.get('preconditions', ''),
                        steps=case_data.get('steps', ''),
                        expected_result=case_data.get('expected_result', ''),
                        priority=case_data.get('priority', 'medium'),
                        test_type=case_data.get('test_type', 'functional'),
                        status=case_data.get('status', 'draft'),
                        sort_order=sort_idx,
                        created_at=batch_time,
                    )
                    # 关联版本
                    if task.version_id:
                        tc.versions.add(task.version_id)
                    # 关联功能模块
                    if task.feature_module_id:
                        tc.feature_modules.add(task.feature_module_id)
                    # 关联测试点
                    if task.test_point_id:
                        tc.test_point_id = task.test_point_id
                        tc.save(update_fields=['test_point'])
                    adopted_count += 1
                
                return Response({
                    'message': f'成功采纳 {adopted_count} 条测试用例到项目 "{project.name}"（关联版本: {task.version_id}）',
                    'adopted_count': adopted_count,
                    'project_name': project.name
                }, status=status.HTTP_200_OK)
                
            except Exception as import_error:
                logger.error(f"导入选中测试用例失败: {import_error}")
                return Response(
                    {'error': f'导入测试用例失败: {str(import_error)}'}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"批量采纳选中测试用例时出错: {e}")
            return Response(
                {'error': f'批量采纳失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='batch-discard')
    def batch_discard(self, request, task_id=None):
        """批量弃用任务的所有测试用例 - 删除整个任务"""
        try:
            task = self.get_object()
            
            logger.info(f"开始批量弃用任务 {task.task_id}")
            
            # 直接删除整个任务记录
            task.delete()
            
            return Response({
                'message': '任务已被弃用并删除，不会再在列表中显示'
            }, status=status.HTTP_200_OK)
                
        except Exception as e:
            logger.error(f"批量弃用任务时出错: {e}")
            return Response(
                {'error': f'批量弃用失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='discard-selected-cases')
    def discard_selected_cases(self, request, task_id=None):
        """弃用选中的测试用例 - 从final_test_cases中删除"""
        try:
            task = self.get_object()
            case_indices = request.data.get('case_indices', [])
            
            if not case_indices:
                return Response(
                    {'error': '没有提供要弃用的测试用例索引'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not task.final_test_cases:
                return Response(
                    {'error': '任务没有最终测试用例'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"开始弃用任务 {task.task_id} 的测试用例，索引: {case_indices}")
            
            # 解析现有的测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)
            
            # 按索引从大到小排序，避免删除时索引变化
            case_indices.sort(reverse=True)
            
            discarded_count = 0
            for index in case_indices:
                if 0 <= index < len(test_cases):
                    removed_case = test_cases.pop(index)
                    discarded_count += 1
                    logger.debug(f"弃用测试用例 {index}: {removed_case.get('scenario', 'unknown')}")
            
            # 如果所有用例都被弃用了，删除整个任务
            if not test_cases:
                logger.info(f"任务 {task.task_id} 的所有用例都被弃用，删除任务")
                task.delete()
                return Response({
                    'message': f'已弃用 {discarded_count} 条测试用例，任务已被删除',
                    'discarded_count': discarded_count,
                    'task_deleted': True
                }, status=status.HTTP_200_OK)
            
            # 重新生成final_test_cases内容
            task.final_test_cases = self._reconstruct_test_cases_content(test_cases)
            task.save()
            
            logger.debug(f"重构后的测试用例内容: {task.final_test_cases[:200]}...")
            
            return Response({
                'message': f'已弃用 {discarded_count} 条测试用例',
                'discarded_count': discarded_count,
                'remaining_cases': len(test_cases),
                'task_deleted': False,
                'updated_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"弃用选中测试用例时出错: {e}")
            return Response(
                {'error': f'弃用失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='discard-single-case')
    def discard_single_case(self, request, task_id=None):
        """弃用单个测试用例"""
        try:
            task = self.get_object()
            case_index = request.data.get('case_index')
            
            if case_index is None:
                return Response(
                    {'error': '没有提供测试用例索引'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not task.final_test_cases:
                return Response(
                    {'error': '任务没有最终测试用例'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"开始弃用任务 {task.task_id} 的单个测试用例，索引: {case_index}")
            
            # 解析现有的测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)
            
            if case_index < 0 or case_index >= len(test_cases):
                return Response(
                    {'error': f'测试用例索引 {case_index} 超出范围，总共有 {len(test_cases)} 个测试用例'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 删除指定索引的测试用例
            removed_case = test_cases.pop(case_index)
            logger.debug(f"弃用测试用例 {case_index}: {removed_case.get('scenario', 'unknown')}")
            
            # 如果所有用例都被弃用了，删除整个任务
            if not test_cases:
                logger.info(f"任务 {task.task_id} 的所有用例都被弃用，删除任务")
                task.delete()
                return Response({
                    'message': '已弃用测试用例，任务已被删除',
                    'discarded_count': 1,
                    'task_deleted': True
                }, status=status.HTTP_200_OK)
            
            # 重新生成final_test_cases内容
            task.final_test_cases = self._reconstruct_test_cases_content(test_cases)
            task.save()
            
            logger.debug(f"单个弃用 - 重构后的测试用例内容: {task.final_test_cases[:200]}...")
            
            return Response({
                'message': '已弃用测试用例',
                'discarded_count': 1,
                'remaining_cases': len(test_cases),
                'task_deleted': False,
                'updated_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"弃用单个测试用例时出错: {e}")
            return Response(
                {'error': f'弃用失败: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='update-test-cases')
    def update_test_cases(self, request, task_id=None):
        """更新测试用例内容"""
        try:
            task = self.get_object()

            final_test_cases = request.data.get('final_test_cases')
            if not final_test_cases:
                return Response(
                    {'error': '缺少final_test_cases参数'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"开始更新任务 {task.task_id} 的测试用例内容")

            # 更新final_test_cases字段
            task.final_test_cases = final_test_cases
            task.save(update_fields=['final_test_cases'])

            logger.info(f"任务 {task.task_id} 测试用例更新成功")

            return Response({
                'message': '测试用例更新成功',
                'task_id': task.task_id,
                'final_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"更新测试用例时出错: {e}")
            return Response(
                {'error': f'更新失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='regenerate')
    def regenerate(self, request, task_id=None):
        """基于当前用例（含人工调整）、评审意见和人工确认回复，重新生成改进后的测试用例"""
        try:
            task = self.get_object()

            current_cases = task.final_test_cases
            review_feedback = task.review_feedback
            human_feedback = request.data.get('human_feedback', '').strip()

            # 支持弹窗中编辑关联参数
            update_fields = []
            if human_feedback:
                task.human_feedback = human_feedback
                update_fields.append('human_feedback')

            feature_module_id = request.data.get('feature_module')
            if feature_module_id:
                try:
                    fm = FeatureModule.objects.get(id=feature_module_id)
                    task.feature_module = fm
                    update_fields.append('feature_module')
                except FeatureModule.DoesNotExist:
                    pass
            elif feature_module_id is None and 'feature_module' in request.data:
                task.feature_module = None
                update_fields.append('feature_module')

            test_point_id = request.data.get('test_point')
            if test_point_id:
                try:
                    tp = TestPoint.objects.get(id=test_point_id)
                    task.test_point = tp
                    update_fields.append('test_point')
                except TestPoint.DoesNotExist:
                    pass
            elif test_point_id is None and 'test_point' in request.data:
                task.test_point = None
                update_fields.append('test_point')

            generation_mode = request.data.get('generation_mode')
            if generation_mode and generation_mode in dict(TestCaseGenerationTask.GENERATION_MODE_CHOICES):
                task.generation_mode = generation_mode
                update_fields.append('generation_mode')

            if update_fields:
                task.save(update_fields=update_fields)

            if not current_cases:
                return Response(
                    {'error': '没有可用的测试用例，请先生成用例'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if not review_feedback:
                return Response(
                    {'error': '没有评审反馈，请先完成AI评审'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if not task.writer_model_config or not task.writer_prompt_config:
                return Response(
                    {'error': '缺少编写模型或提示词配置'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"任务 {task.task_id} 开始人工触发重新生成（人工调整: {bool(human_feedback)}）")

            task.status = 'revising'
            task.progress = 85
            task.save(update_fields=['status', 'progress'])

            import threading

            result_holder = {'result': None, 'error': None}

            def _run_revise():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        gen_config = GenerationConfig.get_active_config()
                        timeout_val = gen_config.review_timeout if gen_config else 120

                        revised = loop.run_until_complete(
                            asyncio.wait_for(
                                AIModelService.revise_test_cases_based_on_review(
                                    task, current_cases, review_feedback,
                                    human_feedback=human_feedback if human_feedback else None
                                ),
                                timeout=timeout_val
                            )
                        )
                        result_holder['result'] = revised
                    finally:
                        loop.close()
                except Exception as e:
                    result_holder['error'] = str(e)

            thread = threading.Thread(
                target=_thread_with_conn_cleanup(_run_revise), daemon=True)
            thread.start()
            thread.join(timeout=180)

            if thread.is_alive():
                task.status = 'completed'
                task.error_message = '重新生成超时，保留原有用例'
                task.save()
                return Response(
                    {'error': '重新生成超时（3分钟），已保留原有用例，请检查网络或模型配置后重试'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            if result_holder['error']:
                task.status = 'completed'
                task.error_message = result_holder['error']
                task.save()
                return Response(
                    {'error': f'重新生成失败: {result_holder["error"]}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            revised_cases = result_holder['result']
            if not revised_cases or len(revised_cases.strip()) == 0:
                task.status = 'completed'
                task.error_message = 'AI返回空内容'
                task.save()
                return Response(
                    {'warning': 'AI未返回有效内容，已保留原有用例', 'task': self.get_serializer(task).data},
                    status=status.HTTP_200_OK
                )

            # 后处理：修整不完整用例 → 排序 → 重新编号 → 去重 → 按生成模式截断
            revised_cases = AIModelService.fix_incomplete_last_case(revised_cases)
            sorted_cases = AIModelService.sort_test_cases_by_id(revised_cases)
            renumbered_cases = AIModelService.renumber_test_cases(sorted_cases)
            # 强制去重（解决"基于评审意见重新生成"时 LLM 不执行"删除冗余"指令的问题）
            # 仅按编号完全相同去重，不按标题相似度（避免误删功能相近但不同的用例）
            deduped_cases, dedup_removed, _ = AIModelService.dedup_duplicate_test_cases(renumbered_cases)
            if dedup_removed > 0:
                logger.info(f"任务 {task.task_id} 重新生成后去重: 删除{dedup_removed}条冗余用例")
                # 重新编号（去重后编号可能不再连续）
                deduped_cases = AIModelService.renumber_test_cases(deduped_cases)

            task.final_test_cases = AIModelService.enforce_case_count_limit(
                deduped_cases, task.generation_mode)[0]
            task.status = 'completed'
            task.progress = 100
            task.completed_at = timezone.now()
            task.save(update_fields=['final_test_cases', 'status', 'progress', 'completed_at'])

            logger.info(f"任务 {task.task_id} 重新生成完成，用例长度: {len(task.final_test_cases)}")

            serializer = self.get_serializer(task)
            return Response({
                'message': '测试用例重新生成成功',
                'task': serializer.data
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"重新生成测试用例时出错: {e}")
            try:
                task = self.get_object()
                task.status = 'completed'
                task.error_message = str(e)
                task.save(update_fields=['status', 'error_message'])
            except Exception:
                pass
            return Response(
                {'error': f'重新生成失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='regenerate-case')
    def regenerate_case(self, request, task_id=None):
        """单条重试：对缺少操作步骤的用例重新生成步骤，并回写该行 markdown。"""
        import re as _re
        task = self.get_object()
        case_id = str(request.data.get('caseId') or request.data.get('case_id') or '').strip()
        if not case_id:
            return Response({'error': '缺少 caseId'}, status=status.HTTP_400_BAD_REQUEST)
        if not task.writer_model_config:
            return Response({'error': '缺少编写模型配置'}, status=status.HTTP_400_BAD_REQUEST)

        content = task.final_test_cases or ''
        lines = content.split('\n')
        target_idx = None
        for i, ln in enumerate(lines):
            if ln.strip().startswith('|') and f'| {case_id} |' in ln:
                target_idx = i
                break
        if target_idx is None:
            return Response({'error': f'未找到用例 {case_id}'}, status=status.HTTP_404_NOT_FOUND)

        # 定位表头与步骤列
        headers = None
        for ln in lines[:target_idx]:
            s = ln.strip()
            if s.startswith('|') and ('步骤' in s or '场景' in s):
                headers = [c.strip().lower() for c in s.strip().strip('|').split('|')]
                break
        step_idx = None
        if headers:
            step_idx = next(
                (j for j, h in enumerate(headers)
                 if '步骤' in h and '预期' not in h and '结果' not in h),
                None,
            )
        if step_idx is None:
            return Response({'error': '无法定位步骤列'}, status=status.HTTP_400_BAD_REQUEST)

        raw = lines[target_idx]
        cells = [c.strip() for c in raw.strip().strip('|').split('|')]
        if step_idx >= len(cells):
            return Response({'error': '无法定位步骤单元格'}, status=status.HTTP_400_BAD_REQUEST)
        if cells[step_idx] and '【步骤缺失' not in cells[step_idx] and '参考测试目标' not in cells[step_idx]:
            return Response({'error': '该用例已有操作步骤，无需重试'}, status=status.HTTP_400_BAD_REQUEST)

        scenario = cells[1] if len(cells) > 1 else ''
        precondition = cells[2] if len(cells) > 2 else ''
        expected = cells[step_idx + 1] if step_idx + 1 < len(cells) else ''
        if expected.lower() in ('用例id', '测试目标', '前置条件', '操作步骤', '测试步骤', '预期结果', '优先级'):
            expected = ''

        prompt = (
            '以下是生成的一条测试用例，缺少「操作步骤」。请根据用例信息补充 2~5 条具体的、可执行的测试操作步骤。\n\n'
            f'用例编号：{case_id}\n'
            f'测试目标：{scenario or ""}\n'
            f'前置条件：{precondition or ""}\n'
            f'预期结果：{expected or ""}\n\n'
            '只输出 JSON：{"steps": ["步骤1", "步骤2", ...]}，不要输出任何解释文字。'
        )
        from apps.requirement_analysis.models import AIModelService
        import asyncio as _asyncio
        result = _asyncio.run(AIModelService.call_openai_compatible_api(
            task.writer_model_config, [{'role': 'user', 'content': prompt}],
            max_tokens=1500, temperature=0,
        ))
        raw_out = (result['choices'][0]['message']['content'] or '')
        m = _re.search(r'\{.*\}', raw_out, _re.DOTALL)
        steps_list = []
        if m:
            try:
                steps_list = (json.loads(m.group(0)) or {}).get('steps') or []
            except Exception:
                steps_list = []
        if not isinstance(steps_list, list) or not any(str(s).strip() for s in steps_list):
            return Response(
                {'error': 'AI 未能生成有效步骤，请稍后重试或手动补充'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        steps_text = '<br>'.join(
            f'{i + 1}. {str(s).strip()}' for i, s in enumerate(steps_list) if str(s).strip()
        )
        cells[step_idx] = steps_text
        # 截掉嵌入表头的残留单元格（LLM 偶发把表头写进数据行）
        header_words = ('用例id', '测试目标', '前置条件', '操作步骤', '测试步骤', '预期结果', '优先级', '测试类型', '关联需求')
        cut = next(
            (j for j in range(step_idx + 1, len(cells)) if cells[j].lower() in header_words),
            None,
        )
        if cut is not None:
            cells = cells[:cut]
        ncols = len(headers) if headers else 8
        while len(cells) < ncols:
            cells.append('')
        cells = cells[:ncols]
        lines[target_idx] = '| ' + ' | '.join(cells) + ' |'
        task.final_test_cases = '\n'.join(lines)
        task.save(update_fields=['final_test_cases'])
        logger.info(f"[regenerate-case] 任务 {task.task_id} 用例 {case_id} 步骤已重新生成")
        return Response({
            'message': '步骤已重新生成',
            'task': self.get_serializer(task).data,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='repair-table')
    def repair_table(self, request, task_id=None):
        """整表修复：把 final_test_cases 交给 LLM 重排为规范表格，
        修复「表头嵌入数据行 / 重复行 / 步骤列截断」等格式问题（一次调用）。"""
        task = self.get_object()
        if not task.writer_model_config:
            return Response({'error': '缺少编写模型配置'}, status=status.HTTP_400_BAD_REQUEST)
        content = task.final_test_cases or ''
        if not content.strip():
            return Response({'error': '没有可修复的内容'}, status=status.HTTP_400_BAD_REQUEST)

        prompt = (
            '下面是 AI 生成的测试用例 Markdown 表格，存在格式问题：'
            '部分数据行中嵌入了表头（如「用例ID | 测试目标 | 前置条件 | …」）、重复行、'
            '或步骤/预期结果列被截断。请把它重排为规范的 Markdown 表格：\n'
            '1. 只保留有效用例行，删除嵌入的表头残留和重复内容；\n'
            '2. 每行必须包含：用例ID、测试目标、前置条件、测试步骤、预期结果、优先级；\n'
            '3. 步骤确实缺失的行保留，但测试步骤列填「【步骤缺失，请补充操作步骤】」；\n'
            '4. 不要改写或增删用例内容，只修复格式。\n\n'
            '只输出修复后的 Markdown 表格，不要任何解释文字。\n\n原始内容：\n' + content
        )
        from apps.requirement_analysis.models import AIModelService
        import asyncio as _asyncio
        try:
            result = _asyncio.run(AIModelService.call_openai_compatible_api(
                task.writer_model_config, [{'role': 'user', 'content': prompt}],
                max_tokens=16000, temperature=0,
            ))
        except Exception as e:
            logger.error(f"[repair-table] 任务 {task.task_id} 修复失败: {e}")
            return Response({'error': f'AI 修复失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        repaired = (result['choices'][0]['message']['content'] or '').strip()
        if not repaired:
            return Response({'error': 'AI 未能生成修复结果'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        # 剥除可能的 markdown 代码块包裹
        if repaired.startswith('```'):
            repaired = repaired.strip('`').strip()
            if repaired.startswith('markdown'):
                repaired = repaired[len('markdown'):].strip()
        task.final_test_cases = repaired
        task.save(update_fields=['final_test_cases'])
        logger.info(f"[repair-table] 任务 {task.task_id} 表格格式已修复")
        return Response({
            'message': '表格格式已修复',
            'task': self.get_serializer(task).data,
        }, status=status.HTTP_200_OK)

    def _parse_test_cases_content(self, content):
        """解析测试用例内容 - 支持多种格式"""
        if not content:
            return []

        # 去除markdown加粗标记，保留纯净文本
        import re
        clean_content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)

        logger.info(f"开始解析测试用例内容，内容长度: {len(clean_content)}")
        logger.info(f"内容前200字符: {clean_content[:200]}")

        # 尝试表格格式解析
        if '|' in clean_content:
            return self._parse_table_format(clean_content)

        # 尝试结构化文本格式解析
        return self._parse_text_format(clean_content)
    
    def _parse_table_format(self, content):
        """解析表格格式的测试用例"""
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        test_cases = []
        table_data = []
        
        # 提取表格数据
        for line in lines:
            if '|' in line and not line.startswith('|-'):
                cells = [cell.strip() for cell in line.split('|')]
                # 移除首尾空字符串（split('|') 在首尾会产生空字符串）
                while cells and cells[0] == '':
                    cells = cells[1:]
                while cells and cells[-1] == '':
                    cells = cells[:-1]
                if len(cells) > 1:
                    table_data.append(cells)
        
        if len(table_data) < 2:
            return []
        
        # 解析表头和数据
        headers = [h.lower() for h in table_data[0]]
        logger.debug(f"表格标题: {headers}")
        
        for row in table_data[1:]:
            if len(row) < len(headers):
                continue
                
            test_case = {}
            for i, header in enumerate(headers):
                value = row[i] if i < len(row) else ''
                
                if any(keyword in header for keyword in ['编号', 'id', '序号', '用例id']):
                    test_case['caseId'] = value
                elif any(keyword in header for keyword in ['场景', '标题', '名称', 'title', 'scenario', '测试目标']):
                    test_case['scenario'] = value
                elif any(keyword in header for keyword in ['前置', '前提', 'precondition']):
                    test_case['precondition'] = value
                elif any(keyword in header for keyword in ['步骤', 'step', '测试步骤', '操作步骤']):
                    test_case['steps'] = value
                elif any(keyword in header for keyword in ['预期', '结果', 'expected', 'result']):
                    test_case['expected'] = value
                elif any(keyword in header for keyword in ['优先级', 'priority']):
                    test_case['priority'] = value
            
            # 缺操作步骤的用例显式标记，避免伪装成有效步骤
            if not (test_case.get('steps') or '').strip() and test_case.get('scenario'):
                test_case['steps'] = '【步骤缺失，请补充操作步骤】'
                test_case['steps_missing'] = True
            if test_case.get('scenario') or test_case.get('steps'):
                test_cases.append(test_case)
                logger.debug(f"解析出表格测试用例: {test_case}")
        
        return test_cases
    
    def _parse_text_format(self, content):
        """解析文本格式的测试用例"""
        lines = content.split('\n')
        test_cases = []
        current_case = {}
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            logger.debug(f"处理行: {line}")
            
            # 检测测试用例开始
            is_case_start = (
                '测试用例' in line or 
                'Test Case' in line or
                line.startswith(('1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', '10.')) or
                line.startswith(('一、', '二、', '三、', '四、', '五、')) or
                bool(re.match(r'^\d+[\.\)、]', line))
            )
            
            if is_case_start:
                if current_case:
                    logger.debug(f"添加测试用例: {current_case}")
                    test_cases.append(current_case)
                
                # 清理标题
                scenario = line
                scenario = scenario.replace('测试用例', '').replace('Test Case', '')
                scenario = scenario.replace(':', '').replace('：', '')
                scenario = re.sub(r'^\d+[\.\)、]\s*', '', scenario)
                scenario = scenario.strip()
                
                current_case = {'scenario': scenario}
                
            elif current_case:  # 只有在已经开始一个测试用例后才处理字段
                # 检测各个字段
                if any(keyword in line for keyword in ['前置条件', '前提条件', '前置', '前提']):
                    current_case['precondition'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['测试步骤', '操作步骤', '执行步骤', '步骤']):
                    current_case['steps'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['预期结果', '期望结果', '预期']):
                    current_case['expected'] = self._extract_field_value(line)
                elif '优先级' in line:
                    current_case['priority'] = self._extract_field_value(line)
        
        if current_case:
            logger.debug(f"添加最后一个测试用例: {current_case}")
            test_cases.append(current_case)
        
        logger.info(f"解析完成，共解析出 {len(test_cases)} 个测试用例")
        for i, case in enumerate(test_cases):
            logger.debug(f"测试用例 {i+1}: {case}")
            
        return test_cases
    
    def _extract_field_value(self, line):
        """提取字段值"""
        # 尝试多种分隔符
        for sep in [':', '：', '】', '】:', '】：']:
            if sep in line:
                return line.split(sep, 1)[-1].strip()
        
        # 如果没有分隔符，移除常见的前缀
        for prefix in ['前置条件', '测试步骤', '操作步骤', '预期结果', '优先级']:
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        
        return line.strip()
    
    def _reconstruct_test_cases_content(self, test_cases):
        """重新构建测试用例内容 - 保持原有格式和编号"""
        if not test_cases:
            return ""
        
        # 检查是否有caseId字段，如果有，说明是表格格式
        has_case_ids = any(test_case.get('caseId') for test_case in test_cases)
        
        if has_case_ids:
            # 重构为表格格式，保持原有编号
            return self._reconstruct_table_format(test_cases)
        else:
            # 重构为文本格式
            return self._reconstruct_text_format(test_cases)
    
    def _reconstruct_table_format(self, test_cases):
        """重构为表格格式"""
        content_lines = []
        content_lines.append("```markdown")
        
        # 检查是否有任何测试用例包含steps字段
        has_steps = any(test_case.get('steps') and test_case.get('steps') != '参考测试目标执行相应操作' for test_case in test_cases)
        
        if has_steps:
            # 包含测试步骤的表格格式
            content_lines.append("| 用例ID | 测试目标 | 前置条件 | 测试步骤 | 预期结果 | 优先级 | 测试类型 | 关联需求 |")
            content_lines.append("|--------|--------|--------|--------|--------|--------|--------|--------|")
            
            for test_case in test_cases:
                case_id = test_case.get('caseId', '')
                scenario = test_case.get('scenario', '')
                precondition = test_case.get('precondition', '')
                steps = test_case.get('steps', '参考测试目标执行相应操作')
                expected = test_case.get('expected', '')
                priority = test_case.get('priority', 'P2')
                
                # 保持原有格式，将换行符转换为<br>
                precondition = precondition.replace('\n', '<br>')
                steps = steps.replace('\n', '<br>')
                expected = expected.replace('\n', '<br>')
                
                content_lines.append(f"| {case_id} | {scenario} | {precondition} | {steps} | {expected} | {priority} | 功能验证 | 需求1 |")
        else:
            # 原始格式（没有测试步骤列）
            content_lines.append("| 用例ID | 测试目标 | 前置条件 | 预期结果 | 优先级 | 测试类型 | 关联需求 |")
            content_lines.append("|--------|--------|--------|--------|--------|--------|--------|")
            
            for test_case in test_cases:
                case_id = test_case.get('caseId', '')
                scenario = test_case.get('scenario', '')
                precondition = test_case.get('precondition', '')
                expected = test_case.get('expected', '')
                priority = test_case.get('priority', 'P2')
                
                # 保持原有格式，将换行符转换为<br>
                precondition = precondition.replace('\n', '<br>')
                expected = expected.replace('\n', '<br>')
                
                content_lines.append(f"| {case_id} | {scenario} | {precondition} | {expected} | {priority} | 功能验证 | 需求1 |")
        
        content_lines.append("```")
        return "\n".join(content_lines)
    
    def _reconstruct_text_format(self, test_cases):
        """重构为文本格式"""
        content_lines = []
        for test_case in test_cases:
            # 获取原有的scenario
            scenario = test_case.get('scenario', '未命名测试用例')
            
            # 确保scenario能被前端正确识别
            # 如果scenario不是以数字开头或不包含"测试用例"，则添加标识
            if not (bool(re.match(r'^\d+[\.\)、]', scenario)) or 
                    '测试用例' in scenario or 
                    'Test Case' in scenario):
                # 添加"测试用例:"前缀确保能被识别
                content_lines.append(f"\n测试用例: {scenario}")
            else:
                content_lines.append(f"\n{scenario}")
            
            if test_case.get('precondition'):
                content_lines.append(f"前置条件: {test_case['precondition']}")
            
            if test_case.get('steps'):
                content_lines.append(f"测试步骤: {test_case['steps']}")
            
            if test_case.get('expected'):
                content_lines.append(f"预期结果: {test_case['expected']}")
            
            if test_case.get('priority'):
                content_lines.append(f"优先级: {test_case['priority']}")
            
            content_lines.append("")  # 空行分隔
        
        return "\n".join(content_lines)
    
    @action(detail=True, methods=['post'])
    def rerun_review(self, request, task_id=None):
        """对已有任务重新执行AI评审（使用最新prompt，自动识别不确定项）"""
        try:
            task = self.get_object()
            
            # 检查任务状态
            if task.status not in ['completed', 'failed']:
                return Response(
                    {'error': f'任务状态为"{task.get_status_display()}"，仅已完成或失败的任务可重新评审'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 获取测试用例内容（优先使用最新的最终用例，其次初次生成内容）
            test_cases = task.final_test_cases or task.generated_test_cases
            if not test_cases or not test_cases.strip():
                return Response(
                    {'error': '该任务没有测试用例内容，无法重新评审'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 检查评审模型配置
            if not task.reviewer_model_config:
                return Response(
                    {'error': '该任务未配置评审模型，无法重新评审'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 标记任务状态为评审中
            task.status = 'reviewing'
            task.progress = 85
            task.save(update_fields=['status', 'progress'])
            
            # 在新线程中执行：先同步取数据 -> 纯异步调用AI -> 同步保存结果
            def execute_review():
                import asyncio
                import django.db
                import traceback
                try:
                    # 关闭主线程的数据库连接，让新线程创建自己的连接
                    django.db.connections.close_all()
                    
                    # === 阶段1: 同步取所有ORM数据 ===
                    task_in_thread = TestCaseGenerationTask.objects.get(task_id=task_id)
                    reviewer_config = task_in_thread.reviewer_model_config
                    reviewer_prompt = task_in_thread.reviewer_prompt_config.content
                    human_feedback_text = task_in_thread.human_feedback or ""
                    logger.info(f"[RERUN] 线程启动: task_id={task_id}, config={reviewer_config.name if reviewer_config else 'None'}, has_human_feedback={bool(human_feedback_text)}")
                    
                    # === 阶段2: 纯异步调用AI（无ORM操作）===
                    async def call_ai_async():
                        from apps.requirement_analysis.models import AIModelService
                        
                        # 构建人工确认上下文：只注入"已真正回复"的确认项
                        # （过滤掉 "确认回复：(未填写)" 的项，避免让 AI 误以为这些也是已确认结论）
                        human_feedback_context = ""
                        if human_feedback_text.strip():
                            import re as _re
                            _blocks = _re.split(r'\n\n(?=【确认项)', human_feedback_text)
                            _answered = [
                                b.strip() for b in _blocks
                                if b.strip() and _re.search(
                                    r'确认回复[：:]\s*(?!\(未填写\))(.+)',
                                    b, _re.DOTALL
                                )
                            ]
                            if _answered:
                                answered_str = "\n\n".join(_answered)
                                human_feedback_context = (
                                    f"【⚠️ 重要：人工已确认的事项（共 {len(_answered)} 项）】\n"
                                    f"以下问题已被人工明确回复，本次评审请直接采纳，\n"
                                    f"不要再将这些问题标记为 ⚠️待确认：\n\n"
                                    f"{answered_str}\n\n"
                                )
                        
                        user_message = (
                            f"请对以下生成的测试用例进行严格的专家级评审。\n\n"
                            f"{human_feedback_context}"
                            f"【评审重点】\n"
                            f"1. **覆盖率漏洞**：请仔细比对用例集是否覆盖了常见的异常场景（如断网、超时、数据冲突）和边界条件。\n"
                            f"2. **逻辑严密性**：检查预期结果是否具体、可验证。\n"
                            f"3. **冗余检查**：指出是否有重复或无效的用例。\n"
                            f"4. **不确定项识别（⚠️重要）**：遇到需求未明确说明、无法确定的问题，\n"
                            f"   必须用以下格式标记：\n"
                            f"   - ⚠️ 待确认：[无法确定的具体问题，用问句形式描述]\n"
                            f"   ⚠️ 注意：已在「人工已确认的事项」中确认过的问题，不要再重复标记！\n\n"
                            f"【待评审用例】\n{test_cases}\n\n"
                            f"【输出格式要求】\n"
                            f"1. 先输出完整的评审报告（含评分、问题列表、改进建议）。\n"
                            f"2. 在报告末尾，汇总所有不确定项：\n\n"
                            f"## ⚠️ 待人工确认项清单\n"
                            f"- ⚠️ 待确认：[问题1简述]\n"
                            f"...\n\n"
                            f"**如果没有不确定项，请输出：## ⚠️ 待人工确认项清单\n无（所有评审点均可确定）**"
                        )
                        messages = [
                            {"role": "system", "content": reviewer_prompt},
                            {"role": "user", "content": user_message}
                        ]
                        response = await AIModelService.call_openai_compatible_api(reviewer_config, messages)
                        return response['choices'][0]['message']['content']
                    
                    review_feedback = asyncio.run(call_ai_async())
                    if not review_feedback or not review_feedback.strip():
                        raise ValueError('评审模型返回空内容：请检查 reviewer 配置（推理模型需读取 reasoning_content）')
                    logger.info(f"[RERUN] AI评审返回，长度={len(review_feedback) if review_feedback else 0}")
                    
                    # === 阶段3: 同步保存结果 ===
                    task_in_thread.review_feedback = review_feedback
                    
                    # 自动提取不确定项（保留用户已回复的旧项）
                    try:
                        from apps.requirement_analysis.models import AIModelService
                        auto_feedback = AIModelService.extract_uncertain_items_from_review(review_feedback)
                        
                        if auto_feedback:
                            # 读取旧的 human_feedback，保留用户已确认的回答
                            old_feedback = task_in_thread.human_feedback or ""
                            merged = _merge_human_feedback(old_feedback, auto_feedback)
                            task_in_thread.human_feedback = merged
                            logger.info(f"[RERUN] 任务 {task_id} 合并了不确定项（保留已回复项）")
                        else:
                            # 本轮没有新的不确定项，保留用户之前的回复
                            old_feedback = task_in_thread.human_feedback or ""
                            if old_feedback.strip():
                                logger.info(f"[RERUN] 任务 {task_id} 无新增不确定项，保留已有回复")
                    except Exception as extract_error:
                        logger.warning(f"[RERUN] 提取不确定项失败: {extract_error}")
                    
                    task_in_thread.status = 'completed'
                    task_in_thread.progress = 100
                    task_in_thread.completed_at = timezone.now()
                    task_in_thread.review_count = (task_in_thread.review_count or 0) + 1
                    task_in_thread.review_updated_at = timezone.now()
                    AIModelService.append_review_history(task_in_thread, review_feedback)
                    task_in_thread.save(update_fields=['review_feedback', 'human_feedback', 'status', 'progress', 'completed_at', 'review_count', 'review_updated_at', 'generation_log'])
                    logger.info(f"[RERUN] 任务 {task_id} 重新评审完成")
                    
                    # === 评审完成后自动触发知识库审计 ===
                    try:
                        audit_result = AIModelService.audit_knowledge_base(task_in_thread, test_cases, review_feedback)
                        if audit_result:
                            task_in_thread.kb_audit_result = audit_result
                            task_in_thread.save(update_fields=['kb_audit_result'])
                            logger.info(f"[RERUN] 任务 {task_id} KB审计完成，结果长度={len(audit_result)}")
                    except Exception as audit_error:
                        logger.warning(f"[RERUN] 任务 {task_id} KB审计失败（不影响评审结果）: {audit_error}")
                    
                except Exception as review_error:
                    logger.error(f"[RERUN] 任务 {task_id} 失败: {review_error}")
                    logger.error(traceback.format_exc())
                    try:
                        django.db.connections.close_all()
                        t = TestCaseGenerationTask.objects.get(task_id=task_id)
                        t.status = 'failed'
                        t.error_message = f'重新评审失败: {str(review_error)}'
                        t.save(update_fields=['status', 'error_message'])
                    except Exception as db_err:
                        logger.error(f"[RERUN] 保存失败状态出错: {db_err}")
            
            thread = threading.Thread(
                target=_thread_with_conn_cleanup(execute_review))
            thread.daemon = True
            thread.start()
            
            return Response({
                'message': '重新评审已启动',
                'task_id': task.task_id,
                'status': task.status
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"启动重新评审失败: {e}")
            return Response(
                {'error': f'启动重新评审失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='audit-kb')
    def audit_knowledge_base(self, request, task_id=None):
        """对已完成的评审进行知识库智能审计"""
        try:
            task = self.get_object()
            
            # 检查任务状态
            if task.status != 'completed':
                return Response(
                    {'error': f'任务状态为"{task.get_status_display()}"，仅已完成的任务可执行知识库审计'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 获取测试用例和评审反馈（优先使用最新的最终用例）
            test_cases = task.final_test_cases or task.generated_test_cases
            if not test_cases or not test_cases.strip():
                return Response(
                    {'error': '该任务没有测试用例内容，无法审计'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            review_feedback = task.review_feedback or ""
            
            # 检查模型配置
            model_config = task.reviewer_model_config or task.writer_model_config
            if not model_config:
                return Response(
                    {'error': '该任务未配置AI模型，无法执行审计'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # 在新线程中异步执行审计
            def execute_audit():
                import django.db
                import traceback as tb
                try:
                    django.db.connections.close_all()
                    
                    task_in_thread = TestCaseGenerationTask.objects.get(task_id=task_id)
                    test_cases_val = task_in_thread.final_test_cases or task_in_thread.generated_test_cases
                    review_val = task_in_thread.review_feedback or ""
                    
                    from apps.requirement_analysis.models import AIModelService
                    audit_result = AIModelService.audit_knowledge_base(
                        task_in_thread, test_cases_val, review_val
                    )
                    logger.info(f"[KB审计] 任务 {task_id} 审计结果长度={len(audit_result) if audit_result else 0}")
                    
                    if audit_result:
                        task_in_thread.kb_audit_result = audit_result
                        task_in_thread.save(update_fields=['kb_audit_result'])
                        logger.info(f"[KB审计] 任务 {task_id} 审计结果已保存")
                    else:
                        logger.info(f"[KB审计] 任务 {task_id} 审计结果为空，跳过保存")
                        
                except Exception as audit_error:
                    logger.error(f"[KB审计] 任务 {task_id} 审计失败: {audit_error}")
                    logger.error(tb.format_exc())
            
            thread = threading.Thread(
                target=_thread_with_conn_cleanup(execute_audit))
            thread.daemon = True
            thread.start()

            return Response({
                'message': '知识库审计已启动',
                'task_id': task.task_id
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"启动知识库审计失败: {e}")
            return Response(
                {'error': f'启动知识库审计失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _map_priority(self, priority_str):
        """映射优先级"""
        priority_map = {
            '最高': 'critical',
            '高': 'high',
            '中': 'medium', 
            '低': 'low',
            'P0': 'critical',
            'P1': 'high',
            'P2': 'medium',
            'P3': 'low'
        }
        return priority_map.get(priority_str, 'medium')


class ConfigStatusViewSet(viewsets.ViewSet):
    """配置状态检查视图集"""
    permission_classes = [IsAuthenticated]  # 需要认证访问

    @action(detail=False, methods=['get'])
    def check(self, request):
        """检查AI配置状态"""
        try:
            # 检查AI模型配置
            ai_model_configs = AIModelConfig.objects.filter(
                role__in=['writer', 'reviewer']
            ).exclude(role__in=['browser_use_text', 'browser_use_vision'])

            # 检查writer模型配置
            writer_model_enabled = ai_model_configs.filter(
                role='writer',
                is_active=True
            ).first()

            writer_model_disabled = ai_model_configs.filter(
                role='writer',
                is_active=False
            ).first()

            # 检查reviewer模型配置
            reviewer_model_enabled = ai_model_configs.filter(
                role='reviewer',
                is_active=True
            ).first()

            reviewer_model_disabled = ai_model_configs.filter(
                role='reviewer',
                is_active=False
            ).first()

            # 检查writer提示词配置
            writer_prompt_enabled = PromptConfig.objects.filter(
                prompt_type='writer',
                is_active=True
            ).first()

            writer_prompt_disabled = PromptConfig.objects.filter(
                prompt_type='writer',
                is_active=False
            ).first()

            # 检查reviewer提示词配置
            reviewer_prompt_enabled = PromptConfig.objects.filter(
                prompt_type='reviewer',
                is_active=True
            ).first()

            reviewer_prompt_disabled = PromptConfig.objects.filter(
                prompt_type='reviewer',
                is_active=False
            ).first()

            # 判断必需配置（writer）
            writer_configured = (
                writer_model_enabled is not None and
                writer_prompt_enabled is not None
            )

            # 判断可选配置（reviewer）
            reviewer_configured = (
                reviewer_model_enabled is not None and
                reviewer_prompt_enabled is not None
            )

            # 检查生成行为配置
            generation_config = GenerationConfig.get_active_config()

            # 判断是否有禁用的配置
            has_disabled = (
                writer_model_disabled is not None or
                writer_prompt_disabled is not None or
                reviewer_model_disabled is not None or
                reviewer_prompt_disabled is not None
            )

            # 判断整体状态
            if writer_configured:
                if has_disabled:
                    overall_status = 'disabled'
                    message = '配置完整，但部分配置处于禁用状态'
                else:
                    overall_status = 'enabled'
                    message = '配置完整且已启用'
            else:
                # writer配置不完整
                if writer_model_enabled or writer_prompt_enabled:
                    overall_status = 'disabled'
                    message = '检测到已配置但未启用的配置'
                else:
                    overall_status = 'not_configured'
                    message = '尚未配置AI模型和提示词'

            # 构建返回数据
            response_data = {
                'overall_status': overall_status,
                'message': message,
                'writer_model': {
                    'configured': writer_model_enabled is not None or writer_model_disabled is not None,
                    'enabled': writer_model_enabled is not None,
                    'name': (writer_model_enabled or writer_model_disabled).name if (writer_model_enabled or writer_model_disabled) else None,
                    'provider': (writer_model_enabled or writer_model_disabled).get_model_type_display() if (writer_model_enabled or writer_model_disabled) else None,
                    'id': (writer_model_enabled or writer_model_disabled).id if (writer_model_enabled or writer_model_disabled) else None,
                    'required': True
                },
                'writer_prompt': {
                    'configured': writer_prompt_enabled is not None or writer_prompt_disabled is not None,
                    'enabled': writer_prompt_enabled is not None,
                    'name': (writer_prompt_enabled or writer_prompt_disabled).name if (writer_prompt_enabled or writer_prompt_disabled) else None,
                    'id': (writer_prompt_enabled or writer_prompt_disabled).id if (writer_prompt_enabled or writer_prompt_disabled) else None,
                    'required': True
                },
                'reviewer_model': {
                    'configured': reviewer_model_enabled is not None or reviewer_model_disabled is not None,
                    'enabled': reviewer_model_enabled is not None,
                    'name': (reviewer_model_enabled or reviewer_model_disabled).name if (reviewer_model_enabled or reviewer_model_disabled) else None,
                    'id': (reviewer_model_enabled or reviewer_model_disabled).id if (reviewer_model_enabled or reviewer_model_disabled) else None,
                    'required': False
                },
                'reviewer_prompt': {
                    'configured': reviewer_prompt_enabled is not None or reviewer_prompt_disabled is not None,
                    'enabled': reviewer_prompt_enabled is not None,
                    'name': (reviewer_prompt_enabled or reviewer_prompt_disabled).name if (reviewer_prompt_enabled or reviewer_prompt_disabled) else None,
                    'id': (reviewer_prompt_enabled or reviewer_prompt_disabled).id if (reviewer_prompt_enabled or reviewer_prompt_disabled) else None,
                    'required': False
                },
                'generation_config': {
                    'configured': generation_config is not None,
                    'enabled': generation_config is not None,
                    'name': generation_config.name if generation_config else None,
                    'id': generation_config.id if generation_config else None,
                    'required': True,
                    'default_output_mode': generation_config.default_output_mode if generation_config else None,
                    'enable_auto_review': generation_config.enable_auto_review if generation_config else None
                }
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"检查配置状态失败: {e}")
            return Response({
                'error': f'检查配置状态失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _strip_markdown_fence(content: str) -> str:
    """去掉模型可能包裹的 ```markdown ... ``` 代码块"""
    if content.startswith('```'):
        lines = content.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        return '\n'.join(lines)
    return content


def _render_pdf_pages_to_base64(file_path: str, max_pages: int = 12, dpi: int = 150):
    """
    使用 pymupdf 将 PDF 各页渲染为 base64 PNG 列表。
    max_pages: 最多处理页数，避免 token 超限。
    """
    try:
        import fitz  # pymupdf
        import base64
        images = []
        doc = fitz.open(file_path)
        total = min(len(doc), max_pages)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for i in range(total):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")
            images.append(base64.b64encode(img_bytes).decode('utf-8'))
        doc.close()
        return images
    except ImportError:
        raise Exception('pymupdf 未安装，无法处理图片型 PDF。请执行：pip install pymupdf')
    except Exception as e:
        raise Exception(f'PDF 渲染失败: {e}')


# ──────────── 需求文档历史记录 ────────────

class GeneratedRequirementDocViewSet(viewsets.ModelViewSet):
    """AI 生成的需求文档历史记录"""
    queryset = GeneratedRequirementDoc.objects.all()
    http_method_names = ['get', 'delete', 'patch']  # 允许查看、删除、部分更新

    def get_serializer_class(self):
        if self.action == 'list':
            return GeneratedRequirementDocListSerializer
        return GeneratedRequirementDocSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_authenticated:
            # 获取当前用户参与的所有项目ID
            from apps.projects.models import Project
            member_project_ids = list(
                Project.objects.filter(
                    Q(owner=self.request.user) |
                    Q(members=self.request.user)
                ).values_list('id', flat=True).distinct()
            )
            # 可选：按请求参数过滤特定项目
            project_id = self.request.query_params.get('project_id')
            if project_id:
                try:
                    pid = int(project_id)
                    if pid in member_project_ids:
                        member_project_ids = [pid]
                    else:
                        # 用户不在该项目中，返回空
                        return qs.none()
                except (ValueError, TypeError):
                    pass
            if member_project_ids:
                qs = qs.filter(project_id__in=member_project_ids)
            else:
                # 用户不属于任何项目，退回到只看自己的
                qs = qs.filter(created_by=self.request.user)
        return qs.order_by('-created_at')

    def partial_update(self, request, *args, **kwargs):
        """只允许编辑 title 和 markdown_content，保护其他字段不被篡改"""
        allowed = {'title', 'markdown_content'}
        data = {k: v for k, v in request.data.items() if k in allowed}
        if not data:
            return Response({'error': '没有可更新的字段（仅允许 title、markdown_content）'},
                          status=status.HTTP_400_BAD_REQUEST)

        instance = self.get_object()
        if instance.created_by != request.user:
            return Response({'error': '无权修改他人文档'}, status=status.HTTP_403_FORBIDDEN)

        from .serializers import GeneratedRequirementDocSerializer
        serializer = GeneratedRequirementDocSerializer(instance, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RequirementAnalysisResultViewSet(viewsets.ModelViewSet):
    """需求拆解结果历史记录"""
    queryset = RequirementAnalysisResult.objects.all()
    http_method_names = ['get', 'post', 'delete']

    def get_serializer_class(self):
        if self.action == 'list':
            return RequirementAnalysisResultListSerializer
        return RequirementAnalysisResultSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_authenticated:
            # 获取当前用户参与的所有项目ID
            from apps.projects.models import Project
            member_project_ids = list(
                Project.objects.filter(
                    Q(owner=self.request.user) |
                    Q(members=self.request.user)
                ).values_list('id', flat=True).distinct()
            )
            # 可选：按请求参数过滤特定项目
            project_id = self.request.query_params.get('project_id')
            if project_id:
                try:
                    pid = int(project_id)
                    if pid in member_project_ids:
                        member_project_ids = [pid]
                    else:
                        return qs.none()
                except (ValueError, TypeError):
                    pass
            if member_project_ids:
                # 同项目成员可见 OR 创建者自己可见（兼容拆解结果未关联项目的历史数据）
                qs = qs.filter(
                    Q(project_id__in=member_project_ids) |
                    Q(created_by=self.request.user, project__isnull=True)
                )
            else:
                qs = qs.filter(created_by=self.request.user)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['get'], url_path='by-docs')
    def by_docs(self, request):
        """
        批量查询若干需求文档对应的最新拆解结果，用于前端重建 docAnalysisMap（刷新后持久化）。
        请求：?doc_ids=1,2,3
        返回：{ "map": { "1": {"id": ..., "title": ...}, ... } }
        """
        doc_ids_param = request.query_params.get('doc_ids', '')
        ids = []
        for x in doc_ids_param.split(','):
            x = x.strip()
            if x.isdigit():
                ids.append(int(x))
        if not ids:
            return Response({'map': {}})
        qs = self.get_queryset().filter(req_doc_id__in=ids)
        result_map = {}
        for r in qs.order_by('req_doc_id', '-created_at'):
            if r.req_doc_id is not None and r.req_doc_id not in result_map:
                result_map[str(r.req_doc_id)] = {'id': r.id, 'title': r.title}
        return Response({'map': result_map})

    @action(detail=False, methods=['post'], url_path='auto-fill-knowledge')
    def auto_fill_knowledge(self, request):
        """
        基于需求拆解精炼阶段确认的问答对，自动将新知识回填到项目知识库。
        
        请求体：{
            "confirmed_answers": [{"question": "...", "answer": "..."}],
            "project_id": 1  （可选，不传则不创建）
        }
        返回：{"created_count": N, "skipped_count": M, "entries": [...]}
        """
        import json
        confirmed_answers = request.data.get('confirmed_answers') or []
        if isinstance(confirmed_answers, str):
            try:
                confirmed_answers = json.loads(confirmed_answers)
            except json.JSONDecodeError:
                confirmed_answers = []
        
        project_id = request.data.get('project_id')
        if not project_id:
            return Response({'created_count': 0, 'skipped_count': 0, 'entries': [], 'message': '缺少 project_id'})
        
        # 取 AI 模型配置（用于智能判断是否为新知识）
        model_config = (
            AIModelConfig.objects.filter(role='analyzer', is_active=True).first()
            or AIModelConfig.objects.filter(role='writer', is_active=True).first()
        )
        
        # 透传功能模块/版本标签（可选，用于知识条目标注同名功能，增强语义区分）
        version_tag = (request.data.get('version') or '').strip()
        module_tag = (request.data.get('module') or request.data.get('feature_module') or '').strip()
        result = AIModelService.auto_fill_knowledge_from_confirmations(
            confirmed_answers=confirmed_answers,
            project_id=int(project_id),
            user_id=request.user.id,
            model_config=model_config,
            version_tag=version_tag,
            module_tag=module_tag,
        )
        return Response(result)


class ClarificationQuestionViewSet(viewsets.ModelViewSet):
    """需求拆解「深度追问清单」条目 CRUD + 批量保存"""
    serializer_class = ClarificationQuestionSerializer
    http_method_names = ['get', 'post', 'put', 'patch', 'delete']

    def get_queryset(self):
        qs = ClarificationQuestion.objects.all()
        result_id = self.request.query_params.get('analysis_result')
        if result_id:
            qs = qs.filter(analysis_result_id=result_id)
        if self.request.user.is_authenticated:
            # 同项目成员互相可见（与 GeneratedRequirementDocViewSet 一致）
            from apps.projects.models import Project
            member_project_ids = list(
                Project.objects.filter(
                    Q(owner=self.request.user) |
                    Q(members=self.request.user)
                ).values_list('id', flat=True).distinct()
            )
            if member_project_ids:
                qs = qs.filter(analysis_result__project_id__in=member_project_ids)
            else:
                qs = qs.filter(analysis_result__created_by=self.request.user)
        return qs.order_by('order', 'id')

    @action(detail=False, methods=['post'], url_path='save-all')
    def save_all(self, request):
        """
        批量保存某拆解结果下的全部追问项：先删除旧条目，再整体重建。
        请求体：{ analysis_result: int, items: [{question, answer, category, status}] }
        """
        analysis_result_id = request.data.get('analysis_result')
        items = request.data.get('items', [])
        if not analysis_result_id:
            return Response({'error': '缺少 analysis_result'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(items, list):
            return Response({'error': 'items 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = RequirementAnalysisResult.objects.get(id=analysis_result_id)
        except RequirementAnalysisResult.DoesNotExist:
            return Response({'error': '拆解结果不存在'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            # 同项目成员有写入权限（与列表/详情可见性保持一致）
            from apps.projects.models import Project
            is_member = Project.objects.filter(
                Q(id=result.project_id) &
                (Q(owner=request.user) | Q(members=request.user))
            ).exists()
            if not is_member:
                # 兼容老数据：拆解结果未关联项目时，退回原 created_by 校验
                if result.created_by_id != request.user.id:
                    return Response({'error': '无权限操作该拆解结果'}, status=status.HTTP_403_FORBIDDEN)

        ClarificationQuestion.objects.filter(analysis_result_id=analysis_result_id).delete()
        created_ids = []
        for idx, it in enumerate(items):
            if not isinstance(it, dict) or not str(it.get('question', '')).strip():
                continue
            cq = ClarificationQuestion.objects.create(
                analysis_result=result,
                question=str(it.get('question', '')).strip(),
                answer=str(it.get('answer', '') or ''),
                category=str(it.get('category', '') or '其他'),
                status=str(it.get('status', 'pending') or 'pending'),
                order=idx
            )
            created_ids.append(cq.id)
        return Response({'saved': len(created_ids), 'ids': created_ids})


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_requirement_doc(request):
    """
    AI将原始需求文案整理为结构化Markdown需求文档。
    支持三种输入模式：
      1. raw_text: 直接传入文字（粘贴 / 文字型文件提取结果）
      2. document_ids: 图片型 PDF 文档 ID 列表，调用 AI 视觉（vision）API 识别
      3. 混合模式：document_ids（图片型）+ text_document_ids（文字型）+ raw_text，合并生成
    """
    try:
        raw_text = request.data.get('raw_text', '').strip()
        document_ids = request.data.get('document_ids', [])  # 图片型 PDF ID 列表
        text_document_ids = request.data.get('text_document_ids', [])  # 文字型文档 ID 列表
        project_id = request.data.get('project_id')  # 关联项目 ID（用于读取知识库辅助生成）

        # 兼容旧的 document_id 单值
        document_id = request.data.get('document_id', None)
        if document_id and not document_ids:
            document_ids = [document_id]

        if not raw_text and not document_ids and not text_document_ids:
            return Response({'error': '请输入原始需求文本或上传需求文件'}, status=status.HTTP_400_BAD_REQUEST)

        # 有图片型 PDF → 优先 writer_vision，回退 writer
        if document_ids:
            writer_model = (
                AIModelConfig.objects.filter(role='writer_vision', is_active=True).first()
                or AIModelConfig.objects.filter(role='writer', is_active=True).first()
            )
            used_role = 'writer_vision' if (writer_model and writer_model.role == 'writer_vision') else 'writer（回退）'
        else:
            writer_model = AIModelConfig.objects.filter(role='writer', is_active=True).first()
            used_role = 'writer'

        if not writer_model:
            return Response(
                {'error': '未找到可用的AI模型配置，请先在设置中配置并启用Writer或视觉识别模型'},
                status=status.HTTP_400_BAD_REQUEST
            )
        logger.info(f'需求文档生成 - 使用模型角色: {used_role}, 模型: {writer_model.model_name}')

        # ── 项目知识库上下文（如指定了项目） ──
        knowledge_section = ''
        if project_id:
            try:
                from apps.projects.models import Project
                project_obj = Project.objects.get(id=int(project_id))
                knowledge_section = AIModelService.build_knowledge_context_from_project(project_obj)
                if knowledge_section:
                    logger.info(f'需求文档生成 - 已加载项目{project_id}知识库上下文 ({len(knowledge_section)}字符)')
            except Exception as e:
                logger.warning(f'需求文档生成 - 加载项目知识库失败: {e}')

        doc_structure_prompt = (
            "文档结构要求（按顺序输出，标题使用中文）：\n"
            "# [功能/产品名称]\n\n"
            "## 一、需求概述\n简要描述背景与目标。\n\n"
            "## 二、功能描述\n详细描述所有功能点，使用有序列表，每条单独成行。\n\n"
            "## 三、业务流程\n描述核心业务流程，使用步骤列表。\n\n"
            "## 四、用户场景\n列举2~4个典型使用场景（含正常场景和异常场景）。\n\n"
            "## 五、边界与异常\n用Markdown表格描述边界条件和异常处理规则。\n\n"
            "## 六、非功能需求（如有）\n性能、兼容性、安全性等要求，若无则省略本节。\n\n"
            "注意：保持原始需求完整性，补充隐含边界条件，使用专业需求文档语言，仅输出Markdown内容不加前言或代码块包裹。"
        )

        # ── 纯文字模式（无图片型PDF） ──
        if not document_ids:
            combined_text = raw_text

            # 如有文字型文档ID，提取其内容合并
            if text_document_ids:
                from .models import RequirementDocument as ReqDocModel
                text_parts = []
                for did in text_document_ids:
                    try:
                        doc_obj = ReqDocModel.objects.get(id=did)
                        extracted = DocumentProcessor.extract_text(doc_obj.file.path)
                        if extracted and extracted.strip():
                            text_parts.append(f"【文档：{doc_obj.title or '文字文档'}】\n{extracted}")
                    except ReqDocModel.DoesNotExist:
                        logger.warning(f'文字文档 {did} 不存在，跳过')
                    except Exception as e:
                        logger.warning(f'提取文字文档 {did} 失败: {e}')
                if text_parts:
                    combined_text = '\n\n---\n\n'.join(text_parts)
                    if raw_text:
                        combined_text += '\n\n---\n\n' + raw_text
                elif not combined_text:
                    return Response({'error': '所有文字文档提取失败或内容为空'}, status=status.HTTP_400_BAD_REQUEST)

            if not combined_text:
                return Response({'error': '无有效文字内容，请提供原始需求文本或上传有效的文档'}, status=status.HTTP_400_BAD_REQUEST)

            # 构建带知识库上下文的系统提示
            kb_instruction = f"\n\n{knowledge_section}\n" if knowledge_section else ''
            system_prompt = (
                "你是一位专业的需求分析师。请将用户提供的原始需求文本，整理成一份结构清晰、格式规范的Markdown需求文档。\n\n"
                + doc_structure_prompt
                + kb_instruction
                + ("\n（以上【项目业务知识库】为该项目的背景知识，生成文档时请参考其中的术语、约束和业务规则。）" if knowledge_section else '')
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请将以下原始需求整理成结构化的Markdown需求文档：\n\n{combined_text}"}
            ]

        # ── 视觉模式（有多文件） ──
        else:
            from .models import RequirementDocument as ReqDocModel

            # 1. 渲染所有图片型 PDF 的页面
            all_images = []
            pdf_names = []
            for did in document_ids:
                try:
                    doc_obj = ReqDocModel.objects.get(id=did)
                except ReqDocModel.DoesNotExist:
                    return Response({'error': f'未找到文档（id={did}），请重新上传'}, status=status.HTTP_404_NOT_FOUND)

                file_path = doc_obj.file.path
                pdf_names.append(doc_obj.title or os.path.basename(file_path))
                logger.info(f'多文件视觉识别 - 渲染: {file_path}')
                pages = _render_pdf_pages_to_base64(file_path, max_pages=8, dpi=150)
                if pages:
                    all_images.extend(pages)

            if not all_images:
                return Response({'error': '所有图片型PDF均无法渲染，文件可能已损坏'}, status=status.HTTP_400_BAD_REQUEST)

            # 2. 收集所有文字型文档的内容
            combined_text_parts = []
            if text_document_ids:
                for did in text_document_ids:
                    try:
                        doc_obj = ReqDocModel.objects.get(id=did)
                        extracted = DocumentProcessor.extract_text(doc_obj.file.path)
                        if extracted and extracted.strip():
                            combined_text_parts.append(
                                f"【文档：{doc_obj.title or '文字文档'}】\n{extracted}"
                            )
                    except Exception as e:
                        logger.warning(f'提取文字文档 {did} 失败: {e}')

            if raw_text:
                combined_text_parts.append(f"【用户提供的文字】\n{raw_text}")

            # 3. 构建 vision 指令
            total_pages = len(all_images)
            file_list = '、'.join(pdf_names)
            kb_context_for_vision = f"\n\n{knowledge_section}\n（以上【项目业务知识库】为该项目的背景知识，生成文档时请参考其中的术语、约束和业务规则。）" if knowledge_section else ''
            vision_instruction = (
                "你是一位专业的需求分析师。以下是一批需求文档的截图（共 {} 页，来自 {} 个文件：{}），"
                "请按页码顺序识别所有图片中的文字、表格、示意图内容，结合下方文字信息，"
                "整理成一份结构化的Markdown需求文档。\n\n"
                "{}"
                "{}"
                "输出要求：仅输出Markdown内容，不加任何前言、解释或代码块包裹。"
            ).format(total_pages, len(document_ids), file_list, doc_structure_prompt, kb_context_for_vision)

            content_blocks = [{"type": "text", "text": vision_instruction}]

            # 附加文字内容
            if combined_text_parts:
                text_context = "\n\n---\n\n".join(combined_text_parts)
                content_blocks.append({"type": "text", "text": f"以下为系统提取的文字内容：\n\n{text_context}"})

            # 附加所有图片
            for b64 in all_images:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}
                })

            messages = [{"role": "user", "content": content_blocks}]

        # ── 调用 AI ──
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                asyncio.wait_for(
                    AIModelService.call_openai_compatible_api(writer_model, messages),
                    timeout=300.0  # 多文件可能较久
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

        markdown_content = _strip_markdown_fence(result['choices'][0]['message']['content'])
        markdown_content = markdown_content.strip()

        # ── 持久化保存到历史记录 ──
        doc_id = None
        try:
            # 提取标题（从第一个 # 行）
            title = '需求文档'
            for line in markdown_content.split('\n'):
                stripped = line.strip()
                if stripped.startswith('# '):
                    title = stripped[2:].strip()
                    break
                elif stripped.startswith('## '):
                    title = stripped[3:].strip()
                    break

            # 判断来源类型
            if document_ids:
                source_type = 'file'
                source_detail = f'视觉识别 {len(document_ids) + len(text_document_ids)} 个文件'
            elif text_document_ids:
                source_type = 'file'
                source_detail = f'{len(text_document_ids)} 个文档合并'
            elif raw_text:
                source_type = 'text'
                source_detail = raw_text[:200]
            else:
                source_type = 'text'
                source_detail = ''

            # 解析项目对象（用于保存到文档记录）
            _project_obj = None
            if project_id:
                try:
                    from apps.projects.models import Project as _Proj
                    _project_obj = _Proj.objects.get(id=int(project_id))
                except Exception:
                    pass

            doc = GeneratedRequirementDoc.objects.create(
                title=title[:300],
                markdown_content=markdown_content,
                source_type=source_type,
                source_detail=source_detail[:500],
                project=_project_obj,
                created_by=request.user if request.user.is_authenticated else None
            )
            doc_id = doc.id
            logger.info(f'需求文档已保存到历史记录: id={doc_id}, title={title}')
        except Exception as save_err:
            logger.warning(f'保存需求文档历史记录失败（不影响返回）: {save_err}')

        return Response({'markdown': markdown_content, 'doc_id': doc_id})

    except asyncio.TimeoutError:
        return Response({'error': 'AI生成超时，请稍后再试'}, status=status.HTTP_408_REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"生成需求文档失败: {e}")
        return Response({'error': f'生成失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_knowledge_base(request):
    """
    AI 根据功能点文字 / 产品设计截图，自动生成项目知识库（Markdown 文件）。
    请求体:
      - project_id: int (必填)
      - raw_text: str (可选，功能点描述文字)
      - images: list[str] (可选，base64 编码的截图)
      - dry_run: bool (可选，默认 false；true 时只返回预览不写入文件)
      - resolve_replies: list[dict] (可选，人工确认回复，用于精炼不确定内容)
        每项: {"file": "路径.md", "question": "问题", "answer": "回复"}
      - original_files: list[dict] (配合 resolve_replies 使用，原始生成的文件)
    返回:
      - files: [{"path": "01-xxx.md", "title": "xxx", "content": "..."}]
      - uncertain_items: [{"file": "路径", "question": "...", "context": "当前推测..."}]
      - preview: str (完整 markdown 预览)
      - written: bool (是否已写入文件系统)
    """
    try:
        project_id = request.data.get('project_id')
        raw_text = (request.data.get('raw_text') or '').strip()
        images = request.data.get('images') or []
        dry_run = request.data.get('dry_run', False)
        resolve_replies = request.data.get('resolve_replies') or []
        original_files = request.data.get('original_files') or []

        logger.info(f'[KB-GEN] project_id={project_id}, raw_text_len={len(raw_text)}, images_len={len(images)}, resolve_replies_len={len(resolve_replies)}, dry_run={dry_run}')
        logger.info(f'[KB-GEN] request.data keys={list(request.data.keys())}')

        if not project_id:
            return Response({'error': '请指定目标项目'}, status=status.HTTP_400_BAD_REQUEST)
        if not raw_text and not images and not resolve_replies:
            logger.warning(f'[KB-GEN] rejected: empty input. raw_text={repr(raw_text[:50])}, images={images}')
            return Response({'error': '请提供功能点描述文字或产品设计截图'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取项目
        from apps.projects.models import Project
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'error': '项目不存在'}, status=status.HTTP_404_NOT_FOUND)

        # 有图片优先 writer_vision，否则直接用 writer
        if images:
            writer_model = (
                AIModelConfig.objects.filter(role='writer_vision', is_active=True).first()
                or AIModelConfig.objects.filter(role='writer', is_active=True).first()
            )
        else:
            writer_model = AIModelConfig.objects.filter(role='writer', is_active=True).first()
        if not writer_model:
            return Response({'error': '未找到可用的AI模型配置，请先在设置中启用 Writer 或 Vision 模型'},
                          status=status.HTTP_400_BAD_REQUEST)

        # 收集现有知识库内容作为参考
        kb_context = AIModelService.build_knowledge_context_from_project(project)
        # 提前解析知识库路径（用于文件清单 + 后续写入）
        kb_path = AIModelService._resolve_knowledge_path(project) if project.knowledge_base_path else ''
        # 如果配置了路径但目录不存在，自动创建
        if not kb_path and project.knowledge_base_path:
            raw_path = project.knowledge_base_path.strip()
            # 解析环境变量
            for match in re.finditer(r'\$\{(\w+)\}', raw_path):
                raw_path = raw_path.replace(match.group(0), os.environ.get(match.group(1), ''))
            raw_path = os.path.expanduser(raw_path)
            os.makedirs(raw_path, exist_ok=True)
            kb_path = os.path.abspath(raw_path)
        # 构建现有文件清单（供 AI 精准匹配已有文件）
        file_inventory = _build_file_inventory(kb_path) if kb_path else ''

        # 构建指令
        kb_structure_guide = (
            "【知识库文件结构规范】\n"
            "⚠️ 核心原则：先匹配现有文件，再考虑新建。不要重复创建已有文件！\n\n"
            "步骤：\n"
            "1. 先查看下方【现有文件清单】，逐一确认每个已有文件覆盖了什么主题\n"
            "2. 对于新内容，判断是否可以合并到某个已有文件中（主题相近即可合并）\n"
            "3. 只有主题完全无法匹配到任何已有文件时，才创建新文件\n\n"
            "文件命名规范：使用两位数字前缀排序（如 01-xxx.md），中文标题。\n"
            "新文件编号规则：取现有最大编号 +1，若是子目录则从 01 开始。\n\n"
            "常见主题匹配表（核心！）：\n"
            "- 产品概述 ≈ 产品简介 ≈ 产品介绍 ≈ 产品概况 → 同一文件\n"
            "- 功能模块 ≈ 核心功能 ≈ 主要功能 ≈ 功能列表 → 同一文件\n"
            "- 业务规则 ≈ 业务约束 ≈ 业务逻辑 ≈ 规则说明 → 同一文件\n"
            "- 测试重点 ≈ 测试要点 ≈ 测试关注点 ≈ 测试策略 → 同一文件\n"
            "- 用户角色 ≈ 用户权限 ≈ 角色权限 → 同一文件\n"
            "- 数据模型 ≈ 数据结构 ≈ 数据字典 → 同一文件\n\n"
            "输出格式示例：\n"
            "--- FILE: 01-产品简介.md ---\n"
            "## 补充内容\n..."
        )

        system_prompt = (
            "你是一位资深的产品分析师和知识库架构师。你的任务是根据用户提供的产品功能点和设计截图，"
            "分析产品业务逻辑，在现有知识库基础上补充/更新内容。\n\n"
            "核心要求：\n"
            "1. 仔细分析所有截图中的界面元素、交互流程、按钮文案、页面结构\n"
            "2. 结合功能点文字，提炼出业务背景、核心功能模块、业务规则和测试重点\n"
            "3. 【关键】先检查【现有文件清单】，每个主题必须先尝试匹配已有文件\n"
            "4. 匹配成功 → 使用已有文件的路径（包括子目录），在内容开头标注 `## 🆕 本次补充`\n"
            "5. 匹配失败 → 按编号递增规则创建新文件\n"
            "6. 使用中文输出，内容专业、具体、可执行\n"
            "7. 优先从截图中提取信息，文字作为补充\n"
            "8. 不要输出无关的前言、解释或代码块包裹\n"
            "9. 对于无法100%确定的内容，在行内用 `[⚠️ 待确认: 具体问题？]` 标记\n"
            "10. 在所有文件之后，用 `--- UNCERTAIN ---` 章节汇总所有待确认项，每项格式：\n"
            "   - 问题: 具体问题描述\n"
            "   - 文件: 所在文件名\n"
            "   - 当前推测: 你基于现有信息做的推测\n"
            "   每项之间用 `---` 分隔\n\n"
            "重要提醒：宁可把新内容合并到已有文件，也不要创建多个主题相同的文件。"
        )

        user_parts = []
        user_parts.append({"type": "text", "text": "请根据以下信息，在现有知识库基础上补充/更新项目知识库："})
        user_parts.append({"type": "text", "text": kb_structure_guide})

        # 文件清单优先级最高（放在最前面让 AI 先匹配）
        if file_inventory:
            user_parts.append({"type": "text", "text": file_inventory})

        if kb_context:
            user_parts.append({"type": "text", "text": f"【现有知识库完整内容（供精读参考）】\n{kb_context}"})

        if raw_text:
            user_parts.append({"type": "text", "text": f"【用户提供的功能点和产品描述】\n{raw_text}"})

        if images:
            user_parts.append({"type": "text", "text": f"以下为 {len(images)} 张产品设计截图，请逐张分析："})
            for b64 in images:
                # 确保 base64 有正确的 data URI 前缀
                if b64.startswith('data:'):
                    img_url = b64
                else:
                    img_url = f"data:image/png;base64,{b64}"
                user_parts.append({"type": "image_url", "image_url": {"url": img_url}})

        if resolve_replies:
            # ===== 人工确认精炼模式 =====
            resolve_system = (
                "你是一位资深的产品分析师。用户对之前生成的知识库中的不确定项进行了人工确认，"
                "请根据用户的回复精炼对应的 Markdown 文件，确保内容准确、完整。\n\n"
                "要求：\n"
                "1. 将用户的确认信息融入对应文件中\n"
                "2. 去除文件中的 `[⚠️ 待确认: ...]` 标记，替换为确定的内容\n"
                "3. 保持原来的文件结构，不要新增或删除文件\n"
                "4. 如果用户回复了某个问题，文件中相关描述必须相应修改\n"
                "5. 不要输出无关的前言或代码块包裹"
            )
            replies_text = "\n".join(
                f"- 文件: {r.get('file', '未知')}\n  问题: {r.get('question', '未知')}\n  回复: {r.get('answer', '(未回复)')}"
                for r in resolve_replies
            )
            originals_text = "\n\n".join(
                f"--- FILE: {f['path']} ---\n{f['content']}"
                for f in original_files
            )
            messages = [
                {"role": "system", "content": resolve_system},
                {"role": "user", "content": (
                    f"以下是用户对不确定项的回复：\n\n{replies_text}\n\n"
                    f"请根据以上回复，精炼以下知识库文件：\n\n{originals_text}"
                )}
            ]
            logger.info(f"[KB精炼] 项目={project.name}(id={project_id}), 待确认项={len(resolve_replies)}")
        else:
            # ===== 首次生成模式 =====
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_parts}
            ]
            logger.info(f"[KB生成] 项目={project.name}(id={project_id}), 文字长度={len(raw_text)}, 图片数={len(images)}")

        # 调用 AI
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                asyncio.wait_for(
                    AIModelService.call_openai_compatible_api(writer_model, messages, max_tokens=16000),
                    timeout=300.0
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

        full_response = result['choices'][0]['message']['content']
        logger.info(f"[KB生成] AI 响应长度={len(full_response)}")

        # 解析 AI 输出中的文件分隔符
        files = _parse_kb_files(full_response)
        # 解析不确定项（仅在首次生成时有，精炼模式没有）
        uncertain_items = _parse_uncertain_items(full_response) if not resolve_replies else []
        preview = full_response if dry_run else _format_kb_preview(files)

        # 如果不是 dry_run 且项目有知识库路径，写入/合并文件
        written = False
        merged_files = []  # 记录哪些文件是合并而非新建
        if not dry_run and kb_path:
            written_files = []
            for f in files:
                try:
                    full_path = os.path.normpath(os.path.join(kb_path, f['path']))
                    if not full_path.startswith(os.path.abspath(kb_path)):
                        logger.warning(f"[KB生成] 路径穿越拒绝: {f['path']}")
                        continue
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    # 检查是否匹配到已有文件
                    file_exists = os.path.isfile(full_path)
                    if file_exists:
                        # 智能合并：读取已有内容，取两者中更完整的保留
                        with open(full_path, 'r', encoding='utf-8') as fh:
                            existing_content = fh.read()
                        # 如果AI输出中已包含 ## 🆕 本次补充 标记，说明AI做了增量更新
                        # 把已有内容放在前面，AI的新分析放在后面
                        merged = (
                            f"{existing_content.strip()}\n\n"
                            f"---\n\n"
                            f"## 🆕 AI 补充分析 ({timezone.now().strftime('%Y-%m-%d %H:%M')})\n\n"
                            f"{f['content'].strip()}"
                        )
                        with open(full_path, 'w', encoding='utf-8') as fh:
                            fh.write(merged)
                        merged_files.append(f['path'])
                        logger.info(f"[KB生成] 已合并到已有文件: {full_path}")
                    else:
                        with open(full_path, 'w', encoding='utf-8') as fh:
                            fh.write(f['content'])
                        logger.info(f"[KB生成] 已新建: {full_path}")
                    written_files.append(f['path'])
                except Exception as e:
                    logger.error(f"[KB生成] 写入失败 {f['path']}: {e}")
            written = len(written_files) > 0
            logger.info(f"[KB生成] 共处理 {len(written_files)}/{len(files)} 个文件（合并 {len(merged_files)}，新建 {len(written_files)-len(merged_files)}）")

        return Response({
            'files': [
                {'path': f['path'], 'title': f['title'], 'content': f['content']}
                for f in files
            ],
            'uncertain_items': uncertain_items,
            'preview': preview,
            'written': written,
            'merged_files': merged_files if not dry_run else [],
            'dry_run': dry_run
        })

    except asyncio.TimeoutError:
        return Response({'error': 'AI生成超时，请减少上传内容或稍后再试'}, status=status.HTTP_408_REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"[KB生成] 失败: {e}")
        import traceback
        traceback.print_exc()
        return Response({'error': f'生成失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _build_file_inventory(kb_path: str) -> str:
    """扫描现有知识库文件目录，构建文件清单供 AI 做主题匹配"""
    if not kb_path or not os.path.isdir(kb_path):
        return ''

    lines = [
        '## 📋 现有知识库文件清单（必须逐项匹配！）',
        '',
        '**在生成任何新文件前，你必须先检查以下清单，判断每个主题是否已被覆盖。**',
        '**匹配到 → 使用该文件路径；未匹配到 → 才可以新建。**',
        '',
    ]

    def scan(dir_path: str, depth: int = 0, prefix: str = ''):
        try:
            items = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except PermissionError:
            return
        for entry in items:
            if entry.name.startswith('.'):
                continue
            rel = f"{prefix}{entry.name}"
            if entry.is_file() and entry.name.endswith('.md'):
                try:
                    with open(entry.path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    # 提取第一个 # 标题作为主题摘要
                    first_heading = ''
                    for line in content.split('\n'):
                        line_stripped = line.strip()
                        if line_stripped.startswith('# ') and not line_stripped.startswith('## '):
                            first_heading = line_stripped[2:].strip()
                            break
                    title = re.sub(r'^\d{1,3}[-_]', '', entry.name)
                    title = re.sub(r'\.md$', '', title)
                    summary = first_heading or title
                    lines.append(f'- 📄 **{rel}** → 主题:「{summary}」')
                except Exception:
                    lines.append(f'- 📄 **{rel}**')
            elif entry.is_dir() and depth < 2:
                lines.append(f'- 📁 **{rel}/**')
                scan(entry.path, depth + 1, f'{rel}/')

    scan(kb_path)
    lines.append('')
    lines.append('---')
    lines.append('**语义匹配指引（必须遵守）：**')
    lines.append('- 产品概述 = 产品简介 = 产品介绍 → 同一个文件')
    lines.append('- 功能模块 = 核心功能 = 主要功能 → 同一个文件')
    lines.append('- 业务规则 = 业务约束 = 业务逻辑 → 同一个文件')
    lines.append('- 测试重点 = 测试要点 = 测试关注点 → 同一个文件')
    lines.append('- 「页面A的交互细节」→ 合并到「功能模块」的文件里，不要新建')
    lines.append('- 「某个功能的补充说明」→ 合并到对应功能的已有文件里')
    lines.append('')
    lines.append('> 简言之：能合并就合并，不要新建文件，除非是完全不同的主题。')

    return '\n'.join(lines)


def _parse_kb_files(ai_output):
    """解析 AI 响应中的 `--- FILE: 路径.md ---` 分隔符，拆分为文件列表"""
    files = []
    # 匹配模式：--- FILE: 路径.md ---
    pattern = re.compile(r'---\s*FILE:\s*(.+?\.md)\s*---\s*\n')
    parts = pattern.split(ai_output)
    # parts[0] 是第一个 FILE 之前的内容（前言），跳过
    # parts[i] 是文件名，parts[i+1] 是内容
    i = 1
    while i < len(parts) - 1:
        filepath = parts[i].strip()
        content = parts[i + 1].strip()
        if content:
            # 从文件名提取标题（去掉编号前缀）
            title = re.sub(r'^\d{1,3}[-_]', '', os.path.basename(filepath))
            title = re.sub(r'\.md$', '', title)
            files.append({
                'path': filepath,
                'title': title,
                'content': content
            })
        i += 2

    # 如果没有解析出 FILE 分隔符，整个响应作为一个文件
    if not files and ai_output.strip():
        files.append({
            'path': '01-知识库.md',
            'title': '知识库',
            'content': ai_output.strip()
        })

    return files


def _parse_uncertain_items(ai_output):
    """从 AI 响应中提取 `--- UNCERTAIN ---` 段落的待确认项"""
    items = []
    # 提取 UNCERTAIN 段
    match = re.search(r'---\s*UNCERTAIN\s*---\s*\n(.*?)(?:\Z)', ai_output, re.DOTALL)
    if not match:
        return items

    section = match.group(1).strip()
    # 按 --- 分隔每个待确认项
    blocks = re.split(r'\n---\n', section)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        item = {}
        for line in block.split('\n'):
            line = line.strip()
            if line.startswith('- 问题:') or line.startswith('问题:'):
                item['question'] = line.split(':', 1)[1].strip()
            elif line.startswith('- 文件:') or line.startswith('文件:'):
                item['file'] = line.split(':', 1)[1].strip()
            elif line.startswith('- 当前推测:') or line.startswith('当前推测:'):
                item['context'] = line.split(':', 1)[1].strip()
        if item.get('question'):
            item.setdefault('file', '未知文件')
            item.setdefault('context', '')
            items.append(item)

    return items


def _format_kb_preview(files):
    """格式化文件预览为 Markdown"""
    lines = ['# 📚 生成的知识库预览\n']
    for f in files:
        lines.append(f'### 📄 {f["path"]}')
        lines.append(f'')
        lines.append(f['content'][:500])
        if len(f['content']) > 500:
            lines.append(f'\n...（共 {len(f["content"])} 字符）')
        lines.append('')
    return '\n'.join(lines)


@csrf_exempt
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_requirement_template(request):
    """获取需求文档空白模板"""
    template = """# [功能名称]

## 一、需求概述

**背景：** 请描述该需求的业务背景

**目标：** 请描述需要达成的目标

---

## 二、功能描述

### 2.1 核心功能

1. **功能点1**：描述功能内容
2. **功能点2**：描述功能内容
3. **功能点3**：描述功能内容

### 2.2 功能说明

（对各功能点进行详细补充说明）

---

## 三、业务流程

**主流程：**

1. 步骤1
2. 步骤2
3. 步骤3

---

## 四、用户场景

**场景1：正常使用场景**
- 前置条件：
- 操作步骤：
- 预期结果：

**场景2：异常/边界场景**
- 前置条件：
- 操作步骤：
- 预期结果：

---

## 五、边界与异常

| 场景 | 触发条件 | 预期处理 |
|------|----------|---------|
| 空输入 | 用户提交空内容 | 提示用户填写必填项 |
| 超长输入 | 超出字符限制 | 限制输入并提示 |
| 网络异常 | 请求超时/断网 | 提示错误，支持重试 |
| 重复操作 | 重复提交/点击 | 防抖处理或提示已操作 |

---

## 六、非功能需求

- **性能：** 接口响应时间 ≤ 3秒
- **兼容性：** 支持 iOS 14+ / Android 9+
- **安全：** 需要登录态验证
"""
    return Response({'template': template})


# ──────────────────────────────────────────────
# 深度追问（流式 SSE）
# ──────────────────────────────────────────────
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def deep_question(request):
    """
    基于需求拆解结果的流式深度追问。
    接收 question + context（拆解结果） + history（对话历史），
    返回 text/event-stream 流式响应。
    """
    from django.http import StreamingHttpResponse

    question = request.data.get('question', '').strip()
    context = request.data.get('context', '')
    history = request.data.get('history', [])

    if not question:
        return Response({'error': '问题不能为空'}, status=status.HTTP_400_BAD_REQUEST)

    # 获取 writer/analyzer 模型
    model_config = (
        AIModelConfig.objects.filter(role='analyzer', is_active=True).first()
        or AIModelConfig.objects.filter(role='writer', is_active=True).first()
    )
    if not model_config:
        return Response({'error': '未配置可用的AI模型（需配置 analyzer 或 writer 角色）'}, status=status.HTTP_400_BAD_REQUEST)

    # 构造消息
    system_msg = (
        "你是一位资深的需求分析与测试专家。用户已经对一段需求进行了拆解分析，"
        "现在会基于拆解结果向你提出深度追问。你的任务是：\n"
        "1. 结合「上下文中的拆解结果」回答问题\n"
        "2. 如果问题涉及边界条件、异常场景、数据依赖等，给出详细分析\n"
        "3. 回答使用 Markdown 格式，结构清晰\n"
        "4. 不要重复上下文中已有的内容，聚焦于问题的核心\n"
        "5. 如果上下文信息不足以回答，明确指出缺失点并给出建议"
    )
    messages = [{'role': 'system', 'content': system_msg}]

    # 截断上下文避免超长（保留最近 8000 字）
    if len(context) > 8000:
        context = context[:4000] + '\n...（中间省略）...\n' + context[-4000:]
    messages.append({'role': 'user', 'content': f'【需求拆解结果】\n{context}\n'})

    # 加入历史对话
    for h in history[-8:]:  # 最近 8 轮
        role = h.get('role', 'user')
        content = h.get('content', '')
        if content:
            messages.append({'role': role, 'content': content})

    messages.append({'role': 'user', 'content': question})

    def event_stream():
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            gen = loop.run_until_complete(
                __async_stream(model_config, messages)
            )
            for chunk in gen:
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error(f"[深度追问] 失败: {e}")
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            loop.close()

    response = StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


async def __async_stream(config, messages):
    """异步生成器：调用流式 API 并逐块产出文本"""
    async for chunk in AIModelService.call_openai_compatible_api_stream(
        config, messages, max_tokens=4096
    ):
        if chunk:
            yield chunk


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def refine_analysis(request):
    """
    基于人工确认回复，精炼需求拆解结果（对齐 AI 用例评审的 resolve_replies 闭环）。
    接收 original_report（原始拆解结果）+ resolve_replies（人工确认回复列表），
    返回精炼后的 report（已去除待确认标记、融入确认结论）。
    """
    original_report = request.data.get('original_report', '')
    resolve_replies = request.data.get('resolve_replies') or []
    if not original_report:
        return Response({'error': '缺少 original_report'}, status=status.HTTP_400_BAD_REQUEST)

    model_config = (
        AIModelConfig.objects.filter(role='analyzer', is_active=True).first()
        or AIModelConfig.objects.filter(role='writer', is_active=True).first()
    )
    if not model_config:
        return Response({'error': '未配置可用的AI模型（需配置 analyzer 或 writer 角色）'}, status=status.HTTP_400_BAD_REQUEST)

    replies_text = ''
    for r in resolve_replies:
        q = r.get('question', '')
        a = r.get('answer', '') or r.get('context', '')
        if q:
            replies_text += f"- 问题：{q}\n  人工确认/回复：{a}\n"

    system_msg = (
        "你是一位资深的需求分析与测试专家。用户已经对一段需求做了拆解分析，"
        "并在结果中标记了一些「待确认」项。现在用户针对这些待确认项给出了人工确认或回复。\n"
        "你的任务：\n"
        "1. 将人工确认的结论融入原拆解结果，修正相关内容\n"
        "2. 去除所有「⚠️ 待确认」「[⚠️ 待确认: ...]」「待确认风险」等不确定标记\n"
        "3. 保持原有的 Markdown 结构与表格格式（含插接矩阵 7 列表格）不变\n"
        "4. 若某待确认项用户未给出回复，则基于上下文做出最合理假设并标注为已确认\n"
        "5. 返回完整精炼后的拆解结果全文\n"
        "6. 【关键】人工确认意味着这些不确定性已被人类关闭。输出中【严禁】再生成"
        "「深度追问清单」「待确认事项」「追问」等章节——这些不确定性已由人工确认解决，"
        "不要再列出任何需要人工确认的问题，直接输出已闭环的终稿。"
    )
    user_msg = f"【原始拆解结果】\n{original_report}\n\n"
    if replies_text:
        user_msg += f"【人工确认回复】\n{replies_text}\n\n"
    else:
        user_msg += "【人工确认回复】\n（用户未提供额外回复，请基于上下文消除所有待确认标记）\n\n"
    user_msg += "请输出精炼后的完整拆解结果："

    messages = [
        {'role': 'system', 'content': system_msg},
        {'role': 'user', 'content': user_msg},
    ]

    try:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                AIModelService.call_openai_compatible_api(model_config, messages, max_tokens=8192)
            )
        finally:
            loop.close()
        report = ''
        try:
            report = result['choices'][0]['message']['content']
        except (KeyError, TypeError, IndexError):
            report = str(result)
        return Response({'report': report})
    except Exception as e:
        logger.error(f"[拆解精炼] 失败: {e}")
        return Response({'error': f'精炼失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_ERROR)
