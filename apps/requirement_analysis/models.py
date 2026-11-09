from django.db import models
from django.utils import timezone
from apps.users.models import User
from apps.projects.models import Project, ProjectKnowledge
import json
import httpx
import asyncio
from typing import Dict, Any, List, AsyncIterator
import logging
import os
import re
import traceback
import hashlib

logger = logging.getLogger(__name__)


class RequirementDocument(models.Model):
    """需求文档模型"""
    DOCUMENT_TYPE_CHOICES = [
        ('pdf', 'PDF文档'),
        ('docx', 'Word文档'),
        ('txt', '文本文档'),
        ('md', 'Markdown文档'),
    ]
    
    STATUS_CHOICES = [
        ('uploaded', '已上传'),
        ('analyzing', '分析中'),
        ('analyzed', '分析完成'),
        ('failed', '分析失败'),
    ]
    
    title = models.CharField(max_length=200, verbose_name='文档标题')
    file = models.FileField(upload_to='requirement_docs/%Y/%m/', verbose_name='文档文件')
    document_type = models.CharField(max_length=10, choices=DOCUMENT_TYPE_CHOICES, verbose_name='文档类型')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='uploaded', verbose_name='状态')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='uploaded_documents', verbose_name='上传者')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='requirement_documents', verbose_name='关联项目', null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    file_size = models.PositiveIntegerField(verbose_name='文件大小(bytes)', null=True, blank=True)
    extracted_text = models.TextField(verbose_name='提取的文本内容', blank=True)
    
    class Meta:
        db_table = 'requirement_documents'
        verbose_name = '需求文档'
        verbose_name_plural = '需求文档'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class RequirementAnalysis(models.Model):
    """需求分析记录"""
    document = models.OneToOneField(RequirementDocument, on_delete=models.CASCADE, related_name='analysis', verbose_name='关联文档')
    analysis_report = models.TextField(verbose_name='分析报告', blank=True)
    requirements_count = models.PositiveIntegerField(verbose_name='需求数量', default=0)
    analysis_time = models.FloatField(verbose_name='分析耗时(秒)', null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'requirement_analyses'
        verbose_name = '需求分析'
        verbose_name_plural = '需求分析'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.document.title} - 分析报告"


class BusinessRequirement(models.Model):
    """业务需求模型"""
    REQUIREMENT_TYPE_CHOICES = [
        ('functional', '功能需求'),
        ('performance', '性能需求'),
        ('security', '安全需求'),
        ('usability', '可用性需求'),
        ('interface', '接口需求'),
        ('other', '其他需求'),
    ]
    
    REQUIREMENT_LEVEL_CHOICES = [
        ('high', '高'),
        ('medium', '中'),
        ('low', '低'),
    ]
    
    analysis = models.ForeignKey(RequirementAnalysis, on_delete=models.CASCADE, related_name='requirements', verbose_name='关联分析')
    requirement_id = models.CharField(max_length=50, verbose_name='需求编号')
    requirement_name = models.CharField(max_length=200, verbose_name='需求名称')
    requirement_type = models.CharField(max_length=20, choices=REQUIREMENT_TYPE_CHOICES, verbose_name='需求类型')
    parent_requirement = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, verbose_name='父级需求')
    module = models.CharField(max_length=100, verbose_name='所属模块')
    requirement_level = models.CharField(max_length=10, choices=REQUIREMENT_LEVEL_CHOICES, verbose_name='需求级别')
    reviewer = models.CharField(max_length=50, verbose_name='评审人', default='admin')
    estimated_hours = models.PositiveIntegerField(verbose_name='预计工时', default=8)
    description = models.TextField(verbose_name='需求描述')
    acceptance_criteria = models.TextField(verbose_name='验收标准')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'business_requirements'
        verbose_name = '业务需求'
        verbose_name_plural = '业务需求'
        ordering = ['-created_at']
        unique_together = ['analysis', 'requirement_id']
    
    def __str__(self):
        return f"{self.requirement_id} - {self.requirement_name}"


