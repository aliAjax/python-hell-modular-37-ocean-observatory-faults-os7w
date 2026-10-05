import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RecoveryPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.operator = Actor("op-1", "operator")
        self.operator2 = Actor("op-2", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------

    def topology(self):
        station = self.service.create(self.admin, "station", {"name": "OSN-01", "region": "East"})
        asset = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-1", "last_seen": "2026-10-04T09:00:00Z"})
        backup = self.service.create(self.admin, "link", {"station_id": station["id"], "asset_id": asset["id"], "link_type": "backup", "capacity": 50})
        mission = self.service.create(self.admin, "mission", {"station_id": station["id"], "purpose": "onsite repair", "window_start": "2026-10-05T00:00:00Z", "window_end": "2026-10-06T00:00:00Z"})
        return station, asset, backup, mission

    def incident(self, asset, kind="link_loss"):
        entity = self.service.create(self.admin, "incident", {"asset_id": asset["id"], "kind": kind, "severity": "high", "summary": "no data"})
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            entity = self.service.transition(self.admin, entity["id"], action)
        return entity

    def chain(self, backup, mission):
        return [
            {"step_id": "s1", "name": "远程重启", "action_type": "remote_restart", "depends_on": None, "resources": []},
            {"step_id": "s2", "name": "切换备用链路", "action_type": "switch_backup", "depends_on": "s1", "resources": [backup["id"]]},
            {"step_id": "s3", "name": "出海作业", "action_type": "dispatch_mission", "depends_on": "s2", "resources": [mission["id"]]},
        ]

    def submit(self, actor, incident, steps):
        return self.service.create(actor, "recovery_plan", {"incident_id": incident["id"], "title": "恢复处理链", "steps": steps})

    def act(self, actor, plan, action, data=None):
        return self.service.transition(actor, plan["id"], action, data or {})

    def step(self, plan, step_id):
        for item in plan["data"]["steps"]:
            if item["step_id"] == step_id:
                return item
        raise AssertionError("step not found: " + step_id)

    def refcount(self, resource):
        return self.service.resource_status(resource["id"])["refcount"]

    # -- 链式执行 ----------------------------------------------------

    def test_chain_runs_in_dependency_order_and_closes_incident(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        plan = self.submit(self.operator, incident, self.chain(backup, mission))
        self.assertEqual(plan["status"], "planned")

        plan = self.act(self.operator, plan, "start_plan")
        with self.assertRaises(InvalidTransition):
            self.act(self.operator, plan, "start_step", {"step_id": "s2"})

        plan = self.act(self.operator, plan, "start_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "complete_step", {"step_id": "s1", "outcome": "asset online"})
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s2"})
        self.assertEqual(self.refcount(backup), 1)
        plan = self.act(self.operator, plan, "complete_step", {"step_id": "s2"})
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s3"})
        self.assertEqual(self.refcount(mission), 1)
        plan = self.act(self.operator, plan, "complete_step", {"step_id": "s3"})
        self.assertEqual(plan["status"], "succeeded")

        resolved = self.service.transition(self.admin, incident["id"], "resolve", {"summary": "restored"})
        self.assertEqual(resolved["status"], "resolved")

    def test_step_actions_require_running_plan_and_step(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        plan = self.submit(self.operator, incident, self.chain(backup, mission))
        with self.assertRaises(InvalidTransition):
            self.act(self.operator, plan, "start_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "start_plan")
        with self.assertRaises(InvalidTransition):
            self.act(self.operator, plan, "complete_step", {"step_id": "s1"})
        with self.assertRaises(InvalidTransition):
            self.act(self.operator, plan, "fail_step", {"step_id": "s1"})
        with self.assertRaises(NotFoundError):
            self.act(self.operator, plan, "start_step", {"step_id": "ghost"})

    # -- 失败回滚 ----------------------------------------------------

    def test_failed_step_rolls_back_effective_steps(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        plan = self.submit(self.operator, incident, self.chain(backup, mission))
        plan = self.act(self.operator, plan, "start_plan")
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "complete_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s2"})
        self.assertEqual(self.refcount(backup), 1)

        plan = self.act(self.operator, plan, "fail_step", {"step_id": "s2", "reason": "backup link no carrier"})
        self.assertEqual(plan["status"], "compensating")

        plan = self.act(self.operator, plan, "compensate")
        self.assertEqual(plan["status"], "compensated")
        self.assertEqual(self.step(plan, "s3")["status"], "skipped")
        self.assertEqual(self.step(plan, "s2")["status"], "compensated")
        self.assertEqual(self.step(plan, "s1")["status"], "compensated")
        self.assertEqual(self.refcount(backup), 0)
        self.assertEqual(self.refcount(mission), 0)

        resolved = self.service.transition(self.admin, incident["id"], "resolve", {"summary": "rolled back"})
        self.assertEqual(resolved["status"], "resolved")

    def test_incident_cannot_close_with_unfinished_plan(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        plan = self.submit(self.operator, incident, self.chain(backup, mission))
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, incident["id"], "resolve", {"summary": "x"})
        plan = self.act(self.operator, plan, "start_plan")
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "complete_step", {"step_id": "s1"})
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s2"})
        plan = self.act(self.operator, plan, "fail_step", {"step_id": "s2", "reason": "x"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, incident["id"], "resolve", {"summary": "x"})
        plan = self.act(self.operator, plan, "compensate")
        self.assertEqual(plan["status"], "compensated")
        resolved = self.service.transition(self.admin, incident["id"], "resolve", {"summary": "clean"})
        self.assertEqual(resolved["status"], "resolved")

    # -- 共享资源引用计数 --------------------------------------------

    def test_shared_resource_survives_other_incidents_compensation(self):
        station, asset, backup, mission = self.topology()
        asset2 = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-2", "last_seen": "2026-10-04T09:00:00Z"})
        incident_a = self.incident(asset, kind="link_loss")
        incident_b = self.incident(asset2, kind="power_failure")
        steps_a = [{"step_id": "a1", "name": "切换备用链路", "depends_on": None, "resources": [backup["id"]]}]
        steps_b = [{"step_id": "b1", "name": "切换备用链路", "depends_on": None, "resources": [backup["id"]]}]
        plan_a = self.act(self.operator, self.submit(self.operator, incident_a, steps_a), "start_plan")
        plan_b = self.act(self.operator2, self.submit(self.operator2, incident_b, steps_b), "start_plan")
        plan_a = self.act(self.operator, plan_a, "start_step", {"step_id": "a1"})
        plan_b = self.act(self.operator2, plan_b, "start_step", {"step_id": "b1"})
        self.assertEqual(self.refcount(backup), 2)

        plan_a = self.act(self.operator, plan_a, "fail_step", {"step_id": "a1", "reason": "x"})
        plan_a = self.act(self.operator, plan_a, "compensate")
        self.assertEqual(plan_a["status"], "compensated")
        status = self.service.resource_status(backup["id"])
        self.assertEqual(status["refcount"], 1)
        self.assertEqual([h["plan_id"] for h in status["holders"]], [plan_b["id"]])

        plan_b = self.act(self.operator2, plan_b, "fail_step", {"step_id": "b1", "reason": "x"})
        plan_b = self.act(self.operator2, plan_b, "compensate")
        self.assertEqual(self.refcount(backup), 0)

    # -- 并发提交：先入库者生效 --------------------------------------

    def test_concurrent_submission_first_write_wins(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def submit(actor):
            barrier.wait()
            try:
                outcomes.append(self.submit(actor, incident, self.chain(backup, mission)))
            except ConflictError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submit, args=(self.operator,)),
            threading.Thread(target=submit, args=(self.operator2,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(self.service.list("recovery_plan")), 1)

    def test_sequential_submission_rejected_while_active(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        plan = self.submit(self.operator, incident, self.chain(backup, mission))
        with self.assertRaises(ConflictError):
            self.submit(self.operator2, incident, self.chain(backup, mission))
        plan = self.act(self.operator, plan, "cancel_plan")
        replacement = self.submit(self.operator2, incident, self.chain(backup, mission))
        self.assertEqual(replacement["status"], "planned")

    # -- 断点与重启恢复 ----------------------------------------------

    def test_compensation_stops_at_breakpoint_and_resumes_after_restart(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        steps = self.chain(backup, mission)
        steps[0]["resources"] = [mission["id"]]
        plan = self.submit(self.operator, incident, steps)
        plan = self.act(self.operator, plan, "start_plan")
        for step_id in ("s1", "s2"):
            plan = self.act(self.operator, plan, "start_step", {"step_id": step_id})
            plan = self.act(self.operator, plan, "complete_step", {"step_id": step_id})
        plan = self.act(self.operator, plan, "start_step", {"step_id": "s3"})
        plan = self.act(self.operator, plan, "fail_step", {"step_id": "s3", "reason": "weather hold"})
        self.assertEqual(self.refcount(backup), 1)
        self.assertEqual(self.refcount(mission), 2)

        # 补偿到一半中断（模拟进程崩溃）：只完成一步，停在断点
        plan = self.act(self.operator, plan, "compensate", {"max_steps": 1})
        self.assertEqual(plan["status"], "compensation_failed")
        self.assertEqual(plan["data"]["compensation"]["breakpoint_step"], "s2")
        self.assertEqual(self.step(plan, "s3")["status"], "compensated")
        self.assertEqual(self.refcount(backup), 1)
        self.assertEqual(self.refcount(mission), 1)

        # 重启后由新的服务实例继续补偿
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        plan = restarted.transition(self.operator, plan["id"], "resume_compensation")
        self.assertEqual(plan["status"], "compensated")
        self.assertEqual(self.refcount(backup), 0)
        self.assertEqual(self.refcount(mission), 0)

        with self.assertRaises(InvalidTransition):
            restarted.transition(self.operator, plan["id"], "compensate")

    def test_repeated_compensation_never_releases_twice(self):
        station, asset, backup, mission = self.topology()
        asset2 = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-2", "last_seen": "2026-10-04T09:00:00Z"})
        incident_a = self.incident(asset, kind="link_loss")
        incident_b = self.incident(asset2, kind="power_failure")
        steps_a = [
            {"step_id": "a1", "name": "切换备用链路", "depends_on": None, "resources": [backup["id"]]},
            {"step_id": "a2", "name": "出海作业", "depends_on": "a1", "resources": [backup["id"]]},
        ]
        steps_b = [{"step_id": "b1", "name": "切换备用链路", "depends_on": None, "resources": [backup["id"]]}]
        plan_a = self.act(self.operator, self.submit(self.operator, incident_a, steps_a), "start_plan")
        plan_b = self.act(self.operator2, self.submit(self.operator2, incident_b, steps_b), "start_plan")
        plan_a = self.act(self.operator, plan_a, "start_step", {"step_id": "a1"})
        plan_a = self.act(self.operator, plan_a, "complete_step", {"step_id": "a1"})
        plan_a = self.act(self.operator, plan_a, "start_step", {"step_id": "a2"})
        plan_b = self.act(self.operator2, plan_b, "start_step", {"step_id": "b1"})
        self.assertEqual(self.refcount(backup), 3)

        plan_a = self.act(self.operator, plan_a, "fail_step", {"step_id": "a2", "reason": "x"})
        # 分两步补偿，每步只释放自己的持有
        plan_a = self.act(self.operator, plan_a, "compensate", {"max_steps": 1})
        self.assertEqual(self.refcount(backup), 2)
        plan_a = self.act(self.operator, plan_a, "resume_compensation")
        self.assertEqual(plan_a["status"], "compensated")
        # 事件 B 的持有不受事件 A 补偿影响
        self.assertEqual(self.refcount(backup), 1)
        holders = self.service.resource_status(backup["id"])["holders"]
        self.assertEqual([h["plan_id"] for h in holders], [plan_b["id"]])
        # 重复补偿被拒绝且资源计数不变
        with self.assertRaises(InvalidTransition):
            self.act(self.operator, plan_a, "compensate")
        self.assertEqual(self.refcount(backup), 1)

    # -- 提交校验 ----------------------------------------------------

    def test_plan_validation(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        with self.assertRaises(ValidationError):
            self.submit(self.operator, incident, [])
        with self.assertRaises(ValidationError):
            self.submit(self.operator, incident, [{"step_id": "s1", "name": "x", "depends_on": "s9"}])
        with self.assertRaises(ValidationError):
            self.submit(self.operator, incident, [
                {"step_id": "s1", "name": "x"},
                {"step_id": "s2", "name": "y", "depends_on": "s9"},
            ])
        with self.assertRaises(ValidationError):
            self.submit(self.operator, incident, [
                {"step_id": "s1", "name": "x"},
                {"step_id": "s1", "name": "y", "depends_on": "s1"},
            ])
        with self.assertRaises(ValidationError):
            self.submit(self.operator, incident, [{"step_id": "s1", "name": "x", "resources": ["no-such-resource"]}])
        with self.assertRaises(ValidationError):
            self.service.create(self.operator, "recovery_plan", {"incident_id": "no-such-incident", "steps": [{"step_id": "s1", "name": "x"}]})
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("viewer", "viewer"), "recovery_plan", {"incident_id": incident["id"], "steps": [{"step_id": "s1", "name": "x"}]})

    def test_cannot_submit_plan_for_resolved_incident(self):
        station, asset, backup, mission = self.topology()
        incident = self.incident(asset)
        resolved = self.service.transition(self.admin, incident["id"], "resolve", {"summary": "done"})
        with self.assertRaises(ValidationError):
            self.submit(self.operator, resolved, self.chain(backup, mission))


if __name__ == "__main__":
    unittest.main()
