#!/usr/bin/env python3
"""
On-chain winner reconstruction for the CoW auction backtester.

Given a winning GPv2 settlement transaction, reconstructs — from PUBLIC data
only (calldata + receipt logs) — what the backtester needs:

  1. the auction id it settled (CoW's driver appends it as the last 8 bytes of
     the settle() calldata; autopilot reads exactly those 8 bytes back), and
  2. the winner's surplus per trade, on the tool's comparison basis (below).

Comparison basis (applied IDENTICALLY to winner and challenger):

    before-fee surplus over the SIGNED order limits, at UNIFORM clearing
    prices, converted to the native token at the auction's referencePrice
    of the surplus token.

  Why this basis:
  - CoW's official accounting scores surplus against the signed order amounts
    (the auction body's fullSellAmount/fullBuyAmount == the calldata trade
    amounts), NOT the remaining/fee-adjusted amounts presented to solvers.
  - Modern settlements encode per-trade CUSTOM (post-fee) prices; the token's
    FIRST occurrence in the tokens[] vector carries the UNIFORM (pre-fee)
    price — autopilot resolves uniform prices the same way. Scoring the winner
    at custom prices but a challenger at its response's uniform prices would
    bias every comparison pro-challenger by the winner's full fee load.
  - The surplus token is the BUY token for sell orders and the SELL token for
    buy orders (matches autopilot's accounting), converted at that token's
    native referencePrice: wei = atoms * referencePrice / 1e18.

  What this number IS: user surplus + protocol fees + network fee. CoW's
  official ranking score is user surplus + protocol fees only, so absolute
  levels here overstate the official score by the network-fee component (on
  the pinned dust-sized reference trade, 1471 vs an official 666 atoms; the
  gap shrinks as trades grow). Consequence: when two solutions carry very
  different fees this basis can order them differently than CoW would. It is
  the only basis we found that can be computed symmetrically offline: a /solve
  response carries no protocol-fee information to subtract, and removing fees
  from the winner alone would bias the comparison the other way.

Why on-chain and not the competition API: the v1 /solver_competition endpoints
were removed in the competition-data migration, and the v2 replacement is
retention-bounded. Reconstructing from chain data is trustless, independent of
API retention, and verifiable by anyone; decode integrity is validated to the
atom against the GPv2 Trade event (see --selftest / --unittest), and
`backtest.py --verify-api` cross-checks it against v2 where available.
"""

import json
import os
import time
import urllib.request

from eth_abi import decode, encode

from ._version import VERSION

# GPv2Settlement, same address on all chains.
SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
SETTLE_SELECTOR = "0x13d79a0b"
# Trade(address indexed owner, address sellToken, address buyToken,
#       uint256 sellAmount, uint256 buyAmount, uint256 feeAmount, bytes orderUid)
TRADE_TOPIC = "0xa07a543ab8a018198e99ca0184c93fe9050a79400a0a723441f84de1d972cc17"
# Autopilot reads the auction id from exactly the last 8 bytes of calldata
# (META_DATA_LEN = 8 in cowprotocol/services).
AUCTION_ID_SUFFIX_LEN = 8

# settle(address[] tokens, uint256[] clearingPrices, Trade[] trades,
#        Interaction[][3] interactions)
_TRADE = "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)"
_INTER = "(address,uint256,bytes)"
_SETTLE_TYPES = ["address[]", "uint256[]", f"{_TRADE}[]", f"{_INTER}[][3]"]

# GPv2 order flags (GPv2Trade.extractOrder): bit 0 = kind (0 sell / 1 buy),
# bit 1 = partiallyFillable. Higher bits: balance locations + signing scheme.
FLAG_KIND_BUY = 1
FLAG_PARTIALLY_FILLABLE = 2


def _ceildiv(a: int, b: int) -> int:
    return (a + b - 1) // b


