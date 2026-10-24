// Read-only check that mainnet forking works from this machine.
const { ethers, network } = require("hardhat");
async function main() {
  const block = await ethers.provider.getBlockNumber();
  const chainId = (await ethers.provider.getNetwork()).chainId;
  console.log(`forked ${process.env.FORK}: chainId=${chainId} block=${block}`);
  // Uniswap v3 factory exists on both chains at the same address
  const code = await ethers.provider.getCode("0x1F98431c8aD98523631AE4a59f267346ea31F984");
  console.log("uniswap v3 factory code bytes:", (code.length - 2) / 2);
}
main().catch((e) => { console.error(e.message.slice(0, 300)); process.exit(1); });
