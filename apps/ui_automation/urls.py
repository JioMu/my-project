from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework.routers import DefaultRouter
from .element_picker_views import (
    dump_device_elements,
    save_picked_elements,
    learn_locator,
    match_steps_to_elements,
)
from .views import (
    UiProjectViewSet,
    LocatorStrategyViewSet,
    ElementGroupViewSet,
    ElementViewSet,
    TestScriptViewSet,
    PageObjectViewSet,
    ScriptStepViewSet,
    TestSuiteViewSet,
    TestExecutionViewSet,
    ScreenshotViewSet,
    TestCaseViewSet,
    TestCaseStepViewSet,
    TestCaseExecutionViewSet,
    UiScheduledTaskViewSet,
    AIExecutionRecordViewSet,
    AICaseViewSet,
    UiNotificationLogViewSet,
    OperationRecordViewSet,
    UiDashboardViewSet,
    AppDeviceViewSet,
    AppConfigViewSet,
    BusinessActionViewSet,
    get_all_projects_unified,
    ensure_ui_project,
    learn_elements,
    save_knowledge,
    start_recording,
    recording_page,
    record_action,
    stop_recording,
    generate_recording_case,
    import_airtest_script,
    device_agent_register,
    llm_proxy,
    ios_dump_elements,
)
from .views_config import EnvironmentConfigViewSet, AIIntelligentModeConfigViewSet
from .pipeline_views import AppPipelineRunViewSet
from . import apk_views
from . import apk_context_views
from . import ai_mode_config_views

router = DefaultRouter()
router.register(r'dashboard', UiDashboardViewSet, basename='dashboard')
router.register(r'projects', UiProjectViewSet)
router.register(r'locator-strategies', LocatorStrategyViewSet)
router.register(r'element-groups', ElementGroupViewSet)
router.register(r'elements', ElementViewSet)
router.register(r'test-scripts', TestScriptViewSet)
router.register(r'page-objects', PageObjectViewSet)
router.register(r'steps', ScriptStepViewSet)
router.register(r'test-suites', TestSuiteViewSet)
router.register(r'test-executions', TestExecutionViewSet)
router.register(r'screenshots', ScreenshotViewSet)
router.register(r'test-cases', TestCaseViewSet)
router.register(r'test-case-steps', TestCaseStepViewSet)
router.register(r'test-case-executions', TestCaseExecutionViewSet)
router.register(r'scheduled-tasks', UiScheduledTaskViewSet)
router.register(r'ai-execution-records', AIExecutionRecordViewSet)
router.register(r'ai-cases', AICaseViewSet, basename='ai-cases')
router.register(r'ai-case-generation', AICaseViewSet, basename='ai-case-generation')
router.register(r'notification-logs', UiNotificationLogViewSet)
router.register(r'operation-records', OperationRecordViewSet)


# Configuration Center APIs
router.register(r'config/environment', EnvironmentConfigViewSet, basename='config-environment')
router.register(r'config/ai-mode', AIIntelligentModeConfigViewSet, basename='config-ai-mode')
router.register(r'ai-models', AIIntelligentModeConfigViewSet, basename='ai-models')

# App 自动化设备管理
router.register(r'app-devices', AppDeviceViewSet)
router.register(r'app-configs', AppConfigViewSet)
router.register(r'app-pipelines', AppPipelineRunViewSet)
router.register(r'business-actions', BusinessActionViewSet)

