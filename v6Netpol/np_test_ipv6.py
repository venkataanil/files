#!/usr/bin/env python3
"""
np_test_ipv6.py

- Parse a NetworkPolicy YAML and extract:
  - namespace
  - podSelector.matchLabels
  - ingress.from[].ipBlock.cidr (IPv6 & IPv4)
  - ports[*].port (we use first TCP port found)
- Query the Kubernetes API for LoadBalancer services matching the pod selector in the namespace
  and collect their IPv6 EXTERNAL-IPs.
- For each service IPv6 EXTERNAL-IP and for each ipBlock cidr (IPv6 only), pick the first usable IPv6,
  assign it transiently to the specified local interface, bind a socket to it, and send an HTTP GET
  to the service IPv6:port. Then remove the added IP.
"""

import argparse
import ipaddress
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import List, Tuple, Optional

import yaml
from kubernetes import client, config

# ----------------------------
# Utility helpers
# ----------------------------
def run_cmd(cmd: List[str], check=True, capture_output=False):
    print(f"[run_cmd] Executing: {' '.join(cmd)}", file=sys.stderr)
    sys.stderr.flush()
    try:
        if capture_output:
            result = subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            print(f"[run_cmd] SUCCESS: {' '.join(cmd)}", file=sys.stderr)
            sys.stderr.flush()
            return result
        else:
            result = subprocess.run(cmd, check=check)
            print(f"[run_cmd] SUCCESS: {' '.join(cmd)}", file=sys.stderr)
            sys.stderr.flush()
            return result
    except subprocess.CalledProcessError as e:
        print(f"[run_cmd] FAILED: {' '.join(cmd)}, error: {e}", file=sys.stderr)
        sys.stderr.flush()
        raise

def pick_one_ipv6_from_cidr(cidr: str) -> Optional[str]:
    """Return a usable IPv6 address from cidr. For simplicity pick the first host address."""
    print(f"[pick_one_ipv6_from_cidr] Processing CIDR: {cidr}", file=sys.stderr)
    sys.stderr.flush()
    net = ipaddress.ip_network(cidr)
    if net.version != 6:
        print(f"[pick_one_ipv6_from_cidr] Skipping non-IPv6 CIDR: {cidr}", file=sys.stderr)
        sys.stderr.flush()
        return None
    # skip network and choose first usable
    hosts = net.hosts()
    try:
        picked = str(next(hosts))
        print(f"[pick_one_ipv6_from_cidr] Picked IPv6: {picked} from {cidr}", file=sys.stderr)
        sys.stderr.flush()
        return picked
    except StopIteration:
        print(f"[pick_one_ipv6_from_cidr] No usable hosts in CIDR: {cidr}", file=sys.stderr)
        sys.stderr.flush()
        return None

def add_ip_to_iface(ip: str, iface: str) -> None:
    # add /64 to interface with nodad flag to skip Duplicate Address Detection
    print(f"[add_ip_to_iface] Adding {ip}/64 to interface {iface} (with nodad)", file=sys.stderr)
    sys.stderr.flush()
    run_cmd(["ip", "-6", "addr", "add", f"{ip}/64", "dev", iface, "nodad"])

    # Wait a moment for the address to become active
    time.sleep(0.5)

    # Verify the address was added
    print(f"[add_ip_to_iface] Verifying address was added...", file=sys.stderr)
    sys.stderr.flush()
    result = subprocess.run(["ip", "-6", "addr", "show", "dev", iface],
                          capture_output=True, text=True, check=False)
    if ip in result.stdout:
        print(f"[add_ip_to_iface] SUCCESS - Address {ip} is on interface {iface}", file=sys.stderr)
        sys.stderr.flush()
    else:
        print(f"[add_ip_to_iface] WARNING - Address {ip} not found on interface {iface}", file=sys.stderr)
        sys.stderr.flush()

def del_ip_from_iface(ip: str, iface: str) -> None:
    print(f"[del_ip_from_iface] Removing {ip}/64 from interface {iface}", file=sys.stderr)
    sys.stderr.flush()
    run_cmd(["ip", "-6", "addr", "del", f"{ip}/64", "dev", iface], check=False)

