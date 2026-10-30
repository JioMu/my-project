# -*- coding: utf-8 -*-
"""项目级弹窗策略：UiProject 新增 auto_dismiss_popup / popup_precheck。

背景（2026-10-29）：权限请求 / 隐私协议 / 开屏广告几乎每个 App 冷启动都会弹，
原来只能靠环境变量（要重启容器）或逐条用例配置（必然漏配）。
改为项目级配置一次长期有效；用例级 AICase.auto_dismiss_popup 仍可覆盖。

⚠️ 关于「预授权」：曾打算用 `adb shell pm grant` 让权限框根本不弹，但实测
RMX2117(Android 13+) 上被 ROM 拒绝（缺 GRANT_RUNTIME_PERMISSIONS /
MANAGE_APP_OPS_MODES），所以弹窗仍需"弹出来再点允许"。

注意：本仓库 0001_initial 是空占位，docker_start.py 启动时 migrate --fake，
**真正加列依靠 docker_start.py 里的幂等 ALTER**；本迁移用于保持 Django
迁移状态与模型一致。两边必须同步。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0018_apk_abort_flag'),
    ]

    operations = [
        migrations.AddField(
            model_name='uiproject',
            name='auto_dismiss_popup',
            field=models.BooleanField(
                default=True,
                help_text='执行时遇到权限请求/协议/广告等弹窗自动关闭并重试该步骤',
                verbose_name='自动处理弹窗',
            ),
        ),
        migrations.AddField(
            model_name='uiproject',
            name='popup_precheck',
            field=models.BooleanField(
                default=True,
                help_text='启动 App 后先清一轮弹窗再开始执行，不等某一步失败才补救',
                verbose_name='执行前预检弹窗',
            ),
        ),
    ]
