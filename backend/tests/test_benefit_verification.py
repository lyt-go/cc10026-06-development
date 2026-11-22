"""批量查询收益核验测试：成对样本、超时缺对照、版本修改后重验。

覆盖的核验口径：
1. 短查询有成对实测 → 收益达线，并说明覆盖的数据范围；
2. 长查询超时缺对照 → 不达线，且不用预估收益补入；
3. 补齐实测后可重新核算；
4. 修改计划版本后旧样本不再适用，需要重新核验；
5. 两轮执行版本可核对。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('DATA_ROOT', tempfile.mkdtemp(prefix='benefit_test_'))


class TestBenefitVerification:
    def setup_method(self):
        import copy
        import tempfile as _tempfile
        from pathlib import Path

        from app.seed import SEED_ROWS
        from app.store import store
        from app.services import data_manager

        # 深拷贝：样本记录/运行明细是嵌套列表，浅拷贝会让用例之间互相污染。
        store._tables = {
            name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()}
        self.data_root = _tempfile.mkdtemp(prefix='benefit_case_')
        data_manager.DATA_ROOT = Path(self.data_root)

        from app.services import service_for
        self.opt = service_for('query_optimize')
        self.batch = service_for('batch_query')

    def teardown_method(self):
        shutil.rmtree(self.data_root, ignore_errors=True)

    def test_short_query_pairs_reach_threshold_with_scope(self):
        """同一批次里短查询成对达线，长查询缺对照不入账，报告说明数据范围。"""
        report = self.opt.verify_plan('QUER-0003')

        assert report['ok'] is True
        assert report['pair_count'] == 2
        assert report['measured_benefit'] is not None
        assert report['measured_benefit'] >= report['threshold']
        assert report['passed'] is True
        assert report['data_scope']['covered_query_classes'] == ['短查询']
        assert report['data_scope']['missing_query_classes'] == ['长查询']
        assert '仅覆盖短查询' in report['message']
        assert '长查询未纳入' in report['message']
        assert all(pair['baseline_latency_ms'] > pair['optimized_latency_ms']
                   for pair in report['pairs'])
        # 达线规则仍要说明可用的数据范围，且证据只来自成对样本。
        assert report['data_scope']['paired_samples'] == 2
        assert report['data_scope']['total_samples'] == 4

    def test_long_query_without_pair_is_excluded_not_estimated(self):
        """长查询只有旧计划耗时/超时：不得用预估值补入。"""
        report = self.opt.verify_plan('QUER-0002')

        assert report['pair_count'] == 0
        assert report['measured_benefit'] is None
        assert report['passed'] is False
        reasons = {item['reason'] for item in report['excluded']}
        assert '超时缺少对照数据' in reasons
        assert '缺少实际计划版本v2的实测耗时' in reasons
        assert report['data_scope']['missing_query_classes'] == ['长查询']
        assert '不以预估收益补算' in report['message']

    def test_plan_without_pairs_has_no_estimated_benefit(self):
        """没有成对样本时，计划收益基数为待核验，不回退预估比例。"""
        # 未核验前：计划只报「待核验」，不把「预估30%」当成可用收益。
        before = self.opt.get_optimization_plan('订单汇总查询')
        assert before['benefit_basis'] == '待核验'
        assert before['measured_benefit'] is None
        assert before['estimated_benefit'] == '预估30%'

        self.opt.verify_plan('QUER-0003')
        plan = self.opt.get_optimization_plan('订单汇总查询')
        assert plan['benefit_basis'] == '实测核验'
        assert plan['measured_benefit'] is not None
        assert plan['measured_benefit'] != 0.30

        detail = self.opt.get_plan_for_api('明细导出查询')
        assert detail['plan_version'] == 'v2'
        assert self.opt.estimate_improvement('明细导出查询') == 0.0

    def test_missing_measurement_can_be_filled_then_reverified(self):
        """补齐长查询实测后，同一批查询可重新核验。"""
        before = self.opt.verify_plan('QUER-0002')
        assert before['pair_count'] == 0

        result = self.batch.record_run(2, '明细/超时', 'v2', latency_ms=900.0)
        assert result['ok'] is True
        self.batch.record_run(2, '明细/大表', 'v2', latency_ms=1100.0)

        after = self.opt.verify_plan('QUER-0002')
        assert after['pair_count'] == 2
        assert after['measured_benefit'] is not None
        assert after['data_scope']['covered_query_classes'] == ['长查询']

    def test_revise_plan_requires_reverification(self):
        """修改计划后旧版本的核验样本不再计入新版本。"""
        first = self.opt.verify_plan('QUER-0002')
        assert first['plan_version'] == 'v2'

        entry, message = self.opt.revise_plan(2, changes={'索引建议': 'idx_detail_dt_order_v3'})
        assert entry['规则版本'] == 'v3'
        assert '重新核验' in message

        second = self.opt.verify_plan('QUER-0002')
        assert second['plan_version'] == 'v3'
        assert second['pair_count'] == 0
        assert any('缺少实际计划版本v3' in item['reason'] for item in second['excluded'])
        # 旧版本报告仍可追溯
        assert self.opt.read_verification('QUER-0002', 'v2')['pair_count'] == 0

    def test_batch_binds_plan_and_keeps_latency_detail(self):
        """执行前绑定计划版本，执行后保留延迟明细与实际版本。"""
        self.opt.verify_plan('QUER-0003')
        result = self.batch.execute_batch(1)

        assert result['ok'] is True
        assert result['plan_version'] == 'v2'
        assert len(result['latency_detail']) == 4
        for detail in result['latency_detail']:
            assert detail['plan_version'] == 'v2'
            assert detail['query_key']
            assert detail['input_scale']
            assert detail['api'] == '订单汇总查询'

        # 实测收益只覆盖短查询；长查询在核验范围外，保持待核验口径。
        by_key = {d['query_key']: d for d in result['latency_detail']}
        assert by_key['汇总/华东']['benefit_basis'] == '实测核验'
        assert by_key['明细/华东']['benefit_basis'] == '待核验'

        entry = self.batch.get_entry(1)
        assert entry['计划版本'] == 'v2'
        assert entry['计划绑定']['rule_no'] == 'QUER-0003'
        rounds = self.batch.batch_rounds(1)
        assert rounds and rounds[0]['latency_detail']
        assert {d['query_key'] for d in rounds[0]['latency_detail']} == {
            '汇总/华东', '汇总/华南', '明细/华东', '明细/超时'}

    def test_second_round_uses_same_queries_and_records_version(self):
        """运营再次触发同一批查询：两轮版本可核对，样本仍成对。"""
        self.batch.execute_batch(1, plan_version='v2')
        self.batch.execute_batch(1, plan_version='v1')

        comparison = self.batch.compare_rounds(1)
        assert comparison['has_contrast'] is True
        assert set(comparison['versions']) == {'v1', 'v2'}
        assert all(round_['request_id'] for round_ in comparison['rounds'])

        # 两轮执行后仍只保留同一查询、同一输入规模的两条样本
        samples = self.batch.verification_samples('订单汇总查询')
        keys = {(s['query_key'], s['input_scale']) for s in samples}
        assert len(samples) == len(keys)

    def test_timeout_run_keeps_only_measured_side(self):
        """超时查询的样本只保留已测一侧，不填预估值。"""
        self.batch.execute_batch(
            2,
            plan_version='v2',
            simulated_latencies={'明细/大表/2026-10-02': 40000, '明细/超时/2026-10-02': 40000},
        )
        samples = self.batch.verification_samples('明细导出查询')
        timeout_sample = next(s for s in samples if s['query_key'] == '明细/超时')
        runs = {run['plan_version']: run for run in timeout_sample['runs']}
        assert runs['v1']['latency_ms'] == 1200.0
        assert runs['v2']['status'] == 'timeout'
        assert runs['v2']['latency_ms'] is None
        assert runs['v2']['measured'] is False

    def test_verification_report_is_archived_per_version(self):
        """核验报告按规则+版本存档，可重复读取。"""
        self.opt.verify_plan('QUER-0003')
        archived = self.opt.read_verification('QUER-0003', 'v2')
        assert archived['rule_no'] == 'QUER-0003'
        assert archived['plan_version'] == 'v2'
        assert archived['passed'] is True
        assert archived['data_scope']['paired_samples'] == 2

    def test_multi_run_sample_uses_median(self):
        """同一批次多次实测按中位数对照，样本不因多次执行重复计数。"""
        report = self.opt.verify_plan('QUER-0003')
        multi = next(p for p in report['pairs'] if p['query_key'] == '汇总/华东')
        # v1 实测 420/430 → 425；v2 实测 280/290 → 285
        assert multi['baseline_latency_ms'] == 425.0
        assert multi['optimized_latency_ms'] == 285.0
        assert multi['run_count'] == 4
        assert report['pair_count'] == 2

    def test_input_scale_mismatch_cannot_borrow_other_scale(self):
        """只有一侧实测时，不能借用另一个输入规模的耗时凑成对照。"""
        samples = self.batch.get_entry(1)['实测样本记录']
        samples.append({
            '样本编号': 'SMP-9999',
            'query_key': '汇总/华东',
            'query_text': '汇总/华东/2026-11-02',
            'query_class': '短查询',
            'input_scale': 9999,
            'runs': [
                {'run_id': 'B9999-1', 'plan_version': 'v2', 'latency_ms': 100.0,
                 'status': 'success', 'measured': True},
            ],
        })
        report = self.opt.verify_plan('QUER-0003')

        # 9999 规模只有 v2 一侧，不能拿 200 规模的 v1 耗时补成对。
        assert {p['input_scale'] for p in report['pairs']} == {200}
        excluded = {item['input_scale']: item['reason'] for item in report['excluded']}
        assert '对照' in excluded[9999]
        # 收益只由 200 规模的两组成对样本决定。
        assert report['pair_count'] == 2

    def test_samples_from_other_api_are_not_counted(self):
        """核验只取本API的样本，其他API的实测不参与。"""
        # 把另一个API的批量任务样本改成极低耗时，确认不会影响本规则的结论。
        for row in self.batch.list_entries(page=1, size=100)[0]:
            if row['关联API'] == '明细导出查询':
                for sample in row.get('实测样本记录') or []:
                    sample['input_scale'] = 200
                    for run in sample['runs']:
                        run['latency_ms'] = 10.0
                        run['measured'] = True

        report = self.opt.verify_plan('QUER-0003')
        assert report['api'] == '订单汇总查询'
        assert report['pair_count'] == 2
        assert all(p['optimized_latency_ms'] > 100 for p in report['pairs'])
