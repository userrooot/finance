// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IV3PoolMinimal} from "../interfaces/IV3PoolMinimal.sol";
import {IV2PairMinimal} from "../interfaces/IV2PairMinimal.sol";

/// @notice TEST ONLY (local Hardhat fork). Trades from its own balance to move a pool's price, i.e. to build an
/// arbitrage gap on the fork. Not part of the product.
contract ForkSwapper {
    using SafeERC20 for IERC20;

    address private _cur;

    function v3Swap(address pool, bool zeroForOne, uint256 amountIn) external returns (uint256 out) {
        _cur = pool;
        (int256 a0, int256 a1) = IV3PoolMinimal(pool).swap(
            address(this),
            zeroForOne,
            int256(amountIn),
            zeroForOne ? 4295128740 : 1461446703485210103287273052203988822378723970341,
            ""
        );
        _cur = address(0);
        out = uint256(-(zeroForOne ? a1 : a0));
    }

    function v2Swap(address pair, bool zeroForOne, uint256 amountIn, uint256 feeBps) external returns (uint256 out) {
        (uint112 r0, uint112 r1, ) = IV2PairMinimal(pair).getReserves();
        (uint256 rIn, uint256 rOut) = zeroForOne ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 g = amountIn * (10000 - feeBps);
        out = (g * rOut) / (rIn * 10000 + g);
        address tin = zeroForOne ? IV2PairMinimal(pair).token0() : IV2PairMinimal(pair).token1();
        IERC20(tin).safeTransfer(pair, amountIn);
        IV2PairMinimal(pair).swap(zeroForOne ? 0 : out, zeroForOne ? out : 0, address(this), "");
    }

    function _pay(int256 a0, int256 a1) internal {
        require(msg.sender == _cur, "ForkSwapper: bad caller");
        if (a0 > 0) IERC20(IV3PoolMinimal(msg.sender).token0()).safeTransfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IV3PoolMinimal(msg.sender).token1()).safeTransfer(msg.sender, uint256(a1));
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        _pay(a0, a1);
    }

    function pancakeV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        _pay(a0, a1);
    }
}
