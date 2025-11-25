# Python script np_test_ipv6.py for testing IPv6 NetworkPolicies
This script parses NetworkPolicy YAML files, extracts relevant information, and performs HTTP GET requests using IPv6 addresses from LoadBalancer services in Kubernetes.

# How to run?
python3 ~/np_test_ipv6.py --np-file ~/cu/scp.yml --iface eno1 --kubeconfig /root/mno/kubeconfig --dst-port=8080 2>&1 | tee ~/log
