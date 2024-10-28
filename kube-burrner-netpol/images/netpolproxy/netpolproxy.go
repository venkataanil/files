package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"sync"
	"time"
)

type connection struct {
        Addresses []string `json:"addresses"`
        Ports     []int32    `json:"ports"`
	Netpol    string   `json:"netpol"`
}

type connTest struct {
	Address   string `json:"address"`
	Port      int    `json:"port"`
	IngressIdx int `json:"ingressidx"`
	NpName string `json:"npname"`
	Timestamp time.Time `json:"timestamp"`
}
const (
	podPort = 9001
	parallelConnections = 20
)

var (
	//connections = make(map[string]map[string][]connection)
	connections = make(map[string][]connection)
	connWg sync.WaitGroup
	sendConnectionsDone bool
	checkStopDone bool
	sendConnMutex sync.Mutex
	checkStopMutex sync.Mutex
	resWg sync.WaitGroup
	clusterResults = make(map[string][]connTest)
	resultsMutex sync.Mutex
	doneInitiate = make(chan bool)
)

type ProxyResponse struct {
        Result bool `json:"result"`
}
// Send connections to all pods
func sendNetpolInfo(pod string, connInfo []connection, semaphore chan struct{}) {
	log.Printf("Inside sendNetpolInfo")
	defer connWg.Done()
	defer func() { <- semaphore }()
	log.Printf("Before json.Marshal")
	data, err := json.Marshal(connInfo)
        if err != nil {
		log.Fatalf("Failed to marshal payload: %v", err)
	}

        url := fmt.Sprintf("http://%s:%d/check", pod, podPort)
	log.Printf("url is %s", url)
        resp, err := http.Post(url, "application/json", bytes.NewBuffer(data))
	log.Printf("POST is called")
        if err != nil {
           		log.Fatalf("Failed to send request: %v", err)
        }
	log.Printf("Before resp.Body.Close")
        resp.Body.Close()
	log.Printf("After resp.Body.Close")
        if resp.StatusCode == http.StatusOK {
        	log.Printf("Connections sent to %s successfully", url)
	}
}

func sendConnections() {
        log.Printf("Inside sendConnections")
	<-doneInitiate
        log.Printf("After doneInitiate")
	log.Printf("Sending connections to %d pods", len(connections))
	semaphore := make(chan struct{}, parallelConnections)
	log.Printf("semaphore created")
	for pod, connInfo := range connections {
		log.Printf("pod %v, connInfo %v", pod, connInfo)
		semaphore <- struct{}{}
		log.Printf("semphore released")
                connWg.Add(1)
		log.Printf("Wg.Add() called")
                go sendNetpolInfo(pod, connInfo, semaphore)
		log.Printf("After sendNetpolInfo")
	}
	connWg.Wait()
	sendConnMutex.Lock()
	sendConnectionsDone = true
	sendConnMutex.Unlock()	
}

func handleCheckConnectionsStatus(w http.ResponseWriter, r *http.Request) {
	sendConnMutex.Lock()
	defer sendConnMutex.Unlock()
	 result := false

	if sendConnectionsDone {
		 result = true
	}
	response := ProxyResponse{Result: result}
	err := json.NewEncoder(w).Encode(response)
        if err != nil {
                log.Println("Error encoding response:", err)
                http.Error(w, "Internal Server Error", http.StatusInternalServerError)
                return
        }	
}

func handleCheckStopStatus(w http.ResponseWriter, r *http.Request) {
	checkStopMutex.Lock()
	defer checkStopMutex.Unlock()

	 result := false

	if checkStopDone {
		 result = true
	}
	response := ProxyResponse{Result: result}
	err := json.NewEncoder(w).Encode(response)
        if err != nil {
                log.Println("Error encoding response:", err)
                http.Error(w, "Internal Server Error", http.StatusInternalServerError)
                return
        }	
}

func handleInitiate(w http.ResponseWriter, r *http.Request) {
	// Send an immediate response to the client
	fmt.Fprintln(w, "Initiate Request received, processing...")

	// Read data from the request
	body, err := ioutil.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Unable to read response body", http.StatusBadRequest)
		return
	}
	err = json.Unmarshal(body, &connections)
	if err != nil {
		http.Error(w, "Unable to parse response body", http.StatusBadRequest)
		return
	}
	r.Body.Close()
	log.Printf("Number of connections got from kube-burner %d", len(connections))
	doneInitiate <- true
}

func handleStop(w http.ResponseWriter, r *http.Request) {
	// Send an immediate response to the client
	fmt.Fprintln(w, "Stop Request received, processing...")

	// Read data from the request
	_, err := ioutil.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Unable to read response body", http.StatusBadRequest)
		return
	}
        r.Body.Close()

    	// Get results from all pods
	go getResults(connections) 
}

// Get results from a single pod
func getPodResult(pod string, semaphore chan struct{}) {
	defer resWg.Done()
	defer func() { <- semaphore }()

        url := fmt.Sprintf("http://%s:%d/results", pod, podPort)
        // Retrieve the results
        resp, err := http.Get(url)
        if err != nil {
         	log.Fatalf("Failed to retrieve results: %v", err)
        }
	defer resp.Body.Close()

        if resp.StatusCode != http.StatusOK {
             	log.Fatalf("Unexpected status code: %d", resp.StatusCode)
        }
        body, err := ioutil.ReadAll(resp.Body)
        if err != nil {
             	log.Fatalf("Failed to read response body: %v", err)
        }

        var results []connTest
        if err := json.Unmarshal(body, &results); err != nil {
              	log.Fatalf("Failed to unmarshal results: %v", err)
        }
        for _, res := range results {
        	log.Printf("Address: %s, Port: %d, IngressIdx: %v, NpName: %s Timestamp: %v\n", res.Address, res.Port, res.IngressIdx, res.NpName, res.Timestamp)
        }
	resultsMutex.Lock()
	clusterResults[pod] = results
	resultsMutex.Unlock()
}

// Get results from all pods
func getResults(cts map[string][]connection) {
	semaphore := make(chan struct{}, parallelConnections)
	for pod, _ := range cts {
		semaphore <- struct{}{}
                resWg.Add(1)
                go getPodResult(pod, semaphore)
	}
	resWg.Wait()
	checkStopMutex.Lock()
	checkStopDone = true
	checkStopMutex.Unlock()
}


func resultsHandler(w http.ResponseWriter, r *http.Request) {
	resultsMutex.Lock()
	defer resultsMutex.Unlock()
        if err := json.NewEncoder(w).Encode(clusterResults); err != nil {
                http.Error(w, err.Error(), http.StatusInternalServerError)
        }
}

func main() {
    	// Send connections to all pods
	go sendConnections()
	go func() {
		http.HandleFunc("/initiate", handleInitiate)
		http.HandleFunc("/checkConnectionsStatus", handleCheckConnectionsStatus)
		http.HandleFunc("/stop", handleStop)
		http.HandleFunc("/checkStopStatus", handleCheckStopStatus)
		http.HandleFunc("/results", resultsHandler)
		log.Println("Client server started on :9002")
		log.Fatal(http.ListenAndServe(":9002", nil))
	}()

	select {} // keep the client running
}
