from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
import hashlib
import json

User = get_user_model()


class UiProject(models.Model):
    """UI自动化测试项目模型"""
    STATUS_CHOICES = [
        ('NOT_STARTED', '未开始'),
        ('IN_PROGRESS', '进行中'),
        ('COMPLETED', '已结束'),
    ]

    name = models.CharField(max_length=200, verbose_name='项目名称')
    description = models.TextField(blank=True, verbose_name='项目描述')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, verbose_name='项目状态', default='IN_PROGRESS')
    base_url = models.URLField(verbose_name='基础URL')
    start_date = models.DateField(null=True, blank=True, verbose_name='开始日期')
    end_date = models.DateField(null=True, blank=True, verbose_name='结束日期')
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_ui_projects', verbose_name='负责人')
    members = models.ManyToManyField(User, blank=True, related_name='ui_projects', verbose_name='团队成员')
    # ---- 全局弹窗策略（项目级，2026-10-29）----
    # 为什么放项目级而不是每条用例各配一遍：权限框/隐私协议/开屏广告几乎每个 App
    # 冷启动都会弹，逐条用例声明既啰嗦又必然漏配；项目级配一次长期有效。
    # 用例级（AICase.auto_dismiss_popup）仍然可以覆盖，优先级更高。
    auto_dismiss_popup = models.BooleanField(
        default=True, verbose_name='自动处理弹窗',
        help_text='执行时遇到权限请求/协议/广告等弹窗自动关闭并重试该步骤'
    )
    popup_precheck = models.BooleanField(
        default=True, verbose_name='执行前预检弹窗',
        help_text='启动 App 后先清一轮弹窗再开始执行，不等某一步失败才补救'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_projects'
        verbose_name = 'UI自动化项目'
        verbose_name_plural = 'UI自动化项目'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class LocatorStrategy(models.Model):
    """元素定位策略模型"""
    name = models.CharField(max_length=50, verbose_name='策略名称')
    description = models.TextField(blank=True, verbose_name='策略描述')

    class Meta:
        db_table = 'locator_strategies'
        verbose_name = '定位策略'
        verbose_name_plural = '定位策略'

    def __str__(self):
        return self.name


class ElementGroup(models.Model):
    """元素分组模型"""
    name = models.CharField(max_length=200, verbose_name='分组名称')
    description = models.TextField(blank=True, verbose_name='分组描述')
    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='element_groups', verbose_name='所属项目')
    parent_group = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, verbose_name='父分组')
    order = models.IntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_element_groups'
        verbose_name = '元素分组'
        verbose_name_plural = '元素分组'
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class Element(models.Model):
    """UI元素模型"""
    ELEMENT_TYPE_CHOICES = [
        ('INPUT', '输入框'),
        ('BUTTON', '按钮'),
        ('LINK', '链接'),
        ('DROPDOWN', '下拉框'),
        ('CHECKBOX', '复选框'),
        ('RADIO', '单选框'),
        ('TEXT', '文本'),
        ('IMAGE', '图片'),
        ('CONTAINER', '容器'),
        ('TABLE', '表格'),
        ('FORM', '表单'),
        ('MODAL', '弹窗'),
    ]

    VALIDATION_STATUS_CHOICES = [
        ('VALID', '有效'),
        ('INVALID', '无效'),
        ('UNKNOWN', '未知'),
        ('PENDING', '待验证'),
    ]

    #: 元素适用的平台。
    #: 同一个业务元素在不同平台上的 locator 完全不同（Android 是 resource-id/xpath，
    #: iOS 是 accessibility id/name，鸿蒙是 component id），所以必须按平台分开存。
    #: 留空 = 通用（历史数据/Web 元素），查询时"不限定平台的元素"对所有平台可见。
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
        ('harmony', 'HarmonyOS'),
        ('web', 'Web'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='elements', verbose_name='所属项目')
    group = models.ForeignKey(ElementGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='elements', verbose_name='所属分组')
    name = models.CharField(max_length=200, verbose_name='元素名称')
    description = models.TextField(blank=True, verbose_name='元素描述')
    element_type = models.CharField(max_length=50, choices=ELEMENT_TYPE_CHOICES, verbose_name='元素类型', default='BUTTON')
    platform = models.CharField(
        max_length=20, choices=PLATFORM_CHOICES, blank=True, default='',
        verbose_name='适用平台',
        help_text='留空表示通用；同一元素在不同平台的 locator 不同，需分平台保存'
    )

    # 主要定位策略
    locator_strategy = models.ForeignKey(LocatorStrategy, on_delete=models.PROTECT, verbose_name='定位策略')
    locator_value = models.CharField(max_length=500, verbose_name='定位表达式')

    # 备用定位策略
    backup_locators = models.JSONField(
        blank=True,
        null=True,
        verbose_name='备用定位器',
        help_text='多个定位策略的JSON数组，格式：[{"strategy": "css", "value": ".button"}, ...]'
    )

    page = models.CharField(max_length=200, verbose_name='所属页面', blank=True)
    component_name = models.CharField(max_length=100, blank=True, verbose_name='组件名称', help_text='所属UI组件名称')

    # 元素层次关系
    parent_element = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='父元素', help_text='用于构建元素层次结构')

    # 元素属性
    is_unique = models.BooleanField(default=False, verbose_name='是否唯一')
    wait_timeout = models.IntegerField(default=5, verbose_name='等待超时(秒)')
    is_visible = models.BooleanField(default=True, verbose_name='是否可见')
    is_enabled = models.BooleanField(default=True, verbose_name='是否启用')
    force_action = models.BooleanField(default=False, verbose_name='强制操作', help_text='对visibility:hidden的元素使用force选项')

    # 统计信息
    usage_count = models.IntegerField(default=0, verbose_name='使用次数', help_text='在脚本中被引用的次数')
    last_validated = models.DateTimeField(null=True, blank=True, verbose_name='最后验证时间')
    validation_status = models.CharField(max_length=20, choices=VALIDATION_STATUS_CHOICES, default='UNKNOWN', verbose_name='验证状态')
    validation_message = models.TextField(blank=True, verbose_name='验证消息')

    # 基础字段
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='创建人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_elements'
        verbose_name = 'UI元素'
        verbose_name_plural = 'UI元素'
        ordering = ['page', 'name']
        indexes = [
            models.Index(fields=['project', 'page']),
            models.Index(fields=['project', 'element_type']),
            models.Index(fields=['validation_status']),
            # 元素拾取/回写按「项目 + 平台」查询，走索引避免全表扫
            models.Index(fields=['project', 'platform']),
        ]

    def __str__(self):
        return f'{self.page}: {self.name}' if self.page else self.name

    def increment_usage_count(self):
        """增加使用次数"""
        self.usage_count = models.F('usage_count') + 1
        self.save(update_fields=['usage_count'])

    def get_all_locators(self):
        """获取所有定位器（主要+备用）"""
        locators = [{
            'strategy': self.locator_strategy.name,
            'value': self.locator_value,
            'is_primary': True
        }]

        if self.backup_locators:
            for backup in self.backup_locators:
                locators.append({
                    'strategy': backup.get('strategy'),
                    'value': backup.get('value'),
                    'is_primary': False
                })

        return locators