class GeneratedTestCase(models.Model):
    """生成的测试用例模型"""
    PRIORITY_CHOICES = [
        ('P0', '最高优先级'),
        ('P1', '高优先级'),
        ('P2', '中优先级'),
        ('P3', '低优先级'),
    ]
    
    STATUS_CHOICES = [
        ('generated', '已生成'),
        ('reviewing', '评审中'),
        ('reviewed', '已评审'),
        ('approved', '已批准'),
        ('rejected', '已拒绝'),
        ('adopted', '已采纳'),
        ('discarded', '已弃用'),
    ]
    
    requirement = models.ForeignKey(BusinessRequirement, on_delete=models.CASCADE, related_name='test_cases', verbose_name='关联需求')
    case_id = models.CharField(max_length=50, verbose_name='用例编号')
    title = models.CharField(max_length=300, verbose_name='用例标题')
    priority = models.CharField(max_length=5, choices=PRIORITY_CHOICES, verbose_name='优先级')
    precondition = models.TextField(verbose_name='前置条件')
    test_steps = models.TextField(verbose_name='测试步骤')
    expected_result = models.TextField(verbose_name='预期结果')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='generated', verbose_name='状态')
    generated_by_ai = models.CharField(max_length=50, verbose_name='生成AI模型', default='AI-A')
    reviewed_by_ai = models.CharField(max_length=50, verbose_name='评审AI模型', null=True, blank=True)
    review_comments = models.TextField(verbose_name='评审意见', blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'generated_test_cases'
        verbose_name = '生成的测试用例'
        verbose_name_plural = '生成的测试用例'
        ordering = ['-created_at']
        unique_together = ['requirement', 'case_id']
    
    def __str__(self):
        return f"{self.case_id} - {self.title[:50]}"


class AnalysisTask(models.Model):
    """分析任务模型"""
    TASK_TYPE_CHOICES = [
        ('requirement_analysis', '需求分析'),
        ('testcase_generation', '测试用例生成'),
        ('testcase_review', '测试用例评审'),
    ]
    
    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('running', '运行中'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]
    
    task_id = models.CharField(max_length=100, unique=True, verbose_name='任务ID')
    task_type = models.CharField(max_length=30, choices=TASK_TYPE_CHOICES, verbose_name='任务类型')
    document = models.ForeignKey(RequirementDocument, on_delete=models.CASCADE, related_name='tasks', verbose_name='关联文档')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    progress = models.PositiveIntegerField(default=0, verbose_name='进度百分比')
    result = models.JSONField(verbose_name='任务结果', null=True, blank=True)
    error_message = models.TextField(verbose_name='错误信息', blank=True)
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    
    class Meta:
        db_table = 'analysis_tasks'
        verbose_name = '分析任务'
        verbose_name_plural = '分析任务'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.task_id} - {self.get_task_type_display()}"


class AIModelConfig(models.Model):
    """AI模型配置模型"""
    MODEL_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('qwen', '通义千问'),
        ('siliconflow', '硅基流动'),
        ('other', '其他'),
    ]

    ROLE_CHOICES = [
        ('writer', '测试用例编写专家'),
        ('writer_vision', '视觉分析专家（截图/图片/PDF）'),
        ('reviewer', '测试评审专家'),
        ('analyzer', '需求拆解专家'),
        ('browser_use_text', 'Browser Use - 文本模式'),
        ('writer_vision_ocr', '视觉分析专家（OCR专用，可配更快模型）'),
        ('midscene_vision', 'Midscene 视觉驱动模型（APP 真机自动化）'),
        ('embedder', '向量化模型（Embedding）'),
        ('reranker', '重排序模型（Reranker）'),
    ]

    name = models.CharField(max_length=100, verbose_name='配置名称')
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES, verbose_name='模型类型')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='角色')
    api_key = models.CharField(max_length=200, verbose_name='API Key', blank=True, null=True)
    base_url = models.URLField(verbose_name='API Base URL')
    model_name = models.CharField(max_length=100, verbose_name='模型名称')
    max_tokens = models.IntegerField(default=4096, verbose_name='最大Token数')
    temperature = models.FloatField(default=0.7, verbose_name='温度参数')
    top_p = models.FloatField(default=0.9, verbose_name='Top P参数')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ai_model_config'
        verbose_name = 'AI模型配置'
        verbose_name_plural = 'AI模型配置'
        # 移除 unique_together 约束，允许同一个 role 有多个配置
        # 在应用层面通过代码控制：每个 role 只能有一个 is_active=True 的配置

    def __str__(self):
        return f"{self.get_model_type_display()} - {self.get_role_display()}"

    @classmethod
    def get_active_config(cls, model_type: str, role: str):
        """获取活跃的配置"""
        return cls.objects.filter(
            model_type=model_type,
            role=role,
            is_active=True
        ).first()

    @classmethod
    def get_active_embedder(cls):
        """获取任意一个启用的向量化（embedding）配置"""
        return cls.objects.filter(role='embedder', is_active=True).first()


