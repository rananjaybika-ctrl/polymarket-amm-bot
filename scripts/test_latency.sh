#!/bin/bash
# Polymarket Latency Test Script
# Run this on different VPS locations to compare latency
# Usage: chmod +x test_latency.sh && ./test_latency.sh

echo "=== Polymarket Latency Test ==="
echo "Location: $(curl -s ifconfig.me/city 2>/dev/null), $(curl -s ifconfig.me/country 2>/dev/null)"
echo "IP: $(curl -s ifconfig.me 2>/dev/null)"
echo ""

echo "Test 1: 10x HTTP Requests to CLOB API"
for i in {1..10}; do
  curl -o /dev/null -s -w "%{time_total}\n" "https://clob.polymarket.com/markets?next_cursor=MA=="
done | awk 'BEGIN{min=999;max=0;sum=0} {sum+=$1; if($1<min)min=$1; if($1>max)max=$1} END{printf "  Min: %.0fms\n  Max: %.0fms\n  Avg: %.0fms\n", min*1000, max*1000, (sum/NR)*1000}'

echo ""
echo "Test 2: 10x HTTP Requests to Gamma API"
for i in {1..10}; do
  curl -o /dev/null -s -w "%{time_total}\n" "https://gamma-api.polymarket.com/events?limit=1"
done | awk 'BEGIN{min=999;max=0;sum=0} {sum+=$1; if($1<min)min=$1; if($1>max)max=$1} END{printf "  Min: %.0fms\n  Max: %.0fms\n  Avg: %.0fms\n", min*1000, max*1000, (sum/NR)*1000}'

echo ""
echo "Test 3: DNS Lookup Time"
echo "  CLOB: $(dig +noall +stats clob.polymarket.com | grep 'Query time' | awk '{print $4}')ms"
echo "  Gamma: $(dig +noall +stats gamma-api.polymarket.com | grep 'Query time' | awk '{print $4}')ms"

echo ""
echo "Test 4: Detailed Timing Breakdown (CLOB)"
curl -o /dev/null -s -w "  DNS Lookup:    %{time_namelookup}s\n  TCP Connect:   %{time_connect}s\n  TLS Handshake: %{time_appconnect}s\n  First Byte:    %{time_starttransfer}s\n  Total Time:    %{time_total}s\n" "https://clob.polymarket.com/markets?next_cursor=MA=="

echo ""
echo "Test 5: Cloudflare Edge Location"
CF_RAY=$(curl -s -I "https://clob.polymarket.com" | grep -i "cf-ray" | awk '{print $2}')
echo "  cf-ray: $CF_RAY"
echo "  Edge: $(echo $CF_RAY | sed 's/.*-//')"

echo ""
echo "=== Test Complete ==="