class TestScript(models.Model):
    """测试脚本模型"""
    SCRIPT_TYPE_CHOICES = [
        ('CODE', '代码'),
        ('LOW_CODE', '低代码'),
        ('NO_CODE', '无代码'),
    ]

    LANGUAGE_CHOICES = [
        ('python', 'Python'),
        ('javascript', 'JavaScript'),
    ]

    FRAMEWORK_CHOICES = [
        ('playwright', 'Playwright'),
        ('selenium', 'Selenium'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='test_scripts', verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='脚本名称')
    description = models.TextField(blank=True, verbose_name='脚本描述')
    script_type = models.CharField(max_length=20, choices=SCRIPT_TYPE_CHOICES, verbose_name='脚本类型', default='LOW_CODE')
    content = models.TextField(verbose_name='脚本内容')  # 可以是代码或JSON格式的低代码配置
    language = models.CharField(max_length=20, choices=LANGUAGE_CHOICES, verbose_name='脚本语言', default='python', blank=True)
    framework = models.CharField(max_length=20, choices=FRAMEWORK_CHOICES, verbose_name='执行框架', default='playwright', blank=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_test_scripts'
        verbose_name = 'UI测试脚本'
        verbose_name_plural = 'UI测试脚本'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class PageObject(models.Model):
    """页面对象模型"""
    name = models.CharField(max_length=200, verbose_name='页面对象名称')
    class_name = models.CharField(max_length=200, verbose_name='类名')
    url_pattern = models.CharField(max_length=500, blank=True, verbose_name='URL模式', help_text='页面URL模式，支持正则表达式')
    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='page_objects', verbose_name='所属项目')
    description = models.TextField(blank=True, verbose_name='描述')
    template_code = models.TextField(blank=True, verbose_name='模板代码', help_text='生成的页面对象代码模板')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='创建人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_page_objects'
        verbose_name = '页面对象'
        verbose_name_plural = '页面对象'
        ordering = ['name']
        unique_together = ['project', 'name']

    def __str__(self):
        return self.name

    def generate_code(self, language='javascript'):
        """生成页面对象代码"""
        if language == 'javascript':
            return self._generate_javascript_code()
        elif language == 'python':
            return self._generate_python_code()
        else:
            raise ValueError(f"Unsupported language: {language}")

    def _generate_javascript_code(self):
        """生成JavaScript页面对象代码"""
        elements = self.page_object_elements.all()

        # 生成元素属性
        element_props = []
        methods = []

        for po_element in elements:
            element = po_element.element
            if po_element.is_property:
                element_props.append(
                    f"        this.{po_element.method_name} = page.locator('{element.locator_value}');"
                )
            else:
                methods.append(f"""
    async {po_element.method_name}() {{
        return this.page.locator('{element.locator_value}');
    }}""")

        template = f"""
class {self.class_name} {{
    constructor(page) {{
        this.page = page;
{chr(10).join(element_props)}
    }}
{chr(10).join(methods)}
}}
        """.strip()

        return template

    def _generate_python_code(self):
        """生成Python页面对象代码"""
        elements = self.page_object_elements.all()

        methods = []
        for po_element in elements:
            element = po_element.element
            methods.append(f"""
    def {po_element.method_name}(self):
        return self.page.locator('{element.locator_value}')""")

        template = f"""
class {self.class_name}:
    def __init__(self, page):
        self.page = page
{chr(10).join(methods)}
        """.strip()

        return template


class PageObjectElement(models.Model):
    """页面对象与元素的关联"""
    page_object = models.ForeignKey(PageObject, on_delete=models.CASCADE, related_name='page_object_elements', verbose_name='页面对象')
    element = models.ForeignKey(Element, on_delete=models.CASCADE, verbose_name='元素')
    method_name = models.CharField(max_length=100, verbose_name='方法名称', help_text='在页面对象中的方法/属性名称')
    is_property = models.BooleanField(default=True, verbose_name='是否为属性', help_text='True为属性，False为方法')
    order = models.IntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ui_page_object_elements'
        verbose_name = '页面对象元素关联'
        verbose_name_plural = '页面对象元素关联'
        unique_together = ['page_object', 'method_name']
        ordering = ['order', 'method_name']

    def __str__(self):
        return f'{self.page_object.name}.{self.method_name}'


class ScriptStep(models.Model):
    """脚本步骤模型"""
    ACTION_TYPE_CHOICES = [
        ('CLICK', '点击'),
        ('INPUT', '输入'),
        ('SELECT', '选择'),
        ('VERIFY', '验证'),
        ('WAIT', '等待'),
        ('HOVER', '悬停'),
        ('SCROLL', '滚动'),
        ('NAVIGATE', '导航'),
        ('SCREENSHOT', '截图'),
        ('SWITCH_TAB', '切换标签页'),
        ('CUSTOM', '自定义'),
    ]

    script = models.ForeignKey(TestScript, on_delete=models.CASCADE, related_name='steps', verbose_name='所属脚本')
    step_order = models.IntegerField(verbose_name='步骤顺序')
    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES, verbose_name='操作类型')
    target_element = models.ForeignKey(Element, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='目标元素')
    page_object = models.ForeignKey(PageObject, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='页面对象')

    # 操作参数
    action_params = models.JSONField(blank=True, null=True, verbose_name='操作参数', help_text='JSON格式的操作参数')

    # 步骤描述
    description = models.CharField(max_length=500, verbose_name='步骤描述')
    expected_result = models.CharField(max_length=500, blank=True, verbose_name='预期结果')

    # 执行配置
    wait_before = models.IntegerField(default=0, verbose_name='执行前等待(毫秒)')
    wait_after = models.IntegerField(default=0, verbose_name='执行后等待(毫秒)')
    retry_count = models.IntegerField(default=0, verbose_name='重试次数')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_script_steps'
        verbose_name = '脚本步骤'
        verbose_name_plural = '脚本步骤'
        ordering = ['step_order']
        unique_together = ['script', 'step_order']

    def __str__(self):
        return f'{self.script.name} - Step {self.step_order}: {self.action_type}'


class ScriptElementUsage(models.Model):
    """脚本元素使用记录"""
    USAGE_TYPE_CHOICES = [
        ('CLICK', '点击'),
        ('INPUT', '输入'),
        ('VERIFY', '验证'),
        ('WAIT', '等待'),
        ('HOVER', '悬停'),
        ('SELECT', '选择'),
        ('SCROLL', '滚动'),
        ('ATTRIBUTE', '属性获取'),
        ('TEXT', '文本获取'),
    ]

    script = models.ForeignKey(TestScript, on_delete=models.CASCADE, related_name='element_usages', verbose_name='脚本')
    element = models.ForeignKey(Element, on_delete=models.CASCADE, related_name='script_usages', verbose_name='元素')
    usage_type = models.CharField(max_length=50, choices=USAGE_TYPE_CHOICES, verbose_name='使用类型')
    line_number = models.IntegerField(verbose_name='行号', help_text='在脚本中的行号')
    context = models.TextField(blank=True, verbose_name='上下文代码', help_text='使用元素的代码上下文')
    frequency = models.IntegerField(default=1, verbose_name='使用频次', help_text='在脚本中使用的次数')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_script_element_usages'
        verbose_name = '脚本元素使用记录'
        verbose_name_plural = '脚本元素使用记录'
        unique_together = ['script', 'element', 'line_number']
        ordering = ['script', 'line_number']

    def __str__(self):
        return f'{self.script.name} uses {self.element.name} ({self.usage_type})'


