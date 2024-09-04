// Copyright 2023 The Kube-burner Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package measurements

import (
	"bytes"
	"context"
	"embed"
	"fmt"
	"io"
	"io/ioutil"
	"encoding/json"
	"path"
	"regexp"
	"sync"
	"strings"
	"strconv"
	"time"
	"net/http"

	"github.com/cloud-bulldozer/go-commons/indexers"
	kutil "github.com/kube-burner/kube-burner/pkg/util"
	kconfig "github.com/kube-burner/kube-burner/pkg/config"
	"github.com/kube-burner/kube-burner/pkg/measurements/metrics"
	"github.com/kube-burner/kube-burner/pkg/measurements/types"
	"github.com/montanaflynn/stats"
	log "github.com/sirupsen/logrus"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/util/wait"
	lcorev1 "k8s.io/client-go/listers/core/v1"
	lnetv1 "k8s.io/client-go/listers/networking/v1"
	"k8s.io/client-go/rest"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/tools/cache"
	"k8s.io/kubectl/pkg/scheme"
	"k8s.io/client-go/restmapper"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/client-go/discovery"
)


var embedFS embed.FS
var embedFSDir string
var discoveryClient *discovery.DiscoveryClient
var nsPodAddresses = make(map[string]map[string][]string)
const podPort = 9001
const proxyRoute = "proxyserver-network-policy-proxy-0.apps.vkommadi-osd2.4mce.s1.devshift.org"
const waitCheckStopStatus = 5

type connection struct {
        Addresses []string `json:"addresses"`
        Ports     []int32    `json:"ports"`
}
var connections = make(map[string]map[string][]connection)

type requestPayload struct {
        Connections map[string][]connection `json:"connections"`
        ClientIP    string                          `json:"client_ip"`
        ClientPort  int                             `json:"client_port"`
}

type connTest struct {
        Address   string `json:"address"`
        Port      int32    `json:"port"`
        IngressIdx int `json:"connectionidx"`
        NpName string `json:"npname"`
        Timestamp time.Time `json:"timestamp"`
}
var clusterResults = make(map[string][]connTest)
var npResults = make(map[string][]float64)
var npCreationTime = make(map[string]time.Time)

type ProxyResponse struct {
        Result bool `json:"result"`
}

const (
	netpolLatencyMeasurement          = "netpolLatencyMeasurement"
	netpolLatencyQuantilesMeasurement = "netpolLatencyQuantilesMeasurement"
)

type netpolLatency struct {
	config           types.Measurement
	netpolWatcher       *metrics.Watcher
	podWatcher        *metrics.Watcher
	podLister         lcorev1.PodLister
	netpolLister        lnetv1.NetworkPolicyLister
	//metrics          map[string]netpolMetric
	metrics          sync.Map
	latencyQuantiles []interface{}
	normLatencies    []interface{}
	metricLock       sync.RWMutex
}

type netpolMetric struct {
	Timestamp         time.Time          `json:"timestamp"`
	ReadyLatency      time.Duration      `json:"ready"`
	MetricName        string             `json:"metricName"`
	UUID              string             `json:"uuid"`
	Namespace         string             `json:"namespace"`
	Name              string             `json:"netpol"`
	Metadata          interface{}        `json:"metadata,omitempty"`
	JobName           string      `json:"jobName,omitempty"`
}

func isEmpty(raw []byte) bool {
	return strings.TrimSpace(string(raw)) == ""
}

// newMapper returns a discovery RESTMapper
func newRESTMapper() meta.RESTMapper {
	apiGroupResouces, err := restmapper.GetAPIGroupResources(discoveryClient)
	if err != nil {
		log.Fatal(err)
	}
	return restmapper.NewDiscoveryRESTMapper(apiGroupResouces)
}

func yamlToUnstructured(fileName string, y []byte, uns *unstructured.Unstructured) (runtime.Object, *schema.GroupVersionKind) {
	o, gvk, err := scheme.Codecs.UniversalDeserializer().Decode(y, nil, uns)
	if err != nil {
		log.Fatalf("Error decoding YAML (%s): %s", fileName, err)
	}
	return o, gvk
}
func prepareTemplate(original []byte) ([]byte, error) {
	// Removing all placeholders from template.
	// This needs to be done due to placeholders not being valid yaml
	if isEmpty(original) {
		return nil, fmt.Errorf("template is empty")
	}
	r, err := regexp.Compile(`\{\{.*\}\}`)
	if err != nil {
		return nil, fmt.Errorf("regexp creation error: %v", err)
	}
	original = r.ReplaceAll(original, []byte{})
	return original, nil
}


