"""Load and verify split fixtures before any strategy receives context."""

import hashlib
import json
from pathlib import Path

from .models import DatasetManifest, DecisionFixture, OutcomeFixture


class FixtureIntegrityError(ValueError):
    pass


class FixtureSet:
    def __init__(self, manifest: DatasetManifest, decisions: list[DecisionFixture], outcomes: dict[str, OutcomeFixture]):
        self.manifest = manifest
        self.decisions = decisions
        self._outcomes = outcomes

    def reveal_outcome(self, scenario_id: str, after) -> OutcomeFixture:
        outcome = self._outcomes[scenario_id]
        decision = next(item for item in self.decisions if item.scenario_id == scenario_id)
        if after < decision.decision_at:
            raise FixtureIntegrityError("outcome cannot be revealed before the decision is complete")
        return outcome


def _read_verified(root: Path, spec) -> list[dict]:
    path = root / spec.path
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec.sha256:
        raise FixtureIntegrityError(f"fixture hash mismatch: {spec.path}")
    return json.loads(raw)


def load_dataset(root: Path) -> FixtureSet:
    manifest = DatasetManifest.model_validate_json((root / "manifest.json").read_text())
    decisions = [DecisionFixture.model_validate(item) for item in _read_verified(root, manifest.decision_fixtures)]
    outcomes_list = [OutcomeFixture.model_validate(item) for item in _read_verified(root, manifest.outcome_fixtures)]
    decision_ids = [item.scenario_id for item in decisions]
    outcome_ids = [item.scenario_id for item in outcomes_list]
    if len(decision_ids) != manifest.scenario_count or len(set(decision_ids)) != len(decision_ids):
        raise FixtureIntegrityError("decision fixture count or IDs do not match the manifest")
    if set(decision_ids) != set(outcome_ids) or len(outcome_ids) != len(set(outcome_ids)):
        raise FixtureIntegrityError("decision and outcome scenario IDs differ")
    outcomes = {item.scenario_id: item for item in outcomes_list}
    for decision in decisions:
        outcome = outcomes[decision.scenario_id]
        if outcome.outcome_at <= decision.decision_at:
            raise FixtureIntegrityError(f"outcome for {decision.scenario_id} is not after its cutoff")
        if set(outcome.prices) != set(decision.prices):
            raise FixtureIntegrityError(f"outcome symbols differ for {decision.scenario_id}")
    return FixtureSet(manifest, decisions, outcomes)
