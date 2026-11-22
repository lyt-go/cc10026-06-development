"""查询优化领域服务：管理查询优化规则、索引建议和预聚合策略。

跨模块数据流：
- 消费 api_registry 快照 → 关联API加载优化规则
- 消费 cache_layer 快照 → 缓存命中率影响优化策略
- 消费 query_route 快照 → 路由权重影响并行策略
- 消费 batch_query 的收益核验样本记录 → 用同类对照查询的实测耗时核算收益
- 优化结果 → 写共享快照供 batch_query 消费

收益核验口径：
- 只接受同一查询、同一输入规模、同一实际计划版本下成对出现的实测样本；
- 超时或缺对照的样本单独列出，不使用预估收益补算；
- 报告按「规则编号 + 实际计划版本」存档，修改计划版本后必须重新核验。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 默认收益阈值：规则未单独配置时，实测收益达到 30% 视为达线。
DEFAULT_BENEFIT_THRESHOLD = 0.30
THRESHOLD_FIELD = "收益阈值"
SUCCESS_RUN_STATUSES = {"success", "成功", "已完成"}
TIMEOUT_RUN_STATUSES = {"timeout", "超时", "timed_out"}


def _parse_percent(value: Any) -> float | None:
    """把「30%」「0.3」这类写法统一成小数；无法解析时返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100.0
        return float(text)
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent_text(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


class QueryOptimizeService(BaseModuleService):
    MODULE = "query_optimize"
    ENTITY = "优化规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "关联API", "索引建议", "分区裁剪", "预聚合", "并行策略", "优化收益", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "关联API", "索引建议"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---------- 规则/计划读取 ----------

    def find_rule(self, rule_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("规则编号") == rule_no:
                return row
        return None

    def find_rule_for_api(self, api_name: str) -> dict[str, Any] | None:
        """查找API对应的规则：优先已生效，其次待校验/草稿的候选规则。"""
        rows = [row for row in store.rows(self.MODULE) if row.get("关联API") == api_name]
        for row in rows:
            if row.get("status") == "已生效":
                return row
        return rows[0] if rows else None

    def get_plan_for_api(self, api_name: str) -> dict[str, Any]:
        """返回纳入评估的优化计划（含尚未生效的候选规则）。

        与 get_optimization_plan 的区别：后者只认已生效规则，代表生产实际
        执行的计划；这里用于收益核验场景，报表团队准备采用的规则也可以
        先按自己的版本跑对照实测。
        """
        row = self.find_rule_for_api(api_name)
        if row is None:
            return {}
        return {
            "rule_no": row.get("规则编号"),
            "index_hint": row.get("索引建议"),
            "partition_prune": row.get("分区裁剪"),
            "pre_aggregation": row.get("预聚合"),
            "parallel_strategy": row.get("并行策略"),
            "estimated_benefit": row.get("优化收益"),
            "plan_version": str(row.get("规则版本") or ""),
            "rule_status": row.get("status"),
        }

    def get_optimization_plan(self, api_name: str) -> dict[str, Any]:
        """获取指定API当前实际生效的优化计划。

        收益只引用已存档的实测核验结论；没有成对实测样本时返回
        benefit_basis=待核验，调用方不得用预估值补算延迟。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                version = str(row.get("规则版本") or "")
                report = self.read_verification(row.get("规则编号", ""), version)
                measured = report.get("measured_benefit") if report else None
                return {
                    "rule_no": row.get("规则编号"),
                    "index_hint": row.get("索引建议"),
                    "partition_prune": row.get("分区裁剪"),
                    "pre_aggregation": row.get("预聚合"),
                    "parallel_strategy": row.get("并行策略"),
                    "estimated_benefit": row.get("优化收益"),
                    "plan_version": version,
                    "benefit_basis": "实测核验" if measured is not None else "待核验",
                    "measured_benefit": measured,
                    "verification_passed": bool(report.get("passed")),
                    "verification_scope": report.get("data_scope", {}) if report else {},
                }
        return {}

    def estimate_improvement(self, api_name: str) -> float:
        """返回实测核验收益；没有成对样本时返回 0，不退回预估比例。"""
        plan = self.get_optimization_plan(api_name)
        if not plan:
            return 0.0
        measured = plan.get("measured_benefit")
        return float(measured) if measured is not None else 0.0

    def threshold_for(self, rule: dict[str, Any]) -> float:
        parsed = _parse_percent(rule.get(THRESHOLD_FIELD))
        if parsed is None:
            parsed = DEFAULT_BENEFIT_THRESHOLD
        return parsed

    def read_verification(self, rule_no: str, plan_version: str) -> dict[str, Any]:
        from app.services.data_manager import read_benefit_verification
        return read_benefit_verification(rule_no, plan_version)

    # ---------- 收益核验 ----------

    def verify_plan(self, rule_no: str, threshold: float | None = None) -> dict[str, Any]:
        """用批量查询收益核验样本核算规则收益，并按计划版本存档报告。

        每个比较样本必须同时满足：
        1. 同一查询（query_key）；
        2. 同一输入规模（input_scale）；
        3. 样本声明的计划版本等于规则当前实际版本，且运行记录带同一版本；
        4. 对照版本与优化版本都有成功实测耗时。

        缺任一项的样本进入 excluded，不用预估收益补入。
        """
        rule = self.find_rule(rule_no)
        if rule is None:
            return {"ok": False, "message": f"优化规则 {rule_no} 不存在"}

        api_name = str(rule.get("关联API") or "")
        plan_version = str(rule.get("规则版本") or "")
        if not plan_version:
            return {"ok": False, "message": f"规则 {rule_no} 未标注实际计划版本，无法核验"}

        threshold = threshold if threshold is not None else self.threshold_for(rule)
        samples = self._load_samples(api_name)

        pairs: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        baseline_versions: set[str] = set()

        for sample in samples:
            base = {
                "sample_id": sample.get("样本编号"),
                "query_key": sample.get("query_key") or sample.get("查询键"),
                "query_class": sample.get("query_class") or sample.get("查询类别") or "未分类",
                "input_scale": sample.get("input_scale") or sample.get("输入规模"),
            }
            reason = self._sample_gap(sample, plan_version)
            if reason:
                excluded.append({**base, "reason": reason})
                continue

            baseline_version = self._baseline_version(sample, plan_version)
            baseline_versions.add(baseline_version)
            runs = sample.get("runs") or []
            baseline_latency = self._median_run_latency(runs, baseline_version)
            optimized_latency = self._median_run_latency(runs, plan_version)

            if baseline_latency is None:
                excluded.append({**base, "reason": "缺少对照版本实测耗时"})
                continue
            if optimized_latency is None:
                excluded.append({**base, "reason": self._missing_reason(runs, plan_version)})
                continue
            if optimized_latency <= 0:
                excluded.append({**base, "reason": "实测耗时无效"})
                continue

            benefit = (baseline_latency - optimized_latency) / baseline_latency
            pairs.append({
                **base,
                "baseline_plan_version": baseline_version,
                "plan_version": plan_version,
                "baseline_latency_ms": round(baseline_latency, 2),
                "optimized_latency_ms": round(optimized_latency, 2),
                "benefit": round(benefit, 4),
                "benefit_text": _percent_text(benefit),
                "run_count": len([
                    run for run in runs
                    if str(run.get("plan_version")) in (baseline_version, plan_version)
                    and self._run_latency(run) is not None
                ]),
            })

        measured_benefit = (
            round(sum(pair["benefit"] for pair in pairs) / len(pairs), 4) if pairs else None)
        passed = bool(pairs) and measured_benefit is not None and measured_benefit >= threshold
        data_scope = self._build_scope(samples, pairs, excluded, plan_version)
        message = self._build_message(rule_no, measured_benefit, threshold, passed, data_scope)

        report = {
            "ok": True,
            "rule_no": rule_no,
            "api": api_name,
            "plan_version": plan_version,
            "baseline_plan_versions": sorted(baseline_versions),
            "threshold": threshold,
            "threshold_text": _percent_text(threshold),
            "measured_benefit": measured_benefit,
            "measured_benefit_text": _percent_text(measured_benefit),
            "passed": passed,
            "pair_count": len(pairs),
            "sample_count": len(samples),
            "pairs": pairs,
            "excluded": excluded,
            "data_scope": data_scope,
            "message": message,
            "verified_at": self._now_iso(),
            "evidence_batches": sorted({
                str(sample.get("_batch_no")) for sample in samples if sample.get("_batch_no")}),
        }

        # 存档：同一规则同一版本可重复核验，本次结论覆盖上次，但版本不同则各自留档。
        from app.services.data_manager import write_benefit_verification
        write_benefit_verification(rule_no, plan_version, report)

        # 规则行只记录核验结果，预估字段保留原值但不参与收益核算。
        rule["核验收益"] = report["measured_benefit_text"]
        rule["核验状态"] = self._verification_state(report)
        rule["核验时间"] = report["verified_at"]
        return report

    def _verification_state(self, report: dict[str, Any]) -> str:
        if not report.get("pair_count"):
            return "证据不足"
        if not report.get("passed"):
            return "未达阈值"
        scope = report.get("data_scope", {})
        if scope.get("missing_query_classes"):
            return "已达阈值（数据范围受限）"
        return "已达阈值"

    def _load_samples(self, api_name: str) -> list[dict[str, Any]]:
        """从批量查询模块读取该API的收益核验样本记录。"""
        from app.services import service_for
        batch_svc = service_for("batch_query")
        if batch_svc is None or not hasattr(batch_svc, "verification_samples"):
            return []
        return batch_svc.verification_samples(api_name)

    def _sample_gap(self, sample: dict[str, Any], plan_version: str) -> str:
        """返回样本不可用于本次核验的原因；可用于核验时返回空串。"""
        query_key = sample.get("query_key") or sample.get("查询键")
        input_scale = sample.get("input_scale") or sample.get("输入规模")

        if not query_key:
            return "缺少查询标识，无法确认同类查询"
        if not input_scale:
            return "缺少输入规模，无法确认同类对照"
        runs = sample.get("runs") or []
        if not runs:
            return "缺少实测运行记录"

        # 样本若显式声明了计划版本，必须与本次核验的版本一致。
        declared = str(sample.get("计划版本") or "")
        if declared and declared != plan_version:
            return f"计划版本不一致（样本为{declared}，当前计划为{plan_version}）"

        known_versions = {
            str(run.get("plan_version")) for run in runs if run.get("plan_version")}
        if plan_version not in known_versions:
            return f"缺少实际计划版本{plan_version}的实测耗时"
        baseline = self._baseline_version(sample, plan_version)
        if not baseline:
            return "缺少对照计划版本"
        if baseline not in known_versions:
            return f"缺少对照版本{baseline}的实测耗时"
        return ""

    @staticmethod
    def _baseline_version(sample: dict[str, Any], plan_version: str) -> str:
        """对照版本：样本显式声明的版本，或运行记录里除当前版本外的实测版本。"""
        declared = str(sample.get("对照计划版本") or "")
        if declared:
            return declared
        for run in sample.get("runs") or []:
            version = str(run.get("plan_version") or "")
            if version and version != plan_version:
                return version
        return ""

    @staticmethod
    def _run_latency(run: dict[str, Any]) -> float | None:
        if str(run.get("status", "success")) not in SUCCESS_RUN_STATUSES:
            return None
        # measured=False 表示该耗时由计划收益推算，不能当作核验证据。
        if run.get("measured") is False:
            return None
        return _safe_float(run.get("latency_ms"))

    def _median_run_latency(self, runs: list[dict[str, Any]], version: str) -> float | None:
        values = [
            latency for run in runs
            if str(run.get("plan_version")) == version
            for latency in [self._run_latency(run)]
            if latency is not None
        ]
        return median(values) if values else None

    @staticmethod
    def _missing_reason(runs: list[dict[str, Any]], plan_version: str) -> str:
        has_timeout = any(
            str(run.get("plan_version")) == plan_version
            and str(run.get("status", "")).lower() in TIMEOUT_RUN_STATUSES
            for run in runs
        )
        return "超时缺少对照数据" if has_timeout else "缺少实际计划版本的实测耗时"

    def _build_scope(
        self,
        samples: list[dict[str, Any]],
        pairs: list[dict[str, Any]],
        excluded: list[dict[str, Any]],
        plan_version: str,
    ) -> dict[str, Any]:
        covered_classes = sorted({pair["query_class"] for pair in pairs})
        missing_classes = sorted({item["query_class"] for item in excluded} - set(covered_classes))
        scales = sorted({str(pair["input_scale"]) for pair in pairs})
        coverage = round(len(pairs) / len(samples), 4) if samples else 0.0
        if pairs:
            scope_text = (
                f"达线/核算结论仅覆盖{'、'.join(covered_classes) or '未分类'}的"
                f"{len(pairs)}组成对样本（输入规模：{'、'.join(scales)}）"
            )
        else:
            scope_text = "没有可用的成对实测样本"
        if missing_classes:
            scope_text += f"；{'、'.join(missing_classes)}未纳入"
        return {
            "covered_query_classes": covered_classes,
            "missing_query_classes": missing_classes,
            "covered_input_scales": scales,
            "paired_samples": len(pairs),
            "total_samples": len(samples),
            "coverage_ratio": coverage,
            "excluded_samples": len(excluded),
            "scope_text": scope_text,
            "plan_version": plan_version,
            "rule": "缺少成对证据的样本保留在 excluded，不使用预估收益补入",
        }

    def _build_message(
        self,
        rule_no: str,
        measured: float | None,
        threshold: float,
        passed: bool,
        scope: dict[str, Any],
    ) -> str:
        if measured is None:
            return (
                f"规则{rule_no}暂无可用的成对实测样本，"
                f"未达阈值{_percent_text(threshold)}，且不以预估收益补算。")
        verdict = "已达到" if passed else "未达到"
        return (
            f"规则{rule_no}实测收益{_percent_text(measured)}，{verdict}"
            f"收益阈值{_percent_text(threshold)}；{scope.get('scope_text', '')}。")

    # ---------- 计划修改 ----------

    def revise_plan(
        self,
        entry_id: int,
        version: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """修改规则内容并切换到新的实际计划版本。

        修改后旧版本的核验报告不再适用于新版本；下一次核验会按新版本
        重新筛选样本，旧样本以「计划版本不一致」排除。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        allowed = {"索引建议", "分区裁剪", "预聚合", "并行策略", "优化收益", "关联API", THRESHOLD_FIELD}
        applied = []
        for field, value in (changes or {}).items():
            if field in allowed:
                entry[field] = value
                applied.append(field)

        if version:
            entry["规则版本"] = version
        else:
            entry["规则版本"] = self._next_version(str(entry.get("规则版本") or "v1"))

        entry["核验收益"] = "待核验"
        entry["核验状态"] = "待核验"
        entry["核验时间"] = ""
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        changed = "、".join(applied) if applied else "无字段变更"
        return entry, f"优化规则已更新为{entry['规则版本']}（{changed}），需重新核验收益"

    @staticmethod
    def _next_version(version: str) -> str:
        prefix = "".join(ch for ch in version if not ch.isdigit())
        digits = "".join(ch for ch in version if ch.isdigit())
        if not digits:
            return f"{version or 'v'}2"
        return f"{prefix}{int(digits) + 1}"

    def _now_iso(self) -> str:
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    # ---------- 动作/快照 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "收益核验":
            report = self.verify_plan(str(entry.get("规则编号") or ""))
            if not report.get("ok"):
                return None, str(report.get("message"))
            from app.services.data_manager import write_module_snapshot
            write_module_snapshot(self.MODULE, self.domain_snapshot())
            return entry, str(report.get("message"))

        if action == "标记生效":
            from app.services.data_manager import read_api_snapshot
            api_name = entry.get("关联API", "")
            registry = read_api_snapshot()
            for api_data in (registry.values() if isinstance(registry, dict) else []):
                if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                    break
            else:
                return None, "关联API尚未注册，无法标记生效优化规则"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        opt_list = []
        for row in rows:
            report = self.read_verification(
                str(row.get("规则编号") or ""), str(row.get("规则版本") or ""))
            opt_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "api": row.get("关联API"),
                "index_hint": row.get("索引建议"),
                "partition": row.get("分区裁剪"),
                "pre_agg": row.get("预聚合"),
                "parallel": row.get("并行策略"),
                "benefit": row.get("优化收益"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
                "measured_benefit": report.get("measured_benefit"),
                "verification_passed": report.get("passed"),
                "verification_scope": report.get("data_scope", {}).get("scope_text"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(opt_list),
            "optimizations": opt_list,
            "active_count": sum(1 for r in opt_list if r.get("status") == "已生效"),
            "verified_count": sum(1 for r in opt_list if r.get("verification_passed")),
        }
