"""Comprehensive Evaluation Harness for Dynamic Engineering Lesson Retrieval (ENG-OS-02 Phase 3D1D).

Measures disaggregated retrieval metrics against:
1. Production Holdout Dataset (tests/eval/eval_retrieval_holdout.json & tests/eval/eval_holdout_registry.md)
2. Baseline Dataset (tests/eval/eval_retrieval_scenarios.json & docs/agent/LESSONS.md)

Reports exact disaggregated metrics with raw numerators and denominators:
- Item-level precision
- Macro unconstrained retrieval recall and capacity-bounded macro recall
- Micro retrieval recall
- Mean Reciprocal Rank (MRR)
- Normalized Discounted Cumulative Gain (nDCG@3)
- Correct abstention rate
- Scenario-level exact-set accuracy and capacity-exact accuracy
- Item-level irrelevant injection rate
- Scenario-level irrelevant injection rate
- End-to-end evaluation latency
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.context_router import build_context_packet

BASELINE_SCENARIOS = WORKSPACE_ROOT / "tests" / "eval" / "eval_retrieval_scenarios.json"
BASELINE_REGISTRY = WORKSPACE_ROOT / "docs" / "agent" / "LESSONS.md"

HOLDOUT_SCENARIOS = WORKSPACE_ROOT / "tests" / "eval" / "eval_retrieval_holdout.json"
HOLDOUT_REGISTRY = WORKSPACE_ROOT / "tests" / "eval" / "eval_holdout_registry.md"


def evaluate_dataset(
    scenarios_path: Path | str,
    registry_path: Path | str,
    dataset_name: str = "Evaluation",
) -> dict[str, Any]:
    """Evaluates lesson retrieval against a labeled scenario dataset and registry."""
    s_path = Path(scenarios_path)
    r_path = Path(registry_path)

    if not s_path.is_absolute():
        s_path = WORKSPACE_ROOT / s_path
    if not r_path.is_absolute():
        r_path = WORKSPACE_ROOT / r_path

    with open(s_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    records: list[dict[str, Any]] = []
    total_retrieved_items = 0
    total_relevant_retrieved_items = 0
    total_irrelevant_retrieved_items = 0
    total_ground_truth_items = 0
    total_capacity_ground_truth_items = 0

    scenarios_with_lessons = 0
    scenarios_with_zero_lessons = 0
    scenarios_correct_abstention = 0
    scenarios_exact_match = 0
    scenarios_capacity_exact_match = 0
    scenarios_with_irrelevant = 0

    sum_reciprocal_ranks = 0.0
    sum_scenario_recall = 0.0
    sum_scenario_capacity_bounded_recall = 0.0
    sum_ndcg3 = 0.0

    t0 = time.perf_counter()

    for s in scenarios:
        sid = s["scenario_id"]
        stype = s.get("scenario_type", "GENERAL")
        prompt = s["prompt"]
        expected_list = s.get("relevant_lessons", [])
        expected = set(expected_list)

        packet = build_context_packet(prompt, registry_path=r_path)
        retrieved_list = [l["id"] for l in packet.get("relevant_lessons", [])]
        retrieved = set(retrieved_list)

        num_retrieved = len(retrieved_list)
        num_expected = len(expected)
        capacity_expected_count = min(num_expected, 3)

        total_retrieved_items += num_retrieved
        total_ground_truth_items += num_expected
        total_capacity_ground_truth_items += capacity_expected_count

        relevant_in_retrieved = len(retrieved & expected)
        irrelevant_in_retrieved = len(retrieved - expected)

        total_relevant_retrieved_items += relevant_in_retrieved
        total_irrelevant_retrieved_items += irrelevant_in_retrieved

        has_irrelevant = irrelevant_in_retrieved > 0
        if has_irrelevant:
            scenarios_with_irrelevant += 1

        is_exact_match = (retrieved == expected) and (num_retrieved == num_expected)
        if is_exact_match:
            scenarios_exact_match += 1

        is_capacity_exact = retrieved.issubset(expected) and (num_retrieved == capacity_expected_count)
        if is_capacity_exact:
            scenarios_capacity_exact_match += 1

        p = (relevant_in_retrieved / num_retrieved) if num_retrieved > 0 else (1.0 if num_expected == 0 else 0.0)

        if num_expected == 0:
            scenarios_with_zero_lessons += 1
            r = 1.0 if num_retrieved == 0 else 0.0
            r_cap = 1.0 if num_retrieved == 0 else 0.0
            mrr = 1.0 if num_retrieved == 0 else 0.0
            ndcg3 = 1.0 if num_retrieved == 0 else 0.0
            if num_retrieved == 0:
                scenarios_correct_abstention += 1
        else:
            scenarios_with_lessons += 1
            r = relevant_in_retrieved / num_expected
            r_cap = relevant_in_retrieved / capacity_expected_count
            sum_scenario_recall += r
            sum_scenario_capacity_bounded_recall += r_cap

            # Mean Reciprocal Rank (MRR)
            mrr = 0.0
            for rank, item_id in enumerate(retrieved_list, 1):
                if item_id in expected:
                    mrr = 1.0 / rank
                    break
            sum_reciprocal_ranks += mrr

            # Normalized Discounted Cumulative Gain at rank 3 (nDCG@3)
            # Binary relevance: rel_i = 1.0 if retrieved item in expected else 0.0
            dcg = 0.0
            for rank, item_id in enumerate(retrieved_list[:3], 1):
                rel = 1.0 if item_id in expected else 0.0
                dcg += rel / math.log2(rank + 1)

            idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, capacity_expected_count + 1))
            ndcg3 = (dcg / idcg) if idcg > 0.0 else (1.0 if num_retrieved == 0 else 0.0)
            sum_ndcg3 += ndcg3

        records.append({
            "scenario_id": sid,
            "scenario_type": stype,
            "expected": expected_list,
            "retrieved": retrieved_list,
            "precision": p,
            "recall": r,
            "capacity_bounded_recall": r_cap,
            "mrr": mrr,
            "ndcg3": ndcg3,
            "exact_match": is_exact_match,
            "capacity_exact_match": is_capacity_exact,
            "has_irrelevant": has_irrelevant,
        })

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    latency_per_query_ms = elapsed_ms / len(scenarios) if scenarios else 0.0

    # Disaggregated metric calculations
    item_precision = total_relevant_retrieved_items / total_retrieved_items if total_retrieved_items > 0 else 1.0
    macro_recall = sum_scenario_recall / scenarios_with_lessons if scenarios_with_lessons > 0 else 1.0
    macro_capacity_recall = sum_scenario_capacity_bounded_recall / scenarios_with_lessons if scenarios_with_lessons > 0 else 1.0
    micro_recall = total_relevant_retrieved_items / total_ground_truth_items if total_ground_truth_items > 0 else 1.0
    micro_capacity_recall = total_relevant_retrieved_items / total_capacity_ground_truth_items if total_capacity_ground_truth_items > 0 else 1.0
    mrr_val = sum_reciprocal_ranks / scenarios_with_lessons if scenarios_with_lessons > 0 else 1.0
    ndcg3_val = sum_ndcg3 / scenarios_with_lessons if scenarios_with_lessons > 0 else 1.0
    abstention_rate = scenarios_correct_abstention / scenarios_with_zero_lessons if scenarios_with_zero_lessons > 0 else 1.0
    exact_set_accuracy = scenarios_exact_match / len(scenarios) if scenarios else 1.0
    capacity_exact_accuracy = scenarios_capacity_exact_match / len(scenarios) if scenarios else 1.0
    item_irrelevant_rate = total_irrelevant_retrieved_items / total_retrieved_items if total_retrieved_items > 0 else 0.0
    scenario_irrelevant_rate = scenarios_with_irrelevant / len(scenarios) if scenarios else 0.0

    return {
        "dataset_name": dataset_name,
        "scenarios_path": str(s_path),
        "registry_path": str(r_path),
        "total_scenarios": len(scenarios),
        "scenarios_with_lessons": scenarios_with_lessons,
        "scenarios_with_zero_lessons": scenarios_with_zero_lessons,
        "timing": {
            "total_elapsed_ms": elapsed_ms,
            "latency_per_query_ms": latency_per_query_ms,
        },
        "metrics": {
            "item_level_precision": {
                "value": item_precision,
                "numerator": total_relevant_retrieved_items,
                "denominator": total_retrieved_items,
                "display": f"{total_relevant_retrieved_items}/{total_retrieved_items} ({item_precision:.4f})",
            },
            "retrieval_recall_macro": {
                "value": macro_recall,
                "numerator": round(sum_scenario_recall, 4),
                "denominator": scenarios_with_lessons,
                "display": f"{sum_scenario_recall:.2f}/{scenarios_with_lessons} ({macro_recall:.4f})",
            },
            "retrieval_recall_capacity_bounded_macro": {
                "value": macro_capacity_recall,
                "numerator": round(sum_scenario_capacity_bounded_recall, 4),
                "denominator": scenarios_with_lessons,
                "display": f"{sum_scenario_capacity_bounded_recall:.2f}/{scenarios_with_lessons} ({macro_capacity_recall:.4f})",
            },
            "retrieval_recall_micro": {
                "value": micro_recall,
                "numerator": total_relevant_retrieved_items,
                "denominator": total_ground_truth_items,
                "display": f"{total_relevant_retrieved_items}/{total_ground_truth_items} ({micro_recall:.4f})",
            },
            "retrieval_recall_capacity_bounded_micro": {
                "value": micro_capacity_recall,
                "numerator": total_relevant_retrieved_items,
                "denominator": total_capacity_ground_truth_items,
                "display": f"{total_relevant_retrieved_items}/{total_capacity_ground_truth_items} ({micro_capacity_recall:.4f})",
            },
            "mean_reciprocal_rank_mrr": {
                "value": mrr_val,
                "numerator": round(sum_reciprocal_ranks, 4),
                "denominator": scenarios_with_lessons,
                "display": f"{sum_reciprocal_ranks:.2f}/{scenarios_with_lessons} ({mrr_val:.4f})",
            },
            "normalized_discounted_cumulative_gain_ndcg3": {
                "value": ndcg3_val,
                "numerator": round(sum_ndcg3, 4),
                "denominator": scenarios_with_lessons,
                "display": f"{sum_ndcg3:.2f}/{scenarios_with_lessons} ({ndcg3_val:.4f})",
            },
            "correct_abstention_rate": {
                "value": abstention_rate,
                "numerator": scenarios_correct_abstention,
                "denominator": scenarios_with_zero_lessons,
                "display": f"{scenarios_correct_abstention}/{scenarios_with_zero_lessons} ({abstention_rate:.4f})",
            },
            "scenario_exact_set_accuracy": {
                "value": exact_set_accuracy,
                "numerator": scenarios_exact_match,
                "denominator": len(scenarios),
                "display": f"{scenarios_exact_match}/{len(scenarios)} ({exact_set_accuracy:.4f})",
            },
            "scenario_capacity_exact_accuracy": {
                "value": capacity_exact_accuracy,
                "numerator": scenarios_capacity_exact_match,
                "denominator": len(scenarios),
                "display": f"{scenarios_capacity_exact_match}/{len(scenarios)} ({capacity_exact_accuracy:.4f})",
            },
            "item_irrelevant_injection_rate": {
                "value": item_irrelevant_rate,
                "numerator": total_irrelevant_retrieved_items,
                "denominator": total_retrieved_items,
                "display": f"{total_irrelevant_retrieved_items}/{total_retrieved_items} ({item_irrelevant_rate:.4f})",
            },
            "scenario_irrelevant_injection_rate": {
                "value": scenario_irrelevant_rate,
                "numerator": scenarios_with_irrelevant,
                "denominator": len(scenarios),
                "display": f"{scenarios_with_irrelevant}/{len(scenarios)} ({scenario_irrelevant_rate:.4f})",
            },
        },
        "per_scenario": records,
    }


def print_evaluation_report(results: dict[str, Any]) -> None:
    name = results["dataset_name"]
    total = results["total_scenarios"]
    metrics = results["metrics"]
    timing = results["timing"]

    print("=" * 80)
    print(f" {name.upper()} RETRIEVAL EVALUATION REPORT ({total} Scenarios)")
    print("=" * 80)
    print(f"Scenarios Dataset: {results['scenarios_path']}")
    print(f"Lessons Registry:  {results['registry_path']}")
    print(f"Execution Latency: {timing['total_elapsed_ms']:.2f} ms total ({timing['latency_per_query_ms']:.3f} ms/query)")
    print("-" * 80)
    print("DISAGGREGATED EVALUATION METRICS:")
    print(f"  A. Item-Level Retrieval Precision:              {metrics['item_level_precision']['display']}")
    print(f"  B. Retrieval Recall (Macro Unconstrained):      {metrics['retrieval_recall_macro']['display']}")
    print(f"     Retrieval Recall (Capacity-Bounded Macro):   {metrics['retrieval_recall_capacity_bounded_macro']['display']}")
    print(f"     Retrieval Recall (Micro Unconstrained):      {metrics['retrieval_recall_micro']['display']}")
    print(f"     Retrieval Recall (Capacity-Bounded Micro):   {metrics['retrieval_recall_capacity_bounded_micro']['display']}")
    print(f"  C. Mean Reciprocal Rank (MRR, k>=1):            {metrics['mean_reciprocal_rank_mrr']['display']}")
    print(f"     Normalized Discounted Cum Gain (nDCG@3):     {metrics['normalized_discounted_cumulative_gain_ndcg3']['display']}")
    print(f"  D. Correct Abstention Rate (k=0):               {metrics['correct_abstention_rate']['display']}")
    print(f"  E. Scenario Exact-Set Accuracy:                 {metrics['scenario_exact_set_accuracy']['display']}")
    print(f"     Scenario Capacity-Exact Accuracy:            {metrics['scenario_capacity_exact_accuracy']['display']}")
    print(f"  F. Item-Level Irrelevant Injection Rate:        {metrics['item_irrelevant_injection_rate']['display']}")
    print(f"     Scenario Irrelevant Injection Rate:          {metrics['scenario_irrelevant_injection_rate']['display']}")
    print("-" * 80)
    print(f"{'Scenario ID':<13} {'Type':<33} {'Expected':<16} {'Retrieved':<16} {'P':<6} {'R':<6} {'R_cap':<6} {'MRR':<6} {'nDCG':<6}")
    print("-" * 80)
    for r in results["per_scenario"]:
        exp_str = ",".join(r["expected"]) if r["expected"] else "NONE"
        ret_str = ",".join(r["retrieved"]) if r["retrieved"] else "NONE"
        print(
            f"{r['scenario_id']:<13} {r['scenario_type']:<33} {exp_str:<16} {ret_str:<16} "
            f"{r['precision']:<6.2f} {r['recall']:<6.2f} {r['capacity_bounded_recall']:<6.2f} {r['mrr']:<6.2f} {r['ndcg3']:<6.2f}"
        )
    print("=" * 80)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate engineering lesson retrieval")
    parser.add_argument("--scenarios", type=str, default=None, help="Path to scenario JSON file")
    parser.add_argument("--registry", type=str, default=None, help="Path to registry Markdown file")
    parser.add_argument("--baseline", action="store_true", help="Run only baseline 10-scenario evaluation")
    parser.add_argument("--holdout", action="store_true", help="Run only holdout 28-scenario evaluation")
    parser.add_argument("--json", action="store_true", help="Output raw JSON format")
    args = parser.parse_args()

    all_results = []

    if args.scenarios and args.registry:
        res = evaluate_dataset(args.scenarios, args.registry, dataset_name="Custom")
        all_results.append(res)
    elif args.baseline:
        res = evaluate_dataset(BASELINE_SCENARIOS, BASELINE_REGISTRY, dataset_name="Baseline (Phase 3D1)")
        all_results.append(res)
    elif args.holdout:
        res = evaluate_dataset(HOLDOUT_SCENARIOS, HOLDOUT_REGISTRY, dataset_name="Holdout Benchmark (Phase 3D1D)")
        all_results.append(res)
    else:
        # Default: run both holdout benchmark and baseline
        res_holdout = evaluate_dataset(HOLDOUT_SCENARIOS, HOLDOUT_REGISTRY, dataset_name="Holdout Benchmark (Phase 3D1D)")
        res_baseline = evaluate_dataset(BASELINE_SCENARIOS, BASELINE_REGISTRY, dataset_name="Baseline (Phase 3D1)")
        all_results.extend([res_holdout, res_baseline])

    if args.json:
        print(json.dumps(all_results, indent=2))
    else:
        for r in all_results:
            print_evaluation_report(r)

    # Verification gates: fail closed if any quality or safety invariant is violated
    for r in all_results:
        m = r["metrics"]
        if m["item_level_precision"]["value"] < 1.0:
            sys.stderr.write(f"Validation failure: Item precision < 1.0 ({m['item_level_precision']['value']})\n")
            return 1
        if m["item_irrelevant_injection_rate"]["value"] > 0.0:
            sys.stderr.write(f"Validation failure: Item irrelevant rate > 0.0 ({m['item_irrelevant_injection_rate']['value']})\n")
            return 1
        if m["scenario_irrelevant_injection_rate"]["value"] > 0.0:
            sys.stderr.write(f"Validation failure: Scenario irrelevant rate > 0.0 ({m['scenario_irrelevant_injection_rate']['value']})\n")
            return 1
        if m["correct_abstention_rate"]["value"] < 1.0:
            sys.stderr.write(f"Validation failure: Abstention rate < 1.0 ({m['correct_abstention_rate']['value']})\n")
            return 1
        if m["retrieval_recall_capacity_bounded_macro"]["value"] < 1.0:
            sys.stderr.write(f"Validation failure: Capacity-bounded macro recall < 1.0 ({m['retrieval_recall_capacity_bounded_macro']['value']})\n")
            return 1
        if m["normalized_discounted_cumulative_gain_ndcg3"]["value"] < 1.0:
            sys.stderr.write(f"Validation failure: nDCG@3 < 1.0 ({m['normalized_discounted_cumulative_gain_ndcg3']['value']})\n")
            return 1
        if m["scenario_capacity_exact_accuracy"]["value"] < 1.0:
            sys.stderr.write(f"Validation failure: Capacity exact accuracy < 1.0 ({m['scenario_capacity_exact_accuracy']['value']})\n")
            return 1
        if m["retrieval_recall_macro"]["value"] < 0.98:
            sys.stderr.write(f"Validation failure: Macro recall < 0.98 ({m['retrieval_recall_macro']['value']})\n")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
