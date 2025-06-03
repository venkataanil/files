#!/usr/bin/env python

import os
import argparse
import json
import glob
from typing import List, Pattern
import re
import tempfile
import csv
import io
import boto3
import uuid
from collections import defaultdict, Counter
from typing import Dict, List, Any


# Constants
GLOBAL_KEYS = ['metadata', 'uuid']
DROP_LIST = ['metadata','uuid','metricName','labels','query', 'value', 'jobName', 'timestamp']
LABELS_LIST = ["mode", "verb", "namespace", "resource", "container", "component", "endpoint"]
DEFAULT_HASH = "xyz"
CHUNK_SIZE = 10

def strhash(value: Any) -> str:
    """Recursively generate a stable string hash from a nested dict or value."""
    if isinstance(value, dict):
        return ''.join(f"{key}:{strhash(value[key])}" for key in sorted(value))
    return str(value)


def parse_arguments() -> argparse.Namespace:
    """Arguments parser"""
    parser = argparse.ArgumentParser(description="Kube-burner report normalizer")
    parser.add_argument("-p", "--path", required=False, default=".", help="Directory containing JSON metric files")
    parser.add_argument("-o", "--outfile", required=False, help="Output file to write merged metrics")
    parser.add_argument("-i", "--indent", type=int, default=8, help="Indentation level for JSON output")
    parser.add_argument("-e", "--excludeMetrics", required=False, default="LatencyMeasurement,-start,jobSummary,nodeRoles,nodeStatus,alert", help="Comma-separated regex patterns to exclude metrics")
    return parser.parse_args()


def compile_exclude_patterns(patterns_str: str) -> List[Pattern]:
    """Compiles the patterns to be excluded"""
    if not patterns_str:
        return []
    return [re.compile(pattern.strip()) for pattern in patterns_str.split(",")]


def should_exclude(metric_name: str, patterns: List[Pattern]) -> bool:
    """Return a boolean on exclusion decision"""
    return any(p.search(metric_name) for p in patterns)


def remove_keys_by_patterns(data: Dict, patterns: List[str]) -> Dict:
    """Removes keys in a dict based on regex list"""
    regexes = [re.compile(p) for p in patterns]
    return {
        k: v for k, v in data.items()
        if not any(r.match(k) for r in regexes)
    }


def load_json_file(filepath: str) -> dict:
    """Load a json file"""
    try:
        with open(filepath, "r") as file:
            return json.load(file)
    except json.JSONDecodeError:
        print(f"Warning: Failed to decode JSON from file: {filepath}")
        return None


def extract_job_config(job_summaries: dict) -> [dict, dict]:
    """Extracts job summary"""
    for summary in job_summaries:
        if summary.get("jobConfig", {}).get("name") != "garbage-collection":
            job_config = summary.pop("jobConfig", None)
            return job_config, summary
    return None, None


def process_json_file(filepath: str, skip_patterns: List[Pattern], output: Dict) -> None:
    """Processes JSON files and generates a huge json with minimal data"""
    with open(filepath, "r") as file:
        try:
            entries = json.load(file)
        except json.JSONDecodeError:
            print(f"Warning: Failed to decode JSON from file: {filepath}")
            return

        if not entries:
            return

        metric_name = entries[0].get("metricName")
        if not metric_name:
            print(f"Warning: 'metricName' missing in first entry of {filepath}")
            return

        if should_exclude(metric_name, skip_patterns):
            return

        grouped_metrics = {}
        for entry in entries:
            # Skip the metircs during churn phase to avoid noise
            if 'churnMetric' in entry:
                continue
            # Skip metrics during garbage collection as well to avoid noise
            if 'jobName' in entry and entry['jobName'].lower() == 'garbage-collection':
                continue
            label_hash = DEFAULT_HASH
            labels = entry.get("labels")
            if labels:
                label_hash = strhash(labels)

            if label_hash not in grouped_metrics:
                grouped_metrics[label_hash] = {"value": 0.0}
                if labels:
                    grouped_metrics[label_hash]["labels"] = {k: labels[k] for k in LABELS_LIST if k in labels}

            # Drop unneeded fields
            if "value" in entry:
                # reduces value to average
                entry = {"value": entry["value"]}
                grouped_metrics[label_hash]["value"] += entry["value"]/2
            else:
                # handles cases where metrics don't have value. for example, quantiles
                for k in DROP_LIST:
                    entry.pop(k,None)
                # Need to deal with this edge case as we set {"value": 0.0} as default above
                if isinstance(grouped_metrics[label_hash]["value"], (int, float)):
                    grouped_metrics[label_hash].pop("value", None)
                if "value" not in grouped_metrics[label_hash]:
                    grouped_metrics[label_hash]["value"] = [entry]
                else:
                    grouped_metrics[label_hash]["value"].append(entry)

        # Adds up condensed data values to output json
        if metric_name in output["metrics"]:
            output["metrics"][metric_name].extend(grouped_metrics.values())
        else:
            output["metrics"][metric_name] = list(grouped_metrics.values())


