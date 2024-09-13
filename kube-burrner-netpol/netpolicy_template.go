package main

import (
	"fmt"

)

func main() {
	// combin provides several ways to work with the combinations of
	// different objects. IndexToCombination allows random access into
	// the combination order. Combined with CombinationIndex this
	// provides a correspondence between integers and combinations.
	namespaces := 10
	peer_namespaces := 5
	pod_selectors := 2
	replica_count := 5
	netpols_per_namespace := 5 //4

	for iteration := 0; iteration < namespaces; iteration++ {
		//fmt.Println("--------- ns %s ", strconv.Itoa(iteration))
		for replica := 1; replica < (replica_count + 1); replica++ {
			// 4 table for iterationation
			startIdx := (iteration * netpols_per_namespace * pod_selectors * peer_namespaces) + ((replica-1) * pod_selectors * peer_namespaces)
			podIdx := ((replica-1) * pod_selectors * peer_namespaces)
			fmt.Printf("startIdx Before :%d ", startIdx)
			if startIdx >= namespaces {
				startIdx = startIdx % namespaces
			}
			fmt.Printf("startIdx :%d\n", startIdx)
			fmt.Printf("podIdx Before :%d ", podIdx)
			podIdx = podIdx / namespaces
			fmt.Printf("podIdx :%d\n", podIdx)
			//startIdx := startIdx + (replica * pod_selectors * peer_namespaces)

			for pod_selector:=0; pod_selector < pod_selectors; pod_selector++ {
				mystart := startIdx + (pod_selector * peer_namespaces)
				nsIdxList := make([]int, 0)
				for i:=0; i < peer_namespaces; i++ {
					nsIdxList = append(nsIdxList, mystart + i)
				}
				//fmt.Println("ns ", strconv.Itoa(iteration), " replica ", strconv.Itoa(replica), " peer ns ", fmt.Sprint(nsIdxList))
				fmt.Printf("ns %d, replica %d, podSelector : %d peer ns %v\n", iteration, replica, pod_selector, nsIdxList)

			}
		}

	}
}
