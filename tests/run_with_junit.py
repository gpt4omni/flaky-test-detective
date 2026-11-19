#!/usr/bin/env python3
"""Minimal zero-dependency unittest runner that emits JUnit XML + shard metadata."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


def _collect_tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(_collect_tests(item))
        else:
            cases.append(item)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit-out", required=True)
    parser.add_argument("--shard-index", type=int, default=1)
    parser.add_argument("--shard-total", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    loader = unittest.TestLoader()
    discovered = loader.discover(str(Path(__file__).resolve().parent))
    all_cases = sorted(
        _collect_tests(discovered),
        key=lambda c: (c.__class__.__module__, c.__class__.__name__, c._testMethodName),
    )

    shard_cases = [
        c
        for idx, c in enumerate(all_cases)
        if (idx % args.shard_total) == (args.shard_index - 1)
    ]
    random.Random(args.seed).shuffle(shard_cases)

    suite_el = ET.Element(
        "testsuite",
        name=f"shard-{args.shard_index}",
        tests=str(len(shard_cases)),
    )

    failures = 0
    errors = 0
    for case in shard_cases:
        res = unittest.TestResult()
        t0 = time.monotonic()
        case.run(res)
        elapsed = time.monotonic() - t0

        classname = f"{case.__class__.__module__}.{case.__class__.__name__}"
        name = case._testMethodName
        tc_el = ET.SubElement(
            suite_el,
            "testcase",
            classname=classname,
            name=name,
            time=f"{elapsed:.4f}",
        )
        if res.failures:
            failures += 1
            _, tb = res.failures[0]
            f_el = ET.SubElement(tc_el, "failure", message=tb.splitlines()[-1])
            f_el.text = tb
        elif res.errors:
            errors += 1
            _, tb = res.errors[0]
            e_el = ET.SubElement(tc_el, "error", message=tb.splitlines()[-1])
            e_el.text = tb

    suite_el.attrib["failures"] = str(failures)
    suite_el.attrib["errors"] = str(errors)

    out_path = Path(args.junit_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite_el).write(out_path, encoding="utf-8", xml_declaration=True)

    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps(
            {
                "commit_sha": os.environ.get("GITHUB_SHA", "local"),
                "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
                "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                "shard": f"shard-{args.shard_index}",
                "runner_id": os.environ.get("RUNNER_NAME", "local-runner"),
                "seed": str(args.seed),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"[shard-{args.shard_index}/{args.shard_total}] ran {len(shard_cases)} test(s), "
        f"failures={failures}, errors={errors} -> {out_path}"
    )
    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
