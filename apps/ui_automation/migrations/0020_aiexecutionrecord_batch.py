"""给执行记录加「多设备并行批次」与「执行设备名称」字段。

背景（2026-10-29 用户反馈"好像输出两条报告"）：
    多选 N 台设备并行执行同一用例时，后端会为每台各建一条执行记录
    （这是设计如此：各自有日志/截图/模型消耗）。但旧实现只把批次信息
    拼进 **日志文本**（`本批第 1/2 台（批次 xxx）`），列表页拿不到，
    用户看到"同一用例、几乎同一时刻的两条记录"，第一反应是重复执行。

落库后：
    - 列表可标「第 1/2 台 · RMX2117」并说明是并行批次，不再像重复；
    - device_name 是快照，设备改名/删除后仍能看出跑在哪台机器上。

⚠️ 用 RunSQL + information_schema 判断，不用 AddField ——
   本项目的迁移历史里 AddField 会因模型不在 state 里报
   `KeyError: ('ui_automation', 'aiexecutionrecord')`。
⚠️ RunSQL 一次只执行一条语句，所以拆成独立操作逐个执行。
"""
from django.db import migrations


def _add_columns(apps, schema_editor):
    """按 information_schema 判断后逐条 ALTER（幂等，可重复执行）。"""
    from django.db import connection

    targets = [
        ('batch_id',
         'ALTER TABLE ui_ai_execution_records '
         "ADD COLUMN batch_id VARCHAR(32) NOT NULL DEFAULT ''"),
        ('batch_index',
         'ALTER TABLE ui_ai_execution_records '
         'ADD COLUMN batch_index INT NOT NULL DEFAULT 0'),
        ('batch_total',
         'ALTER TABLE ui_ai_execution_records '
         'ADD COLUMN batch_total INT NOT NULL DEFAULT 0'),
        ('device_name',
         'ALTER TABLE ui_ai_execution_records '
         "ADD COLUMN device_name VARCHAR(120) NOT NULL DEFAULT ''"),
    ]
    with connection.cursor() as cur:
        for col, ddl in targets:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'ui_ai_execution_records' "
                "AND column_name = %s",
                (col,),
            )
            if cur.fetchone():
                continue
            cur.execute(ddl)

        # batch_id 索引（按批次聚合查询用）。索引名固定，避免重复 ADD 报 1061。
        cur.execute(
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'ui_ai_execution_records' "
            "AND index_name = 'idx_ai_exec_batch'"
        )
        if not cur.fetchone():
            cur.execute(
                'ALTER TABLE ui_ai_execution_records '
                'ADD INDEX idx_ai_exec_batch (batch_id)')


def _noop(apps, schema_editor):
    """回滚不删列：老代码忽略多余列，保留数据比删干净更重要。"""


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0019_ui_project_popup_policy'),
    ]

    operations = [
        migrations.RunPython(_add_columns, _noop),
    ]
