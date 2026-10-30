# -*- coding: utf-8 -*-
"""APK 回归：新增 abort_requested 字段（人工终止真正生效）。

背景：原先「终止执行」只把 status 落成 failed 并强杀容器内 Node 进程，
但执行线程是同步阻塞的，杀完还会继续跑下一条路径 —— 表现为
"点了终止，手机还在动"。

本字段作为跨线程信号：执行线程每跑完一条路径 / 每次执行前回查，
置位就 break 并停止剩余路径。

注意：本仓库 0001_initial 是空占位，docker_start.py 启动时 migrate --fake，
**真正加列依靠 docker_start.py 里的幂等 ALTER**；本迁移用于保持 Django
迁移状态与模型一致。两边必须同步。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0017_ai_mode_model_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='apkregressionrun',
            name='abort_requested',
            field=models.BooleanField(
                default=False,
                help_text='用户点了「终止执行」后置位；执行线程在下一条路径开始前读取并停止',
                verbose_name='已请求中止',
            ),
        ),
    ]