class PromptConfig(models.Model):
    """提示词配置模型"""
    PROMPT_CHOICES = [
        ('writer', '用例编写提示词'),
        ('reviewer', '用例评审提示词'),
        ('analyzer', '需求分析提示词'),
    ]
    
    name = models.CharField(max_length=100, verbose_name='配置名称')
    prompt_type = models.CharField(max_length=20, choices=PROMPT_CHOICES, verbose_name='提示词类型')
    content = models.TextField(verbose_name='提示词内容')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'prompt_config'
        verbose_name = '提示词配置'
        verbose_name_plural = '提示词配置'
    
    def __str__(self):
        return f"{self.get_prompt_type_display()} - {self.name}"
    
    @classmethod
    def get_active_config(cls, prompt_type: str):
        """获取活跃的提示词配置"""
        return cls.objects.filter(
            prompt_type=prompt_type,
            is_active=True
        ).first()


class GenerationConfig(models.Model):
    """生成行为配置模型"""
    OUTPUT_MODE_CHOICES = [
        ('stream', '实时流式输出'),
        ('complete', '完整输出'),
    ]

    name = models.CharField(max_length=100, verbose_name='配置名称', default='默认生成配置')
    default_output_mode = models.CharField(
        max_length=10,
        choices=OUTPUT_MODE_CHOICES,
        default='stream',
        verbose_name='默认输出模式',
        help_text='测试用例生成的默认输出方式'
    )

    # 扩展配置字段
    enable_auto_review = models.BooleanField(
        default=True,
        verbose_name='启用AI评审和改进',
        help_text='生成完成后自动进行AI评审，并根据评审意见改进测试用例'
    )
    review_timeout = models.IntegerField(
        default=120,
        verbose_name='评审和改进超时时间（秒）',
        help_text='AI评审和改进的最大等待时间（总时长）'
    )

    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'generation_config'
        verbose_name = '生成行为配置'
        verbose_name_plural = '生成行为配置'

    def __str__(self):
        return self.name

    @classmethod
    def get_active_config(cls):
        """获取活跃的生成配置"""
        return cls.objects.filter(is_active=True).first()