func init() {
	measurementMap["netpolLatency"] = &netpolLatency{
		metrics: sync.Map{},
	}
}

func getNamespacesByLabel(s *metav1.LabelSelector) []string {
	var namespaces []string
	if s.MatchLabels != nil {
		if ns, exists := s.MatchLabels["kubernetes.io/metadata.name"]; exists {
			namespaces = append(namespaces, ns)
		}
	} else {
		for _, expr := range s.MatchExpressions {
			// Let's restrict testing to IN operator only for now
			if expr.Key == "kubernetes.io/metadata.name" {
				if expr.Operator == metav1.LabelSelectorOpIn {
					namespaces = expr.Values
				} else {
					log.Errorf("Netpol workload supports only IN operator for NamespaceSelector.MatchExpressions")
				}
			}
		}
	}
	return namespaces
}

func addPodsByLabel(ns string, ps *metav1.LabelSelector) []string{
	var addresses []string
	selectorString := ns
	listOptions := metav1.ListOptions{}
	log.Debugf("ANIL ns %v", ns)
	if ns == "" {
		return nil
	}
	if ps != nil {
		selector, err := metav1.LabelSelectorAsSelector(ps)
		if err != nil {
			log.Fatal(err)
		}
		listOptions.LabelSelector = selector.String()
		selectorString = selector.String()
	}
	log.Debugf("ANIL selectorString %v", selectorString)
	log.Debugf("ANIL nsPodAddresses %v", nsPodAddresses)
	if podsMap, ok := nsPodAddresses[ns]; !ok || podsMap[selectorString] == nil {
		pods, err := factory.clientSet.CoreV1().Pods(ns).List(context.TODO(), listOptions)
		if err != nil {
			log.Fatal(err)
		}
		for _, pod := range pods.Items {
			addresses = append(addresses, pod.Status.PodIP)
		}
		if !ok {
			nsPodAddresses[ns] = map[string][]string{selectorString: addresses}
		} else {
			nsPodAddresses[ns][selectorString] = addresses
		}
	} else {
		addresses = nsPodAddresses[ns][selectorString]
	}
	return addresses
}


func (n *netpolLatency) getPodList(ns string, ps *metav1.LabelSelector) *corev1.PodList {
	listOptions := metav1.ListOptions{}
	if ps != nil {
		selector, err := metav1.LabelSelectorAsSelector(ps)
		if err != nil {
			log.Fatal(err)
		}
		if n.config.SkipPodWait == false {
			if err := n.waitForPods(ns, selector); err != nil {
				log.Fatal(err)
			}
		}
		listOptions.LabelSelector = selector.String()

	}

	pods, err := factory.clientSet.CoreV1().Pods(ns).List(context.TODO(), listOptions)
	if err != nil {
		log.Fatal(err)
	}
	return pods
}

func (n *netpolLatency) handleCreateNetpol(obj interface{}) {
	netpol := obj.(*networkingv1.NetworkPolicy)
	npCreationTime[netpol.Name] = netpol.CreationTimestamp.Time.UTC()
	return
}