def send_http_get_v6(src_ip: str, dst_ip: str, dst_port: int, path: str = "/", timeout: float = 5.0) -> Tuple[int, str]:
    """
    Use curl with --interface to send HTTP GET request from src_ip to dst_ip:dst_port,
    return (status_code, response_snippet).
    """
    print(f"[send_http_get_v6] Starting HTTP GET from {src_ip} to [{dst_ip}]:{dst_port}{path}", file=sys.stderr)
    sys.stderr.flush()

    # ensure brackets removed from dst_ip if present
    dst_ip = dst_ip.strip("[]")

    # Build curl command
    # Use --interface to specify source IP
    # -6 to force IPv6
    # -v for verbose output (shows request/response headers)
    # --max-time for timeout
    # -w to output HTTP status code
    url = f"http://[{dst_ip}]:{dst_port}{path}"

    curl_cmd = [
        "curl",
        "-6",                          # Force IPv6
        "--interface", src_ip,         # Bind to source IP
        "-v",                          # Verbose output
        "--max-time", str(int(timeout)),  # Timeout
        "-w", "\\nHTTP_STATUS_CODE:%{http_code}\\n",  # Output status code
        url
    ]

    print(f"[send_http_get_v6] Executing curl command: {' '.join(curl_cmd)}", file=sys.stderr)
    sys.stderr.flush()

    try:
        result = subprocess.run(
            curl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2  # Add buffer to curl's timeout
        )

        print(f"[send_http_get_v6] curl exit code: {result.returncode}", file=sys.stderr)
        sys.stderr.flush()

        # Combine stdout and stderr for full output
        full_output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        print(f"[send_http_get_v6] curl output:\n{full_output[:2000]}", file=sys.stderr)
        sys.stderr.flush()

        # Parse status code from output
        status_code = 0
        if "HTTP_STATUS_CODE:" in result.stdout:
            try:
                status_line = [line for line in result.stdout.split('\n') if 'HTTP_STATUS_CODE:' in line][0]
                status_code = int(status_line.split(':')[1].strip())
                print(f"[send_http_get_v6] SUCCESS - HTTP Status: {status_code}", file=sys.stderr)
                sys.stderr.flush()
            except Exception as e:
                print(f"[send_http_get_v6] FAILED to parse status code: {e}", file=sys.stderr)
                sys.stderr.flush()

        # Also try to parse from stderr (curl's verbose output)
        if status_code == 0 and result.stderr:
            for line in result.stderr.split('\n'):
                if line.startswith('< HTTP/'):
                    try:
                        parts = line.split()
                        if len(parts) >= 2:
                            status_code = int(parts[1])
                            print(f"[send_http_get_v6] SUCCESS - HTTP Status from stderr: {status_code} - {line}", file=sys.stderr)
                            sys.stderr.flush()
                            break
                    except Exception:
                        pass

        if result.returncode != 0 and status_code == 0:
            print(f"[send_http_get_v6] FAILED - curl returned non-zero exit code: {result.returncode}", file=sys.stderr)
            sys.stderr.flush()

        return status_code, result.stdout[:4096]

    except subprocess.TimeoutExpired:
        print(f"[send_http_get_v6] FAILED - curl command timed out after {timeout}s", file=sys.stderr)
        sys.stderr.flush()
        raise Exception(f"curl timeout after {timeout}s")
    except Exception as e:
        print(f"[send_http_get_v6] FAILED - Exception: {e}", file=sys.stderr)
        sys.stderr.flush()
        raise

def parse_networkpolicies_yml(path: str):
    """
    Parse a YAML that may contain multiple NetworkPolicy documents.
    Returns a list of policy dicts:
      { name, namespace, pod_selector (dict), ipblocks (list), ports (list of ints), raw_spec (dict) }
    """
    print(f"[parse_networkpolicies_yml] Parsing file: {path}", file=sys.stderr)
    sys.stderr.flush()
    policies = []
    with open(path) as f:
        docs = list(yaml.safe_load_all(f))
    print(f"[parse_networkpolicies_yml] Found {len(docs)} YAML documents", file=sys.stderr)
    sys.stderr.flush()
    for data in docs:
        if not isinstance(data, dict):
            continue
        kind = data.get("kind", "")
        if kind != "NetworkPolicy":
            continue
        metadata = data.get("metadata", {})
        name = metadata.get("name", "<unknown>")
        namespace = metadata.get("namespace", "default")
        spec = data.get("spec", {}) or {}
        pod_selector = spec.get("podSelector", {}).get("matchLabels", {}) or {}

        ipblocks = []
        ports = []

        ingress_items = spec.get("ingress", []) or []
        for item in ingress_items:
            # handle from: entries (ipBlock, namespaceSelector, podSelector)
            frm = item.get("from", []) or []
            for entry in frm:
                if "ipBlock" in entry:
                    cidr = entry["ipBlock"].get("cidr")
                    if cidr:
                        ipblocks.append(cidr)
                # optionally capture namespaceSelector/podSelector for future resolution
                # We'll keep them in raw spec for now
            # ports
            for p in item.get("ports", []) or []:
                proto = p.get("protocol", "TCP")
                if proto.upper() == "TCP":
                    try:
                        ports.append(int(p.get("port")))
                    except Exception:
                        pass

        policy = {
            "name": name,
            "namespace": namespace,
            "pod_selector": pod_selector,
            "ipblocks": ipblocks,
            "ports": ports,
            "raw_spec": spec,
        }
        policies.append(policy)
        print(f"[parse_networkpolicies_yml] Parsed policy '{name}': {len(ipblocks)} ipBlocks, {len(ports)} ports", file=sys.stderr)
        sys.stderr.flush()

    print(f"[parse_networkpolicies_yml] Total {len(policies)} NetworkPolicy documents parsed", file=sys.stderr)
    sys.stderr.flush()
    return policies


