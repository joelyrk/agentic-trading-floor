import re
from pathlib import Path

from backend.api import app
from backend.product import ExperimentRequest, ProductService, ReplayRequest

DATASET = Path("evals/datasets/historical-v1")


def service(tmp_path) -> ProductService:
    return ProductService(
        str(tmp_path / "product.db"),
        dataset_root=DATASET,
        results_root=tmp_path / "results",
    )


def test_replay_catalog_never_exposes_outcomes(tmp_path) -> None:
    catalog = service(tmp_path).scenarios()
    assert catalog["dataset"]["schema_version"] == "1.0"
    assert len(catalog["scenarios"]) == 30
    assert all(item["outcome_available"] is False for item in catalog["scenarios"])
    assert all("outcome" not in item and "outcome_at" not in item for item in catalog["scenarios"])


def test_replay_requires_separate_reveal_and_retry_preserves_reveal(tmp_path) -> None:
    product = service(tmp_path)
    request = ReplayRequest(scenario_id="hist-2014-01", strategy="multi_agent", seed=7)
    decision = product.create_replay(request)
    assert decision["status"] == "decision_complete"
    assert decision["outcome"] is None
    assert decision["paper_trading_only"] is True

    revealed = product.reveal(decision["replay_id"])
    assert revealed["status"] == "outcome_revealed"
    assert set(revealed["outcome"]) >= {
        "outcome_at",
        "portfolio_return",
        "benchmark_return",
        "benchmark_relative_return",
    }
    retry = product.create_replay(request)
    assert retry == revealed


def test_experiments_compare_prompts_models_and_architectures(tmp_path) -> None:
    product = service(tmp_path)
    product.run_experiment(ExperimentRequest(model="model-a", prompt_version="prompt-a"))
    product.run_experiment(ExperimentRequest(model="model-b", prompt_version="prompt-b"))
    reports = product.experiments()
    assert {report["metadata"]["model"] for report in reports} == {"model-a", "model-b"}
    assert {report["metadata"]["prompt_version"] for report in reports} == {
        "prompt-a",
        "prompt-b",
    }
    assert all(
        {"single_agent", "multi_agent"} <= {result["strategy"] for result in report["results"]}
        for report in reports
    )
    assert all(report["leakage_checks_passed"] is True for report in reports)


def test_frontend_api_routes_match_versioned_openapi_contract() -> None:
    """Fail when a literal frontend API call no longer has the same backend method/path."""
    schema = app.openapi()
    assert schema["info"]["version"] == "1.0.0"
    frontend = Path("frontend/src/api.ts").read_text()
    calls = re.findall(r'\b(get|post)(?:<[^>]+>)?\(["`](/api/[^"?`]+)', frontend)
    assert calls
    for method, client_path in calls:
        normalized = re.sub(r"\$\{[^}]+\}", "{parameter}", client_path)
        matching_paths = [
            route
            for route in schema["paths"]
            if re.sub(r"\{[^}]+\}", "{parameter}", route) == normalized
        ]
        assert matching_paths, (
            f"frontend route missing from OpenAPI: {method.upper()} {client_path}"
        )
        assert method in schema["paths"][matching_paths[0]], (
            f"frontend method missing from OpenAPI: {method.upper()} {client_path}"
        )