class TestSuite(models.Model):
    """测试套件模型"""
    EXECUTION_STATUS_CHOICES = [
        ('not_run', '未执行'),
        ('passed', '通过'),
        ('failed', '失败'),
        ('running', '执行中'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='test_suites', verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='套件名称')
    description = models.TextField(blank=True, verbose_name='套件描述')
    scripts = models.ManyToManyField(TestScript, through='TestSuiteScript', verbose_name='测试脚本')
    test_cases = models.ManyToManyField('TestCase', through='TestSuiteTestCase', verbose_name='测试用例', blank=True)

    # 执行统计字段
    execution_status = models.CharField(max_length=20, choices=EXECUTION_STATUS_CHOICES, default='not_run', verbose_name='执行状态')
    passed_count = models.IntegerField(default=0, verbose_name='通过数')
    failed_count = models.IntegerField(default=0, verbose_name='失败数')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_test_suites'
        verbose_name = 'UI测试套件'
        verbose_name_plural = 'UI测试套件'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class TestSuiteScript(models.Model):
    """测试套件与测试脚本的关联模型"""
    test_suite = models.ForeignKey(TestSuite, on_delete=models.CASCADE, related_name='suite_scripts', verbose_name='测试套件')
    test_script = models.ForeignKey(TestScript, on_delete=models.CASCADE, verbose_name='测试脚本')
    order = models.IntegerField(default=0, verbose_name='执行顺序')

    class Meta:
        db_table = 'ui_test_suite_scripts'
        verbose_name = '测试套件脚本关联'
        verbose_name_plural = '测试套件脚本关联'
        ordering = ['order']


class TestSuiteTestCase(models.Model):
    """测试套件与测试用例的关联模型"""
    test_suite = models.ForeignKey(TestSuite, on_delete=models.CASCADE, related_name='suite_test_cases', verbose_name='测试套件')
    test_case = models.ForeignKey('TestCase', on_delete=models.CASCADE, verbose_name='测试用例')
    order = models.IntegerField(default=0, verbose_name='执行顺序')

    class Meta:
        db_table = 'ui_test_suite_test_cases'
        verbose_name = '测试套件用例关联'
        verbose_name_plural = '测试套件用例关联'
        ordering = ['order']
        unique_together = ['test_suite', 'test_case']

    def __str__(self):
        return f'{self.test_suite.name} - {self.test_case.name}'


class TestExecution(models.Model):
    """测试执行记录模型"""
    STATUS_CHOICES = [
        ('PENDING', '待执行'),
        ('RUNNING', '运行中'),
        ('SUCCESS', '成功'),
        ('FAILED', '失败'),
        ('ABORTED', '中止'),
    ]

    ENVIRONMENT_CHOICES = [
        ('CHROME', 'Chrome'),
        ('FIREFOX', 'Firefox'),
        ('SAFARI', 'Safari'),
        ('EDGE', 'Edge'),
        ('IE', 'IE'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='executions', verbose_name='所属项目')
    test_suite = models.ForeignKey(TestSuite, on_delete=models.CASCADE, related_name='executions', null=True, blank=True, verbose_name='测试套件')
    test_script = models.ForeignKey(TestScript, on_delete=models.CASCADE, related_name='executions', null=True, blank=True, verbose_name='测试脚本')
    environment = models.CharField(max_length=20, choices=ENVIRONMENT_CHOICES, verbose_name='执行环境', default='CHROME')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, verbose_name='执行状态', default='PENDING')

    # 执行统计
    total_cases = models.IntegerField(default=0, verbose_name='总用例数')
    passed_cases = models.IntegerField(default=0, verbose_name='通过用例数')
    failed_cases = models.IntegerField(default=0, verbose_name='失败用例数')
    skipped_cases = models.IntegerField(default=0, verbose_name='跳过用例数')

    # 时间信息
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    duration = models.FloatField(default=0, verbose_name='执行时长(秒)')

    # 执行人员和配置
    executed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ui_test_executions', verbose_name='执行人员')
    engine = models.CharField(max_length=20, default='playwright', verbose_name='测试引擎')
    browser = models.CharField(max_length=20, default='chrome', verbose_name='浏览器')
    headless = models.BooleanField(default=False, verbose_name='无头模式')

    # 结果数据
    result_data = models.JSONField(blank=True, null=True, verbose_name='执行结果数据')
    error_message = models.TextField(blank=True, verbose_name='错误信息')
    report_url = models.CharField(max_length=500, blank=True, verbose_name='报告URL')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ui_test_executions'
        verbose_name = 'UI测试执行记录'
        verbose_name_plural = 'UI测试执行记录'
        ordering = ['-created_at']

    def __str__(self):
        if self.test_suite:
            return f'Suite: {self.test_suite.name} - {self.get_status_display()}'
        elif self.test_script:
            return f'Script: {self.test_script.name} - {self.get_status_display()}'
        return f'Execution #{self.id}'

    @property
    def pass_rate(self):
        """计算通过率"""
        if self.total_cases == 0:
            return 0
        return round((self.passed_cases / self.total_cases) * 100, 2)


class TestEnvironment(models.Model):
    """测试环境配置模型"""
    name = models.CharField(max_length=100, verbose_name='环境名称')
    description = models.TextField(blank=True, verbose_name='环境描述')
    browser_type = models.CharField(max_length=50, verbose_name='浏览器类型')
    browser_version = models.CharField(max_length=50, blank=True, verbose_name='浏览器版本')
    resolution = models.CharField(max_length=50, blank=True, verbose_name='屏幕分辨率')
    os_type = models.CharField(max_length=50, blank=True, verbose_name='操作系统')
    os_version = models.CharField(max_length=50, blank=True, verbose_name='操作系统版本')
    capabilities = models.JSONField(blank=True, null=True, verbose_name='浏览器能力配置')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_test_environments'
        verbose_name = 'UI测试环境'
        verbose_name_plural = 'UI测试环境'
        ordering = ['name']

    def __str__(self):
        return self.name


class Screenshot(models.Model):
    """截图模型"""
    execution = models.ForeignKey(TestExecution, on_delete=models.CASCADE, related_name='screenshots', verbose_name='测试执行')
    name = models.CharField(max_length=200, verbose_name='截图名称')
    image = models.ImageField(upload_to='ui_screenshots/', verbose_name='截图文件')
    description = models.TextField(blank=True, verbose_name='截图描述')
    captured_at = models.DateTimeField(auto_now_add=True, verbose_name='捕获时间')

    class Meta:
        db_table = 'ui_screenshots'
        verbose_name = 'UI截图'
        verbose_name_plural = 'UI截图'
        ordering = ['-captured_at']

    def __str__(self):
        return self.name


class TestCase(models.Model):
    """UI自动化测试用例模型"""
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('ready', '就绪'),
        ('running', '执行中'),
        ('passed', '通过'),
        ('failed', '失败'),
    ]

    PRIORITY_CHOICES = [
        ('high', '高'),
        ('medium', '中'),
        ('low', '低'),
    ]

    name = models.CharField(max_length=200, verbose_name='用例名称')
    description = models.TextField(blank=True, verbose_name='用例描述')
    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='test_cases', verbose_name='所属项目')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name='状态')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium', verbose_name='优先级')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_test_cases', verbose_name='创建人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    tags = models.JSONField(null=True, blank=True, verbose_name='标签')

    class Meta:
        db_table = 'ui_test_cases'
        verbose_name = 'UI测试用例'
        verbose_name_plural = 'UI测试用例'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class TestCaseStep(models.Model):
    """测试用例步骤模型"""
    ACTION_TYPE_CHOICES = [
       ('click', '点击'),
       ('tap', '轻触'),
       ('double_tap', '双击'),
       ('long_press', '长按'),
       ('fill', '输入文本'),
       ('input', '输入'),
       ('clear', '清空'),
       ('getText', '获取文本'),
       ('waitFor', '等待元素'),
       ('hover', '悬停'),
       ('scroll', '滚动'),
       ('swipe', '滑动'),
       ('screenshot', '截图'),
       ('assert', '断言'),
       ('wait', '等待'),
       ('switchTab', '切换标签页'),
       ('launch_app', '启动应用'),
       ('close_app', '关闭应用'),
       ('back', '返回'),
       ('home', '主页'),
       ('aiAct', 'AI操作(Midscene)'),
       ('aiTap', 'AI点击(Midscene)'),
       ('aiInput', 'AI输入(Midscene)'),
       ('aiQuery', 'AI查询(Midscene)'),
       ('aiAssert', 'AI断言(Midscene)'),
       ('navigate', '导航(Midscene)'),
    ]

    ASSERT_TYPE_CHOICES = [
        ('textContains', '文本包含'),
        ('textEquals', '文本等于'),
        ('isVisible', '元素可见'),
        ('exists', '元素存在'),
        ('hasAttribute', '属性值'),
    ]

    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='steps', verbose_name='测试用例')
    step_number = models.IntegerField(verbose_name='步骤序号')
    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES, verbose_name='操作类型')
    element = models.ForeignKey(Element, on_delete=models.CASCADE, null=True, blank=True, verbose_name='目标元素')
    input_value = models.TextField(blank=True, verbose_name='输入值')
    wait_time = models.IntegerField(default=1000, verbose_name='等待时间(毫秒)')
    assert_type = models.CharField(max_length=20, choices=ASSERT_TYPE_CHOICES, blank=True, verbose_name='断言类型')
    assert_value = models.TextField(blank=True, verbose_name='断言期望值')
    description = models.TextField(blank=True, verbose_name='步骤描述')
    # 录制时点击的屏幕坐标（用于坐标回放，比 locator 更稳定）
    center_x = models.IntegerField(null=True, blank=True, verbose_name='点击横坐标(录制坐标)')
    center_y = models.IntegerField(null=True, blank=True, verbose_name='点击纵坐标(录制坐标)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ui_test_case_steps'
        verbose_name = 'UI测试用例步骤'
        verbose_name_plural = 'UI测试用例步骤'
        ordering = ['step_number']
        unique_together = ['test_case', 'step_number']

    def __str__(self):
        return f"{self.test_case.name} - 步骤{self.step_number}"


class TestCaseExecution(models.Model):
    """测试用例执行记录模型"""
    STATUS_CHOICES = [
        ('pending', '待执行'),
        ('running', '执行中'),
        ('passed', '通过'),
        ('failed', '失败'),
        ('error', '错误'),
    ]

    ENGINE_CHOICES = [
        ('playwright', 'Playwright'),
        ('selenium', 'Selenium'),
    ]

    SOURCE_CHOICES = [
        ('manual', '单用例执行'),
        ('suite', '套件执行'),
        ('scheduled', '定时任务执行'),
    ]

    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='executions', verbose_name='测试用例')
    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='test_case_executions', verbose_name='项目')
    test_suite = models.ForeignKey('TestSuite', on_delete=models.CASCADE, null=True, blank=True, related_name='case_executions', verbose_name='所属测试套件')
    execution_source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual', verbose_name='执行来源')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='执行状态')
    engine = models.CharField(max_length=20, choices=ENGINE_CHOICES, default='playwright', verbose_name='测试引擎')
    browser = models.CharField(max_length=50, default='chrome', verbose_name='浏览器')
    headless = models.BooleanField(default=False, verbose_name='无头模式')
    execution_logs = models.TextField(blank=True, verbose_name='执行日志')
    step_results = models.JSONField(default=list, blank=True, verbose_name='步骤执行结果')
    error_message = models.TextField(null=True, blank=True, verbose_name='错误信息')
    screenshots = models.JSONField(default=list, blank=True, verbose_name='截图列表')
    execution_time = models.FloatField(null=True, blank=True, verbose_name='执行时长(秒)')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    #: 人工终止信号。执行线程在「每个步骤前后」重读该字段，命中即停剩余步骤。
    #: 不缓存到内存里：用户点「停止执行」后应尽快生效，而执行线程可能换过。
    abort_requested = models.BooleanField(default=False, verbose_name='是否请求中止')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='test_case_executions', verbose_name='执行人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ui_test_case_executions'
        verbose_name = 'UI测试用例执行记录'
        verbose_name_plural = 'UI测试用例执行记录'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.test_case.name} - {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}"