def resolve_namespace_selector(ns_selector: dict, kube_client: client.CoreV1Api) -> List[str]:
    """
    Given a namespaceSelector matchLabels dict, return list of namespace names that match.
    If ns_selector is empty, returns [].
    """
    if not ns_selector:
        return []
    label_selector = ",".join([f"{k}={v}" for k, v in ns_selector.items()])
    nss = kube_client.list_namespace(label_selector=label_selector).items
    return [ns.metadata.name for ns in nss]


def discover_pod_ipv6s(kube_client: client.CoreV1Api, namespace: str, pod_selector: dict) -> List[str]:
    """
    Query Kubernetes API for pods matching pod_selector in namespace.
    Return list of their IPv6 addresses.
    """
    ipv6s = []
    if not pod_selector:
        # empty selector means all pods
        label_selector = ""
    else:
        label_selector = ",".join([f"{k}={v}" for k, v in pod_selector.items()])

    pods = kube_client.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items
    for pod in pods:
        if pod.status and pod.status.pod_i_ps:
            for ip_info in pod.status.pod_i_ps:
                try:
                    addr = ipaddress.ip_address(ip_info.ip)
                    if addr.version == 6:
                        ipv6s.append(str(addr))
                except Exception:
                    pass
    return ipv6s


def discover_services_with_loadbalancer_ipv6(kube_client: client.CoreV1Api, namespace: str, pod_selector: dict) -> List[Tuple[str, str]]:
    """
    Query Kubernetes API for services matching pod_selector in namespace.
    Return list of tuples: (service_name, loadbalancer_ipv6).
    Only returns services of type LoadBalancer that have an IPv6 EXTERNAL-IP.

    If pod_selector is empty ({}), it means the policy applies to all pods,
    so we return all LoadBalancer services in the namespace.
    """
    print(f"[discover_services_with_loadbalancer_ipv6] Searching in namespace '{namespace}' with pod_selector: {pod_selector}", file=sys.stderr)
    sys.stderr.flush()

    result = []
    services = kube_client.list_namespaced_service(namespace=namespace).items
    print(f"[discover_services_with_loadbalancer_ipv6] Found {len(services)} total services in namespace", file=sys.stderr)
    sys.stderr.flush()

    for svc in services:
        # Check if service type is LoadBalancer
        if svc.spec.type != "LoadBalancer":
            print(f"[discover_services_with_loadbalancer_ipv6] Skipping service '{svc.metadata.name}' (type: {svc.spec.type})", file=sys.stderr)
            sys.stderr.flush()
            continue

        print(f"[discover_services_with_loadbalancer_ipv6] Checking LoadBalancer service '{svc.metadata.name}'", file=sys.stderr)
        sys.stderr.flush()

        # Check if service selector matches the pod_selector
        svc_selector = svc.spec.selector or {}
        if not svc_selector:
            print(f"[discover_services_with_loadbalancer_ipv6] Skipping service '{svc.metadata.name}' (no selector)", file=sys.stderr)
            sys.stderr.flush()
            continue

        print(f"[discover_services_with_loadbalancer_ipv6] Service '{svc.metadata.name}' selector: {svc_selector}", file=sys.stderr)
        sys.stderr.flush()

        # If pod_selector is empty ({}), match all services
        # Otherwise, check if all pod_selector labels are present in service selector
        if pod_selector:
            matches = all(svc_selector.get(k) == v for k, v in pod_selector.items())
            if not matches:
                print(f"[discover_services_with_loadbalancer_ipv6] Service '{svc.metadata.name}' does not match pod_selector", file=sys.stderr)
                sys.stderr.flush()
                continue

        print(f"[discover_services_with_loadbalancer_ipv6] Service '{svc.metadata.name}' MATCHES pod_selector", file=sys.stderr)
        sys.stderr.flush()

        # Extract LoadBalancer ingress IPs (IPv6 only)
        if svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
            for ingress in svc.status.load_balancer.ingress:
                if ingress.ip:
                    try:
                        addr = ipaddress.ip_address(ingress.ip)
                        if addr.version == 6:
                            print(f"[discover_services_with_loadbalancer_ipv6] Found IPv6 EXTERNAL-IP: {addr} for service '{svc.metadata.name}'", file=sys.stderr)
                            sys.stderr.flush()
                            result.append((svc.metadata.name, str(addr)))
                        else:
                            print(f"[discover_services_with_loadbalancer_ipv6] Skipping IPv4 EXTERNAL-IP: {addr} for service '{svc.metadata.name}'", file=sys.stderr)
                            sys.stderr.flush()
                    except Exception as e:
                        print(f"[discover_services_with_loadbalancer_ipv6] Error parsing IP '{ingress.ip}': {e}", file=sys.stderr)
                        sys.stderr.flush()
        else:
            print(f"[discover_services_with_loadbalancer_ipv6] Service '{svc.metadata.name}' has no LoadBalancer ingress IPs", file=sys.stderr)
            sys.stderr.flush()

    print(f"[discover_services_with_loadbalancer_ipv6] Returning {len(result)} service(s) with IPv6 EXTERNAL-IP", file=sys.stderr)
    sys.stderr.flush()
    return result