def rpc(rpc_urls, method, params, tries=3):
    """Minimal JSON-RPC with endpoint rotation + backoff.

    Public RPCs rate-limit (esp. eth_getLogs); rotate endpoints each round and
    back off between rounds so a transient 429/timeout doesn't read as a hard
    failure. Returns None only after all endpoints fail every round."""
    if isinstance(rpc_urls, str):
        rpc_urls = [rpc_urls]
    for attempt in range(tries):
        for url in rpc_urls:
            try:
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode()
                req = urllib.request.Request(
                    url, body, {"Content-Type": "application/json", "User-Agent": f"cow-backtester/{VERSION}"}
                )
                out = json.load(urllib.request.urlopen(req, timeout=25))
                if out.get("result") is not None:
                    return out["result"]
            except Exception:
                continue
        if attempt < tries - 1:
            time.sleep(0.6 * (attempt + 1))
    return None


def decode_settlement(calldata_hex: str):
    """Decode a settle() calldata string.

    Returns dict with tokens[] (lowercased), clearing_prices[], trades[] (raw
    tuples), interactions, auction_id and suffix_len. The auction id is read
    from the LAST 8 BYTES of the appended suffix — the same rule autopilot
    uses — provided the calldata prefix re-encodes canonically and the suffix
    is at least 8 bytes; otherwise auction_id is None (caller should skip and
    count the settlement, not guess). Raises ValueError if not a settle() call.
    """
    if not calldata_hex.startswith("0x"):
        calldata_hex = "0x" + calldata_hex
    if calldata_hex[:10].lower() != SETTLE_SELECTOR:
        raise ValueError(f"not a GPv2 settle() call (selector {calldata_hex[:10]})")
    raw = bytes.fromhex(calldata_hex[10:])
    tokens, prices, trades, interactions = decode(_SETTLE_TYPES, raw)
    canon = encode(_SETTLE_TYPES, (tokens, prices, trades, interactions))
    suffix = raw[len(canon):]
    auction_id = None
    if raw[:len(canon)] == canon and len(suffix) >= AUCTION_ID_SUFFIX_LEN:
        auction_id = int.from_bytes(suffix[-AUCTION_ID_SUFFIX_LEN:], "big")
    return {
        "tokens": [t.lower() for t in tokens],
        "clearing_prices": list(prices),
        "trades": list(trades),
        "interactions": interactions,
        "auction_id": auction_id,
        "suffix_len": len(suffix),
    }


def uniform_price_indices(tokens, sell_token, buy_token):
    """Uniform clearing prices live at the FIRST occurrence of each token in
    tokens[]; later duplicate entries are per-trade custom (post-fee) prices.
    This mirrors autopilot's uniform-price resolution."""
    return tokens.index(sell_token), tokens.index(buy_token)


def gross_surplus_atoms(kind, executed, limit_sell, limit_buy, ps, pb):
    """Before-fee surplus of one trade, in SURPLUS-TOKEN atoms.

    kind='sell': executed is in sell atoms; surplus token = BUY token:
        bought - ceil(executed * limit_buy / limit_sell)
    kind='buy': executed is in buy atoms; surplus token = SELL token:
        floor(executed * limit_sell / limit_buy) - sold

    Rounding (ceil on the sell-order limit + bought, floor on the buy-order
    sold + limit) matches the official autopilot math. Returns 0 on degenerate
    inputs; negative surplus (limit violation) is returned AS-IS so callers
    can distinguish infeasible claims from genuine zero-surplus fills.
    """
    if not (ps and pb and limit_sell and limit_buy and executed):
        return 0
    if kind == "sell":
        bought = _ceildiv(executed * ps, pb)
        lim = _ceildiv(executed * limit_buy, limit_sell)
        return bought - lim
    else:
        sold = executed * pb // ps
        lim = executed * limit_sell // limit_buy
        return lim - sold


def surplus_token(kind, sell_token, buy_token):
    """Official surplus token: buy token for sell orders, sell token for buy."""
    return buy_token if kind == "sell" else sell_token


def to_native(atoms, ref_price):
    """Convert surplus-token atoms to native wei at the auction referencePrice."""
    if ref_price is None:
        return None
    return atoms * ref_price // 10**18