class OperationRecord(models.Model):
    """操作记录模型"""
    OPERATION_TYPE_CHOICES = [
        ('create', '新增'),
        ('edit', '编辑'),
        ('delete', '删除'),
        ('run', '运行'),
        ('rerun', '重新运行'),
        ('save', '保存'),
        ('rename', '重命名'),
    ]

    RESOURCE_TYPE_CHOICES = [
        ('project', '项目'),
        ('element', '元素'),
        ('test_case', '测试用例'),
        ('script', '脚本'),
        ('suite', '套件'),
        ('execution', '执行记录'),
        ('report', '测试报告'),
    ]

    operation_type = models.CharField(max_length=20, choices=OPERATION_TYPE_CHOICES, verbose_name='操作类型')
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPE_CHOICES, verbose_name='资源类型')
    resource_id = models.IntegerField(verbose_name='资源ID')
    resource_name = models.CharField(max_length=200, verbose_name='资源名称')
    description = models.TextField(verbose_name='操作描述')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name='操作用户')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ui_operation_record'
        verbose_name = 'UI操作记录'
        verbose_name_plural = 'UI操作记录'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['resource_type', 'resource_id']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_operation_type_display()} - {self.resource_name}"


class UiScheduledTask(models.Model):
    """UI自动化定时任务模型"""
    TASK_TYPE_CHOICES = [
        ('TEST_SUITE', '测试套件执行'),
        ('TEST_CASE', '测试用例执行'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', '激活'),
        ('PAUSED', '暂停'),
        ('COMPLETED', '已完成'),
        ('FAILED', '失败'),
    ]

    TRIGGER_TYPE_CHOICES = [
        ('CRON', 'Cron表达式'),
        ('INTERVAL', '固定间隔'),
        ('ONCE', '单次执行'),
    ]

    name = models.CharField(max_length=200, verbose_name='任务名称')
    description = models.TextField(blank=True, verbose_name='任务描述')
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES, verbose_name='任务类型')
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_TYPE_CHOICES, verbose_name='触发器类型')

    # Cron表达式配置
    cron_expression = models.CharField(max_length=100, blank=True, verbose_name='Cron表达式')

    # 固定间隔配置（秒）
    interval_seconds = models.IntegerField(null=True, blank=True, verbose_name='间隔秒数')

    # 单次执行时间
    execute_at = models.DateTimeField(null=True, blank=True, verbose_name='执行时间')

    # 任务配置
    project = models.ForeignKey('UiProject', on_delete=models.CASCADE, verbose_name='关联项目')
    test_suite = models.ForeignKey('TestSuite', on_delete=models.CASCADE, null=True, blank=True,
                                   verbose_name='测试套件')
    test_cases = models.JSONField(default=list, blank=True, verbose_name='测试用例列表',
                                 help_text='测试用例ID列表，用于TEST_CASE类型任务')

    # 执行配置
    engine = models.CharField(max_length=20, default='playwright', verbose_name='执行引擎',
                             help_text='playwright或selenium')
    browser = models.CharField(max_length=20, default='chrome', verbose_name='浏览器类型')
    headless = models.BooleanField(default=False, verbose_name='无头模式')

    # 通知配置
    NOTIFICATION_TYPE_CHOICES = [
        ('email', '邮箱通知'),
        ('webhook', 'Webhook机器人'),
        ('both', '两者都发送'),
    ]
    
    notify_on_success = models.BooleanField(default=False, verbose_name='成功时通知')
    notify_on_failure = models.BooleanField(default=False, verbose_name='失败时通知')
    notification_type = models.CharField(max_length=20, blank=True, choices=NOTIFICATION_TYPE_CHOICES, 
                                        verbose_name='通知类型')
    notify_emails = models.JSONField(default=list, blank=True, verbose_name='通知邮箱列表')

    # 状态管理
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', verbose_name='任务状态')
    last_run_time = models.DateTimeField(null=True, blank=True, verbose_name='最后运行时间')
    next_run_time = models.DateTimeField(null=True, blank=True, verbose_name='下次运行时间')
    total_runs = models.IntegerField(default=0, verbose_name='总运行次数')
    successful_runs = models.IntegerField(default=0, verbose_name='成功运行次数')
    failed_runs = models.IntegerField(default=0, verbose_name='失败运行次数')

    # 执行结果
    last_result = models.JSONField(default=dict, verbose_name='最后执行结果')
    error_message = models.TextField(blank=True, verbose_name='错误信息')

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_scheduled_tasks'
        verbose_name = 'UI定时任务'
        verbose_name_plural = 'UI定时任务'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_task_type_display()})"

    def calculate_next_run(self):
        """计算下次运行时间"""
        from datetime import datetime, timedelta
        from croniter import croniter

        now = timezone.now()

        if self.trigger_type == 'CRON' and self.cron_expression:
            try:
                iter = croniter(self.cron_expression, now)
                return iter.get_next(datetime)
            except Exception:
                return None

        elif self.trigger_type == 'INTERVAL' and self.interval_seconds:
            return now + timedelta(seconds=self.interval_seconds)

        elif self.trigger_type == 'ONCE' and self.execute_at:
            return self.execute_at if self.execute_at > now else None

        return None

    def should_run_now(self):
        """检查是否应该现在运行"""
        if self.status != 'ACTIVE':
            return False

        if not self.next_run_time:
            return False

        return timezone.now() >= self.next_run_time


class UiNotificationLog(models.Model):
    """UI自动化通知日志模型"""
    NOTIFICATION_TYPES = [
        ('task_execution', '定时任务执行'),
        ('test_suite_execution', '测试套件执行'),
        ('test_case_execution', '测试用例执行'),
        ('system_alert', '系统警告'),
        ('manual', '手动通知'),
    ]

    STATUS_CHOICES = [
        ('pending', '待发送'),
        ('sending', '发送中'),
        ('success', '发送成功'),
        ('failed', '发送失败'),
        ('cancelled', '已取消'),
    ]

    task = models.ForeignKey(UiScheduledTask, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='关联任务')
    task_name = models.CharField(max_length=200, verbose_name='任务名称', help_text='相关任务的名称')
    task_type = models.CharField(max_length=20, blank=True, null=True, verbose_name='任务类型快照', help_text='发送通知时的任务类型')
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES, verbose_name='通知类型')
    sender_name = models.CharField(max_length=100, verbose_name='发件人姓名')
    sender_email = models.EmailField(verbose_name='发件人邮箱')
    recipient_info = models.JSONField(verbose_name='收件人信息', help_text='接收通知的用户信息')
    webhook_bot_info = models.JSONField(default=dict, blank=True, null=True, verbose_name='Webhook机器人信息')
    notification_content = models.TextField(verbose_name='通知内容', help_text='发送的通知内容')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='发送状态')
    error_message = models.TextField(blank=True, null=True, verbose_name='错误信息', help_text='发送失败时的错误信息')
    response_info = models.JSONField(default=dict, blank=True, null=True, verbose_name='响应信息',
                                     help_text='接收方返回的响应信息')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='发送时间')
    retry_count = models.IntegerField(default=0, verbose_name='重试次数', help_text='已重试的次数')
    is_retried = models.BooleanField(default=False, verbose_name='是否已重试')

    class Meta:
        db_table = 'ui_notification_logs'
        verbose_name = 'UI通知日志'
        verbose_name_plural = 'UI通知日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['notification_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        task_name = self.task.name if self.task else self.task_name
        return f"{task_name} - {self.get_notification_type_display()} - {self.status}"

    def get_recipient_names(self):
        """获取收件人姓名列表"""
        if self.recipient_info:
            if isinstance(self.recipient_info, list):
                recipient_list = []
                for rec in self.recipient_info:
                    email = rec.get('email', '')
                    name = rec.get('name', '')
                    if name and email:
                        recipient_list.append(f"{name}({email})")
                    elif email:
                        recipient_list.append(email)
                    else:
                        recipient_list.append('未知用户')
                return ', '.join(recipient_list)
            elif isinstance(self.recipient_info, dict):
                email = self.recipient_info.get('email', '')
                name = self.recipient_info.get('name', '')
                if name and email:
                    return f"{name}({email})"
                elif email:
                    return email
                else:
                    return '未知用户'
        return "未知收件人"

    def get_retry_status(self):
        """获取重试状态"""
        if self.is_retried:
            return f"已重试 {self.retry_count} 次"
        return "未重试"


