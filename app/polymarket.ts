import { ApiError, ClobClient, AssetType, OpenOrdersResponse, OrderType, Side, UserMarketOrder, UserOrder } from "@polymarket/clob-client";
import { createPublicClient, createWalletClient, http, encodeFunctionData, parseAbi, parseUnits } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import axios from "axios";
import * as dotenv from "dotenv";
import { RelayClient, RelayerTxType } from "@polymarket/builder-relayer-client";
import { BuilderConfig } from "@polymarket/builder-signing-sdk";

dotenv.config();

function getPolygonRpcCandidates() {
  const configured = (process.env.POLYGON_RPC_URL || "").trim();
  const extraConfigured = (process.env.POLYGON_RPC_URLS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const candidates = [
    configured,
    ...extraConfigured,
    "https://polygon.drpc.org",
    "https://tenderly.rpc.polygon.community",
    "https://polygon.publicnode.com",
  ].filter(Boolean);

  const seen = new Set<string>();
  return candidates.filter((url) => {
    const key = url.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export class PolyMarketAPI {
  private client: ClobClient | null = null;
  private readonly dataApiUrl = "https://data-api.polymarket.com";
  private readonly marketLookupCache = new Map<string, string>();
  private privateKey?: string;
  private funderAddress?: string;
  private signatureType?: number;
  private readonly usdcAddress = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
  private readonly pusdAddress = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB";
  private readonly collateralOnrampAddress = "0x93070a847efEf7F70739046A929D47a521F5B8ee";
  private readonly collateralOfframpAddress = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854";
  private readonly conditionalTokensAddress = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
  constructor(
    creds: { key: string; secret: string; passphrase: string },
    privateKey?: string,
    options?: { funderAddress?: string | null; signatureType?: number | null }
  ) {
    this.privateKey = privateKey;
    this.funderAddress = options?.funderAddress || undefined;
    this.signatureType = options?.signatureType ?? undefined;
    this.initClient(creds, privateKey);
  }

  private initClient(creds: { key: string; secret: string; passphrase: string }, privateKey?: string) {
    const host = "https://clob.polymarket.com";
    const chainId = 137;
    
    let signer = undefined;
    if (privateKey) {
      const formattedPK = privateKey.startsWith("0x") ? (privateKey as `0x${string}`) : (`0x${privateKey}` as `0x${string}`);
      const account = privateKeyToAccount(formattedPK);
      signer = createWalletClient({
        account,
        chain: polygon,
        transport: http()
      });
    }

    this.client = new ClobClient(
      host,
      chainId,
      signer as any,
      creds,
      this.signatureType as any,
      this.funderAddress,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      true
    );
  }

  private getSignerWalletClient() {
    if (!this.privateKey) throw new Error("Private Key not found");

    const formattedPK = this.privateKey.startsWith("0x")
      ? (this.privateKey as `0x${string}`)
      : (`0x${this.privateKey}` as `0x${string}`);
    const account = privateKeyToAccount(formattedPK);
    const walletClient = createWalletClient({
      account,
      chain: polygon,
      transport: http(getPolygonRpcCandidates()[0]),
    });

    return { account, walletClient };
  }

  getSignerAddress() {
    const { account } = this.getSignerWalletClient();
    return account.address;
  }

  getConfiguredFunderAddress() {
    return this.funderAddress || this.getSignerAddress();
  }

  async getWalletPusdBalance(address: string) {
    const abi = parseAbi(["function balanceOf(address owner) view returns (uint256)"]);
    let lastError: any = null;

    for (const rpcUrl of getPolygonRpcCandidates()) {
      try {
        const publicClient = createPublicClient({
          chain: polygon,
          transport: http(rpcUrl),
        });

        return await publicClient.readContract({
          address: this.pusdAddress as `0x${string}`,
          abi,
          functionName: "balanceOf",
          args: [address as `0x${string}`],
        });
      } catch (e: any) {
        lastError = e;
        console.warn(`[POLY] RPC read failed on ${rpcUrl}: ${e.message}`);
      }
    }

    throw lastError || new Error("All Polygon RPC endpoints failed.");
  }

  async getWalletUsdcBalance(address: string) {
    return this.getWalletPusdBalance(address);
  }

  private getRelayerClient() {
    const key = (process.env.POLY_BUILDER_API_KEY || "").trim();
    const secret = (process.env.POLY_BUILDER_SECRET || "").trim();
    const passphrase = (process.env.POLY_BUILDER_PASSPHRASE || "").trim();
    if (!key || !secret || !passphrase) {
      return null;
    }

    const { walletClient } = this.getSignerWalletClient();
    const builderConfig = new BuilderConfig({
      localBuilderCreds: { key, secret, passphrase },
    });
    const txType = this.signatureType === 1 ? RelayerTxType.PROXY : RelayerTxType.SAFE;
    const relayerUrl = (process.env.POLY_RELAYER_URL || "https://relayer-v2.polymarket.com/").trim();

    return new RelayClient(relayerUrl, 137, walletClient as any, builderConfig as any, txType);
  }

  async getBalance(retries = 3) {
    if (!this.client) throw new Error("Client not initialized");
    
    for (let i = 0; i < retries; i++) {
        try {
            await this.client.updateBalanceAllowance({
                asset_type: AssetType.COLLATERAL,
            });
            return await this.client.getBalanceAllowance({
                asset_type: AssetType.COLLATERAL,
            });
        } catch (error: any) {
            if (i === retries - 1) throw error;
            console.warn(`[POLY] Balance fetch failed (Attempt ${i+1}/${retries}): ${error.message}. Retrying...`);
            await new Promise(res => setTimeout(res, 2000)); 
        }
    }
    throw new Error("Failed to fetch balance after retries");
  }

  async approveCollateral() {
    const spenders = [
      "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045", // CTF
      "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", // Exchange
      "0xC5d563A36AE78145C45a50134d48A1215220f80a", // Neg Risk CTF Exchange
      "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", // Legacy/adapter path seen in current bot
    ];
    const abi = parseAbi(["function approve(address spender, uint256 amount) public returns (bool)"]);
    const infinite = BigInt("0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");

    if (this.signatureType && this.signatureType !== 0 && this.funderAddress) {
      const relayer = this.getRelayerClient();
      if (!relayer) {
        throw new Error(
          "Proxy wallet approval needs relayer credentials. Set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, and POLY_BUILDER_PASSPHRASE."
        );
      }

      if (this.signatureType === 2) {
        const deployed = await relayer.getDeployed(this.funderAddress);
        if (!deployed) {
          console.log(`[EXEC] Deploying safe for funder ${this.funderAddress} via relayer...`);
          const deployment = await relayer.deploy();
          const deploymentResult = await deployment.wait();
          if (!deploymentResult) {
            throw new Error("Safe deployment failed through relayer.");
          }
        }
      }

      const txs = spenders.map((spender) => ({
        to: this.pusdAddress,
        data: encodeFunctionData({
          abi,
          functionName: "approve",
          args: [spender as `0x${string}`, infinite],
        }),
        value: "0",
      }));

      console.log(`[EXEC] Sending gasless approvals for funder ${this.funderAddress}...`);
      const response = await relayer.execute(txs, "Approve Polymarket spenders");
      const result = await response.wait();
      if (!result?.transactionHash) {
        throw new Error("Gasless approval did not confirm.");
      }
      return [result.transactionHash];
    }

    const { account, walletClient } = this.getSignerWalletClient();
    console.log(`[EXEC] Sending direct approvals for ${account.address}...`);

    const hashes = [];
    for (const spender of spenders) {
      const hash = await walletClient.sendTransaction({
        to: this.pusdAddress as `0x${string}`,
        data: encodeFunctionData({
          abi,
          functionName: "approve",
          args: [spender as `0x${string}`, infinite],
        }),
      });
      hashes.push(hash);
      await new Promise((res) => setTimeout(res, 1000));
    }

    return hashes;
  }

  async approveUSDC() {
    return this.approveCollateral();
  }

  async transferPusdToFunder(amount: number) {
    if (!this.funderAddress) {
      throw new Error("No funder address is configured for this user.");
    }

    const signerAddress = this.getSignerAddress();
    if (signerAddress.toLowerCase() === this.funderAddress.toLowerCase()) {
      throw new Error("Signer wallet and funder wallet are already the same address.");
    }
    if (!(amount > 0)) {
      throw new Error("Amount must be greater than zero.");
    }

    const { walletClient } = this.getSignerWalletClient();
    const abi = parseAbi(["function transfer(address to, uint256 amount) returns (bool)"]);
    const amountUnits = parseUnits(String(amount), 6);

    return walletClient.sendTransaction({
      to: this.pusdAddress as `0x${string}`,
      data: encodeFunctionData({
        abi,
        functionName: "transfer",
        args: [this.funderAddress as `0x${string}`, amountUnits],
      }),
    });
  }

  async transferUsdcToFunder(amount: number) {
    return this.transferPusdToFunder(amount);
  }

  async getPositions(userAddress: string) {
    const url = `${this.dataApiUrl}/positions?user=${userAddress}`;
    const response = await axios.get(url);
    return response.data;
  }

  async getOpenOrders(): Promise<OpenOrdersResponse> {
    if (!this.client) throw new Error("Client not initialized");
    return this.client.getOpenOrders();
  }

  async placeLimitOrder(tokenID: string, side: "BUY" | "SELL", price: number, size: number) {
    if (!this.client) throw new Error("Client not initialized");
    
    console.log(`[EXEC] Placing ${side} order for ${tokenID}: ${size} shares @ ${price}`);
    
    try {
      const book: any = await this.client.getOrderBook(tokenID);
      const tick = String(book?.tick_size || await this.client.getTickSize(tokenID));
      const negRisk = typeof book?.neg_risk === "boolean" ? book.neg_risk : await this.client.getNegRisk(tokenID);

      const order: UserOrder = {
        tokenID: tokenID,
        price: price,
        size: size,
        side: side === "BUY" ? Side.BUY : Side.SELL,
      };
      
      const res = await this.client.createAndPostOrder(
        order,
        { tickSize: tick as any, negRisk },
        OrderType.GTC
      );

      if (!res?.success || !res?.orderID) {
        const details = res?.errorMsg || res?.error || res?.status || JSON.stringify(res);
        throw new Error(`Order was not accepted by Polymarket: ${details}`);
      }

      console.log(`[EXEC] Order Submitted Successfully: ${res.orderID} (${res.status || "unknown"})`);
      return res;
    } catch (e: any) {
      if (e instanceof ApiError) {
        const details = typeof e.data === "object" ? JSON.stringify(e.data) : String(e.data || "");
        console.error(`[EXEC] Order Failed: ${e.message}${details ? ` | details=${details}` : ""}`);
      } else {
        console.error(`[EXEC] Order Failed: ${e.message}`);
      }
      throw e;
    }
  }

  async placeMarketOrder(tokenID: string, side: "BUY" | "SELL", amount: number) {
    if (!this.client) throw new Error("Client not initialized");

    console.log(`[EXEC] Placing ${side} market order for ${tokenID}: amount=${amount}`);

    try {
      const book: any = await this.client.getOrderBook(tokenID);
      const tick = String(book?.tick_size || await this.client.getTickSize(tokenID));
      const negRisk = typeof book?.neg_risk === "boolean" ? book.neg_risk : await this.client.getNegRisk(tokenID);

      const order: UserMarketOrder = {
        tokenID,
        amount,
        side: side === "BUY" ? Side.BUY : Side.SELL,
      };

      const res = await this.client.createAndPostMarketOrder(
        order,
        { tickSize: tick as any, negRisk },
        OrderType.FAK
      );

      if (!res?.success || !res?.orderID) {
        const details = res?.errorMsg || res?.error || res?.status || JSON.stringify(res);
        throw new Error(`Market order was not accepted by Polymarket: ${details}`);
      }

      console.log(`[EXEC] Market Order Submitted Successfully: ${res.orderID} (${res.status || "unknown"})`);
      return res;
    } catch (e: any) {
      if (e instanceof ApiError) {
        const details = typeof e.data === "object" ? JSON.stringify(e.data) : String(e.data || "");
        console.error(`[EXEC] Market Order Failed: ${e.message}${details ? ` | details=${details}` : ""}`);
      } else {
        console.error(`[EXEC] Market Order Failed: ${e.message}`);
      }
      throw e;
    }
  }

  async getMarketByConditionId(conditionId: string) {
    const url = `https://gamma-api.polymarket.com/markets?conditionId=${conditionId}`;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await axios.get(url, { timeout: 20000 });
        const markets = Array.isArray(res.data) ? res.data : [];
        const exact = markets.find((market: any) => String(market?.conditionId || "").toLowerCase() === String(conditionId).toLowerCase());
        return exact || markets[0];
      } catch (e: any) {
        if (attempt === 3) throw e;
        await new Promise((resolve) => setTimeout(resolve, attempt * 1000));
      }
    }
    return null;
  }

  async getMarketById(marketId: string) {
    const url = `https://gamma-api.polymarket.com/markets/${marketId}`;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await axios.get(url, { timeout: 20000 });
        return res.data;
      } catch (e: any) {
        if (attempt === 3) throw e;
        await new Promise((resolve) => setTimeout(resolve, attempt * 1000));
      }
    }
    return null;
  }

  async getPublicProfileByWallet(address: string) {
    const url = "https://gamma-api.polymarket.com/public-profile";
    const res = await axios.get(url, {
      params: { address },
    });
    return res.data;
  }

  async redeemWinnings(conditionId: string) {
    if (!this.privateKey) throw new Error("Private Key not found");

    const formattedPK = this.privateKey.startsWith("0x")
      ? (this.privateKey as `0x${string}`)
      : (`0x${this.privateKey}` as `0x${string}`);

    const account = privateKeyToAccount(formattedPK);
    const walletClient = createWalletClient({
      account,
      chain: polygon,
      transport: http()
    });

    const abi = parseAbi([
      "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)"
    ]);
    const zeroBytes32 = `0x${"0".repeat(64)}` as `0x${string}`;

    console.log(`[SETTLE] Redeeming winnings for condition ${conditionId}...`);
    return walletClient.sendTransaction({
      to: this.conditionalTokensAddress as `0x${string}`,
      data: encodeFunctionData({
        abi,
        functionName: "redeemPositions",
        args: [
          this.usdcAddress as `0x${string}`,
          zeroBytes32,
          conditionId as `0x${string}`,
          [1n, 2n]
        ]
      })
    });
  }
}