class TestCaseGenerationTask(models.Model):
    """测试用例生成任务模型"""
    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('generating', '生成中'),
        ('reviewing', '评审中'),
        ('revising', '改进中'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('cancelled', '已取消'),
    ]

    OUTPUT_MODE_CHOICES = [
        ('stream', '实时流式输出'),
        ('complete', '完整输出'),
    ]

    GENERATION_MODE_CHOICES = [
        ('smart', '智能模式'),
        ('quick', '快速模式'),
        ('standard', '标准模式'),
        ('comprehensive', '全面模式'),
    ]

    task_id = models.CharField(max_length=50, unique=True, verbose_name='任务ID')
    title = models.CharField(max_length=200, verbose_name='任务标题')
    requirement_text = models.TextField(verbose_name='需求描述')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    progress = models.IntegerField(default=0, verbose_name='进度百分比')

    # 流式输出配置
    output_mode = models.CharField(
        max_length=10,
        choices=OUTPUT_MODE_CHOICES,
        default='stream',
        verbose_name='输出模式'
    )

    generation_mode = models.CharField(
        max_length=20,
        choices=GENERATION_MODE_CHOICES,
        default='smart',
        verbose_name='生成模式'
    )

    # 流式缓冲区和状态跟踪
    stream_buffer = models.TextField(blank=True, verbose_name='流式输出缓冲区')
    stream_position = models.IntegerField(default=0, verbose_name='流式输出位置')
    last_stream_update = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='最后流式更新时间'
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联项目'
    )
    version = models.ForeignKey(
        'versions.Version',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联版本'
    )
    feature_module = models.ForeignKey(
        'feature_modules.FeatureModule',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联功能模块'
    )
    test_point = models.ForeignKey(
        'feature_modules.TestPoint',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联测试点'
    )
    
    # 配置参数
    writer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True, 
        related_name='writer_tasks', verbose_name='编写模型配置'
    )
    reviewer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审模型配置'
    )
    writer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='writer_tasks', verbose_name='编写提示词配置'
    )
    reviewer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审提示词配置'
    )
    
    # 生成结果
    generated_test_cases = models.TextField(blank=True, verbose_name='生成的测试用例')
    review_feedback = models.TextField(blank=True, verbose_name='评审反馈')
    human_feedback = models.TextField(blank=True, verbose_name='人工确认回复')  # 评审后人工对不确定需求的回复/确认
    final_test_cases = models.TextField(blank=True, verbose_name='最终测试用例')
    kb_audit_result = models.TextField(blank=True, verbose_name='知识库审计结果')
    confirmed_answers = models.TextField(
        blank=True, verbose_name='需求拆解阶段确认的问答对',
        help_text='JSON数组: [{question, answer}]，来自精炼时用户确认的不确定项回答，用于生成用例时的上下文注入'
    )
    # 历史缺陷复盘注入（2026-10-22，方案 A：任务级显式开关）
    # 只有用户在本次任务显式选择了复盘报告时，才把该报告提炼的测试点注入 writer prompt。
    # 默认 NULL = 不注入，保证既有任务行为完全不变，不会"一次复盘污染以后所有生成"。
    defect_review = models.ForeignKey(
        'zentao.DefectReviewReport',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联历史缺陷复盘',
        help_text='选中后按本次需求二次筛选该报告的测试点并注入生成，不选则不注入',
    )
    defect_review_detail_level = models.CharField(
        max_length=20, blank=True, default='',
        verbose_name='测试点注入详略',
        help_text='brief/standard/full；留空则按改动规模自动决定',
    )
    # 注入审计（2026-10-23 用户提出：生成时要知道「到底用了哪几条」）
    # 生成侧此前只在日志里记「注入了 N 条、多少字符」，重启即丢且用户看不到。
    # 这里把**实际进入 prompt 的测试点快照**落到任务上，供任务详情页与报告页反查。
    # 结构：{injected, reason, matched_domains, items:[{domain,scene,title,count}],
    #        used_count, candidate_count, detail_level, prompt_chars, at}
    defect_review_used = models.JSONField(
        null=True, blank=True,
        verbose_name='历史测试点注入审计',
        help_text='本次实际注入的历史测试点快照；NULL 表示未关联复盘或尚未执行',
    )
    review_count = models.IntegerField(default=0, verbose_name='评审次数')  # 0=未评审, 1=首次, 2+=重新评审
    review_updated_at = models.DateTimeField(null=True, blank=True, verbose_name='最后评审时间')

    # 元数据
    generation_log = models.TextField(blank=True, verbose_name='生成日志')
    error_message = models.TextField(blank=True, verbose_name='错误信息')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    is_saved_to_records = models.BooleanField(default=False, verbose_name='是否已保存到记录')
    saved_at = models.DateTimeField(null=True, blank=True, verbose_name='保存到记录时间')
    
    class Meta:
        db_table = 'testcase_generation_task'
        verbose_name = '测试用例生成任务'
        verbose_name_plural = '测试用例生成任务'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class GeneratedRequirementDoc(models.Model):
    """AI 生成的需求文档记录（历史记录）"""
    SOURCE_CHOICES = [
        ('text', '粘贴文字'),
        ('file', '上传文件'),
        ('template', '空白模板'),
    ]

    title = models.CharField(max_length=300, verbose_name='文档标题')
    markdown_content = models.TextField(verbose_name='Markdown 内容')
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, verbose_name='来源类型')
    source_detail = models.TextField(blank=True, verbose_name='来源详情', help_text='文件名列表或粘贴文字摘要')
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='generated_req_docs', verbose_name='关联项目'
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'generated_requirement_docs'
        verbose_name = '生成的需求文档'
        verbose_name_plural = '生成的需求文档'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class RequirementAnalysisResult(models.Model):
    """需求拆解结果历史记录"""
    title = models.CharField(max_length=300, verbose_name='结果标题')
    requirement_text = models.TextField(verbose_name='拆解的需求文本', blank=True)
    result_content = models.TextField(verbose_name='拆解结果（Markdown）')
    content_preview = models.TextField(verbose_name='内容预览', blank=True)
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='requirement_analysis_results', verbose_name='关联项目'
    )
    req_doc = models.ForeignKey(
        'GeneratedRequirementDoc', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='analysis_results', verbose_name='关联需求文档'
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'requirement_analysis_results'
        verbose_name = '需求拆解结果'
        verbose_name_plural = '需求拆解结果'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class ClarificationQuestion(models.Model):
    """需求拆解「深度追问清单」条目（关联一条拆解结果历史记录）"""
    STATUS_CHOICES = [
        ('pending', '待回复'),
        ('answered', '已回复'),
        ('confirmed', '已确认'),
    ]

    analysis_result = models.ForeignKey(
        RequirementAnalysisResult, on_delete=models.CASCADE,
        related_name='clarifications', verbose_name='关联拆解结果'
    )
    question = models.TextField(verbose_name='追问问题')
    answer = models.TextField(verbose_name='人工回复', blank=True)
    category = models.CharField(max_length=50, verbose_name='分类', default='其他')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态'
    )
    order = models.IntegerField(default=0, verbose_name='排序', db_column='order_idx')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'clarification_questions'
        verbose_name = '深度追问'
        verbose_name_plural = '深度追问'
        ordering = ['order', 'id']

    def __str__(self):
        return f"追问#{self.order}: {self.question[:30]}"


