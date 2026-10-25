"""local-compatible datasets and evaluator contracts for canonical Genesis agents."""

from .datasets import CATEGORIES, load_cases, validate_case, validate_dataset
from .evaluators import evaluate_case

__all__ = ["CATEGORIES", "evaluate_case", "load_cases", "validate_case", "validate_dataset"]