def discover_pods_for_from_entry(kube_client: client.CoreV1Api, policy_spec: dict, policy_namespace: str):
    """
    Discover pod IPs referenced by a policy's 'from' entries where they use podSelector/namespaceSelector.
    This is optional: many policies use ipBlock only, in which case this returns [].
    Returns dict with:
      - pod_ipv6s: list of pod IPv6s in the policy's own namespace matching podSelector
      - referenced_pod_ipv6s: list of pods discovered using namespaceSelector+podSelector found in the policy
    """
    pod_ipv6s = []
    referenced_pod_ipv6s = []

    # pods selected by the policy's podSelector within the policy namespace
    ps = policy_spec.get("podSelector", {}).get("matchLabels", {}) or {}
    if ps is not None:
        # reuse existing discovery function
        pod_ipv6s = discover_pod_ipv6s(kube_client, policy_namespace, ps)

    # now look for any ingress -> from entries with namespaceSelector/podSelector
    ing = policy_spec.get("ingress", []) or []
    for item in ing:
        for entry in item.get("from", []) or []:
            ns_sel = entry.get("namespaceSelector")
            pod_sel = entry.get("podSelector")
            if ns_sel:
                # resolve namespace names matching the selector
                label_map = ns_sel.get("matchLabels", {})
                ns_names = resolve_namespace_selector(label_map, kube_client) if label_map else []
                # for each namespace, find pods matching pod_sel (if provided) or all pods if pod_sel empty
                for ns in ns_names:
                    sel = pod_sel.get("matchLabels", {}) if pod_sel else {}
                    referenced_pod_ipv6s.extend(discover_pod_ipv6s(kube_client, ns, sel))
            elif pod_sel:
                # when only podSelector is provided without namespaceSelector, it means namespace = policy_namespace
                sel = pod_sel.get("matchLabels", {}) or {}
                referenced_pod_ipv6s.extend(discover_pod_ipv6s(kube_client, policy_namespace, sel))

    return {
        "pod_ipv6s": list(dict.fromkeys(pod_ipv6s)),  # unique
        "referenced_pod_ipv6s": list(dict.fromkeys(referenced_pod_ipv6s)),
    }