func (n *netpolLatency) setConfig(cfg types.Measurement) {

	n.config = cfg
}
func (n *netpolLatency) validateConfig() error {
	if n.config.NetpolTimeout == 0 {
		return fmt.Errorf("NetpolTimeout cannot be 0")
	}
	return nil
}
func getNetworkPolicy(iteration int, replica int, obj kconfig.Object, objectSpec []byte) *networkingv1.NetworkPolicy {

	templateData := map[string]interface{}{
		"JobName":   factory.jobConfig.Name,
		"Iteration": strconv.Itoa(iteration),
		"UUID":      globalCfg.UUID,
		"Replica":   strconv.Itoa(replica),
	}
	for k, v := range obj.InputVars {
		templateData[k] = v
	}
	renderedObj, err := kutil.RenderTemplate(objectSpec, templateData, kutil.MissingKeyError)
	if err != nil {
		log.Fatalf("Template error in %s: %s", obj.ObjectTemplate, err)
	}

	networkingv1.AddToScheme(scheme.Scheme)
	decode := serializer.NewCodecFactory(scheme.Scheme).UniversalDeserializer().Decode
	netpolObj, _, err := decode(renderedObj, nil, nil)
	if err != nil {
		log.Fatalf("Failed to decode YAML file: %v", err)
	}

	networkPolicy, ok := netpolObj.(*networkingv1.NetworkPolicy)
	if !ok {
		log.Fatalf("YAML file is not a SecurityGroup")
	}
	return networkPolicy
}
// start netpol latency measurement
func prepareConnections() {
	// Reset latency slices, required in multi-job benchmarks
	for _, obj := range factory.jobConfig.Objects {
		cleanTemplate, err := readTemplate(obj)
		log.Debug("ANIL cleanTemplate %v", cleanTemplate)
		if err != nil {
			log.Fatalf("Error in readTemplate %s: %s", obj.ObjectTemplate, err)
		}
		if getObjectType(obj, cleanTemplate) != "NetworkPolicy" {
			continue
		}
		for i := 0; i < factory.jobConfig.JobIterations; i++ {
			for r := 1; r <= obj.Replicas; r++ {
				networkPolicy := getNetworkPolicy(i, r, obj, cleanTemplate)
				nsIndex := i / factory.jobConfig.IterationsPerNamespace
				namespace := fmt.Sprintf("%s-%d", factory.jobConfig.Namespace, nsIndex)
				localPods := addPodsByLabel(namespace, &networkPolicy.Spec.PodSelector)
				log.Debugf("ANIL iteration: %d replica: %d namespace: %s localPods: %v", i, r, namespace, localPods)
				for _, ingress := range networkPolicy.Spec.Ingress {
					log.Debug("ANIL ingress %v", ingress)
					var ingressPorts []int32
					for _, from := range ingress.From {
						for _, port := range ingress.Ports {
							if *port.Protocol != corev1.ProtocolTCP {
								log.Fatalf("Only TCP ports supported in testing")
							}
							ingressPorts = append(ingressPorts, port.Port.IntVal)
						}
						namespaces := getNamespacesByLabel(from.NamespaceSelector)
						log.Debugf("ANIL namespaces :%v", namespaces)
						for _, namepsace := range namespaces {
							remoteAddrs := addPodsByLabel(namepsace, from.PodSelector)
							log.Debugf("ANIL remoteAddrs %v", remoteAddrs)
                            for _, ra := range remoteAddrs {
								// exclude sending connection request to same ip address
								otherIPs := []string{}
								for _, ip := range localPods {
									if ip != ra {
										otherIPs = append(otherIPs, ip)
									}
								}
                                conn := connection{
                                    Addresses: otherIPs,
                                    Ports: ingressPorts,
                                }
                                if connections[ra] == nil {
                                    connections[ra] = make(map[string][]connection)
                                }
                                connections[ra][networkPolicy.Name] = append(connections[ra][networkPolicy.Name], conn)
                            }
						}
					}
				}
			}
		}
		log.Debugf("ANIL connections: %v", connections)
	}
}

func sendConnections() {
        data, err := json.Marshal(connections)
        if err != nil {
                log.Fatalf("Failed to marshal payload: %v", err)
        }

		url := fmt.Sprintf("http://%s/initiate", proxyRoute)
        resp, err := http.Post(url, "application/json", bytes.NewBuffer(data))
        if err != nil {
                log.Fatalf("Failed to send request: %v", err)
        }
        defer resp.Body.Close()

        if resp.StatusCode == http.StatusOK {
                log.Debugf("Connections sent successfully")
        }
		waitForCondition(fmt.Sprintf("http://%s/checkConnectionsStatus", proxyRoute))
        //time.Sleep(180 * time.Second)
}

func waitForCondition(url string) {
	timeout := time.After(30 * time.Minute)
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-timeout:
			log.Fatalf("Timeout reached. Stopping further checks.")
			return
		case <-ticker.C:
			resp, err := http.Get(url)
			if err != nil {
				log.Fatalf("Failed to check status: %v", err)
				continue
			}

			body, err := ioutil.ReadAll(resp.Body)
			resp.Body.Close()
			if err != nil {
				log.Fatalf("Failed to read response body: %v", err)
				continue
			}
			var response ProxyResponse
	        err = json.Unmarshal(body, &response)
		    if err != nil {
			    fmt.Println("Error decoding response:", err)
                return
			}

			log.Debugf("waitForCondition url: %v Server response: %v", url, response.Result)

			if response.Result == true {
				log.Debugf("waitForCondition url: %v succesful", url)
				return
			}
		}
	}
}