urlpatterns = [
    # ⚠️ 元素拾取器必须排在 router 之前！
    #    router 注册了 `elements`（ElementViewSet），会生成 `elements/<pk>/`，
    #    排在前面就会把 `elements/dump/` 当成 pk='dump' 吃掉 → 返回 405。
    path('elements/dump/', dump_device_elements, name='elements-dump'),
    path('elements/save-picked/', save_picked_elements, name='elements-save-picked'),
    path('elements/learn-locator/', learn_locator, name='elements-learn-locator'),
    # 用元素库校正步骤的定位（自然语言用例 → 真元素定位）
    path('elements/match-steps/', match_steps_to_elements, name='elements-match-steps'),


    path('', include(router.urls)),
    path('all-projects/', get_all_projects_unified, name='all-projects-unified'),
    path('ensure-ui-project/', ensure_ui_project, name='ensure-ui-project'),
    path('learn-elements/', learn_elements, name='learn-elements'),
    path('save-knowledge/', save_knowledge, name='save-knowledge'),
    path('recording/start/', start_recording, name='recording-start'),
    path('recording/page/', recording_page, name='recording-page'),
    path('recording/action/', record_action, name='recording-action'),
    path('recording/stop/', stop_recording, name='recording-stop'),
    path('recording/generate/', generate_recording_case, name='recording-generate'),
    path('airtest/import/', import_airtest_script, name='airtest-import'),
    path('device-agent/register/', device_agent_register, name='device-agent-register'),
    # LLM 用量统计代理：OpenAI 兼容转发，顺带把 usage 记到执行记录上
    path('llm-proxy/', llm_proxy, name='llm-proxy'),
    path('llm-proxy/<path:rest>', llm_proxy, name='llm-proxy-rest'),
    # 导出 iOS 当前页面的可访问性元素树（排查「精确定位该填什么」）
    path('ios-dump-elements/', ios_dump_elements, name='ios-dump-elements'),

    # ---- APK 智能回归（MVP：APK → REA 分析 → 人工确认 → Midscene → HTML 报告） ----
    path('apk-regression/artifacts/upload/', apk_views.upload_apk, name='apk-upload'),
    path('apk-regression/artifacts/', apk_views.artifact_list, name='apk-artifacts'),
    path('apk-regression/artifacts/<int:pk>/', apk_views.artifact_detail, name='apk-artifact-detail'),
    path('apk-regression/runs/', apk_views.run_list, name='apk-runs'),
    path('apk-regression/runs/<int:pk>/', apk_views.run_detail, name='apk-run-detail'),
    path('apk-regression/runs/<int:pk>/analyze/', apk_views.run_analyze, name='apk-run-analyze'),
    path('apk-regression/runs/<int:pk>/approve/', apk_views.run_approve, name='apk-run-approve'),
    path('apk-regression/runs/<int:pk>/execute/', apk_views.run_execute, name='apk-run-execute'),
    path('apk-regression/runs/<int:pk>/abort/', apk_views.run_abort, name='apk-run-abort'),

    # ---- 业务资料输入源（操作说明文档 / 项目知识库 / 人工补充） ----
    path('apk-regression/projects/', apk_context_views.project_options,
         name='apk-context-projects'),
    path('apk-regression/projects/<int:pk>/knowledge/', apk_context_views.project_knowledge,
         name='apk-context-knowledge'),
    path('apk-regression/runs/<int:pk>/context/', apk_context_views.run_context,
         name='apk-run-context'),
    path('apk-regression/runs/<int:pk>/context/upload/', apk_context_views.upload_doc,
         name='apk-context-upload'),
    path('apk-regression/runs/<int:pk>/context/docs/<int:idx>/', apk_context_views.delete_doc,
         name='apk-context-doc-delete'),
    path('apk-regression/runs/<int:pk>/context/preview/', apk_context_views.context_preview,
         name='apk-context-preview'),
    path('apk-regression/runs/<int:pk>/yaml/', apk_views.run_yaml, name='apk-run-yaml'),
    path('apk-regression/runs/<int:pk>/report/', apk_views.run_report, name='apk-run-report'),

    # ---- AI 智能模式专属模型配置（独立表 ui_ai_mode_model_configs，与 AIModelConfig 互不影响） ----
    path('ai-mode-models/roles/', ai_mode_config_views.role_meta, name='ai-mode-model-roles'),
    path('ai-mode-models/active/', ai_mode_config_views.active_configs, name='ai-mode-model-active'),
    path('ai-mode-models/test/', ai_mode_config_views.test_unsaved, name='ai-mode-model-test'),
    path('ai-mode-models/', ai_mode_config_views.config_list, name='ai-mode-models'),
    path('ai-mode-models/<int:pk>/', ai_mode_config_views.config_detail, name='ai-mode-model-detail'),
    path('ai-mode-models/<int:pk>/test/', ai_mode_config_views.test_saved, name='ai-mode-model-test-saved'),
]

# 添加媒体文件路由
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)