package main

import (
	"fmt"
	"log"
	"net"
	"net/http"
	"os/exec"
	"sync"
)

const (
	destIP         = "10.131.0.47:8000"
	maxConcurrency = 2
)

func main() {
	var wg sync.WaitGroup
	sem := make(chan struct{}, maxConcurrency)

	for i := 0; i < 5; i++ {
		baseIP := net.ParseIP("20.0.0.1")
		if baseIP == nil {
			log.Fatalf("Failed to parse base IP")
		}

		for j := 0; j < 5; j++ {

			// Create a new Linux virtual interface
			interfaceName := fmt.Sprintf("dummy%d%d", i, j)
			err := createDummyInterface(interfaceName)
			if err != nil {
				log.Printf("Failed to create virtual interface %s: %v", interfaceName, err)
				continue
			}

			// Spawn a Go routine to add a new IP and send an HTTP request
			for k := 1; k < 255; k++ {
				subnet := fmt.Sprintf("20.%d.%d.%d", i, j, k)
				wg.Add(1)
				// Acquire semaphore, force 20 concurrent threads at a time
				sem <- struct{}{}
				go func(iface, ip string) error {
					defer wg.Done()
					// Release semaphore
					defer func() { <-sem }()
					if err := exec.Command("ip", "addr", "add", ip+"/32", "dev", iface).Run(); err != nil {
						return fmt.Errorf("failed to assign IP %s to dummy interface %s: %v", ip, iface, err)
					}

					if err := sendHTTPRequest(iface, ip, destIP); err != nil {
						log.Printf("Failed to send HTTP request from %s: %v", ip, err)
					}
					return nil
				}(interfaceName, subnet)
			}
		}
	}

	wg.Wait()
	fmt.Println("All tasks completed")
}

// createDummyInterface creates a dummy interface
func createDummyInterface(iface string) error {
	cmd := exec.Command("ip", "link", "add", iface, "type", "dummy")
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("failed to create dummy interface %s: %v", iface, err)
	}

	if err := exec.Command("ip", "link", "set", iface, "up").Run(); err != nil {
		return fmt.Errorf("failed to set dummy interface %s up: %v", iface, err)
	}

	return nil
}

// sendHTTPRequest sends an HTTP GET request using the specified source IP
func sendHTTPRequest(iface, srcIP, destIP string) error {
	// Bind the source IP to the HTTP client
	dialer := &net.Dialer{
		LocalAddr: &net.TCPAddr{
			IP: net.ParseIP(srcIP),
		},
	}

	client := &http.Client{
		Transport: &http.Transport{
			DialContext: dialer.DialContext,
		},
	}

	resp, err := client.Get("http://" + destIP)
	if err != nil {
		return fmt.Errorf("HTTP request failed: %v", err)
	}
	defer resp.Body.Close()

	log.Printf("HTTP request from %s to %s completed with status: %s", srcIP, destIP, resp.Status)
	return nil
}
