"""ERC-8004 registry addresses and a minimal transaction sender for agent wallets."""
import time

import httpx
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

# Official deployments (github.com/erc-8004/erc-8004-contracts), checked on-chain.
NETWORKS = {
    "eip155:84532": {  # Base Sepolia (testnet)
        "rpc": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org",
        "identity": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
        "reputation": "0x8004B663056A597Dffe9eCcC1965A193B7388713",
    },
    "eip155:8453": {  # Base mainnet
        "rpc": "https://mainnet.base.org",
        "explorer": "https://basescan.org",
        "identity": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        "reputation": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
    },
}


def rpc(url: str, method: str, params: list):
    body = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30).json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def send(network: str, key: str, to: str, signature: str, types: list[str], args: list) -> dict:
    """Build, sign, send and wait for a contract call from the key's wallet (which pays gas)."""
    url = NETWORKS[network]["rpc"]
    account = Account.from_key(key)
    data = "0x" + (keccak(text=signature)[:4] + encode(types, args)).hex()
    tx = {
        "to": to, "data": data, "value": 0, "type": 2,
        "chainId": int(network.split(":")[1]),
        "nonce": int(rpc(url, "eth_getTransactionCount", [account.address, "pending"]), 16),
    }
    tx["gas"] = int(int(rpc(url, "eth_estimateGas", [{"from": account.address, "to": to, "data": data}]), 16) * 1.2)
    base_fee = int(rpc(url, "eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    tip = int(rpc(url, "eth_maxPriorityFeePerGas", []), 16)
    tx.update(maxPriorityFeePerGas=tip, maxFeePerGas=base_fee * 2 + tip)
    signed = account.sign_transaction(tx)
    tx_hash = rpc(url, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
    for _ in range(60):
        receipt = rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if receipt["status"] != "0x1":
                raise RuntimeError(f"transaction reverted: {tx_hash}")
            return receipt
        time.sleep(2)
    raise TimeoutError(tx_hash)
