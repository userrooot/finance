// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Solidly style pair (Camelot v2, Aerodrome v2). getReserves is never called on-chain.
interface ISolidlyPairMinimal {
    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256);

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;

    function token0() external view returns (address);

    function token1() external view returns (address);
}