func processResults(n *netpolLatency) {
		serverURL := fmt.Sprintf("http://%s", proxyRoute)
        resp, err := http.Get(fmt.Sprintf("%s/stop", serverURL))
        if err != nil {
                log.Fatalf("Failed to retrieve results: %v", err)
        }

        if resp.StatusCode != http.StatusOK {
                log.Fatalf("Unexpected status code: %d", resp.StatusCode)
        }
        resp.Body.Close()
		// Wait till proxy got results from all pods
		waitForCondition(fmt.Sprintf("%s/checkStopStatus", serverURL))
        //time.Sleep(180 * time.Second)

        // Retrieve the results
        resp, err = http.Get(fmt.Sprintf("%s/results", serverURL))
        if err != nil {
                log.Fatalf("Failed to retrieve results: %v", err)
        }

        if resp.StatusCode != http.StatusOK {
                log.Fatalf("Unexpected status code: %d", resp.StatusCode)
        }
        body, err := ioutil.ReadAll(resp.Body)
        if err != nil {
                log.Fatalf("Failed to read response body: %v", err)
        }

        if err := json.Unmarshal(body, &clusterResults); err != nil {
                log.Fatalf("Failed to unmarshal results: %v", err)
        }
		log.Debugf("clusterResults %v", clusterResults)
        for pod, results := range clusterResults {
                for _, res := range results {
                        fmt.Printf("pod:%s Address: %s, Port: %d, IngressIdx: %v, NpName: %s Timestamp: %v\n", pod, res.Address, res.Port, res.IngressIdx, res.NpName, res.Timestamp)
						//m.VMReadyLatency = int(m.vmReady.Sub(m.Timestamp).Milliseconds())
						connLatency := float64(res.Timestamp.Sub(npCreationTime[res.NpName]).Milliseconds())
						fmt.Printf("Creation Time: %v Connection Time: %v Diff: %v Latency: %v\n", npCreationTime[res.NpName], res.Timestamp, res.Timestamp.Sub(npCreationTime[res.NpName]), connLatency)
						if _, ok := npResults[res.NpName]; !ok {
							npResults[res.NpName] =  []float64{}
						}
						npResults[res.NpName] = append(npResults[res.NpName], connLatency)
                }
        }
        resp.Body.Close()
		for name, npl := range npResults {
				fmt.Printf("name: %v latency slice: %v\n", name, npl)
				val, _ := stats.Percentile(npl, 50)
				p50 := int(val)
				val, _ = stats.Percentile(npl, 95)
				p95 := int(val)
				val, _ = stats.Percentile(npl, 99)
				p99 := int(val)
				val, _ = stats.Max(npl)
				maxVal := int(val)
				val, _ = stats.Mean(npl)
				avgVal := int(val)
				fmt.Printf("%s: 50th: %d 95th: %d 99th: %d max: %d avg: %d\n", name, p50, p95, p99, maxVal, avgVal)

				n.metrics.Store(name, netpolMetric{
					Name:              name,
					Timestamp:         npCreationTime[name],
					MetricName:        netpolLatencyMeasurement,
					ReadyLatency:      time.Duration(p99),
					UUID:              globalCfg.UUID,
					Metadata:          factory.metadata,
					JobName:           factory.jobConfig.Name,
				})
		}
}

func readTemplate(o kconfig.Object) ([]byte, error) {
	var err error
	var f io.Reader

	e := embed.FS{}
	if embedFS == e {
		f, err = kutil.ReadConfig(o.ObjectTemplate)
	} else {
		objectTemplate := path.Join(embedFSDir, o.ObjectTemplate)
		f, err = kutil.ReadEmbedConfig(embedFS, objectTemplate)
	}
	if err != nil {
		log.Fatalf("Error reading template %s: %s", o.ObjectTemplate, err)
	}
	t, err := io.ReadAll(f)
	if err != nil {
		log.Fatalf("Error reading template %s: %s", o.ObjectTemplate, err)
	}
	return t, err
}

func getObjectType(o kconfig.Object, t []byte) string {
	// Deserialize YAML
	cleanTemplate, err := prepareTemplate(t)
	if err != nil {
		log.Fatalf("Error preparing template %s: %s", o.ObjectTemplate, err)
	}
	uns := &unstructured.Unstructured{}
	yamlToUnstructured(o.ObjectTemplate, cleanTemplate, uns)
	return uns.GetKind()
}

