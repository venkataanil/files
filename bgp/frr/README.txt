mkdir /root/frr
podman run -d --rm  -v /root/frr:/etc/frr:Z --net=host --name frr-upstream --privileged quay.io/frrouting/frr:8.2.2