def main():
    print("DEBUG: Starting main()", file=sys.stderr)
    sys.stderr.flush()

    parser = argparse.ArgumentParser()
    parser.add_argument("--np-file", required=True, help="NetworkPolicy YAML file path (can contain multiple docs)")
    parser.add_argument("--iface", required=True, help="Local interface to add source IPv6 addresses to (e.g., eth0)")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig (default ~/.kube/config)")
    parser.add_argument("--pick-count", type=int, default=1, help="How many addresses to pick per cidr (default 1)")
    parser.add_argument("--dst-port", type=int, default=80, help="Destination port to send the request (default 80)")
    parser.add_argument("--path", default="/", help="HTTP path to request")
    parser.add_argument("--dry-run", action="store_true", help="Do not actually add addresses / send requests; just print plan")
    args = parser.parse_args()

    print(f"DEBUG: Args parsed, np-file={args.np_file}", file=sys.stderr)
    sys.stderr.flush()

    # load kubeconfig once
    if args.kubeconfig:
        config.load_kube_config(config_file=args.kubeconfig)
    else:
        config.load_kube_config()
    v1 = client.CoreV1Api()

    print("DEBUG: Kubeconfig loaded", file=sys.stderr)
    sys.stderr.flush()

    policies = parse_networkpolicies_yml(args.np_file)
    print(f"DEBUG: Found {len(policies)} policies", file=sys.stderr)
    sys.stderr.flush()

    if not policies:
        print("No NetworkPolicy documents found in file.")
        sys.exit(1)

    dst_port = args.dst_port

    for pol in policies:
        print(f"\n=== Policy: {pol['name']} (ns: {pol['namespace']}) ===")
        print("podSelector:", pol["pod_selector"])
        print("ipBlocks:", pol["ipblocks"])
        print("ports:", pol["ports"])
        print(f"[main] Processing policy '{pol['name']}'", file=sys.stderr)
        sys.stderr.flush()

        if not pol["ports"]:
            print("  - no TCP ports found; skipping policy")
            print(f"[main] Skipping policy '{pol['name']}' - no TCP ports", file=sys.stderr)
            sys.stderr.flush()
            continue
        # instead of first port use the user provided dest port
        # dst_port = pol["ports"][0]

        # discover services with LoadBalancer IPs that match the policy's podSelector
        print(f"[main] Discovering services for policy '{pol['name']}'", file=sys.stderr)
        sys.stderr.flush()
        service_targets = discover_services_with_loadbalancer_ipv6(v1, pol["namespace"], pol["pod_selector"])

        if not service_targets:
            print("  - no LoadBalancer services with IPv6 EXTERNAL-IP matched by the policy's podSelector; skipping.")
            continue
        print(f"  - found {len(service_targets)} LoadBalancer service(s) with IPv6:")
        for svc_name, svc_ip in service_targets:
            print(f"    - {svc_name}: {svc_ip}")

        # pick source candidates (ipv6) from ipBlocks
        print(f"[main] Picking source IPv6 candidates from {len(pol['ipblocks'])} ipBlocks", file=sys.stderr)
        sys.stderr.flush()
        src_candidates = []
        for cidr in pol["ipblocks"]:
            try:
                picked = pick_one_ipv6_from_cidr(cidr)
                if picked:
                    src_candidates.append(picked)
            except Exception as e:
                print("  - skipping cidr", cidr, "err:", e)
                print(f"[main] Error picking IPv6 from CIDR {cidr}: {e}", file=sys.stderr)
                sys.stderr.flush()

        print(f"[main] Selected {len(src_candidates)} IPv6 source candidates", file=sys.stderr)
        sys.stderr.flush()

        if not src_candidates:
            print("  - no IPv6 source candidates from ipBlocks (policy may use pod/namespace selectors instead).")
            print(f"[main] Skipping policy '{pol['name']}' - no IPv6 source candidates", file=sys.stderr)
            sys.stderr.flush()
            # continue or decide to handle referenced pods differently
            # For now we skip sending requests when no ipBlocks are present
            continue

        # perform requests (serially)
        print(f"[main] Starting HTTP requests: {len(service_targets)} service(s) x {len(src_candidates)} source(s)", file=sys.stderr)
        sys.stderr.flush()

        for svc_name, svc_ip in service_targets:
            for src in src_candidates:
                print(f"\nPolicy {pol['name']}: src={src} -> service={svc_name} dst={svc_ip}:{dst_port}")
                print(f"[main] ========== REQUEST START ==========", file=sys.stderr)
                print(f"[main] Policy: {pol['name']}, Source: {src}, Service: {svc_name}, Dest: {svc_ip}:{dst_port}", file=sys.stderr)
                sys.stderr.flush()

                if args.dry_run:
                    print(f"[main] DRY-RUN mode - skipping actual request", file=sys.stderr)
                    sys.stderr.flush()
                    continue
                try:
                    add_ip_to_iface(src, args.iface)
                    time.sleep(0.15)
                    status, snippet = send_http_get_v6(src, svc_ip, dst_port, path=args.path, timeout=8.0)
                    print("  HTTP status:", status)
                    print(f"[main] ========== REQUEST SUCCESS: HTTP {status} ==========", file=sys.stderr)
                    sys.stderr.flush()
                except Exception as e:
                    print("  request failed:", e)
                    print(f"[main] ========== REQUEST FAILED: {e} ==========", file=sys.stderr)
                    sys.stderr.flush()
                finally:
                    try:
                        del_ip_from_iface(src, args.iface)
                    except Exception as e:
                        print("  failed to remove src ip:", e)
                        print(f"[main] Failed to cleanup source IP: {e}", file=sys.stderr)
                        sys.stderr.flush()

    print("\nAll policies processed.")


if __name__ == "__main__":
    main()
