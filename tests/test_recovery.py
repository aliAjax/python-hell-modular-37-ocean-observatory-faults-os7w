import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.recovery import RecoveryService
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine()
        self.service = DomainService(self.repo, self.rules)
        self.recovery = RecoveryService(self.repo, self.rules, self.service)
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def make_incident(self, link_status="down"):
        station = self.service.create(self.actor, "station", {"name": "S", "region": "R"})
        asset = self.service.create(self.actor, "asset", {
            "station_id": station["id"], "asset_type": "sensor",
            "serial_no": "X", "last_seen": "2026-09-27",
        })
        asset = self.service.transition(self.actor, asset["id"], "fail", {"reason": "faulty"})
        link = self.service.create(self.actor, "link", {
            "station_id": station["id"], "asset_id": asset["id"],
            "link_type": "fiber", "capacity": 100,
        })
        link = self.service.transition(self.actor, link["id"], "fail", {"reason": "down"})
        if link_status == "up":
            link = self.service.transition(self.actor, link["id"], "restore", {})
        incident = self.service.create(self.actor, "incident", {
            "station_id": station["id"], "asset_id": asset["id"], "link_id": link["id"],
            "kind": "link_loss", "severity": "high", "summary": "x",
        })
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.actor, incident["id"], action)
        return station, asset, link, incident

    def step(self, name, target, action, compensation, depends_on=None, resources=None):
        return {
            "name": name,
            "depends_on": depends_on,
            "target": target,
            "action": action,
            "compensation": compensation,
            "resources": resources or [],
        }

    def run_chain(self, incident_id, steps, idempotency_key=None):
        chain = self.recovery.submit_chain(self.actor, incident_id, steps, idempotency_key)
        return self.recovery.execute_chain(chain["id"])

    def create_running_chain(self, incident_id, steps):
        normalized = []
        for index, step in enumerate(steps):
            normalized.append({
                "step_no": index,
                "name": step["name"],
                "depends_on": step.get("depends_on"),
                "target": step["target"],
                "action": step["action"],
                "compensation": step["compensation"],
                "resources": step.get("resources") or [],
                "state": "pending",
                "error": None,
            })
        payload = {
            "incident_id": incident_id,
            "steps": normalized,
            "error": None,
            "created_by": self.actor.user_id,
            "created_by_role": self.actor.role,
        }
        return self.repo.create_entity("chain-" + incident_id, "recovery_chain", "running", payload, self.actor.user_id)

    def force_breakpoint(self, chain_id, step_no):
        chain = self.repo.get_entity(chain_id)
        data = dict(chain["data"])
        data["steps"][step_no]["state"] = "compensation_failed"
        data["steps"][step_no]["error"] = "simulated breakpoint"
        self.repo.update_entity(chain_id, chain["version"], "compensation_failed", data)

    def test_chain_succeeds_when_all_steps_pass(self):
        station, asset, link, incident = self.make_incident()
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
            self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore", depends_on=0),
        ]
        chain = self.run_chain(incident["id"], steps)
        self.assertEqual(chain["status"], "succeeded")
        self.assertEqual(self.service.get(asset["id"])["status"], "rebooting")
        self.assertEqual(self.service.get(link["id"])["status"], "backup_active")
        resolved = self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})
        self.assertEqual(resolved["status"], "resolved")

    def test_failed_step_compensates_dependencies(self):
        station, asset, link, incident = self.make_incident(link_status="up")
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
            self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore", depends_on=0),
        ]
        chain = self.run_chain(incident["id"], steps)
        self.assertEqual(chain["status"], "compensated")
        self.assertEqual(chain["data"]["steps"][0]["state"], "compensated")
        self.assertEqual(chain["data"]["steps"][1]["state"], "compensated")
        self.assertEqual(self.service.get(asset["id"])["status"], "faulty")
        self.assertEqual(self.service.get(link["id"])["status"], "up")
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})

    def test_resource_exhaustion(self):
        self.repo.register_resource("backup_channel", "station:s-1:backup", 1)
        station1, asset1, link1, incident1 = self.make_incident()
        station2, asset2, link2, incident2 = self.make_incident()
        resource = [{"type": "backup_channel", "key": "station:s-1:backup", "units": 1}]
        chain1 = self.run_chain(incident1["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link1["id"]}, "activate_backup", "restore", resources=resource),
        ])
        self.assertEqual(chain1["status"], "succeeded")
        chain2 = self.run_chain(incident2["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link2["id"]}, "activate_backup", "restore", resources=resource),
        ])
        self.assertEqual(chain2["status"], "compensated")
        self.assertEqual(chain2["data"]["steps"][0]["state"], "compensated")
        resources = {r["resource_key"]: r for r in self.repo.list_resources()}
        self.assertEqual(resources["station:s-1:backup"]["held"], 1)
        self.assertEqual(self.service.get(link2["id"])["status"], "down")

    def test_resource_refcount(self):
        self.repo.register_resource("backup_channel", "station:s-1:backup", 2)
        station1, asset1, link1, incident1 = self.make_incident()
        station2, asset2, link2, incident2 = self.make_incident()
        resource = [{"type": "backup_channel", "key": "station:s-1:backup", "units": 1}]
        chain1 = self.run_chain(incident1["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link1["id"]}, "activate_backup", "restore", resources=resource),
            self.step("retry_switch", {"kind": "link", "id": link1["id"]}, "activate_backup", "restore", depends_on=0),
        ])
        self.assertEqual(chain1["status"], "compensated")
        chain2 = self.run_chain(incident2["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link2["id"]}, "activate_backup", "restore", resources=resource),
        ])
        self.assertEqual(chain2["status"], "succeeded")
        resources = {r["resource_key"]: r for r in self.repo.list_resources()}
        self.assertEqual(resources["station:s-1:backup"]["held"], 1)
        self.assertEqual(self.service.get(link2["id"])["status"], "backup_active")
        self.assertEqual(self.service.get(link1["id"])["status"], "up")

    def test_first_writer_wins(self):
        station, asset, link, incident = self.make_incident()
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
        ]
        chain = self.recovery.submit_chain(self.actor, incident["id"], steps)
        self.assertEqual(chain["status"], "running")
        with self.assertRaises(ConflictError):
            self.recovery.submit_chain(self.actor, incident["id"], steps)
        executed = self.recovery.execute_chain(chain["id"])
        self.assertEqual(executed["status"], "succeeded")

    def test_idempotent_replay_returns_first_chain(self):
        station, asset, link, incident = self.make_incident()
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
        ]
        first = self.recovery.submit_chain(self.actor, incident["id"], steps, idempotency_key="idem-1")
        second = self.recovery.submit_chain(self.actor, incident["id"], steps, idempotency_key="idem-1")
        self.assertEqual(first["id"], second["id"])

    def test_concurrent_submissions_first_writer_wins(self):
        station, asset, link, incident = self.make_incident()
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
        ]
        barrier = threading.Barrier(2)
        results = []

        def submit():
            barrier.wait()
            try:
                results.append(("ok", self.recovery.submit_chain(self.actor, incident["id"], steps)))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = [status for status, _ in results]
        self.assertIn("ok", statuses)
        self.assertIn("conflict", statuses)
        chains = self.recovery.list_chains(incident_id=incident["id"])
        self.assertEqual(len(chains), 1)
        winner = [chain for _, chain in results if _ == "ok"][0]
        executed = self.recovery.execute_chain(winner["id"])
        self.assertEqual(executed["status"], "succeeded")

    def test_compensation_breakpoint_resume(self):
        self.repo.register_resource("backup_channel", "station:s-1:backup", 5)
        station, asset, link, incident = self.make_incident()
        resource = [{"type": "backup_channel", "key": "station:s-1:backup", "units": 1}]
        chain = self.run_chain(incident["id"], [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot", resources=resource),
        ])
        self.assertEqual(chain["status"], "succeeded")
        self.assertEqual(self.service.get(asset["id"])["status"], "rebooting")
        self.force_breakpoint(chain["id"], 0)
        self.repo.release_resources(chain["id"], 0)
        claims = self.repo.list_claims(chain_id=chain["id"])
        self.assertEqual(len(claims), 1)
        released_at = claims[0]["released_at"]
        self.assertIsNotNone(released_at)
        resumed = self.recovery.resume_chain(chain["id"])
        self.assertEqual(resumed["status"], "compensated")
        self.assertEqual(self.service.get(asset["id"])["status"], "faulty")
        claims = self.repo.list_claims(chain_id=chain["id"])
        self.assertEqual(claims[0]["released_at"], released_at)
        resumed_again = self.recovery.resume_chain(chain["id"])
        self.assertEqual(resumed_again["status"], "compensated")

    def test_incident_cannot_resolve_with_interrupted_chain(self):
        station, asset, link, incident = self.make_incident()
        chain = self.create_running_chain(incident["id"], [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
        ])
        self.force_breakpoint(chain["id"], 0)
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})
        resumed = self.recovery.resume_chain(chain["id"])
        self.assertEqual(resumed["status"], "compensated")
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})

    def test_recover_interrupted_on_startup(self):
        station, asset, link, incident = self.make_incident()
        chain = self.create_running_chain(incident["id"], [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
        ])
        self.assertEqual(chain["status"], "running")
        results = self.recovery.recover_interrupted()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "succeeded")
        self.assertEqual(self.service.get(asset["id"])["status"], "rebooting")

    def test_dependency_must_be_registered(self):
        station, asset, link, incident = self.make_incident()
        with self.assertRaises(ValidationError):
            self.recovery.submit_chain(self.actor, incident["id"], [
                self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot", depends_on=0),
            ])
        with self.assertRaises(ValidationError):
            self.recovery.submit_chain(self.actor, incident["id"], [
                self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
                self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore", depends_on=5),
            ])

    def test_invalid_compensation_pair_rejected(self):
        station, asset, link, incident = self.make_incident()
        with self.assertRaises(ValidationError):
            self.recovery.submit_chain(self.actor, incident["id"], [
                self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "restore"),
            ])

    def test_empty_steps_rejected(self):
        station, asset, link, incident = self.make_incident()
        with self.assertRaises(ValidationError):
            self.recovery.submit_chain(self.actor, incident["id"], [])

    def test_failed_step_releases_claimed_resources(self):
        self.repo.register_resource("backup_channel", "station:s-1:backup", 1)
        station, asset, link, incident = self.make_incident(link_status="up")
        resource = [{"type": "backup_channel", "key": "station:s-1:backup", "units": 1}]
        chain = self.run_chain(incident["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore", resources=resource),
        ])
        self.assertEqual(chain["status"], "compensated")
        self.assertEqual(chain["data"]["steps"][0]["state"], "compensated")
        resources = {r["resource_key"]: r for r in self.repo.list_resources()}
        self.assertEqual(resources["station:s-1:backup"]["held"], 0)
        claims = self.repo.list_claims(chain_id=chain["id"])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["status"], "released")

    def test_chain_can_be_retried_after_clean_terminal(self):
        station, asset, link, incident = self.make_incident(link_status="up")
        steps = [
            self.step("restart_asset", {"kind": "asset", "id": asset["id"]}, "start_reboot", "abort_reboot"),
            self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore", depends_on=0),
        ]
        chain = self.run_chain(incident["id"], steps)
        self.assertEqual(chain["status"], "compensated")
        link = self.service.transition(self.actor, link["id"], "fail", {"reason": "down again"})
        retry = self.run_chain(incident["id"], [
            self.step("switch_to_backup", {"kind": "link", "id": link["id"]}, "activate_backup", "restore"),
        ])
        self.assertEqual(retry["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
