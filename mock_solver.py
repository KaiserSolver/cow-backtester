#!/usr/bin/env python3
"""Mock CoW solver for TESTING cow-backtester's counterfactual wire path.
Not part of the tool. DTO-conformant (numeric id/gas, interactions list,
fee reported and executed reduced accordingly, /solve path only).

MODE env var:
  empty    -> 200 with no solutions                  (tests returned=0)
  limit    -> fills the first sell order exactly at its limit (valid, zero surplus)
  better   -> fills it at exactly 1.5x the limit     (valid, positive surplus)
  hex      -> like better but all numerics 0x-hex    (tests parser)
  garbage  -> 1000x prices                           (tests implausibility flag)
  invalid  -> prices violate the limit               (tests limit_violation -> INVALID)

Run: MODE=limit python3 mock_solver.py 8899
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.environ.get("MODE", "limit")


def build_solution(body):
    if MODE == "empty":
        return []
    for o in body.get("orders", []):
        if o.get("kind") != "sell":
            continue
        avail_sell = int(o["sellAmount"])
        ls = int(o.get("fullSellAmount", o["sellAmount"]))
        lb = int(o.get("fullBuyAmount", o["buyAmount"]))
        if avail_sell == 0 or ls == 0 or lb == 0:
            continue
        if MODE == "limit":
            # a fill at exactly the limit rate is only feasible with zero fee:
            # charging a fee at limit prices under-delivers for the gross
            # payment, and GPv2 would revert
            fee = 0
        else:
            fee = avail_sell // 1000
        executed = avail_sell - fee   # gross (executed+fee) == presented amount
        if executed <= 0:
            continue
        # price pairs chosen integer-exact per mode; rate = ps/pb vs limit lb/ls
        if MODE == "better" or MODE == "hex":
            ps, pb = 3 * lb, 2 * ls        # 1.5x limit
        elif MODE == "garbage":
            ps, pb = 1000 * lb, ls         # absurd
        elif MODE == "invalid":
            ps, pb = lb, 2 * ls            # 0.5x limit -> violates
        else:                              # limit
            ps, pb = lb, ls                # exactly at limit
        enc = hex if MODE == "hex" else str
        return [{
            "id": 0,
            "prices": {o["sellToken"]: enc(ps), o["buyToken"]: enc(pb)},
            "trades": [{"kind": "fulfillment", "order": o["uid"],
                        "executedAmount": enc(executed), "fee": enc(fee)}],
            "interactions": [],
            "gas": 150000,
        }]
    return []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if not self.path.rstrip("/").endswith("solve"):
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        out = json.dumps({"solutions": build_solution(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"mock solver MODE={MODE} on :{port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