func (n *netpolLatency) start(measurementWg *sync.WaitGroup) error {
	// Reset latency slices, required in multi-job benchmarks

	if factory.jobConfig.Name == "network-policy-perf" {
		prepareConnections()
		sendConnections()
	}
	n.latencyQuantiles, n.normLatencies = nil, nil
	defer measurementWg.Done()
	log.Infof("Creating netpol latency watcher for %s", factory.jobConfig.Name)
	n.netpolWatcher = metrics.NewNetworkWatcher(
		factory.clientSet,
		"netpolWatcher",
		corev1.NamespaceAll,
		func(options *metav1.ListOptions) {
			options.LabelSelector = fmt.Sprintf("kube-burner-uuid=%v", globalCfg.UUID)
		},
		cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc},
	)
	n.netpolWatcher.Informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: n.handleCreateNetpol,
	})
	n.podWatcher = metrics.NewWatcher(
		factory.clientSet.CoreV1().RESTClient().(*rest.RESTClient),
		"podWatcher",
		"pods",
		corev1.NamespaceAll,
		func(options *metav1.ListOptions) {
			options.LabelSelector = fmt.Sprintf("kube-burner-runid=%v", globalCfg.RUNID)
		},
		cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc},
	)
	// Use an endpoints lister to reduce and optimize API interactions in waitForEndpoints
	n.netpolLister = lnetv1.NewNetworkPolicyLister(n.netpolWatcher.Informer.GetIndexer())
	n.podLister = lcorev1.NewPodLister(n.podWatcher.Informer.GetIndexer())
	if err := n.netpolWatcher.StartAndCacheSync(); err != nil {
		log.Errorf("Network Policy Latency measurement error: %s", err)
	}
	if err := n.podWatcher.StartAndCacheSync(); err != nil {
		log.Errorf("Network Policy (podWatcher) Latency measurement error: %s", err)
	}
	return nil
}

func (n *netpolLatency) stop() error {
	if factory.jobConfig.Name == "network-policy-perf" {
		processResults(n)
	}
	_, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer func() {
		cancel()
		n.netpolWatcher.StopWatcher()
		n.podWatcher.StopWatcher()
	}()
	n.normalizeMetrics()
	for _, q := range n.latencyQuantiles {
		pq := q.(metrics.LatencyQuantiles)
		// Divide nanoseconds by 1e6 to get milliseconds
		log.Infof("%s: %s 50th: %d 99th: %d max: %d avg: %d", factory.jobConfig.Name, pq.QuantileName, pq.P50, pq.P99, pq.Max, pq.Avg)

	}
	return nil
}

func (n *netpolLatency) normalizeMetrics() {
	var latencies []float64
	sLen := 0
	n.metrics.Range(func(key, value interface{}) bool {
		sLen++
		metric := value.(netpolMetric)
		latencies = append(latencies, float64(metric.ReadyLatency))
		n.normLatencies = append(n.normLatencies, metric)
		return true
	})
	calcSummary := func(name string, inputLatencies []float64) metrics.LatencyQuantiles {
		latencySummary := metrics.NewLatencySummary(inputLatencies, name)
		latencySummary.UUID = globalCfg.UUID
		latencySummary.Timestamp = time.Now().UTC()
		latencySummary.Metadata = factory.metadata
		latencySummary.MetricName = netpolLatencyQuantilesMeasurement
		latencySummary.JobName = factory.jobConfig.Name
		return latencySummary
	}
	if sLen > 0 {
		n.latencyQuantiles = append(n.latencyQuantiles, calcSummary("Ready", latencies))
	}
}

func (n *netpolLatency) index(jobName string, indexerList map[string]indexers.Indexer) {
	metricMap := map[string][]interface{}{
		netpolLatencyMeasurement:          n.normLatencies,
		netpolLatencyQuantilesMeasurement: n.latencyQuantiles,
	}
	indexLatencyMeasurement(n.config, jobName, metricMap, indexerList)
}

func (n *netpolLatency) waitForPods(ns string, s labels.Selector) error {
	err := wait.PollUntilContextCancel(context.TODO(), 1000*time.Millisecond, true, func(ctx context.Context) (done bool, err error) {
		pods, err := n.podLister.Pods(ns).List(s)
		if err != nil {
			return false, nil
		}
		if len(pods) > 0 {
			for _, pod := range pods {
				if pod.Status.Phase != corev1.PodRunning {
					return false, nil
				}
			}
			return true, nil
		}
		return false, nil
	})
	return err
}

func (n *netpolLatency) collect(measurementWg *sync.WaitGroup) {
	defer measurementWg.Done()
}
