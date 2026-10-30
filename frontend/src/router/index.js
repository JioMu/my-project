import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

// 静态导入常用组件来避免动态导入问题
import Login from '@/views/auth/Login.vue'
import Register from '@/views/auth/Register.vue'
import Layout from '@/layout/index.vue'
import ProjectList from '@/views/projects/ProjectList.vue'

const routes = [
  {
    path: '/',
    redirect: '/home'
  },
  {
    path: '/home',
    name: 'Home',
    component: () => import('@/views/Home.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/help',
    name: 'Help',
    component: () => import('@/views/help/HelpCenter.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: { requiresGuest: true }
  },
  {
    path: '/register',
    name: 'Register',
    component: Register,
    meta: { requiresGuest: true }
  },
  {
    path: '/ai-generation/assistant',
    name: 'Assistant',
    component: () => import('@/views/assistant/AssistantView.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/ai-generation',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'requirement-analysis'
      },
      {
        path: 'requirement-analysis',
        name: 'RequirementAnalysis',
        component: () => import('@/views/requirement-analysis/RequirementAnalysisView.vue')
      },
      {
        path: 'modao-skill',
        name: 'ModaoSkill',
        component: () => import('@/views/requirement-analysis/ModaoListView.vue')
      },
      {
        path: 'modao-canvas/:id',
        name: 'ModaoCanvas',
        component: () => import('@/views/requirement-analysis/ModaoSkillView.vue'),
        props: true
      },
      {
        path: 'modao-board/:id',
        name: 'ModaoCanvasBoard',
        component: () => import('@/views/requirement-analysis/ModaoCanvasBoard.vue'),
        props: true
      },
      {
        path: 'modao-pipeline/:id',
        name: 'CanvasPipelineView',
        component: () => import('@/views/requirement-analysis/CanvasPipelineView.vue'),
        props: true
      },
      {
        path: 'modao-case-merge/:id',
        name: 'CaseMergeView',
        component: () => import('@/views/requirement-analysis/CaseMergeView.vue'),
        props: true
      },
      {
        path: 'projects',
        name: 'Projects',
        component: ProjectList
      },
      {
        path: 'projects/:id',
        name: 'ProjectDetail',
        component: () => import('@/views/projects/ProjectDetail.vue')
      },
      {
        path: 'testcases',
        name: 'TestCases',
        component: () => import('@/views/testcases/TestCaseList.vue')
      },
      {
        path: 'testcases/create',
        name: 'CreateTestCase',
        component: () => import('@/views/testcases/TestCaseForm.vue')
      },
      {
        path: 'testcases/:id',
        name: 'TestCaseDetail',
        component: () => import('@/views/testcases/TestCaseDetail.vue')
      },
      {
        path: 'testcases/:id/edit',
        name: 'EditTestCase',
        component: () => import('@/views/testcases/TestCaseEdit.vue')
      },
      {
        path: 'versions',
        name: 'Versions',
        component: () => import('@/views/versions/VersionList.vue')
      },
      {
        path: 'zentao-coverage',
        name: 'ZentaoCoverage',
        component: () => import('@/views/zentao/ZentaoCoverage.vue')
      },
      {
        path: 'zentao-defect-review',
        name: 'ZentaoDefectReview',
        component: () => import('@/views/zentao/ZentaoDefectReview.vue')
      },
      {
        path: 'reviews',
        name: 'Reviews',
        component: () => import('@/views/reviews/ReviewList.vue')
      },
      {
        path: 'reviews/create',
        name: 'CreateReview',
        component: () => import('@/views/reviews/ReviewForm.vue')
      },
      {
        path: 'reviews/:id',
        name: 'ReviewDetail',
        component: () => import('@/views/reviews/ReviewDetail.vue')
      },
      {
        path: 'reviews/:id/edit',
        name: 'EditReview',
        component: () => import('@/views/reviews/ReviewForm.vue')
      },
      {
        path: 'review-templates',
        name: 'ReviewTemplates',
        component: () => import('@/views/reviews/ReviewTemplateList.vue')
      },
      {
        path: 'testsuites',
        name: 'TestSuites',
        component: () => import('@/views/testsuites/TestSuiteList.vue')
      },
      {
        path: 'executions',
        name: 'Executions',
        component: () => import('@/views/executions/ExecutionListView.vue')
      },
      {
        path: 'executions/:id',
        name: 'ExecutionDetail',
        component: () => import('@/views/executions/ExecutionDetailView.vue')
      },
      {
        path: 'reports',
        name: 'AiTestReport',
        component: () => import('@/views/reports/AiTestReport.vue')
      },

      {
        path: 'prompt-config',
        name: 'PromptConfig',
        component: () => import('@/views/requirement-analysis/PromptConfig.vue')
      },
      {
        path: 'generated-testcases',
        name: 'GeneratedTestCases',
        component: () => import('@/views/requirement-analysis/GeneratedTestCaseList.vue')
      },
      {
        path: 'task-detail/:taskId',
        name: 'TaskDetail',
        component: () => import('@/views/requirement-analysis/TaskDetail.vue')
      },
      {
        path: 'users',
        name: 'UserManagement',
        component: () => import('@/views/users/UserManagement.vue')
      },
      {
        path: 'profile',
        name: 'Profile',
        component: () => import('@/views/profile/UserProfile.vue')
      }
    ]
  },
  {
    path: '/api-testing',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'dashboard'
      },
      {
        path: 'dashboard',
        name: 'ApiDashboard',
        component: () => import('@/views/api-testing/Dashboard.vue')
      },
      {
        path: 'projects',
        name: 'ApiProjects',
        component: () => import('@/views/api-testing/ProjectManagement.vue')
      },
      {
        path: 'interfaces',
        name: 'ApiInterfaces',
        component: () => import('@/views/api-testing/InterfaceManagement.vue')
      },
      {
        path: 'automation',
        name: 'ApiAutomation',
        component: () => import('@/views/api-testing/AutomationTesting.vue')
      },
      {
        path: 'history',
        name: 'ApiHistory',
        component: () => import('@/views/api-testing/RequestHistory.vue')
      },
      {
        path: 'environments',
        name: 'ApiEnvironments',
        component: () => import('@/views/api-testing/EnvironmentManagement.vue')
      },
      {
        path: 'reports',
        name: 'ApiReports',
        component: () => import('@/views/api-testing/ReportView.vue')
      },
      {
        path: 'scheduled-tasks',
        name: 'ApiScheduledTasks',
        component: () => import('@/views/api-testing/ScheduledTasks.vue')
      },
      {
        path: 'notification-logs',
        name: 'ApiNotificationLogs',
        component: () => import('@/views/notification/NotificationLogs.vue')
      }
    ]
  },
  // 自动化测试（合并原 UI自动化 + App自动化 的入口）
  // 只提供「模块首页」这一层：dashboard 复用 UI 自动化的看板，
  // 其余功能页仍走原有的 /ui-automation/* 与 /app-automation/* 路由，
  // 这样 20 处页面内跳转与用户旧书签都不用改。
  {
    path: '/automation',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'dashboard'
      },
      {
        path: 'dashboard',
        name: 'AutomationDashboard',
        component: () => import('@/views/ui-automation/dashboard/Dashboard.vue')
      }
    ]
  },
  {
    path: '/ui-automation',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'dashboard'
      },
      {
        path: 'dashboard',
        name: 'UiDashboard',
        component: () => import('@/views/ui-automation/dashboard/Dashboard.vue')
      },
      {
        path: 'projects',
        name: 'UiProjects',
        component: () => import('@/views/ui-automation/projects/ProjectList.vue')
      },
      {
        path: 'elements-enhanced',
        name: 'UiElementsEnhanced',
        component: () => import('@/views/ui-automation/elements/ElementManagerEnhanced.vue')
      },
      {
        path: 'test-cases',
        name: 'UiTestCases',
        component: () => import('@/views/ui-automation/test-cases/TestCaseManager.vue')
      },
      {
        path: 'scripts-enhanced',
        name: 'UiScriptsEnhanced',
        component: () => import('@/views/ui-automation/scripts/ScriptEditorEnhanced.vue')
      },
      {
        path: 'scripts',
        name: 'UiScripts',
        component: () => import('@/views/ui-automation/scripts/ScriptList.vue')
      },
      {
        path: 'suites',
        name: 'UiSuites',
        component: () => import('@/views/ui-automation/suites/SuiteList.vue')
      },
      {
        path: 'executions',
        name: 'UiExecutions',
        component: () => import('@/views/ui-automation/executions/ExecutionList.vue')
      },
      {
        path: 'reports',
        name: 'UiReports',
        component: () => import('@/views/ui-automation/reports/ReportList.vue')
      },
      {
        path: 'scheduled-tasks',
        name: 'UiScheduledTasks',
        component: () => import('@/views/ui-automation/scheduled-tasks/ScheduledTasks.vue')
      },
      {
        path: 'notification-logs',
        name: 'UiNotificationLogs',
        component: () => import('@/views/ui-automation/notification/NotificationLogs.vue')
      },
      {
        path: 'app-devices',
        name: 'UiAppDeviceList',
        component: () => import('@/views/ui-automation/app/AppDeviceList.vue')
      },
      {
        path: 'app',
        name: 'UiAppConfig',
        component: () => import('@/views/ui-automation/app/AppConfigList.vue')
      }
    ]
  },
  {
    path: '/app-automation',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'dashboard'
      },
      {
        path: 'dashboard',
        name: 'AppDashboard',
        component: () => import('@/views/app-automation/dashboard/Dashboard.vue')
      },
      {
        path: 'devices',
        name: 'AppDevices',
        component: () => import('@/views/app-automation/devices/DeviceList.vue')
      },
      {
        path: 'projects',
        name: 'AppProjects',
        component: () => import('@/views/app-automation/projects/ProjectList.vue')
      },
      {
        path: 'suites',
        name: 'AppSuites',
        component: () => import('@/views/app-automation/suites/SuiteList.vue')
      },
      {
        path: 'test-cases',
        name: 'AppTestCases',
        component: () => import('@/views/app-automation/test-cases/TestCaseList.vue')
      },
      {
        path: 'recorder',
        name: 'AppRecorder',
        component: () => import('@/views/ui-automation/test-cases/TestCaseManager.vue')
      },
      {
        path: 'test-cases/scenes/:id?',
        name: 'AppSceneBuilder',
        component: () => import('@/views/app-automation/test-cases/SceneBuilder.vue')
      },
      {
        path: 'elements',
        name: 'AppElements',
        component: () => import('@/views/app-automation/elements/ElementList.vue')
      },
      {
        path: 'executions',
        name: 'AppExecutions',
        component: () => import('@/views/app-automation/executions/ExecutionList.vue')
      },
      {
        path: 'reports',
        name: 'AppReports',
        component: () => import('@/views/app-automation/reports/ReportList.vue')
      },
      {
        path: 'scheduled-tasks',
        name: 'AppScheduledTasks',
        component: () => import('@/views/app-automation/scheduled-tasks/ScheduledTasks.vue')
      },
      {
        path: 'packages',
        name: 'AppPackages',
        component: () => import('@/views/app-automation/packages/PackageList.vue')
      },
      {
        path: 'pipeline',
        name: 'AppPipeline',
        component: () => import('@/views/app-automation/pipeline/PipelineRun.vue')
      },
      {
        path: 'settings',
        name: 'AppSettings',
        component: () => import('@/views/app-automation/settings/AppSettings.vue')
      },
      {
        path: 'notification',
        name: 'AppNotification',
        component: () => import('@/views/app-automation/notification/NotificationLogs.vue')
      }
    ]
  },
  {
    path: '/defects',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'list'
      },
      {
        path: 'list',
        name: 'DefectList',
        component: () => import('@/views/defects/DefectList.vue')
      },
      {
        path: 'create',
        name: 'CreateDefect',
        component: () => import('@/views/defects/DefectForm.vue')
      },
      {
        path: ':id',
        name: 'DefectDetail',
        component: () => import('@/views/defects/DefectDetail.vue')
      },
      {
        path: ':id/edit',
        name: 'EditDefect',
        component: () => import('@/views/defects/DefectForm.vue')
      },
      {
        path: 'dashboard',
        name: 'DefectDashboard',
        component: () => import('@/views/defects/DefectDashboard.vue')
      },
      {
        path: 'report',
        name: 'DefectReport',
        component: () => import('@/views/defects/DefectReport.vue')
      }
    ]
  },
  {
    path: '/data-factory',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'DataFactory',
        component: () => import('@/views/data-factory/DataFactory.vue')
      }
    ]
  },
  {
    path: '/ai-intelligent-mode',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        redirect: 'testing'
      },
      {
        path: 'testing',
        name: 'AITesting',
        component: () => import('@/views/ui-automation/ai/AITesting.vue')
      },
      {
        path: 'cases',
        name: 'AICaseList',
        component: () => import('@/views/ui-automation/ai/AICaseList.vue')
      },
      {
        path: 'execution-records',
        name: 'AIExecutionRecords',
        component: () => import('@/views/ui-automation/ai/AIExecutionRecords.vue')
      },
      {
        path: 'midscene-playground',
        name: 'MidscenePlayground',
        component: () => import('@/views/ui-automation/ai/MidscenePlayground.vue')
      },
      {
        path: 'midscene-web-playground',
        name: 'WebMidscenePlayground',
        component: () => import('@/views/ui-automation/ai/WebMidscenePlayground.vue')
      },
      {
        path: 'business-actions',
        name: 'BusinessActionList',
        component: () => import('@/views/ui-automation/ai/BusinessActionList.vue')
      },
      {
        // 元素拾取器：从设备抓元素 → 存入元素库（Android / iOS，鸿蒙预留）
        path: 'element-picker',
        name: 'ElementPicker',
        component: () => import('@/views/ui-automation/ElementPicker.vue')
      },
      {
        // APK 智能回归（MVP）：APK 上传 → REA 分析 → 人工确认 → Midscene 执行 → HTML 报告
        path: 'apk-regression',
        name: 'ApkRegression',
        component: () => import('@/views/ui-automation/ai/ApkRegression.vue')
      },
      {
        // AI 智能模式专属模型配置（独立表，与 /configuration/ai-mode 互不影响）
        path: 'model-config',
        name: 'AiModeModelConfig',
        component: () => import('@/views/ui-automation/ai/AiModeModelConfig.vue')
      }
    ]
  },
  {
    path: '/configuration',
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        component: () => import('@/views/configuration/ConfigurationCenter.vue'),
        children: [
          {
            path: '',
            redirect: 'ai-model'
          },
          {
            path: 'ai-model',
            name: 'ConfigAIModel',
            component: () => import('@/views/requirement-analysis/AIModelConfig.vue')
          },
          {
            path: 'prompt-config',
            name: 'ConfigPromptConfig',
            component: () => import('@/views/requirement-analysis/PromptConfig.vue')
          },
          {
            path: 'generation-config',
            name: 'ConfigGenerationConfig',
            component: () => import('@/views/requirement-analysis/GenerationConfigView.vue')
          },
          {
            path: 'ui-env',
            name: 'ConfigUIEnv',
            component: () => import('@/views/configuration/UIEnvironmentConfig.vue')
          },
          {
            path: 'ai-mode',
            name: 'ConfigAIMode',
            component: () => import('@/views/configuration/AIIntelligentModeConfig.vue')
          },
          {
            path: 'scheduled-task',
            name: 'ConfigScheduledTask',
            component: () => import('@/views/ui-automation/notification/NotificationConfigs.vue')
          },
          {
            path: 'dify',
            name: 'DifyConfig',
            component: () => import('@/views/configuration/DifyConfig.vue')
          }
        ]
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async (to, from, next) => {
  const userStore = useUserStore()

  console.log('路由守卫:', {
    to: to.path,
    from: from.path,
    hasToken: !!userStore.token,
    hasUser: !!userStore.user,
    isAuthenticated: userStore.isAuthenticated
  })

  // 只在应用初始化或从登录页面导航时初始化认证
  if (!userStore.user && userStore.token) {
    try {
      console.log('初始化认证...')
      await userStore.initAuth()
      console.log('认证初始化完成:', {
        hasUser: !!userStore.user,
        isAuthenticated: userStore.isAuthenticated
      })
    } catch (error) {
      console.error('认证初始化失败:', error)
    }
  }

  if (to.meta.requiresAuth && !userStore.isAuthenticated) {
    console.log('需要认证但未认证，跳转到登录页')
    next('/login')
  } else if (to.meta.requiresGuest && userStore.isAuthenticated) {
    console.log('访客页面但已认证，跳转到项目页')
    next('/home')
  } else {
    console.log('路由守卫通过，继续导航')
    next()
  }
})

router.afterEach((to, from) => {
  console.log(`Navigated from ${from.path} to ${to.path}`)
})

export default router
