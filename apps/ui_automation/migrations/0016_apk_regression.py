# -*- coding: utf-8 -*-
"""APK 智能回归 MVP：APK 制品 + 运行记录两张表。

注意：本项目 `0001_initial` 是空占位、库内表早已存在，容器启动时
`docker_start.py` 会 `migrate --fake` 全部迁移（不执行 SQL），
因此**真正建表依赖 `docker_start.py` 里的幂等 DDL**（见
`CREATE TABLE IF NOT EXISTS ui_apk_artifacts / ui_apk_regression_runs`）。
本文件保证 `makemigrations --check` 与 `migrate` 的图状态一致。
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0015_aiexecutionrecord_replay_degraded'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ApkArtifact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, default='', max_length=200,
                                          verbose_name='展示名称')),
                ('file', models.FileField(upload_to='apk/%Y/%m/', verbose_name='APK 文件')),
                ('original_name', models.CharField(blank=True, default='', max_length=255,
                                                   verbose_name='原始文件名')),
                ('size', models.BigIntegerField(default=0, verbose_name='文件大小(字节)')),
                ('sha256', models.CharField(blank=True, db_index=True, default='', max_length=64,
                                            verbose_name='SHA-256')),
                ('package_name', models.CharField(blank=True, db_index=True, default='',
                                                  max_length=200, verbose_name='包名')),
                ('version_name', models.CharField(blank=True, default='', max_length=50,
                                                  verbose_name='版本名')),
                ('version_code', models.IntegerField(blank=True, null=True, verbose_name='版本号')),
                ('min_sdk', models.IntegerField(blank=True, null=True, verbose_name='minSdk')),
                ('target_sdk', models.IntegerField(blank=True, null=True, verbose_name='targetSdk')),
                ('launch_activity', models.CharField(blank=True, default='', max_length=300,
                                                     verbose_name='启动 Activity')),
                ('manifest_info', models.JSONField(blank=True, default=dict,
                                                   verbose_name='清单解析结果')),
                ('parse_error', models.CharField(blank=True, default='', max_length=500,
                                                 verbose_name='解析失败原因')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='上传时间')),
                ('app_config', models.ForeignKey(blank=True, null=True,
                                                 on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name='apk_artifacts',
                                                 to='ui_automation.appconfig',
                                                 verbose_name='关联应用配置')),
                ('created_by', models.ForeignKey(blank=True, null=True,
                                                 on_delete=django.db.models.deletion.SET_NULL,
                                                 to=settings.AUTH_USER_MODEL,
                                                 verbose_name='上传人')),
            ],
            options={
                'verbose_name': 'APK 制品',
                'verbose_name_plural': 'APK 制品',
                'db_table': 'ui_apk_artifacts',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ApkRegressionRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('uploaded', '已上传'), ('analyzing', '分析中'),
                             ('analyzed', '待确认'), ('analyzing_failed', '分析失败'),
                             ('confirmed', '已确认'), ('running', '执行中'),
                             ('done', '已完成'), ('failed', '执行失败'),
                             ('cancelled', '已取消')],
                    default='uploaded', max_length=20, verbose_name='状态')),
                ('rea_analysis', models.JSONField(blank=True, default=dict,
                                                  verbose_name='REA 分析结果')),
                ('paths', models.JSONField(blank=True, default=list, verbose_name='测试路径清单')),
                ('confirmation', models.JSONField(blank=True, default=dict, verbose_name='人工确认')),
                ('results', models.JSONField(blank=True, default=list, verbose_name='执行结果')),
                ('log', models.TextField(blank=True, default='', verbose_name='运行日志')),
                ('summary', models.JSONField(blank=True, default=dict, verbose_name='结果摘要')),
                ('report_path', models.CharField(blank=True, default='', max_length=500,
                                                 verbose_name='HTML 报告路径')),
                ('model_calls', models.IntegerField(default=0, verbose_name='模型调用次数')),
                ('prompt_tokens', models.IntegerField(default=0, verbose_name='输入 token')),
                ('completion_tokens', models.IntegerField(default=0, verbose_name='输出 token')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='开始时间')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='结束时间')),
                ('duration', models.FloatField(default=0, verbose_name='耗时(秒)')),
                ('artifact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name='runs',
                                               to='ui_automation.apkartifact',
                                               verbose_name='APK')),
                ('created_by', models.ForeignKey(blank=True, null=True,
                                                 on_delete=django.db.models.deletion.SET_NULL,
                                                 to=settings.AUTH_USER_MODEL,
                                                 verbose_name='创建人')),
                ('device', models.ForeignKey(blank=True, null=True,
                                             on_delete=django.db.models.deletion.SET_NULL,
                                             related_name='apk_runs',
                                             to='ui_automation.appdevice',
                                             verbose_name='执行设备')),
                ('project', models.ForeignKey(blank=True, null=True,
                                              on_delete=django.db.models.deletion.SET_NULL,
                                              related_name='apk_runs',
                                              to='ui_automation.uiproject',
                                              verbose_name='所属项目')),
            ],
            options={
                'verbose_name': 'APK 智能回归运行',
                'verbose_name_plural': 'APK 智能回归运行',
                'db_table': 'ui_apk_regression_runs',
                'ordering': ['-created_at'],
            },
        ),
    ]
