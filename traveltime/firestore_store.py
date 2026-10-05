"""Durable cloud store. No credentials, addresses or route geometry are stored."""
from datetime import timezone
import hashlib
import re
from zoneinfo import ZoneInfo

from google.cloud import firestore

from .db import result_fields


def experiment_id(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", value):
        raise ValueError("CLOUD_EXPERIMENT must be 1–63 letters, digits, underscores or hyphens.")
    return value


class FirestoreStore:
    def __init__(self, project=None, experiment="commute-v1", *, client=None):
        self.client = client if client is not None else firestore.Client(
            project=project, database="(default)")
        self.experiment = experiment_id(experiment)
        self.root = self.client.collection("commute_experiments").document(self.experiment)

    def sample_ref(self, identity):
        # A slash in a future manual ID must never change a document path.
        return self.root.collection("samples").document(
            hashlib.sha256(identity.encode()).hexdigest())

    def claim(self, config, slot, queried, *, manual_id=None):
        utc = queried.astimezone(timezone.utc)
        month = utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")
        identity = manual_id or slot.key
        sample = self.sample_ref(identity)
        # Shared across experiments in this project; changing a route does not reset usage.
        budget = self.client.collection("commute_request_budgets").document(month)
        fingerprint = config.fingerprint()

        @firestore.transactional
        def reserve(transaction):
            metadata = self.root.get(transaction=transaction).to_dict() or {}
            if metadata and metadata.get("route_fingerprint") != fingerprint:
                raise ValueError("Route/interval changed. Choose a new CLOUD_EXPERIMENT.")
            if sample.get(transaction=transaction).exists:
                return "duplicate"
            usage = budget.get(transaction=transaction).to_dict() or {}
            used = int(usage.get("attempts", 0))
            status = "pending" if used < config.monthly_request_limit else "budget_skipped"
            # All reads precede all writes. Never perform HTTP work in a transaction.
            if not metadata:
                transaction.set(self.root, {
                    "route_fingerprint": fingerprint,
                    "interval_minutes": str(config.interval_minutes),
                })
            transaction.set(sample, {
                "slot_id": identity, "scheduled_utc": slot.key,
                "queried_utc": utc.isoformat(), "local_date": slot.when.date().isoformat(),
                "direction": slot.direction, "kind": "manual" if manual_id else "scheduled",
                "status": status, "billing_month": month,
                "quota_reserved": status == "pending",
            })
            if status == "pending":
                transaction.set(budget, {"attempts": used + 1})
            return status

        return reserve(self.client.transaction(max_attempts=3))

    def complete(self, identity, routes, ndw):
        self.sample_ref(identity).update(result_fields(routes, ndw))

    def fail(self, identity, code):
        self.sample_ref(identity).update({"status": "error", "error_code": code})

    def snapshot(self):
        metadata = self.root.get().to_dict() or {}
        # Scheduled ISO timestamps all use UTC and therefore sort chronologically.
        rows = [doc.to_dict() for doc in
                self.root.collection("samples").order_by("scheduled_utc").stream()]
        return metadata, rows