def fetch_settlement(rpc_urls, tx_hash):
    """Fetch a settlement tx's calldata + receipt facts."""
    t = rpc(rpc_urls, "eth_getTransactionByHash", [tx_hash])
    rc = rpc(rpc_urls, "eth_getTransactionReceipt", [tx_hash])
    if not t or not rc:
        return None
    return {
        "calldata": t["input"],
        "from": t.get("from", "").lower(),
        # tx.to distinguishes wrapper entrypoints (solver-owned router
        # contracts) from direct settle() calls, and doubles as the solver
        # identity for wrapper-routed settlements (their tx.from is a
        # throwaway relayer EOA).
        "to": (t.get("to") or "").lower(),
        "block": int(t["blockNumber"], 16),
        "gas_used": int(rc["gasUsed"], 16),
        "eff_gas_price": int(rc.get("effectiveGasPrice", "0x0"), 16),
        "status": rc["status"],
        "logs": rc["logs"],
    }


def trade_events(logs):
    """Extract executed amounts + orderUid from GPv2 Trade events — ground truth.

    Filters by BOTH topic0 and the emitting address: user-supplied pre-hooks
    execute inside settle() before any Trade event, so a foreign contract can
    emit a same-signature event that would otherwise shift the positional
    events[i] <-> trades[i] alignment.

    Data layout: 5 static words, bytes offset, length (56), then the 56-byte
    uid (112 hex chars starting at char 448)."""
    out = []
    for lg in logs:
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != TRADE_TOPIC:
            continue
        if lg.get("address", "").lower() != SETTLEMENT:
            continue
        d = lg["data"][2:]
        out.append({
            "owner": ("0x" + lg["topics"][1][-40:]).lower(),
            "sell_token": ("0x" + d[24:64]).lower(),
            "buy_token": ("0x" + d[64 + 24:128]).lower(),
            "sell_amount": int(d[128:192], 16),
            "buy_amount": int(d[192:256], 16),
            "uid": ("0x" + d[448:560]).lower(),
        })
    return out


def wrapper_settlement_surplus(events, body, ref_prices):
    """Surplus of a WRAPPER-routed settlement, from Trade events alone.

    Wrapper entrypoints (solver router contracts; ~40% of Base/mainnet
    settlements, Aug 2026) hide the inner settle() calldata, so uniform
    clearing prices and calldata limits are unavailable. This scores the
    DELIVERED execution — the Trade event's executed amounts, i.e. custom
    prices, after the settlement's fee take — against the auction body's
    signed limits, valued at referencePrice.

    Basis note: this UNDER-states the direct-path basis (uniform prices,
    before-fee) by exactly the per-trade fee wedge, typically a few bps.
    Rows built from it are tagged entry='wrapper' so consumers can tell the
    bases apart; the alternative was deleting these settlements entirely,
    which mis-sized the field by ~40% with a 12x size bias.

    Rounding matches the direct path: `gross_surplus_atoms` is reused with
    the event's own executed pair substituted as the price ratio, so for a
    sell order it degenerates to `buy_amount - ceil(executed*limit)` — the
    same ceil/floor conventions as the official math.

    JIT trades (uid not in the body) cannot be scored without calldata
    limits and are tallied under jit_excluded, including surplus-capturing
    ones — a small stated under-count, never a misattribution.
    """
    by_uid = {o["uid"].lower(): o for o in body.get("orders", [])}
    total = 0
    per_trade = []
    skipped_no_refprice = 0
    jit_excluded = 0
    anomalies = 0
    for ev in events:
        o = by_uid.get(ev["uid"])
        if o is None:
            jit_excluded += 1
            continue
        kind = "buy" if o.get("kind") == "buy" else "sell"
        limit_sell = to_u256_str(o.get("fullSellAmount") or o.get("sellAmount"))
        limit_buy = to_u256_str(o.get("fullBuyAmount") or o.get("buyAmount"))
        executed = ev["sell_amount"] if kind == "sell" else ev["buy_amount"]
        # Substituting the executed pair as the price ratio makes
        # gross_surplus_atoms return delivered-vs-limit with identical
        # rounding: sell -> buy_amount - ceil(executed*limit_buy/limit_sell).
        s_atoms = gross_surplus_atoms(kind, executed, limit_sell, limit_buy,
                                      ev["buy_amount"], ev["sell_amount"])
        if s_atoms < 0:
            anomalies += 1
            s_atoms = 0
        stok = surplus_token(kind, ev["sell_token"], ev["buy_token"])
        ref = ref_prices.get(stok)
        if ref is None:
            skipped_no_refprice += 1
            per_trade.append({"uid": ev["uid"], "owner": ev["owner"],
                              "sell": ev["sell_token"], "buy": ev["buy_token"],
                              "kind": kind, "executed": executed,
                              "surplus_wei": None, "fee_wei": None})
            continue
        s_wei = to_native(s_atoms, ref)
        total += s_wei
        per_trade.append({"uid": ev["uid"], "owner": ev["owner"],
                          "sell": ev["sell_token"], "buy": ev["buy_token"],
                          "kind": kind, "executed": executed,
                          "surplus_wei": s_wei, "fee_wei": None})
    return {"total_surplus_wei": total, "total_fee_wei": 0,
            "per_trade": per_trade, "jit_excluded": jit_excluded,
            "skipped_no_refprice": skipped_no_refprice, "anomalies": anomalies}


