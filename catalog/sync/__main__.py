from __future__ import annotations

import argparse
from pathlib import Path

from .openrouter import CatalogPolicy, sync_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize the approved OpenRouter catalog")
    parser.add_argument("--policy", default="catalog/catalog-policy.yaml")
    parser.add_argument("--output", default="catalog/generated/approved-models.json")
    args = parser.parse_args()
    result = sync_catalog(CatalogPolicy.from_file(args.policy), Path(args.output))
    action = "reused" if result.stale else "wrote"
    source = "fresh cache after a catalog error" if result.stale else "live catalog"
    print(f"{action} {len(result.models)} approved models from {source}")


if __name__ == "__main__":
    main()
