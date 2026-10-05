"""恢复处理链编排：依赖串链、共享资源引用计数、失败补偿与断点恢复。

处理链（recovery_plan）挂在故障事件下，步骤按 depends_on 串成链。
步骤启动时占用共享资源（resource_hold 表按 (plan, step, resource) 记账），
失败后将已生效步骤沿依赖逆序补偿；补偿按步落库，中断停在断点，
重启后可继续，重复补偿不会重复释放资源。
"""

from .domain import (
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow

PLAN_ROLES = ("admin", "operator", "engineer")
TERMINAL_STEP_STATUSES = ("compensated", "skipped")


class RecoveryPlanService:
    def __init__(self, repository, audit):
        self.repository = repository
        self.audit = audit

    # -- 提交处理链 -------------------------------------------------

    def create_plan(self, actor, entity_id, payload, status):
        steps = []
        for raw in payload.get("steps", []):
            steps.append(
                {
                    "step_id": raw["step_id"],
                    "name": raw.get("name"),
                    "action_type": raw.get("action_type"),
                    "depends_on": raw.get("depends_on"),
                    "resources": list(raw.get("resources") or []),
                    "status": "pending",
                    "detail": {},
                }
            )
        payload = dict(payload, steps=steps)
        return self.repository.create_plan(
            entity_id, payload["incident_id"], status, payload, actor.user_id
        )

    # -- 动作分发 ---------------------------------------------------

    def handle_action(self, actor, plan, action, data, expected_version=None):
        if actor.role not in PLAN_ROLES:
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        handlers = {
            "start_plan": self._start_plan,
            "cancel_plan": self._cancel_plan,
            "start_step": self._start_step,
            "complete_step": self._complete_step,
            "fail_step": self._fail_step,
            "compensate": self._compensate,
            "resume_compensation": self._compensate,
        }
        handler = handlers.get(action)
        if not handler:
            raise InvalidTransition("unknown action %s for recovery_plan" % action)
        return handler(actor, plan, data, expected_version)

    # -- 工具 -------------------------------------------------------

    @staticmethod
    def _steps(plan):
        return plan["data"].get("steps", [])

    @staticmethod
    def _find_step(steps, step_id):
        for step in steps:
            if step.get("step_id") == step_id:
                return step
        return None

    @staticmethod
    def _replace_step(steps, step_id, **changes):
        replaced = []
        for step in steps:
            if step.get("step_id") == step_id:
                merged = dict(step)
                merged.update(changes)
                replaced.append(merged)
            else:
                replaced.append(step)
        return replaced

    def _mutate(self, plan, status, data, expected_version=None, **kwargs):
        version = plan["version"] if expected_version is None else expected_version
        return self.repository.mutate_plan(plan["id"], version, status, data, **kwargs)

    def _require_step(self, plan, data):
        step_id = data.get("step_id")
        if not step_id:
            raise ValidationError("step_id is required")
        step = self._find_step(self._steps(plan), step_id)
        if not step:
            raise NotFoundError("step not found: " + str(step_id))
        return step

    # -- 链级动作 ---------------------------------------------------

    def _start_plan(self, actor, plan, data, expected_version):
        if plan["status"] != "planned":
            raise InvalidTransition("cannot start plan from status " + plan["status"])
        entity, _, _ = self._mutate(plan, "running", plan["data"], expected_version)
        self.audit.record(plan["id"], actor, "start_plan", "planned", "running", {})
        return entity

    def _cancel_plan(self, actor, plan, data, expected_version):
        if plan["status"] != "planned":
            raise InvalidTransition("cannot cancel plan from status " + plan["status"])
        entity, _, _ = self._mutate(
            plan, "cancelled", plan["data"], expected_version, release_guard=True
        )
        self.audit.record(plan["id"], actor, "cancel_plan", "planned", "cancelled", {})
        return entity

    # -- 步骤动作 ---------------------------------------------------

    def _start_step(self, actor, plan, data, expected_version):
        if plan["status"] != "running":
            raise InvalidTransition("plan is not running")
        steps = self._steps(plan)
        step = self._require_step(plan, data)
        if step["status"] != "pending":
            raise InvalidTransition("cannot start step from status " + step["status"])
        depends_on = step.get("depends_on")
        if depends_on:
            dependency = self._find_step(steps, depends_on)
            if not dependency or dependency["status"] != "succeeded":
                raise InvalidTransition(
                    "dependency step %s has not succeeded" % depends_on
                )
        new_steps = self._replace_step(steps, step["step_id"], status="running")
        new_data = dict(plan["data"], steps=new_steps)
        acquire = [(step["step_id"], rid) for rid in step.get("resources", [])]
        entity, acquired, _ = self._mutate(
            plan, "running", new_data, expected_version, acquire=acquire
        )
        self.audit.record(
            plan["id"], actor, "start_step", "running", "running",
            {"step_id": step["step_id"], "acquired": acquired},
        )
        return entity

    def _complete_step(self, actor, plan, data, expected_version):
        if plan["status"] != "running":
            raise InvalidTransition("plan is not running")
        steps = self._steps(plan)
        step = self._require_step(plan, data)
        if step["status"] != "running":
            raise InvalidTransition("cannot complete step from status " + step["status"])
        detail = dict(step.get("detail") or {})
        if data.get("outcome"):
            detail["outcome"] = data.get("outcome")
        detail["completed_by"] = actor.user_id
        new_steps = self._replace_step(
            steps, step["step_id"], status="succeeded", detail=detail
        )
        finished = all(s["status"] == "succeeded" for s in new_steps)
        new_status = "succeeded" if finished else "running"
        new_data = dict(plan["data"], steps=new_steps)
        entity, _, _ = self._mutate(
            plan, new_status, new_data, expected_version, release_guard=finished
        )
        self.audit.record(
            plan["id"], actor, "complete_step", "running", new_status,
            {"step_id": step["step_id"]},
        )
        return entity

    def _fail_step(self, actor, plan, data, expected_version):
        if plan["status"] != "running":
            raise InvalidTransition("plan is not running")
        steps = self._steps(plan)
        step = self._require_step(plan, data)
        if step["status"] != "running":
            raise InvalidTransition("cannot fail step from status " + step["status"])
        detail = dict(step.get("detail") or {})
        detail["reason"] = data.get("reason") or ""
        detail["failed_by"] = actor.user_id
        new_steps = self._replace_step(
            steps, step["step_id"], status="failed", detail=detail
        )
        compensation = {
            "started_at": utcnow(),
            "attempts": 0,
            "failed_step": step["step_id"],
        }
        new_data = dict(plan["data"], steps=new_steps, compensation=compensation)
        entity, _, _ = self._mutate(plan, "compensating", new_data, expected_version)
        self.audit.record(
            plan["id"], actor, "fail_step", "running", "compensating",
            {"step_id": step["step_id"], "reason": detail["reason"]},
        )
        return entity

    # -- 补偿 -------------------------------------------------------

    def _compensate(self, actor, plan, data, expected_version=None):
        """沿依赖逆序回滚已生效步骤，每步一个事务。

        中断（进程重启或达到 max_steps 批次上限）时停在断点，状态记为
        compensation_failed，再次调用即可继续；已补偿步骤自动跳过，
        资源持有记录只释放一次。
        """
        if plan["status"] not in ("compensating", "compensation_failed"):
            raise InvalidTransition("cannot compensate plan from status " + plan["status"])
        max_steps = data.get("max_steps")
        if max_steps is not None:
            try:
                max_steps = int(max_steps)
            except (TypeError, ValueError):
                raise ValidationError("max_steps must be an integer")
            if max_steps < 1:
                raise ValidationError("max_steps must be positive")

        compensation = dict(plan["data"].get("compensation") or {})
        compensation["attempts"] = int(compensation.get("attempts", 0)) + 1
        compensation.setdefault("started_at", utcnow())
        compensation.pop("breakpoint_step", None)
        new_data = dict(plan["data"], compensation=compensation)
        self._mutate(plan, "compensating", new_data)

        processed = 0
        while True:
            current = self.repository.get_entity(plan["id"])
            steps = self._steps(current)
            target = None
            for candidate in reversed(steps):
                if candidate.get("status") not in TERMINAL_STEP_STATUSES:
                    target = candidate
                    break
            if target is None or (max_steps is not None and processed >= max_steps):
                break
            if target["status"] == "pending":
                step_status, release = "skipped", ()
            else:
                step_status, release = "compensated", (target["step_id"],)
            new_steps = self._replace_step(steps, target["step_id"], status=step_status)
            new_data = dict(current["data"], steps=new_steps)
            _, _, released = self._mutate(
                current, "compensating", new_data, release_steps=release
            )
            self.audit.record(
                plan["id"], actor, "compensate_step", "compensating", "compensating",
                {
                    "step_id": target["step_id"],
                    "step_status": step_status,
                    "released": released,
                },
            )
            processed += 1

        current = self.repository.get_entity(plan["id"])
        steps = self._steps(current)
        remaining = [s for s in steps if s.get("status") not in TERMINAL_STEP_STATUSES]
        compensation = dict(current["data"].get("compensation") or {})
        if not remaining:
            compensation["finished_at"] = utcnow()
            new_data = dict(current["data"], steps=steps, compensation=compensation)
            entity, _, _ = self._mutate(
                current, "compensated", new_data, release_guard=True
            )
            self.audit.record(
                plan["id"], actor, "compensated", "compensating", "compensated", {}
            )
            return entity
        compensation["breakpoint_step"] = remaining[-1]["step_id"]
        new_data = dict(current["data"], compensation=compensation)
        entity, _, _ = self._mutate(current, "compensation_failed", new_data)
        self.audit.record(
            plan["id"], actor, "compensation_paused", "compensating",
            "compensation_failed",
            {"breakpoint_step": compensation["breakpoint_step"]},
        )
        return entity

    # -- 查询 -------------------------------------------------------

    def resource_status(self, resource_id):
        if not self.repository.get_entity(resource_id):
            raise NotFoundError("entity not found: " + resource_id)
        holders = self.repository.resource_holders(resource_id)
        return {
            "resource_id": resource_id,
            "refcount": len(holders),
            "holders": holders,
        }