# AIModelService 已迁移至 ai_model_service.py（解耦：models.py 只保留数据模型）
from apps.requirement_analysis.ai_model_service import AIModelService  # re-export

class ModaoPrototype(models.Model):
    """墨刀技能 - 需求来源（墨刀原型 / 需求文本），承载 5 阶段工作流状态与产物。"""
    SOURCE_CHOICES = [
        ('modao', '墨刀原型'),
        ('webshare', 'webshare 页面树'),
        ('text', '需求文本/文档'),
    ]
    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('extracting', '读取中'),
        ('extracted', '已读取'),
        ('structuring', '结构化中'),
        ('clarifying', '澄清中'),
        ('designing', '用例设计中'),
        ('done', '完成'),
        ('failed', '失败'),
    ]

    uuid = models.CharField(max_length=32, unique=True, db_index=True, verbose_name='UUID')
    title = models.CharField(max_length=200, verbose_name='标题', default='墨刀需求梳理')
    url = models.TextField(verbose_name='原型链接', blank=True)
    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='modao', verbose_name='来源类型')
    auth_cookie = models.TextField(blank=True, verbose_name='登录Cookie(JSON)')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    current_stage = models.IntegerField(default=0, verbose_name='当前阶段(0-4)')
    # 各阶段产物（文本/JSON 字符串）
    extracted_json = models.TextField(blank=True, verbose_name='阶段0-逐页提取')
    requirement_summary = models.TextField(blank=True, verbose_name='阶段1-需求摘要')
    clarification_log = models.TextField(blank=True, verbose_name='阶段2-澄清记录')
    module_split = models.TextField(blank=True, verbose_name='阶段3.1-模块拆分')
    risks_json = models.TextField(blank=True, verbose_name='阶段3-风险')
    pci_json = models.TextField(blank=True, verbose_name='阶段3-PCI')
    final_testpoints_json = models.TextField(blank=True, verbose_name='阶段3-合并测试点')
    dedup_audit_json = models.TextField(default='[]', blank=True, verbose_name='阶段3-去重审计记录')
    testcases_json = models.TextField(blank=True, verbose_name='阶段3-用例')
    smoke_json = models.TextField(blank=True, verbose_name='阶段3-冒烟')
    quality_report_json = models.TextField(blank=True, verbose_name='阶段3-质量报告')
    excel_path = models.CharField(max_length=500, blank=True, verbose_name='Excel路径')
    merged_testcases_json = models.TextField(blank=True, verbose_name='汇总-已采纳合并用例')
    merged_smoke_json = models.TextField(blank=True, verbose_name='汇总-已采纳合并冒烟')
    # 自愈闭环(P0)：AI 原始生成的用例快照，于 P3 生成完成后一次性写入，不随人工编辑/采纳变化。
    # 供「AI 生成 vs 最终采纳」偏差对比（冗余/疏漏/改写分析）使用，与 merged_testcases_json 配对。
    ai_testcases_json = models.TextField(blank=True, verbose_name='AI原始-用例快照(自愈用)')
    stage_confirmations = models.TextField(default='{}', verbose_name='阶段确认JSON')
    stage4_decision = models.CharField(max_length=20, blank=True, verbose_name='阶段4决策')
    error_message = models.TextField(blank=True, verbose_name='错误信息')
    work_log = models.TextField(blank=True, verbose_name='工作日志(各阶段进度)')
    qa_log = models.TextField(default='[]', verbose_name='人工答疑记录(JSON数组)')
    feature_module = models.ForeignKey('feature_modules.FeatureModule', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='modao_prototypes',
                                       verbose_name='同步的顶层功能模块')
    version = models.ForeignKey('versions.Version', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='modao_prototypes', verbose_name='关联版本')
    project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='关联项目')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='modao_prototypes', verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'modao_prototype'
        verbose_name = '墨刀原型'
        verbose_name_plural = '墨刀原型'
        ordering = ['-created_at']

    def __str__(self):
        return f'Modao#{self.uuid}'

    def get_confirmations(self):
        try:
            return json.loads(self.stage_confirmations or '{}')
        except Exception:
            return {}

    def set_confirmation(self, stage: int, confirmed: bool = True):
        conf = self.get_confirmations()
        conf[f'stage{stage}_confirmed'] = confirmed
        self.stage_confirmations = json.dumps(conf, ensure_ascii=False)

    def clarification_items(self):
        """Return structured clarification items; legacy free text has no items."""
        raw = (self.clarification_log or '').strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except Exception:
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def unresolved_clarifications(self):
        """Items requiring a human conclusion before case generation."""
        unresolved = []
        for index, item in enumerate(self.clarification_items()):
            resolution = str(item.get('resolution') or item.get('answer') or '').strip()
            if not resolution:
                unresolved.append({
                    'index': index,
                    'type': item.get('type') or '待确认',
                    'location': item.get('location') or '',
                    'issue': item.get('issue') or item.get('suggested_question') or '',
                })
        return unresolved

    def case_generation_fingerprint(self):
        """Bind human approval to the exact clarification and test-point inputs."""
        content = '\n---CLARIFICATIONS---\n'.join([
            self.clarification_log or '', self.final_testpoints_json or ''
        ])
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def case_generation_gate(self):
        conf = self.get_confirmations()
        unresolved = self.unresolved_clarifications()
        current_fingerprint = self.case_generation_fingerprint()
        approved = bool(conf.get('case_generation_approved'))
        fingerprint_matches = (
            approved and conf.get('case_generation_fingerprint') == current_fingerprint
        )
        stage2_confirmed = bool(conf.get('stage2_confirmed'))
        reasons = []
        if unresolved:
            reasons.append(f'仍有 {len(unresolved)} 条位置/需求问题未填写人工结论')
        if not stage2_confirmed:
            reasons.append('阶段2尚未人工确认')
        if not approved:
            reasons.append('尚未批准生成最终测试用例')
        elif not fingerprint_matches:
            reasons.append('澄清结论或测试点已修改，原生成批准已失效')
        return {
            'allowed': not unresolved and stage2_confirmed and fingerprint_matches,
            'unresolved_count': len(unresolved),
            'unresolved_items': unresolved,
            'stage2_confirmed': stage2_confirmed,
            'approved': approved,
            'fingerprint_matches': fingerprint_matches,
            'reasons': reasons,
        }