class UiTaskNotificationSetting(models.Model):
    """UI定时任务通知设置模型"""
    NOTIFICATION_TYPES = [
        ('email', '邮箱通知'),
        ('webhook', 'Webhook机器人'),
        ('both', '两种都发送'),
    ]

    task = models.ForeignKey(UiScheduledTask, on_delete=models.CASCADE, related_name='notification_settings',
                             verbose_name='关联任务')
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='webhook',
                                         verbose_name='通知类型')
    notification_config = models.ForeignKey('core.UnifiedNotificationConfig', on_delete=models.SET_NULL,
                                            null=True, blank=True, related_name='ui_task_notification_settings',
                                            verbose_name='通知配置')
    is_enabled = models.BooleanField(default=False, verbose_name='是否启用通知')
    notify_on_success = models.BooleanField(default=True, verbose_name='成功时通知')
    notify_on_failure = models.BooleanField(default=True, verbose_name='失败时通知')
    notify_on_timeout = models.BooleanField(default=False, verbose_name='超时时通知')
    notify_on_error = models.BooleanField(default=True, verbose_name='错误时通知')
    custom_webhook_bots = models.JSONField(default=dict, blank=True, null=True, verbose_name='自定义Webhook机器人',
                                           help_text='临时覆盖通知配置中的Webhook机器人设置')
    custom_recipients = models.ManyToManyField(User, blank=True,
                                               related_name='ui_task_notification_settings_as_custom_recipient',
                                               verbose_name='自定义收件人', help_text='临时覆盖通知配置中的收件人设置')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_task_notification_settings'
        verbose_name = 'UI任务通知设置'
        verbose_name_plural = 'UI任务通知设置'
        unique_together = ['task']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.task.name} - {self.get_notification_type_display()}"

    def get_active_notification_types(self):
        """获取当前激活的通知类型"""
        active_types = []
        if self.notification_type in ['email', 'both']:
            active_types.append('email')
        if self.notification_type in ['webhook', 'both']:
            active_types.append('webhook')
        return active_types

    def get_notification_config(self):
        """获取通知配置，优先使用任务自定义配置"""
        if self.notification_config:
            return self.notification_config
        # 如果没有指定配置，使用默认配置
        # 使用字符串引用避免循环导入
        from apps.core.models import UnifiedNotificationConfig
        return UnifiedNotificationConfig.objects.filter(is_default=True, is_active=True).first()


# ==================== App 自动化相关模型 ====================

class AppDevice(models.Model):
    """移动设备管理"""
    #: 已接入/规划中的设备平台。加新平台时这里、Element.PLATFORM_CHOICES、
    #: 以及 element_provider 的平台分发都要同步。
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
        ('harmony', 'HarmonyOS'),
    ]

    DEVICE_TYPE_CHOICES = [
        ('emulator', '模拟器'),
        ('real', '真机'),
    ]

    STATUS_CHOICES = [
        ('online', '在线'),
        ('offline', '离线'),
        ('busy', '占用中'),
        ('error', '异常'),
    ]

    name = models.CharField(max_length=200, verbose_name='设备名称')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, verbose_name='平台')
    platform_version = models.CharField(max_length=50, blank=True, verbose_name='系统版本')
    device_type = models.CharField(max_length=20, choices=DEVICE_TYPE_CHOICES, default='real', verbose_name='设备类型')
    udid = models.CharField(max_length=200, unique=True, verbose_name='设备唯一标识')
    appium_server_url = models.CharField(max_length=500, default='http://localhost:4723', verbose_name='Appium Server地址')
    adb_host = models.CharField(max_length=50, null=True, blank=True, default='', verbose_name='ADB主机', help_text='手机USB所在电脑的 IP:PORT（如 192.168.8.71:5037），后端通过该电脑的ADB服务控制此设备')
    wda_host = models.CharField(max_length=50, blank=True, default='', verbose_name='WDA主机', help_text='iOS 设备专用：手机USB所在电脑的 IP（如 192.168.8.71），该电脑需已跑通 WDA 并转发 8100 端口')
    wda_port = models.PositiveIntegerField(default=8100, verbose_name='WDA端口', help_text='iOS 设备专用：WDA 服务端口，默认 8100')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline', verbose_name='设备状态')
    resolution = models.CharField(max_length=50, blank=True, verbose_name='屏幕分辨率')
    capabilities = models.JSONField(default=dict, blank=True, verbose_name='额外配置', help_text='额外的Appium Capabilities')
    last_heartbeat = models.DateTimeField(null=True, blank=True, verbose_name='最后心跳时间')
    notes = models.TextField(blank=True, verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_devices'
        verbose_name = '移动设备'
        verbose_name_plural = '移动设备'
        ordering = ['platform', 'name']

    def __str__(self):
        return f'{self.name} ({self.platform})'


class AppConfig(models.Model):
    """被测应用配置"""
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
    ]

    name = models.CharField(max_length=200, verbose_name='应用名称')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, verbose_name='平台')
    package_name = models.CharField(max_length=200, blank=True, verbose_name='包名/Bundle ID',
                                    help_text='Android包名 或 iOS Bundle ID')
    app_activity = models.CharField(max_length=200, blank=True, verbose_name='启动Activity',
                                    help_text='仅Android需要')
    app_path = models.CharField(max_length=500, blank=True, verbose_name='应用文件路径',
                                help_text='APK/IPA文件路径，留空则使用已安装应用')
    version = models.CharField(max_length=50, blank=True, verbose_name='应用版本')
    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, related_name='app_configs',
                                verbose_name='所属项目', null=True, blank=True)
    ai_project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, related_name='app_configs',
                                   verbose_name='AI用例项目', null=True, blank=True,
                                   help_text='关联AI生成测试用例模块中的项目')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_configs'
        verbose_name = '应用配置'
        verbose_name_plural = '应用配置'
        ordering = ['platform', 'name']

    def __str__(self):
        return f'{self.name} ({self.platform})'


