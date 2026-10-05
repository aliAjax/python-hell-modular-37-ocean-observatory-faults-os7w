import sqlite3
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError


class RecoveryService:
    """Saga-style orchestration for incident recovery chains.

    A chain is registered per incident with an ordered list of steps. Each step
    declares the previous step it depends on and the shared resources it
    occupies. Registration and execution are separate: two operators submitting
    the same incident's chain at the same time race on the insert, and the
    partial unique index lets the first one win while the chain stays in
    ``running`` state. Forward execution runs steps in order; on failure,
    already effective steps are compensated in reverse. Resource claims are
    released by reference count, so claims held by other incidents are never
    released by a single compensation. Compensation is idempotent and can resume
    from its breakpoint after a restart.
    """

    def __init__(self, repository, rules, domain_service):
        self.repository = repository
        self.rules = rules
        self.domain = domain_service
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _actor_for(self, entity):
        return Actor(entity["data"]["created_by"], entity["data"]["created_by_role"])

    def _view(self, entity):
        entity = dict(entity)
        entity["claims"] = self.repository.list_claims(chain_id=entity["id"])
        return entity

    def get_chain(self, chain_id):
        entity = self.repository.get_entity(chain_id)
        if not entity or entity["kind"] != "recovery_chain":
            raise NotFoundError("recovery chain not found: " + chain_id)
        return self._view(entity)

    def list_chains(self, incident_id=None):
        entities = self.repository.list_entities(kind="recovery_chain")
        if incident_id:
            entities = [e for e in entities if e["data"].get("incident_id") == incident_id]
        return [self._view(e) for e in entities]

    def submit_chain(self, actor, incident_id, steps, idempotency_key=None):
        """Register a chain in running state. Does not execute it."""
        payload = {"incident_id": incident_id, "steps": steps, "error": None}
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity and entity["kind"] == "recovery_chain":
                    return self._view(entity)
        self.rules.validate_create(actor, "recovery_chain", payload, self._lookup)
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
        payload["steps"] = normalized
        payload["created_by"] = actor.user_id
        payload["created_by_role"] = actor.role
        chain_id = str(uuid4())
        try:
            entity = self.repository.create_entity(
                chain_id, "recovery_chain", "running", payload, actor.user_id
            )
        except sqlite3.IntegrityError:
            raise ConflictError("recovery chain already exists for incident: " + str(incident_id))
        self.audit.record(
            chain_id, actor, "chain_submitted", None, "running",
            {"incident_id": incident_id, "steps": len(normalized)},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, chain_id)
        return self._view(entity)

    def execute_chain(self, chain_id):
        """Run a registered chain to completion or compensation."""
        entity = self.repository.get_entity(chain_id)
        if not entity or entity["kind"] != "recovery_chain":
            raise NotFoundError("recovery chain not found: " + chain_id)
        if entity["status"] in ("succeeded", "compensated"):
            return self._view(entity)
        if entity["status"] in ("compensating", "compensation_failed"):
            return self._compensate(chain_id)
        return self._run(chain_id)

    def resume_chain(self, chain_id):
        """Resume a chain interrupted at a compensation breakpoint."""
        entity = self.repository.get_entity(chain_id)
        if not entity or entity["kind"] != "recovery_chain":
            raise NotFoundError("recovery chain not found: " + chain_id)
        if entity["status"] in ("succeeded", "compensated"):
            return self._view(entity)
        if entity["status"] == "running":
            return self._run(chain_id)
        return self._compensate(chain_id)

    def recover_interrupted(self):
        """Execute all running chains and resume all interrupted chains."""
        entities = self.repository.list_entities(kind="recovery_chain")
        results = []
        for entity in entities:
            if entity["status"] == "running":
                results.append(self._run(entity["id"]))
            elif entity["status"] in ("compensating", "compensation_failed"):
                results.append(self._compensate(entity["id"]))
        return results

    def _run(self, chain_id):
        entity = self.repository.get_entity(chain_id)
        if entity["status"] in ("succeeded", "compensated"):
            return self._view(entity)
        if entity["status"] in ("compensating", "compensation_failed"):
            return self._compensate(chain_id)
        actor = self._actor_for(entity)
        data = dict(entity["data"])
        steps = data["steps"]
        for step in steps:
            if step["state"] in ("succeeded", "compensated"):
                continue
            if step["state"] == "failed":
                return self._compensate(chain_id)
            if step["depends_on"] is not None:
                dependency = steps[step["depends_on"]]
                if dependency["state"] != "succeeded":
                    raise ConflictError(
                        "step %s depends on step %s which is not succeeded"
                        % (step["step_no"], step["depends_on"])
                    )
            try:
                self._execute_step(chain_id, step, actor)
            except Exception as exc:
                step["state"] = "failed"
                step["error"] = str(exc)
                data["error"] = "step %s failed: %s" % (step["step_no"], exc)
                self._save(entity, "compensating", data)
                self.audit.record(
                    chain_id, actor, "step_failed", "running", "compensating",
                    {"step": step["step_no"], "error": str(exc)},
                )
                return self._compensate(chain_id)
        self._save(entity, "succeeded", data)
        self.audit.record(chain_id, actor, "chain_succeeded", "running", "succeeded", {})
        return self._view(self.repository.get_entity(chain_id))

    def _execute_step(self, chain_id, step, actor):
        self.repository.claim_resources(chain_id, step["step_no"], step["resources"])
        target = self.repository.get_entity(step["target"]["id"])
        if not target:
            raise NotFoundError("step target not found: " + step["target"]["id"])
        self.domain.transition(
            actor, target["id"], step["action"], {}, expected_version=target["version"]
        )
        step["state"] = "succeeded"
        step["error"] = None
        self.audit.record(
            chain_id, actor, "step_succeeded", None, "succeeded",
            {"step": step["step_no"], "action": step["action"], "target": target["id"]},
        )

    def _compensate(self, chain_id):
        entity = self.repository.get_entity(chain_id)
        actor = self._actor_for(entity)
        data = dict(entity["data"])
        steps = data["steps"]
        for step in reversed(steps):
            if step["state"] not in ("succeeded", "compensation_failed", "failed"):
                continue
            try:
                self.repository.release_resources(chain_id, step["step_no"])
                if step["state"] == "failed":
                    step["state"] = "compensated"
                    step["error"] = None
                    self.audit.record(
                        chain_id, actor, "step_compensated", None, "compensated",
                        {"step": step["step_no"], "compensation": "released"},
                    )
                    continue
                target = self.repository.get_entity(step["target"]["id"])
                if not target:
                    raise NotFoundError("step target not found: " + step["target"]["id"])
                restored = self.rules.TRANSITIONS[target["kind"]][step["compensation"]][1]
                if target["status"] != restored:
                    self.domain.transition(
                        actor, target["id"], step["compensation"], {},
                        expected_version=target["version"],
                    )
                step["state"] = "compensated"
                step["error"] = None
                self.audit.record(
                    chain_id, actor, "step_compensated", None, "compensated",
                    {"step": step["step_no"], "compensation": step["compensation"]},
                )
            except Exception as exc:
                step["state"] = "compensation_failed"
                step["error"] = str(exc)
                data["error"] = "compensation of step %s failed: %s" % (step["step_no"], exc)
                self._save(entity, "compensation_failed", data)
                self.audit.record(
                    chain_id, actor, "compensation_failed", "compensating", "compensation_failed",
                    {"step": step["step_no"], "error": str(exc)},
                )
                return self._view(self.repository.get_entity(chain_id))
        self._save(entity, "compensated", data)
        self.audit.record(chain_id, actor, "chain_compensated", "compensating", "compensated", {})
        return self._view(self.repository.get_entity(chain_id))

    def _save(self, entity, status, data):
        self.repository.update_entity(entity["id"], entity["version"], status, data)
