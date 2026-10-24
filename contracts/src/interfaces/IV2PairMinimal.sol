// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Uniswap v2 style pair (Sushi v2).
interface IV2PairMinimal {
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;

    function token0() external view returns (address);

    function token1() external view returns (address);
}