class AICase(models.Model):
    """AI测试用例

    支持两种执行模式：
    - text: 原 browser-use 文本模式（沿用 task_description 自然语言）
    - midscene: 基于 Midscene + 真机的 APP UI 自动化，使用结构化 steps
    """
    EXECUTION_MODE_CHOICES = [
        ('text', '文本模式(browser-use)'),
        ('midscene', 'Midscene APP 自动化'),
        ('midscene_web', 'Midscene Web 自动化'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, null=True, blank=True, verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='用例名称')
    description = models.TextField(blank=True, null=True, verbose_name='描述')
    task_description = models.TextField(blank=True, null=True, verbose_name='任务描述', help_text='自然语言任务描述（text 模式使用）')
    execution_mode = models.CharField(
        max_length=20, choices=EXECUTION_MODE_CHOICES, default='text',
        verbose_name='执行模式', help_text='text=浏览器文本模式；midscene=Midscene 真机 APP 自动化'
    )
    # Midscene 模式的结构化步骤（业务无关，每步可 extract 任意业务数据存入上下文，后续步骤用 ${{key}} 引用）
    steps = models.JSONField(
        default=list, blank=True, verbose_name='步骤',
        help_text='Midscene 模式的结构化步骤列表，元素含 action/prompt/value/extract/assert 等字段'
    )
    # Midscene 模式关联被测应用与设备
    app_config = models.ForeignKey(
        AppConfig, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_cases', verbose_name='应用配置'
    )
    device = models.ForeignKey(
        AppDevice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_cases', verbose_name='执行设备'
    )
    # 弹窗（权限请求/隐私协议/开屏广告……）是随机出现的，挡路就会让步骤失败。
    # 默认自动关掉；专门验证弹窗本身的用例可以取消勾选。
    auto_dismiss_popup = models.BooleanField(
        default=True, verbose_name='自动处理弹窗',
        help_text='执行时遇到权限请求/协议/广告等弹窗自动关闭并重试该步骤'
    )
    # 第一次用自然语言跑通后，每一步「点在哪里」都已经知道了，把这些坐标写回
    # steps 并打上时间戳，之后回放就只按坐标操作、不再调用视觉模型。
    # 为空 = 还没固化（走 AI 探索模式）。
    steps_frozen_at = models.DateTimeField(
        null=True, blank=True, verbose_name='步骤固化时间',
        help_text='非空表示 steps 里已带坐标，执行时走纯坐标回放（不调用视觉模型）'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name='创建者')

    class Meta:
        db_table = 'ui_ai_cases'
        verbose_name = 'AI测试用例'
        verbose_name_plural = 'AI测试用例'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class AIExecutionRecord(models.Model):
    """AI执行记录"""
    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('running', '执行中'),
        ('passed', '成功'),
        ('failed', '失败'),
    ]

    project = models.ForeignKey(UiProject, on_delete=models.CASCADE, null=True, blank=True, verbose_name='所属项目')
    ai_case = models.ForeignKey(AICase, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='关联AI用例')
    case_name = models.CharField(max_length=200, verbose_name='用例名称快照')
    task_description = models.TextField(blank=True, default='', verbose_name='任务描述', help_text='用户输入的原始任务描述')
    execution_mode = models.CharField(max_length=20, choices=[('text', '文本模式'), ('midscene', 'Midscene APP 自动化'), ('midscene_web', 'Midscene Web 自动化')], default='text', verbose_name='执行模式')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='执行状态')
    start_time = models.DateTimeField(auto_now_add=True, verbose_name='开始时间')
    end_time = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    duration = models.FloatField(null=True, blank=True, verbose_name='执行时长(秒)')
    logs = models.TextField(blank=True, default='', verbose_name='执行日志')
    steps_completed = models.JSONField(default=list, verbose_name='已完成步骤')
    planned_tasks = models.JSONField(default=list, verbose_name='规划任务') # 规划的任务列表 [{'id': 1, 'description': '...', 'status': 'pending'}]
    executed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name='执行人')
    gif_path = models.CharField(max_length=500, null=True, blank=True, verbose_name='GIF录制路径')
    screenshots_sequence = models.JSONField(default=list, verbose_name='截图序列')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')

    # ---- 模型消耗统计 ----
    # 回放模式的目标是"零模型"，所以必须能量化每次执行到底花掉多少，
    # 否则无法判断固化到底有没有收益。数字优先取 LLM 代理返回的真实 usage，
    # 代理不可用时退化为按截图尺寸估算（见 llm_usage.py）。
    model_calls = models.IntegerField(default=0, verbose_name='模型调用次数')
    prompt_tokens = models.IntegerField(default=0, verbose_name='输入 token')
    completion_tokens = models.IntegerField(default=0, verbose_name='输出 token')
    tokens_estimated = models.BooleanField(
        default=False, verbose_name='token 为估算值',
        help_text='True 表示没有拿到接口返回的真实 usage，数字是按截图尺寸估算的'
    )
    replay_mode = models.BooleanField(
        default=False, verbose_name='纯坐标回放',
        help_text='True 表示本次执行走的是固化回放，理论上不调用视觉模型'
    )
    # ⚠️ 为什么单独记一个"回放失败降级"（2026-09-22 实测）：
    #   回放失败会自动降级成 AI 探索重跑，而旧代码把 replay_mode 直接置 False，
    #   于是报告里只显示「AI 探索」—— **用户完全看不出这次曾经试过回放**，
    #   反复重跑都是这个标签，就会得出"固化根本没生效、省不了 token"的错误结论。
    #   两个字段配合使用，报告里才能区分三种状态：
    #     纯回放       replay_mode=True                  （真的零模型）
    #     回放失败降级  replay_mode=False + replay_degraded=True
    #     全新 AI 探索  replay_mode=False + replay_degraded=False
    replay_degraded = models.BooleanField(
        default=False, verbose_name='回放失败已降级',
        help_text='True 表示本次曾尝试纯坐标回放但失败，已自动降级为 AI 探索重跑'
    )
    # 回放失败的具体原因（降级时写入，便于判断是"界面改版"还是"页面状态不对"）
    replay_fail_reason = models.CharField(
        max_length=500, blank=True, default='',
        verbose_name='回放失败原因',
        help_text='仅在 replay_degraded=True 时有值'
    )

    # ---- 多设备并行批次 ----
    # ⚠️ 为什么必须落库（2026-10-29 用户反馈"好像输出两条报告"）：
    #   多选 N 台设备时会为每台各建一条执行记录，旧实现只把批次信息拼进
    #   **日志文本**里（'本批第 1/2 台（批次 xxx）'），列表页拿不到
    #   ⇒ 用户看到同一用例、几乎同一时刻的两条记录，第一反应是"重复执行了"。
    #   落库后列表可以标「第 1/2 台」并给出批次说明。
    batch_id = models.CharField(
        max_length=32, blank=True, default='', db_index=True,
        verbose_name='并行批次号',
        help_text='多设备并行执行时同一批记录共用一个值；单设备执行时为空'
    )
    batch_index = models.IntegerField(
        default=0, verbose_name='批次内序号', help_text='从 1 开始，0 表示未参与并行批次'
    )
    batch_total = models.IntegerField(
        default=0, verbose_name='批次设备总数', help_text='本批共有几台设备并行执行'
    )
    device_name = models.CharField(
        max_length=120, blank=True, default='', verbose_name='执行设备名称快照',
        help_text='执行时所用设备的名称；设备改名/删除后仍能看出跑在哪台机器上'
    )


    class Meta:
        db_table = 'ui_ai_execution_records'
        verbose_name = 'AI执行记录'
        verbose_name_plural = 'AI执行记录'
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.case_name} - {self.get_status_display()}"


# ==================== App 自动化流水线运行记录 ====================

class AppPipelineRun(models.Model):
    """App 自动化流水线运行记录

    把「capture(抓快照) → gen_pytest(生成用例) → run_pytest(执行测试) → allure(生成报告)」
    封装成可参数化的一次运行。支持从历史记录一键重跑。
    """
    STAGE_CHOICES = [
        ('capture', '抓取快照'),
        ('gen_pytest', '生成用例'),
        ('run_pytest', '执行测试'),
        ('allure', '生成报告'),
    ]

    STATUS_CHOICES = [
        ('pending', '待执行'),
        ('running', '执行中'),
        ('success', '成功'),
        ('failed', '失败'),
    ]

    app_config = models.ForeignKey(
        AppConfig, on_delete=models.CASCADE, related_name='pipeline_runs', verbose_name='应用配置'
    )
    device = models.ForeignKey(
        AppDevice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pipeline_runs', verbose_name='设备'
    )
    stages = models.JSONField(
        default=list, verbose_name='执行阶段',
        help_text='选中的阶段列表, 如 ["capture","gen_pytest","run_pytest","allure"]'
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态'
    )
    log = models.TextField(blank=True, verbose_name='执行日志')
    result_summary = models.TextField(blank=True, verbose_name='结果摘要')
    snapshot_path = models.CharField(max_length=500, blank=True, verbose_name='快照路径')
    report_url = models.CharField(max_length=500, blank=True, verbose_name='报告地址')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, verbose_name='创建人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    duration = models.FloatField(default=0, verbose_name='耗时(秒)')

    class Meta:
        db_table = 'app_pipeline_runs'
        verbose_name = 'App流水线运行记录'
        verbose_name_plural = 'App流水线运行记录'
        ordering = ['-created_at']

    def __str__(self):
        return f"Pipeline #{self.id} - {self.app_config.name} - {self.get_status_display()}"


# ==================== 业务动作库（Prompt 与代码分离） ====================

class BusinessAction(models.Model):
    """业务动作（一个动作 = 一个明确的自然语言业务操作）。

    Prompt 与代码分离：每个动作对应一条「经过 Playground 真机验证」的语义 prompt，
    由 Playground 一键生成，也可在业务动作库管理页手工编辑。
    正式用例 AICase.steps 中可用 {ref: 动作名} 引用，避免把 prompt 内联写死在用例里。
    """
    ACTION_CHOICES = [
        ('aiAct', 'aiAct（自然语言执行）'),
        ('aiTap', 'aiTap（点击）'),
        ('aiInput', 'aiInput（输入）'),
        ('aiQuery', 'aiQuery（提取）'),
        ('assert', 'assert（断言）'),
    ]

    CATEGORY_CHOICES = [
        ('precondition', '前置准备'),
        ('business', '业务操作'),
        ('extract', '数据提取'),
        ('teardown', '收尾清理'),
        ('other', '其他'),
    ]

    name = models.CharField(max_length=100, unique=True, verbose_name='动作名', help_text='唯一标识，如 login / select-product')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='business',
                                verbose_name='分类',
                                help_text='前置准备=登录/切账号等；业务操作=下单/搜索等；收尾清理=退出登录')
    description = models.CharField(max_length=200, blank=True, verbose_name='描述')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default='aiAct', verbose_name='动作类型')
    prompt = models.TextField(verbose_name='语义指令', help_text='自然语言操作指令，禁止写坐标，跨机型可复用')
    selector_hint = models.CharField(max_length=300, blank=True, verbose_name='定位提示', help_text='可选，aiTap/aiInput 优先用此定位')
    value = models.CharField(max_length=300, blank=True, verbose_name='输入值', help_text='aiInput 使用，支持 ${{key}} 上下文变量')
    wait_ms = models.IntegerField(default=0, verbose_name='等待毫秒')
    extract = models.JSONField(default=list, blank=True, verbose_name='提取配置', help_text='[{key,prompt,type,required}]，aiQuery 提取业务数据写入上下文')
    assert_config = models.JSONField(default=dict, blank=True, verbose_name='断言配置', help_text='{prompt, expect}')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='创建人')

    class Meta:
        db_table = 'ui_business_actions'
        verbose_name = '业务动作'
        verbose_name_plural = '业务动作'
        ordering = ['name']

    def __str__(self):
        return self.name

    def to_step(self) -> dict:
        """展开为 midscene 引擎可直接执行的 step dict。"""
        step = {
            'name': self.name,
            'action': self.action,
            'prompt': self.prompt,
            'selector_hint': self.selector_hint or '',
            'value': self.value or '',
            'wait_ms': self.wait_ms or 0,
        }
        if self.extract:
            step['extract'] = self.extract
        if self.assert_config:
            step['assert'] = self.assert_config
        return step


