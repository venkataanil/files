# Based on https://bobcares.com/blog/the-vpc-has-dependencies-and-cannot-be-deleted-error/

cluster_seed=vpc-anil*

delete_igw()
{
  aws ec2 describe-internet-gateways --filters "Name=tag:Name,Values=$cluster_seed"  > /tmp/igw.json
  cat /tmp/igw.json | jq -c '.InternetGateways[]' | while read igw; do
    igw_id=$(echo $igw | jq -r '.InternetGatewayId')
    vpc_id=$(echo $igw | jq -r '.Attachments[].VpcId')
    echo "Detach internet-gateway $igw_id from vpc $vpc_id"
    aws ec2 detach-internet-gateway --internet-gateway-id $igw_id --vpc-id $vpc_id
    aws ec2 delete-internet-gateway --internet-gateway-id $igw_id
  done
}

unmap_public_addr()
{
  aws ec2 describe-addresses --filters "Name=tag:Name,Values=$cluster_seed" > /tmp/pubaddr.json
  cat /tmp/pubaddr.json | jq -c '.Addresses[]' | while read addr; do
  public_addr=$(echo $addr | jq -r '.PublicIp')
  echo "Disassociating public address $public_addr"
  aws ec2 disassociate-address --public-ip $public_addr
  done

}

delete_nat_gateways()
{
  for vpc_id in `aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$cluster_seed" | jq -r '.Vpcs[].VpcId'`; do
    nat_gw=`aws ec2 describe-nat-gateways --filter Name=vpc-id,Values=$vpc_id | jq -r '.NatGateways[].NatGatewayId'`
    echo "VPC $vpc_id nat gateways $nat_gw"
    if [ -n "$nat_gw" ]; then
      aws ec2 delete-nat-gateway --nat-gateway-id $nat_gw
    fi 
  done

}

delete_subnets()
{
  aws ec2 describe-subnets --filters "Name=tag:Name,Values=$cluster_seed" > /tmp/subnets.json
  cat /tmp/subnets.json | jq -c '.Subnets[]' | while read subnet; do
  subnet_id=$(echo $subnet | jq -r '.SubnetId')
  echo "Deleting subnet $subnet_id"
  aws ec2 delete-subnet --subnet-id $subnet_id
  done
}

delete_rts()
{
  aws ec2 describe-route-tables --filters "Name=tag:Name,Values=$cluster_seed" > /tmp/route_tables.json
  cat /tmp/route_tables.json | jq -c '.RouteTables[]' | while read rt; do
  rt_id=$(echo $rt | jq -r '.RouteTableId')
  echo "Deleting route table $rt_id"
  aws ec2 delete-route-table --route-table-id $rt_id
  done
}

delete_nacl()
{
  for vpc_id in `aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$cluster_seed" | jq -r '.Vpcs[].VpcId'`; do
    for nacl in `aws ec2 describe-network-acls --filter Name=vpc-id,Values=$vpc_id | jq -c '.NetworkAcls[]`; do
      if IsDefault
    nacl_default=`aws ec2 describe-network-acls --filter Name=vpc-id,Values=$vpc_id | jq -r '.NetworkAcls[].NetworkAclId'`
    nacl=`aws ec2 describe-network-acls --filter Name=vpc-id,Values=$vpc_id | jq -r '.NetworkAcls[].NetworkAclId'`
    echo "VPC $vpc_id nat gateways $nacl"
    if [ -n "$nacl" ]; then
      aws ec2 delete-network-acl --network-acl-id $nacl
    fi 
  done

}


delete_vpcs()
{
  aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$cluster_seed" > /tmp/vpcs.json
  cat /tmp/vpcs.json | jq -c '.Vpcs[]' | while read vpc; do
  vpc_id=$(echo $vpc | jq -r '.VpcId')
  echo "Deleting vpc $vpc_id"
  aws ec2 delete-vpc --vpc-id $vpc_id
  done
}

#delete_nat_gateways
#####unmap_public_addr
#delete_igw
#delete_subnets
#delete_rts
delete_nacl
#delete_vpcs
