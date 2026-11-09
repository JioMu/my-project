import asyncio
import json
import os
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.requirement_analysis.modao_extractor import ModaoExtractor
from apps.requirement_analysis.modao_views import _reset_modao_workflow
from apps.requirement_analysis.modao_workflow import ModaoWorkflow


class ModaoExtractionSafetyTests(SimpleTestCase):
    @staticmethod
    def _fake_workflow(extractor_result):
        class Pages:
            def all(self):
                return self

            def delete(self):
                return None

            def create(self, **_kwargs):
                return None

        class Proto:
            source_type = 'modao'
            status = 'pending'
            error_message = ''
            current_stage = 0
            work_log = ''
            extracted_json = ''
            requirement_summary = ''
            pages = Pages()

            def save(self, *args, **kwargs):
                return None

        class FakeExtractor:
            def __init__(self, *args, **kwargs):
                pass

            def extract_sync(self, **_kwargs):
                return extractor_result

        workflow = object.__new__(ModaoWorkflow)
        workflow.prototype = Proto()
        workflow.session_id = 'fusion-gate-test'
        workflow.analyzer_config = object()
        workflow.vision_config = object()
        return workflow, FakeExtractor

    def test_duplicate_screenshots_are_marked_for_audit(self):
        with tempfile.TemporaryDirectory() as media_root:
            extractor = ModaoExtractor(run_id='test-run', media_root=media_root)
            first = os.path.join(extractor.shot_dir, 'page_1.png')
            second = os.path.join(extractor.shot_dir, 'page_2.png')
            with open(first, 'wb') as fh:
                fh.write(b'same-canvas')
            with open(second, 'wb') as fh:
                fh.write(b'same-canvas')
            pages = [
                {'page_no': 1, 'screenshot': os.path.relpath(first, media_root)},
                {'page_no': 2, 'screenshot': os.path.relpath(second, media_root)},
            ]

            groups = extractor._mark_duplicate_screenshots(pages)

            self.assertEqual(groups, [[1, 2]])
            self.assertEqual(pages[1]['duplicate_of_page'], 1)
            self.assertTrue(pages[0]['screenshot_sha256'])
            self.assertIn('未计为独立有效页面', pages[1]['extraction_warnings'][0])

    @patch('apps.requirement_analysis.modao_extractor.extract_axure_text', return_value='页面需求正文')
    @patch('apps.requirement_analysis.modao_extractor.parse_axure_document')
    @patch('requests.get')
    def test_webshare_success_reaches_page_loop(self, mock_get, mock_parse, _mock_extract):
        class Response:
            status_code = 200
            encoding = 'utf-8'
            text = '<html>ok</html>'

        mock_get.return_value = Response()
        mock_parse.return_value = [{'url': 'page-one.html', 'pageName': '页面一'}]
        with tempfile.TemporaryDirectory() as media_root:
            result = ModaoExtractor(run_id='webshare-run', media_root=media_root).extract_webshare_sync(
                'https://example.invalid/prototype/'
            )

        self.assertEqual(len(result['pages']), 1)
        self.assertEqual(result['pages'][0]['page_name'], '页面一')
        self.assertEqual(result['pages'][0]['dom_text'], '页面需求正文')

    def test_dual_route_batches_keep_tail_pages(self):
        class FakeService:
            calls = []

            @classmethod
            async def call_openai_compatible_api(cls, _config, messages, max_tokens=8000):
                content = messages[-1]['content']
                cls.calls.append(content)
                page = '尾页' if '尾页' in content else '前页'
                payload = [{'source_pages': [page], 'module': page, 'feature': '功能',
                            'interaction': '', 'evidence': 'A'}]
                return {'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]}

        extractor = object.__new__(ModaoExtractor)
        blocks = ['### 前页\n' + ('甲' * 9000), '### 尾页\n' + ('乙' * 9000)]
        result = asyncio.run(extractor._dual_extract_one(FakeService, object(), blocks))

        self.assertGreaterEqual(len(FakeService.calls), 2)
        self.assertTrue(any('尾页' in call for call in FakeService.calls))
        self.assertEqual({item['module'] for item in result}, {'前页', '尾页'})

    def test_stage_one_source_uses_fusion_and_skips_duplicate_page(self):
        workflow = object.__new__(ModaoWorkflow)
        workflow.prototype = type('Proto', (), {
            'extracted_json': json.dumps({
                'fused_requirements': [{'module': '训练', 'feature': '保存模板'}],
                'pages': [
                    {'page_name': '有效页', 'text': '有效正文', 'annotations': [], 'visual_artifacts': []},
                    {'page_name': '重复页', 'text': '不得进入', 'duplicate_of_page': 1,
                     'annotations': [], 'visual_artifacts': []},
                ],
            }, ensure_ascii=False),
            'requirement_summary': '',
        })()

        source = workflow._source_text()

        self.assertIn('保存模板', source)
        self.assertIn('有效正文', source)
        self.assertNotIn('不得进入', source)

    def test_run_extract_persists_successful_fusion(self):
        result = {
            'pages': [{
                'page_no': 1, 'page_name': '训练详情', 'text': 'DOM+视觉正文',
                'dom_text': 'DOM正文', 'vision_text': '视觉正文', 'dom_text_len': 5,
                'vision_attempted': True, 'vision_success': True, 'annotations': [],
                'screenshot': '', 'visual_summary': '', 'visual_artifacts': [],
                'extraction_warnings': [],
            }],
            'warnings': [],
            'fused_requirements': [{'source_pages': ['训练详情'], 'module': '训练', 'feature': '查看详情'}],
        }
        workflow, fake_extractor = self._fake_workflow(result)

        with patch('apps.requirement_analysis.modao_extractor.ModaoExtractor', fake_extractor):
            summary = workflow.run_extract(url='https://example.invalid', dual_fusion=True)

        stored = json.loads(workflow.prototype.extracted_json)
        self.assertFalse(summary['blocked'])
        self.assertEqual(workflow.prototype.status, 'extracted')
        self.assertEqual(stored['pages'][0]['dom_text'], 'DOM正文')
        self.assertEqual(stored['fused_requirements'][0]['feature'], '查看详情')

    def test_run_extract_blocks_empty_fusion(self):
        result = {
            'pages': [{
                'page_no': 1, 'page_name': '训练详情', 'text': '正文',
                'dom_text': 'DOM正文', 'vision_text': '视觉正文', 'dom_text_len': 5,
                'vision_attempted': True, 'vision_success': True, 'annotations': [],
                'screenshot': '', 'visual_summary': '', 'visual_artifacts': [],
                'extraction_warnings': [],
            }],
            'warnings': ['双路融合失败'], 'fused_requirements': [],
        }
        workflow, fake_extractor = self._fake_workflow(result)

        with patch('apps.requirement_analysis.modao_extractor.ModaoExtractor', fake_extractor):
            summary = workflow.run_extract(url='https://example.invalid', dual_fusion=True)

        self.assertTrue(summary['blocked'])
        self.assertEqual(workflow.prototype.status, 'failed')
        self.assertIn('未生成有效需求', workflow.prototype.error_message)


class ModaoDedupAuditTests(SimpleTestCase):
    def test_vector_dedup_returns_removed_item_and_similarity(self):
        workflow = object.__new__(ModaoWorkflow)
        workflow.dedup_threshold = 0.85
        workflow._embed = lambda texts: [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
        items = [
            {'id': 'TP-1', 'title': '搜索动作', 'description': '按名称搜索动作', 'priority': 'P1', 'source': ['draft']},
            {'id': 'TP-2', 'title': '动作搜索', 'description': '根据动作名称搜索', 'priority': 'P1', 'source': ['risk']},
            {'id': 'TP-3', 'title': '删除动作', 'description': '删除自定义动作', 'priority': 'P1', 'source': ['draft']},
        ]

        kept, count, audit = workflow._vector_dedup(items)

        self.assertEqual(count, 1)
        self.assertEqual([item['id'] for item in kept], ['TP-1', 'TP-3'])
        self.assertEqual(audit[0]['kept_id'], 'TP-1')
        self.assertEqual(audit[0]['removed_id'], 'TP-2')
        self.assertGreaterEqual(audit[0]['similarity'], 0.85)


class ModaoWorkflowResetTests(SimpleTestCase):
    def test_reset_clears_stage_products_but_keeps_source(self):
        class Pages:
            deleted = False

            def all(self):
                return self

            def delete(self):
                self.deleted = True

        class Proto:
            url = 'https://modao.example/prototype'
            auth_cookie = 'kept-cookie'
            project_id = 7
            version_id = 9
            status = 'testpoints_ready'
            current_stage = 3
            extracted_json = '{"pages":[1]}'
            requirement_summary = '旧摘要'
            final_testpoints_json = '[{"title":"旧测试点"}]'
            testcases_json = '[{"title":"旧用例"}]'
            stage_confirmations = '{"stage2_confirmed":true}'
            pages = Pages()
            saved_fields = []

            def save(self, update_fields=None):
                self.saved_fields = update_fields or []

        proto = Proto()

        _reset_modao_workflow(proto)

        self.assertEqual(proto.status, 'pending')
        self.assertEqual(proto.current_stage, 0)
        self.assertEqual(proto.extracted_json, '')
        self.assertEqual(proto.final_testpoints_json, '')
        self.assertEqual(proto.testcases_json, '')
        self.assertEqual(proto.stage_confirmations, '{}')
        self.assertTrue(proto.pages.deleted)
        self.assertEqual(proto.url, 'https://modao.example/prototype')
        self.assertEqual(proto.auth_cookie, 'kept-cookie')
        self.assertEqual(proto.project_id, 7)
        self.assertEqual(proto.version_id, 9)


class ModaoQualityGateAndStateTests(SimpleTestCase):
    def test_infer_category_game_character_not_permission(self):
        from apps.requirement_analysis.modao_workflow import _infer_category
        tp = {
            'title': '点击编辑篇章跳转秘密篇章列表并携带角色上下文',
            'description': '在秘密篇章中携带当前角色信息进行展示',
            'priority': 'P0'
        }
        cat = _infer_category(tp)
        self.assertEqual(cat, 'main_flow')

    def test_quality_gate_c3_heals_when_p0_classified_as_ui(self):
        workflow = object.__new__(ModaoWorkflow)
        workflow.prototype = type('Proto', (), {'id': 1})()
        tps = [
            {'module': '个人中心', 'title': '个人中心创作Tab角色列表展示', 'priority': 'P0', 'category': 'UI', 'description': 'x' * 100},
        ]
        cases = [
            {'id': 'TC-1', 'module': '个人中心', 'title': '用例1', 'precondition': '前置', 'steps': ['步骤1：进入个人中心', '步骤2：查看角色列表'], 'expected': '预期', 'priority': 'P0', 'type': '功能测试'}
        ]
        res = workflow._code_quality_check(cases, {'smoke_testcases': [{'id': 'TC-1'}]}, tps)
        c3 = res['quality_json']['quality_report']['checks']['C3']
        self.assertEqual(c3['result'], 'PASS')
        self.assertEqual(tps[0]['category'], 'main_flow')
        self.assertEqual(res['blocking'], [])


