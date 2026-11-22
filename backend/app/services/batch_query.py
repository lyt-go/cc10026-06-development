"""批量查询领域服务：管理批量API查询任务的排队、执行和结果汇总。

跨模块数据流：
- 消费 api_registry 快照 → 批量查询API合法性校验
- 消费 query_optimize 的生效计划 → 执行前绑定本次实际计划版本
- 产出收益核验样本记录 → query_optimize 按同类对照查询核算收益
- 查询执行明细 → 写入 data/query_benefit/batches/ 保留延迟明细与实际版本
- 消费 cache_layer 快照 → 批量查询结果缓存策略

收益核验约定：
- 执行前先把任务与「规则编号 + 实际计划版本」关联，再生成延迟样本；
- 每个样本带同类查询标识和输入规模，执行两轮时相同查询可对照；
- 超时或缺少对照的样本只保留已测的一侧，不填预估值。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BatchQueryService(BaseModuleService):
    MODULE = "batch_query"
    ENTITY = "批量任务"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["任务编号", "关联API", "查询清单", "并发度", "失败策略", "结果汇总", "超时控制", "任务状态", "计划版本"]
    REQUIRED_FIELDS = ["任务编号", "关联API", "查询清单"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "部分完成"]
    ACTION_RULES = {"发起执行": "执行中", "确认完成": "已完成", "标记部分完成": "部分完成"}
    NEGATIVE_ACTIONS = []

    # ---------- 计划绑定 ----------

    def bind_plan(self, entry: dict[str, Any], plan_version: str | None = None) -> dict[str, Any]:
        """执行前把计划与延迟样本入口关联。

        plan_version 为空时取该API当前纳入评估的计划版本（优先已生效规则）；
        显式传入时用于对照轮（例如先跑旧计划），两轮执行各自留下实际版本。
        """
        from app.services import service_for
        opt_svc = service_for("query_optimize")
        plan: dict[str, Any] = {}
        if opt_svc and hasattr(opt_svc, "get_plan_for_api"):
            plan = opt_svc.get_plan_for_api(entry.get("关联API", ""))
        if not plan and opt_svc and hasattr(opt_svc, "get_optimization_plan"):
            plan = opt_svc.get_optimization_plan(entry.get("关联API", ""))

        actual_version = str(plan_version or plan.get("plan_version") or entry.get("计划版本") or "")
        baseline_version = str(entry.get("对照计划版本") or "")
        rule_no = str(plan.get("rule_no") or "")

        # 执行前读取收益核验样本记录，判断该版本是否已达阈值；只有实测达线
        # 才把收益用于本轮延迟，未达线或证据不足时保持待核验口径。
        report: dict[str, Any] = {}
        if opt_svc and rule_no and hasattr(opt_svc, "verify_plan"):
            report = opt_svc.verify_plan(rule_no)
            if str(report.get("plan_version") or "") != actual_version:
                report = {}
        measured = report.get("measured_benefit") if report else None
        binding = {
            "rule_no": rule_no,
            "plan_version": actual_version,
            "baseline_plan_version": baseline_version,
            "benefit_basis": "实测核验" if measured is not None else "待核验",
            "measured_benefit": measured,
            "verification_passed": bool(report.get("passed")),
            "verification_scope": report.get("data_scope", {}) if report else {},
            "bound_at": self._now_iso(),
        }
        entry["关联规则"] = binding["rule_no"]
        entry["计划版本"] = actual_version
        entry["计划绑定"] = binding
        return binding

    # ---------- 执行 ----------

    def execute_batch(
        self,
        batch_id: int,
        plan_version: str | None = None,
        simulated_latencies: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """执行批量查询任务。

        - 执行前绑定计划版本与样本记录；
        - 每个查询的耗时按「该计划版本是否已有达标实测收益」调整，
          没有实测样本时不使用预估收益；
        - 超过超时控制的查询记为超时，样本只保留已测一侧；
        - 保留本轮延迟明细、实际版本，并写入本次执行留档。
        """
        entry = store.find(self.MODULE, batch_id)
        if entry is None:
            return {"ok": False, "message": "批量任务不存在"}

        from app.services.data_manager import write_benefit_batch, write_module_snapshot

        binding = self.bind_plan(entry, plan_version)
        actual_version = binding["plan_version"]
        baseline_version = binding["baseline_plan_version"]

        try:
            concurrency = int(entry.get("并发度", "1") or 1)
        except (ValueError, TypeError):
            concurrency = 1
        try:
            timeout_ms = float(entry.get("超时控制", "0") or 0)
        except (ValueError, TypeError):
            timeout_ms = 0.0

        api_name = entry.get("关联API", "")
        query_list_str = entry.get("查询清单", "")
        queries = [q.strip() for q in str(query_list_str).split(",") if q.strip()]

        request_id = f"B{batch_id:04d}-{self._next_round_seq(batch_id, entry)}"
        samples = entry.setdefault("实测样本记录", [])
        latency_detail = entry.setdefault("延迟明细", [])
        results = []
        applied_benefit = (
            float(binding["measured_benefit"])
            if binding.get("verification_passed") and binding.get("measured_benefit") is not None
            else None
        )
        # 实测收益只覆盖核验过的查询类别；未纳入核验的类别仍按待核验执行。
        covered_classes = set(
            (binding.get("verification_scope") or {}).get("covered_query_classes") or [])

        for query in queries:
            query_key = self.query_key(query)
            query_class = self.query_class(query)
            input_scale = self.resolve_input_scale(samples, query_key)
            base_latency = self._simulated_latency(query, simulated_latencies)
            status = "success"
            latency = base_latency
            class_benefit = (
                applied_benefit if not covered_classes or query_class in covered_classes else None)
            projected = False
            if timeout_ms and base_latency > timeout_ms:
                status = "timeout"
                latency = None
            elif class_benefit is not None:
                latency = round(base_latency * (1.0 - class_benefit), 2)
                projected = True

            detail = {
                "request_id": request_id,
                "query": query,
                "query_key": query_key,
                "query_class": query_class,
                "input_scale": input_scale,
                "api": api_name,
                "plan_version": actual_version,
                "baseline_plan_version": baseline_version,
                "rule_no": binding["rule_no"],
                "latency_ms": latency,
                "status": status,
                "measured": latency is not None and not projected,
                "projected": projected,
                "benefit_basis": "实测核验" if class_benefit is not None else "待核验",
                "benefit_scope": "已覆盖" if class_benefit is not None else "未纳入核验范围",
            }
            latency_detail.append(detail)
            results.append({
                "query": query,
                "api": api_name,
                "rows": 10,
                "latency_ms": latency,
                "status": status,
                "plan_version": actual_version,
                "optimized": bool(binding["rule_no"]),
                "benefit_basis": detail["benefit_basis"],
            })

            # 执行后把本轮实测写进同名同规模的样本记录，供优化计划核算收益。
            # 由计划收益推算出来的耗时不是实测，不能回流成核验证据。
            self._upsert_run(
                samples,
                query_key=query_key,
                input_scale=input_scale,
                query_class=detail["query_class"],
                query_text=query,
                plan_version=actual_version,
                baseline_version=baseline_version,
                latency_ms=latency,
                status=status,
                batch_no=str(entry.get("任务编号") or ""),
                run_id=request_id,
                measured=detail["measured"],
            )

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["任务状态"] = "已完成"
        entry["执行批次"] = request_id
        entry["结果汇总"] = f"共{len(results)}个查询完成"

        batch_record = {
            "batch_id": batch_id,
            "task_no": entry.get("任务编号"),
            "request_id": request_id,
            "api": api_name,
            "rule_no": binding["rule_no"],
            "plan_version": actual_version,
            "baseline_plan_version": baseline_version,
            "concurrency": concurrency,
            "timeout_ms": timeout_ms,
            "benefit_basis": "实测核验" if applied_benefit is not None else "待核验",
            "applied_benefit": applied_benefit,
            "latency_detail": [d for d in latency_detail if d.get("request_id") == request_id],
            "started_at": binding["bound_at"],
            "finished_at": self._now_iso(),
        }
        write_benefit_batch(batch_id, request_id, batch_record)

        write_module_snapshot(self.MODULE, {
            "batch_id": batch_id,
            "task_no": entry.get("任务编号"),
            "api": api_name,
            "query_count": len(results),
            "completed_count": sum(1 for r in results if r.get("status") == "success"),
            "optimization_plan": binding["rule_no"] or "none",
            "plan_version": actual_version,
            "results": results,
            "latency_detail": batch_record["latency_detail"],
            "samples": samples,
        })

        cache_svc = self._cache_service()
        if cache_svc and hasattr(cache_svc, "warmup_cache"):
            cache_svc.warmup_cache(api_name, len(results))

        return {
            "ok": True,
            "batch_id": batch_id,
            "request_id": request_id,
            "plan_version": actual_version,
            "query_count": len(results),
            "results": results,
            "latency_detail": batch_record["latency_detail"],
        }

    # ---------- 样本 ----------

    def record_run(
        self,
        batch_id: int,
        query_key: str,
        plan_version: str,
        latency_ms: float,
        status: str = "success",
        input_scale: int | None = None,
        query_class: str | None = None,
        baseline_version: str | None = None,
    ) -> dict[str, Any]:
        """补齐单条实测耗时（例如超时查询补跑成功后回填对照数据）。"""
        entry = store.find(self.MODULE, batch_id)
        if entry is None:
            return {"ok": False, "message": "批量任务不存在"}

        samples = entry.setdefault("实测样本记录", [])
        sample = self._upsert_run(
            samples,
            query_key=query_key,
            input_scale=input_scale if input_scale is not None
            else self.resolve_input_scale(samples, query_key),
            query_class=query_class or self.query_class(query_key),
            query_text=query_key,
            plan_version=plan_version,
            baseline_version=baseline_version if baseline_version is not None
            else str(entry.get("对照计划版本") or ""),
            latency_ms=latency_ms if status == "success" else None,
            status=status,
            batch_no=str(entry.get("任务编号") or ""),
            run_id=f"B{batch_id:04d}-manual",
        )
        return {"ok": True, "sample": sample}

    def verification_samples(self, api_name: str) -> list[dict[str, Any]]:
        """返回指定API下全部收益核验样本记录（跨批量任务去重）。"""
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for row in store.rows(self.MODULE):
            if row.get("关联API") != api_name:
                continue
            batch_no = str(row.get("任务编号") or "")
            for sample in row.get("实测样本记录") or []:
                key = (batch_no, str(sample.get("样本编号") or id(sample)))
                record = dict(sample)
                record.setdefault("来源批次", [batch_no])
                record["_batch_no"] = batch_no
                seen[key] = record
        return list(seen.values())

    def _next_round_seq(self, batch_id: int, entry: dict[str, Any]) -> int:
        """增量轮次：已存档轮次、样本里的历史轮次与任务上残留轮次取大者，
        避免新一轮覆盖已留档的实测证据。"""
        from app.services.data_manager import list_benefit_batches
        candidates = [str(item.get("request_id", "")) for item in list_benefit_batches(batch_id)]
        candidates.append(str(entry.get("执行批次") or ""))
        for sample in entry.get("实测样本记录") or []:
            candidates.extend(str(run.get("run_id") or "") for run in sample.get("runs") or [])
        seqs = [
            int(text.rsplit("-", 1)[-1])
            for text in candidates
            if text.rsplit("-", 1)[-1].isdigit()
        ]
        return max(seqs, default=0) + 1

    def batch_rounds(self, batch_id: int) -> list[dict[str, Any]]:
        """列出某个批量任务的历次执行（含实际计划版本与延迟明细）。"""
        from app.services.data_manager import list_benefit_batches
        return list_benefit_batches(batch_id)

    def compare_rounds(self, batch_id: int) -> dict[str, Any]:
        """核对两轮执行使用的计划版本，确认对照关系。"""
        rounds = self.batch_rounds(batch_id)
        versions = [str(item.get("plan_version") or "") for item in rounds]
        paired = len(rounds) >= 2 and len(set(versions)) >= 2
        return {
            "batch_id": batch_id,
            "rounds": [
                {
                    "request_id": item.get("request_id"),
                    "plan_version": item.get("plan_version"),
                    "baseline_plan_version": item.get("baseline_plan_version"),
                    "rule_no": item.get("rule_no"),
                    "timeout_count": sum(
                        1 for d in item.get("latency_detail", []) if d.get("status") == "timeout"),
                }
                for item in rounds
            ],
            "versions": versions,
            "has_contrast": paired,
            "message": (
                f"两轮分别使用计划版本{' / '.join(versions)}，可作为同类对照"
                if paired else "尚未形成两轮不同计划版本的对照"),
        }

    def _upsert_run(
        self,
        samples: list[dict[str, Any]],
        *,
        query_key: str,
        input_scale: Any,
        query_class: str,
        query_text: str,
        plan_version: str,
        baseline_version: str,
        latency_ms: float | None,
        status: str,
        batch_no: str,
        run_id: str,
        measured: bool = True,
    ) -> dict[str, Any]:
        """按「同一查询 + 同一输入规模」聚合样本，两轮执行写进同一条记录。

        样本本身不锁定计划版本，版本登记在每条 run 上，这样先跑旧计划、
        后跑新计划也能落到同一条样本里形成对照。
        """
        sample = None
        for item in samples:
            if (
                str(item.get("query_key")) == str(query_key)
                and str(item.get("input_scale")) == str(input_scale)
            ):
                sample = item
                break
        if sample is None:
            sample = {
                "样本编号": f"SMP-{len(samples) + 1:04d}",
                "query_key": query_key,
                "query_text": query_text,
                "query_class": query_class,
                "input_scale": input_scale,
                "runs": [],
            }
            samples.append(sample)

        if baseline_version:
            sample["对照计划版本"] = baseline_version
        sources = sample.setdefault("来源批次", [])
        if batch_no and batch_no not in sources:
            sources.append(batch_no)

        run: dict[str, Any] = {
            "run_id": run_id,
            "plan_version": plan_version,
            "latency_ms": latency_ms,
            "status": status,
            "measured": measured and latency_ms is not None,
            "recorded_at": self._now_iso(),
        }
        # 同一轮同一版本重复执行时覆盖，跨轮的旧版本实测保留。
        sample["runs"] = [
            existing for existing in sample.get("runs", [])
            if not (str(existing.get("plan_version")) == str(plan_version)
                    and existing.get("run_id") == run_id)
        ]
        sample["runs"].append(run)
        return sample

    # ---------- 辅助 ----------

    @staticmethod
    def query_key(query: str) -> str:
        """同类查询判定键：去掉具体分区值，保留查询形态。"""
        parts = [part for part in str(query).split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return str(query)

    @staticmethod
    def query_class(query: str) -> str:
        text = str(query)
        if "明细" in text:
            return "长查询"
        if "汇总" in text:
            return "短查询"
        return "未分类"

    @staticmethod
    def infer_input_scale(query: str) -> int:
        """输入规模：按查询键（不含分区值）估算，两轮执行同一查询保持一致。"""
        return 100 + len(BatchQueryService.query_key(query)) * 10

    def resolve_input_scale(self, samples: list[dict[str, Any]], query_key: str) -> int:
        """复用已有样本的输入规模，保证补充实测落到同一条样本上。"""
        for sample in samples:
            if str(sample.get("query_key")) == str(query_key):
                scale = sample.get("input_scale")
                if scale is not None:
                    return int(scale)
        return self.infer_input_scale(query_key)

    def _simulated_latency(
        self, query: str, simulated: dict[str, float] | None) -> float:
        if simulated and query in simulated:
            return float(simulated[query])
        # 示例口径：长查询（明细扫描）明显慢于短查询（汇总），保持同类可比。
        if self.query_class(query) == "长查询":
            return float(1500 + len(str(query)) * 20)
        return float(180 + len(str(query)) * 7)

    def _cache_service(self):
        from app.services import service_for
        return service_for("cache_layer")

    def _now_iso(self) -> str:
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    # ---------- 动作/快照 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发起执行":
            result = self.execute_batch(entry_id)
            if not result.get("ok"):
                return None, result.get("message", "批量查询失败")
            entry["status"] = "已完成"
            entry["pending"] = False
            return entry, (
                f"批量查询完成，共{result.get('query_count', 0)}个查询，"
                f"使用计划版本{result.get('plan_version') or '未绑定'}")

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        batch_list = []
        for row in rows:
            batch_list.append({
                "id": row.get("id"),
                "task_no": row.get("任务编号"),
                "api": row.get("关联API"),
                "queries": row.get("查询清单"),
                "concurrency": row.get("并发度"),
                "failure_policy": row.get("失败策略"),
                "results": row.get("结果汇总"),
                "timeout": row.get("超时控制"),
                "status": row.get("status"),
                "plan_version": row.get("计划版本"),
                "rule_no": row.get("关联规则"),
                "execution_round": row.get("执行批次"),
                "latency_detail": row.get("延迟明细", []),
                "samples": row.get("实测样本记录", []),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(batch_list),
            "batches": batch_list,
            "completed_count": sum(1 for r in batch_list if r.get("status") == "已完成"),
            "sample_count": sum(len(item.get("samples", [])) for item in batch_list),
        }