def to_u256_str(v):
    """Body amounts arrive as decimal strings; tolerate ints. None -> 0.
    Range-checked: auction bodies are trusted-ish, but an out-of-U256 value
    is corrupt data either way and must not reach the arithmetic."""
    if v is None:
        return 0
    n = int(v) if not isinstance(v, str) else int(v, 16) if v.startswith("0x") else int(v)
    if n < 0 or n > (1 << 256) - 1:
        raise ValueError(f"out of U256 range: {v!r}")
    return n


def winner_settlement_surplus(decoded, events, body, ref_prices):
    """Surplus of one winning settlement on the tool's comparison basis.

    Per trade: UNIFORM clearing prices (first-occurrence index), SIGNED limits
    (the calldata trade amounts == body fullSellAmount/fullBuyAmount), executed
    amount from calldata with the CONTRACT's fill-or-kill substitution (GPv2
    ignores calldata executedAmount for FoK orders and uses the signed amount
    — this reports what the chain actually did; note autopilot's own surplus
    accounting reads the calldata value as-is, so the two can differ for a
    non-canonically-encoded FoK trade).

    Which trades count (official accounting): trades on auction orders (uid in
    the body), plus JIT orders whose owner is in the body's
    surplusCapturingJitOrderOwners. Other JIT trades score zero and are tallied
    in jit_excluded.

    Returns error='trade_event_mismatch' when the receipt's Trade events don't
    align 1:1 with calldata trades (never silently misattributes).
    Also reports per-trade fee_wei = (uniform - custom) delivery valued at the
    referencePrice — the settlement's total fee take, a useful diagnostic.
    """
    trades = decoded["trades"]
    if len(events) != len(trades):
        return {"error": "trade_event_mismatch",
                "detail": f"{len(events)} Trade events vs {len(trades)} calldata trades"}
    tokens, prices = decoded["tokens"], decoded["clearing_prices"]
    by_uid = {o["uid"].lower(): o for o in body.get("orders", [])}
    jit_owners = {a.lower() for a in body.get("surplusCapturingJitOrderOwners", []) or []}

    total = 0
    total_fee = 0
    per_trade = []
    skipped_no_refprice = 0
    jit_excluded = 0
    uid_overlap = 0
    anomalies = 0   # negative uniform-basis surplus or fee wedge; possible
                    # only on mis-decodes or non-reference encoders

    for i, tr in enumerate(trades):
        sti, bti, _recv, cd_sell, cd_buy, _vt, _ad, _fee, flags, executed, _sig = tr
        st, bt = tokens[sti], tokens[bti]
        ev = events[i]
        kind = "buy" if (flags & FLAG_KIND_BUY) else "sell"
        pf = bool(flags & FLAG_PARTIALLY_FILLABLE)
        # GPv2 ignores calldata executedAmount for fill-or-kill orders.
        if not pf:
            executed = cd_sell if kind == "sell" else cd_buy

        in_body = ev["uid"] in by_uid
        if in_body:
            uid_overlap += 1
        elif ev["owner"] not in jit_owners:
            jit_excluded += 1
            continue

        u_si, u_bi = uniform_price_indices(tokens, st, bt)
        s_atoms = gross_surplus_atoms(kind, executed, cd_sell, cd_buy,
                                      prices[u_si], prices[u_bi])
        if s_atoms < 0:
            # implies a negative fee wedge; count it rather than clamp silently
            anomalies += 1
            s_atoms = 0
        stok = surplus_token(kind, st, bt)
        ref = ref_prices.get(stok)
        if ref is None:
            skipped_no_refprice += 1
            per_trade.append({"uid": ev["uid"], "owner": ev["owner"], "sell": st, "buy": bt,
                              "kind": kind, "executed": executed,
                              "surplus_wei": None, "fee_wei": None})
            continue
        # Fee diagnostic: uniform-vs-custom delivery difference, in surplus-token atoms.
        if kind == "sell":
            fee_atoms = (_ceildiv(executed * prices[u_si], prices[u_bi])
                         - _ceildiv(executed * prices[sti], prices[bti]))
        else:
            fee_atoms = (executed * prices[bti] // prices[sti]
                         - executed * prices[u_bi] // prices[u_si])
        if fee_atoms < 0:
            anomalies += 1
            fee_atoms = 0
        s_wei = to_native(s_atoms, ref)
        f_wei = to_native(fee_atoms, ref)
        total += s_wei
        total_fee += f_wei
        per_trade.append({"uid": ev["uid"], "owner": ev["owner"], "sell": st, "buy": bt,
                          "kind": kind, "executed": executed,
                          "surplus_wei": s_wei, "fee_wei": f_wei})

    return {"total_surplus_wei": total, "total_fee_wei": total_fee,
            "per_trade": per_trade, "skipped_no_refprice": skipped_no_refprice,
            "jit_excluded": jit_excluded, "uid_overlap": uid_overlap,
            "anomalies": anomalies}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test_surplus_math():
    """Deterministic unit vectors for gross_surplus_atoms — both directions."""
    # SELL: sell 100 A, limit >=150 B; uniform gives 200 B -> +50 B (buy atoms).
    assert gross_surplus_atoms("sell", 100, 100, 150, 2, 1) == 50
    # BUY: buy 100 B, limit <=200 A; uniform sells only 150 A -> +50 A (sell atoms).
    assert gross_surplus_atoms("buy", 100, 200, 100, 2, 3) == 50
    # At-limit fills: zero surplus, both directions.
    assert gross_surplus_atoms("sell", 100, 100, 150, 3, 2) == 0
    assert gross_surplus_atoms("buy", 100, 200, 100, 1, 2) == 0
    # Limit violation comes back NEGATIVE (callers decide clamp vs invalid).
    assert gross_surplus_atoms("sell", 100, 100, 150, 1, 1) < 0
    # Surplus-token routing.
    assert surplus_token("sell", "a", "b") == "b"
    assert surplus_token("buy", "a", "b") == "a"
    print("UNIT PASS: surplus math (sell/buy/at-limit/violation/surplus-token)")


def _test_fixture_decode():
    """Offline decode of the pinned reference settlement (no network).
    Skips (with a notice) when fixtures/ is absent — i.e. on a pip-installed
    copy; run from a source checkout (or pytest) for the full check.

    Arbitrum tx 0xc103e7f2..., auction 8339027: USDC->USDT partial fill.
    Pinned from on-chain observation 2026-08-06:
      custom bought  == Trade event buyAmount == 2385773 (decode integrity)
      uniform bought == 2386578; before-fee surplus == 1471 atoms; fee 805.
    """
    fx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fixtures", "settle_8339027_calldata.hex")
    if not os.path.exists(fx):
        print("UNIT SKIP: fixture decode (fixtures/ not shipped in installed "
              "distributions — run from a source checkout for the full check)")
        return
    dec = decode_settlement(open(fx).read().strip())
    assert dec["auction_id"] == 8339027, dec["auction_id"]
    assert dec["suffix_len"] == 8, dec["suffix_len"]
    assert len(dec["trades"]) == 1
    tr = dec["trades"][0]
    sti, bti, _r, cd_sell, cd_buy, _vt, _ad, _f, flags, executed, _s = tr
    assert (flags & FLAG_KIND_BUY) == 0 and bool(flags & FLAG_PARTIALLY_FILLABLE)
    assert (cd_sell, cd_buy) == (10000000000000, 9998500224966)
    toks, pr = dec["tokens"], dec["clearing_prices"]
    # decode integrity: custom-price delivery == on-chain Trade event buyAmount
    assert _ceildiv(executed * pr[sti], pr[bti]) == 2385773
    # comparison basis: uniform prices at first occurrence + signed limits
    u_si, u_bi = uniform_price_indices(toks, toks[sti], toks[bti])
    assert (u_si, u_bi) == (0, 1)
    s = gross_surplus_atoms("sell", executed, cd_sell, cd_buy, pr[u_si], pr[u_bi])
    assert s == 1471, s
    fee = _ceildiv(executed * pr[u_si], pr[u_bi]) - _ceildiv(executed * pr[sti], pr[bti])
    assert fee == 805, fee
    print("UNIT PASS: fixture decode (auction id 8339027; custom==event 2385773; "
          "before-fee surplus 1471; fee take 805) — offline")


def unittest():
    _test_surplus_math()
    _test_fixture_decode()
    return True


def selftest(rpc_urls):
    """Network self-test: refetch the reference settlement + its S3 body and
    run the full winner_settlement_surplus path end-to-end."""
    unittest()
    import gzip
    tx = "0xc103e7f2f31f6c302de778dda0fc5c10c8ec27e638b97fb47442235b305075cc"
    s = fetch_settlement(rpc_urls, tx)
    assert s, "could not fetch settlement (RPC down?)"
    dec = decode_settlement(s["calldata"])
    assert dec["auction_id"] == 8339027
    ev = trade_events(s["logs"])
    assert len(ev) == len(dec["trades"]) == 1
    assert ev[0]["buy_amount"] == 2385773
    url = "https://solver-instances.s3.amazonaws.com/prod/arbitrum-one/auction/8339027.json"
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": f"cow-backtester/{VERSION}"}), timeout=20).read()
    body = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    refp = {k.lower(): int(v["referencePrice"]) for k, v in body["tokens"].items()
            if v.get("referencePrice") is not None}
    w = winner_settlement_surplus(dec, ev, body, refp)
    assert "error" not in w, w
    assert w["uid_overlap"] == 1 and w["jit_excluded"] == 0
    pt = w["per_trade"][0]
    usdt = pt["buy"]
    assert pt["surplus_wei"] == 1471 * refp[usdt] // 10**18
    assert pt["fee_wei"] == 805 * refp[usdt] // 10**18
    print(f"SELFTEST PASS: winner path end-to-end — auction 8339027, surplus "
          f"{pt['surplus_wei']} wei (1471 atoms), fee {pt['fee_wei']} wei (805 atoms), "
          f"submitter {s['from'][:10]}..")
    return True


if __name__ == "__main__":
    import sys
    RPCS = ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"]
    if len(sys.argv) > 1 and sys.argv[1] == "--unittest":
        unittest()
    elif len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest(RPCS)
    elif len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(VERSION)
    else:
        print("usage: scorer.py --unittest | --selftest | --version   (or import as a module)")