def normalize_metrics(metrics: dict) -> dict:
    """Intermidiate normalization step to further reduce the json"""

    # Labels precedence order used for nesting
    nest_order = ["mode", "verb", "namespace", "component", "resource", "container", "endpoint"]
    nested_metrics = {}

    for metric, entries in metrics:
        nested_metrics.setdefault(metric, {})

        for entry in entries:
            labels = entry.get("labels", {})
            value = entry["value"]

            # Get available keys from labels, in nest_order
            label_keys = [k for k in nest_order if k in labels]
            if not label_keys:
                # No labels at all, store directly under metric
                existing = nested_metrics[metric]
                if isinstance(existing, (int, float)):
                    nested_metrics[metric] = (existing + value) / 2
                elif isinstance(existing, dict):
                    if "_value" in nested_metrics[metric]:
                        nested_metrics[metric]["_value"] = (nested_metrics[metric]["_value"] + value) / 2
                    else:
                        nested_metrics[metric]["_value"] = value
                else:
                    nested_metrics[metric] = value
                continue

            curr = nested_metrics[metric]
            for _, key in enumerate(label_keys):
                # logc to generate nested keys with labels
                key_value = labels[key]
                group_key = f"byLabel{key.capitalize()}"
                curr = curr.setdefault(group_key, {})
                if key_value in curr:
                    if isinstance(curr[key_value], (int, float)):
                        curr[key_value] = {"_value": curr[key_value]}
                    curr = curr[key_value]
                else:
                    curr = curr.setdefault(key_value, {})

            # Now we're at the leaf, insert _value
            if "_value" in curr:
                curr["_value"] = (curr["_value"] + value) / 2
            else:
                curr["_value"] = value

    return nested_metrics


def recursively_flatten_values(obj: dict) -> dict:
    """Recursively flatten the json structure"""
    if isinstance(obj, dict):
        # If the dict is exactly {"_value": ...}, reduce it to the value
        if list(obj.keys()) == ["_value"]:
            return obj["_value"]

        # Otherwise, process each item and flatten children if possible
        new_obj = {}
        for k, v in obj.items():
            flattened_v = recursively_flatten_values(v)
            new_obj[k] = flattened_v
        return new_obj

    elif isinstance(obj, list):
        return [recursively_flatten_values(elem) for elem in obj]

    else:
        return obj


def get_cluster_health(alerts: list, passed: bool) -> str:
    has_error, has_warning = False, False
    for alert in alerts:
        if alert["severity"].lower() == 'warning':
            has_warning = True
        if alert["severity"].lower() == 'error':
            has_error = True
    if has_error or not passed:
        return "Red"
    if has_warning:
        return "Yellow"
    return "Green"


s3bucket = "kube-burner-ai-s3-bucket"

def split_dict_into_chunks(d, chunk_size):
    it = iter(d.items())

    while True:
        chunk = {}

        for _ in range(chunk_size):
            try:
                key, value = next(it)
                chunk[key] = value
            except StopIteration:
                break

        if not chunk:
            break

        yield chunk


def upload_csv_to_s3(data, bucket, filename):
    # use temporary file
    tmp = tempfile.NamedTemporaryFile(mode='w+', newline='', delete=False)
    try:
        writer = csv.writer(tmp)
        writer.writerow(["Metric", "Value"])
        for chunk in split_dict_into_chunks(data, CHUNK_SIZE):
            for k, v in chunk.items():
                writer.writerow([k, v])
        tmp.flush()

        # Initialise AWS s3 client from existing AWS config
        s3 = boto3.client("s3")
        s3.upload_file(tmp.name, bucket, filename)
        print(f"Uploaded to s3://{bucket}/{filename}")

    finally:
        tmp.close()
        os.remove(tmp.name)
        print(f"Temporary file {tmp.name} deleted")


def main():
    """Driver code to triger the execution"""
    args = parse_arguments()
    skip_patterns = compile_exclude_patterns(args.excludeMetrics)

    merged_output = {"metrics": {}}
    metric_files = set(glob.glob(os.path.join(args.path, "*.json")))

    for filepath in metric_files:
        process_json_file(filepath, skip_patterns, merged_output)
    # print(merged_output)

    nested_metrics = normalize_metrics(merged_output["metrics"].items())
    print(nested_metrics)

    final_output = recursively_flatten_values(nested_metrics)

    if args.outfile:
        with open(args.outfile, "w") as f:
            json.dump(final_output, f, indent=args.indent)
        print(f"Merged output written to: {args.outfile}")
    else:
        print(json.dumps(final_output, indent=args.indent))

    print(f"Total metrics collected: {len(merged_output['metrics'])}")

    alerts_file = os.path.join(args.path, "alert.json")
    alerts = load_json_file(alerts_file)
    job_summary_file = os.path.join(args.path, "jobSummary.json")
    job_summaries = load_json_file(job_summary_file)
    job_config, cluster_metadata = extract_job_config(job_summaries)
    upload_csv_to_s3(final_output, s3bucket, cluster_metadata["uuid"] + ".csv")

    print("Alerts")
    print(json.dumps(alerts, indent=args.indent))
    print("Job Configuration")
    print(json.dumps(job_config, indent=args.indent))

    patterns_to_remove = [r"(?i).*time.*", r"uuid", r"version"]
    metadata = remove_keys_by_patterns(cluster_metadata, patterns_to_remove)
    print("Cluster Metadata")
    print(json.dumps(metadata, indent=args.indent))
    cluster_health_score = get_cluster_health(alerts, cluster_metadata["passed"])
    print(f"Cluster Health: {cluster_health_score}")

if __name__ == "__main__":
    main()
