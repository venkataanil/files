
sudo sed -i "s/root/heat-admin/g" ~/.ssh/config
sudo cp /etc/hosts ~/
sudo cp /etc/hosts /etc/hosts_org
source ~/stackrc
total_nodes=`openstack server list -c ID -f value | wc -l`
controllers=3
computes=$((total_nodes-controllers))
echo computes: $computes
for i in $(seq 0 $((computes-1))); do
  sudo sed -i "s/compute-$i.ctlplane$/compute-$i.ctlplane cp-$i/g" /etc/hosts
done  
for i in $(seq 0 $((controllers-1))); do
  sudo sed -i "s/controller-$i.ctlplane$/controller-$i.ctlplane ct-$i/g" /etc/hosts
done  

for i in $(seq 0 $((computes-1))); do
  ssh cp-$i "sudo cp -r ~/.ssh /root/";
done
for i in $(seq 0 $((controllers-1))); do
  ssh ct-$i "sudo cp -r ~/.ssh /root/";
done

sudo sed -i "s/heat-admin/root/g" ~/.ssh/config

echo "[computes]" > ~/inventory
for i in $(seq 0 $((computes-1))); do
  echo "cp-$i" >> ~/inventory
done
echo "[controllers]" >> ~/inventory
for i in $(seq 0 $((controllers-1))); do
  echo "ct-$i" >> ~/inventory
done