# ==================== APK 智能回归（MVP：APK → REA → 人工确认 → Midscene → HTML 报告） ====================
#
# 与已有 AppPipelineRun 的区别（别混用）：
#   AppPipelineRun = 「抓快照 → 生成 pytest → 跑 pytest → Allure」的脚本型流水线
#   ApkRegressionRun = 「上传 APK → 静态+REA 分析 → 人工确认测试路径 → Midscene 真机执行 → HTML 报告」
# 后者的用例载体是**路径（path）**，执行产物是**自包含 HTML**，不需要 pytest/Allure。


class ApkArtifact(models.Model):
    """上传的 APK 与其静态解析结果。

    `sha256` 做去重：同一个包重复上传不必再存一份文件，也方便识别
    「同一个包重新上传后分析结果却不同」这类问题（二进制没变 → 结果必须可复现）。
    """
    name = models.CharField(max_length=200, verbose_name='展示名称', blank=True, default='')

    file = models.FileField(upload_to='apk/%Y/%m/', verbose_name='APK 文件')
    original_name = models.CharField(max_length=255, blank=True, default='', verbose_name='原始文件名')
    size = models.BigIntegerField(default=0, verbose_name='文件大小(字节)')
    sha256 = models.CharField(max_length=64, blank=True, default='', db_index=True,
                              verbose_name='SHA-256')

    package_name = models.CharField(max_length=200, blank=True, default='', db_index=True,
                                    verbose_name='包名')
    version_name = models.CharField(max_length=50, blank=True, default='', verbose_name='版本名')
    version_code = models.IntegerField(null=True, blank=True, verbose_name='版本号')
    min_sdk = models.IntegerField(null=True, blank=True, verbose_name='minSdk')
    target_sdk = models.IntegerField(null=True, blank=True, verbose_name='targetSdk')
    launch_activity = models.CharField(max_length=300, blank=True, default='',
                                       verbose_name='启动 Activity')

    #: 直接存 `apk_inspector.inspect_apk()` 的原始结果（activities/permissions/
    #: dangerous_permissions/has_cleartext …），避免为每个字段单独建模。
    manifest_info = models.JSONField(default=dict, blank=True, verbose_name='清单解析结果')

    #: 非空表示解析失败，值为可读原因（前端直接展示，不用猜）
    parse_error = models.CharField(max_length=500, blank=True, default='', verbose_name='解析失败原因')

    app_config = models.ForeignKey(
        AppConfig, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='apk_artifacts', verbose_name='关联应用配置',
        help_text='上传后自动创建/复用的 AppConfig（包名+启动 Activity 已回填）')

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   verbose_name='上传人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='上传时间')

    class Meta:
        db_table = 'ui_apk_artifacts'
        verbose_name = 'APK 制品'
        verbose_name_plural = 'APK 制品'
        ordering = ['-created_at']

    def __str__(self):
        return '%s %s' % (self.package_name or self.original_name, self.version_name or '')

    @property
    def display_name(self):
        return self.name or self.original_name or self.package_name or ('APK#%s' % self.pk)


