# -*- coding: utf-8 -*-
"""AI 智能模式专属模型配置（ui_ai_mode_model_configs）。

注意：本仓库的 0001_initial 是空占位，docker_start.py 启动时 migrate --fake，
**真正建表依靠 docker_start.py 里的幂等 DDL**。本迁移的作用是：
  1) 让 Django 的迁移状态与模型定义保持一致（避免 makemigrations 反复报差异）；
  2) 在能正常跑迁移的环境里也能建出这张表。
两边字段必须保持同步。
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0016_apk_regression'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AiModeModelConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='配置名称')),
                ('role', models.CharField(
                    choices=[('analysis', '分析与编排（文本推理）'),
                             ('vision', '真机执行（视觉多模态）')],
                    db_index=True, max_length=20, verbose_name='用途角色')),
                ('model_type', models.CharField(
                    choices=[('deepseek', 'DeepSeek'), ('qwen', '通义千问 (Qwen)'),
                             ('openai', 'OpenAI'), ('azure_openai', 'Azure OpenAI'),
                             ('anthropic', 'Anthropic'), ('google_gemini', 'Google Gemini'),
                             ('siliconflow', '硅基流动 (SiliconFlow)'),
                             ('other', '其他 (OpenAI 兼容)')],
                    default='qwen', max_length=20, verbose_name='模型提供商')),
                ('model_name', models.CharField(
                    help_text='如 deepseek-chat / qwen3.7-plus',
                    max_length=100, verbose_name='模型名称')),
                ('api_key', models.CharField(blank=True, default='', max_length=300,
                                             verbose_name='API Key')),
                ('base_url', models.CharField(
                    blank=True, default='', max_length=300, verbose_name='API Base URL',
                    help_text='OpenAI 兼容地址，如 https://dashscope.aliyuncs.com/compatible-mode/v1')),
                ('model_family', models.CharField(
                    blank=True, default='', max_length=50, verbose_name='Midscene 模型家族',
                    help_text='仅视觉角色用：midscene 的 MIDSCENE_MODEL_FAMILY，'
                              '如 qwen3-vl / doubao-vision / gemini / glm-v。留空则按提供商自动推断')),
                ('max_tokens', models.IntegerField(default=4096, verbose_name='最大 Token 数')),
                ('temperature', models.FloatField(default=0.3, verbose_name='温度参数')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('notes', models.CharField(blank=True, default='', max_length=300,
                                           verbose_name='备注')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ai_mode_model_configs', to=settings.AUTH_USER_MODEL,
                    verbose_name='创建者')),
            ],
            options={
                'verbose_name': 'AI 智能模式模型配置',
                'verbose_name_plural': 'AI 智能模式模型配置',
                'db_table': 'ui_ai_mode_model_configs',
                'ordering': ['role', '-is_active', '-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='aimodemodelconfig',
            index=models.Index(fields=['role', 'is_active'], name='idx_aimode_role_active'),
        ),
    ]