class ModaoPage(models.Model):
    """墨刀技能 - 原型单页提取结果。"""
    prototype = models.ForeignKey(ModaoPrototype, on_delete=models.CASCADE, related_name='pages')
    page_no = models.IntegerField(verbose_name='页码')
    page_name = models.CharField(max_length=200, blank=True, verbose_name='页面名')
    text = models.TextField(blank=True, verbose_name='可见文本')
    annotations = models.TextField(default='[]', verbose_name='注释/标注(JSON)')
    screenshot = models.CharField(max_length=500, blank=True, verbose_name='截图路径')
    # 超长/超宽画布分段截图后的全部分段路径列表（JSON 数组，相对 MEDIA_ROOT；含主图即 screenshot 本身）
    screenshot_segments = models.TextField(default='[]', blank=True, verbose_name='分段截图路径(JSON)')
    selected = models.BooleanField(default=True, verbose_name='纳入后续流程')
    recognition_note = models.CharField(max_length=200, blank=True, verbose_name='识别准确率标注')
    # 父级分组标题（如「前置付费页」）。墨刀只读分享页 DOM 不渲染分组层级，
    # 自动化提取无法可靠获取，故默认留空，由用户在校验视图手动填写/校正。
    # 展示用的完整层级名 = (parent_name + '-' + page_name) if parent_name else page_name。
    parent_name = models.CharField(max_length=200, blank=True, verbose_name='父级分组')
    # 完整文件夹路径（如「首页板块/首页」或「功能模块/用户中心/登录页」），由墨刀页面树自动提取。
    # 用于前端按文件夹分组展示与整组选择；未分组时为空字符串。
    folder = models.CharField(max_length=500, blank=True, verbose_name='文件夹路径')


    # ---------- 画布级独立流水线状态（A2-b：进度/回退/重跑，产物仍任务级聚合）----------
    PAGE_STAGE_CHOICES = [
        (0, '待读取'),
        (1, '已结构化'),
        (2, '已澄清'),
        (3, '已生成测试点/用例'),
        (4, '已冒烟决策'),
    ]
    PAGE_STATUS_CHOICES = [
        ('pending', '待处理'),
        ('extracting', '读取中'),
        ('done', '完成'),
        ('failed', '失败'),
        ('needs_rerun', '需重跑'),
    ]
    current_stage = models.IntegerField(default=0, verbose_name='画布当前阶段(0-4)')
    stage_status = models.CharField(max_length=20, choices=PAGE_STATUS_CHOICES, default='pending', verbose_name='画布子状态')
    work_log = models.TextField(blank=True, verbose_name='画布级工作日志')
    error_message = models.TextField(blank=True, verbose_name='画布错误信息')
    # 回退标记：JSON 数组，记录该画布需要重跑的阶段序号（如 [1,2] 表示需重跑结构化与澄清）
    rerun_stages = models.TextField(default='[]', blank=True, verbose_name='需重跑阶段标记(JSON)')

    # ---------- 画布级独立流水线产物（A2-c：按选中子集分别走流程，产物按画布存）----------
    page_summary = models.TextField(blank=True, verbose_name='画布级-阶段1摘要')
    page_clarification = models.TextField(blank=True, verbose_name='画布级-阶段2澄清')
    page_testpoints_json = models.TextField(blank=True, verbose_name='画布级-阶段3测试点')
    page_testcases_json = models.TextField(blank=True, verbose_name='画布级-阶段3用例')
    page_smoke_json = models.TextField(blank=True, verbose_name='画布级-阶段3冒烟')
    page_approved = models.BooleanField(default=False, verbose_name='画布用例是否已采纳')
    # 自愈闭环(P0)：本画布 AI 原始用例快照，run_design_phase2 写入后不再变化，供任务级聚合与对比。
    page_ai_testcases_json = models.TextField(blank=True, verbose_name='画布级-AI原始用例快照(自愈用)')

    class Meta:
        db_table = 'modao_page'
        verbose_name = '墨刀页面'
        verbose_name_plural = '墨刀页面'
        ordering = ['page_no']

    def __str__(self):
        return f'P{self.page_no}-{self.page_name}'

    def get_rerun_stages(self):
        try:
            v = json.loads(self.rerun_stages or '[]')
            return v if isinstance(v, list) else []
        except Exception:
            return []

    def set_rerun_stages(self, stages):
        self.rerun_stages = json.dumps(sorted(set(int(s) for s in stages)), ensure_ascii=False)

    def mark_stage(self, stage, status='done', log=None, error=None):
        """统一推进画布级进度。"""
        self.current_stage = max(self.current_stage, stage)
        self.stage_status = status
        if log:
            ts = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            self.work_log = (self.work_log or '') + f'[{ts}] {log}\n'
        if error is not None:
            self.error_message = error
        elif status == 'done':
            self.error_message = ''