class ApkRegressionRun(models.Model):
    """一次「APK → 分析 → 确认 → 执行 → 报告」的完整运行。

    状态机（非法流转由 workflow 层拦截，不靠前端约束）：
        uploaded
          → analyzing      （阶段1：静态清单 + REA 分析）
          → analyzed       （产出 path 清单，等待人工确认）
          → confirmed      （人工确认闸门通过，指纹绑定）
          → running        （逐条路径 Midscene 真机执行）
          → done / failed  （报告已生成 / 执行或分析出错）
    """
    STATUS_CHOICES = [
        ('uploaded', '已上传'),
        ('analyzing', '分析中'),
        ('analyzed', '待确认'),
        ('analyzing_failed', '分析失败'),
        ('confirmed', '已确认'),
        ('running', '执行中'),
        ('done', '已完成'),
        ('failed', '执行失败'),
        ('cancelled', '已取消'),
    ]

    STATUS_MAP = dict(STATUS_CHOICES)

    artifact = models.ForeignKey(ApkArtifact, on_delete=models.CASCADE,
                                 related_name='runs', verbose_name='APK')
    device = models.ForeignKey(AppDevice, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='apk_runs', verbose_name='执行设备')
    project = models.ForeignKey(UiProject, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='apk_runs', verbose_name='所属项目')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='uploaded',
                              verbose_name='状态')

    #: REA 分析结果（requirements / pages / dangerous_actions / exception_pages /
    #: sensitive_pages / analysis_notes），`paths` 单独一列便于前端编辑与指纹。
    rea_analysis = models.JSONField(default=dict, blank=True, verbose_name='REA 分析结果')
    paths = models.JSONField(default=list, blank=True, verbose_name='测试路径清单')

    # ---- 业务资料输入源（操作说明 / 知识库 / 人工补充） ----
    #: 上传的操作说明文档（已抽取的纯文本）。
    #: 存文本而不是只存文件路径：① 分析/重新分析要反复读，避免每次重解析；
    #: ② 文件可能被清理，文本留下来才能复现当时的分析依据。
    #: 结构：[{'name','text','chars','truncated','uploaded_at'}]
    context_docs = models.JSONField(default=list, blank=True,
                                    verbose_name='操作说明文档（已抽文本）')
    #: 人工补充的业务说明（自由文本）
    context_notes = models.TextField(blank=True, default='',
                                     verbose_name='人工补充说明')

    #: 人工确认记录：{approved, approved_at, approved_by, fingerprint, notes}
    confirmation = models.JSONField(default=dict, blank=True, verbose_name='人工确认')

    #: 逐条路径的执行结果（含 Midscene step 明细、截图、断言）
    results = models.JSONField(default=list, blank=True, verbose_name='执行结果')

    log = models.TextField(blank=True, default='', verbose_name='运行日志')
    summary = models.JSONField(default=dict, blank=True, verbose_name='结果摘要')
    report_path = models.CharField(max_length=500, blank=True, default='', verbose_name='HTML 报告路径')
    # 「人工终止」信号：执行线程每跑完一条路径就回查一次，置位则停止剩余路径。
    # 为什么要独立字段而不是只看 status：中止时会把 status 直接落成 failed，
    # 若用 status 判断，线程无法区分"被中止"和"自己跑挂"，也读不到用户意图。
    abort_requested = models.BooleanField(
        default=False, verbose_name='已请求中止',
        help_text='用户点了「终止执行」后置位；执行线程在下一条路径开始前读取并停止')

    model_calls = models.IntegerField(default=0, verbose_name='模型调用次数')
    prompt_tokens = models.IntegerField(default=0, verbose_name='输入 token')
    completion_tokens = models.IntegerField(default=0, verbose_name='输出 token')

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   verbose_name='创建人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    duration = models.FloatField(default=0, verbose_name='耗时(秒)')

    class Meta:
        db_table = 'ui_apk_regression_runs'
        verbose_name = 'APK 智能回归运行'
        verbose_name_plural = 'APK 智能回归运行'
        ordering = ['-created_at']

    def __str__(self):
        return 'APK回归 #%s - %s - %s' % (
            self.pk, self.artifact.display_name if self.artifact_id else '',
            self.get_status_display())

    # ---- 人工确认闸门 ----
    #: 参与确认指纹的字段 —— **故意不含 `name`**。
    #:
    #: 方案 C（2026-10-28 与用户确认）：改「路径名称」只是改标注，不影响实际跑什么，
    #: 不应该让确认失效（否则改个错别字都要重新确认一遍）；
    #: 改「步骤/跳过项/类型」是真的改了行为，必须重新人工确认。
    #: 这个边界就是"执行内容"与"展示文案"的分界。
    FINGERPRINT_FIELDS = ('steps', 'skip', 'type')

    def execution_payload(self):
        """只包含**会影响真机执行结果**的字段，用于计算确认指纹。"""
        out = []
        for p in (self.paths or []):
            if not isinstance(p, dict):
                out.append(p)
                continue
            item = {k: p.get(k) for k in self.FINGERPRINT_FIELDS}
            # steps / skip 做归一化：去掉首尾空白、丢弃空项，
            # 避免"多了个空格"也被判成内容变更而反复要求确认。
            item['steps'] = [str(s).strip() for s in (p.get('steps') or [])
                             if str(s or '').strip()]
            item['skip'] = [str(s).strip() for s in (p.get('skip') or [])
                            if str(s or '').strip()]
            out.append(item)
        return out

    def paths_fingerprint(self):
        """把「会真正执行的内容」算成一个 SHA-256 指纹。

        沿用墨刀线 `ModaoPrototype.case_generation_fingerprint()` 的思路：
        批准绑定的是**内容指纹**而不是时间戳，因此批准后再改内容会自动失效，
        必须重新确认 —— 防止「确认过 A 路径，实际跑的是被偷偷改成 B 的路径」。

        ⚠️ 指纹范围见 `FINGERPRINT_FIELDS`：**不含 name**。
        所以改名称不会触发失效，改步骤会。
        """
        payload = json.dumps(self.execution_payload(), ensure_ascii=False,
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def is_approved(self):
        """当前路径清单是否已被人工确认（指纹必须匹配）。"""
        conf = self.confirmation or {}
        if not conf.get('approved'):
            return False
        return conf.get('fingerprint') == self.paths_fingerprint()

    def approval_blockers(self):
        """未通过确认闸门的原因（可读列表，直接回给前端）。"""
        reasons = []
        if not (self.paths or []):
            reasons.append('尚未生成任何测试路径，无法确认')
        # 解析错误在 artifact 上，不在 run 上（曾误写成 self.parse_error 直接 AttributeError）
        parse_error = getattr(getattr(self, 'artifact', None), 'parse_error', '') or ''
        if parse_error:
            reasons.append('APK 解析失败：%s' % parse_error)
        if not (self.confirmation or {}).get('approved'):
            reasons.append('尚未人工确认测试路径')
        elif not self.is_approved():
            reasons.append('路径清单在确认后被修改，原确认已失效，请重新确认')
        return reasons


# ======================================================================
# AI 智能模式：独立的模型配置
# ======================================================================
class AiModeModelConfig(models.Model):
    """「AI 智能模式」模块专属的模型配置（**与 requirement_analysis.AIModelConfig 完全隔离**）。

    为什么新建一张表，而不是复用 AIModelConfig
    ------------------------------------------
    1. **互不干扰是硬要求**：AIModelConfig 的 role 是全局共享的，同一 role 只能有一个
       is_active=True（保存时会批量禁用同角色其它记录）。若 AI 智能模式复用
       `role='analyzer'`，用户在配置页"启用新模型"就会把墨刀需求分析正在用的
       analyzer 一起禁掉 —— 这正是用户明确要避免的"两个地方统一"。
    2. **作用域清晰**：本表只服务 AI 智能模式（APK 回归 / Midscene 等），
       其它功能继续读 AIModelConfig，改这里不会波及别处。

    两个角色（一次分析 + 一次执行，能力需求不同，必须分开配）
      - analysis  ：纯文本推理，读 APK 静态清单产出测试路径。便宜、快即可。
      - vision    ：多模态视觉，看截图定位元素。必须支持图片输入。
    """

    ROLE_ANALYSIS = 'analysis'
    ROLE_VISION = 'vision'
    ROLE_CHOICES = [
        (ROLE_ANALYSIS, '分析与编排（文本推理）'),
        (ROLE_VISION, '真机执行（视觉多模态）'),
    ]

    #: 与 AIModelConfig.MODEL_CHOICES 对齐，另外补上方案里实际在用的几个
    MODEL_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('qwen', '通义千问 (Qwen)'),
        ('openai', 'OpenAI'),
        ('azure_openai', 'Azure OpenAI'),
        ('anthropic', 'Anthropic'),
        ('google_gemini', 'Google Gemini'),
        ('siliconflow', '硅基流动 (SiliconFlow)'),
        ('other', '其他 (OpenAI 兼容)'),
    ]

    name = models.CharField(max_length=100, verbose_name='配置名称')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='用途角色',
                            db_index=True)
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES,
                                  verbose_name='模型提供商', default='qwen')
    model_name = models.CharField(max_length=100, verbose_name='模型名称',
                                  help_text='如 deepseek-chat / qwen3.7-plus')
    api_key = models.CharField(max_length=300, blank=True, default='',
                               verbose_name='API Key')
    base_url = models.CharField(max_length=300, blank=True, default='',
                                verbose_name='API Base URL',
                                help_text='OpenAI 兼容地址，如 https://dashscope.aliyuncs.com/compatible-mode/v1')
    model_family = models.CharField(
        max_length=50, blank=True, default='', verbose_name='Midscene 模型家族',
        help_text='仅视觉角色用：midscene 的 MIDSCENE_MODEL_FAMILY，'
                  '如 qwen3-vl / doubao-vision / gemini / glm-v。留空则按提供商自动推断')
    max_tokens = models.IntegerField(default=4096, verbose_name='最大 Token 数')
    temperature = models.FloatField(default=0.3, verbose_name='温度参数')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    notes = models.CharField(max_length=300, blank=True, default='', verbose_name='备注')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='ai_mode_model_configs', verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ui_ai_mode_model_configs'
        verbose_name = 'AI 智能模式模型配置'
        verbose_name_plural = 'AI 智能模式模型配置'
        ordering = ['role', '-is_active', '-updated_at']
        indexes = [models.Index(fields=['role', 'is_active'])]

    def __str__(self):
        return '%s - %s(%s)' % (self.get_role_display(), self.name, self.model_name)

    #: 各提供商默认的 OpenAI 兼容 Base URL（用户没填时兜底）
    DEFAULT_BASE_URLS = {
        'deepseek': 'https://api.deepseek.com',
        'qwen': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'siliconflow': 'https://api.siliconflow.cn/v1',
        'openai': 'https://api.openai.com/v1',
    }

    #: 视觉角色在 midscene 里需要的 model family（用户没填时按提供商/模型名推断）
    #: 取值须在 midscene 支持的集合内：doubao-vision/gemini/qwen2.5-vl/qwen3-vl/glm-v/gpt-5 等。
    #: 写错会被 midscene 直接判为非法值（回退成 openai 后报错），所以宁可留空让它自己认。
    def resolved_base_url(self):
        return (self.base_url or '').strip() or self.DEFAULT_BASE_URLS.get(self.model_type, '')

    def resolved_family(self):
        """推断 midscene 的 MIDSCENE_MODEL_FAMILY。

        ⚠️ family 为空会让 midscene 回退到非法值 `openai` 并直接报错，
        所以这里的推断宁可给出一个合理值，也不要返回空。

        踩过的坑（2026-10-28）：最初只认模型名里带 "vl" 的（qwen3-vl / qwen2.5-vl），
        但本项目实际在用的多模态模型叫 `qwen3.7-plus` —— 名字里根本没有 "vl"，
        于是推断结果为空，真机执行时才暴露成 "Invalid MIDSCENE_MODEL_FAMILY"。
        ⇒ 判断 qwen 系是否为多模态，改为按 `qwen3`/`vl`/`omni` 任一命中来认。
        宁可对纯文本 qwen 也给 qwen3-vl（最多浪费一次调用），
        也不要让视觉角色拿到空 family 直接崩。
        """
        fam = (self.model_family or '').strip()
        if fam:
            return fam

        mn = (self.model_name or '').lower()
        mt = (self.model_type or '').lower()

        # 阿里 / 通义：qwen3.x-*、*-vl-*、*-omni-* 都是多模态
        if 'qwen' in mn or mt == 'qwen':
            if mn.startswith('qwen3'):
                return 'qwen3-vl'
            if 'vl' in mn or 'omni' in mn:
                return 'qwen3-vl'
            if mn.startswith('qwen2') or 'qwen2' in mn:
                return 'qwen2.5-vl'
            return 'qwen3-vl'

        if 'glm' in mn or 'glm' in mt:
            return 'glm-v'
        if 'gemini' in mn or mt == 'google_gemini':
            return 'gemini'
        if 'doubao' in mn or 'seed' in mn:
            return 'doubao-vision'
        if mn.startswith('gpt-') or 'gpt' in mn:
            return 'gpt-5'
        return ''

    def as_runtime_config(self):
        """转成 midscene_engine / apk_rea 需要的 dict 形态。"""
        return {
            'api_key': (self.api_key or '').strip(),
            'base_url': self.resolved_base_url(),
            'model_name': self.model_name or '',
            'model_family': self.resolved_family(),
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
        }


def get_active_ai_mode_config(role):
    """按角色取 AI 智能模式的启用配置，返回 (config, error)。

    同一角色有多个启用记录时按 updated_at 取最新 —— 保存逻辑会保证只有一个启用，
    这里再兜一层，避免脏数据导致行为不确定。
    """
    if role not in (AiModeModelConfig.ROLE_ANALYSIS, AiModeModelConfig.ROLE_VISION):
        return None, '未知角色：%s' % role
    qs = AiModeModelConfig.objects.filter(role=role, is_active=True)
    cfg = qs.order_by('-updated_at').first()
    if not cfg:
        label = dict(AiModeModelConfig.ROLE_CHOICES).get(role, role)
        return None, ('AI 智能模式未配置「%s」模型，请到「AI 智能模式 → 模型配置」新增并启用' % label)
    if not (cfg.api_key or '').strip():
        return None, '「%s」模型「%s」缺少 API Key' % (cfg.get_role_display(), cfg.name)
    if not cfg.model_name:
        return None, '「%s」模型「%s」缺少模型名称' % (cfg.get_role_display(), cfg.name)
    return cfg, ''


