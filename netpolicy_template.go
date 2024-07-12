package main

import (
	"fmt"
	"strconv"

	"gonum.org/v1/gonum/stat/combin"
)

func main() {
	// combin provides several ways to work with the combinations of
	// different objects. IndexToCombination allows random access into
	// the combination order. Combined with CombinationIndex this
	// provides a correspondence between integers and combinations.
	namespaces := 100
	peer_namespaces := 2
	pod_selectors := 2
	replica_count := 2
	netpols_per_namespace := 2
	binomial := combin.Binomial(namespaces, peer_namespaces)

	for iteration := 0; iteration < namespaces; iteration++ {
		//fmt.Println("--------- ns %s ", strconv.Itoa(iteration))
		for replica := 1; replica < (replica_count + 1); replica++ {
			// 4 table for iterationation
			startIdx := iteration * pod_selectors * netpols_per_namespace
			nsShift := (replica - 1) * pod_selectors
			startIdx = startIdx + nsShift - 1
			for pod_selector:=0; pod_selector < pod_selectors; pod_selector++ {
				startIdx = startIdx + 1
				if startIdx > binomial {
					startIdx = startIdx % binomial
				}
				nsIdxList := combin.IndexToCombination(nil, startIdx, namespaces, peer_namespaces)
				//fmt.Println("ns ", strconv.Itoa(iteration), " replica ", strconv.Itoa(replica), " peer ns ", fmt.Sprint(nsIdxList))
				fmt.Println("ns, replica, peer ns ", strconv.Itoa(iteration), strconv.Itoa(replica), fmt.Sprint(nsIdxList))

			}
		}

	}
}
